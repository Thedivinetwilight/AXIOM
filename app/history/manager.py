"""HistoricalDataManager: official Upstox Historical Candle V3 + Intraday V3.

Documented contract (verified from the official docs this session):
- GET {base}/{instrument_key}/{unit}/{interval}/{to_date}/{from_date}
  unit ∈ minutes|hours|days|weeks|months; interval 1..300 (minutes),
  1..5 (hours), 1 (days/weeks/months).
- Availability: minutes/hours from 2022-01-01; days/weeks/months from
  2000-01-01. Per-request limits: minutes 1–15 → 1 month; minutes >15 and
  hours → 1 quarter; days → 1 decade; weeks/months unlimited.
- Intraday: GET {intraday_base}/{instrument_key}/{unit}/{interval} (today).
- Candles: [[ts, open, high, low, close, volume, oi], ...]

Behaviour: requests are chunked per the documented limits; every chunk's
outcome is persisted (download_jobs) so an interrupted download RESUMES at
the first incomplete chunk instead of restarting from zero. Data is
deduplicated on write (timestamp-keyed partitions). Retries are bounded with
backoff. Without credentials or network, sync records an honest "unavailable"
status and local state is untouched.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import date, datetime, timedelta
from app.utils.clock import utc_today

import httpx

from app.data.quality import quality_report
from app.history.store import CandleStore

logger = logging.getLogger("bot.history")

UNIT_LIMITS = {  # documented per-request retrieval windows
    "minutes_small": (1, 15, timedelta(days=31)),     # 1..15 min → 1 month
    "minutes_large": (16, 300, timedelta(days=92)),   # 16..300 min → 1 quarter
    "hours": (1, 5, timedelta(days=92)),
    "days": (1, 1, timedelta(days=3650)),
    "weeks": (1, 1, None),
    "months": (1, 1, None),
}
EPOCH_LIMITS = {"minutes": date(2022, 1, 1), "hours": date(2022, 1, 1),
                "days": date(2000, 1, 1), "weeks": date(2000, 1, 1),
                "months": date(2000, 1, 1)}
VALID_INTERVALS = {"minutes": set(range(1, 301)), "hours": set(range(1, 6)),
                   "days": {1}, "weeks": {1}, "months": {1}}


def chunk_plan(unit: str, interval: int, from_date: date, to_date: date) -> list[tuple[date, date]]:
    if unit == "minutes" and 1 <= interval <= 15:
        window = timedelta(days=31)
    elif unit in {"minutes", "hours"}:
        window = timedelta(days=92)
    elif unit == "days":
        window = timedelta(days=3650)
    else:
        return [(from_date, to_date)]
    plans = []
    cursor = from_date
    while cursor <= to_date:
        plans.append((cursor, min(cursor + window - timedelta(days=1), to_date)))
        cursor += window
    return plans


class HistoricalDataManager:
    def __init__(self, repository, store: CandleStore, settings, *,
                 transport: httpx.BaseTransport | None = None):
        self.repository = repository
        self.store = store
        self.settings = settings
        self._transport = transport

    # -- request layer -------------------------------------------------------

    def _headers(self) -> dict:
        token = (self.settings.market_data_access_token or "").strip()
        if not token:
            raise PermissionError(
                "Upstox access token required for historical data; nothing attempted")
        return {"Accept": "application/json", "Authorization": f"Bearer {token}"}

    def _get_candles(self, instrument_key: str, unit: str, interval: int,
                     from_date: date | None, to_date: date, *,
                     intraday: bool = False) -> list[dict]:
        if intraday:
            url = f"{self.settings.intraday_base}/{instrument_key}/{unit}/{interval}"
        else:
            url = (f"{self.settings.history_base_url}/{instrument_key}/{unit}/"
                   f"{interval}/{to_date.isoformat()}"
                   + (f"/{from_date.isoformat()}" if from_date else ""))
        last_error: Exception | None = None
        for attempt in range(max(1, int(self.settings.max_download_retries))):
            try:
                if self._transport is not None:
                    with httpx.Client(transport=self._transport, timeout=60) as client:
                        response = client.get(url, headers=self._headers())
                else:
                    response = httpx.get(url, headers=self._headers(), timeout=60)
                if response.status_code == 429:  # rate-aware: wait, then retry
                    time.sleep(1.5 ** attempt)
                    last_error = RuntimeError("rate limited (HTTP 429)")
                    continue
                if response.status_code != 200:
                    raise RuntimeError(
                        f"historical request failed: HTTP {response.status_code}")
                candles = response.json()["data"]["candles"]
                return [
                    {"ts": row[0], "open": row[1], "high": row[2], "low": row[3],
                     "close": row[4], "volume": row[5], "oi": row[6] if len(row) > 6 else 0}
                    for row in candles
                ]
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                last_error = exc
                time.sleep(1.5 ** attempt)
        raise RuntimeError(f"historical download failed after retries: {last_error}")

    # -- public API ----------------------------------------------------------

    def sync(self, instrument_key: str, unit: str, interval: int,
             from_date: str, to_date: str) -> dict:
        if unit not in VALID_INTERVALS or int(interval) not in VALID_INTERVALS[unit]:
            raise ValueError(f"invalid unit/interval: {unit}/{interval}")
        interval = int(interval)
        start = max(date.fromisoformat(from_date), EPOCH_LIMITS[unit])
        end = date.fromisoformat(to_date)
        if start > end:
            raise ValueError("from_date after to_date (after availability clamping)")

        job_id = hashlib.sha256(
            f"{instrument_key}|{unit}|{interval}|{from_date}|{to_date}".encode()
        ).hexdigest()[:24]
        plan = chunk_plan(unit, interval, start, end)
        job = {"id": job_id, "kind": "history", "instrument_key": instrument_key,
               "unit": unit, "interval": interval, "from_date": start.isoformat(),
               "to_date": end.isoformat(), "status": "running",
               "chunks_total": len(plan), "detail": json.dumps(
                   {"clamped_from": from_date, "availability": EPOCH_LIMITS[unit].isoformat()})}
        self.repository.record_download_job(job)

        # Resume: skip windows already covered by stored data.
        existing = self.repository.get_coverage(instrument_key, unit, interval)
        records_total = 0
        new_total = 0
        for index, (chunk_start, chunk_end) in enumerate(plan):
            if existing and existing["last_ts"] and existing["first_ts"]:
                # Exchange-local date of the last stored candle (a daily candle
                # stamped 2025-03-31T00:00+05:30 covers through March 31).
                from app.history.store import parse_local

                last_local = parse_local(existing["last_ts"]).date()
                if chunk_end <= last_local:
                    self.repository.update_download_job(
                        job_id, chunks_done=index + 1)
                    continue  # fully covered → no duplicate download
            try:
                candles = self._get_candles(
                    instrument_key, unit, interval, chunk_start, chunk_end)
            except PermissionError as exc:
                self.repository.update_download_job(job_id, status="blocked",
                                                    detail=str(exc))
                return {"job_id": job_id, "status": "blocked", "reason": str(exc),
                        "performed": False}
            except RuntimeError as exc:
                self.repository.update_download_job(job_id, status="interrupted",
                                                    chunks_done=index,
                                                    detail=str(exc))
                return {"job_id": job_id, "status": "interrupted",
                        "at_chunk": index, "chunks_total": len(plan),
                        "reason": str(exc), "performed": True}
            report = quality_report(candles, unit=unit, interval=interval)
            for finding in report.findings:
                self.repository.record_quality_event(
                    report.classification, finding.get("check", "unknown"),
                    finding, instrument_key=instrument_key, unit=unit,
                    interval=interval)
            stats = self.store.upsert_candles(
                instrument_key, unit, interval, candles)
            new_total += stats["new"]
            records_total += len(candles)
            self.repository.update_download_job(
                job_id, chunks_done=index + 1, records=records_total)
        self.refresh_coverage(instrument_key, unit, interval)
        self.repository.update_download_job(job_id, status="complete")
        return {"job_id": job_id, "status": "complete", "performed": True,
                "chunks": len(plan), "candles_seen": records_total,
                "new_records": new_total}

    def sync_intraday(self, instrument_key: str, unit: str = "minutes",
                      interval: int = 1) -> dict:
        try:
            candles = self._get_candles(instrument_key, unit, int(interval), None,
                                        utc_today(), intraday=True)
        except PermissionError as exc:
            return {"status": "blocked", "reason": str(exc), "performed": False}
        except RuntimeError as exc:
            return {"status": "unavailable", "reason": str(exc), "performed": False}
        stats = self.store.upsert_candles(instrument_key, unit, int(interval), candles)
        self.refresh_coverage(instrument_key, unit, int(interval))
        return {"status": "complete", "performed": True, "candles_seen": len(candles),
                "new_records": stats["new"]}

    def refresh_coverage(self, instrument_key: str, unit: str, interval: int) -> dict:
        rows = self.store.read_range(instrument_key, unit, interval)
        report = quality_report(rows, unit=unit, interval=interval)
        gaps = next((f for f in report.findings if f["check"] == "gaps"), {})
        completeness = -1.0  # not computable for calendar-based units (honest sentinel)
        if unit in {"minutes", "hours"} and rows:
            cadence_minutes = int(interval) if unit == "minutes" else 60 * int(interval)
            span = (datetime.fromisoformat(rows[-1]["ts"]) -
                    datetime.fromisoformat(rows[0]["ts"])).total_seconds() / 60
            expected = span / cadence_minutes + 1
            completeness = min(1.0, len(rows) / expected) if expected else 0.0
        gaps_list = gaps.get("examples", []) if isinstance(gaps, dict) else []
        self.repository.upsert_coverage(
            instrument_key, unit, int(interval),
            rows[0]["ts"] if rows else None, rows[-1]["ts"] if rows else None,
            len(rows), gaps_list, round(completeness, 4))
        return {"records": len(rows), "completeness": completeness,
                "classification": report.classification}

    def status(self, instrument_key: str | None = None) -> dict:
        coverage = self.repository.list_coverage(limit=100)
        if instrument_key:
            coverage = [c for c in coverage if c["instrument_key"] == instrument_key]
        jobs = self.repository.list_download_jobs(instrument_key=instrument_key)
        return {"coverage": coverage, "recent_jobs": jobs[:10],
                "store_format": self.store.format, "store_root": str(self.store.root)}

    def audit(self, instrument_key: str, unit: str, interval: int) -> dict:
        """Re-read partitions: hashes, quality, gaps. Records quality events."""
        coverage = self.refresh_coverage(instrument_key, unit, int(interval))
        rows = self.store.read_range(instrument_key, unit, interval)
        report = quality_report(rows, unit=unit, interval=interval)
        hashes = self.store.store_fingerprint(instrument_key, unit, interval)
        return {"instrument_key": instrument_key, "unit": unit, "interval": interval,
                **coverage, "classification": report.classification,
                "findings": report.findings, "partitions": len(hashes),
                "partition_hashes": hashes}
