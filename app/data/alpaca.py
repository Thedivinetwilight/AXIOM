"""Read-only Alpaca US equity market-data adapters.

This module contains no trading client, no order URL, and no order method. The
only hosts used are the documented market-data host
(``https://data.alpaca.markets``) and the market-data WebSocket
(``wss://stream.data.alpaca.markets``). Paper fills remain local to
:class:`app.paper_trading.broker.PaperBroker`.

Design rules enforced here:

* only *completed* 1-minute bars are emitted; nothing is resampled;
* ``updatedBars`` corrections are never replayed on top of an already emitted
  bar (see the module note in ``docs/US_EQUITIES_PAPER.md``);
* authentication/subscription is confirmed by reading the stream's success
  messages, not assumed from the fact that a socket opened;
* reconnects are bounded and counted; every failure is a typed ``ValueError``
  that the simulator maps to an allowlisted status reason (no raw provider text
  and no credential ever reaches a log or the status payload).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
import re

import httpx

from app.data.models import Candle, parse_timestamp, validate_symbol
from app.data.provider import APIProvider, WebSocketProvider
from app.data.validator import validate

logger = logging.getLogger("bot.market")
_TF_API = {"1m": "1Min", "5m": "5Min", "15m": "15Min", "30m": "30Min",
           "1h": "1Hour", "1D": "1Day"}
_TICKER = re.compile(r"^[A-Z][A-Z0-9.\-]{0,14}$")
_DATA_HOST = "https://data.alpaca.markets"
FEEDS = {"iex", "sip", "delayed_sip"}
ADJUSTMENTS = {"raw", "split", "dividend", "all"}


def validate_us_symbol(symbol: str) -> str:
    """Accept only plain listed US tickers (``AAPL``, ``BRK.B``, ``BF-B``).

    The US-specific message is raised *first*: an ``|``/``:``-separated Upstox
    instrument key must be rejected as a US ticker, not as a generic symbol, so
    an operator sees the fix rather than a parsing accident.
    """
    text = str(symbol or "").strip().upper()
    if not _TICKER.fullmatch(text):
        raise ValueError(
            "US paper symbols must be plain listed tickers, e.g. AAPL or BRK.B"
        )
    return validate_symbol(text)


class AlpacaHistoricalProvider(APIProvider):
    """Paginated, read-only stock-bar history. Ranges use [start, end)."""

    supported_timeframes = frozenset(_TF_API)

    def __init__(self, key: str, secret: str, *, feed: str = "iex", timeout: float = 20.0,
                 adjustment: str = "split", client: httpx.Client | None = None):
        if not str(key).strip() or not str(secret).strip():
            raise ValueError("ALPACA_API_KEY and ALPACA_API_SECRET are required")
        if feed not in FEEDS:
            raise ValueError("ALPACA_DATA_FEED must be iex, sip or delayed_sip")
        if adjustment not in ADJUSTMENTS:
            raise ValueError("adjustment must be raw, split, dividend or all")
        self.feed = feed
        self.adjustment = adjustment
        headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
        self._client = client or httpx.Client(timeout=timeout, headers=headers)
        if client is not None:
            self._client.headers.update(headers)
        self._owns_client = client is None

    def historical(self, symbol, timeframe, start=None, end=None) -> list[Candle]:
        self.check_timeframe(timeframe)
        symbol = validate_us_symbol(symbol)
        if start is None or end is None:
            raise ValueError("Alpaca history requires explicit start and end timestamps")
        start, end = parse_timestamp(start), parse_timestamp(end)
        if end <= start:
            raise ValueError("Historical range end must be after start")
        params = {"symbols": symbol, "timeframe": _TF_API[timeframe],
                  "start": start.isoformat().replace("+00:00", "Z"),
                  "end": end.isoformat().replace("+00:00", "Z"),
                  "adjustment": self.adjustment, "feed": self.feed,
                  "sort": "asc", "limit": 10000}
        rows: list[Candle] = []
        page_token = None
        seen_tokens = set()
        while True:
            query = dict(params)
            if page_token:
                query["page_token"] = page_token
            response = self._client.get(f"{_DATA_HOST}/v2/stocks/bars", params=query)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("bars"), dict):
                raise ValueError("Alpaca returned an invalid bars response")
            for row in payload["bars"].get(symbol, []):
                try:
                    candle = Candle(parse_timestamp(row["t"]), float(row["o"]),
                                    float(row["h"]), float(row["l"]), float(row["c"]),
                                    float(row["v"]), symbol)
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError("Alpaca returned an invalid bar") from exc
                if start <= candle.timestamp < end:
                    rows.append(candle)
            page_token = payload.get("next_page_token")
            if not page_token:
                break
            if page_token in seen_tokens or len(seen_tokens) >= 1000:
                raise ValueError("Alpaca pagination did not advance safely")
            seen_tokens.add(page_token)
        if not rows:
            # An empty range is a data fact (holiday, halt, untraded symbol),
            # not corruption: return it as-is instead of failing the caller.
            return []
        report = validate(rows, timeframe, strict_gaps=False)
        report.require_valid()
        rows.sort(key=lambda bar: bar.timestamp)
        return rows

    def close(self):
        if self._owns_client:
            self._client.close()


class AlpacaStream(WebSocketProvider):
    """Read-only IEX/SIP stock bar stream; emits completed 1-minute bars only."""

    supported_timeframes = frozenset({"1m"})
    stale_after_seconds = 120
    max_reconnect_delay = 30
    handshake_timeout = 15

    def __init__(self, key: str, secret: str, symbols: list[str], *, feed: str = "iex"):
        if not str(key).strip() or not str(secret).strip():
            raise ValueError("ALPACA_API_KEY and ALPACA_API_SECRET are required")
        if feed not in FEEDS:
            raise ValueError("ALPACA_DATA_FEED must be iex, sip or delayed_sip")
        if not symbols or len(set(symbols)) != len(symbols):
            raise ValueError("Specify distinct Alpaca symbols")
        self.symbols = [validate_us_symbol(s) for s in symbols]
        self._key, self._secret, self.feed = key, secret, feed
        self._url = f"wss://stream.data.alpaca.markets/v2/{feed}"
        self.status, self.last_message = "DISCONNECTED", None
        self.closed, self._socket = False, None
        self.last_candle_time = None
        self.authenticated = False
        self.subscribed = False
        self.reconnects = 0
        self.errors: list[str] = []
        self.backfill: dict = {"requested": 0, "loaded": 0, "status": "not_requested",
                               "error": None}

    # ------------------------------------------------------------------ decode
    @staticmethod
    def decode(payload) -> Candle | None:
        """Decode finalized bar events; corrections are not replayed."""
        if not isinstance(payload, dict):
            raise ValueError("Invalid Alpaca stream message")
        kind = payload.get("T")
        if kind == "u":  # Alpaca late-trade correction to an already emitted minute.
            return None
        if kind != "b":
            return None
        symbol = validate_us_symbol(str(payload.get("S", "")))
        try:
            return Candle(parse_timestamp(payload["t"]), float(payload["o"]),
                          float(payload["h"]), float(payload["l"]), float(payload["c"]),
                          float(payload["v"]), symbol)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Alpaca returned an invalid bar") from exc

    # ---------------------------------------------------------------- warm-up
    async def warmup_bars(self, count: int) -> list[Candle]:
        """Completed 1m session bars strictly before *now*, newest last.

        Used only to prime indicators so a session started at 11:00 does not
        have to idle for ``history_limit`` minutes. Every returned bar is
        already complete at the time of the call, so this cannot introduce
        look-ahead. Bounded to 10 prior trading days.
        """
        from app.markets.us_calendar import USEquitiesCalendar

        count = int(count)
        self.backfill = {"requested": count, "loaded": 0, "status": "pending",
                         "error": None}
        if count <= 0:
            self.backfill["status"] = "disabled"
            return []
        calendar = USEquitiesCalendar()
        provider = AlpacaHistoricalProvider(
            self._key, self._secret, feed=self.feed, adjustment="split"
        )
        now = datetime.now(timezone.utc)
        collected: list[Candle] = []
        try:
            for symbol in self.symbols:
                rows: list[Candle] = []
                day = calendar.session_date(now)
                for _ in range(10):
                    bounds = calendar.session_bounds(day)
                    day = day - timedelta(days=1)
                    if bounds is None:
                        continue
                    end = min(bounds[1].astimezone(timezone.utc), now)
                    if end <= bounds[0].astimezone(timezone.utc):
                        continue
                    rows = await asyncio.to_thread(
                        provider.historical, symbol, "1m",
                        bounds[0].astimezone(timezone.utc), end,
                    ) + rows
                    if len(rows) >= count:
                        break
                collected.extend(rows[-count:])
        except Exception as exc:  # network/entitlement: degrade, never invent bars
            self.backfill.update(status="failed", error=type(exc).__name__)
            logger.warning("alpaca_warmup_failed", extra={"error": type(exc).__name__})
            return []
        finally:
            provider.close()
        collected.sort(key=lambda bar: (bar.timestamp, bar.symbol))
        # A bar is only usable if it is complete at "now" and inside a real
        # session; anything else is silently excluded rather than repaired.
        usable = [
            bar for bar in collected
            if bar.available_at("1m") <= now and calendar.is_open(bar.timestamp)
        ]
        self.backfill.update(status="ok" if usable else "empty", loaded=len(usable))
        return usable

    # ------------------------------------------------------------------ stream
    async def stream(self, symbols: list[str], timeframe: str):
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException

        self.check_timeframe(timeframe)
        if set(symbols) != set(self.symbols):
            raise ValueError("Alpaca subscription symbols do not match configured symbols")
        errors = 0
        while not self.closed:
            self.status = "CONNECTING"
            try:
                async with connect(self._url, ping_interval=20, ping_timeout=20,
                                   open_timeout=20, close_timeout=5,
                                   max_size=1_048_576) as socket:
                    self._socket = socket
                    await socket.send(json.dumps({"action": "auth", "key": self._key,
                                                  "secret": self._secret}))
                    await self._await_confirmation(socket, "authenticated")
                    self.authenticated = True
                    self.status = "CONNECTED"
                    await socket.send(json.dumps({"action": "subscribe",
                                                  "bars": self.symbols,
                                                  "updatedBars": self.symbols}))
                    await self._await_confirmation(socket, "subscription",
                                                   kind="subscription")
                    self.subscribed = True
                    async for raw in socket:
                        if self.closed:
                            break
                        self.last_message = datetime.now(timezone.utc).isoformat()
                        try:
                            payloads = json.loads(raw)
                        except (TypeError, json.JSONDecodeError) as exc:
                            raise ValueError("Alpaca sent malformed stream data") from exc
                        for payload in payloads if isinstance(payloads, list) else [payloads]:
                            if isinstance(payload, dict) and payload.get("T") == "error":
                                raise ValueError(
                                    "Alpaca stream rejected authentication/subscription"
                                )
                            bar = self.decode(payload)
                            if bar is None:
                                continue
                            if bar.symbol not in self.symbols:
                                raise ValueError("Unsubscribed stream symbol")
                            validate([bar], "1m").require_valid()
                            self.last_candle_time = bar.available_at("1m").isoformat()
                            yield bar
            except (OSError, TimeoutError, ConnectionClosed, InvalidStatus,
                    WebSocketException) as exc:
                self.reconnects += 1
                self.status = "RECONNECTING"
                self.authenticated = self.subscribed = False
                logger.warning("alpaca_stream_reconnect",
                               extra={"error": type(exc).__name__, "attempt": self.reconnects})
                if self.closed:
                    break
                errors += 1
                # Bounded: a permanently broken feed eventually gives up instead
                # of reconnecting forever behind a green "RECONNECTING" badge.
                if errors > 50:
                    raise ValueError("Alpaca stream failed repeatedly") from exc
                await asyncio.sleep(min(self.max_reconnect_delay, 2 ** min(self.reconnects, 5)))
            finally:
                self._socket = None
        self.status = "DISCONNECTED"

    async def _await_confirmation(self, socket, expected: str, *, kind: str = "success"):
        """Read frames until the expected success message arrives.

        Alpaca sends ``[{"T":"success","msg":"connected"}]`` on connect, then
        ``[{"T":"success","msg":"authenticated"}]``. Treating the first frame as
        the authentication answer made every real session fail its handshake.
        """
        deadline = asyncio.get_running_loop().time() + self.handshake_timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise ValueError("Alpaca did not confirm authentication")
            try:
                raw = await asyncio.wait_for(socket.recv(), timeout=remaining)
                payloads = json.loads(raw)
            except (TimeoutError, TypeError, json.JSONDecodeError) as exc:
                raise ValueError("Alpaca did not confirm authentication") from exc
            for payload in payloads if isinstance(payloads, list) else [payloads]:
                if not isinstance(payload, dict):
                    continue
                if payload.get("T") == "error":
                    self.errors.append(str(payload.get("code", "error")))
                    raise ValueError("Alpaca stream rejected authentication/subscription")
                if payload.get("T") == "success" and payload.get("msg") == expected:
                    return
                if kind == "subscription" and payload.get("T") == "subscription":
                    subscribed = set(payload.get("bars") or [])
                    if subscribed != set(self.symbols):
                        raise ValueError("Alpaca did not confirm all requested bar symbols")
                    # Some feeds do not echo `updatedBars`; absence is not a
                    # failure, a *different* set is.
                    updated = payload.get("updatedBars")
                    if updated is not None and set(updated) != set(self.symbols):
                        raise ValueError(
                            "Alpaca did not confirm all correction subscriptions"
                        )
                    return

    # ----------------------------------------------------------------- metrics
    def metrics(self):
        now = datetime.now(timezone.utc)
        stale = None
        stale_reason = None
        if self.last_candle_time:
            age = (now - parse_timestamp(self.last_candle_time)).total_seconds()
            stale = age > self.stale_after_seconds
            if stale:
                stale_reason = "no_completed_bar_recently"
        try:
            from app.markets.us_calendar import USEquitiesCalendar

            calendar = USEquitiesCalendar()
            session_open = calendar.is_open(now)
            clock = "OPEN" if session_open else "CLOSED"
        except Exception:  # pragma: no cover — calendar is pure/deterministic
            session_open, clock = None, None
        if stale and session_open is False:
            # A closed market is not a broken feed. Say so instead of showing a
            # red badge every evening.
            stale, stale_reason = False, "market_closed"
        return {
            "provider": "alpaca", "feed_mode": self.feed, "timeframe": "1m",
            "connection_status": self.status, "authenticated": self.authenticated,
            "subscribed": self.subscribed, "last_message_time": self.last_message,
            "last_candle_time": self.last_candle_time, "reconnects": self.reconnects,
            "stale_feed": stale, "stale_reason": stale_reason,
            "market_status": clock, "market_open": session_open,
            "paper_mode": True, "paper_only": True,
            "symbols": list(self.symbols), "warmup": dict(self.backfill),
            "provider_errors": list(self.errors[-5:]),
        }

    async def close(self):
        self.closed = True
        if self._socket is not None:
            try:
                await self._socket.close()
            except Exception as exc:  # pragma: no cover — a dead socket is not an error
                logger.debug("alpaca_close_failed", extra={"error": type(exc).__name__})
        self.authenticated = False
        self.subscribed = False
        self.status = "DISCONNECTED"
