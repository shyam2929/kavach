"""KAVACH-Crude: MCX Crude Oil Mini options, two paper books, run through the full MCX session.

Main book    gated monthly iron condor (short ~0.6 sigma, wings ~1.5 sigma), 1 Mini lot, entered
             ~12-26 trading days before options expiry, only when the stress gate is open.
Training     one 1-lot condor proposed every MCX day on the nearest options expiry with >= 5 trading
             days left; gates recorded, never enforced. Notional, never counts toward live.
Both         wings bought first; take profit at 60% of credit; everything closed from 17:00 IST when
             2 or fewer trading days remain before options expiry (ITM options would devolve
             into futures); positions past expiry are settled at the last mark.

Quotes: Upstox (instrument master + full market quotes, Analytics Token) or a synthetic chain from
WTI (CL=F) x USD/INR with OVX as implied vol. Session and holidays from Upstox's public MCX calendar.
"""
from __future__ import annotations

import csv
import datetime as dt
import gzip
import hashlib
import io
import json
import math
import os
import time
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from .governor import RiskGovernor, OrderLeg, STATE
from .pricing import black

IST = ZoneInfo("Asia/Kolkata")
NY = ZoneInfo("America/New_York")
LOT = 10                  # Crude Oil Mini, barrels
STEP = 50.0               # strike interval, Rs
TICK = 0.10               # Rs; Upstox lists 0.05, 0.10 is a safe multiple
MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/MCX.json.gz"
DATA_DIR = Path(os.environ.get("KAVACH_DATA", Path(__file__).resolve().parent.parent / "data"))


def P(name: str) -> Path:
    return STATE / name


def log(msg: str, now: dt.datetime | None = None):
    STATE.mkdir(parents=True, exist_ok=True)
    line = f"{(now or dt.datetime.now(IST)):%Y-%m-%d %H:%M:%S} [crude] {msg}"
    print(line, flush=True)
    with P("engine.log").open("a") as f:
        f.write(line + "\n")


def _load(name, default):
    p = P(name)
    return json.loads(p.read_text()) if p.exists() else default


def _save(name, obj):
    STATE.mkdir(parents=True, exist_ok=True)
    P(name).write_text(json.dumps(obj, indent=1, default=str))


def _append_csv(path: Path, row: dict):
    new = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if new:
            w.writeheader()
        w.writerow(row)


# ---------------- calendar ----------------
class MCXCalendar:
    """MCX session from Upstox's public holiday list. A fully closed day has MCX in closed_exchanges;
    a special session (e.g. Diwali evening) lists MCX in open_exchanges with start/end times."""

    def __init__(self, rows: list[dict] | None = None):
        if rows is None:
            try:
                req = urllib.request.Request("https://api.upstox.com/v2/market/holidays",
                                             headers={"Accept": "application/json", "User-Agent": "kavach"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    rows = json.load(r).get("data", [])
            except Exception:
                rows = []
        self.closed, self.special = set(), {}
        for r in rows:
            if "MCX" in (r.get("closed_exchanges") or []):
                self.closed.add(r["date"])
            for o in r.get("open_exchanges") or []:
                if o.get("exchange") == "MCX":
                    s = dt.datetime.fromtimestamp(o["start_time"] / 1000, IST).time()
                    e = dt.datetime.fromtimestamp(o["end_time"] / 1000, IST).time()
                    self.special[r["date"]] = (s, e)

    def session(self, d: dt.date) -> tuple[dt.time, dt.time] | None:
        iso = d.isoformat()
        if iso in self.special:
            return self.special[iso]
        if d.weekday() >= 5 or iso in self.closed:
            return None
        noon_ny = dt.datetime.combine(d, dt.time(12), NY)
        close = dt.time(23, 55) if noon_ny.dst() else dt.time(23, 30)
        return dt.time(9, 0), close

    def is_open(self, now: dt.datetime) -> bool:
        s = self.session(now.date())
        return bool(s) and s[0] <= now.time() <= s[1]

    def trading_days_between(self, a: dt.date, b: dt.date) -> int:
        n, d = 0, a
        while d < b:
            d += dt.timedelta(days=1)
            if self.session(d):
                n += 1
        return n


# ---------------- quotes ----------------
def _yahoo_last(symbol: str) -> float:
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}?range=1d&interval=1m"
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=20) as r:
        j = json.load(r)["chart"]["result"][0]
    closes = [c for c in j["indicators"]["quote"][0]["close"] if c is not None]
    return float(closes[-1])


def option_expiries_from_master(master: list[dict], today: dt.date) -> list[str]:
    exps = {dt.datetime.fromtimestamp(x["expiry"] / 1000, IST).date().isoformat()
            for x in master if x.get("underlying_symbol") == "CRUDEOILM" and x.get("instrument_type") in ("CE", "PE")}
    return sorted(e for e in exps if e >= today.isoformat())


def load_master() -> list[dict]:
    with urllib.request.urlopen(urllib.request.Request(MASTER_URL, headers={"User-Agent": "kavach"}), timeout=60) as r:
        data = gzip.decompress(r.read())
    return [x for x in json.loads(data) if x.get("underlying_symbol") == "CRUDEOILM"]


class SyntheticCrudeQuotes:
    """Model chain: futures = WTI x USD/INR, IV = OVX with a put skew, spreads ~1.5%."""

    def __init__(self, fut_fn, ovx_fn, expiries_fn, today_fn=lambda: dt.datetime.now(IST).date()):
        self.fut_fn, self.ovx_fn, self.expiries_fn, self.today = fut_fn, ovx_fn, expiries_fn, today_fn
        self.source = "synthetic"

    def spot(self) -> float:
        return float(self.fut_fn())

    def expiries(self) -> list[str]:
        return self.expiries_fn()

    def chain(self, expiry: str) -> pd.DataFrame:
        F, iv = self.spot(), self.ovx_fn() / 100
        days = max((dt.date.fromisoformat(expiry) - self.today()).days, 0)
        t = max(days / 365, 0.5 / 365)
        atm = round(F / STEP) * STEP
        rows = []
        for k in range(-60, 61):
            K = atm + k * STEP
            m = max(-4, min(4, math.log(K / F) / (iv * math.sqrt(t))))
            v = max(0.08, iv - 0.02 * m + 0.006 * m * m)
            for cp in ("C", "P"):
                p = black(F, K, t, v, cp, r=0.065)
                half = max(1.0, 0.015 * p)
                rows.append({"strike": K, "cp": cp, "bid": max(0.1, p - half), "ask": p + half, "ltp": p, "iv": v,
                             "security_id": f"SYN-CRUDEOILM-{expiry}-{int(K)}-{cp}"})
        return pd.DataFrame(rows)


class UpstoxCrudeQuotes:
    """Real MCX Crude Oil Mini chain: instrument master + /v2/market-quote/quotes (Analytics Token works)."""

    def __init__(self, token: str | None = None, master: list[dict] | None = None, http=None):
        from .brokers.upstox import Http, _ok, API
        self._ok, self.API = _ok, API
        self.http = http or Http(token or os.environ.get("UPSTOX_ANALYTICS_TOKEN") or os.environ["UPSTOX_ACCESS_TOKEN"])
        self.master = master if master is not None else load_master()
        self.source = "upstox"
        self._cache: dict[str, tuple[float, pd.DataFrame]] = {}

    def expiries(self) -> list[str]:
        return option_expiries_from_master(self.master, dt.datetime.now(IST).date())

    def _quotes(self, keys: list[str]) -> dict:
        out = {}
        for i in range(0, len(keys), 450):
            data = self._ok(self.http("GET", f"{self.API}/v2/market-quote/quotes", {"instrument_key": ",".join(keys[i:i + 450])}))
            for v in (data or {}).values():
                out[v.get("instrument_token")] = v
        return out

    def _fut_key(self, expiry: str) -> str:
        opts = [x for x in self.master if x.get("instrument_type") in ("CE", "PE")
                and dt.datetime.fromtimestamp(x["expiry"] / 1000, IST).date().isoformat() == expiry]
        return opts[0]["underlying_key"]

    def spot(self, expiry: str | None = None) -> float:
        expiry = expiry or self.expiries()[0]
        k = self._fut_key(expiry)
        q = self._quotes([k]).get(k, {})
        return float(q.get("last_price") or q.get("ohlc", {}).get("close") or float("nan"))

    def chain(self, expiry: str) -> pd.DataFrame:
        now = time.time()
        if expiry in self._cache and now - self._cache[expiry][0] < 20:
            return self._cache[expiry][1]
        F = self.spot(expiry)
        opts = [x for x in self.master if x.get("instrument_type") in ("CE", "PE")
                and dt.datetime.fromtimestamp(x["expiry"] / 1000, IST).date().isoformat() == expiry
                and abs(x["strike_price"] / F - 1) < 0.35]
        q = self._quotes([x["instrument_key"] for x in opts])
        rows = []
        for x in opts:
            v = q.get(x["instrument_key"], {})
            depth = v.get("depth") or {}
            bid = float((depth.get("buy") or [{}])[0].get("price") or 0)
            ask = float((depth.get("sell") or [{}])[0].get("price") or 0)
            rows.append({"strike": float(x["strike_price"]), "cp": "C" if x["instrument_type"] == "CE" else "P",
                         "bid": bid, "ask": ask, "ltp": float(v.get("last_price") or 0), "iv": float("nan"),
                         "security_id": x["instrument_key"]})
        df = pd.DataFrame(rows)
        self._cache[expiry] = (now, df)
        return df


# ---------------- signal ----------------
def refresh_crude_daily():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    frames = {}
    for sym, col in (("CL%3DF", "wti"), ("%5EOVX", "ovx"), ("USDINR%3DX", "inr")):
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{sym}?range=2y&interval=1d"
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=30) as r:
            j = json.load(r)["chart"]["result"][0]
        idx = pd.to_datetime(pd.to_datetime(j["timestamp"], unit="s", utc=True).tz_convert(NY).date)
        s = pd.Series(j["indicators"]["quote"][0]["close"], index=idx, name=col).dropna()
        frames[col] = s[~s.index.duplicated(keep="last")]
    df = pd.concat(frames.values(), axis=1).ffill().dropna()
    df = df[(df.wti > 5) & (df.index.weekday < 5)]        # FX trades Sundays; drop those rows
    df["mcx"] = df.wti * df.inr
    df.to_csv(DATA_DIR / "crude_daily.csv")
    return df


def crude_signal(df: pd.DataFrame) -> dict:
    o5 = math.log(df.ovx.iloc[-1] / df.ovx.iloc[-6])
    r5 = math.log(df.mcx.iloc[-1] / df.mcx.iloc[-6])
    dd20 = df.mcx.iloc[-1] / df.mcx.iloc[-21:].max() - 1
    ovx = float(df.ovx.iloc[-1])
    reasons = []
    if o5 > 0.15: reasons.append(f"OVX +{o5:.0%} in 5 days")
    if r5 < -0.05: reasons.append(f"crude {r5:.1%} in 5 days")
    if dd20 < -0.10: reasons.append(f"crude {dd20:.1%} below 20-day high")
    if ovx > 70: reasons.append(f"OVX {ovx:.0f} > 70")
    return {"asof": str(df.index[-1].date()), "ovx": ovx, "ovx_ch5": o5, "ret5": r5, "dd20": dd20,
            "wti": float(df.wti.iloc[-1]), "inr": float(df.inr.iloc[-1]), "mcx": float(df.mcx.iloc[-1]),
            "stress": bool(reasons), "stress_reasons": reasons}


# ---------------- engine ----------------
BOOKS = {
    "main": {"prefix": "C", "short_z": 0.6, "wing_z": 1.5, "min_td": 12, "max_td": 30, "gated": True,
             "journal": "crude_journal.csv"},
    "training": {"prefix": "CT", "short_z": 0.6, "wing_z": 1.5, "min_td": 5, "max_td": 60, "gated": False,
                 "journal": "crude_training_journal.csv"},
}
TAKE_PROFIT = 0.6
WORST_CASE_CAP = 0.10      # main book: worst case <= 10% of equity
COMBINED_CAP = 0.25        # main crude + Nifty main book worst cases <= 25% of equity


class CrudeEngine:
    def __init__(self, broker, governor: RiskGovernor, approval: str = "manual", calendar: MCXCalendar | None = None,
                 signal_fn=None, nifty_open_risk_fn=lambda: 0.0):
        self.b, self.g, self.approval = broker, governor, approval
        self.cal = calendar or MCXCalendar()
        self.signal_fn = signal_fn
        self.nifty_open_risk = nifty_open_risk_fn
        self.st = _load("crude_state.json", {"pending": {}, "positions": [], "last_day": {}, "paused": False,
                                             "signal": None, "last_step": None, "last_reason": {}, "paper_trades": 0})
        self.quote_source = getattr(broker.q, "source", "unknown") if hasattr(broker, "q") else "unknown"

    def persist(self):
        _save("crude_state.json", self.st)

    # -- pricing helpers
    def _t(self, now: dt.datetime, expiry: str) -> float:
        end = dt.datetime.combine(dt.date.fromisoformat(expiry), dt.time(23, 30), IST)
        return max((end - now).total_seconds() / (365 * 86400), 0.5 / 365)

    def _atm_iv(self, chain, F, t):
        from .pricing import implied_vol
        from .brokers import mid
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
        return float(np.mean(ivs)) if ivs else float("nan")

    def _leg(self, chain, cp, K, expiry, side, aggress=0.3) -> OrderLeg:
        from .brokers import mid
        r = chain[(chain.strike == K) & (chain.cp == cp)]
        if r.empty:
            raise ValueError(f"strike {K}{cp} not in chain")
        r = r.iloc[0]
        m = mid(r)
        px = m + aggress * (r.ask - m) if side == "BUY" else m - aggress * (m - r.bid)
        steps = max(TICK, px) / TICK
        px = (math.ceil(steps - 1e-9) if side == "BUY" else math.floor(steps + 1e-9)) * TICK
        return OrderLeg(cp, float(K), expiry, side, 1, round(max(TICK, px), 2), float(r.bid), float(r.ask),
                        str(r.security_id), underlying="CRUDEOILM", lot_size=LOT)

    def _execute(self, legs, expiry):
        done = []
        for leg in sorted(legs, key=lambda l: 0 if l.side == "BUY" else 1):
            fill = None
            for aggress in (0.3, 0.7, 1.0, 1.5):   # 1.5 crosses the touch: last try fills
                lg = self._leg(self.b.chain(expiry), leg.cp, leg.strike, expiry, leg.side, aggress)
                self.g.wait_rate()
                fill = self.b.place(lg)
                if fill["status"] == "FILLED":
                    break
            if not fill or fill["status"] != "FILLED":
                log(f"could not fill {leg.side} {leg.cp}{leg.strike:.0f}; unwinding {len(done)} legs")
                self._unwind(done, expiry)
                return None
            done.append({"leg": vars(lg), "price": fill["avg_price"]})
        return done

    def _unwind(self, filled, expiry):
        for f in filled:
            L = f["leg"]
            side = "SELL" if L["side"] == "BUY" else "BUY"
            for aggress in (0.7, 1.0, 1.5, 2.0):
                lg = self._leg(self.b.chain(expiry), L["cp"], L["strike"], expiry, side, aggress)
                self.g.wait_rate()
                if self.b.place(lg)["status"] == "FILLED":
                    break
            else:
                log(f"!!! FAILED TO UNWIND {L}. KILL SWITCH."); self.b.kill_switch()

    def marks(self, pos) -> list[dict]:
        from .brokers import mid
        ch = self.b.chain(pos["expiry"])
        out = []
        for f in pos["fills"]:
            L = f["leg"]
            r = ch[(ch.strike == L["strike"]) & (ch.cp == L["cp"])]
            r = r.iloc[0] if not r.empty else None
            liq = (r.bid if L["side"] == "BUY" else r.ask) if r is not None else float("nan")
            sgn = 1 if L["side"] == "BUY" else -1
            out.append({"cp": L["cp"], "strike": L["strike"], "side": L["side"], "lots": 1, "entry": f["price"],
                        "bid": None if r is None else float(r.bid), "ask": None if r is None else float(r.ask),
                        "mid": None if r is None else float(mid(r)), "pnl": sgn * (liq - f["price"]) * LOT})
        return out

    # -- proposals
    def _expiry_for(self, book: str, now: dt.datetime) -> tuple[str | None, int]:
        cfg = BOOKS[book]
        for e in sorted(self.b.expiries()):
            td = self.cal.trading_days_between(now.date(), dt.date.fromisoformat(e))
            if cfg["min_td"] <= td <= cfg["max_td"]:
                return e, td
            if td > cfg["max_td"]:
                break
        return None, 0

    def _propose(self, book: str, now: dt.datetime):
        cfg = BOOKS[book]
        expiry, td = self._expiry_for(book, now)
        if not expiry:
            return None, f"no options expiry {cfg['min_td']}-{cfg['max_td']} trading days out"
        chain, F = self.b.chain(expiry), self.b.spot()
        t = self._t(now, expiry)
        iv = self._atm_iv(chain, F, t)
        if not iv == iv:
            return None, "no ATM IV from chain"
        sig = self.st.get("signal") or {}
        gate_open = not sig.get("stress", False)
        gate_reason = "gates open" if gate_open else "stress: " + "; ".join(sig.get("stress_reasons", []))
        if cfg["gated"] and not gate_open:
            return None, gate_reason
        sd = F * iv * math.sqrt(t)
        strikes = sorted(set(chain.strike))
        near = lambda x: min(strikes, key=lambda k: abs(k - x))
        kp, kc = near(F - cfg["short_z"] * sd), near(F + cfg["short_z"] * sd)
        wp = min(near(F - cfg["wing_z"] * sd), kp - 2 * STEP)
        wc = max(near(F + cfg["wing_z"] * sd), kc + 2 * STEP)
        legs = [self._leg(chain, "P", wp, expiry, "BUY"), self._leg(chain, "C", wc, expiry, "BUY"),
                self._leg(chain, "P", kp, expiry, "SELL"), self._leg(chain, "C", kc, expiry, "SELL")]
        why = [p for p in (self.g._price_ok(l) for l in legs) if p]
        d = self.g.defined_risk(legs)
        if d:
            why.append(d)
        ml = self.g.basket_max_loss(legs)
        if book == "main":
            eq = self.g.S.equity
            if ml > WORST_CASE_CAP * eq:
                why.append(f"worst case Rs {ml:,.0f} > {WORST_CASE_CAP:.0%} of equity")
            if ml + self.nifty_open_risk() > COMBINED_CAP * eq:
                why.append("combined crude + Nifty worst case > 25% of equity")
            if self.g.S.halted:
                why.append("account halted")
        if why:
            return None, "; ".join(why)
        credit = sum((1 if l.side == "SELL" else -1) * l.limit_price for l in legs) * LOT
        pid = cfg["prefix"] + hashlib.sha1(f"{book}{now.isoformat()}".encode()).hexdigest()[:5]
        return {"id": pid, "book": book, "created": now.isoformat(timespec="minutes"),
                "valid_until": f"{now.date().isoformat()}T22:00", "expiry": expiry, "td_left": td,
                "strategy": f"crude condor {cfg['short_z']}/{cfg['wing_z']} sigma", "lots": 1, "spot": F, "atm_iv": iv,
                "credit": credit, "max_loss": ml, "gate_open": gate_open, "gate_reason": gate_reason,
                "legs": [{"cp": l.cp, "strike": l.strike, "side": l.side, "lots": 1, "limit": l.limit_price,
                          "bid": l.bid, "ask": l.ask} for l in legs]}, None

    def approve(self, pid: str, now: dt.datetime) -> str:
        book = next((b for b, p in self.st["pending"].items() if p and p["id"] == pid), None)
        if not book:
            return "no matching crude proposal"
        p = self.st["pending"][book]
        if now.isoformat(timespec="minutes") > p["valid_until"]:
            self.st["pending"][book] = None
            return "crude proposal expired"
        chain = self.b.chain(p["expiry"])
        legs = [self._leg(chain, l["cp"], l["strike"], p["expiry"], l["side"]) for l in p["legs"]]
        if self.g.defined_risk(legs):
            self.st["pending"][book] = None
            return "blocked: not defined risk"
        fills = self._execute(legs, p["expiry"])
        self.st["pending"][book] = None
        self.st["last_day"][book] = now.date().isoformat()
        if not fills:
            return "execution failed; legs unwound"
        credit = sum((1 if f["leg"]["side"] == "SELL" else -1) * f["price"] for f in fills) * LOT
        ml = self.g.basket_max_loss([OrderLeg(**{**f["leg"], "limit_price": f["price"]}) for f in fills])
        pos = {k: p[k] for k in ("id", "book", "expiry", "td_left", "strategy", "spot", "atm_iv", "gate_open", "gate_reason")}
        pos.update({"opened": now.isoformat(timespec="minutes"), "fills": fills, "credit": credit, "max_loss": ml,
                    "tp_rs": TAKE_PROFIT * max(credit, 0.0), "last_mtm": 0.0, "lots": 1})
        self.st["positions"].append(pos)
        log(f"OPENED {pid} ({book}) exp {p['expiry']} credit Rs {credit:,.0f} worst case Rs {ml:,.0f}", now)
        return f"crude {book} trade {pid} opened"

    def reject(self, pid: str, now: dt.datetime) -> str:
        for book, p in self.st["pending"].items():
            if p and p["id"] == pid:
                self.st["pending"][book] = None
                self.st["last_day"][book] = now.date().isoformat()
                return f"crude proposal {pid} skipped for today"
        return "no matching crude proposal"

    def close(self, pos, reason: str, now: dt.datetime):
        try:
            pnl = float(sum(m["pnl"] for m in self.marks(pos)))
            self._unwind(pos["fills"], pos["expiry"])
        except Exception as e:
            pnl = pos.get("last_mtm", 0.0)
            log(f"close used last mark ({e!r})", now)
        _append_csv(P(BOOKS[pos["book"]]["journal"]), {
            "closed": now.isoformat(timespec="minutes"), "opened": pos["opened"], "id": pos["id"], "book": pos["book"],
            "expiry": pos["expiry"], "td_left_at_entry": pos["td_left"], "credit": round(pos["credit"], 2),
            "max_loss": round(pos["max_loss"], 2), "pnl": round(pnl, 2), "reason": reason,
            "gate_open": pos["gate_open"], "gate_reason": pos["gate_reason"], "atm_iv": round(pos["atm_iv"], 4),
            "spot_entry": round(pos["spot"], 1), "mode": "paper"})
        if pos["book"] == "main":
            self.st["paper_trades"] += 1
        self.st["positions"] = [p for p in self.st["positions"] if p["id"] != pos["id"]]
        log(f"CLOSED {pos['id']} ({reason}) pnl {pnl:,.0f}", now)

    def command(self, name: str, arg: str, now: dt.datetime) -> str | None:
        """Handles crude commands; returns None if the command isn't for crude."""
        getattr(self.b, "refresh", lambda: None)()   # fresh quote snapshot per step/command
        if name == "approve" and arg.startswith("C"):
            return self.approve(arg, now)
        if name == "reject" and arg.startswith("C"):
            return self.reject(arg, now)
        if name == "flatten" and (not arg or arg.startswith("C")):
            n = 0
            for pos in list(self.st["positions"]):
                if not arg or arg == pos["id"]:
                    self.close(pos, "manual flatten", now); n += 1
            return f"{n} crude position(s) closed"
        if name in ("pause", "resume"):
            self.st["paused"] = name == "pause"
            return f"crude {name}d"
        return None

    def _log_once(self, key: str, msg: str, now):
        if self.st["last_reason"].get(key) != msg:
            log(msg, now)
            self.st["last_reason"][key] = msg

    def step(self, now: dt.datetime | None = None) -> str:
        getattr(self.b, "refresh", lambda: None)()   # fresh quote snapshot per step/command
        now = now or dt.datetime.now(IST)
        self.st["last_step"] = now.isoformat(timespec="seconds")
        try:
            if self.signal_fn:
                try:
                    self.st["signal"] = self.signal_fn(now.date())
                except Exception as e:
                    self._log_once("signal", f"signal unavailable: {e!r}", now)
            if not self.cal.is_open(now):
                return "MCX closed"
            msgs = []
            today = now.date().isoformat()
            for pos in list(self.st["positions"]):
                td = self.cal.trading_days_between(now.date(), dt.date.fromisoformat(pos["expiry"]))
                if pos["expiry"] < today:
                    self.close(pos, "expired (settled at last mark)", now); continue
                try:
                    pos["last_mtm"] = float(sum(m["pnl"] for m in self.marks(pos)))
                except Exception as e:
                    self._log_once("mark", f"mark failed {pos['id']}: {e!r}", now); continue
                if pos["tp_rs"] > 0 and pos["last_mtm"] >= pos["tp_rs"]:
                    self.close(pos, "target", now)
                elif td <= 2 and now.time() >= dt.time(17, 0):
                    self.close(pos, "pre-expiry close (avoid devolvement)", now)
            if self.st["positions"]:
                msgs.append(f"{len(self.st['positions'])} open, MTM {sum(p['last_mtm'] for p in self.st['positions']):,.0f}")
            for book in ("main", "training"):
                p = self.st["pending"].get(book)
                if p and now.isoformat(timespec="minutes") > p["valid_until"]:
                    log(f"{book} proposal {p['id']} lapsed", now)
                    self.st["pending"][book] = None
                    self.st["last_day"][book] = today
                if self.st["pending"].get(book):
                    msgs.append(f"{book}: {self.st['pending'][book]['id']} awaiting approval"); continue
                if self.st["paused"] or self.st["last_day"].get(book) == today:
                    continue
                if not (dt.time(10, 0) <= now.time() <= dt.time(22, 0)):
                    continue
                if now.weekday() == 2 and now.time() >= dt.time(18, 0):
                    continue          # EIA window
                if book == "main" and any(p["book"] == "main" for p in self.st["positions"]):
                    continue
                prop, why = self._propose(book, now)
                if prop is None:
                    self._log_once(book, f"{book}: no proposal ({why})", now)
                    continue
                log(f"PROPOSAL {prop['id']} ({book}) exp {prop['expiry']} credit Rs {prop['credit']:,.0f} "
                    f"worst case Rs {prop['max_loss']:,.0f} [{prop['gate_reason']}]", now)
                self.st["pending"][book] = prop
                if self.approval == "auto":
                    msgs.append(self.approve(prop["id"], now))
                else:
                    msgs.append(f"{book}: {prop['id']} awaiting approval")
            return "; ".join(msgs) or "nothing to do"
        finally:
            self.persist()

    def export(self) -> dict:
        from .dashboard import payoff_curve
        pend = {}
        for b, p in self.st["pending"].items():
            if p:
                pend[b] = dict(p) | {"payoff": payoff_curve([l | {"lots": 1} for l in p["legs"]], float(p["spot"]),
                                                             price_key="limit", lot=LOT)}
        positions = []
        for pos in self.st["positions"]:
            try:
                mk = self.marks(pos)
            except Exception:
                mk = [{"cp": f["leg"]["cp"], "strike": f["leg"]["strike"], "side": f["leg"]["side"], "lots": 1,
                       "entry": f["price"], "bid": None, "ask": None, "mid": None, "pnl": None} for f in pos["fills"]]
            m = sum(x["pnl"] for x in mk if x["pnl"] is not None)
            positions.append({k: pos[k] for k in ("id", "book", "opened", "expiry", "strategy", "lots", "credit",
                                                  "max_loss", "tp_rs", "gate_open", "gate_reason", "spot", "atm_iv")}
                             | {"legs": mk, "mtm": m, "payoff": payoff_curve(mk, float(pos["spot"]), lot=LOT)})
        journals = {}
        for b, cfg in BOOKS.items():
            p = P(cfg["journal"])
            journals[b] = list(csv.DictReader(p.open())) if p.exists() else []
        now = dt.datetime.now(IST)
        sess = self.cal.session(now.date())
        return {"signal": self.st.get("signal"), "pending": pend, "positions": positions, "journals": journals,
                "paused": self.st["paused"], "last_step": self.st["last_step"], "quotes": self.quote_source,
                "mcx_open": self.cal.is_open(now), "session": [str(sess[0])[:5], str(sess[1])[:5]] if sess else None,
                "paper_trades": self.st["paper_trades"]}
