"""3.1.0 — paper can never become live, silently or otherwise.

Mode separation regressions pinned here:

* the wizard backend refuses live enablement keys and live broker values
  (defense in depth on top of /setup/save, which already refuses them);
* after a zero-config paper setup, /health still reports simulated routing;
* the one-click paper start cannot select a live or sandbox source;
* LOCAL_PAPER never constructs a path toward a live order host;
* the live rail stays LOCKED in the readiness view of a paper install.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.api.routes import wizard as wizard_routes
from app.config.envfile import read_values
from app.config.settings import Settings


@pytest.fixture()
def fresh_home(tmp_path):
    saved = dict(os.environ)
    home = tmp_path / "ArenaHome"
    try:
        for key in ("DATABASE_URL", "DATA_DIR", "API_TOKEN", "BROKER", "MODE",
                    "MARKET_DATA_SOURCE", "LIVE_TRADING", "LIVE_CONFIRM"):
            os.environ.pop(key, None)
        os.environ["ARENA_HOME"] = str(home)
        yield home
    finally:
        os.environ.clear()
        os.environ.update(saved)


class TestWizardCannotReachLive:
    def test_prepare_paper_payload_rejects_any_source_but_replay_or_upstox(self, fresh_home):
        with TestClient(create_app(Settings.from_env())) as client:
            for source in ("live", "upstox_live", "binance", "nonsense"):
                response = client.post("/api/wizard/prepare-paper",
                                       json={"data_source": source})
                assert response.status_code == 422, source

    def test_the_wizard_write_list_cannot_carry_live_switches(self, fresh_home):
        # Structural check: the wizard's own code paths never include a live
        # key in what they persist.
        import inspect

        source = inspect.getsource(wizard_routes)
        assert "LIVE_TRADING" in source  # the refusal set is defined…
        assert "updates[\"LIVE_TRADING\"]" not in source  # …never as a write

    def test_after_zero_config_paper_health_reports_simulated_routing(self, fresh_home):
        with TestClient(create_app(Settings.from_env())) as client:
            client.post("/api/wizard/prepare-paper", json={"data_source": "replay"})
            health = client.get("/health").json()
            assert health["real_order_routing"] == "SIMULATED_LOCAL_PAPER"
            assert health["live_trading"] is False
            assert health["broker"] == "LOCAL_PAPER"
            assert health["warning"] is None

    def test_start_default_rejects_non_paper_sources(self, fresh_home):
        with TestClient(create_app(Settings.from_env())) as client:
            token = client.post("/api/wizard/prepare-paper",
                                json={"data_source": "replay"}).json()["token_once"]
            headers = {"Authorization": "Bearer " + token}
            response = client.post("/api/paper/start-default",
                                   json={"source": "binance"}, headers=headers)
            assert response.status_code == 422

    def test_paper_broker_is_not_a_live_execution_path(self):
        from app.paper_trading.broker import FutureRealBroker

        # Either the abstract-class machinery or the explicit guard must make
        # instantiation impossible — both are fail-closed outcomes.
        with pytest.raises((RuntimeError, TypeError)):
            FutureRealBroker()


class TestLiveRailStaysLocked:
    def test_readiness_reports_locked_on_a_paper_install(self, fresh_home):
        with TestClient(create_app(Settings.from_env())) as client:
            token = client.post("/api/wizard/prepare-paper",
                                json={"data_source": "replay"}).json()["token_once"]
            headers = {"Authorization": "Bearer " + token}
            readiness = client.get("/api/live/readiness", headers=headers).json()
            assert readiness["enabled"] is False
            switches = next(c for c in readiness["checks"]
                            if c["name"] == "four_switch_opt_in")
            assert switches["ok"] is False
            assert "missing" in switches["detail"]

    def test_setup_save_still_refuses_live_keys_after_the_redesign(self, fresh_home):
        with TestClient(create_app(Settings.from_env())) as client:
            response = client.post("/setup/save", json={
                "values": {"LIVE_TRADING": "true", "BROKER": "UPSTOX_LIVE"}})
            # refused keys with nothing accepted -> 400 by design
            assert response.status_code == 400
            response = client.post("/setup/save", json={
                "values": {"MODE": "LIVE"}})
            assert response.status_code == 400
