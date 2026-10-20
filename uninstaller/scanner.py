"""Scanner: find candidate Arena/AXIOM artefacts safely.

Three entry points, all feeding one ``Inventory``:

* ``scan_registry``  — Add/Remove Programs registrations (strong evidence);
* ``scan_known_paths`` — well-known install/home/cache/shortcut locations;
* ``scan_drives``    — Deep Clean: every accessible local fixed drive.

Safety rules enforced here:

* reparse points (junctions, symlinks) are detected BEFORE descending and
  never followed — a junction must not turn into a walk through unrelated
  (or recursively linked) locations;
* every filesystem error becomes a reported ``Issue`` — permission denied,
  vanishing files, unreadable directories — never a crash and never silence;
* candidate screening is name-based and cheap; expensive evidence (PE
  metadata parse, marker reads) runs only on candidates;
* a directory is expanded into per-item review entries, so preserved data
  inside a matched tree (trading history, logs, .env, research) is visible
  and unselected instead of being swept along with the tree.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Callable, Iterable, Sequence

from uninstaller import classify, signals
from uninstaller.constants import (
    CATEGORY_AMBIGUOUS,
    PROTECTED_CATEGORIES,
    CATEGORY_EXTRACTED,
    CATEGORY_INSTALLATION,
    CATEGORY_LEFTOVER,
    CATEGORY_REGISTRATION,
    CATEGORY_RUNTIME_HOME,
    CATEGORY_SETUP,
    CATEGORY_TOOL,
    CONFIDENCE_CONFIRMED,
    CONFIDENCE_LIKELY,
    PRODUCT_EXECUTABLES,
    SETUP_EXECUTABLE_RE,
    SHORTCUT_NAMES,
    SIGNAL_REGISTRY,
    SIGNAL_SHORTCUT,
    SIGNAL_TREE,
    UNINSTALL_TOOL_RE,
    UNINSTALLER_LEFTOVERS,
)
from uninstaller.classify import Evidence, classify_file, classify_tree_root
from uninstaller.model import Inventory, Issue, Match
from uninstaller.winapi import KnownPaths, Platform, UninstallEntry

_SETUP_RE = re.compile(SETUP_EXECUTABLE_RE, re.IGNORECASE)
_TOOL_RE = re.compile(UNINSTALL_TOOL_RE, re.IGNORECASE)
_TOKEN_RE = re.compile(r"(?i)(?:^|[^a-z0-9])(axiom|arena)(?:[^a-z0-9]|$)")

#: File suffixes worth listing when they merely carry a product token.
_TOKEN_FILE_SUFFIXES = {".exe", ".msi", ".msix", ".zip", ".7z", ".rar",
                        ".iso", ".lnk", ".bat", ".cmd", ".ps1", ".db",
                        ".sqlite", ".json"}

#: Pure-OS directories a Deep Clean walk steps over (recorded, never candidates).
WALK_SKIP_DIRS = {
    "$recycle.bin", "system volume information", "windows",
    "winsxs", "installer", "$windows.~bt", "$windows.~ws",
    "windows.old", "microsoft", "packages", "drivers",
    "node_modules", "__pycache__", ".git", ".svn", ".hg",
}

MARKER_FILE_NAMES = {"paper-database-override.json", "build_meta.json",
                     "instance.lock", "instance.json"}


def safe_size(path: Path, platform: Platform) -> int:
    """Best-effort size without following reparse points. Never raises."""
    try:
        st = os.lstat(path)
    except OSError:
        return 0
    import stat as stat_mod
    if stat_mod.S_ISLNK(st.st_mode):
        return 0
    if not stat_mod.S_ISDIR(st.st_mode):
        return int(st.st_size)
    total = 0
    stack = [path]
    seen = 0
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    seen += 1
                    if seen > 500_000:  # pathological tree: report what we have
                        return total
                    if platform.is_reparse_point(Path(entry.path)):
                        continue
                    try:
                        if entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                        elif entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _issue(inventory: Inventory, path: object, exc: BaseException) -> None:
    text = str(exc)
    kind = "scan-error"
    if isinstance(exc, PermissionError) or "denied" in text.lower():
        kind = "access-denied"
        inventory.needs_elevation = True
    inventory.issues.append(Issue(path=str(path), kind=kind,
                                  detail=f"{type(exc).__name__}: {exc}"))


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def scan_registry(platform: Platform, inventory: Inventory,
                  known_locations: Sequence[Path]) -> list[UninstallEntry]:
    """Inventory uninstall registrations. Returns the confirmed entries."""
    if platform.registry is None:
        return []
    entries, issues = platform.registry.entries()
    confirmed: list[UninstallEntry] = []
    for path, detail in issues:
        inventory.issues.append(Issue(path=path, kind="scan-error",
                                      detail=detail))
    for entry in entries:
        ok, detail = signals.registry_entry_signal(
            entry.display_name, entry.publisher, app_id=entry.key_path)
        if not ok:
            continue
        confirmed.append(entry)
        location = entry.install_location.strip().rstrip("\\/")
        match = Match(
            path=f"registry:{entry.key_id}",
            category=CATEGORY_REGISTRATION,
            reasons=[f"registry: {detail}", f"registry: {entry.key_id}"],
            signals=[SIGNAL_REGISTRY], confidence=CONFIDENCE_CONFIRMED,
            size_bytes=entry.estimated_size_kb * 1024,
            kind="registry", selected=True,
            registry={
                "key_id": entry.key_id,
                "display_name": entry.display_name,
                "publisher": entry.publisher,
                "install_location": location,
                "uninstall_string": entry.uninstall_string,
            },
        )
        inventory.add(match)
    return confirmed


def _drop_nested(locations: Sequence[Path]) -> list[Path]:
    """Remove locations that sit inside another location already scanned."""
    unique: list[Path] = []
    for location in locations:
        if _norm(location) not in {_norm(p) for p in unique}:
            unique.append(location)
    kept: list[Path] = []
    for location in sorted(unique, key=lambda p: len(p.parts)):
        norm = _norm(location)
        if any(norm.startswith(_norm(other) + os.sep) for other in kept):
            continue
        kept.append(location)
    return kept


def _norm(path: Path | str) -> str:
    try:
        return os.path.normcase(os.path.normpath(str(path)))
    except (OSError, ValueError):  # pragma: no cover - defensive
        return str(path).lower()


# ---------------------------------------------------------------------------
# Known paths
# ---------------------------------------------------------------------------


def scan_known_paths(platform: Platform, known: KnownPaths,
                     inventory: Inventory, *,
                     registry_locations: Sequence[str] = (),
                     expand: bool = True) -> set[Path]:
    """Scan well-known product locations. Returns confirmed tree roots."""
    roots: set[Path] = set()
    locations = list(known.install_candidates())
    if known.models_cache():
        locations.append(known.models_cache())
    extra = [Path(p) for p in registry_locations if p]
    locations.extend(extra)
    locations = _drop_nested(locations)

    known_norms = {_norm(p) for p in locations}
    for location in locations:
        try:
            exists = location.exists()
        except OSError as exc:
            _issue(inventory, location, exc)
            continue
        if not exists:
            continue
        roots.add(location)
        _scan_tree_root(location, platform, inventory,
                        known_norms=known_norms, expand=expand,
                        known_install=True)

    # Start Menu group and desktop shortcuts created by our installer.
    shortcut_roots = []
    if known.start_menu_group():
        shortcut_roots.append(known.start_menu_group())
    for desktop in known.desktops():
        shortcut_roots.append(desktop)
    for base in shortcut_roots:
        try:
            if not base.is_dir():
                continue
            entries = list(base.iterdir())
        except OSError as exc:
            _issue(inventory, base, exc)
            continue
        for entry in entries:
            if entry.name not in SHORTCUT_NAMES and \
                    not (base.name == "AXIOM Arena" and entry.suffix.lower() == ".lnk"):
                continue
            ok, detail = signals.shortcut_signal(entry, SHORTCUT_NAMES)
            if not ok:
                continue
            try:
                size = entry.stat().st_size
            except OSError:
                size = 0
            inventory.add(Match(
                path=str(entry), category=CATEGORY_REGISTRATION,
                reasons=[f"shortcut: {detail}"],
                signals=[SIGNAL_SHORTCUT], confidence=CONFIDENCE_CONFIRMED,
                size_bytes=size, selected=True,
            ))
    return roots


# ---------------------------------------------------------------------------
# Tree roots and expansion
# ---------------------------------------------------------------------------


def _probe_tree_evidence(root: Path, platform: Platform,
                         known_norms: set[str]) -> Evidence:
    """Gather tree-level evidence: known path, markers, confirmed binaries."""
    ev = Evidence()
    if _norm(root) in known_norms or any(
            _norm(root).startswith(n + os.sep) for n in known_norms):
        ev.known_path = "path is a known Arena application location"
    ok, detail = signals.marker_signal(root)
    if ok:
        ev.marker = detail
    # Top-level (and one level down) confirmed binaries prove the tree.
    try:
        with os.scandir(root) as it:
            entries = list(it)
    except OSError:
        entries = []
    for entry in entries[:200]:
        try:
            if not entry.is_file(follow_symlinks=False):
                continue
        except OSError:
            continue
        path = Path(entry.path)
        meta_ok, meta_detail, _meta = signals.metadata_signal(path)
        if meta_ok:
            ev.metadata = meta_detail
            break
        if signals.name_signal(path)[0]:
            ev.filename = ev.filename or signals.name_signal(path)[1]
    if not ev.metadata:
        for sub in ("app", "data"):
            probe = root / sub
            try:
                if probe.is_dir():
                    for inner in list(probe.iterdir())[:50]:
                        if not inner.is_file():
                            continue
                        meta_ok, meta_detail, _meta = signals.metadata_signal(inner)
                        if meta_ok:
                            ev.metadata = meta_detail
                            break
                    if ev.metadata:
                        break
            except OSError:
                continue
    return ev


def _scan_tree_root(root: Path, platform: Platform, inventory: Inventory,
                    *, known_norms: set[str], expand: bool,
                    known_install: bool = False) -> None:
    """Classify one tree root and expand it into reviewable items."""
    evidence = _probe_tree_evidence(root, platform, known_norms)
    if not evidence.known_path and not evidence.metadata and not evidence.marker:
        # Known-install paths count as known-path evidence even empty.
        if known_install:
            evidence.known_path = "path is a known Arena application location"
        else:
            evidence.token = (f"directory name {root.name!r} matches a product "
                              "token but nothing inside proves ownership")
    category, confidence, reasons = classify_tree_root(
        root, known_install=known_install, evidence=evidence)
    size = safe_size(root, platform)
    root_match = Match(
        path=str(root), category=category, reasons=reasons,
        signals=evidence.signal_ids(), confidence=confidence,
        size_bytes=size, is_dir=True, kind="dir",
    )
    if category == CATEGORY_AMBIGUOUS or root_match.ambiguous:
        root_match.selected = False
        inventory.add(root_match)
        return
    # A root that hides preserved data is NEVER removable as a unit: the
    # operator has to choose the individual items listed beneath it. Without
    # this, selecting one folder could sweep a trading database away.
    #
    # The exception is a root whose OWN category is preserved (a source
    # checkout, a cache): it stays selectable so that the explicit
    # ``--include-…`` opt-in can actually remove it — but it is never
    # pre-selected, because protected categories must be enabled first.
    has_preserved = classify.peek_protected(root)
    root_match.selectable = (category in PROTECTED_CATEGORIES
                             or not has_preserved)
    root_match.selected = root_match.default_select() and root_match.selectable
    if category in PROTECTED_CATEGORIES:
        root_match.reasons.append(
            "tree: preserved category — removable only after its explicit "
            "--include-… option is enabled and the item is selected")
    elif not has_preserved:
        root_match.reasons.append(
            "tree: contains no preserved user data — whole folder is disposable")
    else:
        root_match.reasons.append(
            "tree: contains preserved data (trading history, logs, "
            "configuration, research, source or credentials) — it is not "
            "removable as a unit; the disposable items are listed separately")
    inventory.add(root_match)
    # Preserved trees (source checkouts, logs, research) are single units and
    # are not itemised; installs, extracted copies and the per-user home are.
    if expand and category in (CATEGORY_INSTALLATION, CATEGORY_EXTRACTED,
                               CATEGORY_RUNTIME_HOME):
        inventory.extend(expand_tree(root, platform))


def expand_tree(root: Path, platform: Platform) -> list[Match]:
    """Per-item matches for the CONTENTS of a confirmed Arena-owned tree.

    A child directory becomes ONE review item when it is disposable payload
    with nothing preserved hiding inside (``peek_protected``); otherwise it
    is expanded so every preserved unit stays visible on its own.
    """
    matches: list[Match] = []

    def walk(directory: Path, rel: tuple[str, ...]) -> None:
        try:
            entries = sorted(os.scandir(directory),
                             key=lambda e: e.name.lower())
        except OSError as exc:
            matches.append(Match(
                path=str(directory), category=CATEGORY_LEFTOVER,
                reasons=[f"unreadable directory: {type(exc).__name__}"],
                confidence=CONFIDENCE_LIKELY, size_bytes=0, is_dir=True,
                selected=False,
            ))
            return
        for entry in entries:
            path = Path(entry.path)
            rel_parts = rel + (entry.name,)
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                is_dir = False
            if is_dir and platform.is_reparse_point(path):
                matches.append(Match(
                    path=str(path), category=CATEGORY_LEFTOVER,
                    reasons=["reparse point inside the tree — listed but "
                             "never followed or removed by default"],
                    confidence=CONFIDENCE_LIKELY, size_bytes=0, is_dir=True,
                    selected=False,
                ))
                continue
            child_category = classify.classify_tree_child(
                rel_parts, is_dir, entry.name)
            if child_category is not None:
                match = Match(
                    path=str(path), category=child_category,
                    reasons=[f"tree content: {'/'.join(rel_parts)}"],
                    signals=[SIGNAL_TREE],
                    confidence=CONFIDENCE_CONFIRMED,
                    size_bytes=safe_size(path, platform),
                    is_dir=is_dir,
                )
                if not (match.protected or match.ambiguous):
                    match.selected = match.default_select()
                matches.append(match)
                continue
            # Payload. One review item if disposable and nothing preserved
            # inside; otherwise expand so preserved children are separate.
            if is_dir:
                size = safe_size(path, platform)
                if classify.peek_protected(path):
                    walk(path, rel_parts)
                    continue
                match = Match(
                    path=str(path), category=CATEGORY_LEFTOVER,
                    reasons=[f"tree content: {'/'.join(rel_parts)} (disposable)"],
                    signals=[SIGNAL_TREE], confidence=CONFIDENCE_CONFIRMED,
                    size_bytes=size, is_dir=True,
                )
                match.selected = match.default_select()
                matches.append(match)
            else:
                match = Match(
                    path=str(path), category=CATEGORY_LEFTOVER,
                    reasons=[f"tree content: {'/'.join(rel_parts)} (disposable)"],
                    signals=[SIGNAL_TREE], confidence=CONFIDENCE_CONFIRMED,
                    size_bytes=safe_size(path, platform), is_dir=False,
                )
                match.selected = match.default_select()
                matches.append(match)

    walk(root, ())
    return matches


# ---------------------------------------------------------------------------
# Drive walk
# ---------------------------------------------------------------------------


def _file_candidate(name: str) -> bool:
    lowered = name.lower()
    if lowered in {n.lower() for n in PRODUCT_EXECUTABLES}:
        return True
    if lowered in UNINSTALLER_LEFTOVERS:
        return True
    if lowered in MARKER_FILE_NAMES:
        return True
    if _SETUP_RE.match(name) or _TOOL_RE.match(name):
        return True
    stem, _, suffix = name.lower().rpartition(".")
    if suffix and ("." + suffix) in _TOKEN_FILE_SUFFIXES and _TOKEN_RE.search(stem):
        return True
    return False


def _tree_candidate_from_listing(name: str, listing: Iterable[str]) -> bool:
    if _TOKEN_RE.search(name):
        return True
    for child in listing:
        if _file_candidate(child) and (
                child.lower() in {n.lower() for n in PRODUCT_EXECUTABLES}
                or child.lower() == "arena.py"):
            return True
        if child.lower() == "build_meta.json":
            return True
    return False


def _inside_known(path: Path, known_norms: set[str]) -> bool:
    norm = _norm(path)
    return norm in known_norms or any(
        norm.startswith(known + os.sep) for known in known_norms)


def scan_drives(platform: Platform, inventory: Inventory,
                roots: Sequence[Path], *, progress: Callable[[str], None] | None = None,
                known_norms: set[str] | None = None) -> set[Path]:
    """Deep Clean walk over the given roots. Returns confirmed tree roots.

    A directory that PROVES Arena ownership (metadata, marker, registration)
    is classified and expanded on the spot and its subtree is not walked
    again: its contents are already represented as individual review items,
    and re-listing them would both duplicate the inventory and make a
    preserved tree look "unselected" from the outside.
    """
    known_norms = known_norms or set()
    tree_roots: set[Path] = set()
    for drive in roots:
        if progress:
            progress(str(drive))
        stack: list[Path] = [Path(drive)]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as it:
                    entries = list(it)
            except PermissionError as exc:
                _issue(inventory, current, exc)
                continue
            except OSError as exc:
                _issue(inventory, current, exc)
                continue

            names = [e.name for e in entries]
            if (_tree_candidate_from_listing(current.name, names)
                    and not _inside_known(current, known_norms)):
                evidence = _probe_tree_evidence(current, platform, known_norms)
                if evidence.metadata or evidence.marker or evidence.known_path:
                    tree_roots.add(current)
                    _scan_tree_root(current, platform, inventory,
                                    known_norms=known_norms, expand=True)
                    continue  # contents are represented; do not walk again
                _scan_tree_root(current, platform, inventory,
                                known_norms=known_norms, expand=False)

            for entry in entries:
                path = Path(entry.path)
                try:
                    if platform.is_reparse_point(path):
                        # Never follow; mention only when the name is interesting.
                        if _TOKEN_RE.search(entry.name):
                            inventory.add(Match(
                                path=str(path), category="ambiguous",
                                reasons=["reparse point whose name matches a "
                                         "product token — never followed"],
                                confidence=CONFIDENCE_LIKELY,
                                size_bytes=0, is_dir=entry.is_dir(follow_symlinks=False),
                                selected=False,
                            ))
                        continue
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError as exc:
                    _issue(inventory, path, exc)
                    continue
                if is_dir:
                    if entry.name.lower() in WALK_SKIP_DIRS:
                        continue
                    stack.append(path)
                else:
                    if _file_candidate(entry.name):
                        _evaluate_file_candidate(path, platform, inventory,
                                                 known_norms=known_norms)

    return tree_roots


def _evaluate_file_candidate(path: Path, platform: Platform,
                             inventory: Inventory, *,
                             known_norms: set[str]) -> None:
    ev = Evidence()
    try:
        parent_norm = _norm(path.parent)
    except (OSError, ValueError):  # pragma: no cover
        parent_norm = ""
    under_known = any(parent_norm == n or parent_norm.startswith(n + os.sep)
                      for n in known_norms)
    if under_known:
        ev.known_path = "file sits inside a known Arena application location"
    meta_ok, meta_detail, _meta = signals.metadata_signal(path)
    if meta_ok:
        ev.metadata = meta_detail
    tool_ok, tool_detail = signals.tool_metadata_signal(path)
    if tool_ok:
        ev.tool = tool_detail
    name_ok, name_detail = signals.name_signal(path)
    if name_ok:
        ev.filename = name_detail
    if path.name.lower() in MARKER_FILE_NAMES:
        ev.marker = signals.MARKER_FILES.get(path.name.lower(),
                                             f"marker file {path.name!r}")
    if path.suffix.lower() == ".lnk":
        ok, detail = signals.shortcut_signal(path, SHORTCUT_NAMES)
        if ok:
            ev.shortcut = detail
    if signals.token_in_name(path) and not name_ok:
        ev.token = (f"name contains the product token ({path.name!r}) "
                    "but no ownership evidence")
    is_self = platform.is_self_path(path)
    match = classify_file(path, ev, is_self=is_self)
    if not any(m.path == match.path for m in inventory.matches):
        inventory.add(match)


# ---------------------------------------------------------------------------
# Duplicate discovery
# ---------------------------------------------------------------------------


def hash_file(path: Path, limit: int = 512 * 1024 * 1024) -> str | None:
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size > limit:
        return None
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def mark_duplicates(inventory: Inventory, platform: Platform,
                    preferred_roots: Sequence[str] = ()) -> None:
    """Group byte-identical Arena executables/installers; keep one primary.

    Only files that are already classified as owned (installation, setup
    package, tool or tree content) participate — a look-alike can never
    become "the duplicate of an Arena file", because that would promote it
    to selected.
    """
    eligible_categories = {CATEGORY_INSTALLATION, CATEGORY_SETUP,
                           CATEGORY_TOOL, CATEGORY_LEFTOVER, "extracted-application"}
    candidates = [m for m in inventory.matches
                  if not m.is_dir and m.kind == "file"
                  and m.category in eligible_categories
                  and m.confidence == CONFIDENCE_CONFIRMED
                  and _hashable_name(Path(m.path))]
    by_size: dict[int, list[Match]] = {}
    for match in candidates:
        by_size.setdefault(match.size_bytes, []).append(match)
    preferred = [_norm(p) for p in preferred_roots]
    for size, group in by_size.items():
        if size <= 0 or len(group) < 2:
            continue
        hashes: dict[str, list[Match]] = {}
        for match in group:
            digest = hash_file(Path(match.path))
            if digest:
                hashes.setdefault(digest, []).append(match)
        for digest, items in hashes.items():
            if len(items) < 2:
                continue
            items.sort(key=lambda m: (
                0 if any(_norm(m.path).startswith(p) for p in preferred) else 1,
                len(Path(m.path).parts), m.path))
            primary = items[0]
            primary.reasons.append(
                f"duplicate group: primary copy (sha256 {digest[:12]}…, "
                f"{len(items)} copies found)")
            for other in items[1:]:
                other.category = "duplicate-executable"
                other.confidence = CONFIDENCE_LIKELY
                other.duplicate_of = primary.path
                other.signals.append("byte-identical-duplicate")
                other.reasons.append(
                    f"duplicate: byte-identical to {primary.path} "
                    f"(sha256 {digest[:12]}…)")
                if not other.protected:
                    other.selected = other.default_select()


def _hashable_name(path: Path) -> bool:
    lowered = path.name.lower()
    return (lowered.endswith(".exe") or lowered.endswith(".msi")
            or signals.is_setup_package(path.name)
            or signals.is_uninstall_tool(path.name)
            or signals.is_product_executable(path.name))
