import unittest

import market


class _FakeUpbit:
    def __init__(self, payload):
        self._payload = payload

    def get_balances(self):
        return self._payload


class MarketBalanceInfoTests(unittest.TestCase):
    def test_get_balance_info_includes_locked_in_total(self):
        upbit = _FakeUpbit(
            [
                {"currency": "KRW", "balance": "69366.0", "locked": "29749.0"},
                {"currency": "BTC", "balance": "0.0", "locked": "0.0"},
            ]
        )
        avail, total = market.get_balance_info(upbit, "KRW")
        self.assertAlmostEqual(avail, 69_366.0, places=6)
        self.assertAlmostEqual(total, 99_115.0, places=6)

    def test_get_total_balance_wraps_balance_info(self):
        upbit = _FakeUpbit([{"currency": "POKT", "balance": "10", "locked": "2.5"}])
        self.assertAlmostEqual(market.get_balance(upbit, "POKT"), 10.0, places=6)
        self.assertAlmostEqual(market.get_total_balance(upbit, "POKT"), 12.5, places=6)

    def test_get_balance_info_handles_error_payload(self):
        upbit = _FakeUpbit({"error": {"name": "invalid_query_payload", "message": "bad"}})
        avail, total = market.get_balance_info(upbit, "KRW")
        self.assertEqual(avail, 0.0)
        self.assertEqual(total, 0.0)


if __name__ == "__main__":
    unittest.main()
