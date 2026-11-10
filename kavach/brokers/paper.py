"""Paper broker. Quotes come from (a) Upstox's real option chain (UpstoxMarketData), or
(b) a synthetic chain built from live Nifty spot + India VIX with the backtest's pricing and
spread model. Fills are conservative: you buy at the ask and sell at the bid, never the mid."""
from __future__ import annotations

import datetime as dt
import itertools

import pandas as pd

from . import Broker
from ..governor import OrderLeg
from ..pricing import black, forward, skew_iv, STRIKE_STEP
from ..costs import DEFAULT_COSTS
from ..expiry import weekly_expiries


class SyntheticQuotes:
    """Model chain from spot + VIX. Good enough to exercise the pipeline, not to measure edge."""

    def __init__(self, spot_fn, vix_fn, iv_mult: float = 1.0, today_fn=dt.date.today, closed: set | None = None):
        self.spot_fn, self.vix_fn, self.iv_mult, self.today = spot_fn, vix_fn, iv_mult, today_fn
        if closed is None:
            from ..nse_calendar import nfo_closed_dates
            closed = nfo_closed_dates()
        self.closed = closed

    def spot(self) -> float:
        return float(self.spot_fn())

    def expiries(self) -> list[str]:
        days = pd.DatetimeIndex([d for d in pd.bdate_range(self.today(), periods=30)
                                 if d.date().isoformat() not in self.closed])
        return [str(d.date()) for d in weekly_expiries(days) if d.date() >= self.today()][:3]

    def chain(self, expiry: str) -> pd.DataFrame:
        S, iv = self.spot_fn(), self.vix_fn() / 100 * self.iv_mult
        from ..nse_calendar import trading_days_between
        n_td = trading_days_between(self.today(), dt.date.fromisoformat(expiry), self.closed)
        t = (n_td + 0.5) / 252
        F = forward(S, t)
        atm = round(F / STRIKE_STEP) * STRIKE_STEP
        rows = []
        for k in range(-40, 41):
            K = atm + k * STRIKE_STEP
            for cp in ("C", "P"):
                v = skew_iv(iv, F, K, t)
                p = black(F, K, t, v, cp)
                half = DEFAULT_COSTS.slippage(p)
                rows.append({"strike": K, "cp": cp, "bid": max(0.05, p - half), "ask": p + half,
                             "ltp": p, "iv": v, "security_id": f"SYN-{expiry}-{K}-{cp}"})
        return pd.DataFrame(rows)


class PaperBroker(Broker):
    paper = True

    SNAPSHOT_S = 20.0   # one quote snapshot per expiry for this long: the limit is priced and filled
                        # against the same book (live feeds tick between two fetches; that made
                        # marketable sells look non-marketable and unwound good trades)

    def __init__(self, quotes, clock=None):
        import time as _t
        self.q = quotes
        self._ids = itertools.count(1)
        self.fills: list[dict] = []
        self._clock = clock or _t.monotonic
        self._snap: dict[str, tuple[float, pd.DataFrame]] = {}

    def spot(self) -> float:
        return float(self.q.spot())

    def expiries(self) -> list[str]:
        return self.q.expiries()

    def chain(self, expiry: str) -> pd.DataFrame:
        now = self._clock()
        hit = self._snap.get(expiry)
        if hit and now - hit[0] < self.SNAPSHOT_S:
            return hit[1]
        ch = self.q.chain(expiry)
        self._snap[expiry] = (now, ch)
        return ch

    def refresh(self):
        self._snap.clear()

    def place(self, leg: OrderLeg, timeout_s: float = 20.0) -> dict:
        ch = self.chain(leg.expiry)
        r = ch[(ch.strike == leg.strike) & (ch.cp == leg.cp)]
        oid = f"P{next(self._ids)}"
        if r.empty:
            return {"status": "REJECTED", "avg_price": 0.0, "order_id": oid, "msg": "no such strike"}
        r = r.iloc[0]
        if leg.side == "BUY" and leg.limit_price >= r.ask:
            px = float(r.ask)
        elif leg.side == "SELL" and leg.limit_price <= r.bid:
            px = float(r.bid)
        else:
            return {"status": "CANCELLED", "avg_price": 0.0, "order_id": oid, "msg": "limit not marketable"}
        fill = {"status": "FILLED", "avg_price": px, "order_id": oid, "ts": dt.datetime.now().isoformat()}
        self.fills.append({**fill, "leg": vars(leg)})
        return fill
