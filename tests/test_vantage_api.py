import json

import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.config.settings import Settings
from app.vantage.models import SessionMode
from app.vantage.secrets import MemorySecretStore
from tests.mt5_fake import FakeMT5Gateway
from tests.test_vantage_connector import RISK


TOKEN = "local-api-test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


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


def credential(fake, password="fake-api-password"):
    return {
        "environment": "VANTAGE_DEMO", "login": str(fake.login_value),
        "password": password, "server": fake.server, "terminal_path": "",
        "label": "API fake demo", "replace_existing": False,
    }


def connect(client):
    fake = client.app.state.fake_mt5
    response = client.put("/api/trading/credentials", json=credential(fake), headers=HEADERS)
    assert response.status_code == 200, response.text
    assert client.post("/api/trading/session", json={"mode": "VANTAGE_DEMO"}, headers=HEADERS).status_code == 200
    response = client.post("/api/trading/connection/test", headers=HEADERS)
    assert response.status_code == 200, response.text
    assert client.put("/api/trading/risk", json=RISK, headers=HEADERS).status_code == 200
    response = client.put("/api/trading/instruments/selection", json={"symbols": ["EURUSD"]}, headers=HEADERS)
    assert response.status_code == 200, response.text


def activate(client):
    readiness = client.get("/api/trading/readiness", headers=HEADERS).json()
    assert readiness["ready"], readiness["blockers"]
    armed = client.post("/api/trading/arm", json={
        "readiness_id": readiness["readiness_id"], "phrase": readiness["arm_phrase"],
    }, headers=HEADERS)
    assert armed.status_code == 200, armed.text
    body = armed.json()
    activated = client.post("/api/trading/activate", json={
        "activation_id": body["activation_id"], "phrase": body["activation_phrase"],
    }, headers=HEADERS)
    assert activated.status_code == 200, activated.text


def order_payload(key="api-order-000001"):
    return {
        "symbol": "EURUSD", "side": "BUY", "order_type": "MARKET", "volume": 0.1,
        "idempotency_key": key,
    }


def test_all_trading_apis_require_bearer_and_paper_is_default(client):
    assert client.get("/api/trading/status").status_code == 401
    assert client.get("/api/trading/overview").status_code == 401
    status = client.get("/api/trading/status", headers=HEADERS)
    assert status.status_code == 200
    assert status.json()["mode"] == "AXIOM_PAPER"
    overview = client.get("/api/trading/overview", headers=HEADERS).json()
    assert overview["paper_only"] is True and overview["simulated"] is True
    assert overview["status"]["automation_enabled"] is False


def test_validation_and_credential_responses_never_echo_secrets(client):
    sensitive = "x" * 600
    response = client.put("/api/trading/credentials", json=credential(client.app.state.fake_mt5, sensitive), headers=HEADERS)
    assert response.status_code == 422
    assert sensitive not in response.text
    assert "input" not in response.text
    fake = client.app.state.fake_mt5
    response = client.put("/api/trading/credentials", json=credential(fake), headers=HEADERS)
    assert response.status_code == 200, response.text
    public = response.text + client.get("/api/trading/connection", headers=HEADERS).text
    assert "fake-api-password" not in public
    assert '"123456"' not in public
    assert "••••56" in public


def test_complete_fake_demo_order_through_http_api(client):
    connect(client)
    activate(client)
    payload = order_payload()
    review = client.post("/api/trading/orders/review", json=payload, headers=HEADERS)
    assert review.status_code == 200, review.text
    review_body = review.json()
    assert review_body["risk"]["allowed"] is True
    assert review_body["account"]["login_masked"] == "••••56"
    assert review_body["instrument"]["product_type"] == "FOREX"
    submitted = client.post("/api/trading/orders/submit", json={
        **payload, "review_id": review_body["review_id"],
        "confirmation": review_body["confirmation_phrase"],
    }, headers=HEADERS)
    assert submitted.status_code == 200, submitted.text
    result = submitted.json()
    assert result["order"]["status"] == "FILLED"
    assert result["reconciled"] is True
    duplicate = client.post("/api/trading/orders/submit", json={
        **payload, "review_id": "invalid-review", "confirmation": "wrong",
    }, headers=HEADERS)
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True
    assert len(client.app.state.fake_mt5.send_calls) == 1
    workspace = client.get("/api/trading/workspace", headers=HEADERS).json()
    assert workspace["positions"][0]["volume"] == pytest.approx(0.1)
    assert workspace["deals"]


def test_vantage_mode_disables_legacy_paper_entry(client):
    connect(client)
    response = client.post("/paper/order", json={
        "symbol": "DEMO", "side": "BUY", "quantity": 1, "order_type": "MARKET",
    }, headers=HEADERS)
    assert response.status_code == 409
    assert "Vantage" in response.text
    response = client.post("/api/paper/start-default", headers=HEADERS)
    assert response.status_code == 409
    switched = client.post("/api/trading/session", json={"mode": "AXIOM_PAPER"}, headers=HEADERS)
    assert switched.status_code == 200
    assert switched.json()["paper_only"] is True


def test_emergency_stop_api_blocks_and_clear_does_not_activate(client):
    connect(client)
    activate(client)
    stopped = client.post("/api/trading/emergency", headers=HEADERS)
    assert stopped.status_code == 200
    assert stopped.json()["status"]["emergency_stop"] is True
    payload = order_payload("api-emergency-001")
    review = client.post("/api/trading/orders/review", json=payload, headers=HEADERS)
    assert review.status_code == 200
    assert review.json()["risk"]["allowed"] is False
    submitted = client.post("/api/trading/orders/submit", json={
        **payload, "review_id": review.json()["review_id"], "confirmation": "SUBMIT DEMO ORDER",
    }, headers=HEADERS)
    assert submitted.status_code == 409
    assert client.app.state.fake_mt5.send_calls == []
    cleared = client.post("/api/trading/emergency/clear", json={"phrase": "CLEAR EMERGENCY STOP"}, headers=HEADERS)
    assert cleared.status_code == 200
    assert cleared.json()["active"] is False and cleared.json()["armed"] is False


def test_wrong_review_or_confirmation_never_calls_mt5(client):
    connect(client)
    activate(client)
    payload = order_payload("api-review-guard-1")
    review = client.post("/api/trading/orders/review", json=payload, headers=HEADERS).json()
    response = client.post("/api/trading/orders/submit", json={
        **payload, "review_id": review["review_id"], "confirmation": "yes",
    }, headers=HEADERS)
    assert response.status_code == 409
    assert client.app.state.fake_mt5.send_calls == []


def test_connection_test_is_read_only_and_disconnect_disarms(client):
    connect(client)
    activate(client)
    response = client.post("/api/trading/connection/test", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["read_only"] is True
    assert response.json()["active"] is False
    assert client.app.state.fake_mt5.send_calls == []
    response = client.post("/api/trading/connection/disconnect", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["connected"] is False
    assert response.json()["lifecycle"] == "DISARMED"


def test_disconnected_vantage_workspace_remains_usable_and_not_flat(client):
    response = client.post(
        "/api/trading/session", json={"mode": "VANTAGE_DEMO"}, headers=HEADERS,
    )
    assert response.status_code == 200
    workspace = client.get("/api/trading/workspace", headers=HEADERS)
    assert workspace.status_code == 200, workspace.text
    assert workspace.json()["broker_state_available"] is False
    assert workspace.json()["reconciled_at"] is None
    assert "do not prove a flat account" in workspace.json()["note"]


def test_paper_emergency_stop_blocks_entry_until_explicit_clear(client):
    stopped = client.post("/api/trading/emergency", headers=HEADERS)
    assert stopped.status_code == 200
    response = client.post("/paper/order", json={
        "symbol": "DEMO", "side": "BUY", "quantity": 1, "order_type": "MARKET",
    }, headers=HEADERS)
    assert response.status_code == 409
    assert "emergency" in response.text.lower()
    assert client.app.state.simulator.status()["state"] == "STOPPED"
    cleared = client.post(
        "/api/trading/emergency/clear", json={"phrase": "CLEAR EMERGENCY STOP"}, headers=HEADERS,
    )
    assert cleared.status_code == 200
    assert cleared.json()["active"] is False and cleared.json()["armed"] is False


def test_position_modification_through_http_is_confirmed(client):
    connect(client)
    activate(client)
    payload = order_payload("http-modify-source")
    review = client.post("/api/trading/orders/review", json=payload, headers=HEADERS).json()
    submitted = client.post("/api/trading/orders/submit", json={
        **payload, "review_id": review["review_id"],
        "confirmation": review["confirmation_phrase"],
    }, headers=HEADERS)
    assert submitted.status_code == 200
    position = client.get("/api/trading/workspace", headers=HEADERS).json()["positions"][0]
    modification = {
        "position_id": position["position_id"], "stop_loss": 1.09,
        "take_profit": 1.12, "idempotency_key": "http-modify-0001",
    }
    reviewed = client.post(
        "/api/trading/positions/modify/review", json=modification, headers=HEADERS,
    )
    assert reviewed.status_code == 200, reviewed.text
    review_body = reviewed.json()
    result = client.post("/api/trading/positions/modify/submit", json={
        **modification, "review_id": review_body["review_id"],
        "confirmation": review_body["confirmation_phrase"],
    }, headers=HEADERS)
    assert result.status_code == 200, result.text
    assert result.json()["order"]["status"] == "MODIFIED"
    confirmed = client.get("/api/trading/workspace", headers=HEADERS).json()["positions"][0]
    assert confirmed["stop_loss"] == pytest.approx(1.09)
    assert confirmed["take_profit"] == pytest.approx(1.12)
