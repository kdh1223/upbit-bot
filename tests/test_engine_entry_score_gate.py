import datetime as dt
import unittest

import config
import engine_entry


_MISSING = object()


class MainEntryScoreGateTests(unittest.TestCase):
    def setUp(self):
        self._backup = {}
        self._set("ENTRY_SCORE_ENABLED", True)
        self._set("ENTRY_SCORE_REPLACE_MINUTE_OK", True)
        self._set("MAIN_MIN_ENTRY_SCORE", 2)
        self._set("ENTRY_SCORE_CACHE_SEC", 10)
        self._set("USE_INTRADAY_FILTER", False)
        self._set("USE_1M_CONFIRM_FOR_MAIN", False)

        self._orig_check_filters_with_reason = engine_entry.check_filters_with_reason
        self._orig_get_ohlcv = engine_entry.pyupbit.get_ohlcv
        engine_entry.check_filters_with_reason = lambda _ticker: (True, "OK")

    def tearDown(self):
        engine_entry.check_filters_with_reason = self._orig_check_filters_with_reason
        engine_entry.pyupbit.get_ohlcv = self._orig_get_ohlcv
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

    def test_blocks_when_score_is_below_threshold(self):
        now = dt.datetime(2026, 2, 12, 12, 0, 0)
        ticker = "KRW-TEST"
        minute_cache = {
            f"main_score::{ticker}": (1, ["EMA9_UP"], now),
        }

        ok = engine_entry.entry_passes_filters(
            ticker=ticker,
            now=now,
            day_cache={},
            intraday_cache={},
            minute_cache=minute_cache,
            main_mode="CONSERVATIVE",
        )

        self.assertFalse(ok)
        reason = minute_cache.get(f"main_reason::{ticker}")
        self.assertIsInstance(reason, tuple)
        self.assertTrue(str(reason[0]).startswith("ENTRY_SCORE_1/2"))

    def test_pass_clears_stale_reason(self):
        now = dt.datetime(2026, 2, 12, 12, 0, 0)
        ticker = "KRW-TEST"
        minute_cache = {
            f"main_score::{ticker}": (2, ["EMA9_UP", "VOL_SPIKE"], now),
            f"main_reason::{ticker}": ("ENTRY_SCORE_1/2:EMA9_UP", now),
        }

        ok = engine_entry.entry_passes_filters(
            ticker=ticker,
            now=now,
            day_cache={},
            intraday_cache={},
            minute_cache=minute_cache,
            main_mode="CONSERVATIVE",
        )

        self.assertTrue(ok)
        self.assertNotIn(f"main_reason::{ticker}", minute_cache)

    def test_calc_error_blocks_without_silent_pass(self):
        now = dt.datetime(2026, 2, 12, 12, 0, 0)
        ticker = "KRW-TEST"
        engine_entry.pyupbit.get_ohlcv = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom"))

        minute_cache = {}
        ok = engine_entry.entry_passes_filters(
            ticker=ticker,
            now=now,
            day_cache={},
            intraday_cache={},
            minute_cache=minute_cache,
            main_mode="CONSERVATIVE",
        )

        self.assertFalse(ok)
        reason = minute_cache.get(f"main_reason::{ticker}")
        self.assertEqual(reason[0], "ENTRY_SCORE_CALC_ERR")


if __name__ == "__main__":
    unittest.main()

