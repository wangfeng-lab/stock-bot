import unittest

from execution_policy import (
    CASH_RESERVE_RATIO,
    atr_position_qty,
    cash_position_qty,
    entry_budget,
    is_starter_position,
)


class ExecutionPolicyTests(unittest.TestCase):
    def test_micro_budget_respects_fixed_cash(self):
        budget = entry_budget(
            cash=50_000.0,
            initial_cash=100_000.0,
            bucket_alloc=0.0,
            reason='micro_position',
        )
        self.assertEqual(budget, 300.0)

    def test_micro_budget_respects_cash_reserve(self):
        budget = entry_budget(
            cash=19_500.0,
            initial_cash=100_000.0,
            bucket_alloc=0.0,
            reason='micro_position',
            reserve_ratio=CASH_RESERVE_RATIO,
        )
        self.assertEqual(budget, 0.0)

    def test_starter_promotion_budget_caps_to_gap(self):
        budget = entry_budget(
            cash=250_000.0,
            initial_cash=1_000_000.0,
            bucket_alloc=0.10,
            reason='starter_promotion',
            current_position_value=80_000.0,
        )
        self.assertEqual(budget, 20_000.0)

    def test_add_position_budget_caps_to_half_position(self):
        budget = entry_budget(
            cash=250_000.0,
            initial_cash=1_000_000.0,
            bucket_alloc=0.10,
            reason='add_position',
            current_position_value=40_000.0,
        )
        self.assertEqual(budget, 16_000.0)

    def test_cash_position_qty_returns_zero_when_budget_too_small(self):
        self.assertEqual(cash_position_qty(1200.0, 1000.0), 0)

    def test_atr_qty_uses_budget_and_risk_limits(self):
        qty = atr_position_qty(
            price=100.0,
            atr=5.0,
            budget=6_000.0,
            reason='trend_pullback',
        )
        self.assertEqual(qty, 35)

    def test_starter_position_threshold(self):
        self.assertTrue(is_starter_position(25_000.0, 1_000_000.0, 0.10, threshold=0.35))
        self.assertFalse(is_starter_position(40_000.0, 1_000_000.0, 0.10, threshold=0.35))


if __name__ == '__main__':
    unittest.main()
