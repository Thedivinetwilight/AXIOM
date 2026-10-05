"""Supervisor, readiness and drain invariants without real broker orders."""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.database.database import Database
from app.database.repository import Repository
from app.live.lock import EngineLock
from app.live.supervisor import LiveEngineSupervisor, LiveSupervisorError


@pytest.fixture()
def repo():
    database = Database("sqlite:///:memory:")
    repository = Repository(database)
    yield repository
    database.close()


def live_settings():
    return SimpleNamespace(mode="LIVE", broker="UPSTOX_LIVE", live_trading=True)


class FakeProcess:
    def __init__(self, pid=None):
        self.pid = int(pid or os.getpid())
        self.terminated = False
        self.killed = False
        self.wait_calls = []

    def poll(self):
        return None

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        raise subprocess.TimeoutExpired("fixture-live-engine", timeout)

    def terminate(self):  # pragma: no cover - a forbidden path in these tests
        self.terminated = True

    def kill(self):  # pragma: no cover - a forbidden path in these tests
        self.killed = True


def _publish_ready_runtime(repository, lease):
    now = datetime.now(timezone.utc).isoformat()
    repository.set_engine_state("last_market_data_ts", now)
    repository.set_engine_state("last_reconcile_at", {"ts": now, "fills": 0})
    repository.set_engine_state("live_engine_runtime", {
        "state": "READY", "owner": lease["owner"], "pid": lease["pid"],
        "host": lease["host"], "reconciled_at": now})


def test_supervisor_waits_for_matching_lease_and_reconciliation_then_drains(repo):
    process = FakeProcess()
    spawned = []

    def popen(argv, **kwargs):
        spawned.append((argv, kwargs))
        lock = EngineLock(repo, live=True)
        assert lock.acquire().granted
        _publish_ready_runtime(repo, lock.status())
        return process

    supervisor = LiveEngineSupervisor(
        live_settings(), repo, popen_factory=popen,
        argv_factory=lambda: ["fixture-engine"], startup_timeout=1,
        poll_interval=0.01)
    result = supervisor.start(timeout=0.2)

    assert result["state"] == "READY"
    assert result["ready"] is True
    assert result["managed"] is True
    assert result["pid"] == process.pid
    assert len(spawned) == 1
    assert spawned[0][0] == ["fixture-engine"]
    assert spawned[0][1]["stdin"] == subprocess.DEVNULL
    assert repo.get_engine_state("live_engine_control")["stop_requested"] is False

    drained = supervisor.close(timeout=0.01)
    assert drained["running"] is True  # process intentionally retained
    assert drained["stop_requested"] is True
    assert drained["state"] == "DRAINING"
    assert process.terminated is False
    assert process.killed is False
    assert process.wait_calls


def test_startup_timeout_persists_drain_and_never_force_kills(repo):
    process = FakeProcess()
    supervisor = LiveEngineSupervisor(
        live_settings(), repo, popen_factory=lambda *_a, **_k: process,
        argv_factory=lambda: ["fixture-engine"], startup_timeout=1,
        poll_interval=0.01)

    with pytest.raises(LiveSupervisorError, match="did not prove"):
        supervisor.start(timeout=0.1)

    assert repo.get_engine_state("live_engine_control")["stop_requested"] is True
    assert process.poll() is None
    assert process.terminated is False
    assert process.killed is False


def test_fresh_unknown_live_owner_prevents_spawn_and_preserves_stop_request(repo):
    owner = EngineLock(repo, live=True)
    assert owner.acquire().granted
    repo.set_engine_state("live_engine_control", {
        "stop_requested": True, "generation": 7, "reason": "keep draining"})
    spawned = []
    supervisor = LiveEngineSupervisor(
        live_settings(), repo,
        popen_factory=lambda *a, **k: spawned.append((a, k)),
        argv_factory=lambda: ["must-not-start"], startup_timeout=1)

    with pytest.raises(LiveSupervisorError, match="another live engine owns"):
        supervisor.start()

    assert spawned == []
    control = repo.get_engine_state("live_engine_control")
    assert control["stop_requested"] is True
    assert control["generation"] == 7


def test_supervisor_rejects_non_live_configuration_before_spawn(repo):
    spawned = []
    settings = SimpleNamespace(mode="PAPER", broker="LOCAL_PAPER",
                               live_trading=False)
    supervisor = LiveEngineSupervisor(
        settings, repo, popen_factory=lambda *a, **k: spawned.append((a, k)))

    with pytest.raises(LiveSupervisorError, match="LIVE configuration"):
        supervisor.start()
    assert spawned == []
