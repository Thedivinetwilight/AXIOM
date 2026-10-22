"""NYSE regular-session calendar with New York DST and early closes.

This offline calendar is intentionally limited to regular NYSE/Nasdaq sessions.
It is not a substitute for an exchange notice during an exceptional closure.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    last = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _easter(year: int) -> date:
    """Gregorian Easter Sunday (Meeus/Jones/Butcher)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f, g = (b + 8) // 25, (b - (b + 8) // 25 + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    return date(year, month, (h + l - 7 * m + 114) % 31 + 1)


@lru_cache(maxsize=32)
def holidays(year: int) -> frozenset[date]:
    fixed = {date(year, 1, 1), date(year, 7, 4), date(year, 12, 25)}
    if year >= 2022:
        fixed.add(date(year, 6, 19))
    closed = {_observed(day) for day in fixed}
    # New Year's Day observed on Dec 31 belongs to the previous calendar year.
    if date(year + 1, 1, 1).weekday() == 5:
        closed.add(date(year, 12, 31))
    closed.update({
        _nth_weekday(year, 1, 0, 3),       # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),       # Washington's Birthday
        _easter(year) - timedelta(days=2), # Good Friday
        _last_weekday(year, 5, 0),         # Memorial Day
        _nth_weekday(year, 9, 0, 1),       # Labor Day
        _nth_weekday(year, 11, 3, 4),      # Thanksgiving
    })
    return frozenset(closed)


def early_close(day: date) -> bool:
    if day.weekday() >= 5 or day in holidays(day.year):
        return False
    thanksgiving = _nth_weekday(day.year, 11, 3, 4)
    if day == thanksgiving + timedelta(days=1):
        return True
    # The session immediately preceding Independence Day, unless it is itself
    # a scheduled holiday; in that case use the preceding weekday.
    july4 = date(day.year, 7, 4)
    before_july4 = july4 - timedelta(days=1)
    while before_july4.weekday() >= 5 or before_july4 in holidays(day.year):
        before_july4 -= timedelta(days=1)
    if day == before_july4:
        return True
    return day == date(day.year, 12, 24)


class USEquitiesCalendar:
    """Regular US equity session in America/New_York; timestamps are aware."""

    timezone = "America/New_York"
    open_time = time(9, 30)

    def close_time(self, day: date) -> time:
        return time(13, 0) if early_close(day) else time(16, 0)

    def is_open(self, timestamp: datetime) -> bool:
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("US session calendar requires a timezone-aware timestamp")
        local = timestamp.astimezone(ZoneInfo(self.timezone))
        return (
            local.weekday() < 5
            and local.date() not in holidays(local.year)
            and self.open_time <= local.time().replace(tzinfo=None) < self.close_time(local.date())
        )

    def session_bounds(self, day: date) -> tuple[datetime, datetime] | None:
        if day.weekday() >= 5 or day in holidays(day.year):
            return None
        zone = ZoneInfo(self.timezone)
        return (
            datetime.combine(day, self.open_time, zone),
            datetime.combine(day, self.close_time(day), zone),
        )

    def signature(self) -> dict:
        return {"type": "us-equities", "timezone": self.timezone,
                "open": "09:30", "regular_close": "16:00",
                "calendar": "NYSE holiday rules; early close at 13:00"}
