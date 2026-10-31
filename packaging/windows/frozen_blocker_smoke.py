"""Frozen-package PAPER smoke checks for the a5 audit blocker repairs.

This diagnostic is deliberately self-contained: it creates temporary journals,
uses fake readers and local PaperExecution, and never reads operator settings,
credentials, evidence, or databases.  It makes no broker order request and does
not run or emulate a full trading day.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sqlite3
import tempfile

import httpx

from app import __version__
from app.autonomy.config import AutonomySettings
from app.autonomy.feed import SessionWindow, UpstoxCompletedBarFeed
from app.autonomy.models import AccountProfile, Instrument, ProtectedOrder
from app.autonomy.paper import PaperExecution
from app.autonomy.service import AutonomyService
from app.autonomy.store import AutonomyStore
from app.autonomy.strategy import EvidenceRegistry
from app.autonomy.upstox import InstrumentCatalog, Quote, UpstoxReadOnly
from app.data.models import Candle

NOW = datetime(2026, 10, 25, 10, 0, tzinfo=timezone.utc)
KEY = "NSE_EQ|INE000000000"
REQUEST_PATHS: list[str] = []


def instrument() -> Instrument:
    return Instrument(
        KEY, "NSE_EQ-INE000000000", "NSE_EQ", "NSE", "TEST", "EQ",
        0.05, 1, 100000, 1.0, short_route_metadata=True,
    )


def bars() -> list[Candle]:
    result = []
    for index in range(80):
        close = round((100 + index * 0.3 + math.sin(index * 0.8) * 2) / 0.05) * 0.05
        open_price = round((close - 0.1) / 0.05) * 0.05
        high = round((max(open_price, close) + 0.3) / 0.05) * 0.05
        low = round((min(open_price, close) - 0.3 - (index % 5 == 0)) / 0.05) * 0.05
        result.append(Candle(
            NOW - timedelta(minutes=80 - index), open_price, high, low,
            close, 100000, instrument().display_symbol,
        ))
    return result


class Feed:
    def __init__(self, rows: list[Candle]):
        self.bars = rows

    def completed(self, item, timeframe):
        return list(self.bars)


class Reader:
    def __init__(self, feed: Feed, clock):
        self.feed = feed
        self.clock = clock

    def profile(self, **kwargs):
        return AccountProfile(
            True, frozenset({"NSE"}), frozenset({"I"}), frozenset({"LIMIT", "SL"}),
            "fingerprint", frozenset({"1.2.3.4"}),
        )

    def quote(self, key):
        close = self.feed.bars[-1].close
        return Quote(key, close - 0.05, close + 0.05, close, 100000, self.clock())

    def market_timings(self, day):
        return {"NSE": (self.clock() - timedelta(hours=2), self.clock() + timedelta(hours=2))}

    def server_time(self):
        return self.clock()


class FixedCalendar:
    def __init__(self, start, end):
        self.session = SessionWindow(start, end)

    def window(self, item, now):
        return self.session

    def is_open(self, item, now):
        return self.session.contains(now)


def make_service(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    clock = [NOW]
    feed = Feed(bars())
    reader = Reader(feed, lambda: clock[0])
    item = instrument()
    settings = AutonomySettings(
        live_enabled=False, worker_enabled=False,
        # Explicit nonzero bounded hard limits sized for this smoke fixture.
        max_order_quantity=100_000.0,
        max_order_notional=50_000_000.0,
        max_per_symbol_exposure=50_000_000.0,
        max_aggregate_exposure=100_000_000.0,
        max_open_positions=50,
        max_daily_loss_ceiling=1_000_000.0,
        max_daily_drawdown=500_000.0,
        max_daily_order_submits=1_000,
    )
    catalog = InstrumentCatalog(settings, root / "catalog.json", clock=lambda: clock[0])
    catalog.instruments = {KEY: item}
    catalog.refreshed_at = clock[0]
    store = AutonomyStore(root / "journal.sqlite3")
    service = AutonomyService(
        settings, store, catalog, data_dir=root,
        reader_factory=lambda provider: reader,
        live_factory=lambda provider: (_ for _ in ()).throw(
            AssertionError("PAPER smoke must not construct a LIVE executor")
        ),
        recovery_factory=lambda provider: (_ for _ in ()).throw(
            AssertionError("PAPER smoke must not construct a recovery broker")
        ),
        feed_factory=lambda reader: feed,
        token_provider=lambda: "test-only-not-a-credential",
        public_ip_provider=lambda: (_ for _ in ()).throw(
            AssertionError("PAPER smoke must not request a public IP")
        ),
        evidence=EvidenceRegistry([]),
        clock=lambda: clock[0],
    )
    session_id = service.create_session("PAPER", [KEY])["session_id"]
    service.authorize(session_id)
    service.set_risk(
        session_id, max_daily_loss=10000, per_trade_risk_fraction=0.005,
        equity_at_entry=1000000,
    )
    return service, store, feed, clock, session_id


def activate(service: AutonomyService, session_id: str) -> None:
    service.arm(session_id)
    assert service.activate(session_id)["state"] == "ACTIVE"


def close_service(service: AutonomyService, store: AutonomyStore) -> None:
    service.close()
    store.close()


def check_candle_ordering() -> None:
    stamps = [
        "2026-10-25T09:57:00+00:00",
        "2026-10-25T09:58:00+00:00",
        "2026-10-25T09:59:00+00:00",
    ]

    def handler(request):
        REQUEST_PATHS.append(request.url.path)
        assert request.url.path.startswith("/v3/historical-candle/intraday/")
        rows = [[stamp, 100.0, 100.3, 99.8, 100.1, 1000, 0] for stamp in reversed(stamps)]
        return httpx.Response(200, json={"status": "success", "data": {"candles": rows}})

    reader = UpstoxReadOnly(
        lambda: "test-only-not-a-credential",
        transport=httpx.MockTransport(handler), clock=lambda: NOW,
    )
    result = UpstoxCompletedBarFeed(reader, clock=lambda: NOW).completed(instrument(), "1m")
    assert [bar.timestamp.isoformat() for bar in result] == stamps


def legacy_nullable_audit(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / "journal.sqlite3")
    db.execute(
        """CREATE TABLE audit (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            session_id TEXT,
            event_type TEXT NOT NULL,
            payload_json TEXT,
            prev_hash TEXT NOT NULL,
            event_hash TEXT NOT NULL
        )"""
    )
    db.commit()
    db.close()


def check_null_event(root: Path) -> None:
    legacy_nullable_audit(root)
    service, store, _feed, _clock, _session_id = make_service(root)
    try:
        store.db.execute(
            "INSERT INTO audit(ts,session_id,event_type,payload_json,prev_hash,event_hash) "
            "VALUES(?,?,?,?,?,?)",
            (NOW.isoformat(), None, "KRONOS_SETUP_STARTED", None, "0" * 64, "not-a-proof"),
        )
        view = service.kronos_view()
        assert view["status"] == "DEGRADED"
        assert view["audit_status"] == "DEGRADED"
        assert view["audit_events"][0]["payload_status"] == "SQL_NULL"
        assert view["audit_events"][0]["payload"] is None
        assert store.verify_chain() is False
    finally:
        close_service(service, store)


def check_database_failure(root: Path) -> None:
    service, store, _feed, _clock, session_id = make_service(root)
    original = store.save_runtime
    try:
        activate(service, session_id)

        def fail_write(*args, **kwargs):
            raise sqlite3.OperationalError("test-only frozen runtime write failure")

        store.save_runtime = fail_write
        try:
            service.execute_once(session_id)
        except sqlite3.OperationalError:
            pass
        else:
            raise AssertionError("runtime write failure was not propagated")
        view = service.public(session_id)
        assert view["state"] == "HALTED" and view["fault"] == "PERSISTENCE_FAILED"
        assert view["entries_blocked"] and view["stop_requested"]
        assert view["persistence_degraded"] and view["durable_view_stale"]
        try:
            service._permit(service._runtime(session_id), "ENTRY")
        except PermissionError:
            pass
        else:
            raise AssertionError("persistence failure left an ENTRY permit available")
    finally:
        store.save_runtime = original
        close_service(service, store)


def seed_working_paper_intent(service: AutonomyService, store: AutonomyStore,
                              session_id: str) -> None:
    """Set up close-test state without claiming signal/evidence/risk coverage.

    This is not a candidate submission test. It creates a durable intent and a
    local three-leg PAPER GTT through the normal journal, short-lived ACTIVE
    permit and adapter boundaries, then reconciles it to WORKING. No evidence
    object is marked non-synthetic and no research/execution gate is relaxed.
    """
    runtime = service._runtime(session_id)
    assert isinstance(runtime.executor, PaperExecution)
    order = ProtectedOrder(
        intent_id="frozen-close-intent", instrument_key=KEY, side="BUY",
        quantity=1, limit_price=100.0, stop_price=95.0, target_price=105.0,
    )
    assert store.record_intent(
        order.intent_id, session_id, "frozen-close-seed", KEY,
        {"order": order.__dict__, "setup": "close-test-state-only"}, None,
    )
    store.transition_intent(order.intent_id, "SUBMISSION_ATTEMPTED")
    submission = runtime.executor.place_protected_entry(
        order, service._permit(runtime, "ENTRY"),
        session_id=session_id, instrument=runtime.instruments[KEY],
    )
    store.transition_intent(
        order.intent_id, "ACKNOWLEDGED_UNRECONCILED",
        broker_ids=list(submission.broker_ids),
    )
    runtime.working.add(KEY)
    assert service._reconcile(runtime, runtime.executor.account()) is True
    assert service.public(session_id)["working_instruments"] == [KEY]


def check_market_close(root: Path) -> None:
    service, store, _feed, clock, session_id = make_service(root)
    try:
        activate(service, session_id)
        # Process one valid completed-bar scan with an empty evidence registry;
        # this sets the local adapter's deterministic clock without an order.
        assert service.execute_once(session_id)["state"] == "ACTIVE"
        seed_working_paper_intent(service, store, session_id)
        working = service.public(session_id)
        assert working["working_instruments"] == [KEY]
        runtime = service._runtime(session_id)
        assert runtime.executor.broker_mutations_reachable is False
        runtime.calendar = FixedCalendar(NOW - timedelta(hours=1), NOW)
        clock[0] = NOW + timedelta(minutes=1)
        closed = service.execute_once(session_id)
        assert closed["state"] == "HALTED" and closed["fault"] == "MARKET_CLOSED"
        assert closed["working_instruments"] == [] and closed["entries_blocked"]
        assert all(row["state"] in {"CLOSED", "CANCELLED", "REJECTED"}
                   for row in store.intents(session_id))
        event = next(event for event in store.events(session_id=session_id)
                     if event["event_type"] == "MARKET_CLOSE")
        assert event["payload"]["reconciled"] is True
        assert event["payload"]["broker_mutation"] is False
    finally:
        close_service(service, store)


def run() -> int:
    completed = []
    try:
        with tempfile.TemporaryDirectory(prefix="axiom-frozen-paper-smoke-") as temporary:
            root = Path(temporary)
            check_candle_ordering()
            completed.append("candle_ordering")
            check_null_event(root / "null-event")
            completed.append("null_event_status")
            check_database_failure(root / "database-failure")
            completed.append("database_failure_fail_closed")
            check_market_close(root / "market-close")
            completed.append("market_close_cleanup")
        assert REQUEST_PATHS and all(
            path.startswith("/v3/historical-candle/intraday/") for path in REQUEST_PATHS
        )
        print(json.dumps({
            "status": "PASS", "version": __version__, "checks": completed,
            "mode": "PAPER", "live_enabled": False,
            "broker_order_endpoint_calls": 0,
            "network_transport": "httpx.MockTransport historical-candle read only",
            "evidence": "synthetic frozen smoke; not a full-day, owner-device or market result",
        }, separators=(",", ":")))
        return 0
    except Exception as exc:
        print(json.dumps({
            "status": "FAIL", "version": __version__, "checks": completed,
            "error_type": type(exc).__name__, "mode": "PAPER", "live_enabled": False,
            "broker_order_endpoint_calls": 0,
        }, separators=(",", ":")))
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
