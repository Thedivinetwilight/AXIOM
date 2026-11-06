"""Guard tests for .github/workflows/windows-release.yml.

The windows-release workflow must (a) preserve its Windows tests, installer /
portable acceptance checks, source-commit metadata and SHA-256 verification,
and (b) refuse every mixed-source build except the one pinned external target
(PR #9's exact build commit), for which the packaging kit is re-sourced from
that same commit so packaging files and application source can never mix.

The decision table below is a Python transliteration of the inline pwsh guard;
each simulated rule is anchored against the actual script text so the two
cannot silently drift.
"""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "windows-release.yml"
PINNED = "3a8db0a9d7dc2e1ea7a2ff6aeb3fd20db69de190"


def workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def resolve_step() -> str:
    text = workflow_text()
    start = text.index("name: Resolve the target commit and version")
    end = text.index("name: Check out the exact source commit")
    return text[start:end]


def decide_target(target: str, workflow_commit: str) -> str:
    """Transliteration of the pwsh resolve guard (see anchors below).

    Returns 'same-commit', 'pinned-external' or raises (refused).
    """
    if not target:
        target = workflow_commit
    if not re.fullmatch(r"[0-9a-f]{40}", target):
        raise ValueError(f"invalid commit {target!r}")
    if target != workflow_commit:
        if target != PINNED:
            raise ValueError("refusing mixed-source build")
        return "pinned-external"
    return "same-commit"


# --- preservation of the artifact-build contract --------------------------------

def test_workflow_dispatch_inputs_are_manual_and_publish_defaults_false():
    text = workflow_text()
    assert "workflow_dispatch:" in text
    commit = re.search(r"commit:\n\s+description:[^\n]+\n\s+required: false\n\s+default: ''", text)
    version = re.search(r"version:\n\s+description:[^\n]+\n\s+required: false\n\s+default: ''", text)
    publish = re.search(
        r"publish:\n\s+description:[^\n]+\n\s+required: false\n\s+default: false\n\s+type: boolean",
        text,
    )
    assert commit and version and publish


def test_publish_step_stays_gated_and_no_secrets_are_referenced():
    text = workflow_text()
    assert ("if: success() && github.event_name == 'workflow_dispatch' && inputs.publish"
            in text)
    assert "github.token" in text  # the only token: the workflow's own runtime token
    assert "secrets." not in text and "SECRETS." not in text


def test_windows_tests_installer_portable_metadata_and_checksums_preserved():
    text = workflow_text()
    for fragment in (
        "./build_windows.ps1 -ExpectedCommit",
        "./install_test.ps1 -DistDir dist -SetupPath $setup",
        "./portable_test.ps1 -PackagePath $package -ExpectedCommit",
        'if ($meta.commit -ne $env:TARGET_COMMIT)',
        "SHA256SUMS.txt",
        "Get-FileHash -Algorithm SHA256",
        "actions/upload-artifact@v4",
        "SOURCE_COMMIT.txt",
    ):
        assert fragment in text, f"missing preserved contract: {fragment}"


def test_checksums_are_commit_bound_and_build_logs_are_retained():
    text = workflow_text()
    # Header lines carry the exact commit and are excluded from the asset list.
    assert "-not $_.Trim().StartsWith('#')" in text
    # The build log the workflow itself tees must survive in the artifact.
    assert "src/packaging/windows/build-windows.log" in text
    build = build_script_text()
    assert "# commit:  $Head" in build
    assert "# version: $Version" in build
    # Every payload still gets a checksum row.
    assert "Get-FileHash -Algorithm SHA256 $_).Hash.ToLower()" in build


def test_manifest_header_lines_are_ignored_by_sha256sum():
    """`#` rows keep `sha256sum -c` working on the published manifest."""
    rows = [
        "# AXIOM Windows release checksums (SHA-256)",
        "# commit:  " + "a" * 40,
        "0" * 64 + "  AXIOM-Chart.exe",
    ]
    checksum_rows = [row for row in rows if row.strip() and not row.strip().startswith("#")]
    assert len(checksum_rows) == 1
    assert re.fullmatch(r"[0-9a-f]{64}  \S+", checksum_rows[0])


# --- the same-commit guard and its single pinned exception ------------------------

def test_guard_accepts_same_commit_and_pinned_commit_only():
    step = resolve_step()
    # Anchors tying the simulated decision table to the real pwsh text.
    assert "$pinnedExternalTarget = '%s'" % PINNED in step
    assert "if ($target -ne $workflowCommit)" in step
    assert "if ($target -ne $pinnedExternalTarget)" in step
    assert "refusing mixed-source build" in step

    assert decide_target("", "f" * 40) == "same-commit"
    assert decide_target("f" * 40, "f" * 40) == "same-commit"
    assert decide_target(PINNED, "f" * 40) == "pinned-external"
    for rejected in ("a" * 40, "3a8db0a9d7dc2e1ea7a2ff6aeb3fd20db69de191",
                     "3a8db0a", "not-a-sha"):
        with __import__("pytest").raises(ValueError):
            decide_target(rejected, "f" * 40)


def test_pinned_external_build_re_sources_kit_from_the_same_commit():
    step = resolve_step()
    fetch_at = step.index("git -C _kit fetch --depth 1 origin $target")
    checkout_at = step.index("git -C _kit checkout --force FETCH_HEAD")
    verify_at = step.index("if ($kitCommit -ne $target)")
    version_at = step.index("Get-Content _kit/packaging/windows/VERSION")
    # The kit is re-sourced and verified BEFORE any packaging file (the
    # version) is read from it, so every accepted build is single-commit.
    assert fetch_at < checkout_at < verify_at < version_at


def test_default_target_is_the_exact_workflow_commit():
    step = resolve_step()
    assert "if (-not $target) { $target = $workflowCommit }" in step
    text = workflow_text()
    # The source checkout always uses the resolved target, never a branch name.
    assert "ref: ${{ steps.resolve.outputs.TARGET_COMMIT }}" in text


def test_push_trigger_covers_release_branch_and_pr_branch_artifact_only():
    text = workflow_text()
    push_block = text[text.index("on:"):text.index("workflow_dispatch:")]
    assert re.search(r"push:\n(?:.*\n)*?\s+branches:\n\s+- arena/19f4cba3-axiom\n\s+- arena/d832c9d3-axiom", push_block)
    # The branch that produced the current Windows packaging work must also
    # produce its own evidence instead of relying on another branch's run.
    assert "- arena/8d3d005e-axiom" in push_block
    # A push-triggered run can never publish: the publish step is gated on a
    # dispatch event with the explicit publish input.
    assert ("if: success() && github.event_name == 'workflow_dispatch' && inputs.publish"
            in text)


BUILD_SCRIPT = Path(__file__).resolve().parents[1] / "packaging" / "windows" / "build_windows.ps1"


def build_script_text() -> str:
    return BUILD_SCRIPT.read_text(encoding="utf-8")


def test_build_identity_is_written_before_the_freeze_and_carried_in_metadata():
    text = build_script_text()
    written = text.index("Write the machine-readable build identity")
    freeze = text.index("Step '4. Freeze with PyInstaller'")
    assert written < freeze, "the identity file must exist before PyInstaller reads it"
    assert "app\\_build_identity.json" in text
    assert "build_identity = $Identity" in text  # travels in BUILD_METADATA.json
    assert "Remove-Item -Force $IdentityPath" in text  # never dirties a local tree


def test_frozen_smoke_gate_is_hash_anchored_to_the_staged_source():
    text = build_script_text()
    # Regex anchors alone accept stale bundles; the gate must compare the
    # served bytes against the staged source and the embedded identity.
    assert "served autonomy.js bytes match the staged source exactly" in text
    assert "frozen build identity names the exact commit" in text
    assert "served autonomy.js carries the BOD parse diagnostics" in text
    assert text.index("ServedJsHash") < text.index("frozen build identity names the exact commit")
    assert "build identity source hash matches" in text


def test_spec_bundles_the_identity_file_when_present():
    spec = (Path(__file__).resolve().parents[1] / "packaging" / "windows" / "axiom.spec").read_text(encoding="utf-8")
    assert "_build_identity.json" in spec
    assert "identity_datas" in spec
