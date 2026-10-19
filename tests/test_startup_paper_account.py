"""Startup regressions: paper-account mismatch, recovery, diagnostics.

These use the REAL launcher subprocess (`arena.py`, the same entry point the
frozen executable uses) against disposable homes. They pin the behaviour that
used to be a uvicorn traceback + exit 3:

* a saved paper account whose settings changed refuses with the exact field
  names, exit code 3, no traceback, and an UNCHANGED database and .env;
* a corrupt database refuses with exit 4 and is not modified;
* an invalid configuration refuses with exit 3 and no traceback;
* `--new-paper-account` creates a DISTINCT database, preserves the original
  byte-for-byte, updates only DATABASE_URL, and the next start succeeds;
* `--diagnostics` reports the resolved home/env/database and the account state
  truthfully (and never a secret);
* a relative DATABASE_URL is anchored to ARENA_HOME, not to the CWD.

No real broker, credential, or external network is involved.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "startup-test-token-not-a-secret"


# --------------------------------------------------------------------------- helpers

def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _env(home: Path) -> dict:
    env = dict(os.environ)
    env["ARENA_HOME"] = str(home)
    env["PYTHONPATH"] = str(ROOT)
    env["PYTHONUNBUFFERED"] = "1"
    for key in ("DATABASE_URL", "DATA_DIR", "API_TOKEN", "DEFAULT_TIMEFRAME",
                "MODE", "BROKER", "DATA_SOURCE", "DEFAULT_SYMBOL",
                "MARKET_DATA_ACCESS_TOKEN", "MARKET_DATA_SYMBOLS",
                "ARENA_ENV_FILE", "LIVE_TRADING", "LIVE_CONFIRM"):
        env.pop(key, None)
    return env


def _write_env(home: Path, *, timeframe: str = "1D",
               database_url: str | None = None) -> Path:
    path = home / ".env"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "API_TOKEN=" + TOKEN + "\n"
        "MODE=PAPER\nLIVE_TRADING=false\nBROKER=LOCAL_PAPER\n"
        "DATA_SOURCE=replay\nDEFAULT_SYMBOL=DEMO\n"
        f"DEFAULT_TIMEFRAME={timeframe}\n"
        "DATABASE_URL=" + (database_url
                           or f"sqlite:///{home / 'trading_bot.db'}") + "\n",
        encoding="utf-8",
    )
    return path


def _seed_paper(home: Path, *, timeframe: str = "1D") -> Path:
    """Create a database that already holds a paper account, as a real run does."""
    sys.path.insert(0, str(ROOT))
    from app.database.database import Database
    from app.database.repository import Repository
    from app.paper_trading.broker import PaperBroker
    from app.paper_trading.execution import make_calendar

    from app.backtesting.costs import ExecutionConfig
    from app.config.settings import Settings

    path = home / "trading_bot.db"
    database = Database(f"sqlite:///{path}")
    repository = Repository(database)
    # Match what the server builds: float-typed settings values, not the
    # dataclass literals (100000 vs 100000.0 compare equal, but the message
    # should read like a real account).
    broker = PaperBroker(ExecutionConfig(**Settings().execution_kwargs()),
                         timeframe=timeframe, repository=repository,
                         calendar=make_calendar("continuous", timeframe))
    broker.close()
    database.close()
    return path


def _run_launcher(home: Path, *args: str, timeout: int = 60,
                  cwd: str = "/") -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ROOT / "arena.py"), *args],
        cwd=cwd, env=_env(home), capture_output=True, text=True,
        timeout=timeout,
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _wait_health(base: str, timeout: float = 45.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(base + "/health", timeout=2) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.2)
    return False


def _start_and_stop(home: Path) -> tuple[int, str]:
    """Start the real launcher, prove /health, then stop it cleanly."""
    port = _free_port()
    log = home / "launcher.log"
    handle = log.open("wb")
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "arena.py"), "--no-browser",
         "--port", str(port)],
        cwd="/", env=_env(home), stdout=handle, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        assert _wait_health(f"http://127.0.0.1:{port}"), \
            log.read_text(errors="replace")[-2500:]
        process.send_signal(signal.SIGINT)
        process.wait(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        handle.close()
    return process.returncode, log.read_text(errors="replace")


# --------------------------------------------------------------------------- tests

class TestRefusedStart:
    def test_changed_timeframe_names_the_field_and_leaves_everything_alone(
            self, tmp_path):
        home = tmp_path / "Home"
        home.mkdir()
        database = _seed_paper(home, timeframe="1D")
        env_path = _write_env(home, timeframe="5m")
        before_db = _sha256(database)
        before_env = env_path.read_bytes()

        result = _run_launcher(home, "--no-browser")

        assert result.returncode == 3, result.stdout + result.stderr
        combined = result.stdout + result.stderr
        assert "DEFAULT_TIMEFRAME" in combined
        assert "saved '1D'" in combined and "configured '5m'" in combined
        assert "Traceback" not in combined
        assert "ValueError" not in combined
        assert "python arena.py --new-paper-account" in combined
        assert _sha256(database) == before_db, \
            "a refused start must not modify the database"
        assert env_path.read_bytes() == before_env
        assert not Path(str(database) + "-wal").exists()
        assert not Path(str(database) + "-shm").exists()

    def test_changed_initial_capital_is_identified(self, tmp_path):
        home = tmp_path / "Home"
        home.mkdir()
        _seed_paper(home)
        env_path = _write_env(home)
        text = env_path.read_text(encoding="utf-8").replace(
            "DEFAULT_TIMEFRAME=1D", "DEFAULT_TIMEFRAME=1D\nINITIAL_CAPITAL=50000")
        env_path.write_text(text, encoding="utf-8")

        result = _run_launcher(home, "--no-browser")
        combined = result.stdout + result.stderr
        assert result.returncode == 3
        assert "INITIAL_CAPITAL" in combined and "saved 100000.0" in combined
        assert "Traceback" not in combined

    def test_invalid_timeframe_is_a_clean_configuration_refusal(self, tmp_path):
        home = tmp_path / "Home"
        home.mkdir()
        env_path = _write_env(home, timeframe="2h")
        before = env_path.read_bytes()
        result = _run_launcher(home, "--no-browser")
        combined = result.stdout + result.stderr
        assert result.returncode == 3
        assert "Configuration error" in combined
        assert "DEFAULT_TIMEFRAME" in combined
        assert "Traceback" not in combined
        assert env_path.read_bytes() == before

    def test_corrupt_database_refuses_without_modifying_it(self, tmp_path):
        home = tmp_path / "Home"
        home.mkdir()
        _write_env(home)
        database = home / "trading_bot.db"
        database.write_bytes(b"this is not a sqlite database\n" * 16)
        before = _sha256(database)

        result = _run_launcher(home, "--no-browser")
        combined = result.stdout + result.stderr
        assert result.returncode == 4, combined
        assert "could not be read" in combined
        assert "Traceback" not in combined
        assert _sha256(database) == before

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission model")
    def test_unwritable_database_folder_refuses_cleanly(self, tmp_path):
        if os.geteuid() == 0:
            pytest.skip("root ignores directory permissions")
        home = tmp_path / "Home"
        home.mkdir()
        locked = home / "locked"
        locked.mkdir()
        _write_env(home, database_url=f"sqlite:///{locked / 'a.db'}")
        locked.chmod(0o500)
        try:
            result = _run_launcher(home, "--no-browser")
        finally:
            locked.chmod(0o700)
        combined = result.stdout + result.stderr
        assert result.returncode == 4, combined
        assert "not writable" in combined
        assert "Traceback" not in combined

    def test_relative_database_url_is_anchored_to_the_home_not_the_cwd(
            self, tmp_path):
        home = tmp_path / "Home"
        home.mkdir()
        _seed_paper(home)
        _write_env(home, database_url="sqlite:///relative.db")
        # Seed the SAME data into the home-relative file the launcher must use.
        import shutil

        shutil.copy2(home / "trading_bot.db", home / "relative.db")
        result = _run_launcher(home, "--diagnostics")
        payload = json.loads(result.stdout)
        assert payload["database_path"] == str((home / "relative.db"))
        assert payload["database_exists"] is True
        assert not Path("/relative.db").exists()

    def test_two_launches_never_share_one_home(self, tmp_path):
        """The second launch must not reach the paper-compat check at all."""
        home = tmp_path / "Home"
        home.mkdir()
        _seed_paper(home)
        _write_env(home)
        port = _free_port()
        log = (home / "first.log").open("wb")
        first = subprocess.Popen(
            [sys.executable, str(ROOT / "arena.py"), "--no-browser", "--port", str(port)],
            cwd="/", env=_env(home), stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            assert _wait_health(f"http://127.0.0.1:{port}")
            second = _run_launcher(home, "--no-browser", "--port", str(_free_port()))
            assert second.returncode == 0
            assert "already running" in (second.stdout + second.stderr).lower()
        finally:
            if first.poll() is None:
                first.send_signal(signal.SIGINT)
                try:
                    first.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    first.kill()
            log.close()


class TestRecovery:
    def test_new_paper_account_preserves_original_and_starts(self, tmp_path):
        home = tmp_path / "Home"
        home.mkdir()
        original = _seed_paper(home, timeframe="1D")
        env_path = _write_env(home, timeframe="5m")
        before = _sha256(original)
        before_env_lines = env_path.read_text(encoding="utf-8").splitlines()

        result = _run_launcher(home, "--new-paper-account")
        assert result.returncode == 0, result.stdout + result.stderr
        summary = json.loads(result.stdout)
        assert summary["verified_original_unchanged"] is True
        assert summary["original_sha256"] == before
        assert _sha256(original) == before
        new_db = Path(summary["new_database"])
        assert new_db.exists() and new_db != original
        assert summary["new_database_url"] == f"sqlite:///{new_db}"
        lines = env_path.read_text(encoding="utf-8").splitlines()
        # Every unrelated line is preserved; only DATABASE_URL changed.
        assert [line for line in lines if not line.startswith("DATABASE_URL=")] == \
               [line for line in before_env_lines if not line.startswith("DATABASE_URL=")]
        assert any(line.startswith("DATABASE_URL=sqlite:///") and
                   str(new_db) in line for line in lines)

        diagnostics = _run_launcher(home, "--diagnostics")
        payload = json.loads(diagnostics.stdout)
        assert payload["paper_account"]["status"] == "NO_ACCOUNT"
        assert payload["health"]["ok"] is True

        code, log = _start_and_stop(home)
        assert code == 0, log[-2000:]
        assert "Application startup complete" in log

    def test_recovery_refuses_when_database_is_only_in_the_process_environment(
            self, tmp_path):
        home = tmp_path / "Home"
        home.mkdir()
        _seed_paper(home)
        _write_env(home)
        env = _env(home)
        env["DATABASE_URL"] = f"sqlite:///{home / 'trading_bot.db'}"
        result = subprocess.run(
            [sys.executable, str(ROOT / "arena.py"), "--new-paper-account"],
            cwd="/", env=env, capture_output=True, text=True, timeout=60,
        )
        assert result.returncode != 0
        assert "process environment" in result.stdout
        assert "nothing to recover" not in result.stdout.lower()


class TestDiagnostics:
    def test_reports_paths_mode_and_never_a_token(self, tmp_path):
        home = tmp_path / "Home"
        home.mkdir()
        _write_env(home)
        result = _run_launcher(home, "--diagnostics")
        assert result.returncode == 0, result.stdout + result.stderr
        payload = json.loads(result.stdout)
        assert payload["arena_home"] == str(home)
        assert payload["env_file"] == str(home / ".env")
        assert payload["mode"] == "PAPER"
        assert payload["broker"] == "LOCAL_PAPER"
        assert payload["live_trading"] is False
        assert payload["database_path"] == str(home / "trading_bot.db")
        assert payload["paper_account"]["status"] == "NO_ACCOUNT"
        assert payload["api_token_configured"] is True
        assert TOKEN not in result.stdout
        assert "Bearer" not in result.stdout

    def test_reports_a_mismatch_without_refusing_to_diagnose(self, tmp_path):
        home = tmp_path / "Home"
        home.mkdir()
        _seed_paper(home)
        _write_env(home, timeframe="15m")
        result = _run_launcher(home, "--diagnostics")
        assert result.returncode == 0
        payload = json.loads(result.stdout)
        assert payload["paper_account"]["status"] == "MISMATCH"
        assert payload["paper_account"]["differences"][0]["field"] == "DEFAULT_TIMEFRAME"
        assert payload["health"]["ok"] is False

    def test_invalid_configuration_still_prints_structured_diagnostics(
            self, tmp_path):
        home = tmp_path / "Home"
        home.mkdir()
        _write_env(home, timeframe="nonsense")
        result = _run_launcher(home, "--diagnostics")
        assert result.returncode == 1
        payload = json.loads(result.stdout)
        assert "DEFAULT_TIMEFRAME" in payload["config_error"]
        assert payload["health"]["ok"] is False
