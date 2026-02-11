import datetime as dt
import unittest
from zoneinfo import ZoneInfo

from run_daily_report import (
    _calc_mdd_from_equity_points,
    build_0900_mini_report_text,
    build_heartbeat_text,
    build_metrics,
    build_month_end_report_block,
    build_report_text,
    report_window_21_to_21,
)


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

    def test_build_metrics_ignores_partial_reasons(self):
        rows = [
            {"pnl_pct": "3.0", "reason": "TP1"},
            {"pnl_pct": "2.0", "reason": "TP2_PARTIAL"},
            {"pnl_pct": "-1.0", "reason": "RUNNER_TIMEOUT"},
        ]
        m = build_metrics(rows)
        self.assertEqual(m["n"], 1)
        self.assertAlmostEqual(m["avg"], -1.0, places=6)

    def test_build_metrics_repairs_legacy_minus_100_pnl_from_prices(self):
        rows = [
            {"pnl_pct": "-100.0", "entry_price": "3316", "exit_price": "3277", "reason": "STOPLOSS"},
            {"pnl_pct": "-0.93", "entry_price": "3557", "exit_price": "3548", "reason": "STOPLOSS"},
            {"pnl_pct": "-1.18", "entry_price": "3316", "exit_price": "3277", "reason": "STOPLOSS"},
        ]
        m = build_metrics(rows)
        self.assertEqual(m["n"], 3)
        self.assertGreater(m["avg"], -10.0)
        self.assertGreater(m["min"], -10.0)

    def test_build_metrics_repairs_large_mismatch_pnl_from_prices(self):
        rows = [
            {"pnl_pct": "-49.30", "entry_price": "28.50", "exit_price": "28.90", "reason": "FORCE_CLOSE"},
            {"pnl_pct": "-1.03", "entry_price": "29.10", "exit_price": "29.10", "reason": "STOPLOSS"},
        ]
        m = build_metrics(rows)
        self.assertEqual(m["n"], 2)
        self.assertGreater(m["avg"], -5.0)

    def test_snapshot_mdd_is_negative(self):
        points = [
            (dt.datetime(2026, 2, 1, 21, 0, 0, tzinfo=KST), 1_000_000.0),
            (dt.datetime(2026, 2, 2, 21, 0, 0, tzinfo=KST), 1_100_000.0),
            (dt.datetime(2026, 2, 3, 21, 0, 0, tzinfo=KST), 1_000_000.0),
        ]
        mdd = _calc_mdd_from_equity_points(points)
        self.assertLess(mdd, 0.0)

    def test_snapshot_mdd_none_when_insufficient_points(self):
        points = [(dt.datetime(2026, 2, 1, 21, 0, 0, tzinfo=KST), 1_000_000.0)]
        self.assertIsNone(_calc_mdd_from_equity_points(points))

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
            status_emoji="\U0001F7E1",
        )
        self.assertIn("2026-02-10 21:00", text)
        self.assertIn("02/09 21:00 ~ 02/10 21:00", text)
        self.assertIn("- \uC77C\uC77C \uC2E4\uD604\uC190\uC775: +1,000\uC6D0 (+1.00%)", text)
        self.assertIn("- \uC6D4\uAC04 MDD: -1.23%", text)
        self.assertIn("- SCALP_BTC: \uAC70\uB798 3 | \uC2B9\uB960 66.60% | \uD3C9\uADE0 +0.50%", text)
        self.assertIn("\uC0C1\uD0DC: \U0001F7E1 CAUTION", text)

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
            status_emoji="\U0001F7E2",
        )
        self.assertIn("- \uC6D4\uAC04 MDD: N/A", text)

    def test_heartbeat_includes_entry_guard_line(self):
        now = dt.datetime(2026, 2, 10, 9, 5, 0, tzinfo=KST)
        text = build_heartbeat_text(
            now=now,
            service_status="\uC2E4\uD589\uC911",
            asset_text="100,000 KRW",
            main_holding_cnt=0,
            scalp_holding_cnt=0,
            risk_state={"halted_flag": False, "halt_reason": ""},
        )
        self.assertIn("ACTIVE", text)

    def test_build_0900_mini_report_text_normal(self):
        text = build_0900_mini_report_text(
            regime="FULL",
            auto_mode="AGGRESSIVE",
            holding_cnt=1,
            max_holdings=2,
            equity_krw=97_512.0,
            daily_pct=-2.49,
            month_mdd_pct=-30.01,
            guard_active=True,
            risk_state={"halted_flag": False, "halt_reason": ""},
        )
        self.assertIn("09:00", text)
        self.assertIn("레짐: FULL | AUTO: AGGRESSIVE", text)
        self.assertIn("보유: 1 / 2", text)
        self.assertIn("신규진입가드: ACTIVE", text)

    def test_build_0900_mini_report_text_halted(self):
        text = build_0900_mini_report_text(
            regime="FULL",
            auto_mode="CONSERVATIVE",
            holding_cnt=0,
            max_holdings=2,
            equity_krw=100_000.0,
            daily_pct=0.0,
            month_mdd_pct=-5.0,
            guard_active=False,
            risk_state={"halted_flag": True, "halt_reason": "TOTAL_MDD_LIMIT"},
        )
        self.assertIn("상태: ⛔ HALTED (TOTAL_MDD_LIMIT)", text)
        self.assertNotIn("신규진입가드", text)

    def test_build_month_end_report_block(self):
        report_end = dt.datetime(2026, 2, 28, 21, 0, 0, tzinfo=KST)
        text = build_month_end_report_block(
            report_end=report_end,
            month_metrics={"n": 9, "wr": 44.44, "avg": -5.21, "cum": -7.82},
            by_strategy={
                "MAIN": {"n": 8, "wr": 50.0, "avg": -5.75},
                "SCALP_BTC": {"n": 1, "wr": 0.0, "avg": -2.1},
            },
            month_mdd_pct=-30.01,
            month_start_equity=100_000.0,
            month_end_equity=97_512.0,
        )
        self.assertIn("월간 최종 성과 (2026-02)", text)
        self.assertIn("전략별 월간 요약", text)
        self.assertIn("SCALP_BTC: 거래 1 | 승률 0.00% | 평균 -2.10%", text)
        self.assertIn("순증가: -2,488원 (-2.49%)", text)


if __name__ == "__main__":
    unittest.main()
