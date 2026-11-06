"""The hidden legacy Vantage/MT5 live API must not be reachable.

``app/api/routes/vantage.py`` exposed 30 authenticated endpoints under
``/api/trading`` — session selection (``VANTAGE_DEMO`` / ``VANTAGE_LIVE``),
DPAPI credential entry, ``arm``/``activate``, order review/submit/cancel,
position modify and emergency stop. None of it was described by the product
documentation, and the operator UI for it was deleted in ``v3.7.0a9``.

The supported product is NSE (Upstox) / NASDAQ (Alpaca) market data, LOCAL
PAPER simulation and separately gated live trading. Because Vantage is no longer
intended, the router is no longer imported or mounted: every one of those paths
must answer ``404`` rather than silently accepting a live order request.

The dormant module stays in the tree (it is unreferenced by the app) so the
legacy isolation tests keep working; these tests fail if anyone re-mounts it.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.config.settings import Settings

TOKEN = "vantage-removal-test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

# Every route the removed router used to serve.
LEGACY_GET_PATHS = [
    "/api/trading/status",
    "/api/trading/connection",
    "/api/trading/instruments",
    "/api/trading/quotes",
    "/api/trading/chart",
    "/api/trading/readiness",
    "/api/trading/overview",
    "/api/trading/workspace",
    "/api/trading/activity",
    "/api/trading/performance",
]
LEGACY_POST_PATHS = [
    "/api/trading/session",
    "/api/trading/connection/test",
    "/api/trading/arm",
    "/api/trading/activate",
    "/api/trading/orders/review",
    "/api/trading/orders/submit",
    "/api/trading/emergency",
]


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        api_token=TOKEN,
        database_url="sqlite:///:memory:",
        data_dir=str(tmp_path),
    )
    app = create_app(settings, data_dir=tmp_path)
    with TestClient(app) as test_client:
        yield test_client


def test_no_route_is_mounted_under_the_legacy_prefix(client):
    mounted = sorted(
        route.path for route in client.app.routes
        if str(getattr(route, "path", "")).startswith("/api/trading")
    )
    assert mounted == [], f"legacy Vantage MT5 routes are reachable again: {mounted}"


@pytest.mark.parametrize("path", LEGACY_GET_PATHS)
def test_legacy_get_routes_are_gone(client, path):
    assert client.get(path, headers=HEADERS).status_code == 404


@pytest.mark.parametrize("path", LEGACY_POST_PATHS)
def test_legacy_post_routes_are_gone(client, path):
    response = client.post(path, headers=HEADERS, json={})
    assert response.status_code == 404


def test_live_order_submission_cannot_be_reached_at_all(client):
    """An unauthenticated caller gets 404 (not 401): the route does not exist."""
    response = client.post(
        "/api/trading/orders/submit",
        json={"symbol": "EURUSD", "side": "BUY", "volume": 0.1},
    )
    assert response.status_code == 404
    assert "order" not in response.text.lower() or "not found" in response.text.lower()


def test_legacy_package_remains_importable_but_unwired():
    module = importlib.import_module("app.api.routes.vantage")
    assert module.router.prefix == "/api/trading"
    # Importable for the legacy isolation tests, but nothing mounts it.
    import app.api.server as server_module

    source = open(server_module.__file__, encoding="utf-8").read()
    mounted_block = source.split("for module in (", 1)[1].split(")", 1)[0]
    assert "vantage" not in mounted_block


def test_supported_surfaces_still_answer_and_live_stays_off(client):
    health = client.get("/health").json()
    assert health["status"] == "ok"
    assert health["live_trading"] is False
    assert health["real_order_routing"] == "DISABLED_PAPER_ONLY"
    assert health["vantage_api"] == "REMOVED"

    live = client.get("/api/markets/live/status", headers=HEADERS).json()
    assert live["default_live_execution"] == "OFF"
    assert live["nse"]["availability"] == "NOT_AVAILABLE"
    assert live["nasdaq"]["availability"] == "NOT_AVAILABLE"

    # Paper simulation (the supported product) still works.
    assert client.get("/paper/status", headers=HEADERS).status_code == 200
    assert client.get("/api/performance", headers=HEADERS).json()["paper_only"] is True
