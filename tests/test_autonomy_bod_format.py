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


def test_blank_or_missing_series_column_on_cash_segments_is_a_cash_equity(tmp_path):
    # Production loaders match live NSE_EQ rows as "EQ" or "" — the series
    # column is sometimes blank in complete.json.gz. A blank series on an
    # equity segment is a cash equity; derivatives must still name FUT/CE/PE.
    blank = dict(RELIANCE_RAW, instrument_type="")
    missing = {k: v for k, v in RELIANCE_RAW.items() if k != "instrument_type"}
    missing["instrument_key"] = "NSE_EQ|INE002A01019"
    blank_fo = {"instrument_key": "NCD_FO|9", "segment": "NCD_FO", "exchange": "CDS",
                "instrument_type": "", "tick_size": 0.25, "lot_size": 1,
                "freeze_quantity": 1000, "qty_multiplier": 1,
                "trading_symbol": "USDINR FUT", "underlying_symbol": "USDINR",
                "underlying_key": "NSE_INDEX|USDINR", "expiry": 1735689000000}
    catalog = refresh_with([blank, missing, blank_fo], tmp_path)
    assert RELIANCE_KEY in catalog.instruments
    assert catalog.instruments[RELIANCE_KEY].instrument_type == "EQ"
    assert "NSE_EQ|INE002A01019" in catalog.instruments
    assert "NCD_FO|9" not in catalog.instruments  # blank contract type still fails closed


def test_parse_stats_account_for_kept_and_dropped_rows(tmp_path):
    suspended_row = {"instrument_key": "NSE_EQ|INESUSP01010", "segment": "NSE_EQ",
                     "exchange": "NSE", "instrument_type": "EQ",
                     "tick_size": 5.0, "lot_size": 1, "freeze_quantity": 100000,
                     "qty_multiplier": 1, "trading_symbol": "SUSP"}
    index_row = {"instrument_key": "NSE_INDEX|Nifty 50", "segment": "NSE_INDEX",
                 "exchange": "NSE", "instrument_type": "INDEX"}
    expired_fo = {"instrument_key": "NCD_FO|1", "segment": "NCD_FO", "exchange": "CDS",
                  "instrument_type": "FUT", "tick_size": 0.25, "lot_size": 1,
                  "freeze_quantity": 1000, "qty_multiplier": 1,
                  "trading_symbol": "USDINR FUT", "underlying_symbol": "USDINR",
                  "underlying_key": "NSE_INDEX|USDINR",
                  "expiry": int((NOW - timedelta(days=30)).timestamp() * 1000)}
    broken = {"instrument_key": "BSE_EQ|BROKEN1", "segment": "BSE_EQ",
              "exchange": "BSE", "instrument_type": "EQ"}  # missing tick/lot/freeze

    def handler(request):
        if "complete" in request.url.path:
            body = [RELIANCE_RAW, suspended_row, index_row, expired_fo, broken]
        elif "suspended" in request.url.path:
            body = [suspended_row]
        else:
            body = []
        return httpx.Response(200, content=json.dumps(body).encode())

    catalog = InstrumentCatalog(
        AutonomySettings(), tmp_path / "catalog.json",
        transport=httpx.MockTransport(handler), clock=lambda: NOW,
    )
    catalog.refresh()
    stats = catalog.stats
    assert stats["source_rows"] == 5
    assert stats["suspended_keys"] == 1
    assert stats["kept_by_segment"] == {"NSE_EQ": 1}
    assert stats["dropped_suspended"] == 1
    assert stats["dropped_unsupported"] == 1
    assert stats["dropped_unsupported_by"] == {"NSE_INDEX|INDEX": 1}
    assert stats["dropped_invalid"] == 2  # expired F&O + broken equity row
    assert any("BSE_EQ|BROKEN1" in sample for sample in stats["dropped_invalid_samples"])
    assert any("NCD_FO|1" in sample for sample in stats["dropped_invalid_samples"])


def test_stats_survive_the_cache_round_trip(tmp_path):
    catalog = refresh_with([RELIANCE_RAW], tmp_path)
    assert catalog.stats["kept_by_segment"] == {"NSE_EQ": 1}
    reloaded = InstrumentCatalog(AutonomySettings(), tmp_path / "catalog.json",
                                 transport=None, clock=lambda: NOW)
    assert reloaded.load_cache(require_current_day=False)
    assert reloaded.stats["kept_by_segment"] == {"NSE_EQ": 1}
    assert reloaded.public()["stats"]["source_rows"] == 1


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


# ---------------------------------------------------------------------------
# Series-code instrument_type and authoritative key-prefix segment (2026-10).
#
# The official instruments documentation states instrument_type on equity
# segments carries the NSE/BSE share-series code (the docs link the exchange
# "legend of series" pages): EQ, BE, BZ, SM, ST, plus bond/gold series (SG,
# GS, N1, ...) — a real instrument dump shows NSE_EQ|ST/SG/BE/GS rows. The
# documented instrument_key format is SEGMENT|IDENTIFIER, so the key prefix is
# authoritative for the segment. Only real derivative contract types
# (FUT/CE/PE) may leave the cash branches. Every source row must land in
# exactly one accounting bucket (kept / suspended / unsupported / invalid).


def _refresh_full(rows, suspended_rows, tmp_path):
    def handler(request):
        if "suspended" in request.url.path:
            body = suspended_rows
        elif "complete" in request.url.path:
            body = rows
        else:
            body = []
        return httpx.Response(200, content=json.dumps(body).encode())

    catalog = InstrumentCatalog(
        AutonomySettings(), tmp_path / "catalog.json",
        transport=httpx.MockTransport(handler), clock=lambda: NOW,
    )
    catalog.refresh()
    return catalog


def test_bod_cash_series_codes_are_kept_and_key_prefix_segment_is_authoritative(tmp_path):
    def cash(key, series, symbol, segment="NSE_EQ", exchange="NSE"):
        return {"segment": segment, "name": symbol, "exchange": exchange,
                "isin": key.split("|", 1)[1], "instrument_type": series,
                "instrument_key": key, "lot_size": 1,
                "freeze_quantity": 100000.0, "exchange_token": "1",
                "tick_size": 5.0, "trading_symbol": symbol,
                "expiry": 1735689000000, "strike_price": 0.0}

    rows = [
        cash(RELIANCE_KEY, "EQ", "RELIANCE"),
        cash("NSE_EQ|INE839G01010", "BE", "JOCIL"),
        cash("NSE_EQ|INE002A01019", "", "BLANKSERIES"),
        cash("NSE_EQ|INE002A01020", "ST", "SMESTOCK"),
        cash("NSE_EQ|INE002A01021", "SG", "GOLDBOND"),
        cash("NSE_EQ|INE002A01022", "GS", "GSEC"),
        cash("BSE_EQ|INE002A01030", "A", "BSEA", segment="BSE_EQ", exchange="BSE"),
        cash("BSE_EQ|INE002A01031", "G", "BSEG", segment="BSE_EQ", exchange="BSE"),
        # Segment FIELD drifts to a short code; the key prefix stays official.
        dict(cash("NSE_EQ|INE002A01040", "EQ", "DRIFTED"), segment="EQ"),
        # Derivative contract types on a cash segment stay unsupported.
        dict(cash("NSE_EQ|INE002A01041", "CE", "WEIRDOPT")),
        # Present in the source file AND in the suspended list -> counted.
        cash("NSE_EQ|INESUSP01010", "EQ", "SUSPCO"),
    ]
    fo = {"instrument_key": "BCD_FO|2135614", "segment": "BCD_FO", "exchange": "BCD",
          "instrument_type": "CE", "tick_size": 0.25, "lot_size": 1,
          "freeze_quantity": 1000, "qty_multiplier": 1,
          "trading_symbol": "668GS2040 99.25 CE 24 JUN 27",
          "underlying_symbol": "668GS2040", "expiry": 1800000000000}
    rows += [
        fo,
        dict(fo, instrument_key="BCD_FO|2135615", instrument_type="FUT"),
        dict(fo, instrument_key="NCD_FO|77", segment="NCD_FO", instrument_type="PE"),
        dict(fo, instrument_key="NSE_FO|36708", segment="NSE_FO", instrument_type="CE"),
        dict(fo, instrument_key="NSE_FO|36702", segment="NSE_FO", instrument_type="FUT"),
        dict(fo, instrument_key="MCX_FO|434855", segment="MCX_FO", instrument_type="FUT"),
        dict(fo, instrument_key="NSE_INDEX|Nifty 50", segment="NSE_INDEX",
             instrument_type="INDEX"),
    ]
    suspended = [{"instrument_key": "NSE_EQ|INESUSP01010"}]
    catalog = _refresh_full(rows, suspended, tmp_path)

    # Every cash series on equity segments is kept with its exact key.
    for key, series in [(RELIANCE_KEY, "EQ"), ("NSE_EQ|INE839G01010", "BE"),
                        ("NSE_EQ|INE002A01019", "EQ"), ("NSE_EQ|INE002A01020", "ST"),
                        ("NSE_EQ|INE002A01021", "SG"), ("NSE_EQ|INE002A01022", "GS"),
                        ("BSE_EQ|INE002A01030", "A"), ("BSE_EQ|INE002A01031", "G"),
                        ("NSE_EQ|INE002A01040", "EQ")]:
        item = catalog.instruments.get(key)
        assert item is not None, f"{key} (series {series}) must be kept"
        assert item.instrument_type == series
    assert catalog.instruments[RELIANCE_KEY].segment == "NSE_EQ"
    assert catalog.instruments["NSE_EQ|INE002A01040"].segment == "NSE_EQ"
    for key in ("BCD_FO|2135614", "BCD_FO|2135615", "NCD_FO|77"):
        assert key in catalog.instruments, f"{key} must be kept"
    # Unsupported rows are dropped and NAMED with complete per-bucket counts.
    assert "NSE_EQ|INE002A01041" not in catalog.instruments
    stats = catalog.stats
    assert stats["dropped_unsupported_by"] == {
        "NSE_EQ|CE": 1, "NSE_FO|CE": 1, "NSE_FO|FUT": 1,
        "MCX_FO|FUT": 1, "NSE_INDEX|INDEX": 1,
    }
    assert stats["segment_field_mismatch"] == 1
    assert stats["dropped_suspended"] == 1
    assert stats["dropped_invalid"] == 0
    # Complete accounting: every source row lands in exactly one bucket.
    kept = sum(stats["kept_by_segment"].values())
    assert stats["source_rows"] == len(rows)
    assert kept + stats["dropped_suspended"] + stats["dropped_unsupported"] \
        + stats["dropped_invalid"] == stats["source_rows"]
    assert stats["kept_by_segment_and_type"]["NSE_EQ|EQ"] == 3
    assert stats["kept_by_segment_and_type"]["NSE_EQ|SG"] == 1
    assert stats["kept_by_segment_and_type"]["BSE_EQ|A"] == 1


def test_bod_unsupported_breakdown_has_no_twelve_bucket_cap(tmp_path):
    rows = []
    for index in range(20):
        rows.append({"instrument_key": f"NSE_FO|{index}", "segment": "NSE_FO",
                     "exchange": "NSE", "instrument_type": "CE", "tick_size": 5.0,
                     "lot_size": 50, "freeze_quantity": 1800, "qty_multiplier": 1,
                     "trading_symbol": f"X{index} CE", "expiry": 1800000000000})
    for index in range(15):
        rows.append({"instrument_key": f"MCX_FO|{index}", "segment": "MCX_FO",
                     "exchange": "MCX", "instrument_type": f"K{index}", "tick_size": 1.0,
                     "lot_size": 1, "freeze_quantity": 1, "qty_multiplier": 1,
                     "trading_symbol": f"Y{index}", "expiry": 1800000000000})
    rows.append(dict(RELIANCE_RAW))
    catalog = _refresh_full(rows, [], tmp_path)
    by = catalog.stats["dropped_unsupported_by"]
    assert by["NSE_FO|CE"] == 20
    assert sum(by.values()) == 35  # every bucket recorded, none capped away
