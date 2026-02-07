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


def _extract_symbol(ticker: str) -> str:
    if not isinstance(ticker, str):
        return ""
    if "-" in ticker:
        return ticker.split("-", 1)[1].upper().strip()
    return ticker.upper().strip()


def load_keys() -> Tuple[str, str]:
    load_dotenv()
    access = os.getenv("UPBIT_ACCESS")
    secret = os.getenv("UPBIT_SECRET")
    if not access or not secret:
        raise RuntimeError(".env missing UPBIT_ACCESS / UPBIT_SECRET")
    return access, secret


def get_current_price(ticker: str) -> float:
    p = pyupbit.get_current_price(ticker)
    if p is None:
        raise RuntimeError(f"현재가 조회 실패: {ticker}")
    return float(p)


def get_balance(upbit: pyupbit.Upbit, currency: str) -> float:
    bal = upbit.get_balance(currency)
    return float(bal) if bal else 0.0


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

    caution_tokens = ("CAUTION", "WARNING", "ALERT", "주의", "유의", "경고")
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
                if exclude_caution and warning == "CAUTION":
                    reason = "CAUTION"

        if reason:
            inactive.append(t)
            reasons[t] = reason
        else:
            active.append(t)

    return active, inactive, reasons


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
    tickers = pyupbit.get_tickers(fiat="KRW")
    market_info = market_info or {}
    tickers, inactive, reasons = filter_tradeable_tickers(tickers, market_info)
    for t in inactive:
        reason = reasons.get(t, "UNKNOWN")
        if reason == "CAUTION":
            print(f"[FILTER] moved to inactive: {t} (CAUTION)")
    total = len(tickers)

    data = []
    print(f"[SCAN] TOP{n} start: KRW active {total} tickers")

    for i, t in enumerate(tickers, start=1):
        try:
            d = pyupbit.get_ohlcv(t, interval="day", count=1)
            if d is None or d.empty:
                continue

            if "value" in d.columns:
                value = float(d["value"].iloc[-1])
            else:
                value = float(d["volume"].iloc[-1]) * float(d["close"].iloc[-1])

            data.append((t, value))
        except Exception:
            pass

        if i % 15 == 0 or i == total:
            print(f"  진행: {i}/{total} ({i/total*100:.1f}%)", end="\r")

        time.sleep(sleep_sec)

    print()
    data.sort(key=lambda x: x[1], reverse=True)
    top = [t for t, _ in data[:n]]
    print(f"[SCAN] TOP{n} done")
    return top
