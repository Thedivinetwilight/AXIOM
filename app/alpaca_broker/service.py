"""Alpaca brokerage service: gated order lifecycle, risk, audit, recovery.

Orchestrates one explicit environment (Alpaca PAPER or Alpaca LIVE) behind the
LIVE gate. Every action is journaled intent-before-write with redacted audit
events; every failure path is fail-closed. Local PAPER (PaperBroker) is a
separate execution destination and is never touched from here.

Safety contract enforced in code:
- preview before every submission; risk limits must be configured (all > 0)
- duplicate prevention via mandatory ``client_order_id`` + journal + broker
  lookup (restart-safe: an existing broker order is adopted, never re-posted)
- LIVE submissions require gate ACTIVE (a LivePermit) in addition to preview
- failed reconciliation, uncertain recovery, or the kill switch block orders
- account mode and endpoint host are visible in every status/preview/order view
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import uuid

from app.alpaca_broker.client import (
    AlpacaTradingClient,
    BrokerError,
    BrokerRejected,
    utc_now,
)
from app.alpaca_broker.gate import BrokerLiveGate, GateError
from app.data.alpaca import validate_us_symbol

OPEN_STATUSES = {"new", "accepted", "pending_new", "accepted_for_bidding",
                 "pending_cancel", "pending_replace", "partially_filled", "held"}
TERMINAL_STATUSES = {"filled", "canceled", "expired", "rejected", "replaced", "stopped",
                     "suspended", "done_for_day", "calculated"}


@dataclass
class BrokerRiskLimits:
    """All limits are mandatory and must be positive; missing limits fail closed."""

    max_order_value: float = 0.0
    max_open_positions: int = 0
    max_daily_orders: int = 0

    def configured(self) -> bool:
        return (
            self.max_order_value > 0
            and self.max_open_positions > 0
            and self.max_daily_orders > 0
        )

    def to_dict(self) -> dict:
        return {
            "max_order_value": self.max_order_value,
            "max_open_positions": self.max_open_positions,
            "max_daily_orders": self.max_daily_orders,
            "configured": self.configured(),
        }


class BrokerStateBlocked(RuntimeError):
    """The service refuses order actions until an explicit recovery/reconcile."""


class AlpacaBrokerService:
    def __init__(
        self,
        client: AlpacaTradingClient | None,
        *,
        environment: str = "paper",
        limits: BrokerRiskLimits,
        repository=None,
        gate: BrokerLiveGate | None = None,
        asset_lookup=None,
        client_factory=None,
    ):
        self.client = client
        self.environment = environment
        self.limits = limits
        self.repository = repository
        self.gate = gate if gate is not None else BrokerLiveGate()
        self.asset_lookup = asset_lookup  # optional callable(symbol) -> metadata dict
        self.client_factory = client_factory  # (LivePermit) -> AlpacaTradingClient (LIVE)
        self.orders: dict[str, dict] = {}       # client_order_id -> record
        self.fills: list[dict] = []
        self.positions: dict[str, dict] = {}
        self.account: dict | None = None
        self.blocked: str | None = None
        self.last_reconcile: dict | None = None
        self.recovered_at: str | None = None
        self._daily_order_dates: dict[str, str] = {}

    def _require_client(self) -> AlpacaTradingClient:
        if self.client is None:
            raise BrokerStateBlocked(
                "broker client unavailable (credentials not configured, or LIVE "
                "requires explicit Arm + Activate first)"
            )
        return self.client

    # -- visibility ------------------------------------------------------------

    def destination(self) -> dict:
        from app.alpaca_broker.client import LIVE_HOST, PAPER_HOST

        is_live = self.environment == "live"
        return {
            "mode": "ALPACA_PAPER" if not is_live else "ALPACA_LIVE",
            "environment": self.environment,
            "endpoint_host": LIVE_HOST if is_live else PAPER_HOST,
            "is_live": is_live,
            "is_real_money": is_live,
            "destination_label": (
                "ALPACA PAPER · broker-simulated fills · no real money"
                if not is_live
                else "ALPACA LIVE · REAL MONEY · broker-executed orders"
            ),
            "gate": self.gate.status(),
            "risk_limits": self.limits.to_dict(),
            "blocked": self.blocked,
            "client_bound": self.client is not None,
            "last_reconcile": self.last_reconcile,
            "recovered_at": self.recovered_at,
        }

    def status(self) -> dict:
        return {
            **self.destination(),
            "account": dict(self.account) if self.account else None,
            "orders": [self._order_view(o) for o in self.orders.values()],
            "fills": list(self.fills),
            "positions": [dict(p) for p in self.positions.values()],
        }

    @staticmethod
    def _order_view(record: dict) -> dict:
        return {k: v for k, v in record.items() if not k.startswith("_")}

    def _audit(self, kind: str, **fields) -> None:
        if self.repository is None:
            return
        destination = self.destination()
        self.repository.record_system_event(kind, {
            "simulated": self.environment == "paper",
            "execution_mode": destination["mode"],
            "environment": self.environment,
            "endpoint_host": destination["endpoint_host"],
            **fields,
        })

    # -- startup / recovery ----------------------------------------------------

    def recover(self) -> dict:
        """Rebuild state from the broker; uncertainty blocks order actions."""
        client = self._require_client()
        try:
            self.account = client.get_account()
            open_orders = client.get_orders(status="open")
            positions = client.get_positions()
        except (BrokerError, ValueError) as exc:
            self.blocked = "RECOVERY_UNCERTAIN"
            self._audit("ALPACA_BROKER_RECOVERY_FAILED", reason=type(exc).__name__)
            return {"ok": False, "reason": "recovery_uncertain", "blocked": self.blocked}
        for raw in open_orders:
            record = self._record_from_broker(raw, source="broker-adopted")
            self.orders[record["client_order_id"]] = record
        self.positions = {
            str(p.get("symbol")): {
                "symbol": p.get("symbol"),
                "qty": p.get("qty"),
                "side": p.get("side"),
                "avg_entry_price": p.get("avg_entry_price"),
                "unrealized_pl": p.get("unrealized_pl"),
            }
            for p in positions
        }
        self.blocked = None
        self.recovered_at = utc_now()
        self._audit("ALPACA_BROKER_RECOVERED", open_orders=len(open_orders),
                    positions=len(self.positions))
        return {"ok": True, "adopted_orders": len(open_orders),
                "positions": len(self.positions), "account": self.account}

    # -- readiness (LIVE gate input) ---------------------------------------------

    def readiness_checks(self) -> dict:
        """All must pass before LIVE can be armed. Any uncertainty is False."""
        checks = {
            "broker_enabled": True,
            "risk_limits_configured": self.limits.configured(),
            "kill_switch_not_engaged": not self.gate.kill_engaged,
            "service_unblocked": self.blocked is None,
            "reconciliation_ok": self.last_reconcile is None or bool(self.last_reconcile.get("ok")),
        }
        try:
            client = self._require_client()
            self.account = client.get_account()
            account = self.account
            checks["account_verified"] = True
            checks["account_trading_allowed"] = not (
                account.get("trading_blocked") or account.get("account_blocked")
                or account.get("trade_suspended_by_user")
            )
        except BrokerError:
            checks["account_verified"] = False
            checks["account_trading_allowed"] = False
        if self.asset_lookup is not None:
            checks["instrument_validation_available"] = True
        return checks

    # -- gate binding (LIVE) --------------------------------------------------------

    def activate_live(self) -> dict:
        """Arm must already have happened; mints the permit and binds the LIVE client."""
        permit = self.gate.activate()
        if self.client_factory is not None:
            self.client = self.client_factory(permit)
        elif self.client is None or self.client.environment != "live":
            raise BrokerStateBlocked("no LIVE client factory configured; refusing activation")
        self._audit("ALPACA_LIVE_ACTIVATED", permit_issued_at=permit.issued_at)
        return {"active": True, "permit_issued_at": permit.issued_at,
                "destination": self.destination()}

    def disarm(self, reason: str = "operator disarm") -> dict:
        result = self.gate.disarm(reason)
        if self.environment == "live":
            self.client = None  # a live client never survives disarm
        self._audit("ALPACA_GATE_DISARMED", reason=reason)
        return result

    # -- preview / risk checks -------------------------------------------------

    def preview_order(self, *, symbol: str, side: str, qty=None, notional=None,
                      order_type: str = "market", time_in_force: str = "day",
                      limit_price=None, stop_price=None) -> dict:
        checks: list[dict] = []

        def check(name: str, ok: bool, detail: str) -> None:
            checks.append({"check": name, "ok": bool(ok), "detail": detail})

        try:
            symbol = validate_us_symbol(symbol)
            check("instrument_symbol", True, f"{symbol} is a valid US ticker format")
        except ValueError as exc:
            check("instrument_symbol", False, str(exc))
            symbol = str(symbol)
        if self.asset_lookup is not None:
            try:
                asset = self.asset_lookup(symbol)
                check("instrument_listing", True,
                      f"{symbol} listed on {asset.get('listing_venue', 'unknown')}")
            except Exception as exc:
                check("instrument_listing", False, str(exc)[:200])
        else:
            check("instrument_listing", True, "listing lookup not configured; format-only validation")
        check("side", side in {"buy", "sell"}, f"side={side}")
        check("order_type", order_type in {"market", "limit", "stop", "stop_limit"},
              f"type={order_type}")
        check("time_in_force", time_in_force in {"day", "gtc", "ioc", "fok", "opg", "cls"},
              f"tif={time_in_force}")
        has_qty = qty is not None and float(qty) > 0
        has_notional = notional is not None and float(notional) > 0
        check("size", has_qty or has_notional,
              "qty>0 or notional>0 required" if not (has_qty or has_notional) else "ok")
        check("size_exclusive", has_qty != has_notional, "exactly one of qty/notional")
        if order_type in {"limit", "stop_limit"}:
            check("limit_price", limit_price is not None and float(limit_price) > 0,
                  "limit orders require limit_price>0")
        if order_type in {"stop", "stop_limit"}:
            check("stop_price", stop_price is not None and float(stop_price) > 0,
                  "stop orders require stop_price>0")

        check("risk_limits_configured", self.limits.configured(),
              "all risk limits must be configured and positive" if not self.limits.configured()
              else "ok")
        check("kill_switch", not self.gate.kill_engaged,
              self.gate.kill_reason or "not engaged")
        check("service_unblocked", self.blocked is None,
              self.blocked or "ok")
        if self.last_reconcile is not None:
            check("reconciliation", bool(self.last_reconcile.get("ok")),
                  "last reconciliation passed" if self.last_reconcile.get("ok")
                  else "last reconciliation FAILED")
        if self.environment == "live":
            permit = self.gate.permit()
            check("live_gate_active", permit is not None,
                  "LIVE requires explicit Arm + Activate" if permit is None else "gate ACTIVE")

        if self.account is None:
            try:
                self.account = self._require_client().get_account()
            except BrokerError as exc:
                check("account_state", False, f"account state uncertain: {type(exc).__name__}")
                self.account = None
        if self.account is not None:
            account = self.account
            check("account_trading_allowed",
                  not (account.get("trading_blocked") or account.get("account_blocked")
                       or account.get("trade_suspended_by_user")),
                  "account is blocked/suspended at the broker" if (
                      account.get("trading_blocked") or account.get("account_blocked")
                      or account.get("trade_suspended_by_user")) else "ok")
            if self.limits.configured():
                value = self._estimated_value(qty, notional, limit_price)
                if value is not None:
                    check("max_order_value", value <= self.limits.max_order_value,
                          f"estimated value {value:.2f} vs cap {self.limits.max_order_value:.2f}")
                    if side == "buy":
                        try:
                            buying_power = float(account.get("buying_power") or 0)
                        except (TypeError, ValueError):
                            buying_power = 0.0
                        check("buying_power", buying_power >= value,
                              f"buying power {buying_power:.2f} vs needed {value:.2f}")
                open_count = sum(1 for o in self.orders.values()
                                 if o.get("status") in OPEN_STATUSES)
                check("max_open_positions",
                      open_count < self.limits.max_open_positions,
                      f"{open_count} open vs cap {self.limits.max_open_positions}")
                today = datetime.now(timezone.utc).date().isoformat()
                today_orders = sum(1 for d in self._daily_order_dates.values() if d == today)
                check("max_daily_orders",
                      today_orders < self.limits.max_daily_orders,
                      f"{today_orders} today vs cap {self.limits.max_daily_orders}")
        else:
            check("account_state", False, "account state unavailable")

        ok = all(c["ok"] for c in checks)
        return {
            "ok": ok,
            "destination": self.destination(),
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "notional": notional,
            "order_type": order_type,
            "time_in_force": time_in_force,
            "limit_price": limit_price,
            "stop_price": stop_price,
            "checks": checks,
            "rejection_reasons": [c["detail"] for c in checks if not c["ok"]],
            "checked_at": utc_now(),
        }

    @staticmethod
    def _estimated_value(qty, notional, limit_price):
        if notional is not None and float(notional) > 0:
            return float(notional)
        if qty is None or float(qty) <= 0:
            return None
        if limit_price is not None and float(limit_price) > 0:
            return float(qty) * float(limit_price)
        return None  # market orders: value unknown until fill; only caps apply

    # -- submissions ------------------------------------------------------------

    def submit_order(self, *, client_order_id: str | None = None, **order_fields) -> dict:
        if self.gate.kill_engaged:
            raise BrokerStateBlocked("kill switch engaged; orders are blocked")
        if self.blocked is not None:
            raise BrokerStateBlocked(f"broker service is blocked ({self.blocked})")
        preview = self.preview_order(**order_fields)
        client_order_id = client_order_id or f"axiom-{uuid.uuid4().hex}"
        if not preview["ok"]:
            self._audit("ALPACA_ORDER_REJECTED_LOCAL", client_order_id=client_order_id,
                        reasons=preview["rejection_reasons"], symbol=preview["symbol"])
            return {"ok": False, "client_order_id": client_order_id,
                    "rejected": "local_checks", "preview": preview}

        # Duplicate prevention: journal first, then broker lookup (restart-safe).
        existing = self.orders.get(client_order_id)
        if existing is not None:
            return {"ok": True, "duplicate": True, "order": self._order_view(existing)}
        client = self._require_client()
        found = client.get_order_by_client_id(client_order_id)
        if found is not None:
            record = self._record_from_broker(found, source="broker-adopted")
            self.orders[client_order_id] = record
            self._audit("ALPACA_ORDER_ADOPTED_EXISTING", client_order_id=client_order_id,
                        broker_order_id=record.get("broker_order_id"))
            return {"ok": True, "duplicate": True, "order": self._order_view(record)}

        payload = self._build_payload(client_order_id, order_fields, preview["symbol"])
        self._audit("ALPACA_ORDER_INTENT", client_order_id=client_order_id,
                    symbol=payload["symbol"], side=payload["side"], qty=payload.get("qty"),
                    notional=payload.get("notional"), order_type=payload["type"],
                    time_in_force=payload["time_in_force"],
                    limit_price=payload.get("limit_price"), stop_price=payload.get("stop_price"))
        try:
            raw = client.submit_order(payload)
        except BrokerRejected as exc:
            self._audit("ALPACA_ORDER_REJECTED", client_order_id=client_order_id,
                        symbol=payload["symbol"], reason=str(exc)[:200])
            return {"ok": False, "client_order_id": client_order_id,
                    "rejected": "broker", "detail": str(exc)[:300]}
        except BrokerError as exc:
            self._audit("ALPACA_ORDER_SUBMIT_UNCERTAIN", client_order_id=client_order_id,
                        symbol=payload["symbol"], reason=type(exc).__name__)
            return {"ok": False, "client_order_id": client_order_id,
                    "rejected": "uncertain", "detail": str(exc)[:300]}
        record = self._record_from_broker(raw, source="submitted")
        record["client_order_id"] = client_order_id
        self.orders[client_order_id] = record
        self._daily_order_dates[client_order_id] = datetime.now(timezone.utc).date().isoformat()
        self._audit("ALPACA_ORDER_SUBMITTED", client_order_id=client_order_id,
                    broker_order_id=record.get("broker_order_id"),
                    symbol=record.get("symbol"), status=record.get("status"))
        return {"ok": True, "duplicate": False, "order": self._order_view(record)}

    @staticmethod
    def _build_payload(client_order_id: str, fields: dict, symbol: str) -> dict:
        payload = {
            "client_order_id": client_order_id,
            "symbol": symbol,
            "side": fields["side"],
            "type": fields.get("order_type", "market"),
            "time_in_force": fields.get("time_in_force", "day"),
        }
        if fields.get("qty") is not None:
            payload["qty"] = str(fields["qty"])
        if fields.get("notional") is not None:
            payload["notional"] = str(fields["notional"])
        if fields.get("limit_price") is not None:
            payload["limit_price"] = str(fields["limit_price"])
        if fields.get("stop_price") is not None:
            payload["stop_price"] = str(fields["stop_price"])
        return payload

    @staticmethod
    def _record_from_broker(raw: dict, *, source: str) -> dict:
        return {
            "client_order_id": raw.get("client_order_id"),
            "broker_order_id": raw.get("id"),
            "symbol": raw.get("symbol"),
            "side": raw.get("side"),
            "qty": raw.get("qty"),
            "notional": raw.get("notional"),
            "type": raw.get("type"),
            "time_in_force": raw.get("time_in_force"),
            "limit_price": raw.get("limit_price"),
            "stop_price": raw.get("stop_price"),
            "status": raw.get("status"),
            "filled_qty": raw.get("filled_qty"),
            "filled_avg_price": raw.get("filled_avg_price"),
            "submitted_at": raw.get("submitted_at"),
            "updated_at": raw.get("updated_at"),
            "source": source,
            "environment": None,  # filled by caller context when auditing
        }

    # -- updates / fills -----------------------------------------------------------

    def sync_fills(self) -> dict:
        """Poll journal orders at the broker; record status/fill transitions."""
        client = self._require_client()
        new_fills = []
        for client_order_id, record in list(self.orders.items()):
            if record.get("status") in TERMINAL_STATUSES:
                continue
            broker_id = record.get("broker_order_id")
            try:
                raw = (client.get_order(broker_id) if broker_id
                       else client.get_order_by_client_id(client_order_id))
            except BrokerError as exc:
                self._audit("ALPACA_ORDER_STATE_UNCERTAIN", client_order_id=client_order_id,
                            reason=type(exc).__name__)
                continue
            if raw is None:
                continue
            previous_qty = str(record.get("filled_qty") or "0")
            record.update({
                "status": raw.get("status", record.get("status")),
                "filled_qty": raw.get("filled_qty"),
                "filled_avg_price": raw.get("filled_avg_price"),
                "updated_at": raw.get("updated_at"),
            })
            current_qty = str(record.get("filled_qty") or "0")
            if current_qty != previous_qty:
                fill = {
                    "client_order_id": client_order_id,
                    "broker_order_id": record.get("broker_order_id"),
                    "symbol": record.get("symbol"),
                    "side": record.get("side"),
                    "filled_qty": current_qty,
                    "filled_avg_price": record.get("filled_avg_price"),
                    "status": record.get("status"),
                    "at": utc_now(),
                }
                self.fills.append(fill)
                new_fills.append(fill)
                self._audit("ALPACA_ORDER_FILL", client_order_id=client_order_id,
                            symbol=record.get("symbol"), filled_qty=current_qty,
                            filled_avg_price=record.get("filled_avg_price"),
                            status=record.get("status"))
            if record.get("status") in TERMINAL_STATUSES:
                self._audit("ALPACA_ORDER_TERMINAL", client_order_id=client_order_id,
                            status=record.get("status"))
        return {"ok": True, "new_fills": new_fills, "orders": len(self.orders)}

    # -- cancel / replace ------------------------------------------------------------

    def cancel_order(self, client_order_id: str) -> dict:
        if self.gate.kill_engaged or self.blocked is not None:
            raise BrokerStateBlocked("orders are blocked (kill switch or blocked state)")
        record = self.orders.get(client_order_id)
        if record is None:
            raise BrokerError("unknown order; refusing to cancel by guess")
        broker_id = record.get("broker_order_id")
        self._audit("ALPACA_ORDER_CANCEL_INTENT", client_order_id=client_order_id)
        try:
            self._require_client().cancel_order(broker_id)
        except (BrokerRejected, BrokerError) as exc:
            self._audit("ALPACA_ORDER_CANCEL_FAILED", client_order_id=client_order_id,
                        reason=type(exc).__name__)
            return {"ok": False, "detail": str(exc)[:300]}
        record["status"] = "pending_cancel"
        self._audit("ALPACA_ORDER_CANCELED", client_order_id=client_order_id)
        return {"ok": True, "order": self._order_view(record)}

    def replace_order(self, client_order_id: str, **fields) -> dict:
        if self.gate.kill_engaged or self.blocked is not None:
            raise BrokerStateBlocked("orders are blocked (kill switch or blocked state)")
        record = self.orders.get(client_order_id)
        if record is None:
            raise BrokerError("unknown order; refusing to replace by guess")
        self._audit("ALPACA_ORDER_REPLACE_INTENT", client_order_id=client_order_id)
        try:
            raw = self._require_client().replace_order(record.get("broker_order_id"), fields)
        except (BrokerRejected, BrokerError) as exc:
            self._audit("ALPACA_ORDER_REPLACE_FAILED", client_order_id=client_order_id,
                        reason=type(exc).__name__)
            return {"ok": False, "detail": str(exc)[:300]}
        record.update({
            "status": raw.get("status", record.get("status")),
            "qty": raw.get("qty", record.get("qty")),
            "limit_price": raw.get("limit_price", record.get("limit_price")),
            "stop_price": raw.get("stop_price", record.get("stop_price")),
            "updated_at": raw.get("updated_at"),
        })
        self._audit("ALPACA_ORDER_REPLACED", client_order_id=client_order_id)
        return {"ok": True, "order": self._order_view(record)}

    # -- reconciliation ---------------------------------------------------------------

    def reconcile(self) -> dict:
        """Compare journal vs broker; any mismatch blocks order actions."""
        try:
            client = self._require_client()
            open_orders = client.get_orders(status="open")
            positions = client.get_positions()
        except BrokerError as exc:
            self.last_reconcile = {"ok": False, "reason": f"broker read failed: {type(exc).__name__}",
                                   "at": utc_now()}
            self.blocked = "RECONCILIATION_FAILED"
            self.gate.disarm("reconciliation failed")
            self._audit("ALPACA_RECONCILE_FAILED", reason=type(exc).__name__)
            return dict(self.last_reconcile)
        journal_open = {r.get("broker_order_id") for r in self.orders.values()
                        if r.get("status") in OPEN_STATUSES and r.get("broker_order_id")}
        broker_open = {str(o.get("id")) for o in open_orders}
        unknown_at_broker = sorted(broker_open - journal_open)
        vanished_locally = sorted(journal_open - broker_open)
        mismatches = []
        if unknown_at_broker:
            mismatches.append(f"orders open at broker but not journaled: {unknown_at_broker}")
        if vanished_locally:
            mismatches.append(f"journaled open orders missing at broker: {vanished_locally}")
        if mismatches:
            self.last_reconcile = {"ok": False, "mismatches": mismatches, "at": utc_now()}
            self.blocked = "RECONCILIATION_FAILED"
            self.gate.disarm("reconciliation mismatch")
            self._audit("ALPACA_RECONCILE_FAILED", mismatches=len(mismatches))
            return dict(self.last_reconcile)
        self.positions = {
            str(p.get("symbol")): {
                "symbol": p.get("symbol"), "qty": p.get("qty"), "side": p.get("side"),
                "avg_entry_price": p.get("avg_entry_price"),
                "unrealized_pl": p.get("unrealized_pl"),
            }
            for p in positions
        }
        self.last_reconcile = {
            "ok": True, "open_orders": len(broker_open),
            "positions": len(self.positions), "at": utc_now(),
        }
        self.blocked = None
        self._audit("ALPACA_RECONCILE_OK", open_orders=len(broker_open),
                    positions=len(self.positions))
        return dict(self.last_reconcile)

    # -- emergency stop -----------------------------------------------------------------

    def emergency_stop(self, reason: str = "operator emergency stop") -> dict:
        """Kill switch: cancel everything at the broker and block all actions."""
        self.gate.engage_kill_switch(reason)
        self._audit("ALPACA_EMERGENCY_STOP", reason=reason)
        canceled = 0
        try:
            if self.client is not None:
                canceled = self.client.cancel_all()
        except BrokerError as exc:
            self._audit("ALPACA_EMERGENCY_STOP_CANCEL_UNCERTAIN", reason=type(exc).__name__)
            self.blocked = "KILL_SWITCH_CANCEL_UNCERTAIN"
        self._audit("ALPACA_EMERGENCY_STOP_APPLIED", canceled=canceled, reason=reason)
        return {"ok": True, "canceled": canceled, "gate": self.gate.status(),
                "blocked": self.blocked}
