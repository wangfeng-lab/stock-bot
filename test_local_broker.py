import os
import tempfile
import unittest
import json

from local_broker import LocalBroker


class LocalBrokerTests(unittest.TestCase):
    def test_migrates_existing_json_snapshot_to_sqlite(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "account.json")
            log_path = os.path.join(tmp, "trade_log.csv")
            sqlite_path = os.path.join(tmp, "account.sqlite3")

            with open(db_path, "w") as f:
                json.dump({
                    "initial_cash": 5000.0,
                    "cash": 4321.0,
                    "realized_pnl": 12.5,
                    "total_commission": 3.2,
                    "positions": {
                        "US.AAPL": {
                            "qty": 3,
                            "avg_cost": 100.0,
                            "bucket": "longterm",
                            "entry_time": "2026-06-01 10:00:00",
                        }
                    },
                    "orders": [],
                    "fills": [],
                    "next_order_id": 7,
                    "meta": {"markers": {"weekly_dca:US.QQQ": "2026-W23"}},
                }, f, ensure_ascii=False)

            broker = LocalBroker(db_path, log_path, initial_cash=1000.0)
            self.assertTrue(os.path.exists(sqlite_path))

            state = broker.get_state()
            self.assertEqual(state["cash"], 4321.0)
            self.assertEqual(state["next_order_id"], 7)
            self.assertEqual(state["positions"]["US.AAPL"]["qty"], 3)
            self.assertEqual(broker.get_marker("weekly_dca:US.QQQ"), "2026-W23")

    def test_submit_partial_fill_and_cancel(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "account.json")
            log_path = os.path.join(tmp, "trade_log.csv")

            broker = LocalBroker(db_path, log_path, initial_cash=1000.0)
            ok, msg, order = broker.submit_order(
                "US.QQQ", "BUY", 5, 100.0, bucket="dca", reason="weekly_dca"
            )
            self.assertTrue(ok, msg)
            self.assertIsNotNone(order)
            order_id = order["order_id"]

            pending = broker.get_order(order_id)
            self.assertEqual(pending["status"], "NEW")
            self.assertEqual(pending["remaining_qty"], 5)
            self.assertEqual(broker.get_cash(), 1000.0)
            self.assertAlmostEqual(broker.get_available_cash(), 499.0, places=6)

            ok, msg = broker.fill_order(order_id, qty=2, price=100.0)
            self.assertTrue(ok, msg)

            partial = broker.get_order(order_id)
            self.assertEqual(partial["status"], "PARTIALLY_FILLED")
            self.assertEqual(partial["filled_qty"], 2)
            self.assertEqual(partial["remaining_qty"], 3)
            self.assertEqual(len(broker.get_fills()), 1)
            self.assertEqual(broker.get_state()["positions"]["US.QQQ"]["qty"], 2)
            self.assertAlmostEqual(broker.get_cash(), 799.0, places=6)
            self.assertAlmostEqual(broker.get_available_cash(), 498.0, places=6)

            ok, msg = broker.cancel_order(order_id)
            self.assertTrue(ok, msg)
            canceled = broker.get_order(order_id)
            self.assertEqual(canceled["status"], "CANCELED")
            self.assertEqual(canceled["filled_qty"], 2)
            self.assertEqual(canceled["remaining_qty"], 3)
            self.assertAlmostEqual(broker.get_available_cash(), 799.0, places=6)

    def test_buy_creates_filled_order_and_fill_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "account.json")
            log_path = os.path.join(tmp, "trade_log.csv")

            broker = LocalBroker(db_path, log_path, initial_cash=1000.0)
            ok, _ = broker.place_order("US.QQQ", "BUY", 1, 100.0, bucket="dca", reason="weekly_dca")
            self.assertTrue(ok)

            orders = broker.get_orders()
            fills = broker.get_fills()
            self.assertEqual(len(orders), 1)
            self.assertEqual(len(fills), 1)
            self.assertEqual(orders[0]["status"], "FILLED")
            self.assertEqual(orders[0]["requested_qty"], 1)
            self.assertEqual(orders[0]["filled_qty"], 1)
            self.assertEqual(fills[0]["order_id"], orders[0]["order_id"])
            self.assertEqual(fills[0]["code"], "US.QQQ")

    def test_place_order_sell_clip_keeps_compatibility(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "account.json")
            log_path = os.path.join(tmp, "trade_log.csv")

            broker = LocalBroker(db_path, log_path, initial_cash=1000.0)
            ok, _ = broker.place_order("US.NOW", "BUY", 2, 100.0, bucket="micro", reason="micro_position")
            self.assertTrue(ok)

            ok, msg = broker.place_order("US.NOW", "SELL", 5, 101.0, bucket="micro", reason="trailing_stop")
            self.assertTrue(ok, msg)

            last_order = broker.get_orders(limit=1)[0]
            self.assertEqual(last_order["requested_qty"], 5)
            self.assertEqual(last_order["filled_qty"], 2)
            self.assertEqual(last_order["status"], "CANCELED")
            self.assertIn("auto_canceled_remainder", last_order["message"])
            self.assertNotIn("US.NOW", broker.get_state()["positions"])
            self.assertEqual(broker.get_available_cash(), broker.get_cash())

    def test_rejected_order_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "account.json")
            log_path = os.path.join(tmp, "trade_log.csv")

            broker = LocalBroker(db_path, log_path, initial_cash=100.0)
            ok, _ = broker.place_order("US.NVDA", "BUY", 10, 100.0, bucket="longterm", reason="golden_cross")
            self.assertFalse(ok)

            orders = broker.get_orders()
            fills = broker.get_fills()
            self.assertEqual(len(orders), 1)
            self.assertEqual(len(fills), 0)
            self.assertEqual(orders[0]["status"], "REJECTED")
            self.assertIn("现金不足", orders[0]["message"])

    def test_submit_sell_reserves_qty_and_cancel_releases(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "account.json")
            log_path = os.path.join(tmp, "trade_log.csv")

            broker = LocalBroker(db_path, log_path, initial_cash=1000.0)
            ok, _ = broker.place_order("US.MSFT", "BUY", 3, 100.0, bucket="longterm", reason="golden_cross")
            self.assertTrue(ok)

            ok, msg, order = broker.submit_order("US.MSFT", "SELL", 2, 110.0, bucket="longterm", reason="profit_take1")
            self.assertTrue(ok, msg)
            self.assertEqual(order["status"], "NEW")
            self.assertEqual(broker.get_available_qty("US.MSFT"), 1)

            ok, msg = broker.cancel_order(order["order_id"])
            self.assertTrue(ok, msg)
            self.assertEqual(broker.get_available_qty("US.MSFT"), 3)

    def test_marker_persists_across_instances(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "account.json")
            log_path = os.path.join(tmp, "trade_log.csv")

            broker = LocalBroker(db_path, log_path, initial_cash=1000.0)
            self.assertEqual(broker.get_marker("weekly_dca:US.QQQ"), "")

            broker.set_marker("weekly_dca:US.QQQ", "2026-W23")

            broker2 = LocalBroker(db_path, log_path, initial_cash=1000.0)
            self.assertEqual(broker2.get_marker("weekly_dca:US.QQQ"), "2026-W23")

    def test_sell_sets_reentry_cooldown_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "account.json")
            log_path = os.path.join(tmp, "trade_log.csv")

            broker = LocalBroker(db_path, log_path, initial_cash=1000.0)
            ok, _ = broker.place_order("US.NOW", "BUY", 2, 100.0, bucket="micro", reason="micro_position")
            self.assertTrue(ok)
            ok, _ = broker.place_order("US.NOW", "SELL", 2, 101.0, bucket="micro", reason="trailing_stop")
            self.assertTrue(ok)

            self.assertEqual(broker.last_sell_reason("US.NOW"), "trailing_stop")
            self.assertNotEqual(broker.get_marker("last_sell_ts:US.NOW", ""), "")
            self.assertTrue(broker.was_sold_recently("US.NOW", 60))
