"""3.1.0 — UI action matrix: every visible control has a working backend.

A button that does nothing is a defect; a button that crashes is a release
blocker. This matrix pins, control by control, that the endpoint behind each
redesigned-UI action exists AND produces the intended state change. The
frontend calls exactly these paths (see app/static/arena-app.js /
arena-wizard.js), so a failure here means a dead or broken control.
"""

from __future__ import annotations

import os
import time

import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.config.settings import Settings


@pytest.fixture()
def armed(tmp_path):
    """A fully prepared paper session: wizard done, engine stopped, token in hand."""
    saved = dict(os.environ)
    home = tmp_path / "ArenaHome"
    try:
        for key in ("DATABASE_URL", "DATA_DIR", "API_TOKEN", "BROKER", "MODE",
                    "MARKET_DATA_SOURCE", "LIVE_TRADING"):
            os.environ.pop(key, None)
        os.environ["ARENA_HOME"] = str(home)
        app = create_app(Settings.from_env())
        with TestClient(app) as client:
            token = client.post("/api/wizard/prepare-paper",
                                json={"data_source": "replay"}).json()["token_once"]
            yield client, token, app.state.repository
    finally:
        os.environ.clear()
        os.environ.update(saved)


def headers(token):
    return {"Authorization": "Bearer " + token}


MATRIX_METADATA = """
Every row below corresponds to a labeled control in the 3.1.0 interface.
The mapping (control -> endpoint) is what arena-app.js wires on click.
"""


class TestPaperControls:
    def test_start_paper_top_button(self, armed):
        client, token, _ = armed
        response = client.post("/api/paper/start-default",
                               json={"source": "replay", "replay_delay": 0},
                               headers=headers(token))
        assert response.status_code == 200
        assert response.json()["state"] in ("STARTING", "RUNNING")

    def test_stop_paper_button_from_running(self, armed):
        client, token, _ = armed
        client.post("/api/paper/start-default",
                    json={"source": "replay", "replay_delay": 0.05},
                    headers=headers(token))
        time.sleep(0.3)
        stopped = client.post("/paper/stop", headers=headers(token))
        assert stopped.status_code == 200
        assert stopped.json()["state"] == "STOPPED"

    def test_pause_and_resume_buttons(self, armed):
        client, token, _ = armed
        client.post("/api/paper/start-default",
                    json={"source": "replay", "replay_delay": 0.5},
                    headers=headers(token))
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if client.get("/paper/status",
                          headers=headers(token)).json()["state"] == "RUNNING":
                break
            time.sleep(0.1)
        paused = client.post("/paper/pause", headers=headers(token))
        assert paused.status_code == 200 and paused.json()["state"] == "PAUSED"
        resumed = client.post("/paper/resume", headers=headers(token))
        assert resumed.status_code == 200 and resumed.json()["state"] == "RUNNING"
        client.post("/paper/stop", headers=headers(token))

    def test_emergency_stop_and_resume_controls(self, armed):
        client, token, _ = armed
        stop = client.post("/engine/stop", headers=headers(token))
        assert stop.status_code == 200
        data = client.get("/dashboard/data", headers=headers(token)).json()
        assert data["emergency_stop"] is True
        resume = client.post("/engine/resume", headers=headers(token))
        assert resume.status_code == 200
        data = client.get("/dashboard/data", headers=headers(token)).json()
        assert data["emergency_stop"] is False


class TestViewsAndTables:
    def test_view_orders_trades_positions_account(self, armed):
        client, token, _ = armed
        for path in ("/paper/orders", "/paper/trades", "/paper/positions",
                     "/paper/account", "/paper/status"):
            assert client.get(path, headers=headers(token)).status_code == 200, path

    def test_view_risk_and_cockpit_data(self, armed):
        client, token, _ = armed
        data = client.get("/dashboard/data", headers=headers(token)).json()
        for key in ("mode", "broker_mode", "display", "alerts", "system_events"):
            assert key in data, f"cockpit must expose {key}"

    def test_view_logs_and_report(self, armed):
        client, token, _ = armed
        assert client.get("/api/v2/logs?limit=20",
                          headers=headers(token)).status_code == 200
        report = client.get("/api/v2/report?period=weekly&format=markdown",
                            headers=headers(token))
        assert report.status_code == 200 and report.text.strip()

    def test_alerts_view_and_acknowledge_button(self, armed):
        client, token, repository = armed
        from app.live.alerts import AlertRouter

        router = AlertRouter(repository, dedup_seconds=0)
        emitted = router.emit("TEST_KIND", "WARNING", "matrix-test alert")
        assert emitted is not None, "the test alert must be recorded"
        listing = client.get("/api/v2/alerts", headers=headers(token)).json()
        assert listing["alerts"], "the recorded alert must be visible"
        assert listing["unacknowledged_critical"] >= 0
        ack = client.post("/api/v2/alerts/ack", json={"all": True, "by": "matrix"},
                          headers=headers(token))
        assert ack.status_code == 200
        again = client.get("/api/v2/alerts", headers=headers(token)).json()
        assert all(a["acknowledged"] for a in again["alerts"])


class TestResearchControls:
    def test_backtest_run_button_and_saved_history(self, armed):
        client, token, _ = armed
        run = client.post("/backtests/run", json={
            "strategy": "moving_average", "parameters": {},
            "symbol": "DEMO", "timeframe": "1D", "dataset": "sample.csv"},
            headers=headers(token))
        assert run.status_code == 200, run.text
        assert run.json()["id"]
        listing = client.get("/backtests", headers=headers(token)).json()
        assert any(row["id"] == run.json()["id"] for row in listing["backtests"])
        detail = client.get("/backtests/" + run.json()["id"],
                            headers=headers(token)).json()
        assert detail["equity_curve"], "saved run keeps its full result"

    def test_validation_buttons_split_walk_forward_robustness(self, armed):
        client, token, _ = armed
        split = client.post("/api/research/validation",
                            json={"kind": "split"}, headers=headers(token))
        assert split.status_code == 200 and split.json()["sizes"]["test"] > 0
        walk = client.post("/api/research/validation",
                           json={"kind": "walk_forward"}, headers=headers(token))
        assert walk.status_code == 200, walk.text
        assert walk.json()["folds"] >= 1
        robust = client.post("/api/research/validation",
                             json={"kind": "robustness"}, headers=headers(token))
        assert robust.status_code == 200
        assert robust.json()["trials"] > 0

    def test_tournaments_and_experiments_views(self, armed):
        client, token, _ = armed
        assert client.get("/tournaments", headers=headers(token)).status_code == 200
        assert client.get("/api/v2/experiments",
                          headers=headers(token)).status_code == 200

    def test_strategy_catalog_for_the_form(self, armed):
        client, token, _ = armed
        listing = client.get("/strategies", headers=headers(token)).json()
        names = [entry["name"] for entry in listing["strategies"]]
        assert "moving_average" in names and "momentum" in names


class TestSettingsControls:
    def test_save_button_writes_and_reports_restart(self, armed):
        client, token, _ = armed
        saved = client.post("/setup/save",
                            json={"values": {"DEFAULT_SYMBOL": "DEMO"}})
        assert saved.status_code == 200
        assert saved.json()["ok"] is True
        assert saved.json()["restart_required"] is True

    def test_reload_status_button(self, armed):
        client, token, _ = armed
        status = client.get("/setup/status")
        assert status.status_code == 200
        assert "present" in status.json()

    def test_market_data_test_button_rejects_garbage_offline_or_online(self, armed):
        client, token, _ = armed
        response = client.post("/api/wizard/validate-market-data",
                               json={"token": "not-a-real-token",
                                     "symbols": "NSE_EQ|INE002A01018"})
        # Whatever the network state, a fake credential must not validate OK.
        assert response.status_code in (422, 500) or response.json().get("ok") is not True

    def test_market_events_ingest_and_listing(self, armed):
        client, token, _ = armed
        ingested = client.post("/api/market-events/ingest", json={
            "headline": "Matrix test event for Arena",
            "summary": "synthetic event for the UI action matrix",
            "source": "unit-test"}, headers=headers(token))
        assert ingested.status_code == 200, ingested.text
        listing = client.get("/api/market-events", headers=headers(token))
        assert listing.status_code == 200

    def test_system_view_endpoints(self, armed):
        client, token, _ = armed
        info = client.get("/api/system/info", headers=headers(token))
        assert info.status_code == 200
        assert info.json()["writer_lock"] == "held by this session"
        instance = client.get("/api/instance")
        assert instance.status_code == 200
        assert instance.json()["this_pid"]

    def test_live_readiness_view(self, armed):
        client, token, _ = armed
        readiness = client.get("/api/live/readiness", headers=headers(token))
        assert readiness.status_code == 200
        assert readiness.json()["enabled"] is False

    def test_console_catalog_for_the_developer_link(self, armed):
        client, token, _ = armed
        catalog = client.get("/console/catalog", headers=headers(token))
        assert catalog.status_code == 200
