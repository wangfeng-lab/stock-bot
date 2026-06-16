"""
test_new_features.py — 新增功能单元测试

覆盖：
  - signal_attribution()        信号归因，含 FIFO 匹配和 adj_factor
  - discussion_alloc_modifier() 讨论热度调整系数
  - circuit breaker             高水位、熔断触发、恢复
  - in_reentry_cooldown()       按卖出原因区分冷静期
  - entry_budget()              use_dynamic_alloc 开关
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

# ── moomoo 桩（沙箱无此包，shared_state / strategy_config 会 import 它）──
_moomoo_stub = MagicMock()
_moomoo_stub.KLType = MagicMock()
sys.modules.setdefault('moomoo', _moomoo_stub)


# ── signal_attribution ───────────────────────────────────────
class TestSignalAttribution(unittest.TestCase):

    def _make_trades(self):
        """构造一组可预期结果的买卖记录。"""
        return [
            # golden_cross 买入 100股 @ $10
            {'time': '2026-01-01 10:00:00', 'stock': 'US.A', 'side': 'BUY',
             'reason': 'golden_cross', 'price': '10', 'qty': '100', 'pnl': ''},
            # breakout 买入 50股 @ $20
            {'time': '2026-01-02 10:00:00', 'stock': 'US.B', 'side': 'BUY',
             'reason': 'breakout', 'price': '20', 'qty': '50', 'pnl': ''},
            # US.A 止盈卖出 100股 @ $11，pnl = (11-10)*100 = $100
            {'time': '2026-01-05 10:00:00', 'stock': 'US.A', 'side': 'SELL',
             'reason': 'trailing_stop', 'price': '11', 'qty': '100', 'pnl': '100'},
            # US.B 止损卖出 50股 @ $18，pnl = (18-20)*50 = -$100
            {'time': '2026-01-06 10:00:00', 'stock': 'US.B', 'side': 'SELL',
             'reason': 'stop_loss', 'price': '18', 'qty': '50', 'pnl': '-100'},
        ]

    def test_basic_attribution(self):
        from performance import signal_attribution
        trades = self._make_trades()
        attr = signal_attribution(trades)

        # golden_cross → US.A → 盈利 $100 / 入场成本 $1000 = +10%
        self.assertIn('golden_cross', attr)
        self.assertEqual(attr['golden_cross']['trades'], 1)
        self.assertAlmostEqual(attr['golden_cross']['win_rate'], 1.0)
        self.assertAlmostEqual(attr['golden_cross']['avg_win_pct'], 10.0, places=1)

        # breakout → US.B → 亏损 $100 / 入场成本 $1000 = -10%
        self.assertIn('breakout', attr)
        self.assertEqual(attr['breakout']['trades'], 1)
        self.assertAlmostEqual(attr['breakout']['win_rate'], 0.0)
        self.assertAlmostEqual(attr['breakout']['avg_loss_pct'], -10.0, places=1)

    def test_adj_factor_requires_min_trades(self):
        """数据不足 5 笔时 adj_factor 应保持 1.0。"""
        from performance import signal_attribution
        trades = self._make_trades()   # 每类只有 1 笔
        attr = signal_attribution(trades)
        for stats in attr.values():
            self.assertEqual(stats['adj_factor'], 1.0,
                             "数据量不足时 adj_factor 应为 1.0")

    def test_adj_factor_computed_with_enough_trades(self):
        """5 笔以上时 adj_factor 应有所区分。"""
        from performance import signal_attribution

        def _buy(stock, reason, price):
            return {'time': '2026-01-01 09:00:00', 'stock': stock, 'side': 'BUY',
                    'reason': reason, 'price': str(price), 'qty': '10', 'pnl': ''}

        def _sell(stock, pnl, price=11):
            return {'time': '2026-01-05 09:00:00', 'stock': stock, 'side': 'SELL',
                    'reason': 'trailing_stop', 'price': str(price), 'qty': '10', 'pnl': str(pnl)}

        # good_signal: 5次全部盈利
        # bad_signal:  5次全部亏损
        trades = []
        for i in range(5):
            s = f'US.G{i}'
            trades.append(_buy(s, 'good_signal', 10))
            trades.append(_sell(s, 50))   # +50%
        for i in range(5):
            s = f'US.B{i}'
            trades.append(_buy(s, 'bad_signal', 10))
            trades.append(_sell(s, -30, price=7))   # -30%

        attr = signal_attribution(trades)
        self.assertIn('good_signal', attr)
        self.assertIn('bad_signal', attr)
        self.assertGreater(attr['good_signal']['adj_factor'],
                           attr['bad_signal']['adj_factor'],
                           "好信号的 adj_factor 应大于差信号")

    def test_unmatched_sell_goes_to_unmatched(self):
        """没有对应买入记录的卖出应归入 'unmatched'。"""
        from performance import signal_attribution
        trades = [
            {'time': '2026-01-05 10:00:00', 'stock': 'US.X', 'side': 'SELL',
             'reason': 'stop_loss', 'price': '50', 'qty': '10', 'pnl': '-100'},
        ]
        attr = signal_attribution(trades)
        self.assertIn('unmatched', attr)


# ── discussion_alloc_modifier ────────────────────────────────
class TestDiscussionAllocModifier(unittest.TestCase):

    def _make_feed(self, items):
        return {'items': items}

    def test_top10_reduces(self):
        from discussion_universe import discussion_alloc_modifier
        feed = self._make_feed([
            {'rank': 3, 'symbol': 'NVDA', 'mentions': 500},
        ])
        mult, note = discussion_alloc_modifier('US.NVDA', feed)
        self.assertAlmostEqual(mult, 0.75)
        self.assertIn('过热', note)

    def test_rank_11_50_boosts(self):
        from discussion_universe import discussion_alloc_modifier
        feed = self._make_feed([
            {'rank': 25, 'symbol': 'AMD', 'mentions': 100},
        ])
        mult, note = discussion_alloc_modifier('US.AMD', feed)
        self.assertAlmostEqual(mult, 1.15)
        self.assertIn('动量', note)

    def test_rank_51_neutral(self):
        from discussion_universe import discussion_alloc_modifier
        feed = self._make_feed([
            {'rank': 80, 'symbol': 'TSLA', 'mentions': 20},
        ])
        mult, note = discussion_alloc_modifier('US.TSLA', feed)
        self.assertAlmostEqual(mult, 1.00)

    def test_not_in_feed_neutral(self):
        from discussion_universe import discussion_alloc_modifier
        mult, note = discussion_alloc_modifier('US.UNKNOWN', {'items': []})
        self.assertAlmostEqual(mult, 1.00)
        self.assertEqual(note, '')


# ── circuit breaker ──────────────────────────────────────────
class TestCircuitBreaker(unittest.TestCase):

    def setUp(self):
        """每个测试前重置熔断状态。"""
        import shared_state as ss
        ss._portfolio_hwm   = 1_000_000.0
        ss._cb_paused_until = None

    def test_no_trigger_below_threshold(self):
        import shared_state as ss
        # 组合价值只跌了 5%，不触发
        with patch.object(ss, 'estimate_portfolio_value', return_value=950_000.0):
            triggered, _ = ss.update_circuit_breaker()
        self.assertFalse(triggered)
        self.assertFalse(ss.is_circuit_breaker_active())

    def test_trigger_at_threshold(self):
        import shared_state as ss
        # 组合价值从高水位跌 10%，触发熔断
        ss._portfolio_hwm = 1_000_000.0
        with patch.object(ss, 'estimate_portfolio_value', return_value=880_000.0):
            triggered, note = ss.update_circuit_breaker()
        self.assertTrue(triggered)
        self.assertTrue(ss.is_circuit_breaker_active())
        self.assertIn('熔断', note)

    def test_hwm_updates_on_new_high(self):
        import shared_state as ss
        ss._portfolio_hwm = 1_000_000.0
        with patch.object(ss, 'estimate_portfolio_value', return_value=1_200_000.0):
            ss.update_circuit_breaker()
        self.assertAlmostEqual(ss._portfolio_hwm, 1_200_000.0)

    def test_no_retrigger_during_pause(self):
        import shared_state as ss
        # 首次触发
        ss._portfolio_hwm = 1_000_000.0
        with patch.object(ss, 'estimate_portfolio_value', return_value=880_000.0):
            triggered1, _ = ss.update_circuit_breaker()
        self.assertTrue(triggered1)
        # 再次调用，仍在熔断期，不重复触发
        with patch.object(ss, 'estimate_portfolio_value', return_value=800_000.0):
            triggered2, _ = ss.update_circuit_breaker()
        self.assertFalse(triggered2)

    def test_circuit_breaker_expires(self):
        import shared_state as ss
        # 设置一个已过期的熔断时间
        ss._cb_paused_until = datetime.now() - timedelta(seconds=1)
        self.assertFalse(ss.is_circuit_breaker_active())
        self.assertEqual(ss.get_circuit_breaker_status(), '')


# ── in_reentry_cooldown ──────────────────────────────────────
class TestReentryCooldown(unittest.TestCase):

    def _mock_broker(self, last_reason: str, sold_recently: bool):
        mock = MagicMock()
        mock.last_sell_reason.return_value = last_reason
        mock.was_sold_recently.return_value = sold_recently
        return mock

    def test_stop_loss_uses_24h(self):
        import shared_state as ss
        original = ss.broker
        ss.broker = self._mock_broker('stop_loss', True)
        try:
            result = ss.in_reentry_cooldown('US.A', 30)
            # 验证 was_sold_recently 被以 1440 分钟调用
            ss.broker.was_sold_recently.assert_called_once_with('US.A', 1440)
            self.assertTrue(result)
        finally:
            ss.broker = original

    def test_death_cross_uses_4h(self):
        import shared_state as ss
        original = ss.broker
        ss.broker = self._mock_broker('death_cross', False)
        try:
            ss.in_reentry_cooldown('US.B', 30)
            ss.broker.was_sold_recently.assert_called_once_with('US.B', 240)
        finally:
            ss.broker = original

    def test_normal_exit_uses_default(self):
        import shared_state as ss
        original = ss.broker
        ss.broker = self._mock_broker('trailing_stop', False)
        try:
            ss.in_reentry_cooldown('US.C', 30)
            ss.broker.was_sold_recently.assert_called_once_with('US.C', 30)
        finally:
            ss.broker = original


# ── entry_budget dynamic alloc switch ────────────────────────
class TestEntryBudgetSwitch(unittest.TestCase):

    def test_dynamic_off_uses_static(self):
        """use_dynamic_alloc=False 时，不调用 signal_stats。"""
        from execution_policy import entry_budget
        with patch('execution_policy.entry_budget.__wrapped__', create=True):
            with patch('signal_stats.get_dynamic_alloc_mult') as mock_dyn:
                budget = entry_budget(
                    800_000, 1_000_000, 0.05,
                    'golden_cross',
                    use_dynamic_alloc=False,
                )
                mock_dyn.assert_not_called()
        # 静态：bucket_cash=50000, alloc_mult=0.40, liquid=600000 → min=20000
        self.assertAlmostEqual(budget, 20_000.0, delta=1.0)

    def test_dynamic_on_calls_stats(self):
        """use_dynamic_alloc=True 时结果与 adj=1.0 的静态值相同（patch signal_stats）。"""
        from execution_policy import entry_budget
        # get_dynamic_alloc_mult 在 entry_budget 内部 lazy import，
        # 需要 patch signal_stats 模块里的函数
        with patch('signal_stats.get_dynamic_alloc_mult', return_value=0.40):
            budget_dyn = entry_budget(
                800_000, 1_000_000, 0.05,
                'golden_cross',
                use_dynamic_alloc=True,
            )
        budget_static = entry_budget(
            800_000, 1_000_000, 0.05,
            'golden_cross',
            use_dynamic_alloc=False,
        )
        # adj=1.0 时动态和静态应相等
        self.assertAlmostEqual(budget_dyn, budget_static, delta=1.0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
