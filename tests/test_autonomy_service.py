from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import threading
import time

import pytest

from app.autonomy.config import AutonomySettings
from app.autonomy.models import AccountProfile, Candidate, Instrument, StrategyEvidence
from app.autonomy.paper import PaperExecution
from app.autonomy.service import AutonomyService, NotReady, SessionError
from app.autonomy.store import AutonomyStore
from app.autonomy.strategy import EvidenceRegistry
from app.autonomy.upstox import (
    AmbiguousSubmission, InstrumentCatalog, Quote, UpstoxAuthError, UpstoxRejected,
)
from app.data.models import Candle

UTC = timezone.utc
NOW = datetime(2026, 10, 25, 10, 0, tzinfo=UTC)
KEY = "NSE_EQ|INE000000000"


def make_instrument():
    return Instrument(KEY, "NSE_EQ-INE000000000", "NSE_EQ", "NSE", "TEST", "EQ",
                      0.05, 1, 100000, 1.0, short_route_metadata=True)


def make_bars():
    item = make_instrument()
    result = []
    for n in range(80):
        close = round((100 + n * 0.3 + math.sin(n * 0.8) * 2) / 0.05) * 0.05
        open_price = round((close - 0.1) / 0.05) * 0.05
        high = round((max(open_price, close) + 0.3) / 0.05) * 0.05
        low = round((min(open_price, close) - 0.3 - (1 if n % 5 == 0 else 0)) / 0.05) * 0.05
        result.append(Candle(NOW - timedelta(minutes=80 - n), open_price, high, low,
                             close, 100000, item.display_symbol))
    return result


class Feed:
    def __init__(self, bars):
        self.bars = bars

    def completed(self, instrument, timeframe):
        return list(self.bars)


class Reader:
    def __init__(self, feed, clock):
        self.feed = feed
        self.clock = clock

    def profile(self, **kwargs):
        return AccountProfile(True, frozenset({"NSE"}), frozenset({"I"}),
                              frozenset({"LIMIT", "SL"}), "fingerprint", frozenset({"1.2.3.4"}))

    def quote(self, key):
        close = self.feed.bars[-1].close
        return Quote(key, close - 0.05, close + 0.05, close, 100000, self.clock())

    def market_timings(self, day):
        return {"NSE": (self.clock() - timedelta(hours=2), self.clock() + timedelta(hours=2))}


class FakeLive(PaperExecution):
    authoritative = True
    broker_mutations_reachable = True

    def account(self):
        return replace(super().account(), authoritative=True)


class FakeRecovery:
    authoritative = True
    broker_mutations_reachable = False

    def __init__(self, delegate):
        self.delegate = delegate

    @property
    def gtt(self):  # read-only test inspection; no mutation methods are exposed
        return self.delegate.gtt

    def account(self):
        return self.delegate.account()

    def get_gtt(self, identifier):
        return self.delegate.get_gtt(identifier)

    def orders(self):
        return self.delegate.orders()

    def order_history(self, order_id):
        return []

    def find_matching_gtt(self, order):
        matches = []
        for identifier, record in self.delegate.gtt.items():
            if record.get("instrument_token") != order.instrument_key \
                    or record.get("quantity") != order.quantity \
                    or record.get("product") != order.product:
                continue
            rules = {rule.get("strategy"): rule for rule in record.get("rules") or []}
            if set(rules) == {"ENTRY", "STOPLOSS", "TARGET"} and all(
                    math.isclose(float(rules[name]["trigger_price"]), float(price),
                                 rel_tol=0, abs_tol=1e-9)
                    for name, price in (("ENTRY", order.limit_price),
                                        ("STOPLOSS", order.stop_price),
                                        ("TARGET", order.target_price))):
                matches.append(identifier)
        return tuple(matches)


def evidence():
    return StrategyEvidence(
        evidence_id="paper-evidence", instrument_key=KEY, strategy_family="TREND_MOMENTUM",
        side="BUY", timeframe="1m", regime="ALL", generated_at=NOW, data_end=NOW,
        data_sha256="a" * 64, config_sha256="b" * 64, walk_forward_runs=2,
        holdout_runs=1, trades=25, net_expectancy=0.2, max_drawdown=0.1,
        risk_adjusted_score=1.0, baseline_risk_adjusted_score=0.0,
        costs_modelled=True, gaps_modelled=True, partial_fills_modelled=True,
        rejections_modelled=True, survivorship_aware=True, upstox_authoritative=True,
        holdout_locked=True, synthetic=True,
    )


def make_service(tmp_path, live=False):
    clock = [NOW]
    bars = make_bars()
    feed = Feed(bars)
    reader = Reader(feed, lambda: clock[0])
    item = make_instrument()
    settings = AutonomySettings(
        live_enabled=live, worker_enabled=False,
        expected_public_ip="1.2.3.4" if live else "",
        upstox_algo_name="AXIOM-TEST" if live else "",
    )
    catalog = InstrumentCatalog(settings, tmp_path / "catalog.json", clock=lambda: clock[0])
    catalog.instruments = {KEY: item}
    catalog.refreshed_at = clock[0]
    store = AutonomyStore(tmp_path / "journal.sqlite3")
    executor = FakeLive({KEY: item}, starting_equity=1000000, commission_rate=0.0005,
                        slippage_rate=0.0005, spread_rate=0.0002, max_participation=0.1)
    executor._now = clock[0]  # deterministic adapter clock for permit tests
    service = AutonomyService(
        settings, store, catalog, data_dir=tmp_path,
        reader_factory=lambda provider: reader,
        live_factory=lambda provider: executor,
        recovery_factory=lambda provider: FakeRecovery(executor),
        feed_factory=lambda reader: feed,
        token_provider=lambda: "test-only-not-a-credential",
        public_ip_provider=lambda: "1.2.3.4",
        evidence=EvidenceRegistry([
            replace(evidence(), synthetic=False, provenance_verified=live)
        ]),
        clock=lambda: clock[0],
    )
    session = service.create_session("UPSTOX_LIVE" if live else "PAPER", [KEY])
    session_id = session["session_id"]
    service.authorize(session_id)
    service.set_risk(session_id, max_daily_loss=10000, per_trade_risk_fraction=0.005,
                     equity_at_entry=None if live else 1000000)
    return service, store, feed, clock, session_id


def activate(service, session_id):
    service.arm(session_id)
    return service.activate(session_id)


def test_daily_gates_are_separate_and_paper_order_lifecycle_is_reconciled(tmp_path):
    service, store, feed, clock, session_id = make_service(tmp_path)
    runtime = service._runtime(session_id)
    with pytest.raises(PermissionError):
        service._permit(runtime, "ENTRY")
    assert activate(service, session_id)["state"] == "ACTIVE"
    first = service.execute_once(session_id)
    assert first["working_instruments"] == [KEY]
    assert len(store.intents(session_id)) == 1
    assert store.intents(session_id)[0]["state"] == "WORKING"
    # Repeating the same completed bar cannot duplicate the intent.
    service.execute_once(session_id)
    assert len(store.intents(session_id)) == 1

    previous = feed.bars[-1]
    feed.bars.append(Candle(NOW, previous.close - 1.0, previous.close + 0.5,
                            previous.close - 1.2, previous.close - 0.5, 100000,
                            previous.symbol))
    clock[0] = NOW + timedelta(minutes=1)
    second = service.execute_once(session_id)
    assert second["account"]["positions"], second
    assert second["account"]["positions"][0]["protection_id"]
    assert store.intents(session_id)[0]["state"] == "PROTECTED"
    confirmed_stop = second["account"]["positions"][0]["stop_price"]

    # A later gap exits at the worse open; the service reconciles CLOSED rather
    # than assuming the GTT is still working or the account is flat by guess.
    feed.bars.append(Candle(NOW + timedelta(minutes=1), 80.0, 82.0, 79.0, 81.0,
                            100000, previous.symbol))
    clock[0] = NOW + timedelta(minutes=2)
    third = service.execute_once(session_id)
    assert third["account"]["positions"] == []
    assert store.intents(session_id)[0]["state"] == "CLOSED"
    assert confirmed_stop > 0
    assert store.verify_chain()
    service.close()
    store.close()


def test_daily_loss_cap_blocks_entries_but_not_session_or_protection(tmp_path):
    service, store, feed, clock, session_id = make_service(tmp_path)
    activate(service, session_id)
    runtime = service._runtime(session_id)
    runtime.executor.day_realised = -10000
    result = service.scan_once(session_id)
    assert result["state"] == "ACTIVE"
    assert result["entries_blocked"] is True
    assert store.intents(session_id) == []
    events = store.events(session_id=session_id)
    assert any(event["event_type"] == "DAILY_LOSS_CAP" for event in events)
    assert any(event["payload"].get("forced_close") is False for event in events
               if event["event_type"] == "DAILY_LOSS_CAP")
    service.close()
    store.close()


def test_stale_data_halts_entries_and_requires_new_approval(tmp_path):
    service, store, feed, clock, session_id = make_service(tmp_path)
    activate(service, session_id)
    clock[0] = NOW + timedelta(minutes=10)
    result = service.scan_once(session_id)
    assert result["state"] == "HALTED"
    assert result["fault"] == "STALE_DATA"
    assert result["fresh_approval_required"] is True
    assert store.intents(session_id) == []
    with pytest.raises(PermissionError):
        service._permit(service._runtime(session_id), "ENTRY")
    service.close()
    store.close()


def test_pause_and_emergency_cancel_unfilled_and_revoke_entry_permits(tmp_path):
    service, store, feed, clock, session_id = make_service(tmp_path)
    activate(service, session_id)
    service.execute_once(session_id)
    identifier = store.intents(session_id)[0]["broker_ids_json"]
    paused = service.pause(session_id)
    assert paused["state"] == "PAUSED"
    with pytest.raises(PermissionError):
        service._permit(service._runtime(session_id), "PROTECTIVE")
    stopped = service.emergency_stop(session_id)
    assert stopped["state"] == "EMERGENCY_STOPPED"
    assert stopped["emergency"]["cleanup_status"] == "CONFIRMED"
    assert stopped["emergency"]["account_clear"] is False
    assert service._runtime(session_id).executor.gtt
    assert "PAPER-GTT" in identifier
    gtt = next(iter(service._runtime(session_id).executor.gtt.values()))
    assert all(rule["status"] == "CANCELLED" for rule in gtt["rules"])
    assert any(event["payload"].get("state") == "CANCEL_ATTEMPTED"
               for event in store.events(session_id=session_id))
    service.close()
    store.close()


def test_live_requires_explicit_environment_and_synthetic_evidence_cannot_enable_it(tmp_path):
    with pytest.raises(Exception, match="disabled"):
        service, store, feed, clock, session_id = make_service(tmp_path, live=False)
        service.create_session("UPSTOX_LIVE", [KEY])
        service.close()
        store.close()
    item = evidence()
    eligible, reason = __import__("app.autonomy.strategy", fromlist=["EvidenceRegistry"]).EvidenceRegistry([item]).match(
        __import__("app.autonomy.models", fromlist=["Candidate"]).Candidate(
            instrument_key=KEY, signal_id="x", bar_timestamp=NOW, family="TREND_MOMENTUM",
            side="BUY", entry_price=100, stop_price=95, target_price=110,
            regime="RANGE", score=1, explanation="x", evidence_id=item.evidence_id,
        ), "1m", live=True,
    )
    assert not eligible and "evidence" in reason.lower()


def test_live_activation_uses_account_and_static_ip_preflight_without_real_network(tmp_path):
    service, store, feed, clock, session_id = make_service(tmp_path, live=True)
    result = activate(service, session_id)
    assert result["mode"] == "UPSTOX_LIVE"
    assert result["state"] == "ACTIVE"
    assert result["account"]["authoritative"] is True
    assert service._runtime(session_id).executor.broker_mutations_reachable is True
    # No startup/background operation activated a second session.
    assert len(service.runtimes) == 1
    service.close()
    store.close()


def _missing_sl_profile(**kwargs):
    return AccountProfile(True, frozenset({"NSE"}), frozenset({"I"}),
                          frozenset({"LIMIT"}), "fingerprint", frozenset({"1.2.3.4"}))


def test_missing_order_types_block_readiness_and_arm(tmp_path):
    service, store, feed, clock, session_id = make_service(tmp_path, live=True)
    service._runtime(session_id).reader.profile = _missing_sl_profile
    service.authorize(session_id)
    readiness = service.readiness(session_id)
    assert readiness["ready"] is False
    assert any("order-type" in blocker and "SL" in blocker for blocker in readiness["blockers"])
    with pytest.raises(NotReady, match="order-type"):
        service.arm(session_id)
    assert service._runtime(session_id).executor.gtt == {}
    service.close()
    store.close()


def test_order_type_revocation_is_rechecked_by_activation_preflight(tmp_path):
    service, store, feed, clock, session_id = make_service(tmp_path, live=True)
    service.arm(session_id)
    service._runtime(session_id).reader.profile = _missing_sl_profile
    with pytest.raises(NotReady, match="order-type"):
        service.activate(session_id)
    assert service.public(session_id)["state"] == "ARMED"
    assert service._runtime(session_id).executor.gtt == {}
    service.close()
    store.close()


def test_order_type_revocation_halts_before_quote_margin_or_submission(tmp_path, monkeypatch):
    service, store, feed, clock, session_id = make_service(tmp_path, live=True)
    activate(service, session_id)
    runtime = service._runtime(session_id)
    runtime.reader.profile = _missing_sl_profile
    forbidden = []
    for name in ("margin_for", "place_protected_entry", "modify_stop", "cancel_gtt"):
        monkeypatch.setattr(runtime.executor, name,
                            lambda *args, _name=name, **kwargs: forbidden.append(_name))
    monkeypatch.setattr(runtime.reader, "quote",
                        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("quote must not be read")))
    result = service.execute_once(session_id)
    assert result["state"] == "HALTED"
    assert result["fault"] == "PERMISSION_REVOKED"
    assert forbidden == []
    assert store.intents(session_id) == []
    service.close()
    store.close()


def test_reconciliation_latches_loss_cap_without_cancelling_protection(tmp_path):
    service, store, feed, clock, session_id = make_service(tmp_path)
    activate(service, session_id)
    service.execute_once(session_id)
    gtt_id = next(iter(service._runtime(session_id).executor.gtt))
    service._runtime(session_id).executor.day_realised = -10000
    blocked = service.reconcile(session_id)
    assert blocked["entries_blocked"] is True
    assert blocked["state"] == "ACTIVE"
    assert blocked["working_instruments"] == [KEY]
    stop = next(rule for rule in service._runtime(session_id).executor.gtt[gtt_id]["rules"]
                if rule["strategy"] == "STOPLOSS")
    assert stop["trigger_price"] > 0
    assert gtt_id.startswith("PAPER-GTT")
    service.close()
    store.close()


def test_day_rollover_and_stale_activation_quote_fail_closed(tmp_path):
    service, store, feed, clock, session_id = make_service(tmp_path)
    activate(service, session_id)
    service._runtime(session_id).reader.quote = lambda key: Quote(
        instrument_key=key, timestamp=NOW - timedelta(seconds=300), bid=100, ask=101, last=100.5, volume=100000,
    )
    assert service.execute_once(session_id)["state"] == "HALTED"
    assert service.public(session_id)["fault"] == "STALE_DATA"
    service.close()
    store.close()

    service, store, feed, clock, session_id = make_service(tmp_path / "stale")
    service.arm(session_id)
    service._runtime(session_id).reader.quote = lambda key: Quote(
        instrument_key=key, timestamp=NOW - timedelta(seconds=300), bid=100, ask=101, last=100.5, volume=100000,
    )
    with pytest.raises(NotReady, match="quote"):
        service.activate(session_id)
    assert store.session(session_id)["state"] == "ARMED"
    service.close()
    store.close()


def test_ambiguous_live_submission_is_unresolved_and_never_blindly_retried(tmp_path, monkeypatch):
    service, store, feed, clock, session_id = make_service(tmp_path, live=True)
    activate(service, session_id)
    item = evidence()
    service.evidence = EvidenceRegistry([replace(item, synthetic=False, provenance_verified=True)])
    calls = []

    def ambiguous(*args, **kwargs):
        calls.append(kwargs)
        raise AmbiguousSubmission("test timeout after write; outcome unknown")

    monkeypatch.setattr(service._runtime(session_id).executor, "place_protected_entry", ambiguous)
    result = service.execute_once(session_id)
    assert result["state"] == "HALTED"
    assert service.public(session_id)["fault"] == "AMBIGUOUS_SUBMISSION"
    intents = store.intents(session_id)
    assert len(intents) == 1 and intents[0]["state"] == "UNRESOLVED"
    with pytest.raises(NotReady, match="incomplete"):
        service.reconcile(session_id)
    assert len(calls) == 1
    assert store.intents(session_id)[0]["state"] == "UNRESOLVED"
    with pytest.raises(NotReady, match="incomplete"):
        service.reconcile(session_id)
    assert service.public(session_id)["state"] == "HALTED"
    service.close()
    store.close()


def test_rejected_trailing_change_preserves_prior_stop_and_halts_entries(tmp_path, monkeypatch):
    service, store, feed, clock, session_id = make_service(tmp_path)
    activate(service, session_id)
    service.execute_once(session_id)
    previous = feed.bars[-1]
    feed.bars.append(Candle(NOW, previous.close - 1.0, previous.close + 0.5,
                            previous.close - 1.2, previous.close - 0.5, 100000,
                            previous.symbol))
    clock[0] = NOW + timedelta(minutes=1)
    service.execute_once(session_id)  # completed gap entry and initial protection
    runtime = service._runtime(session_id)
    position = runtime.executor.positions[KEY]
    old_stop = next(rule for rule in runtime.executor.gtt[position.protection_id]["rules"]
                    if rule["strategy"] == "STOPLOSS")["trigger_price"]
    monkeypatch.setattr("app.autonomy.service.trailing_stop",
                        lambda *args, **kwargs: (old_stop + 1, "test trail"))

    def reject(*args, **kwargs):
        raise UpstoxRejected("protection modification rejected in fake adapter")

    monkeypatch.setattr(service._runtime(session_id).executor, "modify_stop", reject)
    service._manage_trailing(runtime, make_instrument())
    confirmed_stop = next(rule for rule in runtime.executor.gtt[position.protection_id]["rules"]
                          if rule["strategy"] == "STOPLOSS")["trigger_price"]
    assert confirmed_stop == old_stop
    public = service.public(session_id)
    assert public["state"] == "HALTED" and public["fault"] == "PROTECTION_REJECTED"
    service.close()
    store.close()


def test_token_expiry_during_scan_halts_without_background_reactivation(tmp_path, monkeypatch):
    service, store, feed, clock, session_id = make_service(tmp_path)
    activate(service, session_id)

    def expired(*args, **kwargs):
        raise UpstoxAuthError("fake token expired")

    monkeypatch.setattr(feed, "completed", expired)
    assert service.scan_once(session_id)["state"] == "HALTED"
    assert service.public(session_id)["fault"] == "TOKEN_EXPIRED"
    assert service.reconcile(session_id)["state"] == "HALTED"
    with pytest.raises(PermissionError):
        service._permit(service._runtime(session_id), "ENTRY")
    service.close()
    store.close()


def test_new_day_requires_new_authorization_risk_and_activation(tmp_path):
    service, store, feed, clock, session_id = make_service(tmp_path)
    activate(service, session_id)
    clock[0] = NOW + timedelta(days=1)
    feed.bars.append(Candle(NOW + timedelta(days=1), 125, 126, 124, 125.5,
                            100000, feed.bars[-1].symbol))
    result = service.scan_once(session_id)
    assert result["state"] == "HALTED"
    assert result["fault"] == "TOKEN_EXPIRED"
    assert result["fresh_approval_required"] is True
    service.close()
    store.close()


def _fill_one_position(service, feed, clock, session_id):
    service.execute_once(session_id)
    previous = feed.bars[-1]
    feed.bars.append(Candle(NOW, previous.close - 1.0, previous.close + 0.5,
                            previous.close - 1.2, previous.close - 0.5, 100000,
                            previous.symbol))
    clock[0] = NOW + timedelta(minutes=1)
    result = service.execute_once(session_id)
    assert result["account"]["positions"]
    return result


def _reopen_after_crash(tmp_path, previous, previous_store):
    # Do not call previous.close(): ACTIVE must be recovered by the store,
    # not converted by an orderly shutdown helper.
    previous_store.close()
    store = AutonomyStore(tmp_path / "journal.sqlite3")
    service = AutonomyService(
        previous.settings, store, previous.catalog, data_dir=tmp_path,
        reader_factory=previous.reader_factory, live_factory=previous.live_factory,
        recovery_factory=previous.recovery_factory,
        feed_factory=previous.feed_factory, token_provider=previous.token_provider,
        public_ip_provider=previous.public_ip_provider, kronos=previous.kronos,
        evidence=previous.evidence, clock=previous.clock,
    )
    return service, store


@pytest.mark.parametrize("live", [False, True])
def test_scan_once_is_read_only_in_paper_and_live(tmp_path, monkeypatch, live):
    service, store, feed, clock, session_id = make_service(tmp_path, live=live)
    activate(service, session_id)
    runtime = service._runtime(session_id)
    for name in ("on_bar", "margin_for", "place_protected_entry", "modify_stop", "cancel_gtt"):
        monkeypatch.setattr(
            runtime.executor, name,
            lambda *args, _name=name, **kwargs: (_ for _ in ()).throw(
                AssertionError(f"read-only scan reached {_name}")
            ),
        )
    result = service.scan_once(session_id)
    assert result["state"] == "ACTIVE"
    assert result["last_scan_kind"] == "READ_ONLY"
    assert result["scan_endpoint_effect"] == "READ_ONLY_NO_ORDER_MUTATIONS"
    assert runtime.scanned.get(KEY) == feed.bars[-1].timestamp
    assert runtime.processed == {}
    assert runtime.executor.gtt == {}
    assert store.intents(session_id) == []
    service.close()
    store.close()


def test_order_api_and_runtime_snapshot_use_an_allowlisted_safe_view(tmp_path):
    service, store, feed, clock, session_id = make_service(tmp_path)
    runtime = service._runtime(session_id)
    runtime.orders["GTT-SAFE-VIEW"] = {
        "gtt_order_id": "GTT-SAFE-VIEW", "instrument_token": KEY,
        "quantity": 1, "product": "I", "unexpected_account_identity": "SYNTHETIC_SENTINEL",
        "access_token": "NOT_A_CREDENTIAL_SENTINEL",
        "rules": [{
            "strategy": "ENTRY", "status": "OPEN", "trigger_price": 100,
            "transaction_type": "BUY", "unexpected_user_id": "SYNTHETIC_SENTINEL",
        }],
    }
    service._persist(runtime)
    public_text = json.dumps(service.public(session_id)["orders"], sort_keys=True)
    snapshot_text = json.dumps(store.runtime_snapshot(session_id)["orders"], sort_keys=True)
    for encoded in (public_text, snapshot_text):
        assert "GTT-SAFE-VIEW" in encoded and KEY in encoded
        assert "SYNTHETIC_SENTINEL" not in encoded
        assert "NOT_A_CREDENTIAL_SENTINEL" not in encoded
        assert "unexpected" not in encoded and "access_token" not in encoded
    service.close()
    store.close()


def test_emergency_preempts_slow_execution_scan_and_cancels_unfilled(tmp_path, monkeypatch):
    service, store, feed, clock, session_id = make_service(tmp_path)
    activate(service, session_id)
    service.execute_once(session_id)
    assert store.intents(session_id)[0]["state"] == "WORKING"
    started = threading.Event()
    release = threading.Event()
    original_completed = feed.completed

    def slow_completed(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return original_completed(*args, **kwargs)

    monkeypatch.setattr(feed, "completed", slow_completed)
    errors = []
    worker = threading.Thread(
        target=lambda: _capture_scan_errors(service, session_id, errors), daemon=True
    )
    worker.start()
    assert started.wait(2)
    started_at = time.monotonic()
    try:
        stopped = service.emergency_stop(session_id)
        elapsed = time.monotonic() - started_at
    finally:
        release.set()
    worker.join(5)
    assert not worker.is_alive() and not errors
    assert elapsed < 2, "Emergency waited for the slow feed/service scan lock"
    assert stopped["state"] == "EMERGENCY_STOPPED"
    assert stopped["emergency"]["cleanup_status"] == "CONFIRMED"
    assert stopped["emergency"]["cancellation_attempts"] == 1
    assert stopped["emergency"]["account_clear"] is False
    assert all(rule["status"] == "CANCELLED"
               for record in service._runtime(session_id).executor.gtt.values()
               for rule in record["rules"])
    assert len(store.intents(session_id)) == 1
    service.close()
    store.close()


def _capture_scan_errors(service, session_id, errors):
    try:
        service.execute_once(session_id)
    except Exception as exc:  # pragma: no cover - asserted by the parent test
        errors.append(exc)


def test_emergency_cancellation_ambiguity_stays_unresolved_and_blocks_arm(tmp_path, monkeypatch):
    service, store, feed, clock, session_id = make_service(tmp_path)
    activate(service, session_id)
    service.execute_once(session_id)

    def ambiguous(*args, **kwargs):
        raise AmbiguousSubmission("fake cancellation timeout; outcome unknown")

    monkeypatch.setattr(service._runtime(session_id).executor, "cancel_gtt", ambiguous)
    stopped = service.emergency_stop(session_id)
    assert stopped["state"] == "EMERGENCY_STOPPED"
    assert stopped["emergency"]["cleanup_status"] == "UNRESOLVED"
    assert stopped["emergency"]["managed_entries_resolved"] is False
    assert stopped["emergency"]["unresolved_orders"]
    assert stopped["emergency"]["account_clear"] is False
    row = store.intents(session_id)[0]
    payload = json.loads(row["payload_json"])
    assert row["state"] == "UNRESOLVED"
    assert payload["uncertain_operation"] == "CANCEL"
    assert payload["cancel_reconciliation_required"] is True
    with pytest.raises(NotReady, match="incomplete"):
        service.reconcile(session_id)
    row = store.intents(session_id)[0]
    assert row["state"] == "UNRESOLVED"
    assert json.loads(row["payload_json"])["cancel_reconciliation_required"] is True
    with pytest.raises(NotReady, match="Emergency"):
        service.arm(session_id)
    with pytest.raises(PermissionError):
        service._permit(service._runtime(session_id), "ENTRY")
    service.close()
    store.close()


def test_emergency_preserves_no_id_submission_without_fabricating_gtt(tmp_path):
    service, store, feed, clock, session_id = make_service(tmp_path)
    activate(service, session_id)
    store.record_intent("pre-id-intent", session_id, "pre-id-signal", KEY,
                        {"candidate": {"signal_id": "pre-id-signal"}}, None)
    store.transition_intent("pre-id-intent", "SUBMISSION_ATTEMPTED")
    stopped = service.emergency_stop(session_id)
    row = store.intent("pre-id-intent")
    assert row["state"] == "SUBMISSION_ATTEMPTED"
    assert json.loads(row["broker_ids_json"]) == []
    assert stopped["emergency"]["cleanup_status"] == "UNRESOLVED"
    assert stopped["emergency"]["unresolved_orders"][0]["broker_ids"] == []
    assert stopped["emergency"]["account_clear"] is False
    service.close()
    store.close()


def test_open_position_unresolved_journal_blocks_confirmation_until_reconciled(tmp_path, monkeypatch):
    service, store, feed, clock, session_id = make_service(tmp_path)
    activate(service, session_id)
    _fill_one_position(service, feed, clock, session_id)
    runtime = service._runtime(session_id)
    intent_id = store.intents(session_id)[0]["intent_id"]
    store.transition_intent(intent_id, "UNRESOLVED")
    original_get_gtt = runtime.executor.get_gtt
    monkeypatch.setattr(runtime.executor, "get_gtt", lambda identifier: None)
    stopped = service.emergency_stop(session_id)
    assert stopped["emergency"]["cleanup_status"] == "UNRESOLVED"
    assert stopped["emergency"]["open_positions"] == [KEY]
    assert stopped["emergency"]["account_clear"] is False
    assert any(item["intent_id"] == intent_id
               for item in stopped["emergency"]["unresolved_orders"])
    with pytest.raises(NotReady):
        service.arm(session_id)
    monkeypatch.setattr(runtime.executor, "get_gtt", original_get_gtt)
    reconciled = service.reconcile(session_id)
    assert reconciled["emergency"]["cleanup_status"] == "CONFIRMED"
    assert reconciled["emergency"]["account_clear"] is False
    assert reconciled["account"]["positions"]
    assert not any("Emergency stop" in blocker
                   for blocker in reconciled["readiness"]["blockers"])
    service.close()
    store.close()


def test_restart_restores_paper_positions_protection_and_disarms(tmp_path):
    service, store, feed, clock, session_id = make_service(tmp_path)
    activate(service, session_id)
    before = _fill_one_position(service, feed, clock, session_id)
    old_cash = service._runtime(session_id).executor.cash
    old_gtt = service._runtime(session_id).executor.gtt
    recovered_service, recovered_store = _reopen_after_crash(tmp_path, service, store)
    recovered = recovered_service.public(session_id)
    assert recovered["state"] == "HALTED"
    assert recovered["recovered"] is True
    assert recovered["stop_requested"] is True
    assert recovered["authorized_day"] == "" and recovered["risk"] is None
    assert recovered["account"]["positions"] == before["account"]["positions"]
    assert recovered["orders"] and recovered["intents"]
    runtime = recovered_service._runtime(session_id)
    assert runtime.executor.cash == old_cash
    assert runtime.executor.gtt == old_gtt
    with pytest.raises(PermissionError):
        recovered_service._permit(runtime, "ENTRY")
    unchanged = recovered_service.scan_once(session_id)
    assert unchanged["state"] == "HALTED"
    assert runtime.executor.cash == old_cash and runtime.executor.gtt == old_gtt
    assert any("fresh" in blocker.lower() for blocker in recovered["readiness"]["blockers"])
    recovered_service.close()
    recovered_store.close()


def test_restart_with_unresolved_live_intent_has_no_executor_or_fake_id(tmp_path):
    service, store, feed, clock, session_id = make_service(tmp_path, live=True)
    activate(service, session_id)
    service.execute_once(session_id)
    service.reconcile(session_id)
    row = store.intents(session_id)[0]
    store.transition_intent(row["intent_id"], "SUBMISSION_ATTEMPTED", broker_ids=[])
    factory_calls = []
    original_factory = service.live_factory

    def factory(provider):
        factory_calls.append(provider)
        return original_factory(provider)

    service.live_factory = factory
    recovered_service, recovered_store = _reopen_after_crash(tmp_path, service, store)
    assert factory_calls == [], "recovery must not create a LIVE executor"
    recovered = recovered_service.public(session_id)
    runtime = recovered_service._runtime(session_id)
    assert recovered["state"] == "HALTED" and runtime.executor is None
    assert recovered["broker_mutations_reachable"] is False
    assert recovered["orders"] and recovered["intents"]
    stopped = recovered_service.emergency_stop(session_id)
    recovered_row = recovered_store.intent(row["intent_id"])
    assert recovered_row["state"] == "SUBMISSION_ATTEMPTED"
    assert json.loads(recovered_row["broker_ids_json"]) == []
    assert stopped["emergency"]["cleanup_status"] == "UNRESOLVED"
    assert stopped["emergency"]["account_clear"] is False
    assert factory_calls == []
    recovered_service.close()
    recovered_store.close()


def test_evidence_provenance_effects_and_forged_verification(tmp_path):
    synthetic = evidence()
    candidate = Candidate(
        instrument_key=KEY, signal_id="evidence-check", bar_timestamp=NOW,
        family="TREND_MOMENTUM", side="BUY", entry_price=100, stop_price=95,
        target_price=110, regime="RANGE", score=1, explanation="test",
        evidence_id=synthetic.evidence_id,
    )
    registry = EvidenceRegistry([synthetic])
    assert registry.match(candidate, "1m", live=False)[0] is False
    assert registry.public()[0]["paper_execution_effect"] == "BLOCKED_SYNTHETIC_RESEARCH_ONLY"
    operator = replace(synthetic, synthetic=False, provenance_verified=False)
    registry = EvidenceRegistry([operator])
    assert registry.match(candidate, "1m", live=False)[0] is True
    live = registry.match(candidate, "1m", live=True)
    assert live[0] is False and "independently verified" in live[1]
    verified = replace(operator, provenance_verified=True)
    assert EvidenceRegistry([verified]).match(candidate, "1m", live=True)[0] is True
    raw = asdict(operator)
    raw["generated_at"] = operator.generated_at.isoformat()
    raw["data_end"] = operator.data_end.isoformat()
    raw["schema"] = "axiom.autonomy.evidence.v1"
    raw["provenance_verified"] = True
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "forged.json").write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="cannot be asserted"):
        EvidenceRegistry.load(evidence_dir)


def test_repeated_emergency_after_confirmed_cancel_is_idempotent(tmp_path):
    service, store, feed, clock, session_id = make_service(tmp_path)
    activate(service, session_id)
    service.execute_once(session_id)
    first = service.emergency_stop(session_id)
    assert first["emergency"]["cleanup_status"] == "CONFIRMED"
    second = service.emergency_stop(session_id)
    assert second["emergency"]["cleanup_status"] == "CONFIRMED"
    assert second["emergency"]["cancellation_attempts"] == 0
    attempts = [event for event in store.events(session_id=session_id)
                if event["payload"].get("state") == "CANCEL_ATTEMPTED"]
    assert len(attempts) == 1
    assert store.intents(session_id)[0]["state"] == "CANCELLED"
    service.close()
    store.close()


def test_cancel_does_not_proceed_after_incomplete_reconciliation(tmp_path, monkeypatch):
    service, store, feed, clock, session_id = make_service(tmp_path)
    activate(service, session_id)
    service.execute_once(session_id)
    runtime = service._runtime(session_id)
    monkeypatch.setattr(runtime.executor, "get_gtt", lambda identifier: None)
    cancellations = []
    monkeypatch.setattr(runtime.executor, "cancel_gtt",
                        lambda *args, **kwargs: cancellations.append((args, kwargs)))
    with pytest.raises(SessionError, match="reconciliation"):
        service.cancel_unfilled(session_id, KEY)
    assert cancellations == []
    assert store.intents(session_id)[0]["state"] == "UNRESOLVED"
    with pytest.raises(NotReady):
        service.arm(session_id)
    service.close()
    store.close()


@pytest.mark.parametrize("stage", ["readiness", "activation", "submission"])
@pytest.mark.parametrize("case", ["inactive", "exchange", "product", "order_type", "short"])
def test_route_permissions_rechecked_at_every_order_boundary(tmp_path, stage, case):
    service, store, feed, clock, session_id = make_service(tmp_path, live=True)
    runtime = service._runtime(session_id)
    values = dict(
        active=True, exchanges=frozenset({"NSE"}), products=frozenset({"I"}),
        order_types=frozenset({"LIMIT", "SL"}), user_fingerprint="fingerprint",
        static_ips=frozenset({"1.2.3.4"}),
    )
    expected = {
        "inactive": "Active Upstox profile", "exchange": "exchange permission",
        "product": "product permission", "order_type": "order-type",
        "short": "Short-selling route",
    }[case]
    if case == "inactive":
        values["active"] = False
    elif case == "exchange":
        values["exchanges"] = frozenset()
    elif case == "product":
        values["products"] = frozenset()
    elif case == "order_type":
        values["order_types"] = frozenset({"LIMIT"})

    def revoke_permissions():
        if case == "short":
            runtime.instruments[KEY] = replace(runtime.instruments[KEY], short_route_metadata=False)
        else:
            runtime.reader.profile = lambda **kwargs: AccountProfile(**values)

    if stage == "readiness":
        revoke_permissions()
        if case != "short":
            runtime.profile = AccountProfile(**values)
        readiness = service.readiness(session_id)
        assert readiness["ready"] is False
        assert any(expected in blocker for blocker in readiness["blockers"])
        with pytest.raises(NotReady, match=expected):
            service.arm(session_id)
    elif stage == "activation":
        service.arm(session_id)
        revoke_permissions()
        activation_expected = "profile became inactive" if case == "inactive" else expected
        with pytest.raises(NotReady, match=activation_expected):
            service.activate(session_id)
        assert service.public(session_id)["state"] == "ARMED"
    else:
        activate(service, session_id)
        revoke_permissions()
        result = service.execute_once(session_id)
        assert result["state"] == "HALTED"
        assert result["fault"] == "PERMISSION_REVOKED"
        assert store.intents(session_id) == []
    assert runtime.executor.gtt == {}
    service.close()
    store.close()


def test_cancel_retry_requires_fresh_read_and_clears_only_on_terminal_proof(tmp_path, monkeypatch):
    service, store, feed, clock, session_id = make_service(tmp_path)
    activate(service, session_id)
    service.execute_once(session_id)
    runtime = service._runtime(session_id)
    original_cancel = runtime.executor.cancel_gtt

    def ambiguous(*args, **kwargs):
        raise AmbiguousSubmission("fake timeout")

    monkeypatch.setattr(runtime.executor, "cancel_gtt", ambiguous)
    first = service.cancel_unfilled(session_id, KEY)
    assert first["state"] == "HALTED"
    assert store.intents(session_id)[0]["state"] == "UNRESOLVED"
    with pytest.raises(NotReady, match="incomplete"):
        service.reconcile(session_id)
    assert store.intents(session_id)[0]["state"] == "UNRESOLVED"
    monkeypatch.setattr(runtime.executor, "cancel_gtt", original_cancel)
    service.cancel_unfilled(session_id, KEY)
    row = store.intents(session_id)[0]
    assert row["state"] == "CANCELLED"
    payload = json.loads(row["payload_json"])
    assert payload["cancel_reconciliation_required"] is False
    assert payload["cancel_resolved_at"]
    assert service.reconcile(session_id)["state"] == "HALTED"
    service.close()
    store.close()


def test_browser_recovery_paper_is_read_only_until_fresh_full_approval(tmp_path):
    service, store, feed, clock, session_id = make_service(tmp_path)
    activate(service, session_id)
    before = _fill_one_position(service, feed, clock, session_id)
    runtime = service._runtime(session_id)
    executor = runtime.executor
    gtt = executor.gtt
    recovered = service.recover_view(session_id)
    assert recovered["state"] == "HALTED" and recovered["recovered"] is True
    assert recovered["authorized_day"] == "" and recovered["risk"] is None
    assert recovered["account"]["positions"] == before["account"]["positions"]
    assert runtime.executor is executor and executor.gtt == gtt
    assert recovered["stop_requested"] is True
    assert service.execute_once(session_id)["state"] == "HALTED"
    assert executor.gtt == gtt
    with pytest.raises(NotReady):
        service.arm(session_id)
    service.authorize(session_id)
    service.set_risk(session_id, max_daily_loss=10000,
                     per_trade_risk_fraction=0.005,
                     equity_at_entry=executor.account().equity)
    with pytest.raises(NotReady, match="reconciliation"):
        service.arm(session_id)
    service.reconcile(session_id)
    service.arm(session_id)
    activated = service.activate(session_id)
    assert activated["state"] == "ACTIVE"
    assert activated["account"]["positions"] == before["account"]["positions"]
    assert executor.gtt == gtt and len(store.intents(session_id)) == 1
    service.close()
    store.close()


def test_browser_recovery_live_reader_then_activate_writer_without_duplicate(tmp_path):
    service, store, feed, clock, session_id = make_service(tmp_path, live=True)
    activate(service, session_id)
    service.execute_once(session_id)
    service.reconcile(session_id)
    runtime = service._runtime(session_id)
    writer = runtime.executor
    orders = runtime.orders
    factory_calls = []
    original_factory = service.live_factory

    def factory(provider):
        factory_calls.append(provider)
        return original_factory(provider)

    service.live_factory = factory
    recovered = service.recover_view(session_id)
    assert recovered["state"] == "HALTED" and runtime.executor is None
    assert recovered["orders"] and recovered["intents"]
    assert recovered["authorized_day"] == "" and recovered["risk"] is None
    assert factory_calls == []
    service.authorize(session_id)
    assert isinstance(runtime.executor, FakeRecovery)
    assert service.public(session_id)["broker_mutations_reachable"] is False
    assert factory_calls == []
    service.set_risk(session_id, max_daily_loss=10000,
                     per_trade_risk_fraction=0.005, equity_at_entry=None)
    with pytest.raises(NotReady, match="reconciliation"):
        service.arm(session_id)
    service.reconcile(session_id)
    service.arm(session_id)
    assert isinstance(runtime.executor, FakeRecovery)
    activated = service.activate(session_id)
    assert activated["state"] == "ACTIVE"
    assert runtime.executor is writer and factory_calls
    assert len(store.intents(session_id)) == 1
    assert runtime.orders == orders
    service.close()
    store.close()
