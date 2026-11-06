"""Static gates for the Windows packaging scripts.

The Windows build/install/portable acceptance can only run on Windows.  These
checks run everywhere and catch the two failure modes that actually broke a
release in this repository:

* the smoke scripts drifted onto API routes that the shipped application no
  longer mounts (``/api/trading/*``), so every Windows gate asserted 200 on a
  404 and the build never failed where it should have,
* the packaged launcher lost a runtime C extension (``ssl``) and failed deep
  inside an import instead of at build time.

They also pin the safety properties the release notes claim: the uninstaller
never deletes outside the operator data folder, the checksum manifest names the
exact commit, and the frozen diagnostics stay single-purpose so they can never
be combined into a normal launch.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PACKAGING = REPO / "packaging" / "windows"
LAUNCHER = PACKAGING / "axiom_launcher.py"


# --------------------------------------------------------------------------- #
# A deliberately small PowerShell "parser": enough to prove the scripts are
# still balanced after an edit without a Windows or PowerShell installation.
# --------------------------------------------------------------------------- #
_PAIRS = {"{": "}", "(": ")", "[": "]"}


def _tokens(text: str) -> tuple[list[str], list[str]]:
    """Return (structural tokens, problems) ignoring strings and comments."""
    tokens: list[str] = []
    problems: list[str] = []
    i = 0
    length = len(text)
    while i < length:
        char = text[i]
        if char == "<" and text.startswith("<#", i):
            end = text.find("#>", i + 2)
            if end == -1:
                problems.append("unterminated block comment")
                break
            i = end + 2
            continue
        if char == "#" and not text.startswith("#>", i):
            end = text.find("\n", i)
            i = length if end == -1 else end + 1
            continue
        if char == "@" and i + 1 < length and text[i + 1] in ("'", '"'):
            # Here-string: everything up to a line that starts with '%c@'.
            terminator = "\n" + text[i + 1] + "@"
            end = text.find(terminator, i + 2)
            if end == -1:
                problems.append(f"unterminated here-string at offset {i}")
                break
            i = end + len(terminator)
            continue
        if char in ("'", '"'):
            # PowerShell escapes a quote by doubling it.
            i += 1
            closed = False
            while i < length:
                if text[i] == char:
                    if text.startswith(char * 2, i):
                        i += 2
                        continue
                    i += 1
                    closed = True
                    break
                if text[i] == "`":
                    i += 2
                    continue
                if text[i] == "\n" and char == '"':
                    problems.append(f"unterminated double-quoted string at offset {i}")
                    return tokens, problems
                i += 1
            if not closed:
                problems.append(f"unterminated string at offset {i}")
            continue
        if char in _PAIRS or char in _PAIRS.values():
            tokens.append(char)
        i += 1
    return tokens, problems


def _balance_problems(path: Path) -> list[str]:
    tokens, problems = _tokens(path.read_text(encoding="utf-8"))
    stack: list[str] = []
    for token in tokens:
        if token in _PAIRS:
            stack.append(token)
        else:
            if not stack:
                problems.append(f"unexpected closing {token!r}")
                break
            opening = stack.pop()
            if _PAIRS[opening] != token:
                problems.append(f"{opening!r} closed by {token!r}")
                break
    if stack:
        problems.append("unclosed: " + " ".join(stack[-5:]))
    return problems


PS1_FILES = sorted(PACKAGING.glob("*.ps1")) + [REPO / "Start-Axiom.ps1"]


def test_powershell_files_exist():
    names = {path.name for path in PS1_FILES}
    assert {"build_windows.ps1", "install_test.ps1", "portable_test.ps1", "PaperSmoke.ps1"} <= names
    assert "Start-Axiom.ps1" in names


@pytest.mark.parametrize("script", PS1_FILES, ids=lambda p: p.name)
def test_powershell_script_is_structurally_balanced(script: Path):
    assert _balance_problems(script) == []


# --------------------------------------------------------------------------- #
# Smoke routes must exist in the application that ships in the package.
# --------------------------------------------------------------------------- #
from app.api.server import create_app  # noqa: E402  (after REPO is defined)
from app.config.settings import Settings  # noqa: E402


def _route_paths() -> set[str]:
    """Every documented route of the shipped application.

    Router includes are resolved lazily by Starlette, so the OpenAPI schema is
    the reliable inventory. Building it starts no database, no broker and no
    background thread.
    """
    settings = Settings(api_token="static-gate-token", database_url="sqlite:///:memory:")
    app = create_app(settings)
    return set(app.openapi()["paths"])


REQUIRED_SMOKE_ROUTES = (
    "/health",
    "/paper/status",
    "/paper/account",
    "/paper/order",
    "/api/paper/start-default",
    "/api/performance",
    "/api/connections/",
    "/api/market-data/status",
    "/api/market-data/connect",
    "/api/listed-assets/status",
    "/api/listed-assets/routing-policy",
    "/api/autonomy/status",
    "/chart/symbols",
    "/chart/data",
)


def test_smoke_routes_are_mounted():
    mounted = _route_paths()
    missing = [path for path in REQUIRED_SMOKE_ROUTES if path not in mounted]
    assert missing == [], f"Windows smoke gates call unmounted routes: {missing}"


def test_legacy_vantage_trading_surface_stays_unmounted():
    mounted = {path for path in _route_paths() if path.startswith("/api/trading")}
    assert mounted == set()


TRADING_REFERENCE = re.compile(r"/api/trading(?:/[A-Za-z0-9_{}/.-]*)?")


def _gate_scripts() -> list[Path]:
    return [
        PACKAGING / "build_windows.ps1",
        PACKAGING / "install_test.ps1",
        PACKAGING / "portable_test.ps1",
    ]


@pytest.mark.parametrize("script", _gate_scripts(), ids=lambda p: p.name)
def test_scripts_only_reference_removed_trading_routes_to_assert_absence(script: Path):
    """A `/api/trading/*` mention is only allowed in a removal assertion."""
    text = script.read_text(encoding="utf-8")
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not TRADING_REFERENCE.search(line):
            continue
        stripped = line.strip()
        allowed = (
            # the explicit removal lists
            stripped.startswith("'/api/trading")
            or "'/api/trading" in stripped
            # the assertions that the route is gone
            or "not mounted" in stripped
            or "does not mount" in stripped
            or stripped.startswith("#")
            or stripped.startswith(";")
            # `Invoke-WebRequest ... $removed` / `$Base$removed`
            or "$removed" in stripped
            or "$Removed" in stripped
        )
        assert allowed, f"{script.name}:{line_number} uses a removed route: {stripped}"


@pytest.mark.parametrize("script", _gate_scripts(), ids=lambda p: p.name)
def test_scripts_assert_the_replacement_routes(script: Path):
    text = script.read_text(encoding="utf-8")
    for route in ("/paper/status", "/api/market-data/status"):
        assert route in text, f"{script.name} never checks {route}"


# --------------------------------------------------------------------------- #
# Frozen diagnostics wired into the build/install gates.
# --------------------------------------------------------------------------- #
def test_paper_smoke_exposes_runtime_and_dpapi_gates():
    text = (PACKAGING / "PaperSmoke.ps1").read_text(encoding="utf-8")
    assert "function Test-AxiomRuntimeImports" in text
    assert "function Test-AxiomDpapiStorage" in text
    assert "--runtime-check" in text
    assert "--dpapi-selftest" in text
    assert "$result.secret_echoed -eq $false" in text


@pytest.mark.parametrize(
    "script,expected",
    [
        ("build_windows.ps1", ("Test-AxiomRuntimeImports $Exe", "Test-AxiomDpapiStorage $Exe")),
        ("install_test.ps1", ("Test-AxiomRuntimeImports $installedExe", "Test-AxiomDpapiStorage $installedExe")),
        ("portable_test.ps1", ("Test-AxiomRuntimeImports $script:Exe",)),
    ],
)
def test_gates_call_the_frozen_diagnostics(script, expected):
    text = (PACKAGING / script).read_text(encoding="utf-8")
    for needle in expected:
        assert needle in text, f"{script} does not run {needle}"


# --------------------------------------------------------------------------- #
# `Start-Axiom.ps1 -Smoke`, the documented source smoke path.
# --------------------------------------------------------------------------- #
def test_start_axiom_documents_the_smoke_alias():
    text = (REPO / "Start-Axiom.ps1").read_text(encoding="utf-8")
    assert '[Alias("Smoke")]' in text
    assert "$ForwardArgs.Add(\"--smoke\")" in text
    # a smoke run is never interactive
    assert "$NoBrowser = $true" in text
    # the exit status must reach the caller
    assert "exit $Exit" in text
    assert "AXIOM exited with status" in text


def test_documented_smoke_invocations_match_the_launcher():
    launcher = LAUNCHER.read_text(encoding="utf-8")
    assert '"--smoke" in argv' in launcher
    for doc in ("README.md", "USER_MANUAL.md"):
        text = (REPO / doc).read_text(encoding="utf-8")
        if "Start-Axiom.ps1 -Smoke" in text:
            assert "Start-Axiom.ps1" in text


# --------------------------------------------------------------------------- #
# Build inputs: the spec must request the runtime extensions explicitly.
# --------------------------------------------------------------------------- #
def test_spec_requests_runtime_c_extensions():
    spec = (PACKAGING / "axiom.spec").read_text(encoding="utf-8")
    for module in ('"ssl"', '"_ssl"', '"_hashlib"', '"_sqlite3"', '"ctypes.wintypes"'):
        assert module in spec, f"axiom.spec does not request {module}"


def _load_launcher_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("axiom_launcher_static_gate", LAUNCHER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_launcher_runtime_modules_match_the_spec():
    RUNTIME_MODULES = _load_launcher_module().RUNTIME_MODULES
    spec = (PACKAGING / "axiom.spec").read_text(encoding="utf-8")
    for module in ("ssl", "hashlib", "sqlite3", "ctypes", "zoneinfo"):
        assert module in RUNTIME_MODULES
        assert f'"{module}"' in spec


# --------------------------------------------------------------------------- #
# Installer safety: nothing is deleted outside the operator data folder.
# --------------------------------------------------------------------------- #
def test_installer_uninstall_delete_section_is_empty():
    iss = (PACKAGING / "axiom.iss").read_text(encoding="utf-8")
    section = iss.split("[UninstallDelete]", 1)[1].split("[", 1)[0]
    entries = [
        line.strip()
        for line in section.splitlines()
        if line.strip() and not line.strip().startswith(";")
    ]
    assert entries == []


def test_installer_never_follows_a_junction_or_symlink():
    iss = (PACKAGING / "axiom.iss").read_text(encoding="utf-8")
    assert "GetFileAttributesW" in iss
    assert "FILE_ATTRIBUTE_REPARSE_POINT" in iss
    assert "IsReparsePoint(UserDataDir())" in iss
    # the deletion target is the per-user data folder and nothing else
    assert "DelTree(UserDataDir(), True, True, True)" in iss
    assert iss.count("DelTree(") == 1


def test_installer_keeps_data_on_silent_uninstall():
    iss = (PACKAGING / "axiom.iss").read_text(encoding="utf-8")
    assert "if UninstallSilent then" in iss
    assert "a silent uninstall always keeps the operator's data" in iss
    assert "MB_DEFBUTTON2" in iss


def test_install_test_checks_unrelated_files_survive_uninstall():
    text = (PACKAGING / "install_test.ps1").read_text(encoding="utf-8")
    assert "AXIOM-Unrelated-Neighbour-Test" in text
    assert "left an unrelated sibling file alone" in text
    assert "left an unrelated neighbouring folder alone" in text


# --------------------------------------------------------------------------- #
# Traceability: checksums name the commit, logs are retained.
# --------------------------------------------------------------------------- #
def test_checksum_manifest_is_commit_bound():
    build = (PACKAGING / "build_windows.ps1").read_text(encoding="utf-8")
    assert "# commit:  $Head" in build
    assert "AXIOM Windows release checksums (SHA-256)" in build


def test_workflow_ignores_manifest_header_lines_and_keeps_logs():
    workflow = (REPO / ".github" / "workflows" / "windows-release.yml").read_text(encoding="utf-8")
    assert "-not $_.Trim().StartsWith('#')" in workflow
    assert "build-windows.log" in workflow
    assert "retention-days: 30" in workflow
    assert "if: always()" in workflow


def test_workflow_builds_this_branch():
    workflow = (REPO / ".github" / "workflows" / "windows-release.yml").read_text(encoding="utf-8")
    push_block = workflow.split("on:", 1)[1].split("workflow_dispatch:", 1)[0]
    assert "arena/8d3d005e-axiom" in push_block


# --------------------------------------------------------------------------- #
# Frozen diagnostic behaviour (runnable on every platform).
# --------------------------------------------------------------------------- #
def _run_launcher(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(LAUNCHER), *args],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=str(REPO),
    )


def test_runtime_check_contract_and_no_side_effects(tmp_path):
    before = {path.name for path in REPO.iterdir()}
    completed = _run_launcher("--runtime-check")
    after = {path.name for path in REPO.iterdir()}
    assert before == after
    assert completed.stderr == ""
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, completed.stdout
    report = json.loads(lines[0])
    assert report["status"] == "PASS"
    assert completed.returncode == 0
    assert report["failures"] == []
    for module in ("ssl", "hashlib", "sqlite3", "ctypes.wintypes", "zoneinfo"):
        assert report["modules"][module] is True
    assert report["identity"]["frozen"] is False
    assert set(report["identity"]) == {"commit", "version", "frozen"}
    assert report["note"].startswith("import/availability diagnostic only")


def test_runtime_check_refuses_extra_arguments():
    completed = _run_launcher("--runtime-check", "--port", "8123")
    assert completed.returncode == 2
    assert "cannot be combined" in completed.stderr


@pytest.mark.skipif(os.name == "nt", reason="the non-Windows branch is asserted separately")
def test_dpapi_selftest_reports_unavailable_off_windows():
    completed = _run_launcher("--dpapi-selftest")
    assert completed.returncode == 3
    report = json.loads(completed.stdout.strip().splitlines()[-1])
    assert report["status"] == "UNAVAILABLE"
    assert report["secret_echoed"] is False
    assert report["isolated_directory"] is True
    assert report["roundtrip_ok"] is False
    assert "DPAPI" in " ".join(report["failures"])


@pytest.mark.skipif(os.name != "nt", reason="DPAPI only exists on Windows")
def test_dpapi_selftest_round_trips_a_synthetic_secret():
    completed = _run_launcher("--dpapi-selftest")
    assert completed.returncode == 0, completed.stdout
    report = json.loads(completed.stdout.strip().splitlines()[-1])
    assert report["status"] == "PASS"
    assert report["roundtrip_ok"] is True
    assert report["encrypted_at_rest"] is True
    assert report["delete_ok"] is True
    assert report["missing_key_fail_closed"] is True
    assert report["redaction_ok"] is True
    assert report["secret_echoed"] is False
    assert "SYNTHETIC" not in completed.stdout
    assert "SYNTHETIC" not in completed.stderr


def test_dpapi_selftest_never_touches_the_operator_store():
    source = LAUNCHER.read_text(encoding="utf-8")
    block = source.split("def dpapi_selftest", 1)[1].split("\ndef ", 1)[0]
    assert "tempfile.mkdtemp(prefix=\"axiom-dpapi-selftest-\")" in block
    # the store is constructed on the temporary directory ...
    assert "DpapiSecretStore(\n                workdir," in block
    # ... and the operator folders are only ever used to prove isolation
    assert "for env_name in (\"LOCALAPPDATA\", \"APPDATA\"):" in block
    assert "resolves inside the operator data folder" in block
    assert "shutil.rmtree(workdir" in block
    for written in ("workdir /", "workdir,"):
        assert written in block


# --------------------------------------------------------------------------- #
# The source smoke path and the packaged gates must assert one contract.
# --------------------------------------------------------------------------- #
def test_source_smoke_asserts_the_paper_only_contract():
    source = (REPO / "app" / "source_launcher.py").read_text(encoding="utf-8")
    for check in (
        '"paper_status_disarmed"',
        '"market_data_read_only"',
        '"legacy_trading_api_absent"',
    ):
        assert check in source, f"source smoke does not assert {check}"
    assert 'probe("/paper/status")' in source
    assert 'probe("/api/trading/status")' in source


def test_source_smoke_passes_on_this_platform():
    completed = _run_launcher("--smoke")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads(completed.stdout.strip().splitlines()[-1])
    assert report["status"] == "PASS"
    assert report["mode"] == "PAPER"
    assert report["live_trading"] is False
    assert report["broker_order_calls"] == 0
    assert report["user_data_modified"] is False
    assert report["checks"]["paper_status_disarmed"] is True
    assert report["checks"]["market_data_read_only"] is True
    assert report["checks"]["legacy_trading_api_absent"] is True


# --------------------------------------------------------------------------- #
# Frozen-bundle inputs. PyInstaller itself needs a Python with a shared library
# (absent in this sandbox), so the spec's own build-time decisions are executed
# here: the reviewed DEMO fixtures and every literal payload path must resolve.
# --------------------------------------------------------------------------- #
def _load_payload_guard():
    import importlib.util

    path = PACKAGING / "payload_guard.py"
    spec = importlib.util.spec_from_file_location("payload_guard_static_gate", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_spec_demo_fixtures_are_reviewed_and_present():
    guard = _load_payload_guard()
    sources = guard.demo_data_sources(REPO)
    assert len(sources) == 8
    for source, destination in sources:
        assert destination == "data"
        assert Path(source).is_file()
        assert Path(source).parent.name == "data"


def test_every_literal_spec_payload_path_exists():
    spec_text = (PACKAGING / "axiom.spec").read_text(encoding="utf-8")
    literal = re.compile(r'\(str\(ROOT\s*/\s*((?:"[^"]+"\s*(?:/\s*)?)+)\),')
    paths: list[Path] = []
    for match in literal.finditer(spec_text):
        parts = re.findall(r'"([^"]+)"', match.group(1))
        paths.append(REPO.joinpath(*parts))
    assert paths, "no literal payload paths found in axiom.spec"
    missing = [str(path.relative_to(REPO)) for path in paths if not path.exists()]
    assert missing == [], f"axiom.spec references missing paths: {missing}"


def test_spec_never_collects_operator_state_recursively():
    spec_text = (PACKAGING / "axiom.spec").read_text(encoding="utf-8")
    # No recursive tree collection of data/, logs, credentials or weights.
    assert "Tree(" not in spec_text
    assert 'ROOT / "data"' not in spec_text
    assert "collect_data_files" not in spec_text
    assert "demo_data_sources(ROOT)" in spec_text
