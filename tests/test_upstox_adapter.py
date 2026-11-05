"""Upstox adapter against a fake HTTP layer (no network). Shapes follow Upstox v2/v3 docs."""
import pytest

from kavach.brokers.upstox import UpstoxBroker, UpstoxMarketData, UpstoxError, NIFTY_KEY
from kavach.brokers.paper import PaperBroker
from kavach.governor import OrderLeg


class FakeHttp:
    def __init__(self, fill_status="complete"):
        self.calls = []
        self.fill_status = fill_status

    def __call__(self, method, url, params=None, body=None):
        self.calls.append((method, url, params, body))
        if url.endswith("/v2/option/contract"):
            assert params["instrument_key"] == NIFTY_KEY
            return {"status": "success", "data": [{"expiry": "2099-11-10", "lot_size": 65},
                                                  {"expiry": "2099-11-17", "lot_size": 65},
                                                  {"expiry": "2000-01-01", "lot_size": 65}]}
        if url.endswith("/v2/option/chain"):
            data = []
            for k in range(25000, 26050, 50):
                data.append({"expiry": params["expiry_date"], "strike_price": float(k),
                             "underlying_key": NIFTY_KEY, "underlying_spot_price": 25510.2,
                             "call_options": {"instrument_key": f"NSE_FO|{k}C",
                                              "market_data": {"ltp": 100.5, "bid_price": 100.0, "ask_price": 101.0, "oi": 1},
                                              "option_greeks": {"iv": 12.5, "delta": 0.5}},
                             "put_options": {"instrument_key": f"NSE_FO|{k}P",
                                             "market_data": {"ltp": 90.5, "bid_price": 90.0, "ask_price": 91.0, "oi": 1},
                                             "option_greeks": {"iv": 13.1, "delta": -0.5}}})
            return {"status": "success", "data": data}
        if url.endswith("/v3/order/place"):
            return {"status": "success", "data": {"order_ids": ["111"]}, "metadata": {"latency": 30}}
        if url.endswith("/v2/order/details"):
            filled = 130 if self.fill_status == "complete" else 0
            return {"status": "success", "data": {"status": self.fill_status, "average_price": 100.9,
                                                  "filled_quantity": filled, "status_message": "x"}}
        if url.endswith("/v3/order/cancel"):
            return {"status": "success", "data": {"order_id": params["order_id"]}}
        if url.endswith("/v2/order/positions/exit") or url.endswith("/v2/user/kill-switch"):
            return {"status": "success", "data": {}}
        raise AssertionError(url)


def test_chain_parse_expiries_spot():
    md = UpstoxMarketData(http=FakeHttp())
    assert md.expiries() == ["2099-11-10", "2099-11-17"]            # past expiries dropped
    ch = md.chain("2099-11-10")
    assert len(ch) == 42
    r = ch[(ch.strike == 25500) & (ch.cp == "P")].iloc[0]
    assert (r.bid, r.ask) == (90.0, 91.0) and abs(r.iv - 0.131) < 1e-9 and r.security_id == "NSE_FO|25500P"
    assert md.spot() == 25510.2


def test_place_is_limit_carry_lot65_v3():
    h = FakeHttp()
    b = UpstoxBroker(http=h)
    res = b.place(OrderLeg("C", 25500, "2099-11-10", "BUY", 2, 100.95, 100, 101, "NSE_FO|25500C"))
    assert res["status"] == "FILLED" and res["avg_price"] == 100.9
    m, url, _, body = next(c for c in h.calls if c[1].endswith("/v3/order/place"))
    assert url.startswith("https://api-hft.upstox.com") and m == "POST"
    assert body["order_type"] == "LIMIT" and body["product"] == "D" and body["quantity"] == 130
    assert body["instrument_token"] == "NSE_FO|25500C" and body["tag"] == "kavach"


def test_rejection_and_timeout_cancel():
    b = UpstoxBroker(http=FakeHttp("rejected"))
    assert b.place(OrderLeg("C", 25500, "2099-11-10", "BUY", 1, 100.95, 100, 101, "NSE_FO|25500C"))["status"] == "REJECTED"
    h = FakeHttp("open")
    b = UpstoxBroker(http=h)
    res = b.place(OrderLeg("C", 25500, "2099-11-10", "BUY", 1, 100.95, 100, 101, "NSE_FO|25500C"), timeout_s=0.1)
    assert res["status"] == "CANCELLED"
    assert any(c[1].endswith("/v3/order/cancel") for c in h.calls)


def test_kill_switch_exits_then_disables():
    h = FakeHttp()
    UpstoxBroker(http=h).kill_switch()
    urls = [c[1] for c in h.calls]
    assert urls[0].endswith("/positions/exit") and urls[1].endswith("/user/kill-switch")
    assert h.calls[1][3] == [{"segment": "NSE_FO", "action": "DISABLE"}]


def test_paper_broker_on_upstox_quotes_fills_at_touch():
    pb = PaperBroker(UpstoxMarketData(http=FakeHttp()))
    assert pb.spot() == 25510.2
    f = pb.place(OrderLeg("P", 25500, "2099-11-10", "SELL", 1, 90.0, 90, 91, "NSE_FO|25500P"))
    assert f["status"] == "FILLED" and f["avg_price"] == 90.0
    f = pb.place(OrderLeg("P", 25500, "2099-11-10", "BUY", 1, 90.5, 90, 91, "NSE_FO|25500P"))
    assert f["status"] == "CANCELLED"


def test_missing_token_is_explicit(monkeypatch):
    monkeypatch.delenv("UPSTOX_ANALYTICS_TOKEN", raising=False)
    monkeypatch.delenv("UPSTOX_ACCESS_TOKEN", raising=False)
    with pytest.raises(UpstoxError):
        UpstoxMarketData()
