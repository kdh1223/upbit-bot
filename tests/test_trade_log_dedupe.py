import os
import tempfile
import unittest

from engine_manage import append_trade_log


class TradeLogDedupeTests(unittest.TestCase):
    def test_append_trade_log_skips_identical_consecutive_row(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "trade_log.csv")
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write("time,ticker,entry_price,exit_price,pnl_pct,reason,regime,strategy\n")

            row = [
                "2026-02-09 21:31:32",
                "KRW-BTC",
                "102236000.000000",
                "102331000.000000",
                "0.09",
                "scalp_btc_timeout",
                "SCALP_BTC",
                "SCALP_BTC",
            ]
            r1 = append_trade_log(path, row)
            r2 = append_trade_log(path, row)
            self.assertTrue(r1)
            self.assertFalse(r2)

            with open(path, "r", encoding="utf-8", newline="") as f:
                lines = [ln for ln in f.read().splitlines() if ln.strip()]
            self.assertEqual(len(lines), 2)


if __name__ == "__main__":
    unittest.main()
