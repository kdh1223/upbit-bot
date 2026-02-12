import datetime as dt
import unittest

import config
import engine_entry


_MISSING = object()


class MainConfirm1mTests(unittest.TestCase):
    def setUp(self):
        self._backup = {}
        self._set("USE_INTRADAY_FILTER", False)
        self._set("USE_1M_CONFIRM_FOR_MAIN", True)
        self._set("MAIN_CONFIRM_1M_CACHE_SEC", 5)
        self._set("DEBUG_ENTRY_REJECT", False)
        self._set("ENTRY_SCORE_ENABLED", False)
        self._set("ENTRY_SCORE_REPLACE_MINUTE_OK", False)

        self._orig_check_filters = engine_entry.check_filters
        self._orig_check_filters_with_reason = engine_entry.check_filters_with_reason
        self._orig_minute_entry_ok = engine_entry.minute_entry_ok
        self._orig_main_confirm = engine_entry._main_1m_confirm_ok

        engine_entry.check_filters = lambda _ticker: True
        engine_entry.check_filters_with_reason = lambda _ticker: (True, "OK")
        engine_entry.minute_entry_ok = lambda _ticker: True

    def tearDown(self):
        for name, original in self._backup.items():
            if original is _MISSING:
                try:
                    delattr(config, name)
                except AttributeError:
                    pass
            else:
                setattr(config, name, original)

        engine_entry.check_filters = self._orig_check_filters
        engine_entry.check_filters_with_reason = self._orig_check_filters_with_reason
        engine_entry.minute_entry_ok = self._orig_minute_entry_ok
        engine_entry._main_1m_confirm_ok = self._orig_main_confirm

    def _set(self, name, value):
        if name not in self._backup:
            self._backup[name] = getattr(config, name, _MISSING)
        setattr(config, name, value)

    def test_conservative_blocks_on_1m_confirm_reject(self):
        engine_entry._main_1m_confirm_ok = lambda _ticker: (False, "RSI_DELTA_LOW")
        now = dt.datetime(2026, 2, 10, 12, 0, 0)
        ok = engine_entry.entry_passes_filters("KRW-TEST", now, {}, {}, {}, main_mode="CONSERVATIVE")
        self.assertFalse(ok)

    def test_aggressive_skips_1m_confirm(self):
        engine_entry._main_1m_confirm_ok = lambda _ticker: (False, "RSI_DELTA_LOW")
        now = dt.datetime(2026, 2, 10, 12, 0, 0)
        ok = engine_entry.entry_passes_filters("KRW-TEST", now, {}, {}, {}, main_mode="AGGRESSIVE")
        self.assertTrue(ok)

    def test_1m_confirm_fetch_error_is_true_fallback(self):
        orig = engine_entry.pyupbit.get_ohlcv
        try:
            engine_entry.pyupbit.get_ohlcv = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("x"))
            ok, reason = engine_entry._main_1m_confirm_ok("KRW-TEST")
            self.assertTrue(ok)
            self.assertEqual(reason, "FETCH_ERROR")
        finally:
            engine_entry.pyupbit.get_ohlcv = orig


if __name__ == "__main__":
    unittest.main()
