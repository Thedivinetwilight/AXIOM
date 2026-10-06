"""Consistent SQLite backup and fail-safe restore (master spec section 68).

Each archive contains a SQLite online-backup snapshot plus explicitly selected
non-secret files. Its versioned manifest records a SHA-256 and byte length for
every regular member. Verification rejects missing, duplicate, unlisted,
malformed, linked, truncated, or tampered members without extracting paths from
the archive.

A restore requested while the application is running is prepared beside the
database and applied on the next startup, before SQLite opens it. Offline
restores retain an independent pre-restore snapshot and replace the database
atomically. Hashes detect accidental changes; the manifest is not a signature
and does not establish the authenticity of the backup's source.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import socket
import sqlite3
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

MEMBER_DB = "trading_bot.db"
MEMBER_MANIFEST = "manifest.json"
BACKUP_PREFIX = "axiom-backup-"
MANIFEST_SCHEMA = 1
MAX_MANIFEST_BYTES = 1 << 20
MAX_ARCHIVE_MEMBERS = 256
SQLITE_COPY_CHUNK = 1 << 20
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
logger = logging.getLogger("app.system.backup")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_stream(handle, output=None) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = handle.read(65536)
        if not chunk:
            break
        digest.update(chunk)
        size += len(chunk)
        if output is not None:
            output.write(chunk)
    return digest.hexdigest(), size


def _sqlite_readonly_uri(path: Path) -> str:
    return path.resolve().as_uri() + "?mode=ro"


def _sqlite_check(path: Path) -> list[str]:
    try:
        connection = sqlite3.connect(_sqlite_readonly_uri(path), uri=True, timeout=15)
        try:
            rows = connection.execute("PRAGMA quick_check").fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        return [f"staged database unreadable: {type(exc).__name__}: {exc}"]
    verdicts = [str(row[0]) for row in rows]
    return [] if verdicts == ["ok"] else [
        "staged database integrity: " + "; ".join(verdicts or ["no quick_check result"])
    ]


def _sqlite_snapshot(source: Path, destination: Path) -> None:
    """Take a transactionally consistent SQLite snapshot, including WAL data."""
    if not source.is_file():
        raise FileNotFoundError(f"database not found at {source}")
    source_db = sqlite3.connect(_sqlite_readonly_uri(source), uri=True, timeout=30)
    destination_db = sqlite3.connect(str(destination), timeout=30)
    try:
        source_db.backup(destination_db, pages=256, sleep=0.02)
        destination_db.commit()
    finally:
        destination_db.close()
        source_db.close()
    problems = _sqlite_check(destination)
    if problems:
        raise RuntimeError("SQLite snapshot failed integrity check: " + "; ".join(problems))


def _checkpoint_sqlite(path: Path) -> None:
    """Checkpoint WAL before an offline file replacement; refuse on busy state."""
    if not path.exists():
        return
    connection = sqlite3.connect(str(path), timeout=15)
    try:
        result = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if result is not None and result[0] != 0:
            raise RuntimeError("database WAL checkpoint is busy; refusing file replacement")
    finally:
        connection.close()


def _remove_sqlite_sidecars(path: Path) -> None:
    # Called only after the application/engine is known to be stopped and the
    # current DB has been checkpointed. Leaving an old WAL beside a replacement
    # database could replay pages from the wrong database file.
    for suffix in ("-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)


def _unique_sibling(path: Path, marker: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return Path(f"{path}.{marker}-{stamp}-{uuid4().hex[:10]}")


def _plain_member_name(name: object) -> bool:
    if not isinstance(name, str) or not name or name in {".", "..", MEMBER_MANIFEST}:
        return False
    if "/" in name or "\\" in name or Path(name).name != name:
        return False
    return not name.startswith("..")


def _json_object_no_duplicates(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"duplicate manifest key: {key}")
        obj[key] = value
    return obj


class BackupManager:
    """Create, verify, and restore backup archives.

    ``active_database=True`` is used by the running API: restore then stages a
    verified database for the next startup instead of replacing a file that an
    open SQLite connection may still use. Direct/offline callers may leave it
    false only when they have stopped all database users.
    """

    def __init__(self, db_path: str | Path, *, backup_dir: str | Path,
                 extra_files: dict[str, str | Path] | None = None,
                 active_database: bool = False):
        self.db_path = Path(db_path)
        self.backup_dir = Path(backup_dir)
        self.extra_files = dict(extra_files or {})
        self.active_database = bool(active_database)

    # --------------------------------------------------------------- backup --
    def create(self, *, note: str | None = None) -> dict:
        if not self.db_path.is_file():
            raise FileNotFoundError(f"database not found at {self.db_path}")
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        archive_path = self.backup_dir / (
            f"{BACKUP_PREFIX}{stamp}-{uuid4().hex[:8]}.tar.gz")
        temp_archive = self.backup_dir / f".{archive_path.name}.{uuid4().hex}.tmp"

        with tempfile.TemporaryDirectory(prefix=".axiom-backup-",
                                         dir=self.backup_dir) as temporary:
            work_dir = Path(temporary)
            snapshot = work_dir / MEMBER_DB
            _sqlite_snapshot(self.db_path, snapshot)
            staged_members: dict[str, Path] = {MEMBER_DB: snapshot}
            for member_name, source in self.extra_files.items():
                if not _plain_member_name(member_name):
                    raise ValueError(f"unsafe backup member name: {member_name!r}")
                source_path = Path(source)
                if not source_path.exists():
                    continue
                if not source_path.is_file():
                    raise ValueError(f"backup member source is not a file: {source_path}")
                staged = work_dir / f"extra-{uuid4().hex}"
                shutil.copyfile(source_path, staged)
                staged_members[member_name] = staged

            members = {
                name: {"sha256": _sha256(path), "bytes": path.stat().st_size}
                for name, path in staged_members.items()
            }
            manifest = {
                "schema": MANIFEST_SCHEMA,
                "created_utc": datetime.now(timezone.utc).isoformat(),
                "note": str(note or ""),
                "members": members,
                "excluded": ["API_TOKEN", "UPSTOX_*_ACCESS_TOKEN", "client secrets"],
            }
            manifest_bytes = json.dumps(
                manifest, indent=2, sort_keys=True, allow_nan=False).encode("utf-8")
            if len(manifest_bytes) > MAX_MANIFEST_BYTES:
                raise ValueError("backup manifest exceeds the size limit")

            try:
                with tarfile.open(temp_archive, "w:gz", encoding="utf-8") as archive:
                    for name, path in staged_members.items():
                        archive.add(str(path), arcname=name, recursive=False)
                    info = tarfile.TarInfo(MEMBER_MANIFEST)
                    info.size = len(manifest_bytes)
                    info.mode = 0o600
                    archive.addfile(info, _BytesReader(manifest_bytes))
                with open(temp_archive, "rb") as handle:
                    os.fsync(handle.fileno())
                os.chmod(temp_archive, 0o600)
                os.replace(temp_archive, archive_path)
            finally:
                temp_archive.unlink(missing_ok=True)

        return {"path": str(archive_path), "sha256": _sha256(archive_path),
                "members": members, "note": str(note or "")}

    # --------------------------------------------------------------- list --
    def list(self) -> list[dict]:
        if not self.backup_dir.exists():
            return []
        archives = sorted(self.backup_dir.glob(f"{BACKUP_PREFIX}*.tar.gz"))
        return [{"path": str(archive), "sha256": _sha256(archive),
                 "bytes": archive.stat().st_size,
                 "modified_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                               time.gmtime(archive.stat().st_mtime))}
                for archive in archives if archive.is_file()]

    # --------------------------------------------------------------- verify --
    def verify(self, archive_path: str | Path) -> dict:
        archive_path = Path(archive_path)
        problems: list[str] = []
        member_hashes: dict[str, str] = {}
        if not archive_path.is_file():
            return {"ok": False, "problems": ["backup archive not found"],
                    "members": [], "member_hashes": {}}
        temporary_database: Path | None = None
        try:
            with tarfile.open(archive_path, "r:gz", encoding="utf-8") as archive:
                infos = []
                for index, info in enumerate(archive):
                    if index >= MAX_ARCHIVE_MEMBERS:
                        problems.append("archive contains too many members")
                        break
                    infos.append(info)
                names = [info.name for info in infos]
                if len(names) != len(set(names)):
                    problems.append("archive contains duplicate member names")
                by_name = {info.name: info for info in infos}
                manifest_info = by_name.get(MEMBER_MANIFEST)
                if manifest_info is None or not manifest_info.isfile():
                    problems.append("archive has no regular manifest.json")
                    manifest = None
                elif manifest_info.size > MAX_MANIFEST_BYTES:
                    problems.append("manifest.json exceeds the size limit")
                    manifest = None
                else:
                    handle = archive.extractfile(manifest_info)
                    if handle is None:
                        problems.append("manifest.json is unreadable")
                        manifest = None
                    else:
                        raw = handle.read(MAX_MANIFEST_BYTES + 1)
                        if len(raw) > MAX_MANIFEST_BYTES:
                            problems.append("manifest.json exceeds the size limit")
                            manifest = None
                        else:
                            try:
                                manifest = json.loads(
                                    raw.decode("utf-8"),
                                    object_pairs_hook=_json_object_no_duplicates)
                            except (UnicodeError, ValueError, TypeError):
                                problems.append("manifest.json is malformed")
                                manifest = None

                if manifest is not None:
                    structural = self._validate_manifest(manifest, by_name)
                    problems.extend(structural)
                    declared = manifest.get("members", {}) if isinstance(manifest, dict) else {}
                    if isinstance(declared, dict) and not structural:
                        expected_names = set(declared) | {MEMBER_MANIFEST}
                        if set(names) != expected_names:
                            problems.append(
                                "archive members do not exactly match the manifest")
                    if isinstance(declared, dict) and not structural:
                        for name, metadata in declared.items():
                            info = by_name.get(name)
                            if info is None or not info.isfile():
                                problems.append(f"missing or non-regular member {name}")
                                continue
                            if info.size != metadata["bytes"]:
                                problems.append(f"size mismatch for {name}")
                                continue
                            handle = archive.extractfile(info)
                            if handle is None:
                                problems.append(f"unreadable member {name}")
                                continue
                            output = None
                            if name == MEMBER_DB:
                                temp = tempfile.NamedTemporaryFile(
                                    suffix=".db", delete=False)
                                temporary_database = Path(temp.name)
                                output = temp
                            else:
                                temp = None
                            try:
                                digest, count = _sha256_stream(handle, output)
                            finally:
                                if temp is not None:
                                    temp.close()
                            member_hashes[name] = digest
                            if count != metadata["bytes"]:
                                problems.append(f"byte count mismatch for {name}")
                            if digest != metadata["sha256"]:
                                problems.append(f"hash mismatch for {name}")
                            if name == MEMBER_DB and temporary_database is not None:
                                problems.extend(_sqlite_check(temporary_database))
        except (OSError, tarfile.TarError, EOFError, ValueError, sqlite3.Error) as exc:
            problems.append(f"archive could not be verified ({type(exc).__name__})")
        finally:
            if temporary_database is not None:
                temporary_database.unlink(missing_ok=True)

        if MEMBER_DB not in member_hashes and not any("manifest" in p for p in problems):
            problems.append(f"required database member {MEMBER_DB} was not verified")
        return {"ok": not problems, "problems": problems,
                "members": sorted(member_hashes), "member_hashes": member_hashes}

    @staticmethod
    def _validate_manifest(manifest, archive_members: dict[str, tarfile.TarInfo]) -> list[str]:
        problems: list[str] = []
        if not isinstance(manifest, dict):
            return ["manifest.json must contain an object"]
        schema = manifest.get("schema", MANIFEST_SCHEMA)
        if type(schema) is not int or schema != MANIFEST_SCHEMA:
            problems.append(f"unsupported manifest schema: {schema!r}")
        declared = manifest.get("members")
        if not isinstance(declared, dict):
            return problems + ["manifest members must be an object"]
        if MEMBER_DB not in declared:
            problems.append(f"manifest does not hash required database member {MEMBER_DB}")
        for name, metadata in declared.items():
            if not _plain_member_name(name) or name == MEMBER_MANIFEST:
                problems.append(f"unsafe manifest member name: {name!r}")
                continue
            if not isinstance(metadata, dict):
                problems.append(f"manifest metadata for {name} must be an object")
                continue
            digest = metadata.get("sha256")
            size = metadata.get("bytes")
            if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
                problems.append(f"manifest SHA-256 for {name} is malformed")
            if type(size) is not int or size < 0:
                problems.append(f"manifest byte length for {name} is malformed")
            info = archive_members.get(name)
            if info is None:
                problems.append(f"manifest member {name} is missing from archive")
            elif not info.isfile():
                problems.append(f"archive member {name} is not a regular file")
        return problems

    def _read_manifest(self, archive_path: Path) -> dict | None:
        """Compatibility helper returning a parsed object only when it is safe."""
        try:
            with tarfile.open(archive_path, "r:gz", encoding="utf-8") as archive:
                infos = [info for _, info in zip(range(MAX_ARCHIVE_MEMBERS + 1), archive)]
                if len(infos) > MAX_ARCHIVE_MEMBERS:
                    return None
                selected = [info for info in infos if info.name == MEMBER_MANIFEST]
                if len(selected) != 1 or not selected[0].isfile() \
                        or selected[0].size > MAX_MANIFEST_BYTES:
                    return None
                handle = archive.extractfile(selected[0])
                if handle is None:
                    return None
                raw = handle.read(MAX_MANIFEST_BYTES + 1)
                if len(raw) > MAX_MANIFEST_BYTES:
                    return None
                value = json.loads(raw.decode("utf-8"),
                                   object_pairs_hook=_json_object_no_duplicates)
                return value if isinstance(value, dict) else None
        except (OSError, tarfile.TarError, EOFError, UnicodeError, ValueError, TypeError):
            return None

    # -------------------------------------------------------------- restore --
    def restore(self, archive_path: str | Path) -> dict:
        """Verify and stage/swap a database without ever losing the old path."""
        archive_path = Path(archive_path)
        verification = self.verify(archive_path)
        if not verification["ok"]:
            return {"restored": False, "verification": verification,
                    "note": "restore refused: verification failed; active state untouched"}

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        staging = _unique_sibling(self.db_path, "restore-staging")
        pending = Path(f"{self.db_path}.restore-pending")
        expected_digest = verification["member_hashes"].get(MEMBER_DB)
        try:
            with tarfile.open(archive_path, "r:gz", encoding="utf-8") as archive:
                infos = [info for _, info in zip(range(MAX_ARCHIVE_MEMBERS + 1), archive)]
                database_infos = [info for info in infos if info.name == MEMBER_DB]
                if (len(infos) > MAX_ARCHIVE_MEMBERS or len(database_infos) != 1
                        or not database_infos[0].isfile()):
                    raise ValueError("required database member changed after verification")
                handle = archive.extractfile(database_infos[0])
                if handle is None:
                    raise ValueError("verified database member became unreadable")
                with open(staging, "xb") as output:
                    digest, size = _sha256_stream(handle, output)
                    output.flush()
                    os.fsync(output.fileno())
            if digest != expected_digest:
                raise ValueError("database member changed after archive verification")
            if size != database_infos[0].size:
                raise ValueError("database member byte count changed after verification")
            os.chmod(staging, 0o600)
            problems = _sqlite_check(staging)
            if problems:
                raise ValueError("staged database failed integrity: " + "; ".join(problems))
        except (OSError, tarfile.TarError, EOFError, ValueError, sqlite3.Error) as exc:
            staging.unlink(missing_ok=True)
            return {"restored": False, "verification": verification,
                    "note": f"restore refused before touching active state ({type(exc).__name__})"}

        if self.active_database:
            if pending.exists() or pending.is_symlink():
                staging.unlink(missing_ok=True)
                return {"restored": False, "status": "RESTORE_REFUSED_PENDING_EXISTS",
                        "verification": verification,
                        "note": "a restore is already pending; it was preserved and not overwritten"}
            try:
                os.replace(staging, pending)
            except OSError as exc:
                staging.unlink(missing_ok=True)
                return {"restored": False, "status": "RESTORE_REFUSED",
                        "verification": verification,
                        "note": f"could not stage verified restore ({type(exc).__name__})"}
            return {"restored": False, "status": "RESTORE_PREPARED_RESTART_REQUIRED",
                    "pending_database": str(pending), "verification": verification,
                    "note": "verified database staged; restart AXIOM to apply it before "
                            "SQLite opens the database"}

        previous = None
        try:
            # Preserve a consistent rollback snapshot before touching the
            # current file. The replace itself is atomic and never moves the
            # active path out of the way first.
            if self.db_path.exists():
                previous = _unique_sibling(self.db_path, "pre-restore")
                _sqlite_snapshot(self.db_path, previous)
                os.chmod(previous, 0o600)
                _checkpoint_sqlite(self.db_path)
                _remove_sqlite_sidecars(self.db_path)
            os.replace(staging, self.db_path)
        except (OSError, RuntimeError, sqlite3.Error) as exc:
            # Even a failed target rename leaves the original database path in
            # place. Keep the verified restore for explicit startup recovery.
            if not pending.exists() and staging.exists():
                try:
                    os.replace(staging, pending)
                except OSError:
                    staging.unlink(missing_ok=True)
            else:
                staging.unlink(missing_ok=True)
            return {"restored": False,
                    "status": "RESTORE_PREPARED_RESTART_REQUIRED" if pending.exists()
                    else "RESTORE_REFUSED",
                    "pending_database": str(pending) if pending.exists() else None,
                    "previous_database": str(previous) if previous and previous.exists()
                    else None,
                    "verification": verification,
                    "note": f"restore not applied ({type(exc).__name__}); the active "
                            "database path was not moved away"}

        return {"restored": True, "verification": verification,
                "previous_database": str(previous) if previous else None,
                "note": "restore complete; a consistent previous database snapshot "
                        "is preserved beside the active database"}


def _live_lease_blocks_restore(path: Path) -> str | None:
    """Fail closed if an apparently live engine may still use this database."""
    if not path.is_file():
        return None
    try:
        connection = sqlite3.connect(_sqlite_readonly_uri(path), uri=True, timeout=5)
        try:
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='engine_lease'"
            ).fetchone()
            if table is None:
                return None
            row = connection.execute(
                "SELECT owner,pid,host,heartbeat_ts,live FROM engine_lease WHERE name=?",
                ("engine_lock",)).fetchone()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        return f"could not establish live-engine lease state ({type(exc).__name__})"
    if row is None:
        return None
    if type(row[4]) not in (int, bool) or int(row[4]) not in (0, 1):
        return "live-engine lease mode is malformed"
    if int(row[4]) == 0:
        return None
    try:
        if not isinstance(row[0], str) or not row[0].strip():
            return "live-engine lease owner is missing"
        if type(row[1]) is not int or row[1] <= 0:
            return "live-engine lease PID is malformed"
        pid = row[1]
        host = str(row[2])
        heartbeat = datetime.fromisoformat(str(row[3]).replace("Z", "+00:00"))
        if heartbeat.tzinfo is None or heartbeat.utcoffset() is None:
            return "live-engine lease heartbeat has no timezone"
        heartbeat = heartbeat.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return "live-engine lease is malformed"
    age = (datetime.now(timezone.utc) - heartbeat).total_seconds()
    if not (age > 180 and age < 10 * 365 * 24 * 3600):
        return "live-engine lease heartbeat is fresh or invalid"
    if host != socket.gethostname():
        return "live-engine owner is remote; its process cannot be proven stopped"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return "live-engine process exists but its status is not verifiable"
    except OSError:
        return "live-engine process liveness is unknown"
    return "live-engine owner process is still alive"


def apply_pending_restore(db_path: str | Path) -> dict | None:
    """Apply a verified pending restore before SQLite opens the active database.

    A live/unknown engine lease blocks replacement. Refusal leaves both the
    active database and staged restore intact, allowing the application to
    start conservatively while the operator stops/reconciles any surviving
    engine. Corrupt staging is never used.
    """
    path = Path(db_path)
    pending = Path(f"{path}.restore-pending")
    if not pending.exists() and not pending.is_symlink():
        return None
    if pending.is_symlink() or not pending.is_file():
        return {"applied": False, "status": "RESTORE_PENDING_INVALID",
                "detail": "pending restore is not a regular file"}
    problems = _sqlite_check(pending)
    if problems:
        return {"applied": False, "status": "RESTORE_PENDING_INVALID",
                "detail": "; ".join(problems)}
    lease_problem = _live_lease_blocks_restore(path)
    if lease_problem:
        return {"applied": False, "status": "RESTORE_PENDING_BLOCKED",
                "detail": lease_problem}

    previous = None
    try:
        if path.exists():
            previous = _unique_sibling(path, "pre-restore")
            _sqlite_snapshot(path, previous)
            os.chmod(previous, 0o600)
            _checkpoint_sqlite(path)
            _remove_sqlite_sidecars(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(pending, path)
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        return {"applied": False, "status": "RESTORE_PENDING_BLOCKED",
                "detail": f"atomic startup restore failed ({type(exc).__name__})",
                "previous_database": str(previous) if previous else None}
    return {"applied": True, "status": "RESTORED_ON_STARTUP",
            "previous_database": str(previous) if previous else None}


class _BytesReader:
    """Small file-like adapter; avoids importing io for the manifest write."""

    def __init__(self, data: bytes):
        self._data = data
        self._position = 0

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self._data) - self._position
        start = self._position
        self._position = min(len(self._data), self._position + size)
        return self._data[start:self._position]
