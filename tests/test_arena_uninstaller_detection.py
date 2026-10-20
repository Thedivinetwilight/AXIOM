"""Detection tests: ownership signals, false positives and duplicates.

These are the tests that keep the uninstaller from becoming a data-loss tool.
They assert both directions:

* genuine Arena artefacts (metadata, registration, known locations, markers)
  are classified as owned;
* look-alikes — a folder called "Arena Games", "AXIOM-notes.txt", an
  unrelated Inno Setup ``unins000.exe``, another vendor's "Arena Tool" —
  are NOT, and are never pre-selected.
"""

from __future__ import annotations

import pytest

from tests.uninstaller_fixtures import (
    build_pe,
    build_version_blob,
    make_world,
    fake_platform,
)
from uninstaller import classify, pe, signals
from uninstaller.classify import Evidence, classify_file, classify_tree_root
from uninstaller.constants import (
    CATEGORY_AMBIGUOUS,
    CATEGORY_CONFIGURATION,
    CATEGORY_DUPLICATE,
    CATEGORY_INSTALLATION,
    CATEGORY_LEFTOVER,
    CATEGORY_LOGS,
    CATEGORY_RESEARCH,
    CATEGORY_SETUP,
    CATEGORY_SOURCE_REPO,
    CATEGORY_TRADING_HISTORY,
    CONFIDENCE_AMBIGUOUS,
    CONFIDENCE_CONFIRMED,
    PROTECTED_CATEGORIES,
)
from uninstaller.engine import ScanRequest, build_inventory, dedupe
from uninstaller.winapi import UninstallEntry


# ---------------------------------------------------------------------------
# PE metadata (the strongest file-level evidence)
# ---------------------------------------------------------------------------


def test_version_blob_round_trip():
    blob = build_version_blob({
        "ProductName": "AXIOM Arena",
        "CompanyName": "Thedivinetwilight",
        "InternalName": "Arena",
        "OriginalFilename": "Arena.exe",
    })
    parsed = pe.parse_version_info(blob)
    assert parsed["ProductName"] == "AXIOM Arena"
    assert parsed["CompanyName"] == "Thedivinetwilight"
    assert parsed["InternalName"] == "Arena"
    assert parsed["FileVersion"]


def test_pe_metadata_read_from_a_real_image(tmp_path):
    target = build_pe(tmp_path / "Arena.exe")
    meta = pe.product_strings(target)
    assert meta["product"] == "AXIOM Arena"
    assert meta["company"] == "Thedivinetwilight"
    assert meta["internal"] == "Arena"


def test_garbage_and_truncated_files_report_no_metadata(tmp_path):
    garbage = tmp_path / "garbage.exe"
    garbage.write_bytes(b"not a PE at all")
    truncated = tmp_path / "truncated.exe"
    truncated.write_bytes(b"MZ" + b"\0" * 40)
    broken = tmp_path / "broken.exe"
    broken.write_bytes(build_pe(tmp_path / "ok.exe").read_bytes()[:300])
    for path in (garbage, truncated, broken, tmp_path / "missing.exe"):
        assert pe.read_version_info(path) == {}


def test_metadata_signal_requires_product_and_publisher(tmp_path):
    ours = build_pe(tmp_path / "Arena.exe")
    other_vendor = build_pe(tmp_path / "ArenaTool.exe", product="Arena Tool",
                            company="Arena Inc", internal="ArenaTool",
                            original="ArenaTool.exe")
    assert signals.metadata_signal(ours)[0] is True
    ok, detail, _ = signals.metadata_signal(other_vendor)
    assert ok is False
    assert "Arena Tool" in detail


def test_name_signal_alone_is_never_enough(tmp_path):
    fake = tmp_path / "AXIOM.exe"
    fake.write_bytes(b"just a text file with an executable name")
    evidence = Evidence()
    ok, detail = signals.name_signal(fake)
    assert ok and "AXIOM.exe" in detail
    evidence.filename = detail
    match = classify_file(fake, evidence)
    assert match.category == CATEGORY_AMBIGUOUS
    assert match.confidence == CONFIDENCE_AMBIGUOUS
    assert match.selected is False


def test_uninstaller_leftover_name_alone_is_ambiguous(tmp_path):
    other_inno = build_pe(tmp_path / "unins000.exe",
                          product="Notepad Replacement",
                          company="Some Other Vendor",
                          internal="unins000", original="unins000.exe")
    evidence = Evidence()
    evidence.filename = signals.name_signal(other_inno)[1]
    match = classify_file(other_inno, evidence)
    assert match.category == CATEGORY_AMBIGUOUS
    assert not match.selected


# ---------------------------------------------------------------------------
# Classification of files
# ---------------------------------------------------------------------------


def test_confirmed_metadata_classifies_as_installation(tmp_path):
    exe = build_pe(tmp_path / "Arena.exe")
    evidence = Evidence()
    ok, detail, _ = signals.metadata_signal(exe)
    evidence.metadata = detail
    match = classify_file(exe, evidence)
    assert match.category == CATEGORY_INSTALLATION
    assert match.confidence == CONFIDENCE_CONFIRMED
    assert match.selected is True


def test_setup_package_with_metadata_is_recognised(tmp_path):
    setup = build_pe(tmp_path / "AXIOM-Setup-3.2.4.exe",
                     internal="AXIOM-Setup", original="AXIOM-Setup-3.2.4.exe")
    evidence = Evidence()
    evidence.metadata = signals.metadata_signal(setup)[1]
    evidence.filename = signals.name_signal(setup)[1]
    match = classify_file(setup, evidence)
    assert match.category == CATEGORY_SETUP


def test_known_path_plus_filename_is_confirmed(tmp_path):
    known = tmp_path / "Program Files" / "AXIOM"
    known.mkdir(parents=True)
    exe = known / "unins000.exe"
    exe.write_bytes(b"inno uninstaller")
    evidence = Evidence()
    evidence.known_path = "known location"
    evidence.filename = "installer leftover name 'unins000.exe'"
    match = classify_file(exe, evidence)
    assert match.category == CATEGORY_LEFTOVER
    assert match.confidence == CONFIDENCE_CONFIRMED


def test_standalone_marker_file_is_likely_and_protected(tmp_path):
    override = tmp_path / "D-drive" / "paper-database-override.json"
    override.parent.mkdir(parents=True)
    override.write_text('{"database": "D:/trading/db.sqlite"}',
                        encoding="utf-8")
    evidence = Evidence()
    evidence.marker = "paper-database-override.json (Arena home marker)"
    match = classify_file(override, evidence)
    assert match.category == CATEGORY_CONFIGURATION
    assert match.protected
    assert match.selected is False


@pytest.mark.parametrize("name,content", [
    ("AXIOM-notes.txt", "notes about the axiom"),
    ("Arena Games/readme.txt", "unrelated game"),
    ("arena_tournament_data.csv", "a,b\n1,2\n"),
    ("AXIOM_backup_2024.zip", "not really"),
])
def test_paths_containing_product_tokens_are_not_owned(tmp_path, name, content):
    target = tmp_path / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    evidence = Evidence()
    ok, detail = signals.weak_token_signal(target)
    if ok:
        evidence.token = detail
    match = classify_file(target, evidence)
    assert match.category == CATEGORY_AMBIGUOUS
    assert match.selected is False
    assert match.selectable is True  # visible and individually selectable


def test_tree_root_of_an_unrelated_folder_is_ambiguous(tmp_path):
    root = tmp_path / "Arena Games"
    root.mkdir()
    (root / "readme.txt").write_text("game", encoding="utf-8")
    evidence = Evidence()
    evidence.token = "directory name matches a product token"
    category, confidence, _ = classify_tree_root(root, known_install=False,
                                                 evidence=evidence)
    assert category == CATEGORY_AMBIGUOUS
    assert confidence == CONFIDENCE_AMBIGUOUS


def test_source_checkout_is_a_preserved_source_repository(tmp_path):
    repo = tmp_path / "AXIOM"
    (repo / "app").mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / "arena.py").write_text("print('arena')\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text("[project]\nname='arena'\n",
                                         encoding="utf-8")
    evidence = Evidence()
    evidence.known_path = "known location"
    category, confidence, reasons = classify_tree_root(
        repo, known_install=False, evidence=evidence)
    assert category == CATEGORY_SOURCE_REPO
    assert confidence == CONFIDENCE_CONFIRMED
    assert any("source checkout" in r for r in reasons)
    assert category in PROTECTED_CATEGORIES


# ---------------------------------------------------------------------------
# Tree content rules: what inside an owned tree is preserved
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("parts,is_dir,expected", [
    (("data",), True, CATEGORY_TRADING_HISTORY),
    (("data", "history"), True, CATEGORY_TRADING_HISTORY),
    (("data", "cache"), True, "cache"),
    (("logs",), True, CATEGORY_LOGS),
    ((".env",), False, CATEGORY_CONFIGURATION),
    ((".env.example",), False, CATEGORY_CONFIGURATION),
    (("upstox_token.json",), False, "credentials"),
    (("research",), True, CATEGORY_RESEARCH),
    (("results",), True, CATEGORY_RESEARCH),
    (("backups",), True, CATEGORY_TRADING_HISTORY),
    (("trading_bot.db",), False, CATEGORY_TRADING_HISTORY),
    (("bot.jsonl",), False, CATEGORY_LOGS),
    ((".venv",), True, "development"),
    (("__pycache__",), True, "cache"),
    (("models",), True, "cache"),
])
def test_tree_content_categories(parts, is_dir, expected):
    assert classify.classify_tree_child(parts, is_dir, parts[-1]) == expected


def test_unknown_tree_content_is_disposable_payload():
    assert classify.classify_tree_child(("Arena.exe",), False, "Arena.exe") is None


def test_protected_category_entries_are_never_preselected(tmp_path):
    protected = tmp_path / "data"
    protected.mkdir()
    match = classify_file(protected, Evidence())
    match.category = CATEGORY_TRADING_HISTORY
    assert match.protected
    assert match.default_select() is False


def test_peek_protected_finds_nested_preserved_data(tmp_path):
    tree = tmp_path / "portable"
    (tree / "lib" / "logs").mkdir(parents=True)
    (tree / "lib" / "logs" / "run.log").write_text("x", encoding="utf-8")
    (tree / "app").mkdir()
    (tree / "app" / "Arena.exe").write_bytes(b"binary")
    assert classify.peek_protected(tree) is True


def test_peek_protected_false_for_pure_payload(tmp_path):
    tree = tmp_path / "portable"
    (tree / "lib").mkdir(parents=True)
    (tree / "lib" / "something.dll").write_bytes(b"binary")
    assert classify.peek_protected(tree) is False


# ---------------------------------------------------------------------------
# Registrations
# ---------------------------------------------------------------------------


def _entry(**kwargs) -> UninstallEntry:
    base = dict(hive="HKCU",
                key_path=r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\AXIOM Arena_is1",
                display_name="AXIOM Arena", publisher="Thedivinetwilight")
    base.update(kwargs)
    return UninstallEntry(**base)


def test_registration_by_name_and_publisher_is_confirmed():
    ok, _ = signals.registry_entry_signal("AXIOM Arena", "Thedivinetwilight")
    assert ok


def test_registration_by_app_id_is_confirmed():
    entry = _entry(key_path=(r"SOFTWARE\Microsoft\Windows\CurrentVersion"
                             r"\Uninstall\{B8E5C7A2-4F31-4D29-9E8A-1A2B3C4D5E6F}_is1"),
                   display_name="Something Else", publisher="")
    ok, _ = signals.registry_entry_signal(entry.display_name, entry.publisher,
                                          app_id=entry.key_path)
    assert ok


@pytest.mark.parametrize("name,publisher", [
    ("Arena", "Other Vendor"),
    ("AXIOM Arena", ""),
    ("Arena Launcher", "Thedivinetwilight"),
])
def test_lookalike_registrations_are_rejected(name, publisher):
    ok, _ = signals.registry_entry_signal(name, publisher)
    assert ok is False


def test_registry_scan_and_removal_via_platform(tmp_path):
    world = make_world(tmp_path)
    entry = _entry(install_location=str(world.install_root))
    platform = fake_platform(world, entries=[entry])
    inventory = build_inventory(platform,
                                __import__("uninstaller.winapi", fromlist=["KnownPaths"]).KnownPaths.from_env(world.env),
                                ScanRequest(mode="standard"), "test")
    registrations = [m for m in inventory.matches if m.kind == "registry"]
    assert len(registrations) == 1
    assert registrations[0].selected
    assert registrations[0].registry["install_location"] == str(world.install_root)


# ---------------------------------------------------------------------------
# Duplicates
# ---------------------------------------------------------------------------


def test_duplicate_executables_are_grouped_and_primary_kept(tmp_path):
    world = make_world(tmp_path)
    # A second, byte-identical copy in Downloads.
    copy = world.downloads / "Arena-copy.exe"
    copy.write_bytes((world.install_root / "Arena.exe").read_bytes())

    platform = fake_platform(world)
    from uninstaller.winapi import KnownPaths
    inventory = build_inventory(
        platform, KnownPaths.from_env(world.env),
        ScanRequest(mode="deep", extra_roots=[world.root],
                    include_drives=False), "test")
    known = next(m for m in inventory.matches
                 if m.path == str(world.install_root / "Arena.exe"))
    assert known.category in (CATEGORY_INSTALLATION, CATEGORY_LEFTOVER)
    duplicates = [m for m in inventory.matches if m.category == CATEGORY_DUPLICATE]
    assert duplicates, "the byte-identical copy should be discovered"
    assert duplicates[0].duplicate_of == known.path


def test_duplicate_detection_ignores_unconfirmed_lookalikes(tmp_path):
    world = make_world(tmp_path)
    # Same content as Arena.exe, but NOT proven to be Arena (no metadata).
    fake = world.downloads / "innocent.bin"
    fake.write_bytes((world.install_root / "Arena.exe").read_bytes() + b"x")
    platform = fake_platform(world)
    from uninstaller.winapi import KnownPaths
    inventory = build_inventory(
        platform, KnownPaths.from_env(world.env),
        ScanRequest(mode="deep", extra_roots=[world.root],
                    include_drives=False), "test")
    assert not [m for m in inventory.matches if m.category == CATEGORY_DUPLICATE]


def test_dedupe_keeps_the_richer_record(tmp_path):
    from uninstaller.model import Inventory, Match
    inventory = Inventory(mode="standard")
    inventory.add(Match(path="/x/Arena.exe", category=CATEGORY_LEFTOVER,
                        reasons=["one"]))
    inventory.add(Match(path="/x/Arena.exe", category=CATEGORY_INSTALLATION,
                        reasons=["one", "two", "three"]))
    dedupe(inventory)
    assert len(inventory.matches) == 1
    assert inventory.matches[0].category == CATEGORY_INSTALLATION
