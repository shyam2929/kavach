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


def run_step(engine, site: Path | None, github_commands: bool, now=None) -> str:
    from .live import IST, log
    now = now or dt.datetime.now(IST)
    msgs = []
    if github_commands and os.environ.get("GITHUB_TOKEN"):
        from . import commands
        try:
            for c in commands.pending_commands():
                m = engine.command(c["name"], c["arg"], now)
                msgs.append(f"#{c['number']} {c['name']}: {m}")
                commands.resolve(c["number"], m)
        except Exception as e:
            log(f"command channel error: {e!r}", now)
    try:
        res = engine.step(now)
    except Exception as e:
        res = f"step error: {e!r}"
        log(res, now)
    note = " | ".join(msgs + [res])
    if site:
        engine.export(site / "data", now, note)
    return note


def main(argv=None):
    ap = argparse.ArgumentParser(prog="kavach")
    ap.add_argument("cmd")
    ap.add_argument("sub", nargs="?")
    ap.add_argument("--strategy", default="fly_hold")
    ap.add_argument("--quotes", default="auto", choices=["auto", "synthetic", "upstox"])
    ap.add_argument("--site", default=None, help="dashboard dir; JSON goes to <site>/data")
    ap.add_argument("--github-commands", action="store_true")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--push", action="store_true", help="loop: git commit+push the site dir after each step")
    ap.add_argument("--every", type=int, default=60)
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
        site = Path(a.site) if a.site else None
        if a.cmd == "step":
            print(run_step(eng, site, a.github_commands))
            return
        while True:
            print(run_step(eng, site, a.github_commands))
            if a.push and site:
                subprocess.run(["git", "-C", str(site), "add", "-A"], check=False)
                subprocess.run(["git", "-C", str(site), "commit", "-qm", "kavach: state"], check=False)
                subprocess.run(["git", "-C", str(site), "push", "-q"], check=False)
            time.sleep(a.every)

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
