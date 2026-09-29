"""Session/exchange calendars for honest timestamp handling.

Kronos consumes calendar features (minute, hour, weekday, day, month) computed
from the timestamps AXIOM supplies, so future timestamps must respect the
exchange/session calendar. Slots that are closed are *skipped*, never invented:
AXIOM never fabricates overnight, weekend or holiday candles.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, time, timedelta
from typing import Iterable

from axiom.market.provider import MarketDataError
from axiom.models import TIMEFRAME_SECONDS, UTC

# Bound on calendar scanning so a misconfigured calendar cannot hang a worker.
DEFAULT_MAX_SCAN = 5000


class TradingCalendar(ABC):
    """Decides which bar-open timestamps are legitimate."""

    name: str = "calendar"

    @abstractmethod
    def is_valid_bar_time(self, timestamp: datetime, timeframe: str) -> bool:
        """True when a bar may legitimately OPEN at ``timestamp``."""

    def next_valid(self, timestamp: datetime, timeframe: str) -> datetime:
        """First valid bar-open at or after ``timestamp``."""
        step = TIMEFRAME_SECONDS[timeframe]
        cursor = timestamp
        for _ in range(DEFAULT_MAX_SCAN):
            if self.is_valid_bar_time(cursor, timeframe):
                return cursor
            cursor += timedelta(seconds=step)
        raise MarketDataError(
            f"{self.name}: no valid {timeframe} slot at/after "
            f"{timestamp.isoformat()} within {DEFAULT_MAX_SCAN} steps"
        )

    def previous_valid(self, timestamp: datetime, timeframe: str) -> datetime:
        """Last valid bar-open at or before ``timestamp``."""
        step = TIMEFRAME_SECONDS[timeframe]
        cursor = timestamp
        for _ in range(DEFAULT_MAX_SCAN):
            if self.is_valid_bar_time(cursor, timeframe):
                return cursor
            cursor -= timedelta(seconds=step)
        raise MarketDataError(
            f"{self.name}: no valid {timeframe} slot at/before "
            f"{timestamp.isoformat()} within {DEFAULT_MAX_SCAN} steps"
        )

    def project_future_timestamps(
        self, last_closed: datetime, timeframe: str, count: int
    ) -> list[datetime]:
        """The next ``count`` legitimate bar-open timestamps after a closed bar.

        Closed slots are skipped. Nothing is fabricated: if the calendar cannot
        supply enough slots the call raises instead of padding.
        """
        if count < 1:
            raise MarketDataError("count must be >= 1")
        step = TIMEFRAME_SECONDS[timeframe]
        stamps: list[datetime] = []
        cursor = self.next_valid(last_closed + timedelta(seconds=step), timeframe)
        for _ in range(DEFAULT_MAX_SCAN):
            stamps.append(cursor)
            if len(stamps) == count:
                return stamps
            cursor = self.next_valid(cursor + timedelta(seconds=step), timeframe)
        raise MarketDataError(
            f"{self.name}: cannot project {count} {timeframe} timestamps after "
            f"{last_closed.isoformat()}"
        )


class ContinuousCalendar(TradingCalendar):
    """24/7 markets (e.g. crypto spot): every slot is a trading slot."""

    name = "continuous-24x7"

    def is_valid_bar_time(self, timestamp: datetime, timeframe: str) -> bool:
        step = TIMEFRAME_SECONDS[timeframe]
        return int(timestamp.timestamp()) % step == 0


class DailyCalendar(TradingCalendar):
    """One bar per trading day (weekday/holiday rules, no intraday session)."""

    def __init__(
        self,
        name: str = "daily",
        weekdays: Iterable[int] = range(5),
        holidays: Iterable[datetime] = (),
    ) -> None:
        self.name = name
        self.weekdays = frozenset(weekdays)
        self.holidays = frozenset(h.date() if isinstance(h, datetime) else h for h in holidays)

    def is_valid_bar_time(self, timestamp: datetime, timeframe: str) -> bool:
        if timeframe != "1D":
            raise MarketDataError(f"{self.name} only supports the 1D timeframe")
        day = timestamp.date()
        return day.weekday() in self.weekdays and day not in self.holidays


class SessionCalendar(TradingCalendar):
    """Intraday session calendar: weekday + time-of-day window (+ holidays).

    ``1D`` bars are valid only at the session open of a trading day.
    """

    def __init__(
        self,
        name: str,
        *,
        open_time: time = time(0, 0),
        close_time: time = time(23, 59),
        weekdays: Iterable[int] = range(7),
        holidays: Iterable[datetime] = (),
        timezone=UTC,
    ) -> None:
        self.name = name
        self.open_time = open_time
        self.close_time = close_time
        self.weekdays = frozenset(weekdays)
        self.holidays = frozenset(h.date() if isinstance(h, datetime) else h for h in holidays)
        self.timezone = timezone

    def _local(self, timestamp: datetime) -> datetime:
        return timestamp.astimezone(self.timezone)

    def _is_trading_day(self, timestamp: datetime) -> bool:
        local = self._local(timestamp)
        return local.weekday() in self.weekdays and local.date() not in self.holidays

    def is_valid_bar_time(self, timestamp: datetime, timeframe: str) -> bool:
        if not self._is_trading_day(timestamp):
            return False
        local = self._local(timestamp)
        if timeframe == "1D":
            return local.time() == self.open_time
        return self.open_time <= local.time() < self.close_time

    def session_open(self, timestamp: datetime) -> datetime:
        local = self._local(timestamp).replace(
            hour=self.open_time.hour,
            minute=self.open_time.minute,
            second=0,
            microsecond=0,
        )
        return local.astimezone(UTC)
