"""Deterministic tests for the strategy research qualification gate.

Every case here is offline and synthetic: no provider is contacted, no
credential is read, no broker is reachable and no order is submitted.  The
fixture that reaches ``QUALIFIED`` is an explicit *mechanics* fixture — synthetic
prices registered as an imported capture with an operator attestation — and the
test asserts that the record itself admits the verification level and keeps zero
execution authority.
"""

from __future__ import annotations

import copy
import json
import math
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.data.corporate_actions import canonical_bars_sha256
from app.data.models import Candle
from app.model_lab import qualification as qual
from app.model_lab.datasets import DatasetManager
from app.model_lab.qualification import (
    QUALIFICATION_SCHEMA,
    RULES_VERSION,
    STATUS_BLOCKED,
    STATUS_QUALIFIED,
    STATUS_UNVERIFIED,
    QualificationBlocked,
    assert_no_execution_authority,
    compact_view,
    evaluate_qualification,
    qualification_authority,
    require_qualified,
    rules_document,
)
from app.model_lab.registry import ModelLabService
from app.model_lab.research_pipeline import (
    MA_GRID,
    RIDGE_GRID,
    RSI_GRID,
    declared_search_family,
    execute_research_pipeline,
    manifest_digest,
    split_chronological,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CLEAN_COMMIT = "c" * 40
SOFTWARE = {
    "commit": CLEAN_COMMIT,
    "commit_source": "TEST",
    "tree_dirty": False,
    "pipeline_schema": "axiom.model_lab.research_run.v1",
    "package_version": "test",
    "python": "3.11",
}
CONFORMANCE = {
    "schema": "axiom.provider-conformance.v1",
    "suite_version": "1.1.0",
    "data_source_version": "axiom-synthetic-fixtures-2026-11-06",
    "status": "PASS",
    "network_calls": 0,
    "broker_order_api_calls": 0,
    "live_trading": False,
    "providers": {"upstox": {"status": "PASS", "checks": [{"id": "a"}, {"id": "b"}]}},
}
PROVENANCE = {
    "kind": "READ_ONLY_PROVIDER_CAPTURE",
    "provider": "UPSTOX_V3",
    "captured_at": "2026-01-20T10:00:00+00:00",
    "attested": True,
    "attested_by": "test-suite",
    "note": "fixture attestation used to exercise the qualification mechanics",
}


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def make_bars(
    n: int = 420,
    *,
    amp: float = 0.02,
    period: float = 9.0,
    drift: float = 0.0002,
    symbol: str = "RELIANCE",
    start: datetime | None = None,
) -> list[Candle]:
    t0 = start or datetime(2026, 1, 2, 9, 15, tzinfo=UTC)
    bars: list[Candle] = []
    px = 1000.0
    for i in range(n):
        step = amp * math.sin(2 * math.pi * i / period) + drift
        nxt = max(50.0, px * (1.0 + step))
        bars.append(
            Candle(
                timestamp=t0 + timedelta(hours=i),
                open=round(px, 4),
                high=round(max(px, nxt) * 1.004, 4),
                low=round(min(px, nxt) * 0.996, 4),
                close=round(nxt, 4),
                volume=50_000 + (i % 11) * 2_000,
                symbol=symbol,
            )
        )
        px = nxt
    return bars


def action_payload(symbol: str = "RELIANCE") -> dict:
    return {
        "format": "axiom.corporate-actions",
        "format_version": 1,
        "provider": {"name": "TESTFEED", "version": "1"},
        "actions": [
            {
                "record_id": "T-1",
                "revision": 1,
                "market": "NSE",
                "symbol": symbol,
                "action_type": "CASH_DIVIDEND",
                "ex_date": "2026-01-20",
                "announced_at": "2026-01-05",
                "cash_per_share": 5.0,
                "currency": "INR",
                "source": "TESTFEED",
                "source_version": "1",
                "provenance": "synthetic fixture attestation",
            }
        ],
    }


def register_dataset(tmp_path: Path, bars: list[Candle], **overrides):
    """Register the fixture through the real dataset manager (real provenance code)."""
    manager = DatasetManager(tmp_path / "datasets")
    kwargs = {
        "market": "NSE",
        "symbol": "RELIANCE",
        "timeframe": "1h",
        "bars_or_csv": bars,
        "source": "Upstox Historical v3 (read-only capture)",
        "feed": "UPSTOX_V3",
        "corporate_action_adjustment": "RAW",
        "survivorship_aware": True,
        "real_market_data": True,
        "corporate_actions": action_payload(),
        "corporate_action_policy": "RAW",
    }
    kwargs.update(overrides)
    meta = manager.register_dataset(**kwargs)
    reloaded, reloaded_bars = manager.load_dataset(meta["dataset_id"])
    return manager, reloaded, reloaded_bars


def run_pipeline(meta: dict, bars: list[Candle], **overrides) -> dict:
    kwargs = {
        "dataset_meta": meta,
        "bars": bars,
        "market": "NSE",
        "timeframe": "1h",
        "software": dict(SOFTWARE),
    }
    kwargs.update(overrides)
    return execute_research_pipeline(**kwargs)


@pytest.fixture(scope="module")
def mechanics(tmp_path_factory) -> dict:
    """A manifest whose *research evidence* is complete (mechanics fixture)."""
    tmp = tmp_path_factory.mktemp("qual")
    bars = make_bars()
    manager, meta, loaded = register_dataset(tmp, bars)
    manifest = run_pipeline(meta, loaded)
    return {"manager": manager, "meta": meta, "bars": loaded, "manifest": manifest, "dir": tmp}


def qualified_record(mechanics: dict) -> dict:
    return evaluate_qualification(
        mechanics["manifest"],
        dataset_meta=mechanics["meta"],
        recomputed_dataset_sha256=canonical_bars_sha256(mechanics["bars"]),
        conformance=CONFORMANCE,
        provenance=PROVENANCE,
        expected_software_commit=CLEAN_COMMIT,
    )


def resign(manifest: dict) -> dict:
    """Deep-copy a manifest and re-derive its digest.

    Used to simulate a *self-consistent* manifest produced differently (e.g. by
    an older tool): the integrity check must still pass so the individual
    requirement checks are the thing under test.  Tampering without re-signing is
    tested separately.
    """
    out = copy.deepcopy(manifest)
    out.pop("manifest_sha256", None)
    out["manifest_sha256"] = manifest_digest(out)
    return out


def status_of(record: dict, requirement: str) -> str:
    for item in record["requirements"]:
        if item["requirement"] == requirement:
            return item["status"]
    raise AssertionError(f"requirement {requirement} missing from the record")


def reason_of(record: dict, requirement: str) -> str | None:
    for item in record["requirements"]:
        if item["requirement"] == requirement:
            return item["reason"]
    raise AssertionError(f"requirement {requirement} missing from the record")


# --------------------------------------------------------------------------- #
# 1. Chronological splits and holdout isolation
# --------------------------------------------------------------------------- #
def test_splits_are_strictly_chronological_and_embargoed(mechanics):
    split = split_chronological(mechanics["bars"], train_frac=0.60, val_frac=0.20, embargo_bars=5)
    assert split.train_end + 5 == split.val_start
    assert split.val_end + 5 == split.test_start
    manifest = mechanics["manifest"]
    assert manifest["splits"]["embargo_1_bars"] == 5
    assert manifest["splits"]["embargo_2_bars"] == 5
    assert manifest["splits"]["untouched_test"]["selection_touched_test"] is False
    assert manifest["search_family"]["holdout_used_for_selection"] is False


def test_holdout_bars_cannot_change_selection_or_scaling(tmp_path):
    """Mutating everything at/after the holdout must not move scaling, selection or ranking."""
    bars = make_bars()
    _manager, meta, loaded = register_dataset(tmp_path, bars)
    base = run_pipeline(meta, loaded)
    split = split_chronological(loaded, train_frac=0.60, val_frac=0.20, embargo_bars=5)

    mutated = list(loaded)
    for index in range(split.test_start, len(mutated)):
        bar = mutated[index]
        mutated[index] = Candle(
            timestamp=bar.timestamp,
            open=bar.open * 3.0,
            high=bar.high * 3.0,
            low=bar.low * 3.0,
            close=bar.close * 3.0,
            volume=bar.volume,
            symbol=bar.symbol,
        )
    moved = run_pipeline(meta, mutated)

    # 1. Feature scaling and fitted weights are identical (fit used train bars only).
    ridge_base = next(c for c in base["candidates"] if c["candidate_id"] == "axiom_linear_lagged")
    ridge_moved = next(c for c in moved["candidates"] if c["candidate_id"] == "axiom_linear_lagged")
    assert ridge_base["parameters"]["mean"] == ridge_moved["parameters"]["mean"]
    assert ridge_base["parameters"]["std"] == ridge_moved["parameters"]["std"]
    assert ridge_base["parameters"]["weights"] == ridge_moved["parameters"]["weights"]

    # 2. Selected variants (parameters) are identical per family.
    assert base["search_family"]["selected_variants"] == moved["search_family"]["selected_variants"]

    # 3. The whole ranking over the validation split is untouched.
    base_rank = [
        (v["variant_id"], v["validation_sharpe"]) for v in base["search_family"]["variants"]
    ]
    moved_rank = [
        (v["variant_id"], v["validation_sharpe"]) for v in moved["search_family"]["variants"]
    ]
    assert base_rank == moved_rank

    # 4. Train/validation metrics are identical; only the holdout moved.
    for cid in ("moving_average_tuned", "rsi_tuned", "axiom_linear_lagged"):
        b = next(c for c in base["candidates"] if c["candidate_id"] == cid)
        m = next(c for c in moved["candidates"] if c["candidate_id"] == cid)
        assert b["train"] == m["train"]
        assert b["validation"] == m["validation"]
        assert b["walk_forward_folds"] == m["walk_forward_folds"]
        assert b["untouched_test"] != m["untouched_test"]


def test_holdout_metrics_are_recorded_once_and_marked_untouched(mechanics):
    test_block = mechanics["manifest"]["splits"]["untouched_test"]
    assert test_block["selection_touched_test"] is False
    assert test_block["bars"] >= 25
    assert mechanics["manifest"]["search_family"]["selection_metric"] == "validation.sharpe"
    assert mechanics["manifest"]["search_family"]["selection_split"] == "validation"


# --------------------------------------------------------------------------- #
# 2. Pinned identities
# --------------------------------------------------------------------------- #
def test_manifest_pins_dataset_action_strategy_and_software_identity(mechanics):
    manifest = mechanics["manifest"]
    dataset = manifest["dataset"]
    assert dataset["sha256"] == mechanics["meta"]["sha256"]
    assert len(dataset["actions_sha256"]) == 64
    assert dataset["corporate_actions"]["status"] == "RAW"
    assert dataset["data_range"]["end_ts"] == mechanics["bars"][-1].timestamp.isoformat()
    assert manifest["strategy_versions"]["moving_average"] == "1"
    assert manifest["software"]["commit"] == CLEAN_COMMIT
    assert manifest["software"]["tree_dirty"] is False
    assert len(manifest["software"]["feature_set_sha256"]) == 64
    assert manifest["universe"]["symbols"] == ["RELIANCE"]
    assert manifest["benchmark"]["candidate_id"] == "buy_and_hold"
    for candidate in manifest["candidates"]:
        assert candidate["strategy_id"]
        assert candidate["strategy_version"]
    assert manifest_digest(manifest) == manifest["manifest_sha256"]


def test_manifest_digest_is_deterministic_across_runs(tmp_path):
    bars = make_bars()
    _manager, meta, loaded = register_dataset(tmp_path, bars)
    first = run_pipeline(meta, loaded)
    second = run_pipeline(meta, loaded)
    assert first["manifest_sha256"] == second["manifest_sha256"]
    assert first["search_family"] == second["search_family"]


# --------------------------------------------------------------------------- #
# 3. Synthetic / unverified / stale data stays ineligible
# --------------------------------------------------------------------------- #
def test_bundled_synthetic_fixture_is_blocked_and_never_qualified(tmp_path):
    service = ModelLabService(tmp_path / "lab", repo_data_dir=REPO_ROOT / "data")
    run = service.start_research_run(
        dataset_id="bundled_nse_synthetic_1h", async_execution=False, include_kronos=False
    )
    assert run["status"] == "COMPLETED"
    record = service.get_qualification(run["run_id"])
    assert record["status"] == STATUS_BLOCKED
    assert "SYNTHETIC_DATA" in [b["reason"] for b in record["blocked"]]
    assert status_of(record, "REAL_DATA_PROVENANCE") == "FAIL"
    assert "SYNTHETIC" in record["dataset"]["source_type"]
    # The label is never "qualified", whatever the metrics looked like.
    assert record["candidate"]["promotion_eligible_pipeline_flag"] in (True, False)
    assert record["status"] != STATUS_QUALIFIED


def test_synthetic_dataset_flag_is_enough_to_block_even_with_perfect_metrics(tmp_path):
    bars = make_bars()
    _manager, meta, loaded = register_dataset(
        tmp_path, bars, real_market_data=False, feed="SYNTHETIC_FIXTURE"
    )
    manifest = run_pipeline(meta, loaded)
    record = evaluate_qualification(
        manifest,
        dataset_meta=meta,
        recomputed_dataset_sha256=canonical_bars_sha256(loaded),
        conformance=CONFORMANCE,
        provenance=PROVENANCE,
        expected_software_commit=CLEAN_COMMIT,
    )
    assert status_of(record, "REAL_DATA_PROVENANCE") == "FAIL"
    assert reason_of(record, "REAL_DATA_PROVENANCE") == "SYNTHETIC_DATA"
    assert record["status"] == STATUS_BLOCKED


def test_real_labelled_dataset_without_attestation_is_unverified(mechanics):
    record = evaluate_qualification(
        mechanics["manifest"],
        dataset_meta=mechanics["meta"],
        recomputed_dataset_sha256=canonical_bars_sha256(mechanics["bars"]),
        conformance=CONFORMANCE,
    )
    assert status_of(record, "REAL_DATA_PROVENANCE") == "UNVERIFIED"
    assert reason_of(record, "REAL_DATA_PROVENANCE") == "PROVENANCE_ATTESTATION_MISSING"
    assert record["status"] == STATUS_UNVERIFIED
    assert record["verification_level"] == "OFFLINE_CONFORMANCE_ONLY"


def test_stale_data_is_blocked(mechanics):
    manifest = resign(mechanics["manifest"])
    manifest["completed_at"] = "2027-06-01T00:00:00+00:00"
    record = evaluate_qualification(
        manifest,
        dataset_meta=mechanics["meta"],
        recomputed_dataset_sha256=canonical_bars_sha256(mechanics["bars"]),
        conformance=CONFORMANCE,
        provenance=PROVENANCE,
    )
    assert status_of(record, "DATA_RECENCY") == "FAIL"
    assert reason_of(record, "DATA_RECENCY") == "STALE_DATA"
    assert record["status"] == STATUS_BLOCKED


def test_future_dated_bars_are_blocked(mechanics):
    manifest = resign(mechanics["manifest"])
    manifest["completed_at"] = "2025-01-01T00:00:00+00:00"
    record = evaluate_qualification(
        manifest,
        dataset_meta=mechanics["meta"],
        recomputed_dataset_sha256=canonical_bars_sha256(mechanics["bars"]),
        conformance=CONFORMANCE,
        provenance=PROVENANCE,
    )
    assert reason_of(record, "DATA_RECENCY") == "STALE_DATA"
    assert record["status"] == STATUS_BLOCKED


def test_corporate_action_incomplete_dataset_is_unverified(tmp_path):
    bars = make_bars()
    _manager, meta, loaded = register_dataset(
        tmp_path,
        bars,
        corporate_actions=None,
        corporate_action_adjustment="PROVIDER_DEFAULT_UNVERIFIED",
        corporate_action_policy="RAW",
    )
    manifest = run_pipeline(meta, loaded)
    record = evaluate_qualification(
        manifest,
        dataset_meta=meta,
        recomputed_dataset_sha256=canonical_bars_sha256(loaded),
        conformance=CONFORMANCE,
        provenance=PROVENANCE,
    )
    assert record["corporate_actions"]["verified"] is False
    assert status_of(record, "CORPORATE_ACTION_STATUS") == "UNVERIFIED"
    assert reason_of(record, "CORPORATE_ACTION_STATUS") == "CORPORATE_ACTIONS_UNVERIFIED"
    assert record["status"] != STATUS_QUALIFIED


def test_unsupported_corporate_action_types_block_qualification(tmp_path):
    bars = make_bars()
    payload = action_payload()
    payload["actions"].append(
        {
            "record_id": "T-2",
            "revision": 1,
            "market": "NSE",
            "symbol": "RELIANCE",
            "action_type": "BONUS_ISSUE",
            "ex_date": "2026-01-22",
            "announced_at": "2026-01-10",
            "source": "TESTFEED",
            "source_version": "1",
            "provenance": "synthetic fixture attestation",
        }
    )
    _manager, meta, loaded = register_dataset(tmp_path, bars, corporate_actions=payload)
    manifest = run_pipeline(meta, loaded)
    record = evaluate_qualification(
        manifest,
        dataset_meta=meta,
        recomputed_dataset_sha256=canonical_bars_sha256(loaded),
        conformance=CONFORMANCE,
        provenance=PROVENANCE,
    )
    assert record["corporate_actions"]["unsupported_actions"], record["corporate_actions"]
    assert status_of(record, "CORPORATE_ACTION_STATUS") == "FAIL"
    assert reason_of(record, "CORPORATE_ACTION_STATUS") == "CORPORATE_ACTIONS_UNSUPPORTED"
    assert record["status"] == STATUS_BLOCKED


def test_dataset_hash_mismatch_blocks(mechanics):
    record = evaluate_qualification(
        mechanics["manifest"],
        dataset_meta=mechanics["meta"],
        recomputed_dataset_sha256="0" * 64,
        conformance=CONFORMANCE,
        provenance=PROVENANCE,
    )
    assert reason_of(record, "DATASET_HASH_VERIFIED") == "DATASET_HASH_MISMATCH"
    assert record["status"] == STATUS_BLOCKED


def test_tampered_manifest_is_blocked(mechanics):
    tampered = copy.deepcopy(mechanics["manifest"])
    tampered["candidates"][0]["promotion_eligible"] = True
    record = evaluate_qualification(
        tampered,
        dataset_meta=mechanics["meta"],
        recomputed_dataset_sha256=canonical_bars_sha256(mechanics["bars"]),
        conformance=CONFORMANCE,
        provenance=PROVENANCE,
    )
    assert reason_of(record, "MANIFEST_INTEGRITY") == "MANIFEST_TAMPERED"
    assert record["status"] == STATUS_BLOCKED


def test_missing_provider_conformance_is_unverified(mechanics):
    record = evaluate_qualification(
        mechanics["manifest"],
        dataset_meta=mechanics["meta"],
        recomputed_dataset_sha256=canonical_bars_sha256(mechanics["bars"]),
        provenance=PROVENANCE,
    )
    assert status_of(record, "PROVIDER_CONFORMANCE") == "UNVERIFIED"
    assert reason_of(record, "PROVIDER_CONFORMANCE") == "PROVIDER_CONFORMANCE_MISSING"
    assert record["status"] == STATUS_UNVERIFIED


def test_failing_provider_conformance_blocks(mechanics):
    failing = copy.deepcopy(CONFORMANCE)
    failing["status"] = "FAIL"
    failing["providers"]["upstox"]["status"] = "FAIL"
    record = evaluate_qualification(
        mechanics["manifest"],
        dataset_meta=mechanics["meta"],
        recomputed_dataset_sha256=canonical_bars_sha256(mechanics["bars"]),
        conformance=failing,
        provenance=PROVENANCE,
    )
    assert reason_of(record, "PROVIDER_CONFORMANCE") == "PROVIDER_CONFORMANCE_FAILED"
    assert record["status"] == STATUS_BLOCKED


# --------------------------------------------------------------------------- #
# 4. Multiple testing covers the declared family
# --------------------------------------------------------------------------- #
def test_declared_family_matches_every_tuned_parameter_combination(mechanics):
    family = mechanics["manifest"]["search_family"]
    expected = len(MA_GRID) + len(RSI_GRID) + len(RIDGE_GRID)
    assert family["variants_declared"] == expected
    assert family["variants_evaluated"] == expected
    assert family["family_complete"] is True
    assert family["declared_before_search"] is True
    assert len(family["variants"]) == expected
    assert len(declared_search_family()) == expected
    ids = {v["variant_id"] for v in family["variants"]}
    assert len(ids) == expected


def test_correction_is_the_existing_haircut_applied_to_the_full_family(mechanics):
    from app.ensemble.calibration import multiple_testing_sharpe_haircut

    correction = mechanics["manifest"]["search_family"]["correction"]
    expected = multiple_testing_sharpe_haircut(
        correction["family_size"], correction["oos_bars"]
    )
    assert correction["family_size"] == mechanics["manifest"]["search_family"]["variants_declared"]
    assert correction["sharpe_haircut_threshold"] == expected
    assert correction["applied_before_selection"] is True
    assert correction["method"] == "bonferroni_ln_m_sharpe_haircut"
    for variant in mechanics["manifest"]["search_family"]["variants"]:
        assert variant["clears_multiple_testing_haircut"] == (
            variant["validation_sharpe"] > correction["sharpe_haircut_threshold"]
        )


def test_incomplete_family_manifest_blocks(mechanics):
    manifest = resign(mechanics["manifest"])
    manifest["search_family"]["variants"] = manifest["search_family"]["variants"][:5]
    record = evaluate_qualification(
        manifest,
        dataset_meta=mechanics["meta"],
        recomputed_dataset_sha256=canonical_bars_sha256(mechanics["bars"]),
        conformance=CONFORMANCE,
        provenance=PROVENANCE,
    )
    assert reason_of(record, "SEARCH_FAMILY_DECLARED") == "SEARCH_MANIFEST_INCOMPLETE"
    assert record["status"] == STATUS_BLOCKED


def test_correction_that_does_not_cover_the_family_blocks(mechanics):
    manifest = resign(mechanics["manifest"])
    manifest["search_family"]["correction"]["family_size"] = 1
    record = evaluate_qualification(
        manifest,
        dataset_meta=mechanics["meta"],
        recomputed_dataset_sha256=canonical_bars_sha256(mechanics["bars"]),
        conformance=CONFORMANCE,
        provenance=PROVENANCE,
    )
    assert reason_of(record, "MULTIPLE_TESTING") == "MULTIPLE_TESTING_INCOMPLETE"
    assert record["status"] == STATUS_BLOCKED


def test_candidate_below_the_haircut_blocks(mechanics):
    manifest = resign(mechanics["manifest"])
    threshold = manifest["search_family"]["correction"]["sharpe_haircut_threshold"]
    for candidate in manifest["candidates"]:
        if candidate.get("untouched_test") and candidate.get("validation"):
            candidate["validation"] = {**candidate["validation"], "sharpe": threshold / 2.0}
    record = evaluate_qualification(
        manifest,
        dataset_meta=mechanics["meta"],
        recomputed_dataset_sha256=canonical_bars_sha256(mechanics["bars"]),
        conformance=CONFORMANCE,
        provenance=PROVENANCE,
    )
    assert reason_of(record, "MULTIPLE_TESTING") == "MULTIPLE_TESTING_INCOMPLETE"
    assert record["status"] == STATUS_BLOCKED


def test_pipeline_rejects_variants_below_the_family_haircut(tmp_path):
    # A flat, trendless series gives negative validation Sharpe for the tuned
    # rules, so the haircut must appear among the rejection reasons.
    flat = make_bars(amp=0.00005, drift=0.0, n=300)
    _manager, meta, loaded = register_dataset(tmp_path, flat, symbol="RELIANCE")
    manifest = run_pipeline(meta, loaded)
    threshold = manifest["search_family"]["correction"]["sharpe_haircut_threshold"]
    for candidate in manifest["candidates"]:
        if candidate["candidate_id"] in {"moving_average_tuned", "rsi_tuned"}:
            reasons = " ".join(candidate["rejection_reasons"])
            assert candidate["validation"]["sharpe"] <= threshold
            assert "multiple-testing haircut" in reasons


# --------------------------------------------------------------------------- #
# 5. Explicit blockers for missing evidence
# --------------------------------------------------------------------------- #
def test_missing_costs_block_rather_than_pass(mechanics):
    manifest = resign(mechanics["manifest"])
    manifest["cost_assumptions"] = {}
    record = evaluate_qualification(
        manifest,
        dataset_meta=mechanics["meta"],
        recomputed_dataset_sha256=canonical_bars_sha256(mechanics["bars"]),
        conformance=CONFORMANCE,
        provenance=PROVENANCE,
    )
    assert status_of(record, "COSTS_MODELLED") == "FAIL"
    assert reason_of(record, "COSTS_MODELLED") == "COSTS_MISSING"
    assert record["status"] == STATUS_BLOCKED


def test_failed_cost_stress_blocks(mechanics):
    manifest = resign(mechanics["manifest"])
    for candidate in manifest["candidates"]:
        if candidate.get("stressed_test"):
            candidate["stressed_test"]["net_return_pct"] = -0.5
    record = evaluate_qualification(
        manifest,
        dataset_meta=mechanics["meta"],
        recomputed_dataset_sha256=canonical_bars_sha256(mechanics["bars"]),
        conformance=CONFORMANCE,
        provenance=PROVENANCE,
    )
    assert reason_of(record, "COSTS_MODELLED") == "COST_STRESS_FAILED"
    assert record["status"] == STATUS_BLOCKED


def test_missing_benchmark_is_unverified(mechanics):
    manifest = resign(mechanics["manifest"])
    manifest["candidates"] = [c for c in manifest["candidates"] if c["candidate_id"] != "buy_and_hold"]
    record = evaluate_qualification(
        manifest,
        dataset_meta=mechanics["meta"],
        recomputed_dataset_sha256=canonical_bars_sha256(mechanics["bars"]),
        conformance=CONFORMANCE,
        provenance=PROVENANCE,
    )
    assert reason_of(record, "BENCHMARK") == "BENCHMARK_MISSING"
    assert status_of(record, "BENCHMARK") == "UNVERIFIED"
    assert record["status"] != STATUS_QUALIFIED
    assert "BENCHMARK_MISSING" in [
        u["reason"] for u in record["unverified"]
    ]


def test_underperforming_benchmark_blocks(mechanics):
    manifest = resign(mechanics["manifest"])
    for candidate in manifest["candidates"]:
        if candidate["candidate_id"] == "buy_and_hold":
            candidate["untouched_test"]["net_return_pct"] = 99.0
    record = evaluate_qualification(
        manifest,
        dataset_meta=mechanics["meta"],
        recomputed_dataset_sha256=canonical_bars_sha256(mechanics["bars"]),
        conformance=CONFORMANCE,
        provenance=PROVENANCE,
    )
    assert reason_of(record, "BENCHMARK") == "UNDERPERFORMS_BENCHMARK"
    assert record["status"] == STATUS_BLOCKED


def test_insufficient_observations_block(mechanics):
    manifest = resign(mechanics["manifest"])
    manifest["dataset"]["bar_count"] = 120
    manifest["splits"]["validation"]["bars"] = 10
    manifest["splits"]["untouched_test"]["bars"] = 8
    record = evaluate_qualification(
        manifest,
        dataset_meta=mechanics["meta"],
        recomputed_dataset_sha256=canonical_bars_sha256(mechanics["bars"]),
        conformance=CONFORMANCE,
        provenance=PROVENANCE,
    )
    assert reason_of(record, "MINIMUM_OBSERVATIONS") == "INSUFFICIENT_OBSERVATIONS"
    assert record["status"] == STATUS_BLOCKED


def test_missing_walk_forward_folds_block(mechanics):
    manifest = resign(mechanics["manifest"])
    for candidate in manifest["candidates"]:
        candidate.pop("walk_forward_folds", None)
    record = evaluate_qualification(
        manifest,
        dataset_meta=mechanics["meta"],
        recomputed_dataset_sha256=canonical_bars_sha256(mechanics["bars"]),
        conformance=CONFORMANCE,
        provenance=PROVENANCE,
    )
    assert reason_of(record, "OOS_CONSISTENCY") == "OOS_INCONSISTENT"
    assert record["status"] == STATUS_BLOCKED


def test_excess_drawdown_blocks(mechanics):
    manifest = resign(mechanics["manifest"])
    for candidate in manifest["candidates"]:
        if candidate.get("untouched_test"):
            candidate["untouched_test"]["max_drawdown_pct"] = 61.5
    record = evaluate_qualification(
        manifest,
        dataset_meta=mechanics["meta"],
        recomputed_dataset_sha256=canonical_bars_sha256(mechanics["bars"]),
        conformance=CONFORMANCE,
        provenance=PROVENANCE,
    )
    assert reason_of(record, "RISK_LIMITS") == "RISK_LIMIT_EXCEEDED"
    assert record["status"] == STATUS_BLOCKED


def test_dirty_tree_and_commit_mismatch_are_unverified(mechanics):
    dirty = copy.deepcopy(mechanics["manifest"])
    dirty["software"] = {**dirty["software"], "tree_dirty": True}
    record = evaluate_qualification(
        resign(dirty),
        dataset_meta=mechanics["meta"],
        recomputed_dataset_sha256=canonical_bars_sha256(mechanics["bars"]),
        conformance=CONFORMANCE,
        provenance=PROVENANCE,
    )
    assert reason_of(record, "SOFTWARE_COMMIT") == "SOFTWARE_TREE_DIRTY"
    assert record["status"] == STATUS_UNVERIFIED

    other = evaluate_qualification(
        mechanics["manifest"],
        dataset_meta=mechanics["meta"],
        recomputed_dataset_sha256=canonical_bars_sha256(mechanics["bars"]),
        conformance=CONFORMANCE,
        provenance=PROVENANCE,
        expected_software_commit="d" * 40,
    )
    assert reason_of(other, "SOFTWARE_COMMIT") == "COMMIT_MISMATCH"
    assert other["status"] == STATUS_UNVERIFIED


# --------------------------------------------------------------------------- #
# 6. The qualified path, and its (zero) execution authority
# --------------------------------------------------------------------------- #
def test_mechanics_fixture_qualifies_and_admits_its_verification_level(mechanics):
    record = qualified_record(mechanics)
    assert record["status"] == STATUS_QUALIFIED, record["blocked"]
    assert record["counts"]["FAIL"] == 0
    assert record["counts"]["UNVERIFIED"] == 0
    assert record["verification_level"] == "OPERATOR_ATTESTED_NOT_INDEPENDENTLY_VERIFIED"
    assert "not independent" in record["provenance"]["verification_level"].lower() or (
        "OPERATOR_ATTESTED" in record["provenance"]["verification_level"]
    )
    assert record["scope"] == "RESEARCH_ONLY"
    assert record["schema"] == QUALIFICATION_SCHEMA
    assert record["rules_version"] == RULES_VERSION
    assert record["open_reviews"]
    assert any("guarantee" in note.lower() for note in record["limitations"])


def test_qualified_record_carries_no_execution_authority(mechanics):
    record = qualified_record(mechanics)
    execution = record["execution"]
    assert execution == {
        "live_trading_authorized": False,
        "paper_arming_effect": "NONE",
        "order_path_effect": "NONE",
        "bypasses_risk_checks": False,
        "issues_execution_permit": False,
        "enables_live_market_live": False,
    }
    assert_no_execution_authority(record)
    authority = qualification_authority(record)
    assert authority["authority"] == "NONE"
    assert authority["issues_execution_permit"] is False
    require_qualified(record)  # does not raise: still no authority


def test_record_claiming_authority_is_rejected():
    forged = {
        "scope": "RESEARCH_ONLY",
        "status": STATUS_QUALIFIED,
        "execution": {
            "live_trading_authorized": True,
            "paper_arming_effect": "NONE",
            "order_path_effect": "NONE",
            "bypasses_risk_checks": False,
            "issues_execution_permit": False,
            "enables_live_market_live": False,
        },
    }
    with pytest.raises(QualificationBlocked):
        assert_no_execution_authority(forged)
    with pytest.raises(QualificationBlocked):
        require_qualified(forged)


def test_require_qualified_refuses_unverified_and_missing_records(mechanics):
    unverified = evaluate_qualification(
        mechanics["manifest"],
        dataset_meta=mechanics["meta"],
        recomputed_dataset_sha256=canonical_bars_sha256(mechanics["bars"]),
        conformance=CONFORMANCE,
    )
    with pytest.raises(QualificationBlocked):
        require_qualified(unverified)
    with pytest.raises(QualificationBlocked):
        require_qualified(None)


def test_qualification_does_not_promote_or_arm_models(tmp_path):
    """A registry verdict never changes registry status and never enables execution."""
    service = ModelLabService(tmp_path / "lab", repo_data_dir=REPO_ROOT / "data")
    run = service.start_research_run(
        dataset_id="bundled_nse_synthetic_1h", async_execution=False
    )
    record = service.get_qualification(run["run_id"])
    assert record["status"] == STATUS_BLOCKED
    models = service.list_models()
    assert models, "the run must register candidate models"
    for model in models:
        assert model["registry_status"] in {"GATE_REJECTED", "VALIDATED_CANDIDATE"}
        assert model["live_execution_authorized"] is False
        assert model["execution_authority"] == "NONE"
        assert model["qualification_status"] == record["status"]
    # promotion still requires the research gate, not the qualification verdict
    ineligible = [m for m in models if not m["promotion_eligible"]]
    if ineligible:
        with pytest.raises(ValueError):
            service.promote_model(ineligible[0]["model_id"])
    overview = service.overview()
    assert overview["live_execution_authorized"] is False
    assert overview["qualification"]["execution_authority"] == "NONE"


def test_gate_module_never_references_broker_order_endpoints():
    source = Path(qual.__file__).read_text(encoding="utf-8")
    for needle in ("/v2/orders", "paper-api.alpaca.markets", "api.alpaca.markets", "place_order"):
        assert needle not in source
    assert "ExecutionPermit(" not in source


# --------------------------------------------------------------------------- #
# 7. Reproduction
# --------------------------------------------------------------------------- #
def test_re_evaluating_the_same_manifest_reproduces_the_record(mechanics):
    first = qualified_record(mechanics)
    second = qualified_record(mechanics)
    assert first["record_sha256"] == second["record_sha256"]
    assert {k: v for k, v in first.items() if k != "evaluated_at"} == {
        k: v for k, v in second.items() if k != "evaluated_at"
    }


def test_re_running_the_same_manifest_reproduces_the_record_end_to_end(tmp_path):
    bars = make_bars()
    _manager, meta, loaded = register_dataset(tmp_path, bars)
    records = []
    for _ in range(2):
        manifest = run_pipeline(meta, loaded)
        records.append(
            evaluate_qualification(
                manifest,
                dataset_meta=meta,
                recomputed_dataset_sha256=canonical_bars_sha256(loaded),
                conformance=CONFORMANCE,
                provenance=PROVENANCE,
                expected_software_commit=CLEAN_COMMIT,
            )
        )
    assert records[0]["run"]["manifest_sha256"] == records[1]["run"]["manifest_sha256"]
    assert records[0]["record_sha256"] == records[1]["record_sha256"]
    assert records[0]["status"] == STATUS_QUALIFIED


def test_registry_persists_one_immutable_record_per_run(tmp_path):
    service = ModelLabService(tmp_path / "lab", repo_data_dir=REPO_ROOT / "data")
    run = service.start_research_run(
        dataset_id="bundled_nse_synthetic_1h", async_execution=False
    )
    first = service.get_qualification(run["run_id"])
    second = service.get_qualification(run["run_id"])
    assert first == second
    with sqlite3.connect(service.db_path) as conn:
        rows = conn.execute(
            "SELECT run_id, status, record_sha256 FROM model_lab_qualifications"
        ).fetchall()
    assert rows == [(run["run_id"], first["status"], first["record_sha256"])]


def test_rules_document_lists_every_requirement_and_the_scope():
    doc = rules_document()
    assert doc["scope"] == "RESEARCH_ONLY"
    assert doc["execution_authority"] == "NONE"
    assert [r["requirement"] for r in doc["requirements"]] == list(qual.REQUIREMENT_IDS)
    assert len(doc["requirements"]) == len(qual.RULE_SPECS) == 20
    assert doc["thresholds"]["min_total_bars"] >= 250
    assert any("never guarantees profitability" in item for item in doc["limitations"])


def test_invalid_manifest_inputs_are_rejected(mechanics):
    with pytest.raises(ValueError):
        evaluate_qualification("not a manifest")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        evaluate_qualification({"schema": "some.other.schema"})


def test_compact_view_is_honest_when_there_is_no_record():
    view = compact_view(None)
    assert view["status"] == STATUS_UNVERIFIED
    assert view["reason"] == "NO_QUALIFICATION_RECORD"
    assert view["execution_authority"] == "NONE"


# --------------------------------------------------------------------------- #
# 8. API + CLI surfaces
# --------------------------------------------------------------------------- #
def test_api_exposes_rules_and_run_qualification(tmp_path):
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from app.api.server import create_app
    from app.config.settings import Settings

    settings = replace(Settings(), api_token="qual-tok", database_url=f"sqlite:///{tmp_path}/q.db")
    headers = {"Authorization": "Bearer qual-tok"}
    with TestClient(create_app(settings, data_dir=tmp_path)) as web:
        rules = web.get("/api/model-lab/qualification/rules", headers=headers)
        assert rules.status_code == 200
        assert rules.json()["scope"] == "RESEARCH_ONLY"
        assert len(rules.json()["requirements"]) == 20

        overview = web.get("/api/model-lab/overview", headers=headers)
        assert overview.status_code == 200
        body = overview.json()
        assert body["qualification"]["execution_authority"] == "NONE"
        assert body["live_execution_authorized"] is False

        started = web.post(
            "/api/model-lab/runs",
            headers=headers,
            json={"dataset_id": "bundled_nse_synthetic_1h", "async_execution": False},
        )
        assert started.status_code == 200
        run = started.json()
        assert run["qualification"]["status"] == STATUS_BLOCKED
        assert run["qualification"]["execution_authority"] == "NONE"

        record = web.get(
            f"/api/model-lab/runs/{run['run_id']}/qualification", headers=headers
        )
        assert record.status_code == 200
        assert record.json()["schema"] == QUALIFICATION_SCHEMA
        assert record.json()["execution"]["live_trading_authorized"] is False

        missing = web.get("/api/model-lab/runs/mlrun-doesnotexist/qualification", headers=headers)
        assert missing.status_code == 404


def test_cli_scores_a_manifest_file(mechanics, tmp_path):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(mechanics["manifest"]), encoding="utf-8")
    meta_path = tmp_path / "meta.json"
    meta_path.write_text(json.dumps(mechanics["meta"]), encoding="utf-8")
    prov_path = tmp_path / "provenance.json"
    prov_path.write_text(json.dumps(PROVENANCE), encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "app",
            "research-qualify",
            "--manifest",
            str(manifest_path),
            "--dataset-meta",
            str(meta_path),
            "--provenance",
            str(prov_path),
            "--json",
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=180,
        check=False,
    )
    assert proc.returncode in (0, 1, 3), proc.stderr
    payload = json.loads(proc.stdout[proc.stdout.index("{"):])
    assert payload["schema"] == QUALIFICATION_SCHEMA
    assert payload["execution"]["live_trading_authorized"] is False
    # Provider conformance is not supplied on this path, so it can never be green.
    assert payload["status"] in (STATUS_UNVERIFIED, STATUS_BLOCKED)
    assert proc.returncode == (0 if payload["status"] == STATUS_QUALIFIED else 3 if payload["status"] == STATUS_UNVERIFIED else 1)
