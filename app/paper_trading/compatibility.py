"""Strict, value-redacted compatibility checks for persisted paper accounts.

A saved paper account carries the execution assumptions it was created under
(timeframe, currency, costs, calendar). Restoring positions, orders and fills
under *different* assumptions is unsafe: the same fill would be re-priced
differently, so the account would silently disagree with itself.

That makes a mismatch a REFUSAL, not a migration. But a refusal is only
honest if it says three things:

* **which** fields differ (names only — never the saved or current value),
* **where** the account lives (the database file), and
* **that nothing was changed** (the check is read-only).

This module is the single implementation of that check. It is shared by:

* ``app/api/server.py`` — the startup pre-flight, so an incompatible account
  is refused BEFORE the database is opened, migrated or written;
* ``app/paper_trading/broker.py`` — the restore itself;
* ``app/paper_trading/recovery.py`` — the explicit, separate-database
  recovery, which must be able to *prove* a mismatch before it acts.

Everything here is read-only. The saved account is inspected through SQLite's
read-only URI (``mode=ro``) with ``PRAGMA query_only=ON``, so inspecting an
incompatible account can never create a database, run a migration, change the
WAL mode or touch a single row.
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
    """The saved state cannot be restored under the current configuration.

    Only field NAMES and the local database path are exposed. Saved or current
    account/configuration values are deliberately never included in the error:
    the message is safe to paste into a support conversation.
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
    Numeric strings are not numbers and remain incompatible: accepting them
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
    """Report unexpected stored keys by NAME only, and never echo their values."""
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
    """Return stable, value-free field paths that block a restore.

    All execution-config fields, the currency, the timeframe and the full
    calendar signature are compared. A missing field is incompatible: an
    account saved without a timeframe cannot be restored under one.

    The returned names are the DIAGNOSIS, never a data leak: no saved or
    current value is ever included.
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


class _MalformedPaperState:
    """Marker: the paper payload exists but cannot be parsed."""


class _PaperHistoryWithoutState:
    """Marker: orders/fills exist but the account payload is gone."""


def _read_saved_paper_state(database_path: Path) -> Any | None:
    """Read only the paper state/history; never create or migrate a database."""
    if not database_path.exists():
        return None

    # A queued, explicit database restore is applied by Database before it
    # opens SQLite. Defer this read-only check so compatibility is judged
    # against the RESTORED file, not the file scheduled to be replaced.
    pending_restore = Path(str(database_path) + ".restore-pending")
    if pending_restore.exists() or pending_restore.is_symlink():
        return None

    connection = None
    try:
        # mode=ro matters: opening an incompatible account must not run schema
        # creation/migrations, change the WAL mode, or create a database.
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
            # A corrupt paper payload is never guessed at and never rewritten.
            return _MalformedPaperState
    except (OSError, sqlite3.Error) as exc:
        raise PaperDatabaseInspectionError(database_path, type(exc).__name__) from None
    finally:
        if connection is not None:
            connection.close()


def inspect_paper_account(
    database_url: str,
    expected_config: Mapping[str, Any],
    *,
    currency: str,
    timeframe: str,
    calendar_signature: Mapping[str, Any],
) -> PaperAccountCompatibilityError | None:
    """Return a redacted mismatch for an existing account, otherwise ``None``.

    This function is read-only and safe to call before ``Database`` opens
    SQLite. It resolves the database file exactly as SQLite will — through
    :func:`app.runtime.database_file_path`, the single normalization point —
    so the check and the application can never look at different files.
    """
    database_path = runtime.database_file_path(database_url)
    if database_path is None:
        return None
    saved_state = _read_saved_paper_state(database_path)
    if saved_state is None:
        return None
    if saved_state is _PaperHistoryWithoutState:
        return PaperAccountCompatibilityError(("paper_state.missing",), database_path)
    if saved_state is _MalformedPaperState:
        return PaperAccountCompatibilityError(("paper_state.malformed",), database_path)
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
