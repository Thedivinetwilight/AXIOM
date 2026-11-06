"""Live-execution gates: credential separation, hard limits, lifecycle
confirmation, deep Arm readiness, single-instance lease, and PAPER isolation.

Contract under test:
- PAPER is the startup default; data tokens, quotes, charts and mode
  selection never implicitly enable live execution.
- The manual market-data token flow stays read-only. LIVE execution requires
  a SEPARATE explicit credential/authorization — the read-only token is
  never silently reused, and the LIVE credential is never used for reads.
- Server-side hard limits (quantity/notional/per-symbol/aggregate exposure,
  open positions/daily loss/drawdown/order rate/attempts/quote age) reject
  missing, zero, invalid or exceeded values and are never auto-relaxed.
- Arm requires the full gate list (fresh auth, identity/permissions, exact
  instruments, fresh quotes, market availability, funds/margin, clock sync,
  static IP, reconciliation, no unresolved intents, clear emergency stop,
  single-instance lease, explicit nonzero bounded risk).
- Arm and Activate stay separate; LIVE Activate requires a fresh explicit
  confirmation naming real-money LIVE trading and permitting the
  already-reviewed strategy. Nothing else may Arm or Activate.
- Uncertain submissions are never blindly retried (pinned elsewhere); PAPER
  and read-only paths can never reach order endpoints.
"""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.autonomy.config import AutonomySettings
from app.autonomy.lease import LeaseUnavailable, SingleInstanceLease
from app.autonomy.service import NotReady, SessionError
from app.autonomy.upstox import Quote, UpstoxLiveBroker, UpstoxRejected
from app.autonomy.upstox_token import (
    MemoryUpstoxTokenStore, resolve_live_token, resolve_live_token_provider,
    resolve_token,
)
from tests.test_autonomy_service import LIVE_CONFIRM, activate, make_service

NOW = datetime(2026, 10, 29, 5, 0, tzinfo=timezone.utc)


def _events(store, session_id):
    return store.events(session_id=session_id, limit=500)


# ---------------------------------------------------------------------------
# Startup default and no-implicit-live


def test_startup_is_paper_and_surface_actions_never_arm_or_activate(tmp_path):
    service, store, feed, clock, session_id = make_service(tmp_path)
    assert service.public(session_id)["mode"] == "PAPER"
    # make_service configures risk (READY) but never arms/activates.
    assert service.public(session_id)["state"] == "READY"
    assert all(event["event_type"] not in {"OPERATOR_ARM", "OPERATOR_ACTIVATE"}
               for event in _events(store, session_id))
    store.close()


# ---------------------------------------------------------------------------
# Credential separation


def test_market_data_token_is_never_used_for_live_and_vice_versa(tmp_path, monkeypatch):
    monkeypatch.delenv("UPSTOX_LIVE_ACCESS_TOKEN", raising=False)
    market_store = MemoryUpstoxTokenStore()
    market_store.save("read-only-market-data-token")
    live_store = MemoryUpstoxTokenStore()
    # Empty live slot: the read-only token must NOT leak into live resolution.
    assert resolve_token(market_store) == ("read-only-market-data-token", "protected-store")
    assert resolve_live_token(live_store) == ("", "none")
    live_store.save("separate-live-trading-token")
    assert resolve_live_token(live_store) == ("separate-live-trading-token", "protected-store")
    monkeypatch.setenv("UPSTOX_LIVE_ACCESS_TOKEN", "env-live-token")
    empty_live = MemoryUpstoxTokenStore()
    assert resolve_live_token(empty_live) == ("env-live-token", "environment")
    # The provider chain returns only the live slot/env values.
    assert resolve_live_token_provider(live_store)() == "separate-live-trading-token"
    assert resolve_live_token_provider(empty_live)() == "env-live-token"


def test_live_authorize_requires_the_separate_live_credential(tmp_path, monkeypatch):
    monkeypatch.delenv("UPSTOX_LIVE_ACCESS_TOKEN", raising=False)
    from app.autonomy.upstox import UpstoxAuthError

    service, store, feed, clock, session_id = make_service(tmp_path / "live", live=True)
    service.live_token_provider = lambda: ""
    with pytest.raises(UpstoxAuthError, match="separate LIVE trading credential"):
        service.authorize(session_id)
    # Arm readiness names the same blocker.
    service._runtime(session_id).state = "READY"
    with pytest.raises(NotReady, match="separate LIVE trading credential"):
        service.arm(session_id)
    store.close()


def test_live_executor_binds_to_the_live_provider_not_the_market_data_one(tmp_path):
    providers = []
    service, store, feed, clock, session_id = make_service(tmp_path, live=True)
    service.live_token_provider = lambda: "live-credential-xyz"
    service.token_provider = lambda: "market-data-credential-abc"
    service.live_factory = lambda provider: providers.append(provider()) or service._runtime(
        session_id).executor
    runtime = service._runtime(session_id)
    runtime.executor = None
    service._ensure_live_executor(runtime)
    assert providers == ["live-credential-xyz"]
    assert "market-data-credential-abc" not in providers
    store.close()


# ---------------------------------------------------------------------------
# Hard limits


def test_hard_limits_reject_missing_zero_and_invalid_configuration():
    with pytest.raises(ValueError):
        AutonomySettings(max_order_quantity=0)
    with pytest.raises(ValueError):
        AutonomySettings(max_order_notional=-1)
    with pytest.raises(ValueError):
        AutonomySettings(max_open_positions=0)
    with pytest.raises(ValueError):
        AutonomySettings(max_daily_order_submits=0)
    with pytest.raises(ValueError):
        AutonomySettings(max_per_symbol_exposure=2.0, max_aggregate_exposure=1.0)
    with pytest.raises(ValueError):
        AutonomySettings(max_write_attempts=0)
    AutonomySettings()  # defaults are positive and bounded


def test_exceeded_hard_limits_block_the_order_without_relaxing(tmp_path):
    service, store, feed, clock, session_id = make_service(tmp_path)
    activate(service, session_id)
    service.settings = replace(service.settings, max_order_quantity=1)
    service.execute_once(session_id)
    events = _events(store, session_id)
    blocked = [e for e in events if e["event_type"] == "CANDIDATE_BLOCKED"]
    assert blocked, "the oversized order must be blocked and named"
    assert any("hard limit" in str(e["payload"]) for e in blocked)
    assert store.intents(session_id) == []  # nothing reached the broker path
    # The limit itself is unchanged: never auto-relaxed.
    assert service.settings.max_order_quantity == 1
    store.close()


def test_risk_entry_above_the_hard_ceiling_is_rejected(tmp_path):
    service, store, feed, clock, session_id = make_service(tmp_path)
    # A ceiling below the entered loss: the value passes DailyRisk.validate
    # (<= 5% of equity) but exceeds the configured hard ceiling.
    service.settings = replace(service.settings, max_daily_loss_ceiling=10_000.0)
    runtime = service._runtime(session_id)
    runtime.state = "UNCONFIGURED"
    runtime.authorized_day = service.day()
    with pytest.raises(SessionError, match="ceiling"):
        service.set_risk(session_id, max_daily_loss=40_000.0,
                         per_trade_risk_fraction=0.005, equity_at_entry=1_000_000)
    assert service.settings.max_daily_loss_ceiling == 10_000.0  # never relaxed
    store.close()


def test_write_attempts_are_capped_per_order():
    def handler(request):
        return httpx.Response(200, json={"status": "success",
                                         "data": {"gtt_order_ids": ["GTT-1"]}})

    broker = UpstoxLiveBroker(lambda: "live-token", algo_name="AXIOM-TEST",
                              max_write_attempts=2,
                              transport=httpx.MockTransport(handler), clock=lambda: NOW)
    from tests.test_autonomy_service import make_instrument
    from tests.test_autonomy_foundation import order, permit
    instrument = make_instrument()
    entry = order()
    for _ in range(2):
        broker.place_protected_entry(entry, permit(), session_id="session",
                                     instrument=instrument)
    with pytest.raises(UpstoxRejected, match="write attempts"):
        broker.place_protected_entry(entry, permit(), session_id="session",
                                     instrument=instrument)


# ---------------------------------------------------------------------------
# Arm deep readiness


def _ready_service(tmp_path):
    service, store, feed, clock, session_id = make_service(tmp_path)
    return service, store, feed, clock, session_id


def test_arm_requires_the_single_instance_lease(tmp_path):
    service, store, feed, clock, session_id = _ready_service(tmp_path)
    lease = SingleInstanceLease(tmp_path / "lease-dir")
    service.instance_lease = lease  # not acquired -> held == False
    with pytest.raises(NotReady, match="lease"):
        service.arm(session_id)
    lease.acquire()
    service.arm(session_id)  # held -> arm passes the lease gate
    assert service.public(session_id)["state"] == "ARMED"
    store.close()


def test_second_instance_cannot_take_the_lease(tmp_path):
    first = SingleInstanceLease(tmp_path)
    first.acquire()
    assert first.held
    second = SingleInstanceLease(tmp_path)
    with pytest.raises(LeaseUnavailable, match="Another AXIOM instance"):
        second.acquire()
    assert not second.held
    first.release()
    second.acquire()  # released -> available again
    second.release()


def test_arm_requires_a_synchronized_clock(tmp_path):
    service, store, feed, clock, session_id = _ready_service(tmp_path)
    runtime = service._runtime(session_id)
    runtime.reader.server_time = lambda: service.now() + timedelta(seconds=120)
    with pytest.raises(NotReady, match="clock"):
        service.arm(session_id)
    runtime.reader.server_time = lambda: service.now()
    service.arm(session_id)
    store.close()


def test_arm_requires_fresh_quotes_market_open_and_funds(tmp_path):
    service, store, feed, clock, session_id = _ready_service(tmp_path)
    runtime = service._runtime(session_id)
    # Stale quote blocks Arm.
    runtime.reader.quote = lambda key: Quote(
        key, 100.0, 101.0, 100.5, 1000, NOW - timedelta(seconds=600))
    with pytest.raises(NotReady, match="stale/future"):
        service.arm(session_id)
    runtime.reader.quote = lambda key: Quote(key, 100.0, 101.0, 100.5, 1000, NOW)
    # Closed market blocks Arm.
    runtime.calendar.is_open = lambda instrument, now: False
    with pytest.raises(NotReady, match="closed"):
        service.arm(session_id)
    runtime.calendar.is_open = lambda instrument, now: True
    # Insufficient funds (equity below the daily loss cap) blocks Arm.
    runtime.risk = replace(runtime.risk, max_daily_loss=900_000.0)
    executor = runtime.executor
    original_account = executor.account
    executor.account = lambda: replace(original_account(), equity=100.0,
                                       available_margin=100.0)
    with pytest.raises(NotReady, match="equity does not cover"):
        service.arm(session_id)
    store.close()


# ---------------------------------------------------------------------------
# Arm / Activate separation and LIVE confirmation


def test_live_activate_requires_the_explicit_real_money_confirmation(tmp_path):
    service, store, feed, clock, session_id = make_service(tmp_path / "live", live=True)
    service.arm(session_id)
    with pytest.raises(SessionError, match="ACTIVATE LIVE REAL-MONEY TRADING"):
        service.activate(session_id)  # no confirmation at all
    with pytest.raises(SessionError, match="ACTIVATE LIVE REAL-MONEY TRADING"):
        service.activate(session_id, confirm_live_trading="yes")
    with pytest.raises(SessionError, match="already-reviewed strategy"):
        service.activate(session_id,
                         confirm_live_trading="ACTIVATE LIVE REAL-MONEY TRADING")
    assert service.public(session_id)["state"] == "ARMED"
    # Exactly the required phrase + acknowledgement activates.
    result = service.activate(session_id, **LIVE_CONFIRM)
    assert result["state"] == "ACTIVE"
    events = _events(store, session_id)
    activation = [e for e in events if e["event_type"] == "OPERATOR_ACTIVATE"][-1]
    assert activation["payload"]["live_confirmation"] == "ACTIVATE LIVE REAL-MONEY TRADING"
    assert activation["payload"]["automatic"] is False
    store.close()


def test_paper_activate_never_requires_or_accepts_the_live_confirmation(tmp_path):
    service, store, feed, clock, session_id = make_service(tmp_path)
    activate(service, session_id)
    events = _events(store, session_id)
    activation = [e for e in events if e["event_type"] == "OPERATOR_ACTIVATE"][-1]
    assert activation["payload"]["paper_only"] is True
    assert activation["payload"]["live_confirmation"] == ""
    store.close()


# ---------------------------------------------------------------------------
# PAPER isolation from broker order calls


def test_paper_execution_never_reaches_a_broker_order_write(tmp_path, monkeypatch):
    service, store, feed, clock, session_id = make_service(tmp_path)
    activate(service, session_id)
    runtime = service._runtime(session_id)

    def forbidden(*args, **kwargs):
        raise AssertionError("PAPER path reached a broker order mutation")

    for name in ("place_protected_entry", "modify_stop", "cancel_gtt"):
        monkeypatch.setattr(runtime.executor, name, forbidden)
    service.execute_once(session_id)
    view = service.public(session_id)
    assert view["paper_only"] is True and view["live_execution"] is False
    store.close()


def test_shutdown_releases_the_lease_and_closes_its_handle(tmp_path):
    # Windows regression: an open lock handle kept the data directory busy and
    # tempfile cleanup failed with WinError 32 (PermissionError) at shutdown.
    from tests.test_upstox_token_entry import client as api_client

    with api_client(tmp_path) as web:
        lease = getattr(web.app.state, "autonomy_lease", None)
        assert lease is not None and lease.held
    assert lease.held is False
    assert lease._handle is None  # handle closed: the lock file is deletable
    (tmp_path / "autonomy" / "autonomy-single-instance.lock").unlink()
