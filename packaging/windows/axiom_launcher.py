"""AXIOM chart terminal - packaged launcher (paper only).

This is the entry point of the frozen executable. It does four things:

1. keeps every writable thing (config, database, logs) in a per-user data
   directory, so upgrading or uninstalling never destroys the operator's paper
   account;
2. refuses to start unless the configuration is unambiguously paper-only;
3. starts the local API on the loopback interface and opens the chart;
4. fails closed, with a readable message and a non-zero exit code, when the
   configuration or the data it needs is missing or invalid.

It contains no broker client, no order endpoint and no network client of its
own: the only thing it starts is the same ``app.api.server`` application the
test suite exercises.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

APP_NAME = "AXIOM"
APP_TITLE = "AXIOM chart terminal"
BUILD_LABEL = "chart + US paper data"
DEFAULT_PORT = 8000
TOKEN_ENV = "API_TOKEN"
LOOPBACK = "127.0.0.1"

CONFIG_TEMPLATE = """\
# AXIOM configuration for this installation.
# This file lives in your user data folder, NOT in the application folder:
# upgrading or reinstalling AXIOM never overwrites or deletes it.
# Keep it private. It can contain a local API token and market-data keys.

# --- hard safety switches (AXIOM refuses to start if these are changed) ---
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
 \___/|_||_|___| /_/\_\   PAPER ONLY - every fill is simulated

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
            "this build is paper-only and will not start with:\n"
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
        raise LaunchError("refusing to start: the resolved settings are not paper-only")
    if settings.broker.upper() != "LOCAL_PAPER":
        raise LaunchError(
            f"refusing to start: BROKER={settings.broker}; this build runs the local "
            "paper broker only"
        )
    # create_app() resolves its data directory from the application root, which
    # inside a frozen bundle is the extracted payload (sys._MEIPASS). The sample
    # CSVs are bundled there, so DEMO previews do not depend on the working
    # directory. If they are absent the chart says "no data" instead of failing.
    return create_app(settings)


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
    no_browser = "--no-browser" in argv
    print(BANNER.format(title=APP_TITLE, label=BUILD_LABEL))
    try:
        data_dir = user_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "logs").mkdir(exist_ok=True)
        config = load_configuration(data_dir)
        print(f"  config   {config['config']}"
              f"{'  (created)' if config['created'] == 'yes' else ''}")
        print(f"  data     {data_dir}")
        print(f"  feed     {config['data_source']}")
        print(f"  token    {os.environ.get(TOKEN_ENV, '')}")
        print("           paste this when the chart asks; it stays on this machine")
        app = build_app()
        preferred = int(os.environ.get("AXIOM_PORT") or DEFAULT_PORT)
        port = free_port(preferred)
        url = f"http://{LOOPBACK}:{port}/chart"
        ready = threading.Event()
        if not no_browser:
            threading.Thread(
                target=open_when_ready, args=(url, port, ready), daemon=True
            ).start()
        print(f"\n  chart    {url}")
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
            f"Logs and your paper account are in {user_data_dir()}.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
