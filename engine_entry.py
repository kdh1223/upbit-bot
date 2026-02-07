# engine_entry.py
import time
import config
import position_manager
from indicators import check_filters, intraday_trend_ok, minute_entry_ok, minute_test_signal
from strategy import calc_target


def _safe_caches(prices):
    caches = prices.get("_caches")
    if isinstance(caches, tuple) and len(caches) == 3:
        return caches
    # (day_cache, intraday_cache, minute_cache)
    return ({}, {}, {})


def _safe_krw(prices) -> float:
    try:
        return float(prices.get("_krw", 0.0))
    except Exception:
        return 0.0


def _is_blocked_ticker(ticker: str, inactive_tickers, inactive_positions) -> bool:
    return (ticker in inactive_tickers) or (ticker in inactive_positions)


def entry_passes_filters(ticker: str, now, day_cache, intraday_cache, minute_cache) -> bool:
    # 일봉 캐시
    cached = day_cache.get(ticker)
    if cached and (now - cached[1]).total_seconds() < float(getattr(config, "DAY_FILTER_CACHE_SEC", 60)):
        ok_day = cached[0]
    else:
        ok_day = bool(check_filters(ticker))
        day_cache[ticker] = (ok_day, now)
    if not ok_day:
        return False

    # 4시간봉 캐시
    if bool(getattr(config, "USE_INTRADAY_FILTER", False)):
        cached2 = intraday_cache.get(ticker)
        if cached2 and (now - cached2[1]).total_seconds() < float(getattr(config, "INTRADAY_FILTER_CACHE_SEC", 30)):
            ok_4h = cached2[0]
        else:
            try:
                ok_4h = bool(intraday_trend_ok(ticker))
            except Exception:
                ok_4h = True
            intraday_cache[ticker] = (ok_4h, now)
        if not ok_4h:
            return False

    # 분봉 캐시
    cached3 = minute_cache.get(ticker)
    if cached3 and (now - cached3[1]).total_seconds() < float(getattr(config, "MINUTE_ENTRY_CACHE_SEC", 10)):
        ok_m = cached3[0]
    else:
        try:
            ok_m = bool(minute_entry_ok(ticker))
        except Exception:
            ok_m = True
        minute_cache[ticker] = (ok_m, now)
    if not ok_m:
        return False

    return True


def try_entries(
    upbit,
    now,
    universe,
    prices,
    k_map,
    state,
    cooldown_until,
    per_trade_amt,
    max_holdings,
    holding_cnt,
    regime,
    wait_for_filled_snapshot_fn,
    save_state_fn,
    inactive_tickers=None,
    inactive_positions=None,
):
    """
    성공 시 True 반환 (한 번 진입하면 루프 탈출)
    """
    day_cache, intraday_cache, minute_cache = _safe_caches(prices)
    krw = _safe_krw(prices)
    inactive_tickers = set(inactive_tickers or [])
    inactive_positions = inactive_positions or {}

    # ==========================
    # 1) 메인 진입 우선 스캔
    # ==========================
    for ticker in universe:
        if _is_blocked_ticker(ticker, inactive_tickers, inactive_positions):
            continue

        holding = state.get(ticker, {}).get("holding", False)
        if holding_cnt >= max_holdings and not holding:
            continue

        until = cooldown_until.get(ticker)
        if until is not None and now < until:
            continue

        # 잔고 체크 (메인/추가진입)
        if (not holding) and (krw < float(per_trade_amt)):
            continue
        if holding and (krw < float(per_trade_amt)):
            # 추가매수도 현금 없으면 스킵
            continue

        # 분할/신규 가능 여부
        if holding:
            can_add, _ = position_manager.can_add_position(state, ticker, per_trade_amt)
            if not can_add:
                continue
        else:
            can_new, _ = position_manager.can_open_new_position(state, ticker)
            if not can_new:
                continue

        if not entry_passes_filters(ticker, now, day_cache, intraday_cache, minute_cache):
            continue

        cur = prices.get(ticker)
        if cur is None:
            continue

        # 메인 돌파 진입
        k = float(k_map.get(ticker, getattr(config, "K_DEFAULT", 0.5)))
        try:
            target = float(calc_target(ticker, k))
        except Exception:
            continue

        if float(cur) >= target and float(per_trade_amt) > 0:
            action = "ADD" if holding else "BUY"
            print(f"[ENTRY] {action} {ticker} | Regime={regime} | KRW={per_trade_amt:,.0f}")

            try:
                if _is_blocked_ticker(ticker, inactive_tickers, inactive_positions):
                    print(f"[BLOCK] inactive ticker buy blocked: {ticker}")
                    continue
                if bool(getattr(config, "REAL_ORDER", False)):
                    upbit.buy_market_order(ticker, per_trade_amt)
                    filled_vol, avg_buy = wait_for_filled_snapshot_fn(upbit, ticker, timeout_sec=3.0, interval=0.2)
                    initial_vol = float(filled_vol) if filled_vol > 0 else (float(per_trade_amt) / float(cur))
                    entry_price = float(avg_buy) if avg_buy > 0 else float(cur)
                else:
                    initial_vol = float(per_trade_amt) / float(cur)
                    entry_price = float(cur)
            except Exception as e:
                print(f"[WARN] buy failed: {ticker} err={e}")
                continue

            if holding:
                if bool(getattr(config, "REAL_ORDER", False)):
                    position_manager.apply_add_snapshot(
                        state, ticker, float(initial_vol), float(entry_price), float(per_trade_amt)
                    )
                else:
                    add_vol = float(per_trade_amt) / float(cur)
                    position_manager.apply_add_mock(state, ticker, float(cur), float(add_vol), float(per_trade_amt))
            else:
                state[ticker] = position_manager.init_position_state(
                    float(entry_price), float(initial_vol), float(per_trade_amt), regime
                )
                momentum_candidates = set(prices.get("_momentum_candidates", set()) or set())
                state[ticker]["entry_bucket"] = "MOMENTUM" if ticker in momentum_candidates else "TOP10"
                holding_cnt += 1

            save_state_fn(state, cooldown_until)
            time.sleep(0.15)
            return True

    # ==========================
    # 2) 메인 진입이 없었을 때만 분봉 테스트 스캔
    # ==========================
    if not bool(getattr(config, "USE_MINUTE_TEST_STRATEGY", False)):
        return False

    test_krw = float(getattr(config, "MINUTE_TEST_PER_TRADE_KRW", 10_000))
    if test_krw <= 0:
        return False
    if krw < test_krw:
        return False

    for ticker in universe:
        if _is_blocked_ticker(ticker, inactive_tickers, inactive_positions):
            continue

        holding = state.get(ticker, {}).get("holding", False)
        if holding:
            continue
        if holding_cnt >= max_holdings:
            continue

        until = cooldown_until.get(ticker)
        if until is not None and now < until:
            continue

        can_new, _ = position_manager.can_open_new_position(state, ticker)
        if not can_new:
            continue

        # (원하면) 테스트도 필터를 타게 할지 선택
        # 지금은 기존 코드 흐름을 유지(필터 통과한 종목만 테스트 진입)
        if not entry_passes_filters(ticker, now, day_cache, intraday_cache, minute_cache):
            continue

        cur = prices.get(ticker)
        if cur is None:
            continue

        try:
            if minute_test_signal(ticker):
                print(f"[TEST ENTRY] BUY {ticker} | Regime={regime} | KRW={test_krw:,.0f}")

                try:
                    if _is_blocked_ticker(ticker, inactive_tickers, inactive_positions):
                        print(f"[BLOCK] inactive ticker buy blocked(TEST): {ticker}")
                        continue
                    if bool(getattr(config, "REAL_ORDER", False)):
                        upbit.buy_market_order(ticker, test_krw)
                        filled_vol, avg_buy = wait_for_filled_snapshot_fn(upbit, ticker, timeout_sec=3.0, interval=0.2)
                        initial_vol = float(filled_vol) if filled_vol > 0 else (test_krw / float(cur))
                        entry_price = float(avg_buy) if avg_buy > 0 else float(cur)
                    else:
                        initial_vol = test_krw / float(cur)
                        entry_price = float(cur)
                except Exception as e:
                    print(f"[WARN] buy failed(TEST): {ticker} err={e}")
                    continue

                state[ticker] = position_manager.init_position_state(
                    float(entry_price),
                    float(initial_vol),
                    float(test_krw),
                    regime,
                )
                momentum_candidates = set(prices.get("_momentum_candidates", set()) or set())
                state[ticker]["entry_bucket"] = "MOMENTUM" if ticker in momentum_candidates else "TOP10"
                save_state_fn(state, cooldown_until)
                return True
        except Exception:
            pass

    return False
