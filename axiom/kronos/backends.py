"""Kronos backends: the only place where the model itself is touched.

Two implementations:

``UpstreamKronosBackend``
    Loads the real Kronos tokenizer + model (``shiyu-coder/Kronos``) through its
    ``PyTorchModelHubMixin.from_pretrained`` and drives ``KronosPredictor``.
    PyTorch/pandas are imported lazily and only inside this class, so the base
    AXIOM runtime never needs them.

``StubKronosBackend``
    Deterministic, dependency-free backend for development and tests. It is
    always labelled (``kind="stub"``, provenance ``backend="stub"``) so its
    output can never be mistaken for real Kronos inference.

Sampled paths: ``KronosPredictor.predict`` averages its ``sample_count`` paths
internally, so dispersion is obtained by repeating single-path inference — see
``axiom.kronos.samples``.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import os
import sys
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from math import isfinite
from pathlib import Path
from random import Random

from axiom.kronos.model_cache import ModelCache
from axiom.market.provider import MarketDataBar, MarketDataError
from axiom.models import KronosConfig, KronosState, sha256_text, utcnow

# Context lengths published in the Kronos README model zoo. Anything not listed
# here is not assumed — the configured value is used and flagged in the notes.
KNOWN_MODEL_CONTEXT = {
    "NeoQuasar/Kronos-mini": 2048,
    "NeoQuasar/Kronos-small": 512,
    "NeoQuasar/Kronos-base": 512,
}

# Kronos consumes OHLCV and derives "amount"; the tokenizer input dim is 6.
EXPECTED_TOKENIZER_D_IN = 6

StateCallback = Callable[[KronosState, str], None]


class KronosRuntimeUnavailable(RuntimeError):
    """The Kronos runtime cannot be loaded in this environment."""


class KronosModelMismatch(RuntimeError):
    """Tokenizer and model do not belong together (or to this configuration)."""


@dataclass(frozen=True)
class KronosRuntimeInfo:
    model_id: str
    model_revision: str
    tokenizer_id: str
    tokenizer_revision: str
    device: str
    backend: str
    torch_version: str = ""
    kronos_source: str = ""
    max_context: int = 0
    loaded_at: datetime = field(default_factory=utcnow)
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "model_id": self.model_id,
            "model_revision": self.model_revision,
            "tokenizer_id": self.tokenizer_id,
            "tokenizer_revision": self.tokenizer_revision,
            "device": self.device,
            "backend": self.backend,
            "torch_version": self.torch_version,
            "kronos_source": self.kronos_source,
            "max_context": self.max_context,
            "loaded_at": self.loaded_at.isoformat(),
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class KronosInference:
    """Raw sampled trajectories, still unlabelled forecast values.

    ``model_id`` / ``model_revision`` travel with the result so the caller can
    bind a forecast to the exact weights that produced it — including across the
    IPC boundary, where the supervisor has no direct handle on the runtime.
    """

    paths: tuple[tuple[dict[str, float], ...], ...]
    latency_ms: float
    backend: str
    notes: tuple[str, ...] = ()
    model_id: str = ""
    model_revision: str = ""

    @property
    def sample_paths(self) -> int:
        return len(self.paths)

    @property
    def pred_len(self) -> int:
        return len(self.paths[0]) if self.paths else 0

    def close_paths(self) -> list[list[float]]:
        return [[step["close"] for step in path] for path in self.paths]


class KronosBackend(ABC):
    """Load a model and turn a validated window into sampled trajectories."""

    kind: str = "abstract"

    @abstractmethod
    def load(
        self,
        config: KronosConfig,
        *,
        cache: ModelCache | None = None,
        on_state: StateCallback | None = None,
    ) -> KronosRuntimeInfo:
        """Load tokenizer + model. Raises instead of reporting a false READY."""

    @abstractmethod
    def forecast(
        self,
        bars: Sequence[MarketDataBar],
        future_timestamps: Sequence[datetime],
        config: KronosConfig,
    ) -> KronosInference:
        """Run inference over CLOSED bars for the given future timestamps."""

    def close(self) -> None:
        """Release resources (best effort)."""


def validate_context(config: KronosConfig) -> list[str]:
    """Check ``max_context`` against the published context length of the model.

    Kronos models are NOT interchangeable here: Kronos-small/base are 512,
    Kronos-mini is 2048 (Kronos README model zoo). An unknown model id is not
    assumed to be 512 — the configured value is used and flagged.
    """
    known = KNOWN_MODEL_CONTEXT.get(config.model_id)
    if known is None:
        return [
            f"max_context={config.max_context} taken from AXIOM config: no "
            f"published context length for {config.model_id}"
        ]
    if config.max_context > known:
        raise KronosModelMismatch(
            f"{config.model_id} supports a context length of {known}; "
            f"configured max_context={config.max_context} exceeds it"
        )
    return []


def resolve_device(requested: str) -> tuple[str, str]:
    """Map ``auto`` onto the actual device torch reports. Returns (device, note)."""
    import torch  # local import: torch is a Kronos-runtime dependency only

    if requested != "auto":
        if requested.startswith("cuda") and not torch.cuda.is_available():
            raise KronosRuntimeUnavailable(
                f"device {requested!r} requested but CUDA is not available"
            )
        return requested, ""
    if torch.cuda.is_available():
        return "cuda:0", ""
    mps = getattr(torch.backends, "mps", None)
    if mps is not None and mps.is_available():
        return "mps", ""
    return "cpu", "no accelerator detected; using CPU"


#: Names the real Kronos ``model`` package must expose.
KRONOS_MODULE_EXPORTS = ("Kronos", "KronosTokenizer", "KronosPredictor")


def kronos_checkout_root(source: str) -> Path:
    """Validate an ``AXIOM_KRONOS_SOURCE`` value and return the checkout root.

    Accepts either the checkout root (containing ``model/kronos.py``) or the
    ``model`` directory itself. Nothing is imported here.
    """
    root = Path(source).expanduser()
    if root.name == "model" and (root / "kronos.py").is_file():
        root = root.parent
    if not (root / "model" / "kronos.py").is_file():
        raise KronosRuntimeUnavailable(
            f"AXIOM_KRONOS_SOURCE={source} does not look like a Kronos checkout "
            "(expected model/kronos.py)"
        )
    return root.resolve()


def installed_kronos_available() -> bool:
    """True if ``kronos`` is importable *and* installed as a distribution.

    Requiring distribution metadata is what keeps a stray directory named
    ``kronos`` on ``sys.path`` from being treated as the model implementation.
    """
    if importlib.util.find_spec("kronos") is None:
        return False
    try:
        importlib.metadata.distribution("kronos")
    except importlib.metadata.PackageNotFoundError:
        return False
    return True


def import_kronos_modules() -> tuple[object, object, object, str]:
    """Locate the Kronos implementation from explicitly trusted locations only.

    Search order:

    1. ``AXIOM_KRONOS_SOURCE`` — a Kronos checkout (its root contains
       ``model/kronos.py``). This is the documented, supported route.
    2. a ``kronos`` distribution installed in the environment.

    Deliberately *not* searched: every entry of ``sys.path``. ``model`` is a
    generic name, so scanning the path would let any unrelated ``model`` or
    ``kronos`` directory that happens to define those three attributes stand in
    for the model implementation — arbitrary code execution in the process that
    loads the weights. An untrusted match now fails loudly instead.
    """
    tried: list[str] = []
    source = os.environ.get("AXIOM_KRONOS_SOURCE", "").strip()

    if source:
        root = kronos_checkout_root(source)
        tried.append(f"AXIOM_KRONOS_SOURCE={root}")
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        importlib.invalidate_caches()
        module = _import_trusted("model", root)
        return _extract_exports(module)

    if installed_kronos_available():
        tried.append("installed 'kronos' distribution")
        module = importlib.import_module("kronos")
        location = Path(getattr(module, "__file__", "") or "")
        return _extract_exports(module, under=location.parent.parent or None)

    raise KronosRuntimeUnavailable(
        "Kronos implementation not found. Clone https://github.com/shiyu-coder/Kronos "
        "and set AXIOM_KRONOS_SOURCE to the checkout root (see requirements-kronos.txt), "
        "or install a 'kronos' distribution. "
        f"Tried: {', '.join(tried) or 'nothing (AXIOM_KRONOS_SOURCE unset)'}"
    )


def _import_trusted(module_name: str, root: Path):
    """Import ``module_name``, refusing a module that resolves outside ``root``."""
    module = importlib.import_module(module_name)
    location = Path(getattr(module, "__file__", "") or "")
    if not location.is_file():
        raise KronosRuntimeUnavailable(
            f"'{module_name}' resolved to {location or '<unknown>'}, which is not a file"
        )
    try:
        location.resolve().relative_to(root)
    except ValueError:
        raise KronosRuntimeUnavailable(
            f"refusing to use '{module_name}' from {location}: it is outside "
            f"AXIOM_KRONOS_SOURCE={root}. A different '{module_name}' package is "
            "earlier on sys.path; remove it or unset AXIOM_KRONOS_SOURCE."
        ) from None
    return module


def _extract_exports(module, under: Path | None = None) -> tuple[object, object, object, str]:
    missing = [name for name in KRONOS_MODULE_EXPORTS if not hasattr(module, name)]
    if missing:
        raise KronosRuntimeUnavailable(
            f"{getattr(module, '__name__', module)} does not export "
            f"{', '.join(missing)}; it is not a Kronos implementation"
        )
    location = str(getattr(module, "__file__", module))
    return (
        module.Kronos,
        module.KronosTokenizer,
        module.KronosPredictor,
        location,
    )


class UpstreamKronosBackend(KronosBackend):
    """Real Kronos inference (PyTorch, imported lazily)."""

    kind = "upstream"

    def __init__(self) -> None:
        self.predictor = None
        self.info: KronosRuntimeInfo | None = None
        self._torch = None

    # -- loading -------------------------------------------------------------
    def _from_pretrained(self, cls, model_id: str, revision: str, cache: ModelCache | None,
                         allow_download: bool, on_state: StateCallback | None):
        kwargs: dict[str, object] = {}
        if revision:
            kwargs["revision"] = revision
        if cache is not None:
            kwargs["cache_dir"] = str(cache.root)
        kwargs["local_files_only"] = not allow_download
        if allow_download and on_state is not None:
            on_state(KronosState.DOWNLOADING, f"downloading {model_id}")
        try:
            return cls.from_pretrained(model_id, **kwargs)
        except TypeError:
            # Older/newer hub versions with a different keyword set.
            kwargs.pop("local_files_only", None)
            return cls.from_pretrained(model_id, **kwargs)

    def load(
        self,
        config: KronosConfig,
        *,
        cache: ModelCache | None = None,
        on_state: StateCallback | None = None,
    ) -> KronosRuntimeInfo:
        config.validate()
        notes: list[str] = []

        try:
            import torch  # noqa: F401  (Kronos runtime dependency)
        except ImportError as exc:
            raise KronosRuntimeUnavailable(
                "PyTorch is not installed in this runtime; install the Kronos "
                f"dependency set (requirements-kronos.txt): {exc}"
            ) from exc
        self._torch = sys.modules["torch"]

        Kronos, KronosTokenizer, KronosPredictor, source = import_kronos_modules()

        if on_state is not None:
            on_state(KronosState.LOADING, f"loading {config.tokenizer_id}")
        tokenizer = self._from_pretrained(
            KronosTokenizer, config.tokenizer_id, config.tokenizer_revision,
            cache, config.allow_download, on_state,
        )
        if on_state is not None:
            on_state(KronosState.LOADING, f"loading {config.model_id}")
        model = self._from_pretrained(
            Kronos, config.model_id, config.model_revision,
            cache, config.allow_download, on_state,
        )

        self._check_pairing(model, tokenizer, config)
        device, device_note = resolve_device(config.device)
        if device_note:
            notes.append(device_note)

        notes.extend(validate_context(config))

        self.predictor = KronosPredictor(
            model, tokenizer, device=device, max_context=config.max_context
        )
        self.info = KronosRuntimeInfo(
            model_id=config.model_id,
            model_revision=self._resolve_revision(cache, config.model_id, config.model_revision),
            tokenizer_id=config.tokenizer_id,
            tokenizer_revision=self._resolve_revision(
                cache, config.tokenizer_id, config.tokenizer_revision
            ),
            device=device,
            backend=self.kind,
            torch_version=getattr(self._torch, "__version__", "unknown"),
            kronos_source=str(source),
            max_context=config.max_context,
            notes=tuple(notes),
        )
        if cache is not None:
            self._record_weights(cache, config)
        return self.info

    @staticmethod
    def _check_pairing(model, tokenizer, config: KronosConfig) -> None:
        """A tokenizer/model pair must agree, or the tokens mean nothing."""
        problems: list[str] = []
        for attribute in ("s1_bits", "s2_bits"):
            model_value = getattr(model, attribute, None)
            tokenizer_value = getattr(tokenizer, attribute, None)
            if model_value is None or tokenizer_value is None:
                continue
            if model_value != tokenizer_value:
                problems.append(
                    f"{attribute} mismatch: model={model_value}, tokenizer={tokenizer_value}"
                )
        d_in = getattr(tokenizer, "d_in", None)
        if d_in is not None and d_in != EXPECTED_TOKENIZER_D_IN:
            problems.append(
                f"tokenizer d_in={d_in}, expected {EXPECTED_TOKENIZER_D_IN} "
                "(OHLCV + amount)"
            )
        if problems:
            raise KronosModelMismatch(
                f"{config.tokenizer_id} does not match {config.model_id}: "
                + "; ".join(problems)
            )

    @staticmethod
    def _resolve_revision(cache: ModelCache | None, model_id: str, requested: str) -> str:
        """Pinned revision if configured, else the commit the cache resolved."""
        if requested:
            return requested
        if cache is None:
            return ""
        ref = cache.root / ("models--" + model_id.replace("/", "--")) / "refs" / "main"
        try:
            return ref.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _record_weights(self, cache: ModelCache, config: KronosConfig) -> None:
        """Track what is on disk so later loads can verify SHA-256."""
        for model_id, revision in (
            (config.model_id, self.info.model_revision if self.info else ""),
            (config.tokenizer_id, self.info.tokenizer_revision if self.info else ""),
        ):
            if cache.entry_for(model_id, revision) is not None:
                continue
            for path in cache.locate_weights(model_id):
                try:
                    cache.record(model_id, revision, path)
                    break
                except OSError:
                    continue

    # -- inference -----------------------------------------------------------
    def forecast(
        self,
        bars: Sequence[MarketDataBar],
        future_timestamps: Sequence[datetime],
        config: KronosConfig,
    ) -> KronosInference:
        if self.predictor is None or self.info is None:
            raise KronosRuntimeUnavailable("Kronos is not loaded")
        if not bars:
            raise MarketDataError("no closed bars supplied for inference")
        if len(future_timestamps) != config.pred_len:
            raise MarketDataError(
                f"future timestamps ({len(future_timestamps)}) != pred_len "
                f"({config.pred_len})"
            )
        import pandas as pd

        frame = pd.DataFrame(
            {
                "open": [bar.open for bar in bars],
                "high": [bar.high for bar in bars],
                "low": [bar.low for bar in bars],
                "close": [bar.close for bar in bars],
                "volume": [bar.volume for bar in bars],
            }
        )
        x_timestamp = pd.Series([bar.timestamp for bar in bars])
        y_timestamp = pd.Series(list(future_timestamps))

        started = time.perf_counter()
        paths: list[tuple[dict[str, float], ...]] = []
        for _ in range(max(1, config.sample_paths)):
            # sample_count=1 per call: upstream averages its sampled paths, so
            # repeated single-path calls are the only way to keep dispersion.
            predicted = self.predictor.predict(
                df=frame,
                x_timestamp=x_timestamp,
                y_timestamp=y_timestamp,
                pred_len=config.pred_len,
                T=config.temperature,
                top_k=config.top_k,
                top_p=config.top_p,
                sample_count=1,
                verbose=False,
            )
            paths.append(self._path_from_frame(predicted, future_timestamps))
        latency_ms = (time.perf_counter() - started) * 1000.0
        return KronosInference(
            paths=tuple(paths),
            latency_ms=latency_ms,
            backend=self.kind,
            notes=(
                f"{len(paths)} single-path inference run(s); upstream averages "
                "sample_count>1 internally",
            ),
            model_id=self.info.model_id,
            model_revision=self.info.model_revision,
        )

    @staticmethod
    def _path_from_frame(predicted, future_timestamps: Sequence[datetime]):
        columns = ("open", "high", "low", "close", "volume")
        missing = [c for c in columns if c not in getattr(predicted, "columns", [])]
        if missing:
            raise KronosRuntimeUnavailable(
                f"Kronos output is missing column(s): {', '.join(missing)}"
            )
        if len(predicted) != len(future_timestamps):
            raise KronosRuntimeUnavailable(
                f"Kronos returned {len(predicted)} rows for "
                f"{len(future_timestamps)} future timestamps"
            )
        index = list(getattr(predicted, "index", []))
        for position, expected in enumerate(future_timestamps):
            if position < len(index):
                actual = index[position]
                stamp = getattr(actual, "to_pydatetime", lambda: actual)()
                if stamp != expected:
                    raise KronosRuntimeUnavailable(
                        f"Kronos timestamp misalignment at step {position}: "
                        f"{stamp} != {expected}"
                    )
        path = []
        for position in range(len(future_timestamps)):
            row = predicted.iloc[position]
            values = {name: float(row[name]) for name in columns}
            if not all(isfinite(value) for value in values.values()):
                raise KronosRuntimeUnavailable(
                    f"Kronos produced non-finite values at step {position}: {values!r}"
                )
            path.append(values)
        return tuple(path)

    def close(self) -> None:
        self.predictor = None
        self.info = None


class StubKronosBackend(KronosBackend):
    """Deterministic stand-in used for development and tests.

    It is reproducible (seeded by the input window) but it is NOT Kronos: every
    result is labelled ``backend="stub"`` so it can never be presented as model
    output, and ``KronosConfig.backend`` must be set to ``"stub"`` explicitly.
    """

    kind = "stub"

    def __init__(self) -> None:
        self.info: KronosRuntimeInfo | None = None

    def load(
        self,
        config: KronosConfig,
        *,
        cache: ModelCache | None = None,
        on_state: StateCallback | None = None,
    ) -> KronosRuntimeInfo:
        config.validate()
        if on_state is not None:
            on_state(KronosState.LOADING, "loading deterministic stub backend")
        self.info = KronosRuntimeInfo(
            model_id=f"stub:{config.model_id}",
            model_revision="stub",
            tokenizer_id=f"stub:{config.tokenizer_id}",
            tokenizer_revision="stub",
            device="cpu",
            backend=self.kind,
            torch_version="",
            kronos_source="axiom.kronos.backends.StubKronosBackend",
            max_context=config.max_context,
            notes=(
                "STUB backend: deterministic placeholder trajectories for "
                "development/tests, not Kronos model output",
            ),
        )
        return self.info

    def forecast(
        self,
        bars: Sequence[MarketDataBar],
        future_timestamps: Sequence[datetime],
        config: KronosConfig,
    ) -> KronosInference:
        if self.info is None:
            raise KronosRuntimeUnavailable("stub backend is not loaded")
        if not bars:
            raise MarketDataError("no closed bars supplied for inference")
        if len(future_timestamps) != config.pred_len:
            raise MarketDataError(
                f"future timestamps ({len(future_timestamps)}) != pred_len ({config.pred_len})"
            )
        started = time.perf_counter()
        closes = [bar.close for bar in bars]
        window_hash = sha256_text(
            "|".join(f"{bar.timestamp.isoformat()}:{bar.close:.6f}" for bar in bars)
        )
        drift = (closes[-1] / closes[0] - 1.0) / max(len(closes), 1)
        volatility = _mean_absolute_return(closes) or 1e-4
        mean_volume = sum(bar.volume for bar in bars) / len(bars)

        paths: list[tuple[dict[str, float], ...]] = []
        for path_index in range(max(1, config.sample_paths)):
            seed = int(sha256(f"{window_hash}:{path_index}".encode()).hexdigest()[:12], 16)
            rng = Random(seed)
            previous = closes[-1]
            steps = []
            for step in range(len(future_timestamps)):
                shock = rng.gauss(0.0, volatility)
                # Damped drift + noise; scaled by temperature for realism only.
                move = drift * max(0.2, 1.0 - step / (4 * len(future_timestamps)))
                close = max(1e-6, previous * (1 + move + shock * config.temperature))
                opening = previous
                spread = abs(rng.uniform(0.0, volatility))
                high = max(opening, close) * (1 + spread)
                low = min(opening, close) * (1 - spread)
                volume = max(0.0, mean_volume * (1 + rng.gauss(0.0, 0.15)))
                steps.append(
                    {
                        "open": opening,
                        "high": high,
                        "low": low,
                        "close": close,
                        "volume": volume,
                    }
                )
                previous = close
            paths.append(tuple(steps))
        return KronosInference(
            paths=tuple(paths),
            latency_ms=(time.perf_counter() - started) * 1000.0,
            backend=self.kind,
            notes=("deterministic stub trajectories",),
            model_id=self.info.model_id,
            model_revision=self.info.model_revision,
        )


def _mean_absolute_return(closes: Sequence[float]) -> float:
    if len(closes) < 2:
        return 0.0
    returns = [
        abs(closes[i] / closes[i - 1] - 1.0)
        for i in range(1, len(closes))
        if closes[i - 1]
    ]
    return sum(returns) / len(returns) if returns else 0.0


def build_backend(config: KronosConfig) -> KronosBackend:
    if config.backend == "stub":
        return StubKronosBackend()
    return UpstreamKronosBackend()
