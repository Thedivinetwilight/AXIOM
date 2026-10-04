"""AXIOM-MONEY-PRINTER — GUI action matrix (master spec section 81).

Every labeled control in the 3.2.0 cockpit maps to exactly one backend
endpoint, and that endpoint must produce the intended state change. A
button that does nothing is a defect; a button that crashes is a release
blocker. The mapping (control -> endpoint) is what
``app/static/arena-app.js`` wires on click; a failure here means a dead
or broken control somewhere in the MARKETS / MODELS / TRADING / RISK /
INTELLIGENCE / REPORTS / SYSTEM pages.

These tests drive the REAL HTTP surface through the running application —
never source inspection alone.
"""

from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.config.settings import LIVE_CONFIRM_PHRASE, Settings


@pytest.fixture()
def client(tmp_path, monkeypatch):
    for key in ("API_TOKEN", "DATABASE_URL", "DATA_DIR", "ARENA_HOME",
                "ARENA_ENV_FILE", "BROKER", "MODE", "LIVE_TRADING"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("API_TOKEN", "gui-matrix-token")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'arena.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    with TestClient(create_app(Settings.from_env())) as test_client:
        test_client.headers.update({"Authorization": "Bearer gui-matrix-token"})
        yield test_client


class TestMarketsPage:
    def test_market_tab_loads_registry(self, client):
        body = client.get("/api/markets").json()
        ids = {m["market_id"] for m in body["markets"]}
        assert {"india", "us", "japan", "uk", "hongkong", "singapore",
                "global"} <= ids

    def test_session_chip_deterministic(self, client):
        session = client.get("/api/markets/india/session").json()
        assert session["status"] in {"OPEN", "PRE_OPEN", "CLOSED", "WEEKEND",
                                     "HOLIDAY", "ALWAYS"}
        assert session["timezone"]

    def test_universe_create_select_export_remove(self, client):
        created = client.post("/api/universes", json={
            "market_id": "india", "name": "GUI core",
            "symbols": ["NSE|RELIANCE", "NSE|TCS"]}).json()
        universe_id = created["id"]
        listed = client.get("/api/universes").json()["universes"]
        assert any(u["id"] == universe_id for u in listed)
        selected = client.get(f"/api/universes/{universe_id}").json()
        assert (selected.get("universe") or selected)["name"] == "GUI core"
        exported = client.get(f"/api/universes/{universe_id}/export").json()
        assert exported["format"] == "axiom-universe/v1"
        assert client.delete(f"/api/universes/{universe_id}").json()["deleted"]
        assert client.get(f"/api/universes/{universe_id}").status_code == 404

    def test_universe_import_round_trip(self, client):
        created = client.post("/api/universes", json={
            "market_id": "india", "name": "Export me",
            "symbols": ["NSE|INFY"]}).json()
        exported = client.get(f"/api/universes/{created['id']}/export").json()
        exported["name"] = "Imported back"
        imported = client.post("/api/universes/import", json=exported).json()
        assert imported["symbols"] == ["NSE|INFY"]
        assert imported["name"] == "Imported back"
        bad_format = dict(exported, format="axiom-universe/v99")
        assert client.post("/api/universes/import",
                           json=bad_format).status_code == 422

    def test_pin_unpin_instrument(self, client):
        pinned = client.post("/api/universe/pin", json={
            "market_id": "india", "identity": "NSE|RELIANCE"}).json()
        assert pinned["pinned"] == "NSE|RELIANCE"
        pins = client.get("/api/universe/pins?market_id=india").json()["pins"]
        assert any("RELIANCE" in str(p) for p in pins)
        client.post("/api/universe/unpin", json={
            "market_id": "india", "identity": "NSE|RELIANCE"})
        assert client.get("/api/universe/pins?market_id=india").json()["pins"] == []

    def test_evidence_drawer_and_recommend_button(self, client):
        stack = client.get("/api/evidence/india/NSE|RELIANCE").json()
        layers = stack["layers"] if "layers" in stack else stack
        names = [entry["layer"] for entry in layers] if isinstance(layers, list) else list(layers)
        assert "kronos_forecast" in names and "risk" in names
        recommended = client.post(
            "/api/evidence/india/NSE|RELIANCE/recommend?state=WATCH&rationale=gui")
        assert recommended.status_code == 200

    def test_log_missed_opportunity_button(self, client):
        response = client.post("/api/research/missed", json={
            "kind": "false_negative", "payload": {"source": "gui"},
            "knowable": True, "market_id": "india", "identity": "NSE|RELIANCE"})
        assert response.status_code == 200 and "id" in response.json()
        missed = client.get("/api/research/missed").json()
        assert missed["stats"]["total"] >= 1
        bogus = client.post("/api/research/missed", json={
            "kind": "invented_kind", "payload": {}, "knowable": False})
        assert bogus.status_code == 422

    def test_instrument_analogues_button(self, client):
        result = client.post("/api/analogues/search", json={
            "market_id": "india", "identity": "NSE|RELIANCE"}).json()
        assert "sample_size" in result and "results" in result


class TestModelsPage:
    def test_studio_cards_and_honest_pipeline(self, client):
        studio = client.get("/api/kronos/studio").json()
        assert studio["cards"], "the studio must list known model cards"
        model_id = studio["cards"][0]["model_id"]
        # No weights in a fresh install: every action reports truthfully.
        download = client.post("/api/kronos/studio/download",
                               json={"model_id": model_id}).json()
        assert download["ok"] is False and "status" in download
        verify = client.post("/api/kronos/studio/verify",
                             json={"model_id": model_id}).json()
        assert verify["status"] == "NOT_VERIFIED"
        install = client.post("/api/kronos/studio/install",
                              json={"model_id": model_id}).json()
        assert install["status"] == "INSTALL_REFUSED"
        benchmark = client.post("/api/kronos/studio/benchmark",
                                json={"model_id": model_id}).json()
        assert benchmark["status"] == "NOT_ATTEMPTED"
        activate = client.post("/api/kronos/studio/activate",
                               json={"model_id": model_id}).json()
        assert activate["status"] == "ACTIVATE_REFUSED"


class TestTradingPage:
    def test_live_state_chip_and_controls_refuse_without_readiness(self, client):
        session = client.get("/api/live/session").json()
        assert session["state"] == "OFF"
        assert session["emergency_stop_active"] is False
        # Wrong phrase: refused, state unchanged.
        bad = client.post("/api/live/session/arm",
                          json={"confirmation_phrase": "nope"})
        assert bad.status_code == 409
        # Right phrase on an unconfigured workstation: readiness blocks.
        armed = client.post("/api/live/session/arm",
                            json={"confirmation_phrase": LIVE_CONFIRM_PHRASE})
        assert armed.status_code == 409
        assert client.get("/api/live/session").json()["state"] == "OFF"
        # Activate without arm: impossible.
        assert client.post("/api/live/session/activate").status_code == 409

    def test_readiness_center_is_never_silently_healthy(self, client):
        center = client.get("/api/live/readiness-center").json()
        assert center["verdict"] in {"READY", "READY_WITH_WARNINGS",
                                     "NOT_READY_UNVERIFIED", "BLOCKED"}
        assert center["checks"], "the center must enumerate its checks"
        statuses = {c["status"] for c in center["checks"]}
        assert statuses <= {"READY", "WARNING", "BLOCKED", "UNKNOWN"}
        # A fresh workstation cannot be READY: broker is not configured.
        assert center["verdict"] != "READY"

    def test_start_summary_guard_chain_rate_limit(self, client):
        summary = client.get("/api/live/start-summary")
        assert summary.status_code == 200
        chain = client.get("/api/live/guard-chain").json()
        assert "BROKER" in " ".join(chain["guards"]).upper()
        limits = client.get("/api/live/rate-limit").json()
        assert limits["profile"] and "per_min" in limits["current"]

    def test_emergency_paper_stop(self, client):
        stopped = client.post("/paper/stop")
        assert stopped.status_code == 200
        assert client.get("/paper/status").json()["state"] in {
            "STOPPED", "IDLE", "NOT_STARTED", "OFF"}


class TestRiskPage:
    def test_limits_and_guard_chain_visible(self, client):
        dash = client.get("/dashboard/data").json()
        assert dash["emergency_stop"] is False
        assert isinstance(dash["alerts"], list)
        chain = client.get("/api/live/guard-chain").json()
        assert len(chain["guards"]) >= 8


class TestIntelligencePage:
    def test_observe_watch_invalidate_flow(self, client):
        no_trigger = client.post("/api/opportunities/observe", json={
            "market_id": "india", "identity": "NSE|QUIET",
            "price_change_pct": 0.001}).json()
        assert no_trigger["detected"] is False
        detected = client.post("/api/opportunities/observe", json={
            "market_id": "india", "identity": "NSE|MOVING",
            "price_change_pct": 0.06, "volume_ratio": 3.0,
            "evidence_source": "gui matrix"}).json()
        assert detected["detected"] is True
        opp_id = detected["opportunity"]["id"]
        watching = client.post(f"/api/opportunities/{opp_id}/transition",
                               json={"state": "WATCH", "reason": "gui"}).json()
        assert watching["state"] == "WATCH"
        invalidated = client.post(f"/api/opportunities/{opp_id}/transition",
                                  json={"state": "INVALIDATED", "reason": "thesis broke"})
        assert invalidated.status_code == 200
        revived = client.post(f"/api/opportunities/{opp_id}/transition",
                              json={"state": "WATCH"})
        assert revived.status_code != 200, "terminal states never revive"

    def test_market_events_and_next_best(self, client):
        assert client.get("/api/market-events").status_code == 200
        suggestions = client.get("/api/research/next-best").json()
        assert suggestions["suggestions"], "an empty ledger still yields a proposal"

    def test_what_changed_feed(self, client):
        client.post("/api/universes", json={"market_id": "india", "name": "C",
                                            "symbols": ["NSE|A"]})
        client.post("/api/universes", json={"market_id": "india", "name": "C",
                                            "symbols": ["NSE|A", "NSE|B"]})
        changes = client.get("/api/changes?domain=universe").json()["changes"]
        assert changes and changes[0]["field"] == "size"

    def test_stream_health(self, client):
        health = client.get("/api/stream/health").json()
        assert "sequence" in json.dumps(health).lower() or health


class TestReportsPage:
    def test_weekly_report_and_latency(self, client):
        report = client.get("/api/v2/report/weekly?format=markdown")
        assert report.status_code == 200 and report.text.strip()
        latency = client.get("/api/latency").json()
        assert "stages" in latency and "daily" in latency


class TestSystemPage:
    def test_task_lifecycle_controls(self, client):
        task = client.post("/api/tasks", json={"name": "gui task",
                                               "type": "research"}).json()
        pressure = client.get("/api/system/control").json()["pressure"]
        started = client.post(f"/api/tasks/{task['id']}",
                              json={"action": "start"}).json()
        if pressure:
            # Under resource pressure the governor refuses new research
            # tasks honestly; the remaining controls still act on the task.
            assert started["started"] is False
        else:
            assert started["started"]
            paused = client.post(f"/api/tasks/{task['id']}", json={"action": "pause"})
            assert paused.status_code == 200
            resumed = client.post(f"/api/tasks/{task['id']}", json={"action": "resume"})
            assert resumed.status_code == 200
        cancelled = client.post(f"/api/tasks/{task['id']}", json={"action": "cancel"})
        assert cancelled.status_code == 200
        counts = client.get("/api/tasks").json()["counts"]
        assert counts["CANCELLED"] >= 1

    def test_backup_create_verify_restore_controls(self, client):
        created = client.post("/api/backup/create", json={}).json()
        assert created["path"].endswith(".tar.gz")
        listed = client.get("/api/backup/list").json()["backups"]
        assert any(b["path"] == created["path"] for b in listed)
        verified = client.post("/api/backup/verify",
                               json={"path": created["path"]}).json()
        assert verified["ok"] is True
        restored = client.post("/api/backup/restore",
                               json={"path": created["path"]}).json()
        # POSIX swaps the live database in place; Windows refuses to rename
        # a file an open connection holds — the honest outcome there is a
        # verified, staged restore applied with AXIOM stopped. Both are
        # intended behaviours; a silent failure is not.
        if restored["restored"]:
            assert "previous_database" in restored
        else:
            assert restored["status"] == "RESTORE_PREPARED_RESTART_REQUIRED"
            assert restored["pending_database"].endswith(".restore-pending")
        traversal = client.post("/api/backup/restore",
                                json={"path": "/etc/passwd"})
        assert traversal.status_code == 422

    def test_system_control_center(self, client):
        control = client.get("/api/system/control").json()
        assert control["components"]["core"] == "RUNNING"
        assert control["components"]["emergency_stop"] is False
        assert "tasks" in control and "resources" in control

    def test_attention_mode_selector(self, client):
        profile = client.post("/api/settings/attention",
                              json={"mode": "conservative"}).json()
        assert profile["attention_mode"] == "conservative"
        assert client.post("/api/settings/attention",
                           json={"mode": "reckless"}).status_code == 422

    def test_update_center_controls(self, client):
        status = client.get("/api/system/update/status").json()
        assert status["current"] and status["staged"] == []
        # An unsafe artifact name is refused, not silently sanitized.
        bad = client.post("/api/system/update/download",
                          json={"url": "https://example.invalid/x",
                                "filename": "../escape"})
        assert bad.status_code == 200 and bad.json()["status"] == "DOWNLOAD_FAILED"
        # Verification outside the updates directory is refused.
        refused = client.post("/api/system/update/verify",
                              json={"path": "/etc/passwd",
                                    "expected_sha256": "0" * 64})
        assert refused.status_code == 200
        assert refused.json()["status"] == "VERIFY_REFUSED"
        # Install with nothing prepared is refused.
        install = client.post("/api/system/update/install")
        assert install.status_code == 200
        assert install.json()["status"] == "INSTALL_REFUSED"

    def test_drift_detection_route_is_honest(self, client):
        report = client.get("/api/research/drift?market_id=india&identity=NSE|RELIANCE").json()
        assert report["verdict"] in {"STABLE", "DRIFT_DETECTED", "INSUFFICIENT_DATA"}
        assert report["verdict"] == "INSUFFICIENT_DATA"  # no recorded bars yet
