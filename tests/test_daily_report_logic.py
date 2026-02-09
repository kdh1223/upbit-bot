import datetime as dt
import unittest
from zoneinfo import ZoneInfo

from run_daily_report import _mdd_pct, build_metrics, report_window_21_to_21


KST = ZoneInfo("Asia/Seoul")


class DailyReportLogicTests(unittest.TestCase):
    def test_report_window_before_21(self):
        now = dt.datetime(2026, 2, 10, 20, 0, 0, tzinfo=KST)
        start, end = report_window_21_to_21(now)
        self.assertEqual(end, dt.datetime(2026, 2, 9, 21, 0, 0, tzinfo=KST))
        self.assertEqual(start, dt.datetime(2026, 2, 8, 21, 0, 0, tzinfo=KST))

    def test_build_metrics_basic(self):
        rows = [
            {"pnl_pct": "1.0", "reason": "tp2"},
            {"pnl_pct": "-0.5", "reason": "stoploss"},
            {"pnl_pct": "0.3", "reason": "trailing"},
        ]
        m = build_metrics(rows)
        self.assertEqual(m["n"], 3)
        self.assertGreater(m["wr"], 60.0)
        self.assertAlmostEqual(m["sl_ratio"], 33.3333333333, places=2)

    def test_mdd_is_negative(self):
        rows = [
            {"pnl_pct": "2.0"},
            {"pnl_pct": "-10.0"},
            {"pnl_pct": "1.0"},
        ]
        mdd = _mdd_pct(rows, 1_000_000.0)
        self.assertLess(mdd, 0.0)


if __name__ == "__main__":
    unittest.main()
