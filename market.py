"""업비트 시세/계정 조회와 유니버스 메타 필터링을 담당하는 마켓 유틸."""

import os
import time
from typing import Dict, List, Tuple

import pyupbit
import requests
from dotenv import load_dotenv

import config


STABLECOIN_SYMBOLS = {"USDT", "USDC", "BUSD", "TUSD", "DAI", "FDUSD", "USDP"}
USER_EXCLUDED_SYMBOLS = {"APENFT", "LUNA2", "LUNC"}
_MARKET_META_DEBUG_PRINTED = False
_BALANCE_WARN_LAST_TS = 0.0


def _extract_symbol(ticker: str) -> str:
    if not isinstance(ticker, str):
        return ""
    if "-" in ticker:
        return ticker.split("-", 1)[1].upper().strip()
    return ticker.upper().strip()


def load_keys() -> Tuple[str, str]:
    env_path = os.path.join(os.getcwd(), ".env")
    try:
        load_dotenv(dotenv_path=env_path)
    except Exception:
        # python-dotenv may fail in some stdin/embedded contexts.
        pass

    access = str(os.getenv("UPBIT_ACCESS") or "").strip().strip("'").strip('"')
    secret = str(os.getenv("UPBIT_SECRET") or "").strip().strip("'").strip('"')
    if not access or not secret:
        raise RuntimeError(".env missing UPBIT_ACCESS / UPBIT_SECRET")
    return access, secret


def get_current_price(ticker: str) -> float:
    p = pyupbit.get_current_price(ticker)
    if p is None:
        raise RuntimeError(f"현재가 조회 실패: {ticker}")
    return float(p)


def _warn_balance_issue_once(msg: str):
    global _BALANCE_WARN_LAST_TS
    now_ts = time.time()
    if (now_ts - _BALANCE_WARN_LAST_TS) < 30.0:
        return
    _BALANCE_WARN_LAST_TS = now_ts
    print(f"[WARN] balance fetch issue: {msg}")


def _pick_balance_fields_from_accounts(accounts, currency: str) -> Tuple[float, float]:
    cur = str(currency or "").upper().strip()
    if not cur:
        return 0.0, 0.0

    for row in accounts:
        if not isinstance(row, dict):
            continue
        coin = str(row.get("currency") or "").upper().strip()
        if coin != cur:
            continue
        try:
            bal = float(row.get("balance") or 0.0)
        except Exception:
            bal = 0.0
        try:
            locked = float(row.get("locked") or 0.0)
        except Exception:
            locked = 0.0
        return max(0.0, bal), max(0.0, locked)
    return 0.0, 0.0


def get_balance_info(upbit: pyupbit.Upbit, currency: str) -> Tuple[float, float]:
    """
    Safe balance fetch wrapper.
    Avoids noisy pyupbit.get_balance() exception-class prints and handles
    unexpected payloads defensively.

    Returns:
    - available balance
    - total balance (available + locked)
    """
    try:
        accounts = upbit.get_balances()
    except Exception as e:
        _warn_balance_issue_once(f"{type(e).__name__}: {e}")
        return 0.0, 0.0

    if isinstance(accounts, list):
        bal, locked = _pick_balance_fields_from_accounts(accounts, currency)
        total = max(0.0, float(bal) + float(locked))
        return float(max(0.0, bal)), float(total)

    if isinstance(accounts, dict):
        err = accounts.get("error")
        if isinstance(err, dict):
            name = str(err.get("name") or "unknown")
            msg = str(err.get("message") or "")
            _warn_balance_issue_once(f"api_error name={name} msg={msg}")
        else:
            _warn_balance_issue_once(f"unexpected dict payload keys={list(accounts.keys())[:5]}")
        return 0.0, 0.0

    _warn_balance_issue_once(f"unexpected payload type={type(accounts).__name__}")
    return 0.0, 0.0


def get_balance(upbit: pyupbit.Upbit, currency: str) -> float:
    bal, _ = get_balance_info(upbit, currency)
    return float(max(0.0, bal))


def get_total_balance(upbit: pyupbit.Upbit, currency: str) -> float:
    _, total = get_balance_info(upbit, currency)
    return float(max(0.0, total))


def _pick_balance_from_accounts(accounts, currency: str) -> float:
    # Backward-compatible helper used by legacy call sites/tests.
    bal, _ = _pick_balance_fields_from_accounts(accounts, currency)
    return float(max(0.0, bal))
    return 0.0


def filter_stablecoins(tickers):
    filtered = []
    for ticker in tickers:
        symbol = _extract_symbol(ticker)
        if symbol in STABLECOIN_SYMBOLS:
            continue
        filtered.append(ticker)
    return filtered


def _normalize_market_warning(raw_warning: str) -> str:
    s = str(raw_warning or "").strip()
    if not s:
        return "NONE"

    sup = s.upper().strip()
    if sup in {"NONE", "NORMAL", "OK"}:
        return "NONE"

    caution_tokens = (
        "CAUTION",
        "WARNING",
        "ALERT",
        "RISK",
        "HALT",
        "SUSPEND",
        "DELIST",
        "MANAGE",
        "주의",
        "유의",
        "경고",
        "위험",
        "거래정지",
        "상장폐지",
        "관리",
    )
    if any(tok in s for tok in caution_tokens) or any(tok in sup for tok in caution_tokens):
        return "CAUTION"

    return sup


def get_upbit_krw_markets(timeout_sec: float = 5.0) -> Dict[str, Dict[str, str]]:
    global _MARKET_META_DEBUG_PRINTED
    try:
        resp = requests.get(
            "https://api.upbit.com/v1/market/all",
            params={"isDetails": "true"},
            timeout=timeout_sec,
        )
        resp.raise_for_status()
        rows = resp.json()
    except Exception as e:
        print(f"[WARN] failed to load KRW market info: {e}")
        return {}

    markets: Dict[str, Dict[str, str]] = {}
    debug_rows = []
    for row in rows:
        market = str(row.get("market") or "").upper().strip()
        if not market.startswith("KRW-"):
            continue

        raw_warning = ""
        warning_key = None
        for key in ("market_warning", "warning", "marketWarning", "market_warning_flag"):
            if key in row:
                warning_key = key
                raw_warning = row.get(key)
                break

        if warning_key is None:
            raw_warning = "NONE"

        market_warning = _normalize_market_warning(raw_warning)

        markets[market] = {
            "korean_name": str(row.get("korean_name") or "").strip(),
            "english_name": str(row.get("english_name") or "").strip(),
            "market_warning": market_warning,
        }

        if warning_key not in (None, "market_warning") and len(debug_rows) < 3:
            debug_rows.append(
                {
                    "market": market,
                    "warning_key": warning_key,
                    "raw_warning": str(raw_warning),
                    "normalized": market_warning,
                }
            )

    if debug_rows and not _MARKET_META_DEBUG_PRINTED:
        print(f"[DEBUG] market warning key fallback sample(3): {debug_rows}")
        _MARKET_META_DEBUG_PRINTED = True

    return markets


def filter_tradeable_tickers(tickers, market_info) -> Tuple[List[str], List[str], Dict[str, str]]:
    active: List[str] = []
    inactive: List[str] = []
    reasons: Dict[str, str] = {}

    seen = set()
    use_market_registry = isinstance(market_info, dict) and len(market_info) > 0
    exclude_caution = bool(getattr(config, "EXCLUDE_CAUTION", True))

    for ticker in tickers:
        if not isinstance(ticker, str):
            continue

        t = ticker.upper().strip()
        if not t or t in seen:
            continue
        seen.add(t)

        symbol = _extract_symbol(t)
        reason = ""

        if symbol in STABLECOIN_SYMBOLS:
            reason = "STABLECOIN"
        elif symbol in USER_EXCLUDED_SYMBOLS:
            reason = "USER_EXCLUDED"
        elif use_market_registry:
            info = market_info.get(t)
            if info is None:
                reason = "NOT_LISTED_OR_HALTED"
            else:
                warning = _normalize_market_warning(info.get("market_warning") or info.get("warning") or "NONE")
                if exclude_caution and warning != "NONE":
                    reason = "CAUTION"

        if reason:
            inactive.append(t)
            reasons[t] = reason
        else:
            active.append(t)

    return active, inactive, reasons


def _chunked(items: List[str], size: int):
    size = max(1, int(size))
    for i in range(0, len(items), size):
        yield items[i : i + size]


def get_krw_market_snapshots_24h(
    market_info: Dict[str, Dict[str, str]] = None,
    sleep_sec: float = 0.03,
    chunk_size: int = 80,
) -> List[Dict[str, float]]:
    """
    Fetch KRW market snapshots and return rows sorted by rolling 24h traded value.
    """
    tickers = pyupbit.get_tickers(fiat="KRW")
    market_info = market_info or {}
    tickers, inactive, reasons = filter_tradeable_tickers(tickers, market_info)
    for t in inactive:
        reason = reasons.get(t, "UNKNOWN")
        if reason == "CAUTION":
            print(f"[FILTER] moved to inactive: {t} (CAUTION)")

    total = len(tickers)
    print(f"[SCAN] SNAP24 start: KRW active {total} tickers")

    out: List[Dict[str, float]] = []
    done = 0
    progress_log = bool(getattr(config, "SCAN_PROGRESS_LOG", False))
    for chunk in _chunked(tickers, chunk_size):
        # pyupbit does not provide get_ticker(); call Upbit ticker endpoint directly.
        try:
            resp = requests.get(
                "https://api.upbit.com/v1/ticker",
                params={"markets": ",".join(chunk)},
                timeout=5.0,
            )
            resp.raise_for_status()
            rows = resp.json()
        except Exception:
            rows = []

        for row in rows or []:
            try:
                market = str(row.get("market") or "").upper().strip()
                if not market:
                    continue
                trade_price = float(row.get("trade_price") or 0.0)
                value_24h = float(row.get("acc_trade_price_24h") or 0.0)
                volume_24h = float(row.get("acc_trade_volume_24h") or 0.0)
                if trade_price <= 0 or value_24h <= 0 or volume_24h <= 0:
                    continue  # treat as non-orderable snapshot

                out.append(
                    {
                        "market": market,
                        "trade_price": trade_price,
                        "acc_trade_price_24h": value_24h,
                        "acc_trade_volume_24h": volume_24h,
                    }
                )
            except Exception:
                continue

        done += len(chunk)
        if progress_log and total > 0:
            print(f"  진행: {done}/{total} ({done/total*100:.1f})", end="\r")
        time.sleep(sleep_sec)

    if progress_log:
        print()
    out.sort(key=lambda x: x["acc_trade_price_24h"], reverse=True)
    print("[SCAN] SNAP24 done")
    return out


def get_ranked_krw_by_24h_value(
    n: int,
    market_info: Dict[str, Dict[str, str]] = None,
    sleep_sec: float = 0.03,
) -> List[str]:
    """
    Return top N KRW tickers ranked by rolling 24h traded value.
    """
    snapshots = get_krw_market_snapshots_24h(market_info=market_info, sleep_sec=sleep_sec)
    top = [r["market"] for r in snapshots[: int(max(1, n))]]
    print(f"[SCAN] TOP{int(max(1, n))} done (rolling24h)")
    show_topn = max(0, int(getattr(config, "SCAN_LOG_TOPN", 0)))
    if show_topn > 0 and top:
        head = top[: min(show_topn, len(top))]
        print(f"[SCAN] TOP{len(head)} list: {', '.join(head)}")
    return top


def sanitize_positions(positions, market_info):
    active_positions = {}
    inactive_positions = {}
    repaired_count = 0
    moved_count = 0

    if not isinstance(positions, dict):
        return active_positions, inactive_positions, 1, 0

    _, inactive_tickers, reasons = filter_tradeable_tickers(list(positions.keys()), market_info)
    inactive_set = set(inactive_tickers)

    for ticker, raw in positions.items():
        pos = raw if isinstance(raw, dict) else {}
        if not isinstance(raw, dict):
            repaired_count += 1

        if ticker in inactive_set:
            reason = reasons.get(ticker, "UNKNOWN")
            if pos.get("inactive_reason") != reason:
                pos["inactive_reason"] = reason
                repaired_count += 1

            inactive_positions[ticker] = pos
            moved_count += 1
            print(f"[STATE] moved to inactive: {ticker} ({reason})")
            continue

        active_positions[ticker] = pos

    return active_positions, inactive_positions, repaired_count, moved_count


def get_top_tickers_by_value(
    n: int,
    sleep_sec: float = 0.03,
    market_info: Dict[str, Dict[str, str]] = None,
) -> List[str]:
    """
    KRW 마켓 거래대금(value) 기준 TOP N
    순서:
    1) KRW 전체
    2) STABLECOIN 제외
    3) CAUTION/비거래 대상 제외
    4) 남은 종목에서 거래대금 상위 N
    """
    return get_ranked_krw_by_24h_value(
        n=int(n),
        market_info=market_info,
        sleep_sec=sleep_sec,
    )
