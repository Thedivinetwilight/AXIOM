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
import time
import urllib.error
import urllib.request
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


def build(onefile: bool) -> Path:
    ensure_pyinstaller()
    clean()
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
    """Start the built executable in a throwaway home and talk to it.

    This is the check that matters: a bundle can build cleanly and still be
    missing a module that only gets imported on the first HTTP request.
    """
    home = ROOT / "build" / "smoketest-home"
    if home.exists():
        shutil.rmtree(home, ignore_errors=True)
    home.mkdir(parents=True, exist_ok=True)

    port = "8099"
    env = dict(os.environ)
    env["ARENA_HOME"] = str(home)
    env["ARENA_PORT"] = port
    env.pop("DATABASE_URL", None)
    env.pop("DATA_DIR", None)

    print(f"\n  starting {artifact.name} on port {port}…", flush=True)
    process = subprocess.Popen(
        [str(artifact), "--no-browser"],
        cwd=str(home),  # deliberately NOT the app folder, to catch CWD bugs
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    base = f"http://127.0.0.1:{port}"
    results: dict[str, object] = {}
    try:
        for _ in range(120):  # up to ~60s: first launch unpacks the bundle
            if process.poll() is not None:
                print("  the executable exited early. Output:")
                print(process.stdout.read() if process.stdout else "")
                return False
            try:
                with urllib.request.urlopen(base + "/health", timeout=2) as response:
                    results["health"] = json.load(response)
                break
            except Exception:
                time.sleep(0.5)
        else:
            print("  timed out waiting for /health")
            return False

        for path in ("/setup", "/console", "/dashboard"):
            try:
                with urllib.request.urlopen(base + path, timeout=10) as response:
                    results[path] = response.status
            except Exception as exc:  # pragma: no cover
                results[path] = f"ERROR {exc}"

        print("\n  --- smoke test results ---")
        print(f"  /health    -> {results.get('health')}")
        for path in ("/setup", "/console", "/dashboard"):
            print(f"  {path:10s} -> {results.get(path)}")

        created = sorted(p.name for p in home.iterdir())
        print(f"  created in ARENA_HOME: {created}")
        database = home / "trading_bot.db"
        print(f"  database created outside the bundle: {database.exists()}")

        ok = (
            isinstance(results.get("health"), dict)
            and all(results.get(path) == 200 for path in ("/setup", "/console", "/dashboard"))
            and database.exists()
        )
        return ok
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()


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
            print("  creates its own data directory and writes its database there.")
        else:
            print("  SMOKE TEST FAILED — do not ship this build.")
        return 0 if ok else 1

    print("\n  Run it with:")
    print(f"      {artifact}")
    print("  Then add --test next time to verify the artifact before shipping.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
