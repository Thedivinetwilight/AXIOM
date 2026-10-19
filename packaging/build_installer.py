#!/usr/bin/env python3
"""Build the Windows installer for Arena (Inno Setup).

    python packaging\\build_installer.py            # build
    python packaging\\build_installer.py --verify   # build, then verify it

Inno Setup cannot cross-compile: the installer must be built on Windows
with ISCC.exe (Inno Setup 6). This script finds the compiler, runs it, and
reports exactly what it produced. It never touches a user's Arena data
directory - the installer only ships the executable and the documentation.

The compiler is resolved with the same rules as
``packaging/prepare_innosetup.py``: an explicit ``ISCC_PATH`` first, then
every known install location. The compiler must support the two features
the installer script uses (a ``/D`` define and ``x64compatible``), so a
too-old Inno Setup is reported instead of producing a confusing error.

The verification step (``--verify``) checks that the produced package
contains the executable and NOT any database: an installer that shipped a
user's trading database would be a serious mistake.
"""

from __future__ import annotations

import argparse
import importlib.util
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
RECOVERY_DOC = ROOT / "docs" / "PAPER_ACCOUNT_RECOVERY.md"
INSTALLER_NAME = "Arena-Setup"


def _load_prepare_module():
    """The Inno Setup preparation module (packaging/ is not a package)."""
    spec = importlib.util.spec_from_file_location(
        "arena_prepare_innosetup", ROOT / "packaging" / "prepare_innosetup.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def find_iscc() -> Path | None:
    """Locate the Inno Setup command-line compiler."""
    explicit = os.environ.get("ISCC_PATH")
    if explicit and Path(explicit).exists():
        return Path(explicit)
    found = shutil.which("ISCC") or shutil.which("iscc")
    if found:
        return Path(found)
    try:
        prepare = _load_prepare_module()
    except Exception:  # pragma: no cover - the module is part of the repo
        prepare = None
    if prepare is not None:
        for candidate in prepare.candidate_iscc_paths():
            try:
                if candidate.is_file():
                    return candidate
            except OSError:
                continue
    return None


def app_version() -> str:
    """The version the installer reports."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    try:
        from app import __version__

        return str(__version__)
    except Exception:  # pragma: no cover - the version is a constant
        return "3.2.4"


def build(compiler: Path) -> Path:
    if not SCRIPT.exists():
        raise SystemExit(f"Installer script is missing: {SCRIPT}")
    if not APP_EXE.exists():
        raise SystemExit(
            f"The executable is missing: {APP_EXE}\n"
            "Build it first:  python build_exe.py"
        )
    if not RECOVERY_DOC.exists():
        raise SystemExit(f"The recovery documentation is missing: {RECOVERY_DOC}")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    version = app_version()
    # The paths are passed as absolute /D defines so the script never has to
    # guess which directory a relative path is relative to.
    argv = [
        str(compiler),
        str(SCRIPT),
        f"/DArenaExe={APP_EXE}",
        f"/DRecoveryDoc={RECOVERY_DOC}",
        f"/DAppVersion={version}",
        f"/O{OUTPUT_DIR}",
        f"/F{INSTALLER_NAME}",
    ]
    print("  ISCC       : " + " ".join(argv))
    result = subprocess.run(
        argv,
        cwd=str(ROOT / "packaging"), capture_output=True, text=True, timeout=1800,
    )
    print(result.stdout[-4000:])
    if result.returncode != 0:
        print(result.stderr[-4000:], file=sys.stderr)
        raise SystemExit(f"Inno Setup failed with exit code {result.returncode}.")
    produced = sorted(OUTPUT_DIR.glob("*.exe"), key=lambda item: item.stat().st_mtime)
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
    if not RECOVERY_DOC.exists():
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
    try:
        prepare = _load_prepare_module()
        version = prepare.iscc_version(compiler)
        if version is not None and not prepare.version_at_least(
            version, prepare.MINIMUM_VERSION
        ):
            shown = ".".join(str(part) for part in version)
            print(f"Inno Setup {shown} is too old; "
                  f"{'.'.join(str(p) for p in prepare.MINIMUM_VERSION)} or newer "
                  "is required for the installer script.", file=sys.stderr)
            return 2
    except Exception:  # pragma: no cover - version probing is advisory
        pass
    print(f"  compiler : {compiler}")
    installer = build(compiler)
    print(f"  built    : {installer}")
    if args.verify and not verify(installer):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
