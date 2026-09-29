"""ForecastService — the only Kronos entry point the desktop UI may use.

The GUI thread never blocks: it calls ``request_refresh()`` (queue a job) and
``poll()`` (drain finished results). All provider I/O, gate checks and Kronos
inference happen on this service's worker thread. Arena polling in the UI is
completely independent — nothing here waits on Kronos.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime

from axiom.forecast.pipeline import ForecastOutcome, ForecastPipeline
from axiom.forecast.store import ForecastStore
from axiom.models import (
    ForecastSnapshot,
    KronosConfig,
    KronosState,
    KronosStatus,
    MarketDataQuality,
    utcnow,
)
from axiom.worker.manager import KronosManager


@dataclass(frozen=True)
class ForecastJob:
    instrument: str
    timeframe: str
    force: bool = False
    submitted_at: datetime | None = None


class ForecastService:
    """Background forecast orchestration with non-blocking reads."""

    def __init__(
        self,
        pipeline: ForecastPipeline,
        *,
        manager: KronosManager | None = None,
        clock: Callable[[], datetime] = utcnow,
        max_pending: int = 64,
    ) -> None:
        self.pipeline = pipeline
        self.manager = manager
        self.clock = clock
        self.store: ForecastStore = pipeline.store
        self.config: KronosConfig = pipeline.config
        self._jobs: "queue.Queue[ForecastJob]" = queue.Queue(maxsize=max_pending)
        self._results: "queue.Queue[ForecastOutcome]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._pending: list[ForecastJob] = []
        self._lock = threading.RLock()
        self._status: KronosStatus = KronosStatus.disabled(
            "Kronos service not started"
        )

    # -- lifecycle -----------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> "ForecastService":
        if self.running:
            return self
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._work, name="axiom-forecast-worker", daemon=True
        )
        self._thread.start()
        return self

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None
        if self.manager is not None:
            self.manager.shutdown(timeout=timeout)

    def _work(self) -> None:
        if self.manager is not None:
            self._status = self.manager.load()
        while not self._stop.is_set():
            try:
                job = self._jobs.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                outcome = self.pipeline.run(
                    job.instrument, job.timeframe, now=self.clock(), force=job.force
                )
            except Exception as exc:  # a bug must not kill the worker thread
                outcome = ForecastOutcome(
                    state=KronosState.ERROR,
                    snapshot=None,
                    quality=MarketDataQuality.unavailable(
                        f"forecast service error: {type(exc).__name__}"
                    ),
                    message=f"forecast service error: {type(exc).__name__}: {exc}",
                )
            with self._lock:
                if job in self._pending:
                    self._pending.remove(job)
            self._results.put(outcome)

    # -- GUI-facing API (never blocks) ---------------------------------------
    def request_refresh(
        self, instrument: str, timeframe: str, *, force: bool = False
    ) -> bool:
        """Queue a refresh. Returns False when the queue is full."""
        job = ForecastJob(instrument, timeframe, force, self.clock())
        try:
            self._jobs.put_nowait(job)
        except queue.Full:
            return False
        with self._lock:
            self._pending.append(job)
        if not self.running:
            self.start()
        return True

    def poll(self, limit: int = 32) -> list[ForecastOutcome]:
        """Drain completed outcomes (called from the UI timer)."""
        outcomes: list[ForecastOutcome] = []
        while len(outcomes) < limit:
            try:
                outcomes.append(self._results.get_nowait())
            except queue.Empty:
                break
        return outcomes

    def pending_jobs(self) -> list[ForecastJob]:
        with self._lock:
            return list(self._pending)

    @property
    def queued(self) -> int:
        return self._jobs.qsize()

    # -- reads ---------------------------------------------------------------
    def status(self) -> KronosStatus:
        if self.manager is not None and self.manager.alive:
            self._status = self.manager.status()
        return self._status

    def latest(self, instrument: str, timeframe: str) -> ForecastSnapshot | None:
        return self.store.latest(instrument, timeframe)

    def all_latest(self) -> list[ForecastSnapshot]:
        return self.store.all_latest()

    def state_for(self, instrument: str, timeframe: str) -> KronosState:
        return self.store.state_for(
            instrument,
            timeframe,
            self.status().state,
            now=self.clock(),
            stale_after=self.config.stale_after_seconds,
        )

    def refresh_if_due(
        self, series: Iterable[tuple[str, str]], *, force: bool = False
    ) -> list[tuple[str, str]]:
        """Queue refreshes; the pipeline itself decides whether to re-run.

        The closed-candle gate + cache key mean an unchanged window costs one
        dictionary lookup, so the UI can call this on every poll.
        """
        queued = []
        for instrument, timeframe in series:
            if self.request_refresh(instrument, timeframe, force=force):
                queued.append((instrument, timeframe))
        return queued

    def overlay(
        self, instrument: str, timeframe: str
    ) -> dict[str, list[tuple[str, float]]]:
        """Chart series for MARKETS: actual and forecast are separate keys.

        Key names are explicitly labelled so no drawing code can confuse the
        two, and the actual series comes from the observed closes stored with
        the snapshot — no I/O happens on the GUI thread.
        """
        empty = {
            "actual_close": [],
            "forecast_close": [],
            "forecast_close_p10": [],
            "forecast_close_p90": [],
        }
        snapshot = self.store.latest(instrument, timeframe)
        if snapshot is None:
            return empty
        actual = [(stamp.isoformat(), value) for stamp, value in snapshot.context_tail]
        forecast = [
            (stamp.isoformat(), value) for stamp, value in snapshot.forecast_close_series()
        ]
        band: dict[str, list[tuple[str, float]]] = {
            "forecast_close_p10": [],
            "forecast_close_p90": [],
        }
        uncertainty = snapshot.uncertainty
        if uncertainty is not None and uncertainty.supported:
            stamps = [stamp.isoformat() for stamp, _ in snapshot.forecast_close_series()]
            band = {
                "forecast_close_p10": [
                    (stamp, pair[0]) for stamp, pair in zip(stamps, uncertainty.close_band)
                ],
                "forecast_close_p90": [
                    (stamp, pair[1]) for stamp, pair in zip(stamps, uncertainty.close_band)
                ],
            }
        return {
            "actual_close": actual,
            "forecast_close": forecast,
            **band,
        }

    def describe(self) -> dict:
        return {
            "running": self.running,
            "queued": self.queued,
            "pending": [
                {"instrument": j.instrument, "timeframe": j.timeframe, "force": j.force}
                for j in self.pending_jobs()
            ],
            "status": self.status().to_dict(),
            "store": self.store.stats,
        }
