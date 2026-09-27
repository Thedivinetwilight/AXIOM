from dataclasses import dataclass
from app.indicators.moving_averages import check_period
from app.indicators.momentum import roc
from app.strategies.base import Strategy, Signal, bounded


@dataclass
class MomentumStrategy(Strategy):
    period: int = 20
    threshold: float = 0.02
    name = "momentum"

    def __post_init__(self):
        check_period(self.period)
        bounded(self.threshold, 0, 1, "threshold")
        self.history_limit = self.period + 1

    def generate_signal(self, data):
        if len(data) < self.history_limit:
            return Signal.HOLD
        change = roc([b.close for b in data[-self.history_limit :]], self.period)[-1]
        return (
            Signal.BUY
            if change > self.threshold
            else Signal.EXIT
            if change < -self.threshold
            else Signal.HOLD
        )
