"""Install and exercise the actual Inno Setup package on Windows.

The packaged executable is not the whole install story: a user gets the
INNO SETUP package. This script installs it silently into a throwaway
directory, checks the installed layout and its safety defaults, drives the
INSTALLED executable through the paper-account startup failure and its
recovery, and finally uninstalls it again to prove that the user's data
(database, recovery database, recovery selection) survives.

Run on Windows only:

    python packaging\\installer_e2e.py dist\\installer\\AXIOM-Setup.exe build\\installer-e2e
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    # Running `python packaging/installer_e2e.py` puts only packaging/ at
    # sys.path[0]. The installer test imports app.config.envfile, so make the
    # checkout root explicit rather than relying on cwd/PYTHONPATH behaviour.
    sys.path.insert(0, str(ROOT))


def _run_recovery_e2e(executable: Path, workdir: Path) -> dict:
    spec = importlib.util.spec_from_file_location(
        "arena_installed_recovery_e2e", ROOT / "packaging" / "frozen_recovery_e2e.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.run_frozen_recovery_e2e(executable, workdir)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print("usage: python packaging/installer_e2e.py <AXIOM-Setup.exe> <workdir>")
        return 2
    if sys.platform != "win32":
        print("ERROR: the installer E2E must run on Windows.")
        return 2

    installer = Path(args[0]).resolve()
    workdir = Path(args[1]).resolve()
    if not installer.is_file():
        print(f"ERROR: installer not found: {installer}")
        return 2
    if workdir.exists():
        shutil.rmtree(workdir, ignore_errors=True)
    install_dir = workdir / "installed-app"
    test_dir = workdir / "recovery-test"
    install_dir.mkdir(parents=True, exist_ok=True)

    installed = subprocess.run(
        [
            str(installer),
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            f"/DIR={install_dir}",
        ],
        cwd=str(workdir),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
        check=False,
    )
    if installed.returncode != 0:
        print(f"ERROR: silent installer exit={installed.returncode}")
        print((installed.stdout + installed.stderr)[-2000:])
        return 1

    executable = install_dir / "Arena.exe"
    env_file = install_dir / ".env"
    if not executable.is_file() or not env_file.is_file():
        print("ERROR: the installer did not install Arena.exe and its safe fallback .env")
        return 1

    # Validate only safety keys; never print the file or any other value.
    from app.config.envfile import read_values

    values = read_values(env_file)
    safe_defaults = (
        values.get("MODE") == "PAPER"
        and values.get("LIVE_TRADING", "").lower() == "false"
        and values.get("BROKER") == "LOCAL_PAPER"
    )
    if not safe_defaults:
        print("ERROR: the installed dotenv safety defaults are not "
              "PAPER / false / LOCAL_PAPER")
        return 1
    print("PASS the installer wrote PAPER / LIVE_TRADING=false / LOCAL_PAPER fallbacks")

    if not (install_dir / "PAPER_ACCOUNT_RECOVERY.md").is_file():
        print("ERROR: the recovery documentation was not installed")
        return 1
    print("PASS the recovery documentation is installed beside the executable")

    try:
        result = _run_recovery_e2e(executable, test_dir)
    except Exception as exc:
        print(f"ERROR: the installed executable E2E failed ({type(exc).__name__})")
        return 1
    for entry in result.get("results", []):
        print(("PASS " if entry["ok"] else "FAIL ") + entry["name"]
              + (f" — {entry['detail']}" if entry["detail"] else ""))
    if not result.get("ok"):
        print("ERROR: the installed executable recovery E2E failed")
        return 1

    recovery_db = Path(result.get("recovery_database", ""))
    original_db = Path(result.get("original_database", ""))
    uninstaller = install_dir / "unins000.exe"
    if not uninstaller.is_file():
        print("ERROR: the installer did not register an uninstaller")
        return 1
    removed = subprocess.run(
        [str(uninstaller), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
        cwd=str(workdir),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
        check=False,
    )
    if removed.returncode != 0:
        print(f"ERROR: silent uninstaller exit={removed.returncode}")
        return 1

    pointer = test_dir / "home" / "paper-database-override.json"
    preserved = original_db.is_file() and recovery_db.is_file() and pointer.is_file()
    print("PASS the uninstall preserves the original database, the recovery "
          f"database and the recovery selection: {preserved}")
    print(json.dumps({"ok": preserved, "installed_executable": str(executable)},
                     indent=2))
    return 0 if preserved else 1


if __name__ == "__main__":
    raise SystemExit(main())
