from dataclasses import dataclass
from app.indicators.moving_averages import check_period
from app.indicators.oscillators import rsi
from app.strategies.base import Strategy, Signal, bounded


@dataclass
class RSIStrategy(Strategy):
    period: int = 14
    lower: float = 30
    upper: float = 70
    name = "rsi"

    def __post_init__(self):
        check_period(self.period)
        bounded(self.lower, 0, 100, "lower")
        bounded(self.upper, 0, 100, "upper")
        if self.lower >= self.upper:
            raise ValueError("lower must be smaller than upper")
        self.history_limit = 5 * self.period + 1

    def generate_signal(self, data):
        if len(data) <= self.period:
            return Signal.HOLD
        value = rsi([b.close for b in data[-self.history_limit :]], self.period)[-1]
        return (
            Signal.BUY if value < self.lower else Signal.EXIT if value > self.upper else Signal.HOLD
        )
