"""Build identity: version + commit, truthful in source and frozen builds.

A stale-UI incident (setup page fixed in source, broken page still served by an
old process) showed that every surface must be able to answer *which* Arena is
serving. So the version and the commit it was built from are surfaced in
``/health``, ``/api/boot``, the wizard, the dashboard footer and the frozen
smoke test — all from this single module.

Resolution order for the commit:

1. ``app/BUILD_META.json`` — written by ``build_exe.py`` at build time and
   bundled into the executable, so a packaged binary reports the exact commit
   it was built from even on machines without git;
2. ``git rev-parse`` — when running from a source checkout;
3. ``"source"`` — neither available (e.g. a sdist without .git).

Never raises: a missing identity must not stop Arena from starting.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path

_META_NAME = "BUILD_META.json"


def _bundled_meta_path() -> Path:
    """Location of BUILD_META.json, inside the bundle when frozen."""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return base / "app" / _META_NAME
    return Path(__file__).resolve().parent / _META_NAME


def _meta() -> dict:
    try:
        return json.loads(_bundled_meta_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        # Recorded, not swallowed: a missing identity must stay diagnosable.
        logging.getLogger("bot.build").debug(
            "git_identity_unavailable", extra={"error": type(exc).__name__})
    return ""


def version() -> str:
    from app import __version__

    return __version__


def commit() -> str:
    """Full commit SHA the running code was built from ('' if unknown)."""
    meta = _meta()
    if meta.get("commit"):
        return str(meta["commit"])
    return _git_commit()


def short_commit() -> str:
    sha = commit()
    return sha[:10] if sha else "source"


def build_timestamp() -> str:
    return str(_meta().get("built_at", ""))


def build_id() -> str:
    """A short, human-readable identifier for diagnostics and cache busting:
    ``<version>-<commit10>`` (never contains secrets or spaces)."""
    return f"{version()}-{short_commit()}"


def describe() -> dict:
    return {
        "version": version(),
        "commit": commit(),
        "commit_short": short_commit(),
        "build_id": build_id(),
        "built_at": build_timestamp(),
        "frozen": bool(getattr(sys, "frozen", False)),
    }
