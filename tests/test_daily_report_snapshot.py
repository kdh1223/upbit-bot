import unittest
from unittest.mock import patch

from run_daily_report import _safe_account_snapshot


class _FakeUpbit:
    def __init__(self, accounts):
        self._accounts = list(accounts or [])

    def get_balances(self):
        return list(self._accounts)


class DailyReportSnapshotTests(unittest.TestCase):
    def test_snapshot_price_batch_failure_falls_back_to_single(self):
        logs = []

        accounts = [
            {"currency": "BTC", "balance": "0.001", "locked": "0"},
            {"currency": "ETH", "balance": "0.01", "locked": "0"},
        ]

        def fake_price(arg):
            if isinstance(arg, list):
                raise RuntimeError("Code not found")
            if arg == "KRW-BTC":
                return 100_000_000.0
            if arg == "KRW-ETH":
                return 4_000_000.0
            return None

        with patch("run_daily_report.load_keys", return_value=("a", "b")), patch(
            "run_daily_report.get_balance_info", return_value=(50_000.0, 50_000.0)
        ), patch("run_daily_report.pyupbit.Upbit", return_value=_FakeUpbit(accounts)), patch(
            "run_daily_report.pyupbit.get_tickers", return_value=["KRW-BTC", "KRW-ETH"]
        ), patch(
            "run_daily_report.pyupbit.get_current_price", side_effect=fake_price
        ):
            snapshot = _safe_account_snapshot(logs.append)

        self.assertTrue(snapshot["has_coin"])
        self.assertGreater(snapshot["coin_value"], 0.0)
        self.assertAlmostEqual(snapshot["total_equity"], snapshot["krw_balance"] + snapshot["coin_value"], places=6)

    def test_snapshot_keeps_has_coin_for_non_krw_market_holdings(self):
        logs = []
        accounts = [{"currency": "USDT", "balance": "10", "locked": "0"}]

        with patch("run_daily_report.load_keys", return_value=("a", "b")), patch(
            "run_daily_report.get_balance_info", return_value=(12_345.0, 12_345.0)
        ), patch("run_daily_report.pyupbit.Upbit", return_value=_FakeUpbit(accounts)), patch(
            "run_daily_report.pyupbit.get_tickers", return_value=["KRW-BTC", "KRW-ETH"]
        ):
            snapshot = _safe_account_snapshot(logs.append)

        self.assertTrue(snapshot["has_coin"])
        self.assertEqual(snapshot["coin_value"], 0.0)
        self.assertEqual(snapshot["total_equity"], 12_345.0)
        self.assertTrue(any("skip non-KRW market holdings" in line for line in logs))

    def test_snapshot_uses_total_krw_including_locked(self):
        logs = []
        accounts = []
        with patch("run_daily_report.load_keys", return_value=("a", "b")), patch(
            "run_daily_report.get_balance_info", return_value=(69_366.0, 99_115.0)
        ), patch("run_daily_report.pyupbit.Upbit", return_value=_FakeUpbit(accounts)):
            snapshot = _safe_account_snapshot(logs.append)

        self.assertEqual(snapshot["krw_available"], 69_366.0)
        self.assertEqual(snapshot["krw_balance"], 99_115.0)
        self.assertEqual(snapshot["total_equity"], 99_115.0)


if __name__ == "__main__":
    unittest.main()
