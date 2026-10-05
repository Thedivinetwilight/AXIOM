"""Process supervisor for the real-money engine (FINAL-11/10 §14-15).

The workstation API remains the operator control plane; the execution engine
runs as a separately supervised child so a web request thread can never become
the broker's long-running owner. The child is not considered ready merely
because it exists: readiness requires its exact PID/host to own the atomic
SQLite lease and to have completed startup reconciliation.

The supervisor never enables an epoch or places an order. ARM/ACTIVATE remain
explicit state-machine actions, and the gateway independently checks the
child's lease at the final submission boundary.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from app.live.lock import EngineLock

logger = logging.getLogger("bot.live.supervisor")


class LiveSupervisorError(RuntimeError):
    """A live engine could not be started or proven ready."""


class LiveEngineSupervisor:
    """Own and observe one child ``app engine`` process for this workstation."""

    def __init__(self, settings, repository, *,
                 popen_factory=subprocess.Popen, argv_factory=None,
                 cwd: str | Path | None = None,
                 startup_timeout: float = 120.0,
                 poll_interval: float = 0.1,
                 clock=time.time):
        self.settings = settings
        self.repository = repository
        self._popen_factory = popen_factory
        self._argv_factory = argv_factory or self._default_argv
        self.cwd = Path(cwd or Path(__file__).resolve().parents[2])
        self.startup_timeout = max(1.0, float(startup_timeout))
        self.poll_interval = max(0.01, float(poll_interval))
        self.clock = clock
        self._process = None
        self._lock = threading.RLock()
        self._started_at: float | None = None
        self._state = "STOPPED"
        self._last_error = ""

    @staticmethod
    def _default_argv() -> list[str]:
        if getattr(sys, "frozen", False):
            return [sys.executable, "--cli", "engine"]
        return [sys.executable, "-m", "app", "engine"]

    @staticmethod
    def _host() -> str:
        import socket

        return socket.gethostname()

    def _lease_status(self) -> dict:
        return EngineLock(self.repository, live=True).status()

    def _runtime(self) -> dict:
        try:
            state = self.repository.get_engine_state("live_engine_runtime", {})
        except Exception:
            return {}
        return state if isinstance(state, dict) else {}

    def _control(self) -> dict:
        try:
            control = self.repository.get_engine_state("live_engine_control", {}) or {}
        except Exception as exc:
            raise LiveSupervisorError(
                f"live engine control state is unreadable ({type(exc).__name__})") from exc
        if not isinstance(control, dict):
            raise LiveSupervisorError("live engine control state is malformed")
        return control

    def _clear_stop_request(self) -> None:
        """Start a new control generation only after exclusive ownership is safe."""
        previous = self._control()
        try:
            generation = int(previous.get("generation", 0)) + 1
        except (TypeError, ValueError) as exc:
            raise LiveSupervisorError(
                "live engine control generation is malformed") from exc
        now = self.clock()
        control = {"stop_requested": False, "generation": generation,
                   "reason": "new explicit ARM request", "requested_at": now}
        try:
            self.repository.set_engine_state("live_engine_control", control)
            self.repository.record_system_event(
                "LIVE_ENGINE_CONTROL_RESET", {"generation": generation,
                                               "requested_at": now})
        except Exception as exc:
            raise LiveSupervisorError(
                "could not initialize live-engine control state") from exc

    def _ready_for(self, pid: int) -> tuple[bool, dict, dict]:
        lease = self._lease_status()
        runtime = self._runtime()
        host = self._host()
        expected_owner = f"{host}:{int(pid)}"
        age = lease.get("heartbeat_age_seconds")
        ready = (
            lease.get("readable") is True
            and lease.get("atomic_supported") is True
            and lease.get("held") is True
            and lease.get("live") is True
            and lease.get("owner") == expected_owner
            and lease.get("pid") == int(pid)
            and lease.get("host") == host
            and lease.get("stale") is False
            and lease.get("owner_liveness") == "alive"
            and isinstance(age, (int, float))
            and age <= lease.get("stale_after_seconds", 0)
            and runtime.get("state") == "READY"
            and runtime.get("owner") == expected_owner
            and runtime.get("pid") == int(pid)
            and runtime.get("host") == host
            and bool(runtime.get("reconciled_at")))
        return ready, lease, runtime

    def start(self, *, timeout: float | None = None) -> dict:
        """Start the engine and wait for lease + reconciliation readiness.

        This is called only after the operator submits the exact ARM phrase.
        The session is still not ACTIVE while this method runs, so a correct
        child cannot submit entries. The caller must separately ARM/ACTIVATE.
        """
        if (str(getattr(self.settings, "broker", "")).upper() != "UPSTOX_LIVE"
                or str(getattr(self.settings, "mode", "")).upper() != "LIVE"
                or not bool(getattr(self.settings, "live_trading", False))):
            raise LiveSupervisorError("live engine supervisor requires LIVE configuration")
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                control = self._control()
                if control.get("stop_requested") is True:
                    raise LiveSupervisorError(
                        "the previous live engine is still draining safely; "
                        "wait for it to exit before arming again")
                return self.wait_ready(timeout=timeout)

            current = self._lease_status()
            if current.get("readable") is not True \
                    or current.get("atomic_supported") is not True:
                raise LiveSupervisorError(
                    "atomic live-engine lease is unavailable or unreadable")
            if current.get("held") and current.get("live"):
                # Only the lease acquisition code can prove and atomically
                # compare a stale owner's death. Do not launch beside any
                # fresh, unknown, or external owner.
                if current.get("stale") is not True \
                        or current.get("owner_liveness") != "dead":
                    raise LiveSupervisorError(
                        "another live engine owns the account lease or its "
                        "process death cannot be proven")

            # The old request is reset only after a fresh owner has been ruled
            # out. If an old child still owns risk, the lease check above keeps
            # this process from clearing its drain signal.
            self._clear_stop_request()
            self._state = "STARTING"
            self._last_error = ""
            self._started_at = self.clock()
            try:
                self.repository.record_system_event(
                    "LIVE_ENGINE_SUPERVISOR_START_REQUESTED",
                    {"host": self._host(), "requested_at": self._started_at})
            except Exception as exc:
                self._state = "FAILED"
                self._last_error = type(exc).__name__
                raise LiveSupervisorError(
                    "could not audit live-engine start; refusing to spawn") from exc

            kwargs = {
                "cwd": str(self.cwd),
                "stdin": subprocess.DEVNULL,
                # Child diagnostics go through its structured local logger;
                # stdout/stderr pipes are avoided so a 24/7 child cannot
                # deadlock on a full pipe or leak a token into API output.
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "env": os.environ.copy(),
            }
            if os.name == "nt":  # pragma: no cover - Windows CI
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["start_new_session"] = True
            try:
                self._process = self._popen_factory(self._argv_factory(), **kwargs)
            except Exception as exc:
                self._process = None
                self._state = "FAILED"
                self._last_error = type(exc).__name__
                raise LiveSupervisorError(
                    f"could not start live engine ({type(exc).__name__})") from exc
            try:
                self.repository.record_system_event(
                    "LIVE_ENGINE_SUPERVISOR_STARTED",
                    {"pid": int(self._process.pid), "host": self._host(),
                     "requested_at": self._started_at})
            except Exception as exc:
                # Keep the process handle. Never lose track of a child that may
                # already own a position/lease merely because the audit write
                # failed. Its session is not ACTIVE and the caller requests a
                # durable stop below; the child itself will keep risk managed.
                self._state = "FAILED"
                self._last_error = type(exc).__name__
                try:
                    self.request_stop(reason="start audit unavailable")
                except LiveSupervisorError as stop_exc:
                    logger.error("LIVE_START_AUDIT_FAILURE_STOP_SIGNAL_FAILED",
                                 extra={"error_type": type(stop_exc).__name__})
                raise LiveSupervisorError(
                    "live engine spawned but its start audit failed; a safe "
                    "stop was requested") from exc
        return self.wait_ready(timeout=timeout)

    def wait_ready(self, *, timeout: float | None = None) -> dict:
        process = self._process
        if process is None:
            raise LiveSupervisorError("no supervised live engine process exists")
        deadline = time.monotonic() + (self.startup_timeout if timeout is None
                                       else max(0.1, float(timeout)))
        while time.monotonic() < deadline:
            return_code = process.poll()
            if return_code is not None:
                self._state = "FAILED"
                self._last_error = f"exit_code={return_code}"
                raise LiveSupervisorError(
                    f"live engine exited before readiness (code {return_code})")
            ready, _lease, _runtime = self._ready_for(int(process.pid))
            if ready:
                control = self._control()
                if control.get("stop_requested") is True:
                    self._state = "DRAINING"
                    raise LiveSupervisorError(
                        "live engine received a stop request before readiness")
                self._state = "READY"
                return self.status()
            time.sleep(self.poll_interval)
        self._state = "FAILED"
        self._last_error = "startup readiness timeout"
        # Do not SIGTERM/SIGKILL a child which might have reconciled an open
        # broker position. A durable stop signal keeps its loop risk-only; if
        # the signal store itself is unavailable, the stale session state is
        # still non-ACTIVE and the final gateway checks refuse new entries.
        try:
            self.request_stop(reason="startup readiness timeout")
        except LiveSupervisorError as stop_exc:
            logger.error("LIVE_STARTUP_TIMEOUT_STOP_SIGNAL_FAILED",
                         extra={"error_type": type(stop_exc).__name__})
        raise LiveSupervisorError(
            "live engine did not prove a fresh lease and completed startup "
            "reconciliation before the timeout; a safe drain was requested")

    def require_ready(self) -> dict:
        process = self._process
        if process is None or process.poll() is not None:
            self._state = "FAILED"
            raise LiveSupervisorError("the supervised live engine is not running")
        control = self._control()
        if control.get("stop_requested") is True:
            self._state = "DRAINING"
            raise LiveSupervisorError("the supervised live engine is draining safely")
        ready, _lease, _runtime = self._ready_for(int(process.pid))
        if not ready:
            self._state = "BLOCKED"
            raise LiveSupervisorError(
                "the live engine no longer proves a fresh lease and reconciled runtime")
        self._state = "READY"
        return self.status()

    def readiness_context(self) -> dict:
        """Private context consumed by readiness checks, never request input."""
        status = self.require_ready()
        return {"supervisor_verified": True,
                "supervisor_lease_owner": status["owner"],
                "supervisor_pid": status["pid"],
                "supervisor_host": status["host"],
                "last_market_data_ts": status.get("last_market_data_ts")}

    def status(self) -> dict:
        process = self._process
        managed_running = process is not None and process.poll() is None
        lease = self._lease_status()
        runtime = self._runtime()
        managed_pid = int(process.pid) if process is not None else None
        ready = False
        if managed_running and managed_pid is not None:
            ready, lease, runtime = self._ready_for(managed_pid)
        external_live = bool(
            not managed_running and lease.get("held") is True
            and lease.get("live") is True and lease.get("stale") is False
            and lease.get("owner_liveness") == "alive")
        process_running = bool(managed_running or external_live)
        pid = managed_pid if managed_running else lease.get("pid")
        owner = lease.get("owner")
        host = lease.get("host")
        if external_live and self._state not in {"FAILED", "STOPPED"}:
            self._state = "EXTERNAL_OWNER"
        elif managed_running and not ready and self._state == "READY":
            self._state = "BLOCKED"
        elif managed_running and ready and self._state not in {"FAILED", "DRAINING"}:
            self._state = "READY"
        elif process is not None and not managed_running \
                and self._state not in {"FAILED", "STOPPED"}:
            self._state = "STOPPED"
        try:
            control = self._control()
            stop_requested = control.get("stop_requested") is True
            generation = control.get("generation")
        except LiveSupervisorError:
            stop_requested = True
            generation = None
        if stop_requested and managed_running:
            self._state = "DRAINING"
            ready = False
        return {"state": self._state, "running": process_running,
                "managed": managed_running,
                "external_owner": external_live,
                "ready": bool(ready), "pid": pid,
                "host": host, "owner": owner,
                "lease_readable": lease.get("readable", False),
                "lease_held": bool(lease.get("held")),
                "lease_fresh": bool(lease.get("held") and not lease.get("stale")),
                "runtime_state": runtime.get("state"),
                "last_market_data_ts": self._read_market_data_ts(),
                "stop_requested": stop_requested,
                "control_generation": generation,
                "last_error_type": self._last_error or None}

    def _read_market_data_ts(self):
        try:
            return self.repository.get_engine_state("last_market_data_ts")
        except Exception:
            return None

    def request_stop(self, *, reason: str = "operator stop") -> dict:
        """Ask the child to stop after safely reducing any held position.

        No SIGKILL/force bypass is offered. The engine observes this durable
        request between candles and keeps its lease while risk-reducing exits
        remain necessary.
        """
        now = self.clock()
        try:
            previous = self._control()
            try:
                generation = int(previous.get("generation", 0))
            except (TypeError, ValueError):
                generation = 0
            self.repository.set_engine_state("live_engine_control", {
                "stop_requested": True, "generation": generation,
                "reason": str(reason), "requested_at": now})
            self.repository.record_system_event(
                "LIVE_ENGINE_STOP_REQUESTED", {"reason": str(reason),
                                                "requested_at": now,
                                                "generation": generation})
        except Exception as exc:
            self._last_error = type(exc).__name__
            raise LiveSupervisorError(
                "could not persist live-engine stop request") from exc
        if self._process is not None and self._process.poll() is None:
            self._state = "DRAINING"
        return self.status()

    def close(self, *, timeout: float = 5.0) -> dict:
        """Request an orderly stop; never kill an engine with held risk."""
        status = self.request_stop(reason="workstation shutdown")
        process = self._process
        if process is None or process.poll() is not None:
            return status
        try:
            process.wait(timeout=max(0.1, float(timeout)))
        except subprocess.TimeoutExpired:
            # If a position/order remains, risk management takes priority over
            # stopping the workstation. The process is intentionally retained.
            pass
        return self.status()
