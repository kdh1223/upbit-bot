"""Entry engine for MAIN/SCALP signal checks and buy execution."""

import time

import config
import pyupbit
import position_manager
from indicators import check_filters, get_rsi, intraday_trend_ok, minute_entry_ok, safe_last, scalp_entry_signal
from strategy import calc_target
from utils.telegram_notify import notify_event, notify_order


def _safe_caches(prices):
    caches = prices.get("_caches")
    if isinstance(caches, tuple) and len(caches) == 3:
        return caches
    # (day_cache, intraday_cache, minute_cache)
    return ({}, {}, {})


def _safe_krw(prices) -> float:
    try:
        return float(prices.get("_krw", 0.0))
    except Exception:
        return 0.0


def _is_blocked_ticker(ticker: str, inactive_tickers, inactive_positions) -> bool:
    return (ticker in inactive_tickers) or (ticker in inactive_positions)


def _is_cooldown_active(now, until) -> bool:
    if until is None:
        return False
    try:
        return bool(now < until)
    except TypeError:
        # Defensive fallback for mixed naive/aware datetime values.
        try:
            return float(now.timestamp()) < float(until.timestamp())
        except Exception:
            return False


def _normalize_main_mode(mode: str) -> str:
    m = str(mode or "CONSERVATIVE").upper().strip()
    if m not in {"AGGRESSIVE", "CONSERVATIVE"}:
        m = "CONSERVATIVE"
    return m


def _normalize_main_tp_ratios(tp1, tp2, runner):
    try:
        a = max(0.0, float(tp1))
    except Exception:
        a = 0.0
    try:
        b = max(0.0, float(tp2))
    except Exception:
        b = 0.0
    try:
        c = max(0.0, float(runner))
    except Exception:
        c = 0.0

    total = a + b + c
    if total <= 0:
        a, b, c = 0.60, 0.30, 0.10
        total = 1.0
    return a / total, b / total, c / total


def _resolve_main_tp_ratios(mode: str):
    normalized_mode = _normalize_main_mode(mode)
    table = getattr(config, "MAIN_TP_RATIOS", {}) or {}
    row = table.get(normalized_mode)
    if not isinstance(row, dict):
        row = table.get("CONSERVATIVE", {})
    if not isinstance(row, dict):
        row = {"TP1": 0.60, "TP2": 0.30, "RUNNER": 0.10}

    return _normalize_main_tp_ratios(
        row.get("TP1", 0.60),
        row.get("TP2", 0.30),
        row.get("RUNNER", 0.10),
    )


def _apply_main_tp_profile_on_entry(state_row: dict, mode: str):
    tp1_ratio, tp2_ratio, runner_ratio = _resolve_main_tp_ratios(mode)
    state_row["entry_mode"] = _normalize_main_mode(mode)
    state_row["tp1_ratio"] = float(tp1_ratio)
    state_row["tp2_ratio"] = float(tp2_ratio)
    state_row["runner_ratio"] = float(runner_ratio)
    state_row["tp1_done"] = False
    state_row["tp2_done"] = False
    state_row["runner_active"] = False
    state_row["runner_hwm"] = 0.0
    state_row["runner_start_ts"] = 0.0
    # Keep legacy flags in sync for compatibility.
    state_row["tp1"] = False
    state_row["tp2"] = False


def _main_1m_confirm_ok(ticker: str) -> tuple[bool, str]:
    interval = str(getattr(config, "MAIN_CONFIRM_1M_INTERVAL", "minute1"))
    rsi_period = max(2, int(getattr(config, "MAIN_CONFIRM_1M_RSI_PERIOD", 14)))
    min_needed = max(30, rsi_period + 3)
    try:
        df = pyupbit.get_ohlcv(ticker, interval=interval, count=min_needed + 10)
    except Exception:
        return True, "FETCH_ERROR"
    if df is None or len(df) < min_needed:
        return True, "DATA_SHORT"

    try:
        rsi = get_rsi(df, rsi_period)
        rsi_now = safe_last(rsi)
        rsi_prev = float(rsi.iloc[-2])
        close_now = safe_last(df["close"])
        close_prev = safe_last(df["close"].iloc[:-1])
        if None in (rsi_now, close_now, close_prev):
            return True, "DATA_NAN"
        if rsi_prev != rsi_prev:
            return True, "RSI_PREV_NAN"
    except Exception:
        return True, "CALC_ERROR"

    delta_min = float(getattr(config, "MAIN_CONFIRM_1M_RSI_DELTA_MIN", 0.3))
    if (float(rsi_now) - float(rsi_prev)) < delta_min:
        return False, "RSI_DELTA_LOW"

    if bool(getattr(config, "MAIN_CONFIRM_1M_REQUIRE_REBOUND", True)):
        if not (float(close_now) > float(close_prev)):
            return False, "REBOUND_NOT_CONFIRMED"

    return True, "OK"


def entry_passes_filters(ticker: str, now, day_cache, intraday_cache, minute_cache, main_mode: str = "CONSERVATIVE") -> bool:
    # Daily filter cache
    cached = day_cache.get(ticker)
    if cached and (now - cached[1]).total_seconds() < float(getattr(config, "DAY_FILTER_CACHE_SEC", 60)):
        ok_day = cached[0]
    else:
        ok_day = bool(check_filters(ticker))
        day_cache[ticker] = (ok_day, now)
    if not ok_day:
        return False

    # Intraday trend cache
    if bool(getattr(config, "USE_INTRADAY_FILTER", False)):
        cached2 = intraday_cache.get(ticker)
        if cached2 and (now - cached2[1]).total_seconds() < float(getattr(config, "INTRADAY_FILTER_CACHE_SEC", 30)):
            ok_4h = cached2[0]
        else:
            try:
                ok_4h = bool(intraday_trend_ok(ticker))
            except Exception:
                ok_4h = True
            intraday_cache[ticker] = (ok_4h, now)
        if not ok_4h:
            return False

    # Minute timing cache
    cached3 = minute_cache.get(ticker)
    if cached3 and (now - cached3[1]).total_seconds() < float(getattr(config, "MINUTE_ENTRY_CACHE_SEC", 10)):
        ok_m = cached3[0]
    else:
        try:
            ok_m = bool(minute_entry_ok(ticker))
        except Exception:
            ok_m = True
        minute_cache[ticker] = (ok_m, now)
    if not ok_m:
        return False

    mode = _normalize_main_mode(main_mode)
    use_1m_confirm = bool(getattr(config, "USE_1M_CONFIRM_FOR_MAIN", True))
    if use_1m_confirm and mode == "CONSERVATIVE":
        key = f"main_1m::{ticker}"
        cached4 = minute_cache.get(key)
        cache_sec = max(0.0, float(getattr(config, "MAIN_CONFIRM_1M_CACHE_SEC", 5)))
        if cached4 and (now - cached4[1]).total_seconds() < cache_sec:
            ok_1m, reason_1m = cached4[0], cached4[2]
        else:
            ok_1m, reason_1m = _main_1m_confirm_ok(ticker)
            minute_cache[key] = (ok_1m, now, str(reason_1m))
        if not ok_1m:
            if bool(getattr(config, "DEBUG_ENTRY_REJECT", False)):
                print(f"[MAIN_1M_CONFIRM_REJECT] {ticker} {reason_1m}")
            return False

    return True


def _execute_buy(
    upbit,
    ticker: str,
    buy_krw: float,
    cur: float,
    wait_for_filled_snapshot_fn,
):
    if bool(getattr(config, "REAL_ORDER", False)):
        upbit.buy_market_order(ticker, buy_krw)
        filled_vol, avg_buy = wait_for_filled_snapshot_fn(upbit, ticker, timeout_sec=3.0, interval=0.2)
        initial_vol = float(filled_vol) if filled_vol > 0 else (float(buy_krw) / float(cur))
        entry_price = float(avg_buy) if avg_buy > 0 else float(cur)
    else:
        initial_vol = float(buy_krw) / float(cur)
        entry_price = float(cur)
    return initial_vol, entry_price


def _allow_add_buy() -> bool:
    return bool(getattr(config, "ALLOW_ADD_BUY", False))


def try_main_entries(
    upbit,
    now,
    universe,
    prices,
    k_map,
    state,
    cooldown_until,
    per_trade_amt,
    max_holdings,
    total_holding_cnt,
    regime,
    wait_for_filled_snapshot_fn,
    save_state_fn,
    inactive_tickers=None,
    inactive_positions=None,
    global_holding_tickers=None,
    before_buy_fn=None,
    entry_params=None,
    main_mode: str = "CONSERVATIVE",
    runtime_risk_state=None,
    equity=None,
):
    runtime_risk_state = runtime_risk_state or {}
    if bool(runtime_risk_state.get("halted_flag", False)):
        reason = str(runtime_risk_state.get("halt_reason") or "RISK_CUT")
        eq_txt = "-"
        try:
            eq_txt = f"{float(equity):,.0f}"
        except Exception:
            pass
        print(f"[ENTRY_BLOCKED] risk halted: {reason}")
        print(f"[RISK_GUARD] ENTRY BLOCKED | reason={reason} | equity={eq_txt}")
        return False

    day_cache, intraday_cache, minute_cache = _safe_caches(prices)
    krw = _safe_krw(prices)
    inactive_tickers = set(inactive_tickers or [])
    inactive_positions = inactive_positions or {}
    global_holding_tickers = set(global_holding_tickers or [])

    if total_holding_cnt >= int(max_holdings):
        return False
    if float(per_trade_amt) <= 0:
        return False
    if krw < float(per_trade_amt):
        return False

    for ticker in universe:
        if _is_blocked_ticker(ticker, inactive_tickers, inactive_positions):
            continue
        if ticker in global_holding_tickers:
            # Shared lock: no same ticker duplicate across strategies.
            if not _allow_add_buy():
                continue
            # Add-buy is enabled only for the strategy already holding the ticker.
            if not state.get(ticker, {}).get("holding", False):
                continue

        until = cooldown_until.get(ticker)
        if _is_cooldown_active(now, until):
            continue

        holding = bool(state.get(ticker, {}).get("holding", False))
        if holding:
            if not _allow_add_buy():
                continue
            can_add, _ = position_manager.can_add_position(state, ticker, per_trade_amt)
            if not can_add:
                continue
        else:
            can_new, _ = position_manager.can_open_new_position(state, ticker)
            if not can_new:
                continue

        if not entry_passes_filters(
            ticker,
            now,
            day_cache,
            intraday_cache,
            minute_cache,
            main_mode=main_mode,
        ):
            continue

        cur = prices.get(ticker)
        if cur is None or float(cur) <= 0:
            continue

        k = float(k_map.get(ticker, getattr(config, "K_DEFAULT", 0.5)))
        try:
            target = float(calc_target(ticker, k))
        except Exception:
            continue
        if float(cur) < target:
            continue

        action = "ADD" if holding else "BUY"
        print(f"[MAIN ENTRY] {action} {ticker} | Regime={regime} | KRW={per_trade_amt:,.0f}")
        try:
            if _is_blocked_ticker(ticker, inactive_tickers, inactive_positions):
                print(f"[BLOCK] inactive ticker buy blocked: {ticker}")
                continue
            if callable(before_buy_fn):
                try:
                    allowed = bool(before_buy_fn(ticker=ticker, buy_krw=float(per_trade_amt), cur=float(cur)))
                except Exception as e:
                    print(f"[WARN] before_buy failed(MAIN): {ticker} err={e}")
                    continue
                if not allowed:
                    continue
            initial_vol, entry_price = _execute_buy(
                upbit=upbit,
                ticker=ticker,
                buy_krw=float(per_trade_amt),
                cur=float(cur),
                wait_for_filled_snapshot_fn=wait_for_filled_snapshot_fn,
            )
        except Exception as e:
            print(f"[WARN] buy failed(MAIN): {ticker} err={e}")
            print(f"[WARN] ORDER failed: BUY {ticker}")
            notify_order(
                event_type="ORDER_BUY_FAILED",
                strategy_tag="MAIN",
                ticker=ticker,
                price=float(cur),
                qty=0.0,
                reason="ENTRY",
            )
            continue

        if holding:
            if bool(getattr(config, "REAL_ORDER", False)):
                position_manager.apply_add_snapshot(
                    state, ticker, float(initial_vol), float(entry_price), float(per_trade_amt)
                )
            else:
                add_vol = float(per_trade_amt) / float(cur)
                position_manager.apply_add_mock(state, ticker, float(cur), float(add_vol), float(per_trade_amt))
        else:
            state[ticker] = position_manager.init_position_state(
                float(entry_price),
                float(initial_vol),
                float(per_trade_amt),
                regime,
                strategy_tag="MAIN",
                entry_ts=float(now.timestamp()),
            )
            state[ticker]["entry_bucket"] = "CORE"
            _apply_main_tp_profile_on_entry(state[ticker], mode=main_mode)
            if isinstance(entry_params, dict):
                state[ticker]["sl_one_pct"] = abs(float(entry_params.get("sl_one", 0.0)))
                # MAIN always uses TP1/TP2/RUNNER staged exits.
                state[ticker]["tp_one_pct"] = None
                state[ticker]["trail_from_pct"] = max(0.0, float(entry_params.get("trail_from", 0.0)))
                state[ticker]["trail_giveback_pct"] = max(0.0, float(entry_params.get("trail_giveback", 0.0)))

        save_state_fn()
        buy_notify_ok = bool(
            notify_order(
            event_type="ORDER_BUY_FILLED",
            strategy_tag="MAIN",
            ticker=ticker,
            price=float(entry_price),
            qty=float(initial_vol),
            reason="ENTRY",
            )
        )
        if not buy_notify_ok:
            print(f"[WARN][TELEGRAM] ORDER_BUY_FILLED queued/failed: MAIN {ticker}")
        if holding:
            notify_event(
                event_type="AVG_DOWN_BUY",
                lines=[
                    "\uC804\uB7B5: MAIN",
                    f"\uC885\uBAA9: {ticker}",
                    f"\uAC00\uACA9: {float(entry_price):,.0f}",
                    f"\uC218\uB7C9: {float(initial_vol):.8f}".rstrip("0").rstrip("."),
                    "\uC0AC\uC720: ENTRY",
                ],
            )
        time.sleep(0.15)
        return True

    return False


def try_scalp_entries(
    upbit,
    now,
    universe,
    prices,
    state,
    cooldown_until,
    per_trade_amt,
    max_holdings,
    total_holding_cnt,
    regime,
    wait_for_filled_snapshot_fn,
    save_state_fn,
    inactive_tickers=None,
    inactive_positions=None,
    global_holding_tickers=None,
    conservative=False,
    runtime_risk_state=None,
    equity=None,
):
    runtime_risk_state = runtime_risk_state or {}
    if bool(runtime_risk_state.get("halted_flag", False)):
        reason = str(runtime_risk_state.get("halt_reason") or "RISK_CUT")
        eq_txt = "-"
        try:
            eq_txt = f"{float(equity):,.0f}"
        except Exception:
            pass
        print(f"[ENTRY_BLOCKED] risk halted: {reason}")
        print(f"[RISK_GUARD] ENTRY BLOCKED | reason={reason} | equity={eq_txt}")
        return False

    if not bool(getattr(config, "USE_MINUTE_TEST_STRATEGY", False)):
        return False

    krw = _safe_krw(prices)
    inactive_tickers = set(inactive_tickers or [])
    inactive_positions = inactive_positions or {}
    global_holding_tickers = set(global_holding_tickers or [])
    buy_krw = float(per_trade_amt)

    if total_holding_cnt >= int(max_holdings):
        return False
    if buy_krw <= 0:
        return False
    if krw < buy_krw:
        return False

    for ticker in universe:
        if _is_blocked_ticker(ticker, inactive_tickers, inactive_positions):
            continue
        if ticker in global_holding_tickers:
            # Shared lock: never duplicate ticker in SCALP.
            continue

        until = cooldown_until.get(ticker)
        if _is_cooldown_active(now, until):
            continue

        can_new, _ = position_manager.can_open_new_position(state, ticker)
        if not can_new:
            continue

        cur = prices.get(ticker)
        if cur is None or float(cur) <= 0:
            continue

        try:
            sig = bool(scalp_entry_signal(ticker, conservative=bool(conservative)))
        except Exception:
            sig = False
        if not sig:
            continue

        print(
            f"[SCALP ENTRY] BUY {ticker} | Regime={regime} | KRW={buy_krw:,.0f} "
            f"| cons={'Y' if conservative else 'N'}"
        )
        strategy_tag = "SCALP_BTC" if str(ticker).upper() == "KRW-BTC" else "SCALP"
        try:
            if _is_blocked_ticker(ticker, inactive_tickers, inactive_positions):
                print(f"[BLOCK] inactive ticker buy blocked(SCALP): {ticker}")
                continue
            initial_vol, entry_price = _execute_buy(
                upbit=upbit,
                ticker=ticker,
                buy_krw=buy_krw,
                cur=float(cur),
                wait_for_filled_snapshot_fn=wait_for_filled_snapshot_fn,
            )
        except Exception as e:
            print(f"[WARN] buy failed(SCALP): {ticker} err={e}")
            print(f"[WARN] ORDER failed: BUY {ticker}")
            notify_order(
                event_type="ORDER_BUY_FAILED",
                strategy_tag=strategy_tag,
                ticker=ticker,
                price=float(cur),
                qty=0.0,
                reason="ENTRY",
            )
            continue

        state[ticker] = position_manager.init_position_state(
            float(entry_price),
            float(initial_vol),
            float(buy_krw),
            regime,
            strategy_tag=strategy_tag,
            entry_ts=float(now.timestamp()),
        )
        state[ticker]["entry_bucket"] = "SURGE"
        save_state_fn()
        buy_notify_ok = bool(
            notify_order(
            event_type="ORDER_BUY_FILLED",
            strategy_tag=strategy_tag,
            ticker=ticker,
            price=float(entry_price),
            qty=float(initial_vol),
            reason="ENTRY",
            )
        )
        if not buy_notify_ok:
            print(f"[WARN][TELEGRAM] ORDER_BUY_FILLED queued/failed: {strategy_tag} {ticker}")
        time.sleep(0.10)
        return True

    return False

