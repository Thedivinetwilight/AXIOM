"""RecoveryManager: database backups, verification, and safe restore.

Backups use SQLite's online-backup API (consistent while writers run). Every
backup is verified (integrity_check + core-table counts) before it counts.
Restore REFUSES any backup that fails verification — a corrupt file can never
replace a live database. Restore/backup events are recorded for audit.
"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

CORE_TABLES = ("signals", "sandbox_orders", "sandbox_transitions",
               "live_orders", "live_transitions", "instruments",
               "coverage", "model_versions", "system_events", "engine_state")


def database_path(database) -> str:
    return getattr(database, "path", None) or str(getattr(database, "_path", ""))


class RecoveryManager:
    def __init__(self, database, repository, *, backup_dir: str = "backups"):
        self.database = database
        self.repository = repository
        self.backup_dir = Path(backup_dir)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self._path = str(database_path(database))

    def backup(self, label: str = "scheduled") -> dict:
        target = self.backup_dir / (
            f"arena-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{label}.db")
        source = sqlite3.connect(self._path)
        destination = sqlite3.connect(str(target))
        with destination:
            source.backup(destination)
        source.close()
        destination.close()
        verification = self.verify(str(target))
        result = {"backup": str(target), "label": label,
                  "verified": verification["ok"], "integrity": verification}
        self.repository.record_system_event("BACKUP_CREATED",
                                            {"path": str(target),
                                             "verified": verification["ok"]})
        return result

    def verify(self, path: str | Path) -> dict:
        path = Path(path)
        if not path.exists():
            return {"ok": False, "integrity": "missing", "row_counts": {}}
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                integrity = connection.execute(
                    "PRAGMA integrity_check").fetchone()[0]
                row_counts = {}
                for table in CORE_TABLES:
                    try:
                        row_counts[table] = connection.execute(
                            f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    except sqlite3.DatabaseError:
                        row_counts[table] = None
            finally:
                connection.close()
        except sqlite3.DatabaseError:
            return {"ok": False, "integrity": "not_a_database", "row_counts": {}}
        ok = integrity == "ok" and all(
            count is not None for count in row_counts.values())
        return {"ok": bool(ok), "integrity": integrity,
                "row_counts": row_counts}

    def restore(self, backup_path: str | Path, *,
                target_path: str | None = None) -> dict:
        backup_path = Path(backup_path)
        if not backup_path.exists():
            raise FileNotFoundError(f"backup not found: {backup_path}")
        verification = self.verify(backup_path)
        if not verification["ok"]:
            self.repository.record_system_event(
                "RESTORE_REFUSED", {"backup": str(backup_path)})
            return {"restored": False, "reason": "backup failed verification",
                    "integrity": verification}
        destination = target_path or self._path
        shutil.copy2(backup_path, destination)
        self.repository.record_system_event(
            "RESTORE_COMPLETED", {"backup": str(backup_path),
                                  "target": destination})
        return {"restored": True, "target": destination,
                "integrity": verification}
