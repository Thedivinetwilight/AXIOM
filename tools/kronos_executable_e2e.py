#!/usr/bin/env python3
"""Exercise the packaged AXIOM-Kronos executable without model weights.

This is a process/IPC packaging check only. It uses the explicitly labelled
stub backend, a deterministic synthetic fixture, and a temporary model-cache
path. It does not download weights, access market data, or place orders.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from axiom.kronos.artifacts import smoke_fixture
from axiom.models import KronosConfig
from axiom.worker.protocol import KronosRequest, encode_bars, encode_timestamps


def _child_environment(cache_dir: str) -> dict[str, str]:
    """Pass only OS runtime variables; never forward credentials to the child."""
    keep = (
        "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE",
        "LOCALAPPDATA", "APPDATA", "PATHEXT",
    )
    environment = {key: os.environ[key] for key in keep if key in os.environ}
    environment["AXIOM_MODEL_CACHE"] = cache_dir
    return environment


def run(executable: Path) -> dict:
    if not executable.is_file():
        raise FileNotFoundError(f"AXIOM-Kronos executable not found: {executable}")

    config = KronosConfig(
        enabled=True,
        backend="stub",
        lookback=64,
        pred_len=4,
        max_context=512,
        min_history=16,
        sample_paths=1,
        allow_download=False,
    ).validate()
    fixture = smoke_fixture(context=config.lookback, pred_len=config.pred_len)
    requests = [
        KronosRequest("ping", "ping-1"),
        KronosRequest("load", "load-1", {"config": config.to_dict()}),
        KronosRequest("forecast", "forecast-1", {
            "config": config.to_dict(),
            "bars": encode_bars(fixture.bars),
            "future_timestamps": encode_timestamps(fixture.future_timestamps),
        }),
        KronosRequest("status", "status-1"),
        KronosRequest("shutdown", "shutdown-1"),
    ]
    payload = "\n".join(request.encode() for request in requests) + "\n"

    with tempfile.TemporaryDirectory(prefix="axiom-kronos-e2e-") as cache_dir:
        completed = subprocess.run(
            [str(executable)],
            input=payload,
            capture_output=True,
            text=True,
            timeout=120,
            env=_child_environment(cache_dir),
            cwd=str(executable.parent),
            check=False,
        )
    if completed.returncode != 0:
        raise RuntimeError(
            f"AXIOM-Kronos exited with code {completed.returncode}; "
            f"stderr tail: {completed.stderr[-2000:]}"
        )
    try:
        responses = [json.loads(line) for line in completed.stdout.splitlines()
                     if line.strip()]
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "AXIOM-Kronos emitted non-protocol stdout; "
            f"stdout tail: {completed.stdout[-2000:]}"
        ) from exc
    by_id = {response.get("id"): response for response in responses}
    expected_ids = {request.request_id for request in requests}
    if set(by_id) != expected_ids:
        raise RuntimeError(
            f"IPC response ids differ: expected {sorted(expected_ids)}, "
            f"got {sorted(by_id)}; stderr tail: {completed.stderr[-2000:]}"
        )
    for request_id in ("ping-1", "load-1", "forecast-1", "status-1", "shutdown-1"):
        if by_id[request_id].get("ok") is not True:
            raise RuntimeError(
                f"{request_id} failed: {by_id[request_id].get('error', '')}"
            )
    if by_id["ping-1"]["result"].get("pong") is not True:
        raise RuntimeError("packaged child did not answer ping")
    if by_id["load-1"].get("state") != "READY":
        raise RuntimeError("packaged child did not load the labelled stub backend")
    inference = by_id["forecast-1"]["result"]
    if (inference.get("backend") != "stub"
            or not inference.get("paths")
            or len(inference["paths"][0]) != config.pred_len):
        raise RuntimeError("packaged child returned an invalid stub forecast response")
    if by_id["status-1"].get("result", {}).get("status", {}).get("state") != "READY":
        raise RuntimeError("packaged child status did not remain READY after inference")
    if by_id["shutdown-1"].get("state") != "OFFLINE":
        raise RuntimeError("packaged child did not acknowledge clean shutdown")

    return {
        "executable": str(executable),
        "protocol": "JSON-lines",
        "backend": "stub (test only)",
        "requests": len(requests),
        "forecast_steps": config.pred_len,
        "shutdown": "clean",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    args = parser.parse_args(argv)
    try:
        report = run(args.executable)
    except Exception as exc:
        print(f"packaged Kronos E2E failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    print("PASS: packaged process/IPC smoke only; no pretrained inference was tested")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
