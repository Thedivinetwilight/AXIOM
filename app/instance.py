"""One Arena per data directory — with a human-friendly story.

The database layer already enforces a single paper WRITER (an OS file lock on
``<db>.paper.lock``). That is the right safety rule and it stays. What it does
not provide is a *user experience*: when the rule fires, a second launch used
to die with an opaque traceback, leaving the user with Task Manager as the
only tool.

This module moves the rule up to application level:

* an **OS file lock** (``instance.lock`` in the Arena home) is the single
  source of truth for "another Arena owns this data directory right now".
  OS locks are released automatically when a process dies — including crashes
  and power loss — so a *held* lock always means a live holder, and an
  *unheld* lock means any leftover metadata is provably stale. There is no
  age-based guessing and no user-facing advice to delete lock files;
* a small **``instance.json``** next to it carries the metadata a second
  launch needs to be helpful: pid, port, URL, version, last heartbeat;
* a second launch therefore either **opens the running session in the
  browser** (holder healthy) or reports exactly what it found (holder not
  responding — never killed, never bypassed).

The guard never touches the paper writer lock or the database itself; it only
decides whether THIS process should be the one serving this home.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

LOCK_NAME = "instance.lock"
META_NAME = "instance.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pid_alive(pid: int) -> bool | None:
    """Best-effort liveness check. Returns None when it cannot be decided.

    Only used to make messages friendlier ("process 1234 is alive"). It is
    NEVER used to decide whether a lock may be taken — only the OS lock
    decides that, and it cannot outlive its holder.
    """
    if pid <= 0:
        return False
    if os.name == "posix":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # exists but owned by someone else
        except OSError:
            return None
    try:  # Windows: OpenProcess + exit code STILL_ACTIVE (259)
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            code = ctypes.c_ulong(STILL_ACTIVE)
            ok = ctypes.windll.kernel32.GetExitCodeProcess(
                handle, ctypes.byref(code))
            return bool(ok) and code.value == STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        return None


@dataclass(frozen=True)
class ExistingInstance:
    """What we know about the Arena that already owns this home."""

    pid: int
    host: str
    port: int | None
    url: str
    version: str
    build: str
    started: str
    last_seen: str
    pid_alive: bool | None
    reachable: bool

    def summary(self) -> str:
        state = "responding" if self.reachable else (
            "process alive but not responding yet"
            if self.pid_alive else "holding the session lock")
        return (f"Arena is already running for this data directory "
                f"(pid {self.pid}, {state}, {self.url or 'URL unknown'}).")


class InstanceGuard:
    """Acquires, publishes and releases this process's claim on a home."""

    def __init__(self, home: Path):
        self.home = Path(home)
        self._lock_file = None
        self._heartbeat: threading.Thread | None = None
        self._stop_heartbeat = threading.Event()
        self.meta: dict = {}

    # ------------------------------------------------------------------ lock
    def try_acquire(self) -> ExistingInstance | None:
        """Take the session lock, or describe who already holds it.

        Returns None on success (we are THE Arena for this home); otherwise
        an :class:`ExistingInstance` describing the holder. Never raises for
        a merely contended lock — contention is an expected user situation.
        """
        self.home.mkdir(parents=True, exist_ok=True)
        handle = open(self.home / LOCK_NAME, "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                handle.write(b"0")
                handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return self._describe_holder()
        self._lock_file = handle
        return None

    def _describe_holder(self) -> ExistingInstance:
        meta = self.read_meta()
        pid = int(meta.get("pid") or 0)
        url = str(meta.get("url") or "")
        reachable = False
        if url:
            reachable = probe_health(url, timeout=1.0)
        return ExistingInstance(
            pid=pid,
            host=str(meta.get("host") or ""),
            port=meta.get("port"),
            url=url,
            version=str(meta.get("version") or "?"),
            build=str(meta.get("build") or "?"),
            started=str(meta.get("started") or ""),
            last_seen=str(meta.get("last_seen") or ""),
            pid_alive=_pid_alive(pid) if pid else None,
            reachable=reachable,
        )

    # ------------------------------------------------------------------ meta
    def read_meta(self) -> dict:
        try:
            return json.loads((self.home / META_NAME).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write_meta(self, meta: dict) -> None:
        tmp = self.home / (META_NAME + ".tmp")
        tmp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        os.replace(tmp, self.home / META_NAME)

    def publish(self, *, port: int, host: str, version: str, build: str) -> dict:
        """Record (or refresh) this process's claim. Only the lock holder may
        call this; the previous holder's metadata is by definition stale."""
        if not self._lock_file:
            raise RuntimeError("publish() before try_acquire() succeeded")
        existing = self.read_meta()
        meta = {
            "pid": os.getpid(),
            "host": host,
            "port": port,
            "url": f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}",
            "version": version,
            "build": build,
            "started": existing.get("started") if existing.get("pid") == os.getpid()
            else _now_iso(),
            "last_seen": _now_iso(),
        }
        if not meta["started"]:
            meta["started"] = _now_iso()
        self._write_meta(meta)
        self.meta = meta
        return meta

    def start_heartbeat(self, interval: float = 15.0) -> None:
        """Keep ``last_seen`` fresh so diagnostics can tell a live session
        from an old metadata file at a glance."""
        if self._heartbeat or not self._lock_file:
            return

        def beat() -> None:
            while not self._stop_heartbeat.wait(interval):
                try:
                    meta = self.read_meta()
                    if meta.get("pid") == os.getpid():
                        meta["last_seen"] = _now_iso()
                        self._write_meta(meta)
                except OSError as exc:
                    # A missed beat is harmless — the OS lock is the source of
                    # truth — but the reason stays diagnosable.
                    logging.getLogger("bot.instance").debug(
                        "heartbeat_skipped", extra={"error": type(exc).__name__})

        self._heartbeat = threading.Thread(
            target=beat, name="arena-instance-heartbeat", daemon=True)
        self._heartbeat.start()

    def release(self) -> None:
        self._stop_heartbeat.set()
        if self._lock_file:
            try:
                # Remove our own metadata so a fresh launch starts clean;
                # never delete files we do not demonstrably own.
                meta = self.read_meta()
                if meta.get("pid") == os.getpid():
                    try:
                        (self.home / META_NAME).unlink()
                    except OSError as exc:
                        # Leaving the file is harmless (the next holder
                        # overwrites it), but record why it stayed.
                        logging.getLogger("bot.instance").debug(
                            "meta_cleanup_skipped",
                            extra={"error": type(exc).__name__})
            finally:
                try:
                    self._lock_file.close()
                finally:
                    self._lock_file = None

    # ------------------------------------------------------------ diagnostics
    def status(self) -> dict:
        """For /api/instance and the System panel. No secrets, ever."""
        held = bool(self._lock_file)
        meta = self.meta if held else self.read_meta()
        return {
            "lock_held_by_this_process": held,
            "pid": meta.get("pid"),
            "this_pid": os.getpid(),
            "url": meta.get("url"),
            "version": meta.get("version"),
            "build": meta.get("build"),
            "started": meta.get("started"),
            "last_seen": meta.get("last_seen"),
            "stale": (not held) and bool(meta) and _pid_alive(int(meta.get("pid") or 0)) is False,
        }


def probe_health(base_url: str, timeout: float = 1.0) -> bool:
    """Is another Arena answering at this URL right now?"""
    try:
        with urllib.request.urlopen(base_url + "/health", timeout=timeout) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def wait_until_healthy(base_url: str, timeout: float = 6.0,
                       interval: float = 0.25) -> bool:
    """Poll /health until it answers or the budget is spent."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if probe_health(base_url, timeout=1.0):
            return True
        time.sleep(interval)
    return probe_health(base_url, timeout=1.0)


def hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return "localhost"
