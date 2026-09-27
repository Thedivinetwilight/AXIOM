"""DatasetBuilder: versioned research datasets over the local candle store.

A dataset does NOT copy candle data; it records a versioned manifest of the
exact store partitions (with sha256 hashes), the universe source it came
from, quality classification and coverage at build time. `audit` re-verifies
hashes and coverage so any drift between dataset and store is detected.
"""

from __future__ import annotations

import json

from app.data.quality import quality_report
from app.history.store import CandleStore


class DatasetBuilder:
    def __init__(self, repository, store: CandleStore):
        self.repository = repository
        self.store = store

    def build(self, name: str, instrument_keys: list[str], unit: str, interval: int,
              from_date: str | None = None, to_date: str | None = None) -> dict:
        if not instrument_keys:
            raise ValueError("dataset needs at least one instrument")
        existing = self.repository.list_dataset_versions(name=name)
        version = (max((row["version"] for row in existing), default=0)) + 1
        parts: dict[str, list] = {}
        hashes: dict[str, dict] = {}
        coverage_refs = []
        from app.history.store import parse_local

        total_rows = 0
        for key in instrument_keys:
            rows = self.store.read_range(key, unit, int(interval), from_date, to_date)
            if not rows:
                continue
            report = quality_report(rows, unit=unit, interval=int(interval))
            partitions = self.store.store_fingerprint(key, unit, int(interval))
            keyed = {f"{key}::{partition}": digest for partition, digest in partitions.items()}
            parts[key] = {
                "records": len(rows),
                "first": rows[0]["ts"],
                "last": rows[-1]["ts"],
                "classification": report.classification,
            }
            hashes.update(keyed)
            total_rows += len(rows)
            coverage_refs.append(key)
        if not parts:
            raise ValueError("no local data for the requested instruments/range")
        record = {
            "name": name,
            "version": version,
            "instrument_keys": sorted(parts),
            "unit": unit,
            "interval": int(interval),
            "from_date": from_date,
            "to_date": to_date,
            "parts": [{"instrument": key, **info} for key, info in parts.items()],
            "hashes": hashes,
            "source_refs": {"store_root": str(self.store.root),
                            "store_format": self.store.format},
            "total_records": total_rows,
        }
        dataset_id = self.repository.record_dataset_version(record)
        return {"dataset_id": dataset_id, "version": version,
                "instruments": len(parts), "total_records": total_rows,
                "per_instrument": parts}

    def audit(self, dataset_id: str) -> dict:
        record = self.repository.get_dataset_version(dataset_id)
        if not record:
            raise ValueError(f"unknown dataset: {dataset_id}")
        stored_hashes = json.loads(record["hashes"])
        problems = []
        for composite, digest in stored_hashes.items():
            key, partition = composite.rsplit("::", 1)
            unit, interval = record["unit"], int(record["interval"])
            month = partition.split(".")[0]
            path = self.store.partition_path(key, unit, interval, month)
            if not path.exists():
                problems.append({"issue": "missing_partition", "path": str(path)})
                continue
            if self.store.file_hash(path) != digest:
                problems.append({"issue": "hash_mismatch", "path": str(path)})
        return {"dataset_id": dataset_id, "name": record["name"],
                "version": record["version"], "partitions": len(stored_hashes),
                "problems": problems, "ok": not problems,
                "created_ts": record["created_ts"]}
