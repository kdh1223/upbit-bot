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
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def check_filters(ticker):
    """
    종목(일봉) 필터: MA5>MA20 & RSI<70
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
    시간봉(1시간봉) 보조 필터: MA20>MA60
    """
    df = pyupbit.get_ohlcv(ticker, interval="minute60", count=80)
    if df is None or len(df) < 61:
        return True
    ma_fast = df["close"].rolling(config.INTRADAY_FAST_MA).mean()
    ma_slow = df["close"].rolling(config.INTRADAY_SLOW_MA).mean()
    return bool(ma_fast.iloc[-1] > ma_slow.iloc[-1])

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
        # RSI까지 약하면 완전 차단
        if rsi < 45:
            return "HALT"
        # 추세는 약하지만 RSI가 버티면 'LOW'로 소액만
        return "LOW"

    # 🔺 상승 추세
    if rsi >= 60:
        return "FULL"
    # 상승이지만 애매하면 MID
    return "MID"
