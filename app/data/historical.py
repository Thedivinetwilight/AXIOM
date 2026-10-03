"""CSV import plus a working, read-only public Binance historical adapter."""

import csv
import io
import time
from datetime import datetime, timezone
from pathlib import Path
from app.data.cache import DataCache
from app.data.models import Candle, TIMEFRAMES, validate_symbol
from app.data.provider import APIProvider, MarketDataProvider
from app.data.validator import clean_rows, validate


class CSVProvider(MarketDataProvider):
    def __init__(
        self,
        path: Path,
        timeframe: str,
        cache: DataCache | None = None,
        clean: bool = False,
        strict_gaps: bool = False,
        calendar=None,
    ):
        if timeframe not in TIMEFRAMES:
            raise ValueError("Unsupported timeframe")
        self.path, self.cache, self.clean = Path(path), cache, clean
        self.supported_timeframes = frozenset({timeframe})
        self.strict_gaps, self.calendar = strict_gaps, calendar
        self.warnings: list[str] = []

    def historical(self, symbol, timeframe, start=None, end=None):
        self.check_timeframe(timeframe)
        validate_symbol(symbol)
        content = self.path.read_bytes()
        key = DataCache.key(content, {"timeframe": timeframe, "clean": self.clean})
        bars = self.cache.get(key) if self.cache else None
        self.warnings = []
        if bars is None:
            rows = list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))
            if self.clean:
                bars, self.warnings = clean_rows(rows)
            else:
                try:
                    bars = [Candle.from_dict(row) for row in rows]
                except (ValueError, KeyError, TypeError) as exc:
                    raise ValueError("Missing/invalid CSV fields or timestamp") from exc
        report = validate(bars, timeframe, strict_gaps=self.strict_gaps, calendar=self.calendar)
        report.require_valid()
        self.warnings.extend(report.warnings)
        if self.cache:
            self.cache.put(key, bars)
        result = [
            b
            for b in bars
            if b.symbol == symbol
            and (start is None or b.timestamp >= start)
            and (end is None or b.timestamp < end)
        ]
        if not result:
            raise ValueError("No data for requested symbol/range")
        return result


class BinanceHistorical(APIProvider):
    """Crypto spot public candles, no credentials and no order endpoints.

    Excludes unfinished candles. Daily/intraday UTC, 24x7. Provider access varies
    by jurisdiction. Callers must not label these prices as NSE equity data.
    """

    supported_timeframes = frozenset(TIMEFRAMES)

    def __init__(self, cache: DataCache | None = None):
        self.cache = cache

    def historical(self, symbol, timeframe, start=None, end=None):
        import httpx

        self.check_timeframe(timeframe)
        validate_symbol(symbol)
        if start is None or end is None or start >= end:
            raise ValueError("Explicit start < end required for downloads")
        if start.utcoffset() is None or end.utcoffset() is None:
            raise ValueError("Timezone-aware range required")
        key = DataCache.key(
            b"binance-public-v1",
            {"symbol": symbol, "tf": timeframe, "start": start.isoformat(), "end": end.isoformat()},
        )
        now = datetime.now(timezone.utc)
        # Cache only ranges entirely in the past; don't freeze an unfinished range.
        cacheable = end <= now
        if self.cache and cacheable:
            cached = self.cache.get(key)
            if cached is not None:
                validate(cached, timeframe).require_valid()
                return cached
        cursor, end_ms = int(start.timestamp() * 1000), int(min(end, now).timestamp() * 1000)
        bars = []
        with httpx.Client(timeout=20) as client:
            while cursor < end_ms:
                params = {
                    "symbol": symbol.upper(),
                    "interval": "1d" if timeframe == "1D" else timeframe,
                    "startTime": cursor,
                    "endTime": end_ms - 1,
                    "limit": 1000,
                }
                for attempt in range(5):
                    response = client.get("https://api.binance.com/api/v3/klines", params=params)
                    if response.status_code not in {429, 418, 500, 502, 503, 504}:
                        break
                    retry = response.headers.get("Retry-After", "")
                    time.sleep(min(120, float(retry) if retry.isdigit() else 2 ** (attempt + 1)))
                response.raise_for_status()
                rows = response.json()
                if not rows:
                    break
                for row in rows:
                    if int(row[6]) >= int(now.timestamp() * 1000):
                        continue
                    bars.append(
                        Candle(
                            datetime.fromtimestamp(row[0] / 1000, timezone.utc),
                            *(float(row[i]) for i in (1, 2, 3, 4, 5)),
                            symbol,
                        )
                    )
                next_cursor = int(rows[-1][0]) + TIMEFRAMES[timeframe] * 1000
                if next_cursor <= cursor:
                    raise ValueError("Provider pagination did not advance")
                cursor = next_cursor
                time.sleep(0.25)
        validate(bars, timeframe).require_valid()
        if self.cache and cacheable:
            self.cache.put(key, bars)
        return bars


def write_csv(path: Path, candles: list[Candle]):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(
            file, fieldnames=["timestamp", "open", "high", "low", "close", "volume", "symbol"]
        )
        writer.writeheader()
        writer.writerows(c.to_dict() for c in candles)
