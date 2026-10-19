"""Upstox V3 market-DATA adapter — read-only; order endpoints are never used.

Official documentation this adapter implements (verified current 2026-09-21):
- Market Data Feed Authorize V3:
  GET https://api.upstox.com/v3/feed/market-data-feed/authorize
  Headers: Authorization: Bearer <access token>, Accept: application/json
  Response: {"status": "success", "data": {"authorized_redirect_uri": "wss://…"}}
  The redirect URI embeds a single-use code: it is never logged or stored.
- Market Stream Feed V3 (WebSocket):
  Binary subscription frame:
  {"guid": "<uuid>", "method": "sub",
   "data": {"mode": "ltpc"|"full", "instrumentKeys": ["NSE_EQ|…", …]}}
  Binary protobuf responses decoded with the OFFICIAL schema stored at
  app/data/upstox_proto/MarketDataFeed.proto (sha256 3e1c939dce2c83a3…).
  Response types: initial_feed (snapshot), live_feed, market_info
  (segmentStatus map per the official MarketStatus enum). The server sends
  standard WebSocket ping frames; the websockets library answers pong
  automatically and this client additionally pings when idle.

V0.6.2 additions: UpstoxProtobufDecoder (wire → normalized MarketEvent),
market-status gating (no strategy execution before the market is known OPEN —
a connected WebSocket is NOT a tradable market), snapshot/live/market_info
handling, structured lifecycle logs, and connection-health metrics.

Only market DATA flows through this module. Every candle is emitted strictly
after completion and is then validated and traded (if a signal occurs) by the
existing PaperBroker pipeline. Upstox order placement endpoints are neither
imported, referenced, nor called anywhere in this project.
"""

import asyncio
from datetime import datetime, timezone
import json
import logging
import math
from random import random
import re
import ssl
import time
from uuid import uuid4

import certifi
import httpx
from google.protobuf.message import DecodeError

from app.data.candles import LiveCandleBuilder
from app.data.market_events import MarketEvent
from app.data.models import Candle
from app.data.provider import WebSocketProvider
from app.data.upstox_proto import MarketDataFeed_pb2 as pb2

# Same CA trust store used by app.data.upstox_auth so outbound TLS to Upstox
# works inside a PyInstaller-frozen executable where certifi's cacert.pem
# would otherwise not be on disk.
_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

logger = logging.getLogger("bot.upstox")
feed_logger = logging.getLogger("bot.market")

AUTHORIZE_URL = "https://api.upstox.com/v3/feed/market-data-feed/authorize"

# Instrument keys look like NSE_EQ|INE002A01018 (provider format; index keys
# such as NSE_INDEX|Nifty 50 match the provider pattern but cannot map to the
# project symbol charset and are rejected with a clear error at construction).
INSTRUMENT_KEY = re.compile(r"^[A-Z]{2,6}(?:_[A-Z]+)*\|[^|\r\n]{1,40}$")

TICK_MODES = frozenset({"ltpc"})  # candles built from LTP ticks
INTERVAL_MODES = frozenset({"full"})  # official 1m/30m aggregate bars
#: timeframes the interval ("full") modes can carry; other timeframes are built
#: locally from ticks, which is what the "ltpc" tick mode is for
INTERVAL_MODE_TIMEFRAMES = frozenset({"1m", "30m"})
MODES = TICK_MODES | INTERVAL_MODES
TIMEFRAMES_LIVE = frozenset({"1m", "5m", "15m", "30m", "1h", "1D"})

# Official MarketStatus enum values from the shipped MarketDataFeed.proto.
MARKET_STATUS_NAMES = {
    0: "PRE_OPEN_START",
    1: "PRE_OPEN_END",
    2: "NORMAL_OPEN",
    3: "NORMAL_CLOSE",
    4: "CLOSING_START",
    5: "CLOSING_END",
}
MARKET_OPEN_STATUS = "NORMAL_OPEN"


class UpstoxAuthError(RuntimeError):
    """Permanent authorize failure (401/403). Messages never include secrets."""


class UpstoxTransientError(RuntimeError):
    """A retryable authorize failure: HTTP 429/5xx or a malformed 200 body.

    The previous adapter classified EVERY non-200 authorize response as
    `UpstoxAuthError`, which the stream treats as permanent. One transient
    Upstox 500 or 429 at startup therefore stopped the feed for the whole
    session — indistinguishable to the operator from "cannot connect".
    """

    def __init__(self, message: str, *, kind: str = "UPSTOX_UNAVAILABLE"):
        super().__init__(message)
        self.kind = kind


class UpstoxFeedUnavailable(RuntimeError):
    """Retries were exhausted; the feed stopped and reports UNAVAILABLE.

    Bounded retries are deliberate: an endless reconnect loop hides a permanent
    outage from the operator and can hammer the provider. The simulator turns
    this into an ERROR state (fail closed) and the paper engine stops.
    """


def to_symbol(instrument_key: str) -> str:
    """Map a provider key to the project symbol charset (segments lack '-')."""
    return instrument_key.replace("|", "-")


def from_symbol(symbol: str) -> str:
    """Inverse of to_symbol for API/CLI inputs that already use display names."""
    segment, separator, rest = symbol.partition("-")
    key = f"{segment}|{rest}"
    if not separator or not INSTRUMENT_KEY.fullmatch(key):
        raise ValueError(
            "Use an Upstox instrument key such as NSE_EQ|INE002A01018 "
            "(or its display form NSE_EQ-INE002A01018)"
        )
    return key


class UpstoxProtobufDecoder:
    """Decode official V3 protobuf frames into normalized MarketEvents.

    Only fields actually present in the selected feed mode are populated;
    missing values stay None (never fabricated). Malformed frames are counted
    and skipped so one bad packet cannot kill the stream.
    """

    def __init__(self, symbol_for: dict[str, str], mode: str):
        self.symbol_for = dict(symbol_for)
        self.mode = mode
        self.malformed = 0

    def decode(self, payload: bytes, received_at: float) -> list[MarketEvent]:
        """One binary frame → list of normalized events (possibly empty)."""
        response = pb2.FeedResponse()
        try:
            response.ParseFromString(payload)
        except DecodeError:
            self.malformed += 1
            logger.warning("UPSTOX_DECODE_ERROR", extra={"reason": "protobuf"})
            return []
        kind = "snapshot" if response.type == pb2.initial_feed else "live"
        received = datetime.fromtimestamp(received_at, timezone.utc)
        events: list[MarketEvent] = []
        for key, feed in response.feeds.items():
            if key not in self.symbol_for:
                continue  # frame for an unconfigured instrument; ignored by design
            which = feed.WhichOneof("FeedUnion")
            if which == "ltpc" and self.mode in TICK_MODES:
                events.append(self._tick_event(key, kind, received, ltpc=feed.ltpc))
            elif which == "fullFeed" and self.mode in INTERVAL_MODES:
                events.append(self._full_event(key, kind, received, feed.fullFeed))
        if response.HasField("marketInfo"):
            events.append(self._market_info_event(response.marketInfo, received))
        return events

    # -- event builders -------------------------------------------------------

    def _tick_event(self, key: str, kind: str, received, *, ltpc) -> MarketEvent:
        return MarketEvent(
            provider="upstox",
            instrument_key=key,
            kind=kind,
            received_at=received,
            exchange_ts=datetime.fromtimestamp(ltpc.ltt / 1000, timezone.utc) if ltpc.ltt else None,
            ltp=float(ltpc.ltp) if ltpc.ltp else None,
            close_price=float(ltpc.cp) if ltpc.cp else None,
            last_traded_quantity=float(ltpc.ltq) if ltpc.ltq else None,
        )

    def _full_event(self, key: str, kind: str, received, full) -> MarketEvent:
        inner = (
            full.marketFF
            if full.HasField("marketFF")
            else (full.indexFF if full.HasField("indexFF") else None)
        )
        if inner is None:
            return MarketEvent(provider="upstox", instrument_key=key, kind=kind, received_at=received)
        event = self._tick_event(key, kind, received, ltpc=inner.ltpc)
        bid = ask = None
        if inner.HasField("marketLevel"):
            quotes = inner.marketLevel.bidAskQuote
            if quotes:
                bid, ask = float(quotes[0].bidP), float(quotes[0].askP)
        interval_bar = None
        if inner.HasField("marketOHLC"):
            for bar in inner.marketOHLC.ohlc:
                if bar.interval:  # official aggregate bar; keep the first per frame
                    interval_bar = (
                        bar.interval, int(bar.ts), float(bar.open), float(bar.high),
                        float(bar.low), float(bar.close), float(bar.vol),
                    )
                    break
        return MarketEvent(
            provider=event.provider,
            instrument_key=event.instrument_key,
            kind=event.kind,
            received_at=event.received_at,
            exchange_ts=event.exchange_ts,
            ltp=event.ltp,
            close_price=event.close_price,
            last_traded_quantity=event.last_traded_quantity,
            bid=bid if bid else None,
            ask=ask if ask else None,
            interval_bar=interval_bar,
        )

    def _market_info_event(self, market_info, received) -> MarketEvent:
        statuses = {
            segment: MARKET_STATUS_NAMES.get(int(status), f"UNKNOWN_{int(status)}")
            for segment, status in market_info.segmentStatus.items()
        }
        return MarketEvent(
            provider="upstox",
            instrument_key="",
            kind="market_info",
            received_at=received,
            market_status=self._aggregate_status(statuses),
            segment_statuses=statuses,
        )

    @staticmethod
    def _aggregate_status(statuses: dict[str, str]) -> str:
        """Aggregate the provider segment map into one status label.

        Any open segment → NORMAL_OPEN; otherwise the sorted unique statuses
        are joined. Nothing is invented: the full map always surfaces in
        metrics alongside this label.
        """
        values = list(statuses.values())
        if not values:
            return "UNKNOWN"
        if MARKET_OPEN_STATUS in values:
            return MARKET_OPEN_STATUS
        return ";".join(sorted(set(values)))


class UpstoxStream(WebSocketProvider):
    """Live Upstox market-data stream yielding only completed, normalized candles.

    Authentication failures stop the stream (fail closed). Network drops follow
    bounded exponential backoff with fresh authorization and resubscription.
    With require_market_status=True (default) completed candles are held back
    until a market-info frame shows the market OPEN: connection != tradable.
    """

    supported_timeframes = TIMEFRAMES_LIVE

    def __init__(
        self,
        access_token: str,
        instrument_keys: list[str],
        timeframe: str = "1m",
        mode: str = "ltpc",
        *,
        authorize_url: str = AUTHORIZE_URL,
        max_backoff: float = 60.0,
        stale_after: float = 90.0,
        http_timeout: float = 20.0,
        idle_timeout: float = 30.0,
        ssl_context=None,
        http_transport=None,
        ws_url_schemes=("wss",),
        require_market_status: bool = True,
        max_consecutive_failures: int = 25,
    ):
        if not isinstance(access_token, str) or not access_token.strip():
            raise ValueError("An Upstox access token is required (MARKET_DATA_ACCESS_TOKEN)")
        if mode not in MODES:
            raise ValueError("Upstox mode must be 'ltpc' or 'full'")
        if timeframe not in TIMEFRAMES_LIVE:
            raise ValueError(f"Upstox adapter supports timeframes {sorted(TIMEFRAMES_LIVE)}")
        if mode in INTERVAL_MODES and timeframe not in INTERVAL_MODE_TIMEFRAMES:
            raise ValueError(
                "Upstox full mode carries official "
                f"{'/'.join(sorted(INTERVAL_MODE_TIMEFRAMES))} bars; use ltpc mode "
                "otherwise")
        if authorize_url.split(":")[0] not in {"https", "http"}:
            raise ValueError("Authorize URL must be http(s) for the local/test override")
        keys: list[str] = []
        symbols: list[str] = []
        from app.data.models import validate_symbol

        for raw in instrument_keys:
            key = str(raw).strip()
            if not INSTRUMENT_KEY.fullmatch(key):
                raise ValueError(f"Invalid Upstox instrument key: {key}")
            display = to_symbol(key)
            try:
                validate_symbol(display)
            except ValueError:
                raise ValueError(
                    f"Instrument key not usable as an internal symbol: {key}; "
                    "V0.6.1 supports keys like NSE_EQ|INE002A01018 without spaces"
                )
            if key not in keys:
                keys.append(key)
                symbols.append(display)
        if not keys:
            raise ValueError("At least one instrument key is required")
        self._token = access_token
        self._keys = keys
        self._symbol_for = dict(zip(keys, symbols))
        self.symbols = symbols
        # Best-bid/ask remains a separate observed quote from OHLC candles; it
        # is never synthesized into candle data. The engine consumes only a
        # fresh quote through ``quote_for`` for live liquidity checks.
        self.last_quotes: dict[str, dict] = {}
        self.timeframe, self.mode = timeframe, mode
        self._authorize_url = authorize_url
        self.max_backoff, self.stale_after = max_backoff, stale_after
        self._http_timeout, self._idle = http_timeout, idle_timeout
        self._ssl_context = ssl_context  # None → verified default TLS for wss
        self._transport = http_transport  # test seam; production uses real network
        # Production Upstox always returns wss://. Loopback protocol tests may
        # explicitly allow plain ws://; anything else stays rejected.
        if not ws_url_schemes or any(s not in {"ws", "wss"} for s in ws_url_schemes):
            raise ValueError("ws_url_schemes may only contain 'ws' and/or 'wss'")
        self._schemes = tuple(ws_url_schemes)
        self.require_market_status = bool(require_market_status)
        self.closed = False
        self.status = "DISCONNECTED"
        self.last_message: str | None = None
        self.last_candle_time: str | None = None
        self.authenticated = False
        self.subscribed = False
        self.reconnects = 0
        # Truthful, non-secret failure classification for the UI/logs. A token
        # merely existing never means "connected"; these values are derived
        # from what the provider actually did.
        self.last_error_kind: str | None = None
        self.last_error_detail: str = ""
        self.consecutive_failures = 0
        self.max_consecutive_failures = max(1, int(max_consecutive_failures))
        self.market_status: str | None = None
        self.segment_statuses: dict[str, str] = {}
        self.market_open: bool | None = None
        self.suppressed_by_market_status = 0
        self.last_tick: dict | None = None
        self.connect_latency = {"attempts": 0, "avg_ms": 0.0, "max_ms": 0.0, "last_ms": 0.0}
        self.feed_latency = {"messages": 0, "avg_ms": 0.0, "max_ms": 0.0}
        self._decoder = UpstoxProtobufDecoder(self._symbol_for, mode)
        self._builder = LiveCandleBuilder(timeframe, stale_after=stale_after)
        # Shared normalization pipeline (same folding as the offline synthetic
        # feed), with Upstox-specific observability wired as callbacks.
        from app.data.market_events import MarketEventApplier

        self._applier = MarketEventApplier(
            self._builder,
            self._symbol_for,
            on_market_info=self._on_market_info,
            on_tick=self._on_tick,
        )
        self._socket = None

    def __repr__(self):
        # The access token is intentionally excluded from the repr and logs.
        return (
            f"UpstoxStream(instrument_keys={self._keys}, timeframe={self.timeframe!r}, "
            f"mode={self.mode!r}, paper_only=True)"
        )

    # -- authentication -----------------------------------------------------

    async def _authorize(self) -> str:
        """Exchange the access token for a single-use wss URL (never logged)."""
        client_kwargs: dict = {"timeout": self._http_timeout}
        if self._transport is None:
            client_kwargs["verify"] = _SSL_CONTEXT
        async with httpx.AsyncClient(transport=self._transport, **client_kwargs) as client:
            response = await client.get(
                self._authorize_url,
                headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
            )
        if response.status_code in {401, 403}:
            logger.error("UPSTOX_AUTH_FAILURE", extra={"http_status": response.status_code})
            from app.data.upstox_auth import GUIDANCE_FORBIDDEN, GUIDANCE_REJECTED

            guidance = GUIDANCE_REJECTED if response.status_code == 401 else GUIDANCE_FORBIDDEN
            self.last_error_kind = "AUTH_FAILED"
            self.last_error_detail = f"HTTP {response.status_code}"
            raise UpstoxAuthError(
                f"Upstox {'rejected' if response.status_code == 401 else 'refused'} "
                f"the credential (HTTP {response.status_code}). {guidance}"
            )
        if response.status_code == 429:
            # A rate limit is NOT a credential verdict. Fail closed for this
            # attempt, but let the stream retry with backoff instead of
            # ending the whole session.
            self.last_error_kind = "RATE_LIMITED"
            self.last_error_detail = "HTTP 429 from the authorize endpoint"
            logger.warning("UPSTOX_AUTHORIZE_RATE_LIMITED")
            raise UpstoxTransientError(
                "Upstox rate-limited the market-data authorization (HTTP 429); "
                "this is retryable and not a credential failure.",
                kind="RATE_LIMITED",
            )
        if 500 <= response.status_code <= 599:
            self.last_error_kind = "UPSTOX_UNAVAILABLE"
            self.last_error_detail = f"HTTP {response.status_code} from authorize"
            logger.warning("UPSTOX_AUTHORIZE_SERVER_ERROR",
                           extra={"http_status": response.status_code})
            raise UpstoxTransientError(
                f"Upstox authorize endpoint returned HTTP {response.status_code} "
                "(their service error); retrying with backoff.",
                kind="UPSTOX_UNAVAILABLE",
            )
        if response.status_code != 200:
            logger.error("UPSTOX_AUTH_FAILURE", extra={"http_status": response.status_code})
            self.last_error_kind = "AUTHORIZE_REJECTED"
            self.last_error_detail = f"HTTP {response.status_code} from authorize"
            raise UpstoxAuthError(
                f"Upstox authorize failed with HTTP {response.status_code}; "
                "the request or configuration was rejected (not retried)."
            )
        try:
            uri = response.json()["data"]["authorized_redirect_uri"]
        except (KeyError, TypeError, ValueError):
            self.last_error_kind = "MALFORMED_RESPONSE"
            self.last_error_detail = "authorize response missing authorized_redirect_uri"
            # A malformed 200 is not a network blip: it is what an intercepting
            # proxy or a wrong endpoint returns. Retrying cannot fix it and
            # would hide a possible MITM, so fail closed as an auth error.
            raise UpstoxAuthError(
                "Upstox authorize response was malformed (missing "
                "authorized_redirect_uri); refusing to connect."
            )
        if not isinstance(uri, str) or not any(
            uri.startswith(f"{scheme}://") for scheme in self._schemes
        ):
            self.last_error_kind = "UNTRUSTED_ENDPOINT"
            self.last_error_detail = "authorize returned an unexpected URI scheme"
            raise UpstoxAuthError(
                "Upstox authorize returned an unexpected endpoint scheme; "
                "refusing to connect."
            )
        self.last_error_kind = None
        self.last_error_detail = ""
        logger.info("UPSTOX_AUTH_SUCCESS")
        return uri

    # -- provider protocol --------------------------------------------------

    def subscription(self) -> bytes:
        """Official V3 binary subscription frame for the configured keys."""
        request = {
            "guid": uuid4().hex,
            "method": "sub",
            "data": {"mode": self.mode, "instrumentKeys": self._keys},
        }
        return json.dumps(request).encode()

    def _apply_event(self, event: MarketEvent, received_at: float) -> None:
        """Fold one normalized event through the shared normalization pipeline."""
        self._applier.apply(event, received_at)

    def _on_market_info(self, segment_statuses: dict[str, str], market_status: str) -> None:
        changed = (
            segment_statuses != self.segment_statuses or self.market_open is None
        )
        self.segment_statuses = dict(segment_statuses)
        self.market_status = market_status
        self.market_open = self._relevant_market_open()
        if changed:
            logger.info(
                "UPSTOX_MARKET_STATUS",
                extra={
                    "market_status": self.market_status,
                    "market_open": self.market_open,
                    "segments": dict(self.segment_statuses),
                },
            )

    def _on_tick(self, event: MarketEvent, symbol: str, seconds: float,
                 price: float, quantity: float) -> None:
        self._builder.last_activity_ts = seconds
        self.last_tick = {
            "instrument_key": event.instrument_key,
            "exchange_ts": event.exchange_ts.isoformat(),
            "ltp": price,
            "bid": event.bid,
            "ask": event.ask,
            "kind": event.kind,
        }
        if event.bid is not None and event.ask is not None:
            try:
                bid, ask = float(event.bid), float(event.ask)
                if (not math.isfinite(bid) or not math.isfinite(ask)
                        or bid <= 0 or ask < bid):
                    raise ValueError("invalid observed bid/ask")
                self.last_quotes[symbol] = {
                    "symbol": symbol,
                    "instrument_key": event.instrument_key,
                    "bid": bid,
                    "ask": ask,
                    "exchange_ts": event.exchange_ts.isoformat(),
                    "received_at": event.received_at.timestamp(),
                    "provenance": "OBSERVED",
                    "provider": event.provider,
                }
            except (TypeError, ValueError, OverflowError):
                # Do not let an invalid new quote leave an older quote looking
                # current. The engine will refuse a live entry until a valid
                # quote arrives.
                self.last_quotes.pop(symbol, None)
        logger.debug(
            "UPSTOX_TICK",
            extra={"symbol": symbol, "ltp": price, "kind": event.kind},
        )

    def quote_for(self, symbol: str, *, max_age_seconds: float = 3.0,
                  now: float | None = None) -> dict | None:
        """Return a fresh, observed bid/ask pair; never derive one from LTP.

        ``None`` means quote evidence is absent or stale. Consumers must not
        substitute a model spread for live execution.
        """
        quote = self.last_quotes.get(str(symbol))
        if quote is None:
            return None
        try:
            age = (time.time() if now is None else float(now)) - float(
                quote["received_at"])
            maximum = float(max_age_seconds)
            if not math.isfinite(age) or not math.isfinite(maximum):
                return None
            if age < 0 or maximum <= 0 or age > maximum:
                return None
            return {**quote, "age_seconds": round(age, 3)}
        except (KeyError, TypeError, ValueError, OverflowError):
            return None

    def _relevant_market_open(self) -> bool | None:
        """Map segment statuses to open/closed for the subscribed instruments.

        Tries the instrument exchange prefix (e.g. 'NSE' from 'NSE_EQ|…') in
        the provider's map; if nothing matches, falls back to 'any segment
        open'. Returns None only while no status has been received yet.
        """
        if not self.segment_statuses:
            return None
        for key in self._keys:
            exchange = key.split("|", 1)[0].split("_", 1)[0]
            if exchange in self.segment_statuses:
                return self.segment_statuses[exchange] == MARKET_OPEN_STATUS
            if key.split("|", 1)[0] in self.segment_statuses:
                return self.segment_statuses[key.split("|", 1)[0]] == MARKET_OPEN_STATUS
        return MARKET_OPEN_STATUS in self.segment_statuses.values()

    def _decode(self, payload: bytes) -> list[Candle]:
        """Compatibility path: decode a frame and return completed candles."""
        received_at = time.time()
        for event in self._decoder.decode(payload, received_at):
            self._apply_event(event, received_at)
        started = time.perf_counter()
        bars = self._builder.collect(now=received_at)
        elapsed_ms = (time.perf_counter() - started) * 1000
        self._record_decode_latency(elapsed_ms)
        if bars:
            self.last_candle_time = bars[-1].available_at(self.timeframe).isoformat()
        return bars

    def _record_decode_latency(self, elapsed_ms: float) -> None:
        count = self.feed_latency["messages"] + 1
        self.feed_latency["messages"] = count
        self.feed_latency["avg_ms"] = round(
            self.feed_latency["avg_ms"] + (elapsed_ms - self.feed_latency["avg_ms"]) / count, 3
        )
        self.feed_latency["max_ms"] = max(self.feed_latency["max_ms"], round(elapsed_ms, 3))

    def connection_state(self) -> str:
        """One truthful UI state derived from observed provider behaviour.

        A configured token is never reported as CONNECTED. The states mirror
        the operations runbook: CONNECTED, AUTH_FAILED, UNREACHABLE,
        RATE_LIMITED, STALE_DATA, RECONNECTING, DISCONNECTED, UNAVAILABLE.
        """
        if self.status == "AUTH_FAILED":
            return "AUTH_FAILED"
        if self.status == "UNAVAILABLE":
            return "UNAVAILABLE"
        if self.status == "STALE":
            return "STALE_DATA"
        if self.status == "CONNECTED":
            try:
                stale = self._builder.stale()
            except Exception:
                stale = False
            return "STALE_DATA" if stale else "CONNECTED"
        if self.status in {"CONNECTING", "RECONNECTING"}:
            if self.last_error_kind == "RATE_LIMITED":
                return "RATE_LIMITED"
            if self.last_error_kind == "UNREACHABLE":
                return "UNREACHABLE"
            return "CONNECTING" if self.status == "CONNECTING" else "RECONNECTING"
        return "DISCONNECTED"

    def metrics(self) -> dict:
        """Health/monitoring fields for /market/status. Never includes secrets."""
        counters = self._builder.metrics()
        return {
            "paper_mode": True,
            "provider": "upstox",
            "feed_mode": self.mode,
            "timeframe": self.timeframe,
            "connection_status": self.status,
            "connection_state": self.connection_state(),
            "last_error_kind": self.last_error_kind,
            "last_error_detail": self.last_error_detail,
            "consecutive_failures": self.consecutive_failures,
            "authenticated": self.authenticated,
            "subscribed": self.subscribed,
            "last_message_time": self.last_message,
            "last_candle_time": self.last_candle_time,
            "data_gap_detected": counters["gaps"] > 0,
            "stale_feed": counters["stale_feed"],
            "reconnects": self.reconnects,
            "market_status": self.market_status,
            "market_open": self.market_open,
            "segment_statuses": dict(self.segment_statuses),
            "suppressed_by_market_status": self.suppressed_by_market_status,
            "last_tick": dict(self.last_tick) if self.last_tick else None,
            "connect_latency_ms": dict(self.connect_latency),
            "feed_counters": counters,
            "feed_latency_ms": dict(self.feed_latency),
            "instrument_keys": list(self._keys),
        }

    # -- lifecycle ----------------------------------------------------------

    async def stream(self, symbols: list[str], timeframe: str):
        """Yield completed candles with reconnect + resubscribe + re-auth."""
        if timeframe != self.timeframe:
            raise ValueError("Stream timeframe mismatch")
        if any(symbol not in self.symbols for symbol in symbols):
            raise ValueError("Unsubscribed stream symbol")
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed, InvalidStatus

        attempts = 0
        try:
            while not self.closed:
                self.status = "CONNECTING" if attempts == 0 else "RECONNECTING"
                try:
                    started = time.perf_counter()
                    uri = await self._authorize()
                    self.authenticated = True
                    # TLS policy: an explicit context wins (e.g. pinned trust);
                    # wss:// always uses a VERIFIED default context pointing at
                    # the certifi CA bundle (so the PyInstaller-frozen Windows
                    # executable can validate Upstox's certificate — without an
                    # explicit cafile the frozen bundle has no trust store);
                    # only plain ws:// loopback runs without TLS (ssl=None).
                    if self._ssl_context is not None:
                        tls = self._ssl_context
                    elif uri.startswith("wss://"):
                        tls = _SSL_CONTEXT
                    else:
                        tls = None
                    async with connect(
                        uri,
                        ssl=tls,
                        ping_interval=20,
                        ping_timeout=20,
                        open_timeout=20,
                        close_timeout=5,
                        max_size=1_048_576,
                    ) as socket:
                        elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
                        count = self.connect_latency["attempts"] + 1
                        self.connect_latency.update(
                            attempts=count,
                            avg_ms=round(
                                self.connect_latency["avg_ms"]
                                + (elapsed_ms - self.connect_latency["avg_ms"]) / count,
                                3,
                            ),
                            max_ms=max(self.connect_latency["max_ms"], elapsed_ms),
                            last_ms=elapsed_ms,
                        )
                        self._socket, self.status = socket, "CONNECTED"
                        logger.info(
                            "UPSTOX_CONNECT",
                            extra={"connect_latency_ms": elapsed_ms, "reconnect": attempts > 0},
                        )
                        await socket.send(self.subscription())
                        # A successful authorize + socket + subscription is
                        # proof the credential and endpoint worked NOW; prior
                        # transient failures no longer count toward the cap.
                        self.consecutive_failures = 0
                        self.last_error_kind = None
                        self.last_error_detail = ""
                        logger.info(
                            "UPSTOX_SUBSCRIBE",
                            extra={"keys": len(self._keys), "mode": self.mode},
                        )
                        if attempts > 0:
                            logger.info("UPSTOX_RESUBSCRIBE", extra={"attempt": attempts})
                            if self.market_open is not None:
                                logger.info("SYSTEM_RECOVERY", extra={"state": "resubscribed"})
                        while not self.closed:
                            try:
                                message = await asyncio.wait_for(
                                    socket.recv(), timeout=self._idle
                                )
                            except TimeoutError:
                                pong = await socket.ping()
                                await asyncio.wait_for(pong, timeout=20)
                                if self._builder.stale():
                                    self.status = "STALE"
                                    logger.warning("feed_stale")
                                continue
                            self.last_message = datetime.now(timezone.utc).isoformat()
                            if self.status != "CONNECTED":
                                # Data is flowing again: report recovery rather
                                # than leaving a stale STALE label on screen.
                                self.status = "CONNECTED"
                                self.last_error_kind = None
                                self.last_error_detail = ""
                                logger.info("SYSTEM_RECOVERY",
                                            extra={"state": "feed_data_resumed"})
                            if isinstance(message, str):
                                self._decoder.malformed += 1
                                logger.warning("UPSTOX_DECODE_ERROR", extra={"reason": "text_frame"})
                                continue
                            received_at = time.time()
                            decode_started = time.perf_counter()
                            events = self._decoder.decode(message, received_at)
                            for event in events:
                                self._apply_event(event, received_at)
                            self._record_decode_latency(
                                (time.perf_counter() - decode_started) * 1000
                            )
                            bars = self._builder.collect(now=received_at)
                            if bars:
                                self.last_candle_time = bars[-1].available_at(self.timeframe).isoformat()
                            self.subscribed, attempts = True, 0
                            if bars and not self._candles_allowed():
                                self.suppressed_by_market_status += len(bars)
                                feed_logger.warning(
                                    "candles_suppressed_market_closed",
                                    extra={"count": len(bars), "market_status": self.market_status},
                                )
                                continue
                            for bar in bars:
                                if bar.symbol not in symbols:
                                    raise ValueError("Unsubscribed stream symbol")
                                feed_logger.debug(
                                    "CANDLE_COMPLETED",
                                    extra={"symbol": bar.symbol, "timestamp": bar.timestamp.isoformat()},
                                )
                                yield bar
                except UpstoxAuthError:
                    # Bad/expired token will not heal; fail closed instead of
                    # hammering the provider. Simulator cancels and stops safely.
                    self.status = "AUTH_FAILED"
                    raise
                except (UpstoxTransientError, OSError, TimeoutError,
                        ConnectionClosed, InvalidStatus, httpx.HTTPError) as exc:
                    attempts += 1
                    self.reconnects += 1
                    self.consecutive_failures += 1
                    self.status = "RECONNECTING"
                    self.authenticated = self.subscribed = False
                    if isinstance(exc, UpstoxTransientError):
                        self.last_error_kind = exc.kind
                        self.last_error_detail = str(exc)
                    else:
                        self.last_error_kind = "UNREACHABLE"
                        self.last_error_detail = type(exc).__name__
                    logger.warning(
                        "UPSTOX_RECONNECT",
                        extra={"error": type(exc).__name__,
                               "kind": self.last_error_kind,
                               "attempt": attempts,
                               "consecutive": self.consecutive_failures},
                    )
                    if self.consecutive_failures >= self.max_consecutive_failures:
                        self.status = "UNAVAILABLE"
                        raise UpstoxFeedUnavailable(
                            "Upstox market data remained unavailable after "
                            f"{self.consecutive_failures} consecutive attempts; "
                            "the feed stopped fail-closed. Check the network and "
                            "the credential, then restart.")
                    if not self.closed:
                        # Bounded exponential backoff with jitter. The cap is
                        # the documented max_backoff (60s by default), so a
                        # reconnect storm cannot hammer the provider.
                        base = min(self.max_backoff, 2 ** min(attempts, 6))
                        await asyncio.sleep(base + random() * 0.5)
                finally:
                    self._socket = None
        finally:
            # Keep terminal states visible; everything else ends disconnected
            # (matching the established stream lifecycle).
            if self.status not in {"AUTH_FAILED", "UNAVAILABLE"}:
                self.status = "DISCONNECTED"

    def _candles_allowed(self) -> bool:
        return not self.require_market_status or self.market_open is True

    async def close(self):
        self.closed = True
        if self._socket:
            await self._socket.close()
        self.status = "DISCONNECTED"


# Requested provider-style name; identical read-only implementation.
UpstoxMarketDataProvider = UpstoxStream
