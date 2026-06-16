"""
position_manager.py — 现有持仓的全生命周期管理

职责：
  - 更新移动止损高水位
  - 分批止盈（阶段1 / 阶段2）
  - 固定止损 / 移动止损 / 时间止损 / 死叉止损
  - Starter 晋级加仓
  - 金字塔分批加仓（第二批 / 第三批）

返回值：
  manage_position() 返回 ('sold' | 'added' | 'profit_taken' | 'held', cash)
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from execution_policy import (
    ENTRY_REASON_LABEL, atr_position_qty, entry_budget, is_starter_position
)
from exit_rules import evaluate_trailing_stop, holding_days, should_time_stop
from order_executor import execute_sell, execute_partial_sell, execute_buy
from shared_state import (
    broker, positions, positions_lock,
    _trailing_highs, _trail_lock,
    _profit_stages, _profit_lock,
    has_open_order,
)
from strategy_config import (
    CASH_RESERVE,
    PROFIT_TAKE1_PCT, PROFIT_TAKE2_PCT,
    PYRAMID_ADD1_PROFIT, PYRAMID_ADD1_DAYS,
    PYRAMID_ADD2_PROFIT, PYRAMID_ADD2_DAYS,
    ADD_RSI_MAX,
)


def _update_trail_high(stock: str, price: float) -> float:
    """更新内存+持久化高水位，返回当前高水位。"""
    with _trail_lock:
        new_high = max(_trailing_highs.get(stock, price), price)
        if new_high != _trailing_highs.get(stock):
            _trailing_highs[stock] = new_high
            broker.update_trail_high(stock, new_high)
        return _trailing_highs[stock]


def manage_position(
    ctx,
    stock: str,
    pos_data: dict,
    price: float,
    atr_val: float,
    cfg: dict,
    bucket_name: str,
    label: str,
    ind_str: str,
    ma_death: bool,
    extra_sell: bool,
    add_signal,
    cash: float,
    initial_cash: float,
    rsi_now: float | None = None,
) -> tuple[str, float]:
    """
    管理单只股票的现有持仓。

    Returns:
        (action, updated_cash)
        action: 'sold' | 'profit_taken' | 'added' | 'held'
    """
    ep        = pos_data['entry_price']
    qty       = pos_data['qty']
    pnl_pct   = (price - ep) / ep if ep > 0 else 0.0
    held_days = holding_days(pos_data.get('entry_time'))

    # ── 更新移动高水位 ──────────────────────────────────────
    trail_high  = _update_trail_high(stock, price)
    trail_state = evaluate_trailing_stop(
        ep, price, trail_high, atr_val,
        activate_profit  = float(cfg.get('trail_activate_profit', 0.02)),
        break_even_profit= float(cfg.get('break_even_profit',     0.03)),
        break_even_buffer= float(cfg.get('break_even_buffer',     0.001)),
        atr_mult         = float(cfg.get('trail_atr_mult',        1.2)),
    )

    print(f"[{label}] {stock:12s} {price:>9.2f}"
          f"  {ind_str}  盈亏:{pnl_pct*100:+.1f}%"
          f"  持仓:{held_days}天"
          f"  trail:{'on' if trail_state['trail_active'] else 'off'}"
          f"  stop:${trail_state['effective_stop']:.2f}")

    if has_open_order(stock):
        print(f"[{label}] {stock} 存在未完成订单，暂停重复操作")
        return 'held', broker.get_available_cash()

    # ── 分批止盈 ───────────────────────────────────────────
    with _profit_lock:
        stages_done = set(_profit_stages.get(stock, set()))

    took_profit = False

    if pnl_pct >= PROFIT_TAKE1_PCT and 1 not in stages_done and qty >= 3:
        take_qty = max(1, round(qty * 0.30))
        ok, _, msg = execute_partial_sell(ctx, stock, take_qty, price,
                                         bucket_name, 'profit_take1', label)
        if ok:
            cash = broker.get_available_cash()
            print(f"[{label}] 💰 止盈① 已提交 {stock} -{take_qty}股"
                  f"（盈{pnl_pct*100:.1f}%，等待成交）  {msg}")
            return 'profit_pending', cash

    if pnl_pct >= PROFIT_TAKE2_PCT and 2 not in stages_done and 1 in stages_done and qty >= 2:
        take_qty = max(1, round(qty * 0.60))
        ok, _, msg = execute_partial_sell(ctx, stock, take_qty, price,
                                         bucket_name, 'profit_take2', label)
        if ok:
            cash = broker.get_available_cash()
            print(f"[{label}] 💰 止盈② 已提交 {stock} -{take_qty}股"
                  f"（盈{pnl_pct*100:.1f}%，等待成交）  {msg}")
            return 'profit_pending', cash

    # ── 清仓条件判断 ────────────────────────────────────────
    sell_reason = None
    if pnl_pct <= -cfg['stop_loss']:
        sell_reason = 'stop_loss'
    elif trail_state['triggered']:
        sell_reason = 'trailing_stop'
    elif should_time_stop(
        pos_data.get('entry_time'), pnl_pct,
        max_days   = int(cfg.get('time_stop_days', 0)),
        min_return = float(cfg.get('time_stop_min_return', 0.0)),
    ):
        sell_reason = 'time_stop'
    elif ma_death and (bucket_name != 'longterm' or extra_sell):
        sell_reason = 'death_cross'
    elif extra_sell and bucket_name == 'conservative':
        sell_reason = 'rsi_overbought'

    if sell_reason:
        ok, _, _ = execute_sell(ctx, stock, qty, price, bucket_name, sell_reason, label)
        if ok:
            cash = broker.get_available_cash()
        return 'sold', cash

    if took_profit:
        return 'profit_taken', cash

    # ── 加仓逻辑（止盈后本轮跳过）──────────────────────────
    _bp        = broker.get_state()['positions'].get(stock, {})
    add_count  = int(_bp.get('add_count', 0))
    cur_val    = qty * price

    # Starter 晋级
    starter_like = is_starter_position(
        cur_val, initial_cash, cfg['alloc'],
        threshold=float(cfg.get('starter_ratio', 0.35)),
    )
    if add_count < 1 and starter_like and add_signal is not None:
        sig_reason, sig_note = add_signal
        budget = entry_budget(cash, initial_cash, cfg['alloc'],
                              'starter_promotion',
                              reserve_ratio=CASH_RESERVE,
                              current_position_value=cur_val)
        add_qty = atr_position_qty(price, atr_val, budget, reason='starter_promotion')
        if add_qty > 0:
            ok, exec_p, msg = execute_buy(
                ctx, stock, add_qty, price, bucket_name, sig_reason, label,
                extra_note=f"starter晋级 {ENTRY_REASON_LABEL.get(sig_reason, sig_reason)}({sig_note})")
            if ok:
                cash = broker.get_available_cash()
                return 'added', cash

    _rsi_ok = (rsi_now < cfg.get('add_rsi_max', ADD_RSI_MAX)
               if bucket_name == 'conservative' and rsi_now is not None else True)

    # 金字塔②
    entry_time = _bp.get('entry_time', '')
    try:
        _days = (datetime.now() - datetime.strptime(entry_time, '%Y-%m-%d %H:%M:%S')).days \
                if entry_time else 0
    except (ValueError, TypeError):
        _days = 0

    if (add_count < 1
            and pnl_pct >= PYRAMID_ADD1_PROFIT
            and _days >= PYRAMID_ADD1_DAYS
            and price > pos_data['entry_price']  # 仍在盈利方向
            and _rsi_ok):
        budget = entry_budget(cash, initial_cash, cfg['alloc'],
                              'pyramid_stage2', reserve_ratio=CASH_RESERVE)
        add_qty = atr_position_qty(price, atr_val, budget, reason='pyramid_stage2')
        if add_qty > 0:
            ok, exec_p, msg = execute_buy(
                ctx, stock, add_qty, price, bucket_name, 'pyramid_stage2', label,
                extra_note=f"金字塔② 盈{pnl_pct*100:.1f}% 持{_days}天")
            if ok:
                cash = broker.get_available_cash()
                return 'added', cash

    # 金字塔③
    elif (add_count == 1
            and pnl_pct >= PYRAMID_ADD2_PROFIT
            and _days >= PYRAMID_ADD2_DAYS
            and price > pos_data['entry_price']):
        budget = entry_budget(cash, initial_cash, cfg['alloc'],
                              'pyramid_stage3', reserve_ratio=CASH_RESERVE)
        add_qty = atr_position_qty(price, atr_val, budget, reason='pyramid_stage3')
        if add_qty > 0:
            ok, exec_p, msg = execute_buy(
                ctx, stock, add_qty, price, bucket_name, 'pyramid_stage3', label,
                extra_note=f"金字塔③ 盈{pnl_pct*100:.1f}% 持{_days}天")
            if ok:
                cash = broker.get_available_cash()
                return 'added', cash

    return 'held', cash
