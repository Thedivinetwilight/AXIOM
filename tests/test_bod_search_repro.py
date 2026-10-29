"""Reproduce the user-reported BOD search failure against the actual downloaded
BOD shape and verify the search results end-to-end.

Reported on build aa61866: refreshed BOD keeps 4,618 NSE_EQ rows, yet searches
for `RELIANCE` and the exact key `NSE_EQ|INE002A01018` return no rows.

Ground truth collected for this fix:
- Two independent captures of the REAL complete.json.gz (recorded HTTP fixture
  + a Nifty-100 vendored copy) show equity rows typed `instrument_type: "EQ"`
  with ISIN keys and `trading_symbol: "RELIANCE"`.
- The user's file is a drifted variant: under the old classifier only 2 NSE_EQ
  rows kept, which proves its equity rows are NOT typed "EQ"/blank (series
  codes per the official legend-of-series description). Variants in the wild
  also drift the key suffix away from the ISIN and carry the human symbol only
  in `name` — fields the search never looked at.

These tests run the REAL catalog route (no hand-rolled filter) against
fixtures faithful to both the captured real file and the evidenced drifts, and
pin that both reported queries resolve the RELIANCE row.
"""

from __future__ import annotations

import contextlib
import json
from datetime import datetime, timezone

import httpx
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.config.settings import Settings
from dataclasses import replace

NOW = datetime(2026, 10, 29, 5, 0, tzinfo=timezone.utc)
RELIANCE_KEY = "NSE_EQ|INE002A01018"

# Verbatim from the recorded real complete.json.gz response (2026 field set).
RELIANCE_REAL = {
    "segment": "NSE_EQ", "name": "RELIANCE INDUSTRIES LTD", "exchange": "NSE",
    "isin": "INE002A01018", "instrument_type": "EQ",
    "instrument_key": RELIANCE_KEY, "lot_size": 1, "freeze_quantity": 100000.0,
    "exchange_token": "2885", "tick_size": 10.0, "trading_symbol": "RELIANCE",
    "short_name": "Reliance Industries", "qty_multiplier": 1.0,
    "mtf_enabled": True, "mtf_bracket": 23.0, "security_type": "NORMAL",
    "cas_eligible": True,
}


def _equity(key, **changes):
    row = dict(RELIANCE_REAL, instrument_key=key)
    row.update(changes)
    return row


@contextlib.contextmanager
def _app_with_rows(tmp_path, rows):
    settings = replace(Settings(), api_token="local-api-test",
                       database_url=f"sqlite:///{tmp_path}/app.db")
    with TestClient(create_app(settings, data_dir=tmp_path)) as web:
        headers = {"Authorization": "Bearer local-api-test"}
        catalog = web.app.state.autonomy.catalog

        def fake_download(url):
            return (rows if "complete" in str(url) else []), "bod-digest"

        catalog._download = fake_download
        catalog.clock = lambda: NOW
        web.app.state.autonomy.clock = lambda: NOW
        assert web.post("/api/autonomy/catalog/refresh", headers=headers).status_code == 200
        yield web, headers


def _search(web, headers, query):
    response = web.get(f"/api/autonomy/catalog?limit=500&query={query}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["instruments"]


def test_real_captured_reliance_row_is_kept_and_found_by_both_queries(tmp_path):
    with _app_with_rows(tmp_path, [RELIANCE_REAL]) as (web, headers):
        rows = _search(web, headers, "RELIANCE")
        assert [r["instrument_key"] for r in rows] == [RELIANCE_KEY]
        rows = _search(web, headers, RELIANCE_KEY)
        assert [r["instrument_key"] for r in rows] == [RELIANCE_KEY]


def test_series_typed_equity_rows_stay_searchable(tmp_path):
    # The user's file variant types equity rows by exchange series (the old
    # classifier dropped every one of them; kept NSE_EQ was 2 of ~4.6k).
    rows = [
        _equity("NSE_EQ|INE002A01018", instrument_type="BE"),
        _equity("NSE_EQ|INE002A01019", instrument_type="SG",
                name="SOVEREIGN GOLD 2028", short_name="SGB28",
                trading_symbol="SGB28", isin="INE002A01019"),
    ]
    with _app_with_rows(tmp_path, rows) as (web, headers):
        catalog = web.app.state.autonomy.catalog
        assert catalog.stats["kept_by_segment"]["NSE_EQ"] == 2
        assert len(_search(web, headers, "RELIANCE")) == 1
        assert len(_search(web, headers, RELIANCE_KEY)) == 1


def test_symbol_only_in_name_variant_is_found(tmp_path):
    # Variant drift: trading_symbol/short_name empty and the human symbol only
    # in `name` — the reported symptom (both queries return no rows).
    rows = [_equity("NSE_EQ|INE002A01018", trading_symbol="", short_name="")]
    with _app_with_rows(tmp_path, rows) as (web, headers):
        by_name = _search(web, headers, "RELIANCE")
        assert [r["instrument_key"] for r in by_name] == [RELIANCE_KEY]
        assert [r["instrument_key"] for r in _search(web, headers, RELIANCE_KEY)] == [RELIANCE_KEY]


def test_non_isin_key_suffix_still_resolves_the_exact_key_query(tmp_path):
    # Variant drift: equity key suffix is the exchange token, not the ISIN.
    rows = [_equity("NSE_EQ|2885", trading_symbol="", short_name="")]
    with _app_with_rows(tmp_path, rows) as (web, headers):
        assert [r["instrument_key"] for r in _search(web, headers, "RELIANCE")] == ["NSE_EQ|2885"]
        # SEGMENT|ISIN must resolve through the ISIN alias to the drifted key.
        assert [r["instrument_key"] for r in _search(web, headers, RELIANCE_KEY)] == ["NSE_EQ|2885"]


def test_zero_width_contamination_does_not_defeat_search(tmp_path):
    rows = [_equity("NSE_EQ|\u200bINE002A01018", trading_symbol="Reli\u200bance")]
    with _app_with_rows(tmp_path, rows) as (web, headers):
        assert [r["instrument_key"] for r in _search(web, headers, RELIANCE_KEY)] == [RELIANCE_KEY]
        assert [r["instrument_key"] for r in _search(web, headers, "RELIANCE")] == [RELIANCE_KEY]


def test_search_survives_the_cache_roundtrip(tmp_path):
    # Restart path: the refreshed catalog is reloaded from the cache file and
    # both queries must still resolve.
    rows = [_equity("NSE_EQ|2885", trading_symbol="", short_name="", instrument_type="BE")]
    with _app_with_rows(tmp_path, rows) as (web, headers):
        assert len(_search(web, headers, "RELIANCE")) == 1
    settings = replace(Settings(), api_token="local-api-test",
                       database_url=f"sqlite:///{tmp_path}/app.db")
    with TestClient(create_app(settings, data_dir=tmp_path)) as web2:
        assert web2.app.state.autonomy.catalog.load_cache(require_current_day=False)
        assert len(_search(web2, headers, "RELIANCE")) == 1
        assert len(_search(web2, headers, RELIANCE_KEY)) == 1


def test_user_reported_distribution_is_accounted_with_series_rows_kept(tmp_path):
    # The user's old run: 118,937 rows, kept NSE_EQ 2 (EQ-typed only),
    # unsupported 93,650. Series-typed equity rows must now keep and search.
    rows = [_equity("NSE_EQ|INE002A01018", instrument_type="BE")]
    rows += [_equity(f"NSE_EQ|INE002A01{i:03d}", instrument_type="EQ",
                     name=f"STOCK {i}", short_name=f"S{i}", trading_symbol=f"S{i}",
                     isin=f"INE002A01{i:03d}") for i in range(1, 100)]
    with _app_with_rows(tmp_path, rows) as (web, headers):
        stats = web.get("/api/autonomy/catalog?limit=1", headers=headers).json()["stats"]
        assert stats["kept_by_segment"]["NSE_EQ"] == 100
        assert stats["kept_by_segment_and_type"]["NSE_EQ|BE"] == 1
        assert stats["dropped_invalid"] == 0
