"""텔레그램 최소 운영 알림 유틸."""

import config

try:
    import requests
except Exception:
    requests = None


def tg(msg: str):
    if not bool(getattr(config, "TELEGRAM_ENABLED", False)):
        return

    token = str(getattr(config, "TELEGRAM_TOKEN", "") or "").strip()
    try:
        chat_id = int(getattr(config, "TELEGRAM_CHAT_ID", 0))
    except Exception:
        chat_id = 0

    if not token or chat_id == 0:
        return
    if requests is None:
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": str(msg),
        "disable_web_page_preview": True,
    }
    try:
        requests.post(url, data=payload, timeout=5)
    except Exception:
        pass

