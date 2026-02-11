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
        self._set("RISK_EQUITY_DROP_GUARD_PCT", 0.20)
        self._set("RISK_EQUITY_DROP_GUARD_TICKS", 30)
        self._set("RISK_EQUITY_DROP_GUARD_STRICT_NO_HOLDINGS", True)

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
            info, _, triggered = bot._update_global_risk_cut_state(
                now=now,
                equity=eq,
                risk_state=state,
                holdings_count=1,
            )
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

    def test_sudden_drop_without_holdings_is_guarded_strictly(self):
        self._set("RISK_CUT_CONFIRM_TICKS", 1)
        self._set("RISK_EQUITY_DROP_GUARD_TICKS", 3)
        state = bot._normalize_runtime_risk_state({})
        base = dt.datetime(2026, 2, 11, 10, 0, 0)

        bot._update_global_risk_cut_state(now=base, equity=100_000.0, risk_state=state, holdings_count=0)
        snap = []
        for i in range(1, 6):
            info, _, triggered = bot._update_global_risk_cut_state(
                now=base + dt.timedelta(seconds=i),
                equity=69_000.0,
                risk_state=state,
                holdings_count=0,
            )
            snap.append((info, triggered))

        self.assertTrue(all(not x[0]["halted"] for x in snap))

    def test_sudden_drop_without_holdings_can_fallback_to_legacy_behavior(self):
        self._set("RISK_CUT_CONFIRM_TICKS", 1)
        self._set("RISK_EQUITY_DROP_GUARD_TICKS", 3)
        self._set("RISK_EQUITY_DROP_GUARD_STRICT_NO_HOLDINGS", False)
        state = bot._normalize_runtime_risk_state({})
        base = dt.datetime(2026, 2, 11, 10, 0, 0)

        bot._update_global_risk_cut_state(now=base, equity=100_000.0, risk_state=state, holdings_count=0)
        snap = []
        for i in range(1, 4):
            info, _, triggered = bot._update_global_risk_cut_state(
                now=base + dt.timedelta(seconds=i),
                equity=69_000.0,
                risk_state=state,
                holdings_count=0,
            )
            snap.append((info, triggered))

        self.assertFalse(snap[0][0]["halted"])
        self.assertFalse(snap[1][0]["halted"])
        self.assertTrue(snap[2][0]["halted"])
        self.assertEqual(snap[2][0]["reason"], "TOTAL_MDD_LIMIT")

    def test_sudden_drop_with_holdings_is_not_guarded(self):
        self._set("RISK_CUT_CONFIRM_TICKS", 1)
        self._set("RISK_EQUITY_DROP_GUARD_TICKS", 30)
        state = bot._normalize_runtime_risk_state({})
        base = dt.datetime(2026, 2, 11, 10, 0, 0)

        bot._update_global_risk_cut_state(now=base, equity=100_000.0, risk_state=state, holdings_count=1)
        info, _, triggered = bot._update_global_risk_cut_state(
            now=base + dt.timedelta(seconds=1),
            equity=69_000.0,
            risk_state=state,
            holdings_count=1,
        )

        self.assertTrue(info["halted"])
        self.assertEqual(info["reason"], "TOTAL_MDD_LIMIT")
        self.assertTrue(triggered)


if __name__ == "__main__":
    unittest.main()
