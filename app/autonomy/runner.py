"""Background scanner.  It has no create/Arm/Activate capability."""

from __future__ import annotations

import asyncio
import logging


class AutonomyRunner:
    def __init__(self, service, interval_seconds: float):
        self.service = service
        self.interval_seconds = interval_seconds
        self._task: asyncio.Task | None = None
        self._stopping = False

    async def start(self) -> None:
        if self._task is None:
            self._stopping = False
            self._task = asyncio.create_task(self._run(), name="axiom-autonomy-scanner")

    async def _run(self) -> None:
        while not self._stopping:
            try:
                # Only sessions already activated by an operator in this
                # process are visible here.  Recovery never recreates ACTIVE.
                session_ids = [runtime.session_id for runtime in list(self.service.runtimes.values())
                               if runtime.state == "ACTIVE"]
                for session_id in session_ids:
                    await asyncio.to_thread(self.service.scan_once, session_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A worker error is logged by type only; the service's own
                # fail-closed path handles market/broker faults.
                logging.getLogger("bot.autonomy").error(
                    "autonomy_worker_iteration_failed", extra={"error": type(exc).__name__}
                )
            await asyncio.sleep(self.interval_seconds)

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
