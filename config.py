# ===== 실전 여부 =====
REAL_ORDER = True  # True=실주문, False=모의

TOP_N = 20
REFRESH_MIN = 15
POLL_SEC = 1

# ===== 변동성 돌파 K =====
K_DEFAULT = 0.5
AUTO_K = True
K_LOOKBACK_DAYS = 30

K_CANDIDATES = [
    0.10, 0.15, 0.20, 0.25, 0.30,
    0.35, 0.40, 0.45, 0.50, 0.55,
    0.60, 0.65, 0.70, 0.75, 0.80,
    0.85, 0.90, 0.95
]

# ===== 비용(중요) =====
# 업비트 KRW 일반주문: 매수 0.05% + 매도 0.05% = 왕복 0.10%
# 시장가 슬리피지까지 보수 포함 (0.15% 권장)
COST_ROUNDTRIP_PCT = 0.0015  # 0.15%

# ===== 손절/익절/트레일 =====
STOP_LOSS_PCT = 0.010  # -1.0%

# 시장 상태별 익절/트레일 (유동)
TP_TABLE = {
    "LOW":  {"TP1_PCT": 0.006, "TP2_PCT": 0.012, "TRAIL_BACK_PCT": 0.005},
    "MID":  {"TP1_PCT": 0.010, "TP2_PCT": 0.020, "TRAIL_BACK_PCT": 0.006},
    "FULL": {"TP1_PCT": 0.015, "TP2_PCT": 0.030, "TRAIL_BACK_PCT": 0.010},
    "HALT": {"TP1_PCT": 0.0,   "TP2_PCT": 0.0,   "TRAIL_BACK_PCT": 0.0},
}

TP1_SELL_RATIO = 0.50
TP2_SELL_RATIO = 0.50

# ===== 쿨타임 =====
COOLDOWN_PROFIT_MIN = 10
COOLDOWN_LOSS_MIN = 30

# ===== 시간봉 보조 필터 =====
USE_INTRADAY_FILTER = True
INTRADAY_FAST_MA = 20
INTRADAY_SLOW_MA = 60

# ===== 테스트 구간 =====
TEST_EQUITY_CAP = 200_000
TEST_PER_TRADE_KRW = 10_000
TEST_MAX_HOLDINGS = 1

# ===== 실전 확장 구간 (100% 분할 기본) =====
ACCOUNT_TIERS = [
    {"min_equity": 1_000_000, "max_holdings": 2},
    {"min_equity": 2_000_000, "max_holdings": 3},
    {"min_equity": 5_000_000, "max_holdings": 5},
]

# ===== 시장 컨디션(비중 자동 조절) =====
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

# ===== 주문 최소금액 / 안전 =====
MIN_ORDER_KRW = 5_000
DUST_CLOSE_AS_CLOSED = True

# ===== API 호출 완화(캐시) =====
DAY_FILTER_CACHE_SEC = 60
INTRADAY_FILTER_CACHE_SEC = 30

# ===== 실전 주문 확인(안전장치) =====
# REAL_ORDER=True일 때 시작 시 'yes' 입력해야 진행
REQUIRE_ORDER_CONFIRM = True

# ===== 상태 저장/복구 =====
STATE_FILE = "bot_state.json"
STATE_SAVE_INTERVAL_SEC = 30

# 로그/출력
TRADE_LOG_PATH = "trade_log.csv"
STATUS_PRINT_SEC = 60


