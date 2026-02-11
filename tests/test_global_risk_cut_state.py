import datetime as dt
import unittest

import bot
import config


_MISSING = object()


class GlobalRiskCutStateTests(unittest.TestCase):
    def setUp(self):
        self._backup = {}
        self._set("DAILY_MAX_LOSS_PCT", -5.0)
        self._set("GLOBAL_MDD_LIMIT_PCT", -15.0)
        self._set("RISK_CUT_CONFIRM_TICKS", 3)

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

    def test_risk_cut_requires_consecutive_breaches(self):
        state = bot._normalize_runtime_risk_state({})
        base = dt.datetime(2026, 2, 11, 10, 0, 0)
        seq = [100_000.0, 69_000.0, 68_000.0, 67_000.0]
        snapshots = []

        for i, eq in enumerate(seq):
            now = base + dt.timedelta(minutes=i)
            info, _, triggered = bot._update_global_risk_cut_state(now=now, equity=eq, risk_state=state)
            snapshots.append((info, triggered))

        self.assertFalse(snapshots[1][0]["halted"])
        self.assertFalse(snapshots[2][0]["halted"])
        self.assertTrue(snapshots[3][0]["halted"])
        self.assertEqual(snapshots[3][0]["reason"], "TOTAL_MDD_LIMIT")
        self.assertTrue(snapshots[3][1])

    def test_zero_equity_uses_last_good_cache(self):
        state = bot._normalize_runtime_risk_state({})
        now = dt.datetime(2026, 2, 11, 10, 0, 0)
        bot._update_global_risk_cut_state(now=now, equity=100_000.0, risk_state=state)

        info, _, _ = bot._update_global_risk_cut_state(now=now + dt.timedelta(minutes=1), equity=0.0, risk_state=state)
        self.assertTrue(info.get("used_last_good_equity", False))
        self.assertAlmostEqual(float(info.get("last_good_equity", 0.0)), 100_000.0, places=6)
        self.assertFalse(info.get("halted", False))


if __name__ == "__main__":
    unittest.main()
