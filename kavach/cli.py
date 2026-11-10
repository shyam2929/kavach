"""Command line.

  python -m kavach.cli research              # grid + promotion gates + trial ledger -> report.html
  python -m kavach.cli orb                   # the thread's breakout approach on last 60d of 5m data
  python -m kavach.cli step  [--quotes upstox|synthetic] [--site DIR] [--github-commands]
                                             # ONE engine step (what the cloud runner calls)
  python -m kavach.cli loop  [--live] ...    # local loop: step every 60s (live needs static IP)
  python -m kavach.cli upstox-login          # daily OAuth for the trading token (live only)
  python -m kavach.cli status | calibrate | refresh
  python -m kavach.cli limits diff|accept [--confirm "..."]     # human only
  python -m kavach.cli reset-halt --confirm "RESET HALT YYYY-MM-DD"
"""
import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
import tomllib
from pathlib import Path


def _strategy(name: str):
    from .strategies import IronFly, LongStraddle
    from .research import HOLD
    table = {
        "fly_hold": IronFly(wing_z=1.0, short_z=0.0, name="fly_1.0_hold", **HOLD),
        "condor_hold": IronFly(wing_z=1.8, short_z=0.8, name="condor_0.8_1.8_hold", **HOLD),
        "condor_06_hold": IronFly(wing_z=1.5, short_z=0.6, name="condor_0.6_1.5_hold", **HOLD),
        "long_straddle": LongStraddle(),
    }
    return table[name]


def _paper_broker(quotes: str):
    """Paper broker on Upstox quotes if a token is present, else a synthetic chain from Yahoo spot+VIX."""
    from .brokers.paper import PaperBroker, SyntheticQuotes
    from . import data
    vix_live = lambda: data.yahoo_last("%5EINDIAVIX")
    if quotes in ("upstox", "auto") and (os.environ.get("UPSTOX_ANALYTICS_TOKEN") or os.environ.get("UPSTOX_ACCESS_TOKEN")):
        from .brokers.upstox import UpstoxMarketData
        return PaperBroker(UpstoxMarketData()), "upstox", vix_live
    if quotes == "upstox":
        sys.exit("--quotes upstox needs UPSTOX_ANALYTICS_TOKEN")
    df = data.load_daily()

    def spot():
        try:
            return data.yahoo_last("%5ENSEI")
        except Exception:
            return float(df.close.iloc[-1])

    def vix():
        try:
            return vix_live()
        except Exception:
            return float(df.vix.iloc[-1])
    return PaperBroker(SyntheticQuotes(spot, vix)), "synthetic", None


def _crude_engine(nifty_engine, approval):
    """Paper crude engine sharing the Nifty governor (one account, combined risk cap)."""
    import datetime as _dt
    import pandas as pd
    from . import crude as C
    from .brokers.paper import PaperBroker
    try:
        master = C.load_master()
    except Exception:
        master = []
    exp_fn = lambda: C.option_expiries_from_master(master, _dt.datetime.now(C.IST).date())
    if (os.environ.get("UPSTOX_ANALYTICS_TOKEN") or os.environ.get("UPSTOX_ACCESS_TOKEN")) and master:
        quotes = C.UpstoxCrudeQuotes(master=master)
    else:
        cache = C.DATA_DIR / "crude_daily.csv"

        def last(col, sym):
            try:
                return C._yahoo_last(sym)
            except Exception:
                return float(pd.read_csv(cache, index_col=0)[col].iloc[-1])
        quotes = C.SyntheticCrudeQuotes(lambda: last("wti", "CL%3DF") * last("inr", "USDINR%3DX"),
                                        lambda: last("ovx", "%5EOVX"), exp_fn)
    sig_cache = {}

    def signal(day):
        if day not in sig_cache:
            try:
                df = C.refresh_crude_daily()
            except Exception:
                df = pd.read_csv(C.DATA_DIR / "crude_daily.csv", index_col=0, parse_dates=True)
            sig_cache[day] = C.crude_signal(df)
        return sig_cache[day]

    risk = lambda: float(nifty_engine.book["max_loss"]) if nifty_engine.book else 0.0
    return C.CrudeEngine(PaperBroker(quotes), nifty_engine.g, approval=approval or nifty_engine.approval,
                         signal_fn=signal, nifty_open_risk_fn=risk)


def run_step(engine, site: Path | None, github_commands: bool, now=None, crude=None) -> str:
    from .live import IST, log, _load, _save
    now = now or dt.datetime.now(IST)
    msgs = []
    actions = _load("actions.json", [])
    if github_commands and os.environ.get("GITHUB_TOKEN"):
        from . import commands
        try:
            for c in commands.pending_commands():
                name, arg = c["name"], c["arg"]
                parts = []
                if crude is not None:
                    mc = crude.command(name, arg, now)
                    if mc:
                        parts.append(mc)
                crude_only = arg.startswith("C") and name in ("approve", "reject", "flatten")
                if not crude_only:
                    parts.append(engine.command(name, arg, now))
                m = " | ".join(parts)
                msgs.append(f"#{c['number']} {name}: {m}")
                if crude_only:
                    log(f"COMMAND {name} {arg} -> {m}", now)
                actions = (actions + [{"ts": now.isoformat(timespec="seconds"), "cmd": name, "arg": arg,
                                       "result": m, "issue": c["number"]}])[-15:]
                _save("actions.json", actions)
                commands.resolve(c["number"], m)
        except Exception as e:
            log(f"command channel error: {e!r}", now)
    try:
        res = engine.step(now)
    except Exception as e:
        res = f"step error: {e!r}"
        log(res, now)
    cres, extra = None, {"actions": actions}
    if crude is not None:
        try:
            cres = "crude: " + crude.step(now)
        except Exception as e:
            cres = f"crude step error: {e!r}"
            log(cres, now)
        try:
            extra["crude"] = crude.export()
        except Exception as e:
            log(f"crude export error: {e!r}", now)
    note = " | ".join(msgs + [res] + ([cres] if cres else []))
    if site:
        engine.export(site / "data", now, note, extra=extra)
    return note


def _dispatch_next(workflow: str = "engine.yml"):
    """Start the next runner. workflow_dispatch is one of the events a GITHUB_TOKEN may trigger."""
    import json as _j
    import urllib.request
    tok, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not (tok and repo):
        return
    ref = os.environ.get("GITHUB_REF_NAME", "main")
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/dispatches",
                                 data=_j.dumps({"ref": ref}).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"})
    for _ in range(3):
        try:
            urllib.request.urlopen(req, timeout=20)
            print("next run dispatched", flush=True)
            return
        except Exception as e:
            print(f"dispatch failed: {e!r}", flush=True)
            time.sleep(10)


def _publish(site: Path):
    """Commit and push the site/state dir; rebase onto any concurrent change first."""
    g = ["git", "-C", str(site)]
    subprocess.run(g + ["add", "-A"], check=False)
    if subprocess.run(g + ["diff", "--cached", "--quiet"]).returncode == 0:
        return
    subprocess.run(g + ["commit", "-qm", f"engine step {dt.datetime.utcnow():%Y-%m-%dT%H:%MZ}"], check=False)
    for _ in range(3):
        if subprocess.run(g + ["push", "-q"]).returncode == 0:
            return
        subprocess.run(g + ["pull", "-q", "--rebase", "-X", "theirs"], check=False)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="kavach")
    ap.add_argument("cmd")
    ap.add_argument("sub", nargs="?")
    ap.add_argument("--strategy", default="fly_hold")
    ap.add_argument("--quotes", default="auto", choices=["auto", "synthetic", "upstox"])
    ap.add_argument("--site", default=None, help="dashboard dir; JSON goes to <site>/data")
    ap.add_argument("--github-commands", action="store_true")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--no-crude", action="store_true", help="disable the MCX crude books")
    ap.add_argument("--push", action="store_true", help="loop: git commit+push the site dir after each step")
    ap.add_argument("--every", type=int, default=60)
    ap.add_argument("--max-minutes", type=int, default=0, help="loop: stop after N minutes (cloud runner)")
    ap.add_argument("--chain", action="store_true",
                    help="cloud loop: stay up outside sessions (idle steps) and dispatch the next run at the end, "
                         "so the runner never depends on GitHub's best-effort cron")
    ap.add_argument("--idle-every", type=int, default=900, help="loop --chain: seconds between steps outside sessions")
    ap.add_argument("--confirm", default=None)
    ap.add_argument("--approval", default=None, choices=[None, "manual", "auto"])
    a = ap.parse_args(argv)

    if a.cmd == "refresh":
        from . import data
        data.refresh_daily(); print("daily data refreshed")

    elif a.cmd == "research":
        from .report import build_report
        print(f"report written: {build_report()}")

    elif a.cmd == "orb":
        from .data import load_intraday_yahoo, load_daily
        from .intraday_bt import run_orb
        t = run_orb(load_intraday_yahoo(), load_daily().vix)
        print(t.to_string(index=False))
        print(f"\ntrades {len(t)}  win {(t.pnl > 0).mean():.0%}  total Rs {t.pnl.sum():,.0f}  charges Rs {t.charges.sum():,.0f}")

    elif a.cmd in ("step", "loop"):
        from .live import Engine
        from .governor import RiskGovernor
        if a.live:
            if a.cmd != "loop":
                sys.exit("live runs only as a local `loop` on your static-IP machine")
            phrase = f"LIVE MONEY {dt.date.today().isoformat()}"
            if a.confirm != phrase:
                sys.exit(f'Refusing. Re-run with --confirm "{phrase}" after reading README "Before going live".')
            if os.environ.get("GITHUB_ACTIONS") == "true":
                sys.exit("Refusing: live trading never runs on GitHub Actions (no static IP, shared runners).")
            from .brokers.upstox import UpstoxBroker
            from . import data
            broker, src, vixf = UpstoxBroker(), "upstox-live", (lambda: data.yahoo_last("%5EINDIAVIX"))
        else:
            broker, src, vixf = _paper_broker(a.quotes)
        eng = Engine(broker, _strategy(a.strategy), RiskGovernor(), approval=a.approval, vix_fn=vixf)
        eng.quote_source = src
        crude = None if (a.live or a.no_crude) else _crude_engine(eng, a.approval)
        site = Path(a.site) if a.site else None
        if a.cmd == "step":
            print(run_step(eng, site, a.github_commands, crude=crude))
            return
        # loop: step every --every seconds. --max-minutes bounds a cloud run (GitHub jobs max 6h);
        # outside market hours a cloud run does one step (to publish state and apply commands) and exits.
        from .live import IST
        t_end = time.time() + a.max_minutes * 60 if a.max_minutes else float("inf")
        while True:
            print(run_step(eng, site, a.github_commands, crude=crude), flush=True)
            if a.push and site:
                _publish(site)
            now = dt.datetime.now(IST)
            # NSE 09:15-15:30; MCX 09:00-23:30 (23:55 in US daylight saving)
            end = dt.time(23, 59) if crude is not None else dt.time(15, 35)
            in_session = now.weekday() < 5 and dt.time(8, 55) <= now.time() <= end
            if a.max_minutes and not in_session and not a.chain:
                break
            wait = a.every if in_session else a.idle_every
            if not in_session and a.chain:
                # wake up in time for the next session open
                nxt = now.replace(hour=8, minute=56, second=0, microsecond=0)
                if nxt <= now:
                    nxt += dt.timedelta(days=1)
                wait = max(30, min(wait, (nxt - now).total_seconds()))
            if time.time() + wait > t_end:
                break
            time.sleep(wait)
        if a.chain:
            _dispatch_next()
    elif a.cmd == "upstox-login":
        from .brokers.upstox import login_url, exchange_code
        cid, sec = os.environ["UPSTOX_CLIENT_ID"], os.environ["UPSTOX_CLIENT_SECRET"]
        redir = os.environ.get("UPSTOX_REDIRECT_URI", "http://127.0.0.1:5000/")
        print("Open this URL, log in, and paste the full URL you are redirected to:\n" + login_url(cid, redir))
        url = input("> ").strip()
        code = url.split("code=")[1].split("&")[0] if "code=" in url else url
        tok = exchange_code(code, cid, sec, redir)
        Path(".upstox_token").write_text(tok)
        os.chmod(".upstox_token", 0o600)
        print("Saved to .upstox_token (valid until ~3:30 AM). Export it: export UPSTOX_ACCESS_TOKEN=$(cat .upstox_token)")

    elif a.cmd == "status":
        from .governor import RiskGovernor
        g = RiskGovernor()
        s = vars(g.S).copy(); s["trade_returns"] = f"{len(g.S.trade_returns)} trades"
        print(json.dumps(s, indent=1)); print("live allowed:", g.live_allowed())

    elif a.cmd == "calibrate":
        from .dashboard import iv_calibration, _csv_rows
        from .live import P
        print(json.dumps(iv_calibration(_csv_rows(P("iv_samples.csv"))), indent=1))

    elif a.cmd == "limits":
        from .governor import diff_limits, accept_limits, LIMITS, LOCKED
        if a.sub == "diff":
            if not LOCKED.exists():
                print("no locked limits yet; run `limits accept`")
            else:
                for k, o, n, kind in diff_limits(tomllib.loads(LOCKED.read_text()), tomllib.loads(LIMITS.read_text())):
                    print(f"{kind:8s} {k}: {o} -> {n}")
        elif a.sub == "accept":
            print(accept_limits(a.confirm))

    elif a.cmd == "reset-halt":
        from .governor import RiskGovernor
        print(RiskGovernor().reset_halt(a.confirm or ""))
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
