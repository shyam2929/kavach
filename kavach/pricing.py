"""Black-76 pricing on the Nifty forward, greeks, IV solver, and a simple skew model."""
import math
from scipy.stats import norm
from scipy.optimize import brentq

R = 0.065      # INR risk-free (approx. T-bill yield)
Q = 0.012      # Nifty dividend yield
STRIKE_STEP = 50
LOT_SIZE = 65  # Nifty lot size from Jan 2026 series


def forward(spot: float, t: float, r: float = R, q: float = Q) -> float:
    return spot * math.exp((r - q) * t)


def black(F: float, K: float, t: float, iv: float, cp: str, r: float = R) -> float:
    """Black-76 price. cp = 'C' or 'P'."""
    df = math.exp(-r * t)
    if t <= 0 or iv <= 0:
        intrinsic = max(F - K, 0.0) if cp == "C" else max(K - F, 0.0)
        return df * intrinsic if t > 0 else intrinsic
    s = iv * math.sqrt(t)
    d1 = (math.log(F / K) + 0.5 * s * s) / s
    d2 = d1 - s
    if cp == "C":
        return df * (F * norm.cdf(d1) - K * norm.cdf(d2))
    return df * (K * norm.cdf(-d2) - F * norm.cdf(-d1))


def delta(F: float, K: float, t: float, iv: float, cp: str, r: float = R) -> float:
    if t <= 0 or iv <= 0:
        itm = (F > K) if cp == "C" else (F < K)
        return (1.0 if cp == "C" else -1.0) if itm else 0.0
    s = iv * math.sqrt(t)
    d1 = (math.log(F / K) + 0.5 * s * s) / s
    df = math.exp(-r * t)
    return df * norm.cdf(d1) if cp == "C" else -df * norm.cdf(-d1)


def gamma(F: float, K: float, t: float, iv: float, r: float = R) -> float:
    if t <= 0 or iv <= 0:
        return 0.0
    s = iv * math.sqrt(t)
    d1 = (math.log(F / K) + 0.5 * s * s) / s
    return math.exp(-r * t) * norm.pdf(d1) / (F * s)


def implied_vol(price: float, F: float, K: float, t: float, cp: str, r: float = R) -> float:
    intrinsic = math.exp(-r * t) * (max(F - K, 0) if cp == "C" else max(K - F, 0))
    if price <= intrinsic + 1e-6 or t <= 0:
        return float("nan")
    return brentq(lambda v: black(F, K, t, v, cp, r) - price, 1e-4, 5.0)


def skew_iv(atm_iv: float, F: float, K: float, t: float,
            slope: float = -0.010, curv: float = 0.004) -> float:
    """IV smile in standardized moneyness m = ln(K/F)/(atm_iv*sqrt t).
    Defaults approximate Nifty's put skew: 1-sd OTM put ~ +1.4 vol pts, 1-sd OTM call ~ -0.6.
    Replace with a smile fitted from the Upstox chains the paper runner logs (state/iv_samples.csv)."""
    if t <= 0:
        return atm_iv
    m = math.log(K / F) / (atm_iv * math.sqrt(t))
    m = max(-4.0, min(4.0, m))
    return max(0.03, atm_iv + slope * m + curv * m * m)


def round_strike(x: float, step: int = STRIKE_STEP) -> float:
    return round(x / step) * step
