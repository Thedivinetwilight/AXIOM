"""Model-artifact status: "the code works" vs "real pretrained weights ran".

Nothing here can produce a forecast. This module answers one question with
evidence: are GENUINE pretrained Kronos weights present, did they load, and
did inference actually execute against them?

Why this module exists
----------------------
The upstream ``Kronos`` / ``KronosTokenizer`` classes instantiate happily with
random weights. A tiny random-weight model satisfies every type check, runs
``KronosPredictor.predict`` end to end, and returns finite numbers. So "the
model loaded and inference completed" is NOT evidence of real inference — it
is equally true of a 33k-parameter toy.

Two independent gates are therefore required before
``REAL_INFERENCE_VERIFIED`` can ever be reported:

1. **Checksum** — the artifact on disk matches a recorded SHA-256. This is the
   only real defence against a full-size randomly-initialised model, because
   such a model has the *correct* parameter count and cannot be told apart by
   inspection.
2. **Parameter count** — catches the toy/random-weight case, where the model
   is orders of magnitude smaller than the published one.

Neither gate alone is sufficient; ``classify`` requires both.

Offline artifacts
-----------------
The artifact directory is the *same* directory the backend loads from, so the
report cannot describe a location the loader ignores. ``bootstrap.py`` maps
``AXIOM_MODEL_CACHE`` onto ``KronosConfig.cache_dir``, and
``UpstreamKronosBackend._from_pretrained`` passes that to
``from_pretrained(cache_dir=..., local_files_only=True)``. This module
therefore defaults to :func:`axiom.kronos.model_cache.default_cache_root` and
reuses :class:`ModelCache` for layout, digests and manifest handling rather
than inventing a parallel scheme.

Copy genuine weights obtained on another machine into that directory (Hugging
Face cache layout) and AXIOM loads them with no network access. Nothing is
downloaded here and no weight file is ever committed to Git.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path

from axiom.kronos.model_cache import ModelCache, sha256_file
from axiom.models import utcnow

#: Published pairings (Kronos README model zoo). A model loaded against any
#: other tokenizer produces meaningless tokens, so this is a hard table rather
#: than a guess: an unknown model id is reported as unpaired, never defaulted.
KNOWN_MODEL_PAIRINGS: dict[str, str] = {
    "NeoQuasar/Kronos-mini": "NeoQuasar/Kronos-Tokenizer-2k",
    "NeoQuasar/Kronos-small": "NeoQuasar/Kronos-Tokenizer-base",
    "NeoQuasar/Kronos-base": "NeoQuasar/Kronos-Tokenizer-base",
}

#: Published parameter counts (Kronos README model zoo), approximate.
KNOWN_MODEL_PARAMS: dict[str, int] = {
    "NeoQuasar/Kronos-mini": 4_100_000,
    "NeoQuasar/Kronos-small": 24_700_000,
    "NeoQuasar/Kronos-base": 102_300_000,
}

#: A loaded model must have at least this fraction of its published parameter
#: count to be considered the real architecture. Measured: the random-weight
#: fixture used by the test suite is 33,664 parameters against 24.7M for
#: Kronos-small — a factor of ~733 — so a 50% floor rejects it with enormous
#: margin while tolerating rounding in the published figures.
PARAM_FLOOR_FRACTION = 0.5

#: Weight file extensions recognised in an offline artifact directory.
WEIGHT_SUFFIXES = (".safetensors", ".bin", ".pt", ".pth")


class ModelStatus(str, Enum):
    """How far the model has genuinely progressed, with evidence.

    Ordered by strength of evidence. ``REAL_INFERENCE_VERIFIED`` is reachable
    only through :func:`classify`, which demands a checksum match, a plausible
    parameter count, a successful load AND a completed inference run.
    """

    CODE_UNAVAILABLE = "CODE_UNAVAILABLE"
    CODE_AVAILABLE = "CODE_AVAILABLE"
    WEIGHTS_UNAVAILABLE = "WEIGHTS_UNAVAILABLE"
    WEIGHTS_SUSPECT = "WEIGHTS_SUSPECT"
    WEIGHTS_AVAILABLE = "WEIGHTS_AVAILABLE"
    MODEL_LOADED = "MODEL_LOADED"
    REAL_INFERENCE_VERIFIED = "REAL_INFERENCE_VERIFIED"
    INFERENCE_FAILED = "INFERENCE_FAILED"


#: States that mean "no genuine pretrained forecast is possible right now".
NOT_VERIFIED = frozenset(
    {
        ModelStatus.CODE_UNAVAILABLE,
        ModelStatus.CODE_AVAILABLE,
        ModelStatus.WEIGHTS_UNAVAILABLE,
        ModelStatus.WEIGHTS_SUSPECT,
        ModelStatus.WEIGHTS_AVAILABLE,
        ModelStatus.MODEL_LOADED,
        ModelStatus.INFERENCE_FAILED,
    }
)


def artifact_cache(override: str | None = None) -> ModelCache:
    """The :class:`ModelCache` whose root the backend actually loads from.

    Resolution order, matching the loader rather than inventing a second one:
    ``override`` (tests) → ``AXIOM_KRONOS_ARTIFACTS`` → ``AXIOM_MODEL_CACHE`` →
    the platform default. The last two are what ``bootstrap.py`` feeds into
    ``KronosConfig.cache_dir``, so a report built here and a load performed by
    ``UpstreamKronosBackend`` always look at the same directory.
    """
    raw = override if override is not None else os.environ.get("AXIOM_KRONOS_ARTIFACTS", "")
    return ModelCache(raw or None)


def expected_tokenizer(model_id: str) -> str | None:
    """The tokenizer this model must be paired with, or ``None`` if unknown."""
    return KNOWN_MODEL_PAIRINGS.get(model_id)


def pairing_problems(model_id: str, tokenizer_id: str) -> list[str]:
    """Human-readable reasons a model/tokenizer pair is wrong (empty = fine)."""
    expected = expected_tokenizer(model_id)
    if expected is None:
        return [
            (
                f"{model_id} is not a recognised official Kronos model; its required "
                "tokenizer pairing is unknown, so the pair cannot be confirmed"
            )
        ]
    if tokenizer_id != expected:
        return [
            f"{model_id} must be paired with {expected}, got {tokenizer_id!r}"
        ]
    return []


def param_count_problems(model_id: str, params: int) -> list[str]:
    """Reject models that cannot be the published architecture."""
    published = KNOWN_MODEL_PARAMS.get(model_id)
    if published is None:
        return [
            (
                f"{model_id} has no published parameter count, so the loaded model "
                "cannot be confirmed as genuine by size"
            )
        ]
    floor = int(published * PARAM_FLOOR_FRACTION)
    if params < floor:
        return [
            (
                f"loaded model has {params:,} parameters but {model_id} is published "
                f"at ~{published:,} (floor {floor:,}); this looks like a "
                "randomly-initialised or toy model, not pretrained weights"
            )
        ]
    return []


def classify(
    *,
    code_available: bool,
    artifact_found: bool,
    checksum_ok: bool,
    params_genuine: bool,
    loaded: bool,
    inference_completed: bool,
    error: str = "",
) -> ModelStatus:
    """Derive the status from evidence. The only path to verified inference.

    Deliberately strict and deliberately dumb: it cannot be persuaded by a
    successfully constructed model object, because construction is not one of
    its inputs. Reaching ``REAL_INFERENCE_VERIFIED`` requires ALL of
    ``artifact_found``, ``checksum_ok``, ``params_genuine``, ``loaded`` and
    ``inference_completed``.
    """
    if not code_available:
        return ModelStatus.CODE_UNAVAILABLE
    if not artifact_found:
        # Code imports but there is nothing genuine on disk. This is the state
        # of a sandbox with no Hugging Face access.
        return ModelStatus.WEIGHTS_UNAVAILABLE
    if not checksum_ok:
        # The artifact on disk is not demonstrably the real thing.
        return ModelStatus.WEIGHTS_SUSPECT
    if loaded and not params_genuine:
        # A real load happened and the model is the wrong size: random or toy
        # weights. This gate is deliberately checked BEFORE the error gate, so
        # bogus weights are named as the primary finding rather than being
        # reported as a mere run failure. It is gated on ``loaded`` because a
        # parameter count of 0 after a failed load means "never measured", not
        # "measured and wrong" — letting that drive the status would blame the
        # weights for an unrelated load error.
        return ModelStatus.WEIGHTS_SUSPECT
    if error:
        return ModelStatus.INFERENCE_FAILED
    if not loaded:
        return ModelStatus.WEIGHTS_AVAILABLE
    if not inference_completed:
        return ModelStatus.MODEL_LOADED
    return ModelStatus.REAL_INFERENCE_VERIFIED


@dataclass(frozen=True)
class ArtifactReport:
    """The model-artifact verification report (one row per model)."""

    model_id: str
    tokenizer_id: str
    status: ModelStatus
    artifact_dir: str = ""
    artifact_found: bool = False
    artifact_files: tuple[str, ...] = ()
    artifact_bytes: int = 0
    checksum: str = ""
    checksum_expected: str = ""
    checksum_ok: bool = False
    param_count: int = 0
    param_count_expected: int = 0
    params_genuine: bool = False
    tokenizer_compatible: bool = False
    tokenizer_problems: tuple[str, ...] = ()
    load_result: str = ""
    device: str = ""
    inference_result: str = ""
    real_inference_verified: bool = False
    problems: tuple[str, ...] = ()
    generated_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict:
        return {
            "model_id": self.model_id,
            "tokenizer_id": self.tokenizer_id,
            "status": self.status.value,
            "artifact_dir": self.artifact_dir,
            "artifact_found": self.artifact_found,
            "artifact_files": list(self.artifact_files),
            "artifact_bytes": self.artifact_bytes,
            "checksum": self.checksum,
            "checksum_expected": self.checksum_expected,
            "checksum_ok": self.checksum_ok,
            "param_count": self.param_count,
            "param_count_expected": self.param_count_expected,
            "params_genuine": self.params_genuine,
            "tokenizer_compatible": self.tokenizer_compatible,
            "tokenizer_problems": list(self.tokenizer_problems),
            "load_result": self.load_result,
            "device": self.device,
            "inference_result": self.inference_result,
            "real_inference_verified": self.real_inference_verified,
            "problems": list(self.problems),
            "generated_at": self.generated_at.isoformat(),
        }


def locate_artifacts(cache: ModelCache, model_id: str) -> list[Path]:
    """Weight files present for ``model_id`` in the cache the loader uses.

    Delegates to :meth:`ModelCache.locate_weights` so the Hugging Face layout
    (``models--<owner>--<name>/…``) and the flat fallback are interpreted
    exactly as ``from_pretrained`` will interpret them.
    """
    return cache.locate_weights(model_id)


def inspect_artifact(
    model_id: str,
    *,
    tokenizer_id: str = "",
    cache: ModelCache | None = None,
    expected_checksum: str = "",
) -> ArtifactReport:
    """Inspect on-disk artifacts for one model. Loads nothing, infers nothing.

    This is the honest starting point: it can establish ``WEIGHTS_AVAILABLE``
    or ``WEIGHTS_SUSPECT`` but never ``MODEL_LOADED`` or
    ``REAL_INFERENCE_VERIFIED``, because those need a load and an inference run
    that this function deliberately does not perform.
    """
    resolved_tokenizer = tokenizer_id or (expected_tokenizer(model_id) or "")
    problems: list[str] = []
    tok_problems = pairing_problems(model_id, resolved_tokenizer)
    problems.extend(tok_problems)

    effective = cache if cache is not None else artifact_cache()
    directory = str(effective.root)
    files = locate_artifacts(effective, model_id)
    total = sum(path.stat().st_size for path in files if path.exists())
    checksum = ""
    if files:
        # One stable digest over every weight file in sorted order, so the
        # report has a single comparable value.
        combined = hashlib.sha256()
        for path in files:
            combined.update(sha256_file(path).encode())
        checksum = combined.hexdigest()
    if not files:
        problems.append(
            f"no pretrained weight files for {model_id} under {directory}; "
            "copy genuine artifacts there or set AXIOM_KRONOS_ARTIFACTS"
        )

    checksum_ok = bool(checksum and expected_checksum
                       and checksum == expected_checksum.lower())
    if files and expected_checksum and not checksum_ok:
        problems.append(
            "artifact checksum does not match the recorded expected value; "
            "the weights may be truncated, swapped or randomly generated"
        )

    status = classify(
        code_available=True,
        artifact_found=bool(files),
        checksum_ok=checksum_ok,
        # Cannot be judged without loading; left False so the status can never
        # advance past WEIGHTS_* on inspection alone.
        params_genuine=False,
        loaded=False,
        inference_completed=False,
    )

    return ArtifactReport(
        model_id=model_id,
        tokenizer_id=resolved_tokenizer,
        status=status,
        artifact_dir=directory,
        artifact_found=bool(files),
        artifact_files=tuple(str(path) for path in files),
        artifact_bytes=total,
        checksum=checksum,
        checksum_expected=expected_checksum,
        checksum_ok=checksum_ok,
        param_count_expected=KNOWN_MODEL_PARAMS.get(model_id, 0),
        tokenizer_compatible=not tok_problems,
        tokenizer_problems=tuple(tok_problems),
        real_inference_verified=False,
        problems=tuple(problems),
    )


def inspect_all(**kwargs) -> list[ArtifactReport]:
    """Inspect every supported official model."""
    return [inspect_artifact(model_id, **kwargs) for model_id in KNOWN_MODEL_PAIRINGS]


# ---------------------------------------------------------------------------
# Deterministic verification fixture
# ---------------------------------------------------------------------------

#: Fixed context length and horizon for the artifact smoke test. Small enough
#: to run quickly on CPU, large enough to exercise real auto-regressive steps.
SMOKE_CONTEXT = 64
SMOKE_PRED_LEN = 8

#: Fixed origin so every run produces byte-identical timestamps.
SMOKE_ORIGIN = datetime(2024, 1, 2, 9, 15, tzinfo=UTC)
SMOKE_STEP = timedelta(minutes=1)


@dataclass(frozen=True)
class SmokeFixture:
    """The fixed OHLCV input every artifact verification run must use."""

    bars: tuple
    future_timestamps: tuple

    def to_dict(self) -> dict:
        return {
            "context_bars": len(self.bars),
            "pred_len": len(self.future_timestamps),
            "first_timestamp": self.bars[0].timestamp.isoformat(),
            "last_context_timestamp": self.bars[-1].timestamp.isoformat(),
            "first_prediction_timestamp": self.future_timestamps[0].isoformat(),
            "last_prediction_timestamp": self.future_timestamps[-1].isoformat(),
        }


def smoke_fixture(
    *, context: int = SMOKE_CONTEXT, pred_len: int = SMOKE_PRED_LEN
) -> SmokeFixture:
    """Build the fixed verification fixture. No randomness, no clock reads.

    Deterministic by construction: prices come from a closed-form ramp and the
    timestamps from :data:`SMOKE_ORIGIN`, so two runs on different machines
    produce identical input.
    """
    from axiom.market.provider import MarketDataBar

    bars = []
    for index in range(context):
        close = 100.0 + index * 0.25
        bars.append(
            MarketDataBar(
                timestamp=SMOKE_ORIGIN + SMOKE_STEP * index,
                instrument="AXIOM-SMOKE",
                open=round(close - 0.10, 4),
                high=round(close + 0.30, 4),
                low=round(close - 0.30, 4),
                close=round(close, 4),
                volume=1000.0 + index,
            )
        )
    future = tuple(
        SMOKE_ORIGIN + SMOKE_STEP * (context + index) for index in range(pred_len)
    )
    return SmokeFixture(bars=tuple(bars), future_timestamps=future)


def check_forecast_frame(frame, pred_len: int) -> list[str]:
    """Structural checks on a predictor output frame (empty list = healthy).

    Requirement: expected OHLCV structure, valid timestamps, no silently
    accepted NaN/Inf, and a row count matching the requested ``pred_len``.
    """
    problems: list[str] = []
    columns = list(getattr(frame, "columns", []))
    required = ["open", "high", "low", "close", "volume"]
    missing = [name for name in required if name not in columns]
    if missing:
        problems.append(f"output is missing OHLCV columns: {missing}")
    rows = int(getattr(frame, "shape", (0, 0))[0])
    if rows != pred_len:
        problems.append(f"output has {rows} rows but pred_len requested {pred_len}")
    try:
        import numpy as np

        values = frame[required].to_numpy(dtype=float)
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        problems.append(f"output could not be read as numeric: {exc}")
        return problems
    if not np.isfinite(values).all():
        bad = int((~np.isfinite(values)).sum())
        problems.append(f"output contains {bad} non-finite (NaN/Inf) values")
    index = list(getattr(frame, "index", []))
    if len(index) != pred_len:
        problems.append(f"output index has {len(index)} entries, expected {pred_len}")
    return problems


def count_parameters(module) -> int:
    """Total parameter count of a loaded module (0 if it cannot be counted)."""
    try:
        return int(sum(p.numel() for p in module.parameters()))
    except Exception:  # noqa: BLE001 - reported as unknown, never assumed fine
        return 0


def verify_model(
    config,
    *,
    cache: ModelCache | None = None,
    expected_checksum: str = "",
    fixture: SmokeFixture | None = None,
    backend=None,
) -> ArtifactReport:
    """Load genuine weights and run one real inference, then report the truth.

    This is the only function that can produce ``MODEL_LOADED``,
    ``REAL_INFERENCE_VERIFIED`` or ``INFERENCE_FAILED``. It never synthesises a
    result: every failure is recorded in ``problems`` and drives the status
    down through :func:`classify`.

    ``backend`` is injectable for tests; by default the real
    :class:`UpstreamKronosBackend` is used, so this exercises the production
    load path rather than a parallel one.
    """
    from axiom.kronos.backends import UpstreamKronosBackend

    effective_cache = cache if cache is not None else artifact_cache()
    base = inspect_artifact(
        config.model_id,
        tokenizer_id=config.tokenizer_id,
        cache=effective_cache,
        expected_checksum=expected_checksum,
    )
    problems = list(base.problems)
    load_result = "not attempted"
    inference_result = "not attempted"
    device = ""
    params = 0
    inference_completed = False
    loaded = False
    error = ""

    active = backend if backend is not None else UpstreamKronosBackend()
    try:
        info = active.load(config, cache=effective_cache)
        loaded = True
        device = str(getattr(info, "device", ""))
        load_result = (
            f"loaded {info.model_id}@{info.model_revision or 'default'} on {device} "
            f"via {info.backend} (kronos source: {info.kronos_source})"
        )
        params = count_parameters(getattr(active.predictor, "model", None))
        param_problems = param_count_problems(config.model_id, params)
        problems.extend(param_problems)

        # The fixture must match the configured horizon: backend.forecast
        # rejects a mismatch, and it is right to.
        sample = (
            fixture
            if fixture is not None
            else smoke_fixture(pred_len=config.pred_len)
        )

        # Raw upstream output, checked structurally.
        import pandas as pd

        frame = active.predictor.predict(
            pd.DataFrame(
                {
                    "open": [b.open for b in sample.bars],
                    "high": [b.high for b in sample.bars],
                    "low": [b.low for b in sample.bars],
                    "close": [b.close for b in sample.bars],
                    "volume": [b.volume for b in sample.bars],
                }
            ),
            x_timestamp=pd.Series([b.timestamp for b in sample.bars]),
            y_timestamp=pd.Series(list(sample.future_timestamps)),
            pred_len=len(sample.future_timestamps),
            T=config.temperature,
            top_k=config.top_k,
            top_p=config.top_p,
            sample_count=1,
            verbose=False,
        )
        frame_problems = check_forecast_frame(frame, len(sample.future_timestamps))
        problems.extend(frame_problems)

        # The production AXIOM path, which re-validates the frame itself.
        inference = active.forecast(
            list(sample.bars), list(sample.future_timestamps), config
        )
        if inference.pred_len != len(sample.future_timestamps):
            problems.append(
                f"KronosInference returned pred_len {inference.pred_len}, "
                f"expected {len(sample.future_timestamps)}"
            )
        inference_completed = not frame_problems and inference.pred_len == len(
            sample.future_timestamps
        )
        inference_result = (
            f"{inference.sample_paths} path(s) x {inference.pred_len} steps in "
            f"{inference.latency_ms:.1f} ms"
            + (" (structurally valid)" if inference_completed else " (INVALID)")
        )
    except Exception as exc:  # noqa: BLE001 - the whole point is to record it
        error = f"{type(exc).__name__}: {exc}"
        problems.append(f"verification failed: {error}")
        load_result = load_result if loaded else f"load failed: {error}"
        inference_result = inference_result if inference_completed else f"failed: {error}"

    params_ok = not param_count_problems(config.model_id, params) if params else False
    status = classify(
        code_available=True,
        artifact_found=base.artifact_found,
        checksum_ok=base.checksum_ok,
        params_genuine=params_ok,
        loaded=loaded,
        inference_completed=inference_completed,
        error=error,
    )

    return ArtifactReport(
        model_id=base.model_id,
        tokenizer_id=base.tokenizer_id,
        status=status,
        artifact_dir=base.artifact_dir,
        artifact_found=base.artifact_found,
        artifact_files=base.artifact_files,
        artifact_bytes=base.artifact_bytes,
        checksum=base.checksum,
        checksum_expected=base.checksum_expected,
        checksum_ok=base.checksum_ok,
        param_count=params,
        param_count_expected=base.param_count_expected,
        params_genuine=params_ok,
        tokenizer_compatible=base.tokenizer_compatible,
        tokenizer_problems=base.tokenizer_problems,
        load_result=load_result,
        device=device,
        inference_result=inference_result,
        real_inference_verified=status is ModelStatus.REAL_INFERENCE_VERIFIED,
        problems=tuple(problems),
    )
