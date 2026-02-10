import datetime as dt
import unittest
from zoneinfo import ZoneInfo

from run_daily_report import _calc_window_pnl_krw_pct, _mdd_pct, build_heartbeat_text, build_metrics, build_report_text, report_window_21_to_21


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

    def test_build_metrics_ignores_partial_reasons(self):
        rows = [
            {"pnl_pct": "3.0", "reason": "TP1"},
            {"pnl_pct": "2.0", "reason": "TP2_PARTIAL"},
            {"pnl_pct": "-1.0", "reason": "RUNNER_TIMEOUT"},
        ]
        m = build_metrics(rows)
        self.assertEqual(m["n"], 1)
        self.assertAlmostEqual(m["avg"], -1.0, places=6)

    def test_window_pnl_krw_pct(self):
        rows = [
            {"time_dt": dt.datetime(2026, 2, 8, 22, 0, 0, tzinfo=KST), "pnl_pct": "10.0"},
            {"time_dt": dt.datetime(2026, 2, 9, 22, 0, 0, tzinfo=KST), "pnl_pct": "10.0"},
        ]
        start = dt.datetime(2026, 2, 9, 21, 0, 0, tzinfo=KST)
        end = dt.datetime(2026, 2, 10, 21, 0, 0, tzinfo=KST)
        pnl_krw, pnl_pct, start_eq, end_eq = _calc_window_pnl_krw_pct(rows, start, end, 1000.0)
        self.assertAlmostEqual(start_eq, 1100.0, places=6)
        self.assertAlmostEqual(end_eq, 1210.0, places=6)
        self.assertAlmostEqual(pnl_krw, 110.0, places=6)
        self.assertAlmostEqual(pnl_pct, 10.0, places=6)

    def test_build_report_text_without_overall_sections(self):
        report_end = dt.datetime(2026, 2, 10, 21, 0, 0, tzinfo=KST)
        day_start = dt.datetime(2026, 2, 9, 21, 0, 0, tzinfo=KST)
        text = build_report_text(
            report_end=report_end,
            day_start=day_start,
            day={"n": 2, "wr": 50.0, "avg": 1.23, "max": 3.0, "min": -1.0, "sl_ratio": 50.0, "avg10": 0.5},
            month={"n": 10, "wr": 60.0, "avg": 0.8, "cum": 8.3},
            by_strategy={"MAIN": {"n": 7, "wr": 57.14, "avg": 0.9}, "SCALP_BTC": {"n": 3, "wr": 66.6, "avg": 0.5}},
            snapshot={"krw_balance": 12345.0, "coin_value": 0.0, "total_equity": 12345.0, "has_coin": False},
            pnl_amounts={"daily_krw": 1000.0, "daily_pct": 1.0, "month_krw": 5000.0, "month_pct": 5.0},
            month_mdd_pct=-1.23,
            status_emoji="🟡",
        )
        self.assertIn("📊 일일 성적 리포트 (KST) | 2026-02-10 21:00", text)
        self.assertIn("기간: 02/09 21:00 ~ 02/10 21:00", text)
        self.assertIn("📅 오늘", text)
        self.assertIn("- 일일 손익: +1,000원 (+1.00%)", text)
        self.assertIn("📆 이번 달 (2026-02)", text)
        self.assertIn("- 월간 MDD: -1.23%", text)
        self.assertIn("📌 전략별 (이번 달)", text)
        self.assertIn("상태: 🟡", text)
        self.assertNotIn("전체", text)
        self.assertNotIn("누적(복리)", text)
        self.assertNotIn("누적 손익", text)

    def test_build_report_text_month_mdd_na(self):
        report_end = dt.datetime(2026, 2, 10, 21, 0, 0, tzinfo=KST)
        day_start = dt.datetime(2026, 2, 9, 21, 0, 0, tzinfo=KST)
        text = build_report_text(
            report_end=report_end,
            day_start=day_start,
            day={},
            month={},
            by_strategy={},
            snapshot={"krw_balance": 0.0, "coin_value": 0.0, "total_equity": 0.0, "has_coin": False},
            pnl_amounts={},
            month_mdd_pct=None,
            status_emoji="🟢",
        )
        self.assertIn("- 월간 MDD: N/A", text)

    def test_heartbeat_includes_entry_guard_line(self):
        now = dt.datetime(2026, 2, 10, 9, 5, 0, tzinfo=KST)
        text = build_heartbeat_text(
            now=now,
            service_status="실행중",
            asset_text="100,000 KRW",
            main_holding_cnt=0,
            scalp_holding_cnt=0,
            risk_state={"halted_flag": False, "halt_reason": ""},
        )
        self.assertIn("신규진입가드: ACTIVE", text)


if __name__ == "__main__":
    unittest.main()
