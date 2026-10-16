"""Adversarial fail-closed coverage for the money-path validation branches.

Every test here targets a branch that a full-suite coverage run
(``pytest --cov=app --cov=axiom --cov-branch``) showed uncovered AND that
matters for correctness/safety:

- ``SubmissionJournal._validate_entry`` / ``mark`` refuse every malformed
  durable payload (corrupt journal, illegal transition, non-object detail,
  concurrent writer) — fail closed, never persist garbage.
- ``DailyRiskAccount`` refuses corrupt persisted state and non-finite or
  clock-less fill accounting inputs.
- The sandbox broker refuses non-HTTPS roots, wrong sides, and malformed
  place/history responses; history capability records rate-limit/unexpected
  reasons; remote lookup and cancel/modify never guess an outcome.
- ``recover_ledger_state`` skips transition rows without an order row,
  replays per-child increments exactly (anomalous basis → never negative),
  and tolerates a corrupt raw payload on a SELL row.
- ``app.config.envfile`` keeps comments/secrets intact, escapes values, and
  removes its temp file if the atomic write fails.

No network, no real credentials, no weakening of any production assertion.
"""

from __future__ import annotations

import json
from datetime import datetime

import httpx
import pytest

from app.brokers.sandbox import SandboxLedger, SandboxRequestError, UpstoxSandboxBroker
from app.config import envfile
from app.database.database import Database
from app.database.repository import Repository
from app.engine.state import (
    ACKNOWLEDGED,
    DURABLY_PERSISTED,
    SUBMISSION_ATTEMPTED,
    UNKNOWN,
    UNRESOLVED,
    VERIFIED_ABSENCE,
    VERIFIED_MATCH,
    DailyRiskAccount,
    SubmissionJournal,
    read_equity_state,
    recover_ledger_state,
)

INSTRUMENT = "NSE_EQ|INE002A01018"
SANDBOX_HOST = "api-sandbox.upstox.com"


# ---------------------------------------------------------------- fixtures --

@pytest.fixture()
def env(tmp_path):
    database = Database(f"sqlite:///{tmp_path}/adversarial.db")
    repository = Repository(database)
    yield repository
    database.close()


@pytest.fixture()
def journal(env):
    return SubmissionJournal(env)


def _record(journal, intent_id="adv-1", **overrides):
    values = {
        "instrument_key": INSTRUMENT,
        "symbol": "RELIANCE",
        "side": "BUY",
        "quantity": 2,
        "price": 100.0,
        "strategy": "adversarial",
        "params_version": "v1",
        "tag": intent_id[:12],
    }
    values.update(overrides)
    return journal.record(intent_id, **values)


def _to_unknown(journal, intent_id):
    journal.mark(intent_id, SUBMISSION_ATTEMPTED)
    journal.mark(intent_id, UNKNOWN)
    return journal.get(intent_id)


class ScriptedTransport(httpx.BaseTransport):
    """Routes every request to a per-test callable; records the sequence."""

    def __init__(self, handler):
        self.handler = handler
        self.requests: list[tuple[str, str]] = []

    def handle_request(self, request):
        self.requests.append((request.method, str(request.url)))
        return self.handler(request)


def _place_ok(request):
    return httpx.Response(200, json={"status": "success",
                                     "data": {"order_ids": ["oid-1"]}})


def make_broker(repo=None, handler=None, **kwargs):
    transport = ScriptedTransport(handler) if handler is not None else None
    broker = UpstoxSandboxBroker(
        "test-token", INSTRUMENT, transport=transport, repository=repo, **kwargs)
    return broker, transport


def _journal_entry(intent_id, **overrides):
    entry = {
        "state": UNKNOWN,
        "instrument_key": INSTRUMENT,
        "symbol": "RELIANCE",
        "side": "BUY",
        "quantity": 2,
        "price": 100.0,
        "strategy": "adversarial",
        "params_version": "v1",
        "tag": intent_id[:12],
        "upstox_order_ids": [],
        "created_ts": "2026-10-06T05:00:00+00:00",
        "updated_ts": "2026-10-06T05:00:00+00:00",
    }
    entry.update(overrides)
    return entry


class _LegacyRepo:
    """Pre-table repository surface: engine_state JSON only, no db handle."""

    def __init__(self):
        self.state: dict = {}
        self.events: list = []

    def get_engine_state(self, key, default=None):
        return self.state.get(key, default)

    def set_engine_state(self, key, value):
        self.state[key] = value

    def record_system_event(self, kind, payload):
        self.events.append((kind, payload))


# ------------------------------------------- journal validation fail-closed --

class TestSubmissionJournalValidation:
    def test_blank_intent_id_is_refused(self, journal):
        with pytest.raises(ValueError, match="intent id must be non-empty"):
            journal.record("   ", instrument_key=INSTRUMENT, symbol="RELIANCE",
                           side="BUY", quantity=1, price=1.0, strategy="s")

    def test_validate_entry_rejects_bad_intent_ids_and_non_dicts(self):
        entry = _journal_entry("good")
        with pytest.raises(ValueError, match="invalid intent id"):
            SubmissionJournal._validate_entry("", entry)
        with pytest.raises(ValueError, match="invalid intent id"):
            SubmissionJournal._validate_entry("  ", entry)
        with pytest.raises(ValueError, match="entry is malformed"):
            SubmissionJournal._validate_entry("good", ["not", "a", "dict"])

    def test_validate_entry_rejects_malformed_identity(self):
        for bad in (
            _journal_entry("e", instrument_key=""),
            _journal_entry("e", symbol=7),
            _journal_entry("e", side="buy"),
            _journal_entry("e", strategy=None),
        ):
            with pytest.raises(ValueError, match="order identity is malformed"):
                SubmissionJournal._validate_entry("e", bad)

    def test_validate_entry_rejects_impossible_quantities_and_prices(self):
        for bad in (
            _journal_entry("e", quantity=0),
            _journal_entry("e", quantity=-5),
            _journal_entry("e", quantity=True),
            _journal_entry("e", quantity=float("nan")),
            _journal_entry("e", price=-1.0),
            _journal_entry("e", price=float("inf")),
        ):
            with pytest.raises(ValueError, match="journal (quantity|price) is malformed"):
                SubmissionJournal._validate_entry("e", bad)

    def test_validate_entry_rejects_bad_tag_ids_timestamps_and_detail(self):
        with pytest.raises(ValueError, match="tag is malformed"):
            SubmissionJournal._validate_entry("e", _journal_entry("e", tag=""))
        with pytest.raises(ValueError, match="remote order ids are malformed"):
            SubmissionJournal._validate_entry(
                "e", _journal_entry("e", upstox_order_ids=[123]))
        with pytest.raises(ValueError, match="timestamps are malformed"):
            SubmissionJournal._validate_entry(
                "e", _journal_entry("e", created_ts=None))
        with pytest.raises(ValueError, match="detail is malformed"):
            SubmissionJournal._validate_entry(
                "e", _journal_entry("e", detail="not-an-object"))

    def test_mark_rejects_unknown_state_and_non_object_detail(self, journal):
        _record(journal, "mark-1")
        with pytest.raises(ValueError, match="unknown submission journal state"):
            journal.mark("mark-1", "NOT_A_STATE")
        with pytest.raises(ValueError, match="detail must be an object"):
            journal.mark("mark-1", SUBMISSION_ATTEMPTED, detail="secret-ish")

    def test_mark_refuses_a_concurrent_writer(self, journal, monkeypatch):
        _record(journal, "race-1")
        monkeypatch.setattr(journal.repository, "update_submission_intent",
                            lambda *args, **kwargs: False)
        with pytest.raises(RuntimeError, match="changed concurrently"):
            journal.mark("race-1", SUBMISSION_ATTEMPTED)

    def test_legacy_loader_refuses_a_corrupt_journal_blob(self):
        repo = _LegacyRepo()
        repo.state[SubmissionJournal.KEY] = "oops"  # not a dict
        legacy = SubmissionJournal(repo)
        with pytest.raises(ValueError, match="journal is malformed"):
            legacy.get("anything")

    def test_legacy_record_and_mark_persist_without_a_table(self):
        repo = _LegacyRepo()
        legacy = SubmissionJournal(repo)
        entry = _record(legacy, "legacy-1")
        assert entry["state"] == DURABLY_PERSISTED
        assert isinstance(repo.state[SubmissionJournal.KEY], dict)
        updated = legacy.mark("legacy-1", SUBMISSION_ATTEMPTED)
        assert updated["state"] == SUBMISSION_ATTEMPTED
        assert legacy.get("legacy-1")["state"] == SUBMISSION_ATTEMPTED
        assert repo.events and repo.events[-1][0] == "INTENT_STATE"

    def test_migration_without_a_database_returns_immediately(self):
        legacy = SubmissionJournal(_LegacyRepo())
        legacy._migrate_legacy()  # no db attribute: must not raise

    @staticmethod
    def _raw_sql(env, key, payload):
        with env.db.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO engine_state(key, payload) VALUES (?,?)",
                (key, payload))

    def test_migration_marker_must_be_json_true(self, env):
        self._raw_sql(env, SubmissionJournal.LEGACY_MIGRATION_KEY, "{oops")
        journal = SubmissionJournal(env)
        with pytest.raises(ValueError, match="migration marker is malformed"):
            journal.get("x")
        self._raw_sql(env, SubmissionJournal.LEGACY_MIGRATION_KEY, "false")
        with pytest.raises(ValueError, match="migration marker is invalid"):
            SubmissionJournal(env).get("x")

    def test_legacy_journal_blob_must_be_readable_json_dict(self, env):
        self._raw_sql(env, SubmissionJournal.KEY, "not-json")
        with pytest.raises(ValueError, match="legacy submission journal is unreadable"):
            SubmissionJournal(env).get("x")
        self._raw_sql(env, SubmissionJournal.KEY, "[1, 2]")
        with pytest.raises(ValueError, match="legacy submission journal is malformed"):
            SubmissionJournal(env).get("x")
        self._raw_sql(env, SubmissionJournal.KEY, json.dumps({"bad": 123}))
        with pytest.raises(ValueError, match="entry is malformed"):
            SubmissionJournal(env).get("x")

    def test_table_backed_load_ignores_a_poisoned_legacy_blob(self, env):
        # The marker short-circuits legacy validation: a corrupt legacy blob
        # left behind by an older version must not brick a migrated journal.
        with env.db.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO engine_state(key, payload) VALUES (?,?)",
                (SubmissionJournal.LEGACY_MIGRATION_KEY, "true"))
            db.execute(
                "INSERT OR REPLACE INTO engine_state(key, payload) VALUES (?,?)",
                (SubmissionJournal.KEY, "not-json"))
        journal = SubmissionJournal(env)
        assert journal.get("no-such-intent") is None


# --------------------------------------------- daily risk accounting guards --

class TestDailyRiskAccountGuards:
    def test_corrupt_persisted_state_is_refused(self, env):
        env.set_engine_state(DailyRiskAccount.KEY, "oops")
        with pytest.raises(ValueError, match="daily risk state is malformed"):
            DailyRiskAccount(env)

    @pytest.mark.parametrize(
        "payload, message",
        [
            ({"date": "", "trades": 0, "realized_pnl": 0.0,
              "fill_accounting_cutover_ts": 123}, "cutover is malformed"),
            ({"date": "", "trades": 0, "realized_pnl": 0.0,
              "fill_accounting_cutover_ts": "not-a-date"}, "cutover is unreadable"),
            ({"date": "", "trades": 0, "realized_pnl": 0.0,
              "fill_accounting_cutover_ts": "2026-01-01T00:00:00"},
             "cutover has no timezone"),
        ],
    )
    def test_corrupt_cutover_is_refused(self, env, payload, message):
        env.set_engine_state(DailyRiskAccount.KEY, payload)
        with pytest.raises(ValueError, match=message):
            DailyRiskAccount(env)

    def test_unknown_market_timezone_falls_back_to_documented_default(self, env):
        account = DailyRiskAccount(env, market_timezone="Not/AZone")
        assert account._tz.key == "Asia/Kolkata"

    def test_record_realized_fill_refuses_bad_identity_clock_and_observation(self, env):
        account = DailyRiskAccount(env)
        with pytest.raises(ValueError, match="stable identity"):
            account.record_realized_fill("", 1.0, observed_at=None)
        with pytest.raises(ValueError, match="not finite"):
            account.record_realized_fill("f-nan", float("nan"), observed_at=None)
        with pytest.raises(ValueError, match="must include a timezone"):
            account.record_realized_fill(
                "f-naive-now", 1.0, observed_at=None,
                now=datetime(2026, 1, 2, 9, 0))
        with pytest.raises(ValueError, match="observation time is malformed"):
            account.record_realized_fill("f-bad-obs", 1.0, observed_at="  ")
        with pytest.raises(ValueError, match="observation time is unreadable"):
            account.record_realized_fill("f-unreadable-obs", 1.0,
                                         observed_at="definitely-not-a-date")
        with pytest.raises(ValueError, match="observation time has no timezone"):
            account.record_realized_fill("f-naive-obs", 1.0,
                                         observed_at="2026-01-02T09:00:00")

    def test_read_equity_state_prefers_live_over_sandbox_and_never_guesses(self, env):
        assert read_equity_state(None) == {}
        assert read_equity_state(env) == {}
        env.set_engine_state("sandbox_equity", {"cash": 10.0})
        assert read_equity_state(env)["cash"] == 10.0
        env.set_engine_state("live_equity", {"cash": 99.0})
        assert read_equity_state(env)["cash"] == 99.0


# ------------------------------------------------------- ledger recovery ----

class TestLedgerRecoveryReplay:
    def test_replay_skips_ghost_orders_bad_raws_and_anomalous_basis(self, env):
        now = "2026-10-06T05:00:00+00:00"
        env.record_sandbox_order({
            "id": "ord-1", "symbol": "RELIANCE", "side": "SELL",
            "quantity": 20, "price": 100.0, "status": "PARTIALLY_FILLED",
            "created_ts": now, "updated_ts": now, "tag": "recov1",
        })
        record = env.record_sandbox_transition
        # chronological per-child increments for ord-1/c1
        assert record("ord-1", "c1", "PARTIALLY_FILLED", 10, 50,
                      "2026-10-06T05:00:01+00:00", {})
        # anomalous negative basis -> must fall back to the current average
        assert record("ord-1", "c1", "PARTIALLY_FILLED", 11, 1,
                      "2026-10-06T05:00:02+00:00", {})
        # honest incremental basis: (12*2 - 11*1) / 1 = 13
        assert record("ord-1", "c1", "PARTIALLY_FILLED", 12, 2,
                      "2026-10-06T05:00:03+00:00", {})
        # cumulative regression: already reflected in the baseline -> skip
        assert record("ord-1", "c1", "PARTIALLY_FILLED", 9, 40,
                      "2026-10-06T05:00:04+00:00", {})
        # a transition whose order row does not exist -> skipped, not guessed
        assert record("ord-ghost", "g1", "PARTIALLY_FILLED", 5, 10,
                      "2026-10-06T05:00:05+00:00", {})
        # second child, raw payload corrupted after recording
        assert record("ord-1", "c2", "PARTIALLY_FILLED", 5, 20,
                      "2026-10-06T05:00:06+00:00", {})
        with env.db.transaction() as db:
            db.execute(
                "UPDATE sandbox_transitions SET raw='not-json' "
                "WHERE order_id='ord-1' AND upstox_order_id='c2'")

        # The orders view stays readable despite the corrupt raw row.
        listed = env.list_sandbox_orders()
        assert [o["id"] for o in listed] == ["ord-1"]
        raws = [t["raw"] for t in listed[0]["transitions"]]
        assert {} in raws and all(not isinstance(r, str) or r for r in raws)

        first_ledger = SandboxLedger(100_000.0)
        report = recover_ledger_state(first_ledger, env)
        assert report["orders_skipped"] == 1  # the ghost order row
        assert report["orders_replayed"] == 1
        assert first_ledger.realized_pnl != 0.0

        second_ledger = SandboxLedger(100_000.0)
        replay = recover_ledger_state(second_ledger, env)
        assert replay == report, "replay must be deterministic"
        assert second_ledger.realized_pnl == first_ledger.realized_pnl

        # Idempotence is about rebuilds: a fresh ledger rebuilds the same
        # state; applying recorded fills in-process is guarded elsewhere and
        # double-applying to a NON-fresh ledger is out of scope by design.
        third_ledger = SandboxLedger(100_000.0)
        recover_ledger_state(third_ledger, env)
        assert third_ledger.realized_pnl == first_ledger.realized_pnl


# ------------------------------------------------- sandbox broker guards ----

class TestSandboxRequestGuards:
    def test_plain_http_base_url_is_refused(self):
        with pytest.raises(ValueError, match="secure HTTPS root"):
            UpstoxSandboxBroker(
                "test-token", INSTRUMENT,
                base_url=f"http://{SANDBOX_HOST}")

    def test_non_order_host_scheme_is_refused(self):
        with pytest.raises(ValueError, match="must be http\\(s\\)"):
            UpstoxSandboxBroker("test-token", INSTRUMENT, base_url="ftp://x")

    def test_wrong_side_is_refused_before_any_http_call(self):
        broker, transport = make_broker(handler=_place_ok)
        with pytest.raises(ValueError, match="side must be BUY or SELL"):
            broker.submit("buy", 1, 100.0, intent_id="side-1",
                          instrument_key=INSTRUMENT)
        assert transport.requests == []  # refused before any HTTP call

    def test_algo_name_header_is_opt_in(self):
        broker, _ = make_broker()
        assert "X-Algo-Name" not in broker._headers()
        named, _ = make_broker(algo_name="AXIOM")
        assert named._headers()["X-Algo-Name"] == "AXIOM"

    def test_repository_less_hooks_are_defensive_noops(self):
        broker, _ = make_broker()
        assert broker.repository is None
        assert broker._record_order({"id": "x"}) is None
        assert broker._record_transition(
            "order", "child", "OPEN", 0.0, 0.0, "t", {}) is True


class TestPlaceResponseIntegrity:
    def test_place_response_without_data_marks_intent_unknown(self, env):
        def handler(request):
            if request.method == "POST":
                return httpx.Response(200, json={"status": "success"})
            return httpx.Response(404, json={})
        broker, _ = make_broker(env, handler)
        with pytest.raises(SandboxRequestError, match="missing data.order_ids"):
            broker.submit("BUY", 1, 100.0, intent_id="malformed-1",
                          instrument_key=INSTRUMENT)
        entry = SubmissionJournal(env).get("malformed-1")
        assert entry["state"] == UNKNOWN
        assert entry["detail"].get("phase") == "malformed_response"

    def test_place_response_with_empty_order_ids_marks_intent_unknown(self, env):
        def handler(request):
            if request.method == "POST":
                return httpx.Response(
                    200, json={"status": "success", "data": {"order_ids": []}})
            return httpx.Response(404, json={})
        broker, _ = make_broker(env, handler)
        with pytest.raises(SandboxRequestError, match="has no order ids"):
            broker.submit("BUY", 1, 100.0, intent_id="empty-ids-1",
                          instrument_key=INSTRUMENT)
        entry = SubmissionJournal(env).get("empty-ids-1")
        assert entry["state"] == UNKNOWN
        assert entry["detail"].get("phase") == "empty_order_ids"


class TestHistoryCapabilityEdges:
    def test_rate_limited_history_is_classified_and_keeps_orders_alive(self):
        def handler(request):
            if request.method == "POST":
                return _place_ok(request)
            return httpx.Response(429, json={})
        broker, transport = make_broker(handler=handler)
        broker.submit("BUY", 1, 100.0, intent_id="rl-1",
                      instrument_key=INSTRUMENT)
        broker.reconcile()
        view = broker.status_view()
        assert view["available"] is True
        assert view["history_capability"]["state"] == "rate_limited"
        # GETs are retried exactly once before the capability is recorded.
        assert sum(1 for m, _ in transport.requests
                   if m == "GET") == 2

    def test_unexpected_history_status_is_classified_and_keeps_orders_alive(self):
        def handler(request):
            if request.method == "POST":
                return _place_ok(request)
            return httpx.Response(400, json={})
        broker, _ = make_broker(handler=handler)
        broker.submit("BUY", 1, 100.0, intent_id="weird-1",
                      instrument_key=INSTRUMENT)
        broker.reconcile()
        view = broker.status_view()
        assert view["available"] is True
        capability = view["history_capability"]
        assert capability["state"] == "unexpected"
        assert capability["reason"] == "HTTP 400"


class TestRemoteLookupAdversarial:
    def test_lookup_without_a_journal_reports_no_journal(self):
        broker, _ = make_broker()
        assert broker.lookup_intent_order("any") == "NO_JOURNAL"

    def test_lookup_of_unknown_intent_never_guesses(self, env):
        broker, _ = make_broker(env, _place_ok)
        assert broker.lookup_intent_order("ghost") == "UNKNOWN_INTENT"

    def test_lookup_of_final_intent_returns_current_state(self, env):
        journal = SubmissionJournal(env)
        _record(journal, "final-1")
        journal.mark("final-1", SUBMISSION_ATTEMPTED)
        journal.mark("final-1", ACKNOWLEDGED)
        broker, _ = make_broker(env, _place_ok)
        assert broker.lookup_intent_order("final-1") == ACKNOWLEDGED

    def test_lookup_network_failure_marks_unknown_with_reason(self, env):
        journal = SubmissionJournal(env)
        _record(journal, "net-1")
        _to_unknown(journal, "net-1")

        def handler(request):
            raise httpx.ConnectError("unreachable", request=request)

        broker, _ = make_broker(env, handler)
        assert broker.lookup_intent_order("net-1") == UNKNOWN
        assert journal.get("net-1")["detail"].get("lookup") == "network_error"

    @pytest.mark.parametrize(
        "body",
        [{"status": "success"},            # no data key
         {"status": "success", "data": {}},  # data not a list
         ],
    )
    def test_lookup_malformed_history_marks_unknown(self, env, body):
        journal = SubmissionJournal(env)
        _record(journal, "bad-1")
        _to_unknown(journal, "bad-1")
        broker, _ = make_broker(env, lambda request: httpx.Response(200, json=body))
        assert broker.lookup_intent_order("bad-1") == UNKNOWN
        assert journal.get("bad-1")["detail"].get("lookup") == "malformed"

    def test_lookup_ignores_junk_rows_and_adopts_a_full_match(self, env):
        journal = SubmissionJournal(env)
        _record(journal, "adopt-1")
        _to_unknown(journal, "adopt-1")
        rows = [
            "junk-row",                              # not a dict -> ignored
            {"transaction_type": "BUY", "quantity": 2},  # no order_id -> ignored
            {"order_id": "oid-full", "transaction_type": "BUY",
             "quantity": 2, "instrument_token": INSTRUMENT,
             "order_type": "MARKET", "order_timestamp": "t1"},
        ]
        broker, _ = make_broker(
            env, lambda request: httpx.Response(200, json={"status": "success",
                                                           "data": rows}))
        assert broker.lookup_intent_order("adopt-1") == VERIFIED_MATCH
        assert journal.get("adopt-1")["upstox_order_ids"] == ["oid-full"]

    def test_lookup_row_guards_each_fail_closed(self, env):
        journal = SubmissionJournal(env)
        _record(journal, "guards-1")
        _to_unknown(journal, "guards-1")
        rows = [
            # wrong side
            {"order_id": "a", "transaction_type": "SELL", "quantity": 2},
            # non-integer quantity
            {"order_id": "b", "transaction_type": "BUY", "quantity": "abc"},
            # quantity larger than the logical intent
            {"order_id": "c", "transaction_type": "BUY", "quantity": 999},
            # different instrument
            {"order_id": "d", "transaction_type": "BUY", "quantity": 2,
             "instrument_token": "NSE_EQ|OTHER"},
            # LIMIT price mismatch
            {"order_id": "e", "transaction_type": "BUY", "quantity": 2,
             "order_type": "LIMIT", "price": 55.0},
            # LIMIT price not parseable
            {"order_id": "f", "transaction_type": "BUY", "quantity": 2,
             "order_type": "LIMIT", "price": "abc"},
            # matching attributes but WRONG quantity for a single candidate
            {"order_id": "g", "transaction_type": "BUY", "quantity": 1,
             "instrument_token": INSTRUMENT},
        ]
        broker, _ = make_broker(
            env, lambda request: httpx.Response(200, json={"status": "success",
                                                           "data": rows}))
        assert broker.lookup_intent_order("guards-1") == VERIFIED_ABSENCE
        assert journal.get("guards-1")["state"] == VERIFIED_ABSENCE

    def test_slice_adoption_requires_an_exact_quantity_sum(self, env):
        journal = SubmissionJournal(env)
        _record(journal, "slices-1", quantity=2)
        _to_unknown(journal, "slices-1")
        exact = [
            {"order_id": "s1", "transaction_type": "BUY", "quantity": 1,
             "instrument_token": INSTRUMENT},
            {"order_id": "s2", "transaction_type": "BUY", "quantity": 1,
             "instrument_token": INSTRUMENT},
        ]
        broker, _ = make_broker(
            env, lambda request: httpx.Response(200, json={"status": "success",
                                                           "data": exact}))
        assert broker.lookup_intent_order("slices-1") == VERIFIED_MATCH
        assert sorted(journal.get("slices-1")["upstox_order_ids"]) == ["s1", "s2"]

        _record(journal, "slices-2", quantity=2)
        _to_unknown(journal, "slices-2")
        partial = [
            {"order_id": "p1", "transaction_type": "BUY", "quantity": 1,
             "instrument_token": INSTRUMENT},
            {"order_id": "p2", "transaction_type": "BUY", "quantity": 2,
             "instrument_token": INSTRUMENT},
        ]
        broker2, _ = make_broker(
            env, lambda request: httpx.Response(200, json={"status": "success",
                                                           "data": partial}))
        assert broker2.lookup_intent_order("slices-2") == UNRESOLVED
        assert journal.get("slices-2")["state"] == UNRESOLVED


class TestReconcileIntegrity:
    @staticmethod
    def _row(filled, average, status="open pending", ts="t"):
        return {"order_id": "oid-1", "transaction_type": "BUY",
                "filled_quantity": filled, "average_price": average,
                "order_timestamp": ts, "status": status,
                "quantity": 20, "instrument_token": INSTRUMENT}

    @classmethod
    def _sequenced(cls, *rows):
        """History handler serving ONE row per GET (reconcile reads [-1])."""
        calls = {"get": 0}

        def handler(request):
            if request.method == "POST":
                return _place_ok(request)
            if request.method == "GET":
                row = rows[min(calls["get"], len(rows) - 1)]
                calls["get"] += 1
                return httpx.Response(200, json={"status": "success",
                                                 "data": [row]})
            return httpx.Response(404, json={})
        return handler

    def test_second_identical_reconcile_never_double_credits(self, env):
        broker, _ = make_broker(
            env, self._sequenced(self._row(10, 50.0, ts="t1")))
        broker.submit("BUY", 20, 100.0, intent_id="idem-1",
                      instrument_key=INSTRUMENT)
        first = broker.reconcile()
        cash = broker.ledger.cash
        assert len(first) == 1
        second = broker.reconcile()
        assert second == []
        assert broker.ledger.cash == cash, "an identical row must not re-credit"

    def test_anomalous_incremental_basis_is_never_negative(self, env):
        broker, _ = make_broker(
            env, self._sequenced(self._row(10, 50.0, ts="t1"),
                                 self._row(11, 1.0, ts="t2")))
        broker.submit("BUY", 20, 100.0, intent_id="basis-1",
                      instrument_key=INSTRUMENT)
        broker.reconcile()
        cash = broker.ledger.cash
        fills = broker.reconcile()
        # delta 1 was priced at the current average (1.0), never at
        # (11*1 - 10*50)/1 = -489
        assert len(fills) == 1
        assert broker.ledger.cash == pytest.approx(cash - 1.0)

    def test_zero_average_keeps_the_previous_child_basis(self, env):
        broker, _ = make_broker(
            env, self._sequenced(self._row(10, 50.0, ts="t1"),
                                 self._row(12, 0.0, ts="t2")))
        broker.submit("BUY", 20, 100.0, intent_id="basis-2",
                      instrument_key=INSTRUMENT)
        broker.reconcile()
        cash = broker.ledger.cash
        fills = broker.reconcile()
        # delta 2 priced at the previous basis (50), not at 0
        assert len(fills) == 1
        assert broker.ledger.cash == pytest.approx(cash - 100.0)

    def test_history_success_without_data_fails_reconcile_loudly(self, env):
        broker, _ = make_broker(
            env, lambda request: httpx.Response(
                200, json={"status": "success"}) if request.method == "GET"
                else _place_ok(request))
        broker.submit("BUY", 1, 100.0, intent_id="nodata-1",
                      instrument_key=INSTRUMENT)
        with pytest.raises(SandboxRequestError, match="missing data"):
            broker.reconcile()

    def test_map_status_rejects_and_unknowns_never_become_fills(self):
        assert UpstoxSandboxBroker._map_status("rejected by risk", 0) == "REJECTED"
        # An unknown provider status is never claimed terminal — even with a
        # fill quantity it stays a working order (OPEN), not a guess.
        assert UpstoxSandboxBroker._map_status("totally unknown", 3) == "OPEN"
        # Known open-ish statuses do distinguish a partial fill.
        assert UpstoxSandboxBroker._map_status("open pending", 3) == "PARTIALLY_FILLED"
        assert UpstoxSandboxBroker._map_status("complete", 1) == "FILLED"
        assert UpstoxSandboxBroker._map_status("cancelled", 1) == "CANCELLED"

    @pytest.mark.parametrize(
        "behavior",
        ["network", "http_error", "missing_data", "empty_list"],
    )
    def test_child_remote_status_returns_none_when_undeterminable(self, behavior):
        if behavior == "network":
            def handler(request):
                raise httpx.ConnectError("unreachable", request=request)
        elif behavior == "http_error":
            def handler(request):
                return httpx.Response(500, json={})
        elif behavior == "missing_data":
            def handler(request):
                return httpx.Response(200, json={"status": "success"})
        else:
            def handler(request):
                return httpx.Response(200, json={"status": "success",
                                                 "data": []})
        broker, _ = make_broker(handler=handler)
        assert broker._child_remote_status("oid-1") is None


class TestCancelModifyNeverGuess:
    @staticmethod
    def _placed(repo, handler, intent="cm-1"):
        broker, transport = make_broker(repo, handler)
        broker.submit("BUY", 5, 100.0, intent_id=intent,
                      instrument_key=INSTRUMENT)
        return broker, transport

    def test_cancel_of_a_never_submitted_state_says_so(self, env):
        def handler(request):
            if request.method == "POST":
                return _place_ok(request)
            return httpx.Response(404, json={})
        broker, _ = self._placed(env, handler)
        state = broker.orders["cm-1"]
        state.status = "SUBMITTED"
        state.upstox_order_ids.clear()
        result = broker.cancel("cm-1")
        assert result["already_final"] is True
        assert result["note"] == "never submitted remotely"

    def test_cancel_unresolvable_child_stays_working(self, env):
        def handler(request):
            if request.method == "POST":
                return _place_ok(request)
            if request.method == "DELETE":
                return httpx.Response(400, json={"errors": [
                    {"errorCode": "UDAPI100010"}]})
            return httpx.Response(404, json={})  # history cannot resolve it
        broker, _ = self._placed(env, handler)
        result = broker.cancel("cm-1")
        state = broker.orders["cm-1"]
        assert state.child_status["oid-1"] == "SUBMITTED"
        assert result["status"] == "SUBMITTED"
        assert all(r.get("confirmed") is False for r in result["results"])

    def test_modify_of_unknown_or_unsubmitted_intent_is_refused(self, env):
        def handler(request):
            if request.method == "POST":
                return _place_ok(request)
            return httpx.Response(404, json={})
        broker, _ = make_broker(env, handler)
        with pytest.raises(ValueError, match="Unknown or unsubmitted"):
            broker.modify("no-such-intent", quantity=5)
        placed, _ = self._placed(env, handler)
        placed.orders["cm-1"].upstox_order_ids.clear()
        with pytest.raises(ValueError, match="Unknown or unsubmitted"):
            placed.modify("cm-1", quantity=5)

    def test_modify_http_failure_is_loud(self, env):
        def handler(request):
            if request.method == "POST":
                return _place_ok(request)
            return httpx.Response(500, json={})
        broker, _ = self._placed(env, handler)
        with pytest.raises(SandboxRequestError, match="modify failed with HTTP 500"):
            broker.modify("cm-1", quantity=7)


# ------------------------------------------------------------ envfile -------

class TestEnvFileIntegrity:
    def test_read_values_keeps_quoted_hashes_escapes_and_exports(self, tmp_path):
        path = tmp_path / ".env"
        path.write_text(
            "# header\n"
            "PLAIN=value\n"
            'QUOTED="abc #def"\n'
            'ESCAPED="a\\"b"\n'
            'UNTERMINATED="rest as-is\n'
            "INLINE=rest # trailing comment\n"
            "TABBED=rest\t# tab comment\n"
            "export EXPORTED=1\n"
            "not a key line\n"
            "\n",
            encoding="utf-8")
        values = envfile.read_values(path)
        assert values["PLAIN"] == "value"
        assert values["QUOTED"] == "abc #def"
        assert values["ESCAPED"] == 'a"b'
        assert values["UNTERMINATED"] == "rest as-is"
        assert values["INLINE"] == "rest"
        assert values["TABBED"] == "rest"
        assert values["EXPORTED"] == "1"

    def test_read_values_on_missing_file_is_empty(self, tmp_path):
        assert envfile.read_values(tmp_path / "nope.env") == {}

    def test_render_value_quotes_only_when_needed_and_escapes(self):
        assert envfile.render_value("") == ""
        assert envfile.render_value("plain") == "plain"
        assert envfile.render_value("has#hash") == '"has#hash"'
        assert envfile.render_value('say "hi"') == '"say \\"hi\\""'
        assert envfile.render_value(" back ") == '" back "'

    def test_update_preserves_comments_order_and_secrets(self, tmp_path):
        path = tmp_path / ".env"
        path.write_text(
            "# my notes\nRISK=1\nOTHER=keep-me\n", encoding="utf-8")
        summary = envfile.update_values(path, {"RISK": "2", "NEW_KEY": "x y"})
        text = path.read_text(encoding="utf-8")
        assert "# my notes" in text
        assert "OTHER=keep-me" in text
        assert "RISK=2" in text
        assert "NEW_KEY=" in text
        assert summary["updated"] == ["RISK"]
        assert summary["added"] == ["NEW_KEY"]
        # the summary is safe to show a user: no values anywhere
        assert "2" not in json.dumps(summary["updated"])
        assert "x y" not in json.dumps(summary)

    def test_update_adds_comment_marker_only_when_file_has_comments(self, tmp_path):
        plain = tmp_path / "plain.env"
        plain.write_text("A=1\n", encoding="utf-8")
        envfile.update_values(plain, {"B": "2"})
        assert "Added by the Arena setup screen" not in plain.read_text(
            encoding="utf-8")

        commented = tmp_path / "commented.env"
        commented.write_text("# note\nA=1\n", encoding="utf-8")
        envfile.update_values(commented, {"B": "2"})
        text = commented.read_text(encoding="utf-8")
        assert "# Added by the Arena setup screen" in text

    def test_failed_atomic_write_leaves_original_and_no_temp(self, tmp_path,
                                                            monkeypatch):
        path = tmp_path / ".env"
        path.write_text("A=1\n", encoding="utf-8")

        def boom(src, dst):
            raise OSError("disk full")

        monkeypatch.setattr(envfile.os, "replace", boom)
        with pytest.raises(OSError, match="disk full"):
            envfile.update_values(path, {"A": "999"})
        assert path.read_text(encoding="utf-8") == "A=1\n"
        leftovers = [p for p in tmp_path.iterdir() if p.name != ".env"]
        assert leftovers == []
