"""V1: paper-vs-sandbox separation, sandbox-host isolation, reconnect safety."""

import httpx
import pytest

from app.paper_trading.broker import PaperBroker
from app.brokers.sandbox import SandboxRequestError, UpstoxSandboxBroker
from app.backtesting.costs import ExecutionConfig
from app.config.settings import Settings

INSTRUMENT = "NSE_EQ|INE002A01018"


class RecordingTransport(httpx.BaseTransport):
    """Records every request URL; fakes the documented endpoints."""

    def __init__(self, state):
        self.state = state
        self.urls = []

    def handle_request(self, request):
        import json as _json

        self.urls.append(str(request.url))
        if request.method == "POST" and request.url.path == "/v3/order/place":
            oid = str(self.state["next"])
            self.state["next"] += 1
            self.state["orders"][oid] = {"status": "open", "filled": 0, "avg": 0.0}
            return httpx.Response(200, json={"status": "success",
                                             "data": {"order_ids": [oid]}})
        if request.method == "GET" and request.url.host == "api.upstox.com":
            rec = self.state["orders"].get(request.url.params["order_id"])
            if rec is None:
                return httpx.Response(400, json={"errors": [{"errorCode": "UDAPI100010"}]})
            if self.state.get("feed_down"):
                return httpx.Response(503, json={"errors": [{"errorCode": "UDAPI10999"}]})
            return httpx.Response(200, json={"status": "success", "data": [
                {"status": rec["status"], "filled_quantity": rec["filled"],
                 "average_price": rec["avg"], "order_timestamp": "t",
                 "status_message": ""}]})
        if request.method == "DELETE" and request.url.path == "/v3/order/cancel":
            return httpx.Response(200, json={"status": "success",
                                             "data": {"order_id": request.url.params["order_id"]}})
        return httpx.Response(500, json={"errors": [{"errorCode": "X"}]})


def new_state():
    return {"placed": 0, "orders": {}, "next": 1001, "auth_mode": "ok"}


class TestSandboxHostIsolation:
    def test_all_requests_stay_on_documented_sandbox_hosts(self):
        state = new_state()
        transport = RecordingTransport(state)
        broker = UpstoxSandboxBroker("tok", INSTRUMENT, transport=transport,
                                     repository=None, initial_capital=100000.0)
        broker.instrument_map["RELIANCE"] = INSTRUMENT  # 3.0.2 explicit
        intent = broker.submit("BUY", 5, 0.0, intent_id="iso1", symbol="RELIANCE")
        state["orders"]["1001"].update(status="complete", filled=5, avg=100.0)
        broker.reconcile()
        # Second, still-open order exercises the DELETE path.
        intent2 = broker.submit("SELL", 5, 0.0, intent_id="iso2", symbol="RELIANCE")
        broker.cancel(intent2)
        hosts = {httpx.URL(u).host for u in transport.urls}
        # V2 audit: api-hft.upstox.com is Upstox's LIVE order host and is
        # never contacted by the sandbox broker — only sandbox hosts plus
        # the documented read-only history endpoint.
        assert hosts == {"api-sandbox.upstox.com", "api.upstox.com"}
        assert "api-hft.upstox.com" not in hosts, \
            "the live order host must never be contacted in sandbox mode"
        paths = {httpx.URL(u).path for u in transport.urls}
        assert paths == {"/v3/order/place", "/v2/order/history", "/v3/order/cancel"}


class TestPaperSandboxSeparation:
    def test_default_sandbox_host_is_not_the_live_order_host(self):
        from app.config.hosts import LIVE_ORDER_HOSTS
        from app.config.settings import Settings

        settings = Settings(broker="UPSTOX_SANDBOX",
                            upstox_sandbox_access_token="t")
        assert settings.upstox_sandbox_base_url not in {
            f"https://{host}" for host in LIVE_ORDER_HOSTS}

    def test_paper_pipeline_untouched_by_sandbox_module(self):
        paper = PaperBroker(ExecutionConfig(), timeframe="1m")
        sandbox = UpstoxSandboxBroker("tok", INSTRUMENT, repository=None)
        assert type(paper) is not type(sandbox)
        assert not hasattr(paper, "upstox_order_ids"), "PaperBroker stays Upstox-free"
        assert sandbox.mode == "UPSTOX_SANDBOX"
        assert sandbox.mode == "UPSTOX_SANDBOX"
        # Default settings never select the sandbox broker.
        settings = Settings()
        assert settings.broker == "LOCAL_PAPER"

    def test_partial_then_cancelled_credits_delta_once(self):
        state = new_state()
        transport = RecordingTransport(state)
        broker = UpstoxSandboxBroker("tok", INSTRUMENT, transport=transport,
                                     repository=None, initial_capital=100000.0)
        broker.instrument_map["RELIANCE"] = INSTRUMENT  # 3.0.2 explicit
        broker.submit("BUY", 10, 0.0, intent_id="pf1", symbol="RELIANCE")
        state["orders"]["1001"].update(status="open", filled=4, avg=100.0)
        first = broker.reconcile()
        assert len(first) == 1 and first[0]["quantity"] == 4
        assert broker.ledger.position_quantity == 4
        # Cancelled with 4 filled: no new fills, no double credit.
        state["orders"]["1001"].update(status="cancelled", filled=4)
        assert broker.reconcile() == []
        assert broker.ledger.position_quantity == 4

    def test_incremental_partials_apply_each_delta_once(self):
        state = new_state()
        transport = RecordingTransport(state)
        broker = UpstoxSandboxBroker("tok", INSTRUMENT, transport=transport,
                                     repository=None, initial_capital=100000.0)
        broker.instrument_map["RELIANCE"] = INSTRUMENT  # 3.0.2 explicit
        broker.submit("BUY", 10, 0.0, intent_id="pf2", symbol="RELIANCE")
        state["orders"]["1001"].update(status="open", filled=3, avg=100.0)
        assert broker.reconcile()[0]["quantity"] == 3
        state["orders"]["1001"].update(status="open", filled=7, avg=100.5)
        assert broker.reconcile()[0]["quantity"] == 4
        state["orders"]["1001"].update(status="complete", filled=10, avg=100.25)
        assert broker.reconcile()[0]["quantity"] == 3
        assert broker.ledger.position_quantity == 10
        assert broker.reconcile() == []


class TestReconcileResilience:
    def test_transient_history_failure_does_not_finalize_orders(self):
        state = new_state()
        transport = RecordingTransport(state)
        broker = UpstoxSandboxBroker("tok", INSTRUMENT, transport=transport,
                                     repository=None, initial_capital=100000.0)
        broker.instrument_map["RELIANCE"] = INSTRUMENT  # 3.0.2 explicit
        broker.submit("BUY", 5, 0.0, intent_id="rc1", symbol="RELIANCE")
        state["feed_down"] = True
        assert broker.reconcile() == []  # warning, order stays open
        state["feed_down"] = False
        state["orders"]["1001"].update(status="complete", filled=5, avg=100.0)
        fills = broker.reconcile()
        assert len(fills) == 1 and fills[0]["quantity"] == 5
        assert broker.ledger.position_quantity == 5


class TestModifyBodyMatchesDocs:
    def test_modify_request_carries_documented_fields_only(self):
        import json as _json

        captured = {}

        def handler(request):
            captured.update(_json.loads(request.content))
            return httpx.Response(200, json={"status": "success",
                                             "data": {"order_id": "9001"}})

        broker = UpstoxSandboxBroker("t", "K", transport=httpx.MockTransport(handler),
                                     repository=None)
        from app.brokers.sandbox import SandboxOrderState

        broker.orders["m1"] = SandboxOrderState(
            our_id="sbox-m1", symbol="S", side="BUY", quantity=5, price=100.0,
            status="OPEN", upstox_order_ids=["9001"])
        broker.modify("m1", quantity=7, price=101.0)
        assert set(captured) == {"quantity", "validity", "price", "order_id",
                                 "order_type", "disclosed_quantity",
                                 "trigger_price", "market_protection"}
        assert captured["order_id"] == "9001"
        assert captured["validity"] == "DAY"
        assert captured["market_protection"] == -1
