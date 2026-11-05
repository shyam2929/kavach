"""Strategy definitions. A strategy only *describes* a position (legs) and its exit rules.
It never sizes itself and never talks to a broker."""
from dataclasses import dataclass, field
import math

from ..pricing import forward, round_strike, STRIKE_STEP


@dataclass
class Leg:
    cp: str        # 'C' or 'P'
    K: float
    qty: int       # +1 long / -1 short, per lot


@dataclass
class Plan:
    name: str
    legs: list
    take_profit: float          # fraction of max profit (short premium) or of debit (long)
    stop: float                 # fraction of max loss (credit) or of debit (long)
    exit_days_before: int = 0   # close N trading days before expiry (0 = hold to expiry close)
    meta: dict = field(default_factory=dict)


@dataclass
class IronFly:
    """Short ATM straddle + long wings at +-wing_z standard deviations (defined risk).
    Purest harvest of the variance premium: maximum short gamma/vega at the money."""
    wing_z: float = 1.0
    short_z: float = 0.0        # 0 = iron fly; >0 = iron condor
    take_profit: float = 0.5
    stop: float = 0.5           # of max loss
    min_edge: float = 0.0       # vol points (VIX - forecast RV) required; evidence says don't rely on it
    name: str = "iron_fly"

    def plan(self, spot: float, t: float, atm_iv: float, rv_fcst: float, stress: bool):
        if stress or (atm_iv * 100 - rv_fcst) < self.min_edge:
            return None
        F = forward(spot, t)
        sd = F * atm_iv * math.sqrt(t)
        ks_p = round_strike(F - self.short_z * sd)
        ks_c = round_strike(F + self.short_z * sd)
        kw_p = min(round_strike(F - self.wing_z * sd), ks_p - 2 * STRIKE_STEP)
        kw_c = max(round_strike(F + self.wing_z * sd), ks_c + 2 * STRIKE_STEP)
        legs = [Leg("P", kw_p, +1), Leg("C", kw_c, +1), Leg("P", ks_p, -1), Leg("C", ks_c, -1)]
        return Plan(self.name, legs, self.take_profit, self.stop)


@dataclass
class LongStraddle:
    """Buy ATM straddle only in STRESS regimes (the mirror image of the gate).
    Kept as a challenger; it must earn its place through the same promotion gate."""
    take_profit: float = 0.5
    stop: float = 0.4
    exit_days_before: int = 1
    name: str = "long_straddle_stress"

    def plan(self, spot: float, t: float, atm_iv: float, rv_fcst: float, stress: bool):
        if not stress:
            return None
        K = round_strike(forward(spot, t))
        return Plan(self.name, [Leg("C", K, +1), Leg("P", K, +1)],
                    self.take_profit, self.stop, self.exit_days_before)
