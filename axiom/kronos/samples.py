"""Sampled-trajectory summarisation (P10/P50/P90 + dispersion).

Kronos sampling is stochastic, so repeated single-path inference yields a set of
trajectories. Their spread is **sample dispersion**, not a calibrated
probability: ``ForecastUncertainty.calibrated`` is always ``False`` and the UI
labels it as dispersion.

Important upstream detail: ``KronosPredictor.predict(sample_count=N)`` averages
the N sampled paths internally (``np.mean(preds, axis=1)`` in
``auto_regressive_inference``), so a single call can never expose dispersion.
AXIOM therefore repeats single-path inference ``sample_paths`` times instead —
see ``axiom.kronos.backends.UpstreamKronosBackend``.
"""

from __future__ import annotations

from collections.abc import Sequence
from math import isfinite
from statistics import mean, pstdev

from axiom.models import ForecastUncertainty

MIN_PATHS_FOR_UNCERTAINTY = ForecastUncertainty.MIN_UNCERTAINTY_PATHS


def percentile(values: Sequence[float], quantile: float) -> float:
    """Linear-interpolated percentile (matches numpy's default method)."""
    if not values:
        raise ValueError("percentile() needs at least one value")
    if not 0.0 <= quantile <= 1.0:
        raise ValueError("quantile must be within [0, 1]")
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = quantile * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return float(ordered[lower] * (1 - fraction) + ordered[upper] * fraction)


def summarize_paths(
    close_paths: Sequence[Sequence[float]],
) -> ForecastUncertainty | None:
    """Summarise sampled close trajectories.

    Returns ``None`` when fewer than ``MIN_PATHS_FOR_UNCERTAINTY`` independent
    paths are available — with one path there is no dispersion to report and
    showing percentiles would be misleading.
    """
    paths = [list(path) for path in close_paths]
    if len(paths) < MIN_PATHS_FOR_UNCERTAINTY:
        return None
    horizon = min(len(path) for path in paths)
    if horizon < 1:
        return None
    if any(
        not isfinite(value) for path in paths for value in path[:horizon]
    ):
        return None
    terminal = [path[horizon - 1] for path in paths]
    per_step_std = tuple(
        pstdev([path[index] for path in paths]) for index in range(horizon)
    )
    p10, p50, p90 = (percentile(terminal, q) for q in (0.10, 0.50, 0.90))
    width = p90 - p10
    return ForecastUncertainty(
        sample_count=len(paths),
        p10_close=p10,
        p50_close=p50,
        p90_close=p90,
        dispersion_close=mean(per_step_std),
        interval_width_close=width,
        interval_width_pct=(width / p50 if p50 else 0.0),
        mean_path_std=per_step_std,
        close_band=tuple(path_quantile_band(paths, 0.10, 0.90)),
        calibrated=False,
    )


def path_quantile_band(
    close_paths: Sequence[Sequence[float]], lower: float = 0.10, upper: float = 0.90
) -> list[tuple[float, float]]:
    """Per-step (lower, upper) quantile band across sampled paths."""
    horizon = min(len(path) for path in close_paths)
    return [
        (
            percentile([path[index] for path in close_paths], lower),
            percentile([path[index] for path in close_paths], upper),
        )
        for index in range(horizon)
    ]


def interval_coverage(
    close_paths: Sequence[Sequence[float]],
    actual: Sequence[float],
    lower: float = 0.10,
    upper: float = 0.90,
) -> float | None:
    """Empirical coverage of the sampled interval against realised closes.

    Reported as *empirical* coverage — it is a diagnostic for research, not a
    calibrated probability statement.
    """
    if len(close_paths) < MIN_PATHS_FOR_UNCERTAINTY:
        return None
    band = path_quantile_band(close_paths, lower, upper)
    horizon = min(len(band), len(actual))
    if horizon < 1:
        return None
    hits = sum(
        1
        for index in range(horizon)
        if band[index][0] <= actual[index] <= band[index][1]
    )
    return hits / horizon


def mean_interval_width(
    close_paths: Sequence[Sequence[float]], reference: float | None = None
) -> float | None:
    """Average interval width, optionally normalised by a reference level."""
    if len(close_paths) < MIN_PATHS_FOR_UNCERTAINTY:
        return None
    band = path_quantile_band(close_paths)
    widths = [upper - lower for lower, upper in band]
    average = mean(widths)
    if reference:
        return average / reference
    return average
