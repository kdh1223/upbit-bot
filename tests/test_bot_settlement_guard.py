import unittest

import bot


class BotSettlementGuardTests(unittest.TestCase):
    def test_marks_half_sell_settlement_as_suspicious(self):
        buy, sell, suspicious, _ = bot._settlement_totals_sanity(
            total_buy_krw=29_759.0,
            total_sell_krw=15_088.0,
            close_qty=522.08587884,
            exit_price=28.9,
            raw_reason="FORCE_CLOSE",
        )
        self.assertGreater(buy, 0.0)
        self.assertGreater(sell, 0.0)
        self.assertTrue(suspicious)

    def test_dust_reason_is_not_flagged(self):
        _, _, suspicious, _ = bot._settlement_totals_sanity(
            total_buy_krw=29_759.0,
            total_sell_krw=100.0,
            close_qty=0.0,
            exit_price=0.0,
            raw_reason="dust(<min_order)",
        )
        self.assertFalse(suspicious)


if __name__ == "__main__":
    unittest.main()
