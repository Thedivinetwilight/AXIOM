"""Walk-forward evaluation of Kronos against naive baselines.

The evaluator is origin-anchored and strictly leak-free: for every fold the
model only sees bars whose timestamp is *before* the origin, and each fold
asserts that the last observed timestamp precedes the first predicted one. The
assertions are recorded in the report, so a leak shows up as a failed check
rather than a flattering score.

Forecasting quality only — no P&L, no fees, no fills.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from axiom.market.provider import MarketDataBar, MarketDataError
from axiom.market.validation import validate_market_data
from axiom.models import KronosConfig
from axiom.research.baselines import BASELINES, run_baseline
from axiom.research.metrics import ForecastMetrics, aggregate, compute_metrics

#: (bars, future timestamps) → sampled close trajectories
ForecasterFn = Callable[[Sequence[MarketDataBar], Sequence[datetime]], Sequence[Sequence[float]]]


@dataclass(frozen=True)
class WalkForwardStep:
    origin_index: int
    origin_timestamp: datetime
    last_actual_close: float
    future_timestamps: tuple[datetime, ...]
    actual: tuple[float, ...]
    predictions: dict[str, tuple[float, ...]]
    kronos_paths: tuple[tuple[float, ...], ...] = ()
    leakage_ok: bool = True
    note: str = ""

    def metrics(self) -> dict[str, ForecastMetrics]:
        out: dict[str, ForecastMetrics] = {}
        for name, predicted in self.predictions.items():
            out[name] = compute_metrics(
                predicted,
                self.actual,
                last_actual=self.last_actual_close,
                paths=self.kronos_paths if name == "kronos" else None,
            )
        return out


@dataclass(frozen=True)
class WalkForwardReport:
    instrument: str
    timeframe: str
    steps: tuple[WalkForwardStep, ...]
    models: dict[str, ForecastMetrics] = field(default_factory=dict)
    leakage_ok: bool = True
    notes: tuple[str, ...] = ()

    @property
    def folds(self) -> int:
        return len(self.steps)

    def to_dict(self) -> dict:
        return {
            "instrument": self.instrument,
            "timeframe": self.timeframe,
            "folds": self.folds,
            "leakage_ok": self.leakage_ok,
            "models": {name: metrics.to_dict() for name, metrics in self.models.items()},
            "notes": list(self.notes),
            "steps": [
                {
                    "origin_index": step.origin_index,
                    "origin_timestamp": step.origin_timestamp.isoformat(),
                    "last_actual_close": step.last_actual_close,
                    "horizon": len(step.actual),
                    "leakage_ok": step.leakage_ok,
                    "metrics": {
                        name: metrics.to_dict() for name, metrics in step.metrics().items()
                    },
                }
                for step in self.steps
            ],
        }


class WalkForwardEvaluator:
    """Origin-anchored walk-forward comparison."""

    def __init__(
        self,
        bars: Sequence[MarketDataBar],
        config: KronosConfig,
        *,
        instrument: str = "",
        timeframe: str = "",
        kronos_forecaster: ForecasterFn | None = None,
        baselines: Sequence[str] = ("last_close", "drift", "moving_average"),
        stride: int = 1,
        max_folds: int | None = None,
    ) -> None:
        self.bars = list(bars)
        self.config = config.validate()
        self.instrument = instrument or (self.bars[0].instrument if self.bars else "")
        self.timeframe = timeframe
        self.kronos_forecaster = kronos_forecaster
        unknown = [name for name in baselines if name not in BASELINES]
        if unknown:
            raise ValueError(f"unknown baseline(s): {', '.join(unknown)}")
        self.baselines = tuple(baselines)
        if stride < 1:
            raise ValueError("stride must be >= 1")
        self.stride = stride
        self.max_folds = max_folds

    # -- fold planning -------------------------------------------------------
    def origins(self) -> list[int]:
        """Valid origin indices: enough history before, enough truth after."""
        minimum = max(self.config.min_history, 2)
        horizon = self.config.pred_len
        if len(self.bars) < minimum + horizon:
            return []
        candidates = list(range(minimum, len(self.bars) - horizon + 1, self.stride))
        if self.max_folds and len(candidates) > self.max_folds:
            candidates = candidates[-self.max_folds:]
        return candidates

    # -- evaluation ----------------------------------------------------------
    def run(self) -> WalkForwardReport:
        # Validate before scoring so malformed prices cannot be silently hidden
        # by non-finite metric aggregation. Gaps remain explicit research notes;
        # this evaluator has no exchange calendar unless its caller supplies one.
        quality = validate_market_data(
            self.bars,
            self.timeframe,
            min_history=1,
            strict_gaps=False,
            expected_instrument=self.instrument or None,
        )
        if not quality.ok:
            detail = "; ".join(quality.errors[:8])
            raise MarketDataError(f"walk-forward data rejected: {detail}")

        notes: list[str] = list(quality.warnings)
        if self.kronos_forecaster is None:
            notes.append("no Kronos forecaster supplied; baselines only")
        origins = self.origins()
        if not origins:
            notes.append(
                f"not enough history for one fold: {len(self.bars)} bars, need "
                f"{max(self.config.min_history, 2) + self.config.pred_len}"
            )
            return WalkForwardReport(
                self.instrument, self.timeframe, steps=(), notes=tuple(notes)
            )

        steps: list[WalkForwardStep] = []
        leakage_ok = True
        for origin in origins:
            window = self.bars[:origin][-self.config.lookback:]
            future = self.bars[origin:origin + self.config.pred_len]
            future_timestamps = tuple(bar.timestamp for bar in future)
            actual = tuple(bar.close for bar in future)
            # Hard leak check: the window must end strictly before the horizon.
            step_leak_ok = bool(window) and window[-1].timestamp < future_timestamps[0]
            leakage_ok = leakage_ok and step_leak_ok

            predictions: dict[str, tuple[float, ...]] = {}
            for name in self.baselines:
                predictions[name] = tuple(run_baseline(name, window, len(actual)))

            paths: tuple[tuple[float, ...], ...] = ()
            if self.kronos_forecaster is not None:
                sampled = self.kronos_forecaster(window, list(future_timestamps))
                paths = tuple(tuple(float(v) for v in path) for path in sampled)
                if not paths:
                    notes.append(f"Kronos returned no path at origin {origin}")
                else:
                    horizon = min(len(path) for path in paths)
                    predictions["kronos"] = tuple(_median_column(paths, horizon))

            steps.append(
                WalkForwardStep(
                    origin_index=origin,
                    origin_timestamp=window[-1].timestamp if window else future_timestamps[0],
                    last_actual_close=window[-1].close if window else actual[0],
                    future_timestamps=future_timestamps,
                    actual=actual,
                    predictions=predictions,
                    kronos_paths=paths,
                    leakage_ok=step_leak_ok,
                )
            )

        per_model: dict[str, list[ForecastMetrics]] = {}
        for step in steps:
            for name, metrics in step.metrics().items():
                per_model.setdefault(name, []).append(metrics)
        models = {name: aggregate(items) for name, items in sorted(per_model.items())}
        if not leakage_ok:
            notes.append("LEAKAGE DETECTED: a fold saw data at or after its origin")
        return WalkForwardReport(
            instrument=self.instrument,
            timeframe=self.timeframe,
            steps=tuple(steps),
            models=models,
            leakage_ok=leakage_ok,
            notes=tuple(notes),
        )


def _median_column(paths: Sequence[Sequence[float]], horizon: int) -> list[float]:
    """Per-step median across sampled paths (point forecast for scoring)."""
    out = []
    for index in range(horizon):
        column = sorted(path[index] for path in paths)
        middle = len(column) // 2
        if len(column) % 2:
            out.append(column[middle])
        else:
            out.append((column[middle - 1] + column[middle]) / 2.0)
    return out


def runtime_forecaster(
    runner, config: KronosConfig
) -> ForecasterFn:
    """Adapt a KronosRuntime/KronosManager into an evaluation forecaster.

    ``runner`` must expose ``forecast_paths`` (runtime) or ``forecast``
    (manager). Returns sampled close trajectories.
    """

    def forecast(bars: Sequence[MarketDataBar], future: Sequence[datetime]):
        if hasattr(runner, "forecast_paths"):
            inference = runner.forecast_paths(bars, future)
        else:
            inference = runner.forecast(bars, future)
        return inference.close_paths()

    return forecast
