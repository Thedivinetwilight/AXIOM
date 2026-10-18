"""Runtime model cache and integrity manifest for Kronos weights.

Weights never live in Git and the verified runtime does not download them.
Externally staged model/tokenizer files live in a per-user cache and are tracked
by a manifest that records model id, revision, file size, SHA-256 and the
staging timestamp. Loading verifies the digest first, so a corrupted or swapped
file cannot be silently loaded.

Windows:  ``%LOCALAPPDATA%\\AXIOM\\models\\kronos``
macOS:    ``~/Library/Caches/AXIOM/models/kronos``
other:    ``$XDG_CACHE_HOME/AXIOM/models/kronos`` (default ``~/.cache``)
override: ``AXIOM_MODEL_CACHE``
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import PurePosixPath, PureWindowsPath
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from axiom.models import utcnow

MANIFEST_NAME = "manifest.json"
CHUNK = 1 << 20


def _expand_user_path(value: str, home: Path) -> Path:
    """Expand a leading '~' against ``home``, not the process environment.

    ``Path.expanduser()`` reads USERPROFILE on Windows and HOME elsewhere, so
    the same value would land in two different directories depending on the
    platform — and this function is handed ``home`` precisely so its answer
    does not depend on the interpreter's environment. Only the forms that
    mean "this user's home" are rewritten; anything else keeps the ordinary
    expanduser() handling.
    """
    if value in ("~", "~/", "~\\"):
        return home
    if value.startswith(("~/", "~\\")):
        return home / value[2:]
    return Path(value).expanduser()


def cache_root_for(
    *,
    override: str | None,
    os_name: str,
    platform: str,
    localappdata: str | None,
    xdg_cache_home: str | None,
    home: Path,
) -> Path:
    """Pure location logic — testable without monkeypatching the interpreter."""
    if override:
        return _expand_user_path(override, home)
    if os_name == "nt":
        base = Path(localappdata) if localappdata else home / "AppData" / "Local"
        return base / "AXIOM" / "models" / "kronos"
    if platform == "darwin":
        return home / "Library" / "Caches" / "AXIOM" / "models" / "kronos"
    base = _expand_user_path(xdg_cache_home, home) if xdg_cache_home else home / ".cache"
    return base / "AXIOM" / "models" / "kronos"


def default_cache_root() -> Path:
    return cache_root_for(
        override=os.environ.get("AXIOM_MODEL_CACHE"),
        os_name=os.name,
        platform=sys.platform,
        localappdata=os.environ.get("LOCALAPPDATA"),
        xdg_cache_home=os.environ.get("XDG_CACHE_HOME"),
        home=Path.home(),
    )


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class ModelManifestEntry:
    model_id: str
    revision: str
    file_name: str
    relative_path: str
    size_bytes: int
    sha256: str
    downloaded_at: datetime

    @property
    def key(self) -> str:
        return f"{self.model_id}@{self.revision or 'default'}"

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["downloaded_at"] = self.downloaded_at.isoformat()
        return payload

    @classmethod
    def from_dict(cls, payload: dict) -> "ModelManifestEntry":
        if not isinstance(payload, dict):
            raise TypeError("manifest entry must be an object")
        model_id = payload.get("model_id")
        revision = payload.get("revision", "")
        file_name = payload.get("file_name")
        relative_path = payload.get("relative_path")
        size_bytes = payload.get("size_bytes")
        digest = payload.get("sha256")
        stamp = payload.get("downloaded_at")
        if (not isinstance(model_id, str) or not model_id.strip()
                or not isinstance(revision, str)
                or not isinstance(file_name, str) or not file_name
                or not isinstance(relative_path, str) or not relative_path
                or type(size_bytes) is not int or size_bytes < 0
                or not isinstance(digest, str)
                or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest.lower())):
            raise ValueError("manifest entry fields are malformed")
        if not isinstance(stamp, str):
            raise ValueError("manifest timestamp is malformed")
        downloaded_at = datetime.fromisoformat(stamp)
        if downloaded_at.tzinfo is None or downloaded_at.utcoffset() is None:
            raise ValueError("manifest timestamp must be timezone-aware")
        return cls(
            model_id=model_id,
            revision=revision,
            file_name=file_name,
            relative_path=relative_path,
            size_bytes=size_bytes,
            sha256=digest.lower(),
            downloaded_at=downloaded_at,
        )


@dataclass(frozen=True)
class IntegrityReport:
    ok: bool
    entries: tuple[ModelManifestEntry, ...] = ()
    problems: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "entries": [e.to_dict() for e in self.entries],
            "problems": list(self.problems),
            "notes": list(self.notes),
        }


class ModelCache:
    """Manifest-backed weight store (no weights in the repository)."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root).expanduser() if root else default_cache_root()
        self.manifest_path = self.root / MANIFEST_NAME

    # -- manifest ------------------------------------------------------------
    def _load_document(self) -> dict:
        if not self.manifest_path.exists() or self.manifest_path.is_symlink():
            return {}
        try:
            raw = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        if (not isinstance(raw, dict) or type(raw.get("schema")) is not int
                or raw.get("schema") != 1 or not isinstance(raw.get("entries"), dict)):
            return {}
        return raw

    def load_manifest(self) -> dict[str, ModelManifestEntry]:
        raw_entries = self._load_document().get("entries", {})
        entries: dict[str, ModelManifestEntry] = {}
        for key, payload in raw_entries.items():
            if not isinstance(key, str):
                continue
            try:
                entry = ModelManifestEntry.from_dict(payload)
                if entry.key == key:
                    entries[key] = entry
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
        return entries

    def save_manifest(self, entries: dict[str, ModelManifestEntry], *,
                      artifacts: dict | None = None) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if self.manifest_path.is_symlink():
            raise ValueError("model cache manifest must not be a symlink")
        if artifacts is None:
            existing = self._load_document().get("artifacts", {})
            artifacts = existing if isinstance(existing, dict) else {}
        payload = {
            "schema": 1,
            "updated_at": utcnow().isoformat(),
            "entries": {key: entry.to_dict() for key, entry in sorted(entries.items())},
            "artifacts": artifacts,
        }
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=self.root,
                    prefix=".manifest-", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(json.dumps(payload, indent=2, sort_keys=True))
                handle.flush()
                os.fsync(handle.fileno())
            # os.replace replaces a link itself rather than following it, and
            # makes the manifest update atomic for readers after a crash.
            os.replace(temporary, self.manifest_path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    # -- recording -----------------------------------------------------------
    def record(
        self,
        model_id: str,
        revision: str,
        path: str | Path,
        *,
        downloaded_at: datetime | None = None,
    ) -> ModelManifestEntry:
        file_path = Path(path)
        if not file_path.is_file():
            raise FileNotFoundError(f"weight file not found as a regular file: {file_path}")
        stamp = downloaded_at or utcnow()
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError("downloaded_at must be timezone-aware")
        try:
            relative = file_path.resolve().relative_to(self.root.resolve())
            relative_path = str(relative)
        except ValueError:
            relative_path = str(file_path.resolve())
        entry = ModelManifestEntry(
            model_id=model_id,
            revision=revision,
            file_name=file_path.name,
            relative_path=relative_path,
            size_bytes=file_path.stat().st_size,
            sha256=sha256_file(file_path),
            downloaded_at=stamp,
        )
        entries = self.load_manifest()
        entries[entry.key] = entry
        self.save_manifest(entries)
        return entry

    def entry_for(self, model_id: str, revision: str = "") -> ModelManifestEntry | None:
        return self.load_manifest().get(f"{model_id}@{revision or 'default'}")

    @staticmethod
    def _artifact_key(model_id: str, revision: str) -> str:
        return f"{model_id}@{revision or 'default'}"

    def record_artifact(
        self, model_id: str, revision: str, directory: str | Path
    ) -> Path:
        """Record every regular file in one staged local artifact directory.

        The directory must already live inside the cache. Symlinks and special
        files are refused so the verified file set is exactly what the loader
        will see. This creates mutable local integrity metadata only; it does
        not establish publisher identity or signed release provenance.
        """
        if not isinstance(model_id, str) or not model_id.strip():
            raise ValueError("model_id must be a non-empty string")
        if not isinstance(revision, str):
            raise ValueError("revision must be a string")
        source = Path(directory)
        if source.is_symlink() or not source.is_dir():
            raise ValueError("artifact must be a regular directory, not a symlink")
        root = source.resolve()
        try:
            relative_root = root.relative_to(self.root.resolve()).as_posix()
        except ValueError as exc:
            raise ValueError("artifact directory must be inside the model cache") from exc
        if root == self.root.resolve():
            raise ValueError("artifact directory must not be the cache root itself")

        files: list[dict[str, object]] = []
        for current, directories, names in os.walk(root, followlinks=False):
            parent = Path(current)
            kept_directories = []
            for name in directories:
                item = parent / name
                if item.is_symlink():
                    raise ValueError(f"artifact contains a symlink directory: {item}")
                kept_directories.append(name)
            directories[:] = kept_directories
            for name in names:
                item = parent / name
                if item.is_symlink() or not item.is_file():
                    raise ValueError(f"artifact contains a non-regular file: {item}")
                files.append({
                    "relative_path": item.relative_to(root).as_posix(),
                    "file_name": item.name,
                    "size_bytes": item.stat().st_size,
                    "sha256": sha256_file(item),
                })
        files.sort(key=lambda item: str(item["relative_path"]))
        if not files:
            raise ValueError("artifact directory contains no regular files")
        document = self._load_document()
        artifacts = document.get("artifacts", {})
        artifacts = dict(artifacts) if isinstance(artifacts, dict) else {}
        key = self._artifact_key(model_id, revision)
        artifact_payload = {
            "model_id": model_id,
            "revision": revision,
            "relative_path": relative_root,
            "files": files,
            "recorded_at": utcnow().isoformat(),
        }
        existing = artifacts.get(key)
        if isinstance(existing, dict):
            stable_fields = ("model_id", "revision", "relative_path", "files")
            if any(existing.get(name) != artifact_payload[name] for name in stable_fields):
                raise ValueError(
                    "an immutable model revision is already bound to different "
                    "artifact bytes or a different directory")
        artifacts[key] = artifact_payload
        self.save_manifest(self.load_manifest(), artifacts=artifacts)
        return root

    def verify_artifact(
        self, model_id: str, revision: str = ""
    ) -> IntegrityReport:
        """Verify a complete staged tree, including absence of untracked files.

        The bundle manifest is mutable. A passing report means only that all
        regular files in this exact directory match local recorded byte counts
        and SHA-256 digests; it is not a signed publisher authenticity check.
        """
        document = self._load_document()
        artifacts = document.get("artifacts", {})
        key = self._artifact_key(model_id, revision)
        artifact = artifacts.get(key) if isinstance(artifacts, dict) else None
        if not isinstance(artifact, dict):
            single = self.entry_for(model_id, revision)
            detail = ("single-file integrity metadata is not a complete artifact bundle"
                      if single is not None else "artifact bundle is not tracked")
            return IntegrityReport(False, (), (), (detail,))
        if artifact.get("model_id") != model_id or artifact.get("revision") != revision:
            return IntegrityReport(
                False, (), ("artifact manifest identity does not match the request",))
        relative_root = artifact.get("relative_path")
        raw_files = artifact.get("files")
        if not isinstance(relative_root, str) or not relative_root:
            return IntegrityReport(False, (), ("artifact directory path is malformed",))
        try:
            artifact_root = self.resolve_within_root(relative_root)
        except (OSError, RuntimeError, ValueError) as exc:
            return IntegrityReport(False, (), (str(exc),))
        if not artifact_root.is_dir() or (self.root / relative_root).is_symlink():
            return IntegrityReport(
                False, (), ("recorded artifact directory is missing or not a regular directory",))
        if not isinstance(raw_files, list) or not raw_files:
            return IntegrityReport(False, (), ("artifact file list is malformed or empty",))

        expected: dict[str, dict] = {}
        problems: list[str] = []
        for item in raw_files:
            if not isinstance(item, dict):
                problems.append("artifact file entry is malformed")
                continue
            relative = item.get("relative_path")
            name = item.get("file_name")
            size = item.get("size_bytes")
            digest = item.get("sha256")
            if (not isinstance(relative, str) or not relative or "\\" in relative
                    or PurePosixPath(relative).is_absolute()
                    or PureWindowsPath(relative).is_absolute()
                    or any(part in {"", ".", ".."} for part in PurePosixPath(relative).parts)
                    or not isinstance(name, str) or not name
                    or PurePosixPath(relative).name != name
                    or type(size) is not int or size < 0
                    or not isinstance(digest, str) or len(digest) != 64
                    or any(char not in "0123456789abcdef" for char in digest.lower())):
                problems.append("artifact file entry fields are malformed")
                continue
            if relative in expected:
                problems.append(f"duplicate artifact path: {relative}")
                continue
            expected[relative] = item

        actual: dict[str, Path] = {}
        try:
            for current, directories, names in os.walk(artifact_root, followlinks=False):
                parent = Path(current)
                kept_directories = []
                for name in directories:
                    path = parent / name
                    if path.is_symlink():
                        problems.append(f"artifact contains a symlink directory: {name}")
                    else:
                        kept_directories.append(name)
                directories[:] = kept_directories
                for name in names:
                    path = parent / name
                    relative = path.relative_to(artifact_root).as_posix()
                    if path.is_symlink() or not path.is_file():
                        problems.append(f"artifact contains a non-regular file: {relative}")
                    else:
                        actual[relative] = path
        except (OSError, RuntimeError, ValueError) as exc:
            problems.append(f"artifact tree could not be enumerated ({type(exc).__name__})")

        for relative in sorted(set(expected) - set(actual)):
            problems.append(f"recorded artifact file is missing: {relative}")
        for relative in sorted(set(actual) - set(expected)):
            problems.append(f"untracked file exists in artifact directory: {relative}")

        entries: list[ModelManifestEntry] = []
        recorded_at = artifact.get("recorded_at", "")
        try:
            stamp = datetime.fromisoformat(recorded_at)
            if stamp.tzinfo is None or stamp.utcoffset() is None:
                raise ValueError("recorded_at must be timezone-aware")
        except (TypeError, ValueError, OverflowError):
            stamp = utcnow()
            problems.append("artifact manifest timestamp is malformed")
        for relative in sorted(set(expected) & set(actual)):
            item = expected[relative]
            path = actual[relative]
            try:
                size = path.stat().st_size
                digest = sha256_file(path)
            except (OSError, RuntimeError, ValueError) as exc:
                problems.append(f"artifact file could not be read ({type(exc).__name__}): {relative}")
                continue
            expected_size = item["size_bytes"]
            expected_digest = str(item["sha256"]).lower()
            if size != expected_size:
                problems.append(
                    f"size mismatch for {relative}: recorded {expected_size}, found {size}")
            if digest != expected_digest:
                problems.append(f"SHA-256 mismatch for artifact file {relative}")
            entries.append(ModelManifestEntry(
                model_id=model_id, revision=revision, file_name=path.name,
                relative_path=path.relative_to(self.root.resolve()).as_posix(),
                size_bytes=int(expected_size), sha256=expected_digest,
                downloaded_at=stamp,
            ))
        return IntegrityReport(
            ok=not problems,
            entries=tuple(entries),
            problems=tuple(problems),
            notes=("complete directory matches the mutable local manifest; "
                   "publisher authenticity is not established",),
        )

    def artifact_directory(self, model_id: str, revision: str = "") -> Path:
        report = self.verify_artifact(model_id, revision)
        if not report.ok:
            raise ValueError("artifact bundle was not verified: " +
                             "; ".join(report.problems or report.notes))
        artifact = self._load_document()["artifacts"][self._artifact_key(model_id, revision)]
        return self.resolve_within_root(artifact["relative_path"])

    def resolve_within_root(self, relative_path: str) -> Path:
        """Resolve a manifest path, refusing anything outside the cache root.

        ``Path(root) / absolute`` silently yields the absolute path, so an
        untrusted ``relative_path`` in ``manifest.json`` could otherwise point
        integrity verification at any readable file on disk — and the check
        would pass, because it compares the digest of the very file it was
        pointed at. The manifest is not a trusted input.
        """
        if not isinstance(relative_path, str) or not relative_path:
            raise ValueError("manifest path must be a non-empty string")
        candidate = Path(relative_path).expanduser()
        root = self.root.resolve()
        if not candidate.is_absolute():
            windows = PureWindowsPath(relative_path)
            if windows.is_absolute() or any(
                    part in {"", ".", ".."} for part in candidate.parts):
                raise ValueError(
                    f"manifest path {relative_path!r} is outside the model cache {root}")
            cursor = root
            for part in candidate.parts:
                cursor = cursor / part
                if cursor.is_symlink():
                    raise ValueError(
                        f"manifest path {relative_path!r} traverses a symlink "
                        "inside the model cache")
            resolved = cursor.resolve()
        else:
            resolved = candidate.resolve()
        if resolved == root or root not in resolved.parents:
            raise ValueError(
                f"manifest path {relative_path!r} resolves to {resolved}, outside "
                f"the model cache {root}"
            )
        return resolved

    def locate_weights(self, model_id: str) -> list[Path]:
        """Find weight files for a model inside the cache (HF layout or flat)."""
        if not self.root.exists():
            return []
        folder = "models--" + model_id.replace("/", "--")
        candidates: list[Path] = []
        scoped = self.root / folder
        if scoped.exists():
            candidates.extend(sorted(scoped.rglob("*.safetensors")))
            candidates.extend(sorted(scoped.rglob("*.bin")))
        candidates.extend(p for p in sorted(self.root.glob("*.safetensors")))
        return candidates

    # -- verification --------------------------------------------------------
    def verify(
        self, model_id: str, revision: str = "", *, expected_sha256: str | None = None
    ) -> IntegrityReport:
        """Check the recorded digest of the cached weights before loading.

        The local manifest is mutable and is not publisher provenance. This
        proves only that the tracked file still matches its recorded byte count
        and SHA-256; callers must not label it as a signed/authentic upstream
        artifact.
        """
        identity = f"{model_id}@{revision or 'default'}"
        try:
            entry = self.entry_for(model_id, revision)
        except Exception as exc:
            return IntegrityReport(
                False, (), (f"manifest could not be read ({type(exc).__name__})",))
        problems: list[str] = []
        notes: list[str] = []
        if entry is None:
            if expected_sha256:
                problems.append(
                    f"no cached weights for {identity} to verify against the "
                    "configured digest"
                )
            else:
                notes.append(
                    f"{identity} is not tracked in the AXIOM manifest; integrity "
                    "is unverified"
                )
            return IntegrityReport(False, (), tuple(problems), tuple(notes))

        if entry.model_id != model_id or entry.revision != revision:
            problems.append("manifest identity does not match the requested artifact")
        try:
            path = self.resolve_within_root(entry.relative_path)
        except (OSError, RuntimeError, ValueError) as exc:
            problems.append(str(exc))
            return IntegrityReport(False, (entry,), tuple(problems))
        try:
            if not path.is_file():
                problems.append(f"recorded weight file is missing or not regular: {path}")
                return IntegrityReport(False, (entry,), tuple(problems))
            if path.name != entry.file_name:
                problems.append("manifest file name does not match its resolved path")
                return IntegrityReport(False, (entry,), tuple(problems))
            actual_size = path.stat().st_size
            if actual_size != entry.size_bytes:
                problems.append(
                    f"size mismatch for {entry.file_name}: recorded "
                    f"{entry.size_bytes} bytes, found {actual_size}"
                )
            actual_sha = sha256_file(path)
        except (OSError, RuntimeError, ValueError) as exc:
            problems.append(f"recorded weight file could not be read ({type(exc).__name__})")
            return IntegrityReport(False, (entry,), tuple(problems))
        if actual_sha != entry.sha256:
            problems.append(
                f"SHA-256 mismatch for {entry.file_name}: recorded "
                f"{entry.sha256[:16]}…, found {actual_sha[:16]}…"
            )
        if expected_sha256 and actual_sha != expected_sha256.lower():
            problems.append(
                f"{entry.file_name} does not match the configured SHA-256 "
                f"{expected_sha256[:16]}…"
            )
        return IntegrityReport(
            ok=not problems,
            entries=(entry,),
            problems=tuple(problems),
            notes=tuple(notes),
        )
