#!/usr/bin/env python3
"""Weekly trading summary: trades, net P&L after costs, win rate, max drawdown, errors.

Reads the sandbox order ledger - it places no order and constructs no broker.

Round trips are paired FIFO per symbol: a BUY opens a lot, a SELL closes as
much of the oldest open lot as it can. P&L is per closed round trip, net of a
per-side cost. Max drawdown is measured on the realised-equity curve, i.e. the
cumulative net P&L as round trips complete, which is the only curve this ledger
can honestly reconstruct.

    python tools/weekly_summary.py --db data/arena.db
    python tools/weekly_summary.py --db data/arena.db --days 7 --cost-per-side 0.0005

Unmatched orders (a SELL with no open lot, a BUY never closed) are reported,
never silently dropped or assumed profitable.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    # created_ts comes back from the repository as a STRING of epoch
    # milliseconds, so the numeric case must be tested after coercion.
    text = str(value).strip()
    if text.lstrip("-").isdigit():
        return datetime.fromtimestamp(int(text) / 1000, tz=timezone.utc)
    try:
        text = text.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _load_orders(db_path: str, since: datetime | None, limit: int) -> list[dict]:
    from app.database.database import Database
    from app.database.repository import Repository

    db = Database(f"sqlite:///{db_path}")
    try:
        repo = Repository(db)
        orders = repo.list_sandbox_orders(limit=limit)
    finally:
        db.close()

    if since is None:
        return orders
    kept = []
    for order in orders:
        created = _parse_ts(order.get("created_ts"))
        if created is not None and created >= since:
            kept.append(order)
    return kept


def _pair_round_trips(orders, cost_rate: float):
    """FIFO pairing per symbol. Returns (closed, unmatched, errors)."""
    open_lots: dict[str, deque] = {}
    closed: list[dict] = []
    unmatched: list[dict] = []
    errors: list[str] = []

    for order in sorted(orders, key=lambda o: _parse_ts(o.get("created_ts")) or datetime.min.replace(tzinfo=timezone.utc)):
        symbol = order.get("symbol") or "?"
        side = (order.get("side") or "").upper()
        status = (order.get("status") or "").upper()
        quantity = float(order.get("quantity") or 0.0)
        price = float(order.get("price") or 0.0)

        if status in {"REJECTED", "CANCELLED"}:
            errors.append(f"{symbol} {side} {quantity}@{price}: terminal status {status}")
            continue
        if quantity <= 0:
            errors.append(f"{symbol} {side}: non-positive quantity {quantity}")
            continue
        if price <= 0:
            errors.append(f"{symbol} {side} qty={quantity}: non-positive price {price} "
                          "(market orders record 0; P&L for these is not computable)")
            continue

        if side == "BUY":
            open_lots.setdefault(symbol, deque()).append(
                {"qty": quantity, "price": price, "order": order})
        elif side in {"SELL", "EXIT"}:
            remaining = quantity
            lots = open_lots.setdefault(symbol, deque())
            while remaining > 1e-9 and lots:
                lot = lots[0]
                take = min(lot["qty"], remaining)
                gross = (price - lot["price"]) * take
                cost = (price + lot["price"]) * take * cost_rate
                closed.append({
                    "symbol": symbol, "qty": take,
                    "entry": lot["price"], "exit": price,
                    "gross": gross, "cost": cost, "net": gross - cost,
                    "exit_ts": _parse_ts(order.get("created_ts")),
                })
                lot["qty"] -= take
                remaining -= take
                if lot["qty"] <= 1e-9:
                    lots.popleft()
            if remaining > 1e-9:
                unmatched.append(f"{symbol} SELL {remaining}@{price}: no open lot to close")
        else:
            errors.append(f"{symbol}: unrecognised side {side!r}")

    for symbol, lots in open_lots.items():
        for lot in lots:
            unmatched.append(f"{symbol} BUY {lot['qty']}@{lot['price']}: still open at period end")
    return closed, unmatched, errors


def _max_drawdown(cumulative: list[float]) -> tuple[float, float]:
    """Returns (max_drawdown_abs, peak) over the realised-equity curve."""
    peak = 0.0
    worst = 0.0
    for value in cumulative:
        peak = max(peak, value)
        worst = min(worst, value - peak)
    return worst, peak


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", required=True, help="path to the arena sqlite database")
    parser.add_argument("--days", type=int, default=7, help="lookback window (default 7)")
    parser.add_argument("--cost-per-side", type=float, default=0.0005,
                        help="fractional cost applied per side (default 0.0005 = 5bps)")
    parser.add_argument("--limit", type=int, default=2000, help="max orders to read")
    args = parser.parse_args(argv)

    if not Path(args.db).exists():
        print(f"database not found: {args.db}")
        return 2

    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    orders = _load_orders(args.db, since, args.limit)

    print(f"window : last {args.days} days (since {since:%Y-%m-%d %H:%M} UTC)")
    print(f"orders : {len(orders)}")
    if not orders:
        print("\nno orders in window. Nothing to summarise - this is not a zero-P&L week,")
        print("it is an absence of activity, and the two must not be confused.")
        return 0

    closed, unmatched, errors = _pair_round_trips(orders, args.cost_per_side)

    print("\n=== round trips ===")
    print(f"closed   : {len(closed)}")
    print(f"unmatched: {len(unmatched)}")
    print(f"errors   : {len(errors)}")

    if closed:
        gross = sum(t["gross"] for t in closed)
        cost = sum(t["cost"] for t in closed)
        net = sum(t["net"] for t in closed)
        wins = [t for t in closed if t["net"] > 0]
        losses = [t for t in closed if t["net"] <= 0]

        cumulative = []
        running = 0.0
        for trade in sorted(closed, key=lambda t: t["exit_ts"] or datetime.min.replace(tzinfo=timezone.utc)):
            running += trade["net"]
            cumulative.append(running)
        drawdown, peak = _max_drawdown(cumulative)

        print("\n=== P&L ===")
        print(f"gross          : {gross:>14.2f}")
        print(f"costs          : {cost:>14.2f}  ({args.cost_per_side * 10000:.1f}bps per side)")
        print(f"NET            : {net:>14.2f}")
        print(f"peak realised  : {peak:>14.2f}")
        print(f"max drawdown   : {drawdown:>14.2f}")
        print("\n=== win rate ===")
        print(f"wins / losses  : {len(wins)} / {len(losses)}")
        print(f"win rate       : {len(wins) / len(closed) * 100:.1f}%")
        if wins:
            print(f"avg win        : {sum(t['net'] for t in wins) / len(wins):>14.2f}")
        if losses:
            print(f"avg loss       : {sum(t['net'] for t in losses) / len(losses):>14.2f}")
    else:
        print("\nno closed round trips in window - win rate and drawdown are undefined,")
        print("not zero. Reporting them as 0 would be a fabrication.")

    if unmatched:
        print("\n=== unmatched (needs attention) ===")
        for line in unmatched[:20]:
            print(f"  {line}")
        if len(unmatched) > 20:
            print(f"  ... and {len(unmatched) - 20} more")

    if errors:
        print("\n=== errors ===")
        for line in errors[:20]:
            print(f"  {line}")
        if len(errors) > 20:
            print(f"  ... and {len(errors) - 20} more")

    return 0


if __name__ == "__main__":
    sys.exit(main())
