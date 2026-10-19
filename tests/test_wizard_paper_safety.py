"""The paper wizard is validate-before-write and rolls back on failure.

The 3.2.4 startup defect was not only a message problem: `prepare-paper`
wrote `.env` and closed the old broker *before* discovering the change could
not be applied. These tests drive the real FastAPI app and assert the
invariants the repair promises:

* a refused change leaves `.env` byte-identical;
* the previously running paper broker is still usable;
* the in-process settings are not replaced by a change that failed.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.config.settings import Settings

KEY = "NSE_EQ|INE002A01018"
ENV_KEYS = ["ARENA_HOME", "DATABASE_URL", "API_TOKEN", "BROKER", "MODE",
            "DATA_SOURCE", "DEFAULT_TIMEFRAME", "DEFAULT_SYMBOL",
            "MARKET_DATA_ACCESS_TOKEN", "MARKET_DATA_SYMBOLS", "LIVE_TRADING",
            "LIVE_CONFIRM", "UPSTOX_ACCESS_TOKEN", "MARKET_DATA_TIMEFRAME"]


@pytest.fixture()
def fresh_home(tmp_path, monkeypatch):
    saved = dict(os.environ)
    home = tmp_path / "ArenaHome"
    try:
        for key in ENV_KEYS:
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("ARENA_HOME", str(home))
        yield home
    finally:
        os.environ.clear()
        os.environ.update(saved)


class TestRebuildFailureRollsBack:
    def test_env_and_engine_survive_a_failed_rebuild(self, fresh_home, monkeypatch):
        from app.api.routes import wizard

        async def ok_probe(token):
            from types import SimpleNamespace
            return SimpleNamespace(verdict="feed_ok", guidance="ok")

        monkeypatch.setattr(wizard, "_probe_market_data", ok_probe)

        from app.paper_trading import broker as broker_module
        real_broker = broker_module.PaperBroker
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:  # the NEW broker fails to open
                raise OSError("disk full (simulated)")
            return real_broker(*args, **kwargs)  # restore path

        with TestClient(create_app(Settings.from_env())) as client:
            before_settings = client.app.state.settings
            before_broker = client.app.state.broker
            monkeypatch.setattr(broker_module, "PaperBroker", flaky)
            try:
                response = client.post("/api/wizard/prepare-paper", json={
                    "data_source": "upstox",
                    "market_data_access_token": "test-token",
                    "market_data_symbols": KEY,
                })
            finally:
                monkeypatch.setattr(broker_module, "PaperBroker", real_broker)

            assert response.status_code == 409, response.text
            message = response.json()["detail"]
            assert "Nothing was saved" in message
            assert "previous paper engine was restored" in message
            assert "disk full" in message

            # .env never created / never modified
            env_path = fresh_home / ".env"
            assert not env_path.exists() or env_path.read_bytes() == b""

            # The running broker was replaced by a reopened one, not left closed.
            assert client.app.state.broker is not before_broker
            assert client.app.state.broker.repository is not None
            assert client.app.state.simulator.broker is client.app.state.broker
            assert client.app.state.settings is before_settings

    def test_unrestorable_engine_is_reported_honestly(self, fresh_home, monkeypatch):
        from app.api.routes import wizard
        from app.paper_trading import broker as broker_module

        async def ok_probe(token):
            from types import SimpleNamespace
            return SimpleNamespace(verdict="feed_ok", guidance="ok")

        monkeypatch.setattr(wizard, "_probe_market_data", ok_probe)

        def always_fails(*args, **kwargs):
            raise OSError("cannot open database file (simulated)")

        with TestClient(create_app(Settings.from_env())) as client:
            monkeypatch.setattr(broker_module, "PaperBroker", always_fails)
            response = client.post("/api/wizard/prepare-paper", json={
                "data_source": "upstox",
                "market_data_access_token": "test-token",
                "market_data_symbols": KEY,
            })
            assert response.status_code == 409, response.text
            message = response.json()["detail"]
            assert "Nothing was saved" in message
            assert "could NOT be restored" in message
            assert not (fresh_home / ".env").exists()
