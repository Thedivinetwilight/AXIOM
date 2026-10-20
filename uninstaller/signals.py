"""Evidence gathering for ownership classification.

Each function here answers exactly one question about a path and returns a
``(fires: bool, detail: str)`` pair — never a verdict. The verdict is made in
``classify.py`` by combining independent signals, because the product tokens
"Arena" and "AXIOM" appear in far more places than the product owns.

Signal strengths:

strong  — an uninstall registration, or a PE version resource naming the
          product AND the publisher;
medium  — membership of a well-known product location, or an application
          marker file that only this product writes;
weak    — a file name that merely looks like ours. NEVER sufficient alone.
"""

from __future__ import annotations

import re
from pathlib import Path

from uninstaller import pe
from uninstaller.constants import (
    INSTALLER_APP_ID,
    PRODUCT_EXECUTABLES,
    PRODUCT_NAME_MARKERS,
    PUBLISHER,
    SETUP_EXECUTABLE_RE,
    TOOL_PRODUCT_NAME,
    UNINSTALLER_LEFTOVERS,
    UNINSTALL_TOOL_RE,
)
from uninstaller.redact import looks_secret

_SETUP_RE = re.compile(SETUP_EXECUTABLE_RE, re.IGNORECASE)
_TOOL_RE = re.compile(UNINSTALL_TOOL_RE, re.IGNORECASE)
_TOKEN_RE = re.compile(r"(?i)(?:^|[^a-z0-9])(axiom|arena)(?:[^a-z0-9]|$)")

# Marker file names only this product writes into its own trees.
# ``arena.py`` on its own is NOT here: any project may contain a file with
# that name. It only counts as part of the source-checkout marker set
# (arena.py + app/package + .git|pyproject.toml|tests), which is checked
# separately in classify_tree_root.
MARKER_FILES = {
    "paper-database-override.json": "paper-database-override.json (Arena home marker)",
    "build_meta.json": "app/BUILD_META.json (packaged Arena marker)",
    "instance.lock": "instance.lock (Arena instance guard)",
}

# Marker path fragments -> description. Checked relative to a candidate root.
MARKER_PATHS = (
    (("app", "__init__.py"), "app/__init__.py (Arena application package)"),
    (("data", "history"), "data/history (Arena candle store)"),
    (("data", "cache"), "data/cache (Arena cache tree)"),
    (("axiom", "__init__.py"), "axiom/ (AXIOM package)"),
)


def is_setup_package(name: str) -> bool:
    return bool(_SETUP_RE.match(name))


def is_uninstall_tool(name: str) -> bool:
    return bool(_TOOL_RE.match(name))


def is_product_executable(name: str) -> bool:
    return name.lower() in {n.lower() for n in PRODUCT_EXECUTABLES}


def is_uninstaller_leftover(name: str) -> bool:
    return name.lower() in UNINSTALLER_LEFTOVERS


def name_signal(path: Path) -> tuple[bool, str]:
    """Weak signal: the file NAME matches a product-shipped name pattern."""
    name = path.name
    lowered = name.lower()
    if lowered in {n.lower() for n in PRODUCT_EXECUTABLES}:
        return True, f"file name matches product executable {name!r}"
    if lowered in UNINSTALLER_LEFTOVERS:
        return True, f"installer leftover name {name!r}"
    if _TOOL_RE.match(name):
        return True, f"file name matches Arena uninstall tool {name!r}"
    if _SETUP_RE.match(name):
        return True, f"file name matches Arena setup package {name!r}"
    return False, ""


def token_in_name(path: Path) -> bool:
    """True when the bare file/folder name merely contains a product token."""
    return bool(_TOKEN_RE.search(path.name))


def setup_package_signal(path: Path) -> tuple[bool, str]:
    if _SETUP_RE.match(path.name):
        return True, f"setup package name {path.name!r}"
    return False, ""


def metadata_signal(path: Path) -> tuple[bool, str, dict[str, str]]:
    """Strong signal: PE version resource names product AND publisher.

    A file whose metadata merely mentions "Arena" without our publisher is
    deliberately NOT confirmed — other vendors ship things called Arena.
    """
    try:
        if not path.is_file() or path.stat().st_size > 512 * 1024 * 1024:
            return False, "", {}
    except OSError:
        return False, "", {}
    try:
        with open(path, "rb") as handle:
            head = handle.read(2)
    except OSError:
        return False, "", {}
    if head != pe.PE_MAGIC:
        return False, "", {}
    meta = pe.product_strings(path)
    product = meta.get("product", "").lower()
    company = meta.get("company", "").lower()
    product_is_ours = any(marker == product for marker in PRODUCT_NAME_MARKERS) \
        or product.startswith("axiom arena")
    publisher_matches = company == PUBLISHER.lower()
    if product_is_ours and publisher_matches:
        return True, pe.summarise(meta), meta
    if meta.get("product"):
        return False, pe.summarise(meta), meta
    return False, "", meta


def tool_metadata_signal(path: Path) -> tuple[bool, str]:
    """True when the file IS this uninstaller (or a copy of it)."""
    try:
        if not path.is_file():
            return False, ""
    except OSError:
        return False, ""
    meta = pe.product_strings(path)
    if meta.get("product") == TOOL_PRODUCT_NAME:
        return True, "metadata: this is the Arena uninstall tool"
    return False, ""


def marker_signal(root: Path, depth: int = 3) -> tuple[bool, str]:
    """Medium signal: the tree contains files only Arena writes.

    Bounded and cheap: a handful of ``exists()`` probes plus one guarded read
    of ``app/__init__.py`` looking for the package title line. Never walks
    the whole tree.
    """
    try:
        if not root.is_dir():
            return False, ""
    except OSError:
        return False, ""
    for rel, description in MARKER_PATHS:
        candidate = root.joinpath(*rel)
        try:
            if candidate.exists():
                if rel == ("app", "__init__.py"):
                    if _app_title_is_arena(candidate):
                        return True, description
                    continue
                return True, description
        except OSError:
            continue
    for name, description in MARKER_FILES.items():
        candidate = root / name
        try:
            if candidate.is_file():
                if name == "build_meta.json":
                    # Only counts next to an app/ tree — any tool can write
                    # a file called BUILD_META.json.
                    if (root / "app").is_dir() or (root / "app" / "__init__.py").exists():
                        return True, description
                    continue
                return True, description
        except OSError:
            continue
    return False, ""


def _app_title_is_arena(path: Path) -> bool:
    """True when app/__init__.py declares ``__title__ = "arena"``."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    for line in text.splitlines()[:40]:
        compact = line.replace(" ", "").lower()
        if compact.startswith('__title__="arena"') or compact.startswith("__title__='arena'"):
            return True
    return False


def registry_entry_signal(display_name: str, publisher: str,
                          app_id: str = "") -> tuple[bool, str]:
    """Strong signal for a registration: our AppId, or name AND publisher."""
    if app_id and INSTALLER_APP_ID.upper() in app_id.upper():
        return True, f"uninstall registration AppId {INSTALLER_APP_ID}"
    name = (display_name or "").strip().lower()
    pub = (publisher or "").strip().lower()
    if name in ("axiom arena", "arena", "axiom") and pub == PUBLISHER.lower():
        return True, f"uninstall registration {display_name!r} by {publisher!r}"
    return False, ""


def shortcut_signal(path: Path, known_names: tuple[str, ...]) -> tuple[bool, str]:
    """Shortcut names our installer creates (exact names only)."""
    if path.name in known_names:
        return True, f"shortcut created by the installer ({path.name})"
    return False, ""


def weak_token_signal(path: Path) -> tuple[bool, str]:
    if token_in_name(path):
        return True, (f"name contains the product token "
                      f"({path.name!r}) — no ownership evidence")
    return False, ""


def secret_bearing_name(path: Path) -> bool:
    return looks_secret(path.name)


def describe_evidence(signals: dict[str, str]) -> str:
    return "; ".join(f"{k}: {v}" for k, v in signals.items() if v)
