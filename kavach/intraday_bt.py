"""Baseline: the thread's approach, reconstructed — intraday opening-range breakout, buy the ATM
weekly option, ~20% of capital per trade, strict SL/target, flat by 15:15.
Option premiums are modelled (Black-76, IV = that day's VIX x iv_mult), so this measures the
*shape* of the strategy's economics under real costs, not exact historical fills."""
import datetime as dt

import numpy as np
import pandas as pd

from .costs import CostModel, DEFAULT_COSTS
from .expiry import weekly_expiries
from .pricing import black, forward, round_strike, LOT_SIZE


def run_orb(bars: pd.DataFrame, vix_daily: pd.Series, capital: float = 100_000, alloc: float = 0.20,
            or_minutes: int = 15, sl: float = 0.25, tp: float = 0.50, iv_mult: float = 1.0,
            costs: CostModel = DEFAULT_COSTS) -> pd.DataFrame:
    bars = bars.copy()
    bars["date"] = bars.index.date
    days = pd.DatetimeIndex(sorted(set(pd.to_datetime(bars["date"]))))
    exps = weekly_expiries(days.union(pd.bdate_range(days[-1], periods=8)))
    out, equity = [], capital
    for d, g in bars.groupby("date"):
        g = g.between_time("09:15", "15:30")
        if len(g) < 30:
            continue
        vix = vix_daily.asof(pd.Timestamp(d))
        exp = exps[exps >= pd.Timestamp(d)][0]
        td = len(pd.bdate_range(d, exp)) - 1
        or_end = (dt.datetime.combine(d, dt.time(9, 15)) + dt.timedelta(minutes=or_minutes)).time()
        rng = g.between_time("09:15", or_end, inclusive="left")
        hi, lo = rng.high.max(), rng.low.min()
        after = g[g.index.time >= or_end]
        sig = None
        for ts, b in after.iterrows():
            if ts.time() >= dt.time(14, 30):
                break
            if b.close > hi:
                sig = ("C", ts, b.close); break
            if b.close < lo:
                sig = ("P", ts, b.close); break
        if not sig:
            continue
        cp, t_in, S_in = sig

        def tleft(ts):
            mins = (dt.datetime.combine(d, dt.time(15, 30)) - ts.replace(tzinfo=None)).total_seconds() / 60
            return (td + mins / 375) / 252

        iv = vix / 100 * iv_mult
        K = round_strike(forward(S_in, tleft(t_in)))
        p_in = black(forward(S_in, tleft(t_in)), K, tleft(t_in), iv, cp)
        fill_in = p_in + costs.slippage(p_in)
        lots = max(1, int(alloc * equity // (fill_in * LOT_SIZE)))
        q = lots * LOT_SIZE
        exit_px, reason, t_out = None, "eod", None
        for ts, b in g[g.index > t_in].iterrows():
            adverse = b.low if cp == "C" else b.high
            fav = b.high if cp == "C" else b.low
            pa = black(forward(adverse, tleft(ts)), K, tleft(ts), iv, cp)
            pf = black(forward(fav, tleft(ts)), K, tleft(ts), iv, cp)
            if pa <= fill_in * (1 - sl):
                exit_px, reason, t_out = pa, "sl", ts; break
            if pf >= fill_in * (1 + tp):
                exit_px, reason, t_out = pf, "tp", ts; break
            if ts.time() >= dt.time(15, 15):
                exit_px, t_out = black(forward(b.close, tleft(ts)), K, tleft(ts), iv, cp), ts; break
        if exit_px is None:
            continue
        fill_out = max(0.05, exit_px - costs.slippage(exit_px))
        charges = costs.charges(fill_in * q, fill_out * q, 2)
        pnl = (fill_out - fill_in) * q - charges
        out.append({"date": d, "cp": cp, "K": K, "lots": lots, "in": round(fill_in, 2),
                    "out": round(fill_out, 2), "reason": reason, "charges": round(charges, 1),
                    "pnl": round(pnl, 1), "equity_before": round(equity, 0)})
        equity += pnl
    return pd.DataFrame(out)
