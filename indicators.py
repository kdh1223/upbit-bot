import pyupbit
import config


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


def check_filters(ticker):
    """
    종목(일봉) 필터: MA5>MA20 & RSI<70
    (기존 유지: '일봉 필터' 역할)
    """
    df = pyupbit.get_ohlcv(ticker, interval="day", count=40)
    if df is None or len(df) < 25:
        return False
    ma5 = get_ma(df, 5)
    ma20 = get_ma(df, 20)
    rsi = get_rsi(df, 14)
    return bool(ma5.iloc[-1] > ma20.iloc[-1] and rsi.iloc[-1] < 70)


def intraday_trend_ok(ticker):
    """
    4시간봉 보조 필터: MA20>MA60
    (기존 minute60 → 우리가 하기로 한 minute240로 변경)
    """
    interval = getattr(config, "INTRADAY_TREND_INTERVAL", "minute240")
    df = pyupbit.get_ohlcv(ticker, interval=interval, count=120)
    if df is None or len(df) < (config.INTRADAY_SLOW_MA + 5):
        return True  # 데이터 부족이면 막지 않음(과도한 차단 방지)

    ma_fast = df["close"].rolling(config.INTRADAY_FAST_MA).mean()
    ma_slow = df["close"].rolling(config.INTRADAY_SLOW_MA).mean()
    return bool(ma_fast.iloc[-1] > ma_slow.iloc[-1])


def minute_entry_ok(ticker):
    """
    분봉(ENTRY_MINUTE_INTERVAL) 진입 타이밍 필터:
    - MA(ENTRY_FAST) > MA(ENTRY_SLOW)
    - RSI < ENTRY_RSI_MAX (과열 회피)
    """
    interval = getattr(config, "ENTRY_MINUTE_INTERVAL", "minute5")
    df = pyupbit.get_ohlcv(ticker, interval=interval, count=80)
    if df is None or len(df) < 30:
        return True  # 데이터 부족이면 막지 않음

    ma_fast = get_sma(df["close"], getattr(config, "ENTRY_FAST_MA", 5))
    ma_slow = get_sma(df["close"], getattr(config, "ENTRY_SLOW_MA", 20))
    rsi = get_rsi(df, getattr(config, "ENTRY_RSI_PERIOD", 14))

    ma_fast_now = safe_last(ma_fast)
    ma_slow_now = safe_last(ma_slow)
    rsi_now = safe_last(rsi)
    if ma_fast_now is None or ma_slow_now is None or rsi_now is None:
        return True

    rsi_max = float(getattr(config, "ENTRY_RSI_MAX", 70))
    if not (ma_fast_now > ma_slow_now and rsi_now < rsi_max):
        return False

    if bool(getattr(config, "ENTRY_REQUIRE_RSI_UPTURN", False)):
        rsi_prev = None
        try:
            rsi_prev = float(rsi.iloc[-2])
            if rsi_prev != rsi_prev:
                rsi_prev = None
        except Exception:
            rsi_prev = None

        if rsi_prev is None:
            return False

        delta_min = float(getattr(config, "ENTRY_RSI_DELTA_MIN", 0.8))
        if (rsi_now - rsi_prev) < delta_min:
            return False

    if bool(getattr(config, "ENTRY_USE_VOLUME_FILTER", False)):
        if "volume" not in df.columns:
            return False
        v_now = safe_last(df["volume"])
        vma = safe_last(volume_ma(df, getattr(config, "ENTRY_VOL_MA_PERIOD", 20)))
        if v_now is None or vma is None or vma <= 0:
            return False
        vol_mult = float(getattr(config, "ENTRY_VOL_MULT", 1.1))
        if not (v_now > vma * vol_mult):
            return False

    return True


def minute_test_signal(ticker):
    """
    분봉 단독 테스트 신호(옵션):
    RSI가 중립 구간이면 진입 시도
    """
    interval = getattr(config, "MINUTE_TEST_INTERVAL", "minute1")
    df = pyupbit.get_ohlcv(ticker, interval=interval, count=80)
    if df is None or len(df) < 30:
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

    rsi_prev = None
    try:
        rsi_prev = float(rsi.iloc[-2])
        if rsi_prev != rsi_prev:
            rsi_prev = None
    except Exception:
        rsi_prev = None
    if rsi_prev is None:
        return reject("rsi_prev_nan")

    lo = float(getattr(config, "MINUTE_TEST_RSI_LOW", 45))
    hi = float(getattr(config, "MINUTE_TEST_RSI_HIGH", 60))
    if not (lo < rsi_now < hi):
        return reject("rsi_band")

    use_cross = bool(getattr(config, "MINUTE_TEST_RSI_CROSS", True))
    if use_cross and not (rsi_prev < lo and rsi_now > lo):
        return reject("rsi_cross")

    delta = rsi_now - rsi_prev
    delta_min = float(getattr(config, "MINUTE_TEST_RSI_DELTA_MIN", 1.5))
    if delta < delta_min:
        return reject("rsi_delta")

    open_now = safe_last(df["open"])
    close_now = safe_last(df["close"])
    if open_now is None or close_now is None:
        return reject("candle_nan")

    if bool(getattr(config, "MINUTE_TEST_REQUIRE_GREEN_CANDLE", True)):
        if not (close_now > open_now):
            return reject("not_green_candle")

    ma_fast_now = None
    ma_slow_now = None
    if bool(getattr(config, "MINUTE_TEST_USE_MA_FILTER", True)):
        ma_fast = get_sma(df["close"], int(getattr(config, "MINUTE_TEST_MA_FAST", 5)))
        ma_slow = get_sma(df["close"], int(getattr(config, "MINUTE_TEST_MA_SLOW", 20)))
        ma_fast_now = safe_last(ma_fast)
        ma_slow_now = safe_last(ma_slow)
        if ma_fast_now is None or ma_slow_now is None:
            return reject("ma_nan")
        if not (ma_fast_now > ma_slow_now):
            return reject("ma_align")
        if bool(getattr(config, "MINUTE_TEST_REQUIRE_PRICE_ABOVE_SLOW", True)):
            if not (close_now > ma_slow_now):
                return reject("price_below_slow")

    vol_now = None
    vma_now = None
    vol_mult = float(getattr(config, "MINUTE_TEST_VOL_MULT", 1.2))
    if bool(getattr(config, "MINUTE_TEST_USE_VOLUME_FILTER", True)):
        if "volume" not in df.columns:
            return reject("no_volume")
        vol_now = safe_last(df["volume"])
        vma_now = safe_last(volume_ma(df, int(getattr(config, "MINUTE_TEST_VOL_MA_PERIOD", 20))))
        if vol_now is None or vma_now is None or vma_now <= 0:
            return reject("volume_nan")
        if not (vol_now > vma_now * vol_mult):
            return reject("volume_low")

    if bool(getattr(config, "DEBUG_TRADE_FLOW", False)):
        if ma_fast_now is None:
            ma_fast_now = safe_last(get_sma(df["close"], int(getattr(config, "MINUTE_TEST_MA_FAST", 5))))
        if ma_slow_now is None:
            ma_slow_now = safe_last(get_sma(df["close"], int(getattr(config, "MINUTE_TEST_MA_SLOW", 20))))
        if vol_now is None and "volume" in df.columns:
            vol_now = safe_last(df["volume"])
        if vma_now is None and "volume" in df.columns:
            vma_now = safe_last(volume_ma(df, int(getattr(config, "MINUTE_TEST_VOL_MA_PERIOD", 20))))

        ma_fast_txt = f"{ma_fast_now:.6f}" if ma_fast_now is not None else "na"
        ma_slow_txt = f"{ma_slow_now:.6f}" if ma_slow_now is not None else "na"
        vol_txt = f"{vol_now:.6f}" if vol_now is not None else "na"
        vma_txt = f"{vma_now:.6f}" if vma_now is not None else "na"
        print(
            f"[진입근거_분봉TEST] {ticker} "
            f"rsi_prev={rsi_prev:.2f} rsi_now={rsi_now:.2f} "
            f"lo={lo:.2f} hi={hi:.2f} d={delta:.2f} "
            f"ma_fast={ma_fast_txt} ma_slow={ma_slow_txt} "
            f"vol={vol_txt} vma={vma_txt} mult={vol_mult:.2f}"
        )

    return True


def detect_momentum_candidate(ticker):
    df = pyupbit.get_ohlcv(ticker, interval="minute1", count=60)
    if df is None or len(df) < 30:
        return False
    if "volume" not in df.columns:
        return False

    close = df["close"]
    volume = df["volume"]

    vma20 = safe_last(volume.rolling(20).mean())
    v_now = safe_last(volume)
    if vma20 is None or v_now is None or vma20 <= 0:
        return False

    recent_high = safe_last(close.rolling(20).max().iloc[:-1])
    close_now = safe_last(close)
    ma5 = safe_last(close.rolling(5).mean())
    ma20 = safe_last(close.rolling(20).mean())
    rsi = safe_last(get_rsi(df, 14))

    if recent_high is None or close_now is None or ma5 is None or ma20 is None or rsi is None:
        return False

    cond_volume_spike = v_now > (vma20 * 1.5)
    cond_breakout = close_now > recent_high
    cond_trend_align = ma5 > ma20
    cond_rsi_mid = rsi > 50

    return bool(cond_volume_spike and cond_breakout and cond_trend_align and cond_rsi_mid)


def get_market_regime():
    """
    BTC 일봉 기준으로 시장 컨디션을 4단계로 분류:
    - HALT / LOW / MID / FULL
    """
    df = pyupbit.get_ohlcv("KRW-BTC", interval="day", count=60)
    if df is None or len(df) < 30:
        return "MID"  # 데이터 부족 시 너무 보수적으로 막지 않음

    ma_fast = df["close"].rolling(config.BTC_REGIME_FAST_MA).mean().iloc[-1]
    ma_slow = df["close"].rolling(config.BTC_REGIME_SLOW_MA).mean().iloc[-1]
    rsi = get_rsi(df, config.BTC_REGIME_RSI_PERIOD).iloc[-1]

    uptrend = ma_fast >= ma_slow

    # 🔻 하락 추세
    if not uptrend:
        if rsi < 45:
            return "HALT"
        return "LOW"

    # 🔺 상승 추세
    if rsi >= 60:
        return "FULL"
    return "MID"
