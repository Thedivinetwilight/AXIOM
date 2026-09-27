"""MarketUniverseManager: official Upstox instrument metadata, stored locally.

Source: the public instrument files (verified schema):
  https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz
  https://assets.upstox.com/market-quote/instruments/exchange/{EXCHANGE}.json.gz

Identity: instrument_key. Each sync records added / changed / removed /
unchanged counts. The official file does NOT carry a suspended flag, so the
local `state` column is active/removed only (a suspension cannot be inferred
and is never guessed). Offline or failed syncs change nothing and are
recorded with an honest status.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
from datetime import datetime, timezone
from urllib.request import Request, urlopen

logger = logging.getLogger("bot.universe")

KNOWN_FIELDS = (
    "trading_symbol", "name", "exchange", "segment", "instrument_type", "isin",
    "lot_size", "tick_size", "freeze_quantity", "expiry", "strike_price",
    "option_type", "underlying_key",
)


def _ms_to_iso(value) -> str | None:
    if value in (None, "", 0):
        return None
    try:
        return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError):
        return None


NUMERIC_FIELDS = ("lot_size", "tick_size", "freeze_quantity", "strike_price")


def canonical_fields(raw: dict) -> dict:
    """Fields exactly as they are persisted, so hashes stay stable."""
    fields = {name: raw.get(name) for name in KNOWN_FIELDS}
    if fields.get("expiry") is not None:
        fields["expiry"] = _ms_to_iso(fields["expiry"]) or fields["expiry"]
    for name in NUMERIC_FIELDS:
        value = fields.get(name)
        fields[name] = float(value) if value not in (None, "") else None
    return fields


def normalize_record(raw: dict) -> dict:
    key = raw.get("instrument_key")
    if not key:
        raise ValueError("instrument file row without instrument_key")
    fields = canonical_fields(raw)
    row_hash = hashlib.sha256(
        json.dumps(fields, sort_keys=True, default=str).encode()
    ).hexdigest()
    return {
        "instrument_key": key,
        **fields,
        "details": json.dumps(raw, sort_keys=True, default=str),
        "row_hash": row_hash,
        "state": "active",
    }


class MarketUniverseManager:
    def __init__(self, repository, *, url: str | None = None, transport_hook=None):
        self.repository = repository
        self.url = url or ("https://assets.upstox.com/market-quote/instruments/"
                           "exchange/complete.json.gz")
        self._transport_hook = transport_hook  # tests: inject bytes

    def _download(self) -> bytes:
        if self._transport_hook is not None:
            return self._transport_hook(self.url)
        request = Request(self.url, headers={"Accept-Encoding": "gzip"})
        with urlopen(request, timeout=120) as response:
            payload = response.read()
        if self.url.endswith(".gz") or payload[:2] == b"\x1f\x8b":
            payload = gzip.decompress(payload)
        return payload

    def sync(self) -> dict:
        sync_id = hashlib.sha256(f"{self.url}".encode()).hexdigest()[:16] + "-" + \
            datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        try:
            payload = self._download()
        except Exception as exc:  # offline / network error: state untouched
            result = {"sync_id": sync_id, "status": "unavailable",
                      "reason": type(exc).__name__, "performed": False}
            self.repository.record_universe_sync(
                sync_id, self.url, None, {}, "unavailable", result)
            logger.warning("UNIVERSE_SYNC_UNAVAILABLE", extra={"error": type(exc).__name__})
            return result
        file_hash = hashlib.sha256(payload).hexdigest()
        raw_rows = json.loads(payload)
        records = [normalize_record(row) for row in raw_rows]
        totals = self.repository.upsert_instruments(records, source=self.url)
        known = set(self.repository.all_instrument_keys())
        seen = {record["instrument_key"] for record in records}
        removed = self.repository.mark_absent_instruments(sorted(known - seen))
        totals["total"] = len(records)
        totals["removed"] = removed
        self.repository.record_universe_sync(
            sync_id, self.url, file_hash, totals, "complete", {"rows": len(records)})
        result = {"sync_id": sync_id, "status": "complete", "performed": True,
                  "file_hash": file_hash, **totals}
        logger.info("UNIVERSE_SYNCED", extra={
            "added": totals["added"], "changed": totals["changed"],
            "removed": totals["removed"]})
        return result

    def status(self) -> dict:
        counts = self.repository.instrument_counts()
        syncs = self.repository.list_universe_syncs(limit=3)
        return {"counts": counts, "recent_syncs": syncs,
                "source": self.url}

    def audit(self) -> dict:
        """Internal consistency: hash integrity, states, orphans, empties."""
        problems = []
        instruments = self.repository.list_instruments(state=None, limit=1_000_000)
        for row in instruments:
            if not row["instrument_key"]:
                problems.append({"issue": "empty_key", "id": row["id"] if "id" in row else None})
            stored_fields = {name: row.get(name) for name in KNOWN_FIELDS}
            for name in NUMERIC_FIELDS:
                value = stored_fields.get(name)
                stored_fields[name] = float(value) if value not in (None, "") else None
            recomputed = hashlib.sha256(json.dumps(
                stored_fields, sort_keys=True, default=str).encode()).hexdigest()
            if recomputed != row["row_hash"]:
                problems.append({"issue": "hash_mismatch",
                                 "instrument_key": row["instrument_key"]})
            if row["state"] not in {"active", "removed"}:
                problems.append({"issue": "unknown_state",
                                 "instrument_key": row["instrument_key"],
                                 "state": row["state"]})
        syncs = self.repository.list_universe_syncs(limit=1)
        if not syncs:
            problems.append({"issue": "never_synced"})
        elif syncs[0]["status"] != "complete":
            problems.append({"issue": "last_sync_not_complete", "status": syncs[0]["status"]})
        return {"instruments": len(instruments), "problems": problems,
                "ok": not problems}
