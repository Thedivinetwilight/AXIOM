"""Read-only compatibility check between a saved paper account and settings.

WHY THIS EXISTS
---------------
`PaperBroker` refuses to attach to a database whose saved account was created
with different settings (initial capital, timeframe, calendar, fees...). That
refusal is correct — silently re-labelling somebody's paper history as a
different account would be worse — but it used to happen inside uvicorn's
lifespan startup. The operator saw a ~60-line traceback ending in
``ValueError: Saved paper account config differs`` and exit code 3, with no
indication of WHICH setting differed or how to proceed.

The launcher now performs this check BEFORE the server (and before the
database is opened for writing), using a strictly read-only connection. A
refusal therefore:

* names the exact non-secret field(s) that differ (never a credential),
* leaves ``.env`` and the database byte-for-byte unchanged,
* exits through a documented code with a recovery command instead of a
  traceback.

The check mirrors `PaperBroker.__init__` exactly; the broker still performs
the authoritative comparison while holding the writer lock (defence in
depth).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

from app.database.paths import (
    DatabaseUrlError,
    is_memory_url,
    resolve_sqlite_path,
    sqlite_path_text,
)

#: ExecutionConfig fields are shown to the operator as the environment names
#: they control, not as dataclass identifiers.
CONFIG_FIELDS = (
    ("initial_capital", "INITIAL_CAPITAL"),
    ("commission", "COMMISSION"),
    ("slippage", "SLIPPAGE"),
    ("spread", "SPREAD"),
    ("position_size", "POSITION_SIZE"),
    ("stop_loss", "STOP_LOSS"),
    ("take_profit", "TAKE_PROFIT"),
    ("max_participation", "MAX_PARTICIPATION"),
)


class PaperDatabaseUnreadable(RuntimeError):
    """The configured database exists but cannot be read as an Arena database."""

    def __init__(self, path: str, detail: str):
        self.path = str(path)
        self.detail = str(detail)
        super().__init__(
            f"The database at {self.path} could not be read: {self.detail}. "
            "Nothing was changed."
        )


@dataclass(frozen=True)
class Difference:
    field: str
    saved: object
    configured: object


class PaperAccountMismatch(RuntimeError):
    """A saved paper account exists and was created with different settings."""

    def __init__(self, differences: list[Difference], *, path: str = ""):
        self.differences = tuple(differences)
        self.path = str(path)
        super().__init__(self._summary())

    def _summary(self) -> str:
        lines = [
            "Saved paper account configuration does not match the current "
            "configuration:"
        ]
        for difference in self.differences:
            lines.append(
                f"  {difference.field}: saved {_render(difference.saved)}, "
                f"configured {_render(difference.configured)}"
            )
        return "\n".join(lines)

    def guidance(self, *, env_file: Path | str | None,
                 recovery_command: str) -> str:
        """Full operator message. No value here is a credential."""
        where = f" in {env_file}" if env_file else ""
        fields = {difference.field for difference in self.differences}
        if len(fields) == 1:
            keep = (f"set {next(iter(fields))} back to the saved value{where}")
        else:
            keep = ("restore the saved values listed above"
                    + (f" in {env_file}" if env_file else ""))
        return (
            f"{self._summary()}\n\n"
            f"The database at {self.path} already contains a paper account. "
            "Arena refuses to start rather than mix two accounts or silently "
            "discard its history. Nothing was changed and no order was placed.\n\n"
            f"To keep this account: {keep}.\n"
            "To start a fresh paper account (the existing database is left "
            f"untouched):\n    {recovery_command}"
        )


def _render(value: object) -> str:
    if isinstance(value, (dict, list, tuple)):
        try:
            return json.dumps(value, sort_keys=True, default=str)
        except (TypeError, ValueError):
            return repr(value)
    return repr(value)


def _connect_read_only(path: Path) -> sqlite3.Connection:
    """Open strictly read-only, without creating side files when possible.

    A plain `mode=ro` connection to a WAL-mode database still creates
    `-shm`/`-wal` side files. `immutable=1` avoids that, but it also ignores a
    pending WAL, so it is only safe when no non-empty WAL exists (i.e. the
    previous process shut down cleanly). A crash leaves a WAL we must read.
    `as_uri()` percent-encodes spaces/unicode and emits the Windows
    file:///C:/... form SQLite accepts in URI mode.
    """
    wal = Path(str(path) + "-wal")
    clean = not (wal.exists() and wal.stat().st_size > 0)
    query = "mode=ro&immutable=1" if clean else "mode=ro"
    connection = sqlite3.connect(f"{path.as_uri()}?{query}", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    return connection


def read_saved_paper(database_url: str, *, home: Path | str | None = None,
                     base: Path | str | None = None) -> dict | None:
    """Return the saved paper payload, or None when no account exists.

    Read-only by construction: the file is opened with ``mode=ro`` and no
    PRAGMA may write. A missing file means "fresh install", not an error.
    """
    if is_memory_url(database_url):
        return None
    path = resolve_sqlite_path(database_url, home=home, base=base)
    if not path.exists():
        return None
    if not path.is_file():
        raise PaperDatabaseUnreadable(str(path), "the path is not a regular file")
    try:
        connection = _connect_read_only(path)
    except (sqlite3.Error, OSError) as exc:
        raise PaperDatabaseUnreadable(
            str(path), f"{type(exc).__name__}: {exc}") from None
    try:
        try:
            verdict = connection.execute("PRAGMA quick_check").fetchone()
            integrity = verdict[0] if verdict else "no result"
        except sqlite3.DatabaseError as exc:
            raise PaperDatabaseUnreadable(
                str(path), f"SQLite integrity check failed: {exc}") from None
        if str(integrity).lower() != "ok":
            raise PaperDatabaseUnreadable(
                str(path), f"SQLite integrity check reported {integrity!r}")
        # `state` is created by the schema; an older/foreign database without
        # it has no saved paper account yet.
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='state'"
        ).fetchone()
        if row is None:
            return None
        saved = connection.execute(
            "SELECT payload FROM state WHERE key='paper'").fetchone()
    except sqlite3.DatabaseError as exc:
        raise PaperDatabaseUnreadable(
            str(path), f"SQLite reported {type(exc).__name__}: {exc}") from None
    finally:
        connection.close()
    if saved is None:
        return None
    try:
        payload = json.loads(saved[0])
    except (TypeError, ValueError) as exc:
        raise PaperDatabaseUnreadable(
            str(path), f"the saved paper account is not valid JSON ({exc})"
        ) from None
    if not isinstance(payload, dict):
        raise PaperDatabaseUnreadable(
            str(path), "the saved paper account has an unexpected shape")
    return payload


def _execution_config(settings):
    from app.backtesting.costs import ExecutionConfig

    return ExecutionConfig(**settings.execution_kwargs())


def compare_saved_paper_state(saved: dict, *, config, currency: str,
                              timeframe: str, calendar,
                              path: str = "") -> PaperAccountMismatch | None:
    """Differences between a saved account and explicit runtime values.

    This is the exact comparison `PaperBroker.__init__` performs (config dict,
    currency, timeframe, calendar signature), expressed once so the broker,
    launcher and diagnostics can never drift apart.
    """
    from app.paper_trading.execution import calendar_signature

    differences: list[Difference] = []
    expected = asdict(config)
    saved_config = saved.get("config")
    if not isinstance(saved_config, dict):
        differences.append(Difference("PAPER_ACCOUNT_CONFIG",
                                      saved_config, expected))
    else:
        for name, label in CONFIG_FIELDS:
            if saved_config.get(name) != expected.get(name):
                differences.append(
                    Difference(label, saved_config.get(name), expected.get(name)))
        for name in sorted(set(saved_config) - set(expected)):
            differences.append(
                Difference(f"CONFIG.{name.upper()}", saved_config[name],
                           "(not a current setting)"))
    if saved.get("currency") != currency:
        differences.append(Difference("CURRENCY", saved.get("currency"), currency))
    if saved.get("timeframe") != timeframe:
        differences.append(
            Difference("DEFAULT_TIMEFRAME", saved.get("timeframe"), timeframe))
    expected_calendar = calendar_signature(calendar)
    saved_calendar = saved.get("calendar", expected_calendar)
    if saved_calendar != expected_calendar:
        differences.append(
            Difference("MARKET_CALENDAR", saved_calendar, expected_calendar))
    return (PaperAccountMismatch(differences, path=path)
            if differences else None)


def compare_saved_paper(saved: dict, settings) -> PaperAccountMismatch | None:
    """Differences between a saved account and the current Settings."""
    from app.paper_trading.execution import make_calendar

    return compare_saved_paper_state(
        saved,
        config=_execution_config(settings),
        currency=settings.currency,
        timeframe=settings.default_timeframe,
        calendar=make_calendar(settings.market_calendar,
                               settings.default_timeframe),
    )


@dataclass(frozen=True)
class PaperAccountView:
    """Truthful, non-secret state of the configured paper account."""

    status: str  # NO_ACCOUNT | MATCH | MISMATCH | UNREADABLE
    database_path: str
    detail: str = ""
    differences: tuple[Difference, ...] = ()

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "database_path": self.database_path,
            "detail": self.detail,
            "differences": [
                {"field": item.field, "saved": _render(item.saved),
                 "configured": _render(item.configured)}
                for item in self.differences
            ],
        }


def inspect_paper_account(settings, *, home: Path | str | None = None,
                          base: Path | str | None = None) -> PaperAccountView:
    """Non-raising inspection used by diagnostics and the launcher.

    ``MISMATCH``/``UNREADABLE`` are reported, not raised; callers decide
    whether that is fatal (the launcher says yes).
    """
    try:
        path = ("" if is_memory_url(settings.database_url)
                else str(resolve_sqlite_path(settings.database_url,
                                             home=home, base=base)))
    except DatabaseUrlError as exc:
        return PaperAccountView("UNREADABLE", "", str(exc))
    try:
        saved = read_saved_paper(settings.database_url, home=home, base=base)
    except PaperDatabaseUnreadable as exc:
        return PaperAccountView("UNREADABLE", path, exc.detail)
    if saved is None:
        return PaperAccountView("NO_ACCOUNT", path)
    try:
        mismatch = compare_saved_paper(saved, settings)
    except ValueError as exc:  # invalid calendar/timeframe combination
        return PaperAccountView("UNREADABLE", path, str(exc))
    if mismatch is None:
        return PaperAccountView("MATCH", path)
    return PaperAccountView("MISMATCH", path, str(mismatch),
                            mismatch.differences)


def ensure_compatible(settings, *, env_file: Path | str | None = None,
                      recovery_command: str = "python arena.py --new-paper-account",
                      home: Path | str | None = None,
                      base: Path | str | None = None) -> PaperAccountView:
    """Raises on mismatch/unreadable; returns the (compatible) view otherwise."""
    view = inspect_paper_account(settings, home=home, base=base)
    if view.status == "UNREADABLE":
        raise PaperDatabaseUnreadable(view.database_path or "(unknown)", view.detail)
    if view.status == "MISMATCH":
        raise PaperAccountMismatch(
            list(view.differences), path=view.database_path)
    return view


def saved_paper_path(database_url: str, *, home=None, base=None) -> Path | None:
    """Convenience for diagnostics; None for in-memory databases."""
    if is_memory_url(database_url):
        return None
    try:
        sqlite_path_text(database_url)
    except DatabaseUrlError:
        return None
    return resolve_sqlite_path(database_url, home=home, base=base)
