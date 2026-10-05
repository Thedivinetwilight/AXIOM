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

    # -- order audit (sandbox vs LIVE are separate tables, one implementation) --
    #
    # V2 live-hardening: real-money orders are recorded in ``live_orders`` /
    # ``live_transitions``, never in the sandbox tables. Both modes share the
    # code below, so behaviour (idempotency, per-child identity, reconciliation
    # ordering) is identical while the audit trail stays unambiguous.

    def _record_order(self, table: str, record: dict) -> None:
        with self.db.transaction() as db:
            db.execute(
                f"INSERT OR REPLACE INTO {table} VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record["id"], record.get("upstox_order_id"), record["symbol"],
                    record["side"], record["quantity"], record.get("price"),
                    record.get("order_type", "MARKET"), record["status"],
                    record.get("tag"), record["created_ts"], record.get("updated_ts"),
                    encode(record),
                ),
            )

    def _record_transition(self, order_table: str, transition_table: str,
                           order_id: str, upstox_order_id: str | None,
                           status: str, filled_quantity: float | None,
                           average_price: float | None, ts: str, raw: dict) -> bool:
        """Idempotent state-transition append. Returns False if already recorded.

        3.0.3: the identity includes the CHILD (slice) order id — two children
        of one logical order with equal status/fill/timestamp are distinct
        transitions, never collapsed into one.
        """
        with self.db.transaction() as db:
            existing = db.execute(
                f"SELECT 1 FROM {transition_table} WHERE order_id=? AND status=? "
                "AND filled_quantity IS ? AND ts=? AND upstox_order_id IS ?",
                (order_id, status, filled_quantity, ts, upstox_order_id),
            ).fetchone()
            if existing:
                return False
            db.execute(
                f"INSERT INTO {transition_table} VALUES (?,?,?,?,?,?,?)",
                (order_id, upstox_order_id, status, filled_quantity, average_price, ts, encode(raw)),
            )
            if upstox_order_id:
                db.execute(
                    f"UPDATE {order_table} SET upstox_order_id=?, status=?, updated_ts=? WHERE id=?",
                    (upstox_order_id, status, ts, order_id),
                )
            else:
                db.execute(
                    f"UPDATE {order_table} SET status=?, updated_ts=? WHERE id=?",
                    (status, ts, order_id),
                )
        return True

    def _list_orders(self, order_table: str, transition_table: str, limit=200):
        with self.db.transaction() as db:
            rows = db.execute(
                f"SELECT * FROM {order_table} ORDER BY created_ts DESC LIMIT ?", (limit,)
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
                        f"SELECT * FROM {transition_table} WHERE order_id=? ORDER BY ts",
                        (row["id"],),
                    ).fetchall()
                ]
                for transition in transitions:
                    transition["raw"] = json.loads(transition["raw"])
                order["transitions"] = transitions
                orders.append(order)
        return orders

    def record_sandbox_order(self, record: dict) -> None:
        self._record_order("sandbox_orders", record)

    def record_sandbox_transition(self, order_id: str, upstox_order_id: str | None,
                                  status: str, filled_quantity: float | None,
                                  average_price: float | None, ts: str, raw: dict) -> bool:
        return self._record_transition(
            "sandbox_orders", "sandbox_transitions", order_id, upstox_order_id,
            status, filled_quantity, average_price, ts, raw)

    def list_sandbox_orders(self, limit=200):
        return self._list_orders("sandbox_orders", "sandbox_transitions", limit)

    def open_sandbox_orders(self):
        return [
            order for order in self.list_sandbox_orders(limit=1000)
            if order["status"] in {"SUBMITTED", "OPEN", "PARTIALLY_FILLED"}
        ]

    # -- LIVE (real money) audit: separate tables, identical semantics -------

    def record_live_order(self, record: dict) -> None:
        self._record_order("live_orders", record)

    def record_live_transition(self, order_id: str, upstox_order_id: str | None,
                               status: str, filled_quantity: float | None,
                               average_price: float | None, ts: str, raw: dict) -> bool:
        return self._record_transition(
            "live_orders", "live_transitions", order_id, upstox_order_id,
            status, filled_quantity, average_price, ts, raw)

    def list_live_orders(self, limit=200):
        return self._list_orders("live_orders", "live_transitions", limit)

    def open_live_orders(self):
        return [
            order for order in self.list_live_orders(limit=1000)
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

    # ------------------------------------------------- engine leases (§15) --
    # Atomic, conditional ownership. ONE statement decides the owner, so two
    # engines racing cannot both win. ``stale_before_ts`` is the heartbeat
    # cutoff: an existing owner whose heartbeat is older than the cutoff is
    # provably gone and may be replaced; a fresh owner never is.
    def try_acquire_lease(self, name: str, owner: str, *, pid: int, host: str,
                          live: bool, now_ts: str, stale_before_ts: str,
                          stale_proof: dict | None = None) -> dict:
        """Attempt to own lease ``name``. Returns {acquired, existing}.

        The owner decision remains ONE conditional upsert. A different LIVE
        owner can only be replaced when EngineLock supplies a proof tuple
        (owner, PID, host, heartbeat) that it checked as locally dead; the
        upsert compares every proof field again, closing the race between that
        OS check and the database write. A fresh, remote, malformed, or
        still-alive owner is never displaced by heartbeat age alone.
        """
        proof = stale_proof if isinstance(stale_proof, dict) else {}
        proof_owner = str(proof.get("owner") or "")
        proof_pid = int(proof.get("pid") or -1)
        proof_host = str(proof.get("host") or "")
        proof_heartbeat = str(proof.get("heartbeat_ts") or "")
        with self.db.transaction() as db:
            row = db.execute(
                "SELECT owner, pid, host, started_ts, heartbeat_ts, live "
                "FROM engine_lease WHERE name=?", (name,)).fetchone()
            existing = None
            if row:
                existing = {"owner": row[0], "pid": row[1], "host": row[2],
                            "started_ts": row[3], "heartbeat_ts": row[4],
                            "live": bool(row[5])}
            cur = db.execute(
                "INSERT INTO engine_lease (name, owner, pid, host, started_ts, "
                "heartbeat_ts, live) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET "
                "owner=excluded.owner, pid=excluded.pid, host=excluded.host, "
                "started_ts=CASE WHEN engine_lease.owner=excluded.owner "
                "THEN engine_lease.started_ts ELSE excluded.started_ts END, "
                "heartbeat_ts=excluded.heartbeat_ts, live=excluded.live "
                "WHERE (engine_lease.owner=excluded.owner "
                "AND engine_lease.pid=excluded.pid "
                "AND engine_lease.host=excluded.host) "
                "OR engine_lease.live=0 "
                "OR (engine_lease.owner=? AND engine_lease.pid=? "
                "AND engine_lease.host=? AND engine_lease.heartbeat_ts=? "
                "AND datetime(engine_lease.heartbeat_ts) IS NOT NULL "
                "AND datetime(engine_lease.heartbeat_ts) < datetime(?))",
                (name, owner, pid, host, now_ts, now_ts, int(bool(live)),
                 proof_owner, proof_pid, proof_host, proof_heartbeat,
                 stale_before_ts))
            acquired = cur.rowcount > 0
            renewed = bool(
                acquired and existing and existing["owner"] == owner
                and existing["pid"] == pid and existing["host"] == host)
            took_over_stale = bool(
                acquired and existing and existing["live"] and not renewed
                and proof_owner == existing["owner"]
                and proof_pid == existing["pid"]
                and proof_host == existing["host"]
                and proof_heartbeat == existing["heartbeat_ts"])
            return {"acquired": acquired, "existing": existing,
                    "renewed": renewed, "took_over_stale": took_over_stale}

    def heartbeat_lease(self, name: str, owner: str, *, now_ts: str) -> bool:
        """Refresh the heartbeat ONLY if we still own the lease."""
        with self.db.transaction() as db:
            cur = db.execute(
                "UPDATE engine_lease SET heartbeat_ts=? WHERE name=? AND owner=?",
                (now_ts, name, owner))
            return cur.rowcount > 0

    def release_lease(self, name: str, owner: str) -> bool:
        """Release ONLY if we own it; never delete someone else's lease."""
        with self.db.transaction() as db:
            cur = db.execute(
                "DELETE FROM engine_lease WHERE name=? AND owner=?",
                (name, owner))
            return cur.rowcount > 0

    def read_lease(self, name: str) -> dict | None:
        with self.db.transaction() as db:
            row = db.execute(
                "SELECT owner, pid, host, started_ts, heartbeat_ts, live "
                "FROM engine_lease WHERE name=?", (name,)).fetchone()
        if not row:
            return None
        return {"owner": row[0], "pid": row[1], "host": row[2],
                "started_ts": row[3], "heartbeat_ts": row[4],
                "live": bool(row[5])}


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


class AxiomWorkstationRepositoryMixin:
    """AXIOM-MONEY-PRINTER workstation persistence (master spec v1.0).

    Saved universes are market-scoped; state changes are the durable record
    behind the "what changed?" surface; opportunities carry their own
    lifecycle; research memory and missed opportunities keep provenance.
    Live session rows are append-only — the state machine's latest row is the
    current state, so the audit trail is never rewritten.
    """

    # ------------------------------------------------------------ universes --
    def save_universe(self, *, market_id: str, name: str, symbols: list[str],
                      note: str | None = None) -> dict:
        universe_id = str(uuid4())
        ts = _now_iso()
        payload = encode([str(s) for s in symbols])
        with self.db.transaction() as db:
            existing = db.execute(
                "SELECT id FROM saved_universes WHERE market_id=? AND name=?",
                (market_id, name)).fetchone()
            if existing:
                db.execute(
                    "UPDATE saved_universes SET symbols=?, note=?, updated_ts=? "
                    "WHERE id=?", (payload, note, ts, existing["id"]))
                universe_id = existing["id"]
            else:
                db.execute(
                    "INSERT INTO saved_universes (id, market_id, name, symbols, note,"
                    " created_ts, updated_ts) VALUES (?,?,?,?,?,?,?)",
                    (universe_id, market_id, name, payload, note, ts, ts))
        return {"id": universe_id, "market_id": market_id, "name": name,
                "symbols": [str(s) for s in symbols], "note": note,
                "updated_ts": ts}

    def list_universes(self, market_id: str | None = None) -> list[dict]:
        if market_id:
            with self.db.transaction() as db:
                rows = db.execute(
                    "SELECT * FROM saved_universes WHERE market_id=? "
                    "ORDER BY updated_ts DESC", (market_id,)).fetchall()
        else:
            with self.db.transaction() as db:
                rows = db.execute(
                    "SELECT * FROM saved_universes ORDER BY updated_ts DESC").fetchall()
        out = []
        for row in rows:
            d = dict(row)
            try:
                d["symbols"] = json.loads(d["symbols"])
            except (TypeError, ValueError):
                d["symbols"] = []
            out.append(d)
        return out

    def get_universe(self, universe_id: str) -> dict | None:
        with self.db.transaction() as db:
            row = db.execute("SELECT * FROM saved_universes WHERE id=?",
                             (universe_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["symbols"] = json.loads(d["symbols"])
        except (TypeError, ValueError):
            d["symbols"] = []
        return d

    def delete_universe(self, universe_id: str) -> bool:
        with self.db.transaction() as db:
            cur = db.execute("DELETE FROM saved_universes WHERE id=?", (universe_id,))
        return bool(getattr(cur, "rowcount", 0))

    def pin_instrument(self, market_id: str, identity: str) -> None:
        with self.db.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO universe_pins (market_id, identity, pinned_ts)"
                " VALUES (?,?,?)", (market_id, identity, _now_iso()))

    def unpin_instrument(self, market_id: str, identity: str) -> None:
        with self.db.transaction() as db:
            db.execute("DELETE FROM universe_pins WHERE market_id=? AND identity=?",
                       (market_id, identity))

    def list_pinned(self, market_id: str) -> list[dict]:
        with self.db.transaction() as db:
            rows = db.execute(
                "SELECT * FROM universe_pins WHERE market_id=? ORDER BY pinned_ts DESC",
                (market_id,)).fetchall()
        return [dict(r) for r in rows]

    # ----------------------------------------------------- what changed -----
    def record_state_change(self, *, domain: str, object_key: str, field: str,
                            previous, current, evidence: str | None = None) -> str:
        change_id = str(uuid4())
        with self.db.transaction() as db:
            db.execute(
                "INSERT INTO state_changes (id, ts, domain, object_key, field,"
                " previous, current, evidence) VALUES (?,?,?,?,?,?,?,?)",
                (change_id, _now_iso(), domain, object_key, field,
                 None if previous is None else str(previous),
                 None if current is None else str(current), evidence))
        return change_id

    def list_state_changes(self, domain: str | None = None, limit: int = 100) -> list[dict]:
        if domain:
            with self.db.transaction() as db:
                rows = db.execute(
                    "SELECT * FROM state_changes WHERE domain=? ORDER BY ts DESC LIMIT ?",
                    (domain, limit)).fetchall()
        else:
            with self.db.transaction() as db:
                rows = db.execute(
                    "SELECT * FROM state_changes ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------- opportunities -----
    def upsert_opportunity(self, record: dict) -> None:
        with self.db.transaction() as db:
            db.execute(
                "INSERT INTO opportunities (id, market_id, identity, state, created_ts,"
                " last_evidence_ts, updated_ts, payload, invalidation_reason)"
                " VALUES (?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET state=excluded.state,"
                " last_evidence_ts=excluded.last_evidence_ts,"
                " updated_ts=excluded.updated_ts, payload=excluded.payload,"
                " invalidation_reason=excluded.invalidation_reason",
                (record["id"], record["market_id"], record["identity"], record["state"],
                 record["created_ts"], record["last_evidence_ts"], record["updated_ts"],
                 encode(record.get("payload", {})), record.get("invalidation_reason")))

    def list_opportunity_records(self, *, state: str | None = None,
                           market_id: str | None = None, limit: int = 200) -> list[dict]:
        query = "SELECT * FROM opportunities"
        clauses, args = [], []
        if state:
            clauses.append("state=?")
            args.append(state)
        if market_id:
            clauses.append("market_id=?")
            args.append(market_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_ts DESC LIMIT ?"
        args.append(limit)
        with self.db.transaction() as db:
            rows = db.execute(query, tuple(args)).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            try:
                d["payload"] = json.loads(d["payload"])
            except (TypeError, ValueError):
                d["payload"] = {}
            out.append(d)
        return out

    # --------------------------------------------------- research memory ----
    def record_research_memory(self, *, topic: str, kind: str, payload: dict,
                               provenance: str, confidence: float,
                               market_id: str | None = None,
                               experiment_id: str | None = None) -> str:
        memory_id = str(uuid4())
        with self.db.transaction() as db:
            db.execute(
                "INSERT INTO research_memory (id, ts, market_id, topic, kind, payload,"
                " provenance, confidence, experiment_id) VALUES (?,?,?,?,?,?,?,?,?)",
                (memory_id, _now_iso(), market_id, topic, kind, encode(payload),
                 provenance, float(confidence), experiment_id))
        return memory_id

    def search_research_memory(self, *, query: str = "", market_id: str | None = None,
                               kind: str | None = None, limit: int = 50) -> list[dict]:
        clauses, args = [], []
        if market_id:
            clauses.append("market_id=?")
            args.append(market_id)
        if kind:
            clauses.append("kind=?")
            args.append(kind)
        if query:
            clauses.append("(topic LIKE ? OR payload LIKE ?)")
            like = f"%{query}%"
            args.extend([like, like])
        sql = "SELECT * FROM research_memory"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        with self.db.transaction() as db:
            rows = db.execute(sql, tuple(args)).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            try:
                d["payload"] = json.loads(d["payload"])
            except (TypeError, ValueError):
                d["payload"] = {}
            out.append(d)
        return out

    def record_missed_opportunity(self, *, kind: str, payload: dict, knowable: bool,
                                  market_id: str | None = None, identity: str | None = None,
                                  counterfactual: str | None = None,
                                  research_question: str | None = None) -> str:
        missed_id = str(uuid4())
        with self.db.transaction() as db:
            db.execute(
                "INSERT INTO missed_opportunities (id, ts, kind, market_id, identity,"
                " payload, knowable, counterfactual, research_question)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (missed_id, _now_iso(), kind, market_id, identity, encode(payload),
                 1 if knowable else 0, counterfactual, research_question))
        return missed_id

    def list_missed_opportunities(self, limit: int = 100) -> list[dict]:
        with self.db.transaction() as db:
            rows = db.execute(
                "SELECT * FROM missed_opportunities ORDER BY ts DESC LIMIT ?",
                (limit,)).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            try:
                d["payload"] = json.loads(d["payload"])
            except (TypeError, ValueError):
                d["payload"] = {}
            d["knowable"] = bool(d.get("knowable"))
            out.append(d)
        return out

    # ------------------------------------------------------- live session ---
    def record_live_session_transition(self, *, state: str, reason: str,
                                       payload: dict | None = None) -> str:
        session_id = str(uuid4())
        with self.db.transaction() as db:
            db.execute(
                "INSERT INTO live_sessions (id, state, ts_entered, reason, payload)"
                " VALUES (?,?,?,?,?)",
                (session_id, state, _now_iso(), reason, encode(payload or {})))
        return session_id

    def current_live_session(self) -> dict | None:
        with self.db.transaction() as db:
            row = db.execute(
                "SELECT * FROM live_sessions ORDER BY ts_entered DESC, rowid DESC"
                " LIMIT 1").fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["payload"] = json.loads(d["payload"])
        except (TypeError, ValueError):
            d["payload"] = {}
        return d

    def list_live_sessions(self, limit: int = 50) -> list[dict]:
        with self.db.transaction() as db:
            rows = db.execute(
                "SELECT * FROM live_sessions ORDER BY ts_entered DESC, rowid DESC LIMIT ?",
                (limit,)).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            try:
                d["payload"] = json.loads(d["payload"])
            except (TypeError, ValueError):
                d["payload"] = {}
            out.append(d)
        return out

    # ----------------------------------------------------- latency/tasks ----
    def upsert_latency_summary(self, *, stage: str, day: str, count: int, p50_ms: float,
                               p95_ms: float, p99_ms: float, p999_ms: float,
                               max_ms: float, deadline_misses: int) -> None:
        with self.db.transaction() as db:
            db.execute(
                "INSERT INTO latency_summaries (stage, day, count, p50_ms, p95_ms,"
                " p99_ms, p999_ms, max_ms, deadline_misses) VALUES (?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(stage, day) DO UPDATE SET count=excluded.count,"
                " p50_ms=excluded.p50_ms, p95_ms=excluded.p95_ms, p99_ms=excluded.p99_ms,"
                " p999_ms=excluded.p999_ms, max_ms=excluded.max_ms,"
                " deadline_misses=excluded.deadline_misses",
                (stage, day, count, p50_ms, p95_ms, p99_ms, p999_ms, max_ms,
                 deadline_misses))

    def list_latency_summaries(self, stage: str | None = None, limit: int = 200) -> list[dict]:
        if stage:
            with self.db.transaction() as db:
                rows = db.execute(
                    "SELECT * FROM latency_summaries WHERE stage=? ORDER BY day DESC LIMIT ?",
                    (stage, limit)).fetchall()
        else:
            with self.db.transaction() as db:
                rows = db.execute(
                    "SELECT * FROM latency_summaries ORDER BY day DESC LIMIT ?",
                    (limit,)).fetchall()
        return [dict(r) for r in rows]

    def upsert_task(self, *, task_id: str, name: str, type_: str, state: str,
                    progress: float, payload: dict | None = None) -> None:
        ts = _now_iso()
        with self.db.transaction() as db:
            db.execute(
                "INSERT INTO background_tasks (id, name, type, state, progress,"
                " started_ts, updated_ts, payload) VALUES (?,?,?,?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET state=excluded.state,"
                " progress=excluded.progress, updated_ts=excluded.updated_ts,"
                " payload=excluded.payload",
                (task_id, name, type_, state, float(progress), ts, ts,
                 encode(payload or {})))

    def list_tasks(self, limit: int = 100) -> list[dict]:
        with self.db.transaction() as db:
            rows = db.execute(
                "SELECT * FROM background_tasks ORDER BY updated_ts DESC LIMIT ?",
                (limit,)).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            try:
                d["payload"] = json.loads(d["payload"])
            except (TypeError, ValueError):
                d["payload"] = {}
            out.append(d)
        return out

    # ---------------------------------------------- broker connection meta --
    def upsert_broker_connection(self, *, broker: str, status: str, token_state: str,
                                 account_hint: str | None = None,
                                 note: str | None = None) -> None:
        """Connection METADATA only. Secrets never enter this table."""
        with self.db.transaction() as db:
            db.execute(
                "INSERT INTO broker_connections (broker, status, token_state,"
                " account_hint, updated_ts, note) VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(broker) DO UPDATE SET status=excluded.status,"
                " token_state=excluded.token_state, account_hint=excluded.account_hint,"
                " updated_ts=excluded.updated_ts, note=excluded.note",
                (broker, status, token_state, account_hint, _now_iso(), note))

    def get_broker_connection(self, broker: str) -> dict | None:
        with self.db.transaction() as db:
            row = db.execute("SELECT * FROM broker_connections WHERE broker=?",
                             (broker,)).fetchone()
        return dict(row) if row else None

    def list_broker_connections(self) -> list[dict]:
        with self.db.transaction() as db:
            rows = db.execute(
                "SELECT * FROM broker_connections ORDER BY updated_ts DESC").fetchall()
        return [dict(r) for r in rows]


class Repository(
    MarketEventRepositoryMixin,
    UniverseRepositoryMixin,
    DataPlatformRepositoryMixin,
    ResearchRepositoryMixin,
    AxiomWorkstationRepositoryMixin,
    _V1RepositoryBase, FinalRepositoryMixin):
    """V1 repository + V2.1 mixins (universe, data platform, research) + 3.0.4 market events."""
