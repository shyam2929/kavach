"""RiskGovernor: the only path to a broker.

Invented here: the governor is a *constitution*, not a config file.
  1. Limits live in limits.toml but the governor runs on a LOCKED copy (state/limits.locked.toml)
     whose hash is checked at every start. Editing limits.toml does nothing by itself.
  2. `kavach.cli limits accept` diffs the two. Changes that only TIGHTEN apply immediately.
     Any change that LOOSENS a limit needs a typed confirmation and only takes effect from the
     next calendar day. An agent running at 4am cannot raise its own risk ceiling and trade on it.
  3. Exits are never blocked (except obviously wrong prices). Entries must pass every check.
  4. Drawdown halt is sticky: only `kavach.cli reset-halt` (human) clears it.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import shutil
import time
import tomllib
from dataclasses import dataclass, field, asdict
from pathlib import Path

import numpy as np

from .pricing import LOT_SIZE

ROOT = Path(__file__).resolve().parent.parent
STATE = Path(os.environ.get("KAVACH_STATE", ROOT / "state"))   # engine state (gh-pages in the cloud runner)
LIMITS = ROOT / "limits.toml"
LOCKED = STATE / "limits.locked.toml"
LOCKMETA = STATE / "limits.lock.json"
ACCOUNT = STATE / "account.json"

# direction in which a bigger number is LOOSER (+1) or TIGHTER (-1)
LOOSER_IF_BIGGER = {
    "account.capital_base": +1, "per_trade.risk_per_trade": +1, "per_trade.max_loss_pct": +1,
    "per_trade.max_lots": +1, "daily.max_loss_pct": +1, "daily.max_entries": +1,
    "weekly.max_loss_pct": +1, "drawdown.half_size_at": -1, "drawdown.halt_at": -1,
    "behaviour.max_consecutive_losses": +1, "behaviour.max_open_structures": +1,
    "orders.max_orders_per_sec": +1, "orders.max_price_dev": +1,
    "structure.max_days_to_expiry": +1, "mode.paper_trades_required_for_live": -1,
}


def _flat(d, prefix=""):
    out = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flat(v, key + "."))
        else:
            out[key] = v
    return out


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def diff_limits(old: dict, new: dict) -> list[tuple[str, object, object, str]]:
    """Return [(key, old, new, 'tighter'|'looser'|'changed')]."""
    fo, fn = _flat(old), _flat(new)
    out = []
    for k in sorted(set(fo) | set(fn)):
        a, b = fo.get(k), fn.get(k)
        if a == b:
            continue
        kind = "changed"
        if k in LOOSER_IF_BIGGER and isinstance(a, (int, float)) and isinstance(b, (int, float)):
            kind = "looser" if (b - a) * LOOSER_IF_BIGGER[k] > 0 else "tighter"
        elif k == "orders.limit_only" or k == "structure.defined_risk_only":
            kind = "tighter" if b else "looser"
        elif k == "mode.approval":
            kind = "tighter" if b == "manual" else "looser"
        elif k == "structure.allowed_underlyings":
            kind = "tighter" if set(b) <= set(a) else "looser"
        elif k == "orders.entry_window":
            kind = "tighter" if (b[0] >= a[0] and b[1] <= a[1]) else "looser"
        out.append((k, a, b, kind))
    return out


def accept_limits(confirm: str | None = None, today: dt.date | None = None) -> str:
    """Human-only: promote limits.toml to the locked copy."""
    STATE.mkdir(parents=True, exist_ok=True)
    today = today or dt.date.today()
    new = tomllib.loads(LIMITS.read_text())
    if not LOCKED.exists():
        shutil.copy(LIMITS, LOCKED)
        LOCKMETA.write_text(json.dumps({"sha": _sha(LOCKED), "effective_from": str(today)}))
        return "initial limits locked"
    old = tomllib.loads(LOCKED.read_text())
    changes = diff_limits(old, new)
    if not changes:
        return "no changes"
    looser = [c for c in changes if c[3] != "tighter"]
    if looser:
        phrase = f"I ACCEPT LOOSER LIMITS {today.isoformat()}"
        if confirm != phrase:
            return f"REFUSED. Loosening {[c[0] for c in looser]} requires confirm='{phrase}'"
        eff = today + dt.timedelta(days=1)
    else:
        eff = today
    pending = STATE / "limits.pending.toml"
    shutil.copy(LIMITS, pending)
    LOCKMETA.write_text(json.dumps({"sha": _sha(LOCKED), "pending_sha": _sha(pending),
                                    "effective_from": str(eff)}))
    if eff <= today:
        _activate_pending()
    return f"accepted {len(changes)} change(s), effective {eff}"


def _activate_pending():
    pending = STATE / "limits.pending.toml"
    if pending.exists():
        shutil.move(pending, LOCKED)
        meta = json.loads(LOCKMETA.read_text())
        LOCKMETA.write_text(json.dumps({"sha": _sha(LOCKED), "effective_from": meta["effective_from"]}))


def load_limits(today: dt.date | None = None) -> dict:
    today = today or dt.date.today()
    if not LOCKED.exists():
        accept_limits(today=today)
    meta = json.loads(LOCKMETA.read_text())
    if "pending_sha" in meta and dt.date.fromisoformat(meta["effective_from"]) <= today:
        _activate_pending()
        meta = json.loads(LOCKMETA.read_text())
    if _sha(LOCKED) != meta["sha"]:
        raise RuntimeError("Locked limits file was modified outside `kavach.cli limits accept`. Refusing to run.")
    return tomllib.loads(LOCKED.read_text())


@dataclass
class OrderLeg:
    cp: str              # 'C' / 'P'
    strike: float
    expiry: str          # 'YYYY-MM-DD'
    side: str            # 'BUY' / 'SELL'
    lots: int
    limit_price: float
    bid: float = float("nan")
    ask: float = float("nan")
    security_id: str = ""
    underlying: str = "NIFTY"


@dataclass
class AccountState:
    equity: float
    peak: float
    day: str = ""
    day_start_equity: float = 0.0
    week_start_equity: float = 0.0
    entries_today: int = 0
    open_structures: int = 0
    consecutive_losses: int = 0
    cooldown_until_expiry: str = ""
    halted: bool = False
    halt_reason: str = ""
    trade_returns: list = field(default_factory=list)
    paper_trades: int = 0
    mode: str = "paper"

    @classmethod
    def load(cls, capital: float) -> "AccountState":
        if ACCOUNT.exists():
            return cls(**json.loads(ACCOUNT.read_text()))
        return cls(equity=capital, peak=capital, day_start_equity=capital, week_start_equity=capital)

    def save(self):
        STATE.mkdir(parents=True, exist_ok=True)
        ACCOUNT.write_text(json.dumps(asdict(self), indent=1))


@dataclass
class Decision:
    ok: bool
    reasons: list
    max_loss_rs: float = 0.0


class RiskGovernor:
    def __init__(self, limits: dict | None = None, state: AccountState | None = None):
        self.L = limits or load_limits()
        self.S = state or AccountState.load(self.L["account"]["capital_base"])
        self._stamps: list[float] = []

    # ---------- lifecycle ----------
    def new_day(self, today: dt.date, is_new_cycle: bool):
        if self.S.day != str(today):
            self.S.day = str(today)
            self.S.day_start_equity = self.S.equity
            self.S.entries_today = 0
        if is_new_cycle:
            self.S.week_start_equity = self.S.equity
        self.S.save()

    def mark(self, equity_now: float) -> list[str]:
        """Call on every MTM update. Returns actions: 'FLATTEN_ALL' and/or 'HALT'."""
        L, S = self.L, self.S
        acts = []
        S.equity = equity_now
        S.peak = max(S.peak, equity_now)
        if S.day_start_equity and equity_now / S.day_start_equity - 1 <= -L["daily"]["max_loss_pct"]:
            acts.append("FLATTEN_ALL")
        if equity_now / S.peak - 1 <= L["drawdown"]["halt_at"] and not S.halted:
            S.halted, S.halt_reason = True, f"drawdown {equity_now / S.peak - 1:.1%}"
            acts += ["FLATTEN_ALL", "HALT"]
        S.save()
        return acts

    def on_trade_closed(self, pnl: float, equity_before: float, next_expiry: str, paper: bool):
        S = self.S
        S.trade_returns.append(pnl / equity_before)
        S.open_structures = max(0, S.open_structures - 1)
        S.consecutive_losses = S.consecutive_losses + 1 if pnl < 0 else 0
        if S.consecutive_losses >= self.L["behaviour"]["max_consecutive_losses"]:
            S.cooldown_until_expiry, S.consecutive_losses = next_expiry, 0
        if paper:
            S.paper_trades += 1
        S.save()

    def reset_halt(self, confirm: str):
        phrase = f"RESET HALT {dt.date.today().isoformat()}"
        if confirm != phrase:
            return f"REFUSED. Type exactly: {phrase}"
        self.S.halted, self.S.halt_reason, self.S.peak = False, "", self.S.equity
        self.S.save()
        return "halt cleared; peak reset to current equity"

    # ---------- checks ----------
    def _rate_ok(self) -> bool:
        now = time.monotonic()
        self._stamps = [s for s in self._stamps if now - s < 1.0]
        if len(self._stamps) >= self.L["orders"]["max_orders_per_sec"]:
            return False
        self._stamps.append(now)
        return True

    def wait_rate(self):
        while not self._rate_ok():
            time.sleep(0.05)

    def _price_ok(self, leg: OrderLeg) -> str | None:
        if not (leg.limit_price > 0 and math.isfinite(leg.limit_price)):
            return f"bad limit price {leg.limit_price}"
        if math.isnan(leg.bid) or math.isnan(leg.ask):
            return None
        tol = max(2.0, self.L["orders"]["max_price_dev"] * leg.ask)
        if leg.side == "BUY" and leg.limit_price > leg.ask + tol:
            return f"BUY {leg.cp}{leg.strike} limit {leg.limit_price} far above ask {leg.ask}"
        if leg.side == "SELL" and leg.limit_price < leg.bid - tol:
            return f"SELL {leg.cp}{leg.strike} limit {leg.limit_price} far below bid {leg.bid}"
        return None

    @staticmethod
    def basket_max_loss(legs: list[OrderLeg]) -> float:
        """Worst-case expiry loss in rupees of a basket priced at its limit prices."""
        strikes = [l.strike for l in legs]
        lo, hi = min(strikes) * 0.5, max(strikes) * 1.5
        grid = np.concatenate([np.linspace(lo, hi, 2001), strikes])
        pnl = np.zeros_like(grid)
        for l in legs:
            q = l.lots * LOT_SIZE * (1 if l.side == "BUY" else -1)
            intr = np.maximum(grid - l.strike, 0) if l.cp == "C" else np.maximum(l.strike - grid, 0)
            pnl += q * (intr - l.limit_price)
        # beyond the grid an uncovered short keeps losing: test the slope at the edges
        worst = -pnl.min()
        net_call = sum(l.lots * (1 if l.side == "BUY" else -1) for l in legs if l.cp == "C")
        if net_call < 0:
            worst = float("inf")
        return float(worst)

    @staticmethod
    def defined_risk(legs: list[OrderLeg]) -> str | None:
        for cp in ("C", "P"):
            shorts = sorted([l for l in legs if l.cp == cp and l.side == "SELL"], key=lambda l: l.strike)
            longs = [l for l in legs if l.cp == cp and l.side == "BUY"]
            short_lots = sum(l.lots for l in shorts)
            if short_lots == 0:
                continue
            if cp == "C":
                cover = sum(l.lots for l in longs if l.strike > min(s.strike for s in shorts))
            else:
                cover = sum(l.lots for l in longs if l.strike < max(s.strike for s in shorts))
            if cover < short_lots:
                return f"naked short {cp}: {short_lots} lots short, {cover} covered"
        return None

    def check_entry(self, legs: list[OrderLeg], now: dt.datetime, next_expiry: str) -> Decision:
        L, S = self.L, self.S
        why = []
        if S.halted:
            why.append(f"HALTED ({S.halt_reason}); human must run reset-halt")
        if S.cooldown_until_expiry and next_expiry <= S.cooldown_until_expiry:
            why.append(f"cooldown after {L['behaviour']['max_consecutive_losses']} straight losses until after {S.cooldown_until_expiry}")
        if S.day_start_equity and S.equity / S.day_start_equity - 1 <= -L["daily"]["max_loss_pct"]:
            why.append("daily loss limit reached")
        if S.week_start_equity and S.equity / S.week_start_equity - 1 <= -L["weekly"]["max_loss_pct"]:
            why.append("weekly loss limit reached")
        if S.entries_today >= L["daily"]["max_entries"]:
            why.append("max entries for today")
        if S.open_structures >= L["behaviour"]["max_open_structures"]:
            why.append("max open structures")
        t0, t1 = L["orders"]["entry_window"]
        hhmm = now.strftime("%H:%M")
        if not (t0 <= hhmm <= t1):
            why.append(f"outside entry window {t0}-{t1}")
        for l in legs:
            if l.underlying not in L["structure"]["allowed_underlyings"]:
                why.append(f"underlying {l.underlying} not allowed")
            dte = (dt.date.fromisoformat(l.expiry) - now.date()).days
            if dte > L["structure"]["max_days_to_expiry"] or dte < 0:
                why.append(f"expiry {l.expiry} out of range")
            if l.lots > L["per_trade"]["max_lots"]:
                why.append(f"{l.lots} lots > max {L['per_trade']['max_lots']}")
            p = self._price_ok(l)
            if p:
                why.append(p)
        if L["structure"]["defined_risk_only"]:
            d = self.defined_risk(legs)
            if d:
                why.append(d)
        ml = self.basket_max_loss(legs)
        cap = L["per_trade"]["max_loss_pct"] * S.equity
        if ml > cap:
            why.append(f"worst-case loss Rs {ml:,.0f} > cap Rs {cap:,.0f}")
        dd = S.equity / S.peak - 1
        if dd <= L["drawdown"]["half_size_at"] and ml > cap * 0.5:
            why.append(f"in drawdown {dd:.1%}: size must be halved")
        return Decision(not why, why, ml)

    def check_exit(self, legs: list[OrderLeg]) -> Decision:
        why = [p for p in (self._price_ok(l) for l in legs) if p]
        return Decision(not why, why)

    def record_entry(self):
        self.S.entries_today += 1
        self.S.open_structures += 1
        self.S.save()

    def live_allowed(self) -> tuple[bool, str]:
        need = self.L["mode"]["paper_trades_required_for_live"]
        if self.S.paper_trades < need:
            return False, f"{self.S.paper_trades}/{need} paper trades completed"
        return True, "ok"
