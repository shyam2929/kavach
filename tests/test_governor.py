import datetime as dt
import shutil

import pytest

import kavach.governor as gov
from kavach.governor import RiskGovernor, OrderLeg, AccountState, accept_limits, load_limits, diff_limits


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    st = tmp_path / "state"
    st.mkdir()
    lim = tmp_path / "limits.toml"
    shutil.copy(gov.LIMITS, lim)
    for name, val in {"STATE": st, "LIMITS": lim, "LOCKED": st / "limits.locked.toml",
                      "LOCKMETA": st / "limits.lock.json", "ACCOUNT": st / "account.json"}.items():
        monkeypatch.setattr(gov, name, val)
    yield tmp_path


def G(equity=100000.0):
    L = load_limits()
    return RiskGovernor(L, AccountState(equity=equity, peak=equity, day_start_equity=equity,
                                        week_start_equity=equity))


NOW = dt.datetime(2026, 11, 4, 10, 30)
EXP = "2026-11-09"


def fly(lots=1, wing=500):
    return [OrderLeg("P", 25500 - wing, EXP, "BUY", lots, 20, 19.5, 20.5),
            OrderLeg("C", 25500 + wing, EXP, "BUY", lots, 18, 17.5, 18.5),
            OrderLeg("P", 25500, EXP, "SELL", lots, 190, 189, 191),
            OrderLeg("C", 25500, EXP, "SELL", lots, 185, 184, 186)]


def test_defined_risk_fly_passes():
    d = G().check_entry(fly(), NOW, EXP)
    assert d.ok, d.reasons
    assert 0 < d.max_loss_rs < 15000


def test_naked_short_blocked():
    legs = fly()[2:]          # only the short straddle
    d = G().check_entry(legs, NOW, EXP)
    assert not d.ok and any("naked" in r for r in d.reasons)


def test_oversize_blocked():
    d = G().check_entry(fly(lots=3), NOW, EXP)
    assert not d.ok and any("max" in r for r in d.reasons)


def test_wide_wings_exceed_loss_cap():
    d = G().check_entry(fly(wing=1500), NOW, EXP)
    assert not d.ok and any("worst-case" in r for r in d.reasons)


def test_bad_limit_price_blocked():
    legs = fly(); legs[0].limit_price = 60    # pay 60 for a 20.5 ask
    d = G().check_entry(legs, NOW, EXP)
    assert not d.ok


def test_outside_window_blocked():
    d = G().check_entry(fly(), dt.datetime(2026, 11, 4, 15, 0), EXP)
    assert not d.ok


def test_drawdown_halt_is_sticky_and_needs_human():
    g = G()
    acts = g.mark(79000)
    assert "HALT" in acts and g.S.halted
    g.mark(100000)
    assert g.S.halted
    assert not g.check_entry(fly(), NOW, EXP).ok
    assert "REFUSED" in g.reset_halt("please")
    assert "cleared" in g.reset_halt(f"RESET HALT {dt.date.today().isoformat()}")


def test_consecutive_losses_cooldown():
    g = G()
    for _ in range(3):
        g.on_trade_closed(-1000, 100000, "2026-11-09", paper=True)
    d = g.check_entry(fly(), NOW, "2026-11-09")
    assert not d.ok and any("cooldown" in r for r in d.reasons)


def test_daily_loss_flatten():
    g = G()
    assert "FLATTEN_ALL" not in g.mark(90000)   # inside defined risk: no forced exit
    assert "FLATTEN_ALL" in g.mark(83000)


def test_limits_loosening_needs_confirmation_and_waits_a_day(isolated_state):
    load_limits()                                   # locks initial copy
    lim = gov.LIMITS
    lim.write_text(lim.read_text().replace("max_lots = 2", "max_lots = 10"))
    today = dt.date(2026, 11, 5)
    assert "REFUSED" in accept_limits(None, today)
    msg = accept_limits(f"I ACCEPT LOOSER LIMITS {today}", today)
    assert "2026-11-06" in msg
    assert load_limits(today)["per_trade"]["max_lots"] == 2          # not yet
    assert load_limits(dt.date(2026, 11, 6))["per_trade"]["max_lots"] == 10


def test_limits_tightening_is_immediate(isolated_state):
    load_limits()
    lim = gov.LIMITS
    lim.write_text(lim.read_text().replace("max_lots = 2", "max_lots = 1"))
    today = dt.date(2026, 11, 5)
    assert "effective 2026-11-05" in accept_limits(None, today)
    assert load_limits(today)["per_trade"]["max_lots"] == 1


def test_tampering_with_locked_file_refuses(isolated_state):
    load_limits()
    gov.LOCKED.write_text(gov.LOCKED.read_text().replace("max_lots = 2", "max_lots = 50"))
    with pytest.raises(RuntimeError):
        load_limits()


def test_live_requires_paper_history():
    g = G()
    assert not g.live_allowed()[0]
    g.S.paper_trades = 20
    assert g.live_allowed()[0]
