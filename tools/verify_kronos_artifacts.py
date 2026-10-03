#!/usr/bin/env python3
"""Model-artifact verification report for the Kronos runtime.

Answers one question, with evidence: are GENUINE pretrained Kronos weights
present, do they load, and did inference actually run against them?

    python tools/verify_kronos_artifacts.py                 # inspect, no load
    python tools/verify_kronos_artifacts.py --load          # load + infer
    python tools/verify_kronos_artifacts.py --json out.json # machine-readable

Exit code is 0 only when every requested model reaches
``REAL_INFERENCE_VERIFIED``. Anything less — including a model that loaded
perfectly from random weights — exits non-zero, so this can gate a release
without anyone having to read the prose.

Offline use
-----------
Point ``AXIOM_KRONOS_ARTIFACTS`` (or ``AXIOM_MODEL_CACHE``) at a directory
holding genuine weights copied from a machine that could reach Hugging Face.
Nothing here downloads, and no weight file is ever committed to Git.

``--load`` performs one real inference per model on a fixed OHLCV fixture. It
never fabricates a result: a model that fails to load or produces NaN is
reported as failed, not patched over.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from axiom.kronos.artifacts import (
    KNOWN_MODEL_PAIRINGS,
    ArtifactReport,
    ModelStatus,
    artifact_cache,
    inspect_artifact,
    smoke_fixture,
    verify_model,
)
from axiom.models import KronosConfig

WIDTH = 22


def _row(label: str, value: object) -> str:
    return f"  {label:<{WIDTH}}: {value}"


def render(report: ArtifactReport) -> str:
    """Human-readable block for one model."""
    size = f"{report.artifact_bytes:,} bytes" if report.artifact_bytes else "0 bytes"
    params = (
        f"{report.param_count:,} (published ~{report.param_count_expected:,})"
        if report.param_count_expected
        else f"{report.param_count:,}"
    )
    lines = [
        f"{report.model_id}",
        _row("status", report.status.value),
        _row("tokenizer", report.tokenizer_id),
        _row("tokenizer compatible", "yes" if report.tokenizer_compatible else "NO"),
        _row("artifact location", report.artifact_dir or "(none configured)"),
        _row("artifact exists", "yes" if report.artifact_found else "no"),
        _row("artifact size", size),
        _row("checksum", report.checksum[:16] + "…" if report.checksum else "(unavailable)"),
        _row("checksum verified", "yes" if report.checksum_ok else "no"),
        _row("parameter count", params),
        _row("weights genuine", "yes" if report.params_genuine else "no"),
        _row("load result", report.load_result),
        _row("device", report.device or "(none)"),
        _row("inference result", report.inference_result),
        _row(
            "REAL pretrained inference",
            "VERIFIED" if report.real_inference_verified else "NOT VERIFIED",
        ),
    ]
    if report.problems:
        lines.append("  problems:")
        lines.extend(f"    - {problem}" for problem in report.problems)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--load",
        action="store_true",
        help="load each model and run one real inference on the fixed fixture",
    )
    parser.add_argument(
        "--model",
        action="append",
        default=None,
        help="restrict to a model id (repeatable); default is all supported models",
    )
    parser.add_argument("--json", metavar="PATH", help="also write a JSON report")
    parser.add_argument("--pred-len", type=int, default=8, help="inference horizon")
    args = parser.parse_args(argv)

    model_ids = args.model or list(KNOWN_MODEL_PAIRINGS)
    unknown = [m for m in model_ids if m not in KNOWN_MODEL_PAIRINGS]
    if unknown:
        print(f"unsupported model id(s): {', '.join(unknown)}", file=sys.stderr)
        print(f"supported: {', '.join(KNOWN_MODEL_PAIRINGS)}", file=sys.stderr)
        return 2

    cache = artifact_cache()
    reports: list[ArtifactReport] = []
    for model_id in model_ids:
        if args.load:
            config = KronosConfig(
                enabled=True,
                model_id=model_id,
                tokenizer_id=KNOWN_MODEL_PAIRINGS[model_id],
                pred_len=args.pred_len,
                lookback=64,
                max_context=512,
                device="cpu",
                min_history=8,
                allow_download=False,
            )
            reports.append(verify_model(config, cache=cache))
        else:
            reports.append(inspect_artifact(model_id, cache=cache))

    fixture = smoke_fixture(pred_len=args.pred_len)
    print("Kronos model-artifact verification")
    print(f"artifact directory : {cache.root}")
    print(f"mode               : {'load + infer' if args.load else 'inspect only'}")
    print(f"fixture            : {len(fixture.bars)} context bars, "
          f"{len(fixture.future_timestamps)} step horizon, "
          f"origin {fixture.bars[0].timestamp.isoformat()}")
    print()
    for report in reports:
        print(render(report))
        print()

    verified = [r for r in reports if r.real_inference_verified]
    print(f"result: {len(verified)}/{len(reports)} model(s) reached "
          f"{ModelStatus.REAL_INFERENCE_VERIFIED.value}")
    if not verified:
        print(
            "No genuine pretrained inference was verified. This is expected "
            "without real weights on disk: it is not a failure of the code, "
            "and it must not be reported as a successful model check."
        )

    if args.json:
        payload = {
            "artifact_directory": str(cache.root),
            "mode": "load" if args.load else "inspect",
            "fixture": fixture.to_dict(),
            "verified_count": len(verified),
            "total": len(reports),
            "reports": [report.to_dict() for report in reports],
        }
        Path(args.json).write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
        )
        print(f"wrote {args.json}")

    return 0 if verified and len(verified) == len(reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
