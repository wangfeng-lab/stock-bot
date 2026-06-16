"""
market_utils.py — 快照价格、行情读取、交易时段检测

美股交易时段（美东时间 ET）：
  夜盘   Overnight : 前一日 20:00 → 04:00
  盘前   Pre-Market: 04:00 → 09:30
  正常   Regular   : 09:30 → 16:00
  盘后   After-Hours: 16:00 → 20:00
  休市   Closed    : 周末 / 节假日
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import cast
from zoneinfo import ZoneInfo

import pandas as pd
from moomoo import AuType, RET_OK

ET = ZoneInfo('America/New_York')

# ── 时段定义 ──────────────────────────────────────────────────
SESSION_OVERNIGHT   = 'overnight'    # 夜盘   20:00 → 04:00
SESSION_PREMARKET   = 'premarket'    # 盘前   04:00 → 09:30
SESSION_REGULAR     = 'regular'      # 正常   09:30 → 16:00
SESSION_AFTERHOURS  = 'afterhours'   # 盘后   16:00 → 20:00
SESSION_CLOSED      = 'closed'       # 休市（周末）

SESSION_LABEL = {
    SESSION_OVERNIGHT:  '🌙 夜盘',
    SESSION_PREMARKET:  '🌅 盘前',
    SESSION_REGULAR:    '📈 交易中',
    SESSION_AFTERHOURS: '🌇 盘后',
    SESSION_CLOSED:     '🔴 休市',
}

SESSION_COLOR = {
    SESSION_OVERNIGHT:  '#6366f1',
    SESSION_PREMARKET:  '#f59e0b',
    SESSION_REGULAR:    '#22c55e',
    SESSION_AFTERHOURS: '#8b5cf6',
    SESSION_CLOSED:     '#6b7280',
}

# 各时段开始时间（ET，当日）
_T_OVERNIGHT_END   = time(4,  0)   # 夜盘结束 = 盘前开始
_T_PREMARKET_END   = time(9, 30)   # 盘前结束 = 正常开始
_T_REGULAR_END     = time(16, 0)   # 正常结束 = 盘后开始
_T_AFTERHOURS_END  = time(20, 0)   # 盘后结束 = 夜盘开始


def now_et() -> datetime:
    """返回当前美东时间（自动处理 EST/EDT）。"""
    return datetime.now(ET)


def current_session(dt: datetime | None = None) -> str:
    """根据美东时间返回当前交易时段。"""
    et = dt or now_et()
    # 周六/周日 → 休市
    if et.weekday() >= 5:
        return SESSION_CLOSED
    t = et.time()
    if _T_OVERNIGHT_END <= t < _T_PREMARKET_END:
        return SESSION_PREMARKET
    elif _T_PREMARKET_END <= t < _T_REGULAR_END:
        return SESSION_REGULAR
    elif _T_REGULAR_END <= t < _T_AFTERHOURS_END:
        return SESSION_AFTERHOURS
    else:
        return SESSION_OVERNIGHT


def next_session_info(dt: datetime | None = None) -> dict:
    """
    返回下一个重要时间节点的名称和倒计时（秒）。
    例如：{'label': '距开盘', 'seconds': 3600, 'session': 'regular'}
    """
    et = dt or now_et()
    today = et.date()
    weekday = et.weekday()

    def _make_et(d, t):
        return datetime.combine(d, t, tzinfo=ET)

    sess = current_session(et)

    if sess == SESSION_PREMARKET:
        target = _make_et(today, _T_PREMARKET_END)
        return {'label': '距开盘', 'seconds': (target - et).total_seconds(),
                'next_session': SESSION_REGULAR, 'target': target}

    elif sess == SESSION_REGULAR:
        target = _make_et(today, _T_REGULAR_END)
        return {'label': '距收盘', 'seconds': (target - et).total_seconds(),
                'next_session': SESSION_AFTERHOURS, 'target': target}

    elif sess == SESSION_AFTERHOURS:
        target = _make_et(today, _T_AFTERHOURS_END)
        return {'label': '距夜盘', 'seconds': (target - et).total_seconds(),
                'next_session': SESSION_OVERNIGHT, 'target': target}

    elif sess == SESSION_OVERNIGHT:
        # 如果已过 20:00，盘前是明天 4:00
        if et.time() >= _T_AFTERHOURS_END:
            next_day = today + timedelta(days=1)
            # 跳过周末
            while next_day.weekday() >= 5:
                next_day += timedelta(days=1)
            target = _make_et(next_day, _T_OVERNIGHT_END)
        else:
            target = _make_et(today, _T_OVERNIGHT_END)
        return {'label': '距盘前', 'seconds': (target - et).total_seconds(),
                'next_session': SESSION_PREMARKET, 'target': target}

    else:  # CLOSED（周末）
        # 找下周一的盘前
        days_ahead = (7 - weekday) % 7 or 7
        next_day = today + timedelta(days=days_ahead)
        target = _make_et(next_day, _T_OVERNIGHT_END)
        return {'label': '距盘前', 'seconds': (target - et).total_seconds(),
                'next_session': SESSION_PREMARKET, 'target': target}


def fmt_countdown(seconds: float) -> str:
    """将秒数格式化为 'Xh Ym' 或 'Xm Ys'。"""
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s   = divmod(rem, 60)
    if h > 0:
        return f"{h}h {m:02d}m"
    elif m > 0:
        return f"{m}m {s:02d}s"
    else:
        return f"{s}s"


# ── 时段感知价格选取 ──────────────────────────────────────────
def live_price_from_row(row: pd.Series,
                        session: str | None = None) -> float:
    """
    时段感知的实时价格选取。

    各时段最佳价格来源：
      盘前  (premarket)  : bid/ask → pre_price  → overnight → last
      正常  (regular)    : bid/ask → last_price → pre/after
      盘后  (afterhours) : bid/ask → after_price → last
      夜盘  (overnight)  : bid/ask → overnight  → after → last
      休市  (closed)     : last_price（昨收）
    """
    bid       = float(row.get('bid_price')      or 0)
    ask       = float(row.get('ask_price')      or 0)
    pre       = float(row.get('pre_price')      or 0)
    after     = float(row.get('after_price')    or 0)
    overnight = float(row.get('overnight_price') or 0)
    last      = float(row.get('last_price')     or 0)

    # bid/ask 中间价最精确（只要两边都有报价）
    if bid > 0 and ask > 0:
        # 合理性检查：spread 不超过 5%（过大可能是脏数据）
        if ask > bid and (ask - bid) / bid < 0.05:
            return round((bid + ask) / 2, 4)

    sess = session or current_session()

    if sess == SESSION_PREMARKET:
        return pre or overnight or last

    elif sess == SESSION_REGULAR:
        return last or pre or overnight

    elif sess == SESSION_AFTERHOURS:
        return after or last

    elif sess == SESSION_OVERNIGHT:
        return overnight or after or last

    else:  # closed / fallback
        return last or overnight or pre or after


def display_price_from_row(row: pd.Series,
                           session: str | None = None) -> float:
    """
    Dashboard 展示专用价格（不用于交易决策）。

    核心原则：优先显示“当前时段最近一笔有意义的成交价”，而不是做市商中间价。

    时段策略：
      正常交易时段 (regular) : last_price → bid/ask 中间价 → prev_close
      盘后       (afterhours): after_price → bid/ask 中间价 → last_price
      盘前       (premarket) : pre_price → bid/ask 中间价 → overnight → last_price
      夜盘       (overnight) : overnight_price → bid/ask 中间价 → after_price → last_price
      休市       (closed)    : last_price → prev_close → overnight

    这样处理的原因：
      - 展示层要反映“当前时段最近成交”，不是报价中间价
      - 交易层仍可继续用 bid/ask 中间价，便于本地撮合更接近可成交价
    """
    sess = session or current_session()

    last      = float(row.get('last_price')       or 0)
    prev_cls  = float(row.get('prev_close_price') or 0)
    bid       = float(row.get('bid_price')        or 0)
    ask       = float(row.get('ask_price')        or 0)
    pre       = float(row.get('pre_price')        or 0)
    after     = float(row.get('after_price')      or 0)
    overnight = float(row.get('overnight_price')  or 0)

    # 有效 bid/ask 中间价
    ba_mid = 0.0
    if bid > 0 and ask > 0 and ask > bid and (ask - bid) / bid < 0.05:
        ba_mid = round((bid + ask) / 2, 4)

    if sess == SESSION_REGULAR:
        return last or ba_mid or prev_cls

    elif sess == SESSION_AFTERHOURS:
        return after or ba_mid or last or prev_cls

    elif sess == SESSION_PREMARKET:
        return pre or ba_mid or overnight or last or prev_cls

    elif sess == SESSION_OVERNIGHT:
        return overnight or ba_mid or after or last or prev_cls

    else:
        return last or prev_cls or overnight or ba_mid

    # fallback
    for field in ('close_price_5min', 'avg_price'):
        v = float(row.get(field) or 0)
        if v > 0:
            return v
    return 0.0


def request_kline(ctx, stock: str, ktype, n: int) -> pd.DataFrame | None:
    ret, df, _ = ctx.request_history_kline(
        stock, ktype=ktype, autype=AuType.QFQ, max_count=n
    )
    df = cast(pd.DataFrame, df)
    return df if ret == RET_OK and len(df) >= n - 2 else None
