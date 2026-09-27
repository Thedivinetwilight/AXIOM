from dataclasses import dataclass
from statistics import pstdev
from app.indicators.moving_averages import check_period
from app.strategies.base import Strategy, Signal, bounded


@dataclass
class BollingerStrategy(Strategy):
    period: int = 20
    std_dev: float = 2.0
    name = "bollinger"

    def __post_init__(self):
        check_period(self.period)
        bounded(self.std_dev, 0.1, 10, "std_dev")
        self.history_limit = self.period

    def generate_signal(self, data):
        if len(data) < self.period:
            return Signal.HOLD
        values = [b.close for b in data[-self.period :]]
        mean, std = sum(values) / self.period, pstdev(values)
        return (
            Signal.BUY
            if values[-1] < mean - self.std_dev * std
            else Signal.EXIT
            if values[-1] >= mean
            else Signal.HOLD
        )
