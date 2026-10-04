"""Build identity for live execution intents (FINAL-11/10 §5, §50).

Every live order carries the identity of the code that decided it: the git
tree SHA when running from a checkout, or the SHA-256 of the frozen binary
when running packaged. If neither can be established the identity says so
loudly ("unproven") — it never fabricates a value.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from functools import lru_cache


@lru_cache(maxsize=1)
def _git_identity() -> str:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True,
            timeout=5, cwd=os.path.dirname(os.path.dirname(
                os.path.dirname(os.path.abspath(__file__)))))
        if commit.returncode == 0 and commit.stdout.strip():
            tree = subprocess.run(
                ["git", "rev-parse", "HEAD^{tree}"], capture_output=True,
                text=True, timeout=5, cwd=os.path.dirname(os.path.dirname(
                    os.path.dirname(os.path.abspath(__file__)))))
            tree_sha = tree.stdout.strip() if tree.returncode == 0 else ""
            dirty = subprocess.run(
                ["git", "status", "--porcelain"], capture_output=True,
                text=True, timeout=5, cwd=os.path.dirname(os.path.dirname(
                    os.path.dirname(os.path.abspath(__file__)))))
            state = "dirty" if dirty.stdout.strip() else "clean"
            return (f"git:{commit.stdout.strip()[:12]}"
                    + (f"/tree:{tree_sha[:12]}" if tree_sha else "")
                    + f"/{state}")
    except Exception:
        pass
    return ""


@lru_cache(maxsize=1)
def _binary_identity() -> str:
    if not getattr(sys, "frozen", False):
        return ""
    try:
        exe = sys.executable or ""
        if exe and os.path.exists(exe):
            digest = hashlib.sha256()
            with open(exe, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    digest.update(chunk)
            return f"frozen:sha256:{digest.hexdigest()[:16]}"
    except Exception:
        pass
    return "frozen:unproven"


def build_identity_string() -> str:
    """A compact, deterministic identity string for audit rows."""
    parts = [p for p in (_git_identity(), _binary_identity()) if p]
    parts.append(f"python:{sys.version_info.major}.{sys.version_info.minor}."
                 f"{sys.version_info.micro}")
    return "|".join(parts) if parts else "unproven"
