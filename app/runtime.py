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
"""

from __future__ import annotations

import logging
import os
import sys
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
    """Diagnostics for the setup page and bug reports."""
    return {
        "frozen": is_frozen(),
        "portable": is_portable(),
        "arena_home": str(arena_home()),
        "executable_dir": str(executable_dir()),
        "env_file": str(env_file()),
        "env_file_exists": env_file().exists(),
        "env_candidates": [str(p) for p in env_candidates()],
        "python": sys.version.split()[0],
    }
