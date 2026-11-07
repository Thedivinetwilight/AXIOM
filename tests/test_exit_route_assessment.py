"""Pins the exit-route finding: NSE has no provably close-only exit, so none is routed.

These tests are deliberately written against the *objects an operator sees*
(``exit_route()``, the capability profile, the readiness report) as well as the
submit path, so a future change cannot quietly turn a documented SELL field into
an order route. No broker endpoint, credential or order is involved.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.markets import live_execution as le
from app.markets.broker_capabilities import CAPABILITIES, describe_capabilities
from app.markets.live_execution import (
    LiveBrokerRejected,
    NasdaqLiveAdapter,
    NseLiveAdapter,
)

ROOT = Path(__file__).resolve().parents[1]


def _bare_nse_adapter() -> NseLiveAdapter:
    """Adapter without catalog, gate or broker client — the SELL guard runs first."""
    return object.__new__(NseLiveAdapter)


def test_nse_declares_no_exit_support() -> None:
    assert NseLiveAdapter.supports_exit is False
    assert NseLiveAdapter.supports_short_entry is False


def test_only_the_guarded_alpaca_adapter_claims_exit_support() -> None:
    adapters = [
        obj
        for obj in vars(le).values()
        if isinstance(obj, type) and issubclass(obj, le.LiveMarketAdapter) and obj is not le.LiveMarketAdapter
    ]
    claiming = sorted(a.market for a in adapters if a.supports_exit)
    # Alpaca's SELL is only reachable through the coordinator's held-long guard.
    assert claiming == ["NASDAQ"]
    assert NasdaqLiveAdapter.supports_exit is True


def test_nse_exit_route_reports_each_blocker_with_detail() -> None:
    route = _bare_nse_adapter().exit_route()
    assert route["supported"] is False
    assert route["route"] == "none"
    names = [name for name, _ in le.NSE_EXIT_BLOCKERS]
    assert route["blockers"] == names
    assert set(route["blocker_details"]) == set(names)
    assert len(names) == len(set(names)) >= 4
    assert all(len(detail) > 40 for detail in route["blocker_details"].values())
    assert "manually" in route["operator_action"]


def test_blockers_name_the_real_constraints_not_generic_text() -> None:
    joined = " ".join(f"{name} {detail}" for name, detail in le.NSE_EXIT_BLOCKERS).lower()
    for expected in ("stoploss", "target", "edis", "cancel_gtt", "short-term-positions"):
        assert expected in joined
    assert "naked" in joined or "short" in joined


def test_nse_sell_is_refused_before_any_broker_or_gate_access() -> None:
    adapter = _bare_nse_adapter()  # no gate, no catalog, no broker client
    with pytest.raises(LiveBrokerRejected) as excinfo:
        adapter.submit(
            client_order_id="exit-1",
            symbol="RELIANCE",
            side="SELL",
            qty=10,
            reference_price=1400.0,
            stop_loss=None,
            take_profit=None,
            product="D",
            instrument_meta=None,
        )
    message = str(excinfo.value)
    assert "exit_route_unsupported" in message
    assert "manually" in message


def test_nse_sell_refusal_is_case_insensitive_and_covers_every_product() -> None:
    for side in ("sell", "SELL", "Sell"):
        for product in ("D", "I", "MTF", ""):
            with pytest.raises(LiveBrokerRejected):
                _bare_nse_adapter().submit(
                    client_order_id="exit-2",
                    symbol="RELIANCE",
                    side=side,
                    qty=1,
                    reference_price=1400.0,
                    stop_loss=1380.0,
                    take_profit=1450.0,
                    product=product,
                    instrument_meta=None,
                )


def test_capability_profiles_agree_with_the_adapters() -> None:
    rows = {row["market"]: row for row in describe_capabilities()}
    assert rows["NSE"]["exit_route"]["routed"] is False
    assert rows["NASDAQ"]["exit_route"]["routed"] is True
    assert rows["NSE"]["exit_route"]["routed"] is NseLiveAdapter.supports_exit
    assert rows["NASDAQ"]["exit_route"]["routed"] is NasdaqLiveAdapter.supports_exit
    for market, row in rows.items():
        route = row["exit_route"]
        assert route["summary"] and route["operator_fallback"]
        assert route["opening_risks"], market
        assert route["closing_paths"], market


def test_nse_capability_records_the_specific_opening_risks() -> None:
    route = CAPABILITIES["NSE"].exit_route
    text = " ".join(route.opening_risks).lower()
    assert "entry rule" in text and "short" in text
    assert "single" in text
    assert "cancel" in text
    assert "settled" in text


def test_readiness_document_states_the_limitation() -> None:
    doc = (ROOT / "docs" / "LIVE_EXECUTION_READINESS.md").read_text(encoding="utf-8")
    lowered = doc.lower()
    assert "exit" in lowered
    assert "edis" in lowered
    assert "not routed" in lowered or "no exit route" in lowered
