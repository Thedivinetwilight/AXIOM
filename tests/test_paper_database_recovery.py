"""Paper-account startup diagnostics and explicit separate-DB recovery.

These tests use only temporary SQLite files. The incompatible account is
never reset, migrated, deleted, or copied into the recovery database.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from dataclasses import fields, replace
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app import runtime
from app.api.server import create_app
from app.backtesting.costs import ExecutionConfig
from app.backtesting.orders import Order
from app.config.settings import Settings
from app.data.models import Candle
from app.database.database import Database
from app.database.repository import Repository
from app.paper_trading.broker import PaperBroker
from app.paper_trading.compatibility import (
    PaperAccountCompatibilityError,
    differing_paper_fields,
    inspect_paper_account,
)
from app.paper_trading.execution import (
    ContinuousCalendar,
    SessionCalendar,
    calendar_signature,
)
from app.paper_trading.recovery import (
    PaperRecoveryError,
    create_separate_paper_database,
)


@pytest.fixture
def isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "Arena"
    home.mkdir()
    monkeypatch.setenv("ARENA_HOME", str(home))
    monkeypatch.delenv("ARENA_ENV_FILE", raising=False)
    monkeypatch.delenv("ARENA_DATABASE_URL", raising=False)
    # Do not read a workstation .env or inherit any execution/account setting.
    monkeypatch.setattr(runtime, "executable_dir", lambda: home / "installed")
    monkeypatch.setattr(runtime, "_repo_root", lambda: home / "bundle")
    for item in fields(Settings):
        monkeypatch.delenv(item.name.upper(), raising=False)
    for alias in (
        "UPSTOX_ACCESS_TOKEN", "UPSTOX_INSTRUMENT_KEYS", "MARKET_DATA_SOURCE",
        "UPSTOX_SANDBOX_ACCESS_TOKEN", "UPSTOX_LIVE_ACCESS_TOKEN",
        "MARKET_DATA_ACCESS_TOKEN", "MARKET_DATA_TOKEN",
    ):
        monkeypatch.delenv(alias, raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DATA_DIR", raising=False)
    return home


def _sample_candle(calendar=None) -> Candle:
    if isinstance(calendar, SessionCalendar):
        local = datetime(2024, 1, 2, 10, 0, tzinfo=ZoneInfo(calendar.timezone))
        timestamp = local.astimezone(timezone.utc)
    else:
        timestamp = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return Candle(timestamp, 100.0, 101.0, 99.0, 100.5, 10_000.0, "DEMO")


def _seed_account(
    path: Path,
    *,
    config: ExecutionConfig | None = None,
    currency: str = "INR",
    timeframe: str = "1D",
    calendar=None,
    research_record: bool = True,
) -> tuple[str, str]:
    """Persist one paper order/fill and optionally a research-history row."""
    config = config or ExecutionConfig()
    calendar = calendar or ContinuousCalendar()
    bar = _sample_candle(calendar)
    db = Database(runtime.sqlite_url_for_path(path))
    repository = Repository(db)
    broker = PaperBroker(
        config,
        currency=currency,
        timeframe=timeframe,
        calendar=calendar,
        repository=repository,
    )
    order = Order("DEMO", "BUY", 2, bar.timestamp.isoformat())
    broker.place_order(order)
    broker.on_bar(bar)
    order_id = order.id
    fill_id = broker.fills[0].id
    broker.close()
    if research_record:
        with db.transaction() as connection:
            connection.execute(
                "INSERT INTO research_runs(id, kind, created_at, payload) "
                "VALUES (?, ?, ?, ?)",
                ("recovery-preservation-test", "test", "2024-01-01T00:00:00Z",
                 json.dumps({"record": "preserve-me"})),
            )
    db.close()
    return order_id, fill_id


def _account_snapshot(path: Path) -> dict:
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


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_fresh_then_repeated_startup_is_paper_only(isolated_home: Path):
    settings = Settings.from_env()
    database_path = runtime.database_file_path(settings.database_url)
    assert database_path == isolated_home / "trading_bot.db"

    for launch in range(2):
        with TestClient(create_app(settings)) as client:
            health = client.get("/health").json()
            assert health["mode"] == "PAPER"
            assert health["live_trading"] is False
            assert health["broker"] == "LOCAL_PAPER"
            assert client.app.state.broker.get_orders() == []
            assert client.app.state.broker.fills == []
            if launch == 0:
                assert client.get("/").status_code == 200

    assert database_path.is_file()
    assert _account_snapshot(database_path)["paper"] is not None


def test_compatible_saved_account_restores_orders_and_fills(isolated_home: Path):
    settings = Settings.from_env()
    database_path = runtime.database_file_path(settings.database_url)
    order_id, fill_id = _seed_account(database_path)

    from app.api.server import create_app

    with TestClient(create_app(settings)) as client:
        broker = client.app.state.broker
        assert [order["id"] for order in broker.get_orders()] == [order_id]
        assert [fill.id for fill in broker.fills] == [fill_id]
        health = client.get("/health").json()
        assert (health["mode"], health["live_trading"], health["broker"]) == (
            "PAPER", False, "LOCAL_PAPER"
        )


@pytest.mark.parametrize(
    ("field", "changed_value"),
    [
        ("initial_capital", 100_001.0),
        ("commission", 0.0006),
        ("slippage", 0.0006),
        ("spread", 0.0003),
        ("position_size", 0.30),
        ("stop_loss", 0.06),
        ("take_profit", 0.11),
        ("max_participation", 0.20),
    ],
)
def test_each_execution_setting_mismatch_is_named_and_refused(
    isolated_home: Path, field: str, changed_value: float
):
    base = ExecutionConfig()
    path = isolated_home / f"{field}.db"
    _seed_account(path, config=base)
    before = _account_snapshot(path)
    db = Database(runtime.sqlite_url_for_path(path))
    changed = replace(base, **{field: changed_value})

    with pytest.raises(PaperAccountCompatibilityError) as caught:
        PaperBroker(changed, repository=Repository(db))
    assert caught.value.differing_fields == (f"execution.{field}",)
    assert str(changed_value) not in str(caught.value)
    db.close()
    assert _account_snapshot(path) == before


@pytest.mark.parametrize(
    ("currency", "timeframe", "expected_field"),
    [("USD", "1D", "currency"), ("INR", "5m", "timeframe")],
)
def test_currency_and_timeframe_mismatches_are_named_and_refused(
    isolated_home: Path, currency: str, timeframe: str, expected_field: str
):
    path = isolated_home / f"{expected_field}.db"
    _seed_account(path)
    before = _account_snapshot(path)
    db = Database(runtime.sqlite_url_for_path(path))
    with pytest.raises(PaperAccountCompatibilityError) as caught:
        PaperBroker(currency=currency, timeframe=timeframe, repository=Repository(db))
    assert caught.value.differing_fields == (expected_field,)
    db.close()
    assert _account_snapshot(path) == before


@pytest.mark.parametrize(
    ("saved_calendar", "current_calendar", "expected_field"),
    [
        (ContinuousCalendar(), SessionCalendar(), "calendar.type"),
        (
            SessionCalendar(timezone="Asia/Kolkata"),
            SessionCalendar(timezone="UTC"),
            "calendar.timezone",
        ),
        (
            SessionCalendar(),
            SessionCalendar(open_time=time(9, 30)),
            "calendar.open",
        ),
        (
            SessionCalendar(),
            SessionCalendar(close_time=time(15, 0)),
            "calendar.close",
        ),
        (
            SessionCalendar(),
            SessionCalendar(holidays={"2024-01-02"}),
            "calendar.holidays",
        ),
    ],
)
def test_calendar_signature_mismatches_are_named_and_refused(
    isolated_home: Path, saved_calendar, current_calendar, expected_field: str
):
    path = isolated_home / f"{expected_field.replace('.', '-')}.db"
    _seed_account(path, timeframe="1m", calendar=saved_calendar)
    db = Database(runtime.sqlite_url_for_path(path))
    with pytest.raises(PaperAccountCompatibilityError) as caught:
        PaperBroker(
            timeframe="1m", calendar=current_calendar, repository=Repository(db)
        )
    assert expected_field in caught.value.differing_fields
    db.close()


def test_orphaned_orders_or_fills_without_paper_state_refuse_silent_empty_start(
    isolated_home: Path,
):
    path = isolated_home / "orphaned-history.db"
    database = Database(runtime.sqlite_url_for_path(path))
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO orders(id, payload) VALUES (?, ?)",
            ("preserved-paper-order", "{}"),
        )
        connection.execute(
            "INSERT INTO fills(id, order_id, timestamp, payload) VALUES (?, ?, ?, ?)",
            ("preserved-paper-fill", "preserved-paper-order", "t", "{}"),
        )
    database.close()

    mismatch = inspect_paper_account(
        runtime.sqlite_url_for_path(path),
        Settings().execution_kwargs(),
        currency="INR",
        timeframe="1D",
        calendar_signature=calendar_signature(ContinuousCalendar()),
    )
    assert mismatch is not None
    assert mismatch.differing_fields == ("paper_state.missing",)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM orders").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM fills").fetchone()[0] == 1


def test_json_integer_and_float_representations_are_equivalent_but_strings_are_not():
    expected = {
        "initial_capital": 100_000.0,
        "commission": 0.0005,
        "slippage": 0.0005,
        "spread": 0.0002,
        "position_size": 0.25,
        "stop_loss": 0.05,
        "take_profit": 0.1,
        "max_participation": 0.1,
    }
    saved = {
        "config": dict(expected, initial_capital=100_000),
        "currency": "INR",
        "timeframe": "1D",
        "calendar": {"type": "ContinuousCalendar"},
    }
    args = dict(
        expected_config=expected,
        currency="INR",
        timeframe="1D",
        calendar_signature={"type": "ContinuousCalendar"},
    )
    assert differing_paper_fields(saved, **args) == ()
    saved["config"]["commission"] = "0.0005"
    assert differing_paper_fields(saved, **args) == ("execution.commission",)


def test_unknown_saved_config_field_is_named_without_echoing_its_value():
    saved_config = vars(ExecutionConfig())
    saved_config["legacy_adjustment"] = "private-value"
    saved = {
        "config": saved_config,
        "currency": "INR",
        "timeframe": "1D",
        "calendar": {"type": "ContinuousCalendar"},
    }
    differing = differing_paper_fields(
        saved,
        vars(ExecutionConfig()),
        currency="INR",
        timeframe="1D",
        calendar_signature={"type": "ContinuousCalendar"},
    )
    assert differing == ("execution.legacy_adjustment",)
    error = PaperAccountCompatibilityError(differing, "temporary.db")
    assert "legacy_adjustment" in str(error)
    assert "private-value" not in str(error)


def test_missing_calendar_signature_is_not_silently_assumed_compatible():
    saved = {
        "config": vars(ExecutionConfig()),
        "currency": "INR",
        "timeframe": "1D",
    }
    fields_found = differing_paper_fields(
        saved,
        vars(ExecutionConfig()),
        currency="INR",
        timeframe="1D",
        calendar_signature={"type": "ContinuousCalendar"},
    )
    assert fields_found == ("calendar.signature",)


def test_launcher_reports_safe_mismatch_details_and_returns_code_three(
    isolated_home: Path, capsys
):
    import arena

    path = isolated_home / "trading_bot.db"
    order_id, fill_id = _seed_account(path, timeframe="5m")
    digest_before = _sha256(path)
    result = arena._serve("127.0.0.1", 8042, False, False)
    output = capsys.readouterr().out
    assert result == 3
    assert "Differing fields: timeframe" in output
    assert str(path.resolve()) in output
    assert "--recover-paper-account --confirm-new-empty-paper-account" in output
    assert "PAPER" in output and "LOCAL_PAPER" in output
    assert "100000" not in output
    assert order_id not in output and fill_id not in output
    assert _sha256(path) == digest_before


def test_recovery_cli_requires_confirmation_then_persists_selection(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    import arena

    original = isolated_home / "trading_bot.db"
    _seed_account(original, timeframe="5m")
    with pytest.raises(SystemExit) as missing_confirmation:
        arena.main(["--recover-paper-account"])
    assert missing_confirmation.value.code == 2
    assert runtime.read_paper_database_override(isolated_home) is None

    monkeypatch.setattr(arena, "_serve", lambda *args: 0)
    result = arena.main([
        "--recover-paper-account",
        "--confirm-new-empty-paper-account",
        "--no-browser",
        "--port",
        "8042",
    ])
    output = capsys.readouterr().out
    selected = runtime.read_paper_database_override(isolated_home)
    assert result == 0
    assert selected is not None
    assert selected["database_path"] != str(original.resolve())
    assert "New empty database" in output
    assert runtime.database_file_path(Settings.from_env().database_url) == Path(
        selected["database_path"]
    )


def test_read_only_preflight_reports_fields_without_changing_original(isolated_home: Path):
    path = isolated_home / "trading_bot.db"
    _seed_account(path, timeframe="5m")
    digest_before = _sha256(path)
    settings = Settings.from_env()
    mismatch = inspect_paper_account(
        settings.database_url,
        settings.execution_kwargs(),
        currency=settings.currency,
        timeframe=settings.default_timeframe,
        calendar_signature=calendar_signature(ContinuousCalendar()),
    )
    assert mismatch is not None
    assert mismatch.differing_fields == ("timeframe",)
    assert str(path.resolve()) in str(mismatch)
    assert _sha256(path) == digest_before


def test_recovery_preserves_original_and_restart_uses_new_empty_db(isolated_home: Path):
    settings = Settings.from_env()
    original_path = runtime.database_file_path(settings.database_url)
    order_id, fill_id = _seed_account(original_path, timeframe="5m")
    original_before = _account_snapshot(original_path)
    original_hash = _sha256(original_path)

    recovery = create_separate_paper_database(settings, home=isolated_home)
    selected_path = recovery.selected_database
    assert recovery.previous_database == original_path
    assert recovery.differing_fields == ("timeframe",)
    assert selected_path != original_path and selected_path.is_file()
    assert _sha256(original_path) == original_hash
    assert _account_snapshot(original_path) == original_before
    assert original_before["orders"] == [(order_id,)]
    assert original_before["fills"] == [(fill_id,)]
    assert original_before["research"]

    pointer = runtime.read_paper_database_override(isolated_home)
    assert pointer["database_path"] == str(selected_path)
    assert pointer["differing_fields"] == ["timeframe"]

    for _ in range(2):
        selected_settings = Settings.from_env()
        assert runtime.database_file_path(selected_settings.database_url) == selected_path
        from app.api.server import create_app

        with TestClient(create_app(selected_settings)) as client:
            health = client.get("/health").json()
            assert (health["mode"], health["live_trading"], health["broker"]) == (
                "PAPER", False, "LOCAL_PAPER"
            )
            assert "PAPER ONLY" in client.get("/").text
            assert client.app.state.broker.get_orders() == []
            assert client.app.state.broker.fills == []

    recovered_snapshot = _account_snapshot(selected_path)
    recovered_state = json.loads(recovered_snapshot["paper"])
    assert recovered_state["orders"] == []
    assert recovered_state["fills"] == []
    assert recovered_snapshot["orders"] == []
    assert recovered_snapshot["fills"] == []
    assert recovered_snapshot["research"] == []
    assert _account_snapshot(original_path) == original_before
    assert _sha256(original_path) == original_hash


def test_recovery_refuses_compatible_account_and_unsafe_execution_mode(isolated_home: Path):
    settings = Settings.from_env()
    path = runtime.database_file_path(settings.database_url)
    _seed_account(path)
    with pytest.raises(PaperRecoveryError, match="No incompatible saved paper account"):
        create_separate_paper_database(settings, home=isolated_home)
    assert runtime.read_paper_database_override(isolated_home) is None

    class UnsafeSettings:
        mode = "PAPER"
        live_trading = False
        broker = "UPSTOX_SANDBOX"

    with pytest.raises(PaperRecoveryError, match="BROKER=LOCAL_PAPER"):
        create_separate_paper_database(UnsafeSettings(), home=isolated_home)
    assert runtime.read_paper_database_override(isolated_home) is None


def test_persisted_recovery_never_silently_falls_back_and_clear_removes_pointer_only(
    isolated_home: Path,
):
    settings = Settings.from_env()
    original_path = runtime.database_file_path(settings.database_url)
    _seed_account(original_path, timeframe="5m")
    recovered = create_separate_paper_database(settings, home=isolated_home)
    selected_path = recovered.selected_database

    assert runtime.clear_paper_database_override(isolated_home) is True
    assert selected_path.is_file()
    assert original_path.is_file()
    assert runtime.database_file_path(Settings.from_env().database_url) == original_path

    # Restore selection, then simulate an operator moving the selected file.
    runtime.write_paper_database_override(
        selected_path, recovered.differing_fields, isolated_home
    )
    selected_path.unlink()
    with pytest.raises(ValueError, match="will not silently switch databases"):
        Settings.from_env()


def test_inno_installer_defaults_are_paper_only_and_uninstall_keeps_user_state():
    installer = Path(__file__).resolve().parents[1] / "packaging" / "installer.iss"
    text = installer.read_text(encoding="utf-8")
    assert 'Key: "MODE"; String: "PAPER"' in text
    assert 'Key: "BROKER"; String: "LOCAL_PAPER"' in text
    assert 'Key: "LIVE_TRADING"; String: "false"' in text
    assert "uninsneveruninstall" in text

    uninstall_section = text.split("[UninstallDelete]", 1)[1]
    directives = [
        line.strip() for line in uninstall_section.splitlines()
        if line.strip() and not line.lstrip().startswith(";")
    ]
    assert all(
        term not in " ".join(directives).lower()
        for term in (".env", ".db", "database", "orders", "fills", "research")
    )


def test_frozen_dotenv_candidates_and_process_environment_precedence(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from app import runtime as runtime_module

    install_dir = tmp_path / "install"
    bundle_dir = tmp_path / "bundle"
    install_dir.mkdir()
    bundle_dir.mkdir()
    user_env = isolated_home / ".env"
    exe_env = install_dir / ".env"
    bundle_env = bundle_dir / ".env"
    user_env.write_text(
        "DEFAULT_TIMEFRAME=1D\nDATABASE_URL=sqlite:///user.db\n", encoding="utf-8"
    )
    exe_env.write_text(
        "DEFAULT_TIMEFRAME=5m\nDATABASE_URL=sqlite:///installer.db\n", encoding="utf-8"
    )
    bundle_env.write_text(
        "DEFAULT_TIMEFRAME=15m\nDATABASE_URL=sqlite:///bundle.db\n", encoding="utf-8"
    )

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(install_dir / "Arena.exe"))
    monkeypatch.setattr(runtime_module, "executable_dir", lambda: install_dir)
    monkeypatch.setattr(runtime_module, "_repo_root", lambda: bundle_dir)
    candidates = runtime_module.env_candidates()
    assert candidates == [user_env, exe_env, bundle_env]
    assert runtime_module.database_url_source(process_value_present=False) == (
        f"first matching dotenv candidate: {user_env}"
    )
    loaded = Settings.from_env()
    assert loaded.default_timeframe == "1D"
    assert loaded.database_url == "sqlite:///user.db"

    # Process environment wins even after dotenv has populated os.environ.
    monkeypatch.setenv("DEFAULT_TIMEFRAME", "30m")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///process.db")
    assert Settings.from_env().default_timeframe == "30m"
    assert Settings.from_env().database_url == "sqlite:///process.db"
    assert runtime_module.database_url_source(process_value_present=True).startswith(
        "process environment"
    )
    monkeypatch.setenv("DATABASE_URL", "")
    empty_url_settings = Settings.from_env()
    assert empty_url_settings.database_url == runtime_module.default_database_path(
        isolated_home
    )
    assert runtime_module.database_url_source(
        process_value_present=True, process_value_empty=True
    ).startswith("frozen/portable default")
    described = runtime_module.describe()
    assert described["env_candidates"] == [str(path) for path in candidates]
    assert described["dotenv_precedence"].startswith("inherited process environment first")
