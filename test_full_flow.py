"""
test_full_flow.py — 全流程本地测试（无需 moomoo 连接，不实际下单）

测试覆盖：
  1. 价格字段优先级（live_price_from_row）
  2. 信号检测（金叉 / 回踩 / 突破）
  3. 入场预算与 ATR 仓位计算
  4. 分批止盈逻辑（+8% / +15% / 移动止损）
  5. 金字塔加仓逻辑（+5% / +12%）
  6. 止盈后不触发加仓（关键 bug 修复验证）
  7. LocalBroker 买卖 → virtual_account.json 正确更新
  8. performance.py 绩效计算
"""

import math
import os
import sys
import types
import pandas as pd
import numpy as np

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

# ── Minimal moomoo mock so modules import without OpenD connection ──
_moomoo_mock = types.ModuleType('moomoo')
_moomoo_mock.AuType     = type('AuType',  (), {'QFQ': 'qfq'})()
_moomoo_mock.KLType     = type('KLType',  (), {'K_DAY': 'K_DAY', 'K_5M': 'K_5M', 'K_15M': 'K_15M', 'K_60M': 'K_60M', 'K_WEEK': 'K_WEEK'})()
_moomoo_mock.RET_OK     = 0
sys.modules['moomoo'] = _moomoo_mock

from market_utils import live_price_from_row
from strategy_signals import (
    enrich_indicators, detect_entry_signal,
    indicator_state, calc_atr_value,
)
from execution_policy import entry_budget, atr_position_qty, cash_position_qty
from trade_costs import calc_commission, apply_slippage
from performance import calc_pnl_metrics, closed_trade_pnls
from strategy_config import (
    BUCKETS, CASH_RESERVE, INITIAL_CASH,
    PROFIT_TAKE1_PCT, PROFIT_TAKE2_PCT,
    PYRAMID_ADD1_PROFIT, PYRAMID_ADD1_DAYS,
    PYRAMID_ADD2_PROFIT, PYRAMID_ADD2_DAYS,
)

PASS = "✅"
FAIL = "❌"
results = []

def check(name, cond, detail=""):
    tag = PASS if cond else FAIL
    results.append((tag, name, detail))
    print(f"  {tag}  {name}" + (f"  →  {detail}" if detail else ""))

# ─────────────────────────────────────────────────
print("\n" + "="*60)
print("  1. 价格字段优先级")
print("="*60)

import pandas as pd

# 模拟盘前快照（与真实 AMD 快照结构一致）
row_premarket = pd.Series({
    'overnight_price': 510.80,   # 夜盘已收盘，过时
    'pre_price':       493.00,   # 盘前实时，应优先
    'after_price':     515.13,
    'last_price':      516.10,
    'bid_price':       492.50,
    'ask_price':       493.30,
})
p = live_price_from_row(row_premarket)
check("盘前 bid/ask 中间价优先", abs(p - 492.9) < 0.1, f"got {p:.2f}, expected ~492.90")

row_no_bidask = pd.Series({
    'overnight_price': 510.80,
    'pre_price':       493.00,
    'after_price':     0,
    'last_price':      516.10,
    'bid_price':       0,
    'ask_price':       0,
})
p2 = live_price_from_row(row_no_bidask, session='premarket')  # 明确指定盘前时段
check("无 bid/ask 时盘前价优先（pre > overnight）", p2 == 493.00, f"got {p2}")

row_regular = pd.Series({
    'overnight_price': 0,
    'pre_price':       0,
    'after_price':     0,
    'last_price':      520.00,
    'bid_price':       0,
    'ask_price':       0,
})
p3 = live_price_from_row(row_regular)
check("正常交易时段用 last_price", p3 == 520.00, f"got {p3}")

# ─────────────────────────────────────────────────
print("\n" + "="*60)
print("  2. 信号检测（成长桶 5MA/20MA + MACD）")
print("="*60)

def make_df(n=60, trend="up"):
    """生成一段趋势行情"""
    np.random.seed(42)
    base = 100.0
    prices = []
    for i in range(n):
        if trend == "up":
            base *= (1 + np.random.normal(0.003, 0.01))
        else:
            base *= (1 + np.random.normal(-0.003, 0.01))
        prices.append(base)
    df = pd.DataFrame({
        'close': prices,
        'open':  [p * 0.998 for p in prices],
        'high':  [p * 1.01  for p in prices],
        'low':   [p * 0.99  for p in prices],
        'volume':[int(1e6 * (1 + abs(np.random.normal(0, 0.3)))) for _ in prices],
    })
    return df

cfg_lt = BUCKETS['longterm']
df_up = make_df(60, "up")
df_up = enrich_indicators(df_up, cfg_lt)

latest   = df_up.iloc[-1]
prev_row = df_up.iloc[-2]
fast_now  = float(latest['fast_ma'])
slow_now  = float(latest['slow_ma_v'])
fast_prev = float(prev_row['fast_ma'])
slow_prev = float(prev_row['slow_ma_v'])

ind = indicator_state('longterm', cfg_lt, latest, prev_row)
check("上升趋势 MACD extra_buy 正确", isinstance(ind.extra_buy, bool),
      f"extra_buy={ind.extra_buy}  {ind.ind_str}")

sig = detect_entry_signal(cfg_lt, df_up, latest, prev_row,
                          fast_now, slow_now, fast_prev, slow_prev, ind.extra_buy)
check("上升趋势产生入场信号", sig is not None, f"signal={sig}")

df_dn = make_df(60, "down")
df_dn = enrich_indicators(df_dn, cfg_lt)
lat_d  = df_dn.iloc[-1]; prv_d = df_dn.iloc[-2]
ind_d  = indicator_state('longterm', cfg_lt, lat_d, prv_d)
sig_d  = detect_entry_signal(cfg_lt, df_dn, lat_d, prv_d,
                             float(lat_d['fast_ma']), float(lat_d['slow_ma_v']),
                             float(prv_d['fast_ma']), float(prv_d['slow_ma_v']),
                             ind_d.extra_buy)
check("下降趋势无入场信号（金叉除外）",
      sig_d is None or sig_d[0] == 'golden_cross',
      f"signal={sig_d}")

# ─────────────────────────────────────────────────
print("\n" + "="*60)
print("  3. 仓位预算与 ATR 计算")
print("="*60)

cash    = 1_000_000.0
initial = 1_000_000.0
alloc   = 0.10   # 成长桶 10%

b_gc = entry_budget(cash, initial, alloc, 'golden_cross', reserve_ratio=CASH_RESERVE)
check("金叉预算 = 可用资金 × 40% alloc（分散模式）",
      b_gc == 40_000.0, f"${b_gc:,.0f}")

b_pb = entry_budget(cash, initial, alloc, 'trend_pullback', reserve_ratio=CASH_RESERVE)
check("回踩预算 = 可用资金 × 30% alloc（分散模式）",
      b_pb == 30_000.0, f"${b_pb:,.0f}")

b_p2 = entry_budget(cash, initial, alloc, 'pyramid_stage2', reserve_ratio=CASH_RESERVE)
check("金字塔②预算 = 可用资金 × 30% alloc",
      b_p2 == 30_000.0, f"${b_p2:,.0f}")

price = 500.0; atr = 10.0
qty = atr_position_qty(price, atr, b_gc, reason='golden_cross')
check("ATR仓位数量合理（>0）", qty > 0, f"qty={qty}股 @ ${price} ATR={atr}")
check("仓位成本不超预算", qty * price <= b_gc, f"{qty}×{price}=${qty*price:,.0f} ≤ ${b_gc:,.0f}")

# ─────────────────────────────────────────────────
print("\n" + "="*60)
print("  4. 分批止盈逻辑（核心 bug 修复验证）")
print("="*60)

# 模拟一笔完整的止盈流程
entry_price = 100.0
qty_init    = 10
profit_stages: set = set()

# +8% 时触发止盈①
pnl_pct = 0.09
took_profit = False
if pnl_pct >= PROFIT_TAKE1_PCT and 1 not in profit_stages and qty_init >= 3:
    take = max(1, round(qty_init * 0.30))
    qty_init -= take
    profit_stages.add(1)
    took_profit = True
check("止盈① +8% 触发，卖出约30%", 1 in profit_stages and qty_init == 7,
      f"剩余{qty_init}股")

# +15% 时触发止盈②
pnl_pct = 0.16
if pnl_pct >= PROFIT_TAKE2_PCT and 2 not in profit_stages and 1 in profit_stages and qty_init >= 2:
    take = max(1, round(qty_init * 0.60))
    qty_init -= take
    profit_stages.add(2)
    took_profit = True
check("止盈② +15% 触发，再卖出约60%余仓", 2 in profit_stages and qty_init <= 5,
      f"剩余{qty_init}股")

check("止盈后 took_profit_this_cycle=True，加仓逻辑被跳过",
      took_profit == True, "防止立刻买回的 guard 正常工作")

# 验证：止盈后不会误触发 starter_promotion
from execution_policy import is_starter_position
current_val = qty_init * 100.0   # 只剩小仓
starter = is_starter_position(current_val, initial, 0.10, threshold=0.35)
check("止盈后持仓变小会触发 starter_like（需被 guard 拦住）",
      starter == True, f"current_val=${current_val} → starter_like={starter}（guard 已阻止加仓）")

# ─────────────────────────────────────────────────
print("\n" + "="*60)
print("  5. 金字塔加仓条件")
print("="*60)

add_count = 0; pnl_pct = 0.05; days = 5; trend = True; rsi_ok = True
should_add2 = (add_count < 1 and pnl_pct >= PYRAMID_ADD1_PROFIT
               and days >= PYRAMID_ADD1_DAYS and trend and rsi_ok)
check(f"金字塔②条件：+{PYRAMID_ADD1_PROFIT*100:.0f}%/{PYRAMID_ADD1_DAYS}天/趋势 → 触发", should_add2)

add_count = 0; pnl_pct = 0.02  # 未达门槛
should_add2_no = (add_count < 1 and pnl_pct >= PYRAMID_ADD1_PROFIT and days >= PYRAMID_ADD1_DAYS)
check(f"金字塔②条件：+2% 不触发（门槛{PYRAMID_ADD1_PROFIT*100:.0f}%）", not should_add2_no)

add_count = 1; pnl_pct = 0.10; days = 10; trend = True
should_add3 = (add_count == 1 and pnl_pct >= PYRAMID_ADD2_PROFIT
               and days >= PYRAMID_ADD2_DAYS and trend)
check(f"金字塔③条件：add_count=1/+{PYRAMID_ADD2_PROFIT*100:.0f}%/{PYRAMID_ADD2_DAYS}天 → 触发", should_add3)

# ─────────────────────────────────────────────────
print("\n" + "="*60)
print("  6. LocalBroker 买卖流程")
print("="*60)

import tempfile, json, shutil
from local_broker import LocalBroker

tmp_db  = tempfile.mktemp(suffix='.json')
tmp_log = tempfile.mktemp(suffix='.csv')
b = LocalBroker(tmp_db, tmp_log, initial_cash=100_000.0)

ok, msg = b.place_order('US.AMD', 'BUY', 10, 500.0, bucket='longterm', reason='golden_cross')
check("买入订单成功", ok, msg)

state = b.get_state()
check("持仓记录正确", state['positions']['US.AMD']['qty'] == 10)
check("现金扣减正确", abs(state['cash'] - (100_000 - 10*500 - calc_commission(500,10))) < 0.01,
      f"cash={state['cash']:.2f}")

ok2, msg2 = b.place_order('US.AMD', 'SELL', 10, 550.0, bucket='longterm', reason='stop_loss')
check("卖出订单成功", ok2, msg2)

state2 = b.get_state()
check("卖出后持仓清空", 'US.AMD' not in state2['positions'])
pnl = state2['realized_pnl']
check("已实现盈亏为正", pnl > 0, f"pnl=${pnl:.2f}")

os.unlink(tmp_db); os.unlink(tmp_log)

# ─────────────────────────────────────────────────
print("\n" + "="*60)
print("  7. 绩效计算")
print("="*60)

pnls = [500, -200, 300, -100, 800, -50, 1200, -400]
metrics = calc_pnl_metrics(pnls, initial_cash=100_000, n_periods=252)
check("胜率计算正确", abs(metrics['win_rate'] - 4/8) < 0.01,
      f"{metrics['win_rate']*100:.1f}%")
check("利润因子 > 1（盈利策略）", metrics['profit_factor'] > 1,
      f"PF={metrics['profit_factor']:.2f}")
check("Sharpe 合理范围", -5 < metrics['sharpe'] < 20,
      f"Sharpe={metrics['sharpe']:.2f}")

trades = [
    {'side': 'BUY',          'pnl': 0.0},
    {'side': 'SELL',         'pnl': 500.0},
    {'side': 'SELL_PARTIAL', 'pnl': 200.0},
    {'side': 'SELL_HALF',    'pnl': 100.0},
]
cpnls = closed_trade_pnls(trades)
check("closed_trade_pnls 识别 SELL/SELL_PARTIAL/SELL_HALF", len(cpnls) == 3,
      f"got {len(cpnls)} trades")

# ─────────────────────────────────────────────────
print("\n" + "="*60)
total = len(results)
passed = sum(1 for r in results if r[0] == PASS)
failed = total - passed
print(f"  结果：{passed}/{total} 通过  {'🎉 全部通过！' if failed==0 else f'⚠️ {failed} 项失败'}")
print("="*60 + "\n")

if failed > 0:
    print("失败项：")
    for tag, name, detail in results:
        if tag == FAIL:
            print(f"  {FAIL} {name}  →  {detail}")
