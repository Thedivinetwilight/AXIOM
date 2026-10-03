#!/usr/bin/env python3
"""Arena — one entry point that starts everything.

    python arena.py                 # start the server, open the console
    python arena.py --port 9000     # pick a port
    python arena.py --no-browser    # headless (servers, containers)
    python arena.py --cli live-status   # run any CLI command
    python arena.py --setup        # open the credential screen

This is the same file whether Arena runs from source or as a packaged
executable. When frozen it is the .exe's entry point, and it also acts as the
child-process re-entry point so the feature console can launch commands:
PyInstaller sets ``sys.executable`` to the .exe itself, which cannot be
invoked as ``python -m app``, so the console calls back here with ``--cli``.
"""

from __future__ import annotations

import argparse
import json
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
    """True when no configuration has been saved yet."""
    from app import runtime

    return not runtime.env_file().exists()


def _print_banner(url: str, setup: bool) -> None:
    from app.runtime import ensure_utf8_output

    ensure_utf8_output()
    target = f"{url}/setup" if setup else f"{url}/console"
    print()
    print("  " + "-" * 66)
    print("   ARENA — trading research platform")
    print("  " + "-" * 66)
    print(f"   Open this address:   {target}")
    if setup:
        print("   (first run: enter your credentials, then press Save)")
    print()
    print("   Keep this window open while you use Arena.")
    print("   Press Ctrl+C here to stop.")
    print("  " + "-" * 66)
    print(flush=True)


def _serve(host: str, port: int, open_browser: bool, force_setup: bool) -> int:
    import uvicorn

    from app import runtime
    from app.api.server import create_app
    from app.config.settings import Settings

    runtime.ensure_home()
    settings = Settings.from_env()
    application = create_app(settings)

    shown = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    url = f"http://{shown}:{port}"
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
            webbrowser.open(f"{url}/setup" if setup else f"{url}/console")

    threading.Thread(target=open_when_ready, daemon=True).start()
    _print_banner(url, setup)

    try:
        server.run()
    except KeyboardInterrupt:
        print("\n  Arena stopped.", flush=True)
    return 0


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
                        help="open the credential screen on this run")
    parser.add_argument("--diagnostics", action="store_true",
                        help="print where Arena keeps its files, then exit")
    args = parser.parse_args(argv)

    from app import runtime

    if args.diagnostics:
        print(json.dumps(runtime.describe(), indent=2))
        return 0

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


if __name__ == "__main__":
    raise SystemExit(main())
