"""Single-engine lock: one account, one live engine (V2.1 L-ops).

Two live engines sharing one account double every entry, fight over reconcile
and make the audit trail unreadable. Nothing in a per-order gate can catch that,
because each engine on its own is behaving correctly.

The lock is deliberately *soft* rather than a filesystem lock:

* it lives in the shared database next to the other durable state, so it
  travels with the database the engines already share; FINAL-11/10 §15 made
  ownership a single conditional SQL upsert (``engine_lease``) so two engines
  racing cannot both win, and removed the casual ``--force-live-lock`` bypass
  (takeover still requires the owner's heartbeat to be provably stale);
* it carries a **heartbeat**; a lock whose heartbeat has stopped is treated as
  stale and does not block a new engine, because a crash must never be able to
  wedge an operator out of restarting;
* it is enforced only for LIVE engines. Paper/sandbox runs are how research and
  tests work, and they are frequently concurrent by design — for those the lock
  is recorded and reported, never enforced.
"""

from __future__ import annotations

import logging
import math
import os
import socket
from uuid import uuid4
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

LOCK_KEY = "engine_lock"
_PROCESS_INSTANCE_ID = uuid4().hex


@dataclass(frozen=True)
class LockState:
    owner: str
    pid: int
    host: str
    started_ts: str
    heartbeat_ts: str
    live: bool

    def to_dict(self) -> dict:
        return {"owner": self.owner, "pid": self.pid, "host": self.host,
                "started_ts": self.started_ts, "heartbeat_ts": self.heartbeat_ts,
                "live": self.live}


@dataclass(frozen=True)
class LockDecision:
    granted: bool
    reason: str
    existing: LockState | None = None
    #: FINAL-11/10 §15 — True when this grant replaced a stale owner. The
    #: engine MUST reconcile (and the session MUST mint a fresh epoch) before
    #: it may submit live orders, because the previous owner's open positions
    #: and in-flight intents are unknown.
    took_over_stale: bool = False

    def to_dict(self) -> dict:
        return {"granted": self.granted, "reason": self.reason,
                "existing": self.existing.to_dict() if self.existing else None,
                "took_over_stale": self.took_over_stale}


def _state_from_mapping(raw: dict) -> LockState:
    if not isinstance(raw, dict):
        raise ValueError("lease record is not an object")
    owner = raw.get("owner")
    host = raw.get("host")
    pid = raw.get("pid")
    started = raw.get("started_ts")
    heartbeat = raw.get("heartbeat_ts")
    live = raw.get("live", True)
    if not isinstance(owner, str) or not owner.strip():
        raise ValueError("lease owner is missing")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ValueError("lease PID is invalid")
    if not isinstance(host, str) or not host.strip():
        raise ValueError("lease host is invalid")
    if not isinstance(started, str) or not isinstance(heartbeat, str):
        raise ValueError("lease timestamps are malformed")
    if type(live) is bool:
        is_live = live
    elif type(live) is int and live in (0, 1):
        is_live = bool(live)
    else:
        raise ValueError("lease mode is malformed")
    return LockState(owner=owner, pid=pid, host=host,
                     started_ts=started, heartbeat_ts=heartbeat, live=is_live)


def _parse(ts) -> datetime | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def owner_id(*, pid: int | None = None, host: str | None = None) -> str:
    """A process-instance identity: host + PID + random per-process nonce.

    PID and host alone are not unique across restarts because operating systems
    reuse PIDs. The nonce is stable for this Python process, shared by its
    EngineLock and gateway, and changes after a restart.
    """
    process_id = os.getpid() if pid is None else int(pid)
    hostname = socket.gethostname() if host is None else str(host)
    return f"{hostname}:{process_id}:{_PROCESS_INSTANCE_ID}"


def _pid_alive_windows(pid: int, *, kernel32=None,
                       get_last_error=None) -> bool | None:
    """Query a Windows PID without treating access denial as proof of death.

    ``OpenProcess`` reports ERROR_INVALID_PARAMETER for a PID that does not
    exist. Access-denied and other failures are unknown: they must never be
    converted into permission to take over a LIVE lease.
    """
    try:
        import ctypes
        from ctypes import wintypes

        if kernel32 is None:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        process_query_limited_information = 0x1000
        still_active = 259
        error_invalid_parameter = 87
        error_not_found = 1168

        # Explicit prototypes preserve pointer-sized HANDLEs on 64-bit Windows.
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                         wintypes.DWORD]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        handle = kernel32.OpenProcess(
            process_query_limited_information, False, int(pid))
        if not handle:
            if get_last_error is None:
                get_last_error = ctypes.get_last_error
            error = get_last_error()
            if error in (error_invalid_parameter, error_not_found):
                return False
            return None
        try:
            code = wintypes.DWORD(still_active)
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return None
            return code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


def _pid_alive(pid: int) -> bool | None:
    """Best-effort local PID proof; None means it cannot be established."""
    if int(pid or 0) <= 0:
        return None
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI
        return _pid_alive_windows(int(pid))
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None


class LockStateUnavailable(RuntimeError):
    """The durable owner state could not be safely established."""


class EngineLock:
    """Acquire / heartbeat / release the single-engine lock, durably."""

    def __init__(self, repository, *, stale_after_seconds: int = 180,
                 live: bool = True, owner: str | None = None, now_fn=None):
        self.repository = repository
        self.stale_after_seconds = max(30, int(stale_after_seconds))
        self.live = bool(live)
        self.owner = owner or owner_id()
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self.held = False

    # -- state ---------------------------------------------------------------

    def _load(self) -> LockState | None:
        if self.repository is None:
            return None
        try:
            raw = self.repository.get_engine_state(LOCK_KEY, None)
        except Exception as exc:
            raise LockStateUnavailable(
                f"lease state read failed ({type(exc).__name__})") from exc
        if raw is None:
            return None
        try:
            return _state_from_mapping(raw)
        except (TypeError, ValueError, KeyError) as exc:
            raise LockStateUnavailable("lease record is malformed") from exc

    def _save(self, state: LockState) -> None:
        if self.repository is None:
            return
        self.repository.set_engine_state(LOCK_KEY, state.to_dict())

    def _stale_proof(self, existing: LockState | None, *, now=None) -> dict:
        """Return explicit heartbeat + host/PID proof for a stale LIVE owner.

        A stale timestamp alone is insufficient: a paused process or network
        partition may still own real-money state. Automatic takeover is only
        possible for a local owner whose PID is demonstrably gone. A remote
        host, malformed heartbeat, reused/live PID, or liveness-probe error is
        UNKNOWN and must not be converted into "stale".
        """
        if existing is None:
            return {"proven": True, "reason": "no existing owner", "liveness": "none"}
        heartbeat = _parse(existing.heartbeat_ts)
        if heartbeat is None:
            return {"proven": False, "reason": "heartbeat timestamp is unknown",
                    "liveness": "unknown"}
        now = now or self._now()
        age = (now - heartbeat).total_seconds()
        if not isinstance(age, (int, float)) or not math.isfinite(age):
            return {"proven": False, "reason": "heartbeat age is invalid",
                    "liveness": "unknown"}
        if age < -1.0:
            return {"proven": False,
                    "reason": "heartbeat timestamp is in the future",
                    "liveness": "unknown", "age_seconds": age}
        if age <= self.stale_after_seconds:
            return {"proven": False, "reason": "heartbeat is fresh",
                    "liveness": "alive", "age_seconds": max(0.0, age)}
        if existing.host != socket.gethostname():
            return {"proven": False,
                    "reason": "remote owner PID cannot be verified on this host",
                    "liveness": "unknown", "age_seconds": age}
        alive = _pid_alive(existing.pid)
        if alive is False:
            return {"proven": True, "reason": "stale heartbeat and local PID is absent",
                    "liveness": "dead", "age_seconds": age}
        if alive is True:
            if (existing.pid == os.getpid()
                    and existing.host == socket.gethostname()
                    and existing.owner != self.owner):
                return {"proven": False,
                        "reason": "local PID is alive but its process-instance owner "
                                 "does not match the lease; PID reuse is unknown",
                        "liveness": "unknown", "age_seconds": age}
            return {"proven": False, "reason": "heartbeat is stale but local PID is alive",
                    "liveness": "alive", "age_seconds": age}
        return {"proven": False, "reason": "local PID liveness could not be verified",
                "liveness": "unknown", "age_seconds": age}

    def is_stale(self, existing: LockState | None, *, now=None) -> bool:
        """True only when heartbeat AND local process-death proof are present."""
        return bool(self._stale_proof(existing, now=now)["proven"])


    # -- lifecycle -----------------------------------------------------------

    def acquire(self, *, now=None, force: bool = False) -> LockDecision:
        """Take the lock unless another LIVE engine is demonstrably alive.

        FINAL-11/10 §15: live ownership is decided by ONE conditional SQL
        statement (repository.try_acquire_lease) — never by a read-then-write
        pair, so two engines starting at the same instant cannot both win.
        ``force`` is NOT a casual bypass: takeover still requires the
        existing owner's heartbeat to be provably stale.
        """
        now = now or self._now()
        now_iso = now.isoformat()
        stale_before = (now - timedelta(seconds=self.stale_after_seconds)).isoformat()
        acquire_lease = (getattr(self.repository, "try_acquire_lease", None)
                         if self.repository is not None else None)

        # A read-then-write fallback is not atomic and therefore cannot own a
        # real-money engine. Missing lease support is a refusal, not a reason to
        # weaken the single-owner contract.
        if self.live and not callable(acquire_lease):
            logger.error("LIVE_LEASE_ATOMICITY_UNAVAILABLE")
            return LockDecision(
                False, "atomic live-engine lease unavailable; refusing live start")

        stale_proof = None
        if self.live and callable(acquire_lease):
            try:
                read_lease = getattr(self.repository, "read_lease", None)
                if not callable(read_lease):
                    raise LockStateUnavailable("lease read API is unavailable")
                prior = read_lease(LOCK_KEY)
            except Exception as exc:
                logger.error("LIVE_LEASE_PRECHECK_FAILED",
                             extra={"error_type": type(exc).__name__})
                return LockDecision(
                    False, "live-engine lease owner could not be verified; refusing start")
            if prior is not None:
                try:
                    prior_state = _state_from_mapping(prior)
                except (TypeError, ValueError, KeyError):
                    return LockDecision(
                        False, "live-engine lease record is malformed; refusing start")
                same_process = (
                    prior_state.owner == self.owner
                    and prior_state.pid == os.getpid()
                    and prior_state.host == socket.gethostname())
                if not same_process and prior_state.live:
                    proof = self._stale_proof(prior_state, now=now)
                    if not proof["proven"]:
                        logger.warning("LIVE_LEASE_TAKEOVER_REFUSED",
                                       extra={"owner": prior_state.owner,
                                              "reason": proof["reason"],
                                              "liveness": proof["liveness"]})
                        return LockDecision(
                            False,
                            f"another LIVE engine ({prior_state.owner}) holds the "
                            "lease; running two live engines against one account "
                            "would double every entry. Takeover is refused because "
                            f"the owner cannot be proven stopped: {proof['reason']}",
                            existing=prior_state)
                    stale_proof = {
                        "owner": prior_state.owner, "pid": prior_state.pid,
                        "host": prior_state.host,
                        "heartbeat_ts": prior_state.heartbeat_ts}

        if callable(acquire_lease):
            try:
                result = acquire_lease(
                    LOCK_KEY, self.owner, pid=os.getpid(),
                    host=socket.gethostname(), live=self.live, now_ts=now_iso,
                    stale_before_ts=stale_before, stale_proof=stale_proof)
            except Exception as exc:
                logger.error("LIVE_LEASE_ACQUIRE_FAILED",
                             extra={"error_type": type(exc).__name__})
                return LockDecision(
                    False, "live-engine lease could not be verified; refusing start")
            if not isinstance(result, dict) or "acquired" not in result:
                logger.error("LIVE_LEASE_ACQUIRE_INVALID_RESULT")
                return LockDecision(
                    False, "live-engine lease returned an invalid result; refusing start")
            if result["acquired"]:
                self.held = True
                detail = "lock acquired"
                takeover = bool(result.get("took_over_stale"))
                if takeover:
                    detail = (f"took over stale lease from "
                              f"{result['existing']['owner']}")
                    # FINAL-11/10 §15/§61: a takeover is a safety event; the
                    # proof (old heartbeat, new pid/host) must be durable.
                    try:
                        self.repository.record_system_event(
                            "LIVE_LEASE_TAKEOVER", {
                                "name": LOCK_KEY,
                                "old_owner": result["existing"]["owner"],
                                "old_heartbeat_ts":
                                    result["existing"]["heartbeat_ts"],
                                "stale_before_ts": stale_before,
                                "new_owner": self.owner,
                                "pid": os.getpid(),
                                "host": socket.gethostname(),
                                "live": self.live})
                    except Exception:
                        logger.debug("lease takeover audit event not recorded")
                return LockDecision(True, detail, took_over_stale=takeover)
            existing_row = result.get("existing")
            if not isinstance(existing_row, dict):
                # A conditional upsert can lose to a concurrent INSERT after
                # its pre-upsert SELECT observed no row. That is an ordinary
                # losing race, not a reason to crash or infer that we won.
                # Paper/non-live engines are explicitly non-exclusive, so they
                # may proceed locally without claiming the live lease.
                if not self.live:
                    self.held = True
                    return LockDecision(True, "lock acquired (non-live coexists)")
                return LockDecision(
                    False,
                    "live-engine lease changed during atomic acquisition; refusing start")
            try:
                existing = _state_from_mapping(existing_row)
            except (TypeError, ValueError, KeyError):
                return LockDecision(
                    False, "live-engine lease result is malformed; refusing start")
            if not (self.live and existing.live):
                # Non-live sessions are not exclusive; record and continue.
                self.held = True
                return LockDecision(True, "lock acquired (non-live coexists)")
            return LockDecision(
                False,
                f"another LIVE engine ({existing.owner}) is heartbeating "
                f"(last seen {existing.heartbeat_ts}); running two live "
                "engines against one account would double every entry. "
                "Stop it, or wait for the heartbeat to go stale — there is "
                "no casual force bypass.",
                existing=existing)

        # Paper/test repositories without lease support may use legacy state;
        # unlike LIVE, they cannot own an execution lease.
        try:
            existing = self._load()
        except LockStateUnavailable as exc:
            logger.error("ENGINE_LOCK_STATE_UNREADABLE",
                         extra={"error_type": type(exc).__name__})
            return LockDecision(False,
                                "engine lock state unreadable; refusing start")
        if existing is not None and existing.owner != self.owner:
            if self.is_stale(existing, now=now):
                # a crashed engine must never block a restart
                pass
            elif not (self.live and existing.live):
                pass  # non-live sessions are not exclusive; record and continue
            else:
                return LockDecision(
                    False,
                    f"another LIVE engine ({existing.owner}) is heartbeating "
                    f"(last seen {existing.heartbeat_ts}); running two live "
                    "engines against one account would double every entry.",
                    existing=existing)
        started = (existing.started_ts if existing and existing.owner == self.owner
                   and existing.started_ts else now_iso)
        self._save(LockState(owner=self.owner, pid=os.getpid(),
                             host=socket.gethostname(), started_ts=started,
                             heartbeat_ts=now_iso, live=self.live))
        self.held = True
        return LockDecision(True, "lock acquired")

    def heartbeat(self, *, now=None) -> bool:
        """Prove this engine is alive. Never raises (a tick must not break)."""
        if self.repository is None:
            return False
        now = now or self._now()
        try:
            if hasattr(self.repository, "heartbeat_lease"):
                # Only refreshes when we still own the lease; a takeover is
                # visible immediately (heartbeat returns False and the engine
                # must treat itself as de-authorized).
                ok = self.repository.heartbeat_lease(
                    LOCK_KEY, self.owner, now_ts=now.isoformat())
                self.held = bool(ok)
                return ok
            existing = self._load()
            started = (existing.started_ts
                       if existing and existing.owner == self.owner
                       and existing.started_ts else now.isoformat())
            self._save(LockState(owner=self.owner, pid=os.getpid(),
                                 host=socket.gethostname(), started_ts=started,
                                 heartbeat_ts=now.isoformat(), live=self.live))
            self.held = True
            return True
        except Exception:
            return False

    def release(self) -> bool:
        """Give the lock back, but never steal someone else's."""
        if self.repository is None:
            return False
        try:
            if hasattr(self.repository, "release_lease"):
                released = self.repository.release_lease(LOCK_KEY, self.owner)
                if released:
                    self.held = False
                return released
            existing = self._load()
            if existing is not None and existing.owner != self.owner:
                return False
            self.repository.set_engine_state(LOCK_KEY, None)
            self.held = False
            return True
        except Exception:
            return False

    def status(self, *, now=None) -> dict:
        """What an operator/dashboard needs to know about the lock.

        An unreadable lease is UNKNOWN, never "no owner". The readiness gate
        consumes ``readable`` and ``atomic_supported`` and blocks activation
        when either proof is unavailable.
        """
        now = now or self._now()
        atomic_supported = callable(
            getattr(self.repository, "try_acquire_lease", None)
            if self.repository is not None else None)
        if self.repository is None:
            return {"held": None, "known": False, "readable": False,
                    "atomic_supported": False, "stale": None, "owner": None,
                    "heartbeat_ts": None, "heartbeat_age_seconds": None,
                    "stale_after_seconds": self.stale_after_seconds,
                    "error_type": "RepositoryUnavailable"}
        try:
            read_lease = getattr(self.repository, "read_lease", None)
            lease = read_lease(LOCK_KEY) if callable(read_lease) else None
            if lease is not None:
                try:
                    existing = _state_from_mapping(lease)
                except (TypeError, ValueError, KeyError) as exc:
                    raise LockStateUnavailable(
                        "lease record has an invalid shape") from exc
            else:
                existing = self._load()
        except Exception as exc:
            logger.error("ENGINE_LOCK_STATUS_UNAVAILABLE",
                         extra={"error_type": type(exc).__name__})
            return {"held": None, "known": False, "readable": False,
                    "atomic_supported": atomic_supported, "stale": None,
                    "owner": None, "heartbeat_ts": None,
                    "heartbeat_age_seconds": None,
                    "stale_after_seconds": self.stale_after_seconds,
                    "error_type": type(exc).__name__}
        if existing is None:
            return {"held": False, "known": True, "readable": True,
                    "atomic_supported": atomic_supported, "stale": None,
                    "owner": None, "heartbeat_ts": None,
                    "heartbeat_age_seconds": None,
                    "stale_after_seconds": self.stale_after_seconds}
        heartbeat = _parse(existing.heartbeat_ts)
        age = ((now - heartbeat).total_seconds() if heartbeat else None)
        proof = self._stale_proof(existing, now=now)
        return {"held": True, "known": True, "readable": True,
                "atomic_supported": atomic_supported,
                "stale": bool(proof["proven"]),
                "stale_proof_reason": proof["reason"],
                "owner_liveness": proof["liveness"],
                "owner": existing.owner, "pid": existing.pid,
                "host": existing.host, "live": existing.live,
                "started_ts": existing.started_ts,
                "heartbeat_ts": existing.heartbeat_ts,
                "heartbeat_age_seconds": None if age is None else round(age, 1),
                "stale_after_seconds": self.stale_after_seconds}
