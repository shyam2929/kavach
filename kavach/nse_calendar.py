"""NSE F&O trading calendar from Upstox's public market-holidays API (no token needed).
Special sessions (e.g. Muhurat trading) are treated as CLOSED: the system does not trade them."""
from __future__ import annotations

import datetime as dt
import json
import urllib.request

import pandas as pd

URL = "https://api.upstox.com/v2/market/holidays"
_cache: dict[str, set[str]] = {}


def nfo_closed_dates() -> set[str]:
    if "d" in _cache:
        return _cache["d"]
    try:
        req = urllib.request.Request(URL, headers={"Accept": "application/json", "User-Agent": "kavach"})
        with urllib.request.urlopen(req, timeout=15) as r:
            rows = json.load(r).get("data", [])
        out = {r["date"] for r in rows
               if "NFO" in (r.get("closed_exchanges") or []) or r.get("holiday_type") == "SPECIAL_TIMING"}
    except Exception:
        out = set()          # network failure: fall back to weekdays only
    _cache["d"] = out
    return out


def is_trading_day(d: dt.date, closed: set[str] | None = None) -> bool:
    closed = nfo_closed_dates() if closed is None else closed
    return d.weekday() < 5 and d.isoformat() not in closed


def trading_days_between(a: dt.date, b: dt.date, closed: set[str] | None = None) -> int:
    """Trading days strictly after `a` up to and including `b`."""
    closed = nfo_closed_dates() if closed is None else closed
    days = [d for d in pd.bdate_range(a, b) if d.date().isoformat() not in closed]
    return max(sum(1 for d in days if d.date() > a), 0)
