"""
bucket_runner.py — 单桶策略主循环

把每个策略桶的扫描、持仓管理、入场判断从 portfolio_bot.py 中抽离，
让主入口只负责装配线程和生命周期。
"""

from __future__ import annotations

import time
from typing import cast

import pandas as pd
from moomoo import OpenQuoteContext, RET_OK

from entry_engine import build_signal_cascade, tiered_entry
from fundamental_model import score_slow_fundamentals
from fundamental_store import load_fundamental_cache
from market_utils import current_session, live_price_from_row, request_kline
from position_manager import manage_position
from screener import fundamental_score, volume_signal
from shared_state import (
    broker,
    positions,
    positions_lock,
    _dynamic_lock,
    _dynamic_watch,
    get_regime,
    in_reentry_cooldown,
    mark_worker_beat,
    sync_runtime_positions_from_broker,
)
from strategy_config import (
    BUCKET_LABEL,
    REENTRY_COOLDOWN_MINUTES,
    SECTOR_MAP,
    SLOW_FUND_MIN_SCORE,
    SLOW_FUND_TIER_FULL,
    SLOW_FUND_TIER_MID,
    TRADE_UNIVERSE,
)
from strategy_signals import calc_atr_value, detect_entry_signal, enrich_indicators, indicator_state


def _quality_context(
    stock: str,
    bucket_name: str,
    snap_row: pd.Series | None,
    fund_cache: dict,
) -> dict:
    sector = SECTOR_MAP.get(stock, '')
    if snap_row is not None:
        fund_sc, fund_notes = fundamental_score(snap_row)
    else:
        fund_sc, fund_notes = 5.0, ['快照缺失']

    slow_entry = fund_cache.get(stock)
    slow_eval = score_slow_fundamentals(
        stock,
        sector,
        slow_entry,
        snapshot=snap_row,
    )

    if slow_eval['available']:
        slow_sc = float(slow_eval['score'] or 0.0)
        if slow_sc < SLOW_FUND_MIN_SCORE[bucket_name]:
            return {
                'allowed': False,
                'reason': f"慢基本面不达标({slow_sc:.0f}/100)",
            }
        slow_notes = slow_eval.get('notes', [])
        return {
            'allowed': True,
            'fund_sc': fund_sc,
            'fund_notes': fund_notes,
            'quality_score': slow_sc / 10.0,
            'quality_note': f"慢分{slow_sc:.0f}/100({slow_notes[0] if slow_notes else '通过'})",
            'skip_fast_fund_gate': True,
        }

    return {
        'allowed': True,
        'fund_sc': fund_sc,
        'fund_notes': fund_notes,
        'quality_score': fund_sc,
        'quality_note': f"快分{fund_sc:.0f}/10({fund_notes[0] if fund_notes else '通过'})",
        'skip_fast_fund_gate': False,
    }


def run_bucket(name: str, cfg: dict):
    """外层包装：崩溃后自动重启。"""
    label = cfg['label']
    while True:
        _run_bucket_inner(name, cfg)
        mark_worker_beat(label, "异常退出，等待重启")
        print(f"[{label}] ⚠️ 线程异常退出，5秒后自动重启...")
        time.sleep(5)


def _run_bucket_inner(name: str, cfg: dict):
    min_bars = max(cfg['slow_ma'] + 5, 40)
    ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
    label = BUCKET_LABEL.get(name, cfg.get('label', name))
    print(f"[{label}] 启动  {len(cfg['stocks'])}只  最多{cfg['max_pos']}仓×{cfg['alloc']*100:.0f}%")
    mark_worker_beat(label, f"启动，周期{max(1, cfg['interval']//60)}m")

    try:
        while True:
            state = broker.get_state()
            cash = max(0.0, float(state['cash']) - float(state.get('reserved_cash', 0.0) or 0.0))
            initial_cash = state['initial_cash']

            sync_runtime_positions_from_broker()

            with _dynamic_lock:
                dyn = dict(_dynamic_watch)

            bucket_positions = [
                code for code, pos in state['positions'].items()
                if pos.get('bucket') == name
            ]
            stock_list = sorted(
                list(dict.fromkeys(cfg['stocks'] + bucket_positions)),
                key=lambda code: (
                    0 if code in TRADE_UNIVERSE else 1,
                    -dyn.get(code, -999.0),
                    code,
                ),
            )

            regime = get_regime()
            if regime == 'BEAR' and name != 'conservative':
                mark_worker_beat(label, "BEAR暂停")
                print(f"[{label}] BEAR 市场，暂停操作")
                time.sleep(cfg['interval'])
                continue

            snap_cache: dict[str, object] = {}
            sess = current_session()
            for i in range(0, len(stock_list), 20):
                chunk = stock_list[i:i + 20]
                ret_b, snap_b = ctx.get_market_snapshot(chunk)
                if ret_b != RET_OK:
                    continue
                snap_b = cast(pd.DataFrame, snap_b)
                for _, row in snap_b.iterrows():
                    code = str(row['code'])
                    snap_cache[f"{code}:row"] = row
                    px = live_price_from_row(row, session=sess)
                    if px > 0:
                        snap_cache[code] = px

            fund_cache = load_fundamental_cache()
            scanned = 0

            for stock in stock_list:
                try:
                    df = request_kline(ctx, stock, cfg['ktype'], min_bars)
                    if df is None:
                        continue

                    df = enrich_indicators(df, cfg)
                    atr_val = calc_atr_value(df)
                    latest = df.iloc[-1]
                    prev_row = df.iloc[-2]

                    live_price = float(snap_cache.get(stock, 0.0) or 0.0)
                    no_new_entry = live_price <= 0
                    price = live_price if live_price > 0 else float(latest['close'])
                    if no_new_entry:
                        print(f"[{label}] {stock} 实时价缺失（仅管理持仓，不开新仓）")

                    fast_now = float(latest['fast_ma'])
                    slow_now = float(latest['slow_ma_v'])
                    fast_prev = float(prev_row['fast_ma'])
                    slow_prev = float(prev_row['slow_ma_v'])
                    ma_death = fast_prev > slow_prev and fast_now < slow_now

                    ind = indicator_state(name, cfg, latest, prev_row)
                    add_signal = detect_entry_signal(
                        cfg, df, latest, prev_row,
                        fast_now, slow_now, fast_prev, slow_prev,
                        ind.relaxed_buy,
                    )

                    with positions_lock:
                        pos_data = dict(positions[stock]) if stock in positions else None
                    owns_position = pos_data is not None and pos_data.get('bucket') == name
                    held_by_other_bucket = pos_data is not None and pos_data.get('bucket') != name

                    if held_by_other_bucket:
                        continue

                    if owns_position:
                        _, cash = manage_position(
                            ctx, stock, pos_data, price, atr_val,
                            cfg, name, label, ind.ind_str,
                            ma_death, ind.extra_sell, add_signal,
                            cash, initial_cash, rsi_now=ind.rsi_now,
                        )
                        scanned += 1
                        continue

                    if no_new_entry:
                        continue

                    if in_reentry_cooldown(stock, REENTRY_COOLDOWN_MINUTES):
                        reason = broker.last_sell_reason(stock)
                        reason_note = f" 上次卖出:{reason}" if reason else ''
                        print(f"[{label}] {stock} 卖出后冷静期内，跳过{reason_note}")
                        continue

                    snap_row = cast(pd.Series | None, snap_cache.get(f"{stock}:row"))
                    if snap_row is not None:
                        vol_sig, vol_note = volume_signal(df)
                        if vol_sig == 'negative':
                            print(f"[{label}] {stock} 量价信号负面({vol_note})，观望")
                            continue
                    else:
                        vol_note = ''

                    quality = _quality_context(stock, name, snap_row, fund_cache)
                    if not quality['allowed']:
                        print(f"[{label}] {stock} {quality['reason']}，跳过")
                        continue

                    quality_score = float(quality['quality_score'])
                    allow_uptrend = (
                        quality_score >= (SLOW_FUND_TIER_FULL / 10.0)
                        or (
                            cfg.get('prefer_uptrend_entry')
                            and quality_score >= (SLOW_FUND_TIER_MID / 10.0)
                        )
                    )

                    signal = build_signal_cascade(
                        cfg, df, latest, prev_row,
                        fast_now, slow_now, fast_prev, slow_prev,
                        ind.extra_buy, snap_row, price,
                        allow_uptrend=allow_uptrend,
                    )
                    if signal is None:
                        continue

                    _, cash = tiered_entry(
                        ctx, stock, signal,
                        float(quality['fund_sc']),
                        cast(list, quality['fund_notes']),
                        vol_note,
                        cfg, name, label, ind.ind_str,
                        price, atr_val, cash, initial_cash,
                        no_new_entry=no_new_entry,
                        quality_score=quality_score,
                        skip_fast_fund_gate=bool(quality['skip_fast_fund_gate']),
                        quality_note=str(quality['quality_note']),
                    )
                    scanned += 1
                except Exception as e:
                    print(f"[{label}] {stock} 异常：{e}")

            mark_worker_beat(label, f"完成{scanned}只扫描")
            time.sleep(cfg['interval'])

    except Exception as e:
        mark_worker_beat(label, f"崩溃:{e}")
        print(f"[{label}] 崩溃：{e}")
    finally:
        ctx.close()
