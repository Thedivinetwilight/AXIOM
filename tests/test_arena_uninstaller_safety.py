"""Safety tests: filesystem hazards, preservation, cancellation, confirmation.

Each test here corresponds to a way an uninstaller can destroy something it
should not: following a junction out of its scan root, crashing on a
permission error, dying on a locked file, sweeping up a trading database,
deleting without confirmation, or treating its own running image as a target.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.uninstaller_fixtures import (
    build_pe,
    fake_platform,
    make_fake_install,
    make_world,
)
from uninstaller.constants import (
    CATEGORY_LOGS,
    CATEGORY_TRADING_HISTORY,
    CONFIRMATION_PHRASE,
    PROTECTED_CATEGORIES,
)
from uninstaller.deletion import ExecutionOptions, execute
from uninstaller.engine import RunOptions, ScanRequest, apply_inventory, build_inventory
from uninstaller.model import Inventory, Match
from uninstaller.protect import ProtectionPolicy
from uninstaller.quarantine import Quarantine, restore
from uninstaller.review import confirm_deletion, interactive_review
from uninstaller.scanner import scan_drives
from uninstaller.winapi import KnownPaths


#: ``os.geteuid`` does not exist on Windows, so this must not be evaluated
#: inside a decorator argument during collection.
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


def inventory_for(world, mode="deep"):
    platform = fake_platform(world)
    return platform, build_inventory(
        platform, KnownPaths.from_env(world.env),
        ScanRequest(mode=mode, extra_roots=[world.root], include_drives=False),
        "test")


# ---------------------------------------------------------------------------
# Reparse points: junctions and symlinks are never followed
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink variant")
def test_symlinked_directory_is_not_followed(tmp_path):
    world = make_world(tmp_path, false_positives=False)
    outside = tmp_path / "unrelated"
    make_fake_install(outside)
    marker = outside / "Arena.exe"
    link = world.root / "shortcut-to-outside"
    link.symlink_to(outside, target_is_directory=True)

    platform = fake_platform(world)
    inventory = Inventory(mode="deep")
    scan_drives(platform, inventory, [world.root], known_norms=set())

    paths = {m.path for m in inventory.matches}
    assert str(marker) not in paths, "scan followed a symlink into another tree"
    # The link itself may be mentioned, but never as an owned, selected item.
    for match in inventory.matches:
        if match.path == str(link):
            assert not match.selected


@pytest.mark.skipif(os.name != "nt", reason="Windows junction")
def test_windows_junction_is_not_followed(tmp_path):
    import _winapi

    world = make_world(tmp_path, false_positives=False)
    outside = tmp_path / "unrelated"
    make_fake_install(outside)
    link = world.root / "junction-to-outside"
    _winapi.CreateJunction(str(outside), str(link))

    assert fake_platform(world).is_reparse_point(link) is True
    platform = fake_platform(world)
    inventory = Inventory(mode="deep")
    scan_drives(platform, inventory, [world.root], known_norms=set())
    assert str(outside / "Arena.exe") not in {m.path for m in inventory.matches}


def test_reparse_point_detection_covers_symlinks(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - platform
        pytest.skip("symlinks not permitted here")
    platform = fake_platform(make_world(tmp_path / "w"))
    assert platform.is_reparse_point(link) is True
    assert platform.is_reparse_point(target) is False


# ---------------------------------------------------------------------------
# Permission failures are reported, never fatal
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission model")
@pytest.mark.skipif(IS_ROOT, reason="root ignores permissions")
def test_unreadable_directory_is_reported_and_scan_continues(tmp_path):
    world = make_world(tmp_path)
    locked = world.root / "locked-area" / "AXIOM"
    make_fake_install(locked)
    os.chmod(locked, 0o000)
    try:
        platform, inventory = inventory_for(world)
    finally:
        os.chmod(locked, 0o700)

    kinds = {issue.kind for issue in inventory.issues}
    assert "access-denied" in kinds
    assert inventory.needs_elevation is True
    # The rest of the scan still produced results.
    assert any(m.category for m in inventory.matches)


def test_scan_error_on_one_entry_does_not_abort_the_walk(tmp_path, monkeypatch):
    world = make_world(tmp_path)
    platform = fake_platform(world)
    real_scandir = os.scandir
    calls = {"n": 0}

    def flaky(path):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("simulated disk hiccup")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", flaky)
    inventory = Inventory(mode="deep")
    scan_drives(platform, inventory, [world.root], known_norms=set())
    monkeypatch.setattr(os, "scandir", real_scandir)

    assert any(i.detail.startswith("OSError") for i in inventory.issues)
    # Other scan roots/entries still produced data.
    assert inventory.matches


# ---------------------------------------------------------------------------
# Locked files
# ---------------------------------------------------------------------------


def test_quarantine_failure_is_reported_not_raised(tmp_path, monkeypatch):
    from uninstaller import quarantine as quarantine_mod

    world = make_world(tmp_path)
    target = world.install_root / "Arena.exe"
    match = Match(path=str(target), category="leftover", selected=True,
                  size_bytes=target.stat().st_size)

    def locked_move(*args, **kwargs):
        raise PermissionError(13, "The process cannot access the file because "
                                  "it is being used by another process")

    monkeypatch.setattr(quarantine_mod.shutil, "move", locked_move)
    report = execute(Inventory(mode="deep", matches=[match]),
                     ProtectionPolicy(), fake_platform(world),
                     Quarantine(root=world.tool_home / "quarantine"),
                     ExecutionOptions())
    assert report.failures and report.failures[0].path == str(target)
    assert "PermissionError" in report.failures[0].detail
    assert target.exists(), "a locked file must be left where it is"


def test_permanent_delete_failure_is_reported(tmp_path, monkeypatch):
    world = make_world(tmp_path)
    target = world.install_root / "Arena.exe"
    match = Match(path=str(target), category="leftover", selected=True,
                  size_bytes=target.stat().st_size)

    def locked_unlink(self, *args, **kwargs):
        raise PermissionError(13, "access is denied")

    monkeypatch.setattr(Path, "unlink", locked_unlink)
    report = execute(Inventory(mode="deep", matches=[match]),
                     ProtectionPolicy(), fake_platform(world), None,
                     ExecutionOptions(permanent=True))
    assert report.failures and "locked" in report.failures[0].detail


# ---------------------------------------------------------------------------
# Protected data is preserved by default and only removed on explicit opt-in
# ---------------------------------------------------------------------------


def test_default_run_preserves_every_protected_category(tmp_path):
    world = make_world(tmp_path)
    platform, inventory = inventory_for(world)
    protected_paths = {m.path: m.category for m in inventory.matches
                       if m.category in PROTECTED_CATEGORIES}
    assert protected_paths, "fixture should contain preserved data"
    assert not [m for m in inventory.matches
                if m.category in PROTECTED_CATEGORIES and m.selected]

    report, _log = apply_inventory(inventory, ProtectionPolicy(), platform,
                                   world.tool_home, RunOptions())
    assert report.failures == []
    for path, category in protected_paths.items():
        if category == CATEGORY_LOGS:
            assert Path(path).exists(), f"log data was removed: {path}"
        assert Path(path).exists(), f"preserved {category} was removed: {path}"


def test_protected_selection_is_force_deselected_by_policy(tmp_path):
    world = make_world(tmp_path)
    platform, inventory = inventory_for(world)
    db = next(m for m in inventory.matches
              if m.category == CATEGORY_TRADING_HISTORY and m.path.endswith(".db"))
    db.selected = True  # a stale plan / hand edit tries to select it
    report, _log = apply_inventory(inventory, ProtectionPolicy(), platform,
                                   world.tool_home, RunOptions())
    assert db.selected is False
    assert Path(db.path).exists()
    assert not any(r.path == db.path for r in report.results)


def test_explicit_opt_in_allows_deleting_protected_data(tmp_path):
    world = make_world(tmp_path)
    platform, inventory = inventory_for(world)
    db = next(m for m in inventory.matches
              if m.category == CATEGORY_TRADING_HISTORY and m.path.endswith(".db"))
    policy = ProtectionPolicy({CATEGORY_TRADING_HISTORY})
    db.selected = True
    report, _log = apply_inventory(inventory, policy, platform,
                                   world.tool_home, RunOptions())
    assert any(r.path == db.path for r in report.results)
    assert not Path(db.path).exists()


def test_runtime_home_and_install_tree_are_not_removable_as_units(tmp_path):
    world = make_world(tmp_path)
    platform, inventory = inventory_for(world)
    home = next(m for m in inventory.matches if m.path == str(world.runtime_home))
    install = next(m for m in inventory.matches if m.path == str(world.install_root))
    for match in (home, install):
        assert match.selectable is False
        assert match.selected is False
        assert any("not removable as a unit" in reason for reason in match.reasons)


def test_directory_with_unselected_preserved_children_is_kept(tmp_path):
    world = make_world(tmp_path)
    platform, inventory = inventory_for(world)
    root = next(m for m in inventory.matches if m.path == str(world.install_root))
    root.selected = True
    root.selectable = True  # force the situation the guard exists for
    quarantined_db = next(m for m in inventory.matches
                          if m.path.endswith("trading_bot.db"))
    quarantined_db.selected = False
    report = execute(inventory, ProtectionPolicy(), platform,
                     Quarantine(root=world.tool_home / "q"), ExecutionOptions())
    assert (world.install_root / "trading_bot.db").exists()
    kept = [r for r in report.results if r.path == str(world.install_root)]
    assert kept and kept[0].action == "skipped"


# ---------------------------------------------------------------------------
# Cancellation and the confirmation gate
# ---------------------------------------------------------------------------


class _ScriptedInput:
    def __init__(self, text: str) -> None:
        self._lines = text.splitlines(True)
        self._index = 0

    def readline(self) -> str:
        if self._index >= len(self._lines):
            return ""
        line = self._lines[self._index]
        self._index += 1
        return line

    def write(self, text: str) -> None:  # pragma: no cover - only for stdout
        pass

    def flush(self) -> None:
        pass


def test_quit_at_review_leaves_everything_untouched(tmp_path):
    world = make_world(tmp_path)
    platform, inventory = inventory_for(world)
    before = sorted(p for p in world.root.rglob("*") if p.is_file())
    outcome = interactive_review(inventory, ProtectionPolicy(),
                                 _ScriptedInput("q\n"), _ScriptedInput(""))
    after = sorted(p for p in world.root.rglob("*") if p.is_file())
    assert outcome == "cancel"
    assert before == after


def test_confirmation_requires_the_exact_phrase():
    out = _ScriptedInput("")
    assert confirm_deletion(_ScriptedInput("delete\n"), out) is False
    assert confirm_deletion(_ScriptedInput("yes\n"), out) is False
    assert confirm_deletion(_ScriptedInput("\n"), out) is False
    assert confirm_deletion(_ScriptedInput(f"{CONFIRMATION_PHRASE}\n"), out) is True


def test_review_can_deselect_individual_items(tmp_path):
    world = make_world(tmp_path)
    platform, inventory = inventory_for(world)
    selected = [i for i, m in enumerate(inventory.matches, start=1) if m.selected]
    assert selected
    target_index = selected[0]
    interactive_review(inventory, ProtectionPolicy(),
                       _ScriptedInput(f"d {target_index}\np\n"), _ScriptedInput(""))
    assert inventory.matches[target_index - 1].selected is False


def test_review_refuses_to_select_preserved_items_until_enabled(tmp_path):
    world = make_world(tmp_path)
    platform, inventory = inventory_for(world)
    index = next(i for i, m in enumerate(inventory.matches, start=1)
                 if m.category in PROTECTED_CATEGORIES)
    policy = ProtectionPolicy()
    interactive_review(inventory, policy, _ScriptedInput(f"s {index}\nq\n"),
                       _ScriptedInput(""))
    assert inventory.matches[index - 1].selected is False


def test_review_protect_on_enables_preserved_categories(tmp_path):
    world = make_world(tmp_path)
    platform, inventory = inventory_for(world)
    index = next(i for i, m in enumerate(inventory.matches, start=1)
                 if m.category in PROTECTED_CATEGORIES)
    policy = ProtectionPolicy()
    interactive_review(inventory, policy,
                       _ScriptedInput(f"protect on\ns {index}\nq\n"),
                       _ScriptedInput(""))
    assert index and policy.enabled == set(PROTECTED_CATEGORIES)


# ---------------------------------------------------------------------------
# The uninstaller's own files
# ---------------------------------------------------------------------------


class _SelfPlatform:
    """Platform whose self_paths includes the target we are deleting."""

    def __init__(self, self_path: Path) -> None:
        from uninstaller.winapi import Platform
        self._platform = Platform(registry=None, environ={}, force_admin=False)
        self.self_path = self_path
        self.deferred: list[str] = []

    def is_self_path(self, path) -> bool:
        return str(Path(path)) == str(self.self_path)

    def defer_delete(self, path):
        self.deferred.append(str(path))
        return True, "scheduled for deletion at next reboot"

    def __getattr__(self, item):
        return getattr(self._platform, item)


def test_running_image_is_never_deleted_directly(tmp_path):
    world = make_world(tmp_path)
    tool = build_pe(world.downloads / "Arena-Uninstall.exe",
                    product="AXIOM Arena Uninstall", internal="Arena-Uninstall",
                    original="Arena-Uninstall.exe")
    platform = _SelfPlatform(tool)
    match = Match(path=str(tool), category="uninstall-tool", selected=True,
                  size_bytes=tool.stat().st_size)
    report = execute(Inventory(mode="deep", matches=[match]),
                     ProtectionPolicy(), platform, None,
                     ExecutionOptions(permanent=True))
    assert platform.deferred == [str(tool)]
    assert report.results[0].action == "deferred"
    assert tool.exists()


def test_self_files_are_marked_unselectable_in_the_inventory(tmp_path, monkeypatch):
    world = make_world(tmp_path)
    tool = build_pe(world.downloads / "Arena-Uninstall.exe",
                    product="AXIOM Arena Uninstall", internal="Arena-Uninstall",
                    original="Arena-Uninstall.exe")
    platform = fake_platform(world)
    monkeypatch.setattr(platform, "self_paths", lambda: [str(tool.resolve())])
    inventory = build_inventory(
        platform, KnownPaths.from_env(world.env),
        ScanRequest(mode="deep", extra_roots=[world.root], include_drives=False),
        "test")
    match = next(m for m in inventory.matches if m.path == str(tool))
    assert match.selectable is False
    assert match.selected is False
    assert any("running uninstaller" in r for r in match.reasons)


def test_tool_metadata_is_recognised(tmp_path):
    from uninstaller import signals
    tool = build_pe(tmp_path / "Arena-Uninstall.exe",
                    product="AXIOM Arena Uninstall", internal="Arena-Uninstall",
                    original="Arena-Uninstall.exe")
    ok, detail = signals.tool_metadata_signal(tool)
    assert ok and "uninstall tool" in detail


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def test_log_never_contains_credentials(tmp_path):
    world = make_world(tmp_path)
    platform, inventory = inventory_for(world)
    apply_inventory(inventory, ProtectionPolicy(), platform, world.tool_home,
                    RunOptions())
    logs = list((world.tool_home / "logs").glob("*.log"))
    assert logs, "a cleanup log must always be written"
    text = "\n".join(path.read_text(encoding="utf-8") for path in logs)
    assert "SECRET-TOKEN-VALUE" not in text
    assert "live-secret-123" not in text
    assert str(world.install_root) in text  # paths survive redaction


def test_redaction_keeps_paths_and_hides_tokens():
    from uninstaller.redact import redact
    path = r"C:\Users\me\AppData\Local\Arena\trading_bot.db"
    assert redact(path) == path
    assert "hunter2" not in redact("UPSTOX_API_KEY=hunter2")
    assert "eyJhbGciOi" not in redact("Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOjF9.abcd")
    assert "ABCDEFGHIJKLMNOP" not in redact("AKIAABCDEFGHIJKLMNOP")


# ---------------------------------------------------------------------------
# Quarantine restore
# ---------------------------------------------------------------------------


def test_quarantine_round_trip_restores_exact_paths(tmp_path):
    world = make_world(tmp_path)
    target = world.downloads / "AXIOM-Setup-3.2.4.exe"
    original = target.read_bytes()
    q = Quarantine(root=world.tool_home / "quarantine")
    match = Match(path=str(target), category="setup-installer", selected=True,
                  size_bytes=target.stat().st_size)
    result = q.move(match)
    assert result.ok and not target.exists()
    q.finalize()

    rows = restore(q.directory)
    assert rows and all(status == "restored" for _, status, _ in rows)
    assert target.exists() and target.read_bytes() == original


def test_restore_never_overwrites_new_data(tmp_path):
    world = make_world(tmp_path)
    target = world.downloads / "AXIOM-Setup-3.2.4.exe"
    q = Quarantine(root=world.tool_home / "quarantine")
    q.move(Match(path=str(target), category="setup-installer"))
    q.finalize()
    target.write_text("a newer file", encoding="utf-8")
    rows = restore(q.directory)
    assert any(status == "kept" for _, status, _ in rows)
    assert target.read_text(encoding="utf-8") == "a newer file"


# ---------------------------------------------------------------------------
# Elevation is an offer, never automatic
# ---------------------------------------------------------------------------


def test_elevation_only_flagged_when_access_was_denied(tmp_path, monkeypatch):
    world = make_world(tmp_path)
    platform, inventory = inventory_for(world)
    assert inventory.needs_elevation is False

    real_scandir = os.scandir

    def deny(path):
        if str(path).endswith("Downloads"):
            raise PermissionError("Access is denied")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", deny)
    inventory = Inventory(mode="deep")
    scan_drives(platform, inventory, [world.root], known_norms=set())
    monkeypatch.setattr(os, "scandir", real_scandir)
    assert inventory.needs_elevation is True


def test_platform_admin_override_is_respected(tmp_path):
    world = make_world(tmp_path)
    assert fake_platform(world, admin=True).is_admin() is True
    assert fake_platform(world, admin=False).is_admin() is False


# ---------------------------------------------------------------------------
# Source repositories: preserved by default, removable only on explicit opt-in
# ---------------------------------------------------------------------------


def _make_source_checkout(root: Path) -> Path:
    (root / "app").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "arena.py").write_text("# entry\n", encoding="utf-8")
    (root / "app" / "__init__.py").write_text('__title__ = "arena"\n',
                                              encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname='arena'\n",
                                         encoding="utf-8")
    (root / "tests").mkdir()
    return root


def test_source_checkout_is_preserved_and_not_selected(tmp_path):
    world = make_world(tmp_path, install=False, home=False, downloads=False,
                       false_positives=False)
    repo = _make_source_checkout(world.user_profile / "dev" / "AXIOM")
    platform, inventory = inventory_for(world)
    match = next(m for m in inventory.matches if m.path == str(repo))
    assert match.category == "source-repository"
    assert match.selected is False
    assert match.selectable is True

    report, _log = apply_inventory(inventory, ProtectionPolicy(), platform,
                                   world.tool_home, RunOptions())
    assert repo.exists()
    assert not any(r.path == str(repo) for r in report.results)


def test_source_checkout_removed_only_with_explicit_include(tmp_path):
    world = make_world(tmp_path, install=False, home=False, downloads=False,
                       false_positives=False)
    repo = _make_source_checkout(world.user_profile / "dev" / "AXIOM")
    platform, inventory = inventory_for(world)
    match = next(m for m in inventory.matches if m.path == str(repo))
    policy = ProtectionPolicy({"source-repository"})
    match.selected = True
    report, _log = apply_inventory(inventory, policy, platform,
                                   world.tool_home, RunOptions())
    assert not repo.exists()
    assert any(r.path == str(repo) for r in report.results)


@pytest.mark.skipif(os.name != "nt", reason="MoveFileExW is Windows-only")
def test_defer_delete_renames_and_schedules_the_running_image(tmp_path):
    from uninstaller.winapi import Platform

    platform = Platform(registry=None, environ={})
    target = tmp_path / "Arena-Uninstall.exe"
    target.write_bytes(b"MZ" + b"\0" * 64)
    ok, detail = platform.defer_delete(target)
    renamed = tmp_path / "Arena-Uninstall.exe.arena-uninstall-pending"
    assert detail
    if ok:
        assert "scheduled" in detail
        assert not target.exists()
        assert renamed.exists()
    else:  # a non-administrator cannot write the pending-rename list
        assert "failed" in detail
