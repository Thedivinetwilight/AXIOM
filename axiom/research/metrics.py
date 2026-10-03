"""Forecast quality metrics (research only).

These measure FORECASTING quality, not trading profitability: no fees, no fills,
no positions. Keeping the two apart is deliberate — a good forecaster is not
automatically a profitable strategy and the UI never implies otherwise.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from math import isfinite, sqrt

from axiom.kronos.samples import interval_coverage, mean_interval_width


@dataclass(frozen=True)
class ForecastMetrics:
    n: int
    mae: float
    rmse: float
    mape_pct: float | None
    directional_accuracy: float | None
    horizon_return_error: float | None
    range_error: float | None
    coverage: float | None = None
    interval_width: float | None = None
    dispersion: float | None = None

    def to_dict(self) -> dict:
        return {
            "n": self.n,
            "mae": _round(self.mae),
            "rmse": _round(self.rmse),
            "mape_pct": _round(self.mape_pct),
            "directional_accuracy": _round(self.directional_accuracy),
            "horizon_return_error": _round(self.horizon_return_error),
            "range_error": _round(self.range_error),
            "coverage": _round(self.coverage),
            "interval_width": _round(self.interval_width),
            "dispersion": _round(self.dispersion),
        }


def _round(value: float | None, digits: int = 8) -> float | None:
    if value is None or not isfinite(value):
        return None
    return round(float(value), digits)


def mean_absolute_error(predicted: Sequence[float], actual: Sequence[float]) -> float:
    _same_length(predicted, actual)
    return sum(abs(p - a) for p, a in zip(predicted, actual)) / len(actual)


def root_mean_squared_error(predicted: Sequence[float], actual: Sequence[float]) -> float:
    _same_length(predicted, actual)
    return sqrt(sum((p - a) ** 2 for p, a in zip(predicted, actual)) / len(actual))


def mean_absolute_percentage_error(
    predicted: Sequence[float], actual: Sequence[float]
) -> float | None:
    """MAPE in percent. Undefined when any actual value is zero."""
    _same_length(predicted, actual)
    if any(a == 0 for a in actual):
        return None
    return 100.0 * sum(abs((p - a) / a) for p, a in zip(predicted, actual)) / len(actual)


def directional_accuracy(
    predicted: Sequence[float], actual: Sequence[float], last_actual: float
) -> float | None:
    """Fraction of steps whose sign vs the last observed close is correct."""
    _same_length(predicted, actual)
    if not actual:
        return None
    hits = sum(
        1
        for p, a in zip(predicted, actual)
        if _sign(p - last_actual) == _sign(a - last_actual)
    )
    return hits / len(actual)


def horizon_return_error(
    predicted: Sequence[float], actual: Sequence[float], last_actual: float
) -> float | None:
    """Difference between forecast and realised horizon return."""
    if not predicted or not actual or not last_actual:
        return None
    forecast_return = predicted[-1] / last_actual - 1.0
    realised_return = actual[-1] / last_actual - 1.0
    return forecast_return - realised_return


def range_error(
    predicted_high: Sequence[float],
    predicted_low: Sequence[float],
    actual_high: Sequence[float],
    actual_low: Sequence[float],
) -> float | None:
    """Mean absolute error of the predicted bar RANGE (high - low)."""
    if not predicted_high:
        return None
    _same_length(predicted_high, actual_high)
    _same_length(predicted_low, actual_low)
    predicted = [h - low for h, low in zip(predicted_high, predicted_low)]
    actual = [h - low for h, low in zip(actual_high, actual_low)]
    return sum(abs(p - a) for p, a in zip(predicted, actual)) / len(actual)


def compute_metrics(
    predicted: Sequence[float],
    actual: Sequence[float],
    *,
    last_actual: float | None = None,
    paths: Sequence[Sequence[float]] | None = None,
    predicted_high: Sequence[float] | None = None,
    predicted_low: Sequence[float] | None = None,
    actual_high: Sequence[float] | None = None,
    actual_low: Sequence[float] | None = None,
) -> ForecastMetrics:
    """Point-forecast metrics, plus interval metrics when paths are supplied."""
    if not actual:
        raise ValueError("compute_metrics needs at least one actual value")
    reference = last_actual if last_actual else actual[0]
    coverage = None
    width = None
    dispersion = None
    if paths:
        coverage = interval_coverage(paths, actual)
        width = mean_interval_width(paths, reference=reference or None)
        dispersion = _dispersion(paths)
    return ForecastMetrics(
        n=len(actual),
        mae=mean_absolute_error(predicted, actual),
        rmse=root_mean_squared_error(predicted, actual),
        mape_pct=mean_absolute_percentage_error(predicted, actual),
        directional_accuracy=directional_accuracy(predicted, actual, reference),
        horizon_return_error=horizon_return_error(predicted, actual, reference),
        range_error=(
            range_error(predicted_high, predicted_low, actual_high, actual_low)
            if predicted_high and predicted_low and actual_high and actual_low
            else None
        ),
        coverage=coverage,
        interval_width=width,
        dispersion=dispersion,
    )


def _dispersion(paths: Sequence[Sequence[float]]) -> float | None:
    from statistics import pstdev

    if len(paths) < 2:
        return None
    horizon = min(len(path) for path in paths)
    if horizon < 1:
        return None
    per_step = [pstdev([path[i] for path in paths]) for i in range(horizon)]
    return sum(per_step) / len(per_step)


def aggregate(metrics: Sequence[ForecastMetrics]) -> ForecastMetrics:
    """Macro-average a set of per-fold metrics."""
    if not metrics:
        raise ValueError("aggregate needs at least one ForecastMetrics")

    def average(field: str) -> float | None:
        values = [getattr(m, field) for m in metrics]
        values = [v for v in values if v is not None and isfinite(v)]
        return sum(values) / len(values) if values else None

    return ForecastMetrics(
        n=sum(m.n for m in metrics),
        mae=average("mae") or 0.0,
        rmse=average("rmse") or 0.0,
        mape_pct=average("mape_pct"),
        directional_accuracy=average("directional_accuracy"),
        horizon_return_error=average("horizon_return_error"),
        range_error=average("range_error"),
        coverage=average("coverage"),
        interval_width=average("interval_width"),
        dispersion=average("dispersion"),
    )


def _same_length(predicted: Sequence[float], actual: Sequence[float]) -> None:
    if len(predicted) != len(actual):
        raise ValueError(
            f"length mismatch: {len(predicted)} predicted vs {len(actual)} actual"
        )


def _sign(value: float) -> int:
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0
