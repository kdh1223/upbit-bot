import time
from typing import Dict, List

import numpy as np
import pyupbit

import config


def calc_target(ticker: str, k: float) -> float:
    df = pyupbit.get_ohlcv(ticker, interval="day", count=2)
    if df is None or df.empty or len(df) < 2:
        raise RuntimeError(f"타겟 계산 실패: {ticker}")
    y = df.iloc[-2]
    t = df.iloc[-1]
    return float(t["open"] + (y["high"] - y["low"]) * k)


def _drop_bad(arr) -> np.ndarray:
    arr = np.asarray(arr, dtype=float)
    return arr[np.isfinite(arr)]


def choose_best_k(ticker: str) -> float:
    """
    최근 K_LOOKBACK_DAYS 일봉으로 후보 K 백테스트 → HPR 최대 K 선택
    - 비용은 왕복(COST_ROUNDTRIP_PCT)을 보수적으로 반영
    """
    df = pyupbit.get_ohlcv(ticker, interval="day", count=config.K_LOOKBACK_DAYS + 2)
    if df is None or df.empty or len(df) < 10:
        return config.K_DEFAULT

    high = df["high"]
    low = df["low"]
    open_ = df["open"]
    close = df["close"]

    best_k = config.K_DEFAULT
    best_hpr = -1.0

    cost = float(config.COST_ROUNDTRIP_PCT)  # 왕복 비용

    for k in config.K_CANDIDATES:
        rng = (high - low) * k
        target = open_ + rng.shift(1)

        # 돌파(진입)했을 때만 비용 반영
        # 단순화: target에 진입해서 종가에 청산한다고 가정(기존 방식 유지)
        ror = np.where(high > target, (close / target) * (1 - cost), 1.0)
        ror = _drop_bad(ror)
        if len(ror) < 5:
            continue

        hpr = float(np.cumprod(ror)[-1])
        if hpr > best_hpr:
            best_hpr = hpr
            best_k = float(k)

    return best_k


def build_k_map(universe: List[str], sleep_sec: float = 0.05) -> Dict[str, float]:
    k_map: Dict[str, float] = {}

    if not config.AUTO_K:
        for t in universe:
            k_map[t] = config.K_DEFAULT
        return k_map

    print(
        f"🧠 K 자동화 계산 (lookback={config.K_LOOKBACK_DAYS}d, "
        f"candidates={len(config.K_CANDIDATES)}, cost≈{config.COST_ROUNDTRIP_PCT*100:.2f}%)"
    )

    for i, t in enumerate(universe, start=1):
        try:
            k_map[t] = choose_best_k(t)
        except Exception:
            k_map[t] = config.K_DEFAULT

        if i % 5 == 0 or i == len(universe):
            print(f"  K 계산: {i}/{len(universe)}", end="\r")

        time.sleep(sleep_sec)

    print()
    preview = ", ".join([f"{t}:{k_map[t]:.2f}" for t in universe[:10] if t in k_map])
    print(f"✅ K 맵 준비 완료 (앞 10개): {preview}")
    return k_map
