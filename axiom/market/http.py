"""Real market-data provider abstraction (JSON REST).

A thin, dependency-free adapter over a candle REST endpoint. It performs no
inference about the data: the timeframe is declared, the response is parsed
strictly, and anything unexpected raises ``MarketDataError`` rather than being
patched into something plausible.

Inject ``transport`` to unit-test without a network; the default transport is
``urllib.request`` so the base AXIOM runtime stays lightweight.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone
from math import isfinite
from typing import Any

from axiom.market.provider import (
    MarketDataBar,
    MarketDataError,
    MarketDataProvider,
    MarketDataResult,
)
from axiom.models import utcnow

DEFAULT_MAPPING = {
    "timestamp": "timestamp",
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "volume": "volume",
}

Transport = Callable[[str, dict[str, str], float], bytes]


def _default_transport(url: str, headers: dict[str, str], timeout: float) -> bytes:
    request = urllib.request.Request(url, headers={"Accept": "application/json", **headers})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise MarketDataError(f"market data request failed: {exc}") from exc


def parse_timestamp_field(value: Any) -> datetime:
    """ISO-8601 strings or epoch seconds/millis. Naive stamps are rejected."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 1e11:  # milliseconds
            seconds /= 1000.0
        parsed = datetime.fromtimestamp(seconds, timezone.utc)
    else:
        text = str(value).strip()
        if not text:
            raise MarketDataError("empty timestamp")
        if text.isdigit():
            return parse_timestamp_field(int(text))
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise MarketDataError(f"unparseable timestamp: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MarketDataError(
            f"timestamp {value!r} has no timezone; refusing to assume one"
        )
    return parsed.astimezone(timezone.utc)


#: JSON spellings that ``float()`` would happily accept but that are not prices.
_NON_FINITE_TEXT = {"nan", "+nan", "-nan", "inf", "+inf", "-inf", "infinity",
                    "+infinity", "-infinity"}


def parse_number_field(value: Any, field: str) -> float:
    """Coerce one JSON scalar to a finite float, refusing silent conversions.

    ``float()`` alone is too permissive at a data boundary:

    * ``float(True)`` is ``1.0``, so a boolean ``close`` becomes a real price;
    * ``json.loads`` accepts the bare literals ``NaN`` / ``Infinity`` even
      though they are not valid JSON, so a non-finite price can arrive here;
    * ``float(None)`` raises ``TypeError`` while ``float([])`` raises too, but
      neither message identifies the field.

    Anything this function refuses is reported as a malformed row rather than
    being patched into something plausible.
    """
    if isinstance(value, bool):
        raise MarketDataError(f"{field} is a boolean ({value!r}), not a number")
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text or text.lower() in _NON_FINITE_TEXT:
            raise MarketDataError(f"{field} is not a number: {value!r}")
        try:
            number = float(text)
        except ValueError as exc:
            raise MarketDataError(f"{field} is not a number: {value!r}") from exc
    else:
        raise MarketDataError(
            f"{field} has unsupported type {type(value).__name__}: {value!r}"
        )
    if not isfinite(number):
        raise MarketDataError(f"{field} is not finite: {value!r}")
    return number


class HttpJsonMarketDataProvider(MarketDataProvider):
    """Fetch recorded candles from a JSON REST endpoint."""

    name = "http-json"

    def __init__(
        self,
        url_template: str,
        *,
        timeframe: str | None = None,
        mapping: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 20.0,
        transport: Transport | None = None,
        candles_path: str | None = None,
        supported_timeframes: frozenset[str] | None = None,
        source: str | None = None,
    ) -> None:
        if "{instrument}" not in url_template:
            raise MarketDataError("url_template must contain {instrument}")
        super().__init__(source or f"http:{url_template}")
        self.url_template = url_template
        self.mapping = {**DEFAULT_MAPPING, **(mapping or {})}
        self.headers = dict(headers or {})
        self.timeout = timeout
        self.transport = transport or _default_transport
        self.candles_path = candles_path
        if supported_timeframes is not None:
            self.supported_timeframes = supported_timeframes
        if timeframe is not None:
            self.supported_timeframes = frozenset({timeframe})

    # -- helpers -------------------------------------------------------------
    def build_url(self, instrument: str, timeframe: str, limit: int | None) -> str:
        return self.url_template.format(
            instrument=urllib.parse.quote(instrument, safe=""),
            timeframe=urllib.parse.quote(timeframe),
            limit=limit if limit is not None else "",
        )

    def extract_rows(self, payload: Any) -> list[Any]:
        if isinstance(payload, list):
            return payload
        if not isinstance(payload, dict):
            raise MarketDataError(f"unexpected response type: {type(payload).__name__}")
        node: Any = payload
        if self.candles_path:
            for part in self.candles_path.split("."):
                if not isinstance(node, dict) or part not in node:
                    raise MarketDataError(
                        f"response has no '{self.candles_path}' path"
                    )
                node = node[part]
        else:
            for key in ("candles", "data", "results", "ohlc", "bars"):
                if key in node:
                    node = node[key]
                    break
        if isinstance(node, dict):
            for value in node.values():
                if isinstance(value, list):
                    node = value
                    break
        if not isinstance(node, list):
            raise MarketDataError("response contains no candle list")
        return node

    def parse_row(self, row: Any, instrument: str) -> MarketDataBar:
        if isinstance(row, dict):
            try:
                values = {
                    key: parse_number_field(row[self.mapping[key]], key)
                    for key in ("open", "high", "low", "close", "volume")
                }
                timestamp = parse_timestamp_field(row[self.mapping["timestamp"]])
            except (KeyError, TypeError, ValueError) as exc:
                raise MarketDataError(f"malformed candle row: {row!r} ({exc})") from exc
            symbol_field = self.mapping.get("instrument", "symbol")
            if symbol_field in row:
                symbol = row[symbol_field]
                if (not isinstance(symbol, str) or not symbol.strip()
                        or symbol.strip() != symbol):
                    raise MarketDataError(
                        "instrument identity in candle row must be a non-empty "
                        "trimmed string")
            else:
                symbol = instrument
            return MarketDataBar(timestamp, **values, instrument=symbol)
        if isinstance(row, (list, tuple)):
            if len(row) < 6:
                raise MarketDataError(
                    f"array candle needs [ts, o, h, l, c, v], got {row!r}"
                )
            try:
                timestamp = parse_timestamp_field(row[0])
                values = {
                    key: parse_number_field(row[index], key)
                    for index, key in enumerate(
                        ("open", "high", "low", "close", "volume"), start=1
                    )
                }
            except (TypeError, ValueError) as exc:
                raise MarketDataError(f"malformed candle row: {row!r} ({exc})") from exc
            return MarketDataBar(timestamp, **values, instrument=instrument)
        raise MarketDataError(f"unsupported candle representation: {row!r}")

    # -- MarketDataProvider --------------------------------------------------
    def fetch(
        self,
        instrument: str,
        timeframe: str,
        *,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> MarketDataResult:
        end, limit = self.validate_fetch_args(instrument, timeframe, end, limit)
        url = self.build_url(instrument, timeframe, limit)
        raw = self.transport(url, self.headers, self.timeout)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MarketDataError(f"response is not valid JSON: {exc}") from exc
        rows = self.extract_rows(payload)
        bars = tuple(self.parse_row(row, instrument) for row in rows)
        if end is not None:
            bars = tuple(bar for bar in bars if bar.timestamp < end)
        notes = ["fetched from remote endpoint"]
        if not bars:
            notes.append("endpoint returned no candles in range")
        return MarketDataResult(
            instrument=instrument,
            timeframe=timeframe,
            bars=bars,
            source=self.source,
            provider=self.name,
            retrieved_at=utcnow(),
            notes=tuple(notes),
        )
