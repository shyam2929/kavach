"""Exports engine state as JSON for the static dashboard (site/). No secrets are ever written."""
from __future__ import annotations

import csv
import datetime as dt
import json
from pathlib import Path

import numpy as np

from .pricing import LOT_SIZE
from .sizing import capital_fraction


def _csv_rows(path: Path, last: int | None = None) -> list[dict]:
    if not path.exists():
        return []
    with path.open() as f:
        rows = list(csv.DictReader(f))
    return rows[-last:] if last else rows


def payoff_curve(legs: list[dict], center: float, credit_per_unit: float | None = None, lots_key="lots",
                 price_key="entry") -> list[list[float]]:
    """Expiry P&L (Rs) over spot +-5% for legs [{cp,strike,side,lots,entry|limit}]."""
    xs = np.linspace(center * 0.95, center * 1.05, 121)
    pnl = np.zeros_like(xs)
    for l in legs:
        q = l[lots_key] * LOT_SIZE * (1 if l["side"] == "BUY" else -1)
        intr = np.maximum(xs - l["strike"], 0) if l["cp"] == "C" else np.maximum(l["strike"] - xs, 0)
        pnl += q * (intr - float(l[price_key]))
    return [[round(float(x), 1), round(float(y), 0)] for x, y in zip(xs, pnl)]


def iv_calibration(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0}
    r = np.array([float(x["ratio"]) for x in rows])
    by_td = {}
    for x in rows:
        by_td.setdefault(x["td_left"], []).append(float(x["ratio"]))
    return {"n": int(len(r)), "median_ratio": float(np.median(r)), "p25": float(np.quantile(r, .25)),
            "p75": float(np.quantile(r, .75)), "by_days_left": {k: round(float(np.median(v)), 3) for k, v in sorted(by_td.items())}}


def export(engine, out: Path, now: dt.datetime, note: str = ""):
    from .live import P
    out.mkdir(parents=True, exist_ok=True)
    g, es, S = engine.g, engine.es, engine.g.S

    marks, mtm, position = [], 0.0, None
    if engine.book:
        try:
            marks = engine.leg_marks()
            mtm = float(sum(m["pnl"] for m in marks))
        except Exception as e:  # quotes unavailable (e.g. after hours)
            marks = [{"cp": f["leg"]["cp"], "strike": f["leg"]["strike"], "side": f["leg"]["side"],
                      "lots": f["leg"]["lots"], "entry": f["price"], "bid": None, "ask": None, "mid": None,
                      "pnl": None} for f in engine.book["fills"]]
            note = (note + f" | quotes unavailable: {e}").strip(" |")
        b = engine.book
        center = es.get("last_signal", {}) and es["last_signal"].get("spot") or np.mean([m["strike"] for m in marks])
        position = {k: b[k] for k in ("opened", "expiry", "strategy", "lots", "credit", "max_loss", "tp_rs", "stop_rs",
                                      "equity_at_entry")}
        position.update({"legs": marks, "mtm": mtm, "payoff": payoff_curve(marks, float(center))})

    pending = es.get("pending")
    if pending:
        pending = dict(pending)
        pending["payoff"] = payoff_curve(pending["legs"], float(pending["spot"]), price_key="limit")

    equity_mark = (engine.book["equity_at_entry"] + mtm) if engine.book else S.equity
    series = []
    sp = P("equity_series.json")
    if sp.exists():
        series = json.loads(sp.read_text())
    series.append([now.isoformat(timespec="minutes"), round(equity_mark, 2)])
    series = series[-5000:]
    sp.write_text(json.dumps(series))

    dd = equity_mark / max(S.peak, equity_mark) - 1
    status = {
        "updated": now.isoformat(timespec="seconds"), "note": note,
        "mode": "paper" if engine.b.paper else "LIVE", "quotes": getattr(engine, "quote_source", "unknown"),
        "strategy": engine.strat.name, "approval": engine.approval,
        "market_open": now.weekday() < 5 and dt.time(9, 15) <= now.time() <= dt.time(15, 30),
        "paused": bool(es.get("paused")), "halted": S.halted, "halt_reason": S.halt_reason,
        "equity": round(equity_mark, 2), "realized_equity": round(S.equity, 2), "peak": round(S.peak, 2),
        "drawdown": round(dd, 4), "capital_base": g.L["account"]["capital_base"],
        "cap_frac": capital_fraction(S.trade_returns, dd), "trades": len(S.trade_returns),
        "paper_trades": S.paper_trades, "paper_needed": g.L["mode"]["paper_trades_required_for_live"],
        "consecutive_losses": S.consecutive_losses, "cooldown_until": S.cooldown_until_expiry,
        "entries_today": S.entries_today, "signal": es.get("last_signal"), "last_iv": es.get("last_iv"),
        "last_step": es.get("last_step"), "entered_cycle": es.get("entered_cycle"),
        "position": position, "pending": pending,
        "iv_calibration": iv_calibration(_csv_rows(P("iv_samples.csv"))),
        "limits": {"risk_per_trade": g.L["per_trade"]["risk_per_trade"], "max_loss_pct": g.L["per_trade"]["max_loss_pct"],
                   "max_lots": g.L["per_trade"]["max_lots"], "daily_max_loss_pct": g.L["daily"]["max_loss_pct"],
                   "halt_at": g.L["drawdown"]["halt_at"], "entry_window": g.L["orders"]["entry_window"],
                   "max_consecutive_losses": g.L["behaviour"]["max_consecutive_losses"]},
    }
    (out / "status.json").write_text(json.dumps(status, default=str))
    (out / "journal.json").write_text(json.dumps(_csv_rows(P("journal.csv")), default=str))
    (out / "equity.json").write_text(json.dumps(series))
    (out / "iv.json").write_text(json.dumps(_csv_rows(P("iv_samples.csv"), 1000)))
    lp = P("engine.log")
    lines = lp.read_text().splitlines()[-200:] if lp.exists() else []
    (out / "log.json").write_text(json.dumps(lines))
