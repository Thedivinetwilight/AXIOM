"""``python -m uninstaller`` — same entry point as the frozen executable."""

from __future__ import annotations

from uninstaller.cli import run

if __name__ == "__main__":
    raise SystemExit(run())
