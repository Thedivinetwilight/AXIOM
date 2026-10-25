# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for AXIOM Operator (local paper + guarded Vantage MT5).

Bundle contents that matter at runtime:

* ``app/static``      - the chart terminal's own CSS/JS (no CDN, no remote asset)
* ``data``            - the bundled DEMO sample CSVs the chart can preview
* ``app/data/upstox_proto`` - generated protobuf bindings imported dynamically

Everything writable (config, database, logs) lives in the per-user data
directory created by ``axiom_launcher.py`` so upgrades never touch operator state.
"""

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

# Locate the repository root from the spec's own location, so the recipe works
# regardless of the caller's working directory.
HERE = Path(SPECPATH).resolve()
ROOT = HERE
for _ in range(4):
    if (ROOT / "app").is_dir() and (ROOT / "pyproject.toml").is_file():
        break
    ROOT = ROOT.parent
else:  # pragma: no cover - build-time guard
    raise SystemExit("cannot locate the repository root from %s" % HERE)

datas = [
    (str(ROOT / "app" / "static"), "app/static"),
    (str(ROOT / "data"), "data"),
    (str(ROOT / "app" / "data" / "upstox_proto"), "app/data/upstox_proto"),
    # PyInstaller data destinations are directories, not target filenames.
    # /manual reads USER_MANUAL.md at the frozen resource root.
    (str(ROOT / "USER_MANUAL.md"), "."),
    (str(ROOT / "docs" / "VANTAGE_NEXT_VERSION_PLAN.md"), "docs"),
    (str(ROOT / "docs" / "TERMINAL_REDESIGN_PLAN.md"), "docs"),
    (str(ROOT / "docs" / "TERMINAL_PERFORMANCE.md"), "docs"),
    (str(ROOT / "docs" / "LISTED_ASSETS.md"), "docs"),
    (str(ROOT / "docs" / "RESEARCH_CANDIDATES.md"), "docs"),
    (str(ROOT / "docs" / "WINDOWS_APP_CONTROL.md"), "docs"),
]

# uvicorn resolves its loop and protocol implementations by name at runtime, so
# the static analysis cannot see them.
hiddenimports = (
    collect_submodules("app")
    + collect_submodules("app.data.upstox_proto")
    + [
        "uvicorn",
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
        "uvicorn.protocols.websockets.websockets_impl",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        "uvicorn.lifespan.off",
        "websockets.asyncio.client",
        "websockets.legacy",
        "httpcore",
        "httpx",
        "zoneinfo",
    ]
)

if sys.platform.startswith("win"):
    # The official package is a lazy runtime import and includes its own
    # terminal IPC extension; NumPy is required for MT5 rate arrays.
    hiddenimports.append("MetaTrader5")

# Never ship research/analysis stacks the terminal does not use. NumPy is the
# one numerical dependency retained because the official MT5 wheel needs it.
excludes = [
    "tkinter",
    "PySide6",
    "PyQt5",
    "PyQt6",
    "matplotlib",
    "pandas",
    "scipy",
    "pyarrow",
    "fastparquet",
    "IPython",
    "notebook",
]

a = Analysis(
    [str(HERE / "axiom_launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# The default remains the upgrade-compatible onefile executable.  The App
# Control workaround uses the same analyzed Python code in onedir mode: Windows
# then loads the application and its dependencies from the extracted/install
# folder instead of a PyInstaller temporary _MEI directory.
if os.environ.get("AXIOM_ONEDIR") == "1":
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="AXIOM-Chart",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=True,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        version=str(HERE / "version_info.txt") if sys.platform.startswith("win") else None,
        icon=None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name="AXIOM-Operator",
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.zipfiles,
        a.datas,
        [],
        name="AXIOM-Chart",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        upx_exclude=[],
        runtime_tmpdir=None,
        # Console mode exposes startup/refusal messages and Ctrl+C. Secrets are
        # deliberately not printed; the operator reads the private config locally.
        console=True,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        version=str(HERE / "version_info.txt") if sys.platform.startswith("win") else None,
        icon=None,
    )
