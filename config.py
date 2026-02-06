# ===============================
# 🎛 BOT 전략 모드 스위치
# ===============================
# "TEST"  → 분봉 단독 테스트 전략만
# "MAIN"  → 일봉 + 4시간 + 분봉 타이밍 실전 전략
BOT_MODE = "TEST"     # 🔥 여기만 바꾸면 전략 전체 변경

# ===============================
# 🔒 주문 모드
# ===============================
REAL_ORDER = False    # TEST 단계는 반드시 False
REQUIRE_ORDER_CONFIRM = True

# ===============================
# 📊 공통 기본 설정
# ===============================
TOP_N = 20
REFRESH_MIN = 15
POLL_SEC = 1
MIN_ORDER_KRW = 5_000

# ===============================
# 💰 비용
# ===============================
COST_ROUNDTRIP_PCT = 0.0015  # 0.15%

# ===============================
# 🧠 시장 컨디션
# ===============================
USE_MARKET_REGIME = True
BTC_REGIME_FAST_MA = 5
BTC_REGIME_SLOW_MA = 20
BTC_REGIME_RSI_PERIOD = 14

REGIME_INVEST_FRAC = {
    "HALT": 0.0,
    "LOW":  0.30,
    "MID":  0.70,
    "FULL": 1.00,
}
REGIME_HOLDINGS_MULT = {
    "HALT": 0.0,
    "LOW":  0.50,
    "MID":  0.70,
    "FULL": 1.00,
}

# ===============================
# 🧱 포지션 크기 설정
# ===============================
TEST_EQUITY_CAP = 200_000
TEST_PER_TRADE_KRW = 10_000
TEST_MAX_HOLDINGS = 1

ACCOUNT_TIERS = [
    {"min_equity": 1_000_000, "max_holdings": 2},
    {"min_equity": 2_000_000, "max_holdings": 3},
    {"min_equity": 5_000_000, "max_holdings": 5},
]

# ===============================
# 🧨 손절 / 익절 / 트레일
# ===============================
STOP_LOSS_PCT = 0.010

TP_TABLE = {
    "LOW":  {"TP1_PCT": 0.006, "TP2_PCT": 0.012, "TRAIL_BACK_PCT": 0.005},
    "MID":  {"TP1_PCT": 0.010, "TP2_PCT": 0.020, "TRAIL_BACK_PCT": 0.006},
    "FULL": {"TP1_PCT": 0.015, "TP2_PCT": 0.030, "TRAIL_BACK_PCT": 0.010},
    "HALT": {"TP1_PCT": 0.0,   "TP2_PCT": 0.0,   "TRAIL_BACK_PCT": 0.0},
}

TP1_SELL_RATIO = 0.50
TP2_SELL_RATIO = 0.50

COOLDOWN_PROFIT_MIN = 10
COOLDOWN_LOSS_MIN = 30

# ===============================
# 📈 변동성 돌파 (MAIN 모드 전용)
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
# 📅 MAIN 전략 필터 (일봉 + 4시간 + 분봉)
# ===============================
USE_INTRADAY_FILTER = (BOT_MODE == "MAIN")
INTRADAY_TREND_INTERVAL = "minute240"
INTRADAY_FAST_MA = 20
INTRADAY_SLOW_MA = 60

ENTRY_MINUTE_INTERVAL = "minute5"
ENTRY_FAST_MA = 5
ENTRY_SLOW_MA = 20
ENTRY_RSI_PERIOD = 14
ENTRY_RSI_MAX = 70

# ===============================
# 🧪 TEST 전략 (분봉 단독)
# ===============================
USE_MINUTE_TEST_STRATEGY = (BOT_MODE == "TEST")
MINUTE_TEST_INTERVAL = "minute1"
MINUTE_TEST_RSI_LOW = 48
MINUTE_TEST_RSI_HIGH = 62
MINUTE_TEST_PER_TRADE_KRW = 10_000

# ===============================
# 📂 상태/로그
# ===============================
STATE_FILE = "bot_state.json"
STATE_SAVE_INTERVAL_SEC = 30
TRADE_LOG_PATH = "trade_log.csv"
STATUS_PRINT_SEC = 60

# ===============================
# 📊 자동 성적표
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

# ===== 주문 재시도 =====
ORDER_RETRY_MAX = 3
ORDER_RETRY_SLEEP_SEC = 0.35

# 주문 시도 로그
ORDER_LOG_PATH = "order_log.csv"
