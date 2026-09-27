"""Provider-agnostic normalized market events (V0.6.2).

Sits between provider decoders and the candle builder so the strategy engine
never sees provider-specific payloads. Fields stay None when the provider did
not supply them — values are never fabricated.
"""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class MarketEvent:
    """One normalized observation from any market-data provider.

    kind: "snapshot" (initial_feed), "live" (live_feed),
          "market_info" (segment status change; no price payload implied).
    exchange_ts / received_at: provider timestamp vs local receive time (UTC).
    market_status: normalized status string for market_info events.
    segment_statuses: full status map observed in a market_info frame.
    """

    provider: str
    instrument_key: str
    kind: str
    received_at: datetime
    exchange_ts: datetime | None = None
    ltp: float | None = None
    close_price: float | None = None
    last_traded_quantity: float | None = None
    volume: float | None = None
    bid: float | None = None
    ask: float | None = None
    market_status: str | None = None
    segment_statuses: dict[str, str] = field(default_factory=dict)
    # Official provider aggregate bar (interval, open_epoch, o, h, l, c, vol);
    # present only when the provider itself supplies completed interval bars.
    interval_bar: tuple | None = None


class MarketEventApplier:
    """Shared normalization: MarketEvent → LiveCandleBuilder (+ status hooks).

    Every provider adapter (the real Upstox V3 adapter and the offline
    synthetic feed) folds events through this single path, so verifying the
    pipeline with synthetic data exercises exactly the same normalization,
    candle construction and market-status handling as the real feed.
    """

    def __init__(self, builder, symbol_for: dict[str, str], *,
                 on_market_info=None, on_tick=None):
        self.builder = builder
        self.symbol_for = dict(symbol_for)
        self.on_market_info = on_market_info  # cb(segment_statuses, market_status)
        self.on_tick = on_tick  # cb(event, symbol, exchange_seconds, price, quantity)

    def apply(self, event: MarketEvent, received_at: float) -> None:
        if event.kind == "market_info":
            if self.on_market_info is not None:
                self.on_market_info(event.segment_statuses, event.market_status)
            return
        if event.exchange_ts is not None and event.ltp is not None:
            symbol = self.symbol_for.get(event.instrument_key)
            if symbol is not None:
                seconds = event.exchange_ts.timestamp()
                quantity = event.last_traded_quantity or 0.0
                self.builder.ingest_tick(
                    symbol, seconds, event.ltp, quantity, received_at=received_at
                )
                if self.on_tick is not None:
                    self.on_tick(event, symbol, seconds, event.ltp, quantity)
        bar = event.interval_bar
        if bar is not None:
            symbol = self.symbol_for.get(event.instrument_key)
            if symbol is not None:
                self.builder.ingest_interval(
                    symbol, bar[0], bar[1], bar[2], bar[3], bar[4], bar[5],
                    bar[6], received_at=received_at,
                )

    def public_view(self) -> dict:
        """Safe printable view (used by the market-stream CLI)."""
        return {
            "provider": self.provider,
            "instrument_key": self.instrument_key,
            "kind": self.kind,
            "exchange_ts": self.exchange_ts.isoformat() if self.exchange_ts else None,
            "received_at": self.received_at.isoformat(),
            "ltp": self.ltp,
            "close_price": self.close_price,
            "last_traded_quantity": self.last_traded_quantity,
            "volume": self.volume,
            "bid": self.bid,
            "ask": self.ask,
            "market_status": self.market_status,
            "segment_statuses": dict(self.segment_statuses),
        }
