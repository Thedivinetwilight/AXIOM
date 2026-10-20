"""Engine: scan → review → confirm → execute, in one auditable flow.

The engine owns the ordering guarantees the requirements depend on:

* a complete inventory is produced before anything destructive happens;
* elevation is offered only after a scan proves it is needed, and only for
  reading the places that were refused (least privilege);
* the protection policy is applied both when selection is made and again
  immediately before execution, so a stale plan cannot bypass it;
* the quarantine is preferred over deletion for everything the operator
  selected, and the log is written no matter how the run ends.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from uninstaller import scanner
from uninstaller.constants import (
    DEFAULT_TOOL_DIR_NAME,
    LOG_DIR_NAME,
    QUARANTINE_DIR_NAME,
)
from uninstaller.deletion import ExecutionOptions, execute
from uninstaller.logbook import Logbook
from uninstaller.model import ActionReport, Inventory, Issue
from uninstaller.protect import ProtectionPolicy
from uninstaller.quarantine import Quarantine
from uninstaller.scanner import mark_duplicates, scan_drives, scan_known_paths, scan_registry
from uninstaller.winapi import KnownPaths, Platform, env_get


def default_tool_home(platform: Platform) -> Path:
    """Per-user home for this tool's logs and quarantine.

    Deliberately OUTSIDE any Arena data directory: a cleanup log must not
    live in the tree it is cleaning, and quarantine must survive whatever
    the operator selects.
    """
    env = platform.environ
    explicit = env_get(env, "ARENA_UNINSTALL_HOME").strip()
    if explicit:
        return Path(explicit).expanduser()
    if os.name == "nt":
        base = env_get(env, "LOCALAPPDATA") or env_get(env, "APPDATA")
        if base:
            return Path(base) / DEFAULT_TOOL_DIR_NAME
        return Path.home() / "AppData" / "Local" / DEFAULT_TOOL_DIR_NAME
    xdg = env_get(env, "XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / DEFAULT_TOOL_DIR_NAME
    return Path.home() / ".local" / "share" / DEFAULT_TOOL_DIR_NAME


@dataclass
class ScanRequest:
    mode: str = "standard"           # "standard" | "deep"
    extra_roots: Sequence[Path] = field(default_factory=tuple)
    include_drives: bool = True
    progress: Callable[[str], None] | None = None


def build_inventory(platform: Platform, known: KnownPaths,
                    request: ScanRequest, tool_version: str) -> Inventory:
    inventory = Inventory(mode=request.mode, tool_version=tool_version)
    known_norms = {scanner._norm(p) for p in known.install_candidates()}
    if known.models_cache():
        known_norms.add(scanner._norm(known.models_cache()))

    # 1. Registrations first: they are the strongest evidence and tell us
    #    where to look for the installation tree.
    entries = scan_registry(platform, inventory, known.install_candidates())
    registry_locations = [e.install_location for e in entries if e.install_location]

    # 2. Well-known locations.
    tree_roots = scan_known_paths(platform, known, inventory,
                                  registry_locations=registry_locations,
                                  expand=True)
    for root in tree_roots:
        known_norms.add(scanner._norm(root))

    # 3. Deep Clean: every accessible local fixed drive.
    if request.mode == "deep":
        if request.extra_roots:
            # Explicit roots replace the drive list: a targeted scan must not
            # silently become a whole-machine scan.
            roots = list(request.extra_roots)
        elif request.include_drives:
            roots = [Path(d) for d in platform.fixed_drives()]
        else:
            roots = []
        if not roots:
            inventory.issues.append(Issue(
                path="", kind="scan-error",
                detail="no accessible fixed drives were found"))
        inventory.scanned_roots = [str(r) for r in roots]
        scan_drives(platform, inventory, roots,
                    progress=request.progress, known_norms=known_norms)
    else:
        inventory.scanned_roots = [str(r) for r in tree_roots]

    # 4. One record per path before duplicate grouping, so the walk and the
    #    known-path pass cannot make a file look like a copy of itself.
    dedupe(inventory)

    # 5. Duplicates among confirmed artefacts (exes / installers / tools).
    mark_duplicates(inventory, platform, preferred_roots=list(
        registry_locations) + [str(p) for p in tree_roots])

    # 6. Never let the tool's own running files become deletable entries.
    for match in inventory.matches:
        if platform.is_self_path(match.path):
            match.selected = False
            match.selectable = False
            match.reasons.append(
                "this is the running uninstaller — handled with deferred "
                "deletion if selected, never removed while running")

    dedupe(inventory)
    return inventory


def dedupe(inventory: Inventory) -> None:
    """Collapse duplicate matches for the same path (keep the strongest)."""
    best: dict[str, object] = {}
    ordered: list = []
    for match in inventory.matches:
        key = os.path.normcase(os.path.normpath(match.path))
        existing = best.get(key)
        if existing is None:
            best[key] = match
            ordered.append(match)
            continue
        # Prefer a confirmed record with more reasons.
        if len(match.reasons) > len(existing.reasons):
            match.selected = existing.selected or match.selected
            index = ordered.index(existing)
            ordered[index] = match
            best[key] = match
    inventory.matches = ordered


@dataclass
class RunOptions:
    permanent: bool = False
    dry_run: bool = False
    quarantine_root: Path | None = None
    log_path: Path | None = None


def apply_inventory(inventory: Inventory, policy: ProtectionPolicy,
                    platform: Platform, tool_home: Path,
                    options: RunOptions) -> tuple[ActionReport, Logbook]:
    """Execute the selection and write the cleanup log."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    log_path = options.log_path or (tool_home / LOG_DIR_NAME /
                                    f"arena-uninstall-{stamp}.log")
    log = Logbook(path=log_path)
    log.timestamped("Arena Deep Uninstaller session started")
    log.write(f"mode: {inventory.mode}")
    log.write(f"administrator: {platform.is_admin()}")
    log.write(f"policy: {policy.describe()}")
    log.log_inventory(inventory)

    quarantine: Quarantine | None = None
    if not options.permanent:
        root = options.quarantine_root or (tool_home / QUARANTINE_DIR_NAME)
        quarantine = Quarantine(root=root)

    report = execute(inventory, policy, platform, quarantine,
                     ExecutionOptions(permanent=options.permanent,
                                      dry_run=options.dry_run))
    log.section("SUMMARY")
    log.write(f"results: {len(report.results)}")
    log.write(f"quarantined: "
              f"{sum(1 for r in report.results if r.action == 'quarantined')}")
    log.write(f"permanently deleted: {len(report.permanent_deletions)}")
    log.write(f"failed: {len(report.failures)}")
    log.write(f"quarantine directory: {report.quarantine_dir or '—'}")
    log.log_report(report)
    try:
        report.log_path = str(log.save())
    except OSError as exc:
        report.issues.append(Issue(path=str(log_path), kind="log-failed",
                                   detail=str(exc)))
    return report, log


