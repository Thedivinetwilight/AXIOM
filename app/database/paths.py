"""SQLite URL/path handling shared by configuration, startup and recovery.

`Settings.from_env` used to hand ``DATABASE_URL`` straight to ``sqlite3``. In a
packaged build the working directory is not Arena's home (a double-clicked
Windows executable often starts in ``C:\\Windows\\System32``), so a relative
URL silently resolved somewhere unrelated — or failed with an opaque
``unable to open database file``. These helpers make the resolution explicit
and keep URL parsing in one place.

Supported forms (the forms the project documents)::

    sqlite:///relative.db              -> ARENA_HOME/relative.db when portable
    sqlite:////absolute/path.db        -> /absolute/path.db
    sqlite:///C:/Users/me/arena.db     -> C:/Users/me/arena.db (Windows)
    sqlite:///:memory:                 -> in-memory database

Anything else is refused rather than guessed at.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

PREFIX = "sqlite:///"
WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")


class DatabaseUrlError(ValueError):
    """The configured DATABASE_URL is not a supported SQLite URL."""


def sqlite_path_text(url: str) -> str:
    """The path portion of a ``sqlite:///`` URL (without resolving it)."""
    if not isinstance(url, str) or not url.startswith(PREFIX):
        raise DatabaseUrlError(
            "Only SQLite URLs are supported (sqlite:///path/to/arena.db)")
    path = url[len(PREFIX):]
    if not path:
        raise DatabaseUrlError("DATABASE_URL has no database path")
    if "?" in path or "#" in path:
        raise DatabaseUrlError(
            "DATABASE_URL must not contain a query string or fragment")
    return path


def is_absolute_path_text(path: str) -> bool:
    """Absolute-path test that also understands Windows forms on POSIX.

    ``Path.is_absolute()`` is platform-specific; a Windows path in a config
    file validated on Linux would be misread as relative. A drive prefix,
    a POSIX leading slash or a UNC prefix all mean "already absolute".
    """
    return bool(
        path.startswith("/")
        or path.startswith("\\\\")
        or path.startswith("//")
        or WINDOWS_DRIVE.match(path)
    )


def resolve_sqlite_path(url: str, *, home: Path | str | None = None,
                        base: Path | str | None = None) -> Path:
    """Resolve a SQLite URL to an absolute path.

    ``home`` takes precedence for relative paths (packaged behaviour); ``base``
    defaults to the current working directory (source behaviour).
    """
    raw = sqlite_path_text(url)
    if raw == ":memory:":
        raise DatabaseUrlError("an in-memory database has no filesystem path")
    if is_absolute_path_text(raw):
        return Path(raw)
    anchor = Path(home) if home is not None else Path(base or os.getcwd())
    return (anchor / raw).resolve()


def absolute_database_url(url: str, *, home: Path | str | None = None,
                          base: Path | str | None = None) -> str:
    """Return the same database expressed as an absolute SQLite URL."""
    path = resolve_sqlite_path(url, home=home, base=base)
    return f"{PREFIX}{path}"


def is_memory_url(url: str) -> bool:
    try:
        return sqlite_path_text(url) == ":memory:"
    except DatabaseUrlError:
        return False
