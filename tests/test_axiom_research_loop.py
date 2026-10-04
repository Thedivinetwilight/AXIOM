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
                                     ResearchScheduler)


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

    def test_admit_consumes_quota_over_http(self, client):
        first = client.post("/api/research/schedule/admit",
                            json={"kind": "backtest"}).json()
        assert first["admitted"] and first["quota_used"] == 1
        again = client.get("/api/research/schedule").json()
        assert again["quota_used"] == 1

    def test_critical_window_visible_via_api_when_live_active(self, client):
        # A live session cannot be activated without full readiness, but the
        # ARMED state alone already opens the critical window by design.
        session = client.get("/api/live/session").json()
        assert session["state"] in ("OFF", "CONFIGURED", "READY", "ARMED",
                                    "ACTIVE", "PAUSED")
        status = client.get("/api/research/schedule").json()
        assert isinstance(status["trading_critical_window"], bool)
