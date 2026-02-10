"""Main bot loop coordinating universe refresh, entries/exits, and state persistence."""

import copy
import calendar
import csv
import datetime as dt
import os
import atexit
import sys
import time
import traceback
from collections import Counter
from zoneinfo import ZoneInfo

import pyupbit

import config
import position_manager
from engine_entry import try_main_entries, try_scalp_entries
from engine_manage import append_trade_log, log_order, manage_positions
from indicators import (
    check_filters_with_reason,
    detect_momentum_candidate,
    get_market_regime,
    get_rsi,
    intraday_trend_ok,
    scalp_btc_entry_signal,
)
from market import (
    filter_tradeable_tickers,
    get_balance,
    get_top_tickers_by_value,
    get_upbit_krw_markets,
    load_keys,
    sanitize_positions,
)
from order_utils import wait_for_filled_snapshot
from state_store import STRATEGIES, load_state, save_state, verify_state_with_balance
from strategy import build_k_map
from utils.log_paths import trade_log_path_for
from utils.telegram_notify import (
    flush_telegram_spool,
    has_telegram_credentials,
    load_telegram_env_file,
    notify_event,
    notify_order,
)


BASE_TP_TABLE = copy.deepcopy(getattr(config, "TP_TABLE", {}))
BASE_STOP_LOSS_PCT = float(getattr(config, "STOP_LOSS_PCT", 0.01))
_TG_LAST_ERR_AT = 0.0
_TG_LAST_ERR_KEY = ""
_TG_LAST_RISKCUT_AT = 0.0
_TG_ENV_WARNED = False
_INSTANCE_LOCK_FH = None
KST = ZoneInfo("Asia/Seoul")

AUTO_PARAM_SETS = {
    "CONSERVATIVE": {
        "SCALP_BTC": {
            "sl_one": -0.0035,
            "tp_one": 0.0055,
            "trail_from": 0.0040,
            "trail_giveback": 0.0035,
            "timeout_profit_min": 0.0015,
        },
        "MAIN": {
            "sl_one": -0.009,
            "tp_one": 0.0,
            "trail_from": 0.006,
            "trail_giveback": 0.006,
        },
    },
    "AGGRESSIVE": {
        "SCALP_BTC": {
            "sl_one": -0.006,
            "tp_one": 0.009,
            "trail_from": 0.006,
            "trail_giveback": 0.006,
            "timeout_profit_min": 0.0025,
        },
        "MAIN": {
            "sl_one": -0.012,
            "tp_one": 0.0,
            "trail_from": 0.008,
            "trail_giveback": 0.008,
        },
    },
}


def now_kst():
    return dt.datetime.now(KST)


def _get_auto_params(mode: str):
    m = str(mode or "CONSERVATIVE").upper().strip()
    return AUTO_PARAM_SETS.get(m, AUTO_PARAM_SETS["CONSERVATIVE"])


def _avg_rsi_for_tickers(tickers, interval: str, rsi_period: int = 14):
    vals = []
    for t in list(tickers or []):
        try:
            df = pyupbit.get_ohlcv(t, interval=interval, count=max(30, int(rsi_period) + 5))
        except Exception:
            df = None
        if df is None or len(df) < (int(rsi_period) + 2):
            continue
        try:
            rsi_series = get_rsi(df, int(rsi_period))
            rsi_now = float(rsi_series.iloc[-1])
        except Exception:
            continue
        if rsi_now != rsi_now:
            continue
        vals.append(float(rsi_now))
    if not vals:
        return None
    return float(sum(vals) / len(vals))


def _calc_auto_strategy_mode(market_info=None):
    btc_4h = pyupbit.get_ohlcv("KRW-BTC", interval="minute240", count=80)
    if btc_4h is None or len(btc_4h) < 60:
        return "CONSERVATIVE", "btc_4h_data_missing"

    ma20_4h = btc_4h["close"].rolling(20).mean().iloc[-1]
    ma60_4h = btc_4h["close"].rolling(60).mean().iloc[-1]
    cond_4h = bool(float(ma20_4h) > float(ma60_4h))

    btc_day = pyupbit.get_ohlcv("KRW-BTC", interval="day", count=40)
    if btc_day is None or len(btc_day) < 20:
        return "CONSERVATIVE", "btc_day_data_missing"
    day_ma20 = btc_day["close"].rolling(20).mean().iloc[-1]
    day_close = float(btc_day["close"].iloc[-1])
    cond_day = bool(day_close > float(day_ma20))

    topn = max(1, int(getattr(config, "AUTO_STRATEGY_TOPN", 10)))
    interval = str(getattr(config, "AUTO_STRATEGY_RSI_INTERVAL", "day"))
    top_tickers = get_top_tickers_by_value(topn, market_info=market_info)
    rsi_avg = _avg_rsi_for_tickers(top_tickers, interval=interval, rsi_period=14)
    cond_rsi = bool(rsi_avg is not None and float(rsi_avg) > 55.0)

    if cond_4h and cond_day and cond_rsi:
        return "AGGRESSIVE", f"bull_ok rsi_avg={rsi_avg:.2f}"
    return "CONSERVATIVE", f"bull_fail rsi_avg={rsi_avg:.2f}" if rsi_avg is not None else "bull_fail rsi_avg=None"


def _release_instance_lock():
    global _INSTANCE_LOCK_FH
    fh = _INSTANCE_LOCK_FH
    _INSTANCE_LOCK_FH = None
    if fh is None:
        return
    try:
        if os.name == "posix":
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        elif os.name == "nt":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    except Exception:
        pass
    try:
        fh.close()
    except Exception:
        pass


def _acquire_instance_lock() -> bool:
    global _INSTANCE_LOCK_FH
    if _INSTANCE_LOCK_FH is not None:
        return True

    lock_path = str(getattr(config, "INSTANCE_LOCK_PATH", ".upbit_bot.instance.lock") or ".upbit_bot.instance.lock")
    try:
        fh = open(lock_path, "a+", encoding="utf-8")
    except Exception as e:
        print(f"[LOCK] unable to open instance lock file: {e}")
        return True

    try:
        if os.name == "posix":
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        elif os.name == "nt":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    except Exception:
        try:
            fh.close()
        except Exception:
            pass
        print("[LOCK] another bot instance is already running. exit this process.")
        return False

    try:
        fh.seek(0)
        fh.truncate(0)
        fh.write(f"pid={os.getpid()} started={now_kst().isoformat()}\n")
        fh.flush()
    except Exception:
        pass

    _INSTANCE_LOCK_FH = fh
    return True


def _warn_missing_requests_dependency():
    try:
        import requests  # noqa: F401
    except Exception:
        print("[WARN] requests not installed: pip install requests")


def _warn_missing_telegram_env_once():
    global _TG_ENV_WARNED
    if _TG_ENV_WARNED:
        return
    _TG_ENV_WARNED = True
    load_telegram_env_file("/etc/default/telegram-bot")
    if not has_telegram_credentials():
        print("[WARN] TELEGRAM_TOKEN/TELEGRAM_CHAT_ID missing at startup; realtime telegram alerts may be queued")


def _notify_loop_error_once(exc: Exception):
    global _TG_LAST_ERR_AT, _TG_LAST_ERR_KEY

    cooldown = max(0, int(60))
    err_key = str(type(exc).__name__)
    now_ts = time.time()

    if err_key == _TG_LAST_ERR_KEY and (now_ts - _TG_LAST_ERR_AT) < float(cooldown):
        return

    print(f"[ALERT] ERROR: {type(exc).__name__}: {exc}")
    notify_event(
        event_type="EXCEPTION_RAISED",
        lines=[
            f"예외: {type(exc).__name__}",
            f"메시지: {exc}",
        ],
    )
    _TG_LAST_ERR_AT = float(now_ts)
    _TG_LAST_ERR_KEY = err_key


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _normalize_loss_limit_pct(value, fallback: float) -> float:
    limit = _safe_float(value, fallback)
    if limit > 0:
        limit = -abs(limit)
    return float(limit)


def _default_runtime_risk_state():
    return {
        "peak_equity": 0.0,
        "day_start_equity": 0.0,
        "day_key": "",
        "halted_flag": False,
        "halt_reason": "",
        "halted_at_ts": 0.0,
    }


def _normalize_runtime_risk_state(raw: dict):
    s = dict(_default_runtime_risk_state())
    if isinstance(raw, dict):
        s.update(raw)
    s["peak_equity"] = max(0.0, _safe_float(s.get("peak_equity", 0.0), 0.0))
    s["day_start_equity"] = max(0.0, _safe_float(s.get("day_start_equity", 0.0), 0.0))
    s["day_key"] = str(s.get("day_key") or "")
    s["halted_flag"] = bool(s.get("halted_flag", False))
    s["halt_reason"] = str(s.get("halt_reason") or "")
    s["halted_at_ts"] = max(0.0, _safe_float(s.get("halted_at_ts", 0.0), 0.0))
    return s


def _pct_change(cur: float, base: float) -> float:
    if base <= 0:
        return 0.0
    return (float(cur) / float(base) - 1.0) * 100.0


def _update_global_risk_cut_state(now: dt.datetime, equity: float, risk_state: dict, persist_state_fn=None):
    changed = False
    triggered = False
    s = risk_state

    cur_eq = max(0.0, float(equity))
    day_key = now.date().isoformat()
    if str(s.get("day_key") or "") != day_key:
        s["day_key"] = day_key
        s["day_start_equity"] = float(cur_eq)
        changed = True
        print(
            f"[RISK] day rollover | day_key={day_key} day_start_equity={float(s['day_start_equity']):,.0f}"
        )

    if float(s.get("day_start_equity", 0.0)) <= 0:
        s["day_start_equity"] = float(cur_eq)
        changed = True

    prev_peak = float(s.get("peak_equity", 0.0))
    if prev_peak <= 0:
        s["peak_equity"] = float(cur_eq)
        changed = True
    elif cur_eq > prev_peak:
        s["peak_equity"] = float(cur_eq)
        changed = True

    day_start = float(s.get("day_start_equity", 0.0))
    peak_equity = float(s.get("peak_equity", 0.0))
    daily_loss_pct = _pct_change(cur_eq, day_start)
    mdd_pct = _pct_change(cur_eq, peak_equity)

    daily_limit = _normalize_loss_limit_pct(getattr(config, "DAILY_MAX_LOSS_PCT", -5.0), -5.0)
    mdd_limit = _normalize_loss_limit_pct(getattr(config, "GLOBAL_MDD_LIMIT_PCT", -15.0), -15.0)

    halt_reason = ""
    if daily_loss_pct <= daily_limit:
        halt_reason = "DAILY_LOSS_LIMIT"
    if mdd_pct <= mdd_limit:
        halt_reason = "TOTAL_MDD_LIMIT"

    prev_halted = bool(s.get("halted_flag", False))
    prev_reason = str(s.get("halt_reason") or "")
    if halt_reason:
        s["halted_flag"] = True
        s["halt_reason"] = halt_reason
        if not prev_halted:
            s["halted_at_ts"] = float(now.timestamp())
            triggered = True
            changed = True
            if callable(persist_state_fn):
                try:
                    persist_state_fn()
                except Exception as e:
                    print(f"[WARN] risk state immediate save failed: {e}")
        elif prev_reason != halt_reason:
            changed = True
    else:
        if prev_halted or prev_reason:
            changed = True
        s["halted_flag"] = False
        s["halt_reason"] = ""
        s["halted_at_ts"] = 0.0

    info = {
        "halted": bool(s.get("halted_flag", False)),
        "reason": str(s.get("halt_reason") or ""),
        "daily_loss_pct": float(daily_loss_pct),
        "mdd_pct": float(mdd_pct),
        "daily_limit": float(daily_limit),
        "mdd_limit": float(mdd_limit),
        "day_key": str(day_key),
        "day_start_equity": float(day_start),
        "peak_equity": float(peak_equity),
    }
    return info, changed, triggered


def _notify_risk_cut_once(info: dict, equity: float):
    global _TG_LAST_RISKCUT_AT
    cooldown = max(0, int(60))
    now_ts = time.time()
    if (now_ts - _TG_LAST_RISKCUT_AT) < float(cooldown):
        return

    reason = str(info.get("reason") or "RISK_CUT")
    daily = float(info.get("daily_loss_pct", 0.0))
    mdd = float(info.get("mdd_pct", 0.0))
    print(f"[ALERT] RISK CUT {reason} | equity={float(equity):,.0f} daily={daily:+.2f}% mdd={mdd:+.2f}%")
    event_type = "GLOBAL_MDD_REACHED"
    if reason == "DAILY_LOSS_LIMIT":
        event_type = "DAILY_MAX_LOSS_REACHED"
    notify_event(
        event_type=event_type,
        lines=[
            f"사유: {reason}",
            f"현재자산: {float(equity):,.0f}",
            f"일손익: {daily:+.2f}%",
            f"MDD: {mdd:+.2f}%",
        ],
    )
    _TG_LAST_RISKCUT_AT = float(now_ts)


def _coin_symbol(ticker: str) -> str:
    try:
        return str(ticker).split("-")[1].upper()
    except Exception:
        return ""


def _fmt_qty(qty: float) -> str:
    try:
        txt = f"{float(qty):.8f}".rstrip("0").rstrip(".")
    except Exception:
        txt = "0"
    return txt or "0"


def _is_partial_trade_reason(raw_reason: str) -> bool:
    code = str(raw_reason or "").strip().upper()
    return code in {"TP1", "TP2_PARTIAL"}


def _calc_monthly_stats(now: dt.datetime, trade_log_path: str):
    month_prefix = now.strftime("%Y-%m")
    total = 0
    wins = 0
    pnl_sum = 0.0

    try:
        with open(trade_log_path, "r", newline="", encoding="utf-8") as f:
            for i, row in enumerate(csv.reader(f)):
                if not row:
                    continue
                if i == 0 and str(row[0]).strip().lower() == "time":
                    continue
                if len(row) < 5:
                    continue

                ts = str(row[0]).strip()
                if not ts.startswith(month_prefix):
                    continue
                reason = str(row[5]).strip() if len(row) > 5 else ""
                if _is_partial_trade_reason(reason):
                    continue

                try:
                    pnl_pct = float(row[4])
                except Exception:
                    continue

                total += 1
                if pnl_pct > 0:
                    wins += 1
                pnl_sum += pnl_pct
    except Exception:
        return None

    if total <= 0:
        return None

    return {
        "total": int(total),
        "wins": int(wins),
        "win_rate_pct": (float(wins) / float(total)) * 100.0,
        "avg_pnl_pct": float(pnl_sum) / float(total),
    }


def _normalize_order_reason(raw_reason: str) -> str:
    s_raw = str(raw_reason or "").strip()
    s = s_raw.lower()
    s_up = s_raw.upper()
    if s_up in {"ENTRY", "TP1", "TP2", "TP2_PARTIAL", "RUNNER_TRAIL", "RUNNER_TIMEOUT", "TRAILING", "SL", "FORCE_CLOSE"}:
        return s_up
    if "tp2_partial" in s:
        return "TP2_PARTIAL"
    if "runner_trail" in s:
        return "RUNNER_TRAIL"
    if "runner_timeout" in s:
        return "RUNNER_TIMEOUT"
    if "tp1" in s:
        return "TP1"
    if "tp2" in s or "take_profit" in s:
        return "TP2"
    if "trail" in s:
        return "TRAILING"
    if "stop" in s or "sl" in s:
        return "SL"
    if "switch" in s or "timeout" in s or "force" in s or "dust" in s:
        return "FORCE_CLOSE"
    return "ENTRY"


def _normalize_exit_reason(raw_reason: str) -> str:
    s_raw = str(raw_reason or "").strip()
    s = s_raw.lower()
    s_up = s_raw.upper()
    if s_up in {"TP2", "RUNNER_TRAIL", "RUNNER_TIMEOUT", "TRAILING", "STOPLOSS", "FORCE_CLOSE"}:
        return s_up
    if "runner_trail" in s:
        return "RUNNER_TRAIL"
    if "runner_timeout" in s:
        return "RUNNER_TIMEOUT"
    if "tp2" in s or "take_profit" in s:
        return "TP2"
    if "trail" in s:
        return "TRAILING"
    if "stop" in s or "sl" in s:
        return "STOPLOSS"
    if "switch" in s or "timeout" in s or "force" in s or "dust" in s:
        return "FORCE_CLOSE"
    return "FORCE_CLOSE"


def _close_event_type(raw_reason: str) -> str:
    exit_reason = _normalize_exit_reason(raw_reason)
    if exit_reason == "TP2":
        return "TP2_HIT"
    if exit_reason == "RUNNER_TRAIL":
        return "TRAILING_EXIT"
    if exit_reason == "RUNNER_TIMEOUT":
        return "FORCE_CLOSE"
    if exit_reason == "TRAILING":
        return "TRAILING_EXIT"
    if exit_reason == "STOPLOSS":
        return "STOPLOSS_HIT"
    if exit_reason == "FORCE_CLOSE":
        return "FORCE_CLOSE"
    return ""


def _fmt_krw(value: float) -> str:
    try:
        return f"{float(value):,.0f}"
    except Exception:
        return "0"


def _notify_close_settlement(
    strategy_tag: str,
    ticker: str,
    total_buy_krw: float,
    total_sell_krw: float,
    last_exit_reason: str,
):
    buy_krw = max(0.0, _safe_float(total_buy_krw, 0.0))
    sell_krw = max(0.0, _safe_float(total_sell_krw, 0.0))
    realized_krw = float(sell_krw) - float(buy_krw)
    realized_pct = (float(realized_krw) / float(buy_krw) * 100.0) if buy_krw > 0 else 0.0
    pnl_label = "\uC218\uC775\uAE08" if realized_krw >= 0 else "\uC190\uC2E4\uAE08"
    event_type = _close_event_type(last_exit_reason)

    notify_event(
        event_type=event_type,
        lines=[
            f"\uC804\uB7B5: {str(strategy_tag or 'MAIN').upper().strip() or 'MAIN'}",
            f"\uC885\uBAA9: {ticker}",
            f"\uCD1D\uB9E4\uC218: {_fmt_krw(buy_krw)} KRW",
            f"\uCD1D\uB9E4\uB3C4: {_fmt_krw(sell_krw)} KRW",
            f"{pnl_label}: {_fmt_krw(abs(realized_krw))} KRW",
            f"\uC218\uC775\uB960: {realized_pct:+.2f}%",
            f"\uCD5C\uC885 \uC0AC\uC720: {_normalize_exit_reason(last_exit_reason)}",
        ],
    )
    return float(realized_krw), float(realized_pct)


def _notify_trade_result(ticker: str, qty: float, entry_price: float, exit_price: float, pnl_pct: float):
    symbol = _coin_symbol(ticker)
    qty_txt = _fmt_qty(qty)
    icon = "+" if float(pnl_pct) >= 0 else "-"
    msg = (
        f"{icon} trade closed: {ticker} {qty_txt}{symbol}\n"
        f"price: {float(entry_price):,.6f} -> {float(exit_price):,.6f}\n"
        f"pnl: {float(pnl_pct):+.2f}%"
    )
    print(msg)


def _notify_monthly_stats(now: dt.datetime):
    stats = _calc_monthly_stats(now, trade_log_path_for(now))
    if not stats:
        return

    last_day = calendar.monthrange(int(now.year), int(now.month))[1]
    period = f"{int(now.month)}/1-{int(last_day)}"
    print(
        f"[MONTHLY] ({period}) win_rate={float(stats['win_rate_pct']):.1f}% "
        f"avg_pnl={float(stats['avg_pnl_pct']):+.2f}% total={int(stats['total'])}"
    )


def _notify_closed_trade_events(events):
    for e in events or []:
        ticker = str(e.get("ticker", "") or "")
        if not ticker:
            continue
        qty = _safe_float(e.get("qty", 0.0), 0.0)
        entry_price = _safe_float(e.get("entry_price", 0.0), 0.0)
        exit_price = _safe_float(e.get("exit_price", 0.0), 0.0)
        strategy_tag = str(e.get("strategy_tag", e.get("strategy", "MAIN")) or "MAIN").upper().strip()
        raw_reason = str(e.get("last_exit_reason", e.get("reason", "")) or "")
        reason_code = _normalize_order_reason(raw_reason)
        exit_reason = _normalize_exit_reason(raw_reason)
        total_buy_krw = max(0.0, _safe_float(e.get("total_buy_krw", 0.0), 0.0))
        total_sell_krw = max(0.0, _safe_float(e.get("total_sell_krw", 0.0), 0.0))
        if total_buy_krw <= 0 and entry_price > 0 and qty > 0:
            total_buy_krw = float(entry_price) * float(qty)
        if total_sell_krw <= 0 and exit_price > 0 and qty > 0:
            total_sell_krw = float(exit_price) * float(qty)
        close_time = e.get("time")
        if not isinstance(close_time, dt.datetime):
            close_time = now_kst()

        if "dust" not in str(e.get("reason", "") or "").lower():
            notify_order(
                event_type="ORDER_SELL_FILLED",
                strategy_tag=strategy_tag,
                ticker=ticker,
                price=float(exit_price),
                qty=float(qty),
                reason=reason_code,
            )
        _, pnl_pct = _notify_close_settlement(
            strategy_tag=strategy_tag,
            ticker=ticker,
            total_buy_krw=float(total_buy_krw),
            total_sell_krw=float(total_sell_krw),
            last_exit_reason=exit_reason,
        )

        _notify_trade_result(
            ticker=ticker,
            qty=qty,
            entry_price=entry_price,
            exit_price=exit_price,
            pnl_pct=pnl_pct,
        )
        _notify_monthly_stats(close_time)


def ensure_trade_log_header(path: str):
    expected = ["time", "ticker", "entry_price", "exit_price", "pnl_pct", "reason", "regime", "strategy"]

    if not os.path.exists(path):
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(expected)
        return

    try:
        with open(path, "r", newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
    except Exception:
        return

    if not rows:
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(expected)
        return

    header = rows[0]
    if header == expected:
        return

    legacy_7 = expected[:-1]
    if header == legacy_7:
        migrated = [expected]
        for row in rows[1:]:
            fixed = row[: len(legacy_7)]
            while len(fixed) < len(legacy_7):
                fixed.append("")
            fixed.append("")
            migrated.append(fixed)
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(migrated)
        print(f"[MIGRATE] {path} header updated: added 'strategy' column")
        return

    # Unknown header, do not rewrite automatically.
    print(f"[WARN] unexpected trade log header: {header}")


def batch_get_prices(tickers):
    try:
        data = pyupbit.get_current_price(list(tickers))
        if data is None:
            return {}
        if isinstance(data, dict):
            return {k: float(v) for k, v in data.items() if v is not None}
    except Exception:
        pass
    return {}


def _iter_all_states(strategy_state: dict, inactive_positions: dict = None):
    for strat in STRATEGIES:
        for ticker, s in (strategy_state.get(strat, {}) or {}).items():
            yield strat, ticker, s
    for ticker, s in (inactive_positions or {}).items():
        yield "INACTIVE", ticker, s


def estimate_equity(krw: float, strategy_state: dict, prices: dict, upbit, inactive_positions: dict = None) -> float:
    equity = float(krw)
    for _, ticker, s in _iter_all_states(strategy_state, inactive_positions):
        if not s.get("holding"):
            continue
        coin = ticker.split("-")[1]
        vol = float(get_balance(upbit, coin))
        if vol <= 0:
            continue
        p = prices.get(ticker)
        if p is None:
            continue
        equity += vol * float(p)
    return float(equity)


def print_filter_summary(active, inactive, reasons):
    print(f"[FILTER] active tickers: {len(active)} / inactive tickers: {len(inactive)}")
    if not inactive:
        print("[FILTER] top inactive reasons: none")
        return
    c = Counter([reasons.get(t, "UNKNOWN") for t in inactive])
    top = ", ".join([f"{k}:{v}" for k, v in c.most_common(5)])
    print(f"[FILTER] top inactive reasons: {top}")


def print_main_filter_reject_summary(stats: Counter):
    if not stats:
        return
    topn = max(1, int(getattr(config, "MAIN_FILTER_REJECT_SUMMARY_TOPN", 6)))
    total = sum(int(v) for v in stats.values())
    top = ", ".join([f"{k}:{v}" for k, v in stats.most_common(topn)])
    print(f"[MAIN_FILTER_SUMMARY] total={total} | {top}")


def _dedupe_keep_order(items):
    out = []
    seen = set()
    for x in items:
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out


def _count_strategy_holdings(state: dict) -> int:
    return sum(1 for _, s in (state or {}).items() if s.get("holding", False))


def _count_total_holdings(strategy_state: dict) -> int:
    return sum(_count_strategy_holdings(strategy_state.get(s, {})) for s in STRATEGIES)


def _all_holding_tickers(strategy_state: dict):
    out = set()
    for s in STRATEGIES:
        for ticker, st in (strategy_state.get(s, {}) or {}).items():
            if st.get("holding", False):
                out.add(ticker)
    return out


def _repair_cross_strategy_duplicate_holdings(strategy_state: dict):
    main_state = strategy_state.get("MAIN", {}) or {}
    scalp_state = strategy_state.get("SCALP", {}) or {}
    repaired = 0

    overlap = set(main_state.keys()) & set(scalp_state.keys())
    for ticker in overlap:
        main_h = bool(main_state.get(ticker, {}).get("holding", False))
        scalp_h = bool(scalp_state.get(ticker, {}).get("holding", False))
        if not (main_h and scalp_h):
            continue

        # Keep MAIN, release SCALP.
        s = scalp_state[ticker]
        s["holding"] = False
        s["add_count"] = 0
        s["invested_krw"] = 0.0
        s["target_krw"] = 0.0
        s["initial_volume"] = 0.0
        s["realized_krw"] = 0.0
        s["realized_cost_krw"] = 0.0
        s["tp1"] = False
        s["tp2"] = False
        s["tp1_done"] = False
        s["tp2_done"] = False
        s["runner_active"] = False
        s["runner_hwm"] = 0.0
        s["runner_start_ts"] = 0.0
        s["tp1_ratio"] = 0.0
        s["tp2_ratio"] = 0.0
        s["runner_ratio"] = 0.0
        s["entry_mode"] = ""
        s["entry_ts"] = 0.0
        s["trail_armed"] = False
        s["trail_hwm"] = 0.0
        repaired += 1
        print(f"[LOCK_REPAIR] duplicated holding released from SCALP: {ticker}")

    return repaired


def _core_and_surge_from_ranked(raw_ranked, market_info):
    ranked = _dedupe_keep_order(raw_ranked or [])
    core_n = int(getattr(config, "CORE_TOP_N", getattr(config, "TOP_N", 10)))
    surge_start = int(getattr(config, "SURGE_RANK_START", 11))
    surge_end = int(getattr(config, "SURGE_RANK_END", 30))

    scan_n = int(getattr(config, "UNIVERSE_SCAN_N", max(core_n, surge_end)))
    scan_n = max(scan_n, core_n, surge_end)
    ranked = ranked[:scan_n]

    core_raw = ranked[:core_n]
    if surge_end >= surge_start:
        surge_raw = ranked[max(0, surge_start - 1) : surge_end]
    else:
        surge_raw = []

    core_active, core_inactive, core_reasons = filter_tradeable_tickers(core_raw, market_info)
    surge_active, surge_inactive, surge_reasons = filter_tradeable_tickers(surge_raw, market_info)

    # CORE keeps higher-timeframe trend.
    core_filter_rejects = Counter()
    for ticker in core_inactive:
        reason = str(core_reasons.get(ticker, "UNIVERSE_FILTER"))
        core_filter_rejects[f"UNIVERSE_{reason}"] += 1

    core_filtered = []
    for ticker in core_active:
        try:
            ok_day, day_reason = check_filters_with_reason(ticker)
            if not bool(ok_day):
                core_filter_rejects[str(day_reason or "DAY_FILTER_FAIL")] += 1
                continue
            if bool(getattr(config, "USE_INTRADAY_FILTER", False)) and (not bool(intraday_trend_ok(ticker))):
                core_filter_rejects["INTRADAY_TREND_FAIL"] += 1
                continue
            core_filtered.append(ticker)
        except Exception:
            core_filter_rejects["DAY_FILTER_ERR"] += 1
            continue

    strict_prefilter = bool(getattr(config, "CORE_STRICT_PREFILTER", False))
    core_min_active = max(0, int(getattr(config, "CORE_MIN_ACTIVE", 0)))
    if strict_prefilter:
        core_final = core_filtered
    else:
        core_final = core_filtered if len(core_filtered) >= max(1, core_min_active) else list(core_active)

    inactive_all = _dedupe_keep_order(core_inactive + surge_inactive)
    reasons_all = {}
    reasons_all.update(core_reasons)
    reasons_all.update(surge_reasons)
    return core_final, surge_active, inactive_all, reasons_all, core_filter_rejects


def _update_surge_candidates(surge_pool, momentum_seen_at: dict, now):
    surge_pool = _dedupe_keep_order(surge_pool or [])

    for ticker in surge_pool:
        try:
            if detect_momentum_candidate(ticker):
                momentum_seen_at[ticker] = now
        except Exception:
            continue

    keep_min = int(getattr(config, "SURGE_KEEP_MINUTES", 15))
    cutoff = now - dt.timedelta(minutes=max(1, keep_min))
    for ticker, ts in list(momentum_seen_at.items()):
        if not isinstance(ts, dt.datetime):
            momentum_seen_at.pop(ticker, None)
            continue
        if ts < cutoff:
            momentum_seen_at.pop(ticker, None)

    out = [t for t in surge_pool if t in momentum_seen_at]
    cap = int(getattr(config, "SPIKE_CANDIDATE_MAX", 0))
    if cap > 0:
        out = out[:cap]
    return out, momentum_seen_at


def get_base_position_settings(equity):
    fixed_until = float(getattr(config, "HOLDINGS_FIXED_UNTIL_EQUITY", 1_500_000))
    if float(equity) <= fixed_until:
        return float(config.TEST_PER_TRADE_KRW), 2

    tiers = sorted((getattr(config, "ACCOUNT_TIERS", []) or []), key=lambda x: float(x.get("min_equity", 0)))
    max_holdings = 2
    for t in tiers:
        min_eq = float(t.get("min_equity", 0))
        if equity >= min_eq:
            max_holdings = int(t.get("max_holdings", max_holdings))

    max_holdings = max(2, max_holdings)
    per_trade_amt = float(equity) / float(max_holdings)
    return per_trade_amt, max_holdings


def apply_market_regime(equity, base_per_trade, base_max_holdings, regime: str):
    invest_frac = float(config.REGIME_INVEST_FRAC.get(regime, 0.7))
    holdings_mult = float(config.REGIME_HOLDINGS_MULT.get(regime, 0.7))

    if invest_frac <= 0 or holdings_mult <= 0:
        return 0.0, 0

    eff_max_holdings = max(1, int(base_max_holdings * holdings_mult))
    total_invest_budget = float(equity) * invest_frac
    per_trade_amt = total_invest_budget / float(eff_max_holdings)
    per_trade_amt = max(float(config.MIN_ORDER_KRW), per_trade_amt)
    return float(per_trade_amt), int(eff_max_holdings)


def _pick_holding_scale_key(max_holdings: int):
    table = getattr(config, "HOLDING_SCALE", {}) or {}
    keys = sorted([int(k) for k in table.keys() if str(k).isdigit() or isinstance(k, int)])
    if not keys:
        return 2

    h = int(max_holdings)
    if h <= keys[0]:
        return keys[0]
    if h >= keys[-1]:
        return keys[-1]
    for k in keys:
        if h <= k:
            return k
    return keys[-1]


def apply_runtime_params_by_holdings(max_holdings: int):
    scale_table = getattr(config, "HOLDING_SCALE", {}) or {}
    key = _pick_holding_scale_key(max_holdings)
    scale = float(scale_table.get(key, 1.0))

    base_table = BASE_TP_TABLE or copy.deepcopy(getattr(config, "TP_TABLE", {}))
    scaled_table = {}
    for regime, params in (base_table or {}).items():
        scaled_table[regime] = {
            "TP1_PCT": float(params.get("TP1_PCT", 0.0)) * scale,
            "TP2_PCT": float(params.get("TP2_PCT", 0.0)) * scale,
            "TRAIL_BACK_PCT": float(params.get("TRAIL_BACK_PCT", 0.0)) * scale,
        }

    if scaled_table:
        config.TP_TABLE = scaled_table
    config.STOP_LOSS_PCT = float(BASE_STOP_LOSS_PCT) * scale
    return key, scale


def update_day_tp1_counter(state: dict, counted_tickers: set, day_tp1_count: int, strategy: str):
    for ticker, s in (state or {}).items():
        holding = bool(s.get("holding", False))
        tp1 = bool(s.get("tp1", False))
        if holding and tp1 and ticker not in counted_tickers:
            counted_tickers.add(ticker)
            day_tp1_count += 1
            print(f"[TP1_COUNT][{strategy}] {day_tp1_count}")
        if (not holding) or (not tp1):
            counted_tickers.discard(ticker)
    return int(day_tp1_count)


def update_loss_seq_from_events(events, current: int, strategy: str):
    cur = int(current)
    for e in events or []:
        try:
            pnl = float(e.get("pnl_pct", 0.0))
        except Exception:
            pnl = 0.0
        prev = cur
        if pnl < 0:
            cur += 1
        else:
            cur = 0
        if cur != prev:
            print(f"[LOSS_STREAK][{strategy}] {cur}")
    return int(cur)


def _is_dawn_hour(now: dt.datetime) -> bool:
    h = int(now.hour)
    start = int(getattr(config, "SCALP_DAWN_START_HOUR", 0))
    end = int(getattr(config, "SCALP_DAWN_END_HOUR", 7))
    if start == end:
        return False
    if start < end:
        return start <= h < end
    return h >= start or h < end


def _entry_guard_window(now: dt.datetime):
    if not bool(getattr(config, "ENABLE_0900_ENTRY_GUARD", True)):
        return False, None, None, ""
    if not isinstance(now, dt.datetime):
        now = now_kst()
    if now.tzinfo is None:
        kst_now = now.replace(tzinfo=KST)
    else:
        kst_now = now.astimezone(KST)

    sh = min(23, max(0, int(getattr(config, "ENTRY_GUARD_START_HOUR", 9))))
    sm = min(59, max(0, int(getattr(config, "ENTRY_GUARD_START_MIN", 0))))
    eh = min(23, max(0, int(getattr(config, "ENTRY_GUARD_END_HOUR", 9))))
    em = min(59, max(0, int(getattr(config, "ENTRY_GUARD_END_MIN", 15))))

    start = kst_now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = kst_now.replace(hour=eh, minute=em, second=0, microsecond=0)

    if end <= start:
        # Support across-midnight ranges (e.g. 23:50~00:10).
        if kst_now < end:
            start -= dt.timedelta(days=1)
        else:
            end += dt.timedelta(days=1)

    active = bool(start <= kst_now < end)
    key = f"{start.strftime('%Y%m%d%H%M')}-{end.strftime('%Y%m%d%H%M')}"
    return active, start, end, key


def _resolve_mode() -> str:
    mode = str(getattr(config, "MODE", getattr(config, "BOT_MODE", "MAIN"))).upper().strip()
    if mode not in {"MAIN", "SAFE", "TEST"}:
        mode = "SAFE"
    return mode


def _mode_to_strategy_flags(mode: str):
    """
    MAIN: MAIN + SCALP_BTC
    SAFE: MAIN only
    TEST: MAIN + SCALP_BTC (mock order only)
    """
    legacy_on = bool(getattr(config, "SCALP_LEGACY_ENABLED", False))
    scalp_btc_on = bool(getattr(config, "SCALP_BTC_ENABLED", True))
    if mode == "SAFE":
        return True, False, False, False
    if mode == "TEST":
        return True, False and legacy_on, scalp_btc_on, True
    # MAIN(default)
    return True, legacy_on, scalp_btc_on, False


class _TickerLock:
    def __init__(self):
        self._exp = {}

    def acquire(self, ticker: str, now: dt.datetime, timeout_sec: float) -> bool:
        timeout_sec = max(1.0, float(timeout_sec))
        exp = self._exp.get(ticker)
        if isinstance(exp, dt.datetime) and now < exp:
            return False
        self._exp[ticker] = now + dt.timedelta(seconds=timeout_sec)
        return True

    def release(self, ticker: str):
        self._exp.pop(ticker, None)


def _scalp_btc_is_holding(scalp_btc_state: dict) -> bool:
    return bool((scalp_btc_state or {}).get("holding", False))


def _all_holding_tickers_with_scalp_btc(strategy_state: dict, scalp_btc_state: dict):
    out = _all_holding_tickers(strategy_state)
    if _scalp_btc_is_holding(scalp_btc_state):
        out.add(str(getattr(config, "SCALP_BTC_TICKER", "KRW-BTC")))
    return out


def _count_total_holdings_with_scalp_btc(strategy_state: dict, scalp_btc_state: dict, include_legacy_scalp: bool):
    total = _count_strategy_holdings(strategy_state.get("MAIN", {}))
    if include_legacy_scalp:
        total += _count_strategy_holdings(strategy_state.get("SCALP", {}))
    if _scalp_btc_is_holding(scalp_btc_state):
        total += 1
    return int(total)


def _get_scalp_btc_buy_krw(equity: float, krw: float):
    per_share = float(getattr(config, "SCALP_BTC_PER_TRADE_SHARE", 0.10))
    max_share = float(getattr(config, "SCALP_BTC_MAX_SHARE", 0.20))
    per_krw = float(equity) * per_share
    cap_krw = float(equity) * max_share
    buy_krw = min(per_krw, cap_krw, float(krw))

    required = float(getattr(config, "MIN_ORDER_KRW", 5_000)) * float(getattr(config, "SCALP_BTC_MIN_ORDER_BUFFER", 1.02))
    if buy_krw < required:
        print(f"[SCALP_BTC] skip: size {buy_krw:,.0f} < required_min {required:,.0f} (fee buffer)")
        notify_event(
            event_type="INSUFFICIENT_BALANCE",
            lines=[
                "\uC804\uB7B5: SCALP_BTC",
                f"\uD544\uC694\uAE08\uC561: {required:,.0f}",
                f"\uAC00\uC6A9\uAE08: {float(krw):,.0f}",
            ],
        )
        return 0.0
    return float(buy_krw)


def _scalp_btc_reset_position(state: dict):
    state["holding"] = False
    state["entry_price"] = 0.0
    state["qty"] = 0.0
    state["entry_time"] = None
    state["peak_price"] = 0.0
    state["total_buy_krw"] = 0.0
    state["total_sell_krw"] = 0.0
    state["last_exit_reason"] = ""
    state["strategy_tag"] = "SCALP_BTC"
    state["sl_one_pct"] = None
    state["tp_one_pct"] = None
    state["trail_from_pct"] = None
    state["trail_giveback_pct"] = None
    state["timeout_profit_min"] = None


def _scalp_btc_close_position(
    upbit,
    now: dt.datetime,
    state: dict,
    prices: dict,
    reason: str,
    persist_state_fn,
):
    ticker = str(getattr(config, "SCALP_BTC_TICKER", "KRW-BTC"))
    if not bool(state.get("holding", False)):
        return True, None

    cur = prices.get(ticker)
    if cur is None:
        try:
            cur = float(pyupbit.get_current_price(ticker))
        except Exception:
            cur = None
    if cur is None or float(cur) <= 0:
        return False, "price_unavailable"
    cur = float(cur)

    strategy_tag = str(state.get("strategy_tag") or "SCALP_BTC").upper().strip() or "SCALP_BTC"
    state["strategy_tag"] = strategy_tag
    exit_reason = _normalize_exit_reason(reason)
    order_reason = _normalize_order_reason(exit_reason)

    entry = float(state.get("entry_price", 0.0))
    qty = float(state.get("qty", 0.0))
    if bool(getattr(config, "REAL_ORDER", False)):
        coin = ticker.split("-")[1]
        qty = float(get_balance(upbit, coin))
    if qty <= 0:
        _scalp_btc_reset_position(state)
        persist_state_fn()
        return True, None

    total_buy_krw = max(0.0, _safe_float(state.get("total_buy_krw", 0.0), 0.0))
    if total_buy_krw <= 0 and entry > 0 and qty > 0:
        total_buy_krw = float(entry) * float(qty)
    state["total_buy_krw"] = float(total_buy_krw)

    order_value = qty * cur
    if order_value < float(getattr(config, "MIN_ORDER_KRW", 5_000)):
        if bool(getattr(config, "DUST_CLOSE_AS_CLOSED", True)):
            state["last_exit_reason"] = exit_reason
            total_sell_krw = max(0.0, _safe_float(state.get("total_sell_krw", 0.0), 0.0))
            realized_krw = float(total_sell_krw) - float(total_buy_krw)
            realized_pct = (realized_krw / float(total_buy_krw) * 100.0) if total_buy_krw > 0 else 0.0
            append_trade_log(
                trade_log_path_for(now),
                [
                    now.strftime("%Y-%m-%d %H:%M:%S"),
                    ticker,
                    f"{entry:.6f}",
                    f"{cur:.6f}",
                    f"{realized_pct:.2f}",
                    exit_reason,
                    "SCALP_BTC",
                    "SCALP_BTC",
                ],
            )
            _, notify_pct = _notify_close_settlement(
                strategy_tag=strategy_tag,
                ticker=ticker,
                total_buy_krw=float(total_buy_krw),
                total_sell_krw=float(total_sell_krw),
                last_exit_reason=exit_reason,
            )
            _notify_trade_result(
                ticker=ticker,
                qty=float(qty),
                entry_price=float(entry),
                exit_price=float(cur),
                pnl_pct=float(notify_pct),
            )
            _notify_monthly_stats(now)
            _scalp_btc_reset_position(state)
            persist_state_fn()
            return True, None
        return False, "below_min_order"

    ok = True
    err_msg = ""
    if bool(getattr(config, "REAL_ORDER", False)):
        ok = False
        max_retry = int(getattr(config, "ORDER_RETRY_MAX", 3))
        sleep_sec = float(getattr(config, "ORDER_RETRY_SLEEP_SEC", 0.35))
        for i in range(max_retry):
            try:
                resp = upbit.sell_market_order(ticker, qty)
                log_order("SELL", ticker, qty, True, f"scalp_btc_close try={i+1} resp={str(resp)[:120]}")
                ok = True
                break
            except Exception as e:
                err_msg = str(e)
                log_order("SELL", ticker, qty, False, f"scalp_btc_close try={i+1} err={err_msg}")
                time.sleep(sleep_sec)
    else:
        log_order("SELL", ticker, qty, True, "scalp_btc_mock")

    if not ok:
        print(f"[WARN] ORDER failed: SELL {ticker}")
        notify_order(
            event_type="ORDER_SELL_FAILED",
            strategy_tag=strategy_tag,
            ticker=ticker,
            price=float(cur),
            qty=float(qty),
            reason=order_reason,
        )
        return False, f"sell_failed:{err_msg}"

    state["total_sell_krw"] = max(0.0, _safe_float(state.get("total_sell_krw", 0.0), 0.0)) + (float(qty) * float(cur))
    state["last_exit_reason"] = exit_reason
    total_sell_krw = float(state.get("total_sell_krw", 0.0))
    realized_krw = float(total_sell_krw) - float(total_buy_krw)
    realized_pct = (realized_krw / float(total_buy_krw) * 100.0) if total_buy_krw > 0 else 0.0

    cd_min = int(getattr(config, "SCALP_BTC_COOLDOWN_PROFIT_MIN", 10))
    if realized_krw < 0:
        cd_min = int(getattr(config, "SCALP_BTC_COOLDOWN_LOSS_MIN", 30))
    state["cooldown_until"] = now + dt.timedelta(minutes=max(1, cd_min))

    if realized_krw < 0:
        state["loss_streak"] = int(state.get("loss_streak", 0)) + 1
        print(f"[LOSS_STREAK][SCALP_BTC] {state['loss_streak']}")
        max_streak = int(getattr(config, "SCALP_BTC_MAX_LOSS_STREAK", 2))
        if int(state["loss_streak"]) >= max_streak:
            pause_min = int(getattr(config, "SCALP_BTC_PAUSE_MIN_AFTER_STREAK", 60))
            state["paused_until"] = now + dt.timedelta(minutes=max(1, pause_min))
            state["loss_streak"] = 0
            print(f"[SCALP_BTC_PAUSE] reason=LOSS_STREAK until={state['paused_until'].strftime('%H:%M:%S')}")
    else:
        state["loss_streak"] = 0

    append_trade_log(
        trade_log_path_for(now),
        [
            now.strftime("%Y-%m-%d %H:%M:%S"),
            ticker,
            f"{entry:.6f}",
            f"{cur:.6f}",
            f"{realized_pct:.2f}",
            exit_reason,
            "SCALP_BTC",
            "SCALP_BTC",
        ],
    )
    notify_order(
        event_type="ORDER_SELL_FILLED",
        strategy_tag=strategy_tag,
        ticker=ticker,
        price=float(cur),
        qty=float(qty),
        reason=order_reason,
    )
    _, notify_pct = _notify_close_settlement(
        strategy_tag=strategy_tag,
        ticker=ticker,
        total_buy_krw=float(total_buy_krw),
        total_sell_krw=float(total_sell_krw),
        last_exit_reason=exit_reason,
    )
    _notify_trade_result(
        ticker=ticker,
        qty=float(qty),
        entry_price=float(entry),
        exit_price=float(cur),
        pnl_pct=float(notify_pct),
    )
    _notify_monthly_stats(now)
    print(f"[CLOSE][SCALP_BTC] {ticker} pnl={realized_pct:+.2f}% reason={exit_reason}")
    _scalp_btc_reset_position(state)
    persist_state_fn()
    return True, None


def _manage_scalp_btc_position(upbit, now: dt.datetime, state: dict, prices: dict, persist_state_fn):
    if not bool(state.get("holding", False)):
        return False

    ticker = str(getattr(config, "SCALP_BTC_TICKER", "KRW-BTC"))
    cur = prices.get(ticker)
    if cur is None:
        return False
    cur = float(cur)

    entry = float(state.get("entry_price", 0.0))
    if entry <= 0:
        return False

    state["peak_price"] = max(float(state.get("peak_price", entry)), cur)
    pnl = (cur / entry) - 1.0
    from_peak = (cur / max(float(state.get("peak_price", cur)), 1e-12)) - 1.0

    sl_one = _safe_float(state.get("sl_one_pct"), 0.0)
    if sl_one <= 0:
        sl_one = float(getattr(config, "SCALP_BTC_SL_PCT", 0.009))
    tp_one = _safe_float(state.get("tp_one_pct"), 0.0)
    if tp_one <= 0:
        tp_one = float(getattr(config, "SCALP_BTC_TP_PCT", 0.012))
    trail_from = _safe_float(state.get("trail_from_pct"), 0.0)
    if trail_from <= 0:
        trail_from = float(getattr(config, "SCALP_BTC_TRAIL_FROM", 0.010))
    trail_giveback = _safe_float(state.get("trail_giveback_pct"), 0.0)
    if trail_giveback <= 0:
        trail_giveback = float(getattr(config, "SCALP_BTC_TRAIL_GIVEBACK", 0.006))
    timeout_profit_min = _safe_float(state.get("timeout_profit_min"), 0.0)
    if timeout_profit_min <= 0:
        timeout_profit_min = float(getattr(config, "SCALP_BTC_TIMEOUT_PROFIT_MIN", 0.0))

    reason = None
    if pnl <= -float(sl_one):
        reason = "scalp_btc_stop_loss"
    elif pnl >= float(tp_one):
        reason = "scalp_btc_take_profit"
    elif bool(getattr(config, "SCALP_BTC_TRAIL_ON", True)):
        if pnl >= float(trail_from) and from_peak <= -float(trail_giveback):
            reason = "scalp_btc_trailing"

    if reason is None:
        entry_time = state.get("entry_time")
        if isinstance(entry_time, dt.datetime):
            hold_min = (now - entry_time).total_seconds() / 60.0
            if hold_min >= float(getattr(config, "SCALP_BTC_MAX_HOLD_MIN", 90)):
                if pnl >= float(timeout_profit_min):
                    reason = "scalp_btc_timeout"
                else:
                    reason = None

    if reason is None:
        return False

    _scalp_btc_close_position(
        upbit=upbit,
        now=now,
        state=state,
        prices=prices,
        reason=reason,
        persist_state_fn=persist_state_fn,
    )
    return True


def _try_scalp_btc_entry(
    upbit,
    now: dt.datetime,
    state: dict,
    prices: dict,
    equity: float,
    krw: float,
    max_holdings: int,
    total_holding: int,
    main_state: dict,
    persist_state_fn,
    ticker_lock: _TickerLock,
    runtime_risk_state: dict = None,
    entry_params: dict = None,
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

    ticker = str(getattr(config, "SCALP_BTC_TICKER", "KRW-BTC"))

    if bool(state.get("holding", False)):
        return False
    if total_holding >= int(max_holdings):
        return False
    if int(getattr(config, "SCALP_BTC_MAX_POSITIONS", 1)) < 1:
        return False

    paused_until = state.get("paused_until")
    if isinstance(paused_until, dt.datetime) and now < paused_until:
        return False
    cooldown_until = state.get("cooldown_until")
    if isinstance(cooldown_until, dt.datetime) and now < cooldown_until:
        return False

    if bool(getattr(config, "SCALP_BTC_BLOCK_WHEN_MAIN_HOLDING", False)):
        if _count_strategy_holdings(main_state) > 0:
            return False

    if bool(main_state.get(ticker, {}).get("holding", False)):
        return False

    buy_krw = _get_scalp_btc_buy_krw(equity, krw)
    if buy_krw <= 0:
        return False

    if not bool(scalp_btc_entry_signal(ticker)):
        return False

    lock_timeout = float(getattr(config, "SCALP_BTC_LOCK_TIMEOUT_SEC", 5))
    if not ticker_lock.acquire(ticker, now, lock_timeout):
        print(f"[SCALP_BTC] deferred: lock busy {ticker}")
        return False

    try:
        cur = prices.get(ticker)
        if cur is None or float(cur) <= 0:
            return False
        cur = float(cur)

        if bool(getattr(config, "REAL_ORDER", False)):
            upbit.buy_market_order(ticker, buy_krw)
            qty, entry = wait_for_filled_snapshot(upbit, ticker, timeout_sec=3.0, interval=0.2)
            qty = float(qty) if float(qty) > 0 else float(buy_krw) / cur
            entry = float(entry) if float(entry) > 0 else cur
            log_order("BUY", ticker, qty, True, "scalp_btc")
        else:
            qty = float(buy_krw) / cur
            entry = cur
            log_order("BUY", ticker, qty, True, "scalp_btc_mock")

        state["holding"] = True
        state["ticker"] = ticker
        state["entry_price"] = float(entry)
        state["qty"] = float(qty)
        state["entry_time"] = now
        state["peak_price"] = float(entry)
        filled_buy_krw = float(entry) * float(qty)
        if filled_buy_krw <= 0:
            filled_buy_krw = float(buy_krw)
        state["total_buy_krw"] = float(filled_buy_krw)
        state["total_sell_krw"] = 0.0
        state["last_exit_reason"] = ""
        state["strategy_tag"] = "SCALP_BTC"
        if isinstance(entry_params, dict):
            state["sl_one_pct"] = abs(float(entry_params.get("sl_one", 0.0)))
            state["tp_one_pct"] = max(0.0, float(entry_params.get("tp_one", 0.0)))
            state["trail_from_pct"] = max(0.0, float(entry_params.get("trail_from", 0.0)))
            state["trail_giveback_pct"] = max(0.0, float(entry_params.get("trail_giveback", 0.0)))
            state["timeout_profit_min"] = max(0.0, float(entry_params.get("timeout_profit_min", 0.0)))
        print(f"[SCALP_BTC ENTRY] BUY {ticker} | KRW={buy_krw:,.0f}")
        buy_notify_ok = bool(
            notify_order(
            event_type="ORDER_BUY_FILLED",
            strategy_tag="SCALP_BTC",
            ticker=ticker,
            price=float(entry),
            qty=float(qty),
            reason="ENTRY",
            )
        )
        if not buy_notify_ok:
            print(f"[WARN][TELEGRAM] ORDER_BUY_FILLED queued/failed: SCALP_BTC {ticker}")
        persist_state_fn()
        return True
    except Exception as e:
        log_order("BUY", ticker, 0.0, False, f"scalp_btc_err={e}")
        print(f"[WARN] buy failed(SCALP_BTC): {ticker} err={e}")
        print(f"[WARN] ORDER failed: BUY {ticker}")
        notify_order(
            event_type="ORDER_BUY_FAILED",
            strategy_tag="SCALP_BTC",
            ticker=ticker,
            price=float(cur) if "cur" in locals() else 0.0,
            qty=0.0,
            reason="ENTRY",
        )
        return False
    finally:
        ticker_lock.release(ticker)


def run():
    bot_mode = _resolve_mode()
    enable_main, enable_scalp_legacy, enable_scalp_btc, force_mock_order = _mode_to_strategy_flags(bot_mode)
    if not _acquire_instance_lock():
        return
    atexit.register(_release_instance_lock)
    _warn_missing_requests_dependency()
    _warn_missing_telegram_env_once()

    if force_mock_order and bool(getattr(config, "REAL_ORDER", False)):
        print("[MODE] TEST mode detected: force REAL_ORDER=False")
        config.REAL_ORDER = False

    access, secret = load_keys()
    upbit = pyupbit.Upbit(access, secret)

    if bool(getattr(config, "REAL_ORDER", False)):
        print("[WARN] REAL_ORDER=True (live order mode) | auto-start (confirmation disabled)")

    ensure_trade_log_header(trade_log_path_for(now_kst()))

    strategy_state, strategy_cooldowns, inactive_positions, scalp_btc_state, runtime_risk_state = load_state()
    runtime_risk_state = _normalize_runtime_risk_state(runtime_risk_state)
    for s in STRATEGIES:
        strategy_state.setdefault(s, {})
        strategy_cooldowns.setdefault(s, {})

    verify_state_with_balance(upbit, strategy_state)
    repaired_dup = _repair_cross_strategy_duplicate_holdings(strategy_state)

    market_info = get_upbit_krw_markets()
    if market_info:
        print(f"[FILTER] loaded KRW market info: {len(market_info)}")
    else:
        print("[FILTER] market info unavailable. apply stable/user exclusions only")

    entry_param_mode = "CONSERVATIVE"
    entry_params = _get_auto_params(entry_param_mode)
    last_auto_check_ts = 0.0

    sanitize_repaired = 0
    moved_count = 0
    for s in STRATEGIES:
        active_state, moved_inactive, repaired, moved = sanitize_positions(strategy_state.get(s, {}), market_info)
        strategy_state[s] = active_state
        if moved_inactive:
            inactive_positions.update(moved_inactive)
        sanitize_repaired += int(repaired)
        moved_count += int(moved)
    sanitize_repaired += int(repaired_dup)
    if sanitize_repaired:
        print(f"[STATE] sanitize repaired fields: {sanitize_repaired}")
    if moved_count:
        print(f"[STATE] moved count: {moved_count}")

    def persist_state(*_args, **_kwargs):
        save_state(
            strategy_state,
            strategy_cooldowns,
            inactive_positions=inactive_positions,
            scalp_btc_state=scalp_btc_state,
            risk_state=runtime_risk_state,
        )

    if moved_count or repaired_dup:
        persist_state()

    now = now_kst()
    momentum_seen_at = {}
    surge_stoploss_until = {}
    ticker_lock = _TickerLock()

    raw_ranked = get_top_tickers_by_value(
        int(getattr(config, "UNIVERSE_SCAN_N", config.TOP_N)),
        market_info=market_info,
    )
    core_universe, surge_pool, inactive_universe, inactive_reasons, core_filter_rejects = _core_and_surge_from_ranked(
        raw_ranked, market_info
    )
    if enable_scalp_legacy:
        surge_candidates, momentum_seen_at = _update_surge_candidates(surge_pool, momentum_seen_at, now)
    else:
        surge_candidates = []
    active_universe = _dedupe_keep_order(core_universe + surge_candidates)
    inactive_tickers = set(inactive_universe) | set(inactive_positions.keys())
    main_filter_reject_stats = Counter(core_filter_rejects or {})
    main_filter_summary_last = now

    print_filter_summary(active_universe, inactive_universe, inactive_reasons)
    print(f"[UNIVERSE] core={len(core_universe)} surge={len(surge_candidates)}")
    if surge_candidates:
        print(f"[UNIVERSE] surge picks: {', '.join(surge_candidates[:8])}")

    k_map = build_k_map(core_universe) if enable_main else {}
    last_refresh = now
    last_status = now
    last_state_save = now
    last_tg_spool_flush = now - dt.timedelta(seconds=3600)
    tg_spool_flush_sec = max(5.0, float(getattr(config, "TELEGRAM_SPOOL_FLUSH_SEC", 30)))
    tg_spool_flush_limit = max(1, int(getattr(config, "TELEGRAM_SPOOL_FLUSH_LIMIT", 50)))
    trading_day = now.date()

    day_tp1_count_main = 0
    loss_seq_main = 0
    tp1_counted_main = {
        t
        for t, st in (strategy_state.get("MAIN", {}) or {}).items()
        if bool(st.get("holding", False)) and bool(st.get("tp1", False))
    }
    main_entry_blocked_prev = False
    guard_last_log_minute = None
    guard_notified_window_key = ""

    day_cache = {}
    intraday_cache = {}
    minute_cache = {}

    print(
        f"[BOT] start | MODE={bot_mode} | REAL_ORDER={config.REAL_ORDER} "
        f"| MAIN={'ON' if enable_main else 'OFF'} "
        f"SCALP_BTC={'ON' if enable_scalp_btc else 'OFF'} "
        f"LEGACY_SCALP={'ON' if enable_scalp_legacy else 'OFF'}"
    )
    notify_event(
        event_type="BOT_START",
        lines=[
            f"\uBAA8\uB4DC: {bot_mode}",
            f"REAL_ORDER: {bool(getattr(config, 'REAL_ORDER', False))}",
            f"\uC804\uB7B5: MAIN={'ON' if enable_main else 'OFF'}, SCALP_BTC={'ON' if enable_scalp_btc else 'OFF'}",
        ],
    )


    while True:
        try:
            now = now_kst()
            main_entry_intent = None

            if (now - last_tg_spool_flush).total_seconds() >= tg_spool_flush_sec:
                try:
                    sent = int(flush_telegram_spool(limit=tg_spool_flush_limit))
                    if sent > 0:
                        print(f"[TG_SPOOL] flushed queued notifications: {sent}")
                except Exception as e:
                    print(f"[WARN][TG_SPOOL] flush failed: {type(e).__name__}: {e}")
                last_tg_spool_flush = now

            if (now - last_refresh).total_seconds() >= float(config.REFRESH_MIN) * 60.0:
                print("\n[REFRESH] universe")
                raw_ranked = get_top_tickers_by_value(
                    int(getattr(config, "UNIVERSE_SCAN_N", config.TOP_N)),
                    market_info=market_info,
                )
                (
                    core_universe,
                    surge_pool,
                    inactive_universe,
                    inactive_reasons,
                    core_filter_rejects,
                ) = _core_and_surge_from_ranked(raw_ranked, market_info)

                if enable_scalp_legacy:
                    surge_candidates, momentum_seen_at = _update_surge_candidates(surge_pool, momentum_seen_at, now)
                else:
                    surge_candidates = []
                active_universe = _dedupe_keep_order(core_universe + surge_candidates)
                inactive_tickers = set(inactive_universe) | set(inactive_positions.keys())
                main_filter_reject_stats.update(core_filter_rejects or {})

                print_filter_summary(active_universe, inactive_universe, inactive_reasons)
                print(f"[UNIVERSE] core={len(core_universe)} surge={len(surge_candidates)}")
                if surge_candidates:
                    print(f"[UNIVERSE] surge picks: {', '.join(surge_candidates[:8])}")

                if enable_main:
                    k_map = build_k_map(core_universe)
                last_refresh = now

            main_filter_summary_min = float(getattr(config, "MAIN_FILTER_REJECT_SUMMARY_MIN", 10))
            main_filter_summary_sec = max(60.0, main_filter_summary_min * 60.0)
            if (now - main_filter_summary_last).total_seconds() >= main_filter_summary_sec:
                print_main_filter_reject_summary(main_filter_reject_stats)
                main_filter_reject_stats.clear()
                main_filter_summary_last = now

            if now.date() != trading_day:
                trading_day = now.date()
                day_tp1_count_main = 0
                loss_seq_main = 0
                tp1_counted_main = set()
                main_entry_blocked_prev = False
                print("[DAY_RESET] counters reset")

            regime = "FULL"
            if bool(getattr(config, "USE_MARKET_REGIME", False)):
                try:
                    regime = get_market_regime()
                except Exception:
                    regime = "MID"

            if bool(getattr(config, "AUTO_STRATEGY_MODE", True)):
                now_ts = float(now.timestamp())
                recheck_sec = float(getattr(config, "AUTO_STRATEGY_RECHECK_SEC", 60))
                if (now_ts - float(last_auto_check_ts)) >= recheck_sec:
                    new_mode, why = _calc_auto_strategy_mode(market_info=market_info)
                    if new_mode != entry_param_mode:
                        print(f"[AUTO_MODE] {entry_param_mode} -> {new_mode} ({why})")
                        notify_event(
                            event_type="MODE_CHANGED",
                            lines=[f"\u2699 \uC804\uB7B5 \uBAA8\uB4DC \uC804\uD658: {entry_param_mode} \u2192 {new_mode}"],
                        )
                        entry_param_mode = new_mode
                        entry_params = _get_auto_params(entry_param_mode)
                    last_auto_check_ts = now_ts

            btc_ticker = str(getattr(config, "SCALP_BTC_TICKER", "KRW-BTC"))
            holding_tickers = _all_holding_tickers_with_scalp_btc(strategy_state, scalp_btc_state)
            inactive_holding_tickers = [t for t, s in (inactive_positions or {}).items() if s.get("holding", False)]

            price_targets = set(core_universe) | set(holding_tickers) | set(inactive_holding_tickers)
            if enable_scalp_legacy:
                price_targets |= set(surge_candidates)
            if enable_scalp_btc:
                price_targets.add(btc_ticker)
            prices = batch_get_prices(price_targets)

            krw = float(get_balance(upbit, "KRW"))
            prices["_krw"] = krw
            prices["_caches"] = (day_cache, intraday_cache, minute_cache)

            equity = estimate_equity(krw, strategy_state, prices, upbit, inactive_positions=inactive_positions)
            if _scalp_btc_is_holding(scalp_btc_state):
                btc_px = prices.get(btc_ticker)
                if btc_px is not None:
                    qty = float(scalp_btc_state.get("qty", 0.0))
                    if bool(getattr(config, "REAL_ORDER", False)):
                        qty = float(get_balance(upbit, btc_ticker.split("-")[1]))
                    if qty > 0:
                        equity += qty * float(btc_px)

            base_per_trade, base_max_holdings = get_base_position_settings(equity)
            per_trade_main, max_holdings = apply_market_regime(equity, base_per_trade, base_max_holdings, regime)
            h_key, h_scale = apply_runtime_params_by_holdings(max_holdings)

            total_holding = _count_total_holdings_with_scalp_btc(
                strategy_state, scalp_btc_state, include_legacy_scalp=enable_scalp_legacy
            )

            prev_day_key = str(runtime_risk_state.get("day_key", ""))
            prev_halted = bool(runtime_risk_state.get("halted_flag", False))
            risk_info, _, risk_triggered = _update_global_risk_cut_state(
                now,
                equity,
                runtime_risk_state,
                persist_state_fn=persist_state,
            )
            risk_halted = bool(risk_info.get("halted", False))
            halt_reason = str(risk_info.get("reason", ""))

            if risk_triggered:
                print(
                    f"[RISK_CUT] HALT reason={halt_reason} equity={equity:,.0f} "
                    f"daily={float(risk_info.get('daily_loss_pct', 0.0)):+.2f}% "
                    f"mdd={float(risk_info.get('mdd_pct', 0.0)):+.2f}%"
                )
                _notify_risk_cut_once(risk_info, equity)

            if prev_halted and (not risk_halted):
                print(
                    f"[RISK_CUT] cleared | day={risk_info.get('day_key')} "
                    f"daily={float(risk_info.get('daily_loss_pct', 0.0)):+.2f}% "
                    f"mdd={float(risk_info.get('mdd_pct', 0.0)):+.2f}%"
                )

            if (str(runtime_risk_state.get("day_key", "")) != prev_day_key) or (prev_halted != risk_halted) or risk_triggered:
                persist_state()

            day_tp1_count_main = update_day_tp1_counter(
                strategy_state.get("MAIN", {}),
                tp1_counted_main,
                day_tp1_count_main,
                "MAIN",
            )

            tp1_limit = int(getattr(config, "DAILY_TP1_EXIT_LIMIT", 3))
            main_loss_limit = int(getattr(config, "MAIN_CONSEC_LOSS_LIMIT", getattr(config, "CONSEC_LOSS_EXIT_LIMIT", 4)))
            main_entry_allowed = (day_tp1_count_main < tp1_limit) and (loss_seq_main < main_loss_limit) and (not risk_halted)

            blocked_main = not main_entry_allowed
            if blocked_main and not main_entry_blocked_prev:
                if day_tp1_count_main >= tp1_limit:
                    print(f"[ENTRY_BLOCK][MAIN] reason=TP1_LIMIT count={day_tp1_count_main}")
                if loss_seq_main >= main_loss_limit:
                    print(f"[ENTRY_BLOCK][MAIN] reason=LOSS_STREAK count={loss_seq_main}")
                if risk_halted:
                    print(
                        f"[ENTRY_BLOCK][GLOBAL] reason={halt_reason} "
                        f"daily={float(risk_info.get('daily_loss_pct', 0.0)):+.2f}% "
                        f"mdd={float(risk_info.get('mdd_pct', 0.0)):+.2f}%"
                    )
            main_entry_blocked_prev = blocked_main

            if (now - last_status).total_seconds() >= float(config.STATUS_PRINT_SEC):
                main_h = _count_strategy_holdings(strategy_state.get("MAIN", {}))
                legacy_h = _count_strategy_holdings(strategy_state.get("SCALP", {})) if enable_scalp_legacy else 0
                scalp_btc_h = 1 if _scalp_btc_is_holding(scalp_btc_state) else 0
                pause_txt = "-"
                paused_until = scalp_btc_state.get("paused_until")
                if isinstance(paused_until, dt.datetime) and now < paused_until:
                    pause_txt = f"{max(0, int((paused_until - now).total_seconds() / 60))}m"
                print(
                    f"[STATUS] Regime={regime} | Equity~{equity:,.0f} | PerTrade~{per_trade_main:,.0f} | "
                    f"Holding={total_holding}/{max_holdings} | MAIN={main_h} LEGACY={legacy_h} SCALP_BTC={scalp_btc_h} | "
                    f"Core={len(core_universe)} Surge={len(surge_candidates)} | "
                    f"HKey={h_key} HScale={h_scale:.2f} | TP1_MAIN={day_tp1_count_main} "
                    f"Loss_MAIN={loss_seq_main} SBtcLoss={int(scalp_btc_state.get('loss_streak', 0))} SBtcPause={pause_txt} | "
                    f"RiskHalt={'Y' if risk_halted else 'N'} Day={risk_info.get('day_key', '')} "
                    f"DayStart={float(risk_info.get('day_start_equity', 0.0)):,.0f} Peak={float(risk_info.get('peak_equity', 0.0)):,.0f}"
                )
                last_status = now

            # 1) MAIN position management
            if enable_main:
                events_main = manage_positions(
                    upbit=upbit,
                    now=now,
                    state=strategy_state["MAIN"],
                    prices=prices,
                    cooldown_until=strategy_cooldowns["MAIN"],
                    save_state_fn=persist_state,
                    inactive_tickers=inactive_tickers,
                    inactive_positions=inactive_positions,
                    strategy="MAIN",
                )
                if events_main:
                    _notify_closed_trade_events(events_main)
                    loss_seq_main = update_loss_seq_from_events(events_main, loss_seq_main, "MAIN")
                    day_tp1_count_main = update_day_tp1_counter(
                        strategy_state.get("MAIN", {}),
                        tp1_counted_main,
                        day_tp1_count_main,
                        "MAIN",
                    )

            # 2) SCALP_BTC position management
            if enable_scalp_btc:
                _manage_scalp_btc_position(
                    upbit=upbit,
                    now=now,
                    state=scalp_btc_state,
                    prices=prices,
                    persist_state_fn=persist_state,
                )

            # 2b) Legacy SCALP position management (optional)
            if enable_scalp_legacy:
                events_scalp = manage_positions(
                    upbit=upbit,
                    now=now,
                    state=strategy_state["SCALP"],
                    prices=prices,
                    cooldown_until=strategy_cooldowns["SCALP"],
                    save_state_fn=persist_state,
                    inactive_tickers=inactive_tickers,
                    inactive_positions=inactive_positions,
                    strategy="SCALP",
                )
                if events_scalp:
                    _notify_closed_trade_events(events_scalp)
                    block_min = int(getattr(config, "SURGE_STOPLOSS_REENTRY_BLOCK_MIN", 30))
                    for e in events_scalp:
                        pnl = float(e.get("pnl_pct", 0.0))
                        reason = str(e.get("reason", ""))
                        if pnl < 0 and reason in {"stoploss", "stop_loss", "trailing"}:
                            t = str(e.get("ticker", ""))
                            if t:
                                surge_stoploss_until[t] = now + dt.timedelta(minutes=block_min)
                                print(f"[SURGE_BLOCK] {t} blocked {block_min}m ({reason})")

            if risk_triggered:
                print(
                    f"[RISK_GUARD] same-loop entry scan skipped after risk cut | "
                    f"reason={halt_reason} | equity={equity:,.0f}"
                )
                time.sleep(float(config.POLL_SEC))
                continue

            guard_active, guard_start, guard_end, guard_key = _entry_guard_window(now)
            if guard_active:
                minute_bucket = now.replace(second=0, microsecond=0)
                if guard_last_log_minute != minute_bucket:
                    start_txt = guard_start.strftime("%H:%M") if isinstance(guard_start, dt.datetime) else "09:00"
                    end_txt = guard_end.strftime("%H:%M") if isinstance(guard_end, dt.datetime) else "09:15"
                    print(
                        f"[GUARD] entry blocked ({start_txt}~{end_txt} KST window)"
                    )
                    guard_last_log_minute = minute_bucket
                if bool(getattr(config, "ENTRY_GUARD_NOTIFY_TELEGRAM", False)) and guard_notified_window_key != guard_key:
                    start_txt = guard_start.strftime("%H:%M") if isinstance(guard_start, dt.datetime) else "09:00"
                    end_txt = guard_end.strftime("%H:%M") if isinstance(guard_end, dt.datetime) else "09:15"
                    notify_event(
                        event_type="ENTRY_GUARD_ACTIVE",
                        lines=[
                            f"신규진입 차단: {start_txt}~{end_txt} KST",
                            f"사유: 장시작 변동성 보호",
                        ],
                    )
                    guard_notified_window_key = str(guard_key)

            total_holding = _count_total_holdings_with_scalp_btc(
                strategy_state, scalp_btc_state, include_legacy_scalp=enable_scalp_legacy
            )

            # 3) MAIN entry scan (intent at order-finalization)
            if (not guard_active) and enable_main and main_entry_allowed and total_holding < max_holdings and float(per_trade_main) > 0:
                def before_main_buy(ticker: str, buy_krw: float, cur: float):
                    nonlocal main_entry_intent
                    if ticker != btc_ticker:
                        return True

                    main_entry_intent = {"strategy": "MAIN", "ticker": ticker, "ts": now}
                    try:
                        if not _scalp_btc_is_holding(scalp_btc_state):
                            return True

                        lock_timeout = float(getattr(config, "SCALP_BTC_LOCK_TIMEOUT_SEC", 5))
                        if not ticker_lock.acquire(ticker, now, lock_timeout):
                            print("[SWITCH] lock busy -> main BTC entry deferred")
                            return False

                        try:
                            ok, err = _scalp_btc_close_position(
                                upbit=upbit,
                                now=now,
                                state=scalp_btc_state,
                                prices=prices,
                                reason="switch_to_main",
                                persist_state_fn=persist_state,
                            )
                        finally:
                            ticker_lock.release(ticker)

                        if ok:
                            scalp_btc_state["switch_fail_count"] = 0
                            persist_state()
                            return True

                        print("[SWITCH fail] scalp close failed -> main BTC entry deferred")
                        cnt = int(scalp_btc_state.get("switch_fail_count", 0)) + 1
                        scalp_btc_state["switch_fail_count"] = cnt
                        limit = int(getattr(config, "SCALP_BTC_SWITCH_FAIL_LIMIT", 3))
                        if cnt >= limit:
                            pause_min = int(getattr(config, "SCALP_BTC_SWITCH_FAIL_PAUSE_MIN", 60))
                            scalp_btc_state["paused_until"] = now + dt.timedelta(minutes=max(1, pause_min))
                            scalp_btc_state["switch_fail_count"] = 0
                            print("[SCALP_BTC_PAUSE] paused 60m due to repeated switch failures")
                        persist_state()
                        return False
                    finally:
                        main_entry_intent = None

                try:
                    did_main = try_main_entries(
                        upbit=upbit,
                        now=now,
                        universe=core_universe,
                        prices=prices,
                        k_map=k_map,
                        state=strategy_state["MAIN"],
                        cooldown_until=strategy_cooldowns["MAIN"],
                        per_trade_amt=float(per_trade_main),
                        max_holdings=max_holdings,
                        total_holding_cnt=total_holding,
                        regime=regime,
                        wait_for_filled_snapshot_fn=wait_for_filled_snapshot,
                        save_state_fn=persist_state,
                        inactive_tickers=inactive_tickers,
                        inactive_positions=inactive_positions,
                        global_holding_tickers=_all_holding_tickers_with_scalp_btc(strategy_state, scalp_btc_state),
                        before_buy_fn=before_main_buy,
                        entry_params=entry_params.get("MAIN") if isinstance(entry_params, dict) else None,
                        main_mode=entry_param_mode,
                        runtime_risk_state=runtime_risk_state,
                        equity=equity,
                    )
                finally:
                    main_entry_intent = None

                if did_main:
                    total_holding = _count_total_holdings_with_scalp_btc(
                        strategy_state, scalp_btc_state, include_legacy_scalp=enable_scalp_legacy
                    )

            # 4) Legacy SCALP entry (optional)
            if (not guard_active) and enable_scalp_legacy and (not risk_halted) and total_holding < max_holdings:
                scalp_universe = []
                for ticker in surge_candidates:
                    until = surge_stoploss_until.get(ticker)
                    if until is not None and now < until:
                        continue
                    scalp_universe.append(ticker)

                scalp_buy_krw = float(getattr(config, "MINUTE_TEST_PER_TRADE_KRW", config.TEST_PER_TRADE_KRW))
                did_legacy = try_scalp_entries(
                    upbit=upbit,
                    now=now,
                    universe=scalp_universe,
                    prices=prices,
                    state=strategy_state["SCALP"],
                    cooldown_until=strategy_cooldowns["SCALP"],
                    per_trade_amt=scalp_buy_krw,
                    max_holdings=max_holdings,
                    total_holding_cnt=total_holding,
                    regime=regime,
                    wait_for_filled_snapshot_fn=wait_for_filled_snapshot,
                    save_state_fn=persist_state,
                    inactive_tickers=inactive_tickers,
                    inactive_positions=inactive_positions,
                    global_holding_tickers=_all_holding_tickers_with_scalp_btc(strategy_state, scalp_btc_state),
                    conservative=False,
                    runtime_risk_state=runtime_risk_state,
                    equity=equity,
                )
                if did_legacy:
                    total_holding = _count_total_holdings_with_scalp_btc(
                        strategy_state, scalp_btc_state, include_legacy_scalp=enable_scalp_legacy
                    )

            # 5) SCALP_BTC entry (always last)
            if (not guard_active) and enable_scalp_btc and (not risk_halted) and total_holding < max_holdings:
                did_scalp_btc = _try_scalp_btc_entry(
                    upbit=upbit,
                    now=now,
                    state=scalp_btc_state,
                    prices=prices,
                    equity=equity,
                    krw=krw,
                    max_holdings=max_holdings,
                    total_holding=total_holding,
                    main_state=strategy_state["MAIN"],
                    persist_state_fn=persist_state,
                    ticker_lock=ticker_lock,
                    runtime_risk_state=runtime_risk_state,
                    entry_params=entry_params.get("SCALP_BTC") if isinstance(entry_params, dict) else None,
                )
                if did_scalp_btc:
                    total_holding = _count_total_holdings_with_scalp_btc(
                        strategy_state, scalp_btc_state, include_legacy_scalp=enable_scalp_legacy
                    )

            if (now - last_state_save).total_seconds() >= float(config.STATE_SAVE_INTERVAL_SEC):
                persist_state()
                last_state_save = now

            time.sleep(float(config.POLL_SEC))

        except KeyboardInterrupt:
            print("\nUser interrupted (Ctrl+C)")
            notify_event(event_type="BOT_STOP", lines=["\uC0AC\uC720: \uC0AC\uC6A9\uC790 \uC911\uC9C0"])
            persist_state()
            break
        except Exception as e:
            _notify_loop_error_once(e)
            print(f"[ERROR] {type(e).__name__}: {e}")
            traceback.print_exc(limit=2)
            time.sleep(1)


if __name__ == "__main__":
    run()

