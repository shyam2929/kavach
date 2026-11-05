# KAVACH: a risk-first volatility-edge system for Nifty weekly options

Built in response to the "let Fable 5.1 make ₹10k/day on ₹1L" thread (Oct 2026). It answers that experiment
from first principles instead of copying it.

## The first-principles argument

1. **Before costs, F&O is zero-sum; after costs it's negative-sum.** SEBI found ~91% of individual F&O traders lost money
   in FY25, and 97% of its behavioural sample were mainly option *buyers*. Most money that changes hands flows
   from directional option buyers to the people on the other side.
2. **Every option position is a volatility bet.** Its P&L ≈ ½·Γ·S²·(σ²realized − σ²implied) over its life. If you
   don't have a view on realized vs implied vol, you are paying the spread to someone who does.
3. **The measurable edge is the variance risk premium.** On Nifty 2016–2026, India VIX exceeded next-week realized volatility
   ~80% of the time (median ~3.5 vol pts). The premium compensates for rare catastrophic weeks (Mar 2020: about −70 pts),
   and it has narrowed in 2026.
4. **Forecasting realized vol didn't help pick weeks; avoiding stress did.** A walk-forward HAR forecast had ~zero
   correlation with the realized premium. A simple *stress gate* (VIX up >12% in 5d, Nifty down >2.4% in 5d, or >5% below
   its 20-day high) separated the bad weeks: 11.6% bad-week rate inside stress vs 3.6% outside (15–25% on the
   most extreme individual triggers).
5. **₹10k/day on ₹1L is 10%/day.** That number is a leverage setting, not an edge. With a 65-unit lot, a single
   defined-risk Nifty structure already risks ~10–13% of ₹1L. **₹1L is too small to run any option strategy at sane risk.**
6. **AI agents fail in a specific way:** they search millions of variants and present the winner, then grow size after
   a good streak, and can rewrite their own constraints. The thread's agent did the first two. KAVACH is built so that
   none of the three is possible.

## What's invented here

| Mechanism | What it stops |
|---|---|
| **Constitutional governor** (`kavach/governor.py`). Every order passes it. Limits run from a hash-locked copy. Tightening applies immediately; loosening needs a typed human confirmation and waits until the next day. Tampering with the lock file stops the engine. | An agent raising its own risk limit at 4am and trading on it |
| **Trial ledger + Deflated Sharpe** (`trials.py`, `stats.py`). Every backtest variant is recorded, and the promotion test discounts for *all* of them. | "Tried millions of permutations" overfitting |
| **Evidence-gated capital** (`sizing.py`). Starts at 35% of the risk budget, reaches 60% after 15 trades and 100% after 40, and only while the shrunken edge estimate stays positive. Halves at −10% drawdown. Halts at −20% until a human resets it. | Scaling from 20% to 70% of the account after two good days |
| **Anti-revenge rule**. Three straight losses means sitting out a full expiry cycle. | Tilt |
| **Promotion gate** (`research.py`). DSR ≥ 0.95, profitable at 2× costs, profitable at 0.9× IV, profitable in both halves of the sample, P(30% drawdown) < 25%, ≥ 100 trades. Then 20 paper trades before live. | Shipping a lucky backtest |
| **Wings-first execution**. Long legs are filled before short legs, and a failed short leg triggers an unwind. Defined-risk-only, checked on every basket. | Ever being naked, even for a second |

## Results (model-priced; see report.html)

- **No strategy passed the promotion gate.** That's the system working.
- `fly_1.0_hold` (short ATM straddle, wings at ±1σ, held to expiry) passed every gate except the
  Deflated Sharpe test (0.74 < 0.95 after 6 variants). It made money in both halves of the sample, at 2× costs and at 0.9× IV.
- That same best candidate has **lost ₹31,762 in 2026 so far** in the model, its worst year in the sample. The shrinking
  premium is showing up in its P&L.
- The iron condors held to expiry scored higher on the Deflated Sharpe test, but **lost money in 2022–2026**. All their profit came from 2018–21.
- Stop-loss/take-profit variants were worse than holding to expiry: intraday noise trips the stops.
- **The thread's approach** (opening-range breakout, buying ATM options at 20% of capital) over the last 60 trading days (11 Aug – 5 Nov 2026):
  59 trades, 32% winners, **−₹30,475**. It lost money even at zero cost. No ₹10k day.

## How it runs

```
GitHub Actions (every ~15 min, 09:15–15:30 IST, + on every command)
  └─ kavach.cli step  ──►  paper engine  ──►  governor  ──►  PaperBroker
        │                     │                               └─ quotes: Upstox option chain (Analytics Token)
        │                     └─ state (book, journal, account, IV samples) ─► gh-pages/state
        └─ dashboard JSON ─► gh-pages/data ─► https://<you>.github.io/kavach/
Dashboard buttons ─► GitHub issue "kavach: <cmd>" (owner only) ─► triggers the runner ─► result comment + issue closed
```

- **Paper trading runs entirely in the cloud.** It needs nothing on your computer.
- **Live trading cannot run in the cloud.** Since 1 Apr 2026, Upstox only accepts orders from the static IP registered on
  your API app, with a token from a fresh login each day. `kavach.cli` refuses `--live` on GitHub Actions. Live runs on your own static-IP
  machine with the same code and governor, and can publish to the same dashboard.

## One-time setup

1. **Upstox Analytics Token** (free, read-only, valid 1 year, no static IP needed for market data). Generate it at
   Upstox Developer Apps → *Analytics* tab → *Generate Token*. Then add it to the repo at GitHub → Settings → Secrets and variables →
   Actions → *New repository secret*, named `UPSTOX_ANALYTICS_TOKEN`. Until you do, the runner uses a synthetic chain built
   from live Nifty + VIX, and the dashboard says so.
2. **Optional one-click control.** Create a fine-grained GitHub token limited to this repository, with *Issues: Read and write*,
   and paste it into the dashboard's Settings. Without it, each button opens a pre-filled GitHub issue for you to submit.
3. GitHub disables scheduled workflows after 60 days with no repository activity. The weekly research job commits to `main`,
   which keeps the schedule alive.

## Commands (dashboard buttons → issue titles)

| Issue title | Effect |
|---|---|
| `kavach: approve <id>` | Re-price the proposal, re-check the governor, enter if spot moved < 0.6% |
| `kavach: reject <id>` | Drop the proposal; no entry this cycle |
| `kavach: pause` / `resume` | Stop / allow new entries (open positions are still managed) |
| `kavach: flatten` | Close the open position |
| `kavach: step` | Run a step now |
| `kavach: reset-halt RESET HALT YYYY-MM-DD` | Human-only: clear a drawdown halt |
| `kavach: limits-accept [I ACCEPT LOOSER LIMITS YYYY-MM-DD]` | Human-only: adopt an edited `limits.toml` |

Issues from anyone other than the repo owner are ignored. Every command leaves a closed issue with the result: that's your audit trail.

## Local use

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q                        # 36 tests
python -m kavach.cli research              # grid + gates + ledger -> report.html
python -m kavach.cli orb                   # thread-style baseline on last 60d
export UPSTOX_ANALYTICS_TOKEN=...          # real quotes for paper
python -m kavach.cli loop --site site      # paper loop every 60s; open site/index.html via a local server
```

## Before going live (in order; do not skip)

1. **Calibrate.** The runner logs live ATM IV ÷ India VIX every step (dashboard → *IV calibration*). After a few hundred
   samples, set that ratio as `BTConfig(iv_mult=...)` and rerun research. If nothing passes the gate, stop.
2. **Paper record.** Complete ≥20 paper cycles (~5 months). The governor refuses live mode before that.
3. **Static IP.** Rent a small VPS with a static IP (or get one from your ISP) and register it on your Upstox app
   (My Apps → Static IPs). Orders from any other IP are rejected.
4. **Daily login on that machine.** Set `UPSTOX_CLIENT_ID`, `UPSTOX_CLIENT_SECRET` and `UPSTOX_REDIRECT_URI` (as registered on the app),
   then run `python -m kavach.cli upstox-login`. Tokens expire around 3:30 AM, so this is every trading day.
5. **Start live, manual approval:**
   ```bash
   export UPSTOX_ACCESS_TOKEN=$(cat .upstox_token)
   git clone -b gh-pages https://github.com/<you>/kavach site   # so live state shows on the dashboard
   python -m kavach.cli loop --live --confirm "LIVE MONEY YYYY-MM-DD" --site site --push --github-commands
   ```
   (`--github-commands` also needs a `GITHUB_TOKEN` with Issues read/write, so the live machine obeys dashboard buttons.)
   **Disable the cloud `engine` workflow first** (Actions → engine → Disable), so two engines never manage one book.

## Running it with Claude Code

Read `CLAUDE.md`. Short version: an agent can run research, paper trading and reports. It can propose *tighter* limits.
It cannot loosen limits, reset halts, or start live trading. Those commands need typed phrases with today's date, and
they are for you to type. **Never run a live session with `--dangerously-skip-permissions`.**

## Layout

```
kavach/
  costs.py        Budget-2026 STT, NSE/SEBI/stamp/GST, slippage
  pricing.py      Black-76, greeks, IV solver, smile; lot 65, strike step 50
  expiry.py       Thursday→Tuesday switch (Sep 2025), holiday shifts
  volmodel.py     Garman-Klass + overnight variance, walk-forward log-HAR
  regime.py       stress gate
  strategies/     IronFly (fly/condor), LongStraddle; strategies only describe legs
  backtest.py     weekly-cycle backtester (intraday-extreme stops, full costs)
  intraday_bt.py  thread-style ORB baseline on 5-min bars
  stats.py        Sharpe, Deflated Sharpe, block-bootstrap ruin
  trials.py       trial ledger
  sizing.py       risk-budget lots, evidence-gated capital fraction
  governor.py     constitutional risk governor
  brokers/        paper (Upstox or synthetic quotes), upstox (v3 orders, kill switch)
  live.py         step engine: signal → plan → size → govern → propose/approve → execute → manage
  commands.py     GitHub-issue command channel (owner only)
  dashboard.py    exports JSON for the dashboard
  nse_calendar.py NFO holidays from Upstox's public calendar
  report.py       report.html
site/             the dashboard (static HTML/JS, deployed to gh-pages)
.github/workflows engine (paper runner), research (weekly), tests
limits.toml       the constitution (edit + `cli limits accept`)
state/            trial ledger (engine state lives on the gh-pages branch in the cloud)
```

## Known limits

- Premiums in research are model prices (VIX × iv_mult with an assumed smile), not historical quotes. Calibrate first.
- GitHub's scheduled runs can be late (sometimes 15–30 min) and run at most every 15 minutes, so paper management is coarse.
  Fine for weekly structures; not a scalper.
- Gap risk is captured at daily resolution only.
- This is research software. Not investment advice; I'm not a SEBI-registered adviser.
