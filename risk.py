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
    """
    실주문: 실제 잔고
    모의: state["initial_volume"]를 잔고로 취급
    """
    if _is_real_order():
        coin = ticker.split("-")[1]
        return float(get_balance(upbit, coin))
    return float(state.get("initial_volume", 0.0))


def _mock_reduce_volume(state: dict, qty: float):
    """
    모의모드에서만: initial_volume을 '잔고'처럼 차감
    """
    v = float(state.get("initial_volume", 0.0))
    v = max(0.0, v - float(qty))
    state["initial_volume"] = v


def apply_risk_rules(upbit, ticker: str, state: dict, cur: float, market_sell):
    """
    state keys(권장):
      holding(bool), entry(float), peak(float), tp1(bool), tp2(bool), regime(str),
      initial_volume(float)  # 실주문에서는 체결 후 잔고 스냅샷, 모의에서는 가상 잔고 역할
    """
    entry = float(state.get("entry", 0.0))
    if entry <= 0:
        return {"closed": False}

    # 최고가 갱신
    state["peak"] = max(float(state.get("peak", entry)), float(cur))

    pnl = (float(cur) / entry) - 1.0
    from_peak = (float(cur) / float(state["peak"])) - 1.0

    regime = state.get("regime", "MID")
    tp1, tp2, trail_back = _get_params_by_regime(regime)

    # 잔고(실제 or 모의)
    vol = _get_volume(upbit, ticker, state)

    # 찌꺼기(dust) 처리: 최소 주문금액 미만이면 매도 불가 → 종료 처리(옵션)
    if vol > 0 and not _can_order(float(cur), vol):
        if bool(getattr(config, "DUST_CLOSE_AS_CLOSED", True)):
            state["holding"] = False
            return {"closed": True, "reason": "dust(<min_order)", "exit_price": float(cur)}
        return {"closed": False}

    # =========================
    # 1) 손절(전량)
    # =========================
    if pnl <= -float(getattr(config, "STOP_LOSS_PCT", 0.01)):
        if vol > 0 and _can_order(float(cur), vol):
            try:
                market_sell(upbit, ticker, vol)
            except Exception as e:
                # 실주문에서는 실패 시 포지션을 닫지 않는 편이 안전
                if _is_real_order():
                    return {"closed": False, "reason": f"stop_loss_sell_failed:{e}"}
            if not _is_real_order():
                _mock_reduce_volume(state, vol)

        state["holding"] = False
        return {"closed": True, "reason": "stop_loss", "exit_price": float(cur)}

    # =========================
    # 2) 1차 익절 (initial_volume 기준)
    # =========================
    if (not bool(state.get("tp1", False))) and tp1 > 0 and pnl >= tp1:
        vol = _get_volume(upbit, ticker, state)
        if vol > 0:
            sell_qty = vol * float(getattr(config, "TP1_SELL_RATIO", 0.5))
            sell_qty = min(sell_qty, vol)

            if sell_qty > 0 and _can_order(float(cur), sell_qty):
                try:
                    market_sell(upbit, ticker, sell_qty)
                except Exception as e:
                    if _is_real_order():
                        return {"closed": False, "reason": f"tp1_sell_failed:{e}"}
                if not _is_real_order():
                    _mock_reduce_volume(state, sell_qty)

                state["tp1"] = True

    # =========================
    # 3) 2차 익절 (남은 잔고 기준)
    # =========================
    if (not bool(state.get("tp2", False))) and tp2 > 0 and pnl >= tp2:
        vol = _get_volume(upbit, ticker, state)
        if vol > 0:
            sell_qty = vol * float(getattr(config, "TP2_SELL_RATIO", 0.5))

            if sell_qty > 0 and _can_order(float(cur), sell_qty):
                try:
                    market_sell(upbit, ticker, sell_qty)
                except Exception as e:
                    if _is_real_order():
                        return {"closed": False, "reason": f"tp2_sell_failed:{e}"}
                if not _is_real_order():
                    _mock_reduce_volume(state, sell_qty)

                state["tp2"] = True

    # =========================
    # 4) 트레일링(전량)
    # =========================
    if trail_back > 0 and from_peak <= -trail_back:
        vol = _get_volume(upbit, ticker, state)
        if vol > 0 and _can_order(float(cur), vol):
            try:
                market_sell(upbit, ticker, vol)
            except Exception as e:
                if _is_real_order():
                    return {"closed": False, "reason": f"trail_sell_failed:{e}"}
            if not _is_real_order():
                _mock_reduce_volume(state, vol)

        state["holding"] = False
        return {"closed": True, "reason": "trailing", "exit_price": float(cur)}

    return {"closed": False}
