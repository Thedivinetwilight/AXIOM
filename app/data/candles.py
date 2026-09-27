"""Provider-agnostic live candle building; emits ONLY completed candles.

Used by live market-data adapters (V0.6.1 Upstox adapter) to turn raw ticks or
official interval bars into the same normalized `Candle` records used by
backtesting and replay. The strategy layer never sees provider-specific data.

Completion rules (identical semantics to backtest/replay):
- A tick candle is complete when its time window has passed (wall clock) or a
  later bucket is observed, whichever happens first.
- An official interval bar is complete when a newer bar supersedes it or its
  window has passed.
- Missing candles are NEVER synthesized. Gaps are counted and reported so the
  controller can pause new orders instead of treating missing data as
  continuous.
- Duplicate ticks (same second/price/quantity) and older out-of-order ticks are
  dropped with counters; a late tick inside the forming bucket may only refine
  its extremes, never restart accounting.
"""

import logging
from datetime import datetime, timezone
from math import isfinite
import time

from app.data.models import Candle, TIMEFRAMES

logger = logging.getLogger("bot.market")

# Epochs beyond 1e11 are treated as milliseconds (exchange feeds differ;
# magnitude disambiguates the documented int64 fields until validated live).
_MS_THRESHOLD = 1e11
# Reject absurd timestamps (pre-epoch or >2 days in the future): protects the
# builder from uninitialized snapshot fields and obvious clock faults.
_MAX_FUTURE_SKEW_S = 86_400 * 2


def epoch_seconds(value: int | float) -> float:
    """Normalize an exchange epoch value to seconds."""
    value = float(value)
    return value / 1000.0 if value > _MS_THRESHOLD else value


class LiveCandleBuilder:
    """Accumulate ticks/official bars per symbol; yield completed Candle only."""

    def __init__(self, timeframe: str, *, stale_after: float = 90.0):
        if timeframe not in TIMEFRAMES:
            raise ValueError("Unsupported timeframe for live candle building")
        self.timeframe = timeframe
        self.seconds = TIMEFRAMES[timeframe]
        self.stale_after = float(stale_after)
        self._forming: dict[str, dict] = {}  # tick-built forming bar per symbol
        self._official: dict[str, dict] = {}  # provider interval bar per symbol
        self._last_emitted: dict[str, datetime] = {}
        self._last_tick_ts: dict[str, float] = {}
        self._last_tick_price: dict[str, float] = {}
        self._last_tick_qty: dict[str, float] = {}
        self._last_activity: float | None = None
        self._pending: list[Candle] = []
        self.ticks = 0
        self.duplicates = 0
        self.out_of_order = 0
        self.malformed = 0
        self.invalid_timestamps = 0
        self.ignored_intervals = 0
        self.gaps = 0
        self.regressions = 0
        self.completed = 0

    # -- validation helpers -------------------------------------------------

    def _reject(self, reason: str, **extra):
        self.malformed += 1
        logger.warning("feed_invalid_field", extra={"reason": reason, **extra})

    def _check_timestamp(self, symbol: str, raw) -> float | None:
        try:
            seconds = epoch_seconds(raw)
        except (TypeError, ValueError):
            self.invalid_timestamps += 1
            logger.warning("feed_invalid_timestamp", extra={"symbol": symbol, "reason": "not_numeric"})
            return None
        if not isfinite(seconds) or seconds <= 0 or seconds > time.time() + _MAX_FUTURE_SKEW_S:
            self.invalid_timestamps += 1
            logger.warning("feed_invalid_timestamp", extra={"symbol": symbol, "reason": "out_of_range"})
            return None
        return seconds

    # -- ingestion ----------------------------------------------------------

    def ingest_tick(self, symbol: str, ts, price, quantity=0.0, *, received_at=None):
        """Fold one normalized tick; volume is the sum of observed trade sizes."""
        try:
            price, quantity = float(price), float(quantity)
        except (TypeError, ValueError):
            self._reject("tick_not_numeric", symbol=symbol)
            return
        if not isfinite(price) or price <= 0 or not isfinite(quantity) or quantity < 0:
            self._reject("tick_price_or_volume_invalid", symbol=symbol)
            return
        seconds = self._check_timestamp(symbol, ts)
        if seconds is None:
            return
        bucket = seconds - (seconds % self.seconds)
        self._last_activity = time.time() if received_at is None else received_at
        self.ticks += 1

        state = self._forming.get(symbol)
        last_ts = self._last_tick_ts.get(symbol)
        if last_ts is not None and seconds < last_ts:
            # Out of order: only extremes may be refined inside the same bucket;
            # volume is not added (retransmitted trades must not double count).
            if state is not None and bucket == state["bucket"]:
                state["high"] = max(state["high"], price)
                state["low"] = min(state["low"], price)
            self.out_of_order += 1
            logger.warning("feed_out_of_order_tick", extra={"symbol": symbol})
            return
        if (
            last_ts is not None
            and seconds == last_ts
            and price == self._last_tick_price.get(symbol)
            and quantity == self._last_tick_qty.get(symbol)
        ):
            self.duplicates += 1
            return

        if state is None or bucket > state["bucket"]:
            if state is not None:
                self._emit(symbol, state["bucket"], state["open"], state["high"],
                           state["low"], state["close"], state["volume"])
            self._forming[symbol] = {
                "bucket": bucket, "open": price, "high": price,
                "low": price, "close": price, "volume": quantity,
            }
        else:
            state["high"] = max(state["high"], price)
            state["low"] = min(state["low"], price)
            state["close"] = price
            state["volume"] += quantity
        self._last_tick_ts[symbol] = seconds
        self._last_tick_price[symbol] = price
        self._last_tick_qty[symbol] = quantity

    def ingest_interval(self, symbol: str, interval: str, open_ts, open_p, high_p,
                        low_p, close_p, volume, *, received_at=None):
        """Track one official provider bar (e.g. Upstox marketOHLC) by open time."""
        if interval != self.timeframe:
            self.ignored_intervals += 1
            return
        try:
            values = [float(v) for v in (open_p, high_p, low_p, close_p, volume)]
        except (TypeError, ValueError):
            self._reject("interval_not_numeric", symbol=symbol)
            return
        o, h, low, c, v = values
        if not all(map(isfinite, values)) or min(o, h, low, c) <= 0 or v < 0:
            self._reject("interval_ohlc_invalid", symbol=symbol)
            return
        if low > min(o, c) or h < max(o, c) or low > h:
            self._reject("interval_ohlc_relationships", symbol=symbol)
            return
        seconds = self._check_timestamp(symbol, open_ts)
        if seconds is None:
            return
        seconds -= seconds % self.seconds
        self._last_activity = time.time() if received_at is None else received_at

        prior = self._official.get(symbol)
        if prior is not None and seconds == prior["open_ts"]:
            if (o, h, low, c, v) == prior["values"]:
                self.duplicates += 1
                return
            prior["values"] = (o, h, low, c, v)  # authoritative supersede
            return
        if prior is not None:
            self._emit(symbol, prior["open_ts"], *prior["values"])
        self._official[symbol] = {"open_ts": seconds, "values": (o, h, low, c, v)}

    # -- completion ---------------------------------------------------------

    def _emit(self, symbol: str, bucket: float, o, h, low, c, v):
        timestamp = datetime.fromtimestamp(bucket, timezone.utc)
        last = self._last_emitted.get(symbol)
        if last is not None:
            delta = (timestamp - last).total_seconds()
            if delta == 0:
                self.duplicates += 1
                return
            if delta < 0:
                self.regressions += 1
                logger.warning("feed_candle_regression", extra={"symbol": symbol})
                return
            if delta > self.seconds:
                self.gaps += 1
                logger.warning("feed_candle_gap", extra={"symbol": symbol})
        self._last_emitted[symbol] = timestamp
        self.completed += 1
        self._pending.append(Candle(timestamp, o, h, low, c, v, symbol))

    def collect(self, *, now=None) -> list[Candle]:
        """Emit completed candles whose window closed; return and clear them."""
        now = time.time() if now is None else now
        for symbol, state in list(self._forming.items()):
            if now >= state["bucket"] + self.seconds:
                self._emit(symbol, state["bucket"], state["open"], state["high"],
                           state["low"], state["close"], state["volume"])
                del self._forming[symbol]
        for symbol, bar in list(self._official.items()):
            if now >= bar["open_ts"] + self.seconds:
                self._emit(symbol, bar["open_ts"], *bar["values"])
                del self._official[symbol]
        pending, self._pending = self._pending, []
        return pending

    # -- observability ------------------------------------------------------

    def stale(self, *, now=None) -> bool:
        """True once the feed has been silent longer than `stale_after`."""
        if self._last_activity is None:
            return False
        now = time.time() if now is None else now
        return now - self._last_activity > self.stale_after

    def metrics(self) -> dict:
        last = self._last_emitted and max(self._last_emitted.values())
        return {
            "ticks": self.ticks,
            "duplicates": self.duplicates,
            "out_of_order": self.out_of_order,
            "malformed": self.malformed,
            "invalid_timestamps": self.invalid_timestamps,
            "ignored_intervals": self.ignored_intervals,
            "gaps": self.gaps,
            "regressions": self.regressions,
            "completed_candles": self.completed,
            "last_candle_open_utc": last.isoformat() if last else None,
            "stale_feed": self.stale(),
            "data_gap_detected": self.gaps > 0,
        }
