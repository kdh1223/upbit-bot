"""Telegram notifier with resilient delivery, retry, and local spool fallback."""

import json
import os
import time
import traceback
from typing import Dict, Iterable, List, Optional


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
    "ORDER_PARTIAL_FILL": "\u26A0\uFE0F \uBD80\uBD84\uB9E4\uB3C4 \uC2E4\uD589",
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

_CRITICAL_MISSED_EVENTS = {
    "ORDER_BUY_FILLED",
    "ORDER_SELL_FILLED",
    "POSITION_CLOSED",
    "ORDER_PARTIAL_FILL",
    "TP1_HIT",
    "TP2_HIT",
    "TRAILING_EXIT",
    "STOPLOSS_HIT",
    "FORCE_CLOSE",
    "DAILY_MAX_LOSS_REACHED",
    "GLOBAL_MDD_REACHED",
}
_RETRY_BACKOFF_SEC = (1.0, 3.0, 7.0)
_DEFAULT_ENV_FILE = "/etc/default/telegram-bot"
_DEFAULT_SPOOL_FILE = "telegram_spool.jsonl"
_SPOOL_FLUSH_LIMIT = 50
_SPOOL_FLUSH_INTERVAL_SEC = 30.0
_REQUEST_TIMEOUT_SEC = 1.8

_ENV_LOADED = False
_MISSING_ENV_WARNED = False
_LAST_SPOOL_FLUSH_TS = 0.0
_SPOOL_FLUSHING = False


def _strip_quotes(value: str) -> str:
    s = str(value or "").strip()
    if len(s) >= 2 and ((s[0] == s[-1] == '"') or (s[0] == s[-1] == "'")):
        return s[1:-1].strip()
    return s


def _env_file_path() -> str:
    p = str(os.getenv("TELEGRAM_ENV_FILE") or _DEFAULT_ENV_FILE).strip()
    return p or _DEFAULT_ENV_FILE


def _spool_path() -> str:
    p = str(os.getenv("TELEGRAM_SPOOL_PATH") or _DEFAULT_SPOOL_FILE).strip()
    return p or _DEFAULT_SPOOL_FILE


def _request_timeout() -> float:
    raw = os.getenv("TELEGRAM_TIMEOUT_SEC")
    if raw is None:
        return float(_REQUEST_TIMEOUT_SEC)
    try:
        return max(0.2, float(raw))
    except Exception:
        return float(_REQUEST_TIMEOUT_SEC)


def load_telegram_env_file(path: Optional[str] = None):
    target = str(path or _env_file_path()).strip()
    if not target:
        return
    if not os.path.exists(target):
        return
    try:
        with open(target, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        print(f"[WARN] telegram env file read failed: {target}")
        print(traceback.format_exc().rstrip())
        return

    for raw in lines:
        line = str(raw or "").strip()
        if (not line) or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = str(key or "").strip()
        if not key:
            continue
        if key in os.environ:
            continue
        os.environ[key] = _strip_quotes(value)


def _ensure_env_loaded():
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    load_telegram_env_file()
    _ENV_LOADED = True


def has_telegram_credentials() -> bool:
    _ensure_env_loaded()
    token = str(os.getenv("TELEGRAM_TOKEN") or "").strip()
    chat_id = str(os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    return bool(token and chat_id)


def _warn_missing_credentials_once():
    global _MISSING_ENV_WARNED
    if _MISSING_ENV_WARNED:
        return
    if has_telegram_credentials():
        return
    _MISSING_ENV_WARNED = True
    print("[WARN] TELEGRAM_TOKEN/TELEGRAM_CHAT_ID missing; telegram notifications will be queued to spool")


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


def _fmt_krw(value) -> str:
    try:
        v = float(value)
    except Exception:
        return "-"
    if v <= 0:
        return "-"
    return f"{int(round(v)):,}"


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
    buy_krw=None,
) -> str:
    code = str(event_type or "").upper().strip()
    reason_code = str(reason or "").upper().strip()
    lines = [
        f"\uC804\uB7B5: {str(strategy_tag or '').upper().strip() or '-'}",
        f"\uC885\uBAA9: {str(ticker or '').strip() or '-'}",
        f"\uAC00\uACA9: {_fmt_price(price)}",
        f"\uC218\uB7C9: {_fmt_qty(qty)}",
    ]
    if code == "ORDER_BUY_FILLED":
        buy_txt = _fmt_krw(buy_krw)
        if buy_txt != "-":
            lines.append(f"\uB9E4\uC218\uAE08: {buy_txt} KRW")
    lines.append(f"\uC0AC\uC720: {reason_code or '-'}")
    if reason_code in {"TP1_OB_FVG_ADJUST", "RUNNER_TRAIL_TIGHTEN_OB_FVG"}:
        lines.append("OB/FVG 보정 적용")
    return build_event_message(
        event_type,
        lines,
    )


def _is_critical_event(event_type: str) -> bool:
    code = str(event_type or "").upper().strip()
    return code in _CRITICAL_MISSED_EVENTS


def _alert_telegram_missed(event_type: str, payload_kind: str):
    if not _is_critical_event(event_type):
        return
    print(f"[ALERT][TELEGRAM_MISSED] event={str(event_type or '').upper().strip()} kind={payload_kind}")


def _spool_item(event_type: str, payload_kind: str, payload: Dict) -> Dict:
    return {
        "ts": float(time.time()),
        "event_type": str(event_type or "").upper().strip() or "UNKNOWN",
        "kind": str(payload_kind or "sendMessage"),
        "payload": dict(payload or {}),
    }


def _append_spool(item: Dict):
    path = _spool_path()
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    except Exception:
        print(f"[WARN][TELEGRAM] failed to append spool: {path}")
        print(traceback.format_exc().rstrip())


def _read_spool_items() -> List[Dict]:
    path = _spool_path()
    if not os.path.exists(path):
        return []
    out: List[Dict] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = str(raw or "").strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if isinstance(row, dict):
                    out.append(row)
    except Exception:
        print(f"[WARN][TELEGRAM] failed to read spool: {path}")
        print(traceback.format_exc().rstrip())
    return out


def _write_spool_items(items: List[Dict]):
    path = _spool_path()
    temp = f"{path}.tmp"
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(temp, "w", encoding="utf-8") as f:
            for row in list(items or []):
                if isinstance(row, dict):
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.replace(temp, path)
    except Exception:
        print(f"[WARN][TELEGRAM] failed to write spool: {path}")
        print(traceback.format_exc().rstrip())
        try:
            if os.path.exists(temp):
                os.remove(temp)
        except Exception:
            pass


def _send_message_once(token: str, chat_id: str, text: str):
    import requests

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(
        url,
        data={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        timeout=_request_timeout(),
    )
    resp.raise_for_status()
    try:
        body = resp.json()
    except Exception:
        body = None
    if isinstance(body, dict) and (not bool(body.get("ok", True))):
        raise RuntimeError(f"telegram api not ok: {body.get('description')}")


def _send_photo_once(token: str, chat_id: str, photo_path: str, caption: str = ""):
    import requests

    if not os.path.exists(photo_path):
        raise FileNotFoundError(photo_path)

    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    with open(photo_path, "rb") as f:
        data = {"chat_id": chat_id}
        cap = str(caption or "").strip()
        if cap:
            data["caption"] = cap
        resp = requests.post(
            url,
            data=data,
            files={"photo": f},
            timeout=max(2.0, _request_timeout()),
        )
    resp.raise_for_status()
    try:
        body = resp.json()
    except Exception:
        body = None
    if isinstance(body, dict) and (not bool(body.get("ok", True))):
        raise RuntimeError(f"telegram api not ok: {body.get('description')}")


def _send_with_retry(event_type: str, payload_kind: str, payload: Dict) -> bool:
    _ensure_env_loaded()
    token = str(os.getenv("TELEGRAM_TOKEN") or "").strip()
    chat_id = str(os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    if (not token) or (not chat_id):
        _warn_missing_credentials_once()
        return False

    max_attempt = len(_RETRY_BACKOFF_SEC) + 1
    for attempt in range(1, max_attempt + 1):
        try:
            if payload_kind == "sendPhoto":
                _send_photo_once(
                    token=token,
                    chat_id=chat_id,
                    photo_path=str(payload.get("photo_path") or ""),
                    caption=str(payload.get("caption") or ""),
                )
            else:
                _send_message_once(
                    token=token,
                    chat_id=chat_id,
                    text=str(payload.get("text") or ""),
                )
            return True
        except Exception as e:
            status = ""
            resp = getattr(e, "response", None)
            try:
                if resp is not None and getattr(resp, "status_code", None) is not None:
                    status = f" status={int(resp.status_code)}"
            except Exception:
                status = ""
            print(
                f"[WARN][TELEGRAM] send failed attempt={attempt}/{max_attempt} "
                f"kind={payload_kind} event={str(event_type or '').upper().strip()} "
                f"err={type(e).__name__}: {e}{status}"
            )
            print(traceback.format_exc().rstrip())
            if attempt < max_attempt:
                time.sleep(float(_RETRY_BACKOFF_SEC[attempt - 1]))
    return False


def _flush_spool(force: bool = False, limit: int = _SPOOL_FLUSH_LIMIT) -> int:
    global _LAST_SPOOL_FLUSH_TS, _SPOOL_FLUSHING
    if _SPOOL_FLUSHING:
        return 0

    now_ts = time.time()
    if not force and (now_ts - _LAST_SPOOL_FLUSH_TS) < float(_SPOOL_FLUSH_INTERVAL_SEC):
        return 0
    _LAST_SPOOL_FLUSH_TS = float(now_ts)

    items = _read_spool_items()
    if not items:
        return 0

    _SPOOL_FLUSHING = True
    try:
        limit = max(1, int(limit))
        batch = items[:limit]
        remain_tail = items[limit:]
        remain_head: List[Dict] = []
        sent = 0

        for idx, item in enumerate(batch):
            event_type = str(item.get("event_type") or "UNKNOWN")
            payload_kind = str(item.get("kind") or "sendMessage")
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            ok = _send_with_retry(event_type=event_type, payload_kind=payload_kind, payload=payload)
            if ok:
                sent += 1
                continue
            remain_head = batch[idx:]
            break

        if sent > 0:
            _write_spool_items(remain_head + remain_tail)
        return int(sent)
    finally:
        _SPOOL_FLUSHING = False


def tg_notify(event_type: str, message: str):
    text = str(message or "").strip()
    if not text:
        return False

    code = str(event_type or "").upper().strip()
    _flush_spool(force=False)
    payload = {"text": text}
    ok = _send_with_retry(event_type=event_type, payload_kind="sendMessage", payload=payload)
    if ok:
        if code == "POSITION_CLOSED":
            print("[TG_SEND] event=POSITION_CLOSED")
        _flush_spool(force=True)
        return True

    if code == "POSITION_CLOSED":
        print("[TG_FAIL] event=POSITION_CLOSED queued")
    _append_spool(_spool_item(event_type=event_type, payload_kind="sendMessage", payload=payload))
    _alert_telegram_missed(event_type=event_type, payload_kind="sendMessage")
    return False


def tg_notify_photo(event_type: str, photo_path: str, caption: str = ""):
    payload = {
        "photo_path": str(photo_path or "").strip(),
        "caption": str(caption or "").strip(),
    }
    if not payload["photo_path"]:
        return False

    _flush_spool(force=False)
    ok = _send_with_retry(event_type=event_type, payload_kind="sendPhoto", payload=payload)
    if ok:
        _flush_spool(force=True)
        return True

    _append_spool(_spool_item(event_type=event_type, payload_kind="sendPhoto", payload=payload))
    _alert_telegram_missed(event_type=event_type, payload_kind="sendPhoto")
    return False


def flush_telegram_spool(limit: int = _SPOOL_FLUSH_LIMIT):
    return _flush_spool(force=True, limit=max(1, int(limit)))


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
    buy_krw=None,
):
    msg = build_order_message(
        event_type=event_type,
        strategy_tag=strategy_tag,
        ticker=ticker,
        price=price,
        qty=qty,
        reason=reason,
        buy_krw=buy_krw,
    )
    ok = tg_notify(event_type=event_type, message=msg)
    code = str(event_type or "").upper().strip()
    if code in {"ORDER_BUY_FILLED", "ORDER_SELL_FILLED", "ORDER_BUY_FAILED", "ORDER_SELL_FAILED"}:
        status = "TG_SEND" if ok else "TG_FAIL"
        print(f"[{status}] order {code} {str(ticker or '').upper().strip()}")
    return ok
