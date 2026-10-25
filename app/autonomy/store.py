"""Durable, redacted autonomy journal with a tamper-evident audit hash chain.

Broker tokens and execution permits never enter this store.  Intent records are
written before any broker mutation and uncertain submissions remain unresolved
forever until reconciled; they are never deleted to enable a retry.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any, Iterator

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    trading_day TEXT NOT NULL,
    mode TEXT NOT NULL,
    state TEXT NOT NULL,
    risk_json TEXT,
    auth_fingerprint TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    interruption_reason TEXT
);
CREATE TABLE IF NOT EXISTS intents (
    intent_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    signal_id TEXT NOT NULL,
    instrument_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL,
    broker_ids_json TEXT NOT NULL DEFAULT '[]',
    forecast_audit_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(session_id, signal_id),
    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
);
CREATE TABLE IF NOT EXISTS audit (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    session_id TEXT,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_session_seq ON audit(session_id, seq);
CREATE INDEX IF NOT EXISTS intents_state ON intents(state);
"""

SENSITIVE = ("token", "secret", "password", "authorization", "credential", "access_key")
UNRESOLVED = {"RECORDED", "SUBMISSION_ATTEMPTED", "ACKNOWLEDGED_UNRECONCILED",
              "UNRESOLVED", "MODIFICATION_ATTEMPTED", "CANCEL_ATTEMPTED"}


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def redact(value: Any, key: str = "") -> Any:
    if any(part in key.lower() for part in SENSITIVE):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v, key) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


class AutonomyStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)
        self.recover_interrupted_sessions()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
            except Exception:
                self.db.execute("ROLLBACK")
                raise
            else:
                self.db.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self.db.close()

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def create_session(self, session_id: str, trading_day: str, mode: str) -> None:
        now = self.now()
        with self.transaction() as db:
            db.execute(
                "INSERT INTO sessions(session_id,trading_day,mode,state,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (session_id, trading_day, mode, "UNCONFIGURED", now, now),
            )
        self.audit("SESSION_CREATED", {"mode": mode, "trading_day": trading_day}, session_id)

    def update_session(self, session_id: str, *, state: str | None = None,
                       risk: dict | None = None, auth_fingerprint: str | None = None,
                       interruption_reason: str | None = None) -> None:
        current = self.session(session_id)
        if current is None:
            raise KeyError("Unknown autonomy session")
        now = self.now()
        with self.transaction() as db:
            db.execute(
                "UPDATE sessions SET state=?,risk_json=?,auth_fingerprint=?,updated_at=?,"
                "interruption_reason=? WHERE session_id=?",
                (
                    state or current["state"],
                    _json(redact(risk)) if risk is not None else current["risk_json"],
                    auth_fingerprint if auth_fingerprint is not None else current["auth_fingerprint"],
                    now,
                    interruption_reason,
                    session_id,
                ),
            )

    def session(self, session_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self.db.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()

    def recover_interrupted_sessions(self) -> None:
        """Durable recovery marker; never restores ARMED/ACTIVE execution state."""
        now = self.now()
        with self.transaction() as db:
            rows = db.execute(
                "SELECT session_id,state FROM sessions WHERE state IN "
                "('ARMED','ACTIVE','PAUSED')"
            ).fetchall()
            for row in rows:
                db.execute(
                    "UPDATE sessions SET state='HALTED',interruption_reason=?,updated_at=? "
                    "WHERE session_id=?",
                    ("RECOVERY: fresh session, authorization, risk and approval required", now,
                     row["session_id"]),
                )
        # This runs before the object is fully returned, but audit is safe now.
        for row in rows:
            self.audit("RECOVERY_HALT", {"prior_state": row["state"]}, row["session_id"])

    def record_intent(self, intent_id: str, session_id: str, signal_id: str,
                      instrument_key: str, payload: dict, forecast_audit_id: int | None) -> bool:
        now = self.now()
        with self.transaction() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO intents(intent_id,session_id,signal_id,instrument_key,"
                "payload_json,state,forecast_audit_id,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'RECORDED',?,?,?)",
                (intent_id, session_id, signal_id, instrument_key, _json(redact(payload)),
                 forecast_audit_id, now, now),
            )
            inserted = cursor.rowcount == 1
        if inserted:
            self.audit("INTENT_RECORDED", {"intent_id": intent_id, "signal_id": signal_id,
                                           "instrument_key": instrument_key}, session_id)
        return inserted

    def transition_intent(self, intent_id: str, state: str, *,
                          broker_ids: list[str] | tuple[str, ...] | None = None,
                          event_payload: dict | None = None) -> None:
        row = self.intent(intent_id)
        if row is None:
            raise KeyError("Unknown intent")
        ids = list(broker_ids) if broker_ids is not None else json.loads(row["broker_ids_json"])
        with self.transaction() as db:
            db.execute(
                "UPDATE intents SET state=?,broker_ids_json=?,updated_at=? WHERE intent_id=?",
                (state, _json(ids), self.now(), intent_id),
            )
        payload = {"intent_id": intent_id, "state": state, "broker_ids": ids}
        if event_payload:
            payload.update(redact(event_payload))
        self.audit("INTENT_TRANSITION", payload, row["session_id"])

    def intent(self, intent_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self.db.execute("SELECT * FROM intents WHERE intent_id=?", (intent_id,)).fetchone()

    def intent_by_signal(self, session_id: str, signal_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self.db.execute(
                "SELECT * FROM intents WHERE session_id=? AND signal_id=?", (session_id, signal_id)
            ).fetchone()

    def intents(self, session_id: str) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.db.execute(
                "SELECT * FROM intents WHERE session_id=? ORDER BY created_at", (session_id,)
            ).fetchall())

    def update_intent_details(self, intent_id: str, details: dict) -> None:
        """Merge non-secret reconciliation metadata without changing intent identity."""
        row = self.intent(intent_id)
        if row is None:
            raise KeyError("Unknown intent")
        payload = json.loads(row["payload_json"])
        payload.update(redact(details))
        with self.transaction() as db:
            db.execute("UPDATE intents SET payload_json=?,updated_at=? WHERE intent_id=?",
                       (_json(payload), self.now(), intent_id))

    def unresolved(self, session_id: str | None = None) -> list[sqlite3.Row]:
        placeholders = ",".join("?" for _ in UNRESOLVED)
        params: list[Any] = sorted(UNRESOLVED)
        sql = f"SELECT * FROM intents WHERE state IN ({placeholders})"
        if session_id:
            sql += " AND session_id=?"
            params.append(session_id)
        with self._lock:
            return list(self.db.execute(sql, params).fetchall())

    def audit(self, event_type: str, payload: dict | None = None,
              session_id: str | None = None) -> int:
        safe = redact(payload or {})
        ts = self.now()
        with self.transaction() as db:
            previous = db.execute("SELECT event_hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
            prev_hash = previous["event_hash"] if previous else "0" * 64
            body = {"ts": ts, "session_id": session_id, "event_type": event_type,
                    "payload": safe, "prev_hash": prev_hash}
            digest = hashlib.sha256(_json(body).encode()).hexdigest()
            cursor = db.execute(
                "INSERT INTO audit(ts,session_id,event_type,payload_json,prev_hash,event_hash) "
                "VALUES(?,?,?,?,?,?)",
                (ts, session_id, event_type, _json(safe), prev_hash, digest),
            )
            return int(cursor.lastrowid)

    def events(self, *, session_id: str | None = None, limit: int = 200) -> list[dict]:
        limit = max(1, min(int(limit), 2000))
        if session_id:
            rows = self.db.execute(
                "SELECT * FROM audit WHERE session_id=? ORDER BY seq DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        else:
            rows = self.db.execute("SELECT * FROM audit ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
        return [{
            "seq": row["seq"], "ts": row["ts"], "session_id": row["session_id"],
            "event_type": row["event_type"], "payload": json.loads(row["payload_json"]),
            "prev_hash": row["prev_hash"], "event_hash": row["event_hash"],
        } for row in rows]

    def verify_chain(self) -> bool:
        with self._lock:
            rows = self.db.execute("SELECT * FROM audit ORDER BY seq").fetchall()
        expected_previous = "0" * 64
        for row in rows:
            if row["prev_hash"] != expected_previous:
                return False
            body = {"ts": row["ts"], "session_id": row["session_id"],
                    "event_type": row["event_type"], "payload": json.loads(row["payload_json"]),
                    "prev_hash": row["prev_hash"]}
            if hashlib.sha256(_json(body).encode()).hexdigest() != row["event_hash"]:
                return False
            expected_previous = row["event_hash"]
        return True
