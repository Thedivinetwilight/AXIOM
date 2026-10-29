from __future__ import annotations

from copy import deepcopy
import json

import pytest

from app.autonomy.shakedown import REQUIRED_CHECKS, evaluate_evidence, main


def valid_evidence():
    return {
        "schema_version": 1,
        "mode": "PAPER",
        "trading_day": "2026-10-26",
        "market_timings_source": "UPSTOX_MARKET_TIMINGS",
        "session_open": "2026-10-26T09:15:00+05:30",
        "session_close": "2026-10-26T15:30:00+05:30",
        "observation_started_at": "2026-10-26T09:10:00+05:30",
        "observation_ended_at": "2026-10-26T15:35:00+05:30",
        "scan_count": 4500,
        "completed_bar_count": 375,
        "broker_mutations_reachable": False,
        "broker_order_requests": 0,
        "audit_hash_chain_valid": True,
        "unresolved_intents": 0,
        "unresolved_faults": [],
        "operational_checks": {name: True for name in REQUIRED_CHECKS},
    }


def test_complete_session_operational_acceptance_has_no_profit_gate():
    report = evaluate_evidence(valid_evidence())
    assert report["accepted"] is True
    assert report["complete_market_session"] is True
    assert report["profitability_gate"] is False
    assert report["live_ready"] is False
    assert report["verification_level"] == "OWNER_ATTESTED_NOT_INDEPENDENTLY_VERIFIED"


def test_profit_or_partial_session_cannot_substitute_for_operational_evidence():
    evidence = valid_evidence()
    evidence["observation_ended_at"] = "2026-10-26T12:00:00+05:30"
    evidence["net_profit"] = 1000000000
    evidence["operational_checks"]["restart_remains_disarmed"] = False
    report = evaluate_evidence(evidence)
    assert report["accepted"] is False
    assert report["complete_market_session"] is False
    assert any("complete exchange session" in item for item in report["failures"])
    assert any("restart_remains_disarmed" in item for item in report["failures"])
    assert "net_profit" not in json.dumps(report)


def test_live_or_broker_order_evidence_is_not_a_paper_shakedown():
    evidence = valid_evidence()
    evidence["mode"] = "UPSTOX_LIVE"
    evidence["broker_mutations_reachable"] = True
    evidence["broker_order_requests"] = 1
    report = evaluate_evidence(evidence)
    assert report["accepted"] is False
    assert report["live_ready"] is False
    assert any("only live-market local PAPER" in item for item in report["failures"])
    assert any("must be zero" in item for item in report["failures"])


def test_credentials_are_rejected_without_echo_and_cli_writes_assessment(tmp_path):
    evidence = valid_evidence()
    evidence["nested"] = {"access_token": "do-not-echo-this-value"}
    with pytest.raises(ValueError, match="not accepted") as caught:
        evaluate_evidence(evidence)
    assert "do-not-echo-this-value" not in str(caught.value)

    source = tmp_path / "evidence.json"
    output = tmp_path / "assessment.json"
    source.write_text(json.dumps(valid_evidence()), encoding="utf-8")
    assert main([str(source), "--output", str(output)]) == 0
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["accepted"] is True and saved["live_ready"] is False
    assert saved["evidence_sha256"]
