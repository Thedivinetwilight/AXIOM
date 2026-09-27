from abc import ABC, abstractmethod
from dataclasses import asdict
from enum import Enum
from math import isfinite
from app.data.models import Candle


class Signal(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    EXIT = "EXIT"


class Strategy(ABC):
    name: str
    history_limit: int = 1000

    @property
    def parameters(self) -> dict:
        return asdict(self)

    @abstractmethod
    def generate_signal(self, data: list[Candle]) -> Signal:
        """Only completed candles may be passed in; SELL and EXIT close longs."""


def bounded(value: float, low: float, high: float, name: str):
    if not isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")


def crossing(a_previous, a_current, b_previous, b_current):
    if a_previous <= b_previous and a_current > b_current:
        return Signal.BUY
    if a_previous >= b_previous and a_current < b_current:
        return Signal.SELL
    return Signal.HOLD
