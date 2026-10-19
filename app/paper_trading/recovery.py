"""Explicit, persistent recovery to a separate empty PAPER database.

What this does, and nothing else:

* it PROVES that the saved paper account is incompatible with the current
  configuration (read-only inspection, no writes);
* it creates a NEW, EMPTY paper database at a fresh, exclusive path;
* it records that choice in a small pointer file in the Arena home, so every
  later launch uses the new database instead of silently falling back to the
  incompatible one.

What it never does:

* it never copies, resets, migrates, overwrites or deletes the original
  database, its orders, its fills or its research records;
* it never changes the execution mode — recovery is PAPER / LOCAL_PAPER only;
* it never places an order.

The original account stays exactly where it is and can be opened again by
clearing the selection (``--clear-paper-database-recovery``).
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app import runtime
from app.backtesting.costs import ExecutionConfig
from app.database.database import Database
from app.instance import InstanceGuard
from app.paper_trading.compatibility import (
    PaperAccountCompatibilityError,
    inspect_paper_account,
)
from app.paper_trading.execution import calendar_signature, make_calendar


class PaperRecoveryError(ValueError):
    """A safe paper-database recovery could not be completed."""


@dataclass(frozen=True)
class PaperDatabaseRecovery:
    selected_database: Path
    previous_database: Path
    differing_fields: tuple[str, ...]


def _require_paper_only(settings) -> None:
    if (
        str(getattr(settings, "mode", "")).upper() != "PAPER"
        or bool(getattr(settings, "live_trading", True))
        or str(getattr(settings, "broker", "")).upper() != "LOCAL_PAPER"
    ):
        raise PaperRecoveryError(
            "Separate-database recovery is allowed only with MODE=PAPER, "
            "LIVE_TRADING=false, BROKER=LOCAL_PAPER. Recovery will not change "
            "or enable an execution mode."
        )


def create_separate_paper_database(
    settings, *, home: Path | None = None
) -> PaperDatabaseRecovery:
    """Create and persist a new, empty paper DB only after a strict mismatch.

    The original account is inspected through SQLite read-only mode. The
    helper refuses when it cannot prove a compatibility mismatch, when
    another Arena instance owns this home, or when a recovery database is
    already selected.
    """
    _require_paper_only(settings)
    arena_home = Path(home or runtime.arena_home()).expanduser().resolve(strict=False)
    active_override = runtime.read_paper_database_override(arena_home)
    if active_override is not None:
        raise PaperRecoveryError(
            "A separate paper recovery database is already selected at "
            f"{active_override['database_path']}. No database was changed. "
            "Use --clear-paper-database-recovery to remove only that selection."
        )

    guard = InstanceGuard(arena_home)
    existing = guard.try_acquire()
    if existing is not None:
        raise PaperRecoveryError(
            "Recovery was refused because " + existing.summary()
            + " Stop the running Arena first; no database was changed."
        )

    try:
        previous_database = runtime.database_file_path(settings.database_url)
        if previous_database is None:
            raise PaperRecoveryError(
                "Recovery requires a file-backed SQLite database; no database was changed."
            )
        expected_calendar = calendar_signature(
            make_calendar(settings.market_calendar, settings.default_timeframe)
        )
        mismatch = inspect_paper_account(
            settings.database_url,
            ExecutionConfig(**settings.execution_kwargs()).__dict__,
            currency=settings.currency,
            timeframe=settings.default_timeframe,
            calendar_signature=expected_calendar,
        )
        if mismatch is None:
            raise PaperRecoveryError(
                "No incompatible saved paper account was found in "
                f"{previous_database}. Recovery was not performed and no database "
                "was changed."
            )

        recovery_dir = arena_home / "paper-recovery"
        recovery_dir.mkdir(parents=True, exist_ok=True)
        if recovery_dir.is_symlink():
            raise PaperRecoveryError(
                "The paper-recovery directory must not be a symbolic link; "
                "no database was changed."
            )
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        selected_database = recovery_dir / f"paper-{stamp}-{uuid.uuid4().hex}.db"
        restore_sidecar = Path(str(selected_database) + ".restore-pending")
        if restore_sidecar.exists() or restore_sidecar.is_symlink():
            raise PaperRecoveryError(
                "A pending restore exists beside the proposed recovery path; "
                "no database was selected."
            )

        # Reserve a guaranteed-new file atomically. Path.touch(exist_ok=False)
        # uses exclusive creation; Database treats the zero-byte reservation
        # as fresh and initializes it, never opening an existing path to reset.
        try:
            selected_database.touch(exist_ok=False)
            if os.name != "nt":
                os.chmod(selected_database, 0o600)
        except FileExistsError:  # UUID collision: fail closed, do not reuse it.
            raise PaperRecoveryError(
                "Could not reserve a unique recovery database path; "
                "the original database was not changed."
            ) from None

        database = None
        try:
            database = Database(runtime.sqlite_url_for_path(selected_database))
        finally:
            if database is not None:
                database.close()

        # Persist only after the new database has been initialized. If this
        # write fails, the empty file is left intact rather than deleted; the
        # original remains selected and unchanged.
        runtime.write_paper_database_override(
            selected_database, mismatch.differing_fields, arena_home
        )
        return PaperDatabaseRecovery(
            selected_database=selected_database.resolve(strict=True),
            previous_database=previous_database,
            differing_fields=mismatch.differing_fields,
        )
    finally:
        guard.release()
