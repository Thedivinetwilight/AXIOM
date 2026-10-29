import json
from threading import Thread

import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.config.settings import Settings
from app.listed_assets import (
    ListedAssetIdentity, ListedAssetService, ListedProviderUnavailable,
)
from app.performance import LatencyTracker
from app.vantage.secrets import MemorySecretStore
from tests.mt5_fake import FakeMT5Gateway
from tests.test_vantage_api import HEADERS, activate, connect, order_payload


TOKEN = "local-api-test-token"


@pytest.fixture
def client(tmp_path):
    settings = Settings(api_token=TOKEN, database_url="sqlite:///:memory:", data_dir=str(tmp_path))
    app = create_app(settings, data_dir=tmp_path)
    with TestClient(app) as test_client:
        fake = FakeMT5Gateway()
        app.state.vantage.secret_store = MemorySecretStore()
        app.state.vantage.gateway_factory = lambda: fake
        app.state.fake_mt5 = fake
        yield test_client


def test_latency_tracker_reports_p50_p95_p99():
    tracker = LatencyTracker(128)
    for value in range(1, 101):
        tracker.observe("chart", value)
    row = tracker.summary()["metrics"]["chart"]
    assert row["p50_ms"] == pytest.approx(50.5)
    assert row["p95_ms"] == pytest.approx(95.05)
    assert row["p99_ms"] == pytest.approx(99.01)
    assert row["max_ms"] == 100


def test_latency_tracker_is_bounded_and_keeps_total_count():
    tracker = LatencyTracker(16)
    for value in range(100):
        tracker.observe("feed", value)
    row = tracker.summary()["metrics"]["feed"]
    assert row["samples"] == 100
    assert row["window_samples"] == 16
    assert row["last_ms"] == 99


def test_latency_tracker_ignores_invalid_diagnostics():
    tracker = LatencyTracker(16)
    for value in (None, float("nan"), float("inf"), -1, "bad"):
        tracker.observe("feed", value)
    tracker.observe("", 1)
    tracker.observe("x" * 81, 1)
    assert tracker.summary()["metrics"] == {}


def test_latency_tracker_records_exception_duration_without_swallowing():
    tracker = LatencyTracker(16)
    with pytest.raises(RuntimeError, match="ambiguous"):
        with tracker.measure("broker"):
            raise RuntimeError("ambiguous")
    assert tracker.summary()["metrics"]["broker"]["samples"] == 1


def test_latency_tracker_bounds_metric_names_and_can_clear_benchmark_state():
    tracker = LatencyTracker(16)
    for index in range(80):
        tracker.observe(f"metric-{index}", index)
    assert tracker.summary()["max_metric_names"] == 64
    assert len(tracker.summary()["metrics"]) == 64
    tracker.clear()
    assert tracker.summary()["metrics"] == {}


def test_latency_tracker_rejects_unbounded_configuration():
    with pytest.raises(ValueError):
        LatencyTracker(1)
    with pytest.raises(ValueError):
        LatencyTracker(10000)


def test_latency_tracker_supports_concurrent_observers():
    tracker = LatencyTracker(32)
    threads = [Thread(target=lambda: [tracker.observe("shared", 1) for _ in range(100)]) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    row = tracker.summary()["metrics"]["shared"]
    assert row["samples"] == 800 and row["window_samples"] == 32


def test_performance_api_requires_auth_and_is_initially_empty(client):
    assert client.get("/api/trading/performance").status_code == 401
    response = client.get("/api/trading/performance", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["sample_capacity_per_metric"] == 512
    assert body["metrics"] == {} or set(body["metrics"]) == {"http.request"}
    assert "zero-latency" in body["note"]


def test_fake_demo_order_separates_review_submit_broker_and_reconcile_metrics(client):
    connect(client)
    activate(client)
    payload = order_payload("metric-order-0001")
    review = client.post("/api/trading/orders/review", json=payload, headers=HEADERS)
    assert review.status_code == 200, review.text
    submitted = client.post("/api/trading/orders/submit", json={
        **payload, "review_id": review.json()["review_id"],
        "confirmation": review.json()["confirmation_phrase"],
    }, headers=HEADERS)
    assert submitted.status_code == 200, submitted.text
    body = client.get("/api/trading/performance", headers=HEADERS).json()
    expected = {
        "vantage.order.review_api", "vantage.order.submit_api",
        "vantage.broker.submission_ack", "vantage.reconciliation",
        "vantage.feed.gateway_receipt", "vantage.feed.observed_source_age",
    }
    assert expected <= set(body["metrics"])
    for name in expected:
        assert body["metrics"][name]["p50_ms"] >= 0
        assert body["metrics"][name]["p95_ms"] >= 0
        assert body["metrics"][name]["p99_ms"] >= 0
    assert len(client.app.state.fake_mt5.send_calls) == 1
    encoded = json.dumps(body)
    assert "123456" not in encoded and "password" not in encoded.lower()
    assert body["evidence"].startswith("No real DEMO")


def test_listed_identity_requires_exchange_currency_and_listed_product():
    identity = ListedAssetIdentity("aapl", "nasdaq", "usd", "LISTED_SHARE")
    assert identity.to_dict()["asset_key"] == "LISTED:NASDAQ:AAPL:USD"
    assert "not a CFD" in identity.to_dict()["ownership"]
    for values in (
        ("AAPL", "", "USD", "LISTED_SHARE"),
        ("AAPL", "NASDAQ", "", "LISTED_SHARE"),
        ("AAPL", "NASDAQ", "USD", "SHARE_CFD"),
        ("AAPL/NAS", "NASDAQ", "USD", "LISTED_SHARE"),
    ):
        with pytest.raises(ValueError):
            ListedAssetIdentity(*values)


def test_listed_default_fails_closed_without_cfd_or_execution_fallback():
    service = ListedAssetService()
    status = service.status()
    assert status["data_configured"] is False
    assert status["execution_supported"] is False
    assert status["mt5_routing_allowed"] is False
    assert status["cfd_substitution_allowed"] is False
    identity = ListedAssetIdentity("SPY", "ARCA", "USD", "LISTED_ETF")
    policy = service.routing_policy(identity)
    assert policy["data"]["allowed"] is False
    assert policy["execution"]["allowed"] is False
    assert policy["vantage_mt5_allowed"] is False
    assert policy["order_created"] is False
    with pytest.raises(ListedProviderUnavailable, match="No Vantage/CFD fallback"):
        service.snapshot(identity)


def test_listed_execution_adapter_cannot_be_enabled_by_accident():
    with pytest.raises(ListedProviderUnavailable, match="No listed execution adapter"):
        ListedAssetService(execution_provider=object())


def test_listed_provider_response_must_preserve_exact_identity():
    class WrongProvider:
        name = "fake-listed-data"
        def status(self):
            return {"fake": True}
        def snapshot(self, identity):
            return {"asset_key": "LISTED:OTHER:AAPL:USD"}

    service = ListedAssetService(WrongProvider())
    identity = ListedAssetIdentity("AAPL", "NASDAQ", "USD", "LISTED_SHARE")
    with pytest.raises(ListedProviderUnavailable, match="mismatched"):
        service.snapshot(identity)


def test_listed_api_is_authenticated_read_only_and_never_sends_mt5_order(client):
    assert client.get("/api/listed-assets/status").status_code == 401
    status = client.get("/api/listed-assets/status", headers=HEADERS)
    assert status.status_code == 200
    assert status.json()["execution_supported"] is False
    response = client.post("/api/listed-assets/routing-policy", json={
        "symbol": "AAPL", "exchange": "NASDAQ", "currency": "USD",
        "product_type": "LISTED_SHARE",
    }, headers=HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["order_created"] is False
    assert response.json()["vantage_mt5_allowed"] is False
    rejected = client.post("/api/listed-assets/routing-policy", json={
        "symbol": "AAPL", "exchange": "NASDAQ", "currency": "USD",
        "product_type": "SHARE_CFD",
    }, headers=HEADERS)
    assert rejected.status_code == 422
    assert client.app.state.fake_mt5.send_calls == []


def test_listed_policy_payload_contains_no_secret_or_profitability_claim(client):
    body = client.get("/api/listed-assets/status", headers=HEADERS).json()
    encoded = json.dumps(body).lower()
    for forbidden in ("password", "api_secret", "ciphertext", "profitab"):
        assert forbidden not in encoded
    assert body["setup_document"] == "docs/LISTED_ASSETS.md"
