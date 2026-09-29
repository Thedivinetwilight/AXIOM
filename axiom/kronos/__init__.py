"""Kronos subsystem: backend wrappers, runtime lifecycle, model cache, sampling."""

from axiom.kronos.backends import (
    KNOWN_MODEL_CONTEXT,
    KronosBackend,
    KronosInference,
    KronosModelMismatch,
    KronosRuntimeInfo,
    KronosRuntimeUnavailable,
    StubKronosBackend,
    UpstreamKronosBackend,
    build_backend,
    import_kronos_modules,
    resolve_device,
)
from axiom.kronos.model_cache import (
    IntegrityReport,
    ModelCache,
    ModelManifestEntry,
    default_cache_root,
    sha256_file,
)
from axiom.kronos.runtime import KronosRuntime
from axiom.kronos.samples import (
    MIN_PATHS_FOR_UNCERTAINTY,
    interval_coverage,
    mean_interval_width,
    path_quantile_band,
    percentile,
    summarize_paths,
)

__all__ = [
    "KNOWN_MODEL_CONTEXT",
    "IntegrityReport",
    "KronosBackend",
    "KronosInference",
    "KronosModelMismatch",
    "KronosRuntime",
    "KronosRuntimeInfo",
    "KronosRuntimeUnavailable",
    "MIN_PATHS_FOR_UNCERTAINTY",
    "ModelCache",
    "ModelManifestEntry",
    "StubKronosBackend",
    "UpstreamKronosBackend",
    "build_backend",
    "default_cache_root",
    "import_kronos_modules",
    "interval_coverage",
    "mean_interval_width",
    "path_quantile_band",
    "percentile",
    "resolve_device",
    "sha256_file",
    "summarize_paths",
]
