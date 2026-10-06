import json
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from app.database.models import SCHEMA, migrate_transitions_child_identity

logger = logging.getLogger("app.database")


class Database:
    def __init__(self, url: str):
        if not url.startswith("sqlite:///"):
            raise ValueError("Only SQLite URLs are supported")
        self.path = url[len("sqlite:///") :]
        if self.path != ":memory:":
            from app.system.backup import apply_pending_restore

            restore = apply_pending_restore(self.path)
            if restore is not None:
                if restore.get("applied"):
                    logger.info("pending database restore applied on startup")
                else:
                    logger.warning("pending database restore not applied (%s): %s",
                                   restore.get("status", "unknown"),
                                   restore.get("detail", "no detail"))
                    if not Path(self.path).exists():
                        raise RuntimeError(
                            "a restore is pending but the active database is absent; "
                            "refusing to create an empty database")
        is_new_database = self.path == ":memory:" or not Path(self.path).exists()
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = RLock()
        self.connection = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(SCHEMA)
        # Arena 3.0.3: backward-compatible per-child transition identity
        # migration (schema_version 5; runs at most once per database).
        migrate_transitions_child_identity(self.connection)
        if is_new_database:
            # Only a genuinely new database starts clear, and it does so with
            # an explicit durable record. Existing/missing legacy state is
            # never silently migrated to "clear"; read_emergency_stop treats
            # it as unknown/active until an operator deliberately resumes.
            self.connection.execute(
                "INSERT OR IGNORE INTO engine_state(key, payload) VALUES (?, ?)",
                ("emergency_stop", json.dumps({
                    "stopped": False,
                    "reason": "explicit initial state for a new database",
                    "source": "database-bootstrap",
                }, separators=(",", ":"))),
            )
        self.connection.commit()

    @contextmanager
    def transaction(self):
        with self.lock, self.connection:
            yield self.connection

    def close(self):
        with self.lock:
            self.connection.close()
