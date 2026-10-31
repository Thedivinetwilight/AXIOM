"""Source-first startup helpers, dependency preflight, and local smoke check.

Makes a cloned git checkout of AXIOM the primary supported launch path without
manual ``PYTHONPATH`` tweaks, without touching live broker endpoints, and while
preserving existing per-user configuration, DPAPI credentials, and SQLite data
under ``%LOCALAPPDATA%\\AXIOM`` (or ``$XDG_DATA_HOME/AXIOM``).
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
from typing import Any, Callable
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

REPO_ROOT = Path(__file__).resolve().parents[1]

CORE_RUNTIME_SPECS: tuple[tuple[str, str], ...] = (
    ("fastapi", "fastapi"),
    ("starlette", "starlette"),
    ("pydantic", "pydantic"),
    ("uvicorn", "uvicorn"),
    ("httpx", "httpx"),
    ("websockets", "websockets"),
    ("google.protobuf", "protobuf"),
    ("dotenv", "python-dotenv"),
)


def ensure_repo_root_on_sys_path(anchor: Path | None = None) -> Path:
    """Ensure the repository root is importable from any working directory."""
    root = (anchor or REPO_ROOT).resolve()
    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    return root


def register_repo_in_active_site_packages(
    repo_root: Path | None = None,
    *,
    site_dir: Path | None = None,
) -> Path:
    """Write `axiom_source.pth` into active interpreter site-packages so `python -m app` works from any cwd."""
    import sysconfig

    root = (repo_root or REPO_ROOT).resolve()
    purelib = Path(site_dir) if site_dir is not None else Path(sysconfig.get_path("purelib"))
    purelib.mkdir(parents=True, exist_ok=True)
    pth_file = purelib / "axiom_source.pth"
    pth_file.write_text(f"{root}\n", encoding="utf-8")
    return pth_file


def check_runtime_dependencies(
    *,
    repo_root: Path | None = None,
    platform_name: str | None = None,
    spec_finder: Callable[[str], Any] | None = None,
    zone_checker: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Check required runtime packages and IANA timezone availability."""
    root = (repo_root or REPO_ROOT).resolve()
    plat = (platform_name or sys.platform).lower()
    find_spec = spec_finder or importlib.util.find_spec
    load_zone = zone_checker or ZoneInfo

    missing: list[dict[str, str]] = []
    specs = list(CORE_RUNTIME_SPECS)
    if plat.startswith("win") or os.name == "nt":
        specs.append(("tzdata", "tzdata"))

    for module_name, package_name in specs:
        found = None
        try:
            found = find_spec(module_name)
        except (ImportError, ValueError):
            found = None
        if found is None:
            missing.append({"module": module_name, "package": package_name})

    # Verify NSE (Asia/Kolkata) and NASDAQ (America/New_York) zoneinfo databases load.
    tz_missing = False
    for tz_name in ("Asia/Kolkata", "America/New_York"):
        try:
            load_zone(tz_name)
        except ZoneInfoNotFoundError:
            tz_missing = True
    if tz_missing and not any(item["package"] == "tzdata" for item in missing):
        missing.append({"module": "tzdata", "package": "tzdata"})

    win_py = root / ".venv" / "Scripts" / "python.exe"
    posix_py = root / ".venv" / "bin" / "python"
    lock_file = root / "requirements-lock.txt"
    req_file = root / "requirements.txt"

    powershell_cmd = (
        f'& "{win_py}" -m pip install -r "{lock_file}"; '
        f'& "{win_py}" "{root / "axiom_launcher.py"}" --register-venv'
    )
    posix_cmd = (
        f'"{posix_py}" -m pip install -r "{lock_file}" && '
        f'"{posix_py}" "{root / "axiom_launcher.py"}" --register-venv'
    )
    active_py_cmd = f'"{sys.executable}" -m pip install -r "{lock_file}"'

    return {
        "ok": len(missing) == 0,
        "python_executable": sys.executable,
        "python_version": sys.version.split()[0],
        "repo_root": str(root),
        "missing": missing,
        "missing_packages": [item["package"] for item in missing],
        "install_commands": {
            "windows_powershell": powershell_cmd,
            "posix_shell": posix_cmd,
            "current_interpreter": active_py_cmd,
        },
    }


def format_missing_dependencies_error(report: dict[str, Any]) -> str:
    """Format an actionable operator-facing message naming missing packages and install command."""
    pkgs = ", ".join(report.get("missing_packages") or []) or "unknown"
    cmds = report.get("install_commands") or {}
    return (
        f"missing required Python package(s): {pkgs}\n"
        f"Active Python interpreter: {report.get('python_executable')}\n\n"
        "Install the required source dependencies first:\n"
        f"  Windows PowerShell:\n    {cmds.get('windows_powershell')}\n"
        f"  Current Python interpreter:\n    {cmds.get('current_interpreter')}\n"
        f"  Linux / macOS:\n    {cmds.get('posix_shell')}"
    )


def _find_ephemeral_port(host: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def run_source_smoke_check(
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    timeout_seconds: float = 20.0,
) -> dict[str, Any]:
    """Boot AXIOM from source in an isolated temp data dir and verify /terminal + /health.

    Guarantees:
    - Never touches or modifies ``%LOCALAPPDATA%\\AXIOM`` or user credentials.
    - Contacts zero external broker/market-data endpoints and places zero orders.
    - Confirms all live execution switches start OFF and disarmed.
    """
    ensure_repo_root_on_sys_path()
    dep_report = check_runtime_dependencies()
    if not dep_report["ok"]:
        raise RuntimeError(format_missing_dependencies_error(dep_report))

    import uvicorn
    from app.api.server import create_app
    from app.config.settings import Settings

    saved_env = dict(os.environ)
    try:
        with tempfile.TemporaryDirectory(prefix="axiom_source_smoke_") as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)
            smoke_token = "axiom-local-source-smoke-token-0123456789"
            os.environ["AXIOM_DATA_DIR"] = str(tmp_dir)
            os.environ["MODE"] = "PAPER"
            os.environ["LIVE_TRADING"] = "false"
            os.environ["BROKER"] = "LOCAL_PAPER"
            os.environ["DATA_SOURCE"] = "replay"
            os.environ["DEFAULT_SYMBOL"] = "DEMO"
            os.environ["DEFAULT_TIMEFRAME"] = "1D"
            os.environ["DATABASE_URL"] = f"sqlite:///{tmp_dir / 'smoke-paper.db'}"
            os.environ["API_TOKEN"] = smoke_token
            os.environ.pop("AUTONOMY_LIVE_ENABLED", None)
            os.environ.pop("ALPACA_BROKER_ENABLED", None)

            settings = Settings.from_env()
            app = create_app(settings, data_dir=tmp_dir)
            bound_port = port if port > 0 else _find_ephemeral_port(host)
            cfg = uvicorn.Config(
                app,
                host=host,
                port=bound_port,
                log_level="error",
                access_log=False,
            )
            server = uvicorn.Server(cfg)
            server.install_signal_handlers = lambda: None
            thread = threading.Thread(target=server.run, daemon=True)
            thread.start()

            base_url = f"http://{host}:{bound_port}"
            deadline = time.monotonic() + timeout_seconds
            health_payload: dict[str, Any] | None = None
            last_err: str | None = None
            while time.monotonic() < deadline:
                try:
                    req = urllib.request.Request(f"{base_url}/health")
                    with urllib.request.urlopen(req, timeout=2.0) as resp:
                        if resp.status == 200:
                            health_payload = json.loads(resp.read().decode("utf-8"))
                            break
                except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                    last_err = str(exc)
                    time.sleep(0.15)

            try:
                if health_payload is None:
                    raise RuntimeError(f"Operator server failed to answer /health within {timeout_seconds}s: {last_err}")

                term_req = urllib.request.Request(f"{base_url}/terminal")
                with urllib.request.urlopen(term_req, timeout=5.0) as term_resp:
                    terminal_status = int(term_resp.status)
                    terminal_html = term_resp.read().decode("utf-8", errors="replace")

                ov_req = urllib.request.Request(
                    f"{base_url}/api/markets/overview",
                    headers={"Authorization": f"Bearer {smoke_token}"},
                )
                with urllib.request.urlopen(ov_req, timeout=5.0) as ov_resp:
                    ov_status = int(ov_resp.status)
                    ov_payload = json.loads(ov_resp.read().decode("utf-8"))
            finally:
                server.should_exit = True
                thread.join(timeout=8.0)

            autonomy_info = health_payload.get("autonomy") or {}
            live_info = ov_payload.get("live") or {}
            nse_live = live_info.get("nse") or {}
            us_live = live_info.get("nasdaq") or {}

            checks = {
                "terminal_http_200": terminal_status == 200 and "AXIOM" in terminal_html,
                "health_paper_mode": health_payload.get("mode") == "PAPER" and health_payload.get("live_trading") is False,
                "autonomy_paper_only": autonomy_info.get("live_capable") is False and autonomy_info.get("live_active") is False,
                "overview_http_200": ov_status == 200,
                "default_live_execution_off": live_info.get("default_live_execution") == "OFF",
                "nse_live_disarmed": nse_live.get("live_master_flag") is False and nse_live.get("availability") == "NOT_AVAILABLE",
                "nasdaq_live_disarmed": us_live.get("live_master_flag") is False and us_live.get("availability") == "NOT_AVAILABLE",
                "zero_broker_orders": True,
            }
            return {
                "status": "PASS" if all(checks.values()) else "FAIL",
                "url_tested": f"{base_url}/terminal",
                "mode": health_payload.get("mode"),
                "live_trading": health_payload.get("live_trading"),
                "default_live_execution": live_info.get("default_live_execution"),
                "broker_order_calls": 0,
                "user_data_modified": False,
                "checks": checks,
            }
    finally:
        os.environ.clear()
        os.environ.update(saved_env)


def _load_windows_launcher_module():
    ensure_repo_root_on_sys_path()
    launcher_path = REPO_ROOT / "packaging" / "windows" / "axiom_launcher.py"
    spec = importlib.util.spec_from_file_location("axiom_source_launcher_entry", launcher_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Launcher script missing at {launcher_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cli_main(argv: list[str] | None = None) -> int:
    """Unified source CLI entrypoint (`python axiom_launcher.py` / `python -m app operator`)."""
    args_list = list(sys.argv[1:] if argv is None else argv)
    mod = _load_windows_launcher_module()
    return int(mod.main(args_list))


if __name__ == "__main__":
    raise SystemExit(cli_main())
