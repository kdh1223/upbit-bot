import unittest

import pandas as pd

import indicators


class ScalpBtcEntrySignalTests(unittest.TestCase):
    def setUp(self):
        self._orig_get_ohlcv = indicators.pyupbit.get_ohlcv
        self._orig_get_rsi = indicators.get_rsi
        self._orig_get_ema = indicators.get_ema
        self._orig_debug_trade_flow = getattr(indicators.config, "DEBUG_TRADE_FLOW", False)
        self._orig_regime_filter_on = getattr(indicators.config, "SCALP_BTC_REGIME_FILTER_ON", False)
        self._orig_regime_tf = getattr(indicators.config, "SCALP_BTC_REGIME_TF", "minute240")
        self._orig_regime_ma_fast = getattr(indicators.config, "SCALP_BTC_REGIME_MA_FAST", 20)
        self._orig_regime_ma_slow = getattr(indicators.config, "SCALP_BTC_REGIME_MA_SLOW", 60)
        self._orig_regime_mode = getattr(indicators.config, "SCALP_BTC_REGIME_MODE", "BULL_ONLY")
        self._orig_regime_cache_sec = getattr(indicators.config, "SCALP_BTC_REGIME_CACHE_SEC", 300)
        self._orig_regime_cache = dict(getattr(indicators, "_SCALP_BTC_REGIME_CACHE", {}))

        self.main_df = self._build_main_df()
        self.minute1_df = self._build_minute1_df(low_values=[100.0, 101.0, 102.0])
        self.regime_df = self._build_regime_df(bull=True)
        self.call_counts = {}

        indicators.pyupbit.get_ohlcv = self._fake_get_ohlcv
        indicators.config.DEBUG_TRADE_FLOW = False
        indicators.config.SCALP_BTC_REGIME_FILTER_ON = False
        indicators.config.SCALP_BTC_REGIME_TF = "minute240"
        indicators.config.SCALP_BTC_REGIME_MA_FAST = 20
        indicators.config.SCALP_BTC_REGIME_MA_SLOW = 60
        indicators.config.SCALP_BTC_REGIME_MODE = "BULL_ONLY"
        indicators.config.SCALP_BTC_REGIME_CACHE_SEC = 300
        indicators._SCALP_BTC_REGIME_CACHE.clear()

    def tearDown(self):
        indicators.pyupbit.get_ohlcv = self._orig_get_ohlcv
        indicators.get_rsi = self._orig_get_rsi
        indicators.get_ema = self._orig_get_ema
        indicators.config.DEBUG_TRADE_FLOW = self._orig_debug_trade_flow
        indicators.config.SCALP_BTC_REGIME_FILTER_ON = self._orig_regime_filter_on
        indicators.config.SCALP_BTC_REGIME_TF = self._orig_regime_tf
        indicators.config.SCALP_BTC_REGIME_MA_FAST = self._orig_regime_ma_fast
        indicators.config.SCALP_BTC_REGIME_MA_SLOW = self._orig_regime_ma_slow
        indicators.config.SCALP_BTC_REGIME_MODE = self._orig_regime_mode
        indicators.config.SCALP_BTC_REGIME_CACHE_SEC = self._orig_regime_cache_sec
        indicators._SCALP_BTC_REGIME_CACHE.clear()
        indicators._SCALP_BTC_REGIME_CACHE.update(self._orig_regime_cache)

    @staticmethod
    def _build_main_df():
        bars = 60
        close = [100.0 + (0.2 * i) for i in range(bars)]
        close[-2] = 110.0
        close[-1] = 112.0
        high = [c + 1.0 for c in close]
        low = [c - 1.0 for c in close]
        volume = [100.0 for _ in range(bars)]
        volume[-1] = 260.0
        return pd.DataFrame(
            {
                "open": close,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
            }
        )

    @staticmethod
    def _build_minute1_df(low_values):
        lows = [float(v) for v in low_values]
        return pd.DataFrame({"low": lows})

    @staticmethod
    def _build_regime_df(bull=True):
        bars = 130
        if bull:
            close = [10000.0 + (10.0 * i) for i in range(bars)]
        else:
            close = [10000.0 - (10.0 * i) for i in range(bars)]
        return pd.DataFrame({"close": close})

    def _set_mock_rsi_ema(self, rsi_tail3, ema_last):
        rsi_vals = [45.0] * (len(self.main_df) - 3) + [float(v) for v in rsi_tail3]
        ema_vals = list(self.main_df["close"].astype(float))
        ema_vals[-1] = float(ema_last)
        indicators.get_rsi = lambda *_args, **_kwargs: pd.Series(rsi_vals)
        indicators.get_ema = lambda series, _period: pd.Series(ema_vals, index=series.index)

    def _fake_get_ohlcv(self, _ticker, interval="minute15", count=0):
        _ = count
        key = str(interval)
        self.call_counts[key] = self.call_counts.get(key, 0) + 1
        if key == "minute1":
            return self.minute1_df
        if key == "minute240":
            return self.regime_df
        return self.main_df

    def test_enters_on_rsi_min3_oversold_even_if_rsi_now_is_above_oversold(self):
        self._set_mock_rsi_ema(rsi_tail3=[27.0, 29.0, 31.0], ema_last=111.0)
        ok = indicators.scalp_btc_entry_signal("KRW-BTC")
        self.assertTrue(ok)

    def test_blocks_when_minute1_low_is_not_higher_than_previous_low(self):
        self._set_mock_rsi_ema(rsi_tail3=[27.0, 29.0, 31.0], ema_last=111.0)
        self.minute1_df = self._build_minute1_df(low_values=[102.0, 101.0, 100.0])
        ok = indicators.scalp_btc_entry_signal("KRW-BTC")
        self.assertFalse(ok)

    def test_blocks_when_rsi_series_has_less_than_three_bars(self):
        indicators.get_rsi = lambda *_args, **_kwargs: pd.Series([28.0, 29.0])
        indicators.get_ema = lambda series, _period: pd.Series([110.0] * len(series), index=series.index)
        ok = indicators.scalp_btc_entry_signal("KRW-BTC")
        self.assertFalse(ok)

    def test_blocks_when_regime_filter_enabled_and_not_bull(self):
        indicators.config.SCALP_BTC_REGIME_FILTER_ON = True
        self.regime_df = self._build_regime_df(bull=False)
        self._set_mock_rsi_ema(rsi_tail3=[27.0, 29.0, 31.0], ema_last=111.0)
        ok = indicators.scalp_btc_entry_signal("KRW-BTC")
        self.assertFalse(ok)

    def test_reuses_regime_cache_within_cache_window(self):
        indicators.config.SCALP_BTC_REGIME_FILTER_ON = True
        indicators.config.SCALP_BTC_REGIME_CACHE_SEC = 300
        self.regime_df = self._build_regime_df(bull=True)
        self._set_mock_rsi_ema(rsi_tail3=[27.0, 29.0, 31.0], ema_last=111.0)
        ok1 = indicators.scalp_btc_entry_signal("KRW-BTC")
        ok2 = indicators.scalp_btc_entry_signal("KRW-BTC")
        self.assertTrue(ok1)
        self.assertTrue(ok2)
        self.assertEqual(self.call_counts.get("minute240", 0), 1)


if __name__ == "__main__":
    unittest.main()
