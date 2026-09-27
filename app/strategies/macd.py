from dataclasses import dataclass
from app.indicators.moving_averages import check_period
from app.indicators.oscillators import macd
from app.strategies.base import Strategy, Signal, crossing


@dataclass
class MACDStrategy(Strategy):
    fast_period: int = 12
    slow_period: int = 26
    signal_period: int = 9
    name = "macd"

    def __post_init__(self):
        for value in (self.fast_period, self.slow_period, self.signal_period):
            check_period(value)
        if self.fast_period >= self.slow_period:
            raise ValueError("fast_period must be smaller than slow_period")
        self.history_limit = 5 * (self.slow_period + self.signal_period) + 2

    def generate_signal(self, data):
        if len(data) < self.slow_period + self.signal_period:
            return Signal.HOLD
        line, signal = macd(
            [b.close for b in data[-self.history_limit :]],
            self.fast_period,
            self.slow_period,
            self.signal_period,
        )
        return crossing(line[-2], line[-1], signal[-2], signal[-1])
