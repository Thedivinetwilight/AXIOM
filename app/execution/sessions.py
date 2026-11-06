"""Trading-session windows, evaluated without a network call.

Each broker profile carries its own regular session in the exchange's own
timezone. AXIOM refuses to submit outside that window.

What this module deliberately does **not** claim:

* It does not know exchange holidays. A holiday looks like a normal weekday to
  ``SessionWindow.state``; the broker rejects the order and the rejection is
  recorded as ``REJECTED`` with the broker's own reason. Alpaca additionally
  exposes ``GET /v2/clock`` (``is_open``), and the Alpaca readiness check
  consults it when credentials are present. Upstox has no documented public
  trading-clock endpoint, so its readiness check relies on the window below
  plus the account/market-data endpoints.
* It does not model pre-open, auction, or after-hours sessions. Those are
  documented by both brokers and are listed as unsupported in their profiles.
* It does not read the system clock inside tests: ``now`` is always injectable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

WEEKDAYS = (0, 1, 2, 3, 4)  # Monday..Friday


@dataclass(frozen=True)
class SessionWindow:
    """A regular cash-equity session in one exchange timezone."""

    label: str
    timezone: str
    open_time: time
    close_time: time
    weekdays: tuple[int, ...] = WEEKDAYS

    def _local(self, now: datetime) -> datetime:
        tz = ZoneInfo(self.timezone)
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        return now.astimezone(tz)

    def state(self, now: datetime | None = None) -> dict[str, Any]:
        moment = now or datetime.now(timezone.utc)
        local = self._local(moment)
        weekday = local.weekday()
        opens = datetime.combine(local.date(), self.open_time, tzinfo=local.tzinfo)
        closes = datetime.combine(local.date(), self.close_time, tzinfo=local.tzinfo)
        if weekday not in self.weekdays:
            is_open = False
            reason = "weekend"
            next_open = self._next_open(local)
        elif local < opens:
            is_open = False
            reason = "before_open"
            next_open = opens
        elif local >= closes:
            is_open = False
            reason = "after_close"
            next_open = self._next_open(local)
        else:
            is_open = True
            reason = "open"
            next_open = None
        return {
            "label": self.label,
            "timezone": self.timezone,
            "open": is_open,
            "reason": reason,
            "local_time": local.isoformat(),
            "session_date": local.date().isoformat(),
            "opens_at": opens.isoformat(),
            "closes_at": closes.isoformat(),
            "next_open": None if next_open is None else next_open.isoformat(),
        }

    def _next_open(self, local: datetime) -> datetime | None:
        for offset in range(0, 8):
            day = local.date() + timedelta(days=offset)
            candidate = datetime.combine(day, self.open_time, tzinfo=local.tzinfo)
            if candidate.weekday() in self.weekdays and candidate > local:
                return candidate
        return None

    def is_open(self, now: datetime | None = None) -> bool:
        return bool(self.state(now)["open"])

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "timezone": self.timezone,
            "open_time": self.open_time.strftime("%H:%M"),
            "close_time": self.close_time.strftime("%H:%M"),
            "weekdays": list(self.weekdays),
            "holidays_known": False,
            "note": "exchange holidays and early closes are not modelled; the broker rejects those orders",
        }
