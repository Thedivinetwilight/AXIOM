"""V1: dashboard endpoints — auth, mode badges, emergency STOP/RESUME."""

import pytest
from fastapi.testclient import TestClient

from app import __version__
from app.api.server import create_app
from app.config.settings import Settings
from app.database.database import Database
from app.database.repository import Repository


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/dash.db")
    monkeypatch.setenv("API_TOKEN", "dash-token-123")
    monkeypatch.setenv("DATA_SOURCE", "generic")
    settings = Settings.from_env()
    app = create_app(settings)
    database = Database(settings.database_url)
    app.state.repository = Repository(database)
    with TestClient(app) as test_client:
        yield test_client
    database.close()


H = {"Authorization": "Bearer dash-token-123"}


class TestDashboardAuthAndBadges:
    def test_data_requires_token(self, client):
        assert client.get("/dashboard/data").status_code == 401

    def test_paper_mode_displayed_by_default(self, client):
        data = client.get("/dashboard/data", headers=H).json()
        assert data["broker_mode"] == "LOCAL_PAPER"
        assert data["mode"] == "PAPER"
        assert data["live_trading"] is False
        assert data["live_enabled"] == "NOT ENABLED IN V1"
        assert "PAPER" in data["display"]
        assert data["version"] == __version__

    def test_html_page_is_legacy_paper_only_and_links_to_operator(self, client):
        response = client.get("/dashboard", headers=H)
        assert response.status_code == 200
        assert "LEGACY PAPER/SANDBOX ONLY" in response.text
        assert "VANTAGE OPERATOR" in response.text
        assert 'href="/terminal"' in response.text
        assert "EMERGENCY STOP" in response.text

    def test_sandbox_mode_displayed_when_configured(self, client, tmp_path, monkeypatch):
        monkeypatch.setenv("BROKER", "UPSTOX_SANDBOX")
        monkeypatch.setenv("UPSTOX_SANDBOX_ACCESS_TOKEN", "sbx-token")
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/dash_sandbox.db")
        settings = Settings.from_env()
        app = create_app(settings)
        database = Database(settings.database_url)
        app.state.repository = Repository(database)
        with TestClient(app) as tc:
            data = tc.get("/dashboard/data", headers=H).json()
            assert data["broker_mode"] == "UPSTOX_SANDBOX"
            assert data["mode"] == "PAPER"
            assert "SANDBOX" in data["display"]
        database.close()


class TestEmergencyControls:
    def test_stop_persists_flag_and_records_event(self, client):
        body = client.post("/engine/stop", headers=H).json()
        assert body["stopped"] is True
        data = client.get("/dashboard/data", headers=H).json()
        assert data["emergency_stop"] is True
        kinds = [e["kind"] for e in data["system_events"]]
        assert "EMERGENCY_STOP_REQUESTED" in kinds

    def test_resume_clears_flag(self, client):
        client.post("/engine/stop", headers=H)
        body = client.post("/engine/resume", headers=H).json()
        assert body["stopped"] is False
        data = client.get("/dashboard/data", headers=H).json()
        assert data["emergency_stop"] is False

    def test_stop_blocks_engine_after_restart_of_view(self, client):
        client.post("/engine/stop", headers=H)
        # Simulate an engine consulting the persisted flag afterwards.
        repository = client.app.state.repository
        assert repository.get_engine_state("emergency_stop", {}).get("stopped") is True
