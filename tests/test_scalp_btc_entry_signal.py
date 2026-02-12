import unittest

import pandas as pd

import indicators


class ScalpBtcEntrySignalTests(unittest.TestCase):
    def setUp(self):
        self._orig_get_ohlcv = indicators.pyupbit.get_ohlcv
        self._orig_get_rsi = indicators.get_rsi
        self._orig_get_ema = indicators.get_ema
        self._orig_debug_trade_flow = getattr(indicators.config, "DEBUG_TRADE_FLOW", False)

        self.main_df = self._build_main_df()
        self.minute1_df = self._build_minute1_df(low_values=[100.0, 101.0, 102.0])

        indicators.pyupbit.get_ohlcv = self._fake_get_ohlcv
        indicators.config.DEBUG_TRADE_FLOW = False

    def tearDown(self):
        indicators.pyupbit.get_ohlcv = self._orig_get_ohlcv
        indicators.get_rsi = self._orig_get_rsi
        indicators.get_ema = self._orig_get_ema
        indicators.config.DEBUG_TRADE_FLOW = self._orig_debug_trade_flow

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

    def _set_mock_rsi_ema(self, rsi_tail3, ema_last):
        rsi_vals = [45.0] * (len(self.main_df) - 3) + [float(v) for v in rsi_tail3]
        ema_vals = list(self.main_df["close"].astype(float))
        ema_vals[-1] = float(ema_last)
        indicators.get_rsi = lambda *_args, **_kwargs: pd.Series(rsi_vals)
        indicators.get_ema = lambda series, _period: pd.Series(ema_vals, index=series.index)

    def _fake_get_ohlcv(self, _ticker, interval="minute15", count=0):
        _ = count
        if str(interval) == "minute1":
            return self.minute1_df
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


if __name__ == "__main__":
    unittest.main()
