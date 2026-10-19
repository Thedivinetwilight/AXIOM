# PyInstaller spec for Arena.
#
# Build with:  python build_exe.py           (recommended)
# or:          pyinstaller packaging/arena.spec --noconfirm
#
# PyInstaller builds for the platform it RUNS on — a Windows .exe must be built
# on Windows. This spec is deliberately platform-neutral so the identical file
# produces Arena.exe on Windows, Arena.app/binary on macOS and a Linux binary
# here.
#
# One-file mode is used because the whole point is a single artifact somebody
# can double-click. The trade-off is a few seconds of unpacking at each launch;
# the app compensates by keeping all STATE in a per-user directory
# (see app/runtime.py) rather than next to the executable.

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules, collect_data_files

ROOT = Path(SPECPATH).resolve().parent

# Everything uvicorn imports at runtime by name. PyInstaller's static analysis
# cannot see these (they are chosen from strings), so a build without them
# starts and then dies with "Could not import module" the moment it serves.
hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    # Loaded by app/__main__.py through importlib for the operator-error
    # boundary; importable statically too, but listed so a future refactor
    # cannot silently drop them from the bundle.
    "app.data.upstox_auth",
    "app.data.upstox_stream",
    "app.brokers.sandbox",
    "app.api.routes.console",
    "app.api.routes.setup",
    "app.console.catalog",
    "app.console.runner",
    "app.console.page",
    "app.console.setup_page",
    # 3.1.0: the redesigned frontend + wizard backend + app-level endpoints.
    "app.api.routes.frontend",
    "app.api.routes.wizard",
    "app.api.routes.app_api",
    # The complete live-trading rail ships inside the bundle (locked, but
    # present and functional — activation stays deliberate and external).
    "app.brokers.live",
    "app.live.alerts",
    "app.live.digest",
    "app.live.drill",
    "app.live.guards",
    "app.live.lock",
    "app.live.ops",
    "app.live.pnl",
    "app.live.preflight",
    "app.live.watchdog",
    "app.validation.walk_forward",
    "app.validation.robustness",
    "app.validation.train_test",
    "app.instance",
    "app.build_info",
]
hiddenimports += collect_submodules("uvicorn")
hiddenimports += collect_submodules("websockets")
hiddenimports += collect_submodules("certifi")

# Files the app reads from its own tree. Bundled data lands inside the
# temporary extraction directory, which is exactly where ROOT points when
# frozen, so `ROOT / "data/sample.csv"` keeps working.
datas = []
for pattern in (
    ("data/sample.csv", "data"),
    ("data/sample_1h.csv", "data"),
    ("data/sample_alt.csv", "data"),
    ("data/sample_alt_1h.csv", "data"),
    ("data/example_grids.json", "data"),
    ("data/example_scoring.json", "data"),
    ("data/tournament_datasets.json", "data"),
    (".env.example", "."),
    ("ARENA_GUIDE.md", "."),
    ("requirements.txt", "."),
):
    source = ROOT / pattern[0]
    if source.exists():
        datas.append((str(source), pattern[1]))

# 3.1.0: the redesigned frontend. Static files (never templated through Python
# strings), resolved at runtime by app.runtime.static_dir(). BUILD_META.json is
# written by build_exe.py before the build; a source-run fallback covers its
# absence so the spec stays usable with a bare `pyinstaller` invocation.
for pattern in (
    ("app/static/arena.css", "app/static"),
    ("app/static/arena-core.js", "app/static"),
    ("app/static/arena-wizard.js", "app/static"),
    ("app/static/arena-live.js", "app/static"),
    ("app/static/arena-app.js", "app/static"),
    ("app/BUILD_META.json", "app"),
):
    source = ROOT / pattern[0]
    if source.exists():
        datas.append((str(source), pattern[1]))

# Bundle certifi's CA certificate store. Without it, HTTPS calls to
# api.upstox.com fail inside the frozen Windows .exe with
# "unable to get local issuer certificate" which surfaces to the UI only
# as the generic "Failed to fetch" banner. Must come after `datas = []`
# above so the name is defined.
datas += collect_data_files("certifi")

block_cipher = None

# ctypes is used ONLY for the Windows MessageBox fallback in app.instance /
# arena.py (guarded by os.name checks there). Where the interpreter has no
# _ctypes extension (source-built frozen toolchain without libffi) the module
# cannot be scanned, so exclude it — the runtime code already degrades
# gracefully. Windows CI builds have _ctypes and keep ctypes bundled.
import importlib.util as _ilu
_ctypes_excludes = [] if _ilu.find_spec("_ctypes") else ["ctypes", "ctypes.util"]

a = Analysis(
    [str(ROOT / "arena.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Exclude what a headless trading engine never needs; each of these is
    # large and some of them (tkinter, test suites) can break the build.
    excludes=[
        "tkinter",
        "test",
        "unittest",
        "pytest",
        "PySide6",
        "PyQt5",
        "PyQt6",
        "matplotlib",
        "IPython",
        "notebook",
        "pandas.tests",
        "numpy.tests",
    ] + _ctypes_excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="Arena",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX would flag the binary with most antivirus vendors
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,  # a visible console IS the status window for a local server
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
