"""Nifty weekly expiry calendar.

- Weekly options launched Feb 2019 (earlier history is used only as an approximation).
- Expiry weekday: Thursday until 28 Aug 2025; Tuesday from 2 Sep 2025 (NSE circular).
- If the expiry weekday is a holiday, expiry moves to the previous trading day.
Holidays are inferred from the trading-day index of the data you pass in.
"""
import pandas as pd

TUESDAY_SWITCH = pd.Timestamp("2025-09-01")


def expiry_weekday(d: pd.Timestamp) -> int:
    return 1 if d >= TUESDAY_SWITCH else 3   # Mon=0 ... Thu=3


def weekly_expiries(trading_days: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Return the actual expiry trading day for each week covered by `trading_days`."""
    days = pd.DatetimeIndex(sorted(trading_days))
    out = []
    # anchor each week on its nominal expiry date
    start = days[0] - pd.Timedelta(days=7)
    nominal = pd.date_range(start, days[-1] + pd.Timedelta(days=7), freq="D")
    nominal = [d for d in nominal if d.weekday() == expiry_weekday(d)]
    for nd in nominal:
        prior = days[days <= nd]
        if len(prior) == 0:
            continue
        exp = prior[-1]
        if (nd - exp).days <= 4 and (not out or exp > out[-1]):
            out.append(exp)
    return pd.DatetimeIndex(out)
