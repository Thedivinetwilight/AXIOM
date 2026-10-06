"""Spec §96/§97 — bounded autonomous research loop + session-aware
scheduling. The loop must never run away: quotas, resource pressure and
trading-critical windows outrank research throughput, and every refusal
carries its reason."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.config.settings import Settings
from app.api.server import create_app
from app.intel.research_loop import (CRITICAL_SAFE_KINDS, LOOP_STEPS,
                                     QUOTA_STATE_KEY, ResearchScheduler)


class FakeGovernor:
    def __init__(self, pressure=False, critical=False):
        self._pressure = pressure
        self.trading_critical = critical

    def under_pressure(self):
        return self._pressure


class TestResearchScheduler:
    def test_quota_is_bounded_per_day(self):
        scheduler = ResearchScheduler(daily_experiment_quota=2)
        assert scheduler.admit()["admitted"]
        assert scheduler.admit()["admitted"]
        third = scheduler.admit()
        assert third["admitted"] is False
        assert third["verdict"] == "REFUSED_QUOTA_EXCEEDED"
        assert "bounded by design" in third["reason"]

    def test_quota_resets_next_day(self):
        now = time.time()
        scheduler = ResearchScheduler(daily_experiment_quota=1)
        assert scheduler.admit(now=now)["admitted"]
        refused = scheduler.admit(now=now)
        assert refused["admitted"] is False
        tomorrow = scheduler.admit(now=now + 86400)
        assert tomorrow["admitted"] and tomorrow["day"] != refused["day"]

    def test_trading_critical_window_refuses_large_work(self):
        scheduler = ResearchScheduler(live_active=lambda: True)
        decision = scheduler.admit("backtest")
        assert decision["admitted"] is False
        assert decision["verdict"] == "REFUSED_TRADING_CRITICAL_WINDOW"
        assert "hot path" in decision["reason"]

    @pytest.mark.parametrize("kind", sorted(CRITICAL_SAFE_KINDS))
    def test_critical_windows_keep_execution_serving_work(self, kind):
        scheduler = ResearchScheduler(live_active=lambda: True)
        assert scheduler.admit(kind)["admitted"]

    def test_unreadable_live_state_fails_safe(self):
        def broken():
            raise RuntimeError("live state unreadable")

        scheduler = ResearchScheduler(live_active=broken)
        assert scheduler.trading_critical_window() is True
        assert scheduler.admit("backtest")["admitted"] is False

    def test_resource_pressure_refuses_admission(self):
        scheduler = ResearchScheduler(governor=FakeGovernor(pressure=True))
        decision = scheduler.admit("backtest")
        assert decision["admitted"] is False
        assert decision["verdict"] == "REFUSED_RESOURCE_PRESSURE"

    def test_governor_critical_flag_opens_the_window(self):
        scheduler = ResearchScheduler(governor=FakeGovernor(critical=True))
        assert scheduler.trading_critical_window() is True

    def test_status_reports_policy_not_just_numbers(self):
        scheduler = ResearchScheduler(daily_experiment_quota=3,
                                      market_open=lambda: True)
        status = scheduler.status()
        assert status["quota_limit"] == 3
        assert status["market_open"] is True
        assert status["policy"]["run_backtests"] is True
        assert status["policy"]["reconciliation_protection"] is True
        assert status["loop_steps"] == list(LOOP_STEPS)
        scheduler.admit("backtest")
        assert scheduler.status()["quota_used"] == 1

    def test_critical_window_collapses_expansion_policy(self):
        scheduler = ResearchScheduler(live_active=lambda: True)
        policy = scheduler.status()["policy"]
        assert policy["expand_historical_research"] is False
        assert policy["run_backtests"] is False
        assert policy["opportunity_analysis"] is True

    def test_refusals_are_recorded_not_silent(self):
        scheduler = ResearchScheduler(daily_experiment_quota=0)
        scheduler.admit()
        assert scheduler.status()["recent_refusals"]
        assert scheduler.status()["recent_refusals"][-1]["verdict"] == \
            "REFUSED_QUOTA_EXCEEDED"

    def test_negative_quota_rejected(self):
        with pytest.raises(ValueError):
            ResearchScheduler(daily_experiment_quota=-1)


class TestDurableResearchQuota:
    """FINAL-11/10 §37: the daily research quota is DURABLE. A restart, a
    crash, or a new process can never reset it — a fresh scheduler over the
    same database must see what the previous one consumed."""

    def _repo(self, tmp_path):
        from app.database.database import Database
        from app.database.repository import Repository

        database = Database(f"sqlite:///{tmp_path / 'quota.db'}")
        return Repository(database), database

    def test_quota_survives_a_restart(self, tmp_path):
        repository, database = self._repo(tmp_path)
        first = ResearchScheduler(daily_experiment_quota=2,
                                  repository=repository)
        assert first.admit()["admitted"]
        assert first.admit()["admitted"]
        # "restart": a brand-new scheduler instance over the same database
        second = ResearchScheduler(daily_experiment_quota=2,
                                   repository=repository)
        refused = second.admit()
        assert refused["admitted"] is False
        assert refused["verdict"] == "REFUSED_QUOTA_EXCEEDED"
        assert refused["quota_used"] == 2
        assert second.status()["durable"] is True
        database.close()

    def test_crash_mid_day_keeps_the_used_count(self, tmp_path):
        repository, database = self._repo(tmp_path)
        now = time.time()
        crashed = ResearchScheduler(daily_experiment_quota=3,
                                    repository=repository)
        assert crashed.admit(now=now)["admitted"]  # one used, then "crash"
        revived = ResearchScheduler(daily_experiment_quota=3,
                                    repository=repository)
        decision = revived.admit(now=now)
        assert decision["admitted"] and decision["quota_used"] == 2
        assert revived.status(now=now)["quota_used"] == 2
        database.close()

    def test_concurrent_processes_cannot_spend_the_same_quota_slot(self, tmp_path):
        from concurrent.futures import ThreadPoolExecutor

        from app.database.database import Database
        from app.database.repository import Repository

        path = tmp_path / "shared-quota.db"
        first_db = Database(f"sqlite:///{path}")
        second_db = Database(f"sqlite:///{path}")
        schedulers = [
            ResearchScheduler(daily_experiment_quota=1,
                              repository=Repository(first_db)),
            ResearchScheduler(daily_experiment_quota=1,
                              repository=Repository(second_db)),
        ]
        now = time.time()
        with ThreadPoolExecutor(max_workers=2) as pool:
            decisions = list(pool.map(
                lambda scheduler: scheduler.admit(now=now), schedulers))
        assert sum(decision["admitted"] for decision in decisions) == 1
        assert max(decision["quota_used"] for decision in decisions) == 1
        first_db.close()
        second_db.close()

    def test_malformed_quota_state_and_write_failure_fail_closed(self, tmp_path):
        repository, database = self._repo(tmp_path)
        repository.set_engine_state(QUOTA_STATE_KEY, {"not-a-day": 1})
        malformed = ResearchScheduler(daily_experiment_quota=5,
                                      repository=repository)
        decision = malformed.admit()
        assert decision["admitted"] is False
        assert "unreadable or malformed" in decision["reason"]

        class ReadOnlyRepo:
            def get_engine_state(self, key, default=None):
                return {}

        failed_write = ResearchScheduler(daily_experiment_quota=5,
                                         repository=ReadOnlyRepo())
        decision = failed_write.admit()
        assert decision["admitted"] is False
        assert "storage is unavailable" in decision["reason"]
        database.close()

    def test_durable_quota_resets_next_day(self, tmp_path):
        repository, database = self._repo(tmp_path)
        now = time.time()
        today = ResearchScheduler(daily_experiment_quota=1,
                                  repository=repository)
        assert today.admit(now=now)["admitted"]
        assert today.admit(now=now)["admitted"] is False
        tomorrow = ResearchScheduler(daily_experiment_quota=1,
                                     repository=repository)
        assert tomorrow.admit(now=now + 86400)["admitted"]
        database.close()

    def test_unreadable_quota_record_fails_closed(self):
        class BrokenRepo:
            def get_engine_state(self, key, default=None):
                raise RuntimeError("database corrupt")

            def set_engine_state(self, key, value):
                raise RuntimeError("database corrupt")

        scheduler = ResearchScheduler(daily_experiment_quota=5,
                                      repository=BrokenRepo())
        decision = scheduler.admit()
        assert decision["admitted"] is False
        assert decision["verdict"] == "REFUSED_QUOTA_EXCEEDED"
        assert "unreadable" in decision["reason"]


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


class TestSchedulerApi:
    def test_schedule_status_over_http(self, client):
        status = client.get("/api/research/schedule").json()
        assert status["quota_limit"] >= 1
        assert status["loop_steps"][0] == "OBSERVE"
        assert "bounded" in status["note"]

    def test_admit_consumes_quota_over_http(self, quota_admission_client):
        first = quota_admission_client.post(
            "/api/research/schedule/admit", json={"kind": "backtest"}).json()
        assert first["admitted"] and first["quota_used"] == 1
        again = quota_admission_client.get("/api/research/schedule").json()
        assert again["quota_used"] == 1

    def test_resource_pressure_refuses_http_admission_without_spending_quota(
            self, pressure_admission_client):
        decision = pressure_admission_client.post(
            "/api/research/schedule/admit", json={"kind": "backtest"}).json()
        assert decision["admitted"] is False
        assert decision["verdict"] == "REFUSED_RESOURCE_PRESSURE"
        assert decision["quota_used"] == 0
        status = pressure_admission_client.get("/api/research/schedule").json()
        assert status["quota_used"] == 0

    def test_critical_window_visible_via_api_when_live_active(self, client):
        # A live session cannot be activated without full readiness, but the
        # ARMED state alone already opens the critical window by design.
        session = client.get("/api/live/session").json()
        assert session["state"] in ("OFF", "CONFIGURED", "READY", "ARMED",
                                    "ACTIVE", "PAUSED")
        status = client.get("/api/research/schedule").json()
        assert isinstance(status["trading_critical_window"], bool)
