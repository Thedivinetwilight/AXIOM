"""AXIOM-MONEY-PRINTER — markets, universes and the live first-class mode.

Covers master-spec sections 4, 5, 31-34, 37, 39, 40, 135, 143, 144:
deterministic market/session engine, market-scoped saved universes, the
explicit live state machine (never auto-armed), the order guard chain and
the broker rate-limit policy.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime

import pytest

from app.config.settings import LIVE_CONFIRM_PHRASE, Settings
from app.database.database import Database
from app.database.repository import Repository
from app.live.guard_chain import default_chain
from app.live.rate_limit import RateLimiter
from app.live.session import (ACTIVE, ARMED, BLOCKED, OFF, PAUSED, READY,
                              STOPPED, LiveSessionManager, LiveTransitionError)
from app.markets import (get_market, list_markets, market_session_state,
                         parse_instrument_identity)


@pytest.fixture()
def repo():
    database = Database("sqlite:///:memory:")
    repository = Repository(database)
    yield repository
    database.close()


def live_settings(**overrides) -> Settings:
    """A fully-armed CONFIGURATION (four switches + bounded risk)."""
    values = dict(
        mode="LIVE", live_trading=True, broker="UPSTOX_LIVE",
        live_confirm=LIVE_CONFIRM_PHRASE,
        upstox_live_access_token="live-token-under-test",
        max_position_value=10_000.0, max_open_positions=2,
        max_daily_loss=5_000.0, max_trades_per_day=10,
        default_timeframe="1m", market_data_mode="realtime",
    )
    values.update(overrides)
    return Settings(**values)


# =====================================================================
# Markets registry + session engine (spec 5, 143, 144)
# =====================================================================

class TestMarkets:
    def test_registry_covers_required_markets(self):
        ids = {m["market_id"] for m in list_markets()}
        for required in ("india", "us", "japan", "uk", "hongkong",
                         "singapore", "global"):
            assert required in ids

    def test_unknown_market_raises(self):
        with pytest.raises(KeyError):
            get_market("atlantis")

    def test_session_open_midweek_india(self):
        # Wednesday 11:00 IST -> regular session.
        state = market_session_state("india", datetime(2026, 10, 7, 11, 0))
        assert state.status == "OPEN"
        assert state.timezone == "Asia/Kolkata"
        assert state.minutes_to_next_transition is not None

    def test_session_weekend(self):
        state = market_session_state("india", datetime(2026, 10, 10, 11, 0))
        assert state.status == "WEEKEND"
        assert state.next_transition_iso is not None

    def test_session_holiday(self):
        state = market_session_state("india", datetime(2026, 10, 2, 11, 0))
        assert state.status == "HOLIDAY"

    def test_pre_open_window(self):
        state = market_session_state("india", datetime(2026, 10, 7, 9, 5))
        assert state.status == "PRE_OPEN"

    def test_global_market_is_context_only(self):
        state = market_session_state("global")
        assert state.status == "ALWAYS"
        parsed = parse_instrument_identity("ANYTHING", "global")
        assert not parsed["valid"]
        assert "not executable" in parsed["reason"]

    def test_identity_parsing(self):
        parsed = parse_instrument_identity("NSE_EQ|INE002A01018", "india")
        assert parsed["valid"]
        assert parsed["exchange"] == "NSE"
        assert parsed["segment"] == "EQ"
        assert parsed["identity"] == "NSE|INE002A01018"

    def test_identity_rejects_foreign_exchange(self):
        parsed = parse_instrument_identity("NYSE|AAPL", "india")
        assert not parsed["valid"]
        assert "does not belong" in parsed["reason"]

    def test_identity_rejects_garbage(self):
        for bad in ("", "   ", "NSE|HAS SPACE", "x" * 80):
            assert not parse_instrument_identity(bad, "india")["valid"]

    def test_us_market_sessions_in_new_york_time(self):
        state = market_session_state("us", datetime(2026, 10, 7, 10, 0))
        assert state.status == "OPEN"
        assert state.timezone == "America/New_York"


# =====================================================================
# Saved universes (spec 4, 60)
# =====================================================================

class TestUniverses:
    def test_save_list_roundtrip(self, repo):
        record = repo.save_universe(market_id="india", name="Core",
                                    symbols=["NSE|INE002A01018", "NSE|RELIANCE"])
        assert record["id"]
        listed = repo.list_universes(market_id="india")
        assert [u["name"] for u in listed] == ["Core"]
        assert listed[0]["symbols"] == ["NSE|INE002A01018", "NSE|RELIANCE"]

    def test_universes_are_market_scoped(self, repo):
        repo.save_universe(market_id="india", name="Watch", symbols=["NSE|A"])
        repo.save_universe(market_id="us", name="Watch", symbols=["NASDAQ|B"])
        assert len(repo.list_universes(market_id="india")) == 1
        assert len(repo.list_universes(market_id="us")) == 1
        assert len(repo.list_universes()) == 2

    def test_save_same_name_same_market_updates(self, repo):
        first = repo.save_universe(market_id="india", name="Core", symbols=["NSE|A"])
        second = repo.save_universe(market_id="india", name="Core",
                                    symbols=["NSE|A", "NSE|B"])
        assert first["id"] == second["id"]
        assert len(repo.list_universes(market_id="india")) == 1

    def test_pins(self, repo):
        repo.pin_instrument("india", "NSE|RELIANCE")
        pins = repo.list_pinned("india")
        assert pins and pins[0]["identity"] == "NSE|RELIANCE"
        repo.unpin_instrument("india", "NSE|RELIANCE")
        assert repo.list_pinned("india") == []


# =====================================================================
# Live state machine (spec 31, 33, 135-137)
# =====================================================================

class TestLiveStateMachine:
    def test_starts_off_with_no_history(self, repo):
        manager = LiveSessionManager(repo)
        assert manager.current().state == OFF

    def test_arm_requires_phrase(self, repo):
        manager = LiveSessionManager(repo)
        with pytest.raises(LiveTransitionError):
            manager.arm(live_settings(), confirmation_phrase="let me in")
        assert manager.current().state == OFF

    def test_arm_requires_readiness(self, repo):
        manager = LiveSessionManager(repo)
        # A daily timeframe cannot drive the live engine -> preflight ERROR ->
        # readiness BLOCKED -> arming refused even with the phrase.
        incomplete = live_settings(default_timeframe="1D")
        with pytest.raises(LiveTransitionError):
            manager.arm(incomplete, confirmation_phrase=LIVE_CONFIRM_PHRASE)

    def test_arm_refused_while_emergency_stop_active(self, repo):
        repo.set_engine_state("emergency_stop", {"stopped": True, "reason": "drill"})
        manager = LiveSessionManager(repo)
        with pytest.raises(LiveTransitionError):
            manager.arm(live_settings(), confirmation_phrase=LIVE_CONFIRM_PHRASE)

    def test_full_explicit_lifecycle(self, repo):
        manager = LiveSessionManager(repo)
        settings = live_settings()
        snapshot = manager.arm(settings, confirmation_phrase=LIVE_CONFIRM_PHRASE)
        assert snapshot.state == ARMED
        assert manager.activate().state == ACTIVE
        assert manager.pause().state == PAUSED
        assert manager.resume().state == ACTIVE
        assert manager.begin_reconciliation().state == "RECONCILING"
        assert manager.finish_reconciliation(outcome="clean").state == ACTIVE
        assert manager.stop(reason="operator done").state == STOPPED
        history = [row["state"] for row in manager.history()]
        assert history[:5] == [STOPPED, ACTIVE, "RECONCILING", ACTIVE, PAUSED]

    def test_activate_requires_prior_arming(self, repo):
        manager = LiveSessionManager(repo)
        with pytest.raises(LiveTransitionError):
            manager.activate()

    def test_restart_preserves_authorization_but_never_grants_it(self, repo):
        """Spec 136: a restarted core retains an EXPLICITLY granted state.
        A fresh manager must never derive ARMED/ACTIVE on its own — the only
        path in is the operator's arm() call."""
        pristine = LiveSessionManager(repo)
        assert pristine.current().state == OFF
        pristine.arm(live_settings(), confirmation_phrase=LIVE_CONFIRM_PHRASE)

        reborn = LiveSessionManager(repo)
        assert reborn.current().state == ARMED          # persisted history
        assert reborn.derived_state(live_settings()) == READY  # not ACTIVE
        # A NEW install (empty database) can never wake up armed.
        empty_db = Database("sqlite:///:memory:")
        empty_repo = Repository(empty_db)
        try:
            assert LiveSessionManager(empty_repo).current().state == OFF
            assert LiveSessionManager(empty_repo).derived_state(live_settings()) == READY
        finally:
            empty_db.close()

    def test_stop_clears_to_stopped_not_back_to_active(self, repo):
        manager = LiveSessionManager(repo)
        manager.arm(live_settings(), confirmation_phrase=LIVE_CONFIRM_PHRASE)
        manager.activate()
        manager.stop()
        # STOPPED must not silently drift back to ACTIVE on its own.
        assert LiveSessionManager(repo).current().state == STOPPED

    def test_emergency_block_beats_everything(self, repo):
        manager = LiveSessionManager(repo)
        manager.arm(live_settings(), confirmation_phrase=LIVE_CONFIRM_PHRASE)
        manager.activate()
        assert manager.block(reason="breach monitor").state == BLOCKED
        with pytest.raises(LiveTransitionError):
            manager.release_block(destination=ACTIVE)
        assert manager.release_block(destination=READY).state == READY

    def test_derived_state_tracks_configuration(self, repo):
        manager = LiveSessionManager(repo)
        assert manager.derived_state(Settings()) == OFF
        # A settings-like object with live INTENT but a broken confirmation
        # phrase is CONFIGURED, not READY. (The real Settings class refuses
        # to construct half-configured live mode at all — fail closed.)
        from types import SimpleNamespace

        intent_only = SimpleNamespace(mode="LIVE", live_trading=True,
                                      broker="UPSTOX_LIVE", live_confirm="",
                                      max_position_value=0, max_daily_loss=0,
                                      max_exposure=None)
        assert manager.derived_state(intent_only) == "CONFIGURED"
        assert manager.derived_state(live_settings()) == READY
        assert manager.derived_state(live_settings(), emergency_stop=True) == BLOCKED

    def test_start_confirmation_summary_is_honest(self, repo):
        manager = LiveSessionManager(repo)
        summary = manager.start_confirmation_summary(live_settings(), repository=repo)
        assert summary["broker"] == "UPSTOX_LIVE"
        assert summary["required_phrase"] == LIVE_CONFIRM_PHRASE
        assert "REAL orders" in summary["warning"]
        assert summary["emergency_stop_active"] is False


# =====================================================================
# Readiness center (spec 32, 39, 144)
# =====================================================================

class TestReadinessCenter:
    def test_statuses_never_mask_unknown_as_healthy(self, repo):
        from app.live.preflight import run_readiness_center_checks

        checks = run_readiness_center_checks(live_settings(), repository=repo)
        by_name = {c.name: c for c in checks}
        # data freshness had no input -> must be a not-checked failure
        data_check = by_name["data_freshness"]
        assert data_check.ok is False
        assert data_check.severity == "INFO"

    def test_clock_sanity_passes_on_a_sane_machine(self, repo):
        from app.live.preflight import run_readiness_center_checks

        checks = run_readiness_center_checks(live_settings(), repository=repo)
        assert any(c.name == "clock_sanity" and c.ok for c in checks)

    def test_emergency_stop_blocks_readiness(self, repo):
        from app.live.preflight import run_readiness_center_checks

        repo.set_engine_state("emergency_stop", {"stopped": True, "reason": "test"})
        checks = run_readiness_center_checks(live_settings(), repository=repo)
        kill = next(c for c in checks if c.name == "kill_switch_available")
        assert kill.ok is False and kill.severity == "ERROR"

    def test_center_verdict_not_ready_when_unverified(self, repo):
        from app.api.routes.workstation import _serialize_readiness
        from app.live.preflight import run_readiness_center_checks

        verdict = _serialize_readiness(
            run_readiness_center_checks(live_settings(), repository=repo))["verdict"]
        # Without data-freshness/token context the center must NOT say READY.
        assert verdict in ("NOT_READY_UNVERIFIED", "BLOCKED")


# =====================================================================
# Guard chain (spec 34)
# =====================================================================

class TestGuardChain:
    def _order(self, **overrides):
        order = {"side": "BUY", "quantity": 5, "price": 100.0,
                 "symbol": "NSE|RELIANCE", "strategy": "moving_average",
                 "intent": "ENTRY"}
        order.update(overrides)
        return order

    def _context(self, **overrides):
        import time as _time

        context = {"signal_id": "sig-1", "intent_id": "intent-1",
                   "signal_ts": _time.time(), "live_state": "ACTIVE",
                   "current_exposure": 0.0}
        context.update(overrides)
        return context

    def test_entry_passes_when_everything_is_clear(self):
        chain = default_chain(settings=live_settings(), session_state="ACTIVE")
        verdict = chain.evaluate(self._order(), self._context())
        assert verdict.allowed
        assert [r.guard for r in verdict.results][-1] == "FINAL AUTHORIZATION"

    def test_each_guard_can_refuse_independently(self):
        import time as _time

        cases = [
            (self._order(strategy=""), self._context(), "SIGNAL"),
            (self._order(quantity=0), self._context(), "POSITION CHECK"),
            (self._order(side="HOLD"), self._context(), "ORDER VALIDATION"),
            (self._order(), self._context(live_state="PAUSED"), "BROKER RULE CHECK"),
            (self._order(), self._context(signal_ts=_time.time() - 60),
             "DATA FRESHNESS / LATENCY CHECK"),
            (self._order(), self._context(intent_id=None), "DUPLICATE CHECK"),
        ]
        for order, context, expected_guard in cases:
            chain = default_chain(settings=live_settings(), session_state="ACTIVE")
            verdict = chain.evaluate(order, context)
            assert not verdict.allowed, (expected_guard, verdict.to_dict())
            assert verdict.refusing_guard == expected_guard

    def test_exits_bypass_entry_guards_but_stay_validated(self):
        chain = default_chain(settings=live_settings(), session_state="PAUSED")
        order = self._order(intent="EXIT", side="SELL")
        verdict = chain.evaluate(order, self._context(live_state="PAUSED"))
        assert verdict.allowed, verdict.to_dict()
        assert verdict.is_exit
        guards = [r.guard for r in verdict.results]
        assert "SIGNAL" not in guards          # entry-only guard skipped
        assert "ORDER VALIDATION" in guards    # still validated

    def test_guard_exception_fails_closed(self):
        from app.live.guard_chain import GuardChain

        chain = GuardChain()
        chain.add("BROKEN", lambda order, ctx: (_ for _ in ()).throw(RuntimeError("boom")))
        verdict = chain.evaluate(self._order(), self._context())
        assert not verdict.allowed
        assert verdict.refusing_guard == "BROKEN"

    def test_duplicate_intent_blocked(self):
        chain = default_chain(settings=live_settings(), session_state="ACTIVE",
                              recent_intent_ids={"intent-1"})
        verdict = chain.evaluate(self._order(), self._context())
        assert not verdict.allowed
        assert verdict.refusing_guard == "DUPLICATE CHECK"

    def test_emergency_stop_blocks_final_authorization(self):
        chain = default_chain(settings=live_settings(), session_state="ACTIVE",
                              emergency_stop=True)
        verdict = chain.evaluate(self._order(), self._context())
        assert not verdict.allowed
        assert verdict.refusing_guard == "FINAL AUTHORIZATION"


# =====================================================================
# Rate limits (spec 40)
# =====================================================================

class TestRateLimiter:
    def test_per_second_limit_enforced(self):
        fake_now = [1000.0]
        limiter = RateLimiter("REGULAR_ALGO", clock=lambda: fake_now[0])
        for _ in range(10):
            assert limiter.admit().allowed
        refused = limiter.admit()
        assert not refused.allowed
        assert refused.retry_after_s > 0

    def test_window_release(self):
        fake_now = [1000.0]
        limiter = RateLimiter("REGULAR_ALGO", clock=lambda: fake_now[0])
        for _ in range(10):
            limiter.admit()
        fake_now[0] += 1.1
        assert limiter.admit().allowed

    def test_rejections_are_not_counted(self):
        fake_now = [1000.0]
        limiter = RateLimiter("REGULAR_ALGO", clock=lambda: fake_now[0])
        for _ in range(10):
            limiter.admit()
        limiter.admit()
        assert limiter.counts()["per_sec"] == 10
        assert limiter.stats()["rejected"] == 1

    def test_profile_configurable_not_constant(self):
        limiter = RateLimiter("REGISTERED_ALGO")
        assert limiter.limits["per_sec"] == 50
        limiter.configure(per_sec=5)
        assert limiter.limits["per_sec"] == 5
        with pytest.raises(ValueError):
            limiter.configure(per_sec=-1)
        with pytest.raises(KeyError):
            RateLimiter("UNLIMITED")
