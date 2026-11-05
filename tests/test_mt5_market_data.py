"""Read-only MT5 market data + local paper integration tests.

All terminals, accounts, credentials, ticks and bars here are in-memory fakes.
No MetaTrader5 package, broker connection, order method or external network is
used. Real Vantage DEMO verification remains a separate local Windows gate.
"""

from __future__ import annotations

import ast
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import time
from types import ModuleType, SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.config.settings import Settings
from app.market_data.mt5 import (
    MarketDataAuthenticationError,
    MarketDataError,
    MarketDataIdentityError,
    MarketDataInvalid,
    MarketDataStale,
    MT5MarketDataService,
    PROVIDER_NAME,
)
from app.market_data.paper_provider import MT5PaperProvider
from app.market_data.read_only_gateway import ReadOnlyMT5Gateway
from app.vantage.models import SessionMode
from app.vantage.secrets import MemorySecretStore
from tests.mt5_market_data_fake import FakeReadOnlyMT5Gateway


TOKEN = "fake-mt5-market-data-api-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
FORBIDDEN = (
    "order_send", "order_check", "order_calc_margin", "orders_get",
    "positions_get", "history_orders_get", "history_deals_get",
)


def make_service(gateway=None, **kwargs):
    gateway = gateway or FakeReadOnlyMT5Gateway()
    secrets = MemorySecretStore()
    secrets.put("VANTAGE_DEMO", {
        "login": "998877", "password": "fake-unit-test-password",
        "server": gateway.server, "terminal_path": "",
    })
    secrets.put("VANTAGE_LIVE", {
        "login": "111111", "password": "fake-live-must-never-be-read",
        "server": "fake-live-must-never-be-read", "terminal_path": "",
    })
    options = {"advance_timeout_seconds": 0.05, "poll_seconds": 0.0}
    options.update(kwargs)
    service = MT5MarketDataService(
        secrets, lambda: SessionMode.PAPER, lambda: gateway, **options,
    )
    return service, gateway, secrets


def test_read_only_surface_has_no_order_or_position_action_methods():
    service, gateway, _ = make_service()
    provider = MT5PaperProvider(service, "EURUSD", "1m")
    expected_gateway_api = {
        "initialize", "shutdown", "last_error", "account_info", "terminal_info",
        "symbols_get", "symbol_info", "symbol_select", "symbol_info_tick",
        "copy_rates_from_pos", "constant",
    }
    production_api = {
        name for name in dir(ReadOnlyMT5Gateway)
        if not name.startswith("_") and callable(getattr(ReadOnlyMT5Gateway, name))
    }
    assert production_api == expected_gateway_api
    for obj in (service, gateway, provider):
        public = {name for name in dir(obj) if not name.startswith("_")}
        assert not set(FORBIDDEN) & public


def test_production_gateway_retains_no_module_or_order_bindings(monkeypatch):
    import app.market_data.read_only_gateway as gateway_module

    calls = []
    def order_trap(*args, **kwargs):
        raise AssertionError("an order/position/history function was reachable or called")

    fake = ModuleType("MetaTrader5")
    returns = {
        "initialize": True, "shutdown": None, "last_error": (0, "fake"),
        "account_info": {}, "terminal_info": {}, "symbols_get": [],
        "symbol_info": {}, "symbol_select": True, "symbol_info_tick": {},
        "copy_rates_from_pos": [],
    }
    for name, result in returns.items():
        def reader(*args, _name=name, _result=result, **kwargs):
            calls.append(_name)
            return _result
        setattr(fake, name, reader)
    for name in FORBIDDEN:
        setattr(fake, name, order_trap)
    for name in (
        "TIMEFRAME_M1", "TIMEFRAME_M5", "TIMEFRAME_M15", "TIMEFRAME_M30",
        "TIMEFRAME_H1", "TIMEFRAME_H4", "TIMEFRAME_D1",
    ):
        setattr(fake, name, 1)
    monkeypatch.setattr(gateway_module, "os", SimpleNamespace(name="nt"))
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)

    gateway = ReadOnlyMT5Gateway()
    assert set(gateway._api) == set(returns)
    assert not hasattr(gateway, "_mt5")
    assert set(vars(gateway)) == {"_api", "_constants"}
    assert not set(FORBIDDEN) & set(gateway._api)
    assert gateway.initialize(login=1, password="fake-only", server="fake-only") is True
    gateway.shutdown()
    assert calls == ["initialize", "shutdown"]

    # Static reachability guard for every production module in this package.
    package = Path(gateway_module.__file__).parent
    for source in package.glob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr not in FORBIDDEN, f"{source}:{node.lineno}"


def test_connect_reads_only_demo_and_preserves_exact_account_specific_symbols():
    service, gateway, secrets = make_service()
    original_get = secrets.get
    requested = []
    def recording_get(key):
        requested.append(key)
        return original_get(key)
    secrets.get = recording_get

    status = service.connect()
    assert requested == ["VANTAGE_DEMO"]
    assert status["execution_mode"] == "AXIOM_PAPER"
    assert status["account"]["account_trade_mode"] == "DEMO"
    assert status["account"]["login_masked"] == "••••77"
    assert status["instrument_count"] == 4
    assert service.execution_mode() == SessionMode.PAPER
    serialized = json.dumps(status)
    assert "fake-unit-test-password" not in serialized
    assert "998877" not in serialized
    assert "fake-live-must-never-be-read" not in serialized

    rows = {item["symbol"]: item for item in service.instrument_list()}
    assert "aBc.Def" in rows and "ABC.DEF" not in rows
    assert rows["AAPL"]["product_type"] == "SHARE_CFD"
    assert rows["AAPL"]["is_cfd"] is True
    assert rows["AAPL"]["contract_size"] == 100000
    assert rows["MYSTERY"]["market_data_supported"] is False
    service.ensure_advancing("aBc.Def")
    assert service.selected_symbol == "aBc.Def"
    with pytest.raises(MarketDataError, match="not returned"):
        service.instrument("ABC.DEF")
    with pytest.raises(MarketDataError):
        service.ensure_advancing("MYSTERY")


def test_authentication_identity_and_terminal_fail_closed():
    service, gateway, _ = make_service()
    gateway.auth_ok = False
    with pytest.raises(MarketDataAuthenticationError):
        service.connect()
    assert service.status()["connected"] is False

    service, gateway, _ = make_service()
    gateway.trade_mode = 2
    with pytest.raises(MarketDataIdentityError, match="not confirmed DEMO"):
        service.connect()

    service, gateway, _ = make_service()
    gateway.server = "unexpected-server"
    with pytest.raises(MarketDataIdentityError, match="identity"):
        service.connect()

    service, gateway, _ = make_service()
    service.connect()
    service.ensure_advancing("EURUSD")
    gateway.connected = False
    # The entry guard itself must detect shutdown, even before another poll.
    reason = service.entry_block_reason("EURUSD")
    assert reason and ("disconnected" in reason.lower() or "shut down" in reason.lower())
    assert service.status()["state"] == "DEGRADED"
    with pytest.raises(MarketDataError, match="disconnected|shut down"):
        service.quote("EURUSD")


@pytest.mark.parametrize("behavior", ["stale", "future", "invalid", "crossed", "missing"])
def test_bad_ticks_are_never_accepted(behavior):
    service, gateway, _ = make_service()
    service.connect()
    gateway.tick_behavior = behavior
    with pytest.raises(MarketDataError):
        service.ensure_advancing("EURUSD")
    assert service.selected_symbol is None
    assert service.entry_block_reason("EURUSD")


def test_subscription_refusal_is_explicit_and_blocks_entries():
    service, gateway, _ = make_service()
    service.connect()
    gateway.subscription_ok = False
    with pytest.raises(MarketDataError, match="refused the symbol subscription"):
        service.ensure_advancing("EURUSD")
    status = service.status()
    assert status["state"] == "DEGRADED"
    assert status["selected_symbol"] is None
    assert "subscription" in status["detail"]
    assert service.entry_block_reason("EURUSD")


def test_out_of_order_and_non_advancing_ticks_fail_closed():
    service, gateway, _ = make_service()
    service.connect()
    service.ensure_advancing("EURUSD")
    gateway.tick_behavior = "out_of_order"
    with pytest.raises(MarketDataInvalid, match="out of order"):
        service.quote("EURUSD")

    service, gateway, _ = make_service(advance_timeout_seconds=0.0)
    service.connect()
    gateway.tick_behavior = "same"
    with pytest.raises(MarketDataStale, match="No advancing"):
        service.ensure_advancing("EURUSD")
    assert service.selected_symbol is None


@pytest.mark.parametrize("behavior", ["invalid", "unsorted", "duplicate", "future", "nan", "none", "empty"])
def test_bad_bars_are_rejected_without_substitute_data(behavior):
    service, gateway, _ = make_service()
    service.connect()
    service.ensure_advancing("EURUSD")
    gateway.bar_behavior = behavior
    with pytest.raises(MarketDataError):
        service.copy_bars("EURUSD", "1m", 3)


def test_closed_bar_request_excludes_forming_bar_and_preserves_volume_kind():
    service, gateway, _ = make_service()
    service.connect()
    service.ensure_advancing("EURUSD")
    bars, _ = service.copy_bars("EURUSD", "1m", 3)
    assert len(bars) == 3
    assert gateway.copy_calls[-1]["start"] == 1
    assert all(bar.volume > 0 for bar in bars)
    now = datetime.now(timezone.utc)
    available_at = bars[-1].available_at("1m")
    assert now - timedelta(seconds=5) <= available_at <= now + timedelta(seconds=2)
    assert available_at > bars[-1].timestamp
    assert [bar.timestamp for bar in bars] == sorted(bar.timestamp for bar in bars)


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    gateway = FakeReadOnlyMT5Gateway()
    secrets = MemorySecretStore()
    secrets.put("VANTAGE_DEMO", {
        "login": "998877", "password": "fake-api-test-password",
        "server": gateway.server, "terminal_path": "",
    })

    def factory(_secret_store, execution_mode, **kwargs):
        return MT5MarketDataService(
            secrets, execution_mode, lambda: gateway,
            audit=kwargs.get("audit"), advance_timeout_seconds=0.05,
            poll_seconds=0.0,
        )

    monkeypatch.setattr("app.api.server.MT5MarketDataService", factory)
    settings = Settings(
        database_url=f"sqlite:///{tmp_path}/market-data.db", api_token=TOKEN,
        currency="USD", default_timeframe="1m", data_source="generic",
        market_calendar="continuous",
    )
    app = create_app(settings, data_dir=tmp_path)
    with TestClient(app) as client:
        client.gateway = gateway
        yield client


def test_authenticated_api_live_prices_manual_only_paper_and_fail_closed(api_client):
    client = api_client
    assert client.post("/api/market-data/connect").status_code in {401, 403}
    before = client.get("/api/trading/status", headers=AUTH).json()
    connected = client.post("/api/market-data/connect", headers=AUTH)
    assert connected.status_code == 200, connected.text
    after = client.get("/api/trading/status", headers=AUTH).json()
    assert before["mode"] == after["mode"] == "AXIOM_PAPER"
    assert after["active"] is False and after["automation_enabled"] is False
    assert connected.json()["read_only"] is True

    discovered = client.get("/api/market-data/instruments?supported_only=true", headers=AUTH)
    assert discovered.status_code == 200
    symbols = {row["symbol"] for row in discovered.json()["instruments"]}
    assert "aBc.Def" in symbols and "DEMO" not in symbols

    chart = client.get(
        "/api/market-data/chart?symbol=aBc.Def&timeframe=1m&limit=30", headers=AUTH,
    )
    assert chart.status_code == 200, chart.text
    payload = chart.json()
    assert payload["market_data"] is True
    assert payload["symbol"] == "aBc.Def"
    assert payload["quote"]["fresh"] is True
    assert payload["quote"]["advancing"] is True
    assert payload["volume_kind"] == "BROKER_TICK_VOLUME"
    assert "READ-ONLY MARKET DATA" in payload["data_feed_label"]
    assert "REPLAY" not in payload["data_feed_label"]

    started = client.post("/paper/start", headers=AUTH, json={
        "source": "vantage_mt5", "symbol": "aBc.Def", "timeframe": "1m",
        "strategy": "manual_only", "parameters": {}, "dataset": "sample.csv",
        "replay_delay": 0.0,
    })
    assert started.status_code == 200, started.text
    assert started.json()["strategy"] == "manual_only"
    assert started.json()["automation_enabled"] is False
    deadline = time.time() + 2
    while time.time() < deadline:
        session = client.get("/paper/status", headers=AUTH).json()
        if session["processed_bars"] >= 1:
            break
        time.sleep(0.01)
    assert session["state"] == "RUNNING", session
    assert session["processed_bars"] == 1
    assert client.get("/paper/orders", headers=AUTH).json()["orders"] == []

    ticket = client.get("/paper/ticket?symbol=aBc.Def", headers=AUTH).json()
    assert ticket["entry_blocked"] is None
    assert ticket["quote_source"] == PROVIDER_NAME
    assert ticket["quote_timestamp"] and ticket["quote_received_at"] and ticket["quote_id"]

    order = {
        "symbol": "aBc.Def", "side": "BUY", "quantity": 1,
        "order_type": "MARKET", "idempotency_key": "paper-test-key-0001",
    }
    filled = client.post("/paper/order", headers=AUTH, json=order)
    assert filled.status_code == 200, filled.text
    result = filled.json()
    assert result["simulated"] is True and result["paper_only"] is True
    assert result["order"]["quote_source"] == PROVIDER_NAME
    assert result["fill"]["quote_source"] == PROVIDER_NAME
    assert result["fill"]["quote_timestamp"] == ticket["quote_timestamp"]
    assert result["fill"]["quote_received_at"] == ticket["quote_received_at"]
    duplicate = client.post("/paper/order", headers=AUTH, json=order)
    assert duplicate.status_code == 200 and duplicate.json()["duplicate"] is True
    assert len(client.get("/paper/orders", headers=AUTH).json()["orders"]) == 1
    changed = dict(order, quantity=2)
    assert client.post("/paper/order", headers=AUTH, json=changed).status_code == 409

    # The idempotency result is durable broker state, not a process-local cache.
    assert client.post("/paper/stop", headers=AUTH).status_code == 200
    from app.paper_trading.broker import PaperBroker
    previous_broker = client.app.state.broker
    repository = client.app.state.repository
    repository.release_writer()
    restored = PaperBroker(
        config=previous_broker.config, currency=previous_broker.currency,
        timeframe=previous_broker.timeframe, calendar=previous_broker.calendar,
        risk_limits=previous_broker.risk_limits.to_dict(), repository=repository,
    )
    client.app.state.broker = restored
    client.app.state.simulator.broker = restored
    after_restart = client.post("/paper/order", headers=AUTH, json=order)
    assert after_restart.status_code == 200, after_restart.text
    assert after_restart.json()["duplicate"] is True
    assert len(client.get("/paper/orders", headers=AUTH).json()["orders"]) == 1
    assert len(client.app.state.broker.fills) == 1

    position_before = client.get("/paper/positions", headers=AUTH).json()["positions"]
    fills_before = client.get("/api/trading/workspace", headers=AUTH).json()["deals"]
    assert len(position_before) == 1 and len(fills_before) == 1
    assert client.post("/api/market-data/disconnect", headers=AUTH).status_code == 200
    degraded_ticket = client.get("/paper/ticket?symbol=aBc.Def", headers=AUTH).json()
    assert degraded_ticket["entry_blocked"]
    assert degraded_ticket["quote_source"] == ticket["quote_source"]
    assert degraded_ticket["quote_timestamp"] == ticket["quote_timestamp"]
    assert degraded_ticket["quote_received_at"] == ticket["quote_received_at"]
    assert degraded_ticket["quote_id"] == ticket["quote_id"]
    blocked = client.post("/paper/order", headers=AUTH, json=dict(
        order, idempotency_key="paper-test-key-0002",
    ))
    assert blocked.status_code == 422
    assert "Disconnected" in blocked.text or "connect" in blocked.text
    assert client.get("/paper/positions", headers=AUTH).json()["positions"] == position_before
    assert len(client.get("/api/trading/workspace", headers=AUTH).json()["deals"]) == 1

    # Feed loss blocks exposure, not a reviewed local risk-reducing exit.
    exited = client.post("/paper/order", headers=AUTH, json={
        "symbol": "aBc.Def", "side": "SELL", "quantity": 1,
        "order_type": "MARKET", "idempotency_key": "paper-test-key-0003",
    })
    assert exited.status_code == 200, exited.text
    assert exited.json()["order"]["quote_source"] == PROVIDER_NAME
    assert exited.json()["fill"]["quote_timestamp"] == ticket["quote_timestamp"]
    assert exited.json()["fill"]["quote_received_at"] == ticket["quote_received_at"]
    assert exited.json()["fill"]["quote_id"] == ticket["quote_id"]
    assert client.get("/paper/positions", headers=AUTH).json()["positions"] == []
    unavailable_chart = client.get(
        "/api/market-data/chart?symbol=aBc.Def&timeframe=1m&limit=30", headers=AUTH,
    )
    assert unavailable_chart.status_code == 409
    assert "REPLAY" not in unavailable_chart.text.upper()


def test_failed_mt5_start_does_not_cancel_existing_working_orders(api_client, monkeypatch):
    from datetime import datetime, timezone
    from app.backtesting.orders import Order

    client = api_client
    assert client.post("/api/market-data/connect", headers=AUTH).status_code == 200
    broker = client.app.state.broker
    existing = Order("EURUSD", "BUY", 1, datetime.now(timezone.utc).isoformat())
    broker.orders[existing.id] = existing
    broker._persist()

    async def reject_start(provider, strategy, symbols):
        raise ValueError("injected startup refusal")
    monkeypatch.setattr(client.app.state.simulator, "start", reject_start)
    response = client.post("/paper/start", headers=AUTH, json={
        "source": "vantage_mt5", "symbol": "EURUSD", "timeframe": "1m",
        "strategy": "manual_only", "parameters": {}, "dataset": "sample.csv",
    })
    assert response.status_code == 422
    assert broker.orders[existing.id].status == "NEW"
    assert len(broker.fills) == 0
    assert not broker.portfolio.positions


def test_mt5_start_rejects_automation_and_missing_idempotency(api_client):
    client = api_client
    assert client.post("/api/market-data/connect", headers=AUTH).status_code == 200
    response = client.post("/paper/start", headers=AUTH, json={
        "source": "vantage_mt5", "symbol": "EURUSD", "timeframe": "1m",
        "strategy": "moving_average", "parameters": {}, "dataset": "sample.csv",
    })
    assert response.status_code == 422
    assert "manual_only" in response.text

    # An API caller cannot turn a manual-only request into an automated one.
    from app.api.schemas import PaperStartRequest
    with pytest.raises(ValueError, match="manual_only"):
        PaperStartRequest(source="vantage_mt5", strategy="moving_average")

    unsupported = client.post("/paper/start", headers=AUTH, json={
        "source": "vantage_mt5", "symbol": "EURUSD", "timeframe": "2m",
        "strategy": "manual_only", "parameters": {}, "dataset": "sample.csv",
    })
    assert unsupported.status_code == 422
    assert "Unsupported timeframe" in unsupported.text

    started = client.post("/paper/start", headers=AUTH, json={
        "source": "vantage_mt5", "symbol": "EURUSD", "timeframe": "1m",
        "strategy": "manual_only", "parameters": {}, "dataset": "sample.csv",
    })
    assert started.status_code == 200, started.text
    deadline = time.time() + 2
    while time.time() < deadline:
        session = client.get("/paper/status", headers=AUTH).json()
        if session["processed_bars"] >= 1:
            break
        time.sleep(0.01)
    assert session["processed_bars"] >= 1
    keyless = client.post("/paper/order", headers=AUTH, json={
        "symbol": "EURUSD", "side": "BUY", "quantity": 1, "order_type": "MARKET",
    })
    assert keyless.status_code == 409
    assert "idempotency key" in keyless.text
    assert client.get("/paper/orders", headers=AUTH).json()["orders"] == []


def test_provider_does_not_backfill_history_into_decisions():
    async def scenario():
        service, gateway, _ = make_service()
        service.connect()
        provider = MT5PaperProvider(service, "EURUSD", "1m")
        provider.prepare()
        stream = provider.stream(["EURUSD"], "1m")
        first = await asyncio.wait_for(stream.__anext__(), timeout=1)
        provenance = provider.provenance_for(first)
        assert provenance["source"] == PROVIDER_NAME
        assert provenance["kind"] == "MT5_CLOSED_BAR"
        assert len(provenance["id"]) == 64
        # Only one latest closed bar is staged, not the available chart history.
        assert not provider._pending
        await stream.aclose()
        await provider.close()
    asyncio.run(scenario())
