"""지표 계산 함수와 전략 진입/필터 신호 함수를 제공하는 모듈."""

from collections import Counter
import time

import pyupbit

import config


_MINUTE_REJECT_COUNTER = Counter()
_MINUTE_REJECT_TOTAL = 0
_MINUTE_REJECT_LAST_PRINT_TS = time.time()


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


def safe_last(series):
    try:
        v = float(series.iloc[-1])
        if v != v:
            return None
        return v
    except Exception:
        return None


def volume_ma(df, period):
    return get_sma(df["volume"], period)


def get_rsi(df, period=14):
    delta = df["close"].diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    gain = up.rolling(period).mean()
    loss = down.rolling(period).mean()
    rs = gain / (loss + 1e-12)
    return 100 - (100 / (1 + rs))


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

