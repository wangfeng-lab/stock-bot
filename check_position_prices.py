"""
诊断：对比持仓股票的 moomoo 快照价格 vs dashboard 显示价格
用法：python3.14 check_position_prices.py
"""
from moomoo import OpenQuoteContext, RET_OK
from local_broker import LocalBroker
from market_utils import display_price_from_row, live_price_from_row, current_session

broker = LocalBroker('virtual_account.json', 'trade_log.csv')
state = broker.get_state()

positions = state['positions']
codes = list(positions.keys())
print(f"持仓 {len(codes)} 只：{[c.replace('US.','') for c in codes]}")
print(f"当前时段：{current_session()}\n")

ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
ret, snap = ctx.get_market_snapshot(codes)
ctx.close()

if ret != RET_OK:
    print("快照获取失败"); exit()

import pandas as pd
snap = pd.DataFrame(snap) if not hasattr(snap, 'iterrows') else snap

print(f"{'股票':8s} {'成本价':>10s} {'display':>10s} {'live':>10s} {'last':>10s} {'overnight':>10s} {'pre':>10s} {'bid/ask':>12s}")
print("-" * 80)
for _, row in snap.iterrows():
    code = str(row['code'])
    pos  = positions.get(code, {})
    cost = float(pos.get('avg_cost', 0))

    disp  = display_price_from_row(row)
    live  = live_price_from_row(row)
    last  = float(row.get('last_price') or 0)
    ovn   = float(row.get('overnight_price') or 0)
    pre   = float(row.get('pre_price') or 0)
    bid   = float(row.get('bid_price') or 0)
    ask   = float(row.get('ask_price') or 0)

    match = "✅" if abs(disp - cost) > 0.01 else "⚠️"
    ba_str = f"{bid:.2f}/{ask:.2f}" if bid > 0 else "—"
    print(f"{match} {code.replace('US.',''):6s}  ${cost:>9.2f}  ${disp:>9.2f}  ${live:>9.2f}  ${last:>9.2f}  ${ovn:>9.2f}  ${pre:>9.2f}  {ba_str}")
