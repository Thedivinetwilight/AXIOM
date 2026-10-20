"""Tests of the PACKAGED Arena-Uninstall executable.

Two layers:

* always-run structural checks — the PyInstaller spec parses, the version
  resource carries the identity string the classifier relies on, and the
  build entry points exist;
* the real end-to-end run — against an actual built artifact, found via
  ``$ARENA_UNINSTALL_EXE`` or ``dist/Arena-Uninstall(.exe)``. It is skipped
  (not silently passed) when no artifact exists, because claiming a Windows
  binary was verified without running one would be a lie.

CI builds the Windows executable and then runs this module with
``ARENA_UNINSTALL_EXE`` set — see ``.github/workflows/arena-uninstaller-windows.yml``.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
E2E = ROOT / "packaging" / "uninstaller_e2e.py"
SPEC = ROOT / "packaging" / "uninstaller.spec"
ENV_VAR = "ARENA_UNINSTALL_EXE"


def artifact_path() -> Path | None:
    explicit = os.environ.get(ENV_VAR)
    if explicit:
        path = Path(explicit)
        return path if path.is_file() else None
    for name in ("Arena-Uninstall.exe", "Arena-Uninstall"):
        candidate = ROOT / "dist" / name
        if candidate.is_file():
            return candidate
    return None


def _load_e2e():
    spec = importlib.util.spec_from_file_location("arena_uninstaller_e2e", E2E)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Structural checks (always run)
# ---------------------------------------------------------------------------


def test_spec_is_valid_python_and_targets_the_entry_point():
    source = SPEC.read_text(encoding="utf-8")
    compile(source, str(SPEC), "exec")
    assert "arena_uninstall.py" in source
    assert "Arena-Uninstall" in source
    # The uninstaller must never bundle the application itself.
    assert '"app"' in source


def test_version_resource_carries_the_identity_the_classifier_uses():
    sys.path.insert(0, str(ROOT))
    try:
        import build_uninstaller
    finally:
        sys.path.pop(0)
    from uninstaller.constants import PUBLISHER, TOOL_PRODUCT_NAME
    text = build_uninstaller.version_resource_text("3.2.4")
    assert f"'{TOOL_PRODUCT_NAME}'" in text
    assert f"'{PUBLISHER}'" in text
    compile(text.split("\n", 1)[1], "<version-resource>", "exec")


def test_build_entry_point_exists_and_has_a_test_mode():
    script = (ROOT / "build_uninstaller.py").read_text(encoding="utf-8")
    assert "--test" in script
    assert "uninstaller_e2e" in script


def test_e2e_module_is_importable_without_the_application():
    module = _load_e2e()
    assert hasattr(module, "run_uninstaller_e2e")


def test_uninstaller_version_tracks_the_product_version():
    sys.path.insert(0, str(ROOT))
    try:
        from app import __version__ as app_version
        from uninstaller import __version__ as tool_version
    finally:
        sys.path.pop(0)
    assert tool_version == app_version, (
        "the uninstaller is distributed with the product; bump both versions")


# ---------------------------------------------------------------------------
# Real artifact
# ---------------------------------------------------------------------------


def test_packaged_uninstaller_runs_end_to_end(tmp_path):
    artifact = artifact_path()
    if artifact is None:
        pytest.skip(
            f"no packaged uninstaller found (set {ENV_VAR} or build one with "
            "`python build_uninstaller.py`)")
    module = _load_e2e()
    result = module.run_uninstaller_e2e(artifact, tmp_path / "e2e")
    failures = [r for r in result["results"] if not r["ok"]]
    detail = "\n".join(f"- {r['name']}: {r['detail']}" for r in failures)
    assert result["ok"], f"packaged uninstaller failed:\n{detail}"


def test_packaged_uninstaller_is_self_contained(tmp_path):
    artifact = artifact_path()
    if artifact is None:
        pytest.skip(f"no packaged uninstaller found (set {ENV_VAR})")
    env = {
        key: value for key, value in os.environ.items()
        if key.upper() not in {"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"}
    }
    proc = subprocess.run([str(artifact), "--version"], cwd=str(tmp_path),
                          env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert "Arena-Uninstall" in proc.stdout
    # Runs from a directory with no Arena checkout, no app package, nothing.
    assert not (tmp_path / "app").exists()


def test_packaged_uninstaller_reports_categories(tmp_path):
    artifact = artifact_path()
    if artifact is None:
        pytest.skip(f"no packaged uninstaller found (set {ENV_VAR})")
    proc = subprocess.run([str(artifact), "--list-categories"],
                          cwd=str(tmp_path), capture_output=True, text=True,
                          timeout=120)
    assert proc.returncode == 0
    for category in ("trading-history", "logs", "configuration", "credentials",
                     "source-repository", "development", "research"):
        assert category in proc.stdout
