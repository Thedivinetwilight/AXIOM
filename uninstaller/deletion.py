"""Execution: turn a reviewed, confirmed selection into actions.

Order of operations (all of it visible in the report):

1. registry registrations the operator selected — removed explicitly;
2. files, deepest first — quarantined (preferred) or permanently deleted
   when ``--permanent`` was requested;
3. directory frames, deepest first — removed only when empty after the
   items under them were processed; a directory whose preserved contents
   were NOT selected is kept and reported as such;
4. the tool's own running files — renamed and scheduled for the next reboot
   instead of being attacked while running.

Failures are never retried destructively: a locked or inaccessible item is
reported and left alone.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from uninstaller.model import ActionReport, ActionResult, Inventory, Issue, Match
from uninstaller.protect import ProtectionPolicy
from uninstaller.quarantine import Quarantine
from uninstaller.winapi import Platform


@dataclass
class ExecutionOptions:
    permanent: bool = False
    dry_run: bool = False


def execute(inventory: Inventory, policy: ProtectionPolicy,
            platform: Platform, quarantine: Quarantine | None,
            options: ExecutionOptions) -> ActionReport:
    report = ActionReport(mode=inventory.mode)
    policy.enforce_selection(inventory)
    selected = [m for m in inventory.selected() if m.selectable]

    # 1. Registry registrations.
    for match in selected:
        if match.kind == "registry":
            report.results.append(_remove_registration(match, platform, options))

    # 2. Files first (deepest paths first), then directories.
    files = sorted((m for m in selected if not m.is_dir and m.kind != "registry"),
                   key=lambda m: (-len(Path(m.path).parts), m.path))
    dirs = sorted((m for m in selected if m.is_dir),
                  key=lambda m: (-len(Path(m.path).parts), m.path))

    for match in files:
        report.results.append(_handle_file(match, platform, quarantine, options))

    for match in dirs:
        report.results.append(_handle_dir(match, inventory, platform,
                                          quarantine, options))

    report.issues = list(inventory.issues)
    if quarantine is not None and quarantine.entries and not options.dry_run:
        try:
            manifest = quarantine.finalize()
            report.quarantine_dir = str(quarantine.directory)
            report.issues.append(Issue(
                path=str(manifest), kind="info",
                detail=f"quarantine manifest written ({len(quarantine.entries)} items)"))
        except OSError as exc:
            report.issues.append(Issue(
                path=str(quarantine.directory), kind="delete-failed",
                detail=f"could not write quarantine manifest: {exc}"))
    return report


def _has_unselected_descendant(match: Match, inventory: Inventory) -> bool:
    prefix = match.path.rstrip("\\/") + os.sep
    alt_prefix = match.path.rstrip("\\/") + "/"
    for other in inventory.matches:
        if other.path == match.path or other.kind == "registry":
            continue
        if other.path.startswith(prefix) or other.path.startswith(alt_prefix):
            if not other.selected or not other.selectable:
                return True
    return False


def _remove_registration(match: Match, platform: Platform,
                         options: ExecutionOptions) -> ActionResult:
    if options.dry_run:
        return ActionResult(match.path, match.category, "skipped",
                            "dry run: registration would be removed")
    if match.registry is None:
        return ActionResult(match.path, match.category, "failed",
                            "registration payload missing")
    try:
        ok, detail = platform.registry.remove_registration(match.registry)
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        return ActionResult(match.path, match.category, "failed",
                            f"{type(exc).__name__}: {exc}")
    return ActionResult(match.path, match.category,
                        "registry-removed" if ok else "failed", detail)


def _handle_file(match: Match, platform: Platform,
                 quarantine: Quarantine | None,
                 options: ExecutionOptions) -> ActionResult:
    path = Path(match.path)
    if options.dry_run:
        return ActionResult(match.path, match.category, "skipped",
                            "dry run: no changes made")
    if not _exists(path):
        return ActionResult(match.path, match.category, "skipped",
                            "already gone")

    if platform.is_self_path(path):
        ok, detail = platform.defer_delete(path)
        return ActionResult(match.path, match.category,
                            "deferred" if ok else "failed",
                            detail or "own running file")

    if not options.permanent and quarantine is not None:
        result = quarantine.move(match)
        if result.ok:
            return ActionResult(match.path, match.category, "quarantined",
                                result.detail,
                                quarantine_path=result.quarantine_path)
        return ActionResult(match.path, match.category, "failed", result.detail)

    # Explicit permanent deletion.
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    except OSError as exc:
        kind = "locked" if _is_locked(exc) else "delete-failed"
        return ActionResult(match.path, match.category, "failed",
                            f"{kind}: {type(exc).__name__}: {exc}")
    return ActionResult(match.path, match.category, "deleted",
                        "permanently deleted")


def _handle_dir(match: Match, inventory: Inventory, platform: Platform,
                quarantine: Quarantine | None,
                options: ExecutionOptions) -> ActionResult:
    path = Path(match.path)
    if options.dry_run:
        return ActionResult(match.path, match.category, "skipped",
                            "dry run: no changes made")
    if not _exists(path):
        return ActionResult(match.path, match.category, "skipped",
                            "already gone")
    if platform.is_self_path(path):
        ok, detail = platform.defer_delete(path)
        return ActionResult(match.path, match.category,
                            "deferred" if ok else "failed", detail)

    if _has_unselected_descendant(match, inventory):
        if _dir_is_empty(path):
            return _remove_empty_frame(path, match)
        return ActionResult(match.path, match.category, "skipped",
                            "kept: contains preserved or unselected items")

    # Everything under this directory was selected (or nothing was listed
    # beneath it and it is a disposable unit).
    if options.permanent or quarantine is None:
        return _handle_file(match, platform, None,
                            ExecutionOptions(permanent=True, dry_run=False))
    result = quarantine.move(match)
    if result.ok:
        return ActionResult(match.path, match.category, "quarantined",
                            result.detail, quarantine_path=result.quarantine_path)
    return ActionResult(match.path, match.category, "failed", result.detail)


def _remove_empty_frame(path: Path, match: Match) -> ActionResult:
    try:
        path.rmdir()
    except OSError as exc:
        return ActionResult(match.path, match.category, "failed",
                            f"empty-frame removal failed: {type(exc).__name__}: {exc}")
    return ActionResult(match.path, match.category, "deleted",
                        "empty directory frame removed")


def _dir_is_empty(path: Path) -> bool:
    try:
        with os.scandir(path) as it:
            for _ in it:
                return False
    except OSError:
        return False
    return True


def _exists(path: Path) -> bool:
    try:
        return path.exists() or path.is_symlink()
    except OSError:
        return False


def _is_locked(exc: OSError) -> bool:
    text = str(exc).lower()
    return "being used" in text or "locked" in text or "denied" in text
