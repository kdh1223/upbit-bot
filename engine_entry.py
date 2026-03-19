"""Entry engine for MAIN/SCALP signal checks and buy execution."""

from concurrent.futures import ThreadPoolExecutor
import time

import config
import pyupbit
import position_manager
from indicators import (
    check_filters,
    check_filters_with_reason,
    get_atr,
    get_ema,
    get_market_regime,
    get_rsi,
    h4_trend_ok,
    intraday_trend_ok,
    minute_entry_ok,
    minute_entry_score,
    safe_last,
    scalp_entry_signal,
    sr_only_entry_signal_df,
    sr_tv_combo_entry_signal_df,
    v5_breakout_pullback_signal,
    v5_breakout_pullback_signal_df,
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


def _v5_fetch_ohlcv(ticker: str, interval: str, lookback: int):
    try:
        df = pyupbit.get_ohlcv(ticker, interval=interval, count=lookback)
    except Exception:
        return ticker, None, ""
    if df is None or len(df) < 40:
        return ticker, None, ""
    try:
        last_bar_ts = str(df.index[-1])
    except Exception:
        last_bar_ts = ""
    return ticker, df, last_bar_ts


def _resolve_v5_rs_exempt_tickers() -> set:
    raw = getattr(config, "V5_RS_EXEMPT_TICKERS", {"KRW-BTC"})
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
    return {"KRW-BTC"}


def _v5_relative_strength_ok(ticker: str, coin_df, btc_df):
    if not bool(getattr(config, "V5_REL_STRENGTH_FILTER_ON", False)):
        return True, {"reason": "RS_FILTER_OFF"}

    ticker = str(ticker or "").upper().strip()
    if ticker in _resolve_v5_rs_exempt_tickers():
        return True, {"reason": "RS_EXEMPT"}

    rs_lookback = max(2, int(getattr(config, "V5_RS_LOOKBACK_BARS", 12)))
    rs_min_excess_pct = float(getattr(config, "V5_RS_MIN_EXCESS_PCT", 0.30))

    if coin_df is None or btc_df is None or len(coin_df) <= rs_lookback or len(btc_df) <= rs_lookback:
        return False, {"reason": "RS_DATA_SHORT"}

    try:
        coin_now = float(coin_df["close"].iloc[-1])
        coin_prev = float(coin_df["close"].iloc[-1 - rs_lookback])
        btc_now = float(btc_df["close"].iloc[-1])
        btc_prev = float(btc_df["close"].iloc[-1 - rs_lookback])
    except Exception:
        return False, {"reason": "RS_DATA_NAN"}

    if coin_prev <= 0 or btc_prev <= 0:
        return False, {"reason": "RS_BASE_INVALID"}

    coin_ret_pct = ((coin_now / coin_prev) - 1.0) * 100.0
    btc_ret_pct = ((btc_now / btc_prev) - 1.0) * 100.0
    excess_pct = float(coin_ret_pct - btc_ret_pct)
    if excess_pct < float(rs_min_excess_pct):
        return False, {
            "reason": "RS_TOO_WEAK",
            "coin_ret_pct": float(coin_ret_pct),
            "btc_ret_pct": float(btc_ret_pct),
            "excess_pct": float(excess_pct),
        }

    return True, {
        "reason": "OK",
        "coin_ret_pct": float(coin_ret_pct),
        "btc_ret_pct": float(btc_ret_pct),
        "excess_pct": float(excess_pct),
    }


def _v5_universe_momentum_allowlist(fetched_map: dict, symbols) -> tuple[set[str] | None, dict[str, float], dict[str, int]]:
    if not bool(getattr(config, "V5_UNIVERSE_MOM_FILTER_ON", False)):
        return None, {}, {}

    lookback = max(2, int(getattr(config, "V5_UNIVERSE_MOM_LOOKBACK_BARS", 12)))
    top_n = max(1, int(getattr(config, "V5_UNIVERSE_MOM_TOP_N", 3)))

    ranked_rows = []
    seen = set()
    for raw_ticker in list(symbols or []):
        ticker = str(raw_ticker or "").upper().strip()
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        df, _last_bar_ts = fetched_map.get(ticker, (None, ""))
        if df is None or len(df) <= lookback:
            continue
        try:
            now_px = float(df["close"].iloc[-1])
            prev_px = float(df["close"].iloc[-1 - lookback])
        except Exception:
            continue
        if prev_px <= 0:
            continue
        ret_pct = ((now_px / prev_px) - 1.0) * 100.0
        ranked_rows.append((ticker, float(ret_pct)))

    if not ranked_rows:
        return None, {}, {}

    ranked_rows.sort(key=lambda item: (float(item[1]), str(item[0])), reverse=True)
    allowed = {ticker for ticker, _ret in ranked_rows[: min(top_n, len(ranked_rows))]}
    ret_map = {ticker: float(ret) for ticker, ret in ranked_rows}
    rank_map = {ticker: idx + 1 for idx, (ticker, _ret) in enumerate(ranked_rows)}
    return allowed, ret_map, rank_map


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


def _normalize_v5_tp_ratios(tp1, tp2, runner):
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
        a, b, c = 0.50, 0.30, 0.20
        total = 1.0
    return float(a / total), float(b / total), float(c / total)


def _resolve_v5_tp_ratios():
    row = getattr(config, "V5_TP_RATIOS", {}) or {}
    if not isinstance(row, dict):
        row = {"TP1": 0.50, "TP2": 0.30, "RUNNER": 0.20}
    return _normalize_v5_tp_ratios(
        row.get("TP1", 0.50),
        row.get("TP2", 0.30),
        row.get("RUNNER", 0.20),
    )


def _resolve_v5_blocked_regimes() -> set[str]:
    raw = getattr(config, "V5_REGIME_BLOCKED", {"LOW"})
    if isinstance(raw, str):
        return {x.strip().upper() for x in raw.split(",") if str(x).strip()}
    if isinstance(raw, (list, tuple, set)):
        out = set()
        for x in raw:
            s = str(x).strip().upper()
            if s:
                out.add(s)
        return out
    return {"LOW"}


def _v5_btc_filter_ok(now, minute_cache: dict):
    if not bool(getattr(config, "V5_BTC_FILTER_ON", False)):
        return True, "OFF"

    ticker = str(getattr(config, "V5_BTC_FILTER_TICKER", "KRW-BTC")).upper().strip() or "KRW-BTC"
    tf = str(getattr(config, "V5_BTC_FILTER_TF", "minute240")).strip() or "minute240"
    mode = str(getattr(config, "V5_BTC_FILTER_MODE", "EMA200_UP")).upper().strip()
    if mode not in {"EMA200_UP", "MA20_GT_MA60", "MA_FAST_GT_SLOW", "MA20_OVER_MA60_RATIO"}:
        mode = "EMA200_UP"

    ema_len = max(2, int(getattr(config, "V5_BTC_FILTER_EMA_LEN", 200)))
    ma_fast_n = max(2, int(getattr(config, "V5_BTC_FILTER_MA_FAST", 20)))
    ma_slow_n = max(ma_fast_n + 1, int(getattr(config, "V5_BTC_FILTER_MA_SLOW", 60)))
    ma_ratio_min = max(0.0001, float(getattr(config, "V5_BTC_FILTER_MA_RATIO_MIN", 0.995)))

    required = (ema_len + 3) if mode == "EMA200_UP" else (ma_slow_n + 3)
    count = max(required + 20, 260)
    cache_key = f"v5_btc_filter::{ticker}::{tf}::{mode}"

    try:
        df = pyupbit.get_ohlcv(ticker, interval=tf, count=count)
    except Exception:
        return False, "FETCH_ERR"
    if df is None or len(df) < required:
        return False, "DATA_SHORT"

    try:
        last_bar_ts = str(df.index[-1])
    except Exception:
        last_bar_ts = ""

    cached = minute_cache.get(cache_key)
    if isinstance(cached, tuple) and len(cached) == 3 and str(cached[0]) == str(last_bar_ts):
        return bool(cached[1]), str(cached[2] or "")

    close = df["close"]
    last_close = safe_last(close)
    if last_close is None:
        minute_cache[cache_key] = (str(last_bar_ts), False, "CLOSE_NAN")
        return False, "CLOSE_NAN"

    if mode == "EMA200_UP":
        ema = get_ema(close, ema_len)
        ema_now = safe_last(ema)
        if ema_now is None:
            minute_cache[cache_key] = (str(last_bar_ts), False, "EMA_NAN")
            return False, "EMA_NAN"
        ok = bool(float(last_close) > float(ema_now))
        detail = f"C={float(last_close):.2f}|EMA{ema_len}={float(ema_now):.2f}"
    else:
        try:
            ma_fast = float(close.rolling(ma_fast_n).mean().iloc[-1])
            ma_slow = float(close.rolling(ma_slow_n).mean().iloc[-1])
        except Exception:
            minute_cache[cache_key] = (str(last_bar_ts), False, "MA_NAN")
            return False, "MA_NAN"
        if (ma_fast != ma_fast) or (ma_slow != ma_slow):
            minute_cache[cache_key] = (str(last_bar_ts), False, "MA_NAN")
            return False, "MA_NAN"
        if mode == "MA20_OVER_MA60_RATIO":
            if float(ma_slow) <= 0:
                minute_cache[cache_key] = (str(last_bar_ts), False, "MA_SLOW_ZERO")
                return False, "MA_SLOW_ZERO"
            ratio = float(ma_fast) / float(ma_slow)
            ok = bool(ratio >= float(ma_ratio_min))
            detail = (
                f"MA{ma_fast_n}/MA{ma_slow_n}={ratio:.4f}"
                f"|MIN={float(ma_ratio_min):.4f}"
                f"|MA{ma_fast_n}={float(ma_fast):.2f}"
                f"|MA{ma_slow_n}={float(ma_slow):.2f}"
            )
        else:
            ok = bool(float(ma_fast) > float(ma_slow))
            detail = f"MA{ma_fast_n}={float(ma_fast):.2f}|MA{ma_slow_n}={float(ma_slow):.2f}"

    minute_cache[cache_key] = (str(last_bar_ts), bool(ok), str(detail))
    return bool(ok), str(detail)


def _v5_btc_vol_cap_ok(now, minute_cache: dict):
    if not bool(getattr(config, "V5_BTC_VOL_CAP_ON", False)):
        return True, "OFF"

    ticker = str(getattr(config, "V5_BTC_VOL_TICKER", "KRW-BTC")).upper().strip() or "KRW-BTC"
    tf = str(getattr(config, "V5_BTC_VOL_TF", "minute60")).strip() or "minute60"
    atr_n = max(2, int(getattr(config, "V5_BTC_VOL_ATR_PERIOD", 14)))
    lookback = max(atr_n + 5, int(getattr(config, "V5_BTC_VOL_LOOKBACK", 80)))
    cap_pct = max(0.1, float(getattr(config, "V5_BTC_VOL_CAP_PCT", 1.5)))
    cache_key = f"v5_btc_vol_cap::{ticker}::{tf}::{atr_n}::{lookback}::{cap_pct:.4f}"

    try:
        df = pyupbit.get_ohlcv(ticker, interval=tf, count=lookback)
    except Exception:
        return False, "FETCH_ERR"
    if df is None or len(df) < (atr_n + 2):
        return False, "DATA_SHORT"

    try:
        last_bar_ts = str(df.index[-1])
    except Exception:
        last_bar_ts = ""

    cached = minute_cache.get(cache_key)
    if isinstance(cached, tuple) and len(cached) == 3 and str(cached[0]) == str(last_bar_ts):
        return bool(cached[1]), str(cached[2] or "")

    close = df["close"]
    last_close = safe_last(close)
    atr_now = safe_last(get_atr(df, atr_n))
    if last_close is None or atr_now is None or float(last_close) <= 0:
        minute_cache[cache_key] = (str(last_bar_ts), False, "ATR_NAN")
        return False, "ATR_NAN"

    atr_pct = (float(atr_now) / float(last_close)) * 100.0
    ok = bool(float(atr_pct) < float(cap_pct))
    detail = f"ATR%={float(atr_pct):.4f}|CAP={float(cap_pct):.4f}|TF={tf}"
    minute_cache[cache_key] = (str(last_bar_ts), bool(ok), str(detail))
    return bool(ok), str(detail)


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
    surge_tickers=None,
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
    surge_ticker_set = {
        str(t).upper().strip() for t in list(surge_tickers or []) if str(t).strip()
    }

    if total_holding_cnt >= int(max_holdings):
        return False
    if float(per_trade_amt) <= 0:
        return False
    if krw < float(per_trade_amt):
        return False

    for ticker in universe:
        ticker_u = str(ticker).upper().strip()
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

        existing_bucket = str((state.get(ticker, {}) or {}).get("entry_bucket", "")).upper().strip()
        is_surge_entry = (ticker_u in surge_ticker_set) or (existing_bucket == "SURGE")
        entry_reason = "ENTRY_SURGE" if is_surge_entry else "ENTRY"
        entry_bucket = "SURGE" if is_surge_entry else "CORE"

        action = "ADD" if holding else "BUY"
        print(
            f"[MAIN ENTRY] {action} {ticker} | Source={entry_bucket} "
            f"| Regime={regime} | KRW={per_trade_amt:,.0f}"
        )
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
                reason=entry_reason,
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
            state[ticker]["entry_bucket"] = entry_bucket
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
            reason=entry_reason,
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
                    f"\uC0AC\uC720: {entry_reason}",
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


def try_v5_entries(
    upbit,
    now,
    universe,
    prices,
    state,
    cooldown_until,
    per_trade_amt,
    max_holdings,
    total_holding_cnt,
    wait_for_filled_snapshot_fn,
    save_state_fn,
    inactive_tickers=None,
    inactive_positions=None,
    global_holding_tickers=None,
    runtime_risk_state=None,
    equity=None,
):
    runtime_risk_state = runtime_risk_state or {}
    a_only_live = bool(getattr(config, "A_ONLY_ENABLED", False))
    strategy_tag = "A_ONLY" if a_only_live else "V5"
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

    if not bool(getattr(config, "ENABLE_V5_STRATEGY", False)):
        return False

    day_cache, intraday_cache, minute_cache = _safe_caches(prices)
    _ = day_cache, intraday_cache  # reserved for future extensions
    krw = _safe_krw(prices)
    inactive_tickers = set(inactive_tickers or [])
    inactive_positions = inactive_positions or {}
    global_holding_tickers = set(global_holding_tickers or [])
    buy_krw = float(per_trade_amt)
    if a_only_live:
        max_holdings = max(1, int(getattr(config, "A_ONLY_MAX_HOLDINGS", max_holdings)))
        allow_add_buy = bool(getattr(config, "A_ONLY_ALLOW_ADD_BUY", False))
        if allow_add_buy:
            print("[WARN] A_ONLY_ALLOW_ADD_BUY should remain False in live; forcing False")
            allow_add_buy = False

    if total_holding_cnt >= int(max_holdings):
        return False
    if buy_krw <= 0:
        return False
    if krw < buy_krw:
        return False

    interval = str(getattr(config, "V5_SIGNAL_INTERVAL", "minute5"))
    lookback = max(60, int(getattr(config, "V5_SIGNAL_LOOKBACK", 120)))
    workers = max(1, int(getattr(config, "V5_ENTRY_PARALLEL_WORKERS", 1)))
    tp1_ratio, tp2_ratio, runner_ratio = _resolve_v5_tp_ratios()
    entry_regime = "OFF"
    if bool(getattr(config, "V5_REGIME_FILTER_ON", False)):
        day_key = str(now.date())
        cache_key = "v5_regime_daily"
        cached = minute_cache.get(cache_key)
        if isinstance(cached, tuple) and len(cached) == 2 and str(cached[0]) == day_key:
            entry_regime = str(cached[1] or "MID").upper().strip()
        else:
            try:
                entry_regime = str(get_market_regime() or "MID").upper().strip()
            except Exception:
                entry_regime = "MID"
            minute_cache[cache_key] = (day_key, entry_regime)

        blocked = _resolve_v5_blocked_regimes()
        if entry_regime in blocked:
            log_key = "v5_regime_block_log_day"
            if minute_cache.get(log_key) != day_key:
                print(f"[V5 ENTRY BLOCKED] regime={entry_regime} blocked={sorted(blocked)}")
                minute_cache[log_key] = day_key
            return False

    btc_filter_ok, btc_filter_detail = _v5_btc_filter_ok(now, minute_cache)
    if not bool(btc_filter_ok):
        mode = str(getattr(config, "V5_BTC_FILTER_MODE", "EMA200_UP")).upper().strip()
        tf = str(getattr(config, "V5_BTC_FILTER_TF", "minute240")).strip() or "minute240"
        log_key = f"v5_btc_filter_block::{tf}::{mode}"
        try:
            bucket = now.replace(second=0, microsecond=0)
        except Exception:
            bucket = str(now)
        if minute_cache.get(log_key) != bucket:
            print(f"[V5 ENTRY BLOCKED] btc_filter mode={mode} tf={tf} detail={btc_filter_detail}")
            minute_cache[log_key] = bucket
        return False

    btc_vol_ok, btc_vol_detail = _v5_btc_vol_cap_ok(now, minute_cache)
    if not bool(btc_vol_ok):
        tf = str(getattr(config, "V5_BTC_VOL_TF", "minute60")).strip() or "minute60"
        cap = float(getattr(config, "V5_BTC_VOL_CAP_PCT", 1.5))
        log_key = f"v5_btc_vol_block::{tf}::{cap:.4f}"
        try:
            bucket = now.replace(second=0, microsecond=0)
        except Exception:
            bucket = str(now)
        if minute_cache.get(log_key) != bucket:
            print(f"[V5 ENTRY BLOCKED] btc_vol_cap tf={tf} detail={btc_vol_detail}")
            minute_cache[log_key] = bucket
        return False

    try:
        topn = max(1, int(getattr(config, "A_ONLY_TOPN", 10))) if a_only_live else max(1, int(len(universe)))
    except Exception:
        topn = max(1, int(len(universe)))
    eval_universe = list(universe[:topn]) if a_only_live else list(universe)

    candidates = []
    for ticker in eval_universe:
        if _is_blocked_ticker(ticker, inactive_tickers, inactive_positions):
            continue
        if ticker in global_holding_tickers:
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

        candidates.append((str(ticker), float(cur)))

    if not candidates:
        return False

    fetch_symbols = [str(t) for t, _ in candidates]
    if bool(getattr(config, "V5_UNIVERSE_MOM_FILTER_ON", False)):
        seen = set(fetch_symbols)
        for raw_ticker in eval_universe:
            ticker = str(raw_ticker or "").upper().strip()
            if ticker and ticker not in seen:
                fetch_symbols.append(ticker)
                seen.add(ticker)

    fetched_map = {}
    if workers > 1 and len(fetch_symbols) > 1:
        max_workers = min(workers, len(fetch_symbols))
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futs = [ex.submit(_v5_fetch_ohlcv, t, interval, lookback) for t in fetch_symbols]
            for fut in futs:
                t, df, last_bar_ts = fut.result()
                fetched_map[str(t)] = (df, str(last_bar_ts))
    else:
        for t in fetch_symbols:
            _t, _df, _bar = _v5_fetch_ohlcv(t, interval, lookback)
            fetched_map[str(_t)] = (_df, str(_bar))

    btc_rs_df = None
    if bool(getattr(config, "V5_REL_STRENGTH_FILTER_ON", False)):
        rs_btc_ticker = str(getattr(config, "V5_RS_BTC_TICKER", "KRW-BTC")).upper().strip() or "KRW-BTC"
        rs_need = max(int(lookback), int(getattr(config, "V5_RS_LOOKBACK_BARS", 12)) + 5)
        _, btc_rs_df, _ = _v5_fetch_ohlcv(rs_btc_ticker, interval, rs_need)

    universe_allowed, universe_ret_map, universe_rank_map = _v5_universe_momentum_allowlist(
        fetched_map=fetched_map,
        symbols=eval_universe,
    )

    for ticker, cur in candidates:
        df, last_bar_ts = fetched_map.get(str(ticker), (None, ""))
        if df is None or len(df) < 40:
            continue

        if universe_allowed is not None and str(ticker) not in universe_allowed:
            log_key = f"v5_universe_filter_block::{ticker}"
            try:
                bucket = now.replace(second=0, microsecond=0)
            except Exception:
                bucket = str(now)
            if minute_cache.get(log_key) != bucket:
                rank = int(universe_rank_map.get(str(ticker), 0) or 0)
                ret_pct = float(universe_ret_map.get(str(ticker), 0.0) or 0.0)
                top_n = max(1, int(getattr(config, "V5_UNIVERSE_MOM_TOP_N", 3)))
                print(
                    f"[V5 ENTRY BLOCKED] universe_mom ticker={ticker} rank={rank} "
                    f"ret={ret_pct:.3f}% topn={top_n}"
                )
                minute_cache[log_key] = bucket
            continue

        sig_key = f"v5_sig::{ticker}"
        cached_sig = minute_cache.get(sig_key)
        if isinstance(cached_sig, tuple) and len(cached_sig) == 2 and str(cached_sig[0]) == str(last_bar_ts):
            signal = cached_sig[1]
        else:
            signal = v5_breakout_pullback_signal_df(df, cfg=config)
            minute_cache[sig_key] = (str(last_bar_ts), signal)

        if not bool((signal or {}).get("ok", False)):
            continue

        rs_ok, rs_detail = _v5_relative_strength_ok(str(ticker), df, btc_rs_df)
        if not bool(rs_ok):
            rs_reason = str((rs_detail or {}).get("reason", "RS_FILTER_FAIL"))
            log_key = f"v5_rs_block::{ticker}"
            try:
                bucket = now.replace(second=0, microsecond=0)
            except Exception:
                bucket = str(now)
            if minute_cache.get(log_key) != bucket:
                print(f"[V5 ENTRY BLOCKED] rs_filter ticker={ticker} detail={rs_detail}")
                minute_cache[log_key] = bucket
            continue

        last_entry_key = f"v5_last_entry_bar::{ticker}"
        if str(minute_cache.get(last_entry_key, "")) == str(last_bar_ts):
            continue

        entry_px = float(cur)
        swing_low = float((signal or {}).get("swing_low", 0.0) or 0.0)
        if swing_low <= 0 or swing_low >= entry_px:
            continue

        raw_sl_pct = (entry_px - swing_low) / entry_px
        sl_min = max(0.0, float(getattr(config, "V5_SL_MIN_PCT", 0.008)))
        sl_max = max(sl_min, float(getattr(config, "V5_SL_MAX_PCT", 0.018)))
        sl_pct = max(sl_min, min(sl_max, float(raw_sl_pct)))
        if sl_pct <= 0:
            continue
        stop_price = entry_px * (1.0 - sl_pct)

        print(
            f"[V5 ENTRY] BUY {ticker} | KRW={buy_krw:,.0f} "
            f"| stop={stop_price:.6f} r={sl_pct*100.0:.3f}%"
        )
        try:
            initial_vol, entry_price = _execute_buy(
                upbit=upbit,
                ticker=ticker,
                buy_krw=buy_krw,
                cur=float(cur),
                wait_for_filled_snapshot_fn=wait_for_filled_snapshot_fn,
            )
        except Exception as e:
            print(f"[WARN] buy failed(V5): {ticker} err={e}")
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
            "OFF",
            strategy_tag=strategy_tag,
            entry_ts=float(now.timestamp()),
        )
        st = state[ticker]
        st["entry_bucket"] = strategy_tag
        st["sl_one_pct"] = float(sl_pct)
        st["v5_stop_price"] = float(stop_price)
        st["v5_swing_low"] = float(swing_low)
        st["v5_r_pct"] = float(sl_pct)
        st["tp1_ratio"] = float(tp1_ratio)
        st["tp2_ratio"] = float(tp2_ratio)
        st["runner_ratio"] = float(runner_ratio)
        st["tp1_done"] = False
        st["tp2_done"] = False
        st["tp1"] = False
        st["tp2"] = False
        st["runner_active"] = False
        st["runner_hwm"] = 0.0
        st["runner_start_ts"] = 0.0
        st["regime"] = str(entry_regime or "OFF")
        st["entry_mode"] = "V5"

        filled_buy_krw = float(entry_price) * float(initial_vol)
        if filled_buy_krw <= 0:
            filled_buy_krw = float(buy_krw)

        save_state_fn()
        minute_cache[last_entry_key] = str(last_bar_ts)
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


def try_sr_only_entries(
    upbit,
    now,
    universe,
    prices,
    state,
    cooldown_until,
    per_trade_amt,
    max_holdings,
    total_holding_cnt,
    wait_for_filled_snapshot_fn,
    save_state_fn,
    inactive_tickers=None,
    inactive_positions=None,
    global_holding_tickers=None,
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

    if not bool(getattr(config, "ENABLE_SR_ONLY_STRATEGY", False)):
        return False

    _day_cache, _intraday_cache, minute_cache = _safe_caches(prices)
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

    interval_15 = str(getattr(config, "SR_ZONE_INTERVAL", "minute15"))
    interval_5 = str(getattr(config, "SR_EXEC_INTERVAL", "minute5"))
    lookback_15 = max(
        int(getattr(config, "SR_SIGNAL_LOOKBACK_15M", 360)),
        int(getattr(config, "SR_EMA_LEN", 200)) + int(getattr(config, "SR_LOOKBACK", 20)) + 40,
    )
    lookback_5 = max(int(getattr(config, "SR_SIGNAL_LOOKBACK_5M", 240)), 80)
    cooldown_bars = max(0, int(getattr(config, "SR_COOLDOWN_BARS", 40)))
    require_flip = bool(getattr(config, "SR_REQUIRE_FLIP_BREAK_BEFORE_RESIGNAL", True))

    for ticker in universe:
        if _is_blocked_ticker(ticker, inactive_tickers, inactive_positions):
            continue
        if ticker in global_holding_tickers:
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
            df15 = pyupbit.get_ohlcv(ticker, interval=interval_15, count=lookback_15)
        except Exception:
            df15 = None
        try:
            df5 = pyupbit.get_ohlcv(ticker, interval=interval_5, count=lookback_5)
        except Exception:
            df5 = None
        if df15 is None or df5 is None or len(df15) < 80 or len(df5) < 40:
            continue

        try:
            bar5_ts_num = float(df5.index[-1].timestamp())
        except Exception:
            bar5_ts_num = 0.0
        if bar5_ts_num <= 0:
            continue

        lock_key = f"sr_lock::{ticker}"
        if require_flip and bool(minute_cache.get(lock_key, False)):
            cur_close5 = safe_last(df5["close"])
            sup_low = float(minute_cache.get(f"sr_last_sup_low::{ticker}", 0.0) or 0.0)
            res_high = float(minute_cache.get(f"sr_last_res_high::{ticker}", 0.0) or 0.0)
            flip_ok = False
            if cur_close5 is not None:
                if sup_low > 0 and float(cur_close5) <= float(sup_low):
                    flip_ok = True
                if (not flip_ok) and res_high > 0 and float(cur_close5) >= float(res_high):
                    flip_ok = True
            if flip_ok:
                minute_cache[lock_key] = False
            else:
                continue

        last_entry_bar = float(minute_cache.get(f"sr_last_entry_bar::{ticker}", 0.0) or 0.0)
        if cooldown_bars > 0 and last_entry_bar > 0:
            bars_since = int((float(bar5_ts_num) - float(last_entry_bar)) // 300.0)
            if bars_since < cooldown_bars:
                continue

        sig_key = f"sr_sig::{ticker}"
        cached_sig = minute_cache.get(sig_key)
        if isinstance(cached_sig, tuple) and len(cached_sig) == 2 and float(cached_sig[0]) == float(bar5_ts_num):
            signal = cached_sig[1]
        else:
            signal = sr_only_entry_signal_df(df15=df15, df5=df5, cfg=config)
            minute_cache[sig_key] = (float(bar5_ts_num), signal)

        if not bool((signal or {}).get("ok", False)):
            continue

        entry_px = float(cur)
        stop_price = float((signal or {}).get("stop_price", 0.0) or 0.0)
        if stop_price <= 0 or entry_px <= stop_price:
            continue
        sl_pct = (float(entry_px) - float(stop_price)) / float(entry_px)
        if sl_pct <= 0:
            continue

        tp_mode = str((signal or {}).get("tp_mode", "R_FIXED")).upper().strip()
        tp_price = float((signal or {}).get("tp_price", 0.0) or 0.0)
        if tp_price <= entry_px:
            tp_mode = "R_FIXED"
            tp_r = max(0.1, float(getattr(config, "SR_TP_R", 2.0)))
            tp_price = float(entry_px) * (1.0 + float(tp_r) * float(sl_pct))

        print(
            f"[SR ENTRY] BUY {ticker} | KRW={buy_krw:,.0f} "
            f"| stop={stop_price:.6f} tp={tp_price:.6f} mode={tp_mode}"
        )
        try:
            initial_vol, entry_price = _execute_buy(
                upbit=upbit,
                ticker=ticker,
                buy_krw=buy_krw,
                cur=float(cur),
                wait_for_filled_snapshot_fn=wait_for_filled_snapshot_fn,
            )
        except Exception as e:
            print(f"[WARN] buy failed(SR_ONLY): {ticker} err={e}")
            print(f"[WARN] ORDER failed: BUY {ticker}")
            notify_order(
                event_type="ORDER_BUY_FAILED",
                strategy_tag="SR_ONLY",
                ticker=ticker,
                price=float(cur),
                qty=0.0,
                reason="ENTRY_SR_RETEST",
            )
            continue

        state[ticker] = position_manager.init_position_state(
            float(entry_price),
            float(initial_vol),
            float(buy_krw),
            "OFF",
            strategy_tag="SR_ONLY",
            entry_ts=float(now.timestamp()),
        )
        st = state[ticker]
        st["entry_bucket"] = "SR_ONLY"
        st["entry_mode"] = "SR_ONLY"
        st["regime"] = "OFF"
        st["sl_one_pct"] = float(sl_pct)
        st["sr_stop_price"] = float(stop_price)
        st["sr_tp_mode"] = str(tp_mode)
        st["sr_tp_price"] = float(tp_price)
        st["sr_r_pct"] = float(sl_pct)
        st["v5_r_pct"] = float(sl_pct)
        st["sr_support_low"] = float((signal or {}).get("support_low", 0.0) or 0.0)
        st["sr_support_high"] = float((signal or {}).get("support_high", 0.0) or 0.0)
        st["sr_resistance_low"] = float((signal or {}).get("resistance_low", 0.0) or 0.0)
        st["sr_resistance_high"] = float((signal or {}).get("resistance_high", 0.0) or 0.0)
        st["tp1_ratio"] = 0.0
        st["tp2_ratio"] = 0.0
        st["runner_ratio"] = 0.0
        st["tp1_done"] = False
        st["tp2_done"] = False
        st["tp1"] = False
        st["tp2"] = False
        st["runner_active"] = False
        st["runner_hwm"] = 0.0
        st["runner_start_ts"] = 0.0

        minute_cache[f"sr_last_entry_bar::{ticker}"] = float(bar5_ts_num)
        minute_cache[f"sr_last_sup_low::{ticker}"] = float(st["sr_support_low"])
        minute_cache[f"sr_last_res_high::{ticker}"] = float(st["sr_resistance_high"])
        minute_cache[lock_key] = bool(require_flip)

        filled_buy_krw = float(entry_price) * float(initial_vol)
        if filled_buy_krw <= 0:
            filled_buy_krw = float(buy_krw)

        save_state_fn()
        buy_notify_ok = bool(
            notify_order(
                event_type="ORDER_BUY_FILLED",
                strategy_tag="SR_ONLY",
                ticker=ticker,
                price=float(entry_price),
                qty=float(initial_vol),
                reason="ENTRY_SR_RETEST",
                buy_krw=float(filled_buy_krw),
            )
        )
        if not buy_notify_ok:
            print(f"[WARN][TELEGRAM] ORDER_BUY_FILLED queued/failed: SR_ONLY {ticker}")
        time.sleep(0.10)
        return True

    return False


def try_sr_tv_combo_entries(
    upbit,
    now,
    universe,
    prices,
    state,
    cooldown_until,
    per_trade_amt,
    max_holdings,
    total_holding_cnt,
    wait_for_filled_snapshot_fn,
    save_state_fn,
    strategy_tag="SR_ONLY_TV_COMBO",
    inactive_tickers=None,
    inactive_positions=None,
    global_holding_tickers=None,
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

    enabled = bool(getattr(config, "ENABLE_SR_ONLY_TV_COMBO", False)) or bool(
        getattr(config, "ENABLE_SR_TV_COMBO_STRATEGY", False)
    )
    if not enabled:
        return False

    tag = str(strategy_tag or "SR_ONLY_TV_COMBO").upper().strip()
    if tag not in {"SR_ONLY_TV_COMBO", "SR_ONLY_TV_COMBO_A", "SR_ONLY_TV_COMBO_B"}:
        tag = "SR_ONLY_TV_COMBO"
    sl_mode = "A" if tag.endswith("_A") else "B"
    if tag == "SR_ONLY_TV_COMBO":
        sl_mode = "A"

    _day_cache, _intraday_cache, minute_cache = _safe_caches(prices)
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

    # Signal is defined on completed 5m candles; skip non-close minutes.
    exec_interval = str(getattr(config, "SR_TV_EXEC_INTERVAL", "minute5")).lower().strip()
    try:
        exec_minutes = int(exec_interval.replace("minute", ""))
    except Exception:
        exec_minutes = 5
    exec_minutes = max(1, int(exec_minutes))
    if exec_minutes > 1:
        try:
            minute_of_day = int(now.hour) * 60 + int(now.minute)
            if (minute_of_day % exec_minutes) != 0:
                return False
        except Exception:
            pass

    interval_15 = str(getattr(config, "SR_TV_ZONE_INTERVAL", "minute15"))
    interval_5 = str(getattr(config, "SR_TV_EXEC_INTERVAL", "minute5"))
    lookback_15 = max(
        int(getattr(config, "SR_TV_SIGNAL_LOOKBACK_15M", 360)),
        int(getattr(config, "SR_TV_EMA_LEN", 200)) + int(getattr(config, "SR_TV_LOOKBACK", 20)) + 40,
    )
    lookback_5 = max(int(getattr(config, "SR_TV_SIGNAL_LOOKBACK_5M", 240)), 80)
    cooldown_bars = max(0, int(getattr(config, "SR_TV_COOLDOWN_BARS", 40)))
    max_touch_count = max(1, int(getattr(config, "SR_TV_MAX_TOUCH_COUNT", 3)))
    require_flip = bool(getattr(config, "SR_TV_REQUIRE_FLIP_BREAK_BEFORE_RESIGNAL", True))
    sl_buffer_pct = max(0.0, float(getattr(config, "SR_TV_SL_BUFFER_PCT", 0.003)))

    for ticker in universe:
        if _is_blocked_ticker(ticker, inactive_tickers, inactive_positions):
            continue
        if ticker in global_holding_tickers:
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
            df15 = pyupbit.get_ohlcv(ticker, interval=interval_15, count=lookback_15)
        except Exception:
            df15 = None
        try:
            df5 = pyupbit.get_ohlcv(ticker, interval=interval_5, count=lookback_5)
        except Exception:
            df5 = None
        if df15 is None or df5 is None or len(df15) < 80 or len(df5) < 20:
            continue

        try:
            bar5_ts_num = float(df5.index[-1].timestamp())
        except Exception:
            bar5_ts_num = 0.0
        if bar5_ts_num <= 0:
            continue

        sig_key = f"sr_tv_sig::{ticker}"
        cached_sig = minute_cache.get(sig_key)
        if isinstance(cached_sig, tuple) and len(cached_sig) == 2 and float(cached_sig[0]) == float(bar5_ts_num):
            signal = cached_sig[1]
        else:
            signal = sr_tv_combo_entry_signal_df(df15=df15, df5=df5, cfg=config)
            minute_cache[sig_key] = (float(bar5_ts_num), signal)

        zone_id = str((signal or {}).get("zone_id", "") or "")
        lock_zone_key = f"sr_tv_lock_zone::{ticker}"
        locked_zone = str(minute_cache.get(lock_zone_key, "") or "")
        if require_flip and locked_zone:
            flip_ok = bool((signal or {}).get("flip_break_detected", False))
            if flip_ok:
                minute_cache[lock_zone_key] = ""
                locked_zone = ""
            elif (not zone_id) or (zone_id == locked_zone):
                continue

        touch_counts_key = f"sr_tv_touch_counts::{ticker}"
        touch_last_key = f"sr_tv_touch_lastbar::{ticker}"
        touch_counts = minute_cache.get(touch_counts_key)
        if not isinstance(touch_counts, dict):
            touch_counts = {}
        touch_last = minute_cache.get(touch_last_key)
        if not isinstance(touch_last, dict):
            touch_last = {}

        if zone_id and bool((signal or {}).get("touch_detected", False)):
            prev_touch_bar = float(touch_last.get(zone_id, 0.0) or 0.0)
            if abs(float(bar5_ts_num) - float(prev_touch_bar)) > 1e-9:
                touch_counts[zone_id] = int(touch_counts.get(zone_id, 0)) + 1
                touch_last[zone_id] = float(bar5_ts_num)
                minute_cache[touch_counts_key] = touch_counts
                minute_cache[touch_last_key] = touch_last

        if zone_id and int(touch_counts.get(zone_id, 0)) >= int(max_touch_count):
            continue

        last_entry_bar_key = f"sr_tv_last_entry_bar::{ticker}"
        last_entry_bar = float(minute_cache.get(last_entry_bar_key, 0.0) or 0.0)
        if cooldown_bars > 0 and last_entry_bar > 0:
            bars_since = int((float(bar5_ts_num) - float(last_entry_bar)) // 300.0)
            if bars_since < cooldown_bars:
                continue

        if not bool((signal or {}).get("ok", False)):
            continue

        entry_px = float(cur)
        support_low = float((signal or {}).get("support_low", 0.0) or 0.0)
        support_high = float((signal or {}).get("support_high", 0.0) or 0.0)
        resistance_low = float((signal or {}).get("resistance_low", 0.0) or 0.0)
        resistance_high = float((signal or {}).get("resistance_high", 0.0) or 0.0)
        if support_low <= 0 or support_high <= 0 or resistance_low <= 0:
            continue
        if resistance_low <= entry_px:
            continue

        if sl_mode == "A":
            stop_price = float(support_low) * (1.0 - float(sl_buffer_pct))
        else:
            stop_price = float(support_high)
        if stop_price <= 0 or entry_px <= stop_price:
            continue

        sl_pct = (float(entry_px) - float(stop_price)) / float(entry_px)
        if sl_pct <= 0:
            continue

        tp_price = float((signal or {}).get("tp_price", resistance_low) or resistance_low)
        if tp_price <= entry_px:
            tp_price = float(resistance_low)
        if tp_price <= entry_px:
            continue

        print(
            f"[SR_TV ENTRY] BUY {ticker} | KRW={buy_krw:,.0f} "
            f"| stop={stop_price:.6f} tp={tp_price:.6f} mode={sl_mode}"
        )
        try:
            initial_vol, entry_price = _execute_buy(
                upbit=upbit,
                ticker=ticker,
                buy_krw=buy_krw,
                cur=float(cur),
                wait_for_filled_snapshot_fn=wait_for_filled_snapshot_fn,
            )
        except Exception as e:
            print(f"[WARN] buy failed({tag}): {ticker} err={e}")
            print(f"[WARN] ORDER failed: BUY {ticker}")
            notify_order(
                event_type="ORDER_BUY_FAILED",
                strategy_tag=tag,
                ticker=ticker,
                price=float(cur),
                qty=0.0,
                reason="ENTRY_SR_TV_COMBO",
            )
            continue

        state[ticker] = position_manager.init_position_state(
            float(entry_price),
            float(initial_vol),
            float(buy_krw),
            "OFF",
            strategy_tag=tag,
            entry_ts=float(now.timestamp()),
        )
        st = state[ticker]
        st["entry_bucket"] = "SR_TV_COMBO"
        st["entry_mode"] = str(tag)
        st["regime"] = "OFF"
        st["sl_one_pct"] = float(sl_pct)
        st["sr_tv_sl_mode"] = str(sl_mode)
        st["sr_stop_price"] = float(stop_price) if sl_mode == "A" else 0.0
        st["sr_tp_mode"] = "RESIST"
        st["sr_tp_price"] = float(tp_price)
        st["sr_r_pct"] = float(sl_pct)
        st["v5_r_pct"] = float(sl_pct)
        st["sr_support_low"] = float(support_low)
        st["sr_support_high"] = float(support_high)
        st["sr_resistance_low"] = float(resistance_low)
        st["sr_resistance_high"] = float(resistance_high)
        st["sr_zone_id"] = str(zone_id)
        st["tp1_ratio"] = 0.0
        st["tp2_ratio"] = 0.0
        st["runner_ratio"] = 0.0
        st["tp1_done"] = False
        st["tp2_done"] = False
        st["tp1"] = False
        st["tp2"] = False
        st["runner_active"] = False
        st["runner_hwm"] = 0.0
        st["runner_start_ts"] = 0.0

        minute_cache[last_entry_bar_key] = float(bar5_ts_num)
        if require_flip and zone_id:
            minute_cache[lock_zone_key] = str(zone_id)

        filled_buy_krw = float(entry_price) * float(initial_vol)
        if filled_buy_krw <= 0:
            filled_buy_krw = float(buy_krw)

        save_state_fn()
        buy_notify_ok = bool(
            notify_order(
                event_type="ORDER_BUY_FILLED",
                strategy_tag=tag,
                ticker=ticker,
                price=float(entry_price),
                qty=float(initial_vol),
                reason="ENTRY_SR_TV_COMBO",
                buy_krw=float(filled_buy_krw),
            )
        )
        if not buy_notify_ok:
            print(f"[WARN][TELEGRAM] ORDER_BUY_FILLED queued/failed: {tag} {ticker}")
        time.sleep(0.10)
        return True

    return False

