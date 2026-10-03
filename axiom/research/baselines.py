"""Naive baselines Kronos must beat to be worth its compute.

Every baseline only uses information available at the forecast origin — the same
closed window Kronos sees. That is what makes the comparison meaningful.
"""

from __future__ import annotations

from collections.abc import Sequence

from axiom.market.provider import MarketDataBar

BaselineFn = "callable"


def last_close_forecast(
    bars: Sequence[MarketDataBar], horizon: int
) -> list[float]:
    """Flat line at the last observed close (the benchmark to beat)."""
    if not bars:
        raise ValueError("last_close_forecast needs at least one bar")
    return [bars[-1].close] * horizon


def drift_forecast(bars: Sequence[MarketDataBar], horizon: int) -> list[float]:
    """Linear extrapolation of the average per-bar change over the window."""
    if len(bars) < 2:
        return last_close_forecast(bars, horizon)
    closes = [bar.close for bar in bars]
    drift = (closes[-1] - closes[0]) / (len(closes) - 1)
    return [closes[-1] + drift * (step + 1) for step in range(horizon)]


def moving_average_forecast(
    bars: Sequence[MarketDataBar], horizon: int, *, window: int = 20
) -> list[float]:
    """Statistical baseline: mean-reversion toward the trailing mean."""
    if not bars:
        raise ValueError("moving_average_forecast needs at least one bar")
    closes = [bar.close for bar in bars]
    tail = closes[-window:] if window else closes
    average = sum(tail) / len(tail)
    # Half-life style decay toward the mean, flat after convergence.
    out = []
    level = closes[-1]
    for _ in range(horizon):
        level = level + (average - level) * 0.25
        out.append(level)
    return out


def seasonal_naive_forecast(
    bars: Sequence[MarketDataBar], horizon: int, *, season: int = 24
) -> list[float]:
    """Repeat the last full season (useful for intraday bars)."""
    if not bars:
        raise ValueError("seasonal_naive_forecast needs at least one bar")
    closes = [bar.close for bar in bars]
    if len(closes) < season:
        return last_close_forecast(bars, horizon)
    last_season = closes[-season:]
    return [last_season[step % season] for step in range(horizon)]


BASELINES = {
    "last_close": last_close_forecast,
    "drift": drift_forecast,
    "moving_average": moving_average_forecast,
}


def baseline_names() -> list[str]:
    return sorted(BASELINES)


def run_baseline(name: str, bars: Sequence[MarketDataBar], horizon: int) -> list[float]:
    if name not in BASELINES:
        raise ValueError(
            f"unknown baseline {name!r} (available: {', '.join(baseline_names())})"
        )
    return BASELINES[name](bars, horizon)
