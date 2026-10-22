"""Read-only Alpaca US equity market-data adapters.

This module contains no trading client, order URL, or order method. Paper fills
remain local to :class:`PaperBroker`.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import re

import httpx

from app.data.models import Candle, TIMEFRAMES, parse_timestamp, validate_symbol
from app.data.provider import APIProvider, WebSocketProvider
from app.data.validator import validate

logger = logging.getLogger("bot.market")
_TF_API = {"1m": "1Min", "5m": "5Min", "15m": "15Min", "30m": "30Min",
           "1h": "1Hour", "1D": "1Day"}
_TICKER = re.compile(r"^[A-Z][A-Z0-9.\-]{0,14}$")
_DATA_HOST = "https://data.alpaca.markets"


def validate_us_symbol(symbol: str) -> str:
    symbol = validate_symbol(symbol.strip().upper())
    if not _TICKER.fullmatch(symbol):
        raise ValueError("US paper symbols must be plain listed tickers, e.g. AAPL or BRK.B")
    return symbol


class AlpacaHistoricalProvider(APIProvider):
    """Paginated, read-only stock-bar history. Ranges use [start, end)."""

    supported_timeframes = frozenset(_TF_API)

    def __init__(self, key: str, secret: str, *, feed: str = "iex", timeout: float = 20.0,
                 client: httpx.Client | None = None):
        if not key.strip() or not secret.strip():
            raise ValueError("ALPACA_API_KEY and ALPACA_API_SECRET are required")
        if feed not in {"iex", "sip", "delayed_sip"}:
            raise ValueError("ALPACA_DATA_FEED must be iex, sip or delayed_sip")
        self.feed = feed
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
                  # Alpaca range endpoints are inclusive; request a point before
                  # the caller's exclusive end and filter locally below.
                  "end": end.isoformat().replace("+00:00", "Z"),
                  "adjustment": "raw", "feed": self.feed, "sort": "asc", "limit": 10000}
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
        report = validate(rows, timeframe, strict_gaps=False)
        report.require_valid()
        return rows

    def close(self):
        if self._owns_client:
            self._client.close()


class AlpacaStream(WebSocketProvider):
    """Read-only IEX/SIP stock bar stream; current version accepts completed 1m bars."""

    supported_timeframes = frozenset({"1m"})
    stale_after_seconds = 120

    def __init__(self, key: str, secret: str, symbols: list[str], *, feed: str = "iex"):
        if not key.strip() or not secret.strip():
            raise ValueError("ALPACA_API_KEY and ALPACA_API_SECRET are required")
        if feed not in {"iex", "sip", "delayed_sip"}:
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

    @staticmethod
    def decode(payload) -> Candle | None:
        """Decode only finalized bar events; correction events are not replayed."""
        if not isinstance(payload, dict):
            raise ValueError("Invalid Alpaca stream message")
        kind = payload.get("T")
        if kind == "u":  # Alpaca late-trade correction to an already emitted minute.
            return None
        if kind != "b":
            return None
        symbol = validate_us_symbol(str(payload.get("S", "")))
        return Candle(parse_timestamp(payload["t"]), float(payload["o"]),
                      float(payload["h"]), float(payload["l"]), float(payload["c"]),
                      float(payload["v"]), symbol)

    async def stream(self, symbols: list[str], timeframe: str):
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed, InvalidStatus

        self.check_timeframe(timeframe)
        if set(symbols) != set(self.symbols):
            raise ValueError("Alpaca subscription symbols do not match configured symbols")
        try:
            while not self.closed:
                self.status = "CONNECTING"
                try:
                    async with connect(self._url, ping_interval=20, ping_timeout=20,
                                       open_timeout=20, close_timeout=5,
                                       max_size=1_048_576) as socket:
                        self._socket = socket
                        await socket.send(json.dumps({"action": "auth", "key": self._key,
                                                      "secret": self._secret}))
                        await self._wait_for_status(socket, "authenticated")
                        self.authenticated = True
                        self.status = "CONNECTED"
                        await socket.send(json.dumps({"action": "subscribe", "bars": self.symbols,
                                                      "updatedBars": self.symbols}))
                        await self._wait_for_subscription(socket)
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
                                    raise ValueError("Alpaca stream rejected authentication/subscription")
                                bar = self.decode(payload)
                                if bar is not None:
                                    if bar.symbol not in self.symbols:
                                        raise ValueError("Unsubscribed stream symbol")
                                    validate([bar], "1m").require_valid()
                                    self.last_candle_time = bar.available_at("1m").isoformat()
                                    yield bar
                except (OSError, TimeoutError, ConnectionClosed, InvalidStatus) as exc:
                    self.reconnects += 1
                    self.status = "RECONNECTING"
                    self.authenticated = self.subscribed = False
                    logger.warning("alpaca_stream_reconnect", extra={"error": type(exc).__name__})
                    if not self.closed:
                        await asyncio.sleep(min(30, 2 ** min(self.reconnects, 5)))
                finally:
                    self._socket = None
        finally:
            self.status = "DISCONNECTED"

    async def _wait_for_status(self, socket, expected: str):
        try:
            raw = await asyncio.wait_for(socket.recv(), timeout=15)
            payloads = json.loads(raw)
        except (TimeoutError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Alpaca did not confirm authentication") from exc
        payloads = payloads if isinstance(payloads, list) else [payloads]
        for payload in payloads:
            if isinstance(payload, dict) and payload.get("T") == "error":
                raise ValueError("Alpaca stream authentication failed")
            if isinstance(payload, dict) and payload.get("T") == "success" and payload.get("msg") == expected:
                return
        raise ValueError("Alpaca did not confirm authentication")

    async def _wait_for_subscription(self, socket):
        try:
            raw = await asyncio.wait_for(socket.recv(), timeout=15)
            payloads = json.loads(raw)
        except (TimeoutError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Alpaca did not confirm the bar subscription") from exc
        payloads = payloads if isinstance(payloads, list) else [payloads]
        for payload in payloads:
            if isinstance(payload, dict) and payload.get("T") == "error":
                raise ValueError("Alpaca stream subscription failed")
            if isinstance(payload, dict) and payload.get("T") == "subscription":
                if set(payload.get("bars", [])) != set(self.symbols):
                    raise ValueError("Alpaca did not confirm all requested bar symbols")
                if set(payload.get("updatedBars", [])) != set(self.symbols):
                    raise ValueError("Alpaca did not confirm all correction subscriptions")
                return
        raise ValueError("Alpaca did not confirm the bar subscription")

    def metrics(self):
        stale = None
        if self.last_candle_time:
            stale = (datetime.now(timezone.utc) - parse_timestamp(self.last_candle_time)).total_seconds() > self.stale_after_seconds
        return {"provider": "alpaca", "feed_mode": self.feed, "timeframe": "1m",
                "connection_status": self.status, "authenticated": self.authenticated,
                "subscribed": self.subscribed, "last_message_time": self.last_message,
                "last_candle_time": self.last_candle_time, "reconnects": self.reconnects,
                "stale_feed": stale, "paper_mode": True}

    async def close(self):
        self.closed = True
        if self._socket is not None:
            await self._socket.close()
        self.status = "DISCONNECTED"
