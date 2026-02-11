import unittest
from unittest.mock import patch

import bot


class EquityEstimateFallbackTests(unittest.TestCase):
    def test_estimate_equity_falls_back_to_state_qty_and_entry_price(self):
        strategy_state = {
            "MAIN": {
                "KRW-BTC": {
                    "holding": True,
                    "qty": 0.1234,
                    "entry": 100_000.0,
                }
            },
            "SCALP": {},
        }
        prices = {}

        with patch("bot.get_balance_info", return_value=(0.0, 0.0)):
            eq = bot.estimate_equity(krw=50_000.0, strategy_state=strategy_state, prices=prices, upbit=object())

        # 50,000 + (0.1234 * 100,000) = 62,340
        self.assertAlmostEqual(eq, 62_340.0, places=6)

    def test_estimate_equity_prefers_live_price_when_available(self):
        strategy_state = {
            "MAIN": {
                "KRW-BTC": {
                    "holding": True,
                    "qty": 1.0,
                    "entry": 100_000.0,
                }
            },
            "SCALP": {},
        }
        prices = {"KRW-BTC": 120_000.0}

        with patch("bot.get_balance_info", return_value=(1.0, 1.0)):
            eq = bot.estimate_equity(krw=10_000.0, strategy_state=strategy_state, prices=prices, upbit=object())

        self.assertAlmostEqual(eq, 130_000.0, places=6)

    def test_estimate_equity_falls_back_to_initial_volume_minus_realized_cost(self):
        strategy_state = {
            "MAIN": {
                "KRW-BTC": {
                    "holding": True,
                    "entry": 100_000.0,
                    "initial_volume": 2.0,
                    "total_buy_krw": 200_000.0,
                    "realized_cost_krw": 100_000.0,
                }
            },
            "SCALP": {},
        }
        prices = {"KRW-BTC": 120_000.0}

        with patch("bot.get_balance_info", return_value=(0.0, 0.0)):
            eq = bot.estimate_equity(krw=10_000.0, strategy_state=strategy_state, prices=prices, upbit=object())

        self.assertAlmostEqual(eq, 130_000.0, places=6)


if __name__ == "__main__":
    unittest.main()
