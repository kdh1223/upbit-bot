"""Entry engine for MAIN/SCALP signal checks and buy execution."""

import time

import config
import pyupbit
import position_manager
from indicators import (
    check_filters,
    check_filters_with_reason,
    get_rsi,
    h4_trend_ok,
    intraday_trend_ok,
    minute_entry_ok,
    minute_entry_score,
    safe_last,
    scalp_entry_signal,
    vol_ok_recent,
)
from strategy import calc_target
from utils.telegram_notify import notify_event, notify_order

_SURGE_GUARD_LAST_LOG = {}


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


def _safe_pct_change(cur: float, base: float) -> float:
    try:
        c = float(cur)
        b = float(base)
        if b <= 0:
            return 0.0
        return ((c / b) - 1.0) * 100.0
    except Exception:
        return 0.0


def _surge_guard_exempt_tickers() -> set:
    raw = getattr(config, "SURGE_EXTRA_GUARD_EXEMPT_TICKERS", {"KRW-BTC", "KRW-ETH", "KRW-XRP"})
    if isinstance(raw, str):
        items = [x.strip().upper() for x in raw.split(",")]
        return {x for x in items if x}
    if isinstance(raw, (list, tuple, set)):
        out = set()
        for x in raw:
            s = str(x).strip().upper()
            if s:
                out.add(s)
        return out
    return {"KRW-BTC", "KRW-ETH", "KRW-XRP"}


def _log_surge_guard_block_once(ticker: str, reason: str, now):
    key = f"{str(ticker).upper()}::{str(reason)}"
    try:
        bucket = now.replace(second=0, microsecond=0)
    except Exception:
        bucket = str(now)
    if _SURGE_GUARD_LAST_LOG.get(key) == bucket:
        return
    _SURGE_GUARD_LAST_LOG[key] = bucket
    print(f"[ENTRY_BLOCK][MAIN] reason={reason} ticker={ticker}")


def _set_main_filter_reason(minute_cache: dict, ticker: str, reason: str, now):
    minute_cache[f"main_reason::{ticker}"] = (str(reason or "UNKNOWN"), now)


def _clear_main_filter_reason(minute_cache: dict, ticker: str):
    minute_cache.pop(f"main_reason::{ticker}", None)


def _get_main_filter_reason(minute_cache: dict, ticker: str) -> str:
    raw = minute_cache.get(f"main_reason::{ticker}")
    if isinstance(raw, tuple) and len(raw) >= 1:
        return str(raw[0] or "UNKNOWN")
    return "UNKNOWN"


def _get_main_entry_score_cached(ticker: str, now, minute_cache: dict):
    """
    Return (score:int, reasons:list[str], err_reason:str|None) using main_score cache.
    """
    score_key = f"main_score::{ticker}"
    cache_sec = max(0.0, float(getattr(config, "ENTRY_SCORE_CACHE_SEC", 5)))
    cached_score = minute_cache.get(score_key)
    if isinstance(cached_score, tuple) and len(cached_score) == 3:
        try:
            if (now - cached_score[2]).total_seconds() < cache_sec:
                score = int(cached_score[0])
                reasons = list(cached_score[1]) if isinstance(cached_score[1], (list, tuple)) else []
                return score, reasons, None
        except Exception:
            pass

    try:
        interval = str(getattr(config, "ENTRY_SCORE_INTERVAL", "minute1"))
        lookback = max(25, int(getattr(config, "ENTRY_SCORE_LOOKBACK", 40)))
        df = pyupbit.get_ohlcv(ticker, interval=interval, count=lookback)
        score, reasons, _ = minute_entry_score(df, cfg=config)
        score = int(score)
        reasons = list(reasons or [])
        minute_cache[score_key] = (score, reasons, now)
        return score, reasons, None
    except Exception:
        return 0, [], "ENTRY_SCORE_CALC_ERR"


def _day_ma_soft_bypass_ok(ticker: str, now, minute_cache: dict):
    """
    Conservative DAY_MA_FAIL bypass gate.
    Returns: (ok:bool, score:int, h4_ok:bool|None, vol_ok:bool|None)
    """
    if not bool(getattr(config, "DAY_MA_SOFT_BYPASS_ENABLED", False)):
        return False, 0, None, None

    cooldown_min = max(1, int(getattr(config, "DAY_MA_BYPASS_COOLDOWN_MIN", 60)))
    cooldown_key = f"day_ma_bypass::{ticker}"
    last_bypass = minute_cache.get(cooldown_key)
    if last_bypass is not None:
        try:
            if (now - last_bypass).total_seconds() < (cooldown_min * 60.0):
                return False, 0, None, None
        except Exception:
            pass

    score, _reasons, score_err = _get_main_entry_score_cached(ticker, now, minute_cache)
    if score_err is not None:
        return False, 0, None, None

    req_score = max(1, int(getattr(config, "DAY_MA_BYPASS_REQUIRE_ENTRY_SCORE", 3)))
    if int(score) < int(req_score):
        return False, int(score), None, None

    h4_ok = None
    if bool(getattr(config, "DAY_MA_BYPASS_REQUIRE_H4_TREND", True)):
        h4_key = f"day_ma_bypass_h4::{ticker}"
        cached_h4 = minute_cache.get(h4_key)
        if isinstance(cached_h4, tuple) and len(cached_h4) == 2:
            try:
                if (now - cached_h4[1]).total_seconds() < 60.0:
                    h4_ok = bool(cached_h4[0])
            except Exception:
                h4_ok = None
        if h4_ok is None:
            try:
                df4h = pyupbit.get_ohlcv(ticker, interval="minute240", count=80)
            except Exception:
                df4h = None
            h4_ok = bool(h4_trend_ok(df4h))
            minute_cache[h4_key] = (bool(h4_ok), now)
        if not bool(h4_ok):
            return False, int(score), False, None

    vol_ok = None
    if bool(getattr(config, "DAY_MA_BYPASS_REQUIRE_VOL_OK", True)):
        vol_key = f"day_ma_bypass_vol::{ticker}"
        cached_vol = minute_cache.get(vol_key)
        if isinstance(cached_vol, tuple) and len(cached_vol) == 2:
            try:
                if (now - cached_vol[1]).total_seconds() < 20.0:
                    vol_ok = bool(cached_vol[0])
            except Exception:
                vol_ok = None
        if vol_ok is None:
            interval = str(getattr(config, "ENTRY_SCORE_INTERVAL", "minute1"))
            lookback = max(25, int(getattr(config, "ENTRY_SCORE_LOOKBACK", 40)))
            try:
                df_vol = pyupbit.get_ohlcv(ticker, interval=interval, count=lookback)
            except Exception:
                df_vol = None
            vol_ok = bool(vol_ok_recent(df_vol))
            minute_cache[vol_key] = (bool(vol_ok), now)
        if not bool(vol_ok):
            return False, int(score), bool(h4_ok) if h4_ok is not None else None, False

    minute_cache[cooldown_key] = now
    return True, int(score), bool(h4_ok) if h4_ok is not None else None, bool(vol_ok) if vol_ok is not None else None


def _main_surge_extra_guard_ok(ticker: str, now, minute_cache) -> tuple[bool, str]:
    if not bool(getattr(config, "SURGE_EXTRA_GUARD_ENABLED", True)):
        return True, "DISABLED"

    t = str(ticker or "").upper().strip()
    if not t:
        return True, "EMPTY"

    if t in _surge_guard_exempt_tickers():
        return True, "EXEMPT"

    interval = str(getattr(config, "SURGE_EXTRA_GUARD_INTERVAL", "minute1"))
    cache_sec = max(0.0, float(getattr(config, "SURGE_EXTRA_GUARD_CACHE_SEC", 5)))
    cache_key = f"main_surge_guard::{t}"
    cached = minute_cache.get(cache_key)
    if cached and (now - cached[1]).total_seconds() < cache_sec:
        return bool(cached[0]), str(cached[2])

    try:
        df = pyupbit.get_ohlcv(t, interval=interval, count=8)
    except Exception:
        minute_cache[cache_key] = (True, now, "FETCH_ERROR")
        return True, "FETCH_ERROR"

    if df is None or len(df) < 6:
        minute_cache[cache_key] = (True, now, "DATA_SHORT")
        return True, "DATA_SHORT"

    try:
        close_now = float(df["close"].iloc[-1])
        close_prev_1m = float(df["close"].iloc[-2])
        close_prev_5m = float(df["close"].iloc[-6])
        open_now = float(df["open"].iloc[-1])
    except Exception:
        minute_cache[cache_key] = (True, now, "DATA_NAN")
        return True, "DATA_NAN"

    move_1m = abs(_safe_pct_change(close_now, close_prev_1m))
    move_5m = abs(_safe_pct_change(close_now, close_prev_5m))
    body_now = abs(_safe_pct_change(close_now, open_now))

    lim_1m = max(0.1, float(getattr(config, "SURGE_EXTRA_GUARD_1M_MAX_PCT", 3.0)))
    lim_5m = max(0.1, float(getattr(config, "SURGE_EXTRA_GUARD_5M_MAX_PCT", 10.0)))
    lim_body = max(0.1, float(getattr(config, "SURGE_EXTRA_GUARD_BODY_MAX_PCT", 2.5)))

    if move_1m >= lim_1m:
        reason = f"SURGE_GUARD_1M({move_1m:.2f}%>={lim_1m:.2f}%)"
        minute_cache[cache_key] = (False, now, reason)
        return False, reason
    if move_5m >= lim_5m:
        reason = f"SURGE_GUARD_5M({move_5m:.2f}%>={lim_5m:.2f}%)"
        minute_cache[cache_key] = (False, now, reason)
        return False, reason
    if body_now >= lim_body:
        reason = f"SURGE_GUARD_BODY({body_now:.2f}%>={lim_body:.2f}%)"
        minute_cache[cache_key] = (False, now, reason)
        return False, reason

    minute_cache[cache_key] = (True, now, "OK")
    return True, "OK"


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


def _resolve_small_equity_tp_override(equity):
    if not bool(getattr(config, "SMALL_EQUITY_MAIN_TP_PROFILE_ENABLED", False)):
        return None

    try:
        eq = float(equity)
    except Exception:
        return None

    max_equity = float(getattr(config, "SMALL_EQUITY_MAIN_TP_MAX_EQUITY", 200_000))
    if eq <= 0 or eq > max_equity:
        return None

    row = getattr(config, "SMALL_EQUITY_MAIN_TP_RATIOS", None)
    if not isinstance(row, dict):
        row = {"TP1": 0.60, "TP2": 0.00, "RUNNER": 0.40}

    return _normalize_main_tp_ratios(
        row.get("TP1", 0.60),
        row.get("TP2", 0.00),
        row.get("RUNNER", 0.40),
    )


def _apply_main_tp_profile_on_entry(state_row: dict, mode: str, equity=None):
    tp1_ratio, tp2_ratio, runner_ratio = _resolve_main_tp_ratios(mode)
    small_equity_ratios = _resolve_small_equity_tp_override(equity)
    if small_equity_ratios is not None:
        tp1_ratio, tp2_ratio, runner_ratio = small_equity_ratios

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
    day_cache_sec = float(getattr(config, "DAY_FILTER_CACHE_SEC", 60))
    day_reason_key = f"main_day_reason::{ticker}"
    day_reason = None
    if cached and (now - cached[1]).total_seconds() < day_cache_sec:
        ok_day = bool(cached[0])
        if not ok_day:
            cached_reason = minute_cache.get(day_reason_key)
            if isinstance(cached_reason, tuple) and len(cached_reason) >= 2:
                try:
                    if (now - cached_reason[1]).total_seconds() < day_cache_sec:
                        day_reason = str(cached_reason[0] or "DAY_FILTER_FAIL")
                except Exception:
                    day_reason = None
            if day_reason is None:
                ok_day_refetch, day_reason_refetch = check_filters_with_reason(ticker)
                ok_day = bool(ok_day_refetch)
                day_reason = str(day_reason_refetch or "DAY_FILTER_FAIL")
                day_cache[ticker] = (ok_day, now)
                minute_cache[day_reason_key] = (day_reason, now)
    else:
        ok_day, day_reason = check_filters_with_reason(ticker)
        ok_day = bool(ok_day)
        day_reason = str(day_reason or "DAY_FILTER_FAIL")
        day_cache[ticker] = (ok_day, now)
        minute_cache[day_reason_key] = (day_reason, now)
    if not ok_day:
        # Conservative bypass: only allow DAY_MA_FAIL to proceed when strict
        # minute/H4/volume conditions and cooldown pass.
        if str(day_reason) == "DAY_MA_FAIL" and bool(getattr(config, "DAY_MA_SOFT_BYPASS_ENABLED", False)):
            bypass_ok, bypass_score, bypass_h4_ok, bypass_vol_ok = _day_ma_soft_bypass_ok(ticker, now, minute_cache)
            if bypass_ok:
                req_score = max(1, int(getattr(config, "DAY_MA_BYPASS_REQUIRE_ENTRY_SCORE", 3)))
                _set_main_filter_reason(minute_cache, ticker, f"DAY_MA_BYPASS(score>={req_score})", now)
                if bool(getattr(config, "DAY_MA_BYPASS_LOG", True)):
                    h4_txt = "SKIP" if bypass_h4_ok is None else ("OK" if bool(bypass_h4_ok) else "FAIL")
                    vol_txt = "SKIP" if bypass_vol_ok is None else ("OK" if bool(bypass_vol_ok) else "FAIL")
                    print(f"[DAY_MA_BYPASS][MAIN] ticker={ticker} score={int(bypass_score)} h4={h4_txt} vol={vol_txt}")
            else:
                _set_main_filter_reason(minute_cache, ticker, "DAY_MA_FAIL", now)
                return False
        else:
            _set_main_filter_reason(minute_cache, ticker, str(day_reason or "DAY_FILTER_FAIL"), now)
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
            _set_main_filter_reason(minute_cache, ticker, "INTRADAY_TREND_FAIL", now)
            return False

    # Minute timing cache / score gate
    if bool(getattr(config, "ENTRY_SCORE_ENABLED", True)) and bool(getattr(config, "ENTRY_SCORE_REPLACE_MINUTE_OK", True)):
        score, reasons, score_err = _get_main_entry_score_cached(ticker, now, minute_cache)
        if score_err is not None:
            _set_main_filter_reason(minute_cache, ticker, str(score_err), now)
            return False

        min_score = max(1, int(getattr(config, "MAIN_MIN_ENTRY_SCORE", 2)))
        if int(score) < int(min_score):
            if reasons:
                reason = f"ENTRY_SCORE_{int(score)}/{int(min_score)}:{'+'.join([str(x) for x in reasons])}"
            else:
                reason = f"ENTRY_SCORE_{int(score)}/{int(min_score)}:NONE"
            _set_main_filter_reason(minute_cache, ticker, reason, now)
            return False
    else:
        cached3 = minute_cache.get(ticker)
        if cached3 and (now - cached3[1]).total_seconds() < float(getattr(config, "MINUTE_ENTRY_CACHE_SEC", 10)):
            ok_m = bool(cached3[0])
        else:
            try:
                ok_m = bool(minute_entry_ok(ticker))
            except Exception:
                ok_m = True
            minute_cache[ticker] = (ok_m, now)
        if not ok_m:
            _set_main_filter_reason(minute_cache, ticker, "MINUTE_NOT_OK", now)
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
            _set_main_filter_reason(minute_cache, ticker, f"MAIN_1M_CONFIRM_{str(reason_1m)}", now)
            if bool(getattr(config, "DEBUG_ENTRY_REJECT", False)):
                print(f"[MAIN_1M_CONFIRM_REJECT] {ticker} {reason_1m}")
            return False

    _clear_main_filter_reason(minute_cache, ticker)
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

        surge_guard_ok, surge_guard_reason = _main_surge_extra_guard_ok(ticker, now, minute_cache)
        if not surge_guard_ok:
            _log_surge_guard_block_once(ticker, surge_guard_reason, now)
            continue

        if not entry_passes_filters(
            ticker,
            now,
            day_cache,
            intraday_cache,
            minute_cache,
            main_mode=main_mode,
        ):
            if bool(getattr(config, "ENTRY_SCORE_LOG_BLOCKED", True)):
                reason = _get_main_filter_reason(minute_cache, ticker)
                block_key = f"main_block_log::{ticker}::{reason}"
                last_logged = minute_cache.get(block_key)
                should_log = True
                if isinstance(last_logged, type(now)):
                    try:
                        should_log = (now - last_logged).total_seconds() >= 60.0
                    except Exception:
                        should_log = True
                if should_log:
                    print(f"[ENTRY_BLOCK][MAIN] reason={reason} ticker={ticker}")
                    minute_cache[block_key] = now
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
            _apply_main_tp_profile_on_entry(state[ticker], mode=main_mode, equity=equity)
            state[ticker]["tp1_adjusted_done"] = False
            state[ticker]["runner_trail_tightened_done"] = False
            state[ticker]["runner_trail_giveback_pct"] = None
            if isinstance(entry_params, dict):
                state[ticker]["sl_one_pct"] = abs(float(entry_params.get("sl_one", 0.0)))
                # MAIN always uses TP1/TP2/RUNNER staged exits.
                state[ticker]["tp_one_pct"] = None
                state[ticker]["trail_from_pct"] = max(0.0, float(entry_params.get("trail_from", 0.0)))
                state[ticker]["trail_giveback_pct"] = max(0.0, float(entry_params.get("trail_giveback", 0.0)))

        filled_buy_krw = float(entry_price) * float(initial_vol)
        if filled_buy_krw <= 0:
            filled_buy_krw = float(per_trade_amt)

        save_state_fn()
        buy_notify_ok = bool(
            notify_order(
            event_type="ORDER_BUY_FILLED",
            strategy_tag="MAIN",
            ticker=ticker,
            price=float(entry_price),
            qty=float(initial_vol),
            reason="ENTRY",
            buy_krw=float(filled_buy_krw),
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
                    f"\uB9E4\uC218\uAE08: {float(filled_buy_krw):,.0f} KRW",
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
        filled_buy_krw = float(entry_price) * float(initial_vol)
        if filled_buy_krw <= 0:
            filled_buy_krw = float(buy_krw)
        save_state_fn()
        buy_notify_ok = bool(
            notify_order(
            event_type="ORDER_BUY_FILLED",
            strategy_tag=strategy_tag,
            ticker=ticker,
            price=float(entry_price),
            qty=float(initial_vol),
            reason="ENTRY",
            buy_krw=float(filled_buy_krw),
            )
        )
        if not buy_notify_ok:
            print(f"[WARN][TELEGRAM] ORDER_BUY_FILLED queued/failed: {strategy_tag} {ticker}")
        time.sleep(0.10)
        return True

    return False

