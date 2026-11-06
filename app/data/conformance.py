"""Read-only conformance suite for the Upstox and Alpaca market-data adapters.

Purpose
-------
Prove, deterministically and without any network access, that each adapter:

* parses the documented provider responses it claims to support,
* rejects malformed / partial / unauthorised / throttled / stale input by
  failing closed instead of substituting fixture or sample data,
* normalizes to AXIOM's single ``Candle``/quote contract (UTC, ordered, finite),
* reaches the **local paper** engine through that normalized contract, and
* never touches a broker order endpoint, a credential store or a real account.

Everything here runs against ``httpx.MockTransport`` doubles and synthetic
fixtures, so a PASS result says **nothing** about live provider connectivity or
production data quality. The report states that explicitly; live connectivity is
reported as ``UNVERIFIED`` and the optional ``python -m app provider-check
--live`` probe is the only path that opens a socket (and it is never invoked by
this suite or by the test suite).

The report is machine readable::

    {
      "schema": "axiom.provider-conformance.v1",
      "suite_version": "1.1.0",
      "data_source_version": "axiom-synthetic-fixtures-2026-11-06",
      "status": "PASS" | "FAIL" | "UNVERIFIED",
      "network_calls": 0,
      "broker_order_api_calls": 0,
      "live_trading": false,
      "providers": {
        "upstox": {"status": ..., "capabilities": {...}, "checks": [...],
                    "limitations": [...]},
        "alpaca": {...}
      },
      "cross_market": {...},
      "redaction": {"fields": [...], "secrets_found": false}
    }
"""

from __future__ import annotations

import json
import math
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import httpx

CONFORMANCE_SCHEMA = "axiom.provider-conformance.v1"
SUITE_VERSION = "1.1.0"
# Version of the bundled synthetic fixture set. Bump it when a fixture changes
# so a stored report can be tied to the exact data it was produced from.
DATA_SOURCE_VERSION = "axiom-synthetic-fixtures-2026-11-06"

UTC = timezone.utc
STATUSES = ("PASS", "FAIL", "UNVERIFIED")

# Monday 2026-03-02 (before the US DST switch) so both calendars are stable.
NSE_NOW = datetime(2026, 3, 2, 5, 30, tzinfo=UTC)  # 11:00 IST — NSE cash open
US_NOW = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)  # 10:00 EST — NASDAQ open

UPSTOX_KEY = "NSE_EQ|INE002A01018"
UPSTOX_SYMBOL = "NSE_EQ-INE002A01018"
UPSTOX_TRADING_SYMBOL = "RELIANCE"
ALPACA_SYMBOL = "AAPL"

# Synthetic credentials. They are only ever used as literal mock values and are
# asserted absent from the rendered report.
SYNTHETIC_UPSTOX_TOKEN = "conformance-synthetic-upstox-token-not-real"
SYNTHETIC_ALPACA_KEY = "conformance-synthetic-alpaca-key-not-real"
SYNTHETIC_ALPACA_SECRET = "conformance-synthetic-alpaca-secret-not-real"

SENSITIVE_FIELDS = (
    "access_token", "api_key", "api_secret", "authorization", "bearer", "client_id",
    "client_secret", "secret", "token", "account_id", "account_number", "apca-api-key-id",
    "apca-api-secret-key", "password", "authorized_redirect_uri",
)


# --------------------------------------------------------------------------- #
# Report primitives
# --------------------------------------------------------------------------- #
@dataclass
class CheckResult:
    check_id: str
    capability: str
    status: str
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.check_id,
            "capability": self.capability,
            "status": self.status,
            "detail": _bounded(self.detail),
        }


@dataclass
class ProviderReport:
    provider: str
    checks: list[CheckResult] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    requests: list[dict[str, str]] = field(default_factory=list)

    def add(self, check_id: str, capability: str, ok: bool, detail: str = "") -> None:
        self.checks.append(
            CheckResult(check_id, capability, "PASS" if ok else "FAIL", detail)
        )

    def add_status(self, check_id: str, capability: str, status: str, detail: str = "") -> None:
        self.checks.append(CheckResult(check_id, capability, status, detail))

    @property
    def status(self) -> str:
        statuses = [c.status for c in self.checks]
        if "FAIL" in statuses:
            return "FAIL"
        if not statuses or "UNVERIFIED" in statuses:
            return "UNVERIFIED"
        return "PASS"

    def capabilities(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for check in self.checks:
            bucket = out.setdefault(check.capability, {"status": "PASS", "checks": []})
            bucket["checks"].append(check.check_id)
            if check.status == "FAIL":
                bucket["status"] = "FAIL"
            elif check.status == "UNVERIFIED" and bucket["status"] == "PASS":
                bucket["status"] = "UNVERIFIED"
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "status": self.status,
            "capabilities": self.capabilities(),
            "checks": [check.as_dict() for check in self.checks],
            "limitations": list(self.limitations),
            "requests_issued": len(self.requests),
            "request_methods": sorted({r["method"] for r in self.requests}),
            "request_paths": sorted({_url_path(r["url"]) for r in self.requests}),
        }


def _bounded(text: str, limit: int = 240) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _url_path(url: str) -> str:
    try:
        return httpx.URL(url).path or "/"
    except Exception:  # pragma: no cover - defensive for odd test doubles
        return str(url).split("?")[0]


def redact_payload(value: Any) -> Any:
    """Recursively replace sensitive values so a report can be shared safely."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if str(key).strip().lower() in SENSITIVE_FIELDS:
                out[key] = "[REDACTED]"
            else:
                out[key] = redact_payload(item)
        return out
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    return value


def contains_secret(payload: Any, secrets: Iterable[str]) -> bool:
    text = json.dumps(payload, default=str)
    return any(secret and secret in text for secret in secrets)


# --------------------------------------------------------------------------- #
# Deterministic doubles
# --------------------------------------------------------------------------- #
def recording_transport(
    responder: Callable[[httpx.Request], httpx.Response],
    log: list[dict[str, str]],
) -> httpx.MockTransport:
    """Wrap a responder so every request is recorded (read-only introspection)."""

    def handler(request: httpx.Request) -> httpx.Response:
        log.append({"method": request.method, "url": str(request.url)})
        return responder(request)

    return httpx.MockTransport(handler)


def json_responder(payload: Any, status_code: int = 200) -> Callable[[httpx.Request], httpx.Response]:
    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload, request=request)

    return responder


def error_responder(status_code: int) -> Callable[[httpx.Request], httpx.Response]:
    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": "synthetic"}, request=request)

    return responder


def timeout_responder(exc_factory: Callable[[str], Exception]) -> Callable[[httpx.Request], httpx.Response]:
    def responder(request: httpx.Request) -> httpx.Response:
        raise exc_factory(f"synthetic timeout for {request.url.host}")

    return responder


# --------------------------------------------------------------------------- #
# Synthetic fixtures (fixture version: DATA_SOURCE_VERSION)
# --------------------------------------------------------------------------- #
def _epoch(day: datetime, *, minutes: int = 0) -> int:
    return int((day + timedelta(minutes=minutes)).timestamp())


def _upstox_ltpc_frame(price: float, ts: datetime, *, qty: int = 10_000, initial: bool = False) -> bytes:
    from app.data.upstox_proto.MarketDataFeed_pb2 import FeedResponse

    seconds = int(ts.timestamp())
    response = FeedResponse(type=0 if initial else 1, currentTs=int(seconds * 1000))
    feed = response.feeds[UPSTOX_KEY]
    feed.ltpc.ltp = float(price)
    feed.ltpc.ltt = int(seconds * 1000)
    feed.ltpc.ltq = int(qty)
    return response.SerializeToString()


def _upstox_ohlc_frame(
    open_ts: datetime, o: float, h: float, low: float, c: float, v: int, *, interval: str = "1m"
) -> bytes:
    from app.data.upstox_proto.MarketDataFeed_pb2 import FeedResponse

    response = FeedResponse(type=1, currentTs=int((open_ts.timestamp() + 60) * 1000))
    bar = response.feeds[UPSTOX_KEY].fullFeed.marketFF.marketOHLC.ohlc.add()
    bar.interval = interval
    bar.ts = int(open_ts.timestamp())
    bar.open, bar.high, bar.low, bar.close, bar.vol = o, h, low, c, int(v)
    return response.SerializeToString()


def _upstox_market_info_frame(segment: str, status: int = 2) -> bytes:
    from app.data.upstox_proto.MarketDataFeed_pb2 import FeedResponse

    response = FeedResponse(type=2, currentTs=int(NSE_NOW.timestamp() * 1000))
    response.marketInfo.segmentStatus[segment] = status
    return response.SerializeToString()


def upstox_quote_payload(
    *,
    last: float = 2600.5,
    bid: float = 2600.0,
    ask: float = 2601.0,
    volume: float = 123_456,
    stamp: datetime | None = None,
    include_last: bool = True,
    instrument_token: str = UPSTOX_KEY,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "instrument_token": instrument_token,
        "volume": volume,
        "timestamp": (stamp or (NSE_NOW - timedelta(seconds=5))).isoformat(),
        "ohlc": {"open": 2590.0, "high": 2602.0, "low": 2588.0, "close": 2595.0},
        "depth": {
            "buy": [{"price": bid, "quantity": 100}],
            "sell": [{"price": ask, "quantity": 120}],
        },
    }
    if include_last:
        entry["last_price"] = last
    return {"status": "success", "data": {UPSTOX_TRADING_SYMBOL: entry}}


def alpaca_bars_payload(
    *,
    symbol: str = ALPACA_SYMBOL,
    start: datetime | None = None,
    count: int = 3,
    minutes: int = 5,
    next_page_token: str | None = None,
) -> dict[str, Any]:
    start = start or (US_NOW - timedelta(minutes=minutes * count))
    bars = []
    price = 220.0
    for index in range(count):
        stamp = start + timedelta(minutes=minutes * index)
        bars.append(
            {
                "t": stamp.isoformat().replace("+00:00", "Z"),
                "o": round(price, 2),
                "h": round(price + 0.4, 2),
                "l": round(price - 0.3, 2),
                "c": round(price + 0.2, 2),
                "v": 10_000 + index,
                "n": 42,
                "vw": round(price, 4),
            }
        )
        price += 0.2
    payload: dict[str, Any] = {"bars": {symbol: bars}, "next_page_token": next_page_token}
    if next_page_token is None:
        payload["next_page_token"] = None
    return payload


def alpaca_snapshot_payload(
    *,
    symbol: str = ALPACA_SYMBOL,
    last: float = 220.5,
    bid: float = 220.45,
    ask: float = 220.55,
    volume: float = 88_000,
    stamp: datetime | None = None,
) -> dict[str, Any]:
    moment = (stamp or (US_NOW - timedelta(seconds=3))).isoformat().replace("+00:00", "Z")
    return {
        "symbol": symbol,
        "latestQuote": {"bp": bid, "ap": ask, "t": moment},
        "latestTrade": {"p": last, "s": 100, "t": moment},
        "dailyBar": {"o": 219.0, "h": 221.0, "l": 218.5, "c": last, "v": volume, "t": moment},
        "prevDailyBar": {"c": 218.0, "t": moment},
    }


def synthetic_minute_bars(
    symbol: str, *, end: datetime, count: int = 40, minutes: int = 5, base: float = 100.0, step: float = 0.15
) -> list[Any]:
    """Deterministic, ordered, positive candles ending strictly before ``end``."""
    from app.data.models import Candle

    bars = []
    start = end - timedelta(minutes=minutes * count)
    price = base
    for index in range(count):
        stamp = start + timedelta(minutes=minutes * index)
        nxt = price + step
        bars.append(
            Candle(
                timestamp=stamp,
                open=round(price, 4),
                high=round(max(price, nxt) + 0.05, 4),
                low=round(min(price, nxt) - 0.05, 4),
                close=round(nxt, 4),
                volume=1_000 + index * 10,
                symbol=symbol,
            )
        )
        price = nxt
    return bars


# --------------------------------------------------------------------------- #
# Instrument resolvers used by the paper engine in these checks
# --------------------------------------------------------------------------- #
def _resolve_nse(symbol_or_key: str) -> dict[str, Any] | None:
    def normalise(value: str) -> str:
        return str(value).strip().upper().replace("|", "-")

    if normalise(symbol_or_key) not in {UPSTOX_SYMBOL, UPSTOX_TRADING_SYMBOL}:
        return None
    return {
        "instrument_key": UPSTOX_KEY,
        "trading_symbol": UPSTOX_TRADING_SYMBOL,
        "display_symbol": UPSTOX_SYMBOL,
        "exchange": "NSE",
        "market": "NSE",
        "currency": "INR",
    }


def _resolve_nasdaq(symbol_or_key: str) -> dict[str, Any] | None:
    if str(symbol_or_key).strip().upper() != ALPACA_SYMBOL:
        return None
    return {
        "symbol": ALPACA_SYMBOL,
        "instrument_key": f"NASDAQ|{ALPACA_SYMBOL}",
        "exchange": "NASDAQ",
        "market": "NASDAQ",
        "currency": "USD",
    }


# Documented read-only endpoints these adapters may use. The hosts are taken
# from the adapter modules themselves, so this module never has to name a
# trading host of its own (the repository guard forbids that outside the gated
# broker package).
READ_ONLY_PATH_PREFIXES = (
    "/v2/market-quote",                     # Upstox full market quote (data)
    "/v3/feed/market-data-feed/authorize",  # Upstox feed authorization (data)
    "/v2/stocks",                           # Alpaca stock bars/snapshots (data)
    "/v1/assets",                           # Alpaca asset metadata
    "/v2/assets",                           # Alpaca asset universe
)
_FORBIDDEN_PATH_MARKERS = ("order", "position", "holding", "account", "funds", "margin")


def _documented_read_hosts() -> tuple[str, ...]:
    from urllib.parse import urlsplit

    from app.autonomy.upstox import API_BASE
    from app.data.alpaca import _ASSET_HOST, _DATA_HOST

    hosts = {urlsplit(API_BASE).hostname, urlsplit(_DATA_HOST).hostname, urlsplit(_ASSET_HOST).hostname}
    return tuple(sorted(host for host in hosts if host))


def assert_no_order_endpoints(requests: Iterable[dict[str, str]]) -> tuple[bool, str]:
    """Every recorded request must be a documented read-only market-data GET."""
    from urllib.parse import urlsplit

    hosts = _documented_read_hosts()
    bad: list[str] = []
    total = 0
    for request in requests:
        total += 1
        method = str(request.get("method", "")).upper()
        parsed = urlsplit(str(request["url"]))
        path = parsed.path or "/"
        if method != "GET":
            bad.append(f"{method} {path}")
            continue
        if any(marker in path.lower() for marker in _FORBIDDEN_PATH_MARKERS):
            bad.append(f"mutating endpoint {path}")
            continue
        if parsed.hostname not in hosts:
            bad.append(f"unexpected host {parsed.hostname}")
            continue
        if not any(path.startswith(prefix) for prefix in READ_ONLY_PATH_PREFIXES):
            bad.append(f"undocumented path {path}")
    if bad:
        return False, "non-read-only requests recorded: " + ", ".join(sorted(set(bad))[:5])
    return True, f"{total} recorded request(s), all read-only market-data GETs on the documented hosts"


# --------------------------------------------------------------------------- #
# Upstox conformance
# --------------------------------------------------------------------------- #
def _upstox_limitations() -> list[str]:
    return [
        "Deterministic protobuf/HTTP doubles only: no Upstox endpoint was contacted.",
        "Synthetic fixture prices are not market data; no production data-quality claim.",
        "The WebSocket transport is exercised through its decoder/candle-builder seam, "
        "not through a live socket; TLS pinning and venue latency remain unverified here.",
        "An index instrument key (e.g. NSE_INDEX|Nifty 50) is rejected by design.",
    ]


def _run_upstox(report: ProviderReport, workdir: Path) -> None:
    from app.data.market_events import MarketEventApplier
    from app.data.candles import LiveCandleBuilder
    from app.data.upstox_auth import verify_feed_credential
    from app.data.upstox_stream import (
        INSTRUMENT_KEY,
        UpstoxAuthError,
        UpstoxProtobufDecoder,
        UpstoxStream,
        from_symbol,
        to_symbol,
    )

    # -- instrument identity ---------------------------------------------------
    try:
        round_trip = from_symbol(to_symbol(UPSTOX_KEY)) == UPSTOX_KEY
        rejections = []
        for bad in ("NSE_INDEX|Nifty 50", "NSE_EQ|", "no-pipe", "nse_eq|ine002a01018"):
            try:
                key = from_symbol(bad)
                if not INSTRUMENT_KEY.fullmatch(key):
                    rejections.append(bad)
                else:
                    rejections.append(f"ACCEPTED:{bad}")
            except ValueError:
                rejections.append(bad)
        accepted = [r for r in rejections if str(r).startswith("ACCEPTED:")]
        report.add(
            "upstox.instrument_key_resolution",
            "instrument_resolution",
            round_trip and not accepted,
            f"round-trip {UPSTOX_KEY!r} ok={round_trip}; refused {len(rejections)} malformed key(s)"
            + (f"; wrongly accepted {accepted}" if accepted else ""),
        )
    except Exception as exc:  # pragma: no cover - defensive
        report.add("upstox.instrument_key_resolution", "instrument_resolution", False, f"{type(exc).__name__}: {exc}")

    # -- decoder + candle normalization (tick mode) ---------------------------
    try:
        decoder = UpstoxProtobufDecoder({UPSTOX_KEY: UPSTOX_SYMBOL}, "ltpc")
        builder = LiveCandleBuilder("1m", stale_after=60.0)
        applier = MarketEventApplier(builder, {UPSTOX_KEY: UPSTOX_SYMBOL})
        base = NSE_NOW - timedelta(minutes=6)
        prices = [2600.0, 2600.4, 2600.9, 2601.2]
        for index, price in enumerate(prices):
            stamp = base + timedelta(seconds=5 + index * 10)
            payload = _upstox_ltpc_frame(price, stamp, initial=(index == 0))
            for event in decoder.decode(payload, stamp.timestamp()):
                assert event.kind in {"snapshot", "live"}, event.kind
                assert event.exchange_ts is not None, "decoder dropped the exchange timestamp"
                applier.apply(event, stamp.timestamp())
        bars = builder.collect(now=(base + timedelta(minutes=2)).timestamp())
        ok = (
            len(bars) == 1
            and bars[0].timestamp.tzinfo is not None
            and bars[0].timestamp.utcoffset() == timedelta(0)
            and bars[0].symbol == UPSTOX_SYMBOL
            and bars[0].open == prices[0]
            and bars[0].high == max(prices)
            and bars[0].low == min(prices)
            and bars[0].close == prices[-1]
            and bars[0].volume > 0
        )
        report.add(
            "upstox.tick_normalization",
            "candles",
            ok,
            f"{len(bars)} completed 1m candle(s) from {len(prices)} ticks; UTC={bars[0].timestamp.isoformat() if bars else None}",
        )
    except Exception as exc:
        report.add("upstox.tick_normalization", "candles", False, f"{type(exc).__name__}: {exc}")

    # -- official interval bars (full mode) -----------------------------------
    try:
        decoder = UpstoxProtobufDecoder({UPSTOX_KEY: UPSTOX_SYMBOL}, "full")
        builder = LiveCandleBuilder("1m", stale_after=60.0)
        applier = MarketEventApplier(builder, {UPSTOX_KEY: UPSTOX_SYMBOL})
        open_ts = NSE_NOW - timedelta(minutes=4)
        frames = [
            _upstox_ohlc_frame(open_ts, 2600.0, 2601.0, 2599.0, 2600.5, 1000),
            _upstox_ohlc_frame(open_ts + timedelta(minutes=1), 2600.5, 2602.0, 2600.0, 2601.5, 1200),
            _upstox_ohlc_frame(open_ts, 2600.0, 2601.5, 2598.5, 2600.9, 1100, interval="30m"),
        ]
        for frame in frames:
            for event in decoder.decode(frame, NSE_NOW.timestamp()):
                applier.apply(event, NSE_NOW.timestamp())
        bars = builder.collect(now=(open_ts + timedelta(minutes=5)).timestamp())
        metrics = builder.metrics()
        ok = (
            len(bars) == 2
            and [b.timestamp for b in bars] == sorted(b.timestamp for b in bars)
            and bars[0].close == 2600.5
            and metrics["ignored_intervals"] == 1
        )
        report.add(
            "upstox.interval_bar_normalization",
            "candles",
            ok,
            f"{len(bars)} official bar(s) emitted, ignored_intervals={metrics['ignored_intervals']}",
        )
    except Exception as exc:
        report.add("upstox.interval_bar_normalization", "candles", False, f"{type(exc).__name__}: {exc}")

    # -- duplicates / out-of-order / regressions ------------------------------
    try:
        decoder = UpstoxProtobufDecoder({UPSTOX_KEY: UPSTOX_SYMBOL}, "ltpc")
        builder = LiveCandleBuilder("1m", stale_after=60.0)
        applier = MarketEventApplier(builder, {UPSTOX_KEY: UPSTOX_SYMBOL})
        base = NSE_NOW - timedelta(minutes=3)
        eight = base + timedelta(seconds=5)
        frame = _upstox_ltpc_frame(2600.0, eight)
        for _ in range(3):  # exact duplicate ticks must not triple the volume
            for event in decoder.decode(frame, eight.timestamp()):
                applier.apply(event, eight.timestamp())
        older = base - timedelta(minutes=1) + timedelta(seconds=10)
        for event in decoder.decode(_upstox_ltpc_frame(2590.0, older), older.timestamp()):
            applier.apply(event, older.timestamp())
        later = base + timedelta(minutes=1) + timedelta(seconds=5)
        for event in decoder.decode(_upstox_ltpc_frame(2602.0, later), later.timestamp()):
            applier.apply(event, later.timestamp())
        bars = builder.collect(now=(base + timedelta(minutes=4)).timestamp())
        metrics = builder.metrics()
        ok = (
            len(bars) == 2
            and metrics["duplicates"] == 2
            and metrics["out_of_order"] == 1
            and metrics["regressions"] == 0
            and [b.timestamp for b in bars] == sorted(b.timestamp for b in bars)
            and bars[0].volume == 10_000
        )
        report.add(
            "upstox.duplicates_and_ordering",
            "candles",
            ok,
            f"duplicates={metrics['duplicates']}, out_of_order={metrics['out_of_order']}, "
            f"regressions={metrics['regressions']}, candles={len(bars)}",
        )
    except Exception as exc:
        report.add("upstox.duplicates_and_ordering", "candles", False, f"{type(exc).__name__}: {exc}")

    # -- gaps are counted, never filled --------------------------------------
    try:
        decoder = UpstoxProtobufDecoder({UPSTOX_KEY: UPSTOX_SYMBOL}, "ltpc")
        builder = LiveCandleBuilder("1m", stale_after=600.0)
        applier = MarketEventApplier(builder, {UPSTOX_KEY: UPSTOX_SYMBOL})
        base = NSE_NOW - timedelta(minutes=10)
        for offset in (0, 1, 4):  # minutes 2 and 3 never arrive
            stamp = base + timedelta(minutes=offset, seconds=5)
            for event in decoder.decode(_upstox_ltpc_frame(2600.0 + offset, stamp), stamp.timestamp()):
                applier.apply(event, stamp.timestamp())
        bars = builder.collect(now=(base + timedelta(minutes=6)).timestamp())
        metrics = builder.metrics()
        ok = len(bars) == 3 and metrics["gaps"] == 1 and metrics["data_gap_detected"] is True
        report.add(
            "upstox.missing_bars_counted_not_filled",
            "freshness",
            ok,
            f"candles={len(bars)} (no synthetic fills), gaps={metrics['gaps']}",
        )
    except Exception as exc:
        report.add("upstox.missing_bars_counted_not_filled", "freshness", False, f"{type(exc).__name__}: {exc}")

    # -- malformed input fails closed ----------------------------------------
    try:
        decoder = UpstoxProtobufDecoder({UPSTOX_KEY: UPSTOX_SYMBOL}, "ltpc")
        events = decoder.decode(b"\x00\xffnot-a-protobuf-frame", NSE_NOW.timestamp())
        builder = LiveCandleBuilder("1m", stale_after=60.0)
        builder.ingest_tick(UPSTOX_SYMBOL, NSE_NOW.timestamp(), 0.0, 10)   # zero price
        builder.ingest_tick(UPSTOX_SYMBOL, NSE_NOW.timestamp(), -5.0, 10)  # negative price
        builder.ingest_tick(UPSTOX_SYMBOL, NSE_NOW.timestamp(), float("nan"), 10)
        builder.ingest_tick(UPSTOX_SYMBOL, NSE_NOW.timestamp(), 2600.0, -1)  # negative size
        bars = builder.collect(now=NSE_NOW.timestamp())
        ok = (
            events == []
            and decoder.malformed == 1
            and builder.metrics()["malformed"] == 4
            and bars == []
        )
        report.add(
            "upstox.malformed_frames_fail_closed",
            "error_handling",
            ok,
            f"decoder.malformed={decoder.malformed}, builder.malformed={builder.metrics()['malformed']}, candles={len(bars)}",
        )
    except Exception as exc:
        report.add("upstox.malformed_frames_fail_closed", "error_handling", False, f"{type(exc).__name__}: {exc}")

    # -- stale detection ------------------------------------------------------
    try:
        builder = LiveCandleBuilder("1m", stale_after=60.0)
        stamp = NSE_NOW - timedelta(minutes=5)
        builder.ingest_tick(UPSTOX_SYMBOL, stamp.timestamp(), 2600.0, 10, received_at=stamp.timestamp())
        fresh = builder.stale(now=(stamp + timedelta(seconds=30)).timestamp())
        stale = builder.stale(now=(stamp + timedelta(seconds=90)).timestamp())
        report.add(
            "upstox.stale_feed_detection",
            "freshness",
            fresh is False and stale is True,
            f"stale(30s)={fresh}, stale(90s)={stale}, threshold=60s",
        )
    except Exception as exc:
        report.add("upstox.stale_feed_detection", "freshness", False, f"{type(exc).__name__}: {exc}")

    # -- authorize/auth failures (GET only, token never echoed) --------------
    try:
        outcomes = {}
        for status in (401, 403, 429):
            log: list[dict[str, str]] = []
            stream = UpstoxStream(
                SYNTHETIC_UPSTOX_TOKEN,
                [UPSTOX_KEY],
                timeframe="1m",
                mode="ltpc",
                http_transport=recording_transport(error_responder(status), log),
            )
            try:
                import asyncio

                asyncio.run(stream._authorize())
                outcomes[status] = "no error"
            except UpstoxAuthError as exc:
                outcomes[status] = str(exc)
                assert SYNTHETIC_UPSTOX_TOKEN not in str(exc), "token leaked in an auth error"
            assert log and all(r["method"] == "GET" for r in log), log

        from app.data.upstox_stream import AUTHORIZE_URL

        async def _verdict(status: int) -> str:
            check = await verify_feed_credential(
                SYNTHETIC_UPSTOX_TOKEN,
                authorize_url=AUTHORIZE_URL,
                transport=httpx.MockTransport(error_responder(status)),
            )
            assert SYNTHETIC_UPSTOX_TOKEN not in json.dumps(check.public_view()), "token leaked"
            return check.verdict

        import asyncio

        verdicts = {status: asyncio.run(_verdict(status)) for status in (401, 403)}
        ok = (
            outcomes[401].startswith("Upstox rejected")
            and outcomes[403].startswith("Upstox refused")
            and "429" in outcomes[429]
            and verdicts == {401: "rejected", 403: "forbidden"}
        )
        report.add(
            "upstox.auth_failures_fail_closed",
            "error_handling",
            ok,
            f"authorize verdicts 401/403 rejected, 429 surfaced, credential verdicts={verdicts}",
        )
    except Exception as exc:
        report.add("upstox.auth_failures_fail_closed", "error_handling", False, f"{type(exc).__name__}: {exc}")

    # -- quote snapshot parsing + paper validation ----------------------------
    from app.autonomy.upstox import NoTradedQuote, UpstoxReadOnly
    from app.markets.live_paper import LiveMarketPaperEngine

    def _engine(now: datetime, quote_transport_factory, bars: list[Any]):
        log: list[dict[str, str]] = []

        def quote_provider(instrument_key: str) -> dict[str, Any]:
            reader = UpstoxReadOnly(
                lambda: SYNTHETIC_UPSTOX_TOKEN,
                transport=quote_transport_factory(log),
            )
            snap = reader.quote_snapshot(instrument_key)
            return {
                "instrument_key": snap.instrument_key,
                "bid": snap.bid,
                "ask": snap.ask,
                "last": snap.last,
                "volume": snap.volume,
                "timestamp": snap.timestamp.isoformat(),
                "source_timestamp": snap.timestamp.isoformat(),
                "local_received_at": now.isoformat(),
                "feed": "upstox_nse_v2",
                "feed_entitlement_label": "Upstox NSE Cash Read-Only Feed (NSE_EQ)",
                "is_delayed": False,
                "source": "upstox-market-quote-v2",
            }

        engine = LiveMarketPaperEngine(
            workdir / f"upstox-{now.timestamp():.0f}-{id(log)}.sqlite3",
            nse_quote_provider=quote_provider,
            nse_bars_provider=lambda symbol, timeframe: bars,
            nse_instrument_resolver=_resolve_nse,
            nasdaq_instrument_resolver=_resolve_nasdaq,
            clock=lambda: now,
        )
        return engine, log

    bars = synthetic_minute_bars(UPSTOX_TRADING_SYMBOL, end=NSE_NOW, count=40, minutes=5, base=2500.0)

    def _paper_quote_status(payload: dict[str, Any], *, now: datetime = NSE_NOW) -> tuple[str, str, int, bool]:
        engine, log = _engine(now, lambda log: recording_transport(json_responder(payload), log), bars)
        try:
            result = engine.analyze_market("NSE", symbols=[UPSTOX_TRADING_SYMBOL], trigger="conformance")
            symbol = (result.get("symbols") or [{}])[0]
            return (
                str(symbol.get("status")),
                str(symbol.get("quote_status")),
                int(result.get("broker_order_api_calls", -1)),
                bool(result.get("simulated_only")),
            )
        finally:
            report.requests.extend(log)
            engine.close()

    try:
        status, quote_status, orders, simulated = _paper_quote_status(upstox_quote_payload())
        ok = status == "SUCCEEDED" and quote_status == "FRESH" and orders == 0 and simulated is True
        report.add(
            "upstox.quote_enters_paper",
            "paper_path",
            ok,
            f"symbol status={status}, quote_status={quote_status}, broker_order_api_calls={orders}",
        )
    except Exception as exc:
        report.add("upstox.quote_enters_paper", "paper_path", False, f"{type(exc).__name__}: {exc}")

    try:
        stale_stamp = NSE_NOW - timedelta(minutes=30)
        status, quote_status, orders, _ = _paper_quote_status(upstox_quote_payload(stamp=stale_stamp))
        ok = quote_status == "STALE_QUOTE" and orders == 0
        report.add(
            "upstox.stale_quote_refused",
            "freshness",
            ok,
            f"30-minute-old quote -> status={status}, quote_status={quote_status}",
        )
    except Exception as exc:
        report.add("upstox.stale_quote_refused", "freshness", False, f"{type(exc).__name__}: {exc}")

    try:
        _, quote_status, orders, _ = _paper_quote_status(upstox_quote_payload(last=0.0))
        ok = quote_status == "NO_TRADED_PRICE" and orders == 0
        report.add(
            "upstox.zero_price_refused",
            "error_handling",
            ok,
            f"last_price=0 -> quote_status={quote_status} (fail-closed, nothing substituted)",
        )
    except Exception as exc:
        report.add("upstox.zero_price_refused", "error_handling", False, f"{type(exc).__name__}: {exc}")

    try:
        _, quote_status, orders, _ = _paper_quote_status(upstox_quote_payload(include_last=False))
        ok = quote_status == "MALFORMED_QUOTE" and orders == 0
        report.add(
            "upstox.partial_quote_refused",
            "error_handling",
            ok,
            f"missing last_price -> quote_status={quote_status} (fail-closed)",
        )
    except Exception as exc:
        report.add("upstox.partial_quote_refused", "error_handling", False, f"{type(exc).__name__}: {exc}")

    # provider error statuses must surface as an explicit, observable quote
    # verdict; the symbol's price never becomes usable for a paper decision.
    for label, http_status, expected in (
        ("auth", 401, "AUTH_REJECTED"),
        ("throttled", 429, "RATE_LIMITED"),
        ("server_error", 500, "DISCONNECTED"),
    ):
        try:
            engine, log = _engine(
                NSE_NOW,
                lambda log, code=http_status: recording_transport(error_responder(code), log),
                bars,
            )
            try:
                result = engine.analyze_market("NSE", symbols=[UPSTOX_TRADING_SYMBOL], trigger="conformance")
            finally:
                report.requests.extend(log)
                engine.close()
            symbol = (result.get("symbols") or [{}])[0]
            ok = (
                str(symbol.get("quote_status")) == expected
                and symbol.get("trade_eligible_now") is False
                and int(result.get("broker_order_api_calls", -1)) == 0
            )
            report.add(
                f"upstox.provider_{label}_status",
                "error_handling",
                ok,
                f"HTTP {http_status} -> quote_status={symbol.get('quote_status')}, "
                f"trade_eligible_now={symbol.get('trade_eligible_now')}",
            )
        except Exception as exc:
            report.add(f"upstox.provider_{label}_status", "error_handling", False, f"{type(exc).__name__}: {exc}")

    # -- timeout is bounded and observable ------------------------------------
    try:
        engine, log = _engine(
            NSE_NOW,
            lambda log: recording_transport(timeout_responder(httpx.ReadTimeout), log),
            bars,
        )
        try:
            result = engine.analyze_market("NSE", symbols=[UPSTOX_TRADING_SYMBOL], trigger="conformance")
        finally:
            report.requests.extend(log)
            engine.close()
        symbol = (result.get("symbols") or [{}])[0]
        ok = (
            len(log) == 1
            and str(symbol.get("quote_status")) == "DISCONNECTED"
            and int(result.get("broker_order_api_calls", -1)) == 0
        )
        report.add(
            "upstox.timeout_bounded_no_retry_storm",
            "error_handling",
            ok,
            f"attempts={len(log)} (no implicit retry), quote_status={symbol.get('quote_status')}",
        )
    except Exception as exc:
        report.add("upstox.timeout_bounded_no_retry_storm", "error_handling", False, f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- #
# Alpaca conformance
# --------------------------------------------------------------------------- #
def _alpaca_limitations() -> list[str]:
    return [
        "Deterministic HTTP/WS doubles only: no Alpaca endpoint was contacted.",
        "IEX is a single venue; a mocked IEX quote says nothing about SIP entitlement "
        "or consolidated-tape coverage.",
        "Synthetic fixture prices are not market data; no production data-quality claim.",
        "WebSocket reconnects/auth handshakes are checked at the decode seam, not over a socket.",
    ]


def _run_alpaca(report: ProviderReport, workdir: Path) -> None:
    from app.data.alpaca import (
        AlpacaHistoricalProvider,
        AlpacaQuoteReader,
        AlpacaStream,
        feed_label,
    )

    # -- feed identity: never upgraded, never silently SIP --------------------
    try:
        iex = feed_label("iex")
        sip = feed_label("sip")
        delayed = feed_label("delayed_sip")
        rejected = False
        try:
            feed_label("consolidated")
        except ValueError:
            rejected = True
        ok = (
            "NOT the consolidated SIP tape" in iex
            and "all US exchanges" in sip
            and "delayed 15 minutes" in delayed
            and rejected
        )
        report.add(
            "alpaca.feed_identity_labels",
            "feed_identity",
            ok,
            "iex/sip/delayed_sip labelled honestly; unknown feed refused",
        )
    except Exception as exc:
        report.add("alpaca.feed_identity_labels", "feed_identity", False, f"{type(exc).__name__}: {exc}")

    try:
        log: list[dict[str, str]] = []
        client = httpx.Client(
            transport=recording_transport(json_responder(alpaca_snapshot_payload()), log)
        )
        reader = AlpacaQuoteReader(
            SYNTHETIC_ALPACA_KEY, SYNTHETIC_ALPACA_SECRET, feed="iex", client=client
        )
        quote = reader.latest_quote(ALPACA_SYMBOL)
        reader.close()
        report.requests.extend(log)
        ok = (
            quote["feed"] == "iex"
            and quote["feed_is_consolidated"] is False
            and quote["is_delayed"] is False
            and "IEX" in quote["feed_entitlement_label"]
            and quote["read_only"] is True
            and "feed=iex" in str(log[0]["url"])
        )
        report.add(
            "alpaca.iex_feed_identity_on_quote",
            "feed_identity",
            ok,
            f"feed={quote['feed']}, consolidated={quote['feed_is_consolidated']}, "
            f"request carried feed=iex",
        )
    except Exception as exc:
        report.add("alpaca.iex_feed_identity_on_quote", "feed_identity", False, f"{type(exc).__name__}: {exc}")

    # -- historical bars: valid, ordered, exclusive end ----------------------
    try:
        log: list[dict[str, str]] = []
        start = US_NOW - timedelta(minutes=30)
        client = httpx.Client(
            transport=recording_transport(
                json_responder(alpaca_bars_payload(start=start, count=6)), log
            )
        )
        provider = AlpacaHistoricalProvider(
            SYNTHETIC_ALPACA_KEY, SYNTHETIC_ALPACA_SECRET, feed="iex", client=client
        )
        bars = provider.historical(ALPACA_SYMBOL, "5m", start, US_NOW)
        provider.close()
        report.requests.extend(log)
        ok = (
            len(bars) == 6
            and [b.timestamp for b in bars] == sorted(b.timestamp for b in bars)
            and all(b.timestamp.utcoffset() == timedelta(0) for b in bars)
            and all(b.timestamp < US_NOW for b in bars)
            and all(b.symbol == ALPACA_SYMBOL for b in bars)
            and "adjustment=raw" in str(log[0]["url"])
        )
        report.add(
            "alpaca.historical_bars_normalized",
            "historical_bars",
            ok,
            f"{len(bars)} bars, ascending UTC, all < exclusive end, adjustment=raw requested",
        )
    except Exception as exc:
        report.add("alpaca.historical_bars_normalized", "historical_bars", False, f"{type(exc).__name__}: {exc}")

    # -- partial / missing symbol must not be substituted --------------------
    try:
        client = httpx.Client(transport=httpx.MockTransport(json_responder({"bars": {}, "next_page_token": None})))
        provider = AlpacaHistoricalProvider(
            SYNTHETIC_ALPACA_KEY, SYNTHETIC_ALPACA_SECRET, feed="iex", client=client
        )
        try:
            provider.historical(ALPACA_SYMBOL, "5m", US_NOW - timedelta(hours=2), US_NOW)
            detail, ok = "no error raised for an empty symbol response", False
        except ValueError as exc:
            detail, ok = f"empty bars for the symbol refused: {_bounded(str(exc), 80)}", True
        finally:
            provider.close()
        report.add("alpaca.missing_symbol_refused", "error_handling", ok, detail)
    except Exception as exc:
        report.add("alpaca.missing_symbol_refused", "error_handling", False, f"{type(exc).__name__}: {exc}")

    # -- malformed bars ------------------------------------------------------
    try:
        bad_rows = [
            ({"t": "2026-03-02T14:55:00Z", "o": "x", "h": 1, "l": 1, "c": 1, "v": 1}, "non-numeric"),
            ({"t": "2026-03-02T14:55:00Z", "o": 1, "h": 1, "l": 1, "v": 1}, "missing close"),
            ({"t": "not-a-time", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}, "missing timezone"),
        ]
        outcomes = []
        for row, label in bad_rows:
            client = httpx.Client(
                transport=httpx.MockTransport(json_responder({"bars": {ALPACA_SYMBOL: [row]}}))
            )
            provider = AlpacaHistoricalProvider(
                SYNTHETIC_ALPACA_KEY, SYNTHETIC_ALPACA_SECRET, feed="iex", client=client
            )
            try:
                provider.historical(ALPACA_SYMBOL, "5m", US_NOW - timedelta(hours=1), US_NOW)
                outcomes.append(f"{label}:ACCEPTED")
            except ValueError:
                outcomes.append(f"{label}:refused")
            finally:
                provider.close()
        ok = all(item.endswith(":refused") for item in outcomes)
        report.add(
            "alpaca.malformed_bars_refused",
            "error_handling",
            ok,
            "; ".join(outcomes),
        )
    except Exception as exc:
        report.add("alpaca.malformed_bars_refused", "error_handling", False, f"{type(exc).__name__}: {exc}")

    # -- pagination is bounded ----------------------------------------------
    try:
        start = US_NOW - timedelta(minutes=30)
        pages = {
            None: alpaca_bars_payload(start=start, count=3, next_page_token="page-2"),
            "page-2": alpaca_bars_payload(start=start + timedelta(minutes=15), count=3),
        }

        def responder(request: httpx.Request) -> httpx.Response:
            token = request.url.params.get("page_token")
            return httpx.Response(200, json=pages.get(token, pages[None]), request=request)

        log: list[dict[str, str]] = []
        client = httpx.Client(transport=recording_transport(responder, log))
        provider = AlpacaHistoricalProvider(
            SYNTHETIC_ALPACA_KEY, SYNTHETIC_ALPACA_SECRET, feed="iex", client=client
        )
        bars = provider.historical(ALPACA_SYMBOL, "5m", start, US_NOW)
        provider.close()
        report.requests.extend(log)
        ok = len(bars) == 6 and len(log) == 2 and all(b.timestamp < US_NOW for b in bars)
        report.add(
            "alpaca.pagination_bounded",
            "historical_bars",
            ok,
            f"{len(log)} request(s) for 2 pages, {len(bars)} bars, no duplicate timestamps",
        )
    except Exception as exc:
        report.add("alpaca.pagination_bounded", "historical_bars", False, f"{type(exc).__name__}: {exc}")

    try:
        sticky = httpx.MockTransport(
            json_responder(alpaca_bars_payload(count=1, next_page_token="same-token"))
        )
        provider = AlpacaHistoricalProvider(
            SYNTHETIC_ALPACA_KEY, SYNTHETIC_ALPACA_SECRET, feed="iex", client=httpx.Client(transport=sticky)
        )
        try:
            provider.historical(ALPACA_SYMBOL, "5m", US_NOW - timedelta(hours=1), US_NOW)
            detail, ok = "a repeated page token was accepted", False
        except ValueError as exc:
            detail, ok = f"repeated page token refused: {_bounded(str(exc), 70)}", True
        finally:
            provider.close()
        report.add("alpaca.pagination_no_infinite_loop", "error_handling", ok, detail)
    except Exception as exc:
        report.add("alpaca.pagination_no_infinite_loop", "error_handling", False, f"{type(exc).__name__}: {exc}")

    # -- provider error statuses --------------------------------------------
    try:
        outcomes = []
        for status, marker in ((401, "401"), (403, "403"), (429, "rate limit"), (500, "500")):
            client = httpx.Client(transport=httpx.MockTransport(error_responder(status)))
            provider = AlpacaHistoricalProvider(
                SYNTHETIC_ALPACA_KEY, SYNTHETIC_ALPACA_SECRET, feed="iex", client=client
            )
            try:
                provider.historical(ALPACA_SYMBOL, "5m", US_NOW - timedelta(hours=1), US_NOW)
                outcomes.append(f"{status}:ACCEPTED")
            except ValueError as exc:
                outcomes.append(f"{status}:{'ok' if marker in str(exc).lower() else 'wrong-message'}")
            except httpx.HTTPStatusError:
                outcomes.append(f"{status}:ok")
            finally:
                provider.close()
        ok = all(item.endswith(":ok") for item in outcomes)
        report.add(
            "alpaca.provider_error_statuses",
            "error_handling",
            ok,
            "; ".join(outcomes),
        )
    except Exception as exc:
        report.add("alpaca.provider_error_statuses", "error_handling", False, f"{type(exc).__name__}: {exc}")

    # -- stream decode: bars only, corrections ignored -----------------------
    try:
        bar = AlpacaStream.decode(
            {"T": "b", "S": ALPACA_SYMBOL, "t": "2026-03-02T14:59:00Z",
             "o": 220.0, "h": 220.4, "l": 219.8, "c": 220.2, "v": 1200}
        )
        correction = AlpacaStream.decode({"T": "u", "S": ALPACA_SYMBOL, "t": "2026-03-02T14:59:00Z"})
        control = AlpacaStream.decode({"T": "success", "msg": "authenticated"})
        rejected = 0
        for bad in (None, {"T": "b", "S": "", "t": "2026-03-02T14:59:00Z"}, {"T": "b", "S": "AAPL"}):
            try:
                AlpacaStream.decode(bad)
            except ValueError:
                rejected += 1
        ok = (
            bar is not None
            and bar.close == 220.2
            and bar.timestamp.utcoffset() == timedelta(0)
            and correction is None
            and control is None
            and rejected == 3
        )
        report.add(
            "alpaca.stream_decode_rules",
            "candles",
            ok,
            f"bar decoded, correction/control ignored, {rejected}/3 malformed frames refused",
        )
    except Exception as exc:
        report.add("alpaca.stream_decode_rules", "candles", False, f"{type(exc).__name__}: {exc}")

    # -- quote validation ----------------------------------------------------
    try:
        outcomes = []
        for label, payload in (
            ("inverted", {"latestQuote": {"bp": 221.0, "ap": 220.0, "t": "2026-03-02T14:59:00Z"},
                          "latestTrade": {"p": 220.5, "t": "2026-03-02T14:59:00Z"}}),
            ("non_finite", {"latestQuote": {"bp": "nan", "ap": 220.5, "t": "2026-03-02T14:59:00Z"},
                            "latestTrade": {"p": 220.5, "t": "2026-03-02T14:59:00Z"}}),
            ("no_price", {"latestQuote": {"bp": 0, "ap": 0, "t": "2026-03-02T14:59:00Z"},
                          "latestTrade": {}, "dailyBar": {}}),
        ):
            client = httpx.Client(transport=httpx.MockTransport(json_responder(payload)))
            reader = AlpacaQuoteReader(
                SYNTHETIC_ALPACA_KEY, SYNTHETIC_ALPACA_SECRET, feed="iex", client=client
            )
            try:
                reader.latest_quote(ALPACA_SYMBOL)
                outcomes.append(f"{label}:ACCEPTED")
            except ValueError:
                outcomes.append(f"{label}:refused")
            finally:
                reader.close()
        ok = all(item.endswith(":refused") for item in outcomes)
        report.add(
            "alpaca.malformed_quotes_refused",
            "error_handling",
            ok,
            "; ".join(outcomes),
        )
    except Exception as exc:
        report.add("alpaca.malformed_quotes_refused", "error_handling", False, f"{type(exc).__name__}: {exc}")

    # -- path into the local paper engine -----------------------------------
    from app.markets.live_paper import LiveMarketPaperEngine

    bars = synthetic_minute_bars(ALPACA_SYMBOL, end=US_NOW, count=40, minutes=5, base=220.0)

    def _alpaca_engine(now: datetime, payload: Any, log: list[dict[str, str]], bars_list=bars):
        def quote_provider(symbol: str) -> dict[str, Any]:
            client = httpx.Client(
                transport=recording_transport(
                    json_responder(payload) if not callable(payload) else payload, log
                )
            )
            reader = AlpacaQuoteReader(
                SYNTHETIC_ALPACA_KEY, SYNTHETIC_ALPACA_SECRET, feed="iex", client=client
            )
            try:
                return reader.latest_quote(symbol)
            finally:
                reader.close()

        return LiveMarketPaperEngine(
            workdir / f"alpaca-{now.timestamp():.0f}-{len(report.checks)}-{len(log)}.sqlite3",
            nasdaq_quote_provider=quote_provider,
            nasdaq_bars_provider=lambda symbol, timeframe: bars_list,
            nse_instrument_resolver=_resolve_nse,
            nasdaq_instrument_resolver=_resolve_nasdaq,
            clock=lambda: now,
        )

    try:
        log: list[dict[str, str]] = []
        engine = _alpaca_engine(US_NOW, alpaca_snapshot_payload(), log)
        try:
            result = engine.analyze_market("NASDAQ", symbols=[ALPACA_SYMBOL], trigger="conformance")
        finally:
            report.requests.extend(log)
            engine.close()
        symbol = (result.get("symbols") or [{}])[0]
        ok = (
            symbol.get("status") == "SUCCEEDED"
            and symbol.get("quote_status") == "FRESH"
            and int(result.get("broker_order_api_calls", -1)) == 0
            and bool(result.get("simulated_only")) is True
        )
        report.add(
            "alpaca.quote_enters_paper",
            "paper_path",
            ok,
            f"symbol status={symbol.get('status')}, quote_status={symbol.get('quote_status')}, "
            f"broker_order_api_calls={result.get('broker_order_api_calls')}",
        )
    except Exception as exc:
        report.add("alpaca.quote_enters_paper", "paper_path", False, f"{type(exc).__name__}: {exc}")

    try:
        stale = US_NOW - timedelta(minutes=45)
        log: list[dict[str, str]] = []
        engine = _alpaca_engine(US_NOW, alpaca_snapshot_payload(stamp=stale), log)
        try:
            result = engine.analyze_market("NASDAQ", symbols=[ALPACA_SYMBOL], trigger="conformance")
        finally:
            report.requests.extend(log)
            engine.close()
        symbol = (result.get("symbols") or [{}])[0]
        ok = symbol.get("quote_status") == "STALE_QUOTE" and int(result.get("broker_order_api_calls", -1)) == 0
        report.add(
            "alpaca.stale_quote_refused",
            "freshness",
            ok,
            f"45-minute-old quote -> quote_status={symbol.get('quote_status')}",
        )
    except Exception as exc:
        report.add("alpaca.stale_quote_refused", "freshness", False, f"{type(exc).__name__}: {exc}")

    try:
        log: list[dict[str, str]] = []
        engine = _alpaca_engine(US_NOW, lambda request: httpx.Response(429, json={}, request=request), log)
        try:
            result = engine.analyze_market("NASDAQ", symbols=[ALPACA_SYMBOL], trigger="conformance")
        finally:
            report.requests.extend(log)
            engine.close()
        symbol = (result.get("symbols") or [{}])[0]
        ok = (
            str(symbol.get("quote_status")) == "RATE_LIMITED"
            and symbol.get("trade_eligible_now") is False
            and int(result.get("broker_order_api_calls", -1)) == 0
        )
        report.add(
            "alpaca.throttling_observable",
            "error_handling",
            ok,
            f"HTTP 429 -> quote_status={symbol.get('quote_status')} (fail-closed, no silent retry)",
        )
    except Exception as exc:
        report.add("alpaca.throttling_observable", "error_handling", False, f"{type(exc).__name__}: {exc}")

    try:
        log: list[dict[str, str]] = []
        engine = _alpaca_engine(
            US_NOW,
            lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("synthetic", request=request)),
            log,
        )
        try:
            result = engine.analyze_market("NASDAQ", symbols=[ALPACA_SYMBOL], trigger="conformance")
        finally:
            report.requests.extend(log)
            engine.close()
        symbol = (result.get("symbols") or [{}])[0]
        ok = (
            len(log) == 1
            and str(symbol.get("quote_status")) == "DISCONNECTED"
            and int(result.get("broker_order_api_calls", -1)) == 0
        )
        report.add(
            "alpaca.timeout_bounded_no_retry_storm",
            "error_handling",
            ok,
            f"attempts={len(log)} (no implicit retry), quote_status={symbol.get('quote_status')}",
        )
    except Exception as exc:
        report.add("alpaca.timeout_bounded_no_retry_storm", "error_handling", False, f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- #
# Cross-market isolation + corporate-action provenance
# --------------------------------------------------------------------------- #
def _run_cross_market(report_out: dict[str, Any], workdir: Path) -> None:
    from app.markets.live_paper import LiveMarketPaperEngine

    checks: list[CheckResult] = []
    log: list[dict[str, str]] = []

    def failing_nse(_key: str) -> dict[str, Any]:
        raise TimeoutError("synthetic NSE provider disconnect")

    def healthy_nasdaq(symbol: str) -> dict[str, Any]:
        client = httpx.Client(transport=recording_transport(json_responder(alpaca_snapshot_payload()), log))
        from app.data.alpaca import AlpacaQuoteReader

        reader = AlpacaQuoteReader(
            SYNTHETIC_ALPACA_KEY, SYNTHETIC_ALPACA_SECRET, feed="iex", client=client
        )
        try:
            return reader.latest_quote(symbol)
        finally:
            reader.close()

    nse_bars = synthetic_minute_bars(UPSTOX_TRADING_SYMBOL, end=US_NOW, count=40, minutes=5, base=2500.0)
    us_bars = synthetic_minute_bars(ALPACA_SYMBOL, end=US_NOW, count=40, minutes=5, base=220.0)
    engine = LiveMarketPaperEngine(
        workdir / "cross-market.sqlite3",
        nse_quote_provider=failing_nse,
        nasdaq_quote_provider=healthy_nasdaq,
        nse_bars_provider=lambda symbol, timeframe: nse_bars,
        nasdaq_bars_provider=lambda symbol, timeframe: us_bars,
        nse_instrument_resolver=_resolve_nse,
        nasdaq_instrument_resolver=_resolve_nasdaq,
        clock=lambda: US_NOW,
    )
    try:
        engine.start_session("NSE", strategy="moving_average", timeframe="5m", symbols=[UPSTOX_TRADING_SYMBOL])
        engine.start_session("NASDAQ", strategy="moving_average", timeframe="5m", symbols=[ALPACA_SYMBOL])
        cycle = engine.advance_selected_markets_concurrently(
            markets=["NSE", "NASDAQ"], only_active=True, market_timeout_seconds=10.0
        )
        nse = cycle["markets"]["NSE"]
        us = cycle["markets"]["NASDAQ"]
        nse_symbols = ((nse.get("snapshot") or {}).get("last_analysis") or {}).get("symbols") or []
        us_counts = ((us.get("snapshot") or {}).get("last_analysis") or {}).get("counts") or {}
        nse_quote_status = str((nse_symbols[0] if nse_symbols else {}).get("quote_status"))
        advanced = (
            us.get("status") == "COMPLETED"
            and int(us.get("decisions_count", 0)) >= 1
            and nse.get("status") in {"COMPLETED", "FAILED", "TIMEOUT"}
            and nse_quote_status not in {"FRESH", "CLOSED_LAST_QUOTE", ""}
            and float(cycle.get("wall_ms", 0.0)) < 10_000.0
        )
        checks.append(
            CheckResult(
                "paper.failed_provider_does_not_block_other_market",
                "cross_market_isolation",
                "PASS" if advanced else "FAIL",
                f"NSE provider raises -> NSE {nse.get('status')} (quote {nse_quote_status or 'n/a'}), "
                f"{nse.get('decisions_count')} decision(s); NASDAQ {us.get('status')} with "
                f"{us.get('decisions_count')} decision(s) in {cycle.get('wall_ms')} ms "
                "(no cross-market blocking)",
            )
        )
        checks.append(
            CheckResult(
                "paper.zero_broker_order_calls",
                "cross_market_isolation",
                "PASS"
                if int(cycle.get("broker_order_api_calls", -1)) == 0
                and cycle.get("live_trading_enabled") is False
                and all(int(m.get("broker_order_api_calls", -1)) == 0 for m in cycle["markets"].values())
                else "FAIL",
                f"cycle broker_order_api_calls={cycle.get('broker_order_api_calls')}, "
                f"live_trading_enabled={cycle.get('live_trading_enabled')}",
            )
        )
        ok_readonly, detail = assert_no_order_endpoints(log)
        checks.append(CheckResult("paper.cross_market_requests_read_only", "cross_market_isolation",
                                  "PASS" if ok_readonly else "FAIL", detail))
        checks.append(
            CheckResult(
                "paper.paper_engine_cannot_reach_orders",
                "cross_market_isolation",
                "PASS"
                if getattr(LiveMarketPaperEngine, "broker_order_endpoints_reachable", None) is False
                else "FAIL",
                "LiveMarketPaperEngine.broker_order_endpoints_reachable is False",
            )
        )
    except Exception as exc:
        checks.append(
            CheckResult(
                "paper.failed_provider_does_not_block_other_market",
                "cross_market_isolation",
                "FAIL",
                f"{type(exc).__name__}: {exc}",
            )
        )
    finally:
        engine.close()

    # -- corporate actions: provenance survives, adjusted never executes ----
    try:
        from app.data.corporate_actions import (
            AdjustedSeriesError,
            AdjustmentPolicy,
            assert_raw_candles,
            block_for_meta,
        )
        from app.data.alpaca import AlpacaHistoricalProvider
        from app.model_lab.datasets import DatasetManager

        # 65 five-minute bars: the dataset importer requires >= 60 chronological bars.
        start = US_NOW - timedelta(minutes=5 * 65)
        client = httpx.Client(
            transport=httpx.MockTransport(json_responder(alpaca_bars_payload(start=start, count=65)))
        )
        provider = AlpacaHistoricalProvider(
            SYNTHETIC_ALPACA_KEY, SYNTHETIC_ALPACA_SECRET, feed="iex", client=client
        )
        try:
            bars = provider.historical(ALPACA_SYMBOL, "5m", start, US_NOW)
        finally:
            provider.close()

        book_payload = {
            "format": "axiom.corporate-actions",
            "format_version": 1,
            "provider": {"name": "synthetic-fixture-actions", "version": "1"},
            "actions": [
                {
                    "record_id": "conformance-nasdaq-split",
                    "revision": 1,
                    "action_type": "SPLIT",
                    "symbol": ALPACA_SYMBOL,
                    "market": "NASDAQ",
                    "ex_date": "2026-02-25",
                    "announced_at": "2026-02-10",
                    "ratio_from": 1,
                    "ratio_to": 2,
                    "source": "synthetic conformance fixture",
                    "source_version": "2026-02-10",
                    "provenance": "generated by app/data/conformance.py; not an exchange filing",
                }
            ],
        }
        manager = DatasetManager(workdir / "datasets")
        meta = manager.register_dataset(
            dataset_id="conformance-alpaca-5m",
            market="NASDAQ",
            symbol=ALPACA_SYMBOL,
            timeframe="5m",
            bars_or_csv=bars,
            source="alpaca-market-data:iex",
            feed="iex",
            corporate_action_adjustment="RECORDED",
            corporate_actions=book_payload,
            corporate_action_policy=AdjustmentPolicy.SPLIT_ADJUSTED.value,
        )
        stored_block = block_for_meta(meta)
        provenance_ok = (
            meta.get("source") == "alpaca-market-data:iex"
            and meta.get("feed") == "iex"
            and stored_block.get("status") == "SPLIT_ADJUSTED"
            and bool(stored_block.get("actions_sha256"))
            and (stored_block.get("provider") or {}).get("name") == "synthetic-fixture-actions"
        )
        checks.append(
            CheckResult(
                "corporate_actions.provenance_survives_import",
                "corporate_actions",
                "PASS" if provenance_ok else "FAIL",
                f"status={stored_block.get('status')}, provider="
                f"{(stored_block.get('provider') or {}).get('name')}, "
                f"actions_sha256={'present' if stored_block.get('actions_sha256') else 'MISSING'}",
            )
        )

        loaded_meta, raw_bars = manager.load_dataset("conformance-alpaca-5m")
        _, series = manager.load_adjusted_dataset("conformance-alpaca-5m", policy=AdjustmentPolicy.SPLIT_ADJUSTED)
        refused = False
        try:
            series.for_execution()
        except AdjustedSeriesError:
            refused = True
        raw_accepted = len(assert_raw_candles(raw_bars, context="conformance")) == len(raw_bars)
        checks.append(
            CheckResult(
                "corporate_actions.adjusted_never_executes",
                "corporate_actions",
                "PASS" if refused and raw_accepted and series.prices_adjusted else "FAIL",
                f"adjusted series refused at the execution boundary={refused}, "
                f"raw dataset still usable={raw_accepted}, prices_adjusted={series.prices_adjusted}",
            )
        )
        checks.append(
            CheckResult(
                "corporate_actions.raw_dataset_stays_raw",
                "corporate_actions",
                "PASS"
                if (loaded_meta.get("corporate_actions") or {}).get("policy") == AdjustmentPolicy.SPLIT_ADJUSTED.value
                and [b.close for b in raw_bars] == [b.close for b in bars]
                else "FAIL",
                "the stored dataset keeps raw provider prices while the research series is derived per call",
            )
        )
    except Exception as exc:
        checks.append(
            CheckResult(
                "corporate_actions.provenance_survives_import",
                "corporate_actions",
                "FAIL",
                f"{type(exc).__name__}: {exc}",
            )
        )

    report_out["checks"] = checks


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def run_conformance(*, providers: Iterable[str] = ("upstox", "alpaca"), now: datetime | None = None) -> dict[str, Any]:
    """Run the offline conformance suite and return the machine-readable report.

    ``now`` is accepted for reproducibility only; each provider check uses the
    fixed ``NSE_NOW``/``US_NOW`` session anchors so two runs on two machines
    produce the same verdicts.
    """
    requested = [str(p).strip().lower() for p in providers]
    unknown = [p for p in requested if p not in {"upstox", "alpaca"}]
    if unknown:
        raise ValueError(f"unsupported provider(s): {unknown}; choose upstox and/or alpaca")

    reports: dict[str, ProviderReport] = {}
    cross: dict[str, Any] = {"checks": []}
    with tempfile.TemporaryDirectory(prefix="axiom-conformance-") as tmp:
        workdir = Path(tmp)
        if "upstox" in requested:
            upstox = ProviderReport("upstox", limitations=_upstox_limitations())
            _run_upstox(upstox, workdir)
            reports["upstox"] = upstox
        if "alpaca" in requested:
            alpaca = ProviderReport("alpaca", limitations=_alpaca_limitations())
            _run_alpaca(alpaca, workdir)
            reports["alpaca"] = alpaca
        _run_cross_market(cross, workdir)

    all_requests = [r for report in reports.values() for r in report.requests]
    readonly_ok, readonly_detail = assert_no_order_endpoints(all_requests)
    # The cross-market block records its own requests through the same helper and
    # is asserted separately; both must be read-only.

    statuses = [report.status for report in reports.values()]
    cross_status = "PASS"
    for check in cross["checks"]:
        if check.status == "FAIL":
            cross_status = "FAIL"
        elif check.status == "UNVERIFIED" and cross_status == "PASS":
            cross_status = "UNVERIFIED"
    if not readonly_ok:
        cross_status = "FAIL"

    if "FAIL" in statuses or cross_status == "FAIL":
        overall = "FAIL"
    elif "UNVERIFIED" in statuses or cross_status == "UNVERIFIED":
        overall = "UNVERIFIED"
    else:
        overall = "PASS"

    payload: dict[str, Any] = {
        "schema": CONFORMANCE_SCHEMA,
        "suite_version": SUITE_VERSION,
        "data_source_version": DATA_SOURCE_VERSION,
        "generated_at": (now or datetime.now(UTC)).isoformat(),
        "status": overall,
        "network_calls": 0,
        "broker_order_api_calls": 0,
        "live_trading": False,
        "mode": "PAPER",
        "providers": {name: report.as_dict() for name, report in reports.items()},
        "cross_market": {
            "status": cross_status,
            "checks": [check.as_dict() for check in cross["checks"]],
        },
        "read_only_requests": {"ok": readonly_ok, "detail": readonly_detail},
        "capability_summary": {
            name: {cap: bucket["status"] for cap, bucket in report.capabilities().items()}
            for name, report in reports.items()
        },
        "limitations": [
            "Offline deterministic doubles: no Upstox or Alpaca endpoint was contacted, so "
            "provider connectivity, entitlement and production data quality remain UNVERIFIED.",
            "Fixture prices and volumes are synthetic; no profitability, slippage or liquidity "
            "claim is made or implied.",
            "Live execution stayed disabled and no broker order endpoint exists on these paths.",
            "Real credentials, real accounts and the DPAPI credential store were not touched.",
            *(limitation for report in reports.values() for limitation in report.limitations),
        ],
        "unverified": [
            {
                "capability": "live_provider_connectivity",
                "reason": "no network call is made by this suite; run "
                "`python -m app provider-check --live` with owner credentials to verify.",
            }
        ],
    }
    payload = redact_payload(payload)
    payload["redaction"] = {
        "fields": list(SENSITIVE_FIELDS),
        "secrets_found": contains_secret(
            payload, (SYNTHETIC_UPSTOX_TOKEN, SYNTHETIC_ALPACA_KEY, SYNTHETIC_ALPACA_SECRET)
        ),
    }
    return payload


def render_text(report: dict[str, Any]) -> str:
    """Operator-friendly rendering of :func:`run_conformance` (no secrets)."""
    lines = [
        f"AXIOM provider conformance  schema={report['schema']}  suite={report['suite_version']}",
        f"data source: {report['data_source_version']}   status: {report['status']}",
        f"network calls: {report['network_calls']}   broker order calls: {report['broker_order_api_calls']}"
        f"   live trading: {report['live_trading']}",
        "",
    ]
    for name, provider in report["providers"].items():
        lines.append(f"[{name}] {provider['status']}")
        for check in provider["checks"]:
            lines.append(f"  {check['status']:<9} {check['id']}: {check['detail']}")
        lines.append("")
    lines.append(f"[cross-market] {report['cross_market']['status']}")
    for check in report["cross_market"]["checks"]:
        lines.append(f"  {check['status']:<9} {check['id']}: {check['detail']}")
    lines.append("")
    lines.append("Limitations:")
    for limitation in report["limitations"]:
        lines.append(f"  - {limitation}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Opt-in live probe (never called by the suite, the tests or a normal launch)
# --------------------------------------------------------------------------- #
def live_probe(
    *,
    providers: Iterable[str] = ("upstox", "alpaca"),
    settings: Any = None,
    symbol: str | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Perform the documented read-only provider GETs and report the verdicts.

    This is the ONLY function in this module that can open a socket, and it is
    reachable only through ``python -m app provider-check --live``. It:

    * performs ``GET`` on the documented market-data endpoints only,
    * never touches an order/position/account endpoint,
    * never writes, stores or prints a credential (no config file is modified),
    * makes exactly one attempt per provider (no retry loop),
    * returns ``UNVERIFIED`` when credentials are absent instead of guessing.

    Callers own the credential source. Nothing here is run by the test suite.
    """
    import asyncio

    requested = [str(p).strip().lower() for p in providers]
    out: dict[str, Any] = {
        "schema": CONFORMANCE_SCHEMA,
        "suite_version": SUITE_VERSION,
        "data_source_version": DATA_SOURCE_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "probe": "live_read_only",
        "network_calls": 0,
        "broker_order_api_calls": 0,
        "live_trading": False,
        "providers": {},
        "limitations": [
            "A live probe proves credential/endpoint reachability at this moment only; "
            "it is not a data-quality, latency or profitability claim.",
            "The probe prints verdicts only: no credential value, token, account id or "
            "raw provider payload is stored or displayed.",
        ],
    }

    if settings is None:
        from app.config.settings import Settings

        settings = Settings.from_env()

    if "upstox" in requested:
        token = str(getattr(settings, "market_data_access_token", "") or "").strip()
        if not token:
            out["providers"]["upstox"] = {
                "status": "UNVERIFIED",
                "detail": "MARKET_DATA_ACCESS_TOKEN is not set; nothing was contacted.",
            }
        else:
            from app.data.upstox_auth import verify_feed_credential
            from app.data.upstox_stream import AUTHORIZE_URL

            try:
                verdict = asyncio.run(
                    verify_feed_credential(token, authorize_url=AUTHORIZE_URL, timeout=timeout)
                )
                out["network_calls"] += 1
                view = verdict.public_view()
                out["providers"]["upstox"] = {
                    "status": "PASS" if verdict.verdict == "feed_ok" else "FAIL",
                    "verdict": verdict.verdict,
                    "http_status": view.get("http_status"),
                    "authenticated": view.get("authenticated"),
                    "detail": _bounded(str(view.get("guidance") or "")),
                }
            except Exception as exc:  # network/transport failure: reported, not raised
                out["network_calls"] += 1
                out["providers"]["upstox"] = {
                    "status": "FAIL",
                    "detail": f"{type(exc).__name__}: {_bounded(str(exc), 120)}",
                }

    if "alpaca" in requested:
        key = str(getattr(settings, "alpaca_api_key", "") or "").strip()
        secret = str(getattr(settings, "alpaca_api_secret", "") or "").strip()
        feed = str(getattr(settings, "alpaca_data_feed", "iex") or "iex")
        target = (symbol or (settings.alpaca_symbol_list() or ["AAPL"])[0]).strip().upper()
        if not key or not secret:
            out["providers"]["alpaca"] = {
                "status": "UNVERIFIED",
                "detail": "ALPACA_API_KEY / ALPACA_API_SECRET are not set; nothing was contacted.",
            }
        else:
            from app.data.alpaca import AlpacaQuoteReader

            reader = None
            try:
                reader = AlpacaQuoteReader(key, secret, feed=feed, timeout=timeout)
                quote = reader.latest_quote(target)
                out["network_calls"] += 1
                out["providers"]["alpaca"] = {
                    "status": "PASS",
                    "verdict": "quote_ok",
                    "symbol": target,
                    "feed": quote.get("feed"),
                    "feed_entitlement_label": quote.get("feed_entitlement_label"),
                    "feed_is_consolidated": quote.get("feed_is_consolidated"),
                    "is_delayed": quote.get("is_delayed"),
                    "source_timestamp": quote.get("source_timestamp"),
                    "detail": "read-only snapshot GET returned a usable quote",
                }
            except Exception as exc:
                out["network_calls"] += 1
                out["providers"]["alpaca"] = {
                    "status": "FAIL",
                    "detail": f"{type(exc).__name__}: {_bounded(str(exc), 120)}",
                }
            finally:
                if reader is not None:
                    reader.close()

    statuses = [p.get("status") for p in out["providers"].values()]
    out["status"] = (
        "FAIL" if "FAIL" in statuses else ("UNVERIFIED" if "UNVERIFIED" in statuses else "PASS")
    )
    out = redact_payload(out)
    out["redaction"] = {
        "fields": list(SENSITIVE_FIELDS),
        "secrets_found": contains_secret(
            out, (SYNTHETIC_UPSTOX_TOKEN, SYNTHETIC_ALPACA_KEY, SYNTHETIC_ALPACA_SECRET)
        ),
    }
    return out
