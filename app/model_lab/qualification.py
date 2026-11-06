"""Strategy research qualification gate (RESEARCH ONLY — never an execution permit).

One manifest in, one auditable decision out.  The gate does not re-run a
backtest and it does not invent evidence: it reads what the research pipeline
already recorded (dataset hashes, corporate-action provenance, chronological
splits, cost assumptions, the pre-declared search family and its
multiple-testing correction, the untouched-test metrics, the benchmark and the
software identity), adds the provider-conformance and provenance attestations an
operator supplies, and decides ``QUALIFIED`` / ``UNVERIFIED`` / ``BLOCKED``.

Design rules that make the decision safe to publish:

* **Fail closed.**  A missing artifact is never treated as a pass: it becomes an
  ``UNVERIFIED`` requirement with a stable reason code, and the run cannot carry
  a green label while any requirement is unverified.
* **Exchangeable evidence.**  Every requirement names the manifest path or input
  it read, so a reviewer can re-derive the verdict by hand.
* **No execution authority.**  ``QUALIFIED`` says "this research record is
  complete, leakage-free, cost-aware and provenance-pinned at a stated
  verification level".  It never arms a strategy, never enables LIVE trading,
  never issues an ``ExecutionPermit`` and never bypasses a risk check.  The
  record states this explicitly and :func:`assert_no_execution_authority` proves
  it on any record.
* **Synthetic data cannot qualify.**  Synthetic/demo fixtures are rejected at the
  provenance requirement before any performance metric is looked at, so a
  fixture may test the mechanics of this gate but can never produce a qualified
  strategy.
* **Deterministic.**  ``record_sha256`` covers everything except ``evaluated_at``,
  so re-evaluating the same manifest yields the same digest.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.data.corporate_actions import AdjustmentStatus, block_for_meta
from app.model_lab.research_pipeline import FEATURE_NAMES, manifest_digest

QUALIFICATION_SCHEMA = "axiom.research.qualification.v1"
RULES_VERSION = "1.0.0"

STATUS_QUALIFIED = "QUALIFIED"
STATUS_UNVERIFIED = "UNVERIFIED"
STATUS_BLOCKED = "BLOCKED"

PASS = "PASS"
FAIL = "FAIL"
UNVERIFIED = "UNVERIFIED"
NOT_APPLICABLE = "NOT_APPLICABLE"

#: Verification levels a record can carry.  Only the first two can ever appear on
#: a QUALIFIED record; the gate cannot verify an attestation by itself.
VERIFICATION_LEVELS = {
    "OPERATOR_ATTESTED_NOT_INDEPENDENTLY_VERIFIED": (
        "Dataset provenance rests on an operator-supplied attestation. Nothing here "
        "independently confirms the upstream provider, the capture window or the "
        "corporate-action feed."
    ),
    "OFFLINE_CONFORMANCE_ONLY": (
        "Only deterministic offline adapter conformance was available; no provider "
        "endpoint was contacted."
    ),
    "NONE": "No provenance evidence was supplied at all.",
}

#: Default thresholds.  They are deliberately conservative and are echoed into
#: every record so a reviewer can see exactly what was required.
DEFAULTS: dict[str, Any] = {
    "min_total_bars": 250,
    "min_validation_bars": 40,
    "min_holdout_bars": 25,
    "min_trades_per_split": 5,
    "min_oos_folds_positive": 0.60,
    "max_holdout_drawdown_pct": 25.0,
    "max_data_age_days": 400,
    "require_benchmark_outperformance": True,
    "require_dirty_tree_clean": True,
    "min_stress_multiplier": 1.25,
}

#: Statuses of a corporate-action block that count as "handled, not ignored".
VERIFIED_ACTION_STATUSES = frozenset(
    {
        AdjustmentStatus.RAW.value,
        AdjustmentStatus.SPLIT_ADJUSTED.value,
        AdjustmentStatus.DIVIDEND_ADJUSTED.value,
    }
)

_ADJUSTED_STATUSES = frozenset(
    {AdjustmentStatus.SPLIT_ADJUSTED.value, AdjustmentStatus.DIVIDEND_ADJUSTED.value}
)


class QualificationBlocked(RuntimeError):
    """Raised when a caller tries to use a non-QUALIFIED record as authority."""


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _requirement(
    requirement: str,
    status: str,
    *,
    reason: str = "",
    detail: str = "",
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "requirement": requirement,
        "status": status,
        "reason": reason or None,
        "detail": detail,
        "evidence": evidence or {},
    }


@dataclass(frozen=True)
class RuleSpec:
    requirement: str
    title: str
    group: str
    question: str


#: The published rule set — also returned verbatim by :func:`rules_document` so
#: the API/UI/docs and the evaluator can never drift apart.
RULE_SPECS: tuple[RuleSpec, ...] = (
    RuleSpec("DATASET_IDENTITY", "Dataset identity pinned", "DATA",
             "Are market, symbol, timeframe and dataset id recorded in the manifest?"),
    RuleSpec("DATASET_HASH_VERIFIED", "Dataset hash re-verified", "DATA",
             "Does the stored dataset hash equal a fresh hash of the evaluated bars?"),
    RuleSpec("ACTION_DATA_HASH", "Corporate-action data pinned", "PROVENANCE",
             "Is an action-book hash recorded when corporate actions are in scope?"),
    RuleSpec("REAL_DATA_PROVENANCE", "Real market data provenance", "PROVENANCE",
             "Is the dataset real provider data with an attested capture, not a fixture?"),
    RuleSpec("CORPORATE_ACTION_STATUS", "Corporate actions verified", "PROVENANCE",
             "Is the corporate-action status explicit, verified and free of unsupported actions?"),
    RuleSpec("PROVIDER_CONFORMANCE", "Provider adapter conformance", "PROVENANCE",
             "Does the read-only conformance suite pass for this market's provider?"),
    RuleSpec("CHRONOLOGICAL_SPLITS", "Chronological splits, holdout untouched", "METHOD",
             "Train < validation < test, embargoed, with the holdout never used for selection?"),
    RuleSpec("MINIMUM_OBSERVATIONS", "Minimum observations and trades", "METHOD",
             "Enough bars per split and enough trades in validation and holdout?"),
    RuleSpec("DATA_RECENCY", "Data not stale or in the future", "DATA",
             "Is the last bar plausible relative to the run and inside the freshness window?"),
    RuleSpec("SEARCH_FAMILY_DECLARED", "Search family declared and complete", "METHOD",
             "Was every searched variant declared before search and all of them recorded?"),
    RuleSpec("MULTIPLE_TESTING", "Multiple-testing correction applied", "METHOD",
             "Does the reported variant clear the family-wide Sharpe haircut?"),
    RuleSpec("OOS_CONSISTENCY", "Out-of-sample consistency", "METHOD",
             "Do validation, walk-forward folds and the untouched holdout agree in sign?"),
    RuleSpec("PIPELINE_GATE", "Existing promotion gate accepted", "METHOD",
             "Did the pipeline's own validation + cost-stress gate accept the reported candidate?"),
    RuleSpec("COSTS_MODELLED", "Costs and cost stress", "COSTS",
             "Are spread, slippage, fees and a stressed-cost rerun recorded and survivable?"),
    RuleSpec("BENCHMARK", "Benchmark comparison", "METHOD",
             "Is a same-split comparator recorded and beaten on the holdout?"),
    RuleSpec("RISK_LIMITS", "Risk limits respected", "RISK",
             "Is the holdout drawdown inside the configured limit?"),
    RuleSpec("STRATEGY_VERSION", "Strategy and parameter identity", "REPRO",
             "Is the strategy id, version, parameter set and feature schema pinned?"),
    RuleSpec("UNIVERSE", "Universe declared", "REPRO",
             "Are the evaluated symbols and survivorship treatment recorded?"),
    RuleSpec("SOFTWARE_COMMIT", "Software commit pinned", "REPRO",
             "Is the producing commit recorded, clean and equal to the expected commit?"),
    RuleSpec("MANIFEST_INTEGRITY", "Manifest hash intact", "REPRO",
             "Does manifest_sha256 still match a fresh digest of the manifest body?"),
)

REQUIREMENT_IDS: tuple[str, ...] = tuple(spec.requirement for spec in RULE_SPECS)


def rules_document() -> dict[str, Any]:
    """Machine-readable copy of the published qualification rules."""
    return {
        "schema": "axiom.research.qualification.rules.v1",
        "rules_version": RULES_VERSION,
        "qualification_schema": QUALIFICATION_SCHEMA,
        "statuses": {
            STATUS_QUALIFIED: "every requirement passed",
            STATUS_UNVERIFIED: "nothing failed, but at least one requirement lacks evidence",
            STATUS_BLOCKED: "at least one requirement failed",
        },
        "requirement_statuses": [PASS, FAIL, UNVERIFIED, NOT_APPLICABLE],
        "thresholds": dict(DEFAULTS),
        "requirements": [
            {"requirement": s.requirement, "title": s.title, "group": s.group, "question": s.question}
            for s in RULE_SPECS
        ],
        "scope": "RESEARCH_ONLY",
        "execution_authority": "NONE",
        "limitations": list(_STANDING_LIMITATIONS),
    }


_STANDING_LIMITATIONS = (
    "Qualification never guarantees profitability, and a historical simulation is not a forecast.",
    "A QUALIFIED record is research evidence only: it does not arm a strategy, does not enable "
    "LIVE-MARKET LIVE trading and cannot mint an ExecutionPermit.",
    "Offline provider conformance proves the adapter contract, not live connectivity or data quality.",
    "Operator-supplied provenance attestations are recorded as attestations; they are not independent "
    "verification of the upstream provider.",
    "Synthetic fixtures can exercise these mechanics but can never produce a QUALIFIED result.",
)


# --------------------------------------------------------------------------- #
# Evidence readers
# --------------------------------------------------------------------------- #
def _get(mapping: Any, *path: str, default: Any = None) -> Any:
    node: Any = mapping
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def _is_sha256(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == 64 and all(c in "0123456789abcdef" for c in text)


def _as_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and out not in (float("inf"), float("-inf")) else None


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _provider_for_market(market: str) -> str:
    return "upstox" if str(market or "").strip().upper() == "NSE" else "alpaca"


def conformance_evidence(report: dict[str, Any] | None, market: str) -> dict[str, Any]:
    """Reduce a provider-conformance report to the facts a review needs.

    Timestamps and per-check detail are dropped on purpose: the evidence must be
    stable enough to hash, and it must never imply live connectivity.
    """
    if not isinstance(report, dict):
        return {}
    provider = _provider_for_market(market)
    providers = report.get("providers") if isinstance(report.get("providers"), dict) else {}
    entry = providers.get(provider) if isinstance(providers.get(provider), dict) else {}
    return {
        "schema": report.get("schema"),
        "suite_version": report.get("suite_version"),
        "data_source_version": report.get("data_source_version"),
        "overall_status": report.get("status"),
        "provider": provider,
        "provider_status": entry.get("status"),
        "provider_checks": len(entry.get("checks") or []),
        "network_calls": report.get("network_calls"),
        "broker_order_api_calls": report.get("broker_order_api_calls"),
        "live_trading": report.get("live_trading"),
        "offline_only": report.get("network_calls") == 0,
    }


def provenance_evidence(provenance: dict[str, Any] | None) -> dict[str, Any]:
    """Normalise an operator provenance attestation (never invented here)."""
    if not isinstance(provenance, dict) or not provenance:
        return {}
    return {
        "kind": str(provenance.get("kind") or ""),
        "provider": str(provenance.get("provider") or ""),
        "captured_at": str(provenance.get("captured_at") or ""),
        "attested": bool(provenance.get("attested") or provenance.get("verified")),
        "attested_by": str(provenance.get("attested_by") or ""),
        "note": str(provenance.get("note") or "")[:400],
        "verification_level": "OPERATOR_ATTESTED_NOT_INDEPENDENTLY_VERIFIED",
    }


# --------------------------------------------------------------------------- #
# Gate
# --------------------------------------------------------------------------- #
def evaluate_qualification(
    manifest: dict[str, Any],
    *,
    dataset_meta: dict[str, Any] | None = None,
    recomputed_dataset_sha256: str = "",
    conformance: dict[str, Any] | None = None,
    provenance: dict[str, Any] | None = None,
    expected_software_commit: str = "",
    thresholds: dict[str, Any] | None = None,
    evaluated_at: datetime | str | None = None,
) -> dict[str, Any]:
    """Return an immutable qualification record for one research manifest.

    ``manifest`` is the exact document ``execute_research_pipeline`` produced.
    Nothing is mutated and nothing is fetched.
    """
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be a JSON object")
    schema = str(manifest.get("schema") or "")
    if schema != "axiom.model_lab.research_run.v1":
        raise ValueError(f"Unsupported research manifest schema {schema!r}")

    limits = {**DEFAULTS, **(thresholds or {})}
    market = str(manifest.get("market") or "").strip().upper()
    candidates = [c for c in (manifest.get("candidates") or []) if isinstance(c, dict)]
    dataset = manifest.get("dataset") if isinstance(manifest.get("dataset"), dict) else {}
    meta = dataset_meta if isinstance(dataset_meta, dict) else {}
    action_block = block_for_meta({**meta, **dataset}) if (meta or dataset) else block_for_meta(dataset)
    checks: list[dict[str, Any]] = []

    # ---------------- DATA ------------------------------------------------ #
    identity_missing = [
        field
        for field, value in (
            ("dataset_id", dataset.get("dataset_id")),
            ("sha256", dataset.get("sha256")),
            ("symbol", dataset.get("symbol")),
            ("market", manifest.get("market")),
            ("timeframe", manifest.get("timeframe")),
        )
        if not str(value or "").strip()
    ]
    checks.append(
        _requirement(
            "DATASET_IDENTITY",
            FAIL if identity_missing else PASS,
            reason="DATASET_IDENTITY_MISSING" if identity_missing else "",
            detail=(
                f"missing {', '.join(identity_missing)}"
                if identity_missing
                else f"{dataset.get('market')}/{dataset.get('symbol')} {manifest.get('timeframe')} dataset {dataset.get('dataset_id')}"
            ),
            evidence={
                "dataset_id": dataset.get("dataset_id"),
                "sha256": dataset.get("sha256"),
                "symbol": dataset.get("symbol"),
                "market": manifest.get("market"),
                "timeframe": manifest.get("timeframe"),
            },
        )
    )

    recorded_sha = str(dataset.get("sha256") or "").strip().lower()
    if not _is_sha256(recorded_sha):
        checks.append(
            _requirement(
                "DATASET_HASH_VERIFIED",
                FAIL,
                reason="DATASET_HASH_MISSING",
                detail=f"recorded dataset hash is not a SHA-256 digest ({recorded_sha[:12]!r})",
            )
        )
    elif not recomputed_dataset_sha256:
        checks.append(
            _requirement(
                "DATASET_HASH_VERIFIED",
                UNVERIFIED,
                reason="DATASET_HASH_NOT_RECOMPUTED",
                detail="no fresh hash of the evaluated bars was supplied, so the pinned hash is unconfirmed",
                evidence={"recorded_sha256": recorded_sha},
            )
        )
    elif str(recomputed_dataset_sha256).strip().lower() != recorded_sha:
        checks.append(
            _requirement(
                "DATASET_HASH_VERIFIED",
                FAIL,
                reason="DATASET_HASH_MISMATCH",
                detail=(
                    f"pinned {recorded_sha[:12]}… differs from the evaluated bars "
                    f"({str(recomputed_dataset_sha256)[:12]}…)"
                ),
            )
        )
    else:
        checks.append(
            _requirement(
                "DATASET_HASH_VERIFIED",
                PASS,
                detail=f"evaluated bars re-hash to the pinned dataset digest {recorded_sha[:12]}…",
                evidence={"sha256": recorded_sha},
            )
        )

    actions_sha = str(dataset.get("actions_sha256") or action_block.get("actions_sha256") or "")
    actions_total = int(action_block.get("actions_total") or 0)
    adjusted_claim = str(dataset.get("corporate_action_adjustment") or "") or str(
        action_block.get("claim") or ""
    )
    actions_in_scope = bool(adjusted_claim) and "NOT_APPLICABLE" not in adjusted_claim.upper()
    if action_block.get("status") in _ADJUSTED_STATUSES or adjusted_claim.upper().startswith(
        ("SPLIT", "DIVIDEND", "TOTAL_RETURN")
    ):
        actions_in_scope = True
    if not actions_in_scope and not actions_total:
        checks.append(
            _requirement(
                "ACTION_DATA_HASH",
                NOT_APPLICABLE,
                detail="no corporate-action scope claimed and no action book attached",
            )
        )
    elif _is_sha256(actions_sha):
        checks.append(
            _requirement(
                "ACTION_DATA_HASH",
                PASS,
                detail=f"{actions_total} action record(s) pinned by SHA-256 {actions_sha[:12]}…",
                evidence={
                    "actions_sha256": actions_sha,
                    "actions_total": actions_total,
                    "actions_history_sha256": dataset.get("actions_history_sha256")
                    or action_block.get("actions_history_sha256"),
                },
            )
        )
    else:
        checks.append(
            _requirement(
                "ACTION_DATA_HASH",
                FAIL,
                reason="ACTION_HASH_MISSING",
                detail="corporate actions are in scope but no action-book hash was recorded",
            )
        )

    provenance_block = provenance_evidence(provenance)
    source_type = str(dataset.get("source_type") or meta.get("source_type") or "")
    feed = str(dataset.get("feed") or meta.get("feed") or "")
    real_flag = bool(dataset.get("real_market_data", meta.get("real_market_data", False)))
    synthetic_markers = (
        "SYNTHETIC" in source_type.upper()
        or "SYNTHETIC" in feed.upper()
        or "BUNDLED_FIXTURE" in source_type.upper()
        or "DEMO" in str(dataset.get("symbol") or "").upper()
    )
    if synthetic_markers or not real_flag:
        checks.append(
            _requirement(
                "REAL_DATA_PROVENANCE",
                FAIL,
                reason="SYNTHETIC_DATA",
                detail=(
                    f"source_type={source_type or 'unknown'} feed={feed or 'unknown'} "
                    f"real_market_data={real_flag}: fixtures and demo data can never qualify a strategy"
                ),
                evidence={"source_type": source_type, "feed": feed, "real_market_data": real_flag},
            )
        )
    elif not provenance_block:
        checks.append(
            _requirement(
                "REAL_DATA_PROVENANCE",
                UNVERIFIED,
                reason="PROVENANCE_ATTESTATION_MISSING",
                detail=(
                    "the dataset is labelled real market data but no provider-capture attestation was "
                    "supplied, so provenance stays unverified"
                ),
                evidence={"source_type": source_type, "feed": feed},
            )
        )
    elif not provenance_block["attested"] or not provenance_block["provider"]:
        checks.append(
            _requirement(
                "REAL_DATA_PROVENANCE",
                UNVERIFIED,
                reason="PROVENANCE_NOT_ATTESTED",
                detail="the provenance block is present but not attested or names no provider",
                evidence=provenance_block,
            )
        )
    else:
        checks.append(
            _requirement(
                "REAL_DATA_PROVENANCE",
                PASS,
                detail=(
                    f"{provenance_block['provider']} capture attested by "
                    f"{provenance_block['attested_by'] or 'operator'} "
                    f"(verification level: {provenance_block['verification_level']})"
                ),
                evidence=provenance_block,
            )
        )

    ca_status = str(action_block.get("status") or "")
    ca_verified = bool(action_block.get("verified"))
    unsupported = action_block.get("unsupported_actions") or []
    if ca_status == AdjustmentStatus.PARTIAL_UNSUPPORTED.value or unsupported:
        checks.append(
            _requirement(
                "CORPORATE_ACTION_STATUS",
                FAIL,
                reason="CORPORATE_ACTIONS_UNSUPPORTED",
                detail=(
                    f"status={ca_status or 'unknown'} with {len(unsupported)} unsupported action(s); "
                    "unsupported action types must be resolved, not silently applied"
                ),
                evidence={"status": ca_status, "unsupported": unsupported[:5]},
            )
        )
    elif ca_status in VERIFIED_ACTION_STATUSES and ca_verified:
        checks.append(
            _requirement(
                "CORPORATE_ACTION_STATUS",
                PASS,
                detail=f"status={ca_status}, verified=True, policy={action_block.get('policy')}",
                evidence={
                    "status": ca_status,
                    "policy": action_block.get("policy"),
                    "verified": ca_verified,
                    "prices_adjusted": action_block.get("prices_adjusted"),
                    "raw_bars_for_execution": action_block.get("raw_bars_for_execution"),
                },
            )
        )
    else:
        checks.append(
            _requirement(
                "CORPORATE_ACTION_STATUS",
                UNVERIFIED,
                reason="CORPORATE_ACTIONS_UNVERIFIED",
                detail=(
                    f"status={ca_status or 'missing'} verified={ca_verified}; a series with unverified "
                    "split/dividend handling cannot be qualified"
                ),
                evidence={"status": ca_status, "verified": ca_verified,
                         "limitations": (action_block.get("limitations") or [])[:3]},
            )
        )

    conf = conformance_evidence(conformance, market)
    if not conf:
        checks.append(
            _requirement(
                "PROVIDER_CONFORMANCE",
                UNVERIFIED,
                reason="PROVIDER_CONFORMANCE_MISSING",
                detail=f"no read-only conformance report was supplied for {_provider_for_market(market)}",
            )
        )
    elif conf["overall_status"] != "PASS" or conf["provider_status"] != "PASS":
        checks.append(
            _requirement(
                "PROVIDER_CONFORMANCE",
                FAIL,
                reason="PROVIDER_CONFORMANCE_FAILED",
                detail=(
                    f"conformance status={conf['overall_status']} "
                    f"{conf['provider']}={conf['provider_status']} (suite {conf['suite_version']})"
                ),
                evidence=conf,
            )
        )
    elif conf["broker_order_api_calls"] not in (0, None) or conf["live_trading"] not in (False, None):
        checks.append(
            _requirement(
                "PROVIDER_CONFORMANCE",
                FAIL,
                reason="CONFORMANCE_NOT_PAPER_SAFE",
                detail="the conformance report claims non-zero broker order calls or live trading",
                evidence=conf,
            )
        )
    else:
        checks.append(
            _requirement(
                "PROVIDER_CONFORMANCE",
                PASS,
                detail=(
                    f"{conf['provider']} adapter conformance PASS ({conf['provider_checks']} checks, "
                    f"suite {conf['suite_version']}, data source {conf['data_source_version']}, "
                    f"network_calls={conf['network_calls']})"
                ),
                evidence=conf,
            )
        )

    # ---------------- METHOD ---------------------------------------------- #
    splits = manifest.get("splits") if isinstance(manifest.get("splits"), dict) else {}
    train = splits.get("train") or {}
    val = splits.get("validation") or {}
    test = splits.get("untouched_test") or {}
    split_problems: list[str] = []
    train_end_ts = _parse_ts(train.get("end_ts"))
    val_start_ts = _parse_ts(val.get("start_ts"))
    val_end_ts = _parse_ts(val.get("end_ts"))
    test_start_ts = _parse_ts(test.get("start_ts"))
    if not all((train_end_ts, val_start_ts, val_end_ts, test_start_ts)):
        split_problems.append("split boundaries or timestamps are missing")
    else:
        if not (train_end_ts < val_start_ts < val_end_ts < test_start_ts):
            split_problems.append("train < validation < test timestamp order is violated")
    for index in (int(train.get("end_idx") or 0), int(val.get("start_idx") or 0),
                  int(val.get("end_idx") or 0), int(test.get("start_idx") or 0)):
        if index <= 0:
            split_problems.append("split indices are missing or degenerate")
            break
    if int(val.get("start_idx") or 0) <= int(train.get("end_idx") or 0):
        split_problems.append("validation overlaps train (no embargo)")
    if int(test.get("start_idx") or 0) <= int(val.get("end_idx") or 0):
        split_problems.append("untouched test overlaps validation (no embargo)")
    if int(splits.get("embargo_1_bars") or 0) < 1 or int(splits.get("embargo_2_bars") or 0) < 1:
        split_problems.append("embargo_bars must be >= 1 between every split")
    if test.get("selection_touched_test") is not False:
        split_problems.append("the manifest does not assert selection_touched_test=false")
    if _get(manifest, "search_family", "holdout_used_for_selection") is not False:
        split_problems.append("the search family does not assert holdout_used_for_selection=false")
    checks.append(
        _requirement(
            "CHRONOLOGICAL_SPLITS",
            FAIL if split_problems else PASS,
            reason="SPLIT_LEAKAGE" if split_problems else "",
            detail="; ".join(split_problems) if split_problems
            else (
                f"train {train.get('bars')} bars → embargo {splits.get('embargo_1_bars')} → "
                f"validation {val.get('bars')} → embargo {splits.get('embargo_2_bars')} → "
                f"untouched test {test.get('bars')} bars, never used for selection"
            ),
            evidence={
                "train": train.get("bars"),
                "validation": val.get("bars"),
                "untouched_test": test.get("bars"),
                "embargo_1_bars": splits.get("embargo_1_bars"),
                "embargo_2_bars": splits.get("embargo_2_bars"),
            },
        )
    )

    bar_count = int(dataset.get("bar_count") or 0)
    best = _pick_reported_candidate(candidates)
    val_metrics = best.get("validation") or {}
    test_metrics = best.get("untouched_test") or {}
    observation_problems: list[str] = []
    if bar_count < int(limits["min_total_bars"]):
        observation_problems.append(f"total bars {bar_count} < {limits['min_total_bars']}")
    if int(val.get("bars") or 0) < int(limits["min_validation_bars"]):
        observation_problems.append(f"validation bars {val.get('bars')} < {limits['min_validation_bars']}")
    if int(test.get("bars") or 0) < int(limits["min_holdout_bars"]):
        observation_problems.append(f"holdout bars {test.get('bars')} < {limits['min_holdout_bars']}")
    if int(val_metrics.get("trade_count") or 0) < int(limits["min_trades_per_split"]):
        observation_problems.append(
            f"validation trades {val_metrics.get('trade_count')} < {limits['min_trades_per_split']}"
        )
    if int(test_metrics.get("trade_count") or 0) < int(limits["min_trades_per_split"]):
        observation_problems.append(
            f"holdout trades {test_metrics.get('trade_count')} < {limits['min_trades_per_split']}"
        )
    checks.append(
        _requirement(
            "MINIMUM_OBSERVATIONS",
            FAIL if observation_problems else PASS,
            reason="INSUFFICIENT_OBSERVATIONS" if observation_problems else "",
            detail="; ".join(observation_problems) if observation_problems
            else (
                f"{bar_count} bars, validation {val.get('bars')} bars / "
                f"{val_metrics.get('trade_count')} trades, holdout {test.get('bars')} bars / "
                f"{test_metrics.get('trade_count')} trades"
            ),
            evidence={
                "total_bars": bar_count,
                "validation_bars": val.get("bars"),
                "holdout_bars": test.get("bars"),
                "validation_trades": val_metrics.get("trade_count"),
                "holdout_trades": test_metrics.get("trade_count"),
                "thresholds": {k: limits[k] for k in ("min_total_bars", "min_validation_bars",
                                                      "min_holdout_bars", "min_trades_per_split")},
            },
        )
    )

    data_end = _parse_ts(_get(dataset, "data_range", "end_ts")) or _parse_ts(test.get("end_ts"))
    run_at = _parse_ts(manifest.get("completed_at")) or datetime.now(UTC)
    recency_problems: list[str] = []
    data_age_days: int | None = None
    if data_end is None:
        recency_problems.append("the last bar timestamp is missing")
    else:
        if data_end > run_at + timedelta(minutes=5):
            recency_problems.append(f"last bar {data_end.isoformat()} is in the future of the run")
        data_age_days = (run_at - data_end).days
        if data_age_days > int(limits["max_data_age_days"]):
            recency_problems.append(
                f"last bar is {data_age_days} days old (> {limits['max_data_age_days']})"
            )
    checks.append(
        _requirement(
            "DATA_RECENCY",
            FAIL if recency_problems else PASS,
            reason="STALE_DATA" if recency_problems else "",
            detail="; ".join(recency_problems) if recency_problems
            else (
                f"last bar {data_end.isoformat()} is {data_age_days} day(s) old at run time "  # type: ignore[union-attr]
                f"(window {limits['max_data_age_days']} days)"
            ),
            evidence={
                "data_end": data_end.isoformat() if data_end else None,
                "data_age_days": data_age_days,
                "max_data_age_days": limits["max_data_age_days"],
            },
        )
    )

    family = manifest.get("search_family") if isinstance(manifest.get("search_family"), dict) else {}
    family_problems: list[str] = []
    if not family:
        family_problems.append("no search-family manifest was recorded")
    else:
        if family.get("declared_before_search") is not True:
            family_problems.append("the family was not declared before the search")
        if int(family.get("variants_declared") or 0) <= 0:
            family_problems.append("no variants were declared")
        if int(family.get("variants_evaluated") or 0) != int(family.get("variants_declared") or 0):
            family_problems.append(
                f"evaluated {family.get('variants_evaluated')} of {family.get('variants_declared')} declared variants"
            )
        if family.get("family_complete") is not True:
            family_problems.append("the declared family was not fully evaluated")
        if not _is_sha256(family.get("family_sha256")):
            family_problems.append("the declared family was not hashed")
        if family.get("selection_split") != "validation":
            family_problems.append("selection did not use the validation split")
        if len(family.get("variants") or []) != int(family.get("variants_declared") or 0):
            family_problems.append("the recorded variant list does not match the declared count")
    checks.append(
        _requirement(
            "SEARCH_FAMILY_DECLARED",
            FAIL if family_problems else PASS,
            reason="SEARCH_MANIFEST_INCOMPLETE" if family_problems else "",
            detail="; ".join(family_problems) if family_problems
            else (
                f"{family.get('variants_declared')} variants declared before search and all recorded "
                f"(family sha256 {str(family.get('family_sha256'))[:12]}…), selection on validation only"
            ),
            evidence={
                "variants_declared": family.get("variants_declared"),
                "variants_evaluated": family.get("variants_evaluated"),
                "family_sha256": family.get("family_sha256"),
                "selection_split": family.get("selection_split"),
            },
        )
    )

    correction = family.get("correction") if isinstance(family.get("correction"), dict) else {}
    mt_problems: list[str] = []
    mt_status = PASS
    mt_reason = ""
    if not correction:
        mt_status, mt_reason = UNVERIFIED, "MULTIPLE_TESTING_MISSING"
        mt_problems.append("no multiple-testing correction was recorded")
    else:
        declared_size = int(correction.get("family_size") or 0)
        if declared_size != int(family.get("variants_declared") or 0):
            mt_problems.append(
                f"the correction covers {declared_size} variants but the family declares "
                f"{family.get('variants_declared')}"
            )
        if int(family.get("variants_evaluated") or 0) > declared_size:
            mt_problems.append("more variants were evaluated than the correction accounts for")
        threshold = _as_float(correction.get("sharpe_haircut_threshold"))
        if threshold is None:
            mt_problems.append("the haircut threshold is missing or non-finite")
        if correction.get("applied_before_selection") is not True:
            mt_problems.append("the haircut was not applied before selection")
        reported_sharpe = _as_float(val_metrics.get("sharpe"))
        if threshold is not None and reported_sharpe is not None:
            if reported_sharpe <= threshold:
                mt_problems.append(
                    f"reported validation Sharpe {reported_sharpe:.2f} <= family haircut {threshold:.2f}"
                )
            elif best.get("promotion_eligible") and reported_sharpe <= threshold:
                mt_problems.append("an eligible candidate sits below the family haircut")
        if mt_problems:
            mt_status, mt_reason = FAIL, "MULTIPLE_TESTING_INCOMPLETE"
    checks.append(
        _requirement(
            "MULTIPLE_TESTING",
            mt_status,
            reason=mt_reason,
            detail="; ".join(mt_problems) if mt_problems
            else (
                f"{correction.get('method')} over a family of {correction.get('family_size')} variants "
                f"and {correction.get('oos_bars')} validation bars → haircut "
                f"{_as_float(correction.get('sharpe_haircut_threshold')):.2f}; reported Sharpe "
                f"{_as_float(val_metrics.get('sharpe')):.2f} clears it"
            ),
            evidence={
                "family_size": correction.get("family_size"),
                "oos_bars": correction.get("oos_bars"),
                "sharpe_haircut_threshold": correction.get("sharpe_haircut_threshold"),
                "method": correction.get("method"),
                "reported_validation_sharpe": val_metrics.get("sharpe"),
                "family_sha256": family.get("family_sha256"),
            },
        )
    )

    wf_folds = [f for f in (best.get("walk_forward_folds") or []) if isinstance(f, dict)]
    oos_problems: list[str] = []
    val_sharpe = _as_float(val_metrics.get("sharpe"))
    test_sharpe = _as_float(test_metrics.get("sharpe"))
    test_return = _as_float(test_metrics.get("net_return_pct"))
    if val_sharpe is None or test_sharpe is None or test_return is None:
        oos_problems.append("validation/holdout metrics are missing")
    else:
        if val_sharpe <= 0:
            oos_problems.append(f"validation Sharpe {val_sharpe:.2f} <= 0")
        if test_sharpe <= 0:
            oos_problems.append(f"holdout Sharpe {test_sharpe:.2f} <= 0")
        if test_return <= 0:
            oos_problems.append(f"holdout net return {test_return:.2f}% <= 0")
        if val_sharpe * test_sharpe < 0:
            oos_problems.append("validation and holdout Sharpes disagree in sign")
    if wf_folds:
        positive = sum(1 for f in wf_folds if (_as_float(f.get("net_return_pct")) or 0.0) > 0)
        ratio = positive / len(wf_folds)
        if ratio < float(limits["min_oos_folds_positive"]):
            oos_problems.append(
                f"only {positive}/{len(wf_folds)} walk-forward folds were positive "
                f"(< {float(limits['min_oos_folds_positive']):.0%})"
            )
    else:
        oos_problems.append("no walk-forward folds were recorded")
    checks.append(
        _requirement(
            "OOS_CONSISTENCY",
            FAIL if oos_problems else PASS,
            reason="OOS_INCONSISTENT" if oos_problems else "",
            detail="; ".join(oos_problems) if oos_problems
            else (
                f"validation Sharpe {val_sharpe:.2f}, holdout Sharpe {test_sharpe:.2f}, "
                f"holdout net {test_return:.2f}%, {len(wf_folds)} walk-forward fold(s) positive"
            ),
            evidence={
                "validation_sharpe": val_metrics.get("sharpe"),
                "holdout_sharpe": test_metrics.get("sharpe"),
                "holdout_net_return_pct": test_metrics.get("net_return_pct"),
                "walk_forward_folds": len(wf_folds),
            },
        )
    )

    pipeline_reasons = [str(r) for r in (best.get("rejection_reasons") or [])]
    if best.get("promotion_eligible") is True:
        checks.append(
            _requirement(
                "PIPELINE_GATE",
                PASS,
                detail=(
                    "the pipeline's validation Sharpe, minimum-trades, untouched-test trade and "
                    "cost-stress gates all accepted this candidate"
                ),
                evidence={"promotion_eligible": True},
            )
        )
    else:
        checks.append(
            _requirement(
                "PIPELINE_GATE",
                FAIL,
                reason="PIPELINE_GATE_REJECTED",
                detail=(
                    "no candidate passed the pipeline gates"
                    if not pipeline_reasons
                    else "pipeline rejected the reported candidate: " + "; ".join(pipeline_reasons[:4])
                ),
                evidence={"promotion_eligible": False, "reasons": pipeline_reasons[:6]},
            )
        )

    costs = manifest.get("cost_assumptions") if isinstance(manifest.get("cost_assumptions"), dict) else {}
    stressed = best.get("stressed_test") if isinstance(best.get("stressed_test"), dict) else {}
    cost_problems: list[str] = []
    cost_status = PASS
    cost_reason = ""
    spread = _as_float(costs.get("spread_bps"))
    slippage = _as_float(costs.get("slippage_bps"))
    stress_mult = _as_float(costs.get("stress_multiplier"))
    if spread is None or slippage is None or stress_mult is None:
        cost_status, cost_reason = FAIL, "COSTS_MISSING"
        cost_problems.append("cost assumptions (spread/slippage/stress multiplier) are missing")
    else:
        if spread <= 0 or slippage <= 0:
            cost_problems.append(f"costs are not positive (spread {spread}, slippage {slippage})")
        if stress_mult < float(limits["min_stress_multiplier"]):
            cost_problems.append(
                f"stress multiplier {stress_mult} < {float(limits['min_stress_multiplier'])}"
            )
    stressed_return = _as_float(stressed.get("net_return_pct"))
    if not stressed:
        cost_problems.append("no stressed-cost rerun was recorded")
        cost_reason = cost_reason or "COST_STRESS_MISSING"
    elif stressed_return is None:
        cost_problems.append("the stressed rerun reported no net return")
    elif stressed_return <= 0:
        cost_problems.append(f"stressed net return {stressed_return:.2f}% <= 0")
    if cost_problems:
        cost_status = FAIL
        cost_reason = cost_reason or "COST_STRESS_FAILED"
    checks.append(
        _requirement(
            "COSTS_MODELLED",
            cost_status,
            reason=cost_reason,
            detail="; ".join(cost_problems) if cost_problems
            else (
                f"spread {spread} bps, slippage {slippage} bps, fees via "
                f"{costs.get('fee_schedule')}; {stress_mult}× stress rerun returns "
                f"{stressed_return:.2f}%"
            ),
            evidence={
                "spread_bps": spread,
                "slippage_bps": slippage,
                "stress_multiplier": stress_mult,
                "stressed_net_return_pct": stressed.get("net_return_pct"),
                "fee_schedule": costs.get("fee_schedule"),
            },
        )
    )

    benchmark_id = str(_get(manifest, "benchmark", "candidate_id") or "buy_and_hold")
    bench = next((c for c in candidates if c.get("candidate_id") == benchmark_id), None)
    bench_test = (bench or {}).get("untouched_test") or {}
    bench_problems: list[str] = []
    bench_status = PASS
    bench_reason = ""
    if bench is None or not bench_test:
        bench_status, bench_reason = UNVERIFIED, "BENCHMARK_MISSING"
        bench_problems.append(f"no same-split benchmark ({benchmark_id}) was recorded")
    elif bool(limits["require_benchmark_outperformance"]):
        bench_return = _as_float(bench_test.get("net_return_pct"))
        if bench_return is None or test_return is None:
            bench_status, bench_reason = UNVERIFIED, "BENCHMARK_NOT_COMPARABLE"
            bench_problems.append("benchmark or candidate holdout return is missing")
        elif test_return <= bench_return:
            bench_problems.append(
                f"holdout net return {test_return:.2f}% does not beat {benchmark_id} {bench_return:.2f}%"
            )
    if bench_problems and bench_status == PASS:
        bench_status, bench_reason = FAIL, "UNDERPERFORMS_BENCHMARK"
    checks.append(
        _requirement(
            "BENCHMARK",
            bench_status,
            reason=bench_reason,
            detail="; ".join(bench_problems) if bench_problems
            else (
                f"holdout net return {test_return:.2f}% vs {benchmark_id} "
                f"{_as_float(bench_test.get('net_return_pct')):.2f}% on the same holdout"
            ),
            evidence={
                "benchmark": benchmark_id,
                "benchmark_holdout_net_return_pct": bench_test.get("net_return_pct"),
                "candidate_holdout_net_return_pct": test_metrics.get("net_return_pct"),
            },
        )
    )

    drawdown = _as_float(test_metrics.get("max_drawdown_pct"))
    if drawdown is None:
        checks.append(
            _requirement(
                "RISK_LIMITS",
                UNVERIFIED,
                reason="RISK_METRICS_MISSING",
                detail="the holdout drawdown was not recorded, so risk limits cannot be checked",
            )
        )
    elif abs(drawdown) > float(limits["max_holdout_drawdown_pct"]):
        checks.append(
            _requirement(
                "RISK_LIMITS",
                FAIL,
                reason="RISK_LIMIT_EXCEEDED",
                detail=(
                    f"holdout max drawdown {abs(drawdown):.2f}% exceeds "
                    f"{float(limits['max_holdout_drawdown_pct']):.2f}%"
                ),
                evidence={"max_drawdown_pct": drawdown},
            )
        )
    else:
        checks.append(
            _requirement(
                "RISK_LIMITS",
                PASS,
                detail=(
                    f"holdout max drawdown {abs(drawdown):.2f}% within "
                    f"{float(limits['max_holdout_drawdown_pct']):.2f}%"
                ),
                evidence={"max_drawdown_pct": drawdown,
                          "limit_pct": limits["max_holdout_drawdown_pct"]},
            )
        )

    # ---------------- REPRODUCIBILITY ------------------------------------- #
    params = best.get("parameters")
    feature_sha = str(_get(manifest, "software", "feature_set_sha256") or "")
    version_problems: list[str] = []
    if not str(best.get("strategy_id") or "").strip():
        version_problems.append("the candidate names no strategy id")
    if not str(best.get("strategy_version") or "").strip():
        version_problems.append("the candidate names no strategy version")
    if not isinstance(params, dict) or not params:
        version_problems.append("the candidate parameters were not recorded")
    if feature_sha and feature_sha != _sha256(canonical(list(FEATURE_NAMES))):
        version_problems.append("the recorded feature schema hash does not match this build's features")
    checks.append(
        _requirement(
            "STRATEGY_VERSION",
            FAIL if version_problems else PASS,
            reason="STRATEGY_VERSION_MISSING" if version_problems else "",
            detail="; ".join(version_problems) if version_problems
            else (
                f"{best.get('strategy_id')}@v{best.get('strategy_version')} with "
                f"{len(params)} parameter(s) and feature schema {feature_sha[:12]}…"
            ),
            evidence={
                "strategy_id": best.get("strategy_id"),
                "strategy_version": best.get("strategy_version"),
                "parameter_keys": sorted(params) if isinstance(params, dict) else [],
                "feature_set_sha256": feature_sha,
            },
        )
    )

    universe = manifest.get("universe") if isinstance(manifest.get("universe"), dict) else {}
    symbols = [s for s in (universe.get("symbols") or []) if str(s).strip()]
    if not symbols:
        checks.append(
            _requirement(
                "UNIVERSE",
                UNVERIFIED,
                reason="UNIVERSE_MISSING",
                detail="the evaluated symbol set was not recorded",
            )
        )
    else:
        checks.append(
            _requirement(
                "UNIVERSE",
                PASS,
                detail=(
                    f"{len(symbols)} symbol(s) {', '.join(symbols[:4])}"
                    f"{'…' if len(symbols) > 4 else ''}; survivorship_aware="
                    f"{bool(universe.get('survivorship_aware'))}"
                ),
                evidence={
                    "symbols": symbols,
                    "source": universe.get("source"),
                    "survivorship_aware": universe.get("survivorship_aware"),
                },
            )
        )

    software = manifest.get("software") if isinstance(manifest.get("software"), dict) else {}
    commit = str(software.get("commit") or "").strip().lower()
    dirty = software.get("tree_dirty")
    sw_problems: list[str] = []
    sw_status = PASS
    sw_reason = ""
    if not commit:
        sw_status, sw_reason = UNVERIFIED, "SOFTWARE_COMMIT_MISSING"
        sw_problems.append("the manifest records no producing commit")
    elif expected_software_commit and commit != str(expected_software_commit).strip().lower():
        sw_status, sw_reason = UNVERIFIED, "COMMIT_MISMATCH"
        sw_problems.append(
            f"manifest commit {commit[:12]}… differs from the expected {str(expected_software_commit)[:12]}…"
        )
    if dirty is True and bool(limits["require_dirty_tree_clean"]):
        sw_status, sw_reason = UNVERIFIED, "SOFTWARE_TREE_DIRTY"
        sw_problems.append("the producing tree had uncommitted changes, so the run is not reproducible")
    elif dirty is None and commit:
        sw_problems.append("the producing tree's cleanliness could not be determined")
        if sw_status == PASS:
            sw_status, sw_reason = UNVERIFIED, "SOFTWARE_TREE_STATE_UNKNOWN"
    checks.append(
        _requirement(
            "SOFTWARE_COMMIT",
            sw_status,
            reason=sw_reason,
            detail="; ".join(sw_problems) if sw_problems
            else f"commit {commit[:12]}… ({software.get('commit_source')}), tree_dirty={dirty}",
            evidence={
                "commit": commit,
                "commit_source": software.get("commit_source"),
                "tree_dirty": dirty,
                "expected_commit": expected_software_commit or None,
                "pipeline_schema": software.get("pipeline_schema"),
            },
        )
    )

    recorded_digest = str(manifest.get("manifest_sha256") or "")
    fresh_digest = manifest_digest(manifest)
    checks.append(
        _requirement(
            "MANIFEST_INTEGRITY",
            PASS if recorded_digest == fresh_digest else FAIL,
            reason="" if recorded_digest == fresh_digest else "MANIFEST_TAMPERED",
            detail=(
                f"manifest_sha256 {recorded_digest[:12]}… matches a fresh digest"
                if recorded_digest == fresh_digest
                else f"recorded {recorded_digest[:12] or 'missing'}… != recomputed {fresh_digest[:12]}…"
            ),
            evidence={"manifest_sha256": recorded_digest, "recomputed": fresh_digest},
        )
    )

    # ---------------- verdict --------------------------------------------- #
    by_requirement = {c["requirement"]: c for c in checks}
    counts = {PASS: 0, FAIL: 0, UNVERIFIED: 0, NOT_APPLICABLE: 0}
    for check in checks:
        counts[check["status"]] = counts.get(check["status"], 0) + 1
    blocked = [c for c in checks if c["status"] == FAIL]
    unverified = [c for c in checks if c["status"] == UNVERIFIED]
    status = STATUS_BLOCKED if blocked else (STATUS_UNVERIFIED if unverified else STATUS_QUALIFIED)

    verification_level = "NONE"
    if provenance_block:
        verification_level = provenance_block["verification_level"]
    elif conf:
        verification_level = "OFFLINE_CONFORMANCE_ONLY"

    core = {
        "schema": QUALIFICATION_SCHEMA,
        "rules_version": RULES_VERSION,
        "scope": "RESEARCH_ONLY",
        "status": status,
        "verification_level": verification_level,
        "run": {
            "manifest_sha256": recorded_digest,
            "schema": manifest.get("schema"),
            "market": manifest.get("market"),
            "timeframe": manifest.get("timeframe"),
            "currency": manifest.get("currency"),
        },
        "candidate": {
            "candidate_id": best.get("candidate_id"),
            "family": best.get("family"),
            "strategy_id": best.get("strategy_id"),
            "strategy_version": best.get("strategy_version"),
            "parameters": params if isinstance(params, dict) else {},
            "promotion_eligible_pipeline_flag": bool(best.get("promotion_eligible")),
            "pipeline_rejection_reasons": list(best.get("rejection_reasons") or []),
        },
        "dataset": {
            "dataset_id": dataset.get("dataset_id"),
            "sha256": dataset.get("sha256"),
            "recomputed_sha256": recomputed_dataset_sha256 or None,
            "source_type": source_type,
            "feed": feed,
            "real_market_data": real_flag,
            "symbol": dataset.get("symbol"),
            "market": manifest.get("market"),
            "timeframe": manifest.get("timeframe"),
            "bar_count": bar_count,
            "data_range": dataset.get("data_range"),
            "timezone": dataset.get("timezone"),
            "survivorship_aware": dataset.get("survivorship_aware"),
        },
        "corporate_actions": {
            "status": ca_status,
            "verified": ca_verified,
            "policy": action_block.get("policy"),
            "claim": adjusted_claim,
            "actions_sha256": actions_sha or None,
            "actions_total": actions_total,
            "unsupported_actions": unsupported,
            "prices_adjusted": action_block.get("prices_adjusted"),
            "raw_bars_for_execution": action_block.get("raw_bars_for_execution"),
            "limitations": list(action_block.get("limitations") or []),
        },
        "provenance": provenance_block or {"verification_level": "NONE"},
        "provider_conformance": conf or {"status": "MISSING", "provider": _provider_for_market(market)},
        "search_family": {
            "declared_before_search": family.get("declared_before_search"),
            "variants_declared": family.get("variants_declared"),
            "variants_evaluated": family.get("variants_evaluated"),
            "family_sha256": family.get("family_sha256"),
            "selection_split": family.get("selection_split"),
            "selection_metric": family.get("selection_metric"),
            "holdout_used_for_selection": family.get("holdout_used_for_selection"),
            "correction": correction,
            "selected_variants": list(family.get("selected_variants") or []),
            "family_complete": family.get("family_complete"),
        },
        "splits": {
            "train_bars": train.get("bars"),
            "validation_bars": val.get("bars"),
            "untouched_test_bars": test.get("bars"),
            "embargo_1_bars": splits.get("embargo_1_bars"),
            "embargo_2_bars": splits.get("embargo_2_bars"),
            "selection_touched_test": test.get("selection_touched_test"),
        },
        "costs": {
            "spread_bps": costs.get("spread_bps"),
            "slippage_bps": costs.get("slippage_bps"),
            "stress_multiplier": costs.get("stress_multiplier"),
            "fee_schedule": costs.get("fee_schedule"),
            "min_trades": costs.get("min_trades"),
        },
        "benchmark": {
            "candidate_id": benchmark_id,
            "holdout_net_return_pct": bench_test.get("net_return_pct"),
            "holdout_sharpe": bench_test.get("sharpe"),
        },
        "metrics": {
            "validation": _metric_view(val_metrics),
            "holdout": _metric_view(test_metrics),
            "stressed_holdout": _metric_view(stressed),
            "walk_forward_folds": len(wf_folds),
        },
        "software": {
            "commit": commit,
            "commit_source": software.get("commit_source"),
            "tree_dirty": dirty,
            "pipeline_schema": software.get("pipeline_schema"),
            "feature_set_sha256": software.get("feature_set_sha256"),
            "package_version": software.get("package_version"),
            "python": software.get("python"),
        },
        "thresholds": {k: limits[k] for k in sorted(DEFAULTS)},
        "requirements": checks,
        "counts": counts,
        "blocked": [
            {"requirement": c["requirement"], "reason": c["reason"], "detail": c["detail"]}
            for c in blocked
        ],
        "unverified": [
            {"requirement": c["requirement"], "reason": c["reason"], "detail": c["detail"]}
            for c in unverified
        ],
        "requirement_index": {rid: by_requirement[rid]["status"] for rid in REQUIREMENT_IDS if rid in by_requirement},
        "execution": {
            "live_trading_authorized": False,
            "paper_arming_effect": "NONE",
            "order_path_effect": "NONE",
            "bypasses_risk_checks": False,
            "issues_execution_permit": False,
            "enables_live_market_live": False,
        },
        "limitations": list(_STANDING_LIMITATIONS),
        "open_reviews": [
            "INDEPENDENT_PROVENANCE_REVIEW",
            "LIVE_PROVIDER_CONNECTIVITY",
            "PRODUCTION_DATA_QUALITY",
            "OUT_OF_SAMPLE_MONITORING",
        ],
    }
    core["record_sha256"] = _sha256(canonical(core))
    stamp = evaluated_at or datetime.now(UTC)
    if isinstance(stamp, str):
        stamp = _parse_ts(stamp) or datetime.now(UTC)
    return {
        **core,
        "run_completed_at": manifest.get("completed_at"),
        "evaluated_at": stamp.isoformat(),
    }


def _metric_view(metrics: dict[str, Any] | None) -> dict[str, Any]:
    metrics = metrics or {}
    return {
        "sharpe": metrics.get("sharpe"),
        "net_return_pct": metrics.get("net_return_pct"),
        "max_drawdown_pct": metrics.get("max_drawdown_pct"),
        "trade_count": metrics.get("trade_count"),
        "profit_factor": metrics.get("profit_factor"),
        "win_rate_pct": metrics.get("win_rate_pct"),
    }


def _pick_reported_candidate(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """The candidate a review should look at: the best eligible, else best holdout.

    Baselines are never promoted to "the reported candidate", and no metric the
    pipeline did not record is synthesised.
    """
    scored = [c for c in candidates if isinstance(c.get("untouched_test"), dict)]
    if not scored:
        return {}
    non_baseline = [c for c in scored if c.get("family") != "BASELINE"] or scored
    eligible = [c for c in non_baseline if c.get("promotion_eligible")]

    def _key(c: dict[str, Any]) -> tuple[float, float]:
        holdout = _as_float(_get(c, "untouched_test", "net_return_pct")) or -1e9
        validation = _as_float(_get(c, "validation", "sharpe")) or -1e9
        return (holdout, validation)

    pool = eligible or non_baseline
    return max(pool, key=_key)


# --------------------------------------------------------------------------- #
# Execution boundary — qualification is never authority
# --------------------------------------------------------------------------- #
def qualification_authority(record: dict[str, Any] | None) -> dict[str, Any]:
    """The only execution-related object a qualification can ever produce."""
    status = str((record or {}).get("status") or "MISSING")
    return {
        "authority": "NONE",
        "qualification_status": status,
        "live_trading_authorized": False,
        "paper_arming_effect": "NONE",
        "issues_execution_permit": False,
        "bypasses_risk_checks": False,
        "reason": (
            "A qualified research record documents evidence only. Arming, paper selection, "
            "risk sizing and any LIVE decision remain separate operator-controlled gates."
        ),
    }


def assert_no_execution_authority(record: dict[str, Any]) -> None:
    """Fail closed if a record ever claims execution rights."""
    execution = record.get("execution") if isinstance(record, dict) else None
    if not isinstance(execution, dict):
        raise QualificationBlocked("qualification record is missing its execution block")
    forbidden = {
        "live_trading_authorized": False,
        "paper_arming_effect": "NONE",
        "order_path_effect": "NONE",
        "bypasses_risk_checks": False,
        "issues_execution_permit": False,
        "enables_live_market_live": False,
    }
    for key, expected in forbidden.items():
        if execution.get(key) != expected:
            raise QualificationBlocked(f"qualification record claims {key}={execution.get(key)!r}")
    if str(record.get("scope")) != "RESEARCH_ONLY":
        raise QualificationBlocked("qualification record scope must be RESEARCH_ONLY")


def require_qualified(record: dict[str, Any] | None) -> None:
    """Raise unless the record is QUALIFIED *and* still carries no authority."""
    if not record:
        raise QualificationBlocked("no qualification record is available")
    assert_no_execution_authority(record)
    if str(record.get("status")) != STATUS_QUALIFIED:
        reasons = ", ".join(
            f"{item.get('requirement')}:{item.get('reason')}"
            for item in (record.get("blocked") or record.get("unverified") or [])[:4]
        )
        raise QualificationBlocked(f"research is not qualified ({record.get('status')}): {reasons}")


def compact_view(record: dict[str, Any] | None) -> dict[str, Any]:
    """Small payload for lists, tables and UI badges."""
    if not record:
        return {
            "status": "UNVERIFIED",
            "reason": "NO_QUALIFICATION_RECORD",
            "record_sha256": None,
            "verification_level": "NONE",
            "counts": {"PASS": 0, "FAIL": 0, "UNVERIFIED": 0, "NOT_APPLICABLE": 0},
            "blocked": [],
            "unverified": [],
            "candidate_id": None,
            "manifest_sha256": None,
            "execution_authority": "NONE",
        }
    return {
        "status": record.get("status"),
        "record_sha256": record.get("record_sha256"),
        "rules_version": record.get("rules_version"),
        "verification_level": record.get("verification_level"),
        "counts": record.get("counts"),
        "blocked": [b.get("reason") for b in (record.get("blocked") or [])],
        "unverified": [u.get("reason") for u in (record.get("unverified") or [])],
        "candidate_id": _get(record, "candidate", "candidate_id"),
        "manifest_sha256": _get(record, "run", "manifest_sha256"),
        "execution_authority": "NONE",
    }


def render_text(record: dict[str, Any]) -> str:
    """Operator-facing text rendering (no colour, no secrets, stable layout)."""
    lines = [
        f"AXIOM research qualification  status={record['status']}  "
        f"level={record['verification_level']}  rules={record['rules_version']}",
        f"candidate: {_get(record, 'candidate', 'candidate_id')} "
        f"({_get(record, 'candidate', 'strategy_id')}@v{_get(record, 'candidate', 'strategy_version')})",
        f"manifest:  {_get(record, 'run', 'manifest_sha256')}",
        f"dataset:   {_get(record, 'dataset', 'dataset_id')} "
        f"sha256={(str(_get(record, 'dataset', 'sha256') or ''))[:16]}… "
        f"source={_get(record, 'dataset', 'source_type')}",
        f"actions:   status={_get(record, 'corporate_actions', 'status')} "
        f"verified={_get(record, 'corporate_actions', 'verified')}",
        f"search:    {_get(record, 'search_family', 'variants_evaluated')}/"
        f"{_get(record, 'search_family', 'variants_declared')} variants, haircut "
        f"{_get(record, 'search_family', 'correction', 'sharpe_haircut_threshold')}",
        f"holdout:   net {_get(record, 'metrics', 'holdout', 'net_return_pct')}% "
        f"vs benchmark {_get(record, 'benchmark', 'holdout_net_return_pct')}%",
        "",
    ]
    for check in record.get("requirements") or []:
        lines.append(
            f"  [{check['status']:<14}] {check['requirement']:<26} "
            f"{check.get('reason') or ''} {check.get('detail') or ''}".rstrip()
        )
    lines.append("")
    lines.append(f"  counts: {record.get('counts')}")
    lines.append(f"  execution authority: NONE (live trading stays disabled)")
    lines.append(f"  record sha256: {record.get('record_sha256')}")
    for limitation in record.get("limitations") or []:
        lines.append(f"  note: {limitation}")
    return "\n".join(lines)
