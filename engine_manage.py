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


def append_trade_log(path: str, row):
    with open(path, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(row)


def _sell_with_retry(upbit, ticker: str, qty: float, max_retry: int = 3, sleep_sec: float = 0.35):
    if qty <= 0:
        return True

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


def manage_positions(upbit, now, state, prices, cooldown_until, save_state_fn):
    for ticker, s in list(state.items()):
        if not s.get("holding", False):
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
            )

        result = apply_risk_rules(upbit, ticker, s, float(cur), sell_fn)

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
            pnl_pct = _calc_close_pnl_pct(s, float(cur))

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
                ],
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

            save_state_fn(state, cooldown_until)

            if bool(getattr(config, "AUTO_REPORT", False)):
                analyze.maybe_generate_report(
                    trade_log_path=config.TRADE_LOG_PATH,
                    out_csv=analyze.OUT_SUMMARY_CSV,
                    out_xlsx=analyze.OUT_XLSX,
                    min_interval_sec=float(getattr(config, "AUTO_REPORT_MIN_INTERVAL_SEC", 30)),
                    quiet=bool(getattr(config, "AUTO_REPORT_QUIET", True)),
                )
