"""Exercise mismatch refusal and separate-database recovery in the real bundle.

Called after PyInstaller builds Arena.exe and again against the installed
binary after the Inno Setup installer is installed. It deliberately seeds a
temporary incompatible paper account, verifies safe field-only diagnostics,
recovers to a new empty database, restarts the executable, and probes the local
UI. No credentials or real orders are used.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _get_json(base: str, route: str, timeout: float = 4.0):
    try:
        with urllib.request.urlopen(base + route, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return 0, {}


def _get_text(base: str, route: str, timeout: float = 4.0):
    try:
        with urllib.request.urlopen(base + route, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError):
        return 0, ""


def _wait_health(base: str, process: subprocess.Popen, timeout: float = 90.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        status, payload = _get_json(base, "/health")
        if status == 200 and payload.get("status") == "ok":
            return True
        time.sleep(0.25)
    return False


def _clean_env(home: Path, port: int) -> dict[str, str]:
    env = dict(os.environ)
    # Do not inherit workstation or CI account credentials/configuration.
    sys.path.insert(0, str(ROOT))
    try:
        from app.config.settings import Settings
        from dataclasses import fields

        setting_names = {item.name.upper() for item in fields(Settings)}
    finally:
        try:
            sys.path.remove(str(ROOT))
        except ValueError:
            pass
    prefixes = ("UPSTOX", "MARKET_DATA", "LIVE_")
    for key in list(env):
        if key.startswith(prefixes) or key in setting_names or key in {
            "DATABASE_URL", "DATA_DIR", "ARENA_ENV_FILE", "ARENA_DATABASE_URL",
        }:
            env.pop(key, None)
    env.update({
        "ARENA_HOME": str(home),
        "ARENA_PORT": str(port),
        "MODE": "PAPER",
        "LIVE_TRADING": "false",
        "BROKER": "LOCAL_PAPER",
        # A frozen startup refusal normally shows a modal dialog when stdin is
        # detached; tests assert its exit code/output without blocking CI.
        "ARENA_SUPPRESS_FATAL_DIALOG": "1",
    })
    return env


def _seed_original_database(path: Path) -> dict:
    # This is local test data, generated in a private temporary workdir. The
    # frozen executable will inspect this file; the real account is never used.
    sys.path.insert(0, str(ROOT))
    try:
        from app.backtesting.costs import ExecutionConfig
        from app.backtesting.orders import Order
        from app.data.models import Candle
        from app.database.database import Database
        from app.database.repository import Repository
        from app.paper_trading.broker import PaperBroker
        from app.paper_trading.execution import ContinuousCalendar

        database = Database("sqlite:///" + path.as_posix())
        repository = Repository(database)
        broker = PaperBroker(
            ExecutionConfig(), timeframe="5m", calendar=ContinuousCalendar(),
            repository=repository,
        )
        timestamp = datetime(2024, 1, 1, tzinfo=timezone.utc)
        bar = Candle(timestamp, 100.0, 101.0, 99.0, 100.5, 10_000.0, "DEMO")
        order = Order("DEMO", "BUY", 2, timestamp.isoformat())
        broker.place_order(order)
        broker.on_bar(bar)
        order_id = order.id
        fill_id = broker.fills[0].id
        broker.close()
        with database.transaction() as connection:
            connection.execute(
                "INSERT INTO research_runs(id, kind, created_at, payload) "
                "VALUES (?, ?, ?, ?)",
                ("frozen-recovery-test", "test", timestamp.isoformat(),
                 json.dumps({"marker": "preserve"})),
            )
        database.close()
        return {"order_id": order_id, "fill_id": fill_id}
    finally:
        sys.path.pop(0)


def _snapshot(path: Path) -> dict:
    with sqlite3.connect(path) as connection:
        state = connection.execute(
            "SELECT payload FROM state WHERE key='paper'"
        ).fetchone()
        return {
            "paper": state[0] if state else None,
            "orders": connection.execute("SELECT id FROM orders ORDER BY id").fetchall(),
            "fills": connection.execute("SELECT id FROM fills ORDER BY id").fetchall(),
            "research": connection.execute(
                "SELECT id, payload FROM research_runs ORDER BY id"
            ).fetchall(),
        }


def _stop(process: subprocess.Popen, *, timeout: float = 30.0) -> bool:
    if process.poll() is not None:
        return process.returncode == 0
    graceful_signal_sent = False
    graceful_wait_completed = False
    try:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            process.send_signal(signal.SIGINT)
        graceful_signal_sent = True
        process.wait(timeout=timeout)
        graceful_wait_completed = True
    except (OSError, subprocess.TimeoutExpired):
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
    if process.returncode == 0:
        return True
    # The Windows PyInstaller one-file parent reports status 3 after a
    # delivered CTRL_BREAK_EVENT even though the child/server has exited. Only
    # accept that observed platform status when the signal was sent and the
    # process exited within the graceful wait; forced termination stays a fail.
    return (
        os.name == "nt"
        and graceful_signal_sent
        and graceful_wait_completed
        and process.returncode == 3
    )


def _start(executable: Path, home: Path, port: int, log_path: Path,
           *, recover: bool = False) -> subprocess.Popen:
    argv = [str(executable), "--no-browser", "--port", str(port)]
    if recover:
        argv[1:1] = [
            "--recover-paper-account",
            "--confirm-new-empty-paper-account",
        ]
    log = open(log_path, "ab")
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    try:
        return subprocess.Popen(
            argv,
            cwd=str(log_path.parent),
            env=_clean_env(home, port),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=flags,
        )
    finally:
        # Popen has duplicated the handle in its child on Windows; close our
        # parent handle now so flush/teardown is deterministic.
        log.close()


def run_frozen_recovery_e2e(executable: Path, workdir: Path, *, verbose: bool = True) -> dict:
    executable = Path(executable).resolve()
    workdir = Path(workdir).resolve()
    if not executable.is_file():
        raise FileNotFoundError(f"Frozen executable not found: {executable}")
    if workdir.exists():
        shutil.rmtree(workdir, ignore_errors=True)
    home = workdir / "user-home"
    launch = workdir / "launch"
    home.mkdir(parents=True, exist_ok=True)
    launch.mkdir(parents=True, exist_ok=True)
    original = home / "trading_bot.db"
    _seeded = _seed_original_database(original)
    before = _snapshot(original)
    original_digest = __import__("hashlib").sha256(original.read_bytes()).hexdigest()
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    log_path = workdir / "frozen-recovery.log"
    results: list[dict] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append({"name": name, "ok": bool(ok), "detail": detail})
        if verbose:
            print(f"  [{'PASS' if ok else 'FAIL'} recovery-e2e] {name}"
                  + (f" — {detail}" if detail else ""), flush=True)

    # Exercise the real frozen dotenv lookup order using only temporary,
    # credential-free DATABASE_URLs. The per-user file wins over the installed
    # executable-adjacent .env; removing it exposes the adjacent fallback.
    user_env = home / ".env"
    executable_env = executable.parent / ".env"
    user_env_before = user_env.read_bytes() if user_env.exists() else None
    executable_env_before = (
        executable_env.read_bytes() if executable_env.exists() else None
    )
    fallback_database = home / "executable-dotenv-fallback.db"
    try:
        user_env.write_text(
            f"DATABASE_URL=sqlite:///{original.as_posix()}\n", encoding="utf-8"
        )
        adjacent_text = (
            executable_env.read_text(encoding="utf-8")
            if executable_env.exists() else ""
        )
        adjacent_lines = [
            line for line in adjacent_text.splitlines()
            if line.partition("=")[0].strip() != "DATABASE_URL"
        ]
        adjacent_lines.append(
            f"DATABASE_URL=sqlite:///{fallback_database.as_posix()}"
        )
        executable_env.write_text("\n".join(adjacent_lines) + "\n", encoding="utf-8")
        dotenv_env = _clean_env(home, port)
        user_dotenv = subprocess.run(
            [str(executable), "--diagnostics"], cwd=str(launch), env=dotenv_env,
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60,
        )
        try:
            user_report = json.loads(user_dotenv.stdout)
        except (ValueError, TypeError):
            user_report = {}
        check(
            "frozen user .env overrides the executable-adjacent package .env",
            user_dotenv.returncode == 0
            and user_report.get("database_path") == str(original.resolve())
            and user_report.get("database_url_source")
                == f"first matching dotenv candidate: {user_env}",
        )
        user_env.unlink()
        adjacent_dotenv = subprocess.run(
            [str(executable), "--diagnostics"], cwd=str(launch), env=dotenv_env,
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60,
        )
        try:
            adjacent_report = json.loads(adjacent_dotenv.stdout)
        except (ValueError, TypeError):
            adjacent_report = {}
        check(
            "frozen executable-adjacent .env is used when the user .env is absent",
            adjacent_dotenv.returncode == 0
            and adjacent_report.get("database_path") == str(fallback_database.resolve())
            and adjacent_report.get("database_url_source")
                == f"first matching dotenv candidate: {executable_env}",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        check(
            "frozen packaged dotenv precedence is testable",
            False,
            type(exc).__name__,
        )
    finally:
        if user_env_before is None:
            user_env.unlink(missing_ok=True)
        else:
            user_env.write_bytes(user_env_before)
        if executable_env_before is None:
            executable_env.unlink(missing_ok=True)
        else:
            executable_env.write_bytes(executable_env_before)

    env = _clean_env(home, port)
    diag = subprocess.run(
        [str(executable), "--diagnostics"], cwd=str(launch), env=env,
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60,
    )
    try:
        paths = json.loads(diag.stdout)
    except (ValueError, TypeError):
        paths = {}
    check(
        "frozen diagnostics identify the exact active database path",
        diag.returncode == 0 and paths.get("database_path") == str(original.resolve()),
        "database path is reported without configuration values",
    )

    refused = subprocess.run(
        [str(executable), "--no-browser", "--port", str(port)],
        cwd=str(launch), env=env, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
    )
    refusal_text = (refused.stdout or "") + (refused.stderr or "")
    check(
        "incompatible frozen startup refuses without changing the account",
        refused.returncode == 3 and "Differing fields: timeframe" in refusal_text
        and str(original.resolve()) in refusal_text,
        f"exit={refused.returncode}; field names/path only",
    )
    check(
        "startup diagnostics do not expose paper values or order identifiers",
        "100000" not in refusal_text
        and _seeded["order_id"] not in refusal_text
        and _seeded["fill_id"] not in refusal_text,
    )
    check(
        "original paper orders, fills, research, and saved payload are unchanged",
        _snapshot(original) == before
        and __import__("hashlib").sha256(original.read_bytes()).hexdigest() == original_digest,
    )

    process = _start(executable, home, port, log_path, recover=True)
    first_health = _wait_health(base, process, timeout=120)
    check("explicit frozen recovery starts the local server", first_health,
          f"exit={process.poll()}")
    if first_health:
        status, health = _get_json(base, "/health")
        check(
            "recovered runtime remains PAPER / LOCAL_PAPER with live disabled",
            status == 200 and health.get("mode") == "PAPER"
            and health.get("live_trading") is False
            and health.get("broker") == "LOCAL_PAPER",
            json.dumps({key: health.get(key) for key in
                        ("mode", "live_trading", "broker")}, sort_keys=True),
        )
        status, landing = _get_text(base, "/")
        check("recovered local UI serves the paper-only landing page",
              status == 200 and "PAPER ONLY" in landing)
        status, setup = _get_text(base, "/setup")
        check("recovered local setup UI is served", status == 200 and "wizard-root" in setup)

    pointer_file = home / "paper-database-override.json"
    try:
        pointer = json.loads(pointer_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pointer = {}
    recovered = Path(pointer.get("database_path", "")) if pointer.get("database_path") else None
    check(
        "recovery selection is persisted at a separate user-data path",
        recovered is not None and recovered.is_file() and recovered != original
        and "paper-recovery" in recovered.parts,
        str(recovered) if recovered else "no persistent selection",
    )
    first_stop_ok = _stop(process)
    first_stop_detail = f"exit={process.returncode}"
    if os.name == "nt" and process.returncode == 3:
        first_stop_detail += " (Windows one-file CTRL_BREAK exit status)"
    check("recovery server exits after the shutdown signal", first_stop_ok,
          first_stop_detail)

    if recovered is not None and recovered.is_file():
        process2 = _start(executable, home, port, log_path, recover=False)
        restarted = _wait_health(base, process2, timeout=120)
        check("normal second launch reuses the persistent recovery database", restarted)
        if restarted:
            status, health = _get_json(base, "/health")
            check(
                "second launch is still PAPER / LOCAL_PAPER, never LIVE",
                status == 200 and health.get("mode") == "PAPER"
                and health.get("live_trading") is False
                and health.get("broker") == "LOCAL_PAPER",
            )
            status, landing = _get_text(base, "/")
            check("second-launch local UI remains available", status == 200
                  and "PAPER ONLY" in landing)
        second_stop_ok = _stop(process2)
        second_stop_detail = f"exit={process2.returncode}"
        if os.name == "nt" and process2.returncode == 3:
            second_stop_detail += " (Windows one-file CTRL_BREAK exit status)"
        check("second server exits after the shutdown signal", second_stop_ok,
              second_stop_detail)

        recovered_snapshot = _snapshot(recovered)
        try:
            state = json.loads(recovered_snapshot["paper"] or "{}")
        except ValueError:
            state = {}
        check(
            "new recovery database has no orders or fills",
            recovered_snapshot["orders"] == [] and recovered_snapshot["fills"] == []
            and state.get("orders") == [] and state.get("fills") == [],
        )
        check(
            "original data remains intact after recovery and restart",
            _snapshot(original) == before
            and __import__("hashlib").sha256(original.read_bytes()).hexdigest()
                == original_digest,
        )
    else:
        check("normal second launch reuses the persistent recovery database", False,
              "recovery pointer did not contain a valid DB path")
        check("second-launch local UI remains available", False)
        check("new recovery database has no orders or fills", False)
        check("original data remains intact after recovery and restart", False)

    log_text = log_path.read_text(encoding="utf-8", errors="replace") \
        if log_path.exists() else ""
    check("frozen recovery logs contain no seeded account/order identifiers",
          _seeded["order_id"] not in log_text and _seeded["fill_id"] not in log_text)

    return {
        "ok": all(item["ok"] for item in results),
        "results": results,
        "original_database": str(original),
        "recovery_database": str(recovered) if recovered else None,
    }


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: python packaging/frozen_recovery_e2e.py <Arena.exe> <workdir>")
        raise SystemExit(2)
    result = run_frozen_recovery_e2e(Path(sys.argv[1]), Path(sys.argv[2]))
    raise SystemExit(0 if result["ok"] else 1)
