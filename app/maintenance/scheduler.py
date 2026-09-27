"""MaintenanceScheduler: rate-aware daily jobs (universe sync, coverage,
dataset refresh, research). Deterministic: jobs run when `due(now)` says so;
a job never runs twice inside its interval. Used by the 24/7 engine loop and
`maintenance run` CLI; tests inject a clock."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

logger = logging.getLogger("bot.maintenance")


class MaintenanceScheduler:
    def __init__(self, jobs: list[dict], *, min_gap: timedelta = timedelta(hours=20)):
        # jobs: [{"name": str, "fn": callable, "interval": timedelta}]
        self.jobs = sorted(jobs, key=lambda job: job["name"])
        self.min_gap = min_gap
        self._last_run: dict[str, datetime] = {}

    def due(self, now: datetime) -> list[str]:
        pending = []
        for job in self.jobs:
            last = self._last_run.get(job["name"])
            interval = job.get("interval") or self.min_gap
            if last is None or now - last >= interval:
                pending.append(job["name"])
        return pending

    def run_due(self, now: datetime | None = None) -> dict:
        now = now or datetime.now()
        results = {}
        for name in self.due(now):
            job = next(job for job in self.jobs if job["name"] == name)
            try:
                results[name] = {"status": "complete", "result": job["fn"]()}
                self._last_run[name] = now
            except Exception as exc:
                # Rate-aware backoff: treat the failure as a run so the next
                # attempt waits at least half the interval.
                self._last_run[name] = now - (job.get("interval") or self.min_gap) / 2
                results[name] = {"status": "failed", "error": type(exc).__name__}
                logger.warning("MAINTENANCE_JOB_FAILED", extra={"job": name})
        return results
