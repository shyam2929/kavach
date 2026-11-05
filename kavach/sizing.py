"""Position sizing: risk-budgeted lots, scaled by an evidence-gated capital fraction.

Evidence gate ('earned capital'): a strategy starts with a fraction of the account and earns
more only as live/paper trades accumulate AND the shrunken (Bayesian) estimate of its edge stays
positive. This is the opposite of what the thread's agent did (it grew size from 20% to 70% of
the account after two good days).
"""
import math

import numpy as np


def shrunk_edge(trade_returns: list[float], prior_strength: float = 20.0) -> float:
    """Posterior mean per-trade return with a zero-mean prior worth `prior_strength` trades."""
    n = len(trade_returns)
    if n == 0:
        return 0.0
    return float(np.mean(trade_returns) * n / (n + prior_strength))


def capital_fraction(trade_returns: list[float], drawdown: float,
                     tiers=((0, 0.35), (15, 0.6), (40, 1.0)),
                     dd_half: float = -0.10, dd_stop: float = -0.20) -> float:
    """Fraction of risk budget available, by number of completed trades and current drawdown."""
    n = len(trade_returns)
    frac = 0.0
    for min_n, f in tiers:
        if n >= min_n:
            frac = f
    if n >= tiers[1][0] and shrunk_edge(trade_returns) <= 0:
        frac = tiers[0][1]              # no demonstrated edge: back to the floor
    if drawdown <= dd_stop:
        return 0.0                      # human reset required (governor enforces)
    if drawdown <= dd_half:
        frac *= 0.5
    return frac


def lots_for(max_loss_per_lot: float, equity: float, risk_per_trade: float,
             cap_frac: float, max_lots: int, hard_cap: float = 0.15) -> int:
    """Lots such that worst-case loss <= equity*risk_per_trade*cap_frac.
    Small-account exception: if that rounds to 0 but ONE lot's worst case is within
    `hard_cap` of equity, trade 1 lot (a lot is indivisible). With Rs 1L and a 65-unit lot
    this exception is usually what applies, which is itself the main risk warning."""
    if max_loss_per_lot <= 0 or cap_frac <= 0:
        return 0
    budget = equity * risk_per_trade * cap_frac
    n = int(max(0, min(max_lots, math.floor(budget / max_loss_per_lot))))
    if n == 0 and max_loss_per_lot <= equity * hard_cap:
        n = 1
    return n
