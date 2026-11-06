"""Phase 2 regression tests: NSE BOD reconciliation, classification, search, and off-hours."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path

from fastapi.testclient import TestClient
import httpx

from app.api.server import create_app
from app.autonomy.config import AutonomySettings
from app.autonomy.upstox import InstrumentCatalog, UpstoxReadOnly
from app.config.settings import Settings
from app.markets.nse_reconciliation import nse_session_status

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "upstox_bod_sanitized_master.json"
RELIANCE_KEY = "NSE_EQ|INE002A01018"


def _fixture_transport() -> httpx.MockTransport:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        low = url.lower()
        if url.endswith("complete.json.gz") or url.endswith("NSE.json.gz"):
            body = gzip.compress(json.dumps(fixture["complete"]).encode("utf-8"))
            return httpx.Response(200, content=body)
        if "suspended" in low:
            return httpx.Response(200, json=fixture["suspended"])
        if "mis" in low and "bse" in low:
            return httpx.Response(200, json=fixture["mis_bse"])
        if "mis" in low:
            return httpx.Response(200, json=fixture["mis_nse"])
        raise AssertionError(f"Unexpected catalog URL: {url}")

    return httpx.MockTransport(handler)


def _refreshed_catalog(tmp_path: Path) -> InstrumentCatalog:
    now = datetime(2026, 10, 29, 4, 0, tzinfo=timezone.utc)  # 09:30 IST Thursday
    catalog = InstrumentCatalog(
        AutonomySettings(),
        tmp_path / "instrument-catalog.json",
        transport=_fixture_transport(),
        clock=lambda: now,
    )
    catalog.refresh()
    return catalog


def test_sanitized_bod_master_reconciles_every_row_and_detects_duplicates(tmp_path: Path):
    catalog = _refreshed_catalog(tmp_path)
    stats = catalog.stats

    kept_total = sum(stats["kept_by_segment"].values())
    assert stats["unique_instruments"] == len(catalog.instruments)
    assert kept_total - len(stats["duplicate_keys"]) == len(catalog.instruments)
    assert (
        kept_total
        + stats["dropped_suspended"]
        + stats["dropped_unsupported"]
        + stats["dropped_invalid"]
        == stats["source_rows"]
    )
    assert stats["dropped_suspended"] == 1
    assert stats["duplicate_keys"] == [RELIANCE_KEY]
    assert catalog.instruments[RELIANCE_KEY].name == "RELIANCE INDUSTRIES LTD"
    assert stats["dropped_unsupported_by"]["NSE_INDEX|INDEX"] == 1
    assert stats["dropped_unsupported_by"]["NSE_FO|FUT"] == 1
    assert stats["watch_only_indices"] == 1
    assert "NSE_INDEX|Nifty 50" in catalog.indices
    assert catalog.indices["NSE_INDEX|Nifty 50"]["tradable"] is False


def test_nse_subclasses_series_settlement_and_metadata_fields(tmp_path: Path):
    catalog = _refreshed_catalog(tmp_path)

    rel = catalog.instruments[RELIANCE_KEY]
    assert rel.asset_subclass == "CASH_EQUITY"
    assert rel.product == "I"
    assert rel.mis_intraday_allowed is True
    assert rel.delivery_allowed is True
    assert rel.tick_size == 0.05
    assert rel.precision_decimals == 2
    assert rel.quantity_step == 1
    assert rel.isin == "INE002A01018"

    etf = catalog.instruments["NSE_EQ|INF204KB14I2"]
    assert etf.asset_subclass == "ETF"
    assert etf.mis_intraday_allowed is True

    t2t = catalog.instruments["NSE_EQ|INE669E01016"]
    assert t2t.asset_subclass == "TRADE_FOR_TRADE_EQUITY"
    assert t2t.product == "D"
    assert t2t.mis_intraday_allowed is False

    sme = catalog.instruments["NSE_EQ|INE888S01012"]
    assert sme.asset_subclass == "SME_EQUITY"
    assert sme.quantity_step == 1200

    sgb = catalog.instruments["NSE_EQ|IN0020230150"]
    assert sgb.asset_subclass == "SOVEREIGN_GOLD_BOND"
    assert sgb.product == "D"
    assert sgb.mis_intraday_allowed is False


def test_nse_search_handles_reliance_aliases_unicode_whitespace_and_ambiguity(tmp_path: Path):
    catalog = _refreshed_catalog(tmp_path)

    for variant in (
        "RELIANCE",
        "NSE_EQ|INE002A01018",
        "NSE_EQ-INE002A01018",
        "NSE:RELIANCE",
        "RELIANCE.NS",
        "INE002A01018",
        "  \u200bＲＥＬＩＡＮＣＥ\ufeff  ",
        "RELIANCE-EQ",
    ):
        res = catalog.search(variant)
        assert res["items"], f"expected match for {variant!r}"
        assert res["items"][0]["instrument_key"] == RELIANCE_KEY, (
            f"NSE_EQ RELIANCE must rank first for {variant!r}, got {res['items'][0]['instrument_key']}"
        )

    # Corporate ticker rename alias: ZOMATO -> ETERNAL
    alias_res = catalog.search("ZOMATO")
    assert alias_res["items"][0]["instrument_key"] == "NSE_EQ|INE758T01015"
    assert alias_res["items"][0]["matched_via_alias"] == "ZOMATO"

    # Multi-series ambiguity within NSE_EQ (EQ + BE sharing trading_symbol DUALSES)
    dual = catalog.search("DUALSES")
    assert dual["ambiguous"] is True
    assert "Multiple NSE_EQ series" in (dual["ambiguity_detail"] or "")
    assert dual["items"][0]["instrument_type"] == "EQ"

    # Watch-only index inclusion
    idx_res = catalog.search("NIFTY 50", include_indices=True)
    assert any(item["instrument_key"] == "NSE_INDEX|Nifty 50" and item["tradable"] is False for item in idx_res["items"])


def test_reconciliation_report_and_seed_fallback_honesty(tmp_path: Path):
    # Cold-start seed fallback never claims authoritative full-BOD reconciliation.
    cold = InstrumentCatalog(AutonomySettings(), tmp_path / "cold.json")
    assert cold.load_seed_fallback() is True
    cold_report = cold.reconciliation_report()
    assert cold_report["source_kind"] == "bundled_seed_reference"
    assert cold_report["authoritative_reconciled"] is False
    assert "NOT YET RECONCILED" in cold_report["honest_coverage_statement"]
    assert cold.search("RELIANCE")["items"][0]["instrument_key"] == RELIANCE_KEY

    # Refreshed BOD catalog reports full provenance, exclusions, and anomalies.
    live_cat = _refreshed_catalog(tmp_path)
    report = live_cat.reconciliation_report(now=datetime(2026, 10, 29, 13, 0, tzinfo=timezone.utc))
    assert report["authoritative_reconciled"] is True
    assert report["fresh_for_current_ist_day"] is True
    assert report["session"]["state"] == "POST_CLOSE"
    assert report["session"]["is_open"] is False
    assert report["exclusions_and_anomalies"]["duplicate_instrument_key_count"] == 1
    assert report["exclusions_and_anomalies"]["nse_eq_missing_or_invalid_isin_count"] == 1
    assert "DUALSES" in report["exclusions_and_anomalies"]["nse_eq_multi_series_symbol_samples"]
    assert report["licensing_and_terms"]["redistribution_prohibited"] is True


def test_upstox_daily_candles_and_off_hours_session_work_read_only():
    after_hours = datetime(2026, 10, 29, 14, 0, tzinfo=timezone.utc)  # 19:30 IST
    sess = nse_session_status(after_hours)
    assert sess["is_open"] is False and sess["state"] == "POST_CLOSE"

    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        seen_paths.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "candles": [
                        ["2026-10-29T00:00:00+05:30", 1410.0, 1435.5, 1402.0, 1428.75, 8450120, 0],
                        ["2026-10-28T00:00:00+05:30", 1398.0, 1415.0, 1392.5, 1411.20, 7120300, 0],
                    ]
                },
            },
        )

    reader = UpstoxReadOnly(
        lambda: "FAKE-READONLY-TOKEN",
        transport=httpx.MockTransport(handler),
        clock=lambda: after_hours,
    )
    bars = reader.daily_candles(RELIANCE_KEY, "NSE_EQ-INE002A01018", days=30)
    assert len(bars) == 2
    assert bars[0].timestamp < bars[1].timestamp
    assert bars[-1].close == 1428.75
    assert any("/v3/historical-candle/" in p and "/days/1/" in p for p in seen_paths)


def test_autonomy_catalog_reconciliation_route(tmp_path: Path):
    settings = replace(Settings(), api_token="local-api-test", database_url=f"sqlite:///{tmp_path}/app.db")
    headers = {"Authorization": "Bearer local-api-test"}
    with TestClient(create_app(settings, data_dir=tmp_path)) as web:
        web.app.state.autonomy.catalog.transport = _fixture_transport()
        assert web.post("/api/autonomy/catalog/refresh", headers=headers).status_code == 200
        rec = web.get("/api/autonomy/catalog/reconciliation", headers=headers)
        assert rec.status_code == 200
        body = rec.json()
        assert body["authoritative_reconciled"] is True
        assert body["counts"]["nse_eq_by_subclass"]["ETF"] == 1
        cat = web.get("/api/autonomy/catalog", params={"query": "NSE:RELIANCE"}, headers=headers).json()
        assert cat["instruments"][0]["instrument_key"] == RELIANCE_KEY
