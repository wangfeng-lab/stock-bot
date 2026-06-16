"""
order_reconciler.py — 挂单撮合与超时撤单

把“开放订单 -> 成交 / 撤单”的状态推进逻辑抽离成可测试函数。
"""

from __future__ import annotations

from datetime import datetime


def _parse_ts(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    except ValueError:
        return None


def reconcile_open_orders(
    broker,
    price_map: dict[str, float],
    *,
    min_fill_age_seconds: int,
    timeout_seconds: int,
    now: datetime | None = None,
) -> dict:
    """
    推进当前开放订单状态。

    规则：
      - 挂单时间小于 min_fill_age_seconds：继续等待
      - 有行情价格：按当前价格整单成交
      - 无行情且超时：撤单
      - 无行情且未超时：继续等待
    """
    current = now or datetime.now()
    state = broker.get_state()
    open_orders = [
        o for o in state.get('orders', [])
        if str(o.get('status', '') or '') in {'NEW', 'PARTIALLY_FILLED'}
    ]

    summary = {
        'open_orders': len(open_orders),
        'filled_orders': 0,
        'canceled_orders': 0,
        'waiting_orders': 0,
        'quote_wait_orders': 0,
    }

    for order in open_orders:
        order_id = str(order.get('order_id', '') or '')
        submitted_at = _parse_ts(str(order.get('submitted_at', '') or ''))
        if submitted_at is None:
            age_seconds = timeout_seconds + 1
        else:
            age_seconds = max(0.0, (current - submitted_at).total_seconds())

        if age_seconds < max(0, int(min_fill_age_seconds)):
            summary['waiting_orders'] += 1
            continue

        code = str(order.get('code', '') or '')
        px = float(price_map.get(code, 0.0) or 0.0)
        if px > 0:
            ok, _ = broker.fill_order(order_id, price=px)
            if ok:
                summary['filled_orders'] += 1
            else:
                summary['quote_wait_orders'] += 1
            continue

        if age_seconds >= max(0, int(timeout_seconds)):
            ok, _ = broker.cancel_order(order_id, message='timeout_no_quote')
            if ok:
                summary['canceled_orders'] += 1
            else:
                summary['quote_wait_orders'] += 1
            continue

        summary['quote_wait_orders'] += 1

    return summary
