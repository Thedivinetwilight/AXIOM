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

MAX_JOURNAL_INTENTS = 2000  # bounded memory on engine_state


class SubmissionJournal:
    """Durable per-intent state machine backed by engine_state (SQLite)."""

    KEY = "submission_intents"

    def __init__(self, repository):
        self.repository = repository

    def _load(self) -> dict:
        raw = self.repository.get_engine_state(self.KEY, {}) or {}
        return raw if isinstance(raw, dict) else {}

    def _save(self, journal: dict) -> None:
        # keep only the most recent intents (bounded)
        if len(journal) > MAX_JOURNAL_INTENTS:
            ordered = sorted(journal.items(),
                             key=lambda kv: kv[1].get("updated_ts", ""))
            for key, _ in ordered[: len(journal) - MAX_JOURNAL_INTENTS]:
                journal.pop(key, None)
        self.repository.set_engine_state(self.KEY, journal)

    def record(self, intent_id: str, *, instrument_key: str, symbol: str,
               side: str, quantity: float, price: float, strategy: str,
               params_version: str = "", tag: str = "") -> dict:
        """Create + durably persist the intent BEFORE any submission.

        `tag` is the durable intent-derived broker reference (3.0.2): it is
        persisted HERE, before the POST, and is the key used by REMOTE_LOOKUP
        to find a remotely-accepted order after a lost response.
        """
        journal = self._load()
        existing = journal.get(intent_id)
        if existing is not None:
            return existing  # already journaled (idempotent)
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
            "created_ts": datetime.now(timezone.utc).isoformat(),
            "updated_ts": datetime.now(timezone.utc).isoformat(),
        }
        journal[intent_id] = entry
        self._save(journal)
        self.repository.record_system_event(
            "INTENT_PERSISTED",
            {"intent_id": intent_id, "symbol": symbol, "side": side,
             "quantity": float(quantity), "instrument_key": instrument_key})
        return entry

    def get(self, intent_id: str) -> dict | None:
        return self._load().get(intent_id)

    def mark(self, intent_id: str, state: str, *, detail: dict | None = None,
             upstox_order_ids: list | None = None) -> dict | None:
        journal = self._load()
        entry = journal.get(intent_id)
        if entry is None:
            return None
        entry["state"] = state
        entry["updated_ts"] = datetime.now(timezone.utc).isoformat()
        if upstox_order_ids is not None:
            entry["upstox_order_ids"] = list(upstox_order_ids)
        if detail:
            entry["detail"] = detail
        self._save(journal)
        self.repository.record_system_event(
            "INTENT_STATE", {"intent_id": intent_id, "state": state,
                             **(detail or {})})
        return entry

    def resubmission_forbidden(self, intent_id: str) -> bool:
        entry = self.get(intent_id)
        return entry is not None and entry.get("state") in NO_RESUBMIT_STATES

    def unresolved(self) -> dict:
        """Intents whose remote fate is not yet certain (never resubmitted).

        Includes UNKNOWN / REMOTE_LOOKUP / UNRESOLVED so reconciliation can
        resolve them via the documented tag lookup (3.0.2).
        """
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
        self.date: str = str(raw.get("date", ""))
        self.trades: int = int(raw.get("trades", 0))
        self.realized_pnl: float = float(raw.get("realized_pnl", 0.0))
        self.rollover(now=datetime.now(timezone.utc))

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

    def state(self) -> dict:
        return {"date": self.date, "trades": self.trades,
                "realized_pnl": round(self.realized_pnl, 6)}

    def _persist(self) -> None:
        self.repository.set_engine_state(self.KEY, self.state())


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
            f"average_price, ts, rowid AS rid FROM {transition_table} "
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
                               "side": order["side"], "delta": delta,
                               "price": price})
            prev_filled = filled
            prev_avg = average if average > 0 else prev_avg
    increments.sort(key=lambda inc: (inc["ts"], inc["rid"]))
    for inc in increments:
        ledger.apply_fill(inc["side"], inc["delta"], inc["price"], 0.0)
    touched_orders = {row["order_id"] for key_rows in per_child.values()
                      for row in key_rows if row["order_id"] in orders_by_id}
    return {"orders_replayed": len(touched_orders),
            "orders_skipped": skipped,
            "child_orders_replayed": len(per_child),
            "increments_replayed": len(increments),
            "cash": ledger.cash,
            "position_quantity": ledger.position_quantity,
            "position_avg_price": ledger.position_avg_price,
            "realized_pnl": ledger.realized_pnl,
            "fees": ledger.fees}
