#!/usr/bin/env python3
"""Repository-root source launcher for AXIOM Operator (`/terminal`).

Works whether invoked from the repository root or from any other working
directory without manual `PYTHONPATH` configuration:

  PowerShell (from repo root):
    .\\.venv\\Scripts\\python.exe axiom_launcher.py

  PowerShell (from any current directory):
    & "C:\\path\\to\\AXIOM\\.venv\\Scripts\\python.exe" "C:\\path\\to\\AXIOM\\axiom_launcher.py"

Preserves existing `%LOCALAPPDATA%\\AXIOM` configuration, DPAPI credentials,
and SQLite databases, and keeps all live execution gates disabled and disarmed
by default.
"""

from __future__ import annotations

from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def main(argv: list[str] | None = None) -> int:
    from app.source_launcher import cli_main

    return cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
