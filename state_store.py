# state_store.py
import os
import json
import datetime as dt
import config
from market import get_balance


def now_kst():
    return dt.datetime.now()


def save_state(state: dict, cooldown_until: dict):
    try:
        payload = {
            "state": state,
            "cooldown_until": {k: v.isoformat() for k, v in cooldown_until.items()},
            "saved_at": now_kst().isoformat(),
        }
        with open(config.STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ state 저장 실패: {e}")


def load_state():
    if not os.path.exists(config.STATE_FILE):
        return {}, {}
    try:
        with open(config.STATE_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
        state = payload.get("state", {}) or {}
        cd_raw = payload.get("cooldown_until", {}) or {}
        cooldown_until = {}
        for k, v in cd_raw.items():
            try:
                cooldown_until[k] = dt.datetime.fromisoformat(v)
            except Exception:
                pass
        print(f"📂 state 복구: {len(state)}개")
        return state, cooldown_until
    except Exception as e:
        print(f"⚠️ state 복구 실패: {e}")
        return {}, {}


def verify_state_with_balance(upbit, state: dict):
    """
    복구된 state가 실제 잔고와 맞는지 검증.
    """
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
            fixed += 1
            continue
        if float(s.get("initial_volume", 0.0)) <= 0:
            s["initial_volume"] = vol
            fixed += 1
    if fixed:
        print(f"🧹 state 보정 {fixed}건(잔고기반)")
