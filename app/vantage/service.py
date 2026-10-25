"""Fail-closed Vantage session, lifecycle, review and execution service."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import secrets
from threading import RLock
from typing import Callable

from app.vantage.connector import (
    AXIOM_MAGIC, BrokerError, BrokerUnavailable, InvalidMarketData, MT5Connector,
)
from app.vantage.gateway import OfficialMT5Gateway
from app.vantage.models import (
    ConnectionState, FINAL_ORDER_STATES, Lifecycle, OrderInput, ReadinessCheck, RiskLimits,
    SessionMode, UNRESOLVED_ORDER_STATES, iso_now, public_dict, utcnow,
)
from app.vantage.risk import ContractViolation, evaluate_entry, normalize_order
from app.vantage.secrets import DpapiSecretStore, SecretNotFound
from app.vantage.store import VantageStore, now_iso


class ServiceError(RuntimeError):
    pass


class OrderBlocked(ServiceError):
    def __init__(self, issues):
        self.issues = list(issues) if isinstance(issues, (list, tuple)) else [str(issues)]
        super().__init__("; ".join(self.issues))


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _age(value: str | None) -> float:
    parsed = _parse_time(value)
    return (utcnow() - parsed).total_seconds() if parsed else float("inf")


def _comment(intent_id: str) -> str:
    safe = "".join(char for char in intent_id.upper() if char.isalnum())
    return f"AXM{safe[:24]}"


class VantageService:
    """One mutually exclusive session. Nothing connects or activates on init."""

    def __init__(
        self,
        store: VantageStore,
        secret_store=None,
        gateway_factory: Callable = OfficialMT5Gateway,
        lock_check: Callable[[], bool] | None = None,
    ):
        self.store = store
        self.secret_store = secret_store  # packaged server supplies DPAPI explicitly
        self.gateway_factory = gateway_factory
        self.lock_check = lock_check or (lambda: False)
        self._lock = RLock()
        self.connector: MT5Connector | None = None
        self.lifecycle = Lifecycle.DISARMED
        self._armed_fingerprint: str | None = None
        self._reviews: dict[str, dict] = {}
        self._nonces: dict[str, datetime] = {}
        raw_mode = store.get_runtime("session_mode", SessionMode.PAPER.value)
        try:
            self.mode = SessionMode(raw_mode)
        except ValueError:
            # Never guess live from damaged state. Preserve the bad value for
            # diagnosis but fail closed in local paper with a prominent event.
            self.mode = SessionMode.PAPER
            store.record_event(self.mode.value, "INVALID_STORED_MODE_FAIL_CLOSED", {})
            store.set_runtime("session_mode", self.mode.value)
        store.set_runtime("automation_enabled", False)
        store.record_event(self.mode.value, "RUNTIME_STARTED_DISARMED", {
            "connected": False, "automation_enabled": False,
            "emergency_stop": self.emergency_stop,
        })

    @property
    def emergency_stop(self) -> bool:
        return bool(self.store.get_runtime("emergency_stop", {}).get("stopped", False))

    def _set_emergency(self, stopped: bool, reason: str):
        self.store.set_runtime("emergency_stop", {"stopped": stopped, "reason": reason, "ts": iso_now()})

    @property
    def selected_symbols(self) -> list[str]:
        return list(self.store.get_runtime(f"selected:{self.mode.value}", []))

    @property
    def risk_limits(self) -> RiskLimits:
        try:
            return RiskLimits(**self.store.get_runtime(f"risk:{self.mode.value}", {}))
        except (TypeError, ValueError):
            return RiskLimits()

    def _disarm(self, reason: str, lifecycle=Lifecycle.DISARMED):
        was = self.lifecycle
        self.lifecycle = lifecycle
        self._armed_fingerprint = None
        self._reviews.clear()
        self._nonces.clear()
        if was != lifecycle or reason:
            self.store.record_event(self.mode.value, "SESSION_DISARMED", {
                "reason": reason, "previous": was.value, "current": lifecycle.value,
            })

    def _metadata(self, environment=None):
        return self.store.credential_metadata(environment or self.mode.value)

    def status(self) -> dict:
        metadata = self._metadata() if self.mode.is_vantage else None
        connector = self.connector
        account = connector.account if connector and connector.account else None
        state = (
            connector.state.value if connector else
            ConnectionState.DISCONNECTED.value if metadata else ConnectionState.NOT_CONFIGURED.value
        )
        return {
            "mode": self.mode.value,
            "broker": "Vantage MT5" if self.mode.is_vantage else "AXIOM local paper",
            "server": (account.server if account else (metadata or {}).get("server")),
            "account": public_dict(account) if account else None,
            "account_login_masked": account.login_masked if account else (metadata or {}).get("login_masked"),
            "account_label": (metadata or {}).get("label") or None,
            "connection": {"state": state, "detail": connector.state_detail if connector else (
                "Credential saved; not connected" if metadata else "No Vantage credential configured"
            )},
            "connected": bool(connector and connector.connected),
            "lifecycle": self.lifecycle.value,
            "armed": self.lifecycle in {Lifecycle.ARMED, Lifecycle.ACTIVE, Lifecycle.PAUSED},
            "active": self.lifecycle == Lifecycle.ACTIVE,
            "paused": self.lifecycle == Lifecycle.PAUSED,
            "emergency_stop": self.emergency_stop,
            "automation_enabled": False,
            "automation_available": False,
            "selected_symbols": self.selected_symbols,
            "risk_limits": self.risk_limits.to_dict(),
            "credential": metadata,
            "last_reconciled_at": connector.last_reconciled_at if connector else None,
            "identity_fingerprint": connector.identity_fingerprint if connector else None,
            "paper_only": self.mode == SessionMode.PAPER,
            "live_trading": self.mode == SessionMode.LIVE and self.lifecycle == Lifecycle.ACTIVE,
            "profitability_claim": "none",
        }

    def select_mode(self, mode: SessionMode) -> dict:
        with self._lock:
            if mode == self.mode:
                return self.status()
            if self.connector:
                self.connector.disconnect()
                self.connector = None
            previous = self.mode
            self.mode = mode
            self.store.set_runtime("session_mode", mode.value)
            self._disarm(f"session mode changed from {previous.value} to {mode.value}")
            self.store.record_event(mode.value, "SESSION_MODE_SELECTED", {
                "previous": previous.value, "selected": mode.value,
                "connected": False, "armed": False, "active": False,
            })
            return self.status()

    def save_credentials(self, environment: SessionMode, values: dict, replace_existing=False) -> dict:
        if not environment.is_vantage:
            raise ServiceError("Credentials are separate for VANTAGE_DEMO and VANTAGE_LIVE")
        login = str(values.get("login") or "").strip()
        password = str(values.get("password") or "")
        server = str(values.get("server") or "").strip()
        path = str(values.get("terminal_path") or "").strip()
        label = str(values.get("label") or "").strip()
        if not login.isdigit() or len(login) > 19:
            raise ServiceError("MT5 login must be the numeric account login")
        if not password or len(password) > 512:
            raise ServiceError("A trading password is required when saving/replacing credentials")
        if not server or len(server) > 160 or any(ord(c) < 32 for c in server):
            raise ServiceError("Exact MT5 server is required")
        if len(path) > 500 or "\x00" in path or len(label) > 80:
            raise ServiceError("Terminal path or account label is invalid")
        if self.secret_store is None:
            raise ServiceError("OS-protected credential storage is unavailable")
        existing = self.store.credential_metadata(environment.value)
        if existing and not replace_existing:
            raise ServiceError("Credential exists; use the explicit Replace action")
        if environment == self.mode and self.connector:
            self.connector.disconnect()
            self.connector = None
        secret = {
            "login": login, "password": password, "server": server, "terminal_path": path,
        }
        self.secret_store.put(environment.value, secret)
        from app.vantage.models import mask_login
        self.store.save_credential_metadata(
            environment.value, login_masked=mask_login(login), server=server,
            terminal_path=path, label=label,
        )
        if environment == self.mode:
            self._disarm("broker credential/account/server changed")
        self.store.record_event(environment.value, "CREDENTIAL_REPLACED" if existing else "CREDENTIAL_SAVED", {
            "login_masked": mask_login(login), "server": server,
            "secret_storage": "Windows DPAPI", "connected": False,
        })
        return self.status() if environment == self.mode else {
            "environment": environment.value, "configured": True, "login_masked": mask_login(login),
        }

    def delete_credentials(self, environment: SessionMode) -> dict:
        if not environment.is_vantage:
            raise ServiceError("Invalid credential environment")
        if environment == self.mode and self.connector:
            self.connector.disconnect()
            self.connector = None
        if self.secret_store is not None:
            self.secret_store.delete(environment.value)
        self.store.delete_credential_metadata(environment.value)
        if environment == self.mode:
            self._disarm("broker credential deleted")
        self.store.record_event(environment.value, "CREDENTIAL_DELETED", {})
        return {"environment": environment.value, "configured": False}

    def test_connection(self) -> dict:
        """Read-only connection: initialize, identify, discover and reconcile only."""
        with self._lock:
            if not self.mode.is_vantage:
                raise ServiceError("Select VANTAGE DEMO or VANTAGE LIVE before testing MT5")
            metadata = self._metadata()
            if not metadata or self.secret_store is None:
                raise ServiceError("Vantage credential is not configured")
            self._disarm("new MT5 connection/test; prior activation is invalid")
            try:
                credentials = self.secret_store.get(self.mode.value)
            except SecretNotFound as exc:
                raise ServiceError("Protected Vantage credential is missing") from exc
            if self.connector:
                self.connector.disconnect()
            self.connector = MT5Connector(self.gateway_factory())
            account = self.connector.connect(credentials, self.mode)
            instruments = self.connector.discover()
            snapshot = self.reconcile(reason="read-only connection test")
            self.store.record_event(self.mode.value, "READ_ONLY_CONNECTION_TEST", {
                "account": account.login_masked, "server": account.server,
                "instrument_count": len(instruments), "orders_modified": False,
            })
            return {
                **self.status(), "read_only": True, "orders_modified": False,
                "instruments_discovered": len(instruments), "reconciled_at": snapshot["reconciled_at"],
            }

    def disconnect(self) -> dict:
        with self._lock:
            if self.connector:
                self.connector.disconnect()
            self._disarm("operator disconnected MT5")
            return self.status()

    def select_instruments(self, symbols: list[str]) -> dict:
        with self._lock:
            connector = self._connector()
            if not symbols or len(symbols) > 50 or len(set(symbols)) != len(symbols):
                raise ServiceError("Select 1-50 distinct broker symbols")
            if not connector.contracts:
                connector.discover()
            for symbol in symbols:
                if symbol not in connector.contracts:
                    raise ServiceError(f"{symbol!r} is not exposed by the connected account")
                connector.select_symbol(symbol)
            self.store.set_runtime(f"selected:{self.mode.value}", symbols)
            self._disarm("selected instruments changed")
            self.store.record_event(self.mode.value, "INSTRUMENTS_SELECTED", {"symbols": symbols})
            return {"selected": symbols, "status": self.status()}

    def instruments(self, query="", product_type="", selected_only=False) -> dict:
        connector = self._connector()
        if not connector.contracts:
            connector.discover()
        query = query.lower().strip()
        selected = set(self.selected_symbols)
        rows = []
        for contract in connector.contracts.values():
            if query and query not in f"{contract.symbol} {contract.display_name} {contract.path}".lower():
                continue
            if product_type and contract.product_type != product_type:
                continue
            if selected_only and contract.symbol not in selected:
                continue
            item = contract.to_dict()
            item["selected"] = contract.symbol in selected
            quote = connector.quotes.get(contract.symbol)
            item["quote"] = public_dict(quote) if quote else None
            rows.append(item)
        rows.sort(key=lambda item: (not item["selected"], not item["tradable"], item["symbol"]))
        return {
            "instruments": rows[:1000], "total_matching": len(rows),
            "account_specific": True, "source": "connected MT5 symbols_get",
            "selected": list(self.selected_symbols),
            "note": "Only instruments returned by this account are listed; incomplete products are untradable.",
        }

    def quotes(self) -> dict:
        connector = self._connector()
        rows, errors = {}, {}
        for symbol in self.selected_symbols:
            try:
                rows[symbol] = public_dict(connector.quote(symbol))
            except BrokerError as exc:
                errors[symbol] = str(exc)
        if errors:
            self._disarm("selected quote unavailable/invalid")
        return {
            "quotes": rows, "errors": errors, "generated_at": iso_now(),
            "source": "same Vantage MT5 connection as execution",
        }

    def chart(self, symbol: str, timeframe: str, limit: int) -> dict:
        connector = self._connector()
        if symbol not in self.selected_symbols:
            raise ServiceError("Select the instrument before requesting its execution-source chart")
        contract = connector.contract(symbol)
        bars = connector.rates(symbol, timeframe, limit)
        quote = connector.quote(symbol)
        return {
            "symbol": symbol, "timeframe": timeframe, "bars": bars, "count": len(bars),
            "quote": public_dict(quote), "product_type": contract.product_type,
            "is_cfd": contract.is_cfd, "source": "VANTAGE_MT5", "market_data": True,
            "simulated": self.mode == SessionMode.DEMO,
            "data_feed_label": f"{self.mode.value} · SAME MT5 ACCOUNT AS EXECUTION",
            "note": "Historical bars are broker data; quote freshness is evaluated separately for orders.",
            "generated_at": iso_now(),
        }

    def save_risk_limits(self, values: dict) -> dict:
        with self._lock:
            try:
                limits = RiskLimits(**values)
            except TypeError as exc:
                raise ServiceError("Unknown or missing risk-limit field") from exc
            issues = limits.issues()
            if issues:
                raise OrderBlocked(issues)
            self.store.set_runtime(f"risk:{self.mode.value}", limits.to_dict())
            self._disarm("risk limits changed")
            self.store.record_event(self.mode.value, "RISK_LIMITS_CHANGED", limits.to_dict())
            return {"risk_limits": limits.to_dict(), "status": self.status()}

    def _connector(self) -> MT5Connector:
        if not self.mode.is_vantage:
            raise ServiceError("This action requires a Vantage session; local paper remains separate")
        if not self.connector or not self.connector.connected:
            raise ServiceError("MT5 is disconnected; test/connect again and repeat readiness")
        return self.connector

    def _reconcile_modification(self, intent: dict, snapshot: dict) -> str:
        """Confirm SL/TP changes only from the resulting broker position."""
        request = intent.get("request") or {}
        ticket = str(request.get("position") or "")
        position = next(
            (item for item in snapshot["positions"] if item.get("position_id") == ticket), None,
        )
        proposed = {
            "stop_loss": float(request.get("sl") or 0.0) or None,
            "take_profit": float(request.get("tp") or 0.0) or None,
        }
        result = dict(intent.get("result") or {})
        result["broker_evidence"] = {
            "position": position, "proposed": proposed,
            "reconciled_at": snapshot["reconciled_at"],
        }
        if position is None:
            self.store.update_intent(
                intent["id"], status="UNKNOWN",
                reason="position is absent; protection modification cannot be confirmed",
                result=result,
            )
            return "UNKNOWN"
        matches = all(
            (position.get(field) is None and value is None)
            or (
                position.get(field) is not None and value is not None
                and abs(float(position[field]) - value) <= 1e-8
            )
            for field, value in proposed.items()
        )
        if matches:
            self.store.update_intent(
                intent["id"], status="MODIFIED",
                reason="broker position confirms the requested SL/TP values",
                result=result, confirmed_ts=snapshot["reconciled_at"],
            )
            return "MODIFIED"
        status = intent["status"] if intent["status"] in UNRESOLVED_ORDER_STATES else "UNKNOWN"
        self.store.update_intent(
            intent["id"], status=status,
            reason="broker position does not yet confirm the requested SL/TP values",
            result=result,
        )
        return status

    def reconcile(self, reason="operator/reconnect reconciliation") -> dict:
        connector = self._connector()
        snapshot = connector.snapshot()
        self.store.upsert_deals(self.mode.value, snapshot["deals"])
        working_by_id = {item["order_id"]: item for item in snapshot["orders"]}
        history_by_id = {item["order_id"]: item for item in snapshot["history_orders"]}
        deals_by_order: dict[str, list] = {}
        for deal in snapshot["deals"]:
            if deal.get("order_id"):
                deals_by_order.setdefault(str(deal["order_id"]), []).append(deal)
        unresolved_after = 0
        for intent in self.store.list_intents(self.mode.value, 2000):
            if intent["status"] in FINAL_ORDER_STATES:
                continue
            if intent.get("order_type") == "MODIFY_SLTP":
                status = self._reconcile_modification(intent, snapshot)
                if status in UNRESOLVED_ORDER_STATES:
                    unresolved_after += 1
                continue
            ticket = intent.get("broker_order")
            candidates = []
            if not ticket:
                for candidate in list(working_by_id.values()) + list(history_by_id.values()):
                    if candidate.get("comment") != intent["comment"] or not candidate.get("managed"):
                        continue
                    if candidate.get("symbol") != intent["symbol"] or candidate.get("side") != intent["side"]:
                        continue
                    if abs(float(candidate.get("volume_initial") or 0) - float(intent["volume"])) > 1e-8:
                        continue
                    candidates.append(candidate)
                unique = {item["order_id"]: item for item in candidates}
                if len(unique) > 1:
                    self.store.update_intent(intent["id"], status="UNRESOLVED", reason="multiple broker orders match the intent")
                    unresolved_after += 1
                    continue
                if len(unique) == 1:
                    ticket = next(iter(unique))
                    self.store.update_intent(intent["id"], broker_order=ticket, reason="strict unique broker match adopted")
            if not ticket:
                if intent["status"] in UNRESOLVED_ORDER_STATES:
                    unresolved_after += 1
                continue
            working = working_by_id.get(str(ticket))
            history = history_by_id.get(str(ticket))
            deals = deals_by_order.get(str(ticket), [])
            filled_volume = sum(float(deal.get("volume") or 0) for deal in deals)
            status = intent["status"]
            reason_text = intent.get("reason")
            if working:
                partial = filled_volume > 0 or (
                    float(working.get("volume_initial") or 0) > float(working.get("volume_current") or 0)
                )
                status = "PARTIALLY_FILLED" if partial else "PENDING"
                reason_text = "broker confirms working order"
            elif history:
                remote = history.get("state")
                if remote in {"CANCELLED", "REJECTED", "EXPIRED", "FILLED"}:
                    status = remote
                    reason_text = f"broker history confirms {remote.lower()}"
                elif remote == "PARTIAL" or filled_volume > 0:
                    status = "PARTIALLY_FILLED"
                    reason_text = "broker history confirms partial execution"
            elif deals:
                if filled_volume + 1e-8 >= float(intent["volume"]):
                    status = "FILLED"
                    reason_text = "broker deals confirm full execution"
                elif filled_volume > 0:
                    status = "PARTIALLY_FILLED"
                    reason_text = "broker deals confirm partial execution"
            if status in UNRESOLVED_ORDER_STATES:
                unresolved_after += 1
            result = dict(intent.get("result") or {})
            result.update({
                "broker_evidence": {
                    "working": working, "history": history, "deal_volume": filled_volume,
                    "reconciled_at": snapshot["reconciled_at"],
                }
            })
            if status != intent["status"] or ticket != intent.get("broker_order") or result != intent.get("result"):
                self.store.update_intent(
                    intent["id"], status=status, broker_order=str(ticket), reason=reason_text,
                    result=result, confirmed_ts=snapshot["reconciled_at"] if (working or history or deals) else None,
                )
        self.store.record_event(self.mode.value, "BROKER_RECONCILED", {
            "reason": reason, "positions": len(snapshot["positions"]),
            "working_orders": len(snapshot["orders"]), "deals": len(snapshot["deals"]),
            "unresolved_intents": unresolved_after,
        })
        snapshot["unresolved_intents"] = unresolved_after
        return snapshot

    def _risk_state(self, snapshot: dict) -> tuple[float, float]:
        account = snapshot["account"]
        equity = float(account["equity"])
        today = utcnow().date().isoformat()
        key = f"equity-risk:{self.mode.value}"
        state = self.store.get_runtime(key, {})
        current_unrealized = sum(
            float(position.get("profit") or 0) + float(position.get("swap") or 0)
            for position in snapshot["positions"]
        )
        today_net = 0.0
        nontrading_today = False
        for deal in snapshot["deals"]:
            if str(deal.get("timestamp") or "")[:10] != today:
                continue
            if deal.get("side") == "UNKNOWN":
                nontrading_today = True
            today_net += sum(float(deal.get(field) or 0) for field in ("profit", "commission", "swap", "fee"))
        if state.get("date") != today:
            if nontrading_today:
                # Deposits/withdrawals/balance operations make a synthesized
                # day-open baseline untrustworthy. Fail closed for this day.
                day_start = 0.0
            else:
                day_start = equity - today_net - current_unrealized
            state = {"date": today, "day_start_equity": day_start, "peak_equity": max(day_start, equity)}
        else:
            day_start = float(state.get("day_start_equity") or 0)
            state["peak_equity"] = max(float(state.get("peak_equity") or 0), equity)
        self.store.set_runtime(key, state)
        return day_start, float(state["peak_equity"])

    def _margin_exposure(self, snapshot: dict) -> float:
        connector = self._connector()
        total = float(snapshot["account"]["margin"])
        for order in snapshot["orders"]:
            volume = float(order.get("volume_current") or 0)
            if volume <= 0:
                continue
            quote = connector.quote(order["symbol"])
            total += connector.margin(
                order["side"], order["symbol"], volume,
                float(order.get("price") or (quote.ask if order["side"] == "BUY" else quote.bid)),
            )
        return total

    @staticmethod
    def _check_result(result: dict) -> list[str]:
        if result.get("retcode") in {0, 10009}:
            return []
        return [f"MT5 read-only precheck refused the request ({result.get('retcode')}): {result.get('comment') or 'no broker detail'}"]

    def _prepare(self, order: OrderInput, *, for_submit=False) -> dict:
        connector = self._connector()
        order.validate()
        if order.symbol not in self.selected_symbols:
            raise OrderBlocked("Instrument is not in the explicitly selected account instrument set")
        snapshot = self.reconcile(reason="order review/pre-submission reconciliation")
        contract = connector.contract(order.symbol, refresh=True)
        connector.select_symbol(order.symbol)
        quote = connector.quote(order.symbol)
        if order.position_id:
            return self._prepare_close(order, snapshot, contract, quote)
        normalized = normalize_order(order, contract, quote)
        account = connector.account
        estimated_price = normalized.limit_price or (quote.ask if order.side == "BUY" else quote.bid)
        estimated_margin = connector.margin(order.side, order.symbol, order.volume, estimated_price)
        exposure = self._margin_exposure(snapshot)
        day_start, peak = self._risk_state(snapshot)
        decision = evaluate_entry(
            normalized, contract, quote, account, self.risk_limits,
            positions=snapshot["positions"], working_orders=snapshot["orders"],
            current_margin_exposure=exposure, estimated_margin=estimated_margin,
            trades_today=self.store.trades_today(self.mode.value, utcnow().date().isoformat()),
            day_start_equity=day_start, peak_equity=peak,
        )
        issues = list(decision["issues"])
        if self.emergency_stop:
            issues.append("durable emergency stop is set")
        if self.lifecycle != Lifecycle.ACTIVE:
            issues.append("session is not activated; complete readiness, Arm and Activate")
        if self._armed_fingerprint != connector.identity_fingerprint:
            issues.append("armed account identity is absent or changed")
        comment = _comment(order.idempotency_key)
        request = connector.order_request(normalized, contract, quote, comment=comment)
        request["deviation"] = self.risk_limits.max_slippage_points
        precheck = connector.check(request)
        issues.extend(self._check_result(precheck))
        decision["issues"] = issues
        decision["allowed"] = not issues
        return {
            "order": normalized, "contract": contract, "quote": quote, "snapshot": snapshot,
            "request": request, "precheck": precheck, "decision": decision,
            "risk_reducing": False, "comment": comment,
        }

    def _prepare_close(self, order: OrderInput, snapshot, contract, quote) -> dict:
        connector = self._connector()
        position = next(
            (item for item in snapshot["positions"] if item["position_id"] == str(order.position_id)), None
        )
        if not position:
            raise OrderBlocked("Broker position is absent; reconcile before any close attempt")
        expected_side = "SELL" if position["side"] == "BUY" else "BUY"
        if order.side != expected_side or order.order_type != "MARKET":
            raise OrderBlocked("Close must be an opposite market order tied to the current broker position")
        if order.stop_loss is not None or order.take_profit is not None:
            raise OrderBlocked("A close request cannot attach new SL/TP")
        normalized = normalize_order(order, contract, quote)
        if normalized.volume > float(position["volume"]) + 1e-8:
            raise OrderBlocked("Close volume exceeds the broker-confirmed position volume")
        issues = list(self.risk_limits.issues())
        if not connector.account.trading_permitted:
            issues.append("account or terminal trading permission is disabled")
        age = quote.age_seconds()
        if age < 0 or age > self.risk_limits.max_quote_age_seconds:
            issues.append("quote is stale or from the future")
        if quote.spread_bps > self.risk_limits.max_spread_bps:
            issues.append("spread exceeds the configured cap")
        if contract.trade_mode == 0:
            issues.append("broker symbol is disabled")
        comment = _comment(order.idempotency_key)
        request = connector.close_request(position, normalized.volume, contract, quote, comment)
        request["deviation"] = self.risk_limits.max_slippage_points
        precheck = connector.check(request)
        issues.extend(self._check_result(precheck))
        decision = {
            "allowed": not issues, "issues": issues, "quote_age_seconds": age,
            "spread_bps": quote.spread_bps, "estimated_price": quote.ask if order.side == "BUY" else quote.bid,
            "estimated_margin": 0.0, "risk_reducing": True,
            "cost_note": "Close is position-ticket bound; broker deals determine actual costs and filled volume.",
        }
        return {
            "order": normalized, "contract": contract, "quote": quote, "snapshot": snapshot,
            "request": request, "precheck": precheck, "decision": decision,
            "risk_reducing": True, "comment": comment, "position": position,
        }

    def _public_review(self, review_id: str, prepared: dict) -> dict:
        order = prepared["order"]
        return {
            "review_id": review_id,
            "expires_at": (utcnow() + timedelta(seconds=30)).isoformat(),
            "broker": "Vantage MT5", "server": self.connector.account.server,
            "account": public_dict(self.connector.account), "mode": self.mode.value,
            "lifecycle": self.lifecycle.value, "emergency_stop": self.emergency_stop,
            "instrument": prepared["contract"].to_dict(), "order": asdict(order),
            "quote": public_dict(prepared["quote"]), "risk": prepared["decision"],
            "precheck": prepared["precheck"], "risk_reducing": prepared["risk_reducing"],
            "confirmation_phrase": self.confirmation_phrase(order),
            "note": "Review is read-only. Final submission repeats every check and requires the exact phrase.",
        }

    def review(self, order: OrderInput) -> dict:
        with self._lock:
            if self.store.get_intent(order.idempotency_key):
                raise OrderBlocked("This idempotency key already has a persisted intent; reconcile it instead of reviewing a duplicate")
            prepared = self._prepare(order)
            review_id = secrets.token_urlsafe(18)
            self._reviews[review_id] = {
                "expires": utcnow() + timedelta(seconds=30), "order": order,
                "fingerprint": self.connector.identity_fingerprint,
                "allowed": prepared["decision"]["allowed"], "risk_reducing": prepared["risk_reducing"],
            }
            self.store.record_event(self.mode.value, "ORDER_REVIEWED", {
                "review_id": review_id, "symbol": order.symbol, "side": order.side,
                "volume": order.volume, "allowed": prepared["decision"]["allowed"],
                "issues": prepared["decision"]["issues"], "risk_reducing": prepared["risk_reducing"],
            })
            return self._public_review(review_id, prepared)

    def confirmation_phrase(self, order: OrderInput) -> str:
        environment = "LIVE" if self.mode == SessionMode.LIVE else "DEMO"
        return f"CLOSE {environment} POSITION" if order.position_id else f"SUBMIT {environment} ORDER"

    def submit(self, order: OrderInput, review_id: str, confirmation: str) -> dict:
        with self._lock:
            order.validate()
            existing = self.store.get_intent(order.idempotency_key)
            if existing:
                same = (
                    existing.get("environment") == self.mode.value
                    and existing.get("symbol") == order.symbol
                    and existing.get("side") == order.side
                    and existing.get("order_type") == order.order_type
                    and abs(float(existing.get("volume") or 0) - order.volume) < 1e-12
                    and (existing.get("request") or {}).get("position") == (
                        int(order.position_id) if order.position_id else None
                    )
                )
                if not same:
                    raise OrderBlocked("Idempotency key was already used for a different session or order")
                return {
                    "duplicate": True, "submission_attempted": False, "order": existing,
                    "note": "Existing durable intent returned; no second MT5 request was sent.",
                }
            review = self._reviews.get(review_id)
            if not review or review["expires"] < utcnow() or review["order"] != order:
                raise OrderBlocked("Review is missing, expired, or does not match the final order")
            if not review["allowed"]:
                raise OrderBlocked("The displayed review had blockers; obtain a fresh passing review")
            if confirmation != self.confirmation_phrase(order):
                raise OrderBlocked(f"Type the exact confirmation phrase: {self.confirmation_phrase(order)}")
            connector = self._connector()
            if review["fingerprint"] != connector.identity_fingerprint:
                raise OrderBlocked("Account identity changed since review")
            prepared = self._prepare(order, for_submit=True)
            if not prepared["decision"]["allowed"]:
                raise OrderBlocked(prepared["decision"]["issues"])
            self._reviews.pop(review_id, None)
            intent = {
                "id": order.idempotency_key, "environment": self.mode.value,
                "symbol": order.symbol, "side": order.side, "order_type": order.order_type,
                "volume": order.volume, "limit_price": order.limit_price,
                "stop_loss": order.stop_loss, "take_profit": order.take_profit,
                "comment": prepared["comment"], "status": "INTENT_RECORDED",
                "broker_order": None, "broker_deal": None, "reason": "durable intent before submission",
                "request": prepared["request"], "result": {},
                "created_ts": now_iso(), "updated_ts": now_iso(), "confirmed_ts": None,
            }
            if not self.store.create_intent(intent):
                existing = self.store.get_intent(order.idempotency_key)
                return {"duplicate": True, "submission_attempted": False, "order": existing}
            self.store.update_intent(order.idempotency_key, status="SUBMISSION_ATTEMPTED")
            self.store.record_event(self.mode.value, "ORDER_SUBMISSION_ATTEMPTED", {
                "intent_id": order.idempotency_key, "symbol": order.symbol,
                "risk_reducing": prepared["risk_reducing"],
            })
            try:
                # Exactly one call. There is no exception/timeout retry anywhere.
                result = connector.send(prepared["request"])
            except Exception as exc:
                self.store.update_intent(
                    order.idempotency_key, status="UNKNOWN",
                    reason=f"ambiguous submission outcome ({type(exc).__name__}); reconcile before any new entry",
                    result={"error_type": type(exc).__name__},
                )
                self._disarm("ambiguous MT5 submission outcome")
                self.store.record_event(self.mode.value, "ORDER_OUTCOME_UNKNOWN", {
                    "intent_id": order.idempotency_key, "error_type": type(exc).__name__,
                })
                return {
                    "duplicate": False, "submission_attempted": True,
                    "order": self.store.get_intent(order.idempotency_key), "reconciled": False,
                }
            retcode = result.get("retcode")
            broker_order = result.get("order")
            broker_deal = result.get("deal")
            definite_rejects = {
                10006, 10013, 10014, 10015, 10016, 10017, 10018, 10019, 10020, 10021,
                10022, 10023, 10024, 10025, 10026, 10027, 10029, 10030, 10032, 10033,
                10034, 10035, 10036, 10038, 10039, 10040, 10041, 10042, 10043, 10044,
                10045, 10046,
            }
            if retcode in definite_rejects:
                status = "REJECTED"
            elif retcode == 10008:
                status = "PENDING"
            elif retcode in {10009, 10010}:
                status = "ACCEPTED_UNRECONCILED"
            else:
                status = "UNKNOWN"
            self.store.update_intent(
                order.idempotency_key, status=status, broker_order=broker_order,
                broker_deal=broker_deal, reason=f"MT5 retcode {retcode}: {result.get('comment') or ''}"[:500],
                result=result, confirmed_ts=iso_now() if status in {"PENDING", "REJECTED"} else None,
            )
            if status == "UNKNOWN":
                self._disarm(f"unrecognized/ambiguous MT5 retcode {retcode}")
            try:
                self.reconcile(reason="post-submission broker confirmation")
                reconciled = True
            except BrokerError as exc:
                if status in {"PENDING", "ACCEPTED_UNRECONCILED"}:
                    self.store.update_intent(
                        order.idempotency_key, status="UNKNOWN",
                        reason="submission acknowledged but immediate reconciliation failed",
                    )
                self._disarm("post-submission reconciliation failed")
                reconciled = False
            final = self.store.get_intent(order.idempotency_key)
            self.store.record_event(self.mode.value, "ORDER_BROKER_RESULT", {
                "intent_id": order.idempotency_key, "retcode": retcode,
                "status": final["status"], "reconciled": reconciled,
            })
            return {
                "duplicate": False, "submission_attempted": True, "order": final,
                "reconciled": reconciled, "account_state": self.connector.last_snapshot,
            }

    def cancel(self, intent_id: str, confirmation: str) -> dict:
        with self._lock:
            environment = "LIVE" if self.mode == SessionMode.LIVE else "DEMO"
            if confirmation != f"CANCEL {environment} ORDER":
                raise OrderBlocked(f"Type CANCEL {environment} ORDER to confirm")
            connector = self._connector()
            intent = self.store.get_intent(intent_id)
            if not intent or intent["environment"] != self.mode.value or not intent.get("broker_order"):
                raise OrderBlocked("No reconciled AXIOM broker order matches this cancel request")
            snapshot = self.reconcile(reason="pre-cancel reconciliation")
            ticket = str(intent["broker_order"])
            working = next((item for item in snapshot["orders"] if item["order_id"] == ticket), None)
            if not working:
                return {"order": self.store.get_intent(intent_id), "cancel_confirmed": False,
                        "note": "Order is not working in the latest broker snapshot"}
            if not working.get("managed"):
                raise OrderBlocked("Refusing to cancel an order not owned by AXIOM")
            self.store.update_intent(intent_id, status="CANCEL_ATTEMPTED", reason="one cancel request attempted")
            try:
                result = connector.cancel(ticket)
                self.store.update_intent(intent_id, result=result)
            except Exception as exc:
                self.store.update_intent(intent_id, status="CANCEL_UNKNOWN", reason="cancel outcome unknown")
                self._disarm("ambiguous cancel outcome")
                return {"order": self.store.get_intent(intent_id), "cancel_confirmed": False}
            snapshot = self.reconcile(reason="post-cancel confirmation")
            final = self.store.get_intent(intent_id)
            confirmed = final["status"] == "CANCELLED"
            if not confirmed:
                self._disarm("cancel was not broker-confirmed")
            return {
                "order": final, "cancel_confirmed": confirmed,
                "remaining_working_orders": snapshot["orders"],
                "note": "CANCELLED is shown only when broker history confirms it.",
            }

    def _prepare_position_modification(
        self, position_id: str, stop_loss: float | None, take_profit: float | None,
        idempotency_key: str,
    ) -> dict:
        connector = self._connector()
        if not str(position_id).isdigit() or int(position_id) <= 0:
            raise OrderBlocked("A positive MT5 position ticket is required")
        snapshot = self.reconcile(reason="position-modification review/pre-submission reconciliation")
        position = next(
            (item for item in snapshot["positions"] if item.get("position_id") == str(position_id)), None,
        )
        if not position:
            raise OrderBlocked("Broker position is absent; reconcile before any modification")
        if position["symbol"] not in self.selected_symbols:
            raise OrderBlocked("Select the position's instrument before modifying its protection")
        contract = connector.contract(position["symbol"], refresh=True)
        quote = connector.quote(position["symbol"])
        # Reuse the exact tick, stop-direction, support and stop-distance
        # validation as entries. The outgoing SLTP request contains no volume.
        protection = OrderInput(
            symbol=position["symbol"], side=position["side"], order_type="MARKET",
            volume=float(position["volume"]), stop_loss=stop_loss, take_profit=take_profit,
            idempotency_key=idempotency_key,
        )
        normalized = normalize_order(protection, contract, quote)
        issues = list(self.risk_limits.issues())
        if not connector.account.trading_permitted:
            issues.append("account or terminal trading permission is disabled")
        age = quote.age_seconds()
        if age < 0 or age > self.risk_limits.max_quote_age_seconds:
            issues.append("quote is stale or from the future")
        if quote.spread_bps > self.risk_limits.max_spread_bps:
            issues.append("spread exceeds the configured cap")
        if contract.trade_mode == 0:
            issues.append("broker symbol is disabled")
        if self.emergency_stop:
            issues.append("durable emergency stop is set; use a position close for risk reduction")
        if self.lifecycle != Lifecycle.ACTIVE:
            issues.append("session is not activated; protection modification requires an active session")
        if self._armed_fingerprint != connector.identity_fingerprint:
            issues.append("armed account identity is absent or changed")
        if (
            normalized.stop_loss == position.get("stop_loss")
            and normalized.take_profit == position.get("take_profit")
        ):
            issues.append("requested SL/TP values already match the broker position")
        request = connector.position_protection_request(
            position, normalized.stop_loss, normalized.take_profit,
        )
        return {
            "position": position, "contract": contract, "quote": quote,
            "order": normalized, "request": request,
            "decision": {
                "allowed": not issues, "issues": issues, "quote_age_seconds": age,
                "spread_bps": quote.spread_bps, "risk_reducing": False,
                "margin_change": 0.0,
                "cost_note": "SL/TP modification adds no intended exposure; broker state confirms the result.",
            },
        }

    def review_position_modification(
        self, position_id: str, stop_loss: float | None, take_profit: float | None,
        idempotency_key: str,
    ) -> dict:
        with self._lock:
            if self.store.get_intent(idempotency_key):
                raise OrderBlocked("This idempotency key already has a persisted intent; reconcile it")
            prepared = self._prepare_position_modification(
                position_id, stop_loss, take_profit, idempotency_key,
            )
            review_id = secrets.token_urlsafe(18)
            modification = {
                "position_id": str(position_id), "stop_loss": stop_loss,
                "take_profit": take_profit, "idempotency_key": idempotency_key,
            }
            self._reviews[review_id] = {
                "kind": "POSITION_SLTP", "expires": utcnow() + timedelta(seconds=30),
                "modification": modification, "allowed": prepared["decision"]["allowed"],
                "fingerprint": self.connector.identity_fingerprint,
                "original": {
                    "volume": prepared["position"]["volume"],
                    "side": prepared["position"]["side"],
                    "stop_loss": prepared["position"].get("stop_loss"),
                    "take_profit": prepared["position"].get("take_profit"),
                },
            }
            self.store.record_event(self.mode.value, "POSITION_MODIFICATION_REVIEWED", {
                "review_id": review_id, "position_id": str(position_id),
                "allowed": prepared["decision"]["allowed"],
                "issues": prepared["decision"]["issues"],
            })
            environment = "LIVE" if self.mode == SessionMode.LIVE else "DEMO"
            return {
                "review_id": review_id, "expires_at": (utcnow() + timedelta(seconds=30)).isoformat(),
                "broker": "Vantage MT5", "server": self.connector.account.server,
                "account": public_dict(self.connector.account), "mode": self.mode.value,
                "lifecycle": self.lifecycle.value, "emergency_stop": self.emergency_stop,
                "position": prepared["position"], "instrument": prepared["contract"].to_dict(),
                "proposed": modification, "quote": public_dict(prepared["quote"]),
                "risk": prepared["decision"], "request": prepared["request"],
                "confirmation_phrase": f"MODIFY {environment} POSITION",
                "note": "Read-only review. Submission repeats checks and is confirmed only from the resulting broker position.",
            }

    def submit_position_modification(
        self, position_id: str, stop_loss: float | None, take_profit: float | None,
        idempotency_key: str, review_id: str, confirmation: str,
    ) -> dict:
        with self._lock:
            modification = {
                "position_id": str(position_id), "stop_loss": stop_loss,
                "take_profit": take_profit, "idempotency_key": idempotency_key,
            }
            existing = self.store.get_intent(idempotency_key)
            if existing:
                request = existing.get("request") or {}
                same = (
                    existing.get("environment") == self.mode.value
                    and existing.get("order_type") == "MODIFY_SLTP"
                    and str(request.get("position") or "") == str(position_id)
                    and (float(request.get("sl") or 0.0) or None) == stop_loss
                    and (float(request.get("tp") or 0.0) or None) == take_profit
                )
                if not same:
                    raise OrderBlocked("Idempotency key was already used for a different request")
                return {
                    "duplicate": True, "submission_attempted": False, "order": existing,
                    "note": "Existing durable modification intent returned; no second MT5 request was sent.",
                }
            review = self._reviews.get(review_id)
            if (
                not review or review.get("kind") != "POSITION_SLTP"
                or review["expires"] < utcnow() or review["modification"] != modification
            ):
                raise OrderBlocked("Modification review is missing, expired, or does not match")
            environment = "LIVE" if self.mode == SessionMode.LIVE else "DEMO"
            phrase = f"MODIFY {environment} POSITION"
            if confirmation != phrase:
                raise OrderBlocked(f"Type the exact confirmation phrase: {phrase}")
            if not review["allowed"]:
                raise OrderBlocked("The displayed modification review had blockers")
            connector = self._connector()
            if review["fingerprint"] != connector.identity_fingerprint:
                raise OrderBlocked("Account identity changed since modification review")
            prepared = self._prepare_position_modification(
                position_id, stop_loss, take_profit, idempotency_key,
            )
            current = prepared["position"]
            original = review["original"]
            if any(
                current.get(field) != original.get(field)
                for field in ("volume", "side", "stop_loss", "take_profit")
            ):
                raise OrderBlocked("Position volume/side/SL/TP changed since review; obtain a fresh review")
            if not prepared["decision"]["allowed"]:
                raise OrderBlocked(prepared["decision"]["issues"])
            self._reviews.pop(review_id, None)
            intent = {
                "id": idempotency_key, "environment": self.mode.value,
                "symbol": current["symbol"], "side": current["side"],
                "order_type": "MODIFY_SLTP", "volume": current["volume"],
                "limit_price": None, "stop_loss": stop_loss, "take_profit": take_profit,
                "comment": _comment(idempotency_key), "status": "INTENT_RECORDED",
                "broker_order": None, "broker_deal": None,
                "reason": "durable modification intent before submission",
                "request": prepared["request"], "result": {},
                "created_ts": now_iso(), "updated_ts": now_iso(), "confirmed_ts": None,
            }
            if not self.store.create_intent(intent):
                return {
                    "duplicate": True, "submission_attempted": False,
                    "order": self.store.get_intent(idempotency_key),
                }
            self.store.update_intent(idempotency_key, status="MODIFICATION_ATTEMPTED")
            self.store.record_event(self.mode.value, "POSITION_MODIFICATION_ATTEMPTED", {
                "intent_id": idempotency_key, "position_id": str(position_id),
            })
            try:
                # Exactly one SLTP send; an unknown outcome is never retried.
                result = connector.send(prepared["request"])
            except Exception as exc:
                self.store.update_intent(
                    idempotency_key, status="UNKNOWN",
                    reason=f"ambiguous modification outcome ({type(exc).__name__}); reconcile",
                    result={"error_type": type(exc).__name__},
                )
                self._disarm("ambiguous MT5 position-modification outcome")
                return {
                    "duplicate": False, "submission_attempted": True,
                    "order": self.store.get_intent(idempotency_key), "reconciled": False,
                }
            retcode = result.get("retcode")
            definite_rejects = {
                10006, 10013, 10014, 10015, 10016, 10017, 10018, 10019, 10020, 10021,
                10022, 10023, 10024, 10025, 10026, 10027, 10029, 10030, 10032, 10033,
                10034, 10035, 10036, 10038, 10039, 10040, 10041, 10042, 10043, 10044,
                10045, 10046,
            }
            status = "REJECTED" if retcode in definite_rejects else (
                "MODIFICATION_ATTEMPTED" if retcode == 10009 else "UNKNOWN"
            )
            self.store.update_intent(
                idempotency_key, status=status,
                reason=f"MT5 retcode {retcode}: {result.get('comment') or ''}"[:500], result=result,
            )
            if status == "UNKNOWN":
                self._disarm(f"unrecognized/ambiguous MT5 modification retcode {retcode}")
            try:
                self.reconcile(reason="post-modification broker-position confirmation")
                reconciled = True
            except BrokerError:
                self.store.update_intent(
                    idempotency_key, status="UNKNOWN",
                    reason="modification response received but reconciliation failed",
                )
                self._disarm("post-modification reconciliation failed")
                reconciled = False
            final = self.store.get_intent(idempotency_key)
            self.store.record_event(self.mode.value, "POSITION_MODIFICATION_RESULT", {
                "intent_id": idempotency_key, "retcode": retcode,
                "status": final["status"], "reconciled": reconciled,
            })
            return {
                "duplicate": False, "submission_attempted": True,
                "order": final, "reconciled": reconciled,
            }

    def readiness(self, generate_challenge=True) -> dict:
        with self._lock:
            checks: list[ReadinessCheck] = []

            def add(key, passed, label, detail):
                checks.append(ReadinessCheck(key, bool(passed), label, detail))

            add("mode", self.mode.is_vantage, "Explicit Vantage environment",
                f"Selected mode: {self.mode.value}; PAPER cannot be armed as a broker session")
            connector = self.connector if self.mode.is_vantage else None
            connected = bool(connector and connector.connected and connector.account)
            add("connection", connected, "Verified MT5 connection",
                connector.state_detail if connector else "No verified MT5 connection")
            snapshot = None
            if connected:
                try:
                    snapshot = self.reconcile(reason="readiness")
                    account = connector.refresh_account()
                    expected = "DEMO" if self.mode == SessionMode.DEMO else "REAL"
                    identity_ok = account.account_trade_mode == expected and account.server == connector._expected_server
                    add("identity", identity_ok, "Exact server/account identity",
                        f"{account.server} · {account.login_masked} · {account.account_trade_mode}")
                    add("permission", account.trading_permitted, "Broker and terminal trading permission",
                        "Trading is permitted" if account.trading_permitted else "Account/terminal trading is disabled")
                except BrokerError as exc:
                    self._disarm("readiness connection/reconciliation failure")
                    add("identity", False, "Exact server/account identity", str(exc))
                    add("permission", False, "Broker and terminal trading permission", "Unavailable")
            else:
                add("identity", False, "Exact server/account identity", "Not verified")
                add("permission", False, "Broker and terminal trading permission", "Not verified")
            symbols = self.selected_symbols
            add("selection", bool(symbols), "Explicit instrument selection",
                ", ".join(symbols) if symbols else "Select at least one account-discovered instrument")
            quotes_ok, contracts_ok, clock_ok, market_ok = bool(symbols), bool(symbols), bool(symbols), bool(symbols)
            quote_details, contract_details = [], []
            if connected and symbols:
                for symbol in symbols:
                    try:
                        quote = connector.quote(symbol)
                        contract = connector.contract(symbol, refresh=True)
                        age = quote.age_seconds()
                        fresh = 0 <= age <= self.risk_limits.max_quote_age_seconds if self.risk_limits.max_quote_age_seconds else False
                        quotes_ok = quotes_ok and fresh
                        clock_ok = clock_ok and age >= -5
                        market_ok = market_ok and contract.trade_mode not in {0, None}
                        contracts_ok = contracts_ok and contract.metadata_complete
                        quote_details.append(f"{symbol}: {age:.2f}s")
                        if not contract.metadata_complete:
                            contract_details.append(f"{symbol}: {', '.join(contract.metadata_issues)}")
                    except BrokerError as exc:
                        quotes_ok = contracts_ok = clock_ok = market_ok = False
                        quote_details.append(f"{symbol}: {exc}")
            else:
                quote_details.append("No connected selected-instrument quotes")
            add("quotes", quotes_ok, "Recent valid quotes", "; ".join(quote_details))
            add("clock", clock_ok, "Clock sanity", "UTC and broker tick times must be ordered and within the configured freshness bound")
            add("market", market_ok, "Market/symbol availability",
                "Broker trade mode and fresh quote verified; next session transition is unavailable from MT5 Python symbol_info")
            add("contracts", contracts_ok, "Complete contract metadata",
                "; ".join(contract_details) if contract_details else "All selected contracts are normalized and understood" if symbols else "No selected contracts")
            risk_issues = self.risk_limits.issues()
            add("risk", not risk_issues, "Valid bounded risk limits", "; ".join(risk_issues) if risk_issues else "All required limits are explicitly bounded")
            if snapshot:
                account = snapshot["account"]
                funds_ok = account["equity"] > 0 and account["margin_free"] >= 0
                add("funds", funds_ok, "Funds and margin", f"Equity {account['equity']:.2f} {account['currency']}; free margin {account['margin_free']:.2f}")
                reconciled_ok = 0 <= _age(snapshot["reconciled_at"]) <= 30
                add("reconciliation", reconciled_ok, "Fresh broker reconciliation", snapshot["reconciled_at"])
                unresolved = self.store.unresolved(self.mode.value)
                add("unresolved", not unresolved, "No unresolved order intents",
                    f"{len(unresolved)} unresolved intent(s)" if unresolved else "No ambiguous submission/cancel intents")
                unexplained = [item for item in snapshot["positions"] + snapshot["orders"] if not item.get("managed")]
                add("explained", not unexplained, "No unexplained broker positions/orders",
                    f"{len(unexplained)} external/unmatched broker item(s); reconcile or flatten outside AXIOM before arming" if unexplained else "All open broker items are AXIOM-matched")
                day_start, peak = self._risk_state(snapshot)
                add("equity_baseline", day_start > 0 and peak > 0, "Daily loss/drawdown baseline",
                    f"Day start {day_start:.2f}; observed peak {peak:.2f}" if day_start > 0 else "Balance operations prevent a trustworthy day-start baseline")
            else:
                for key, label, detail in (
                    ("funds", "Funds and margin", "Unavailable"), ("reconciliation", "Fresh broker reconciliation", "Unavailable"),
                    ("unresolved", "No unresolved order intents", "Broker state unavailable"),
                    ("explained", "No unexplained broker positions/orders", "Broker state unavailable"),
                    ("equity_baseline", "Daily loss/drawdown baseline", "Unavailable"),
                ):
                    add(key, False, label, detail)
            add("emergency", not self.emergency_stop, "Emergency stop clear",
                "Durable emergency stop is SET" if self.emergency_stop else "Durable emergency stop is clear")
            try:
                lock_ok = bool(self.lock_check())
            except Exception:
                lock_ok = False
            add("instance_lock", lock_ok, "Single-instance writer lock", "Valid exclusive writer lock" if lock_ok else "Exclusive writer lock is not valid")
            audit_ok = self.store.ping()
            add("audit", audit_ok, "Audit persistence", "SQLite durable write verified" if audit_ok else "SQLite durable write failed")
            add("alerting", audit_ok, "Operator alerting", "Local Activity/Audit alerts available; no external delivery is claimed" if audit_ok else "Local alert persistence unavailable")
            add("automation", True, "Automation default", "Vantage automation is unavailable and OFF; arming never starts a strategy")
            ready = all(check.passed for check in checks)
            challenge = None
            if generate_challenge:
                challenge = secrets.token_urlsafe(18)
                self._nonces[challenge] = utcnow() + timedelta(seconds=60)
            return {
                "ready": ready, "checks": [check.to_dict() for check in checks],
                "blockers": [f"{check.label}: {check.detail}" for check in checks if not check.passed],
                "readiness_id": challenge, "challenge_expires_seconds": 60,
                "arm_phrase": f"ARM {'VANTAGE LIVE' if self.mode == SessionMode.LIVE else 'VANTAGE DEMO'}",
                "status": self.status(), "generated_at": iso_now(),
            }

    def _valid_nonce(self, value: str) -> bool:
        expiry = self._nonces.get(value)
        if not expiry or expiry < utcnow():
            return False
        self._nonces.pop(value, None)
        return True

    def arm(self, readiness_id: str, phrase: str) -> dict:
        with self._lock:
            expected = f"ARM {'VANTAGE LIVE' if self.mode == SessionMode.LIVE else 'VANTAGE DEMO'}"
            if phrase != expected:
                raise ServiceError(f"Type the exact phrase: {expected}")
            if not self._valid_nonce(readiness_id):
                raise ServiceError("Readiness challenge is missing or expired; run readiness again")
            current = self.readiness(generate_challenge=False)
            if not current["ready"]:
                raise OrderBlocked(current["blockers"])
            connector = self._connector()
            self.lifecycle = Lifecycle.ARMED
            self._armed_fingerprint = connector.identity_fingerprint
            activation_id = secrets.token_urlsafe(18)
            self._nonces[activation_id] = utcnow() + timedelta(seconds=60)
            phrase_next = f"ACTIVATE {'VANTAGE LIVE' if self.mode == SessionMode.LIVE else 'VANTAGE DEMO'}"
            self.store.record_event(self.mode.value, "SESSION_ARMED", {
                "account": connector.account.login_masked, "server": connector.account.server,
                "orders_submitted": 0,
            })
            return {
                "status": self.status(), "activation_id": activation_id,
                "activation_phrase": phrase_next, "orders_submitted": 0,
                "note": "Armed only. Activate is a separate action; no order was submitted.",
            }

    def activate(self, activation_id: str, phrase: str) -> dict:
        with self._lock:
            if self.lifecycle not in {Lifecycle.ARMED, Lifecycle.PAUSED}:
                raise ServiceError("A currently armed or paused session is required")
            expected = f"ACTIVATE {'VANTAGE LIVE' if self.mode == SessionMode.LIVE else 'VANTAGE DEMO'}"
            if phrase != expected:
                raise ServiceError(f"Type the exact phrase: {expected}")
            if not self._valid_nonce(activation_id):
                raise ServiceError("Activation challenge is missing or expired; Arm again")
            if not self._connector().identity_fingerprint == self._armed_fingerprint:
                self._disarm("identity changed before activation")
                raise ServiceError("Account identity changed; readiness and Arm are required again")
            current = self.readiness(generate_challenge=False)
            if not current["ready"]:
                raise OrderBlocked(current["blockers"])
            self.lifecycle = Lifecycle.ACTIVE
            self.store.record_event(self.mode.value, "SESSION_ACTIVATED", {
                "operator_confirmed": True, "orders_submitted": 0, "automation_enabled": False,
            })
            return {"status": self.status(), "orders_submitted": 0}

    def pause(self) -> dict:
        with self._lock:
            if self.lifecycle != Lifecycle.ACTIVE:
                raise ServiceError("Only an active session can be paused")
            self.lifecycle = Lifecycle.PAUSED
            activation_id = secrets.token_urlsafe(18)
            self._nonces[activation_id] = utcnow() + timedelta(seconds=60)
            self.store.record_event(self.mode.value, "SESSION_PAUSED", {
                "new_entries_blocked": True, "broker_state_still_reported": True,
            })
            return {
                "status": self.status(), "activation_id": activation_id,
                "activation_phrase": f"ACTIVATE {'VANTAGE LIVE' if self.mode == SessionMode.LIVE else 'VANTAGE DEMO'}",
                "note": "New entries are blocked. Working broker orders/positions are not assumed cancelled or closed.",
            }

    def _cancel_all_managed(self, reason: str) -> list[dict]:
        connector = self._connector()
        snapshot = self.reconcile(reason=reason)
        outcomes = []
        by_ticket = {item.get("broker_order"): item for item in self.store.list_intents(self.mode.value, 2000)}
        for order in snapshot["orders"]:
            if not order.get("managed"):
                outcomes.append({"order_id": order["order_id"], "cancelled": False, "note": "external order left untouched"})
                continue
            intent = by_ticket.get(order["order_id"])
            if intent:
                self.store.update_intent(intent["id"], status="CANCEL_ATTEMPTED", reason=reason)
            try:
                connector.cancel(order["order_id"])
                outcomes.append({"order_id": order["order_id"], "cancel_attempted": True})
            except Exception as exc:
                if intent:
                    self.store.update_intent(intent["id"], status="CANCEL_UNKNOWN", reason="cancel outcome unresolved")
                outcomes.append({"order_id": order["order_id"], "cancel_attempted": True, "resolved": False})
        final = self.reconcile(reason=f"{reason}: final broker state")
        return [{"outcomes": outcomes, "remaining_orders": final["orders"], "remaining_positions": final["positions"]}]

    def stop(self) -> dict:
        with self._lock:
            if self.lifecycle == Lifecycle.ACTIVE:
                self.lifecycle = Lifecycle.STOPPED
            else:
                self._disarm("operator stopped session", Lifecycle.STOPPED)
            self._armed_fingerprint = None
            results = []
            if self.connector and self.connector.connected:
                results = self._cancel_all_managed("operator Stop; one cancel attempt per managed working order")
            self.store.record_event(self.mode.value, "SESSION_STOPPED", {"broker_results": results})
            return {"status": self.status(), "broker_results": results,
                    "note": "Remaining orders/positions are broker-reported; Stop does not claim a flat account."}

    def emergency(self, reason="operator emergency stop") -> dict:
        with self._lock:
            # Durable halt comes FIRST. Every new-entry prepare reads this flag.
            self._set_emergency(True, reason)
            self.lifecycle = Lifecycle.STOPPED
            self._armed_fingerprint = None
            self._reviews.clear()
            self._nonces.clear()
            results = []
            if self.mode.is_vantage and self.connector and self.connector.connected:
                results = self._cancel_all_managed("durable emergency stop")
            self.store.record_event(self.mode.value, "EMERGENCY_STOP_SET", {
                "reason": reason, "durable": True, "broker_results": results,
            })
            return {"status": self.status(), "broker_results": results,
                    "note": "Emergency stop persists across restart. Remaining broker state is reported, not assumed flat."}

    def clear_emergency(self, phrase: str) -> dict:
        with self._lock:
            if phrase != "CLEAR EMERGENCY STOP":
                raise ServiceError("Type CLEAR EMERGENCY STOP to confirm")
            self._set_emergency(False, "operator explicitly cleared; still disarmed")
            self._disarm("emergency stop cleared; readiness and Arm still required")
            return self.status()

    def workspace(self) -> dict:
        """Return a truthful operator view even before MT5 is connected.

        Configuration and lifecycle pages must remain usable with no credential
        or terminal.  Empty collections here mean "no broker snapshot", not a
        flat account; ``reconciled_at=None`` and ``broker_state_available``
        make that distinction explicit for clients.
        """
        common = {
            "intents": self.store.list_intents(self.mode.value, 500),
            "simulated": self.mode == SessionMode.DEMO,
            "paper_only": False,
        }
        if not self.connector or not self.connector.connected:
            return {
                "status": self.status(), **common,
                "account": None, "positions": [], "orders": [],
                "history_orders": [], "deals": self.store.list_deals(self.mode.value, 500),
                "quotes": {}, "pnl": None, "reconciled_at": None,
                "history_start": None, "account_currency": None,
                "broker_state_available": False,
                "note": "No current broker snapshot; empty collections do not prove a flat account.",
            }
        connector = self.connector
        snapshot = connector.last_snapshot
        if not snapshot or _age(snapshot.get("reconciled_at")) > 15:
            snapshot = self.reconcile(reason="workspace refresh")
        return {
            "status": self.status(), **snapshot, **common,
            "deals": self.store.list_deals(self.mode.value, 500),
            "quotes": {symbol: public_dict(quote) for symbol, quote in connector.quotes.items()},
            "broker_state_available": True,
        }

    def activity(self, limit=200) -> dict:
        return {"events": self.store.list_events(None if self.mode.is_vantage else self.mode.value, limit),
                "note": "Redacted local audit. Secrets and full account logins are never exported."}
