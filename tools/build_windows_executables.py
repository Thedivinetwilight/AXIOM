#!/usr/bin/env python3
"""Build the two Windows executables for the AXIOM terminal.

    AXIOM.exe          the desktop terminal (GUI + Arena client + Kronos manager)
    AXIOM-Kronos.exe   the Kronos runtime (PyTorch, Kronos, tokenizer/model)

They are built separately on purpose: the GUI process must stay small and must
never load PyTorch, while the Kronos process is allowed to be heavy because a
crash, an OOM or a long inference cannot take the terminal down.

Run this on Windows inside the Kronos virtual environment:

    .venv-kronos\\Scripts\\activate.bat
    pip install pyinstaller
    python tools\\build_windows_executables.py

Model weights are NOT bundled. They are downloaded on first load into
%LOCALAPPDATA%\\AXIOM\\models\\kronos and verified against the SHA-256 manifest.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"

# name, entry module, hidden imports, heavy runtime?
TARGETS = (
    {
        "name": "AXIOM",
        "module": "desktop",
        "hidden": ["desktop.app", "desktop.api_client", "desktop.viewmodels",
                   "desktop.forecast_controller", "desktop.forecast_viewmodels",
                   "axiom.bootstrap", "axiom.service", "axiom.worker.manager",
                   "axiom.worker.protocol"],
        "heavy": False,
    },
    {
        "name": "AXIOM-Kronos",
        "module": "axiom.worker.child",
        "hidden": ["axiom.worker.child", "axiom.kronos.backends",
                   "axiom.kronos.runtime", "axiom.kronos.model_cache",
                   "axiom.market.provider", "axiom.market.validation"],
        "heavy": True,
    },
)


def build(target: dict, *, onedir: bool, clean: bool) -> int:
    command = [
        sys.executable, "-m", "PyInstaller",
        "--name", target["name"],
        "--noconfirm",
        "--console" if target["name"] == "AXIOM-Kronos" else "--windowed",
        "--onedir" if onedir else "--onefile",
        "--paths", str(ROOT),
        "--distpath", str(DIST),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
    ]
    if clean:
        command.append("--clean")
    for hidden in target["hidden"]:
        command += ["--hidden-import", hidden]
    # The Kronos implementation itself lives OUTSIDE the bundle and is resolved
    # at runtime through AXIOM_KRONOS_SOURCE (it is not a PyPI package).
    command.append(str(ROOT / _entry_point(target["module"])))
    print("·", " ".join(command))
    return subprocess.call(command, cwd=str(ROOT), env=os.environ.copy())


def _entry_point(module: str) -> str:
    return module.replace(".", os.sep) + ".py"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--onedir", action="store_true",
                        help="build a folder instead of a single file")
    parser.add_argument("--clean", action="store_true", help="clear PyInstaller caches")
    parser.add_argument("--only", choices=[t["name"] for t in TARGETS],
                        help="build a single target")
    args = parser.parse_args(argv)

    if os.name != "nt":  # informational only; the build itself needs Windows
        print("warning: AXIOM.exe/AXIOM-Kronos.exe are Windows targets; "
              "building on this platform produces a native binary instead.")
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller is required:  pip install pyinstaller", file=sys.stderr)
        return 2

    DIST.mkdir(parents=True, exist_ok=True)
    failures = []
    for target in TARGETS:
        if args.only and target["name"] != args.only:
            continue
        print(f"\n=== {target['name']} ({target['module']}) ===")
        code = build(target, onedir=args.onedir, clean=args.clean)
        if code != 0:
            failures.append(target["name"])
    if failures:
        print("\nfailed targets: " + ", ".join(failures), file=sys.stderr)
        return 1
    print(f"\nbuilt into {DIST}")
    for path in sorted(DIST.iterdir()):
        print(" -", path.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
