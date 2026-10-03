"""Run a real CLI command on behalf of the console.

Design rules, in order of importance:

1. **The console is a front end, not a second implementation.** It invokes the
   same ``python -m app ...`` / ``python run.py ...`` entry points an operator
   would type, as a child process. Every gate, lock, mode check and refusal
   therefore behaves identically to the terminal. The console cannot enable
   anything the CLI would refuse.

2. **No shell, ever.** The argument vector is passed as a LIST to
   ``subprocess`` with ``shell=False``, so there is no command string to
   inject into and no quoting to get wrong. A flag is only ever a single argv
   element.

3. **No environment injection.** The child inherits the server's environment
   unchanged. The console deliberately offers no way to set a variable, so it
   cannot be used to flip a mode switch that the operator did not already have
   set. In particular it cannot enable live trading.

4. **Bounded.** Every run has a timeout and a process group that is killed on
   expiry, so a runaway or interactive command cannot wedge the server.

5. **One at a time.** A single-flight lock serialises runs. Several commands
   write to the same SQLite database, and letting the console stampede them
   would produce lock errors that look like application bugs.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from app.console.catalog import cli_commands, run_modes

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 900
MAX_OUTPUT_CHARS = 200_000

_run_lock = threading.Lock()


@dataclass
class RunResult:
    ok: bool
    program: str
    argv: list[str]
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    truncated: bool
    refused: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


class ConsoleError(RuntimeError):
    """Raised for requests the console refuses before running anything."""


def _terminate_tree(process: subprocess.Popen, *, force: bool) -> None:
    """Stop a command and everything it started.

    POSIX: the child was launched with start_new_session=True, so it leads its
    own process group and the whole tree can be signalled at once — important
    because a command like `engine` may have spawned threads or helpers that
    would otherwise keep the SQLite lock.

    Windows: there is no process-group signal. `os.killpg` does not exist at
    all there, so calling it raised AttributeError the first time a timeout
    fired. `taskkill /T` is the supported way to take down a tree.
    """
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True,
            check=False,
        )
        return

    signal_number = signal.SIGKILL if force else signal.SIGTERM
    try:
        os.killpg(os.getpgid(process.pid), signal_number)
    except (ProcessLookupError, PermissionError):
        # The process is already gone, or we may not signal it: fall back to
        # the single-process API rather than letting the timeout handler raise.
        process.kill() if force else process.terminate()


def _allowed() -> dict[str, set[str]]:
    """Command allowlist, derived from the live parsers on every call."""
    allowed = {"app": {c.name for c in cli_commands()}}
    allowed["run"] = {c.name for c in run_modes()}
    return allowed


def build_argv(program: str, command: str, args: list[str]) -> list[str]:
    """Translate a (program, command, args) request into a real argv list.

    Raises ConsoleError if the program or command is not one this platform
    actually offers — the allowlist comes from the parsers, so the console can
    only ever launch a feature that exists.
    """
    if program not in {"app", "run"}:
        raise ConsoleError(f"unknown entry point {program!r}")

    allowed = _allowed()
    if command not in allowed[program]:
        raise ConsoleError(
            f"{command!r} is not a {program!r} command; "
            f"valid: {', '.join(sorted(allowed[program]))}"
        )

    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise ConsoleError("args must be a list of strings")

    for arg in args:
        if "\x00" in arg:
            raise ConsoleError("arguments may not contain NUL bytes")
        if len(arg) > 4000:
            raise ConsoleError("argument too long")

    # Packaged builds: `sys.executable` is Arena.exe, which cannot be asked to
    # run `-m app`. It re-enters itself through the launcher's --cli/--run
    # switches instead, so the console keeps working identically inside a
    # one-file executable.
    if getattr(sys, "frozen", False):
        if program == "app":
            return [sys.executable, "--cli", command, *args]
        return [sys.executable, "--run", command, *args]

    if program == "app":
        return [sys.executable, "-m", "app", command, *args]
    return [sys.executable, str(REPO_ROOT / "run.py"), "--mode", command, *args]


def run_command(
    program: str,
    command: str,
    args: list[str] | None = None,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    cwd: Path | None = None,
) -> RunResult:
    """Run one command to completion and capture everything it printed."""
    argv = build_argv(program, command, args or [])

    timeout = max(1, min(int(timeout), MAX_TIMEOUT))
    started = time.perf_counter()

    if not _run_lock.acquire(blocking=False):
        raise ConsoleError(
            "another command is already running; wait for it to finish"
        )
    try:
        try:
            process = subprocess.Popen(
                argv,
                cwd=str(cwd or REPO_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                # A fresh process group lets the timeout kill the whole tree
                # (uvicorn workers, engine threads) rather than just the
                # parent, which would leave orphans holding the database.
                start_new_session=True,
                env=os.environ.copy(),
            )
        except OSError as exc:  # pragma: no cover - environment failure
            raise ConsoleError(f"could not start the command: {exc}") from exc

        timed_out = False
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_tree(process, force=False)
            try:
                stdout, stderr = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover
                _terminate_tree(process, force=True)
                stdout, stderr = process.communicate()

        duration_ms = int((time.perf_counter() - started) * 1000)
    finally:
        _run_lock.release()

    truncated = False
    if len(stdout) > MAX_OUTPUT_CHARS:
        stdout, truncated = stdout[:MAX_OUTPUT_CHARS], True
    if len(stderr) > MAX_OUTPUT_CHARS:
        stderr, truncated = stderr[:MAX_OUTPUT_CHARS], True

    exit_code = process.returncode
    if timed_out:
        stderr = (
            stderr
            + f"\n[console] stopped after {timeout}s; the command was still running."
        ).strip()

    return RunResult(
        ok=(exit_code == 0 and not timed_out),
        program=program,
        argv=argv,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_ms=duration_ms,
        timed_out=timed_out,
        truncated=truncated,
    )
