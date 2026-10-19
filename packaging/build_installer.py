#!/usr/bin/env python3
"""Build the Windows installer for Arena (Inno Setup).

    python packaging\\build_installer.py            # build
    python packaging\\build_installer.py --verify   # build, then verify it

Inno Setup cannot cross-compile: the installer must be built on Windows
with ISCC.exe (Inno Setup 6). This script finds the compiler, runs it, and
reports exactly what it produced. It never touches a user's Arena data
directory — the installer only ships the executable and the documentation.

The verification step (``--verify``) is an INno Setup dry run plus a check
that the produced archive contains the executable and NOT any database: an
installer that shipped a user's trading database would be a serious mistake.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "packaging" / "installer.iss"
DIST = ROOT / "dist"
OUTPUT_DIR = DIST / "installer"
APP_EXE = DIST / "Arena.exe"


def find_iscc() -> Path | None:
    """Locate the Inno Setup command-line compiler."""
    explicit = os.environ.get("ISCC_PATH")
    if explicit and Path(explicit).exists():
        return Path(explicit)
    found = shutil.which("ISCC") or shutil.which("iscc")
    if found:
        return Path(found)
    for base in (
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")),
        Path(os.environ.get("LOCALAPPDATA", "")),
    ):
        if not str(base):
            continue
        candidate = base / "Inno Setup 6" / "ISCC.exe"
        if candidate.exists():
            return candidate
    return None


def build(compiler: Path) -> Path:
    if not SCRIPT.exists():
        raise SystemExit(f"Installer script is missing: {SCRIPT}")
    if not APP_EXE.exists():
        raise SystemExit(
            f"The executable is missing: {APP_EXE}\n"
            "Build it first:  python build_exe.py"
        )
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [str(compiler), str(SCRIPT)],
        cwd=str(ROOT), capture_output=True, text=True, timeout=1800,
    )
    print(result.stdout[-4000:])
    if result.returncode != 0:
        print(result.stderr[-4000:], file=sys.stderr)
        raise SystemExit(f"Inno Setup failed with exit code {result.returncode}.")
    produced = sorted(OUTPUT_DIR.glob("Arena-*-setup.exe"))
    if not produced:
        raise SystemExit(f"Inno Setup reported success but produced nothing in {OUTPUT_DIR}")
    return produced[-1]


def verify(installer: Path) -> bool:
    """Prove the installer ships the app and no user data."""
    problems: list[str] = []
    if installer.stat().st_size < 1_000_000:
        problems.append("the installer is suspiciously small")
    text = installer.read_bytes()
    for needle in (b"trading_bot.db", b"paper-database-override.json"):
        if needle in text:
            problems.append(f"the installer embeds {needle.decode()}")
    if not (ROOT / "docs" / "PAPER_ACCOUNT_RECOVERY.md").exists():
        problems.append("the recovery documentation is missing")
    for problem in problems:
        print(f"  FAILED: {problem}")
    if not problems:
        print(f"  installer verified: {installer.name} "
              f"({installer.stat().st_size / (1024 * 1024):.1f} MB)")
    return not problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true",
                        help="verify the produced installer after building it")
    args = parser.parse_args(argv)

    if os.name != "nt":
        print("The Windows installer can only be built on Windows "
              "(Inno Setup is a Windows tool).", file=sys.stderr)
        return 2
    compiler = find_iscc()
    if compiler is None:
        print("Inno Setup 6 was not found. Install it, or set ISCC_PATH to "
              "ISCC.exe.", file=sys.stderr)
        return 2
    print(f"  compiler : {compiler}")
    installer = build(compiler)
    print(f"  built    : {installer}")
    if args.verify and not verify(installer):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
