"""3.0.1 adversarial regression: instrument routing, sizing vs risk order,
session-aware daily limits. Every test targets a CONFIRMED audit defect."""

import json

import httpx
import pytest

from app.brokers.sandbox import UpstoxSandboxBroker
from app.config.settings import Settings
from app.data.models import Candle
from app.database.database import Database
from app.database.repository import Repository
from app.engine.sandbox_engine import SandboxEngine
from app.learning.journal import LearningJournal
from app.risk.limits import HardRiskLimits, OrderIntent
from app.strategies.base import Signal

RELIANCE_KEY = "NSE_EQ|INE002A01018"
TCS_KEY = "NSE_EQ|TCS"


def candle(minute, close=100.0, symbol="RELIANCE", volume=1000):
    from datetime import datetime, timezone

    return Candle(datetime(2026, 1, 2, 9, minute, tzinfo=timezone.utc),
                  close * 0.999, close * 1.001, close * 0.998, close,
                  volume, symbol)


def make_handler(state):
    def handler(request):
        if state.get("auth_mode") != "ok":
            return httpx.Response(401, json={"errors": [{"errorCode": "UDAPI1052"}]})
        if request.method == "POST":
            state["placed"] += 1
            state.setdefault("bodies", []).append(request.content)
            oid = str(state["next"])
            state["next"] += 1
            state["orders"][oid] = {"status": "open pending", "filled": 0,
                                    "avg": 0.0}
            return httpx.Response(200, json={"status": "success",
                                             "data": {"order_ids": [oid]}})
        if request.method == "DELETE":
            oid = request.url.params["order_id"]
            if oid in state["orders"]:
                state["orders"][oid]["status"] = "cancelled"
            return httpx.Response(200, json={"status": "success"})
        oid = request.url.params.get("order_id", "")
        rec = state["orders"].get(oid)
        if rec is None:
            return httpx.Response(400, json={"errors": [
                {"errorCode": "UDAPI100010", "message": "order not found"}]})
        return httpx.Response(200, json={"status": "success", "data": [
            {"status": rec["status"], "filled_quantity": rec["filled"],
             "average_price": rec["avg"], "order_timestamp": "t",
             "status_message": ""}]})
    return handler


def make_broker(state, repository=None, key=RELIANCE_KEY, keys=None):
    broker = UpstoxSandboxBroker(
        "tok", key, transport=httpx.MockTransport(make_handler(state)),
        repository=repository, initial_capital=100000.0)
    # 3.0.2: explicit configured identity; the broker default key alone no
    # longer implicitly maps arbitrary symbols
    broker.instrument_map.update(
        keys if keys is not None else
        {key.split("|")[-1]: key, "RELIANCE": key})
    return broker


class FixedSignal:
    def __init__(self, signal=Signal.BUY, name="momentum",
                 parameters=None):
        self.signal = signal
        self.name = name
        self.parameters = parameters or {"period": 2, "threshold": 0.0}
        self.history_limit = 30

    def generate_signal(self, candles):
        return self.signal


class _P:
    symbols = ["RELIANCE"]

    def metrics(self):
        return {"market_open": True, "stale_feed": False}


def make_engine(repository, broker, *, strategy=None, **kwargs):
    defaults = dict(
        provider=_P(), strategy=strategy or FixedSignal(), broker=broker,
        repository=repository, risk_limits=HardRiskLimits.from_settings(Settings()),
        journal=LearningJournal(repository), timeframe="1m", evaluate_every=5,
        # 3.0.2: explicit configured identity (the broker-default fallback is
        # gone; tests configure the symbol→key map explicitly)
        instrument_keys={"RELIANCE": RELIANCE_KEY})
    defaults.update(kwargs)
    return SandboxEngine(**defaults)


@pytest.fixture()
def env(tmp_path):
    database = Database(f"sqlite:///{tmp_path}/v301.db")
    repository = Repository(database)
    yield repository
    database.close()


class TestInstrumentRouting:
    def test_mapped_symbol_routes_to_its_own_key(self, env):
        state = {"placed": 0, "orders": {}, "next": 1001, "auth_mode": "ok"}
        broker = make_broker(state, env, keys={"TCS": TCS_KEY,
                                               "RELIANCE": RELIANCE_KEY})
        broker.submit("BUY", 5, 100.0, intent_id="tcs-1", symbol="TCS",
                      instrument_key=TCS_KEY)
        body = httpx.Request.content and None
        posted = state["bodies"][0]
        import json

        payload = json.loads(posted)
        assert payload["instrument_token"] == TCS_KEY, \
            "TCS order must carry the TCS instrument key"

    def test_ambiguous_identity_rejected_on_multi_instrument_broker(self, env):
        state = {"placed": 0, "orders": {}, "next": 1001, "auth_mode": "ok"}
        broker = make_broker(state, env, keys={"TCS": TCS_KEY,
                                               "RELIANCE": RELIANCE_KEY})
        with pytest.raises(ValueError):
            broker.submit("BUY", 5, 100.0, intent_id="x1", symbol="WIPRO")
        assert state["placed"] == 0

    def test_symbol_cannot_override_conflicting_explicit_key(self, env):
        state = {"placed": 0, "orders": {}, "next": 1001, "auth_mode": "ok"}
        broker = make_broker(state, env, keys={"TCS": TCS_KEY,
                                               "RELIANCE": RELIANCE_KEY})
        with pytest.raises(ValueError):
            broker.submit("BUY", 5, 100.0, intent_id="x2", symbol="TCS",
                          instrument_key=RELIANCE_KEY)
        assert state["placed"] == 0

    def test_unmapped_symbol_rejected_even_on_single_instrument_broker(self, env):
        """3.0.2: the single-instrument default is GONE — an unmapped symbol
        is rejected even when the broker has exactly one configured key."""
        state = {"placed": 0, "orders": {}, "next": 1001, "auth_mode": "ok"}
        broker = make_broker(state, env)
        with pytest.raises(ValueError):
            broker.submit("BUY", 5, 100.0, intent_id="legacy-1",
                          symbol="UNMAPPED")
        assert state["placed"] == 0

    def test_engine_routes_each_symbol_through_its_key(self, env):
        state = {"placed": 0, "orders": {}, "next": 1001, "auth_mode": "ok"}
        broker = make_broker(state, env, keys={"TCS": TCS_KEY,
                                               "RELIANCE": RELIANCE_KEY})
        engine = make_engine(env, broker, instrument_keys={
            "TCS": TCS_KEY, "RELIANCE": RELIANCE_KEY})
        engine.process_candle(candle(1, 100.0, symbol="TCS"))
        import json

        payload = json.loads(state["bodies"][0])
        assert payload["instrument_token"] == TCS_KEY
        records = env.list_sandbox_orders(limit=5)
        assert records[0]["symbol"] == "TCS"

    def test_fractional_quantity_rejected(self, env):
        state = {"placed": 0, "orders": {}, "next": 1001, "auth_mode": "ok"}
        broker = make_broker(state, env)
        with pytest.raises(ValueError):
            broker.submit("BUY", 5.5, 100.0, intent_id="frac-1",
                          symbol="RELIANCE")
        assert state["placed"] == 0


class TestSizingBeforeRisk:
    def test_exact_gated_quantity_reaches_broker(self, env):
        """The quantity that passes the hard gate IS the submitted quantity."""
        settings = Settings(max_position_value=0.0)  # unlimited position cap
        state = {"placed": 0, "orders": {}, "next": 1001, "auth_mode": "ok"}
        broker = make_broker(state, env)
        engine = make_engine(env, broker,
                             risk_limits=HardRiskLimits.from_settings(settings),
                             sizing_fraction=0.5, cost_rate=0.001)
        engine.process_candle(candle(1, 100.0))
        import json

        payload = json.loads(state["bodies"][0])
        # budget = min(cash 100k, equity*0.5=50k) → qty = floor(50000/100.1)=499
        assert payload["quantity"] == 499
        decisions = [e for e in env.list_system_events(limit=50)
                     if e["kind"] == "RISK_DECISION"]
        assert decisions and decisions[0]["payload"]["quantity"] == 499
        assert decisions[0]["payload"]["allowed"] is True

    def test_oversized_position_rejected_by_gate(self, env):
        """Sizing alone cannot bypass the cap: the gate rejects the intent."""
        settings = Settings(max_position_value=5000.0)
        state = {"placed": 0, "orders": {}, "next": 1001, "auth_mode": "ok"}
        broker = make_broker(state, env)
        engine = make_engine(env, broker,
                             risk_limits=HardRiskLimits.from_settings(settings),
                             sizing_fraction=1.0, cost_rate=0.0)
        engine.process_candle(candle(1, 100.0))  # wants 1000 shares = 100k
        kinds = [e["kind"] for e in env.list_system_events(limit=50)]
        assert "RISK_REJECT" in kinds
        assert state["placed"] == 0, "oversized intent must never reach the broker"

    def test_fees_aware_sizing_keeps_exposure_under_cap(self, env):
        settings = Settings(max_position_value=100000.0)
        state = {"placed": 0, "orders": {}, "next": 1001, "auth_mode": "ok"}
        broker = make_broker(state, env)
        engine = make_engine(env, broker,
                             risk_limits=HardRiskLimits.from_settings(settings),
                             sizing_fraction=1.0, cost_rate=0.01)
        engine.process_candle(candle(1, 100.0))
        import json

        payload = json.loads(state["bodies"][0])
        quantity = payload["quantity"]
        assert quantity == 990  # floor(100000 / (100*1.01))
        notional = quantity * 100.0
        assert notional + quantity * 100.0 * 0.01 <= 100000.0 + 1e-6, \
            "all-in exposure (incl. fees) must respect the cap"


def state_next(state):
    return str(state["next"] - 1)


class TestDailyRiskAccounting:
    def test_daily_accumulation_and_limits(self, env):
        from app.engine.state import DailyRiskAccount

        account = DailyRiskAccount(env)
        account.register_trade()
        account.register_trade()
        account.add_realized(-300.0)
        account.add_realized(-250.0)
        state = account.state()
        assert state["trades"] == 2
        assert state["realized_pnl"] == pytest.approx(-550.0)
        limits = HardRiskLimits(max_daily_loss=500.0, max_trades_per_day=3)
        decision = limits.check(
            OrderIntent("X", "BUY", 1, 100.0), open_positions=0,
            open_position_value=0.0, trades_today=account.trades,
            realized_today=account.realized_pnl)
        assert decision.allowed is False
        assert decision.reason == "max_daily_loss_reached"

    def test_rollover_resets_counters_and_keeps_history(self, env):
        from datetime import datetime, timezone

        from app.engine.state import DailyRiskAccount

        account = DailyRiskAccount(env)
        day1 = datetime(2026, 1, 2, 18, 30, tzinfo=timezone.utc)  # 2 Jan IST
        account.rollover(now=day1)
        account.register_trade(now=day1)
        account.add_realized(-100.0, now=day1)
        day2 = datetime(2026, 1, 5, 4, 0, tzinfo=timezone.utc)  # 5 Jan IST
        rolled = account.rollover(now=day2)
        assert rolled is True
        assert account.state() == {"date": "2026-01-05", "trades": 0,
                                   "realized_pnl": 0.0}
        kinds = [e["kind"] for e in env.list_system_events(limit=50)]
        assert "DAILY_STATE_ROLLOVER" in kinds

    def test_midnight_boundary_in_market_timezone(self, env):
        from datetime import datetime, timezone

        from app.engine.state import DailyRiskAccount

        account = DailyRiskAccount(env)
        # 18:59 UTC == 00:29 IST on 3 Jan; 19:01 UTC == 00:31 IST on 3 Jan
        before = datetime(2026, 1, 2, 18, 59, tzinfo=timezone.utc)
        account.rollover(now=before)
        account.register_trade(now=before)
        just_after = datetime(2026, 1, 2, 19, 1, tzinfo=timezone.utc)
        assert account.rollover(now=just_after) is False
        assert account.trades == 1  # same IST trading day → counters kept
        assert account.state()["date"] == "2026-01-03"
        # crossing into the next IST morning resets
        next_morning = datetime(2026, 1, 5, 4, 0, tzinfo=timezone.utc)
        assert account.rollover(now=next_morning) is True
        assert account.trades == 0

    def test_same_day_restart_preserves_totals(self, env):
        from datetime import datetime, timezone

        from app.engine.state import DailyRiskAccount

        now = datetime.now(timezone.utc)  # same real trading day
        first = DailyRiskAccount(env)
        first.rollover(now=now)
        first.register_trade(now=now)
        first.register_trade(now=now)
        first.add_realized(-120.5, now=now)
        # "restart": a brand-new account over the same repository, same day
        second = DailyRiskAccount(env)
        assert second.state() == first.state()
        assert second.trades == 2
        assert second.realized_pnl == pytest.approx(-120.5)

    def test_engine_daily_state_exposed_and_persisted(self, env):
        settings = Settings()
        state = {"placed": 0, "orders": {}, "next": 1001, "auth_mode": "ok"}
        broker = make_broker(state, env)
        engine = make_engine(env, broker, risk_limits=HardRiskLimits.from_settings(settings))
        engine.process_candle(candle(1, 100.0))
        assert engine.daily.trades == 1
        restored = env.get_engine_state("daily_risk_state")
        assert restored["trades"] == 1


class TestRestartRecovery:
    def test_ledger_rebuilt_from_recorded_fills(self, env):
        """Restart reconstructs cash/position/realized exactly (defect 5)."""
        state = {"placed": 0, "orders": {}, "next": 1001, "auth_mode": "ok"}
        broker = make_broker(state, env)
        engine = make_engine(env, broker)
        engine.process_candle(candle(1, 100.0))
        state["orders"][state_next(state)] = {"status": "complete",
                                              "filled": 10, "avg": 100.0}
        engine.reconcile()  # BUY 10 @100 fills → cash down, position 10
        ledger_after_fill = broker.ledger.snapshot()
        assert ledger_after_fill["position_quantity"] == 10

        # "restart": brand-new broker+engine over the same repository
        state2 = {"placed": 0, "orders": {}, "next": 2001, "auth_mode": "ok"}
        state2["orders"]["1"] = {"status": "complete", "filled": 10, "avg": 100.0}
        broker2 = make_broker(state2, env)
        engine2 = make_engine(env, broker2)
        recovered = broker2.ledger.snapshot()
        assert recovered["cash"] == pytest.approx(ledger_after_fill["cash"])
        assert recovered["position_quantity"] == 10
        assert recovered["position_avg_price"] == 100.0
        assert engine2._recovery_report["orders_replayed"] == 1
        # and the recovered engine trades on correct state
        engine2.process_candle(candle(3, 110.0, volume=1000))
        assert broker2.ledger.mark == 110.0

    def test_round_trip_realized_matches_original(self, env):
        state = {"placed": 0, "orders": {}, "next": 1001, "auth_mode": "ok"}
        broker = make_broker(state, env)
        engine = make_engine(env, broker)
        engine.process_candle(candle(1, 100.0))
        state["orders"][state_next(state)] = {"status": "complete",
                                              "filled": 10, "avg": 100.0}
        engine.reconcile()
        # exit the full position at 105
        broker.submit("SELL", 10, 105.0, intent_id="exit-1", symbol="RELIANCE")
        state["orders"][state_next(state)] = {"status": "complete",
                                              "filled": 10, "avg": 105.0}
        engine.reconcile()
        original = broker.ledger.snapshot()
        assert original["realized_pnl"] == pytest.approx((105 - 100) * 10)

        state2 = {"placed": 0, "orders": {}, "next": 3001, "auth_mode": "ok"}
        state2["orders"]["1"] = {"status": "complete", "filled": 10, "avg": 100.0}
        state2["orders"]["2"] = {"status": "complete", "filled": 10, "avg": 105.0}
        broker2 = make_broker(state2, env)
        engine2 = make_engine(env, broker2)
        recovered = broker2.ledger.snapshot()
        assert recovered["realized_pnl"] == pytest.approx(original["realized_pnl"])
        assert recovered["cash"] == pytest.approx(original["cash"])
        assert recovered["position_quantity"] == 0

    def test_daily_totals_and_emergency_stop_survive_restart(self, env):
        settings = Settings(max_position_value=0.0)
        state = {"placed": 0, "orders": {}, "next": 1001, "auth_mode": "ok"}
        broker = make_broker(state, env)
        engine = make_engine(env, broker,
                             risk_limits=HardRiskLimits.from_settings(settings))
        engine.process_candle(candle(1, 100.0))
        assert engine.daily.trades == 1
        engine.emergency_stop("test")
        state2 = {"placed": 0, "orders": {}, "next": 4001, "auth_mode": "ok"}
        broker2 = make_broker(state2, env)
        engine2 = make_engine(env, broker2,
                              risk_limits=HardRiskLimits.from_settings(settings))
        assert engine2.daily.trades == 1
        assert engine2._check_emergency() is True
        # and while stopped, nothing can be submitted
        engine2.process_candle(candle(2, 100.0))
        assert state2["placed"] == 0

    def test_unresolved_intents_surfaced_never_resubmitted(self, env):
        from app.engine.state import SubmissionJournal, SUBMISSION_ATTEMPTED

        journal = SubmissionJournal(env)
        journal.record("crash-intent", instrument_key=RELIANCE_KEY,
                       symbol="RELIANCE", side="BUY", quantity=10, price=100.0,
                       strategy="momentum")
        journal.mark("crash-intent", SUBMISSION_ATTEMPTED)
        state = {"placed": 0, "orders": {}, "next": 5001, "auth_mode": "ok"}
        broker = make_broker(state, env)
        engine = make_engine(env, broker)
        kinds = [e["kind"] for e in env.list_system_events(limit=50)]
        assert "RECONCILE_REQUIRED" in kinds
        assert engine.status()["unresolved_intents"] == 1
        # the same intent can never be submitted again
        assert journal.resubmission_forbidden("crash-intent") is True
        assert state["placed"] == 0


class TestCrashSafeSubmission:
    def test_provider_5xx_marks_unknown_and_never_retries_post(self, env):
        def handler(request):
            if request.method == "POST":
                return httpx.Response(500, json={"errors": [{"errorCode": "X"}]})
            return httpx.Response(200, json={"status": "success", "data": []})

        broker = UpstoxSandboxBroker(
            "tok", RELIANCE_KEY, transport=httpx.MockTransport(handler),
            repository=env, initial_capital=100000.0)
        from app.brokers.sandbox import SandboxRequestError

        with pytest.raises(SandboxRequestError):
            broker.submit("BUY", 5, 100.0, intent_id="u1", symbol="RELIANCE", instrument_key=RELIANCE_KEY)
        from app.engine.state import UNKNOWN

        assert broker._journal.get("u1")["state"] == UNKNOWN
        # restart: unknown intent blocks resubmission (refused WITHOUT a POST)
        state = {"placed": 0, "orders": {}, "next": 6001, "auth_mode": "ok"}
        broker2 = make_broker(state, env)
        make_engine(env, broker2)
        result = broker2.submit("BUY", 5, 100.0, intent_id="u1", symbol="RELIANCE")
        assert result == "u1"
        assert state["placed"] == 0, "UNKNOWN intent must never be re-POSTed"

    def test_network_error_marks_unknown(self, env):
        def handler(request):
            raise httpx.ConnectError("boom")

        broker = UpstoxSandboxBroker(
            "tok", RELIANCE_KEY, transport=httpx.MockTransport(handler),
            repository=env, initial_capital=100000.0)
        from app.brokers.sandbox import SandboxRequestError

        with pytest.raises(SandboxRequestError):
            broker.submit("BUY", 5, 100.0, intent_id="n1", symbol="RELIANCE", instrument_key=RELIANCE_KEY)
        from app.engine.state import UNKNOWN

        assert broker._journal.get("n1")["state"] == UNKNOWN

    def test_journal_records_full_state_machine(self, env):
        state = {"placed": 0, "orders": {}, "next": 7001, "auth_mode": "ok"}
        broker = make_broker(state, env)
        broker.submit("BUY", 5, 100.0, intent_id="sm-1", symbol="RELIANCE", instrument_key=RELIANCE_KEY)
        entry = broker._journal.get("sm-1")
        assert entry["state"] == "ACKNOWLEDGED"
        assert entry["instrument_key"] == RELIANCE_KEY
        assert entry["upstox_order_ids"]
        engine = make_engine(env, broker)
        engine.reconcile()  # no fill recorded yet → stays ACKNOWLEDGED
        # simulate a fill and reconcile to completion
        state["orders"][state_next(state)] = {"status": "complete",
                                              "filled": 5, "avg": 100.0}
        fills = engine.reconcile()
        assert fills and broker._journal.get("sm-1")["state"] == "FINAL_STATE"


class TestProtectiveExits:
    def _engine_with_position(self, env, *, stop=0.05, take=0.10):
        settings = Settings(max_position_value=0.0)
        state = {"placed": 0, "orders": {}, "next": 8001, "auth_mode": "ok"}
        broker = make_broker(state, env)
        engine = make_engine(env, broker,
                             risk_limits=HardRiskLimits.from_settings(settings),
                             stop_loss=stop, take_profit=take,
                             strategy=FixedSignal(Signal.HOLD))
        engine.process_candle(candle(1, 100.0))
        assert state["placed"] == 0  # HOLD strategy never enters
        # fabricate a filled 10-share position @100 via a recorded order+fill
        broker.submit("BUY", 10, 100.0, intent_id="pos-1", symbol="RELIANCE")
        baseline = state["placed"]  # includes the setup order itself
        state["orders"][state_next(state)] = {"status": "complete",
                                              "filled": 10, "avg": 100.0}
        engine.reconcile()
        broker.ledger.mark = 100.0
        return engine, broker, state, baseline

    def test_stop_loss_triggers_independent_of_strategy(self, env):
        engine, broker, state, baseline = self._engine_with_position(env)
        engine.process_candle(candle(2, 94.0))  # ≤ 100 × 0.95
        assert state["placed"] == baseline + 1, \
            "stop-loss must fire without a strategy SELL"
        decisions = [e for e in env.list_system_events(limit=50)
                     if e["kind"] == "DECISION"]
        assert decisions and decisions[0]["payload"]["reason"] == "PROTECTIVE_STOP_LOSS"

    def test_take_profit_triggers(self, env):
        engine, broker, state, baseline = self._engine_with_position(env)
        engine.process_candle(candle(2, 111.0))  # ≥ 100 × 1.10
        assert state["placed"] == baseline + 1
        decisions = [e for e in env.list_system_events(limit=50)
                     if e["kind"] == "DECISION"]
        assert decisions[0]["payload"]["reason"] == "PROTECTIVE_TAKE_PROFIT"

    def test_no_exit_inside_band_and_no_duplicates(self, env):
        engine, broker, state, baseline = self._engine_with_position(env)
        engine.process_candle(candle(2, 100.0))  # inside band
        assert state["placed"] == baseline
        engine.process_candle(candle(3, 94.0))   # exit fires
        assert state["placed"] == baseline + 1
        engine.process_candle(candle(4, 90.0))   # same stop again → no dup order
        assert state["placed"] == baseline + 1

    def test_exit_survives_restart(self, env):
        engine, broker, state, baseline = self._engine_with_position(env)
        # restart with the position recovered from recorded fills
        state2 = {"placed": 0, "orders": {}, "next": 9001, "auth_mode": "ok"}
        state2["orders"]["1"] = {"status": "complete", "filled": 10, "avg": 100.0}
        broker2 = make_broker(state2, env)
        engine2 = make_engine(env, broker2, stop_loss=0.05, take_profit=0.10,
                              strategy=FixedSignal(Signal.HOLD))
        assert broker2.ledger.position_quantity == 10  # recovered
        engine2.process_candle(candle(2, 94.0))
        assert state2["placed"] == 1, "exit must work off recovered state"

    def test_emergency_stop_never_traps_a_held_position(self, env):
        # FINAL-11/10 §12: the emergency stop halts NEW RISK, but it must
        # never hold a position hostage — the protective exit still fires.
        engine, broker, state, baseline = self._engine_with_position(env)
        engine.emergency_stop("test")
        engine.process_candle(candle(2, 94.0))
        assert state["placed"] == baseline + 1, \
            "a risk-reducing stop-loss must escape the emergency stop"
        allowed = [e for e in env.list_system_events(limit=100)
                   if e["kind"] == "EXECUTION_STOPPED_EXIT_ALLOWED"]
        assert allowed, "the permitted exit must be audited"

    def test_emergency_stop_still_blocks_new_entries(self, env):
        state = {"placed": 0, "orders": {}, "next": 8101, "auth_mode": "ok"}
        broker = make_broker(state, env)
        engine = make_engine(env, broker, strategy=FixedSignal(Signal.BUY))
        engine.emergency_stop("test")
        engine.process_candle(candle(1, 100.0))
        assert state["placed"] == 0, "new risk stays blocked under STOP"
        skips = [e for e in env.list_system_events(limit=100)
                 if e["kind"] == "EXECUTION_STOPPED_SKIP"]
        assert skips, "the blocked entry must be audited"


class TestEventDrivenDecisions:
    def test_events_recorded_and_gate_evaluation(self, env):
        from app.engine.events import EventDrivenDecisionEngine

        events = EventDrivenDecisionEngine(move_threshold=0.003)
        events.register_cadence("momentum", 3)  # debounce: every 3rd candle
        state = {"placed": 0, "orders": {}, "next": 10001, "auth_mode": "ok"}
        broker = make_broker(state, env)
        engine = make_engine(env, broker, event_engine=events,
                             strategy=FixedSignal(Signal.BUY))
        engine.process_candle(candle(1, 100.0))  # cadence candle 1 (1 % 3 ≠ 0)
        assert state["placed"] == 0, "non-cadence candle without events → no trade"
        engine.process_candle(candle(2, 103.0))  # significant move fires
        assert state["placed"] == 1, "significant_move must drive a decision"
        recorded = [e for e in env.list_system_events(limit=50)
                    if e["kind"] == "DECISION"]
        assert recorded and "significant_move" in recorded[0]["payload"]["triggers"]

    def test_pause_blocks_entries_not_exits(self, env):
        from app.engine.events import PauseGate

        gate = PauseGate(env)
        settings = Settings(max_position_value=0.0)
        state = {"placed": 0, "orders": {}, "next": 11001, "auth_mode": "ok"}
        broker = make_broker(state, env)
        engine = make_engine(env, broker,
                             risk_limits=HardRiskLimits.from_settings(settings),
                             pause_gate=gate, stop_loss=0.05, take_profit=0.0,
                             strategy=FixedSignal(Signal.BUY))
        engine.process_candle(candle(1, 100.0))  # entry
        assert state["placed"] == 1
        gate.pause("execution", "anomaly test")
        engine.process_candle(candle(2, 100.0))  # entry attempt while paused
        assert state["placed"] == 1, "paused entries blocked"
        # position via recorded fill, then a stop-loss exit must still fire
        broker.submit("BUY", 5, 100.0, intent_id="p1", symbol="RELIANCE")
        baseline = state["placed"]
        state["orders"][state_next(state)] = {"status": "complete",
                                              "filled": 5, "avg": 100.0}
        engine.reconcile()
        engine.process_candle(candle(3, 90.0))
        assert state["placed"] == baseline + 1, "risk-reducing exits bypass the pause"


class TestProbeClassifier:
    """UDAPI100010 probe classification (tautology removed)."""

    def _response(self, status, body):
        return httpx.Response(status, json=body)

    def test_documented_not_found_is_expected(self):
        from app.brokers.sandbox import classify_history_probe

        for status in (400, 404):
            verdict = classify_history_probe(self._response(status, {
                "errors": [{"errorCode": "UDAPI100010", "message": "not found"}]}))
            assert verdict["credential_accepted"] is True
            assert verdict["class"] == "documented_not_found"

    def test_unrelated_400_is_not_expected(self):
        from app.brokers.sandbox import classify_history_probe

        verdict = classify_history_probe(self._response(400, {
            "errors": [{"errorCode": "UDAPI11002", "message": "insufficient margin"}]}))
        assert verdict["credential_accepted"] is False
        assert verdict["class"] == "unexpected"

    def test_unrelated_404_is_not_expected(self):
        from app.brokers.sandbox import classify_history_probe

        verdict = classify_history_probe(self._response(404, {
            "errors": [{"errorCode": "UDAPI99999", "message": "route missing"}]}))
        assert verdict["credential_accepted"] is False

    def test_auth_401_403(self):
        from app.brokers.sandbox import classify_history_probe

        for status in (401, 403):
            verdict = classify_history_probe(self._response(status, {}))
            assert verdict["credential_accepted"] is False
            assert verdict["class"] == "auth_rejected"

    def test_rate_limited_429(self):
        from app.brokers.sandbox import classify_history_probe

        verdict = classify_history_probe(self._response(429, {}))
        assert verdict["credential_accepted"] is False
        assert verdict["class"] == "rate_limited"

    def test_provider_5xx(self):
        from app.brokers.sandbox import classify_history_probe

        for status in (500, 502, 503):
            verdict = classify_history_probe(self._response(status, {}))
            assert verdict["credential_accepted"] is False
            assert verdict["class"] == "provider_error"

    def test_malformed_body_is_unexpected(self):
        class FakeResponse:
            status_code = 400

            def json(self):
                raise ValueError("not json")

        from app.brokers.sandbox import classify_history_probe

        verdict = classify_history_probe(FakeResponse())
        assert verdict["credential_accepted"] is False
        assert verdict["class"] == "unexpected"

    def test_network_timeout_fails_closed(self, env):
        def handler(request):
            raise httpx.ConnectTimeout("timeout")

        broker = UpstoxSandboxBroker(
            "tok", RELIANCE_KEY, transport=httpx.MockTransport(handler),
            repository=env, initial_capital=100000.0)
        from app.brokers.sandbox import SandboxRequestError

        with pytest.raises(SandboxRequestError):
            broker._request("GET", broker.history_endpoint,
                            params={"order_id": "0"})


def _approve(env, strategy="momentum", parameters=None):
    """Record the COMPLETE research ladder for a candidate version (3.0.2)."""
    from app.engine.adaptive import RouterStrategySource
    from app.learning.lifecycle import StrategyLifecycleManager

    parameters = parameters or {"period": 3, "threshold": 0.0}
    source = RouterStrategySource(env)
    lineage = source.lineage_for(strategy, parameters)
    env.record_lineage(lineage, strategy, parameters, 0, None)
    manager = StrategyLifecycleManager(env)
    for stage in ("RESEARCH", "VALIDATION", "WALK_FORWARD", "APPROVED"):
        env.record_lifecycle(lineage, stage, "ACTIVE", {"why": "test ladder"})
    return lineage


class TestRouterControlsExecution:
    def _promote(self, repository, strategy="momentum", parameters=None,
                 expectancy=0.05, trades=9):
        parameters = parameters or {"period": 3, "threshold": 0.0}
        repository.record_candidate(
            "cand-router-1", experiment_id=None, strategy=strategy,
            parameters=parameters, dataset_id=None,
            metrics={"max_drawdown": 0.1},
            oos={"oos_expectancy": expectancy, "oos_trades": trades},
            robustness={"score": 0.8}, promoted=True, reason="test evidence")

    def test_selected_candidate_drives_orders_and_is_versioned(self, env):
        from app.engine.adaptive import RouterStrategySource
        from app.learning.calibration import ConfidenceCalibration

        self._promote(env, strategy="momentum", parameters={"period": 3,
                                                            "threshold": 0.0})
        _approve(env, strategy="momentum",
                 parameters={"period": 3, "threshold": 0.0})
        settings = Settings(max_position_value=0.0)
        state = {"placed": 0, "orders": {}, "next": 12001, "auth_mode": "ok"}
        broker = make_broker(state, env)
        source = RouterStrategySource(env, calibration=ConfidenceCalibration(env))
        engine = make_engine(env, broker,
                             risk_limits=HardRiskLimits.from_settings(settings),
                             strategy=FixedSignal(Signal.HOLD),
                             strategy_source=source)
        price = 100.0
        for i in range(2, 7):
            price *= 1.004
            engine.process_candle(candle(i, price))
        active = env.get_engine_state("active_strategy")
        assert active["strategy"] == "momentum" and active["parameters"]["period"] == 3
        swaps = [e for e in env.list_system_events(limit=100)
                 if e["kind"] == "STRATEGY_SWAPPED"]
        assert swaps, "router-selected swap must be a versioned, recorded event"
        decisions = [e for e in env.list_learning_events(limit=100)
                     if e["kind"] == "ROUTER_DECISION"]
        assert decisions, "accepted router decisions must be persisted"
        payloads = [json.loads(d["payload"]) if isinstance(d["payload"], str)
                    else d["payload"] for d in decisions]
        assert any(pl.get("candidate_id") == "cand-router-1" for pl in payloads)
        assert state["placed"] >= 1, "the selected strategy must trade"
        payload = json.loads(state["bodies"][0])
        assert payload["instrument_token"] == RELIANCE_KEY
        decision_events = [e for e in env.list_system_events(limit=100)
                           if e["kind"] == "DECISION"]
        first = decision_events[-1]["payload"]  # events are newest-first
        assert first["strategy"] == "momentum"
        assert first["quantity"] == payload["quantity"]
        assert first["instrument_key"] == RELIANCE_KEY

    def test_negative_oos_candidate_never_tradable(self, env):
        from app.engine.adaptive import RouterStrategySource

        self._promote(env, expectancy=-0.4, trades=12)
        source = RouterStrategySource(env)
        decision, instance = source.select("trend_up_normal")
        assert decision["selected"] is False
        assert instance is None
        assert decision["strategy"] == source.fallback[0]

    def test_degraded_strategy_never_tradable(self, env):
        """3.0.2: PROBATION (and worse) fails the ACTIVE requirement — the
        router may still score it, but nothing executes."""
        from app.engine.adaptive import RouterStrategySource
        from app.learning.lifecycle import StrategyLifecycleManager

        self._promote(env, strategy="momentum", parameters={"period": 3,
                                                            "threshold": 0.0})
        lineage = _approve(env, strategy="momentum",
                           parameters={"period": 3, "threshold": 0.0})
        manager = StrategyLifecycleManager(env)
        healthy_source = RouterStrategySource(env)
        healthy, instance = healthy_source.select("trend_up_normal")
        assert healthy["selected"] is True and instance is not None
        manager.detect_degradation(lineage, [-1.0] * 10, baseline_expectancy=5.0)
        source = RouterStrategySource(env)
        decision, instance = source.select("trend_up_normal")
        assert decision["selected"] is False
        assert instance is None
        assert "lifecycle" in decision and decision["lifecycle"]["eligible"] is False


class TestLockedTestContamination:
    def test_locked_test_never_feeds_selection(self, env):
        """Promotion reads VALIDATION only; the locked test runs once, for the
        final report of the already-chosen winner."""
        from app.learning.experiments import ExperimentEngine
        from app.backtesting.costs import ExecutionConfig
        from app.backtesting.engine import BacktestEngine

        calls = []

        class SpyEngine(BacktestEngine):
            def run(self, candles, strategy, timeframe="1D", **kwargs):
                calls.append((kwargs.get("label"), len(candles)))
                return super().run(candles, strategy, timeframe, **kwargs)

        engine = ExperimentEngine(env, engine=SpyEngine(ExecutionConfig()))
        closes = []
        price = 100.0
        for i in range(240):
            price *= 1 + (0.004 if i % 10 < 5 else -0.003)
            closes.append(price)
        from tests.test_v2_datasets_research import candles_of

        candles = candles_of(closes)
        run = engine.run("grid", candles, "momentum",
                         [{"period": 2, "threshold": 0.0},
                          {"period": 4, "threshold": 0.0},
                          {"period": 6, "threshold": 0.0}], "1m")
        labels = [label for label, _ in calls]
        # selection phase: TRAINING + VALIDATION per candidate (no locked runs)
        selection_calls = len([l for l in labels if l in {"TRAINING", "VALIDATION"}])
        assert selection_calls >= 6
        locked_calls = [i for i, l in enumerate(labels) if l == "UNSEEN TEST"]
        assert len(locked_calls) <= 1, "locked test may run at most once"
        if locked_calls:
            assert locked_calls[0] == len(labels) - 1, "the locked test is always the FINAL run"
        for row in env.list_candidates(limit=10):
            oos = row["oos"] if isinstance(row["oos"], dict) else json.loads(row["oos"])
            if oos:
                assert oos.get("leg") == "validation", "selection evidence must be labeled validation"
        experiment = env.list_experiments(limit=1)[0]
        result = experiment["result"] if isinstance(experiment["result"], dict) else json.loads(experiment["result"])
        assert result["partitions"]["selection_metric"] == "validation_expectancy"
        if result.get("locked_test_report"):
            assert result["locked_test_report"]["leg"] == "locked_test_final_report_only"
        reports = [e for e in env.list_learning_events(limit=20)
                   if e["kind"] == "LOCKED_TEST_REPORT"]
        assert len(reports) <= 1


class TestCapitalAllocationInSizing:
    def test_allocator_plan_bounds_entry_size(self, env):
        plan = {"allocations": {"momentum": 5000.0},
                "hard_cap_per_strategy": 0.35}
        env.set_engine_state("capital_allocation", plan)
        settings = Settings(max_position_value=0.0)
        state = {"placed": 0, "orders": {}, "next": 13001, "auth_mode": "ok"}
        broker = make_broker(state, env)
        engine = make_engine(env, broker,
                             risk_limits=HardRiskLimits.from_settings(settings),
                             sizing_fraction=1.0, cost_rate=0.0)
        engine.process_candle(candle(1, 100.0))
        import json

        payload = json.loads(state["bodies"][0])
        assert payload["quantity"] == 50, "allocator allowance (5000 @ 100) must bound the final quantity"
        decisions = [e for e in env.list_system_events(limit=50)
                     if e["kind"] == "DECISION"]
        assert decisions[0]["payload"]["allocated_capital"] == 5000.0


class TestExecutionGraphEndToEnd:
    """Brief 17: machine-checkable proof that upstream-selected values
    (strategy version, quantity, instrument) reach the broker POST,
    reconciliation, ledger, attribution and P&L — nothing replaced en route."""

    def test_full_graph_strategy_to_attribution(self, env):
        from app.engine.adaptive import RouterStrategySource

        env.record_candidate(
            "cand-e2e-1", experiment_id=None, strategy="momentum",
            parameters={"period": 3, "threshold": 0.0}, dataset_id=None,
            metrics={"max_drawdown": 0.05},
            oos={"oos_expectancy": 0.08, "oos_trades": 9},
            robustness={"score": 0.9}, promoted=True, reason="e2e evidence")
        _approve(env, strategy="momentum",
                 parameters={"period": 3, "threshold": 0.0})
        settings = Settings(max_position_value=0.0)
        state = {"placed": 0, "orders": {}, "next": 14001, "auth_mode": "ok"}
        broker = make_broker(state, env)
        source = RouterStrategySource(env)
        engine = make_engine(env, broker,
                             risk_limits=HardRiskLimits.from_settings(settings),
                             strategy=FixedSignal(Signal.HOLD),
                             strategy_source=source,
                             stop_loss=0.05, take_profit=0.10)
        price = 100.0
        for i in range(2, 8):  # rising series → selected momentum enters
            price *= 1.004
            engine.process_candle(candle(i, price))
        assert state["placed"] == 1
        post = json.loads(state["bodies"][0])
        decision = [e for e in env.list_system_events(limit=50)
                    if e["kind"] == "DECISION"][-1]["payload"]
        risk = [e for e in env.list_system_events(limit=50)
                if e["kind"] == "RISK_DECISION"][-1]["payload"]
        # graph edge 1: DECISION → risk gate → broker POST (same qty/instrument)
        assert decision["quantity"] == risk["quantity"] == post["quantity"]
        assert decision["instrument_key"] == RELIANCE_KEY
        assert post["instrument_token"] == RELIANCE_KEY
        assert decision["strategy"] == "momentum"
        active = env.get_engine_state("active_strategy")
        assert active["strategy"] == "momentum"
        assert active["parameters"]["period"] == 3

        # graph edge 2: reconciliation applies the exact remote fill
        entry_avg = 100.4
        state["orders"][state_next(state)] = {"status": "complete",
                                              "filled": post["quantity"],
                                              "avg": entry_avg}
        engine.reconcile()
        ledger = broker.ledger.snapshot()
        assert ledger["position_quantity"] == post["quantity"]
        assert ledger["position_avg_price"] == entry_avg

        # graph edge 3: protective take-profit exits the SAME instrument
        for i in range(8, 18):  # > +10% move → TP exit
            price *= 1.012
            engine.process_candle(candle(i, price))
        assert state["placed"] == 2
        exit_post = json.loads(state["bodies"][1])
        assert exit_post["instrument_token"] == RELIANCE_KEY
        assert exit_post["transaction_type"] == "SELL"
        assert exit_post["quantity"] == post["quantity"]
        exit_avg = float(price)
        state["orders"][state_next(state)] = {"status": "complete",
                                              "filled": exit_post["quantity"],
                                              "avg": exit_avg}
        engine.reconcile()
        final = broker.ledger.snapshot()
        assert final["position_quantity"] == 0
        expected = (exit_avg - entry_avg) * post["quantity"]
        assert final["realized_pnl"] == pytest.approx(expected, rel=1e-6)

        # graph edge 4: attribution learns from the ACTUAL executed round trip
        outcomes = [e for e in env.list_system_events(limit=50)
                    if e["kind"] == "TRADE_OUTCOME"]
        assert outcomes, "round trip must be recorded for attribution"
        payload = outcomes[0]["payload"]
        payload = json.loads(payload) if isinstance(payload, str) else payload
        assert payload["strategy"] == "momentum"
        assert payload["symbol"] == "RELIANCE"
        assert payload["quantity"] == post["quantity"]
        assert payload["pnl"] == pytest.approx(expected, rel=1e-6)


class TestPromotionOnSeparatedValidation:
    """Brief 9: promotion basis is VALIDATION; the locked test is never read."""

    def test_promotion_ignores_locked_test_partition(self, env):
        from app.backtesting.costs import ExecutionConfig
        from app.backtesting.engine import BacktestEngine
        from app.learning.evaluator import evaluate_and_maybe_promote

        seen_series = []

        class SpyEngine(BacktestEngine):
            def run(self, candles, strategy, timeframe="1D", **kwargs):
                seen_series.append(tuple(c.timestamp for c in candles))
                return super().run(candles, strategy, timeframe, **kwargs)

        engine = SpyEngine(ExecutionConfig())
        # validation window: candidate (period 4) clearly better
        validation = [candle(i % 60, 100.0 + (2.0 if i % 4 == 0 else 0.0)
                             + 0.5 * i) for i in range(60)]
        # full research series whose tail (locked test) would favor period 2
        locked_tail = [candle(i % 60, 100.0 + (1.0 if i % 2 == 0 else 0.0))
                       for i in range(40)]
        full = validation + locked_tail
        validation_ids = {id(c) for c in validation}

        decision = evaluate_and_maybe_promote(
            env, full, "momentum", {"period": 2, "threshold": 0.0}, engine,
            "1m", candidate_parameters={"period": 4, "threshold": 0.0},
            validation_candles=validation)
        assert decision["promotion_basis"] == "validation"
        # every backtest run used ONLY validation candles
        for series in seen_series:
            assert all(ts in {c.timestamp for c in validation} for ts in series), \
                "promotion backtests must never touch the locked partition"
        assert decision["current"]["leg"] == "validation"
        assert decision["candidate"]["leg"] == "validation"

    def test_rolling_window_labels_promotion_basis(self, env):
        from app.backtesting.costs import ExecutionConfig
        from app.backtesting.engine import BacktestEngine
        from app.learning.evaluator import evaluate_and_maybe_promote

        series = [candle(i % 60, 100.0 + 0.5 * i) for i in range(60)]
        decision = evaluate_and_maybe_promote(
            env, series, "momentum", {"period": 2, "threshold": 0.0},
            BacktestEngine(ExecutionConfig()), "1m")
        assert decision["promotion_basis"] == "validation_window"
        assert decision["current"]["split"]["chronological"] is True
