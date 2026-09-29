"""Runtime model cache and integrity manifest for Kronos weights.

Weights never live in Git. They are downloaded once into a per-user runtime
cache and tracked by a manifest that records model id, revision, file size,
SHA-256 and the download timestamp. Loading verifies the digest first, so a
corrupted or swapped file cannot be silently loaded.

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
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from axiom.models import utcnow

MANIFEST_NAME = "manifest.json"
CHUNK = 1 << 20


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
        return Path(override).expanduser()
    if os_name == "nt":
        base = Path(localappdata) if localappdata else home / "AppData" / "Local"
        return base / "AXIOM" / "models" / "kronos"
    if platform == "darwin":
        return home / "Library" / "Caches" / "AXIOM" / "models" / "kronos"
    base = Path(xdg_cache_home).expanduser() if xdg_cache_home else home / ".cache"
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
        stamp = payload["downloaded_at"]
        return cls(
            model_id=payload["model_id"],
            revision=payload.get("revision", ""),
            file_name=payload["file_name"],
            relative_path=payload["relative_path"],
            size_bytes=int(payload["size_bytes"]),
            sha256=payload["sha256"],
            downloaded_at=(
                stamp if isinstance(stamp, datetime) else datetime.fromisoformat(stamp)
            ),
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
    def load_manifest(self) -> dict[str, ModelManifestEntry]:
        if not self.manifest_path.exists():
            return {}
        try:
            raw = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        entries = {}
        for key, payload in (raw.get("entries") or {}).items():
            try:
                entries[key] = ModelManifestEntry.from_dict(payload)
            except (KeyError, TypeError, ValueError):
                continue
        return entries

    def save_manifest(self, entries: dict[str, ModelManifestEntry]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": 1,
            "updated_at": utcnow().isoformat(),
            "entries": {key: entry.to_dict() for key, entry in sorted(entries.items())},
        }
        self.manifest_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
        )

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
        if not file_path.exists():
            raise FileNotFoundError(f"weight file not found: {file_path}")
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
            downloaded_at=downloaded_at or utcnow(),
        )
        entries = self.load_manifest()
        entries[entry.key] = entry
        self.save_manifest(entries)
        return entry

    def entry_for(self, model_id: str, revision: str = "") -> ModelManifestEntry | None:
        return self.load_manifest().get(f"{model_id}@{revision or 'default'}")

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
        """Check the recorded digest of the cached weights before loading."""
        entry = self.entry_for(model_id, revision)
        problems: list[str] = []
        notes: list[str] = []
        if entry is None:
            if expected_sha256:
                problems.append(
                    f"no cached weights for {model_id}@{revision or 'default'} "
                    "to verify against the configured digest"
                )
            else:
                notes.append(
                    f"{model_id}@{revision or 'default'} is not tracked in the "
                    "AXIOM manifest; integrity will be recorded after download"
                )
            return IntegrityReport(False, (), tuple(problems), tuple(notes))

        path = self.root / entry.relative_path
        if not path.exists():
            problems.append(f"recorded weight file is missing: {path}")
            return IntegrityReport(False, (entry,), tuple(problems))
        actual_size = path.stat().st_size
        if actual_size != entry.size_bytes:
            problems.append(
                f"size mismatch for {entry.file_name}: recorded "
                f"{entry.size_bytes} bytes, found {actual_size}"
            )
        actual_sha = sha256_file(path)
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
