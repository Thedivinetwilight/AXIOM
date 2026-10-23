# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for the AXIOM chart terminal (paper only).

Bundle contents that matter at runtime:

* ``app/static``      - the chart terminal's own CSS/JS (no CDN, no remote asset)
* ``data``            - the bundled DEMO sample CSVs the chart can preview
* ``app/data/upstox_proto`` - generated protobuf bindings imported dynamically

Everything writable (config, database, logs) lives in the per-user data
directory created by ``axiom_launcher.py`` so upgrades never touch operator state.
"""

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

# Never ship research/analysis stacks the terminal does not use.
excludes = [
    "tkinter",
    "PySide6",
    "PyQt5",
    "PyQt6",
    "matplotlib",
    "pandas",
    "numpy",
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
    # console=True on purpose: this application prints the local API token and
    # the chart URL at start-up, and a windowed build could not show them.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=str(HERE / "version_info.txt") if sys.platform.startswith("win") else None,
    icon=None,
)
