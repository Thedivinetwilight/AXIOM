"""Static contract checks for the unsigned Windows App Control workaround.

These tests do not emulate Smart App Control and are not a substitute for the
Windows install/portable gates or the owner's device verification.
"""
import importlib.util
import json
from pathlib import Path
import sys
import types
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / "packaging" / "windows"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def payload_guard():
    spec = importlib.util.spec_from_file_location("axiom_payload_guard", KIT / "payload_guard.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_version_is_consistent_across_frozen_and_operator_metadata():
    version = read(KIT / "VERSION").strip()
    assert version == "3.7.0a6"
    assert f'__version__ = "{version}"' in read(ROOT / "app" / "__init__.py")
    assert f'version = "{version}"' in read(ROOT / "pyproject.toml")
    assert f"**Current source version: `{version}`.**" in read(ROOT / "README.md")
    assert f"**Current source version: {version}**" in read(ROOT / "USER_MANUAL.md")
    version_info = read(KIT / "version_info.txt")
    assert "filevers=(3, 7, 0, 6)" in version_info
    assert "prodvers=(3, 7, 0, 6)" in version_info
    assert "StringStruct('FileVersion', '3.7.0.6')" in version_info
    assert f"StringStruct('ProductVersion', '{version}')" in version_info
    manual = read(KIT / "WINDOWS_INSTALL_MANUAL.md")
    assert f"**Version:** {version}" in manual
    assert f"-ExpectedCommit <full-source-commit> -Version {version}" in manual
    assert f"AXIOM-Operator-{version}-windows-x64-no-temp-install.zip" in manual
    assert f"AXIOM-Operator-{version}-windows-x64-portable.zip" in manual


def test_release_workflow_derives_a_new_tag_and_requires_explicit_publication():
    assert read(KIT / "VERSION").strip() == "3.7.0a6"
    workflow = read(ROOT / ".github" / "workflows" / "windows-release.yml")
    assert "Get-Content _kit/packaging/windows/VERSION" in workflow
    assert '$tag = "v$($env:VERSION)"' in workflow
    assert "gh release create $tag --prerelease --target $meta.commit" in workflow
    assert "github.event_name == 'workflow_dispatch' && inputs.publish" in workflow
    assert "default: false" in workflow and "type: boolean" in workflow
    assert "gh release delete" not in workflow
    assert "refusing to replace existing release/tag" in workflow
    assert "git -C $kit push" not in workflow
    assert "v3.7.0a1" not in workflow


def test_inno_uses_loaderless_mode_and_complete_onedir_sources():
    script = read(KIT / "axiom.iss")
    assert "UseSetupLdr=no" in script
    assert "SourceExe" not in script
    assert 'Source: "dist-onedir\\AXIOM-Operator\\*"' in script
    assert "recursesubdirs createallsubdirs" in script
    assert "-windows-x64-no-temp-setup" in script
    assert "WINDOWS_APP_CONTROL.md" in script
    assert "RESEARCH_CANDIDATES.md" in script


def test_spec_retains_legacy_onefile_and_has_explicit_onedir_branch():
    spec = read(KIT / "axiom.spec")
    assert 'os.environ.get("AXIOM_ONEDIR") == "1"' in spec
    assert "exclude_binaries=True" in spec
    assert 'COLLECT(' in spec and 'name="AXIOM-Operator"' in spec
    assert "RESEARCH_CANDIDATES.md" in spec
    assert "from payload_guard import demo_data_sources" in spec
    assert '(str(ROOT / "data"), "data")' not in spec
    assert "demo_datas" in spec
    # The default EXE still contains binaries directly for upgrade compatibility.
    default = spec.rsplit("else:", 1)[-1]
    assert "a.binaries" in default and "runtime_tmpdir=None" in default


def test_kronos_frozen_resources_and_installed_inference_release_gate():
    spec = read(KIT / "axiom.spec")
    for resource in (
        'kronos_artifacts.py', 'kronos_runtime.py', 'kronos_worker.py',
        'kronos-small.json', 'kronos-runtime-requirements.txt',
    ):
        assert resource in spec
    installer = read(KIT / "install_test.ps1")
    assert "/api/autonomy/kronos/install" in installer
    assert "/api/autonomy/kronos/python" in installer
    assert "automatically discovers CPython x64 with pip" in installer
    assert "explicit py -3.11-64 x64 selector" in installer
    assert "$launcherIdentity.machine -match '^(AMD64|x86_64)$'" in installer
    assert "self-test is bound to the selected Python version and platform" in installer
    assert "kronos-status.json" in installer
    assert "SYNTHETIC_INFERENCE_SMOKE_ONLY" in installer
    assert "installedWeights.Count -eq 0" in installer
    workflow = read(ROOT / ".github" / "workflows" / "windows-release.yml")
    assert "kronos_real_inference" in workflow and "kronos_evidence" in workflow
    for asset in (
        "kronos-status.json", "kronos-small.json", "kronos-runtime-requirements.txt",
        "ARCHITECTURE_AUDIT.md",
    ):
        assert asset in workflow
    build = read(KIT / "build_windows.ps1")
    assert "tests/test_kronos_setup.py" in build
    attributes = read(ROOT / ".gitattributes")
    assert "app/autonomy/vendor/kronos/model/*.py -text" in attributes
    assert "config/kronos-small.json text eol=lf" in attributes
    assert "config/kronos-runtime-requirements.txt text eol=lf" in attributes


def test_build_packages_sidecars_and_both_zip_alternatives():
    build = read(KIT / "build_windows.ps1")
    assert "$env:AXIOM_ONEDIR = '1'" in build
    assert "Remove-Item Env:AXIOM_ONEDIR" in build
    assert "*-no-temp-setup.exe" in build
    assert '"-$index.bin"' in build
    assert "Compress-Archive" in build
    assert "windows-x64-no-temp-install.zip" in build
    assert "windows-x64-portable.zip" in build
    assert "foreach ($f in @($Exe, $InstallZip, $PortableZip))" in build
    assert "app_control_device_verification" not in build  # added only after real portable gate
    assert "UNSIGNED workaround only" in build
    assert "RESEARCH_CANDIDATES.md" in build
    assert "tests/test_research_candidate_cli.py" in build
    assert "tests/test_paper_replay_audit.py" in build
    for kind in ("source", "onefile", "tree", "zip"):
        assert f"$PayloadGuard {kind}" in build
    assert "node --check app/static/chart/chart.js; node --check app/static/terminal/terminal.js; node --check app/static/autonomy/autonomy.js; node tests/js/autonomy_smoke.mjs; node tests/js/chart_smoke.mjs; node tests/js/terminal_smoke.mjs" in build
    assert "--output `\"$BenchmarkOutput`\"" in build
    assert "installer_test" in build and "payload_hygiene" in build


def test_demo_allowlist_ignores_local_autonomy_state(tmp_path):
    guard = payload_guard()
    data = tmp_path / "data"
    autonomy = data / "autonomy"
    autonomy.mkdir(parents=True)
    for name in guard.DEMO_DATA_FILES:
        (data / name).write_text("synthetic demo fixture\n", encoding="utf-8")
    journal = autonomy / "journal.sqlite3"
    journal.write_bytes(guard.SQLITE_MAGIC + b"local-only-state")
    sources = guard.demo_data_sources(tmp_path)
    assert [source.name for source, destination in sources] == list(guard.DEMO_DATA_FILES)
    assert all(destination == "data" for source, destination in sources)
    assert journal not in [source for source, destination in sources]


def test_forbidden_payload_names_and_sqlite_magic_fail_closed(tmp_path):
    guard = payload_guard()
    for name in (
        ".env", "axiom.env", "data/autonomy/journal.sqlite3",
        "data/cache/user.json", "results/report.json", "model.safetensors",
        "operator.paper.lock", "logs/session.jsonl",
    ):
        with pytest.raises(guard.PayloadError):
            guard.check_payload(name)
    for name in ("data/sample.csv", "_internal/sqlite3.dll", "_internal/certifi/cacert.pem"):
        guard.check_payload(name)
    with pytest.raises(guard.PayloadError, match="SQLite"):
        guard.check_payload("otherwise-benign.bin", guard.SQLITE_MAGIC + b"state")

    root = tmp_path / "onedir"
    root.mkdir()
    (root / "AXIOM-Chart.exe").write_bytes(b"safe test placeholder")
    (root / "state.bin").write_bytes(guard.SQLITE_MAGIC + b"state")
    with pytest.raises(guard.PayloadError, match="SQLite"):
        guard.assert_tree_clean(root)

    archive_path = tmp_path / "portable.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("AXIOM-Operator/_internal/data/sample.csv", "synthetic")
        archive.writestr("AXIOM-Operator/_internal/data/autonomy/evidence/user.json", "{}")
    with pytest.raises(guard.PayloadError, match="operator state"):
        guard.assert_zip_clean(archive_path)


def test_onefile_guard_checks_every_carchive_payload_with_a_fake_reader(tmp_path, monkeypatch):
    guard = payload_guard()
    executable = tmp_path / "AXIOM-Chart.exe"
    executable.write_bytes(b"fake archive for unit test")

    class Reader:
        def __init__(self, path):
            self.toc = {"data/sample.csv": object(), "state.bin": object()}

        def extract(self, name):
            return guard.SQLITE_MAGIC + b"state" if name == "state.bin" else b"synthetic"

    package = types.ModuleType("PyInstaller")
    archive = types.ModuleType("PyInstaller.archive")
    readers = types.ModuleType("PyInstaller.archive.readers")
    readers.CArchiveReader = Reader
    package.archive = archive
    archive.readers = readers
    monkeypatch.setitem(sys.modules, "PyInstaller", package)
    monkeypatch.setitem(sys.modules, "PyInstaller.archive", archive)
    monkeypatch.setitem(sys.modules, "PyInstaller.archive.readers", readers)
    with pytest.raises(guard.PayloadError, match="state.bin.*SQLite"):
        guard.assert_onefile_clean(executable)


def test_install_and_portable_acceptance_are_separate_hard_gates():
    workflow = read(ROOT / ".github" / "workflows" / "windows-release.yml")
    assert "Expand-Archive -Path $package -DestinationPath $extract" in workflow
    assert "./install_test.ps1 -DistDir dist -SetupPath $setup" in workflow
    assert "./portable_test.ps1 -PackagePath $package -ExpectedCommit" in workflow
    assert "portable_checks_failed" in workflow
    assert "app_control_device_verification" in workflow
    assert "*-no-temp-install.zip" in workflow
    assert "*-portable.zip" in workflow
    assert "(Get-ChildItem dist -Filter '*-setup.exe'" not in workflow
    assert workflow.count("RESEARCH_CANDIDATES.md") >= 3
    assert "byte-length mismatch" in workflow
    assert "checksum manifest does not cover" in workflow
    assert "release assets and checksum manifest must be unique and one-to-one" in workflow

    install = read(KIT / "install_test.ps1")
    assert "refusing to run an incomplete loaderless installer" in install
    assert "installed runtime" in install
    portable = read(KIT / "portable_test.ps1")
    assert "Expand-Archive" in portable
    assert "_MEI*" in portable
    assert "LOCAL PAPER" in portable
    assert '"$Base/api/trading/connect"' not in portable
    assert '"$Base/api/trading/arm"' not in portable
    assert "if ($script:ExitCode -ne 0) { exit 1 }" in portable
    assert portable.rstrip().endswith("exit 0")
    assert "-UseBasicRequest" not in portable


def test_a5_blocker_repairs_have_explicit_frozen_package_acceptance():
    smoke = read(KIT / "frozen_blocker_smoke.py")
    assert "PaperExecution" in smoke and "MockTransport" in smoke
    assert "UpstoxLiveBroker" not in smoke
    assert "broker_order_endpoint_calls" in smoke
    assert "--autonomy-blocker-smoke" in read(KIT / "axiom_launcher.py")
    assert '"frozen_blocker_smoke"' in read(KIT / "axiom.spec")
    helper = ". (Join-Path $PSScriptRoot 'PaperSmoke.ps1')"
    for name in ("build_windows.ps1", "install_test.ps1", "portable_test.ps1"):
        script = read(KIT / name)
        assert helper in script
        assert "Test-AxiomPaperBlockerSmoke" in script
    build = read(KIT / "build_windows.ps1")
    assert "$BlockerFixFiles.Count -ne 8" in build
    assert "tests/test_autonomy_release_blockers.py" in build
    workflow = read(ROOT / ".github" / "workflows" / "windows-release.yml")
    assert "$meta.blocker_fix_files.Count -ne 8" in workflow
    assert "installed/upgraded frozen PAPER blocker diagnostics are missing" in workflow
    assert "portable frozen PAPER blocker diagnostics are missing" in workflow
    linux = read(ROOT / ".github" / "workflows" / "paper-checks.yml")
    assert "tests/test_autonomy_release_blockers.py" in linux


def test_paper_blocker_diagnostic_source_contract(capsys):
    path = KIT / "frozen_blocker_smoke.py"
    spec = importlib.util.spec_from_file_location("axiom_paper_blocker_smoke_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.run() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "PASS"
    assert result["mode"] == "PAPER" and result["live_enabled"] is False
    assert result["broker_order_endpoint_calls"] == 0
    assert set(result["checks"]) == {
        "candle_ordering", "null_event_status",
        "database_failure_fail_closed", "market_close_cleanup",
    }


def test_documentation_does_not_claim_unsigned_policy_bypass():
    guide = read(ROOT / "docs" / "WINDOWS_APP_CONTROL.md")
    for phrase in ("UseSetupLdr=no", "-0.bin", "-1.bin", "_internal", "UNSIGNED", "not a promise"):
        assert phrase in guide
    assert "do not disable" in guide.lower() or "does not disable" in guide.lower()
    manual = read(ROOT / "USER_MANUAL.md")
    assert "extract the entire portable ZIP" in manual
    assert "Keep Application Control enabled" in manual
