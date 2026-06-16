import calendar as pycal
import streamlit as st
import pandas as pd
import json, os, time
from html import escape
from datetime import datetime, timedelta
from moomoo import OpenQuoteContext, RET_OK
from typing import cast

from discussion_universe import build_watch_universe, discussion_codes, load_discussion_universe
from fundamental_model import score_slow_fundamentals
from fundamental_store import load_fundamental_cache
from local_broker import LocalBroker
from market_utils import (
    live_price_from_row,
    display_price_from_row,
    current_session, next_session_info, fmt_countdown,
    SESSION_LABEL, SESSION_COLOR, now_et,
)
from performance import calc_pnl_metrics
from recurring_invest import current_new_york_time, week_marker
from screener import fundamental_score, volume_signal
from strategy_config import (
    BASE_WATCH_UNIVERSE,
    BUCKET_LABEL,
    BUCKET_ORDER,
    DISCUSSION_WATCH_LIMIT,
    SECTOR_MAP,
    SECTOR_ORDER,
    TRADE_UNIVERSE,
    WEEKLY_DCA_MIN_HOUR_ET,
    WEEKLY_DCA_PLAN,
    WEEKLY_DCA_WEEKDAY_ET,
)

BASE           = os.path.dirname(__file__)
BROKER_DB      = os.path.join(BASE, 'virtual_account.json')
BROKER_SQLITE  = os.path.join(BASE, 'virtual_account.sqlite3')
LOG_FILE       = os.path.join(BASE, 'trade_log.csv')
CUSTOM_WL_FILE = os.path.join(BASE, 'custom_watchlist.json')
REFRESH        = 15

def _load_custom_wl() -> list[str]:
    try:
        with open(CUSTOM_WL_FILE) as f:
            return json.load(f)
    except Exception:
        return []

def _save_custom_wl(codes: list[str]):
    with open(CUSTOM_WL_FILE, 'w') as f:
        json.dump(sorted(set(codes)), f, indent=2)

DISCUSSION_FEED = load_discussion_universe()
DISCUSSION_UNIVERSE = discussion_codes(DISCUSSION_FEED, limit=200)
_base_watch = build_watch_universe(
    BASE_WATCH_UNIVERSE,
    DISCUSSION_FEED,
    extra_limit=DISCUSSION_WATCH_LIMIT,
)
# 合并自选股（文件在 sidebar 初始化之前先读一次）
_custom_init = _load_custom_wl() if os.path.exists(CUSTOM_WL_FILE) else []
WATCH_UNIVERSE = sorted(set(_base_watch) | set(_custom_init))
ALL_STOCKS = WATCH_UNIVERSE

# ── 页面配置 ─────────────────────────────────────────────────
st.set_page_config(page_title="Portfolio Bot", layout="wide", page_icon="📈")

# ── 深浅主题切换（侧边栏）────────────────────────────────────
if 'dark_mode' not in st.session_state:
    st.session_state.dark_mode = False

if 'custom_wl' not in st.session_state:
    st.session_state.custom_wl = _load_custom_wl()

with st.sidebar:
    st.markdown("### ⚙️ 设置")
    dark_val = st.toggle("🌙 深色模式", value=st.session_state.dark_mode)
    if dark_val != st.session_state.dark_mode:
        st.session_state.dark_mode = dark_val
        st.rerun()
    st.caption(f"每 {REFRESH} 秒自动刷新")

    # ── 交易时段指示器 ────────────────────────────────────
    _sess     = current_session()
    _sess_lbl = SESSION_LABEL.get(_sess, _sess)
    _sess_clr = SESSION_COLOR.get(_sess, '#888')
    _nxt      = next_session_info()
    _et_now   = now_et()

    st.markdown(f"""
<div style="
    background:{_sess_clr}18;
    border:1px solid {_sess_clr}55;
    border-radius:10px;
    padding:10px 14px;
    margin:6px 0 4px;
">
  <div style="font-size:.75rem;color:{_sess_clr};font-weight:700;letter-spacing:.06em">
    {_sess_lbl}
  </div>
  <div style="font-size:.7rem;color:#888;margin-top:3px">
    美东 {_et_now.strftime('%H:%M:%S')}
  </div>
  <div style="font-size:.72rem;color:#aaa;margin-top:4px">
    {_nxt['label']} <b style="color:{_sess_clr}">{fmt_countdown(_nxt['seconds'])}</b>
  </div>
</div>
""", unsafe_allow_html=True)
    st.divider()

    # ── 自选股添加 ────────────────────────────────────────
    st.markdown("#### 📌 添加自选股")
    _input = st.text_input(
        "输入股票代码（如 AAPL）",
        placeholder="NVDA / TSLA / ASTS ...",
        key="wl_input",
        label_visibility="collapsed",
    )
    col_add, col_clear = st.columns([1, 1])
    with col_add:
        if st.button("➕ 添加", use_container_width=True):
            raw = _input.strip().upper()
            codes_to_add = [c.strip() for c in raw.replace(',', ' ').split() if c.strip()]
            added = []
            for code in codes_to_add:
                full = f"US.{code}" if not code.startswith("US.") else code
                if full not in st.session_state.custom_wl:
                    st.session_state.custom_wl.append(full)
                    added.append(code)
            if added:
                _save_custom_wl(st.session_state.custom_wl)
                st.success(f"已添加：{', '.join(added)}")
                st.rerun()

    # 显示当前自选股，可逐个删除
    if st.session_state.custom_wl:
        st.markdown(f"**自选股（{len(st.session_state.custom_wl)} 只）**")
        for code in list(st.session_state.custom_wl):
            label = code.replace("US.", "")
            c1, c2 = st.columns([3, 1])
            c1.markdown(f"`{label}`")
            if c2.button("✕", key=f"rm_{code}"):
                st.session_state.custom_wl.remove(code)
                _save_custom_wl(st.session_state.custom_wl)
                st.rerun()
    with col_clear:
        if st.session_state.custom_wl and st.button("🗑 清空", use_container_width=True):
            st.session_state.custom_wl = []
            _save_custom_wl([])
            st.rerun()

    st.divider()
    st.caption(f"行情来源：Moomoo OpenD\n交易：本地虚拟撮合")
    st.caption(
        f"观察池 {len(WATCH_UNIVERSE)} 只  |  可交易池 {len(TRADE_UNIVERSE)} 只"
        f"  |  讨论池 {len(DISCUSSION_UNIVERSE)} 只"
    )

DARK = st.session_state.dark_mode

# 主题色变量
BG       = "#0a0a0f"    if DARK else "#f8fafc"
BG2      = "#13131a"    if DARK else "#ffffff"
BORDER   = "#1e293b"    if DARK else "#e2e8f0"
TEXT     = "#e2e8f0"    if DARK else "#0f172a"
TEXT2    = "#64748b"    if DARK else "#64748b"
HERO_BG  = ("linear-gradient(135deg,#0f172a 0%,#1e1b4b 60%,#0f172a 100%)"
            if DARK else
            "linear-gradient(135deg,#eff6ff 0%,#eef2ff 60%,#eff6ff 100%)")
HERO_VAL = "#f8fafc"    if DARK else "#0f172a"
KPI_BG   = "#0d1117"    if DARK else "#ffffff"
KPI_BOR  = "#1e293b"    if DARK else "#e2e8f0"
KPI_VAL  = "#f1f5f9"    if DARK else "#0f172a"

st.markdown(f"""
<style>
/* 全局背景和文字 */
html, body, .stApp, [class*="css"] {{
    background-color: {BG} !important;
    color: {TEXT} !important;
}}

/* 隐藏 Streamlit 顶部白色 header 条 */
[data-testid="stHeader"],
header[data-testid="stHeader"],
.stApp > header,
[data-testid="stDecoration"],
[data-testid="stAppDeployButton"],
.stDeployButton,
#stDecoration {{
    display: none !important;
    height: 0 !important;
    min-height: 0 !important;
    background: transparent !important;
}}

/* 侧边栏完整深色 */
section[data-testid="stSidebar"],
section[data-testid="stSidebar"] > div,
section[data-testid="stSidebar"] > div > div {{
    background-color: {"#0d1117" if DARK else "#f8fafc"} !important;
    border-right: 1px solid {BORDER} !important;
}}
section[data-testid="stSidebar"] label,
section[data-testid="stSidebar"] p,
section[data-testid="stSidebar"] span,
section[data-testid="stSidebar"] small {{
    color: {TEXT} !important;
}}

/* Tab 栏背景 */
[data-testid="stTabs"] > div:first-child {{
    background-color: {BG} !important;
}}

/* 输入/toggle 控件适配 */
.stToggle span {{ color: {TEXT} !important; }}

/* 隐藏刷新动画和顶部工具栏 */
[data-testid="stStatusWidget"]  {{ display: none !important; }}
[data-testid="stToolbar"]        {{ display: none !important; }}

.stDataFrame > div, .stDataFrame iframe {{ background: transparent !important; }}

/* 涨跌语义色 */
.up   {{ color: #22c55e !important; font-weight: 600; }}
.down {{ color: #ef4444 !important; font-weight: 600; }}
.warn {{ color: #f59e0b !important; font-weight: 600; }}
.flat {{ color: #6b7280; }}

.hero {{
    background: {HERO_BG};
    border: 1px solid {BORDER};
    border-radius: 20px;
    padding: 32px 36px 28px;
    margin-bottom: 16px;
}}
.hero-tag   {{ font-size:.7rem; color:{TEXT2}; text-transform:uppercase; letter-spacing:.1em; margin-bottom:8px; }}
.hero-value {{ font-size:3.2rem; font-weight:800; color:{HERO_VAL}; letter-spacing:-.03em; line-height:1; }}
.hero-row   {{ display:flex; align-items:center; gap:24px; margin-top:12px; flex-wrap:wrap; }}
.hero-item  {{ display:flex; flex-direction:column; }}
.hero-num   {{ font-size:1.1rem; font-weight:700; }}
.hero-lbl   {{ font-size:.7rem; color:{TEXT2}; margin-top:2px; }}

.kpi-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:10px; margin-bottom:20px; }}
.kpi {{
    background:{KPI_BG}; border:1px solid {KPI_BOR};
    border-radius:14px; padding:18px 20px; transition:border-color .15s;
}}
.kpi:hover  {{ border-color:#3b82f6; }}
.kpi-label  {{ font-size:.65rem; color:{TEXT2}; text-transform:uppercase; letter-spacing:.08em; }}
.kpi-value  {{ font-size:1.4rem; font-weight:700; color:{KPI_VAL}; margin:5px 0 2px; line-height:1; }}
.kpi-sub    {{ font-size:.72rem; color:{TEXT2}; }}
.kpi-subline {{ font-size:.72rem; color:{TEXT2}; margin-top:4px; line-height:1.35; }}

.badge {{
    display:inline-flex; align-items:center; white-space:nowrap;
    padding:2px 10px; border-radius:99px;
    font-size:.7rem; font-weight:600;
    background:{"#1e293b" if DARK else "#e2e8f0"};
    color:{"#94a3b8" if DARK else "#475569"};
}}
.badge-row {{
    display:flex; flex-wrap:wrap; gap:8px;
    margin-bottom:8px;
}}
.group-header {{
    display:flex; align-items:center; gap:10px;
    padding:10px 0 6px; border-bottom:1px solid {BORDER}; margin-bottom:6px;
}}
.group-title {{ font-size:.85rem; font-weight:600; color:{"#cbd5e1" if DARK else "#1e293b"}; }}
.group-meta  {{ font-size:.72rem; color:{TEXT2}; }}

.pnl-calendar {{
    border:1px solid {BORDER};
    border-radius:16px;
    padding:14px;
    background:{BG2};
}}
.pnl-calendar-grid {{
    display:grid;
    grid-template-columns:repeat(7,minmax(0,1fr));
    gap:8px;
}}
.pnl-calendar-head {{
    font-size:.72rem;
    font-weight:700;
    color:{TEXT2};
    text-align:center;
    padding:4px 0 6px;
}}
.pnl-calendar-cell {{
    min-height:90px;
    border-radius:12px;
    border:1px solid {BORDER};
    padding:10px 10px 8px;
    display:flex;
    flex-direction:column;
    gap:6px;
}}
.pnl-calendar-empty {{
    opacity:.45;
}}
.pnl-calendar-day {{
    font-size:.78rem;
    font-weight:700;
    color:{TEXT};
}}
.pnl-calendar-pnl {{
    font-size:.82rem;
    font-weight:800;
    line-height:1.1;
}}
.pnl-calendar-meta {{
    font-size:.68rem;
    color:{TEXT2};
    line-height:1.25;
}}

#MainMenu, footer {{ visibility:hidden; }}
.block-container  {{ padding-top:1.2rem; padding-bottom:2rem; }}
</style>
""", unsafe_allow_html=True)

# ── 数据加载 ─────────────────────────────────────────────────

@st.cache_data(ttl=15)
def load_prices(stocks: tuple) -> dict:
    result = {}
    sess = current_session()   # 取一次，整批用同一时段
    try:
        ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
        batch_size = 25
        for i in range(0, len(stocks), batch_size):
            chunk = list(stocks[i:i + batch_size])
            ret, data = ctx.get_market_snapshot(chunk)
            if ret != RET_OK:
                # 单个坏代码不应让整页雷达失效
                for code in chunk:
                    ret_one, data_one = ctx.get_market_snapshot([code])
                    if ret_one != RET_OK:
                        continue
                    for _, r in data_one.iterrows():
                        last      = float(r['last_price']           or 0)
                        overnight = float(r['overnight_price']      or 0)
                        pre       = float(r['pre_price']            or 0)
                        live = display_price_from_row(r, session=sess)
                        result[str(r['code'])] = {
                            'live':    live,
                            'last':    last,
                            'prev':    float(r['prev_close_price']     or 0),
                            'high52':  float(r['highest52weeks_price'] or 0),
                            'low52':   float(r['lowest52weeks_price']  or 0),
                            'pe':      float(r['pe_ttm_ratio']         or 0),
                            'eps':     float(r['earning_per_share']    or 0),
                            'mkt_cap': float(r['total_market_val']     or 0),
                            'overnight': overnight,
                            'overnight_chg': float(r['overnight_change_rate'] or 0),
                            'pre_p':   pre,
                            'pre_chg': float(r['pre_change_rate']     or 0),
                        }
                continue

            for _, r in data.iterrows():
                last      = float(r['last_price']           or 0)
                overnight = float(r['overnight_price']      or 0)
                pre       = float(r['pre_price']            or 0)
                live = display_price_from_row(r, session=sess)
                result[str(r['code'])] = {
                    'live':    live,          # 当前最新价（展示用）
                    'last':    last,          # 收盘价（评分参考用）
                    'prev':    float(r['prev_close_price']     or 0),
                    'high52':  float(r['highest52weeks_price'] or 0),
                    'low52':   float(r['lowest52weeks_price']  or 0),
                    'pe':      float(r['pe_ttm_ratio']         or 0),
                    'eps':     float(r['earning_per_share']    or 0),
                    'mkt_cap': float(r['total_market_val']     or 0),
                    'overnight': overnight,
                    'overnight_chg': float(r['overnight_change_rate'] or 0),
                    'pre_p':   pre,
                    'pre_chg': float(r['pre_change_rate']     or 0),
                }
        ctx.close()
        return result
    except Exception as e:
        st.toast(f"行情异常：{e}", icon="⚠️")
    return result

@st.cache_data(ttl=1800)
def load_klines(stocks: tuple) -> dict:
    result = {}
    try:
        from moomoo import OpenQuoteContext, KLType, AuType
        ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
        for idx, s in enumerate(stocks):
            try:
                r, df, _ = ctx.request_history_kline(
                    s, ktype=KLType.K_DAY, autype=AuType.QFQ, max_count=60
                )
                df = cast(pd.DataFrame, df)
                if r == RET_OK and len(df) >= 20:
                    result[s] = df
            except Exception:
                pass
            if idx and idx % 20 == 0:
                time.sleep(0.3)
        ctx.close()
    except Exception:
        pass
    return result

def load_account() -> dict:
    if not os.path.exists(BROKER_DB) and not os.path.exists(BROKER_SQLITE):
        return {'initial_cash':1_000_000,'cash':1_000_000,
                'reserved_cash':0,'realized_pnl':0,'total_commission':0,
                'positions':{},'orders':[],'fills':[],'meta':{'markers':{}}}
    broker = LocalBroker(BROKER_DB, LOG_FILE, initial_cash=1_000_000.0)
    return broker.get_state()

def load_trades() -> pd.DataFrame:
    if not os.path.exists(LOG_FILE):
        return pd.DataFrame(columns=['time','stock','bucket','side','price','qty','reason','pnl'])
    df = pd.read_csv(LOG_FILE)
    for c in ['bucket','pnl']:
        if c not in df.columns:
            df[c] = '' if c=='bucket' else None
    df['price'] = pd.to_numeric(df['price'], errors='coerce')
    df['qty']   = pd.to_numeric(df['qty'],   errors='coerce').fillna(0).astype(int)
    df['pnl']   = pd.to_numeric(df['pnl'],   errors='coerce')
    return df


@st.cache_data(ttl=1800)
def load_slow_fundamentals() -> dict:
    return load_fundamental_cache()


def format_dual_time(dt: datetime) -> str:
    local_dt = dt.astimezone()
    return (
        f"{dt.strftime('%Y-%m-%d %H:%M %Z')}"
        f" / {local_dt.strftime('%Y-%m-%d %H:%M %Z')}"
    )


def build_daily_fill_summary(fills: list[dict]) -> pd.DataFrame:
    cols = ['trade_date', 'trade_count', 'buy_count', 'sell_count', 'realized_pnl', 'commission']
    if not fills:
        return pd.DataFrame(columns=cols)

    df = pd.DataFrame(fills).copy()
    if 'time' not in df.columns:
        return pd.DataFrame(columns=cols)

    df['time'] = pd.to_datetime(df['time'], errors='coerce')
    df = df.dropna(subset=['time'])
    if df.empty:
        return pd.DataFrame(columns=cols)

    df['side'] = df['side'].astype(str).str.upper()
    df['commission'] = pd.to_numeric(df.get('commission'), errors='coerce').fillna(0.0)
    df['pnl'] = pd.to_numeric(df.get('pnl'), errors='coerce')
    df['trade_date'] = df['time'].dt.normalize()
    df['buy_count'] = (df['side'] == 'BUY').astype(int)
    df['sell_count'] = (df['side'] == 'SELL').astype(int)

    summary = (
        df.groupby('trade_date', as_index=False)
          .agg(
              trade_count=('side', 'size'),
              buy_count=('buy_count', 'sum'),
              sell_count=('sell_count', 'sum'),
              realized_pnl=('pnl', lambda s: float(s.dropna().sum()) if not s.dropna().empty else 0.0),
              commission=('commission', 'sum'),
          )
          .sort_values('trade_date')
          .reset_index(drop=True)
    )
    return summary


def render_daily_pnl_calendar(summary: pd.DataFrame, month_key: str) -> str:
    if summary.empty:
        return (
            f'<div class="pnl-calendar"><div class="pnl-calendar-meta">'
            f'暂无日度收益数据'
            f'</div></div>'
        )

    month_ts = pd.Timestamp(f'{month_key}-01')
    month_mask = summary['trade_date'].dt.to_period('M') == month_ts.to_period('M')
    month_df = summary.loc[month_mask].copy()
    month_map = {
        int(row.trade_date.day): row
        for row in month_df.itertuples(index=False)
    }
    scale = max(float(month_df['realized_pnl'].abs().max() or 0.0), 1.0)

    def cell_style(pnl: float | None) -> str:
        if pnl is None:
            return f'background:{BG};border-color:{BORDER};'
        alpha = min(0.35, max(0.08, abs(float(pnl)) / scale * 0.28))
        if pnl > 0:
            return f'background:rgba(34,197,94,{alpha:.3f});border-color:rgba(34,197,94,0.45);'
        if pnl < 0:
            return f'background:rgba(239,68,68,{alpha:.3f});border-color:rgba(239,68,68,0.45);'
        return f'background:rgba(148,163,184,0.12);border-color:{BORDER};'

    def pnl_text(pnl: float) -> str:
        return f"${pnl:+,.0f}" if abs(pnl) >= 1000 else f"${pnl:+,.2f}"

    week_headers = ''.join(
        f'<div class="pnl-calendar-head">{label}</div>'
        for label in ['一', '二', '三', '四', '五', '六', '日']
    )

    days_html: list[str] = []
    cal = pycal.Calendar(firstweekday=0)
    for week in cal.monthdayscalendar(month_ts.year, month_ts.month):
        for day in week:
            if day == 0:
                days_html.append(
                    '<div class="pnl-calendar-cell pnl-calendar-empty"></div>'
                )
                continue

            row = month_map.get(day)
            if row is None:
                meta = '<div class="pnl-calendar-meta">无成交</div>'
                pnl_block = '<div class="pnl-calendar-pnl flat">—</div>'
                style = cell_style(None)
            else:
                pnl = float(row.realized_pnl or 0.0)
                pnl_cls = 'up' if pnl > 0 else ('down' if pnl < 0 else 'flat')
                pnl_block = f'<div class="pnl-calendar-pnl {pnl_cls}">{pnl_text(pnl)}</div>'
                meta = (
                    f'<div class="pnl-calendar-meta">'
                    f'成交 {int(row.trade_count)} 笔<br>'
                    f'买 {int(row.buy_count)} / 卖 {int(row.sell_count)}'
                    f'</div>'
                )
                style = cell_style(pnl)

            days_html.append(
                f'<div class="pnl-calendar-cell" style="{style}">'
                f'<div class="pnl-calendar-day">{day}</div>'
                f'{pnl_block}'
                f'{meta}'
                f'</div>'
            )

    return (
        f'<div class="pnl-calendar">'
        f'<div class="pnl-calendar-grid">{week_headers}{"".join(days_html)}</div>'
        f'</div>'
    )


def next_weekly_window(now_et: datetime, is_complete: bool) -> tuple[datetime | None, bool]:
    days_ahead = (WEEKLY_DCA_WEEKDAY_ET - now_et.weekday()) % 7
    candidate = (now_et + timedelta(days=days_ahead)).replace(
        hour=WEEKLY_DCA_MIN_HOUR_ET,
        minute=0,
        second=0,
        microsecond=0,
    )

    due_now = (
        now_et.weekday() == WEEKLY_DCA_WEEKDAY_ET
        and now_et >= candidate
        and not is_complete
    )
    if due_now:
        return None, True

    if days_ahead == 0 and now_et >= candidate:
        candidate += timedelta(days=7)
    return candidate, False


def weekly_dca_status(account: dict, trades: pd.DataFrame) -> dict:
    plan_codes = list(WEEKLY_DCA_PLAN.keys())
    markers = account.get('meta', {}).get('markers', {})
    now_et = current_new_york_time()
    this_week = week_marker(now_et)

    done_codes = [
        code for code in plan_codes
        if markers.get(f'weekly_dca:{code}', '') == this_week
    ]
    done_count = len(done_codes)
    total_count = len(plan_codes)
    is_complete = total_count > 0 and done_count == total_count
    next_run_et, due_now = next_weekly_window(now_et, is_complete)

    dca_trades = trades[
        trades['reason'].eq('weekly_dca')
        & trades['side'].eq('BUY')
        & trades['stock'].isin(plan_codes)
    ].copy()
    if not dca_trades.empty:
        dca_trades['time_dt'] = pd.to_datetime(dca_trades['time'], errors='coerce')

    last_price_parts = []
    for code in plan_codes:
        code_trades = dca_trades[dca_trades['stock'] == code] if not dca_trades.empty else pd.DataFrame()
        if code_trades.empty:
            last_price_parts.append(f"{code.replace('US.', '')} —")
            continue
        latest = code_trades.sort_values('time_dt').iloc[-1]
        last_price_parts.append(f"{code.replace('US.', '')} ${float(latest['price']):.2f}")

    if total_count == 0:
        status_text = '未配置'
        status_class = 'flat'
        next_run_text = '—'
    elif is_complete:
        status_text = f"已完成 {done_count}/{total_count}"
        status_class = 'up'
        next_run_text = format_dual_time(cast(datetime, next_run_et)) if next_run_et else '—'
    elif due_now:
        status_text = f"待执行 {done_count}/{total_count}"
        status_class = 'warn'
        next_run_text = '本周窗口已打开，当前可执行'
    else:
        status_text = f"未完成 {done_count}/{total_count}"
        status_class = 'warn'
        next_run_text = format_dual_time(cast(datetime, next_run_et)) if next_run_et else '—'

    return {
        'status_text': status_text,
        'status_class': status_class,
        'next_run_text': next_run_text,
        'last_price_text': ' / '.join(last_price_parts) if last_price_parts else '—',
        'done_codes_text': ' / '.join(code.replace('US.', '') for code in done_codes) if done_codes else '本周尚未成交',
    }

# ── 技术指标 ─────────────────────────────────────────────────

def calc_rsi(s, n=14):
    d=s.diff(); g=d.clip(lower=0).rolling(n).mean(); l=(-d.clip(upper=0)).rolling(n).mean()
    v=100-100/(1+g/l.replace(0,float('nan'))); return float(v.iloc[-1]) if len(v) else float('nan')

def calc_macd_h(s):
    m=s.ewm(span=12,adjust=False).mean()-s.ewm(span=26,adjust=False).mean()
    return float((m-m.ewm(span=9,adjust=False).mean()).iloc[-1])

def mom20(s):
    return float((s.iloc[-1]-s.iloc[-21])/s.iloc[-21]) if len(s)>=21 else 0.0

# ── 颜色 helpers ─────────────────────────────────────────────

def _n(val):
    try: return float(str(val).replace('$','').replace('+','').replace('%','').replace(',',''))
    except: return None

def color_pnl(val):
    n = _n(val)
    if n is None: return ''
    if n > 0: return 'color:#22c55e;font-weight:600'
    if n < 0: return 'color:#ef4444;font-weight:600'
    return 'color:#6b7280'

def color_score(val):
    n = _n(val)
    if n is None: return ''
    if n >= 7.5: return 'color:#22c55e;font-weight:700'
    if n >= 6.0: return 'color:#fbbf24;font-weight:600'
    if n <  4.5: return 'color:#ef4444'
    return ''

def color_rsi(val):
    n = _n(val)
    if n is None: return ''
    if n >= 70: return 'color:#ef4444;font-weight:600'
    if n <= 30: return 'color:#22c55e;font-weight:600'
    return ''

def color_warn(val):
    """亏损接近止损线 → 橙色警告"""
    n = _n(val)
    if n is None: return ''
    if n <= -3: return 'color:#f59e0b;font-weight:600'
    return color_pnl(val)

def color_status(val):
    text = str(val)
    if '已成交' in text:
        return 'color:#22c55e;font-weight:600'
    if '部分成交' in text:
        return 'color:#f59e0b;font-weight:600'
    if '待成交' in text:
        return 'color:#3b82f6;font-weight:600'
    if '已撤销' in text:
        return 'color:#64748b;font-weight:600'
    if '拒单' in text:
        return 'color:#ef4444;font-weight:600'
    return ''


def format_order_message(msg: object) -> str:
    text = str(msg or '').strip()
    if not text:
        return '—'
    if text == 'timeout_no_quote':
        return '无行情超时撤单'
    if text == 'accepted':
        return '已接受，等待成交'
    if text.startswith('accepted_clipped:'):
        payload = text.split(':', 1)[1]
        return f'可卖数量不足，已按 {payload} 接受'
    if text.startswith('auto_canceled_remainder:'):
        payload = text.split(':', 1)[1]
        return f'部分成交后撤余单 {payload}'
    if text.startswith('partially_filled:'):
        payload = text.split(':', 1)[1]
        return f'部分成交 {payload}'
    return text


def format_order_reserved(row: pd.Series) -> str:
    status = str(row.get('status', '') or '')
    side = str(row.get('side', '') or '')
    if any(token in status for token in ('已成交', '已撤销', '拒单')):
        return '已释放'
    if side.startswith('🟢'):
        reserved_cash = float(row.get('reserved_cash', 0.0) or 0.0)
        return f"预留 ${reserved_cash:,.2f}" if reserved_cash > 0 else '—'
    reserved_qty = int(row.get('reserved_qty', 0) or 0)
    return f"锁定 {reserved_qty} 股" if reserved_qty > 0 else '—'

def s(n): return '+' if n >= 0 else ''

# ── 主数据 ───────────────────────────────────────────────────

account   = load_account()
trades    = load_trades()
prices    = load_prices(tuple(ALL_STOCKS))
klines    = load_klines(tuple(ALL_STOCKS))
slow_fund_cache = load_slow_fundamentals()
positions = account.get('positions', {})
orders = list(account.get('orders', []))
fills = list(account.get('fills', []))
dailies = build_daily_fill_summary(fills)
dca_status = weekly_dca_status(account, trades)
reserved_cash = float(account.get('reserved_cash', 0.0) or 0.0)
available_cash = max(0.0, float(account.get('cash', 0.0) or 0.0) - reserved_cash)
open_orders = [
    o for o in orders
    if str(o.get('status', '') or '') in {'NEW', 'PARTIALLY_FILLED'}
]
open_buy_orders = sum(1 for o in open_orders if str(o.get('side', '') or '').upper() == 'BUY')
open_sell_orders = sum(1 for o in open_orders if str(o.get('side', '') or '').upper() == 'SELL')
terminal_orders = [
    o for o in orders
    if str(o.get('status', '') or '') in {'CANCELED', 'REJECTED'}
]
timeout_cancel_count = sum(
    1 for o in terminal_orders
    if str(o.get('status', '') or '') == 'CANCELED'
    and str(o.get('message', '') or '') == 'timeout_no_quote'
)
latest_terminal_text = '—'
if terminal_orders:
    latest_terminal_order = max(
        terminal_orders,
        key=lambda o: str(o.get('updated_at', '') or ''),
    )
    latest_terminal_text = (
        f"{str(latest_terminal_order.get('code', '') or '').replace('US.', '')} "
        f"{format_order_message(latest_terminal_order.get('message', ''))}"
    )
reserved_qty_total = sum(int(p.get('reserved_qty', 0) or 0) for p in positions.values())
fill_pnl_by_order: dict[str, float] = {}
if fills:
    _fill_pnl_df = pd.DataFrame(fills).copy()
    if 'order_id' in _fill_pnl_df.columns:
        _fill_pnl_df['pnl'] = pd.to_numeric(_fill_pnl_df.get('pnl'), errors='coerce')
        fill_pnl_by_order = (
            _fill_pnl_df.groupby('order_id')['pnl']
            .sum(min_count=1)
            .dropna()
            .to_dict()
        )

mkt_val = unrealized = today_pnl = 0.0
pos_rows = []

for code, p in positions.items():
    px   = prices.get(code, {})
    # 用 or 链：live/last 为 0 时自动降级，不用 dict.get 默认值（0 不触发默认值）
    cur  = px.get('live') or px.get('last') or p['avg_cost']
    prev = px.get('prev') or p['avg_cost']
    entry_str = p.get('entry_time','')
    try:
        entry_dt = datetime.strptime(entry_str, '%Y-%m-%d %H:%M:%S')
        is_new_today = entry_dt.date() == datetime.now().date()
    except Exception:
        entry_dt = None
        is_new_today = False

    # 今日新开的仓位，不应把买入前的日内涨跌算进"今日盈亏"
    day_ref = p['avg_cost'] if is_new_today else prev
    val  = cur * p['qty']
    unr  = (cur - p['avg_cost']) * p['qty']
    t_pl = (cur - day_ref) * p['qty']
    t_pc = (cur - day_ref) / day_ref * 100 if day_ref else 0
    pct  = (cur - p['avg_cost']) / p['avg_cost'] * 100
    try:
        days_held = (datetime.now() - entry_dt).days if entry_dt else 0
    except Exception:
        days_held = 0

    mkt_val    += val
    unrealized += unr
    today_pnl  += t_pl

    pos_rows.append({
        '_code':    code,
        '_bucket':  p.get('bucket',''),
        '_sector':  SECTOR_MAP.get(code,'其他'),
        '_unr':     unr,
        '_t_pl':    t_pl,
        '_pct':     pct,
        '_t_pc':    t_pc,
        '_days':    days_held,
        '_horizon': '长期(>30d)' if days_held>30 else ('中期(7-30d)' if days_held>=7 else '短期(<7d)'),
        '_status':  ('⚠️ 接近止损' if pct <= -3 else ('🟢 盈利' if pct > 0 else '🔴 亏损')),
        '股票':     code.replace('US.',''),
        '板块':     SECTOR_MAP.get(code,'其他'),
        '策略':     BUCKET_LABEL.get(p.get('bucket',''),''),
        '数量':     p['qty'],
        '可卖数量':  max(0, int(p.get('qty', 0) or 0) - int(p.get('reserved_qty', 0) or 0)),
        '锁定数量':  int(p.get('reserved_qty', 0) or 0),
        '成本价':   p['avg_cost'],
        '现价':     cur,
        '市值':     val,
        '未实现盈亏': unr,
        '收益率%':   pct,
        '今日盈亏':  t_pl,
        '今日%':     t_pc,
        '持仓天数':  days_held,
        '加码次数':  p.get('add_count',0),
    })

total = account['cash'] + mkt_val
init  = account['initial_cash']
real  = account['realized_pnl']
comm  = account['total_commission']
t_ret = (total - init) / init * 100
t_pct = today_pnl / (total - today_pnl) * 100 if total != today_pnl else 0
trade_count = len(fills) if fills else len(trades)
buy_count = int(trades['side'].eq('BUY').sum()) if 'side' in trades.columns else 0
sell_count = int(trades['side'].eq('SELL').sum()) if 'side' in trades.columns else 0
today_key = pd.Timestamp.now().normalize()
today_daily = dailies.loc[dailies['trade_date'] == today_key]
today_realized = float(today_daily['realized_pnl'].iloc[0]) if not today_daily.empty else 0.0
today_fill_count = int(today_daily['trade_count'].iloc[0]) if not today_daily.empty else 0
today_buy_fills = int(today_daily['buy_count'].iloc[0]) if not today_daily.empty else 0
today_sell_fills = int(today_daily['sell_count'].iloc[0]) if not today_daily.empty else 0
today_realized_cls = 'up' if today_realized > 0 else ('down' if today_realized < 0 else 'flat')

# ══════════════════════════════════════════════════════════
# 顶部英雄区域
# ══════════════════════════════════════════════════════════

up_c = 'up' if today_pnl >= 0 else 'down'
tr_c = 'up' if t_ret >= 0 else 'down'

st.markdown(f"""
<div class="hero">
  <div class="hero-tag">总资产 USD</div>
  <div class="hero-value">${total:,.2f}</div>
  <div class="hero-row">
    <div class="hero-item">
      <span class="hero-num {up_c}">{s(today_pnl)}{today_pnl:,.2f}&ensp;({s(t_pct)}{t_pct:.2f}%)</span>
      <span class="hero-lbl">今日盈亏</span>
    </div>
    <div class="hero-item">
      <span class="hero-num {tr_c}">{s(t_ret)}{t_ret:.3f}%</span>
      <span class="hero-lbl">总收益率</span>
    </div>
    <div class="hero-item">
      <span class="hero-num {'up' if unrealized>=0 else 'down'}">{s(unrealized)}{unrealized:,.2f}</span>
      <span class="hero-lbl">未实现盈亏</span>
    </div>
    <div class="hero-item">
      <span class="hero-num {'up' if real>=0 else 'down'}">{s(real)}{real:,.2f}</span>
      <span class="hero-lbl">已实现盈亏</span>
    </div>
  </div>
</div>
<div class="kpi-grid">
  <div class="kpi">
    <div class="kpi-label">现金余额</div>
    <div class="kpi-value">${account['cash']:,.0f}</div>
    <div class="kpi-sub">可用 ${available_cash:,.0f} · {account['cash']/total*100:.1f}% 占比</div>
    <div class="kpi-subline">预留 ${reserved_cash:,.2f}</div>
  </div>
  <div class="kpi">
    <div class="kpi-label">持仓市值</div>
    <div class="kpi-value">${mkt_val:,.0f}</div>
    <div class="kpi-sub">{len(positions)} 只 · {mkt_val/total*100:.1f}%</div>
    <div class="kpi-subline">锁定卖出 {reserved_qty_total} 股</div>
  </div>
  <div class="kpi">
    <div class="kpi-label">今日最佳</div>
    <div class="kpi-value up">{max((r['今日%'] for r in pos_rows), default=0):+.2f}%</div>
    <div class="kpi-sub">{max(pos_rows, key=lambda r:r['今日%'], default={'股票':'—'})['股票']}</div>
  </div>
  <div class="kpi">
    <div class="kpi-label">当日已实现收益</div>
    <div class="kpi-value {today_realized_cls}">${today_realized:+,.2f}</div>
    <div class="kpi-sub">成交 {today_fill_count} 笔 · 买 {today_buy_fills} / 卖 {today_sell_fills}</div>
    <div class="kpi-subline">仅统计已成交流水中的当日已实现结果</div>
  </div>
  <div class="kpi">
    <div class="kpi-label">20% 储备线</div>
    <div class="kpi-value">${init*0.2:,.0f}</div>
    <div class="kpi-sub">可追加 ${max(0,account['cash']-init*0.2):,.0f}</div>
  </div>
  <div class="kpi">
    <div class="kpi-label">累计手续费</div>
    <div class="kpi-value">${comm:.2f}</div>
    <div class="kpi-sub">{pd.Timestamp.now().strftime('%H:%M:%S')} 刷新</div>
  </div>
  <div class="kpi">
    <div class="kpi-label">待处理订单</div>
    <div class="kpi-value">{len(open_orders)}</div>
    <div class="kpi-sub">买单 {open_buy_orders} · 卖单 {open_sell_orders}</div>
    <div class="kpi-subline">订单账本 {len(orders)} 笔</div>
    <div class="kpi-subline">超时撤单 {timeout_cancel_count} 笔</div>
    <div class="kpi-subline">最近撤单：{latest_terminal_text}</div>
  </div>
  <div class="kpi">
    <div class="kpi-label">本周定投状态</div>
    <div class="kpi-value {dca_status['status_class']}">{dca_status['status_text']}</div>
    <div class="kpi-subline">下次执行：{dca_status['next_run_text']}</div>
    <div class="kpi-subline">已完成：{dca_status['done_codes_text']}</div>
    <div class="kpi-subline">上次成交价：{dca_status['last_price_text']}</div>
  </div>
  <div class="kpi">
    <div class="kpi-label">交易次数</div>
    <div class="kpi-value">{trade_count}</div>
    <div class="kpi-sub">买入 {buy_count} · 卖出 {sell_count}</div>
  </div>
</div>
""", unsafe_allow_html=True)

# ══════════════════════════════════════════════════════════
# Tabs
# ══════════════════════════════════════════════════════════
t1, t2, t3, t4, t5 = st.tabs(["💼 持仓", "🔍 自选股雷达", "📈 盈亏曲线", "📋 交易记录", "📊 绩效分析"])

PNL_COLS  = ['未实现盈亏','收益率%','今日盈亏','今日%']
BASE_COLS = ['股票','板块','策略','数量','可卖数量','锁定数量','成本价','现价','市值',
             '未实现盈亏','收益率%','今日盈亏','今日%','持仓天数']
HOLDING_SORT_OPTIONS = {
    '默认顺序': None,
    '总市值': '市值',
    '单股股价': '现价',
    '成本价': '成本价',
    '持仓数量': '数量',
    '未实现盈亏': '未实现盈亏',
    '收益率': '收益率%',
    '今日盈亏': '今日盈亏',
    '今日涨跌幅': '今日%',
    '持仓天数': '持仓天数',
    '股票代码': '股票',
    '板块': '板块',
    '策略': '策略',
}
FMT = {
    '成本价':   '${:.2f}', '现价':    '${:.2f}', '市值':   '${:,.0f}',
    '未实现盈亏':'${:+,.2f}','收益率%':'{:+.2f}%',
    '今日盈亏': '${:+,.2f}','今日%':  '{:+.2f}%',
}

def fmt_val(col, val):
    """格式化单元格数值。"""
    if val is None or (isinstance(val, float) and val != val):
        return '—'
    f = FMT.get(col)
    if f is None:
        return str(val)
    try:
        return f.format(val) if isinstance(f, str) else f(val)
    except Exception:
        return str(val)

def cell_color(col, val):
    """返回单元格文字颜色 CSS。"""
    PNL = {'未实现盈亏','今日盈亏','今日%','收益率%'}
    if col not in PNL:
        return ''
    try:
        n = float(str(val).replace('$','').replace('+','').replace('%','').replace(',',''))
        if col == '收益率%' and n <= -3:
            return 'color:#f59e0b;font-weight:600'  # 橙色警告
        if n > 0: return 'color:#22c55e;font-weight:600'
        if n < 0: return 'color:#ef4444;font-weight:600'
    except Exception:
        pass
    return ''

def sort_rows(rows, sort_col=None, descending=True):
    if not rows or not sort_col:
        return list(rows)

    def _sort_key(row):
        val = row.get(sort_col)
        if val is None:
            return (1, 0)
        if isinstance(val, str):
            return (0, val.lower())
        return (0, val)

    return sorted(rows, key=_sort_key, reverse=descending)


def make_table(rows, extra_cols=None, sort_col=None, descending=True):
    """生成完全受主题控制的 HTML 表格（不依赖 st.dataframe）。"""
    rows = sort_rows(rows, sort_col=sort_col, descending=descending)
    all_cols  = list(dict.fromkeys(BASE_COLS + (extra_cols or [])))
    avail     = [c for c in all_cols if any(c in r for r in rows)]

    # 主题色
    hdr_bg  = '#1e293b' if DARK else '#f1f5f9'
    hdr_fg  = '#94a3b8' if DARK else '#64748b'
    row_bg  = '#0d1117' if DARK else '#ffffff'
    row_alt = '#13131a' if DARK else '#f8fafc'
    border  = '#1e293b' if DARK else '#e2e8f0'
    fg      = '#e2e8f0' if DARK else '#0f172a'

    th = (f'padding:9px 14px;text-align:left;font-size:.68rem;font-weight:600;'
          f'text-transform:uppercase;letter-spacing:.07em;'
          f'color:{hdr_fg};background:{hdr_bg};border-bottom:2px solid {border}')

    html = (f'<div style="overflow-x:auto;border-radius:10px;'
            f'border:1px solid {border};margin-bottom:16px">'
            f'<table style="width:100%;border-collapse:collapse;'
            f'font-size:.84rem;color:{fg}">')
    html += '<thead><tr>' + ''.join(f'<th style="{th}">{c}</th>' for c in avail) + '</tr></thead>'
    html += '<tbody>'
    for i, row in enumerate(rows):
        bg = row_bg if i % 2 == 0 else row_alt
        html += f'<tr style="background:{bg}">'
        for c in avail:
            val = row.get(c, '')
            txt = fmt_val(c, val)
            cc  = cell_color(c, val)
            td  = f'padding:9px 14px;border-bottom:1px solid {border};{cc}'
            html += f'<td style="{td}">{txt}</td>'
        html += '</tr>'
    html += '</tbody></table></div>'
    st.markdown(html, unsafe_allow_html=True)

def summary_bar(rows, group_key, val_key='_unr'):
    """在表格上方显示分组汇总徽章"""
    groups = {}
    for r in rows:
        g = r[group_key]
        groups[g] = groups.get(g, 0) + r[val_key]
    if not groups:
        return

    parts = ['<div class="badge-row">']
    for g, v in sorted(groups.items()):
        c = 'up' if v > 0 else ('down' if v < 0 else 'flat')
        label = escape(str(g))
        parts.append(
            f'<div class="badge"><span class="{c}">{label} {s(v)}${abs(v):,.0f}</span></div>'
        )
    parts.append('</div>')
    st.markdown(''.join(parts), unsafe_allow_html=True)

# ── Tab 1：持仓（多视角）────────────────────────────────────
with t1:
    if not pos_rows:
        st.info("当前空仓，策略运行中等待信号…")
    else:
        sort_c1, sort_c2 = st.columns([2, 1])
        with sort_c1:
            holding_sort_label = st.selectbox(
                "排序字段",
                list(HOLDING_SORT_OPTIONS.keys()),
                index=0,
                key="holding_sort_label",
            )
        with sort_c2:
            holding_sort_desc = st.selectbox(
                "排序方向",
                ["从高到低", "从低到高"],
                index=0,
                key="holding_sort_direction",
            )

        holding_sort_col = HOLDING_SORT_OPTIONS[holding_sort_label]
        holding_sort_desc_flag = holding_sort_desc == "从高到低"
        views = st.tabs(["🗂 全览", "📂 按策略桶", "🏭 按行业板块", "⏱ 按持仓时间", "📊 按表现"])

        # ── 视角0：全览（默认第一个）──────────────────────
        with views[0]:
            make_table(
                pos_rows,
                sort_col=holding_sort_col,
                descending=holding_sort_desc_flag,
            )

        # ── 视角1：策略桶 ──────────────────────────────────
        with views[1]:
            summary_bar(pos_rows, '策略')
            st.markdown("")
            for bk in BUCKET_ORDER:
                rows = [r for r in pos_rows if r['_bucket'] == bk]
                if not rows: continue
                bk_unr = sum(r['_unr'] for r in rows)
                bk_t = sum(r['_t_pl'] for r in rows)
                c = 'up' if bk_unr >= 0 else 'down'
                st.markdown(f"""
                <div class="group-header">
                  <span class="group-title">{BUCKET_LABEL[bk]}</span>
                  <span class="group-meta">{len(rows)} 只</span>
                  <span class="group-meta {c}">未实现 {s(bk_unr)}${abs(bk_unr):,.0f}</span>
                  <span class="group-meta {'up' if bk_t>=0 else 'down'}">今日 {s(bk_t)}${abs(bk_t):,.0f}</span>
                </div>""", unsafe_allow_html=True)
                make_table(
                    rows,
                    sort_col=holding_sort_col,
                    descending=holding_sort_desc_flag,
                )

        # ── 视角2：行业板块 ────────────────────────────────
        with views[2]:
            summary_bar(pos_rows, '板块')
            st.markdown("")
            held_sectors = sorted({r['_sector'] for r in pos_rows},
                                   key=lambda x: SECTOR_ORDER.index(x) if x in SECTOR_ORDER else 99)
            for sec in held_sectors:
                rows = [r for r in pos_rows if r['_sector'] == sec]
                sec_unr = sum(r['_unr'] for r in rows)
                c = 'up' if sec_unr >= 0 else 'down'
                st.markdown(f"""
                <div class="group-header">
                  <span class="group-title">{sec}</span>
                  <span class="group-meta">{len(rows)} 只</span>
                  <span class="group-meta {c}">未实现 {s(sec_unr)}${abs(sec_unr):,.0f}</span>
                </div>""", unsafe_allow_html=True)
                make_table(
                    rows,
                    sort_col=holding_sort_col,
                    descending=holding_sort_desc_flag,
                )

        # ── 视角3：持仓时间 ────────────────────────────────
        with views[3]:
            horizon_order = ['短期(<7d)','中期(7-30d)','长期(>30d)']
            for hz in horizon_order:
                rows = [r for r in pos_rows if r['_horizon'] == hz]
                if not rows: continue
                hz_unr = sum(r['_unr'] for r in rows)
                c = 'up' if hz_unr >= 0 else 'down'
                st.markdown(f"""
                <div class="group-header">
                  <span class="group-title">{hz}</span>
                  <span class="group-meta">{len(rows)} 只</span>
                  <span class="group-meta {c}">未实现 {s(hz_unr)}${abs(hz_unr):,.0f}</span>
                </div>""", unsafe_allow_html=True)
                make_table(
                    rows,
                    ['持仓天数','加码次数'],
                    sort_col=holding_sort_col,
                    descending=holding_sort_desc_flag,
                )

        # ── 视角4：按表现 ──────────────────────────────────
        with views[4]:
            status_order = ['⚠️ 接近止损','🔴 亏损','🟢 盈利']
            for st_key in status_order:
                rows = [r for r in pos_rows if r['_status'] == st_key]
                if not rows: continue
                st_unr = sum(r['_unr'] for r in rows)
                c = 'warn' if '止损' in st_key else ('up' if st_unr >= 0 else 'down')
                st.markdown(f"""
                <div class="group-header">
                  <span class="group-title">{st_key}</span>
                  <span class="group-meta">{len(rows)} 只</span>
                  <span class="group-meta {c}">{s(st_unr)}${abs(st_unr):,.0f}</span>
                </div>""", unsafe_allow_html=True)
                make_table(
                    rows,
                    sort_col=holding_sort_col,
                    descending=holding_sort_desc_flag,
                )

# ── Tab 2：自选股雷达 ─────────────────────────────────────────
with t2:
    held = {c.replace('US.','') for c in positions}
    radar = []

    for stock in ALL_STOCKS:
        px   = prices.get(stock, {})
        last = px.get('last', 0)
        if not last: continue

        prev  = px.get('prev', last)
        h52   = px.get('high52', last)
        l52   = px.get('low52',  last)
        d_chg = (last-prev)/prev*100 if prev else 0

        snap = pd.Series({'pe_ttm_ratio':px.get('pe',0),'earning_per_share':px.get('eps',0),
                           'pb_ratio':0,'total_market_val':px.get('mkt_cap',0)})
        fund_sc, notes = fundamental_score(snap)
        slow_eval = score_slow_fundamentals(
            stock,
            SECTOR_MAP.get(stock, ''),
            slow_fund_cache.get(stock),
            snapshot=snap,
        )
        slow_sc = slow_eval['score'] if slow_eval['available'] else None
        fund_score_display = round((slow_sc / 10.0), 1) if slow_sc is not None else round(fund_sc, 1)

        df_k   = klines.get(stock)
        mom_v  = mom20(df_k['close'])*100   if df_k is not None else 0
        rsi_v  = calc_rsi(df_k['close'])    if df_k is not None else float('nan')
        macd_v = calc_macd_h(df_k['close']) if df_k is not None else float('nan')
        _, vn  = volume_signal(df_k) if df_k is not None else (None,'')

        w52    = (last-l52)/(h52-l52)*100 if h52>l52 else 50
        pos_sc = max(0, 10-abs(w52-50)*0.15)
        mom_sc = max(0, min(10, 5+mom_v*0.3))
        score  = round(fund_score_display*0.40 + mom_sc*0.35 + pos_sc*0.25, 1)

        sig = '—'
        if rsi_v == rsi_v:
            if rsi_v < 35 and mom_v < -5:   sig = '⚡ 超卖'
            elif rsi_v > 70:                 sig = '🔴 超买'
            elif mom_v > 10 and score > 7:   sig = '🟢 强势'
            elif macd_v == macd_v and macd_v > 0 and score > 6: sig = '📈 看多'

        radar.append({
            '':        '●' if stock.replace('US.','') in held else '',
            '股票':    stock.replace('US.',''),
            '板块':    SECTOR_MAP.get(stock,''),
            '现价':    last,
            '今日%':   d_chg,
            '盘前%':   px.get('pre_chg',0) if px.get('pre_p') else None,
            '评分':    score,
            '基本面':  fund_score_display,
            '慢分':    round(slow_sc,1) if slow_sc is not None else None,
            '动量20d': mom_v,
            'RSI':     rsi_v if rsi_v==rsi_v else None,
            '信号':    sig,
        })

    if not radar:
        st.warning("雷达页暂无可用行情数据。请检查 OpenD 连接或稍后刷新。")
    else:
        rdf = pd.DataFrame(radar).sort_values('评分', ascending=False)
        pre_df = rdf[rdf['盘前%'].notna()].copy()
        if not pre_df.empty:
            st.subheader("盘前异动榜")
            pre_rank = pre_df.loc[pre_df['盘前%'].abs() >= 1.0].copy()
            if not pre_rank.empty:
                c_up, c_dn = st.columns(2)
                with c_up:
                    st.caption("盘前上涨 Top 8")
                    pre_up = pre_rank.sort_values('盘前%', ascending=False).head(8)
                    st.dataframe(
                        pre_up[['股票', '板块', '现价', '盘前%', '评分', '信号']]
                            .style
                            .format({'现价': '${:.2f}', '盘前%': '{:+.2f}%'})
                            .map(color_pnl, subset=['盘前%'])
                            .map(color_score, subset=['评分']),
                        width="stretch",
                        hide_index=True,
                    )
                with c_dn:
                    st.caption("盘前下跌 Top 8")
                    pre_dn = pre_rank.sort_values('盘前%').head(8)
                    st.dataframe(
                        pre_dn[['股票', '板块', '现价', '盘前%', '评分', '信号']]
                            .style
                            .format({'现价': '${:.2f}', '盘前%': '{:+.2f}%'})
                            .map(color_pnl, subset=['盘前%'])
                            .map(color_score, subset=['评分']),
                        width="stretch",
                        hide_index=True,
                    )
                st.caption("策略已接入盘前异动信号：默认阈值为盘前涨幅 ≥ 2% 且盘前量 ≥ 5万股。")

        styled = (rdf.style
                  .format({'现价':'${:.2f}',
                           '今日%':   lambda x: f'{x:+.2f}%' if pd.notna(x) else '—',
                           '盘前%':   lambda x: f'{x:+.2f}%' if pd.notna(x) else '—',
                           '慢分':    lambda x: f'{x:.0f}' if pd.notna(x) else '—',
                           '动量20d': '{:+.1f}%',
                           'RSI':     lambda x: f'{x:.0f}'   if pd.notna(x) else '—'})
                  .map(color_pnl,   subset=['今日%','盘前%','动量20d'])
                  .map(color_score, subset=['评分'])
                  .map(color_rsi,   subset=['RSI']))
        st.dataframe(styled, width="stretch", hide_index=True)
        st.caption("● 已持仓  |  评分 🟢≥7.5 🟡6-7.5 🔴<4.5  |  RSI 🟢≤30超卖 🔴≥70超买")

# ── Tab 3：盈亏曲线 ──────────────────────────────────────────
with t3:
    sells = trades[trades['side']=='SELL'].dropna(subset=['pnl']).copy()
    if sells.empty:
        st.info("暂无已实现交易")
    else:
        sells['time'] = pd.to_datetime(sells['time'])
        sells = sells.sort_values('time')
        sells['累计盈亏'] = sells['pnl'].cumsum()
        c1, c2 = st.columns([3,1])
        with c1:
            st.subheader("累计已实现盈亏")
            st.line_chart(sells.set_index('time')[['累计盈亏']])
        with c2:
            st.subheader("各桶")
            bp = sells.groupby('bucket')['pnl'].sum().reset_index()
            bp['bucket'] = bp['bucket'].map(BUCKET_LABEL).fillna(bp['bucket'])
            bp.columns = ['策略','盈亏']
            st.dataframe(bp.style.format({'盈亏':'${:+,.2f}'})
                           .map(color_pnl, subset=['盈亏']),
                         width="stretch", hide_index=True)
            if pos_rows:
                st.subheader("持仓盈亏")
                ur = pd.DataFrame([{'股票':r['股票'],'未实现':r['_unr']} for r in pos_rows])
                st.dataframe(ur.style.format({'未实现':'${:+,.2f}'})
                               .map(color_pnl, subset=['未实现']),
                             width="stretch", hide_index=True)

    st.divider()
    st.subheader("每日收益日历")
    if dailies.empty:
        st.info("暂无按日汇总的成交流水。")
    else:
        month_options = (
            dailies['trade_date']
            .dt.strftime('%Y-%m')
            .drop_duplicates()
            .sort_values(ascending=False)
            .tolist()
        )
        selected_month = st.selectbox(
            "选择月份",
            month_options,
            index=0,
            key="daily_pnl_calendar_month",
        )
        st.markdown(render_daily_pnl_calendar(dailies, selected_month), unsafe_allow_html=True)
        st.caption("当前日历按已实现收益统计：绿色为盈利日，红色为亏损日；无持仓市值快照的历史日期暂不回放总资产日收益。")

        daily_show = dailies.copy().sort_values('trade_date', ascending=False)
        daily_show['日期'] = daily_show['trade_date'].dt.strftime('%Y-%m-%d')
        daily_show = daily_show.rename(columns={
            'trade_count': '成交笔数',
            'buy_count': '买入',
            'sell_count': '卖出',
            'realized_pnl': '已实现收益',
            'commission': '手续费',
        })
        st.dataframe(
            daily_show[['日期', '成交笔数', '买入', '卖出', '已实现收益', '手续费']]
                .style
                .format({'已实现收益': '${:+,.2f}', '手续费': '${:,.2f}'})
                .map(color_pnl, subset=['已实现收益']),
            width="stretch",
            hide_index=True,
        )

# ── Tab 4：交易记录 ──────────────────────────────────────────
with t4:
    order_status_map = {
        'NEW': '🟦 待成交',
        'PARTIALLY_FILLED': '🟨 部分成交',
        'FILLED': '🟢 已成交',
        'CANCELED': '⚪ 已撤销',
        'REJECTED': '🔴 拒单',
    }
    q1, q2 = st.columns(2)
    q1.metric("待处理订单", f"{len(open_orders)} 笔", f"买 {open_buy_orders} / 卖 {open_sell_orders}")
    q2.metric("可用现金", f"${available_cash:,.2f}", f"预留 ${reserved_cash:,.2f}")

    order_tab, fill_tab, trade_tab = st.tabs(["📨 订单状态", "✅ 成交流水", "📋 交易日志"])

    with order_tab:
        if not orders:
            st.info("暂无订单记录")
        else:
            odf = pd.DataFrame(orders).iloc[::-1].reset_index(drop=True)
            odf['side'] = odf['side'].map({'BUY':'🟢 买入','SELL':'🔴 卖出'}).fillna(odf['side'])
            odf['status'] = odf['status'].map(order_status_map).fillna(odf['status'])
            odf['stock'] = odf['code'].str.replace('US.', '', regex=False)
            odf['message'] = odf['message'].map(format_order_message)
            odf['reserved'] = odf.apply(format_order_reserved, axis=1)
            odf['avg_fill_price'] = odf['avg_fill_price'].map(
                lambda x: f"${float(x):.2f}" if pd.notna(x) and x not in ('', None) else '-'
            )
            odf['requested_price'] = odf['requested_price'].map(lambda x: f"${float(x):.2f}")
            odf['commission'] = odf['commission'].map(lambda x: f"${float(x):.2f}")

            # 盈亏列：仅卖出且已成交时显示，买入或未成交显示 '—'
            def _fmt_order_pnl(row):
                raw_side = str(row.get('side', '') or '')
                is_sell = '卖出' in raw_side or raw_side.upper() == 'SELL'
                pnl = fill_pnl_by_order.get(str(row.get('order_id', '') or ''))
                if not is_sell or pnl is None:
                    return '—'
                try:
                    v = float(pnl)
                    return f"+${v:,.2f}" if v >= 0 else f"-${abs(v):,.2f}"
                except (ValueError, TypeError):
                    return '—'

            odf['盈亏'] = odf.apply(_fmt_order_pnl, axis=1)

            odf_show = odf.rename(columns={
                'submitted_at':'提交时间',
                'order_id':'订单号',
                'stock':'股票',
                'side':'方向',
                'status':'状态',
                'requested_qty':'申请数量',
                'filled_qty':'已成交',
                'remaining_qty':'剩余',
                'requested_price':'委托价',
                'avg_fill_price':'成交均价',
                'commission':'手续费',
                'reason':'原因',
                'message':'备注',
                'reserved':'锁定资源',
            })

            def color_pnl(val):
                if val == '—':
                    return ''
                return 'color:#22c55e' if val.startswith('+') else 'color:#ef4444'

            st.dataframe(
                odf_show[['提交时间','订单号','股票','方向','状态','申请数量','已成交','剩余','委托价','成交均价','锁定资源','手续费','原因','盈亏','备注']]
                    .style
                    .map(color_status, subset=['状态'])
                    .map(color_pnl, subset=['盈亏']),
                width="stretch",
                hide_index=True,
            )
            st.caption('`手续费` 对应实际成交；`盈亏` 为卖出成交后的已实现损益（扣除手续费）；`锁定资源` 只表示订单尚未完成时占用的现金或可卖数量，成交后会显示"已释放"。')

    with fill_tab:
        if not fills:
            st.info("暂无成交记录")
        else:
            fdf = pd.DataFrame(fills).iloc[::-1].reset_index(drop=True)
            fdf['side'] = fdf['side'].map({'BUY':'🟢 买入','SELL':'🔴 卖出'}).fillna(fdf['side'])
            fdf['stock'] = fdf['code'].str.replace('US.', '', regex=False)
            fdf['price'] = fdf['price'].map(lambda x: f"${float(x):.2f}" if pd.notna(x) else '—')
            fdf['commission'] = fdf['commission'].map(lambda x: f"${float(x):.2f}" if pd.notna(x) else '—')
            fdf['pnl'] = fdf['pnl'].map(lambda x: f"${float(x):+.2f}" if pd.notna(x) else '—')
            fdf_show = fdf.rename(columns={
                'time':'时间',
                'fill_id':'成交号',
                'order_id':'订单号',
                'stock':'股票',
                'side':'方向',
                'qty':'数量',
                'price':'价格',
                'commission':'手续费',
                'reason':'原因',
                'pnl':'盈亏',
            })
            st.dataframe(
                fdf_show[['时间','成交号','订单号','股票','方向','数量','价格','手续费','原因','盈亏']]
                    .style.map(color_pnl, subset=['盈亏']),
                width="stretch",
                hide_index=True,
            )

    with trade_tab:
        if trades.empty:
            st.info("暂无记录")
        else:
            disp = trades.copy().iloc[::-1].reset_index(drop=True)
            disp['side']   = disp['side'].map({'BUY':'🟢 买入','SELL':'🔴 卖出'}).fillna(disp['side'])
            disp['reason'] = disp['reason'].map({
                'weekly_dca':'每周定投',
                'golden_cross':'金叉','trend_pullback':'回踩确认','breakout':'突破',
                'death_cross':'死叉','stop_loss':'止损','trailing_stop':'移动止损',
                'rsi_overbought':'RSI超买','partial_profit':'分批止盈',
                'init_position':'初始建仓','add_position':'加码',
                'micro_position':'底仓建仓','micro_stop_loss':'底仓止损',
            }).fillna(disp['reason'])
            disp['bucket'] = disp['bucket'].map(BUCKET_LABEL).fillna(disp['bucket'])
            disp = disp.rename(columns={'time':'时间','stock':'股票','bucket':'策略',
                                        'side':'方向','price':'价格','qty':'数量',
                                        'reason':'原因','pnl':'盈亏'})
            disp_show = disp[['时间','股票','策略','方向','价格','数量','原因','盈亏']].copy()
            disp_show['价格'] = disp_show['价格'].map(
                lambda x: f'${x:.2f}' if pd.notna(x) else '—'
            )
            disp_show['盈亏'] = disp_show['盈亏'].map(
                lambda x: f'${x:+.2f}' if pd.notna(x) else '—'
            )
            st.dataframe(
                disp_show.style
                    .map(color_pnl, subset=['盈亏']),
                width="stretch", hide_index=True)
            st.divider()
            wins  = (sells['pnl']>0).sum() if not sells.empty else 0
            losses= (sells['pnl']<=0).sum() if not sells.empty else 0
            total_t = len(sells)
            s1,s2,s3,s4,s5 = st.columns(5)
            s1.metric("已完成",   f"{total_t} 笔")
            s2.metric("胜率",     f"{wins/total_t*100:.1f}%" if total_t else "—")
            s3.metric("平均盈利", f"${sells[sells['pnl']>0]['pnl'].mean():+.2f}"  if wins   else "—")
            s4.metric("平均亏损", f"${sells[sells['pnl']<=0]['pnl'].mean():+.2f}" if losses else "—")
            s5.metric("手续费",   f"${comm:.2f}")

# ── Tab 5：绩效分析 ──────────────────────────────────────────
with t5:
    if sells.empty:
        st.info("暂无已实现交易，绩效指标将在首次卖出后显示。")
    else:
        pnls = sells['pnl'].dropna().tolist()
        metrics = calc_pnl_metrics(
            pnls,
            initial_cash=account['initial_cash'],
            n_periods=max(len(pnls), 1),
        )
        n = metrics['total_trades']
        win_rate_ = metrics['win_rate']
        pf_ = metrics['profit_factor']
        max_dd = metrics['max_dd']
        sharpe_ = metrics['sharpe']

        # KPI 行
        k1, k2, k3, k4, k5 = st.columns(5)
        k1.metric("已实现交易",  f"{n} 笔")
        k2.metric("胜率",        f"{win_rate_*100:.1f}%",
                  delta=f"{'盈利' if win_rate_>=0.5 else '亏损'}为主")
        k3.metric("利润因子",    f"{min(pf_, 99):.2f}",
                  delta="≥1.5 优秀" if pf_ >= 1.5 else "需改进")
        k4.metric("最大回撤",    f"{max_dd*100:.1f}%",
                  delta_color="inverse")
        k5.metric("Sharpe（近似）", f"{sharpe_:.2f}",
                  delta="≥1.0 可接受" if sharpe_ >= 1.0 else "偏低")

        st.divider()
        c_left, c_right = st.columns(2)

        with c_left:
            st.subheader("盈亏分布")
            dist_df = pd.DataFrame({'盈亏': pnls})
            dist_df['区间'] = pd.cut(dist_df['盈亏'],
                bins=[-float('inf'),-500,-100,0,100,500,float('inf')],
                labels=['<-$500','-$500~-$100','-$100~$0','$0~$100','$100~$500','>$500'])
            cnt = dist_df['区间'].value_counts().sort_index()
            st.bar_chart(cnt)

        with c_right:
            st.subheader("各桶绩效对比")
            if 'bucket' in sells.columns:
                bk_stats = []
                for bk, grp in sells.groupby('bucket'):
                    bpnls = grp['pnl'].dropna().tolist()
                    bwins = [p for p in bpnls if p > 0]
                    bk_stats.append({
                        '策略': BUCKET_LABEL.get(bk, bk),
                        '交易数': len(bpnls),
                        '胜率': f"{len(bwins)/len(bpnls)*100:.0f}%" if bpnls else '—',
                        '总盈亏': f"${sum(bpnls):+,.0f}",
                        '均盈': f"${sum(bwins)/len(bwins):+.0f}" if bwins else '—',
                        '均亏': f"${sum(p for p in bpnls if p<=0)/max(1,len([p for p in bpnls if p<=0])):+.0f}" if any(p<=0 for p in bpnls) else '—',
                    })
                bk_df = pd.DataFrame(bk_stats)
                st.dataframe(bk_df, width="stretch", hide_index=True)

        st.divider()
        st.subheader("月度盈亏")
        if 'time' in sells.columns:
            monthly = (sells.assign(月份=pd.to_datetime(sells['time']).dt.to_period('M'))
                       .groupby('月份')['pnl'].sum().reset_index())
            monthly['月份'] = monthly['月份'].astype(str)
            monthly.columns = ['月份', '盈亏']
            colors = ['#22c55e' if v >= 0 else '#ef4444' for v in monthly['盈亏']]
            st.bar_chart(monthly.set_index('月份')['盈亏'])

        avg_win_  = metrics['avg_win']
        avg_loss_ = metrics['avg_loss']
        st.caption(f"平均盈利 ${avg_win_:+.0f}  |  平均亏损 ${avg_loss_:+.0f}"
                   f"  |  盈亏比 {abs(avg_win_/avg_loss_):.2f}" if avg_loss_ else
                   f"平均盈利 ${avg_win_:+.0f}  |  无亏损记录")

# ── 自动刷新 ─────────────────────────────────────────────────
time.sleep(REFRESH)
st.rerun()
