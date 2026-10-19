"""End-to-end verification of the ACTUAL BUILT executable for the paper
account startup failure.

A source checkout cannot prove what a double-clicked executable does with an
incompatible saved paper account: the frozen runtime has its own working
directory, its own data directory and its own console. This module drives the
real artifact through the exact incident:

 1. the artifact starts in a FRESH external home and creates a paper account;
 2. the configuration is changed so the saved account no longer matches
    (this is what the setup screen does when DEFAULT_TIMEFRAME is edited);
 3. the artifact is started again and must REFUSE — with the reported
    message and exit code 3 — instead of crashing with a traceback;
 4. the refused account must be untouched: same file, same account, same
    orders and fills;
 5. the documented recovery creates a SEPARATE EMPTY paper account, keeps
    the original database intact, and persists the choice;
 6. the artifact starts again on the recovered installation.

Nothing here fakes a result: every check observes the real process or reads
the real database file. Stdlib only, so it runs on any build machine
(Windows included — the Windows CI job runs it against Arena.exe).
"""

from __future__ import annotations

import json
import os
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _log(msg: str, verbose: bool) -> None:
    if verbose:
        print(f"  [frozen-recovery-e2e] {msg}", flush=True)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _port_is_free(port: int) -> bool:
    """True when nothing is listening on the port any more."""
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _mentions_path(text: str, path: Path) -> bool:
    """Whether a log mentions this path, in any of the spellings a log uses.

    A Windows path is written with backslashes and a JSON log escapes them,
    so the comparison is done on a normalised form: forward slashes, no
    repeated separators, lower case. The file name alone is also accepted,
    because the recovery database has a unique name.
    """
    def normalise(value: str) -> str:
        value = value.replace("\\", "/")
        value = re.sub(r"/+", "/", value)
        return value.lower()

    haystack = normalise(text)
    return normalise(str(path)) in haystack or path.name.lower() in haystack


def _get(base: str, path: str, timeout: float = 10.0):
    with urllib.request.urlopen(base + path, timeout=timeout) as response:
        return response.status, response.read().decode("utf-8")


def _post(base: str, path: str, payload: dict, timeout: float = 60.0):
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        base + path, data=data, method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def _wait_health(base: str, timeout: float = 120.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status, _ = _get(base, "/health", timeout=2)
            if status == 200:
                return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.4)
    return False


def _read_paper_state(database: Path) -> dict | None:
    """The saved paper account, read directly from the file."""
    if not database.exists():
        return None
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=5)
    try:
        row = connection.execute(
            "SELECT payload FROM state WHERE key='paper'").fetchone()
    except sqlite3.DatabaseError:
        return None
    finally:
        connection.close()
    return json.loads(row[0]) if row else None


def _count_rows(database: Path, table: str) -> int:
    if not database.exists():
        return -1
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=5)
    try:
        return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.DatabaseError:
        return -1
    finally:
        connection.close()


class FrozenRecoveryE2E:
    def __init__(self, artifact: Path, *, workdir: Path, verbose: bool = True):
        self.artifact = Path(artifact)
        self.workdir = Path(workdir)
        self.verbose = verbose
        self.results: list[dict] = []
        self.home = self.workdir / "home"
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.output_log = self.workdir / "arena-process.log"

    # ------------------------------------------------------------- helpers
    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.results.append({"name": name, "ok": bool(ok), "detail": detail})
        _log(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""),
             self.verbose)
        return bool(ok)

    def _env(self) -> dict:
        env = dict(os.environ)
        env["ARENA_HOME"] = str(self.home)
        env["ARENA_PORT"] = str(self.port)
        # A modal dialog would block a headless build machine; the same
        # message still reaches stdout and the durable log.
        env["ARENA_SUPPRESS_FATAL_DIALOG"] = "1"
        for key in ("DATABASE_URL", "DATA_DIR", "API_TOKEN"):
            env.pop(key, None)
        return env

    def _launch_dir(self) -> Path:
        # Neither the repository nor the home: the double-click situation.
        launch = self.workdir / "launchdir"
        launch.mkdir(parents=True, exist_ok=True)
        return launch

    def start(self, *extra: str) -> subprocess.Popen:
        log = open(self.output_log, "ab")
        # A frozen one-file build runs the application in a CHILD process.
        # On Windows the child belongs to the parent's process group only if
        # the group is created explicitly, so the whole tree can be stopped
        # later instead of leaving a server behind holding the port and the
        # session lock.
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
        try:
            return subprocess.Popen(
                [str(self.artifact), *extra],
                cwd=str(self._launch_dir()), env=self._env(),
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                creationflags=flags)
        finally:
            # Popen duplicated the handle into the child; release our copy so
            # the log is flushed and closed deterministically.
            log.close()

    def stop(self, process: subprocess.Popen) -> None:
        """Stop the artifact and everything it started.

        A frozen one-file build runs the application in a CHILD process, so
        on Windows the whole tree is stopped: signalling only the parent
        would leave the server running, holding the port and the session
        lock, and the next phase could not start.
        """
        if process.poll() is None:
            try:
                if os.name == "nt":
                    # Ctrl+Break reaches the whole process group; the
                    # launcher treats it as a clean stop.
                    process.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    process.terminate()
                process.wait(timeout=30)
            except (OSError, subprocess.TimeoutExpired):
                if os.name == "nt":
                    # Kill the whole tree: the bootloader AND its child.
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                        capture_output=True, timeout=120, check=False)
                else:
                    process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
        self._wait_port_free()

    def _wait_port_free(self, timeout: float = 60.0) -> bool:
        """Wait until the artifact released the port it was serving on."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if _port_is_free(self.port):
                return True
            time.sleep(0.25)
        return False

    def _read_log(self) -> str:
        try:
            return self.output_log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def _reset_log(self) -> None:
        """Start a phase with an empty log, so a check reads THIS launch only."""
        try:
            self.output_log.write_text("", encoding="utf-8")
        except OSError:
            pass

    # --------------------------------------------------------------- phases
    def run(self) -> dict:
        started = time.monotonic()
        try:
            self._phase_create_account()
            self._phase_refusal()
            self._phase_recovery()
            self._phase_restart_after_recovery()
        finally:
            self._cleanup()
        elapsed = time.monotonic() - started
        ok = all(r["ok"] for r in self.results)
        return {"ok": ok, "elapsed_seconds": round(elapsed, 1),
                "artifact": str(self.artifact), "results": self.results,
                "original_database": str(self.database)
                if getattr(self, "database", None) else "",
                "recovery_database": str(self.selected)
                if getattr(self, "selected", None) else ""}

    def _phase_create_account(self) -> None:
        """A first run that really saves a paper account."""
        self.home.mkdir(parents=True, exist_ok=True)
        process = self.start("--no-browser")
        healthy = _wait_health(self.base, timeout=180)
        if not self.check("artifact starts in a fresh external home",
                          healthy, self._read_log()[-300:]):
            raise RuntimeError("artifact did not become healthy")
        try:
            status, _ = _post(self.base, "/api/wizard/prepare-paper",
                               {"data_source": "replay"})
            self.check("wizard prepares the paper environment", status == 200,
                       f"status={status}")
        except (urllib.error.URLError, OSError) as exc:
            self.check("wizard prepares the paper environment", False, str(exc))

        database = self.home / "trading_bot.db"
        # The account must be ON DISK, not merely in memory: the harness
        # waits for the saved state instead of trusting the shutdown path,
        # so the result is the same on every platform.
        state = None
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            state = _read_paper_state(database)
            if state and state.get("timeframe") == "1D":
                break
            time.sleep(0.5)
        self.stop(process)
        time.sleep(1.0)

        state = _read_paper_state(database) or state
        self.check("a paper account is saved in the external home",
                   bool(state) and state.get("timeframe") == "1D",
                   f"timeframe={(state or {}).get('timeframe')}")
        self.database = database
        self.account_timeframe = (state or {}).get("timeframe")

    def _phase_refusal(self) -> None:
        """The reported failure: the artifact must refuse, not crash."""
        self._reset_log()
        env_file = self.home / ".env"
        text = env_file.read_text(encoding="utf-8")
        if "DEFAULT_TIMEFRAME=1D" in text:
            updated = text.replace("DEFAULT_TIMEFRAME=1D", "DEFAULT_TIMEFRAME=1m")
        else:
            updated = text.rstrip("\n") + "\nDEFAULT_TIMEFRAME=1m\n"
        env_file.write_text(updated, encoding="utf-8")

        process = self.start("--no-browser")
        try:
            code = process.wait(timeout=180)
        except subprocess.TimeoutExpired:
            self.stop(process)
            code = None
        output = self._read_log()
        self.check("an incompatible saved account exits with code 3", code == 3,
                   f"exit={code}")
        self.check("the refusal names the differing field",
                   "Differing fields: timeframe" in output,
                   "expected 'Differing fields: timeframe'")
        self.check("the refusal names the database",
                   "trading_bot.db" in output,
                   "expected the database path")
        self.check("the refusal states that nothing was changed",
                   "was not restored or changed" in output,
                   "expected the no-change statement")
        self.check("the refusal points at the recovery command",
                   "recover-paper-account" in output,
                   "expected the recovery command")
        self.check("no server was started", "Uvicorn running" not in output,
                   "the artifact must refuse before serving")

        # The account must be exactly where it was.
        state = _read_paper_state(self.database)
        self.check("the refused account is untouched",
                   bool(state) and state.get("timeframe") == self.account_timeframe,
                   f"timeframe={(state or {}).get('timeframe')}")
        self.check("the original database file still exists",
                   self.database.exists(), str(self.database))

    def _phase_recovery(self) -> None:
        """The documented recovery, through the real executable."""
        self._reset_log()
        before = self.database.read_bytes()
        original_state = _read_paper_state(self.database) or {}
        original_orders = {str(order.get("id"))
                           for order in original_state.get("orders", [])}
        original_fills = {str(fill.get("id"))
                          for fill in original_state.get("fills", [])}
        process = self.start("--recover-paper-account",
                             "--confirm-new-empty-paper-account", "--no-browser")
        pointer = self.home / "paper-database-override.json"
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline and not pointer.exists():
            if process.poll() is not None:
                break
            time.sleep(0.25)
        self.check("the recovery writes a durable selection", pointer.exists(),
                   self._read_log()[-300:])
        # The recovery continues into a normal start, so the selection is
        # only proven when the recovered installation actually serves.
        healthy = _wait_health(self.base, timeout=180)
        self.check("the recovered installation serves after the recovery",
                   healthy, self._read_log()[-300:])
        self.stop(process)
        time.sleep(1.0)

        output = self._read_log()
        self.check("the recovery reports a separate empty account",
                   "Separate PAPER database selected." in output, output[-300:])
        self.check("the recovery states that nothing was copied",
                   "No orders or fills were copied or placed." in output,
                   output[-300:])

        self.check("the original database is byte-for-byte unchanged",
                   self.database.read_bytes() == before)
        state = _read_paper_state(self.database)
        self.check("the original account still holds its timeframe",
                   bool(state) and state.get("timeframe") == self.account_timeframe)

        if pointer.exists():
            payload = json.loads(pointer.read_text(encoding="utf-8"))
            selected = Path(payload["database_path"])
            self.check("the selected database is a new, separate file",
                       selected.exists() and selected != self.database,
                       str(selected))
            # The new account is a DIFFERENT account: it starts empty, so
            # none of the original account's orders or fills may appear in
            # it. (Once the recovered installation serves, it holds its own
            # fresh account - that is the point of the recovery.)
            selected_state = _read_paper_state(selected) or {}
            selected_orders = {str(order.get("id"))
                               for order in selected_state.get("orders", [])}
            selected_fills = {str(fill.get("id"))
                              for fill in selected_state.get("fills", [])}
            copied_orders = original_orders & selected_orders
            copied_fills = original_fills & selected_fills
            self.check(
                "the selected database holds none of the original account's "
                "orders or fills",
                not copied_orders and not copied_fills,
                f"orders={len(copied_orders)} fills={len(copied_fills)} "
                f"of {len(original_orders)}/{len(original_fills)}",
            )
            self.selected = selected
        else:
            self.selected = None

    def _phase_restart_after_recovery(self) -> None:
        """The recovered installation must start again."""
        self._reset_log()
        process = self.start("--no-browser")
        healthy = _wait_health(self.base, timeout=180)
        self.check("the recovered installation starts and serves",
                   healthy, self._read_log()[-300:])
        if healthy:
            status, body = _get(self.base, "/health")
            try:
                payload = json.loads(body)
            except ValueError:
                payload = {}
            self.check("/health answers after the recovery",
                       status == 200 and payload.get("status") == "ok",
                       f"status={status}")
        self.stop(process)
        time.sleep(0.5)

        # The database actually in use is the recovered one. The durable
        # startup diagnostics are written to <home>/logs/arena.log.
        log = self._read_log()
        durable = ""
        try:
            durable = (self.home / "logs" / "arena.log").read_text(
                encoding="utf-8", errors="replace")
        except OSError:
            pass
        if self.selected is not None:
            used = _mentions_path(log, self.selected) or _mentions_path(durable,
                                                                        self.selected)
            detail = str(self.selected)
            if not used:
                # Say what the runtime actually reported, so a failure names
                # the file that was opened instead of the one expected.
                for line in durable.splitlines():
                    if "startup_paths" in line:
                        detail = line[-400:]
            self.check("the recovered database is the one in use", used, detail)
        self.check("the original account is still readable after the restart",
                   bool(_read_paper_state(self.database)))

    def _cleanup(self) -> None:
        for name in ("instance.lock", "instance.json"):
            try:
                (self.home / name).unlink(missing_ok=True)
            except OSError:
                pass


def run_frozen_recovery_e2e(artifact: Path, workdir: Path,
                            verbose: bool = True) -> dict:
    """Run the packaged recovery verification and return a report."""
    workdir = Path(workdir)
    if workdir.exists():
        import shutil

        shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True, exist_ok=True)
    harness = FrozenRecoveryE2E(artifact, workdir=workdir, verbose=verbose)
    try:
        return harness.run()
    except Exception as exc:  # a crash is a failed check, never a silent pass
        harness.check("the packaged recovery verification completed", False,
                      f"{type(exc).__name__}: {exc}")
        return {"ok": False, "artifact": str(artifact), "results": harness.results}


if __name__ == "__main__":
    artifact = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "dist" / "Arena"
    target = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "build" / "recovery-e2e-workdir"
    report = run_frozen_recovery_e2e(artifact, target)
    for entry in report["results"]:
        print(("PASS " if entry["ok"] else "FAIL ") + entry["name"]
              + (f" — {entry['detail']}" if entry["detail"] else ""))
    raise SystemExit(0 if report["ok"] else 1)
