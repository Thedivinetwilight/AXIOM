"""Build identity of the running package.

Lets an operator verify — from the running app itself, in a cache-proof way —
which commit was frozen and which UI/API files are actually being served,
instead of trusting an install path, a shortcut target, or a browser cache.

In a packaged build the Windows freeze writes ``app/_build_identity.json``
(commit, version, build time and SHA-256 of the reviewed source files) into
the bundle; in a development checkout the file is absent and the commit reads
``dev``. Either way ``served_files`` is hashed at request time from the exact
bytes the server serves.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent

# UI/API files whose served bytes are hashed at runtime for identity checks.
SERVED_FILES = (
    "static/autonomy/autonomy.js",
    "static/autonomy/index.html",
    "static/chart/chart.js",
    "static/terminal/terminal.js",
)


def _embedded_identity() -> dict:
    try:
        return json.loads((APP_DIR / "_build_identity.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def build_identity() -> dict:
    identity = _embedded_identity()
    served: dict[str, str] = {}
    for relative in SERVED_FILES:
        path = APP_DIR / relative
        if path.is_file():
            served[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "commit": str(identity.get("commit") or "dev"),
        "version": str(identity.get("version") or ""),
        "built_at": str(identity.get("built_at") or ""),
        "frozen": bool(getattr(sys, "frozen", False)),
        "source_hashes": dict(identity.get("source_hashes") or {}),
        "served_files": served,
    }
