"""Chronological splitting; no random shuffling, no test-set parameter selection."""

from dataclasses import dataclass
from math import isclose, isfinite
from app.data.validator import validate


@dataclass(frozen=True)
class SplitConfig:
    train: float = 0.6
    validation: float = 0.2
    test: float = 0.2

    def __post_init__(self):
        if any(not isfinite(v) or not 0 < v < 1 for v in (self.train, self.validation, self.test)):
            raise ValueError("Split percentages must be positive fractions")
        if not isclose(self.train + self.validation + self.test, 1.0):
            raise ValueError("Split percentages must sum to 1")


def split_data(candles, config: SplitConfig | None = None, timeframe="1D"):
    config = config or SplitConfig()
    validate(candles, timeframe).require_valid()
    if len({b.symbol for b in candles}) != 1:
        raise ValueError("Split a single symbol at a time")
    first = int(len(candles) * config.train)
    second = first + int(len(candles) * config.validation)
    if min(first, second - first, len(candles) - second) < 2:
        raise ValueError("At least two candles required in each split")
    return candles[:first], candles[first:second], candles[second:]
