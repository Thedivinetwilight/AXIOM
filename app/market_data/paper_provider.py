"""Fail-closed MT5 closed-bar provider for the local paper simulator."""

from __future__ import annotations

import asyncio
from hashlib import sha256

from app.data.models import TIMEFRAMES
from app.market_data.mt5 import (
    MarketDataStale, MarketDataUnavailable, PROVIDER_NAME, SUPPORTED_TIMEFRAMES, utcnow,
)


class MT5PaperProvider:
    """Streams only newly validated MT5 closed bars; never backfills decisions.

    ``prepare`` proves fresh advancing ticks and stages at most the latest
    completed bar.  Older chart history is never fed to strategy/order logic.
    """

    requires_fresh_live = True
    provider_name = "vantage_mt5"

    def __init__(self, service, symbol: str, timeframe: str, *, poll_seconds: float = 0.5):
        if timeframe not in SUPPORTED_TIMEFRAMES:
            raise ValueError(
                "Unsupported MT5 paper timeframe; choose 1m, 5m, 15m, 30m, 1h, 4h or 1D"
            )
        self.service = service
        self.symbol = symbol
        self.timeframe = timeframe
        self.symbols = [symbol]
        self.supported_timeframes = SUPPORTED_TIMEFRAMES
        self.poll_seconds = float(poll_seconds)
        self.stale_after_seconds = max(
            service.max_quote_age_seconds, TIMEFRAMES[timeframe] * 2
        )
        self.status = "DISCONNECTED"
        self.last_message = "Not prepared"
        self._pending = []
        self._provenance = {}
        self._last_bar_timestamp = None
        self._closed = False
        self._last_quote = None
        self._last_candle = None
        self._data_gap = False

    def _stage(self, candle):
        received_at = utcnow().isoformat()
        identity = "|".join((
            PROVIDER_NAME, candle.symbol, self.timeframe,
            candle.timestamp.isoformat(), str(candle.open), str(candle.high),
            str(candle.low), str(candle.close), str(candle.volume),
        ))
        self._provenance[(candle.symbol, candle.timestamp)] = {
            "source": PROVIDER_NAME,
            "timestamp": candle.timestamp.isoformat(),
            "received_at": received_at,
            "id": sha256(identity.encode("utf-8")).hexdigest(),
            "kind": "MT5_CLOSED_BAR",
            "timeframe": self.timeframe,
        }
        return candle

    def prepare(self):
        if self.service.execution_mode().value != "AXIOM_PAPER":
            raise MarketDataUnavailable("MT5 paper data requires AXIOM PAPER execution")
        self.status = "CONNECTING"
        self._last_quote = self.service.ensure_advancing(self.symbol)
        candles, warnings = self.service.copy_bars(self.symbol, self.timeframe, 1)
        latest = candles[-1]
        age = (utcnow() - latest.available_at(self.timeframe)).total_seconds()
        if age > self.stale_after_seconds:
            self.status = "STALE"
            self.last_message = "Latest closed MT5 bar is too old for paper decisions"
            raise MarketDataStale(self.last_message)
        self._pending = [self._stage(latest)]
        self._last_bar_timestamp = latest.timestamp
        self._last_candle = latest.timestamp.isoformat()
        self._data_gap = bool(warnings)
        self.status = "CONNECTED"
        self.last_message = "Fresh advancing demo ticks and a validated closed bar confirmed"
        return self.metrics()

    def assert_resume_ready(self):
        reason = self.entry_block_reason(self.symbol)
        if reason:
            raise MarketDataUnavailable(reason)
        self._last_quote = self.service.ensure_advancing(self.symbol)
        return self.metrics()

    def entry_block_reason(self, symbol: str) -> str | None:
        if symbol != self.symbol:
            return "This paper stream is subscribed only to its exact discovered MT5 symbol"
        if self._closed:
            return "The MT5 paper stream is stopped; start a fresh session before new entries"
        reason = self.service.entry_block_reason(symbol)
        if reason:
            return reason
        return None

    def provenance_for(self, candle):
        return self._provenance.get((candle.symbol, candle.timestamp))

    async def stream(self, symbols, timeframe):
        if list(symbols) != self.symbols or timeframe != self.timeframe:
            raise ValueError("MT5 paper stream symbol/timeframe does not match its prepared subscription")
        if not self._pending:
            raise MarketDataUnavailable("MT5 paper provider was not freshly prepared")
        self._closed = False
        self.status = "STREAMING"
        while self._pending:
            yield self._pending.pop(0)
        while not self._closed:
            await asyncio.sleep(self.poll_seconds)
            reason = self.entry_block_reason(self.symbol)
            if reason:
                self.status = "DEGRADED"
                self.last_message = reason
                raise MarketDataStale(reason)
            self._last_quote = self.service.quote(self.symbol)
            candles, warnings = self.service.copy_bars(self.symbol, self.timeframe, 1)
            latest = candles[-1]
            if self._last_bar_timestamp and latest.timestamp < self._last_bar_timestamp:
                self._data_gap = True
                raise MarketDataStale("MT5 closed-bar timestamps moved backwards")
            if latest.timestamp == self._last_bar_timestamp:
                continue
            self._data_gap = bool(warnings)
            self._last_bar_timestamp = latest.timestamp
            self._last_candle = latest.timestamp.isoformat()
            self.last_message = "Validated fresh MT5 closed bar"
            yield self._stage(latest)

    def metrics(self):
        service = self.service.status()
        quote = service.get("quote") if service.get("selected_symbol") == self.symbol else None
        fresh = bool(quote and quote.get("fresh")) and not self._closed
        return {
            "provider": self.provider_name,
            "connection_status": self.status,
            "authenticated": service.get("authenticated", False),
            "subscribed": self.status in {"CONNECTED", "STREAMING"} and not self._closed,
            "last_message_time": quote.get("timestamp") if quote else None,
            "last_candle_time": self._last_candle,
            "data_gap_detected": self._data_gap,
            "stale_feed": not fresh,
            "paper_mode": True,
            "reconnects": 0,
            "market_status": "FEED_FRESH" if fresh else "FEED_DEGRADED",
            "market_open": True if fresh else None,
            "feed_mode": "MT5_READ_ONLY_CLOSED_BARS",
            "timeframe": self.timeframe,
            "instrument_keys": [self.symbol],
            "feed_counters": {},
        }

    async def close(self):
        self._closed = True
        if self.status != "DEGRADED":
            self.status = "STOPPED"
        self.last_message = "MT5 paper stream stopped; account records are preserved"
