import os
import time
from typing import List, Tuple

import pyupbit
from dotenv import load_dotenv


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


def get_top_tickers_by_value(n: int, sleep_sec: float = 0.03) -> List[str]:
    """
    KRW 마켓 전체에서 거래대금(value) 기준 TOP N
    """
    tickers = pyupbit.get_tickers(fiat="KRW")
    total = len(tickers)

    data = []
    print(f"[SCAN] TOP{n} start: KRW {total} tickers")

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
