"""AXIOM operator - packaged Windows launcher.

Writable configuration, SQLite state, logs and DPAPI blobs live in the per-user
data directory and survive upgrades. The legacy engine configuration remains
hard-pinned to local paper; NSE (Upstox) and NASDAQ (Alpaca) live execution
gates always start disarmed and OFF by default. The launcher opens the
authenticated operator shell on loopback and never prints the local API token
or broker credentials.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

if not getattr(sys, "frozen", False):
    _SCRIPT_DIR = Path(__file__).resolve().parent
    _REPO_ROOT = _SCRIPT_DIR.parents[1]
    if str(_SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(_SCRIPT_DIR))
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))

APP_NAME = "AXIOM"
APP_TITLE = "AXIOM Operator"
BUILD_LABEL = "local paper + Upstox (NSE) & Alpaca (NASDAQ)"
DEFAULT_PORT = 8000
TOKEN_ENV = "API_TOKEN"
LOOPBACK = "127.0.0.1"

CONFIG_TEMPLATE = """\
# AXIOM configuration for this installation.
# This file lives in your user data folder, NOT in the application folder:
# upgrading or reinstalling AXIOM never overwrites or deletes it.
# Keep it private. It can contain a local API token and market-data keys.

# --- legacy engine safety switches (do not change) ---
# Upstox (NSE) and Alpaca (NASDAQ) credentials are stored separately in
# Windows DPAPI via the operator UI and always start disarmed.
MODE=PAPER
LIVE_TRADING=false
BROKER=LOCAL_PAPER

# --- paper account ---
INITIAL_CAPITAL=100000
CURRENCY=INR

# --- what the chart draws by default ---
# replay = the bundled DEMO sample data. It is NOT market data and the chart
# labels it as such. Change DATA_SOURCE only after reading the manual.
DATA_SOURCE=replay
DEFAULT_SYMBOL=DEMO
DEFAULT_TIMEFRAME=1D

# --- local API token (generated on first run; paste it when the chart asks) ---
API_TOKEN={token}

# --- US equities paper mode (read-only Alpaca market data, simulated fills) ---
# To enable, uncomment and fill in every line, including all five limits, and
# put your own market-DATA key and secret here. Never share this file.
# MARKET=us
# CURRENCY=USD
# MARKET_CALENDAR=us-equities
# DATA_SOURCE=alpaca
# DEFAULT_SYMBOL=AAPL
# DEFAULT_TIMEFRAME=1m
# ALPACA_API_KEY=
# ALPACA_API_SECRET=
# ALPACA_DATA_FEED=iex
# ALPACA_SYMBOLS=AAPL,MSFT
# PAPER_MAX_POSITION_VALUE=1000
# PAPER_MAX_OPEN_POSITIONS=2
# PAPER_MAX_DAILY_LOSS=100
# PAPER_MAX_DRAWDOWN=0.05
# PAPER_MAX_TRADES_PER_DAY=4
"""

BANNER = r"""
  ___  _  _ ___  __  __
 / _ \| || |_ _| \ \/ /   {title}
| (_) | __ || |   >  <    {label}
 \___/|_||_|___| /_/\_\   PAPER DEFAULT - LIVE GATES DISARMED

"""


class LaunchError(RuntimeError):
    """A refusal the operator can act on, printed without a traceback."""


def user_data_dir() -> Path:
    """Per-user, per-application data directory (survives install/upgrade)."""
    override = os.environ.get("AXIOM_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_NAME


def config_path(data_dir: Path) -> Path:
    return data_dir / "axiom.env"


def ensure_config(data_dir: Path) -> tuple[Path, bool]:
    """Create the config on first run. Returns (path, created)."""
    path = config_path(data_dir)
    if path.exists():
        return path, False
    import secrets

    token = secrets.token_urlsafe(24)
    path.write_text(CONFIG_TEMPLATE.format(token=token), encoding="utf-8")
    try:  # best effort: the file holds a local secret
        path.chmod(0o600)
    except OSError:
        pass
    return path, True


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip().upper()] = value.strip()
    return values


def load_configuration(data_dir: Path) -> dict[str, str]:
    """Load the user config into the environment, refusing unsafe settings."""
    config, created = ensure_config(data_dir)
    try:
        values = _read_env_file(config)
    except OSError as exc:
        raise LaunchError(
            f"cannot read your configuration file {config}: {exc}\n"
            "Fix or delete that file and start AXIOM again."
        ) from exc
    except UnicodeDecodeError as exc:
        raise LaunchError(
            f"your configuration file {config} is not valid UTF-8 text.\n"
            "Delete it (a fresh one with a new token is created) or repair it."
        ) from exc

    mode = values.get("MODE", "PAPER").upper()
    live = values.get("LIVE_TRADING", "false").lower()
    broker = values.get("BROKER", "LOCAL_PAPER").upper()
    if mode != "PAPER" or live not in {"false", "0", "no", "off"} or broker != "LOCAL_PAPER":
        raise LaunchError(
            "the legacy engine requires paper-only process settings; refusing:\n"
            f"  MODE={mode}  LIVE_TRADING={live}  BROKER={broker}\n"
            "Set MODE=PAPER, LIVE_TRADING=false and BROKER=LOCAL_PAPER in\n"
            f"  {config}\n"
            "No order was sent anywhere; nothing was changed for you."
        )

    # Defaults that keep all state inside the per-user folder. Explicit values
    # in the config (or the environment) still win.
    values.setdefault("DATABASE_URL", f"sqlite:///{data_dir / 'axiom-paper.db'}")
    if not values.get("API_TOKEN"):
        raise LaunchError(
            f"API_TOKEN is empty in {config}.\n"
            "Delete the file to have a fresh token generated, or set one yourself."
        )
    for key, value in values.items():
        os.environ.setdefault(key, value)
    os.environ["MODE"] = "PAPER"
    os.environ["LIVE_TRADING"] = "false"
    os.environ["BROKER"] = "LOCAL_PAPER"
    return {
        "config": str(config),
        "created": "yes" if created else "no",
        "database": os.environ.get("DATABASE_URL", ""),
        "data_source": os.environ.get("DATA_SOURCE", "replay"),
    }


def free_port(preferred: int) -> int:
    for candidate in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind((LOOPBACK, candidate))
            except OSError:
                continue
            return candidate
    raise LaunchError(
        f"no free local port between {preferred} and {preferred + 19}.\n"
        "Close whatever is using them, or set AXIOM_PORT to another value."
    )


def build_app():
    """Construct the same application the test suite uses."""
    if not getattr(sys, "frozen", False):
        from app.source_launcher import check_runtime_dependencies, format_missing_dependencies_error

        dep_report = check_runtime_dependencies()
        if not dep_report["ok"]:
            raise LaunchError(format_missing_dependencies_error(dep_report))
    try:
        from app.api.server import create_app
        from app.config.settings import Settings
    except Exception as exc:  # a broken bundle must say so, not traceback
        raise LaunchError(f"the packaged application is incomplete: {exc}") from exc
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        raise LaunchError(
            "configuration refused:\n  " + str(exc) + "\n"
            "See the configuration section of the manual."
        ) from exc
    if settings.mode.upper() != "PAPER" or settings.live_trading:
        raise LaunchError("refusing to start: the legacy engine settings are not paper-only")
    if settings.broker.upper() != "LOCAL_PAPER":
        raise LaunchError(
            f"refusing to start: BROKER={settings.broker}; the legacy engine requires "
            "LOCAL_PAPER (configure Upstox and Alpaca only in the operator UI)"
        )
    # create_app() resolves its data directory from the application root, which
    # inside a frozen bundle is the extracted payload (sys._MEIPASS). The sample
    # CSVs are bundled there, so DEMO previews do not depend on the working
    # directory. If they are absent the chart says "no data" instead of failing.
    return create_app(settings)


RUNTIME_MODULES = (
    # Stdlib modules the operator console, the paper engine, HTTPS downloads and
    # the per-user SQLite store need. PyInstaller normally collects all of them;
    # a bundle that lost one of the C extensions fails deep inside a request, so
    # this diagnostic names the missing module up front.
    "ssl", "_ssl", "hashlib", "_hashlib", "sqlite3", "_sqlite3", "ctypes",
    "ctypes.wintypes", "json", "socket", "_socket", "select", "zlib", "lzma",
    "bz2", "unicodedata", "zoneinfo", "http.client", "urllib.request",
)


def runtime_check() -> int:
    """Report whether this build can import everything it needs at runtime.

    Reads no operator configuration, creates no files and contacts no network.
    Prints one JSON object; the exit status is 2 when a module is missing so a
    build/install gate fails closed with an actionable message.
    """
    import importlib
    import importlib.util

    failures: list[str] = []
    imported: dict[str, bool] = {}
    for name in RUNTIME_MODULES:
        try:
            importlib.import_module(name)
            imported[name] = True
        except Exception as exc:  # ImportError, OSError for a missing DLL
            imported[name] = False
            failures.append(f"{name}: {type(exc).__name__}: {exc}")

    # The official MT5 package opens terminal IPC on import, so only its
    # presence is reported here. It is a Windows-only build input.
    mt5_present: bool | None = None
    if os.name == "nt":
        try:
            mt5_present = importlib.util.find_spec("MetaTrader5") is not None
        except (ImportError, ValueError):
            mt5_present = False
        if not mt5_present:
            failures.append("MetaTrader5: module is not bundled")

    identity: dict[str, object] = {}
    try:
        from app.build_info import build_identity

        info = build_identity()
        identity = {
            "commit": info.get("commit"),
            "version": info.get("version"),
            "frozen": bool(info.get("frozen")),
        }
    except Exception as exc:  # a broken bundle must still print a report
        failures.append(f"app.build_info: {type(exc).__name__}: {exc}")

    report = {
        "status": "PASS" if not failures else "FAIL",
        "python": sys.version.split()[0],
        "frozen": bool(getattr(sys, "frozen", False)),
        "identity": identity,
        "modules": imported,
        "mt5_package_present": mt5_present,
        "failures": failures,
        "note": "import/availability diagnostic only; no broker, credential or network access",
    }
    print(json.dumps(report, sort_keys=True))
    return 0 if not failures else 2


def dpapi_selftest() -> int:
    """Prove protected credential storage works, using synthetic values only.

    Runs entirely inside a fresh temporary directory so the operator's real
    store in ``%LOCALAPPDATA%\\AXIOM\\credentials`` is never opened, written,
    read or deleted. The synthetic secret is generated here, never printed, and
    the report states explicitly whether it leaked into the output, whether the
    blob on disk is unreadable ciphertext, and whether the redactor masks it.

    Exit status: 0 verified, 3 DPAPI unavailable (non-Windows or a broken
    profile), 2 a storage defect. Only a sanitized JSON line is printed.
    """
    import shutil
    import tempfile

    report: dict[str, object] = {
        "status": "FAIL",
        "backend": "Windows DPAPI (current user)",
        "isolated_directory": False,
        "roundtrip_ok": False,
        "encrypted_at_rest": False,
        "delete_ok": False,
        "missing_key_fail_closed": False,
        "redaction_ok": False,
        "secret_echoed": False,
        "failures": [],
    }
    failures: list[str] = []
    secret = "SYNTHETIC-" + os.urandom(16).hex().upper()
    payload = {"api_token": secret, "note": "synthetic self-test value"}
    workdir = Path(tempfile.mkdtemp(prefix="axiom-dpapi-selftest-"))
    # The real store must never be touched: refuse to continue if a caller's
    # temporary directory resolves inside AXIOM's per-user data directory.
    protected_roots = []
    for env_name in ("LOCALAPPDATA", "APPDATA"):
        raw = os.environ.get(env_name)
        if raw:
            protected_roots.append((Path(raw) / APP_NAME).resolve())
    try:
        resolved = workdir.resolve()
        if any(resolved == root or root in resolved.parents for root in protected_roots):
            failures.append("self-test directory resolves inside the operator data folder")
        else:
            report["isolated_directory"] = True

        from app.security.dpapi_secrets import (
            DpapiSecretStore,
            SecretNotFound,
            SecretStoreUnavailable,
            redact,
        )

        try:
            store = DpapiSecretStore(
                workdir,
                allowed_keys={"selftest"},
                label="AXIOM DPAPI self-test",
            )
        except SecretStoreUnavailable as exc:
            report["status"] = "UNAVAILABLE"
            report["failures"] = [str(exc)]
            print(json.dumps(report, sort_keys=True))
            return 3

        try:
            store.put("selftest", payload)
            report["roundtrip_ok"] = store.exists("selftest") and store.get("selftest") == payload
            if not report["roundtrip_ok"]:
                failures.append("stored value did not round-trip exactly")
            blob_path = workdir / "selftest.dpapi"
            raw_blob = blob_path.read_bytes() if blob_path.exists() else b""
            marker = secret.encode()
            report["encrypted_at_rest"] = bool(raw_blob) and marker not in raw_blob and b"SYNTHETIC" not in raw_blob
            if not report["encrypted_at_rest"]:
                failures.append("credential blob is missing or readable plaintext on disk")
            store.delete("selftest")
            try:
                store.get("selftest")
                failures.append("deleted credential was still readable")
            except SecretNotFound:
                report["delete_ok"] = not store.exists("selftest")
            if not report["delete_ok"]:
                failures.append("deleted credential file is still present")
            try:
                store.get("not-an-allowed-key")
                failures.append("store accepted a key outside allowed_keys")
            except (ValueError, KeyError):
                report["missing_key_fail_closed"] = True
            if not report["missing_key_fail_closed"]:
                failures.append("store did not refuse an unlisted credential key")
            masked = redact({"credential": secret, "nested": [{"token": secret}]}, secrets=(secret,))
            report["redaction_ok"] = secret not in json.dumps(masked)
            if not report["redaction_ok"]:
                failures.append("redactor left the synthetic secret visible")
        except Exception as exc:  # a DPAPI failure must never crash the report
            failures.append(f"{type(exc).__name__}: {exc}")
    except Exception as exc:
        failures.append(f"{type(exc).__name__}: {exc}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    report["failures"] = failures
    report["status"] = "PASS" if not failures else "FAIL"
    rendered = json.dumps(report, sort_keys=True)
    # Self-evident proof that the synthetic secret never reaches the output.
    report["secret_echoed"] = secret in rendered
    rendered = json.dumps(report, sort_keys=True)
    print(rendered)
    return 0 if report["status"] == "PASS" else 2


def open_when_ready(url: str, port: int, server_ready: threading.Event) -> None:
    server_ready.wait(timeout=30)
    for _ in range(20):
        try:
            with socket.create_connection((LOOPBACK, port), 0.5):
                break
        except OSError:
            time.sleep(0.25)
    try:
        webbrowser.open(url)
    except Exception:  # a headless session simply has no browser
        pass


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Build/acceptance-only diagnostic: it exits before reading or creating any
    # operator configuration and uses isolated fake PAPER fixtures only.
    if "--autonomy-blocker-smoke" in argv:
        if argv != ["--autonomy-blocker-smoke"]:
            print("--autonomy-blocker-smoke cannot be combined with other arguments", file=sys.stderr)
            return 2
        from frozen_blocker_smoke import run
        return run()
    if "--runtime-check" in argv:
        if argv != ["--runtime-check"]:
            print("--runtime-check cannot be combined with other arguments", file=sys.stderr)
            return 2
        return runtime_check()
    if "--dpapi-selftest" in argv:
        if argv != ["--dpapi-selftest"]:
            print("--dpapi-selftest cannot be combined with other arguments", file=sys.stderr)
            return 2
        return dpapi_selftest()
    if "--check-deps" in argv:
        from app.source_launcher import check_runtime_dependencies, format_missing_dependencies_error

        report = check_runtime_dependencies()
        if not report["ok"]:
            print(f"AXIOM dependency check failed:\n\n{format_missing_dependencies_error(report)}", file=sys.stderr)
            return 2
        print(json.dumps(report, sort_keys=True))
        return 0
    if "--register-venv" in argv:
        from app.source_launcher import register_repo_in_active_site_packages

        pth = register_repo_in_active_site_packages()
        print(json.dumps({"registered": True, "pth_file": str(pth), "repo_root": str(_REPO_ROOT)}, sort_keys=True))
        return 0
    if "--smoke" in argv:
        from app.source_launcher import (
            check_runtime_dependencies,
            format_missing_dependencies_error,
            run_source_smoke_check,
        )

        dep_report = check_runtime_dependencies()
        if not dep_report["ok"]:
            print(f"AXIOM did not start:\n\n{format_missing_dependencies_error(dep_report)}", file=sys.stderr)
            return 2
        result = run_source_smoke_check()
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("status") == "PASS" else 1
    port_override: int | None = None
    if "--port" in argv:
        idx = argv.index("--port")
        if idx + 1 >= len(argv):
            print("--port requires an integer port number", file=sys.stderr)
            return 2
        try:
            port_override = int(argv[idx + 1])
        except ValueError:
            print(f"invalid --port value: {argv[idx + 1]!r}", file=sys.stderr)
            return 2
    no_browser = "--no-browser" in argv
    print(BANNER.format(title=APP_TITLE, label=BUILD_LABEL))
    try:
        if not getattr(sys, "frozen", False):
            from app.source_launcher import check_runtime_dependencies, format_missing_dependencies_error

            dep_report = check_runtime_dependencies()
            if not dep_report["ok"]:
                raise LaunchError(format_missing_dependencies_error(dep_report))
        data_dir = user_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "logs").mkdir(exist_ok=True)
        config = load_configuration(data_dir)
        print(f"  config   {config['config']}"
              f"{'  (created)' if config['created'] == 'yes' else ''}")
        print(f"  data     {data_dir}")
        print(f"  feed     {config['data_source']}")
        print("  token    read API_TOKEN from the private config file (not printed)")
        print("           enter it in the operator page; it is separate from Upstox and Alpaca credentials")
        app = build_app()
        preferred = port_override if port_override is not None else int(os.environ.get("AXIOM_PORT") or DEFAULT_PORT)
        port = free_port(preferred)
        url = f"http://{LOOPBACK}:{port}/terminal"
        ready = threading.Event()
        if not no_browser:
            threading.Thread(
                target=open_when_ready, args=(url, port, ready), daemon=True
            ).start()
        print(f"\n  operator {url}")
        print("  stop     Ctrl+C in this window\n")
        import uvicorn

        ready.set()
        uvicorn.run(app, host=LOOPBACK, port=port, log_level="warning", access_log=False)
        return 0
    except LaunchError as exc:
        print(f"\nAXIOM did not start:\n\n{exc}\n", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0
    except Exception as exc:  # never leave a console user with a raw traceback
        print(
            f"\nAXIOM stopped unexpectedly ({type(exc).__name__}: {exc}).\n"
            f"Logs and your operator data are in {user_data_dir()}.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
