"""모드, 자금 배분, 필터, 리스크, 로그 설정을 모아둔 중앙 설정 파일."""

import os


def _env_str(name: str, default: str) -> str:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return str(default)
    return str(raw).strip()


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        try:
            return int(default)
        except Exception:
            return 0
    try:
        return int(str(raw).strip())
    except Exception:
        try:
            return int(default)
        except Exception:
            return 0


# ===============================
# BOT mode
# ===============================
MODE = "MAIN"  # MAIN / SAFE / TEST
BOT_MODE = MODE  # backward compatibility
REAL_ORDER = MODE != "TEST"
REQUIRE_ORDER_CONFIRM = False

ENABLE_MAIN_STRATEGY = True
ENABLE_SCALP_STRATEGY = False

# Legacy SCALP(11~30) and BTC-only SCALP switch
SCALP_LEGACY_ENABLED = False
SCALP_BTC_ENABLED = True

# ===============================
# Universe / loop
# ===============================
TOP_N = 10
UNIVERSE_SCAN_N = 30
CORE_TOP_N = 10
CORE_MIN_ACTIVE = 3
CORE_STRICT_PREFILTER = False
SURGE_RANK_START = 11
SURGE_RANK_END = 30
SURGE_KEEP_MINUTES = 10
SURGE_STOPLOSS_REENTRY_BLOCK_MIN = 45

SPIKE_CANDIDATE_MAX = 3
SPIKE_RANK_SURGE_MIN = 5
SPIKE_VOL_LOOKBACK_MIN = 5
SPIKE_VOL_MULT = 1.2
SPIKE_RSI_CROSS_LEVEL = 50
SPIKE_MA_FAST = 5
SPIKE_MA_SLOW = 20

REFRESH_MIN = 5
POLL_SEC = 1
MIN_ORDER_KRW = 5_000

NO_DUPLICATE_TICKER_ACROSS_STRATEGIES = True
ALLOW_ADD_BUY = False
EXCLUDE_CAUTION = True

DEBUG_STOP_PCT = False
DEBUG_TRADE_FLOW = True
DEBUG_ENTRY_REJECT = False

# ===============================
# Costs
# ===============================
# Buy 0.05% + Sell 0.05% + slippage buffer
COST_ROUNDTRIP_PCT = 0.0015

# ===============================
# Market regime
# ===============================
USE_MARKET_REGIME = False

REGIME_INVEST_FRAC = {
    "HALT": 0.0,
    "LOW": 0.6,
    "MID": 0.6,
    "FULL": 0.6,
}
REGIME_HOLDINGS_MULT = {
    "HALT": 1.0,
    "LOW": 1.0,
    "MID": 1.0,
    "FULL": 1.0,
}

BTC_REGIME_FAST_MA = 5
BTC_REGIME_SLOW_MA = 20
BTC_REGIME_RSI_PERIOD = 14

# ===============================
# Account sizing
# ===============================
TEST_EQUITY_CAP = 200_000
TEST_PER_TRADE_KRW = 30_000
MINUTE_TEST_PER_TRADE_KRW = 30_000
TEST_MAX_HOLDINGS = 2

# Keep 2 holdings until this equity.
HOLDINGS_FIXED_UNTIL_EQUITY = 1_500_000

# Tier-based expansion above HOLDINGS_FIXED_UNTIL_EQUITY
ACCOUNT_TIERS = [
    {"min_equity": 1_500_001, "max_holdings": 3},
    {"min_equity": 2_000_001, "max_holdings": 4},
    {"min_equity": 3_000_001, "max_holdings": 5},
]

HOLDING_SCALE = {
    2: 1.00,
    3: 0.90,
    4: 0.80,
    5: 0.75,
}

# ===============================
# Risk / exits
# ===============================
STOP_LOSS_PCT = 0.010
STOP_LOSS_MODE = "ATR"  # FIXED / ATR
STOP_LOSS_ATR_PERIOD = 14
STOP_LOSS_ATR_MULT_TABLE = {"LOW": 1.1, "MID": 1.3, "FULL": 1.5, "HALT": 1.0}
STOP_LOSS_MIN_PCT = 0.010
STOP_LOSS_MAX_PCT = 0.025

TP_TABLE = {
    "LOW": {"TP1_PCT": 0.005, "TP2_PCT": 0.010, "TRAIL_BACK_PCT": 0.004},
    "MID": {"TP1_PCT": 0.008, "TP2_PCT": 0.016, "TRAIL_BACK_PCT": 0.005},
    "FULL": {"TP1_PCT": 0.012, "TP2_PCT": 0.025, "TRAIL_BACK_PCT": 0.007},
    "HALT": {"TP1_PCT": 0.0, "TP2_PCT": 0.0, "TRAIL_BACK_PCT": 0.0},
}
TP1_SELL_RATIO = 0.50
TP2_SELL_RATIO = 0.50
TRAIL_BACK_PCT = 0.0070

# Trailing arm guard (percent-based fields, not ratio):
# - TRAIL_ARM_PCT: minimum unrealized profit (%) to enable trailing
# - TRAIL_DRAWDOWN_PCT: optional trailing drawdown (%) override; None = use regime TRAIL_BACK_PCT
TRAIL_ARM_SEC = 120
TRAIL_ARM_PCT = 0.5
TRAIL_DRAWDOWN_PCT = None

TP_SL_BY_HOLDINGS = {
    2: {
        "tp1": 0.008,
        "tp2": 0.014,
        "tp1_ratio": 0.50,
        "tp2_ratio": 0.30,
        "trail_back": 0.0070,
        "stop_fixed": 0.016,
        "daily_tp1_stop": 3,
        "consec_loss_stop": 4,
    },
    3: {
        "tp1": 0.007,
        "tp2": 0.012,
        "tp1_ratio": 0.55,
        "tp2_ratio": 0.30,
        "trail_back": 0.0075,
        "stop_fixed": 0.014,
        "daily_tp1_stop": 4,
        "consec_loss_stop": 5,
    },
    4: {
        "tp1": 0.006,
        "tp2": 0.010,
        "tp1_ratio": 0.60,
        "tp2_ratio": 0.25,
        "trail_back": 0.0080,
        "stop_fixed": 0.012,
        "daily_tp1_stop": 5,
        "consec_loss_stop": 6,
    },
    5: {
        "tp1": 0.0055,
        "tp2": 0.009,
        "tp1_ratio": 0.65,
        "tp2_ratio": 0.20,
        "trail_back": 0.0085,
        "stop_fixed": 0.011,
        "daily_tp1_stop": 6,
        "consec_loss_stop": 7,
    },
}

DAILY_TP1_STOP_COUNT = TP_SL_BY_HOLDINGS[2]["daily_tp1_stop"]
CONSEC_LOSS_STOP_COUNT = TP_SL_BY_HOLDINGS[2]["consec_loss_stop"]
DAILY_TP1_EXIT_LIMIT = 3
CONSEC_LOSS_EXIT_LIMIT = 4
MAIN_CONSEC_LOSS_LIMIT = 4
SCALP_CONSEC_LOSS_LIMIT = 4

COOLDOWN_PROFIT_MIN = 10
COOLDOWN_LOSS_MIN = 30

# Global risk-cut (account-level circuit breaker)
# negative percent values: -5 means -5%
DAILY_MAX_LOSS_PCT = -5.0
GLOBAL_MDD_LIMIT_PCT = -15.0
RISK_CUT_FORCE_CLOSE = False

# ===============================
# MAIN strategy
# ===============================
K_DEFAULT = 0.5
AUTO_K = True
K_LOOKBACK_DAYS = 30
K_CANDIDATES = [
    0.10,
    0.15,
    0.20,
    0.25,
    0.30,
    0.35,
    0.40,
    0.45,
    0.50,
    0.55,
    0.60,
    0.65,
    0.70,
    0.75,
    0.80,
    0.85,
    0.90,
    0.95,
]

USE_INTRADAY_FILTER = False
INTRADAY_TREND_INTERVAL = "minute240"
INTRADAY_FAST_MA = 20
INTRADAY_SLOW_MA = 60

ENTRY_MINUTE_INTERVAL = "minute5"
ENTRY_FAST_MA = 5
ENTRY_SLOW_MA = 20
ENTRY_RSI_PERIOD = 14
ENTRY_RSI_MAX = 70
ENTRY_ENABLE_TREND_RSI_RELAX = True
ENTRY_RSI_MAX_STRONG = 74
ENTRY_MA_SLOPE_BARS = 1
ENTRY_NEAR_HIGH_BLOCK_PCT = 0.002
ENTRY_PULLBACK_MIN_PCT = 0.003
ENTRY_PULLBACK_MAX_PCT = 0.010
ENTRY_PULLBACK_LOOKBACK = 20
ENTRY_PULLBACK_WINDOW = 8
ENTRY_REQUIRE_REBOUND = True
ENTRY_REQUIRE_VOL_HOLD = True
ENTRY_MINUTE_REJECT_SUMMARY_MIN = 10
ENTRY_MINUTE_REJECT_SUMMARY_TOPN = 6
ENTRY_RSI_CROSS_LEVEL = 45
ENTRY_RSI_DELTA_MIN = 1.5
ENTRY_MA_FAST = 5
ENTRY_MA_SLOW = 20
ENTRY_USE_VOLUME_FILTER = True
ENTRY_VOL_MA_PERIOD = 20
ENTRY_VOL_MULT = 1.2
ENTRY_USE_BREAKOUT = True
ENTRY_BREAKOUT_LOOKBACK = 20
ENTRY_REQUIRE_RSI_UPTURN = False
MAIN_FILTER_REJECT_SUMMARY_MIN = 10
MAIN_FILTER_REJECT_SUMMARY_TOPN = 6

# ===============================
# SCALP strategy
# ===============================
USE_MINUTE_TEST_STRATEGY = True
MINUTE_TEST_INTERVAL = "minute1"

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
MINUTE_TEST_USE_BREAKOUT = True
MINUTE_TEST_BREAKOUT_LOOKBACK = 20

# SCALP stabilization extensions
SCALP_CONFIRM_BARS = 2
SCALP_BREAKOUT_LOOKBACK = 20
SCALP_RSI_MIN = 52.0
SCALP_RSI_DELTA_MIN = 0.6
SCALP_MA_FAST = 5
SCALP_MA_SLOW = 20
SCALP_VOL_MA_PERIOD = 20
SCALP_VOL_MULT = 1.4
SCALP_CONSERVATIVE_RSI_MIN = 52.0
SCALP_CONSERVATIVE_VOL_MULT = 1.5
SCALP_RSI_MAX = 72.0
SCALP_BREAKOUT_MAX_GAP_PCT = 0.008
SCALP_MAX_CANDLE_BODY_PCT = 0.012
SCALP_DAWN_START_HOUR = 0
SCALP_DAWN_END_HOUR = 7
SCALP_DAWN_CONSERVATIVE = True
SCALP_DAWN_BLOCK = True
SCALP_LOSSSEQ_CONSERVATIVE_TRIGGER = 1
SCALP_PAUSE_ON_LOSSSEQ = True
SCALP_PAUSE_LOSSSEQ_TRIGGER = 2
SCALP_PAUSE_MINUTES = 60

# ===============================
# SCALP_BTC strategy (KRW-BTC only)
# ===============================
SCALP_BTC_TICKER = "KRW-BTC"
SCALP_BTC_TF = "minute15"

SCALP_BTC_MAX_SHARE = 0.20
SCALP_BTC_PER_TRADE_SHARE = 0.10
SCALP_BTC_MAX_POSITIONS = 1
SCALP_BTC_BLOCK_WHEN_MAIN_HOLDING = False

SCALP_BTC_RSI_LEN = 14
SCALP_BTC_RSI_OS = 28
SCALP_BTC_VOL_LOOKBACK = 20
SCALP_BTC_VOL_SPIKE_MULT = 1.5
SCALP_BTC_VOL_SPIKE_WINDOW = 3
SCALP_BTC_EMA_FAST = 9

SCALP_BTC_TP_PCT = 0.012
SCALP_BTC_SL_PCT = 0.009
SCALP_BTC_MAX_HOLD_MIN = 90

SCALP_BTC_TRAIL_ON = True
SCALP_BTC_TRAIL_FROM = 0.010
SCALP_BTC_TRAIL_GIVEBACK = 0.006

SCALP_BTC_COOLDOWN_PROFIT_MIN = 10
SCALP_BTC_COOLDOWN_LOSS_MIN = 30
SCALP_BTC_MAX_LOSS_STREAK = 2
SCALP_BTC_PAUSE_MIN_AFTER_STREAK = 60

SCALP_BTC_SWITCH_FAIL_LIMIT = 3
SCALP_BTC_SWITCH_FAIL_PAUSE_MIN = 60
SCALP_BTC_LOCK_TIMEOUT_SEC = 5
SCALP_BTC_MIN_ORDER_BUFFER = 1.02

# ===============================
# State / logging
# ===============================
STATE_FILE = "bot_state.json"
STATE_SAVE_INTERVAL_SEC = 30

TRADE_LOG_PATH = "trade_log.csv"
STATUS_PRINT_SEC = 60

# ===============================
# Reporting
# ===============================
AUTO_REPORT = True
AUTO_REPORT_MIN_INTERVAL_SEC = 30
AUTO_REPORT_QUIET = True
INITIAL_CAPITAL = 1_000_000

# ===============================
# Position management
# ===============================
POSITION_TARGET_MULT = 2.0
POSITION_MAX_BUY_COUNT = 2
DUST_CLOSE_AS_CLOSED = True

# ===============================
# Filter caches
# ===============================
DAY_FILTER_CACHE_SEC = 60
INTRADAY_FILTER_CACHE_SEC = 30
MINUTE_ENTRY_CACHE_SEC = 10

# ===============================
# Order retry / order log
# ===============================
ORDER_RETRY_MAX = 3
ORDER_RETRY_SLEEP_SEC = 0.35
ORDER_LOG_PATH = "order_log.csv"

