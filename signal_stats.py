"""
signal_stats.py — 信号历史统计与动态 alloc_mult

职责：
  1. 从 trade_log.csv 加载已平仓交易记录
  2. 调用 performance.signal_attribution() 计算每类信号的 adj_factor
  3. 提供 get_dynamic_alloc_mult(reason, base_mult) 接口供 execution_policy 调用
  4. 结果缓存到 signal_stats_cache.json，避免每次重新计算

使用方式：
  from signal_stats import get_dynamic_alloc_mult
  adjusted = get_dynamic_alloc_mult('golden_cross', 0.40)

刷新时机：
  - 首次导入时自动从 trade_log.csv 计算
  - 调用 refresh() 可强制重新计算（例如回测结束后）
  - 若 trade_log.csv 不存在或数据不足，直接返回 base_mult（无调整）
"""

from __future__ import annotations

import csv
import json
import os
import threading
from typing import Any

from performance import signal_attribution

# ── 路径 ────────────────────────────────────────────────────────────────────
_BASE      = os.path.dirname(__file__)
_LOG_PATH  = os.path.join(_BASE, 'trade_log.csv')
_CACHE_PATH = os.path.join(_BASE, 'signal_stats_cache.json')

# ── 全局状态 ─────────────────────────────────────────────────────────────────
_stats: dict[str, dict] = {}
_lock  = threading.Lock()
_loaded = False


# ── 内部工具 ─────────────────────────────────────────────────────────────────

def _load_trade_log() -> list[dict]:
    """读取 trade_log.csv，返回记录列表。"""
    if not os.path.exists(_LOG_PATH):
        return []
    records = []
    try:
        with open(_LOG_PATH, newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                records.append(row)
    except Exception:
        pass
    return records


def _save_cache(attr: dict[str, dict]) -> None:
    try:
        with open(_CACHE_PATH, 'w', encoding='utf-8') as f:
            json.dump(attr, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _load_cache() -> dict[str, dict]:
    if not os.path.exists(_CACHE_PATH):
        return {}
    try:
        with open(_CACHE_PATH, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


# ── 公开接口 ─────────────────────────────────────────────────────────────────

def refresh(save: bool = True) -> dict[str, dict]:
    """
    重新计算信号统计并更新内存缓存。

    Args:
        save: 是否同时写入 signal_stats_cache.json

    Returns:
        信号归因字典，与 performance.signal_attribution() 格式一致。
    """
    global _stats, _loaded
    trades = _load_trade_log()
    attr   = signal_attribution(trades) if trades else {}
    with _lock:
        _stats  = attr
        _loaded = True
    if save:
        _save_cache(attr)
    return attr


def _ensure_loaded() -> None:
    global _loaded
    if _loaded:
        return
    # 优先从缓存加载（快），再尝试重新计算
    cached = _load_cache()
    if cached:
        with _lock:
            _stats.update(cached)
            _loaded = True
    else:
        refresh(save=True)


def get_stats() -> dict[str, dict]:
    """返回完整的信号归因字典（只读）。"""
    _ensure_loaded()
    with _lock:
        return dict(_stats)


def get_dynamic_alloc_mult(reason: str, base_mult: float) -> float:
    """
    根据历史信号表现返回调整后的 alloc_mult。

    - 若该信号数据不足（adj_factor == 1.0）或未出现，直接返回 base_mult
    - adj_factor 范围为 [0.5, 1.5]，即最多在 base_mult 基础上 ±50%
    - 最终结果还会被 clip 到 [0.10, 0.80]，避免极端仓位

    Args:
        reason:    入场信号名称，如 'golden_cross'
        base_mult: execution_policy.py 中 ENTRY_SIZE_POLICY 的静态 alloc_mult

    Returns:
        调整后的 alloc_mult（float）
    """
    _ensure_loaded()
    with _lock:
        stat = _stats.get(reason)

    if stat is None:
        return base_mult

    adj = float(stat.get('adj_factor', 1.0))
    adjusted = base_mult * adj
    # 安全边界：不允许单信号占用过高或过低比例
    return max(0.10, min(0.80, adjusted))


def print_summary() -> None:
    """打印当前缓存的信号统计摘要（调试用）。"""
    from performance import print_signal_attribution
    _ensure_loaded()
    with _lock:
        data = dict(_stats)
    print_signal_attribution(data)
