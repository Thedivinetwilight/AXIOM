"""Strict, value-redacted compatibility checks for persisted paper accounts.

The saved account includes execution assumptions. Restoring positions, orders,
and fills under different assumptions is unsafe, so a mismatch is a refusal,
not a migration. This module is shared by normal startup, the paper broker, and
the explicit separate-database recovery command.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from app import runtime


class PaperAccountCompatibilityError(ValueError):
    """Saved state cannot be restored under the current paper configuration.

    Only field names and the local database path are exposed. Saved or current
    account/configuration values are deliberately never included in the error.
    """

    def __init__(self, differing_fields, database_path: str | Path | None = None):
        self.differing_fields = tuple(dict.fromkeys(str(name) for name in differing_fields))
        self.database_path = str(database_path) if database_path is not None else "unknown"
        fields_text = ", ".join(self.differing_fields) or "paper_state"
        super().__init__(
            "Saved paper account is incompatible with the current configuration. "
            f"Differing fields: {fields_text}. Database: {self.database_path}. "
            "The saved paper account was not restored or changed."
        )


class PaperDatabaseInspectionError(ValueError):
    """The current paper database could not be inspected without writing it."""

    def __init__(self, database_path: str | Path, error_type: str):
        self.database_path = str(database_path)
        self.error_type = error_type
        super().__init__(
            "Arena could not safely inspect the existing paper database "
            f"{self.database_path} ({error_type}). No recovery database was selected "
            "and no paper account data was changed."
        )


def _same_value(saved: Any, expected: Any) -> bool:
    """Compare JSON values without making integer/float serialization a diff.

    JSON numbers such as ``1`` and ``1.0`` have identical execution meaning.
    Numeric strings are not numbers and remain incompatible; accepting them
    would weaken the existing strict check.
    """
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        if not isinstance(saved, (int, float)) or isinstance(saved, bool):
            return False
        try:
            return Decimal(str(saved)) == Decimal(str(expected))
        except (InvalidOperation, ValueError):
            return False
    return type(saved) is type(expected) and saved == expected


_FIELD_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")


def _append_unknown_fields(fields: list[str], prefix: str, unknown: set) -> None:
    safe_names = [
        name for name in unknown
        if isinstance(name, str) and _FIELD_NAME.fullmatch(name)
    ]
    fields.extend(f"{prefix}.{name}" for name in sorted(safe_names))
    if len(safe_names) != len(unknown):
        fields.append(f"{prefix}.unknown_fields")


def differing_paper_fields(
    saved_state: Any,
    expected_config: Mapping[str, Any],
    *,
    currency: str,
    timeframe: str,
    calendar_signature: Mapping[str, Any],
) -> tuple[str, ...]:
    """Return stable, value-free field paths that block restore.

    All execution-config fields, currency, timeframe, and the full calendar
    signature are compared. Missing fields are incompatible. Safe unknown key
    names are included without values; malformed key names become a generic
    marker so arbitrary stored content is never echoed.
    """
    fields: list[str] = []
    if not isinstance(saved_state, Mapping):
        return ("paper_state",)

    saved_config = saved_state.get("config")
    if not isinstance(saved_config, Mapping):
        fields.append("execution_config")
    else:
        for name, expected in expected_config.items():
            if name not in saved_config or not _same_value(saved_config[name], expected):
                fields.append(f"execution.{name}")
        _append_unknown_fields(
            fields, "execution", set(saved_config) - set(expected_config)
        )

    if "currency" not in saved_state or not _same_value(
        saved_state.get("currency"), currency
    ):
        fields.append("currency")
    if "timeframe" not in saved_state or not _same_value(
        saved_state.get("timeframe"), timeframe
    ):
        fields.append("timeframe")

    saved_calendar = saved_state.get("calendar")
    if not isinstance(saved_calendar, Mapping):
        fields.append("calendar.signature")
    else:
        for name, expected in calendar_signature.items():
            if name not in saved_calendar or not _same_value(saved_calendar[name], expected):
                fields.append(f"calendar.{name}")
        _append_unknown_fields(
            fields, "calendar", set(saved_calendar) - set(calendar_signature)
        )

    return tuple(fields)


def _has_rows(connection: sqlite3.Connection, table: str) -> bool:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if exists is None:
        return False
    return connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None


def _read_saved_paper_state(database_path: Path) -> Any | None:
    """Read only paper state/history; never create or migrate a database."""
    if not database_path.exists():
        return None

    # A queued, explicit database restore is applied by Database before it
    # opens SQLite. Defer this read-only check so compatibility is checked
    # against the restored file, not the file scheduled to be replaced.
    pending_restore = Path(str(database_path) + ".restore-pending")
    if pending_restore.exists() or pending_restore.is_symlink():
        return None

    connection = None
    try:
        # mode=ro is important: opening an incompatible account must not run
        # schema creation/migrations, change WAL mode, or create a database.
        connection = sqlite3.connect(
            database_path.as_uri() + "?mode=ro", uri=True, timeout=2.0
        )
        connection.execute("PRAGMA query_only=ON")
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='state'"
        ).fetchone()
        if exists is None:
            return (
                _PaperHistoryWithoutState
                if _has_rows(connection, "orders") or _has_rows(connection, "fills")
                else None
            )
        row = connection.execute(
            "SELECT payload FROM state WHERE key = ?", ("paper",)
        ).fetchone()
        if row is None:
            return (
                _PaperHistoryWithoutState
                if _has_rows(connection, "orders") or _has_rows(connection, "fills")
                else None
            )
        try:
            state = json.loads(row[0])
            return _MalformedPaperState if state is None else state
        except (TypeError, ValueError, json.JSONDecodeError):
            # Corrupt paper payloads are never guessed at or rewritten.
            return _MalformedPaperState
    except (OSError, sqlite3.Error) as exc:
        raise PaperDatabaseInspectionError(database_path, type(exc).__name__) from None
    finally:
        if connection is not None:
            connection.close()


class _MalformedPaperStateType:
    pass


class _PaperHistoryWithoutStateType:
    pass


_MalformedPaperState = _MalformedPaperStateType()
_PaperHistoryWithoutState = _PaperHistoryWithoutStateType()


def inspect_paper_account(
    database_url: str,
    expected_config: Mapping[str, Any],
    *,
    currency: str,
    timeframe: str,
    calendar_signature: Mapping[str, Any],
) -> PaperAccountCompatibilityError | None:
    """Return a redacted mismatch for an existing account, otherwise ``None``.

    This function is read-only and is safe to call before Database initializes
    SQLite. It resolves relative URLs exactly as SQLite will: against the
    process working directory at startup.
    """
    database_path = runtime.database_file_path(database_url)
    if database_path is None:
        return None
    saved_state = _read_saved_paper_state(database_path)
    if saved_state is None:
        return None
    if saved_state is _PaperHistoryWithoutState:
        return PaperAccountCompatibilityError(("paper_state.missing",), database_path)
    fields = differing_paper_fields(
        saved_state,
        expected_config,
        currency=currency,
        timeframe=timeframe,
        calendar_signature=calendar_signature,
    )
    if fields:
        return PaperAccountCompatibilityError(fields, database_path)
    return None
