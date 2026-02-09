"""리스크 적용, 청산 처리, 주문/거래 로그 기록을 담당하는 포지션 관리 모듈."""

# engine_manage.py
import csv
import datetime as dt
import os
import time

import analyze
import config
from market import get_balance
from risk import apply_risk_rules


ORDER_LOG_PATH = getattr(config, "ORDER_LOG_PATH", "order_log.csv")


def _ensure_order_log_header(path: str):
    if os.path.exists(path):
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["time", "side", "ticker", "qty", "mode", "ok", "message"])


def log_order(side: str, ticker: str, qty: float, ok: bool, message: str):
    _ensure_order_log_header(ORDER_LOG_PATH)
    mode = "REAL" if bool(getattr(config, "REAL_ORDER", False)) else "MOCK"
    with open(ORDER_LOG_PATH, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(
            [
                dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                side,
                ticker,
                f"{qty:.12f}",
                mode,
                "1" if ok else "0",
                message[:200],
            ]
        )


def _normalize_csv_row(row) -> list:
    return [str(x) for x in list(row or [])]


def _read_last_csv_row(path: str):
    if not os.path.exists(path):
        return None
    last = None
    try:
        with open(path, "r", newline="", encoding="utf-8") as f:
            for row in csv.reader(f):
                if row:
                    last = [str(x) for x in row]
    except Exception:
        return None
    return last


def _acquire_lockfile(lock_path: str, wait_sec: float = 1.0, stale_sec: float = 30.0):
    wait_deadline = time.time() + max(0.0, float(wait_sec))
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                stamp = f"{os.getpid()} {time.time():.6f}\n"
                os.write(fd, stamp.encode("ascii", "ignore"))
            except Exception:
                pass
            return fd
        except FileExistsError:
            try:
                age = time.time() - float(os.path.getmtime(lock_path))
                if age > float(stale_sec):
                    os.remove(lock_path)
                    continue
            except FileNotFoundError:
                continue
            except Exception:
                pass
            if time.time() >= wait_deadline:
                return None
            time.sleep(0.01)
        except Exception:
            return None


def _release_lockfile(fd, lock_path: str):
    if fd is None:
        return
    try:
        os.close(fd)
    except Exception:
        pass
    try:
        os.remove(lock_path)
    except FileNotFoundError:
        pass
    except Exception:
        pass


def append_trade_log(path: str, row):
    row_norm = _normalize_csv_row(row)
    lock_path = f"{path}.lock"
    lock_fd = _acquire_lockfile(lock_path, wait_sec=1.0, stale_sec=30.0)
    try:
        last_row = _read_last_csv_row(path)
        if last_row == row_norm:
            return False
        with open(path, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(row_norm)
        return True
    finally:
        _release_lockfile(lock_fd, lock_path)


def _sell_with_retry(
    upbit,
    ticker: str,
    qty: float,
    max_retry: int = 3,
    sleep_sec: float = 0.35,
    inactive_tickers=None,
    inactive_positions=None,
):
    if qty <= 0:
        return True

    inactive_tickers = set(inactive_tickers or [])
    inactive_positions = inactive_positions or {}
    if ticker in inactive_tickers or ticker in inactive_positions:
        log_order("SELL_BLOCK", ticker, qty, False, "inactive_ticker_blocked")
        print(f"[BLOCK] inactive ticker sell blocked: {ticker}")
        return False

    if not bool(getattr(config, "REAL_ORDER", False)):
        print(f"[MOCK SELL] {ticker} qty={qty}")
        log_order("SELL", ticker, qty, True, "mock")
        return True

    for i in range(max_retry):
        try:
            resp = upbit.sell_market_order(ticker, qty)
            log_order("SELL", ticker, qty, True, f"try={i+1} resp={str(resp)[:120]}")
            return True
        except Exception as e:
            log_order("SELL", ticker, qty, False, f"try={i+1} err={str(e)}")
            time.sleep(sleep_sec)

    return False


def _calc_close_pnl_pct(state: dict, cur: float) -> float:
    realized_krw = float(state.get("realized_krw", 0.0))
    realized_cost_krw = float(state.get("realized_cost_krw", 0.0))
    if realized_cost_krw > 0:
        return (realized_krw / realized_cost_krw - 1.0) * 100.0

    entry = float(state.get("entry", 0.0))
    return (float(cur) / entry - 1.0) * 100.0 if entry > 0 else 0.0


def _is_dust_value(krw_value: float) -> bool:
    return float(krw_value) < float(getattr(config, "MIN_ORDER_KRW", 5_000))


def _calc_close_qty(state: dict, entry: float) -> float:
    realized_cost_krw = float(state.get("realized_cost_krw", 0.0))
    if realized_cost_krw > 0 and entry > 0:
        return max(0.0, realized_cost_krw / entry)
    return max(0.0, float(state.get("initial_volume", 0.0)))


def _safe_nonneg_float(value, default: float = 0.0) -> float:
    try:
        return max(0.0, float(value))
    except Exception:
        return max(0.0, float(default))


def _normalize_exit_reason(raw_reason: str) -> str:
    s = str(raw_reason or "").lower().strip()
    if ("stop" in s) or ("sl" in s):
        return "STOPLOSS"
    if "trail" in s:
        return "TRAILING"
    if ("tp2" in s) or ("take_profit" in s):
        return "TP2"
    return "FORCE_CLOSE"


def manage_positions(
    upbit,
    now,
    state,
    prices,
    cooldown_until,
    save_state_fn,
    inactive_tickers=None,
    inactive_positions=None,
    strategy: str = "MAIN",
):
    inactive_tickers = set(inactive_tickers or [])
    inactive_positions = inactive_positions or {}
    strategy = str(strategy or "MAIN").upper().strip()
    events = []

    for ticker, s in list(state.items()):
        if not s.get("holding", False):
            continue

        if ticker in inactive_tickers or ticker in inactive_positions:
            print(f"[BLOCK] inactive ticker position management skipped: {ticker}")
            continue

        cur = prices.get(ticker)
        if cur is None:
            continue

        def sell_fn(u, t, v):
            return _sell_with_retry(
                u,
                t,
                v,
                max_retry=int(getattr(config, "ORDER_RETRY_MAX", 3)),
                sleep_sec=float(getattr(config, "ORDER_RETRY_SLEEP_SEC", 0.35)),
                inactive_tickers=inactive_tickers,
                inactive_positions=inactive_positions,
            )

        result = apply_risk_rules(
            upbit,
            ticker,
            s,
            float(cur),
            sell_fn,
            now=now,
            strategy_tag=strategy,
        )

        if result.get("closed"):
            if bool(getattr(config, "REAL_ORDER", False)):
                coin = ticker.split("-")[1]
                vol_now = float(get_balance(upbit, coin))
                if vol_now > 0:
                    remain_value = vol_now * float(cur)
                    dust_ok = bool(getattr(config, "DUST_CLOSE_AS_CLOSED", True)) and _is_dust_value(remain_value)

                    if not dust_ok:
                        # Recover state if close failed but balance still exists.
                        s["holding"] = True
                        log_order("CLOSE_CHECK", ticker, vol_now, False, "balance_remaining_after_close")
                        print(
                            f"[WARN] CLOSE postponed: {ticker} balance remains ({vol_now}). retry next loop"
                        )
                        save_state_fn(state, cooldown_until)
                        continue

                    log_order("CLOSE_CHECK", ticker, vol_now, True, "balance_dust_after_close")

            entry = float(s.get("entry", 0.0))
            exit_price = float(result.get("exit_price", float(cur)))
            total_buy_krw = _safe_nonneg_float(s.get("total_buy_krw", s.get("invested_krw", 0.0)), 0.0)
            total_sell_krw = _safe_nonneg_float(s.get("total_sell_krw", s.get("realized_krw", 0.0)), 0.0)
            if total_buy_krw <= 0:
                total_buy_krw = _safe_nonneg_float(s.get("realized_cost_krw", 0.0), 0.0)
            if total_sell_krw <= 0:
                total_sell_krw = _safe_nonneg_float(s.get("realized_krw", 0.0), 0.0)
            realized_krw = float(total_sell_krw) - float(total_buy_krw)
            if total_buy_krw > 0:
                pnl_pct = (float(realized_krw) / float(total_buy_krw)) * 100.0
            else:
                pnl_pct = _calc_close_pnl_pct(s, float(cur))
            close_qty = _safe_nonneg_float(result.get("close_qty", 0.0), 0.0)
            if close_qty <= 0:
                close_qty = _calc_close_qty(s, entry)
            last_exit_reason = str(s.get("last_exit_reason") or "")
            if not last_exit_reason:
                last_exit_reason = _normalize_exit_reason(result.get("reason", ""))
            s["last_exit_reason"] = str(last_exit_reason)
            strategy_tag = str(s.get("strategy_tag") or strategy).upper().strip() or str(strategy).upper().strip()

            cd_min = config.COOLDOWN_PROFIT_MIN if pnl_pct > 0 else config.COOLDOWN_LOSS_MIN
            cooldown_until[ticker] = now + dt.timedelta(minutes=cd_min)

            print(
                f"[CLOSE] {ticker} pnl={pnl_pct:+.2f}% | cooldown={cd_min}m | reason={result.get('reason')}"
            )

            append_trade_log(
                config.TRADE_LOG_PATH,
                [
                    now.strftime("%Y-%m-%d %H:%M:%S"),
                    ticker,
                    f"{entry:.6f}",
                    f"{exit_price:.6f}",
                    f"{pnl_pct:.2f}",
                    result.get("reason", ""),
                    s.get("regime", ""),
                    strategy,
                ],
            )
            events.append(
                {
                    "time": now,
                    "ticker": ticker,
                    "qty": float(close_qty),
                    "entry_price": float(entry),
                    "exit_price": float(exit_price),
                    "pnl_pct": float(pnl_pct),
                    "reason": str(result.get("reason", "")),
                    "strategy": strategy,
                    "strategy_tag": strategy_tag,
                    "total_buy_krw": float(total_buy_krw),
                    "total_sell_krw": float(total_sell_krw),
                    "realized_krw": float(realized_krw),
                    "realized_pct": float(pnl_pct),
                    "last_exit_reason": str(last_exit_reason),
                }
            )

            s["holding"] = False
            s["add_count"] = 0
            s["invested_krw"] = 0.0
            s["target_krw"] = 0.0
            s["initial_volume"] = 0.0
            s["tp1"] = False
            s["tp2"] = False
            s["realized_krw"] = 0.0
            s["realized_cost_krw"] = 0.0
            s["total_buy_krw"] = 0.0
            s["total_sell_krw"] = 0.0
            s["last_exit_reason"] = ""
            s["strategy_tag"] = strategy
            s["entry_ts"] = 0.0
            s["trail_armed"] = False
            s["trail_hwm"] = 0.0

            save_state_fn(state, cooldown_until)

            if bool(getattr(config, "AUTO_REPORT", False)):
                analyze.maybe_generate_report(
                    trade_log_path=config.TRADE_LOG_PATH,
                    out_csv=analyze.OUT_SUMMARY_CSV,
                    out_xlsx=analyze.OUT_XLSX,
                    min_interval_sec=float(getattr(config, "AUTO_REPORT_MIN_INTERVAL_SEC", 30)),
                    quiet=bool(getattr(config, "AUTO_REPORT_QUIET", True)),
                )
    return events
