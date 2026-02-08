"""상태 저장/복구, 스키마 마이그레이션, 잔고 정합성 보정을 담당하는 모듈."""

import datetime as dt
import json
import os
from typing import Dict, Tuple

import config
from market import get_balance


STRATEGIES = ("MAIN", "SCALP")
SCALP_BTC_DT_FIELDS = ("entry_time", "cooldown_until", "paused_until")


def _default_scalp_btc_state():
    return {
        "holding": False,
        "ticker": str(getattr(config, "SCALP_BTC_TICKER", "KRW-BTC")),
        "entry_price": 0.0,
        "qty": 0.0,
        "entry_time": None,
        "peak_price": 0.0,
        "cooldown_until": None,
        "loss_streak": 0,
        "paused_until": None,
        "switch_fail_count": 0,
    }


def now_kst():
    return dt.datetime.now()


def _empty_strategy_payload():
    return {k: {} for k in STRATEGIES}


def _to_iso_map(dt_map: dict):
    out = {}
    for k, v in (dt_map or {}).items():
        try:
            out[k] = v.isoformat()
        except Exception:
            continue
    return out


def _from_iso_map(raw: dict):
    out = {}
    for k, v in (raw or {}).items():
        try:
            out[k] = dt.datetime.fromisoformat(v)
        except Exception:
            continue
    return out


def _parse_dt(value):
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value
    try:
        return dt.datetime.fromisoformat(str(value))
    except Exception:
        return None


def _normalize_scalp_btc_state(raw: dict):
    state = dict(raw or {})
    base = _default_scalp_btc_state()

    # Idempotent migration: fill only missing fields.
    for k, v in base.items():
        state.setdefault(k, v)

    # Type repair.
    state["holding"] = bool(state.get("holding", False))
    state["ticker"] = str(state.get("ticker") or base["ticker"])
    try:
        state["entry_price"] = float(state.get("entry_price", 0.0))
    except Exception:
        state["entry_price"] = 0.0
    try:
        state["qty"] = float(state.get("qty", 0.0))
    except Exception:
        state["qty"] = 0.0
    try:
        state["peak_price"] = float(state.get("peak_price", 0.0))
    except Exception:
        state["peak_price"] = 0.0
    try:
        state["loss_streak"] = int(state.get("loss_streak", 0))
    except Exception:
        state["loss_streak"] = 0
    try:
        state["switch_fail_count"] = int(state.get("switch_fail_count", 0))
    except Exception:
        state["switch_fail_count"] = 0

    for key in SCALP_BTC_DT_FIELDS:
        state[key] = _parse_dt(state.get(key))
    return state


def _scalp_btc_to_json(state: dict):
    out = dict(state or {})
    for key in SCALP_BTC_DT_FIELDS:
        v = out.get(key)
        if isinstance(v, dt.datetime):
            out[key] = v.isoformat()
        elif v is None:
            out[key] = None
        else:
            try:
                out[key] = dt.datetime.fromisoformat(str(v)).isoformat()
            except Exception:
                out[key] = None
    return out


def _is_strategy_dict(value) -> bool:
    if not isinstance(value, dict):
        return False
    if not value:
        return False
    return any(k in value for k in STRATEGIES)


def _legacy_target_strategy() -> str:
    mode = str(getattr(config, "BOT_MODE", "TEST")).upper().strip()
    if mode == "MAIN":
        return "MAIN"
    return "SCALP"


def save_state(state: dict, cooldown_until: dict, inactive_positions: dict = None, scalp_btc_state: dict = None):
    """
    New format:
    - state: {"MAIN": {...}, "SCALP": {...}}
    - cooldown_until: {"MAIN": {...dt}, "SCALP": {...dt}}

    Backward compatible:
    - legacy flat dict inputs are still accepted and saved under one strategy.
    """
    try:
        if _is_strategy_dict(state):
            state_out = _empty_strategy_payload()
            for s in STRATEGIES:
                state_out[s] = dict((state or {}).get(s, {}) or {})
        else:
            state_out = _empty_strategy_payload()
            state_out[_legacy_target_strategy()] = dict(state or {})

        if _is_strategy_dict(cooldown_until):
            cd_out = {s: _to_iso_map((cooldown_until or {}).get(s, {}) or {}) for s in STRATEGIES}
        else:
            cd_out = _empty_strategy_payload()
            cd_out[_legacy_target_strategy()] = _to_iso_map(cooldown_until or {})

        payload = {
            "state": state_out,
            "inactive_positions": inactive_positions or {},
            "cooldown_until": cd_out,
            "scalp_btc": _scalp_btc_to_json(_normalize_scalp_btc_state(scalp_btc_state or {})),
            "saved_at": now_kst().isoformat(),
            "schema": 2,
        }
        with open(config.STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[WARN] state save failed: {e}")


def load_state() -> Tuple[Dict[str, dict], Dict[str, dict], dict, dict]:
    if not os.path.exists(config.STATE_FILE):
        return _empty_strategy_payload(), _empty_strategy_payload(), {}, _default_scalp_btc_state()

    try:
        with open(config.STATE_FILE, "r", encoding="utf-8-sig") as f:
            payload = json.load(f)

        inactive_positions = payload.get("inactive_positions", {}) or {}
        state_raw = payload.get("state", {}) or {}
        cd_raw = payload.get("cooldown_until", {}) or {}
        scalp_btc_raw = payload.get("scalp_btc", {}) or {}

        strategy_state = _empty_strategy_payload()
        strategy_cd = _empty_strategy_payload()

        # New schema
        if _is_strategy_dict(state_raw):
            for s in STRATEGIES:
                strategy_state[s] = dict(state_raw.get(s, {}) or {})
        else:
            # Legacy flat schema
            strategy_state[_legacy_target_strategy()] = dict(state_raw or {})

        if _is_strategy_dict(cd_raw):
            for s in STRATEGIES:
                strategy_cd[s] = _from_iso_map(cd_raw.get(s, {}) or {})
        else:
            strategy_cd[_legacy_target_strategy()] = _from_iso_map(cd_raw or {})

        loaded_main = len(strategy_state["MAIN"])
        loaded_scalp = len(strategy_state["SCALP"])
        print(f"[STATE] loaded positions: MAIN={loaded_main}, SCALP={loaded_scalp}")
        scalp_btc_state = _normalize_scalp_btc_state(scalp_btc_raw)
        return strategy_state, strategy_cd, inactive_positions, scalp_btc_state
    except Exception as e:
        print(f"[WARN] state load failed: {e}")
        return _empty_strategy_payload(), _empty_strategy_payload(), {}, _default_scalp_btc_state()


def _repair_strategy_state_with_balance(upbit, state: dict) -> int:
    fixed = 0
    for ticker, s in list((state or {}).items()):
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

    return fixed


def verify_state_with_balance(upbit, state):
    """
    Backward compatible:
    - input may be flat legacy dict or strategy dict.
    """
    if _is_strategy_dict(state):
        fixed_total = 0
        for s in STRATEGIES:
            fixed_total += _repair_strategy_state_with_balance(upbit, state.get(s, {}) or {})
        if fixed_total:
            print(f"[STATE] repaired fields: {fixed_total}")
        return

    fixed = _repair_strategy_state_with_balance(upbit, state or {})
    if fixed:
        print(f"[STATE] repaired fields: {fixed}")
