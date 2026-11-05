"""Performance statistics, including the Deflated Sharpe Ratio (Bailey & Lopez de Prado, 2014),
which discounts a backtest's Sharpe for the number of strategy variants you tried.
'It has tried millions of permutations' (the thread) is exactly the situation DSR exists for."""
import math

import numpy as np
from scipy.stats import norm, skew, kurtosis

EULER_GAMMA = 0.5772156649


def sharpe(r: np.ndarray) -> float:
    r = np.asarray(r, float)
    return float(r.mean() / r.std(ddof=1)) if len(r) > 1 and r.std(ddof=1) > 0 else 0.0


def max_drawdown(equity: np.ndarray) -> float:
    eq = np.asarray(equity, float)
    peak = np.maximum.accumulate(eq)
    return float(((eq - peak) / peak).min())


def expected_max_sharpe(n_trials: int, var_sr: float) -> float:
    """Expected maximum per-period Sharpe among n_trials skill-less strategies."""
    if n_trials <= 1:
        return 0.0
    a = norm.ppf(1 - 1.0 / n_trials)
    b = norm.ppf(1 - 1.0 / (n_trials * math.e))
    return math.sqrt(var_sr) * ((1 - EULER_GAMMA) * a + EULER_GAMMA * b)


def deflated_sharpe(r: np.ndarray, n_trials: int, var_sr_trials: float | None = None) -> dict:
    """Probability that the true per-period Sharpe exceeds what luck alone would produce
    given n_trials attempts. DSR > 0.95 is the bar for promotion."""
    r = np.asarray(r, float)
    T = len(r)
    sr = sharpe(r)
    g3 = float(skew(r)) if T > 2 else 0.0
    g4 = float(kurtosis(r, fisher=False)) if T > 3 else 3.0
    if var_sr_trials is None:
        var_sr_trials = 1.0 / max(T - 1, 1)          # variance of SR estimator under H0
    sr0 = expected_max_sharpe(n_trials, var_sr_trials)
    denom = math.sqrt(max(1e-12, 1 - g3 * sr + (g4 - 1) / 4 * sr * sr))
    z = (sr - sr0) * math.sqrt(max(T - 1, 1)) / denom
    return {"sr": sr, "sr0": sr0, "dsr": float(norm.cdf(z)), "skew": g3, "kurt": g4, "T": T}


def bootstrap_ruin(r: np.ndarray, horizon: int = 52, n: int = 20000, dd_limit: float = -0.30,
                   block: int = 4, seed: int = 7) -> dict:
    """Block-bootstrap per-period returns; probability of hitting dd_limit within horizon."""
    rng = np.random.default_rng(seed)
    r = np.asarray(r, float)
    if len(r) < block * 2:
        return {"p_dd": float("nan"), "median_ret": float("nan"), "p_loss": float("nan")}
    hits, finals = 0, np.empty(n)
    nb = math.ceil(horizon / block)
    for i in range(n):
        starts = rng.integers(0, len(r) - block, nb)
        path = np.concatenate([r[s:s + block] for s in starts])[:horizon]
        eq = np.cumprod(1 + path)
        if max_drawdown(np.concatenate([[1.0], eq])) <= dd_limit:
            hits += 1
        finals[i] = eq[-1] - 1
    return {"p_dd": hits / n, "median_ret": float(np.median(finals)),
            "p_loss": float((finals < 0).mean()), "p5": float(np.quantile(finals, 0.05)),
            "p95": float(np.quantile(finals, 0.95))}


def summarize(trades_pnl: np.ndarray, period_returns: np.ndarray, equity: np.ndarray,
              periods_per_year: int = 52) -> dict:
    p = np.asarray(trades_pnl, float)
    r = np.asarray(period_returns, float)
    yrs = len(r) / periods_per_year if len(r) else float("nan")
    cagr = (equity[-1] / equity[0]) ** (1 / yrs) - 1 if len(r) and equity[-1] > 0 else -1.0
    return {
        "trades": int(len(p)),
        "win_rate": float((p > 0).mean()) if len(p) else float("nan"),
        "avg_win": float(p[p > 0].mean()) if (p > 0).any() else 0.0,
        "avg_loss": float(p[p < 0].mean()) if (p < 0).any() else 0.0,
        "worst_trade": float(p.min()) if len(p) else 0.0,
        "total_pnl": float(p.sum()),
        "cagr": float(cagr),
        "ann_sharpe": sharpe(r) * math.sqrt(periods_per_year),
        "max_dd": max_drawdown(equity),
    }
