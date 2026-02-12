import datetime as dt
import unittest

import pandas as pd

import config
import engine_entry


_MISSING = object()


class DayMaBypassTests(unittest.TestCase):
    def setUp(self):
        self._backup = {}
        self._set("ENTRY_SCORE_ENABLED", True)
        self._set("ENTRY_SCORE_REPLACE_MINUTE_OK", True)
        self._set("MAIN_MIN_ENTRY_SCORE", 2)
        self._set("USE_INTRADAY_FILTER", False)
        self._set("USE_1M_CONFIRM_FOR_MAIN", False)
        self._set("DAY_MA_SOFT_BYPASS_ENABLED", True)
        self._set("DAY_MA_BYPASS_REQUIRE_ENTRY_SCORE", 3)
        self._set("DAY_MA_BYPASS_REQUIRE_H4_TREND", True)
        self._set("DAY_MA_BYPASS_REQUIRE_VOL_OK", True)
        self._set("DAY_MA_BYPASS_COOLDOWN_MIN", 60)
        self._set("DAY_MA_BYPASS_LOG", False)

        self._orig_check_filters_with_reason = engine_entry.check_filters_with_reason
        self._orig_get_ohlcv = engine_entry.pyupbit.get_ohlcv

        self.day_ok = False
        self.day_reason = "DAY_MA_FAIL"
        engine_entry.check_filters_with_reason = self._mock_check_filters
        engine_entry.pyupbit.get_ohlcv = self._mock_get_ohlcv

        self.df4h = self._build_df4h()
        self.df1m_vol_ok = self._build_df1m_vol_ok()

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

    def _mock_check_filters(self, _ticker):
        return bool(self.day_ok), str(self.day_reason)

    def _mock_get_ohlcv(self, _ticker, interval="minute1", count=0):
        _ = count
        if str(interval) == "minute240":
            return self.df4h
        return self.df1m_vol_ok

    @staticmethod
    def _build_df4h():
        closes = [100.0 + (0.4 * i) for i in range(90)]
        return pd.DataFrame({"close": closes})

    @staticmethod
    def _build_df1m_vol_ok():
        n = 60
        closes = [100.0 + (0.02 * i) for i in range(n)]
        highs = [c + 0.1 for c in closes]
        lows = [c - 0.1 for c in closes]
        volumes = [100.0 for _ in range(n)]
        volumes[-1] = 220.0
        return pd.DataFrame(
            {
                "open": closes,
                "high": highs,
                "low": lows,
                "close": closes,
                "volume": volumes,
            }
        )

    def test_day_ma_fail_bypass_success_with_strict_conditions(self):
        now = dt.datetime(2026, 2, 12, 16, 0, 0)
        ticker = "KRW-TEST"
        minute_cache = {
            f"main_score::{ticker}": (3, ["RSI_UP", "EMA9_UP", "VOL_SPIKE"], now),
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
        self.assertIn(f"day_ma_bypass::{ticker}", minute_cache)
        self.assertNotIn(f"main_reason::{ticker}", minute_cache)

    def test_non_day_ma_fail_is_not_bypassed(self):
        now = dt.datetime(2026, 2, 12, 16, 0, 0)
        ticker = "KRW-TEST"
        self.day_ok = False
        self.day_reason = "DAY_RSI_FAIL"
        minute_cache = {
            f"main_score::{ticker}": (5, ["RSI_UP", "CLOSE_GT_EMA9", "BREAK_PREV_HIGH", "EMA9_UP", "VOL_SPIKE"], now),
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
        self.assertEqual(reason[0], "DAY_RSI_FAIL")

    def test_day_ma_bypass_cooldown_blocks_reentry(self):
        now = dt.datetime(2026, 2, 12, 16, 0, 0)
        ticker = "KRW-TEST"
        minute_cache = {
            f"main_score::{ticker}": (4, ["RSI_UP", "CLOSE_GT_EMA9", "EMA9_UP", "VOL_SPIKE"], now),
            f"day_ma_bypass::{ticker}": now - dt.timedelta(minutes=30),
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
        self.assertEqual(reason[0], "DAY_MA_FAIL")

    def test_day_ma_bypass_reuses_cached_score_without_minute_fetch(self):
        now = dt.datetime(2026, 2, 12, 16, 0, 0)
        ticker = "KRW-TEST"
        self._set("DAY_MA_BYPASS_REQUIRE_H4_TREND", False)
        self._set("DAY_MA_BYPASS_REQUIRE_VOL_OK", False)
        engine_entry.pyupbit.get_ohlcv = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("should_not_fetch"))
        minute_cache = {
            f"main_score::{ticker}": (3, ["RSI_UP", "EMA9_UP", "VOL_SPIKE"], now),
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


if __name__ == "__main__":
    unittest.main()

