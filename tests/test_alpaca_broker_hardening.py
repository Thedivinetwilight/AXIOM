"""Phase 3 & 5 tests: Alpaca Broker hardening (P0/P1 safety, SQLite journal, LIVE gate)."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import uuid

from fastapi.testclient import TestClient
import httpx
import pytest

from app.alpaca_broker.client import (
    LIVE_HOST,
    PAPER_HOST,
    AlpacaReadOnlyClient,
    AlpacaTradingClient,
)
from app.alpaca_broker.gate import GateError, GateState
from app.alpaca_broker.journal import AlpacaBrokerJournal
from app.alpaca_broker.service import (
    LIVE_CONFIRM_PHRASE,
    AlpacaBrokerService,
    BrokerRiskLimits,
    BrokerStateBlocked,
)
from app.api.server import create_app
from app.config.settings import Settings
from app.markets.us_calendar import USEquitiesCalendar

NOW_OPEN = datetime(2026, 10, 29, 14, 30, tzinfo=timezone.utc)  # 10:30 EDT regular session


class FakeBrokerBackend:
    def __init__(self, *, expected_host: str = PAPER_HOST, is_open: bool = True, equity: float = 100_000.0, last_equity: float = 100_000.0):
        self.expected_host = expected_host
        self.is_open = is_open
        self.equity = equity
        self.last_equity = last_equity
        self.orders: dict[str, dict] = {}
        self.by_client_id: dict[str, str] = {}
        self.positions: list[dict] = []
        self.fail_next_submit_500 = False
        self.requests: list[tuple[str, str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        host = str(request.url.host)
        method = request.method
        path = request.url.path
        self.requests.append((host, method, path))
        assert host == self.expected_host, f"unexpected host {host}"

        if (method, path) == ("GET", "/v2/account"):
            return httpx.Response(
                200,
                json={
                    "id": "acct-9999",
                    "account_number": "PA99887766",
                    "status": "ACTIVE",
                    "currency": "USD",
                    "cash": str(self.equity),
                    "buying_power": str(self.equity),
                    "equity": str(self.equity),
                    "last_equity": str(self.last_equity),
                    "portfolio_value": str(self.equity),
                    "trading_blocked": False,
                    "account_blocked": False,
                    "trade_suspended_by_user": False,
                },
            )
        if (method, path) == ("GET", "/v2/clock"):
            return httpx.Response(
                200,
                json={
                    "timestamp": NOW_OPEN.isoformat(),
                    "is_open": self.is_open,
                    "next_open": "2026-10-30T13:30:00Z",
                    "next_close": "2026-10-29T20:00:00Z",
                },
            )
        if (method, path) == ("GET", "/v2/positions"):
            return httpx.Response(200, json=self.positions)
        if (method, path) == ("GET", "/v2/orders"):
            open_list = [
                o for o in self.orders.values() if o["status"] in {"accepted", "new", "partially_filled"}
            ]
            return httpx.Response(200, json=open_list)
        if (method, path) == ("GET", "/v2/orders:by_client_order_id"):
            cid = request.url.params.get("client_order_id", "")
            oid = self.by_client_id.get(cid)
            if not oid:
                return httpx.Response(404, json={"message": "not found"})
            return httpx.Response(200, json=self.orders[oid])
        if method == "GET" and path.startswith("/v2/orders/"):
            oid = path.rsplit("/", 1)[-1]
            if oid not in self.orders:
                return httpx.Response(404, json={"message": "not found"})
            return httpx.Response(200, json=self.orders[oid])
        if (method, path) == ("POST", "/v2/orders"):
            if self.fail_next_submit_500:
                self.fail_next_submit_500 = False
                return httpx.Response(502, json={"message": "gateway timeout"})
            body = json.loads(request.content.decode("utf-8"))
            oid = f"brk-{uuid.uuid4().hex[:10]}"
            order = {
                "id": oid,
                "client_order_id": body["client_order_id"],
                "symbol": body["symbol"],
                "side": body["side"],
                "qty": body.get("qty"),
                "notional": body.get("notional"),
                "type": body["type"],
                "time_in_force": body["time_in_force"],
                "limit_price": body.get("limit_price"),
                "stop_price": body.get("stop_price"),
                "status": "accepted",
                "filled_qty": "0",
                "filled_avg_price": None,
                "submitted_at": NOW_OPEN.isoformat(),
                "updated_at": NOW_OPEN.isoformat(),
            }
            self.orders[oid] = order
            self.by_client_id[body["client_order_id"]] = oid
            return httpx.Response(200, json=order)
        if method == "PATCH" and path.startswith("/v2/orders/"):
            old_oid = path.rsplit("/", 1)[-1]
            old = self.orders[old_oid]
            old["status"] = "replaced"
            patch = json.loads(request.content.decode("utf-8"))
            new_oid = f"brk-repl-{uuid.uuid4().hex[:8]}"
            replaced = {
                **old,
                "id": new_oid,
                "status": "accepted",
                "qty": patch.get("qty", old["qty"]),
                "limit_price": patch.get("limit_price", old["limit_price"]),
                "updated_at": NOW_OPEN.isoformat(),
            }
            self.orders[new_oid] = replaced
            self.by_client_id[old["client_order_id"]] = new_oid
            return httpx.Response(200, json=replaced)
        if method == "DELETE" and path.startswith("/v2/orders/"):
            oid = path.rsplit("/", 1)[-1]
            self.orders[oid]["status"] = "canceled"
            return httpx.Response(204)
        if (method, path) == ("DELETE", "/v2/orders"):
            open_list = [o for o in self.orders.values() if o["status"] == "accepted"]
            for o in open_list:
                o["status"] = "canceled"
            return httpx.Response(200, json=open_list)
        if method == "DELETE" and path.startswith("/v2/positions/"):
            sym = path.rsplit("/", 1)[-1]
            self.positions = [p for p in self.positions if p["symbol"] != sym]
            close_oid = f"brk-close-{uuid.uuid4().hex[:8]}"
            close_order = {
                "id": close_oid,
                "client_order_id": f"close-{sym}",
                "symbol": sym,
                "side": "sell",
                "qty": "5",
                "type": "market",
                "time_in_force": "day",
                "status": "accepted",
                "filled_qty": "0",
                "submitted_at": NOW_OPEN.isoformat(),
                "updated_at": NOW_OPEN.isoformat(),
            }
            self.orders[close_oid] = close_order
            return httpx.Response(200, json=close_order)
        raise AssertionError(f"Unhandled fake request: {method} {path}")

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def _fresh_quote(_symbol: str) -> dict:
    return {
        "symbol": "AAPL",
        "last": 225.00,
        "timestamp": NOW_OPEN.isoformat(),
        "is_delayed": False,
        "source": "alpaca-market-data:iex",
    }


def test_live_readiness_arm_and_confirmed_activate_use_readonly_preflight_with_zero_orders():
    """Fixes P0 ISS-06 + P1 ISS-11: LIVE preflight/arm works via AlpacaReadOnlyClient; zero orders sent."""
    fake = FakeBrokerBackend(expected_host=LIVE_HOST, is_open=True)
    ro_client = AlpacaReadOnlyClient(
        "live-k", "live-s", environment="live", client=httpx.Client(transport=fake.transport())
    )
    service = AlpacaBrokerService(
        None,
        environment="live",
        limits=BrokerRiskLimits(10_000, 5, 20),
        read_only_client=ro_client,
        client_factory=lambda permit: AlpacaTradingClient(
            "live-k", "live-s", environment="live", live_permit=permit,
            client=httpx.Client(transport=fake.transport()),
        ),
        quote_provider=_fresh_quote,
        calendar=USEquitiesCalendar(),
        clock=lambda: NOW_OPEN,
    )
    assert service.gate.state == GateState.DISABLED
    checks = service.readiness_checks(symbol="AAPL")
    assert all(checks.values()), f"expected all readiness checks to pass: {checks}"

    service.gate.arm(checks)
    assert service.gate.state == GateState.ARMED
    assert service.client is None

    # Wrong phrase or unacknowledged risk is refused
    with pytest.raises(GateError):
        service.activate_live_confirmed(confirm_live_trading="wrong phrase", acknowledge_risk=True, symbol="AAPL")
    with pytest.raises(GateError):
        service.activate_live_confirmed(confirm_live_trading=LIVE_CONFIRM_PHRASE, acknowledge_risk=False, symbol="AAPL")

    activated = service.activate_live_confirmed(
        confirm_live_trading=LIVE_CONFIRM_PHRASE, acknowledge_risk=True, symbol="AAPL"
    )
    assert activated["active"] is True
    assert service.client is not None
    # Zero POST/DELETE/PATCH calls occurred during readiness, Arm, or Activate!
    assert all(method == "GET" for _, method, _ in fake.requests)


def test_market_order_valuation_and_reg_nms_tick_fail_closed():
    """Fixes P0 ISS-07: share-qty market orders cannot bypass max_order_value or buying_power."""
    fake = FakeBrokerBackend(expected_host=PAPER_HOST)
    paper_client = AlpacaTradingClient(
        "pk", "ps", environment="paper", client=httpx.Client(transport=fake.transport())
    )
    service = AlpacaBrokerService(
        paper_client,
        environment="paper",
        limits=BrokerRiskLimits(max_order_value=1_000.0, max_open_positions=5, max_daily_orders=20),
        quote_provider=_fresh_quote,  # AAPL @ $225.00
        clock=lambda: NOW_OPEN,
        require_market_price_bound=True,
    )
    # 10 shares * $225 = $2,250 > $1,000 cap -> MUST be rejected!
    oversized_mkt = service.preview_order(symbol="AAPL", side="buy", qty=10, order_type="market")
    assert oversized_mkt["ok"] is False
    assert any(c["check"] == "max_order_value" and not c["ok"] for c in oversized_mkt["checks"])

    # Stale quote blocks market order valuation
    stale_service = AlpacaBrokerService(
        paper_client,
        environment="paper",
        limits=BrokerRiskLimits(max_order_value=5_000.0, max_open_positions=5, max_daily_orders=20),
        quote_provider=lambda _s: {"last": 225.0, "timestamp": "2026-10-29T10:00:00Z"},
        clock=lambda: NOW_OPEN,
        require_market_price_bound=True,
    )
    stale_prev = stale_service.preview_order(symbol="AAPL", side="buy", qty=1, order_type="market")
    assert stale_prev["ok"] is False
    assert any(c["check"] == "market_reference_price" and not c["ok"] for c in stale_prev["checks"])

    # Reg NMS Rule 612: sub-penny >= $1.00 rejected; sub-dollar 4-decimal accepted
    bad_tick = service.preview_order(symbol="AAPL", side="buy", qty=1, order_type="limit", limit_price=100.005)
    assert bad_tick["ok"] is False
    good_subdollar = service.preview_order(symbol="AAPL", side="buy", qty=100, order_type="limit", limit_price=0.5125)
    assert good_subdollar["ok"] is True


def test_durable_sqlite_intent_blocks_after_uncertain_500_until_recovered(tmp_path: Path):
    """Fixes P1 ISS-08: SQLite journal persists unresolved intent across crash/restart."""
    fake = FakeBrokerBackend(expected_host=PAPER_HOST)
    fake.fail_next_submit_500 = True
    db_file = tmp_path / "broker_journal.sqlite3"

    def build_svc():
        return AlpacaBrokerService(
            AlpacaTradingClient("pk", "ps", environment="paper", client=httpx.Client(transport=fake.transport())),
            environment="paper",
            limits=BrokerRiskLimits(10_000, 5, 20),
            journal=AlpacaBrokerJournal(db_file),
            clock=lambda: NOW_OPEN,
        )

    svc1 = build_svc()
    res = svc1.submit_order(
        client_order_id="crash-cid-1", symbol="AAPL", side="buy", qty=2, order_type="limit", limit_price=100.0
    )
    assert res["ok"] is False and res["rejected"] == "uncertain"
    assert svc1.blocked == "UNRESOLVED_ORDER_INTENT"

    # Simulated process restart: fresh service loads SQLite journal and STILL blocks!
    svc2 = build_svc()
    assert svc2.blocked == "UNRESOLVED_ORDER_INTENT"
    assert "crash-cid-1" in svc2.unresolved_intents
    with pytest.raises(BrokerStateBlocked):
        svc2.submit_order(symbol="AAPL", side="buy", qty=1, order_type="limit", limit_price=100.0)

    # Recovery queries broker by client_order_id (404 not created), clears unresolved intent cleanly
    rec = svc2.recover()
    assert rec["ok"] is True and rec["resolved_intents"] == 1
    assert svc2.blocked is None and not svc2.unresolved_intents


def test_replace_order_updates_id_incremental_fills_and_protective_emergency_stop():
    """Fixes P1 ISS-09, ISS-10, and ISS-12."""
    fake = FakeBrokerBackend(expected_host=PAPER_HOST)
    client = AlpacaTradingClient("pk", "ps", environment="paper", client=httpx.Client(transport=fake.transport()))
    svc = AlpacaBrokerService(
        client,
        environment="paper",
        limits=BrokerRiskLimits(50_000, 5, 20),
        clock=lambda: NOW_OPEN,
    )

    # Submit, replace -> new broker_order_id -> reconcile MUST pass!
    sub = svc.submit_order(
        client_order_id="rep-1", symbol="AAPL", side="buy", qty=4, order_type="limit", limit_price=200.0
    )
    old_id = sub["order"]["broker_order_id"]
    rep = svc.replace_order("rep-1", qty=5, limit_price=201.0)
    assert rep["ok"] is True
    new_id = rep["order"]["broker_order_id"]
    assert new_id != old_id
    assert svc.reconcile()["ok"] is True

    # Partial fills: 2 shares, then cumulative 5 shares -> incremental_qty is "2", then "3"!
    fake.orders[new_id].update({"filled_qty": "2", "filled_avg_price": "200.50", "status": "partially_filled"})
    f1 = svc.sync_fills()["new_fills"]
    assert len(f1) == 1 and f1[0]["incremental_qty"] == "2" and f1[0]["filled_qty"] == "2"

    fake.orders[new_id].update({"filled_qty": "5", "filled_avg_price": "200.80", "status": "filled", "updated_at": "2026-10-29T14:35:00Z"})
    f2 = svc.sync_fills()["new_fills"]
    assert len(f2) == 1 and f2[0]["incremental_qty"] == "3" and f2[0]["filled_qty"] == "5"

    # Open position AAPL qty=5 with a protective sell stop AND a working MSFT buy entry
    fake.positions = [{"symbol": "AAPL", "qty": "5", "side": "long", "avg_entry_price": "200.80", "unrealized_pl": "10.0"}]
    svc.reconcile()
    stop_res = svc.submit_order(
        client_order_id="prot-stop-1", symbol="AAPL", side="sell", qty=5, order_type="stop", stop_price=190.0
    )
    entry_res = svc.submit_order(
        client_order_id="entry-msft-1", symbol="MSFT", side="buy", qty=2, order_type="limit", limit_price=400.0
    )
    stop_id = stop_res["order"]["broker_order_id"]
    entry_id = entry_res["order"]["broker_order_id"]

    kill = svc.emergency_stop("volatility halt")
    assert kill["canceled"] == 1
    assert stop_id in kill["preserved_protective_orders"]
    assert fake.orders[stop_id]["status"] == "accepted"
    assert fake.orders[entry_id]["status"] == "canceled"

    # Explicit operator position close remains auditable after reset
    svc.gate.reset_kill_switch(operator_ack=True)
    closed = svc.close_position("AAPL", reason="operator flatten")
    assert closed["ok"] is True and closed["symbol"] == "AAPL"


def test_alpaca_broker_credential_and_mode_api_routes_never_echo_secrets(tmp_path: Path):
    settings = replace(Settings(), api_token="local-api-test", database_url=f"sqlite:///{tmp_path}/app.db")
    headers = {"Authorization": "Bearer local-api-test"}
    with TestClient(create_app(settings, data_dir=tmp_path)) as web:
        st0 = web.get("/api/alpaca-broker/credentials", headers=headers).json()
        assert st0["paper"]["present"] is False and st0["live"]["present"] is False

        saved = web.post(
            "/api/alpaca-broker/credentials/paper",
            headers=headers,
            json={"api_key": "PK-SECRET-CANARY", "api_secret": "PS-SECRET-CANARY"},
        )
        assert saved.status_code == 200
        assert "CANARY" not in saved.text
        assert saved.json()["paper"]["present"] is True
        assert saved.json()["live"]["present"] is False

        # Enabling LIVE when ALPACA_BROKER_ENABLED=false is refused fail-closed!
        denied_live = web.post(
            "/api/alpaca-broker/mode",
            headers=headers,
            json={"enabled": True, "environment": "live"},
        )
        assert denied_live.status_code == 409
