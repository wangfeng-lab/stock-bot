import sys
import types
import unittest

import pandas as pd


_moomoo_mock = types.ModuleType('moomoo')
_moomoo_mock.AuType = type('AuType', (), {'QFQ': 'qfq'})()
_moomoo_mock.KLType = type(
    'KLType',
    (),
    {'K_DAY': 'K_DAY', 'K_5M': 'K_5M', 'K_15M': 'K_15M', 'K_60M': 'K_60M', 'K_WEEK': 'K_WEEK'},
)()
_moomoo_mock.RET_OK = 0
sys.modules.setdefault('moomoo', _moomoo_mock)

from market_utils import (
    SESSION_AFTERHOURS,
    SESSION_CLOSED,
    SESSION_OVERNIGHT,
    SESSION_PREMARKET,
    SESSION_REGULAR,
    display_price_from_row,
)


class MarketUtilsTests(unittest.TestCase):
    def test_display_price_uses_premarket_trade_before_bid_ask(self):
        row = pd.Series({
            'pre_price': 493.00,
            'overnight_price': 510.80,
            'last_price': 516.10,
            'prev_close_price': 516.10,
            'bid_price': 492.50,
            'ask_price': 493.30,
        })
        self.assertEqual(display_price_from_row(row, session=SESSION_PREMARKET), 493.00)

    def test_display_price_uses_overnight_trade_before_bid_ask(self):
        row = pd.Series({
            'overnight_price': 1037.25,
            'after_price': 1036.80,
            'last_price': 1035.50,
            'prev_close_price': 1035.50,
            'bid_price': 1040.00,
            'ask_price': 1040.13,
        })
        self.assertEqual(display_price_from_row(row, session=SESSION_OVERNIGHT), 1037.25)

    def test_display_price_uses_afterhours_trade_before_bid_ask(self):
        row = pd.Series({
            'after_price': 782.17,
            'last_price': 775.35,
            'prev_close_price': 775.35,
            'bid_price': 782.00,
            'ask_price': 782.30,
        })
        self.assertEqual(display_price_from_row(row, session=SESSION_AFTERHOURS), 782.17)

    def test_display_price_regular_prefers_last_trade(self):
        row = pd.Series({
            'last_price': 520.00,
            'prev_close_price': 518.00,
            'bid_price': 519.80,
            'ask_price': 520.20,
        })
        self.assertEqual(display_price_from_row(row, session=SESSION_REGULAR), 520.00)

    def test_display_price_closed_falls_back_to_last(self):
        row = pd.Series({
            'last_price': 515.10,
            'prev_close_price': 514.90,
            'overnight_price': 0,
            'bid_price': 0,
            'ask_price': 0,
        })
        self.assertEqual(display_price_from_row(row, session=SESSION_CLOSED), 515.10)


if __name__ == '__main__':
    unittest.main()
