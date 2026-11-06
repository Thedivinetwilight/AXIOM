"""Documented broker capabilities inside the existing LIVE execution boundary.

The capability registry (``app/markets/broker_capabilities.py``) is reference
data for the readiness report and a refusal helper. It must state what each
broker documents, refuse what it does not, and never pretend the two brokers
are interchangeable. Nothing here contacts a broker.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.markets import broker_capabilities as caps
from app.markets.live_execution import (
    LiveExecutionCoordinator,
    LiveMarketAdapter,
    LiveRiskLimits,
    MODE_PAPER,
)


# --------------------------------------------------------------------------- #
# Documented facts
# --------------------------------------------------------------------------- #
def test_nse_profile_matches_the_documented_upstox_order_parameters():
    profile = caps.capabilities_for("NSE")
    assert profile.broker == "UPSTOX"
    assert profile.instrument_identifier.startswith("instrument_key")
    assert profile.order_types == ("MARKET", "LIMIT", "SL", "SL-M")
    assert profile.time_in_force == ("DAY", "IOC")
    assert profile.products == ("I", "D")
    assert profile.session.timezone == "Asia/Kolkata"
    assert (profile.session.open_local, profile.session.close_local) == ("09:15", "15:30")
    assert profile.idempotency.field == "tag"
    assert profile.idempotency.max_length == 20
    assert profile.idempotency.rejects_duplicates is False
    joined = " ".join(profile.unsupported).lower()
    for feature in ("derivatives", "mtf", "after-market", "gtd", "cover orders"):
        assert feature in joined


def test_nasdaq_profile_matches_the_documented_alpaca_order_parameters():
    profile = caps.capabilities_for("NASDAQ")
    assert profile.broker == "ALPACA"
    assert profile.instrument_identifier == "ticker symbol"
    assert profile.idempotency.field == "client_order_id"
    assert profile.idempotency.max_length == 128
    assert profile.idempotency.rejects_duplicates is True
    assert profile.session.timezone == "America/New_York"
    assert (profile.session.open_local, profile.session.close_local) == ("09:30", "16:00")
    assert "I" not in profile.products
    joined = " ".join(profile.unsupported).lower()
    for feature in ("options", "crypto", "notional", "bracket", "extended_hours", "fractional"):
        assert feature in joined


def test_every_profile_records_its_sources_and_read_date():
    for profile in caps.CAPABILITIES.values():
        assert profile.sources and all(url.startswith("https://") for url in profile.sources)
        assert profile.documented_as_of == caps.DOCUMENTED_AS_OF
        payload = profile.as_dict()
        assert payload["sources"] == list(profile.sources)
        assert payload["documented_as_of"] == caps.DOCUMENTED_AS_OF


def test_profiles_are_not_treated_as_equivalent():
    nse, nasdaq = caps.capabilities_for("NSE"), caps.capabilities_for("NASDAQ")
    assert nse.idempotency.rejects_duplicates != nasdaq.idempotency.rejects_duplicates
    assert nse.instrument_identifier != nasdaq.instrument_identifier
    assert nse.products != nasdaq.products
    assert nse.session.timezone != nasdaq.session.timezone
    assert nse.sources != nasdaq.sources


def test_idempotency_asymmetry_is_stated_not_inferred():
    diff = caps.idempotency_asymmetry()
    assert diff["nse"]["broker_rejects_duplicates"] is False
    assert diff["nasdaq"]["broker_rejects_duplicates"] is True
    assert "look the tag up" in diff["nse"]["axiom_rule"]
    assert "rejects a duplicate client_order_id" in diff["nasdaq"]["axiom_rule"]


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "market, order_type, time_in_force, product",
    [
        ("NSE", "TRAILING_STOP", None, None),
        ("NSE", None, "GTC", None),
        ("NSE", None, None, "MTF"),
        ("NASDAQ", None, "IOC_FOK", None),
        ("NASDAQ", None, None, "INTRADAY"),
        ("NASDAQ", "OCO", None, None),
    ],
)
def test_undocumented_requests_are_refused_by_name(market, order_type, time_in_force, product):
    with pytest.raises(caps.CapabilityRefused) as exc:
        caps.capabilities_for(market).check_request(
            order_type=order_type, time_in_force=time_in_force, product=product
        )
    assert exc.value.code == "UNSUPPORTED_ORDER_TYPE"
    assert caps.capabilities_for(market).broker in exc.value.detail


def test_documented_requests_pass_the_helper():
    caps.capabilities_for("NSE").check_request(order_type="SL-M", time_in_force="DAY", product="D")
    caps.capabilities_for("NASDAQ").check_request(order_type="LIMIT", time_in_force="DAY", product="CASH")


def test_unknown_market_is_refused():
    with pytest.raises(caps.CapabilityRefused) as exc:
        caps.capabilities_for("MCX")
    assert exc.value.code == "UNSUPPORTED_INSTRUMENT"


def test_capability_data_names_no_broker_order_host():
    """The order surface stays confined to the gated broker package."""
    source = Path(caps.__file__).read_text(encoding="utf-8")
    for forbidden in ("/v2/orders", "paper-api.alpaca.markets", "api.alpaca.markets"):
        assert forbidden not in source


# --------------------------------------------------------------------------- #
# Integration with the existing coordinator
# --------------------------------------------------------------------------- #
class _StubAdapter(LiveMarketAdapter):
    """Minimal adapter: enough to exercise the coordinator's readiness payload."""

    def __init__(self, market: str) -> None:
        self.market = market
        self.broker_name = caps.capabilities_for(market).broker

    def missing_dependencies(self):
        return []

    def readiness_checks(self, *, symbol=None):
        return {"probe": True}

    def gate_status(self):
        return {"market": self.market, "state": "DISABLED", "kill_engaged": False}


def _coordinator(tmp_path: Path) -> LiveExecutionCoordinator:
    now = datetime(2026, 11, 6, 6, 0, tzinfo=timezone.utc)
    return LiveExecutionCoordinator(
        journal_path=tmp_path / "live.sqlite3",
        adapters={"NSE": _StubAdapter("NSE"), "NASDAQ": _StubAdapter("NASDAQ")},
        limits={
            "NSE": LiveRiskLimits(),
            "NASDAQ": LiveRiskLimits(),
        },
        clock=lambda: now,
    )


def test_coordinator_exposes_capabilities_without_a_broker_call(tmp_path):
    coordinator = _coordinator(tmp_path)
    assert [row["market"] for row in coordinator.capabilities()] == ["NSE", "NASDAQ"]
    nse = coordinator.capabilities("nse")
    assert nse["broker"] == "UPSTOX"
    assert coordinator.capability_differences()["nasdaq"]["broker_rejects_duplicates"] is True


def test_readiness_payload_carries_the_documented_capabilities(tmp_path):
    coordinator = _coordinator(tmp_path)
    report = coordinator.readiness("NSE")
    documented = report["documented_capabilities"]
    assert documented["broker"] == "UPSTOX"
    assert documented["idempotency"]["field"] == "tag"
    assert documented["documented_as_of"] == caps.DOCUMENTED_AS_OF
    # Readiness remains a real check: unset limits still fail the report.
    assert report["checks"]["risk_limits_configured"] is False
    assert report["ok"] is False


def test_status_names_the_broker_and_capability_date(tmp_path):
    coordinator = _coordinator(tmp_path)
    status = coordinator.status("NASDAQ")
    assert status["broker"] == "ALPACA"
    assert status["documented_capabilities_as_of"] == caps.DOCUMENTED_AS_OF
    assert status["mode"] == MODE_PAPER


def test_capabilities_refusal_does_not_touch_the_gate(tmp_path):
    coordinator = _coordinator(tmp_path)
    before = coordinator.status("NSE")["gate"]
    with pytest.raises(caps.CapabilityRefused):
        caps.capabilities_for("NSE").check_request(order_type="OCO")
    assert coordinator.status("NSE")["gate"] == before
