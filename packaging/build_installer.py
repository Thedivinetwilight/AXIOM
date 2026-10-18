#!/usr/bin/env python3
"""Build the Windows Inno Setup installer from a verified Arena.exe."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging"
RELEASE = ROOT / "release"
ISS = PACKAGING / "installer.iss"


def find_iscc() -> Path | None:
    """Locate Inno Setup 6 compiler on a Windows build machine."""
    candidates = [
        Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
        Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
        Path.home() / "AppData" / "Local" / "Programs" / "Inno Setup 6" / "ISCC.exe",
    ]
    return next((path for path in candidates if path.is_file()), None)


def main() -> int:
    iscc = find_iscc()
    if iscc is None:
        print("ERROR: Inno Setup 6 was not found on this machine.")
        return 1
    if not ISS.is_file():
        print(f"ERROR: installer script not found: {ISS}")
        return 1

    exe_src = RELEASE / "Arena.exe"
    exe_dst = PACKAGING / "Arena.exe"
    if not exe_src.is_file():
        print(f"ERROR: {exe_src} not found; build and verify Arena.exe first.")
        return 1

    RELEASE.mkdir(parents=True, exist_ok=True)
    shutil.copy2(exe_src, exe_dst)

    sys.path.insert(0, str(ROOT))
    try:
        from app import __version__
    finally:
        sys.path.pop(0)

    argv = [
        str(iscc),
        str(ISS),
        "/DArenaExe=Arena.exe",
        f"/DAppVersion={__version__}",
        f"/O{RELEASE}",
        "/FAXIOM-Setup",
    ]
    print(f"ISCC: {iscc}")
    print(f"Version: {__version__}")
    result = subprocess.run(argv, cwd=str(PACKAGING), check=False)
    installer = RELEASE / "AXIOM-Setup.exe"
    if result.returncode != 0 or not installer.is_file():
        print(f"ERROR: Inno Setup failed with exit code {result.returncode}")
        return result.returncode or 1

    digest = hashlib.sha256(installer.read_bytes()).hexdigest()
    size = installer.stat().st_size
    (RELEASE / "SHA256SUMS-installer.txt").write_text(
        f"{digest}  AXIOM-Setup.exe\n", encoding="utf-8"
    )
    print(f"Installer: {installer}")
    print(f"Size: {size:,} bytes ({size / 1024 / 1024:.1f} MB)")
    print(f"SHA-256: {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
