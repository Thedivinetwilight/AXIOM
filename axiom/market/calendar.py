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


#: How many nominal steps a single gap between projected slots may consume, for
#: a calendar whose closed periods scale with the timeframe (24/7 markets).
MAX_GAP_STEPS = 10

#: How long a single gap may be for a calendar whose closed periods are fixed in
#: wall-clock terms (sessions, weekends, holiday weeks) and therefore do *not*
#: scale with the timeframe — an overnight gap is the same wall-clock length
#: whether the bars are 1m or 1h.
MAX_GAP_DAYS = 10


def require_aware(timestamp: datetime, context: str) -> datetime:
    """Reject a naive timestamp instead of guessing its offset.

    ``datetime.timestamp()`` and ``.date()`` both silently reinterpret a naive
    value in the machine's local zone, so the same instant would get a different
    verdict on two machines. Every other AXIOM boundary already refuses naive
    stamps; the calendars must not be the one place that assumes.
    """
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise MarketDataError(
            f"{context}: timestamp {timestamp.isoformat()} has no timezone; "
            "refusing to assume one"
        )
    return timestamp


class TradingCalendar(ABC):
    """Decides which bar-open timestamps are legitimate."""

    name: str = "calendar"

    @abstractmethod
    def is_valid_bar_time(self, timestamp: datetime, timeframe: str) -> bool:
        """True when a bar may legitimately OPEN at ``timestamp``."""

    def advance(self, timestamp: datetime, timeframe: str, *, direction: int = 1) -> datetime:
        """Move ``timestamp`` by one nominal ``timeframe`` step.

        The default is absolute (UTC) arithmetic, which is correct for a 24/7
        calendar. A calendar whose slots are defined by local wall-clock time
        must override this: adding an absolute 86400s across a DST transition
        turns 09:30 local into 10:30 local, and a slot check expressed in local
        time then never matches again until the offset reverts.
        """
        step = TIMEFRAME_SECONDS[timeframe]
        return timestamp + timedelta(seconds=step * direction)

    def next_valid(self, timestamp: datetime, timeframe: str) -> datetime:
        """First valid bar-open at or after ``timestamp``."""
        require_aware(timestamp, self.name)
        cursor = timestamp
        for _ in range(DEFAULT_MAX_SCAN):
            if self.is_valid_bar_time(cursor, timeframe):
                return cursor
            cursor = self.advance(cursor, timeframe)
        raise MarketDataError(
            f"{self.name}: no valid {timeframe} slot at/after "
            f"{timestamp.isoformat()} within {DEFAULT_MAX_SCAN} steps"
        )

    def previous_valid(self, timestamp: datetime, timeframe: str) -> datetime:
        """Last valid bar-open at or before ``timestamp``."""
        require_aware(timestamp, self.name)
        cursor = timestamp
        for _ in range(DEFAULT_MAX_SCAN):
            if self.is_valid_bar_time(cursor, timeframe):
                return cursor
            cursor = self.advance(cursor, timeframe, direction=-1)
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
        require_aware(last_closed, self.name)
        stamps: list[datetime] = []
        cursor = self.next_valid(self.advance(last_closed, timeframe), timeframe)
        for _ in range(DEFAULT_MAX_SCAN):
            stamps.append(cursor)
            if len(stamps) == count:
                self._check_gaps(stamps, last_closed, timeframe)
                return stamps
            cursor = self.next_valid(self.advance(cursor, timeframe), timeframe)
        raise MarketDataError(
            f"{self.name}: cannot project {count} {timeframe} timestamps after "
            f"{last_closed.isoformat()}"
        )

    def max_gap(self, timeframe: str) -> timedelta:
        """Longest legitimate gap between two consecutive projected slots.

        The default suits a calendar whose closed periods scale with the
        timeframe. Session and daily calendars override it: an overnight or
        weekend gap is a fixed wall-clock length regardless of bar size.
        """
        return timedelta(seconds=TIMEFRAME_SECONDS[timeframe] * MAX_GAP_STEPS)

    def _check_gaps(
        self, stamps: list[datetime], last_closed: datetime, timeframe: str
    ) -> None:
        """Fail loudly if any gap is far longer than the calendar can explain.

        Without this, a calendar defect returns timestamps that look plausible
        but sit months out — and ``validate_forecast`` only checks that they are
        strictly increasing, so nothing downstream would notice.
        """
        limit = self.max_gap(timeframe)
        previous = last_closed
        for stamp in stamps:
            gap = stamp - previous
            if gap > limit:
                raise MarketDataError(
                    f"{self.name}: gap of {gap} before {stamp.isoformat()} exceeds "
                    f"the maximum explainable gap of {limit} for {timeframe} — "
                    "refusing to emit implausible timestamps"
                )
            previous = stamp


class ContinuousCalendar(TradingCalendar):
    """24/7 markets (e.g. crypto spot): every slot is a trading slot."""

    name = "continuous-24x7"

    def is_valid_bar_time(self, timestamp: datetime, timeframe: str) -> bool:
        step = TIMEFRAME_SECONDS[timeframe]
        # .timestamp() on a naive stamp assumes the machine's local zone, so the
        # same wall-clock string would align or not depending on where it runs.
        return int(require_aware(timestamp, self.name).timestamp()) % step == 0


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
        # .date() on a naive stamp would mean "in the machine's local zone".
        day = require_aware(timestamp, self.name).astimezone(UTC).date()
        return day.weekday() in self.weekdays and day not in self.holidays

    def max_gap(self, timeframe: str) -> timedelta:
        """A weekend or holiday week is a wall-clock span, not N daily steps."""
        return timedelta(days=MAX_GAP_DAYS)


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

    def advance(self, timestamp: datetime, timeframe: str, *, direction: int = 1) -> datetime:
        """Step in local wall-clock time so a session open stays a session open.

        Absolute arithmetic would move a 09:30 EST open to 10:30 EDT across a
        DST transition, and ``is_valid_bar_time`` (which compares local times)
        would then reject every subsequent day until the offset reverts — the
        scan would silently return slots months away.
        """
        step = TIMEFRAME_SECONDS[timeframe]
        local = self._local(timestamp)
        if timeframe == "1D":
            stepped = local + timedelta(days=direction)
        else:
            stepped = local + timedelta(seconds=step * direction)
        # Re-anchor to the same wall-clock time: replacing the folded offset with
        # a fresh tz-aware construction lets the zone supply the correct offset.
        return stepped.replace(tzinfo=None).replace(tzinfo=self.timezone).astimezone(UTC)

    def _is_trading_day(self, timestamp: datetime) -> bool:
        local = self._local(timestamp)
        return local.weekday() in self.weekdays and local.date() not in self.holidays

    def max_gap(self, timeframe: str) -> timedelta:
        """An overnight or weekend gap does not scale with the bar size."""
        return timedelta(days=MAX_GAP_DAYS)

    def is_valid_bar_time(self, timestamp: datetime, timeframe: str) -> bool:
        require_aware(timestamp, self.name)
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
