import os
import unittest

from utils.telegram_notify import build_event_message, build_order_message, tg_notify


class TelegramNotifyTests(unittest.TestCase):
    def test_event_title_mapping(self):
        msg = build_event_message("ORDER_BUY_FILLED")
        self.assertTrue(msg.startswith("\U0001F7E2 \uB9E4\uC218 \uCCB4\uACB0"))

    def test_order_message_format(self):
        msg = build_order_message(
            event_type="ORDER_BUY_FILLED",
            strategy_tag="SCALP_BTC",
            ticker="KRW-BTC",
            price=43_210_000,
            qty=0.0021,
            reason="ENTRY",
        )
        self.assertIn("\uC804\uB7B5: SCALP_BTC", msg)
        self.assertIn("\uC885\uBAA9: KRW-BTC", msg)
        self.assertIn("\uAC00\uACA9:", msg)
        self.assertIn("\uC218\uB7C9:", msg)
        self.assertIn("\uC0AC\uC720: ENTRY", msg)

    def test_tg_notify_returns_false_without_env(self):
        old_token = os.environ.pop("TELEGRAM_TOKEN", None)
        old_chat = os.environ.pop("TELEGRAM_CHAT_ID", None)
        try:
            ok = tg_notify("HEARTBEAT", "test")
            self.assertFalse(ok)
        finally:
            if old_token is not None:
                os.environ["TELEGRAM_TOKEN"] = old_token
            if old_chat is not None:
                os.environ["TELEGRAM_CHAT_ID"] = old_chat


if __name__ == "__main__":
    unittest.main()
