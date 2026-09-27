"""Deterministic OFFLINE market-data feed — V0.6.4 Termux/paper verification.

Purpose: run the complete pipeline

    synthetic feed → normalized MarketEvent → MarketEventApplier
    → LiveCandleBuilder (completed candles ONLY) → market-status gate
    → existing strategies → existing risk engine → PaperBroker → SQLite

entirely offline with ZERO third-party dependencies (pure standard library:
no httpx, protobuf, websockets, fastapi or pydantic), e.g. on Android Termux
or any minimal Python 3.11+.

The feed emits the SAME provider-neutral `MarketEvent` objects as the real
Upstox V3 adapter and folds them through the SAME `MarketEventApplier` +
`LiveCandleBuilder` pipeline, including the market-status gate. It is clearly
labeled SYNTHETIC everywhere (`provider: "synthetic-offline"`) and is never a
substitute for real Upstox verification.

The default scenario is fully deterministic: fixed base epoch, fixed prices,
fixed quantities, a virtual clock instead of wall time, and an embedded
duplicate tick — so every run produces identical candles, signals, orders,
fills and equity. Restart/replay determinism follows from the committed
cursor: replayed candles are ignored exactly like a real feed's duplicates.
"""

import asyncio
from datetime import datetime, timedelta, timezone

from app.data.candles import LiveCandleBuilder
from app.data.market_events import MarketEvent, MarketEventApplier
from app.data.models import TIMEFRAMES, validate_symbol
from app.data.provider import WebSocketProvider

PROVIDER_NAME = "synthetic-offline"

# Fixed base epoch (2026-01-05 09:30:00 UTC): nothing derives from wall time.
BASE_EPOCH = datetime(2026, 1, 5, 9, 30, 0, tzinfo=timezone.utc)

# Default deterministic tick script: (bucket_index, price, quantity).
# Volumes (10k/tick) stay above the max_participation cap so the round
# trip fills completely. Buckets 0–1 tick while the market is still CLOSED; the feed then opens and
# prices rise 100.0 → 100.5 → 101.0 → 101.5 → 102.0 (a momentum BUY), dip to
# 101.0 and 100.75 (exit). One duplicate tick is embedded on purpose.
DEFAULT_TICKS = (
    (0, 100.0, 10000),
    (1, 100.5, 10000),
    (2, 101.0, 10000),
    (3, 101.5, 10000),
    (4, 102.0, 10000),
    (4, 102.0, 10000),  # exact duplicate: must never double-count volume
    (5, 101.0, 10000),
    (6, 100.75, 10000),
)

SEGMENT = "NSE"


def build_scenario(instrument_key: str, timeframe: str = "1m",
                   ticks: tuple = DEFAULT_TICKS) -> list[MarketEvent]:
    """Deterministic event list: closed → suppressed candle → open → session."""
    seconds = TIMEFRAMES[timeframe]
    events: list[MarketEvent] = []

    def at(offset: float) -> datetime:
        return BASE_EPOCH + timedelta(seconds=offset)

    # 1) market CLOSED before any price data (the gate must hold it shut).
    events.append(MarketEvent(
        provider=PROVIDER_NAME, instrument_key="", kind="market_info",
        received_at=at(0), market_status="NORMAL_CLOSE",
        segment_statuses={SEGMENT: "NORMAL_CLOSE"},
    ))
    # 2) two closed-market buckets: the first candle completes while still
    #    closed and is therefore emitted only as a suppressed counter.
    for bucket, price, quantity in ticks[:2]:
        events.append(MarketEvent(
            provider=PROVIDER_NAME, instrument_key=instrument_key, kind="live",
            received_at=at(bucket * seconds + 10),
            exchange_ts=at(bucket * seconds + 10),
            ltp=price, last_traded_quantity=float(quantity),
        ))
    # 3) market OPEN: the documented status that unlocks strategy execution.
    events.append(MarketEvent(
        provider=PROVIDER_NAME, instrument_key="", kind="market_info",
        received_at=at(2 * seconds + 5), market_status="NORMAL_OPEN",
        segment_statuses={SEGMENT: "NORMAL_OPEN"},
    ))
    # 4) session ticks: snapshot then live updates (second entry of bucket 4
    #    is the embedded exact duplicate).
    for index, (bucket, price, quantity) in enumerate(ticks[2:]):
        events.append(MarketEvent(
            provider=PROVIDER_NAME, instrument_key=instrument_key,
            kind="snapshot" if index == 0 else "live",
            received_at=at(bucket * seconds + 10),
            exchange_ts=at(bucket * seconds + 10),
            ltp=price, last_traded_quantity=float(quantity),
        ))
    return events


def key_to_symbol(instrument_key: str) -> str:
    """Same mapping convention as the Upstox adapter (no httpx import needed)."""
    display = instrument_key.replace("|", "-")
    validate_symbol(display)  # rejects spaces/unsupported keys with a clear error
    return display


class SyntheticStream(WebSocketProvider):
    """Offline deterministic provider behind the existing provider interface."""

    supported_timeframes = frozenset(TIMEFRAMES)

    def __init__(self, timeframe: str = "1m", instrument_key: str = "NSE_EQ|INE002A01018",
                 *, scenario: list[MarketEvent] | None = None,
                 require_market_status: bool = True, stale_after: float = 90.0):
        if timeframe not in TIMEFRAMES:
            raise ValueError("Unsupported timeframe for the synthetic feed")
        self.instrument_key = str(instrument_key)
        self.symbol = key_to_symbol(self.instrument_key)
        self.symbols = [self.symbol]
        self.timeframe = timeframe
        self.require_market_status = bool(require_market_status)
        self.closed = False
        self.status = "DISCONNECTED"
        self.last_message: str | None = None
        self.last_candle_time: str | None = None
        self.market_status: str | None = None
        self.market_open: bool | None = None
        self.segment_statuses: dict[str, str] = {}
        self.suppressed_by_market_status = 0
        self.reconnects = 0
        self._builder = LiveCandleBuilder(timeframe, stale_after=stale_after)
        self._applier = MarketEventApplier(
            self._builder, {self.instrument_key: self.symbol},
            on_market_info=self._on_market_info,
        )
        self._events = list(scenario) if scenario is not None else build_scenario(
            self.instrument_key, timeframe
        )

    # -- status handling (shared semantics with the Upstox adapter) ----------

    def _on_market_info(self, segment_statuses: dict[str, str], market_status: str) -> None:
        self.segment_statuses = dict(segment_statuses)
        self.market_status = market_status
        self.market_open = self._relevant_market_open()

    def _relevant_market_open(self) -> bool | None:
        if not self.segment_statuses:
            return None
        for key in (self.instrument_key,):
            exchange = key.split("|", 1)[0].split("_", 1)[0]
            if exchange in self.segment_statuses:
                return self.segment_statuses[exchange] == "NORMAL_OPEN"
        return "NORMAL_OPEN" in self.segment_statuses.values()

    def _candles_allowed(self) -> bool:
        return not self.require_market_status or self.market_open is True

    # -- provider protocol ---------------------------------------------------

    def metrics(self) -> dict:
        counters = self._builder.metrics()
        return {
            "paper_mode": True,
            "provider": PROVIDER_NAME,
            "feed_mode": "deterministic-scenario",
            "timeframe": self.timeframe,
            "connection_status": self.status,
            "authenticated": True,  # offline fixture: nothing to authenticate
            "subscribed": self.status == "CONNECTED",
            "last_message_time": self.last_message,
            "last_candle_time": self.last_candle_time,
            "data_gap_detected": counters["gaps"] > 0,
            "stale_feed": counters["stale_feed"],
            "reconnects": 0,
            "market_status": self.market_status,
            "market_open": self.market_open,
            "segment_statuses": dict(self.segment_statuses),
            "suppressed_by_market_status": self.suppressed_by_market_status,
            "last_tick": None,
            "feed_counters": counters,
            "feed_latency_ms": {"messages": 0, "avg_ms": 0.0, "max_ms": 0.0},
            "instrument_keys": [self.instrument_key],
        }

    async def stream(self, symbols: list[str], timeframe: str):
        """Yield completed candles from the deterministic scenario."""
        if timeframe != self.timeframe:
            raise ValueError("Stream timeframe mismatch")
        if any(symbol not in self.symbols for symbol in symbols):
            raise ValueError("Unsubscribed stream symbol")
        self.status = "CONNECTED"
        try:
            seconds = TIMEFRAMES[self.timeframe]
            for event in self._events:
                if self.closed:
                    break
                received_at = event.received_at.timestamp()
                self._applier.apply(event, received_at)
                self.last_message = event.received_at.isoformat()
                bars = self._builder.collect(now=received_at)
                if bars and not self._candles_allowed():
                    self.suppressed_by_market_status += len(bars)
                    continue
                for bar in bars:
                    if bar.symbol not in symbols:
                        raise ValueError("Unsubscribed stream symbol")
                    yield bar
            # Deterministic final flush at one window past the last event.
            final_now = self._events[-1].received_at.timestamp() + seconds
            bars = self._builder.collect(now=final_now)
            if bars and not self._candles_allowed():
                self.suppressed_by_market_status += len(bars)
            else:
                for bar in bars:
                    if bar.symbol not in symbols:
                        raise ValueError("Unsubscribed stream symbol")
                    yield bar
        finally:
            self.status = "DISCONNECTED"

    async def close(self):
        self.closed = True
        self.status = "DISCONNECTED"


# Requested provider-style alias; identical offline implementation.
SyntheticMarketDataProvider = SyntheticStream
