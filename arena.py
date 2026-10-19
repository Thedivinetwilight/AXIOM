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


#: Exit codes are part of the operator contract (documented in README.md).
#: A refused start is not a crash: scripts and the packaged smoke tests can
#: tell "configuration/data needs attention" apart from "unexpected bug".
EXIT_UNEXPECTED = 1
EXIT_CONFIG = 3
EXIT_STORAGE = 4


class StartupRefusal(RuntimeError):
    """A startup condition the operator can fix; printed without a traceback."""

    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = int(code)


def _recovery_command() -> str:
    from app.maintenance.paper_recovery import recovery_command

    return recovery_command()


def _log_location() -> str:
    from app import runtime

    return str(runtime.arena_home() / "logs" / "arena.log")


def _print_startup_refusal(reason: str) -> None:
    """Explain a refused start in operator language, never a traceback."""
    from app.runtime import ensure_utf8_output

    ensure_utf8_output()
    print()
    print("  " + "-" * 66)
    print("   Arena did not start")
    print("  " + "-" * 66)
    for line in str(reason).splitlines():
        print(f"   {line}" if line else "")
    print()
    print("   Nothing was changed, no data was deleted, no order was placed.")
    print("  " + "-" * 66, flush=True)


def _check_paper_writer_lock(path) -> None:
    """Refuse cleanly when another process owns this database's writer lock.

    The lock itself is authoritative; this probe only gives its refusal a
    readable message before uvicorn would otherwise print a traceback. The
    probe releases its own claim immediately, and the real lock is still taken
    by `Repository.acquire_writer`.
    """
    import os
    from pathlib import Path

    lock_path = Path(str(path) + ".paper.lock")
    if not lock_path.exists():
        return
    try:
        handle = open(lock_path, "rb")
    except OSError as exc:
        raise StartupRefusal(
            f"Another Arena process may be using {path} (its writer-lock file "
            f"is present but unreadable: {type(exc).__name__}). Close the other "
            "instance and retry, or remove the stale lock only after confirming "
            "no Arena process is running.",
            EXIT_STORAGE,
        ) from None
    try:
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (OSError, ImportError):
            raise StartupRefusal(
                f"Another paper process currently owns {path}. Close it and "
                "retry. (Arena will not open a second writer on one paper "
                "account.)",
                EXIT_STORAGE,
            ) from None
    finally:
        handle.close()


def _preflight(settings) -> None:
    """Everything that can be checked before the server claims the database.

    All checks are READ-ONLY: a refused start must leave `.env` and the
    database byte-for-byte unchanged.
    """
    import os
    from pathlib import Path

    from app import runtime
    from app.database.paths import (
        DatabaseUrlError,
        is_memory_url,
        resolve_sqlite_path,
    )
    from app.paper_trading import compatibility

    env_file = runtime.env_file()
    recovery = _recovery_command()

    try:
        compatibility.ensure_compatible(
            settings, env_file=env_file, recovery_command=recovery,
            home=runtime.arena_home() if runtime.is_portable() else None)
    except compatibility.PaperAccountMismatch as exc:
        raise StartupRefusal(
            exc.guidance(env_file=env_file, recovery_command=recovery),
            EXIT_CONFIG,
        ) from None
    except compatibility.PaperDatabaseUnreadable as exc:
        raise StartupRefusal(
            f"{exc}\n\n"
            "The file was not modified. If it is damaged, keep a copy for "
            "inspection and restore a verified backup "
            "(`python -m app backup verify --path ...`).\n"
            "To start a fresh paper account instead (the existing file is "
            f"left untouched):\n    {recovery}",
            EXIT_STORAGE,
        ) from None
    except ValueError as exc:
        raise StartupRefusal(f"Configuration error: {exc}", EXIT_CONFIG) from None

    try:
        if is_memory_url(settings.database_url):
            return
        path = resolve_sqlite_path(
            settings.database_url,
            home=runtime.arena_home() if runtime.is_portable() else None)
    except DatabaseUrlError as exc:
        raise StartupRefusal(f"Configuration error: {exc}", EXIT_CONFIG) from None

    parent = path.parent
    if path.exists():
        if not path.is_file():
            raise StartupRefusal(
                f"DATABASE_URL points at {path}, which is not a regular file.",
                EXIT_STORAGE)
        if not os.access(path, os.W_OK):
            raise StartupRefusal(
                f"The paper database at {path} is read-only for this user. "
                "Arena needs write access to open it (SQLite WAL). Fix the "
                "file/folder permissions, or point DATABASE_URL at a writable "
                "location. The file was not modified.",
                EXIT_STORAGE)
        _check_paper_writer_lock(path)
    else:
        if parent.exists() and not parent.is_dir():
            raise StartupRefusal(
                f"The database folder {parent} is not a directory.",
                EXIT_STORAGE)
        probe = parent
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        if not os.access(probe, os.W_OK):
            raise StartupRefusal(
                f"Arena cannot create its database at {path}: {probe} is not "
                "writable for this user. Grant write access or set DATABASE_URL "
                "to a writable location. Nothing was changed.",
                EXIT_STORAGE)

    # Outputs are part of readiness: a read-only home would make paper trading
    # start and then fail on its first write. Checking now keeps that failure
    # out of the request path.
    home = runtime.arena_home()
    if home.exists():
        probe = home
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        if not os.access(probe, os.W_OK):
            raise StartupRefusal(
                f"Arena's home directory {home} is not writable for this user. "
                "Grant write access, or set ARENA_HOME to a writable folder.",
                EXIT_STORAGE)


def _diagnostics() -> int:
    """`--diagnostics`: resolved paths, effective mode, account health.

    Never prints a token or credential. Exit 0 when the installation looks
    coherent, 1 when a problem needs attention (the JSON still explains it).
    """
    from app import build_info, runtime

    described = runtime.describe()
    # Keep the long-standing top-level keys (tests and support scripts read
    # `arena_home` directly) and also group them for readability.
    payload: dict = {**described, "runtime": described,
                     "build": build_info.describe()}
    problems: list[str] = []
    try:
        from app.config.settings import Settings

        settings = Settings.from_env()
    except Exception as exc:
        payload["config_error"] = f"{type(exc).__name__}: {exc}"
        payload["health"] = {"ok": False,
                             "problems": ["configuration could not be loaded"]}
        print(json.dumps(payload, indent=2, default=str))
        return 1

    from app.database.paths import DatabaseUrlError, is_memory_url, resolve_sqlite_path
    from app.paper_trading.compatibility import inspect_paper_account

    anchor = runtime.arena_home() if runtime.is_portable() else None
    try:
        if is_memory_url(settings.database_url):
            database_path = ":memory:"
        else:
            database_path = str(resolve_sqlite_path(settings.database_url,
                                                    home=anchor))
    except DatabaseUrlError:
        database_path = settings.database_url
        problems.append("DATABASE_URL is not a supported SQLite URL")

    view = inspect_paper_account(settings, home=anchor)
    if view.status == "MISMATCH":
        problems.append("saved paper account does not match the configuration")
    elif view.status == "UNREADABLE":
        problems.append("paper database could not be read")
    static = runtime.static_dir()
    if not static.is_dir():
        problems.append("frontend static assets are missing")
    # No .env yet is the normal first-run state, not a problem.

    payload.update({
        "mode": (settings.mode or "PAPER").upper(),
        "live_trading": bool(settings.live_trading),
        "broker": (settings.broker or "LOCAL_PAPER").upper(),
        "data_source": settings.data_source,
        "default_timeframe": settings.default_timeframe,
        "database_url": settings.database_url,
        "database_path": database_path,
        "database_exists": (database_path not in (":memory:", "")
                            and __import__("pathlib").Path(database_path).exists()),
        "paper_account": view.to_dict(),
        "api_token_configured": bool(settings.api_token),
        "frontend_assets": {"directory": str(static), "present": static.is_dir()},
        "health": {"ok": not problems, "problems": problems},
    })
    print(json.dumps(payload, indent=2, default=str))
    # Diagnostics is informational: a successful inspection exits 0 even when
    # `health.ok` is false (the JSON is the finding). Only an unloadable
    # configuration is a command failure.
    return 0


def _new_paper_account() -> int:
    """`--new-paper-account`: non-destructive recovery (see paper_recovery.py)."""
    from app import runtime
    from app.instance import InstanceGuard
    from app.maintenance.paper_recovery import (
        PaperRecoveryError,
        create_replacement_database,
    )

    runtime.ensure_home()
    guard = InstanceGuard(runtime.arena_home())
    existing = guard.try_acquire()
    if existing is not None:
        return _handle_existing_instance(existing, open_browser=False)
    try:
        try:
            summary = create_replacement_database()
        except PaperRecoveryError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
            return EXIT_STORAGE
        print(json.dumps(summary, indent=2, default=str))
        return 0
    finally:
        guard.release()


def _serve(host: str, port: int, open_browser: bool, force_setup: bool) -> int:
    import logging

    import uvicorn

    from app import build_info, runtime
    from app.api.server import create_app
    from app.config.settings import Settings
    from app.instance import InstanceGuard

    runtime.ensure_home()
    _configure_file_logging()

    guard = InstanceGuard(runtime.arena_home())
    existing = guard.try_acquire()
    if existing is not None:
        return _handle_existing_instance(existing, open_browser=open_browser)

    try:
        try:
            settings = Settings.from_env()
        except ValueError as exc:
            raise StartupRefusal(f"Configuration error: {exc}", EXIT_CONFIG) from None
        _preflight(settings)

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
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else EXIT_UNEXPECTED
            if code == 0:
                raise
            raise StartupRefusal(
                "Arena could not complete startup. The detailed cause is in "
                f"{_log_location()} (search for the last ERROR entry). No order "
                "was placed.",
                EXIT_STORAGE,
            ) from None
        return 0
    except StartupRefusal as exc:
        _print_startup_refusal(str(exc))
        return exc.code
    except Exception as exc:  # unexpected: log the traceback, explain on screen
        logging.getLogger("arena.startup").exception("startup failed")
        _print_startup_refusal(
            f"Unexpected startup failure: {type(exc).__name__}: {exc}\n"
            f"Details: {_log_location()}")
        return EXIT_UNEXPECTED
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
                        help="print where Arena keeps its files, then exit")
    parser.add_argument("--new-paper-account", action="store_true",
                        help=("create a NEW paper database and point the "
                              "configuration at it; the existing database is "
                              "left untouched"))
    args = parser.parse_args(argv)

    from app import runtime

    if args.diagnostics:
        return _diagnostics()
    if args.new_paper_account:
        return _new_paper_account()

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
    if argv and argv[0] in ("--cli", "--run", "--diagnostics",
                            "--new-paper-account"):
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
    """Last-resort visibility for a WINDOWED frozen build on Windows.

    A windowed executable has no console to keep open, so a startup failure
    would otherwise be completely invisible. ctypes talks to user32 directly,
    which needs no extra bundled dependency (tkinter is deliberately excluded
    from the build). On any other platform the durable log in
    ``<home>/logs/arena.log`` already carries the reason.
    """
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
