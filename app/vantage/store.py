"""Additive, redacted SQLite persistence for Vantage runtime and order intents."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from uuid import uuid4

from app.vantage.models import FINAL_ORDER_STATES, UNRESOLVED_ORDER_STATES
from app.vantage.secrets import redact


SCHEMA = """
CREATE TABLE IF NOT EXISTS vantage_credentials (
 environment TEXT PRIMARY KEY,
 login_masked TEXT NOT NULL,
 server TEXT NOT NULL,
 terminal_path TEXT NOT NULL DEFAULT '',
 label TEXT NOT NULL DEFAULT '',
 updated_ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vantage_intents (
 id TEXT PRIMARY KEY,
 environment TEXT NOT NULL,
 symbol TEXT NOT NULL,
 side TEXT NOT NULL,
 order_type TEXT NOT NULL,
 volume REAL NOT NULL,
 limit_price REAL,
 stop_loss REAL,
 take_profit REAL,
 comment TEXT NOT NULL,
 status TEXT NOT NULL,
 broker_order TEXT,
 broker_deal TEXT,
 reason TEXT,
 request_json TEXT NOT NULL,
 result_json TEXT NOT NULL,
 created_ts TEXT NOT NULL,
 updated_ts TEXT NOT NULL,
 confirmed_ts TEXT
);
CREATE INDEX IF NOT EXISTS idx_vantage_intents_env_time
 ON vantage_intents(environment, created_ts);
CREATE INDEX IF NOT EXISTS idx_vantage_intents_broker_order
 ON vantage_intents(environment, broker_order);
CREATE TABLE IF NOT EXISTS vantage_deals (
 deal_id TEXT PRIMARY KEY,
 environment TEXT NOT NULL,
 order_id TEXT,
 position_id TEXT,
 symbol TEXT,
 side TEXT,
 volume REAL,
 price REAL,
 profit REAL,
 commission REAL,
 swap REAL,
 fee REAL,
 timestamp TEXT,
 payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vantage_deals_env_time
 ON vantage_deals(environment, timestamp);
CREATE TABLE IF NOT EXISTS vantage_runtime (
 key TEXT PRIMARY KEY,
 payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vantage_events (
 id TEXT PRIMARY KEY,
 ts TEXT NOT NULL,
 environment TEXT NOT NULL,
 kind TEXT NOT NULL,
 payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vantage_events_time ON vantage_events(ts);
"""


def encode(value) -> str:
    return json.dumps(redact(value), allow_nan=False, separators=(",", ":"))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class VantageStore:
    def __init__(self, database):
        self.db = database
        with self.db.transaction() as conn:
            conn.executescript(SCHEMA)

    def ping(self) -> bool:
        """Prove both read and durable write access without touching order state."""
        try:
            with self.db.transaction() as conn:
                conn.execute("SELECT 1").fetchone()
                conn.execute(
                    "INSERT OR REPLACE INTO vantage_runtime VALUES ('audit_write_probe', ?)",
                    (encode({"available": True}),),
                )
            return True
        except Exception:
            return False

    def set_runtime(self, key: str, value) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO vantage_runtime VALUES (?,?)", (key, encode(value))
            )

    def get_runtime(self, key: str, default=None):
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT payload FROM vantage_runtime WHERE key=?", (key,)
            ).fetchone()
        return json.loads(row["payload"]) if row else default

    def save_credential_metadata(
        self, environment: str, *, login_masked: str, server: str,
        terminal_path: str = "", label: str = "",
    ) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO vantage_credentials VALUES (?,?,?,?,?,?)",
                (environment, login_masked, server, terminal_path, label, now_iso()),
            )

    def credential_metadata(self, environment: str) -> dict | None:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM vantage_credentials WHERE environment=?", (environment,)
            ).fetchone()
        return dict(row) if row else None

    def delete_credential_metadata(self, environment: str) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM vantage_credentials WHERE environment=?", (environment,))

    def create_intent(self, intent: dict) -> bool:
        """Insert exactly once. Returns False for an existing idempotency key."""
        columns = (
            "id", "environment", "symbol", "side", "order_type", "volume",
            "limit_price", "stop_loss", "take_profit", "comment", "status",
            "broker_order", "broker_deal", "reason", "request_json", "result_json",
            "created_ts", "updated_ts", "confirmed_ts",
        )
        row = {name: intent.get(name) for name in columns}
        row["request_json"] = encode(intent.get("request", {}))
        row["result_json"] = encode(intent.get("result", {}))
        with self.db.transaction() as conn:
            cursor = conn.execute(
                f"INSERT OR IGNORE INTO vantage_intents ({','.join(columns)}) "
                f"VALUES ({','.join('?' for _ in columns)})",
                tuple(row[name] for name in columns),
            )
        return cursor.rowcount == 1

    def update_intent(self, intent_id: str, **changes) -> None:
        allowed = {
            "status", "broker_order", "broker_deal", "reason", "result",
            "confirmed_ts", "volume",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"Unsupported intent fields: {sorted(unknown)}")
        assignments, values = [], []
        for key, value in changes.items():
            column = "result_json" if key == "result" else key
            assignments.append(f"{column}=?")
            values.append(encode(value) if key == "result" else value)
        assignments.append("updated_ts=?")
        values.append(now_iso())
        values.append(intent_id)
        with self.db.transaction() as conn:
            conn.execute(
                f"UPDATE vantage_intents SET {','.join(assignments)} WHERE id=?", values
            )

    @staticmethod
    def _intent(row) -> dict:
        item = dict(row)
        item["request"] = json.loads(item.pop("request_json"))
        item["result"] = json.loads(item.pop("result_json"))
        return item

    def get_intent(self, intent_id: str) -> dict | None:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM vantage_intents WHERE id=?", (intent_id,)
            ).fetchone()
        return self._intent(row) if row else None

    def list_intents(self, environment: str | None = None, limit: int = 500) -> list[dict]:
        query = "SELECT * FROM vantage_intents"
        params: list = []
        if environment:
            query += " WHERE environment=?"
            params.append(environment)
        query += " ORDER BY created_ts DESC LIMIT ?"
        params.append(limit)
        with self.db.transaction() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._intent(row) for row in rows]

    def unresolved(self, environment: str | None = None) -> list[dict]:
        return [
            item for item in self.list_intents(environment, 2000)
            if item["status"] in UNRESOLVED_ORDER_STATES
        ]

    def trades_today(self, environment: str, day: str) -> int:
        # Count an intent once, when it was actually attempted. Reconnects and
        # reconciliation never increment this number.
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM vantage_intents WHERE environment=? "
                "AND substr(created_ts,1,10)=? AND status NOT IN ('INTENT_RECORDED')",
                (environment, day),
            ).fetchone()
        return int(row["n"])

    def record_event(self, environment: str, kind: str, payload: dict | None = None) -> str:
        event_id = uuid4().hex
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO vantage_events VALUES (?,?,?,?,?)",
                (event_id, now_iso(), environment, kind, encode(payload or {})),
            )
        return event_id

    def list_events(self, environment: str | None = None, limit: int = 200) -> list[dict]:
        query = "SELECT * FROM vantage_events"
        params: list = []
        if environment:
            query += " WHERE environment=?"
            params.append(environment)
        query += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        with self.db.transaction() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row, payload=json.loads(row["payload"])) for row in rows]

    def upsert_deals(self, environment: str, deals: list[dict]) -> None:
        with self.db.transaction() as conn:
            for deal in deals:
                deal_id = str(deal.get("deal_id") or deal.get("ticket") or "")
                if not deal_id:
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO vantage_deals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        deal_id, environment, str(deal.get("order_id") or "") or None,
                        str(deal.get("position_id") or "") or None, deal.get("symbol"),
                        deal.get("side"), deal.get("volume"), deal.get("price"),
                        deal.get("profit"), deal.get("commission"), deal.get("swap"),
                        deal.get("fee"), deal.get("timestamp"), encode(deal),
                    ),
                )

    def list_deals(self, environment: str, limit: int = 500) -> list[dict]:
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM vantage_deals WHERE environment=? "
                "ORDER BY timestamp DESC LIMIT ?", (environment, limit),
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    @staticmethod
    def is_final(status: str) -> bool:
        return status in FINAL_ORDER_STATES
