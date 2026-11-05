"""Explicit no-automation strategy for reviewed manual paper trading."""

from dataclasses import dataclass

from app.strategies.base import Signal, Strategy


@dataclass(frozen=True)
class ManualOnlyStrategy(Strategy):
    name = "manual_only"
    history_limit = 2

    def generate_signal(self, data) -> Signal:
        # Market data can update marks/risk state, but never creates a signal order.
        return Signal.HOLD
