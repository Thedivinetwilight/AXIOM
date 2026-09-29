"""Forecast pipeline: the whole Kronos data path in one place.

    MarketDataProvider → validation → closed-candle gate → cache check
        → Kronos inference → forecast validation → ForecastStore

The pipeline never raises into its caller: every failure is returned as an
explicit ``KronosState`` plus a message, so a broken data source or a dead model
degrades the UI instead of freezing it. Nothing here touches Arena.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from axiom.forecast.cache import forecast_cache_key, input_hash
from axiom.forecast.store import ForecastStore
from axiom.forecast.validation import build_forecast_candles, validate_forecast
from axiom.kronos.backends import KronosInference, KronosRuntimeUnavailable
from axiom.kronos.runtime import KronosRuntime
from axiom.kronos.samples import MIN_PATHS_FOR_UNCERTAINTY, summarize_paths
from axiom.market.calendar import ContinuousCalendar, TradingCalendar
from axiom.market.gate import ClosedCandleGate
from axiom.market.provider import (
    MarketDataBar,
    MarketDataError,
    MarketDataProvider,
)
from axiom.market.validation import validate_market_data
from axiom.models import (
    DataProvenance,
    DataQualityState,
    ForecastSnapshot,
    KronosConfig,
    KronosState,
    MarketDataQuality,
    utcnow,
)

InferFn = Callable[[Sequence[MarketDataBar], Sequence[datetime]], KronosInference]

# How many trailing ACTUAL closes a snapshot keeps for the MARKETS overlay.
CONTEXT_TAIL = 120


@dataclass(frozen=True)
class ForecastOutcome:
    state: KronosState
    snapshot: ForecastSnapshot | None
    quality: MarketDataQuality
    message: str
    cache_hit: bool = False
    last_closed_timestamp: datetime | None = None
    latency_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return self.snapshot is not None

    def to_dict(self) -> dict:
        return {
            "state": self.state.value,
            "message": self.message,
            "cache_hit": self.cache_hit,
            "latency_ms": round(self.latency_ms, 3),
            "last_closed_timestamp": (
                self.last_closed_timestamp.isoformat()
                if self.last_closed_timestamp
                else None
            ),
            "data_quality": self.quality.to_dict(),
            "snapshot": self.snapshot.to_dict() if self.snapshot else None,
        }


class ForecastPipeline:
    """Turn recorded candles into a validated, provenance-tagged forecast."""

    def __init__(
        self,
        provider: MarketDataProvider,
        config: KronosConfig,
        *,
        calendar: TradingCalendar | None = None,
        store: ForecastStore | None = None,
        runtime: KronosRuntime | None = None,
        infer: InferFn | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        if infer is None and runtime is None:
            raise ValueError("ForecastPipeline needs a Kronos runtime or an infer callable")
        self.provider = provider
        self.config = config.validate()
        self.calendar = calendar or ContinuousCalendar()
        self.store = store if store is not None else ForecastStore()
        self.runtime = runtime
        self.infer: InferFn = infer or runtime.forecast_paths  # type: ignore[union-attr]
        self.clock = clock

    # -- helpers -------------------------------------------------------------
    def _provenance(self, result, window: Sequence[MarketDataBar], digest: str) -> DataProvenance:
        return DataProvenance(
            source=result.source,
            provider=result.provider,
            instrument=result.instrument,
            timeframe=result.timeframe,
            bars=len(window),
            first_timestamp=window[0].timestamp if window else None,
            last_timestamp=window[-1].timestamp if window else None,
            retrieved_at=result.retrieved_at,
            input_hash=digest,
            notes=tuple(result.notes),
        )

    def _reject(
        self,
        state: KronosState,
        message: str,
        quality: MarketDataQuality,
        last_closed: datetime | None = None,
    ) -> ForecastOutcome:
        return ForecastOutcome(
            state=state,
            snapshot=None,
            quality=quality,
            message=message,
            last_closed_timestamp=last_closed,
        )

    # -- main ----------------------------------------------------------------
    def run(
        self,
        instrument: str,
        timeframe: str,
        *,
        now: datetime | None = None,
        force: bool = False,
    ) -> ForecastOutcome:
        moment = now or self.clock()

        # 1) real market data (or an honest failure) -------------------------
        try:
            result = self.provider.fetch(instrument, timeframe)
        except MarketDataError as exc:
            return self._reject(
                KronosState.DATA_ERROR,
                f"market data unavailable: {exc}",
                MarketDataQuality.unavailable(str(exc)),
            )
        except Exception as exc:  # provider bug / network surprise
            return self._reject(
                KronosState.DATA_ERROR,
                f"market data provider failed: {type(exc).__name__}: {exc}",
                MarketDataQuality.unavailable(f"{type(exc).__name__}: {exc}"),
            )

        # 2) closed-candle gate ---------------------------------------------
        gate = ClosedCandleGate(timeframe, self.calendar)
        window = gate.apply(list(result.bars), moment)
        if not window.closed:
            return self._reject(
                KronosState.INSUFFICIENT_HISTORY,
                "no CLOSED candles available yet",
                MarketDataQuality.insufficient(0, self.config.min_history),
            )

        # 3) validate the window (never repair) ------------------------------
        bars = list(window.closed[-self.config.lookback:])
        quality = validate_market_data(
            bars,
            timeframe,
            min_history=self.config.min_history,
            calendar=self.calendar,
            expected_instrument=instrument,
        )
        if quality.state is DataQualityState.INSUFFICIENT_HISTORY:
            return self._reject(
                KronosState.INSUFFICIENT_HISTORY,
                "; ".join(quality.errors[:3]) or "insufficient history",
                quality,
                window.last_closed_timestamp,
            )
        if quality.state is DataQualityState.DATA_ERROR:
            return self._reject(
                KronosState.DATA_ERROR,
                "market data rejected: " + "; ".join(quality.errors[:5]),
                quality,
                window.last_closed_timestamp,
            )

        digest = input_hash(bars)
        last_closed = window.last_closed_timestamp
        info = self.runtime.info if self.runtime is not None else None
        model_revision = info.model_revision if info else self.config.model_revision
        cache_key = forecast_cache_key(
            instrument=instrument,
            timeframe=timeframe,
            last_closed_timestamp=last_closed,
            input_hash_value=digest,
            model_revision=model_revision,
            config_hash=self.config.config_hash(),
        )

        # 4) cache: only a new closed candle (or changed input) re-runs ------
        cached = self.store.latest(instrument, timeframe)
        if not force and cached is not None and self.store.has_cache_key(
            instrument, timeframe, cache_key
        ):
            self.store.take_cache_hit()
            return ForecastOutcome(
                state=KronosState.READY,
                snapshot=cached,
                quality=quality,
                message="cached forecast reused (input window unchanged)",
                cache_hit=True,
                last_closed_timestamp=last_closed,
                latency_ms=cached.latency_ms,
            )
        self.store.record_miss()

        # 5) inference on CLOSED bars only ----------------------------------
        try:
            future_timestamps = self.calendar.project_future_timestamps(
                last_closed, timeframe, self.config.pred_len  # type: ignore[arg-type]
            )
        except MarketDataError as exc:
            return self._reject(
                KronosState.DATA_ERROR,
                f"cannot project future timestamps: {exc}",
                quality,
                last_closed,
            )

        try:
            inference = self.infer(bars, future_timestamps)
        except KronosRuntimeUnavailable as exc:
            return self._reject(KronosState.ERROR, str(exc), quality, last_closed)
        except MarketDataError as exc:
            return self._reject(KronosState.DATA_ERROR, str(exc), quality, last_closed)
        except Exception as exc:
            return self._reject(
                KronosState.ERROR,
                f"Kronos inference failed: {type(exc).__name__}: {exc}",
                quality,
                last_closed,
            )

        # 6) validate the forecast ------------------------------------------
        check = validate_forecast(inference, future_timestamps, self.config)
        if not check.ok:
            return self._reject(
                KronosState.ERROR,
                "forecast rejected: " + "; ".join(check.errors[:5]),
                quality,
                last_closed,
            )

        candles = build_forecast_candles(inference.paths, future_timestamps)
        uncertainty = summarize_paths(inference.close_paths())
        notes = list(quality.warnings) + list(check.warnings) + list(inference.notes)
        if uncertainty is None and self.config.sample_paths > 1:
            notes.append(
                "sample dispersion unavailable: at least "
                f"{MIN_PATHS_FOR_UNCERTAINTY} sampled paths are required"
            )
        snapshot = ForecastSnapshot(
            instrument=instrument,
            timeframe=timeframe,
            generated_at=moment,
            candles=candles,
            config=self.config,
            provenance=self._provenance(result, bars, digest),
            data_quality=quality,
            model_id=(info.model_id if info else self.config.model_id),
            model_revision=model_revision,
            tokenizer_id=(info.tokenizer_id if info else self.config.tokenizer_id),
            tokenizer_revision=(
                info.tokenizer_revision if info else self.config.tokenizer_revision
            ),
            device=(info.device if info else ""),
            backend=inference.backend,
            last_closed_timestamp=last_closed,
            last_actual_close=bars[-1].close,
            uncertainty=uncertainty,
            latency_ms=inference.latency_ms,
            input_hash=digest,
            cache_key=cache_key,
            context_closes=tuple(
                (bar.timestamp, bar.close) for bar in bars[-CONTEXT_TAIL:]
            ),
            note=(
                "MODEL FORECAST — not observed market data"
                + (f" | {'; '.join(notes[:2])}" if notes else "")
            ),
        )
        self.store.put(snapshot)
        return ForecastOutcome(
            state=KronosState.READY,
            snapshot=snapshot,
            quality=quality,
            message=f"forecast generated for {instrument} {timeframe}",
            last_closed_timestamp=last_closed,
            latency_ms=inference.latency_ms,
        )
