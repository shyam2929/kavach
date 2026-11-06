"""Upstox adapter (REST, no SDK dependency).

Two credentials, deliberately separate:
  UPSTOX_ANALYTICS_TOKEN  read-only, 1-year validity, NO static IP needed for market data
                          (option chain, quotes). Safe to give to the GitHub Actions paper runner.
  UPSTOX_ACCESS_TOKEN     trading token from the daily OAuth login (valid till ~3:30 AM next day).
                          Orders must originate from the static IP registered on your Upstox app
                          (SEBI/NSE retail-algo framework, enforced since 1 Apr 2026).
Optional: UPSTOX_ALGO_NAME -> sent as X-Algo-Name (only if your app has an approved algo name).

Endpoints (Upstox docs, Nov 2026):
  GET  https://api.upstox.com/v2/option/contract?instrument_key=NSE_INDEX|Nifty 50
  GET  https://api.upstox.com/v2/option/chain?instrument_key=...&expiry_date=YYYY-MM-DD
  POST https://api-hft.upstox.com/v3/order/place
  GET  https://api.upstox.com/v2/order/details?order_id=
  DEL  https://api-hft.upstox.com/v3/order/cancel?order_id=
  POST https://api.upstox.com/v2/order/positions/exit?segment=NSE_FO
  POST https://api.upstox.com/v2/user/kill-switch   [{"segment":"NSE_FO","action":"DISABLE"}]
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import pandas as pd

from . import Broker
from ..governor import OrderLeg
from ..pricing import LOT_SIZE

API = "https://api.upstox.com"
HFT = "https://api-hft.upstox.com"
NIFTY_KEY = "NSE_INDEX|Nifty 50"


class UpstoxError(RuntimeError):
    pass


class Http:
    """Tiny JSON client; swapped for a fake in tests."""

    def __init__(self, token: str, algo_name: str | None = None):
        self.token, self.algo = token, algo_name

    def __call__(self, method: str, url: str, params: dict | None = None, body=None) -> dict:
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self.token}",
                   "User-Agent": "kavach/0.2"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        if self.algo:
            headers["X-Algo-Name"] = self.algo
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            raise UpstoxError(f"{method} {url} -> {e.code}: {e.read()[:400]!r}") from None


def _ok(resp: dict):
    if not isinstance(resp, dict) or resp.get("status") not in ("success", "partial_success"):
        raise UpstoxError(f"Upstox error: {resp}")
    return resp.get("data")


class UpstoxMarketData:
    """Option-chain quotes. Works with the analytics token (no static IP)."""

    def __init__(self, token: str | None = None, http: Http | None = None):
        tok = token or os.environ.get("UPSTOX_ANALYTICS_TOKEN") or os.environ.get("UPSTOX_ACCESS_TOKEN")
        if not tok and http is None:
            raise UpstoxError("Set UPSTOX_ANALYTICS_TOKEN (read-only market data) or UPSTOX_ACCESS_TOKEN")
        self.http = http or Http(tok)
        self._cache: dict[str, tuple[float, pd.DataFrame, float]] = {}
        self._exp: tuple[float, list[str]] | None = None

    def expiries(self) -> list[str]:
        if self._exp and time.time() - self._exp[0] < 3600:
            return self._exp[1]
        data = _ok(self.http("GET", f"{API}/v2/option/contract", {"instrument_key": NIFTY_KEY}))
        exps = sorted({str(c["expiry"])[:10] for c in data if c.get("expiry")})
        today = time.strftime("%Y-%m-%d")
        exps = [e for e in exps if e >= today]
        self._exp = (time.time(), exps)
        return exps

    def _fetch(self, expiry: str):
        now = time.time()
        if expiry in self._cache and now - self._cache[expiry][0] < 5:
            return self._cache[expiry]
        data = _ok(self.http("GET", f"{API}/v2/option/chain", {"instrument_key": NIFTY_KEY, "expiry_date": expiry}))
        rows, spot = [], float("nan")
        for r in data or []:
            spot = float(r.get("underlying_spot_price", spot))
            K = float(r["strike_price"])
            for side, cp in (("call_options", "C"), ("put_options", "P")):
                x = r.get(side) or {}
                md, gk = x.get("market_data") or {}, x.get("option_greeks") or {}
                iv = gk.get("iv")
                rows.append({"strike": K, "cp": cp,
                             "bid": float(md.get("bid_price") or 0), "ask": float(md.get("ask_price") or 0),
                             "ltp": float(md.get("ltp") or 0), "oi": md.get("oi"),
                             "iv": float(iv) / 100 if iv not in (None, 0) else float("nan"),
                             "delta": gk.get("delta"), "security_id": x.get("instrument_key", "")})
        if not rows:
            raise UpstoxError(f"empty option chain for {expiry} (holiday, wrong expiry, or token scope)")
        self._cache[expiry] = (now, pd.DataFrame(rows), spot)
        return self._cache[expiry]

    def chain(self, expiry: str) -> pd.DataFrame:
        return self._fetch(expiry)[1]

    def spot(self) -> float:
        return self._fetch(self.expiries()[0])[2]


class UpstoxBroker(Broker):
    """LIVE trading. Run only on the machine whose static IP is registered with your Upstox app."""
    paper = False

    def __init__(self, access_token: str | None = None, http: Http | None = None,
                 md: UpstoxMarketData | None = None):
        tok = access_token or os.environ.get("UPSTOX_ACCESS_TOKEN")
        if not tok and http is None:
            raise UpstoxError("UPSTOX_ACCESS_TOKEN missing: run `python -m kavach.cli upstox-login` first")
        self.http = http or Http(tok, os.environ.get("UPSTOX_ALGO_NAME"))
        self.md = md or UpstoxMarketData(http=self.http)

    def spot(self) -> float:
        return self.md.spot()

    def expiries(self) -> list[str]:
        return self.md.expiries()

    def chain(self, expiry: str) -> pd.DataFrame:
        return self.md.chain(expiry)

    def place(self, leg: OrderLeg, timeout_s: float = 20.0) -> dict:
        body = {"quantity": leg.lots * leg.lot_size, "product": "D", "validity": "DAY",
                "price": round(leg.limit_price, 2), "tag": "kavach", "instrument_token": leg.security_id,
                "order_type": "LIMIT", "transaction_type": leg.side, "disclosed_quantity": 0,
                "trigger_price": 0, "is_amo": False, "slice": True}
        data = _ok(self.http("POST", f"{HFT}/v3/order/place", body=body))
        ids = data.get("order_ids") or []
        if not ids:
            return {"status": "REJECTED", "avg_price": 0.0, "order_id": "", "msg": str(data)}
        t_end = time.time() + timeout_s
        last = {}
        while time.time() < t_end:
            fills = [self._details(i) for i in ids]
            last = fills
            sts = {f.get("status", "").lower() for f in fills}
            if sts == {"complete"}:
                qty = sum(int(f.get("filled_quantity") or 0) for f in fills)
                avg = sum(float(f.get("average_price") or 0) * int(f.get("filled_quantity") or 0) for f in fills) / max(qty, 1)
                return {"status": "FILLED", "avg_price": avg, "order_id": ",".join(ids)}
            if sts & {"rejected", "cancelled"} and not sts & {"open", "trigger pending", "put order req received", "validation pending", "open pending"}:
                if any(int(f.get("filled_quantity") or 0) for f in fills):
                    break
                msg = "; ".join(f.get("status_message") or "" for f in fills)
                return {"status": "REJECTED", "avg_price": 0.0, "order_id": ",".join(ids), "msg": msg}
            time.sleep(1.0)
        for i, f in zip(ids, last or [{}] * len(ids)):
            if (f.get("status") or "").lower() not in ("complete", "rejected", "cancelled"):
                try:
                    self.http("DELETE", f"{HFT}/v3/order/cancel", {"order_id": i})
                except UpstoxError:
                    pass
        fills = [self._details(i) for i in ids]
        qty = sum(int(f.get("filled_quantity") or 0) for f in fills)
        if qty:
            avg = sum(float(f.get("average_price") or 0) * int(f.get("filled_quantity") or 0) for f in fills) / qty
            return {"status": "PARTIAL", "filled_qty": qty, "avg_price": avg, "order_id": ",".join(ids)}
        return {"status": "CANCELLED", "avg_price": 0.0, "order_id": ",".join(ids), "msg": "timeout"}

    def _details(self, oid: str) -> dict:
        return _ok(self.http("GET", f"{API}/v2/order/details", {"order_id": oid})) or {}

    def kill_switch(self) -> None:
        """Exit every NSE_FO position, then disable the NSE_FO segment for the day."""
        try:
            self.http("POST", f"{API}/v2/order/positions/exit", {"segment": "NSE_FO"}, body={})
        finally:
            time.sleep(3)
            self.http("POST", f"{API}/v2/user/kill-switch", body=[{"segment": "NSE_FO", "action": "DISABLE"}])


def login_url(client_id: str, redirect_uri: str) -> str:
    return ("https://api.upstox.com/v2/login/authorization/dialog?" +
            urllib.parse.urlencode({"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri}))


def exchange_code(code: str, client_id: str, client_secret: str, redirect_uri: str) -> str:
    """Daily OAuth: exchange the ?code= from the redirect for an access token."""
    data = urllib.parse.urlencode({"code": code, "client_id": client_id, "client_secret": client_secret,
                                   "redirect_uri": redirect_uri, "grant_type": "authorization_code"}).encode()
    req = urllib.request.Request(f"{API}/v2/login/authorization/token", data=data, method="POST",
                                 headers={"Accept": "application/json",
                                          "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)["access_token"]
