"""Send daily report (21:00 KST) and optional 09:00 heartbeat to Telegram."""

import argparse
import csv
import datetime as dt
import json
import math
import os
import subprocess
import traceback
from typing import Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

import pyupbit

import config
from market import get_balance_info, load_keys
from state_store import load_state
from utils.log_paths import list_trade_log_paths, report_log_path_for, trade_log_path_for
from utils.telegram_notify import has_telegram_credentials, load_telegram_env_file, tg_notify


KST = ZoneInfo("Asia/Seoul")
SUMMARY_CSV = "trading_summary.csv"
SUMMARY_XLSX = "trading_summary.xlsx"
DEFAULT_SERVICE_NAME = "upbit-bot"
EQUITY_HISTORY_JSONL = "equity_snapshot_history.jsonl"
EQUITY_ANCHOR_MAX_AFTER_SEC = 3600


def now_kst() -> dt.datetime:
    return dt.datetime.now(KST)


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _parse_trade_time(raw: str):
    s = str(raw or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return dt.datetime.strptime(s, fmt).replace(tzinfo=KST)
        except Exception:
            pass
    try:
        parsed = dt.datetime.fromisoformat(s)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=KST)
        return parsed.astimezone(KST)
    except Exception:
        return None


def _dedupe_key(row: Dict[str, str]):
    return (
        str(row.get("time", "")).strip(),
        str(row.get("ticker", "")).strip(),
        str(row.get("entry_price", "")).strip(),
        str(row.get("exit_price", "")).strip(),
        str(row.get("pnl_pct", "")).strip(),
        str(row.get("reason", "")).strip(),
        str(row.get("strategy", "")).strip(),
    )


def _read_trade_rows(path: str):
    if not os.path.exists(path):
        return []
    out = []
    try:
        with open(path, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                ts = _parse_trade_time(row.get("time", ""))
                if ts is None:
                    continue
                out.append(
                    {
                        "time_dt": ts,
                        "time": str(row.get("time", "")).strip(),
                        "ticker": str(row.get("ticker", "")).strip(),
                        "entry_price": str(row.get("entry_price", "")).strip(),
                        "exit_price": str(row.get("exit_price", "")).strip(),
                        "pnl_pct": str(row.get("pnl_pct", "")).strip(),
                        "reason": str(row.get("reason", "")).strip(),
                        "regime": str(row.get("regime", "")).strip(),
                        "strategy": str(row.get("strategy", "")).strip().upper(),
                    }
                )
    except Exception:
        return []
    return out


def load_all_trades(base_dir: str = "."):
    files = list_trade_log_paths(base_dir=base_dir)
    rows = []
    for path in files:
        rows.extend(_read_trade_rows(path))
    rows.sort(key=lambda x: x["time_dt"])

    deduped = []
    seen = set()
    for row in rows:
        key = _dedupe_key(row)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped, files


def report_window_21_to_21(now: dt.datetime):
    today_21 = now.replace(hour=21, minute=0, second=0, microsecond=0)
    end = today_21 if now >= today_21 else (today_21 - dt.timedelta(days=1))
    start = end - dt.timedelta(days=1)
    return start, end


def month_window(end: dt.datetime):
    return end.replace(day=1, hour=21, minute=0, second=0, microsecond=0), end


def filter_rows(rows: List[Dict[str, str]], start: dt.datetime = None, end: dt.datetime = None):
    out = []
    for row in rows:
        ts = row["time_dt"]
        if (start is not None) and ts < start:
            continue
        if (end is not None) and ts >= end:
            continue
        out.append(row)
    out.sort(key=lambda x: x["time_dt"])
    return out


def _is_stop_reason(reason: str) -> bool:
    s = str(reason or "").strip().lower()
    if not s:
        return False
    if s in {"sl", "stoploss", "stop_loss"}:
        return True
    if "stop" in s:
        return True
    if "loss" in s and "timeout" not in s:
        return True
    return False


def _is_partial_reason(reason: str) -> bool:
    code = str(reason or "").strip().upper()
    return code in {"TP1", "TP2_PARTIAL"}


def _derive_pnl_pct_from_prices(row: Dict[str, str]) -> Optional[float]:
    entry = _to_float((row or {}).get("entry_price", 0.0), 0.0)
    exit_ = _to_float((row or {}).get("exit_price", 0.0), 0.0)
    if entry > 0 and exit_ > 0:
        return (float(exit_) / float(entry) - 1.0) * 100.0
    return None


def _row_pnl_pct_for_metrics(row: Dict[str, str]) -> float:
    """
    Guard daily-report stats from legacy malformed pnl_pct (e.g. -100% artifacts).
    Prefer logged pnl_pct, but if it is clearly invalid and prices are sane, fallback
    to price-derived pnl.
    """
    logged = _to_float((row or {}).get("pnl_pct", 0.0), 0.0)
    if not math.isfinite(logged):
        logged = 0.0

    derived = _derive_pnl_pct_from_prices(row)
    if derived is None or (not math.isfinite(derived)):
        return float(logged)

    # Legacy settlement glitch occasionally wrote -100% even when exit/entry shows normal loss.
    if float(logged) <= -95.0 and float(derived) > -30.0:
        return float(derived)
    # Additional guard: if logged pnl largely disagrees with price-derived pnl,
    # prefer derived value to avoid distorted stats from malformed legacy rows.
    if abs(float(logged) - float(derived)) >= 20.0 and abs(float(derived)) <= 30.0:
        return float(derived)
    return float(logged)


def _compound_return_pct(rows: List[Dict[str, str]]) -> float:
    rows = [r for r in rows if not _is_partial_reason(r.get("reason", ""))]
    mult = 1.0
    for row in rows:
        pnl = _row_pnl_pct_for_metrics(row)
        mult *= (1.0 + pnl / 100.0)
    return (mult - 1.0) * 100.0


def build_metrics(rows: List[Dict[str, str]]):
    rows = [r for r in rows if not _is_partial_reason(r.get("reason", ""))]
    n = len(rows)
    pnls = [_row_pnl_pct_for_metrics(row) for row in rows]
    wins = sum(1 for x in pnls if x > 0)
    wr = (wins / n * 100.0) if n > 0 else 0.0
    avg = (sum(pnls) / n) if n > 0 else 0.0
    cum = _compound_return_pct(rows) if n > 0 else 0.0
    max_pnl = max((x for x in pnls if x > 0), default=0.0)
    min_pnl = min((x for x in pnls if x < 0), default=0.0)
    sl_cnt = sum(1 for row in rows if _is_stop_reason(row.get("reason", "")))
    sl_ratio = (sl_cnt / n * 100.0) if n > 0 else 0.0
    recent = pnls[-10:]
    avg10 = (sum(recent) / len(recent)) if recent else 0.0
    return {
        "n": int(n),
        "wr": float(wr),
        "avg": float(avg),
        "cum": float(cum),
        "max": float(max_pnl),
        "min": float(min_pnl),
        "sl_ratio": float(sl_ratio),
        "avg10": float(avg10),
    }


def _rows_without_partials(rows: List[Dict[str, str]]):
    return [r for r in list(rows or []) if not _is_partial_reason(r.get("reason", ""))]


def _load_krw_market_set(log_line=None) -> Optional[Set[str]]:
    try:
        markets = pyupbit.get_tickers(fiat="KRW")
    except Exception as e:
        if callable(log_line):
            log_line(f"[WARN] snapshot market list failed: {type(e).__name__}: {e}")
        return None
    if not isinstance(markets, list):
        return None
    out: Set[str] = set()
    for raw in markets:
        t = str(raw or "").strip().upper()
        if t.startswith("KRW-"):
            out.add(t)
    return out


def _fetch_price_map_with_fallback(tickers: List[str], log_line=None) -> Dict[str, float]:
    ordered: List[str] = []
    seen = set()
    for raw in list(tickers or []):
        t = str(raw or "").strip().upper()
        if (not t) or t in seen:
            continue
        seen.add(t)
        ordered.append(t)
    if not ordered:
        return {}

    prices: Dict[str, float] = {}
    remaining: List[str] = list(ordered)
    try:
        raw = pyupbit.get_current_price(ordered)
        if isinstance(raw, dict):
            next_remaining: List[str] = []
            for t in ordered:
                p = _to_float(raw.get(t, 0.0), 0.0)
                if p > 0:
                    prices[t] = float(p)
                else:
                    next_remaining.append(t)
            remaining = next_remaining
        elif len(ordered) == 1:
            p = _to_float(raw, 0.0)
            if p > 0:
                prices[ordered[0]] = float(p)
                remaining = []
        else:
            remaining = list(ordered)
    except Exception as e:
        if callable(log_line):
            log_line(f"[WARN] snapshot batch price failed: {type(e).__name__}: {e}")
        remaining = list(ordered)

    for t in remaining:
        try:
            raw_one = pyupbit.get_current_price(t)
        except Exception as e:
            if callable(log_line):
                log_line(f"[WARN] snapshot price failed: {t} {type(e).__name__}: {e}")
            continue
        p = _to_float(raw_one, 0.0)
        if p > 0:
            prices[t] = float(p)
    return prices


def _safe_account_snapshot(log_line):
    snapshot = {
        "krw_balance": 0.0,
        "krw_available": 0.0,
        "coin_value": 0.0,
        "total_equity": 0.0,
        "has_coin": False,
    }
    try:
        access, secret = load_keys()
        upbit = pyupbit.Upbit(access, secret)
        krw_available, krw_total = get_balance_info(upbit, "KRW")
        snapshot["krw_available"] = float(max(0.0, krw_available))
        snapshot["krw_balance"] = float(max(0.0, krw_total))

        coin_qty = {}
        try:
            accounts = upbit.get_balances()
        except Exception:
            accounts = []

        if isinstance(accounts, list):
            for row in accounts:
                if not isinstance(row, dict):
                    continue
                currency = str(row.get("currency") or "").upper().strip()
                if (not currency) or currency == "KRW":
                    continue
                bal = max(0.0, _to_float(row.get("balance", 0.0), 0.0))
                locked = max(0.0, _to_float(row.get("locked", 0.0), 0.0))
                qty = bal + locked
                if qty <= 0:
                    continue
                ticker = f"KRW-{currency}"
                coin_qty[ticker] = float(coin_qty.get(ticker, 0.0)) + float(qty)

        has_coin = bool(coin_qty)
        snapshot["has_coin"] = bool(has_coin)
        coin_value = 0.0
        if has_coin:
            krw_markets = _load_krw_market_set(log_line=log_line)
            priceable_qty = {}
            skipped_tickers = []
            for t, q in coin_qty.items():
                if (krw_markets is not None) and (t not in krw_markets):
                    skipped_tickers.append(t)
                    continue
                priceable_qty[t] = float(q)
            if skipped_tickers and callable(log_line):
                head = ", ".join(skipped_tickers[:5])
                tail = " ..." if len(skipped_tickers) > 5 else ""
                log_line(f"[WARN] snapshot skip non-KRW market holdings: {head}{tail}")

            price_map = _fetch_price_map_with_fallback(list(priceable_qty.keys()), log_line=log_line)
            for t, q in priceable_qty.items():
                p = _to_float(price_map.get(t, 0.0), 0.0)
                if p > 0 and q > 0:
                    coin_value += float(q) * float(p)

        snapshot["coin_value"] = float(max(0.0, coin_value))
        snapshot["total_equity"] = float(snapshot["krw_balance"] + snapshot["coin_value"])
    except Exception as e:
        log_line(f"[WARN] account snapshot failed: {type(e).__name__}: {e}")
        log_line(traceback.format_exc().strip())
        snapshot["total_equity"] = float(snapshot["krw_balance"] + snapshot["coin_value"])
    return snapshot


def _parse_history_ts(raw) -> Optional[dt.datetime]:
    try:
        ts = dt.datetime.fromisoformat(str(raw))
    except Exception:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=KST)
    return ts.astimezone(KST)


def _load_equity_history(path: str, log_line=None) -> List[Tuple[dt.datetime, float]]:
    out: List[Tuple[dt.datetime, float]] = []
    if not os.path.exists(path):
        return out
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                s = str(line or "").strip()
                if not s:
                    continue
                try:
                    row = json.loads(s)
                except Exception:
                    continue
                ts = _parse_history_ts(row.get("ts"))
                eq = max(0.0, _to_float(row.get("total_equity", 0.0), 0.0))
                if ts is None or eq <= 0:
                    continue
                out.append((ts, float(eq)))
    except Exception as e:
        if callable(log_line):
            log_line(f"[WARN] equity history load failed: {type(e).__name__}: {e}")
    out.sort(key=lambda x: x[0])
    filtered, dropped = _filter_equity_spikes(out)
    if dropped > 0 and callable(log_line):
        log_line(f"[WARN] equity history spike filtered: {dropped}")
    return filtered


def _filter_equity_spikes(
    points: List[Tuple[dt.datetime, float]],
    drop_pct: float = -20.0,
    recover_floor_pct: float = -5.0,
    spike_up_pct: float = 20.0,
    rollback_ceiling_pct: float = 5.0,
    max_gap_min: float = 30.0,
) -> Tuple[List[Tuple[dt.datetime, float]], int]:
    """
    Remove single-point spikes that revert quickly:
    - sudden drop then quick recovery
    - sudden jump then quick rollback
    """
    src = list(points or [])
    if len(src) < 3:
        return src, 0

    keep = [True] * len(src)
    for i in range(1, len(src) - 1):
        ts_prev, eq_prev = src[i - 1]
        ts_cur, eq_cur = src[i]
        ts_next, eq_next = src[i + 1]
        prev = _to_float(eq_prev, 0.0)
        cur = _to_float(eq_cur, 0.0)
        nxt = _to_float(eq_next, 0.0)
        if prev <= 0 or cur <= 0 or nxt <= 0:
            continue
        gap_min = (ts_next - ts_cur).total_seconds() / 60.0
        if gap_min < 0 or gap_min > float(max_gap_min):
            continue
        cur_vs_prev = (cur / prev - 1.0) * 100.0
        next_vs_prev = (nxt / prev - 1.0) * 100.0

        drop_spike = (cur_vs_prev <= float(drop_pct)) and (next_vs_prev >= float(recover_floor_pct))
        up_spike = (cur_vs_prev >= float(spike_up_pct)) and (next_vs_prev <= float(rollback_ceiling_pct))
        if drop_spike or up_spike:
            keep[i] = False

    filtered = [p for k, p in zip(keep, src) if k]
    dropped = len(src) - len(filtered)
    return filtered, dropped


def _append_equity_history(path: str, captured_at: dt.datetime, total_equity: float, log_line=None):
    eq = max(0.0, _to_float(total_equity, 0.0))
    if eq <= 0:
        return
    row = {"ts": captured_at.astimezone(KST).isoformat(), "total_equity": float(eq)}
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        if callable(log_line):
            log_line(f"[WARN] equity history append failed: {type(e).__name__}: {e}")


def _find_anchor_equity(
    history: List[Tuple[dt.datetime, float]],
    boundary: dt.datetime,
    max_after_sec: float = EQUITY_ANCHOR_MAX_AFTER_SEC,
) -> Optional[float]:
    if not history:
        return None
    before = [float(eq) for ts, eq in history if ts <= boundary and eq > 0]
    if before:
        return float(before[-1])

    first_after = None
    for ts, eq in history:
        if ts > boundary and eq > 0:
            first_after = (ts, float(eq))
            break
    if first_after is None:
        return None
    if (first_after[0] - boundary).total_seconds() <= float(max_after_sec):
        return float(first_after[1])
    return None


def _calc_snapshot_pnl(current_equity: float, anchor_equity: Optional[float]) -> Tuple[float, float, bool]:
    cur = max(0.0, _to_float(current_equity, 0.0))
    anchor = _to_float(anchor_equity, 0.0) if anchor_equity is not None else 0.0
    if anchor <= 0:
        return 0.0, 0.0, False
    pnl_krw = float(cur - anchor)
    pnl_pct = (pnl_krw / float(anchor)) * 100.0
    return float(pnl_krw), float(pnl_pct), True


def _build_month_equity_points(
    history: List[Tuple[dt.datetime, float]],
    month_start: dt.datetime,
    now: dt.datetime,
    current_equity: float,
) -> List[Tuple[dt.datetime, float]]:
    points: List[Tuple[dt.datetime, float]] = []
    month_anchor = _find_anchor_equity(history, month_start)
    if month_anchor is not None and month_anchor > 0:
        points.append((month_start, float(month_anchor)))
    for ts, eq in history:
        if month_start <= ts <= now and float(eq) > 0:
            points.append((ts, float(eq)))
    cur = max(0.0, _to_float(current_equity, 0.0))
    if cur > 0:
        points.append((now, float(cur)))

    points.sort(key=lambda x: x[0])
    deduped: List[Tuple[dt.datetime, float]] = []
    for ts, eq in points:
        if deduped and deduped[-1][0] == ts:
            deduped[-1] = (ts, eq)
        else:
            deduped.append((ts, eq))
    return deduped


def _calc_mdd_from_equity_points(points: List[Tuple[dt.datetime, float]]) -> Optional[float]:
    vals = [max(0.0, _to_float(eq, 0.0)) for _, eq in list(points or []) if _to_float(eq, 0.0) > 0]
    if len(vals) < 2:
        return None
    peak = float(vals[0])
    mdd = 0.0
    for eq in vals[1:]:
        cur = float(eq)
        if cur > peak:
            peak = cur
        if peak > 0:
            dd = (cur / peak - 1.0) * 100.0
            if dd < mdd:
                mdd = dd
    return float(mdd)


def _resolve_status_emoji(daily_pnl_pct: float, risk_state: dict) -> str:
    if bool((risk_state or {}).get("halted_flag", False)):
        return "\U0001F6D1"
    if _to_float(daily_pnl_pct, 0.0) < 0:
        return "\U0001F7E1"
    return "\U0001F7E2"


def _status_label(status_emoji: str) -> str:
    code = str(status_emoji or "").strip()
    if code == "\U0001F6D1":
        return "HALT"
    if code == "\U0001F7E1":
        return "CAUTION"
    return "NORMAL"
def build_strategy_metrics_month(rows_month: List[Dict[str, str]]):
    out = {}
    for strategy in ("MAIN", "SCALP_BTC"):
        part = [r for r in rows_month if str(r.get("strategy", "")).upper() == strategy]
        m = build_metrics(part)
        out[strategy] = {"n": m["n"], "wr": m["wr"], "avg": m["avg"]}
    return out


def write_summary_csv(path: str, daily: dict, month: dict, by_strategy: dict, month_mdd_pct: Optional[float] = None):
    mdd_text = "N/A" if month_mdd_pct is None else f"{_to_float(month_mdd_pct, 0.0):.4f}"
    rows = [
        ["section", "metric", "value"],
        ["daily", "trades", daily["n"]],
        ["daily", "winrate_pct", f"{daily['wr']:.4f}"],
        ["daily", "avg_pnl_pct", f"{daily['avg']:.4f}"],
        ["daily", "compound_pct", f"{daily['cum']:.4f}"],
        ["daily", "max_pnl_pct", f"{daily['max']:.4f}"],
        ["daily", "min_pnl_pct", f"{daily['min']:.4f}"],
        ["daily", "stoploss_ratio_pct", f"{daily['sl_ratio']:.4f}"],
        ["daily", "recent10_avg_pnl_pct", f"{daily['avg10']:.4f}"],
        ["month", "trades", month["n"]],
        ["month", "winrate_pct", f"{month['wr']:.4f}"],
        ["month", "avg_pnl_pct", f"{month['avg']:.4f}"],
        ["month", "compound_pct", f"{month['cum']:.4f}"],
        ["month", "mdd_pct", mdd_text],
    ]
    for strategy in ("MAIN", "SCALP_BTC"):
        s = by_strategy.get(strategy, {"n": 0, "wr": 0.0, "avg": 0.0})
        rows.append([f"month_{strategy}", "trades", s["n"]])
        rows.append([f"month_{strategy}", "winrate_pct", f"{s['wr']:.4f}"])
        rows.append([f"month_{strategy}", "avg_pnl_pct", f"{s['avg']:.4f}"])

    with open(path, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)


def write_xlsx_if_requested(path: str, daily: dict, month: dict, by_strategy: dict, month_mdd_pct: Optional[float] = None):
    try:
        from openpyxl import Workbook
    except Exception:
        return False, "openpyxl_missing"

    wb = Workbook()
    ws = wb.active
    ws.title = "summary"
    ws.append(["section", "metric", "value"])

    for section, data in (("daily", daily), ("month", month)):
        for k, v in data.items():
            ws.append([section, k, v])
    ws.append(["month", "mdd_pct", "N/A" if month_mdd_pct is None else float(month_mdd_pct)])
    for strategy in ("MAIN", "SCALP_BTC"):
        s = by_strategy.get(strategy, {"n": 0, "wr": 0.0, "avg": 0.0})
        ws.append([f"month_{strategy}", "n", s["n"]])
        ws.append([f"month_{strategy}", "wr", s["wr"]])
        ws.append([f"month_{strategy}", "avg", s["avg"]])

    wb.save(path)
    return True, ""


def build_report_text(
    report_end: dt.datetime,
    day_start: dt.datetime,
    day: dict,
    month: dict,
    by_strategy: dict,
    snapshot: dict,
    pnl_amounts: dict,
    month_mdd_pct: Optional[float],
    status_emoji: str,
):
    krw_balance = max(0.0, _to_float((snapshot or {}).get("krw_balance", 0.0), 0.0))
    coin_value = max(0.0, _to_float((snapshot or {}).get("coin_value", 0.0), 0.0))
    total_equity = max(0.0, _to_float((snapshot or {}).get("total_equity", 0.0), 0.0))
    daily_krw = _to_float((pnl_amounts or {}).get("daily_krw", 0.0), 0.0)
    daily_pct = _to_float((pnl_amounts or {}).get("daily_pct", 0.0), 0.0)
    month_krw = _to_float((pnl_amounts or {}).get("month_krw", 0.0), 0.0)
    month_pct = _to_float((pnl_amounts or {}).get("month_pct", 0.0), 0.0)
    month_mdd_text = f"{_to_float(month_mdd_pct, 0.0):.2f}%" if month_mdd_pct is not None else "N/A"
    month_compound = _to_float((month or {}).get("cum", 0.0), 0.0)
    main_stats = (by_strategy or {}).get("MAIN", {"n": 0, "wr": 0.0, "avg": 0.0})
    scalp_stats = (by_strategy or {}).get("SCALP_BTC", {"n": 0, "wr": 0.0, "avg": 0.0})
    status_code = str(status_emoji or "\U0001F7E2")
    status_text = _status_label(status_code)
    sep = "\u2501" * 18

    lines = [
        f"\U0001F4CA \uC77C\uC77C \uC131\uC801 \uB9AC\uD3EC\uD2B8 (KST) | {report_end.strftime('%Y-%m-%d 21:00')}",
        f"\uAE30\uAC04: {day_start.strftime('%m/%d 21:00')} ~ {report_end.strftime('%m/%d 21:00')}",
        "",
        sep,
        "\U0001F3E6 \uACC4\uC88C \uC2A4\uB0C5\uC0F7",
        f"- KRW \uC794\uACE0: {krw_balance:,.0f}\uC6D0",
        f"- \uCF54\uC778 \uD3C9\uAC00\uAE08: {coin_value:,.0f}\uC6D0",
        f"- \uCD1D\uC790\uC0B0: {total_equity:,.0f}\uC6D0",
        f"- \uC804\uC77C \uB300\uBE44: {daily_krw:+,.0f}\uC6D0 ({daily_pct:+.2f}%)",
        "",
        sep,
        "\U0001F4C5 \uC624\uB298",
        f"- \uAC70\uB798 {int((day or {}).get('n', 0))} | \uC2B9\uB960 {_to_float((day or {}).get('wr', 0.0), 0.0):.2f}% | \uD3C9\uADE0 {_to_float((day or {}).get('avg', 0.0), 0.0):+.2f}%",
        f"- \uC77C\uC77C \uC2E4\uD604\uC190\uC775: {daily_krw:+,.0f}\uC6D0 ({daily_pct:+.2f}%)",
        f"- \uCD5C\uB300\uC775\uC808 {_to_float((day or {}).get('max', 0.0), 0.0):+.2f}% | \uCD5C\uB300\uC190\uC808 {_to_float((day or {}).get('min', 0.0), 0.0):+.2f}%",
        f"- \uC190\uC808\uBE44\uC911 {_to_float((day or {}).get('sl_ratio', 0.0), 0.0):.2f}% | \uCD5C\uADFC10\uD3C9\uADE0 {_to_float((day or {}).get('avg10', 0.0), 0.0):+.2f}%",
        "",
        sep,
        f"\U0001F4C6 \uC774\uBC88 \uB2EC ({report_end.strftime('%Y-%m')})",
        f"- \uAC70\uB798 {int((month or {}).get('n', 0))} | \uC2B9\uB960 {_to_float((month or {}).get('wr', 0.0), 0.0):.2f}% | \uD3C9\uADE0 {_to_float((month or {}).get('avg', 0.0), 0.0):+.2f}%",
        f"- \uC6D4\uAC04 \uC2E4\uD604\uC190\uC775: {month_krw:+,.0f}\uC6D0 ({month_pct:+.2f}%)",
        f"- \uC6D4\uAC04 \uBCF5\uB9AC: {month_compound:+.2f}%",
        f"- \uC6D4\uAC04 MDD: {month_mdd_text}",
        "",
        sep,
        "\U0001F4CC \uC804\uB7B5\uBCC4 (\uC774\uBC88 \uB2EC)",
        f"- MAIN: \uAC70\uB798 {int(_to_float(main_stats.get('n', 0), 0.0))} | \uC2B9\uB960 {_to_float(main_stats.get('wr', 0.0), 0.0):.2f}% | \uD3C9\uADE0 {_to_float(main_stats.get('avg', 0.0), 0.0):+.2f}%",
        f"- SCALP_BTC: \uAC70\uB798 {int(_to_float(scalp_stats.get('n', 0), 0.0))} | \uC2B9\uB960 {_to_float(scalp_stats.get('wr', 0.0), 0.0):.2f}% | \uD3C9\uADE0 {_to_float(scalp_stats.get('avg', 0.0), 0.0):+.2f}%",
        "",
        sep,
        f"\uC0C1\uD0DC: {status_code} {status_text}",
    ]
    return "\n".join(lines)


def _count_holdings(strategy_state: dict, strategy: str) -> int:
    out = 0
    for _ticker, pos in ((strategy_state or {}).get(strategy, {}) or {}).items():
        if bool((pos or {}).get("holding", False)):
            out += 1
    return int(out)


def _safe_service_status(service_name: str = DEFAULT_SERVICE_NAME) -> str:
    cmd = ["systemctl", "is-active", str(service_name or DEFAULT_SERVICE_NAME)]
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=2.0, check=False)
        raw = str(cp.stdout or cp.stderr or "").strip().lower()
        if raw == "active":
            return "\uC2E4\uD589\uC911 (systemd)"
        if raw:
            return f"\uC911\uB2E8 (systemd:{raw})"
        return "\uC911\uB2E8 (systemd:unknown)"
    except Exception:
        return "\uC2E4\uD589\uC911 (systemd \uD655\uC778\uC2E4\uD328)"


def _entry_guard_status_text(now: dt.datetime) -> str:
    if not bool(getattr(config, "ENABLE_0900_ENTRY_GUARD", True)):
        return "OFF"
    ts = now if isinstance(now, dt.datetime) else now_kst()
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=KST)
    ts = ts.astimezone(KST)
    sh = min(23, max(0, int(getattr(config, "ENTRY_GUARD_START_HOUR", 9))))
    sm = min(59, max(0, int(getattr(config, "ENTRY_GUARD_START_MIN", 0))))
    eh = min(23, max(0, int(getattr(config, "ENTRY_GUARD_END_HOUR", 9))))
    em = min(59, max(0, int(getattr(config, "ENTRY_GUARD_END_MIN", 15))))
    start = ts.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = ts.replace(hour=eh, minute=em, second=0, microsecond=0)
    if end <= start:
        if ts < end:
            start -= dt.timedelta(days=1)
        else:
            end += dt.timedelta(days=1)
    active = bool(start <= ts < end)
    window_txt = f"{start.strftime('%H:%M')}~{end.strftime('%H:%M')}"
    return f"{'ACTIVE' if active else 'OFF'} ({window_txt})"


def _safe_krw_balance(log_line) -> str:
    try:
        access, secret = load_keys()
        upbit = pyupbit.Upbit(access, secret)
        _avail, krw = get_balance_info(upbit, "KRW")
        return f"{krw:,.0f} KRW"
    except Exception as e:
        log_line(f"[ERR] heartbeat balance calc failed: {type(e).__name__}: {e}")
        log_line(traceback.format_exc().strip())
        return "\uACC4\uC0B0 \uC2E4\uD328(\uB85C\uADF8 \uD655\uC778)"


def build_heartbeat_text(
    now: dt.datetime,
    service_status: str,
    asset_text: str,
    main_holding_cnt: int,
    scalp_holding_cnt: int,
    risk_state: dict,
):
    halted = bool((risk_state or {}).get("halted_flag", False))
    reason = str((risk_state or {}).get("halt_reason") or "").strip()
    risk_txt = f"\uC815\uC9C0({reason})" if halted and reason else ("\uC815\uC9C0" if halted else "\uC5C6\uC74C")
    guard_txt = _entry_guard_status_text(now)
    return "\n".join(
        [
            "\U0001F7E2 [\uC624\uC804 9\uC2DC \uC810\uAC80] \uBD07 \uC815\uC0C1 \uB3D9\uC791",
            f"- \uC2DC\uAC01: {now.strftime('%Y-%m-%d %H:%M')} KST",
            f"- \uC0C1\uD0DC: {service_status}",
            f"- \uC790\uC0B0: {asset_text}",
            f"- \uBCF4\uC720: MAIN {int(main_holding_cnt)} / SCALP {int(scalp_holding_cnt)}",
            f"- \uB9AC\uC2A4\uD06C\uC815\uC9C0: {risk_txt}",
            f"- \uC2E0\uADDC\uC9C4\uC785\uAC00\uB4DC: {guard_txt}",
        ]
    )


def send_heartbeat(now: dt.datetime, log_line):
    strategy_state, _, _, _, risk_state = load_state()
    main_holding_cnt = _count_holdings(strategy_state, "MAIN")
    scalp_holding_cnt = _count_holdings(strategy_state, "SCALP")
    service_status = _safe_service_status(DEFAULT_SERVICE_NAME)
    asset_text = _safe_krw_balance(log_line)
    msg = build_heartbeat_text(
        now=now,
        service_status=service_status,
        asset_text=asset_text,
        main_holding_cnt=main_holding_cnt,
        scalp_holding_cnt=scalp_holding_cnt,
        risk_state=risk_state,
    )
    ok = bool(tg_notify(event_type="HEARTBEAT", message=msg))
    if ok:
        log_line("[OK] heartbeat telegram sent")
    else:
        log_line("[WARN] heartbeat telegram send failed or queued to spool")
    return ok


def _schedule_state_path() -> str:
    path = str(getattr(config, "REPORT_SCHEDULE_STATE_FILE", "report_schedule_state.json") or "").strip()
    return path or "report_schedule_state.json"


def _kst_now(ts: dt.datetime) -> dt.datetime:
    if not isinstance(ts, dt.datetime):
        return now_kst()
    if ts.tzinfo is None:
        return ts.replace(tzinfo=KST)
    return ts.astimezone(KST)


def _target_time_kst(now: dt.datetime, hour: int, minute: int) -> dt.datetime:
    base = _kst_now(now)
    h = min(23, max(0, int(hour)))
    m = min(59, max(0, int(minute)))
    return base.replace(hour=h, minute=m, second=0, microsecond=0)


def _load_schedule_state(path: str, log_line) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        return {}
    except Exception as e:
        log_line(f"[WARN] schedule state load failed: {type(e).__name__}: {e}")
        return {}


def _save_schedule_state(path: str, data: dict, log_line):
    tmp = f"{path}.tmp"
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data or {}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        log_line(f"[WARN] schedule state save failed: {type(e).__name__}: {e}")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def _should_send_scheduled(
    now: dt.datetime,
    task_key: str,
    target_hour: int,
    target_min: int,
    window_min: int,
    force: bool,
    log_line,
):
    kst_now = _kst_now(now)
    target = _target_time_kst(kst_now, target_hour, target_min)
    if force:
        return True, target

    w_min = max(1, int(window_min))
    delta_min = (kst_now - target).total_seconds() / 60.0
    if delta_min < 0:
        log_line(
            f"[INFO] {task_key} skip: outside KST window (before target) "
            f"now={kst_now.strftime('%H:%M')} target={target.strftime('%H:%M')} window=+{w_min}m"
        )
        return False, target
    if delta_min > float(w_min):
        log_line(
            f"[INFO] {task_key} skip: outside KST window "
            f"now={kst_now.strftime('%H:%M')} target={target.strftime('%H:%M')} window=+{w_min}m"
        )
        return False, target

    path = _schedule_state_path()
    state = _load_schedule_state(path, log_line)
    day_key = target.strftime("%Y-%m-%d")
    last_key = str(state.get(task_key) or "")
    if last_key == day_key:
        log_line(f"[INFO] {task_key} skip: already sent for {day_key} KST")
        return False, target
    return True, target


def _mark_scheduled_sent(task_key: str, target: dt.datetime, log_line):
    path = _schedule_state_path()
    state = _load_schedule_state(path, log_line)
    state[str(task_key)] = str(target.strftime("%Y-%m-%d"))
    _save_schedule_state(path, state, log_line)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--xlsx", action="store_true")
    parser.add_argument("--heartbeat-only", action="store_true")
    parser.add_argument("--scheduled-report", action="store_true")
    parser.add_argument("--scheduled-heartbeat", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--schedule-window-min",
        type=int,
        default=max(1, int(getattr(config, "REPORT_SCHEDULE_WINDOW_MIN", 30))),
    )
    args = parser.parse_args()

    now = now_kst()
    report_log_path = report_log_path_for(now)

    def log_line(msg: str):
        line = f"[{now_kst().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
        print(line)
        try:
            with open(report_log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    load_telegram_env_file("/etc/default/telegram-bot")

    schedule_window_min = max(1, int(args.schedule_window_min))
    hb_hour = int(getattr(config, "HEARTBEAT_SEND_KST_HOUR", 9))
    hb_min = int(getattr(config, "HEARTBEAT_SEND_KST_MIN", 0))
    report_hour = int(getattr(config, "REPORT_SEND_KST_HOUR", 21))
    report_min = int(getattr(config, "REPORT_SEND_KST_MIN", 0))

    if args.heartbeat_only or args.scheduled_heartbeat:
        should_send, target = _should_send_scheduled(
            now=now,
            task_key="heartbeat",
            target_hour=hb_hour,
            target_min=hb_min,
            window_min=schedule_window_min,
            force=bool(args.force),
            log_line=log_line,
        )
        if not should_send:
            return
        if not has_telegram_credentials():
            log_line("[WARN] TELEGRAM_TOKEN/TELEGRAM_CHAT_ID missing; heartbeat will be queued to spool")
        hb_ok = send_heartbeat(_kst_now(now), log_line)
        if hb_ok:
            _mark_scheduled_sent("heartbeat", target, log_line)
        else:
            log_line("[WARN] heartbeat not marked as sent due telegram delivery failure")
        return

    should_send_report, report_target = _should_send_scheduled(
        now=now,
        task_key="daily_report",
        target_hour=report_hour,
        target_min=report_min,
        window_min=schedule_window_min,
        force=bool(args.force),
        log_line=log_line,
    )
    if not should_send_report:
        return

    rows_all, files = load_all_trades(".")
    day_start, report_end = report_window_21_to_21(now)
    month_start, _ = month_window(report_end)

    month_file = trade_log_path_for(report_end - dt.timedelta(seconds=1))
    if not os.path.exists(month_file):
        log_line(f"[INFO] monthly trade file missing: {month_file}")
    if not files:
        log_line("[INFO] no trade_log_YYYY-MM.csv files found")

    rows_all_no_partial = _rows_without_partials(rows_all)
    rows_daily = filter_rows(rows_all_no_partial, start=day_start, end=report_end)
    rows_month = filter_rows(rows_all_no_partial, start=month_start, end=report_end)

    day_metrics = build_metrics(rows_daily)
    month_metrics = build_metrics(rows_month)
    by_strategy = build_strategy_metrics_month(rows_month)

    snapshot = _safe_account_snapshot(log_line)
    total_equity = max(0.0, _to_float(snapshot.get("total_equity", 0.0), 0.0))
    history_path = str(getattr(config, "REPORT_EQUITY_HISTORY_PATH", EQUITY_HISTORY_JSONL))
    anchor_max_after_sec = float(getattr(config, "REPORT_ANCHOR_MAX_AFTER_SEC", EQUITY_ANCHOR_MAX_AFTER_SEC))
    equity_history = _load_equity_history(history_path, log_line=log_line)

    daily_anchor = _find_anchor_equity(equity_history, day_start, max_after_sec=anchor_max_after_sec)
    month_anchor = _find_anchor_equity(equity_history, month_start, max_after_sec=anchor_max_after_sec)
    daily_pnl_krw, daily_pnl_pct, daily_anchor_ok = _calc_snapshot_pnl(total_equity, daily_anchor)
    month_pnl_krw, month_pnl_pct, month_anchor_ok = _calc_snapshot_pnl(total_equity, month_anchor)
    if not daily_anchor_ok:
        log_line("[WARN] daily equity anchor missing; using 0 PnL for daily snapshot section")
    if not month_anchor_ok:
        log_line("[WARN] monthly equity anchor missing; using 0 PnL for monthly snapshot section")

    month_points = _build_month_equity_points(equity_history, month_start, now, total_equity)
    month_mdd_pct = _calc_mdd_from_equity_points(month_points)

    _append_equity_history(history_path, now, total_equity, log_line=log_line)

    risk_state = {}
    try:
        _, _, _, _, risk_state = load_state()
    except Exception as e:
        log_line(f"[WARN] state load failed for report status: {type(e).__name__}: {e}")
        risk_state = {}
    status_emoji = _resolve_status_emoji(daily_pnl_pct, risk_state)

    pnl_amounts = {
        "daily_krw": float(daily_pnl_krw),
        "daily_pct": float(daily_pnl_pct),
        "month_krw": float(month_pnl_krw),
        "month_pct": float(month_pnl_pct),
    }

    write_summary_csv(SUMMARY_CSV, day_metrics, month_metrics, by_strategy, month_mdd_pct=month_mdd_pct)
    log_line(f"[OK] wrote {SUMMARY_CSV}")

    if args.xlsx:
        ok, err = write_xlsx_if_requested(
            SUMMARY_XLSX,
            day_metrics,
            month_metrics,
            by_strategy,
            month_mdd_pct=month_mdd_pct,
        )
        if ok:
            log_line(f"[OK] wrote {SUMMARY_XLSX}")
        else:
            log_line(f"[WARN] xlsx skipped: {err}")

    text = build_report_text(
        report_end=report_end,
        day_start=day_start,
        day=day_metrics,
        month=month_metrics,
        by_strategy=by_strategy,
        snapshot=snapshot,
        pnl_amounts=pnl_amounts,
        month_mdd_pct=month_mdd_pct,
        status_emoji=status_emoji,
    )

    if not has_telegram_credentials():
        log_line("[WARN] TELEGRAM_TOKEN/TELEGRAM_CHAT_ID missing; report notifications will be queued to spool")

    text_ok = bool(tg_notify(event_type="DAILY_REPORT", message=text))
    if text_ok:
        log_line("[OK] telegram text sent")
        _mark_scheduled_sent("daily_report", report_target, log_line)
    else:
        log_line("[WARN] telegram text send failed or queued to spool")
        log_line("[WARN] daily_report not marked as sent due telegram delivery failure")


if __name__ == "__main__":
    main()

