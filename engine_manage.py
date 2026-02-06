# engine_manage.py
import csv
import datetime as dt
import os
import time

import config
import analyze
from risk import apply_risk_rules
from market import get_balance


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
        csv.writer(f).writerow([
            dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            side,
            ticker,
            f"{qty:.12f}",
            mode,
            "1" if ok else "0",
            message[:200],
        ])


def append_trade_log(path: str, row):
    with open(path, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(row)


def _sell_with_retry(upbit, ticker: str, qty: float, max_retry: int = 3, sleep_sec: float = 0.35):
    """
    실주문: sell_market_order 재시도 + 결과 로그
    모의: 프린트 + 성공 처리
    """
    if qty <= 0:
        return True

    if not bool(getattr(config, "REAL_ORDER", False)):
        print(f"[MOCK SELL] {ticker} qty={qty}")
        log_order("SELL", ticker, qty, True, "mock")
        return True

    last_err = ""
    for i in range(max_retry):
        try:
            resp = upbit.sell_market_order(ticker, qty)
            # 업비트 응답이 None이어도 예외는 아니지만, 보수적으로 성공으로 취급
            log_order("SELL", ticker, qty, True, f"try={i+1} resp={str(resp)[:120]}")
            return True
        except Exception as e:
            last_err = str(e)
            log_order("SELL", ticker, qty, False, f"try={i+1} err={last_err}")
            time.sleep(sleep_sec)

    return False


def manage_positions(upbit, now, state, prices, cooldown_until, save_state_fn):
    """
    보유 포지션 전체에 대해 리스크룰 적용 + 종료처리 + 성적표
    """
    for ticker, s in list(state.items()):
        if not s.get("holding", False):
            continue

        cur = prices.get(ticker)
        if cur is None:
            continue

        # ✅ 리스크룰이 호출하는 매도 함수: 재시도/로그 포함
        def sell_fn(u, t, v):
            return _sell_with_retry(u, t, v,
                                    max_retry=int(getattr(config, "ORDER_RETRY_MAX", 3)),
                                    sleep_sec=float(getattr(config, "ORDER_RETRY_SLEEP_SEC", 0.35)))

        result = apply_risk_rules(upbit, ticker, s, float(cur), sell_fn)

        # risk.py가 closed를 True로 줬다면 종료 처리
        if result.get("closed"):
            # ✅ 실주문에서는 "정말 잔고가 0에 가까운지" 최종 확인
            if bool(getattr(config, "REAL_ORDER", False)):
                coin = ticker.split("-")[1]
                vol_now = float(get_balance(upbit, coin))
                if vol_now > 0:
                    # 아직 잔고가 남아있으면 closed로 확정하면 안 됨 → 재시도 유도
                    log_order("CLOSE_CHECK", ticker, vol_now, False, "balance_remaining_after_close")
                    print(f"⚠️ CLOSE 보류: {ticker} 잔고 남음({vol_now}). 다음 루프에서 재시도.")
                    # state 유지하고 저장만
                    save_state_fn(state, cooldown_until)
                    continue

            entry = float(s.get("entry", 0))
            exit_price = float(result.get("exit_price", float(cur)))
            pnl_pct = (exit_price / entry - 1.0) * 100.0 if entry > 0 else 0.0

            cd_min = config.COOLDOWN_PROFIT_MIN if pnl_pct > 0 else config.COOLDOWN_LOSS_MIN
            cooldown_until[ticker] = now + dt.timedelta(minutes=cd_min)

            print(f"📤 CLOSE {ticker} pnl={pnl_pct:+.2f}% | cooldown={cd_min}m | reason={result.get('reason')}")

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
                ]
            )

            # 종료 상태 정리
            s["holding"] = False
            s["add_count"] = 0
            s["invested_krw"] = 0.0
            s["target_krw"] = 0.0

            save_state_fn(state, cooldown_until)

            if bool(getattr(config, "AUTO_REPORT", False)):
                analyze.maybe_generate_report(
                    trade_log_path=config.TRADE_LOG_PATH,
                    out_csv=analyze.OUT_SUMMARY_CSV,
                    out_xlsx=analyze.OUT_XLSX,
                    min_interval_sec=float(getattr(config, "AUTO_REPORT_MIN_INTERVAL_SEC", 30)),
                    quiet=bool(getattr(config, "AUTO_REPORT_QUIET", True)),
                )
