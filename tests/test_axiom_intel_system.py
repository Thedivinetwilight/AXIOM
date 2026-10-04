"""AXIOM-MONEY-PRINTER — intelligence, state and system services.

Covers master-spec sections 8, 10, 11, 14-16, 18, 20-21, 24, 26, 57, 68,
71: event gateway, what-changed engine, opportunity lifecycle + expiry,
evidence stack honesty, historical analogues without look-ahead, missed
opportunities, latency observability, task center recovery, backup/restore
integrity and Kronos Studio truthful statuses — plus the HTTP surface.
"""

from __future__ import annotations

import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.config.settings import Settings
from app.database.database import Database
from app.database.repository import Repository
from app.intel.analogues import AnalogueEngine
from app.intel.evidence import EvidenceStack
from app.intel.missed import MissedOpportunityRecorder
from app.intel.opportunity import (DETECTED, EXPIRED, HIGH_CONVICTION,
                                   INVALIDATED, OpportunityCenter)
from app.state.bus import EventBus
from app.state.changes import ChangeEngine
from app.system.backup import BackupManager
from app.system.latency import LatencyTracker, track
from app.system.resources import ResourceGovernor
from app.system.tasks import TaskCenter


@pytest.fixture()
def repo():
    database = Database("sqlite:///:memory:")
    repository = Repository(database)
    yield repository
    database.close()


# =====================================================================
# Event gateway (spec 71)
# =====================================================================

class TestEventBus:
    def test_publish_subscribe_sequence(self):
        bus = EventBus()
        seen = []
        bus.subscribe("price.update", seen.append)
        bus.publish("price.update", {"last": 100})
        bus.publish("other", {})
        bus.publish("price.update", {"last": 101})
        assert [e["payload"]["last"] for e in seen] == [100, 101]
        assert seen[1]["seq"] > seen[0]["seq"]

    def test_replay_since_detects_gaps(self):
        bus = EventBus()
        bus.publish("a", {"i": 1})
        first = bus.latest_sequence()
        bus.publish("b", {"i": 2})
        bus.publish("a", {"i": 3})
        replayed = bus.replay_since(first)
        assert [e["seq"] for e in replayed] == [first + 1, first + 2]

    def test_broken_subscriber_never_breaks_publish(self):
        bus = EventBus()

        def bad(event):
            raise RuntimeError("subscriber bug")

        bus.subscribe("*", bad)
        event = bus.publish("x", {})
        assert event["seq"] == 1
        assert bus.health()["subscriber_errors"] == 1


# =====================================================================
# What changed (spec 8)
# =====================================================================

class TestChangeEngine:
    def test_deltas_recorded_and_published(self, repo):
        bus = EventBus()
        engine = ChangeEngine(repo, bus=bus)
        seen = []
        bus.subscribe("state.changed", seen.append)
        assert engine.observe("regime", "NSE|RELIANCE", {"label": "trend"}) == []
        changes = engine.observe("regime", "NSE|RELIANCE", {"label": "high-vol"},
                                 evidence="volatility crossed 2x median")
        assert len(changes) == 1
        assert changes[0]["previous"] == "trend"
        assert changes[0]["current"] == "high-vol"
        assert seen and seen[0]["payload"]["field"] == "label"
        assert repo.list_state_changes(domain="regime")[0]["field"] == "label"

    def test_numeric_tolerance_not_reported_as_change(self, repo):
        engine = ChangeEngine(repo, tolerance=1e-6)
        engine.observe("account", "equity", {"value": 100000.0})
        assert engine.observe("account", "equity", {"value": 100000.0000001}) == []

    def test_new_field_has_previous_none_not_fake_zero(self, repo):
        engine = ChangeEngine(repo)
        engine.observe("feed", "sym", {"price": 1.0})
        changes = engine.observe("feed", "sym", {"price": 1.0, "volume": 5})
        assert len(changes) == 1
        assert changes[0]["previous"] is None and changes[0]["current"] == 5


# =====================================================================
# Opportunity center (spec 14, 15, 139)
# =====================================================================

class TestOpportunityCenter:
    def test_attention_mode_thresholds(self):
        now = [1000.0]
        center = OpportunityCenter(attention_mode="conservative", clock=lambda: now[0])
        assert center.observe(market_id="india", identity="NSE|A",
                              price_change_pct=0.02, volume_ratio=1.0) is None
        center.set_attention_mode("aggressive")
        assert center.observe(market_id="india", identity="NSE|A",
                              price_change_pct=0.02, volume_ratio=1.0) is not None

    def test_invalid_attention_mode_refused(self):
        center = OpportunityCenter()
        with pytest.raises(ValueError):
            center.set_attention_mode("yolo")

    def test_lifecycle_and_terminal_states(self):
        now = [1000.0]
        center = OpportunityCenter(clock=lambda: now[0])
        opportunity = center.observe(market_id="india", identity="NSE|A",
                                     price_change_pct=0.05)
        center.transition(opportunity.opportunity_id, HIGH_CONVICTION,
                          reason="evidence stack aligned")
        assert center.get(opportunity.opportunity_id)["state"] == HIGH_CONVICTION
        center.transition(opportunity.opportunity_id, INVALIDATED, reason="news denied")
        with pytest.raises(ValueError):
            center.transition(opportunity.opportunity_id, DETECTED)

    def test_stale_evidence_expires(self, repo):
        now = [1000.0]
        center = OpportunityCenter(repo, clock=lambda: now[0], attention_mode="balanced")
        opportunity = center.observe(market_id="india", identity="NSE|A",
                                     price_change_pct=0.05)
        now[0] += center.profile["ttl_seconds"] + 1
        expired = center.expire_stale()
        assert [o.opportunity_id for o in expired] == [opportunity.opportunity_id]
        assert center.get(opportunity.opportunity_id)["state"] == EXPIRED
        # persisted with the honest reason
        rows = repo.list_opportunity_records()
        assert rows[0]["state"] == EXPIRED
        assert rows[0]["invalidation_reason"].startswith("evidence older than")

    def test_capacity_eviction_is_bounded(self):
        center = OpportunityCenter(attention_mode="conservative")  # max 5
        for i in range(7):
            center.observe(market_id="india", identity=f"NSE|S{i}",
                           price_change_pct=0.09)
        assert len(center._active_items()) <= 5


# =====================================================================
# Evidence stack (spec 16, 140, 141)
# =====================================================================

class TestEvidenceStack:
    def test_layers_carry_provenance_and_age(self):
        now = [1000.0]
        stack = EvidenceStack(identity="NSE|A", market_id="india", clock=lambda: now[0])
        stack.set("market_state", kind="OBSERVED", value={"last": 1.0},
                  provenance="feed")
        now[0] += 5
        view = stack.to_dict()
        layer = next(l for l in view["layers"] if l["layer"] == "market_state")
        assert layer["age_seconds"] == 5.0
        assert layer["kind"] == "OBSERVED"

    def test_unavailable_is_never_faked(self):
        stack = EvidenceStack(identity="NSE|A", market_id="india")
        stack.set("kronos_forecast", kind="MODEL", value=None, provenance="kronos",
                  unavailable_reason="weights not verified")
        view = stack.to_dict()
        layer = next(l for l in view["layers"] if l["layer"] == "kronos_forecast")
        assert layer["status"] == "UNAVAILABLE"
        assert layer["value"]["unavailable_reason"] == "weights not verified"

    def test_recommendation_expires_and_invalidates_on_change(self):
        now = [1000.0]
        stack = EvidenceStack(identity="NSE|A", market_id="india", clock=lambda: now[0])
        stack.set("market_state", kind="OBSERVED", value={"last": 1.0}, provenance="feed")
        rec = stack.recommend("WATCH", rationale="developing", validity_s=60)
        assert stack.recommendation()["state"] == "WATCH"
        now[0] += 61
        assert stack.recommendation() is None          # expired, not kept alive
        # fresh recommendation then evidence change invalidates it
        now[0] += 1
        stack.recommend("FAVORABLE", rationale="x")
        stack.set("market_state", kind="OBSERVED", value={"last": 2.0}, provenance="feed")
        assert stack.recommendation() is None

    def test_recommendation_vocabulary_enforced(self):
        stack = EvidenceStack(identity="NSE|A", market_id="india")
        with pytest.raises(ValueError):
            stack.recommend("GUARANTEED WIN", rationale="nope")


# =====================================================================
# Historical analogues (spec 10)
# =====================================================================

def _trend_then_noise(n=120, seed=7):
    import random

    rng = random.Random(seed)
    closes, price = [], 100.0
    for i in range(n):
        drift = 0.15 if i < n // 2 else -0.05
        price *= 1 + drift / 100 + rng.uniform(-0.4, 0.4) / 100
        closes.append(price)
    return closes


class TestAnalogues:
    def test_search_excludes_look_ahead(self):
        engine = AnalogueEngine(lookback=20, horizon=5, top_k=4)
        closes = _trend_then_noise()
        search = engine.search("NSE|A", closes)
        assert search.sample_size > 0
        n = len(closes)
        for result in search.results:
            # analogue window + forward horizon must end before current window
            assert result.analogue_start_index + 20 + 5 <= n - 20
        assert search.methodology
        assert any("not a guarantee" in l for l in search.limitations)

    def test_summary_requires_sample_size(self):
        engine = AnalogueEngine(lookback=20, horizon=5)
        search = engine.search("NSE|A", _trend_then_noise(60))
        summary = engine.summarize(search)
        if search.sample_size < 3:
            assert summary["summarizable"] is False

    def test_bad_data_refused_not_repaired(self):
        engine = AnalogueEngine()
        closes = _trend_then_noise(40)
        closes[-3] = float("nan")   # inside the CURRENT window
        with pytest.raises(ValueError):
            engine.search("NSE|A", closes)

    def test_negative_analogues_included(self):
        engine = AnalogueEngine(lookback=20, horizon=5, top_k=3)
        search = engine.search("NSE|A", _trend_then_noise(200), include_negatives=True)
        assert search.negatives
        assert search.negatives[0].similarity <= search.results[0].similarity


# =====================================================================
# Missed opportunities (spec 11, 24)
# =====================================================================

class TestMissedOpportunities:
    def test_record_and_stats(self, repo):
        recorder = MissedOpportunityRecorder(repo)
        recorder.record(kind="false_negative", payload={"signal": "momentum"},
                        knowable=True, identity="NSE|A",
                        research_question="threshold too high?")
        recorder.record(kind="blocked_by_safety", payload={"guard": "RISK CHECK"},
                        knowable=True)
        stats = recorder.stats()
        assert stats["total"] == 2
        assert stats["by_kind"]["false_negative"] == 1
        assert stats["knowable"] == 2

    def test_unknown_kind_refused(self, repo):
        recorder = MissedOpportunityRecorder(repo)
        with pytest.raises(ValueError):
            recorder.record(kind="vibes", payload={}, knowable=False)


# =====================================================================
# Latency observability (spec 24, 87)
# =====================================================================

class TestLatency:
    def test_exact_percentiles_on_small_reservoir(self):
        tracker = LatencyTracker()
        for value in range(1, 101):
            tracker.record("risk_check", float(value))
        summary = tracker.summary("risk_check")
        assert summary["p50_ms"] == pytest.approx(50.5, abs=1.0)
        assert summary["p95_ms"] >= 94
        assert summary["p99_ms"] >= 98

    def test_deadline_misses_counted(self):
        tracker = LatencyTracker(deadlines_ms={"risk_check": 10.0})
        assert tracker.record("risk_check", 5.0) is False
        assert tracker.record("risk_check", 11.0) is True
        assert tracker.summary("risk_check")["deadline_misses"] == 1

    def test_track_context_manager(self):
        tracker = LatencyTracker()
        with track(tracker, "feature_update"):
            time.sleep(0.001)
        assert tracker.summary("feature_update")["count"] == 1

    def test_daily_persistence(self, repo):
        tracker = LatencyTracker()
        tracker.record("broker_request", 12.0)
        written = tracker.persist_daily(repo, day="2026-10-04")
        assert written == 1
        rows = repo.list_latency_summaries(stage="broker_request")
        assert rows[0]["day"] == "2026-10-04"
        assert rows[0]["count"] == 1


# =====================================================================
# Task center + resource governor (spec 26, 57, 97)
# =====================================================================

class TestTaskCenter:
    def test_lifecycle(self, repo):
        tasks = TaskCenter(repo)
        task = tasks.submit("walk-forward sweep", "research")
        assert tasks.may_start(task.task_id)
        tasks.update_progress(task.task_id, 0.5, note="halfway")
        assert tasks.get(task.task_id)["progress"] == 0.5
        tasks.complete(task.task_id, payload={"result": "ok"})
        assert tasks.get(task.task_id)["state"] == "COMPLETED"

    def test_research_concurrency_cap(self):
        tasks = TaskCenter(max_concurrent_research=1)
        a = tasks.submit("a", "research")
        b = tasks.submit("b", "research")
        assert tasks.may_start(a.task_id)
        assert not tasks.may_start(b.task_id)

    def test_crash_recovery_marks_stuck_failed(self, repo):
        tasks = TaskCenter(repo)
        task = tasks.submit("long scan", "research")
        tasks.may_start(task.task_id)
        recovered = tasks.recover()
        assert recovered == 1
        assert tasks.get(task.task_id)["state"] == "FAILED"
        assert "restarted" in tasks.get(task.task_id)["error"]

    def test_pressure_blocks_research_not_critical(self):
        class Pressured:
            def under_pressure(self):
                return True

        tasks = TaskCenter(governor=Pressured())
        research = tasks.submit("sweep", "research")
        critical = tasks.submit("reconcile", "critical")
        assert not tasks.may_start(research.task_id)
        assert tasks.may_start(critical.task_id)

    def test_governor_reports_unknown_honestly(self, tmp_path):
        governor = ResourceGovernor(disk_path=str(tmp_path))
        snapshot = governor.snapshot()
        assert "load_1m" in snapshot and "disk_used_pct" in snapshot
        assert governor.research_concurrency_cap() >= 1
        governor.set_trading_critical(True)
        assert governor.research_concurrency_cap() == 1


# =====================================================================
# Backup / restore (spec 68)
# =====================================================================

class TestBackupRestore:
    def _seeded_db(self, tmp_path):
        db_path = tmp_path / "trading_bot.db"
        database = Database(f"sqlite:///{db_path}")
        repository = Repository(database)
        repository.save_universe(market_id="india", name="Pre", symbols=["NSE|X"])
        database.close()
        return db_path

    def test_roundtrip(self, tmp_path):
        db_path = self._seeded_db(tmp_path)
        manager = BackupManager(db_path, backup_dir=tmp_path / "backups")
        created = manager.create(note="roundtrip")
        assert created["sha256"]
        verified = manager.verify(created["path"])
        assert verified["ok"], verified["problems"]
        result = manager.restore(created["path"])
        assert result["restored"]
        assert result["previous_database"]  # rollback copy preserved
        database = Database(f"sqlite:///{db_path}")
        repository = Repository(database)
        assert repository.list_universes()[0]["name"] == "Pre"
        database.close()

    def test_corrupt_archive_refused(self, tmp_path):
        db_path = self._seeded_db(tmp_path)
        manager = BackupManager(db_path, backup_dir=tmp_path / "backups")
        created = manager.create()
        with open(created["path"], "r+b") as handle:
            handle.seek(100)
            handle.write(b"\x00" * 200)   # corrupt the payload
        verification = manager.verify(created["path"])
        result = manager.restore(created["path"])
        assert result["restored"] is False
        assert not verification["ok"] or "refused" in result["note"]

    def test_locked_target_yields_honest_restart_required(self, tmp_path, monkeypatch):
        """Windows keeps open SQLite files locked against rename. The swap
        failure path must stage the verified database, never fake success."""
        from pathlib import Path as _Path

        db_path = self._seeded_db(tmp_path)
        manager = BackupManager(db_path, backup_dir=tmp_path / "backups")
        created = manager.create()
        real_replace = _Path.replace

        def refusing_replace(self, target):
            if self.name.endswith(".restore-staging") and \
                    str(target).endswith("trading_bot.db"):
                raise PermissionError(32, "The process cannot access the file")
            return real_replace(self, target)

        monkeypatch.setattr(_Path, "replace", refusing_replace)
        result = manager.restore(created["path"])
        assert result["restored"] is False
        assert result["status"] == "RESTORE_PREPARED_RESTART_REQUIRED"
        pending = _Path(result["pending_database"])
        assert pending.exists() and pending.name.endswith(".restore-pending")
        # The staged file is itself a valid SQLite database.
        import sqlite3
        connection = sqlite3.connect(pending)
        try:
            assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        finally:
            connection.close()


# =====================================================================
# Kronos Studio (spec 18)
# =====================================================================

class TestKronosStudio:
    def test_cards_are_truthful_without_weights(self, tmp_path, repo):
        from axiom.kronos.model_cache import ModelCache
        from app.models.studio import KronosStudio

        studio = KronosStudio(ModelCache(tmp_path / "cache"), repository=repo)
        cards = studio.catalog()
        assert len(cards) == 3
        for card in cards:
            assert card["lifecycle"] == "NOT_INSTALLED"
            assert card["integrity"]["ok"] is False

    def test_download_without_source_is_honest(self, tmp_path, repo):
        from axiom.kronos.model_cache import ModelCache
        from app.models.studio import KronosStudio

        studio = KronosStudio(ModelCache(tmp_path / "cache"), repository=repo)
        result = studio.download("NeoQuasar/Kronos-mini")
        assert result["ok"] is False
        assert result["status"] == "DOWNLOAD_NOT_CONFIGURED"

    def test_install_and_activate_refuse_unverified(self, tmp_path, repo):
        from axiom.kronos.model_cache import ModelCache
        from app.models.studio import KronosStudio

        studio = KronosStudio(ModelCache(tmp_path / "cache"), repository=repo)
        assert studio.install("NeoQuasar/Kronos-mini")["ok"] is False
        assert studio.activate("NeoQuasar/Kronos-mini")["ok"] is False

    def test_verified_weights_install_and_activate(self, tmp_path, repo):
        from axiom.kronos.model_cache import ModelCache
        from app.models.studio import KronosStudio

        cache = ModelCache(tmp_path / "cache")
        weights = tmp_path / "cache" / "kronos.bin"
        weights.parent.mkdir(parents=True, exist_ok=True)
        weights.write_bytes(b"\x00" * 64)
        cache.record("NeoQuasar/Kronos-mini", "", weights)
        studio = KronosStudio(cache, repository=repo)
        assert studio.verify("NeoQuasar/Kronos-mini")["ok"] is True
        assert studio.install("NeoQuasar/Kronos-mini")["ok"] is True
        activated = studio.activate("NeoQuasar/Kronos-mini")
        assert activated["ok"] is True
        assert repo.get_engine_state("kronos_active_model") == "NeoQuasar/Kronos-mini"

    def test_benchmark_never_fabricated(self, tmp_path, repo):
        from axiom.kronos.model_cache import ModelCache
        from app.models.studio import KronosStudio

        studio = KronosStudio(ModelCache(tmp_path / "cache"), repository=repo)
        result = studio.benchmark("NeoQuasar/Kronos-mini")
        assert result["ok"] is False
        assert result["status"] == "NOT_ATTEMPTED"


# =====================================================================
# HTTP surface (integration; bearer-gated like every data route)
# =====================================================================

@pytest.fixture()
def client(tmp_path, monkeypatch):
    for key in ("API_TOKEN", "DATABASE_URL", "DATA_DIR", "ARENA_HOME",
                "ARENA_ENV_FILE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("API_TOKEN", "workstation-token")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'arena.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    with TestClient(create_app(Settings.from_env())) as test_client:
        test_client.headers.update({"Authorization": "Bearer workstation-token"})
        yield test_client


class TestWorkstationApi:
    def test_markets_and_sessions(self, client):
        body = client.get("/api/markets").json()
        assert {m["market_id"] for m in body["markets"]} >= {"india", "us", "japan"}
        session = client.get("/api/markets/india/session").json()
        assert session["status"] in {"OPEN", "PRE_OPEN", "CLOSED", "WEEKEND",
                                     "HOLIDAY", "ALWAYS"}

    def test_universe_crud_export_import(self, client):
        saved = client.post("/api/universes", json={
            "market_id": "india", "name": "Core",
            "symbols": ["NSE_EQ|INE002A01018", "RELIANCE"]}).json()
        assert saved["symbols"] == ["NSE|INE002A01018", "NSE|RELIANCE"]
        exported = client.get(f"/api/universes/{saved['id']}/export").json()
        assert exported["format"] == "axiom-universe/v1"
        imported = client.post("/api/universes/import", json={
            **{k: exported[k] for k in ("format", "market_id", "symbols")},
            "name": "Core copy"}).json()
        assert imported["name"] == "Core copy"
        assert client.delete(f"/api/universes/{imported['id']}").json()["deleted"]

    def test_universe_rejects_cross_market_identity(self, client):
        response = client.post("/api/universes", json={
            "market_id": "india", "name": "Bad", "symbols": ["NYSE|AAPL"]})
        assert response.status_code == 422

    def test_opportunity_lifecycle_over_http(self, client):
        detected = client.post("/api/opportunities/observe", json={
            "market_id": "india", "identity": "NSE|RELIANCE",
            "price_change_pct": 0.04}).json()
        assert detected["detected"]
        opportunity_id = detected["opportunity"]["id"]
        moved = client.post(f"/api/opportunities/{opportunity_id}/transition",
                            json={"state": "QUALIFYING", "reason": "volume confirmed"}).json()
        assert moved["state"] == "QUALIFYING"
        listed = client.get("/api/opportunities?market_id=india").json()
        assert listed["counts"]["QUALIFYING"] == 1

    def test_live_session_transitions_over_http(self, client):
        from app.config.settings import LIVE_CONFIRM_PHRASE

        session = client.get("/api/live/session").json()
        assert session["state"] == "OFF"
        assert "ACTIVE" in session["states"]
        refused = client.post("/api/live/session/arm",
                              json={"confirmation_phrase": "nope"})
        assert refused.status_code == 409
        # default settings are not live-configured -> readiness blocks arming
        blocked = client.post("/api/live/session/arm",
                              json={"confirmation_phrase": LIVE_CONFIRM_PHRASE})
        assert blocked.status_code == 409
        summary = client.get("/api/live/start-summary").json()
        assert summary["required_phrase"] == LIVE_CONFIRM_PHRASE
        readiness = client.get("/api/live/readiness-center").json()
        assert readiness["verdict"] in {"BLOCKED", "NOT_READY_UNVERIFIED"}
        # every readiness check carries an explicit status, none silent
        assert all(c["status"] in {"READY", "WARNING", "BLOCKED", "UNKNOWN", "ERROR"}
                   for c in readiness["checks"])

    def test_kronos_studio_over_http(self, client):
        overview = client.get("/api/kronos/studio").json()
        assert len(overview["cards"]) == 3
        download = client.post("/api/kronos/studio/download",
                               json={"model_id": "NeoQuasar/Kronos-mini"}).json()
        assert download["ok"] is False  # honest: no source configured here

    def test_tasks_backup_latency_over_http(self, client):
        task = client.post("/api/tasks", json={"name": "scan", "type": "research"}).json()
        started = client.post(f"/api/tasks/{task['id']}", json={"action": "start"}).json()
        assert started["started"]
        assert client.get("/api/tasks").json()["counts"]["RUNNING"] == 1
        assert client.get("/api/backup/list").json()["backups"] == []
        created = client.post("/api/backup/create", json={"note": "api"}).json()
        assert created["sha256"]
        assert client.get("/api/latency").json()["stages"]

    def test_event_stream_carries_published_events(self, client):
        # publish something on the app bus, then read it back through /api/stream
        app = client.app
        workstation = app.state.workstation
        workstation.bus.publish("state.changed", {"field": "mode"})
        events = workstation.bus.replay_since(0)
        assert any(e["type"] == "state.changed" for e in events)
        assert client.get("/api/stream/health").json()["published"] >= 1

    def test_changes_endpoint_lists_what_changed(self, client):
        # First save establishes the snapshot; the UPDATE produces the delta —
        # the change engine must never report a first observation as a change.
        client.post("/api/universes", json={"market_id": "india", "name": "Delta",
                                            "symbols": ["NSE|X"]})
        client.post("/api/universes", json={"market_id": "india", "name": "Delta",
                                            "symbols": ["NSE|X", "NSE|Y"]})
        changes = client.get("/api/changes?domain=universe").json()["changes"]
        assert changes and changes[0]["domain"] == "universe"
        assert changes[0]["field"] == "size"
        assert (changes[0]["previous"], changes[0]["current"]) == ("1", "2")

    def test_backup_path_confinement(self, client, tmp_path):
        evil = client.post("/api/backup/restore", json={"path": "/etc/passwd"})
        assert evil.status_code in (404, 422)

    def test_secrets_not_in_boot_surfaces(self, client):
        boot = client.get("/api/boot").json()
        assert "workstation-token" not in json.dumps(boot)


# =====================================================================
# Drift detection (spec 25)
# =====================================================================

class TestDriftDetector:
    def _closes(self, base_level=100.0, recent_level=100.0, n=40, noise=0.001):
        import random
        rng = random.Random(7)
        out = []
        for i in range(n):
            level = base_level if i < n // 2 else recent_level
            out.append(level * (1 + rng.uniform(-noise, noise)))
        return out

    def test_stable_series_is_stable(self):
        from app.intel.drift import DriftDetector
        report = DriftDetector(window=20, min_samples=15).evaluate(
            "NSE|X", self._closes())
        assert report.verdict == "STABLE"
        assert report.drifted == ()
        assert report.statistics["location"]["baseline"] > 0

    def test_level_shift_is_detected_with_evidence(self):
        from app.intel.drift import DriftDetector
        report = DriftDetector(window=20, min_samples=15).evaluate(
            "NSE|X", self._closes(base_level=100.0, recent_level=130.0))
        assert report.verdict == "DRIFT_DETECTED"
        assert "location" in report.drifted
        assert report.statistics["location"]["recent"] > \
            report.statistics["location"]["baseline"]

    def test_insufficient_data_makes_no_claim(self):
        from app.intel.drift import DriftDetector
        report = DriftDetector(window=20, min_samples=15).evaluate(
            "NSE|X", self._closes(n=12))
        assert report.verdict == "INSUFFICIENT_DATA"
        assert report.statistics == {}

    def test_bad_input_refused_not_repaired(self):
        from app.intel.drift import DriftDetector
        import pytest as _pt
        with _pt.raises(ValueError):
            DriftDetector().evaluate("NSE|X", [100.0, float("nan"), 101.0])
        with _pt.raises(ValueError):
            DriftDetector().evaluate("NSE|X", [100.0, -5.0, 101.0])
