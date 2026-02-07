# bot.py
import time
import datetime as dt
import csv
import os
from collections import Counter

import pyupbit
import config

from market import (
    load_keys,
    get_balance,
    get_top_tickers_by_value,
    get_upbit_krw_markets,
    filter_tradeable_tickers,
    sanitize_positions,
)
from strategy import build_k_map
from indicators import get_market_regime, minute_test_signal, detect_momentum_candidate

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


def estimate_equity(krw: float, state: dict, prices: dict, upbit, inactive_positions: dict = None) -> float:
    equity = float(krw)
    for positions in (state, inactive_positions or {}):
        for ticker, s in positions.items():
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


def _dedupe_keep_order(items):
    out = []
    seen = set()
    for x in items:
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out


def get_slot_limits(max_holdings: int):
    m = max(0, int(max_holdings))
    if m <= 2:
        return m, 0
    return m - 1, 1


def count_slot_usage(state: dict, top_universe):
    top_set = set(top_universe or [])
    top_cnt = 0
    momentum_cnt = 0
    for ticker, s in state.items():
        if not s.get("holding", False):
            continue
        if ticker in top_set:
            top_cnt += 1
        else:
            momentum_cnt += 1
    return int(top_cnt), int(momentum_cnt)


def build_entry_universe(
    state: dict,
    top_universe,
    momentum_candidates,
    max_holdings: int,
    now,
    momentum_block_until: dict,
):
    top_universe = _dedupe_keep_order(top_universe or [])
    top_set = set(top_universe)
    momentum_candidates = [t for t in _dedupe_keep_order(momentum_candidates or []) if t not in top_set]

    filtered_momentum = []
    for ticker in momentum_candidates:
        until = momentum_block_until.get(ticker)
        if until is not None and now < until:
            continue
        filtered_momentum.append(ticker)

    top_limit, momentum_limit = get_slot_limits(max_holdings)
    top_holdings, momentum_holdings = count_slot_usage(state, top_universe)
    allow_new_top = top_holdings < top_limit
    allow_new_momentum = (max_holdings >= 3) and (momentum_holdings < momentum_limit)

    holding_tickers = [t for t, s in state.items() if s.get("holding", False)]
    entry_list = []
    entry_list.extend(holding_tickers)  # allow ADD logic for existing holdings in MAIN mode
    if allow_new_top:
        entry_list.extend(top_universe)
    if allow_new_momentum:
        entry_list.extend(filtered_momentum)

    return _dedupe_keep_order(entry_list), top_limit, momentum_limit, top_holdings, momentum_holdings


def build_rotation_universe(raw_ranked, market_info, momentum_seen_at: dict, now):
    ranked = _dedupe_keep_order(raw_ranked or [])
    top_n = int(getattr(config, "TOP_N", 10))
    scan_n = int(getattr(config, "UNIVERSE_SCAN_N", 40))
    scan_n = max(top_n, scan_n)
    ranked = ranked[:scan_n]

    raw_top = ranked[:top_n]
    raw_outside = ranked[top_n:]

    top_active, top_inactive, top_reasons = filter_tradeable_tickers(raw_top, market_info)
    outside_active, outside_inactive, outside_reasons = filter_tradeable_tickers(raw_outside, market_info)

    for ticker in outside_active:
        try:
            if detect_momentum_candidate(ticker):
                momentum_seen_at[ticker] = now
        except Exception:
            pass

    keep_after = now - dt.timedelta(minutes=15)
    for ticker, seen_at in list(momentum_seen_at.items()):
        if not isinstance(seen_at, dt.datetime):
            momentum_seen_at.pop(ticker, None)
            continue
        if seen_at < keep_after:
            momentum_seen_at.pop(ticker, None)

    momentum_candidates = [t for t in outside_active if t in momentum_seen_at]

    inactive_all = _dedupe_keep_order(top_inactive + outside_inactive)
    reason_all = {}
    reason_all.update(top_reasons)
    reason_all.update(outside_reasons)

    return top_active, momentum_candidates, inactive_all, reason_all, momentum_seen_at


def read_new_trade_rows(path: str, consumed_rows: int):
    if not os.path.exists(path):
        return [], max(0, int(consumed_rows))

    try:
        with open(path, "r", newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
    except Exception:
        return [], max(0, int(consumed_rows))

    if not rows:
        return [], 0

    data = rows[1:]
    idx = max(0, int(consumed_rows))
    if idx > len(data):
        idx = len(data)
    return data[idx:], len(data)


def update_momentum_stoploss_block(new_rows, now, momentum_entry_tickers: set, momentum_stoploss_until: dict):
    for row in new_rows:
        if len(row) < 6:
            continue
        ticker = str(row[1]).strip()
        reason = str(row[5]).strip()
        if reason != "stop_loss":
            continue
        if ticker not in momentum_entry_tickers:
            continue
        momentum_stoploss_until[ticker] = now + dt.timedelta(minutes=30)
        print(f"[MOMENTUM BLOCK] {ticker} 30m (stop_loss)")


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
    top_universe,
    momentum_candidates,
    prices,
    state,
    cooldown_until,
    max_holdings,
    holding_cnt,
    regime,
    wait_for_filled_snapshot_fn,
    save_state_fn,
    momentum_block_until=None,
    momentum_entry_tickers=None,
    inactive_tickers=None,
    inactive_positions=None,
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

    top_universe = _dedupe_keep_order(top_universe or [])
    top_set = set(top_universe)
    momentum_candidates = [t for t in _dedupe_keep_order(momentum_candidates or []) if t not in top_set]
    momentum_block_until = momentum_block_until or {}
    momentum_entry_tickers = momentum_entry_tickers if isinstance(momentum_entry_tickers, set) else set()

    filtered_momentum = []
    for ticker in momentum_candidates:
        until = momentum_block_until.get(ticker)
        if until is not None and now < until:
            continue
        filtered_momentum.append(ticker)

    top_limit, momentum_limit = get_slot_limits(max_holdings)
    top_holdings, momentum_holdings = count_slot_usage(state, top_universe)
    allow_new_top = top_holdings < top_limit
    allow_new_momentum = (max_holdings >= 3) and (momentum_holdings < momentum_limit)
    if not allow_new_top and not allow_new_momentum:
        return False

    entry_candidates = []
    if allow_new_top:
        entry_candidates.extend(top_universe)
    if allow_new_momentum:
        entry_candidates.extend(filtered_momentum)

    inactive_tickers = set(inactive_tickers or [])
    inactive_positions = inactive_positions or {}

    for ticker in entry_candidates:
        if ticker in inactive_tickers or ticker in inactive_positions:
            continue

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

        if ticker in top_set and not allow_new_top:
            continue
        if ticker not in top_set and not allow_new_momentum:
            continue

        print(f"[TEST ENTRY] BUY {ticker} | Regime={regime} | KRW={test_krw:,.0f}")

        if config.REAL_ORDER:
            if ticker in inactive_tickers or ticker in inactive_positions:
                print(f"[BLOCK] inactive ticker buy blocked(TEST): {ticker}")
                continue
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
        if ticker in top_set:
            state[ticker]["entry_bucket"] = "TOP10"
        else:
            state[ticker]["entry_bucket"] = "MOMENTUM"
            momentum_entry_tickers.add(ticker)
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
    state, cooldown_until, inactive_positions = load_state()
    verify_state_with_balance(upbit, state)

    # 마켓 정보 로딩 (실패 시 스테이블/사용자 제외만 적용)
    market_info = get_upbit_krw_markets()
    if market_info:
        print(f"[FILTER] loaded KRW market info: {len(market_info)}")
    else:
        print("[FILTER] market info unavailable. apply stable/user exclusions only")

    # 거래 불가 코인 포지션 분리
    state, moved_inactive, sanitize_repaired, moved_count = sanitize_positions(state, market_info)
    if moved_inactive:
        inactive_positions.update(moved_inactive)
    if sanitize_repaired:
        print(f"[STATE] sanitize repaired fields: {sanitize_repaired}")
    if moved_count:
        print(f"[STATE] moved count: {moved_count}")

    def persist_state(state_ref, cooldown_ref):
        save_state(state_ref, cooldown_ref, inactive_positions=inactive_positions)

    if moved_count:
        persist_state(state, cooldown_until)

    momentum_seen_at = {}
    momentum_stoploss_until = {}
    momentum_entry_tickers = set()
    _, trade_log_consumed_rows = read_new_trade_rows(config.TRADE_LOG_PATH, 0)

    # 유니버스(Top10 + 모멘텀 후보) / K맵
    raw_ranked = get_top_tickers_by_value(int(getattr(config, "UNIVERSE_SCAN_N", config.TOP_N)))
    top_universe, momentum_candidates, inactive_universe, inactive_reasons, momentum_seen_at = build_rotation_universe(
        raw_ranked, market_info, momentum_seen_at, now_kst()
    )
    universe = _dedupe_keep_order(top_universe + momentum_candidates)
    top_set = set(top_universe)
    for ticker, s in state.items():
        if not s.get("holding", False):
            continue
        if s.get("entry_bucket") == "MOMENTUM" or ticker not in top_set:
            momentum_entry_tickers.add(ticker)

    inactive_tickers = set(inactive_universe) | set(inactive_positions.keys())
    print_filter_summary(universe, inactive_universe, inactive_reasons)
    print(f"[UNIVERSE] top={len(top_universe)} momentum={len(momentum_candidates)}")
    if momentum_candidates:
        print(f"[UNIVERSE] momentum picks: {', '.join(momentum_candidates[:5])}")

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
                raw_ranked = get_top_tickers_by_value(int(getattr(config, "UNIVERSE_SCAN_N", config.TOP_N)))
                top_universe, momentum_candidates, inactive_universe, inactive_reasons, momentum_seen_at = build_rotation_universe(
                    raw_ranked, market_info, momentum_seen_at, now
                )
                universe = _dedupe_keep_order(top_universe + momentum_candidates)
                inactive_tickers = set(inactive_universe) | set(inactive_positions.keys())
                print_filter_summary(universe, inactive_universe, inactive_reasons)
                print(f"[UNIVERSE] top={len(top_universe)} momentum={len(momentum_candidates)}")
                if momentum_candidates:
                    print(f"[UNIVERSE] momentum picks: {', '.join(momentum_candidates[:5])}")
                k_map = build_k_map(universe) if BOT_MODE == "MAIN" else {}
                last_refresh = now

            keep_after = now - dt.timedelta(minutes=15)
            for ticker, seen_at in list(momentum_seen_at.items()):
                if not isinstance(seen_at, dt.datetime) or seen_at < keep_after:
                    momentum_seen_at.pop(ticker, None)
            momentum_candidates = [t for t in momentum_candidates if t in momentum_seen_at]
            universe = _dedupe_keep_order(top_universe + momentum_candidates)

            # 시장 컨디션
            regime = "FULL"
            if config.USE_MARKET_REGIME:
                try:
                    regime = get_market_regime()
                except Exception:
                    regime = "MID"

            holding_tickers = [t for t, s in state.items() if s.get("holding")]
            inactive_holding_tickers = [t for t, s in inactive_positions.items() if s.get("holding")]
            price_targets = set(universe) | set(holding_tickers) | set(inactive_holding_tickers)
            prices = batch_get_prices(price_targets)

            krw = float(get_balance(upbit, "KRW"))
            prices["_krw"] = krw
            prices["_caches"] = (day_cache, intraday_cache, minute_cache)
            prices["_inactive_tickers"] = inactive_tickers
            prices["_inactive_positions"] = inactive_positions
            prices["_momentum_candidates"] = set(momentum_candidates)

            equity = estimate_equity(krw, state, prices, upbit, inactive_positions=inactive_positions)

            base_per_trade, base_max_holdings = get_base_position_settings(equity)
            per_trade_amt, max_holdings = apply_market_regime(equity, base_per_trade, base_max_holdings, regime)
            holding_cnt = sum(1 for s in state.values() if s.get("holding"))
            entry_universe, top_limit, momentum_limit, top_holdings, momentum_holdings = build_entry_universe(
                state, top_universe, momentum_candidates, max_holdings, now, momentum_stoploss_until
            )

            if (now - last_status).total_seconds() >= config.STATUS_PRINT_SEC:
                print(
                    f"[STATUS] Regime={regime} | Equity~{equity:,.0f} | "
                    f"PerTrade~{per_trade_amt:,.0f} | Holding={holding_cnt}/{max_holdings} | "
                    f"Top={top_holdings}/{top_limit} Momentum={momentum_holdings}/{momentum_limit}"
                )
                last_status = now

            # 신규 진입
            if max_holdings > 0:

                if BOT_MODE == "TEST":
                    # TEST 모드는 per_trade_amt(메인 예산) 안 씀. MINUTE_TEST_PER_TRADE_KRW만 씀.
                    _ = try_minute_test_entries(
                        upbit=upbit,
                        now=now,
                        top_universe=top_universe,
                        momentum_candidates=momentum_candidates,
                        prices=prices,
                        state=state,
                        cooldown_until=cooldown_until,
                        max_holdings=max_holdings,
                        holding_cnt=holding_cnt,
                        regime=regime,
                        wait_for_filled_snapshot_fn=wait_for_filled_snapshot,
                        save_state_fn=persist_state,
                        momentum_block_until=momentum_stoploss_until,
                        momentum_entry_tickers=momentum_entry_tickers,
                        inactive_tickers=inactive_tickers,
                        inactive_positions=inactive_positions,
                    )

                else:
                    # MAIN 모드: 기존 엔진 사용 (일봉 + 4시간 + 분봉타이밍 + 돌파)
                    if per_trade_amt > 0:
                        _ = try_entries(
                            upbit=upbit,
                            now=now,
                            universe=entry_universe,
                            prices=prices,
                            k_map=k_map,
                            state=state,
                            cooldown_until=cooldown_until,
                            per_trade_amt=per_trade_amt,
                            max_holdings=max_holdings,
                            holding_cnt=holding_cnt,
                            regime=regime,
                            wait_for_filled_snapshot_fn=wait_for_filled_snapshot,
                            save_state_fn=persist_state,
                            inactive_tickers=inactive_tickers,
                            inactive_positions=inactive_positions,
                        )

            # 포지션 관리(항상)
            manage_positions(
                upbit,
                now,
                state,
                prices,
                cooldown_until,
                save_state_fn=persist_state,
                inactive_tickers=inactive_tickers,
                inactive_positions=inactive_positions,
            )

            top_set = set(top_universe)
            for ticker, s in state.items():
                if not s.get("holding", False):
                    continue
                if ticker not in top_set:
                    momentum_entry_tickers.add(ticker)

            new_rows, trade_log_consumed_rows = read_new_trade_rows(config.TRADE_LOG_PATH, trade_log_consumed_rows)
            update_momentum_stoploss_block(new_rows, now, momentum_entry_tickers, momentum_stoploss_until)

            # 주기 저장
            if (now - last_state_save).total_seconds() >= float(config.STATE_SAVE_INTERVAL_SEC):
                persist_state(state, cooldown_until)
                last_state_save = now

            time.sleep(config.POLL_SEC)

        except KeyboardInterrupt:
            print("\n사용자 종료(Ctrl+C)")
            persist_state(state, cooldown_until)
            break
        except Exception as e:
            print("에러:", e)
            time.sleep(1)


if __name__ == "__main__":
    run()
