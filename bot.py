import time
import datetime as dt
import csv
import os
import json
from typing import Tuple

import pyupbit
import config
from indicators import check_filters, intraday_trend_ok, get_market_regime
from market import load_keys, get_balance, get_top_tickers_by_value
from strategy import calc_target, build_k_map
from risk import apply_risk_rules


def now_kst():
    return dt.datetime.now()


def ensure_trade_log_header(path: str):
    if os.path.exists(path):
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["time", "ticker", "entry_price", "exit_price", "pnl_pct", "reason", "regime"])


def append_trade_log(path: str, row):
    with open(path, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(row)


# -----------------------
# 상태 저장/복구
# -----------------------
def save_state(state: dict, cooldown_until: dict):
    try:
        payload = {
            "state": state,
            "cooldown_until": {k: v.isoformat() for k, v in cooldown_until.items()},
            "saved_at": now_kst().isoformat(),
        }
        with open(config.STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ state 저장 실패: {e}")


def load_state():
    if not os.path.exists(config.STATE_FILE):
        return {}, {}
    try:
        with open(config.STATE_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
        state = payload.get("state", {}) or {}
        cd_raw = payload.get("cooldown_until", {}) or {}
        cooldown_until = {}
        for k, v in cd_raw.items():
            try:
                cooldown_until[k] = dt.datetime.fromisoformat(v)
            except Exception:
                pass
        print(f"📂 state 복구: {len(state)}개")
        return state, cooldown_until
    except Exception as e:
        print(f"⚠️ state 복구 실패: {e}")
        return {}, {}


def verify_state_with_balance(upbit, state: dict):
    """
    복구된 state가 실제 잔고와 맞는지 검증.
    - holding=True인데 잔고가 0이면 holding False로 정리
    - initial_volume 없거나 0이면 현재 잔고로 보정
    """
    fixed = 0
    for ticker, s in list(state.items()):
        if not s.get("holding"):
            continue
        coin = ticker.split("-")[1]
        vol = float(get_balance(upbit, coin))
        if vol <= 0:
            s["holding"] = False
            fixed += 1
            continue
        if float(s.get("initial_volume", 0.0)) <= 0:
            s["initial_volume"] = vol
            fixed += 1
        if float(s.get("entry", 0.0)) <= 0:
            # entry가 비어있으면 일단 0으로 두지 말고, 현재가/평단은 아래에서 다시 보정 가능
            fixed += 1
    if fixed:
        print(f"🧹 state 보정 {fixed}건(잔고기반)")


# -----------------------
# 가격/체결 헬퍼
# -----------------------
def batch_get_prices(tickers):
    """
    pyupbit.get_current_price(list)-> dict
    실패 시 빈 dict
    """
    try:
        data = pyupbit.get_current_price(list(tickers))
        if data is None:
            return {}
        if isinstance(data, dict):
            return {k: float(v) for k, v in data.items() if v is not None}
        return {}
    except Exception:
        return {}


def get_coin_snapshot_from_balances(upbit, coin: str) -> Tuple[float, float]:
    """
    returns: (balance, avg_buy_price)
    """
    try:
        bals = upbit.get_balances()
        if not bals:
            return 0.0, 0.0
        for b in bals:
            if b.get("currency") == coin:
                bal = float(b.get("balance") or 0.0)
                avg = float(b.get("avg_buy_price") or 0.0)
                return bal, avg
    except Exception:
        pass
    return 0.0, 0.0


def wait_for_filled_snapshot(upbit, ticker: str, timeout_sec: float = 3.0, interval: float = 0.2) -> Tuple[float, float]:
    """
    실주문 직후:
      - 코인 잔고(balance)
      - 평균매수가(avg_buy_price)
    둘 다 유효해질 때까지 대기 후 반환
    """
    coin = ticker.split("-")[1]
    deadline = time.time() + timeout_sec
    last_bal, last_avg = 0.0, 0.0

    while time.time() < deadline:
        bal, avg = get_coin_snapshot_from_balances(upbit, coin)
        last_bal, last_avg = bal, avg
        if bal > 0 and avg > 0:
            return bal, avg
        time.sleep(interval)

    return float(last_bal), float(last_avg)


# -----------------------
# 포지션/시장 로직
# -----------------------
def estimate_equity(krw: float, state: dict, prices: dict, upbit) -> float:
    equity = float(krw)
    for ticker, s in state.items():
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


def get_base_position_settings(equity):
    if equity <= config.TEST_EQUITY_CAP:
        return float(config.TEST_PER_TRADE_KRW), int(config.TEST_MAX_HOLDINGS)

    tier = config.ACCOUNT_TIERS[0]
    for t in config.ACCOUNT_TIERS:
        if equity >= t["min_equity"]:
            tier = t

    max_holdings = int(tier["max_holdings"])
    per_trade_amt = float(equity) / max_holdings
    return per_trade_amt, max_holdings


def apply_market_regime(equity, base_per_trade, base_max_holdings, regime: str):
    invest_frac = config.REGIME_INVEST_FRAC.get(regime, 0.7)
    holdings_mult = config.REGIME_HOLDINGS_MULT.get(regime, 0.7)

    if invest_frac <= 0 or holdings_mult <= 0:
        return 0.0, 0  # HALT

    eff_max_holdings = max(1, int(base_max_holdings * holdings_mult))
    total_invest_budget = equity * invest_frac
    per_trade_amt = total_invest_budget / eff_max_holdings
    per_trade_amt = max(float(config.MIN_ORDER_KRW), per_trade_amt)
    return float(per_trade_amt), int(eff_max_holdings)


def run():
    access, secret = load_keys()
    upbit = pyupbit.Upbit(access, secret)

    # 실주문 확인 프롬프트(안전장치)
    if bool(getattr(config, "REAL_ORDER", False)) and bool(getattr(config, "REQUIRE_ORDER_CONFIRM", False)):
        print("🚨 REAL_ORDER=True (실주문 모드)")
        ans = input("정말 실전 매매를 시작할까요? 진행하려면 'yes' 입력: ").strip().lower()
        if ans != "yes":
            print("❌ 취소됨")
            return

    ensure_trade_log_header(config.TRADE_LOG_PATH)

    # state 복구
    state, cooldown_until = load_state()
    verify_state_with_balance(upbit, state)

    universe = get_top_tickers_by_value(config.TOP_N)
    k_map = build_k_map(universe)
    last_refresh = now_kst()

    last_status = now_kst()
    last_state_save = now_kst()

    # 필터 캐시: (ok, updated_at)
    day_cache = {}
    intraday_cache = {}

    print("🤖 Bot start | REAL_ORDER=", config.REAL_ORDER)

    while True:
        try:
            now = now_kst()

            # 유니버스 갱신
            if (now - last_refresh).total_seconds() >= config.REFRESH_MIN * 60:
                print("\n🔄 Refresh universe + K map")
                universe = get_top_tickers_by_value(config.TOP_N)
                k_map = build_k_map(universe)
                last_refresh = now

            # 시장 컨디션
            regime = "FULL"
            if config.USE_MARKET_REGIME:
                try:
                    regime = get_market_regime()
                except Exception:
                    regime = "MID"

            holding_tickers = [t for t, s in state.items() if s.get("holding")]
            price_targets = set(universe) | set(holding_tickers)
            prices = batch_get_prices(price_targets)

            krw = float(get_balance(upbit, "KRW"))
            equity = estimate_equity(krw, state, prices, upbit)

            base_per_trade, base_max_holdings = get_base_position_settings(equity)
            per_trade_amt, max_holdings = apply_market_regime(equity, base_per_trade, base_max_holdings, regime)

            holding_cnt = sum(1 for s in state.values() if s.get("holding"))

            if (now - last_status).total_seconds() >= config.STATUS_PRINT_SEC:
                print(
                    f"📊 Regime={regime} | Equity≈{equity:,.0f} | "
                    f"PerTrade≈{per_trade_amt:,.0f} | Holding={holding_cnt}/{max_holdings}"
                )
                last_status = now

            allow_entry = (max_holdings > 0 and per_trade_amt > 0)

            # ✅ 신규 진입
            if allow_entry:
                for ticker in universe:
                    if holding_cnt >= max_holdings:
                        break
                    if state.get(ticker, {}).get("holding", False):
                        continue
                    if krw < per_trade_amt:
                        continue

                    until = cooldown_until.get(ticker)
                    if until is not None and now < until:
                        continue

                    # --- 일봉 필터 캐시 ---
                    cached = day_cache.get(ticker)
                    if cached and (now - cached[1]).total_seconds() < config.DAY_FILTER_CACHE_SEC:
                        ok = cached[0]
                    else:
                        ok = bool(check_filters(ticker))
                        day_cache[ticker] = (ok, now)
                    if not ok:
                        continue

                    # --- 시간봉 보조 필터 캐시 ---
                    if config.USE_INTRADAY_FILTER:
                        cached2 = intraday_cache.get(ticker)
                        if cached2 and (now - cached2[1]).total_seconds() < config.INTRADAY_FILTER_CACHE_SEC:
                            ok2 = cached2[0]
                        else:
                            try:
                                ok2 = bool(intraday_trend_ok(ticker))
                            except Exception:
                                ok2 = True
                            intraday_cache[ticker] = (ok2, now)
                        if not ok2:
                            continue

                    cur = prices.get(ticker)
                    if cur is None:
                        continue

                    k = float(k_map.get(ticker, config.K_DEFAULT))
                    try:
                        target = float(calc_target(ticker, k))
                    except Exception:
                        continue

                    if float(cur) >= target:
                        print(f"💰 BUY {ticker} | Regime={regime} | KRW={per_trade_amt:,.0f}")

                        if config.REAL_ORDER:
                            upbit.buy_market_order(ticker, per_trade_amt)

                            # ✅ 체결/잔고 반영 기다려: 수량 + 평균매수가 확보
                            filled_vol, avg_buy = wait_for_filled_snapshot(upbit, ticker, timeout_sec=3.0, interval=0.2)

                            initial_vol = float(filled_vol) if filled_vol > 0 else (float(per_trade_amt) / float(cur))
                            entry_price = float(avg_buy) if avg_buy > 0 else float(cur)
                        else:
                            initial_vol = float(per_trade_amt) / float(cur)
                            entry_price = float(cur)

                        state[ticker] = {
                            "holding": True,
                            "entry": float(entry_price),  # ✅ 평균매수가 기반
                            "peak": float(cur),
                            "tp1": False,
                            "tp2": False,
                            "regime": regime,
                            "initial_volume": float(initial_vol),
                        }

                        holding_cnt += 1
                        krw = float(get_balance(upbit, "KRW"))

                        # 즉시 저장
                        save_state(state, cooldown_until)

                        time.sleep(0.15)

            # ✅ 포지션 관리(항상)
            for ticker, s in list(state.items()):
                if not s.get("holding", False):
                    continue

                cur = prices.get(ticker)
                if cur is None:
                    continue

                def sell_fn(u, t, v):
                    if v <= 0:
                        return
                    try:
                        if config.REAL_ORDER:
                            return u.sell_market_order(t, v)
                        print(f"[MOCK SELL] {t} qty={v}")
                    except Exception as e:
                        print(f"⚠️ sell 실패: {t} qty={v} err={e}")

                result = apply_risk_rules(upbit, ticker, s, float(cur), sell_fn)

                if result.get("closed"):
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

                    s["holding"] = False

                    # 종료 즉시 저장
                    save_state(state, cooldown_until)

            # 주기 저장
            if (now - last_state_save).total_seconds() >= float(config.STATE_SAVE_INTERVAL_SEC):
                save_state(state, cooldown_until)
                last_state_save = now

            time.sleep(config.POLL_SEC)

        except KeyboardInterrupt:
            print("\n사용자 종료(Ctrl+C)")
            save_state(state, cooldown_until)
            break
        except Exception as e:
            print("에러:", e)
            time.sleep(1)
