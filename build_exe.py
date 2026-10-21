#!/usr/bin/env python3
"""Build a single-file Arena executable for THIS platform.

    python build_exe.py                 # build
    python build_exe.py --test          # build, then smoke-test the artifact
    python build_exe.py --onedir        # a folder instead of one file

PyInstaller cannot cross-compile: a Windows .exe must be built on Windows.
Run this on the machine you want the executable for.

Why a script instead of a bare PyInstaller command: the build is worthless if
the artifact starts and then fails on first request, which is the usual outcome
when a dynamic import was missed. So this script installs the build dependency,
runs the build, and (with --test) proves the result actually serves the console
and sets up its own data directory.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SPEC = ROOT / "packaging" / "arena.spec"
DIST = ROOT / "dist"
BUILD = ROOT / "build"
ARTIFACT = DIST / ("Arena.exe" if os.name == "nt" else "Arena")


def run(argv: list[str], **kwargs) -> int:
    print("  $ " + " ".join(argv), flush=True)
    return subprocess.call(argv, cwd=str(ROOT), **kwargs)


def ensure_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401

        return
    except ImportError:
        pass
    print("  PyInstaller is not installed; installing it now…", flush=True)
    code = run([sys.executable, "-m", "pip", "install", "pyinstaller"])
    if code != 0:
        raise SystemExit(
            "Could not install PyInstaller. Install it yourself with:\n"
            f"    {sys.executable} -m pip install pyinstaller"
        )


def clean() -> None:
    for path in (DIST, BUILD):
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)


def write_build_meta() -> Path:
    """Record the exact commit/version this bundle is built from.

    The file is bundled as data, so a packaged Arena can identify itself on
    machines without git — and the UI/tests can detect stale builds. Written
    fresh on every build; never committed.
    """
    from datetime import datetime, timezone

    sys.path.insert(0, str(ROOT))
    try:
        from app import __version__
    finally:
        sys.path.pop(0)
    commit = ""
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(ROOT),
                             capture_output=True, text=True, timeout=10)
        if out.returncode == 0:
            commit = out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    meta = {
        "version": __version__,
        "commit": commit,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "platform": sys.platform,
        "python": sys.version.split()[0],
    }
    path = ROOT / "app" / "BUILD_META.json"
    path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return path


def build(onefile: bool) -> Path:
    ensure_pyinstaller()
    clean()
    write_build_meta()
    argv = [
        sys.executable, "-m", "PyInstaller", str(SPEC),
        "--noconfirm", "--distpath", str(DIST), "--workpath", str(BUILD),
    ]
    if not onefile:
        argv.append("--onedir")
    code = run(argv)
    if code != 0:
        raise SystemExit(f"Build failed with exit code {code}.")
    if not ARTIFACT.exists():
        raise SystemExit(f"Build reported success but {ARTIFACT} is missing.")
    return ARTIFACT


def smoke_test(artifact: Path) -> bool:
    """Full end-to-end verification of the ACTUAL executable.

    Not a smoke test in the old sense: this runs packaging/frozen_e2e.py,
    which starts the artifact in a fresh external home, validates the served
    frontend JavaScript with a real parser, drives the real wizard button flow
    (start paper -> replay -> preflight -> START PAPER TRADING) against the
    running build, verifies simulated executions, restart recovery and
    duplicate-launch handling, checks the default Upstox-key startup in a
    LOCAL_PAPER-configured frozen process, and ensures no live orders occur.

    Loaded by path: ``packaging/`` must NOT be a Python package, because the
    ``packaging`` name belongs to the PyPI library PyInstaller itself imports.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "arena_frozen_e2e", ROOT / "packaging" / "frozen_e2e.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    result = module.run_frozen_e2e(artifact, ROOT / "build" / "e2e-workdir")
    failed = [r for r in result["results"] if not r["ok"]]
    if failed:
        print("\n  FAILED CHECKS:")
        for entry in failed:
            print(f"    - {entry['name']}: {entry['detail']}")

    # The paper-account startup failure is a real incident on an installed
    # Windows build, so the ARTIFACT itself is driven through it: refuse with
    # exit code 3, keep the saved account, recover into a separate empty
    # account, start again.
    recovery_spec = importlib.util.spec_from_file_location(
        "arena_frozen_recovery_e2e", ROOT / "packaging" / "frozen_recovery_e2e.py")
    recovery_module = importlib.util.module_from_spec(recovery_spec)
    recovery_spec.loader.exec_module(recovery_module)
    recovery = recovery_module.run_frozen_recovery_e2e(
        artifact, ROOT / "build" / "recovery-e2e-workdir")
    recovery_failed = [r for r in recovery["results"] if not r["ok"]]
    if recovery_failed:
        print("\n  FAILED PAPER-RECOVERY CHECKS:")
        for entry in recovery_failed:
            print(f"    - {entry['name']}: {entry['detail']}")

    upstox_start = module.run_frozen_upstox_start_e2e(
        artifact, ROOT / "build" / "upstox-start-e2e-workdir")
    upstox_failed = [r for r in upstox_start["results"] if not r["ok"]]
    if upstox_failed:
        print("\n  FAILED UPSTOX-START CHECKS:")
        for entry in upstox_failed:
            print(f"    - {entry['name']}: {entry['detail']}")
    return result["ok"] and recovery["ok"] and upstox_start["ok"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the Arena executable.")
    parser.add_argument("--test", action="store_true",
                        help="smoke-test the built executable")
    parser.add_argument("--onedir", action="store_true",
                        help="build a folder instead of a single file")
    args = parser.parse_args()

    print(f"  platform : {sys.platform}  python {sys.version.split()[0]}")
    print(f"  output   : {ARTIFACT.name}")

    artifact = build(onefile=not args.onedir)
    size_mb = artifact.stat().st_size / (1024 * 1024)
    print(f"\n  built: {artifact}  ({size_mb:.1f} MB)")

    if args.test:
        ok = smoke_test(artifact)
        print()
        if ok:
            print("  SMOKE TEST PASSED — the executable serves the interface,")
            print("  starts paper safely for replay and the default Upstox key,")
            print("  and creates its database in the external user-data directory.")
        else:
            print("  SMOKE TEST FAILED — do not ship this build.")
        return 0 if ok else 1

    print("\n  Run it with:")
    print(f"      {artifact}")
    print("  Then add --test next time to verify the artifact before shipping.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
