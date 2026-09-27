import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from app.database.models import SCHEMA, migrate_transitions_child_identity


class Database:
    def __init__(self, url: str):
        if not url.startswith("sqlite:///"):
            raise ValueError("Only SQLite URLs are supported")
        self.path = url[len("sqlite:///") :]
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
        self.connection.commit()

    @contextmanager
    def transaction(self):
        with self.lock, self.connection:
            yield self.connection

    def close(self):
        with self.lock:
            self.connection.close()
