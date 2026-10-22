"""NYSE regular-session calendar with New York DST and early closes.

Rules implemented here are the ones published by NYSE Group in
"NYSE Group Announces 2026, 2027 and 2028 Holiday and Early Closings
Calendar" (2025-12-23) and the historical equivalent for earlier years:

* all times are New York local time, so daylight-saving transitions are
  handled by the IANA ``America/New_York`` rules rather than by an offset;
* regular session 09:30-16:00; early close at 13:00;
* early closes are exactly: the day after Thanksgiving, December 24 when
  December 25 is a weekday, and July 3 when July 4 is a weekday;
* New Year's Day is the one holiday NYSE does *not* observe on the preceding
  Friday: when January 1 falls on a Saturday, no holiday is observed at all
  (NYSE 2028 calendar note *). 2027-12-31 is therefore a trading day.

The offline rule set cannot discover an extraordinary exchange closure
(9/11-style halt, national day of mourning, hurricane). Those are handled by
explicit operator overrides, which are recorded in the calendar signature so a
paper account can never be restored silently under a different closure set.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

NY_TZ = "America/New_York"
OPEN_TIME = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    last = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(day: date) -> date | None:
    """Weekend-observed holiday date, NYSE convention.

    Saturday holidays move to the preceding Friday, Sunday holidays to the
    following Monday. ``None`` means "not observed at all" — only New Year's
    Day uses that (see :func:`holidays`).
    """
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _easter(year: int) -> date:
    """Gregorian Easter Sunday (Meeus/Jones/Butcher)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    g = (b - (b + 8) // 25 + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    return date(year, month, (h + l - 7 * m + 114) % 31 + 1)


@lru_cache(maxsize=64)
def holidays(year: int) -> frozenset[date]:
    """Full-day NYSE closures for ``year`` (calendar year containment)."""
    closed: set[date] = set()
    # New Year's Day: observed on the day itself (Mon-Fri) or the following
    # Monday when it falls on Sunday. A Saturday New Year's Day is NOT observed
    # on the preceding Friday (NYSE 2028 calendar note *).
    new_year = date(year, 1, 1)
    if new_year.weekday() == 5:
        pass  # no observance
    elif new_year.weekday() == 6:
        closed.add(date(year, 1, 2))
    else:
        closed.add(new_year)
    # A New Year's Day observed on December 31 does not exist on NYSE, so no
    # cross-year carry is added here.
    for fixed in (date(year, 7, 4), date(year, 12, 25)):
        weekend_observed = _observed(fixed)
        if weekend_observed is not None:
            closed.add(weekend_observed)
    juneteenth = date(year, 6, 19)
    if year >= 2022:  # NYSE began observing Juneteenth in 2022.
        observed = _observed(juneteenth)
        if observed is not None:
            closed.add(observed)
    closed.update({
        _nth_weekday(year, 1, 0, 3),       # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),       # Washington's Birthday
        _easter(year) - timedelta(days=2),  # Good Friday
        _last_weekday(year, 5, 0),         # Memorial Day
        _nth_weekday(year, 9, 0, 1),       # Labor Day
        _nth_weekday(year, 11, 3, 4),      # Thanksgiving
    })
    return frozenset(closed)


def early_close(day: date) -> bool:
    """True only for the scheduled 13:00 closes published by NYSE."""
    if day.weekday() >= 5 or day in holidays(day.year):
        return False
    thanksgiving = _nth_weekday(day.year, 11, 3, 4)
    if day == thanksgiving + timedelta(days=1):
        return True
    # Christmas Eve is a half day only when Christmas Day itself is a weekday.
    # When Christmas falls at the weekend, December 24 is either the observed
    # full closure (Saturday case) or a weekend day, never a half day.
    if day == date(day.year, 12, 24) and date(day.year, 12, 25).weekday() < 5:
        return True
    # July 3 is a half day only when July 4 is a weekday and observed on the
    # day itself. A Saturday July 4 moves the closure to Friday July 3; a
    # Sunday July 4 moves it to Monday July 5. Neither creates a half day.
    return day == date(day.year, 7, 3) and date(day.year, 7, 4).weekday() < 5


def _parse_override_dates(values) -> frozenset[date]:
    parsed = set()
    for value in values or ():
        text = str(value).strip()
        if not text:
            continue
        try:
            parsed.add(date.fromisoformat(text))
        except ValueError as exc:
            raise ValueError(f"Invalid US session override date: {text!r}") from exc
    return frozenset(parsed)


class USEquitiesCalendar:
    """Regular US equity session in America/New_York; timestamps are aware."""

    timezone = NY_TZ
    open_time = OPEN_TIME

    def __init__(self, *, closed_dates=(), early_close_dates=()):
        # Operator overrides for extraordinary closures / unscheduled half days
        # (exchange notice required). Never inferred from data.
        self.closed_dates = _parse_override_dates(closed_dates)
        self.early_close_dates = _parse_override_dates(early_close_dates)
        overlap = self.closed_dates & self.early_close_dates
        if overlap:
            raise ValueError(
                "A US session date cannot be both fully closed and an early close: "
                + ", ".join(sorted(d.isoformat() for d in overlap))
            )

    def close_time(self, day: date) -> time:
        if day in self.closed_dates:
            # No session at all; callers must use is_open/session_bounds.
            return OPEN_TIME
        if day in self.early_close_dates or early_close(day):
            return EARLY_CLOSE
        return REGULAR_CLOSE

    def is_trading_day(self, day: date) -> bool:
        return day.weekday() < 5 and day not in holidays(day.year) and day not in self.closed_dates

    def is_open(self, timestamp: datetime) -> bool:
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("US session calendar requires a timezone-aware timestamp")
        local = timestamp.astimezone(ZoneInfo(self.timezone))
        day = local.date()
        if not self.is_trading_day(day):
            return False
        local_time = local.time().replace(tzinfo=None)
        return self.open_time <= local_time < self.close_time(day)

    def session_bounds(self, day: date) -> tuple[datetime, datetime] | None:
        if not self.is_trading_day(day):
            return None
        zone = ZoneInfo(self.timezone)
        return (
            datetime.combine(day, self.open_time, zone),
            datetime.combine(day, self.close_time(day), zone),
        )

    def next_session_bounds(self, timestamp: datetime) -> tuple[datetime, datetime] | None:
        """Bounds of the next session that starts strictly after ``timestamp``."""
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("US session calendar requires a timezone-aware timestamp")
        local = timestamp.astimezone(ZoneInfo(self.timezone))
        candidate = local.date()
        for _ in range(30):
            bounds = self.session_bounds(candidate)
            if bounds is not None and bounds[0] > local:
                return bounds
            candidate += timedelta(days=1)
        return None

    def session_date(self, timestamp: datetime) -> date:
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("US session calendar requires a timezone-aware timestamp")
        return timestamp.astimezone(ZoneInfo(self.timezone)).date()

    def signature(self) -> dict:
        return {
            "type": "us-equities",
            "timezone": self.timezone,
            "open": "09:30",
            "regular_close": "16:00",
            "periodic_closes": "13:00 (day after Thanksgiving; Dec 24 when Dec 25 is a weekday; "
                               "Jul 3 when Jul 4 is a weekday)",
            "holidays": "NYSE rule set, generated per year",
            "closed_date_overrides": sorted(d.isoformat() for d in self.closed_dates),
            "early_close_overrides": sorted(d.isoformat() for d in self.early_close_dates),
        }
