"""CLI tests: the complete uninstall flow, driven exactly as a user runs it.

Every test that can delete anything runs inside a throw-away fake machine
(``make_world``) whose drive roots, per-user folders and tool home all point
into ``tmp_path`` — the real machine is never touched.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.uninstaller_fixtures import fake_platform, make_world
from uninstaller.cli import main
from uninstaller.constants import CONFIRMATION_PHRASE
from uninstaller.winapi import KnownPaths

#: ``os.geteuid`` does not exist on Windows, so this must not be evaluated
#: inside a decorator argument during collection.
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "arena_uninstall.py"


def run_cli(world, *args, stdin_text: str | None = None, platform=None):
    """Run the CLI in-process against the fake machine."""
    platform = platform or fake_platform(world)
    known = KnownPaths.from_env(world.env)

    class Input:
        def __init__(self, text: str) -> None:
            self.lines = text.splitlines(True)
            self.index = 0

        def readline(self) -> str:
            if self.index >= len(self.lines):
                return ""
            line = self.lines[self.index]
            self.index += 1
            return line

    class Output:
        def __init__(self) -> None:
            self.parts: list[str] = []

        def write(self, text: str) -> None:
            self.parts.append(text)

        def flush(self) -> None:
            pass

        def text(self) -> str:
            return "".join(self.parts)

    out = Output()
    code = main(list(args), stdin=Input(stdin_text or ""), stdout=out,
                platform=platform, known=known)
    return code, out.text()


def scan_args(world) -> list[str]:
    return ["--scan-path", str(world.root), "--no-drives"]


# ---------------------------------------------------------------------------
# Basics
# ---------------------------------------------------------------------------


def test_version_and_categories(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0

    world = make_world(tmp_path / "cats")
    code, out = run_cli(world, "--list-categories")
    assert code == 0
    for category in ("installation", "duplicate-executable",
                     "trading-history", "source-repository", "ambiguous"):
        assert category in out


def test_scan_only_changes_nothing(tmp_path):
    world = make_world(tmp_path)
    before = {p: p.read_bytes() for p in world.root.rglob("*") if p.is_file()}
    code, out = run_cli(world, "--mode", "deep", *scan_args(world), "--scan-only")
    after = {p: p.read_bytes() for p in world.root.rglob("*") if p.is_file()}
    assert code == 0
    assert before == after
    assert "installation" in out
    assert "trading-history" in out
    assert "setup-installer" in out
    assert "reason:" in out  # every match shows why


def test_inventory_lists_path_size_category_and_reason(tmp_path):
    world = make_world(tmp_path)
    plan = tmp_path / "inventory.json"
    code, _ = run_cli(world, "--mode", "deep", *scan_args(world),
                      "--scan-only", "--json", str(plan))
    assert code == 0
    data = json.loads(plan.read_text(encoding="utf-8"))
    assert data["format"] == "arena-uninstall-plan/1"
    assert data["matches"]
    for match in data["matches"]:
        assert {"path", "category", "reasons", "size_bytes", "selected",
                "confidence", "signals"} <= set(match)


def test_false_positives_are_listed_but_never_selected(tmp_path):
    world = make_world(tmp_path)
    plan = tmp_path / "inventory.json"
    run_cli(world, "--mode", "deep", *scan_args(world), "--scan-only",
            "--json", str(plan))
    data = json.loads(plan.read_text(encoding="utf-8"))
    for match in data["matches"]:
        if match["category"] == "ambiguous":
            assert match["selected"] is False
    # The unrelated "Arena Games" folder is detected and rejected.
    amb = [m for m in data["matches"]
           if m["path"].endswith("Arena Games")]
    assert amb and amb[0]["confidence"] == "ambiguous"


# ---------------------------------------------------------------------------
# Confirmation / cancellation
# ---------------------------------------------------------------------------


def test_missing_confirmation_deletes_nothing(tmp_path):
    world = make_world(tmp_path)
    code, out = run_cli(world, "--mode", "deep", *scan_args(world))
    assert code == 3
    assert "Refusing to delete" in out
    assert (world.install_root / "Arena.exe").exists()


def test_wrong_confirmation_phrase_deletes_nothing(tmp_path):
    world = make_world(tmp_path)
    code, _ = run_cli(world, "--mode", "deep", *scan_args(world),
                      "--confirm-delete", "yes")
    assert code == 3
    assert (world.install_root / "Arena.exe").exists()


def test_interactive_cancel_at_review(tmp_path):
    world = make_world(tmp_path)
    code, out = run_cli(world, "--mode", "deep", *scan_args(world),
                        "--interactive", stdin_text="q\n")
    assert code == 3
    assert "nothing has been changed" in out.lower()
    assert (world.install_root / "Arena.exe").exists()


def test_interactive_cancel_at_confirmation_prompt(tmp_path):
    world = make_world(tmp_path)
    code, out = run_cli(world, "--mode", "deep", *scan_args(world),
                        "--interactive", stdin_text="p\nno thanks\n")
    assert code == 3
    assert "Cancelled" in out
    assert (world.install_root / "Arena.exe").exists()


def test_dry_run_reports_without_changing_anything(tmp_path):
    world = make_world(tmp_path)
    code, out = run_cli(world, "--mode", "deep", *scan_args(world),
                        "--confirm-delete", CONFIRMATION_PHRASE, "--dry-run")
    assert code == 0
    assert "DRY RUN" in out
    assert (world.install_root / "Arena.exe").exists()
    assert (world.downloads / "AXIOM-Setup-3.2.4.exe").exists()


# ---------------------------------------------------------------------------
# The real flow
# ---------------------------------------------------------------------------


def test_full_deep_clean_quarantines_and_preserves(tmp_path):
    world = make_world(tmp_path)
    report_path = tmp_path / "report.json"
    code, out = run_cli(world, "--mode", "deep", *scan_args(world),
                        "--confirm-delete", CONFIRMATION_PHRASE,
                        "--report", str(report_path))
    assert code == 0, out
    report = json.loads(report_path.read_text(encoding="utf-8"))

    quarantined = [r for r in report["results"] if r["action"] == "quarantined"]
    assert quarantined
    assert not (world.install_root / "Arena.exe").exists()
    assert not (world.downloads / "AXIOM-Setup-3.2.4.exe").exists()

    # Preserved data and unrelated files survive.
    for kept in (
        world.install_root / "trading_bot.db",
        world.install_root / "data" / "history" / "DEMO.csv",
        world.install_root / "logs" / "bot.jsonl",
        world.install_root / ".env",
        world.runtime_home / "trading_bot.db",
        world.runtime_home / "data" / "history" / "AAA.csv",
        world.downloads / "arena-notes.txt",
        world.root / "Games" / "AXIOM-notes.txt",
        world.root / "Games" / "arena_tournament_data.csv",
    ):
        assert kept.exists(), f"{kept} should have been preserved"

    assert report["log_path"]
    assert Path(report["log_path"]).exists()
    assert report["quarantine_dir"]
    manifest = Path(report["quarantine_dir"]) / "manifest.json"
    assert manifest.exists()


def test_restore_after_deep_clean(tmp_path):
    world = make_world(tmp_path)
    run_cli(world, "--mode", "deep", *scan_args(world),
            "--confirm-delete", CONFIRMATION_PHRASE)
    quarantine_dirs = list((world.tool_home / "quarantine").glob("*"))
    assert quarantine_dirs
    code, out = run_cli(world, "--restore", str(quarantine_dirs[0]))
    assert code == 0
    assert "restored" in out
    assert (world.install_root / "Arena.exe").exists()
    assert (world.downloads / "AXIOM-Setup-3.2.4.exe").exists()


def test_plan_then_apply(tmp_path):
    world = make_world(tmp_path)
    plan = tmp_path / "plan.json"
    code, out = run_cli(world, "--mode", "deep", *scan_args(world),
                        "--plan", str(plan))
    assert code == 0 and plan.exists()
    assert "Arena-Uninstall --apply" in out

    # Nothing happens while the plan is only reviewed.
    assert (world.install_root / "Arena.exe").exists()

    code, out = run_cli(world, "--apply", str(plan),
                        "--confirm-delete", CONFIRMATION_PHRASE)
    assert code == 0, out
    assert not (world.install_root / "Arena.exe").exists()


def test_apply_plan_still_refuses_preserved_categories(tmp_path):
    world = make_world(tmp_path)
    plan = tmp_path / "plan.json"
    run_cli(world, "--mode", "deep", *scan_args(world), "--plan", str(plan))
    data = json.loads(plan.read_text(encoding="utf-8"))
    # A hand-edited plan marks the trading database for deletion.
    for match in data["matches"]:
        if match["category"] == "trading-history":
            match["selected"] = True
    plan.write_text(json.dumps(data), encoding="utf-8")

    code, _ = run_cli(world, "--apply", str(plan),
                      "--confirm-delete", CONFIRMATION_PHRASE)
    assert code in (0, 3)
    dbs = list(world.install_root.glob("trading_bot.db"))
    assert dbs, "a hand-edited plan must not delete a trading database"


def test_explicit_include_flag_allows_log_deletion(tmp_path):
    world = make_world(tmp_path)
    log_file = world.install_root / "logs" / "bot.jsonl"
    assert log_file.exists()
    code, out = run_cli(world, "--mode", "deep", *scan_args(world),
                        "--include-logs",
                        "--select-category", "logs",
                        "--confirm-delete", CONFIRMATION_PHRASE)
    assert code == 0, out
    assert not log_file.exists()
    # Trading history is still preserved: each category is a separate opt-in.
    assert (world.install_root / "trading_bot.db").exists()


def test_deselect_category_keeps_it(tmp_path):
    world = make_world(tmp_path)
    setup = world.downloads / "AXIOM-Setup-3.2.4.exe"
    code, _ = run_cli(world, "--mode", "deep", *scan_args(world),
                      "--deselect-category", "setup-installer",
                      "--confirm-delete", CONFIRMATION_PHRASE)
    assert code == 0
    assert setup.exists()
    assert not (world.install_root / "Arena.exe").exists()


def test_permanent_deletion_is_reported_loudly(tmp_path):
    world = make_world(tmp_path)
    report_path = tmp_path / "report.json"
    code, out = run_cli(world, "--mode", "deep", *scan_args(world),
                        "--permanent", "--confirm-delete", CONFIRMATION_PHRASE,
                        "--report", str(report_path))
    assert code == 0
    assert "PERMANENTLY DELETED" in out
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert any(r["action"] == "deleted" for r in report["results"])
    assert not list((world.tool_home / "quarantine").glob("*"))


def test_no_secret_reaches_log_or_report(tmp_path):
    world = make_world(tmp_path)
    report_path = tmp_path / "report.json"
    run_cli(world, "--mode", "deep", *scan_args(world),
            "--confirm-delete", CONFIRMATION_PHRASE, "--report", str(report_path))
    texts = [report_path.read_text(encoding="utf-8")]
    texts += [p.read_text(encoding="utf-8")
              for p in (world.tool_home / "logs").glob("*.log")]
    texts += [p.read_text(encoding="utf-8")
              for p in (world.tool_home / "quarantine").rglob("manifest.json")]
    joined = "\n".join(texts)
    for secret in ("SECRET-TOKEN-VALUE", "live-secret-123"):
        assert secret not in joined


def test_standard_mode_does_not_walk_the_whole_tree(tmp_path):
    world = make_world(tmp_path)
    code, out = run_cli(world, "--mode", "standard", "--scan-only")
    assert code == 0
    # The setup file in Downloads is a Deep Clean discovery, not a Standard one.
    assert "AXIOM-Setup-3.2.4.exe" not in out
    assert "installation" in out


def test_usage_error_for_bad_plan(tmp_path):
    world = make_world(tmp_path)
    code, out = run_cli(world, "--apply", str(tmp_path / "nope.json"),
                        "--confirm-delete", CONFIRMATION_PHRASE)
    assert code == 2


def test_selected_registration_is_removed(tmp_path):
    from tests.uninstaller_fixtures import fake_platform as _fake
    from uninstaller.winapi import UninstallEntry

    world = make_world(tmp_path)
    entry = UninstallEntry(
        hive="HKCU",
        key_path=(r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
                  r"\AXIOM Arena_is1"),
        display_name="AXIOM Arena", publisher="Thedivinetwilight",
        install_location=str(world.install_root))
    platform = _fake(world, entries=[entry])
    known = KnownPaths.from_env(world.env)
    from uninstaller.cli import main as cli_main

    class Input:
        def readline(self):
            return ""

    class Output:
        def __init__(self):
            self.parts = []

        def write(self, text):
            self.parts.append(text)

        def flush(self):
            pass

    out = Output()
    code = cli_main(["--mode", "standard", "--confirm-delete",
                     CONFIRMATION_PHRASE, "--permanent"],
                    stdin=Input(), stdout=out, platform=platform, known=known)
    assert code == 0, out.parts
    assert platform.registry.removed, "the registration should have been removed"
    assert "registry-removed" in "".join(out.parts)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission model")
@pytest.mark.skipif(IS_ROOT, reason="root ignores permissions")
def test_elevation_is_offered_only_when_the_scan_was_refused(tmp_path,
                                                             monkeypatch):
    from tests.uninstaller_fixtures import make_fake_install

    world = make_world(tmp_path)
    blocked = world.root / "blocked" / "AXIOM"
    make_fake_install(blocked)
    platform = fake_platform(world)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        platform, "relaunch_elevated",
        lambda argv: (calls.append(list(argv)), (True, "relaunched elevated"))[1])

    os.chmod(blocked, 0o000)
    try:
        code, out = run_cli(world, "--mode", "deep", *scan_args(world),
                            "--interactive", stdin_text="y\n", platform=platform)
    finally:
        os.chmod(blocked, 0o700)

    assert calls, "elevation was needed but never offered"
    assert code == 0
    assert "relaunched elevated" in out


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission model")
@pytest.mark.skipif(IS_ROOT, reason="root ignores permissions")
def test_no_elevation_flag_never_relaunches(tmp_path, monkeypatch):
    from tests.uninstaller_fixtures import make_fake_install

    world = make_world(tmp_path)
    blocked = world.root / "blocked" / "AXIOM"
    make_fake_install(blocked)
    platform = fake_platform(world)
    calls: list[list[str]] = []
    monkeypatch.setattr(platform, "relaunch_elevated",
                        lambda argv: (calls.append(list(argv)), (False, "x"))[1])
    os.chmod(blocked, 0o000)
    try:
        code, _ = run_cli(world, "--mode", "deep", *scan_args(world),
                          "--interactive", "--no-elevation", stdin_text="q\n",
                          platform=platform)
    finally:
        os.chmod(blocked, 0o700)
    assert calls == []
    assert code == 3


# ---------------------------------------------------------------------------
# Structural guarantees
# ---------------------------------------------------------------------------


def test_cli_imports_nothing_from_the_application():
    code = (
        "import sys, uninstaller.cli;"
        "bad=[m for m in sys.modules if m=='app' or m.startswith('app.')"
        " or m=='desktop' or m.startswith('desktop.')];"
        "print(','.join(sorted(bad)))"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "", f"unexpected application imports: {out.stdout}"


def test_entry_point_runs_without_site_packages():
    """The uninstaller is standard-library only — proven by ``python -S``."""
    out = subprocess.run([sys.executable, "-S", str(ENTRY), "--version"],
                         cwd=str(ROOT), capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "Arena-Uninstall" in out.stdout


def test_entry_point_works_without_arena_installed(tmp_path):
    """A boxed copy of the tool runs from a directory with no Arena at all."""
    import shutil
    box = tmp_path / "box"
    box.mkdir()
    shutil.copytree(ROOT / "uninstaller", box / "uninstaller")
    shutil.copy2(ENTRY, box / "arena_uninstall.py")
    world = make_world(tmp_path / "machine")
    env = dict(os.environ)
    env.update(world.env)
    env["PYTHONPATH"] = str(box)
    out = subprocess.run([sys.executable, str(box / "arena_uninstall.py"),
                          "--mode", "deep", "--scan-path", str(world.root),
                          "--no-drives", "--scan-only"],
                         cwd=str(box), capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stderr
    assert "installation" in out.stdout
