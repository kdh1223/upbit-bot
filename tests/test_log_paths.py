import datetime as dt
import unittest
from zoneinfo import ZoneInfo

from utils.log_paths import report_log_path_for, trade_log_path_for


KST = ZoneInfo("Asia/Seoul")


class LogPathTests(unittest.TestCase):
    def test_monthly_trade_path(self):
        ts = dt.datetime(2026, 2, 9, 21, 0, 0, tzinfo=KST)
        self.assertTrue(trade_log_path_for(ts).endswith("trade_log_2026-02.csv"))

    def test_monthly_report_path(self):
        ts = dt.datetime(2026, 12, 1, 0, 0, 0, tzinfo=KST)
        self.assertTrue(report_log_path_for(ts).endswith("report_2026-12.log"))


if __name__ == "__main__":
    unittest.main()
