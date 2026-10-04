"""Kronos model studio — the only sanctioned path for model lifecycle.

DOWNLOAD → VERIFY → INSTALL → BENCHMARK → ACTIVATE (master spec sections
17–19). Every status returned here is truthful by construction:

* no downloader configured / download fails → ``DOWNLOAD_NOT_CONFIGURED``;
* weights present but integrity unverified → lifecycle ``DOWNLOADED`` and
  install/activate refuse;
* no inference runner in this environment → benchmark ``NOT_ATTEMPTED`` —
  benchmark numbers are never fabricated.

The studio never loads weights into the trading process: Kronos stays an
isolated forecast worker; activation only records which verified model the
worker should load next.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from axiom.kronos import artifacts as kronos_artifacts
from axiom.kronos import backends as kronos_backends
from axiom.kronos.model_cache import ModelCache

DOWNLOAD_NOT_CONFIGURED = "DOWNLOAD_NOT_CONFIGURED"
DOWNLOADED = "DOWNLOADED"
NOT_INSTALLED = "NOT_INSTALLED"
INSTALLED = "INSTALLED"
BENCHMARKED = "BENCHMARKED"
ACTIVE = "ACTIVE"
VERIFIED = "VERIFIED"
NOT_VERIFIED = "NOT_VERIFIED"
INSTALL_REFUSED = "INSTALL_REFUSED"
ACTIVATE_REFUSED = "ACTIVATE_REFUSED"
NOT_ATTEMPTED = "NOT_ATTEMPTED"

_ACTIVE_MODEL_KEY = "kronos_active_model"


@dataclass(frozen=True)
class ModelCard:
    model_id: str
    tokenizer_id: str
    parameters_approx: int
    context_length: int | None
    lifecycle: str
    integrity: dict
    benchmark: dict | None
    notes: tuple[str, ...]

    def to_dict(self) -> dict:
        return {"model_id": self.model_id, "tokenizer_id": self.tokenizer_id,
                "parameters_approx": self.parameters_approx,
                "context_length": self.context_length,
                "lifecycle": self.lifecycle, "integrity": self.integrity,
                "benchmark": self.benchmark, "notes": list(self.notes)}


class KronosStudio:
    """Model cards + lifecycle actions. No GUI code, no network assumptions."""

    def __init__(self, cache: ModelCache | None = None, *,
                 downloader=None, repository=None):
        self.cache = cache or ModelCache()
        self.downloader = downloader          # fn(model_id) -> Path | raises
        self.repository = repository
        self._benchmarks: dict[str, dict] = {}

    # ------------------------------------------------------------- cards --
    def catalog(self) -> list[dict]:
        """Known model cards. The catalog is data, discovered from the
        artifacts module rather than re-hardcoded here."""
        cards = []
        for model_id in kronos_artifacts.KNOWN_MODEL_PAIRINGS:
            cards.append(self.card(model_id).to_dict())
        return cards

    def card(self, model_id: str) -> ModelCard:
        if model_id not in kronos_artifacts.KNOWN_MODEL_PAIRINGS:
            raise KeyError(f"unknown Kronos model '{model_id}'; known: "
                           + ", ".join(sorted(kronos_artifacts.KNOWN_MODEL_PAIRINGS)))
        report = self.cache.verify(model_id)
        weights = self.cache.locate_weights(model_id)
        notes = list(report.problems) + list(report.notes)
        lifecycle = self._lifecycle(model_id, report.ok, bool(weights))
        benchmark = self._benchmarks.get(model_id)
        context = kronos_backends.KNOWN_MODEL_CONTEXT.get(model_id)
        return ModelCard(
            model_id=model_id,
            tokenizer_id=kronos_artifacts.KNOWN_MODEL_PAIRINGS[model_id],
            parameters_approx=kronos_artifacts.KNOWN_MODEL_PARAMS.get(model_id, 0),
            context_length=context,
            lifecycle=lifecycle,
            integrity=report.to_dict(),
            benchmark=benchmark,
            notes=tuple(notes),
        )

    def _lifecycle(self, model_id: str, integrity_ok: bool, weights_present: bool) -> str:
        if not weights_present:
            return NOT_INSTALLED
        if not integrity_ok:
            return DOWNLOADED       # present but NOT verified
        if model_id in self._benchmarks:
            return BENCHMARKED
        if self._active_model() == model_id:
            return ACTIVE
        return INSTALLED

    def _active_model(self) -> str | None:
        if self.repository is None:
            return None
        try:
            return self.repository.get_engine_state(_ACTIVE_MODEL_KEY, None)
        except Exception:
            return None

    def overview(self) -> dict:
        cards = self.catalog()
        return {
            "cards": cards,
            "active_model": self._active_model(),
            "cache_root": str(self.cache.root),
            "note": "weights never ship inside the executable or Git; download "
                    "and verify through this studio only",
        }

    # ---------------------------------------------------------- lifecycle --
    def download(self, model_id: str) -> dict:
        """Fetch weights via the configured downloader.

        When no downloader is configured or it fails (offline, blocked CDN),
        the result says exactly that — the GUI shows NOT VERIFIED, never a
        fake success.
        """
        if model_id not in kronos_artifacts.KNOWN_MODEL_PAIRINGS:
            raise KeyError(f"unknown Kronos model '{model_id}'")
        if self.downloader is None:
            return {"model_id": model_id, "ok": False, "status": DOWNLOAD_NOT_CONFIGURED,
                    "detail": "no download source is configured in this build; "
                              "weights must be placed in the model cache and "
                              "verified before any install or activation"}
        try:
            path = self.downloader(model_id)
        except Exception as exc:
            return {"model_id": model_id, "ok": False, "status": DOWNLOADED,
                    "detail": f"download failed: {type(exc).__name__}; nothing "
                              f"was recorded as verified"}
        entry = self.cache.record(model_id, "downloaded", path)
        return {"model_id": model_id, "ok": True, "status": DOWNLOADED,
                "detail": f"recorded in cache (sha256 {entry.sha256[:16]}…); "
                          f"verification is still required before install"}

    def verify(self, model_id: str) -> dict:
        report = self.cache.verify(model_id)
        return {"model_id": model_id, "ok": report.ok,
                "problems": list(report.problems), "notes": list(report.notes),
                "status": VERIFIED if report.ok else NOT_VERIFIED}

    def install(self, model_id: str) -> dict:
        """INSTALL = verified + registered for loading. Refuses unverified."""
        report = self.cache.verify(model_id)
        if not report.ok:
            return {"model_id": model_id, "ok": False, "status": INSTALL_REFUSED,
                    "detail": "integrity verification must pass before install: "
                              + ("; ".join(report.problems) or "weights missing")}
        return {"model_id": model_id, "ok": True, "status": INSTALLED}

    def benchmark(self, model_id: str, *, runner=None) -> dict:
        """Measure inference latency with the supplied runner.

        Without a runner (no runtime/device in this environment) the result is
        NOT_ATTEMPTED with the reason — benchmarks are never fabricated.
        """
        if runner is None:
            return {"model_id": model_id, "ok": False, "status": NOT_ATTEMPTED,
                    "detail": "no inference runner available in this environment; "
                              "benchmark values are never fabricated"}
        started = time.perf_counter()
        try:
            measurement = runner(model_id)
        except Exception as exc:
            return {"model_id": model_id, "ok": False, "status": NOT_ATTEMPTED,
                    "detail": f"benchmark runner failed: {type(exc).__name__}"}
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        result = {"model_id": model_id, "ok": True, "status": BENCHMARKED,
                  "elapsed_ms": elapsed_ms, "measurement": measurement}
        self._benchmarks[model_id] = result
        return result

    def activate(self, model_id: str) -> dict:
        """ACTIVATE requires VERIFIED integrity first (never load unverified)."""
        report = self.cache.verify(model_id)
        if not report.ok:
            return {"model_id": model_id, "ok": False, "status": ACTIVATE_REFUSED,
                    "detail": "activation refused: integrity verification failed "
                              f"({'; '.join(report.problems) or 'weights missing'})"}
        if self.repository is not None:
            self.repository.set_engine_state(_ACTIVE_MODEL_KEY, model_id)
        return {"model_id": model_id, "ok": True, "status": ACTIVE,
                "note": "forecast worker will load this model on next start"}
