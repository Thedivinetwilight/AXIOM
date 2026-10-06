"""Kronos manager — supervises the Kronos runtime from the AXIOM side.

Two transports:

``process``     spawns ``python -m axiom.worker.child`` (the packaged
                ``AXIOM-Kronos.exe``). PyTorch, Kronos and the weights live in
                that process only; if it dies the GUI keeps running and reports
                ``OFFLINE``.
``in-process``  runs the same dispatcher inside this process (used by tests and
                by environments where spawning is not wanted).

All methods are blocking and must be called from a worker thread — the GUI only
ever talks to ``ForecastService``, which owns that thread.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import uuid
from collections import deque
from collections.abc import Callable, Sequence
from datetime import datetime

from axiom.kronos.backends import KronosInference, KronosRuntimeUnavailable
from axiom.kronos.runtime import KronosRuntime
from axiom.log import get_logger
from axiom.market.provider import MarketDataBar
from axiom.market.validation import validate_inference_inputs
from axiom.models import KronosConfig, KronosState, KronosStatus, utcnow
from axiom.worker.child import ChildRuntime
from axiom.worker.protocol import (
    KronosRequest,
    KronosResponse,
    ProtocolError,
    encode_bars,
    encode_timestamps,
)

#: Environment variables that decide where the Kronos runtime looks for its
#: implementation and weights. The child inherits the full environment (a
#: stripped one breaks Python startup on Windows), and these are the ones the
#: caller is expected to control explicitly.
ENV_PASSTHROUGH = (
    "AXIOM_KRONOS_SOURCE",
    "AXIOM_MODEL_CACHE",
    "HF_HOME",
    "HUGGINGFACE_HUB_CACHE",
    "PYTHONPATH",
)


logger = get_logger("worker.manager")


def child_environment(
    overrides: dict[str, str] | None = None, base: dict[str, str] | None = None
) -> dict[str, str]:
    """Environment for the Kronos child process.

    Inherits everything (``SYSTEMROOT``/``PATH``/``TEMP`` are mandatory for a
    Python child on Windows) and then applies explicit overrides, so the caller
    can pin ``AXIOM_KRONOS_SOURCE`` or ``AXIOM_MODEL_CACHE`` per child.
    """
    environment = dict(os.environ if base is None else base)
    environment.update(overrides or {})
    return environment


class KronosManager:
    """Owns the Kronos process lifecycle and the request/response plumbing."""

    def __init__(
        self,
        config: KronosConfig,
        *,
        mode: str = "process",
        command: Sequence[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout: float = 300.0,
        clock: Callable[[], datetime] = utcnow,
        python_executable: str | None = None,
        child: ChildRuntime | None = None,
        capture_stderr: bool = False,
    ) -> None:
        if mode not in {"process", "in-process"}:
            raise ValueError("mode must be 'process' or 'in-process'")
        self.config = config.validate()
        self.mode = mode
        self.command = list(command) if command else None
        self.env = env
        self.cwd = cwd
        self.timeout = timeout
        self.clock = clock
        self.python_executable = python_executable or sys.executable
        self.capture_stderr = capture_stderr
        self._stderr_lines: deque[str] = deque(maxlen=200)
        self._stderr_reader: threading.Thread | None = None
        self._child = child
        self._process: subprocess.Popen | None = None
        self._responses: "queue.Queue[KronosResponse]" = queue.Queue()
        self._reader: threading.Thread | None = None
        # Three separate locks, deliberately:
        #   _io_lock      serialises one request/response exchange at a time
        #   _stderr_lock  lets the stderr drainer run while a request is in
        #                 flight (a blocked drainer would let the pipe fill and
        #                 wedge the child)
        #   _status_lock  guards status mutation from the reader thread
        self._io_lock = threading.RLock()
        self._stderr_lock = threading.Lock()
        self._status_lock = threading.RLock()
        self._status = KronosStatus(
            state=KronosState.DISABLED if not config.enabled else KronosState.INITIALIZING,
            message="Kronos manager not started",
            backend=config.backend,
            model_id=config.model_id,
            tokenizer_id=config.tokenizer_id,
            updated_at=clock(),
        )
        self._shutdown = False

    # -- lifecycle -----------------------------------------------------------
    @property
    def alive(self) -> bool:
        if self._shutdown:
            return False
        if self.mode == "in-process":
            return self._child is not None
        return self._process is not None and self._process.poll() is None

    def child_command(self) -> list[str]:
        if self.command:
            return list(self.command)
        return [self.python_executable, "-m", "axiom.worker.child"]

    def start(self) -> KronosStatus:
        if not self.config.enabled:
            self._status = KronosStatus.disabled("Kronos is disabled in configuration")
            return self._status
        with self._io_lock:
            if self.alive:
                return self._status
            if self.mode == "in-process":
                self._child = self._child or ChildRuntime(self.config)
                self._status = self._update(
                    KronosState.INITIALIZING, "in-process Kronos runtime ready to load"
                )
                return self._status
            # Inherit the full environment and then apply explicit overrides.
            # A stripped environment breaks the child on Windows (no SYSTEMROOT,
            # PATH or TEMP), which would make AXIOM-Kronos.exe unstartable.
            child_env = child_environment(self.env)
            try:
                self._process = subprocess.Popen(  # noqa: S603 - fixed interpreter/module
                    self.child_command(),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    # Inherit stderr by default: a chatty child must never fill a
                    # pipe and block, and its tracebacks stay visible to the user.
                    stderr=subprocess.PIPE if self.capture_stderr else None,
                    text=True,
                    bufsize=1,
                    cwd=self.cwd,
                    env=child_env,
                )
            except (OSError, ValueError) as exc:
                self._status = KronosStatus.error(
                    f"could not start the Kronos process: {exc}", base=self._status
                )
                return self._status
            self._reader = threading.Thread(
                target=self._pump_responses, name="kronos-ipc-reader", daemon=True
            )
            self._reader.start()
            if self.capture_stderr:
                self._stderr_reader = threading.Thread(
                    target=self._pump_stderr, name="kronos-stderr-reader", daemon=True
                )
                self._stderr_reader.start()
            self._status = self._update(
                KronosState.INITIALIZING,
                f"Kronos process started (pid={self._process.pid})",
                pid=self._process.pid,
            )
            logger.info(
                "kronos child started: pid=%s command=%s axiom_env=%s",
                self._process.pid,
                self.child_command(),
                sorted(k for k in child_env if k.startswith("AXIOM_")),
            )
            return self._status

    def _pump_responses(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        for line in self._process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self._responses.put(KronosResponse.decode(line))
            except ProtocolError as exc:
                print(f"kronos ipc: {exc}", file=sys.stderr, flush=True)
        # Stream closed: the child is gone.
        logger.warning(
            "kronos child closed its output stream (exit=%s)",
            self._process.poll() if self._process else "?",
        )
        self._update(KronosState.OFFLINE, "Kronos process closed its output stream")

    # -- requests ------------------------------------------------------------
    def _request(self, kind: str, payload: dict, timeout: float | None = None) -> KronosResponse:
        if not self.alive:
            return KronosResponse.failure(
                "-", "Kronos runtime is not running", KronosState.OFFLINE.value
            )
        request = KronosRequest(kind=kind, request_id=uuid.uuid4().hex[:12], payload=payload)
        limit = self.timeout if timeout is None else timeout
        with self._io_lock:
            if self.mode == "in-process":
                assert self._child is not None
                return self._child.handle(request)
            assert self._process is not None and self._process.stdin is not None
            try:
                self._process.stdin.write(request.encode() + "\n")
                self._process.stdin.flush()
            except (OSError, ValueError) as exc:
                self._status = self._update(
                    KronosState.OFFLINE, f"Kronos process is unreachable: {exc}"
                )
                return KronosResponse.failure(request.request_id, str(exc),
                                              KronosState.OFFLINE.value)
            try:
                while True:
                    response = self._responses.get(timeout=limit)
                    if response.request_id == request.request_id:
                        return response
            except queue.Empty:
                # Distinguish "the child died" from "the child is wedged": a
                # dead process is OFFLINE (restartable), a live one that ignores
                # us is ERROR. Reporting a corpse as ERROR would hide the fact
                # that a restart is the fix.
                dead = self.mode == "process" and (
                    self._process is None or self._process.poll() is not None
                )
                state = KronosState.OFFLINE if dead else KronosState.ERROR
                message = (
                    f"Kronos request '{kind}' timed out after {limit:.0f}s"
                    + (" — the Kronos process is no longer running" if dead else "")
                )
                logger.warning("%s (pid=%s)", message,
                               self._process.pid if self._process else "?")
                self._update(state, message, last_error=message)
                return KronosResponse.failure(
                    request.request_id, message, state.value
                )

    def _update(self, state: KronosState, message: str, **extra) -> KronosStatus:
        with self._status_lock:
            self._status = self._status.with_updates(
                state=state, message=message, **extra)
            return self._status

    def cached_status(self) -> KronosStatus:
        """Non-blocking status read — safe from the GUI thread.

        Only ``process.poll()`` runs here (a cheap, non-blocking syscall); no IPC
        round-trip. The worker thread refreshes the cached status via
        :meth:`status`.
        """
        if not self.alive and self._status.state not in {
            KronosState.DISABLED, KronosState.OFFLINE
        }:
            return self._update(KronosState.OFFLINE, "Kronos process is not running")
        with self._status_lock:
            return self._status

    def _apply_status_payload(self, response: KronosResponse) -> KronosStatus:
        payload = (response.result or {}).get("status")
        if isinstance(payload, dict) and payload:
            status = KronosStatus.from_dict(payload)
            # The child cannot know its own supervisor-side pid.
            if self._process is not None and not status.pid:
                status = status.with_updates(pid=self._process.pid)
            self._status = status
        elif response.state:
            self._status = self._update(KronosState(response.state), response.error or "")
        return self._status

    # -- public API ----------------------------------------------------------
    def load(self, timeout: float | None = None) -> KronosStatus:
        if not self.config.enabled:
            self._status = KronosStatus.disabled("Kronos is disabled in configuration")
            return self._status
        self.start()
        response = self._request("load", {"config": self.config.to_dict()}, timeout)
        if not response.ok and not (response.result or {}).get("status"):
            self._status = self._update(
                KronosState.ERROR, response.error or "Kronos load failed"
            )
            return self._status
        return self._apply_status_payload(response)

    def status(self) -> KronosStatus:
        """Blocking status read: asks the child. WORKER THREADS ONLY.

        The GUI must use :meth:`cached_status` — this performs an IPC
        round-trip and can wait behind an in-flight inference.
        """
        if not self.alive:
            if self._status.state not in {KronosState.DISABLED, KronosState.OFFLINE}:
                self._status = self._update(KronosState.OFFLINE, "Kronos process is not running")
            return self._status
        response = self._request("status", {}, timeout=min(self.timeout, 30.0))
        if response.ok:
            return self._apply_status_payload(response)
        return self._status

    def forecast(
        self,
        bars: Sequence[MarketDataBar],
        future_timestamps: Sequence[datetime],
        timeout: float | None = None,
    ) -> KronosInference:
        # Validate before serialization as well as in the child/runtime/backend:
        # malformed bars or timestamps must not cross the process boundary.
        validate_inference_inputs(bars, future_timestamps, self.config)
        response = self._request(
            "forecast",
            {
                "config": self.config.to_dict(),
                "bars": encode_bars(bars),
                "future_timestamps": encode_timestamps(future_timestamps),
            },
            timeout,
        )
        if response.result.get("status"):
            self._apply_status_payload(response)
        if not response.ok:
            raise KronosRuntimeUnavailable(
                response.error or "Kronos inference failed in the worker process"
            )
        payload = response.result
        paths = tuple(tuple(step) for step in payload.get("paths", []))
        if not paths:
            raise KronosRuntimeUnavailable("Kronos worker returned no trajectory")
        return KronosInference(
            paths=paths,
            latency_ms=float(payload.get("latency_ms", 0.0)),
            backend=str(payload.get("backend", self.config.backend)),
            notes=tuple(payload.get("notes", ())),
            model_id=str(payload.get("model_id") or self.config.model_id),
            model_revision=str(
                payload.get("model_revision") or self.config.model_revision
            ),
        )

    def ping(self, timeout: float = 10.0) -> bool:
        response = self._request("ping", {}, timeout)
        return bool(response.ok and response.result.get("pong"))

    def restart(self, timeout: float = 10.0) -> KronosStatus:
        """Recover from a wedged or dead child: shut down, then start + load."""
        self.shutdown(timeout=timeout)
        self._shutdown = False
        with self._stderr_lock:
            self._stderr_lines.clear()
        while not self._responses.empty():
            try:
                self._responses.get_nowait()
            except queue.Empty:  # pragma: no cover - race with the reader thread
                break
        return self.load(timeout=self.timeout)

    def shutdown(self, timeout: float = 10.0) -> None:
        """Clean shutdown: ask politely, then terminate, then kill.

        Never waits on ``_io_lock``: if a request is still in flight the child is
        by definition not answering, so terminating it is the correct action.
        """
        self._shutdown = True
        if self.mode == "in-process":
            with self._io_lock:
                if self._child is not None:
                    self._child.handle(
                        KronosRequest(kind="shutdown", request_id="shutdown")
                    )
                self._update(KronosState.OFFLINE, "Kronos runtime shut down")
            return
        process = self._process
        if process is None:
            return
        if process.poll() is None:
            try:
                if process.stdin is not None:
                    process.stdin.write(
                        KronosRequest(kind="shutdown", request_id="shutdown").encode()
                        + "\n"
                    )
                    process.stdin.flush()
            except (OSError, ValueError):
                pass
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=timeout)
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        self._process = None
        self._update(KronosState.OFFLINE, "Kronos process shut down")

    def stderr_tail(self, limit: int = 40) -> list[str]:
        """Recent child diagnostics (only when ``capture_stderr=True``)."""
        with self._stderr_lock:
            return list(self._stderr_lines)[-limit:]

    def _pump_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        for line in process.stderr:
            text = line.rstrip()
            if text:
                # Never the I/O lock: this drainer must keep running while a
                # long inference holds it, or the child's stderr pipe fills up
                # and the child blocks on write.
                with self._stderr_lock:
                    self._stderr_lines.append(text)

    def describe(self) -> dict:
        return {
            "mode": self.mode,
            "alive": self.alive,
            "command": self.child_command() if self.mode == "process" else ["in-process"],
            "status": self._status.to_dict(),
            "config": self.config.to_dict(),
        }

    def to_json(self) -> str:
        return json.dumps(self.describe(), indent=2, sort_keys=True, default=str)


def in_process_manager(
    config: KronosConfig, runtime: KronosRuntime | None = None
) -> KronosManager:
    """Manager backed by a child runtime in this process (tests/dev)."""
    child = ChildRuntime(runtime=runtime) if runtime is not None else None
    return KronosManager(config, mode="in-process", child=child)
