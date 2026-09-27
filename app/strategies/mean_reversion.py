from dataclasses import dataclass
from app.indicators.moving_averages import check_period
from app.strategies.base import Strategy, Signal, bounded


@dataclass
class MeanReversionStrategy(Strategy):
    period: int = 20
    deviation: float = 0.03
    name = "mean_reversion"

    def __post_init__(self):
        check_period(self.period)
        bounded(self.deviation, 0.000001, 1, "deviation")
        self.history_limit = self.period

    def generate_signal(self, data):
        if len(data) < self.period:
            return Signal.HOLD
        mean = sum(b.close for b in data[-self.period :]) / self.period
        deviation = data[-1].close / mean - 1
        return (
            Signal.BUY
            if deviation < -self.deviation
            else Signal.EXIT
            if deviation >= 0
            else Signal.HOLD
        )
