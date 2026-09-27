from dataclasses import dataclass
from app.indicators.moving_averages import check_period, sma
from app.strategies.base import Strategy, Signal, crossing


@dataclass
class MovingAverageStrategy(Strategy):
    fast_period: int = 20
    slow_period: int = 50
    name = "moving_average"

    def __post_init__(self):
        check_period(self.fast_period)
        check_period(self.slow_period)
        if self.fast_period >= self.slow_period:
            raise ValueError("fast_period must be smaller than slow_period")
        self.history_limit = self.slow_period + 1

    def generate_signal(self, data):
        if len(data) < self.history_limit:
            return Signal.HOLD
        closes = [b.close for b in data[-self.history_limit :]]
        fast, slow = sma(closes, self.fast_period), sma(closes, self.slow_period)
        return crossing(fast[-2], fast[-1], slow[-2], slow[-1])
