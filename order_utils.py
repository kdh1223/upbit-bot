"""시장가 매수 직후 체결 스냅샷을 확인하는 주문 유틸 모듈."""

import time
from typing import Tuple
import pyupbit


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
    실주문 직후: (balance, avg_buy_price) 유효해질 때까지 대기
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
