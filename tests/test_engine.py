"""Step-based engine with a simulated clock: proposals, approval, execution, exits, export, commands."""
import datetime as dt
import json
import shutil
from zoneinfo import ZoneInfo

import pytest

import kavach.governor as gov
import kavach.live as live
from kavach.brokers.paper import PaperBroker, SyntheticQuotes
from kavach.governor import RiskGovernor, AccountState, load_limits
from kavach.strategies import IronFly
from kavach.commands import parse

IST = ZoneInfo("Asia/Kolkata")
T0 = dt.datetime(2026, 11, 4, 10, 30, tzinfo=IST)     # Wed after the 3 Nov expiry


@pytest.fixture
def env(tmp_path, monkeypatch):
    st = tmp_path / "state"; st.mkdir()
    lim = tmp_path / "limits.toml"; shutil.copy(gov.LIMITS, lim)
    for name, val in {"STATE": st, "LIMITS": lim, "LOCKED": st / "limits.locked.toml",
                      "LOCKMETA": st / "limits.lock.json", "ACCOUNT": st / "account.json"}.items():
        monkeypatch.setattr(gov, name, val)
    monkeypatch.setattr(live, "STATE", st)
    return tmp_path


def make(approval="auto", stress=False, spot=25500.0, training=False):
    clock = {"d": T0.date()}
    s = {"s": spot}
    q = SyntheticQuotes(lambda: s["s"], lambda: 13.0, today_fn=lambda: clock["d"])
    b = PaperBroker(q)
    g = RiskGovernor(load_limits(), AccountState(equity=1e5, peak=1e5, day_start_equity=1e5, week_start_equity=1e5))
    e = live.Engine(b, IronFly(wing_z=1.0, take_profit=0.5, stop=0.5, name="fly_test"), g,
                    approval=approval, refresh=False, vix_fn=lambda: 13.0, holidays=set(),
                    training=training)
    e.daily_signal = lambda today: {"day": str(today), "rv_fcst": 10.0, "stress": stress, "vix": 13.0}
    return e, b, clock, s


def test_auto_cycle_wings_first_then_stop(env):
    e, b, clock, s = make("auto")
    assert e.step(T0) == "entered"
    sides = [f["leg"]["side"] for f in e.book["fills"]]
    assert sides == ["BUY", "BUY", "SELL", "SELL"]
    assert e.step(T0 + dt.timedelta(minutes=15)).startswith("holding")
    assert len(b.fills) == 4
    s["s"], clock["d"] = 26600.0, dt.date(2026, 11, 5)
    assert e.step(dt.datetime(2026, 11, 5, 11, 0, tzinfo=IST)) == "stop hit"
    assert e.book is None and len(b.fills) == 8
    assert ",stop," in (env / "state" / "journal.csv").read_text()


def test_manual_proposal_approve_executes(env):
    e, b, clock, s = make("manual")
    r = e.step(T0)
    assert "awaiting approval" in r and e.book is None and not b.fills
    pid = e.es["pending"]["id"]
    assert e.step(T0 + dt.timedelta(minutes=15)).startswith("waiting for approval")
    msg = e.command("approve", pid, T0 + dt.timedelta(minutes=20))
    assert msg == "entered" and e.book and len(b.fills) == 4 and e.es["pending"] is None


def test_reject_and_expiry_and_pause(env):
    e, b, *_ = make("manual")
    e.step(T0)
    pid = e.es["pending"]["id"]
    assert "rejected" in e.command("reject", pid, T0)
    assert "already decided" in e.step(T0 + dt.timedelta(minutes=15))    # no re-proposal this cycle

    e2, *_ = make("manual")
    e2.es["entered_cycle"] = None
    e2.step(T0)
    pid = e2.es["pending"]["id"]
    assert "expired" in e2.command("approve", pid, T0 + dt.timedelta(minutes=60))

    e3, b3, *_ = make("auto")
    e3.command("pause", "", T0)
    assert e3.step(T0) == "paused" and not b3.fills


def test_stress_blocks_and_logs_once(env):
    e, b, *_ = make("auto", stress=True)
    r1 = e.step(T0); r2 = e.step(T0 + dt.timedelta(minutes=15))
    assert "stress" in r1 and not b.fills
    log = (env / "state" / "engine.log").read_text()
    assert log.count("no trade: stress") == 1


def test_export_writes_dashboard_json(env):
    e, b, *_ = make("manual")
    e.step(T0)
    e.export(env / "site" / "data", T0, "test")
    st = json.loads((env / "site" / "data" / "status.json").read_text())
    assert st["mode"] == "paper" and st["pending"]["id"] and len(st["pending"]["payoff"]) == 121
    assert st["iv_calibration"]["n"] == 1
    for f in ("journal.json", "equity.json", "iv.json", "log.json"):
        assert (env / "site" / "data" / f).exists()
    e.command("approve", st["pending"]["id"], T0 + dt.timedelta(minutes=5))
    e.export(env / "site" / "data", T0 + dt.timedelta(minutes=5))
    st = json.loads((env / "site" / "data" / "status.json").read_text())
    assert st["position"]["legs"] and st["position"]["max_loss"] > 0


def test_market_closed_noop(env):
    e, b, *_ = make("auto")
    assert e.step(dt.datetime(2026, 11, 7, 11, 0, tzinfo=IST)) == "market closed"


def test_holiday_is_closed(env):
    e, b, *_ = make("auto")
    e.holidays = {"2026-11-04"}
    assert e.step(T0) == "market holiday" and not b.fills


def test_trading_days_skip_holidays():
    from kavach.nse_calendar import trading_days_between
    import datetime as d
    # Wed 4 Nov -> Mon 9 Nov expiry (Tue 10 Nov is Diwali-Balipratipada)
    assert trading_days_between(d.date(2026, 11, 4), d.date(2026, 11, 9), set()) == 3
    assert trading_days_between(d.date(2026, 11, 4), d.date(2026, 11, 11), {"2026-11-10"}) == 4


def test_training_book_daily_regardless_of_gates(env):
    e, b, clock, s = make("manual", stress=True, training=True)
    e.es["last_signal"] = {"stress": True, "rv_fcst": 10.0, "vix": 13.0}
    r = e.step(T0)
    assert "stress" in r and "training: proposal" in r            # main blocked, training proposes
    p = e.es["data_pending"]
    assert p["gate_open"] is False and p["gate_reason"] == "stress regime" and p["lots"] == 1
    assert p["td_left"] >= 2
    assert "opened" in e.command("approve", p["id"], T0 + dt.timedelta(minutes=5))
    assert len(e.data_book) == 1 and len(b.fills) == 4
    assert [f["leg"]["side"] for f in e.data_book[0]["fills"]] == ["BUY", "BUY", "SELL", "SELL"]
    # one per day: no second proposal today
    e.step(T0 + dt.timedelta(minutes=30))
    assert e.es.get("data_pending") is None and len(e.data_book) == 1
    # next day: a new proposal while the first stays open
    clock["d"] = dt.date(2026, 11, 5)
    e.step(dt.datetime(2026, 11, 5, 10, 0, tzinfo=IST))
    assert e.es["data_pending"] and e.es["data_pending"]["id"] != p["id"]
    # flatten closes training positions and journals them with the gate label
    e.command("flatten", "", dt.datetime(2026, 11, 5, 10, 5, tzinfo=IST))
    assert not e.data_book
    j = (env / "state" / "data_journal.csv").read_text()
    assert "stress regime" in j and "paper-training" in j


def test_training_never_on_live_broker(env):
    e, b, *_ = make("auto", training=True)
    b.paper = False
    from kavach.live import Engine
    e.g.S.paper_trades = 99
    e2 = Engine(b, e.strat, e.g, approval="auto", refresh=False, holidays=set(), training=True)
    assert e2.training is False


def test_command_parse():
    assert parse("kavach: approve ab12cd") == ("approve", "ab12cd")
    assert parse("KAVACH: reset-halt RESET HALT 2026-11-05") == ("reset-halt", "RESET HALT 2026-11-05")
    assert parse("kavach: rm -rf /") is None
    assert parse("hello") is None
