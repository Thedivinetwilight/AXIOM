"""Background task center (master spec sections 26, 57, 97).

A durable registry of background work with live progress, cancellation and
recovery semantics. Tasks never run trading-critical code; the scheduler
distinguishes task CLASSES so research cannot starve execution:

* ``critical``  — trading-path work (never queued behind research);
* ``interactive`` — user-facing actions;
* ``research``  — experiments/backtests (budget-capped);
* ``maintenance`` — downloads, backups, compaction.

The registry itself executes nothing; workers report into it. That keeps the
GUI honest (progress shown is progress recorded) and keeps this module
deterministically testable.
"""

from __future__ import annotations

import threading
import time

logger = __import__('logging').getLogger(__name__)
from dataclasses import dataclass, field
from uuid import uuid4

from app.system.resources import ResourceGovernor

STATES = ("QUEUED", "RUNNING", "PAUSED", "COMPLETED", "FAILED", "CANCELLED")
CLASSES = ("critical", "interactive", "research", "maintenance")


@dataclass
class Task:
    task_id: str
    name: str
    type: str                     # class: critical|interactive|research|maintenance
    state: str
    progress: float
    started_ts: float
    updated_ts: float
    error: str | None = None
    payload: dict = field(default_factory=dict)

    def to_dict(self, now: float | None = None) -> dict:
        now = now or time.time()
        return {"id": self.task_id, "name": self.name, "type": self.type,
                "state": self.state, "progress": round(self.progress, 3),
                "age_seconds": round(now - self.started_ts, 1),
                "since_update_seconds": round(now - self.updated_ts, 1),
                "error": self.error, "payload": self.payload}


class TaskCenter:
    def __init__(self, repository=None, *, governor: ResourceGovernor | None = None,
                 max_concurrent_research: int = 2, clock=time.time):
        self.repository = repository
        self.governor = governor
        self.max_concurrent_research = max_concurrent_research
        self.clock = clock
        self._lock = threading.RLock()
        self._tasks: dict[str, Task] = {}

    # -------------------------------------------------------------- create --
    def submit(self, name: str, type_: str, *, payload: dict | None = None) -> Task:
        if type_ not in CLASSES:
            raise ValueError(f"unknown task class '{type_}'")
        now = self.clock()
        task = Task(task_id=str(uuid4()), name=name, type=type_, state="QUEUED",
                    progress=0.0, started_ts=now, updated_ts=now,
                    payload=payload or {})
        with self._lock:
            self._tasks[task.task_id] = task
        self._persist(task)
        return task

    def may_start(self, task_id: str) -> bool:
        """Admission control: research tasks are capped; pressure pauses more."""
        task = self._require(task_id)
        with self._lock:
            if task.state != "QUEUED":
                return False
            if task.type == "research":
                running_research = sum(1 for t in self._tasks.values()
                                       if t.type == "research" and t.state == "RUNNING")
                if running_research >= self.max_concurrent_research:
                    return False
        if self.governor is not None and self.governor.under_pressure() \
                and task.type in ("research", "maintenance"):
            return False
        return self._set_state(task_id, "RUNNING")

    # ------------------------------------------------------------- updates --
    def update_progress(self, task_id: str, progress: float, *,
                        note: str | None = None) -> Task:
        task = self._require(task_id)
        if not 0.0 <= progress <= 1.0:
            raise ValueError("progress must be within [0, 1]")
        with self._lock:
            task.progress = progress
            task.updated_ts = self.clock()
            if note:
                task.payload["note"] = note
        self._persist(task)
        return task

    def pause(self, task_id: str) -> Task:
        task = self._require(task_id)
        if task.state not in ("RUNNING", "QUEUED"):
            raise ValueError(f"cannot pause task in state {task.state}")
        self._set_state(task_id, "PAUSED")
        return self._require(task_id)

    def resume(self, task_id: str) -> Task:
        task = self._require(task_id)
        if task.state != "PAUSED":
            raise ValueError(f"cannot resume task in state {task.state}")
        self._set_state(task_id, "RUNNING")
        return self._require(task_id)

    def cancel(self, task_id: str) -> Task:
        task = self._require(task_id)
        if task.state in ("COMPLETED", "FAILED", "CANCELLED"):
            raise ValueError(f"cannot cancel task in state {task.state}")
        self._set_state(task_id, "CANCELLED")
        return self._require(task_id)

    def complete(self, task_id: str, *, payload: dict | None = None) -> Task:
        task = self._require(task_id)
        with self._lock:
            task.progress = 1.0
            if payload:
                task.payload.update(payload)
        self._set_state(task_id, "COMPLETED")
        return self._require(task_id)

    def fail(self, task_id: str, error: str) -> Task:
        task = self._require(task_id)
        with self._lock:
            task.error = error
        self._set_state(task_id, "FAILED")
        return self._require(task_id)

    # ------------------------------------------------------------- queries --
    def get(self, task_id: str) -> dict | None:
        task = self._tasks.get(task_id)
        return task.to_dict(now=self.clock()) if task else None

    def list(self, *, state: str | None = None, limit: int = 100) -> list[dict]:
        tasks = sorted(self._tasks.values(), key=lambda t: -t.updated_ts)
        if state:
            tasks = [t for t in tasks if t.state == state]
        return [t.to_dict(now=self.clock()) for t in tasks[:limit]]

    def counts(self) -> dict:
        out: dict[str, int] = {}
        for task in self._tasks.values():
            out[task.state] = out.get(task.state, 0) + 1
        return out

    def recover(self) -> int:
        """After a crash/restart, RUNNING tasks with no live worker are not
        silently resumed: they are marked FAILED with a recovery note so a
        duplicate never runs and the user sees exactly what happened."""
        recovered = 0
        with self._lock:
            stuck = [t for t in self._tasks.values()
                     if t.state in ("RUNNING", "QUEUED")]
        for task in stuck:
            with self._lock:
                task.error = "process restarted before task finished"
            self._set_state(task.task_id, "FAILED")
            recovered += 1
        return recovered

    # -------------------------------------------------------------- helper --
    def _require(self, task_id: str) -> Task:
        task = self._tasks.get(task_id)
        if task is None:
            raise KeyError(f"unknown task {task_id}")
        return task

    def _set_state(self, task_id: str, state: str) -> bool:
        if state not in STATES:
            raise ValueError(f"unknown task state '{state}'")
        with self._lock:
            task = self._tasks[task_id]
            task.state = state
            task.updated_ts = self.clock()
            if state in ("COMPLETED", "FAILED", "CANCELLED") and \
                    task.state == "COMPLETED":
                task.progress = 1.0
        self._persist(task)
        return True

    def _persist(self, task: Task) -> None:
        if self.repository is None:
            return
        try:
            self.repository.upsert_task(task_id=task.task_id, name=task.name,
                                        type_=task.type, state=task.state,
                                        progress=task.progress,
                                        payload={"error": task.error, **task.payload})
        except Exception as exc:
            logger.warning("task persistence skipped (in-memory state kept): %s", exc)
