"""
workers.py — 后台线程集合

包含所有非交易核心的守护线程：
  run_bot_heartbeat()       — 每分钟打印系统摘要
  run_order_matcher()       — 开放订单撮合与超时撤单
  run_fundamental_refresh() — 每日刷新慢速基本面缓存
  run_dynamic_screener()    — 定期对观察池全量评分
  run_micro_builder()       — 分散微量建仓 + 底仓止损
  run_weekly_dca()          — 每周定投执行
  run_regime_monitor()      — SPY/VIX 市场状态检测
  run_crash_monitor()       — 暴跌 / 极度超卖预警
  run_signal_stats_refresh() — 每日刷新信号统计 adj_factor
"""

from __future__ import annotations

import threading
import time
from typing import cast

import pandas as pd
from moomoo import AuType, KLType, OpenQuoteContext, RET_OK

from discussion_universe import build_watch_universe, load_discussion_universe
from execution_policy import cash_position_qty, entry_budget
from fundamental_store import refresh_fundamental_cache
from market_utils import live_price_from_row
from micro_portfolio import score_snapshot_row, select_diversified_micro_candidates
from order_reconciler import reconcile_open_orders
from recurring_invest import current_new_york_time, should_execute_weekly_dca, week_marker
from screener import fundamental_score, volume_signal
from shared_state import (
    broker,
    positions, positions_lock,
    _dynamic_watch, _dynamic_lock,
    _trailing_highs, _trail_lock,
    _profit_stages, _profit_lock,
    _worker_beats, _worker_lock,
    BOT_START_TS, HEARTBEAT_INTERVAL,
    count_bucket_positions, in_reentry_cooldown,
    mark_worker_beat, notify,
    runtime_position_snapshot, sync_runtime_positions_from_broker,
    get_regime, set_regime,
    update_circuit_breaker, get_circuit_breaker_status,
)
from strategy_config import (
    BASE_WATCH_UNIVERSE,
    BUCKET_LABEL,
    BUCKETS,
    CASH_RESERVE,
    CRASH_RSI_ALERT,
    CRASH_THRESHOLD,
    DISCUSSION_WATCH_LIMIT,
    DYNAMIC_INTERVAL,
    MICRO_ALLOC,
    MICRO_MAX_POS,
    MICRO_MIN_SCORE,
    MICRO_REENTRY_COOLDOWN_MINUTES,
    MICRO_REQUIRED_SECTORS,
    MICRO_SECTOR_CAP,
    MICRO_SECTOR_CAP_OVERRIDES,
    MICRO_SECTOR_UNIVERSE,
    MICRO_TARGET_POS,
    ORDER_MATCH_INTERVAL,
    ORDER_MIN_FILL_AGE_SECONDS,
    ORDER_TIMEOUT_SECONDS,
    SLOW_FUND_REFRESH_INTERVAL,
    WEEKLY_DCA_INTERVAL,
    WEEKLY_DCA_MIN_HOUR_ET,
    WEEKLY_DCA_PLAN,
    WEEKLY_DCA_WEEKDAY_ET,
)
from strategy_signals import calc_rsi


def _fmt_age(seconds: float) -> str:
    s = max(0, int(seconds))
    h, r = divmod(s, 3600); m, s2 = divmod(r, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s2:02d}s"


def _fmt_uptime(seconds: float) -> str:
    s = max(0, int(seconds))
    d, r = divmod(s, 86400); h, r2 = divmod(r, 3600); m = r2 // 60
    return f"{d}d{h}h{m:02d}m" if d else (f"{h}h{m:02d}m" if h else f"{m}m")


def _position_bucket_summary() -> str:
    from shared_state import positions
    counts: dict[str, int] = {}
    for v in positions.values():
        b = v.get('bucket', '?')
        counts[b] = counts.get(b, 0) + 1
    return ' '.join(f"{b}:{n}" for b, n in counts.items())


def _current_watch_universe() -> list[str]:
    return build_watch_universe(
        BASE_WATCH_UNIVERSE,
        load_discussion_universe(),
        extra_limit=DISCUSSION_WATCH_LIMIT,
    )


# ── 心跳 ────────────────────────────────────────────────────
def run_bot_heartbeat():
    from datetime import datetime
    import time as _time
    print(f"[心跳] 运行监控启动，每 {HEARTBEAT_INTERVAL // 60} 分钟输出一次摘要")
    while True:
        state  = broker.get_state()
        regime = get_regime()
        with _dynamic_lock:
            watch_count = len(_dynamic_watch)
        with _worker_lock:
            beats = dict(_worker_beats)

        worker_order = (['执行', '基本面', '筛选', '底仓', '定投', 'Regime', '预警']
                        + [cfg['label'] for cfg in BUCKETS.values()])
        parts = []
        for w in worker_order:
            beat = beats.get(w)
            if not beat:
                continue
            age    = _fmt_age(_time.time() - float(beat['ts']))
            detail = str(beat.get('detail', '')).strip()
            parts.append(f"{w}:{age}" + (f"({detail})" if detail else ""))

        # 熔断检查：在心跳里更新高水位，新触发时发出告警
        cb_triggered, cb_note = update_circuit_breaker()
        if cb_triggered:
            print(f"[心跳] 🚨 {cb_note}")
            notify("🚨 组合熔断触发", cb_note, modal=True)
        cb_status = get_circuit_breaker_status()

        print(
            f"[心跳] {datetime.now().strftime('%H:%M:%S')} 运行中"
            f"  uptime={_fmt_uptime(_time.time() - BOT_START_TS)}"
            f"  regime={regime}"
            f"  现金=${state['cash']:,.0f}"
            f"  持仓={len(state['positions'])} ({_position_bucket_summary()})"
            f"  watch={watch_count}"
            + (f"  [{cb_status}]" if cb_status else "")
        )
        if parts:
            print("[心跳] 线程状态:", " | ".join(parts))
        _time.sleep(HEARTBEAT_INTERVAL)


def run_order_matcher():
    ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
    seen_fill_ids = {
        str(fill.get('fill_id', '') or '')
        for fill in broker.get_fills()
    }
    seen_terminal_orders = {
        str(order.get('order_id', '') or '')
        for order in broker.get_orders()
        if str(order.get('status', '') or '') in {'CANCELED', 'REJECTED'}
    }
    print(f"[执行] 撮合线程启动，每 {ORDER_MATCH_INTERVAL}s 检查开放订单")
    mark_worker_beat('执行', f"启动，周期{ORDER_MATCH_INTERVAL}s")
    try:
        while True:
            state = broker.get_state()
            open_orders = [
                order for order in state.get('orders', [])
                if str(order.get('status', '') or '') in {'NEW', 'PARTIALLY_FILLED'}
            ]

            price_map: dict[str, float] = {}
            if open_orders:
                codes = sorted({str(order.get('code', '') or '') for order in open_orders})
                ret, snap = ctx.get_market_snapshot(codes)
                if ret == RET_OK:
                    snap = cast(pd.DataFrame, snap)
                    price_map = {
                        str(row['code']): live_price_from_row(row)
                        for _, row in snap.iterrows()
                    }

            _ = reconcile_open_orders(
                broker,
                price_map,
                min_fill_age_seconds=ORDER_MIN_FILL_AGE_SECONDS,
                timeout_seconds=ORDER_TIMEOUT_SECONDS,
            )

            state = broker.get_state()
            state_changed = False
            new_fills = 0
            for fill in state.get('fills', []):
                fill_id = str(fill.get('fill_id', '') or '')
                if not fill_id or fill_id in seen_fill_ids:
                    continue
                seen_fill_ids.add(fill_id)
                new_fills += 1
                state_changed = True

                code = str(fill.get('code', '') or '').replace('US.', '')
                side = str(fill.get('side', '') or '').upper()
                code_full = str(fill.get('code', '') or '')
                bucket = BUCKET_LABEL.get(str(fill.get('bucket', '') or ''), str(fill.get('bucket', '') or ''))
                qty = int(fill.get('qty', 0) or 0)
                price = float(fill.get('price', 0.0) or 0.0)
                reason = str(fill.get('reason', '') or '')
                if reason == 'profit_take1':
                    stages = broker.get_profit_stages(code_full)
                    if 1 not in stages:
                        stages.add(1)
                        broker.update_profit_stages(code_full, stages)
                elif reason == 'profit_take2':
                    stages = broker.get_profit_stages(code_full)
                    if 2 not in stages:
                        stages.add(2)
                        broker.update_profit_stages(code_full, stages)
                if side == 'BUY':
                    print(f"[执行] ✅ {bucket} {code} 买入成交 {qty}股 @ ${price:.2f}"
                          f"  原因={reason}")
                else:
                    pnl = fill.get('pnl')
                    pnl_text = (
                        f"  盈亏 ${float(pnl):+.2f}"
                        if pnl not in (None, '', 'None') else ''
                    )
                    print(f"[执行] 🔻 {bucket} {code} 卖出成交 {qty}股 @ ${price:.2f}"
                          f"{pnl_text}  原因={reason}")

            terminal_events = 0
            for order in state.get('orders', []):
                order_id = str(order.get('order_id', '') or '')
                status = str(order.get('status', '') or '')
                if status not in {'CANCELED', 'REJECTED'} or order_id in seen_terminal_orders:
                    continue
                seen_terminal_orders.add(order_id)
                terminal_events += 1
                state_changed = True
                code = str(order.get('code', '') or '').replace('US.', '')
                side = str(order.get('side', '') or '').upper()
                if status == 'REJECTED':
                    print(f"[执行] ❌ {code} {side} 订单拒绝  OID={order_id}  {order.get('message', '')}")
                else:
                    print(f"[执行] ⚪ {code} {side} 订单撤销  OID={order_id}  {order.get('message', '')}")

            if state_changed:
                sync_runtime_positions_from_broker()

            state = broker.get_state()
            open_after = [
                order for order in state.get('orders', [])
                if str(order.get('status', '') or '') in {'NEW', 'PARTIALLY_FILLED'}
            ]
            mark_worker_beat(
                '执行',
                f"开放{len(open_after)} 成交{new_fills} 撤/拒{terminal_events}"
                if (open_orders or new_fills or terminal_events)
                else "空闲",
            )
            time.sleep(ORDER_MATCH_INTERVAL)
    except Exception as e:
        mark_worker_beat('执行', f"异常:{e}")
        print(f"[执行] 异常：{e}")
    finally:
        ctx.close()


# ── 基本面日更 ───────────────────────────────────────────────
def run_fundamental_refresh():
    universe = _current_watch_universe()
    print(f"[基本面] 慢速基本面刷新线程启动（观察池 {len(universe)} 只）")
    mark_worker_beat('基本面', f"启动，观察池{len(universe)}只")
    while True:
        try:
            universe = _current_watch_universe()
            summary  = refresh_fundamental_cache(universe)
            mark_worker_beat('基本面',
                f"更新{summary['updated']} 跳过{summary['skipped']} "
                f"失败{summary['failed']} 扫描{len(universe)}")
            print(f"[基本面] 日更完成：更新{summary['updated']} 跳过{summary['skipped']} "
                  f"失败{summary['failed']} 扫描{len(universe)}只")
        except Exception as e:
            mark_worker_beat('基本面', f"异常:{e}")
            print(f"[基本面] 刷新异常：{e}")
        time.sleep(SLOW_FUND_REFRESH_INTERVAL)


# ── 动态筛选 ─────────────────────────────────────────────────
def _score_universe(ctx, universe: list[str]) -> dict[str, float]:
    scores: dict[str, float] = {}
    for i in range(0, len(universe), 40):
        ret, snap = ctx.get_market_snapshot(universe[i:i+40])
        if ret != RET_OK:
            continue
        snap = cast(pd.DataFrame, snap)
        for _, row in snap.iterrows():
            try:
                scores[str(row['code'])] = score_snapshot_row(row)
            except Exception:
                pass
    return scores


def run_dynamic_screener():
    ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
    universe = _current_watch_universe()
    print(f"[筛选] 动态筛选引擎启动，观察池 {len(universe)} 只，每 {DYNAMIC_INTERVAL//60} 分钟更新")
    mark_worker_beat('筛选', f"启动，周期{DYNAMIC_INTERVAL//60}m")
    try:
        while True:
            universe = _current_watch_universe()
            scores   = _score_universe(ctx, universe)
            with _dynamic_lock:
                _dynamic_watch.clear()
                _dynamic_watch.update(scores)
            top = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:15]
            print("[筛选] Top 15:", "  ".join(f"{c.replace('US.','')}({v})" for c, v in top))
            mark_worker_beat('筛选', f"已更新{len(scores)}只")
            time.sleep(DYNAMIC_INTERVAL)
    except Exception as e:
        mark_worker_beat('筛选', f"异常:{e}")
        print(f"[筛选] 异常：{e}")
    finally:
        ctx.close()


# ── 微量建仓 ─────────────────────────────────────────────────
def run_micro_builder():
    ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
    print(f"[底仓] 启动（每只${MICRO_ALLOC:.0f}，目标{MICRO_TARGET_POS}只，上限{MICRO_MAX_POS}只）")
    mark_worker_beat('底仓', f"启动，目标{MICRO_TARGET_POS}只")

    # 等待筛选器首次就绪
    for _ in range(30):
        with _dynamic_lock:
            if len(_dynamic_watch) > 0:
                break
        time.sleep(10)

    try:
        while True:
            with _dynamic_lock:
                scores = dict(_dynamic_watch)

            state    = broker.get_state()
            cash     = max(0.0, float(state['cash']) - float(state.get('reserved_cash', 0.0) or 0.0))
            initial  = state['initial_cash']
            held     = set(state['positions'].keys())
            micro_held = {c for c, p in state['positions'].items() if p.get('bucket') == 'micro'}

            micro_codes = [c for stocks in MICRO_SECTOR_UNIVERSE.values() for c in stocks]
            price_map: dict[str, float] = {}
            ret, snap = ctx.get_market_snapshot(micro_codes)
            if ret == RET_OK:
                snap = cast(pd.DataFrame, snap)
                price_map = {str(r['code']): live_price_from_row(r) for _, r in snap.iterrows()}

            target_new = max(0, min(MICRO_TARGET_POS, MICRO_MAX_POS) - len(micro_held))
            max_new    = max(0, MICRO_MAX_POS - len(micro_held))

            if max_new <= 0:
                mark_worker_beat('底仓', f"已满{MICRO_MAX_POS}只")
                print(f"[底仓] 底仓已满（{MICRO_MAX_POS} 只），跳过")
                time.sleep(DYNAMIC_INTERVAL * 2)
                continue

            sel_universe = {
                sector: [c for c in stocks if price_map.get(c, 0.0) > 0]
                for sector, stocks in MICRO_SECTOR_UNIVERSE.items()
            }
            raw_cands = select_diversified_micro_candidates(
                scores, sel_universe, held,
                target_positions=target_new, max_positions=max_new,
                sector_cap=MICRO_SECTOR_CAP, min_score=MICRO_MIN_SCORE,
                sector_caps=MICRO_SECTOR_CAP_OVERRIDES,
                required_sectors=MICRO_REQUIRED_SECTORS,
            )
            candidates = [r for r in raw_cands
                          if not in_reentry_cooldown(r['code'], MICRO_REENTRY_COOLDOWN_MINUTES)]

            if not candidates and target_new > 0:
                mark_worker_beat('底仓', f"缺口{target_new}，无候选")
                print(f"[底仓] 无新增候选（缺口{target_new}，阈值{MICRO_MIN_SCORE}）")

            bought = 0
            for row in candidates:
                if bought >= target_new:
                    break
                code  = row['code']
                price = price_map.get(code, 0.0)
                if price <= 0:
                    continue
                budget = entry_budget(cash, initial, 0.0, 'micro_position',
                                      reserve_ratio=CASH_RESERVE)
                if budget <= 0:
                    print("[底仓] 现金储备不足，停止建仓"); break
                qty = cash_position_qty(price, budget)
                if qty <= 0:
                    continue
                ok, msg = broker.place_order(code, 'BUY', qty, price,
                                             bucket='micro', reason='micro_position')
                if ok:
                    sync_runtime_positions_from_broker()
                    cash = broker.get_available_cash()
                    bought += 1
                    print(f"[底仓] ✅ {code:14s} {qty}股 @ ${price:.2f}"
                          f"  ≈${qty*price:.0f}  评分{row['score']:.1f}  {row['sector']}  {msg}")

            if bought:
                mark_worker_beat('底仓', f"新建{bought}只")
                print(f"[底仓] 本轮新建 {bought} 只，剩余现金 ${cash:,.0f}")
            else:
                mark_worker_beat('底仓', f"候选{len(candidates)}只，无成交")

            # 底仓止损 -10%
            for code in list(micro_held):
                pos   = state['positions'].get(code)
                price = price_map.get(code, 0.0)
                if not pos or price <= 0:
                    continue
                pnl = (price - pos['avg_cost']) / pos['avg_cost']
                if pnl <= -0.10:
                    ok, msg = broker.place_order(code, 'SELL', pos['qty'], price,
                                                 bucket='micro', reason='micro_stop_loss')
                    if ok:
                        sync_runtime_positions_from_broker()
                        print(f"[底仓] 🛑 止损清出 {code}  盈亏{pnl*100:.1f}%  {msg}")

            time.sleep(DYNAMIC_INTERVAL * 2)
    except Exception as e:
        mark_worker_beat('底仓', f"异常:{e}")
        print(f"[底仓] 异常：{e}")
    finally:
        ctx.close()


# ── 每周定投（价值平均版）────────────────────────────────────
def _dca_qty_multiplier(ctx, anchor_code: str = 'US.QQQ', ma_period: int = 200) -> tuple[float, str]:
    """
    根据锚定标的（默认 QQQ）相对 200 日均线的偏离度，返回定投数量倍率及说明。

    偏离度 = (当前价 - MA200) / MA200

    映射规则（量化"便宜程度"）：
      < -10%  : ×2.0  大幅低估，加倍买入
      -10%~-5%: ×1.5  中度低估，增量买入
      -5%~+5% : ×1.0  正常区间，按计划买入
      +5%~+10%: ×0.5  偏贵，减量买入
      > +10%  : ×0.0  明显高估，本周跳过
    """
    try:
        ret, df, _ = ctx.request_history_kline(
            anchor_code, ktype=KLType.K_DAY, autype=AuType.QFQ, max_count=ma_period + 5)
        if ret != RET_OK or df is None or len(df) < ma_period:
            return 1.0, 'MA数据不足(默认×1.0)'
        df = cast(pd.DataFrame, df)
        ma200 = float(df['close'].rolling(ma_period).mean().iloc[-1])
        cur   = float(df['close'].iloc[-1])
        dev   = (cur - ma200) / ma200   # 偏离度
        if dev < -0.10:
            return 2.0, f'低估{dev*100:.1f}%(×2.0加倍)'
        elif dev < -0.05:
            return 1.5, f'偏低{dev*100:.1f}%(×1.5增量)'
        elif dev <= 0.05:
            return 1.0, f'正常{dev*100:+.1f}%(×1.0)'
        elif dev <= 0.10:
            return 0.5, f'偏贵{dev*100:.1f}%(×0.5减量)'
        else:
            return 0.0, f'高估{dev*100:.1f}%(×0.0跳过)'
    except Exception as e:
        return 1.0, f'计算异常({e})，默认×1.0'


def run_weekly_dca():
    if not WEEKLY_DCA_PLAN:
        print("[定投] 未配置定投标的，退出"); return

    ctx       = OpenQuoteContext(host='127.0.0.1', port=11111)
    plan_desc = " / ".join(f"{c.replace('US.','')}×{q}" for c, q in WEEKLY_DCA_PLAN.items())
    print(f"[定投] 启动（价值平均版）（每周{['一','二','三','四','五','六','日'][WEEKLY_DCA_WEEKDAY_ET]} "
          f"{WEEKLY_DCA_MIN_HOUR_ET}:00 ET：{plan_desc}）")
    mark_worker_beat('定投', f"启动，周{WEEKLY_DCA_WEEKDAY_ET+1} {WEEKLY_DCA_MIN_HOUR_ET}:00 ET")

    try:
        while True:
            now_et = current_new_york_time()
            marker = week_marker(now_et)

            if not should_execute_weekly_dca(now_et, '', WEEKLY_DCA_WEEKDAY_ET, WEEKLY_DCA_MIN_HOUR_ET):
                mark_worker_beat('定投', f"等待 {now_et.strftime('%Y-%m-%d %H:%M')} ET")
                time.sleep(WEEKLY_DCA_INTERVAL)
                continue

            # 价值平均：根据 QQQ 相对 MA200 的偏离度动态调整定投量
            qty_mult, mult_note = _dca_qty_multiplier(ctx)
            print(f"[定投] 本周估值调整：{mult_note}")

            if qty_mult <= 0:
                print(f"[定投] 市场高估，本周跳过定投")
                # 仍然更新 marker 避免重复触发
                for code in WEEKLY_DCA_PLAN:
                    broker.set_marker(f"weekly_dca:{code}", marker)
                mark_worker_beat('定投', f"{marker} 高估跳过")
                time.sleep(WEEKLY_DCA_INTERVAL)
                continue

            ret, snap = ctx.get_market_snapshot(list(WEEKLY_DCA_PLAN.keys()))
            if ret != RET_OK:
                print("[定投] 快照失败，跳过"); time.sleep(WEEKLY_DCA_INTERVAL); continue

            snap      = cast(pd.DataFrame, snap)
            price_map = {str(r['code']): live_price_from_row(r) for _, r in snap.iterrows()}
            buys      = 0

            for code, base_qty in WEEKLY_DCA_PLAN.items():
                if not should_execute_weekly_dca(
                    now_et, broker.get_marker(f"weekly_dca:{code}", ''),
                    WEEKLY_DCA_WEEKDAY_ET, WEEKLY_DCA_MIN_HOUR_ET,
                ):
                    continue
                price = float(price_map.get(code, 0.0))
                if price <= 0:
                    print(f"[定投] {code} 价格缺失，跳过"); continue

                # 以基准股数对应的美元额为基础，乘以倍率后换算股数
                base_dollars = base_qty * price
                adj_dollars  = base_dollars * qty_mult
                qty = max(0, round(adj_dollars / price))
                if qty <= 0:
                    print(f"[定投] {code} 调整后数量为0，跳过"); continue

                ok, msg = broker.place_order(code, 'BUY', qty, price,
                                             bucket='dca', reason='weekly_dca')
                if ok:
                    broker.set_marker(f"weekly_dca:{code}", marker)
                    sync_runtime_positions_from_broker()
                    buys += 1
                    print(f"[定投] ✅ {code:12s} {qty}股(基准{base_qty}×{qty_mult}) "
                          f"@ ${price:.2f}  ≈${qty*price:.0f}  {mult_note}  {msg}")
                else:
                    print(f"[定投] {code} 失败: {msg}")

            mark_worker_beat('定投', f"{marker} 执行{buys}笔({mult_note})" if buys else f"{marker} 无成交")
            time.sleep(WEEKLY_DCA_INTERVAL)
    except Exception as e:
        mark_worker_beat('定投', f"异常:{e}")
        print(f"[定投] 异常：{e}")
    finally:
        ctx.close()


# ── 市场状态监测 ─────────────────────────────────────────────
# Hysteresis：连续 REGIME_CONFIRM_COUNT 次读到同一状态才真正切换，
# 防止 SPY/VIX 在临界值附近频繁抖动导致 _REGIME_SCALE 来回切换。
_REGIME_CONFIRM_COUNT = 3   # 需要连续 3 次（= 3小时）确认才切换

def run_regime_monitor():
    ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
    print("[Regime] 市场状态监测启动（Hysteresis确认数=3）")
    mark_worker_beat('Regime', '启动')

    # 候选状态计数器：仅在候选与当前不同时才递增
    _candidate: str        = get_regime()
    _confirm_count: int    = 0

    try:
        while True:
            try:
                ret_s, df_s, _ = ctx.request_history_kline(
                    'US.SPY', ktype=KLType.K_DAY, autype=AuType.QFQ, max_count=210)
                df_s    = cast(pd.DataFrame, df_s)
                spy_200 = float(df_s['close'].rolling(200).mean().iloc[-1])
                spy_cur = float(df_s['close'].iloc[-1])

                ret_v, vix_snap = ctx.get_market_snapshot(['US.VIX'])
                vix = float(cast(pd.DataFrame, vix_snap).iloc[0]['last_price']) \
                      if ret_v == RET_OK else 20.0

                if spy_cur < spy_200 * 0.97 and vix > 28:
                    raw_regime = 'BEAR'
                elif spy_cur >= spy_200 and vix < 22:
                    raw_regime = 'BULL'
                else:
                    raw_regime = 'NEUTRAL'

                old = get_regime()

                if raw_regime == old:
                    # 与当前状态一致：重置候选计数器
                    _candidate     = old
                    _confirm_count = 0
                elif raw_regime == _candidate:
                    # 与候选一致：累积确认次数
                    _confirm_count += 1
                    if _confirm_count >= _REGIME_CONFIRM_COUNT:
                        # 连续确认达到阈值，正式切换
                        set_regime(raw_regime)
                        notify(f"市场状态 → {raw_regime}",
                               f"连续{_confirm_count}次确认  SPY={spy_cur:.0f}/{spy_200:.0f}  VIX={vix:.1f}")
                        print(f"[Regime] ✅ 状态切换 {old} → {raw_regime}  "
                              f"(确认{_confirm_count}次)  "
                              f"SPY={spy_cur:.0f}/{spy_200:.0f}  VIX={vix:.1f}")
                        _candidate     = raw_regime
                        _confirm_count = 0
                    else:
                        print(f"[Regime] 候选 {raw_regime} 确认 {_confirm_count}/{_REGIME_CONFIRM_COUNT}  "
                              f"SPY={spy_cur:.0f}/{spy_200:.0f}  VIX={vix:.1f}")
                else:
                    # 候选状态发生变化：重新从1开始
                    _candidate     = raw_regime
                    _confirm_count = 1
                    print(f"[Regime] 候选 {raw_regime} 确认 1/{_REGIME_CONFIRM_COUNT}  "
                          f"SPY={spy_cur:.0f}/{spy_200:.0f}  VIX={vix:.1f}")

                mark_worker_beat('Regime',
                    f"{old}→候选{_candidate}({_confirm_count}) VIX={vix:.1f}"
                    if _candidate != old else f"{old} VIX={vix:.1f}")

            except Exception as e:
                mark_worker_beat('Regime', f"异常:{e}")
                print(f"[Regime] 检测异常：{e}")
            time.sleep(3600)
    finally:
        ctx.close()


# ── 暴跌预警 ─────────────────────────────────────────────────
def run_crash_monitor():
    from market_utils import request_kline
    all_stocks = list({s for b in BUCKETS.values() for s in b['stocks']})
    ctx        = OpenQuoteContext(host='127.0.0.1', port=11111)
    alerted: set[str] = set()
    print(f"[预警] 启动，覆盖 {len(all_stocks)} 只")
    mark_worker_beat('预警', f"启动，覆盖{len(all_stocks)}只")
    try:
        while True:
            ret, snap = ctx.get_market_snapshot(all_stocks)
            if ret == RET_OK:
                snap = cast(pd.DataFrame, snap)
                for _, row in snap.iterrows():
                    code = str(row['code'])
                    last = float(row['last_price'])
                    prev = float(row['prev_close_price'])
                    if prev == 0:
                        continue
                    chg = (last - prev) / prev
                    if chg <= CRASH_THRESHOLD and code not in alerted:
                        rsi_val, vol_note = None, ''
                        df_day = request_kline(ctx, code, KLType.K_DAY, 30)
                        if df_day is not None:
                            rsi_val  = float(calc_rsi(df_day['close'], 14).iloc[-1])
                            _, vol_note = volume_signal(df_day)
                        fund_sc, fund_notes = fundamental_score(row)
                        msg = (f"{code} 暴跌{chg*100:.1f}%  现价${last:.2f}"
                               + (f"  RSI={rsi_val:.0f}" if rsi_val else "")
                               + f"  基本面{fund_sc:.0f}/10({fund_notes[0]})  [{vol_note}]")
                        if rsi_val is not None and rsi_val < CRASH_RSI_ALERT:
                            print(f"[预警] 🚨 {msg}")
                            notify("🚨 极度超卖！潜在抄底", msg, modal=True)
                        else:
                            print(f"[预警] ⚠️  {msg}")
                            notify("⚠️ 暴跌预警", msg)
                        alerted.add(code)
                    elif chg > CRASH_THRESHOLD / 2 and code in alerted:
                        alerted.discard(code)
                mark_worker_beat('预警', f"扫描{len(all_stocks)}只，告警{len(alerted)}")
            else:
                mark_worker_beat('预警', '快照失败')
            time.sleep(300)
    finally:
        ctx.close()


# ── 信号统计定期刷新 ─────────────────────────────────────────
# 每天凌晨（美东时间 00:30）重新计算 signal_stats，保证动态 alloc
# 的 adj_factor 始终基于最新的实盘成交记录，而不是上次回测时的快照。
_SIGNAL_STATS_REFRESH_HOUR_ET   = 0    # 美东时间 0 点刷新
_SIGNAL_STATS_REFRESH_MINUTE_ET = 30

def run_signal_stats_refresh():
    from datetime import date
    from recurring_invest import current_new_york_time
    import signal_stats as _ss

    print("[信号统计] 定期刷新线程启动（每天美东 00:30 更新 adj_factor）")
    mark_worker_beat('信号统计', '启动')

    last_refresh_date: date | None = None

    while True:
        try:
            now_et = current_new_york_time()
            today  = now_et.date()

            should_run = (
                now_et.hour   == _SIGNAL_STATS_REFRESH_HOUR_ET
                and now_et.minute >= _SIGNAL_STATS_REFRESH_MINUTE_ET
                and last_refresh_date != today
            )

            if should_run:
                attr = _ss.refresh(save=True)
                last_refresh_date = today
                n_signals = len([s for s in attr.values() if s['trades'] >= 5])
                mark_worker_beat('信号统计',
                    f"{today} 更新完成，{n_signals}类信号有效")
                print(f"[信号统计] ✅ {today} 刷新完成，"
                      f"{len(attr)}类信号，{n_signals}类有足够数据")
            else:
                mark_worker_beat('信号统计',
                    f"等待 {_SIGNAL_STATS_REFRESH_HOUR_ET:02d}:{_SIGNAL_STATS_REFRESH_MINUTE_ET:02d} ET"
                    f"（上次：{last_refresh_date or '从未'}）")

        except Exception as e:
            mark_worker_beat('信号统计', f"异常:{e}")
            print(f"[信号统计] 刷新异常：{e}")

        time.sleep(600)   # 每 10 分钟轮询一次，避免错过窗口


def build_support_threads() -> list[threading.Thread]:
    return [
        threading.Thread(target=run_bot_heartbeat, daemon=True, name='heartbeat'),
        threading.Thread(target=run_order_matcher, daemon=True, name='order_matcher'),
        threading.Thread(target=run_fundamental_refresh, daemon=True, name='fund_refresh'),
        threading.Thread(target=run_regime_monitor, daemon=True, name='regime'),
        threading.Thread(target=run_dynamic_screener, daemon=True, name='screener'),
        threading.Thread(target=run_micro_builder, daemon=True, name='micro'),
        threading.Thread(target=run_weekly_dca, daemon=True, name='weekly_dca'),
        threading.Thread(target=run_crash_monitor, daemon=True, name='crash_monitor'),
        threading.Thread(target=run_signal_stats_refresh, daemon=True, name='signal_stats'),
    ]


def build_runtime_threads(bucket_runner) -> list[threading.Thread]:
    return build_support_threads() + [
        threading.Thread(
            target=bucket_runner,
            args=(name, cfg),
            daemon=True,
            name=f'bucket_{name}',
        )
        for name, cfg in BUCKETS.items()
    ]


def print_runtime_banner():
    print("┌─────────────────────────────────────────────────┐")
    print("│  Portfolio Bot  已启动（本地虚拟撮合）           │")
    print("│  行情：moomoo OpenD  交易：SQLite 本地账本       │")
    print("│  架构：workers / bucket_runner / order_executor │")
    print("│  Ctrl+C 停止                                    │")
    print("└─────────────────────────────────────────────────┘")
