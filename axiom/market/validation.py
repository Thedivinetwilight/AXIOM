"""Market data validation for the Kronos input window.

Fail closed, report loudly, never repair. A bad window is rejected with the
reason; the pipeline then surfaces ``DATA_ERROR`` instead of feeding garbage to
the model or quietly patching values.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from math import isfinite

from axiom.market.calendar import TradingCalendar
from axiom.market.provider import MarketDataBar, MarketDataError
from axiom.models import (
    MARKET_ORIGIN,
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
    try:
        items = list(bars)
    except (TypeError, ValueError) as exc:
        return MarketDataQuality(
            state=DataQualityState.DATA_ERROR,
            errors=(f"bars are not iterable: {type(exc).__name__}",), bars=0)
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(timeframe, str) or timeframe not in TIMEFRAME_SECONDS:
        return MarketDataQuality(
            state=DataQualityState.DATA_ERROR,
            errors=(f"unsupported timeframe: {timeframe}",),
            bars=0,
        )
    if isinstance(min_history, bool) or not isinstance(min_history, int) or min_history < 1:
        return MarketDataQuality(
            state=DataQualityState.DATA_ERROR,
            errors=("min_history must be a positive integer",), bars=len(items))
    if expected_instrument is not None and (
            not isinstance(expected_instrument, str) or not expected_instrument.strip()):
        return MarketDataQuality(
            state=DataQualityState.DATA_ERROR,
            errors=("expected_instrument must be a non-empty string",), bars=len(items))
    if not items:
        return MarketDataQuality(
            state=DataQualityState.DATA_ERROR, errors=("empty window",), bars=0
        )

    step = TIMEFRAME_SECONDS[timeframe]
    seen: set[object] = set()
    previous: MarketDataBar | None = None

    for index, bar in enumerate(items):
        if not isinstance(bar, MarketDataBar):
            errors.append(f"row {index}: value is not a MarketDataBar")
            continue
        timestamp = getattr(bar, "timestamp", None)
        if not isinstance(timestamp, datetime):
            errors.append(f"row {index}: timestamp is not a datetime")
            continue
        where = f"row {index} ({timestamp.isoformat()})"
        values = (bar.open, bar.high, bar.low, bar.close, bar.volume)
        if any(isinstance(value, bool) for value in values):
            errors.append(f"{where}: OHLCV values must be numeric, not boolean")
            continue
        try:
            finite = all(isfinite(value) for value in values)
        except (TypeError, ValueError, OverflowError):
            finite = False
        if not finite:
            errors.append(f"{where}: non-finite or non-numeric OHLCV {values!r}")
            continue
        try:
            offset = timestamp.utcoffset()
        except (TypeError, ValueError, OverflowError):
            offset = None
        if timestamp.tzinfo is None or offset is None:
            # `continue` like the non-finite guard above: this function is
            # documented as returning a verdict and never raising, but the
            # spacing arithmetic below subtracts timestamps, and mixing a naive
            # stamp with an aware one raises TypeError.
            errors.append(f"{where}: timestamp is not timezone-aware")
            continue
        if offset != timedelta(0):
            errors.append(f"{where}: timestamp is not normalized to UTC")
            continue
        if (not isinstance(bar.instrument, str) or not bar.instrument.strip()
                or bar.instrument.strip() != bar.instrument):
            errors.append(
                f"{where}: instrument identity must be non-empty and trimmed")
        if getattr(bar, "origin", None) != "MARKET_OBSERVED":
            errors.append(f"{where}: origin is not MARKET_OBSERVED")
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

        key = (bar.instrument if isinstance(bar.instrument, str) else None, timestamp)
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


def validate_inference_inputs(
    bars: Sequence[MarketDataBar],
    future_timestamps: Sequence[datetime],
    config,
    *,
    expected_instrument: str | None = None,
) -> None:
    """Validate the model boundary even when callers bypass the pipeline.

    The pipeline additionally checks timeframe spacing, trading sessions,
    gaps, closed-candle state, and the requested instrument. This boundary
    check protects direct runtime/backend use and the worker IPC child from
    malformed order, timezone, OHLCV, horizon, or configuration inputs.
    """
    try:
        config.validate()
    except (AttributeError, TypeError, ValueError) as exc:
        raise MarketDataError(f"invalid Kronos inference configuration: {exc}") from exc

    if not isinstance(bars, Sequence) or isinstance(bars, (str, bytes)):
        raise MarketDataError("bars must be an ordered sequence")
    if not bars:
        raise MarketDataError("no closed bars supplied for inference")
    if len(bars) < config.min_history:
        raise MarketDataError(
            f"bar count ({len(bars)}) < min_history ({config.min_history})")
    if len(bars) > config.lookback:
        raise MarketDataError(
            f"bar count ({len(bars)}) > configured lookback ({config.lookback})")

    instrument = None
    previous = None
    for index, bar in enumerate(bars):
        if not isinstance(bar, MarketDataBar):
            raise MarketDataError(f"bar {index} is not a MarketDataBar")
        timestamp = getattr(bar, "timestamp", None)
        if not isinstance(timestamp, datetime):
            raise MarketDataError(f"bar {index} has no datetime timestamp")
        try:
            offset = timestamp.utcoffset()
        except (TypeError, ValueError, OverflowError) as exc:
            raise MarketDataError(f"bar {index} timestamp is malformed") from exc
        if timestamp.tzinfo is None or offset is None:
            raise MarketDataError(f"bar {index} timestamp must be timezone-aware UTC")
        if offset != timedelta(0):
            raise MarketDataError(f"bar {index} timestamp must be UTC (offset +00:00)")
        values = (bar.open, bar.high, bar.low, bar.close, bar.volume)
        if any(isinstance(value, bool) for value in values):
            raise MarketDataError(f"bar {index} OHLCV values must be numeric, not boolean")
        try:
            finite = all(isfinite(value) for value in values)
        except (TypeError, ValueError, OverflowError):
            finite = False
        if not finite:
            raise MarketDataError(f"bar {index} has non-finite or non-numeric OHLCV values")
        if min(values[:4]) <= 0 or bar.volume < 0:
            raise MarketDataError(f"bar {index} has non-positive prices or negative volume")
        if (bar.high < max(bar.open, bar.close)
                or bar.low > min(bar.open, bar.close) or bar.high < bar.low):
            raise MarketDataError(f"bar {index} has invalid OHLC relationships")
        if getattr(bar, "origin", None) != MARKET_ORIGIN:
            raise MarketDataError(f"bar {index} origin must be {MARKET_ORIGIN}")
        symbol = bar.instrument
        if (not isinstance(symbol, str) or not symbol.strip()
                or symbol.strip() != symbol):
            raise MarketDataError(f"bar {index} has no valid instrument identity")
        if instrument is None:
            instrument = symbol
        elif symbol != instrument:
            raise MarketDataError("bars contain more than one instrument identity")
        if expected_instrument is not None and symbol != expected_instrument:
            raise MarketDataError(
                f"bar {index} instrument {symbol!r} != requested {expected_instrument!r}")
        if previous is not None and timestamp <= previous:
            raise MarketDataError(
                f"bar timestamps must be unique and strictly increasing (row {index})")
        previous = timestamp

    if not isinstance(future_timestamps, Sequence) or isinstance(
            future_timestamps, (str, bytes)):
        raise MarketDataError("future timestamps must be an ordered sequence")
    if len(future_timestamps) != config.pred_len:
        raise MarketDataError(
            f"future timestamps ({len(future_timestamps)}) != pred_len "
            f"({config.pred_len})")

    previous_future = None
    for index, timestamp in enumerate(future_timestamps):
        if not isinstance(timestamp, datetime):
            raise MarketDataError(f"future timestamp {index} is not a datetime")
        try:
            offset = timestamp.utcoffset()
        except (TypeError, ValueError, OverflowError) as exc:
            raise MarketDataError(f"future timestamp {index} is malformed") from exc
        if timestamp.tzinfo is None or offset is None:
            raise MarketDataError(
                f"future timestamp {index} must be timezone-aware UTC")
        if offset != timedelta(0):
            raise MarketDataError(
                f"future timestamp {index} must be UTC (offset +00:00)")
        if timestamp <= bars[-1].timestamp:
            raise MarketDataError("future timestamps must be strictly after the last input bar")
        if previous_future is not None and timestamp <= previous_future:
            raise MarketDataError("future timestamps must be unique and strictly increasing")
        previous_future = timestamp


def require_valid(quality: MarketDataQuality) -> MarketDataQuality:
    """Raise with the collected reasons when the window is unusable."""
    if not quality.ok:
        raise ValueError(
            f"market data rejected ({quality.state.value}): "
            + "; ".join(quality.errors[:8])
        )
    return quality
