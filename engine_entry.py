"""MAIN/SCALP 전략 신호 판단과 매수 실행을 담당하는 진입 엔진."""

import time

import config
import position_manager
from indicators import check_filters, intraday_trend_ok, minute_entry_ok, scalp_entry_signal
from strategy import calc_target
from utils.telegram import tg


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


def _is_cooldown_active(now, until) -> bool:
    if until is None:
        return False
    try:
        return bool(now < until)
    except TypeError:
        # Defensive fallback for mixed naive/aware datetime values.
        try:
            return float(now.timestamp()) < float(until.timestamp())
        except Exception:
            return False


def entry_passes_filters(ticker: str, now, day_cache, intraday_cache, minute_cache) -> bool:
    # Daily filter cache
    cached = day_cache.get(ticker)
    if cached and (now - cached[1]).total_seconds() < float(getattr(config, "DAY_FILTER_CACHE_SEC", 60)):
        ok_day = cached[0]
    else:
        ok_day = bool(check_filters(ticker))
        day_cache[ticker] = (ok_day, now)
    if not ok_day:
        return False

    # Intraday trend cache
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

    # Minute timing cache
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


def _execute_buy(
    upbit,
    ticker: str,
    buy_krw: float,
    cur: float,
    wait_for_filled_snapshot_fn,
):
    if bool(getattr(config, "REAL_ORDER", False)):
        upbit.buy_market_order(ticker, buy_krw)
        filled_vol, avg_buy = wait_for_filled_snapshot_fn(upbit, ticker, timeout_sec=3.0, interval=0.2)
        initial_vol = float(filled_vol) if filled_vol > 0 else (float(buy_krw) / float(cur))
        entry_price = float(avg_buy) if avg_buy > 0 else float(cur)
    else:
        initial_vol = float(buy_krw) / float(cur)
        entry_price = float(cur)
    return initial_vol, entry_price


def _allow_add_buy() -> bool:
    return bool(getattr(config, "ALLOW_ADD_BUY", False))


def try_main_entries(
    upbit,
    now,
    universe,
    prices,
    k_map,
    state,
    cooldown_until,
    per_trade_amt,
    max_holdings,
    total_holding_cnt,
    regime,
    wait_for_filled_snapshot_fn,
    save_state_fn,
    inactive_tickers=None,
    inactive_positions=None,
    global_holding_tickers=None,
    before_buy_fn=None,
):
    day_cache, intraday_cache, minute_cache = _safe_caches(prices)
    krw = _safe_krw(prices)
    inactive_tickers = set(inactive_tickers or [])
    inactive_positions = inactive_positions or {}
    global_holding_tickers = set(global_holding_tickers or [])

    if total_holding_cnt >= int(max_holdings):
        return False
    if float(per_trade_amt) <= 0:
        return False
    if krw < float(per_trade_amt):
        return False

    for ticker in universe:
        if _is_blocked_ticker(ticker, inactive_tickers, inactive_positions):
            continue
        if ticker in global_holding_tickers:
            # Shared lock: no same ticker duplicate across strategies.
            if not _allow_add_buy():
                continue
            # Add-buy is enabled only for the strategy already holding the ticker.
            if not state.get(ticker, {}).get("holding", False):
                continue

        until = cooldown_until.get(ticker)
        if _is_cooldown_active(now, until):
            continue

        holding = bool(state.get(ticker, {}).get("holding", False))
        if holding:
            if not _allow_add_buy():
                continue
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
        if cur is None or float(cur) <= 0:
            continue

        k = float(k_map.get(ticker, getattr(config, "K_DEFAULT", 0.5)))
        try:
            target = float(calc_target(ticker, k))
        except Exception:
            continue
        if float(cur) < target:
            continue

        action = "ADD" if holding else "BUY"
        print(f"[MAIN ENTRY] {action} {ticker} | Regime={regime} | KRW={per_trade_amt:,.0f}")
        try:
            if _is_blocked_ticker(ticker, inactive_tickers, inactive_positions):
                print(f"[BLOCK] inactive ticker buy blocked: {ticker}")
                continue
            if callable(before_buy_fn):
                try:
                    allowed = bool(before_buy_fn(ticker=ticker, buy_krw=float(per_trade_amt), cur=float(cur)))
                except Exception as e:
                    print(f"[WARN] before_buy failed(MAIN): {ticker} err={e}")
                    continue
                if not allowed:
                    continue
            initial_vol, entry_price = _execute_buy(
                upbit=upbit,
                ticker=ticker,
                buy_krw=float(per_trade_amt),
                cur=float(cur),
                wait_for_filled_snapshot_fn=wait_for_filled_snapshot_fn,
            )
        except Exception as e:
            print(f"[WARN] buy failed(MAIN): {ticker} err={e}")
            tg(f"⚠️ ORDER 실패: BUY {ticker}")
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
                float(entry_price),
                float(initial_vol),
                float(per_trade_amt),
                regime,
                entry_ts=float(now.timestamp()),
            )
            state[ticker]["entry_bucket"] = "CORE"

        save_state_fn()
        time.sleep(0.15)
        return True

    return False


def try_scalp_entries(
    upbit,
    now,
    universe,
    prices,
    state,
    cooldown_until,
    per_trade_amt,
    max_holdings,
    total_holding_cnt,
    regime,
    wait_for_filled_snapshot_fn,
    save_state_fn,
    inactive_tickers=None,
    inactive_positions=None,
    global_holding_tickers=None,
    conservative=False,
):
    if not bool(getattr(config, "USE_MINUTE_TEST_STRATEGY", False)):
        return False

    krw = _safe_krw(prices)
    inactive_tickers = set(inactive_tickers or [])
    inactive_positions = inactive_positions or {}
    global_holding_tickers = set(global_holding_tickers or [])
    buy_krw = float(per_trade_amt)

    if total_holding_cnt >= int(max_holdings):
        return False
    if buy_krw <= 0:
        return False
    if krw < buy_krw:
        return False

    for ticker in universe:
        if _is_blocked_ticker(ticker, inactive_tickers, inactive_positions):
            continue
        if ticker in global_holding_tickers:
            # Shared lock: never duplicate ticker in SCALP.
            continue

        until = cooldown_until.get(ticker)
        if _is_cooldown_active(now, until):
            continue

        can_new, _ = position_manager.can_open_new_position(state, ticker)
        if not can_new:
            continue

        cur = prices.get(ticker)
        if cur is None or float(cur) <= 0:
            continue

        try:
            sig = bool(scalp_entry_signal(ticker, conservative=bool(conservative)))
        except Exception:
            sig = False
        if not sig:
            continue

        print(
            f"[SCALP ENTRY] BUY {ticker} | Regime={regime} | KRW={buy_krw:,.0f} "
            f"| cons={'Y' if conservative else 'N'}"
        )
        try:
            if _is_blocked_ticker(ticker, inactive_tickers, inactive_positions):
                print(f"[BLOCK] inactive ticker buy blocked(SCALP): {ticker}")
                continue
            initial_vol, entry_price = _execute_buy(
                upbit=upbit,
                ticker=ticker,
                buy_krw=buy_krw,
                cur=float(cur),
                wait_for_filled_snapshot_fn=wait_for_filled_snapshot_fn,
            )
        except Exception as e:
            print(f"[WARN] buy failed(SCALP): {ticker} err={e}")
            tg(f"⚠️ ORDER 실패: BUY {ticker}")
            continue

        state[ticker] = position_manager.init_position_state(
            float(entry_price),
            float(initial_vol),
            float(buy_krw),
            regime,
            entry_ts=float(now.timestamp()),
        )
        state[ticker]["entry_bucket"] = "SURGE"
        save_state_fn()
        time.sleep(0.10)
        return True

    return False
