"""Source-backed regression cases for fail-closed engine entry gates.

Every engine order here is handled by the existing in-memory httpx.MockTransport
fixture; these tests make no broker/network requests and use disposable SQLite DBs.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from app.config.settings import Settings
from app.data.cross_market import CrossMarketCache
from app.data.liquidity import LiquidityIntelligence, LiquiditySnapshot
from app.engine.events import EventDrivenDecisionEngine
from app.learning.volatility_sizing import SizingContext, VolatilityAwareSizer
from app.research.quality_scorer import QualityResult
from app.risk.net_edge import NetEdgeGate
from app.strategies.base import Signal
from app.database.database import Database
from app.database.repository import Repository
from app.risk.limits import HardRiskLimits
from tests.test_v3_0_1_execution_safety import (
    FixedSignal,
    candle,
    make_broker,
    make_engine,
)


@pytest.fixture()
def audit_repository(tmp_path):
    database = Database(f"sqlite:///{tmp_path}/engine-audit.db")
    repository = Repository(database)
    yield repository
    database.close()


def broker_state():
    return {"placed": 0, "orders": {}, "next": 21001, "auth_mode": "ok"}


def gate_failures(repository, gate):
    return [event for event in repository.list_system_events(limit=100)
            if event["kind"] == "ENTRY_GATE_FAILURE"
            and event["payload"].get("gate") == gate]


class RaisingScore:
    def score(self, **_kwargs):
        raise RuntimeError("test scorer failure")

    def passes(self, _result):
        raise AssertionError("passes must not run after score fails")


class RaisingPass:
    def score(self, **_kwargs):
        return QualityResult(quality_score=1.0, confidence=1.0)

    def passes(self, _result):
        raise RuntimeError("test scorer pass failure")


@pytest.mark.parametrize("scorer", [RaisingScore(), RaisingPass()])
def test_quality_scorer_failures_block_entries(audit_repository, scorer):
    state = broker_state()
    broker = make_broker(state, audit_repository)
    engine = make_engine(audit_repository, broker, quality_scorer=scorer,
                         cost_rate=0.0)

    engine.process_candle(candle(1, 100.0))

    assert state["placed"] == 0
    assert gate_failures(audit_repository, "quality_scorer")


class RaisingSizer:
    def adjust(self, _context: SizingContext):
        raise RuntimeError("test sizing failure")


class ExpandingSizer:
    def adjust(self, context: SizingContext):
        return context.base_quantity * 2, {"reason": "test expansion"}


def test_volatility_sizer_cannot_expand_past_sizing_budget(audit_repository):
    import json

    state = broker_state()
    broker = make_broker(state, audit_repository)
    engine = make_engine(
        audit_repository, broker, sizing_fraction=0.10,
        volatility_sizer=ExpandingSizer(), liquidity_intel=None,
        cost_rate=0.0,
    )

    engine.process_candle(candle(1, 100.0))

    assert state["placed"] == 1
    submitted = json.loads(state["bodies"][0])
    assert submitted["quantity"] <= 100


def test_volatility_sizing_failure_blocks_entries(audit_repository):
    state = broker_state()
    broker = make_broker(state, audit_repository)
    engine = make_engine(audit_repository, broker,
                         volatility_sizer=RaisingSizer(), cost_rate=0.0)

    engine.process_candle(candle(1, 100.0))

    assert state["placed"] == 0
    assert gate_failures(audit_repository, "volatility_sizing")


def test_quality_gate_failure_does_not_trap_protective_stop(audit_repository):
    state = broker_state()
    broker = make_broker(state, audit_repository)
    settings = Settings()
    engine = make_engine(
        audit_repository, broker,
        strategy=FixedSignal(Signal.HOLD),
        quality_scorer=RaisingScore(),
        risk_limits=HardRiskLimits.from_settings(settings),
        stop_loss=0.05, take_profit=0.10, cost_rate=0.0,
    )
    engine.process_candle(candle(1, 100.0))

    broker.submit("BUY", 10, 100.0, intent_id="audit-position",
                  symbol="RELIANCE")
    remote_id = next(iter(state["orders"]))
    state["orders"][remote_id] = {
        "status": "complete", "filled": 10, "avg": 100.0,
    }
    engine.reconcile()
    before_stop = state["placed"]

    engine.process_candle(candle(2, 94.0))

    assert state["placed"] == before_stop + 1
    decisions = [event for event in audit_repository.list_system_events(limit=100)
                 if event["kind"] == "DECISION"]
    assert decisions[0]["payload"]["reason"] == "PROTECTIVE_STOP_LOSS"


@pytest.mark.parametrize("plan", [
    None,
    {},
    {"allocations": {}},
    {"allocations": {"momentum": "not-a-number"}},
    {"allocations": {"momentum": "nan"}},
    {"allocations": {"momentum": "inf"}},
    {"allocations": {"momentum": -1.0}},
])
def test_required_allocator_missing_or_invalid_plan_blocks_entries(
        audit_repository, plan):
    audit_repository.set_engine_state("capital_allocation", plan)
    state = broker_state()
    broker = make_broker(state, audit_repository)
    allocator = SimpleNamespace(repository=audit_repository)
    engine = make_engine(audit_repository, broker, allocator=allocator,
                         sizing_fraction=1.0, cost_rate=0.0)

    engine.process_candle(candle(1, 100.0))

    assert state["placed"] == 0
    assert gate_failures(audit_repository, "capital_allocation")


def test_no_configured_allocator_keeps_ordinary_sizing_policy(audit_repository):
    audit_repository.set_engine_state("capital_allocation", None)
    state = broker_state()
    broker = make_broker(state, audit_repository)
    engine = make_engine(audit_repository, broker, allocator=None,
                         sizing_fraction=0.10, cost_rate=0.0)

    engine.process_candle(candle(1, 100.0))

    assert state["placed"] == 1


def test_sandbox_engine_uses_configured_position_size(audit_repository):
    state = broker_state()
    broker = make_broker(state, audit_repository)
    engine = make_engine(
        audit_repository, broker, settings=Settings(position_size=0.10))

    assert engine.sizing_fraction == pytest.approx(0.10)


def test_hot_path_setting_records_candle_latency_and_is_dashboard_visible(
        audit_repository):
    state = broker_state()
    broker = make_broker(state, audit_repository)
    engine = make_engine(
        audit_repository, broker, strategy=FixedSignal(Signal.HOLD),
        settings=Settings(hot_path_max_latency_ms=100.0),
    )

    engine.process_candle(candle(1, 100.0))

    assert engine.hot_path_monitor is engine.hot_path
    assert engine.hot_path_monitor.snapshot()["count"] == 1


class ReducingLiquidity(LiquidityIntelligence):
    def assess(self, symbol, **_kwargs):
        return LiquiditySnapshot(
            symbol=symbol, timestamp=time.time(), bid=99.9, ask=100.1,
            bid_qty=1000.0, ask_qty=1000.0, last_price=100.0,
            volume=1000.0, spread=0.2, spread_bps=20.0, imbalance=0.0,
            liquidity_score=0.3, execution_quality="moderate", stale=False,
            reasons=["moderate liquidity"],
        )

    def should_reduce_or_reject(self, _snapshot):
        return False, True, "moderate liquidity"


class CountingRiskLimits:
    def __init__(self, delegate):
        self.delegate = delegate
        self.calls = 0

    def check(self, *args, **kwargs):
        self.calls += 1
        return self.delegate.check(*args, **kwargs)


def test_liquidity_reduction_recommendation_is_enforced_before_hard_risk(
        audit_repository):
    state = broker_state()
    broker = make_broker(state, audit_repository)
    risk_limits = CountingRiskLimits(HardRiskLimits.from_settings(Settings()))
    engine = make_engine(
        audit_repository, broker, liquidity_intel=ReducingLiquidity(),
        risk_limits=risk_limits, cost_rate=0.0,
    )

    engine.process_candle(candle(1, 100.0))

    assert state["placed"] == 0
    assert risk_limits.calls == 0
    liquidity_events = [event for event in audit_repository.list_system_events(limit=100)
                        if event["kind"] == "LIQUIDITY_REJECT"]
    assert liquidity_events
    assert liquidity_events[0]["payload"].get("reduction_requested") is True


def test_min_liquidity_setting_controls_reject_floor():
    intelligence = LiquidityIntelligence.from_settings(
        Settings(min_liquidity_score=0.5))
    snapshot = LiquiditySnapshot(
        symbol="RELIANCE", timestamp=time.time(), bid=99.9, ask=100.1,
        bid_qty=500.0, ask_qty=500.0, last_price=100.0,
        volume=1000.0, spread=0.2, spread_bps=20.0, imbalance=0.0,
        liquidity_score=0.4, execution_quality="moderate", stale=False,
        reasons=[],
    )

    rejected, reduce, _reason = intelligence.should_reduce_or_reject(snapshot)

    assert intelligence.min_liquidity_score == 0.5
    assert rejected is True
    assert reduce is False


class GoodLiquidity(LiquidityIntelligence):
    def assess(self, symbol, **_kwargs):
        return LiquiditySnapshot(
            symbol=symbol, timestamp=time.time(), bid=99.99, ask=100.01,
            bid_qty=5000.0, ask_qty=5000.0, last_price=100.0,
            volume=5000.0, spread=0.02, spread_bps=2.0, imbalance=0.0,
            liquidity_score=0.9, execution_quality="good", stale=False,
            reasons=[],
        )

    def should_reduce_or_reject(self, _snapshot):
        return False, False, "ok"


class CapturingQuality:
    def __init__(self):
        self.features = None

    def score(self, **features):
        self.features = features
        return QualityResult(quality_score=0.0, confidence=0.0)

    def passes(self, _result):
        return False


class CapturingSizer:
    def __init__(self, delegate):
        self.delegate = delegate
        self.lookback = delegate.lookback
        self.context = None

    def adjust(self, context):
        self.context = context
        return self.delegate.adjust(context)


def test_configured_volatility_lookback_is_used_by_engine(audit_repository):
    state = broker_state()
    broker = make_broker(state, audit_repository)
    settings = Settings(volatility_lookback=3)
    engine = make_engine(audit_repository, broker, settings=settings,
                         liquidity_intel=GoodLiquidity(), cost_rate=0.0)
    capture = CapturingSizer(engine.volatility_sizer)
    engine.volatility_sizer = capture
    engine._history = [candle(1, 100.0), candle(2, 101.0), candle(3, 99.0)]

    engine.process_candle(candle(4, 102.0))

    assert capture.lookback == 3
    assert capture.context is not None
    assert capture.context.volatility is not None


def test_atr_uses_each_recent_candle_previous_close(audit_repository):
    from app.data.models import Candle

    state = broker_state()
    broker = make_broker(state, audit_repository)
    engine = make_engine(audit_repository, broker,
                         liquidity_intel=GoodLiquidity(), cost_rate=0.0)
    capture = CapturingSizer(engine.volatility_sizer)
    engine.volatility_sizer = capture
    engine._history = [candle(i + 1, 100.0 if i < 12 else 200.0)
                       for i in range(20)]
    current = candle(21, 200.0)
    expected_history = [*engine._history, current]
    recent = expected_history[-10:]
    true_ranges = [
        max(float(bar.high) - float(bar.low),
            abs(float(bar.high) - float(previous.close)),
            abs(float(bar.low) - float(previous.close)))
        for previous, bar in zip(recent, recent[1:])
    ]
    expected_atr = sum(true_ranges) / len(true_ranges)

    engine.process_candle(current)

    assert isinstance(current, Candle)
    assert capture.context is not None
    assert capture.context.atr == pytest.approx(expected_atr)


def test_engine_net_edge_rejects_confidence_without_proposal_sized_pnl(
        audit_repository):
    state = broker_state()
    broker = make_broker(state, audit_repository)
    engine = make_engine(
        audit_repository, broker, net_edge_gate=NetEdgeGate(),
        liquidity_intel=GoodLiquidity(), cost_rate=0.0,
    )
    engine.last_router_decision = {"confidence": 0.99, "selected": True}

    engine.process_candle(candle(1, 100.0))

    assert state["placed"] == 0
    rejected = [event for event in audit_repository.list_system_events(limit=100)
                if event["kind"] == "NET_EDGE_REJECT"]
    assert rejected
    assert "missing gross_expected" in rejected[0]["payload"]["reason"]


def test_quality_features_do_not_claim_unavailable_positive_evidence(
        audit_repository):
    audit_repository.set_engine_state(
        "capital_allocation", {"allocations": {"momentum": 50000.0}})
    state = broker_state()
    broker = make_broker(state, audit_repository)
    scorer = CapturingQuality()
    allocator = SimpleNamespace(repository=audit_repository)
    engine = make_engine(
        audit_repository, broker, allocator=allocator,
        event_engine=EventDrivenDecisionEngine(),
        liquidity_intel=GoodLiquidity(), quality_scorer=scorer,
        cost_rate=0.0,
    )

    engine.process_candle(candle(1, 100.0))

    assert state["placed"] == 0
    assert scorer.features is not None
    assert scorer.features["expected_net_edge"] is None
    assert scorer.features["strategy_confidence"] is None
    assert scorer.features["recent_oos_expectancy"] is None
    assert scorer.features["event_context"] is None
    assert scorer.features["regime_compatibility"] is None
    assert scorer.features["market_context"] is None
    assert scorer.features["portfolio_correlation"] is None
    assert scorer.features["trend_alignment"] is None
    assert scorer.features["volatility_regime"] is None
    assert scorer.features["signal_age"] is None
    assert scorer.features["alpha_decay_factor"] is None
    assert scorer.features["reward_risk"] is None
    assert scorer.features["stop_distance"] is None
    assert scorer.features["spread_bps"] == pytest.approx(
        engine._last_liquidity.spread_bps)


def test_cross_market_cache_does_not_invent_neutral_or_stale_context():
    cache = CrossMarketCache()

    empty = cache.context_for()
    assert empty.market_direction is None
    assert empty.volatility_context is None

    cache.update("NIFTY", {"close": 100.0}, ts=time.time())
    cache.update("INDIA_VIX", {}, ts=time.time())
    cache.update("CRUDE", {}, ts=time.time())
    cache.update("GOLD", {}, ts=time.time())
    cache.update("USDINR", {}, ts=time.time())
    missing = cache.context_for(sector="BANK")
    assert missing.market_direction is None
    assert missing.sector_direction is None
    assert missing.volatility_context is None
    assert missing.commodity_context is None
    assert missing.currency_context is None

    cache.update("NIFTY", {"return": 0.0, "volatility": 0.01}, ts=time.time())
    explicit = cache.context_for()
    assert explicit.market_direction == "flat"
    assert explicit.volatility_context == "low"

    stale_cache = CrossMarketCache(ttl=1)
    stale_cache.update("NIFTY", {"return": 0.05}, ts=time.time() - 5)
    stale = stale_cache.context_for()
    assert stale.market_direction is None
    assert stale.volatility_context is None


@pytest.mark.parametrize("key, data", [
    ("NIFTY", {"close": 100.0}),
    ("INDIA_VIX", {}),
])
def test_cross_market_quality_context_requires_explicit_current_evidence(
        audit_repository, key, data):
    state = broker_state()
    broker = make_broker(state, audit_repository)
    cache = CrossMarketCache()
    cache.update(key, data, ts=time.time())
    scorer = CapturingQuality()
    engine = make_engine(
        audit_repository, broker, cross_market_cache=cache,
        liquidity_intel=GoodLiquidity(), quality_scorer=scorer,
        event_engine=EventDrivenDecisionEngine(), cost_rate=0.0,
    )

    engine.process_candle(candle(1, 100.0))

    assert state["placed"] == 0
    assert scorer.features is not None
    assert scorer.features["market_context"] is None
    assert scorer.features["volatility_regime"] is None


def test_liquidity_cache_does_not_reuse_snapshot_for_changed_quote():
    intelligence = LiquidityIntelligence(cache_ttl=10.0)
    quote_time = time.time()
    first = intelligence.assess(
        "RELIANCE", bid=99.0, ask=101.0, last_price=100.0,
        volume=5000.0, timestamp=quote_time)
    changed = intelligence.assess(
        "RELIANCE", bid=95.0, ask=105.0, last_price=100.0,
        volume=5000.0, timestamp=quote_time)

    assert changed is not first
    assert changed.spread_bps != first.spread_bps
