"""상태 저장/복구, 스키마 마이그레이션, 잔고 정합성 보정을 담당하는 모듈."""

import datetime as dt
import json
import os
from typing import Dict, Tuple
from zoneinfo import ZoneInfo

import config
from market import get_balance


STRATEGIES = ("MAIN", "SCALP")
SCALP_BTC_DT_FIELDS = ("entry_time", "cooldown_until", "paused_until")
KST = ZoneInfo("Asia/Seoul")


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


def _default_risk_state():
    return {
        "peak_equity": 0.0,
        "day_start_equity": 0.0,
        "day_key": "",
        "halted_flag": False,
        "halt_reason": "",
        "halted_at_ts": 0.0,
    }


def now_kst():
    return dt.datetime.now(KST)


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
            parsed = _parse_dt(v)
            if parsed is not None:
                out[k] = parsed
        except Exception:
            continue
    return out


def _parse_dt(value):
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=KST)
        try:
            return value.astimezone(KST)
        except Exception:
            return value
    try:
        parsed = dt.datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=KST)
        return parsed.astimezone(KST)
    except Exception:
        return None


def _parse_ts(value):
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return float(value.timestamp())
    try:
        ts = float(value)
        if ts > 0:
            return ts
    except Exception:
        pass
    try:
        parsed = dt.datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=KST)
        else:
            parsed = parsed.astimezone(KST)
        return float(parsed.timestamp())
    except Exception:
        return None


def _normalize_position_state(raw: dict):
    s = dict(raw or {})
    s["holding"] = bool(s.get("holding", False))

    try:
        entry = float(s.get("entry", 0.0))
    except Exception:
        entry = 0.0
    s["entry"] = float(entry)

    try:
        s["peak"] = float(s.get("peak", entry if entry > 0 else 0.0))
    except Exception:
        s["peak"] = float(entry if entry > 0 else 0.0)

    entry_ts = _parse_ts(s.get("entry_ts"))
    if entry_ts is None and bool(s.get("holding", False)):
        entry_ts = float(now_kst().timestamp())
    s["entry_ts"] = float(entry_ts) if entry_ts is not None else 0.0

    s["trail_armed"] = bool(s.get("trail_armed", False))
    try:
        trail_hwm = float(s.get("trail_hwm", 0.0))
    except Exception:
        trail_hwm = 0.0

    if trail_hwm < 0:
        trail_hwm = 0.0
    if bool(s.get("trail_armed", False)) and trail_hwm <= 0:
        s["trail_armed"] = False

    if not bool(s.get("holding", False)):
        s["trail_armed"] = False
        trail_hwm = 0.0
    s["trail_hwm"] = float(trail_hwm)

    return s


def _normalize_position_map(raw: dict):
    out = {}
    for ticker, pos in (raw or {}).items():
        out[ticker] = _normalize_position_state(pos if isinstance(pos, dict) else {})
    return out


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


def _normalize_risk_state(raw: dict):
    state = dict(raw or {})
    base = _default_risk_state()

    for k, v in base.items():
        state.setdefault(k, v)

    try:
        state["peak_equity"] = max(0.0, float(state.get("peak_equity", 0.0)))
    except Exception:
        state["peak_equity"] = 0.0
    try:
        state["day_start_equity"] = max(0.0, float(state.get("day_start_equity", 0.0)))
    except Exception:
        state["day_start_equity"] = 0.0
    state["day_key"] = str(state.get("day_key") or "")
    state["halted_flag"] = bool(state.get("halted_flag", False))
    state["halt_reason"] = str(state.get("halt_reason") or "")
    try:
        state["halted_at_ts"] = max(0.0, float(state.get("halted_at_ts", 0.0)))
    except Exception:
        state["halted_at_ts"] = 0.0

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


def save_state(
    state: dict,
    cooldown_until: dict,
    inactive_positions: dict = None,
    scalp_btc_state: dict = None,
    risk_state: dict = None,
):
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
                state_out[s] = _normalize_position_map((state or {}).get(s, {}) or {})
        else:
            state_out = _empty_strategy_payload()
            state_out[_legacy_target_strategy()] = _normalize_position_map(state or {})

        if _is_strategy_dict(cooldown_until):
            cd_out = {s: _to_iso_map((cooldown_until or {}).get(s, {}) or {}) for s in STRATEGIES}
        else:
            cd_out = _empty_strategy_payload()
            cd_out[_legacy_target_strategy()] = _to_iso_map(cooldown_until or {})

        payload = {
            "state": state_out,
            "inactive_positions": _normalize_position_map(inactive_positions or {}),
            "cooldown_until": cd_out,
            "scalp_btc": _scalp_btc_to_json(_normalize_scalp_btc_state(scalp_btc_state or {})),
            "risk_state": _normalize_risk_state(risk_state or {}),
            "saved_at": now_kst().isoformat(),
            "schema": 2,
        }
        with open(config.STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[WARN] state save failed: {e}")


def load_state() -> Tuple[Dict[str, dict], Dict[str, dict], dict, dict, dict]:
    if not os.path.exists(config.STATE_FILE):
        return _empty_strategy_payload(), _empty_strategy_payload(), {}, _default_scalp_btc_state(), _default_risk_state()

    try:
        with open(config.STATE_FILE, "r", encoding="utf-8-sig") as f:
            payload = json.load(f)

        inactive_positions = payload.get("inactive_positions", {}) or {}
        state_raw = payload.get("state", {}) or {}
        cd_raw = payload.get("cooldown_until", {}) or {}
        scalp_btc_raw = payload.get("scalp_btc", {}) or {}
        risk_raw = payload.get("risk_state", {}) or {}

        strategy_state = _empty_strategy_payload()
        strategy_cd = _empty_strategy_payload()

        # New schema
        if _is_strategy_dict(state_raw):
            for s in STRATEGIES:
                strategy_state[s] = _normalize_position_map(state_raw.get(s, {}) or {})
        else:
            # Legacy flat schema
            strategy_state[_legacy_target_strategy()] = _normalize_position_map(state_raw or {})

        inactive_positions = _normalize_position_map(inactive_positions)

        if _is_strategy_dict(cd_raw):
            for s in STRATEGIES:
                strategy_cd[s] = _from_iso_map(cd_raw.get(s, {}) or {})
        else:
            strategy_cd[_legacy_target_strategy()] = _from_iso_map(cd_raw or {})

        loaded_main = len(strategy_state["MAIN"])
        loaded_scalp = len(strategy_state["SCALP"])
        print(f"[STATE] loaded positions: MAIN={loaded_main}, SCALP={loaded_scalp}")
        scalp_btc_state = _normalize_scalp_btc_state(scalp_btc_raw)
        risk_state = _normalize_risk_state(risk_raw)
        return strategy_state, strategy_cd, inactive_positions, scalp_btc_state, risk_state
    except Exception as e:
        print(f"[WARN] state load failed: {e}")
        return _empty_strategy_payload(), _empty_strategy_payload(), {}, _default_scalp_btc_state(), _default_risk_state()


def _repair_strategy_state_with_balance(upbit, state: dict) -> int:
    fixed = 0
    for ticker, s in list((state or {}).items()):
        src = s if isinstance(s, dict) else {}
        normalized = _normalize_position_state(src)
        if (not isinstance(s, dict)) or (normalized != src):
            state[ticker] = normalized
            s = state[ticker]
            fixed += 1

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
            s["entry_ts"] = 0.0
            s["trail_armed"] = False
            s["trail_hwm"] = 0.0
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
