"""Regime gate. Measured on Nifty 2016-2026 (see README, 'Evidence'):
base rate of a 'bad' week for short vol (VIX - realized 5d vol < -5 pts) = 5.4%.
It jumps to 15-25% when any of these hold, and the average volatility premium turns negative:
  - VIX up >12% over 5 days         (vol is being repriced upward)
  - Nifty down >2.4% over 5 days    (selling pressure; vol clusters after drops)
  - Nifty >5% below its 20-day high (drawdown regime)
Short-volatility entries are blocked in STRESS. Thresholds were set from the 90th-percentile
cut-points, once, and are not tuned in the backtest.
"""
import numpy as np
import pandas as pd


def regime_frame(df: pd.DataFrame, vix_jump: float = 0.12, ret5_floor: float = -0.024,
                 dd20_floor: float = -0.05, vix_cap: float = 28.0) -> pd.DataFrame:
    out = pd.DataFrame(index=df.index)
    out["vix_ch5"] = np.log(df["vix"]).diff(5)
    out["ret5"] = np.log(df["close"]).diff(5)
    out["dd20"] = df["close"] / df["close"].rolling(20).max() - 1
    out["stress"] = ((out["vix_ch5"] > vix_jump) | (out["ret5"] < ret5_floor)
                     | (out["dd20"] < dd20_floor) | (df["vix"] > vix_cap))
    return out
