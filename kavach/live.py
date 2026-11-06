"""Engine: same code for paper and live; only the Broker differs.

Designed to run as discrete STEPS (a GitHub Actions run every ~15 min, or a local loop), with all
state on disk, so any step can be killed and the next one carries on.

Cycle logic (weekly):
  * Entry: once per expiry cycle, inside the entry window, when 3-6 trading days remain.
  * Signal: walk-forward HAR forecast + regime gate from daily data; ATM IV from the LIVE chain.
  * Approval: in 'manual' mode the engine writes a PROPOSAL and stops. You approve or reject it
    from the dashboard (GitHub issue command). Approval re-prices and re-checks the governor.
  * Execution: wings (BUY) first, then body (SELL). A failed short leg unwinds the wings.
  * Management: MTM each step; take-profit / stop per plan; governor may force FLATTEN_ALL.
    Expiry day: legs within 1% of spot are closed by 15:10; far-OTM legs expire worthless.
  * Calibration: every step logs live ATM IV vs India VIX to iv_samples.csv. That builds the
    real IV/VIX ratio the backtest needs, for free, from your Upstox data.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import math
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from .brokers import Broker, mid
from .governor import RiskGovernor, OrderLeg, STATE, accept_limits
from .pricing import forward, implied_vol, LOT_SIZE
from .regime import regime_frame
from .sizing import capital_fraction, lots_for
from .volmodel import WalkForwardHAR
from . import data

IST = ZoneInfo("Asia/Kolkata")
PROPOSAL_TTL_MIN = 45


def P(name: str) -> Path:
    return STATE / name


def log(msg: str, now: dt.datetime | None = None):
    STATE.mkdir(parents=True, exist_ok=True)
    line = f"{(now or dt.datetime.now(IST)):%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    with P("engine.log").open("a") as f:
        f.write(line + "\n")


def _append_csv(path: Path, row: dict):
    new = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if new:
            w.writeheader()
        w.writerow(row)


def trading_days_between(a: dt.date, b: dt.date, closed: set[str] | None = None) -> int:
    from .nse_calendar import trading_days_between as tdb
    return tdb(a, b, closed)


def _load(name, default):
    p = P(name)
    return json.loads(p.read_text()) if p.exists() else default


def _save(name, obj):
    STATE.mkdir(parents=True, exist_ok=True)
    P(name).write_text(json.dumps(obj, indent=1, default=str))


class Engine:
    def __init__(self, broker: Broker, strategy, governor: RiskGovernor | None = None,
                 approval: str | None = None, refresh: bool = True, vix_fn=None,
                 holidays: set[str] | None = None, training: bool = True):
        self.b, self.strat = broker, strategy
        self.g = governor or RiskGovernor()
        self.approval = approval or self.g.L["mode"]["approval"]
        self.refresh = refresh
        self.vix_fn = vix_fn
        if holidays is None:
            from .nse_calendar import nfo_closed_dates
            holidays = nfo_closed_dates()
        self.holidays = holidays
        self.es = _load("engine.json", {"entered_cycle": None, "paused": False, "pending": None,
                                        "last_step": None, "last_signal": None, "last_iv": None,
                                        "strategy": strategy.name})
        self.book = _load("book.json", None)
        self.data_book = _load("data_book.json", [])      # training book: daily 1-lot paper trades
        self.training = training and broker.paper           # never on a live broker
        self._signal_cache = None
        if not broker.paper:
            ok, why = self.g.live_allowed()
            if not ok:
                raise SystemExit(f"LIVE refused by governor: {why}. Run in paper mode first.")
            self.g.S.mode = "live"
        else:
            self.g.S.mode = "paper"

    def _persist(self):
        _save("engine.json", self.es)
        _save("data_book.json", self.data_book)
        if self.book:
            _save("book.json", self.book)
        else:
            P("book.json").unlink(missing_ok=True)
        self.g.S.save()

    # ---------------- signal ----------------
    def daily_signal(self, today: dt.date) -> dict:
        if self._signal_cache and self._signal_cache["day"] == str(today):
            return self._signal_cache
        if self.refresh:
            try:
                data.refresh_daily()
            except Exception as e:
                log(f"daily refresh failed ({e}); using cached data")
        df = data.load_daily()
        fc = WalkForwardHAR().forecast_series(df).iloc[-1]
        reg = regime_frame(df).iloc[-1]
        self._signal_cache = {"day": str(today), "asof": str(df.index[-1].date()), "rv_fcst": float(fc),
                              "stress": bool(reg["stress"]), "vix": float(df.vix.iloc[-1]),
                              "vix_ch5": float(reg["vix_ch5"]), "ret5": float(reg["ret5"]), "dd20": float(reg["dd20"]),
                              "spot_close": float(df.close.iloc[-1])}
        return self._signal_cache

    def atm_iv(self, chain: pd.DataFrame, spot: float, expiry: str, now: dt.datetime):
        td = trading_days_between(now.date(), dt.date.fromisoformat(expiry), self.holidays)
        frac_left = max(0.0, (15.5 - (now.hour + now.minute / 60)) / 6.25)
        t = max((td + frac_left) / 252, 0.25 / 252)
        F = forward(spot, t)
        K = chain.strike.iloc[(chain.strike - F).abs().argsort().iloc[0]]
        ivs = []
        for cp in ("C", "P"):
            r = chain[(chain.strike == K) & (chain.cp == cp)]
            if not r.empty:
                try:
                    ivs.append(implied_vol(mid(r.iloc[0]), F, K, t, cp))
                except ValueError:
                    pass
        ivs = [v for v in ivs if v == v]
        return (float(np.mean(ivs)) if ivs else float("nan")), t, F, float(K)

    # ---------------- order helpers ----------------
    def _leg(self, chain, cp, K, expiry, side, lots, aggress=0.3) -> OrderLeg:
        r = chain[(chain.strike == K) & (chain.cp == cp)]
        if r.empty:
            raise ValueError(f"strike {K}{cp} not in chain")
        r = r.iloc[0]
        m = mid(r)
        px = m + aggress * (r.ask - m) if side == "BUY" else m - aggress * (m - r.bid)
        tick = 20 * max(0.05, px)
        px = (math.ceil(tick - 1e-9) if side == "BUY" else math.floor(tick + 1e-9)) / 20
        return OrderLeg(cp, float(K), expiry, side, lots, max(0.05, px), float(r.bid), float(r.ask), str(r.security_id))

    def _execute(self, legs: list[OrderLeg], expiry: str) -> list[dict] | None:
        done = []
        for leg in sorted(legs, key=lambda l: 0 if l.side == "BUY" else 1):
            fill, lg = None, None
            for aggress in (0.3, 0.7, 1.0):
                lg = self._leg(self.b.chain(expiry), leg.cp, leg.strike, expiry, leg.side, leg.lots, aggress)
                chk = self.g.check_exit([lg])
                if not chk.ok:
                    log(f"price check failed {chk.reasons}"); continue
                self.g.wait_rate()
                fill = self.b.place(lg)
                if fill["status"] == "FILLED":
                    break
                if fill["status"] == "PARTIAL":
                    done.append({"leg": vars(lg) | {"lots": fill["filled_qty"] // LOT_SIZE}, "price": fill["avg_price"]})
                    fill = None
                    break
            if not fill or fill["status"] != "FILLED":
                log(f"could not fill {leg.side} {leg.cp}{leg.strike:.0f}; unwinding {len(done)} legs")
                self._unwind(done, expiry)
                return None
            done.append({"leg": vars(lg), "price": fill["avg_price"]})
            log(f"FILLED {lg.side} {lg.lots}x {lg.cp}{lg.strike:.0f} @ {fill['avg_price']:.2f}")
        return done

    def _unwind(self, filled: list[dict], expiry: str):
        for f in filled:
            L = f["leg"]
            if L["lots"] <= 0:
                continue
            side = "SELL" if L["side"] == "BUY" else "BUY"
            for aggress in (0.7, 1.0, 1.0):
                lg = self._leg(self.b.chain(expiry), L["cp"], L["strike"], expiry, side, L["lots"], aggress)
                self.g.wait_rate()
                if self.b.place(lg)["status"] == "FILLED":
                    break
            else:
                log(f"!!! FAILED TO UNWIND {L}. KILL SWITCH. MANUAL ACTION REQUIRED.")
                self.b.kill_switch()

    # ---------------- book ----------------
    def leg_marks(self) -> list[dict]:
        if not self.book:
            return []
        ch = self.b.chain(self.book["expiry"])
        out = []
        for f in self.book["fills"]:
            L = f["leg"]
            r = ch[(ch.strike == L["strike"]) & (ch.cp == L["cp"])]
            r = r.iloc[0] if not r.empty else None
            liq = (r.bid if L["side"] == "BUY" else r.ask) if r is not None else float("nan")
            sgn = 1 if L["side"] == "BUY" else -1
            out.append({"cp": L["cp"], "strike": L["strike"], "side": L["side"], "lots": L["lots"],
                        "entry": f["price"], "bid": None if r is None else float(r.bid),
                        "ask": None if r is None else float(r.ask), "mid": None if r is None else float(mid(r)),
                        "pnl": sgn * (liq - f["price"]) * L["lots"] * LOT_SIZE})
        return out

    def mtm(self) -> float:
        return float(sum(m["pnl"] for m in self.leg_marks()))

    def close_book(self, reason: str, now: dt.datetime, only_near: float | None = None):
        if not self.book:
            return
        exp = self.book["expiry"]
        spot = self.b.spot()
        pnl = self.mtm()
        to_close = [f for f in self.book["fills"]
                    if only_near is None or abs(f["leg"]["strike"] / spot - 1) < only_near]
        self._unwind(to_close, exp)
        eq_before = self.book["equity_at_entry"]
        self.g.S.equity = eq_before + pnl
        self.g.S.peak = max(self.g.S.peak, self.g.S.equity)
        _append_csv(P("journal.csv"), {
            "closed": now.isoformat(timespec="minutes"), "opened": self.book["opened"], "expiry": exp,
            "strategy": self.book["strategy"], "lots": self.book["lots"], "credit": round(self.book["credit"], 2),
            "max_loss": round(self.book["max_loss"], 2), "pnl": round(pnl, 2), "reason": reason,
            "mode": "paper" if self.b.paper else "live"})
        later = [e for e in self.b.expiries() if e > exp]
        self.g.on_trade_closed(pnl, eq_before, later[0] if later else exp, self.b.paper)
        log(f"CLOSED ({reason}) pnl {pnl:,.0f}", now)
        self.book = None

    # ---------------- proposal / execution ----------------
    def _build(self, now: dt.datetime, expiry: str):
        sig = self.daily_signal(now.date())
        chain = self.b.chain(expiry)
        spot = self.b.spot()
        iv, t, F, _ = self.atm_iv(chain, spot, expiry, now)
        self.es["last_signal"] = {k: v for k, v in sig.items() if k != "day"} | {"atm_iv": iv, "spot": spot}
        if not iv == iv:
            return None, "no ATM IV from chain"
        plan = self.strat.plan(spot, t, iv, sig["rv_fcst"], sig["stress"])
        if plan is None:
            why = "stress regime: short-vol entries blocked" if sig["stress"] else \
                  f"no edge: ATM IV {iv * 100:.1f} < forecast RV {sig['rv_fcst']:.1f}"
            return None, why
        strikes = sorted(set(chain.strike))
        legs1 = [self._leg(chain, l.cp, min(strikes, key=lambda k: abs(k - l.K)), expiry,
                           "BUY" if l.qty > 0 else "SELL", 1) for l in plan.legs]
        ml1 = self.g.basket_max_loss(legs1)
        dd = self.g.S.equity / self.g.S.peak - 1
        frac = capital_fraction(self.g.S.trade_returns, dd)
        lots = lots_for(ml1, self.g.S.equity, self.g.L["per_trade"]["risk_per_trade"], frac,
                        self.g.L["per_trade"]["max_lots"], self.g.L["per_trade"]["max_loss_pct"])
        if lots == 0:
            return None, f"size 0 (worst case/lot Rs {ml1:,.0f}, capital fraction {frac:.2f})"
        legs = [OrderLeg(**{**vars(l), "lots": lots}) for l in legs1]
        return {"plan": plan, "legs": legs, "spot": spot, "atm_iv": iv, "cap_frac": frac, "lots": lots}, None

    def _proposal_dict(self, b, dec, now, expiry):
        legs = b["legs"]
        credit = sum((1 if l.side == "SELL" else -1) * l.limit_price for l in legs) * b["lots"] * LOT_SIZE
        pid = hashlib.sha1(f"{now.isoformat()}{expiry}".encode()).hexdigest()[:6]
        return {"id": pid, "created": now.isoformat(timespec="minutes"), "expiry": expiry,
                "strategy": b["plan"].name, "lots": b["lots"], "spot": b["spot"], "atm_iv": b["atm_iv"],
                "cap_frac": b["cap_frac"], "credit": credit, "max_loss": dec.max_loss_rs,
                "take_profit": b["plan"].take_profit, "stop": b["plan"].stop,
                "legs": [{"cp": l.cp, "strike": l.strike, "side": l.side, "lots": l.lots, "limit": l.limit_price,
                          "bid": l.bid, "ask": l.ask} for l in legs]}

    def _open(self, b, dec, now, expiry) -> bool:
        fills = self._execute(b["legs"], expiry)
        self.es["entered_cycle"] = expiry
        if not fills:
            return False
        self.g.record_entry()
        legs = b["legs"]
        credit = sum((1 if f["leg"]["side"] == "SELL" else -1) * f["price"] for f in fills) * b["lots"] * LOT_SIZE
        ml = self.g.basket_max_loss([OrderLeg(**{**f["leg"], "limit_price": f["price"]}) for f in fills])
        self.book = {"opened": now.isoformat(timespec="minutes"), "expiry": expiry, "strategy": b["plan"].name,
                     "lots": b["lots"], "fills": fills, "equity_at_entry": self.g.S.equity, "credit": credit,
                     "max_loss": ml,
                     "tp_rs": b["plan"].take_profit * (credit if credit > 0 else -credit),
                     "stop_rs": b["plan"].stop * (ml if credit > 0 else -credit)}
        log(f"OPENED {b['plan'].name} x{b['lots']} credit Rs {credit:,.0f} worst case Rs {ml:,.0f}", now)
        return True

    # ---------------- commands (from dashboard / GitHub issues) ----------------
    def command(self, name: str, arg: str, now: dt.datetime) -> str:
        name = name.lower()
        if name == "pause":
            self.es["paused"] = True; msg = "paused: no new entries"
        elif name == "resume":
            self.es["paused"] = False; msg = "resumed"
        elif name == "flatten":
            parts = []
            if self.book:
                self.close_book("manual flatten", now); parts.append("main position closed")
            n = 0
            for pos in list(self.data_book):
                if not arg or arg == pos["id"]:
                    self._close_data(pos, "manual flatten", now); n += 1
            if n:
                parts.append(f"{n} training position(s) closed")
            msg = "; ".join(parts) or "no open position"
        elif name == "reject" and self.es.get("data_pending") and arg == self.es["data_pending"]["id"]:
            self.es["data_pending"] = None
            self.es["data_last_day"] = now.date().isoformat()
            msg = f"training proposal {arg} rejected for today"
        elif name == "approve" and self.es.get("data_pending") and arg == self.es["data_pending"]["id"]:
            msg = self._approve_data(arg, now)
        elif name == "reject":
            p = self.es.get("pending")
            if p and (not arg or arg == p["id"]):
                self.es["pending"] = None; self.es["entered_cycle"] = p["expiry"]; msg = f"proposal {p['id']} rejected"
            else:
                msg = "no matching proposal"
        elif name == "approve":
            msg = self._approve(arg, now)
        elif name == "reset-halt":
            msg = self.g.reset_halt(arg)
        elif name == "limits-accept":
            msg = accept_limits(arg or None)
        elif name == "step":
            msg = "step requested"
        else:
            msg = f"unknown command {name}"
        log(f"COMMAND {name} {arg} -> {msg}", now)
        self._persist()
        return msg

    def _approve(self, pid: str, now: dt.datetime) -> str:
        p = self.es.get("pending")
        if not p or (pid and pid != p["id"]):
            return "no matching proposal"
        age = (now - dt.datetime.fromisoformat(p["created"])).total_seconds() / 60
        if age > PROPOSAL_TTL_MIN:
            self.es["pending"] = None
            return f"proposal {p['id']} expired ({age:.0f} min old); a fresh one will be made next step"
        if self.book:
            self.es["pending"] = None
            return "already have a position"
        b, why = self._build(now, p["expiry"])
        if b is None:
            self.es["pending"] = None
            return f"conditions changed, not entering: {why}"
        dec = self.g.check_entry(b["legs"], now, p["expiry"])
        if not dec.ok:
            self.es["pending"] = None
            return f"governor blocked at approval: {dec.reasons}"
        if abs(b["spot"] / p["spot"] - 1) > 0.006:
            self.es["pending"] = self._proposal_dict(b, dec, now, p["expiry"])
            return "spot moved >0.6% since proposal; re-proposed with fresh prices, approve again"
        self.es["pending"] = None
        return "entered" if self._open(b, dec, now, p["expiry"]) else "execution failed; legs unwound"

    # ---------------- calibration samples ----------------
    def _sample_iv(self, now, expiry):
        if self.vix_fn is None:
            return
        try:
            chain, spot = self.b.chain(expiry), self.b.spot()
            iv, t, F, K = self.atm_iv(chain, spot, expiry, now)
            vix = float(self.vix_fn())
            if iv == iv and vix > 0:
                _append_csv(P("iv_samples.csv"), {"ts": now.isoformat(timespec="minutes"), "expiry": expiry,
                                                  "td_left": trading_days_between(now.date(), dt.date.fromisoformat(expiry), self.holidays),
                                                  "spot": round(spot, 2), "atm_strike": K, "atm_iv": round(iv * 100, 3),
                                                  "vix": round(vix, 3), "ratio": round(iv * 100 / vix, 4)})
                self.es["last_iv"] = {"atm_iv": iv * 100, "vix": vix, "ts": now.isoformat(timespec="minutes")}
        except Exception as e:
            log(f"iv sample failed: {e!r}", now)

    def _log_once(self, msg: str, now: dt.datetime):
        """Log a recurring no-trade reason only when it changes (one step every 15 min would spam)."""
        if self.es.get("last_reason") != msg:
            log(msg, now)
            self.es["last_reason"] = msg

    # ---------------- main step ----------------
    def step(self, now: dt.datetime | None = None) -> str:
        now = now or dt.datetime.now(IST)
        self.es["last_step"] = now.isoformat(timespec="seconds")
        try:
            sig = self.daily_signal(now.date())
            prev = self.es.get("last_signal") or {}
            self.es["last_signal"] = ({k: v for k, v in sig.items() if k != "day"}
                                      | {k: prev[k] for k in ("atm_iv", "spot") if k in prev})
        except Exception as e:
            self._log_once(f"signal unavailable: {e!r}", now)
        try:
            res = self._step(now)
            if self.training and res not in ("market closed", "market holiday"):
                try:
                    d = self._data_step(now)
                    if d:
                        res = f"{res} || training: {d}"
                except Exception as e:
                    log(f"training step error: {e!r}", now)
            return res
        finally:
            self._persist()

    def _step(self, now: dt.datetime) -> str:
        if now.weekday() >= 5 or not (dt.time(9, 15) <= now.time() <= dt.time(15, 30)):
            return "market closed"
        if now.date().isoformat() in self.holidays:
            return "market holiday"
        exps = sorted(self.b.expiries())
        expiry = exps[0]
        if self.book and self.book["expiry"] < now.date().isoformat():
            # expired while we were not running: settle at last known marks
            self.close_book("expired (settled at last mark)", now)
        td_left = trading_days_between(now.date(), dt.date.fromisoformat(expiry), self.holidays)
        self.g.new_day(now.date(), is_new_cycle=(self.book is None and td_left >= 3))
        self._sample_iv(now, expiry)

        if self.book:
            pnl = self.mtm()
            acts = self.g.mark(self.book["equity_at_entry"] + pnl)
            if "FLATTEN_ALL" in acts:
                self.close_book("governor:" + ",".join(acts), now); return "flattened by governor"
            if pnl >= self.book["tp_rs"]:
                self.close_book("target", now); return "target hit"
            if pnl <= -self.book["stop_rs"]:
                self.close_book("stop", now); return "stop hit"
            if now.date().isoformat() == self.book["expiry"] and now.strftime("%H:%M") >= self.g.L["orders"]["flatten_on_expiry_by"]:
                self.close_book("expiry-flatten", now, only_near=0.01); return "expiry flatten"
            return f"holding, MTM {pnl:,.0f}"

        if self.es.get("pending"):
            p = self.es["pending"]
            age = (now - dt.datetime.fromisoformat(p["created"])).total_seconds() / 60
            if age <= PROPOSAL_TTL_MIN:
                return f"waiting for approval of {p['id']}"
            log(f"proposal {p['id']} expired unapproved", now)
            self.es["pending"] = None
        if self.es.get("paused"):
            return "paused"
        if self.es.get("entered_cycle") == expiry:
            return f"already decided for the {expiry} expiry; next entries after it"
        if not (3 <= td_left <= 6):
            return (f"no entry: {td_left} trading day(s) left to the {expiry} expiry; "
                    "new weekly positions open only when 3-6 trading days remain")
        b, why = self._build(now, expiry)
        if b is None:
            self._log_once(f"no trade: {why}", now)
            if "size 0" in why:
                self.es["entered_cycle"] = expiry
            return why
        dec = self.g.check_entry(b["legs"], now, expiry)
        if not dec.ok:
            self._log_once(f"GOVERNOR BLOCKED: {'; '.join(dec.reasons)}", now)
            return "governor blocked: " + "; ".join(dec.reasons)
        prop = self._proposal_dict(b, dec, now, expiry)
        log(f"PROPOSAL {prop['id']} {prop['strategy']} x{prop['lots']} credit Rs {prop['credit']:,.0f} "
            f"worst case Rs {prop['max_loss']:,.0f}", now)
        if self.approval == "manual":
            self.es["pending"] = prop
            return f"proposal {prop['id']} awaiting approval"
        return "entered" if self._open(b, dec, now, expiry) else "execution failed"

    # ---------------- training book (daily paper trades for data) ----------------
    def _data_expiry(self, now: dt.datetime) -> str | None:
        for e in sorted(self.b.expiries()):
            if trading_days_between(now.date(), dt.date.fromisoformat(e), self.holidays) >= 2:
                return e
        return None

    def _data_marks(self, pos) -> list[dict]:
        ch = self.b.chain(pos["expiry"])
        out = []
        for f in pos["fills"]:
            L = f["leg"]
            r = ch[(ch.strike == L["strike"]) & (ch.cp == L["cp"])]
            r = r.iloc[0] if not r.empty else None
            liq = (r.bid if L["side"] == "BUY" else r.ask) if r is not None else float("nan")
            sgn = 1 if L["side"] == "BUY" else -1
            out.append({"cp": L["cp"], "strike": L["strike"], "side": L["side"], "lots": L["lots"],
                        "entry": f["price"], "bid": None if r is None else float(r.bid),
                        "ask": None if r is None else float(r.ask), "mid": None if r is None else float(mid(r)),
                        "pnl": sgn * (liq - f["price"]) * L["lots"] * LOT_SIZE})
        return out

    def _close_data(self, pos, reason: str, now: dt.datetime):
        try:
            pnl = float(sum(m["pnl"] for m in self._data_marks(pos)))
            self._unwind(pos["fills"], pos["expiry"])
        except Exception as e:
            pnl = pos.get("last_mtm", 0.0)
            log(f"training close used last mark ({e!r})", now)
        row = {"closed": now.isoformat(timespec="minutes"), "opened": pos["opened"], "id": pos["id"],
               "expiry": pos["expiry"], "td_left_at_entry": pos["td_left"], "strategy": pos["strategy"],
               "lots": pos["lots"], "credit": round(pos["credit"], 2), "max_loss": round(pos["max_loss"], 2),
               "pnl": round(pnl, 2), "reason": reason, "gate_open": pos["gate_open"], "gate_reason": pos["gate_reason"],
               "vix": pos["signal"].get("vix"), "rv_fcst": pos["signal"].get("rv_fcst"), "atm_iv": pos["atm_iv"],
               "spot_entry": pos["spot"], "mode": "paper-training"}
        _append_csv(P("data_journal.csv"), row)
        self.data_book = [p for p in self.data_book if p["id"] != pos["id"]]
        log(f"TRAINING CLOSED {pos['id']} ({reason}) pnl {pnl:,.0f}", now)

    def _data_proposal(self, now: dt.datetime):
        expiry = self._data_expiry(now)
        if not expiry:
            return None, "no suitable expiry"
        sig = self.es.get("last_signal") or {}
        chain, spot = self.b.chain(expiry), self.b.spot()
        iv, t, F, _ = self.atm_iv(chain, spot, expiry, now)
        if not iv == iv:
            return None, "no ATM IV from chain"
        # gates are RECORDED, not enforced: force the plan, then label what the gates said
        plan = self.strat.plan(spot, t, iv, rv_fcst=-1e9, stress=False)
        gate_open = (not sig.get("stress")) and (iv * 100 >= sig.get("rv_fcst", 0))
        gate_reason = ("stress regime" if sig.get("stress") else
                       f"no premium (IV {iv * 100:.1f} < RV fcst {sig.get('rv_fcst', float('nan')):.1f})"
                       if iv * 100 < sig.get("rv_fcst", 0) else "gates open")
        strikes = sorted(set(chain.strike))
        legs = [self._leg(chain, l.cp, min(strikes, key=lambda k: abs(k - l.K)), expiry,
                          "BUY" if l.qty > 0 else "SELL", 1) for l in plan.legs]
        dec = self.g.check_structure(legs, now)
        if not dec.ok:
            return None, "; ".join(dec.reasons)
        credit = sum((1 if l.side == "SELL" else -1) * l.limit_price for l in legs) * LOT_SIZE
        pid = "T" + hashlib.sha1(f"{now.isoformat()}{expiry}".encode()).hexdigest()[:5]
        return {"id": pid, "kind": "training", "created": now.isoformat(timespec="minutes"),
                "valid_until": f"{now.date().isoformat()}T{self.g.L['orders']['entry_window'][1]}",
                "expiry": expiry, "td_left": trading_days_between(now.date(), dt.date.fromisoformat(expiry), self.holidays),
                "strategy": plan.name, "lots": 1, "spot": spot, "atm_iv": iv, "credit": credit,
                "max_loss": dec.max_loss_rs, "take_profit": plan.take_profit, "stop": plan.stop,
                "gate_open": gate_open, "gate_reason": gate_reason, "signal": dict(sig),
                "legs": [{"cp": l.cp, "strike": l.strike, "side": l.side, "lots": l.lots, "limit": l.limit_price,
                          "bid": l.bid, "ask": l.ask} for l in legs]}, None

    def _approve_data(self, pid: str, now: dt.datetime) -> str:
        p = self.es.get("data_pending")
        if not p or p["id"] != pid:
            return "no matching training proposal"
        if now.date().isoformat() != p["created"][:10]:
            self.es["data_pending"] = None
            return "training proposal expired (made on an earlier day)"
        legs = []
        chain = self.b.chain(p["expiry"])
        for l in p["legs"]:
            legs.append(self._leg(chain, l["cp"], l["strike"], p["expiry"], l["side"], 1))
        dec = self.g.check_structure(legs, now)
        if not dec.ok:
            self.es["data_pending"] = None
            return "blocked at approval: " + "; ".join(dec.reasons)
        fills = self._execute(legs, p["expiry"])
        self.es["data_pending"] = None
        self.es["data_last_day"] = now.date().isoformat()
        if not fills:
            return "execution failed; legs unwound"
        credit = sum((1 if f["leg"]["side"] == "SELL" else -1) * f["price"] for f in fills) * LOT_SIZE
        ml = self.g.basket_max_loss([OrderLeg(**{**f["leg"], "limit_price": f["price"]}) for f in fills])
        pos = {k: p[k] for k in ("id", "expiry", "td_left", "strategy", "lots", "spot", "atm_iv", "gate_open",
                                 "gate_reason", "signal")}
        pos.update({"opened": now.isoformat(timespec="minutes"), "fills": fills, "credit": credit, "max_loss": ml,
                    "tp_rs": p["take_profit"] * max(credit, 0.0), "last_mtm": 0.0})
        self.data_book.append(pos)
        log(f"TRAINING OPENED {pos['id']} {pos['strategy']} exp {pos['expiry']} credit Rs {credit:,.0f} "
            f"worst case Rs {ml:,.0f} [{pos['gate_reason']}]", now)
        return f"training trade {pid} opened"

    def _data_step(self, now: dt.datetime) -> str:
        msgs = []
        today = now.date().isoformat()
        for pos in list(self.data_book):
            if pos["expiry"] < today:
                self._close_data(pos, "expired (settled at last mark)", now); continue
            try:
                pos["last_mtm"] = float(sum(m["pnl"] for m in self._data_marks(pos)))
            except Exception as e:
                log(f"training mark failed {pos['id']}: {e!r}", now); continue
            if pos["tp_rs"] > 0 and pos["last_mtm"] >= pos["tp_rs"]:
                self._close_data(pos, "target", now)
            elif pos["expiry"] == today and now.strftime("%H:%M") >= self.g.L["orders"]["flatten_on_expiry_by"]:
                self._close_data(pos, "expiry close", now)
        if self.data_book:
            msgs.append(f"{len(self.data_book)} open, MTM {sum(p['last_mtm'] for p in self.data_book):,.0f}")
        p = self.es.get("data_pending")
        if p and now.isoformat(timespec="minutes") > p["valid_until"]:
            log(f"training proposal {p['id']} expired unapproved", now)
            self.es["data_pending"] = None
            self.es["data_last_day"] = today
        if self.es.get("data_pending"):
            msgs.append(f"proposal {self.es['data_pending']['id']} awaiting approval")
        elif self.es.get("data_last_day") != today and not self.es.get("paused"):
            prop, why = self._data_proposal(now)
            if prop is None:
                self._log_once(f"training: no proposal ({why})", now)
            else:
                log(f"TRAINING PROPOSAL {prop['id']} {prop['strategy']} exp {prop['expiry']} credit Rs {prop['credit']:,.0f} "
                    f"worst case Rs {prop['max_loss']:,.0f} [{prop['gate_reason']}]", now)
                if self.approval == "manual":
                    self.es["data_pending"] = prop
                    msgs.append(f"proposal {prop['id']} awaiting approval")
                else:
                    self.es["data_pending"] = prop
                    msgs.append(self._approve_data(prop["id"], now))
        return "; ".join(msgs)

    # ---------------- dashboard export ----------------
    def export(self, out_dir: Path, now: dt.datetime | None = None, note: str = "", extra: dict | None = None):
        from .dashboard import export
        export(self, Path(out_dir), now or dt.datetime.now(IST), note, extra=extra)
