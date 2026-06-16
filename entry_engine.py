"""
entry_engine.py — 入场信号级联与分层买入

职责：
  - build_signal_cascade(): 按优先级聚合所有 10 种入场信号
  - tiered_entry(): 根据基本面评分分三层决定仓位
  - 调用 order_executor.execute_buy() 下单
"""

from __future__ import annotations

import pandas as pd

import time as _time

from discussion_universe import discussion_alloc_modifier, load_discussion_universe
from execution_policy import (
    ENTRY_REASON_LABEL, atr_position_qty, entry_budget
)
from order_executor import execute_buy
from screener import MIN_FUND_SCORE
from shared_state import (
    broker, count_bucket_positions, get_regime, has_open_order,
    is_circuit_breaker_active, get_circuit_breaker_status,
)
from strategy_config import CASH_RESERVE, SECTOR_MAP
from strategy_signals import (
    detect_entry_signal,
    detect_uptrend,
    detect_premarket_signal,
    detect_momentum_surge,
    detect_rsi_bounce,
    detect_bollinger_breakout,
    detect_52w_high_breakout,
    detect_macd_zero_cross,
)


# 入场前行业集中度上限（与风控层的 max_industry_pct 保持一致）
_PRE_ENTRY_SECTOR_CAP = 0.30

# 市场状态 → alloc_mult 缩放
_REGIME_SCALE = {'BULL': 1.00, 'NEUTRAL': 0.85, 'BEAR': 0.70}

# 同板块持仓数 → 相关性惩罚系数
_CORR_PENALTY = {0: 1.00, 1: 0.90, 2: 0.80}
_CORR_PENALTY_MAX = 0.70   # 3只及以上同板块持仓时的下限

# 讨论热度缓存：TTL 3600s，与 refresh_discussion_universe 的刷新周期对齐
_discussion_feed_cache: dict = {}
_discussion_feed_ts: float   = 0.0
_DISCUSSION_CACHE_TTL        = 3600.0   # 1小时


def _get_discussion_feed() -> dict:
    """返回讨论热度数据，超过 TTL 自动从磁盘重新加载。"""
    global _discussion_feed_cache, _discussion_feed_ts
    if _time.monotonic() - _discussion_feed_ts > _DISCUSSION_CACHE_TTL:
        _discussion_feed_cache = load_discussion_universe()
        _discussion_feed_ts    = _time.monotonic()
    return _discussion_feed_cache


def check_sector_concentration(stock: str, label: str) -> bool:
    """
    入场前检查目标股票所属行业的当前持仓占比。

    Returns:
        True  → 板块占比在上限内，可以入场
        False → 超出上限，跳过本次入场

    逻辑：
      1. 从 SECTOR_MAP 查目标股票的板块
      2. 从 broker 获取所有持仓（含成本价×数量作为市值近似）
      3. 计算该板块持仓价值 / 全部持仓总价值
      4. 若比例 > _PRE_ENTRY_SECTOR_CAP，打印提示并返回 False
    """
    sector = SECTOR_MAP.get(stock)
    if sector is None:
        return True   # 未收录的板块不限制

    state = broker.get_state()
    positions = state.get('positions', {})
    if not positions:
        return True

    total_value = 0.0
    sector_value = 0.0
    for code, pos in positions.items():
        cost = float(pos.get('avg_cost', pos.get('entry_price', 0)) or 0)
        qty  = int(pos.get('qty', 0) or 0)
        val  = cost * qty
        total_value += val
        if SECTOR_MAP.get(code) == sector:
            sector_value += val

    if total_value <= 0:
        return True

    ratio = sector_value / total_value
    if ratio >= _PRE_ENTRY_SECTOR_CAP:
        print(
            f"[{label}] {stock} 板块集中度预检未通过：{sector} 占比 "
            f"{ratio*100:.1f}% ≥ {_PRE_ENTRY_SECTOR_CAP*100:.0f}%，跳过入场"
        )
        return False

    return True


def compute_entry_scale(stock: str) -> tuple[float, str]:
    """
    综合计算入场仓位缩放系数，返回 (scale, note_str)。

    三层独立调整相乘，任意一层收缩都会反映到最终预算：

    1. 讨论热度（discussion_alloc_modifier）
       - 热榜 1-10  → ×0.75（过热，降权）
       - 热榜 11-50 → ×1.15（有动量，加成）

    2. 市场状态（Regime）
       - BULL    → ×1.00
       - NEUTRAL → ×0.85
       - BEAR    → ×0.70

    3. 板块内相关性（同板块现有持仓数）
       - 0 只同板块持仓 → ×1.00
       - 1 只           → ×0.90
       - 2 只           → ×0.80
       - 3 只及以上     → ×0.70

    最终 scale 被 clip 到 [0.30, 1.30]，避免极端情况。
    """
    notes: list[str] = []

    # ── 层1：讨论热度（TTL缓存，每小时自动刷新）────────────────
    disc_mult, disc_note = discussion_alloc_modifier(stock, _get_discussion_feed())
    if disc_note:
        notes.append(disc_note)

    # ── 层2：市场状态 ─────────────────────────────────────────
    regime = get_regime()
    regime_mult = _REGIME_SCALE.get(regime, 1.00)
    if regime != 'BULL':
        notes.append(f'regime={regime}(×{regime_mult:.2f})')

    # ── 层3：板块内相关性（用成本价估算持仓价值，不需要实时行情）─────
    sector = SECTOR_MAP.get(stock)
    corr_mult = 1.00
    if sector:
        state = broker.get_state()
        same_sector_count = sum(
            1 for code, pos in state.get('positions', {}).items()
            if SECTOR_MAP.get(code) == sector
        )
        corr_mult = _CORR_PENALTY.get(same_sector_count, _CORR_PENALTY_MAX)
        if same_sector_count > 0:
            notes.append(f'{sector}已持{same_sector_count}只(×{corr_mult:.2f})')

    scale = disc_mult * regime_mult * corr_mult
    scale = max(0.30, min(1.30, scale))
    note  = ' | '.join(notes) if notes else ''
    return scale, note


def build_signal_cascade(
    cfg: dict,
    df: pd.DataFrame,
    latest: pd.Series,
    prev_row: pd.Series,
    fast_now: float,
    slow_now: float,
    fast_prev: float,
    slow_prev: float,
    extra_buy: bool,
    snap_row: pd.Series | None,
    price: float,
    allow_uptrend: bool = False,
) -> tuple[str, str] | None:
    """
    按优先级聚合全部入场信号，返回最优信号或 None。

    优先级（高→低）：
      原始信号（金叉/回踩/突破）
      > MACD零轴上穿
      > 52周新高
      > BB收窄突破
      > 动量加速
      > RSI超卖反弹
      > 盘前异动
    """
    # 原始技术信号（金叉 / 回踩 / 突破）
    base_sig = detect_entry_signal(
        cfg, df, latest, prev_row,
        fast_now, slow_now, fast_prev, slow_prev,
        extra_buy,
    )
    if base_sig:
        return base_sig

    if allow_uptrend:
        uptrend_sig = detect_uptrend(df, fast_now, slow_now)
        if uptrend_sig:
            return uptrend_sig

    # MACD 零轴上穿
    macd_sig = detect_macd_zero_cross(df)
    if macd_sig:
        return macd_sig

    # 52 周新高
    if snap_row is not None:
        h52_sig = detect_52w_high_breakout(snap_row, price, within_pct=0.03)
        if h52_sig:
            return h52_sig

    # 布林带收窄突破
    bb_sig = detect_bollinger_breakout(df, squeeze_threshold=0.04)
    if bb_sig:
        return bb_sig

    # 动量加速
    mom_sig = detect_momentum_surge(df, fast_now, slow_now,
                                    vol_surge_mult=2.0, price_accel_pct=0.5)
    if mom_sig:
        return mom_sig

    # RSI 超卖反弹
    rsi_sig = detect_rsi_bounce(df, oversold=32.0, recover=38.0)
    if rsi_sig:
        return rsi_sig

    # 盘前异动
    if snap_row is not None:
        pre_sig = detect_premarket_signal(snap_row, min_gap_pct=2.0, min_pre_vol=50_000)
        if pre_sig:
            return pre_sig

    return None


def tiered_entry(
    ctx,
    stock: str,
    signal: tuple[str, str],
    fund_sc: float,
    fund_notes: list,
    vol_note: str,
    cfg: dict,
    bucket_name: str,
    label: str,
    ind_str: str,
    price: float,
    atr_val: float,
    cash: float,
    initial_cash: float,
    no_new_entry: bool = False,
    quality_score: float | None = None,
    skip_fast_fund_gate: bool = False,
    quality_note: str = '',
) -> tuple[bool, float]:
    """
    三层分级买入：
      蓝筹底仓 (score ≥ 7.0)  → 100% alloc
      成长趋势 (score 5.0–7.0) →  70% alloc
      热门赛道 (score 3.0–5.0) →  40% alloc

    Returns:
        (bought, updated_cash)
    """
    if no_new_entry:
        return False, cash

    # 组合熔断检查：回撤超10%时暂停所有新入场
    if is_circuit_breaker_active():
        cb_status = get_circuit_breaker_status()
        print(f"[{label}] {stock} {cb_status}，跳过入场")
        return False, cash

    if (not skip_fast_fund_gate) and fund_sc < MIN_FUND_SCORE.get(bucket_name, 3.0):
        print(f"[{label}] {stock} 基本面不达标({fund_sc:.0f}/10)，跳过")
        return False, cash

    if has_open_order(stock):
        print(f"[{label}] {stock} 已有未完成订单，跳过")
        return False, cash

    # 板块集中度预检：超限直接跳过，不等事后风控介入
    if not check_sector_concentration(stock, label):
        return False, cash

    if count_bucket_positions(bucket_name) >= cfg['max_pos']:
        print(f"[{label}] {stock} 已满{cfg['max_pos']}仓，跳过")
        return False, cash

    min_cash = initial_cash * CASH_RESERVE
    if cash <= min_cash:
        print(f"[{label}] {stock} 现金储备不足，跳过")
        return False, cash

    tier_score = float(quality_score if quality_score is not None else fund_sc)

    # 确定分层
    if tier_score >= 7.0:
        alloc_mult  = 1.0
        tier_label  = f"蓝筹底仓(质量{tier_score:.1f})"
        signal_reason, signal_note = signal
    elif tier_score >= 5.0:
        alloc_mult  = 0.7
        tier_label  = f"成长趋势(质量{tier_score:.1f})"
        signal_reason, signal_note = signal
    else:
        alloc_mult  = 0.4
        tier_label  = f"热门赛道(质量{tier_score:.1f})"
        signal_reason, signal_note = signal

    # 综合入场缩放：讨论热度 × 市场状态 × 板块相关性
    entry_scale, scale_note = compute_entry_scale(stock)

    alloc_cash = entry_budget(
        cash, initial_cash, cfg['alloc'],
        signal_reason, reserve_ratio=CASH_RESERVE,
    ) * alloc_mult * entry_scale

    qty = atr_position_qty(price, atr_val, alloc_cash, reason=signal_reason)
    if qty <= 0:
        print(f"[{label}] {stock} 预算不足，跳过")
        return False, cash

    extra_note = (
        f"{tier_label}"
        f" | {ENTRY_REASON_LABEL.get(signal_reason, signal_reason)}({signal_note})"
        f" | 基本面{fund_sc:.0f}/10({fund_notes[0] if fund_notes else ''})"
        f"{f' | {quality_note}' if quality_note else ''}"
        f"{f' | 缩放×{entry_scale:.2f}({scale_note})' if scale_note else ''}"
        f" | {vol_note} | ATR={atr_val:.2f}"
    )

    ok, exec_p, msg = execute_buy(
        ctx, stock, qty, price, bucket_name, signal_reason, label,
        extra_note=f"{ind_str} | {extra_note}",
    )

    if ok:
        cash = broker.get_available_cash()
        return True, cash

    return False, cash
