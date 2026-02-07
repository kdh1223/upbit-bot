# risk.py
import config
from market import get_balance


def _get_params_by_regime(regime: str):
    r = regime if regime in config.TP_TABLE else "MID"
    p = config.TP_TABLE[r]
    return float(p["TP1_PCT"]), float(p["TP2_PCT"]), float(p["TRAIL_BACK_PCT"])


def _can_order(cur: float, qty: float) -> bool:
    return (cur * qty) >= float(config.MIN_ORDER_KRW)


def _is_real_order() -> bool:
    return bool(getattr(config, "REAL_ORDER", False))


def _get_volume(upbit, ticker: str, state: dict) -> float:
    if _is_real_order():
        coin = ticker.split("-")[1]
        return float(get_balance(upbit, coin))
    return float(state.get("initial_volume", 0.0))


def _mock_reduce_volume(state: dict, qty: float):
    v = float(state.get("initial_volume", 0.0))
    v = max(0.0, v - float(qty))
    state["initial_volume"] = v


def _record_realized(state: dict, qty: float, price: float):
    q = float(qty)
    p = float(price)
    if q <= 0 or p <= 0:
        return
    entry = float(state.get("entry", 0.0))
    state["realized_krw"] = float(state.get("realized_krw", 0.0)) + (q * p)
    state["realized_cost_krw"] = float(state.get("realized_cost_krw", 0.0)) + (q * entry)


def _execute_sell(upbit, ticker: str, qty: float, market_sell, fail_reason: str):
    try:
        ok = bool(market_sell(upbit, ticker, qty))
    except Exception as e:
        if _is_real_order():
            return False, f"{fail_reason}:{e}"
        ok = False

    if _is_real_order() and not ok:
        return False, f"{fail_reason}:returned_false"
    return True, ""


def apply_risk_rules(upbit, ticker: str, state: dict, cur: float, market_sell):
    entry = float(state.get("entry", 0.0))
    if entry <= 0:
        return {"closed": False}

    state["peak"] = max(float(state.get("peak", entry)), float(cur))

    pnl = (float(cur) / entry) - 1.0
    from_peak = (float(cur) / float(state["peak"])) - 1.0

    regime = state.get("regime", "MID")
    tp1, tp2, trail_back = _get_params_by_regime(regime)

    vol = _get_volume(upbit, ticker, state)

    if vol > 0 and not _can_order(float(cur), vol):
        if bool(getattr(config, "DUST_CLOSE_AS_CLOSED", True)):
            state["holding"] = False
            return {"closed": True, "reason": "dust(<min_order)", "exit_price": float(cur)}
        return {"closed": False}

    # 1) stop loss full close
    if pnl <= -float(getattr(config, "STOP_LOSS_PCT", 0.01)):
        if vol > 0 and _can_order(float(cur), vol):
            ok, err = _execute_sell(upbit, ticker, vol, market_sell, "stop_loss_sell_failed")
            if not ok:
                return {"closed": False, "reason": err}
            _record_realized(state, vol, float(cur))
            if not _is_real_order():
                _mock_reduce_volume(state, vol)

        state["holding"] = False
        return {"closed": True, "reason": "stop_loss", "exit_price": float(cur)}

    # 2) TP1 partial
    if (not bool(state.get("tp1", False))) and tp1 > 0 and pnl >= tp1:
        vol = _get_volume(upbit, ticker, state)
        if vol > 0:
            sell_qty = min(vol * float(getattr(config, "TP1_SELL_RATIO", 0.5)), vol)
            if sell_qty > 0 and _can_order(float(cur), sell_qty):
                ok, err = _execute_sell(upbit, ticker, sell_qty, market_sell, "tp1_sell_failed")
                if not ok:
                    return {"closed": False, "reason": err}
                _record_realized(state, sell_qty, float(cur))
                if not _is_real_order():
                    _mock_reduce_volume(state, sell_qty)
                state["tp1"] = True

    # 3) TP2 partial
    if (not bool(state.get("tp2", False))) and tp2 > 0 and pnl >= tp2:
        vol = _get_volume(upbit, ticker, state)
        if vol > 0:
            sell_qty = vol * float(getattr(config, "TP2_SELL_RATIO", 0.5))
            if sell_qty > 0 and _can_order(float(cur), sell_qty):
                ok, err = _execute_sell(upbit, ticker, sell_qty, market_sell, "tp2_sell_failed")
                if not ok:
                    return {"closed": False, "reason": err}
                _record_realized(state, sell_qty, float(cur))
                if not _is_real_order():
                    _mock_reduce_volume(state, sell_qty)
                state["tp2"] = True

    # 4) trailing full close
    if trail_back > 0 and from_peak <= -trail_back:
        vol = _get_volume(upbit, ticker, state)
        if vol > 0 and _can_order(float(cur), vol):
            ok, err = _execute_sell(upbit, ticker, vol, market_sell, "trail_sell_failed")
            if not ok:
                return {"closed": False, "reason": err}
            _record_realized(state, vol, float(cur))
            if not _is_real_order():
                _mock_reduce_volume(state, vol)

        state["holding"] = False
        return {"closed": True, "reason": "trailing", "exit_price": float(cur)}

    return {"closed": False}
