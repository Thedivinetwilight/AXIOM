"""V2.1: HistoricalDataManager — chunking, resume, dedup, quality, coverage."""

import json
from datetime import date, timedelta

import httpx
import pytest

from app.config.settings import Settings
from app.database.database import Database
from app.database.repository import Repository
from app.history.manager import HistoricalDataManager, chunk_plan
from app.history.store import CandleStore


@pytest.fixture()
def stack(tmp_path):
    database = Database(f"sqlite:///{tmp_path}/history.db")
    repository = Repository(database)
    store = CandleStore(tmp_path / "store", parquet_enabled=False)
    settings = Settings.from_env()
    yield database, repository, store, settings
    database.close()


def day_handler(calls=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        parts = request.url.path.split("/")
        to_date, from_date = parts[6], parts[7]
        cur, end = date.fromisoformat(from_date), date.fromisoformat(to_date)
        rows = []
        while cur <= end:
            rows.append([f"{cur.isoformat()}T00:00:00+05:30", 100, 101, 99,
                         100.5, 1000, 0])
            cur += timedelta(days=1)
        return httpx.Response(200, json={"status": "success", "data": {"candles": rows}})
    return handler


def make_manager(repository, store, settings, handler, token="tok"):
    populated = {**settings.__dict__, "market_data_access_token": token,
                 "max_download_retries": 2}
    populated = type("S", (), populated)()
    return HistoricalDataManager(repository, store, populated,
                                 transport=httpx.MockTransport(handler))


class TestChunking:
    def test_documented_chunk_limits(self):
        plan = chunk_plan("minutes", 1, date(2025, 1, 1), date(2025, 3, 31))
        assert len(plan) == 3, "1-minute data: documented 1-month request windows"
        plan_hours = chunk_plan("hours", 1, date(2025, 1, 1), date(2025, 12, 31))
        assert len(plan_hours) == 4, "hours: documented 1-quarter windows (365/92)"
        plan_days = chunk_plan("days", 1, date(2020, 1, 1), date(2025, 12, 31))
        assert len(plan_days) == 1, "days: documented 1-decade window"
        plan_weeks = chunk_plan("weeks", 1, date(2000, 1, 1), date(2025, 12, 31))
        assert len(plan_weeks) == 1, "weeks: documented unlimited window"

    def test_availability_clamping(self, stack):
        _, repository, store, settings = stack
        manager = make_manager(repository, store, settings, day_handler())
        result = manager.sync("NSE_EQ|X", "minutes", 1, "2019-01-01", "2025-02-15")
        assert result["status"] == "complete"
        job = repository.list_download_jobs()[0]
        detail = json.loads(job["detail"])
        assert detail["clamped_from"] == "2019-01-01"
        assert detail["availability"] == "2022-01-01"

    def test_invalid_interval_rejected(self, stack):
        _, repository, store, settings = stack
        manager = make_manager(repository, store, settings, day_handler())
        with pytest.raises(ValueError):
            manager.sync("NSE_EQ|X", "minutes", 400, "2025-01-01", "2025-02-01")
        with pytest.raises(ValueError):
            manager.sync("NSE_EQ|X", "hours", 9, "2025-01-01", "2025-02-01")


class TestResumeAndDedup:
    def test_completed_download_never_redownloads(self, stack):
        _, repository, store, settings = stack
        calls = []
        manager = make_manager(repository, store, settings, day_handler(calls))
        first = manager.sync("NSE_EQ|X", "days", 1, "2025-01-01", "2025-03-31")
        assert first["status"] == "complete" and first["new_records"] == 90
        request_count = len(calls)
        second = manager.sync("NSE_EQ|X", "days", 1, "2025-01-01", "2025-03-31")
        assert second["new_records"] == 0
        assert len(calls) == request_count, "covered windows must not be re-fetched"
        assert len(store.read_range("NSE_EQ|X", "days", 1)) == 90, "no duplicate candles"

    def test_interrupted_download_resumes_from_failure_point(self, stack):
        _, repository, store, settings = stack
        calls = []
        real = day_handler(calls)
        attempts = {"n": 0}

        def flaky(request):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise httpx.ConnectError("network dropped")
            return real(request)

        manager = make_manager(repository, store, settings, flaky)
        result = manager.sync("NSE_EQ|X", "minutes", 1, "2025-01-01", "2025-02-15")
        assert result["status"] == "complete", result
        assert attempts["n"] >= 2
        rows = store.read_range("NSE_EQ|X", "minutes", 1, "2025-01-01", "2025-02-15")
        assert len(rows) == 46
        job = [j for j in repository.list_download_jobs() if j["status"] == "complete"][0]
        assert job["chunks_done"] == job["chunks_total"]

    def test_no_token_blocks_without_attempt(self, stack):
        _, repository, store, settings = stack
        manager = make_manager(repository, store, settings, day_handler(), token="")
        result = manager.sync("NSE_EQ|X", "days", 1, "2025-01-01", "2025-02-01")
        assert result["status"] == "blocked" and result["performed"] is False
        assert repository.list_download_jobs()[0]["status"] == "blocked"

    def test_intraday_merges_without_duplicates(self, stack):
        _, repository, store, settings = stack
        manager = make_manager(repository, store, settings, day_handler())
        manager.sync("NSE_EQ|X", "days", 1, "2025-01-01", "2025-01-10")
        from app.history.continuity import merge

        historical = store.read_range("NSE_EQ|X", "days", 1)
        intraday = [dict(row, close=row["close"] + 0.5) for row in historical[:3]]
        merged = merge(("historical", historical), ("intraday", intraday))
        assert len(merged) == len(historical)
        assert [row["source"] for row in merged[:3]] == ["intraday"] * 3
        assert merged[0]["close"] == historical[0]["close"] + 0.5


class TestQualityAndCoverage:
    def test_quality_classifications(self):
        from app.data.quality import quality_report

        good = [{"ts": f"2025-01-0{d}T09:15:00+05:30", "open": 100, "high": 101,
                 "low": 99, "close": 100.5, "volume": 10, "oi": 0}
                for d in range(1, 6)]
        assert quality_report(good, unit="days", interval=1).classification == "VALID"
        bad_ohlc = good + [{"ts": "2025-01-06T09:15:00+05:30", "open": 100,
                            "high": 90, "low": 99, "close": 100.5, "volume": 1, "oi": 0}]
        assert quality_report(bad_ohlc, unit="days", interval=1).classification == "INVALID"
        dupes = good + [good[-1]]
        assert quality_report(dupes, unit="days", interval=1).classification == "WARNING"
        assert quality_report([], unit="days", interval=1).classification == "UNVERIFIED"
        # daily/weekly/monthly calendars are not 'gaps' (no holiday fabrication)
        spaced = [good[0], good[-1]]
        assert quality_report(spaced, unit="days", interval=1).classification == "VALID"
        minute_rows = [{"ts": f"2025-01-01T09:{m:02d}:00+05:30", "open": 100,
                        "high": 101, "low": 99, "close": 100.5, "volume": 1, "oi": 0}
                       for m in (15, 16, 20)]  # 17,18,19 missing
        report = quality_report(minute_rows, unit="minutes", interval=1)
        assert report.classification == "MISSING"
        assert any(f["check"] == "gaps" for f in report.findings)

    def test_coverage_records_and_gap_detection(self, stack):
        _, repository, store, settings = stack
        manager = make_manager(repository, store, settings, day_handler())
        manager.sync("NSE_EQ|X", "days", 1, "2025-01-01", "2025-01-31")
        coverage = repository.get_coverage("NSE_EQ|X", "days", 1)
        assert coverage["records"] == 31
        assert coverage["first_ts"].startswith("2025-01-01")
        assert coverage["last_ts"].startswith("2025-01-31")
        assert coverage["completeness"] == -1.0  # calendar unit: honestly n/a
        audit = manager.audit("NSE_EQ|X", "days", 1)
        assert audit["classification"] == "VALID" and audit["partitions"] == 1
        assert audit["partition_hashes"], "audit records partition hashes"
