"""Validate owner-attested, complete-session PAPER shakedown evidence.

This module is deliberately offline: it has no Upstox client, token input,
execution adapter, Arm/Activate method, or profitability threshold.  It checks
an operator's exported evidence; it cannot independently prove that the events
occurred and never turns the result into a live-readiness claim.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

SCHEMA_VERSION = 1
REQUIRED_CHECKS = (
    "fresh_daily_access_and_risk",
    "explicit_paper_arm_and_activate",
    "authoritative_upstox_data",
    "exact_instrument_metadata",
    "completed_bar_processing",
    "no_duplicate_orders",
    "daily_loss_entry_block_with_exit_management",
    "stale_data_fail_closed",
    "disconnect_fail_closed",
    "ambiguous_outcome_reconciled_without_blind_retry",
    "protection_continuous_and_confirmed",
    "reconciliation_and_audit_reviewed",
    "restart_remains_disarmed",
)
SENSITIVE_KEY_PARTS = (
    "token", "secret", "password", "authorization", "credential", "access_key",
    "api_key", "account_number", "login", "user_id",
)


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamps must be ISO-8601 strings")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return parsed


def _reject_sensitive_fields(value: Any, path: str = "evidence") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            name = str(key).lower()
            if any(part in name for part in SENSITIVE_KEY_PARTS):
                raise ValueError(f"credential/account-like field is not accepted: {path}.{key}")
            _reject_sensitive_fields(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_sensitive_fields(child, f"{path}[{index}]")


def evaluate_evidence(evidence: dict[str, Any], *, evidence_bytes: bytes | None = None) -> dict[str, Any]:
    """Return a non-live-ready assessment without echoing operator evidence."""
    if not isinstance(evidence, dict):
        raise ValueError("shakedown evidence must be a JSON object")
    _reject_sensitive_fields(evidence)
    failures: list[str] = []
    if evidence.get("schema_version") != SCHEMA_VERSION:
        failures.append("unsupported schema_version")
    if evidence.get("mode") != "PAPER":
        failures.append("only live-market local PAPER evidence is accepted")
    if evidence.get("broker_mutations_reachable") is not False:
        failures.append("broker mutation reachability was not explicitly false")
    if evidence.get("broker_order_requests") != 0:
        failures.append("broker order request count must be zero")
    if evidence.get("audit_hash_chain_valid") is not True:
        failures.append("audit hash chain is not valid")
    if not isinstance(evidence.get("scan_count"), int) or evidence["scan_count"] <= 0:
        failures.append("scan_count must be positive")
    if not isinstance(evidence.get("completed_bar_count"), int) or evidence["completed_bar_count"] <= 0:
        failures.append("completed_bar_count must be positive")
    if evidence.get("unresolved_intents") != 0:
        failures.append("unresolved intents remain")
    if evidence.get("unresolved_faults") != []:
        failures.append("unresolved faults remain")

    checks = evidence.get("operational_checks")
    if not isinstance(checks, dict):
        checks = {}
        failures.append("operational_checks object is missing")
    for name in REQUIRED_CHECKS:
        if checks.get(name) is not True:
            failures.append(f"operational check not accepted: {name}")

    complete_session = False
    try:
        session_open = _timestamp(evidence.get("session_open"))
        session_close = _timestamp(evidence.get("session_close"))
        observed_start = _timestamp(evidence.get("observation_started_at"))
        observed_end = _timestamp(evidence.get("observation_ended_at"))
        trading_day = evidence.get("trading_day")
        if session_open >= session_close:
            failures.append("exchange session open must precede close")
        if observed_start > observed_end:
            failures.append("observation start must precede end")
        if trading_day != session_open.date().isoformat() or trading_day != session_close.date().isoformat():
            failures.append("trading_day does not match exchange session timestamps")
        if evidence.get("market_timings_source") != "UPSTOX_MARKET_TIMINGS":
            failures.append("session bounds must come from Upstox market timings")
        complete_session = (
            observed_start <= session_open and observed_end >= session_close
            and session_open < session_close and observed_start <= observed_end
        )
        if not complete_session:
            failures.append("observation does not cover the complete exchange session")
    except (TypeError, ValueError) as exc:
        failures.append(f"invalid session evidence: {exc}")

    raw = evidence_bytes if evidence_bytes is not None else json.dumps(
        evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return {
        "schema_version": SCHEMA_VERSION,
        "verification_level": "OWNER_ATTESTED_NOT_INDEPENDENTLY_VERIFIED",
        "accepted": not failures,
        "complete_market_session": complete_session,
        "operational_acceptance": not failures,
        "profitability_gate": False,
        "live_ready": False,
        "external_demo_verified": False,
        "evidence_sha256": hashlib.sha256(raw).hexdigest(),
        "required_checks": list(REQUIRED_CHECKS),
        "failures": failures,
        "note": "This offline assessment does not prove account permissions, external execution, profitability, compliance, or live readiness.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path, help="owner-exported shakedown JSON; never include credentials")
    parser.add_argument("--output", type=Path, help="optional assessment JSON path")
    args = parser.parse_args(argv)
    try:
        raw = args.evidence.read_bytes()
        report = evaluate_evidence(json.loads(raw), evidence_bytes=raw)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        report = {"accepted": False, "live_ready": False, "error": str(exc)}
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)
    return 0 if report.get("accepted") else 1


if __name__ == "__main__":
    raise SystemExit(main())
