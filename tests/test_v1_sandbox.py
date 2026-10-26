"""V1: UpstoxSandboxBroker lifecycle against a deterministic mock transport.

Covers: documented place body, order-id persistence + transitions, intent-id
idempotency (no duplicate POSTs), reconciliation idempotency, restart-safe
restoration (never resubmit), cancel/modify, fail-closed auth handling, token
secrecy, and the persisted emergency stop.
"""

import json
import logging

import httpx
import pytest

from app.brokers.sandbox import (
    SandboxAuthError,
    SandboxOrderState,
    SandboxRequestError,
    UpstoxSandboxBroker,
)
from app.config.settings import Settings
from app.database.database import Database
from app.database.repository import Repository
from app.engine.sandbox_engine import SandboxEngine
from app.learning.journal import LearningJournal
from app.risk.limits import HardRiskLimits

PLACE_URL = "https://api-hft.upstox.com/v3/order/place"
CANCEL_URL = "https://api-hft.upstox.com/v3/order/cancel"
INSTRUMENT = "NSE_EQ|INE002A01018"


def make_handler(state):
    def handler(request: httpx.Request) -> httpx.Response:
        if state["auth_mode"] != "ok":
            return httpx.Response(
                401, json={"errors": {"errorCode": "UDAPI1052", "message": "auth"}}
            )
        if request.method == "POST" and request.url.path == "/v3/order/place":
            body = json.loads(request.content)
            assert body["product"] == "I" and body["validity"] == "DAY"
            # 3.0.2: the tag is the durable intent-derived broker reference
            assert body["tag"] and len(body["tag"]) <= 20
            assert body["instrument_token"] == INSTRUMENT
            oid = str(state["next"])
            state["next"] += 1
            state["placed"] += 1
            state["orders"][oid] = {"status": "open pending", "filled": 0, "avg": 0.0}
            return httpx.Response(
                200,
                json={"status": "success", "data": {"order_ids": [oid]},
                      "metadata": {"latency": 1}},
            )
        if request.method == "GET" and request.url.host == "api.upstox.com":
            oid = request.url.params["order_id"]
            rec = state["orders"].get(oid)
            if rec is None:
                return httpx.Response(400, json={"errors": [
                    {"errorCode": "UDAPI100010", "message": "order not found"}]})
            return httpx.Response(200, json={"status": "success", "data": [
                {"status": rec["status"], "filled_quantity": rec["filled"],
                 "average_price": rec["avg"], "order_timestamp": "2026-01-01T09:15:00",
                 "status_message": ""}]})
        if request.method == "DELETE" and request.url.path == "/v3/order/cancel":
            oid = request.url.params["order_id"]
            rec = state["orders"].get(oid)
            if rec is None:
                return httpx.Response(400, json={"errors": [{"errorCode": "UDAPI100010"}]})
            if rec["status"] == "complete":
                return httpx.Response(400, json={"errors": [{"errorCode": "UDAPI100040"}]})
            rec["status"] = "cancel pending"
            return httpx.Response(200, json={"status": "success", "data": {"order_id": oid}})
        return httpx.Response(500, json={"errors": [{"errorCode": "X", "message": "unexpected"}]})

    return handler


def new_state():
    return {"placed": 0, "orders": {}, "next": 1001, "auth_mode": "ok"}


@pytest.fixture()
def repo(tmp_path):
    database = Database(f"sqlite:///{tmp_path}/sandbox_test.db")
    repository = Repository(database)
    yield repository
    database.close()


def make_broker(state, repo=None):
    broker = UpstoxSandboxBroker(
        "sandbox-token-xyz", INSTRUMENT,
        transport=httpx.MockTransport(make_handler(state)),
        repository=repo, initial_capital=100000.0,
    )
    # 3.0.2 fail-closed routing: tests configure symbol identity explicitly
    broker.instrument_map["RELIANCE"] = INSTRUMENT
    return broker


def stub_engine(broker, repo):
    class _P:
        symbols = ["RELIANCE"]

    strategy = type("S", (), {"name": "momentum", "parameters": {"period": 2},
                              "history_limit": 30})()
    return SandboxEngine(provider=_P(), strategy=strategy, broker=broker,
                         repository=repo,
                         risk_limits=HardRiskLimits.from_settings(Settings()),
                         journal=LearningJournal(repo),
                         instrument_keys={"RELIANCE": INSTRUMENT})


class TestSandboxLifecycle:
    def test_submit_places_documented_body_and_persists(self, repo):
        state = new_state()
        broker = make_broker(state, repo)
        intent = broker.submit("BUY", 10, 0.0, intent_id="int-buy-1", symbol="RELIANCE")
        assert intent == "int-buy-1"
        assert state["placed"] == 1
        orders = repo.list_sandbox_orders()
        assert orders[0]["upstox_order_id"] == "1001"
        assert orders[0]["intent_id"] == "int-buy-1"
        assert orders[0]["side"] == "BUY" and orders[0]["quantity"] == 10
        assert orders[0]["tag"] == "int-buy-1"
        assert orders[0]["transitions"], "initial transition must be recorded"

    def test_duplicate_intent_id_never_reposts(self, repo):
        state = new_state()
        broker = make_broker(state, repo)
        broker.submit("BUY", 10, 0.0, intent_id="int-1", symbol="RELIANCE")
        broker.submit("BUY", 10, 0.0, intent_id="int-1", symbol="RELIANCE")
        assert state["placed"] == 1

    def test_reconcile_applies_fill_once_and_updates_status(self, repo):
        state = new_state()
        broker = make_broker(state, repo)
        broker.submit("BUY", 10, 0.0, intent_id="int-1", symbol="RELIANCE")
        state["orders"]["1001"].update(status="complete", filled=10, avg=100.0)
        fills = broker.reconcile()
        assert len(fills) == 1 and fills[0]["side"] == "BUY"
        assert fills[0]["quantity"] == 10 and fills[0]["price"] == 100.0
        assert broker.reconcile() == []  # idempotent
        assert repo.list_sandbox_orders()[0]["status"] == "FILLED"
        ledger = broker.ledger
        assert ledger.position_quantity == 10 and ledger.cash < 100000

    def test_rejected_order_persists_rejected_status(self, repo):
        def rejecting(request):
            return httpx.Response(400, json={"errors": [
                {"errorCode": "UDAPI11002", "message": "insufficient margin"}]})

        broker = UpstoxSandboxBroker(
            "t", "K", transport=httpx.MockTransport(rejecting), repository=repo)
        broker.instrument_map["RELIANCE"] = "K"
        with pytest.raises(SandboxRequestError):
            broker.submit("BUY", 10, 0.0, intent_id="r1", symbol="RELIANCE")
        assert repo.list_sandbox_orders()[0]["status"] == "REJECTED"

    def test_zero_quantity_rejected_before_any_http_call(self, repo):
        state = new_state()
        broker = make_broker(state, repo)
        with pytest.raises(ValueError):
            broker.submit("BUY", 0, 0.0, intent_id="q0", symbol="S")
        assert state["placed"] == 0


class TestRestartSafety:
    def test_restore_reregisters_open_orders_without_resubmit(self, repo):
        state = new_state()
        broker = make_broker(state, repo)
        broker.submit("BUY", 10, 0.0, intent_id="int-1", symbol="RELIANCE")
        state["orders"]["1001"].update(status="complete", filled=10, avg=100.0)
        broker.reconcile()
        intent2 = broker.submit("SELL", 5, 0.0, intent_id="int-2", symbol="RELIANCE")
        state["orders"]["1002"].update(status="open", filled=0, avg=0.0)

        fresh = make_broker(state, repo)
        engine = stub_engine(fresh, repo)  # constructor auto-restores open orders
        assert "int-2" in fresh.orders
        assert engine.restore_from_repository() == 0  # already registered
        assert state["placed"] == 2, "restore must never resubmit"

    def test_engine_deduplicates_processed_signals_across_restart(self, repo):
        from app.data.models import Candle
        from datetime import datetime, timezone

        state = new_state()
        broker = make_broker(state, repo)
        engine = stub_engine(broker, repo)
        candle = Candle(datetime(2026, 1, 2, tzinfo=timezone.utc), 100, 101, 99, 100.5,
                        10000, "RELIANCE")

        class _Strat:
            name = "momentum"
            parameters = {"period": 2}
            history_limit = 30

            def generate_signal(self, candles):
                from app.strategies.base import Signal

                return Signal.BUY

        class _Metrics:
            def metrics(self):
                return {"market_open": True, "stale_feed": False}

        engine.provider = _Metrics()
        engine.strategy = _Strat()
        engine.processed = 0
        broker2_state = new_state()  # fresh broker so history is empty → BUY
        state.update(broker2_state)
        engine.broker = make_broker(state, repo)
        engine.process_candle(candle)
        assert engine.submitted == 1
        assert state["placed"] == 1

        # Restart: new engine, same repository → same candle must not repost.
        engine2 = stub_engine(make_broker(state, repo), repo)
        engine2.strategy = _Strat()
        engine2.provider = _Metrics()
        engine2.process_candle(candle)
        assert state["placed"] == 1, "duplicate signal after restart must be ignored"


class TestCancelModify:
    def test_cancel_unknown_intent_raises_locally(self, repo):
        state = new_state()
        broker = make_broker(state, repo)
        with pytest.raises(ValueError):
            broker.cancel("does-not-exist")
        assert state["placed"] == 0

    def test_cancel_of_finalized_order_is_guarded(self, repo):
        state = new_state()
        broker = make_broker(state, repo)
        intent = broker.submit("BUY", 5, 0.0, intent_id="c1", symbol="RELIANCE")
        state["orders"]["1001"].update(status="complete", filled=5, avg=100.0)
        broker.reconcile()
        result = broker.cancel(intent)
        assert result == {"status": "FILLED", "already_final": True}
        assert state["orders"]["1001"]["status"] == "complete"  # no DELETE sent

    def test_cancel_rejection_by_api_is_surfaced_harmlessly(self, repo):
        state = new_state()
        broker = make_broker(state, repo)
        intent = broker.submit("BUY", 5, 0.0, intent_id="c2", symbol="RELIANCE")
        # Broker-side finalized transition between place and cancel: the API
        # answers the documented UDAPI100040 rejection; cancel must not crash.
        state["orders"]["1001"].update(status="complete")
        result = broker.cancel(intent)
        assert result["results"][0]["cancelled"] is False
        assert result["results"][0]["http_status"] == 400

    def test_cancel_open_order_and_reconcile(self, repo):
        state = new_state()
        broker = make_broker(state, repo)
        intent = broker.submit("SELL", 5, 0.0, intent_id="int-2", symbol="RELIANCE")
        broker.cancel(intent)
        assert state["orders"]["1001"]["status"] == "cancel pending"
        state["orders"]["1001"].update(status="cancelled")
        broker.reconcile()
        assert repo.list_sandbox_orders()[0]["status"] == "CANCELLED"

    def test_modify_hits_documented_v3_endpoint(self, repo):
        def modify_handler(request):
            if request.method == "PUT" and request.url.path == "/v3/order/modify":
                return httpx.Response(200, json={
                    "status": "success",
                    "data": {"order_id": json.loads(request.content)["order_id"]}})
            return httpx.Response(500)

        broker = UpstoxSandboxBroker(
            "t", "K", transport=httpx.MockTransport(modify_handler), repository=None)
        broker.orders["im"] = SandboxOrderState(
            our_id="sbox-im", symbol="S", side="BUY", quantity=5, price=100.0,
            status="OPEN", upstox_order_ids=["9001"])
        result = broker.modify("im", quantity=7, price=101.0)
        assert result["status"] == "MODIFIED"
        assert result["order_id"] == "9001"


class TestFailClosed:
    def test_auth_failure_blocks_all_calls(self, repo):
        state = new_state()
        broker = make_broker(state, repo)
        broker.submit("BUY", 5, 0.0, intent_id="int-1", symbol="RELIANCE")
        state["auth_mode"] = "bad"
        with pytest.raises(SandboxAuthError):
            broker.submit("BUY", 1, 0.0, intent_id="int-x", symbol="RELIANCE")
        assert broker.available is False
        with pytest.raises(SandboxAuthError):
            broker.reconcile()
        assert state["placed"] == 1

    def test_token_never_appears_in_errors_or_logs(self, repo):
        state = new_state()
        state["auth_mode"] = "bad"
        stream = io_string()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("bot.broker.sandbox")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            with pytest.raises(SandboxAuthError) as excinfo:
                UpstoxSandboxBroker(
                    "SECRET-TOKEN", "K",
                    transport=httpx.MockTransport(make_handler(state))).submit(
                    "BUY", 1, 0.0, intent_id="i", symbol="S",
                    instrument_key="K")  # auth fails before identity matters
            assert "SECRET-TOKEN" not in str(excinfo.value)
        finally:
            logger.removeHandler(handler)
        assert "SECRET-TOKEN" not in stream.getvalue()


class TestEmergencyStop:
    def test_emergency_stop_persists_and_cancels_open_orders(self, repo):
        state = new_state()
        broker = make_broker(state, repo)
        broker.submit("BUY", 3, 0.0, intent_id="int-eme", symbol="RELIANCE")
        engine = stub_engine(broker, repo)
        stop = engine.emergency_stop("verification")
        assert stop["stopped"] and len(stop["cancelled"]) == 1
        assert engine.state == "EMERGENCY_STOPPED"
        assert repo.get_engine_state("emergency_stop", {}).get("stopped") is True
        engine.resume()
        assert engine._check_emergency() is False

    def test_run_halts_when_persisted_stop_flag_set(self, repo):
        from datetime import datetime, timezone

        from app.data.models import Candle
        from app.strategies.base import Signal

        class _P:
            symbols = ["RELIANCE"]

            def metrics(self):
                return {"market_open": True, "stale_feed": False, "mode": "ltpc"}

            async def stream(self, symbols, timeframe):
                yield Candle(datetime(2026, 1, 2, tzinfo=timezone.utc), 100, 101, 99,
                             100.5, 10000, "RELIANCE")

        class _Strat:
            name = "momentum"
            parameters = {"period": 2}
            history_limit = 30

            def generate_signal(self, candles):
                return Signal.BUY

        state = new_state()
        broker = make_broker(state, repo)
        engine = stub_engine(broker, repo)
        engine.provider = _P()
        engine.strategy = _Strat()
        repo.set_engine_state("emergency_stop", {"stopped": True})
        engine.run(max_candles=1)
        assert engine.state == "EMERGENCY_STOPPED"
        assert state["placed"] == 0, "no order may be submitted after a persisted stop"


def io_string():
    import io

    return io.StringIO()


@pytest.mark.parametrize("base_url", [
    "http://api-hft.upstox.com",
    "https://api-hft.upstox.com.evil.example",
    "https://api.upstox.com",
    "https://example.invalid",
])
def test_sandbox_rejects_nonofficial_order_host_without_network(base_url):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(500)

    with pytest.raises(ValueError, match="official"):
        UpstoxSandboxBroker(
            "not-a-real-token", INSTRUMENT, base_url=base_url,
            transport=httpx.MockTransport(handler),
        )
    assert calls == []


@pytest.mark.parametrize("history_url", [
    "http://api.upstox.com/v2/order/history",
    "https://api.upstox.com.evil.example/v2/order/history",
    "https://api-hft.upstox.com/v2/order/history",
    "https://api.upstox.com/v3/order/history",
])
def test_sandbox_rejects_nonofficial_history_url_without_network(history_url):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(500)

    with pytest.raises(ValueError, match="official"):
        UpstoxSandboxBroker(
            "not-a-real-token", INSTRUMENT, history_url=history_url,
            transport=httpx.MockTransport(handler),
        )
    assert calls == []


def test_sandbox_rechecks_endpoints_after_construction_tampering():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"status": "success", "data": []})

    broker = UpstoxSandboxBroker(
        "not-a-real-token", INSTRUMENT,
        transport=httpx.MockTransport(handler),
    )
    broker._base_url = "https://api-hft.upstox.com.evil.example"
    with pytest.raises(ValueError, match="official"):
        broker._request("POST", "/v3/order/place", json={})
    broker._base_url = "https://api-hft.upstox.com"
    broker._history_url = "https://api.upstox.com.evil.example/v2/order/history"
    with pytest.raises(ValueError, match="official"):
        broker._request("GET", broker._history_url)
    assert calls == []


def test_settings_rejects_forged_sandbox_endpoints():
    with pytest.raises(ValueError, match="official"):
        Settings(
            broker="UPSTOX_SANDBOX",
            upstox_sandbox_access_token="not-a-real-token",
            upstox_sandbox_base_url="https://api-hft.upstox.com.evil.example",
        )
