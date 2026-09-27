"""Read-only data interfaces; these have no relationship to order routing."""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from datetime import datetime
from app.data.models import Candle, TIMEFRAMES


class MarketDataProvider(ABC):
    supported_timeframes: frozenset[str] = frozenset()

    def check_timeframe(self, timeframe: str):
        if timeframe not in self.supported_timeframes:
            raise ValueError(f"Provider does not support {timeframe}")

    @abstractmethod
    def historical(
        self,
        symbol: str,
        timeframe: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Candle]:
        """Return complete candles. Range is [start, end)."""


class APIProvider(MarketDataProvider):
    """Extension point for read-only historical APIs."""


class WebSocketProvider(MarketDataProvider):
    """Streaming adapter yields only CLOSED candles, never partial updates."""

    supported_timeframes = frozenset(TIMEFRAMES)

    def historical(self, symbol, timeframe, start=None, end=None):
        raise NotImplementedError("Use a separate historical adapter for backfill")

    status: str = "DISCONNECTED"
    last_message: str | None = None

    @abstractmethod
    def stream(self, symbols: list[str], timeframe: str) -> AsyncIterator[Candle]: ...

    @abstractmethod
    async def close(self): ...
