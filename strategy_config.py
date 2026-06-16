"""
strategy_config.py — 统一策略配置与共享常量

这里放：
1. 各桶参数
2. 研究/模拟共用常量
3. 股票宇宙与行业映射
"""

from __future__ import annotations

from moomoo import KLType

from discussion_universe import build_watch_universe, discussion_codes, load_discussion_universe
from execution_policy import CASH_RESERVE_RATIO, ENTRY_SIZE_POLICY

INITIAL_CASH = 1_000_000.0
CASH_RESERVE = CASH_RESERVE_RATIO

CRASH_THRESHOLD = -0.08
CRASH_RSI_ALERT = 25

MICRO_ALLOC = float(ENTRY_SIZE_POLICY['micro_position']['cash'])
MICRO_TARGET_POS = 8
MICRO_MIN_POS = 5
MICRO_MAX_POS = 10
MICRO_SECTOR_CAP = 1
MICRO_SECTOR_CAP_OVERRIDES = {
    'AI软件/云': 2,
}
MICRO_REQUIRED_SECTORS = ('太空国防',)
MICRO_MIN_SCORE = 6.0
DYNAMIC_INTERVAL = 900
WEEKLY_DCA_INTERVAL = 900
WEEKLY_DCA_WEEKDAY_ET = 0
WEEKLY_DCA_MIN_HOUR_ET = 10
WEEKLY_DCA_PLAN: dict[str, int] = {
    'US.QQQ': 1,
    'US.VOO': 1,
}
SLOW_FUND_REFRESH_INTERVAL = 21600
SLOW_FUND_MIN_SCORE = {
    'conservative': 56.0,
    'longterm': 48.0,
    'shortterm': 35.0,
}
SLOW_FUND_TIER_FULL = 75.0
SLOW_FUND_TIER_MID = 60.0
REENTRY_COOLDOWN_MINUTES = 30
MICRO_REENTRY_COOLDOWN_MINUTES = 180
DISCUSSION_WATCH_LIMIT = 40
ORDER_MATCH_INTERVAL = 5
ORDER_MIN_FILL_AGE_SECONDS = 5
ORDER_TIMEOUT_SECONDS = 120

ADD_MIN_PROFIT = 0.05
ADD_MIN_DAYS = 7
ADD_RSI_MAX = 65

# ── 分批建仓（Pyramid Entry）参数 ─────────────────────────
PYRAMID_ADD1_PROFIT = 0.03   # +3%  触发第二批加仓（原 +5%）
PYRAMID_ADD1_DAYS   = 3      # 最短持仓天数（原 7 天）
PYRAMID_ADD2_PROFIT = 0.07   # +7%  触发第三批加仓（原 +12%）
PYRAMID_ADD2_DAYS   = 7      # 最短持仓天数（原 14 天）

# ── 分批止盈（Tiered Profit-Taking）参数 ──────────────────
PROFIT_TAKE1_PCT   = 0.04    # +4%  止盈第一批（原 +8%）
PROFIT_TAKE2_PCT   = 0.08    # +8%  止盈第二批（原 +15%）
# 剩余约 30% 仓位由移动止损（trail_stop）管理
DEFAULT_TRAIL_ATR_MULT        = 1.2   # 收紧：原 1.5（更快触发移动止损）
DEFAULT_TRAIL_ACTIVATE_PROFIT = 0.02  # 盈利 2% 即激活移动止损（原 3%）
DEFAULT_BREAK_EVEN_PROFIT     = 0.03  # 盈利 3% 即保本（原 5%）
DEFAULT_BREAK_EVEN_BUFFER     = 0.001

RISK_PER_TRADE = 200.0   # 每笔风险敞口上限（原 $500，调小使仓位更分散）
ATR_STOP_MULT = 2.0
BACKTEST_SLIPPAGE_BPS = 5.0
BACKTEST_LOOKBACK_BUFFER = 60


def _unique_codes(codes: list[str]) -> list[str]:
    return list(dict.fromkeys(codes))

BUCKET_LABEL = {
    'conservative': '保守',
    'longterm': '成长',
    'shortterm': '短线',
    'dca': '定投',
    'micro': '底仓',
}
BUCKET_ORDER = ['conservative', 'longterm', 'shortterm', 'dca', 'micro']

BUCKETS: dict[str, dict] = {
    'conservative': {
        'label': '保守',
        'stocks': [
            'US.AAPL', 'US.MSFT', 'US.GOOGL', 'US.META',
            'US.AVGO', 'US.ORCL', 'US.AMZN',
            'US.V', 'US.MA', 'US.DELL', 'US.HPQ',  # 惠普
            'US.ADBE', 'US.CSCO', 'US.IBM', 'US.INTU', 'US.SAP',
        ],
        'max_pos': 6,
        'alloc': 0.05,   # 原 0.18 → 单桶上限 $50k，每笔更小
        'stop_loss': 0.06,             # 原 0.08，更快止损
        'fast_ma': 10,
        'slow_ma': 50,
        'ktype': KLType.K_DAY,
        'backtest_ktype': KLType.K_DAY,
        'interval': 3600,
        'rsi_period': 14,
        'rsi_buy': 70,
        'rsi_sell': 72,                # 原 78，RSI 稍高即卖（更活跃）
        'add_rsi_max': 65,
        'entry_modes': ('golden_cross', 'trend_pullback'),
        'pullback_lookback': 4,
        'pullback_band': 0.012,
        'pullback_reclaim_tol': 0.005,
        'starter_ratio': 0.40,
        'trail_activate_profit': 0.02, # 原 0.03
        'break_even_profit': 0.04,     # 原 0.08
        'trail_atr_mult': 1.2,         # 原 1.5，更紧
    },
    'longterm': {
        'label': '成长',
        'stocks': [
            'US.NVDA', 'US.AMD', 'US.MU', 'US.AMAT',
            'US.MRVL', 'US.KLAC', 'US.LRCX', 'US.ARM', 'US.QCOM',
            'US.WDC', 'US.STX', 'US.SNDK', 'US.DRAM', # 存储：HDD/NAND/SanDisk/ETF
            'US.CIEN', 'US.COHR', 'US.GLW',        # 光模块（+康宁）
            'US.TSM',                              # 台积电
            'US.INTC', 'US.MX',                    # 芯片
            'US.CRWV',                             # CoreWeave AI云
            'US.GLD',                              # 黄金ETF
            'US.ADI', 'US.NXPI', 'US.GFS', 'US.MPWR', 'US.TER', 'US.ENTG',
        ],
        'max_pos': 10,      # 持仓数多，但每笔更小
        'alloc': 0.04,   # 原 0.10 → 单桶上限 $40k
        'stop_loss': 0.05,             # 原 0.07
        'fast_ma': 5,
        'slow_ma': 20,
        'ktype': KLType.K_DAY,
        'backtest_ktype': KLType.K_DAY,
        'interval': 1800,
        'macd_fast': 12,
        'macd_slow': 26,
        'macd_signal': 9,
        'entry_modes': ('golden_cross', 'trend_pullback', 'breakout'),
        'pullback_lookback': 5,
        'pullback_band': 0.015,
        'pullback_reclaim_tol': 0.006,
        'breakout_lookback': 20,
        'breakout_buffer': 0.002,
        'breakout_vol_mult': 1.0,
        'starter_ratio': 0.35,
        'trail_activate_profit': 0.02, # 原 0.04
        'break_even_profit': 0.05,     # 原 0.10
        'trail_atr_mult': 1.2,         # 原 1.5
        'prefer_uptrend_entry': True,
    },
    'shortterm': {
        'label': '短线',
        'stocks': [
            'US.TSLA', 'US.NOK',                              # 特斯拉、诺基亚
            'US.ASTS', 'US.POET', 'US.FOTO',               # 卫星互联网、光子芯片
            'US.UUUU', 'US.OKLO',                          # 铀矿、小型核反应堆
            'US.AEP',                                       # 电力大盘
            'US.DXYZ', 'US.PS', 'US.XE', 'US.ONDS',       # 新兴/无人机
            'US.PLTR', 'US.APP', 'US.NOW', 'US.CRWD',
            'US.DDOG', 'US.PANW', 'US.SMCI',
            'US.KTOS', 'US.RKLB', 'US.LUNR', 'US.RDW',  # 太空（+Redwire）
            'US.VST', 'US.CEG', 'US.NRG',
            'US.FCX', 'US.MP',
            'US.LITE', 'US.VIAV',               # 光模块（Lumentum / Viavi）
            'US.CCJ',                            # 铀矿/核能（Cameco）
            'US.AXON',                           # 警务/执法科技
            'US.NET', 'US.ZS', 'US.PEGA', 'US.AI',
            'US.AAOI', 'US.AVAV', 'US.FSLR', 'US.FN',
        ],
        'max_pos': 10,      # 短线分散更多
        'alloc': 0.03,   # 原 0.06 → 单桶上限 $30k
        'stop_loss': 0.04,             # 短线更快止损（原 0.05）
        'fast_ma': 5,
        'slow_ma': 20,
        'ktype': KLType.K_5M,
        # 回测仍默认用日线近似，避免短线桶在本地请求超长 5 分钟数据时失真过大。
        'backtest_ktype': KLType.K_DAY,
        'interval': 180,
        'vol_period': 20,
        'vol_mult': 1.0,       # 测试期进一步放宽
        'add_vol_mult': 1.0,   # 加码/回踩确认不强制要求放量
        'entry_modes': ('golden_cross', 'trend_pullback', 'breakout'),
        'pullback_lookback': 4,
        'pullback_band': 0.010,
        'pullback_reclaim_tol': 0.003,
        'breakout_lookback': 15,
        'breakout_buffer': 0.001,
        'breakout_vol_mult': 1.0,   # 放宽：不再要求放量突破
        'starter_ratio': 0.35,
        'trail_activate_profit': 0.01, # 盈利 1% 即激活移动止损
        'break_even_profit': 0.02,     # 盈利 2% 保本
        'break_even_buffer': 0.001,
        'trail_atr_mult': 1.0,         # 更紧移动止损（原 1.3）
        'time_stop_days': 2,           # 2 天不涨就走（原 3 天）
        'time_stop_min_return': 0.01,  # 达不到 1% 就时间止损（原 2%）
        'prefer_uptrend_entry': True,
    },
}

WATCH_SECTOR_GROUPS: dict[str, list[str]] = {
    '大型科技': [
        'US.AAPL', 'US.MSFT', 'US.GOOGL', 'US.META', 'US.AMZN',
        'US.TSLA', 'US.NFLX', 'US.UBER', 'US.ABNB', 'US.DELL',
        'US.ADBE', 'US.CSCO', 'US.IBM', 'US.INTU', 'US.SHOP',
        'US.SAP', 'US.SPOT', 'US.PINS', 'US.SNAP', 'US.ROKU', 'US.EBAY',
    ],
    'AI芯片': [
        'US.NVDA', 'US.AMD', 'US.MU', 'US.AMAT', 'US.MRVL',
        'US.KLAC', 'US.LRCX', 'US.ARM', 'US.QCOM', 'US.AVGO',
        'US.INTC', 'US.TXN', 'US.ASML', 'US.MCHP', 'US.ON',
        'US.SWKS', 'US.ADI', 'US.NXPI', 'US.GFS', 'US.MPWR',
        'US.AMKR', 'US.TER', 'US.QRVO', 'US.LSCC', 'US.ENTG',
        'US.AMBA', 'US.ACLS',
    ],
    '存储': ['US.MU', 'US.WDC', 'US.STX', 'US.SNDK', 'US.DRAM', 'US.NTAP', 'US.NTNX'],
    '光模块': ['US.CIEN', 'US.COHR', 'US.LITE', 'US.VIAV', 'US.AAOI', 'US.FN', 'US.CALX', 'US.GLW', 'US.POET'],
    'AI软件': [
        'US.PLTR', 'US.APP', 'US.NOW', 'US.CRWD', 'US.DDOG',
        'US.PANW', 'US.SMCI', 'US.ORCL', 'US.CRM', 'US.SNOW',
        'US.MDB', 'US.GTLB', 'US.ZS', 'US.NET', 'US.HUBS',
        'US.BILL', 'US.SNPS', 'US.CDNS', 'US.TEAM', 'US.AI',
        'US.ESTC', 'US.DOCN', 'US.PEGA', 'US.PATH', 'US.OKTA',
        'US.S', 'US.FTNT', 'US.PCOR', 'US.MNDY',
    ],
    '金融科技': ['US.V', 'US.MA', 'US.PYPL', 'US.XYZ', 'US.COIN', 'US.HOOD', 'US.SOFI', 'US.AFRM', 'US.JKHY', 'US.FIS', 'US.GPN', 'US.NU', 'US.UPST'],
    '太空国防': ['US.KTOS', 'US.RKLB', 'US.LUNR', 'US.RDW', 'US.ASTS', 'US.LMT', 'US.RTX', 'US.NOC', 'US.AXON', 'US.GD', 'US.LHX', 'US.LDOS', 'US.BA', 'US.HII', 'US.MRCY', 'US.AVAV', 'US.HEI'],
    '电力能源': ['US.VST', 'US.CEG', 'US.NRG', 'US.NEE', 'US.AES', 'US.CCJ', 'US.UUUU', 'US.OKLO', 'US.VRT', 'US.ETN', 'US.GEV', 'US.PWR', 'US.DUK', 'US.SO', 'US.AEP', 'US.FSLR'],
    '金属矿物': ['US.FCX', 'US.MP', 'US.VALE', 'US.NEM', 'US.SCCO', 'US.BHP', 'US.RIO', 'US.NUE', 'US.CLF'],
    '医疗/生物': ['US.LLY', 'US.MRNA', 'US.ABBV', 'US.TMO', 'US.ISRG', 'US.DHR', 'US.UNH', 'US.ABT', 'US.SYK', 'US.MDT', 'US.AMGN', 'US.REGN'],
    '消费/零售': ['US.COST', 'US.WMT', 'US.TGT', 'US.SBUX', 'US.HD', 'US.KO', 'US.PEP', 'US.MCD', 'US.LOW', 'US.TJX'],
    '指数ETF': ['US.QQQ', 'US.VOO', 'US.SPY', 'US.IWM', 'US.VTI'],
}
DISCUSSION_FEED = load_discussion_universe()
DISCUSSION_UNIVERSE: list[str] = discussion_codes(DISCUSSION_FEED, limit=200)
SECTOR_GROUPS = WATCH_SECTOR_GROUPS
SECTOR_ORDER = list(WATCH_SECTOR_GROUPS.keys())
SECTOR_MAP = {
    code: sector
    for sector, stocks in WATCH_SECTOR_GROUPS.items()
    for code in stocks
}

MICRO_SECTOR_UNIVERSE: dict[str, list[str]] = {
    '大型科技': ['US.TSLA', 'US.NFLX', 'US.UBER', 'US.ABNB'],
    'AI芯片': ['US.AMD', 'US.QCOM', 'US.INTC', 'US.TXN', 'US.ON'],
    'AI软件/云': ['US.CRM', 'US.NOW', 'US.SNOW', 'US.MDB', 'US.NET', 'US.ZS', 'US.GTLB'],
    '金融科技': ['US.V', 'US.MA', 'US.PYPL', 'US.COIN', 'US.HOOD'],
    '太空国防': ['US.RKLB', 'US.KTOS', 'US.LMT', 'US.RTX'],
    '电力能源': ['US.NEE', 'US.AES', 'US.NRG'],
    '金属矿物': ['US.FCX', 'US.MP', 'US.VALE', 'US.NEM'],
    '医疗/生物': ['US.LLY', 'US.MRNA', 'US.ABBV', 'US.TMO'],
    '消费/零售': ['US.COST', 'US.WMT', 'US.TGT', 'US.SBUX'],
}


def bucket_stocks(bucket_names: list[str] | None = None) -> list[str]:
    names = bucket_names or list(BUCKETS.keys())
    return _unique_codes(sorted({
        stock
        for bucket_name in names
        for stock in BUCKETS[bucket_name]['stocks']
    }))


def micro_stocks() -> list[str]:
    return _unique_codes([
        stock
        for stocks in MICRO_SECTOR_UNIVERSE.values()
        for stock in stocks
    ])


BASE_WATCH_UNIVERSE: list[str] = _unique_codes([
    code
    for stocks in WATCH_SECTOR_GROUPS.values()
    for code in stocks
] + bucket_stocks())
WATCH_UNIVERSE: list[str] = build_watch_universe(
    BASE_WATCH_UNIVERSE,
    DISCUSSION_FEED,
    extra_limit=DISCUSSION_WATCH_LIMIT,
)
MICRO_UNIVERSE: list[str] = micro_stocks()
TRADE_UNIVERSE: list[str] = _unique_codes(
    bucket_stocks()
    + list(WEEKLY_DCA_PLAN.keys())
)

# 兼容旧引用：UNIVERSE 现在明确表示“观察池”
UNIVERSE = WATCH_UNIVERSE
