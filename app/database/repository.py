"""Atomic paper checkpoints + queryable records; one paper writer per database."""

import json
from dataclasses import asdict
from datetime import datetime, timezone
from uuid import uuid4


def encode(value):
    return json.dumps(value, allow_nan=False, separators=(",", ":"))


class _V1RepositoryBase:
    def __init__(self, database):
        self.db = database
        self._writer_file = None
        self._memory_writer = False

    def acquire_writer(self):
        if self._writer_file or self._memory_writer:
            raise RuntimeError("Paper writer already active")
        if self.db.path == ":memory:":
            self._memory_writer = True
            return
        handle = open(self.db.path + ".paper.lock", "a+b")
        try:
            import os

            if os.name == "nt":
                import msvcrt

                handle.write(b"0")
                handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, ImportError) as exc:
            handle.close()
            raise RuntimeError(
                "Another paper process owns this database (or locking unsupported)"
            ) from exc
        self._writer_file = handle

    def release_writer(self):
        self._memory_writer = False
        if self._writer_file:
            self._writer_file.close()
            self._writer_file = None

    def save_backtest(self, result):
        row = result.to_dict()
        with self.db.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO backtests VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    row["id"],
                    row["strategy"],
                    encode(row["parameters"]),
                    row["symbol"],
                    row["timeframe"],
                    row["start_date"],
                    row["end_date"],
                    encode(row["metrics"]),
                    row["created_at"],
                    encode(row),
                ),
            )

    def list_backtests(self, limit=100, offset=0):
        with self.db.transaction() as db:
            rows = db.execute(
                "SELECT id,strategy,parameters,symbol,timeframe,start_date,end_date,metrics,created_at FROM backtests ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        result = [dict(row) for row in rows]
        for row in result:
            for key in ("parameters", "metrics"):
                row[key] = json.loads(row[key])
        return result

    def get_backtest(self, backtest_id):
        with self.db.transaction() as db:
            row = db.execute("SELECT payload FROM backtests WHERE id=?", (backtest_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def load_broker(self):
        with self.db.transaction() as db:
            row = db.execute("SELECT payload FROM state WHERE key='paper' ").fetchone()
        return json.loads(row[0]) if row else None

    def save_broker(self, broker, snapshot=None):
        if not (self._writer_file or self._memory_writer):
            raise RuntimeError("Paper write requires exclusive ownership")
        with self.db.transaction() as db:
            db.execute("INSERT OR REPLACE INTO state VALUES ('paper', ?)", (encode(broker.dump()),))
            for order in broker.get_orders():
                db.execute(
                    "INSERT OR REPLACE INTO orders VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        order["id"],
                        order["symbol"],
                        order["side"],
                        order["quantity"],
                        order["price"],
                        order["status"],
                        order["strategy"],
                        order["timestamp"],
                        encode(order),
                    ),
                )
            for fill in broker.fills:
                db.execute(
                    "INSERT OR IGNORE INTO fills VALUES (?,?,?,?)",
                    (fill.id, fill.order_id, fill.timestamp, encode(asdict(fill))),
                )
            for trade in broker.portfolio.trades:
                db.execute(
                    "INSERT OR IGNORE INTO trades VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        trade["id"],
                        trade["entry"],
                        trade["exit"],
                        trade["quantity"],
                        trade["pnl"],
                        trade["strategy"],
                        trade["entry_timestamp"],
                        trade["exit_timestamp"],
                        encode(trade),
                    ),
                )
            if snapshot:
                db.execute(
                    "INSERT OR REPLACE INTO portfolio_snapshots VALUES (?,?,?,?,?)",
                    (
                        snapshot["timestamp"],
                        snapshot["cash"],
                        snapshot["equity"],
                        snapshot["drawdown"],
                        encode(snapshot),
                    ),
                )

    def save_research(self, kind, payload):
        research_id = uuid4().hex
        with self.db.transaction() as db:
            db.execute(
                "INSERT INTO research_runs VALUES (?,?,?,?)",
                (research_id, kind, datetime.now(timezone.utc).isoformat(), encode(payload)),
            )
        return research_id

    def list_research(self, limit=20):
        with self.db.transaction() as db:
            return [
                dict(row, payload=json.loads(row["payload"]))
                for row in db.execute(
                    "SELECT * FROM research_runs ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
            ]

    # ------------------------------------------------------------------
    # V1 (Arena): learning journal, sandbox order audit, engine state.
    # ------------------------------------------------------------------

    def record_signal(self, record: dict) -> str:
        signal_id = record.get("id") or uuid4().hex
        with self.db.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO signals VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    signal_id, record["ts"], record["strategy"],
                    record.get("params_version", "baseline"), record["symbol"],
                    record["timeframe"], record.get("regime"), record["signal"],
                    record.get("price"), encode(record),
                ),
            )
        return signal_id

    def list_signals(self, limit=500):
        with self.db.transaction() as db:
            return [
                dict(row, payload=json.loads(row["payload"]))
                for row in db.execute(
                    "SELECT * FROM signals ORDER BY ts DESC LIMIT ?", (limit,)
                ).fetchall()
            ]

    def record_sandbox_order(self, record: dict) -> None:
        with self.db.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO sandbox_orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record["id"], record.get("upstox_order_id"), record["symbol"],
                    record["side"], record["quantity"], record.get("price"),
                    record.get("order_type", "MARKET"), record["status"],
                    record.get("tag"), record["created_ts"], record.get("updated_ts"),
                    encode(record),
                ),
            )

    def record_sandbox_transition(self, order_id: str, upstox_order_id: str | None,
                                  status: str, filled_quantity: float | None,
                                  average_price: float | None, ts: str, raw: dict) -> bool:
        """Idempotent state-transition append. Returns False if already recorded.

        3.0.3: the identity includes the CHILD (slice) order id — two children
        of one logical order with equal status/fill/timestamp are distinct
        transitions, never collapsed into one.
        """
        with self.db.transaction() as db:
            existing = db.execute(
                "SELECT 1 FROM sandbox_transitions WHERE order_id=? AND status=? "
                "AND filled_quantity IS ? AND ts=? AND upstox_order_id IS ?",
                (order_id, status, filled_quantity, ts, upstox_order_id),
            ).fetchone()
            if existing:
                return False
            db.execute(
                "INSERT INTO sandbox_transitions VALUES (?,?,?,?,?,?,?)",
                (order_id, upstox_order_id, status, filled_quantity, average_price, ts, encode(raw)),
            )
            if upstox_order_id:
                db.execute(
                    "UPDATE sandbox_orders SET upstox_order_id=?, status=?, updated_ts=? WHERE id=?",
                    (upstox_order_id, status, ts, order_id),
                )
            else:
                db.execute(
                    "UPDATE sandbox_orders SET status=?, updated_ts=? WHERE id=?",
                    (status, ts, order_id),
                )
        return True

    def list_sandbox_orders(self, limit=200):
        with self.db.transaction() as db:
            rows = db.execute(
                "SELECT * FROM sandbox_orders ORDER BY created_ts DESC LIMIT ?", (limit,)
            ).fetchall()
            orders = []
            for row in rows:
                # Columns are authoritative (payload may be stale after updates).
                order = json.loads(row["payload"])
                order.update(
                    id=row["id"],
                    upstox_order_id=row["upstox_order_id"],
                    symbol=row["symbol"],
                    side=row["side"],
                    quantity=row["quantity"],
                    price=row["price"],
                    order_type=row["order_type"],
                    status=row["status"],
                    tag=row["tag"],
                    created_ts=row["created_ts"],
                    updated_ts=row["updated_ts"],
                )
                transitions = [
                    dict(t) for t in db.execute(
                        "SELECT * FROM sandbox_transitions WHERE order_id=? ORDER BY ts",
                        (row["id"],),
                    ).fetchall()
                ]
                for transition in transitions:
                    transition["raw"] = json.loads(transition["raw"])
                order["transitions"] = transitions
                orders.append(order)
        return orders

    def open_sandbox_orders(self):
        return [
            order for order in self.list_sandbox_orders(limit=1000)
            if order["status"] in {"SUBMITTED", "OPEN", "PARTIALLY_FILLED"}
        ]

    def record_model_version(self, version: str, strategy: str, parameters: dict,
                             promoted: bool, validation: dict, created_ts: str) -> None:
        with self.db.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO model_versions VALUES (?,?,?,?,?,?)",
                (version, strategy, encode(parameters), int(promoted), encode(validation), created_ts),
            )

    def promoted_version(self, strategy: str) -> dict | None:
        with self.db.transaction() as db:
            row = db.execute(
                "SELECT version,payload FROM (SELECT version, parameters, promoted, validation, created_ts, "
                "parameters AS payload FROM model_versions WHERE strategy=? AND promoted=1 "
                "ORDER BY created_ts DESC) LIMIT 1",
                (strategy,),
            ).fetchone()
            if row:
                params = json.loads(db.execute(
                    "SELECT parameters FROM model_versions WHERE version=?", (row["version"],)
                ).fetchone()[0])
                return {"version": row["version"], "parameters": params}
        return None

    def list_model_versions(self, limit=50):
        with self.db.transaction() as db:
            return [
                {
                    "version": row["version"], "strategy": row["strategy"],
                    "parameters": json.loads(row["parameters"]),
                    "promoted": bool(row["promoted"]),
                    "validation": json.loads(row["validation"]),
                    "created_ts": row["created_ts"],
                }
                for row in db.execute(
                    "SELECT * FROM model_versions ORDER BY created_ts DESC LIMIT ?", (limit,)
                ).fetchall()
            ]

    def record_system_event(self, kind: str, payload: dict | None = None,
                            ts: str | None = None) -> str:
        event_id = uuid4().hex
        with self.db.transaction() as db:
            db.execute(
                "INSERT INTO system_events VALUES (?,?,?,?)",
                (event_id, ts or datetime.now(timezone.utc).isoformat(), kind,
                 encode(payload or {})),
            )
        return event_id

    def list_system_events(self, limit=50):
        with self.db.transaction() as db:
            return [
                dict(row, payload=json.loads(row["payload"]))
                for row in db.execute(
                    "SELECT * FROM system_events ORDER BY ts DESC LIMIT ?", (limit,)
                ).fetchall()
            ]

    def set_engine_state(self, key: str, value) -> None:
        with self.db.transaction() as db:
            db.execute("INSERT OR REPLACE INTO engine_state VALUES (?,?)", (key, encode(value)))

    def get_engine_state(self, key: str, default=None):
        with self.db.transaction() as db:
            row = db.execute("SELECT payload FROM engine_state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default


# ------------------------------------------------------------------ V2.1 ----

def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


class UniverseRepositoryMixin:
    """V2.1 market-universe persistence (additive mixin; no V1 changes)."""

    def upsert_instruments(self, records: list[dict], source: str) -> dict:
        """Insert/update instruments. Returns added/changed/unchanged counts.

        Identity = instrument_key. A row whose content hash changed is
        'changed'; identical rows are 'unchanged'; keys missing from this
        sync must be handled by mark_absent_instruments.
        """
        added = changed = unchanged = 0
        now = _now_iso()
        with self.db.transaction() as db:
            existing = {
                row["instrument_key"]: row["row_hash"]
                for row in db.execute(
                    "SELECT instrument_key, row_hash FROM instruments"
                ).fetchall()
            }
            for record in records:
                key = record["instrument_key"]
                row_hash = record["row_hash"]
                if key not in existing:
                    db.execute(
                        "INSERT OR REPLACE INTO instruments VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (key, record.get("trading_symbol"), record.get("name"),
                         record.get("exchange"), record.get("segment"),
                         record.get("instrument_type"), record.get("isin"),
                         record.get("lot_size"), record.get("tick_size"),
                         record.get("freeze_quantity"), record.get("expiry"),
                         record.get("strike_price"), record.get("option_type"),
                         record.get("underlying_key"), record.get("details", "{}"),
                         row_hash, record.get("state", "active"), source, now, now),
                    )
                    added += 1
                elif existing[key] != row_hash:
                    db.execute(
                        "UPDATE instruments SET trading_symbol=?, name=?, exchange=?, segment=?,"
                        " instrument_type=?, isin=?, lot_size=?, tick_size=?, freeze_quantity=?,"
                        " expiry=?, strike_price=?, option_type=?, underlying_key=?, details=?,"
                        " row_hash=?, state=?, source=?, last_seen_ts=? WHERE instrument_key=?",
                        (record.get("trading_symbol"), record.get("name"),
                         record.get("exchange"), record.get("segment"),
                         record.get("instrument_type"), record.get("isin"),
                         record.get("lot_size"), record.get("tick_size"),
                         record.get("freeze_quantity"), record.get("expiry"),
                         record.get("strike_price"), record.get("option_type"),
                         record.get("underlying_key"), record.get("details", "{}"),
                         row_hash, record.get("state", "active"), source, now, key),
                    )
                    changed += 1
                else:
                    db.execute(
                        "UPDATE instruments SET last_seen_ts=?, state=? WHERE instrument_key=?",
                        (now, record.get("state", "active"), key),
                    )
                    unchanged += 1
        return {"added": added, "changed": changed, "unchanged": unchanged}

    def mark_absent_instruments(self, keys: list[str], ts: str | None = None) -> int:
        """Instruments missing from the latest sync file → state 'removed'."""
        if not keys:
            return 0
        ts = ts or _now_iso()
        with self.db.transaction() as db:
            db.executemany(
                "UPDATE instruments SET state='removed', last_seen_ts=? WHERE instrument_key=?",
                [(ts, key) for key in keys],
            )
        return len(keys)

    def list_instruments(self, *, exchange: str | None = None, segment: str | None = None,
                         instrument_type: str | None = None, state: str | None = "active",
                         symbol_like: str | None = None, limit: int = 50) -> list[dict]:
        query = "SELECT * FROM instruments WHERE 1=1"
        params: list = []
        if exchange:
            query += " AND exchange=?"
            params.append(exchange)
        if segment:
            query += " AND segment=?"
            params.append(segment)
        if instrument_type:
            query += " AND instrument_type=?"
            params.append(instrument_type)
        if state:
            query += " AND state=?"
            params.append(state)
        if symbol_like:
            query += " AND trading_symbol LIKE ?"
            params.append(f"%{symbol_like}%")
        query += " ORDER BY instrument_key LIMIT ?"
        params.append(int(limit))
        with self.db.transaction() as db:
            rows = db.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def instrument_counts(self) -> dict:
        with self.db.transaction() as db:
            by_state = {
                f"state_{row[0]}": row[1]
                for row in db.execute(
                    "SELECT state, COUNT(*) FROM instruments GROUP BY state").fetchall()
            }
            by_exchange = {
                row[0]: row[1]
                for row in db.execute(
                    "SELECT exchange, COUNT(*) FROM instruments WHERE state='active'"
                    " GROUP BY exchange ORDER BY COUNT(*) DESC").fetchall()
            }
            segments = {
                row[0]: row[1]
                for row in db.execute(
                    "SELECT segment, COUNT(*) FROM instruments WHERE state='active'"
                    " GROUP BY segment ORDER BY COUNT(*) DESC LIMIT 20").fetchall()
            }
        return {"total": sum(by_state.values()), **by_state,
                "by_exchange": by_exchange, "by_segment": segments}

    def record_universe_sync(self, sync_id: str, source: str, file_hash: str | None,
                             totals: dict, status: str, detail: dict,
                             ts: str | None = None) -> None:
        with self.db.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO universe_syncs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (sync_id, ts or _now_iso(), source, file_hash,
                 totals.get("total"), totals.get("added"), totals.get("changed"),
                 totals.get("removed"), totals.get("unchanged"), status,
                 json.dumps(detail, sort_keys=True, default=str)),
            )

    def list_universe_syncs(self, limit: int = 10) -> list[dict]:
        with self.db.transaction() as db:
            rows = db.execute(
                "SELECT * FROM universe_syncs ORDER BY ts DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [dict(row) for row in rows]

    def all_instrument_keys(self) -> list[str]:
        with self.db.transaction() as db:
            rows = db.execute("SELECT instrument_key FROM instruments").fetchall()
        return [row[0] for row in rows]


class DataPlatformRepositoryMixin:
    """V2.1 download jobs, coverage, quality events, dataset versions."""

    def record_download_job(self, job: dict) -> None:
        with self.db.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO download_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job["id"], job.get("kind", "history"), job["instrument_key"],
                 job["unit"], int(job["interval"]), job["from_date"], job["to_date"],
                 job.get("status", "planned"), int(job.get("chunks_total", 0)),
                 int(job.get("chunks_done", 0)), int(job.get("records", 0)),
                 job.get("detail", "{}"), job.get("created_ts") or _now_iso(),
                 job.get("updated_ts") or _now_iso()),
            )

    def update_download_job(self, job_id: str, **fields) -> None:
        allowed = {"status", "chunks_total", "chunks_done", "records", "detail"}
        sets, params = [], []
        for name, value in fields.items():
            if name not in allowed:
                continue
            sets.append(f"{name}=?")
            params.append(int(value) if isinstance(value, int) else str(value))
        sets.append("updated_ts=?")
        params.append(_now_iso())
        params.append(job_id)
        with self.db.transaction() as db:
            db.execute(f"UPDATE download_jobs SET {', '.join(sets)} WHERE id=?", params)

    def list_download_jobs(self, *, instrument_key: str | None = None,
                           status: str | None = None, limit: int = 50) -> list[dict]:
        query = "SELECT * FROM download_jobs WHERE 1=1"
        params: list = []
        if instrument_key:
            query += " AND instrument_key=?"
            params.append(instrument_key)
        if status:
            query += " AND status=?"
            params.append(status)
        query += " ORDER BY created_ts DESC LIMIT ?"
        params.append(int(limit))
        with self.db.transaction() as db:
            rows = db.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def upsert_coverage(self, instrument_key: str, unit: str, interval: int,
                        first_ts: str | None, last_ts: str | None, records: int,
                        gaps: list, completeness: float) -> None:
        with self.db.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO coverage VALUES (?,?,?,?,?,?,?,?,?)",
                (instrument_key, unit, int(interval), first_ts, last_ts,
                 int(records), json.dumps(gaps, default=str), float(completeness),
                 _now_iso()),
            )

    def get_coverage(self, instrument_key: str, unit: str, interval: int) -> dict | None:
        with self.db.transaction() as db:
            row = db.execute(
                "SELECT * FROM coverage WHERE instrument_key=? AND unit=? AND interval=?",
                (instrument_key, unit, int(interval)),
            ).fetchone()
        return dict(row) if row else None

    def list_coverage(self, *, unit: str | None = None, interval: int | None = None,
                      limit: int = 100) -> list[dict]:
        query = "SELECT * FROM coverage WHERE 1=1"
        params: list = []
        if unit:
            query += " AND unit=?"
            params.append(unit)
        if interval is not None:
            query += " AND interval=?"
            params.append(int(interval))
        query += " ORDER BY instrument_key LIMIT ?"
        params.append(int(limit))
        with self.db.transaction() as db:
            rows = db.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def record_quality_event(self, classification: str, check_name: str, detail: dict,
                             *, instrument_key: str | None = None, unit: str | None = None,
                             interval: int | None = None, ts: str | None = None) -> str:
        event_id = uuid4().hex
        with self.db.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO quality_events VALUES (?,?,?,?,?,?,?,?)",
                (event_id, ts or _now_iso(), instrument_key, unit, interval,
                 classification, check_name,
                 json.dumps(detail, sort_keys=True, default=str)),
            )
        return event_id

    def list_quality_events(self, *, classification: str | None = None,
                            limit: int = 50) -> list[dict]:
        query = "SELECT * FROM quality_events WHERE 1=1"
        params: list = []
        if classification:
            query += " AND classification=?"
            params.append(classification)
        query += " ORDER BY ts DESC LIMIT ?"
        params.append(int(limit))
        with self.db.transaction() as db:
            rows = db.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def record_dataset_version(self, record: dict) -> str:
        dataset_id = record.get("id") or uuid4().hex
        with self.db.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO dataset_versions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (dataset_id, record["name"], int(record["version"]),
                 json.dumps(record["instrument_keys"]),
                 record["unit"], int(record["interval"]),
                 record.get("from_date"), record.get("to_date"),
                 json.dumps(record.get("parts", [])),
                 json.dumps(record.get("hashes", {})),
                 json.dumps(record.get("source_refs", {})),
                 record.get("created_ts") or _now_iso()),
            )
        return dataset_id

    def list_dataset_versions(self, *, name: str | None = None,
                              limit: int = 20) -> list[dict]:
        query = "SELECT * FROM dataset_versions WHERE 1=1"
        params: list = []
        if name:
            query += " AND name=?"
            params.append(name)
        query += " ORDER BY created_ts DESC LIMIT ?"
        params.append(int(limit))
        with self.db.transaction() as db:
            rows = db.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def get_dataset_version(self, dataset_id: str) -> dict | None:
        with self.db.transaction() as db:
            row = db.execute(
                "SELECT * FROM dataset_versions WHERE id=?", (dataset_id,)).fetchone()
        return dict(row) if row else None


class ResearchRepositoryMixin:
    """V2.1 experiments, candidates, shadow trades, learning events, snapshots."""

    def record_experiment(self, experiment_id: str, name: str, config: dict,
                          status: str = "running", ts: str | None = None) -> None:
        with self.db.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO experiments VALUES (?,?,?,?,?,?,?)",
                (experiment_id, name, status, json.dumps(config, sort_keys=True, default=str),
                 None, ts or _now_iso(), None),
            )

    def finish_experiment(self, experiment_id: str, result: dict,
                          status: str = "complete") -> None:
        with self.db.transaction() as db:
            db.execute(
                "UPDATE experiments SET status=?, result=?, finished_ts=? WHERE id=?",
                (status, json.dumps(result, sort_keys=True, default=str),
                 _now_iso(), experiment_id),
            )

    def list_experiments(self, limit: int = 20) -> list[dict]:
        with self.db.transaction() as db:
            rows = db.execute(
                "SELECT * FROM experiments ORDER BY created_ts DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [dict(row) for row in rows]

    def record_candidate(self, candidate_id: str, *, experiment_id: str | None,
                         strategy: str, parameters: dict, dataset_id: str | None,
                         metrics: dict, oos: dict, robustness: dict | None,
                         promoted: bool, reason: str) -> None:
        with self.db.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO candidates VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (candidate_id, experiment_id, strategy,
                 json.dumps(parameters, sort_keys=True), dataset_id,
                 json.dumps(metrics, sort_keys=True, default=str),
                 json.dumps(oos, sort_keys=True, default=str),
                 json.dumps(robustness or {}, sort_keys=True, default=str),
                 int(promoted), reason, _now_iso()),
            )

    def list_candidates(self, *, strategy: str | None = None,
                        promoted: bool | None = None, limit: int = 50) -> list[dict]:
        query = "SELECT * FROM candidates WHERE 1=1"
        params: list = []
        if strategy:
            query += " AND strategy=?"
            params.append(strategy)
        if promoted is not None:
            query += " AND promoted=?"
            params.append(int(promoted))
        query += " ORDER BY created_ts DESC LIMIT ?"
        params.append(int(limit))
        with self.db.transaction() as db:
            rows = db.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def record_shadow_trade(self, *, strategy: str, parameters: dict,
                            instrument_key: str, action: str, price: float | None,
                            regime: str | None, unit: str, interval: int,
                            result: dict, ts: str | None = None) -> str:
        shadow_id = uuid4().hex
        with self.db.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO shadow_trades VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (shadow_id, ts or _now_iso(), strategy,
                 json.dumps(parameters, sort_keys=True), instrument_key, action,
                 price, regime, unit, int(interval),
                 json.dumps(result, sort_keys=True, default=str)),
            )
        return shadow_id

    def list_shadow_trades(self, *, strategy: str | None = None,
                           limit: int = 100) -> list[dict]:
        query = "SELECT * FROM shadow_trades WHERE 1=1"
        params: list = []
        if strategy:
            query += " AND strategy=?"
            params.append(strategy)
        query += " ORDER BY ts DESC LIMIT ?"
        params.append(int(limit))
        with self.db.transaction() as db:
            rows = db.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def record_learning_event(self, kind: str, payload: dict,
                              ts: str | None = None) -> str:
        event_id = uuid4().hex
        with self.db.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO learning_events VALUES (?,?,?,?)",
                (event_id, ts or _now_iso(), kind,
                 json.dumps(payload, sort_keys=True, default=str)),
            )
        return event_id

    def list_learning_events(self, *, kind: str | None = None,
                             limit: int = 50) -> list[dict]:
        query = "SELECT * FROM learning_events WHERE 1=1"
        params: list = []
        if kind:
            query += " AND kind=?"
            params.append(kind)
        query += " ORDER BY ts DESC LIMIT ?"
        params.append(int(limit))
        with self.db.transaction() as db:
            rows = db.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def upsert_daily_snapshot(self, date: str, *, equity: float | None, realized: float,
                              unrealized: float, trades: int, regime: str | None) -> None:
        with self.db.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO daily_snapshots VALUES (?,?,?,?,?,?,?)",
                (date, equity, realized, unrealized, int(trades), regime,
                 json.dumps({"recorded_at": _now_iso()}, sort_keys=True)),
            )

    def list_daily_snapshots(self, limit: int = 400) -> list[dict]:
        with self.db.transaction() as db:
            rows = db.execute(
                "SELECT * FROM daily_snapshots ORDER BY date DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [dict(row) for row in rows]


class FinalRepositoryMixin:
    """FINAL (3.0.0): lineage, lifecycle, regime memory, opportunities,
    research questions, system status, reality gap, calibration. Additive."""

    # -- lineage -------------------------------------------------------------
    def record_lineage(self, lineage_id: str, strategy: str, parameters: dict,
                       generation: int, parent_id: str | None,
                       mutation: str | None = None,
                       origin: str = "root") -> None:
        with self.db.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO strategy_lineage VALUES (?,?,?,?,?,?,?,?)",
                (lineage_id, strategy, json.dumps(parameters, sort_keys=True),
                 int(generation), parent_id, mutation, origin, _now_iso()),
            )

    def get_lineage(self, lineage_id: str) -> dict | None:
        with self.db.transaction() as db:
            row = db.execute(
                "SELECT * FROM strategy_lineage WHERE id=?", (lineage_id,)
            ).fetchone()
        if row is None:
            return None
        record = dict(row)
        record["parameters"] = json.loads(record["parameters"])
        return record

    def list_lineage(self, strategy: str | None = None,
                     limit: int = 500) -> list[dict]:
        query = "SELECT * FROM strategy_lineage"
        params: list = []
        if strategy:
            query += " WHERE strategy=?"
            params.append(strategy)
        query += f" ORDER BY created_ts LIMIT {int(limit)}"
        with self.db.transaction() as db:
            rows = db.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    # -- lifecycle -----------------------------------------------------------
    def record_lifecycle(self, lineage_id: str, stage: str, status: str,
                         evidence: dict) -> str:
        entry_id = uuid4().hex
        with self.db.transaction() as db:
            db.execute(
                "INSERT INTO strategy_lifecycle VALUES (?,?,?,?,?,?)",
                (entry_id, lineage_id, stage, status,
                 json.dumps(evidence, sort_keys=True, default=str), _now_iso()),
            )
        return entry_id

    def lifecycle_history(self, lineage_id: str | None = None,
                          limit: int = 500) -> list[dict]:
        query = "SELECT * FROM strategy_lifecycle"
        params: list = []
        if lineage_id:
            query += " WHERE lineage_id=?"
            params.append(lineage_id)
        query += f" ORDER BY ts LIMIT {int(limit)}"
        with self.db.transaction() as db:
            rows = db.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def current_lifecycle(self, lineage_id: str) -> dict | None:
        history = self.lifecycle_history(lineage_id)
        return history[-1] if history else None

    # -- regime memory ---------------------------------------------------------
    def record_regime_observation(self, ts: str, instrument: str, regime: str,
                                  features: dict) -> None:
        with self.db.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO regime_history VALUES (?,?,?,?)",
                (ts, instrument, regime,
                 json.dumps(features, sort_keys=True, default=str)),
            )

    def list_regime_history(self, instrument: str = "MARKET",
                            limit: int = 2000) -> list[dict]:
        with self.db.transaction() as db:
            rows = db.execute(
                "SELECT * FROM regime_history WHERE instrument=? ORDER BY ts DESC LIMIT ?",
                (instrument, int(limit))).fetchall()
        return [dict(row) for row in rows]

    def list_regime_history_all(self, limit: int = 2000) -> list[dict]:
        """Recent regimes across ALL instruments (regime_history is per-instrument)."""
        with self.db.transaction() as db:
            rows = db.execute(
                "SELECT * FROM regime_history ORDER BY ts DESC LIMIT ?",
                (int(limit),)).fetchall()
        return [dict(row) for row in rows]

    # -- opportunity scans -----------------------------------------------------
    def record_opportunity(self, instrument_key: str, strategy: str,
                           action: str, score: float, detail: dict) -> str:
        scan_id = uuid4().hex
        with self.db.transaction() as db:
            db.execute(
                "INSERT INTO opportunity_scans VALUES (?,?,?,?,?,?,?)",
                (scan_id, _now_iso(), instrument_key, strategy, action,
                 float(score), json.dumps(detail, sort_keys=True, default=str)),
            )
        return scan_id

    def list_opportunities(self, limit: int = 50) -> list[dict]:
        with self.db.transaction() as db:
            rows = db.execute(
                "SELECT * FROM opportunity_scans ORDER BY ts DESC LIMIT ?",
                (int(limit),)).fetchall()
        return [dict(row, detail=json.loads(row["detail"])) for row in rows]

    # -- research questions ------------------------------------------------------
    def record_research_question(self, question: str, rationale: dict) -> str:
        question_id = uuid4().hex
        now = _now_iso()
        with self.db.transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO research_questions VALUES (?,?,?,?,?,?,?)",
                (question_id, question, json.dumps(rationale, sort_keys=True,
                                                   default=str),
                 "open", None, now, now),
            )
        return question_id

    def list_research_questions(self, status: str | None = None,
                                limit: int = 50) -> list[dict]:
        query = "SELECT * FROM research_questions WHERE 1=1"
        params: list = []
        if status:
            query += " AND status=?"
            params.append(status)
        query += " ORDER BY created_ts LIMIT ?"
        params.append(int(limit))
        with self.db.transaction() as db:
            rows = db.execute(query, params).fetchall()
        return [dict(row, rationale=json.loads(row["rationale"]))
                for row in rows]

    def update_research_question(self, question_id: str, *, status: str,
                                 experiment_id: str | None = None) -> None:
        with self.db.transaction() as db:
            db.execute(
                "UPDATE research_questions SET status=?, experiment_id=?, "
                "updated_ts=? WHERE id=?",
                (status, experiment_id, _now_iso(), question_id),
            )

    # -- system status -----------------------------------------------------------
    def record_system_status(self, payload: dict, ts: str | None = None) -> None:
        with self.db.transaction() as db:
            db.execute("INSERT OR REPLACE INTO system_status VALUES (?,?)",
                       (ts or _now_iso(),
                        json.dumps(payload, sort_keys=True, default=str)))

    def list_system_status(self, limit: int = 50) -> list[dict]:
        with self.db.transaction() as db:
            rows = db.execute(
                "SELECT * FROM system_status ORDER BY ts DESC LIMIT ?",
                (int(limit),)).fetchall()
        return [dict(row, payload=json.loads(row["payload"])) for row in rows]

    # -- reality gap ---------------------------------------------------------------
    def record_reality_gap(self, strategy: str, comparison: str, metrics: dict,
                           flagged: bool) -> str:
        gap_id = uuid4().hex
        with self.db.transaction() as db:
            db.execute(
                "INSERT INTO reality_gap VALUES (?,?,?,?,?,?)",
                (gap_id, _now_iso(), strategy, comparison,
                 json.dumps(metrics, sort_keys=True, default=str),
                 int(bool(flagged))),
            )
        return gap_id

    def list_reality_gaps(self, flagged: bool | None = None,
                          limit: int = 50) -> list[dict]:
        query = "SELECT * FROM reality_gap WHERE 1=1"
        params: list = []
        if flagged is not None:
            query += " AND flagged=?"
            params.append(int(flagged))
        query += " ORDER BY ts DESC LIMIT ?"
        params.append(int(limit))
        with self.db.transaction() as db:
            rows = db.execute(query, params).fetchall()
        return [dict(row, metrics=json.loads(row["metrics"]),
                     flagged=bool(row["flagged"])) for row in rows]

    # -- calibration -----------------------------------------------------------------
    def record_calibration(self, strategy: str, predicted: float,
                           realized_win: bool, detail: dict) -> str:
        entry_id = uuid4().hex
        with self.db.transaction() as db:
            db.execute(
                "INSERT INTO confidence_calibration VALUES (?,?,?,?,?,?)",
                (entry_id, _now_iso(), strategy, float(predicted),
                 int(bool(realized_win)),
                 json.dumps(detail, sort_keys=True, default=str)),
            )
        return entry_id

    def list_calibration(self, strategy: str | None = None,
                         limit: int = 200) -> list[dict]:
        query = "SELECT * FROM confidence_calibration"
        params: list = []
        if strategy:
            query += " WHERE strategy=?"
            params.append(strategy)
        query += " ORDER BY ts DESC LIMIT ?"
        params.append(int(limit))
        with self.db.transaction() as db:
            rows = db.execute(query, params).fetchall()
        return [dict(row, detail=json.loads(row["detail"])) for row in rows]


class MarketEventRepositoryMixin:
    """3.0.4 Market Event Listener — internet event intelligence, observational memory. Additive."""

    def record_market_event(self, record: dict) -> str:
        event_id = record.get("event_id") or uuid4().hex
        with self.db.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO market_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    event_id,
                    record.get("timestamp") or _now_iso(),
                    record.get("headline", "")[:500],
                    record.get("summary", "")[:2000],
                    record.get("event_type", "other"),
                    encode(record.get("affected_countries", [])),
                    encode(record.get("affected_markets", [])),
                    encode(record.get("affected_assets", [])),
                    encode(record.get("affected_sectors", [])),
                    float(record.get("confidence", 0.5)),
                    float(record.get("relevance", 0.5)),
                    record.get("expected_market_effect", "")[:2000],
                    record.get("arena_response", "IGNORE"),
                    record.get("action_taken", "NONE"),
                    record.get("strategy_used"),
                    encode(record.get("instruments_affected", [])),
                    encode(record.get("result_after_event")) if record.get("result_after_event") is not None else None,
                    encode(record.get("sources", [])),
                    encode(record.get("raw_payload", {})),
                    record.get("created_ts") or _now_iso(),
                    record.get("fingerprint", ""),
                    encode(record.get("transmission_paths", [])),
                    record.get("conflict_note"),
                ),
            )
        return event_id

    def get_market_event(self, event_id: str) -> dict | None:
        with self.db.transaction() as db:
            row = db.execute("SELECT * FROM market_events WHERE event_id=?", (event_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        # decode JSON fields
        for k in ("affected_countries", "affected_markets", "affected_assets", "affected_sectors", "instruments_affected", "sources", "raw_payload", "transmission_paths", "result_after_event"):
            val = d.get(k)
            if val is not None:
                try:
                    d[k] = json.loads(val) if isinstance(val, str) else val
                except Exception:
                    d[k] = val
        return d

    def list_market_events(self, *, event_type: str | None = None, limit: int = 50, offset: int = 0) -> list[dict]:
        query = "SELECT * FROM market_events WHERE 1=1"
        params: list = []
        if event_type:
            query += " AND event_type=?"
            params.append(event_type)
        query += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        params.extend([int(limit), int(offset)])
        with self.db.transaction() as db:
            rows = db.execute(query, params).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            for k in ("affected_countries", "affected_markets", "affected_assets", "affected_sectors", "instruments_affected", "sources", "raw_payload", "transmission_paths", "result_after_event"):
                val = d.get(k)
                if val is not None:
                    try:
                        d[k] = json.loads(val) if isinstance(val, str) else val
                    except Exception:
                        d[k] = val  # keep original on decode failure
            result.append(d)
        return result

    def update_market_event(self, event_id: str, **fields) -> None:
        allowed = {"confidence", "relevance", "expected_market_effect", "arena_response", "action_taken", "strategy_used", "result_after_event", "sources", "conflict_note", "fingerprint", "transmission_paths"}
        sets, params = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k in {"sources", "result_after_event", "transmission_paths"}:
                sets.append(f"{k}=?")
                params.append(encode(v) if v is not None else None)
            elif k == "confidence" or k == "relevance":
                sets.append(f"{k}=?")
                params.append(float(v))
            else:
                sets.append(f"{k}=?")
                params.append(v)
        if not sets:
            return
        params.append(event_id)
        with self.db.transaction() as db:
            db.execute(f"UPDATE market_events SET {', '.join(sets)} WHERE event_id=?", params)

    def add_market_event_source(self, event_id: str, source) -> None:
        # source can be dict or MarketEventSource
        if hasattr(source, "to_dict"):
            source = source.to_dict()
        elif isinstance(source, dict):
            pass
        else:
            source = {"source": str(source), "source_url": ""}
        with self.db.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO market_event_sources VALUES (?,?,?,?,?,?)",
                (
                    event_id,
                    source.get("source", ""),
                    source.get("source_url", ""),
                    source.get("fetched_ts") or _now_iso(),
                    source.get("headline", "")[:500],
                    encode(source.get("raw_data", {})),
                ),
            )

    def list_market_event_sources(self, event_id: str) -> list[dict]:
        with self.db.transaction() as db:
            rows = db.execute("SELECT * FROM market_event_sources WHERE event_id=? ORDER BY fetched_ts", (event_id,)).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            try:
                d["raw_data"] = json.loads(d["raw_data"]) if isinstance(d["raw_data"], str) else d["raw_data"]
            except Exception:
                d["raw_data"] = d.get("raw_data")  # keep original on failure
            result.append(d)
        return result

    def record_market_observation(self, event_id: str, observation: dict) -> str:
        obs_id = observation.get("id") or uuid4().hex
        with self.db.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO market_event_observations VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    obs_id,
                    event_id,
                    observation.get("window", "1d"),
                    observation.get("observed_ts") or _now_iso(),
                    observation.get("price_movement"),
                    observation.get("volatility"),
                    observation.get("volume"),
                    observation.get("affected_instrument"),
                    observation.get("arena_action"),
                    encode(observation.get("result", {})),
                ),
            )
        return obs_id

    def list_market_observations(self, event_id: str | None = None, *, limit: int = 50) -> list[dict]:
        query = "SELECT * FROM market_event_observations WHERE 1=1"
        params: list = []
        if event_id:
            query += " AND event_id=?"
            params.append(event_id)
        query += " ORDER BY observed_ts DESC LIMIT ?"
        params.append(int(limit))
        with self.db.transaction() as db:
            rows = db.execute(query, params).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            try:
                d["result"] = json.loads(d["result"]) if isinstance(d["result"], str) else d["result"]
            except Exception:
                d["result"] = d.get("result")  # keep original on failure
            result.append(d)
        return result

    def get_market_event_by_fingerprint(self, fingerprint: str) -> dict | None:
        with self.db.transaction() as db:
            row = db.execute("SELECT * FROM market_events WHERE fingerprint=? LIMIT 1", (fingerprint,)).fetchone()
        if not row:
            return None
        d = dict(row)
        for k in ("affected_countries", "affected_markets", "affected_assets", "affected_sectors", "instruments_affected", "sources", "raw_payload", "transmission_paths", "result_after_event"):
            val = d.get(k)
            if val is not None:
                try:
                    d[k] = json.loads(val) if isinstance(val, str) else val
                except Exception:
                    d[k] = val  # decode failed
        return d


class Repository(
    MarketEventRepositoryMixin,
    UniverseRepositoryMixin,
    DataPlatformRepositoryMixin,
    ResearchRepositoryMixin,
    _V1RepositoryBase, FinalRepositoryMixin):
    """V1 repository + V2.1 mixins (universe, data platform, research) + 3.0.4 market events."""
