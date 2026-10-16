"""Regressions for durable submission identity and fail-closed transitions."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.database.database import Database
from app.database.repository import Repository
from app.engine.state import (
    DURABLY_PERSISTED,
    REMOTE_LOOKUP,
    SUBMISSION_ATTEMPTED,
    UNKNOWN,
    UNRESOLVED,
    VERIFIED_ABSENCE,
    SubmissionJournal,
)


def _repository(tmp_path, name="journal.db"):
    database = Database(f"sqlite:///{tmp_path / name}")
    return database, Repository(database)


def _record(journal, intent_id="intent-1", **overrides):
    values = {
        "instrument_key": "NSE_EQ|INE002A01018",
        "symbol": "RELIANCE",
        "side": "BUY",
        "quantity": 2,
        "price": 100.0,
        "strategy": "regression",
        "params_version": "v1",
        "tag": intent_id[:12],
    }
    values.update(overrides)
    return journal.record(intent_id, **values)


def test_submission_intents_are_not_evicted_and_survive_reopen(tmp_path):
    database, repository = _repository(tmp_path)
    now = "2026-10-05T00:00:00+00:00"
    entries = []
    for index in range(2000):
        intent_id = f"retained-{index:04d}"
        entry = {
            "state": UNKNOWN,
            "instrument_key": "NSE_EQ|INE002A01018",
            "symbol": "RELIANCE",
            "side": "BUY",
            "quantity": 1,
            "price": 100.0,
            "strategy": "regression",
            "params_version": "v1",
            "tag": intent_id,
            "upstox_order_ids": [],
            "created_ts": now,
            "updated_ts": now,
        }
        entries.append((intent_id, json.dumps(entry)))
    with database.transaction() as db:
        db.executemany(
            "INSERT INTO submission_intents(intent_id,payload) VALUES (?,?)",
            entries)

    journal = SubmissionJournal(repository)
    _record(journal, "intent-after-old-limit")
    assert journal.get("retained-0000")["state"] == UNKNOWN
    assert journal.get("retained-1999")["state"] == UNKNOWN
    assert journal.resubmission_forbidden("retained-0000")
    database.close()

    reopened, reopened_repository = _repository(tmp_path)
    reopened_journal = SubmissionJournal(reopened_repository)
    assert reopened_journal.get("retained-0000")["state"] == UNKNOWN
    assert reopened_journal.get("intent-after-old-limit")["state"] == DURABLY_PERSISTED
    with reopened.transaction() as db:
        count = db.execute("SELECT COUNT(*) FROM submission_intents").fetchone()[0]
    assert count == 2001
    reopened.close()


def test_legacy_engine_state_journal_migrates_once_without_resetting_intent(tmp_path):
    database, repository = _repository(tmp_path)
    legacy = {
        "state": UNKNOWN,
        "instrument_key": "NSE_EQ|INE002A01018",
        "symbol": "RELIANCE",
        "side": "BUY",
        "quantity": 2.0,
        "price": 100.0,
        "strategy": "legacy",
        "params_version": "v1",
        "tag": "legacy-1",
        "upstox_order_ids": [],
        "created_ts": "2026-10-04T00:00:00+00:00",
        "updated_ts": "2026-10-04T00:00:00+00:00",
    }
    repository.set_engine_state(SubmissionJournal.KEY, {"legacy-1": legacy})

    journal = SubmissionJournal(repository)
    assert journal.get("legacy-1") == legacy
    assert journal.resubmission_forbidden("legacy-1")
    # A retry of the same id retains its uncertain old state; it cannot create
    # a fresh DURABLY_PERSISTED entry that would authorize another POST.
    assert journal.record(
        "legacy-1", instrument_key="NSE_EQ|INE002A01018", symbol="RELIANCE",
        side="BUY", quantity=2, price=100.0, strategy="new") == legacy
    assert journal.get("legacy-1")["state"] == UNKNOWN
    assert repository.get_engine_state(SubmissionJournal.KEY) == {"legacy-1": legacy}
    assert repository.get_engine_state(SubmissionJournal.LEGACY_MIGRATION_KEY) is True
    with database.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM submission_intents").fetchone()[0] == 1
    database.close()


def test_duplicate_concurrent_record_reserves_one_durable_intent(tmp_path):
    database, repository = _repository(tmp_path)
    journal = SubmissionJournal(repository)
    with ThreadPoolExecutor(max_workers=8) as pool:
        entries = list(pool.map(lambda _: _record(journal, "same-intent"), range(16)))
    assert all(entry["state"] == DURABLY_PERSISTED for entry in entries)
    assert journal.get("same-intent")["state"] == DURABLY_PERSISTED
    with database.transaction() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM submission_intents WHERE intent_id=?",
            ("same-intent",)).fetchone()[0] == 1
        event_count = db.execute(
            "SELECT COUNT(*) FROM system_events WHERE kind='INTENT_PERSISTED'"
        ).fetchone()[0]
    assert event_count == 1
    database.close()


def test_invalid_or_unknown_durable_state_fails_closed(tmp_path):
    database, repository = _repository(tmp_path)
    malformed = {
        "state": "MAYBE_SUBMITTED",
        "instrument_key": "NSE_EQ|INE002A01018",
        "symbol": "RELIANCE",
        "side": "BUY",
        "quantity": 1,
        "price": 100.0,
        "strategy": "regression",
        "params_version": "v1",
        "tag": "broken-intent",
        "upstox_order_ids": [],
        "created_ts": "now",
        "updated_ts": "now",
    }
    repository.insert_submission_intent("broken-intent", malformed)
    journal = SubmissionJournal(repository)
    with pytest.raises(ValueError, match="unknown state"):
        journal.get("broken-intent")
    with pytest.raises(ValueError, match="unknown state"):
        journal.unresolved()
    database.close()


def test_transition_graph_prevents_retry_authorizing_regression(tmp_path):
    database, repository = _repository(tmp_path)
    journal = SubmissionJournal(repository)
    _record(journal)
    journal.mark("intent-1", SUBMISSION_ATTEMPTED)
    journal.mark("intent-1", UNKNOWN)
    journal.mark("intent-1", REMOTE_LOOKUP)
    journal.mark("intent-1", UNRESOLVED)
    with pytest.raises(ValueError, match="invalid submission journal transition"):
        journal.mark("intent-1", DURABLY_PERSISTED)
    assert journal.get("intent-1")["state"] == UNRESOLVED
    assert journal.resubmission_forbidden("intent-1")

    # Even a resolved absence is terminal for this identifier; no transition
    # can turn it into a new submit-ready intent.
    journal.mark("intent-1", REMOTE_LOOKUP)
    journal.mark("intent-1", VERIFIED_ABSENCE)
    with pytest.raises(ValueError, match="invalid submission journal transition"):
        journal.mark("intent-1", SUBMISSION_ATTEMPTED)
    assert journal.get("intent-1")["state"] == VERIFIED_ABSENCE
    database.close()
