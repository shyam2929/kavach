"""Realized-volatility forecasting: Garman-Klass + overnight variance, HAR model (Corsi 2009),
fitted walk-forward so the backtest never sees the future.

Why this is the core: a delta-neutral option position earns approximately
    0.5 * Gamma * S^2 * (sigma_realized^2 - sigma_implied^2) * dt
summed over its life. Every option trade is a bet on realized vs implied volatility, whether the
trader knows it or not. The system therefore trades only when its forecast of realized vol differs
from the market's implied vol by enough to pay for costs.
"""
import numpy as np
import pandas as pd


def daily_variance(df: pd.DataFrame) -> pd.Series:
    """Per-day variance estimate = overnight gap^2 + Garman-Klass intraday variance."""
    o, h, l, c = (np.log(df[k]) for k in ("open", "high", "low", "close"))
    overnight = (o - c.shift(1)) ** 2
    gk = 0.5 * (h - l) ** 2 - (2 * np.log(2) - 1) * (c - o) ** 2
    v = overnight.fillna(0) + gk.clip(lower=0)
    return v.rename("var")


def har_features(var: pd.Series) -> pd.DataFrame:
    return pd.DataFrame({
        "d": var,
        "w": var.rolling(5).mean(),
        "m": var.rolling(22).mean(),
    })


def fwd_target(var: pd.Series, horizon: int = 5) -> pd.Series:
    """Mean daily variance over the next `horizon` days (t+1 .. t+h)."""
    return var[::-1].rolling(horizon).mean()[::-1].shift(-1)


class WalkForwardHAR:
    """Log-HAR fitted on an expanding window, refit every `refit` days."""

    def __init__(self, horizon: int = 5, min_obs: int = 250, refit: int = 21):
        self.h, self.min_obs, self.refit = horizon, min_obs, refit

    def forecast_series(self, df: pd.DataFrame) -> pd.Series:
        """Annualized forecast vol (in vol points, like VIX) for each date, using only past data."""
        var = daily_variance(df)
        X = np.log(har_features(var).clip(lower=1e-10))
        y = np.log(fwd_target(var, self.h).clip(lower=1e-10))
        out = pd.Series(np.nan, index=df.index)
        beta, resid_var = None, 0.0
        for i in range(len(df)):
            if i < self.min_obs + 22:
                continue
            if beta is None or (i % self.refit == 0):
                # training rows: target must be fully observed before date i
                train_end = i - self.h
                Xt, yt = X.iloc[22:train_end], y.iloc[22:train_end]
                ok = Xt.notna().all(axis=1) & yt.notna()
                A = np.column_stack([np.ones(ok.sum()), Xt[ok].values])
                beta, *_ = np.linalg.lstsq(A, yt[ok].values, rcond=None)
                resid_var = np.var(yt[ok].values - A @ beta)
            x = X.iloc[i].values
            if np.isnan(x).any():
                continue
            logv = beta[0] + x @ beta[1:]
            mean_var = np.exp(logv + 0.5 * resid_var)   # lognormal bias correction
            out.iloc[i] = 100 * np.sqrt(mean_var * 252)
        return out.rename("rv_fcst")


def realized_fwd_vol(df: pd.DataFrame, horizon: int = 5) -> pd.Series:
    return (100 * np.sqrt(fwd_target(daily_variance(df), horizon) * 252)).rename("rv_real")
