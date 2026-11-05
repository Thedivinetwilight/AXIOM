"""Authoritative current-session completed bars and exchange-session checks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from app.autonomy.models import Instrument
from app.autonomy.upstox import IST, ReconciliationFailure, UpstoxReadOnly
from app.data.models import Candle, TIMEFRAMES
from app.data.validator import validate


class BarFeed(Protocol):
    def completed(self, instrument: Instrument, timeframe: str) -> list[Candle]: ...


@dataclass(frozen=True)
class SessionWindow:
    start: datetime
    end: datetime

    def contains(self, now: datetime) -> bool:
        return self.start <= now.astimezone(timezone.utc) <= self.end


class ExchangeCalendar:
    """Uses Upstox's date-specific exchange timings, never a guessed session."""

    def __init__(self, reader: UpstoxReadOnly, *, clock=None):
        self.reader = reader
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._cache: dict[str, dict[str, SessionWindow]] = {}

    def windows(self, trading_day: str) -> dict[str, SessionWindow]:
        if trading_day not in self._cache:
            raw = self.reader.market_timings(trading_day)
            self._cache[trading_day] = {
                exchange: SessionWindow(start, end) for exchange, (start, end) in raw.items()
            }
        return self._cache[trading_day]

    def window(self, instrument: Instrument, now: datetime) -> SessionWindow:
        day = now.astimezone(IST).date().isoformat()
        windows = self.windows(day)
        # Upstox returns exchange-level schedules (NSE/BSE/CDS/BCD), not a
        # display-symbol schedule.  Require the exact exchange entry.
        try:
            return windows[instrument.exchange]
        except KeyError:
            raise ReconciliationFailure(
                f"No current exchange session metadata for {instrument.exchange}"
            ) from None

    def is_open(self, instrument: Instrument, now: datetime) -> bool:
        return self.window(instrument, now).contains(now)


class UpstoxCompletedBarFeed:
    """Polling adapter for current-day V3 candles; no replay or fallback feed."""

    def __init__(self, reader: UpstoxReadOnly, *, clock=None):
        self.reader = reader
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def completed(self, instrument: Instrument, timeframe: str) -> list[Candle]:
        if timeframe not in TIMEFRAMES or timeframe == "1D":
            raise ValueError("Unsupported completed-bar timeframe")
        bars = self.reader.intraday_candles(
            instrument.instrument_key, instrument.display_symbol, timeframe
        )
        now = self.clock().astimezone(timezone.utc)
        # Only the current exchange day is eligible.  This also prevents a
        # provider's prior-day history from being mistaken for a live stream.
        current_day = now.astimezone(IST).date()
        bars = [bar for bar in bars if bar.timestamp.astimezone(IST).date() == current_day]
        if not bars:
            raise ReconciliationFailure("No completed current-session bars")
        report = validate(bars, timeframe, strict_gaps=True)
        report.require_valid()
        latest = bars[-1]
        if latest.available_at(timeframe) > now:
            raise ReconciliationFailure("Forming bar reached the completed-bar boundary")
        return bars


def completed_bar_is_fresh(bar: Candle, timeframe: str, now: datetime,
                           stale_after_seconds: float) -> bool:
    age = (now.astimezone(timezone.utc) - bar.available_at(timeframe)).total_seconds()
    return 0 <= age <= stale_after_seconds
