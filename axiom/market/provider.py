"""AXIOM market data contract (independent of Arena).

Arena 3.0.5 does not expose authoritative OHLCV/candle endpoints for Kronos, so
AXIOM owns its own *read-only* market data contract. This is a boundary type,
not a re-implementation of Arena's data stack: it never touches order routing,
and Kronos output never flows back into it.

Two rules shape everything here:

* Providers return only what they were given — nobody invents bars.
* Bars carry timezone-aware UTC OPEN timestamps and an explicit
  ``origin=MARKET_OBSERVED`` tag, so observations and forecasts stay distinct.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from math import isfinite
from typing import Any

from axiom.models import MARKET_ORIGIN, TIMEFRAME_SECONDS, require_aware_utc, utcnow


class MarketDataError(RuntimeError):
    """Raised when a provider cannot deliver a usable, honest window."""


@dataclass(frozen=True)
class MarketDataBar:
    """One observed candle. ``timestamp`` is the bar OPEN time (UTC)."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    instrument: str
    origin: str = MARKET_ORIGIN

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", require_aware_utc(self.timestamp))
        if (not isinstance(self.instrument, str) or not self.instrument.strip()
                or self.instrument.strip() != self.instrument):
            raise ValueError(
                "MarketDataBar.instrument must be a non-empty trimmed string")
        if self.origin != MARKET_ORIGIN:
            raise ValueError(
                f"MarketDataBar.origin must be {MARKET_ORIGIN!r}, got {self.origin!r}"
            )

    @property
    def is_forecast(self) -> bool:
        return False

    def available_at(self, timeframe: str) -> datetime:
        """The instant this bar becomes CLOSED and therefore usable."""
        return self.timestamp + timedelta(seconds=TIMEFRAME_SECONDS[timeframe])

    def is_closed_at(self, timeframe: str, now: datetime) -> bool:
        return self.available_at(timeframe) <= now

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["timestamp"] = self.timestamp.isoformat()
        return payload

    @classmethod
    def from_row(cls, row: dict[str, Any], instrument: str = "") -> MarketDataBar:
        """Parse one mapping; malformed rows raise instead of being repaired."""
        try:
            timestamp = row["timestamp"]
            raw_values = {name: row[name]
                          for name in ("open", "high", "low", "close", "volume")}
            if any(isinstance(value, bool) for value in raw_values.values()):
                raise ValueError("OHLCV values must be numeric, not boolean")
            values = {name: float(value) for name, value in raw_values.items()}
            if isinstance(timestamp, datetime):
                parsed = timestamp
            else:
                parsed = datetime.fromisoformat(str(timestamp))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("timestamp must be timezone-aware")
        except (KeyError, TypeError, ValueError) as exc:
            raise MarketDataError(f"malformed OHLCV row: {row!r} ({exc})") from exc
        if "symbol" in row:
            raw_symbol = row["symbol"]
        elif "instrument" in row:
            raw_symbol = row["instrument"]
        else:
            raw_symbol = instrument
        if (not isinstance(raw_symbol, str) or not raw_symbol.strip()
                or raw_symbol.strip() != raw_symbol):
            raise MarketDataError(
                "row instrument identity must be a non-empty trimmed string")
        return cls(parsed, **values, instrument=raw_symbol)

    @property
    def prices_finite(self) -> bool:
        return all(
            isfinite(value)
            for value in (self.open, self.high, self.low, self.close, self.volume)
        )


@dataclass(frozen=True)
class MarketDataResult:
    """A provider response, with provenance attached."""

    instrument: str
    timeframe: str
    bars: tuple[MarketDataBar, ...]
    source: str
    provider: str
    retrieved_at: datetime = field(default_factory=utcnow)
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (not isinstance(self.instrument, str) or not self.instrument.strip()
                or self.instrument.strip() != self.instrument):
            raise MarketDataError(
                "provider result instrument must be a non-empty trimmed string")
        if not isinstance(self.timeframe, str) or self.timeframe not in TIMEFRAME_SECONDS:
            raise MarketDataError(f"unsupported timeframe: {self.timeframe}")
        if not isinstance(self.source, str) or not self.source.strip():
            raise MarketDataError("provider result source must be a non-empty string")
        if not isinstance(self.provider, str) or not self.provider.strip():
            raise MarketDataError("provider result name must be a non-empty string")
        if not isinstance(self.bars, (tuple, list)):
            raise MarketDataError("provider result bars must be an ordered sequence")
        bars = tuple(self.bars)
        if any(not isinstance(bar, MarketDataBar) for bar in bars):
            raise MarketDataError("provider result contains a non-MarketDataBar value")
        if any(bar.instrument != self.instrument for bar in bars):
            raise MarketDataError("provider result bars do not match its instrument")
        object.__setattr__(self, "bars", bars)
        try:
            object.__setattr__(self, "retrieved_at",
                               require_aware_utc(self.retrieved_at, "retrieved_at"))
        except (TypeError, ValueError, OverflowError) as exc:
            raise MarketDataError(f"provider result retrieved_at is invalid: {exc}") from exc

    @property
    def first_timestamp(self) -> datetime | None:
        return self.bars[0].timestamp if self.bars else None

    @property
    def last_timestamp(self) -> datetime | None:
        return self.bars[-1].timestamp if self.bars else None

    @property
    def last_closed(self) -> MarketDataBar | None:
        return self.bars[-1] if self.bars else None


class MarketDataProvider(ABC):
    """Read-only source of recorded candles.

    Implementations must:

    * return only CLOSED, recorded bars (partial bars are dropped upstream by
      the closed-candle gate, but providers must not synthesise them either);
    * never fill gaps, resample or repair values — that is the validator's job
      to *report*, and the pipeline's job to reject;
    * honour ``end`` as an exclusive upper bound (this is what makes
      walk-forward evaluation leak-free).
    """

    name: str = "provider"
    supported_timeframes: frozenset[str] = frozenset(TIMEFRAME_SECONDS)

    def __init__(self, source: str = "unknown") -> None:
        self.source = source

    def check_timeframe(self, timeframe: str) -> None:
        if not isinstance(timeframe, str) or timeframe not in TIMEFRAME_SECONDS:
            raise MarketDataError(f"unknown timeframe: {timeframe}")
        if timeframe not in self.supported_timeframes:
            raise MarketDataError(
                f"{self.name} does not support timeframe {timeframe} "
                f"(supported: {', '.join(sorted(self.supported_timeframes))})"
            )

    def validate_fetch_args(self, instrument, timeframe, end, limit):
        """Validate query identity/time bounds once for every provider."""
        if (not isinstance(instrument, str) or not instrument.strip()
                or instrument.strip() != instrument):
            raise MarketDataError("instrument must be a non-empty trimmed string")
        self.check_timeframe(timeframe)
        if end is not None:
            try:
                end = require_aware_utc(end, "market-data end")
            except (TypeError, ValueError, OverflowError) as exc:
                raise MarketDataError(f"market-data end timestamp is invalid: {exc}") from exc
        if limit is not None and (
                isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
            raise MarketDataError("limit must be a positive integer")
        return end, limit

    @abstractmethod
    def fetch(
        self,
        instrument: str,
        timeframe: str,
        *,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> MarketDataResult:
        """Return recorded bars for ``[?, end)`` in ascending order."""

    def describe(self) -> str:
        return f"{self.name}({self.source})"
