"""Crude module: MCX calendar, both books, gating, execution, expiry handling, Upstox chain parsing."""
import datetime as dt
import shutil
from zoneinfo import ZoneInfo

import pytest

import kavach.crude as crude
import kavach.governor as gov
from kavach.brokers.paper import PaperBroker
from kavach.crude import CrudeEngine, MCXCalendar, SyntheticCrudeQuotes, UpstoxCrudeQuotes, crude_signal, LOT
from kavach.governor import RiskGovernor, AccountState, load_limits

IST = ZoneInfo("Asia/Kolkata")
# Upstox holiday rows as published for Nov-Dec 2026 (trimmed)
ROWS = [
    {"date": "2026-11-10", "closed_exchanges": ["NSE", "NFO", "BSE"],
     "open_exchanges": [{"exchange": "MCX", "start_time": 1794310200000, "end_time": 1794335100000}]},
    {"date": "2026-11-24", "closed_exchanges": ["NSE", "NFO", "BSE"],
     "open_exchanges": [{"exchange": "MCX", "start_time": 1795519800000, "end_time": 1795544700000}]},
    {"date": "2026-12-25", "closed_exchanges": ["NSE", "MCX"], "open_exchanges": []},
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    st = tmp_path / "state"; st.mkdir()
    lim = tmp_path / "limits.toml"; shutil.copy(gov.LIMITS, lim)
    for name, val in {"STATE": st, "LIMITS": lim, "LOCKED": st / "limits.locked.toml",
                      "LOCKMETA": st / "limits.lock.json", "ACCOUNT": st / "account.json"}.items():
        monkeypatch.setattr(gov, name, val)
    monkeypatch.setattr(crude, "STATE", st)
    return tmp_path


def test_calendar_sessions():
    c = MCXCalendar(ROWS)
    assert c.session(dt.date(2026, 11, 6)) == (dt.time(9), dt.time(23, 30))       # US standard time
    assert c.session(dt.date(2026, 7, 6)) == (dt.time(9), dt.time(23, 55))        # US daylight saving
    s = c.session(dt.date(2026, 11, 10))                                           # Diwali: evening session only
    assert s and s[0] >= dt.time(17) and s[1] <= dt.time(23, 59)
    assert c.session(dt.date(2026, 12, 25)) is None and c.session(dt.date(2026, 11, 7)) is None
    assert c.is_open(dt.datetime(2026, 11, 6, 22, 0, tzinfo=IST))
    assert not c.is_open(dt.datetime(2026, 11, 6, 23, 45, tzinfo=IST))
    assert c.trading_days_between(dt.date(2026, 11, 6), dt.date(2026, 11, 17)) == 7


def make(env, approval="manual", stress=False, fut=8480.0, ovx=40.0):
    clock = {"d": dt.date(2026, 11, 9)}
    q = SyntheticCrudeQuotes(lambda: fut, lambda: ovx, lambda: ["2026-11-17", "2026-12-16", "2027-01-15"],
                             today_fn=lambda: clock["d"])
    b = PaperBroker(q)
    g = RiskGovernor(load_limits(), AccountState(equity=1e5, peak=1e5, day_start_equity=1e5, week_start_equity=1e5))
    sig = {"stress": stress, "stress_reasons": ["OVX +20% in 5 days"] if stress else [], "ovx": ovx}
    e = CrudeEngine(b, g, approval=approval, calendar=MCXCalendar(ROWS), signal_fn=lambda d: sig)
    return e, b, clock


def test_both_books_propose_with_right_expiries(env):
    e, b, _ = make(env)
    r = e.step(dt.datetime(2026, 11, 9, 10, 5, tzinfo=IST))
    pm, pt = e.st["pending"]["main"], e.st["pending"]["training"]
    assert pm["expiry"] == "2026-12-16" and 12 <= pm["td_left"] <= 30          # main: ~monthly
    assert pt["expiry"] == "2026-11-17" and pt["td_left"] >= 5                 # training: nearest with >= 5
    assert pm["id"].startswith("C") and pt["id"].startswith("CT")
    # condor shape, defined risk, Mini lot maths
    sides = sorted((l["side"], l["cp"]) for l in pm["legs"])
    assert sides == [("BUY", "C"), ("BUY", "P"), ("SELL", "C"), ("SELL", "P")]
    widths = [abs(a["strike"] - c["strike"]) for a in pm["legs"] for c in pm["legs"]
              if a["cp"] == c["cp"] and a["side"] != c["side"]]
    assert 0 < pm["max_loss"] <= max(widths) * LOT and pm["max_loss"] <= 0.10 * 1e5
    assert pm["credit"] > 0


def test_gate_blocks_main_not_training(env):
    e, b, _ = make(env, stress=True)
    e.step(dt.datetime(2026, 11, 9, 10, 5, tzinfo=IST))
    assert e.st["pending"].get("main") is None
    t = e.st["pending"]["training"]
    assert t["gate_open"] is False and "OVX" in t["gate_reason"]


def test_approve_executes_wings_first_and_target_and_preexpiry_close(env):
    e, b, clock = make(env)
    now = dt.datetime(2026, 11, 9, 10, 5, tzinfo=IST)
    e.step(now)
    pid = e.st["pending"]["training"]["id"]
    assert "opened" in e.command("approve", pid, now + dt.timedelta(minutes=2))
    pos = e.st["positions"][0]
    assert [f["leg"]["side"] for f in pos["fills"]] == ["BUY", "BUY", "SELL", "SELL"]
    assert all(f["leg"]["lot_size"] == LOT for f in pos["fills"])
    # no second training proposal today
    e.step(now + dt.timedelta(minutes=30))
    assert e.st["pending"].get("training") is None
    # Nov 13 (2 trading days before Nov 17) at 17:05 -> closed to avoid devolvement
    clock["d"] = dt.date(2026, 11, 13)
    e.step(dt.datetime(2026, 11, 13, 17, 5, tzinfo=IST))
    assert not any(p["id"] == pid for p in e.st["positions"])
    assert "pre-expiry" in (env / "state" / "crude_training_journal.csv").read_text()


def test_eia_window_and_session(env):
    e, b, _ = make(env)
    assert e.step(dt.datetime(2026, 11, 7, 12, 0, tzinfo=IST)) == "MCX closed"          # Saturday
    e.step(dt.datetime(2026, 11, 11, 19, 0, tzinfo=IST))                                # Wednesday after 18:00
    assert not any(e.st["pending"].values())


def test_reject_and_flatten_commands(env):
    e, b, _ = make(env)
    now = dt.datetime(2026, 11, 9, 10, 5, tzinfo=IST)
    e.step(now)
    pm = e.st["pending"]["main"]["id"]
    assert "skipped" in e.command("reject", pm, now)
    assert e.command("approve", "T12345", now) is None          # Nifty id: not for crude
    pt = e.st["pending"]["training"]["id"]
    e.command("approve", pt, now)
    assert "1 crude" in e.command("flatten", "", now)
    assert not e.st["positions"]


def test_signal_flags_stress():
    import pandas as pd
    idx = pd.date_range("2026-10-01", periods=30, freq="B")
    df = pd.DataFrame({"wti": 90.0, "ovx": 40.0, "inr": 94.0}, index=idx)
    df.loc[idx[-1], "ovx"] = 50.0
    df["mcx"] = df.wti * df.inr
    s = crude_signal(df)
    assert s["stress"] and "OVX" in s["stress_reasons"][0]


def test_upstox_crude_chain_parsing():
    exp_ms = int(dt.datetime(2026, 12, 16, 23, 30, tzinfo=IST).timestamp() * 1000)
    master = [{"underlying_symbol": "CRUDEOILM", "instrument_type": t, "expiry": exp_ms, "strike_price": k,
               "instrument_key": f"MCX_FO|{int(k)}{t}", "underlying_key": "MCX_FO|FUT1"}
              for k in (8400.0, 8450.0, 8500.0) for t in ("CE", "PE")]

    class H:
        def __call__(self, m, url, params=None, body=None):
            keys = params["instrument_key"].split(",")
            data = {}
            for k in keys:
                if k == "MCX_FO|FUT1":
                    data["MCX_FO:CRUDEOILM DEC FUT"] = {"instrument_token": k, "last_price": 8480.0}
                else:
                    data[f"MCX_FO:{k}"] = {"instrument_token": k, "last_price": 150.0,
                                           "depth": {"buy": [{"price": 148.0}], "sell": [{"price": 152.0}]}}
            return {"status": "success", "data": data}

    q = UpstoxCrudeQuotes(master=master, http=H())
    assert q.spot("2026-12-16") == 8480.0
    ch = q.chain("2026-12-16")
    assert len(ch) == 6 and set(ch.cp) == {"C", "P"}
    r = ch[(ch.strike == 8450) & (ch.cp == "P")].iloc[0]
    assert (r.bid, r.ask) == (148.0, 152.0) and r.security_id == "MCX_FO|8450PE"


def test_paper_snapshot_fills_marketable_sell_despite_ticking_feed():
    """Regression (10 Nov 2026): the feed moved between pricing and filling, so a sell at the bid
    was 'not marketable' and a good trade was unwound."""
    import itertools
    import pandas as pd
    from kavach.brokers.paper import PaperBroker
    from kavach.governor import OrderLeg
    tick = itertools.count()

    class Ticking:
        def chain(self, expiry):
            b = 100.0 - next(tick)          # bid falls by 1 on every fetch
            return pd.DataFrame([{"strike": 7700.0, "cp": "P", "bid": b, "ask": b + 2, "ltp": b + 1,
                                  "iv": .5, "security_id": "x"}])
    br = PaperBroker(Ticking(), clock=lambda: 0.0)
    r = br.chain("2026-11-17").iloc[0]
    leg = OrderLeg("P", 7700.0, "2026-11-17", "SELL", 1, float(r.bid), float(r.bid), float(r.ask), "x")
    assert br.place(leg)["status"] == "FILLED"
