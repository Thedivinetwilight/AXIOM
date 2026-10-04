"""Backup / restore (master spec section 68).

Backups are a single archive containing the database plus non-secret
workspace settings, with a manifest of SHA-256 hashes. Restore verifies
EVERY member against the manifest before touching active state, and restores
to a staging copy first — a partial or corrupted backup never half-replaces
live state. Secrets are never copied: the API token and broker credentials
stay in their secure paths and are explicitly excluded.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tarfile
import time
from pathlib import Path

MEMBER_DB = "trading_bot.db"
MEMBER_MANIFEST = "manifest.json"
BACKUP_PREFIX = "axiom-backup-"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


class BackupManager:
    def __init__(self, db_path: str | Path, *, backup_dir: str | Path,
                 extra_files: dict[str, str | Path] | None = None):
        self.db_path = Path(db_path)
        self.backup_dir = Path(backup_dir)
        self.extra_files = dict(extra_files or {})

    # --------------------------------------------------------------- backup --
    def create(self, *, note: str | None = None) -> dict:
        if not self.db_path.exists():
            raise FileNotFoundError(f"database not found at {self.db_path}")
        self._check_database_readable()
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        archive_path = self.backup_dir / f"{BACKUP_PREFIX}{stamp}.tar.gz"
        manifest: dict = {
            "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "note": note or "",
            "members": {},
            "excluded": ["API_TOKEN", "UPSTOX_*_ACCESS_TOKEN", "client secrets"],
        }
        with tarfile.open(archive_path, "w:gz", encoding="utf-8") as archive:
            self._add(archive, MEMBER_DB, self.db_path, manifest)
            for member_name, source in self.extra_files.items():
                source_path = Path(source)
                if source_path.exists():
                    self._add(archive, member_name, source_path, manifest)
            manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
            info = tarfile.TarInfo(MEMBER_MANIFEST)
            info.size = len(manifest_bytes)
            import io

            archive.addfile(info, io.BytesIO(manifest_bytes))
        return {"path": str(archive_path), "sha256": _sha256(archive_path),
                "members": manifest["members"], "note": manifest["note"]}

    def _add(self, archive: tarfile.TarFile, member_name: str, source: Path,
             manifest: dict) -> None:
        # Defense against path traversal in caller-supplied member names.
        if member_name != MEMBER_DB and ("/" in member_name or "\\" in member_name
                                         or member_name.startswith("..")):
            raise ValueError(f"unsafe backup member name: {member_name!r}")
        archive.add(str(source), arcname=member_name)
        manifest["members"][member_name] = {"sha256": _sha256(source),
                                            "bytes": source.stat().st_size}

    def _check_database_readable(self) -> None:
        connection = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            connection.close()
        if integrity != "ok":
            raise RuntimeError(f"refusing to back up a corrupt database: {integrity}")

    # ----------------------------------------------------------------- list --
    def list(self) -> list[dict]:
        if not self.backup_dir.exists():
            return []
        archives = sorted(self.backup_dir.glob(f"{BACKUP_PREFIX}*.tar.gz"))
        return [{"path": str(a), "sha256": _sha256(a),
                 "bytes": a.stat().st_size,
                 "modified_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                               time.gmtime(a.stat().st_mtime))}
                for a in archives]

    # --------------------------------------------------------------- verify --
    def verify(self, archive_path: str | Path) -> dict:
        archive_path = Path(archive_path)
        problems: list[str] = []
        manifest = self._read_manifest(archive_path)
        if manifest is None:
            return {"ok": False, "problems": ["archive has no manifest.json"]}
        with tarfile.open(archive_path, "r:gz", encoding="utf-8") as archive:
            names = set(archive.getnames())
            for member, meta in manifest["members"].items():
                if member not in names:
                    problems.append(f"missing member {member}")
                    continue
                handle = archive.extractfile(member)
                if handle is None:
                    problems.append(f"unreadable member {member}")
                    continue
                digest = hashlib.sha256(handle.read()).hexdigest()
                if digest != meta.get("sha256"):
                    problems.append(f"hash mismatch for {member}")
            if MEMBER_DB in names:
                handle = archive.extractfile(MEMBER_DB)
                if handle is not None:
                    problems.extend(self._check_sqlite_bytes(handle.read()))
        return {"ok": not problems, "problems": problems,
                "members": list(manifest["members"])}

    def _read_manifest(self, archive_path: Path) -> dict | None:
        try:
            with tarfile.open(archive_path, "r:gz", encoding="utf-8") as archive:
                handle = archive.extractfile(MEMBER_MANIFEST)
                if handle is None:
                    return None
                return json.loads(handle.read().decode("utf-8"))
        except (tarfile.TarError, ValueError, OSError):
            return None

    @staticmethod
    def _check_sqlite_bytes(data: bytes) -> list[str]:
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        try:
            connection = sqlite3.connect(tmp_path)
            try:
                integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
            finally:
                connection.close()
            return [] if integrity == "ok" else [f"staged database integrity: {integrity}"]
        except sqlite3.Error as exc:
            return [f"staged database unreadable: {exc}"]
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    # -------------------------------------------------------------- restore --
    def restore(self, archive_path: str | Path) -> dict:
        """Verify fully, then atomically swap the database.

        The previous database is kept beside the new one (``.pre-restore``)
        so a bad restore is reversible; the swap happens only after the
        archive verified clean.
        """
        verification = self.verify(archive_path)
        if not verification["ok"]:
            return {"restored": False, "verification": verification,
                    "note": "restore refused: verification failed; active state untouched"}
        archive_path = Path(archive_path)
        staging = self.db_path.with_suffix(self.db_path.suffix + ".restore-staging")
        with tarfile.open(archive_path, "r:gz", encoding="utf-8") as archive:
            handle = archive.extractfile(MEMBER_DB)
            if handle is None:
                return {"restored": False, "verification": verification,
                        "note": "archive verified but database member unreadable"}
            staging.write_bytes(handle.read())
        connection = sqlite3.connect(staging)
        try:
            integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            connection.close()
        if integrity != "ok":
            staging.unlink(missing_ok=True)
            return {"restored": False, "verification": verification,
                    "note": f"staged database failed integrity ({integrity}); untouched"}
        previous = self.db_path.with_suffix(self.db_path.suffix + ".pre-restore")
        if self.db_path.exists():
            self.db_path.replace(previous)
        staging.replace(self.db_path)
        return {"restored": True, "verification": verification,
                "previous_database": str(previous) if previous.exists() else None,
                "note": "restore complete; the previous database is preserved "
                        "alongside for rollback"}
