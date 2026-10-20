"""Data model for scan results, inventory and action reporting.

Everything the uninstaller does is expressed as ``Match`` records: the review
step shows them, the confirmation step commits to them and the action step
executes exactly the ones the operator left selected. There is no hidden
deletion path that bypasses a ``Match``.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

from uninstaller.constants import (
    ALWAYS_DESELECTED,
    CONFIDENCE_AMBIGUOUS,
    PROTECTED_CATEGORIES,
)

PLAN_FORMAT = "arena-uninstall-plan/1"
REPORT_FORMAT = "arena-uninstall-report/1"


@dataclass
class Match:
    """One candidate item (file, directory or registry registration)."""

    path: str
    category: str
    reasons: list[str] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)
    confidence: str = "confirmed"
    size_bytes: int = 0
    is_dir: bool = False
    kind: str = "file"  # "file" | "dir" | "registry"
    selected: bool = False
    #: False for items the tool refuses to touch at all (its own running image
    #: is handled through the deferred-delete path instead).
    selectable: bool = True
    #: Extra data for registry matches.
    registry: dict[str, Any] | None = None
    #: For duplicates: the primary path this one duplicates.
    duplicate_of: str | None = None

    @property
    def protected(self) -> bool:
        return self.category in PROTECTED_CATEGORIES

    @property
    def ambiguous(self) -> bool:
        return self.category in ALWAYS_DESELECTED or self.confidence == CONFIDENCE_AMBIGUOUS

    def default_select(self) -> bool:
        """Whether the item starts selected.

        Protected categories and ambiguous matches NEVER start selected; the
        former need an explicit category opt-in, the latter an explicit
        per-item choice.
        """
        if self.protected or self.ambiguous or not self.selectable:
            return False
        return True

    def to_json(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Match":
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Issue:
    """A scan or action problem that must be reported, never silenced."""

    path: str
    kind: str  # "access-denied" | "scan-error" | "locked" | "delete-failed" | ...
    detail: str

    def to_json(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Issue":
        return cls(path=str(data.get("path", "")),
                   kind=str(data.get("kind", "error")),
                   detail=str(data.get("detail", "")))


@dataclass
class Inventory:
    """The complete reviewable result of a scan."""

    mode: str
    matches: list[Match] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    needs_elevation: bool = False
    scanned_roots: list[str] = field(default_factory=list)
    tool_version: str = ""

    def add(self, match: Match) -> None:
        self.matches.append(match)

    def extend(self, matches: Iterable[Match]) -> None:
        self.matches.extend(matches)

    def selected(self) -> list[Match]:
        return [m for m in self.matches if m.selected and m.selectable]

    def by_category(self) -> dict[str, list[Match]]:
        groups: dict[str, list[Match]] = {}
        for match in self.matches:
            groups.setdefault(match.category, []).append(match)
        return groups

    def total_selected_bytes(self) -> int:
        return sum(m.size_bytes for m in self.selected())

    def __iter__(self) -> Iterator[Match]:
        return iter(self.matches)

    def __len__(self) -> int:
        return len(self.matches)

    def to_plan(self) -> dict[str, Any]:
        return {
            "format": PLAN_FORMAT,
            "mode": self.mode,
            "tool_version": self.tool_version,
            "scanned_roots": list(self.scanned_roots),
            "needs_elevation": self.needs_elevation,
            "matches": [m.to_json() for m in self.matches],
            "issues": [i.to_json() for i in self.issues],
        }

    @classmethod
    def from_plan(cls, data: dict[str, Any]) -> "Inventory":
        if data.get("format") != PLAN_FORMAT:
            raise ValueError(f"not an Arena uninstall plan: {data.get('format')!r}")
        inv = cls(mode=str(data.get("mode", "deep")),
                  tool_version=str(data.get("tool_version", "")))
        inv.matches = [Match.from_json(m) for m in data.get("matches", [])]
        inv.issues = [Issue.from_json(i) for i in data.get("issues", [])]
        inv.needs_elevation = bool(data.get("needs_elevation", False))
        inv.scanned_roots = [str(r) for r in data.get("scanned_roots", [])]
        return inv

    def to_json_text(self) -> str:
        return json.dumps(self.to_plan(), indent=2, ensure_ascii=False)


@dataclass
class ActionResult:
    """Outcome of executing one selected ``Match``."""

    path: str
    category: str
    action: str  # "quarantined" | "deleted" | "deferred" | "failed" | "skipped" | "registry-removed"
    detail: str = ""
    quarantine_path: str = ""

    def to_json(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class ActionReport:
    mode: str
    results: list[ActionResult] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    log_path: str = ""
    quarantine_dir: str = ""
    cancelled: bool = False

    @property
    def failures(self) -> list[ActionResult]:
        return [r for r in self.results if r.action == "failed"]

    @property
    def permanent_deletions(self) -> list[ActionResult]:
        return [r for r in self.results if r.action == "deleted"]

    @property
    def ok(self) -> bool:
        return not self.failures and not self.cancelled

    def to_json(self) -> dict[str, Any]:
        return {
            "format": REPORT_FORMAT,
            "mode": self.mode,
            "cancelled": self.cancelled,
            "log_path": self.log_path,
            "quarantine_dir": self.quarantine_dir,
            "results": [r.to_json() for r in self.results],
            "issues": [i.to_json() for i in self.issues],
        }

    def to_json_text(self) -> str:
        return json.dumps(self.to_json(), indent=2, ensure_ascii=False)


def human_size(num: float) -> str:
    """Readable size for the review table (decimal-free, stable in tests)."""
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < step or unit == "TB":
            if unit == "B":
                return f"{int(num)} B"
            return f"{num:.1f} {unit}"
        num /= step
    return f"{num:.1f} TB"


def resolve_display_path(path: str | Path) -> str:
    """Display paths without resolving symlinks (which would leave the tree).

    ``Path.resolve()`` follows reparse points — exactly what this tool must not
    do when describing a target. Absolute-ify without touching the filesystem
    link structure instead.
    """
    p = Path(path)
    try:
        if not p.is_absolute():
            p = Path.cwd() / p
    except OSError:  # pragma: no cover - defensive
        pass
    return str(p)
