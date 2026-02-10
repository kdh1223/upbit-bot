"""Send daily report (21:00 KST) and optional 09:00 heartbeat to Telegram."""

import argparse
import csv
import datetime as dt
import json
import os
import subprocess
import traceback
from typing import Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

import pyupbit

import config
from market import get_balance, load_keys
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


def _compound_return_pct(rows: List[Dict[str, str]]) -> float:
    rows = [r for r in rows if not _is_partial_reason(r.get("reason", ""))]
    mult = 1.0
    for row in rows:
        pnl = _to_float(row.get("pnl_pct", 0.0), 0.0)
        mult *= (1.0 + pnl / 100.0)
    return (mult - 1.0) * 100.0


def build_metrics(rows: List[Dict[str, str]]):
    rows = [r for r in rows if not _is_partial_reason(r.get("reason", ""))]
    n = len(rows)
    pnls = [_to_float(row.get("pnl_pct", 0.0), 0.0) for row in rows]
    wins = sum(1 for x in pnls if x > 0)
    wr = (wins / n * 100.0) if n > 0 else 0.0
    avg = (sum(pnls) / n) if n > 0 else 0.0
    cum = _compound_return_pct(rows) if n > 0 else 0.0
    max_pnl = max(pnls) if pnls else 0.0
    min_pnl = min(pnls) if pnls else 0.0
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
        "coin_value": 0.0,
        "total_equity": 0.0,
        "has_coin": False,
    }
    try:
        access, secret = load_keys()
        upbit = pyupbit.Upbit(access, secret)
        krw_balance = float(get_balance(upbit, "KRW"))
        snapshot["krw_balance"] = float(max(0.0, krw_balance))

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
    return out


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
        return "🔴"
    if _to_float(daily_pnl_pct, 0.0) < 0:
        return "🟡"
    return "🟢"


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
    has_coin = bool((snapshot or {}).get("has_coin", False))

    coin_value_text = f"{coin_value:,.0f}원"
    if not has_coin:
        coin_value_text = "0원 (보유 없음)"
    month_mdd_text = f"{_to_float(month_mdd_pct, 0.0):.2f}%" if month_mdd_pct is not None else "N/A"

    main_stats = by_strategy.get("MAIN", {"n": 0, "wr": 0.0, "avg": 0.0})
    scalp_stats = by_strategy.get("SCALP_BTC", {"n": 0, "wr": 0.0, "avg": 0.0})

    lines = [
        f"📊 일일 성적 리포트 (KST) | {report_end.strftime('%Y-%m-%d 21:00')}",
        f"기간: {day_start.strftime('%m/%d 21:00')} ~ {report_end.strftime('%m/%d 21:00')}",
        "",
        "🏦 계좌 스냅샷",
        f"- KRW 잔고: {krw_balance:,.0f}원",
        f"- 코인 평가금: {coin_value_text}",
        f"- 총자산: {total_equity:,.0f}원",
        "",
        "📅 오늘",
        f"- 거래 {int(day.get('n', 0))} | 승률 {_to_float(day.get('wr', 0.0), 0.0):.2f}% | 평균 {_to_float(day.get('avg', 0.0), 0.0):+.2f}%",
        f"- 일일 손익: {_to_float(pnl_amounts.get('daily_krw', 0.0), 0.0):+,.0f}원 ({_to_float(pnl_amounts.get('daily_pct', 0.0), 0.0):+.2f}%)",
        f"- 최대익절 {_to_float(day.get('max', 0.0), 0.0):+.2f}% | 최대손절 {_to_float(day.get('min', 0.0), 0.0):+.2f}%",
        f"- 손절비중 {_to_float(day.get('sl_ratio', 0.0), 0.0):.2f}% | 최근10평균 {_to_float(day.get('avg10', 0.0), 0.0):+.2f}%",
        "",
        f"📆 이번 달 ({report_end.strftime('%Y-%m')})",
        f"- 거래 {int(month.get('n', 0))} | 승률 {_to_float(month.get('wr', 0.0), 0.0):.2f}% | 평균 {_to_float(month.get('avg', 0.0), 0.0):+.2f}%",
        f"- 월간 손익: {_to_float(pnl_amounts.get('month_krw', 0.0), 0.0):+,.0f}원 ({_to_float(pnl_amounts.get('month_pct', 0.0), 0.0):+.2f}%)",
        f"- 월간 MDD: {month_mdd_text}",
        "",
        "📌 전략별 (이번 달)",
        f"- MAIN: 거래 {int(main_stats.get('n', 0))} | 승률 {_to_float(main_stats.get('wr', 0.0), 0.0):.2f}% | 평균 {_to_float(main_stats.get('avg', 0.0), 0.0):+.2f}%",
        f"- SCALP_BTC: 거래 {int(scalp_stats.get('n', 0))}",
        "",
        f"상태: {str(status_emoji or '🟢')}",
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
        krw = float(get_balance(upbit, "KRW"))
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--xlsx", action="store_true")
    parser.add_argument("--heartbeat-only", action="store_true")
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

    if args.heartbeat_only:
        if not has_telegram_credentials():
            log_line("[WARN] TELEGRAM_TOKEN/TELEGRAM_CHAT_ID missing; heartbeat will be queued to spool")
        send_heartbeat(now, log_line)
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
    else:
        log_line("[WARN] telegram text send failed or queued to spool")


if __name__ == "__main__":
    main()
