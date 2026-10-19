"""Where Arena keeps its files.

Running from source, everything lives in the repository — that is unchanged.

Running as a PACKAGED EXECUTABLE, that assumption breaks completely:

* PyInstaller unpacks a one-file build into a temporary directory and deletes
  it on exit, so anything written next to the code is lost;
* the executable may sit in a read-only location (``Program Files``);
* the working directory of a double-clicked program is not its own folder — on
  Windows it is often ``C:\\Windows\\System32``. Every relative path such as
  ``sqlite:///trading_bot.db`` or ``data`` would therefore resolve somewhere
  random, and the database would appear to reset on every launch.

So a packaged Arena keeps its state in a per-user data directory and resolves
relative paths against that, not against the working directory. ``ARENA_HOME``
overrides the choice, which is also what tests and portable installs use.

THAT PROMISURE IS NOW ENFORCED
    ``sqlite:///trading.db`` in a packaged install used to be handed to SQLite
    verbatim, which resolves it against the *process working directory* — the
    directory the user happened to double-click from, on Windows often
    ``C:\\Windows\\System32``. The same install could then open a different
    database on every launch, and the account in it appeared and disappeared.
    A relative database path in a PORTABLE install is now resolved against
    the Arena home, so one installation always means one database file.

    The database path is also *normalized* in exactly one place
    (:func:`database_file_path`) and reported through :func:`describe` and
    the startup log, so a support conversation talks about one spelling of
    one file instead of three.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

APP_DIR_NAME = "Arena"


def is_frozen() -> bool:
    """True when running from a PyInstaller/ cx_Freeze-style bundle."""
    return bool(getattr(sys, "frozen", False))


def _platform_data_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base)
        return Path.home() / "AppData" / "Local"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    base = os.environ.get("XDG_DATA_HOME")
    if base:
        return Path(base)
    return Path.home() / ".local" / "share"


def _repo_root() -> Path:
    # app/runtime.py -> app -> <repo>
    return Path(__file__).resolve().parents[1]


def arena_home() -> Path:
    """The directory holding the database, data, logs and configuration.

    Precedence:
      1. ``$ARENA_HOME`` if set (explicit always wins);
      2. the per-user data directory when running as a packaged executable;
      3. the repository root when running from source (unchanged behaviour).
    """
    explicit = (os.environ.get("ARENA_HOME") or "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    if is_frozen():
        return _platform_data_dir() / APP_DIR_NAME
    return _repo_root()


def is_portable() -> bool:
    """True when state lives outside the source tree (packaged or explicit)."""
    return is_frozen() or bool((os.environ.get("ARENA_HOME") or "").strip())


def executable_dir() -> Path:
    """The folder the user actually launched from (the .exe's own folder).

    Used only to LOOK for things the user may have placed beside the
    executable, e.g. a portable ``.env``. Never used for writing, because that
    location may be read-only.
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return _repo_root()


def env_file() -> Path:
    """The .env Arena reads and the setup page writes."""
    explicit = (os.environ.get("ARENA_ENV_FILE") or "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    return arena_home() / ".env"


def env_candidates() -> list[Path]:
    """Every .env location to load, most specific first.

    A portable install can carry a ``.env`` beside the executable; that is
    checked before the per-user one so a USB stick or a network share behaves
    the way an operator expects.
    """
    seen: list[Path] = []
    for candidate in (env_file(), executable_dir() / ".env", _repo_root() / ".env"):
        resolved = candidate.resolve() if candidate.exists() else candidate
        if resolved not in seen:
            seen.append(resolved)
    return seen


def ensure_home(home: Path | None = None) -> Path:
    """Create the home directory tree. Safe to call repeatedly."""
    root = Path(home or arena_home())
    for child in ("data", "data/history", "data/cache", "logs", "results", "backups"):
        (root / child).mkdir(parents=True, exist_ok=True)
    return root


def default_database_path(home: Path | None = None) -> str:
    """Absolute sqlite path for a fresh install, independent of the CWD."""
    root = Path(home or arena_home())
    return f"sqlite:///{(root / 'trading_bot.db').as_posix()}"


# ---------------------------------------------------------------------------
# The database path: one spelling, one resolution, one identity
# ---------------------------------------------------------------------------

SQLITE_PREFIX = "sqlite:///"


def database_file_path(database_url: str) -> Path | None:
    """The exact SQLite FILE behind ``database_url``, without opening it.

    This is the single place where a database URL becomes a path, so the
    compatibility check, the database layer, the diagnostics and the recovery
    command can never disagree about which file is in play.

    ``:memory:`` has no file and returns ``None``. Everything else is
    resolved to an absolute path (symlinks included) so the same file always
    has the same answer, whatever the working directory or the URL spelling.
    """
    url = str(database_url)
    if not url.startswith(SQLITE_PREFIX):
        raise ValueError("Only SQLite URLs are supported")
    raw = url[len(SQLITE_PREFIX):]
    if raw == ":memory:":
        return None
    return Path(raw).expanduser().resolve(strict=False)


def sqlite_url_for_path(path: str | Path) -> str:
    """Build a SQLite URL for an absolute path on POSIX or Windows."""
    resolved = Path(path).expanduser().resolve(strict=False)
    return f"{SQLITE_PREFIX}{resolved.as_posix()}"


def path_identity(path: str | Path) -> str:
    """A comparable identity for a filesystem path.

    Windows paths are case-insensitive, so ``C:\\Users\\Me\\...`` and
    ``c:\\users\\me\\...`` are the same file and must not be reported as two
    different databases. Symlinks are already resolved by
    :func:`database_file_path`.
    """
    return os.path.normcase(str(Path(path).expanduser().resolve(strict=False)))


def is_absolute_database_path(raw: str) -> bool:
    """True when the file part of a SQLite URL is an absolute path.

    SQLite's own convention decides this, not the operating system: a URL
    with FOUR slashes (``sqlite:////path``) names an absolute path and one
    with three (``sqlite:///path``) a relative one. The two questions are
    not the same - on Windows a leading slash alone is drive-relative
    (``C:\\custom``), so asking the operating system would make an
    operator's explicit setting look relative and silently re-anchor it
    into the Arena home.

    A Windows drive letter (``C:/...``) counts as absolute even when the
    code runs on POSIX, so a Windows-style URL is never silently re-anchored
    to the Arena home by a non-Windows build.
    """
    if raw in ("", ":memory:"):
        return False
    if raw.startswith("/") or raw.startswith("\\"):
        return True
    if len(raw) >= 3 and raw[1] == ":" and raw[2] in "/\\" and raw[0].isalpha():
        return True
    return Path(raw).expanduser().is_absolute()


def resolve_portable_database_url(database_url: str) -> str:
    """Anchor a RELATIVE database URL to the Arena home in a portable install.

    Returns the URL unchanged when it is already absolute, ``:memory:``, or
    when the install is not portable (a source checkout keeps SQLite's
    working-directory semantics, exactly as before).
    """
    url = str(database_url)
    if not url.startswith(SQLITE_PREFIX):
        return url
    raw = url[len(SQLITE_PREFIX):]
    if raw == ":memory:" or is_absolute_database_path(raw):
        return url
    if not is_portable():
        return url
    return sqlite_url_for_path(Path(arena_home()) / raw)


def dotenv_files_with_key(name: str) -> list[Path]:
    """Existing dotenv candidates that define ``name`` (values never shown)."""
    try:
        from dotenv import dotenv_values
    except ImportError:
        return []
    matches: list[Path] = []
    for candidate in env_candidates():
        if not candidate.is_file():
            continue
        try:
            values = dotenv_values(candidate)
        except (OSError, UnicodeError, ValueError):
            continue
        if name in values and values[name] is not None:
            if name == "DATABASE_URL" and not str(values[name]).strip():
                continue  # Settings treats an empty URL as unset.
            matches.append(candidate)
    return matches


def database_url_source(
    *, process_value_present: bool, process_value_empty: bool = False
) -> str:
    """Where the effective DATABASE_URL came from — never its value."""
    if process_value_present and not process_value_empty:
        return "process environment at Settings load (highest priority)"
    if process_value_present and process_value_empty:
        if is_portable():
            return f"frozen/portable default under Arena home: {arena_home()}"
        return "empty process DATABASE_URL (source configuration is invalid)"
    matches = dotenv_files_with_key("DATABASE_URL")
    if matches:
        return f"first matching dotenv candidate: {matches[0]}"
    if is_portable():
        return f"frozen/portable default under Arena home: {arena_home()}"
    return "source default sqlite:///trading_bot.db (relative to process CWD)"


# ---------------------------------------------------------------------------
# The durable pointer for an explicitly selected recovery database
# ---------------------------------------------------------------------------

PAPER_DATABASE_OVERRIDE_NAME = "paper-database-override.json"


def paper_database_override_file(home: Path | None = None) -> Path:
    """The durable pointer for an explicitly selected recovery database."""
    return Path(home or arena_home()) / PAPER_DATABASE_OVERRIDE_NAME


def read_paper_database_override(home: Path | None = None) -> dict | None:
    """Read a persisted paper DB selection, failing closed if it went missing.

    A deleted or moved recovery database must never make Arena silently fall
    back to the original (possibly incompatible) account.
    """
    pointer = paper_database_override_file(home)
    if pointer.is_symlink():
        raise ValueError(
            f"Paper database recovery pointer must be a regular file: {pointer}; "
            "no database was selected."
        )
    if not pointer.exists():
        return None
    if not pointer.is_file():
        raise ValueError(
            f"Paper database recovery pointer is not a regular file: {pointer}; "
            "no database was selected."
        )
    try:
        payload = json.loads(pointer.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        raise ValueError(
            f"Paper database recovery pointer is unreadable: {pointer}; "
            "no database was selected."
        ) from None
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError(
            f"Paper database recovery pointer is invalid: {pointer}; "
            "no database was selected."
        )
    raw_path = payload.get("database_path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError(
            f"Paper database recovery pointer has no database path: {pointer}."
        )
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        raise ValueError(
            f"Paper database recovery path is not absolute: {pointer}; "
            "refusing to fall back to another database."
        )
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError(
            "The selected paper recovery database is missing or is not a regular "
            f"file: {candidate}. Arena will not silently switch databases."
        )
    payload["database_path"] = str(candidate.resolve(strict=True))
    fields = payload.get("differing_fields", [])
    if not isinstance(fields, list) or not all(isinstance(item, str) for item in fields):
        raise ValueError(
            f"Paper database recovery pointer is invalid: {pointer}; "
            "no database was selected."
        )
    return payload


def write_paper_database_override(
    database_path: str | Path,
    differing_fields: list[str] | tuple[str, ...],
    home: Path | None = None,
) -> Path:
    """Persist a separate paper DB selection without touching any database."""
    root = Path(home or arena_home())
    root.mkdir(parents=True, exist_ok=True)
    database = Path(database_path).expanduser().resolve(strict=True)
    if database.is_symlink() or not database.is_file():
        raise ValueError("The recovery database must be an existing regular file")
    pointer = paper_database_override_file(root)
    if pointer.exists() or pointer.is_symlink():
        raise FileExistsError("A paper database recovery selection already exists")
    payload = {
        "version": 1,
        "database_path": str(database),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "differing_fields": list(dict.fromkeys(str(item) for item in differing_fields)),
    }
    fd, temporary = tempfile.mkstemp(
        prefix=pointer.name + ".", suffix=".tmp", dir=str(root)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if pointer.exists() or pointer.is_symlink():
            raise FileExistsError("A paper database recovery selection already exists")
        os.replace(temporary, pointer)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return pointer


def clear_paper_database_override(home: Path | None = None) -> bool:
    """Remove only the selection pointer; never delete its database."""
    pointer = paper_database_override_file(home)
    try:
        pointer.unlink()
        return True
    except FileNotFoundError:
        return False


def default_data_dir(home: Path | None = None) -> str:
    """Absolute data directory for a fresh install, independent of the CWD."""
    return str((Path(home or arena_home()) / "data").resolve())


def ensure_utf8_output() -> None:
    """Make stdout/stderr survive a non-UTF-8 console.

    Windows consoles and pipes default to a legacy code page (cp1252), which
    cannot encode characters Arena prints routinely — the arrow in a
    `paper-live` description, the em dash in a banner. Python then raises
    UnicodeEncodeError at the first such character and the whole command dies,
    so `python -m app --help` exited 1 on Windows and the packaged launcher
    would fail before printing its address.

    The streams are reconfigured to UTF-8 with `errors="replace"`: correct
    output on any modern terminal, and a `?` rather than a crash on an ancient
    one. Absent streams (pytest capture, a closed pipe) are left alone.
    """
    for name, stream in (("stdout", sys.stdout), ("stderr", sys.stderr)):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue  # pre-3.7-style stream; nothing to configure
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError) as exc:
            # Not fatal: the stream keeps its original encoding. Recorded
            # rather than swallowed, because output staying on a legacy code
            # page is exactly the condition that caused the original crash and
            # may need to appear in a support log.
            logging.getLogger("bot.runtime").debug(
                "stream_reconfigure_skipped",
                extra={"stream": name, "error": type(exc).__name__},
            )


def describe() -> dict:
    """Diagnostics for the setup page, the startup log and bug reports.

    Paths and field NAMES only — never a secret and never a configuration
    value.
    """
    candidates = env_candidates()
    try:
        override = read_paper_database_override()
        override_error = None
    except ValueError as exc:
        override = None
        override_error = str(exc)
    return {
        "frozen": is_frozen(),
        "portable": is_portable(),
        "arena_home": str(arena_home()),
        "executable_dir": str(executable_dir()),
        "env_file": str(env_file()),
        "env_file_exists": env_file().exists(),
        "env_candidates": [str(p) for p in candidates],
        "dotenv_files_present": [str(p) for p in candidates if p.is_file()],
        "dotenv_precedence": (
            "inherited process environment first; then existing .env files in "
            "env_candidates order, loaded with override=False"
        ),
        "paper_database_override_file": str(paper_database_override_file()),
        "paper_database_override_active": override is not None,
        "paper_database_override_path": (
            override["database_path"] if override is not None else None
        ),
        "paper_database_override_error": override_error,
        "python": sys.version.split()[0],
    }


def static_dir() -> Path:
    """The folder holding Arena's CSS/JS frontend assets.

    From source this is ``app/static``. Frozen, the files are bundled via the
    spec's ``datas`` and extracted under ``sys._MEIPASS`` — the same
    mechanism the bundled sample data uses — so the lookup must branch on
    ``frozen`` exactly like every other bundle-relative path.
    """
    if is_frozen():
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return base / "app" / "static"
    return Path(__file__).resolve().parent / "static"


def output_root() -> Path:
    """Where Arena writes RESULTS, LOGS and CACHE.

    These are outputs, not source: nothing reads them back to run the program.
    From source they stay in the repository, exactly as before. Packaged, they
    must NOT go to ROOT — when frozen, ROOT is PyInstaller's temporary
    extraction directory, which is deleted on exit. A backtest run from the
    .exe would print

        Results: /tmp/_MEIabc123/results/backtest/result.json

    and that path would be gone moments later.
    """
    return arena_home() if is_portable() else _repo_root()
