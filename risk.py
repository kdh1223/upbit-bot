# risk.py
import config
import pyupbit
from indicators import get_atr
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


def _calc_stop_pct(entry: float, ticker: str, regime: str) -> float:
    fallback = float(getattr(config, "STOP_LOSS_PCT", 0.01))
    stop_pct = fallback
    mode = str(getattr(config, "STOP_LOSS_MODE", "FIXED")).upper().strip()

    if mode == "ATR":
        try:
            period = int(getattr(config, "STOP_LOSS_ATR_PERIOD", 14))
            period = max(2, period)
            df = pyupbit.get_ohlcv(ticker, interval="day", count=period + 5)
            if df is None or len(df) < (period + 1):
                raise ValueError("ohlcv_short")

            atr_val = float(get_atr(df, period=period).iloc[-1])
            if atr_val <= 0:
                raise ValueError("atr_invalid")

            atr_pct = float(atr_val) / float(entry)

            mult_table = getattr(config, "STOP_LOSS_ATR_MULT_TABLE", {}) or {}
            mid_mult = float(mult_table.get("MID", 1.3))
            mult = float(mult_table.get(regime, mid_mult))
            stop_pct = atr_pct * mult

            min_pct = float(getattr(config, "STOP_LOSS_MIN_PCT", fallback))
            max_pct = float(getattr(config, "STOP_LOSS_MAX_PCT", fallback))
            if min_pct > max_pct:
                min_pct, max_pct = max_pct, min_pct
            stop_pct = max(min_pct, min(max_pct, stop_pct))
        except Exception:
            stop_pct = fallback

    if bool(getattr(config, "DEBUG_STOP_PCT", False)):
        print(f"[손절폭] {ticker} stop_pct={stop_pct:.4f}")

    return float(stop_pct)


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
    stop_pct = _calc_stop_pct(entry, ticker, regime)
    if pnl <= -stop_pct:
        if bool(getattr(config, "DEBUG_TRADE_FLOW", False)):
            print(f"[손절] {ticker} pnl={pnl:.4f}")
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
                if bool(getattr(config, "DEBUG_TRADE_FLOW", False)):
                    print(f"[TP1_익절] {ticker} pnl={pnl:.4f}")
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
                if bool(getattr(config, "DEBUG_TRADE_FLOW", False)):
                    print(f"[TP2_익절] {ticker} pnl={pnl:.4f}")
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
            if bool(getattr(config, "DEBUG_TRADE_FLOW", False)):
                print(f"[트레일청산] {ticker} from_peak={from_peak:.4f}")
            ok, err = _execute_sell(upbit, ticker, vol, market_sell, "trail_sell_failed")
            if not ok:
                return {"closed": False, "reason": err}
            _record_realized(state, vol, float(cur))
            if not _is_real_order():
                _mock_reduce_volume(state, vol)

        state["holding"] = False
        return {"closed": True, "reason": "trailing", "exit_price": float(cur)}

    return {"closed": False}
