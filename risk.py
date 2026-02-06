import config
from market import get_balance


def _get_params_by_regime(regime: str):
    r = regime if regime in config.TP_TABLE else "MID"
    p = config.TP_TABLE[r]
    return float(p["TP1_PCT"]), float(p["TP2_PCT"]), float(p["TRAIL_BACK_PCT"])


def _can_order(cur: float, qty: float) -> bool:
    return (cur * qty) >= float(config.MIN_ORDER_KRW)


def apply_risk_rules(upbit, ticker: str, state: dict, cur: float, market_sell):
    """
    state keys:
      holding, entry, peak, tp1, tp2, regime, initial_volume(optional)
    """
    entry = float(state.get("entry", 0.0))
    if entry <= 0:
        return {"closed": False}

    # 최고가 갱신
    state["peak"] = max(float(state.get("peak", entry)), cur)

    pnl = (cur / entry) - 1.0
    from_peak = (cur / float(state["peak"])) - 1.0

    regime = state.get("regime", "MID")
    tp1, tp2, trail_back = _get_params_by_regime(regime)

    coin = ticker.split("-")[1]
    vol = float(get_balance(upbit, coin))

    # 찌꺼기(dust) 처리: 최소 주문금액 미만이면 매도 불가 → 종료 처리(옵션)
    if vol > 0 and not _can_order(cur, vol):
        if config.DUST_CLOSE_AS_CLOSED:
            state["holding"] = False
            return {"closed": True, "reason": "dust(<min_order)", "exit_price": cur}
        return {"closed": False}

    # 손절(전량)
    if pnl <= -float(config.STOP_LOSS_PCT):
        if vol > 0 and _can_order(cur, vol):
            market_sell(upbit, ticker, vol)
        state["holding"] = False
        return {"closed": True, "reason": "stop_loss", "exit_price": cur}

    # ✅ 1차 익절: initial_volume 기준 (예측 가능한 분할)
    if (not bool(state.get("tp1", False))) and tp1 > 0 and pnl >= tp1:
        initial_vol = float(state.get("initial_volume", vol))
        sell_qty = initial_vol * float(config.TP1_SELL_RATIO)
        sell_qty = min(sell_qty, vol)  # 잔고 초과 방지

        if sell_qty > 0 and _can_order(cur, sell_qty):
            market_sell(upbit, ticker, sell_qty)
            state["tp1"] = True

    # ✅ 2차 익절: TP1 이후 남은 잔고 기준
    if (not bool(state.get("tp2", False))) and tp2 > 0 and pnl >= tp2:
        current_vol = float(get_balance(upbit, coin))
        sell_qty = current_vol * float(config.TP2_SELL_RATIO)

        if sell_qty > 0 and _can_order(cur, sell_qty):
            market_sell(upbit, ticker, sell_qty)
            state["tp2"] = True

    # 트레일링(전량)
    if trail_back > 0 and from_peak <= -trail_back:
        final_vol = float(get_balance(upbit, coin))
        if final_vol > 0 and _can_order(cur, final_vol):
            market_sell(upbit, ticker, final_vol)
        state["holding"] = False
        return {"closed": True, "reason": "trailing", "exit_price": cur}

    return {"closed": False}
