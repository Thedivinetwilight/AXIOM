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
from axiom.log import get_logger
from axiom.market.calendar import ContinuousCalendar, TradingCalendar
from axiom.market.gate import ClosedCandleGate
from axiom.market.provider import (
    MarketDataBar,
    MarketDataError,
    MarketDataProvider,
    MarketDataResult,
)
from axiom.market.validation import validate_market_data
from axiom.models import (
    DataProvenance,
    DataQualityState,
    ForecastSnapshot,
    KronosConfig,
    KronosState,
    MarketDataQuality,
    TIMEFRAME_SECONDS,
    require_aware_utc,
    utcnow,
)

InferFn = Callable[[Sequence[MarketDataBar], Sequence[datetime]], KronosInference]

# Cheap, non-blocking accessor for (model_id, model_revision) of the weights the
# runtime actually loaded. Needed because in process mode the pipeline holds no
# runtime handle, yet the cache key must still change when the model changes.
ModelIdentityFn = Callable[[], tuple[str, str]]

# How many trailing ACTUAL closes a snapshot keeps for the MARKETS overlay.
CONTEXT_TAIL = 120

logger = get_logger("forecast.pipeline")


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
        model_identity: ModelIdentityFn | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        if infer is None and runtime is None:
            raise ValueError("ForecastPipeline needs a Kronos runtime or an infer callable")
        self.provider = provider
        self.config = config.validate()
        if runtime is not None and runtime.config != self.config:
            raise ValueError(
                "ForecastPipeline configuration differs from its Kronos runtime")
        self.calendar = calendar or ContinuousCalendar()
        self.store = store if store is not None else ForecastStore()
        self.runtime = runtime
        self.infer: InferFn = infer or runtime.forecast_paths  # type: ignore[union-attr]
        self.model_identity = model_identity
        self.clock = clock

    # -- helpers -------------------------------------------------------------
    def _resolved_model(self) -> tuple[str, str, str, str, str]:
        """Return (model_id, model_revision, tokenizer_id, tokenizer_rev, device).

        Preference order: the runtime's own info (in-process truth) → the
        injected ``model_identity`` accessor (process mode) → config defaults.
        Never invents a revision: an unknown revision stays empty, which keeps
        the cache key honest about what produced the forecast.
        """
        info = self.runtime.info if self.runtime is not None else None
        identity = ("", "")
        if self.model_identity is not None:
            try:
                reported = self.model_identity()
                if (not isinstance(reported, Sequence) or
                        isinstance(reported, (str, bytes)) or len(reported) != 2):
                    raise ValueError("model identity must be a two-item sequence")
                identity = (str(reported[0] or ""), str(reported[1] or ""))
            except Exception as exc:  # accessor must never break forecasting
                logger.warning("model identity accessor failed: %s", exc)
        return (
            info.model_id if info else (identity[0] or self.config.model_id),
            info.model_revision
            if info
            else (identity[1] or self.config.model_revision),
            info.tokenizer_id if info else self.config.tokenizer_id,
            info.tokenizer_revision if info else self.config.tokenizer_revision,
            info.device if info else "",
        )
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
        logger.warning("forecast rejected (%s): %s", state.value, message)
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
        moment = self.clock() if now is None else now
        if not isinstance(instrument, str) or not instrument.strip():
            return self._reject(
                KronosState.DATA_ERROR, "instrument must be a non-empty string",
                MarketDataQuality.unavailable("invalid instrument"),
            )
        if not isinstance(timeframe, str) or timeframe not in TIMEFRAME_SECONDS:
            return self._reject(
                KronosState.DATA_ERROR, f"unsupported timeframe: {timeframe}",
                MarketDataQuality.unavailable("unsupported timeframe"),
            )
        try:
            moment = require_aware_utc(moment, "forecast time")
        except (TypeError, ValueError, OverflowError) as exc:
            return self._reject(
                KronosState.DATA_ERROR, f"invalid forecast time: {exc}",
                MarketDataQuality.unavailable(f"invalid forecast time: {exc}"),
            )

        # 1) real market data (or an honest failure) -------------------------
        try:
            result = self.provider.fetch(instrument, timeframe)
            if not isinstance(result, MarketDataResult):
                raise MarketDataError("provider returned an invalid market-data result")
            if result.instrument != instrument:
                raise MarketDataError(
                    f"provider result instrument {result.instrument!r} != requested "
                    f"{instrument!r}"
                )
            if result.timeframe != timeframe:
                raise MarketDataError(
                    f"provider result timeframe {result.timeframe!r} != requested "
                    f"{timeframe!r}"
                )
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
        try:
            gate = ClosedCandleGate(timeframe, self.calendar)
            window = gate.apply(list(result.bars), moment)
        except Exception as exc:
            return self._reject(
                KronosState.DATA_ERROR,
                f"market data timestamps could not be gated: {type(exc).__name__}: {exc}",
                MarketDataQuality.unavailable("invalid market-data timestamps"),
            )
        if not window.closed:
            return self._reject(
                KronosState.INSUFFICIENT_HISTORY,
                "no CLOSED candles available yet",
                MarketDataQuality.insufficient(0, self.config.min_history),
            )

        # 3) validate the window (never repair) ------------------------------
        bars = list(window.closed[-self.config.lookback:])
        try:
            quality = validate_market_data(
                bars,
                timeframe,
                min_history=self.config.min_history,
                calendar=self.calendar,
                expected_instrument=instrument,
            )
        except Exception as exc:
            return self._reject(
                KronosState.DATA_ERROR,
                f"market data could not be validated: {type(exc).__name__}: {exc}",
                MarketDataQuality.unavailable("market data validation failed"),
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
        (
            model_id,
            model_revision,
            tokenizer_id,
            tokenizer_revision,
            device,
        ) = self._resolved_model()
        cache_key = forecast_cache_key(
            instrument=instrument,
            timeframe=timeframe,
            last_closed_timestamp=last_closed,
            input_hash_value=digest,
            model_revision=model_revision,
            config_hash=self.config.config_hash(),
            model_id=model_id,
        )

        # 4) cache: only a new closed candle (or changed input) re-runs ------
        cached = self.store.latest(instrument, timeframe)
        if not force and cached is not None and self.store.has_cache_key(
            instrument, timeframe, cache_key
        ):
            self.store.take_cache_hit()
            age = (moment - cached.generated_at).total_seconds()
            fresh = age <= self.config.stale_after_seconds
            return ForecastOutcome(
                state=(KronosState.READY if fresh else KronosState.STALE),
                snapshot=cached,
                quality=quality,
                message=(
                    "cached forecast reused (input window unchanged)"
                    if fresh
                    else (
                        "cached forecast reused but is "
                        f"{age:.0f}s old (> {self.config.stale_after_seconds:.0f}s) — STALE"
                    )
                ),
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
        except Exception as exc:
            return self._reject(
                KronosState.DATA_ERROR,
                f"cannot project future timestamps: {type(exc).__name__}: {exc}",
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
        try:
            check = validate_forecast(inference, future_timestamps, self.config)
        except Exception as exc:
            return self._reject(
                KronosState.ERROR,
                f"forecast validation failed: {type(exc).__name__}: {exc}",
                quality,
                last_closed,
            )
        if not check.ok:
            return self._reject(
                KronosState.ERROR,
                "forecast rejected: " + "; ".join(check.errors[:5]),
                quality,
                last_closed,
            )

        # The inference result is the final model identity evidence for a
        # process-backed runtime. If its loaded identity differs from the
        # pre-inference status used for lookup, store only under the identity
        # that actually came back with the trajectories.
        actual_model_id = inference.model_id or model_id
        actual_model_revision = inference.model_revision or model_revision
        identity_changed = (actual_model_id != model_id
                            or actual_model_revision != model_revision)
        if identity_changed:
            logger.warning(
                "model identity used for inference (%s@%s) differs from the "
                "pre-inference cache identity (%s@%s) for %s %s",
                actual_model_id or "<unset>", actual_model_revision or "<unset>",
                model_id or "<unset>", model_revision or "<unset>",
                instrument, timeframe)
            cache_key = forecast_cache_key(
                instrument=instrument,
                timeframe=timeframe,
                last_closed_timestamp=last_closed,
                input_hash_value=digest,
                model_id=actual_model_id,
                model_revision=actual_model_revision,
                config_hash=self.config.config_hash(),
            )

        # 7) assemble and store the snapshot ---------------------------------
        #    ForecastSnapshot re-validates its own invariants (provenance,
        #    origin, OHLC sanity), so a construction failure is a real — if
        #    unexpected — error. run() promises never to raise into its caller,
        #    and this is the last place that could break that promise.
        try:
            candles = build_forecast_candles(inference.paths, future_timestamps)
            uncertainty = summarize_paths(inference.close_paths())
            notes = list(quality.warnings) + list(check.warnings) + list(inference.notes)
            if identity_changed:
                notes.append(
                    f"model identity resolved to {actual_model_id}@"
                    f"{actual_model_revision} at inference; cache identity updated"
                )
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
                model_id=actual_model_id,
                model_revision=actual_model_revision,
                tokenizer_id=tokenizer_id,
                tokenizer_revision=tokenizer_revision,
                device=device,
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
        except Exception as exc:
            return self._reject(
                KronosState.ERROR,
                f"could not assemble the forecast snapshot: "
                f"{type(exc).__name__}: {exc}",
                quality,
                last_closed,
            )
        return ForecastOutcome(
            state=KronosState.READY,
            snapshot=snapshot,
            quality=quality,
            message=f"forecast generated for {instrument} {timeframe}",
            last_closed_timestamp=last_closed,
            latency_ms=inference.latency_ms,
        )
