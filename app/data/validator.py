"""Fail closed on corrupt data; report gaps without inventing candles."""

from dataclasses import dataclass, field
from math import isfinite
from app.data.models import Candle, TIMEFRAMES, validate_symbol


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def require_valid(self):
        if self.errors:
            raise ValueError("Invalid market data: " + "; ".join(self.errors[:10]))


def validate(
    candles: list[Candle], timeframe: str, *, strict_gaps: bool = False, calendar=None
) -> ValidationReport:
    report = ValidationReport()
    if timeframe not in TIMEFRAMES:
        report.errors.append("Unsupported timeframe")
        return report
    if not candles:
        report.errors.append("Empty dataset")
        return report
    previous = {}
    seen = set()
    for i, bar in enumerate(candles):
        try:
            validate_symbol(bar.symbol)
            if bar.timestamp.tzinfo is None or bar.timestamp.utcoffset() is None:
                raise ValueError("Missing timestamp timezone")
            values = [bar.open, bar.high, bar.low, bar.close, bar.volume]
            if not all(isfinite(v) for v in values):
                raise ValueError("Missing or non-finite OHLCV")
            if min(values[:4]) <= 0:
                raise ValueError("Impossible price: must be positive")
            if bar.volume < 0:
                raise ValueError("Invalid volume")
            if (
                bar.low > min(bar.open, bar.close)
                or bar.high < max(bar.open, bar.close)
                or bar.low > bar.high
            ):
                raise ValueError("Invalid OHLC relationships")
            key = (bar.symbol, bar.timestamp)
            if key in seen:
                raise ValueError("Duplicate candle")
            seen.add(key)
            prev = previous.get(bar.symbol)
            if prev is not None:
                delta = (bar.timestamp - prev).total_seconds()
                if delta <= 0:
                    raise ValueError("Unsorted timestamps")
                if delta < TIMEFRAMES[timeframe]:
                    raise ValueError("Overlapping candles / wrong timeframe")
                if delta > TIMEFRAMES[timeframe]:
                    from datetime import timedelta

                    step = timedelta(seconds=TIMEFRAMES[timeframe])
                    # Calendar-aware: overnight/weekend closure isn't a missing candle.
                    cursor, missing = prev + step, False
                    while cursor < bar.timestamp:
                        if calendar is None or calendar.is_open(cursor):
                            missing = True
                            break
                        cursor += step
                    if missing:
                        target = report.errors if strict_gaps else report.warnings
                        target.append(f"Missing timestamps/gap before row {i} ({bar.symbol})")
            previous[bar.symbol] = bar.timestamp
        except (ValueError, TypeError, AttributeError) as exc:
            report.errors.append(f"row {i}: {exc}")
    return report


def clean_rows(rows: list[dict]) -> tuple[list[Candle], list[str]]:
    """Explicit cleaning: normalize/sort and drop EXACT duplicates, never repair prices."""
    unique = {}
    notes = []
    for i, row in enumerate(rows):
        try:
            bar = Candle.from_dict(row)
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"Invalid CSV row {i}: missing/invalid required value") from exc
        key = (bar.symbol, bar.timestamp)
        if key in unique:
            if unique[key] != bar:
                raise ValueError(f"Conflicting duplicate at {bar.timestamp}")
            notes.append(f"Dropped exact duplicate row {i}")
        unique[key] = bar
    ordered = sorted(unique.values(), key=lambda b: (b.timestamp, b.symbol))
    if list(unique.values()) != ordered:
        notes.append("Sorted timestamps")
    return ordered, notes
