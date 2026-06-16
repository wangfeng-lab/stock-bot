import os
import tempfile
import unittest
from datetime import datetime, timedelta

from local_broker import LocalBroker
from order_reconciler import reconcile_open_orders


class OrderReconcilerTests(unittest.TestCase):
    def test_reconcile_fills_open_buy_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "account.json")
            log_path = os.path.join(tmp, "trade_log.csv")
            broker = LocalBroker(db_path, log_path, initial_cash=1000.0)

            ok, msg, order = broker.submit_order(
                "US.QQQ", "BUY", 2, 100.0, bucket="dca", reason="weekly_dca"
            )
            self.assertTrue(ok, msg)

            summary = reconcile_open_orders(
                broker,
                {"US.QQQ": 101.0},
                min_fill_age_seconds=0,
                timeout_seconds=120,
                now=datetime.now() + timedelta(seconds=10),
            )
            self.assertEqual(summary["filled_orders"], 1)

            state = broker.get_state()
            self.assertEqual(state["orders"][0]["status"], "FILLED")
            self.assertEqual(state["positions"]["US.QQQ"]["qty"], 2)
            self.assertEqual(len(state["fills"]), 1)

    def test_reconcile_cancels_quote_less_timeout_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "account.json")
            log_path = os.path.join(tmp, "trade_log.csv")
            broker = LocalBroker(db_path, log_path, initial_cash=1000.0)

            ok, msg, order = broker.submit_order(
                "US.QQQ", "BUY", 2, 100.0, bucket="dca", reason="weekly_dca"
            )
            self.assertTrue(ok, msg)

            submitted_at = datetime.now() - timedelta(seconds=180)
            state = broker.get_state()
            state["orders"][0]["submitted_at"] = submitted_at.strftime("%Y-%m-%d %H:%M:%S")
            state["orders"][0]["updated_at"] = state["orders"][0]["submitted_at"]
            broker._write(state)

            summary = reconcile_open_orders(
                broker,
                {},
                min_fill_age_seconds=5,
                timeout_seconds=120,
                now=datetime.now(),
            )
            self.assertEqual(summary["canceled_orders"], 1)

            order_after = broker.get_orders()[0]
            self.assertEqual(order_after["status"], "CANCELED")
            self.assertIn("timeout_no_quote", order_after["message"])
            self.assertEqual(len(broker.get_fills()), 0)


if __name__ == "__main__":
    unittest.main()
