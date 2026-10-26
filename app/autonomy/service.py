"""Daily operator-bound autonomous session orchestration.

The service is synchronous at its core so every state transition and broker
write can be tested deterministically.  Only the background worker calls
``execute_once``; the public ``scan_once`` API is strictly read-only and no
worker can create, Arm, or Activate a session.

State/invariant summary:
- UNCONFIGURED -> READY requires fresh daily read-only authorization + new risk.
- READY/HALTED/PAUSED -> ARMED requires an explicit operator request and preflight.
- ARMED -> ACTIVE requires a second explicit operator request and fresh preflight.
- Only ACTIVE can issue an in-memory permit.  No permit is serialized.
- HALTED/EMERGENCY_STOPPED never place or modify orders automatically.
- Daily loss blocks entries only; confirmed protection remains managed.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import statistics
import threading
from typing import Any, Callable
from uuid import uuid4

from app.autonomy.config import AutonomySettings
from app.config.settings import ROOT as APPLICATION_ROOT
from app.autonomy.feed import ExchangeCalendar, UpstoxCompletedBarFeed, completed_bar_is_fresh
from app.autonomy.kronos import KronosService
from app.autonomy.kronos_setup import KronosSetupManager
from app.autonomy.models import (
    AccountProfile, AccountSnapshot, Candidate, DailyRisk, ExecutionPermit, Fault,
    Instrument, Lifecycle, Position, ProtectedOrder, SessionMode, StrategyEvidence,
)
from app.autonomy.paper import PaperExecution
from app.autonomy.risk import (
    daily_loss_breached, floor_tick, portfolio_entry_check, stop_sized_quantity,
    trailing_stop,
)
from app.autonomy.store import AutonomyStore, redact
from app.autonomy.strategy import EvidenceRegistry, FAMILIES, generate_candidates
from app.autonomy.upstox import (
    AmbiguousSubmission, InstrumentCatalog, Quote, ReconciliationFailure, UpstoxAuthError,
    UpstoxLiveBroker, UpstoxReadOnly, UpstoxRecoveryReadOnly, UpstoxRejected,
)
from app.data.models import Candle, parse_timestamp


class SessionError(RuntimeError):
    pass


class NotReady(SessionError):
    pass


@dataclass
class Runtime:
    session_id: str
    mode: SessionMode
    instruments: dict[str, Instrument]
    reader: Any
    feed: Any
    calendar: Any
    executor: Any = None
    state: str = Lifecycle.UNCONFIGURED.value
    risk: DailyRisk | None = None
    authorized_day: str = ""
    auth_fingerprint: str = ""
    profile: AccountProfile | None = None
    account: AccountSnapshot | None = None
    entries_blocked: bool = False
    fault: str = ""
    fault_reason: str = ""
    daily_loss_noticed: bool = False
    bars: dict[str, list[Candle]] = field(default_factory=dict)
    forecasts: dict[str, Any] = field(default_factory=dict)
    diagnostics: list[dict] = field(default_factory=list)
    working: set[str] = field(default_factory=set)
    processed: dict[str, datetime] = field(default_factory=dict)
    scanned: dict[str, datetime] = field(default_factory=dict)
    correlations: dict[tuple[str, str], float] = field(default_factory=dict)
    orders: dict[str, dict] = field(default_factory=dict)
    last_scan: datetime | None = None
    last_scan_kind: str = ""
    scan_count: int = 0
    recovered: bool = False
    recovery_reconciled: bool = False
    recovery_error: str = ""
    emergency: dict = field(default_factory=dict)
    # Emergency sets this event without waiting for the scan/service lock.
    # Only actual broker mutations use order_lock, so a slow feed read does not
    # block risk-reducing cancellation.
    stop_requested: threading.Event = field(default_factory=threading.Event, repr=False)
    control_lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    order_lock: threading.RLock = field(default_factory=threading.RLock, repr=False)


def _token_from_environment() -> str:
    # Read on every request so an operator's newly generated daily token is
    # used without persisting it in the autonomy journal or API responses.
    return os.getenv("UPSTOX_ACCESS_TOKEN", "") or os.getenv("MARKET_DATA_ACCESS_TOKEN", "")


def _default_public_ip() -> str:
    # This is deliberately an activation-only, non-market-data request.  It is
    # never used as a quote or execution provider.  Failure blocks LIVE.
    import httpx
    try:
        response = httpx.get("https://api.ipify.org", timeout=5.0)
        if response.status_code != 200:
            return ""
        return str(ipaddress.ip_address(response.text.strip()))
    except (httpx.HTTPError, ValueError):
        return ""


class AutonomyService:
    def __init__(self, settings: AutonomySettings, store: AutonomyStore,
                 catalog: InstrumentCatalog, *, data_dir: Path,
                 reader_factory: Callable | None = None,
                 live_factory: Callable | None = None,
                 recovery_factory: Callable | None = None,
                 feed_factory: Callable | None = None,
                 token_provider: Callable[[], str] | None = None,
                 public_ip_provider: Callable[[], str] | None = None,
                 kronos: KronosService | None = None,
                 evidence: EvidenceRegistry | None = None,
                 clock: Callable[[], datetime] | None = None):
        self.settings = settings
        self.store = store
        self.catalog = catalog
        self.data_dir = data_dir
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.token_provider = token_provider or _token_from_environment
        self.public_ip_provider = public_ip_provider or _default_public_ip
        self.reader_factory = reader_factory or (
            lambda provider: UpstoxReadOnly(provider, clock=self.clock)
        )
        self.live_factory = live_factory or (
            lambda provider: UpstoxLiveBroker(
                provider, algo_name=settings.upstox_algo_name,
                write_rate_per_second=settings.write_rate_per_second, clock=self.clock,
            )
        )
        self.recovery_factory = recovery_factory or (
            lambda provider: UpstoxRecoveryReadOnly(provider, clock=self.clock)
        )
        self.feed_factory = feed_factory or (
            lambda reader: UpstoxCompletedBarFeed(reader, clock=self.clock)
        )
        manifest_path = Path(settings.kronos_manifest)
        if not manifest_path.is_absolute():
            manifest_path = APPLICATION_ROOT / manifest_path
        artifact_root = Path(settings.kronos_root)
        if not artifact_root.is_absolute() and settings.kronos_root == "data/autonomy/kronos":
            artifact_root = data_dir / "kronos"
        vendor_root = Path(__file__).resolve().parent / "vendor" / "kronos"
        evidence_path = Path(settings.evidence_dir)
        if not evidence_path.is_absolute() and settings.evidence_dir == "data/autonomy/evidence":
            evidence_path = data_dir / "evidence"
        self.kronos = kronos or KronosService(
            manifest_path, artifact_root, vendor_root, application_root=APPLICATION_ROOT,
        )
        self.evidence = evidence or EvidenceRegistry.load(evidence_path)
        self.kronos_status = self.kronos.verify()
        self.kronos_setup = KronosSetupManager(
            self.kronos, audit=self.store.audit, on_status=self._kronos_updated,
        )
        self.runtimes: dict[str, Runtime] = {}
        self._lock = threading.RLock()
        self._closed = False
        self._restore_sessions()

    def _kronos_updated(self, status: dict) -> None:
        # Atomic reference update only; setup work never holds the session lock.
        self.kronos_status = status

    def kronos_view(self) -> dict:
        result = self.kronos_setup.status()
        result["audit_events"] = [
            event for event in self.store.events(limit=300)
            if event["session_id"] is None and event["event_type"].startswith("KRONOS_")
        ][:30]
        return result

    def kronos_install(self) -> dict:
        result = self.kronos_setup.start_install()
        self.kronos_status = self.kronos.status()
        return result

    def kronos_self_test(self) -> dict:
        result = self.kronos_setup.start_self_test()
        self.kronos_status = self.kronos.status()
        return result

    def kronos_python(self, executable: str | None) -> dict:
        result = self.kronos_setup.select_python(executable)
        self.kronos_status = self.kronos.status()
        return result

    def kronos_cancel(self) -> dict:
        result = self.kronos_setup.cancel()
        self.kronos_status = self.kronos.status()
        return result

    def now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None:
            raise ValueError("Autonomy clock must be timezone-aware")
        return value.astimezone(timezone.utc)

    def day(self) -> str:
        from app.autonomy.upstox import IST
        return self.now().astimezone(IST).date().isoformat()

    @staticmethod
    def _instrument_from_snapshot(raw: dict) -> Instrument:
        values = {key: value for key, value in raw.items() if key in Instrument.__dataclass_fields__}
        if values.get("expiry"):
            values["expiry"] = parse_timestamp(values["expiry"])
        item = Instrument(**values)
        item.validate()  # Expired contracts remain visible, but cannot later pass readiness.
        return item

    @staticmethod
    def _account_from_snapshot(raw: dict | None) -> AccountSnapshot | None:
        if raw is None:
            return None
        return AccountSnapshot(
            equity=float(raw["equity"]), available_margin=float(raw["available_margin"]),
            used_margin=float(raw["used_margin"]), day_pnl=float(raw["day_pnl"]),
            positions=tuple(Position(**item) for item in raw.get("positions") or []),
            observed_at=parse_timestamp(raw["observed_at"]),
            authoritative=bool(raw["authoritative"]),
        )

    @staticmethod
    def _account_snapshot(account: AccountSnapshot | None) -> dict | None:
        if account is None:
            return None
        return {
            "equity": account.equity, "available_margin": account.available_margin,
            "used_margin": account.used_margin, "day_pnl": account.day_pnl,
            "authoritative": account.authoritative, "observed_at": account.observed_at.isoformat(),
            "positions": [position.public() for position in account.positions],
        }

    @staticmethod
    def _order_view(record: dict) -> dict:
        """Allowlist display/recovery fields; never copy arbitrary broker identity."""
        fields = (
            "gtt_order_id", "type", "instrument_token", "quantity", "product",
            "simulated", "created_at", "updated_at",
        )
        rule_fields = (
            "strategy", "status", "trigger_type", "trigger_price", "transaction_type",
            "order_id", "quantity", "filled_quantity", "average_price",
        )

        def safe_values(raw: dict, allowed: tuple[str, ...]) -> dict:
            return {
                key: raw[key] for key in allowed
                if key in raw and (raw[key] is None or isinstance(raw[key], (str, int, float, bool)))
            }

        result = safe_values(record, fields)
        result["rules"] = [
            safe_values(rule, rule_fields)
            for rule in record.get("rules") or [] if isinstance(rule, dict)
        ]
        return result

    def _persist(self, runtime: Runtime) -> None:
        paper_state = None
        if runtime.mode == "PAPER" and runtime.executor is not None:
            paper_state = runtime.executor.snapshot()
        self.store.save_runtime(runtime.session_id, {
            "schema": "axiom.autonomy.runtime-view.v1",
            "mode": runtime.mode,
            "instruments": [item.public() for item in runtime.instruments.values()],
            "account": self._account_snapshot(runtime.account),
            "paper_state": paper_state,
            "orders": [self._order_view(record) for record in runtime.orders.values()],
            "working": sorted(runtime.working),
            "processed": {key: value.isoformat() for key, value in runtime.processed.items()},
            "scanned": {key: value.isoformat() for key, value in runtime.scanned.items()},
            "diagnostics": runtime.diagnostics[-1000:],
            "scan_count": runtime.scan_count,
            "last_scan": runtime.last_scan.isoformat() if runtime.last_scan else None,
            "last_scan_kind": runtime.last_scan_kind,
            "emergency": runtime.emergency,
        })

    def _restore_sessions(self) -> None:
        """Load durable views only; never restore approval or a LIVE executor."""
        for row in self.store.sessions():
            session_id = row["session_id"]
            snapshot = self.store.runtime_snapshot(session_id) or {}
            reader = self.reader_factory(self.token_provider)
            runtime = Runtime(
                session_id=session_id, mode=row["mode"], instruments={}, reader=reader,
                feed=self.feed_factory(reader), calendar=ExchangeCalendar(reader, clock=self.clock),
                state=Lifecycle.HALTED.value, recovered=True, recovery_reconciled=False,
            )
            runtime.stop_requested.set()
            self.runtimes[session_id] = runtime
            try:
                if snapshot and snapshot.get("schema") != "axiom.autonomy.runtime-view.v1":
                    raise ValueError("Unsupported durable runtime-view schema")
                runtime.instruments = {
                    item.instrument_key: item
                    for item in (self._instrument_from_snapshot(raw)
                                 for raw in snapshot.get("instruments") or [])
                }
                runtime.account = self._account_from_snapshot(snapshot.get("account"))
                runtime.orders = {
                    str(record["gtt_order_id"]): record
                    for record in snapshot.get("orders") or [] if record.get("gtt_order_id")
                }
                runtime.working = set(snapshot.get("working") or [])
                runtime.processed = {key: parse_timestamp(value)
                                     for key, value in (snapshot.get("processed") or {}).items()}
                runtime.scanned = {key: parse_timestamp(value)
                                   for key, value in (snapshot.get("scanned") or {}).items()}
                runtime.diagnostics = list(snapshot.get("diagnostics") or [])
                runtime.scan_count = int(snapshot.get("scan_count") or 0)
                runtime.last_scan = parse_timestamp(snapshot["last_scan"]) if snapshot.get("last_scan") else None
                runtime.last_scan_kind = str(snapshot.get("last_scan_kind") or "")
                runtime.emergency = dict(snapshot.get("emergency") or {})
                if runtime.mode == "PAPER":
                    paper_snapshot = snapshot.get("paper_state")
                    if paper_snapshot:
                        runtime.executor = PaperExecution.from_snapshot(runtime.instruments, paper_snapshot)
                    elif self.store.intents(session_id) or (runtime.account and runtime.account.positions):
                        raise ValueError("Paper positions/orders exist without a durable paper snapshot")
                    else:
                        runtime.executor = PaperExecution(
                            runtime.instruments, starting_equity=self.settings.paper_starting_equity,
                            commission_rate=self.settings.paper_commission_rate,
                            slippage_rate=self.settings.paper_slippage_rate,
                            spread_rate=self.settings.paper_spread_rate,
                            max_participation=self.settings.max_participation,
                        )
                    if runtime.executor is not None and runtime.account is not None:
                        expected = {p.instrument_key: p.quantity for p in runtime.account.positions}
                        actual = {key: p.quantity for key, p in runtime.executor.positions.items()}
                        if expected != actual:
                            raise ValueError("Durable paper account and position snapshots disagree")
                    known_intents = set(runtime.executor.intent_for_id.values()) if runtime.executor else set()
                    for intent in self._active_intents(runtime):
                        ids = json.loads(intent["broker_ids_json"])
                        if ids and intent["intent_id"] not in known_intents:
                            raise ValueError("Durable paper intent lacks its GTT/protection snapshot")
                if not snapshot:
                    runtime.recovery_error = (
                        "Legacy session has no durable runtime view; intents remain available, "
                        "but execution recovery is blocked"
                    )
            except (KeyError, TypeError, ValueError) as exc:
                runtime.executor = None
                runtime.recovery_error = f"Recovery snapshot is incomplete or inconsistent: {exc}"
            runtime.fault = Fault.RECOVERY.value
            runtime.fault_reason = runtime.recovery_error or (
                "Recovered read-only state; fresh authorization, risk, reconciliation, Arm and Activate required"
            )
            runtime.entries_blocked = True
            self.store.clear_session_approval(session_id)
            self.store.update_session(
                session_id, state=Lifecycle.HALTED.value,
                interruption_reason="RECOVERY: durable read-only view; fresh operator authorization required",
            )
            self.store.audit("SESSION_RESTORED_READ_ONLY", {
                "prior_state": row["state"], "execution_adapter_loaded": runtime.executor is not None,
                "recovery_error": runtime.recovery_error,
            }, session_id)
            self._persist(runtime)

    def _public_intents(self, session_id: str) -> list[dict]:
        result = []
        for row in self.store.intents(session_id):
            result.append({
                "intent_id": row["intent_id"], "signal_id": row["signal_id"],
                "instrument_key": row["instrument_key"], "state": row["state"],
                "broker_ids": json.loads(row["broker_ids_json"]),
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"], "updated_at": row["updated_at"],
                "recovered_view": self._runtime(session_id).recovered and not self._runtime(session_id).recovery_reconciled,
            })
        return result

    def create_session(self, mode: str, instrument_keys: list[str]) -> dict:
        with self._lock:
            if mode not in {"PAPER", "UPSTOX_LIVE"}:
                raise SessionError("Mode must be PAPER or UPSTOX_LIVE")
            if mode == "UPSTOX_LIVE" and not self.settings.live_enabled:
                raise SessionError("LIVE is disabled by AUTONOMY_LIVE_ENABLED")
            keys = list(dict.fromkeys(instrument_keys))
            if not keys or len(keys) > 500:
                raise SessionError("Select at least one exact instrument key (maximum 500 scanned keys)")
            instruments = {}
            for key in keys:
                item = self.catalog.instruments.get(key)
                if item is None:
                    raise SessionError(f"Unknown exact Upstox instrument key: {key}")
                item.validate(now=self.now())
                instruments[key] = item
            session_id = uuid4().hex
            reader = self.reader_factory(self.token_provider)
            runtime = Runtime(
                session_id=session_id, mode=mode, instruments=instruments, reader=reader,
                feed=self.feed_factory(reader), calendar=ExchangeCalendar(reader, clock=self.clock),
            )
            if mode == "PAPER":
                runtime.executor = PaperExecution(
                    instruments, starting_equity=self.settings.paper_starting_equity,
                    commission_rate=self.settings.paper_commission_rate,
                    slippage_rate=self.settings.paper_slippage_rate,
                    spread_rate=self.settings.paper_spread_rate,
                    max_participation=self.settings.max_participation,
                )
            self.runtimes[session_id] = runtime
            self.store.create_session(session_id, self.day(), mode)
            self.store.audit("SESSION_BOUNDARY", {
                "mode": mode, "paper_only": mode == "PAPER",
                "live_execution_enabled": mode == "UPSTOX_LIVE" and self.settings.live_enabled,
                "instrument_keys": keys,
            }, session_id)
            self._persist(runtime)
            return self.public(session_id)

    def _runtime(self, session_id: str) -> Runtime:
        try:
            return self.runtimes[session_id]
        except KeyError:
            raise SessionError("Unknown or non-loaded autonomy session; recovery requires a new session") from None

    def _set_state(self, runtime: Runtime, state: str, *, reason: str | None = None) -> None:
        runtime.state = state
        self.store.update_session(runtime.session_id, state=state, interruption_reason=reason)
        self.store.audit("STATE", {"state": state, "reason": reason or ""}, runtime.session_id)

    def authorize(self, session_id: str) -> dict:
        """Refresh daily read-only authorization; request body contains no secret."""
        with self._lock:
            runtime = self._runtime(session_id)
            if runtime.state in {Lifecycle.ACTIVE.value, Lifecycle.ARMED.value}:
                raise SessionError("Pause and reconfigure before changing daily authorization")
            token = self.token_provider().strip()
            if not token:
                raise UpstoxAuthError("Configure a fresh daily Upstox token locally, then retry")
            profile = runtime.reader.profile(include_static_ips=runtime.mode == "UPSTOX_LIVE")
            if not profile.active:
                raise UpstoxAuthError("Upstox profile is not active")
            if runtime.mode == "UPSTOX_LIVE" and runtime.executor is None:
                runtime.executor = self.recovery_factory(self.token_provider)
            runtime.authorized_day = self.day()
            runtime.auth_fingerprint = hashlib.sha256(token.encode()).hexdigest()
            runtime.profile = profile
            self.store.update_session(session_id, auth_fingerprint=runtime.auth_fingerprint)
            self.store.audit("DAILY_READ_AUTHORIZED", {
                "day": runtime.authorized_day, "profile_active": True,
                "static_ip_entries": len(profile.static_ips),
            }, session_id)
            self._persist(runtime)
            return self.public(session_id)

    def set_risk(self, session_id: str, *, max_daily_loss: float,
                 per_trade_risk_fraction: float, equity_at_entry: float | None = None) -> dict:
        with self._lock:
            runtime = self._runtime(session_id)
            if runtime.state in {Lifecycle.ACTIVE.value, Lifecycle.ARMED.value}:
                raise SessionError("Risk is immutable while ARMED or ACTIVE")
            if runtime.recovery_error:
                raise NotReady(runtime.recovery_error)
            if runtime.authorized_day != self.day():
                raise NotReady("Fresh daily read-only authorization is required before risk entry")
            if runtime.mode == "UPSTOX_LIVE":
                if runtime.executor is None:
                    raise NotReady("Fresh read-only authorization/account access is required")
                account = runtime.executor.account()
                equity = account.equity
                if equity_at_entry is not None and not math.isclose(equity_at_entry, equity, rel_tol=0.01):
                    raise SessionError("LIVE risk equity comes from Upstox, not an entered approximation")
            else:
                if equity_at_entry is None:
                    raise SessionError("Enter paper starting equity explicitly; no prior-day value is reused")
                if runtime.executor is None:
                    raise NotReady("Recovered paper execution state is unavailable")
                equity = float(equity_at_entry)
                if runtime.recovered:
                    current_equity = runtime.executor.account().equity
                    if not math.isclose(equity, current_equity, rel_tol=0.01):
                        raise SessionError(
                            "Recovered paper risk equity must match the restored account; cash/positions are not reset"
                        )
                else:
                    runtime.executor.starting_equity = equity
                    runtime.executor.cash = equity
            risk = DailyRisk(self.day(), float(max_daily_loss), float(per_trade_risk_fraction),
                             float(equity), self.now())
            risk.validate()
            runtime.risk = risk
            runtime.entries_blocked = runtime.stop_requested.is_set()
            runtime.daily_loss_noticed = False
            self.store.update_session(session_id, state=Lifecycle.READY.value, risk=risk.public())
            runtime.state = Lifecycle.READY.value
            self.store.audit("DAILY_RISK_ENTERED", risk.public(), session_id)
            self._persist(runtime)
            return self.public(session_id)

    def _ensure_live_executor(self, runtime: Runtime) -> Any:
        if runtime.mode != "UPSTOX_LIVE":
            return runtime.executor
        if not self.settings.live_enabled:
            raise NotReady("LIVE master switch is disabled")
        if not self.settings.upstox_algo_name:
            raise NotReady("AUTONOMY_UPSTOX_ALGO_NAME is required for LIVE")
        if runtime.executor is None or not getattr(
                runtime.executor, "broker_mutations_reachable", False):
            runtime.executor = self.live_factory(self.token_provider)
        return runtime.executor

    @staticmethod
    def _permission_blockers(runtime: Runtime, profile: AccountProfile | None) -> list[str]:
        """Validate every route the autonomous long/short strategy may use."""
        if profile is None or not profile.active:
            return ["Active Upstox profile required for route permissions"]
        blockers: list[str] = []
        # The three-leg route contains limit entry/target behavior and a
        # protective stop.  Merely collecting order_types is not a check.
        required_order_types = {"LIMIT", "SL"}
        missing_types = sorted(required_order_types - {str(item).upper() for item in profile.order_types})
        if missing_types:
            blockers.append("Account order-type permission missing: " + ", ".join(missing_types))
        exchanges = {str(item).upper() for item in profile.exchanges}
        products = {str(item).upper() for item in profile.products}
        for item in runtime.instruments.values():
            if item.exchange.upper() not in exchanges:
                blockers.append(f"Account exchange permission missing: {item.exchange}")
            if item.product.upper() not in products:
                blockers.append(f"Account product permission missing: {item.product}")
            if not item.currency_derivative:
                if item.product != "I" or not item.short_route_metadata:
                    blockers.append(
                        f"Short-selling route is not validated for {item.instrument_key}: exact MIS metadata and product I required"
                    )
        return list(dict.fromkeys(blockers))

    def readiness(self, session_id: str) -> dict:
        runtime = self._runtime(session_id)
        blockers: list[str] = []
        if runtime.recovery_error:
            blockers.append(runtime.recovery_error)
        if runtime.recovered and not runtime.recovery_reconciled:
            blockers.append("Recovered session requires successful read-only reconciliation")
        if runtime.emergency.get("cleanup_status") in {"IN_PROGRESS", "UNRESOLVED", "ADAPTER_UNAVAILABLE"}:
            blockers.append("Emergency stop has unresolved managed-order cleanup")
        if runtime.authorized_day != self.day():
            blockers.append("Fresh daily Upstox read-only authorization required")
        if runtime.risk is None or runtime.risk.trading_day != self.day():
            blockers.append("New daily maximum loss and per-trade risk required")
        if not self.catalog.fresh_for_day(self.day()):
            blockers.append("Current-day Upstox BOD instrument catalog required")
        for item in runtime.instruments.values():
            try:
                item.validate(now=self.now())
            except ValueError as exc:
                blockers.append(str(exc))
        if runtime.profile is None or not runtime.profile.active:
            blockers.append("Active Upstox profile required")
        if runtime.mode == "UPSTOX_LIVE":
            if not self.settings.live_enabled:
                blockers.append("LIVE master switch disabled")
            if not self.settings.upstox_algo_name:
                blockers.append("Configured Upstox Algo Name required")
            if not self.settings.expected_public_ip:
                blockers.append("Expected registered static public IP required")
            if runtime.profile and self.settings.expected_public_ip:
                try:
                    expected = str(ipaddress.ip_address(self.settings.expected_public_ip))
                    if expected not in runtime.profile.static_ips:
                        blockers.append("Configured IP is not in Upstox registered static IPs")
                except ValueError:
                    blockers.append("Configured static IP is invalid")
            blockers.extend(self._permission_blockers(runtime, runtime.profile))
        if self.store.unresolved():
            blockers.append("Unresolved broker outcomes require reconciliation before activation")
        return {"ready": not blockers, "blockers": list(dict.fromkeys(blockers)),
                "checked_at": self.now().isoformat()}

    def arm(self, session_id: str) -> dict:
        with self._lock:
            runtime = self._runtime(session_id)
            with runtime.control_lock:
                if runtime.state == Lifecycle.ACTIVE.value:
                    raise SessionError("Session is already ACTIVE")
                readiness = self.readiness(session_id)
                if not readiness["ready"]:
                    raise NotReady("; ".join(readiness["blockers"]))
                # Only this explicit operator action clears a prior emergency
                # preemption event.  Emergency uses the same short control
                # lock, so a concurrent stop cannot be erased here.
                runtime.stop_requested.clear()
                runtime.entries_blocked = False
                runtime.fault = ""
                runtime.fault_reason = ""
                runtime.emergency = {}
                self._set_state(runtime, Lifecycle.ARMED.value)
                self.store.audit("OPERATOR_ARM", {"automatic": False}, session_id)
                self._persist(runtime)
                return self.public(session_id)

    def refresh_catalog(self) -> dict:
        if any(runtime.state in {Lifecycle.ARMED.value, Lifecycle.ACTIVE.value}
               for runtime in self.runtimes.values()):
            raise SessionError("Pause/halt sessions before replacing contract metadata")
        return self.catalog.refresh()

    def _preflight(self, runtime: Runtime) -> AccountSnapshot:
        if self.store.unresolved(runtime.session_id):
            raise NotReady("Unresolved broker outcomes require reconciliation before activation")
        if runtime.mode == "UPSTOX_LIVE":
            executor = self._ensure_live_executor(runtime)
            # A fresh profile read is intentional: a quote connection alone is
            # never treated as execution permission.
            profile = runtime.reader.profile(include_static_ips=True)
            if not profile.active:
                raise NotReady("Upstox profile became inactive")
            runtime.profile = profile
            permission_blockers = self._permission_blockers(runtime, profile)
            if permission_blockers:
                raise NotReady("; ".join(permission_blockers))
            expected = str(ipaddress.ip_address(self.settings.expected_public_ip))
            if expected not in profile.static_ips:
                raise NotReady("Registered static IP does not match configuration")
            observed = self.public_ip_provider().strip()
            try:
                observed = str(ipaddress.ip_address(observed))
            except ValueError:
                raise NotReady("Current public IP could not be verified; LIVE remains blocked") from None
            if observed != expected:
                raise NotReady("Current public IP does not match the registered static IP")
            account = executor.account()
        else:
            executor = runtime.executor
            account = executor.account()
        account.validate()
        for instrument in runtime.instruments.values():
            instrument.validate(now=self.now())
            runtime.calendar.window(instrument, self.now())
            if not runtime.calendar.is_open(instrument, self.now()):
                raise NotReady(f"Exchange session is closed for {instrument.exchange}")
            bars = runtime.feed.completed(instrument, self.settings.timeframe)
            if not bars:
                raise NotReady(f"No completed bars for {instrument.instrument_key}")
            if not completed_bar_is_fresh(bars[-1], self.settings.timeframe, self.now(),
                                          self.settings.stale_after_seconds):
                raise NotReady(f"Completed bar is stale for {instrument.instrument_key}")
            quote = runtime.reader.quote(instrument.instrument_key)
            self._validate_quote(quote, instrument)
            quote_age = (self.now() - quote.timestamp.astimezone(timezone.utc)).total_seconds()
            if quote_age < -self.settings.max_bar_future_skew_seconds or \
                    quote_age > self.settings.stale_after_seconds:
                raise NotReady(f"Execution quote is stale/future for {instrument.instrument_key}")
        # Never allow two LIVE operator sessions to own the same contract or
        # unresolved/working intent, even when their signal ids differ.
        if runtime.mode == "UPSTOX_LIVE" and any(
                self._live_instrument_conflict(runtime, key) for key in runtime.instruments):
            raise NotReady("Another LIVE session owns a selected instrument or open intent")
        # Never adopt/manage an unrelated existing account position.
        known = {p.instrument_key for p in account.positions if p.quantity}
        selected = set(runtime.instruments)
        if known - selected:
            raise NotReady("Account contains an unselected position; reconcile externally first")
        if account.positions and self.store.intents(runtime.session_id) == []:
            raise NotReady("Existing positions have no session protection journal")
        if not self._reconcile(runtime, account):
            raise NotReady("Recovered/current orders and positions could not be reconciled")
        runtime.account = runtime.account or account
        runtime.recovery_reconciled = True
        return runtime.account

    @staticmethod
    def _validate_quote(quote: Quote, instrument: Instrument) -> None:
        if quote.instrument_key != instrument.instrument_key:
            raise ReconciliationFailure("Quote instrument key mismatch")
        if quote.timestamp.tzinfo is None or quote.timestamp.utcoffset() is None:
            raise ReconciliationFailure("Quote timestamp must be timezone-aware")
        values = (quote.bid, quote.ask, quote.last, quote.volume)
        if not all(math.isfinite(value) for value in values) or min(quote.bid, quote.ask, quote.last) <= 0 \
                or quote.ask < quote.bid or quote.volume < 0:
            raise ReconciliationFailure("Quote failed executable-price validation")

    def activate(self, session_id: str) -> dict:
        with self._lock:
            runtime = self._runtime(session_id)
            if runtime.state != Lifecycle.ARMED.value:
                raise SessionError("Explicit Arm is required before Activate")
            readiness = self.readiness(session_id)
            if not readiness["ready"]:
                raise NotReady("; ".join(readiness["blockers"]))
            account = self._preflight(runtime)
            with runtime.control_lock:
                if runtime.stop_requested.is_set() or runtime.state != Lifecycle.ARMED.value:
                    raise SessionError("Activation was preempted; fresh Arm and Activate are required")
                runtime.account = account
                runtime.entries_blocked = bool(runtime.risk and daily_loss_breached(runtime.risk, account.day_pnl))
                self._set_state(runtime, Lifecycle.ACTIVE.value)
                self.store.audit("OPERATOR_ACTIVATE", {
                    "automatic": False, "mode": runtime.mode,
                    "paper_only": runtime.mode == "PAPER", "equity": account.equity,
                    "entries_blocked_at_activation": runtime.entries_blocked,
                }, session_id)
                self._persist(runtime)
                return self.public(session_id)

    def pause(self, session_id: str) -> dict:
        with self._lock:
            runtime = self._runtime(session_id)
            with runtime.control_lock:
                if runtime.state == Lifecycle.ACTIVE.value and not runtime.stop_requested.is_set():
                    self._set_state(runtime, Lifecycle.PAUSED.value,
                                    reason="Operator pause; fresh Arm/Activate required")
                runtime.entries_blocked = True
                self.store.audit("OPERATOR_PAUSE", {"protection_preserved": True}, session_id)
                self._persist(runtime)
                return self.public(session_id)

    def recover_view(self, session_id: str) -> dict:
        """Browser/session reload boundary: durable read-only view, never approval."""
        runtime = self._runtime(session_id)
        runtime.stop_requested.set()
        runtime.entries_blocked = True
        with runtime.control_lock:
            with runtime.order_lock:
                prior_state = runtime.state
                target_state = (Lifecycle.EMERGENCY_STOPPED.value
                                if prior_state == Lifecycle.EMERGENCY_STOPPED.value
                                else Lifecycle.HALTED.value)
                self._set_state(
                    runtime, target_state,
                    reason="Browser/session recovery view; fresh authorization, risk and approval required",
                )
                if runtime.mode == "UPSTOX_LIVE":
                    runtime.executor = None
                runtime.authorized_day = ""
                runtime.auth_fingerprint = ""
                runtime.risk = None
                runtime.recovered = True
                runtime.recovery_reconciled = False
                unresolved_emergency = runtime.emergency.get("cleanup_status") in {
                    "IN_PROGRESS", "UNRESOLVED", "ADAPTER_UNAVAILABLE"
                }
                if not unresolved_emergency:
                    runtime.fault = Fault.RECOVERY.value
                    runtime.fault_reason = (
                        "Recovered read-only view; fresh authorization, risk, reconciliation, Arm and Activate required"
                    )
                self.store.clear_session_approval(session_id)
                self.store.audit("BROWSER_RECOVERY_VIEW", {
                    "prior_state": prior_state, "state": target_state,
                    "live_executor_detached": runtime.mode == "UPSTOX_LIVE",
                    "broker_mutation": False,
                }, session_id)
                self._persist(runtime)
                return self.public(session_id)

    def emergency_stop(self, session_id: str) -> dict:
        # Deliberately do not wait for self._lock: a worker may hold it during
        # a slow feed read.  The event immediately prevents subsequent permits;
        # order_lock only waits for an actual in-flight broker mutation.
        runtime = self._runtime(session_id)
        with runtime.control_lock:
            runtime.stop_requested.set()
            runtime.entries_blocked = True
            runtime.fault = Fault.MANUAL_STOP.value
            runtime.fault_reason = "Emergency stop: entries blocked; managed unfilled entries require reconciliation"
            runtime.emergency = {
                "requested_at": self.now().isoformat(), "cleanup_status": "IN_PROGRESS",
                "managed_entries_resolved": False, "unresolved_orders": [],
                "open_positions": [], "account_clear": False,
                "account_clear_status": "NOT_CLAIMED",
            }
            self._set_state(
                runtime, Lifecycle.EMERGENCY_STOPPED.value,
                reason="Manual emergency stop; entries preempted, no forced liquidation",
            )
            self.store.audit("EMERGENCY_STOP", {
                "protection_preserved": True, "forced_close": False,
                "cancel_unfilled_entries": True,
            }, session_id)
        self._emergency_cleanup(runtime)
        self._persist(runtime)
        return self.public(session_id)

    def _emergency_cleanup(self, runtime: Runtime) -> None:
        unresolved: list[dict] = []
        attempts = 0
        cleanup_failed = False
        if runtime.executor is None or (runtime.mode == "UPSTOX_LIVE" and not getattr(
                runtime.executor, "broker_mutations_reachable", False)):
            for row in self._active_intents(runtime):
                if json.loads(row["broker_ids_json"]):
                    self.store.transition_intent(row["intent_id"], "UNRESOLVED")
                unresolved.append({"intent_id": row["intent_id"], "instrument_key": row["instrument_key"],
                                   "broker_ids": json.loads(row["broker_ids_json"]),
                                   "state": row["state"],
                                   "reason": "Order-capable adapter unavailable; recovery reader is read-only"})
            runtime.emergency.update({
                "cleanup_status": "UNRESOLVED" if unresolved else "ADAPTER_UNAVAILABLE",
                "managed_entries_resolved": False, "unresolved_orders": unresolved,
                "open_positions": [p.instrument_key for p in (runtime.account.positions if runtime.account else ())],
                "completed_at": self.now().isoformat(),
            })
            self._set_state(runtime, Lifecycle.EMERGENCY_STOPPED.value,
                            reason="Emergency cleanup unavailable; external broker reconciliation required")
            return
        with runtime.order_lock:
            try:
                account = runtime.executor.account()
                runtime.account = account
                self._reconcile(runtime, account)
                positions = {p.instrument_key: p for p in account.positions if p.quantity}
                for row in list(self._active_intents(runtime)):
                    key = row["instrument_key"]
                    if key in positions:
                        continue  # Never cancel protection for an open position.
                    ids = json.loads(row["broker_ids_json"])
                    if not ids:
                        # Preserve SUBMISSION_ATTEMPTED/UNRESOLVED exactly.  No
                        # GTT id can be inferred or fabricated for cancellation.
                        continue
                    record = runtime.executor.get_gtt(ids[0])
                    if record is None:
                        # A completed GTT may be absent from this endpoint;
                        # retain the original durable state for order-history
                        # reconciliation instead of erasing it to UNRESOLVED.
                        continue
                    rules = {rule.get("strategy"): rule for rule in record.get("rules") or []}
                    entry_status = str((rules.get("ENTRY") or {}).get("status") or "").upper()
                    if entry_status in {"OPEN", "PENDING", "SCHEDULED"}:
                        attempts += 1
                        self._cancel_unfilled_runtime(runtime, key, emergency=True)
                # Final broker reads determine whether any managed entry is
                # still working.  Acknowledgement alone is never enough.
                account = runtime.executor.account()
                runtime.account = account
                self._reconcile(runtime, account)
            except Exception as exc:
                cleanup_failed = True
                for row in self._active_intents(runtime):
                    if not any(p.instrument_key == row["instrument_key"] and p.quantity
                               for p in (runtime.account.positions if runtime.account else ())):
                        if json.loads(row["broker_ids_json"]):
                            self.store.transition_intent(row["intent_id"], "UNRESOLVED")
                runtime.fault = Fault.AMBIGUOUS_SUBMISSION.value
                runtime.fault_reason = f"Emergency cancellation/reconciliation unresolved: {type(exc).__name__}"
            positions = {p.instrument_key: p for p in (runtime.account.positions if runtime.account else ()) if p.quantity}
            for row in self._active_intents(runtime):
                if row["instrument_key"] in positions:
                    continue
                ids = json.loads(row["broker_ids_json"])
                unresolved.append({
                    "intent_id": row["intent_id"], "instrument_key": row["instrument_key"],
                    "broker_ids": ids, "state": row["state"],
                })
            known = {item["intent_id"] for item in unresolved if "intent_id" in item}
            for row in self.store.unresolved(runtime.session_id):
                if row["intent_id"] not in known:
                    unresolved.append({
                        "intent_id": row["intent_id"], "instrument_key": row["instrument_key"],
                        "broker_ids": json.loads(row["broker_ids_json"]), "state": row["state"],
                    })
            if cleanup_failed and not unresolved:
                unresolved.append({"reason": "Final broker account/order reconciliation was not confirmed"})
            runtime.emergency.update({
                "cleanup_status": "UNRESOLVED" if unresolved else "CONFIRMED",
                "managed_entries_resolved": not unresolved,
                "unresolved_orders": unresolved,
                "open_positions": sorted(positions),
                "cancellation_attempts": attempts,
                "completed_at": self.now().isoformat(),
                "account_clear": False,
                "account_clear_status": "NOT_CLAIMED",
                "note": "Managed-entry cleanup only; positions and external/unmanaged exposure are not declared clear.",
            })
            if unresolved:
                runtime.fault = Fault.AMBIGUOUS_SUBMISSION.value
                runtime.fault_reason = "Emergency stop has unresolved managed orders; fresh approval is blocked"
            self._set_state(runtime, Lifecycle.EMERGENCY_STOPPED.value,
                            reason=runtime.fault_reason)
            self.store.audit("EMERGENCY_CLEANUP", runtime.emergency, runtime.session_id)

    def _halt(self, runtime: Runtime, fault: Fault | str, reason: str) -> None:
        runtime.entries_blocked = True
        runtime.fault = str(fault.value if isinstance(fault, Fault) else fault)
        runtime.fault_reason = reason
        self._set_state(runtime, Lifecycle.HALTED.value, reason=reason)
        self.store.audit("ALERT", {"fault": runtime.fault, "reason": reason,
                                   "entries_blocked": True, "protection_preserved": True,
                                   "fresh_approval_required": True}, runtime.session_id)
        self._persist(runtime)

    def _permit(self, runtime: Runtime, kind: str) -> ExecutionPermit:
        if kind == "CANCEL_UNFILLED":
            # Risk-reducing cancellation is valid after Pause/Halt/Emergency;
            # it never requires reactivation.  All exposure-increasing kinds
            # remain strictly ACTIVE-only and are preempted by Emergency.
            if runtime.executor is None:
                raise PermissionError("No adapter is available for cancellation")
        else:
            if runtime.state != Lifecycle.ACTIVE.value or runtime.stop_requested.is_set():
                raise PermissionError("No entry/protection permit outside non-preempted ACTIVE")
            if kind == "ENTRY" and runtime.entries_blocked:
                raise PermissionError("Entry permit blocked by daily loss/fault state")
        return ExecutionPermit(runtime.session_id, kind, uuid4().hex,
                               self.now() + timedelta(seconds=30))

    @staticmethod
    def _order_from_payload(payload: dict) -> ProtectedOrder:
        raw = payload["order"]
        return ProtectedOrder(**raw)

    def _active_intents(self, runtime: Runtime) -> list:
        terminal = {"CLOSED", "REJECTED", "CANCELLED"}
        return [row for row in self.store.intents(runtime.session_id) if row["state"] not in terminal]

    def _reconcile(self, runtime: Runtime, account: AccountSnapshot) -> bool:
        """Fail closed and preserve every incomplete outcome in the journal."""
        resolved = self._reconcile_inner(runtime, account)
        for row in self.store.intents(runtime.session_id):
            payload = json.loads(row["payload_json"])
            if payload.get("cancel_reconciliation_required") and row["state"] in {
                    "CANCELLED", "CLOSED", "REJECTED", "PROTECTED"}:
                self.store.update_intent_details(row["intent_id"], {
                    "uncertain_operation": "",
                    "cancel_reconciliation_required": False,
                    "cancel_resolved_at": self.now().isoformat(),
                })
        if not resolved:
            for row in self._active_intents(runtime):
                if row["state"] != "SUBMISSION_ATTEMPTED":
                    self.store.transition_intent(row["intent_id"], "UNRESOLVED")
            self._persist(runtime)
        return resolved

    def _reconcile_inner(self, runtime: Runtime, account: AccountSnapshot) -> bool:
        """Reconcile broker truth before any further write; never infer fills."""
        try:
            active = self._active_intents(runtime)
            by_key = {row["instrument_key"]: row for row in active}
            positions = {p.instrument_key: p for p in account.positions if p.quantity}
            if any(key not in by_key for key in positions):
                self._halt(runtime, Fault.RECONCILIATION_FAILED,
                           "An account position has no exact session intent/protection mapping")
                return False
            enriched = []
            for row in active:
                key = row["instrument_key"]
                payload = __import__("json").loads(row["payload_json"])
                ids = __import__("json").loads(row["broker_ids_json"])
                identifier = ids[0] if ids else ""
                record = runtime.executor.get_gtt(identifier) if identifier else None
                if identifier:
                    if record is None:
                        runtime.orders.pop(identifier, None)
                    else:
                        runtime.orders[identifier] = record
                if record is None and row["state"] in {"UNRESOLVED", "SUBMISSION_ATTEMPTED"} \
                        and hasattr(runtime.executor, "find_matching_gtt"):
                    matches = runtime.executor.find_matching_gtt(self._order_from_payload(payload))
                    if len(matches) == 1:
                        identifier = matches[0]
                        self.store.transition_intent(row["intent_id"], "ACKNOWLEDGED_UNRECONCILED",
                                                     broker_ids=[identifier])
                        record = runtime.executor.get_gtt(identifier)
                        if record is not None:
                            runtime.orders[identifier] = record
                position = positions.get(key)
                if record is None:
                    if not self._reconcile_missing(runtime, row, position, account):
                        return False
                    continue
                if record.get("instrument_token") != key:
                    self._halt(runtime, Fault.RECONCILIATION_FAILED, "GTT instrument mapping mismatch")
                    return False
                rules = {rule.get("strategy"): rule for rule in record.get("rules") or []}
                if set(rules) != {"ENTRY", "STOPLOSS", "TARGET"}:
                    self._halt(runtime, Fault.PROTECTION_REJECTED, "GTT does not contain all three protective legs")
                    return False
                child_ids = [str(rule.get("order_id")) for rule in rules.values() if rule.get("order_id")]
                self.store.update_intent_details(row["intent_id"], {
                    "child_order_ids": child_ids,
                    "rule_status": {name: rule.get("status") for name, rule in rules.items()},
                    "confirmed_stop": rules["STOPLOSS"].get("trigger_price"),
                    "confirmed_target": rules["TARGET"].get("trigger_price"),
                })
                entry_status = str(rules["ENTRY"].get("status") or "").upper()
                stop_status = str(rules["STOPLOSS"].get("status") or "").upper()
                target_status = str(rules["TARGET"].get("status") or "").upper()
                if position is not None:
                    if entry_status not in {"COMPLETED", "PARTIALLY_FILLED", "TRIGGERED"}:
                        self._halt(runtime, Fault.RECONCILIATION_FAILED,
                                   "Position exists without a confirmed entry state")
                        return False
                    if abs(position.quantity) != float(record.get("quantity") or 0):
                        self._halt(runtime, Fault.PROTECTION_REJECTED,
                                   "Position quantity and broker protection quantity differ")
                        return False
                    if stop_status not in {"SCHEDULED", "OPEN", "PENDING"} or \
                            target_status not in {"SCHEDULED", "OPEN", "PENDING"}:
                        self._halt(runtime, Fault.PROTECTION_REJECTED,
                                   "Open position lacks confirmed live stop/target protection")
                        return False
                    stop = float(rules["STOPLOSS"]["trigger_price"])
                    target = float(rules["TARGET"]["trigger_price"])
                    old_stop = payload.get("confirmed_stop")
                    if old_stop is not None and ((position.quantity > 0 and stop < float(old_stop)) or
                                                 (position.quantity < 0 and stop > float(old_stop))):
                        self._halt(runtime, Fault.PROTECTION_REJECTED, "Broker stop moved in a loosening direction")
                        return False
                    enriched.append(replace(position, stop_price=stop, target_price=target,
                                            protection_id=identifier,
                                            correlation_group=runtime.instruments[key].correlation_group))
                    self.store.transition_intent(row["intent_id"], "PROTECTED", broker_ids=[identifier])
                    runtime.working.add(key)
                else:
                    if entry_status in {"OPEN", "PENDING", "SCHEDULED"}:
                        if payload.get("cancel_reconciliation_required"):
                            # The read proves that the GTT still exists; it does
                            # not prove what the ambiguous cancel request did.
                            self.store.transition_intent(
                                row["intent_id"], "UNRESOLVED", broker_ids=[identifier]
                            )
                        else:
                            self.store.transition_intent(row["intent_id"], "WORKING", broker_ids=[identifier])
                        runtime.working.add(key)
                    elif entry_status in {"FAILED", "CANCELLED", "EXPIRED"}:
                        terminal = "CANCELLED" if entry_status == "CANCELLED" else "REJECTED"
                        self.store.transition_intent(row["intent_id"], terminal, broker_ids=[identifier])
                        runtime.working.discard(key)
                    elif entry_status in {"COMPLETED", "PARTIALLY_FILLED"} and \
                            all(status in {"COMPLETED", "CANCELLED", "FAILED", "EXPIRED"}
                                for status in (entry_status, stop_status, target_status)):
                        self.store.transition_intent(row["intent_id"], "CLOSED", broker_ids=[identifier])
                        runtime.working.discard(key)
                    else:
                        self._halt(runtime, Fault.RECONCILIATION_FAILED,
                                   "GTT state is ambiguous without a matching position")
                        return False
            # Include positions which were not enriched only if none exist;
            # every actual position must have confirmed protection above.
            runtime.account = replace(account, positions=tuple(enriched))
            self._enforce_daily_loss(runtime)
            runtime.recovery_reconciled = True
            self._refresh_emergency_reconciliation(runtime)
            self._persist(runtime)
            return True
        except (UpstoxAuthError, ReconciliationFailure, ValueError, KeyError, TypeError) as exc:
            self._halt(runtime, Fault.RECONCILIATION_FAILED,
                       f"Broker reconciliation failed: {type(exc).__name__}")
            return False

    def _refresh_emergency_reconciliation(self, runtime: Runtime) -> None:
        if not runtime.emergency or runtime.emergency.get("cleanup_status") == "IN_PROGRESS":
            return
        positions = {p.instrument_key for p in (runtime.account.positions if runtime.account else ()) if p.quantity}
        unresolved = []
        for row in self._active_intents(runtime):
            if row["instrument_key"] in positions:
                continue
            unresolved.append({
                "intent_id": row["intent_id"], "instrument_key": row["instrument_key"],
                "broker_ids": json.loads(row["broker_ids_json"]), "state": row["state"],
            })
        known = {item["intent_id"] for item in unresolved}
        for row in self.store.unresolved(runtime.session_id):
            if row["intent_id"] not in known:
                unresolved.append({
                    "intent_id": row["intent_id"], "instrument_key": row["instrument_key"],
                    "broker_ids": json.loads(row["broker_ids_json"]), "state": row["state"],
                })
        runtime.emergency.update({
            "cleanup_status": "UNRESOLVED" if unresolved else "CONFIRMED",
            "managed_entries_resolved": not unresolved,
            "unresolved_orders": unresolved,
            "open_positions": sorted(positions),
            "account_clear": False,
            "account_clear_status": "NOT_CLAIMED",
        })

    def _enforce_daily_loss(self, runtime: Runtime) -> None:
        if runtime.risk and runtime.account and daily_loss_breached(runtime.risk, runtime.account.day_pnl):
            runtime.entries_blocked = True
            if not runtime.daily_loss_noticed:
                runtime.daily_loss_noticed = True
                self.store.audit("DAILY_LOSS_CAP", {
                    "entries_blocked": True, "forced_close": False,
                    "protection_continues": True, "latched_for_day": True,
                }, runtime.session_id)

    def _reconcile_missing(self, runtime: Runtime, row, position, account: AccountSnapshot) -> bool:
        if position is not None:
            self._halt(runtime, Fault.PROTECTION_REJECTED,
                       "Completed/missing GTT cannot prove protection for an open position")
            return False
        payload = __import__("json").loads(row["payload_json"])
        child_ids = set(payload.get("child_order_ids") or [])
        if child_ids and hasattr(runtime.executor, "orders"):
            orders = runtime.executor.orders()
            matching = [order for order in orders if str(order.get("order_id")) in child_ids]
            terminal = {"COMPLETE", "COMPLETED", "CANCELLED", "REJECTED", "FAILED", "EXPIRED"}
            if len(matching) == len(child_ids) and all(
                    str(order.get("status", "")).upper() in terminal for order in matching):
                self.store.transition_intent(row["intent_id"], "CLOSED")
                runtime.working.discard(row["instrument_key"])
                return True
        self._halt(runtime, Fault.AMBIGUOUS_SUBMISSION,
                   "GTT is unavailable and ordinary orders/positions cannot prove a terminal outcome")
        return False

    def _manage_trailing(self, runtime: Runtime, instrument: Instrument) -> None:
        if runtime.account is None or runtime.stop_requested.is_set():
            return
        position = next((p for p in runtime.account.positions
                         if p.instrument_key == instrument.instrument_key), None)
        if position is None or not position.protection_id or position.stop_price is None:
            return
        bars = runtime.bars.get(instrument.instrument_key, [])
        try:
            result = trailing_stop(bars, position.side, position.stop_price, bars[-1].close,
                                   instrument.tick_size, lookback=self.settings.swing_bars)
        except ValueError:
            return  # No new confirmed swing is not a market-data failure.
        if result is None:
            return
        new_stop, evidence = result
        row = next((row for row in self._active_intents(runtime)
                    if row["instrument_key"] == instrument.instrument_key), None)
        if row is None:
            self._halt(runtime, Fault.RECONCILIATION_FAILED, "Protected position lacks intent")
            return
        try:
            with runtime.order_lock:
                if runtime.state != Lifecycle.ACTIVE.value or runtime.stop_requested.is_set():
                    return
                self.store.transition_intent(row["intent_id"], "MODIFICATION_ATTEMPTED",
                                             event_payload={"new_stop": new_stop, "evidence": evidence})
                submission = runtime.executor.modify_stop(
                    position.protection_id, new_stop, self._permit(runtime, "PROTECTIVE"),
                    session_id=runtime.session_id, instrument=instrument,
                )
                self.store.transition_intent(row["intent_id"], "ACKNOWLEDGED_UNRECONCILED",
                                             broker_ids=list(submission.broker_ids))
            if not self._reconcile(runtime, runtime.executor.account()):
                return
            self.store.audit("PROTECTION_TRAILED", {"instrument_key": instrument.instrument_key,
                                                     "stop": new_stop, "evidence": evidence},
                             runtime.session_id)
        except AmbiguousSubmission:
            self.store.transition_intent(row["intent_id"], "UNRESOLVED")
            self._halt(runtime, Fault.AMBIGUOUS_SUBMISSION,
                       "Stop modification outcome unknown; old confirmed stop retained for reconciliation")
        except (UpstoxRejected, UpstoxAuthError, ReconciliationFailure, PermissionError) as exc:
            self.store.transition_intent(row["intent_id"], "UNRESOLVED")
            fault = Fault.TOKEN_EXPIRED if isinstance(exc, UpstoxAuthError) else Fault.PROTECTION_REJECTED
            self._halt(runtime, fault, f"Protection modification failed: {type(exc).__name__}")
        except Exception as exc:
            self.store.transition_intent(row["intent_id"], "UNRESOLVED")
            self._halt(runtime, Fault.AMBIGUOUS_SUBMISSION,
                       f"Protection outcome requires reconciliation: {type(exc).__name__}")

    def _correlations(self, runtime: Runtime) -> None:
        keys = list(runtime.bars)
        for index, left in enumerate(keys):
            for right in keys[index + 1:]:
                lb = {bar.timestamp: bar.close for bar in runtime.bars[left][-60:]}
                rb = {bar.timestamp: bar.close for bar in runtime.bars[right][-60:]}
                common = sorted(set(lb) & set(rb))
                if len(common) < 12:
                    continue
                lr = [lb[common[i]] / lb[common[i - 1]] - 1 for i in range(1, len(common))]
                rr = [rb[common[i]] / rb[common[i - 1]] - 1 for i in range(1, len(common))]
                if len(lr) < 10 or statistics.pstdev(lr) <= 0 or statistics.pstdev(rr) <= 0:
                    continue
                mean_l, mean_r = statistics.mean(lr), statistics.mean(rr)
                covariance = sum((a - mean_l) * (b - mean_r) for a, b in zip(lr, rr)) / len(lr)
                rho = covariance / (statistics.pstdev(lr) * statistics.pstdev(rr))
                if math.isfinite(rho):
                    runtime.correlations[tuple(sorted((left, right)))] = max(-1.0, min(1.0, rho))

    def _forecast(self, runtime: Runtime, instrument: Instrument,
                  bars: list[Candle]):
        key = instrument.instrument_key
        last = bars[-1].timestamp
        cached = runtime.forecasts.get(key)
        if cached is not None and cached.input_end == last:
            return cached
        evidence = [item for item in self.evidence.for_instrument(key)
                    if item.kronos_required and item.kronos_artifact_digest == self.kronos.artifact_digest]
        if not self.kronos_status.get("available_for_inference"):
            return None
        try:
            forecast = self.kronos.forecast(
                key, bars, timeframe=self.settings.timeframe,
                validated=bool(evidence), validation_id=evidence[0].evidence_id if evidence else "",
            )
            runtime.forecasts[key] = forecast
            self.kronos_status = self.kronos.status()
            return forecast
        except Exception as exc:
            self.kronos_status = self.kronos.status()
            runtime.diagnostics.append({"instrument_key": key, "kronos": "INFERENCE_ERROR",
                                        "error_type": type(exc).__name__})
            return None

    def _live_instrument_conflict(self, runtime: Runtime, key: str) -> bool:
        if runtime.mode != "UPSTOX_LIVE":
            return False
        for other in self.runtimes.values():
            if other.session_id == runtime.session_id or other.mode != "UPSTOX_LIVE":
                continue
            if key in other.working:
                return True
            if any(row["instrument_key"] == key for row in self._active_intents(other)):
                return True
        return False

    def _submit(self, runtime: Runtime, candidate: Candidate) -> None:
        key = candidate.instrument_key
        if runtime.state != Lifecycle.ACTIVE.value or runtime.stop_requested.is_set():
            return
        if self._live_instrument_conflict(runtime, key):
            self.store.audit("CANDIDATE_BLOCKED", {"signal_id": candidate.signal_id,
                                                   "reason": "Another LIVE session owns this instrument/intent"},
                             runtime.session_id)
            return
        if key in runtime.working or any(p.instrument_key == key for p in (runtime.account.positions if runtime.account else ())):
            return
        if runtime.entries_blocked:
            return
        existing = self.store.intent_by_signal(runtime.session_id, candidate.signal_id)
        if existing is not None:
            return
        instrument = runtime.instruments[key]
        if candidate.side == "SELL" and not instrument.currency_derivative \
                and (not instrument.short_route_metadata or instrument.product != "I"):
            self.store.audit("CANDIDATE_BLOCKED", {"signal_id": candidate.signal_id,
                                                   "reason": "No exact MIS product-I short-route metadata"}, runtime.session_id)
            return
        try:
            if runtime.mode == "UPSTOX_LIVE":
                profile = runtime.reader.profile(include_static_ips=False)
                runtime.profile = profile
                permission_blockers = self._permission_blockers(runtime, profile)
                if permission_blockers:
                    raise UpstoxRejected("; ".join(permission_blockers))
            quote = runtime.reader.quote(key)
            self._validate_quote(quote, instrument)
            quote_age = (self.now() - quote.timestamp.astimezone(timezone.utc)).total_seconds()
            if quote_age < -self.settings.max_bar_future_skew_seconds or \
                    quote_age > self.settings.stale_after_seconds:
                self._halt(runtime, Fault.STALE_DATA,
                           "Execution quote is stale/future; fresh daily approval is required")
                return
            account = runtime.account or runtime.executor.account()
            size = stop_sized_quantity(
                candidate, instrument, account, runtime.risk,
                bar_volume=runtime.bars[key][-1].volume,
                max_participation=self.settings.max_participation,
                gap_risk_fraction=self.settings.gap_risk_fraction,
                cost_rate=self.settings.paper_commission_rate + self.settings.paper_slippage_rate,
            )
            order = ProtectedOrder(
                intent_id=uuid4().hex, instrument_key=key, side=candidate.side,
                quantity=size.quantity, limit_price=candidate.entry_price,
                stop_price=candidate.stop_price, target_price=candidate.target_price,
                product=instrument.product,
                margin_price=(quote.ask if candidate.side == "BUY" else quote.bid),
            )
            order.validate()
            if size.quantity <= 0:
                self.store.audit("CANDIDATE_BLOCKED", {"signal_id": candidate.signal_id,
                                                       "reason": size.reason}, runtime.session_id)
                return
            required_margin = runtime.executor.margin_for(order)
            decision = portfolio_entry_check(
                candidate, size, instrument, account, runtime.risk, runtime.instruments,
                required_margin=required_margin, bid=quote.bid, ask=quote.ask,
                quote_time=quote.timestamp, now=self.now(),
                stale_after_seconds=self.settings.stale_after_seconds,
                max_spread_bps=self.settings.max_spread_bps,
                max_margin_usage_fraction=self.settings.max_margin_usage_fraction,
                gap_risk_fraction=self.settings.gap_risk_fraction,
                correlations=runtime.correlations,
            )
            if not decision.allowed:
                self.store.audit("CANDIDATE_BLOCKED", {"signal_id": candidate.signal_id,
                                                       "reasons": decision.reasons}, runtime.session_id)
                return
            payload = {"candidate": candidate.public(), "order": order.__dict__,
                       "size": size.__dict__, "quote": {"bid": quote.bid, "ask": quote.ask,
                       "timestamp": quote.timestamp.isoformat()}, "required_margin": required_margin}
            with runtime.order_lock:
                if runtime.state != Lifecycle.ACTIVE.value or runtime.stop_requested.is_set():
                    return
                if not self.store.record_intent(order.intent_id, runtime.session_id, candidate.signal_id,
                                                key, payload, None):
                    return
                self.store.transition_intent(order.intent_id, "RISK_APPROVED")
                self.store.transition_intent(order.intent_id, "SUBMISSION_ATTEMPTED")
                try:
                    submission = runtime.executor.place_protected_entry(
                        order, self._permit(runtime, "ENTRY"), session_id=runtime.session_id,
                        instrument=instrument,
                    )
                except AmbiguousSubmission:
                    self.store.transition_intent(order.intent_id, "UNRESOLVED")
                    self._halt(runtime, Fault.AMBIGUOUS_SUBMISSION,
                               "Entry outcome unknown; no retry until broker reconciliation")
                    return
                except (UpstoxRejected, UpstoxAuthError, ReconciliationFailure) as exc:
                    self.store.transition_intent(order.intent_id, "UNRESOLVED")
                    fault = Fault.TOKEN_EXPIRED if isinstance(exc, UpstoxAuthError) else Fault.RECONCILIATION_FAILED
                    self._halt(runtime, fault, f"Entry submission requires reconciliation: {type(exc).__name__}")
                    return
                except Exception as exc:
                    # A caller/adapter bug after the intent boundary is not proof
                    # that no request reached the broker.  Fail closed.
                    self.store.transition_intent(order.intent_id, "UNRESOLVED")
                    self._halt(runtime, Fault.AMBIGUOUS_SUBMISSION,
                               f"Entry outcome requires reconciliation: {type(exc).__name__}")
                    return
                self.store.transition_intent(order.intent_id, "ACKNOWLEDGED_UNRECONCILED",
                                             broker_ids=list(submission.broker_ids))
                runtime.working.add(key)
                self._reconcile(runtime, runtime.executor.account())
        except UpstoxRejected as exc:
            self._halt(runtime, Fault.PERMISSION_REVOKED,
                       f"Account/order route permission failed: {exc}")
        except (UpstoxAuthError, ReconciliationFailure) as exc:
            self._halt(runtime, Fault.TOKEN_EXPIRED if isinstance(exc, UpstoxAuthError)
                       else Fault.RECONCILIATION_FAILED, f"Entry preflight failed: {type(exc).__name__}")
        except ValueError as exc:
            self.store.audit("CANDIDATE_BLOCKED", {"signal_id": candidate.signal_id,
                                                   "reason": str(exc)}, runtime.session_id)

    def scan_once(self, session_id: str) -> dict:
        """Read-only candidate/data scan; never fills, submits, modifies or cancels."""
        return self._scan(session_id, execute=False)

    def execute_once(self, session_id: str) -> dict:
        """Worker-only execution scan for an explicitly ACTIVE daily session."""
        return self._scan(session_id, execute=True)

    def _scan(self, session_id: str, *, execute: bool) -> dict:
        with self._lock:
            runtime = self._runtime(session_id)
            if runtime.state != Lifecycle.ACTIVE.value or runtime.stop_requested.is_set():
                return self.public(session_id)
            if runtime.authorized_day != self.day() or runtime.risk is None or \
                    runtime.risk.trading_day != self.day():
                self._halt(runtime, Fault.TOKEN_EXPIRED,
                           "Daily authorization/risk expired; fresh daily setup and approval required")
                return self.public(session_id)
            runtime.last_scan = self.now()
            runtime.last_scan_kind = "EXECUTION" if execute else "READ_ONLY"
            runtime.scan_count += 1
            new_bars: list[str] = []
            try:
                for instrument in runtime.instruments.values():
                    if runtime.stop_requested.is_set():
                        return self.public(session_id)
                    if not runtime.calendar.is_open(instrument, self.now()):
                        continue
                    bars = runtime.feed.completed(instrument, self.settings.timeframe)
                    # Emergency may have completed cleanup while this read was
                    # blocked.  Do not process a bar or issue a later write.
                    if runtime.stop_requested.is_set():
                        return self.public(session_id)
                    if not bars or not completed_bar_is_fresh(
                            bars[-1], self.settings.timeframe, self.now(),
                            self.settings.stale_after_seconds):
                        self._halt(runtime, Fault.STALE_DATA, "Latest completed bar is missing or stale")
                        return self.public(session_id)
                    markers = runtime.processed if execute else runtime.scanned
                    previous = markers.get(instrument.instrument_key)
                    if previous == bars[-1].timestamp:
                        continue
                    if previous is not None and bars[-1].timestamp <= previous:
                        self._halt(runtime, Fault.INVALID_DATA, "Out-of-order completed bars")
                        return self.public(session_id)
                    runtime.bars[instrument.instrument_key] = bars
                    if execute:
                        with runtime.order_lock:
                            if runtime.state != Lifecycle.ACTIVE.value or runtime.stop_requested.is_set():
                                return self.public(session_id)
                            if runtime.mode == "PAPER":
                                runtime.executor.on_bar(bars[-1])
                            runtime.processed[instrument.instrument_key] = bars[-1].timestamp
                            self._persist(runtime)
                    else:
                        runtime.scanned[instrument.instrument_key] = bars[-1].timestamp
                    new_bars.append(instrument.instrument_key)
                if runtime.stop_requested.is_set():
                    return self.public(session_id)
                runtime.account = runtime.executor.account()
                if runtime.stop_requested.is_set():
                    return self.public(session_id)
                if not self._reconcile(runtime, runtime.account):
                    return self.public(session_id)
                self._enforce_daily_loss(runtime)
                if execute:
                    for key in new_bars:
                        if runtime.state != Lifecycle.ACTIVE.value or runtime.stop_requested.is_set():
                            break
                        self._manage_trailing(runtime, runtime.instruments[key])
                self._correlations(runtime)
                for key in new_bars:
                    if runtime.state != Lifecycle.ACTIVE.value or runtime.stop_requested.is_set():
                        break
                    instrument = runtime.instruments[key]
                    bars = runtime.bars[key]
                    forecast = self._forecast(runtime, instrument, bars)
                    output = generate_candidates(
                        key, bars, tick_size=instrument.tick_size,
                        timeframe=self.settings.timeframe, swing_bars=self.settings.swing_bars,
                        reward_risk=self.settings.reward_risk, forecast=forecast,
                        kronos_status=self.kronos_status.get("status", "UNAVAILABLE"),
                        evidence=self.evidence.for_instrument(key),
                    )
                    runtime.diagnostics.extend({"instrument_key": key, **item}
                                                for item in output.diagnostics)
                    runtime.diagnostics = runtime.diagnostics[-1000:]
                    for candidate in output.candidates:
                        eligible, reason = self.evidence.match(
                            candidate, self.settings.timeframe, live=runtime.mode == "UPSTOX_LIVE",
                            kronos_digest=self.kronos.artifact_digest,
                        )
                        if not eligible:
                            self.store.audit("EVIDENCE_BLOCKED", {"signal_id": candidate.signal_id,
                                                                  "reason": reason}, session_id)
                            continue
                        if not execute:
                            runtime.diagnostics.append({
                                "instrument_key": key, "family": candidate.family, "side": candidate.side,
                                "status": "ELIGIBLE_CANDIDATE_READ_ONLY_NO_ORDER",
                                "signal_id": candidate.signal_id,
                            })
                            continue
                        self._submit(runtime, candidate)
                        if runtime.state != Lifecycle.ACTIVE.value or runtime.stop_requested.is_set():
                            break
                self._persist(runtime)
            except UpstoxAuthError as exc:
                self._halt(runtime, Fault.TOKEN_EXPIRED, f"Daily authorization failed: {type(exc).__name__}")
            except ReconciliationFailure as exc:
                self._halt(runtime, Fault.DISCONNECTED, f"Market/account connection failed: {type(exc).__name__}")
            except (ValueError, KeyError, TypeError) as exc:
                self._halt(runtime, Fault.INVALID_DATA, f"Invalid market/session data: {type(exc).__name__}")
            return self.public(session_id)

    def reconcile(self, session_id: str) -> dict:
        """Read-only broker/account reconciliation; never Arms or Activates."""
        with self._lock:
            runtime = self._runtime(session_id)
            if runtime.recovery_error:
                raise NotReady(runtime.recovery_error)
            if runtime.executor is None:
                raise NotReady("No execution/account adapter exists; fresh authorization is required after recovery")
            reconciled = False
            try:
                account = runtime.executor.account()
                runtime.account = account
                reconciled = self._reconcile(runtime, account)
                if self.store.unresolved(runtime.session_id):
                    reconciled = False
                self.store.audit("OPERATOR_RECONCILE", {
                    "state": runtime.state, "reconciled": reconciled,
                    "fresh_approval_still_required": True,
                }, session_id)
            except UpstoxAuthError:
                self._halt(runtime, Fault.TOKEN_EXPIRED, "Reconciliation credential expired")
            except ReconciliationFailure:
                self._halt(runtime, Fault.RECONCILIATION_FAILED, "Reconciliation endpoint failed")
            self._persist(runtime)
            if not reconciled:
                raise NotReady(
                    "Broker/order reconciliation is incomplete; unresolved intents remain and fresh approval is blocked"
                )
            return self.public(session_id)

    def cancel_unfilled(self, session_id: str, instrument_key: str) -> dict:
        """Explicit risk-reducing cancellation, also valid after Pause/Halt/Emergency."""
        with self._lock:
            runtime = self._runtime(session_id)
            self._cancel_unfilled_runtime(runtime, instrument_key)
            self._persist(runtime)
            return self.public(session_id)

    def _cancel_unfilled_runtime(self, runtime: Runtime, instrument_key: str,
                                 *, emergency: bool = False) -> None:
        with runtime.order_lock:
            if runtime.executor is None:
                raise SessionError("No execution adapter is available for cancellation")
            if runtime.mode == "UPSTOX_LIVE" and not getattr(
                    runtime.executor, "broker_mutations_reachable", False):
                raise SessionError("Recovered read-only adapter cannot cancel; Activate only after reconciliation")
            row = next((row for row in self._active_intents(runtime)
                        if row["instrument_key"] == instrument_key), None)
            if row is None:
                raise SessionError("No active unfilled intent for exact instrument")
            ids = json.loads(row["broker_ids_json"])
            if not ids:
                raise SessionError("No broker GTT id to cancel; unresolved intent requires reconciliation")
            try:
                # Refresh immediately before cancellation so a newly filled
                # entry never has its protection removed using a stale view.
                account = runtime.executor.account()
                runtime.account = account
                if not self._reconcile(runtime, account):
                    self.store.transition_intent(row["intent_id"], "UNRESOLVED")
                    raise SessionError(
                        "Fresh reconciliation is incomplete; cancellation was not attempted"
                    )
                if self.store.intent(row["intent_id"])["state"] in {"CLOSED", "REJECTED", "CANCELLED"}:
                    self._persist(runtime)
                    return
                if any(p.instrument_key == instrument_key and p.quantity
                       for p in (runtime.account.positions if runtime.account else ())):
                    raise SessionError("Refusing to cancel protection for an open position")
                self.store.update_intent_details(row["intent_id"], {
                    "uncertain_operation": "CANCEL",
                    "cancel_reconciliation_required": True,
                    "cancel_attempted_at": self.now().isoformat(),
                })
                self.store.transition_intent(row["intent_id"], "CANCEL_ATTEMPTED")
                submission = runtime.executor.cancel_gtt(
                    ids[0], self._permit(runtime, "CANCEL_UNFILLED"), session_id=runtime.session_id
                )
                self.store.transition_intent(row["intent_id"], "ACKNOWLEDGED_UNRECONCILED",
                                             broker_ids=list(submission.broker_ids))
                self._reconcile(runtime, runtime.executor.account())
            except SessionError:
                raise
            except (AmbiguousSubmission, UpstoxRejected, ReconciliationFailure) as exc:
                self.store.transition_intent(row["intent_id"], "UNRESOLVED")
                if emergency:
                    runtime.fault = Fault.AMBIGUOUS_SUBMISSION.value
                    runtime.fault_reason = f"Emergency cancellation unresolved: {type(exc).__name__}"
                else:
                    self._halt(runtime, Fault.AMBIGUOUS_SUBMISSION,
                               f"Cancellation requires reconciliation: {type(exc).__name__}")
            self._persist(runtime)

    def public(self, session_id: str) -> dict:
        runtime = self._runtime(session_id)
        account = runtime.account
        account_public = None
        if account is not None:
            account_public = {
                "equity": account.equity, "available_margin": account.available_margin,
                "used_margin": account.used_margin, "day_pnl": account.day_pnl,
                "authoritative": account.authoritative, "observed_at": account.observed_at.isoformat(),
                "positions": [position.public() for position in account.positions],
            }
        return {
            "session_id": session_id, "mode": runtime.mode, "state": runtime.state,
            "paper_only": runtime.mode == "PAPER", "simulated": runtime.mode == "PAPER",
            "live_execution": runtime.mode == "UPSTOX_LIVE",
            "live_enabled_by_environment": self.settings.live_enabled,
            "broker_mutations_reachable": bool(
                runtime.mode == "UPSTOX_LIVE" and runtime.executor
                and getattr(runtime.executor, "broker_mutations_reachable", False)
            ),
            "authorized_day": runtime.authorized_day,
            "risk": runtime.risk.public() if runtime.risk else None,
            "entries_blocked": runtime.entries_blocked,
            "fault": runtime.fault, "fault_reason": runtime.fault_reason,
            "fresh_approval_required": runtime.state in {Lifecycle.HALTED.value,
                                                           Lifecycle.EMERGENCY_STOPPED.value,
                                                           Lifecycle.PAUSED.value},
            "instruments": [item.public() for item in runtime.instruments.values()],
            "account": account_public,
            "kronos": self.kronos_status,
            "evidence": self.evidence.public(),
            "diagnostics": runtime.diagnostics[-100:],
            "working_instruments": sorted(runtime.working),
            "orders": [self._order_view(record) for record in runtime.orders.values()],
            "intents": self._public_intents(session_id),
            "emergency": runtime.emergency,
            "recovered": runtime.recovered,
            "recovery_reconciled": runtime.recovery_reconciled,
            "recovery_error": runtime.recovery_error,
            "stop_requested": runtime.stop_requested.is_set(),
            "scan_count": runtime.scan_count,
            "last_scan": runtime.last_scan.isoformat() if runtime.last_scan else None,
            "last_scan_kind": runtime.last_scan_kind,
            "scan_endpoint_effect": "READ_ONLY_NO_ORDER_MUTATIONS",
            "readiness": self.readiness(session_id),
            "operator_note": "PAPER is local simulation; UPSTOX_LIVE is real-money only after daily authorization, Arm and Activate. Scan once is read-only.",
        }

    def sessions(self) -> list[dict]:
        with self._lock:
            return [self.public(session_id) for session_id in self.runtimes]

    def audit(self, session_id: str, limit: int = 200) -> dict:
        self._runtime(session_id)
        return {"events": self.store.events(session_id=session_id, limit=limit),
                "hash_chain_valid": self.store.verify_chain(),
                "credentials_stored": False, "permits_persisted": False}

    def close(self) -> None:
        self.kronos_setup.close()
        with self._lock:
            for runtime in self.runtimes.values():
                runtime.stop_requested.set()
                if runtime.state in {Lifecycle.ACTIVE.value, Lifecycle.ARMED.value, Lifecycle.PAUSED.value}:
                    self._set_state(runtime, Lifecycle.HALTED.value,
                                    reason="Service shutdown; fresh operator approval required")
                runtime.entries_blocked = True
                self._persist(runtime)
            self._closed = True
