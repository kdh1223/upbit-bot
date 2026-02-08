"""신규/추가 매수 가능 여부와 평균단가 상태 갱신을 처리하는 포지션 유틸."""

import config


def _get_target_krw(per_trade_amt: float) -> float:
    mult = float(getattr(config, "POSITION_TARGET_MULT", 2.0))
    return float(per_trade_amt) * mult


def can_open_new_position(state: dict, ticker: str):
    if state.get(ticker, {}).get("holding", False):
        return False, "already_holding"
    return True, ""


def can_add_position(state: dict, ticker: str, per_trade_amt: float):
    s = state.get(ticker, {})
    if not s.get("holding", False):
        return False, ""

    per_trade_amt = float(per_trade_amt)

    invested_krw = float(s.get("invested_krw", 0.0))
    target_krw = float(s.get("target_krw", 0.0))
    add_count = int(s.get("add_count", 0))

    # State repair for recovery/mismatch
    if invested_krw <= 0:
        invested_krw = per_trade_amt
    if target_krw <= 0:
        target_krw = _get_target_krw(per_trade_amt)
    target_krw = max(target_krw, invested_krw)
    if add_count <= 0:
        add_count = 1

    s["invested_krw"] = float(invested_krw)
    s["target_krw"] = float(target_krw)
    s["add_count"] = int(add_count)

    max_buys = int(getattr(config, "POSITION_MAX_BUY_COUNT", 2))

    if invested_krw >= target_krw - 1e-6:
        return False, "target_allocation_reached"
    if add_count >= max_buys:
        return False, "split_entry_limit"
    return True, ""


def init_position_state(
    entry_price: float,
    initial_vol: float,
    per_trade_amt: float,
    regime: str,
):
    return {
        "holding": True,
        "entry": float(entry_price),
        "peak": float(entry_price),
        "tp1": False,
        "tp2": False,
        "regime": regime,
        "initial_volume": float(initial_vol),
        "invested_krw": float(per_trade_amt),
        "target_krw": float(_get_target_krw(per_trade_amt)),
        "add_count": 1,
        "realized_krw": 0.0,
        "realized_cost_krw": 0.0,
    }


def apply_add_snapshot(state: dict, ticker: str, total_vol: float, avg_buy: float, add_krw: float):
    s = state.get(ticker, {})
    if not s:
        return

    if avg_buy > 0:
        s["entry"] = float(avg_buy)
    if total_vol > 0:
        s["initial_volume"] = float(total_vol)

    s["invested_krw"] = float(s.get("invested_krw", 0.0)) + float(add_krw)
    s["add_count"] = int(s.get("add_count", 0)) + 1


def apply_add_mock(state: dict, ticker: str, add_price: float, add_vol: float, add_krw: float):
    s = state.get(ticker, {})
    if not s:
        return

    prev_vol = float(s.get("initial_volume", 0.0))
    prev_entry = float(s.get("entry", 0.0))
    new_vol = prev_vol + float(add_vol)

    if new_vol > 0:
        new_entry = (prev_entry * prev_vol + float(add_price) * float(add_vol)) / new_vol
        s["entry"] = float(new_entry)
        s["initial_volume"] = float(new_vol)

    s["invested_krw"] = float(s.get("invested_krw", 0.0)) + float(add_krw)
    s["add_count"] = int(s.get("add_count", 0)) + 1
