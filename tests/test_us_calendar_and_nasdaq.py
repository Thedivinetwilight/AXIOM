"""Phase 3 tests: NYSE/NASDAQ calendar fixes, NASDAQ symbol discovery, and DPAPI data keys."""

from __future__ import annotations

from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import stat

import httpx
import pytest

from app.markets.alpaca_credentials import (
    FileAlpacaDataStore,
    MemoryAlpacaDataStore,
    resolve_alpaca_data_credentials,
)
from app.markets.nasdaq_catalog import NasdaqCatalog
from app.markets.us_calendar import USEquitiesCalendar, holidays


def test_nyse_rule_7_2_dec_31_2027_is_open_and_overrides_work():
    # Jan 1, 2028 is Saturday -> NYSE Rule 7.2 keeps Friday Dec 31, 2027 OPEN!
    assert date(2027, 12, 31) not in holidays(2027)
    cal = USEquitiesCalendar()
    assert cal.is_trading_day(date(2027, 12, 31)) is True
    stamp = datetime(2027, 12, 31, 15, 0, tzinfo=timezone.utc)  # 10:00 EST
    assert cal.is_open(stamp) is True

    # Sunday Jan 1, 2023 -> Monday Jan 2, 2023 IS observed closed
    assert date(2023, 1, 2) in holidays(2023)

    # Runtime operator overrides for unplanned closure & unplanned early close
    custom = USEquitiesCalendar(
        closed_dates={date(2026, 10, 29)},
        early_close_dates={date(2026, 10, 30)},
    )
    assert custom.is_trading_day(date(2026, 10, 29)) is False
    assert custom.market_state(datetime(2026, 10, 29, 15, 0, tzinfo=timezone.utc))["status"] == "CLOSED"
    bounds = custom.session_bounds(date(2026, 10, 30))
    assert bounds is not None and bounds[1].hour == 13


def test_nasdaq_catalog_seed_sync_etf_classification_and_aliases(tmp_path: Path):
    cache_file = tmp_path / "nasdaq-catalog.json"
    cat = NasdaqCatalog(cache_file)
    assert cat.source_kind == "bundled_seed_reference"
    assert cat.reconciliation_report()["authoritative_reconciled"] is False

    # Seed search handles prefixes, suffixes, full-width Unicode, and aliases
    for q in ("AAPL", "NASDAQ:AAPL", "AAPL.O", "$AAPL", "  \u200bＡＡＰＬ  "):
        found = cat.search(q)
        assert found["items"][0]["symbol"] == "AAPL"
    assert cat.search("GOOGLE")["items"][0]["symbol"] in {"GOOGL", "GOOG"}
    assert cat.get("QQQ").asset_subclass == "ETF"
    assert cat.get("AAPL").asset_subclass == "COMMON_STOCK"

    # Mocked /v2/assets sync from Alpaca Assets endpoint
    seen_requests: list[tuple[str, str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append((request.method, request.url.path, dict(request.url.params)))
        return httpx.Response(
            200,
            json=[
                {
                    "symbol": "AAPL",
                    "name": "Apple Inc. Common Stock",
                    "exchange": "NASDAQ",
                    "class": "us_equity",
                    "status": "active",
                    "tradable": True,
                    "fractionable": True,
                    "marginable": True,
                    "shortable": True,
                    "easy_to_borrow": True,
                },
                {
                    "symbol": "QQQ",
                    "name": "Invesco QQQ Trust ETF",
                    "exchange": "NASDAQ",
                    "class": "us_equity",
                    "status": "active",
                    "tradable": True,
                    "fractionable": True,
                },
                {
                    "symbol": "DEADTICK",
                    "name": "Delisted Corp",
                    "exchange": "NASDAQ",
                    "class": "us_equity",
                    "status": "inactive",
                    "tradable": False,
                },
            ],
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    report = cat.sync_from_alpaca("FAKE-KEY", "FAKE-SECRET", exchange="NASDAQ", client=client)
    assert report["authoritative_reconciled"] is True
    assert report["counts"]["total"] == 2
    assert report["counts"]["by_subclass"]["ETF"] == 1
    assert report["counts"]["by_subclass"]["COMMON_STOCK"] == 1
    assert seen_requests == [("GET", "/v2/assets", {"status": "active", "asset_class": "us_equity", "exchange": "NASDAQ"})]

    # Reload from persisted cache
    reloaded = NasdaqCatalog(cache_file)
    assert reloaded.source_kind == "authoritative_alpaca_assets"
    assert len(reloaded.instruments) == 2


def test_alpaca_market_data_secret_store_permissions_and_redaction(tmp_path: Path):
    mem = MemoryAlpacaDataStore()
    st = mem.save("AK-DATA-CANARY", "AS-DATA-CANARY", "iex")
    assert st["present"] is True and st["secret_returned"] is False
    assert "CANARY" not in json.dumps(st)

    creds, src = resolve_alpaca_data_credentials(mem)
    assert src == "protected-store" and creds["api_key"] == "AK-DATA-CANARY"

    if os.name != "nt":
        fstore = FileAlpacaDataStore(tmp_path / "secrets")
        fstore.save("AK-FILE-1", "AS-FILE-1", "sip")
        secret_file = tmp_path / "secrets" / "alpaca-market-data.secret.json"
        mode = stat.S_IMODE(secret_file.stat().st_mode)
        assert mode == 0o600
        assert fstore.delete() is True
        assert fstore.status()["present"] is False

    with pytest.raises(ValueError):
        mem.save("", "secret", "iex")
    with pytest.raises(ValueError):
        mem.save("key", "secret", "invalid_feed")
