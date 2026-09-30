"""Forecast validation and candle construction.

Model output is validated before it is ever stored or drawn:

* hard errors (shape, alignment, non-finite values) reject the forecast;
* structural oddities (high < max(open, close), negative forecast volume) are
  counted and reported as warnings — they are never silently repaired.

When several sampled paths exist, the trajectory that is stored/drawn is a real
sampled path selected by median horizon close. Averaging fields independently
would break OHLC consistency; dispersion across all paths is reported
separately in ``ForecastUncertainty``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from math import isfinite

from axiom.kronos.backends import KronosInference
from axiom.models import FORECAST_ORIGIN, ForecastCandle, KronosConfig

FORECAST_FIELDS = ("open", "high", "low", "close", "volume")


@dataclass(frozen=True)
class ForecastValidation:
    ok: bool
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    paths: int = 0
    steps: int = 0

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "paths": self.paths,
            "steps": self.steps,
        }


def validate_forecast(
    inference: KronosInference,
    future_timestamps: Sequence[datetime],
    config: KronosConfig,
) -> ForecastValidation:
    errors: list[str] = []
    warnings: list[str] = []
    paths = inference.paths

    if not paths:
        return ForecastValidation(False, errors=("model returned no trajectory",))
    if len(paths) != config.sample_paths:
        errors.append(
            f"expected {config.sample_paths} sampled path(s), got {len(paths)}"
        )
    if len(future_timestamps) != config.pred_len:
        errors.append(
            f"expected {config.pred_len} future timestamps, got {len(future_timestamps)}"
        )
    if any(b <= a for a, b in zip(future_timestamps, future_timestamps[1:])):
        errors.append("future timestamps must be strictly increasing")

    ohlc_violations = 0
    negative_volume = 0
    for path_index, path in enumerate(paths):
        if len(path) != len(future_timestamps):
            errors.append(
                f"path {path_index} has {len(path)} steps, expected "
                f"{len(future_timestamps)}"
            )
            continue
        for step_index, step in enumerate(path):
            missing = [f for f in FORECAST_FIELDS if f not in step]
            if missing:
                errors.append(
                    f"path {path_index} step {step_index} is missing "
                    f"{', '.join(missing)}"
                )
                continue
            values = {name: step[name] for name in FORECAST_FIELDS}
            if not all(isfinite(value) for value in values.values()):
                errors.append(
                    f"path {path_index} step {step_index} has non-finite values: "
                    f"{values!r}"
                )
                continue
            if values["high"] < max(values["open"], values["close"]) or values["low"] > min(
                values["open"], values["close"]
            ) or values["high"] < values["low"]:
                ohlc_violations += 1
            if values["volume"] < 0:
                negative_volume += 1

    if ohlc_violations:
        warnings.append(
            f"{ohlc_violations} forecast step(s) violate OHLC relationships "
            "(reported, not repaired)"
        )
    if negative_volume:
        warnings.append(
            f"{negative_volume} forecast step(s) have negative forecast volume "
            "(reported, not repaired)"
        )
    return ForecastValidation(
        ok=not errors,
        errors=tuple(errors),
        warnings=tuple(warnings),
        paths=len(paths),
        steps=len(future_timestamps),
    )


def median_path_index(paths: Sequence[Sequence[dict[str, float]]]) -> int:
    """Index of the path whose horizon close is the median across paths."""
    if len(paths) <= 1:
        return 0
    terminals = sorted((path[-1]["close"], index) for index, path in enumerate(paths))
    return terminals[len(terminals) // 2][1]


def build_forecast_candles(
    paths: Sequence[Sequence[dict[str, float]]],
    future_timestamps: Sequence[datetime],
) -> tuple[ForecastCandle, ...]:
    """Build the labelled forecast trajectory (median path when sampled)."""
    if not paths:
        raise ValueError("cannot build forecast candles without a trajectory")
    chosen = median_path_index(paths)
    path = paths[chosen]
    if len(path) != len(future_timestamps):
        raise ValueError(
            f"trajectory length {len(path)} != {len(future_timestamps)} timestamps"
        )
    return tuple(
        ForecastCandle(
            timestamp=timestamp,
            forecast_open=step["open"],
            forecast_high=step["high"],
            forecast_low=step["low"],
            forecast_close=step["close"],
            forecast_volume=step["volume"],
            origin=FORECAST_ORIGIN,
        )
        for timestamp, step in zip(future_timestamps, path)
    )
