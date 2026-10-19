"""Transport-level contract for Upstox historical/intraday downloads.

These are the real request paths used by `sync` and `sync_intraday`. Before
the fix, the intraday URL read `settings.intraday_base`, an attribute
`Settings` never defined, so every intraday sync died with AttributeError
before a single HTTP request — the "history/connectivity" symptom.
"""

from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import httpx
import pytest

from app.database.database import Database
from app.database.repository import Repository
from app.history.manager import HistoricalDataManager
from app.history.store import CandleStore

KEY = "NSE_EQ|INE002A01018"


@pytest.fixture
def stack(tmp_path):
    database = Database(f"sqlite:///{tmp_path}/history.db")
    repository = Repository(database)
    store = CandleStore(tmp_path / "store", parquet_enabled=False)
    yield repository, store
    database.close()


def _settings(**overrides):
    base = dict(
        market_data_access_token="tok",
        history_base_url="https://api.upstox.com/v3/historical-candle",
        intraday_base_url="https://api.upstox.com/v3/historical-candle/intraday",
        max_download_retries=2,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _candles(*days):
    return [[f"{day}T00:00:00+05:30", 100, 101, 99, 100.5, 1000, 0] for day in days]


class TestIntraday:
    def test_intraday_uses_the_documented_intraday_base(self, stack):
        repository, store = stack
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"data": {"candles": _candles("2026-10-19")}})

        manager = HistoricalDataManager(
            repository, store,
            _settings(market_data_access_token="secret-token"),
            transport=httpx.MockTransport(handler))
        result = manager.sync_intraday(KEY, "minutes", 5)

        assert result["status"] == "complete", result
        assert result["performed"] is True
        assert len(seen) == 1
        url = str(seen[0].url)
        assert url == (f"https://api.upstox.com/v3/historical-candle/intraday/"
                       f"{KEY}/minutes/5")
        assert seen[0].headers["Authorization"] == "Bearer secret-token"

    def test_missing_token_blocks_without_a_request(self, stack):
        repository, store = stack
        seen = []

        def handler(request):  # pragma: no cover - must not run
            seen.append(request)
            return httpx.Response(200, json={"data": {"candles": []}})

        manager = HistoricalDataManager(
            repository, store, _settings(market_data_access_token=""),
            transport=httpx.MockTransport(handler))
        result = manager.sync_intraday(KEY, "minutes", 1)
        assert result["status"] == "blocked" and result["performed"] is False
        assert seen == []


class TestHistorical:
    def test_url_carries_to_and_from_dates(self, stack):
        repository, store = stack
        seen = []

        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(200, json={"data": {"candles": _candles("2025-01-02")}})

        manager = HistoricalDataManager(repository, store, _settings(),
                                        transport=httpx.MockTransport(handler))
        manager._get_candles(KEY, "days", 1, date(2025, 1, 1), date(2025, 1, 10))
        assert seen == [f"https://api.upstox.com/v3/historical-candle/"
                        f"{KEY}/days/1/2025-01-10/2025-01-01"]

    def test_rate_limit_is_retried_then_succeeds(self, stack, monkeypatch):
        repository, store = stack
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, json={"error": "rate limit"})
            return httpx.Response(200, json={"data": {"candles": _candles("2025-01-02")}})

        monkeypatch.setattr("app.history.manager.time.sleep", lambda _s: None)
        manager = HistoricalDataManager(repository, store, _settings(),
                                        transport=httpx.MockTransport(handler))
        rows = manager._get_candles(KEY, "days", 1, None, date(2025, 1, 2))
        assert len(rows) == 1 and calls["n"] == 2

    def test_persistent_rate_limit_raises_without_the_token(self, stack, monkeypatch):
        repository, store = stack

        def handler(request):
            return httpx.Response(429, json={"error": "rate limit"})

        monkeypatch.setattr("app.history.manager.time.sleep", lambda _s: None)
        manager = HistoricalDataManager(
            repository, store, _settings(market_data_access_token="secret-token"),
            transport=httpx.MockTransport(handler))
        with pytest.raises(RuntimeError) as exc:
            manager._get_candles(KEY, "days", 1, None, date(2025, 1, 2))
        assert "429" in str(exc.value)
        assert "secret-token" not in str(exc.value)

    def test_http_error_is_reported_as_interrupted_not_complete(self, stack, monkeypatch):
        repository, store = stack

        def handler(request):
            return httpx.Response(500, json={"error": "server"})

        monkeypatch.setattr("app.history.manager.time.sleep", lambda _s: None)
        manager = HistoricalDataManager(repository, store, _settings(),
                                        transport=httpx.MockTransport(handler))
        result = manager.sync(KEY, "days", 1, "2025-01-01", "2025-01-10")
        assert result["status"] == "interrupted", result
        assert result["performed"] is True
        # The job row must not claim success.
        jobs = repository.list_download_jobs() if hasattr(repository, "list_download_jobs") else []
        for job in jobs:
            if job["id"] == result["job_id"]:
                assert job["status"] == "interrupted"
