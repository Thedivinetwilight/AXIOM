"""Alpaca brokerage service: gated order lifecycle, risk, audit, recovery.

Orchestrates one explicit environment (Alpaca PAPER or Alpaca LIVE) behind the
LIVE gate. Every action is journaled intent-before-write with redacted audit
events; every failure path is fail-closed. Local PAPER (PaperBroker /
LiveMarketPaperEngine) is a separate execution destination and is never touched
from here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import math
from typing import Any, Callable
import uuid

from app.alpaca_broker.client import (
    AlpacaReadOnlyClient,
    AlpacaTradingClient,
    BrokerError,
    BrokerRejected,
    utc_now,
)
from app.alpaca_broker.gate import BrokerLiveGate, GateError
from app.alpaca_broker.journal import AlpacaBrokerJournal
from app.data.alpaca import validate_us_symbol
from app.data.models import parse_timestamp

OPEN_STATUSES = {
    "new", "accepted", "pending_new", "accepted_for_bidding",
    "pending_cancel", "pending_replace", "partially_filled", "held",
}
TERMINAL_STATUSES = {
    "filled", "canceled", "expired", "rejected", "replaced", "stopped",
    "suspended", "done_for_day", "calculated",
}

LIVE_CONFIRM_PHRASE = "ACTIVATE LIVE REAL-MONEY TRADING"


def valid_reg_nms_tick(price: float) -> bool:
    """Validate US Reg NMS Rule 612 minimum pricing increment.

    Orders priced >= $1.00 must be in $0.01 increments; sub-dollar orders
    (< $1.00) may use $0.0001 increments.
    """
    if not (isinstance(price, (int, float)) and math.isfinite(price) and price > 0):
        return False
    try:
        dec = Decimal(str(price))
    except InvalidOperation:
        return False
    quantum = Decimal("0.01") if dec >= Decimal("1.00") else Decimal("0.0001")
    return (dec % quantum) == 0


def _format_decimal(value: Decimal) -> str:
    normalized = value.normalize()
    text = format(normalized, "f")
    return text if text != "-0" else "0"


@dataclass
class BrokerRiskLimits:
    """All limits are mandatory and must be positive; missing limits fail closed."""

    max_order_value: float = 0.0
    max_open_positions: int = 0
    max_daily_orders: int = 0
    max_daily_loss: float = 2_500.0
    max_symbol_exposure: float = 50_000.0
    max_total_exposure: float = 200_000.0
    max_quote_age_seconds: float = 60.0
    max_clock_skew_seconds: float = 30.0

    def configured(self) -> bool:
        return (
            self.max_order_value > 0
            and self.max_open_positions > 0
            and self.max_daily_orders > 0
            and self.max_daily_loss > 0
            and self.max_symbol_exposure > 0
            and self.max_total_exposure > 0
            and self.max_quote_age_seconds > 0
            and self.max_clock_skew_seconds > 0
        )

    def to_dict(self) -> dict:
        return {
            "max_order_value": self.max_order_value,
            "max_open_positions": self.max_open_positions,
            "max_daily_orders": self.max_daily_orders,
            "max_daily_loss": self.max_daily_loss,
            "max_symbol_exposure": self.max_symbol_exposure,
            "max_total_exposure": self.max_total_exposure,
            "max_quote_age_seconds": self.max_quote_age_seconds,
            "max_clock_skew_seconds": self.max_clock_skew_seconds,
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
        read_only_client: AlpacaReadOnlyClient | None = None,
        read_only_factory: Callable[[], AlpacaReadOnlyClient | None] | None = None,
        journal: AlpacaBrokerJournal | None = None,
        quote_provider: Callable[[str], dict[str, Any] | None] | None = None,
        calendar=None,
        clock: Callable[[], datetime] | None = None,
        require_market_price_bound: bool = False,
    ):
        self.client = client
        self.environment = environment
        self.limits = limits
        self.repository = repository
        self.gate = gate if gate is not None else BrokerLiveGate()
        self.asset_lookup = asset_lookup
        self.client_factory = client_factory
        self.read_only_client = read_only_client
        self.read_only_factory = read_only_factory
        self.journal = journal
        self.quote_provider = quote_provider
        self.calendar = calendar
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.require_market_price_bound = bool(require_market_price_bound or environment == "live")

        self.orders: dict[str, dict] = {}
        self.unresolved_intents: dict[str, str] = {}
        self.fills: list[dict] = []
        self.positions: dict[str, dict] = {}
        self.account: dict | None = None
        self.blocked: str | None = None
        self.last_reconcile: dict | None = None
        self.recovered_at: str | None = None
        self._daily_order_dates: dict[str, str] = {}

        if self.journal is not None:
            persisted = self.journal.load_state(self.environment)
            self.orders.update(persisted["orders"])
            self.unresolved_intents.update(persisted["unresolved"])
            self._daily_order_dates.update(persisted["daily_dates"])
            self.fills.extend(persisted["fills"])
            if self.unresolved_intents:
                self.blocked = "UNRESOLVED_ORDER_INTENT"

    def _require_client(self) -> AlpacaTradingClient:
        if self.client is None:
            raise BrokerStateBlocked(
                "broker client unavailable (credentials not configured, or LIVE "
                "requires explicit Arm + Activate first)"
            )
        return self.client

    def _require_read_client(self):
        if self.client is not None:
            return self.client
        if self.read_only_client is not None:
            return self.read_only_client
        if self.read_only_factory is not None:
            ro = self.read_only_factory()
            if ro is not None:
                return ro
        raise BrokerStateBlocked(
            "broker read client unavailable (configure distinct broker credentials first)"
        )

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
            "unresolved_intents": dict(self.unresolved_intents),
            "client_bound": self.client is not None,
            "last_reconcile": self.last_reconcile,
            "recovered_at": self.recovered_at,
            "live_confirmation_phrase": LIVE_CONFIRM_PHRASE if is_live else None,
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

    def _resolve_unresolved_with_client(self, reader) -> list[str]:
        resolved: list[str] = []
        for cid in list(self.unresolved_intents):
            found = reader.get_order_by_client_id(cid)
            if found is not None:
                record = self._record_from_broker(found, source="broker-adopted")
                record["client_order_id"] = cid
                self.orders[cid] = record
                if self.journal is not None:
                    self.journal.save_order(self.environment, cid, record)
                self._audit("ALPACA_UNRESOLVED_INTENT_ADOPTED", client_order_id=cid,
                            broker_order_id=record.get("broker_order_id"))
            else:
                if self.journal is not None:
                    self.journal.clear_unresolved(cid)
                self._audit("ALPACA_UNRESOLVED_INTENT_NOT_FOUND", client_order_id=cid)
            self.unresolved_intents.pop(cid, None)
            resolved.append(cid)
        return resolved

    def recover(self) -> dict:
        """Rebuild state from the broker; uncertainty blocks order actions."""
        reader = self._require_read_client()
        try:
            self.account = reader.get_account()
            open_orders = reader.get_orders(status="open")
            positions = reader.get_positions()
            resolved_intents = self._resolve_unresolved_with_client(reader)
        except (BrokerError, ValueError) as exc:
            self.blocked = "RECOVERY_UNCERTAIN"
            self._audit("ALPACA_BROKER_RECOVERY_FAILED", reason=type(exc).__name__)
            return {"ok": False, "reason": "recovery_uncertain", "blocked": self.blocked}
        for raw in open_orders:
            record = self._record_from_broker(raw, source="broker-adopted")
            cid = str(record["client_order_id"])
            self.orders[cid] = record
            if self.journal is not None:
                self.journal.save_order(self.environment, cid, record)
        self.positions = {
            str(p.get("symbol")): {
                "symbol": p.get("symbol"),
                "qty": p.get("qty"),
                "side": p.get("side"),
                "avg_entry_price": p.get("avg_entry_price"),
                "market_value": p.get("market_value"),
                "unrealized_pl": p.get("unrealized_pl"),
            }
            for p in positions
        }
        self.blocked = None
        self.recovered_at = utc_now()
        self._audit(
            "ALPACA_BROKER_RECOVERED",
            open_orders=len(open_orders),
            positions=len(self.positions),
            resolved_intents=len(resolved_intents),
        )
        return {
            "ok": True,
            "adopted_orders": len(open_orders),
            "resolved_intents": len(resolved_intents),
            "positions": len(self.positions),
            "account": self.account,
        }

    # -- readiness (LIVE gate input) -------------------------------------------

    def readiness_checks(self, *, symbol: str | None = None) -> dict:
        """All must pass before LIVE can be armed. Any uncertainty is False."""
        checks: dict[str, bool] = {
            "broker_enabled": True,
            "risk_limits_configured": self.limits.configured(),
            "kill_switch_not_engaged": not self.gate.kill_engaged,
            "service_unblocked": self.blocked is None,
            "no_unresolved_intents": len(self.unresolved_intents) == 0,
            "reconciliation_ok": self.last_reconcile is None or bool(self.last_reconcile.get("ok")),
        }
        try:
            reader = self._require_read_client()
            self.account = reader.get_account()
            account = self.account
            checks["account_verified"] = True
            checks["account_trading_allowed"] = not (
                account.get("trading_blocked")
                or account.get("account_blocked")
                or account.get("trade_suspended_by_user")
            )
            currency = str(account.get("currency") or "USD").upper()
            checks["account_currency_usd"] = currency == "USD"
            checks["daily_loss_within_limit"] = not self._daily_loss_breached(account)
            if hasattr(reader, "get_clock"):
                try:
                    clock_payload = reader.get_clock()
                    if isinstance(clock_payload, dict) and clock_payload.get("timestamp"):
                        broker_ts = parse_timestamp(str(clock_payload["timestamp"]))
                        skew = abs((self._clock() - broker_ts).total_seconds())
                        checks["clock_synced"] = skew <= self.limits.max_clock_skew_seconds
                        if self.environment == "live" and "is_open" in clock_payload:
                            checks["market_session_open"] = bool(clock_payload.get("is_open"))
                except BrokerError:
                    if self.environment == "live":
                        checks["clock_synced"] = False
        except (BrokerError, BrokerStateBlocked):
            checks["account_verified"] = False
            checks["account_trading_allowed"] = False

        if self.calendar is not None and self.environment == "live":
            try:
                checks["calendar_session_open"] = bool(self.calendar.is_open(self._clock()))
            except Exception:
                checks["calendar_session_open"] = False

        if self.quote_provider is not None and symbol:
            quote = self._fresh_quote_for(symbol)
            checks["market_quote_fresh"] = quote["ok"]

        if self.asset_lookup is not None:
            checks["instrument_validation_available"] = True
        return checks

    def _daily_loss_breached(self, account: dict | None) -> bool:
        if not account or not self.limits.configured():
            return False
        try:
            equity = float(account.get("equity") or 0.0)
            last_equity = float(account.get("last_equity") or 0.0)
        except (TypeError, ValueError):
            return False
        if equity > 0 and last_equity > 0:
            day_pnl = equity - last_equity
            return day_pnl <= -abs(self.limits.max_daily_loss)
        return False

    def _fresh_quote_for(self, symbol: str) -> dict[str, Any]:
        if self.quote_provider is None:
            return {"ok": False, "price": None, "detail": "no live US quote provider configured"}
        try:
            snap = self.quote_provider(symbol)
        except Exception as exc:
            return {"ok": False, "price": None, "detail": f"quote lookup failed: {str(exc)[:160]}"}
        if not isinstance(snap, dict):
            return {"ok": False, "price": None, "detail": "quote snapshot unavailable"}
        try:
            price = float(snap.get("last") or snap.get("price") or 0.0)
            stamp = parse_timestamp(str(snap.get("timestamp") or ""))
        except (TypeError, ValueError):
            return {"ok": False, "price": None, "detail": "quote price or timestamp invalid"}
        if not (math.isfinite(price) and price > 0):
            return {"ok": False, "price": None, "detail": "quote price is non-positive"}
        age = (self._clock() - stamp).total_seconds()
        if age < -5.0 or age > self.limits.max_quote_age_seconds:
            return {
                "ok": False,
                "price": price,
                "detail": f"quote is stale ({age:.1f}s > {self.limits.max_quote_age_seconds:.0f}s)",
            }
        if self.environment == "live" and bool(snap.get("is_delayed")):
            return {
                "ok": False,
                "price": price,
                "detail": "delayed_sip feed cannot authorize LIVE real-money orders; use iex or sip",
            }
        return {"ok": True, "price": price, "age_seconds": round(max(age, 0.0), 2), "detail": "ok"}

    # -- gate binding (LIVE) ---------------------------------------------------

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

    def activate_live_confirmed(
        self,
        *,
        confirm_live_trading: str,
        acknowledge_risk: bool,
        symbol: str | None = None,
    ) -> dict:
        """Operator HTTP boundary for LIVE activation requiring typed phrase + fresh preflight."""
        if not acknowledge_risk or (confirm_live_trading or "").strip() != LIVE_CONFIRM_PHRASE:
            raise GateError(
                f"LIVE activation requires acknowledge_risk=true and exact phrase {LIVE_CONFIRM_PHRASE!r}"
            )
        checks = self.readiness_checks(symbol=symbol)
        failed = sorted(name for name, passed in checks.items() if not passed)
        if failed:
            self.disarm(f"activation preflight failed: {', '.join(failed)}")
            raise GateError(f"LIVE activation refused; failed readiness checks: {', '.join(failed)}")
        return self.activate_live()

    def disarm(self, reason: str = "operator disarm") -> dict:
        result = self.gate.disarm(reason)
        if self.environment == "live":
            self.client = None  # a live client never survives disarm
        self._audit("ALPACA_GATE_DISARMED", reason=reason)
        return result

    # -- preview / risk checks -------------------------------------------------

    def preview_order(
        self,
        *,
        symbol: str,
        side: str,
        qty=None,
        notional=None,
        order_type: str = "market",
        time_in_force: str = "day",
        limit_price=None,
        stop_price=None,
        reference_price: float | None = None,
    ) -> dict:
        checks: list[dict] = []

        def check(name: str, ok: bool, detail: str) -> None:
            checks.append({"check": name, "ok": bool(ok), "detail": detail})

        try:
            symbol = validate_us_symbol(symbol)
            check("instrument_symbol", True, f"{symbol} is a valid US ticker format")
        except ValueError as exc:
            check("instrument_symbol", False, str(exc))
            symbol = str(symbol)

        asset_meta: dict | None = None
        if self.asset_lookup is not None:
            try:
                asset_meta = self.asset_lookup(symbol)
                tradable = bool(asset_meta.get("tradable", True))
                check(
                    "instrument_listing",
                    tradable,
                    f"{symbol} listed on {asset_meta.get('listing_venue', 'unknown')}"
                    if tradable
                    else f"{symbol} is marked untradable by asset metadata",
                )
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

        if has_qty:
            qty_float = float(qty)
            is_fractional = abs(qty_float - round(qty_float)) > 1e-9
            if is_fractional:
                if order_type != "market" or time_in_force != "day":
                    check(
                        "fractional_rules",
                        False,
                        "fractional share quantities require market DAY orders at Alpaca",
                    )
                elif asset_meta is not None and not bool(asset_meta.get("fractionable", True)):
                    check("fractional_rules", False, f"{symbol} is not fractionable")
                else:
                    check("fractional_rules", True, "fractional share order rules satisfied")

        if order_type in {"limit", "stop_limit"}:
            lp_ok = limit_price is not None and float(limit_price) > 0 and valid_reg_nms_tick(float(limit_price))
            check(
                "limit_price",
                lp_ok,
                "limit orders require positive Reg NMS tick-valid limit_price ($0.01 >= $1, $0.0001 < $1)"
                if not lp_ok
                else "ok",
            )
        if order_type in {"stop", "stop_limit"}:
            sp_ok = stop_price is not None and float(stop_price) > 0 and valid_reg_nms_tick(float(stop_price))
            check(
                "stop_price",
                sp_ok,
                "stop orders require positive Reg NMS tick-valid stop_price ($0.01 >= $1, $0.0001 < $1)"
                if not sp_ok
                else "ok",
            )

        check("risk_limits_configured", self.limits.configured(),
              "all risk limits must be configured and positive" if not self.limits.configured()
              else "ok")
        check("kill_switch", not self.gate.kill_engaged,
              self.gate.kill_reason or "not engaged")
        check("service_unblocked", self.blocked is None,
              self.blocked or "ok")
        check(
            "no_unresolved_intents",
            len(self.unresolved_intents) == 0,
            f"unresolved order intents pending reconciliation: {sorted(self.unresolved_intents)}"
            if self.unresolved_intents
            else "ok",
        )
        if self.last_reconcile is not None:
            check("reconciliation", bool(self.last_reconcile.get("ok")),
                  "last reconciliation passed" if self.last_reconcile.get("ok")
                  else "last reconciliation FAILED")
        if self.environment == "live":
            permit = self.gate.permit()
            check("live_gate_active", permit is not None,
                  "LIVE requires explicit Arm + Activate" if permit is None else "gate ACTIVE")
            if self.calendar is not None:
                sess_open = bool(self.calendar.is_open(self._clock()))
                check(
                    "market_session_open",
                    sess_open,
                    "regular US market session is OPEN" if sess_open else "US regular session is CLOSED",
                )

        if self.account is None:
            try:
                self.account = self._require_read_client().get_account()
            except (BrokerError, BrokerStateBlocked) as exc:
                check("account_state", False, f"account state uncertain: {type(exc).__name__}")
                self.account = None

        estimated_value = self._estimated_value(qty, notional, limit_price, stop_price, reference_price)
        if estimated_value is None and has_qty:
            if self.quote_provider is not None:
                quote = self._fresh_quote_for(symbol)
                if quote["ok"] and quote["price"]:
                    estimated_value = float(qty) * float(quote["price"])
                    check("market_reference_price", True, f"valued from fresh quote ${quote['price']:.2f}")
                else:
                    check("market_reference_price", False, f"market order risk valuation blocked: {quote['detail']}")
            elif self.require_market_price_bound:
                check(
                    "market_reference_price",
                    False,
                    "share-quantity market orders require a fresh verified US quote or notional cap",
                )

        if self.account is not None:
            account = self.account
            check(
                "account_trading_allowed",
                not (
                    account.get("trading_blocked")
                    or account.get("account_blocked")
                    or account.get("trade_suspended_by_user")
                ),
                "account is blocked/suspended at the broker"
                if (
                    account.get("trading_blocked")
                    or account.get("account_blocked")
                    or account.get("trade_suspended_by_user")
                )
                else "ok",
            )
            if self.limits.configured():
                check(
                    "daily_loss_limit",
                    not self._daily_loss_breached(account),
                    f"daily loss breached max_daily_loss ({self.limits.max_daily_loss:.2f})"
                    if self._daily_loss_breached(account)
                    else "ok",
                )
                if estimated_value is not None:
                    check(
                        "max_order_value",
                        estimated_value <= self.limits.max_order_value,
                        f"estimated value {estimated_value:.2f} vs cap {self.limits.max_order_value:.2f}",
                    )
                    if side == "buy":
                        try:
                            buying_power = float(account.get("buying_power") or 0)
                        except (TypeError, ValueError):
                            buying_power = 0.0
                        check(
                            "buying_power",
                            buying_power >= estimated_value,
                            f"buying power {buying_power:.2f} vs needed {estimated_value:.2f}",
                        )
                        sym_exp = self._symbol_exposure(symbol) + estimated_value
                        check(
                            "max_symbol_exposure",
                            sym_exp <= self.limits.max_symbol_exposure,
                            f"{symbol} exposure {sym_exp:.2f} vs cap {self.limits.max_symbol_exposure:.2f}",
                        )
                        tot_exp = self._total_exposure() + estimated_value
                        check(
                            "max_total_exposure",
                            tot_exp <= self.limits.max_total_exposure,
                            f"total exposure {tot_exp:.2f} vs cap {self.limits.max_total_exposure:.2f}",
                        )
                open_count = sum(
                    1 for o in self.orders.values() if o.get("status") in OPEN_STATUSES
                )
                check(
                    "max_open_positions",
                    open_count < self.limits.max_open_positions,
                    f"{open_count} open vs cap {self.limits.max_open_positions}",
                )
                today = self._clock().date().isoformat()
                today_orders = sum(1 for d in self._daily_order_dates.values() if d == today)
                check(
                    "max_daily_orders",
                    today_orders < self.limits.max_daily_orders,
                    f"{today_orders} today vs cap {self.limits.max_daily_orders}",
                )
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
            "estimated_value": round(estimated_value, 4) if estimated_value is not None else None,
            "checks": checks,
            "rejection_reasons": [c["detail"] for c in checks if not c["ok"]],
            "checked_at": utc_now(),
        }

    def _symbol_exposure(self, symbol: str) -> float:
        pos = self.positions.get(symbol)
        if not pos:
            return 0.0
        try:
            if pos.get("market_value") is not None:
                return abs(float(pos["market_value"]))
            return abs(float(pos.get("qty") or 0.0) * float(pos.get("avg_entry_price") or 0.0))
        except (TypeError, ValueError):
            return 0.0

    def _total_exposure(self) -> float:
        return sum(self._symbol_exposure(sym) for sym in self.positions)

    @staticmethod
    def _estimated_value(qty, notional, limit_price, stop_price=None, reference_price=None):
        if notional is not None and float(notional) > 0:
            return float(notional)
        if qty is None or float(qty) <= 0:
            return None
        for candidate in (limit_price, stop_price, reference_price):
            if candidate is not None and float(candidate) > 0:
                return float(qty) * float(candidate)
        return None

    # -- submissions -----------------------------------------------------------

    def submit_order(self, *, client_order_id: str | None = None, **order_fields) -> dict:
        if self.gate.kill_engaged:
            raise BrokerStateBlocked("kill switch engaged; orders are blocked")
        if self.blocked is not None:
            raise BrokerStateBlocked(f"broker service is blocked ({self.blocked})")
        client = self._require_client()
        preview = self.preview_order(**order_fields)
        client_order_id = client_order_id or f"axiom-{uuid.uuid4().hex}"
        if not preview["ok"]:
            self._audit("ALPACA_ORDER_REJECTED_LOCAL", client_order_id=client_order_id,
                        reasons=preview["rejection_reasons"], symbol=preview["symbol"])
            return {"ok": False, "client_order_id": client_order_id,
                    "rejected": "local_checks", "preview": preview}

        existing = self.orders.get(client_order_id)
        if existing is not None:
            return {"ok": True, "duplicate": True, "order": self._order_view(existing)}
        found = client.get_order_by_client_id(client_order_id)
        if found is not None:
            record = self._record_from_broker(found, source="broker-adopted")
            record["client_order_id"] = client_order_id
            self.orders[client_order_id] = record
            if self.journal is not None:
                self.journal.save_order(self.environment, client_order_id, record)
            self._audit("ALPACA_ORDER_ADOPTED_EXISTING", client_order_id=client_order_id,
                        broker_order_id=record.get("broker_order_id"))
            return {"ok": True, "duplicate": True, "order": self._order_view(record)}

        payload = self._build_payload(client_order_id, order_fields, preview["symbol"])
        today = self._clock().date().isoformat()
        if self.journal is not None:
            self.journal.record_intent(
                environment=self.environment,
                client_order_id=client_order_id,
                payload=payload,
                order_date=today,
            )
        self._audit("ALPACA_ORDER_INTENT", client_order_id=client_order_id,
                    symbol=payload["symbol"], side=payload["side"], qty=payload.get("qty"),
                    notional=payload.get("notional"), order_type=payload["type"],
                    time_in_force=payload["time_in_force"],
                    limit_price=payload.get("limit_price"), stop_price=payload.get("stop_price"))
        try:
            raw = client.submit_order(payload)
        except BrokerRejected as exc:
            if self.journal is not None:
                self.journal.remove_rejected_intent(client_order_id)
            self._audit("ALPACA_ORDER_REJECTED", client_order_id=client_order_id,
                        symbol=payload["symbol"], reason=str(exc)[:200])
            return {"ok": False, "client_order_id": client_order_id,
                    "rejected": "broker", "detail": str(exc)[:300]}
        except BrokerError as exc:
            reason = f"{type(exc).__name__}: {str(exc)[:180]}"
            self.unresolved_intents[client_order_id] = reason
            self.blocked = "UNRESOLVED_ORDER_INTENT"
            if self.environment == "live":
                self.disarm("uncertain order submission outcome")
            if self.journal is not None:
                self.journal.mark_unresolved(client_order_id, reason)
            self._audit("ALPACA_ORDER_SUBMIT_UNCERTAIN", client_order_id=client_order_id,
                        symbol=payload["symbol"], reason=type(exc).__name__)
            return {"ok": False, "client_order_id": client_order_id,
                    "rejected": "uncertain", "detail": str(exc)[:300]}

        record = self._record_from_broker(raw, source="submitted")
        record["client_order_id"] = client_order_id
        record["environment"] = self.environment
        self.orders[client_order_id] = record
        self._daily_order_dates[client_order_id] = today
        if self.journal is not None:
            self.journal.save_order(self.environment, client_order_id, record, order_date=today)
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
            "environment": None,
        }

    # -- updates / fills -------------------------------------------------------

    def sync_fills(self) -> dict:
        """Poll journal orders at the broker; record incremental & cumulative fills."""
        reader = self._require_read_client()
        new_fills = []
        for client_order_id, record in list(self.orders.items()):
            if record.get("status") in TERMINAL_STATUSES:
                continue
            broker_id = record.get("broker_order_id")
            try:
                raw = (
                    reader.get_order(broker_id)
                    if broker_id
                    else reader.get_order_by_client_id(client_order_id)
                )
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
            if self.journal is not None:
                self.journal.save_order(self.environment, client_order_id, record)
            current_qty = str(record.get("filled_qty") or "0")
            try:
                prev_dec = Decimal(previous_qty)
                curr_dec = Decimal(current_qty)
            except InvalidOperation:
                prev_dec = Decimal("0")
                curr_dec = Decimal("0")
            delta_dec = curr_dec - prev_dec
            if delta_dec > 0:
                incremental_qty = _format_decimal(delta_dec)
                fill = {
                    "client_order_id": client_order_id,
                    "broker_order_id": record.get("broker_order_id"),
                    "symbol": record.get("symbol"),
                    "side": record.get("side"),
                    "incremental_qty": incremental_qty,
                    "filled_qty": current_qty,
                    "filled_avg_price": record.get("filled_avg_price"),
                    "status": record.get("status"),
                    "at": utc_now(),
                }
                fill_key = f"{client_order_id}:{current_qty}:{record.get('updated_at') or ''}"
                if self.journal is None or self.journal.save_fill(self.environment, fill_key, client_order_id, fill):
                    self.fills.append(fill)
                    new_fills.append(fill)
                    self._audit(
                        "ALPACA_ORDER_FILL",
                        client_order_id=client_order_id,
                        symbol=record.get("symbol"),
                        incremental_qty=incremental_qty,
                        filled_qty=current_qty,
                        filled_avg_price=record.get("filled_avg_price"),
                        status=record.get("status"),
                    )
            if record.get("status") in TERMINAL_STATUSES:
                self._audit("ALPACA_ORDER_TERMINAL", client_order_id=client_order_id,
                            status=record.get("status"))
        return {"ok": True, "new_fills": new_fills, "orders": len(self.orders)}

    # -- cancel / replace ------------------------------------------------------

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
        if self.journal is not None:
            self.journal.save_order(self.environment, client_order_id, record)
        self._audit("ALPACA_ORDER_CANCELED", client_order_id=client_order_id)
        return {"ok": True, "order": self._order_view(record)}

    def replace_order(self, client_order_id: str, **fields) -> dict:
        if self.gate.kill_engaged or self.blocked is not None:
            raise BrokerStateBlocked("orders are blocked (kill switch or blocked state)")
        record = self.orders.get(client_order_id)
        if record is None:
            raise BrokerError("unknown order; refusing to replace by guess")
        old_broker_id = record.get("broker_order_id")
        self._audit("ALPACA_ORDER_REPLACE_INTENT", client_order_id=client_order_id,
                    broker_order_id=old_broker_id)
        try:
            raw = self._require_client().replace_order(old_broker_id, fields)
        except (BrokerRejected, BrokerError) as exc:
            self._audit("ALPACA_ORDER_REPLACE_FAILED", client_order_id=client_order_id,
                        reason=type(exc).__name__)
            return {"ok": False, "detail": str(exc)[:300]}
        new_broker_id = raw.get("id") or old_broker_id
        record.update({
            "broker_order_id": new_broker_id,
            "previous_broker_order_id": old_broker_id if new_broker_id != old_broker_id else record.get("previous_broker_order_id"),
            "status": raw.get("status", record.get("status")),
            "qty": raw.get("qty", record.get("qty")),
            "limit_price": raw.get("limit_price", record.get("limit_price")),
            "stop_price": raw.get("stop_price", record.get("stop_price")),
            "updated_at": raw.get("updated_at"),
        })
        if self.journal is not None:
            self.journal.save_order(self.environment, client_order_id, record)
        self._audit("ALPACA_ORDER_REPLACED", client_order_id=client_order_id,
                    old_broker_order_id=old_broker_id, broker_order_id=new_broker_id)
        return {"ok": True, "order": self._order_view(record)}

    def close_position(self, symbol: str, *, reason: str = "operator explicit position close") -> dict:
        """Explicit auditable position exit initiated by the operator."""
        symbol_clean = validate_us_symbol(symbol)
        client = self._require_client()
        self._audit("ALPACA_POSITION_CLOSE_INTENT", symbol=symbol_clean, reason=reason)
        try:
            raw = client.close_position(symbol_clean)
        except (BrokerRejected, BrokerError) as exc:
            self._audit("ALPACA_POSITION_CLOSE_FAILED", symbol=symbol_clean, reason=type(exc).__name__)
            return {"ok": False, "symbol": symbol_clean, "detail": str(exc)[:300]}
        cid = str(raw.get("client_order_id") or f"axiom-close-{uuid.uuid4().hex[:12]}")
        record = self._record_from_broker(raw, source="position-close")
        record["client_order_id"] = cid
        self.orders[cid] = record
        if self.journal is not None:
            self.journal.save_order(self.environment, cid, record)
        self._audit("ALPACA_POSITION_CLOSED", symbol=symbol_clean,
                    client_order_id=cid, broker_order_id=record.get("broker_order_id"))
        return {"ok": True, "symbol": symbol_clean, "order": self._order_view(record)}

    # -- reconciliation --------------------------------------------------------

    def reconcile(self) -> dict:
        """Compare journal vs broker; any mismatch blocks order actions."""
        try:
            reader = self._require_read_client()
            self._resolve_unresolved_with_client(reader)
            open_orders = reader.get_orders(status="open")
            positions = reader.get_positions()
        except (BrokerError, BrokerStateBlocked) as exc:
            self.last_reconcile = {"ok": False, "reason": f"broker read failed: {type(exc).__name__}",
                                   "at": utc_now()}
            self.blocked = "RECONCILIATION_FAILED"
            self.gate.disarm("reconciliation failed")
            self._audit("ALPACA_RECONCILE_FAILED", reason=type(exc).__name__)
            return dict(self.last_reconcile)
        journal_open = {
            str(r.get("broker_order_id"))
            for r in self.orders.values()
            if r.get("status") in OPEN_STATUSES and r.get("broker_order_id")
        }
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
                "symbol": p.get("symbol"),
                "qty": p.get("qty"),
                "side": p.get("side"),
                "avg_entry_price": p.get("avg_entry_price"),
                "market_value": p.get("market_value"),
                "unrealized_pl": p.get("unrealized_pl"),
            }
            for p in positions
        }
        self.last_reconcile = {
            "ok": True,
            "open_orders": len(broker_open),
            "positions": len(self.positions),
            "at": utc_now(),
        }
        self.blocked = None
        self._audit("ALPACA_RECONCILE_OK", open_orders=len(broker_open),
                    positions=len(self.positions))
        return dict(self.last_reconcile)

    # -- emergency stop --------------------------------------------------------

    @staticmethod
    def _is_protective_exit_order(order_raw: dict, positions: dict[str, dict]) -> bool:
        symbol = str(order_raw.get("symbol") or "")
        pos = positions.get(symbol)
        if not pos:
            return False
        pos_side = str(pos.get("side") or "long").lower()
        ord_side = str(order_raw.get("side") or "").lower()
        ord_type = str(order_raw.get("type") or "").lower()
        is_opposite = (pos_side == "long" and ord_side == "sell") or (pos_side == "short" and ord_side == "buy")
        if not is_opposite:
            return False
        if ord_type not in {"stop", "stop_limit", "trailing_stop", "limit"}:
            return False
        try:
            pos_qty = abs(float(pos.get("qty") or 0.0))
            ord_qty = abs(float(order_raw.get("qty") or 0.0))
        except (TypeError, ValueError):
            return False
        return pos_qty > 0 and 0 < ord_qty <= pos_qty + 1e-9

    def emergency_stop(self, reason: str = "operator emergency stop") -> dict:
        """Kill switch: block new entries, cancel unfilled entries, preserve position protection."""
        active_client = self.client
        self.gate.engage_kill_switch(reason)
        if self.environment == "live":
            self.client = None
        self._audit("ALPACA_EMERGENCY_STOP", reason=reason)
        canceled = 0
        preserved: list[str] = []
        try:
            if active_client is not None:
                if not self.positions:
                    canceled = active_client.cancel_all()
                else:
                    open_orders = active_client.get_orders(status="open")
                    for order_raw in open_orders:
                        oid = str(order_raw.get("id") or "")
                        if self._is_protective_exit_order(order_raw, self.positions):
                            preserved.append(oid)
                        elif oid:
                            active_client.cancel_order(oid)
                            canceled += 1
        except BrokerError as exc:
            self._audit("ALPACA_EMERGENCY_STOP_CANCEL_UNCERTAIN", reason=type(exc).__name__)
            self.blocked = "KILL_SWITCH_CANCEL_UNCERTAIN"
        self._audit(
            "ALPACA_EMERGENCY_STOP_APPLIED",
            canceled=canceled,
            preserved_protective_orders=len(preserved),
            reason=reason,
        )
        return {
            "ok": True,
            "canceled": canceled,
            "preserved_protective_orders": preserved,
            "gate": self.gate.status(),
            "blocked": self.blocked,
        }
