#!/usr/bin/env python3
"""Build AXIOM Arena for Windows: executable + Inno Setup installer.

Run this script on a Windows machine with Python 3.11+ installed:

    python build_windows.py              # build executable only
    python build_windows.py --installer  # build executable + Inno Setup installer
    python build_windows.py --test       # build + verify the executable
    python build_windows.py --all        # build + test + installer

Prerequisites:
    - Python 3.11+ (64-bit)
    - pip (included with Python)
    - Inno Setup 6 (only for --installer): https://jrsoftware.org/isinfo.php

The script:
    1. Installs dependencies from requirements.txt
    2. Installs PyInstaller
    3. Builds Arena.exe using the existing spec
    4. Runs the frozen E2E smoke test (optional)
    5. Builds the Inno Setup installer (optional)
    6. Records SHA-256 checksums and build metadata

Safety: This build defaults to MODE=PAPER, BROKER=LOCAL_PAPER, LIVE_TRADING=false.
No production credentials are used or required.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
RELEASE = ROOT / "release"
SPEC = ROOT / "packaging" / "arena.spec"
ISS = ROOT / "packaging" / "installer.iss"


def run(argv: list[str], **kwargs) -> int:
    """Run a command, printing it first."""
    print(f"  $ {' '.join(argv)}", flush=True)
    return subprocess.call(argv, cwd=str(ROOT), **kwargs)


def check_prerequisites() -> bool:
    """Verify we're on Windows with Python 3.11+."""
    if sys.platform != "win32":
        print("ERROR: This script must be run on Windows.")
        print("  PyInstaller cannot cross-compile — a Windows .exe must be")
        print("  built on Windows. Run this script on a Windows machine.")
        return False

    if sys.version_info < (3, 11):
        print(f"ERROR: Python 3.11+ required, found {sys.version}")
        return False

    if not sys.maxsize > 2**32:
        print("ERROR: 64-bit Python required.")
        return False

    print(f"  Platform: Windows {sys.platform}")
    print(f"  Python:   {sys.version.split()[0]} ({'64-bit'})")
    return True


def install_dependencies() -> bool:
    """Install project dependencies and PyInstaller."""
    print("\n[1/5] Installing dependencies...")
    req = ROOT / "requirements.txt"
    if not req.exists():
        print(f"ERROR: {req} not found")
        return False

    code = run([sys.executable, "-m", "pip", "install", "--upgrade", "pip"])
    if code != 0:
        print("WARNING: pip upgrade failed, continuing with current pip")

    code = run([sys.executable, "-m", "pip", "install", "-r", str(req)])
    if code != 0:
        print("ERROR: Failed to install requirements")
        return False

    code = run([sys.executable, "-m", "pip", "install", "pyinstaller"])
    if code != 0:
        print("ERROR: Failed to install PyInstaller")
        return False

    print("  Dependencies installed successfully.")
    return True


def write_build_meta() -> Path:
    """Record the exact commit/version for the build."""
    sys.path.insert(0, str(ROOT))
    try:
        from app import __version__
    finally:
        sys.path.pop(0)

    commit = ""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=10,
        )
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
    print(f"  Build meta: version={__version__}, commit={commit[:12] if commit else 'unknown'}")
    return path


def build_executable() -> Path | None:
    """Build Arena.exe using PyInstaller."""
    print("\n[2/5] Building Arena.exe...")

    # Clean previous build
    for path in (DIST, ROOT / "build"):
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)

    write_build_meta()

    code = run([
        sys.executable, "-m", "PyInstaller",
        str(SPEC),
        "--noconfirm",
        "--distpath", str(DIST),
        "--workpath", str(ROOT / "build"),
    ])

    exe = DIST / "Arena.exe"
    if code != 0 or not exe.exists():
        print(f"ERROR: Build failed (exit code {code})")
        if exe.exists():
            print(f"  But {exe} exists — it may be from a previous build")
        return None

    size_mb = exe.stat().st_size / (1024 * 1024)
    print(f"  Built: {exe} ({size_mb:.1f} MB)")
    return exe


def run_smoke_test(exe: Path) -> bool:
    """Run the frozen E2E smoke test against the built executable."""
    print("\n[3/5] Running frozen E2E smoke test...")
    print("  This starts Arena.exe, drives the wizard, verifies paper trading,")
    print("  tests restart recovery, and checks secret redaction.")
    print("  (May take 2-3 minutes)")

    # Import and run the frozen E2E module
    import importlib.util

    e2e_path = ROOT / "packaging" / "frozen_e2e.py"
    spec = importlib.util.spec_from_file_location("arena_frozen_e2e", e2e_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    workdir = ROOT / "build" / "e2e-workdir"
    result = module.run_frozen_e2e(exe, workdir, verbose=True)

    failed = [r for r in result["results"] if not r["ok"]]
    passed = [r for r in result["results"] if r["ok"]]

    print(f"\n  Results: {len(passed)} passed, {len(failed)} failed")

    if failed:
        print("\n  FAILED CHECKS:")
        for entry in failed:
            print(f"    - {entry['name']}: {entry['detail']}")

        # Distinguish network failures from real failures
        network_fails = [f for f in failed if "dns_or_connect" in str(f.get("detail", ""))]
        code_fails = [f for f in failed if f not in network_fails]

        if code_fails:
            print(f"\n  {len(code_fails)} CODE FAILURES — do not ship this build.")
            return False
        elif network_fails:
            print(f"\n  {len(network_fails)} network-only failures (no egress to api.upstox.com).")
            print("  All code checks passed. Build is safe for paper trading.")
            return True

    print("  All checks passed.")
    return True


def create_release(exe: Path) -> dict:
    """Create release artifacts with checksums."""
    print("\n[4/5] Creating release artifacts...")
    RELEASE.mkdir(exist_ok=True)

    target = RELEASE / "Arena.exe"
    target.write_bytes(exe.read_bytes())

    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    size_bytes = target.stat().st_size

    # SHA-256 checksum
    checksum_file = RELEASE / "SHA256SUMS-windows-x64.txt"
    checksum_file.write_text(f"{digest}  Arena.exe\n", encoding="utf-8")

    # Build info
    meta_path = ROOT / "app" / "BUILD_META.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    build_id = f"{meta.get('version', 'unknown')}-{meta.get('commit', 'unknown')[:10]}"

    record = {
        "artifact": "Arena.exe",
        "version": meta.get("version", "unknown"),
        "build_id": build_id,
        "commit": meta.get("commit", "unknown"),
        "built_at": meta.get("built_at", "unknown"),
        "platform": meta.get("platform", sys.platform),
        "size_bytes": size_bytes,
        "sha256": digest,
    }
    record_path = RELEASE / "Arena.exe.build-info.json"
    record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")

    print(f"  Release directory: {RELEASE}")
    print(f"  Arena.exe: {size_bytes:,} bytes ({size_bytes / 1024 / 1024:.1f} MB)")
    print(f"  SHA-256:   {digest}")
    print(f"  Build ID:  {build_id}")

    return record


def build_installer(exe: Path) -> Path | None:
    """Build the Inno Setup installer."""
    print("\n[5/5] Building Inno Setup installer...")

    if not ISS.exists():
        print(f"ERROR: Installer script not found: {ISS}")
        return None

    # Find Inno Setup compiler
    iscc_candidates = [
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Inno Setup 6" / "ISCC.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Inno Setup 6" / "ISCC.exe",
        Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
        Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
    ]

    iscc = None
    for candidate in iscc_candidates:
        if candidate.exists():
            iscc = candidate
            break

    if iscc is None:
        print("WARNING: Inno Setup 6 not found.")
        print("  Install it from: https://jrsoftware.org/isinfo.php")
        print("  Then re-run this script with --installer")
        print(f"  The installer script is at: {ISS}")
        return None

    # Copy exe to packaging directory for the installer
    pkg_exe = ROOT / "packaging" / "Arena.exe"
    shutil.copy2(exe, pkg_exe)

    # Get version
    sys.path.insert(0, str(ROOT))
    try:
        from app import __version__
    finally:
        sys.path.pop(0)

    # Run Inno Setup
    code = run([
        str(iscc),
        str(ISS),
        f"/DArenaExe={pkg_exe}",
        f"/DAppVersion={__version__}",
        f"/O{RELEASE}",
        "/FAXIOM-Setup",
    ])

    # Clean up the copy
    pkg_exe.unlink(missing_ok=True)

    installer = RELEASE / "AXIOM-Setup.exe"
    if code != 0 or not installer.exists():
        print(f"ERROR: Installer build failed (exit code {code})")
        return None

    installer_size = installer.stat().st_size
    installer_hash = hashlib.sha256(installer.read_bytes()).hexdigest()

    # Record installer checksum
    (RELEASE / "SHA256SUMS-installer.txt").write_text(
        f"{installer_hash}  AXIOM-Setup.exe\n", encoding="utf-8",
    )

    print(f"  Installer: {installer}")
    print(f"  Size:      {installer_size:,} bytes ({installer_size / 1024 / 1024:.1f} MB)")
    print(f"  SHA-256:   {installer_hash}")

    return installer


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build AXIOM Arena for Windows: executable + installer",
    )
    parser.add_argument("--test", action="store_true",
                        help="Run the frozen E2E smoke test")
    parser.add_argument("--installer", action="store_true",
                        help="Build the Inno Setup installer")
    parser.add_argument("--all", action="store_true",
                        help="Build + test + installer (equivalent to --test --installer)")
    args = parser.parse_args()

    if args.all:
        args.test = True
        args.installer = True

    print("=" * 60)
    print("  AXIOM Arena — Windows Build")
    print("=" * 60)
    print(f"  Date:     {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print()

    if not check_prerequisites():
        return 1

    if not install_dependencies():
        return 1

    exe = build_executable()
    if exe is None:
        return 1

    if args.test:
        ok = run_smoke_test(exe)
        if not ok:
            print("\n  SMOKE TEST FAILED — do not ship this build.")
            return 1

    record = create_release(exe)

    if args.installer:
        installer = build_installer(exe)
        if installer is None:
            print("\n  Installer build failed or Inno Setup not found.")
            print("  The executable is still available in release/")
            return 1

    print("\n" + "=" * 60)
    print("  BUILD COMPLETE")
    print("=" * 60)
    print(f"  Source commit: {record['commit']}")
    print(f"  Version:       {record['version']}")
    print(f"  Build ID:      {record['build_id']}")
    print(f"  Executable:    release/Arena.exe ({record['size_bytes'] / 1024 / 1024:.1f} MB)")
    print(f"  SHA-256:       {record['sha256']}")
    if args.installer and (RELEASE / "AXIOM-Setup.exe").exists():
        sz = (RELEASE / "AXIOM-Setup.exe").stat().st_size
        print(f"  Installer:     release/AXIOM-Setup.exe ({sz / 1024 / 1024:.1f} MB)")
    print()
    print("  Safety: MODE=PAPER, BROKER=LOCAL_PAPER, LIVE_TRADING=false")
    print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())