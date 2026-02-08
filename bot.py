"""유니버스 갱신, 진입/청산, 상태 저장을 총괄하는 봇 메인 루프."""

import copy
import csv
import datetime as dt
import os
import sys
import time
import traceback
from collections import Counter

import pyupbit

import config
import position_manager
from engine_entry import try_main_entries, try_scalp_entries
from engine_manage import append_trade_log, log_order, manage_positions
from indicators import (
    check_filters_with_reason,
    detect_momentum_candidate,
    get_market_regime,
    intraday_trend_ok,
    scalp_btc_entry_signal,
)
from market import (
    filter_tradeable_tickers,
    get_balance,
    get_top_tickers_by_value,
    get_upbit_krw_markets,
    load_keys,
    sanitize_positions,
)
from order_utils import wait_for_filled_snapshot
from state_store import STRATEGIES, load_state, save_state, verify_state_with_balance
from strategy import build_k_map


BASE_TP_TABLE = copy.deepcopy(getattr(config, "TP_TABLE", {}))
BASE_STOP_LOSS_PCT = float(getattr(config, "STOP_LOSS_PCT", 0.01))


def now_kst():
    return dt.datetime.now()


def ensure_trade_log_header(path: str):
    expected = ["time", "ticker", "entry_price", "exit_price", "pnl_pct", "reason", "regime", "strategy"]

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

    legacy_7 = expected[:-1]
    if header == legacy_7:
        migrated = [expected]
        for row in rows[1:]:
            fixed = row[: len(legacy_7)]
            while len(fixed) < len(legacy_7):
                fixed.append("")
            fixed.append("")
            migrated.append(fixed)
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(migrated)
        print(f"[MIGRATE] {path} header updated: added 'strategy' column")
        return

    # Unknown header, do not rewrite automatically.
    print(f"[WARN] unexpected trade log header: {header}")


def batch_get_prices(tickers):
    try:
        data = pyupbit.get_current_price(list(tickers))
        if data is None:
            return {}
        if isinstance(data, dict):
            return {k: float(v) for k, v in data.items() if v is not None}
    except Exception:
        pass
    return {}


def _iter_all_states(strategy_state: dict, inactive_positions: dict = None):
    for strat in STRATEGIES:
        for ticker, s in (strategy_state.get(strat, {}) or {}).items():
            yield strat, ticker, s
    for ticker, s in (inactive_positions or {}).items():
        yield "INACTIVE", ticker, s


def estimate_equity(krw: float, strategy_state: dict, prices: dict, upbit, inactive_positions: dict = None) -> float:
    equity = float(krw)
    for _, ticker, s in _iter_all_states(strategy_state, inactive_positions):
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


def print_main_filter_reject_summary(stats: Counter):
    if not stats:
        return
    topn = max(1, int(getattr(config, "MAIN_FILTER_REJECT_SUMMARY_TOPN", 6)))
    total = sum(int(v) for v in stats.values())
    top = ", ".join([f"{k}:{v}" for k, v in stats.most_common(topn)])
    print(f"[MAIN_일봉필터요약] total={total} | {top}")


def _dedupe_keep_order(items):
    out = []
    seen = set()
    for x in items:
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out


def _count_strategy_holdings(state: dict) -> int:
    return sum(1 for _, s in (state or {}).items() if s.get("holding", False))


def _count_total_holdings(strategy_state: dict) -> int:
    return sum(_count_strategy_holdings(strategy_state.get(s, {})) for s in STRATEGIES)


def _all_holding_tickers(strategy_state: dict):
    out = set()
    for s in STRATEGIES:
        for ticker, st in (strategy_state.get(s, {}) or {}).items():
            if st.get("holding", False):
                out.add(ticker)
    return out


def _repair_cross_strategy_duplicate_holdings(strategy_state: dict):
    main_state = strategy_state.get("MAIN", {}) or {}
    scalp_state = strategy_state.get("SCALP", {}) or {}
    repaired = 0

    overlap = set(main_state.keys()) & set(scalp_state.keys())
    for ticker in overlap:
        main_h = bool(main_state.get(ticker, {}).get("holding", False))
        scalp_h = bool(scalp_state.get(ticker, {}).get("holding", False))
        if not (main_h and scalp_h):
            continue

        # Keep MAIN, release SCALP.
        s = scalp_state[ticker]
        s["holding"] = False
        s["add_count"] = 0
        s["invested_krw"] = 0.0
        s["target_krw"] = 0.0
        s["initial_volume"] = 0.0
        s["realized_krw"] = 0.0
        s["realized_cost_krw"] = 0.0
        repaired += 1
        print(f"[LOCK_REPAIR] duplicated holding released from SCALP: {ticker}")

    return repaired


def _core_and_surge_from_ranked(raw_ranked, market_info):
    ranked = _dedupe_keep_order(raw_ranked or [])
    core_n = int(getattr(config, "CORE_TOP_N", getattr(config, "TOP_N", 10)))
    surge_start = int(getattr(config, "SURGE_RANK_START", 11))
    surge_end = int(getattr(config, "SURGE_RANK_END", 30))

    scan_n = int(getattr(config, "UNIVERSE_SCAN_N", max(core_n, surge_end)))
    scan_n = max(scan_n, core_n, surge_end)
    ranked = ranked[:scan_n]

    core_raw = ranked[:core_n]
    if surge_end >= surge_start:
        surge_raw = ranked[max(0, surge_start - 1) : surge_end]
    else:
        surge_raw = []

    core_active, core_inactive, core_reasons = filter_tradeable_tickers(core_raw, market_info)
    surge_active, surge_inactive, surge_reasons = filter_tradeable_tickers(surge_raw, market_info)

    # CORE keeps higher-timeframe trend.
    core_filter_rejects = Counter()
    for ticker in core_inactive:
        reason = str(core_reasons.get(ticker, "UNIVERSE_FILTER"))
        core_filter_rejects[f"UNIVERSE_{reason}"] += 1

    core_filtered = []
    for ticker in core_active:
        try:
            ok_day, day_reason = check_filters_with_reason(ticker)
            if not bool(ok_day):
                core_filter_rejects[str(day_reason or "DAY_FILTER_FAIL")] += 1
                continue
            if bool(getattr(config, "USE_INTRADAY_FILTER", False)) and (not bool(intraday_trend_ok(ticker))):
                core_filter_rejects["INTRADAY_TREND_FAIL"] += 1
                continue
            core_filtered.append(ticker)
        except Exception:
            core_filter_rejects["DAY_FILTER_ERR"] += 1
            continue

    strict_prefilter = bool(getattr(config, "CORE_STRICT_PREFILTER", False))
    core_min_active = max(0, int(getattr(config, "CORE_MIN_ACTIVE", 0)))
    if strict_prefilter:
        core_final = core_filtered
    else:
        core_final = core_filtered if len(core_filtered) >= max(1, core_min_active) else list(core_active)

    inactive_all = _dedupe_keep_order(core_inactive + surge_inactive)
    reasons_all = {}
    reasons_all.update(core_reasons)
    reasons_all.update(surge_reasons)
    return core_final, surge_active, inactive_all, reasons_all, core_filter_rejects


def _update_surge_candidates(surge_pool, momentum_seen_at: dict, now):
    surge_pool = _dedupe_keep_order(surge_pool or [])

    for ticker in surge_pool:
        try:
            if detect_momentum_candidate(ticker):
                momentum_seen_at[ticker] = now
        except Exception:
            continue

    keep_min = int(getattr(config, "SURGE_KEEP_MINUTES", 15))
    cutoff = now - dt.timedelta(minutes=max(1, keep_min))
    for ticker, ts in list(momentum_seen_at.items()):
        if not isinstance(ts, dt.datetime):
            momentum_seen_at.pop(ticker, None)
            continue
        if ts < cutoff:
            momentum_seen_at.pop(ticker, None)

    out = [t for t in surge_pool if t in momentum_seen_at]
    cap = int(getattr(config, "SPIKE_CANDIDATE_MAX", 0))
    if cap > 0:
        out = out[:cap]
    return out, momentum_seen_at


def get_base_position_settings(equity):
    fixed_until = float(getattr(config, "HOLDINGS_FIXED_UNTIL_EQUITY", 1_500_000))
    if float(equity) <= fixed_until:
        return float(config.TEST_PER_TRADE_KRW), 2

    tiers = sorted((getattr(config, "ACCOUNT_TIERS", []) or []), key=lambda x: float(x.get("min_equity", 0)))
    max_holdings = 2
    for t in tiers:
        min_eq = float(t.get("min_equity", 0))
        if equity >= min_eq:
            max_holdings = int(t.get("max_holdings", max_holdings))

    max_holdings = max(2, max_holdings)
    per_trade_amt = float(equity) / float(max_holdings)
    return per_trade_amt, max_holdings


def apply_market_regime(equity, base_per_trade, base_max_holdings, regime: str):
    invest_frac = float(config.REGIME_INVEST_FRAC.get(regime, 0.7))
    holdings_mult = float(config.REGIME_HOLDINGS_MULT.get(regime, 0.7))

    if invest_frac <= 0 or holdings_mult <= 0:
        return 0.0, 0

    eff_max_holdings = max(1, int(base_max_holdings * holdings_mult))
    total_invest_budget = float(equity) * invest_frac
    per_trade_amt = total_invest_budget / float(eff_max_holdings)
    per_trade_amt = max(float(config.MIN_ORDER_KRW), per_trade_amt)
    return float(per_trade_amt), int(eff_max_holdings)


def _pick_holding_scale_key(max_holdings: int):
    table = getattr(config, "HOLDING_SCALE", {}) or {}
    keys = sorted([int(k) for k in table.keys() if str(k).isdigit() or isinstance(k, int)])
    if not keys:
        return 2

    h = int(max_holdings)
    if h <= keys[0]:
        return keys[0]
    if h >= keys[-1]:
        return keys[-1]
    for k in keys:
        if h <= k:
            return k
    return keys[-1]


def apply_runtime_params_by_holdings(max_holdings: int):
    scale_table = getattr(config, "HOLDING_SCALE", {}) or {}
    key = _pick_holding_scale_key(max_holdings)
    scale = float(scale_table.get(key, 1.0))

    base_table = BASE_TP_TABLE or copy.deepcopy(getattr(config, "TP_TABLE", {}))
    scaled_table = {}
    for regime, params in (base_table or {}).items():
        scaled_table[regime] = {
            "TP1_PCT": float(params.get("TP1_PCT", 0.0)) * scale,
            "TP2_PCT": float(params.get("TP2_PCT", 0.0)) * scale,
            "TRAIL_BACK_PCT": float(params.get("TRAIL_BACK_PCT", 0.0)) * scale,
        }

    if scaled_table:
        config.TP_TABLE = scaled_table
    config.STOP_LOSS_PCT = float(BASE_STOP_LOSS_PCT) * scale
    return key, scale


def update_day_tp1_counter(state: dict, counted_tickers: set, day_tp1_count: int, strategy: str):
    for ticker, s in (state or {}).items():
        holding = bool(s.get("holding", False))
        tp1 = bool(s.get("tp1", False))
        if holding and tp1 and ticker not in counted_tickers:
            counted_tickers.add(ticker)
            day_tp1_count += 1
            print(f"[TP1_COUNT][{strategy}] {day_tp1_count}")
        if (not holding) or (not tp1):
            counted_tickers.discard(ticker)
    return int(day_tp1_count)


def update_loss_seq_from_events(events, current: int, strategy: str):
    cur = int(current)
    for e in events or []:
        try:
            pnl = float(e.get("pnl_pct", 0.0))
        except Exception:
            pnl = 0.0
        prev = cur
        if pnl < 0:
            cur += 1
        else:
            cur = 0
        if cur != prev:
            print(f"[LOSS_STREAK][{strategy}] {cur}")
    return int(cur)


def _is_dawn_hour(now: dt.datetime) -> bool:
    h = int(now.hour)
    start = int(getattr(config, "SCALP_DAWN_START_HOUR", 0))
    end = int(getattr(config, "SCALP_DAWN_END_HOUR", 7))
    if start == end:
        return False
    if start < end:
        return start <= h < end
    return h >= start or h < end


def _resolve_mode() -> str:
    mode = str(getattr(config, "MODE", getattr(config, "BOT_MODE", "MAIN"))).upper().strip()
    if mode not in {"MAIN", "SAFE", "TEST"}:
        mode = "SAFE"
    return mode


def _mode_to_strategy_flags(mode: str):
    """
    MAIN: MAIN + SCALP_BTC
    SAFE: MAIN only
    TEST: MAIN + SCALP_BTC (mock order only)
    """
    legacy_on = bool(getattr(config, "SCALP_LEGACY_ENABLED", False))
    scalp_btc_on = bool(getattr(config, "SCALP_BTC_ENABLED", True))
    if mode == "SAFE":
        return True, False, False, False
    if mode == "TEST":
        return True, False and legacy_on, scalp_btc_on, True
    # MAIN(default)
    return True, legacy_on, scalp_btc_on, False


class _TickerLock:
    def __init__(self):
        self._exp = {}

    def acquire(self, ticker: str, now: dt.datetime, timeout_sec: float) -> bool:
        timeout_sec = max(1.0, float(timeout_sec))
        exp = self._exp.get(ticker)
        if isinstance(exp, dt.datetime) and now < exp:
            return False
        self._exp[ticker] = now + dt.timedelta(seconds=timeout_sec)
        return True

    def release(self, ticker: str):
        self._exp.pop(ticker, None)


def _scalp_btc_is_holding(scalp_btc_state: dict) -> bool:
    return bool((scalp_btc_state or {}).get("holding", False))


def _all_holding_tickers_with_scalp_btc(strategy_state: dict, scalp_btc_state: dict):
    out = _all_holding_tickers(strategy_state)
    if _scalp_btc_is_holding(scalp_btc_state):
        out.add(str(getattr(config, "SCALP_BTC_TICKER", "KRW-BTC")))
    return out


def _count_total_holdings_with_scalp_btc(strategy_state: dict, scalp_btc_state: dict, include_legacy_scalp: bool):
    total = _count_strategy_holdings(strategy_state.get("MAIN", {}))
    if include_legacy_scalp:
        total += _count_strategy_holdings(strategy_state.get("SCALP", {}))
    if _scalp_btc_is_holding(scalp_btc_state):
        total += 1
    return int(total)


def _get_scalp_btc_buy_krw(equity: float, krw: float):
    per_share = float(getattr(config, "SCALP_BTC_PER_TRADE_SHARE", 0.10))
    max_share = float(getattr(config, "SCALP_BTC_MAX_SHARE", 0.20))
    per_krw = float(equity) * per_share
    cap_krw = float(equity) * max_share
    buy_krw = min(per_krw, cap_krw, float(krw))

    required = float(getattr(config, "MIN_ORDER_KRW", 5_000)) * float(getattr(config, "SCALP_BTC_MIN_ORDER_BUFFER", 1.02))
    if buy_krw < required:
        print(f"[SCALP_BTC] skip: size {buy_krw:,.0f} < required_min {required:,.0f} (fee buffer)")
        return 0.0
    return float(buy_krw)


def _scalp_btc_reset_position(state: dict):
    state["holding"] = False
    state["entry_price"] = 0.0
    state["qty"] = 0.0
    state["entry_time"] = None
    state["peak_price"] = 0.0


def _scalp_btc_close_position(
    upbit,
    now: dt.datetime,
    state: dict,
    prices: dict,
    reason: str,
    persist_state_fn,
):
    ticker = str(getattr(config, "SCALP_BTC_TICKER", "KRW-BTC"))
    if not bool(state.get("holding", False)):
        return True, None

    cur = prices.get(ticker)
    if cur is None:
        try:
            cur = float(pyupbit.get_current_price(ticker))
        except Exception:
            cur = None
    if cur is None or float(cur) <= 0:
        return False, "price_unavailable"
    cur = float(cur)

    entry = float(state.get("entry_price", 0.0))
    qty = float(state.get("qty", 0.0))
    if bool(getattr(config, "REAL_ORDER", False)):
        coin = ticker.split("-")[1]
        qty = float(get_balance(upbit, coin))
    if qty <= 0:
        _scalp_btc_reset_position(state)
        persist_state_fn()
        return True, None

    order_value = qty * cur
    if order_value < float(getattr(config, "MIN_ORDER_KRW", 5_000)):
        if bool(getattr(config, "DUST_CLOSE_AS_CLOSED", True)):
            _scalp_btc_reset_position(state)
            persist_state_fn()
            return True, None
        return False, "below_min_order"

    ok = True
    err_msg = ""
    if bool(getattr(config, "REAL_ORDER", False)):
        ok = False
        max_retry = int(getattr(config, "ORDER_RETRY_MAX", 3))
        sleep_sec = float(getattr(config, "ORDER_RETRY_SLEEP_SEC", 0.35))
        for i in range(max_retry):
            try:
                resp = upbit.sell_market_order(ticker, qty)
                log_order("SELL", ticker, qty, True, f"scalp_btc_close try={i+1} resp={str(resp)[:120]}")
                ok = True
                break
            except Exception as e:
                err_msg = str(e)
                log_order("SELL", ticker, qty, False, f"scalp_btc_close try={i+1} err={err_msg}")
                time.sleep(sleep_sec)
    else:
        log_order("SELL", ticker, qty, True, "scalp_btc_mock")

    if not ok:
        return False, f"sell_failed:{err_msg}"

    pnl = (cur / entry - 1.0) if entry > 0 else 0.0
    cd_min = int(getattr(config, "SCALP_BTC_COOLDOWN_PROFIT_MIN", 10))
    if pnl < 0:
        cd_min = int(getattr(config, "SCALP_BTC_COOLDOWN_LOSS_MIN", 30))
    state["cooldown_until"] = now + dt.timedelta(minutes=max(1, cd_min))

    if pnl < 0:
        state["loss_streak"] = int(state.get("loss_streak", 0)) + 1
        print(f"[LOSS_STREAK][SCALP_BTC] {state['loss_streak']}")
        max_streak = int(getattr(config, "SCALP_BTC_MAX_LOSS_STREAK", 2))
        if int(state["loss_streak"]) >= max_streak:
            pause_min = int(getattr(config, "SCALP_BTC_PAUSE_MIN_AFTER_STREAK", 60))
            state["paused_until"] = now + dt.timedelta(minutes=max(1, pause_min))
            state["loss_streak"] = 0
            print(f"[SCALP_BTC_PAUSE] reason=LOSS_STREAK until={state['paused_until'].strftime('%H:%M:%S')}")
    else:
        state["loss_streak"] = 0

    append_trade_log(
        config.TRADE_LOG_PATH,
        [
            now.strftime("%Y-%m-%d %H:%M:%S"),
            ticker,
            f"{entry:.6f}",
            f"{cur:.6f}",
            f"{(pnl * 100.0):.2f}",
            reason,
            "SCALP_BTC",
            "SCALP_BTC",
        ],
    )
    print(f"[CLOSE][SCALP_BTC] {ticker} pnl={(pnl * 100.0):+.2f}% reason={reason}")
    _scalp_btc_reset_position(state)
    persist_state_fn()
    return True, None


def _manage_scalp_btc_position(upbit, now: dt.datetime, state: dict, prices: dict, persist_state_fn):
    if not bool(state.get("holding", False)):
        return False

    ticker = str(getattr(config, "SCALP_BTC_TICKER", "KRW-BTC"))
    cur = prices.get(ticker)
    if cur is None:
        return False
    cur = float(cur)

    entry = float(state.get("entry_price", 0.0))
    if entry <= 0:
        return False

    state["peak_price"] = max(float(state.get("peak_price", entry)), cur)
    pnl = (cur / entry) - 1.0
    from_peak = (cur / max(float(state.get("peak_price", cur)), 1e-12)) - 1.0

    reason = None
    if pnl <= -float(getattr(config, "SCALP_BTC_SL_PCT", 0.009)):
        reason = "scalp_btc_stop_loss"
    elif pnl >= float(getattr(config, "SCALP_BTC_TP_PCT", 0.012)):
        reason = "scalp_btc_take_profit"
    elif bool(getattr(config, "SCALP_BTC_TRAIL_ON", True)):
        trail_from = float(getattr(config, "SCALP_BTC_TRAIL_FROM", 0.010))
        giveback = float(getattr(config, "SCALP_BTC_TRAIL_GIVEBACK", 0.006))
        if pnl >= trail_from and from_peak <= -giveback:
            reason = "scalp_btc_trailing"

    if reason is None:
        entry_time = state.get("entry_time")
        if isinstance(entry_time, dt.datetime):
            hold_min = (now - entry_time).total_seconds() / 60.0
            if hold_min >= float(getattr(config, "SCALP_BTC_MAX_HOLD_MIN", 90)):
                reason = "scalp_btc_timeout"

    if reason is None:
        return False

    _scalp_btc_close_position(
        upbit=upbit,
        now=now,
        state=state,
        prices=prices,
        reason=reason,
        persist_state_fn=persist_state_fn,
    )
    return True


def _try_scalp_btc_entry(
    upbit,
    now: dt.datetime,
    state: dict,
    prices: dict,
    equity: float,
    krw: float,
    max_holdings: int,
    total_holding: int,
    main_state: dict,
    persist_state_fn,
    ticker_lock: _TickerLock,
):
    ticker = str(getattr(config, "SCALP_BTC_TICKER", "KRW-BTC"))

    if bool(state.get("holding", False)):
        return False
    if total_holding >= int(max_holdings):
        return False
    if int(getattr(config, "SCALP_BTC_MAX_POSITIONS", 1)) < 1:
        return False

    paused_until = state.get("paused_until")
    if isinstance(paused_until, dt.datetime) and now < paused_until:
        return False
    cooldown_until = state.get("cooldown_until")
    if isinstance(cooldown_until, dt.datetime) and now < cooldown_until:
        return False

    if bool(getattr(config, "SCALP_BTC_BLOCK_WHEN_MAIN_HOLDING", False)):
        if _count_strategy_holdings(main_state) > 0:
            return False

    if bool(main_state.get(ticker, {}).get("holding", False)):
        return False

    buy_krw = _get_scalp_btc_buy_krw(equity, krw)
    if buy_krw <= 0:
        return False

    if not bool(scalp_btc_entry_signal(ticker)):
        return False

    lock_timeout = float(getattr(config, "SCALP_BTC_LOCK_TIMEOUT_SEC", 5))
    if not ticker_lock.acquire(ticker, now, lock_timeout):
        print(f"[SCALP_BTC] deferred: lock busy {ticker}")
        return False

    try:
        cur = prices.get(ticker)
        if cur is None or float(cur) <= 0:
            return False
        cur = float(cur)

        if bool(getattr(config, "REAL_ORDER", False)):
            upbit.buy_market_order(ticker, buy_krw)
            qty, entry = wait_for_filled_snapshot(upbit, ticker, timeout_sec=3.0, interval=0.2)
            qty = float(qty) if float(qty) > 0 else float(buy_krw) / cur
            entry = float(entry) if float(entry) > 0 else cur
            log_order("BUY", ticker, qty, True, "scalp_btc")
        else:
            qty = float(buy_krw) / cur
            entry = cur
            log_order("BUY", ticker, qty, True, "scalp_btc_mock")

        state["holding"] = True
        state["ticker"] = ticker
        state["entry_price"] = float(entry)
        state["qty"] = float(qty)
        state["entry_time"] = now
        state["peak_price"] = float(entry)
        print(f"[SCALP_BTC ENTRY] BUY {ticker} | KRW={buy_krw:,.0f}")
        persist_state_fn()
        return True
    except Exception as e:
        log_order("BUY", ticker, 0.0, False, f"scalp_btc_err={e}")
        print(f"[WARN] buy failed(SCALP_BTC): {ticker} err={e}")
        return False
    finally:
        ticker_lock.release(ticker)


def run():
    bot_mode = _resolve_mode()
    enable_main, enable_scalp_legacy, enable_scalp_btc, force_mock_order = _mode_to_strategy_flags(bot_mode)

    if force_mock_order and bool(getattr(config, "REAL_ORDER", False)):
        print("[MODE] TEST mode detected: force REAL_ORDER=False")
        config.REAL_ORDER = False

    access, secret = load_keys()
    upbit = pyupbit.Upbit(access, secret)

    if bool(getattr(config, "REAL_ORDER", False)):
        print("[WARN] REAL_ORDER=True (live order mode) | auto-start (confirmation disabled)")

    ensure_trade_log_header(config.TRADE_LOG_PATH)

    strategy_state, strategy_cooldowns, inactive_positions, scalp_btc_state = load_state()
    for s in STRATEGIES:
        strategy_state.setdefault(s, {})
        strategy_cooldowns.setdefault(s, {})

    verify_state_with_balance(upbit, strategy_state)
    repaired_dup = _repair_cross_strategy_duplicate_holdings(strategy_state)

    market_info = get_upbit_krw_markets()
    if market_info:
        print(f"[FILTER] loaded KRW market info: {len(market_info)}")
    else:
        print("[FILTER] market info unavailable. apply stable/user exclusions only")

    sanitize_repaired = 0
    moved_count = 0
    for s in STRATEGIES:
        active_state, moved_inactive, repaired, moved = sanitize_positions(strategy_state.get(s, {}), market_info)
        strategy_state[s] = active_state
        if moved_inactive:
            inactive_positions.update(moved_inactive)
        sanitize_repaired += int(repaired)
        moved_count += int(moved)
    sanitize_repaired += int(repaired_dup)
    if sanitize_repaired:
        print(f"[STATE] sanitize repaired fields: {sanitize_repaired}")
    if moved_count:
        print(f"[STATE] moved count: {moved_count}")

    def persist_state(*_args, **_kwargs):
        save_state(
            strategy_state,
            strategy_cooldowns,
            inactive_positions=inactive_positions,
            scalp_btc_state=scalp_btc_state,
        )

    if moved_count or repaired_dup:
        persist_state()

    now = now_kst()
    momentum_seen_at = {}
    surge_stoploss_until = {}
    ticker_lock = _TickerLock()

    raw_ranked = get_top_tickers_by_value(
        int(getattr(config, "UNIVERSE_SCAN_N", config.TOP_N)),
        market_info=market_info,
    )
    core_universe, surge_pool, inactive_universe, inactive_reasons, core_filter_rejects = _core_and_surge_from_ranked(
        raw_ranked, market_info
    )
    if enable_scalp_legacy:
        surge_candidates, momentum_seen_at = _update_surge_candidates(surge_pool, momentum_seen_at, now)
    else:
        surge_candidates = []
    active_universe = _dedupe_keep_order(core_universe + surge_candidates)
    inactive_tickers = set(inactive_universe) | set(inactive_positions.keys())
    main_filter_reject_stats = Counter(core_filter_rejects or {})
    main_filter_summary_last = now

    print_filter_summary(active_universe, inactive_universe, inactive_reasons)
    print(f"[UNIVERSE] core={len(core_universe)} surge={len(surge_candidates)}")
    if surge_candidates:
        print(f"[UNIVERSE] surge picks: {', '.join(surge_candidates[:8])}")

    k_map = build_k_map(core_universe) if enable_main else {}
    last_refresh = now
    last_status = now
    last_state_save = now
    trading_day = now.date()

    day_tp1_count_main = 0
    loss_seq_main = 0
    tp1_counted_main = {
        t
        for t, st in (strategy_state.get("MAIN", {}) or {}).items()
        if bool(st.get("holding", False)) and bool(st.get("tp1", False))
    }
    main_entry_blocked_prev = False

    day_cache = {}
    intraday_cache = {}
    minute_cache = {}

    print(
        f"[BOT] start | MODE={bot_mode} | REAL_ORDER={config.REAL_ORDER} "
        f"| MAIN={'ON' if enable_main else 'OFF'} "
        f"SCALP_BTC={'ON' if enable_scalp_btc else 'OFF'} "
        f"LEGACY_SCALP={'ON' if enable_scalp_legacy else 'OFF'}"
    )

    while True:
        try:
            now = now_kst()
            main_entry_intent = None

            if (now - last_refresh).total_seconds() >= float(config.REFRESH_MIN) * 60.0:
                print("\n[REFRESH] universe")
                raw_ranked = get_top_tickers_by_value(
                    int(getattr(config, "UNIVERSE_SCAN_N", config.TOP_N)),
                    market_info=market_info,
                )
                (
                    core_universe,
                    surge_pool,
                    inactive_universe,
                    inactive_reasons,
                    core_filter_rejects,
                ) = _core_and_surge_from_ranked(raw_ranked, market_info)

                if enable_scalp_legacy:
                    surge_candidates, momentum_seen_at = _update_surge_candidates(surge_pool, momentum_seen_at, now)
                else:
                    surge_candidates = []
                active_universe = _dedupe_keep_order(core_universe + surge_candidates)
                inactive_tickers = set(inactive_universe) | set(inactive_positions.keys())
                main_filter_reject_stats.update(core_filter_rejects or {})

                print_filter_summary(active_universe, inactive_universe, inactive_reasons)
                print(f"[UNIVERSE] core={len(core_universe)} surge={len(surge_candidates)}")
                if surge_candidates:
                    print(f"[UNIVERSE] surge picks: {', '.join(surge_candidates[:8])}")

                if enable_main:
                    k_map = build_k_map(core_universe)
                last_refresh = now

            main_filter_summary_min = float(getattr(config, "MAIN_FILTER_REJECT_SUMMARY_MIN", 10))
            main_filter_summary_sec = max(60.0, main_filter_summary_min * 60.0)
            if (now - main_filter_summary_last).total_seconds() >= main_filter_summary_sec:
                print_main_filter_reject_summary(main_filter_reject_stats)
                main_filter_reject_stats.clear()
                main_filter_summary_last = now

            if now.date() != trading_day:
                trading_day = now.date()
                day_tp1_count_main = 0
                loss_seq_main = 0
                tp1_counted_main = set()
                main_entry_blocked_prev = False
                print("[DAY_RESET] counters reset")

            regime = "FULL"
            if bool(getattr(config, "USE_MARKET_REGIME", False)):
                try:
                    regime = get_market_regime()
                except Exception:
                    regime = "MID"

            btc_ticker = str(getattr(config, "SCALP_BTC_TICKER", "KRW-BTC"))
            holding_tickers = _all_holding_tickers_with_scalp_btc(strategy_state, scalp_btc_state)
            inactive_holding_tickers = [t for t, s in (inactive_positions or {}).items() if s.get("holding", False)]

            price_targets = set(core_universe) | set(holding_tickers) | set(inactive_holding_tickers)
            if enable_scalp_legacy:
                price_targets |= set(surge_candidates)
            if enable_scalp_btc:
                price_targets.add(btc_ticker)
            prices = batch_get_prices(price_targets)

            krw = float(get_balance(upbit, "KRW"))
            prices["_krw"] = krw
            prices["_caches"] = (day_cache, intraday_cache, minute_cache)

            equity = estimate_equity(krw, strategy_state, prices, upbit, inactive_positions=inactive_positions)
            if _scalp_btc_is_holding(scalp_btc_state):
                btc_px = prices.get(btc_ticker)
                if btc_px is not None:
                    qty = float(scalp_btc_state.get("qty", 0.0))
                    if bool(getattr(config, "REAL_ORDER", False)):
                        qty = float(get_balance(upbit, btc_ticker.split("-")[1]))
                    if qty > 0:
                        equity += qty * float(btc_px)

            base_per_trade, base_max_holdings = get_base_position_settings(equity)
            per_trade_main, max_holdings = apply_market_regime(equity, base_per_trade, base_max_holdings, regime)
            h_key, h_scale = apply_runtime_params_by_holdings(max_holdings)

            total_holding = _count_total_holdings_with_scalp_btc(
                strategy_state, scalp_btc_state, include_legacy_scalp=enable_scalp_legacy
            )

            day_tp1_count_main = update_day_tp1_counter(
                strategy_state.get("MAIN", {}),
                tp1_counted_main,
                day_tp1_count_main,
                "MAIN",
            )

            tp1_limit = int(getattr(config, "DAILY_TP1_EXIT_LIMIT", 3))
            main_loss_limit = int(getattr(config, "MAIN_CONSEC_LOSS_LIMIT", getattr(config, "CONSEC_LOSS_EXIT_LIMIT", 4)))
            main_entry_allowed = (day_tp1_count_main < tp1_limit) and (loss_seq_main < main_loss_limit)

            blocked_main = not main_entry_allowed
            if blocked_main and not main_entry_blocked_prev:
                if day_tp1_count_main >= tp1_limit:
                    print(f"[ENTRY_BLOCK][MAIN] reason=TP1_LIMIT count={day_tp1_count_main}")
                if loss_seq_main >= main_loss_limit:
                    print(f"[ENTRY_BLOCK][MAIN] reason=LOSS_STREAK count={loss_seq_main}")
            main_entry_blocked_prev = blocked_main

            if (now - last_status).total_seconds() >= float(config.STATUS_PRINT_SEC):
                main_h = _count_strategy_holdings(strategy_state.get("MAIN", {}))
                legacy_h = _count_strategy_holdings(strategy_state.get("SCALP", {})) if enable_scalp_legacy else 0
                scalp_btc_h = 1 if _scalp_btc_is_holding(scalp_btc_state) else 0
                pause_txt = "-"
                paused_until = scalp_btc_state.get("paused_until")
                if isinstance(paused_until, dt.datetime) and now < paused_until:
                    pause_txt = f"{max(0, int((paused_until - now).total_seconds() / 60))}m"
                print(
                    f"[STATUS] Regime={regime} | Equity~{equity:,.0f} | PerTrade~{per_trade_main:,.0f} | "
                    f"Holding={total_holding}/{max_holdings} | MAIN={main_h} LEGACY={legacy_h} SCALP_BTC={scalp_btc_h} | "
                    f"Core={len(core_universe)} Surge={len(surge_candidates)} | "
                    f"HKey={h_key} HScale={h_scale:.2f} | TP1_MAIN={day_tp1_count_main} "
                    f"Loss_MAIN={loss_seq_main} SBtcLoss={int(scalp_btc_state.get('loss_streak', 0))} SBtcPause={pause_txt}"
                )
                last_status = now

            # 1) MAIN position management
            if enable_main:
                events_main = manage_positions(
                    upbit=upbit,
                    now=now,
                    state=strategy_state["MAIN"],
                    prices=prices,
                    cooldown_until=strategy_cooldowns["MAIN"],
                    save_state_fn=persist_state,
                    inactive_tickers=inactive_tickers,
                    inactive_positions=inactive_positions,
                    strategy="MAIN",
                )
                if events_main:
                    loss_seq_main = update_loss_seq_from_events(events_main, loss_seq_main, "MAIN")
                    day_tp1_count_main = update_day_tp1_counter(
                        strategy_state.get("MAIN", {}),
                        tp1_counted_main,
                        day_tp1_count_main,
                        "MAIN",
                    )

            # 2) SCALP_BTC position management
            if enable_scalp_btc:
                _manage_scalp_btc_position(
                    upbit=upbit,
                    now=now,
                    state=scalp_btc_state,
                    prices=prices,
                    persist_state_fn=persist_state,
                )

            # 2b) Legacy SCALP position management (optional)
            if enable_scalp_legacy:
                events_scalp = manage_positions(
                    upbit=upbit,
                    now=now,
                    state=strategy_state["SCALP"],
                    prices=prices,
                    cooldown_until=strategy_cooldowns["SCALP"],
                    save_state_fn=persist_state,
                    inactive_tickers=inactive_tickers,
                    inactive_positions=inactive_positions,
                    strategy="SCALP",
                )
                if events_scalp:
                    block_min = int(getattr(config, "SURGE_STOPLOSS_REENTRY_BLOCK_MIN", 30))
                    for e in events_scalp:
                        pnl = float(e.get("pnl_pct", 0.0))
                        reason = str(e.get("reason", ""))
                        if pnl < 0 and reason in {"stop_loss", "trailing"}:
                            t = str(e.get("ticker", ""))
                            if t:
                                surge_stoploss_until[t] = now + dt.timedelta(minutes=block_min)
                                print(f"[SURGE_BLOCK] {t} blocked {block_min}m ({reason})")

            total_holding = _count_total_holdings_with_scalp_btc(
                strategy_state, scalp_btc_state, include_legacy_scalp=enable_scalp_legacy
            )

            # 3) MAIN entry scan (intent at order-finalization)
            if enable_main and main_entry_allowed and total_holding < max_holdings and float(per_trade_main) > 0:
                def before_main_buy(ticker: str, buy_krw: float, cur: float):
                    nonlocal main_entry_intent
                    if ticker != btc_ticker:
                        return True

                    main_entry_intent = {"strategy": "MAIN", "ticker": ticker, "ts": now}
                    try:
                        if not _scalp_btc_is_holding(scalp_btc_state):
                            return True

                        lock_timeout = float(getattr(config, "SCALP_BTC_LOCK_TIMEOUT_SEC", 5))
                        if not ticker_lock.acquire(ticker, now, lock_timeout):
                            print("[SWITCH] lock busy -> main BTC entry deferred")
                            return False

                        try:
                            ok, err = _scalp_btc_close_position(
                                upbit=upbit,
                                now=now,
                                state=scalp_btc_state,
                                prices=prices,
                                reason="switch_to_main",
                                persist_state_fn=persist_state,
                            )
                        finally:
                            ticker_lock.release(ticker)

                        if ok:
                            scalp_btc_state["switch_fail_count"] = 0
                            persist_state()
                            return True

                        print("[SWITCH fail] scalp close failed -> main BTC entry deferred")
                        cnt = int(scalp_btc_state.get("switch_fail_count", 0)) + 1
                        scalp_btc_state["switch_fail_count"] = cnt
                        limit = int(getattr(config, "SCALP_BTC_SWITCH_FAIL_LIMIT", 3))
                        if cnt >= limit:
                            pause_min = int(getattr(config, "SCALP_BTC_SWITCH_FAIL_PAUSE_MIN", 60))
                            scalp_btc_state["paused_until"] = now + dt.timedelta(minutes=max(1, pause_min))
                            scalp_btc_state["switch_fail_count"] = 0
                            print("[SCALP_BTC_PAUSE] paused 60m due to repeated switch failures")
                        persist_state()
                        return False
                    finally:
                        main_entry_intent = None

                try:
                    did_main = try_main_entries(
                        upbit=upbit,
                        now=now,
                        universe=core_universe,
                        prices=prices,
                        k_map=k_map,
                        state=strategy_state["MAIN"],
                        cooldown_until=strategy_cooldowns["MAIN"],
                        per_trade_amt=float(per_trade_main),
                        max_holdings=max_holdings,
                        total_holding_cnt=total_holding,
                        regime=regime,
                        wait_for_filled_snapshot_fn=wait_for_filled_snapshot,
                        save_state_fn=persist_state,
                        inactive_tickers=inactive_tickers,
                        inactive_positions=inactive_positions,
                        global_holding_tickers=_all_holding_tickers_with_scalp_btc(strategy_state, scalp_btc_state),
                        before_buy_fn=before_main_buy,
                    )
                finally:
                    main_entry_intent = None

                if did_main:
                    total_holding = _count_total_holdings_with_scalp_btc(
                        strategy_state, scalp_btc_state, include_legacy_scalp=enable_scalp_legacy
                    )

            # 4) Legacy SCALP entry (optional)
            if enable_scalp_legacy and total_holding < max_holdings:
                scalp_universe = []
                for ticker in surge_candidates:
                    until = surge_stoploss_until.get(ticker)
                    if until is not None and now < until:
                        continue
                    scalp_universe.append(ticker)

                scalp_buy_krw = float(getattr(config, "MINUTE_TEST_PER_TRADE_KRW", config.TEST_PER_TRADE_KRW))
                did_legacy = try_scalp_entries(
                    upbit=upbit,
                    now=now,
                    universe=scalp_universe,
                    prices=prices,
                    state=strategy_state["SCALP"],
                    cooldown_until=strategy_cooldowns["SCALP"],
                    per_trade_amt=scalp_buy_krw,
                    max_holdings=max_holdings,
                    total_holding_cnt=total_holding,
                    regime=regime,
                    wait_for_filled_snapshot_fn=wait_for_filled_snapshot,
                    save_state_fn=persist_state,
                    inactive_tickers=inactive_tickers,
                    inactive_positions=inactive_positions,
                    global_holding_tickers=_all_holding_tickers_with_scalp_btc(strategy_state, scalp_btc_state),
                    conservative=False,
                )
                if did_legacy:
                    total_holding = _count_total_holdings_with_scalp_btc(
                        strategy_state, scalp_btc_state, include_legacy_scalp=enable_scalp_legacy
                    )

            # 5) SCALP_BTC entry (always last)
            if enable_scalp_btc and total_holding < max_holdings:
                did_scalp_btc = _try_scalp_btc_entry(
                    upbit=upbit,
                    now=now,
                    state=scalp_btc_state,
                    prices=prices,
                    equity=equity,
                    krw=krw,
                    max_holdings=max_holdings,
                    total_holding=total_holding,
                    main_state=strategy_state["MAIN"],
                    persist_state_fn=persist_state,
                    ticker_lock=ticker_lock,
                )
                if did_scalp_btc:
                    total_holding = _count_total_holdings_with_scalp_btc(
                        strategy_state, scalp_btc_state, include_legacy_scalp=enable_scalp_legacy
                    )

            if (now - last_state_save).total_seconds() >= float(config.STATE_SAVE_INTERVAL_SEC):
                persist_state()
                last_state_save = now

            time.sleep(float(config.POLL_SEC))

        except KeyboardInterrupt:
            print("\n사용자 종료(Ctrl+C)")
            persist_state()
            break
        except Exception as e:
            print(f"[ERROR] {type(e).__name__}: {e}")
            traceback.print_exc(limit=2)
            time.sleep(1)


if __name__ == "__main__":
    run()
