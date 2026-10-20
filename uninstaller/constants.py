"""Constant definitions for the Arena Deep Uninstaller.

Every product-identity token the uninstaller trusts lives here. The golden
safety rule of this tool is encoded in ``classify.py``: a path substring such
as "AXIOM" or "Arena" NEVER proves ownership. Ownership requires an
authoritative signal (an uninstall registration, a PE version resource, a
product marker file) or at least two independent weak signals.

None of this code imports anything from the Arena application: the uninstaller
must keep working when Arena is closed, corrupted or completely removed.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Product identity
# ---------------------------------------------------------------------------

PRODUCT_NAME = "AXIOM Arena"
SHORT_NAMES = ("AXIOM", "Arena")
PUBLISHER = "Thedivinetwilight"
# AppId from packaging/installer.iss (Inno Setup {AppId=...}).
INSTALLER_APP_ID = "{B8E5C7A2-4F31-4D29-9E8A-1A2B3C4D5E6F}"

# Executable file names the product ships. A file with one of these names is
# still only a WEAK signal until metadata or a registration confirms it.
PRODUCT_EXECUTABLES = ("Arena.exe", "AXIOM.exe", "AXIOM-Kronos.exe")
SETUP_EXECUTABLE_RE = r"^(?:axiom|arena)[-_ ]?(?:setup|install)[^/\\]*\.exe$"
UNINSTALL_TOOL_RE = r"^arena[-_ ]?uninstall[^/\\]*\.exe$"
# Inno Setup / NSIS leftovers that only ever appear beside an installed app.
UNINSTALLER_LEFTOVERS = ("unins000.exe", "unins000.dat", "unins000.msg",
                         "uninstall.exe", "uninstall.dat", "uninstall.xml")

# PE version resource values our builds carry (set in packaging/uninstaller.spec
# and by Inno Setup's VersionInfo* settings).
TOOL_PRODUCT_NAME = "AXIOM Arena Uninstall"
PRODUCT_NAME_MARKERS = ("axiom arena", "axiom", "arena")
PRODUCT_INTERNAL_NAMES = ("arena", "axiom", "axiom-kronos", "arena-uninstall")

# Start Menu / desktop shortcut names created by packaging/installer.iss.
SHORTCUT_NAMES = ("AXIOM Arena.lnk", "AXIOM Arena (Setup).lnk",
                  "Uninstall AXIOM Arena.lnk")
START_MENU_GROUP = "AXIOM Arena"

# ---------------------------------------------------------------------------
# Per-user locations (see app/runtime.py — APP_DIR_NAME == "Arena")
# ---------------------------------------------------------------------------

LOCAL_APP_DIR = "Arena"          # runtime home: %LOCALAPPDATA%\Arena
LOCAL_AXIOM_DIR = "AXIOM"        # %LOCALAPPDATA%\AXIOM (models cache)
MODELS_CACHE_SUBDIR = ("AXIOM", "models")
INSTALL_DIR_NAMES = ("AXIOM", "Arena")

# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------

CATEGORY_INSTALLATION = "installation"
CATEGORY_REGISTRATION = "uninstall-registration"
CATEGORY_DUPLICATE = "duplicate-executable"
CATEGORY_SETUP = "setup-installer"
CATEGORY_EXTRACTED = "extracted-application"
CATEGORY_RUNTIME_HOME = "runtime-home"
CATEGORY_CACHE = "cache"
CATEGORY_LEFTOVER = "leftover"
CATEGORY_AMBIGUOUS = "ambiguous"
CATEGORY_SELF = "self"
CATEGORY_TOOL = "uninstall-tool"
CATEGORY_TRADING_HISTORY = "trading-history"
CATEGORY_LOGS = "logs"
CATEGORY_CONFIGURATION = "configuration"
CATEGORY_CREDENTIALS = "credentials"
CATEGORY_SOURCE_REPO = "source-repository"
CATEGORY_DEVELOPMENT = "development"
CATEGORY_RESEARCH = "research"

#: Categories preserved by default. Deleting anything in these requires the
#: operator to enable the category explicitly (separate, deliberate option)
#: BEFORE it can even be selected for deletion.
PROTECTED_CATEGORIES = frozenset({
    CATEGORY_TRADING_HISTORY,
    CATEGORY_LOGS,
    CATEGORY_CONFIGURATION,
    CATEGORY_CREDENTIALS,
    CATEGORY_SOURCE_REPO,
    CATEGORY_DEVELOPMENT,
    CATEGORY_RESEARCH,
})

#: Ambiguous matches are never removed automatically and never pre-selected.
ALWAYS_DESELECTED = frozenset({CATEGORY_AMBIGUOUS})

#: CLI flag name -> protected category it unlocks.
PROTECTED_CATEGORY_FLAGS = {
    "include-trading-history": CATEGORY_TRADING_HISTORY,
    "include-logs": CATEGORY_LOGS,
    "include-configuration": CATEGORY_CONFIGURATION,
    "include-credentials": CATEGORY_CREDENTIALS,
    "include-source-repositories": CATEGORY_SOURCE_REPO,
    "include-development": CATEGORY_DEVELOPMENT,
    "include-research": CATEGORY_RESEARCH,
}

CATEGORY_LABELS = {
    CATEGORY_INSTALLATION: "Installed application",
    CATEGORY_REGISTRATION: "Uninstall registration / shortcut",
    CATEGORY_DUPLICATE: "Duplicate Arena executable",
    CATEGORY_SETUP: "Setup installer package",
    CATEGORY_EXTRACTED: "Extracted application folder",
    CATEGORY_RUNTIME_HOME: "Per-user data folder (contents classified)",
    CATEGORY_CACHE: "Application cache",
    CATEGORY_LEFTOVER: "Leftover file",
    CATEGORY_AMBIGUOUS: "Ambiguous match (never automatic)",
    CATEGORY_SELF: "This uninstaller (handled safely)",
    CATEGORY_TOOL: "Uninstall tool copy",
    CATEGORY_TRADING_HISTORY: "Trading history (preserved)",
    CATEGORY_LOGS: "Logs (preserved)",
    CATEGORY_CONFIGURATION: "Configuration (preserved)",
    CATEGORY_CREDENTIALS: "Credentials (preserved)",
    CATEGORY_SOURCE_REPO: "Source repository (preserved)",
    CATEGORY_DEVELOPMENT: "Development folder (preserved)",
    CATEGORY_RESEARCH: "Research data (preserved)",
}

# ---------------------------------------------------------------------------
# Confidences
# ---------------------------------------------------------------------------

CONFIDENCE_CONFIRMED = "confirmed"
CONFIDENCE_LIKELY = "likely"
CONFIDENCE_AMBIGUOUS = "ambiguous"

# ---------------------------------------------------------------------------
# Signal identifiers (recorded on every match so a reviewer can audit WHY)
# ---------------------------------------------------------------------------

SIGNAL_REGISTRY = "registry-uninstall"
SIGNAL_KNOWN_PATH = "known-app-path"
SIGNAL_METADATA = "pe-metadata"
SIGNAL_MARKER = "app-marker"
SIGNAL_FILENAME = "filename-only"
SIGNAL_DUPLICATE = "byte-identical-duplicate"
SIGNAL_SHORTCUT = "installer-shortcut"
SIGNAL_TREE = "owned-tree-content"

# ---------------------------------------------------------------------------
# Deletion safety
# ---------------------------------------------------------------------------

#: The phrase the operator must type (or pass via --confirm-delete) before any
#: destructive action runs. Empty confirmation NEVER deletes.
CONFIRMATION_PHRASE = "DELETE"

#: Quarantine lives under the tool home so it survives until the user purges it.
DEFAULT_TOOL_DIR_NAME = "Arena-Uninstall"
QUARANTINE_DIR_NAME = "quarantine"
LOG_DIR_NAME = "logs"

#: Regular expression fragments used for filename screening during a scan.
WEAK_NAME_RE = r"(?i)^(?:axiom|arena)"
