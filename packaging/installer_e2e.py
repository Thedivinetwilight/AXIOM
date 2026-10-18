"""Install and exercise the actual Inno Setup package on Windows CI."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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
        print("ERROR: Installer E2E must run on Windows.")
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
        timeout=180,
        check=False,
    )
    if installed.returncode != 0:
        print(f"ERROR: silent installer exit={installed.returncode}")
        print((installed.stdout + installed.stderr)[-2000:])
        return 1

    executable = install_dir / "Arena.exe"
    env_file = install_dir / ".env"
    if not executable.is_file() or not env_file.is_file():
        print("ERROR: installer did not install Arena.exe and its safe fallback .env")
        return 1

    # Validate only safety keys; never print the file or any other values.
    from app.config.envfile import read_values

    values = read_values(env_file)
    safe_defaults = (
        values.get("MODE") == "PAPER"
        and values.get("LIVE_TRADING", "").lower() == "false"
        and values.get("BROKER") == "LOCAL_PAPER"
    )
    if not safe_defaults:
        print("ERROR: installed dotenv safety defaults are not PAPER/false/LOCAL_PAPER")
        return 1
    print("PASS installer wrote PAPER / LIVE_TRADING=false / LOCAL_PAPER fallback")

    try:
        result = _run_recovery_e2e(executable, test_dir)
    except Exception as exc:
        print(f"ERROR: installed executable E2E failed ({type(exc).__name__})")
        return 1
    if not result.get("ok"):
        print("ERROR: installed executable recovery E2E failed")
        return 1

    recovery_db = Path(result["recovery_database"])
    original_db = Path(result["original_database"])
    uninstaller = install_dir / "unins000.exe"
    if not uninstaller.is_file():
        print("ERROR: installer did not register an uninstaller")
        return 1
    removed = subprocess.run(
        [str(uninstaller), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
        cwd=str(workdir),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )
    if removed.returncode != 0:
        print(f"ERROR: silent uninstaller exit={removed.returncode}")
        return 1

    pointer = test_dir / "user-home" / "paper-database-override.json"
    preserved = original_db.is_file() and recovery_db.is_file() and pointer.is_file()
    print(
        "PASS uninstall preserves the user-home original DB, recovery DB, and "
        f"selection pointer: {preserved}"
    )
    print(json.dumps({"ok": preserved, "installed_executable": str(executable)}, indent=2))
    return 0 if preserved else 1


if __name__ == "__main__":
    raise SystemExit(main())
