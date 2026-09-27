from dataclasses import dataclass
from app.indicators.moving_averages import check_period
from app.strategies.base import Strategy, Signal


@dataclass
class BreakoutStrategy(Strategy):
    period: int = 20
    name = "breakout"

    def __post_init__(self):
        check_period(self.period)
        self.history_limit = self.period + 1

    def generate_signal(self, data):
        if len(data) < self.history_limit:
            return Signal.HOLD
        previous = data[-self.history_limit : -1]  # Exclude current bar from breakout band.
        if data[-1].close > max(b.high for b in previous):
            return Signal.BUY
        if data[-1].close < min(b.low for b in previous):
            return Signal.EXIT
        return Signal.HOLD
