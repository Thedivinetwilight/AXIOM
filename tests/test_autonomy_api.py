from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.server import create_app
from app.config.settings import Settings


def client(tmp_path):
    settings = replace(Settings(), api_token="local-api-test", database_url=f"sqlite:///{tmp_path}/app.db")
    return TestClient(create_app(settings, data_dir=tmp_path))


def test_autonomy_page_status_and_manual_are_local_and_credentialed(tmp_path):
    with client(tmp_path) as web:
        assert web.get("/api/autonomy/status").status_code == 401
        headers = {"Authorization": "Bearer local-api-test"}
        status = web.get("/api/autonomy/status", headers=headers)
        assert status.status_code == 200
        assert status.json()["automatic_arm_or_activate"] is False
        assert status.json()["profitability_claim"] == "none"
        page = web.get("/autonomy")
        assert page.status_code == 200
        assert "PAPER" in page.text and "UPSTOX_LIVE" in page.text
        assert "/static/autonomy/autonomy.js" in page.text
        assert "/static/vendor/lightweight-charts/" in page.text
        assert "https://" not in page.text
        manual = web.get("/autonomy/manual")
        assert manual.status_code == 200
        assert "not a live-readiness certificate" in manual.text
        script = web.get("/static/autonomy/autonomy.js")
        assert script.status_code == 200
        assert "fetch('/api/autonomy'" in script.text
        assert "localhost" not in script.text and "127.0.0.1" not in script.text


def test_credential_fields_are_rejected_and_never_echoed(tmp_path):
    with client(tmp_path) as web:
        headers = {"Authorization": "Bearer local-api-test"}
        secret = "not-a-real-token-but-must-not-be-echoed"
        response = web.post("/api/autonomy/sessions", headers=headers, json={
            "mode": "PAPER", "instrument_keys": ["NSE_EQ|INE000000000"],
            "access_token": secret,
        })
        assert response.status_code == 422
        assert secret not in response.text
        assert response.json()["detail"][0]["loc"][-1] == "access_token"


def test_no_generic_order_or_credential_api_is_exposed(tmp_path):
    with client(tmp_path) as web:
        schema = web.get("/openapi.json").json()
        paths = list(schema["paths"])
        assert "/api/autonomy/sessions/{session_id}/activate" in paths
        assert "/api/autonomy/sessions/{session_id}/arm" in paths
        assert "/api/autonomy/sessions/{session_id}/reconcile" in paths
        assert not any("place-order" in path or "token" in path.rsplit("/", 1)[-1]
                       for path in paths if path.startswith("/api/autonomy"))
        health = web.get("/health").json()
        assert health["mode"] == "PAPER" and health["live_trading"] is False
        assert health["autonomy"]["live_active"] is False
