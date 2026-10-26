"""Verified, offline-only Kronos inference using a per-user CPU runtime.

No synthetic weights or forecast fallback exists here.  Artifact presence,
artifact integrity, runtime readiness, model loading and real forecast output
are deliberately separate status facts.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import threading
from typing import Iterable
from uuid import uuid4

from app.autonomy.kronos_artifacts import (
    ArtifactError, ArtifactStore, KronosArtifactInstaller, SecureHuggingFaceDownloader,
    load_manifest, manifest_digest, sha256_file,
)
from app.autonomy.kronos_runtime import KronosRuntimeManager
from app.autonomy.models import Forecast
from app.data.models import Candle, TIMEFRAMES


class KronosService:
    def __init__(self, manifest_path: Path, artifact_root: Path, vendor_root: Path, *,
                 runtime_lock_path: Path | None = None, worker_path: Path | None = None,
                 application_root: Path | None = None,
                 runtime_manager: KronosRuntimeManager | None = None,
                 artifact_installer: KronosArtifactInstaller | None = None):
        self.manifest_path = Path(manifest_path)
        self.artifact_root = Path(artifact_root)
        self.vendor_root = Path(vendor_root)
        self.application_root = Path(application_root or Path(__file__).resolve().parents[2])
        self.runtime_lock_path = Path(
            runtime_lock_path or self.application_root / "config" / "kronos-runtime-requirements.txt"
        )
        self.worker_path = Path(worker_path or Path(__file__).with_name("kronos_worker.py"))
        self.store = ArtifactStore(self.artifact_root)
        self.runtime = runtime_manager or KronosRuntimeManager(
            self.artifact_root, self.runtime_lock_path, self.worker_path,
            application_root=self.application_root,
        )
        self.installer = artifact_installer or KronosArtifactInstaller(
            self.manifest_path, self.store, SecureHuggingFaceDownloader(),
        )
        self._lock = threading.RLock()
        self._status = "NOT_CHECKED"
        self._runtime_status: dict = {"status": "NOT_CHECKED", "verified": False}
        self._artifact_verified = False
        self._available = False
        self._issues: list[str] = []
        self._digest = ""
        self._manifest_file_digest = ""
        self._manifest: dict | None = None
        self._inspection: dict = {}
        self._worker_active = False
        self._last_inference: dict | None = None
        self._last_self_test: dict | None = None

    @property
    def artifact_digest(self) -> str:
        return self._digest

    @property
    def manifest(self) -> dict:
        if self._manifest is None:
            self._manifest = load_manifest(self.manifest_path)
            self._digest = manifest_digest(self._manifest)
            self._manifest_file_digest = sha256_file(self.manifest_path)
        return self._manifest

    def _python_identity(self) -> dict:
        python = self._runtime_status.get("python") or {}
        return {
            "python_version": python.get("version"),
            "python_machine": python.get("machine"),
            "python_system": python.get("system"),
        }

    def _load_recorded_self_test(self) -> None:
        self._last_self_test = None
        try:
            raw = json.loads((self.artifact_root / "self-test.json").read_text(encoding="utf-8"))
            runtime_lock = self._runtime_status.get("runtime_lock_sha256") \
                or self._runtime_status.get("lock_sha256")
            identity = self._python_identity()
            if raw.get("status") == "PASSED" and raw.get("manifest_sha256") == self._digest \
                    and raw.get("manifest_file_sha256") == self._manifest_file_digest \
                    and raw.get("runtime_lock_sha256") == runtime_lock \
                    and all(value is not None for value in identity.values()) \
                    and all(raw.get(key) == value for key, value in identity.items()):
                self._last_self_test = raw
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return

    def verify(self) -> dict:
        with self._lock:
            self._issues = []
            self._artifact_verified = False
            self._available = False
            self._inspection = {}
            try:
                manifest = self.manifest
                self._digest = manifest_digest(manifest)
                self._inspection = self.store.inspect(manifest, self.vendor_root, hash_files=True)
                self._artifact_verified = bool(self._inspection["artifact_verified"])
                self._issues.extend(self._inspection["issues"])
            except ArtifactError as exc:
                self._issues.append(str(exc))
            try:
                self._runtime_status = self.runtime.probe()
            except Exception as exc:
                self._runtime_status = {
                    "status": "UNAVAILABLE", "verified": False,
                    "issues": [f"Runtime verification failed: {type(exc).__name__}"],
                }
            self._issues.extend(self._runtime_status.get("issues") or [])
            self._available = self._artifact_verified and self._runtime_status.get("status") == "READY"
            if self._available:
                self._status = "READY"
            elif self._issues and not self._artifact_verified:
                self._status = "UNVERIFIED" if any("MISMATCH" in issue for issue in self._issues) \
                    else "UNAVAILABLE"
            else:
                self._status = "UNAVAILABLE"
            self._load_recorded_self_test()
            return self.status()

    def select_python(self, executable: str | None) -> dict:
        """Select only the inference interpreter; never touch execution state."""
        with self._lock:
            self.runtime.select_python(executable)
            self._last_inference = None
            self._last_self_test = None
            return self.verify()

    def install_plan(self) -> dict:
        manifest = self.manifest
        components = {}
        for component in ("model", "tokenizer"):
            spec = manifest[component]
            components[component] = {
                "repository": spec["repository"], "revision": spec["revision"],
                "expected_bytes": sum(item["size"] for item in spec["files"].values()),
                "files": [{
                    "name": relative, "size": expected["size"], "sha256": expected["sha256"],
                    "url": SecureHuggingFaceDownloader.url_for(
                        spec["repository"], spec["revision"], relative,
                    ),
                } for relative, expected in spec["files"].items()],
            }
        return {
            "manifest_sha256": manifest_digest(manifest),
            "manifest_file_sha256": self._manifest_file_digest or sha256_file(self.manifest_path),
            "source": {"repository": manifest["source"]["repository"],
                       "commit": manifest["source"]["commit"]},
            **components,
            "expected_total_bytes": sum(
                components[component]["expected_bytes"] for component in ("model", "tokenizer")
            ),
            "download_policy": "HTTPS only; every redirect host allowlisted; size + SHA-256 enforced",
            "atomic_install": "Complete verified generation, then atomic current-pointer replacement",
            "location": "AXIOM per-user data directory; never repository/application install directory",
            "runtime": self.runtime.install_plan(),
        }

    @staticmethod
    def _public_runtime(runtime: dict) -> dict:
        result = {key: value for key, value in runtime.items() if key != "python"}
        python = runtime.get("python")
        if python:
            result["python"] = {
                key: python.get(key) for key in (
                    "version", "machine", "system", "pip", "implementation", "bits",
                    "selection", "command_label",
                )
            }
        return result

    def status(self) -> dict:
        with self._lock:
            manifest = self._manifest or {}
            return {
                "status": self._status,
                "artifact_verified": self._artifact_verified,
                "available_for_inference": self._available,
                "inference_runtime": self._runtime_status.get("status", "NOT_CHECKED"),
                "issues": list(self._issues),
                "artifact_digest": self._digest,
                "manifest_file_sha256": self._manifest_file_digest,
                "model_repository": manifest.get("model", {}).get("repository", ""),
                "model_revision": manifest.get("model", {}).get("revision", ""),
                "tokenizer_repository": manifest.get("tokenizer", {}).get("repository", ""),
                "tokenizer_revision": manifest.get("tokenizer", {}).get("revision", ""),
                "source_commit": manifest.get("source", {}).get("commit", ""),
                "source": self._inspection.get("source") or {},
                "model": self._inspection.get("model") or {},
                "tokenizer": self._inspection.get("tokenizer") or {},
                "artifact_layout": self._inspection.get("layout", "NOT_CHECKED"),
                "partial_installs": self._inspection.get("partial_installs", 0),
                "runtime": self._public_runtime(self._runtime_status),
                "loaded": self._worker_active,
                "producing_real_forecasts": self._worker_active,
                "last_inference": self._last_inference,
                "last_self_test": self._last_self_test,
                "weights_bundled": False,
                "fallback": "Validated non-Kronos path only; no Kronos attribution",
                "mock_outputs_used": False,
                "device": "cpu",
            }

    def _payload(self, instrument_key: str, bars: list[Candle], timeframe: str,
                 horizon: int, samples: int, input_label: str) -> dict:
        step = timedelta(seconds=TIMEFRAMES[timeframe])
        seed_base = int(hashlib.sha256(
            f"{self._digest}|{instrument_key}|{bars[-1].timestamp.isoformat()}".encode()
        ).hexdigest()[:8], 16)
        return {
            "offline": True, "manifest_path": str(self.manifest_path),
            "artifact_root": str(self.artifact_root), "vendor_root": str(self.vendor_root),
            "manifest_sha256": self._digest,
            "manifest_file_sha256": self._manifest_file_digest,
            "instrument_key": instrument_key,
            "bars": [{
                "timestamp": bar.timestamp.isoformat(), "open": bar.open, "high": bar.high,
                "low": bar.low, "close": bar.close, "volume": bar.volume,
            } for bar in bars],
            "x_timestamp": [bar.timestamp.isoformat() for bar in bars],
            "y_timestamp": [
                (bars[-1].timestamp + step * index).isoformat()
                for index in range(1, horizon + 1)
            ],
            "horizon": horizon, "samples": samples, "seed_base": seed_base,
            "input_label": input_label,
        }

    def forecast(self, instrument_key: str, candles: Iterable[Candle], *, timeframe: str,
                 horizon: int = 5, samples: int = 8, validated: bool = False,
                 validation_id: str = "", input_label: str = "COMPLETED_BARS") -> Forecast:
        bars = list(candles)
        if not bars or horizon <= 0 or samples < 4 or timeframe not in TIMEFRAMES:
            raise ValueError("Completed candles, a valid timeframe/horizon and at least four samples required")
        with self._lock:
            if not self._available or self._manifest is None:
                raise ArtifactError("Kronos is UNAVAILABLE: verified artifacts and runtime are required")
            self._worker_active = True
            try:
                result = self.runtime.forecast(self._payload(
                    instrument_key, bars, timeframe, horizon, samples, input_label,
                ))
                if result.get("status") != "FORECAST" or not result.get("real_forecast") \
                        or result.get("mock_outputs_used") is not False:
                    raise ArtifactError(
                        f"Real Kronos inference failed: {result.get('error_type', 'WORKER_ERROR')}"
                    )
                if result.get("manifest_sha256") != self._digest \
                        or result.get("model_revision") != self._manifest["model"]["revision"] \
                        or result.get("tokenizer_revision") != self._manifest["tokenizer"]["revision"] \
                        or result.get("device") != "cpu":
                    raise ArtifactError("Kronos worker artifact/device identity mismatch")
                raw = result["forecast"]
                generated_at = datetime.now(timezone.utc)
                forecast = Forecast(
                    instrument_key=instrument_key, input_end=bars[-1].timestamp,
                    generated_at=generated_at, horizon=horizon,
                    mean_close=float(raw["mean_close"]), q10_close=float(raw["q10_close"]),
                    q90_close=float(raw["q90_close"]), up_probability=float(raw["up_probability"]),
                    artifact_digest=self._digest,
                    model_revision=self._manifest["model"]["revision"],
                    tokenizer_revision=self._manifest["tokenizer"]["revision"],
                    validated=validated, validation_id=validation_id,
                )
                forecast.validate(bars[-1].close)
                runtime = result.get("runtime") or {}
                self._last_inference = {
                    "status": "REAL_FORECAST", "generated_at": generated_at.isoformat(),
                    "manifest_sha256": self._digest,
                    "manifest_file_sha256": self._manifest_file_digest,
                    "model_revision": result["model_revision"],
                    "tokenizer_revision": result["tokenizer_revision"],
                    "runtime_lock_sha256": runtime.get("runtime_lock_sha256"),
                    "package_versions": runtime.get("direct_versions") or runtime.get("package_versions"),
                    "device": "cpu", "cuda_available": runtime.get("cuda_available"),
                    "cpu_threads": runtime.get("cpu_threads"),
                    "load_ms": result.get("load_ms"), "inference_ms": result.get("inference_ms"),
                    "input_label": input_label, "model_loaded": True,
                    "forecast": forecast.public(), "mock_outputs_used": False,
                }
                self._status = "READY"
                return forecast
            except Exception as exc:
                self._available = False
                self._status = "INFERENCE_ERROR"
                self._issues = ["Real Kronos inference failed; non-Kronos fallback is required"]
                self._last_inference = {
                    "status": "ERROR", "error_type": type(exc).__name__,
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "manifest_sha256": self._digest, "mock_outputs_used": False,
                }
                raise
            finally:
                self._worker_active = False

    def self_test(self) -> dict:
        """Offline real-inference smoke test; never a market evaluation."""
        self.verify()
        if not self._available:
            raise ArtifactError("Kronos self-test is UNAVAILABLE until artifacts and runtime verify")
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        bars = []
        for index in range(128):
            close = 100.0 + index * 0.025 + math.sin(index / 7.0) * 0.4
            bars.append(Candle(
                start + timedelta(minutes=index), close - 0.08, close + 0.22,
                close - 0.24, close, 10_000 + (index % 9) * 31, "KRONOS_SMOKE",
            ))
        forecast = self.forecast(
            "KRONOS_SMOKE", bars, timeframe="1m", horizon=3, samples=4,
            validated=False, validation_id="",
            input_label="SYNTHETIC_INFERENCE_SMOKE_ONLY_NOT_MARKET_EVALUATION",
        )
        with self._lock:
            record = dict(self._last_inference or {})
            record.update({
                "status": "PASSED", "schema": "axiom.kronos.self-test.v1",
                "manifest_sha256": self._digest,
                "runtime_lock_sha256": (self._last_inference or {}).get("runtime_lock_sha256"),
                "source_commit": self._manifest["source"]["commit"],
                **self._python_identity(),
                "artifacts": {
                    component: {
                        relative: view["actual_sha256"]
                        for relative, view in self._inspection[component]["files"].items()
                    } for component in ("model", "tokenizer")
                },
                "forecast_validated": forecast.mean_close > 0,
                "market_evaluation": False,
                "profitability_evidence": False,
            })
            temporary = self.artifact_root / f".self-test-{uuid4().hex}.tmp"
            self.artifact_root.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(record, sort_keys=True, allow_nan=False), encoding="utf-8")
            with temporary.open("r+b") as handle:
                os.fsync(handle.fileno())
            os.replace(temporary, self.artifact_root / "self-test.json")
            self._last_self_test = record
            return record

    def close(self) -> None:
        self.installer.downloader.close()
        self.runtime.cancel_active()

    @staticmethod
    def stale(forecast: Forecast, current_bar: Candle, *, max_age_seconds: float,
              now: datetime) -> bool:
        age = (now - forecast.generated_at).total_seconds()
        return forecast.input_end != current_bar.timestamp or not 0 <= age <= max_age_seconds
