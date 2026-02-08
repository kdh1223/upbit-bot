# bot.py
import copy
import csv
import datetime as dt
import os
import sys
import time
from collections import Counter

import pyupbit

import config
import position_manager
from engine_entry import try_main_entries, try_scalp_entries
from engine_manage import manage_positions
from indicators import check_filters, detect_momentum_candidate, get_market_regime, intraday_trend_ok
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
    core_filtered = []
    for ticker in core_active:
        try:
            if not bool(check_filters(ticker)):
                continue
            if bool(getattr(config, "USE_INTRADAY_FILTER", False)) and (not bool(intraday_trend_ok(ticker))):
                continue
            core_filtered.append(ticker)
        except Exception:
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
    return core_final, surge_active, inactive_all, reasons_all


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


def _mode_to_strategy_flags(bot_mode: str):
    mode = str(bot_mode or "").upper().strip()
    if mode == "MAIN":
        return True, False
    if mode == "TEST":
        return False, True
    if mode == "DUAL":
        return True, True
    return bool(getattr(config, "ENABLE_MAIN_STRATEGY", True)), bool(getattr(config, "ENABLE_SCALP_STRATEGY", True))


def run():
    bot_mode = str(getattr(config, "BOT_MODE", "TEST")).upper().strip()
    enable_main, enable_scalp = _mode_to_strategy_flags(bot_mode)

    access, secret = load_keys()
    upbit = pyupbit.Upbit(access, secret)

    if bool(getattr(config, "REAL_ORDER", False)) and bool(getattr(config, "REQUIRE_ORDER_CONFIRM", False)):
        print("[WARN] REAL_ORDER=True (live order mode)")
        ans = input("정말 실전 매매를 시작할까요? 진행하려면 'yes' 입력: ").strip().lower()
        if ans != "yes":
            print("[STOP] canceled")
            return

    ensure_trade_log_header(config.TRADE_LOG_PATH)

    strategy_state, strategy_cooldowns, inactive_positions = load_state()
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
        save_state(strategy_state, strategy_cooldowns, inactive_positions=inactive_positions)

    if moved_count or repaired_dup:
        persist_state()

    now = now_kst()
    momentum_seen_at = {}
    surge_stoploss_until = {}

    raw_ranked = get_top_tickers_by_value(
        int(getattr(config, "UNIVERSE_SCAN_N", config.TOP_N)),
        market_info=market_info,
    )
    core_universe, surge_pool, inactive_universe, inactive_reasons = _core_and_surge_from_ranked(raw_ranked, market_info)
    surge_candidates, momentum_seen_at = _update_surge_candidates(surge_pool, momentum_seen_at, now)
    active_universe = _dedupe_keep_order(core_universe + surge_candidates)
    inactive_tickers = set(inactive_universe) | set(inactive_positions.keys())

    print_filter_summary(active_universe, inactive_universe, inactive_reasons)
    print(f"[UNIVERSE] core={len(core_universe)} surge={len(surge_candidates)}")
    if surge_candidates:
        print(f"[UNIVERSE] surge picks: {', '.join(surge_candidates[:8])}")

    k_map = build_k_map(core_universe) if enable_main else {}
    last_refresh = now
    last_status = now
    last_state_save = now
    trading_day = now.date()

    day_tp1_count = {s: 0 for s in STRATEGIES}
    loss_seq = {s: 0 for s in STRATEGIES}
    scalp_pause_until = None
    tp1_counted = {
        s: {
            t
            for t, st in (strategy_state.get(s, {}) or {}).items()
            if bool(st.get("holding", False)) and bool(st.get("tp1", False))
        }
        for s in STRATEGIES
    }
    entry_blocked_prev = {s: False for s in STRATEGIES}

    day_cache = {}
    intraday_cache = {}
    minute_cache = {}

    print(
        f"[BOT] start | MODE={bot_mode} | REAL_ORDER={config.REAL_ORDER} "
        f"| MAIN={'ON' if enable_main else 'OFF'} SCALP={'ON' if enable_scalp else 'OFF'}"
    )

    while True:
        try:
            now = now_kst()

            if (now - last_refresh).total_seconds() >= float(config.REFRESH_MIN) * 60.0:
                print("\n[REFRESH] universe")
                raw_ranked = get_top_tickers_by_value(
                    int(getattr(config, "UNIVERSE_SCAN_N", config.TOP_N)),
                    market_info=market_info,
                )
                core_universe, surge_pool, inactive_universe, inactive_reasons = _core_and_surge_from_ranked(
                    raw_ranked, market_info
                )
                surge_candidates, momentum_seen_at = _update_surge_candidates(surge_pool, momentum_seen_at, now)
                active_universe = _dedupe_keep_order(core_universe + surge_candidates)
                inactive_tickers = set(inactive_universe) | set(inactive_positions.keys())

                print_filter_summary(active_universe, inactive_universe, inactive_reasons)
                print(f"[UNIVERSE] core={len(core_universe)} surge={len(surge_candidates)}")
                if surge_candidates:
                    print(f"[UNIVERSE] surge picks: {', '.join(surge_candidates[:8])}")

                if enable_main:
                    k_map = build_k_map(core_universe)
                last_refresh = now

            today = now.date()
            if today != trading_day:
                trading_day = today
                day_tp1_count = {s: 0 for s in STRATEGIES}
                loss_seq = {s: 0 for s in STRATEGIES}
                scalp_pause_until = None
                tp1_counted = {s: set() for s in STRATEGIES}
                entry_blocked_prev = {s: False for s in STRATEGIES}
                print("[DAY_RESET] counters reset")

            regime = "FULL"
            if bool(getattr(config, "USE_MARKET_REGIME", False)):
                try:
                    regime = get_market_regime()
                except Exception:
                    regime = "MID"

            holding_tickers = _all_holding_tickers(strategy_state)
            inactive_holding_tickers = [t for t, s in (inactive_positions or {}).items() if s.get("holding", False)]
            price_targets = set(core_universe) | set(surge_candidates) | set(holding_tickers) | set(inactive_holding_tickers)
            prices = batch_get_prices(price_targets)

            krw = float(get_balance(upbit, "KRW"))
            prices["_krw"] = krw
            prices["_caches"] = (day_cache, intraday_cache, minute_cache)

            equity = estimate_equity(krw, strategy_state, prices, upbit, inactive_positions=inactive_positions)
            base_per_trade, base_max_holdings = get_base_position_settings(equity)
            per_trade_main, max_holdings = apply_market_regime(equity, base_per_trade, base_max_holdings, regime)
            h_key, h_scale = apply_runtime_params_by_holdings(max_holdings)

            total_holding = _count_total_holdings(strategy_state)

            for s in STRATEGIES:
                day_tp1_count[s] = update_day_tp1_counter(
                    strategy_state.get(s, {}),
                    tp1_counted[s],
                    day_tp1_count[s],
                    s,
                )

            tp1_limit = int(getattr(config, "DAILY_TP1_EXIT_LIMIT", 3))
            main_loss_limit = int(getattr(config, "MAIN_CONSEC_LOSS_LIMIT", getattr(config, "CONSEC_LOSS_EXIT_LIMIT", 4)))
            scalp_loss_limit = int(
                getattr(config, "SCALP_CONSEC_LOSS_LIMIT", getattr(config, "CONSEC_LOSS_EXIT_LIMIT", 4))
            )

            entry_allowed = {
                "MAIN": (day_tp1_count["MAIN"] < tp1_limit) and (loss_seq["MAIN"] < main_loss_limit),
                "SCALP": (day_tp1_count["SCALP"] < tp1_limit)
                and (loss_seq["SCALP"] < scalp_loss_limit)
                and (scalp_pause_until is None or now >= scalp_pause_until),
            }

            for s in STRATEGIES:
                blocked = not entry_allowed[s]
                if blocked and not entry_blocked_prev[s]:
                    if day_tp1_count[s] >= tp1_limit:
                        print(f"[ENTRY_BLOCK][{s}] reason=TP1_LIMIT count={day_tp1_count[s]}")
                    if (s == "MAIN" and loss_seq[s] >= main_loss_limit) or (
                        s == "SCALP" and loss_seq[s] >= scalp_loss_limit
                    ):
                        print(f"[ENTRY_BLOCK][{s}] reason=LOSS_STREAK count={loss_seq[s]}")
                    if s == "SCALP" and scalp_pause_until is not None and now < scalp_pause_until:
                        remain = int((scalp_pause_until - now).total_seconds() / 60)
                        print(f"[ENTRY_BLOCK][SCALP] reason=PAUSE remain_min={max(0, remain)}")
                entry_blocked_prev[s] = blocked

            if (now - last_status).total_seconds() >= float(config.STATUS_PRINT_SEC):
                main_h = _count_strategy_holdings(strategy_state.get("MAIN", {}))
                scalp_h = _count_strategy_holdings(strategy_state.get("SCALP", {}))
                pause_txt = "-"
                if scalp_pause_until is not None and now < scalp_pause_until:
                    pause_txt = f"{max(0, int((scalp_pause_until - now).total_seconds() / 60))}m"
                print(
                    f"[STATUS] Regime={regime} | Equity~{equity:,.0f} | PerTrade~{per_trade_main:,.0f} | "
                    f"Holding={total_holding}/{max_holdings} | MAIN={main_h} SCALP={scalp_h} | "
                    f"Core={len(core_universe)} Surge={len(surge_candidates)} | "
                    f"HKey={h_key} HScale={h_scale:.2f} | TP1(M/S)={day_tp1_count['MAIN']}/{day_tp1_count['SCALP']} "
                    f"Loss(M/S)={loss_seq['MAIN']}/{loss_seq['SCALP']} | SPause={pause_txt}"
                )
                last_status = now

            # MAIN entry
            if enable_main and entry_allowed["MAIN"] and total_holding < max_holdings:
                if float(per_trade_main) > 0:
                    _ = try_main_entries(
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
                        global_holding_tickers=_all_holding_tickers(strategy_state),
                    )
                    if _:
                        total_holding = _count_total_holdings(strategy_state)

            # SCALP entry
            if enable_scalp and entry_allowed["SCALP"] and total_holding < max_holdings:
                if _is_dawn_hour(now) and bool(getattr(config, "SCALP_DAWN_BLOCK", False)):
                    pass
                else:
                    conservative = False
                    if _is_dawn_hour(now) and bool(getattr(config, "SCALP_DAWN_CONSERVATIVE", True)):
                        conservative = True
                    if loss_seq["SCALP"] >= int(getattr(config, "SCALP_LOSSSEQ_CONSERVATIVE_TRIGGER", 2)):
                        conservative = True

                    scalp_universe = []
                    for ticker in surge_candidates:
                        until = surge_stoploss_until.get(ticker)
                        if until is not None and now < until:
                            continue
                        scalp_universe.append(ticker)

                    scalp_buy_krw = float(getattr(config, "MINUTE_TEST_PER_TRADE_KRW", config.TEST_PER_TRADE_KRW))
                    _ = try_scalp_entries(
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
                        global_holding_tickers=_all_holding_tickers(strategy_state),
                        conservative=conservative,
                    )
                    if _:
                        total_holding = _count_total_holdings(strategy_state)

            # Position management per strategy
            for s in STRATEGIES:
                events = manage_positions(
                    upbit=upbit,
                    now=now,
                    state=strategy_state[s],
                    prices=prices,
                    cooldown_until=strategy_cooldowns[s],
                    save_state_fn=persist_state,
                    inactive_tickers=inactive_tickers,
                    inactive_positions=inactive_positions,
                    strategy=s,
                )
                if events:
                    prev_loss = int(loss_seq[s])
                    loss_seq[s] = update_loss_seq_from_events(events, loss_seq[s], s)
                    if s == "SCALP":
                        if bool(getattr(config, "SCALP_PAUSE_ON_LOSSSEQ", True)):
                            trig = int(getattr(config, "SCALP_PAUSE_LOSSSEQ_TRIGGER", 2))
                            pause_min = int(getattr(config, "SCALP_PAUSE_MINUTES", 60))
                            if prev_loss < trig <= int(loss_seq[s]):
                                scalp_pause_until = now + dt.timedelta(minutes=max(1, pause_min))
                                print(
                                    f"[SCALP_PAUSE] reason=LOSS_STREAK "
                                    f"loss_seq={loss_seq[s]} until={scalp_pause_until.strftime('%H:%M:%S')}"
                                )
                        block_min = int(getattr(config, "SURGE_STOPLOSS_REENTRY_BLOCK_MIN", 30))
                        for e in events:
                            pnl = float(e.get("pnl_pct", 0.0))
                            reason = str(e.get("reason", ""))
                            if pnl < 0 and reason in {"stop_loss", "trailing"}:
                                t = str(e.get("ticker", ""))
                                if t:
                                    surge_stoploss_until[t] = now + dt.timedelta(minutes=block_min)
                                    print(f"[SURGE_BLOCK] {t} blocked {block_min}m ({reason})")

                day_tp1_count[s] = update_day_tp1_counter(
                    strategy_state.get(s, {}),
                    tp1_counted[s],
                    day_tp1_count[s],
                    s,
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
            print("[ERROR]", e)
            time.sleep(1)


if __name__ == "__main__":
    run()
