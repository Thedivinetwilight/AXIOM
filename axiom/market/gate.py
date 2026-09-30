"""Closed-candle gate.

Kronos must only ever see CLOSED historical candles. The gate:

* computes the open time of the most recent bar that is closed *now*;
* splits a provider window into closed / still-forming bars;
* decides whether the input window actually changed (so the UI refresh loop
  does not re-run inference on every snapshot poll).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from axiom.market.calendar import ContinuousCalendar, TradingCalendar
from axiom.market.provider import MarketDataBar
from axiom.models import TIMEFRAME_SECONDS, UTC


@dataclass(frozen=True)
class ClosedBarWindow:
    closed: tuple[MarketDataBar, ...]
    incomplete: tuple[MarketDataBar, ...]
    last_closed_timestamp: datetime | None
    now: datetime

    @property
    def last_closed_bar(self) -> MarketDataBar | None:
        return self.closed[-1] if self.closed else None

    @property
    def bar_count(self) -> int:
        return len(self.closed)


class ClosedCandleGate:
    """Timeframe + calendar aware closed-bar filter."""

    def __init__(self, timeframe: str, calendar: TradingCalendar | None = None) -> None:
        if timeframe not in TIMEFRAME_SECONDS:
            raise ValueError(f"unknown timeframe: {timeframe}")
        self.timeframe = timeframe
        self.step = TIMEFRAME_SECONDS[timeframe]
        self.calendar = calendar or ContinuousCalendar()

    def floor_to_bar(self, moment: datetime) -> datetime:
        """Open time of the bar that contains ``moment``."""
        seconds = int(moment.timestamp())
        aligned = (seconds // self.step) * self.step
        return datetime.fromtimestamp(aligned, UTC)

    def last_closed_open_time(self, now: datetime) -> datetime:
        """Open time of the most recent CLOSED bar as of ``now``."""
        candidate = self.floor_to_bar(now) - timedelta(seconds=self.step)
        return self.calendar.previous_valid(candidate, self.timeframe)

    def is_closed(self, bar: MarketDataBar, now: datetime) -> bool:
        return bar.timestamp + timedelta(seconds=self.step) <= now

    def apply(
        self,
        bars: list[MarketDataBar] | tuple[MarketDataBar, ...],
        now: datetime,
    ) -> ClosedBarWindow:
        closed = tuple(bar for bar in bars if self.is_closed(bar, now))
        incomplete = tuple(bar for bar in bars if not self.is_closed(bar, now))
        last_closed = closed[-1].timestamp if closed else None
        return ClosedBarWindow(closed, incomplete, last_closed, now)

    def window_changed(
        self, previous_last_closed: datetime | None, current_last_closed: datetime | None
    ) -> bool:
        """A new closed candle is the only legitimate reason to re-run Kronos."""
        return previous_last_closed != current_last_closed
