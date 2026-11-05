"""Broker adapters. All share one small interface so paper and live run the same code path."""
from __future__ import annotations

import abc
import datetime as dt
import math

import pandas as pd

from ..governor import OrderLeg


class Broker(abc.ABC):
    paper: bool = True

    @abc.abstractmethod
    def spot(self) -> float: ...

    @abc.abstractmethod
    def expiries(self) -> list[str]: ...

    @abc.abstractmethod
    def chain(self, expiry: str) -> pd.DataFrame:
        """Columns: strike, cp ('C'/'P'), bid, ask, ltp, iv (decimal), security_id."""

    @abc.abstractmethod
    def place(self, leg: OrderLeg, timeout_s: float = 20.0) -> dict:
        """Place one LIMIT order. Returns {'status': 'FILLED'|'REJECTED'|'OPEN'|'CANCELLED',
        'avg_price': float, 'order_id': str}."""

    def kill_switch(self) -> None:
        pass


def mid(row) -> float:
    b, a = row["bid"], row["ask"]
    if b > 0 and a > 0 and math.isfinite(b) and math.isfinite(a):
        return (b + a) / 2
    return float(row["ltp"])
