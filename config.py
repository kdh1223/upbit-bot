# ===============================
# 🎛 BOT 전략 모드
# ===============================
BOT_MODE = "TEST"      # TEST = 분봉 테스트
REAL_ORDER = True      # 소액 실주문 테스트
REQUIRE_ORDER_CONFIRM = True

# ===============================
# 📊 기본 설정
# ===============================
TOP_N = 10
UNIVERSE_SCAN_N = 40
SPIKE_CANDIDATE_MAX = 3
SPIKE_RANK_SURGE_MIN = 5
SPIKE_VOL_LOOKBACK_MIN = 5
SPIKE_VOL_MULT = 1.2
SPIKE_RSI_CROSS_LEVEL = 50
SPIKE_MA_FAST = 5
SPIKE_MA_SLOW = 20
REFRESH_MIN = 15
POLL_SEC = 1
MIN_ORDER_KRW = 5_000
EXCLUDE_CAUTION = True
DEBUG_STOP_PCT = False
DEBUG_TRADE_FLOW = True
DEBUG_ENTRY_REJECT = False

# ===============================
# 💰 비용
# ===============================
# 업비트 KRW 일반주문: 매수 0.05% + 매도 0.05% = 왕복 0.10%
# 시장가 슬리피지 포함 보수적으로 0.15%
COST_ROUNDTRIP_PCT = 0.0015  # 0.15%

# ===============================
# 🚫 시장 차단 기능 OFF (테스트용)
# ===============================
USE_MARKET_REGIME = False  # 🔥 테스트 동안은 HALT로 막히지 않게 끔

# ⚠️ 봇이 항상 참조하는 테이블이라 "반드시 존재"해야 함
# (USE_MARKET_REGIME=False라도 코드가 읽을 수 있음)
REGIME_INVEST_FRAC = {
    "HALT": 1.0,   # 테스트에서는 막지 않도록 1.0
    "LOW":  1.0,
    "MID":  1.0,
    "FULL": 1.0,
}
REGIME_HOLDINGS_MULT = {
    "HALT": 1.0,   # 테스트에서는 0/0 방지
    "LOW":  1.0,
    "MID":  1.0,
    "FULL": 1.0,
}

# (참고: MAIN 모드로 갈 때는 원래 값으로 되돌리면 됨)
# REGIME_INVEST_FRAC = {"HALT":0.0,"LOW":0.30,"MID":0.70,"FULL":1.00}
# REGIME_HOLDINGS_MULT = {"HALT":0.0,"LOW":0.50,"MID":0.70,"FULL":1.00}

# ===============================
# 🧠 시장 컨디션 지표 파라미터 (함수에서 참조할 수 있어 보관)
# ===============================
BTC_REGIME_FAST_MA = 5
BTC_REGIME_SLOW_MA = 20
BTC_REGIME_RSI_PERIOD = 14

# ===============================
# 🧱 포지션 크기
# ===============================
TEST_EQUITY_CAP = 200_000
TEST_PER_TRADE_KRW = 30_000
TEST_MAX_HOLDINGS = 2

ACCOUNT_TIERS = [
    {"min_equity": 0, "max_holdings": 2},
    {"min_equity": 500_000, "max_holdings": 3},
    {"min_equity": 1_000_000, "max_holdings": 4},
    {"min_equity": 3_000_000, "max_holdings": 5},
]

# ===============================
# 🧨 손절/익절/트레일
# ===============================
STOP_LOSS_PCT = 0.010  # -1.0%
STOP_LOSS_MODE = "ATR"  # "FIXED" or "ATR"
STOP_LOSS_ATR_PERIOD = 14
STOP_LOSS_ATR_MULT_TABLE = {"LOW": 1.1, "MID": 1.3, "FULL": 1.5, "HALT": 1.0}
STOP_LOSS_MIN_PCT = 0.010
STOP_LOSS_MAX_PCT = 0.025

TP_TABLE = {
    "LOW":  {"TP1_PCT": 0.005, "TP2_PCT": 0.010, "TRAIL_BACK_PCT": 0.004},
    "MID":  {"TP1_PCT": 0.008, "TP2_PCT": 0.016, "TRAIL_BACK_PCT": 0.005},
    "FULL": {"TP1_PCT": 0.012, "TP2_PCT": 0.025, "TRAIL_BACK_PCT": 0.007},
    "HALT": {"TP1_PCT": 0.0,   "TP2_PCT": 0.0,   "TRAIL_BACK_PCT": 0.0},
}

TP1_SELL_RATIO = 0.50
TP2_SELL_RATIO = 0.50

COOLDOWN_PROFIT_MIN = 10
COOLDOWN_LOSS_MIN = 30

# ===============================
# 📈 메인 전략 (테스트에서는 사실상 미사용이지만, 참조될 수 있어 유지)
# ===============================
K_DEFAULT = 0.5
AUTO_K = True
K_LOOKBACK_DAYS = 30

K_CANDIDATES = [
    0.10, 0.15, 0.20, 0.25, 0.30,
    0.35, 0.40, 0.45, 0.50, 0.55,
    0.60, 0.65, 0.70, 0.75, 0.80,
    0.85, 0.90, 0.95
]

# ===============================
# ⏱ 4시간/분봉 보조 필터 (테스트에서는 OFF 권장)
# ===============================
USE_INTRADAY_FILTER = False
INTRADAY_TREND_INTERVAL = "minute240"
INTRADAY_FAST_MA = 20
INTRADAY_SLOW_MA = 60

ENTRY_MINUTE_INTERVAL = "minute5"
ENTRY_FAST_MA = 5
ENTRY_SLOW_MA = 20
ENTRY_RSI_PERIOD = 14
ENTRY_RSI_MAX = 70
ENTRY_USE_VOLUME_FILTER = False
ENTRY_VOL_MA_PERIOD = 20
ENTRY_VOL_MULT = 1.1
ENTRY_REQUIRE_RSI_UPTURN = False
ENTRY_RSI_DELTA_MIN = 0.8

# ===============================
# 🧪 TEST 전략 (분봉)
# ===============================
USE_MINUTE_TEST_STRATEGY = True
MINUTE_TEST_INTERVAL = "minute1"

# 🔥 신호 잘 나오게 완화(테스트용)
MINUTE_TEST_RSI_LOW = 45
MINUTE_TEST_RSI_HIGH = 60
MINUTE_TEST_RSI_CROSS = True
MINUTE_TEST_RSI_DELTA_MIN = 1.5
MINUTE_TEST_USE_VOLUME_FILTER = True
MINUTE_TEST_VOL_MA_PERIOD = 20
MINUTE_TEST_VOL_MULT = 1.2
MINUTE_TEST_REQUIRE_GREEN_CANDLE = True
MINUTE_TEST_USE_MA_FILTER = True
MINUTE_TEST_MA_FAST = 5
MINUTE_TEST_MA_SLOW = 20
MINUTE_TEST_REQUIRE_PRICE_ABOVE_SLOW = True
MINUTE_TEST_PER_TRADE_KRW = 30_000

# ===============================
# 📂 상태/로그
# ===============================
STATE_FILE = "bot_state.json"
STATE_SAVE_INTERVAL_SEC = 30

TRADE_LOG_PATH = "trade_log.csv"
STATUS_PRINT_SEC = 60

# ===============================
# 📊 성적표
# ===============================
AUTO_REPORT = True
AUTO_REPORT_MIN_INTERVAL_SEC = 30
AUTO_REPORT_QUIET = True
INITIAL_CAPITAL = 1_000_000

# ===============================
# 🧩 분할 매수
# ===============================
POSITION_TARGET_MULT = 2.0
POSITION_MAX_BUY_COUNT = 2
DUST_CLOSE_AS_CLOSED = True

# ===============================
# 🚀 캐시
# ===============================
DAY_FILTER_CACHE_SEC = 60
INTRADAY_FILTER_CACHE_SEC = 30
MINUTE_ENTRY_CACHE_SEC = 10

# ===============================
# 🔁 주문 재시도 + 주문 로그
# ===============================
ORDER_RETRY_MAX = 3
ORDER_RETRY_SLEEP_SEC = 0.35
ORDER_LOG_PATH = "order_log.csv"
