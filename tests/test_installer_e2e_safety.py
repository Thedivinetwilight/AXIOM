"""Safety guards for the destructive Windows installer integration harness."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "axiom_installer_e2e_safety", ROOT / "packaging" / "installer_e2e.py")
installer_e2e = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(installer_e2e)


@pytest.mark.parametrize("path", [ROOT, ROOT / "tests" / "new-e2e-workdir",
                                   ROOT.parent])
def test_workdir_rejects_repository_and_ancestor_paths(path):
    with pytest.raises(ValueError):
        installer_e2e._prepare_workdir(path)


def test_workdir_rejects_preexisting_path_without_deleting_caller_data(tmp_path):
    existing = tmp_path / "caller-data"
    existing.mkdir()
    sentinel = existing / "keep.txt"
    sentinel.write_text("preserve this", encoding="utf-8")

    with pytest.raises(FileExistsError):
        installer_e2e._prepare_workdir(existing)

    assert sentinel.read_text(encoding="utf-8") == "preserve this"


def test_workdir_rejects_symlink_without_touching_target(tmp_path):
    target = tmp_path / "caller-data"
    target.mkdir()
    sentinel = target / "keep.txt"
    sentinel.write_text("preserve this", encoding="utf-8")
    alias = tmp_path / "workdir-link"
    try:
        alias.symlink_to(target, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    with pytest.raises(ValueError):
        installer_e2e._prepare_workdir(alias)

    assert sentinel.read_text(encoding="utf-8") == "preserve this"


def test_workdir_is_created_only_as_a_fresh_child_of_existing_temp_parent(tmp_path):
    fresh = tmp_path / "installer-e2e-run"

    result = installer_e2e._prepare_workdir(fresh)

    assert result == fresh.resolve()
    assert result.is_dir()
    assert list(result.iterdir()) == []


def test_workdir_rejects_missing_parent_without_creating_it(tmp_path):
    missing_parent = tmp_path / "not-created"

    with pytest.raises(ValueError):
        installer_e2e._prepare_workdir(missing_parent / "workdir")

    assert not missing_parent.exists()


def test_installer_main_preserves_existing_workdir(monkeypatch, tmp_path):
    installer = tmp_path / "AXIOM-Setup.exe"
    installer.write_bytes(b"test executable placeholder")
    existing = tmp_path / "existing-workdir"
    existing.mkdir()
    sentinel = existing / "user-data.txt"
    sentinel.write_text("must survive", encoding="utf-8")
    monkeypatch.setattr(installer_e2e.sys, "platform", "win32")

    result = installer_e2e.main([str(installer), str(existing)])

    assert result == 2
    assert sentinel.read_text(encoding="utf-8") == "must survive"
