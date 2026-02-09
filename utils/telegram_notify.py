"""Telegram notifier with event codes and Korean alert titles."""

import os
from typing import Iterable, Optional


EVENT_TITLE = {
    "BOT_START": "\U0001F7E2 \uBD07 \uC2DC\uC791",
    "BOT_STOP": "\u26D4 \uBD07 \uC885\uB8CC",
    "BOT_RESTART": "\U0001F501 \uBD07 \uC7AC\uC2DC\uC791",
    "BOT_CRASH": "\U0001F534 \uBD07 \uBE44\uC815\uC0C1 \uC885\uB8CC",
    "BOT_RECOVERED": "\U0001FA79 \uBD07 \uC790\uB3D9 \uBCF5\uAD6C",
    "ORDER_BUY_FILLED": "\U0001F7E2 \uB9E4\uC218 \uCCB4\uACB0",
    "ORDER_SELL_FILLED": "\U0001F535 \uB9E4\uB3C4 \uCCB4\uACB0",
    "ORDER_BUY_FAILED": "\u274C \uB9E4\uC218 \uC2E4\uD328",
    "ORDER_SELL_FAILED": "\u274C \uB9E4\uB3C4 \uC2E4\uD328",
    "ORDER_PARTIAL_FILL": "\u26A0\uFE0F \uBD80\uBD84 \uCCB4\uACB0",
    "ORDER_CANCELED": "\U0001F9FE \uC8FC\uBB38 \uCDE8\uC18C",
    "TP1_HIT": "\U0001F3AF 1\uCC28 \uC775\uC808",
    "TP2_HIT": "\U0001F3AF \uCD5C\uC885 \uC775\uC808",
    "TRAILING_EXIT": "\U0001FA82 \uD2B8\uB808\uC77C\uB9C1 \uC775\uC808",
    "STOPLOSS_HIT": "\U0001F9EF \uC190\uC808 \uC2E4\uD589",
    "AVG_DOWN_BUY": "\U0001F504 \uCD94\uAC00 \uB9E4\uC218",
    "FORCE_CLOSE": "\U0001F6A8 \uAC15\uC81C \uCCAD\uC0B0",
    "DAILY_MAX_LOSS_REACHED": "\u26D4 \uC77C\uC77C \uC190\uC2E4 \uC81C\uD55C \uB3C4\uB2EC",
    "GLOBAL_MDD_REACHED": "\u26D4 \uACC4\uC88C \uCD5C\uB300\uB099\uD3ED \uC81C\uD55C \uB3C4\uB2EC",
    "INSUFFICIENT_BALANCE": "\U0001FA99 \uC794\uACE0 \uBD80\uC871",
    "MAX_HOLDINGS_REACHED": "\U0001F9F1 \uBCF4\uC720 \uC885\uBAA9 \uC218 \uC81C\uD55C \uB3C4\uB2EC",
    "REGIME_CHANGED": "\U0001F326\uFE0F \uC2DC\uC7A5 \uC0C1\uD0DC \uBCC0\uACBD",
    "MODE_CHANGED": "\U0001F9E0 \uC6B4\uC6A9 \uBAA8\uB4DC \uBCC0\uACBD",
    "COOLDOWN_STARTED": "\U0001F9CA \uCFFC\uD0C0\uC784 \uC2DC\uC791",
    "COOLDOWN_ENDED": "\U0001F525 \uCFFC\uD0C0\uC784 \uD574\uC81C",
    "DAILY_REPORT": "\U0001F4CA \uD558\uB8E8 \uB9C8\uAC10 \uB9AC\uD3EC\uD2B8",
    "POSITION_SNAPSHOT": "\U0001F4CC \uBCF4\uC720 \uD604\uD669",
    "HEARTBEAT": "\U0001F493 \uBD07 \uC815\uC0C1 \uC791\uB3D9 \uC911",
    "EXCEPTION_RAISED": "\u26A0\uFE0F \uC608\uC678 \uBC1C\uC0DD",
}


def _fmt_price(price) -> str:
    try:
        p = float(price)
    except Exception:
        return "-"
    if p <= 0:
        return "-"
    if p >= 1000:
        return f"{p:,.0f}"
    return f"{p:.8f}".rstrip("0").rstrip(".")


def _fmt_qty(qty) -> str:
    try:
        q = float(qty)
    except Exception:
        return "-"
    if q <= 0:
        return "-"
    return f"{q:.8f}".rstrip("0").rstrip(".")


def _title(event_type: str) -> str:
    code = str(event_type or "").upper().strip()
    return EVENT_TITLE.get(code, f"\u2139\uFE0F {code or 'UNKNOWN'}")


def build_event_message(event_type: str, lines: Optional[Iterable[str]] = None) -> str:
    out = [_title(event_type)]
    for line in list(lines or []):
        txt = str(line or "").strip()
        if txt:
            out.append(txt)
    return "\n".join(out)


def build_order_message(
    event_type: str,
    strategy_tag: str,
    ticker: str,
    price,
    qty,
    reason: str,
) -> str:
    return build_event_message(
        event_type,
        [
            f"\uC804\uB7B5: {str(strategy_tag or '').upper().strip() or '-'}",
            f"\uC885\uBAA9: {str(ticker or '').strip() or '-'}",
            f"\uAC00\uACA9: {_fmt_price(price)}",
            f"\uC218\uB7C9: {_fmt_qty(qty)}",
            f"\uC0AC\uC720: {str(reason or '').upper().strip() or '-'}",
        ],
    )


def tg_notify(event_type: str, message: str):
    try:
        token = str(os.getenv("TELEGRAM_TOKEN") or "").strip()
        chat_id = str(os.getenv("TELEGRAM_CHAT_ID") or "").strip()
        text = str(message or "").strip()
        if (not token) or (not chat_id) or (not text):
            return False

        import requests

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        requests.post(
            url,
            data={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=5,
        )
        return True
    except Exception:
        return False


def notify_event(event_type: str, lines: Optional[Iterable[str]] = None):
    msg = build_event_message(event_type, lines=lines)
    return tg_notify(event_type=event_type, message=msg)


def notify_order(
    event_type: str,
    strategy_tag: str,
    ticker: str,
    price,
    qty,
    reason: str,
):
    msg = build_order_message(
        event_type=event_type,
        strategy_tag=strategy_tag,
        ticker=ticker,
        price=price,
        qty=qty,
        reason=reason,
    )
    return tg_notify(event_type=event_type, message=msg)
