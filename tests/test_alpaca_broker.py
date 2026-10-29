"""Alpaca broker path tests: lifecycle, duplicates, rejects, fills, kill switch.

All broker HTTP is mocked. The fake paper broker REFUSES any request whose host
is not ``paper-api.alpaca.markets`` — together with the permit tests this is the
proof that these tests cannot reach a LIVE endpoint. These tests are not proof
of real broker connectivity or of real fills; that requires the owner's Alpaca
paper account (and for LIVE, broker approval plus explicit Arm + Activate).
"""

from __future__ import annotations

import asyncio
import json
import uuid

import httpx
import pytest

from app.alpaca_broker.client import (
    LIVE_HOST,
    PAPER_HOST,
    AlpacaTradingClient,
    BrokerError,
    BrokerRejected,
    LiveEnvironmentBlocked,
    LivePermit,
)
from app.alpaca_broker.credentials import (
    MemoryBrokerSecretStore,
    load_broker_credentials,
)
from app.alpaca_broker.gate import BrokerLiveGate, GateError, GateState
from app.alpaca_broker.service import (
    AlpacaBrokerService,
    BrokerRiskLimits,
    BrokerStateBlocked,
)
from app.config.settings import Settings


class AuditLog:
    def __init__(self):
        self.events = []

    def record_system_event(self, kind, payload=None, **kwargs):
        self.events.append((kind, payload or {}))

    def kinds(self):
        return [kind for kind, _ in self.events]


class FakeAlpacaPaper:
    """In-memory Alpaca paper environment. Hard-fails any non-paper host."""

    def __init__(self, *, buying_power=100_000.0):
        self.account = {
            "id": "acct-uuid-1234567890", "account_number": "PA12345678",
            "status": "ACTIVE", "currency": "USD", "cash": "100000",
            "buying_power": str(buying_power), "equity": "100000",
            "last_equity": "100000", "portfolio_value": "100000",
            "trading_blocked": False, "trade_suspended_by_user": False,
            "account_blocked": False,
        }
        self.orders: dict[str, dict] = {}
        self.by_client: dict[str, str] = {}
        self.positions: list[dict] = []
        self.requests: list[tuple[str, str, str]] = []
        self.post_count = 0
        self.fail: dict[str, int] = {}      # "METHOD path" -> forced status
        self.hard_fail: set[str] = set()    # "METHOD path" -> 500

    # -- plumbing ---------------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != PAPER_HOST:
            raise AssertionError(
                f"LIVE endpoint reached in tests: {request.url.host}{request.url.path}"
            )
        method, path = request.method, request.url.path
        self.requests.append((request.url.host, method, path))
        forced = f"{method} {path}"
        if forced in self.hard_fail:
            return httpx.Response(500, json={"message": "fake broker failure"})
        if forced in self.fail:
            return httpx.Response(self.fail[forced], json={"message": "fake broker rejection"})

        if method == "GET" and path == "/v2/account":
            return httpx.Response(200, json=self.account)
        if method == "GET" and path == "/v2/clock":
            return httpx.Response(200, json={"is_open": True, "timestamp": "2026-10-29T14:30:00Z"})
        if method == "POST" and path == "/v2/orders":
            self.post_count += 1
            body = json.loads(request.content)
            client_id = body["client_order_id"]
            if client_id in self.by_client:
                return httpx.Response(422, json={"message": "client_order_id must be unique"})
            order = {
                "id": f"order-{uuid.uuid4().hex[:8]}",
                "client_order_id": client_id,
                "symbol": body["symbol"], "side": body["side"], "type": body["type"],
                "time_in_force": body["time_in_force"],
                "qty": body.get("qty"), "notional": body.get("notional"),
                "limit_price": body.get("limit_price"), "stop_price": body.get("stop_price"),
                "status": "accepted", "filled_qty": "0", "filled_avg_price": None,
                "submitted_at": "2026-10-29T14:30:01Z", "updated_at": "2026-10-29T14:30:01Z",
            }
            self.orders[order["id"]] = order
            self.by_client[client_id] = order["id"]
            return httpx.Response(200, json=order)
        if method == "GET" and path == "/v2/orders:by_client_order_id":
            client_id = request.url.params.get("client_order_id")
            order_id = self.by_client.get(client_id)
            if order_id is None:
                return httpx.Response(404, json={"message": "order not found"})
            return httpx.Response(200, json=self.orders[order_id])
        if method == "GET" and path == "/v2/orders":
            open_statuses = {"new", "accepted", "pending_new", "partially_filled",
                             "pending_cancel", "pending_replace", "held"}
            return httpx.Response(200, json=[
                o for o in self.orders.values() if o["status"] in open_statuses
            ])
        if method == "GET" and path.startswith("/v2/orders/"):
            order = self.orders.get(path.rsplit("/", 1)[-1])
            return httpx.Response(200, json=order) if order else httpx.Response(404, json={})
        if method == "DELETE" and path == "/v2/orders":
            canceled = []
            for order in self.orders.values():
                if order["status"] in {"new", "accepted", "partially_filled", "pending_new", "held"}:
                    order["status"] = "canceled"
                    canceled.append(order)
            return httpx.Response(200, json=canceled)
        if method == "DELETE" and path.startswith("/v2/orders/"):
            order = self.orders.get(path.rsplit("/", 1)[-1])
            if order is None:
                return httpx.Response(404, json={"message": "order not found"})
            order["status"] = "canceled"
            return httpx.Response(200, json={})
        if method == "PATCH" and path.startswith("/v2/orders/"):
            order = self.orders.get(path.rsplit("/", 1)[-1])
            if order is None:
                return httpx.Response(404, json={"message": "order not found"})
            body = json.loads(request.content)
            order.update({k: str(v) for k, v in body.items() if v is not None})
            order["status"] = "accepted"
            order["updated_at"] = "2026-10-29T14:35:00Z"
            return httpx.Response(200, json=order)
        if method == "GET" and path == "/v2/positions":
            return httpx.Response(200, json=self.positions)
        if method == "GET" and path == "/v2/account/activities":
            return httpx.Response(200, json=[])
        raise AssertionError(f"unexpected fake-broker request: {method} {path}")

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def client(self) -> AlpacaTradingClient:
        return AlpacaTradingClient(
            "fake-broker-key", "fake-broker-secret", environment="paper",
            client=httpx.Client(transport=self.transport()),
        )

    def fill(self, order_id: str, qty, price) -> dict:
        order = self.orders[order_id]
        order["filled_qty"] = str(qty)
        order["filled_avg_price"] = str(price)
        order["status"] = "filled"
        order["updated_at"] = "2026-10-29T15:00:00Z"
        return order


def make_service(fake: FakeAlpacaPaper, *, limits=None, audit=None, **kwargs):
    return AlpacaBrokerService(
        fake.client(),
        environment="paper",
        limits=limits or BrokerRiskLimits(10_000, 5, 20),
        repository=audit,
        **kwargs,
    )


ORDER = dict(symbol="AAPL", side="buy", qty=2, order_type="limit",
             time_in_force="day", limit_price=100.0)


# --- paper order lifecycle ----------------------------------------------------

def test_paper_order_lifecycle_preview_submit_partial_fill_complete():
    fake, audit = FakeAlpacaPaper(), AuditLog()
    service = make_service(fake, audit=audit)
    preview = service.preview_order(**ORDER)
    assert preview["ok"] is True
    assert preview["destination"]["mode"] == "ALPACA_PAPER"
    assert preview["destination"]["endpoint_host"] == PAPER_HOST
    assert preview["destination"]["is_real_money"] is False

    result = service.submit_order(client_order_id="axiom-test-1", **ORDER)
    assert result["ok"] is True and result["duplicate"] is False
    order = result["order"]
    assert order["status"] == "accepted"
    assert order["symbol"] == "AAPL"

    # Partial fill, then completion.
    fake.orders[order["broker_order_id"]].update(
        {"filled_qty": "1", "filled_avg_price": "100.1", "status": "partially_filled"})
    sync = service.sync_fills()
    assert len(sync["new_fills"]) == 1
    fake.fill(order["broker_order_id"], 2, "100.2")
    sync = service.sync_fills()
    assert service.orders["axiom-test-1"]["status"] == "filled"

    kinds = audit.kinds()
    assert "ALPACA_ORDER_INTENT" in kinds and "ALPACA_ORDER_SUBMITTED" in kinds
    assert kinds.count("ALPACA_ORDER_FILL") == 2
    assert "ALPACA_ORDER_TERMINAL" in kinds
    # Intent-before-write ordering.
    assert kinds.index("ALPACA_ORDER_INTENT") < kinds.index("ALPACA_ORDER_SUBMITTED")
    # Every broker request hit the paper host only (the fake hard-fails others).
    assert {host for host, _, _ in fake.requests} == {PAPER_HOST}
    assert fake.post_count == 1


def test_duplicate_submission_is_prevented():
    fake = FakeAlpacaPaper()
    service = make_service(fake)
    first = service.submit_order(client_order_id="dup-1", **ORDER)
    second = service.submit_order(client_order_id="dup-1", **ORDER)
    assert first["ok"] and not first["duplicate"]
    assert second["ok"] and second["duplicate"] is True
    assert second["order"]["broker_order_id"] == first["order"]["broker_order_id"]
    assert fake.post_count == 1  # one POST only


def test_restart_duplicate_is_adopted_not_reposted():
    fake = FakeAlpacaPaper()
    first = make_service(fake)
    first.submit_order(client_order_id="restart-1", **ORDER)
    # Simulated restart: fresh service with an empty journal, same broker.
    second = make_service(fake)
    result = second.submit_order(client_order_id="restart-1", **ORDER)
    assert result["ok"] and result["duplicate"] is True
    assert result["order"]["source"] == "broker-adopted"
    assert fake.post_count == 1  # never re-posted


# --- rejection paths -----------------------------------------------------------

def test_missing_risk_limits_fail_closed():
    fake = FakeAlpacaPaper()
    service = make_service(fake, limits=BrokerRiskLimits(0, 0, 0))
    preview = service.preview_order(**ORDER)
    assert preview["ok"] is False
    assert any("risk_limits_configured" == c["check"] and not c["ok"] for c in preview["checks"])
    result = service.submit_order(**ORDER)
    assert result["ok"] is False and result["rejected"] == "local_checks"
    assert fake.post_count == 0


def test_insufficient_buying_power_and_oversize_are_rejected():
    fake = FakeAlpacaPaper(buying_power=50.0)  # cannot afford 2 x $100
    service = make_service(fake)
    preview = service.preview_order(**ORDER)
    assert preview["ok"] is False
    reasons = " ".join(preview["rejection_reasons"])
    assert "buying power" in reasons
    result = service.submit_order(**ORDER)
    assert result["ok"] is False and fake.post_count == 0

    # Oversize vs max_order_value cap.
    over = dict(ORDER, qty=500, limit_price=100.0)
    preview = make_service(FakeAlpacaPaper()).preview_order(**over)
    assert preview["ok"] is False
    assert any("max_order_value" == c["check"] and not c["ok"] for c in preview["checks"])


def test_invalid_orders_are_rejected_locally():
    service = make_service(FakeAlpacaPaper())
    bad_symbol = service.preview_order(**dict(ORDER, symbol="lower case"))
    assert bad_symbol["ok"] is False
    both = service.preview_order(**dict(ORDER, qty=1, notional=50))
    assert both["ok"] is False
    limitless = service.preview_order(symbol="AAPL", side="buy", qty=1,
                                      order_type="limit")
    assert limitless["ok"] is False
    assert service.submit_order(**dict(ORDER, symbol="lower case"))["rejected"] == "local_checks"


def test_broker_rejection_is_fail_closed_with_reason():
    fake = FakeAlpacaPaper()
    fake.fail["POST /v2/orders"] = 422
    service = make_service(fake, audit=AuditLog())
    result = service.submit_order(client_order_id="rej-1", **ORDER)
    assert result["ok"] is False and result["rejected"] == "broker"
    assert "fake broker rejection" in result["detail"]
    assert service.orders == {}


# --- cancel / replace -----------------------------------------------------------

def test_cancel_and_replace_orders():
    fake, audit = FakeAlpacaPaper(), AuditLog()
    service = make_service(fake, audit=audit)
    submitted = service.submit_order(client_order_id="cr-1", **ORDER)["order"]
    replaced = service.replace_order("cr-1", qty=3, limit_price=101.0)
    assert replaced["ok"] and replaced["order"]["qty"] == "3"
    assert replaced["order"]["limit_price"] == "101.0"
    canceled = service.cancel_order("cr-1")
    assert canceled["ok"] and canceled["order"]["status"] == "pending_cancel"
    assert "ALPACA_ORDER_REPLACED" in audit.kinds()
    assert "ALPACA_ORDER_CANCELED" in audit.kinds()
    with pytest.raises(BrokerError):
        service.cancel_order("never-existed")


# --- reconciliation and recovery --------------------------------------------------

def test_reconcile_clean_and_mismatch_fail_closed():
    fake = FakeAlpacaPaper()
    service = make_service(fake)
    service.submit_order(client_order_id="rec-1", **ORDER)
    clean = service.reconcile()
    assert clean["ok"] is True and service.blocked is None

    # An order at the broker that the journal does not know about.
    rogue = dict(ORDER, client_order_id="rogue-1")
    other = make_service(fake)
    other.submit_order(**rogue)
    mismatch = service.reconcile()
    assert mismatch["ok"] is False
    assert service.blocked == "RECONCILIATION_FAILED"
    with pytest.raises(BrokerStateBlocked):
        service.submit_order(client_order_id="rec-2", **ORDER)


def test_restart_recovery_adopts_broker_state_and_uncertainty_blocks():
    fake = FakeAlpacaPaper()
    seed = make_service(fake)
    seed.submit_order(client_order_id="open-1", **ORDER)
    fake.positions = [{"symbol": "AAPL", "qty": "2", "side": "long",
                       "avg_entry_price": "100", "unrealized_pl": "0"}]

    restarted = make_service(fake)
    result = restarted.recover()
    assert result["ok"] is True and result["adopted_orders"] == 1
    assert restarted.positions["AAPL"]["qty"] == "2"
    assert restarted.orders["open-1"]["source"] == "broker-adopted"

    # Uncertain broker state (500) -> recovery fails closed.
    uncertain = FakeAlpacaPaper()
    uncertain.hard_fail.add("GET /v2/orders")
    service = make_service(uncertain)
    result = service.recover()
    assert result["ok"] is False and service.blocked == "RECOVERY_UNCERTAIN"
    with pytest.raises(BrokerStateBlocked):
        service.submit_order(**ORDER)


# --- kill switch -------------------------------------------------------------------

def test_emergency_stop_cancels_all_and_blocks_until_explicit_reset():
    fake, audit = FakeAlpacaPaper(), AuditLog()
    service = make_service(fake, audit=audit)
    service.submit_order(client_order_id="kill-1", **ORDER)
    result = service.emergency_stop("operator panic")
    assert result["ok"] is True and result["canceled"] == 1
    assert result["gate"]["state"] == "KILL_SWITCH"
    with pytest.raises(BrokerStateBlocked):
        service.submit_order(client_order_id="kill-2", **ORDER)
    with pytest.raises(GateError):
        service.gate.reset_kill_switch(operator_ack=False)
    reset = service.gate.reset_kill_switch(operator_ack=True)
    assert reset["state"] == "DISABLED"
    assert "ALPACA_EMERGENCY_STOP" in audit.kinds()
    # After reset the gate must be armed+activated again before LIVE; paper
    # submissions are allowed again once the kill switch is cleared.
    assert service.submit_order(client_order_id="kill-3", **ORDER)["ok"] is True


# --- LIVE endpoint proof + gate -------------------------------------------------------

def test_live_client_requires_permit_and_tests_never_reach_live():
    # Without a permit the LIVE client raises BEFORE any network call.
    with pytest.raises(LiveEnvironmentBlocked):
        AlpacaTradingClient("k", "s", environment="live")
    # A permit can never ride on a paper client.
    permit = LivePermit(gate_id="g", issued_at="2026-10-29T00:00:00+00:00")
    with pytest.raises(ValueError, match="never"):
        AlpacaTradingClient("k", "s", environment="paper", live_permit=permit)
    # Every fake-broker request in this file is host-checked; nothing but the
    # paper host is reachable. The live host constant is never requested.
    fake = FakeAlpacaPaper()
    service = make_service(fake)
    service.submit_order(client_order_id="hosts-1", **ORDER)
    assert {host for host, _, _ in fake.requests} == {PAPER_HOST}
    assert LIVE_HOST != PAPER_HOST


def test_gate_requires_arm_then_activate_and_disarm_invalidates():
    gate = BrokerLiveGate()
    assert gate.state == GateState.DISABLED  # LIVE disabled by default
    with pytest.raises(GateError):
        gate.activate()  # Activate without Arm is refused
    with pytest.raises(GateError):
        gate.arm({"risk_limits_configured": False})  # failed readiness refuses Arm
    gate.arm({"risk_limits_configured": True, "account_verified": True,
              "account_trading_allowed": True})
    assert gate.state == GateState.ARMED and gate.permit() is None
    permit = gate.activate()
    assert isinstance(permit, LivePermit) and gate.permit() is not None
    status = gate.disarm("credential change")
    assert status["permit_active"] is False and gate.permit() is None


def test_live_service_binds_client_only_after_activation():
    bound = {}

    def factory(permit):
        bound["permit"] = permit
        # Never sends a request: binding alone is what is under test.
        return AlpacaTradingClient("k", "s", environment="live", live_permit=permit)

    service = AlpacaBrokerService(
        None, environment="live", limits=BrokerRiskLimits(1000, 2, 5),
        client_factory=factory,
    )
    assert service.destination()["client_bound"] is False
    with pytest.raises(BrokerStateBlocked):
        service.submit_order(**ORDER)  # unbound live client fails closed
    service.gate.arm({"risk_limits_configured": True, "account_verified": True,
                      "account_trading_allowed": True})
    result = service.activate_live()
    assert result["active"] is True and bound["permit"] is not None
    assert service.destination()["endpoint_host"] == LIVE_HOST
    assert service.destination()["is_real_money"] is True
    service.disarm("operator disarm")
    assert service.client is None and service.destination()["client_bound"] is False


# --- credentials and visibility -------------------------------------------------------

def test_market_data_and_broker_credentials_are_distinct_fields():
    names = set(Settings.__dataclass_fields__)
    broker_fields = {n for n in names if n.startswith("alpaca_broker_")}
    data_fields = {n for n in names if n.startswith("alpaca_api") or n == "alpaca_data_feed"}
    assert broker_fields and not broker_fields & data_fields

    store = MemoryBrokerSecretStore()
    store.put("ALPACA_BROKER_PAPER", {"api_key": "bk", "api_secret": "bs"})
    creds, source = load_broker_credentials(store, "paper", Settings())
    assert source == "protected-store" and creds == {"api_key": "bk", "api_secret": "bs"}
    # The LIVE profile never reads the paper credential.
    creds_live, source_live = load_broker_credentials(store, "live", Settings())
    assert source_live == "none" and creds_live["api_key"] == ""
    # Environment fallback is reported honestly and never aliases market keys.
    settings = Settings(alpaca_broker_api_key="ek", alpaca_broker_api_secret="es")
    creds_env, source_env = load_broker_credentials(MemoryBrokerSecretStore(), "paper", settings)
    assert source_env == "environment" and creds_env["api_key"] == "ek"


def test_destination_is_visible_in_status_preview_and_orders():
    fake = FakeAlpacaPaper()
    service = make_service(fake)
    for payload in (service.status(), service.preview_order(**ORDER)):
        destination = payload.get("destination") or payload
        assert destination["mode"] == "ALPACA_PAPER"
        assert destination["endpoint_host"] == PAPER_HOST
        assert "no real money" in destination["destination_label"]
    order = service.submit_order(client_order_id="vis-1", **ORDER)
    status = service.status()
    assert status["mode"] == "ALPACA_PAPER" and status["endpoint_host"] == PAPER_HOST
    assert status["account"]["account_number_masked"].endswith("5678")
    assert "api_key" not in json.dumps(status) and "fake-broker-key" not in json.dumps(status)
