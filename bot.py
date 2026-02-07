# bot.py
import time
import datetime as dt
import csv
import os

import pyupbit
import config

from market import load_keys, get_balance, get_top_tickers_by_value
from strategy import build_k_map
from indicators import get_market_regime, minute_test_signal

from state_store import load_state, save_state, verify_state_with_balance
from order_utils import wait_for_filled_snapshot
from engine_entry import try_entries  # MAIN 모드에서만 사용
from engine_manage import manage_positions

import position_manager


def ensure_trade_log_header(path: str):
    expected = ["time", "ticker", "entry_price", "exit_price", "pnl_pct", "reason", "regime"]

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

    # Backward compatibility: old header without "regime" column.
    legacy = expected[:-1]
    if header == legacy:
        migrated = [expected]
        for row in rows[1:]:
            fixed = row[: len(legacy)]
            while len(fixed) < len(legacy):
                fixed.append("")
            fixed.append("")
            migrated.append(fixed)

        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(migrated)
        print(f"[MIGRATE] {path} header updated: added 'regime' column")


def now_kst():
    return dt.datetime.now()


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


def try_minute_test_entries(
    upbit,
    now,
    universe,
    prices,
    state,
    cooldown_until,
    max_holdings,
    holding_cnt,
    regime,
    wait_for_filled_snapshot_fn,
    save_state_fn,
):
    """
    TEST 모드 전용:
    - 일봉/4시간/돌파 로직/필터 전혀 안 씀
    - indicators.minute_test_signal()만 보고 진입
    - 쿨다운/보유수/잔고 체크만 적용
    """
    if not bool(getattr(config, "USE_MINUTE_TEST_STRATEGY", False)):
        return False

    test_krw = float(getattr(config, "MINUTE_TEST_PER_TRADE_KRW", 10_000))
    if test_krw <= 0:
        return False

    if holding_cnt >= max_holdings:
        return False

    if prices.get("_krw", 0.0) < test_krw:
        return False

    for ticker in universe:
        # 이미 보유면 스킵 (TEST는 분할매수 안 함)
        holding = state.get(ticker, {}).get("holding", False)
        if holding:
            continue

        # 쿨다운
        until = cooldown_until.get(ticker)
        if until is not None and now < until:
            continue

        cur = prices.get(ticker)
        if cur is None or cur <= 0:
            continue

        # 신호
        try:
            sig = bool(minute_test_signal(ticker))
        except Exception:
            sig = False
        if not sig:
            continue

        print(f"[TEST ENTRY] BUY {ticker} | Regime={regime} | KRW={test_krw:,.0f}")

        if config.REAL_ORDER:
            upbit.buy_market_order(ticker, test_krw)
            filled_vol, avg_buy = wait_for_filled_snapshot_fn(upbit, ticker, timeout_sec=3.0, interval=0.2)
            initial_vol = float(filled_vol) if filled_vol > 0 else (test_krw / float(cur))
            entry_price = float(avg_buy) if avg_buy > 0 else float(cur)
        else:
            initial_vol = test_krw / float(cur)
            entry_price = float(cur)

        state[ticker] = position_manager.init_position_state(
            float(entry_price),
            float(initial_vol),
            float(test_krw),
            regime,
        )
        save_state_fn(state, cooldown_until)
        time.sleep(0.15)
        return True

    return False


def run():
    BOT_MODE = str(getattr(config, "BOT_MODE", "MAIN")).upper().strip()

    access, secret = load_keys()
    upbit = pyupbit.Upbit(access, secret)

    # 실주문 확인 프롬프트(안전장치)
    if bool(getattr(config, "REAL_ORDER", False)) and bool(getattr(config, "REQUIRE_ORDER_CONFIRM", False)):
        print("[WARN] REAL_ORDER=True (live order mode)")
        ans = input("정말 실전 매매를 시작할까요? 진행하려면 'yes' 입력: ").strip().lower()
        if ans != "yes":
            print("[STOP] canceled")
            return

    ensure_trade_log_header(config.TRADE_LOG_PATH)

    # state 복구
    state, cooldown_until = load_state()
    verify_state_with_balance(upbit, state)

    # 유니버스/ K맵
    universe = get_top_tickers_by_value(config.TOP_N)

    # MAIN에서만 K맵 필요(돌파 진입)
    k_map = build_k_map(universe) if BOT_MODE == "MAIN" else {}
    last_refresh = now_kst()

    last_status = now_kst()
    last_state_save = now_kst()

    # 필터 캐시들 (MAIN 엔진에서만 사용)
    day_cache = {}
    intraday_cache = {}
    minute_cache = {}

    print(f"[BOT] start | MODE={BOT_MODE} | REAL_ORDER={config.REAL_ORDER}")

    while True:
        try:
            now = now_kst()

            # 유니버스 갱신
            if (now - last_refresh).total_seconds() >= config.REFRESH_MIN * 60:
                print("\n[REFRESH] universe" + (" + K map" if BOT_MODE == "MAIN" else ""))
                universe = get_top_tickers_by_value(config.TOP_N)
                k_map = build_k_map(universe) if BOT_MODE == "MAIN" else {}
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
            prices["_krw"] = krw
            prices["_caches"] = (day_cache, intraday_cache, minute_cache)

            equity = estimate_equity(krw, state, prices, upbit)

            base_per_trade, base_max_holdings = get_base_position_settings(equity)
            per_trade_amt, max_holdings = apply_market_regime(equity, base_per_trade, base_max_holdings, regime)
            holding_cnt = sum(1 for s in state.values() if s.get("holding"))

            if (now - last_status).total_seconds() >= config.STATUS_PRINT_SEC:
                print(
                    f"[STATUS] Regime={regime} | Equity~{equity:,.0f} | "
                    f"PerTrade~{per_trade_amt:,.0f} | Holding={holding_cnt}/{max_holdings}"
                )
                last_status = now

            # 신규 진입
            if max_holdings > 0:

                if BOT_MODE == "TEST":
                    # TEST 모드는 per_trade_amt(메인 예산) 안 씀. MINUTE_TEST_PER_TRADE_KRW만 씀.
                    _ = try_minute_test_entries(
                        upbit=upbit,
                        now=now,
                        universe=universe,
                        prices=prices,
                        state=state,
                        cooldown_until=cooldown_until,
                        max_holdings=max_holdings,
                        holding_cnt=holding_cnt,
                        regime=regime,
                        wait_for_filled_snapshot_fn=wait_for_filled_snapshot,
                        save_state_fn=save_state,
                    )

                else:
                    # MAIN 모드: 기존 엔진 사용 (일봉 + 4시간 + 분봉타이밍 + 돌파)
                    if per_trade_amt > 0:
                        _ = try_entries(
                            upbit=upbit,
                            now=now,
                            universe=universe,
                            prices=prices,
                            k_map=k_map,
                            state=state,
                            cooldown_until=cooldown_until,
                            per_trade_amt=per_trade_amt,
                            max_holdings=max_holdings,
                            holding_cnt=holding_cnt,
                            regime=regime,
                            wait_for_filled_snapshot_fn=wait_for_filled_snapshot,
                            save_state_fn=save_state,
                        )

            # 포지션 관리(항상)
            manage_positions(
                upbit,
                now,
                state,
                prices,
                cooldown_until,
                save_state_fn=save_state,
            )

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


if __name__ == "__main__":
    run()
