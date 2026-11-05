"""Research harness: runs a pre-declared grid of strategy variants, records every one in the trial
ledger, and applies the PROMOTION GATE. A strategy may go to paper trading only if it passes all:

  G1  Deflated Sharpe Ratio >= 0.95 given ALL trials ever run in its family
  G2  Positive after doubling every cost (2x slippage + charges)
  G3  Positive with weekly IV at 0.90 x VIX (model-risk haircut)
  G4  Positive in BOTH halves of the sample (2018-2021 and 2022-now)
  G5  Bootstrap probability of a 30% drawdown within a year < 25%
  G6  At least 100 trades

Paper -> live requires a further 20 paper trades whose results sit inside the backtest's
5-95% band (kavach.governor checks this before allowing live mode).
"""
from dataclasses import replace
import json

import numpy as np
import pandas as pd

from .backtest import run, BTConfig
from .costs import CostModel
from .stats import deflated_sharpe, bootstrap_ruin, summarize
from .strategies import IronFly, LongStraddle
from . import trials

HOLD = dict(take_profit=0.8, stop=1.01)   # 1.01 x max loss = never stopped before expiry
ACTIVE = dict(take_profit=0.5, stop=0.5)


def grid():
    out = []
    for name, sz, wz in (("fly_1.0", 0.0, 1.0), ("condor_0.6_1.5", 0.6, 1.5), ("condor_0.8_1.8", 0.8, 1.8)):
        for ex_name, ex in (("hold", HOLD), ("active", ACTIVE)):
            out.append(("short_vol", IronFly(short_z=sz, wing_z=wz, name=f"{name}_{ex_name}", **ex)))
    out.append(("long_vol", LongStraddle()))
    return out


def evaluate(df, strat, family, rv_fcst, base: BTConfig = BTConfig(), record=True):
    res = run(df, strat, base, rv_fcst=rv_fcst)
    t = res["trades"]
    pnl = t["pnl"].values if len(t) else np.array([])
    summ = summarize(pnl, res["period_returns"], res["equity"])
    r = res["period_returns"]
    r_traded = t["ret"].values if len(t) else np.array([0.0])

    stress_cost = run(df, strat, replace(base, costs=CostModel(stress=2.0)), rv_fcst=rv_fcst)
    iv90 = run(df, strat, replace(base, iv_mult=0.90), rv_fcst=rv_fcst)
    h1 = run(df, strat, replace(base, end="2021-12-31"), rv_fcst=rv_fcst)
    h2 = run(df, strat, replace(base, start="2022-01-01"), rv_fcst=rv_fcst)
    tot = lambda x: float(x["trades"]["pnl"].sum()) if len(x["trades"]) else 0.0
    ruin = bootstrap_ruin(r, horizon=52, dd_limit=-0.30)

    params = {"strategy": strat.name, **{k: v for k, v in vars(strat).items() if k != "name"},
              "iv_mult": base.iv_mult, "risk": base.risk_per_trade}
    n_trials = trials.record(family, params, summ) if record else max(1, trials.count(family))
    dsr = deflated_sharpe(r_traded, max(n_trials, 1))
    gates = {
        "G1_dsr": dsr["dsr"] >= 0.95,
        "G2_cost2x": tot(stress_cost) > 0,
        "G3_iv90": tot(iv90) > 0,
        "G4_both_halves": tot(h1) > 0 and tot(h2) > 0,
        "G5_ruin": ruin["p_dd"] < 0.25,
        "G6_n": summ["trades"] >= 100,
    }
    return {"name": strat.name, "family": family, "summary": summ, "dsr": dsr, "n_trials": n_trials,
            "cost2x_pnl": tot(stress_cost), "iv90_pnl": tot(iv90), "h1_pnl": tot(h1), "h2_pnl": tot(h2),
            "ruin": ruin, "halts": res["halts"], "gates": gates, "promote": all(gates.values()),
            "_res": res}


def run_all(df, rv_fcst, base: BTConfig = BTConfig()):
    rows = [evaluate(df, s, fam, rv_fcst, base) for fam, s in grid()]
    # DSR must use the FINAL trial count of each family (later variants raise the bar for earlier ones)
    for row in rows:
        n = trials.count(row["family"])
        t = row["_res"]["trades"]
        row["dsr"] = deflated_sharpe(t["ret"].values if len(t) else np.array([0.0]), n)
        row["n_trials"] = n
        row["gates"]["G1_dsr"] = row["dsr"]["dsr"] >= 0.95
        row["promote"] = all(row["gates"].values())
    return rows


def table(rows) -> pd.DataFrame:
    recs = []
    for r in rows:
        s = r["summary"]
        recs.append({"strategy": r["name"], "trades": s["trades"], "win%": round(100 * s["win_rate"], 1),
                     "total_pnl": round(s["total_pnl"]), "CAGR%": round(100 * s["cagr"], 1),
                     "Sharpe": round(s["ann_sharpe"], 2), "maxDD%": round(100 * s["max_dd"], 1),
                     "worst": round(s["worst_trade"]), "DSR": round(r["dsr"]["dsr"], 3),
                     "trials": r["n_trials"], "pnl@2xcost": round(r["cost2x_pnl"]),
                     "pnl@0.9IV": round(r["iv90_pnl"]), "2018-21": round(r["h1_pnl"]),
                     "2022-26": round(r["h2_pnl"]), "P(DD>30%,1y)": round(r["ruin"]["p_dd"], 3),
                     "halts": r["halts"], "PROMOTE": r["promote"]})
    return pd.DataFrame(recs)
