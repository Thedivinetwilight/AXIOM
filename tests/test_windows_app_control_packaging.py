"""Static contract checks for the unsigned Windows App Control workaround.

These tests do not emulate Smart App Control and are not a substitute for the
Windows install/portable gates or the owner's device verification.
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / "packaging" / "windows"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_version_is_consistent_across_frozen_and_operator_metadata():
    version = read(KIT / "VERSION").strip()
    assert version == "3.7.0a1"
    assert f'__version__ = "{version}"' in read(ROOT / "app" / "__init__.py")
    assert f"StringStruct('ProductVersion', '{version}')" in read(KIT / "version_info.txt")
    assert f"**Version:** {version}" in read(KIT / "WINDOWS_INSTALL_MANUAL.md")
    assert f"**Current source version: {version}**" in read(ROOT / "USER_MANUAL.md")


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
    # The default EXE still contains binaries directly for upgrade compatibility.
    default = spec.rsplit("else:", 1)[-1]
    assert "a.binaries" in default and "runtime_tmpdir=None" in default


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


def test_documentation_does_not_claim_unsigned_policy_bypass():
    guide = read(ROOT / "docs" / "WINDOWS_APP_CONTROL.md")
    for phrase in ("UseSetupLdr=no", "-0.bin", "-1.bin", "_internal", "UNSIGNED", "not a promise"):
        assert phrase in guide
    assert "do not disable" in guide.lower() or "does not disable" in guide.lower()
    manual = read(ROOT / "USER_MANUAL.md")
    assert "extract the entire portable ZIP" in manual
    assert "Keep Application Control enabled" in manual
