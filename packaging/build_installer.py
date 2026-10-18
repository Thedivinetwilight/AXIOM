#!/usr/bin/env python3
"""Build the Inno Setup installer from CI or local Windows environment.

Invoked from the GitHub Actions workflow and from build_windows.py.
Locates ISCC.exe, runs it with the correct arguments, and records the result.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging"
RELEASE = ROOT / "release"
ISS = PACKAGING / "installer.iss"


def find_iscc() -> Path | None:
    """Locate Inno Setup 6 compiler."""
    candidates = [
        Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
        Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
        Path.home() / "AppData" / "Local" / "Programs" / "Inno Setup 6" / "ISCC.exe",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def main() -> int:
    iscc = find_iscc()
    if iscc is None:
        print("ERROR: Inno Setup 6 not found. Install from https://jrsoftware.org/isinfo.php")
        return 1
    print(f"ISCC: {iscc}")

    # Copy exe to packaging dir (relative path for ISS)
    exe_src = RELEASE / "Arena.exe"
    exe_dst = PACKAGING / "Arena.exe"
    if not exe_src.exists():
        print(f"ERROR: {exe_src} not found. Build the executable first.")
        return 1
    shutil.copy2(exe_src, exe_dst)
    print(f"Copied {exe_src} -> {exe_dst}")

    # Get version
    sys.path.insert(0, str(ROOT))
    from app import __version__
    version = __version__
    print(f"Version: {version}")

    # Run ISCC with minimal script first for debugging
    minimal_iss = PACKAGING / "installer-minimal.iss"
    for iss_file in [minimal_iss, ISS]:
        argv = [
            str(iscc),
            str(iss_file),
            f"/DArenaExe=Arena.exe",
            f"/DAppVersion={version}",
            f"/O{RELEASE}",
            "/FAXIOM-Setup",
        ]
        print(f"\nRunning ISCC with {iss_file.name}:")
        print(f"  CWD: {PACKAGING}")
        print(f"  CMD: {' '.join(argv)}")
        result = subprocess.run(argv, cwd=str(PACKAGING))
        print(f"  Exit code: {result.returncode}")

        installer = RELEASE / "AXIOM-Setup.exe"
        if result.returncode == 0 and installer.exists():
            size = installer.stat().st_size
            digest = hashlib.sha256(installer.read_bytes()).hexdigest()
            (RELEASE / "SHA256SUMS-installer.txt").write_text(
                f"{digest}  AXIOM-Setup.exe\n", encoding="utf-8",
            )
            print(f"\nInstaller built successfully with {iss_file.name}!")
            print(f"  File:    {installer}")
            print(f"  Size:    {size:,} bytes ({size / 1024 / 1024:.1f} MB)")
            print(f"  SHA-256: {digest}")
            return 0
        else:
            print(f"  Failed with {iss_file.name}, trying next...")

    print("ERROR: All ISS scripts failed")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())