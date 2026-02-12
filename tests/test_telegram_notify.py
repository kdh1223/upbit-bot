import json
import os
import tempfile
import unittest
from unittest import mock

import utils.telegram_notify as telegram_notify
from utils.telegram_notify import build_event_message, build_order_message, flush_telegram_spool, tg_notify


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

    def test_order_message_includes_ob_fvg_adjust_line(self):
        msg = build_order_message(
            event_type="ORDER_PARTIAL_FILL",
            strategy_tag="MAIN",
            ticker="KRW-BTC",
            price=100_000_000,
            qty=0.001,
            reason="TP1_OB_FVG_ADJUST",
        )
        self.assertIn("OB/FVG 보정 적용", msg)

    def test_position_closed_is_critical_missed_event(self):
        self.assertTrue(telegram_notify._is_critical_event("POSITION_CLOSED"))

    def test_tg_notify_returns_false_without_env(self):
        old_env_file = os.environ.get("TELEGRAM_ENV_FILE")
        old_spool = os.environ.get("TELEGRAM_SPOOL_PATH")
        old_token = os.environ.pop("TELEGRAM_TOKEN", None)
        old_chat = os.environ.pop("TELEGRAM_CHAT_ID", None)
        with tempfile.TemporaryDirectory() as td:
            os.environ["TELEGRAM_ENV_FILE"] = "__missing__.env"
            os.environ["TELEGRAM_SPOOL_PATH"] = os.path.join(td, "telegram_spool.jsonl")
            try:
                ok = tg_notify("HEARTBEAT", "test")
                self.assertFalse(ok)
            finally:
                if old_env_file is None:
                    os.environ.pop("TELEGRAM_ENV_FILE", None)
                else:
                    os.environ["TELEGRAM_ENV_FILE"] = old_env_file
                if old_spool is None:
                    os.environ.pop("TELEGRAM_SPOOL_PATH", None)
                else:
                    os.environ["TELEGRAM_SPOOL_PATH"] = old_spool
                if old_token is not None:
                    os.environ["TELEGRAM_TOKEN"] = old_token
                if old_chat is not None:
                    os.environ["TELEGRAM_CHAT_ID"] = old_chat

    def test_tg_notify_failed_send_is_spooled(self):
        old_token = os.environ.get("TELEGRAM_TOKEN")
        old_chat = os.environ.get("TELEGRAM_CHAT_ID")
        old_spool = os.environ.get("TELEGRAM_SPOOL_PATH")
        old_env_file = os.environ.get("TELEGRAM_ENV_FILE")
        with tempfile.TemporaryDirectory() as td:
            spool_path = os.path.join(td, "telegram_spool.jsonl")
            os.environ["TELEGRAM_TOKEN"] = "test_token"
            os.environ["TELEGRAM_CHAT_ID"] = "test_chat"
            os.environ["TELEGRAM_SPOOL_PATH"] = spool_path
            os.environ["TELEGRAM_ENV_FILE"] = "__missing__.env"
            try:
                with mock.patch("requests.post", side_effect=RuntimeError("network down")), mock.patch(
                    "time.sleep", return_value=None
                ):
                    ok = tg_notify("ORDER_BUY_FILLED", "filled")
                self.assertFalse(ok)
                self.assertTrue(os.path.exists(spool_path))
                with open(spool_path, "r", encoding="utf-8") as f:
                    rows = [json.loads(line) for line in f if line.strip()]
                self.assertGreaterEqual(len(rows), 1)
                self.assertEqual(rows[0].get("event_type"), "ORDER_BUY_FILLED")
            finally:
                if old_token is None:
                    os.environ.pop("TELEGRAM_TOKEN", None)
                else:
                    os.environ["TELEGRAM_TOKEN"] = old_token
                if old_chat is None:
                    os.environ.pop("TELEGRAM_CHAT_ID", None)
                else:
                    os.environ["TELEGRAM_CHAT_ID"] = old_chat
                if old_spool is None:
                    os.environ.pop("TELEGRAM_SPOOL_PATH", None)
                else:
                    os.environ["TELEGRAM_SPOOL_PATH"] = old_spool
                if old_env_file is None:
                    os.environ.pop("TELEGRAM_ENV_FILE", None)
                else:
                    os.environ["TELEGRAM_ENV_FILE"] = old_env_file

    def test_position_closed_failure_prints_missed_alert(self):
        old_token = os.environ.get("TELEGRAM_TOKEN")
        old_chat = os.environ.get("TELEGRAM_CHAT_ID")
        old_spool = os.environ.get("TELEGRAM_SPOOL_PATH")
        old_env_file = os.environ.get("TELEGRAM_ENV_FILE")
        with tempfile.TemporaryDirectory() as td:
            spool_path = os.path.join(td, "telegram_spool.jsonl")
            os.environ["TELEGRAM_TOKEN"] = "test_token"
            os.environ["TELEGRAM_CHAT_ID"] = "test_chat"
            os.environ["TELEGRAM_SPOOL_PATH"] = spool_path
            os.environ["TELEGRAM_ENV_FILE"] = "__missing__.env"
            try:
                with mock.patch("requests.post", side_effect=RuntimeError("network down")), mock.patch(
                    "time.sleep", return_value=None
                ), mock.patch("builtins.print") as mock_print:
                    ok = tg_notify("POSITION_CLOSED", "closed")
                self.assertFalse(ok)
                joined = "\n".join(" ".join(str(x) for x in c.args) for c in mock_print.call_args_list)
                self.assertIn("[ALERT][TELEGRAM_MISSED] event=POSITION_CLOSED kind=sendMessage", joined)
            finally:
                if old_token is None:
                    os.environ.pop("TELEGRAM_TOKEN", None)
                else:
                    os.environ["TELEGRAM_TOKEN"] = old_token
                if old_chat is None:
                    os.environ.pop("TELEGRAM_CHAT_ID", None)
                else:
                    os.environ["TELEGRAM_CHAT_ID"] = old_chat
                if old_spool is None:
                    os.environ.pop("TELEGRAM_SPOOL_PATH", None)
                else:
                    os.environ["TELEGRAM_SPOOL_PATH"] = old_spool
                if old_env_file is None:
                    os.environ.pop("TELEGRAM_ENV_FILE", None)
                else:
                    os.environ["TELEGRAM_ENV_FILE"] = old_env_file

    def test_flush_spool_resends_oldest_first_with_limit(self):
        old_token = os.environ.get("TELEGRAM_TOKEN")
        old_chat = os.environ.get("TELEGRAM_CHAT_ID")
        old_spool = os.environ.get("TELEGRAM_SPOOL_PATH")
        old_env_file = os.environ.get("TELEGRAM_ENV_FILE")
        with tempfile.TemporaryDirectory() as td:
            spool_path = os.path.join(td, "telegram_spool.jsonl")
            os.environ["TELEGRAM_TOKEN"] = "test_token"
            os.environ["TELEGRAM_CHAT_ID"] = "test_chat"
            os.environ["TELEGRAM_SPOOL_PATH"] = spool_path
            os.environ["TELEGRAM_ENV_FILE"] = "__missing__.env"
            first = {
                "ts": 1.0,
                "event_type": "ORDER_BUY_FILLED",
                "kind": "sendMessage",
                "payload": {"text": "first"},
            }
            second = {
                "ts": 2.0,
                "event_type": "ORDER_SELL_FILLED",
                "kind": "sendMessage",
                "payload": {"text": "second"},
            }
            with open(spool_path, "w", encoding="utf-8") as f:
                f.write(json.dumps(first) + "\n")
                f.write(json.dumps(second) + "\n")

            mock_resp = mock.Mock()
            mock_resp.raise_for_status.return_value = None
            mock_resp.json.return_value = {"ok": True}
            try:
                with mock.patch("requests.post", return_value=mock_resp):
                    sent = flush_telegram_spool(limit=1)
                self.assertEqual(sent, 1)
                with open(spool_path, "r", encoding="utf-8") as f:
                    rows = [json.loads(line) for line in f if line.strip()]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0].get("payload", {}).get("text"), "second")
            finally:
                if old_token is None:
                    os.environ.pop("TELEGRAM_TOKEN", None)
                else:
                    os.environ["TELEGRAM_TOKEN"] = old_token
                if old_chat is None:
                    os.environ.pop("TELEGRAM_CHAT_ID", None)
                else:
                    os.environ["TELEGRAM_CHAT_ID"] = old_chat
                if old_spool is None:
                    os.environ.pop("TELEGRAM_SPOOL_PATH", None)
                else:
                    os.environ["TELEGRAM_SPOOL_PATH"] = old_spool
                if old_env_file is None:
                    os.environ.pop("TELEGRAM_ENV_FILE", None)
                else:
                    os.environ["TELEGRAM_ENV_FILE"] = old_env_file


if __name__ == "__main__":
    unittest.main()
