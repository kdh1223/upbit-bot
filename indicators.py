"""지표 계산 함수와 전략 진입/필터 신호 함수를 제공하는 모듈."""

from collections import Counter
import time

import pyupbit

import config


_MINUTE_REJECT_COUNTER = Counter()
_MINUTE_REJECT_TOTAL = 0
_MINUTE_REJECT_LAST_PRINT_TS = time.time()
_SCALP_BTC_REGIME_CACHE = {}


def _flush_minute_reject_summary(force: bool = False):
    global _MINUTE_REJECT_TOTAL, _MINUTE_REJECT_LAST_PRINT_TS

    summary_min = float(getattr(config, "ENTRY_MINUTE_REJECT_SUMMARY_MIN", 10))
    interval_sec = max(60.0, summary_min * 60.0)
    now_ts = time.time()
    if (not force) and ((now_ts - _MINUTE_REJECT_LAST_PRINT_TS) < interval_sec):
        return

    _MINUTE_REJECT_LAST_PRINT_TS = now_ts
    if not _MINUTE_REJECT_COUNTER:
        return

    topn = max(1, int(getattr(config, "ENTRY_MINUTE_REJECT_SUMMARY_TOPN", 6)))
    parts = ", ".join([f"{k}:{v}" for k, v in _MINUTE_REJECT_COUNTER.most_common(topn)])
    print(f"[MAIN_분봉거절요약] total={_MINUTE_REJECT_TOTAL} | {parts}")
    _MINUTE_REJECT_COUNTER.clear()
    _MINUTE_REJECT_TOTAL = 0


def _track_minute_reject(reason: str):
    global _MINUTE_REJECT_TOTAL
    key = str(reason or "UNKNOWN")
    _MINUTE_REJECT_COUNTER[key] += 1
    _MINUTE_REJECT_TOTAL += 1
    _flush_minute_reject_summary(force=False)


def get_ma(df, period):
    return df["close"].rolling(period).mean()


def get_sma(series, period):
    return series.rolling(period).mean()


def get_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


def safe_last(series):
    try:
        v = float(series.iloc[-1])
        if v != v:
            return None
        return v
    except Exception:
        return None


def _scalp_btc_regime_allows_entry(ticker: str) -> bool:
    if not bool(getattr(config, "SCALP_BTC_REGIME_FILTER_ON", False)):
        return True

    tf = str(getattr(config, "SCALP_BTC_REGIME_TF", "minute240"))
    ma_fast_n = max(2, int(getattr(config, "SCALP_BTC_REGIME_MA_FAST", 20)))
    ma_slow_n = max(ma_fast_n + 1, int(getattr(config, "SCALP_BTC_REGIME_MA_SLOW", 60)))
    mode = str(getattr(config, "SCALP_BTC_REGIME_MODE", "BULL_ONLY")).upper().strip()
    cache_sec = max(0.0, float(getattr(config, "SCALP_BTC_REGIME_CACHE_SEC", 300)))
    key = (str(ticker or "").upper().strip(), tf, ma_fast_n, ma_slow_n, mode)

    now_ts = time.time()
    cached = _SCALP_BTC_REGIME_CACHE.get(key)
    if isinstance(cached, tuple) and len(cached) == 2:
        cached_ok, cached_ts = cached
        try:
            if (now_ts - float(cached_ts)) < cache_sec:
                return bool(cached_ok)
        except Exception:
            pass

    df4 = pyupbit.get_ohlcv(ticker, interval=tf, count=max(ma_slow_n + 5, 120))
    if df4 is None or len(df4) < (ma_slow_n + 2) or "close" not in df4.columns:
        _SCALP_BTC_REGIME_CACHE[key] = (False, now_ts)
        return False

    close4 = df4["close"]
    last_close = safe_last(close4)
    ma_fast = safe_last(close4.rolling(ma_fast_n).mean())
    ma_slow = safe_last(close4.rolling(ma_slow_n).mean())
    if None in (last_close, ma_fast, ma_slow):
        _SCALP_BTC_REGIME_CACHE[key] = (False, now_ts)
        return False

    bull = (float(last_close) > float(ma_fast)) and (float(ma_fast) > float(ma_slow))

    ok = True
    if mode == "BULL_ONLY":
        ok = bool(bull)

    _SCALP_BTC_REGIME_CACHE[key] = (bool(ok), now_ts)
    return bool(ok)


def volume_ma(df, period):
    return get_sma(df["volume"], period)


def _rsi_from_close(close, period=14):
    n = max(2, int(period))
    delta = close.diff()
    up = delta.clip(lower=0.0)
    down = -delta.clip(upper=0.0)
    mode = str(getattr(config, "TA_RSI_MODE", "RMA")).upper().strip()

    if mode == "SMA":
        gain = up.rolling(n, min_periods=n).mean()
        loss = down.rolling(n, min_periods=n).mean()
    else:
        alpha = 1.0 / float(n)
        gain = up.ewm(alpha=alpha, adjust=False, min_periods=n).mean()
        loss = down.ewm(alpha=alpha, adjust=False, min_periods=n).mean()

    rs = gain / (loss + 1e-12)
    rsi = 100.0 - (100.0 / (1.0 + rs))

    both_zero = (gain <= 1e-12) & (loss <= 1e-12)
    loss_zero = (loss <= 1e-12) & (gain > 1e-12)
    rsi = rsi.where(~both_zero, 50.0)
    rsi = rsi.where(~loss_zero, 100.0)
    return rsi


def get_rsi(df, period=14):
    return _rsi_from_close(df["close"], period=period)


def get_atr(df, period=14):
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)

    tr1 = (high - low).abs()
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()

    tr = tr1.combine(tr2, max).combine(tr3, max)
    return tr.rolling(period).mean()


def check_filters_with_reason(ticker):
    """
    Daily filter detail:
    - MA5 > MA20
    - RSI < 70
    """
    df = pyupbit.get_ohlcv(ticker, interval="day", count=40)
    if df is None or len(df) < 25:
        return False, "DAY_DATA_SHORT"

    ma5 = get_ma(df, 5)
    ma20 = get_ma(df, 20)
    rsi = get_rsi(df, 14)

    ma5_now = safe_last(ma5)
    ma20_now = safe_last(ma20)
    rsi_now = safe_last(rsi)
    if ma5_now is None or ma20_now is None or rsi_now is None:
        return False, "DAY_DATA_NAN"

    if not (ma5_now > ma20_now):
        return False, "DAY_MA_FAIL"
    if not (rsi_now < 70):
        return False, "DAY_RSI_FAIL"
    return True, "OK"


def check_filters(ticker):
    ok, _ = check_filters_with_reason(ticker)
    return bool(ok)


def intraday_trend_ok(ticker):
    """
    Intraday trend filter: MA(fast) > MA(slow) on configured interval.
    """
    interval = getattr(config, "INTRADAY_TREND_INTERVAL", "minute240")
    df = pyupbit.get_ohlcv(ticker, interval=interval, count=120)
    if df is None or len(df) < (config.INTRADAY_SLOW_MA + 5):
        return True  # Do not block entry on missing intraday data.

    ma_fast = df["close"].rolling(config.INTRADAY_FAST_MA).mean()
    ma_slow = df["close"].rolling(config.INTRADAY_SLOW_MA).mean()
    return bool(ma_fast.iloc[-1] > ma_slow.iloc[-1])


def minute_entry_ok(ticker):
    """
    MAIN minute timing filter.
    Daily filter is handled elsewhere and not changed here.
    """
    interval = getattr(config, "ENTRY_MINUTE_INTERVAL", "minute5")
    df = pyupbit.get_ohlcv(ticker, interval=interval, count=80)
    if df is None or len(df) < 30:
        return True  # Do not block entry on missing minute data.

    debug_reject = bool(getattr(config, "DEBUG_ENTRY_REJECT", False))

    def reject(reason: str):
        _track_minute_reject(reason)
        if debug_reject:
            print(f"[MAIN_분봉거절] {ticker} {reason}")
        return False

    ma_fast_period = int(getattr(config, "ENTRY_MA_FAST", getattr(config, "ENTRY_FAST_MA", 5)))
    ma_slow_period = int(getattr(config, "ENTRY_MA_SLOW", getattr(config, "ENTRY_SLOW_MA", 20)))
    rsi_period = int(getattr(config, "ENTRY_RSI_PERIOD", 14))
    lookback = int(getattr(config, "ENTRY_PULLBACK_LOOKBACK", getattr(config, "ENTRY_BREAKOUT_LOOKBACK", 20)))
    lookback = max(5, lookback)
    min_needed = max(30, ma_slow_period + 3, lookback + 3)
    if len(df) < min_needed:
        return True  # Keep legacy behavior on short data.

    close_series = df["close"]
    vol_series = df["volume"] if "volume" in df.columns else None
    ma_fast = get_sma(close_series, ma_fast_period)
    ma_slow = get_sma(close_series, ma_slow_period)
    rsi = get_rsi(df, rsi_period)

    ma_fast_now = safe_last(ma_fast)
    ma_slow_now = safe_last(ma_slow)
    slope_bars = max(1, int(getattr(config, "ENTRY_MA_SLOPE_BARS", 1)))
    ma_fast_prev = None
    try:
        ma_fast_prev = float(ma_fast.iloc[-1 - slope_bars])
        if ma_fast_prev != ma_fast_prev:
            ma_fast_prev = None
    except Exception:
        ma_fast_prev = None
    rsi_now = safe_last(rsi)
    close_now = safe_last(close_series)
    close_prev = safe_last(close_series.iloc[:-1])
    if None in (ma_fast_now, ma_slow_now, ma_fast_prev, rsi_now, close_now, close_prev):
        return True

    ma_fast_slope = float(ma_fast_now) - float(ma_fast_prev)
    strong_trend = bool(close_now > ma_fast_now > ma_slow_now and ma_fast_slope > 0.0)

    base_rsi_max = float(getattr(config, "ENTRY_RSI_MAX", 70))
    rsi_max = float(base_rsi_max)
    relaxed_rsi_mode = False
    if bool(getattr(config, "ENTRY_ENABLE_TREND_RSI_RELAX", True)) and strong_trend:
        strong_rsi_max = float(getattr(config, "ENTRY_RSI_MAX_STRONG", 74))
        rsi_max = max(base_rsi_max, strong_rsi_max)
        relaxed_rsi_mode = bool(rsi_max > base_rsi_max)

    if not (ma_fast_now > ma_slow_now):
        return reject("MA_ALIGN")
    if not (rsi_now < rsi_max):
        return reject("RSI_OVERHEAT")

    recent_high = None
    try:
        recent_high = float(df["high"].rolling(lookback).max().iloc[-2])
        if recent_high != recent_high or recent_high <= 0:
            recent_high = None
    except Exception:
        recent_high = None

    if recent_high is not None:
        near_high_block_pct = float(getattr(config, "ENTRY_NEAR_HIGH_BLOCK_PCT", 0.002))
        if float(close_now) >= float(recent_high) * (1.0 - near_high_block_pct):
            return reject("NEAR_RECENT_HIGH")

    # If RSI upper is relaxed in strong trend, only allow entry
    # after a pullback zone has appeared, then a rebound with non-weak volume.
    if relaxed_rsi_mode:
        if recent_high is None:
            return reject("PULLBACK_REF_MISSING")

        pull_min = float(getattr(config, "ENTRY_PULLBACK_MIN_PCT", 0.003))
        pull_max = float(getattr(config, "ENTRY_PULLBACK_MAX_PCT", 0.010))
        if pull_min > pull_max:
            pull_min, pull_max = pull_max, pull_min

        pull_window = max(2, int(getattr(config, "ENTRY_PULLBACK_WINDOW", 8)))
        pull_slice = close_series.iloc[max(0, len(close_series) - 1 - pull_window) : len(close_series) - 1]
        if pull_slice.empty:
            return reject("PULLBACK_WINDOW_SHORT")

        drawdown = 1.0 - (pull_slice / float(recent_high))
        pull_seen = bool(((drawdown >= pull_min) & (drawdown <= pull_max)).any())
        if not pull_seen:
            return reject("PULLBACK_NOT_SEEN")

        if bool(getattr(config, "ENTRY_REQUIRE_REBOUND", True)):
            if not (float(close_now) > float(close_prev)):
                return reject("REBOUND_NOT_CONFIRMED")

        if bool(getattr(config, "ENTRY_REQUIRE_VOL_HOLD", True)):
            if vol_series is None:
                return reject("VOLUME_MISSING")
            vol_now = safe_last(vol_series)
            vol_prev = safe_last(vol_series.iloc[:-1])
            if vol_now is None or vol_prev is None:
                return reject("VOLUME_NAN")
            if float(vol_now) < float(vol_prev):
                return reject("VOLUME_DECAY")

    if bool(getattr(config, "ENTRY_REQUIRE_RSI_UPTURN", False)):
        rsi_prev = None
        try:
            rsi_prev = float(rsi.iloc[-2])
            if rsi_prev != rsi_prev:
                rsi_prev = None
        except Exception:
            rsi_prev = None

        if rsi_prev is None:
            return reject("RSI_PREV_NAN")

        delta_min = float(getattr(config, "ENTRY_RSI_DELTA_MIN", 0.8))
        if (rsi_now - rsi_prev) < delta_min:
            return reject("RSI_DELTA_LOW")

    if bool(getattr(config, "ENTRY_USE_VOLUME_FILTER", False)):
        if vol_series is None:
            return reject("VOLUME_MISSING")
        v_now = safe_last(vol_series)
        vma = safe_last(volume_ma(df, getattr(config, "ENTRY_VOL_MA_PERIOD", 20)))
        if v_now is None or vma is None or vma <= 0:
            return reject("VOLUME_NAN")
        vol_mult = float(getattr(config, "ENTRY_VOL_MULT", 1.1))
        if not (v_now > vma * vol_mult):
            return reject("VOLUME_FILTER_FAIL")

    return True


def minute_entry_score(df, cfg=config):
    """
    1m entry score for MAIN (0~5).
    Returns: (score:int, reasons:list[str], metrics:dict)
    """
    required_cols = {"open", "high", "low", "close", "volume"}
    if df is None or len(df) < 25 or (not required_cols.issubset(df.columns)):
        return 0, ["DATA_SHORT"], {}

    try:
        close_series = df["close"]
        high_series = df["high"]
        vol_series = df["volume"]

        rsi_series = get_rsi(df, 14)
        ema9_series = get_ema(close_series, 9)
        vol_ma20_series = volume_ma(df, 20)

        close_now = safe_last(close_series)
        high_prev = float(high_series.iloc[-2])
        rsi_now = float(rsi_series.iloc[-1])
        rsi_prev = float(rsi_series.iloc[-2])
        ema9_now = float(ema9_series.iloc[-1])
        ema9_prev = float(ema9_series.iloc[-2])
        vol_now = float(vol_series.iloc[-1])
        vol_prev = float(vol_series.iloc[-2])
        vol_ma20_now = float(vol_ma20_series.iloc[-1])
    except Exception:
        return 0, ["DATA_SHORT"], {}

    vals = [close_now, high_prev, rsi_now, rsi_prev, ema9_now, ema9_prev, vol_now, vol_prev, vol_ma20_now]
    if any((v != v) for v in vals) or close_now is None or vol_ma20_now <= 0:
        return 0, ["DATA_SHORT"], {}

    score = 0
    reasons = []

    rsi_max = float(getattr(cfg, "ENTRY_SCORE_RSI_MAX", 55.0))
    vol_mult = float(getattr(cfg, "ENTRY_SCORE_VOL_MULT", 1.20))

    if (rsi_now > rsi_prev) and (rsi_now <= rsi_max):
        score += 1
        reasons.append("RSI_UP")

    if float(close_now) > float(ema9_now):
        score += 1
        reasons.append("CLOSE_GT_EMA9")

    if float(close_now) > float(high_prev):
        score += 1
        reasons.append("BREAK_PREV_HIGH")

    if float(ema9_now) > float(ema9_prev):
        score += 1
        reasons.append("EMA9_UP")

    if (float(vol_now) > float(vol_ma20_now) * float(vol_mult)) and (float(vol_now) > float(vol_prev)):
        score += 1
        reasons.append("VOL_SPIKE")

    metrics = {
        "close_now": float(close_now),
        "high_prev": float(high_prev),
        "rsi_now": float(rsi_now),
        "rsi_prev": float(rsi_prev),
        "ema9_now": float(ema9_now),
        "ema9_prev": float(ema9_prev),
        "vol_now": float(vol_now),
        "vol_prev": float(vol_prev),
        "vol_ma20_now": float(vol_ma20_now),
    }
    return int(score), reasons, metrics


def _v5_clamp_sl_pct(raw_sl_pct: float, cfg=config) -> float:
    sl_min = max(0.0, float(getattr(cfg, "V5_SL_MIN_PCT", 0.008)))
    sl_max = max(sl_min, float(getattr(cfg, "V5_SL_MAX_PCT", 0.018)))
    return max(sl_min, min(sl_max, float(raw_sl_pct)))


def _v5_breakout_quality_check(
    *,
    breakout_level: float,
    cur_open: float,
    cur_high: float,
    cur_low: float,
    cur_close: float,
    cfg=config,
):
    candle_range = float(cur_high) - float(cur_low)
    if candle_range <= 0:
        return False, "BREAKOUT_RANGE_ZERO", {}

    body = abs(float(cur_close) - float(cur_open))
    if body <= 0:
        return False, "BREAKOUT_BODY_ZERO", {}

    close_pos = (float(cur_close) - float(cur_low)) / candle_range
    top_pct = max(0.01, min(0.49, float(getattr(cfg, "V5_BREAKOUT_CLOSE_TOP_PCT", 0.25))))
    if close_pos < (1.0 - top_pct):
        return False, "BREAKOUT_CLOSE_WEAK", {"close_pos": float(close_pos)}

    wick_body_max = max(0.0, float(getattr(cfg, "V5_BREAKOUT_UPPER_WICK_MAX_BODY", 0.50)))
    upper_wick = float(cur_high) - max(float(cur_open), float(cur_close))
    wick_body_ratio = float(upper_wick / body) if body > 0 else 999.0
    if upper_wick > (body * wick_body_max):
        return False, "BREAKOUT_UPPER_WICK_TOO_LARGE", {"wick_body_ratio": float(wick_body_ratio)}

    if float(cur_close) <= 0:
        return False, "BREAKOUT_CLOSE_INVALID", {}

    min_body_pct = max(0.0, float(getattr(cfg, "V5_BREAKOUT_MIN_BODY_PCT", 0.0)))
    body_pct = body / float(cur_close)
    if body_pct < min_body_pct:
        return False, "BREAKOUT_BODY_TOO_SMALL", {"body_pct": float(body_pct)}

    min_range_pct = max(0.0, float(getattr(cfg, "V5_BREAKOUT_MIN_RANGE_PCT", 0.0)))
    range_pct = candle_range / float(cur_close)
    if range_pct < min_range_pct:
        return False, "BREAKOUT_RANGE_TOO_SMALL", {"range_pct": float(range_pct)}

    breakout_level = float(breakout_level)
    if breakout_level <= 0:
        return False, "BREAKOUT_REF_INVALID", {}

    confirm_min_pct = max(0.0, float(getattr(cfg, "V5_BREAKOUT_CONFIRM_PCT", 0.0015)))
    confirm_pct = (float(cur_close) / breakout_level) - 1.0
    if confirm_pct < confirm_min_pct:
        return False, "BREAKOUT_CONFIRM_TOO_SMALL", {"confirm_pct": float(confirm_pct)}

    return True, "OK", {
        "close_pos": float(close_pos),
        "wick_body_ratio": float(wick_body_ratio),
        "body_pct": float(body_pct),
        "range_pct": float(range_pct),
        "confirm_pct": float(confirm_pct),
    }


def v5_breakout_pullback_signal_df(df, cfg=config):
    """
    V5 long-only signal on completed 5m candles:
    1) Breakout candle closes above prior N high with volume spike.
    2) 1~M pullback candles hold above EMA.
    3) Latest candle re-accelerates above previous high and closes above EMA.
    """
    required_cols = {"open", "high", "low", "close", "volume"}
    if df is None or len(df) < 40 or (not required_cols.issubset(df.columns)):
        return {"ok": False, "reason": "DATA_SHORT"}

    lookback = max(5, int(getattr(cfg, "V5_BREAKOUT_LOOKBACK", 20)))
    pullback_max = max(1, int(getattr(cfg, "V5_PULLBACK_MAX_BARS", 3)))
    ema_period = max(2, int(getattr(cfg, "V5_EMA_PERIOD", 20)))
    vol_ma_period = max(2, int(getattr(cfg, "V5_VOL_MA_PERIOD", 20)))
    vol_mult = max(
        1.0,
        float(getattr(cfg, "V5_VOLUME_MULT", getattr(cfg, "V5_BREAKOUT_VOL_MULT", 1.7))),
    )
    require_quality = bool(getattr(cfg, "V5_BREAKOUT_QUALITY_FILTER", False))
    require_retest = bool(getattr(cfg, "V5_REQUIRE_RETEST_CONFIRM", False))
    retest_touch_tol = max(0.0, float(getattr(cfg, "V5_RETEST_TOUCH_TOL_PCT", 0.0015)))
    retest_break_tol = max(0.0, float(getattr(cfg, "V5_RETEST_BREAK_TOL_PCT", 0.0030)))
    retest_reclaim_min = max(0.0, float(getattr(cfg, "V5_RETEST_RECLAIM_MIN_PCT", 0.0)))

    n = len(df)
    min_needed = lookback + pullback_max + max(ema_period, vol_ma_period) + 5
    if n < min_needed:
        return {"ok": False, "reason": "DATA_SHORT"}

    close = df["close"]
    open_ = df["open"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]
    ema = get_ema(close, ema_period)
    vol_ma = volume_ma(df, vol_ma_period)

    cur_open = safe_last(open_)
    cur_high = safe_last(high)
    cur_low = safe_last(low)
    cur_close = safe_last(close)
    cur_prev_high = safe_last(high.iloc[:-1])
    cur_ema = safe_last(ema)
    if None in (cur_open, cur_high, cur_low, cur_close, cur_prev_high, cur_ema):
        return {"ok": False, "reason": "DATA_NAN"}

    trend_mode = str(getattr(cfg, "V5_TREND_FILTER", "OFF")).upper().strip()
    if trend_mode in {"CLOSE_GT_EMA50", "EMA20_GT_EMA50"}:
        ema50 = get_ema(close, 50)
        ema50_now = safe_last(ema50)
        if ema50_now is None:
            return {"ok": False, "reason": "TREND_DATA_NAN"}
        if not (float(cur_close) > float(ema50_now)):
            return {"ok": False, "reason": "TREND_CLOSE_BELOW_EMA50"}
        if trend_mode == "EMA20_GT_EMA50":
            if not (float(cur_ema) > float(ema50_now)):
                return {"ok": False, "reason": "TREND_EMA20_BELOW_EMA50"}

    # Optional RSI upturn gate for V5 experiments.
    if bool(getattr(cfg, "ENTRY_REQUIRE_RSI_UPTURN", False)):
        rsi_series = get_rsi(df, 14)
        rsi_now = safe_last(rsi_series)
        rsi_prev = None
        try:
            rsi_prev = float(rsi_series.iloc[-2])
            if rsi_prev != rsi_prev:
                rsi_prev = None
        except Exception:
            rsi_prev = None
        if rsi_now is None or rsi_prev is None:
            return {"ok": False, "reason": "RSI_DATA_NAN"}
        delta_min = float(getattr(cfg, "ENTRY_RSI_DELTA_MIN", 0.8))
        if (float(rsi_now) - float(rsi_prev)) < float(delta_min):
            return {"ok": False, "reason": "RSI_DELTA_LOW"}

    # Require re-acceleration now.
    if not (float(cur_close) > float(cur_prev_high) and float(cur_close) > float(cur_ema)):
        return {"ok": False, "reason": "REACCEL_FAIL"}

    for pullback_len in range(1, pullback_max + 1):
        breakout_idx = n - 1 - pullback_len
        if breakout_idx <= lookback:
            continue

        prev_high_n = safe_last(high.iloc[breakout_idx - lookback : breakout_idx])
        breakout_close = safe_last(close.iloc[: breakout_idx + 1])
        breakout_vol = safe_last(volume.iloc[: breakout_idx + 1])
        breakout_vol_ma = safe_last(vol_ma.iloc[: breakout_idx + 1])
        breakout_ema = safe_last(ema.iloc[: breakout_idx + 1])

        if None in (prev_high_n, breakout_close, breakout_vol, breakout_vol_ma, breakout_ema):
            continue
        if not (float(breakout_close) > float(prev_high_n)):
            continue
        if not (float(breakout_vol_ma) > 0 and float(breakout_vol) >= float(breakout_vol_ma) * float(vol_mult)):
            continue
        if not (float(breakout_close) > float(breakout_ema)):
            continue

        pull_start = breakout_idx + 1
        pull_end = n - 1  # exclude current re-acceleration candle
        if pull_start >= pull_end:
            continue

        lows = low.iloc[pull_start:pull_end]
        emas = ema.iloc[pull_start:pull_end]
        if lows.empty or emas.empty or len(lows) != len(emas):
            continue
        if bool((lows < emas).any()):
            continue

        if require_retest:
            breakout_level = float(prev_high_n)
            if breakout_level <= 0:
                continue
            pull_lows = lows.astype("float64")
            pull_closes = close.iloc[pull_start:pull_end].astype("float64")
            if pull_closes.empty or len(pull_closes) != len(pull_lows):
                continue
            min_pull_low = float(pull_lows.min())
            touched = bool(min_pull_low <= (breakout_level * (1.0 + retest_touch_tol)))
            no_deep_break = bool(min_pull_low >= (breakout_level * (1.0 - retest_break_tol)))
            reclaimed = bool((pull_closes >= (breakout_level * (1.0 + retest_reclaim_min))).any())
            if not (touched and no_deep_break and reclaimed):
                continue

        quality_metrics = {}
        if require_quality:
            ok_quality, quality_reason, quality_metrics = _v5_breakout_quality_check(
                breakout_level=float(prev_high_n),
                cur_open=float(cur_open),
                cur_high=float(cur_high),
                cur_low=float(cur_low),
                cur_close=float(cur_close),
                cfg=cfg,
            )
            if not ok_quality:
                return {"ok": False, "reason": quality_reason, **dict(quality_metrics or {})}

        swing_low = float(low.iloc[breakout_idx:n].min())
        entry_price = float(cur_close)
        if swing_low <= 0 or entry_price <= swing_low:
            continue
        raw_sl_pct = (entry_price - swing_low) / entry_price
        sl_pct = _v5_clamp_sl_pct(raw_sl_pct, cfg=cfg)
        stop_price = float(entry_price) * (1.0 - float(sl_pct))

        return {
            "ok": True,
            "reason": "V5_BREAKOUT_PULLBACK",
            "entry_price": float(entry_price),
            "stop_price": float(stop_price),
            "sl_pct": float(sl_pct),
            "swing_low": float(swing_low),
            "pullback_bars": int(pullback_len),
            "breakout_level": float(prev_high_n),
            "breakout_close": float(breakout_close),
            "retest_required": bool(require_retest),
            "quality_filter": bool(require_quality),
            **dict(quality_metrics or {}),
        }

    return {"ok": False, "reason": "PATTERN_NOT_FOUND"}


def v5_breakout_pullback_signal(ticker: str, cfg=config):
    interval = str(getattr(cfg, "V5_SIGNAL_INTERVAL", "minute5"))
    lookback = max(60, int(getattr(cfg, "V5_SIGNAL_LOOKBACK", 120)))
    df = pyupbit.get_ohlcv(ticker, interval=interval, count=lookback)
    return v5_breakout_pullback_signal_df(df, cfg=cfg)


def _sr_pivot_levels(df, lookback: int, delta_len: int):
    if df is None or len(df) < 10:
        return [], []
    if ("high" not in df.columns) or ("low" not in df.columns):
        return [], []

    lookback = max(10, int(lookback))
    delta_len = max(1, int(delta_len))
    work = df.tail(max(lookback + delta_len * 2 + 8, 40))
    if len(work) < (delta_len * 2 + 3):
        return [], []

    highs = work["high"].astype("float64").reset_index(drop=True)
    lows = work["low"].astype("float64").reset_index(drop=True)
    n = len(work)
    support_levels = []
    resistance_levels = []

    for i in range(delta_len, n - delta_len):
        lv_low = float(lows.iloc[i])
        lv_high = float(highs.iloc[i])
        left_low = float(lows.iloc[i - delta_len : i].min())
        right_low = float(lows.iloc[i + 1 : i + delta_len + 1].min())
        left_high = float(highs.iloc[i - delta_len : i].max())
        right_high = float(highs.iloc[i + 1 : i + delta_len + 1].max())

        if lv_low <= left_low and lv_low <= right_low:
            support_levels.append(float(lv_low))
        if lv_high >= left_high and lv_high >= right_high:
            resistance_levels.append(float(lv_high))

    # Keep recency while dropping near-duplicates.
    def _dedupe_recent(levels):
        out = []
        for lv in list(levels or []):
            if not out:
                out.append(float(lv))
                continue
            prev = float(out[-1])
            if prev <= 0:
                out.append(float(lv))
                continue
            if abs(float(lv) - prev) / prev < 0.0005:
                continue
            out.append(float(lv))
        return out[-24:]

    return _dedupe_recent(support_levels), _dedupe_recent(resistance_levels)


def _sr_build_zones_15m(df15, cfg=config):
    if df15 is None or len(df15) < 30:
        return [], [], None

    lookback = max(10, int(getattr(cfg, "SR_LOOKBACK", 20)))
    delta_len = max(1, int(getattr(cfg, "SR_DELTA_VOL_LEN", 2)))
    box_mult = max(0.1, float(getattr(cfg, "SR_BOX_ATR_MULT", 1.0)))
    atr15 = get_atr(df15, 14)
    atr_now = safe_last(atr15)
    close_now = safe_last(df15["close"]) if "close" in df15.columns else None
    if atr_now is None or atr_now <= 0:
        if close_now is None or close_now <= 0:
            return [], [], None
        atr_now = float(close_now) * 0.003

    zone_half = max(1e-9, float(atr_now) * float(box_mult) * 0.5)
    support_levels, resistance_levels = _sr_pivot_levels(df15, lookback=lookback, delta_len=delta_len)

    support_zones = []
    for lv in support_levels:
        support_zones.append(
            {
                "low": float(lv) - float(zone_half),
                "high": float(lv) + float(zone_half),
                "pivot": float(lv),
            }
        )

    resistance_zones = []
    for lv in resistance_levels:
        resistance_zones.append(
            {
                "low": float(lv) - float(zone_half),
                "high": float(lv) + float(zone_half),
                "pivot": float(lv),
            }
        )

    support_zones = sorted(support_zones, key=lambda z: float(z["high"]))
    resistance_zones = sorted(resistance_zones, key=lambda z: float(z["low"]))
    return support_zones, resistance_zones, float(atr_now)


def _sr_volume_zscore(volume_series, length: int):
    if volume_series is None:
        return None
    length = max(5, int(length))
    if len(volume_series) < (length + 2):
        return None
    win = volume_series.iloc[-length:].astype("float64")
    mean_v = float(win.mean())
    std_v = float(win.std(ddof=0))
    cur_v = float(volume_series.iloc[-1])
    if std_v <= 1e-12:
        return 0.0
    return float((cur_v - mean_v) / std_v)


def sr_only_entry_signal_df(df15, df5, cfg=config):
    required_cols = {"open", "high", "low", "close", "volume"}
    if df15 is None or len(df15) < 80 or (not required_cols.issubset(df15.columns)):
        return {"ok": False, "reason": "SR_15M_DATA_SHORT"}
    if df5 is None or len(df5) < 40 or (not required_cols.issubset(df5.columns)):
        return {"ok": False, "reason": "SR_5M_DATA_SHORT"}

    c15 = safe_last(df15["close"])
    if c15 is None or c15 <= 0:
        return {"ok": False, "reason": "SR_15M_CLOSE_NAN"}

    support_zones, resistance_zones, atr15 = _sr_build_zones_15m(df15, cfg=cfg)
    if not support_zones:
        return {"ok": False, "reason": "SR_SUPPORT_NOT_FOUND"}

    if bool(getattr(cfg, "SR_USE_EMA_TREND_FILTER", True)):
        ema_len = max(20, int(getattr(cfg, "SR_EMA_LEN", 200)))
        ema_line = get_ema(df15["close"], ema_len)
        ema_now = safe_last(ema_line)
        if ema_now is None:
            return {"ok": False, "reason": "SR_EMA_DATA_NAN"}
        if not (float(c15) > float(ema_now)):
            return {"ok": False, "reason": "SR_EMA_FILTER_FAIL"}

    if bool(getattr(cfg, "SR_USE_VOL_Z_FILTER", True)):
        z_len = max(5, int(getattr(cfg, "SR_VOL_Z_LEN", 50)))
        z_min = float(getattr(cfg, "SR_VOL_Z_MIN", 0.3))
        z_now = _sr_volume_zscore(df15["volume"], z_len)
        if z_now is None:
            return {"ok": False, "reason": "SR_VOL_Z_DATA_SHORT"}
        if float(z_now) < float(z_min):
            return {"ok": False, "reason": "SR_VOL_Z_FILTER_FAIL"}

    supports_below = [z for z in support_zones if float(z["high"]) <= float(c15)]
    if not supports_below:
        supports_cover = [z for z in support_zones if float(z["low"]) <= float(c15) <= float(z["high"])]
        if supports_cover:
            support = supports_cover[-1]
        else:
            return {"ok": False, "reason": "SR_NO_NEAR_SUPPORT"}
    else:
        support = min(supports_below, key=lambda z: abs(float(c15) - float(z["high"])))

    support_low = float(support["low"])
    support_high = float(support["high"])
    if float(c15) <= float(support_high):
        return {"ok": False, "reason": "SR_PRICE_NOT_ABOVE_SUPPORT"}

    resistances_above = [z for z in resistance_zones if float(z["low"]) > float(c15)]
    nearest_res = min(resistances_above, key=lambda z: float(z["low"])) if resistances_above else None

    atr5 = safe_last(get_atr(df5, 14))
    close5 = safe_last(df5["close"])
    prev_high5 = safe_last(df5["high"].iloc[:-1]) if len(df5) >= 2 else None
    low5 = safe_last(df5["low"])
    if None in (close5, prev_high5, low5):
        return {"ok": False, "reason": "SR_5M_DATA_NAN"}
    if atr5 is None or atr5 <= 0:
        atr5 = float(close5) * 0.003

    tol_atr = float(atr5) * max(0.01, float(getattr(cfg, "SR_RETEST_ATR5_MULT", 0.10)))
    tol_fix = float(close5) * max(0.0, float(getattr(cfg, "SR_RETEST_FIXED_PCT", 0.0015)))
    retest_tol = max(float(tol_atr), float(tol_fix))

    touched = (float(low5) <= float(support_high) + float(retest_tol)) and (
        float(low5) >= float(support_high) - float(retest_tol)
    )
    if not touched:
        return {"ok": False, "reason": "SR_RETEST_NOT_TOUCHED"}

    if not (float(close5) > float(prev_high5)):
        return {"ok": False, "reason": "SR_REACCEL_FAIL"}
    if not (float(close5) > float(support_high)):
        return {"ok": False, "reason": "SR_CLOSE_BELOW_SUPPORT_TOP"}

    entry_price = float(close5)
    sl_offset_pct = max(0.0, float(getattr(cfg, "SR_SL_OFFSET_PCT", 0.003)))
    stop_price = float(support_low) * (1.0 - float(sl_offset_pct))
    if stop_price <= 0 or not (float(entry_price) > float(stop_price)):
        return {"ok": False, "reason": "SR_STOP_INVALID"}

    r_pct = (float(entry_price) - float(stop_price)) / float(entry_price)
    if r_pct <= 0:
        return {"ok": False, "reason": "SR_R_INVALID"}

    mode = str(getattr(cfg, "SR_TP_MODE", "R_FIXED")).upper().strip()
    tp_mode = "R_FIXED"
    tp_reason = "EXIT_SR_TP_R"
    tp_price = float(entry_price) * (1.0 + float(max(0.1, float(getattr(cfg, "SR_TP_R", 2.0)))) * float(r_pct))
    if mode in {"RESIST", "MODE_A", "TP_RESIST"}:
        if nearest_res is not None and float(nearest_res["low"]) > float(entry_price):
            tp_mode = "RESIST"
            tp_reason = "EXIT_SR_TP_RESIST"
            tp_price = float(nearest_res["low"])

    return {
        "ok": True,
        "reason": "ENTRY_SR_RETEST",
        "entry_price": float(entry_price),
        "stop_price": float(stop_price),
        "r_pct": float(r_pct),
        "tp_mode": str(tp_mode),
        "tp_reason": str(tp_reason),
        "tp_price": float(tp_price),
        "support_low": float(support_low),
        "support_high": float(support_high),
        "resistance_low": float(nearest_res["low"]) if nearest_res is not None else 0.0,
        "resistance_high": float(nearest_res["high"]) if nearest_res is not None else 0.0,
        "atr15": float(atr15) if atr15 is not None else 0.0,
        "atr5": float(atr5),
        "bar_5m_ts": str(df5.index[-1]),
    }


def sr_only_entry_signal(ticker: str, cfg=config):
    interval_15 = str(getattr(cfg, "SR_ZONE_INTERVAL", "minute15"))
    interval_5 = str(getattr(cfg, "SR_EXEC_INTERVAL", "minute5"))
    lookback_15 = max(
        int(getattr(cfg, "SR_SIGNAL_LOOKBACK_15M", 360)),
        int(getattr(cfg, "SR_EMA_LEN", 200)) + int(getattr(cfg, "SR_LOOKBACK", 20)) + 40,
    )
    lookback_5 = max(int(getattr(cfg, "SR_SIGNAL_LOOKBACK_5M", 240)), 80)
    df15 = pyupbit.get_ohlcv(ticker, interval=interval_15, count=lookback_15)
    df5 = pyupbit.get_ohlcv(ticker, interval=interval_5, count=lookback_5)
    return sr_only_entry_signal_df(df15=df15, df5=df5, cfg=cfg)


def _sr_pivot_points(df, lookback: int, delta_len: int):
    if df is None or len(df) < 10:
        return [], []
    if ("high" not in df.columns) or ("low" not in df.columns):
        return [], []

    lookback = max(10, int(lookback))
    delta_len = max(1, int(delta_len))
    work = df.tail(max(lookback + delta_len * 2 + 8, 60)).copy()
    if len(work) < (delta_len * 2 + 3):
        return [], []

    highs = work["high"].astype("float64").reset_index(drop=True)
    lows = work["low"].astype("float64").reset_index(drop=True)
    idx_list = list(work.index)
    n = len(work)
    supports = []
    resistances = []
    for i in range(delta_len, n - delta_len):
        lv_low = float(lows.iloc[i])
        lv_high = float(highs.iloc[i])
        left_low = float(lows.iloc[i - delta_len : i].min())
        right_low = float(lows.iloc[i + 1 : i + delta_len + 1].min())
        left_high = float(highs.iloc[i - delta_len : i].max())
        right_high = float(highs.iloc[i + 1 : i + delta_len + 1].max())
        candle_range = max(1e-9, float(highs.iloc[i] - lows.iloc[i]))
        if lv_low <= left_low and lv_low <= right_low:
            supports.append(
                {
                    "price": float(lv_low),
                    "candle_range": float(candle_range),
                    "idx": int(i),
                    "ts": str(idx_list[i]),
                }
            )
        if lv_high >= left_high and lv_high >= right_high:
            resistances.append(
                {
                    "price": float(lv_high),
                    "candle_range": float(candle_range),
                    "idx": int(i),
                    "ts": str(idx_list[i]),
                }
            )

    def _dedupe_recent(points):
        out = []
        for p in list(points or []):
            if not out:
                out.append(dict(p))
                continue
            prev = float(out[-1]["price"])
            cur = float(p["price"])
            if prev > 0 and abs(cur - prev) / prev < 0.0005:
                continue
            out.append(dict(p))
        return out[-24:]

    return _dedupe_recent(supports), _dedupe_recent(resistances)


def _sr_volume_zscore_at(volume_series, idx: int, length: int):
    if volume_series is None:
        return None
    length = max(5, int(length))
    idx = int(idx)
    if idx < (length - 1) or idx >= len(volume_series):
        return None
    win = volume_series.iloc[idx - length + 1 : idx + 1].astype("float64")
    mean_v = float(win.mean())
    std_v = float(win.std(ddof=0))
    cur_v = float(volume_series.iloc[idx])
    if std_v <= 1e-12:
        return 0.0
    return float((cur_v - mean_v) / std_v)


def _sr_build_zones_15m_tv(df15, cfg=config):
    if df15 is None or len(df15) < 40:
        return [], [], None
    lookback = max(10, int(getattr(cfg, "SR_TV_LOOKBACK", 20)))
    delta_len = max(1, int(getattr(cfg, "SR_TV_DELTA_VOL_LEN", 2)))
    box_mult = max(0.1, float(getattr(cfg, "SR_TV_BOX_ATR_MULT", 1.0)))
    use_vol_z = bool(getattr(cfg, "SR_TV_USE_VOL_Z_FILTER", True))
    vol_z_len = max(5, int(getattr(cfg, "SR_TV_VOL_Z_LEN", 50)))
    vol_z_min = float(getattr(cfg, "SR_TV_VOL_Z_MIN", 0.3))

    atr15 = get_atr(df15, 14)
    atr_now = safe_last(atr15)
    close_now = safe_last(df15["close"]) if "close" in df15.columns else None
    if atr_now is None or atr_now <= 0:
        if close_now is None or close_now <= 0:
            return [], [], None
        atr_now = float(close_now) * 0.003

    supports, resistances = _sr_pivot_points(df15, lookback=lookback, delta_len=delta_len)
    vol_series = df15["volume"].astype("float64").reset_index(drop=True)
    support_zones = []
    for p in supports:
        z = _sr_volume_zscore_at(vol_series, int(p["idx"]), vol_z_len)
        if use_vol_z and (z is None or float(z) < float(vol_z_min)):
            continue
        half = max(float(atr_now) * float(box_mult) * 0.5, float(p["candle_range"]) * 0.5)
        support_zones.append(
            {
                "id": f"S|{p['ts']}|{float(p['price']):.6f}",
                "low": float(p["price"]) - float(half),
                "high": float(p["price"]) + float(half),
                "pivot": float(p["price"]),
                "pivot_ts": str(p["ts"]),
                "vol_z": float(z) if z is not None else 0.0,
            }
        )

    resistance_zones = []
    for p in resistances:
        z = _sr_volume_zscore_at(vol_series, int(p["idx"]), vol_z_len)
        if use_vol_z and (z is None or float(z) < float(vol_z_min)):
            continue
        half = max(float(atr_now) * float(box_mult) * 0.5, float(p["candle_range"]) * 0.5)
        resistance_zones.append(
            {
                "id": f"R|{p['ts']}|{float(p['price']):.6f}",
                "low": float(p["price"]) - float(half),
                "high": float(p["price"]) + float(half),
                "pivot": float(p["price"]),
                "pivot_ts": str(p["ts"]),
                "vol_z": float(z) if z is not None else 0.0,
            }
        )

    support_zones = sorted(support_zones, key=lambda z: float(z["high"]))
    resistance_zones = sorted(resistance_zones, key=lambda z: float(z["low"]))
    return support_zones, resistance_zones, float(atr_now)


def sr_tv_combo_entry_signal_df(df15, df5, cfg=config):
    out = {
        "ok": False,
        "reason": "TV_INIT",
        "zone_id": "",
        "touch_detected": False,
        "flip_break_detected": False,
        "entry_price": 0.0,
        "support_low": 0.0,
        "support_high": 0.0,
        "resistance_low": 0.0,
        "resistance_high": 0.0,
        "tp_price": 0.0,
        "bar_5m_ts": "",
    }
    required_cols = {"open", "high", "low", "close", "volume"}
    if df15 is None or len(df15) < 80 or (not required_cols.issubset(df15.columns)):
        out["reason"] = "SR_TV_15M_DATA_SHORT"
        return out
    if df5 is None or len(df5) < 20 or (not required_cols.issubset(df5.columns)):
        out["reason"] = "SR_TV_5M_DATA_SHORT"
        return out

    c15 = safe_last(df15["close"])
    if c15 is None or c15 <= 0:
        out["reason"] = "SR_TV_15M_CLOSE_NAN"
        return out

    support_zones, resistance_zones, _atr15 = _sr_build_zones_15m_tv(df15=df15, cfg=cfg)
    if not support_zones:
        out["reason"] = "SR_TV_SUPPORT_NOT_FOUND"
        return out

    if bool(getattr(cfg, "SR_TV_USE_EMA_TREND_FILTER", True)):
        ema_len = max(20, int(getattr(cfg, "SR_TV_EMA_LEN", 200)))
        ema_line = get_ema(df15["close"], ema_len)
        ema_now = safe_last(ema_line)
        if ema_now is None:
            out["reason"] = "SR_TV_EMA_DATA_NAN"
            return out
        if not (float(c15) > float(ema_now)):
            out["reason"] = "SR_TV_EMA_FILTER_FAIL"
            return out

    supports_below = [z for z in support_zones if float(z["high"]) <= float(c15)]
    if not supports_below:
        supports_cover = [z for z in support_zones if float(z["low"]) <= float(c15) <= float(z["high"])]
        support = supports_cover[-1] if supports_cover else None
    else:
        support = min(supports_below, key=lambda z: abs(float(c15) - float(z["high"])))
    if support is None:
        out["reason"] = "SR_TV_NO_NEAR_SUPPORT"
        return out

    support_low = float(support["low"])
    support_high = float(support["high"])
    out["zone_id"] = str(support.get("id", ""))
    out["support_low"] = float(support_low)
    out["support_high"] = float(support_high)

    resistances_above = [z for z in resistance_zones if float(z["low"]) > float(support_high)]
    nearest_res = min(resistances_above, key=lambda z: float(z["low"])) if resistances_above else None
    if nearest_res is not None:
        out["resistance_low"] = float(nearest_res["low"])
        out["resistance_high"] = float(nearest_res["high"])

    close5 = safe_last(df5["close"])
    open5 = safe_last(df5["open"])
    low5 = safe_last(df5["low"])
    prev_high5 = safe_last(df5["high"].iloc[:-1]) if len(df5) >= 2 else None
    if None in (close5, open5, low5, prev_high5):
        out["reason"] = "SR_TV_5M_DATA_NAN"
        return out
    out["bar_5m_ts"] = str(df5.index[-1])

    tol_pct = max(0.0, float(getattr(cfg, "SR_TV_RETEST_TOL_PCT", 0.0015)))
    touch_in_zone = float(support_low) <= float(low5) <= float(support_high)
    touch_upper = abs(float(low5) - float(support_high)) <= (float(support_high) * float(tol_pct))
    out["touch_detected"] = bool(touch_in_zone or touch_upper)
    out["flip_break_detected"] = bool(
        (float(close5) <= float(support_low))
        or (float(out["resistance_high"]) > 0 and float(close5) >= float(out["resistance_high"]))
    )

    if not bool(out["touch_detected"]):
        out["reason"] = "SR_TV_TOUCH_FAIL"
        return out

    bullish = float(close5) > float(open5)
    if not bullish:
        out["reason"] = "SR_TV_NOT_BULLISH"
        return out

    recover = float(close5) > float(support_high)
    break_prev = float(close5) > float(prev_high5)
    base_long_signal = bool(recover or break_prev)
    if not bool(base_long_signal):
        out["reason"] = "SR_TV_RECOVERY_FAIL"
        return out

    if nearest_res is None or float(out["resistance_low"]) <= float(close5):
        out["reason"] = "SR_TV_NO_RESIST_TP"
        return out

    version = str(getattr(cfg, "SR_TV_VERSION", "V1")).upper().strip()
    if version == "V2_RECLAIM":
        if bool(getattr(cfg, "SR_TV_RECLAIM_ON", True)):
            reclaim_ok = bool(float(close5) >= float(support_high))
            if not reclaim_ok:
                out["reason"] = "SR_TV_V2_RECLAIM_FAIL"
                return out
    elif version == "V2_EMA200":
        if bool(getattr(cfg, "SR_TV_EMA200_ON", True)):
            ema_len_5m = max(20, int(getattr(cfg, "SR_TV_EMA_LEN", 200)))
            ema200_5m = get_ema(df5["close"], ema_len_5m)
            ema200_now = safe_last(ema200_5m)
            if ema200_now is None:
                out["reason"] = "SR_TV_V2_EMA200_DATA_NAN"
                return out
            if not (float(close5) > float(ema200_now)):
                out["reason"] = "SR_TV_V2_EMA200_FAIL"
                return out
    elif version == "V2_VOLCONF":
        if bool(getattr(cfg, "SR_TV_VOLCONF_ON", True)):
            ma_n = max(2, int(getattr(cfg, "SR_TV_VOLCONF_MA_N", 20)))
            mult = max(0.0, float(getattr(cfg, "SR_TV_VOLCONF_MULT", 1.0)))
            vol_now_5m = safe_last(df5["volume"])
            vol_ma_5m = safe_last(df5["volume"].rolling(ma_n).mean())
            if vol_now_5m is None or vol_ma_5m is None or float(vol_ma_5m) <= 0:
                out["reason"] = "SR_TV_V2_VOLCONF_DATA_NAN"
                return out
            if not (float(vol_now_5m) >= float(vol_ma_5m) * float(mult)):
                out["reason"] = "SR_TV_V2_VOLCONF_FAIL"
                return out

    out["ok"] = True
    out["reason"] = "ENTRY_SR_TV_COMBO"
    out["entry_price"] = float(close5)
    out["tp_price"] = float(out["resistance_low"])
    return out


def sr_tv_combo_entry_signal(ticker: str, cfg=config):
    interval_15 = str(getattr(cfg, "SR_TV_ZONE_INTERVAL", "minute15"))
    interval_5 = str(getattr(cfg, "SR_TV_EXEC_INTERVAL", "minute5"))
    lookback_15 = max(
        int(getattr(cfg, "SR_TV_SIGNAL_LOOKBACK_15M", 360)),
        int(getattr(cfg, "SR_TV_EMA_LEN", 200)) + int(getattr(cfg, "SR_TV_LOOKBACK", 20)) + 40,
    )
    lookback_5 = max(int(getattr(cfg, "SR_TV_SIGNAL_LOOKBACK_5M", 240)), 80)
    df15 = pyupbit.get_ohlcv(ticker, interval=interval_15, count=lookback_15)
    df5 = pyupbit.get_ohlcv(ticker, interval=interval_5, count=lookback_5)
    return sr_tv_combo_entry_signal_df(df15=df15, df5=df5, cfg=cfg)


def sr_only_tv_combo_entry_signal_df(df15, df5, cfg=config):
    return sr_tv_combo_entry_signal_df(df15=df15, df5=df5, cfg=cfg)


def sr_only_tv_combo_entry_signal(ticker: str, cfg=config):
    return sr_tv_combo_entry_signal(ticker=ticker, cfg=cfg)


def h4_trend_ok(df4h):
    """
    Conservative higher-timeframe trend check used by DAY_MA bypass.
    """
    if df4h is None or len(df4h) < 25 or ("close" not in df4h.columns):
        return False
    try:
        close_series = df4h["close"]
        ema20 = get_ema(close_series, 20)
        close_now = safe_last(close_series)
        ema20_now = safe_last(ema20)
        ema20_prev = float(ema20.iloc[-2])
    except Exception:
        return False
    vals = [close_now, ema20_now, ema20_prev]
    if any(v is None for v in vals):
        return False
    if any((float(v) != float(v)) for v in vals):
        return False
    return bool(float(close_now) > float(ema20_now) and float(ema20_now) > float(ema20_prev))


def vol_ok_recent(df, ma_period: int = 20):
    """
    Conservative volume health check.
    """
    if df is None or len(df) < max(25, int(ma_period) + 2) or ("volume" not in df.columns):
        return False
    try:
        vol_series = df["volume"]
        vol_now = safe_last(vol_series)
        vol_ma_now = safe_last(volume_ma(df, int(ma_period)))
    except Exception:
        return False
    if vol_now is None or vol_ma_now is None:
        return False
    if float(vol_now) != float(vol_now) or float(vol_ma_now) != float(vol_ma_now) or float(vol_ma_now) <= 0:
        return False
    return bool(float(vol_now) > float(vol_ma_now))


def minute_test_signal(ticker):
    """
    강화 분봉 실전형 테스트 신호(1분봉)
    - RSI 상승 전환 + 상승폭
    - 녹색 캔들 확인(옵션)
    - MA 정렬 + 종가가 MA_slow 위(옵션)
    - 거래량 증가(옵션)
    - 최근 구조(고점) 돌파(옵션)
    """
    interval = getattr(config, "MINUTE_TEST_INTERVAL", "minute1")
    df = pyupbit.get_ohlcv(ticker, interval=interval, count=80)
    if df is None or len(df) < 35:
        return False

    debug_reject = bool(getattr(config, "DEBUG_ENTRY_REJECT", False))

    def reject(reason: str):
        if debug_reject:
            print(f"[진입거절_분봉TEST] {ticker} {reason}")
        return False

    rsi = get_rsi(df, 14)
    rsi_now = safe_last(rsi)
    if rsi_now is None:
        return reject("rsi_now_nan")

    try:
        rsi_prev = float(rsi.iloc[-2])
        if rsi_prev != rsi_prev:
            return reject("rsi_prev_nan")
    except Exception:
        return reject("rsi_prev_nan")

    rsi_low = float(getattr(config, "MINUTE_TEST_RSI_LOW", 45))
    rsi_high = float(getattr(config, "MINUTE_TEST_RSI_HIGH", 60))
    use_rsi_cross = bool(getattr(config, "MINUTE_TEST_RSI_CROSS", True))

    if use_rsi_cross:
        if not (rsi_prev < rsi_low and rsi_now > rsi_low):
            return reject("rsi_cross")
    else:
        if not (rsi_low < rsi_now < rsi_high):
            return reject("rsi_band")

    delta = rsi_now - rsi_prev
    delta_min = float(getattr(config, "MINUTE_TEST_RSI_DELTA_MIN", 1.5))
    if delta < delta_min:
        return reject("rsi_delta")

    close_now = safe_last(df["close"])
    if close_now is None:
        return reject("close_nan")

    require_green = bool(getattr(config, "MINUTE_TEST_REQUIRE_GREEN_CANDLE", True))
    if require_green:
        open_now = safe_last(df["open"])
        if open_now is None:
            return reject("open_nan")
        if not (close_now > open_now):
            return reject("not_green")

    ma_fast_now = None
    ma_slow_now = None
    use_ma_filter = bool(getattr(config, "MINUTE_TEST_USE_MA_FILTER", True))
    if use_ma_filter:
        ma_fast_period = int(getattr(config, "MINUTE_TEST_MA_FAST", 5))
        ma_slow_period = int(getattr(config, "MINUTE_TEST_MA_SLOW", 20))
        ma_fast_now = safe_last(get_sma(df["close"], ma_fast_period))
        ma_slow_now = safe_last(get_sma(df["close"], ma_slow_period))
        if ma_fast_now is None or ma_slow_now is None:
            return reject("ma_nan")
        if not (ma_fast_now > ma_slow_now):
            return reject("ma_align")
        if bool(getattr(config, "MINUTE_TEST_REQUIRE_PRICE_ABOVE_SLOW", True)):
            if not (close_now > ma_slow_now):
                return reject("price_below_ma_slow")

    vol_now = None
    vma_now = None
    vol_mult = float(getattr(config, "MINUTE_TEST_VOL_MULT", 1.2))
    use_volume_filter = bool(getattr(config, "MINUTE_TEST_USE_VOLUME_FILTER", True))
    if use_volume_filter:
        if "volume" not in df.columns:
            return reject("no_volume")
        vol_now = safe_last(df["volume"])
        vol_ma_period = int(getattr(config, "MINUTE_TEST_VOL_MA_PERIOD", 20))
        vma_now = safe_last(volume_ma(df, vol_ma_period))
        if vol_now is None or vma_now is None or vma_now <= 0:
            return reject("volume_nan")
        if not (vol_now > vma_now * vol_mult):
            return reject("volume_low")

    recent_high = None
    use_breakout = bool(getattr(config, "MINUTE_TEST_USE_BREAKOUT", getattr(config, "ENTRY_USE_BREAKOUT", True)))
    if use_breakout:
        lookback = int(
            getattr(
                config,
                "MINUTE_TEST_BREAKOUT_LOOKBACK",
                getattr(config, "ENTRY_BREAKOUT_LOOKBACK", 20),
            )
        )
        lookback = max(5, lookback)
        if len(df) < (lookback + 2):
            return reject("breakout_short")
        recent_high = safe_last(df["close"].rolling(lookback).max().iloc[:-1])
        if recent_high is None:
            return reject("recent_high_nan")
        if not (close_now > recent_high):
            return reject("breakout_fail")

    if bool(getattr(config, "DEBUG_TRADE_FLOW", False)):
        ma_fast_txt = f"{ma_fast_now:.6f}" if ma_fast_now is not None else "off"
        ma_slow_txt = f"{ma_slow_now:.6f}" if ma_slow_now is not None else "off"
        vol_txt = f"{vol_now:.6f}" if vol_now is not None else "off"
        vma_txt = f"{vma_now:.6f}" if vma_now is not None else "off"
        recent_high_txt = f"{recent_high:.6f}" if recent_high is not None else "off"
        print(
            f"[진입근거_분봉TEST] {ticker} "
            f"rsi_prev={rsi_prev:.2f} rsi_now={rsi_now:.2f} lo={rsi_low:.2f} hi={rsi_high:.2f} d={delta:.2f} "
            f"ma_fast={ma_fast_txt} ma_slow={ma_slow_txt} "
            f"vol={vol_txt} vma={vma_txt} mult={vol_mult:.2f} recent_high={recent_high_txt} close={close_now:.6f}"
        )

    return True


def scalp_btc_entry_signal(ticker):
    """
    BTC-only scalp entry signal (V1 upgrade):
    - A) min RSI(14) of recent 3 bars <= oversold threshold
    - B) at least one volume spike in recent 3 bars
    - C) 2 of 3 rebound triggers:
         close > prev_high, close > EMA9, RSI upturn
    - D) current 1m low > previous 1m low
    """
    # 4h regime filter: block entries while higher timeframe trend is bearish.
    if not _scalp_btc_regime_allows_entry(ticker):
        return False

    interval = str(getattr(config, "SCALP_BTC_TF", "minute15"))
    rsi_len = int(getattr(config, "SCALP_BTC_RSI_LEN", 14))
    rsi_os = float(getattr(config, "SCALP_BTC_RSI_OS", 28))
    vol_lb = max(5, int(getattr(config, "SCALP_BTC_VOL_LOOKBACK", 20)))
    vol_mult = float(getattr(config, "SCALP_BTC_VOL_SPIKE_MULT", 1.5))
    vol_win = max(1, int(getattr(config, "SCALP_BTC_VOL_SPIKE_WINDOW", 3)))
    ema_fast = max(2, int(getattr(config, "SCALP_BTC_EMA_FAST", 9)))
    min_needed = max(40, vol_lb + vol_win + 5, rsi_len + 5, ema_fast + 5)
    df = pyupbit.get_ohlcv(ticker, interval=interval, count=min_needed + 10)
    if df is None or len(df) < min_needed:
        return False
    if not {"high", "close", "volume"}.issubset(df.columns):
        return False

    close = df["close"]
    high = df["high"]
    volume = df["volume"]

    rsi_series = get_rsi(df, rsi_len)
    ema_fast_series = get_ema(close, ema_fast)
    vol_ma = volume.rolling(vol_lb).mean()

    close_now = safe_last(close)
    ema_now = safe_last(ema_fast_series)
    if None in (close_now, ema_now):
        return False

    try:
        prev_high = float(high.iloc[-2])
        rsi_now = float(rsi_series.iloc[-1])
        rsi_prev = float(rsi_series.iloc[-2])
        rsi_prev2 = float(rsi_series.iloc[-3])
        if (
            prev_high != prev_high
            or rsi_now != rsi_now
            or rsi_prev != rsi_prev
            or rsi_prev2 != rsi_prev2
        ):
            return False
    except Exception:
        return False

    # A) RSI oversold environment using the recent-3 minimum.
    rsi_min3 = min(float(rsi_now), float(rsi_prev), float(rsi_prev2))
    cond_env_rsi = float(rsi_min3) <= float(rsi_os)

    # B) Keep volume-spike requirement in recent window.
    spike_flags = (volume > (vol_ma * vol_mult)).tail(vol_win)
    cond_env_vol = bool(spike_flags.any())
    if not (cond_env_rsi and cond_env_vol):
        return False

    # C) Rebound triggers: pass when at least 2 of 3 hold.
    trig_break_prev_high = float(close_now) > float(prev_high)
    trig_break_ema = float(close_now) > float(ema_now)
    trig_rsi_up = float(rsi_now) > float(rsi_prev)
    triggers_hit = int(trig_break_prev_high) + int(trig_break_ema) + int(trig_rsi_up)
    cond_triggers = triggers_hit >= 2
    if not cond_triggers:
        return False

    # D) 1m higher-low filter to reduce falling-knife entries.
    df_1m = pyupbit.get_ohlcv(ticker, interval="minute1", count=3)
    if df_1m is None or len(df_1m) < 2 or "low" not in df_1m.columns:
        return False
    try:
        low_now = float(df_1m["low"].iloc[-1])
        low_prev = float(df_1m["low"].iloc[-2])
        if low_now != low_now or low_prev != low_prev:
            return False
    except Exception:
        return False
    cond_low_up = float(low_now) > float(low_prev)

    ok = bool(cond_env_rsi and cond_env_vol and cond_triggers and cond_low_up)

    if ok and bool(getattr(config, "DEBUG_TRADE_FLOW", False)):
        print(
            f"[SCALP_BTC_ENTRY] {ticker} "
            f"RSImin3={rsi_min3:.2f}, vol_spike={cond_env_vol}, triggers={triggers_hit}/3, low_up={cond_low_up}"
        )
    return bool(ok)

def scalp_entry_signal(ticker, conservative=False):
    """
    SCALP dedicated entry signal:
    - breakout confirmation bars
    - RSI as secondary filter
    - MA align
    - volume expansion
    """
    df = pyupbit.get_ohlcv(ticker, interval="minute1", count=120)
    if df is None or len(df) < 80:
        return False
    if "volume" not in df.columns:
        return False

    close = df["close"]
    open_ = df["open"]
    volume = df["volume"]
    rsi_series = get_rsi(df, 14)

    close_now = safe_last(close)
    open_now = safe_last(open_)
    rsi_now = safe_last(rsi_series)
    ma_fast_now = safe_last(close.rolling(int(getattr(config, "SCALP_MA_FAST", 5))).mean())
    ma_slow_now = safe_last(close.rolling(int(getattr(config, "SCALP_MA_SLOW", 20))).mean())
    vol_now = safe_last(volume)
    vma_now = safe_last(volume.rolling(int(getattr(config, "SCALP_VOL_MA_PERIOD", 20))).mean())

    if None in (close_now, open_now, rsi_now, ma_fast_now, ma_slow_now, vol_now, vma_now):
        return False
    if float(vma_now) <= 0:
        return False

    try:
        rsi_prev = float(rsi_series.iloc[-2])
        if rsi_prev != rsi_prev:
            return False
    except Exception:
        return False

    lookback = int(getattr(config, "SCALP_BREAKOUT_LOOKBACK", 20))
    confirm_bars = max(0, int(getattr(config, "SCALP_CONFIRM_BARS", 1)))
    if len(df) < (lookback + confirm_bars + 5):
        return False

    # Confirm breakout for N+1 latest bars (N=0 means only current bar).
    latest_idx = len(df) - 1
    latest_breakout_level = None
    for offset in range(confirm_bars + 1):
        ci = latest_idx - offset
        ws = ci - lookback
        if ws < 0:
            return False
        level = float(close.iloc[ws:ci].max())
        if offset == 0:
            latest_breakout_level = level
        if float(close.iloc[ci]) <= level:
            return False

    rsi_min = float(
        getattr(
            config,
            "SCALP_CONSERVATIVE_RSI_MIN" if conservative else "SCALP_RSI_MIN",
            52.0 if conservative else 50.0,
        )
    )
    rsi_delta_min = float(getattr(config, "SCALP_RSI_DELTA_MIN", 0.2))
    rsi_max = float(getattr(config, "SCALP_RSI_MAX", 72.0))
    vol_mult = float(
        getattr(
            config,
            "SCALP_CONSERVATIVE_VOL_MULT" if conservative else "SCALP_VOL_MULT",
            1.5 if conservative else 1.2,
        )
    )
    max_gap_pct = float(getattr(config, "SCALP_BREAKOUT_MAX_GAP_PCT", 0.008))
    max_body_pct = float(getattr(config, "SCALP_MAX_CANDLE_BODY_PCT", 0.012))

    cond_rsi = (
        float(rsi_now) >= rsi_min
        and float(rsi_now) <= rsi_max
        and (float(rsi_now) - float(rsi_prev)) >= rsi_delta_min
    )
    cond_ma = float(ma_fast_now) > float(ma_slow_now) and float(close_now) > float(ma_slow_now)
    cond_vol = float(vol_now) > float(vma_now) * vol_mult
    cond_body = abs(float(close_now) - float(open_now)) / max(float(open_now), 1e-12) <= max_body_pct
    cond_gap = True
    if latest_breakout_level is not None and float(latest_breakout_level) > 0:
        cond_gap = (float(close_now) / float(latest_breakout_level) - 1.0) <= max_gap_pct

    if not (cond_rsi and cond_ma and cond_vol and cond_body and cond_gap):
        return False

    if bool(getattr(config, "DEBUG_TRADE_FLOW", False)):
        print(
            f"[SCALP_진입근거] {ticker} "
            f"cons={'Y' if conservative else 'N'} "
            f"rsi_prev={rsi_prev:.2f} rsi_now={float(rsi_now):.2f} "
            f"ma_fast={float(ma_fast_now):.6f} ma_slow={float(ma_slow_now):.6f} "
            f"vol={float(vol_now):.6f} vma={float(vma_now):.6f} mult={vol_mult:.2f} "
            f"body_ok={int(cond_body)} gap_ok={int(cond_gap)}"
        )
    return True


def detect_momentum_candidate(ticker):
    df = pyupbit.get_ohlcv(ticker, interval="minute1", count=90)
    if df is None or len(df) < 70:
        return False
    if "volume" not in df.columns:
        return False

    close = df["close"]
    volume = df["volume"]

    v_now = safe_last(volume)
    vma20 = safe_last(volume.rolling(20).mean())
    if v_now is None or vma20 is None or float(vma20) <= 0:
        return False

    close_now = safe_last(close)
    close_prev = safe_last(close.iloc[:-1])
    recent_high = safe_last(close.rolling(20).max().iloc[:-1])
    ma5 = safe_last(close.rolling(5).mean())
    ma20 = safe_last(close.rolling(20).mean())
    ma60 = safe_last(close.rolling(60).mean())
    rsi_series = get_rsi(df, 14)
    rsi_now = safe_last(rsi_series)
    try:
        rsi_prev = float(rsi_series.iloc[-2])
        if rsi_prev != rsi_prev:
            return False
    except Exception:
        return False

    if None in (close_now, close_prev, recent_high, ma5, ma20, ma60, rsi_now):
        return False

    v5 = float(volume.tail(5).sum())
    pv5 = float(volume.iloc[-10:-5].sum())
    if pv5 <= 0:
        return False

    cond_volume_spike = float(v_now) > float(vma20) * 1.5 and v5 > pv5 * 1.4
    cond_breakout = float(close_now) > float(recent_high)
    cond_trend_align = float(ma5) > float(ma20)
    cond_rsi_cross = float(rsi_prev) <= 50.0 and float(rsi_now) > 50.0
    cond_not_downtrend = float(ma20) >= float(ma60) and float(close_now) >= float(ma20) and float(close_now) >= float(close_prev)

    return bool(cond_volume_spike and cond_breakout and cond_trend_align and cond_rsi_cross and cond_not_downtrend)


def get_market_regime():
    """
    Classify market regime from BTC daily trend:
    - HALT / LOW / MID / FULL
    """
    df = pyupbit.get_ohlcv("KRW-BTC", interval="day", count=60)
    if df is None or len(df) < 30:
        return "MID"  # Conservative fallback on missing data.

    ma_fast = df["close"].rolling(config.BTC_REGIME_FAST_MA).mean().iloc[-1]
    ma_slow = df["close"].rolling(config.BTC_REGIME_SLOW_MA).mean().iloc[-1]
    rsi = get_rsi(df, config.BTC_REGIME_RSI_PERIOD).iloc[-1]

    uptrend = ma_fast >= ma_slow

    # Downtrend
    if not uptrend:
        if rsi < 45:
            return "HALT"
        return "LOW"

    # Uptrend
    if rsi >= 60:
        return "FULL"
    return "MID"

