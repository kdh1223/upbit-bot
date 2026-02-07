# state_store.py
import datetime as dt
import json
import os

import config
from market import get_balance


def now_kst():
    return dt.datetime.now()


def save_state(state: dict, cooldown_until: dict, inactive_positions: dict = None):
    try:
        payload = {
            "state": state,
            "inactive_positions": inactive_positions or {},
            "cooldown_until": {k: v.isoformat() for k, v in cooldown_until.items()},
            "saved_at": now_kst().isoformat(),
        }
        with open(config.STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[WARN] state save failed: {e}")


def load_state():
    if not os.path.exists(config.STATE_FILE):
        return {}, {}, {}

    try:
        # Accept both plain UTF-8 and UTF-8 with BOM.
        with open(config.STATE_FILE, "r", encoding="utf-8-sig") as f:
            payload = json.load(f)

        state = payload.get("state", {}) or {}
        inactive_positions = payload.get("inactive_positions", {}) or {}
        cd_raw = payload.get("cooldown_until", {}) or {}
        cooldown_until = {}

        for k, v in cd_raw.items():
            try:
                cooldown_until[k] = dt.datetime.fromisoformat(v)
            except Exception:
                pass

        print(f"[STATE] loaded positions: {len(state)}")
        return state, cooldown_until, inactive_positions
    except Exception as e:
        print(f"[WARN] state load failed: {e}")
        return {}, {}, {}


def verify_state_with_balance(upbit, state: dict):
    fixed = 0

    for ticker, s in list(state.items()):
        if not s.get("holding"):
            continue

        coin = ticker.split("-")[1]
        vol = float(get_balance(upbit, coin))

        if vol <= 0:
            s["holding"] = False
            s["add_count"] = 0
            s["invested_krw"] = 0.0
            s["target_krw"] = 0.0
            s["initial_volume"] = 0.0
            s["realized_krw"] = 0.0
            s["realized_cost_krw"] = 0.0
            fixed += 1
            continue

        if float(s.get("initial_volume", 0.0)) <= 0:
            s["initial_volume"] = vol
            fixed += 1

        if "realized_krw" not in s:
            s["realized_krw"] = 0.0
            fixed += 1

        if "realized_cost_krw" not in s:
            s["realized_cost_krw"] = 0.0
            fixed += 1

    if fixed:
        print(f"[STATE] repaired fields: {fixed}")
