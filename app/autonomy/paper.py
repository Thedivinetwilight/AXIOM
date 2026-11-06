"""Live-market PAPER execution: local simulation with no broker mutation path.

This module deliberately has no HTTP client, Upstox order URL, token provider,
or live broker dependency.  Prices come from the authoritative completed-bar
stream supplied by the session engine; every fill is local and labeled PAPER.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import copy
import hashlib
import math

from app.autonomy.models import (
    AccountProfile, AccountSnapshot, ExecutionPermit, Instrument, Position,
    ProtectedOrder, Submission, validate_order_route,
)
from app.data.models import Candle, parse_timestamp


@dataclass
class PaperPosition:
    instrument_key: str
    quantity: float
    average_price: float
    last_price: float
    realised: float
    stop_price: float
    target_price: float
    protection_id: str
    correlation_group: str
    entry_commission: float = 0.0
    entry_time: datetime | None = None
    regime: str = ""


class PaperExecution:
    broker_mutations_reachable = False
    authoritative = False

    def __init__(self, instruments: dict[str, Instrument], *, starting_equity: float,
                 commission_rate: float, slippage_rate: float, spread_rate: float,
                 max_participation: float, rejection_rate: float = 0.0):
        if starting_equity <= 0:
            raise ValueError("Paper starting equity must be positive")
        if not 0 <= rejection_rate <= 1:
            raise ValueError("Paper rejection rate must be between zero and one")
        self.instruments = instruments
        self.starting_equity = float(starting_equity)
        self.cash = float(starting_equity)
        self.day_realised = 0.0
        self.commission_rate = commission_rate
        self.slippage_rate = slippage_rate
        self.spread_rate = spread_rate
        self.max_participation = max_participation
        self.rejection_rate = rejection_rate
        self.rejections = 0
        self.trade_log: list[dict] = []
        self.positions: dict[str, PaperPosition] = {}
        self.gtt: dict[str, dict] = {}
        self.intent_for_id: dict[str, str] = {}
        self._now = datetime.now(timezone.utc)

    def snapshot(self) -> dict:
        """Return a JSON-safe durable copy; no credentials or permits exist here."""
        positions = []
        for position in self.positions.values():
            raw = asdict(position)
            raw["entry_time"] = position.entry_time.isoformat() if position.entry_time else ""
            positions.append(raw)
        return {
            "schema": "axiom.autonomy.paper-state.v1",
            "instrument_keys": sorted(self.instruments),
            "starting_equity": self.starting_equity,
            "cash": self.cash,
            "day_realised": self.day_realised,
            "commission_rate": self.commission_rate,
            "slippage_rate": self.slippage_rate,
            "spread_rate": self.spread_rate,
            "max_participation": self.max_participation,
            "rejection_rate": self.rejection_rate,
            "rejections": self.rejections,
            "trade_log": copy.deepcopy(self.trade_log),
            "positions": positions,
            "gtt": copy.deepcopy(self.gtt),
            "intent_for_id": dict(self.intent_for_id),
            "now": self._now.isoformat(),
        }

    @classmethod
    def from_snapshot(cls, instruments: dict[str, Instrument], snapshot: dict) -> "PaperExecution":
        """Restore a halted paper runtime, rejecting incomplete/inconsistent state."""
        if not isinstance(snapshot, dict) or snapshot.get("schema") != "axiom.autonomy.paper-state.v1":
            raise ValueError("Unsupported or missing durable paper state")
        if set(snapshot.get("instrument_keys") or []) != set(instruments):
            raise ValueError("Durable paper instruments do not match the recovered session")
        paper = cls(
            instruments,
            starting_equity=float(snapshot["starting_equity"]),
            commission_rate=float(snapshot["commission_rate"]),
            slippage_rate=float(snapshot["slippage_rate"]),
            spread_rate=float(snapshot["spread_rate"]),
            max_participation=float(snapshot["max_participation"]),
            rejection_rate=float(snapshot["rejection_rate"]),
        )
        paper.cash = float(snapshot["cash"])
        paper.day_realised = float(snapshot["day_realised"])
        if not all(math.isfinite(value) for value in (paper.cash, paper.day_realised)):
            raise ValueError("Durable paper cash/P&L is invalid")
        paper.rejections = int(snapshot["rejections"])
        paper.trade_log = copy.deepcopy(snapshot["trade_log"])
        paper.gtt = copy.deepcopy(snapshot["gtt"])
        paper.intent_for_id = dict(snapshot["intent_for_id"])
        paper._now = parse_timestamp(snapshot["now"])
        for raw in snapshot["positions"]:
            values = dict(raw)
            values["entry_time"] = parse_timestamp(values["entry_time"]) if values.get("entry_time") else None
            position = PaperPosition(**values)
            if position.instrument_key not in instruments:
                raise ValueError("Durable paper position has an unknown instrument")
            paper.positions[position.instrument_key] = position
        for identifier, record in paper.gtt.items():
            if record.get("instrument_token") not in instruments:
                raise ValueError("Durable paper GTT has an unknown instrument")
            if identifier not in paper.intent_for_id:
                raise ValueError("Durable paper GTT lacks its intent mapping")
        for position in paper.positions.values():
            if position.protection_id not in paper.gtt:
                raise ValueError("Durable paper position lacks protection state")
        paper.account().validate()
        return paper

    def _check_permit(self, permit: ExecutionPermit | None, kind: str,
                      session_id: str | None = None) -> None:
        if permit is None or permit.kind != kind:
            raise PermissionError("Paper execution requires the session's explicit in-memory permit")
        permit.validate(session_id or permit.session_id, self._now)

    def profile(self) -> AccountProfile:
        return AccountProfile(
            active=True,
            exchanges=frozenset({"NSE", "BSE", "CDS", "BCD"}),
            products=frozenset({"I"}), order_types=frozenset({"LIMIT", "SL", "SL-M"}),
            user_fingerprint="local-paper-no-account",
        )

    def margin_for(self, order: ProtectedOrder) -> float:
        instrument = self.instruments[order.instrument_key]
        # No leverage claim: paper reserves the full simulated notional.  Live
        # mode instead requires Upstox's current account-specific margin API.
        return order.quantity * (order.margin_price or order.limit_price) * float(instrument.contract_multiplier or 0)

    def place_protected_entry(self, order: ProtectedOrder,
                              permit: ExecutionPermit, *, session_id: str | None = None,
                              instrument: Instrument | None = None) -> Submission:
        self._check_permit(permit, "ENTRY", session_id)
        validate_order_route(order, instrument or self.instruments[order.instrument_key])
        if order.intent_id in self.intent_for_id.values():
            raise ValueError("Duplicate paper intent")
        if order.instrument_key in self.positions:
            raise ValueError("Paper pyramiding is disabled per instrument")
        if any(record["instrument_token"] == order.instrument_key and
               record["rules"][0]["status"] in {"OPEN", "PENDING", "SCHEDULED"}
               for record in self.gtt.values()):
            raise ValueError("An unfilled paper entry already exists for this instrument")
        instrument = self.instruments[order.instrument_key]
        instrument.validate()
        if order.quantity <= 0 or order.quantity % instrument.quantity_step:
            raise ValueError("Paper quantity does not match the exact contract step")
        identifier = "PAPER-GTT-" + hashlib.sha256(order.intent_id.encode()).hexdigest()[:16]
        record = {
            "gtt_order_id": identifier,
            "type": "MULTIPLE",
            "instrument_token": order.instrument_key,
            "quantity": order.quantity,
            "product": order.product,
            "simulated": True,
            "research_regime": "",
            "rules": [
                {"strategy": "ENTRY", "status": "OPEN", "trigger_type": "IMMEDIATE",
                 "trigger_price": order.limit_price, "transaction_type": order.side,
                 "order_id": "PAPER-ENTRY-" + identifier[-12:]},
                {"strategy": "STOPLOSS", "status": "INACTIVE", "trigger_type": "IMMEDIATE",
                 "trigger_price": order.stop_price,
                 "transaction_type": "SELL" if order.side == "BUY" else "BUY",
                 "order_id": None},
                {"strategy": "TARGET", "status": "INACTIVE", "trigger_type": "IMMEDIATE",
                 "trigger_price": order.target_price,
                 "transaction_type": "SELL" if order.side == "BUY" else "BUY",
                 "order_id": None},
            ],
        }
        self.gtt[identifier] = record
        self.intent_for_id[identifier] = order.intent_id
        return Submission((identifier,), "ACKNOWLEDGED_UNRECONCILED",
                          {"simulated": True, "paper_only": True})

    def get_gtt(self, identifier: str) -> dict | None:
        return self.gtt.get(identifier)

    def modify_stop(self, identifier: str, new_stop: float,
                    permit: ExecutionPermit, *, session_id: str | None = None,
                    instrument: Instrument) -> Submission:
        self._check_permit(permit, "PROTECTIVE", session_id)
        record = self.gtt.get(identifier)
        if not record:
            raise ValueError("Paper protection not found")
        entry = next(rule for rule in record["rules"] if rule["strategy"] == "ENTRY")
        stop = next(rule for rule in record["rules"] if rule["strategy"] == "STOPLOSS")
        old = float(stop["trigger_price"])
        side = entry["transaction_type"]
        if (side == "BUY" and new_stop <= old) or (side == "SELL" and new_stop >= old):
            raise ValueError("Paper stop modification would loosen or repeat the stop")
        if stop["status"] in {"COMPLETED", "CANCELLED", "FAILED"}:
            raise ValueError("Paper protection is no longer active")
        stop["trigger_price"] = new_stop
        position = self.positions.get(record["instrument_token"])
        if position:
            position.stop_price = new_stop
        return Submission((identifier,), "MODIFIED_UNRECONCILED", {"simulated": True})

    def cancel_gtt(self, identifier: str, permit: ExecutionPermit, *,
                   session_id: str | None = None) -> Submission:
        self._check_permit(permit, "CANCEL_UNFILLED", session_id)
        record = self.gtt.get(identifier)
        if not record:
            raise ValueError("Paper GTT not found")
        if record["instrument_token"] in self.positions:
            raise ValueError("Refusing to remove protection from an open paper position")
        for rule in record["rules"]:
            if rule["status"] not in {"COMPLETED", "FAILED"}:
                rule["status"] = "CANCELLED"
        return Submission((identifier,), "CANCELLED_UNRECONCILED", {"simulated": True})

    def orders(self) -> list[dict]:
        return list(self.gtt.values())

    @staticmethod
    def _steps(value: float, step: int) -> int:
        return int(max(0.0, value) // step) * step

    def _should_reject(self, record: dict, bar: Candle) -> bool:
        if self.rejection_rate <= 0:
            return False
        digest = hashlib.sha256(
            f"{record['gtt_order_id']}|{bar.timestamp.isoformat()}".encode()
        ).digest()
        return int.from_bytes(digest[:8], "big") / 2**64 < self.rejection_rate

    def _fill_entry(self, record: dict, bar: Candle, instrument: Instrument) -> None:
        entry = record["rules"][0]
        if entry["status"] not in {"OPEN", "PENDING", "SCHEDULED"}:
            return
        side = entry["transaction_type"]
        limit = float(entry["trigger_price"])
        adverse = (self.slippage_rate + self.spread_rate / 2) * (1 if side == "BUY" else -1)
        touched = bar.low <= limit <= bar.high
        if not touched:
            return
        if self._should_reject(record, bar):
            self.rejections += 1
            entry["status"] = "FAILED"
            entry["message"] = "PAPER deterministic broker rejection model"
            for rule in record["rules"][1:]:
                rule["status"] = "CANCELLED"
            return
        raw = bar.open if (side == "BUY" and bar.open <= limit) or \
            (side == "SELL" and bar.open >= limit) else limit
        fill_price = raw * (1 + adverse)
        if (side == "BUY" and fill_price > limit) or (side == "SELL" and fill_price < limit):
            return  # realistic limit/slippage rejection on this bar; do not invent a fill
        capacity = self._steps(bar.volume * self.max_participation, instrument.quantity_step)
        if capacity < instrument.quantity_step:
            entry["status"] = "FAILED"
            entry["message"] = "PAPER insufficient bar liquidity"
            for rule in record["rules"][1:]:
                rule["status"] = "CANCELLED"
            return
        requested = record["quantity"]
        quantity = min(requested, capacity)
        commission = quantity * fill_price * float(instrument.contract_multiplier or 0) \
            * self.commission_rate
        signed = quantity if side == "BUY" else -quantity
        self.cash -= signed * fill_price * float(instrument.contract_multiplier or 0) + commission
        self.positions[instrument.instrument_key] = PaperPosition(
            instrument_key=instrument.instrument_key, quantity=signed,
            average_price=fill_price, last_price=bar.close, realised=-commission,
            stop_price=float(record["rules"][1]["trigger_price"]),
            target_price=float(record["rules"][2]["trigger_price"]),
            protection_id=record["gtt_order_id"], correlation_group=instrument.correlation_group,
            entry_commission=commission, entry_time=bar.timestamp,
            regime=record.get("research_regime", ""),
        )
        self.day_realised -= commission
        record["quantity"] = quantity
        entry["status"] = "COMPLETED" if quantity == requested else "PARTIALLY_FILLED"
        entry["filled_quantity"] = quantity
        entry["average_price"] = fill_price
        for rule in record["rules"][1:]:
            rule["status"] = "SCHEDULED"

    def _exit_position(self, position: PaperPosition, bar: Candle,
                       instrument: Instrument, reason: str, trigger: float) -> None:
        side = "SELL" if position.quantity > 0 else "BUY"
        adverse = (self.slippage_rate + self.spread_rate / 2) * (1 if side == "SELL" else -1)
        fill_price = trigger * (1 + adverse)
        capacity = self._steps(bar.volume * self.max_participation, instrument.quantity_step)
        if capacity < instrument.quantity_step:
            return  # protection remains working; never assume a fill/flat account
        quantity = min(abs(position.quantity), capacity)
        signed_exit = quantity if side == "BUY" else -quantity
        multiplier = float(instrument.contract_multiplier or 0)
        gross = (fill_price - position.average_price) * position.quantity * multiplier
        # Scale gross for partial exits; position.quantity is signed.
        gross *= quantity / abs(position.quantity)
        commission = quantity * fill_price * multiplier * self.commission_rate
        self.cash += -signed_exit * fill_price * multiplier - commission
        pnl = gross - commission
        fraction = quantity / abs(position.quantity)
        allocated_entry_commission = position.entry_commission * fraction
        position.entry_commission -= allocated_entry_commission
        self.day_realised += pnl
        position.realised += pnl
        self.trade_log.append({
            "instrument_key": position.instrument_key,
            "side": "BUY" if position.quantity > 0 else "SELL",
            "quantity": quantity, "entry_price": position.average_price,
            "exit_price": fill_price, "entry_time": position.entry_time.isoformat()
            if position.entry_time else "", "exit_time": bar.timestamp.isoformat(),
            "gross": gross, "commission": commission + allocated_entry_commission,
            "net": pnl - allocated_entry_commission, "reason": reason,
            "regime": position.regime,
        })
        remaining = abs(position.quantity) - quantity
        record = self.gtt[position.protection_id]
        if remaining < instrument.quantity_step:
            del self.positions[position.instrument_key]
            for rule in record["rules"]:
                if rule["strategy"] in {"STOPLOSS", "TARGET"}:
                    rule["status"] = "COMPLETED" if rule["strategy"] == reason else "CANCELLED"
        else:
            position.quantity = math.copysign(remaining, position.quantity)
            record["quantity"] = remaining
            record["rules"][0]["status"] = "PARTIALLY_FILLED"

    def _protect(self, position: PaperPosition, bar: Candle, instrument: Instrument) -> None:
        long = position.quantity > 0
        stop, target = position.stop_price, position.target_price
        stop_hit = bar.open <= stop or bar.low <= stop if long else bar.open >= stop or bar.high >= stop
        target_hit = bar.open >= target or bar.high >= target if long else bar.open <= target or bar.low <= target
        # Conservative same-bar ordering: stop first.  A gap through a stop is
        # filled at the worse opening price; a target gap earns only the target.
        if stop_hit:
            trigger = bar.open if (long and bar.open < stop) or (not long and bar.open > stop) else stop
            self._exit_position(position, bar, instrument, "STOPLOSS", trigger)
        elif target_hit:
            self._exit_position(position, bar, instrument, "TARGET", target)

    def on_bar(self, bar: Candle) -> None:
        instrument = self.instruments.get(bar.symbol.replace("-", "|", 1))
        if instrument is None:
            # Exact display-symbol lookup avoids assuming that every dash is a
            # segment separator in a trading symbol.
            instrument = next((item for item in self.instruments.values()
                               if item.display_symbol == bar.symbol), None)
        if instrument is None:
            raise ValueError(f"Unselected paper instrument {bar.symbol}")
        self._now = bar.timestamp
        for record in list(self.gtt.values()):
            if record["instrument_token"] == instrument.instrument_key:
                self._fill_entry(record, bar, instrument)
        position = self.positions.get(instrument.instrument_key)
        if position is not None:
            self._protect(position, bar, instrument)
        position = self.positions.get(instrument.instrument_key)
        if position is not None:
            position.last_price = bar.close

    def close_all_for_research(self, bar: Candle) -> None:
        """Explicit end-of-backtest mark; never called by the live session."""
        for position in list(self.positions.values()):
            instrument = self.instruments[position.instrument_key]
            self._exit_position(position, bar, instrument, "RESEARCH_END", bar.close)

    def account(self) -> AccountSnapshot:
        positions = []
        unrealised = 0.0
        used_margin = 0.0
        for position in self.positions.values():
            instrument = self.instruments[position.instrument_key]
            multiplier = float(instrument.contract_multiplier or 0)
            pnl = (position.last_price - position.average_price) * position.quantity * multiplier
            unrealised += pnl
            used_margin += abs(position.quantity * position.average_price * multiplier)
            positions.append(Position(
                instrument_key=position.instrument_key, quantity=position.quantity,
                average_price=position.average_price, last_price=position.last_price,
                multiplier=multiplier, realised=position.realised, unrealised=pnl, stop_price=position.stop_price,
                target_price=position.target_price, protection_id=position.protection_id,
                correlation_group=position.correlation_group,
            ))
        equity = self.cash + sum(
            p.quantity * p.last_price * float(self.instruments[p.instrument_key].contract_multiplier or 0)
            for p in self.positions.values()
        )
        available = max(0.0, equity - used_margin)
        return AccountSnapshot(equity, available, used_margin, self.day_realised + unrealised,
                               tuple(positions), datetime.now(timezone.utc), False)

    def public(self) -> dict:
        return {
            "paper_only": True,
            "simulated": True,
            "broker_mutations_reachable": False,
            "account": self.account().__dict__ | {
                "positions": [p.public() for p in self.account().positions],
                "observed_at": self.account().observed_at.isoformat(),
            },
            "orders": self.orders(),
            "rejections": self.rejections,
            "trade_log": list(self.trade_log),
            "note": "Local simulated fills from authoritative Upstox bars; no broker order route exists",
        }
