import unittest

import bot


class FinalCloseMessageTests(unittest.TestCase):
    def test_message_omits_tp2_when_not_done(self):
        msg = bot._build_final_close_message(
            {
                "strategy_tag": "MAIN",
                "ticker": "KRW-POKT",
                "entry_price": 28.5,
                "exit_price": 29.0,
                "total_buy_krw": 29_759.0,
                "total_sell_krw": 30_100.0,
                "tp1_done": True,
                "tp2_done": False,
                "tp1_ratio": 0.5,
                "tp2_ratio": 0.0,
                "tp1_pnl_pct": 1.4,
                "tp2_pnl_pct": None,
                "last_exit_reason": "FINAL",
            }
        )
        self.assertIn("TP1:", msg)
        self.assertIn("최종매도:", msg)
        self.assertNotIn("TP2:", msg)

    def test_message_shows_only_final_when_no_partials(self):
        msg = bot._build_final_close_message(
            {
                "strategy_tag": "MAIN",
                "ticker": "KRW-POKT",
                "entry_price": 28.5,
                "exit_price": 29.0,
                "total_buy_krw": 29_759.0,
                "total_sell_krw": 30_100.0,
                "tp1_done": False,
                "tp2_done": False,
                "tp1_ratio": 0.0,
                "tp2_ratio": 0.0,
                "last_exit_reason": "FINAL",
            }
        )
        self.assertNotIn("TP1:", msg)
        self.assertNotIn("TP2:", msg)
        self.assertIn("최종매도: +1.75% | 100%", msg)


if __name__ == "__main__":
    unittest.main()
