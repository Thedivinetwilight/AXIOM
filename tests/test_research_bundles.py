"""Tests for portable research-run bundles: export, offline verify, replay.

Everything here is offline and synthetic. The pipeline runs on the bundled
synthetic fixture, so the resulting qualification is BLOCKED by design — these
tests assert that a bundle *preserves* that verdict, never that it lifts it.
"""

from __future__ import annotations

import copy
import json
import os
import pathlib
import socket
import zipfile
from typing import Any, Callable

import pytest

from app.model_lab import bundle as B
from app.model_lab.registry import ModelLabService
from app.model_lab.research_pipeline import manifest_digest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"


# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def service(tmp_path_factory) -> ModelLabService:
    store = tmp_path_factory.mktemp("bundle_store")
    svc = ModelLabService(store, repo_data_dir=DATA_DIR)
    run = svc.start_research_run(
        dataset_id="bundled_nse_synthetic_1h", async_execution=False, include_kronos=False
    )
    assert run["status"] == "COMPLETED"
    svc._test_run_id = run["run_id"]  # type: ignore[attr-defined]
    return svc


@pytest.fixture(scope="module")
def run_id(service: ModelLabService) -> str:
    return service._test_run_id  # type: ignore[attr-defined]


@pytest.fixture(scope="module")
def document(service: ModelLabService, run_id: str) -> dict[str, Any]:
    return B.export_run_bundle(service, run_id)


def payload_of(document: dict[str, Any], role: str) -> bytes:
    path = path_of(document, role)
    entry = document["inline"][path]
    if entry.get("encoding") == "base64":
        return B.base64.b64decode(entry["content"])  # type: ignore[attr-defined]
    return str(entry["content"]).encode("utf-8")


def path_of(document: dict[str, Any], role: str) -> str:
    for entry in document["artifacts"]:
        if entry["role"] == role:
            return str(entry["path"])
    raise AssertionError(f"role {role} not in bundle")


def json_of(document: dict[str, Any], role: str) -> dict[str, Any]:
    return json.loads(payload_of(document, role))


def reseal(document: dict[str, Any], updates: dict[str, bytes]) -> dict[str, Any]:
    """Re-sign a sealed document after replacing payloads.

    This is the *adversary* model: an attacker who recomputes every hash, path and
    the bundle digest.  Verification must still reject it on semantic grounds.
    """
    new = copy.deepcopy(document)
    for entry in new["artifacts"]:
        role = entry["role"]
        if role not in updates:
            continue
        payload = updates[role]
        digest = B.sha256_bytes(payload)
        new["inline"].pop(entry["path"], None)
        entry["sha256"] = digest
        entry["size"] = len(payload)
        entry["path"] = B.artifact_path(digest, entry["media_type"])
        new["inline"][entry["path"]] = {"encoding": "utf-8", "content": payload.decode("utf-8")}
    core = {
        key: value
        for key, value in new.items()
        if key not in {"bundle_sha256", "created_at", "generator", "inline"}
    }
    new["bundle_sha256"] = B.sha256_json(core)
    return new


def with_artifact_bytes(document: dict[str, Any], role: str, payload: bytes) -> dict[str, Any]:
    """Replace an artifact's bytes *without* updating the index (raw tampering)."""
    new = copy.deepcopy(document)
    new["inline"][path_of(new, role)] = {"encoding": "utf-8", "content": payload.decode("utf-8")}
    return new


def codes(report: dict[str, Any]) -> list[str]:
    return list(report["reason_codes"])


# --------------------------------------------------------------------------- #
# 1. Export
# --------------------------------------------------------------------------- #
def test_export_is_deterministic(document, service, run_id):
    again = B.export_run_bundle(service, run_id)
    assert again["bundle_sha256"] == document["bundle_sha256"]
    assert [a["sha256"] for a in again["artifacts"]] == [a["sha256"] for a in document["artifacts"]]
    assert [a["path"] for a in again["artifacts"]] == [a["path"] for a in document["artifacts"]]
    assert again["artifacts"] == document["artifacts"]
    assert again["sections"] == document["sections"]


def test_export_enumerates_every_artifact_with_size_and_hash(document):
    roles = {a["role"] for a in document["artifacts"]}
    assert set(B.REQUIRED_ROLES).issubset(roles)
    assert roles <= set(B.ALL_ROLES)
    assert len(roles) == len(document["artifacts"]), "roles must be unique"
    inline_paths = set(document["inline"])
    assert inline_paths == {a["path"] for a in document["artifacts"]}
    for entry in document["artifacts"]:
        payload = payload_of(document, entry["role"])
        assert entry["size"] == len(payload)
        assert entry["sha256"] == B.sha256_bytes(payload)
        assert entry["path"].startswith(f"{B.ARTIFACT_DIR}/{entry['sha256']}.")


def test_export_pins_the_required_inputs(document, service, run_id):
    manifest = json_of(document, "run_manifest")
    dataset_meta = json_of(document, "dataset_meta")
    run_config = json_of(document, "run_config")
    feature_schema = json_of(document, "feature_schema")
    software = json_of(document, "software_fingerprint")
    qual_inputs = json_of(document, "qualification_inputs")
    record = json_of(document, "qualification_record")

    assert manifest_digest(manifest) == manifest["manifest_sha256"]
    assert dataset_meta["sha256"] == manifest["dataset"]["sha256"]
    assert run_config["dataset_id"] == manifest["dataset"]["dataset_id"]
    assert feature_schema["feature_names"] == list(B.FEATURE_NAMES)
    assert software["commit"] == manifest["software"]["commit"]
    assert qual_inputs["recomputed_dataset_sha256"] == manifest["dataset"]["sha256"]
    assert record["schema"] == "axiom.research.qualification.v1"

    sections = document["sections"]
    for name in (
        "search_family",
        "folds",
        "benchmark",
        "universe",
        "strategy_versions",
        "costs",
        "dependency_lock",
        "run_outputs",
    ):
        assert name in sections, name
        assert len(str(sections[name]["sha256"])) == 64
    assert sections["search_family"]["variants_declared"] >= 1
    assert sections["folds"]["holdout_bars"] >= 25


def test_export_records_no_execution_authority_and_no_secrets(document):
    assert document["execution_view"]["authority"] == "NONE"
    policy = document["content_policy"]
    assert policy["credentials_included"] is False
    assert policy["user_data_dirs_included"] is False
    assert policy["network_calls"] == 0
    assert policy["broker_order_api_calls"] == 0
    assert policy["live_trading"] is False
    assert document["qualification"]["execution_authority"] == "NONE"
    assert "never re-scores" in document["qualification"]["upgrade_effect"]
    assert "does not prove the provider's data is genuine" in document["integrity_scope"]
    for entry in document["artifacts"]:
        assert B.find_secret(payload_of(document, entry["role"]).decode("utf-8", "replace")) is None


def test_export_refuses_a_payload_that_looks_like_a_credential(service, run_id, tmp_path):
    manifest = service.get_run(run_id)["manifest"]
    poisoned = copy.deepcopy(manifest)
    poisoned["dataset"]["source"] = "capture (access_token=AbCdEf0123456789XyZ)"
    poisoned.pop("manifest_sha256")
    poisoned["manifest_sha256"] = manifest_digest(poisoned)
    inputs = B.RunInputs(
        run_id=run_id,
        manifest=poisoned,
        qualification_record=service.get_qualification(run_id),
        dataset_meta=service.datasets.load_dataset(service.get_run(run_id)["dataset_id"])[0],
        bars=service.datasets.load_dataset(service.get_run(run_id)["dataset_id"])[1],
        run_config=service.get_run(run_id)["config"],
        dependency_lock=b"pytest==1",
        conformance_report=service.conformance_report(),
    )
    with pytest.raises(B.BundleError) as exc:
        B.build_bundle_document(inputs)
    assert exc.value.code == "SECRET_DETECTED"


def test_export_refuses_a_tampered_manifest(service, run_id):
    inputs = B._gather_run_inputs(service, run_id)
    inputs.manifest = copy.deepcopy(inputs.manifest)
    inputs.manifest["candidates"][0]["promotion_eligible"] = True
    with pytest.raises(B.BundleError) as exc:
        B.build_bundle_document(inputs)
    assert exc.value.code == "MANIFEST_TAMPERED"


def test_export_refuses_mismatched_bars(service, run_id):
    inputs = B._gather_run_inputs(service, run_id)
    inputs.bars = inputs.bars[:-1]
    with pytest.raises(B.BundleError) as exc:
        B.build_bundle_document(inputs)
    assert exc.value.code == "DATASET_HASH_MISMATCH"


def test_export_requires_a_dependency_lock(monkeypatch, tmp_path):
    monkeypatch.setattr(B, "DEPENDENCY_LOCK_CANDIDATES", ("does-not-exist.lock",))
    with pytest.raises(B.BundleError) as exc:
        B.read_dependency_lock(tmp_path)
    assert exc.value.code == "DEPENDENCY_LOCK_MISSING"


# --------------------------------------------------------------------------- #
# 2. Verification across containers
# --------------------------------------------------------------------------- #
def test_verify_sealed_document_passes(document):
    report = B.verify_bundle(document)
    assert report["status"] == "PASS", report["findings"]
    assert report["reason_codes"] == []
    assert report["container"] == "SEALED_JSON"
    assert report["checked"]["artifacts"] == len(document["artifacts"])
    assert report["qualification"]["matches"] is True
    assert report["network_calls"] == 0
    assert report["broker_order_api_calls"] == 0
    assert report["live_trading"] is False
    assert report["execution_authority"] == "NONE"


def test_verify_directory_and_zip_containers(document, tmp_path):
    directory = B.write_bundle(document, tmp_path / "bundle_dir")
    archive = B.write_bundle(document, tmp_path / "bundle.zip", archive=True)
    assert (pathlib.Path(directory) / B.BUNDLE_INDEX_NAME).is_file()
    assert zipfile.is_zipfile(archive)
    for target, kind in ((directory, "DIRECTORY"), (archive, "ZIP")):
        report = B.verify_bundle(target)
        assert report["status"] == "PASS", report["findings"]
        assert report["container"] == kind
        assert report["bundle_sha256"] == document["bundle_sha256"]


def test_verify_survives_a_round_trip_through_disk(document, tmp_path):
    sealed_path = tmp_path / "bundle.json"
    sealed_path.write_text(json.dumps(document), encoding="utf-8")
    report = B.verify_bundle(sealed_path)
    assert report["status"] == "PASS", report["findings"]
    assert report["container"] == "SEALED_JSON"


# --------------------------------------------------------------------------- #
# 3. Tampering with each artifact class
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("role", list(B.REQUIRED_ROLES))
def test_raw_tampering_is_detected_for_every_required_role(document, role):
    original = payload_of(document, role)
    tampered = with_artifact_bytes(document, role, original + b"\n")
    report = B.verify_bundle(tampered)
    assert report["status"] == "FAIL"
    assert "ARTIFACT_HASH_MISMATCH" in codes(report)
    assert report["execution_authority"] == "NONE"


def test_tampering_an_optional_corporate_action_sidecar_is_detected(service, run_id):
    inputs = B._gather_run_inputs(service, run_id)
    inputs.corporate_actions_bytes = json.dumps(
        {
            "format": "axiom.corporate-actions",
            "format_version": 1,
            "provider": {"name": "TEST"},
            "actions": [],
        }
    ).encode()
    document = B.build_bundle_document(inputs)
    assert any(a["role"] == "corporate_actions" for a in document["artifacts"])
    report = B.verify_bundle(document)
    assert report["status"] == "FAIL"
    assert "ACTION_HASH_MISMATCH" in codes(report)
    # Raw byte tampering of that same sidecar is caught earlier, by the hash.
    payload = payload_of(document, "corporate_actions")
    report = B.verify_bundle(with_artifact_bytes(document, "corporate_actions", payload + b" "))
    assert "ARTIFACT_HASH_MISMATCH" in codes(report)


def test_altered_manifest_is_rejected(document):
    manifest = json_of(document, "run_manifest")
    manifest["candidates"][0]["promotion_eligible"] = True
    report = B.verify_bundle(reseal(document, {"run_manifest": B.pretty(manifest)}))
    assert report["status"] == "FAIL"
    assert "MANIFEST_TAMPERED" in codes(report)


def test_altered_bars_are_rejected(document):
    rows = json.loads(payload_of(document, "dataset_bars"))
    rows[7]["close"] = float(rows[7]["close"]) * 1.25
    report = B.verify_bundle(reseal(document, {"dataset_bars": B.pretty(rows)}))
    assert report["status"] == "FAIL"
    assert "DATASET_HASH_MISMATCH" in codes(report)


def test_altered_run_config_is_rejected(document):
    config = json_of(document, "run_config")
    config["spread_bps"] = 0.0
    report = B.verify_bundle(reseal(document, {"run_config": B.pretty(config)}))
    assert report["status"] == "FAIL"
    assert {"RUN_CONFIG_MISMATCH", "SECTION_DIGEST_MISMATCH"} & set(codes(report))


def test_altered_software_fingerprint_is_rejected(document):
    software = json_of(document, "software_fingerprint")
    software["commit"] = "0" * 40
    report = B.verify_bundle(reseal(document, {"software_fingerprint": B.pretty(software)}))
    assert report["status"] == "FAIL"
    assert "SOFTWARE_FINGERPRINT_MISMATCH" in codes(report)


def test_altered_section_pin_is_rejected(document):
    new = copy.deepcopy(document)
    new["sections"]["search_family"]["sha256"] = "1" * 64
    core = {k: v for k, v in new.items() if k not in {"bundle_sha256", "created_at", "generator", "inline"}}
    new["bundle_sha256"] = B.sha256_json(core)
    report = B.verify_bundle(new)
    assert report["status"] == "FAIL"
    assert "SECTION_DIGEST_MISMATCH" in codes(report)


def test_altered_index_run_block_is_rejected(document):
    new = copy.deepcopy(document)
    new["run"]["manifest_sha256"] = "2" * 64
    core = {k: v for k, v in new.items() if k not in {"bundle_sha256", "created_at", "generator", "inline"}}
    new["bundle_sha256"] = B.sha256_json(core)
    report = B.verify_bundle(new)
    assert report["status"] == "FAIL"
    assert "INDEX_RUN_MISMATCH" in codes(report)


def test_bundle_digest_covers_the_index(document):
    new = copy.deepcopy(document)
    new["limits"]["max_artifacts"] = 999
    report = B.verify_bundle(new)
    assert report["status"] == "FAIL"
    assert "BUNDLE_DIGEST_MISMATCH" in codes(report)


def test_missing_required_artifact_is_rejected(document):
    new = copy.deepcopy(document)
    dropped = path_of(new, "dataset_bars")
    new["artifacts"] = [a for a in new["artifacts"] if a["role"] != "dataset_bars"]
    new["inline"].pop(dropped, None)
    core = {k: v for k, v in new.items() if k not in {"bundle_sha256", "created_at", "generator", "inline"}}
    new["bundle_sha256"] = B.sha256_json(core)
    report = B.verify_bundle(new)
    assert report["status"] == "FAIL"
    assert "ARTIFACT_ROLE_MISSING" in codes(report)
    assert report["qualification"].get("bundled_status") in (None, "BLOCKED")


def test_missing_file_in_directory_bundle_is_rejected(document, tmp_path):
    directory = pathlib.Path(B.write_bundle(document, tmp_path / "d"))
    target = directory / path_of(document, "run_manifest")
    target.unlink()
    report = B.verify_bundle(directory)
    assert report["status"] == "FAIL"
    assert "ARTIFACT_NOT_IN_BUNDLE" in codes(report)


def test_duplicate_roles_and_paths_are_rejected(document):
    duplicated = copy.deepcopy(document)
    duplicated["artifacts"].append(copy.deepcopy(duplicated["artifacts"][0]))
    core = {
        k: v for k, v in duplicated.items() if k not in {"bundle_sha256", "created_at", "generator", "inline"}
    }
    duplicated["bundle_sha256"] = B.sha256_json(core)
    report = B.verify_bundle(duplicated)
    assert report["status"] == "FAIL"
    assert {"ARTIFACT_DUPLICATE_ROLE", "ARTIFACT_DUPLICATE_PATH"} & set(codes(report))


def test_unreferenced_file_in_a_directory_bundle_is_rejected(document, tmp_path):
    directory = pathlib.Path(B.write_bundle(document, tmp_path / "extra"))
    (directory / B.ARTIFACT_DIR / f"{'a' * 64}.json").write_text("{}", encoding="utf-8")
    report = B.verify_bundle(directory)
    assert report["status"] == "FAIL"
    assert "ARTIFACT_UNREFERENCED" in codes(report)


# --------------------------------------------------------------------------- #
# 4. Path traversal and unsafe paths
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "bad_path",
    [
        "artifacts/../../etc/passwd",
        "/etc/passwd",
        "artifacts/..%2f..%2fpasswd",
        "artifacts/sub/../../x.json",
        "..\\windows\\system32\\config",
    ],
)
def test_path_traversal_is_rejected(document, bad_path):
    new = copy.deepcopy(document)
    new["artifacts"][0]["path"] = bad_path
    core = {k: v for k, v in new.items() if k not in {"bundle_sha256", "created_at", "generator", "inline"}}
    new["bundle_sha256"] = B.sha256_json(core)
    report = B.verify_bundle(new)
    assert report["status"] == "FAIL"
    assert {"ARTIFACT_PATH_UNSAFE", "ARTIFACT_PATH_NOT_CONTENT_ADDRESSED"} & set(codes(report))
    assert not B.lookahead_safe_name(bad_path)


def test_zip_slip_member_is_rejected(document, tmp_path):
    archive_path = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        index = {k: v for k, v in copy.deepcopy(document).items() if k != "inline"}
        index["artifacts"][0]["path"] = "artifacts/../../escape.json"
        archive.writestr(B.BUNDLE_INDEX_NAME, B.pretty(index))
        archive.writestr("artifacts/../../escape.json", "{}")
    report = B.verify_bundle(archive_path)
    assert report["status"] == "FAIL"
    assert "ARTIFACT_PATH_UNSAFE" in codes(report)


def test_symlinked_artifact_is_rejected(document, tmp_path):
    directory = pathlib.Path(B.write_bundle(document, tmp_path / "sym"))
    real = directory / path_of(document, "run_config")
    outside = tmp_path / "outside.json"
    outside.write_bytes(real.read_bytes())
    real.unlink()
    os.symlink(outside, real)
    report = B.verify_bundle(directory)
    assert report["status"] == "FAIL"
    assert "ARTIFACT_SYMLINK" in codes(report)


def test_non_content_addressed_path_is_rejected(document):
    new = copy.deepcopy(document)
    entry = new["artifacts"][0]
    new["inline"]["plain.json"] = new["inline"].pop(entry["path"])
    entry["path"] = "plain.json"
    core = {k: v for k, v in new.items() if k not in {"bundle_sha256", "created_at", "generator", "inline"}}
    new["bundle_sha256"] = B.sha256_json(core)
    report = B.verify_bundle(new)
    assert report["status"] == "FAIL"
    assert "ARTIFACT_PATH_NOT_CONTENT_ADDRESSED" in codes(report)


# --------------------------------------------------------------------------- #
# 5. Versions and limits
# --------------------------------------------------------------------------- #
def test_unsupported_bundle_version_is_rejected(document):
    new = copy.deepcopy(document)
    new["format_version"] = B.BUNDLE_FORMAT_VERSION + 1
    core = {k: v for k, v in new.items() if k not in {"bundle_sha256", "created_at", "generator", "inline"}}
    new["bundle_sha256"] = B.sha256_json(core)
    report = B.verify_bundle(new)
    assert report["status"] == "FAIL"
    assert "UNSUPPORTED_BUNDLE_VERSION" in codes(report)


def test_unsupported_bundle_schema_is_rejected(document):
    new = copy.deepcopy(document)
    new["schema"] = "axiom.research.bundle.v0"
    core = {k: v for k, v in new.items() if k not in {"bundle_sha256", "created_at", "generator", "inline"}}
    new["bundle_sha256"] = B.sha256_json(core)
    report = B.verify_bundle(new)
    assert report["status"] == "FAIL"
    assert "UNSUPPORTED_BUNDLE_SCHEMA" in codes(report)
    assert report["bundle_schema"] == "axiom.research.bundle.v0"


def test_oversized_artifact_is_rejected(document):
    report = B.verify_bundle(document, limits=B.BundleLimits(max_artifact_bytes=64))
    assert report["status"] == "FAIL"
    assert "ARTIFACT_OVERSIZED" in codes(report)


def test_too_many_artifacts_is_rejected(document):
    report = B.verify_bundle(document, limits=B.BundleLimits(max_artifacts=2))
    assert report["status"] == "FAIL"
    assert "ARTIFACTS_TOO_MANY" in codes(report)


def test_total_size_limit_is_enforced(document):
    report = B.verify_bundle(document, limits=B.BundleLimits(max_total_bytes=1024, max_artifact_bytes=1 << 30))
    assert report["status"] == "FAIL"
    assert "BUNDLE_TOO_LARGE" in codes(report)


def test_missing_bundle_path_is_reported(tmp_path):
    report = B.verify_bundle(tmp_path / "nope.json")
    assert report["status"] == "FAIL"
    assert "BUNDLE_NOT_FOUND" in codes(report)


def test_directory_without_index_is_reported(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    report = B.verify_bundle(empty)
    assert report["status"] == "FAIL"
    assert "BUNDLE_MANIFEST_MISSING" in codes(report)


# --------------------------------------------------------------------------- #
# 6. Qualification can never be upgraded
# --------------------------------------------------------------------------- #
def test_synthetic_run_stays_blocked_in_the_bundle(document):
    assert document["run"]["qualification_status"] == "BLOCKED"
    assert "SYNTHETIC_DATA" in document["qualification"]["blocked"]
    record = json_of(document, "qualification_record")
    assert record["status"] == "BLOCKED"
    assert any(item["reason"] == "SYNTHETIC_DATA" for item in record["blocked"])
    assert record["execution"]["live_trading_authorized"] is False


def test_forged_qualified_status_is_detected(document):
    record = json_of(document, "qualification_record")
    record["status"] = "QUALIFIED"
    report = B.verify_bundle(reseal(document, {"qualification_record": B.pretty(record)}))
    assert report["status"] == "FAIL"
    assert "QUALIFICATION_ALTERED" in codes(report)
    assert report["qualification"]["matches"] is False


def test_fully_resigned_forgery_is_still_detected(document):
    record = json_of(document, "qualification_record")
    record["status"] = "QUALIFIED"
    record["blocked"] = []
    record["counts"] = {"PASS": 20, "FAIL": 0, "UNVERIFIED": 0, "NOT_APPLICABLE": 0}
    # The attacker also recomputes the record's own digest and re-seals everything.
    body = {k: v for k, v in record.items() if k not in {"record_sha256", "evaluated_at", "run_completed_at"}}
    record["record_sha256"] = B.sha256_json(body)
    forged = reseal(document, {"qualification_record": B.pretty(record)})
    forged["qualification"]["status"] = "QUALIFIED"
    forged["qualification"]["record_sha256"] = record["record_sha256"]
    forged["run"]["qualification_status"] = "QUALIFIED"
    core = {k: v for k, v in forged.items() if k not in {"bundle_sha256", "created_at", "generator", "inline"}}
    forged["bundle_sha256"] = B.sha256_json(core)
    report = B.verify_bundle(forged)
    assert report["status"] == "FAIL"
    assert "QUALIFICATION_ALTERED" in codes(report)
    # and replay refuses to run it at all
    replay = B.replay_bundle(forged)
    assert replay["status"] == "UNVERIFIED"
    assert "REPLAY_NOT_REPRODUCIBLE" in replay["reason_codes"]


def test_record_claiming_execution_authority_is_rejected(document):
    record = json_of(document, "qualification_record")
    record["execution"]["live_trading_authorized"] = True
    report = B.verify_bundle(reseal(document, {"qualification_record": B.pretty(record)}))
    assert report["status"] == "FAIL"
    assert "QUALIFICATION_SCOPE_VIOLATION" in codes(report)


def test_bundle_preserves_the_attestation_level(document):
    record = json_of(document, "qualification_record")
    assert document["qualification"]["verification_level"] == record["verification_level"]
    assert document["qualification"]["status"] == record["status"]
    assert "INDEPENDENT_PROVENANCE_REVIEW" in record["open_reviews"]


# --------------------------------------------------------------------------- #
# 7. Replay
# --------------------------------------------------------------------------- #
def test_replay_reproduces_the_pinned_manifest(document):
    report = B.replay_bundle(document)
    assert report["status"] in {"REPRODUCED", "REPRODUCED_WITH_ENVIRONMENT_DRIFT"}, report["findings"]
    assert report["reproduced"]["manifest_sha256"] == report["pinned"]["manifest_sha256"]
    assert report["comparisons"] == []
    assert report["verification"]["status"] == "PASS"
    assert report["execution_authority"] == "NONE"
    assert report["network_calls"] == 0
    assert report["broker_order_api_calls"] == 0
    assert report["live_trading"] is False


def test_replay_never_upgrades_the_qualification(document):
    report = B.replay_bundle(document)
    assert report["qualification"]["bundled_status"] == "BLOCKED"
    assert report["qualification"]["effective_status"] == "BLOCKED"
    assert report["qualification"]["upgraded"] is False


def test_replay_reports_environment_drift(document):
    report = B.replay_bundle(document)
    pinned = report["environment"]["pinned"]
    assert "commit" in pinned and "python" in pinned
    for item in report["environment"]["drift"]:
        assert item["field"] in B._ENVIRONMENT_FIELDS
    strict = B.replay_bundle(document, strict_environment=True)
    if report["environment"]["drift"]:
        assert strict["status"] == "DIVERGED"
    else:
        assert strict["status"] in {"REPRODUCED", "REPRODUCED_WITH_ENVIRONMENT_DRIFT"}


def test_strict_replay_without_drift_is_still_reproduced(document, monkeypatch):
    """strict_environment only escalates *existing* drift; a clean machine stays REPRODUCED."""
    import app.model_lab.provenance as provenance

    pinned = json_of(document, "software_fingerprint")
    monkeypatch.setattr(provenance, "software_fingerprint", lambda: dict(pinned))
    report = B.replay_bundle(document, strict_environment=True)
    assert report["environment"]["drift"] == []
    assert report["status"] == "REPRODUCED"
    assert report["qualification"]["effective_status"] == "BLOCKED"


def test_replay_refuses_a_bundle_that_does_not_verify(document):
    broken = with_artifact_bytes(document, "run_manifest", b"{}\n")
    report = B.replay_bundle(broken)
    assert report["status"] == "UNVERIFIED"
    assert "REPLAY_NOT_REPRODUCIBLE" in report["reason_codes"]


def test_replay_detects_a_substituted_bar_series(document):
    rows = json.loads(payload_of(document, "dataset_bars"))
    for row in rows:
        row["close"] = float(row["close"]) + 1.0
        row["open"] = float(row["open"]) + 1.0
    swapped = reseal(document, {"dataset_bars": B.pretty(rows)})
    report = B.replay_bundle(swapped)
    # The bundle no longer verifies (the bars contradict the pinned dataset hash),
    # so replay refuses rather than reproducing numbers that were never evaluated.
    assert report["status"] == "UNVERIFIED"
    assert "DATASET_HASH_MISMATCH" in report["verification"]["reason_codes"]


def test_replay_tolerance_is_documented_and_configurable(document):
    loose = B.replay_bundle(document, abs_tol=1.0, rel_tol=1.0)
    tight = B.replay_bundle(document, abs_tol=0.0, rel_tol=0.0)
    assert loose["tolerance"] == {"abs": 1.0, "rel": 1.0}
    assert tight["tolerance"] == {"abs": 0.0, "rel": 0.0}
    assert tight["status"] in {"REPRODUCED", "REPRODUCED_WITH_ENVIRONMENT_DRIFT"}
    assert tight["reproduced"]["manifest_sha256"] == tight["pinned"]["manifest_sha256"]


def test_replay_reports_kronos_runs_as_unverified(service, run_id):
    from app.model_lab.qualification import evaluate_qualification

    inputs = B._gather_run_inputs(service, run_id)
    manifest = copy.deepcopy(inputs.manifest)
    kronos = [c for c in manifest["candidates"] if c["candidate_id"] == "kronos_ensemble"]
    assert kronos, "the manifest always declares the Kronos ablation"
    kronos[0]["status"] = "EVALUATED_GENUINE_KRONOS"
    manifest.pop("manifest_sha256")
    manifest["manifest_sha256"] = manifest_digest(manifest)
    inputs.manifest = manifest
    # Build a *self-consistent* bundle: the record is what the verifier will re-derive.
    inputs.qualification_record = evaluate_qualification(
        manifest,
        dataset_meta=inputs.dataset_meta,
        recomputed_dataset_sha256=str(manifest["dataset"]["sha256"]),
        conformance=B._stable_conformance(inputs.conformance_report),
        expected_software_commit=inputs.expected_software_commit,
    )
    document = B.build_bundle_document(inputs)
    assert B.verify_bundle(document)["status"] == "PASS"
    report = B.replay_bundle(document)
    assert report["status"] == "UNVERIFIED"
    assert report["reason_codes"] == ["REPLAY_REQUIRES_KRONOS"]
    assert "mock" in json.dumps(report["findings"])
    assert report["qualification"]["upgraded"] is False


# --------------------------------------------------------------------------- #
# 8. No network, no broker orders
# --------------------------------------------------------------------------- #
def test_export_verify_and_replay_open_no_sockets(document, monkeypatch, service, run_id):
    def _no_connect(*_args, **_kwargs):
        raise AssertionError("a bundle operation attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", _no_connect, raising=True)
    monkeypatch.setattr(socket.socket, "connect_ex", _no_connect, raising=True)
    fresh = B.export_run_bundle(service, run_id)
    assert B.verify_bundle(fresh)["status"] == "PASS"
    replay = B.replay_bundle(fresh)
    assert replay["status"] in {"REPRODUCED", "REPRODUCED_WITH_ENVIRONMENT_DRIFT"}
    assert replay["network_calls"] == 0
    assert replay["broker_order_api_calls"] == 0


def test_bundle_module_has_no_order_surface():
    source = pathlib.Path(B.__file__).read_text(encoding="utf-8")
    for needle in ("/v2/orders", "paper-api.alpaca.markets", "api.alpaca.markets", "place_order", "ExecutionPermit("):
        assert needle not in source


# --------------------------------------------------------------------------- #
# 9. CLI and API
# --------------------------------------------------------------------------- #
def test_cli_export_verify_replay_round_trip(document, service, run_id, tmp_path):
    import subprocess
    import sys

    store = str(service.storage_dir)
    out = tmp_path / "cli_bundle"
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}

    proc = subprocess.run(
        [sys.executable, "-m", "app", "research-bundle", "export", "--run", run_id, "--store", store,
         "--output", str(out)],
        capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=300, check=False, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert "bundle exported" in proc.stdout

    proc = subprocess.run(
        [sys.executable, "-m", "app", "research-bundle", "verify", "--bundle", str(out), "--json"],
        capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=300, check=False, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout[proc.stdout.index("{"):])
    assert report["status"] == "PASS"

    proc = subprocess.run(
        [sys.executable, "-m", "app", "research-bundle", "replay", "--bundle", str(out), "--json"],
        capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=300, check=False, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    replay = json.loads(proc.stdout[proc.stdout.index("{"):])
    assert replay["reproduced"]["manifest_sha256"] == replay["pinned"]["manifest_sha256"]
    assert replay["qualification"]["effective_status"] == "BLOCKED"


def test_cli_verify_reports_failure_exit_code(document, tmp_path):
    import subprocess
    import sys

    sealed = tmp_path / "bad.json"
    sealed.write_text(json.dumps(with_artifact_bytes(document, "run_manifest", b"{}\n")), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "app", "research-bundle", "verify", "--bundle", str(sealed)],
        capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=300, check=False,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
    )
    assert proc.returncode == 1
    assert "ARTIFACT_HASH_MISMATCH" in proc.stdout


def test_api_exports_verifies_and_replays(tmp_path):
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from app.api.server import create_app
    from app.config.settings import Settings

    settings = replace(Settings(), api_token="bundle-tok", database_url=f"sqlite:///{tmp_path}/b.db")
    headers = {"Authorization": "Bearer bundle-tok"}
    with TestClient(create_app(settings, data_dir=tmp_path)) as web:
        fmt = web.get("/api/model-lab/bundles/format", headers=headers)
        assert fmt.status_code == 200
        body = fmt.json()
        assert body["schema"] == B.BUNDLE_SCHEMA
        assert body["guarantees"]["can_upgrade_qualification"] is False
        assert "ARTIFACT_HASH_MISMATCH" in body["reason_codes"]

        run = web.post(
            "/api/model-lab/runs",
            headers=headers,
            json={"dataset_id": "bundled_nse_synthetic_1h", "async_execution": False},
        ).json()
        exported = web.post(f"/api/model-lab/runs/{run['run_id']}/bundle", headers=headers)
        assert exported.status_code == 200
        doc = exported.json()
        assert doc["schema"] == B.BUNDLE_SCHEMA
        assert doc["execution_view"]["authority"] == "NONE"

        verified = web.post("/api/model-lab/bundles/verify", headers=headers, json={"bundle": doc})
        assert verified.status_code == 200
        assert verified.json()["status"] == "PASS"

        replayed = web.post("/api/model-lab/bundles/replay", headers=headers, json={"bundle": doc})
        assert replayed.status_code == 200
        assert replayed.json()["qualification"]["effective_status"] == "BLOCKED"
        assert replayed.json()["qualification"]["upgraded"] is False

        assert web.post("/api/model-lab/runs/nope/bundle", headers=headers).status_code == 404
