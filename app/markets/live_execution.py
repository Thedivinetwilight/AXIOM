"""Per-market LIVE execution (NSE/INR · Upstox GTT v3, NASDAQ/USD · Alpaca Trading v2).

This module is the **separate execution path** required for real-money trading.
It is deliberately independent of :class:`app.markets.live_paper.LiveMarketPaperEngine`:

* Local LIVE-MARKET PAPER never enters this module.
* This module never writes to the paper ledger, and paper never writes here.
* Switching a market between ``PAPER`` and ``LIVE`` is an explicit operator action;
  nothing auto-promotes a paper session.

Safety model (per market, independent of the other market)
---------------------------------------------------------
1. ``mode`` must be explicitly ``LIVE`` (default ``PAPER``).
2. Every readiness check must pass; the gate is then ``ARMED``.
3. ``Activate`` additionally requires the exact typed phrase
   ``ACTIVATE LIVE REAL-MONEY TRADING`` plus ``acknowledge_risk=True``.
4. Only ``ACTIVE`` can mint an in-memory permit; permits are never persisted.
5. Gates are process-local and start ``DISABLED`` on every startup/restart, so
   live trading is always disarmed after a restart, crash, or recovery.
6. Every order is journaled *intent-before-write*; ambiguity latches the market
   ``UNRESOLVED_INTENT``, disarms that market, and blocks further submissions
   until an explicit broker reconciliation succeeds.
7. A central risk gate (order value, daily orders, daily loss, open positions,
   symbol/total exposure, quote freshness, price deviation, idempotency) runs
   before any broker call. Limits are positive by construction and never relaxed.
8. Emergency stop is per market: halting NSE never disables NASDAQ safeguards.

Secrets are never written to the journal, audit trail, logs, or API payloads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Callable
from uuid import uuid4

from app.autonomy.models import ExecutionPermit, Instrument, ProtectedOrder
from app.autonomy.risk import ceil_tick, floor_tick
from app.autonomy.upstox import (
    AmbiguousSubmission,
    ReconciliationFailure,
    UpstoxError,
    UpstoxRejected,
)
from app.data.models import parse_timestamp
from app.markets.broker_capabilities import (
    CapabilityRefused,
    capabilities_for,
    describe_capabilities,
    idempotency_asymmetry,
)
from app.markets.nse_reconciliation import nse_session_status, resolve_nse_instrument_strict
from app.monitoring.paper_events import redact_sensitive_data, redact_sensitive_text

MARKETS: tuple[str, ...] = ("NSE", "NASDAQ")

MODE_OFF = "OFF"
MODE_PAPER = "PAPER"
MODE_LIVE = "LIVE"
EXECUTION_MODES: tuple[str, ...] = (MODE_OFF, MODE_PAPER, MODE_LIVE)

LIVE_CONFIRM_PHRASE = "ACTIVATE LIVE REAL-MONEY TRADING"
PERMIT_TTL_SECONDS = 300.0


class LiveExecutionError(RuntimeError):
    """Base class for fail-closed live-execution refusals."""


class LiveGateRefused(LiveExecutionError):
    """The per-market gate is not ACTIVE / readiness failed / kill switch engaged."""


class LiveBrokerRejected(LiveExecutionError):
    """The broker refused the order; nothing was accepted."""


class LiveSubmissionUncertain(LiveExecutionError):
    """The submission outcome is unknown; reconcile before any retry."""


class LiveReconciliationFailed(LiveExecutionError):
    """Broker reconciliation could not establish the true state."""


def _utc_now(clock: Callable[[], datetime] | None = None) -> datetime:
    return (clock or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Risk limits (per market, currency-scoped)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LiveRiskLimits:
    """Every live limit is explicit; a missing/zero value fails closed."""

    max_order_value: float = 0.0
    max_daily_orders: int = 0
    max_daily_loss: float = 0.0
    max_open_positions: int = 0
    max_symbol_exposure: float = 0.0
    max_total_exposure: float = 0.0
    max_quote_age_seconds: float = 0.0
    max_price_deviation_bps: float = 0.0

    def configured(self) -> bool:
        values = (
            self.max_order_value,
            self.max_daily_orders,
            self.max_daily_loss,
            self.max_open_positions,
            self.max_symbol_exposure,
            self.max_total_exposure,
            self.max_quote_age_seconds,
            self.max_price_deviation_bps,
        )
        return all(isinstance(v, (int, float)) and math.isfinite(v) and v > 0 for v in values)

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_order_value": self.max_order_value,
            "max_daily_orders": self.max_daily_orders,
            "max_daily_loss": self.max_daily_loss,
            "max_open_positions": self.max_open_positions,
            "max_symbol_exposure": self.max_symbol_exposure,
            "max_total_exposure": self.max_total_exposure,
            "max_quote_age_seconds": self.max_quote_age_seconds,
            "max_price_deviation_bps": self.max_price_deviation_bps,
            "configured": self.configured(),
        }


# ---------------------------------------------------------------------------
# Per-market gate
# ---------------------------------------------------------------------------


class GateState(str, Enum):
    DISABLED = "DISABLED"
    READINESS_FAILED = "READINESS_FAILED"
    ARMED = "ARMED"
    ACTIVE = "ACTIVE"
    KILL_SWITCH = "KILL_SWITCH"


@dataclass(frozen=True)
class LiveSessionPermit:
    market: str
    nonce: str
    issued_at: datetime

    def validate(self, market: str, now: datetime) -> None:
        if self.market != market or not self.nonce:
            raise LiveGateRefused("Live permit does not belong to this market")
        if now >= self.issued_at + timedelta(seconds=PERMIT_TTL_SECONDS):
            raise LiveGateRefused("Live permit expired; re-activate before submitting")


class MarketLiveGate:
    """In-memory Arm/Activate state machine for one market. Never persisted."""

    def __init__(self, market: str, *, clock: Callable[[], datetime] | None = None) -> None:
        self.market = market
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.state = GateState.DISABLED
        self.kill_engaged = False
        self.kill_reason: str | None = None
        self.armed_at: str | None = None
        self.activated_at: str | None = None
        self.last_readiness: dict[str, Any] = {}
        self.last_disarm_reason: str | None = None
        self._permit: LiveSessionPermit | None = None
        self._lock = threading.RLock()

    # -- readiness ---------------------------------------------------------
    def readiness_snapshot(self, checks: dict[str, bool]) -> dict[str, Any]:
        if not isinstance(checks, dict) or not checks:
            raise LiveGateRefused("readiness checks are missing; LIVE is unavailable")
        with self._lock:
            self.last_readiness = {
                "checked_at": _iso(_utc_now(self._clock)),
                "checks": {str(k): bool(v) for k, v in checks.items()},
                "all_passed": all(bool(v) for v in checks.values()),
            }
            return dict(self.last_readiness)

    def _failed(self) -> list[str]:
        checks = (self.last_readiness or {}).get("checks") or {}
        if not checks:
            raise LiveGateRefused("no readiness checks recorded; run readiness before arming")
        return sorted(name for name, ok in checks.items() if not ok)

    def arm(self, checks: dict[str, bool]) -> dict[str, Any]:
        with self._lock:
            if self.kill_engaged:
                raise LiveGateRefused("kill switch engaged; reset it before any LIVE action")
            self.readiness_snapshot(checks)
            failed = self._failed()
            if failed:
                self.state = GateState.READINESS_FAILED
                raise LiveGateRefused(f"readiness checks failed: {', '.join(failed)}; LIVE is unavailable")
            self.state = GateState.ARMED
            self.armed_at = _iso(_utc_now(self._clock))
            self.activated_at = None
            self._permit = None
            return self.status()

    def activate(self, *, confirm_phrase: str, acknowledge_risk: bool) -> LiveSessionPermit:
        with self._lock:
            if self.kill_engaged:
                raise LiveGateRefused("kill switch engaged; reset it before any LIVE action")
            if not acknowledge_risk or (confirm_phrase or "").strip() != LIVE_CONFIRM_PHRASE:
                raise LiveGateRefused(
                    "LIVE activation requires acknowledge_risk=true and the exact phrase "
                    f"{LIVE_CONFIRM_PHRASE!r}"
                )
            if self.state != GateState.ARMED:
                raise LiveGateRefused("LIVE requires an explicit Arm (with passing readiness) before Activate")
            self._failed()
            self.state = GateState.ACTIVE
            self.activated_at = _iso(_utc_now(self._clock))
            self._permit = LiveSessionPermit(
                market=self.market,
                nonce=uuid4().hex,
                issued_at=_utc_now(self._clock),
            )
            return self._permit

    def permit(self) -> LiveSessionPermit | None:
        with self._lock:
            return self._permit if self.state == GateState.ACTIVE and not self.kill_engaged else None

    def disarm(self, reason: str = "operator disarm") -> dict[str, Any]:
        with self._lock:
            self.state = GateState.KILL_SWITCH if self.kill_engaged else GateState.DISABLED
            self.armed_at = None
            self.activated_at = None
            self._permit = None
            self.last_disarm_reason = reason
            return self.status()

    def engage_kill_switch(self, reason: str) -> dict[str, Any]:
        with self._lock:
            self.kill_engaged = True
            self.kill_reason = reason
            self.state = GateState.KILL_SWITCH
            self.armed_at = None
            self.activated_at = None
            self._permit = None
            self.last_disarm_reason = f"kill switch: {reason}"
            return self.status()

    def reset_kill_switch(self, *, operator_ack: bool) -> dict[str, Any]:
        if not operator_ack:
            raise LiveGateRefused("kill-switch reset requires an explicit operator acknowledgement")
        with self._lock:
            self.kill_engaged = False
            self.kill_reason = None
            self.state = GateState.DISABLED
            self._permit = None
            self.armed_at = None
            self.activated_at = None
            return self.status()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "market": self.market,
                "state": self.state.value,
                "kill_engaged": self.kill_engaged,
                "kill_reason": self.kill_reason,
                "armed_at": self.armed_at,
                "activated_at": self.activated_at,
                "permit_active": self._permit is not None and self.state == GateState.ACTIVE,
                "readiness": dict(self.last_readiness),
                "live_available": self.state == GateState.ACTIVE and not self.kill_engaged,
                "disarm_reason": self.last_disarm_reason,
                "live_confirmation_phrase": LIVE_CONFIRM_PHRASE,
                "permit_ttl_seconds": PERMIT_TTL_SECONDS,
            }


# ---------------------------------------------------------------------------
# Durable intent-before-write journal with live audit hash chain
# ---------------------------------------------------------------------------


class LiveMarketOrderJournal:
    """Thread-safe SQLite journal: modes, intents, orders, fills and audit chain.

    * ``intent`` rows are written BEFORE any broker call.
    * ``UNRESOLVED_INTENT`` latches a market until reconciliation proves state.
    * The audit table stores a SHA-256 chain with ``real_money: true``.
    """

    def __init__(self, db_path: Path | str | None, *, clock: Callable[[], datetime] | None = None) -> None:
        self._in_memory = db_path is None or str(db_path) == ":memory:"
        self.db_path = Path(":memory:") if self._in_memory else Path(str(db_path))
        self._clock = clock or (lambda: datetime.now(timezone.utc))
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
                CREATE TABLE IF NOT EXISTS live_market_modes (
                    market TEXT PRIMARY KEY,
                    mode TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS live_market_order_intents (
                    market TEXT NOT NULL,
                    client_order_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    order_date TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    unresolved_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (market, client_order_id)
                );
                CREATE TABLE IF NOT EXISTS live_market_orders (
                    market TEXT NOT NULL,
                    client_order_id TEXT NOT NULL,
                    broker_order_id TEXT,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    order_date TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (market, client_order_id)
                );
                CREATE TABLE IF NOT EXISTS live_market_fills (
                    market TEXT NOT NULL,
                    fill_key TEXT NOT NULL,
                    client_order_id TEXT NOT NULL,
                    fill_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (market, fill_key)
                );
                CREATE TABLE IF NOT EXISTS live_market_settings (
                    market TEXT PRIMARY KEY,
                    limits_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS live_market_audit (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    market TEXT NOT NULL,
                    event_kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    prev_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    # -- modes -------------------------------------------------------------
    def load_modes(self) -> dict[str, str]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT market, mode FROM live_market_modes").fetchall()
        return {str(r["market"]): str(r["mode"]) for r in rows}

    def save_mode(self, market: str, mode: str) -> None:
        now = _iso(_utc_now(self._clock))
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO live_market_modes (market, mode, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(market) DO UPDATE SET mode = excluded.mode, updated_at = excluded.updated_at
                """,
                (market, mode, now),
            )

    # -- per-market live settings -----------------------------------------
    def save_limits(self, market: str, limits: dict[str, Any]) -> None:
        now = _iso(_utc_now(self._clock))
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO live_market_settings (market, limits_json, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(market) DO UPDATE SET
                    limits_json = excluded.limits_json, updated_at = excluded.updated_at
                """,
                (market, json.dumps(limits, sort_keys=True), now),
            )

    def load_limits(self) -> dict[str, dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT market, limits_json FROM live_market_settings").fetchall()
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            try:
                parsed = json.loads(row["limits_json"])
            except ValueError:
                continue
            if isinstance(parsed, dict):
                out[str(row["market"])] = parsed
        return out

    # -- intents / orders --------------------------------------------------
    def record_intent(
        self,
        *,
        market: str,
        client_order_id: str,
        symbol: str,
        side: str,
        payload: dict[str, Any],
    ) -> None:
        now = _iso(_utc_now(self._clock))
        record = {
            "client_order_id": client_order_id,
            "market": market,
            "symbol": symbol,
            "side": side,
            "qty": payload.get("qty"),
            "order_type": payload.get("order_type"),
            "limit_price": payload.get("limit_price"),
            "stop_price": payload.get("stop_price"),
            "target_price": payload.get("target_price"),
            "reference_price": payload.get("reference_price"),
            "real_money": True,
            "status": "INTENT_PENDING",
            "broker_order_ids": [],
            "submitted_at": now,
            "updated_at": now,
        }
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO live_market_order_intents
                (market, client_order_id, state, symbol, side, order_date, record_json,
                 unresolved_reason, created_at, updated_at)
                VALUES (?, ?, 'INTENT_PENDING', ?, ?, ?, ?, 'in_flight_intent', ?, ?)
                ON CONFLICT(market, client_order_id) DO NOTHING
                """,
                (
                    market,
                    client_order_id,
                    symbol,
                    side,
                    _utc_now(self._clock).date().isoformat(),
                    json.dumps(record, sort_keys=True),
                    now,
                    now,
                ),
            )

    def mark_unresolved(self, market: str, client_order_id: str, reason: str) -> None:
        now = _iso(_utc_now(self._clock))
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE live_market_order_intents
                SET state = 'UNRESOLVED_INTENT', unresolved_reason = ?, updated_at = ?
                WHERE market = ? AND client_order_id = ?
                """,
                (redact_sensitive_text(str(reason))[:240], now, market, client_order_id),
            )

    def remove_intent(self, market: str, client_order_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM live_market_order_intents WHERE market = ? AND client_order_id = ? AND state = 'INTENT_PENDING'",
                (market, client_order_id),
            )

    def clear_unresolved(self, market: str, client_order_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM live_market_order_intents WHERE market = ? AND client_order_id = ?",
                (market, client_order_id),
            )

    def save_order(
        self,
        *,
        market: str,
        client_order_id: str,
        record: dict[str, Any],
    ) -> None:
        now = _iso(_utc_now(self._clock))
        payload = redact_sensitive_data({**record, "real_money": True})
        if not isinstance(payload, dict):
            payload = {"detail": str(payload)}
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO live_market_orders
                (market, client_order_id, broker_order_id, symbol, side, order_date,
                 record_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(market, client_order_id) DO UPDATE SET
                    broker_order_id = excluded.broker_order_id,
                    record_json = excluded.record_json,
                    updated_at = excluded.updated_at
                """,
                (
                    market,
                    client_order_id,
                    record.get("broker_order_id") or (record.get("broker_order_ids") or [None])[0],
                    str(record.get("symbol") or ""),
                    str(record.get("side") or ""),
                    _utc_now(self._clock).date().isoformat(),
                    json.dumps(payload, sort_keys=True),
                    now,
                    now,
                ),
            )
            conn.execute(
                "UPDATE live_market_order_intents SET state = 'CONFIRMED', updated_at = ?, unresolved_reason = NULL "
                "WHERE market = ? AND client_order_id = ?",
                (now, market, client_order_id),
            )

    def save_fill(self, market: str, fill_key: str, client_order_id: str, fill: dict[str, Any]) -> bool:
        now = _iso(_utc_now(self._clock))
        safe = redact_sensitive_data({**fill, "real_money": True})
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO live_market_fills
                (market, fill_key, client_order_id, fill_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (market, fill_key, client_order_id, json.dumps(safe, sort_keys=True), now),
            )
            return cur.rowcount > 0

    def has_order(self, market: str, client_order_id: str) -> bool:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM live_market_orders WHERE market = ? AND client_order_id = ?",
                (market, client_order_id),
            ).fetchone()
            if row is not None:
                return True
            intent = conn.execute(
                "SELECT 1 FROM live_market_order_intents WHERE market = ? AND client_order_id = ?",
                (market, client_order_id),
            ).fetchone()
        return intent is not None

    def load_state(self, market: str) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            orders: dict[str, Any] = {}
            unresolved: dict[str, str] = {}
            daily_dates: dict[str, str] = {}
            fills: list[Any] = []
            for row in conn.execute(
                "SELECT * FROM live_market_orders WHERE market = ? ORDER BY created_at ASC",
                (market,),
            ):
                orders[str(row["client_order_id"])] = json.loads(row["record_json"])
            for row in conn.execute(
                "SELECT * FROM live_market_order_intents WHERE market = ? ORDER BY created_at ASC",
                (market,),
            ):
                cid = str(row["client_order_id"])
                daily_dates[cid] = str(row["order_date"])
                if str(row["state"]) in {"INTENT_PENDING", "UNRESOLVED_INTENT"}:
                    unresolved[cid] = str(row["unresolved_reason"] or row["state"])
            for row in conn.execute(
                "SELECT * FROM live_market_fills WHERE market = ? ORDER BY created_at ASC",
                (market,),
            ):
                fills.append(json.loads(row["fill_json"]))
        return {"orders": orders, "unresolved": unresolved, "daily_dates": daily_dates, "fills": fills}

    def daily_order_count(self, market: str, day: str) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM live_market_order_intents WHERE market = ? AND order_date = ?",
                (market, day),
            ).fetchone()
        return int(row["n"] if row is not None else 0)

    def journaled_orders(self, market: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT record_json FROM live_market_orders WHERE market = ? ORDER BY created_at DESC LIMIT 200",
                (market,),
            ).fetchall()
        return [json.loads(r["record_json"]) for r in rows]

    # -- audit chain -------------------------------------------------------
    def audit(self, market: str, event_kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        now = _iso(_utc_now(self._clock))
        safe = redact_sensitive_data({"market": market, "real_money": True, **payload})
        if not isinstance(safe, dict):
            safe = {"detail": str(safe)}
        serialized = json.dumps(safe, sort_keys=True)
        with self._lock, self._connect() as conn:
            last = conn.execute("SELECT entry_hash FROM live_market_audit ORDER BY seq DESC LIMIT 1").fetchone()
            prev_hash = str(last["entry_hash"]) if last else "GENESIS"
            digest = hashlib.sha256(
                f"{prev_hash}|{market}|{event_kind}|{now}|{serialized}".encode("utf-8")
            ).hexdigest()
            conn.execute(
                """
                INSERT INTO live_market_audit (market, event_kind, payload_json, prev_hash, entry_hash, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (market, event_kind, serialized, prev_hash, digest, now),
            )
            seq_row = conn.execute("SELECT seq FROM live_market_audit ORDER BY seq DESC LIMIT 1").fetchone()
        return {
            "seq": int(seq_row["seq"]) if seq_row else 0,
            "market": market,
            "event_kind": event_kind,
            "payload": safe,
            "prev_hash": prev_hash,
            "entry_hash": digest,
            "created_at": now,
        }

    def audit_log(self, *, market: str = "", limit: int = 200) -> list[dict[str, Any]]:
        lim = max(1, min(int(limit), 500))
        with self._lock, self._connect() as conn:
            if market:
                rows = conn.execute(
                    "SELECT * FROM live_market_audit WHERE market = ? ORDER BY seq DESC LIMIT ?",
                    (market, lim),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM live_market_audit ORDER BY seq DESC LIMIT ?", (lim,)
                ).fetchall()
        return [
            {
                "seq": int(r["seq"]),
                "market": str(r["market"]),
                "event_kind": str(r["event_kind"]),
                "payload": json.loads(r["payload_json"]),
                "prev_hash": str(r["prev_hash"]),
                "entry_hash": str(r["entry_hash"]),
                "created_at": str(r["created_at"]),
            }
            for r in rows
        ]

    def close(self) -> None:
        if self._mem_conn is not None:
            self._mem_conn.close()
            self._mem_conn = None


# ---------------------------------------------------------------------------
# Broker adapters
# ---------------------------------------------------------------------------


class LiveMarketAdapter:
    """Uniform per-market broker surface used by :class:`LiveExecutionCoordinator`."""

    market = ""
    currency = ""
    broker_name = ""
    destination_label = ""
    supports_short_entry = False

    def missing_dependencies(self) -> list[str]:
        return ["adapter is not implemented"]

    def readiness_checks(self, *, symbol: str | None = None) -> dict[str, bool]:
        return {"adapter_implemented": False}

    def gate_status(self) -> dict[str, Any]:
        return {"market": self.market, "state": GateState.DISABLED.value, "live_available": False}

    def arm(self, checks: dict[str, bool]) -> dict[str, Any]:
        raise LiveGateRefused(f"{self.market} adapter cannot arm LIVE")

    def activate(self, *, confirm_phrase: str, acknowledge_risk: bool) -> dict[str, Any]:
        raise LiveGateRefused(f"{self.market} adapter cannot activate LIVE")

    def disarm(self, reason: str) -> dict[str, Any]:
        raise LiveExecutionError(f"{self.market} adapter cannot disarm")

    def engage_kill_switch(self, reason: str) -> dict[str, Any]:
        raise LiveExecutionError(f"{self.market} adapter cannot engage a kill switch")

    def reset_kill_switch(self, *, operator_ack: bool) -> dict[str, Any]:
        raise LiveExecutionError(f"{self.market} adapter cannot reset a kill switch")

    def account(self) -> dict[str, Any]:
        raise LiveExecutionError(f"{self.market} account is unavailable")

    def positions(self) -> list[dict[str, Any]]:
        raise LiveExecutionError(f"{self.market} positions are unavailable")

    def working_orders(self) -> list[dict[str, Any]]:
        return []

    def quote(self, symbol: str) -> dict[str, Any] | None:
        return None

    def market_open(self, now: datetime) -> bool:
        return False

    def submit(
        self,
        *,
        client_order_id: str,
        symbol: str,
        side: str,
        qty: float,
        reference_price: float,
        stop_loss: float | None,
        take_profit: float | None,
        product: str,
        instrument_meta: dict[str, Any] | None,
    ) -> dict[str, Any]:
        raise LiveExecutionError(f"{self.market} adapter cannot submit orders")

    def reconcile(self, unresolved: dict[str, str]) -> dict[str, Any]:
        raise LiveReconciliationFailed(f"{self.market} adapter cannot reconcile")

    def cancel(self, client_order_id: str) -> dict[str, Any]:
        raise LiveBrokerRejected(f"{self.market} adapter cannot cancel orders")


class NasdaqLiveAdapter(LiveMarketAdapter):
    """NASDAQ/USD live routing through the existing gated Alpaca broker service."""

    market = "NASDAQ"
    currency = "USD"
    broker_name = "Alpaca Trading API v2"
    destination_label = "NASDAQ · ALPACA LIVE · REAL MONEY"
    supports_short_entry = True

    def __init__(
        self,
        service_provider: Callable[[], Any],
        *,
        quote_provider: Callable[[str], dict[str, Any] | None] | None = None,
        calendar: Any = None,
        account_ttl_seconds: float = 10.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._service_provider = service_provider
        self._quote_provider = quote_provider
        self._calendar = calendar
        self._account_ttl = max(0.0, float(account_ttl_seconds))
        self._account_cached_at: datetime | None = None
        self._account_cache: dict[str, Any] | None = None
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # -- helpers -----------------------------------------------------------
    def _service(self) -> Any:
        return self._service_provider()

    def missing_dependencies(self) -> list[str]:
        svc = self._service()
        if svc is None:
            return [
                "Alpaca Broker path is disabled — enable the Alpaca Broker path and set "
                "ALPACA_BROKER_ENABLED=true in the host environment"
            ]
        missing: list[str] = []
        if getattr(svc, "environment", "paper") != "live":
            missing.append(
                "Alpaca Broker environment is not 'live' — select LIVE and save distinct "
                "ALPACA_BROKER_LIVE keys (PAPER routing is a different destination)"
            )
        if not svc.limits.configured():
            missing.append("Alpaca broker hard limits are incomplete (all limits must be positive)")
        return missing

    def readiness_checks(self, *, symbol: str | None = None) -> dict[str, bool]:
        svc = self._service()
        if svc is None:
            return {"alpaca_broker_configured": False}
        checks = dict(svc.readiness_checks(symbol=symbol))
        checks["environment_is_live"] = getattr(svc, "environment", "paper") == "live"
        if self._calendar is not None:
            try:
                checks["calendar_session_open"] = bool(self._calendar.is_open(_utc_now(self._clock)))
            except (ValueError, TypeError):
                checks["calendar_session_open"] = False
        return checks

    def gate_status(self) -> dict[str, Any]:
        svc = self._service()
        if svc is None:
            return {
                "market": self.market,
                "state": GateState.DISABLED.value,
                "kill_engaged": False,
                "live_available": False,
                "permit_active": False,
                "configured": False,
                "live_confirmation_phrase": LIVE_CONFIRM_PHRASE,
            }
        return {**svc.gate.status(), "market": self.market, "configured": True,
                "live_confirmation_phrase": LIVE_CONFIRM_PHRASE}

    def arm(self, checks: dict[str, bool]) -> dict[str, Any]:
        svc = self._service()
        if svc is None:
            raise LiveGateRefused("Alpaca broker path is disabled; LIVE is unavailable")
        return svc.gate.arm(checks)

    def activate(self, *, confirm_phrase: str, acknowledge_risk: bool) -> dict[str, Any]:
        svc = self._service()
        if svc is None:
            raise LiveGateRefused("Alpaca broker path is disabled; LIVE is unavailable")
        return svc.activate_live_confirmed(
            confirm_live_trading=confirm_phrase,
            acknowledge_risk=acknowledge_risk,
        )

    def disarm(self, reason: str) -> dict[str, Any]:
        svc = self._service()
        if svc is None:
            return {"state": GateState.DISABLED.value, "live_available": False}
        return svc.disarm(reason)

    def engage_kill_switch(self, reason: str) -> dict[str, Any]:
        svc = self._service()
        if svc is None:
            return {"state": GateState.KILL_SWITCH.value, "kill_engaged": True}
        return svc.gate.engage_kill_switch(reason)

    def reset_kill_switch(self, *, operator_ack: bool) -> dict[str, Any]:
        svc = self._service()
        if svc is None:
            raise LiveGateRefused("Alpaca broker path is disabled; nothing to reset")
        return svc.gate.reset_kill_switch(operator_ack=operator_ack)

    def account(self) -> dict[str, Any]:
        svc = self._service()
        if svc is None:
            raise LiveExecutionError("Alpaca broker path is disabled; account is unavailable")
        now = _utc_now(self._clock)
        if self._account_cache is not None and self._account_cached_at is not None:
            if (now - self._account_cached_at).total_seconds() <= self._account_ttl:
                return dict(self._account_cache)
        if getattr(svc, "account", None) is None:
            svc.readiness_checks()
        account = getattr(svc, "account", None)
        if not isinstance(account, dict) or not account:
            raise LiveExecutionError("Alpaca account state is unavailable; live orders are blocked")
        try:
            equity = float(account.get("equity") or 0.0)
            last_equity = float(account.get("last_equity") or 0.0)
            buying_power = float(account.get("buying_power") or 0.0)
            cash = float(account.get("cash") or 0.0)
        except (TypeError, ValueError):
            raise LiveExecutionError("Alpaca account values are not numeric") from None
        if not all(math.isfinite(v) for v in (equity, last_equity, buying_power, cash)) or equity <= 0:
            raise LiveExecutionError("Alpaca account state is not finite/positive")
        payload = {
            "market": self.market,
            "currency": str(account.get("currency") or "USD").upper(),
            "equity": equity,
            "last_equity": last_equity,
            "day_pnl": equity - last_equity if last_equity > 0 else 0.0,
            "buying_power": buying_power,
            "cash": cash,
            "trading_blocked": bool(
                account.get("trading_blocked")
                or account.get("account_blocked")
                or account.get("trade_suspended_by_user")
            ),
            "authoritative": True,
        }
        self._account_cache = payload
        self._account_cached_at = now
        return dict(payload)

    def positions(self) -> list[dict[str, Any]]:
        svc = self._service()
        if svc is None:
            return []
        result = []
        for sym, pos in dict(getattr(svc, "positions", {}) or {}).items():
            try:
                qty = float(pos.get("qty") or 0.0)
                market_value = abs(float(pos.get("market_value") or 0.0))
            except (TypeError, ValueError):
                continue
            result.append({"symbol": sym, "qty": qty, "market_value": market_value})
        return result

    def working_orders(self) -> list[dict[str, Any]]:
        svc = self._service()
        if svc is None:
            return []
        open_statuses = {"new", "accepted", "pending_new", "partially_filled", "held", "accepted_for_bidding"}
        return [
            dict(record)
            for record in dict(getattr(svc, "orders", {}) or {}).values()
            if str(record.get("status") or "").lower() in open_statuses
        ]

    def quote(self, symbol: str) -> dict[str, Any] | None:
        if self._quote_provider is None:
            return None
        return self._quote_provider(symbol)

    def market_open(self, now: datetime) -> bool:
        if self._calendar is None:
            return False
        return bool(self._calendar.is_open(now))

    def submit(
        self,
        *,
        client_order_id: str,
        symbol: str,
        side: str,
        qty: float,
        reference_price: float,
        stop_loss: float | None,
        take_profit: float | None,
        product: str,
        instrument_meta: dict[str, Any] | None,
    ) -> dict[str, Any]:
        svc = self._service()
        if svc is None:
            raise LiveGateRefused("Alpaca broker path is disabled; LIVE is unavailable")
        side_l = side.lower()
        if side_l not in {"buy", "sell"}:
            raise LiveBrokerRejected(f"invalid side {side!r}")
        if not (math.isfinite(qty) and qty > 0):
            raise LiveBrokerRejected("quantity must be a finite positive number")
        # Bounded limit price at the decision reference keeps fills inside the quoted band.
        decimals = 4 if reference_price < 1.0 else 2
        limit_price = round(float(reference_price), decimals)
        try:
            result = svc.submit_order(
                client_order_id=client_order_id,
                symbol=symbol,
                side=side_l,
                qty=qty,
                order_type="limit",
                time_in_force="day",
                limit_price=limit_price,
                reference_price=limit_price,
            )
        except Exception as exc:  # noqa: BLE001 - unknown outcome must fail closed
            raise LiveSubmissionUncertain(
                f"Alpaca submission outcome is unknown ({type(exc).__name__}); reconcile before retry"
            ) from None
        if not isinstance(result, dict):
            raise LiveSubmissionUncertain("Alpaca returned an unreadable submission result")
        if result.get("rejected") == "uncertain":
            raise LiveSubmissionUncertain(
                str(result.get("detail") or "Alpaca submission outcome is uncertain")
            )
        if not result.get("ok"):
            reasons = (result.get("preview") or {}).get("rejection_reasons") or [
                result.get("detail") or result.get("rejected") or "rejected"
            ]
            raise LiveBrokerRejected("; ".join(str(r) for r in reasons)[:400])
        order = result.get("order") or {}
        broker_id = order.get("broker_order_id") or order.get("id")
        record = {
            "client_order_id": client_order_id,
            "broker_order_id": broker_id,
            "broker_order_ids": [broker_id] if broker_id else [],
            "symbol": symbol,
            "side": side_l.upper(),
            "qty": qty,
            "order_type": "limit",
            "limit_price": limit_price,
            "stop_price": stop_loss,
            "target_price": take_profit,
            "status": order.get("status") or "SUBMITTED",
            "duplicate": bool(result.get("duplicate")),
            "broker": "alpaca",
            "environment": getattr(svc, "environment", "live"),
            "real_money": True,
        }
        protection = self._submit_protection(
            entry_client_order_id=client_order_id,
            symbol=symbol,
            entry_side=side_l,
            qty=qty,
            stop_loss=stop_loss,
            take_profit=take_profit,
            reference_price=reference_price,
        )
        record["protection"] = protection
        return record

    def _submit_protection(
        self,
        *,
        entry_client_order_id: str,
        symbol: str,
        entry_side: str,
        qty: float,
        stop_loss: float | None,
        take_profit: float | None,
        reference_price: float,
    ) -> dict[str, Any]:
        """Submit the protective stop created from the decision's risk plan.

        A failed protection attempt is reported (never hidden) so the coordinator
        can block the market and demand operator action.
        """
        svc = self._service()
        if svc is None:
            return {"ok": False, "reason": "broker_unavailable"}
        if stop_loss is None or take_profit is None:
            return {"ok": False, "reason": "no_protective_prices_in_decision"}
        try:
            stop_side = "sell" if entry_side == "buy" else "buy"
            stop_px = round(float(stop_loss), 4 if float(stop_loss) < 1.0 else 2)
            result = svc.submit_order(
                client_order_id=f"{entry_client_order_id}-sl",
                symbol=symbol,
                side=stop_side,
                qty=qty,
                order_type="stop",
                time_in_force="day",
                stop_price=stop_px,
                reference_price=stop_px,
            )
        except Exception as exc:  # noqa: BLE001 - fail closed, report explicitly
            return {"ok": False, "reason": f"protection_submit_error:{type(exc).__name__}"}
        if not isinstance(result, dict) or not result.get("ok"):
            detail = ""
            if isinstance(result, dict):
                detail = str(result.get("detail") or result.get("rejected") or "")
            return {"ok": False, "reason": redact_sensitive_text(detail)[:200] or "protection_rejected"}
        return {
            "ok": True,
            "client_order_id": f"{entry_client_order_id}-sl",
            "stop_price": stop_px,
            "reference_price": reference_price,
        }

    def reconcile(self, unresolved: dict[str, str]) -> dict[str, Any]:
        svc = self._service()
        if svc is None:
            raise LiveReconciliationFailed("Alpaca broker path is disabled; cannot reconcile")
        try:
            result = svc.recover()
        except Exception as exc:  # noqa: BLE001 - reconciliation failure blocks live
            raise LiveReconciliationFailed(
                f"Alpaca reconciliation failed ({type(exc).__name__})"
            ) from None
        if not isinstance(result, dict) or not result.get("ok"):
            raise LiveReconciliationFailed("Alpaca reconciliation did not confirm broker state")
        self._account_cache = None
        self._account_cached_at = None
        return {
            "ok": True,
            "broker": "alpaca",
            "resolved_intents": int(result.get("resolved_intents") or 0),
            "adopted_orders": int(result.get("adopted_orders") or 0),
            "positions": int(result.get("positions") or 0),
            "pending_unresolved": len(unresolved),
        }

    def cancel(self, client_order_id: str) -> dict[str, Any]:
        svc = self._service()
        if svc is None:
            raise LiveBrokerRejected("Alpaca broker path is disabled; cannot cancel")
        return svc.cancel_order(client_order_id)


class NseLiveAdapter(LiveMarketAdapter):
    """NSE/INR live routing through Upstox protected multi-leg GTT v3 orders."""

    market = "NSE"
    currency = "INR"
    broker_name = "Upstox (Protected Multi-Leg GTT v3)"
    destination_label = "NSE · UPSTOX LIVE · REAL MONEY"
    supports_short_entry = False

    def __init__(
        self,
        catalog: Any,
        *,
        live_token_provider: Callable[[], str],
        algo_name: str,
        live_enabled: bool,
        expected_public_ip: str = "",
        quote_provider: Callable[[str], dict[str, Any] | None] | None = None,
        broker_factory: Callable[[Callable[[], str]], Any] | None = None,
        write_rate_per_second: float = 5.0,
        max_write_attempts: int = 3,
        require_fresh_catalog: bool = True,
        session_provider: Callable[[datetime], dict[str, Any]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.catalog = catalog
        self._live_token_provider = live_token_provider
        self.algo_name = str(algo_name or "").strip()
        self.live_enabled = bool(live_enabled)
        self.expected_public_ip = str(expected_public_ip or "").strip()
        self._quote_provider = quote_provider
        self._broker_factory = broker_factory or (
            lambda token_provider: self._default_broker(token_provider, write_rate_per_second, max_write_attempts)
        )
        self.require_fresh_catalog = bool(require_fresh_catalog)
        self._session_provider = session_provider or nse_session_status
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.gate = MarketLiveGate("NSE", clock=self._clock)
        self._broker: Any = None
        self._broker_token: str = ""
        self._session_id = f"nse-live-{uuid4().hex[:12]}"
        self._lock = threading.RLock()

    @staticmethod
    def _default_broker(token_provider: Callable[[], str], rate: float, attempts: int) -> Any:
        from app.autonomy.upstox import UpstoxLiveBroker

        return UpstoxLiveBroker(
            token_provider,
            algo_name="AXIOM",
            write_rate_per_second=rate,
            max_write_attempts=attempts,
        )

    # -- configuration -----------------------------------------------------
    def _algo_configured(self) -> bool:
        return bool(self.algo_name) and not any(c in self.algo_name for c in "\r\n")

    def _live_token_present(self) -> bool:
        try:
            return bool(str(self._live_token_provider() or "").strip())
        except (RuntimeError, ValueError):
            return False

    def _catalog_fresh(self) -> bool:
        if not self.require_fresh_catalog:
            return True
        try:
            source_kind = str(getattr(self.catalog, "source_kind", "") or "")
            fresh_for_day = getattr(self.catalog, "fresh_for_day", None)
            if source_kind != "authoritative_upstox_bod" or not callable(fresh_for_day):
                return False
            trading_day = str(self._session_provider(_utc_now(self._clock)).get("trading_day") or "")
            return bool(trading_day) and bool(fresh_for_day(trading_day))
        except (ValueError, TypeError, RuntimeError):
            return False

    def missing_dependencies(self) -> list[str]:
        missing: list[str] = []
        if not self.live_enabled:
            missing.append("Set AUTONOMY_LIVE_ENABLED=true in the host environment")
        if not self._live_token_present():
            missing.append("Save a separate UPSTOX_LIVE_ACCESS_TOKEN in Connections (read-only token is never reused)")
        if not self._algo_configured():
            missing.append("Set UPSTOX_ALGO_NAME and register the SEBI static IP with Upstox")
        if not self.expected_public_ip:
            missing.append("Set the expected registered static public IP (AXIOM_UPSTOX_EXPECTED_PUBLIC_IP)")
        if not self._catalog_fresh():
            missing.append("Refresh the authoritative same-day Upstox BOD instrument catalog")
        return missing

    # -- gate --------------------------------------------------------------
    def gate_status(self) -> dict[str, Any]:
        status = self.gate.status()
        status["configured"] = not self.missing_dependencies()
        status["broker"] = self.broker_name
        return status

    def arm(self, checks: dict[str, bool]) -> dict[str, Any]:
        return self.gate.arm(checks)

    def activate(self, *, confirm_phrase: str, acknowledge_risk: bool) -> dict[str, Any]:
        permit = self.gate.activate(confirm_phrase=confirm_phrase, acknowledge_risk=acknowledge_risk)
        return {"active": True, "permit_issued_at": _iso(permit.issued_at), "gate": self.gate.status()}

    def disarm(self, reason: str) -> dict[str, Any]:
        with self._lock:
            self._broker = None
            self._broker_token = ""
        return self.gate.disarm(reason)

    def engage_kill_switch(self, reason: str) -> dict[str, Any]:
        with self._lock:
            self._broker = None
            self._broker_token = ""
        return self.gate.engage_kill_switch(reason)

    def reset_kill_switch(self, *, operator_ack: bool) -> dict[str, Any]:
        return self.gate.reset_kill_switch(operator_ack=operator_ack)

    # -- broker ------------------------------------------------------------
    def _resolve_instrument(self, symbol_or_key: str) -> Instrument:
        meta = resolve_nse_instrument_strict(self.catalog, symbol_or_key, allow_index=False)
        key = str(meta.get("instrument_key") or "")
        instruments = dict(getattr(self.catalog, "instruments", {}) or {})
        inst = instruments.get(key)
        if inst is None:
            raise LiveBrokerRejected(f"NSE instrument {key} is not in the authoritative catalog")
        return inst

    def _broker_for_write(self) -> Any:
        token = str(self._live_token_provider() or "").strip()
        if not token:
            raise LiveGateRefused("A separate LIVE Upstox credential is required for live order writes")
        with self._lock:
            if self._broker is None or self._broker_token != token:
                self._broker = self._broker_factory(lambda: token)
                self._broker_token = token
            return self._broker

    def account(self) -> dict[str, Any]:
        broker = self._broker_for_write()
        snapshot = broker.account()
        return {
            "market": self.market,
            "currency": "INR",
            "equity": float(snapshot.equity),
            "available_margin": float(snapshot.available_margin),
            "used_margin": float(snapshot.used_margin),
            "day_pnl": float(snapshot.day_pnl),
            "trading_blocked": False,
            "authoritative": bool(snapshot.authoritative),
        }

    def positions(self) -> list[dict[str, Any]]:
        broker = self._broker_for_write()
        result = []
        for pos in broker._positions():  # read-only GTT/position inspection
            result.append(
                {
                    "symbol": pos.instrument_key,
                    "qty": float(pos.quantity),
                    "market_value": float(pos.market_value),
                }
            )
        return result

    def working_orders(self) -> list[dict[str, Any]]:
        if self.gate.state != GateState.ACTIVE:
            return []
        broker = self._broker_for_write()
        return [dict(item) for item in broker.list_gtt()]

    def quote(self, symbol: str) -> dict[str, Any] | None:
        if self._quote_provider is None:
            return None
        inst = self._resolve_instrument(symbol)
        return self._quote_provider(inst.instrument_key)

    def market_open(self, now: datetime) -> bool:
        return bool(self._session_provider(now).get("is_open"))

    def readiness_checks(self, *, symbol: str | None = None) -> dict[str, bool]:
        checks = {
            "live_master_flag": self.live_enabled,
            "separate_live_credential": self._live_token_present(),
            "algo_name_configured": self._algo_configured(),
            "static_ip_configured": bool(self.expected_public_ip),
            "catalog_authoritative_today": self._catalog_fresh(),
            "market_session_open": self.market_open(_utc_now(self._clock)),
            "kill_switch_not_engaged": not self.gate.kill_engaged,
        }
        try:
            account = self.account()
            checks["account_verified"] = True
            checks["account_trading_allowed"] = not bool(account.get("trading_blocked"))
            checks["account_currency_inr"] = str(account.get("currency") or "").upper() == "INR"
        except (LiveExecutionError, UpstoxError, PermissionError, ValueError, KeyError):
            checks["account_verified"] = False
            checks["account_trading_allowed"] = False
        if symbol:
            try:
                quote = self.quote(symbol)
                checks["market_quote_available"] = isinstance(quote, dict) and bool(quote)
            except (LiveExecutionError, UpstoxError, ValueError):
                checks["market_quote_available"] = False
        return checks

    @staticmethod
    def _tick_align(value: float, tick: float, *, up: bool) -> float:
        return float(ceil_tick(value, tick)) if up else float(floor_tick(value, tick))

    def submit(
        self,
        *,
        client_order_id: str,
        symbol: str,
        side: str,
        qty: float,
        reference_price: float,
        stop_loss: float | None,
        take_profit: float | None,
        product: str,
        instrument_meta: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if side.upper() != "BUY":
            raise LiveBrokerRejected(
                "exit_route_unsupported: NSE live exits are not routed through the protected-GTT "
                "adapter; exit the position manually in Upstox (no new order endpoint was added)"
            )
        permit = self.gate.permit()
        if permit is None:
            raise LiveGateRefused("NSE live permit is not active")
        permit.validate(self.market, _utc_now(self._clock))
        if stop_loss is None or take_profit is None:
            raise LiveBrokerRejected("NSE live entries require both a protective stop and target")
        inst = self._resolve_instrument(symbol)
        tick = float(inst.tick_size)
        step = int(inst.quantity_step)
        quantity = int(math.floor(float(qty) / step) * step)
        if quantity <= 0:
            raise LiveBrokerRejected(f"quantity {qty} is below one contract step ({step})")
        if inst.freeze_quantity and quantity > int(inst.freeze_quantity):
            raise LiveBrokerRejected(
                f"quantity {quantity} exceeds the NSE freeze quantity {inst.freeze_quantity}"
            )
        limit_price = self._tick_align(float(reference_price), tick, up=False)
        stop_price = self._tick_align(float(stop_loss), tick, up=False)
        target_price = self._tick_align(float(take_profit), tick, up=True)
        if not (stop_price < limit_price < target_price):
            raise LiveBrokerRejected(
                "protective stop/entry/target geometry is invalid after tick alignment"
            )
        order = ProtectedOrder(
            intent_id=client_order_id,
            instrument_key=inst.instrument_key,
            side="BUY",
            quantity=quantity,
            limit_price=limit_price,
            stop_price=stop_price,
            target_price=target_price,
            product="D" if str(product or "D").upper() not in {"I", "D", "MTF"} else str(product).upper(),
            margin_price=limit_price,
        )
        execution_permit = ExecutionPermit(
            session_id=self._session_id,
            kind="ENTRY",
            nonce=permit.nonce,
            expires_at=permit.issued_at + timedelta(seconds=PERMIT_TTL_SECONDS),
        )
        broker = self._broker_for_write()
        try:
            submission = broker.place_protected_entry(
                order,
                execution_permit,
                session_id=self._session_id,
                instrument=inst,
            )
        except AmbiguousSubmission as exc:
            raise LiveSubmissionUncertain(
                f"Upstox acknowledgement is ambiguous ({redact_sensitive_text(str(exc))[:160]})"
            ) from None
        except UpstoxRejected as exc:
            raise LiveBrokerRejected(redact_sensitive_text(str(exc))[:300]) from None
        except ReconciliationFailure as exc:
            raise LiveSubmissionUncertain(
                f"Upstox reconciliation could not confirm the order ({redact_sensitive_text(str(exc))[:160]})"
            ) from None
        except (UpstoxError, PermissionError, ValueError) as exc:
            raise LiveBrokerRejected(
                f"Upstox submission refused ({type(exc).__name__})"
            ) from None
        ids = [str(item) for item in (getattr(submission, "broker_ids", ()) or ())]
        return {
            "client_order_id": client_order_id,
            "broker_order_id": ids[0] if ids else None,
            "broker_order_ids": ids,
            "symbol": inst.trading_symbol,
            "instrument_key": inst.instrument_key,
            "side": "BUY",
            "qty": quantity,
            "order_type": "GTT_MULTIPLE",
            "limit_price": limit_price,
            "stop_price": stop_price,
            "target_price": target_price,
            "status": getattr(submission, "status", "ACKNOWLEDGED_UNRECONCILED"),
            "broker": "upstox",
            "environment": "live",
            "real_money": True,
            "protection": {"ok": True, "mode": "atomic_gtt_multi_leg"},
        }

    def reconcile(self, unresolved: dict[str, str]) -> dict[str, Any]:
        if not unresolved:
            return {"ok": True, "broker": "upstox", "resolved_intents": 0, "adopted_orders": 0}
        broker = self._broker_for_write()
        resolved = 0
        adopted = 0
        for cid in list(unresolved):
            try:
                record = broker.get_gtt(cid)
            except (UpstoxError, ReconciliationFailure) as exc:
                raise LiveReconciliationFailed(
                    f"Upstox GTT reconciliation failed ({type(exc).__name__})"
                ) from None
            if record is not None:
                adopted += 1
            resolved += 1
        return {
            "ok": True,
            "broker": "upstox",
            "resolved_intents": resolved,
            "adopted_orders": adopted,
            "positions": len(self.positions()),
        }

    def cancel(self, client_order_id: str) -> dict[str, Any]:
        permit = self.gate.permit()
        if permit is None:
            raise LiveGateRefused("NSE live permit is not active; refusing to cancel")
        broker = self._broker_for_write()
        execution_permit = ExecutionPermit(
            session_id=self._session_id,
            kind="CANCEL_UNFILLED",
            nonce=permit.nonce,
            expires_at=permit.issued_at + timedelta(seconds=PERMIT_TTL_SECONDS),
        )
        submission = broker.cancel_gtt(client_order_id, execution_permit, session_id=self._session_id)
        return {"ok": True, "broker_ids": list(getattr(submission, "broker_ids", ()) or ())}


# ---------------------------------------------------------------------------
# Coordinator (central risk gate + idempotency + reconciliation)
# ---------------------------------------------------------------------------


@dataclass
class _MarketRuntime:
    blocked: str | None = None
    blocked_detail: str = ""
    last_cycle_at: str | None = None
    last_cycle_status: str = "IDLE"
    last_error: str | None = None
    cycles_completed: int = 0
    submitted_total: int = 0
    rejected_total: int = 0


class LiveExecutionCoordinator:
    """Coordinates independent per-market LIVE execution with a central risk gate."""

    live_trading_enabled_by_default = False

    def __init__(
        self,
        *,
        journal_path: Path | str | None,
        adapters: dict[str, LiveMarketAdapter],
        limits: dict[str, LiveRiskLimits],
        paper_engine: Any = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.journal = LiveMarketOrderJournal(journal_path, clock=self._clock)
        self.adapters = {m.upper(): a for m, a in adapters.items()}
        self.limits = {m.upper(): lim for m, lim in limits.items()}
        self.paper_engine = paper_engine
        self._locks = {m.upper(): threading.RLock() for m in MARKETS}
        self._runtime = {m: _MarketRuntime() for m in MARKETS}
        self._orders: dict[str, dict[str, dict[str, Any]]] = {m: {} for m in MARKETS}
        self._unresolved: dict[str, dict[str, str]] = {m: {} for m in MARKETS}
        self._daily_dates: dict[str, dict[str, str]] = {m: {} for m in MARKETS}
        self._modes = {m: MODE_PAPER for m in MARKETS}
        self._load_persisted_state()
        self._apply_persisted_limits()

    def _apply_persisted_limits(self) -> None:
        try:
            stored = self.journal.load_limits()
        except sqlite3.Error:
            stored = {}
        integer_fields = {"max_daily_orders", "max_open_positions"}
        for mkt, values in stored.items():
            if mkt not in MARKETS:
                continue
            try:
                parsed = {
                    k: (int(float(v)) if k in integer_fields else float(v))
                    for k, v in values.items()
                    if k in LiveRiskLimits.__dataclass_fields__
                }
                self.limits[mkt] = LiveRiskLimits(**parsed)
            except (TypeError, ValueError):
                continue

    def update_limits(self, market: str, **values: Any) -> dict[str, Any]:
        """Persist explicit per-market live limits. Never relaxed implicitly; disarms first."""
        mkt = self._validate_market(market)
        allowed = set(LiveRiskLimits.__dataclass_fields__)
        unknown = sorted(k for k in values if k not in allowed)
        if unknown:
            raise LiveExecutionError(f"Unknown live risk limit(s): {', '.join(unknown)}")
        merged = {**self.limits[mkt].to_dict(), **{k: v for k, v in values.items() if v is not None}}
        merged.pop("configured", None)
        try:
            parsed = {
                k: (int(float(merged[k])) if k in {"max_daily_orders", "max_open_positions"} else float(merged[k]))
                for k in allowed
            }
        except (KeyError, TypeError, ValueError):
            raise LiveExecutionError("Live risk limits must be finite positive numbers") from None
        limits = LiveRiskLimits(**parsed)
        if not limits.configured():
            raise LiveExecutionError(
                "Every live risk limit must be a positive finite value; incomplete limits fail closed"
            )
        with self._locks[mkt]:
            self.adapters[mkt].disarm("risk limits updated")
            self.limits[mkt] = limits
            self.journal.save_limits(mkt, limits.to_dict())
            self.journal.audit(mkt, "LIVE_LIMITS_UPDATED", {"risk_limits": limits.to_dict()})
            return {"market": mkt, "risk_limits": limits.to_dict(),
                    "gate": self.adapters[mkt].gate_status()}

    # -- state -------------------------------------------------------------
    def _validate_market(self, market: str) -> str:
        mkt = str(market or "").strip().upper()
        if mkt not in MARKETS:
            raise LiveExecutionError(f"Unsupported market {market!r}; expected one of {MARKETS}")
        return mkt

    def _load_persisted_state(self) -> None:
        try:
            modes = self.journal.load_modes()
        except sqlite3.Error:
            modes = {}
        for mkt in MARKETS:
            mode = str(modes.get(mkt) or MODE_PAPER).upper()
            self._modes[mkt] = mode if mode in EXECUTION_MODES else MODE_PAPER
            state = self.journal.load_state(mkt)
            self._orders[mkt] = dict(state["orders"])
            self._unresolved[mkt] = dict(state["unresolved"])
            self._daily_dates[mkt] = dict(state["daily_dates"])
            if self._unresolved[mkt]:
                self._runtime[mkt].blocked = "UNRESOLVED_ORDER_INTENT"
                self._runtime[mkt].blocked_detail = (
                    "A previous live submission has an unknown outcome; reconcile before any new order."
                )

    def now(self) -> datetime:
        return _utc_now(self._clock)

    def mode(self, market: str) -> str:
        return self._modes[self._validate_market(market)]

    def is_paper(self, market: str) -> bool:
        return self.mode(market) == MODE_PAPER

    def paper_execution_allowed(self, market: str) -> bool:
        return self.mode(market) != MODE_LIVE

    def set_mode(self, market: str, mode: str) -> dict[str, Any]:
        mkt = self._validate_market(market)
        target = str(mode or "").strip().upper()
        if target not in EXECUTION_MODES:
            raise LiveExecutionError(f"mode must be one of {EXECUTION_MODES}")
        with self._locks[mkt]:
            previous = self._modes[mkt]
            if target != MODE_LIVE:
                # Leaving LIVE always disarms first: never silently keep a live permit.
                self.adapters[mkt].disarm("execution mode changed")
            self._modes[mkt] = target
            self.journal.save_mode(mkt, target)
            self.journal.audit(
                mkt,
                "LIVE_MODE_CHANGED",
                {
                    "previous_mode": previous,
                    "mode": target,
                    "live_disarmed": target != MODE_LIVE,
                    "note": (
                        "LIVE mode selected; Arm + phrase-confirmed Activate are still required "
                        "before any real-money order can be sent"
                        if target == MODE_LIVE
                        else "Local paper routing restored for this market"
                    ),
                },
            )
            return {"market": mkt, "mode": target, "previous_mode": previous,
                    "gate": self.adapters[mkt].gate_status()}

    # -- gate operations ---------------------------------------------------
    def status(self, market: str | None = None) -> dict[str, Any]:
        markets = [self._validate_market(market)] if market else list(MARKETS)
        out: dict[str, Any] = {}
        for mkt in markets:
            adapter = self.adapters[mkt]
            gate = adapter.gate_status()
            runtime = self._runtime[mkt]
            missing = adapter.missing_dependencies()
            out[mkt] = {
                "market": mkt,
                "currency": adapter.currency,
                "broker": adapter.broker_name,
                "destination_label": adapter.destination_label,
                "mode": self._modes[mkt],
                "mode_label": (
                    "LIVE · REAL MONEY" if self._modes[mkt] == MODE_LIVE
                    else ("LIVE-MARKET PAPER · SIMULATED" if self._modes[mkt] == MODE_PAPER
                          else "OFF · NO EXECUTION")
                ),
                "real_money": self._modes[mkt] == MODE_LIVE,
                "gate": gate,
                "configured": not missing,
                "missing_dependencies": missing,
                "risk_limits": self.limits[mkt].to_dict(),
                "blocked": runtime.blocked,
                "blocked_detail": runtime.blocked_detail,
                "unresolved_intents": sorted(self._unresolved[mkt].keys()),
                "live_available": bool(gate.get("live_available")) and self._modes[mkt] == MODE_LIVE,
                "last_cycle_at": runtime.last_cycle_at,
                "last_cycle_status": runtime.last_cycle_status,
                "last_error": runtime.last_error,
                "cycles_completed": runtime.cycles_completed,
                "submitted_total": runtime.submitted_total,
                "rejected_total": runtime.rejected_total,
                "live_confirmation_phrase": LIVE_CONFIRM_PHRASE,
                "broker": capabilities_for(mkt).broker,
                "documented_capabilities_as_of": capabilities_for(mkt).documented_as_of,
            }
        return out if market is None else out[markets[0]]

    def readiness(self, market: str, *, symbol: str | None = None) -> dict[str, Any]:
        mkt = self._validate_market(market)
        adapter = self.adapters[mkt]
        missing = adapter.missing_dependencies()
        checks: dict[str, bool] = {}
        if missing:
            checks["configuration_complete"] = False
        else:
            checks["configuration_complete"] = True
            try:
                checks.update(adapter.readiness_checks(symbol=symbol))
            except (LiveExecutionError, LiveGateRefused, ValueError, KeyError, RuntimeError) as exc:
                checks["readiness_probe"] = False
                checks["readiness_probe_error"] = str(exc)[:120] != ""
        limits = self.limits[mkt]
        checks["risk_limits_configured"] = limits.configured()
        checks["mode_is_live"] = self._modes[mkt] == MODE_LIVE
        checks["no_unresolved_intents"] = len(self._unresolved[mkt]) == 0
        checks["not_blocked"] = self._runtime[mkt].blocked is None
        checks["kill_switch_not_engaged"] = not bool(
            adapter.gate_status().get("kill_engaged")
        )
        failed = sorted(name for name, ok in checks.items() if not ok)
        return {
            "market": mkt,
            "ok": not failed,
            "checks": checks,
            "failed": failed,
            "missing_dependencies": missing,
            "gate": adapter.gate_status(),
            "live_confirmation_phrase": LIVE_CONFIRM_PHRASE,
            "documented_capabilities": capabilities_for(mkt).as_dict(),
        }

    def capabilities(self, market: str | None = None):
        """Documented broker capabilities for one market, or both (reference data only)."""
        if market is None:
            return describe_capabilities(None)
        return describe_capabilities(self._validate_market(market))

    def capability_differences(self) -> dict[str, object]:
        """State how the two brokers differ where it changes behaviour."""
        return idempotency_asymmetry()

    def arm(self, market: str, *, symbol: str | None = None) -> dict[str, Any]:
        mkt = self._validate_market(market)
        report = self.readiness(mkt, symbol=symbol)
        if not report["ok"]:
            raise LiveGateRefused(
                f"{mkt} readiness failed: {', '.join(report['failed'])}"
            )
        with self._locks[mkt]:
            status = self.adapters[mkt].arm(report["checks"])
            self.journal.audit(mkt, "LIVE_GATE_ARMED", {"checks": report["checks"]})
            return {"market": mkt, "gate": status, "readiness": report}

    def activate(self, market: str, *, confirm_phrase: str, acknowledge_risk: bool) -> dict[str, Any]:
        mkt = self._validate_market(market)
        if self._modes[mkt] != MODE_LIVE:
            raise LiveGateRefused(
                f"{mkt} execution mode is {self._modes[mkt]}; set mode LIVE before activating"
            )
        report = self.readiness(mkt)
        if not report["ok"]:
            raise LiveGateRefused(f"{mkt} readiness failed: {', '.join(report['failed'])}")
        with self._locks[mkt]:
            result = self.adapters[mkt].activate(
                confirm_phrase=confirm_phrase,
                acknowledge_risk=acknowledge_risk,
            )
            self.journal.audit(
                mkt,
                "LIVE_GATE_ACTIVATED",
                {"activated_at": _iso(self.now()), "broker": self.adapters[mkt].broker_name},
            )
            return {"market": mkt, "activation": result, "gate": self.adapters[mkt].gate_status()}

    def disarm(self, market: str, reason: str = "operator disarm") -> dict[str, Any]:
        mkt = self._validate_market(market)
        with self._locks[mkt]:
            status = self.adapters[mkt].disarm(reason)
            self.journal.audit(mkt, "LIVE_GATE_DISARMED", {"reason": reason})
            return {"market": mkt, "gate": status}

    def emergency_stop(
        self,
        market: str,
        reason: str = "operator emergency stop",
        *,
        cancel_working: bool = False,
    ) -> dict[str, Any]:
        """Per-market kill switch: blocks that market only; other market is untouched."""
        mkt = self._validate_market(market)
        with self._locks[mkt]:
            canceled: list[str] = []
            cancel_errors: list[str] = []
            if cancel_working:
                for cid, record in list(self._orders[mkt].items()):
                    status = str(record.get("status") or "").lower()
                    if status not in {"new", "accepted", "pending_new", "partially_filled", "submitted", "acknowledged_unreconciled"}:
                        continue
                    try:
                        self.adapters[mkt].cancel(cid)
                        canceled.append(cid)
                    except (LiveExecutionError, UpstoxError, RuntimeError) as exc:
                        cancel_errors.append(f"{cid}: {type(exc).__name__}")
            gate = self.adapters[mkt].engage_kill_switch(reason)
            self.journal.audit(
                mkt,
                "LIVE_EMERGENCY_STOP",
                {
                    "reason": reason,
                    "cancel_working_requested": bool(cancel_working),
                    "canceled_orders": canceled,
                    "cancel_errors": cancel_errors,
                },
            )
            return {
                "ok": True,
                "market": mkt,
                "real_money": True,
                "gate": gate,
                "canceled_orders": canceled,
                "cancel_errors": cancel_errors,
                "other_market_untouched": True,
            }

    def reset_kill_switch(self, market: str, *, operator_ack: bool) -> dict[str, Any]:
        mkt = self._validate_market(market)
        with self._locks[mkt]:
            gate = self.adapters[mkt].reset_kill_switch(operator_ack=operator_ack)
            self.journal.audit(mkt, "LIVE_EMERGENCY_STOP_CLEARED", {"market": mkt})
            return {"ok": True, "market": mkt, "gate": gate}

    def reconcile(self, market: str) -> dict[str, Any]:
        mkt = self._validate_market(market)
        with self._locks[mkt]:
            try:
                result = self.adapters[mkt].reconcile(dict(self._unresolved[mkt]))
            except LiveReconciliationFailed as exc:
                self._runtime[mkt].blocked = "RECONCILIATION_FAILED"
                self._runtime[mkt].blocked_detail = str(exc)[:240]
                self.journal.audit(mkt, "LIVE_RECONCILIATION_FAILED", {"reason": str(exc)[:200]})
                raise
            for cid in list(self._unresolved[mkt]):
                self._unresolved[mkt].pop(cid, None)
                self.journal.clear_unresolved(mkt, cid)
            self._runtime[mkt].blocked = None
            self._runtime[mkt].blocked_detail = ""
            self.journal.audit(
                mkt,
                "LIVE_RECONCILED",
                {
                    "resolved_intents": int(result.get("resolved_intents") or 0),
                    "adopted_orders": int(result.get("adopted_orders") or 0),
                },
            )
            return {"market": mkt, **result}

    # -- risk gate ---------------------------------------------------------
    def _quote_snapshot(
        self, mkt: str, symbol: str, limits: LiveRiskLimits, now: datetime
    ) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []
        try:
            quote = self.adapters[mkt].quote(symbol)
        except (LiveExecutionError, ValueError, RuntimeError) as exc:
            return {
                "ok": False,
                "quote": None,
                "checks": [{"check": "quote_available", "ok": False, "detail": type(exc).__name__}],
            }
        if not isinstance(quote, dict) or not quote:
            return {
                "ok": False,
                "quote": None,
                "checks": [{"check": "quote_available", "ok": False, "detail": "no live quote for symbol"}],
            }
        try:
            bid = float(quote.get("bid") or 0.0)
            ask = float(quote.get("ask") or 0.0)
            last = float(quote.get("last") or quote.get("price") or 0.0)
            stamp = parse_timestamp(
                str(quote.get("source_timestamp") or quote.get("timestamp") or "")
            )
        except (TypeError, ValueError):
            return {
                "ok": False,
                "quote": quote,
                "checks": [{"check": "quote_parsed", "ok": False, "detail": "unparsable quote"}],
            }
        age = (now - stamp.astimezone(timezone.utc)).total_seconds()
        fresh = math.isfinite(age) and -5.0 <= age <= limits.max_quote_age_seconds
        checks.append(
            {
                "check": "quote_freshness",
                "ok": fresh,
                "detail": f"age={age:.1f}s limit={limits.max_quote_age_seconds:.0f}s",
            }
        )
        if not fresh:
            return {"ok": False, "quote": quote, "checks": checks}
        if not (math.isfinite(bid) and math.isfinite(ask) and bid > 0 and ask > 0 and ask >= bid):
            checks.append({"check": "quote_bid_ask", "ok": False, "detail": "invalid bid/ask"})
            return {"ok": False, "quote": quote, "checks": checks}
        if bool(quote.get("is_delayed")):
            checks.append(
                {"check": "quote_entitlement", "ok": False, "detail": "delayed feed cannot authorize LIVE"}
            )
            return {"ok": False, "quote": quote, "checks": checks}
        checks.append({"check": "quote_bid_ask", "ok": True, "detail": f"bid={bid} ask={ask}"})
        mid = (bid + ask) / 2.0
        return {"ok": True, "quote": quote, "bid": bid, "ask": ask, "last": last or mid,
                "mid": mid, "age_seconds": round(max(age, 0.0), 2), "checks": checks}

    def _risk_checks(
        self,
        mkt: str,
        *,
        symbol: str,
        side: str,
        qty: float,
        reference_price: float,
        now: datetime,
    ) -> dict[str, Any]:
        limits = self.limits[mkt]
        checks: list[dict[str, Any]] = []

        def add(name: str, ok: bool, detail: str) -> None:
            checks.append({"check": name, "ok": bool(ok), "detail": str(detail)})

        add("risk_limits_configured", limits.configured(), limits.to_dict())
        if not limits.configured():
            return {"ok": False, "checks": checks}

        add("quantity_positive", math.isfinite(qty) and qty > 0, f"qty={qty}")
        add(
            "reference_price_positive",
            math.isfinite(reference_price) and reference_price > 0,
            f"reference={reference_price}",
        )
        if not (math.isfinite(qty) and qty > 0 and math.isfinite(reference_price) and reference_price > 0):
            return {"ok": False, "checks": checks}

        quote_state = self._quote_snapshot(mkt, symbol, limits, now)
        checks.extend(quote_state["checks"])
        if not quote_state["ok"]:
            return {"ok": False, "checks": checks, "quote": quote_state.get("quote")}
        mid = float(quote_state["mid"])
        deviation_bps = abs(reference_price - mid) / mid * 10_000.0
        add(
            "price_deviation",
            deviation_bps <= limits.max_price_deviation_bps,
            f"{deviation_bps:.1f}bps vs limit {limits.max_price_deviation_bps:.1f}bps",
        )

        notional = qty * reference_price
        add(
            "max_order_value",
            notional <= limits.max_order_value,
            f"notional={notional:.2f} cap={limits.max_order_value:.2f}",
        )

        try:
            account = self.adapters[mkt].account()
        except (LiveExecutionError, UpstoxError, RuntimeError, ValueError) as exc:
            add("account_available", False, f"{type(exc).__name__}")
            return {"ok": False, "checks": checks}
        add("account_available", True, "broker account read")
        add("account_trading_allowed", not bool(account.get("trading_blocked")), "account restrictions")
        day_pnl = float(account.get("day_pnl") or 0.0)
        add(
            "daily_loss_limit",
            math.isfinite(day_pnl) and day_pnl > -abs(limits.max_daily_loss),
            f"day_pnl={day_pnl:.2f} limit=-{limits.max_daily_loss:.2f}",
        )
        buying_power = float(account.get("buying_power") or account.get("available_margin") or 0.0)
        if side.upper() == "BUY":
            add("buying_power", buying_power >= notional, f"power={buying_power:.2f} needed={notional:.2f}")
        try:
            positions = self.adapters[mkt].positions()
        except (LiveExecutionError, UpstoxError, RuntimeError, ValueError) as exc:
            add("positions_available", False, f"{type(exc).__name__}")
            return {"ok": False, "checks": checks}
        positions = [p for p in positions if p]
        total_exposure = sum(abs(float(p.get("market_value") or 0.0)) for p in positions)
        symbol_exposure = sum(
            abs(float(p.get("market_value") or 0.0))
            for p in positions
            if str(p.get("symbol") or "").upper() in {symbol.upper(), str(symbol).replace("|", "-").upper()}
        )
        add(
            "max_symbol_exposure",
            symbol_exposure + notional <= limits.max_symbol_exposure,
            f"symbol={symbol_exposure + notional:.2f} cap={limits.max_symbol_exposure:.2f}",
        )
        add(
            "max_total_exposure",
            total_exposure + notional <= limits.max_total_exposure,
            f"total={total_exposure + notional:.2f} cap={limits.max_total_exposure:.2f}",
        )
        open_positions = len(positions)
        reducing = side.upper() == "SELL"
        add(
            "max_open_positions",
            reducing or open_positions < limits.max_open_positions,
            f"open={open_positions} cap={limits.max_open_positions}",
        )
        today = now.date().isoformat()
        todays = sum(1 for d in self._daily_dates[mkt].values() if d == today)
        add(
            "max_daily_orders",
            todays < limits.max_daily_orders,
            f"today={todays} cap={limits.max_daily_orders}",
        )
        return {
            "ok": all(c["ok"] for c in checks),
            "checks": checks,
            "quote": quote_state.get("quote"),
            "notional": round(notional, 4),
            "account": account,
            "positions": positions,
        }

    # -- submission --------------------------------------------------------
    @staticmethod
    def _client_order_id(mkt: str, symbol: str, bar_timestamp: str, side: str) -> str:
        digest = hashlib.sha256(
            f"{mkt}|{symbol.upper()}|{bar_timestamp}|{side.upper()}|live".encode("utf-8")
        ).hexdigest()[:14]
        safe_symbol = "".join(ch for ch in symbol.upper() if ch.isalnum() or ch in {"-", "_"})[:18]
        return f"live-{mkt.lower()}-{safe_symbol.lower()}-{digest}"

    def submit_decision(
        self,
        market: str,
        decision: dict[str, Any],
        *,
        session_open: bool,
        mode_override: str | None = None,
    ) -> dict[str, Any]:
        mkt = self._validate_market(market)
        symbol = str(decision.get("symbol") or "").strip().upper()
        side = str(decision.get("final_action") or "").strip().upper()
        if side not in {"BUY", "SELL"}:
            return {"market": mkt, "symbol": symbol, "status": "SKIPPED_NO_ACTION", "broker_order_api_calls": 0}
        qty = float(decision.get("selected_qty") or 0.0)
        if qty <= 0:
            return {"market": mkt, "symbol": symbol, "status": "SKIPPED_ZERO_QTY", "broker_order_api_calls": 0}
        if str(mode_override or self._modes[mkt]) != MODE_LIVE:
            return {"market": mkt, "symbol": symbol, "status": "SKIPPED_PAPER_MODE", "broker_order_api_calls": 0}
        gate = self.adapters[mkt].gate_status()
        if str(gate.get("state")) != GateState.ACTIVE.value or not gate.get("live_available"):
            return {
                "market": mkt,
                "symbol": symbol,
                "status": "SKIPPED_GATE_NOT_ACTIVE",
                "gate_state": gate.get("state"),
                "broker_order_api_calls": 0,
            }
        if self._runtime[mkt].blocked:
            return {
                "market": mkt,
                "symbol": symbol,
                "status": "SKIPPED_BLOCKED",
                "blocked": self._runtime[mkt].blocked,
                "broker_order_api_calls": 0,
            }
        if not session_open:
            return {
                "market": mkt,
                "symbol": symbol,
                "status": "SKIPPED_MARKET_CLOSED",
                "broker_order_api_calls": 0,
            }
        bar_ts = str(decision.get("bar_timestamp") or "")
        if not bar_ts:
            return {"market": mkt, "symbol": symbol, "status": "SKIPPED_NO_BAR_TIMESTAMP",
                    "broker_order_api_calls": 0}
        cid = self._client_order_id(mkt, symbol, bar_ts, side)
        reference_price = float(decision.get("reference_price") or 0.0)
        stop_loss = decision.get("stop_loss")
        take_profit = decision.get("take_profit")
        if side == "SELL" and (stop_loss is None or take_profit is None):
            # Naked short is never allowed; only a risk-reducing exit of an existing long.
            try:
                held_positions = self.adapters[mkt].positions()
            except (LiveExecutionError, RuntimeError, ValueError):
                held_positions = []
            existing = [
                p for p in held_positions
                if str(p.get("symbol") or "").upper() == symbol and float(p.get("qty") or 0) > 0
            ]
            held = sum(float(p.get("qty") or 0.0) for p in existing)
            if held < qty:
                self.journal.audit(
                    mkt,
                    "LIVE_RISK_REJECTED",
                    {"symbol": symbol, "side": side, "reason": "naked_short_refused", "held": held},
                )
                return {
                    "market": mkt,
                    "symbol": symbol,
                    "status": "REJECTED_RISK_GATE",
                    "reasons": ["SELL without protective prices requires an existing long position (no naked shorts)"],
                    "broker_order_api_calls": 0,
                }
        with self._locks[mkt]:
            if self.journal.has_order(mkt, cid) or cid in self._orders[mkt]:
                return {
                    "market": mkt,
                    "symbol": symbol,
                    "status": "DUPLICATE_SUPPRESSED",
                    "client_order_id": cid,
                    "broker_order_api_calls": 0,
                }
            now = _utc_now(self._clock)
            gate_check = self._risk_checks(
                mkt, symbol=symbol, side=side, qty=qty, reference_price=reference_price, now=now
            )
            if not gate_check["ok"]:
                reasons = [c["detail"] for c in gate_check["checks"] if not c["ok"]]
                self.journal.audit(
                    mkt,
                    "LIVE_RISK_REJECTED",
                    {"symbol": symbol, "side": side, "client_order_id": cid, "reasons": reasons[:8]},
                )
                self._runtime[mkt].rejected_total += 1
                return {
                    "market": mkt,
                    "symbol": symbol,
                    "status": "REJECTED_RISK_GATE",
                    "client_order_id": cid,
                    "reasons": reasons,
                    "checks": gate_check["checks"],
                    "broker_order_api_calls": 0,
                }
            instrument_meta = None
            if self.paper_engine is not None:
                try:
                    instrument_meta = self.paper_engine._resolve_instrument(mkt, symbol)
                except (AttributeError, TypeError, ValueError, KeyError, RuntimeError):
                    # Instrument metadata is advisory for the adapter; the adapter still
                    # resolves the strict catalog entry before any broker write.
                    instrument_meta = None
            payload = {
                "qty": qty,
                "order_type": "LIMIT" if mkt == "NASDAQ" else "GTT_MULTIPLE",
                "reference_price": reference_price,
                "stop_price": stop_loss,
                "target_price": take_profit,
                "limit_price": reference_price,
            }
            self.journal.record_intent(
                market=mkt,
                client_order_id=cid,
                symbol=symbol,
                side=side,
                payload=payload,
            )
            self._daily_dates[mkt][cid] = now.date().isoformat()
            self.journal.audit(
                mkt,
                "LIVE_ORDER_INTENT",
                {
                    "client_order_id": cid,
                    "symbol": symbol,
                    "side": side,
                    "qty": qty,
                    "reference_price": reference_price,
                    "stop_loss": stop_loss,
                    "take_profit": take_profit,
                    "broker": self.adapters[mkt].broker_name,
                    "decision_id": decision.get("decision_id"),
                    "bar_timestamp": bar_ts,
                },
            )
            try:
                result = self.adapters[mkt].submit(
                    client_order_id=cid,
                    symbol=symbol,
                    side=side.lower(),
                    qty=qty,
                    reference_price=reference_price,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    product=str(decision.get("product") or ("D" if mkt == "NSE" else "CASH")),
                    instrument_meta=instrument_meta,
                )
            except LiveSubmissionUncertain as exc:
                self._unresolved[mkt][cid] = str(exc)
                self._runtime[mkt].blocked = "UNRESOLVED_ORDER_INTENT"
                self._runtime[mkt].blocked_detail = str(exc)[:240]
                self.journal.mark_unresolved(mkt, cid, str(exc))
                self.adapters[mkt].disarm(f"uncertain submission {cid}")
                self.journal.audit(
                    mkt,
                    "LIVE_ORDER_AMBIGUOUS",
                    {"client_order_id": cid, "symbol": symbol, "reason": str(exc)[:240]},
                )
                return {
                    "market": mkt,
                    "symbol": symbol,
                    "status": "SUBMISSION_UNCERTAIN",
                    "client_order_id": cid,
                    "reconcile_required": True,
                    "reasons": [str(exc)[:300]],
                    "broker_order_api_calls": 1,
                }
            except LiveBrokerRejected as exc:
                self.journal.remove_intent(mkt, cid)
                self._daily_dates[mkt].pop(cid, None)
                self._runtime[mkt].rejected_total += 1
                self.journal.audit(
                    mkt,
                    "LIVE_ORDER_REJECTED",
                    {"client_order_id": cid, "symbol": symbol, "reason": str(exc)[:300]},
                )
                return {
                    "market": mkt,
                    "symbol": symbol,
                    "status": "REJECTED_BROKER",
                    "client_order_id": cid,
                    "reasons": [str(exc)[:300]],
                    "broker_order_api_calls": 1,
                }
            except (LiveExecutionError, LiveGateRefused) as exc:
                self.journal.remove_intent(mkt, cid)
                self._daily_dates[mkt].pop(cid, None)
                self.journal.audit(
                    mkt,
                    "LIVE_ORDER_REJECTED",
                    {"client_order_id": cid, "symbol": symbol, "reason": str(exc)[:300]},
                )
                return {
                    "market": mkt,
                    "symbol": symbol,
                    "status": "REJECTED_LOCAL",
                    "client_order_id": cid,
                    "reasons": [str(exc)[:300]],
                    "broker_order_api_calls": 0,
                }
            record = {"real_money": True, **dict(result or {})}
            self.journal.save_order(market=mkt, client_order_id=cid, record=record)
            self._orders[mkt][cid] = record
            self._runtime[mkt].submitted_total += 1
            self.journal.audit(
                mkt,
                "LIVE_ORDER_SUBMITTED",
                {
                    "client_order_id": cid,
                    "broker_order_id": record.get("broker_order_id"),
                    "symbol": symbol,
                    "side": side,
                    "qty": qty,
                    "status": record.get("status"),
                    "protection": record.get("protection"),
                },
            )
            protection = record.get("protection") or {}
            if protection and protection.get("ok") is False:
                self._runtime[mkt].blocked = "PROTECTION_FAILED"
                self._runtime[mkt].blocked_detail = str(protection.get("reason") or "")[:240]
                self.adapters[mkt].disarm("protective stop could not be placed")
                self.journal.audit(
                    mkt,
                    "LIVE_PROTECTION_FAILED",
                    {"client_order_id": cid, "reason": str(protection.get("reason") or "")[:240]},
                )
            return {
                "market": mkt,
                "symbol": symbol,
                "status": "SUBMITTED",
                "client_order_id": cid,
                "broker_order_id": record.get("broker_order_id"),
                "order": record,
                "protection_ok": bool(protection.get("ok", True)),
                "broker_order_api_calls": 2 if mkt == "NASDAQ" else 1,
            }

    # -- market cycle ------------------------------------------------------
    def advance_market(
        self,
        market: str,
        ensemble_service: Any,
        *,
        symbols: list[str] | None = None,
        execute: bool = True,
    ) -> dict[str, Any]:
        mkt = self._validate_market(market)
        runtime = self._runtime[mkt]
        if self._modes[mkt] != MODE_LIVE:
            return {"market": mkt, "status": "SKIPPED_MODE_NOT_LIVE", "mode": self._modes[mkt],
                    "broker_order_api_calls": 0}
        gate = self.adapters[mkt].gate_status()
        if str(gate.get("state")) != GateState.ACTIVE.value or not gate.get("live_available"):
            return {"market": mkt, "status": "SKIPPED_GATE_NOT_ACTIVE", "gate_state": gate.get("state"),
                    "broker_order_api_calls": 0}
        if runtime.blocked:
            return {"market": mkt, "status": "SKIPPED_BLOCKED", "blocked": runtime.blocked,
                    "broker_order_api_calls": 0}
        if ensemble_service is None:
            return {"market": mkt, "status": "SKIPPED_NO_ENSEMBLE", "broker_order_api_calls": 0}
        now = _utc_now(self._clock)
        with self._locks[mkt]:
            if symbols is None and self.paper_engine is not None:
                try:
                    symbols = self.paper_engine._effective_symbols_for_market(mkt)
                except (ValueError, KeyError, RuntimeError):
                    symbols = []
            symbols = [s for s in (symbols or []) if s]
            session_open = self.adapters[mkt].market_open(now)
            try:
                snapshot = (self.paper_engine.market_snapshot(mkt) if self.paper_engine is not None else {})
            except (ValueError, KeyError, RuntimeError):
                snapshot = {}
            if symbols:
                try:
                    cycle = ensemble_service.evaluate_watchlist_cycle(
                        mkt,
                        symbols=symbols,
                        timeframe=(snapshot.get("session") or {}).get("timeframe"),
                        session_open_override=session_open,
                        execute_in_paper=False,
                        persist_decisions=bool(session_open and execute),
                        invoke_kronos=bool(session_open and execute),
                        now_utc=now,
                    )
                except (ValueError, RuntimeError) as exc:
                    runtime.last_cycle_at = _iso(now)
                    runtime.last_cycle_status = "FAILED"
                    runtime.last_error = redact_sensitive_text(str(exc))[:200]
                    self.journal.audit(mkt, "LIVE_CYCLE_FAILED", {"reason": runtime.last_error})
                    return {"market": mkt, "status": "FAILED", "error": runtime.last_error,
                            "broker_order_api_calls": 0}
                decisions = list(cycle.get("decisions") or [])
            else:
                decisions = []
            results = []
            if session_open and execute:
                for dec in decisions:
                    if str(dec.get("final_action") or "") not in {"BUY", "SELL"}:
                        continue
                    results.append(self.submit_decision(mkt, dec, session_open=session_open))
            runtime.cycles_completed += 1
            runtime.last_cycle_at = _iso(now)
            runtime.last_cycle_status = "EXECUTED" if results else "ANALYZED_ONLY"
            runtime.last_error = None
            self.journal.audit(
                mkt,
                "LIVE_MARKET_ANALYSIS_REPORT",
                {
                    "session_open": session_open,
                    "execution_permitted": bool(session_open and execute),
                    "symbols": symbols,
                    "decisions": len(decisions),
                    "submitted": sum(1 for r in results if r["status"] == "SUBMITTED"),
                    "rejected": sum(1 for r in results if r["status"].startswith("REJECTED")),
                    "broker": self.adapters[mkt].broker_name,
                },
            )
            return {
                "market": mkt,
                "status": "EXECUTED" if results else "ANALYZED_ONLY",
                "mode": MODE_LIVE,
                "session_open": session_open,
                "symbols": symbols,
                "decisions": len(decisions),
                "results": results,
                "submitted": sum(1 for r in results if r["status"] == "SUBMITTED"),
                "rejected": sum(1 for r in results if r["status"].startswith("REJECTED")),
                "uncertain": sum(1 for r in results if r["status"] == "SUBMISSION_UNCERTAIN"),
                "broker_order_api_calls": sum(int(r.get("broker_order_api_calls") or 0) for r in results),
            }

    def live_status_summary(self) -> dict[str, Any]:
        return {
            "default_live_execution": "OFF",
            "real_money_enabled": any(
                self._modes[m] == MODE_LIVE and bool(self.adapters[m].gate_status().get("live_available"))
                for m in MARKETS
            ),
            "markets": self.status(),
            "confirmation_phrase": LIVE_CONFIRM_PHRASE,
        }

    def close(self) -> None:
        for mkt in MARKETS:
            try:
                self.adapters[mkt].disarm("application shutdown")
            except (LiveExecutionError, RuntimeError):
                continue
        self.journal.close()


class MultiMarketLiveRunner:
    """Background coordinator for LIVE-mode markets only.

    Each market advances in its own worker with an independent timeout, so a slow
    broker or provider on NSE never blocks NASDAQ (and vice versa). Markets that
    are not explicitly ``LIVE`` with an ``ACTIVE`` gate are skipped with a reason;
    nothing here can enable live trading.
    """

    live_trading_enabled_by_default = False

    def __init__(
        self,
        coordinator: LiveExecutionCoordinator,
        ensemble_service: Any,
        *,
        interval_seconds: float = 15.0,
        market_timeout_seconds: float = 20.0,
        enabled: bool = True,
    ) -> None:
        import asyncio
        from concurrent.futures import ThreadPoolExecutor

        self.coordinator = coordinator
        self.ensemble_service = ensemble_service
        self.interval_seconds = max(1.0, float(interval_seconds))
        self.market_timeout_seconds = max(0.1, float(market_timeout_seconds))
        self.enabled = bool(enabled)
        self._asyncio = asyncio
        self._task: Any = None
        self._stop_event: Any = None
        self._executor = ThreadPoolExecutor(max_workers=len(MARKETS) * 2, thread_name_prefix="axiom-mm-live")
        self._in_flight: dict[str, bool] = {m: False for m in MARKETS}
        self._stats_lock = threading.Lock()
        self._cycles_total = 0
        self._last_tick_at: str | None = None
        self._per_market: dict[str, dict[str, Any]] = {
            m: {"market": m, "cycles_completed": 0, "last_cycle_at": None,
                "last_status": "IDLE", "last_error": None, "in_flight": False}
            for m in MARKETS
        }

    def start(self) -> None:
        if not self.enabled or (self._task is not None and not self._task.done()):
            return
        self._stop_event = self._asyncio.Event()
        self._task = self._asyncio.create_task(self._loop(), name="axiom-multimarket-live-runner")

    async def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except self._asyncio.CancelledError:
                self._stop_event = None
            self._task = None
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _eligible(self) -> list[str]:
        eligible: list[str] = []
        for mkt in MARKETS:
            if self.coordinator.mode(mkt) != MODE_LIVE:
                continue
            gate = self.coordinator.adapters[mkt].gate_status()
            if str(gate.get("state")) != GateState.ACTIVE.value or not gate.get("live_available"):
                continue
            if self.coordinator._runtime[mkt].blocked:
                continue
            eligible.append(mkt)
        return eligible

    async def _advance_one_market(self, mkt: str) -> dict[str, Any]:
        with self._stats_lock:
            if self._in_flight.get(mkt):
                return {"market": mkt, "status": "SKIPPED_IN_FLIGHT", "broker_order_api_calls": 0}
            self._in_flight[mkt] = True
            self._per_market[mkt]["in_flight"] = True
        t0 = time.perf_counter()
        loop = self._asyncio.get_running_loop()
        try:
            result = await self._asyncio.wait_for(
                loop.run_in_executor(
                    self._executor, self.coordinator.advance_market, mkt, self.ensemble_service
                ),
                timeout=self.market_timeout_seconds,
            )
            status = str(result.get("status") or "COMPLETED")
            with self._stats_lock:
                self._per_market[mkt].update(
                    {
                        "cycles_completed": int(self._per_market[mkt]["cycles_completed"]) + 1,
                        "last_cycle_at": _iso(self.coordinator.now()),
                        "last_status": status,
                        "last_error": None,
                    }
                )
            return {**result, "wall_ms": round((time.perf_counter() - t0) * 1000.0, 2)}
        except self._asyncio.TimeoutError:
            err = f"{mkt} live cycle timed out after {self.market_timeout_seconds:.1f}s"
            # The executor thread may still be inside a broker write; the outcome cannot be
            # assumed. Latch the market so no further order can be sent until an operator
            # reconciles against the broker (never guess whether an order was accepted).
            runtime = self.coordinator._runtime[mkt]
            runtime.blocked = "CYCLE_TIMEOUT_RECONCILE_REQUIRED"
            runtime.blocked_detail = (
                "A live cycle exceeded its timeout; broker state is unconfirmed. "
                "Reconcile this market before resuming live routing."
            )
            runtime.last_cycle_at = _iso(self.coordinator.now())
            runtime.last_cycle_status = "TIMEOUT"
            runtime.last_error = err
            self.coordinator.journal.audit(
                mkt,
                "LIVE_CYCLE_TIMEOUT",
                {"timeout_seconds": self.market_timeout_seconds, "reason": err},
            )
            with self._stats_lock:
                self._per_market[mkt].update(
                    {"last_cycle_at": runtime.last_cycle_at,
                     "last_status": "TIMEOUT", "last_error": err}
                )
            return {
                "market": mkt,
                "status": "TIMEOUT",
                "error": err,
                "reconcile_required": True,
                "broker_order_api_calls": None,
                "broker_calls_unknown": True,
            }
        except Exception as exc:  # noqa: BLE001 - one market must never break the other
            err = redact_sensitive_text(f"{type(exc).__name__}: {exc}")[:200]
            with self._stats_lock:
                self._per_market[mkt].update(
                    {"last_cycle_at": _iso(self.coordinator.now()),
                     "last_status": "FAILED", "last_error": err}
                )
            return {"market": mkt, "status": "FAILED", "error": err, "broker_order_api_calls": 0}
        finally:
            with self._stats_lock:
                self._in_flight[mkt] = False
                self._per_market[mkt]["in_flight"] = False

    async def run_once(self, *, markets: list[str] | None = None) -> dict[str, Any]:
        selected = [self.coordinator._validate_market(m) for m in markets] if markets else list(MARKETS)
        eligible = self._eligible()
        targets = [m for m in selected if m in eligible]
        skipped = {
            m: {
                "market": m,
                "status": "SKIPPED_NOT_LIVE_ACTIVE",
                "mode": self.coordinator.mode(m),
                "gate_state": self.coordinator.adapters[m].gate_status().get("state"),
                "broker_order_api_calls": 0,
            }
            for m in selected
            if m not in targets
        }
        t0 = time.perf_counter()
        ran = await self._asyncio.gather(*[self._advance_one_market(m) for m in targets]) if targets else []
        now_iso = _iso(self.coordinator.now())
        with self._stats_lock:
            self._cycles_total += 1
            self._last_tick_at = now_iso
        return {
            "ok": True,
            "execution_mode": "LIVE_MARKET_LIVE",
            "real_money": any(self.coordinator.mode(m) == MODE_LIVE for m in targets),
            "evaluated_at": now_iso,
            "selected_markets": selected,
            "markets_advanced": targets,
            "wall_ms": round((time.perf_counter() - t0) * 1000.0, 2),
            "markets": {**skipped, **{r["market"]: r for r in ran}},
        }

    async def _loop(self) -> None:
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            interval_elapsed = False
            try:
                await self._asyncio.wait_for(self._stop_event.wait(), timeout=self.interval_seconds)
                break
            except self._asyncio.TimeoutError:
                interval_elapsed = True
            if not interval_elapsed or self._stop_event.is_set():
                break
            try:
                await self.run_once()
            except Exception as exc:  # noqa: BLE001 - a failed cycle must not kill the loop
                self.coordinator.journal.audit(
                    "LOCAL", "LIVE_RUNNER_CYCLE_FAILED", {"reason": redact_sensitive_text(str(exc))[:200]}
                )

    def status(self) -> dict[str, Any]:
        with self._stats_lock:
            per_mkt = {k: dict(v) for k, v in self._per_market.items()}
            cycles = self._cycles_total
            last_at = self._last_tick_at
        running = self._task is not None and not self._task.done()
        return {
            "enabled": self.enabled,
            "background_loop_running": running,
            "interval_seconds": self.interval_seconds,
            "market_timeout_seconds": self.market_timeout_seconds,
            "cycles_total": cycles,
            "last_tick_at": last_at,
            "live_trading_enabled_by_default": False,
            "eligible_markets": self._eligible(),
            "markets": per_mkt,
        }
