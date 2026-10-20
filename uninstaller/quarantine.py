"""Reversible quarantine for user-selected leftovers.

Deleting a wrong file is the worst outcome an uninstaller can produce, so the
default destructive action is not deletion at all: it is a MOVE into a
timestamped quarantine directory plus a manifest that remembers exactly where
each item came from. ``restore`` puts everything back.

Quarantine layout::

    <tool-home>/quarantine/2026-10-20T12-00-00Z/
        manifest.json
        files/<drive-and-path-mangled>/<original-name>...

The manifest is written with UTF-8 explicitly and contains no file contents —
paths, sizes, categories, reasons and a SHA-256 of each moved file.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from uninstaller.model import Match
from uninstaller.redact import redact

MANIFEST_NAME = "manifest.json"
MANIFEST_FORMAT = "arena-uninstall-quarantine/1"


@dataclass
class QuarantineResult:
    ok: bool
    detail: str
    quarantine_path: str = ""


@dataclass
class Quarantine:
    root: Path
    stamp: str = ""
    entries: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.stamp:
            self.stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")

    @property
    def directory(self) -> Path:
        return self.root / self.stamp

    @property
    def files_dir(self) -> Path:
        return self.directory / "files"

    def _target_for(self, path: Path) -> Path:
        # Keep the shape of the original path so a restore is unambiguous.
        anchor = path.anchor.replace(":", "").replace("\\", "").replace("/", "") or "root"
        parts = [anchor] + list(path.parts[1:]) if path.anchor else list(path.parts)
        safe = [p.replace(":", "_") for p in parts]
        return self.files_dir.joinpath(*safe)

    def move(self, match: Match) -> QuarantineResult:
        """Move one selected item into the quarantine. Never raises."""
        source = Path(match.path)
        if not source.exists():
            return QuarantineResult(True, "already gone", "")
        target = self._target_for(source)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                target = target.with_name(target.name + f".dup{len(self.entries)}")
            shutil.move(str(source), str(target))
        except OSError as exc:
            return QuarantineResult(
                False, f"quarantine move failed: {type(exc).__name__}: {exc}", "")
        digest = ""
        try:
            if target.is_file():
                digest = _sha256(target)
        except OSError:
            pass
        self.entries.append({
            "original_path": redact(str(source)),
            "quarantine_path": str(target),
            "category": match.category,
            "reasons": [redact(r) for r in match.reasons],
            "size_bytes": match.size_bytes,
            "is_dir": match.is_dir,
            "sha256": digest,
        })
        return QuarantineResult(True, "moved to quarantine", str(target))

    def finalize(self) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        manifest = {
            "format": MANIFEST_FORMAT,
            "created": self.stamp,
            "entries": self.entries,
        }
        path = self.directory / MANIFEST_NAME
        path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False),
                        encoding="utf-8")
        return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def restore(quarantine_dir: Path) -> list[tuple[str, str, str]]:
    """Restore a quarantine. Returns (original, status, detail) rows.

    Restoring never overwrites: if the original path is occupied again, the
    item stays in the quarantine and the row reports the conflict.
    """
    manifest_path = quarantine_dir / MANIFEST_NAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [("", "failed", f"cannot read manifest: {exc}")]
    if manifest.get("format") != MANIFEST_FORMAT:
        return [("", "failed", f"unknown manifest format: {manifest.get('format')!r}")]
    rows: list[tuple[str, str, str]] = []
    for entry in manifest.get("entries", []):
        original = Path(entry["original_path"])
        stored = Path(entry["quarantine_path"])
        if not stored.exists():
            rows.append((str(original), "failed", "quarantined copy is missing"))
            continue
        if original.exists():
            rows.append((str(original), "kept", "original path is occupied; "
                        "item left in quarantine"))
            continue
        try:
            original.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(stored), str(original))
        except OSError as exc:
            rows.append((str(original), "failed",
                         f"{type(exc).__name__}: {exc}"))
            continue
        rows.append((str(original), "restored", ""))
    return rows
