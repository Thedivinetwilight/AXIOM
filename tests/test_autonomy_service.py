from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import math
from pathlib import Path

import pytest

from app.autonomy.config import AutonomySettings
from app.autonomy.models import AccountProfile, Instrument, StrategyEvidence
from app.autonomy.paper import PaperExecution
from app.autonomy.service import AutonomyService, NotReady
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
                              frozenset({"LIMIT"}), "fingerprint", frozenset({"1.2.3.4"}))

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
    service = AutonomyService(
        settings, store, catalog, data_dir=tmp_path,
        reader_factory=lambda provider: reader,
        live_factory=lambda provider: executor,
        feed_factory=lambda reader: feed,
        token_provider=lambda: "test-only-not-a-credential",
        public_ip_provider=lambda: "1.2.3.4",
        evidence=EvidenceRegistry([evidence()]), clock=lambda: clock[0],
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
    first = service.scan_once(session_id)
    assert first["working_instruments"] == [KEY]
    assert len(store.intents(session_id)) == 1
    assert store.intents(session_id)[0]["state"] == "WORKING"
    # Repeating the same completed bar cannot duplicate the intent.
    service.scan_once(session_id)
    assert len(store.intents(session_id)) == 1

    previous = feed.bars[-1]
    feed.bars.append(Candle(NOW, previous.close - 1.0, previous.close + 0.5,
                            previous.close - 1.2, previous.close - 0.5, 100000,
                            previous.symbol))
    clock[0] = NOW + timedelta(minutes=1)
    second = service.scan_once(session_id)
    assert second["account"]["positions"], second
    assert second["account"]["positions"][0]["protection_id"]
    assert store.intents(session_id)[0]["state"] == "PROTECTED"
    confirmed_stop = second["account"]["positions"][0]["stop_price"]

    # A later gap exits at the worse open; the service reconciles CLOSED rather
    # than assuming the GTT is still working or the account is flat by guess.
    feed.bars.append(Candle(NOW + timedelta(minutes=1), 80.0, 82.0, 79.0, 81.0,
                            100000, previous.symbol))
    clock[0] = NOW + timedelta(minutes=2)
    third = service.scan_once(session_id)
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


def test_pause_and_emergency_stop_preserve_protection_and_revoke_permits(tmp_path):
    service, store, feed, clock, session_id = make_service(tmp_path)
    activate(service, session_id)
    service.scan_once(session_id)
    identifier = store.intents(session_id)[0]["broker_ids_json"]
    paused = service.pause(session_id)
    assert paused["state"] == "PAUSED"
    with pytest.raises(PermissionError):
        service._permit(service._runtime(session_id), "PROTECTIVE")
    stopped = service.emergency_stop(session_id)
    assert stopped["state"] == "EMERGENCY_STOPPED"
    assert service._runtime(session_id).executor.gtt
    assert "PAPER-GTT" in identifier
    assert all(event["event_type"] != "CANCEL_ATTEMPTED" for event in store.events(session_id=session_id))
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


def test_reconciliation_latches_loss_cap_without_cancelling_protection(tmp_path):
    service, store, feed, clock, session_id = make_service(tmp_path)
    activate(service, session_id)
    service.scan_once(session_id)
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
    assert service.scan_once(session_id)["state"] == "HALTED"
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
    service.evidence = EvidenceRegistry([replace(item, synthetic=False)])
    calls = []

    def ambiguous(*args, **kwargs):
        calls.append(kwargs)
        raise AmbiguousSubmission("test timeout after write; outcome unknown")

    monkeypatch.setattr(service._runtime(session_id).executor, "place_protected_entry", ambiguous)
    result = service.scan_once(session_id)
    assert result["state"] == "HALTED"
    assert service.public(session_id)["fault"] == "AMBIGUOUS_SUBMISSION"
    intents = store.intents(session_id)
    assert len(intents) == 1 and intents[0]["state"] == "UNRESOLVED"
    service.reconcile(session_id)
    assert len(calls) == 1
    assert store.intents(session_id)[0]["state"] == "UNRESOLVED"
    assert service.reconcile(session_id)["state"] == "HALTED"
    service.close()
    store.close()


def test_rejected_trailing_change_preserves_prior_stop_and_halts_entries(tmp_path, monkeypatch):
    service, store, feed, clock, session_id = make_service(tmp_path)
    activate(service, session_id)
    service.scan_once(session_id)
    previous = feed.bars[-1]
    feed.bars.append(Candle(NOW, previous.close - 1.0, previous.close + 0.5,
                            previous.close - 1.2, previous.close - 0.5, 100000,
                            previous.symbol))
    clock[0] = NOW + timedelta(minutes=1)
    service.scan_once(session_id)  # completed gap entry and initial protection
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
