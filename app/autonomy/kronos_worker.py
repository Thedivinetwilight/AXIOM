"""Standalone offline Kronos runtime probe/inference worker.

Executed by a supported system Python, including from the frozen Windows app.
No AXIOM server, broker, account, credential, or network client is imported.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import sysconfig
import time
import traceback


def _error_trace(exc: Exception) -> list[dict]:
    """Return code identity only: no paths, messages, locals or environment."""
    frames = []
    for frame, lineno in traceback.walk_tb(exc.__traceback__):
        frames.append({
            "module": frame.f_globals.get("__name__"),
            "function": frame.f_code.co_name,
            "line": lineno,
        })
    return frames[-4:]


def _probe_stage_error(stage: str, exc: Exception, versions: dict | None = None) -> dict:
    return {
        "status": "UNAVAILABLE", "error_type": "RUNTIME_PROBE_STAGE_ERROR",
        "stage": stage, "exception_type": type(exc).__name__,
        "exception_name": getattr(exc, "name", None), "trace": _error_trace(exc),
        "package_versions": versions or {},
    }


def _runtime_probe(site: Path, lock_path: Path) -> dict:
    try:
        from app.autonomy.kronos_runtime import applicable_packages, runtime_lock
    except Exception as exc:
        return _probe_stage_error("SUPPORT_IMPORT", exc)
    try:
        lock = runtime_lock(lock_path)
        expected = {item["normalized_name"]: item["version"] for item in applicable_packages(lock)}
    except Exception as exc:
        return _probe_stage_error("LOCK_READ", exc)
    versions = {}
    try:
        for distribution in importlib.metadata.distributions(path=[str(site)]):
            name = distribution.metadata.get("Name")
            if name:
                versions[name.lower().replace("_", "-")] = distribution.version
    except Exception as exc:
        return _probe_stage_error("DISTRIBUTION_SCAN", exc, versions)
    if versions != expected:
        return {"status": "UNAVAILABLE", "error_type": "RUNTIME_LOCK_MISMATCH",
                "package_versions": versions}
    modules = {}
    for name in ("numpy", "pandas", "einops", "safetensors", "huggingface_hub", "tqdm", "torch"):
        try:
            modules[name] = importlib.import_module(name)
        except Exception as exc:
            return {
                "status": "UNAVAILABLE", "error_type": "RUNTIME_IMPORT_ERROR",
                "module": name, "exception_type": type(exc).__name__,
                "exception_name": getattr(exc, "name", None),
                "package_versions": versions,
            }
    numpy = modules["numpy"]
    pandas = modules["pandas"]
    einops = modules["einops"]
    safetensors = modules["safetensors"]
    huggingface_hub = modules["huggingface_hub"]
    torch = modules["torch"]

    try:
        threads = max(1, min(4, os.cpu_count() or 1))
        torch.set_num_threads(threads)
        torch.set_num_interop_threads(1)
        direct_versions = {
            "torch": torch.__version__, "numpy": numpy.__version__,
            "pandas": pandas.__version__, "einops": einops.__version__,
            "safetensors": safetensors.__version__,
            "huggingface_hub": huggingface_hub.__version__,
        }
        cuda_available = bool(torch.cuda.is_available())
        torch_cuda_build = torch.version.cuda
    except Exception as exc:
        return _probe_stage_error("RUNTIME_READY_PROBE", exc, versions)
    return {
        "status": "READY", "schema": "axiom.kronos.runtime-probe.v1",
        "package_versions": versions, "direct_versions": direct_versions,
        "device": "cpu", "cuda_available": cuda_available,
        "torch_cuda_build": torch_cuda_build,
        "cpu_threads": threads, "runtime_lock_sha256": lock["sha256"],
    }


def _forecast(payload: dict, runtime: dict) -> dict:
    import numpy as np
    import pandas as pd
    import torch
    from app.autonomy.kronos_artifacts import ArtifactStore, load_manifest, manifest_digest, sha256_file

    manifest_path = Path(payload["manifest_path"])
    manifest = load_manifest(manifest_path)
    digest = manifest_digest(manifest)
    if digest != payload.get("manifest_sha256") \
            or sha256_file(manifest_path) != payload.get("manifest_file_sha256"):
        raise ValueError("Kronos manifest changed before inference")
    inspection = ArtifactStore(Path(payload["artifact_root"])).inspect(
        manifest, Path(payload["vendor_root"]), hash_files=True,
    )
    if not inspection["artifact_verified"]:
        raise ValueError("Kronos source/artifacts failed offline verification")

    vendor_root = str(Path(payload["vendor_root"]))
    sys.path.insert(0, vendor_root)
    import model

    model_spec = manifest["model"]
    tokenizer_spec = manifest["tokenizer"]
    root = Path(payload["artifact_root"])
    active_root, _layout = ArtifactStore(root).active_root(digest)
    load_started = time.perf_counter()
    tokenizer = model.KronosTokenizer.from_pretrained(
        str(active_root / tokenizer_spec["directory"]), local_files_only=True,
    )
    loaded_tokenizer = model.Kronos.from_pretrained(
        str(active_root / model_spec["directory"]), local_files_only=True,
    )
    predictor = model.KronosPredictor(
        loaded_tokenizer, tokenizer, max_context=int(model_spec["context"]), device="cpu",
    )
    load_ms = (time.perf_counter() - load_started) * 1000.0

    bars = payload["bars"]
    frame = pd.DataFrame([{
        "open": bar["open"], "high": bar["high"], "low": bar["low"],
        "close": bar["close"], "volume": bar["volume"],
    } for bar in bars])
    # Upstream calc_time_stamps uses the Series `.dt` accessor; DatetimeIndex
    # exposes calendar fields directly and raises AttributeError for `.dt`.
    x_timestamp = pd.Series(pd.to_datetime(payload["x_timestamp"]))
    y_timestamp = pd.Series(pd.to_datetime(payload["y_timestamp"]))
    horizon = int(payload["horizon"])
    samples = int(payload["samples"])
    seed_base = int(payload["seed_base"])
    closes = []
    inference_started = time.perf_counter()
    for sample in range(samples):
        torch.manual_seed(seed_base + sample)
        prediction = predictor.predict(
            frame, x_timestamp, y_timestamp, pred_len=horizon,
            sample_count=1, verbose=False,
        )
        closes.append(float(prediction["close"].iloc[-1]))
    inference_ms = (time.perf_counter() - inference_started) * 1000.0
    if not np.isfinite(closes).all() or min(closes) <= 0:
        raise ValueError("Kronos returned non-finite/non-positive prices")
    last_close = float(bars[-1]["close"])
    mean_close = float(np.mean(closes))
    result = {
        "instrument_key": payload["instrument_key"],
        "input_end": bars[-1]["timestamp"], "horizon": horizon,
        "mean_close": mean_close,
        "q10_close": float(np.quantile(closes, 0.10)),
        "q90_close": float(np.quantile(closes, 0.90)),
        "up_probability": sum(value > last_close for value in closes) / samples,
        "samples": samples,
    }
    return {
        "status": "FORECAST", "schema": "axiom.kronos.forecast.v1",
        "forecast": result, "model_loaded": True, "real_forecast": True,
        "mock_outputs_used": False, "device": "cpu",
        "manifest_sha256": digest,
        "model_revision": model_spec["revision"],
        "tokenizer_revision": tokenizer_spec["revision"],
        "load_ms": load_ms, "inference_ms": inference_ms,
        "runtime": runtime,
        "input_label": payload.get("input_label", "OPERATOR_COMPLETED_BARS"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--app-root", type=Path, required=True)
    parser.add_argument("--probe-only", action="store_true")
    args = parser.parse_args()
    # Isolated mode ignores PYTHONPATH/user site.  Keep the selected
    # interpreter's stdlib (including DLLs/) ahead of frozen app resources:
    # the frozen app's extension modules can target a different CPython ABI.
    # The locked runtime and reviewed app files still precede global site-packages.
    def normalized(path) -> str:
        return os.path.normcase(os.path.normpath(os.fspath(path)))

    site_directories = {
        normalized(sysconfig.get_path(name)) for name in ("purelib", "platlib")
        if sysconfig.get_path(name)
    }
    insertion = next((
        index for index, entry in enumerate(sys.path)
        if entry and normalized(entry) in site_directories
    ), len(sys.path))
    sys.path.insert(insertion, str(args.app_root))
    sys.path.insert(insertion, str(args.runtime))
    result_stream = sys.stdout
    stage = "RUNTIME_PROBE"
    try:
        # huggingface_hub 0.33.1 prints local weight-loading notices to stdout.
        # Keep every library notice away from the single-record JSON IPC channel.
        with contextlib.redirect_stdout(sys.stderr):
            runtime = _runtime_probe(args.runtime, args.lock)
            if runtime["status"] != "READY" or args.probe_only:
                result = runtime
            else:
                stage = "OPERATOR_PAYLOAD"
                payload = json.load(sys.stdin)
                if payload.get("offline") is not True:
                    raise ValueError("Kronos worker requires explicit offline mode")
                stage = "REAL_INFERENCE"
                result = _forecast(payload, runtime)
    except Exception as exc:
        # Never return raw exception text, paths, URLs, environment or stderr.
        result = {
            "status": "ERROR", "error_type": type(exc).__name__,
            "error_name": getattr(exc, "name", None), "stage": stage,
            "trace": _error_trace(exc),
            "message": "Real Kronos runtime/inference failed; no forecast was produced",
            "real_forecast": False, "mock_outputs_used": False,
        }
    print(json.dumps(result, allow_nan=False), file=result_stream)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
