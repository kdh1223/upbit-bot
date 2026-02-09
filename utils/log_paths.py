"""Helpers for monthly log/report file paths in KST."""

import datetime as dt
import glob
import os
from zoneinfo import ZoneInfo


KST = ZoneInfo("Asia/Seoul")


def now_kst() -> dt.datetime:
    return dt.datetime.now(KST)


def as_kst(value=None) -> dt.datetime:
    if value is None:
        return now_kst()
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=KST)
        return value.astimezone(KST)
    raise TypeError("value must be datetime or None")


def trade_log_path_for(value=None, base_dir: str = ".") -> str:
    ts = as_kst(value)
    ym = ts.strftime("%Y-%m")
    return os.path.join(base_dir, f"trade_log_{ym}.csv")


def report_log_path_for(value=None, base_dir: str = ".") -> str:
    ts = as_kst(value)
    ym = ts.strftime("%Y-%m")
    return os.path.join(base_dir, f"report_{ym}.log")


def list_trade_log_paths(base_dir: str = "."):
    pattern = os.path.join(base_dir, "trade_log_????-??.csv")
    return sorted(glob.glob(pattern))
