import datetime as dt
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

import config
from run_daily_report import _mark_schedule_sent_key, _mark_scheduled_sent, _should_append_month_end_block, _should_send_scheduled


KST = ZoneInfo("Asia/Seoul")
UTC = dt.timezone.utc
_MISSING = object()


class DailyReportScheduleTests(unittest.TestCase):
    def setUp(self):
        self._backup = {}

    def tearDown(self):
        for name, original in self._backup.items():
            if original is _MISSING:
                try:
                    delattr(config, name)
                except AttributeError:
                    pass
            else:
                setattr(config, name, original)

    def _set(self, name, value):
        if name not in self._backup:
            self._backup[name] = getattr(config, name, _MISSING)
        setattr(config, name, value)

    def test_blocks_send_outside_kst_window(self):
        logs = []
        with TemporaryDirectory() as td:
            self._set("REPORT_SCHEDULE_STATE_FILE", str(Path(td) / "schedule_state.json"))
            now = dt.datetime(2026, 2, 11, 6, 0, 0, tzinfo=KST)
            ok, _ = _should_send_scheduled(
                now=now,
                task_key="daily_report",
                target_hour=21,
                target_min=0,
                window_min=30,
                force=False,
                log_line=logs.append,
            )
        self.assertFalse(ok)
        self.assertTrue(any("outside KST window" in line for line in logs))

    def test_blocks_send_before_target_even_within_legacy_pre_window(self):
        logs = []
        with TemporaryDirectory() as td:
            self._set("REPORT_SCHEDULE_STATE_FILE", str(Path(td) / "schedule_state.json"))
            now = dt.datetime(2026, 2, 11, 20, 45, 0, tzinfo=KST)
            ok, _ = _should_send_scheduled(
                now=now,
                task_key="daily_report",
                target_hour=21,
                target_min=0,
                window_min=30,
                force=False,
                log_line=logs.append,
            )
        self.assertFalse(ok)
        self.assertTrue(any("before target" in line for line in logs))

    def test_allows_once_and_dedupes_same_kst_day(self):
        logs = []
        with TemporaryDirectory() as td:
            self._set("REPORT_SCHEDULE_STATE_FILE", str(Path(td) / "schedule_state.json"))
            now = dt.datetime(2026, 2, 11, 21, 5, 0, tzinfo=KST)
            ok1, target = _should_send_scheduled(
                now=now,
                task_key="daily_report",
                target_hour=21,
                target_min=0,
                window_min=30,
                force=False,
                log_line=logs.append,
            )
            self.assertTrue(ok1)
            _mark_scheduled_sent("daily_report", target, logs.append)

            ok2, _ = _should_send_scheduled(
                now=now + dt.timedelta(minutes=3),
                task_key="daily_report",
                target_hour=21,
                target_min=0,
                window_min=30,
                force=False,
                log_line=logs.append,
            )
        self.assertFalse(ok2)
        self.assertTrue(any("already sent" in line for line in logs))

    def test_utc_input_is_converted_to_kst(self):
        logs = []
        with TemporaryDirectory() as td:
            self._set("REPORT_SCHEDULE_STATE_FILE", str(Path(td) / "schedule_state.json"))
            now_utc = dt.datetime(2026, 2, 11, 0, 5, 0, tzinfo=UTC)  # 09:05 KST
            ok, _ = _should_send_scheduled(
                now=now_utc,
                task_key="heartbeat",
                target_hour=9,
                target_min=0,
                window_min=10,
                force=False,
                log_line=logs.append,
            )
        self.assertTrue(ok)

    def test_month_end_report_is_last_day_only(self):
        logs = []
        with TemporaryDirectory() as td:
            self._set("REPORT_SCHEDULE_STATE_FILE", str(Path(td) / "schedule_state.json"))
            not_last_day = dt.datetime(2026, 2, 27, 21, 0, 0, tzinfo=KST)
            ok, _ = _should_append_month_end_block(not_last_day, logs.append)
        self.assertFalse(ok)

    def test_month_end_report_dedupes_by_month(self):
        logs = []
        with TemporaryDirectory() as td:
            self._set("REPORT_SCHEDULE_STATE_FILE", str(Path(td) / "schedule_state.json"))
            month_end = dt.datetime(2026, 2, 28, 21, 0, 0, tzinfo=KST)
            ok1, key1 = _should_append_month_end_block(month_end, logs.append)
            self.assertTrue(ok1)
            self.assertEqual(key1, "2026-02")
            _mark_schedule_sent_key("month_end_report", key1, logs.append)
            ok2, _ = _should_append_month_end_block(month_end + dt.timedelta(minutes=5), logs.append)
        self.assertFalse(ok2)
        self.assertTrue(any("already sent" in line for line in logs))


if __name__ == "__main__":
    unittest.main()
