"""Durable engine state: submission journal, daily risk accounting, recovery.

Everything here is CRASH-SAFETY infrastructure for the sandbox engine:

- SubmissionJournal: a durable intent state machine persisted in engine_state
  BEFORE any remote submission, so a crash can never produce a blind
  duplicate order:
      INTENT_CREATED → DURABLY_PERSISTED → SUBMISSION_ATTEMPTED
      → ACKNOWLEDGED / UNKNOWN → RECONCILED → FINAL_STATE
  An intent in SUBMISSION_ATTEMPTED or UNKNOWN may already exist at the
  broker; it is NEVER resubmitted — only reconciled or surfaced for
  reconciliation.

- DailyRiskAccount: session-aware daily trade/loss accounting in the MARKET
  timezone (settings.market_timezone). Counters reset at the trading-day
  boundary, persist across restarts within the same day, and keep history
  (rollovers are recorded as events; daily snapshots keep the history).

- recover_ledger_state: rebuilds the local ledger (cash, position, average
  entry, realized P&L, fees) by replaying RECORDED fills exactly once.
  Fills come from the idempotent sandbox_transitions audit rows (or
  live_transitions in live mode: ``live=True``), never from both.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# Intent state machine (ordered; the journal records transitions)
INTENT_CREATED = "INTENT_CREATED"
DURABLY_PERSISTED = "DURABLY_PERSISTED"
SUBMISSION_ATTEMPTED = "SUBMISSION_ATTEMPTED"
ACKNOWLEDGED = "ACKNOWLEDGED"
UNKNOWN = "UNKNOWN"
REMOTE_LOOKUP = "REMOTE_LOOKUP"          # 3.0.2: querying broker history by tag
VERIFIED_MATCH = "VERIFIED_MATCH"        # exactly one strict attribute match adopted
VERIFIED_ABSENCE = "VERIFIED_ABSENCE"    # authoritative lookup: order never existed
UNRESOLVED = "UNRESOLVED"                # ambiguous lookup → fail closed (no resubmit)
RECONCILED = "RECONCILED"
FINAL_STATE = "FINAL_STATE"

# States from which a resubmission is FORBIDDEN (the order may exist remotely).
# UNKNOWN/REMOTE_LOOKUP/UNRESOLVED can only be resolved by REMOTE LOOKUP —
# never by a second POST. VERIFIED_ABSENCE is also final for THIS intent id
# (a re-entry must create a NEW intent through the normal sizing/gate path).
NO_RESUBMIT_STATES = {SUBMISSION_ATTEMPTED, UNKNOWN, REMOTE_LOOKUP,
                      VERIFIED_MATCH, VERIFIED_ABSENCE, UNRESOLVED,
                      ACKNOWLEDGED, RECONCILED, FINAL_STATE}

KNOWN_INTENT_STATES = {
    INTENT_CREATED, DURABLY_PERSISTED, SUBMISSION_ATTEMPTED, ACKNOWLEDGED,
    UNKNOWN, REMOTE_LOOKUP, VERIFIED_MATCH, VERIFIED_ABSENCE, UNRESOLVED,
    RECONCILED, FINAL_STATE,
}
ALLOWED_INTENT_TRANSITIONS = {
    INTENT_CREATED: {DURABLY_PERSISTED},
    DURABLY_PERSISTED: {SUBMISSION_ATTEMPTED, FINAL_STATE},
    SUBMISSION_ATTEMPTED: {ACKNOWLEDGED, UNKNOWN, FINAL_STATE},
    ACKNOWLEDGED: {RECONCILED, FINAL_STATE},
    UNKNOWN: {UNKNOWN, REMOTE_LOOKUP, VERIFIED_MATCH, VERIFIED_ABSENCE,
              UNRESOLVED},
    REMOTE_LOOKUP: {UNKNOWN, VERIFIED_MATCH, VERIFIED_ABSENCE, UNRESOLVED},
    VERIFIED_MATCH: {ACKNOWLEDGED, RECONCILED, FINAL_STATE},
    VERIFIED_ABSENCE: set(),
    UNRESOLVED: {REMOTE_LOOKUP, UNRESOLVED},
    RECONCILED: {RECONCILED, FINAL_STATE},
    FINAL_STATE: set(),
}

MAX_JOURNAL_INTENTS = 2000  # legacy compatibility constant; never evict intents


class SubmissionJournal:
    """Permanent, strict per-intent journal backed by its own SQLite table."""

    KEY = "submission_intents"  # legacy engine_state key, migrated once
    LEGACY_MIGRATION_KEY = "submission_intents_migrated_v11"

    def __init__(self, repository):
        self.repository = repository

    @staticmethod
    def _validate_entry(intent_id: str, entry: dict) -> dict:
        import math

        if not isinstance(intent_id, str) or not intent_id.strip():
            raise ValueError("submission journal contains an invalid intent id")
        if not isinstance(entry, dict):
            raise ValueError("submission journal entry is malformed")
        if entry.get("state") not in KNOWN_INTENT_STATES:
            raise ValueError("submission journal contains an unknown state")
        if (not isinstance(entry.get("instrument_key"), str)
                or not entry["instrument_key"].strip()
                or not isinstance(entry.get("symbol"), str)
                or not isinstance(entry.get("side"), str)
                or entry.get("side") not in {"BUY", "SELL"}
                or not isinstance(entry.get("strategy"), str)):
            raise ValueError("submission journal order identity is malformed")
        for name, minimum in (("quantity", 0.0), ("price", 0.0)):
            value = entry.get(name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    or (value <= minimum if name == "quantity" else value < minimum)):
                raise ValueError(f"submission journal {name} is malformed")
        if not isinstance(entry.get("tag"), str) or not entry["tag"]:
            raise ValueError("submission journal tag is malformed")
        order_ids = entry.get("upstox_order_ids")
        if (not isinstance(order_ids, list)
                or any(not isinstance(value, str) or not value for value in order_ids)):
            raise ValueError("submission journal remote order ids are malformed")
        if not isinstance(entry.get("created_ts"), str) or \
                not isinstance(entry.get("updated_ts"), str):
            raise ValueError("submission journal timestamps are malformed")
        if "detail" in entry and not isinstance(entry["detail"], dict):
            raise ValueError("submission journal detail is malformed")
        return entry

    def _migrate_legacy(self) -> None:
        database = getattr(self.repository, "db", None)
        if database is None:
            return
        with database.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            marker_row = db.execute(
                "SELECT payload FROM engine_state WHERE key=?",
                (self.LEGACY_MIGRATION_KEY,)).fetchone()
            if marker_row is not None:
                try:
                    marker = json.loads(marker_row[0])
                except (TypeError, ValueError) as exc:
                    raise ValueError("submission journal migration marker is malformed") from exc
                if marker is not True:
                    raise ValueError("submission journal migration marker is invalid")
                return
            legacy_row = db.execute(
                "SELECT payload FROM engine_state WHERE key=?", (self.KEY,)
            ).fetchone()
            if legacy_row is not None:
                try:
                    legacy = json.loads(legacy_row[0])
                except (TypeError, ValueError) as exc:
                    raise ValueError("legacy submission journal is unreadable") from exc
                if not isinstance(legacy, dict):
                    raise ValueError("legacy submission journal is malformed")
                for intent_id, entry in legacy.items():
                    self._validate_entry(intent_id, entry)
                    payload = json.dumps(entry, allow_nan=False,
                                         separators=(",", ":"))
                    db.execute(
                        "INSERT OR IGNORE INTO submission_intents(intent_id,payload) "
                        "VALUES (?,?)", (intent_id, payload))
            db.execute(
                "INSERT INTO engine_state(key,payload) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload",
                (self.LEGACY_MIGRATION_KEY, "true"))

    def _table_backed(self) -> bool:
        return callable(getattr(self.repository, "list_submission_intents", None))

    def _load(self) -> dict:
        if self._table_backed():
            self._migrate_legacy()
            raw = self.repository.list_submission_intents()
        else:
            raw = self.repository.get_engine_state(self.KEY, {})
            if raw is None:
                raw = {}
        if not isinstance(raw, dict):
            raise ValueError("submission journal is malformed")
        clean = {}
        for intent_id, entry in raw.items():
            clean[intent_id] = self._validate_entry(intent_id, entry)
        return clean

    def _save(self, journal: dict) -> None:
        # Compatibility fallback for test/legacy repository adapters. Never
        # evict an intent: eviction would let a deterministic id be replayed.
        # Production Repository instances use the unbounded keyed SQLite table.
        self.repository.set_engine_state(self.KEY, journal)

    def record(self, intent_id: str, *, instrument_key: str, symbol: str,
               side: str, quantity: float, price: float, strategy: str,
               params_version: str = "", tag: str = "") -> dict:
        """Create and durably persist a new intent before any broker call."""
        if not isinstance(intent_id, str) or not intent_id.strip():
            raise ValueError("submission intent id must be non-empty")
        journal = self._load()
        existing = journal.get(intent_id)
        if existing is not None:
            return existing
        now = datetime.now(timezone.utc).isoformat()
        entry = {
            "state": DURABLY_PERSISTED,
            "instrument_key": instrument_key,
            "symbol": symbol,
            "side": side,
            "quantity": float(quantity),
            "price": float(price),
            "strategy": strategy,
            "params_version": params_version,
            "tag": tag or intent_id[:12],
            "upstox_order_ids": [],
            "created_ts": now,
            "updated_ts": now,
        }
        self._validate_entry(intent_id, entry)
        if self._table_backed():
            inserted = self.repository.insert_submission_intent(intent_id, entry)
            if not inserted:
                # Unique primary key is the cross-thread/process reservation.
                return self.get(intent_id)
        else:
            journal[intent_id] = entry
            self._save(journal)
        self.repository.record_system_event(
            "INTENT_PERSISTED",
            {"intent_id": intent_id, "symbol": symbol, "side": side,
             "quantity": float(quantity), "instrument_key": instrument_key})
        return entry

    def get(self, intent_id: str) -> dict | None:
        if self._table_backed():
            self._migrate_legacy()
            entry = self.repository.get_submission_intent(intent_id)
            return self._validate_entry(intent_id, entry) if entry is not None else None
        return self._load().get(intent_id)

    def mark(self, intent_id: str, state: str, *, detail: dict | None = None,
             upstox_order_ids: list | None = None) -> dict | None:
        if state not in KNOWN_INTENT_STATES:
            raise ValueError("unknown submission journal state")
        entry = self.get(intent_id)
        if entry is None:
            return None
        previous = entry.get("state")
        if state not in ALLOWED_INTENT_TRANSITIONS.get(previous, set()):
            raise ValueError(f"invalid submission journal transition {previous} -> {state}")
        updated = dict(entry)
        updated["state"] = state
        updated["updated_ts"] = datetime.now(timezone.utc).isoformat()
        if upstox_order_ids is not None:
            updated["upstox_order_ids"] = list(upstox_order_ids)
        if detail:
            if not isinstance(detail, dict):
                raise ValueError("submission journal detail must be an object")
            updated["detail"] = detail
        self._validate_entry(intent_id, updated)
        if self._table_backed():
            changed = self.repository.update_submission_intent(
                intent_id, updated, expected_state=previous)
            if not changed:
                raise RuntimeError("submission journal changed concurrently")
        else:
            journal = self._load()
            journal[intent_id] = updated
            self._save(journal)
        self.repository.record_system_event(
            "INTENT_STATE", {"intent_id": intent_id, "state": state,
                             **(detail or {})})
        return updated

    def resubmission_forbidden(self, intent_id: str) -> bool:
        entry = self.get(intent_id)
        return entry is not None and entry.get("state") in NO_RESUBMIT_STATES

    def unresolved(self) -> dict:
        """Return intents whose remote fate is not yet certain."""
        return {intent_id: entry
                for intent_id, entry in self._load().items()
                if entry.get("state") in {SUBMISSION_ATTEMPTED, UNKNOWN,
                                          REMOTE_LOOKUP, UNRESOLVED}}


class DailyRiskAccount:
    """Trading-day-scoped counters (trades, realized P&L) in the market tz.

    Persisted in engine_state as {"date", "trades", "realized_pnl"}; a new
    trading day starts clean while rollovers and daily snapshots keep the
    history. Restart-safe within the same trading day.
    """

    KEY = "daily_risk_state"

    def __init__(self, repository, market_timezone: str = "Asia/Kolkata"):
        self.repository = repository
        try:
            self._tz = ZoneInfo(market_timezone)
        except Exception:
            self._tz = ZoneInfo("Asia/Kolkata")  # documented default session
        raw = repository.get_engine_state(self.KEY, {}) or {}
        if not isinstance(raw, dict):
            raise ValueError("daily risk state is malformed")
        now = datetime.now(timezone.utc)
        self.date: str = str(raw.get("date", ""))
        self.trades: int = int(raw.get("trades", 0))
        self.realized_pnl: float = float(raw.get("realized_pnl", 0.0))
        cutover = raw.get("fill_accounting_cutover_ts")
        self._new_fill_accounting_cutover = cutover is None
        if cutover is None:
            self.fill_accounting_cutover_ts = now.isoformat()
        else:
            if not isinstance(cutover, str):
                raise ValueError("daily fill-accounting cutover is malformed")
            try:
                parsed_cutover = datetime.fromisoformat(
                    cutover.strip().replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("daily fill-accounting cutover is unreadable") from exc
            if parsed_cutover.tzinfo is None or parsed_cutover.utcoffset() is None:
                raise ValueError("daily fill-accounting cutover has no timezone")
            self.fill_accounting_cutover_ts = parsed_cutover.astimezone(
                timezone.utc).isoformat()
        self.rollover(now=now)
        if self._new_fill_accounting_cutover and self.date == self.trading_date(now):
            self._persist()

    def trading_date(self, now: datetime) -> str:
        return now.astimezone(self._tz).date().isoformat()

    def rollover(self, *, now: datetime) -> bool:
        """Reset counters when the market-tz trading day changed."""
        today = self.trading_date(now)
        if self.date == today:
            return False
        if self.date:
            self.repository.record_system_event(
                "DAILY_STATE_ROLLOVER",
                {"previous_date": self.date, "new_date": today,
                 "trades": self.trades, "realized_pnl": self.realized_pnl})
        self.date, self.trades, self.realized_pnl = today, 0, 0.0
        self._persist()
        return True

    def register_trade(self, *, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        self.rollover(now=now)
        self.trades += 1
        self._persist()

    def add_realized(self, pnl: float, *, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        self.rollover(now=now)
        self.realized_pnl += float(pnl)
        self._persist()

    def record_realized_fill(self, fill_id: str, pnl: float, *,
                             observed_at: str | None,
                             now: datetime | None = None) -> bool:
        """Credit one reconciled SELL fill atomically and idempotently.

        The per-fill key, credited/baseline classification, and daily aggregate
        commit together. A broker observation predating the persisted cutover
        is recorded for audit but never re-counted as today's realized P&L.
        """
        if not isinstance(fill_id, str) or not fill_id:
            raise ValueError("realized fill has no stable identity")
        pnl = float(pnl)
        if not math.isfinite(pnl):
            raise ValueError("realized fill P&L is not finite")
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("daily-risk clock must include a timezone")
        now = now.astimezone(timezone.utc)
        self.rollover(now=now)
        observed_ts = None
        observed = None
        if observed_at is not None:
            if not isinstance(observed_at, str) or not observed_at.strip():
                raise ValueError("realized fill observation time is malformed")
            try:
                observed = datetime.fromisoformat(
                    observed_at.strip().replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("realized fill observation time is unreadable") from exc
            if observed.tzinfo is None or observed.utcoffset() is None:
                raise ValueError("realized fill observation time has no timezone")
            observed = observed.astimezone(timezone.utc)
            observed_ts = observed.isoformat()
        today = self.trading_date(now)
        credited = bool(
            observed is not None
            and observed >= datetime.fromisoformat(
                self.fill_accounting_cutover_ts)
            and self.trading_date(observed) == today
        )
        encoded_id = fill_id
        with self.repository.db.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT payload FROM engine_state WHERE key=?", (self.KEY,)
            ).fetchone()
            if row is None:
                raise ValueError("daily risk state disappeared during fill accounting")
            try:
                persisted = json.loads(row["payload"])
            except (TypeError, ValueError) as exc:
                raise ValueError("daily risk state became unreadable") from exc
            if not isinstance(persisted, dict):
                raise ValueError("daily risk state became malformed")
            if (persisted.get("date") != today
                    or persisted.get("fill_accounting_cutover_ts")
                    != self.fill_accounting_cutover_ts):
                raise ValueError("daily risk state changed during fill accounting")
            try:
                persisted_pnl = float(persisted.get("realized_pnl", 0.0))
                persisted_trades = int(persisted.get("trades", 0))
            except (TypeError, ValueError) as exc:
                raise ValueError("daily risk counters became malformed") from exc
            if not math.isfinite(persisted_pnl) or persisted_trades < 0:
                raise ValueError("daily risk counters became invalid")
            inserted = conn.execute(
                "INSERT OR IGNORE INTO daily_risk_fills "
                "(fill_id, observed_ts, trading_date, realized_pnl, credited) "
                "VALUES (?, ?, ?, ?, ?)",
                (encoded_id, observed_ts, today, pnl, int(credited)),
            )
            if inserted.rowcount != 1:
                self.date = today
                self.trades = persisted_trades
                self.realized_pnl = persisted_pnl
                return False
            if credited:
                persisted_pnl += pnl
                persisted.update({"date": today, "trades": persisted_trades,
                                  "realized_pnl": persisted_pnl,
                                  "fill_accounting_cutover_ts":
                                      self.fill_accounting_cutover_ts})
                conn.execute(
                    "UPDATE engine_state SET payload=? WHERE key=?",
                    (json.dumps(persisted, sort_keys=True, allow_nan=False),
                     self.KEY),
                )
        if credited:
            self.date = today
            self.trades = persisted_trades
            self.realized_pnl = persisted_pnl
        return True

    def state(self) -> dict:
        return {"date": self.date, "trades": self.trades,
                "realized_pnl": round(self.realized_pnl, 6),
                "fill_accounting_cutover_ts":
                    self.fill_accounting_cutover_ts}

    def _persist(self) -> None:
        self.repository.set_engine_state(self.KEY, self.state())


def reconciled_fill_id(order_id: str, child_order_id: str | None,
                       cumulative_quantity: float) -> str:
    """Stable identity for one newly observed cumulative child-fill delta."""
    identity = "\0".join((str(order_id), str(child_order_id or ""),
                           float(cumulative_quantity).hex()))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def equity_state_key(live: bool) -> str:
    """Engine-state key holding the current equity snapshot for the mode."""
    return "live_equity" if live else "sandbox_equity"


def read_equity_state(repository) -> dict:
    """Live equity when a live engine recorded it, otherwise sandbox equity.

    Prefers the LIVE snapshot so an operator reading the dashboard or the NL
    command center can never be shown a stale sandbox equity as the current
    real-money account state.
    """
    if repository is None:
        return {}
    for key in ("live_equity", "sandbox_equity"):
        state = repository.get_engine_state(key)
        if state:
            return state
    return {}


def recover_ledger_state(ledger, repository, *, live: bool = False) -> dict:
    """Rebuild ledger account state from RECORDED fills (exact, idempotent).

    3.0.3 per-child replay: every recorded transition carries the child
    (slice) order id. Each child's cumulative filled quantity is walked in
    recorded order and applied as INCREMENTS priced at the actual incremental
    basis -- (f_now*avg_now - f_prev*avg_prev)/(f_now - f_prev) -- never by
    re-applying a cumulative average to a whole delta. The logical fill of a
    sliced order is the SUM over its children, and CANCELLED rows carrying an
    executed quantity are included, so a partial-then-cancelled child keeps
    its credit across restarts. Deterministic: transitions were recorded
    exactly once (per-child identity), so replay can never double-count.
    """
    if live:
        orders_by_id = {order["id"]: order
                        for order in repository.list_live_orders(limit=2000)}
    else:
        orders_by_id = {order["id"]: order
                        for order in repository.list_sandbox_orders(limit=2000)}
    transition_table = "live_transitions" if live else "sandbox_transitions"
    with repository.db.transaction() as conn:
        rows = conn.execute(
            "SELECT order_id, upstox_order_id, status, filled_quantity, "
            f"average_price, ts, raw, rowid AS rid FROM {transition_table} "
            "WHERE filled_quantity IS NOT NULL AND filled_quantity > 0 "
            "ORDER BY ts, rowid").fetchall()
    # per child (logical order id + child order id): chronological increments
    per_child: dict = {}
    for row in rows:
        key = (row["order_id"], row["upstox_order_id"])
        per_child.setdefault(key, []).append(dict(row))
    increments: list = []
    skipped = 0
    for (order_id, _child_id), child_rows in per_child.items():
        order = orders_by_id.get(order_id)
        if order is None:
            skipped += 1  # cannot reconstruct without a recorded order row
            continue
        prev_filled, prev_avg = 0.0, 0.0
        for row in child_rows:  # already ordered by (ts, rowid)
            filled = float(row["filled_quantity"] or 0)
            average = float(row["average_price"] or 0)
            if filled <= prev_filled:
                continue  # already reflected in the baseline (idempotent)
            delta = filled - prev_filled
            if prev_filled > 0 and average > 0:
                price = (filled * average - prev_filled * prev_avg) / delta
                if price < 0:  # anomalous remote basis; never fabricate one
                    price = average
            else:
                price = average if average > 0 else prev_avg
            increments.append({"ts": row["ts"], "rid": row["rid"],
                               "order_id": order_id,
                               "child_id": _child_id,
                               "cumulative_filled": filled,
                               "raw": row["raw"],
                               "side": order["side"], "delta": delta,
                               "price": price})
            prev_filled = filled
            prev_avg = average if average > 0 else prev_avg
    increments.sort(key=lambda inc: (inc["ts"], inc["rid"]))
    replayed_fills = []
    for inc in increments:
        realized_before = ledger.realized_pnl
        ledger.apply_fill(inc["side"], inc["delta"], inc["price"], 0.0)
        realized_delta = ledger.realized_pnl - realized_before
        if inc["side"] == "SELL":
            try:
                raw = json.loads(inc["raw"])
            except (TypeError, ValueError):
                raw = {}
            replayed_fills.append({
                "fill_id": reconciled_fill_id(
                    inc["order_id"], inc["child_id"], inc["cumulative_filled"]),
                "order_id": inc["order_id"], "child_order_id": inc["child_id"],
                "side": "SELL", "quantity": inc["delta"],
                "price": inc["price"], "realized_pnl": realized_delta,
                "observed_at": (raw.get("_axiom_observed_at")
                                if isinstance(raw, dict) else None),
            })
    touched_orders = {row["order_id"] for key_rows in per_child.values()
                      for row in key_rows if row["order_id"] in orders_by_id}
    return {"orders_replayed": len(touched_orders),
            "orders_skipped": skipped,
            "child_orders_replayed": len(per_child),
            "increments_replayed": len(increments),
            "replayed_fills": replayed_fills,
            "cash": ledger.cash,
            "position_quantity": ledger.position_quantity,
            "position_avg_price": ledger.position_avg_price,
            "realized_pnl": ledger.realized_pnl,
            "fees": ledger.fees}
