"""
shared_state.py — 全局共享状态与基础工具函数

所有需要跨线程访问的变量集中在此，避免循环导入。
其他模块只需 `from shared_state import broker, positions, ...`
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from datetime import datetime, timedelta

from local_broker import LocalBroker
from strategy_config import INITIAL_CASH

# ── 路径 ────────────────────────────────────────────────────
BASE      = os.path.dirname(__file__)
BROKER_DB = os.path.join(BASE, 'virtual_account.json')
LOG_FILE  = os.path.join(BASE, 'trade_log.csv')

# ── Broker 实例（全局唯一）─────────────────────────────────
broker = LocalBroker(BROKER_DB, LOG_FILE, initial_cash=INITIAL_CASH)

# ── 内存持仓缓存 ────────────────────────────────────────────
def runtime_position_snapshot(raw_pos: dict) -> dict:
    """把 broker 原始 pos dict 标准化为内存格式。"""
    return {
        'bucket':      str(raw_pos.get('bucket', '') or ''),
        'entry_price': float(raw_pos.get('avg_cost', raw_pos.get('entry_price', 0.0)) or 0.0),
        'qty':         int(raw_pos.get('qty', 0) or 0),
        'entry_time':  str(raw_pos.get('entry_time', '') or ''),
    }

_raw = broker.get_state()['positions']
positions: dict = {
    code: runtime_position_snapshot(p)
    for code, p in _raw.items()
}
positions_lock = threading.Lock()

# ── 市场状态 ────────────────────────────────────────────────
_regime      = 'BULL'   # 'BULL' | 'NEUTRAL' | 'BEAR'
_regime_lock = threading.Lock()

def get_regime() -> str:
    with _regime_lock:
        return _regime

def set_regime(val: str):
    global _regime
    with _regime_lock:
        _regime = val

# ── 移动止损高水位 ───────────────────────────────────────────
_trailing_highs: dict[str, float] = {}
_trail_lock = threading.Lock()

# ── 分批止盈阶段标记 ─────────────────────────────────────────
_profit_stages: dict[str, set[int]] = {}
_profit_lock = threading.Lock()

# ── 动态 Watchlist ───────────────────────────────────────────
_dynamic_watch: dict[str, float] = {}
_dynamic_lock = threading.Lock()

# ── 线程心跳 ────────────────────────────────────────────────
_worker_beats: dict[str, dict] = {}
_worker_lock = threading.Lock()

BOT_START_TS = time.time()
HEARTBEAT_INTERVAL = 60


def mark_worker_beat(name: str, detail: str = ''):
    with _worker_lock:
        _worker_beats[name] = {'ts': time.time(), 'detail': detail}


def restore_runtime_state():
    """从 broker JSON 恢复内存中的止盈阶段和高水位（重启时调用）。"""
    state = broker.get_state()
    for code, pos in state['positions'].items():
        stages = set(pos.get('profit_stages', []))
        if stages:
            _profit_stages[code] = stages
        trail = float(pos.get('trail_high', 0.0))
        if trail > 0:
            _trailing_highs[code] = trail


def sync_runtime_positions_from_broker():
    """把 broker 真源状态同步回内存缓存，并清理已消失持仓的派生状态。"""
    state = broker.get_state()
    live_positions = state.get('positions', {})

    with positions_lock:
        for code, pos in live_positions.items():
            positions[code] = runtime_position_snapshot(pos)
        for code in list(positions.keys()):
            if code not in live_positions:
                positions.pop(code, None)

    with _trail_lock:
        for code in list(_trailing_highs.keys()):
            if code not in live_positions:
                _trailing_highs.pop(code, None)
        for code, pos in live_positions.items():
            trail = float(pos.get('trail_high', 0.0) or 0.0)
            if trail > 0:
                _trailing_highs[code] = trail

    with _profit_lock:
        for code in list(_profit_stages.keys()):
            if code not in live_positions:
                _profit_stages.pop(code, None)
        for code, pos in live_positions.items():
            stages = set(pos.get('profit_stages', []))
            if stages:
                _profit_stages[code] = stages
            else:
                _profit_stages.pop(code, None)


# ── 组合熔断 ────────────────────────────────────────────────
_CB_DRAWDOWN_THRESHOLD = 0.10   # 从高水位回撤超 10% 触发熔断
_CB_PAUSE_DAYS         = 5      # 熔断暂停天数

_portfolio_hwm:    float             = float(INITIAL_CASH)
_cb_paused_until:  datetime | None   = None
_cb_lock           = threading.Lock()


def estimate_portfolio_value() -> float:
    """
    估算当前组合总价值（现金 + 持仓成本价 × 数量）。
    用成本价作为持仓市值的近似：不需要实时行情，但在持仓亏损时会高估组合价值，
    因此属于保守的熔断触发条件（只有在大量止损出清后现金减少才会真正触发）。
    """
    state = broker.get_state()
    cash  = float(state.get('cash', 0))
    pos_value = sum(
        float(pos.get('avg_cost', pos.get('entry_price', 0)) or 0)
        * int(pos.get('qty', 0) or 0)
        for pos in state.get('positions', {}).values()
    )
    return cash + pos_value


def update_circuit_breaker() -> tuple[bool, str]:
    """
    更新高水位并检查是否需要触发熔断。

    Returns:
        (newly_triggered, note_str)
        - newly_triggered: 本次调用是否新触发了熔断（已在熔断中时返回 False）
        - note_str: 用于日志的说明文字
    """
    global _portfolio_hwm, _cb_paused_until
    value = estimate_portfolio_value()
    with _cb_lock:
        if value > _portfolio_hwm:
            _portfolio_hwm = value

        drawdown = (_portfolio_hwm - value) / _portfolio_hwm if _portfolio_hwm > 0 else 0.0
        now = datetime.now()

        # 已在熔断期内，不重复触发
        if _cb_paused_until is not None and now < _cb_paused_until:
            return False, ''

        if drawdown >= _CB_DRAWDOWN_THRESHOLD:
            _cb_paused_until = now + timedelta(days=_CB_PAUSE_DAYS)
            note = (
                f"组合回撤{drawdown*100:.1f}%(高水位${_portfolio_hwm:,.0f} → 当前${value:,.0f})，"
                f"熔断{_CB_PAUSE_DAYS}天至{_cb_paused_until.strftime('%m-%d')}"
            )
            return True, note

    return False, ''


def is_circuit_breaker_active() -> bool:
    """返回当前是否处于熔断暂停期。"""
    with _cb_lock:
        return _cb_paused_until is not None and datetime.now() < _cb_paused_until


def get_circuit_breaker_status() -> str:
    """返回熔断状态描述，未熔断时返回空字符串。"""
    with _cb_lock:
        if _cb_paused_until is None or datetime.now() >= _cb_paused_until:
            return ''
        remaining = (_cb_paused_until - datetime.now()).days + 1
        return f"熔断中({remaining}天剩余，至{_cb_paused_until.strftime('%m-%d')})"


# ── 工具函数 ────────────────────────────────────────────────
def count_bucket_positions(name: str) -> int:
    state = broker.get_state()
    current_codes = {
        code for code, pos in state.get('positions', {}).items()
        if pos.get('bucket') == name
    }
    pending_buy_codes = {
        str(order.get('code', '') or '')
        for order in state.get('orders', [])
        if str(order.get('status', '') or '') in {'NEW', 'PARTIALLY_FILLED'}
        and str(order.get('side', '') or '').upper() == 'BUY'
        and str(order.get('bucket', '') or '') == name
        and str(order.get('code', '') or '') not in current_codes
    }
    return len(current_codes) + len(pending_buy_codes)


def has_open_order(code: str, side: str | None = None, bucket: str | None = None) -> bool:
    side_filter = str(side).upper() if side else ''
    bucket_filter = str(bucket) if bucket else ''
    state = broker.get_state()
    for order in state.get('orders', []):
        if str(order.get('status', '') or '') not in {'NEW', 'PARTIALLY_FILLED'}:
            continue
        if str(order.get('code', '') or '') != code:
            continue
        if side_filter and str(order.get('side', '') or '').upper() != side_filter:
            continue
        if bucket_filter and str(order.get('bucket', '') or '') != bucket_filter:
            continue
        return True
    return False


# 按卖出原因区分冷静期（分钟）
# stop_loss/death_cross 表示趋势性下跌，短期内不应再入场
_COOLDOWN_BY_REASON: dict[str, int] = {
    'stop_loss':       1440,   # 24h：触发止损说明趋势已变，冷静足够时间
    'micro_stop_loss': 1440,   # 同上
    'death_cross':      240,   # 4h：均线死叉，趋势转弱，冷静半天
}


def in_reentry_cooldown(code: str, cooldown_minutes: int) -> bool:
    """
    检查是否在再入场冷静期内。
    冷静时长根据上次卖出原因动态确定：
      - stop_loss / micro_stop_loss → 24h（趋势性下跌，不轻易再入场）
      - death_cross                 → 4h（均线转弱）
      - 其他（止盈/正常卖出）       → cooldown_minutes（通常 30min）
    """
    reason = broker.last_sell_reason(code)
    effective_cooldown = _COOLDOWN_BY_REASON.get(reason, cooldown_minutes)
    return broker.was_sold_recently(code, effective_cooldown)


def notify(title: str, msg: str, modal: bool = False):
    script = (
        f'display dialog "{msg}" with title "{title}" '
        f'buttons {{"忽略","关注"}} default button "关注"'
        if modal else
        f'display notification "{msg}" with title "{title}" sound name "Basso"'
    )
    subprocess.Popen(['osascript', '-e', script])


# 启动时恢复
restore_runtime_state()
