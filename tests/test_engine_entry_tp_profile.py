import unittest

import config
import engine_entry


_MISSING = object()


class EngineEntryTpProfileTests(unittest.TestCase):
    def setUp(self):
        self._backup = {}
        self._set(
            "MAIN_TP_RATIOS",
            {
                "CONSERVATIVE": {"TP1": 0.60, "TP2": 0.30, "RUNNER": 0.10},
                "AGGRESSIVE": {"TP1": 0.40, "TP2": 0.40, "RUNNER": 0.20},
            },
        )
        self._set("SMALL_EQUITY_MAIN_TP_PROFILE_ENABLED", True)
        self._set("SMALL_EQUITY_MAIN_TP_MAX_EQUITY", 200_000)
        self._set("SMALL_EQUITY_MAIN_TP_RATIOS", {"TP1": 0.60, "TP2": 0.00, "RUNNER": 0.40})

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

    def test_small_equity_overrides_mode_tp_profile(self):
        row = {}
        engine_entry._apply_main_tp_profile_on_entry(row, mode="AGGRESSIVE", equity=200_000)
        self.assertAlmostEqual(float(row["tp1_ratio"]), 0.60, places=8)
        self.assertAlmostEqual(float(row["tp2_ratio"]), 0.00, places=8)
        self.assertAlmostEqual(float(row["runner_ratio"]), 0.40, places=8)
        self.assertEqual(row["entry_mode"], "AGGRESSIVE")

    def test_above_threshold_uses_mode_profile(self):
        row = {}
        engine_entry._apply_main_tp_profile_on_entry(row, mode="AGGRESSIVE", equity=200_001)
        self.assertAlmostEqual(float(row["tp1_ratio"]), 0.40, places=8)
        self.assertAlmostEqual(float(row["tp2_ratio"]), 0.40, places=8)
        self.assertAlmostEqual(float(row["runner_ratio"]), 0.20, places=8)


if __name__ == "__main__":
    unittest.main()
