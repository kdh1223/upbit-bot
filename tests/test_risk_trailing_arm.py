import unittest

import config
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
            "TP_TABLE",
            {
                "LOW": {"TP1_PCT": 0.0, "TP2_PCT": 0.0, "TRAIL_BACK_PCT": 0.006},
                "MID": {"TP1_PCT": 0.0, "TP2_PCT": 0.0, "TRAIL_BACK_PCT": 0.006},
                "FULL": {"TP1_PCT": 0.0, "TP2_PCT": 0.0, "TRAIL_BACK_PCT": 0.006},
                "HALT": {"TP1_PCT": 0.0, "TP2_PCT": 0.0, "TRAIL_BACK_PCT": 0.0},
            },
        )

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
    def _base_state(entry_ts=1_000.0):
        return {
            "holding": True,
            "entry": 100.0,
            "peak": 100.0,
            "entry_ts": float(entry_ts),
            "trail_armed": False,
            "trail_hwm": 0.0,
            "tp1": False,
            "tp2": False,
            "regime": "MID",
            "initial_volume": 1.0,
            "invested_krw": 100.0,
            "target_krw": 200.0,
            "add_count": 1,
            "realized_krw": 0.0,
            "realized_cost_krw": 0.0,
        }

    def test_no_trailing_close_before_arm_window(self):
        state = self._base_state(entry_ts=1_000.0)

        r1 = apply_risk_rules(None, "KRW-TEST", state, 99.7, self._mock_sell, now=1_040.0)
        r2 = apply_risk_rules(None, "KRW-TEST", state, 99.1, self._mock_sell, now=1_110.0)

        self.assertFalse(r1.get("closed", False))
        self.assertFalse(r2.get("closed", False))
        self.assertNotEqual(r1.get("reason"), "trailing")
        self.assertNotEqual(r2.get("reason"), "trailing")
        self.assertFalse(state.get("trail_armed", False))
        self.assertEqual(float(state.get("trail_hwm", 0.0)), 0.0)

    def test_trailing_arms_and_closes_after_drawdown(self):
        state = self._base_state(entry_ts=1_000.0)

        r1 = apply_risk_rules(None, "KRW-TEST", state, 100.6, self._mock_sell, now=1_120.0)
        self.assertFalse(r1.get("closed", False))
        self.assertTrue(state.get("trail_armed", False))
        self.assertAlmostEqual(float(state.get("trail_hwm", 0.0)), 100.6, places=8)

        r2 = apply_risk_rules(None, "KRW-TEST", state, 101.2, self._mock_sell, now=1_130.0)
        self.assertFalse(r2.get("closed", False))
        self.assertAlmostEqual(float(state.get("trail_hwm", 0.0)), 101.2, places=8)

        r3 = apply_risk_rules(None, "KRW-TEST", state, 100.59, self._mock_sell, now=1_140.0)
        self.assertTrue(r3.get("closed", False))
        self.assertEqual(r3.get("reason"), "trailing")

    def test_stoploss_reason_in_arm_window(self):
        state = self._base_state(entry_ts=1_000.0)

        result = apply_risk_rules(None, "KRW-TEST", state, 98.9, self._mock_sell, now=1_060.0)

        self.assertTrue(result.get("closed", False))
        self.assertEqual(result.get("reason"), "stoploss")
        self.assertNotEqual(result.get("reason"), "trailing")


if __name__ == "__main__":
    unittest.main()
