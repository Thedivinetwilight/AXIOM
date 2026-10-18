#!/usr/bin/env python3
"""Build the two Windows executables for the AXIOM terminal.

    AXIOM.exe          the desktop terminal (GUI + Arena client + Kronos manager)
    AXIOM-Kronos.exe   the Kronos runtime (PyTorch, Kronos, tokenizer/model)

They are built separately on purpose: the GUI process must stay small and must
never load PyTorch, while the Kronos process is allowed to be heavy because a
crash, an OOM or a long inference cannot take the terminal down.

Run this on Windows with separate build environments so the GUI bundle cannot
collect PyTorch from the Kronos environment. Install ``requirements.txt`` and
``requirements-desktop.txt`` plus PyInstaller into the desktop environment
(without PyTorch); install ``requirements-kronos.txt`` plus PyInstaller into the
worker environment. Run the builder from the worker environment:

    python tools/build_windows_executables.py --desktop-python <desktop-python.exe> --kronos-python <kronos-python.exe>

Model weights are NOT bundled or automatically downloaded by verified
inference. Stage the complete model/tokenizer artifacts under
%LOCALAPPDATA%\\AXIOM\\models\\kronos and verify them against the local
SHA-256 manifests before enabling inference.
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


def _has_pyinstaller(python_executable: str) -> bool:
    try:
        result = subprocess.run(
            [python_executable, "-m", "PyInstaller", "--version"],
            capture_output=True, text=True, check=False)
    except OSError:
        return False
    return result.returncode == 0


def _module_available(python_executable: str, module: str) -> bool | None:
    """Return module presence, or None if the target interpreter could not run."""
    probe = (
        "import importlib.util, sys; "
        "sys.exit(0 if importlib.util.find_spec(sys.argv[1]) is not None else 1)"
    )
    try:
        result = subprocess.run(
            [python_executable, "-c", probe, module],
            capture_output=True, text=True, check=False)
    except OSError:
        return None
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return None


def build(target: dict, *, python_executable: str, onedir: bool, clean: bool) -> int:
    command = [
        python_executable, "-m", "PyInstaller",
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
    """Resolve a module name to its package entry script.

    ``desktop`` is a package, not a top-level ``desktop.py`` file; PyInstaller
    must receive its ``__main__.py``. Plain leaf modules such as
    ``axiom.worker.child`` still resolve to their matching ``.py`` file.
    """
    module_path = ROOT / module.replace(".", os.sep)
    if module_path.is_dir():
        entry = module_path / "__main__.py"
        if not entry.is_file():
            raise FileNotFoundError(f"package entry point not found: {entry}")
        return str(entry)
    entry = module_path.with_suffix(".py")
    if not entry.is_file():
        raise FileNotFoundError(f"module entry point not found: {entry}")
    return str(entry)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--onedir", action="store_true",
                        help="build a folder instead of a single file")
    parser.add_argument("--clean", action="store_true", help="clear PyInstaller caches")
    parser.add_argument("--only", choices=[t["name"] for t in TARGETS],
                        help="build a single target")
    parser.add_argument(
        "--desktop-python", default=sys.executable,
        help="Python environment for AXIOM.exe (install PySide6, but not PyTorch)")
    parser.add_argument(
        "--kronos-python", default=sys.executable,
        help="Python environment for AXIOM-Kronos.exe (install the Kronos dependencies)")
    args = parser.parse_args(argv)

    if os.name != "nt":  # informational only; the build itself needs Windows
        print("warning: AXIOM.exe/AXIOM-Kronos.exe are Windows targets; "
              "building on this platform produces a native binary instead.")
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller is required:  pip install pyinstaller", file=sys.stderr)
        return 2

    selected = [target for target in TARGETS
                if not args.only or target["name"] == args.only]
    interpreters = {
        "AXIOM": args.desktop_python,
        "AXIOM-Kronos": args.kronos_python,
    }
    for target in selected:
        python_executable = interpreters[target["name"]]
        if not _has_pyinstaller(python_executable):
            print(
                f"PyInstaller is required in the {target['name']} build "
                f"environment ({python_executable})",
                file=sys.stderr,
            )
            return 2
        if not target["heavy"]:
            torch_available = _module_available(python_executable, "torch")
            if torch_available is not False:
                reason = ("PyTorch is installed" if torch_available else
                          "the target Python environment could not be inspected")
                print(
                    f"Refusing to build AXIOM.exe: the GUI environment must be "
                    f"PyTorch-free ({reason}; {python_executable})",
                    file=sys.stderr,
                )
                return 2

    DIST.mkdir(parents=True, exist_ok=True)
    failures = []
    for target in selected:
        print(f"\n=== {target['name']} ({target['module']}) ===")
        python_executable = interpreters[target["name"]]
        code = build(target, python_executable=python_executable,
                     onedir=args.onedir, clean=args.clean)
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
