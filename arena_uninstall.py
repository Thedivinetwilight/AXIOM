#!/usr/bin/env python3
"""Arena Deep Uninstaller — entry point.

    python arena_uninstall.py --mode deep

On Windows this module is the PyInstaller entry point for the standalone
``Arena-Uninstall.exe`` (see ``build_uninstaller.py`` and
``packaging/uninstaller.spec``). It imports nothing from Arena, so the tool
works when Arena is closed, corrupted or already removed.

Modes:
    standard   remove one installed Arena (registration, install tree's
               disposable parts, installer shortcuts) and preserve user data
    deep       additionally scan every accessible local fixed drive for
               leftover and duplicate Arena/AXIOM artefacts
"""

from __future__ import annotations

import sys


def main() -> int:
    from uninstaller.cli import run
    return run()


if __name__ == "__main__":
    sys.exit(main())
