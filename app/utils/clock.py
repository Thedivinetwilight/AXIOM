"""One clock for the whole system: UTC.

The trading day, report windows, snapshot dates and history bounds are all
defined in UTC everywhere else in this codebase (``datetime.now(timezone.utc)``
appears in 36 modules). A handful of call sites used ``date.today()``, which is
the LOCAL calendar date — so on a host set to IST (UTC+5:30, the natural choice
for Indian equities) the date is already tomorrow between 00:00 and 05:30 local
time while the market data is still the UTC day. That silently shifted report
windows and history bounds by one day on exactly the machines most likely to run
this system.

Use these helpers for "what day is it" questions. Keep using
``datetime.now(timezone.utc)`` for timestamps.
"""

from __future__ import annotations

from datetime import date, datetime, timezone


def utc_now() -> datetime:
    """Timezone-aware current UTC time."""
    return datetime.now(timezone.utc)


def utc_today() -> date:
    """The CURRENT UTC CALENDAR DATE.

    Prefer this over ``date.today()`` anywhere the date bounds a data window,
    labels a snapshot, or describes a trading day.
    """
    return datetime.now(timezone.utc).date()


def utc_today_iso() -> str:
    """``utc_today()`` as an ISO-8601 string (YYYY-MM-DD)."""
    return utc_today().isoformat()
