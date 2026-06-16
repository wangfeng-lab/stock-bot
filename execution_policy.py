"""
execution_policy.py — 统一的买入事件与仓位预算规则

目标：
1. 混合模式下，底仓脚本和自动 bot 共用一套金额规范
2. 不同入场事件有不同的预算倍率和风险倍率
3. 所有买入都先保留现金储备，再决定可用预算
"""

from __future__ import annotations

CASH_RESERVE_RATIO = 0.20

ENTRY_SIZE_POLICY: dict[str, dict[str, float | str]] = {
    # 广泛底仓：固定美元金额（分散小额）
    'micro_position': {
        'kind': 'fixed_cash',
        'cash': 300.0,        # 每笔底仓 $300（原 $500）
        'risk_mult': 1.00,
    },
    # 新仓：完整信号仓位，缩至 40%（分散优先）
    'golden_cross': {
        'kind': 'bucket_alloc',
        'alloc_mult': 0.40,   # 原 1.00 → 单笔更小
        'risk_mult': 0.80,
    },
    # 趋势中回踩确认
    'trend_pullback': {
        'kind': 'bucket_alloc',
        'alloc_mult': 0.30,   # 原 0.60
        'risk_mult': 0.70,
    },
    # 突破追随
    'breakout': {
        'kind': 'bucket_alloc',
        'alloc_mult': 0.35,   # 原 0.80
        'risk_mult': 0.80,
    },
    # starter 仓位晋级
    'starter_promotion': {
        'kind': 'position_gap',
        'alloc_mult': 0.40,   # 原 0.60
        'position_mult': 0.80,
        'risk_mult': 0.70,
    },
    # 常规加码
    'add_position': {
        'kind': 'position_gap',
        'alloc_mult': 0.30,   # 原 0.50
        'position_mult': 0.40,
        'risk_mult': 0.70,
    },
    # 蓝筹趋势底仓
    'uptrend': {
        'kind': 'bucket_alloc',
        'alloc_mult': 0.40,
        'risk_mult': 0.80,
    },
    # 热门赛道小仓位
    'hot_sector': {
        'kind': 'bucket_alloc',
        'alloc_mult': 0.20,
        'risk_mult': 0.50,
    },
    # 盘前异动：小仓位试探
    'premarket_gap': {
        'kind': 'bucket_alloc',
        'alloc_mult': 0.25,   # 盘前不确定性高，小仓
        'risk_mult': 0.60,
    },
    # 动量加速：趋势中积极跟进
    'momentum_surge': {
        'kind': 'bucket_alloc',
        'alloc_mult': 0.50,
        'risk_mult': 0.90,
    },
    # RSI超卖反弹：低位布局，小仓试探
    'rsi_bounce': {
        'kind': 'bucket_alloc',
        'alloc_mult': 0.30,
        'risk_mult': 0.70,
    },
    # 布林带收窄突破：爆发行情，较积极
    'bb_breakout': {
        'kind': 'bucket_alloc',
        'alloc_mult': 0.45,
        'risk_mult': 0.85,
    },
    # 52周新高：机构买入信号，标准仓
    '52w_high': {
        'kind': 'bucket_alloc',
        'alloc_mult': 0.50,
        'risk_mult': 1.00,
    },
    # MACD零轴上穿：趋势确认，较积极
    'macd_zero_cross': {
        'kind': 'bucket_alloc',
        'alloc_mult': 0.55,
        'risk_mult': 0.95,
    },
    # 分批建仓 第二批（+5% 利润触发，占目标仓位约 30%）
    'pyramid_stage2': {
        'kind': 'bucket_alloc',
        'alloc_mult': 0.30,
        'risk_mult': 0.80,
    },
    # 分批建仓 第三批（+12% 利润触发，占目标仓位约 30%）
    'pyramid_stage3': {
        'kind': 'bucket_alloc',
        'alloc_mult': 0.30,
        'risk_mult': 0.70,
    },
}

ENTRY_REASON_LABEL: dict[str, str] = {
    'weekly_dca': '每周定投',
    'uptrend':        '趋势底仓',
    'hot_sector':     '热门赛道',
    'premarket_gap':   '盘前异动',
    'momentum_surge':  '动量加速',
    'rsi_bounce':      'RSI超卖反弹',
    'bb_breakout':     'BB收窄突破',
    '52w_high':        '52周新高',
    'macd_zero_cross': 'MACD零轴上穿',
    'golden_cross':    '金叉',
    'trend_pullback': '回踩确认',
    'breakout': '突破',
    'starter_promotion': 'starter晋级',
    'add_position': '加码',
    'micro_position': '底仓',
    'pyramid_stage2': '金字塔第二批',
    'pyramid_stage3': '金字塔第三批',
}


def reserve_cash(initial_cash: float,
                 reserve_ratio: float = CASH_RESERVE_RATIO) -> float:
    return max(0.0, initial_cash * reserve_ratio)


def available_cash(cash: float,
                   initial_cash: float,
                   reserve_ratio: float = CASH_RESERVE_RATIO) -> float:
    return max(0.0, cash - reserve_cash(initial_cash, reserve_ratio))


def target_bucket_cash(initial_cash: float, bucket_alloc: float) -> float:
    return max(0.0, initial_cash * bucket_alloc)


def is_starter_position(current_value: float,
                        initial_cash: float,
                        bucket_alloc: float,
                        threshold: float = 0.35) -> bool:
    target_cash = target_bucket_cash(initial_cash, bucket_alloc)
    if target_cash <= 0:
        return False
    return current_value <= target_cash * threshold


def entry_budget(cash: float,
                 initial_cash: float,
                 bucket_alloc: float,
                 reason: str,
                 reserve_ratio: float = CASH_RESERVE_RATIO,
                 current_position_value: float = 0.0,
                 use_dynamic_alloc: bool = True) -> float:
    """
    给定事件类型，返回本次允许使用的标准化预算。
    - fixed_cash    : 固定金额（底仓）
    - bucket_alloc  : 目标桶仓位 × 倍率
    - position_gap  : 目标仓位缺口内补仓，并限制补仓幅度

    Args:
        use_dynamic_alloc: 若 True，则通过 signal_stats 动态调整 alloc_mult。
                           开关式设计，回测中可传 False 保持确定性。
    """
    policy = ENTRY_SIZE_POLICY.get(reason)
    if policy is None:
        raise KeyError(f'未知入场原因: {reason}')

    liquid_cash = available_cash(cash, initial_cash, reserve_ratio)
    if liquid_cash <= 0:
        return 0.0

    kind = str(policy['kind'])
    if kind == 'fixed_cash':
        return min(liquid_cash, float(policy['cash']))

    bucket_cash = target_bucket_cash(initial_cash, bucket_alloc)
    if bucket_cash <= 0:
        return 0.0

    base_alloc_mult = float(policy.get('alloc_mult', 1.0))
    if use_dynamic_alloc and kind == 'bucket_alloc':
        # 延迟导入，避免循环依赖和启动时的性能开销
        try:
            from signal_stats import get_dynamic_alloc_mult
            alloc_mult = get_dynamic_alloc_mult(reason, base_alloc_mult)
        except Exception:
            alloc_mult = base_alloc_mult
    else:
        alloc_mult = base_alloc_mult

    budget = min(liquid_cash, bucket_cash * alloc_mult)

    if kind == 'position_gap':
        gap = max(0.0, bucket_cash - current_position_value)
        budget = min(budget, gap)
        if current_position_value > 0:
            pos_mult = float(policy.get('position_mult', 1.0))
            budget = min(budget, current_position_value * pos_mult)

    return max(0.0, budget)


def atr_position_qty(price: float,
                     atr: float,
                     budget: float,
                     risk_per_trade: float = 500.0,
                     atr_mult: float = 2.0,
                     reason: str = 'golden_cross') -> int:
    if price <= 0 or budget < price:
        return 0

    policy = ENTRY_SIZE_POLICY.get(reason, {})
    stop_dist = max(atr * atr_mult, price * 0.01)
    risk_cash = risk_per_trade * float(policy.get('risk_mult', 1.0))

    qty_risk = max(1, int(risk_cash / stop_dist))
    qty_budget = max(1, int(budget / price))
    return min(qty_risk, qty_budget)


def cash_position_qty(price: float, budget: float) -> int:
    if price <= 0 or budget < price:
        return 0
    return max(1, int(budget / price))
