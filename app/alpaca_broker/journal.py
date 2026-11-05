"""Durable SQLite intent-before-write journal for Alpaca broker execution.

Persists order intents BEFORE any HTTP submission is attempted, records terminal
rejections, and latches ``UNRESOLVED_INTENT`` on transport/5xx ambiguity so a
process crash or network timeout never permits duplicate or unbounded entries
before explicit broker reconciliation.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AlpacaBrokerJournal:
    """Thread-safe WAL SQLite journal for Alpaca order intents and fills."""

    def __init__(self, db_path: Path | str) -> None:
        self._in_memory = str(db_path) == ":memory:"
        self.db_path = Path(":memory:") if self._in_memory else Path(db_path)
        self._lock = threading.RLock()
        self._mem_conn: sqlite3.Connection | None = None
        if self._in_memory:
            self._mem_conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._mem_conn.row_factory = sqlite3.Row
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        if self._mem_conn is not None:
            return self._mem_conn
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS alpaca_broker_orders (
                    client_order_id TEXT PRIMARY KEY,
                    environment TEXT NOT NULL,
                    state TEXT NOT NULL,
                    broker_order_id TEXT,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    order_date TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    unresolved_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS alpaca_broker_fills (
                    fill_key TEXT PRIMARY KEY,
                    environment TEXT NOT NULL,
                    client_order_id TEXT NOT NULL,
                    fill_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def record_intent(
        self,
        *,
        environment: str,
        client_order_id: str,
        payload: dict[str, Any],
        order_date: str,
    ) -> None:
        now = _utc_now()
        intent_record = {
            "client_order_id": client_order_id,
            "broker_order_id": None,
            "symbol": payload.get("symbol"),
            "side": payload.get("side"),
            "qty": payload.get("qty"),
            "notional": payload.get("notional"),
            "type": payload.get("type"),
            "time_in_force": payload.get("time_in_force"),
            "limit_price": payload.get("limit_price"),
            "stop_price": payload.get("stop_price"),
            "status": "intent_pending",
            "filled_qty": "0",
            "filled_avg_price": None,
            "submitted_at": now,
            "updated_at": now,
            "source": "intent",
            "environment": environment,
        }
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO alpaca_broker_orders
                (client_order_id, environment, state, broker_order_id, symbol, side,
                 order_date, record_json, unresolved_reason, created_at, updated_at)
                VALUES (?, ?, 'INTENT_PENDING', NULL, ?, ?, ?, ?, 'in_flight_intent', ?, ?)
                """,
                (
                    client_order_id,
                    environment,
                    str(payload.get("symbol") or ""),
                    str(payload.get("side") or ""),
                    order_date,
                    json.dumps(intent_record, sort_keys=True),
                    now,
                    now,
                ),
            )

    def mark_unresolved(self, client_order_id: str, reason: str) -> None:
        now = _utc_now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE alpaca_broker_orders
                SET state = 'UNRESOLVED_INTENT', unresolved_reason = ?, updated_at = ?
                WHERE client_order_id = ?
                """,
                (reason[:250], now, client_order_id),
            )

    def remove_rejected_intent(self, client_order_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM alpaca_broker_orders WHERE client_order_id = ? AND state = 'INTENT_PENDING'",
                (client_order_id,),
            )

    def save_order(self, environment: str, client_order_id: str, record: dict[str, Any], *, order_date: str | None = None) -> None:
        now = _utc_now()
        day = order_date or now[:10]
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO alpaca_broker_orders
                (client_order_id, environment, state, broker_order_id, symbol, side,
                 order_date, record_json, unresolved_reason, created_at, updated_at)
                VALUES (?, ?, 'CONFIRMED', ?, ?, ?, ?, ?, NULL, ?, ?)
                ON CONFLICT(client_order_id) DO UPDATE SET
                    state = 'CONFIRMED',
                    broker_order_id = excluded.broker_order_id,
                    record_json = excluded.record_json,
                    unresolved_reason = NULL,
                    updated_at = excluded.updated_at
                """,
                (
                    client_order_id,
                    environment,
                    record.get("broker_order_id"),
                    str(record.get("symbol") or ""),
                    str(record.get("side") or ""),
                    day,
                    json.dumps(record, sort_keys=True),
                    now,
                    now,
                ),
            )

    def save_fill(self, environment: str, fill_key: str, client_order_id: str, fill: dict[str, Any]) -> bool:
        now = _utc_now()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO alpaca_broker_fills
                (fill_key, environment, client_order_id, fill_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (fill_key, environment, client_order_id, json.dumps(fill, sort_keys=True), now),
            )
            return cur.rowcount > 0

    def load_state(self, environment: str) -> dict[str, Any]:
        orders: dict[str, dict[str, Any]] = {}
        unresolved: dict[str, str] = {}
        daily_dates: dict[str, str] = {}
        fills: list[dict[str, Any]] = []
        with self._lock, self._connect() as conn:
            for row in conn.execute(
                "SELECT * FROM alpaca_broker_orders WHERE environment = ? ORDER BY created_at ASC",
                (environment,),
            ):
                cid = str(row["client_order_id"])
                state = str(row["state"])
                rec = json.loads(row["record_json"])
                daily_dates[cid] = str(row["order_date"])
                if state in {"INTENT_PENDING", "UNRESOLVED_INTENT"}:
                    unresolved[cid] = str(row["unresolved_reason"] or state)
                else:
                    orders[cid] = rec
            for f_row in conn.execute(
                "SELECT fill_json FROM alpaca_broker_fills WHERE environment = ? ORDER BY created_at ASC",
                (environment,),
            ):
                fills.append(json.loads(f_row["fill_json"]))
        return {
            "orders": orders,
            "unresolved": unresolved,
            "daily_dates": daily_dates,
            "fills": fills,
        }

    def clear_unresolved(self, client_order_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM alpaca_broker_orders WHERE client_order_id = ? AND state IN ('INTENT_PENDING', 'UNRESOLVED_INTENT')",
                (client_order_id,),
            )

    def close(self) -> None:
        if self._mem_conn is not None:
            self._mem_conn.close()
            self._mem_conn = None
