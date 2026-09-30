"""Market data validation for the Kronos input window.

Fail closed, report loudly, never repair. A bad window is rejected with the
reason; the pipeline then surfaces ``DATA_ERROR`` instead of feeding garbage to
the model or quietly patching values.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta
from math import isfinite

from axiom.market.calendar import TradingCalendar
from axiom.market.provider import MarketDataBar
from axiom.models import (
    TIMEFRAME_SECONDS,
    DataQualityState,
    MarketDataQuality,
)


def validate_market_data(
    bars: Iterable[MarketDataBar],
    timeframe: str,
    *,
    min_history: int = 2,
    calendar: TradingCalendar | None = None,
    strict_gaps: bool = True,
    expected_instrument: str | None = None,
) -> MarketDataQuality:
    """Validate an OHLCV window. Returns a quality verdict; never mutates."""
    items = list(bars)
    errors: list[str] = []
    warnings: list[str] = []

    if timeframe not in TIMEFRAME_SECONDS:
        return MarketDataQuality(
            state=DataQualityState.DATA_ERROR,
            errors=(f"unsupported timeframe: {timeframe}",),
            bars=0,
        )
    if not items:
        return MarketDataQuality(
            state=DataQualityState.DATA_ERROR, errors=("empty window",), bars=0
        )

    step = TIMEFRAME_SECONDS[timeframe]
    seen: set[object] = set()
    previous: MarketDataBar | None = None

    for index, bar in enumerate(items):
        where = f"row {index} ({bar.timestamp.isoformat()})"
        values = (bar.open, bar.high, bar.low, bar.close, bar.volume)
        if not all(isfinite(value) for value in values):
            errors.append(f"{where}: non-finite OHLCV (NaN/Inf) {values!r}")
            continue
        if bar.timestamp.tzinfo is None or bar.timestamp.utcoffset() is None:
            errors.append(f"{where}: timestamp is not timezone-aware")
        if expected_instrument and bar.instrument != expected_instrument:
            errors.append(
                f"{where}: instrument {bar.instrument!r} != requested "
                f"{expected_instrument!r}"
            )
        if min(values[:4]) <= 0:
            errors.append(f"{where}: price must be > 0, got {values[:4]!r}")
        if bar.volume < 0:
            errors.append(f"{where}: volume must be >= 0, got {bar.volume!r}")
        if bar.high < max(bar.open, bar.close):
            errors.append(
                f"{where}: high {bar.high} < max(open, close) "
                f"{max(bar.open, bar.close)}"
            )
        if bar.low > min(bar.open, bar.close):
            errors.append(
                f"{where}: low {bar.low} > min(open, close) {min(bar.open, bar.close)}"
            )
        if bar.high < bar.low:
            errors.append(f"{where}: high {bar.high} < low {bar.low}")

        key = (bar.instrument, bar.timestamp)
        if key in seen:
            errors.append(f"{where}: duplicate timestamp")
        seen.add(key)

        if previous is not None:
            delta = (bar.timestamp - previous.timestamp).total_seconds()
            if delta <= 0:
                errors.append(f"{where}: timestamps are not strictly increasing")
            elif delta < step:
                errors.append(
                    f"{where}: spacing {delta:.0f}s < {timeframe} ({step}s) — "
                    "wrong timeframe or overlapping bars"
                )
            elif delta > step:
                missing = _gap_is_real(previous.timestamp, bar.timestamp, step, calendar)
                if missing:
                    message = (
                        f"gap before {where}: {int(delta // step) - 1} missing "
                        f"{timeframe} bar(s)"
                    )
                    if delta % step == 0 and delta // step > 1:
                        # Systematic whole-multiple spacing is the classic
                        # symptom of a mis-declared timeframe.
                        message += (
                            f" (spacing is {int(delta // step)}x the declared "
                            f"{timeframe} step of {step}s — wrong timeframe declared?)"
                        )
                    (errors if strict_gaps else warnings).append(message)

        if calendar is not None and not calendar.is_valid_bar_time(bar.timestamp, timeframe):
            warnings.append(
                f"{where}: timestamp outside the {calendar.name} session calendar"
            )
        previous = bar

    if len(items) < min_history:
        errors.append(
            f"insufficient history: {len(items)} bar(s), {min_history} required"
        )

    if errors:
        # "insufficient history" on its own is a distinct, recoverable verdict;
        # anything structurally wrong is a hard DATA_ERROR.
        structural = [e for e in errors if not e.startswith("insufficient history")]
        state = (
            DataQualityState.INSUFFICIENT_HISTORY
            if not structural and len(items) < min_history
            else DataQualityState.DATA_ERROR
        )
        return MarketDataQuality(
            state=state, errors=tuple(errors), warnings=tuple(warnings), bars=len(items)
        )
    return MarketDataQuality(
        state=DataQualityState.OK, warnings=tuple(warnings), bars=len(items)
    )


def _gap_is_real(
    previous_ts, next_ts, step: int, calendar: TradingCalendar | None
) -> bool:
    """A gap only counts if the skipped slots were actually trading slots."""
    if calendar is None:
        return True
    cursor = previous_ts + timedelta(seconds=step)
    while cursor < next_ts:
        if calendar.is_valid_bar_time(cursor, _timeframe_for(step)):
            return True
        cursor += timedelta(seconds=step)
    return False


def _timeframe_for(step: int) -> str:
    for name, seconds in TIMEFRAME_SECONDS.items():
        if seconds == step:
            return name
    return "1m"


def require_valid(quality: MarketDataQuality) -> MarketDataQuality:
    """Raise with the collected reasons when the window is unusable."""
    if not quality.ok:
        raise ValueError(
            f"market data rejected ({quality.state.value}): "
            + "; ".join(quality.errors[:8])
        )
    return quality
