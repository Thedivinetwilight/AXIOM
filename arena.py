#!/usr/bin/env python3
"""Arena — one entry point that starts everything.

    python arena.py                 # start the server, open the interface
    python arena.py --port 9000     # pick a port
    python arena.py --no-browser    # headless (servers, containers)
    python arena.py --cli live-status   # run any CLI command
    python arena.py --setup         # open the setup screen on this run

This is the same file whether Arena runs from source or as a packaged
executable. When frozen it is the .exe's entry point, and it also acts as the
child-process re-entry point so the feature console can launch commands:
PyInstaller sets ``sys.executable`` to the .exe itself, which cannot be
invoked as ``python -m app``, so the console calls back here with ``--cli``.

Desktop behaviours implemented here (3.1.0):

* **single instance per data directory** — a second launch detects the running
  session through ``app.instance.InstanceGuard`` and opens it in the browser
  instead of starting a conflicting server (no tracebacks, no Task Manager);
* **free port discovery** — a busy default port never stops a launch;
* **first-run routing** — a fresh install opens the setup wizard, an
  established one opens the main application;
* **durable logs** — a frozen build also logs to ``<home>/logs/arena.log``
  so a windowed launch still leaves diagnostics behind;
* **readable failures** — a double-clicked build that cannot start explains
  itself instead of flashing a window.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

# Ensure the repository root is importable when run as a plain script.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))


def _pin_ca_bundle() -> None:
    """Point every SSL-using library at certifi's CA bundle when frozen.

    PyInstaller 6.22 ships no hook that installs a trust store, and on
    Windows the frozen bundle has no default CA path. Setting
    ``SSL_CERT_FILE`` / ``REQUESTS_CA_BUNDLE`` before any network call
    means httpx/requests/urllib3/websockets/ssl.create_default_context()
    all pick up the bundled CA file — we don't have to thread ``verify=``
    through every outbound call site.
    """
    if not getattr(sys, "frozen", False):
        return  # source runs use the OS trust store
    if os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE"):
        return  # operator override wins
    try:
        import certifi
        cafile = certifi.where()
    except Exception:
        return
    if not Path(cafile).exists():
        return
    os.environ["SSL_CERT_FILE"] = cafile
    os.environ["REQUESTS_CA_BUNDLE"] = cafile
    os.environ["CURL_CA_BUNDLE"] = cafile


_pin_ca_bundle()

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8042
logger = logging.getLogger("arena.startup")


def _free_port(preferred: int, host: str, tries: int = 25) -> int:
    """Return `preferred` if it is usable, else the next free port.

    A double-clicked .exe must never fail with "address already in use" — most
    often the culprit is a previous Arena that has not fully exited.

    The probe must bind the way the server will, and SO_REUSEADDR does NOT mean
    the same thing on both platforms:

    * POSIX — SO_REUSEADDR only relaxes TIME_WAIT, so it is safe and avoids
      discarding a port that is about to become available.
    * Windows — SO_REUSEADDR permits binding to an address that another socket
      is ALREADY USING. Setting it here would make every port look free, the
      picker would return a busy port, and Arena would fail at startup with the
      very error this function exists to prevent. asyncio also disables address
      reuse on Windows, so a plain bind is the accurate test there.
    """
    if not 0 < preferred < 65536:
        raise SystemExit(
            f"--port must be between 1 and 65535 (got {preferred})."
        )
    for offset in range(tries):
        candidate = preferred + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if os.name != "nt":
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((host, candidate))
            except OSError:
                continue
        return candidate
    raise SystemExit(
        f"Arena could not find a free port near {preferred}. "
        "Close other programs using these ports, or pass --port."
    )


def _first_run() -> bool:
    """True when the guided setup screen should open.

    First run means no configuration file yet. A file that exists but has no
    API_TOKEN also counts as "not set up": without a token the protected API
    (and therefore the dashboard) cannot be used, which is exactly the state
    the wizard exists to fix.
    """
    from app import runtime
    from app.config.envfile import read_values

    path = runtime.env_file()
    if not path.exists():
        return True
    try:
        return not (read_values(path).get("API_TOKEN") or "").strip()
    except OSError:
        return True


def _print_banner(url: str, setup: bool) -> None:
    from app.runtime import ensure_utf8_output

    ensure_utf8_output()
    target = f"{url}/setup" if setup else f"{url}/app"
    print()
    print("  " + "-" * 66)
    print("   ARENA — quantitative research & paper trading")
    print("  " + "-" * 66)
    print(f"   Open this address:   {target}")
    if setup:
        print("   (first run: the guided setup starts paper trading in")
        print("    one click — no credentials and no commands needed)")
    print()
    print("   Keep this window open while you use Arena.")
    print("   Press Ctrl+C here to stop.")
    print("  " + "-" * 66, flush=True)


def _configure_file_logging() -> None:
    """Always keep a durable log in the Arena home.

    A windowed/frozen launch has no console to read, so the file is where
    startup problems become diagnosable. Failures here must never stop a
    launch — logging is observability, not a dependency.
    """
    try:
        from app import runtime

        logs = runtime.arena_home() / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(logs / "arena.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s"))
        root = logging.getLogger()
        if not any(isinstance(h, logging.FileHandler) for h in root.handlers):
            root.addHandler(handler)
        # Startup paths are safe diagnostics (paths and field names only) and
        # must survive a frozen console closing after a mismatch.
        logging.getLogger("arena.startup").setLevel(logging.INFO)
        if root.level == logging.NOTSET:
            root.setLevel(logging.INFO)
    except (OSError, ValueError):
        pass  # observability must never break startup


def _handle_existing_instance(existing, *, open_browser: bool) -> int:
    """A second launch while one Arena already owns this data directory.

    Never starts a conflicting server, never kills the holder, never deletes
    a lock. Either opens the running session or explains precisely what is
    in the way.
    """
    from app import runtime
    from app.instance import wait_until_healthy

    runtime.ensure_utf8_output()
    print()
    print("  " + "-" * 66)
    print("   Arena is already running for this data directory.")
    print("   A second server is NOT started — one engine per session.")
    if existing.pid:
        print(f"   Running session: pid {existing.pid}"
              + (f", {existing.url}" if existing.url else ""))
    print("  " + "-" * 66)

    url = existing.url
    reachable = existing.reachable
    if url and not reachable:
        # The holder may simply be mid-startup (a one-file build unpacks for
        # a few seconds). Give it a short, bounded chance to answer.
        print("   Waiting for the running session to answer…", flush=True)
        reachable = wait_until_healthy(url, timeout=8.0)

    if url and reachable:
        print(f"   Opening the running session: {url}/app")
        if open_browser:
            try:
                webbrowser.open(f"{url}/app")
            except Exception:
                pass  # no browser is not an error worth failing for
        print("  " + "-" * 66)
        return 0

    # Lock held but nothing answering: fail closed and explain. We do NOT
    # guess that the holder is stale — the OS lock only survives a live
    # process, so deleting anything here could destroy an active session.
    print("   The running session is not answering HTTP requests yet.")
    print("   Look for an Arena window or console on this machine. If you")
    print("   are certain no Arena is running, a reboot of the machine")
    print("   releases every lock safely — no files need manual deletion.")
    print("  " + "-" * 66, flush=True)
    return 2


def _serve(host: str, port: int, open_browser: bool, force_setup: bool) -> int:
    import uvicorn

    from app import build_info, runtime
    from app.api.server import create_app
    from app.config.settings import Settings
    from app.instance import InstanceGuard

    runtime.ensure_home()
    runtime.ensure_utf8_output()
    _configure_file_logging()

    guard = InstanceGuard(runtime.arena_home())
    existing = guard.try_acquire()
    if existing is not None:
        return _handle_existing_instance(existing, open_browser=open_browser)

    try:
        database_url_in_process = "DATABASE_URL" in os.environ
        database_url_process_empty = not bool(
            (os.environ.get("DATABASE_URL") or "").strip()
        )
        settings = Settings.from_env()
        env_candidates = runtime.env_candidates()
        database_path = runtime.database_file_path(settings.database_url)
        try:
            active_override = runtime.read_paper_database_override()
        except ValueError:
            # Settings.from_env already fails closed for a bad pointer; this is
            # only defensive in case an alternate Settings implementation is used.
            active_override = None
        logger.info(
            "startup_paths %s",
            json.dumps({
                "arena_home": str(runtime.arena_home()),
                "database_path": str(database_path or ":memory:"),
                "database_url_source": (
                    "persistent paper recovery selection"
                    if active_override is not None
                    else runtime.database_url_source(
                        process_value_present=database_url_in_process,
                        process_value_empty=database_url_process_empty,
                    )
                ),
                "dotenv_files_loaded": [str(path) for path in env_candidates if path.is_file()],
                "dotenv_precedence": (
                    "process environment first; then env_candidates order; "
                    "load_dotenv override=False"
                ),
                "paper_database_override": active_override is not None,
                "mode": settings.mode.upper(),
                "live_trading": bool(settings.live_trading),
                "broker": settings.broker.upper(),
            }, sort_keys=True),
        )

        # Diagnose a mismatch before FastAPI/Database startup. This is a
        # read-only check: no SQLite schema migration or account write occurs
        # before the user explicitly selects a separate recovery database.
        from dataclasses import asdict
        from app.backtesting.costs import ExecutionConfig
        from app.paper_trading.compatibility import (
            PaperDatabaseInspectionError,
            inspect_paper_account,
        )
        from app.paper_trading.execution import calendar_signature, make_calendar

        try:
            mismatch = inspect_paper_account(
                settings.database_url,
                asdict(ExecutionConfig(**settings.execution_kwargs())),
                currency=settings.currency,
                timeframe=settings.default_timeframe,
                calendar_signature=calendar_signature(
                    make_calendar(settings.market_calendar, settings.default_timeframe)
                ),
            )
            if mismatch is not None:
                recovery_command = (
                    f'"{sys.executable}" --recover-paper-account '
                    "--confirm-new-empty-paper-account"
                    if runtime.is_frozen()
                    else f'"{sys.executable}" "{Path(__file__).resolve()}" '
                    "--recover-paper-account --confirm-new-empty-paper-account"
                )
                message = (
                    f"{mismatch}\n\n"
                    "To continue, explicitly create a separate empty PAPER account; "
                    "the existing database, orders, fills, and research remain untouched.\n"
                    f"Run: {recovery_command}\n"
                    "Recovery stays MODE=PAPER, LIVE_TRADING=false, "
                    "BROKER=LOCAL_PAPER and does not place or copy orders."
                )
                logger.error("%s", message)
                print("\n" + message, flush=True)
                if runtime.is_frozen() and not (sys.stdin and sys.stdin.isatty()):
                    _show_fatal_dialog(message)
                return 3
        except PaperDatabaseInspectionError as exc:
            logger.error("%s", exc)
            message = "Arena startup refused: " + str(exc)
            print("\n" + message, flush=True)
            if runtime.is_frozen() and not (sys.stdin and sys.stdin.isatty()):
                _show_fatal_dialog(message)
            return 3

        application = create_app(settings)
        application.state.instance_guard = guard

        shown = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
        url = f"http://{shown}:{port}"
        guard.publish(port=port, host=host, version=build_info.version(),
                      build=build_info.build_id())
        guard.start_heartbeat()
        setup = force_setup or _first_run()
        config = uvicorn.Config(
            application,
            host=host,
            port=port,
            log_level="info",
            access_log=False,
            # A frozen bundle must not spawn reloaders or worker processes: they
            # re-execute the bundle and multiply memory for no benefit.
            reload=False,
            workers=1,
        )
        server = uvicorn.Server(config)

        def open_when_ready() -> None:
            for _ in range(200):  # up to ~20s
                if getattr(server, "started", False):
                    break
                time.sleep(0.1)
            else:
                return
            if open_browser:
                webbrowser.open(f"{url}/setup" if setup else f"{url}/app")

        threading.Thread(target=open_when_ready, daemon=True).start()
        _print_banner(url, setup)

        try:
            server.run()
        except KeyboardInterrupt:
            print("\n  Arena stopped.", flush=True)
        return 0
    finally:
        guard.release()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # ---- child-process re-entry (used by the feature console) --------------
    if argv and argv[0] == "--cli":
        from app.__main__ import main as cli_main

        try:
            cli_main(argv[1:])
        except SystemExit as exc:
            return int(exc.code or 0)
        return 0

    if argv and argv[0] == "--run":
        import run as run_module

        try:
            run_module.main(["--mode", *argv[1:]])
        except SystemExit as exc:
            return int(exc.code or 0)
        return 0

    parser = argparse.ArgumentParser(
        prog="arena",
        description="Arena — start the platform and open the interface.",
    )
    parser.add_argument("--host", default=os.environ.get("ARENA_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("ARENA_PORT", DEFAULT_PORT)))
    parser.add_argument("--no-browser", action="store_true",
                        help="do not open a web browser")
    parser.add_argument("--setup", action="store_true",
                        help="open the setup screen on this run")
    parser.add_argument("--diagnostics", action="store_true",
                        help="print resolved paths and dotenv precedence, then exit")
    recovery_group = parser.add_mutually_exclusive_group()
    recovery_group.add_argument(
        "--recover-paper-account", action="store_true",
        help="create and select a separate empty PAPER database after a strict mismatch")
    recovery_group.add_argument(
        "--clear-paper-database-recovery", action="store_true",
        help="remove only the saved recovery selection; never delete either database")
    parser.add_argument(
        "--confirm-new-empty-paper-account", action="store_true",
        help="confirm that recovery creates a separate empty account (no data is copied)")
    args = parser.parse_args(argv)

    from app import runtime

    if args.diagnostics and (args.recover_paper_account or args.clear_paper_database_recovery):
        parser.error("--diagnostics cannot be combined with a recovery action")
    if args.confirm_new_empty_paper_account and not args.recover_paper_account:
        parser.error("--confirm-new-empty-paper-account requires --recover-paper-account")

    if args.clear_paper_database_recovery:
        removed = runtime.clear_paper_database_override()
        if removed:
            print(
                "Cleared the persistent paper-database selection only. "
                "No database, order, fill, or research record was deleted."
            )
        else:
            print("No separate paper-database selection exists; nothing was changed.")
        return 0

    if args.diagnostics:
        report = runtime.describe()
        database_url_in_process = "DATABASE_URL" in os.environ
        database_url_process_empty = not bool(
            (os.environ.get("DATABASE_URL") or "").strip()
        )
        try:
            from app.config.settings import Settings

            settings = Settings.from_env()
            path = runtime.database_file_path(settings.database_url)
            report["database_path"] = str(path or ":memory:")
            report["database_url_source"] = (
                "persistent paper recovery selection"
                if report.get("paper_database_override_active")
                else runtime.database_url_source(
                    process_value_present=database_url_in_process,
                    process_value_empty=database_url_process_empty,
                )
            )
            report["database_selection"] = (
                "persistent separate PAPER recovery database"
                if report.get("paper_database_override_active")
                else "configured DATABASE_URL or frozen default"
            )
            report["execution_safety"] = {
                "mode": settings.mode.upper(),
                "live_trading": bool(settings.live_trading),
                "broker": settings.broker.upper(),
            }
        except Exception as exc:
            # Never print .env values or exception text in the path report.
            report["configuration_error_type"] = type(exc).__name__
        print(json.dumps(report, indent=2))
        return 0

    if args.recover_paper_account:
        runtime.ensure_utf8_output()
        if not args.confirm_new_empty_paper_account:
            parser.error(
                "recovery requires --confirm-new-empty-paper-account; it creates "
                "a separate empty account and does not copy old paper state"
            )
        from app.config.settings import Settings
        from app.paper_trading.recovery import (
            PaperRecoveryError,
            create_separate_paper_database,
        )

        try:
            settings = Settings.from_env(apply_paper_database_override=False)
            recovered = create_separate_paper_database(settings)
        except (PaperRecoveryError, ValueError) as exc:
            message = "Paper recovery refused: " + str(exc)
            print(message, flush=True)
            if runtime.is_frozen() and not (sys.stdin and sys.stdin.isatty()):
                _show_fatal_dialog(message)
            return 3
        print(
            "Separate PAPER database selected.\n"
            f"Original database (preserved): {recovered.previous_database}\n"
            f"New empty database: {recovered.selected_database}\n"
            "Differing fields: " + ", ".join(recovered.differing_fields) + "\n"
            "No orders or fills were copied or placed. The selection persists "
            "across launches; execution remains PAPER / LOCAL_PAPER.\n",
            flush=True,
        )

    # On Windows a frozen build must call this before any process is spawned,
    # or a child re-executing the bundle recurses instead of running.
    try:
        import multiprocessing

        multiprocessing.freeze_support()
    except Exception:  # pragma: no cover
        pass

    port = _free_port(args.port, args.host)
    if port != args.port:
        print(f"  Port {args.port} is in use; using {port} instead.", flush=True)
    return _serve(args.host, port, not args.no_browser, args.setup)


def _should_hold_console(argv: list[str], *, frozen: bool, attached: bool) -> bool:
    """Whether a failed start should wait for a keypress before exiting.

    On Windows a double-clicked .exe owns a console that closes the moment the
    process exits, so a startup failure — a port it could not bind, a damaged
    data directory, an unexpected error — reaches the user as a window that
    flashes and is gone. There is nothing to act on and it looks exactly like
    "the exe is broken".

    Deliberately narrow: a frozen build, a real terminal, and the server path
    only. `--cli`/`--run`/`--diagnostics` are commands whose exit code callers
    (scripts, the feature console, the packaged smoke test) depend on, and a
    piped run must never stop to wait for input.
    """
    if not (frozen and attached):
        return False
    if argv and argv[0] in ("--cli", "--run", "--diagnostics"):
        return False
    return True


def _hold_console_open(reason: str) -> None:
    """Print why Arena did not start, then wait so the message can be read.

    Only called once `_should_hold_console` has agreed; the caller has already
    printed the traceback if there is one, so the order on screen is: cause
    first, then this block, then the window waits.
    """
    print()
    print("  " + "-" * 66)
    print(f"   Arena did not start: {reason}")
    print("   Nothing was changed, no data was deleted, no order was placed.")
    print("   Read the message above, then press Enter to close this window.")
    print("  " + "-" * 66, flush=True)
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        pass  # the window is closing anyway; never mask the original error


def _show_fatal_dialog(reason: str) -> None:
    """Last-resort visibility for a windowless frozen build on Windows.

    A windowless executable has no console to keep open, so a startup failure
    would otherwise be invisible. Headless automation can explicitly suppress
    the modal dialog while still receiving the same exit code, stdout, and log.
    ctypes talks to user32 directly, which needs no extra bundled dependency
    (tkinter is deliberately excluded from the build).
    """
    if os.environ.get("ARENA_SUPPRESS_FATAL_DIALOG") == "1":
        return
    if os.name != "nt" or not sys.stdin or sys.stdin.isatty():
        return
    try:
        import ctypes

        text = (f"Arena did not start:\n\n{reason}\n\n"
                "Nothing was changed and no order was placed.\n"
                "Details: %LOCALAPPDATA%\\Arena\\logs\\arena.log")
        MB_ICONERROR = 0x10
        ctypes.windll.user32.MessageBoxW(0, text, "Arena", MB_ICONERROR)
    except Exception:
        pass  # the dialog is best-effort; never mask the original error


if __name__ == "__main__":
    # A frozen build is very likely a double-click. Anything that stops
    # it from starting must be readable rather than a window that flashes.
    from app.runtime import is_frozen

    def reader_is_watching() -> bool:
        return _should_hold_console(sys.argv[1:], frozen=is_frozen(),
                                    attached=bool(sys.stdin and sys.stdin.isatty()))

    try:
        raise SystemExit(main())
    except SystemExit as exc:
        code = 0 if exc.code is None else exc.code
        if code != 0 and reader_is_watching():
            # Python prints a string code itself; the wait must leave the
            # message on screen rather than overwriting it.
            _hold_console_open(
                code if isinstance(code, str) else f"exit code {code}")
        raise
    except Exception as exc:
        if reader_is_watching():
            import traceback

            traceback.print_exc()
            _hold_console_open(f"{type(exc).__name__}: {exc}")
            raise SystemExit(1)  # already reported; do not print it twice
        if is_frozen():
            _show_fatal_dialog(f"{type(exc).__name__}: {exc}")
        raise
