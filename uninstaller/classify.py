"""Turn evidence into verdicts: category, confidence and default selection.

The combination rules encode the product's golden safety rule — a name is
never enough. The exact table (see ``classify_file``) is unit-tested from both
directions: real artefacts must classify as owned, and look-alikes (a folder
called "Arena Games", "AXIOM-notes.txt", an unrelated "unins000.exe") must
classify as ambiguous and stay unselected.

Content INSIDE a confirmed Arena-owned tree is classified by path role
(trading history, logs, configuration, credentials, source, development,
research vs. disposable payload) rather than by ownership signals: the tree
itself is already proven, and the question becomes "may this be deleted by
default?" — for the seven preserved categories the answer is always no.
"""

from __future__ import annotations

from pathlib import Path
from uninstaller import signals
from uninstaller.constants import (
    CATEGORY_AMBIGUOUS,
    CATEGORY_CACHE,
    CATEGORY_CONFIGURATION,
    CATEGORY_CREDENTIALS,
    CATEGORY_DEVELOPMENT,
    CATEGORY_DUPLICATE,
    CATEGORY_EXTRACTED,
    CATEGORY_INSTALLATION,
    CATEGORY_LEFTOVER,
    CATEGORY_LOGS,
    CATEGORY_REGISTRATION,
    CATEGORY_RESEARCH,
    CATEGORY_RUNTIME_HOME,
    CATEGORY_SETUP,
    CATEGORY_SOURCE_REPO,
    CATEGORY_TOOL,
    CATEGORY_TRADING_HISTORY,
    CONFIDENCE_AMBIGUOUS,
    CONFIDENCE_CONFIRMED,
    CONFIDENCE_LIKELY,
    PRODUCT_EXECUTABLES,
    SIGNAL_DUPLICATE,
    SIGNAL_FILENAME,
    SIGNAL_KNOWN_PATH,
    SIGNAL_MARKER,
    SIGNAL_METADATA,
    SIGNAL_REGISTRY,
    SIGNAL_SHORTCUT,
    SIGNAL_TREE,
    UNINSTALLER_LEFTOVERS,
)
from uninstaller.model import Match

# ---------------------------------------------------------------------------
# Evidence container
# ---------------------------------------------------------------------------


class Evidence:
    """What we know about one path. Empty strings mean 'no evidence'."""

    def __init__(self) -> None:
        self.registry: str = ""
        self.known_path: str = ""
        self.metadata: str = ""
        self.marker: str = ""
        self.filename: str = ""
        self.shortcut: str = ""
        self.token: str = ""
        self.duplicate: str = ""
        self.tree: str = ""
        self.tool: str = ""

    def fired(self) -> dict[str, str]:
        out = {}
        for name in ("registry", "known_path", "metadata", "marker", "filename",
                     "shortcut", "token", "duplicate", "tree", "tool"):
            value = getattr(self, name)
            if value:
                out[name] = value
        return out

    def reasons(self) -> list[str]:
        return [f"{k}: {v}" for k, v in self.fired().items()]

    def signal_ids(self) -> list[str]:
        mapping = {
            "registry": SIGNAL_REGISTRY,
            "known_path": SIGNAL_KNOWN_PATH,
            "metadata": SIGNAL_METADATA,
            "marker": SIGNAL_MARKER,
            "filename": SIGNAL_FILENAME,
            "shortcut": SIGNAL_SHORTCUT,
            "duplicate": SIGNAL_DUPLICATE,
            "tree": SIGNAL_TREE,
        }
        return [mapping[k] for k in self.fired() if k in mapping]


# ---------------------------------------------------------------------------
# Tree-content rules (path roles inside a CONFIRMED Arena-owned tree)
# ---------------------------------------------------------------------------

_DATA_PROTECTED_SUB = {"history"}
_CACHE_DIRS = {"__pycache__", "build", "dist", "models"}
_CACHE_PREFIXES = ("_mei",)
_DEVELOPMENT_DIRS = {"node_modules", ".tox", ".nox", ".mypy_cache",
                     ".pytest_cache", ".ruff_cache", "venv", "env"}
_DEVELOPMENT_PREFIXES = (".venv",)
_RESEARCH_DIRS = {"research", "results", "notebooks"}
_LOG_SUFFIXES = (".log", ".jsonl", ".out", ".err")
_DB_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".db-shm", ".db-wal")
_CONFIG_NAMES = {"paper-database-override.json"}
_CONFIG_SUFFIXES = (".ini", ".cfg", ".conf", ".toml", ".yaml", ".yml")


def classify_tree_child(rel_parts: tuple[str, ...], is_dir: bool,
                        name: str) -> str | None:
    """Category for a path inside an owned tree, or ``None`` for payload.

    Protected categories are returned for whole subtrees (``logs/``,
    ``data/history/`` ...): the reviewer sees one preserved unit, and nothing
    inside it is ever pre-selected.
    """
    lowered = name.lower()
    top = rel_parts[0].lower() if rel_parts else lowered

    # Credential-bearing names win over everything, including suffixes.
    if signals.secret_bearing_name(Path(name)):
        return CATEGORY_CREDENTIALS

    if lowered == ".env" or lowered.startswith(".env."):
        return CATEGORY_CONFIGURATION
    if lowered in _CONFIG_NAMES:
        return CATEGORY_CONFIGURATION

    if top == ".git":
        return CATEGORY_SOURCE_REPO
    if top == "logs":
        return CATEGORY_LOGS
    if top in ("data", "backups", "history"):
        if len(rel_parts) > 1 and rel_parts[1].lower() in _DATA_PROTECTED_SUB:
            return CATEGORY_TRADING_HISTORY
        if len(rel_parts) > 1 and rel_parts[1].lower() == "cache":
            return CATEGORY_CACHE
        return CATEGORY_TRADING_HISTORY
    if top in _RESEARCH_DIRS:
        return CATEGORY_RESEARCH
    if top in _CACHE_DIRS or any(top.startswith(p) for p in _CACHE_PREFIXES):
        return CATEGORY_CACHE
    if top in _DEVELOPMENT_DIRS or any(top.startswith(p) for p in _DEVELOPMENT_PREFIXES):
        return CATEGORY_DEVELOPMENT
    if lowered.endswith(".egg-info") or lowered.endswith(".egg"):
        return CATEGORY_DEVELOPMENT

    if not is_dir:
        if lowered.endswith(".pyc") or lowered.endswith(".pyo"):
            return CATEGORY_CACHE
        if lowered.endswith(_LOG_SUFFIXES):
            return CATEGORY_LOGS
        if lowered.endswith(_DB_SUFFIXES):
            return CATEGORY_TRADING_HISTORY
        if lowered.endswith(".ipynb"):
            return CATEGORY_RESEARCH
        if lowered.endswith(_CONFIG_SUFFIXES):
            return CATEGORY_CONFIGURATION
        if lowered in UNINSTALLER_LEFTOVERS:
            return CATEGORY_LEFTOVER
        if signals.is_setup_package(name):
            return CATEGORY_SETUP
        if signals.is_uninstall_tool(name):
            return CATEGORY_TOOL
    return None  # payload: deletable with the tree


PROTECTED_RULE_DIRS = (
    ("logs", CATEGORY_LOGS),
    ("research", CATEGORY_RESEARCH),
    ("results", CATEGORY_RESEARCH),
    ("notebooks", CATEGORY_RESEARCH),
    ("backups", CATEGORY_TRADING_HISTORY),
    ("history", CATEGORY_TRADING_HISTORY),
)


def peek_protected(path: Path, depth: int = 2) -> bool:
    """Cheap check: does this directory hide any preserved-category content?

    Used to decide whether a payload directory can be handled as ONE review
    item or must be expanded so preserved data inside it stays visible and
    unselected. Bounded: at most ``depth`` levels, no symlink following.
    """
    if depth < 0:
        return False
    try:
        entries = list(path.iterdir())
    except OSError:
        return True  # unreadable: assume the worst, expand if possible
    for entry in entries:
        try:
            is_dir = entry.is_dir() and not entry.is_symlink()
        except OSError:
            continue
        category = classify_tree_child((entry.name,), is_dir, entry.name)
        if category is not None and category not in (
                CATEGORY_CACHE, CATEGORY_LEFTOVER, CATEGORY_SETUP,
                CATEGORY_TOOL):
            return True
        if is_dir and depth > 0 and peek_protected(entry, depth - 1):
            return True
    return False


# ---------------------------------------------------------------------------
# File verdicts (outside an owned tree)
# ---------------------------------------------------------------------------


def classify_file(path: Path, evidence: Evidence, *,
                  is_self: bool = False) -> Match:
    """Combine evidence into a verdict for a standalone file."""
    name = path.name
    lowered = name.lower()
    size = 0
    try:
        size = path.stat().st_size
    except OSError:
        pass

    # 1. This uninstaller itself (running or not).
    if evidence.tool or is_self:
        return Match(
            path=str(path), category=CATEGORY_TOOL,
            reasons=evidence.reasons() or ["metadata: Arena uninstall tool"],
            signals=evidence.signal_ids(), confidence=CONFIDENCE_CONFIRMED,
            size_bytes=size, is_dir=False,
            selectable=not is_self,
            selected=False,
        )

    confirmed = False
    confidence = CONFIDENCE_AMBIGUOUS
    category = CATEGORY_AMBIGUOUS

    # 2. Strong metadata evidence (product AND publisher in the PE resource).
    if evidence.metadata:
        confirmed = True
        confidence = CONFIDENCE_CONFIRMED
        if signals.is_setup_package(name):
            category = CATEGORY_SETUP
        elif signals.is_product_executable(name):
            category = CATEGORY_INSTALLATION
        else:
            category = CATEGORY_LEFTOVER

    # 3. Registration evidence.
    if evidence.registry:
        confirmed = True
        confidence = CONFIDENCE_CONFIRMED
        if category in (CATEGORY_AMBIGUOUS,):
            category = CATEGORY_INSTALLATION

    # 4. Two medium signals: known path + filename/marker.
    if evidence.known_path and (evidence.filename or evidence.marker):
        confirmed = True
        confidence = CONFIDENCE_CONFIRMED
        if category == CATEGORY_AMBIGUOUS:
            category = _category_from_name(name, lowered)

    # 5. Installer shortcut (exact names created by our installer).
    if evidence.shortcut:
        confirmed = True
        confidence = CONFIDENCE_CONFIRMED
        category = CATEGORY_REGISTRATION

    # 6. Marker file standing alone (e.g. a portable override): likely ours,
    #    but classified by ROLE and never pre-selected when protected.
    if not confirmed and evidence.marker and not evidence.token:
        confidence = CONFIDENCE_LIKELY
        category = _category_from_name(name, lowered)

    # 7. Everything else — filename or bare token — is AMBIGUOUS.
    if not confirmed and category == CATEGORY_AMBIGUOUS:
        if evidence.duplicate:
            category = CATEGORY_DUPLICATE
            confidence = CONFIDENCE_LIKELY
        else:
            category = CATEGORY_AMBIGUOUS
            confidence = CONFIDENCE_AMBIGUOUS

    if evidence.duplicate and category in (CATEGORY_INSTALLATION,
                                           CATEGORY_LEFTOVER):
        category = CATEGORY_DUPLICATE

    match = Match(
        path=str(path), category=category,
        reasons=evidence.reasons(), signals=evidence.signal_ids(),
        confidence=confidence, size_bytes=size, is_dir=False,
    )
    if match.protected or match.ambiguous:
        match.selected = False
    else:
        match.selected = match.default_select()
    return match


def _category_from_name(name: str, lowered: str) -> str:
    # Files that play a preserved ROLE (configuration, credentials, history,
    # logs, research) keep that role even when found on their own.
    role = classify_tree_child((name,), False, name)
    if role in (CATEGORY_CONFIGURATION, CATEGORY_CREDENTIALS,
                CATEGORY_TRADING_HISTORY, CATEGORY_LOGS, CATEGORY_RESEARCH):
        return role
    if signals.is_setup_package(name):
        return CATEGORY_SETUP
    if signals.is_uninstall_tool(name):
        return CATEGORY_TOOL
    if signals.is_product_executable(name):
        return CATEGORY_INSTALLATION
    if lowered in UNINSTALLER_LEFTOVERS:
        return CATEGORY_LEFTOVER
    return CATEGORY_LEFTOVER


# ---------------------------------------------------------------------------
# Tree-root verdicts (directories)
# ---------------------------------------------------------------------------


def _is_models_cache(root: Path) -> bool:
    """True for the Kronos weight cache, at any nesting level."""
    try:
        if (root / "kronos").is_dir():
            return True
        return (root / "models" / "kronos").is_dir()
    except OSError:
        return False


def _is_runtime_home(root: Path) -> bool:
    r"""True for ``%LOCALAPPDATA%\Arena`` (the runtime home).

    Recognised by content only — a folder called "Arena" somewhere else is
    not a home. The runtime home itself is never a deletable unit; its
    children are classified one by one.
    """
    try:
        # An installation ships executables; a data folder does not. This is
        # what keeps "Program Files\AXIOM" (which carries data/ and .env
        # beside Arena.exe) an installation rather than a home.
        if any((root / name).exists() for name in PRODUCT_EXECUTABLES):
            return False
        markers = 0
        for name in ("trading_bot.db", "instance.json", ".env"):
            if (root / name).exists():
                markers += 1
        for name in ("data", "logs", "backups", "results"):
            if (root / name).is_dir():
                markers += 1
        return markers >= 3
    except OSError:
        return False


def classify_tree_root(root: Path, *, known_install: bool,
                       evidence: Evidence) -> tuple[str, str, list[str]]:
    """Verdict for a candidate directory: (category, confidence, reasons).

    ``source-repository`` outranks everything: a git checkout of the product
    is development material and is preserved by default even though it is
    unambiguously "ours".
    """
    reasons = evidence.reasons()

    is_source = ((root / ".git").exists() or (root / "pyproject.toml").exists()
                 or (root / "tests").is_dir()) and (
        (root / "arena.py").exists() or (root / "app" / "__init__.py").exists())
    if is_source:
        return (CATEGORY_SOURCE_REPO, CONFIDENCE_CONFIRMED,
                reasons + ["tree: Arena source checkout markers "
                           "(arena.py/app + .git|pyproject.toml|tests/)"])

    if root.name.lower().startswith("_mei"):
        # A PyInstaller one-file extraction directory: Arena's own temporary
        # unpack, left behind when a launch was killed.
        return (CATEGORY_CACHE, CONFIDENCE_CONFIRMED,
                reasons + ["tree: PyInstaller extraction directory "
                           "(_MEIxxxx) left behind"])
    if _is_models_cache(root):
        return (CATEGORY_CACHE, CONFIDENCE_CONFIRMED,
                reasons + ["tree: Kronos model cache (models/kronos)"])
    if _is_runtime_home(root):
        return (CATEGORY_RUNTIME_HOME, CONFIDENCE_CONFIRMED,
                reasons + ["tree: AXIOM Arena per-user data folder "
                           "(database, logs, configuration, research live here)"])

    if known_install:
        return (CATEGORY_INSTALLATION, CONFIDENCE_CONFIRMED,
                reasons + ["tree: known Arena application location"])

    if evidence.metadata or evidence.marker:
        category = CATEGORY_EXTRACTED
        return (category, CONFIDENCE_CONFIRMED,
                reasons + ["tree: contains confirmed Arena application files"])

    if evidence.filename or evidence.token:
        return (CATEGORY_AMBIGUOUS, CONFIDENCE_AMBIGUOUS,
                (reasons or [f"tree: directory name {root.name!r} looks related "
                             "but contains no Arena evidence"]))

    return (CATEGORY_AMBIGUOUS, CONFIDENCE_AMBIGUOUS,
            reasons or ["tree: no ownership evidence"])
