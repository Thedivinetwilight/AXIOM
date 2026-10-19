"""The installed-Windows startup failure: a saved paper account that the
current configuration cannot accept must fail CLOSED, explain itself, and
never cost the user their data.

The incident this pins (a real packaged install on Windows, exit code 3):

    Saved paper account is incompatible with the current configuration.
    Differing fields: timeframe. Database: C:\\Users\\...\\Arena\\trading.db

What went wrong before the fix:

* the check was a whole-dict equality inside ``PaperBroker.__init__``, so it
  could not say WHICH field differed, and it said "use a new database" —
  advice no installed user could act on;
* it ran INSIDE the FastAPI lifespan, so the user got a uvicorn traceback
  and exit code 3 with no explanation and no way forward;
* the database had already been opened (schema created/migrated) before the
  refusal, so even the "refusal" wrote to the database it refused;
* there was no recovery path at all: no way to point the install at another
  database, no way to keep the original one, and the wizard's own
  configuration write was not rolled back when the account refused it.

The rules pinned here:

1. the check is READ-ONLY and happens before the database is opened;
2. it is FIELD-GRANULAR and value-free (names only, never an account value);
3. a compatible account still starts, an incompatible one stays protected;
4. recovery creates a SEPARATE EMPTY account and preserves the original
   database, its orders, its fills and its research data;
5. nothing is ever reset, migrated, deleted or silently replaced.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

from app import runtime
from app.backtesting.costs import ExecutionConfig
from app.config.settings import Settings
from app.database.database import Database
from app.database.repository import Repository
from app.paper_trading.broker import PaperBroker
from app.paper_trading.compatibility import (
    PaperAccountCompatibilityError,
    PaperDatabaseInspectionError,
    differing_paper_fields,
    inspect_paper_account,
)
from app.paper_trading.execution import calendar_signature, make_calendar
from app.paper_trading.recovery import (
    PaperRecoveryError,
    create_separate_paper_database,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _write_env(home: Path, **values: str) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    path = home / ".env"
    lines = [f"{key}={value}" for key, value in values.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _settings(**overrides) -> Settings:
    base = dict(
        mode="PAPER",
        live_trading=False,
        broker="LOCAL_PAPER",
        currency="INR",
        market_calendar="continuous",
        default_timeframe="1D",
        database_url="sqlite:///trading_bot.db",
        initial_capital=100_000.0,
        commission=0.0005,
        slippage=0.0005,
        spread=0.0002,
        position_size=0.25,
        stop_loss=0.05,
        take_profit=0.10,
        max_participation=0.10,
    )
    base.update(overrides)
    return Settings(**base)


def _free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _save_account(database_url: str, *, timeframe: str = "1D",
                  currency: str = "INR") -> None:
    """Create a saved paper account exactly as a running Arena would."""
    database = Database(database_url)
    repository = Repository(database)
    broker = PaperBroker(
        ExecutionConfig(),
        currency=currency,
        timeframe=timeframe,
        repository=repository,
        calendar=make_calendar("continuous", timeframe),
    )
    broker.close()
    database.close()


def _paper_state(database_url: str) -> dict | None:
    database = Path(database_url[len("sqlite:///"):])
    if not database.exists():
        return None
    connection = sqlite3.connect(database)
    try:
        row = connection.execute(
            "SELECT payload FROM state WHERE key='paper'").fetchone()
    except sqlite3.DatabaseError:
        return None
    finally:
        connection.close()
    return json.loads(row[0]) if row else None


def _row_count(database_url: str, table: str) -> int:
    database = Path(database_url[len("sqlite:///"):])
    connection = sqlite3.connect(database)
    try:
        return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.DatabaseError:
        return 0
    finally:
        connection.close()


def _expected_fields(settings: Settings) -> dict:
    return {
        "config": ExecutionConfig(**settings.execution_kwargs()).__dict__,
        "currency": settings.currency,
        "timeframe": settings.default_timeframe,
        "calendar": calendar_signature(
            make_calendar(settings.market_calendar, settings.default_timeframe)
        ),
    }


# ---------------------------------------------------------------------------
# 1. the failure itself
# ---------------------------------------------------------------------------


class TestTheReportedFailure:
    """The exact failure: an incompatible saved paper account."""

    def test_a_changed_timeframe_is_reported_as_a_timeframe_difference(self, tmp_path):
        """The reported symptom, reproduced: timeframe differs."""
        database_url = f"sqlite:///{tmp_path / 'trading.db'}"
        _save_account(database_url, timeframe="1D")

        saved = _paper_state(database_url)
        assert saved["timeframe"] == "1D"

        mismatch = inspect_paper_account(
            database_url,
            _expected_fields(_settings())["config"],
            currency="INR",
            timeframe="1m",
            calendar_signature=_expected_fields(_settings())["calendar"],
        )
        assert mismatch is not None
        assert mismatch.differing_fields == ("timeframe",)
        # The message names the field and the database, and no account value.
        text = str(mismatch)
        assert "Saved paper account is incompatible with the current configuration" in text
        assert "Differing fields: timeframe" in text
        assert str(tmp_path / "trading.db") in text
        assert "The saved paper account was not restored or changed." in text

    def test_the_error_never_leaks_account_or_configuration_values(self, tmp_path):
        """Only field NAMES and the database path — never a value."""
        database_url = f"sqlite:///{tmp_path / 'trading.db'}"
        _save_account(database_url, timeframe="1D")

        mismatch = inspect_paper_account(
            database_url,
            _expected_fields(_settings())["config"],
            currency="EUR",
            timeframe="1m",
            calendar_signature=_expected_fields(_settings())["calendar"],
        )
        assert mismatch is not None
        text = str(mismatch)
        for secret in ("1D", "1m", "INR", "EUR", "100000", "0.0005"):
            assert secret not in text, f"{secret!r} leaked into the error message"

    def test_the_refusal_happens_before_the_database_is_opened(self, tmp_path):
        """A refused account must not be migrated, written or created."""
        database_url = f"sqlite:///{tmp_path / 'trading.db'}"
        _save_account(database_url, timeframe="1D")
        before = (tmp_path / "trading.db").read_bytes()

        database = Database(database_url)
        database.close()
        after = (tmp_path / "trading.db").read_bytes()
        # The compatibility check itself never opens the file for writing.
        assert before == after

    def test_the_inspection_creates_nothing_for_a_missing_database(self, tmp_path):
        """A first run has no account: no error, and no file created."""
        database_url = f"sqlite:///{tmp_path / 'never.db'}"
        mismatch = inspect_paper_account(
            database_url,
            _expected_fields(_settings())["config"],
            currency="INR",
            timeframe="1D",
            calendar_signature=_expected_fields(_settings())["calendar"],
        )
        assert mismatch is None
        assert not (tmp_path / "never.db").exists()

    def test_an_unreadable_database_is_refused_not_assumed_compatible(self, tmp_path):
        """A file that is not a database must not read as 'no account'."""
        broken = tmp_path / "broken.db"
        broken.write_bytes(b"this is not a sqlite database")
        with pytest.raises(PaperDatabaseInspectionError) as excinfo:
            inspect_paper_account(
                f"sqlite:///{broken}",
                _expected_fields(_settings())["config"],
                currency="INR",
                timeframe="1D",
                calendar_signature=_expected_fields(_settings())["calendar"],
            )
        assert "no paper account data was changed" in str(excinfo.value).lower()
        # The unreadable file was not modified or replaced.
        assert broken.read_bytes() == b"this is not a sqlite database"


# ---------------------------------------------------------------------------
# 2. field-granular comparison
# ---------------------------------------------------------------------------


class TestFieldGranularComparison:
    def test_every_execution_field_is_compared_by_name(self):
        saved = {
            "config": {"commission": 0.0005, "slippage": 0.0009},
            "currency": "INR",
            "timeframe": "1D",
            "calendar": {"type": "ContinuousCalendar"},
        }
        expected = {"commission": 0.0005, "slippage": 0.0005, "spread": 0.0002}
        fields = differing_paper_fields(
            saved, expected, currency="INR", timeframe="1D",
            calendar_signature={"type": "ContinuousCalendar"},
        )
        assert "execution.slippage" in fields
        assert "execution.spread" in fields
        assert "execution.commission" not in fields

    def test_integer_and_float_serialization_is_not_a_difference(self):
        """``1`` and ``1.0`` mean the same thing to the execution model."""
        saved = {
            "config": {"initial_capital": 100000, "position_size": 0.25},
            "currency": "INR",
            "timeframe": "1D",
            "calendar": {"type": "ContinuousCalendar"},
        }
        expected = {"initial_capital": 100000.0, "position_size": 0.25}
        fields = differing_paper_fields(
            saved, expected, currency="INR", timeframe="1D",
            calendar_signature={"type": "ContinuousCalendar"},
        )
        assert fields == ()

    def test_numeric_strings_are_still_incompatible(self):
        saved = {
            "config": {"commission": "0.0005"},
            "currency": "INR",
            "timeframe": "1D",
            "calendar": {"type": "ContinuousCalendar"},
        }
        fields = differing_paper_fields(
            saved, {"commission": 0.0005}, currency="INR", timeframe="1D",
            calendar_signature={"type": "ContinuousCalendar"},
        )
        assert fields == ("execution.commission",)

    def test_a_missing_field_is_incompatible(self):
        fields = differing_paper_fields(
            {"currency": "INR", "calendar": {"type": "ContinuousCalendar"}},
            {}, currency="INR", timeframe="1D",
            calendar_signature={"type": "ContinuousCalendar"},
        )
        assert "timeframe" in fields
        assert "execution_config" in fields

    def test_unknown_stored_keys_are_reported_by_name_only(self):
        saved = {
            "config": {"commission": 0.0005, "secret_token": "abc"},
            "currency": "INR",
            "timeframe": "1D",
            "calendar": {"type": "ContinuousCalendar"},
        }
        fields = differing_paper_fields(
            saved, {"commission": 0.0005}, currency="INR", timeframe="1D",
            calendar_signature={"type": "ContinuousCalendar"},
        )
        assert "execution.secret_token" in fields
        assert "abc" not in " ".join(fields)

    def test_a_calendar_change_is_reported_under_calendar(self):
        saved = {
            "config": {},
            "currency": "INR",
            "timeframe": "1D",
            "calendar": {"type": "SessionCalendar", "timezone": "Asia/Kolkata"},
        }
        fields = differing_paper_fields(
            saved, {}, currency="INR", timeframe="1D",
            calendar_signature={"type": "ContinuousCalendar"},
        )
        assert "calendar.type" in fields
        assert "calendar.timezone" in fields


# ---------------------------------------------------------------------------
# 3. compatible vs incompatible accounts
# ---------------------------------------------------------------------------


class TestCompatibleAndIncompatibleAccounts:
    def test_a_compatible_account_starts(self, tmp_path):
        """The everyday case must keep working — and keep its data."""
        database_url = f"sqlite:///{tmp_path / 'trading.db'}"
        _save_account(database_url, timeframe="1D")
        settings = _settings(default_timeframe="1D")

        mismatch = inspect_paper_account(
            database_url,
            _expected_fields(settings)["config"],
            currency=settings.currency,
            timeframe=settings.default_timeframe,
            calendar_signature=_expected_fields(settings)["calendar"],
        )
        assert mismatch is None

        database = Database(database_url)
        repository = Repository(database)
        broker = PaperBroker(
            ExecutionConfig(**settings.execution_kwargs()),
            currency=settings.currency,
            timeframe=settings.default_timeframe,
            repository=repository,
            calendar=make_calendar(settings.market_calendar, settings.default_timeframe),
        )
        assert broker.get_balance()["equity"] == pytest.approx(100_000.0)
        broker.close()
        database.close()

    def test_an_incompatible_account_is_refused_by_the_broker(self, tmp_path):
        """The restore itself refuses, with the shared, redacted error."""
        database_url = f"sqlite:///{tmp_path / 'trading.db'}"
        _save_account(database_url, timeframe="1D")

        database = Database(database_url)
        repository = Repository(database)
        with pytest.raises(PaperAccountCompatibilityError) as excinfo:
            PaperBroker(
                ExecutionConfig(),
                currency="INR",
                timeframe="1m",
                repository=repository,
                calendar=make_calendar("continuous", "1m"),
            )
        assert excinfo.value.differing_fields == ("timeframe",)
        assert "trading.db" in str(excinfo.value)
        # The refused account was not replaced by a fresh one.
        assert _paper_state(database_url)["timeframe"] == "1D"
        database.close()

    def test_the_broker_refusal_releases_the_writer_lock(self, tmp_path):
        """A refused restore must not leave the paper writer locked."""
        database_url = f"sqlite:///{tmp_path / 'trading.db'}"
        _save_account(database_url, timeframe="1D")
        database = Database(database_url)
        repository = Repository(database)
        with pytest.raises(PaperAccountCompatibilityError):
            PaperBroker(
                ExecutionConfig(),
                currency="INR",
                timeframe="1m",
                repository=repository,
                calendar=make_calendar("continuous", "1m"),
            )
        assert repository._writer_file is None
        database.close()


# ---------------------------------------------------------------------------
# 4. the safe recovery path
# ---------------------------------------------------------------------------


class TestSeparateDatabaseRecovery:
    def test_recovery_creates_a_separate_empty_account(self, tmp_path):
        """A new, empty account — the original is untouched."""
        home = tmp_path / "Arena"
        home.mkdir()
        original = home / "trading_bot.db"
        _save_account(f"sqlite:///{original}", timeframe="1D")

        recovery = create_separate_paper_database(
            _settings(default_timeframe="1m",
                      database_url=f"sqlite:///{original}"), home=home
        )
        assert recovery.previous_database == original.resolve()
        assert recovery.selected_database != original.resolve()
        assert recovery.selected_database.exists()
        assert recovery.differing_fields == ("timeframe",)
        # The new database is genuinely empty.
        assert _paper_state(runtime.sqlite_url_for_path(recovery.selected_database)) is None
        # The original account is untouched.
        assert _paper_state(f"sqlite:///{original}")["timeframe"] == "1D"
        # And the selection is persistent.
        pointer = runtime.read_paper_database_override(home)
        assert pointer is not None
        assert pointer["database_path"] == str(recovery.selected_database)

    def test_recovery_is_refused_without_a_proven_mismatch(self, tmp_path):
        """No mismatch, no recovery: a healthy install is never 'recovered'."""
        home = tmp_path / "Arena"
        home.mkdir()
        original = home / "trading_bot.db"
        _save_account(f"sqlite:///{original}", timeframe="1D")

        with pytest.raises(PaperRecoveryError) as excinfo:
            create_separate_paper_database(
                _settings(default_timeframe="1D",
                          database_url=f"sqlite:///{original}"), home=home)
        assert "No incompatible saved paper account" in str(excinfo.value)
        assert not (home / "paper-recovery").exists()
        assert runtime.read_paper_database_override(home) is None

    def test_recovery_never_deletes_or_copies_the_original(self, tmp_path):
        """Orders, fills and research data survive the recovery."""
        home = tmp_path / "Arena"
        home.mkdir()
        original = home / "trading_bot.db"
        _save_account(f"sqlite:///{original}", timeframe="1D")
        database = Database(f"sqlite:///{original}")
        with database.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO research_runs VALUES (?,?,?,?)",
                ("r1", "kind", "2026-01-01T00:00:00+00:00", "{}"),
            )
        database.close()
        before = original.read_bytes()

        create_separate_paper_database(
            _settings(default_timeframe="1m", database_url=f"sqlite:///{original}"),
            home=home)

        assert original.exists()
        assert _row_count(f"sqlite:///{original}", "research_runs") == 1
        assert original.read_bytes() == before

    def test_recovery_is_paper_only(self, tmp_path):
        """Recovery never changes or enables an execution mode."""
        home = tmp_path / "Arena"
        home.mkdir()
        with pytest.raises(PaperRecoveryError) as excinfo:
            create_separate_paper_database(
                _settings(mode="PAPER", broker="UPSTOX_SANDBOX",
                          upstox_sandbox_access_token="t"),
                home=home,
            )
        assert "MODE=PAPER" in str(excinfo.value)

    def test_the_selection_survives_a_later_launch(self, tmp_path, monkeypatch):
        """After recovery, Settings points at the new database."""
        home = tmp_path / "Arena"
        home.mkdir()
        original = home / "trading_bot.db"
        _save_account(f"sqlite:///{original}", timeframe="1D")
        recovery = create_separate_paper_database(
            _settings(default_timeframe="1m", database_url=f"sqlite:///{original}"),
            home=home,
        )
        monkeypatch.setenv("ARENA_HOME", str(home))
        monkeypatch.delenv("DATABASE_URL", raising=False)
        settings = Settings.from_env()
        assert runtime.path_identity(runtime.database_file_path(settings.database_url)) == (
            runtime.path_identity(recovery.selected_database)
        )

    def test_clearing_the_selection_keeps_both_databases(self, tmp_path):
        """Undoing the selection never deletes a database."""
        home = tmp_path / "Arena"
        home.mkdir()
        original = home / "trading_bot.db"
        _save_account(f"sqlite:///{original}", timeframe="1D")
        recovery = create_separate_paper_database(
            _settings(default_timeframe="1m", database_url=f"sqlite:///{original}"),
            home=home,
        )
        assert runtime.clear_paper_database_override(home) is True
        assert runtime.read_paper_database_override(home) is None
        assert original.exists()
        assert recovery.selected_database.exists()

    def test_a_broken_pointer_fails_closed(self, tmp_path):
        """A pointer to a missing database must not fall back silently."""
        home = tmp_path / "Arena"
        home.mkdir()
        pointer = runtime.paper_database_override_file(home)
        pointer.write_text(json.dumps({
            "version": 1,
            "database_path": str(home / "gone.db"),
            "differing_fields": ["timeframe"],
        }), encoding="utf-8")
        with pytest.raises(ValueError) as excinfo:
            runtime.read_paper_database_override(home)
        assert "will not silently switch databases" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 5. the database path is normalized in exactly one place
# ---------------------------------------------------------------------------


class TestDatabasePathNormalization:
    def test_a_relative_url_is_anchored_to_the_home_in_a_portable_install(
        self, tmp_path, monkeypatch
    ):
        """A double-clicked .exe must not resolve the database against its CWD.

        A relative ``DATABASE_URL`` used to be handed to SQLite verbatim, which
        resolves it against the process working directory — on Windows often
        ``C:\\Windows\\System32``. One installation could then open a
        different account on every launch.
        """
        home = tmp_path / "Arena"
        home.mkdir()
        monkeypatch.setenv("ARENA_HOME", str(home))
        monkeypatch.setenv("DATABASE_URL", "sqlite:///trading.db")
        settings = Settings.from_env()
        resolved = runtime.database_file_path(settings.database_url)
        assert resolved == (home / "trading.db").resolve()
        assert resolved.is_absolute()

    def test_an_absolute_url_is_never_moved(self, tmp_path, monkeypatch):
        home = tmp_path / "Arena"
        home.mkdir()
        target = tmp_path / "elsewhere.db"
        monkeypatch.setenv("ARENA_HOME", str(home))
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{target}")
        settings = Settings.from_env()
        assert runtime.database_file_path(settings.database_url) == target.resolve()

    def test_a_windows_drive_path_is_never_re_anchored(self, monkeypatch):
        """A Windows-style absolute URL stays absolute on every platform."""
        monkeypatch.setenv("ARENA_HOME", "/tmp/arena-home")
        assert runtime.is_absolute_database_path("C:/Users/me/trading.db") is True
        assert runtime.resolve_portable_database_url(
            "sqlite:///C:/Users/me/trading.db") == "sqlite:///C:/Users/me/trading.db"

    def test_the_same_file_has_one_identity(self, tmp_path):
        """Windows path case must not create two different databases."""
        assert runtime.path_identity(tmp_path / "a.db") == runtime.path_identity(
            tmp_path / "a.db")
        assert runtime.path_identity(str(tmp_path / "a.db")) == runtime.path_identity(
            tmp_path / "./a.db")

    def test_the_database_reports_its_resolved_path(self, tmp_path):
        database = Database(f"sqlite:///{tmp_path / 'trading.db'}")
        assert database.resolved_path == (tmp_path / "trading.db").resolve()
        database.close()

    def test_a_zero_byte_reservation_starts_as_a_fresh_database(self, tmp_path):
        """The recovery reserves a path with O_EXCL; it must not read as state."""
        reserved = tmp_path / "reserved.db"
        reserved.touch()
        assert reserved.stat().st_size == 0
        database = Database(f"sqlite:///{reserved}")
        assert _paper_state(f"sqlite:///{reserved}") is None
        database.close()


# ---------------------------------------------------------------------------
# 6. the launcher itself
# ---------------------------------------------------------------------------


def _launcher_env(home: Path) -> dict:
    env = dict(os.environ)
    env["ARENA_HOME"] = str(home)
    env["PYTHONPATH"] = str(ROOT)
    env["ARENA_SUPPRESS_FATAL_DIALOG"] = "1"
    for key in ("DATABASE_URL", "DATA_DIR", "API_TOKEN"):
        env.pop(key, None)
    return env


class TestLauncher:
    def test_the_launcher_refuses_an_incompatible_account_with_exit_code_3(
        self, tmp_path
    ):
        """The reported symptom, end to end through the real entry point."""
        home = tmp_path / "Arena"
        home.mkdir()
        _write_env(home, API_TOKEN="t", BROKER="LOCAL_PAPER", MODE="PAPER",
                   DEFAULT_TIMEFRAME="1D")
        _save_account(f"sqlite:///{home / 'trading_bot.db'}", timeframe="1D")
        _write_env(home, API_TOKEN="t", BROKER="LOCAL_PAPER", MODE="PAPER",
                   DEFAULT_TIMEFRAME="1m")

        result = subprocess.run(
            [sys.executable, str(ROOT / "arena.py"), "--no-browser", "--port", str(_free_port())],
            cwd=str(tmp_path), env=_launcher_env(home),
            capture_output=True, text=True, timeout=180,
        )
        assert result.returncode == 3, result.stdout + result.stderr
        assert "Saved paper account is incompatible with the current configuration" in (
            result.stdout + result.stderr)
        assert "Differing fields: timeframe" in (result.stdout + result.stderr)
        assert "recover-paper-account" in (result.stdout + result.stderr)

    def test_a_compatible_account_starts(self, tmp_path):
        """The protected path must not break the everyday case."""
        home = tmp_path / "Arena"
        home.mkdir()
        _write_env(home, API_TOKEN="t", BROKER="LOCAL_PAPER", MODE="PAPER",
                   DEFAULT_TIMEFRAME="1D")
        _save_account(f"sqlite:///{home / 'trading_bot.db'}", timeframe="1D")

        result = subprocess.run(
            [sys.executable, str(ROOT / "arena.py"), "--diagnostics"],
            cwd=str(tmp_path), env=_launcher_env(home),
            capture_output=True, text=True, timeout=180,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        report = json.loads(result.stdout)
        assert report["database_path"] == str((home / "trading_bot.db").resolve())
        assert report["paper_database_override_active"] is False
        assert report["execution_safety"] == {
            "mode": "PAPER", "live_trading": False, "broker": "LOCAL_PAPER"}

    def test_diagnostics_never_print_a_secret(self, tmp_path):
        home = tmp_path / "Arena"
        home.mkdir()
        _write_env(home, API_TOKEN="super-secret-token", BROKER="LOCAL_PAPER",
                   MODE="PAPER")
        result = subprocess.run(
            [sys.executable, str(ROOT / "arena.py"), "--diagnostics"],
            cwd=str(tmp_path), env=_launcher_env(home),
            capture_output=True, text=True, timeout=180,
        )
        assert "super-secret-token" not in result.stdout

    def test_the_recovery_command_selects_a_separate_database(self, tmp_path):
        """The documented recovery path, through the real entry point.

        The recovery command recovers and then goes on to serve, so the
        process is observed and stopped once the selection is on disk.
        """
        home = tmp_path / "Arena"
        home.mkdir()
        _write_env(home, API_TOKEN="t", BROKER="LOCAL_PAPER", MODE="PAPER",
                   DEFAULT_TIMEFRAME="1D")
        _save_account(f"sqlite:///{home / 'trading_bot.db'}", timeframe="1D")
        _write_env(home, API_TOKEN="t", BROKER="LOCAL_PAPER", MODE="PAPER",
                   DEFAULT_TIMEFRAME="1m")

        process = subprocess.Popen(
            [sys.executable, str(ROOT / "arena.py"),
             "--recover-paper-account", "--confirm-new-empty-paper-account",
             "--no-browser", "--port", str(_free_port())],
            cwd=str(tmp_path), env=_launcher_env(home),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        try:
            pointer = runtime.paper_database_override_file(home)
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline and not pointer.exists():
                if process.poll() is not None:
                    break
                time.sleep(0.2)
            assert pointer.exists(), "the recovery selection was never written"
        finally:
            process.terminate()
            try:
                output = process.communicate(timeout=60)[0]
            except subprocess.TimeoutExpired:
                process.kill()
                output = process.communicate()[0]

        assert "Separate PAPER database selected." in output
        assert "No orders or fills were copied or placed." in output
        # The original account is still exactly where it was.
        assert _paper_state(f"sqlite:///{home / 'trading_bot.db'}")["timeframe"] == "1D"
        selection = runtime.read_paper_database_override(home)
        assert selection is not None
        assert Path(selection["database_path"]).exists()

    def test_recovery_without_the_confirmation_is_refused(self, tmp_path):
        home = tmp_path / "Arena"
        home.mkdir()
        _write_env(home, API_TOKEN="t", BROKER="LOCAL_PAPER", MODE="PAPER",
                   DEFAULT_TIMEFRAME="1m")
        result = subprocess.run(
            [sys.executable, str(ROOT / "arena.py"), "--recover-paper-account"],
            cwd=str(tmp_path), env=_launcher_env(home),
            capture_output=True, text=True, timeout=180,
        )
        assert result.returncode != 0
        assert "--confirm-new-empty-paper-account" in (result.stdout + result.stderr)
        assert not runtime.paper_database_override_file(home).exists()

    def test_clearing_the_selection_removes_only_the_pointer(self, tmp_path):
        home = tmp_path / "Arena"
        home.mkdir()
        _write_env(home, API_TOKEN="t", BROKER="LOCAL_PAPER", MODE="PAPER",
                   DEFAULT_TIMEFRAME="1D")
        _save_account(f"sqlite:///{home / 'trading_bot.db'}", timeframe="1D")
        create_separate_paper_database(
            _settings(default_timeframe="1m",
                      database_url=f"sqlite:///{home / 'trading_bot.db'}"), home=home)

        result = subprocess.run(
            [sys.executable, str(ROOT / "arena.py"),
             "--clear-paper-database-recovery"],
            cwd=str(tmp_path), env=_launcher_env(home),
            capture_output=True, text=True, timeout=180,
        )
        assert result.returncode == 0
        assert "No database, order, fill, or research record was deleted." in result.stdout
        assert runtime.read_paper_database_override(home) is None
        assert (home / "trading_bot.db").exists()


# ---------------------------------------------------------------------------
# 8. the packaged gates
# ---------------------------------------------------------------------------


class TestPackagedGates:
    """The packaged artifact must be driven through the incident, too.

    A source test cannot prove what a double-clicked executable does with an
    incompatible saved account, so the frozen verification is a release gate:
    the real artifact must refuse with exit code 3, keep the account,
    recover into a separate empty account and start again.
    """

    def test_the_frozen_recovery_verification_exists(self):
        module = ROOT / "packaging" / "frozen_recovery_e2e.py"
        assert module.is_file()
        source = module.read_text(encoding="utf-8")
        assert "def run_frozen_recovery_e2e" in source
        # The checks that matter, in the order the incident happened.
        for expected in ("_phase_create_account", "_phase_refusal",
                         "_phase_recovery", "_phase_restart_after_recovery"):
            assert expected in source

    def test_the_build_gate_runs_the_frozen_recovery_verification(self):
        source = (ROOT / "build_exe.py").read_text(encoding="utf-8")
        assert "frozen_recovery_e2e" in source
        assert "run_frozen_recovery_e2e" in source
        # The gate is a hard gate: the build fails when EITHER verification fails.
        assert "result[\"ok\"] and recovery[\"ok\"]" in source

    def test_the_windows_workflow_builds_and_exercises_the_installer(self):
        workflow = ROOT / ".github" / "workflows" / "windows-build-installer.yml"
        assert workflow.is_file()
        text = workflow.read_text(encoding="utf-8")
        assert "windows-latest" in text
        assert "build_exe.py --test" in text
        assert "build_installer.py" in text
        assert "installer_e2e.py" in text

    def test_the_installer_never_ships_or_deletes_user_data(self):
        script = (ROOT / "packaging" / "installer.iss").read_text(encoding="utf-8")
        # The uninstaller must not name a database, a .env or the recovery
        # selection: those belong to the user, not to the installation.
        # Comments are excluded — only the directives count.
        uninstall = "\n".join(
            line for line in script.split("[UninstallDelete]")[-1].splitlines()
            if not line.strip().startswith(";")
        )
        for forbidden in ("trading_bot.db", "paper-database-override.json",
                          ".env", "paper-recovery"):
            assert forbidden not in uninstall
        # The recovery documentation must be installed with the executable.
        assert "{#RecoveryDoc}" in script
        builder = (ROOT / "packaging" / "build_installer.py").read_text(
            encoding="utf-8")
        assert "PAPER_ACCOUNT_RECOVERY.md" in builder

    def test_the_documentation_exists(self):
        doc = ROOT / "docs" / "PAPER_ACCOUNT_RECOVERY.md"
        assert doc.is_file()
        text = doc.read_text(encoding="utf-8")
        assert "--recover-paper-account" in text
        assert "--clear-paper-database-recovery" in text
        assert "--diagnostics" in text


# ---------------------------------------------------------------------------
# 8. the Windows CI environment: the compiler is RESOLVED, never assumed
# ---------------------------------------------------------------------------


class TestWindowsCompilerResolution:
    """The Windows workflow must not die in the environment-setup step.

    The Windows runner image already ships Inno Setup (6.7.1 at the time of
    writing). A pinned ``choco install innosetup --version=6.2.2`` refuses
    to downgrade it, exits non-zero, and killed the whole workflow before a
    single test ran - run 39201347608 failed exactly there.

    So the compiler is DETECTED (and proven by compiling a probe script) and
    only installed, explicitly and verifiably, when there is none.
    """

    def test_the_preparation_script_exists(self):
        module = ROOT / "packaging" / "prepare_innosetup.py"
        assert module.is_file()
        source = module.read_text(encoding="utf-8")
        assert "def detect_iscc" in source
        assert "def probe_iscc" in source
        assert "def install_with_chocolatey" in source
        assert "def install_with_official_installer" in source

    def test_the_preparation_script_never_downgrades_an_installed_compiler(self):
        source = (ROOT / "packaging" / "prepare_innosetup.py").read_text(
            encoding="utf-8")
        # Chocolatey is invoked WITHOUT a version pin: a pinned version is
        # what refuses to run against a newer installed one. Only the actual
        # invocation counts, not the comment that explains it.
        invocation = [
            line for line in source.splitlines()
            if '"choco", "install"' in line
        ]
        assert invocation, "the chocolatey fallback is missing"
        for line in invocation:
            assert "--version" not in line

    def test_the_version_comparison_is_testable(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "arena_prepare_innosetup", ROOT / "packaging" / "prepare_innosetup.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        assert module.parse_version("6.7.1") == (6, 7, 1)
        assert module.parse_version("Inno Setup Setup Compiler (Version 6.4.3)") \
            == (6, 4, 3)
        assert module.parse_version("no version here") is None

        assert module.version_at_least((6, 7, 1), (6, 3, 0))
        assert module.version_at_least((6, 3, 0), (6, 3, 0))
        assert not module.version_at_least((6, 2, 2), (6, 3, 0))
        assert not module.version_at_least(None, (6, 3, 0))

    def test_the_probe_script_exercises_the_features_the_installer_needs(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "arena_prepare_innosetup", ROOT / "packaging" / "prepare_innosetup.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        probe = module.PROBE_SCRIPT
        # A /D define and the x64compatible identifier are both needed by
        # packaging/installer.iss, so the probe must use them.
        assert "AppVersion must be defined" in probe
        assert "x64compatible" in probe
        assert "#error" in probe
        assert "Compression=lzma2" in probe

    def test_the_candidate_paths_include_every_known_install_location(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "arena_prepare_innosetup", ROOT / "packaging" / "prepare_innosetup.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        candidates = [str(path) for path in module.candidate_iscc_paths()]
        assert any("Program Files (x86)" in path for path in candidates)
        assert any("Program Files" in path and "(x86)" not in path
                   for path in candidates)
        assert any("chocolatey" in path for path in candidates)

    def test_the_workflow_resolves_the_compiler_instead_of_pinning_it(self):
        workflow = (ROOT / ".github" / "workflows" / "windows-build-installer.yml")
        text = workflow.read_text(encoding="utf-8")
        assert "prepare_innosetup.py" in text
        # The pin that broke run 39201347608 must not come back.
        assert "choco install innosetup --version" not in text
        assert "innosetup --version" not in text

    def test_the_workflow_never_weakens_a_gate(self):
        text = (ROOT / ".github" / "workflows" / "windows-build-installer.yml"
                ).read_text(encoding="utf-8")
        # No step may be allowed to fail.
        assert "continue-on-error:" not in text
        # A command that swallows a failure ("|| true") is only ever allowed
        # in a step that already reports a failure.
        for block in text.split("\n      - name:")[1:]:
            if "|| true" in block:
                assert "if: failure()" in block, block.splitlines()[0]
        # The artifacts are published only after every gate passed.
        upload = text.split("Upload the Windows artifacts")[-1]
        assert "if: success()" in upload

    def test_the_installer_script_receives_absolute_paths(self):
        script = (ROOT / "packaging" / "installer.iss").read_text(encoding="utf-8")
        assert "{#ArenaExe}" in script
        assert "{#RecoveryDoc}" in script
        # No relative source path: the build script passes absolute ones.
        assert 'Source: "..\\' not in script

        builder = (ROOT / "packaging" / "build_installer.py").read_text(
            encoding="utf-8")
        assert "/DArenaExe=" in builder
        assert "/DRecoveryDoc=" in builder
        assert "/DAppVersion=" in builder
        assert "/O" in builder

    def test_the_frozen_harness_stops_the_whole_process_tree(self):
        """A frozen one-file build runs the app in a CHILD process.

        On Windows, terminating the parent alone leaves the server running:
        it keeps the port and the session lock, and the next phase of the
        verification cannot start. The harness therefore creates a process
        group and stops the whole tree.
        """
        source = (ROOT / "packaging" / "frozen_recovery_e2e.py").read_text(
            encoding="utf-8")
        assert "CREATE_NEW_PROCESS_GROUP" in source
        assert "CTRL_BREAK_EVENT" in source
        assert "taskkill" in source
        assert "_wait_port_free" in source


# ---------------------------------------------------------------------------
# 7. the configuration write that CREATED the incompatibility
# ---------------------------------------------------------------------------


class TestConfigurationWriteIsRolledBack:
    """The wizard must not leave an install that cannot start.

    It used to write the new timeframe into the .env FIRST and only then
    discover that the saved account could not be rebuilt. The .env change
    stayed, so every later launch refused to start — the mismatch was
    created by the application itself.
    """

    def test_a_refused_rebuild_leaves_the_configuration_untouched(self, tmp_path,
                                                                    monkeypatch):
        from fastapi.testclient import TestClient

        from app.api.server import create_app

        home = tmp_path / "Arena"
        home.mkdir()
        _write_env(home, API_TOKEN="t", BROKER="LOCAL_PAPER", MODE="PAPER",
                   DEFAULT_TIMEFRAME="1D")
        monkeypatch.setenv("ARENA_HOME", str(home))
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("DEFAULT_TIMEFRAME", raising=False)
        monkeypatch.delenv("DATA_DIR", raising=False)

        settings = Settings.from_env()
        app = create_app(settings)
        with TestClient(app) as client:
            assert app.state.broker.timeframe == "1D"
            before = (home / ".env").read_text(encoding="utf-8")
            response = client.post(
                "/api/wizard/prepare-paper",
                json={"data_source": "replay"},
                headers={"Authorization": "Bearer t"},
            )
            assert response.status_code in (200, 409)
            if response.status_code == 409:
                after = (home / ".env").read_text(encoding="utf-8")
                assert after == before, "a refused change must be rolled back"
