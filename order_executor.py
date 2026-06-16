"""
order_executor.py — 买卖执行层

职责：
  - fresh_price(): 下单前强制取最新快照价
  - execute_buy() / execute_sell(): 提交挂单
  - execute_partial_sell(): 仍保持即时成交
"""

from __future__ import annotations

import pandas as pd
from typing import cast
from moomoo import RET_OK

from market_utils import live_price_from_row, current_session
from shared_state import (
    broker, positions, positions_lock,
    _trailing_highs, _trail_lock,
    _profit_stages, _profit_lock,
    runtime_position_snapshot, has_open_order,
)


def fresh_price(ctx, stock: str, fallback: float = 0.0) -> float:
    """
    下单前强制取一次最新快照价（时段感知）。
    避免用扫描开始时可能 30-60 分钟前的旧缓存价下单。
    """
    try:
        ret, snap = ctx.get_market_snapshot([stock])
        if ret == RET_OK:
            snap = cast(pd.DataFrame, snap)
            p = live_price_from_row(snap.iloc[0], session=current_session())
            if p > 0:
                return p
    except Exception:
        pass
    return fallback


def execute_buy(
    ctx,
    stock: str,
    qty: int,
    cached_price: float,
    bucket: str,
    reason: str,
    label: str = '',
    extra_note: str = '',
) -> tuple[bool, float, str]:
    """
    提交买入挂单：下单前刷新价格，成功后只锁定资金，不立即改持仓。

    Returns:
        (accepted, exec_price, message)
    """
    exec_price = fresh_price(ctx, stock, fallback=cached_price)
    if exec_price <= 0:
        return False, cached_price, '价格无效'

    if has_open_order(stock):
        return False, exec_price, '已有未完成订单'

    ok, msg, order = broker.submit_order(stock, 'BUY', qty, exec_price,
                                         bucket=bucket, reason=reason)
    if ok:
        if label:
            note = f"执行价${exec_price:.2f}" + (f" | {extra_note}" if extra_note else '')
            order_id = str((order or {}).get('order_id', '') or '')
            print(f"[{label}] ⏳ 提交买单 {stock} {qty}股 @ ${exec_price:.2f}"
                  f"  OID={order_id}  {note}")

    return ok, exec_price, msg


def execute_sell(
    ctx,
    stock: str,
    qty: int,
    cached_price: float,
    bucket: str,
    reason: str,
    label: str = '',
) -> tuple[bool, float, str]:
    """
    提交卖出挂单：下单前刷新价格，成功后只锁定可卖数量。

    Returns:
        (accepted, exec_price, message)
    """
    exec_price = fresh_price(ctx, stock, fallback=cached_price)
    if exec_price <= 0:
        exec_price = cached_price

    if has_open_order(stock):
        return False, exec_price, '已有未完成订单'

    ok, msg, order = broker.submit_order(stock, 'SELL', qty, exec_price,
                                         bucket=bucket, reason=reason)
    if ok:
        if label:
            tags = {
                'stop_loss':     '🛑 止损',
                'trailing_stop': '📉 移动止损',
                'time_stop':     '⏳ 时间止损',
                'death_cross':   '🔴 死叉',
                'rsi_overbought':'🟡 RSI超买',
                'profit_take1':  '💰 止盈①',
                'profit_take2':  '💰 止盈②',
            }
            tag = tags.get(reason, '卖出')
            order_id = str((order or {}).get('order_id', '') or '')
            print(f"[{label}] {tag} 提交 {stock} {qty}股 @ ${exec_price:.2f}"
                  f"  OID={order_id}")

    return ok, exec_price, msg


def execute_partial_sell(
    ctx,
    stock: str,
    qty: int,
    cached_price: float,
    bucket: str,
    reason: str,
    label: str = '',
) -> tuple[bool, float, str]:
    """
    提交部分卖出挂单（止盈分批），不立即改持仓。
    """
    exec_price = fresh_price(ctx, stock, fallback=cached_price)
    if exec_price <= 0:
        exec_price = cached_price

    if has_open_order(stock):
        return False, exec_price, '已有未完成订单'

    ok, msg, order = broker.submit_order(stock, 'SELL', qty, exec_price,
                                         bucket=bucket, reason=reason)
    if ok:
        if label:
            tag = '💰 止盈①' if reason == 'profit_take1' else '💰 止盈②'
            order_id = str((order or {}).get('order_id', '') or '')
            print(f"[{label}] {tag} 提交 {stock} -{qty}股 @ ${exec_price:.2f}"
                  f"  OID={order_id}")

    return ok, exec_price, msg
