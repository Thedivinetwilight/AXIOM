"""Kronos runtime: load the model, hold status, run inference.

This is the object that lives inside the AXIOM-Kronos process. It never touches
market data providers, stores or the GUI — it is handed CLOSED bars and the
future timestamps to predict, and returns sampled trajectories.

``READY`` is only reported after the tokenizer and the model were actually
loaded and cross-checked; every failure path reports ``ERROR`` with the reason.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime

from axiom.kronos.backends import (
    KronosBackend,
    KronosInference,
    KronosModelMismatch,
    KronosRuntimeUnavailable,
    build_backend,
)
from axiom.kronos.model_cache import ModelCache
from axiom.log import get_logger
from axiom.market.provider import MarketDataBar
from axiom.models import (
    KronosConfig,
    KronosState,
    KronosStatus,
    utcnow,
)


logger = get_logger("kronos.runtime")


class KronosRuntime:
    """Owns one Kronos backend and its lifecycle state."""

    def __init__(
        self,
        config: KronosConfig,
        *,
        backend: KronosBackend | None = None,
        cache: ModelCache | None = None,
        clock: Callable[[], datetime] = utcnow,
        on_state: Callable[[KronosState, str], None] | None = None,
    ) -> None:
        self.config = config.validate()
        self.cache = cache
        self.clock = clock
        self.on_state = on_state
        self.backend = backend or build_backend(self.config)
        self._info = None
        self._status = self._initial_status()

    # -- status --------------------------------------------------------------
    def _initial_status(self) -> KronosStatus:
        if not self.config.enabled:
            return KronosStatus(
                state=KronosState.DISABLED,
                message="Kronos is disabled in configuration",
                backend=self.config.backend,
                model_id=self.config.model_id,
                tokenizer_id=self.config.tokenizer_id,
                updated_at=self.clock(),
            )
        return KronosStatus(
            state=KronosState.INITIALIZING,
            message="Kronos has not been loaded yet",
            backend=self.config.backend,
            model_id=self.config.model_id,
            tokenizer_id=self.config.tokenizer_id,
            cache_dir=str(self.cache.root) if self.cache else "",
            updated_at=self.clock(),
        )

    @property
    def status(self) -> KronosStatus:
        return self._status

    @property
    def info(self):
        return self._info

    def _set(self, state: KronosState, message: str, **extra) -> KronosStatus:
        previous = self._status.state
        self._status = self._status.with_updates(state=state, message=message, **extra)
        if state is KronosState.ERROR:
            logger.error("kronos %s -> ERROR: %s", previous.value, message)
        elif state is not previous:
            logger.info("kronos %s -> %s: %s", previous.value, state.value, message)
        if self.on_state is not None:
            self.on_state(state, message)
        return self._status

    # -- lifecycle -----------------------------------------------------------
    def load(self) -> KronosStatus:
        if not self.config.enabled:
            return self._set(KronosState.DISABLED, "Kronos is disabled in configuration")
        self._set(KronosState.INITIALIZING, f"preparing {self.config.backend} backend")
        if self.cache is not None:
            # Integrity is verified before ANY backend loads weights, so a
            # corrupted or swapped file can never reach the model.
            integrity = self.cache.verify(self.config.model_id, self.config.model_revision)
            if integrity.problems:
                return self._set(
                    KronosState.ERROR,
                    "model integrity check failed: " + "; ".join(integrity.problems),
                    last_error="; ".join(integrity.problems),
                )
        try:
            self._info = self.backend.load(
                self.config, cache=self.cache, on_state=self._notify
            )
        except (KronosRuntimeUnavailable, KronosModelMismatch) as exc:
            self._info = None
            return self._set(KronosState.ERROR, str(exc), last_error=str(exc))
        except Exception as exc:  # never leave the UI believing it is READY
            self._info = None
            message = f"Kronos load failed: {type(exc).__name__}: {exc}"
            return self._set(KronosState.ERROR, message, last_error=message)
        info = self._info
        return self._set(
            KronosState.READY,
            f"{info.model_id} loaded on {info.device}",
            model_id=info.model_id,
            model_revision=info.model_revision,
            tokenizer_id=info.tokenizer_id,
            tokenizer_revision=info.tokenizer_revision,
            device=info.device,
            torch_version=info.torch_version,
            kronos_source=info.kronos_source,
            loaded_at=info.loaded_at,
            last_error="",
        )

    def _notify(self, state: KronosState, message: str) -> None:
        self._set(state, message)

    # -- inference -----------------------------------------------------------
    def forecast_paths(
        self,
        bars: Sequence[MarketDataBar],
        future_timestamps: Sequence[datetime],
    ) -> KronosInference:
        """Run inference. Raises if the runtime is not READY."""
        if self._status.state is not KronosState.READY or self._info is None:
            raise KronosRuntimeUnavailable(
                f"Kronos is not ready (state={self._status.state.value}: "
                f"{self._status.message})"
            )
        self._set(KronosState.INFERENCING, "running Kronos inference")
        try:
            result = self.backend.forecast(bars, future_timestamps, self.config)
        except Exception as exc:
            # Inference failures leave the runtime READY-but-errored: the model
            # is still loaded, so the next closed bar can retry.
            self._set(
                KronosState.READY,
                f"inference failed: {type(exc).__name__}: {exc}",
                last_error=str(exc),
            )
            raise
        self._set(
            KronosState.READY,
            f"{self._info.model_id} ready",
            last_inference_ms=result.latency_ms,
            inferences=self._status.inferences + 1,
            last_error="",
        )
        return result

    def mark_stale(self, reason: str) -> KronosStatus:
        return self._set(KronosState.STALE, reason)

    def mark_offline(self, reason: str) -> KronosStatus:
        self._info = None
        return self._set(KronosState.OFFLINE, reason)

    def shutdown(self) -> None:
        try:
            self.backend.close()
        finally:
            self._info = None
            if self._status.state is not KronosState.DISABLED:
                self._set(KronosState.OFFLINE, "Kronos runtime shut down")
