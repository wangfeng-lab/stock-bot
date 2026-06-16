"""
performance.py — 统一绩效指标计算
"""

from __future__ import annotations

import math
from collections import defaultdict


# ── 信号归因统计 ────────────────────────────────────────────────────────────

def signal_attribution(trades: list[dict]) -> dict[str, dict]:
    """
    按入场信号类型对已平仓交易进行归因分析。

    输入：trade_log 中的记录列表（trade_log.csv 全量），字段：
        time   : 成交时间
        stock  : 股票代码
        side   : 'BUY' / 'SELL' / 'SELL_HALF' 等
        reason : BUY 侧为入场信号（golden_cross / breakout / ...），
                 SELL 侧为出场原因（stop_loss / trailing_stop / ...）
        pnl    : 平仓盈亏（美元），仅卖出记录非空
        price  : 成交价
        qty    : 成交量

    Returns:
        { entry_signal: { trades, win_rate, avg_win_pct, avg_loss_pct,
                          profit_factor, expectancy_pct, adj_factor } }

    adj_factor：相对于所有信号均值的调整系数，可直接乘以 alloc_mult。
      - adj_factor > 1.0 → 该信号历史表现优于均值，建议加仓
      - adj_factor < 1.0 → 该信号表现劣于均值，建议减仓
      - 数据不足（< MIN_TRADES）时 adj_factor = 1.0（不调整）

    实现：FIFO 匹配
      按时间排序后，维护每只股票的买入队列。
      每次卖出时从对应股票的买入队列头部取出，将卖出的 pnl_pct 归属到该入场信号。
    """
    MIN_TRADES = 5

    # 按时间排序（字符串时间格式可直接排序）
    sorted_trades = sorted(trades, key=lambda t: str(t.get('time', '')))

    # FIFO 匹配：每只股票维护买入队列 [(entry_reason, entry_price, qty), ...]
    buy_queues: dict[str, list[tuple[str, float, int]]] = defaultdict(list)
    groups: dict[str, list[float]] = defaultdict(list)

    for t in sorted_trades:
        side   = str(t.get('side', '') or '').upper()
        stock  = str(t.get('stock', '') or '')
        reason = str(t.get('reason', '') or '').strip()
        price  = float(t.get('price', 0) or 0)
        qty    = int(t.get('qty', 0) or 0)
        pnl_raw = t.get('pnl', '')

        if side == 'BUY' and qty > 0 and reason:
            buy_queues[stock].append((reason, price, qty))

        elif side.startswith('SELL') and pnl_raw not in ('', None) and qty > 0:
            pnl = float(pnl_raw)
            queue = buy_queues.get(stock, [])

            # 取对应买入记录（FIFO），若有多笔则按比例归因
            if queue:
                entry_reason, entry_price, entry_qty = queue[0]
                # pnl_pct 以入场成本为分母（更准确）
                entry_cost = entry_price * qty
                pnl_pct = pnl / entry_cost if entry_cost > 0 else 0.0

                groups[entry_reason].append(pnl_pct)

                # 更新队列：若卖出量 >= 首笔买入量，弹出该笔
                remaining = entry_qty - qty
                if remaining <= 0:
                    queue.pop(0)
                else:
                    queue[0] = (entry_reason, entry_price, remaining)
            else:
                # 没有匹配的买入记录（可能是历史遗留），归入 'unmatched'
                sell_value = price * qty
                pnl_pct = pnl / sell_value if sell_value > 0 else 0.0
                groups['unmatched'].append(pnl_pct)

    def _single_stats(pnl_pcts: list[float]) -> dict:
        n = len(pnl_pcts)
        wins   = [p for p in pnl_pcts if p > 0]
        losses = [p for p in pnl_pcts if p <= 0]
        win_rate      = len(wins) / n if n else 0.0
        avg_win       = sum(wins)   / len(wins)   if wins   else 0.0
        avg_loss      = sum(losses) / len(losses) if losses else 0.0
        gross_win     = sum(wins)
        gross_loss    = abs(sum(losses))
        profit_factor = gross_win / gross_loss if gross_loss > 0 else float('inf')
        expectancy    = win_rate * avg_win + (1 - win_rate) * avg_loss
        return {
            'trades':         n,
            'win_rate':       round(win_rate, 4),
            'avg_win_pct':    round(avg_win  * 100, 3),
            'avg_loss_pct':   round(avg_loss * 100, 3),
            'profit_factor':  round(profit_factor, 3),
            'expectancy_pct': round(expectancy * 100, 4),
            'adj_factor':     1.0,
        }

    result = {reason: _single_stats(pcts) for reason, pcts in groups.items()}

    # adj_factor：对有效数据量 >= MIN_TRADES 的信号做 z-score 归一化
    qualified = {r: s for r, s in result.items() if s['trades'] >= MIN_TRADES}
    if qualified:
        exp_values = [s['expectancy_pct'] for s in qualified.values()]
        mean_exp   = sum(exp_values) / len(exp_values)
        variance   = sum((x - mean_exp) ** 2 for x in exp_values) / len(exp_values)
        std_exp    = math.sqrt(variance) if variance > 0 else 1e-6
        for reason, stats in result.items():
            if stats['trades'] >= MIN_TRADES:
                z   = (stats['expectancy_pct'] - mean_exp) / (std_exp + 1e-10)
                adj = 1.0 + 0.2 * max(-2.5, min(2.5, z))   # ±50% 最大调整
                result[reason]['adj_factor'] = round(adj, 4)

    return result


def print_signal_attribution(attr: dict[str, dict]) -> None:
    """打印信号归因报告（控制台友好格式）。"""
    if not attr:
        print("暂无已平仓交易数据。")
        return

    # 按 expectancy_pct 降序
    rows = sorted(attr.items(), key=lambda x: x[1]['expectancy_pct'], reverse=True)
    header = f"{'信号类型':<20} {'交易数':>6} {'胜率':>7} {'均盈%':>8} {'均亏%':>8} {'盈亏比':>7} {'期望收益%':>10} {'adj':>6}"
    print("\n── 信号归因统计 " + "─" * 60)
    print(header)
    print("─" * len(header))
    for reason, s in rows:
        adj_flag = f"{s['adj_factor']:.2f}" if s['adj_factor'] != 1.0 else "  —  "
        print(
            f"{reason:<20} "
            f"{s['trades']:>6} "
            f"{s['win_rate']*100:>6.1f}% "
            f"{s['avg_win_pct']:>8.2f} "
            f"{s['avg_loss_pct']:>8.2f} "
            f"{s['profit_factor']:>7.2f} "
            f"{s['expectancy_pct']:>10.3f} "
            f"{adj_flag:>6}"
        )
    print("─" * len(header))
    print("  adj_factor > 1.0 → 该信号表现优于均值，动态加权模式下仓位放大")
    print("  adj_factor < 1.0 → 该信号表现劣于均值，动态加权模式下仓位收缩")
    print("  '—' 表示交易数不足（<5笔），不参与调整\n")


def calc_pnl_metrics(pnls: list[float],
                     initial_cash: float,
                     n_periods: int,
                     annualization: int = 252) -> dict:
    if not pnls:
        return {
            'total_trades': 0,
            'win_rate': 0.0,
            'avg_win': 0.0,
            'avg_loss': 0.0,
            'profit_factor': 0.0,
            'total_pnl': 0.0,
            'total_ret': 0.0,
            'ann_ret': 0.0,
            'max_dd': 0.0,
            'sharpe': 0.0,
        }

    total_pnl = sum(pnls)
    total_ret = total_pnl / initial_cash if initial_cash else 0.0
    ann_ret = (1 + total_ret) ** (annualization / max(n_periods, 1)) - 1 if total_ret > -1 else -1.0

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    win_rate = len(wins) / len(pnls)
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    profit_factor = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else float('inf')

    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for pnl in pnls:
        cum += pnl
        peak = max(peak, cum)
        drawdown = (peak - cum) / initial_cash if initial_cash else 0.0
        max_dd = max(max_dd, drawdown)

    if len(pnls) > 1:
        returns = [p / initial_cash for p in pnls]
        mean = sum(returns) / len(returns)
        variance = sum((x - mean) ** 2 for x in returns) / len(returns)
        sharpe = mean / (math.sqrt(variance) + 1e-10) * math.sqrt(annualization)
    else:
        sharpe = 0.0

    return {
        'total_trades': len(pnls),
        'win_rate': win_rate,
        'avg_win': avg_win,
        'avg_loss': avg_loss,
        'profit_factor': profit_factor,
        'total_pnl': total_pnl,
        'total_ret': total_ret,
        'ann_ret': ann_ret,
        'max_dd': max_dd,
        'sharpe': sharpe,
    }


def closed_trade_pnls(trades: list[dict]) -> list[float]:
    return [
        float(t['pnl'])
        for t in trades
        if t.get('side') in ('SELL', 'SELL_HALF', 'SELL_PARTIAL') and t.get('pnl') is not None
    ]
