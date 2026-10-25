from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path

import httpx
import pytest

from app.autonomy.config import AutonomySettings
from app.autonomy.models import (
    AccountProfile, Candidate, DailyRisk, ExecutionPermit, Instrument, ProtectedOrder,
    StrategyEvidence, validate_order_route,
)
from app.autonomy.paper import PaperExecution
from app.autonomy.risk import initial_stop, trailing_stop
from app.autonomy.store import AutonomyStore
from app.autonomy.upstox import (
    AmbiguousSubmission, InstrumentCatalog, Quote, UpstoxLiveBroker, UpstoxReadOnly,
)
from app.data.models import Candle


UTC = timezone.utc
NOW = datetime(2026, 10, 25, 10, 0, tzinfo=UTC)
KEY = "NSE_EQ|INE000000000"


def instrument(**changes):
    values = dict(
        instrument_key=KEY, display_symbol="NSE_EQ-INE000000000", segment="NSE_EQ",
        exchange="NSE", trading_symbol="TEST", instrument_type="EQ", tick_size=0.05,
        quantity_step=1, freeze_quantity=100000, contract_multiplier=1.0,
        short_route_metadata=True,
    )
    values.update(changes)
    return Instrument(**values)


def order(**changes):
    values = dict(intent_id="intent-1", instrument_key=KEY, side="BUY", quantity=10,
                  limit_price=100.0, stop_price=95.0, target_price=110.0)
    values.update(changes)
    return ProtectedOrder(**values)


def permit(kind="ENTRY", session="session"):
    return ExecutionPermit(session, kind, "nonce", datetime.now(UTC) + timedelta(minutes=2))


def bar(minute=0, open=99.0, high=101.0, low=98.0, close=100.0, volume=10000):
    return Candle(NOW + timedelta(minutes=minute), open, high, low, close, volume,
                  instrument().display_symbol)


def test_live_is_disabled_by_default_and_legacy_flags_are_not_used(monkeypatch):
    monkeypatch.delenv("AUTONOMY_LIVE_ENABLED", raising=False)
    monkeypatch.setenv("MODE", "LIVE")
    monkeypatch.setenv("LIVE_TRADING", "true")
    settings = AutonomySettings.from_env()
    assert settings.live_enabled is False


def test_missing_currency_multiplier_is_not_tradable():
    item = instrument(
        instrument_key="NCD_FO|123", display_symbol="NCD_FO-123", segment="NCD_FO",
        exchange="CDS", instrument_type="FUT", underlying_symbol="USDINR",
        contract_multiplier=None,
    )
    with pytest.raises(ValueError, match="multiplier"):
        item.validate()
    assert item.currency_derivative and "not spot forex" in item.public()["product_label"]


def test_adapter_route_checks_tick_lot_freeze_and_geometry():
    validate_order_route(order(), instrument())
    with pytest.raises(ValueError, match="tick"):
        validate_order_route(order(limit_price=100.01), instrument())
    with pytest.raises(ValueError, match="geometry"):
        validate_order_route(order(stop_price=101.0), instrument())
    with pytest.raises(ValueError, match="lot/freeze"):
        validate_order_route(order(quantity=100001), instrument())
    with pytest.raises(ValueError, match="lot/freeze"):
        validate_order_route(order(quantity=3), instrument(quantity_step=2))


def test_confirmed_swing_excludes_forming_side_and_trailing_never_loosens():
    bars = [
        Candle(NOW + timedelta(minutes=i), 100 + i, 102 + i, 99 + i, 101 + i, 1000, "X")
        for i in range(8)
    ]
    # Insert a confirmed low with two completed bars on each side.
    bars[3] = replace(bars[3], low=90.0, open=99.0, close=100.0)
    stop, evidence = initial_stop(bars, "BUY", 110.0, 0.05, lookback=2)
    assert stop == 89.9 and "confirmed" in evidence
    old = stop
    result = trailing_stop(bars, "BUY", old, 110.0, 0.05, lookback=2)
    assert result is None or result[0] > old
    with pytest.raises(ValueError):
        initial_stop(bars[:3], "BUY", 110.0, 0.05, lookback=2)


def test_paper_requires_permit_and_has_no_live_mutation_route():
    paper = PaperExecution({KEY: instrument()}, starting_equity=100000,
                           commission_rate=0.001, slippage_rate=0.0,
                           spread_rate=0.0, max_participation=0.1)
    with pytest.raises(PermissionError):
        paper.place_protected_entry(order(), None, session_id="session", instrument=instrument())
    assert paper.broker_mutations_reachable is False
    assert not hasattr(paper, "place_order")
    assert not hasattr(paper, "cancel_order")
    assert not hasattr(paper, "modify_order")


def test_paper_partial_fill_and_conservative_same_bar_stop_priority():
    paper = PaperExecution({KEY: instrument()}, starting_equity=100000,
                           commission_rate=0.0, slippage_rate=0.0,
                           spread_rate=0.0, max_participation=0.1)
    paper.place_protected_entry(order(quantity=100), permit(), session_id="session",
                                instrument=instrument())
    # Capacity is one unit; entry is partial and the remainder is not assumed filled.
    paper.on_bar(bar(open=99.0, low=98.0, high=100.0, close=99.0, volume=10))
    position = paper.positions[KEY]
    assert position.quantity == 1
    identifier = position.protection_id
    assert paper.gtt[identifier]["rules"][0]["status"] == "PARTIALLY_FILLED"
    # Both target and stop are inside this bar: stop wins, including a gap.
    paper.on_bar(bar(minute=1, open=90.0, low=89.0, high=111.0, close=100.0, volume=10000))
    assert KEY not in paper.positions
    assert paper.day_realised < 0
    assert paper.gtt[identifier]["rules"][1]["status"] == "COMPLETED"
    assert paper.gtt[identifier]["rules"][2]["status"] == "CANCELLED"


def test_paper_gap_through_stop_uses_worse_open_not_stop_price():
    paper = PaperExecution({KEY: instrument()}, starting_equity=100000,
                           commission_rate=0.0, slippage_rate=0.0,
                           spread_rate=0.0, max_participation=1.0)
    paper.place_protected_entry(order(), permit(), session_id="session", instrument=instrument())
    paper.on_bar(bar(open=99.0, low=98.0, high=100.0, close=99.0, volume=10000))
    paper.on_bar(bar(minute=1, open=90.0, low=89.0, high=91.0, close=90.0, volume=10000))
    assert KEY not in paper.positions
    # Buy at 99 and exit at the 90 gap, not at the 95 stop.
    assert paper.day_realised == pytest.approx(-90.0)


def test_store_recovers_active_session_and_redacts_secrets(tmp_path):
    path = tmp_path / "journal.sqlite3"
    store = AutonomyStore(path)
    store.create_session("s", "2026-10-25", "PAPER")
    store.update_session("s", state="ACTIVE", risk={"max_daily_loss": 10})
    store.record_intent("i", "s", "signal", KEY,
                        {"access_token": "never-store", "nested": {"password": "never"}}, None)
    store.audit("TEST", {"authorization": "never"})
    assert store.verify_chain()
    store.close()
    recovered = AutonomyStore(path)
    assert recovered.session("s")["state"] == "HALTED"
    payload = json.loads(recovered.intent("i")["payload_json"])
    assert payload["access_token"] == "[REDACTED]"
    assert payload["nested"]["password"] == "[REDACTED]"
    assert recovered.unresolved("s")[0]["state"] == "RECORDED"
    recovered.close()


def test_mock_upstox_readonly_quote_and_live_gtt_payload():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/v2/market-quote/full":
            return httpx.Response(200, json={"status": "success", "data": {KEY: {
                "last_price": 100.0, "volume": 1000, "timestamp": "2026-10-25T10:00:00+00:00",
                "depth": {"buy": [{"price": 99.95}], "sell": [{"price": 100.05}]},
            }}})
        if request.url.path == "/v3/order/gtt/place":
            body = json.loads(request.content)
            assert body["instrument_token"] == KEY
            assert {rule["strategy"] for rule in body["rules"]} == {"ENTRY", "STOPLOSS", "TARGET"}
            assert request.headers["X-Algo-Name"] == "AXIOM-TEST"
            return httpx.Response(200, json={"status": "success",
                                             "data": {"gtt_order_ids": ["GTT-TEST-1"]}})
        if request.url.path == "/v3/order/gtt":
            return httpx.Response(200, json={"status": "success", "data": [{
                "gtt_order_id": "GTT-TEST-1", "instrument_token": KEY, "quantity": 10,
                "product": "I", "rules": [
                    {"strategy": "ENTRY", "status": "COMPLETED", "trigger_type": "IMMEDIATE",
                     "trigger_price": 100.0, "transaction_type": "BUY", "order_id": "E1"},
                    {"strategy": "STOPLOSS", "status": "OPEN", "trigger_type": "IMMEDIATE",
                     "trigger_price": 95.0, "order_id": "S1"},
                    {"strategy": "TARGET", "status": "OPEN", "trigger_type": "IMMEDIATE",
                     "trigger_price": 110.0, "order_id": "T1"},
                ],
            }]})
        raise AssertionError(request.url.path)

    transport = httpx.MockTransport(handler)
    reader = UpstoxReadOnly(lambda: "test-only", transport=transport, clock=lambda: NOW)
    quote = reader.quote(KEY)
    assert quote.bid == 99.95 and quote.ask == 100.05
    live = UpstoxLiveBroker(lambda: "test-only", algo_name="AXIOM-TEST",
                            transport=transport, clock=lambda: NOW)
    submission = live.place_protected_entry(order(), permit(), session_id="session",
                                            instrument=instrument())
    assert submission.broker_ids == ("GTT-TEST-1",)
    assert sum(request.url.path == "/v3/order/gtt/place" for request in requests) == 1


def test_mock_upstox_timeout_is_ambiguous_and_not_retried():
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectTimeout("test timeout", request=request)

    live = UpstoxLiveBroker(lambda: "test-only", algo_name="AXIOM-TEST",
                            transport=httpx.MockTransport(handler), clock=lambda: NOW)
    with pytest.raises(AmbiguousSubmission):
        live.place_protected_entry(order(), permit(), session_id="session", instrument=instrument())
    assert len(calls) == 1


def test_catalog_normalizes_paise_ticks_and_rejects_unknown_contract_multiplier(tmp_path):
    settings = AutonomySettings()
    complete = [{
        "instrument_key": KEY, "segment": "NSE_EQ", "exchange": "NSE", "instrument_type": "EQ",
        "tick_size": 5.0, "lot_size": 1, "freeze_quantity": 100000,
        "trading_symbol": "TEST", "qty_multiplier": 1,
    }, {
        "instrument_key": "NCD_FO|1", "segment": "NCD_FO", "exchange": "CDS",
        "instrument_type": "FUT", "tick_size": 0.25, "lot_size": 1,
        "freeze_quantity": 1000, "trading_symbol": "USDINR FUT", "underlying_symbol": "USDINR",
    }]

    def handler(request):
        return httpx.Response(200, content=json.dumps(complete if "complete" in request.url.path else []).encode())

    catalog = InstrumentCatalog(settings, tmp_path / "catalog.json",
                                transport=httpx.MockTransport(handler), clock=lambda: NOW)
    result = catalog.refresh()
    assert result["count"] == 2
    assert catalog.instruments[KEY].tick_size == 0.05
    assert catalog.instruments["NCD_FO|1"].contract_multiplier is None
    with pytest.raises(ValueError):
        catalog.instruments["NCD_FO|1"].validate()
    assert catalog.load_cache(require_current_day=False)
