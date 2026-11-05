import datetime as dt
import math
import shutil
import tomllib

import numpy as np
import pandas as pd
import pytest

from kavach.costs import CostModel
from kavach.pricing import black, implied_vol, forward, LOT_SIZE
from kavach.expiry import weekly_expiries
from kavach.data import load_daily
from kavach.volmodel import WalkForwardHAR
from kavach.sizing import lots_for, capital_fraction, shrunk_edge
from kavach.stats import deflated_sharpe, expected_max_sharpe


# ---------- costs ----------
def test_stt_matches_budget_2026_example():
    # Upstox worked example: sell 1 lot Nifty option @150, lot 65 -> STT 0.15% = Rs 14.625
    c = CostModel(brokerage_per_order=0, exch_txn=0, sebi=0, ipft=0, stamp_buy=0, gst=0)
    assert math.isclose(c.charges(0, 150 * 65, 1), 14.625, rel_tol=1e-9)


def test_round_trip_charges_reasonable():
    c = CostModel()
    ch = c.charges(100 * 65, 120 * 65, 2)
    # brokerage 40 + GST + STT 11.7 + exch ~4.6 + stamp 0.2 ~ Rs 64
    assert 55 < ch < 75


# ---------- pricing ----------
def test_put_call_parity_and_iv_roundtrip():
    S, K, t, iv = 25000, 25200, 4 / 252, 0.14
    F = forward(S, t)
    c, p = black(F, K, t, iv, "C"), black(F, K, t, iv, "P")
    assert math.isclose(c - p, math.exp(-0.065 * t) * (F - K), rel_tol=1e-9)
    assert math.isclose(implied_vol(c, F, K, t, "C"), iv, rel_tol=1e-6)


# ---------- expiry calendar ----------
def test_expiry_calendar_handles_tuesday_switch_and_holiday():
    idx = load_daily().index
    ex = set(weekly_expiries(idx).date)
    assert dt.date(2026, 10, 27) in ex          # Tuesday, monthly
    assert dt.date(2026, 10, 19) in ex          # Dussehra Tue 20 Oct -> Mon 19 Oct
    assert dt.date(2026, 10, 13) in ex
    assert dt.date(2025, 8, 28) in ex           # last Thursday-era weekly
    assert dt.date(2025, 9, 2) in ex            # first Tuesday-era weekly


# ---------- no look-ahead ----------
def test_har_forecast_has_no_lookahead():
    df = load_daily().iloc[:1200]
    full = WalkForwardHAR().forecast_series(df)
    cut = WalkForwardHAR().forecast_series(df.iloc[:1000])
    shocked = df.copy()
    shocked.iloc[1000:, :] = shocked.iloc[1000:, :] * 1.0
    shocked.loc[shocked.index[1000:], ["high"]] *= 1.10    # wild future
    s2 = WalkForwardHAR().forecast_series(shocked)
    pd.testing.assert_series_equal(full.iloc[:1000], cut, check_names=False)
    pd.testing.assert_series_equal(s2.iloc[:1000], cut, check_names=False)


# ---------- sizing ----------
def test_sizing_small_account_floor_and_cap():
    assert lots_for(12000, 100000, 0.06, 0.35, 2, hard_cap=0.15) == 1     # floor: 1 lot fits under 15%
    assert lots_for(20000, 100000, 0.06, 0.35, 2, hard_cap=0.15) == 0     # one lot too big: no trade
    assert lots_for(3000, 1_000_000, 0.06, 1.0, 2) == 2                   # max_lots binds


def test_evidence_gate():
    assert capital_fraction([], 0.0) == 0.35
    assert capital_fraction([0.01] * 20, 0.0) == 0.6
    assert capital_fraction([-0.01] * 20, 0.0) == 0.35        # no edge -> floor
    assert capital_fraction([0.01] * 50, -0.12) == 0.5        # drawdown halves
    assert capital_fraction([0.01] * 50, -0.25) == 0.0        # halt
    assert shrunk_edge([0.02] * 20, 20) == pytest.approx(0.01)


# ---------- DSR ----------
def test_dsr_penalises_trials():
    rng = np.random.default_rng(0)
    r = rng.normal(0.004, 0.02, 300)
    assert deflated_sharpe(r, 1)["dsr"] > deflated_sharpe(r, 1000)["dsr"]
    assert expected_max_sharpe(1000, 1 / 299) > expected_max_sharpe(10, 1 / 299)
