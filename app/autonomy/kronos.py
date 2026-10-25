"""Conditional Kronos inference with pinned, hash-verified real artifacts.

No synthetic weights or forecast fallback exists in this module.  Missing files,
hash mismatches, optional dependency failures, stale inputs and inference errors
all produce an explicit unavailable/unverified status.  Execution code must use
validated non-Kronos candidates without attributing them to Kronos.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import threading
from typing import Iterable

from app.autonomy.models import Forecast
from app.data.models import Candle, TIMEFRAMES


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class KronosService:
    def __init__(self, manifest_path: Path, artifact_root: Path, vendor_root: Path):
        self.manifest_path = manifest_path
        self.artifact_root = artifact_root
        self.vendor_root = vendor_root
        self._lock = threading.RLock()
        self._predictor = None
        self._status = "NOT_CHECKED"
        self._runtime_status = "NOT_CHECKED"
        self._artifact_verified = False
        self._issues: list[str] = []
        self._digest = ""
        self._manifest = None

    @property
    def artifact_digest(self) -> str:
        return self._digest

    def verify(self) -> dict:
        with self._lock:
            self._issues = []
            self._digest = ""
            self._predictor = None
            self._runtime_status = "NOT_CHECKED"
            self._artifact_verified = False
            try:
                self._manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
                if self._manifest.get("schema") != "axiom.kronos.artifacts.v1":
                    raise ValueError("Unsupported Kronos artifact manifest")
                source = self._manifest["source"]
                for relative, expected in source["files"].items():
                    path = self.vendor_root / relative
                    if not path.is_file() or _sha256(path) != expected:
                        self._issues.append(f"Vendored source mismatch: {relative}")
                for component in ("model", "tokenizer"):
                    spec = self._manifest[component]
                    if len(spec["revision"]) != 40:
                        self._issues.append(f"{component} revision is not pinned")
                    for relative, expected in spec["files"].items():
                        path = self.artifact_root / spec["directory"] / relative
                        if not path.is_file():
                            self._issues.append(f"Missing {component} artifact: {relative}")
                            continue
                        if path.stat().st_size != expected["size"]:
                            self._issues.append(f"Size mismatch: {component}/{relative}")
                        elif _sha256(path) != expected["sha256"]:
                            self._issues.append(f"SHA-256 mismatch: {component}/{relative}")
                self._digest = hashlib.sha256(_json(self._manifest).encode()).hexdigest()
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                self._issues.append(f"Artifact manifest invalid: {type(exc).__name__}")
            self._status = "VERIFIED" if not self._issues else (
                "UNAVAILABLE" if any("Missing" in issue for issue in self._issues) else "UNVERIFIED"
            )
            self._artifact_verified = self._status == "VERIFIED"
            return self.status()

    def status(self) -> dict:
        manifest = self._manifest or {}
        return {
            "status": self._status,
            "artifact_verified": self._artifact_verified,
            "available_for_inference": self._status == "VERIFIED" and self._predictor is not None,
            "inference_runtime": self._runtime_status,
            "issues": list(self._issues),
            "artifact_digest": self._digest,
            "model_repository": manifest.get("model", {}).get("repository", "NeoQuasar/Kronos-small"),
            "model_revision": manifest.get("model", {}).get("revision", ""),
            "tokenizer_repository": manifest.get("tokenizer", {}).get(
                "repository", "NeoQuasar/Kronos-Tokenizer-base"
            ),
            "tokenizer_revision": manifest.get("tokenizer", {}).get("revision", ""),
            "source_commit": manifest.get("source", {}).get("commit", ""),
            "weights_bundled": False,
            "fallback": "Validated non-Kronos path only; no Kronos attribution",
            "mock_outputs_used": False,
        }

    def _load(self):
        if self._predictor is not None:
            return self._predictor
        if self._status != "VERIFIED" or self._manifest is None:
            raise RuntimeError("Kronos artifacts are not verified")
        try:
            # Upstream imports `model.module`; add only the pinned vendor root
            # while importing, then remove it.  No hub download is permitted.
            sys.path.insert(0, str(self.vendor_root))
            os.environ["HF_HUB_OFFLINE"] = "1"
            os.environ["TRANSFORMERS_OFFLINE"] = "1"
            module = importlib.import_module("model")
            model_spec = self._manifest["model"]
            tokenizer_spec = self._manifest["tokenizer"]
            tokenizer = module.KronosTokenizer.from_pretrained(
                str(self.artifact_root / tokenizer_spec["directory"]), local_files_only=True
            )
            model = module.Kronos.from_pretrained(
                str(self.artifact_root / model_spec["directory"]), local_files_only=True
            )
            self._predictor = module.KronosPredictor(
                model, tokenizer, max_context=int(model_spec["context"]), device="cpu"
            )
            self._runtime_status = "READY"
            return self._predictor
        except Exception as exc:
            self._runtime_status = "UNAVAILABLE"
            self._status = "UNAVAILABLE_DEPENDENCIES"
            self._issues = [f"Real Kronos inference could not load: {type(exc).__name__}"]
            raise
        finally:
            # Remove every temporary entry without hiding cleanup failures.
            sys.path[:] = [entry for entry in sys.path if entry != str(self.vendor_root)]

    def forecast(self, instrument_key: str, candles: Iterable[Candle], *, timeframe: str,
                 horizon: int = 5, samples: int = 8, validated: bool = False,
                 validation_id: str = "") -> Forecast:
        """Run actual upstream inference and derive empirical sample distribution.

        Multiple real stochastic upstream samples (not fabricated intervals)
        provide the mean, q10/q90 and directional frequency.  Inference is
        deterministic for a given input/artifact sample schedule via local seeds.
        """
        bars = list(candles)
        if not bars or horizon <= 0 or samples < 4:
            raise ValueError("Completed candles, a positive horizon and at least four samples required")
        with self._lock:
            predictor = self._load()
            try:
                import numpy as np
                import pandas as pd
                import torch

                frame = pd.DataFrame([{
                    "open": bar.open, "high": bar.high, "low": bar.low,
                    "close": bar.close, "volume": bar.volume,
                } for bar in bars])
                x_timestamp = pd.DatetimeIndex([bar.timestamp for bar in bars])
                step = timedelta(seconds=TIMEFRAMES[timeframe])
                y_timestamp = pd.DatetimeIndex([
                    bars[-1].timestamp + step * index for index in range(1, horizon + 1)
                ])
                seed_base = int(hashlib.sha256(
                    f"{self._digest}|{instrument_key}|{bars[-1].timestamp.isoformat()}".encode()
                ).hexdigest()[:8], 16)
                closes = []
                for sample in range(samples):
                    torch.manual_seed(seed_base + sample)
                    prediction = predictor.predict(
                        frame, x_timestamp, y_timestamp, pred_len=horizon,
                        sample_count=1, verbose=False,
                    )
                    closes.append(float(prediction["close"].iloc[-1]))
                if not np.isfinite(closes).all() or min(closes) <= 0:
                    raise ValueError("Kronos returned non-finite/non-positive prices")
                last_close = bars[-1].close
                forecast = Forecast(
                    instrument_key=instrument_key,
                    input_end=bars[-1].timestamp,
                    generated_at=datetime.now(timezone.utc),
                    horizon=horizon,
                    mean_close=float(np.mean(closes)),
                    q10_close=float(np.quantile(closes, 0.10)),
                    q90_close=float(np.quantile(closes, 0.90)),
                    up_probability=sum(value > last_close for value in closes) / samples,
                    artifact_digest=self._digest,
                    model_revision=self._manifest["model"]["revision"],
                    tokenizer_revision=self._manifest["tokenizer"]["revision"],
                    validated=validated,
                    validation_id=validation_id,
                )
                forecast.validate(last_close)
                return forecast
            except Exception:
                self._runtime_status = "ERROR"
                self._status = "INFERENCE_ERROR"
                self._issues = ["Real Kronos inference failed; fallback required"]
                raise

    @staticmethod
    def stale(forecast: Forecast, current_bar: Candle, *, max_age_seconds: float,
              now: datetime) -> bool:
        age = (now - forecast.generated_at).total_seconds()
        return forecast.input_end != current_bar.timestamp or not 0 <= age <= max_age_seconds
