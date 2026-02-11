"""손절/익절/트레일링과 매도 의사결정을 수행하는 리스크 규칙 모듈."""

# risk.py
import datetime as dt
import time

import config
import pyupbit
from indicators import get_atr
from market import get_balance
from utils.telegram_notify import notify_order


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
        live_vol = _as_nonneg_float(get_balance(upbit, coin), 0.0)
        if live_vol > 0:
            return float(live_vol)

        # Upbit balance can transiently return 0 right after partial fills.
        # Fall back to state-based remaining qty to avoid false FORCE_CLOSE settlement.
        fallback_vol = _estimated_remaining_qty_from_state(state)
        if fallback_vol > 0:
            return float(fallback_vol)
        return 0.0
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


def _as_nonneg_float(value, default: float = 0.0) -> float:
    try:
        return max(0.0, float(value))
    except Exception:
        return max(0.0, float(default))


def _estimated_remaining_qty_from_state(state: dict) -> float:
    entry = _as_nonneg_float(state.get("entry", 0.0), 0.0)

    qty_hint = _as_nonneg_float(state.get("qty", 0.0), 0.0)
    init_vol = _as_nonneg_float(state.get("initial_volume", 0.0), 0.0)
    buy_krw = _as_nonneg_float(state.get("total_buy_krw", state.get("invested_krw", 0.0)), 0.0)

    base_qty = 0.0
    if qty_hint > 0:
        base_qty = float(qty_hint)
    elif entry > 0 and buy_krw > 0:
        base_qty = float(buy_krw / entry)
    elif init_vol > 0:
        base_qty = float(init_vol)

    if base_qty <= 0:
        return 0.0

    sold_qty = 0.0
    if entry > 0:
        realized_cost = _as_nonneg_float(state.get("realized_cost_krw", 0.0), 0.0)
        if realized_cost > 0:
            sold_qty = float(realized_cost / entry)

    return max(0.0, float(base_qty) - float(sold_qty))


def _ensure_position_accounting(state: dict, strategy_tag: str):
    state["total_buy_krw"] = _as_nonneg_float(state.get("total_buy_krw", state.get("invested_krw", 0.0)), 0.0)
    state["total_sell_krw"] = _as_nonneg_float(state.get("total_sell_krw", 0.0), 0.0)
    state["last_exit_reason"] = str(state.get("last_exit_reason") or "")
    tag = str(state.get("strategy_tag") or strategy_tag or "MAIN").upper().strip()
    state["strategy_tag"] = tag or "MAIN"


def _get_state_pct(state: dict, key: str, default=None):
    if key not in state:
        return default
    try:
        value = float(state.get(key))
    except Exception:
        return default
    if value <= 0:
        return default
    return float(value)


def _record_sell_krw(state: dict, qty: float, price: float):
    q = _as_nonneg_float(qty, 0.0)
    p = _as_nonneg_float(price, 0.0)
    if q <= 0 or p <= 0:
        return
    state["total_sell_krw"] = _as_nonneg_float(state.get("total_sell_krw", 0.0), 0.0) + (q * p)


def _reason_code_from_fail_reason(fail_reason: str) -> str:
    raw = str(fail_reason or "").lower()
    if "tp2_partial" in raw:
        return "TP2_PARTIAL"
    if "runner_trail" in raw:
        return "RUNNER_TRAIL"
    if "runner_timeout" in raw:
        return "RUNNER_TIMEOUT"
    if "tp1" in raw:
        return "TP1"
    if "tp2" in raw:
        return "TP2"
    if "trail" in raw:
        return "TRAILING"
    if "stop" in raw:
        return "SL"
    return "ENTRY"


def _execute_sell(
    upbit,
    ticker: str,
    qty: float,
    market_sell,
    fail_reason: str,
    strategy_tag: str,
    cur_price: float,
):
    reason_code = _reason_code_from_fail_reason(fail_reason)
    try:
        ok = bool(market_sell(upbit, ticker, qty))
    except Exception as e:
        if _is_real_order():
            print(f"⚠️ SELL 실패: {ticker} reason={fail_reason}")
            notify_order(
                event_type="ORDER_SELL_FAILED",
                strategy_tag=strategy_tag,
                ticker=ticker,
                price=cur_price,
                qty=qty,
                reason=reason_code,
            )
            return False, f"{fail_reason}:{e}"
        ok = False

    if _is_real_order() and not ok:
        print(f"⚠️ SELL 실패: {ticker} reason={fail_reason}")
        notify_order(
            event_type="ORDER_SELL_FAILED",
            strategy_tag=strategy_tag,
            ticker=ticker,
            price=cur_price,
            qty=qty,
            reason=reason_code,
        )
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


def _parse_timestamp(value):
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
        return float(parsed.timestamp())
    except Exception:
        return None


def _now_ts(now=None) -> float:
    ts = _parse_timestamp(now)
    if ts is None:
        ts = float(time.time())
    return float(ts)


def _ensure_trail_state(state: dict, now_ts: float):
    entry_ts = _parse_timestamp(state.get("entry_ts"))
    if entry_ts is None:
        entry_ts = float(now_ts)
    state["entry_ts"] = float(entry_ts)

    state["trail_armed"] = bool(state.get("trail_armed", False))
    try:
        state["trail_hwm"] = max(0.0, float(state.get("trail_hwm", 0.0)))
    except Exception:
        state["trail_hwm"] = 0.0

    # Invalid armed state repair.
    if bool(state.get("trail_armed", False)) and float(state.get("trail_hwm", 0.0)) <= 0:
        state["trail_armed"] = False
        state["trail_hwm"] = 0.0


def _resolve_trail_drawdown_pct(regime_trail_back: float) -> float:
    raw = getattr(config, "TRAIL_DRAWDOWN_PCT", None)
    if raw is not None:
        try:
            return max(0.0, float(raw))
        except Exception:
            pass

    try:
        return max(0.0, float(regime_trail_back) * 100.0)
    except Exception:
        pass

    return 0.6


def _is_main_strategy(strategy_tag: str) -> bool:
    return str(strategy_tag or "").upper().strip() == "MAIN"


def _normalize_main_mode(mode: str) -> str:
    m = str(mode or "CONSERVATIVE").upper().strip()
    if m not in {"AGGRESSIVE", "CONSERVATIVE"}:
        m = "CONSERVATIVE"
    return m


def _normalize_main_ratios(tp1, tp2, runner):
    a = _as_nonneg_float(tp1, 0.0)
    b = _as_nonneg_float(tp2, 0.0)
    c = _as_nonneg_float(runner, 0.0)
    total = a + b + c
    if total <= 0:
        return 0.60, 0.30, 0.10
    return float(a / total), float(b / total), float(c / total)


def _main_ratios_from_mode(mode: str):
    normalized_mode = _normalize_main_mode(mode)
    table = getattr(config, "MAIN_TP_RATIOS", {}) or {}
    row = table.get(normalized_mode)
    if not isinstance(row, dict):
        row = table.get("CONSERVATIVE", {})
    if not isinstance(row, dict):
        row = {"TP1": 0.60, "TP2": 0.30, "RUNNER": 0.10}
    return _normalize_main_ratios(
        row.get("TP1", 0.60),
        row.get("TP2", 0.30),
        row.get("RUNNER", 0.10),
    )


def _ensure_main_stage_state(state: dict, now_ts: float, cur: float):
    mode = _normalize_main_mode(state.get("entry_mode"))
    state["entry_mode"] = mode

    fallback_tp1, fallback_tp2, fallback_runner = _main_ratios_from_mode(mode)
    state_tp1, state_tp2, state_runner = _normalize_main_ratios(
        state.get("tp1_ratio", fallback_tp1),
        state.get("tp2_ratio", fallback_tp2),
        state.get("runner_ratio", fallback_runner),
    )
    state["tp1_ratio"] = float(state_tp1)
    state["tp2_ratio"] = float(state_tp2)
    state["runner_ratio"] = float(state_runner)

    tp1_done = bool(state.get("tp1_done", state.get("tp1", False)))
    tp2_done = bool(state.get("tp2_done", state.get("tp2", False)))
    state["tp1_done"] = bool(tp1_done)
    state["tp2_done"] = bool(tp2_done)
    state["tp1"] = bool(tp1_done)
    state["tp2"] = bool(tp2_done)

    state["runner_active"] = bool(state.get("runner_active", False))
    state["runner_hwm"] = _as_nonneg_float(state.get("runner_hwm", 0.0), 0.0)
    runner_start_ts = _parse_timestamp(state.get("runner_start_ts"))
    state["runner_start_ts"] = float(runner_start_ts) if runner_start_ts is not None else 0.0

    if not bool(state.get("holding", False)):
        state["runner_active"] = False
        state["runner_hwm"] = 0.0
        state["runner_start_ts"] = 0.0
        return

    # Recovery path: if TP2 already done but runner fields were missing, re-arm safely.
    if state["tp2_done"] and (not state["runner_active"]):
        state["runner_active"] = True
        state["runner_hwm"] = max(float(cur), _as_nonneg_float(state.get("runner_hwm", 0.0), 0.0))
        if float(state.get("runner_start_ts", 0.0)) <= 0:
            state["runner_start_ts"] = float(now_ts)


def _should_arm_runner_after_tp1(state: dict) -> bool:
    tp2_ratio = _as_nonneg_float(state.get("tp2_ratio", 0.0), 0.0)
    runner_ratio = _as_nonneg_float(state.get("runner_ratio", 0.0), 0.0)
    return (tp2_ratio <= 1e-12) and (runner_ratio > 0.0)


def _estimate_entry_qty(state: dict, entry: float) -> float:
    qty_hint = _as_nonneg_float(state.get("qty", 0.0), 0.0)
    if qty_hint > 0:
        return float(qty_hint)
    if entry > 0:
        total_buy_krw = _as_nonneg_float(state.get("total_buy_krw", state.get("invested_krw", 0.0)), 0.0)
        if total_buy_krw > 0:
            return float(total_buy_krw / entry)
    return _as_nonneg_float(state.get("initial_volume", 0.0), 0.0)


def apply_risk_rules(upbit, ticker: str, state: dict, cur: float, market_sell, now=None, strategy_tag: str = "MAIN"):
    now_ts = _now_ts(now)
    entry = float(state.get("entry", 0.0))
    if entry <= 0:
        return {"closed": False}
    _ensure_position_accounting(state, strategy_tag=strategy_tag)
    tag = str(state.get("strategy_tag") or strategy_tag or "MAIN").upper().strip() or "MAIN"
    is_main = _is_main_strategy(tag)

    _ensure_trail_state(state, now_ts)
    state["peak"] = max(float(state.get("peak", entry)), float(cur))
    elapsed_sec = max(0.0, float(now_ts) - float(state.get("entry_ts", now_ts)))

    pnl = (float(cur) / entry) - 1.0
    pnl_pct = pnl * 100.0

    regime = state.get("regime", "MID")
    tp1, tp2, trail_back = _get_params_by_regime(regime)
    sl_one = _get_state_pct(state, "sl_one_pct", None)
    tp_one = _get_state_pct(state, "tp_one_pct", None)
    trail_from = _get_state_pct(state, "trail_from_pct", None)
    trail_giveback = _get_state_pct(state, "trail_giveback_pct", None)
    trail_arm_sec = max(0.0, float(getattr(config, "TRAIL_ARM_SEC", 120)))
    trail_arm_pct = float(getattr(config, "TRAIL_ARM_PCT", 0.5))
    partials = []

    if is_main:
        _ensure_main_stage_state(state, now_ts=now_ts, cur=float(cur))
        tp_one = None

    vol = _get_volume(upbit, ticker, state)

    if vol > 0 and not _can_order(float(cur), vol):
        if bool(getattr(config, "DUST_CLOSE_AS_CLOSED", True)):
            state["holding"] = False
            state["last_exit_reason"] = "FORCE_CLOSE"
            state["runner_active"] = False
            return {
                "closed": True,
                "reason": "dust(<min_order)",
                "exit_price": float(cur),
                "close_qty": float(vol),
                "partials": partials,
            }
        return {"closed": False, "partials": partials}

    # 1) stop loss full close
    # In the early post-entry window, force fixed SL only (no ATR stop widening/tightening).
    if elapsed_sec < trail_arm_sec:
        stop_pct = float(sl_one) if sl_one is not None else float(getattr(config, "STOP_LOSS_PCT", 0.01))
    else:
        stop_pct = float(sl_one) if sl_one is not None else _calc_stop_pct(entry, ticker, regime)
    if pnl <= -stop_pct:
        if bool(getattr(config, "DEBUG_TRADE_FLOW", False)):
            print(f"[손절] {ticker} pnl={pnl:.4f}")
        if vol > 0 and _can_order(float(cur), vol):
            ok, err = _execute_sell(
                upbit,
                ticker,
                vol,
                market_sell,
                "stop_loss_sell_failed",
                strategy_tag=tag,
                cur_price=float(cur),
            )
            if not ok:
                return {"closed": False, "reason": err, "partials": partials}
            _record_realized(state, vol, float(cur))
            _record_sell_krw(state, vol, float(cur))
            if not _is_real_order():
                _mock_reduce_volume(state, vol)

        state["holding"] = False
        state["runner_active"] = False
        state["last_exit_reason"] = "STOPLOSS"
        return {
            "closed": True,
            "reason": "stoploss",
            "exit_price": float(cur),
            "close_qty": float(max(0.0, vol)),
            "partials": partials,
        }

    if is_main:
        def _close_main_dust_after_partial():
            vol_now = _get_volume(upbit, ticker, state)
            if vol_now <= 0:
                state["holding"] = False
                state["runner_active"] = False
                state["last_exit_reason"] = "FORCE_CLOSE"
                return {
                    "closed": True,
                    "reason": "dust(<min_order)",
                    "exit_price": float(cur),
                    "close_qty": 0.0,
                    "partials": partials,
                }
            if _can_order(float(cur), vol_now):
                return None
            if not bool(getattr(config, "DUST_CLOSE_AS_CLOSED", True)):
                return None
            state["holding"] = False
            state["runner_active"] = False
            state["last_exit_reason"] = "FORCE_CLOSE"
            return {
                "closed": True,
                "reason": "dust(<min_order)",
                "exit_price": float(cur),
                "close_qty": float(vol_now),
                "partials": partials,
            }

        # MAIN staged TP1 partial
        if (not bool(state.get("tp1_done", False))) and tp1 > 0 and pnl >= tp1:
            vol = _get_volume(upbit, ticker, state)
            if vol > 0:
                base_qty = _estimate_entry_qty(state, entry)
                target_qty = max(0.0, float(base_qty) * float(state.get("tp1_ratio", 0.0)))
                sell_qty = min(float(target_qty), float(vol))
                if sell_qty > 0 and _can_order(float(cur), sell_qty):
                    if bool(getattr(config, "DEBUG_TRADE_FLOW", False)):
                        print(f"[TP1_익절][MAIN] {ticker} pnl={pnl:.4f}")
                    ok, err = _execute_sell(
                        upbit,
                        ticker,
                        sell_qty,
                        market_sell,
                        "tp1_sell_failed",
                        strategy_tag=tag,
                        cur_price=float(cur),
                    )
                    if not ok:
                        return {"closed": False, "reason": err, "partials": partials}
                    _record_realized(state, sell_qty, float(cur))
                    _record_sell_krw(state, sell_qty, float(cur))
                    if not _is_real_order():
                        _mock_reduce_volume(state, sell_qty)
                    state["tp1_done"] = True
                    state["tp1"] = True
                    notify_order(
                        event_type="ORDER_PARTIAL_FILL",
                        strategy_tag=tag,
                        ticker=ticker,
                        price=float(cur),
                        qty=float(sell_qty),
                        reason="TP1",
                    ) if bool(getattr(config, "TELEGRAM_PARTIAL_NOTIFY", False)) else None
                    state["tp1_pnl_pct"] = float(pnl * 100.0)
                    partials.append(
                        {
                            "reason": "TP1",
                            "qty": float(sell_qty),
                            "price": float(cur),
                            "pnl_pct": float(pnl * 100.0),
                        }
                    )
                    dust_closed = _close_main_dust_after_partial()
                    if dust_closed:
                        return dust_closed

                    if _should_arm_runner_after_tp1(state):
                        state["runner_active"] = True
                        state["runner_hwm"] = max(
                            _as_nonneg_float(state.get("runner_hwm", 0.0), 0.0),
                            float(cur),
                        )
                        if _as_nonneg_float(state.get("runner_start_ts", 0.0), 0.0) <= 0:
                            state["runner_start_ts"] = float(now_ts)

        # MAIN staged TP2 partial -> activate runner
        if (not bool(state.get("tp2_done", False))) and tp2 > 0 and pnl >= tp2:
            vol = _get_volume(upbit, ticker, state)
            if vol > 0:
                base_qty = _estimate_entry_qty(state, entry)
                target_qty = max(0.0, float(base_qty) * float(state.get("tp2_ratio", 0.0)))
                # Never allow TP2 step to close the position fully.
                max_sell = max(0.0, float(vol) * (1.0 - 1e-9))
                sell_qty = min(float(target_qty), float(max_sell))
                if sell_qty > 0 and _can_order(float(cur), sell_qty):
                    if bool(getattr(config, "DEBUG_TRADE_FLOW", False)):
                        print(f"[TP2_익절][MAIN] {ticker} pnl={pnl:.4f}")
                    ok, err = _execute_sell(
                        upbit,
                        ticker,
                        sell_qty,
                        market_sell,
                        "tp2_partial_sell_failed",
                        strategy_tag=tag,
                        cur_price=float(cur),
                    )
                    if not ok:
                        return {"closed": False, "reason": err, "partials": partials}
                    _record_realized(state, sell_qty, float(cur))
                    _record_sell_krw(state, sell_qty, float(cur))
                    if not _is_real_order():
                        _mock_reduce_volume(state, sell_qty)
                    state["tp2_done"] = True
                    state["tp2"] = True
                    state["runner_active"] = True
                    state["runner_hwm"] = float(cur)
                    state["runner_start_ts"] = float(now_ts)
                    notify_order(
                        event_type="ORDER_PARTIAL_FILL",
                        strategy_tag=tag,
                        ticker=ticker,
                        price=float(cur),
                        qty=float(sell_qty),
                        reason="TP2_PARTIAL",
                    ) if bool(getattr(config, "TELEGRAM_PARTIAL_NOTIFY", False)) else None
                    state["tp2_pnl_pct"] = float(pnl * 100.0)
                    partials.append(
                        {
                            "reason": "TP2_PARTIAL",
                            "qty": float(sell_qty),
                            "price": float(cur),
                            "pnl_pct": float(pnl * 100.0),
                        }
                    )
                    dust_closed = _close_main_dust_after_partial()
                    if dust_closed:
                        return dust_closed

        # MAIN runner management: trailing giveback and timeout exit.
        if bool(state.get("runner_active", False)):
            state["runner_hwm"] = max(
                _as_nonneg_float(state.get("runner_hwm", 0.0), 0.0),
                float(cur),
            )
            runner_hwm = _as_nonneg_float(state.get("runner_hwm", 0.0), 0.0)
            trail_giveback = max(0.0, float(getattr(config, "MAIN_RUNNER_TRAIL_GIVEBACK_PCT", 0.007)))
            trail_hit = False
            if runner_hwm > 0 and trail_giveback > 0:
                cut_price = float(runner_hwm) * (1.0 - float(trail_giveback))
                trail_hit = float(cur) <= float(cut_price)

            if trail_hit:
                vol = _get_volume(upbit, ticker, state)
                if vol > 0 and _can_order(float(cur), vol):
                    ok, err = _execute_sell(
                        upbit,
                        ticker,
                        vol,
                        market_sell,
                        "runner_trail_sell_failed",
                        strategy_tag=tag,
                        cur_price=float(cur),
                    )
                    if not ok:
                        return {"closed": False, "reason": err, "partials": partials}
                    _record_realized(state, vol, float(cur))
                    _record_sell_krw(state, vol, float(cur))
                    if not _is_real_order():
                        _mock_reduce_volume(state, vol)
                state["holding"] = False
                state["runner_active"] = False
                state["last_exit_reason"] = "RUNNER_TRAIL"
                return {
                    "closed": True,
                    "reason": "RUNNER_TRAIL",
                    "exit_price": float(cur),
                    "close_qty": float(max(0.0, vol)),
                    "partials": partials,
                }

            runner_start_ts = _as_nonneg_float(state.get("runner_start_ts", 0.0), 0.0)
            max_hold_min = max(0.0, float(getattr(config, "MAIN_RUNNER_MAX_HOLD_MIN", 120)))
            timeout_sec = max_hold_min * 60.0
            timeout_pnl_min = float(getattr(config, "MAIN_RUNNER_TIMEOUT_CLOSE_IF_PNL_GE", 0.0))
            timeout_hit = False
            if runner_start_ts > 0 and timeout_sec > 0:
                timeout_hit = (now_ts - runner_start_ts) >= timeout_sec and pnl >= timeout_pnl_min

            if timeout_hit:
                vol = _get_volume(upbit, ticker, state)
                if vol > 0 and _can_order(float(cur), vol):
                    ok, err = _execute_sell(
                        upbit,
                        ticker,
                        vol,
                        market_sell,
                        "runner_timeout_sell_failed",
                        strategy_tag=tag,
                        cur_price=float(cur),
                    )
                    if not ok:
                        return {"closed": False, "reason": err, "partials": partials}
                    _record_realized(state, vol, float(cur))
                    _record_sell_krw(state, vol, float(cur))
                    if not _is_real_order():
                        _mock_reduce_volume(state, vol)
                state["holding"] = False
                state["runner_active"] = False
                state["last_exit_reason"] = "RUNNER_TIMEOUT"
                return {
                    "closed": True,
                    "reason": "RUNNER_TIMEOUT",
                    "exit_price": float(cur),
                    "close_qty": float(max(0.0, vol)),
                    "partials": partials,
                }

        return {"closed": False, "partials": partials}

    # 1b) One-shot take profit full close
    if tp_one is not None and pnl >= float(tp_one):
        if bool(getattr(config, "DEBUG_TRADE_FLOW", False)):
            print(f"[TP_ONE] {ticker} pnl={pnl:.4f}")
        if vol > 0 and _can_order(float(cur), vol):
            ok, err = _execute_sell(
                upbit,
                ticker,
                vol,
                market_sell,
                "tp_one_sell_failed",
                strategy_tag=strategy_tag,
                cur_price=float(cur),
            )
            if not ok:
                return {"closed": False, "reason": err}
            _record_realized(state, vol, float(cur))
            _record_sell_krw(state, vol, float(cur))
            if not _is_real_order():
                _mock_reduce_volume(state, vol)

        state["holding"] = False
        state["last_exit_reason"] = "TP2"
        return {
            "closed": True,
            "reason": "tp2",
            "exit_price": float(cur),
            "close_qty": float(max(0.0, vol)),
        }

    # 2) TP1 partial
    if (tp_one is None) and (not bool(state.get("tp1", False))) and tp1 > 0 and pnl >= tp1:
        vol = _get_volume(upbit, ticker, state)
        if vol > 0:
            sell_qty = min(vol * float(getattr(config, "TP1_SELL_RATIO", 0.5)), vol)
            if sell_qty > 0 and _can_order(float(cur), sell_qty):
                if bool(getattr(config, "DEBUG_TRADE_FLOW", False)):
                    print(f"[TP1_익절] {ticker} pnl={pnl:.4f}")
                ok, err = _execute_sell(
                    upbit,
                    ticker,
                    sell_qty,
                    market_sell,
                    "tp1_sell_failed",
                    strategy_tag=strategy_tag,
                    cur_price=float(cur),
                )
                if not ok:
                    return {"closed": False, "reason": err}
                _record_realized(state, sell_qty, float(cur))
                _record_sell_krw(state, sell_qty, float(cur))
                if not _is_real_order():
                    _mock_reduce_volume(state, sell_qty)
                state["tp1"] = True
                notify_order(
                    event_type="ORDER_PARTIAL_FILL",
                    strategy_tag=strategy_tag,
                    ticker=ticker,
                    price=float(cur),
                    qty=float(sell_qty),
                    reason="TP1",
                ) if bool(getattr(config, "TELEGRAM_PARTIAL_NOTIFY", False)) else None
                state["tp1_pnl_pct"] = float(pnl * 100.0)

    # 3) TP2 partial
    if (tp_one is None) and (not bool(state.get("tp2", False))) and tp2 > 0 and pnl >= tp2:
        vol = _get_volume(upbit, ticker, state)
        if vol > 0:
            sell_qty = vol * float(getattr(config, "TP2_SELL_RATIO", 0.5))
            if sell_qty > 0 and _can_order(float(cur), sell_qty):
                if bool(getattr(config, "DEBUG_TRADE_FLOW", False)):
                    print(f"[TP2_익절] {ticker} pnl={pnl:.4f}")
                ok, err = _execute_sell(
                    upbit,
                    ticker,
                    sell_qty,
                    market_sell,
                    "tp2_sell_failed",
                    strategy_tag=strategy_tag,
                    cur_price=float(cur),
                )
                if not ok:
                    return {"closed": False, "reason": err}
                _record_realized(state, sell_qty, float(cur))
                _record_sell_krw(state, sell_qty, float(cur))
                if not _is_real_order():
                    _mock_reduce_volume(state, sell_qty)
                state["tp2"] = True
                notify_order(
                    event_type="ORDER_PARTIAL_FILL",
                    strategy_tag=strategy_tag,
                    ticker=ticker,
                    price=float(cur),
                    qty=float(sell_qty),
                    reason="TP2",
                ) if bool(getattr(config, "TELEGRAM_PARTIAL_NOTIFY", False)) else None
                state["tp2_pnl_pct"] = float(pnl * 100.0)

    # 4) Trailing arm/close: arm only after elapsed time + minimum profit threshold.
    if (not bool(state.get("trail_armed", False))) and elapsed_sec >= trail_arm_sec:
        if trail_from is not None:
            arm_ok = pnl >= float(trail_from)
        else:
            arm_ok = pnl_pct >= trail_arm_pct
        if arm_ok:
            state["trail_armed"] = True
            state["trail_hwm"] = float(cur)
            if bool(getattr(config, "DEBUG_TRADE_FLOW", False)):
                print(
                    f"[TRAIL_ARM] {ticker} pnl={pnl_pct:+.2f}% elapsed={elapsed_sec:.0f}s hwm={float(cur):.6f}"
                )

    # Never evaluate trailing before arm.
    if bool(state.get("trail_armed", False)):
        state["trail_hwm"] = max(float(state.get("trail_hwm", float(cur))), float(cur))
        trail_hwm = float(state.get("trail_hwm", float(cur)))

        if trail_giveback is not None:
            drawdown_pct = ((float(cur) / trail_hwm) - 1.0) * 100.0 if trail_hwm > 0 else 0.0
            cut_price = trail_hwm * (1.0 - float(trail_giveback))
            hit = trail_hwm > 0 and float(cur) <= cut_price
        else:
            trail_drawdown_pct = _resolve_trail_drawdown_pct(trail_back)
            if trail_drawdown_pct <= 0:
                hit = False
            else:
                drawdown_pct = ((float(cur) / trail_hwm) - 1.0) * 100.0 if trail_hwm > 0 else 0.0
                cut_price = trail_hwm * (1.0 - (trail_drawdown_pct / 100.0))
                hit = trail_hwm > 0 and float(cur) <= cut_price

        if hit:
            vol = _get_volume(upbit, ticker, state)
            if vol > 0 and _can_order(float(cur), vol):
                if bool(getattr(config, "DEBUG_TRADE_FLOW", False)):
                    print(f"[트레일청산] {ticker} drawdown={drawdown_pct:.3f}% hwm={trail_hwm:.6f}")
                ok, err = _execute_sell(
                    upbit,
                    ticker,
                    vol,
                    market_sell,
                    "trail_sell_failed",
                    strategy_tag=strategy_tag,
                    cur_price=float(cur),
                )
                if not ok:
                    return {"closed": False, "reason": err}
                _record_realized(state, vol, float(cur))
                _record_sell_krw(state, vol, float(cur))
                if not _is_real_order():
                    _mock_reduce_volume(state, vol)

            state["holding"] = False
            state["last_exit_reason"] = "TRAILING"
            return {
                "closed": True,
                "reason": "trailing",
                "exit_price": float(cur),
                "close_qty": float(max(0.0, vol)),
            }

    return {"closed": False}

