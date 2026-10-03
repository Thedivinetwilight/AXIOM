"""Single-engine lock: one account, one live engine (V2.1 L-ops).

Two live engines sharing one account double every entry, fight over reconcile
and make the audit trail unreadable. Nothing in a per-order gate can catch that,
because each engine on its own is behaving correctly.

The lock is deliberately *soft* rather than a filesystem lock:

* it lives in ``engine_state`` next to the other durable state, so it travels
  with the database the engines already share;
* it carries a **heartbeat**; a lock whose heartbeat has stopped is treated as
  stale and does not block a new engine, because a crash must never be able to
  wedge an operator out of restarting;
* it is enforced only for LIVE engines. Paper/sandbox runs are how research and
  tests work, and they are frequently concurrent by design — for those the lock
  is recorded and reported, never enforced.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

LOCK_KEY = "engine_lock"


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

    def to_dict(self) -> dict:
        return {"granted": self.granted, "reason": self.reason,
                "existing": self.existing.to_dict() if self.existing else None}


def _parse(ts) -> datetime | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def owner_id(*, pid: int | None = None, host: str | None = None) -> str:
    """A stable identity for this process: host + pid."""
    return f"{host or socket.gethostname()}:{pid or os.getpid()}"


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
        except Exception:
            return None
        if not isinstance(raw, dict) or not raw.get("owner"):
            return None
        return LockState(owner=str(raw.get("owner")), pid=int(raw.get("pid") or 0),
                         host=str(raw.get("host") or "?"),
                         started_ts=str(raw.get("started_ts") or ""),
                         heartbeat_ts=str(raw.get("heartbeat_ts") or ""),
                         live=bool(raw.get("live", True)))

    def _save(self, state: LockState) -> None:
        if self.repository is None:
            return
        self.repository.set_engine_state(LOCK_KEY, state.to_dict())

    def is_stale(self, existing: LockState | None, *, now=None) -> bool:
        """A lock with no recent heartbeat is stale (its process is gone)."""
        if existing is None:
            return True
        heartbeat = _parse(existing.heartbeat_ts)
        if heartbeat is None:
            return True
        now = now or self._now()
        return (now - heartbeat) > timedelta(seconds=self.stale_after_seconds)

    # -- lifecycle -----------------------------------------------------------

    def acquire(self, *, now=None, force: bool = False) -> LockDecision:
        """Take the lock unless another LIVE engine is demonstrably alive."""
        now = now or self._now()
        existing = self._load()
        if existing is not None and existing.owner != self.owner:
            if self.is_stale(existing, now=now):
                # a crashed engine must never block a restart
                pass
            elif not (self.live and existing.live):
                pass  # non-live sessions are not exclusive; record and continue
            elif not force:
                return LockDecision(
                    False,
                    f"another LIVE engine ({existing.owner}) is heartbeating "
                    f"(last seen {existing.heartbeat_ts}); running two live "
                    "engines against one account would double every entry. "
                    "Stop it, wait for the heartbeat to go stale, or pass the "
                    "explicit --force-live-lock flag if you are certain.",
                    existing=existing)
        started = (existing.started_ts if existing and existing.owner == self.owner
                   and existing.started_ts else now.isoformat())
        self._save(LockState(owner=self.owner, pid=os.getpid(),
                             host=socket.gethostname(), started_ts=started,
                             heartbeat_ts=now.isoformat(), live=self.live))
        self.held = True
        return LockDecision(True, "lock acquired")

    def heartbeat(self, *, now=None) -> bool:
        """Prove this engine is alive. Never raises (a tick must not break)."""
        if self.repository is None:
            return False
        now = now or self._now()
        try:
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
            existing = self._load()
            if existing is not None and existing.owner != self.owner:
                return False
            self.repository.set_engine_state(LOCK_KEY, None)
            self.held = False
            return True
        except Exception:
            return False

    def status(self, *, now=None) -> dict:
        """What an operator/dashboard needs to know about the lock."""
        existing = self._load()
        now = now or self._now()
        if existing is None:
            return {"held": False, "stale": None, "owner": None,
                    "heartbeat_ts": None, "heartbeat_age_seconds": None,
                    "stale_after_seconds": self.stale_after_seconds}
        heartbeat = _parse(existing.heartbeat_ts)
        age = ((now - heartbeat).total_seconds() if heartbeat else None)
        return {"held": True, "stale": self.is_stale(existing, now=now),
                "owner": existing.owner, "pid": existing.pid,
                "host": existing.host, "live": existing.live,
                "started_ts": existing.started_ts,
                "heartbeat_ts": existing.heartbeat_ts,
                "heartbeat_age_seconds": None if age is None else round(age, 1),
                "stale_after_seconds": self.stale_after_seconds}
