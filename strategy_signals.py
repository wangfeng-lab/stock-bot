"""
strategy_signals.py — 共享指标与事件检测
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class IndicatorState:
    ind_str: str
    extra_buy: bool
    extra_sell: bool
    relaxed_buy: bool
    rsi_now: float | None = None
    volume_ratio: float | None = None


def calc_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    return 100 - 100 / (1 + gain / loss.replace(0, float('nan')))


def calc_macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    macd = close.ewm(span=fast, adjust=False).mean() - close.ewm(span=slow, adjust=False).mean()
    return macd, macd.ewm(span=signal, adjust=False).mean()


def calc_volume_ratio(df: pd.DataFrame, period: int = 20) -> float:
    if 'volume' not in df.columns or len(df) < period:
        return 1.0
    vol_ma = df['volume'].rolling(period).mean()
    base = float(vol_ma.iloc[-1]) if len(vol_ma) else 0.0
    cur = float(df['volume'].iloc[-1])
    return cur / base if base > 0 else 1.0


def calc_atr_series(df: pd.DataFrame, period: int = 14) -> pd.Series:
    h, l, c = df['high'], df['low'], df['close']
    tr = pd.concat([
        (h - l),
        (h - c.shift()).abs(),
        (l - c.shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def calc_atr_value(df: pd.DataFrame, period: int = 14) -> float:
    atr = calc_atr_series(df, period)
    value = float(atr.iloc[-1])
    close = float(df['close'].iloc[-1])
    return value if value == value else close * 0.02


def enrich_indicators(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    out = df.copy()
    out['fast_ma'] = out['close'].rolling(cfg['fast_ma']).mean()
    out['slow_ma_v'] = out['close'].rolling(cfg['slow_ma']).mean()
    if 'high' in out.columns and 'low' in out.columns:
        out['atr'] = calc_atr_series(out)
    if 'rsi_period' in cfg:
        out['rsi'] = calc_rsi(out['close'], cfg['rsi_period'])
    if 'macd_fast' in cfg:
        out['macd'], out['macd_sig'] = calc_macd(
            out['close'],
            cfg['macd_fast'],
            cfg['macd_slow'],
            cfg['macd_signal'],
        )
    if 'vol_period' in cfg:
        out['vol_ma'] = out['volume'].rolling(cfg['vol_period']).mean()
    return out


def indicator_state(bucket_name: str,
                    cfg: dict,
                    latest: pd.Series,
                    prev_row: pd.Series) -> IndicatorState:
    if bucket_name == 'conservative':
        rsi_now = float(latest.get('rsi', 50))
        return IndicatorState(
            ind_str=f"RSI={rsi_now:.0f}",
            extra_buy=rsi_now < cfg['rsi_buy'],
            extra_sell=rsi_now > cfg['rsi_sell'],
            relaxed_buy=rsi_now < cfg.get('add_rsi_max', 65),
            rsi_now=rsi_now,
        )

    if bucket_name == 'longterm':
        m = float(latest.get('macd', 0))
        s = float(latest.get('macd_sig', 0))
        mp = float(prev_row.get('macd', 0))
        sp = float(prev_row.get('macd_sig', 0))
        return IndicatorState(
            ind_str=f"MACD={m:.2f}/SIG={s:.2f}",
            extra_buy=m > s,
            extra_sell=mp > sp and m < s,
            relaxed_buy=m > s,
        )

    vol_period = int(cfg.get('vol_period', 20))
    vol_ma = float(latest.get('vol_ma', 0))
    volume_ratio = float(latest.get('volume', 0)) / max(vol_ma, 1)
    return IndicatorState(
        ind_str=f"量比={volume_ratio:.1f}x",
        extra_buy=volume_ratio >= cfg['vol_mult'],
        extra_sell=False,
        relaxed_buy=volume_ratio >= cfg.get('add_vol_mult', cfg['vol_mult']),
        volume_ratio=volume_ratio,
    )


def detect_uptrend(df: pd.DataFrame,
                   fast_now: float,
                   slow_now: float,
                   n: int = 3) -> tuple[str, str] | None:
    """
    蓝筹底仓入场：连续 N 根 K 线快线均在慢线上方。
    不要求具体形态（金叉/回踩），只确认整体趋势向上。
    """
    if fast_now <= slow_now or len(df) < n + 1:
        return None
    recent_fast = df['fast_ma'].iloc[-n:].dropna().values
    recent_slow = df['slow_ma_v'].iloc[-n:].dropna().values
    if len(recent_fast) < n or len(recent_slow) < n:
        return None
    if all(f > s for f, s in zip(recent_fast, recent_slow)):
        return 'uptrend', f"趋势确认({n}根快>慢线)"
    return None


def detect_entry_signal(cfg: dict,
                        df: pd.DataFrame,
                        latest: pd.Series,
                        prev_row: pd.Series,
                        fast_now: float,
                        slow_now: float,
                        fast_prev: float,
                        slow_prev: float,
                        signal_ok: bool) -> tuple[str, str] | None:
    """
    买入事件：
    1. golden_cross   新金叉
    2. trend_pullback 上升趋势中回踩快线后重新站上
    3. breakout       上升趋势中突破近期高点
    """
    entry_modes = set(cfg.get('entry_modes', ('golden_cross',)))
    price = float(latest['close'])
    ma_golden = fast_prev < slow_prev and fast_now > slow_now

    # 金叉是最基础信号，不要求附加指标确认
    if 'golden_cross' in entry_modes and ma_golden:
        return 'golden_cross', '新金叉'

    if fast_now <= slow_now:
        return None

    # 回踩信号：快线在慢线上方即可，不强制附加指标（模拟仓底仓观察模式）
    if 'trend_pullback' in entry_modes:
        lookback = int(cfg.get('pullback_lookback', 4))
        band = float(cfg.get('pullback_band', 0.012))
        reclaim_tol = float(cfg.get('pullback_reclaim_tol', 0.005))
        if len(df) >= lookback + 2:
            recent = df.iloc[-(lookback + 1):-1]
            low_col = 'low' if 'low' in recent.columns else 'close'
            recent_low = float(recent[low_col].min())
            prev_close = float(prev_row['close'])

            near_fast = recent_low <= fast_now * (1 + band)
            reclaim_ok = (
                prev_close <= fast_prev * (1 + reclaim_tol)
                and price > fast_now
                and price > prev_close
            )
            if near_fast and reclaim_ok:
                return 'trend_pullback', f"回踩{cfg['fast_ma']}MA后再站上"

    # 突破信号：同样不再强制 signal_ok
    if 'breakout' in entry_modes:
        lookback = int(cfg.get('breakout_lookback', 20))
        buffer = float(cfg.get('breakout_buffer', 0.002))
        vol_mult = float(cfg.get('breakout_vol_mult', 1.2))
        if len(df) >= lookback + 2:
            recent = df.iloc[-(lookback + 1):-1]
            high_col = 'high' if 'high' in recent.columns else 'close'
            recent_high = float(recent[high_col].max())
            volume_ratio = calc_volume_ratio(df, int(cfg.get('vol_period', 20)))
            if price >= recent_high * (1 + buffer) and volume_ratio >= vol_mult:
                return 'breakout', f"{lookback}bar突破 量比{volume_ratio:.1f}x"

    return None


# ── 盘前异动信号 ───────────────────────────────────────────────
def detect_premarket_signal(
    snap_row: pd.Series,
    min_gap_pct: float = 2.0,
    min_pre_vol: int = 50_000,
) -> tuple[str, str] | None:
    """
    盘前异动：盘前涨幅 ≥ min_gap_pct% 且成交量达到最低门槛 → 小仓跟进。
    数据来自 moomoo get_market_snapshot 的 pre_change_rate / pre_volume。
    """
    try:
        pre_chg = float(snap_row.get('pre_change_rate') or 0)
        pre_vol = float(snap_row.get('pre_volume')      or 0)
    except (TypeError, ValueError):
        return None

    if pre_chg < min_gap_pct:
        return None
    if pre_vol < min_pre_vol:
        return None

    return 'premarket_gap', f"盘前+{pre_chg:.1f}% 盘前量{pre_vol/1e4:.0f}万"


# ── 动量加速信号 ───────────────────────────────────────────────
def detect_momentum_surge(
    df: pd.DataFrame,
    fast_now: float,
    slow_now: float,
    vol_surge_mult: float = 2.0,
    price_accel_pct: float = 0.5,
) -> tuple[str, str] | None:
    """
    动量加速：uptrend 中量价齐升加速 → 不等回踩直接入场。

    条件：
      1. 快线 > 慢线（趋势确认）
      2. 最新 K 线单根涨幅 ≥ price_accel_pct%
      3. 当前成交量 ≥ vol_surge_mult × 20 根均量
    """
    if fast_now <= slow_now or len(df) < 22:
        return None

    prev_close = float(df.iloc[-2]['close'])
    cur_close  = float(df.iloc[-1]['close'])
    bar_chg    = (cur_close - prev_close) / prev_close * 100 if prev_close > 0 else 0

    if bar_chg < price_accel_pct:
        return None

    vol_ma    = float(df['volume'].iloc[-21:-1].mean())
    cur_vol   = float(df.iloc[-1].get('volume', 0))
    vol_ratio = cur_vol / vol_ma if vol_ma > 0 else 0

    if vol_ratio < vol_surge_mult:
        return None

    return 'momentum_surge', f"动量+{bar_chg:.1f}% 量{vol_ratio:.1f}x"


# ══════════════════════════════════════════════════════════
# 新增策略信号（散户实战四策略）
# ══════════════════════════════════════════════════════════

def calc_bollinger_bands(
    close: pd.Series,
    period: int = 20,
    std_mult: float = 2.0,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """返回 (mid, upper, lower) 布林带"""
    mid   = close.rolling(period).mean()
    std   = close.rolling(period).std()
    upper = mid + std_mult * std
    lower = mid - std_mult * std
    return mid, upper, lower


def detect_rsi_bounce(
    df: pd.DataFrame,
    oversold: float = 32.0,
    recover: float = 38.0,
    rsi_period: int = 14,
) -> tuple[str, str] | None:
    """
    RSI 超卖反弹策略（散户逻辑：跌深了要反弹）

    条件：
      - 前 N 根内 RSI 曾跌破 oversold（恐慌性抛售）
      - 最新 RSI 回升到 recover 以上（企稳迹象）
      - 价格在均线附近（不是纯粹跌势）

    适用：保守桶 / 成长桶的低位布局
    """
    if len(df) < rsi_period + 10:
        return None

    rsi = calc_rsi(df['close'], rsi_period)
    rsi_now  = float(rsi.iloc[-1])
    rsi_prev = float(rsi.iloc[-5:-1].min())   # 近5根最低RSI

    if rsi_prev > oversold:
        return None   # 没有真正超卖过
    if rsi_now < recover:
        return None   # 还没企稳

    return 'rsi_bounce', f"RSI超卖反弹 低点{rsi_prev:.0f}→{rsi_now:.0f}"


def detect_bollinger_breakout(
    df: pd.DataFrame,
    bb_period: int = 20,
    squeeze_threshold: float = 0.04,
    std_mult: float = 2.0,
) -> tuple[str, str] | None:
    """
    布林带收窄后向上突破（散户逻辑：盘整蓄力后爆发）

    条件：
      1. 近期布林带宽度 / 中轨 < squeeze_threshold（盘整收窄）
      2. 当前收盘价突破上轨
      3. 成交量配合

    适用：短线桶 / 成长桶的爆发行情捕捉
    """
    if len(df) < bb_period + 5:
        return None

    mid, upper, lower = calc_bollinger_bands(df['close'], bb_period, std_mult)

    # 近5根的布林带宽度均值（判断是否处于收窄状态）
    bw_recent = ((upper - lower) / mid).iloc[-6:-1].mean()
    if bw_recent > squeeze_threshold:
        return None   # 还没有足够收窄

    cur_price = float(df['close'].iloc[-1])
    cur_upper = float(upper.iloc[-1])

    if cur_price < cur_upper:
        return None   # 还未突破上轨

    # 成交量确认
    vol_ratio = calc_volume_ratio(df, bb_period)

    return 'bb_breakout', f"BB突破上轨{cur_upper:.2f} 宽度{bw_recent*100:.1f}% 量{vol_ratio:.1f}x"


def detect_52w_high_breakout(
    snap_row: pd.Series,
    current_price: float,
    within_pct: float = 0.03,
) -> tuple[str, str] | None:
    """
    52周新高突破（散户逻辑：创新高说明机构在买，跟上）

    条件：
      - 当前价格在52周高点的 within_pct 范围内或已突破
      - 不在52周低点附近（避免反弹陷阱）

    数据来源：moomoo 快照的 highest52weeks_price
    """
    try:
        high52 = float(snap_row.get('highest52weeks_price') or 0)
        low52  = float(snap_row.get('lowest52weeks_price')  or 0)
    except (TypeError, ValueError):
        return None

    if high52 <= 0 or current_price <= 0:
        return None

    dist_from_high = (high52 - current_price) / high52

    # 在高点 within_pct 以内或已突破
    if dist_from_high > within_pct:
        return None

    # 不能太靠近52周低点（说明只是弱势反弹）
    if low52 > 0:
        range52 = high52 - low52
        if range52 > 0 and (current_price - low52) / range52 < 0.6:
            return None

    pct_str = f"距高点{dist_from_high*100:.1f}%" if dist_from_high > 0 else "突破52周高"
    return '52w_high', f"52周新高区域 {pct_str}"


def detect_macd_zero_cross(
    df: pd.DataFrame,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[str, str] | None:
    """
    MACD 零轴上穿（散户逻辑：趋势由弱转强的明确信号）

    条件：
      - 前一根 MACD < 0
      - 当前 MACD > 0（首次穿越零轴）
      - MACD 柱状图扩大（动量加速）

    比普通金叉更强：零轴上穿意味着短期均线整体超过长期均线
    """
    if len(df) < slow + signal + 5:
        return None

    macd_line, sig_line = calc_macd(df['close'], fast, slow, signal)
    hist = macd_line - sig_line

    m_now  = float(macd_line.iloc[-1])
    m_prev = float(macd_line.iloc[-2])
    h_now  = float(hist.iloc[-1])
    h_prev = float(hist.iloc[-2])

    # 零轴上穿
    if not (m_prev < 0 and m_now > 0):
        return None

    # 柱状图扩大（动量增强）
    if h_now <= h_prev:
        return None

    return 'macd_zero_cross', f"MACD零轴上穿 {m_prev:.3f}→{m_now:.3f}"
