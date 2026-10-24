"""Chart series for the trading terminal: aggregation, overlays, provenance.

Design rules that this module deliberately enforces:

* An aggregated bar is emitted **only** when every source bar it covers is
  present and contiguous. A bucket built from partial data would misreport its
  high, low and volume, which is worse than showing a gap.
* Daily bars are never synthesised from intraday bars. A session boundary is a
  calendar property, not a whole number of UTC seconds, so ``1D`` is only ever
  passed through unchanged.
* Nothing here invents a bar, a price or a fill. If the requested symbol has no
  data, the payload says so and returns an empty series.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.data.models import Candle, TIMEFRAMES, parse_timestamp
from app.indicators.moving_averages import ema, sma
from app.indicators.oscillators import rsi

# Overlay definitions the terminal can draw. `key` is what the UI toggles and
# what appears in the payload; order is drawing order (cheap lines last).
OVERLAYS: dict[str, dict] = {
    "sma20": {"label": "SMA 20", "kind": "sma", "period": 20, "color": "#4aa3ff"},
    "sma50": {"label": "SMA 50", "kind": "sma", "period": 50, "color": "#f0b429"},
    "ema200": {"label": "EMA 200", "kind": "ema", "period": 200, "color": "#b47cff"},
}

OSCILLATORS: dict[str, dict] = {
    "rsi14": {"label": "RSI 14", "kind": "rsi", "period": 14, "min": 0, "max": 100,
              "guides": [30, 50, 70]},
}


class ResampleError(ValueError):
    """Requested aggregation is not exact or not allowed."""


def _seconds(timeframe: str) -> int:
    try:
        return TIMEFRAMES[timeframe]
    except KeyError as exc:
        raise ValueError(f"Unsupported timeframe {timeframe!r}") from exc


def can_aggregate(source: str, target: str) -> bool:
    """True when `target` is an exact, session-safe multiple of `source`."""
    if source == target:
        return True
    if source == "1D" or target == "1D":
        # A trading session is not a fixed UTC window: never invent a day bar.
        return False
    source_seconds, target_seconds = _seconds(source), _seconds(target)
    return target_seconds > source_seconds and target_seconds % source_seconds == 0


def _bucket_start(timestamp: datetime, seconds: int) -> datetime:
    epoch = int(timestamp.timestamp())
    return datetime.fromtimestamp(epoch - epoch % seconds, tz=timezone.utc)


def resample(candles: list[Candle], source: str, target: str) -> list[Candle]:
    """Aggregate closed candles upward, emitting only complete buckets.

    Completeness means the bucket starts on its boundary and contains exactly
    the expected number of consecutive source bars. Anything else is dropped,
    so a gap in the feed appears as a gap on the chart rather than as a bar
    whose volume silently excludes the missing minutes.
    """
    if source == target:
        return sorted(candles, key=lambda c: c.timestamp)
    if not can_aggregate(source, target):
        raise ResampleError(
            f"{target} cannot be derived from {source}: only exact intraday "
            "multiples are aggregated (daily bars are never synthesised)"
        )
    source_seconds, target_seconds = _seconds(source), _seconds(target)
    expected = target_seconds // source_seconds
    ordered = sorted(candles, key=lambda c: c.timestamp)
    buckets: dict[datetime, list[Candle]] = {}
    for candle in ordered:
        buckets.setdefault(_bucket_start(candle.timestamp, target_seconds), []).append(candle)
    output: list[Candle] = []
    for start in sorted(buckets):
        group = buckets[start]
        if len(group) != expected:
            continue
        contiguous = all(
            group[index].timestamp - group[index - 1].timestamp
            == timedelta(seconds=source_seconds)
            for index in range(1, expected)
        )
        if not contiguous or group[0].timestamp != start:
            continue
        output.append(
            Candle(
                start,
                group[0].open,
                max(c.high for c in group),
                min(c.low for c in group),
                group[-1].close,
                sum(c.volume for c in group),
                group[0].symbol,
            )
        )
    return output


def overlay_series(candles: list[Candle], keys: list[str]) -> list[dict]:
    """Indicator lines aligned 1:1 with `candles` (None during warm-up)."""
    closes = [c.close for c in candles]
    series: list[dict] = []
    for key in keys:
        spec = OVERLAYS.get(key)
        if spec is None:
            continue
        compute = sma if spec["kind"] == "sma" else ema
        values = compute(closes, spec["period"]) if closes else []
        series.append(
            {
                "key": key,
                "label": spec["label"],
                "color": spec["color"],
                "period": spec["period"],
                "values": [None if v is None else round(v, 6) for v in values],
            }
        )
    return series


def oscillator_series(candles: list[Candle], keys: list[str]) -> list[dict]:
    closes = [c.close for c in candles]
    series: list[dict] = []
    for key in keys:
        spec = OSCILLATORS.get(key)
        if spec is None:
            continue
        values = rsi(closes, spec["period"]) if closes else []
        series.append(
            {
                "key": key,
                "label": spec["label"],
                "min": spec["min"],
                "max": spec["max"],
                "guides": list(spec.get("guides", [])),
                "values": [None if v is None else round(v, 4) for v in values],
            }
        )
    return series


def bar_payload(candles: list[Candle]) -> dict:
    """Columnar OHLCV — compact over the wire and cheap to index in JS."""
    return {
        "time": [c.timestamp.isoformat() for c in candles],
        "open": [c.open for c in candles],
        "high": [c.high for c in candles],
        "low": [c.low for c in candles],
        "close": [c.close for c in candles],
        "volume": [c.volume for c in candles],
    }


def bucket_index(candles: list[Candle], timestamp: str | datetime, timeframe: str) -> int | None:
    """Index of the bar whose interval contains `timestamp`, or None.

    Used to bin trade markers onto the drawn bars; a fill that happened inside
    a 5-minute candle belongs to that candle.
    """
    if not candles:
        return None
    moment = parse_timestamp(timestamp)
    span = timedelta(seconds=_seconds(timeframe))
    for index, candle in enumerate(candles):
        if candle.timestamp <= moment < candle.timestamp + span:
            return index
    return None


def series_bounds(candles: list[Candle]) -> dict | None:
    if not candles:
        return None
    return {
        "from": candles[0].timestamp.isoformat(),
        "to": candles[-1].timestamp.isoformat(),
        "count": len(candles),
    }


@dataclass(frozen=True)
class ChartRequest:
    symbol: str
    timeframe: str
    limit: int = 600

    def validated(self) -> "ChartRequest":
        from app.data.models import validate_symbol

        validate_symbol(self.symbol)
        if self.timeframe not in TIMEFRAMES:
            raise ValueError(f"Unsupported timeframe {self.timeframe!r}")
        if not 30 <= self.limit <= 2000:
            raise ValueError("limit must be between 30 and 2000 bars")
        return self
