"""Builds report.html: evidence, research grid with promotion gates, baseline, and the arithmetic."""
import base64
import datetime as dt
import io
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from .data import load_daily, load_intraday_yahoo
from .intraday_bt import run_orb
from .regime import regime_frame
from .research import run_all, table
from .volmodel import WalkForwardHAR, realized_fwd_vol

ROOT = Path(__file__).resolve().parent.parent
INK, MUTED, GRID = "#1f2328", "#6e7781", "#d0d7de"
C1, C2, C3, C4 = "#2f6f9f", "#c2603a", "#5b8a3a", "#8a5bb0"


def _style(ax):
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.tick_params(colors=MUTED, labelsize=9)
    ax.grid(axis="y", color=GRID, lw=0.6, alpha=0.7)
    ax.set_axisbelow(True)


def _png(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def build_report(out: Path = ROOT / "report.html") -> Path:
    df = load_daily()
    fc = WalkForwardHAR().forecast_series(df)
    rv = realized_fwd_vol(df)
    ev = pd.concat([df.vix, fc, rv], axis=1).dropna()
    ev["vrp"] = ev.vix - ev.rv_real
    reg = regime_frame(df).reindex(ev.index)

    # 1. premium by year
    yr = ev.groupby(ev.index.year).vrp.agg(["mean", "median"])
    fig, ax = plt.subplots(figsize=(7.5, 3))
    ax.bar(yr.index - 0.18, yr["median"], 0.36, color=C1, label="Median")
    ax.bar(yr.index + 0.18, yr["mean"], 0.36, color=C2, label="Mean")
    ax.axhline(0, color=INK, lw=0.8)
    ax.set_ylabel("Vol points", color=MUTED, fontsize=9)
    ax.set_title("India VIX minus next-week realized Nifty vol, by year", loc="left", fontsize=11, color=INK)
    ax.legend(frameon=False, fontsize=9, loc="lower left")
    _style(ax)
    img_vrp = _png(fig)

    # 2. regime table
    ev["bad"] = ev.vrp < -5
    calm, stress = ev[~reg.stress], ev[reg.stress]
    regime_rows = [("All weeks", len(ev), ev.bad.mean(), ev.vrp.mean(), ev.vrp.median()),
                   ("Calm (gate open)", len(calm), calm.bad.mean(), calm.vrp.mean(), calm.vrp.median()),
                   ("Stress (gate shut)", len(stress), stress.bad.mean(), stress.vrp.mean(), stress.vrp.median())]

    # 3. research grid
    rows = run_all(df, fc)
    tab = table(rows)
    fig, ax = plt.subplots(figsize=(7.5, 3.4))
    for r, col in zip([x for x in rows if x["name"] in ("fly_1.0_hold", "condor_0.8_1.8_hold", "condor_0.6_1.5_hold")], (C1, C2, C3)):
        t = r["_res"]["trades"]
        eq = pd.Series(r["_res"]["equity"][1:], index=range(len(r["_res"]["equity"]) - 1))
        if len(t):
            s = pd.Series(100_000 + t.pnl.cumsum().values, index=t["exit"])
            ax.plot(s.index, s.values / 1e5, color=col, lw=1.6, label=r["name"])
    ax.axhline(1, color=INK, lw=0.8)
    ax.set_ylabel("Equity (Rs lakh)", color=MUTED, fontsize=9)
    ax.set_title("Model equity, Rs 1L start, evidence-gated sizing", loc="left", fontsize=11, color=INK)
    ax.legend(frameon=False, fontsize=9)
    _style(ax)
    img_eq = _png(fig)

    best = next(r for r in rows if r["name"] == "fly_1.0_hold")
    bt = best["_res"]["trades"]
    by_year = bt.groupby(pd.to_datetime(bt["exit"]).dt.year).pnl.agg(["count", "sum"]).round(0)

    # 4. thread baseline
    try:
        orb = run_orb(load_intraday_yahoo(), df.vix)
        orb_txt = (f"{len(orb)} trades, {orb.pnl.gt(0).mean():.0%} winners, total Rs {orb.pnl.sum():,.0f}, "
                   f"charges Rs {orb.charges.sum():,.0f}, best day Rs {orb.pnl.max():,.0f}, worst Rs {orb.pnl.min():,.0f}, "
                   f"window {orb['date'].min()} to {orb['date'].max()}")
    except Exception as e:
        orb_txt = f"unavailable ({e})"

    def fmt_table(d: pd.DataFrame) -> str:
        return d.to_html(index=False, border=0, classes="t", na_rep="")

    reg_html = "".join(f"<tr><td>{a}</td><td>{b}</td><td>{c:.1%}</td><td>{d:.2f}</td><td>{e:.2f}</td></tr>"
                       for a, b, c, d, e in regime_rows)
    gates = ("G1 Deflated Sharpe ≥ 0.95 for all trials in the family · G2 profitable at 2× costs · "
             "G3 profitable with weekly IV = 0.90×VIX · G4 profitable in 2018–21 and 2022–26 · "
             "G5 P(30% drawdown in a year) &lt; 25% · G6 ≥ 100 trades")
    html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>KAVACH research report</title>
<style>
:root{{--ink:#1f2328;--muted:#6e7781;--line:#d0d7de;--bg:#fff;--soft:#f6f8fa;--warn:#9a3412}}
@media (prefers-color-scheme:dark){{:root{{--ink:#e6edf3;--muted:#8d96a0;--line:#30363d;--bg:#0d1117;--soft:#161b22;--warn:#f0883e}}
img{{background:#fff;border-radius:6px}}}}
body{{font:15px/1.6 -apple-system,Segoe UI,Roboto,sans-serif;color:var(--ink);background:var(--bg);max-width:980px;margin:0 auto;padding:28px 20px 60px}}
h1{{font-size:26px;margin:0 0 4px}} h2{{font-size:19px;margin:34px 0 8px;border-bottom:1px solid var(--line);padding-bottom:4px}}
.sub{{color:var(--muted);margin:0 0 18px}} .call{{background:var(--soft);border-left:3px solid var(--warn);padding:10px 14px;margin:14px 0}}
table.t{{border-collapse:collapse;font-size:12.5px;width:100%;overflow-x:auto;display:block}} .t th,.t td{{padding:5px 7px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}}
.t th:first-child,.t td:first-child{{text-align:left}} .t th{{color:var(--muted);font-weight:600}}
img{{max-width:100%}} code{{background:var(--soft);padding:1px 4px;border-radius:4px}}
</style></head><body>
<h1>KAVACH: research report</h1>
<p class="sub">Nifty weekly options · data {df.index[0]:%d %b %Y} – {df.index[-1]:%d %b %Y} · generated {dt.datetime.now():%d %b %Y %H:%M}</p>

<div class="call"><b>Verdict.</b> No strategy passed the promotion gate. The volatility premium is real
but thin and shrinking, the most profitable variants made their money before 2022, and the
thread's breakout approach lost money over the same 60 days even before costs. The best candidate
(<code>fly_1.0_hold</code>) is down in 2026 so far, its worst year in the sample. The system's
correct output today is: paper-trade the best candidate, calibrate on real option data, and risk nothing yet.</div>

<h2>1. The arithmetic of "Rs 10k a day on Rs 1L"</h2>
<p>Rs 10,000/day on Rs 1,00,000 is 10% per day. Without compounding that is ~2,500% a year; compounded over
250 trading days it is 1.1<sup>250</sup> ≈ 2×10<sup>10</sup>. For scale, SEBI found ~91% of individual F&amp;O traders
lost money in FY25, and 97% of traders in its behavioural sample were mainly option buyers. A target like that does not
describe an edge. It sets the leverage, and the leverage sets the risk of ruin.</p>

<h2>2. Where an edge actually exists: the volatility premium</h2>
<p>An option position's P&amp;L is, to first order, ½·Γ·S²·(realized variance − implied variance) over its life.
On {len(ev):,} days, India VIX exceeded next-week realized Nifty volatility {(ev.vrp > 0).mean():.0%} of the time, by a median
of {ev.vrp.median():.1f} vol points. The premium compensates sellers for rare, violent losses (worst week {ev.vrp.min():.0f} pts)
and has narrowed in 2026.</p>
<img src="{img_vrp}" alt="Volatility premium by year">
<p>Forecasting realized vol (walk-forward HAR model) did <b>not</b> predict a larger premium. But a simple
stress gate, fixed in advance at the 90th-percentile cut-points, separated the bad weeks:</p>
<table class="t"><tr><th>Regime</th><th>Days</th><th>P(bad week: premium &lt; −5)</th><th>Mean premium</th><th>Median premium</th></tr>{reg_html}</table>

<h2>3. Research grid and promotion gate</h2>
<p>{gates}. Every variant run is written to a trial ledger. Its count raises the bar in the Deflated Sharpe test,
so trying more variants cannot manufacture significance.</p>
{fmt_table(tab)}
<img src="{img_eq}" alt="Equity curves">
<p>Best all-round candidate, <code>fly_1.0_hold</code> (short ATM straddle, wings ±1σ, held to expiry), P&amp;L by year:</p>
{fmt_table(by_year.reset_index().rename(columns={"exit": "year", "count": "trades", "sum": "pnl (Rs)"}))}

<h2>4. The thread's approach, reconstructed</h2>
<p>Opening-range breakout, buy ATM weekly option, ~20% of capital, SL 25% / target 50%, flat by 15:15, on 5-minute
Nifty bars with modelled premiums and full costs: {orb_txt}. That window contains the days the thread reported.</p>

<h2>5. Limits of this report</h2>
<p>Premiums are model prices from VIX with an assumed smile, not historical option quotes. Weekly ATM IV can sit
above or below VIX; the IV multiplier is the largest single source of error. The paper runner measures it live from Upstox chains (dashboard: IV calibration);
once enough samples exist, set that multiplier and rerun research before trusting any rupee figure.
Holidays are inferred from the data. Overnight gaps are captured only at daily resolution.</p>
</body></html>"""
    out.write_text(html)
    return out
