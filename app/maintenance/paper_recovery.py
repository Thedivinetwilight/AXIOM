"""Non-destructive recovery from an incompatible or damaged paper database.

When the saved paper account no longer matches the configuration (for example
``DEFAULT_TIMEFRAME`` was changed), the correct behaviour is to REFUSE to
start — mixing two accounts in one file would corrupt the history. This module
gives the operator the other half of that story: an explicit, one-command way
to start a fresh paper account in a NEW database while leaving the original
byte-for-byte untouched.

Guarantees:

* the existing database is opened read-only (only to fingerprint it) and is
  never modified, moved, renamed or deleted;
* the replacement has a distinct, timestamped name in the same directory and
  is never allowed to overwrite an existing file;
* only the ``DATABASE_URL`` line in the operator's ``.env`` is changed
  (atomically, owner-only where the platform supports it); every other line is
  preserved;
* a ``DATABASE_URL`` exported in the process environment is refused, because
  editing ``.env`` would not change what the next launch uses;
* the SHA-256 of the original is compared before and after, and the command
  fails loudly if it changed.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from app import runtime
from app.config.envfile import read_values, update_values
from app.database.paths import (
    DatabaseUrlError,
    absolute_database_url,
    is_absolute_path_text,
    is_memory_url,
    resolve_sqlite_path,
    sqlite_path_text,
)


class PaperRecoveryError(RuntimeError):
    """Recovery was refused; the message states what the operator must do."""


def recovery_command() -> str:
    """The command an operator can run in THIS build to recover."""
    return ("Arena.exe --new-paper-account" if runtime.is_frozen()
            else "python arena.py --new-paper-account")


def effective_database_url(*, env_file: Path | None = None) -> tuple[str, str]:
    """The database URL the next launch will actually use, and its source."""
    env_file = Path(env_file or runtime.env_file())
    process_value = (__import__("os").environ.get("DATABASE_URL") or "").strip()
    if process_value:
        return process_value, "process-environment"
    try:
        file_value = (read_values(env_file).get("DATABASE_URL") or "").strip()
    except OSError:
        file_value = ""
    if file_value:
        return file_value, "env-file"
    if runtime.is_portable():
        return runtime.default_database_path(), "default"
    return "sqlite:///trading_bot.db", "default"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _anchor() -> Path | None:
    """Relative SQLite paths are anchored to ARENA_HOME when portable."""
    return runtime.arena_home() if runtime.is_portable() else None


def create_replacement_database(*, env_file: Path | None = None,
                                now: datetime | None = None) -> dict:
    """Create a new paper database and repoint DATABASE_URL at it."""
    env_file = Path(env_file or runtime.env_file())
    url, source = effective_database_url(env_file=env_file)
    if source == "process-environment":
        raise PaperRecoveryError(
            "DATABASE_URL is set in the process environment, so changing "
            f"{env_file} would not change the database that launches. Unset "
            "the environment variable (or export the new URL) and retry.")
    if is_memory_url(url):
        raise PaperRecoveryError(
            "The configured database is in memory; there is no paper account "
            "to recover.")
    try:
        sqlite_path_text(url)
        original = resolve_sqlite_path(url, home=_anchor())
    except DatabaseUrlError as exc:
        raise PaperRecoveryError(str(exc)) from None
    if not original.exists():
        raise PaperRecoveryError(
            f"No existing paper database was found at {original}; there is "
            "nothing to recover. Arena will create a fresh one on the next "
            "start.")
    if not original.is_file():
        raise PaperRecoveryError(
            f"{original} is not a regular file; refusing to recover.")
    if original.is_symlink():
        raise PaperRecoveryError(
            f"{original} is a symbolic link; point DATABASE_URL at the real "
            "file and retry rather than re-pointing a link.")

    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    suffix = original.suffix or ".db"
    candidate = original.with_name(f"{original.stem}.recovered-{stamp}{suffix}")
    counter = 2
    while candidate.exists():
        candidate = original.with_name(
            f"{original.stem}.recovered-{stamp}-{counter}{suffix}")
        counter += 1
    if candidate.resolve() == original.resolve():
        raise PaperRecoveryError("Refusing to reuse the existing database path.")

    before = _sha256(original)
    before_size = original.stat().st_size

    # Imported here: the recovery command must run even when the current
    # configuration would not construct (that is often why it is needed).
    from app.database.database import Database

    new_url = f"sqlite:///{candidate}"
    try:
        database = Database(new_url)
        database.close()
    except Exception as exc:
        raise PaperRecoveryError(
            f"Could not create the replacement database at {candidate}: "
            f"{type(exc).__name__}: {exc}. The existing database was not "
            "changed.") from None

    after = _sha256(original)
    if before != after or original.stat().st_size != before_size:
        # Should be impossible: the original is only read. If it ever happens,
        # stop before changing any configuration.
        raise PaperRecoveryError(
            "The original database changed during recovery; no configuration "
            "was updated. Investigate before continuing.")

    summary = update_values(env_file, {"DATABASE_URL": new_url})
    return {
        "ok": True,
        "source": source,
        "original_database": str(original),
        "original_sha256": before,
        "original_size_bytes": before_size,
        "new_database": str(candidate),
        "new_database_url": new_url,
        "env_file": summary["path"],
        "env_updated": summary["updated"],
        "env_added": summary["added"],
        "verified_original_unchanged": True,
        "note": ("The original database is untouched. Restart Arena; the new "
                 "paper account starts empty. Keep or archive the original "
                 "file for its history."),
    }


def resolve_for_display(url: str) -> str:
    """Absolute path for diagnostics; the URL itself when not a path URL."""
    try:
        if is_memory_url(url):
            return ":memory:"
        return str(resolve_sqlite_path(url, home=_anchor()))
    except DatabaseUrlError:
        return url


def absolute_url_if_relative(url: str) -> str:
    """Rebase a relative URL for reporting; leave absolute forms untouched."""
    try:
        raw = sqlite_path_text(url)
    except DatabaseUrlError:
        return url
    if raw == ":memory:" or is_absolute_path_text(raw):
        return url
    return absolute_database_url(url, home=_anchor())
