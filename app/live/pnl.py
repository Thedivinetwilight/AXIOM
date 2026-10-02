"""Live audit-trail PnL: an INDEPENDENT second opinion on what happened.

The engine's in-memory ledger is the number everything else trusts. If it ever
diverges from what the broker actually did — a dropped fill, a duplicated
delta, a restart that replayed the wrong table — every downstream decision is
wrong in a way that looks plausible.

This module recomputes realized PnL from the raw audit rows alone
(``live_transitions`` + ``live_orders``) using the same per-child incremental
basis rule that reconciliation uses, then lets the caller compare that
independent figure with the ledger's own ``realized_pnl``. A mismatch beyond a
tolerance is a genuine alarm: two implementations disagreeing about money.

Scope and honesty:

- fees are **not** part of the recorded transitions, so the audit figure is
  explicitly *pre-fee*; the comparison reports the fee total separately rather
  than pretending it is included;
- the sandbox and live audit tables are never mixed (``live=True/False``);
- nothing here writes, submits, cancels or amends anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Position:
    quantity: float = 0.0
    average_price: float = 0.0


@dataclass
class ReplayResult:
    fills: list[dict] = field(default_factory=list)
    realized_pnl: float = 0.0          # pre-fee, from the audit trail alone
    turnover: float = 0.0              # sum of |delta| * price
    buys: int = 0
    sells: int = 0
    per_symbol: dict = field(default_factory=dict)
    open_positions: dict = field(default_factory=dict)  # symbol -> qty/avg
    first_ts: str | None = None
    last_ts: str | None = None
    orders_replayed: int = 0

    def realized_series(self) -> list[tuple[str, float]]:
        """Cumulative realized PnL after each SELL (for a chart/report)."""
        series, running = [], 0.0
        for fill in self.fills:
            if fill["side"] == "SELL":
                running += fill["realized"]
                series.append((fill["ts"], running))
        return series

    def to_dict(self) -> dict:
        return {
            "realized_pnl_pre_fee": round(self.realized_pnl, 6),
            "turnover": round(self.turnover, 2),
            "fills": len(self.fills), "buys": self.buys, "sells": self.sells,
            "orders_replayed": self.orders_replayed,
            "first_ts": self.first_ts, "last_ts": self.last_ts,
            "per_symbol": self.per_symbol,
            "open_positions": self.open_positions,
            "note": "recomputed from the audit trail only; pre-fee by construction",
        }


def _order_rows(repository, live: bool) -> dict:
    listed = (repository.list_live_orders(limit=5000) if live
              else repository.list_sandbox_orders(limit=5000))
    return {order["id"]: order for order in listed}


def _transition_rows(repository, live: bool) -> list[dict]:
    table = "live_transitions" if live else "sandbox_transitions"
    with repository.db.transaction() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT order_id, upstox_order_id, status, filled_quantity, "
            f"average_price, ts, rowid AS rid FROM {table} "
            "WHERE filled_quantity IS NOT NULL AND filled_quantity > 0 "
            "ORDER BY ts, rowid").fetchall()]


def replay_fills(repository, *, live: bool = True) -> ReplayResult:
    """Recompute fills and realized PnL from the audit rows alone.

    Per-``(order, child)`` incremental basis: a child that fills 5 then 5 more
    contributes two deltas at their own incremental prices, so a
    partial-then-cancelled child credits exactly its executed quantity — the
    same invariant the engine's reconciliation guarantees.
    """
    result = ReplayResult()
    if repository is None:
        return result
    orders = _order_rows(repository, live)
    rows = _transition_rows(repository, live)

    per_child: dict = {}
    for row in rows:
        key = (row["order_id"], row["upstox_order_id"])
        per_child.setdefault(key, []).append(row)

    positions: dict[str, Position] = {}
    increments: list[dict] = []
    for (order_id, _child), child_rows in per_child.items():
        order = orders.get(order_id)
        if order is None:
            continue  # cannot attribute a fill without its order row
        prev_filled, prev_avg = 0.0, 0.0
        for row in child_rows:
            filled = float(row["filled_quantity"] or 0)
            average = float(row["average_price"] or 0)
            if filled <= prev_filled:
                continue  # already reflected in this child's baseline
            delta = filled - prev_filled
            if prev_filled > 0 and average > 0:
                price = (filled * average - prev_filled * prev_avg) / delta
                if price < 0:
                    price = average  # anomalous remote basis: never fabricate
            else:
                price = average if average > 0 else prev_avg
            increments.append({
                "ts": str(row["ts"]), "rid": row["rid"], "delta": delta,
                "price": price, "side": str(order["side"]).upper(),
                "symbol": order.get("symbol") or "UNKNOWN",
                "order_id": order_id,
            })
            prev_filled = filled
            prev_avg = average if average > 0 else prev_avg
        result.orders_replayed += 1

    increments.sort(key=lambda inc: (inc["ts"], inc["rid"]))
    for inc in increments:
        symbol = inc["symbol"]
        position = positions.setdefault(symbol, Position())
        bucket = result.per_symbol.setdefault(
            symbol, {"buys": 0, "sells": 0, "realized": 0.0, "turnover": 0.0,
                     "fills": 0})
        price, delta = inc["price"], inc["delta"]
        result.turnover += abs(delta) * price
        bucket["turnover"] += abs(delta) * price
        bucket["fills"] += 1
        realized = 0.0
        if inc["side"] == "BUY":
            new_qty = position.quantity + delta
            if new_qty > 0:
                position.average_price = (
                    (position.average_price * position.quantity + delta * price)
                    / new_qty)
            position.quantity = new_qty
            result.buys += 1
            bucket["buys"] += 1
        else:
            realized = (price - position.average_price) * delta
            result.realized_pnl += realized
            bucket["realized"] += realized
            position.quantity -= delta
            if position.quantity <= 1e-12:
                position.quantity, position.average_price = 0.0, 0.0
            result.sells += 1
            bucket["sells"] += 1
        result.fills.append({
            "ts": inc["ts"], "symbol": symbol, "side": inc["side"],
            "quantity": delta, "price": price, "realized": realized,
            "order_id": inc["order_id"]})
        result.first_ts = result.first_ts or inc["ts"]
        result.last_ts = inc["ts"]

    for symbol, position in positions.items():
        if position.quantity > 1e-12:
            result.open_positions[symbol] = {
                "quantity": position.quantity,
                "average_price": round(position.average_price, 6)}
    for symbol, bucket in result.per_symbol.items():
        bucket["realized"] = round(bucket["realized"], 6)
        bucket["turnover"] = round(bucket["turnover"], 2)
    return result


def compare_with_ledger(replay: ReplayResult, ledger, *, tolerance: float = 1.0,
                        fees_recorded: float | None = None) -> dict:
    """Compare the independent audit figure with the engine's own ledger.

    ``tolerance`` is an absolute currency amount: small differences are normal
    (rounding, a fill applied a moment ago), but a difference larger than the
    tolerance means the two implementations disagree about money and must be
    investigated before trusting either number.
    """
    ledger_realized = float(getattr(ledger, "realized_pnl", 0.0))
    delta = ledger_realized - replay.realized_pnl
    return {
        "ledger_realized_pnl": round(ledger_realized, 6),
        "audit_realized_pnl_pre_fee": round(replay.realized_pnl, 6),
        "fees_recorded": (None if fees_recorded is None
                          else round(float(fees_recorded), 6)),
        "difference": round(delta, 6),
        "tolerance": float(tolerance),
        "match": abs(delta) <= float(tolerance),
        "explanation": (
            "audit replay and ledger agree within tolerance" if abs(delta) <= tolerance
            else "ledger and audit-trail PnL disagree: investigate before trading"),
    }
