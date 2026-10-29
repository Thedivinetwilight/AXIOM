"""Regression tests for the real Upstox BOD (complete.json.gz) format.

The live BOD pads cash-equity rows with an `expiry` epoch value that is NOT a
contract expiry. Parsing it as one made every NSE/BSE equity row fail the
"expired contracts cannot be selected" check and disappear from the catalog:
search for RELIANCE returned nothing and NSE_EQ|INE002A01018 was rejected as
an unknown exact key. These tests pin the real row shape (documented from the
official Upstox instruments documentation and real complete.json.gz copies)
so search, selection and session validation work for BOD-backed keys while
unknown keys and expired derivative contracts still fail closed.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.autonomy.config import AutonomySettings
from app.autonomy.models import Instrument
from app.autonomy.upstox import InstrumentCatalog
from app.config.settings import Settings

NOW = datetime(2026, 10, 29, 5, 0, tzinfo=timezone.utc)
RELIANCE_KEY = "NSE_EQ|INE002A01018"

# The exact real complete.json.gz row shape (Upstox instruments documentation
# sample, verified against real file copies; tick_size is paise, expiry is a
# padded epoch-ms value that must NOT gate cash equities).
RELIANCE_RAW = {
    "segment": "NSE_EQ", "name": "RELIANCE INDUSTRIES LTD",
    "exchange": "NSE", "isin": "INE002A01018",
    "instrument_type": "EQ", "instrument_key": RELIANCE_KEY,
    "lot_size": 1, "freeze_quantity": 100000.0, "exchange_token": "2885",
    "tick_size": 5.0, "trading_symbol": "RELIANCE", "short_name": "RELIANCE",
    "expiry": 1735689000000, "strike_price": 0.0,
}


def refresh_with(rows, tmp_path):
    def handler(request):
        body = rows if "complete" in request.url.path else []
        return httpx.Response(200, content=json.dumps(body).encode())

    catalog = InstrumentCatalog(
        AutonomySettings(), tmp_path / "catalog.json",
        transport=httpx.MockTransport(handler), clock=lambda: NOW,
    )
    catalog.refresh()
    return catalog


def test_real_bod_reliance_row_is_kept_with_exact_key_and_normalized_fields(tmp_path):
    catalog = refresh_with([RELIANCE_RAW], tmp_path)
    item = catalog.instruments.get(RELIANCE_KEY)
    assert item is not None, "the real BOD Reliance row must not be dropped"
    assert item.instrument_key == RELIANCE_KEY
    assert item.trading_symbol == "RELIANCE"
    assert item.segment == "NSE_EQ" and item.exchange == "NSE"
    assert item.tick_size == 0.05  # raw 5.0 paise -> rupees
    assert item.quantity_step == 1 and item.freeze_quantity == 100000
    assert item.expiry is None  # padded BOD epoch is not a contract expiry
    item.validate(now=NOW)  # session validation accepts this BOD-backed key


def test_real_bod_row_without_expiry_column_also_parses(tmp_path):
    # Real file copies vary: some rows omit the expiry column entirely.
    row = {k: v for k, v in RELIANCE_RAW.items() if k not in {"expiry", "strike_price", "qty_multiplier"}}
    catalog = refresh_with([row], tmp_path)
    assert RELIANCE_KEY in catalog.instruments


def test_derivative_expiry_rules_remain_strict(tmp_path):
    past = int((NOW - timedelta(days=30)).timestamp() * 1000)
    future = int((NOW + timedelta(days=30)).timestamp() * 1000)
    rows = [
        {"instrument_key": "NCD_FO|1", "segment": "NCD_FO", "exchange": "CDS",
         "instrument_type": "FUT", "tick_size": 0.25, "lot_size": 1,
         "freeze_quantity": 1000, "qty_multiplier": 1,
         "trading_symbol": "USDINR FUT", "underlying_symbol": "USDINR",
         "underlying_key": "NSE_INDEX|USDINR", "expiry": past},
        {"instrument_key": "NCD_FO|2", "segment": "NCD_FO", "exchange": "CDS",
         "instrument_type": "FUT", "tick_size": 0.25, "lot_size": 1,
         "freeze_quantity": 1000, "qty_multiplier": 1,
         "trading_symbol": "USDINR FUT", "underlying_symbol": "USDINR",
         "underlying_key": "NSE_INDEX|USDINR", "expiry": future},
    ]
    catalog = refresh_with(rows, tmp_path)
    assert "NCD_FO|1" not in catalog.instruments  # expired contract still rejected
    assert "NCD_FO|2" in catalog.instruments


def test_equity_cannot_carry_a_contract_expiry():
    item = Instrument(
        instrument_key=RELIANCE_KEY, display_symbol="NSE_EQ-INE002A01018",
        segment="NSE_EQ", exchange="NSE", trading_symbol="RELIANCE",
        instrument_type="EQ", tick_size=0.05, quantity_step=1,
        freeze_quantity=100000, contract_multiplier=1.0,
        expiry=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    try:
        item.validate()
    except ValueError as exc:
        assert "do not expire" in str(exc)
    else:
        raise AssertionError("equity contract expiry must fail closed")


def _web(tmp_path):
    settings = Settings(api_token="local-api-test", database_url=f"sqlite:///{tmp_path}/app.db")
    return TestClient(create_app(settings, data_dir=tmp_path))


def test_api_search_returns_reliance_row_and_session_accepts_exact_key(tmp_path):
    with _web(tmp_path) as web:
        headers = {"Authorization": "Bearer local-api-test"}
        catalog = web.app.state.autonomy.catalog

        def fake_download(url):
            return ([RELIANCE_RAW] if "complete" in str(url) else []), "bod-digest"

        catalog._download = fake_download
        refreshed = web.post("/api/autonomy/catalog/refresh", headers=headers)
        assert refreshed.status_code == 200
        assert refreshed.json()["count"] == 1

        # Symbol search returns the correct BOD row with its exact key.
        search = web.get("/api/autonomy/catalog", params={"query": "RELIANCE"}, headers=headers)
        assert search.status_code == 200
        rows = search.json()["instruments"]
        assert [row["instrument_key"] for row in rows] == [RELIANCE_KEY]
        assert rows[0]["trading_symbol"] == "RELIANCE"
        assert rows[0]["display_symbol"] == "NSE_EQ-INE002A01018"

        # Selecting that row submits its exact validated key to a PAPER session.
        created = web.post("/api/autonomy/sessions", headers=headers, json={
            "mode": "PAPER", "instrument_keys": [RELIANCE_KEY],
        })
        assert created.status_code in {200, 201}, created.text
        assert RELIANCE_KEY in created.text


def test_api_unknown_keys_still_fail_closed(tmp_path):
    with _web(tmp_path) as web:
        headers = {"Authorization": "Bearer local-api-test"}
        catalog = web.app.state.autonomy.catalog

        def fake_download(url):
            return ([RELIANCE_RAW] if "complete" in str(url) else []), "bod-digest"

        catalog._download = fake_download
        web.post("/api/autonomy/catalog/refresh", headers=headers)
        rejected = web.post("/api/autonomy/sessions", headers=headers, json={
            "mode": "PAPER", "instrument_keys": ["NSE_EQ|INE000000000"],
        })
        assert rejected.status_code >= 400
        assert "Unknown exact Upstox instrument key" in rejected.text
        # Mixed selection: the unknown key refuses the whole request.
        mixed = web.post("/api/autonomy/sessions", headers=headers, json={
            "mode": "PAPER", "instrument_keys": [RELIANCE_KEY, "NSE_EQ|INE000000000"],
        })
        assert mixed.status_code >= 400
        assert "Unknown exact Upstox instrument key" in mixed.text
