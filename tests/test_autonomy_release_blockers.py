"""Paper-only regressions for the v3.7.0a5 pre-session audit blockers."""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import math
from pathlib import Path
import sqlite3

from fastapi.testclient import TestClient
import httpx
import pytest

from app.api.server import create_app
from app.autonomy.feed import SessionWindow, UpstoxCompletedBarFeed
from app.autonomy.service import NotReady
from app.autonomy.store import AutonomyStore
from app.autonomy.upstox import ReconciliationFailure, UpstoxReadOnly
from app.config.settings import Settings
from app.data.models import Candle
from tests.test_autonomy_service import (
    KEY, NOW, _reopen_after_crash, activate, make_instrument, make_service,
)


def _candle_feed(rows):
    def handler(request):
        assert request.url.path.startswith("/v3/historical-candle/intraday/")
        return httpx.Response(200, json={"status": "success", "data": {"candles": rows}})

    reader = UpstoxReadOnly(
        lambda: "test-only-not-a-credential",
        transport=httpx.MockTransport(handler), clock=lambda: NOW,
    )
    return UpstoxCompletedBarFeed(reader, clock=lambda: NOW)


def _row(timestamp):
    return [timestamp, 100.0, 100.3, 99.8, 100.1, 1000, 0]


@pytest.mark.parametrize("newest_first", [False, True])
def test_v3_candles_are_normalized_before_downstream_validation(newest_first):
    stamps = [
        "2026-10-25T09:57:00+00:00",
        "2026-10-25T09:58:00+00:00",
        "2026-10-25T09:59:00+00:00",
    ]
    rows = [_row(stamp) for stamp in stamps]
    if newest_first:
        rows.reverse()
    bars = _candle_feed(rows).completed(make_instrument(), "1m")
    assert [bar.timestamp.isoformat() for bar in bars] == [
        "2026-10-25T09:57:00+00:00",
        "2026-10-25T09:58:00+00:00",
        "2026-10-25T09:59:00+00:00",
    ]


def test_v3_duplicate_candle_timestamp_is_rejected_not_deduplicated():
    rows = [_row("2026-10-25T09:59:00+00:00")] * 2
    with pytest.raises(ReconciliationFailure, match="Duplicate candle timestamp"):
        _candle_feed(rows).completed(make_instrument(), "1m")


@pytest.mark.parametrize("timestamp", [None, "", "not-a-time", "2026-10-25T09:59:00"])
def test_v3_missing_invalid_or_naive_timestamp_is_rejected(timestamp):
    with pytest.raises(ReconciliationFailure, match="timestamp is missing or invalid"):
        _candle_feed([_row(timestamp)]).completed(make_instrument(), "1m")


def test_v3_normalized_sub_timeframe_timestamp_is_still_rejected():
    rows = [
        _row("2026-10-25T09:58:30+00:00"),
        _row("2026-10-25T09:58:00+00:00"),
    ]
    with pytest.raises(ValueError, match="Overlapping candles"):
        _candle_feed(rows).completed(make_instrument(), "1m")


def _nullable_legacy_audit_db(tmp_path: Path) -> None:
    path = tmp_path / "autonomy" / "journal.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
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


@pytest.mark.parametrize("stored", ["missing", "sql_null", "json_null", "malformed", "valid"])
def test_kronos_status_degrades_safely_for_stored_event_payloads(tmp_path, stored):
    _nullable_legacy_audit_db(tmp_path)
    settings = replace(
        Settings(), api_token="test-only-local-api",
        database_url=f"sqlite:///{tmp_path}/application.sqlite3",
    )
    with TestClient(create_app(settings, data_dir=tmp_path), raise_server_exceptions=False) as web:
        store = web.app.state.autonomy.store
        if stored != "missing":
            payload = {
                "sql_null": None,
                "json_null": "null",
                "malformed": "{not-json",
                "valid": '{"safe":true}',
            }[stored]
            store.db.execute(
                "INSERT INTO audit(ts,session_id,event_type,payload_json,prev_hash,event_hash) "
                "VALUES(?,?,?,?,?,?)",
                ("2026-10-26T00:00:00+00:00", None, "KRONOS_SETUP_STARTED",
                 payload, "0" * 64, "test-only-not-a-chain-proof"),
            )
        headers = {"Authorization": "Bearer test-only-local-api"}
        response = web.get("/api/autonomy/kronos", headers=headers)
        assert response.status_code == 200, response.text
        assert "{not-json" not in response.text
        view = response.json()
        events = view["audit_events"]
        if stored == "missing":
            assert events == [] and view["audit_status"] == "NO_EVENTS"
        else:
            assert len(events) == 1
            expected_status = {
                "sql_null": "SQL_NULL", "json_null": "JSON_NULL",
                "malformed": "MALFORMED", "valid": "OK",
            }[stored]
            assert events[0]["payload_status"] == expected_status
            if stored == "valid":
                assert events[0]["payload"] == {"safe": True}
                assert view["audit_status"] == "OK"
            else:
                assert events[0]["payload"] is None
                assert view["audit_status"] == "DEGRADED"
                assert view["status"] == "DEGRADED"
                assert view["last_inference"] is None
                assert view["audit_degraded_events"] == [
                    {"seq": events[0]["seq"], "payload_status": expected_status}
                ]
            if stored in {"sql_null", "malformed"}:
                assert store.verify_chain() is False
        # The aggregate status route shares this optional Kronos read path.
        assert web.get("/api/autonomy/status", headers=headers).status_code == 200


def _assert_persistence_halt(view):
    assert view["state"] == "HALTED"
    assert view["fault"] == "PERSISTENCE_FAILED"
    assert view["entries_blocked"] is True
    assert view["stop_requested"] is True
    assert view["persistence_degraded"] is True
    assert view["durable_view_stale"] is True
    assert view["persistence_status"] == "DEGRADED"
    assert view["recovery_reconciled"] is False
    assert "durable view may be stale" in view["fault_reason"]


def test_session_update_failure_revokes_permits_and_cannot_look_active(tmp_path, monkeypatch):
    service, store, feed, clock, session_id = make_service(tmp_path)
    try:
        def fail_write(*args, **kwargs):
            raise sqlite3.OperationalError("test-only session update failure")

        monkeypatch.setattr(store, "update_session", fail_write)
        with pytest.raises(sqlite3.OperationalError):
            service.arm(session_id)
        view = service.public(session_id)
        _assert_persistence_halt(view)
        assert "durable session marker failed" in view["persistence_reason"]
        for kind in ("ENTRY", "PROTECTIVE", "CANCEL_UNFILLED"):
            with pytest.raises(PermissionError):
                service._permit(service._runtime(session_id), kind)
        assert store.intents(session_id) == []
        assert service._runtime(session_id).executor.gtt == {}
    finally:
        monkeypatch.undo()
        service.close()
        store.close()


def test_intent_creation_failure_rolls_back_and_prevents_retry(tmp_path, monkeypatch):
    service, store, feed, clock, session_id = make_service(tmp_path)
    try:
        activate(service, session_id)
        calls = []

        def fail_write(*args, **kwargs):
            calls.append(args)
            raise sqlite3.OperationalError("test-only intent insert failure")

        monkeypatch.setattr(store, "record_intent", fail_write)
        with pytest.raises(sqlite3.OperationalError):
            service.execute_once(session_id)
        view = service.public(session_id)
        _assert_persistence_halt(view)
        assert len(calls) == 1
        assert store.intents(session_id) == []
        assert service._runtime(session_id).executor.gtt == {}
        # A halted worker iteration is a no-op, not an automatic order retry.
        assert service.execute_once(session_id)["state"] == "HALTED"
        assert len(calls) == 1
    finally:
        monkeypatch.undo()
        service.close()
        store.close()


def test_runtime_write_failure_matches_original_audit_regression(tmp_path, monkeypatch):
    service, store, feed, clock, session_id = make_service(tmp_path)
    try:
        activate(service, session_id)

        def fail_write(*args, **kwargs):
            raise sqlite3.OperationalError("test-only runtime write failure")

        monkeypatch.setattr(store, "save_runtime", fail_write)
        with pytest.raises(sqlite3.OperationalError):
            service.execute_once(session_id)
        view = service.public(session_id)
        _assert_persistence_halt(view)
        assert "durable snapshot marker failed" in view["persistence_reason"]
        with pytest.raises(PermissionError):
            service._permit(service._runtime(session_id), "ENTRY")
    finally:
        monkeypatch.undo()
        service.close()
        store.close()


def _append_fill_bar(feed, clock):
    previous = feed.bars[-1]
    feed.bars.append(Candle(
        NOW, previous.close - 1.0, previous.close + 0.5,
        previous.close - 1.2, previous.close - 0.5, 100000, previous.symbol,
    ))
    clock[0] = NOW + timedelta(minutes=1)


def test_fill_snapshot_failure_preserves_intent_and_restart_is_disarmed(tmp_path, monkeypatch):
    service, store, feed, clock, session_id = make_service(tmp_path)
    recovered_service = None
    recovered_store = None
    try:
        activate(service, session_id)
        assert service.execute_once(session_id)["working_instruments"] == [KEY]
        _append_fill_bar(feed, clock)

        def fail_write(*args, **kwargs):
            raise sqlite3.OperationalError("test-only post-fill snapshot failure")

        monkeypatch.setattr(store, "save_runtime", fail_write)
        with pytest.raises(sqlite3.OperationalError):
            service.execute_once(session_id)
        view = service.public(session_id)
        _assert_persistence_halt(view)
        # The local in-memory fill remains visible, explicitly unpersisted;
        # the durable intent has not been falsely marked PROTECTED/reconciled.
        assert view["account"]["positions"]
        assert view["orders"]
        assert store.intents(session_id)[0]["state"] == "WORKING"
        assert service.execute_once(session_id)["state"] == "HALTED"
        monkeypatch.undo()

        recovered_service, recovered_store = _reopen_after_crash(tmp_path, service, store)
        recovered = recovered_service.public(session_id)
        assert recovered["state"] == "HALTED"
        assert recovered["recovered"] is True
        assert recovered["stop_requested"] is True
        assert recovered["entries_blocked"] is True
        assert recovered["authorized_day"] == "" and recovered["risk"] is None
        assert recovered["persistence_degraded"] is False
        assert recovered["durable_view_stale"] is True
        assert recovered["recovery_reconciled"] is False
        # Only the last durable WORKING snapshot is restored.  The unpersisted
        # fill is not invented, and the GTT/intent remains available to reconcile.
        assert recovered["account"]["positions"] == []
        assert recovered["orders"] and recovered["intents"]
        assert recovered_store.intents(session_id)[0]["state"] == "WORKING"
        with pytest.raises(PermissionError):
            recovered_service._permit(recovered_service._runtime(session_id), "ENTRY")
        reconciled = recovered_service.reconcile(session_id)
        assert reconciled["state"] == "HALTED"
        assert reconciled["recovery_reconciled"] is True
        assert reconciled["durable_view_stale"] is False
        assert reconciled["account"]["positions"] == []
        assert recovered_store.intents(session_id)[0]["state"] == "WORKING"
    finally:
        monkeypatch.undo()
        if recovered_service is None:
            service.close()
        if recovered_store is None:
            store.close()
        else:
            recovered_service.close()
            recovered_store.close()


class FixedCalendar:
    def __init__(self, start, end):
        self.session = SessionWindow(start, end)

    def window(self, instrument, now):
        return self.session

    def is_open(self, instrument, now):
        return self.session.contains(now)


def test_eod_cancel_write_failure_does_not_claim_reconciliation(tmp_path, monkeypatch):
    service, store, feed, clock, session_id = make_service(tmp_path)
    try:
        activate(service, session_id)
        service.execute_once(session_id)
        runtime = service._runtime(session_id)
        runtime.calendar = FixedCalendar(NOW - timedelta(hours=1), NOW)
        clock[0] = NOW + timedelta(minutes=1)
        original_transition = store.transition_intent

        def fail_terminal_write(intent_id, state, **kwargs):
            if state == "CANCELLED":
                raise sqlite3.OperationalError("test-only EOD terminal-write failure")
            return original_transition(intent_id, state, **kwargs)

        monkeypatch.setattr(store, "transition_intent", fail_terminal_write)
        with pytest.raises(sqlite3.OperationalError):
            service.execute_once(session_id)
        view = service.public(session_id)
        _assert_persistence_halt(view)
        # The local adapter cancelled, but its terminal journal write failed.
        # Keep the last durable non-terminal intent and expose the local order;
        # do not write a MARKET_CLOSE success event or claim cleanup completed.
        assert store.intents(session_id)[0]["state"] == "ACKNOWLEDGED_UNRECONCILED"
        assert view["orders"]
        assert not any(event["event_type"] == "MARKET_CLOSE"
                       for event in store.events(session_id=session_id))
        with pytest.raises(PermissionError):
            service._permit(runtime, "CANCEL_UNFILLED")
    finally:
        monkeypatch.undo()
        service.close()
        store.close()


def _next_bar(previous, index):
    close = round((100 + index * 0.3 + math.sin(index * 0.8) * 2) / 0.05) * 0.05
    open_price = round((close - 0.1) / 0.05) * 0.05
    high = round((max(open_price, close) + 0.3) / 0.05) * 0.05
    low = round((min(open_price, close) - 0.3 - (1 if index % 5 == 0 else 0)) / 0.05) * 0.05
    return Candle(
        previous.timestamp + timedelta(minutes=1), open_price, high, low,
        close, 100000, previous.symbol,
    )


def test_375_bar_session_closes_reconciles_and_restarts_disarmed(tmp_path):
    service, store, feed, clock, session_id = make_service(tmp_path)
    recovered_service = None
    recovered_store = None
    try:
        start = NOW
        close = NOW + timedelta(minutes=374)
        service._runtime(session_id).calendar = FixedCalendar(start, close)
        clock[0] = start - timedelta(minutes=1)
        service.arm(session_id)
        with pytest.raises(NotReady, match="closed"):
            service.activate(session_id)
        clock[0] = start
        service.activate(session_id)

        processed_count = 0
        last_marker = None
        for index in range(375):
            clock[0] = start + timedelta(minutes=index)
            if index:
                feed.bars.append(_next_bar(feed.bars[-1], 79 + index))
            view = service.execute_once(session_id)
            assert view["mode"] == "PAPER" and view["state"] == "ACTIVE"
            assert view["broker_mutations_reachable"] is False
            marker = service._runtime(session_id).processed[KEY]
            if marker != last_marker:
                processed_count += 1
                last_marker = marker
        assert processed_count == 375
        assert last_marker == close - timedelta(minutes=1)
        intent_count_before = len(store.intents(session_id))
        assert intent_count_before > 0
        assert any(row["state"] == "WORKING" for row in store.intents(session_id))
        service.execute_once(session_id)
        assert len(store.intents(session_id)) == intent_count_before

        clock[0] = close + timedelta(minutes=1)
        closed = service.execute_once(session_id)
        assert closed["state"] == "HALTED"
        assert closed["fault"] == "MARKET_CLOSED"
        assert closed["entries_blocked"] is True and closed["stop_requested"] is True
        assert closed["working_instruments"] == []
        assert closed["account"]["positions"] == []
        assert all(row["state"] in {"CLOSED", "CANCELLED", "REJECTED"}
                   for row in store.intents(session_id))
        close_events = [event for event in store.events(session_id=session_id)
                        if event["event_type"] == "MARKET_CLOSE"]
        assert len(close_events) == 1
        assert close_events[0]["payload"]["reconciled"] is True
        assert close_events[0]["payload"]["cleanup_pending"] is False
        assert close_events[0]["payload"]["broker_mutation"] is False
        expected_paper = service._runtime(session_id).executor.snapshot()

        recovered_service, recovered_store = _reopen_after_crash(tmp_path, service, store)
        recovered = recovered_service.public(session_id)
        assert recovered["state"] == "HALTED" and recovered["recovered"] is True
        assert recovered["stop_requested"] is True and recovered["entries_blocked"] is True
        assert recovered["authorized_day"] == "" and recovered["risk"] is None
        runtime = recovered_service._runtime(session_id)
        assert runtime.executor.snapshot()["positions"] == expected_paper["positions"]
        assert runtime.executor.snapshot()["gtt"] == expected_paper["gtt"]
        assert recovered_store.verify_chain()
    finally:
        if recovered_service is None:
            service.close()
            store.close()
        else:
            recovered_service.close()
            recovered_store.close()


def test_market_close_preserves_open_position_and_protection(tmp_path):
    service, store, feed, clock, session_id = make_service(tmp_path)
    try:
        activate(service, session_id)
        service.execute_once(session_id)
        _append_fill_bar(feed, clock)
        assert service.execute_once(session_id)["account"]["positions"]
        runtime = service._runtime(session_id)
        runtime.calendar = FixedCalendar(NOW - timedelta(hours=1), NOW + timedelta(minutes=1))
        clock[0] = NOW + timedelta(minutes=2)
        closed = service.execute_once(session_id)
        assert closed["state"] == "HALTED" and closed["fault"] == "MARKET_CLOSED"
        assert closed["account"]["positions"]
        assert store.intents(session_id)[0]["state"] == "PROTECTED"
        position = runtime.executor.positions[KEY]
        record = runtime.executor.gtt[position.protection_id]
        assert {rule["status"] for rule in record["rules"][1:]} == {"SCHEDULED"}
        event = next(event for event in store.events(session_id=session_id)
                     if event["event_type"] == "MARKET_CLOSE")
        assert event["payload"]["open_positions"] == [KEY]
        assert event["payload"]["cancellation_attempts"] == 0
        assert event["payload"]["forced_close"] is False
        assert event["payload"]["reconciled"] is True
    finally:
        service.close()
        store.close()


def test_read_only_close_scan_never_cancels_and_marks_operator_cleanup(tmp_path):
    service, store, feed, clock, session_id = make_service(tmp_path)
    try:
        activate(service, session_id)
        service.execute_once(session_id)
        runtime = service._runtime(session_id)
        runtime.calendar = FixedCalendar(NOW - timedelta(hours=1), NOW)
        clock[0] = NOW + timedelta(minutes=1)
        closed = service.scan_once(session_id)
        assert closed["state"] == "HALTED" and closed["fault"] == "MARKET_CLOSED"
        assert "explicit operator cancellation" in closed["fault_reason"]
        assert store.intents(session_id)[0]["state"] == "WORKING"
        record = next(iter(runtime.executor.gtt.values()))
        assert record["rules"][0]["status"] == "OPEN"
        event = next(event for event in store.events(session_id=session_id)
                     if event["event_type"] == "MARKET_CLOSE")
        assert event["payload"]["scan_kind"] == "READ_ONLY"
        assert event["payload"]["automatic_unfilled_paper_cancellation"] is False
        assert event["payload"]["cleanup_pending"] is True
        assert event["payload"]["cancellation_attempts"] == 0
        cleaned = service.cancel_unfilled(session_id, KEY)
        assert cleaned["state"] == "HALTED"
        assert store.intents(session_id)[0]["state"] == "CANCELLED"
    finally:
        service.close()
        store.close()
