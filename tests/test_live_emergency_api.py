"""Authenticated emergency-stop UI/API wiring; these tests place no orders."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.config.settings import LIVE_CONFIRM_PHRASE, Settings


TOKEN = "fixture-emergency-api-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def _settings(database_url: str, *, live: bool = False) -> Settings:
    values = {
        "database_url": database_url,
        "api_token": TOKEN,
        "data_source": "generic",
    }
    if live:
        values.update({
            "mode": "LIVE", "live_trading": True,
            "broker": "UPSTOX_LIVE", "live_confirm": LIVE_CONFIRM_PHRASE,
            "upstox_live_access_token": "fixture-token-not-used",
            "max_position_value": 10_000.0, "max_open_positions": 2,
            "max_daily_loss": 5_000.0, "max_trades_per_day": 10,
        })
    return Settings(**values)


@pytest.fixture()
def client_factory(tmp_path):
    opened = []

    def create(*, live=False):
        settings = _settings(
            f"sqlite:///{tmp_path / ('live' if live else 'paper')}.db",
            live=live)
        test_client = TestClient(create_app(settings))
        test_client.__enter__()
        opened.append(test_client)
        return test_client

    yield create
    for test_client in reversed(opened):
        test_client.__exit__(None, None, None)


def test_emergency_stop_clear_requires_exact_phrase_and_never_rearms(client_factory):
    client = client_factory()
    stopped = client.post("/api/live/emergency-stop", headers=HEADERS,
                          json={"reason": "test stop"})
    assert stopped.status_code == 200
    assert stopped.json()["state"] == "BLOCKED"
    repository = client.app.state.repository
    assert repository.get_engine_state("emergency_stop")["stopped"] is True

    wrong = client.post("/api/live/emergency-stop/clear", headers=HEADERS,
                        json={"confirmation_phrase": "not the configured phrase"})
    assert wrong.status_code == 409
    assert repository.get_engine_state("emergency_stop")["stopped"] is True
    assert client.app.state.workstation.live_sessions.current().state == "BLOCKED"

    cleared = client.post("/api/live/emergency-stop/clear", headers=HEADERS,
                          json={"confirmation_phrase": LIVE_CONFIRM_PHRASE})
    assert cleared.status_code == 200
    assert cleared.json()["state"] == "STOPPED"
    assert cleared.json()["stopped"] is False
    assert "fresh ARM" in cleared.json()["note"]
    assert "explicit ACTIVATE" in cleared.json()["note"]
    assert repository.get_engine_state("emergency_stop")["stopped"] is False
    assert client.app.state.workstation.live_sessions.current().state == "STOPPED"


def test_legacy_resume_cannot_clear_a_live_stop_without_phrase(client_factory):
    client = client_factory(live=True)
    repository = client.app.state.repository
    assert client.post("/engine/stop", headers=HEADERS).status_code == 200
    assert repository.get_engine_state("emergency_stop")["stopped"] is True

    refused = client.post("/engine/resume", headers=HEADERS)
    assert refused.status_code == 409
    assert repository.get_engine_state("emergency_stop")["stopped"] is True

    cleared = client.post("/engine/resume", headers=HEADERS,
                          json={"confirmation_phrase": LIVE_CONFIRM_PHRASE})
    assert cleared.status_code == 200
    assert repository.get_engine_state("emergency_stop")["stopped"] is False
    assert cleared.json()["fresh_arm_required"] is True
    assert client.app.state.workstation.live_sessions.current().state == "OFF"
