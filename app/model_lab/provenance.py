"""Software/environment identity pinned into research manifests.

A qualification record is only reproducible if it names the exact code that
produced it.  This helper stays dependency-free and read-only:

* it reads ``.git/HEAD`` (loose ref, then ``packed-refs``) instead of requiring
  a ``git`` binary, so a developer checkout, a CI checkout and a frozen Windows
  bundle all produce the same field shape;
* it optionally runs ``git status --porcelain`` when a ``git`` binary is
  available *and* the checkout is writable-territory — only to label the tree
  dirty; failure is reported as ``None`` ("unknown"), never as "clean";
* nothing is written, no network call is made and no environment variable is
  echoed except the ones named below (which carry no secrets).
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]

# Only these keys are ever copied into a manifest; anything resembling a
# credential is excluded by construction rather than by filtering.
SAFE_ENV_KEYS = ("AXIOM_DATA_SOURCE", "AXIOM_BROKER", "BROKER", "DATA_SOURCE")


def _read_git_commit(git_dir: Path) -> str:
    head = git_dir / "HEAD"
    try:
        text = head.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if not text:
        return ""
    if not text.startswith("ref:"):
        return text.lower() if len(text) == 40 else ""
    ref = text.split(":", 1)[1].strip()
    try:
        loose = (git_dir / ref).read_text(encoding="utf-8").strip().lower()
    except OSError:
        loose = ""  # unreadable loose ref: fall back to packed-refs below
    if loose:
        return loose
    try:
        for line in (git_dir / "packed-refs").read_text(encoding="utf-8").splitlines():
            if line.startswith("#") or " " not in line:
                continue
            sha, name = line.split(" ", 1)
            if name.strip() == ref:
                return sha.strip().lower()
    except OSError:
        return ""
    return ""


def _git_dirty(repo_root: Path) -> bool | None:
    """``True``/``False`` when git can answer, ``None`` when it cannot."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--porcelain", "--untracked-files=no"],
            capture_output=True,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return bool(proc.stdout.strip())


@lru_cache(maxsize=4)
def _fingerprint_cached(repo_root: str) -> tuple[tuple[str, Any], ...]:
    root = Path(repo_root)
    git_dir = root / ".git"
    commit = _read_git_commit(git_dir) if git_dir.is_dir() else ""
    dirty = _git_dirty(root) if commit else None
    try:
        from app.build_info import build_identity

        build = build_identity()
    except (ImportError, OSError, ValueError):  # pragma: no cover - defensive
        build = {}
    env = {key: os.environ[key] for key in SAFE_ENV_KEYS if os.environ.get(key)}
    return tuple(
        sorted(
            {
                "commit": commit or str(build.get("commit") or "dev"),
                "commit_source": "GIT_CHECKOUT" if commit else "BUILD_IDENTITY_OR_DEV",
                "tree_dirty": dirty,
                "package_version": str(build.get("version") or ""),
                "frozen": bool(build.get("frozen", False)),
                "python": platform.python_version(),
                "platform": platform.platform(terse=True),
                "safe_env": env,
            }.items()
        )
    )


def software_fingerprint(repo_root: Path | str | None = None) -> dict[str, Any]:
    """Read-only identity of the code that is running this process."""
    return dict(_fingerprint_cached(str(repo_root or REPO_ROOT)))


def reset_cache() -> None:
    """Test hook: forget the cached fingerprint."""
    _fingerprint_cached.cache_clear()


def python_runtime() -> dict[str, str]:
    return {
        "implementation": platform.python_implementation(),
        "version": platform.python_version(),
        "executable_kind": "frozen" if getattr(sys, "frozen", False) else "source",
    }
