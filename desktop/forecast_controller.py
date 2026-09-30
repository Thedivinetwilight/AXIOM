"""Qt-free controller behind the FORECAST panel and the MARKETS overlay.

All Kronos work happens on the ``ForecastService`` worker thread. The methods
here only queue jobs and drain finished results, so calling them from a Qt timer
never blocks the GUI thread and never waits on Arena polling.

If the ``axiom`` package or a data source is unavailable, the controller degrades
to a DISABLED panel: the rest of the desktop app is unaffected.
"""

from __future__ import annotations

from typing import Any, Callable

from desktop import forecast_viewmodels as fvm


class ForecastUiState:
    """Holds the Kronos stack and renders it as plain rows/series."""

    def __init__(self, clock: Callable[[], Any] | None = None) -> None:
        self.environment = None
        self.service = None
        self.error: str = ""
        self._selected: tuple[str, str] | None = None
        self._clock = clock
        self._last_status: dict = {"state": "DISABLED", "message": "Kronos not configured"}
        self._outcomes: list[dict] = []

    # -- lifecycle -----------------------------------------------------------
    def bootstrap(self, env: dict | None = None) -> "ForecastUiState":
        """Build the stack from environment configuration; never raises."""
        try:
            from axiom.bootstrap import build_forecast_service

            kwargs = {"clock": self._clock} if self._clock is not None else {}
            self.environment = build_forecast_service(env, **kwargs)
        except Exception as exc:  # pragma: no cover - defensive UI boundary
            self.error = f"{type(exc).__name__}: {exc}"
            self._last_status = {
                "state": "ERROR",
                "message": f"Kronos unavailable: {self.error}",
            }
            return self
        self.service = self.environment.service
        if self.service is not None:
            self.service.start()
            self._last_status = self.environment.status.to_dict()
            if self.environment.series:
                self._selected = self.environment.series[0]
                self.service.refresh_if_due(self.environment.series)
        else:
            self._last_status = self.environment.status.to_dict()
        return self

    def shutdown(self) -> None:
        if self.service is not None:
            self.service.stop()

    # -- state ---------------------------------------------------------------
    @property
    def available(self) -> bool:
        return self.service is not None

    @property
    def series(self) -> list[tuple[str, str]]:
        if self.environment is None:
            return []
        return list(self.environment.series)

    def series_labels(self) -> list[str]:
        return [f"{instrument} · {timeframe}" for instrument, timeframe in self.series]

    def selected(self) -> tuple[str, str] | None:
        if self._selected is None and self.series:
            self._selected = self.series[0]
        return self._selected

    def select(self, label: str) -> None:
        for instrument, timeframe in self.series:
            if label == f"{instrument} · {timeframe}":
                self._selected = (instrument, timeframe)
                return

    def notes(self) -> list[str]:
        if self.error:
            return [self.error]
        return list(self.environment.notes) if self.environment else []

    # -- non-blocking refresh ------------------------------------------------
    def refresh(self) -> list[dict]:
        """Queue due refreshes and drain finished outcomes. Never blocks."""
        if self.service is None:
            return []
        selected = self.series
        if selected:
            self.service.refresh_if_due(selected)
        outcomes = [outcome.to_dict() for outcome in self.service.poll()]
        if outcomes:
            self._outcomes = (self._outcomes + outcomes)[-32:]
            self._last_status = self.service.status().to_dict()
        return outcomes

    def recent_outcomes(self) -> list[dict]:
        return list(self._outcomes)

    def status(self) -> dict:
        if self.service is not None:
            self._last_status = self.service.status().to_dict()
        return self._last_status

    # -- rendering -----------------------------------------------------------
    def panel_rows(self) -> list[tuple[str, str]]:
        status = self.status()
        snapshot = self.snapshot_dict()
        rows = fvm.forecast_panel_rows(status, snapshot)
        if not self.available:
            rows = rows + [
                ("AXIOM without Kronos",
                 "the terminal keeps working; forecasting is unavailable"),
            ]
            if self.error:
                rows.append(("Error", self.error))
        for note in self.notes()[:3]:
            rows.append(("Note", note))
        return rows

    def snapshot_dict(self) -> dict | None:
        selected = self.selected()
        if self.service is None or selected is None:
            return None
        snapshot = self.service.latest(*selected)
        return snapshot.to_dict() if snapshot is not None else None

    def table_rows(self) -> list[list[str]]:
        if self.service is None:
            return [["instrument", "tf", "status/quality", "last actual close (ACTUAL)",
                     "forecast close (MODEL)", "horizon return (MODEL)",
                     "endpoint (MODEL)", "latency ms", "updated"]]
        return fvm.forecast_table_rows(
            [snapshot.to_dict() for snapshot in self.service.all_latest()]
        )

    def forecast_candles(self, limit: int = 12) -> list[list[str]]:
        return fvm.forecast_candle_rows(self.snapshot_dict(), limit=limit)

    def overlay(self) -> dict[str, list[tuple[str, float]]]:
        selected = self.selected()
        if self.service is None or selected is None:
            return fvm.overlay_series(None)
        return fvm.overlay_series(self.service.overlay(*selected))

    def chart_series(self) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
        """(actual, forecast) point lists for the QPainter chart widgets."""
        overlay = self.overlay()
        actual = [(index, value) for index, (_, value)
                  in enumerate(fvm.overlay_series(
                      {"actual_close": overlay["ACTUAL close"]})["ACTUAL close"])]
        offset = max(len(actual) - 1, 0)
        forecast = [(offset + index, value) for index, (_, value)
                    in enumerate(overlay["MODEL FORECAST close"])]
        return actual, forecast

    def banner(self) -> str:
        return fvm.FORECAST_BANNER

    def legend(self) -> str:
        return fvm.overlay_legend()
