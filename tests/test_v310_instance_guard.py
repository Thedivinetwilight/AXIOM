"""3.1.0 — one Arena per data directory, with a graceful user story.

Regression for the SQLite-writer-lock launch failure: a second Arena must
never die with an opaque traceback, never start a conflicting server, and
never require Task Manager. Verified with REAL subprocesses of the launcher
(the same code path the executable uses):

* second launch while the first is healthy  -> exit 0, "already running",
  first session untouched;
* crash recovery — after the holder dies violently (SIGKILL), the next launch
  succeeds because the OS lock cannot outlive its process;
* stale metadata without a live lock never blocks;
* the guard's metadata never contains secrets.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "arena.py"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _wait_health(base: str, timeout: float = 40.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(base + "/health", timeout=2) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.3)
    return False


def _env_for(home: Path, port: int) -> dict:
    env = dict(os.environ)
    env["ARENA_HOME"] = str(home)
    env["ARENA_PORT"] = str(port)
    env["PYTHONPATH"] = str(ROOT)
    for key in ("DATABASE_URL", "DATA_DIR", "API_TOKEN"):
        env.pop(key, None)
    return env


@pytest.fixture()
def arena_home(tmp_path):
    return tmp_path / "ArenaHome"


class TestInstanceGuardBehaviour:
    def test_metadata_lifecycle_without_a_second_process(self, arena_home):
        from app.instance import InstanceGuard

        guard = InstanceGuard(arena_home)
        assert guard.try_acquire() is None
        meta = guard.publish(port=8123, host="127.0.0.1",
                             version="3.1.0", build="3.1.0-test")
        assert meta["pid"] == os.getpid()
        assert meta["url"] == "http://127.0.0.1:8123"
        assert not any(secret in json.dumps(meta).lower()
                       for secret in ("token", "secret", "password"))
        guard.release()
        assert not (arena_home / "instance.json").exists()

    def test_a_held_lock_describes_the_holder_instead_of_raising(self, arena_home):
        from app.instance import InstanceGuard

        first = InstanceGuard(arena_home)
        assert first.try_acquire() is None
        first.publish(port=8124, host="127.0.0.1", version="x", build="y")
        try:
            second = InstanceGuard(arena_home)
            existing = second.try_acquire()
            assert existing is not None, "second guard must not acquire"
            assert existing.pid == os.getpid()
            assert "already running" in existing.summary().lower()
        finally:
            first.release()

    def test_stale_metadata_never_blocks_a_new_launch(self, arena_home):
        arena_home.mkdir(parents=True, exist_ok=True)
        # metadata from a dead session, but NO held lock: must be reclaimable
        (arena_home / "instance.json").write_text(json.dumps(
            {"pid": 999999, "url": "http://127.0.0.1:1", "version": "old"}),
            encoding="utf-8")
        from app.instance import InstanceGuard

        guard = InstanceGuard(arena_home)
        assert guard.try_acquire() is None, "stale metadata must not block"
        guard.release()


class TestTwoRealProcesses:
    def test_second_launch_opens_the_existing_session_and_exits(self, arena_home):
        port = _free_port()
        env = _env_for(arena_home, port)
        base = f"http://127.0.0.1:{port}"
        first = subprocess.Popen(
            [sys.executable, str(LAUNCHER), "--no-browser"],
            cwd=str(arena_home.parent), env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True)
        try:
            assert _wait_health(base), "first Arena did not start"

            second = subprocess.run(
                [sys.executable, str(LAUNCHER), "--no-browser"],
                cwd=str(arena_home.parent), env=env,
                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                timeout=120)
            assert second.returncode == 0, (
                f"a duplicate launch must be a handled situation, not an error: "
                f"exit={second.returncode}\n{second.stdout[-1000:]}")
            assert "already running" in second.stdout.lower()
            assert "Traceback" not in second.stdout

            # the original session is untouched
            assert _wait_health(base, timeout=10)
            with urllib.request.urlopen(base + "/health", timeout=5) as response:
                assert json.loads(response.read())["status"] == "ok"
        finally:
            first.terminate()
            try:
                first.wait(timeout=15)
            except subprocess.TimeoutExpired:
                first.kill()

    def test_a_crashed_holder_never_wedges_the_next_launch(self, arena_home):
        port = _free_port()
        env = _env_for(arena_home, port)
        base = f"http://127.0.0.1:{port}"
        first = subprocess.Popen(
            [sys.executable, str(LAUNCHER), "--no-browser"],
            cwd=str(arena_home.parent), env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        assert _wait_health(base), "first Arena did not start"
        # a crash: no graceful shutdown, no lock release call
        if os.name == "posix":
            os.kill(first.pid, signal.SIGKILL)
        else:  # pragma: no cover - platform specific
            subprocess.run(["taskkill", "/F", "/PID", str(first.pid)],
                           capture_output=True)
        first.wait(timeout=15)

        replacement = subprocess.Popen(
            [sys.executable, str(LAUNCHER), "--no-browser"],
            cwd=str(arena_home.parent), env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True)
        try:
            assert _wait_health(base, timeout=40), (
                "after a crash the next launch must succeed — the OS lock "
                "cannot outlive its process, so no stale-lock wedge exists")
        finally:
            replacement.terminate()
            try:
                replacement.wait(timeout=15)
            except subprocess.TimeoutExpired:
                replacement.kill()
