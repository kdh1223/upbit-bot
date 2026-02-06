import pyupbit
import config


def get_ma(df, period):
    return df["close"].rolling(period).mean()


def get_rsi(df, period=14):
    delta = df["close"].diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    gain = up.rolling(period).mean()
    loss = down.rolling(period).mean()
    rs = gain / (loss + 1e-12)
    return 100 - (100 / (1 + rs))


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

    ma_fast = df["close"].rolling(getattr(config, "ENTRY_FAST_MA", 5)).mean()
    ma_slow = df["close"].rolling(getattr(config, "ENTRY_SLOW_MA", 20)).mean()
    rsi = get_rsi(df, getattr(config, "ENTRY_RSI_PERIOD", 14))

    rsi_max = float(getattr(config, "ENTRY_RSI_MAX", 70))
    return bool(ma_fast.iloc[-1] > ma_slow.iloc[-1] and rsi.iloc[-1] < rsi_max)


def minute_test_signal(ticker):
    """
    분봉 단독 테스트 신호(옵션):
    RSI가 중립 구간이면 진입 시도
    """
    interval = getattr(config, "MINUTE_TEST_INTERVAL", "minute1")
    df = pyupbit.get_ohlcv(ticker, interval=interval, count=80)
    if df is None or len(df) < 30:
        return False

    r = float(get_rsi(df, 14).iloc[-1])
    lo = float(getattr(config, "MINUTE_TEST_RSI_LOW", 48))
    hi = float(getattr(config, "MINUTE_TEST_RSI_HIGH", 62))
    return bool(lo < r < hi)


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
