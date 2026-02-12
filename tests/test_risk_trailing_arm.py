import unittest
from unittest.mock import patch

import pandas as pd

import config
import risk
from risk import apply_risk_rules


_MISSING = object()


class RiskTrailingArmTests(unittest.TestCase):
    def setUp(self):
        self._backup = {}
        self._set("REAL_ORDER", False)
        self._set("MIN_ORDER_KRW", 1)
        self._set("STOP_LOSS_MODE", "FIXED")
        self._set("STOP_LOSS_PCT", 0.01)
        self._set("TRAIL_ARM_SEC", 120)
        self._set("TRAIL_ARM_PCT", 0.5)
        self._set("TRAIL_DRAWDOWN_PCT", 0.6)
        self._set("DEBUG_TRADE_FLOW", False)
        self._set("TP1_SELL_RATIO", 0.5)
        self._set("TP2_SELL_RATIO", 0.5)
        self._set(
            "MAIN_TP_RATIOS",
            {
                "CONSERVATIVE": {"TP1": 0.60, "TP2": 0.30, "RUNNER": 0.10},
                "AGGRESSIVE": {"TP1": 0.40, "TP2": 0.40, "RUNNER": 0.20},
            },
        )
        self._set("MAIN_RUNNER_TRAIL_GIVEBACK_PCT", 0.007)
        self._set("MAIN_RUNNER_MAX_HOLD_MIN", 120)
        self._set("MAIN_RUNNER_TIMEOUT_CLOSE_IF_PNL_GE", 0.0)
        self._set("MAIN_OB_FVG_TP_ADJUST_ENABLED", False)
        self._set("MAIN_OB_FVG_RESIST_NEAR_PCT", 0.0025)
        self._set("MAIN_OB_FVG_TP1_MIN_PNL_PCT", 0.0035)
        self._set("MAIN_OB_FVG_TP1_HIT_RATIO", 0.75)
        self._set("MAIN_OB_FVG_RUNNER_TIGHTEN_FACTOR", 2.0 / 3.0)
        self._set("MAIN_OB_FVG_RUNNER_MIN_TRAIL_PCT", 0.003)
        self._set("MAIN_OB_FVG_CACHE_SEC", 0.0)
        self._set(
            "TP_TABLE",
            {
                "LOW": {"TP1_PCT": 0.0, "TP2_PCT": 0.0, "TRAIL_BACK_PCT": 0.006},
                "MID": {"TP1_PCT": 0.0, "TP2_PCT": 0.0, "TRAIL_BACK_PCT": 0.006},
                "FULL": {"TP1_PCT": 0.0, "TP2_PCT": 0.0, "TRAIL_BACK_PCT": 0.006},
                "HALT": {"TP1_PCT": 0.0, "TP2_PCT": 0.0, "TRAIL_BACK_PCT": 0.0},
            },
        )
        risk._MAIN_OB_FVG_CACHE.clear()

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

    @staticmethod
    def _mock_sell(*_args, **_kwargs):
        return True

    @staticmethod
    def _base_state(entry_ts=1_000.0, strategy_tag="MAIN"):
        return {
            "holding": True,
            "entry": 100.0,
            "peak": 100.0,
            "entry_ts": float(entry_ts),
            "trail_armed": False,
            "trail_hwm": 0.0,
            "tp1": False,
            "tp2": False,
            "tp1_done": False,
            "tp2_done": False,
            "runner_active": False,
            "runner_hwm": 0.0,
            "runner_start_ts": 0.0,
            "tp1_ratio": 0.60,
            "tp2_ratio": 0.30,
            "runner_ratio": 0.10,
            "entry_mode": "CONSERVATIVE",
            "regime": "MID",
            "initial_volume": 1.0,
            "invested_krw": 100.0,
            "target_krw": 200.0,
            "add_count": 1,
            "realized_krw": 0.0,
            "realized_cost_krw": 0.0,
            "total_buy_krw": 100.0,
            "total_sell_krw": 0.0,
            "last_exit_reason": "",
            "strategy_tag": str(strategy_tag or "MAIN").upper(),
        }

    @staticmethod
    def _main_ob_fvg_df(resistance_level: float):
        bars = 40
        base = float(resistance_level) - 1.0
        open_vals = [base for _ in range(bars)]
        close_vals = [base for _ in range(bars)]
        high_vals = [base + 0.2 for _ in range(bars)]
        low_vals = [base - 0.2 for _ in range(bars)]
        vol_vals = [140.0 for _ in range(bars)]

        # Bearish FVG near current price:
        # candle1 low > candle3 high => use candle(-3) and candle(-1).
        open_vals[-3] = resistance_level + 0.2
        close_vals[-3] = resistance_level + 0.1
        high_vals[-3] = resistance_level + 0.3
        low_vals[-3] = resistance_level

        open_vals[-2] = resistance_level - 0.05
        close_vals[-2] = resistance_level - 0.25
        high_vals[-2] = resistance_level
        low_vals[-2] = resistance_level - 0.3

        open_vals[-1] = resistance_level - 0.15
        close_vals[-1] = resistance_level - 0.35
        high_vals[-1] = resistance_level - 0.25
        low_vals[-1] = resistance_level - 0.5

        vol_vals[-2] = 150.0
        vol_vals[-1] = 80.0

        return pd.DataFrame(
            {
                "open": open_vals,
                "high": high_vals,
                "low": low_vals,
                "close": close_vals,
                "volume": vol_vals,
            }
        )

    def test_no_trailing_close_before_arm_window(self):
        state = self._base_state(entry_ts=1_000.0, strategy_tag="SCALP")

        r1 = apply_risk_rules(None, "KRW-TEST", state, 99.7, self._mock_sell, now=1_040.0, strategy_tag="SCALP")
        r2 = apply_risk_rules(None, "KRW-TEST", state, 99.1, self._mock_sell, now=1_110.0, strategy_tag="SCALP")

        self.assertFalse(r1.get("closed", False))
        self.assertFalse(r2.get("closed", False))
        self.assertNotEqual(r1.get("reason"), "trailing")
        self.assertNotEqual(r2.get("reason"), "trailing")
        self.assertFalse(state.get("trail_armed", False))
        self.assertEqual(float(state.get("trail_hwm", 0.0)), 0.0)

    def test_trailing_arms_and_closes_after_drawdown(self):
        state = self._base_state(entry_ts=1_000.0, strategy_tag="SCALP")

        r1 = apply_risk_rules(None, "KRW-TEST", state, 100.6, self._mock_sell, now=1_120.0, strategy_tag="SCALP")
        self.assertFalse(r1.get("closed", False))
        self.assertTrue(state.get("trail_armed", False))
        self.assertAlmostEqual(float(state.get("trail_hwm", 0.0)), 100.6, places=8)

        r2 = apply_risk_rules(None, "KRW-TEST", state, 101.2, self._mock_sell, now=1_130.0, strategy_tag="SCALP")
        self.assertFalse(r2.get("closed", False))
        self.assertAlmostEqual(float(state.get("trail_hwm", 0.0)), 101.2, places=8)

        r3 = apply_risk_rules(None, "KRW-TEST", state, 100.59, self._mock_sell, now=1_140.0, strategy_tag="SCALP")
        self.assertTrue(r3.get("closed", False))
        self.assertEqual(r3.get("reason"), "trailing")

    def test_stoploss_reason_in_arm_window(self):
        state = self._base_state(entry_ts=1_000.0)
        state["strategy_tag"] = "SCALP"

        result = apply_risk_rules(None, "KRW-TEST", state, 98.9, self._mock_sell, now=1_060.0)

        self.assertTrue(result.get("closed", False))
        self.assertEqual(result.get("reason"), "stoploss")
        self.assertNotEqual(result.get("reason"), "trailing")

    def test_partial_take_profit_updates_total_sell_only(self):
        self._set(
            "TP_TABLE",
            {
                "LOW": {"TP1_PCT": 0.01, "TP2_PCT": 0.0, "TRAIL_BACK_PCT": 0.006},
                "MID": {"TP1_PCT": 0.01, "TP2_PCT": 0.0, "TRAIL_BACK_PCT": 0.006},
                "FULL": {"TP1_PCT": 0.01, "TP2_PCT": 0.0, "TRAIL_BACK_PCT": 0.006},
                "HALT": {"TP1_PCT": 0.0, "TP2_PCT": 0.0, "TRAIL_BACK_PCT": 0.0},
            },
        )
        state = self._base_state(entry_ts=1_000.0, strategy_tag="SCALP")

        result = apply_risk_rules(None, "KRW-TEST", state, 101.0, self._mock_sell, now=1_130.0, strategy_tag="SCALP")

        self.assertFalse(result.get("closed", False))
        self.assertTrue(state.get("tp1", False))
        self.assertEqual(state.get("last_exit_reason", ""), "")
        self.assertAlmostEqual(float(state.get("total_sell_krw", 0.0)), 50.5, places=8)

    def test_full_close_sets_last_exit_reason_and_total_sell(self):
        state = self._base_state(entry_ts=1_000.0)

        result = apply_risk_rules(None, "KRW-TEST", state, 98.9, self._mock_sell, now=1_060.0)

        self.assertTrue(result.get("closed", False))
        self.assertEqual(state.get("last_exit_reason"), "STOPLOSS")
        self.assertAlmostEqual(float(state.get("total_sell_krw", 0.0)), 98.9, places=8)

    def test_tp_one_full_close_non_main(self):
        state = self._base_state(entry_ts=1_000.0, strategy_tag="SCALP")
        state["tp_one_pct"] = 0.01

        result = apply_risk_rules(None, "KRW-TEST", state, 101.1, self._mock_sell, now=1_200.0, strategy_tag="SCALP")

        self.assertTrue(result.get("closed", False))
        self.assertEqual(state.get("last_exit_reason"), "TP2")
        self.assertEqual(result.get("reason"), "tp2")

    def test_main_ignores_tp_one(self):
        state = self._base_state(entry_ts=1_000.0)
        state["tp_one_pct"] = 0.01

        result = apply_risk_rules(None, "KRW-TEST", state, 101.1, self._mock_sell, now=1_200.0, strategy_tag="MAIN")

        self.assertFalse(result.get("closed", False))
        self.assertNotEqual(result.get("reason"), "tp2")

    def test_main_tp2_activates_runner_and_trails_close(self):
        self._set(
            "TP_TABLE",
            {
                "LOW": {"TP1_PCT": 0.01, "TP2_PCT": 0.02, "TRAIL_BACK_PCT": 0.006},
                "MID": {"TP1_PCT": 0.01, "TP2_PCT": 0.02, "TRAIL_BACK_PCT": 0.006},
                "FULL": {"TP1_PCT": 0.01, "TP2_PCT": 0.02, "TRAIL_BACK_PCT": 0.006},
                "HALT": {"TP1_PCT": 0.0, "TP2_PCT": 0.0, "TRAIL_BACK_PCT": 0.0},
            },
        )
        state = self._base_state(entry_ts=1_000.0)

        r1 = apply_risk_rules(None, "KRW-TEST", state, 102.1, self._mock_sell, now=1_120.0, strategy_tag="MAIN")
        self.assertFalse(r1.get("closed", False))
        self.assertTrue(state.get("tp1_done", False))
        self.assertTrue(state.get("tp2_done", False))
        self.assertTrue(state.get("runner_active", False))
        reasons = [str(x.get("reason", "")) for x in (r1.get("partials", []) or [])]
        self.assertIn("TP1", reasons)
        self.assertIn("TP2_PARTIAL", reasons)
        self.assertGreater(float(state.get("initial_volume", 0.0)), 0.0)

        r2 = apply_risk_rules(None, "KRW-TEST", state, 101.3, self._mock_sell, now=1_150.0, strategy_tag="MAIN")
        self.assertTrue(r2.get("closed", False))
        self.assertEqual(r2.get("reason"), "RUNNER_TRAIL")

    def test_main_tp1_arms_runner_when_tp2_ratio_zero(self):
        self._set(
            "TP_TABLE",
            {
                "LOW": {"TP1_PCT": 0.01, "TP2_PCT": 0.02, "TRAIL_BACK_PCT": 0.006},
                "MID": {"TP1_PCT": 0.01, "TP2_PCT": 0.02, "TRAIL_BACK_PCT": 0.006},
                "FULL": {"TP1_PCT": 0.01, "TP2_PCT": 0.02, "TRAIL_BACK_PCT": 0.006},
                "HALT": {"TP1_PCT": 0.0, "TP2_PCT": 0.0, "TRAIL_BACK_PCT": 0.0},
            },
        )
        state = self._base_state(entry_ts=1_000.0)
        state["tp1_ratio"] = 0.60
        state["tp2_ratio"] = 0.0
        state["runner_ratio"] = 0.40

        r1 = apply_risk_rules(None, "KRW-TEST", state, 101.1, self._mock_sell, now=1_120.0, strategy_tag="MAIN")
        self.assertFalse(r1.get("closed", False))
        self.assertTrue(state.get("tp1_done", False))
        self.assertFalse(state.get("tp2_done", False))
        self.assertTrue(state.get("runner_active", False))

        r2 = apply_risk_rules(None, "KRW-TEST", state, 100.3, self._mock_sell, now=1_150.0, strategy_tag="MAIN")
        self.assertTrue(r2.get("closed", False))
        self.assertEqual(r2.get("reason"), "RUNNER_TRAIL")

    def test_main_runner_timeout_close(self):
        self._set(
            "TP_TABLE",
            {
                "LOW": {"TP1_PCT": 0.01, "TP2_PCT": 0.02, "TRAIL_BACK_PCT": 0.006},
                "MID": {"TP1_PCT": 0.01, "TP2_PCT": 0.02, "TRAIL_BACK_PCT": 0.006},
                "FULL": {"TP1_PCT": 0.01, "TP2_PCT": 0.02, "TRAIL_BACK_PCT": 0.006},
                "HALT": {"TP1_PCT": 0.0, "TP2_PCT": 0.0, "TRAIL_BACK_PCT": 0.0},
            },
        )
        self._set("MAIN_RUNNER_MAX_HOLD_MIN", 1)
        self._set("MAIN_RUNNER_TRAIL_GIVEBACK_PCT", 0.5)
        self._set("MAIN_RUNNER_TIMEOUT_CLOSE_IF_PNL_GE", 0.0)

        state = self._base_state(entry_ts=1_000.0)

        r1 = apply_risk_rules(None, "KRW-TEST", state, 102.1, self._mock_sell, now=1_120.0, strategy_tag="MAIN")
        self.assertFalse(r1.get("closed", False))
        self.assertTrue(state.get("runner_active", False))

        r2 = apply_risk_rules(None, "KRW-TEST", state, 101.0, self._mock_sell, now=1_181.0, strategy_tag="MAIN")
        self.assertTrue(r2.get("closed", False))
        self.assertEqual(r2.get("reason"), "RUNNER_TIMEOUT")

    def test_main_tp1_transient_zero_balance_does_not_force_close(self):
        self._set("REAL_ORDER", True)
        self._set("MIN_ORDER_KRW", 5_000)
        self._set(
            "TP_TABLE",
            {
                "LOW": {"TP1_PCT": 0.01, "TP2_PCT": 0.05, "TRAIL_BACK_PCT": 0.006},
                "MID": {"TP1_PCT": 0.01, "TP2_PCT": 0.05, "TRAIL_BACK_PCT": 0.006},
                "FULL": {"TP1_PCT": 0.01, "TP2_PCT": 0.05, "TRAIL_BACK_PCT": 0.006},
                "HALT": {"TP1_PCT": 0.0, "TP2_PCT": 0.0, "TRAIL_BACK_PCT": 0.0},
            },
        )
        state = self._base_state(entry_ts=1_000.0)
        qty = 1_044.17175768
        state["entry"] = 28.5
        state["peak"] = 28.5
        state["initial_volume"] = qty
        state["invested_krw"] = qty * 28.5
        state["total_buy_krw"] = qty * 28.5
        state["tp1_ratio"] = 0.5
        state["tp2_ratio"] = 0.3
        state["runner_ratio"] = 0.2

        with patch("risk.get_balance", side_effect=[qty, qty, 0.0]), patch("risk.notify_order", return_value=True):
            result = apply_risk_rules(
                upbit=object(),
                ticker="KRW-POKT",
                state=state,
                cur=28.9,
                market_sell=self._mock_sell,
                now=1_130.0,
                strategy_tag="MAIN",
            )

        self.assertFalse(result.get("closed", False))
        self.assertTrue(state.get("tp1_done", False))
        expected_sell = qty * 0.5 * 28.9
        self.assertAlmostEqual(float(state.get("total_sell_krw", 0.0)), expected_sell, places=6)

    def test_main_tp1_ob_fvg_adjust_triggers_before_base_tp1(self):
        self._set("MAIN_OB_FVG_TP_ADJUST_ENABLED", True)
        self._set(
            "TP_TABLE",
            {
                "LOW": {"TP1_PCT": 0.01, "TP2_PCT": 0.02, "TRAIL_BACK_PCT": 0.006},
                "MID": {"TP1_PCT": 0.01, "TP2_PCT": 0.02, "TRAIL_BACK_PCT": 0.006},
                "FULL": {"TP1_PCT": 0.01, "TP2_PCT": 0.02, "TRAIL_BACK_PCT": 0.006},
                "HALT": {"TP1_PCT": 0.0, "TP2_PCT": 0.0, "TRAIL_BACK_PCT": 0.0},
            },
        )
        state = self._base_state(entry_ts=1_000.0)
        df_15m = self._main_ob_fvg_df(resistance_level=101.0)
        rsi_series = pd.Series([60.0] * (len(df_15m) - 2) + [55.0, 50.0], index=df_15m.index)

        with patch("risk.pyupbit.get_ohlcv", return_value=df_15m), patch("risk.get_rsi", return_value=rsi_series):
            result = apply_risk_rules(
                upbit=None,
                ticker="KRW-TEST",
                state=state,
                cur=100.8,
                market_sell=self._mock_sell,
                now=1_140.0,
                strategy_tag="MAIN",
            )

        self.assertFalse(result.get("closed", False))
        self.assertTrue(state.get("tp1_done", False))
        self.assertTrue(state.get("tp1_adjusted_done", False))
        reasons = [str(x.get("reason", "")) for x in (result.get("partials", []) or [])]
        self.assertIn("TP1_OB_FVG_ADJUST", reasons)
        self.assertNotIn("TP1", reasons)

    def test_main_runner_trail_tighten_ob_fvg_applies_once(self):
        self._set("MAIN_OB_FVG_TP_ADJUST_ENABLED", True)
        state = self._base_state(entry_ts=1_000.0)
        state["tp1_done"] = True
        state["tp2_done"] = True
        state["tp1"] = True
        state["tp2"] = True
        state["runner_active"] = True
        state["runner_hwm"] = 105.0
        state["runner_start_ts"] = 1_060.0
        state["runner_trail_tightened_done"] = False
        state["runner_trail_giveback_pct"] = None

        df_15m = self._main_ob_fvg_df(resistance_level=105.0)
        rsi_series = pd.Series([62.0] * (len(df_15m) - 2) + [58.0, 52.0], index=df_15m.index)

        with patch("risk.pyupbit.get_ohlcv", return_value=df_15m), patch("risk.get_rsi", return_value=rsi_series):
            r1 = apply_risk_rules(
                upbit=None,
                ticker="KRW-TEST",
                state=state,
                cur=104.75,
                market_sell=self._mock_sell,
                now=1_150.0,
                strategy_tag="MAIN",
            )
            r2 = apply_risk_rules(
                upbit=None,
                ticker="KRW-TEST",
                state=state,
                cur=104.74,
                market_sell=self._mock_sell,
                now=1_160.0,
                strategy_tag="MAIN",
            )

        self.assertFalse(r1.get("closed", False))
        self.assertFalse(r2.get("closed", False))
        self.assertTrue(state.get("runner_trail_tightened_done", False))
        tightened = float(state.get("runner_trail_giveback_pct", 0.0))
        self.assertGreaterEqual(tightened, 0.003)
        self.assertLess(tightened, 0.007)
        r1_reasons = [str(x.get("reason", "")) for x in (r1.get("partials", []) or [])]
        r2_reasons = [str(x.get("reason", "")) for x in (r2.get("partials", []) or [])]
        self.assertIn("RUNNER_TRAIL_TIGHTEN_OB_FVG", r1_reasons)
        self.assertNotIn("RUNNER_TRAIL_TIGHTEN_OB_FVG", r2_reasons)


if __name__ == "__main__":
    unittest.main()
