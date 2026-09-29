"""AXIOM domain models for the Kronos forecasting subsystem.

These models are deliberately SEPARATE from Arena's authoritative state. Arena
3.0.5 (``app/``) owns trading state, risk, strategy lifecycle, execution,
accounts and learning. Kronos is a forecasting subsystem only, so its output
lives in its own types (``ForecastSnapshot`` / ``ForecastCandle``) that are
never written into Arena snapshots, decisions, risk or account state.

Every model-derived value carries explicit provenance: the ``origin`` field is
always ``KRONOS_FORECAST`` and every price/volume attribute is prefixed with
``forecast_`` so a predicted value can never be rendered or persisted as an
observed "Close" / "Volume".

The module imports nothing from ``app`` and nothing heavier than the standard
library — it is shared by the GUI process, the Kronos worker process and the
research tooling.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from math import isfinite
from typing import Any

# Explicit provenance tags. A forecast is never an observation.
MARKET_ORIGIN = "MARKET_OBSERVED"
FORECAST_ORIGIN = "KRONOS_FORECAST"

# Timeframe → bar length in seconds. Declared, never inferred: a CSV or API
# that claims a timeframe must actually be spaced that way (see validation).
TIMEFRAME_SECONDS: dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1D": 86400,
}

UTC = timezone.utc


class KronosState(str, Enum):
    """Lifecycle states of the Kronos runtime, as surfaced by the UI.

    ``READY`` is only ever set after the tokenizer AND the model were really
    loaded and a smoke check passed (see ``axiom.kronos.runtime``).
    """

    DISABLED = "DISABLED"
    INITIALIZING = "INITIALIZING"
    DOWNLOADING = "DOWNLOADING"
    LOADING = "LOADING"
    READY = "READY"
    INFERENCING = "INFERENCING"
    STALE = "STALE"
    OFFLINE = "OFFLINE"
    ERROR = "ERROR"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    DATA_ERROR = "DATA_ERROR"


#: States that mean "no usable forecast can be produced right now".
UNAVAILABLE_STATES = frozenset(
    {
        KronosState.DISABLED,
        KronosState.OFFLINE,
        KronosState.ERROR,
        KronosState.INSUFFICIENT_HISTORY,
        KronosState.DATA_ERROR,
    }
)


class DataQualityState(str, Enum):
    """Quality verdict for the market data window fed to Kronos."""

    OK = "OK"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    DATA_ERROR = "DATA_ERROR"
    UNAVAILABLE = "UNAVAILABLE"


def utcnow() -> datetime:
    return datetime.now(UTC)


def require_aware_utc(value: datetime, name: str = "timestamp") -> datetime:
    """Timestamps must be timezone-aware; naive timestamps are rejected."""
    if not isinstance(value, datetime):
        raise ValueError(f"{name} must be a datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware (UTC)")
    return value.astimezone(UTC)


def canonical_json(payload: Any) -> str:
    """Deterministic JSON for hashing (sorted keys, no float ambiguity)."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class KronosConfig:
    """Kronos runtime configuration.

    Defaults follow the Kronos README for ``NeoQuasar/Kronos-small`` +
    ``NeoQuasar/Kronos-Tokenizer-base`` (context length 512). They are NOT
    assumed to be valid for every Kronos model: ``max_context`` is a property
    of the loaded model and is validated against the loaded tokenizer/model
    configuration at load time (``axiom.kronos.runtime``), not trusted blindly.
    """

    enabled: bool = False
    backend: str = "upstream"  # "upstream" (real Kronos) or "stub" (dev/test)
    model_id: str = "NeoQuasar/Kronos-small"
    model_revision: str = ""  # "" = default branch head; pinned revision preferred
    tokenizer_id: str = "NeoQuasar/Kronos-Tokenizer-base"
    tokenizer_revision: str = ""
    lookback: int = 400
    pred_len: int = 120
    max_context: int = 512
    temperature: float = 1.0
    top_k: int = 0
    top_p: float = 0.9
    sample_paths: int = 1
    device: str = "auto"
    update_on_closed_bar: bool = True
    min_history: int = 64
    cache_dir: str = ""
    allow_download: bool = False
    stale_after_seconds: float = 3600.0

    def validate(self) -> "KronosConfig":
        """Fail loudly on an unusable configuration; never silently clamp."""
        problems: list[str] = []
        if self.backend not in {"upstream", "stub"}:
            problems.append("backend must be 'upstream' or 'stub'")
        if self.lookback < 2:
            problems.append("lookback must be >= 2")
        if self.pred_len < 1:
            problems.append("pred_len must be >= 1")
        if self.max_context < 8:
            problems.append("max_context must be >= 8")
        if self.lookback > self.max_context:
            # The upstream rolling buffer would silently truncate the window;
            # that is a data-contract violation, not a convenience.
            problems.append(
                f"lookback ({self.lookback}) must not exceed max_context ({self.max_context})"
            )
        if self.min_history < 2 or self.min_history > self.lookback:
            problems.append("min_history must be within [2, lookback]")
        if not isfinite(self.temperature) or self.temperature <= 0:
            problems.append("temperature must be a finite number > 0")
        if not isfinite(self.top_p) or not 0.0 < self.top_p <= 1.0:
            problems.append("top_p must be within (0, 1]")
        if self.top_k < 0:
            problems.append("top_k must be >= 0 (0 disables top-k filtering)")
        if self.sample_paths < 1:
            problems.append("sample_paths must be >= 1")
        if self.stale_after_seconds <= 0:
            problems.append("stale_after_seconds must be > 0")
        device = str(self.device)
        if device not in {"auto", "cpu", "mps"} and not device.startswith("cuda"):
            problems.append("device must be 'auto', 'cpu', 'mps' or 'cuda[:n]'")
        for name in ("model_id", "tokenizer_id"):
            if not str(getattr(self, name)).strip():
                problems.append(f"{name} must not be empty")
        if problems:
            raise ValueError("Invalid KronosConfig: " + "; ".join(problems))
        return self

    def config_hash(self) -> str:
        """Stable hash of the inference-affecting settings (cache identity)."""
        payload = {
            "backend": self.backend,
            "model_id": self.model_id,
            "model_revision": self.model_revision,
            "tokenizer_id": self.tokenizer_id,
            "tokenizer_revision": self.tokenizer_revision,
            "lookback": self.lookback,
            "pred_len": self.pred_len,
            "max_context": self.max_context,
            "temperature": self.temperature,
            "top_k": self.top_k,
            "top_p": self.top_p,
            "sample_paths": self.sample_paths,
            "device": self.device,
        }
        return sha256_text(canonical_json(payload))[:32]

    def sampling_signature(self) -> str:
        """Hash of only the sampling knobs (used for uncertainty reporting)."""
        return sha256_text(
            canonical_json(
                {
                    "temperature": self.temperature,
                    "top_k": self.top_k,
                    "top_p": self.top_p,
                    "sample_paths": self.sample_paths,
                }
            )
        )[:16]

    def with_updates(self, **changes: Any) -> "KronosConfig":
        return replace(self, **changes).validate()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "KronosConfig":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in payload.items() if k in known}).validate()


@dataclass(frozen=True)
class KronosStatus:
    """Runtime status of the Kronos process/runtime (UI-facing)."""

    state: KronosState = KronosState.DISABLED
    message: str = "Kronos is disabled"
    backend: str = "upstream"
    model_id: str = ""
    model_revision: str = ""
    tokenizer_id: str = ""
    tokenizer_revision: str = ""
    device: str = ""
    torch_version: str = ""
    kronos_source: str = ""
    cache_dir: str = ""
    loaded_at: datetime | None = None
    updated_at: datetime | None = None
    last_error: str = ""
    last_inference_ms: float = 0.0
    inferences: int = 0
    pid: int = 0

    @property
    def is_ready(self) -> bool:
        return self.state in {KronosState.READY, KronosState.INFERENCING}

    @property
    def available(self) -> bool:
        return self.state not in UNAVAILABLE_STATES

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["state"] = self.state.value
        for key in ("loaded_at", "updated_at"):
            if payload[key] is not None:
                payload[key] = payload[key].isoformat()
        return payload

    def with_updates(self, **changes: Any) -> "KronosStatus":
        return replace(self, updated_at=utcnow(), **changes)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "KronosStatus":
        """Rebuild a status received over IPC (worker → GUI process)."""

        def _stamp(value: Any) -> datetime | None:
            if value in (None, ""):
                return None
            return value if isinstance(value, datetime) else datetime.fromisoformat(value)

        known = set(cls.__dataclass_fields__)
        data = {key: value for key, value in payload.items() if key in known}
        data["state"] = KronosState(payload.get("state", KronosState.DISABLED.value))
        data["loaded_at"] = _stamp(payload.get("loaded_at"))
        data["updated_at"] = _stamp(payload.get("updated_at"))
        return cls(**data)

    @classmethod
    def disabled(cls, message: str = "Kronos is disabled") -> "KronosStatus":
        return cls(state=KronosState.DISABLED, message=message, updated_at=utcnow())

    @classmethod
    def error(cls, message: str, *, base: "KronosStatus | None" = None) -> "KronosStatus":
        source = base or cls()
        return replace(
            source,
            state=KronosState.ERROR,
            message=message,
            last_error=message,
            updated_at=utcnow(),
        )


@dataclass(frozen=True)
class DataProvenance:
    """Where the input window came from. Required on every forecast."""

    source: str  # e.g. "replay:data/sample_1h.csv", "http:https://...", "csv:/path"
    provider: str  # provider class name
    instrument: str
    timeframe: str
    bars: int
    first_timestamp: datetime | None = None
    last_timestamp: datetime | None = None
    retrieved_at: datetime | None = None
    input_hash: str = ""
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in ("first_timestamp", "last_timestamp", "retrieved_at"):
            if payload[key] is not None:
                payload[key] = payload[key].isoformat()
        payload["notes"] = list(self.notes)
        return payload


@dataclass(frozen=True)
class MarketDataQuality:
    """Verdict for the validated input window."""

    state: DataQualityState = DataQualityState.OK
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    bars: int = 0

    @property
    def ok(self) -> bool:
        return self.state is DataQualityState.OK

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "bars": self.bars,
        }

    @classmethod
    def unavailable(cls, reason: str) -> "MarketDataQuality":
        return cls(state=DataQualityState.UNAVAILABLE, errors=(reason,), bars=0)

    @classmethod
    def insufficient(cls, bars: int, required: int) -> "MarketDataQuality":
        return cls(
            state=DataQualityState.INSUFFICIENT_HISTORY,
            errors=(f"insufficient history: {bars} closed bars, {required} required",),
            bars=bars,
        )


@dataclass(frozen=True)
class ForecastCandle:
    """One predicted bar. Field names are prefixed so a forecast can never be
    mistaken for an observed candle."""

    timestamp: datetime
    forecast_open: float
    forecast_high: float
    forecast_low: float
    forecast_close: float
    forecast_volume: float
    origin: str = FORECAST_ORIGIN

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", require_aware_utc(self.timestamp))
        if self.origin != FORECAST_ORIGIN:
            raise ValueError(
                f"ForecastCandle.origin must be {FORECAST_ORIGIN!r}, got {self.origin!r}"
            )
        values = (
            self.forecast_open,
            self.forecast_high,
            self.forecast_low,
            self.forecast_close,
            self.forecast_volume,
        )
        for name, value in zip(
            ("forecast_open", "forecast_high", "forecast_low", "forecast_close",
             "forecast_volume"),
            values,
        ):
            if not isfinite(value):
                raise ValueError(f"{name} must be finite, got {value!r}")

    @property
    def is_forecast(self) -> bool:
        return True

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["timestamp"] = self.timestamp.isoformat()
        return payload


@dataclass(frozen=True)
class ForecastUncertainty:
    """Dispersion of sampled trajectories.

    This is *sample dispersion*, NOT calibrated probability: ``calibrated`` is
    always ``False`` and the UI must label it as dispersion. It is only present
    when several independent sampled paths were generated
    (``sample_paths >= MIN_UNCERTAINTY_PATHS``).
    """

    sample_count: int
    p10_close: float
    p50_close: float
    p90_close: float
    dispersion_close: float  # mean std-dev across the horizon
    interval_width_close: float  # p90 - p10 at the horizon
    interval_width_pct: float  # interval width relative to the median
    mean_path_std: tuple[float, ...] = ()  # per-step std, optional
    close_band: tuple[tuple[float, float], ...] = ()  # per-step (p10, p90)
    calibrated: bool = False

    MIN_UNCERTAINTY_PATHS = 3

    @property
    def supported(self) -> bool:
        return self.sample_count >= self.MIN_UNCERTAINTY_PATHS and not self.calibrated

    def label(self) -> str:
        return "sample dispersion (not calibrated probability)"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["mean_path_std"] = list(self.mean_path_std)
        payload["close_band"] = [list(pair) for pair in self.close_band]
        payload["supported"] = self.supported
        return payload


@dataclass(frozen=True)
class ForecastSnapshot:
    """A validated Kronos forecast for one instrument/timeframe."""

    instrument: str
    timeframe: str
    generated_at: datetime
    candles: tuple[ForecastCandle, ...]
    config: KronosConfig
    provenance: DataProvenance
    data_quality: MarketDataQuality = field(default_factory=MarketDataQuality)
    model_id: str = ""
    model_revision: str = ""
    tokenizer_id: str = ""
    tokenizer_revision: str = ""
    device: str = ""
    backend: str = "upstream"
    last_closed_timestamp: datetime | None = None
    last_actual_close: float | None = None
    uncertainty: ForecastUncertainty | None = None
    latency_ms: float = 0.0
    input_hash: str = ""
    cache_key: str = ""
    context_closes: tuple[tuple[datetime, float], ...] = ()
    origin: str = FORECAST_ORIGIN
    note: str = "MODEL FORECAST — not observed market data"

    def __post_init__(self) -> None:
        object.__setattr__(self, "generated_at", require_aware_utc(self.generated_at))
        if self.origin != FORECAST_ORIGIN:
            raise ValueError(
                f"ForecastSnapshot.origin must be {FORECAST_ORIGIN!r}, got {self.origin!r}"
            )
        if not self.candles:
            raise ValueError("ForecastSnapshot requires at least one forecast candle")
        if len(self.candles) != self.config.pred_len:
            raise ValueError(
                f"forecast length {len(self.candles)} != configured pred_len "
                f"{self.config.pred_len}"
            )
        stamps = [c.timestamp for c in self.candles]
        if any(b <= a for a, b in zip(stamps, stamps[1:])):
            raise ValueError("forecast timestamps must be strictly increasing")
        if self.uncertainty is not None and not self.uncertainty.supported:
            raise ValueError(
                "uncertainty requires >= "
                f"{ForecastUncertainty.MIN_UNCERTAINTY_PATHS} sampled paths "
                "(sample dispersion is not calibrated probability)"
            )

    @property
    def forecast_endpoint(self) -> datetime:
        return self.candles[-1].timestamp

    @property
    def context_tail(self) -> tuple[tuple[datetime, float], ...]:
        """Trailing ACTUAL closes (observed market data, not forecast)."""
        return self.context_closes

    @property
    def horizon_close(self) -> float:
        """Terminal forecast close: median path when sampled, else the path."""
        if self.uncertainty is not None:
            return self.uncertainty.p50_close
        return self.candles[-1].forecast_close

    @property
    def horizon_return(self) -> float | None:
        """Forecast horizon return vs the last ACTUAL close (labelled)."""
        if not self.last_actual_close:
            return None
        return self.horizon_close / self.last_actual_close - 1.0

    def forecast_close_series(self) -> list[tuple[datetime, float]]:
        return [(c.timestamp, c.forecast_close) for c in self.candles]

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument": self.instrument,
            "timeframe": self.timeframe,
            "origin": self.origin,
            "note": self.note,
            "generated_at": self.generated_at.isoformat(),
            "model_id": self.model_id,
            "model_revision": self.model_revision,
            "tokenizer_id": self.tokenizer_id,
            "tokenizer_revision": self.tokenizer_revision,
            "device": self.device,
            "backend": self.backend,
            "lookback": self.config.lookback,
            "pred_len": self.config.pred_len,
            "max_context": self.config.max_context,
            "sample_paths": self.config.sample_paths,
            "temperature": self.config.temperature,
            "top_p": self.config.top_p,
            "top_k": self.config.top_k,
            "last_closed_timestamp": (
                self.last_closed_timestamp.isoformat()
                if self.last_closed_timestamp
                else None
            ),
            "last_actual_close": self.last_actual_close,
            "forecast_endpoint": self.forecast_endpoint.isoformat(),
            "horizon_close": self.horizon_close,
            "horizon_return": self.horizon_return,
            "uncertainty": self.uncertainty.to_dict() if self.uncertainty else None,
            "latency_ms": round(self.latency_ms, 3),
            "input_hash": self.input_hash,
            "cache_key": self.cache_key,
            "provenance": self.provenance.to_dict(),
            "data_quality": self.data_quality.to_dict(),
            "context_closes": [
                [stamp.isoformat(), value] for stamp, value in self.context_closes
            ],
            "candles": [c.to_dict() for c in self.candles],
        }
