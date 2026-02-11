import datetime as dt
import unittest

import pandas as pd

import config
import engine_entry


_MISSING = object()


class MainSurgeGuardTests(unittest.TestCase):
    def setUp(self):
        self._backup = {}
        self._set("SURGE_EXTRA_GUARD_ENABLED", True)
        self._set("SURGE_EXTRA_GUARD_EXEMPT_TICKERS", {"KRW-BTC", "KRW-ETH", "KRW-XRP"})
        self._set("SURGE_EXTRA_GUARD_1M_MAX_PCT", 3.0)
        self._set("SURGE_EXTRA_GUARD_5M_MAX_PCT", 10.0)
        self._set("SURGE_EXTRA_GUARD_BODY_MAX_PCT", 2.5)
        self._set("SURGE_EXTRA_GUARD_CACHE_SEC", 0.0)

        self._orig_get_ohlcv = engine_entry.pyupbit.get_ohlcv

    def tearDown(self):
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

    @staticmethod
    def _ohlcv_from_closes(closes):
        opens = [float(c) for c in closes]
        # keep candle body near 0 unless test intentionally targets body condition
        return pd.DataFrame({"open": opens, "close": [float(c) for c in closes]})

    def test_exempt_ticker_skips_guard(self):
        def _boom(*_args, **_kwargs):
            raise RuntimeError("should not be called for exempt ticker")

        engine_entry.pyupbit.get_ohlcv = _boom
        ok, reason = engine_entry._main_surge_extra_guard_ok(
            "KRW-BTC",
            dt.datetime(2026, 2, 11, 12, 0, 0),
            {},
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "EXEMPT")

    def test_blocks_non_core_when_1m_spike_is_too_large(self):
        engine_entry.pyupbit.get_ohlcv = lambda *_args, **_kwargs: self._ohlcv_from_closes(
            [100, 100, 100, 100, 100, 104]
        )
        ok, reason = engine_entry._main_surge_extra_guard_ok(
            "KRW-ALT",
            dt.datetime(2026, 2, 11, 12, 0, 0),
            {},
        )
        self.assertFalse(ok)
        self.assertTrue(reason.startswith("SURGE_GUARD_1M"))

    def test_blocks_non_core_when_5m_spike_is_too_large(self):
        engine_entry.pyupbit.get_ohlcv = lambda *_args, **_kwargs: self._ohlcv_from_closes(
            [100, 102, 104, 106, 108, 111]
        )
        ok, reason = engine_entry._main_surge_extra_guard_ok(
            "KRW-ALT",
            dt.datetime(2026, 2, 11, 12, 0, 0),
            {},
        )
        self.assertFalse(ok)
        self.assertTrue(reason.startswith("SURGE_GUARD_5M"))

    def test_allows_non_core_when_move_is_stable(self):
        engine_entry.pyupbit.get_ohlcv = lambda *_args, **_kwargs: self._ohlcv_from_closes(
            [100, 100.2, 100.4, 100.8, 101.0, 101.3]
        )
        ok, reason = engine_entry._main_surge_extra_guard_ok(
            "KRW-ALT",
            dt.datetime(2026, 2, 11, 12, 0, 0),
            {},
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "OK")


if __name__ == "__main__":
    unittest.main()

