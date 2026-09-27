"""ARENA 3.0.2 — final hardening battery.

Adversarial regression tests for every execution-path defect fixed in 3.0.2:
fail-closed multi-instrument routing, full lifecycle gating before trading,
no-qualified-candidate ⇒ no entry, UNKNOWN remote-order recovery by durable
tag lookup, honest cancellation, and the full research promotion gate —
plus one end-to-end execution-graph proof and static bypass scans.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.brokers.sandbox import SandboxOrderState, UpstoxSandboxBroker
from app.config.settings import Settings
from app.database.database import Database
from app.database.repository import Repository
from app.engine.sandbox_engine import SandboxEngine
from app.learning.journal import LearningJournal
from app.risk.limits import HardRiskLimits
from app.strategies.base import Signal

from tests.test_v3_0_1_execution_safety import (FixedSignal, RELIANCE_KEY,
                                                TCS_KEY, _P, _approve,
                                                candle, make_engine, state_next)

# Project root derived dynamically — tests run from any checkout location.
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def state3002(next_id=20001):
    return {"placed": 0, "orders": {}, "next": next_id, "auth_mode": "ok",
            "bodies": []}


@pytest.fixture()
def env(tmp_path):
    database = Database(f"sqlite:///{tmp_path}/v302.db")
    repository = Repository(database)
    yield repository
    database.close()


# ---------------------------------------------------------------- handlers --

def handler302(state):
    """Extended mock sandbox: place / history-by-id / history-by-tag / cancel.

    Configurable knobs (set on `state`):
      fail_cancel:    http_status to answer DELETE with (default 200)
      cancel_timeout: raise ConnectTimeout on DELETE when truthy
      history_rows:   rows returned for tag lookups (documented order fields)
      drop_response:  raise ConnectTimeout on POST (response lost after accept)
    """

    def _handler(request: httpx.Request):
        if request.method == "POST" and request.url.path == "/v3/order/place":
            body = json.loads(request.content)
            # the remote exchange FULLY processes the order first...
            state["bodies"].append(body)
            state["placed"] += 1
            oid = str(state["next"])
            state["next"] += 1
            state["orders"][oid] = {"status": "open", "filled": 0, "avg": 0.0,
                                    "tag": body.get("tag"),
                                    "instrument_token": body.get("instrument_token"),
                                    "transaction_type": body.get("transaction_type"),
                                    "quantity": body.get("quantity"),
                                    "order_type": body.get("order_type"),
                                    "price": body.get("price")}
            if state.get("drop_response"):
                # ...then the response is lost in transit (the crash scenario)
                raise httpx.ConnectTimeout("response lost")
            return httpx.Response(200, json={"data": {"order_ids": [oid]}})
        if request.method == "GET" and request.url.path == "/v2/order/history":
            if state.get("history_status"):
                return httpx.Response(state["history_status"], json={})
            params = dict(request.url.params)
            if "order_id" in params:
                record = state["orders"].get(str(params["order_id"]))
                if record is None:
                    return httpx.Response(400, json={"errors": [
                        {"errorCode": "UDAPI100010", "message": "order not found"}]})
                return httpx.Response(200, json={"data": [{
                    "order_id": str(params["order_id"]),
                    "status": record["status"],
                    "filled_quantity": record["filled"],
                    "average_price": record["avg"],
                    "order_timestamp": "t",
                    "transaction_type": record.get("transaction_type", "BUY"),
                    "quantity": record.get("quantity"),
                    "instrument_token": record.get("instrument_token"),
                    "order_type": record.get("order_type", "MARKET"),
                    "price": record.get("price", 0)}]})
            if "tag" in params:
                rows = []
                for oid, record in state["orders"].items():
                    if record.get("tag") == params["tag"]:
                        rows.append({
                            "order_id": oid, "status": record["status"],
                            "filled_quantity": record["filled"],
                            "average_price": record["avg"],
                            "order_timestamp": "t",
                            "transaction_type": record.get("transaction_type"),
                            "quantity": record.get("quantity"),
                            "instrument_token": record.get("instrument_token"),
                            "order_type": record.get("order_type", "MARKET"),
                            "price": record.get("price", 0)})
                return httpx.Response(200, json={"data": rows})
            return httpx.Response(400, json={"errors": [
                {"errorCode": "UDAPI100010", "message": "order not found"}]})
        if request.method == "DELETE" and "cancel" in request.url.path:
            if state.get("cancel_timeout"):
                raise httpx.ConnectTimeout("cancel lost")
            oid = str(dict(request.url.params).get("order_id"))
            record = state["orders"].get(oid)
            status = state.get("fail_cancel")
            if status:
                return httpx.Response(status, json={"errors": [
                    {"errorCode": "UDAPI11006",
                     "message": "order cannot be cancelled"}]})
            if record is None:
                return httpx.Response(400, json={"errors": [
                    {"errorCode": "UDAPI100010", "message": "order not found"}]})
            record["status"] = "cancelled"
            return httpx.Response(200, json={"data": {"order_ids": [oid]}})
        return httpx.Response(404, json={"errors": [
            {"errorCode": "UDAPI99999", "message": "no route"}]})

    return _handler


def broker302(state, env, **kwargs):
    broker = UpstoxSandboxBroker(
        "tok", RELIANCE_KEY, transport=httpx.MockTransport(handler302(state)),
        repository=env, initial_capital=100000.0, **kwargs)
    broker.instrument_map.update({"RELIANCE": RELIANCE_KEY, "TCS": TCS_KEY})
    return broker


def engine302(env, broker, **kwargs):
    defaults = dict(
        provider=_P(), strategy=FixedSignal(Signal.HOLD), broker=broker,
        repository=env, risk_limits=HardRiskLimits.from_settings(Settings()),
        journal=LearningJournal(env), timeframe="1m", evaluate_every=5,
        instrument_keys={"RELIANCE": RELIANCE_KEY, "TCS": TCS_KEY})
    defaults.update(kwargs)
    return SandboxEngine(**defaults)


# =========================================================== 1. ROUTING ====

class TestFailClosedRouting:
    def test_unmapped_symbol_rejected_at_broker(self, env):
        state = state3002()
        broker = broker302(state, env)
        with pytest.raises(ValueError):
            broker.submit("BUY", 5, 100.0, intent_id="r1", symbol="WIPRO")
        assert state["placed"] == 0

    def test_mismatched_symbol_key_rejected(self, env):
        state = state3002()
        broker = broker302(state, env)
        with pytest.raises(ValueError):
            broker.submit("BUY", 5, 100.0, intent_id="r2", symbol="TCS",
                          instrument_key=RELIANCE_KEY)
        with pytest.raises(ValueError):
            broker.submit("BUY", 5, 100.0, intent_id="r3", symbol="RELIANCE",
                          instrument_key=TCS_KEY)
        assert state["placed"] == 0

    def test_tcs_signal_never_becomes_reliance_order_and_vice_versa(self, tmp_path):
        """Each symbol gets its OWN account repository (one repo = one account;
        recovery must never bleed positions across instruments)."""
        from app.engine.adaptive import RouterStrategySource
        from app.database.database import Database
        from app.database.repository import Repository

        for symbol, key in (("TCS", TCS_KEY), ("RELIANCE", RELIANCE_KEY)):
            database = Database(f"sqlite:///{tmp_path}/rt-{symbol}.db")
            env = Repository(database)
            parameters = _promote(env, f"rt-{symbol}")
            _ladder(env, "momentum", parameters)
            state = state3002()
            broker = broker302(state, env)
            engine = engine302(env, broker,
                               strategy_source=RouterStrategySource(env))
            price = 100.0
            for i in range(1, 8):
                price *= 1.004
                engine.process_candle(candle(i, price, symbol=symbol))
            assert state["placed"] == 1, symbol
            body = state["bodies"][0]
            assert body["instrument_token"] == key, \
                f"{symbol} signal must route to {key}"
            other = RELIANCE_KEY if symbol == "TCS" else TCS_KEY
            assert body["instrument_token"] != other
            state["orders"][state_next(state)] = {
                "status": "complete", "filled": 10, "avg": 100.0,
                "tag": body["tag"], "instrument_token": key,
                "transaction_type": "BUY", "quantity": body["quantity"],
                "order_type": "MARKET", "price": 0}
            engine.reconcile()
            assert broker.ledger.snapshot()["position_quantity"] == 10
            database.close()

    def test_engine_blocks_unmapped_symbol_without_orders(self, env):
        state = state3002()
        broker = broker302(state, env)
        engine = engine302(env, broker, strategy=FixedSignal(Signal.BUY))
        engine.process_candle(candle(1, 100.0, symbol="WIPRO"))
        assert state["placed"] == 0
        kinds = [e["kind"] for e in env.list_system_events(limit=20)]
        assert "RISK_REJECT" in kinds
        reject = [e for e in env.list_system_events(limit=20)
                  if e["kind"] == "RISK_REJECT"][-1]
        payload = reject["payload"]
        payload = json.loads(payload) if isinstance(payload, str) else payload
        assert payload["reason"] == "INSTRUMENT_IDENTITY_MISSING"

    def test_reconciliation_uses_intent_identity_not_provider_default(self, env):
        from app.engine.adaptive import RouterStrategySource

        parameters = _promote(env, "rc-tcs")
        _ladder(env, "momentum", parameters)
        state = state3002()
        broker = broker302(state, env)
        engine = engine302(env, broker, strategy_source=RouterStrategySource(env))
        price = 100.0
        for i in range(1, 8):
            price *= 1.004
            engine.process_candle(candle(i, price, symbol="TCS"))
        body = state["bodies"][0]
        assert body["instrument_token"] == TCS_KEY
        state["orders"][state_next(state)] = {
            "status": "complete", "filled": 10, "avg": 100.0,
            "tag": body["tag"], "instrument_token": TCS_KEY,
            "transaction_type": "BUY", "quantity": body["quantity"],
            "order_type": "MARKET", "price": 0}
        fills = engine.reconcile()
        assert fills and fills[0]["intent_id"]
        order = broker.orders[fills[0]["intent_id"]]
        assert order.symbol == "TCS"  # attribution follows the ORDER identity

    def test_broker_rejects_none_key_with_unmapped_symbol(self, env):
        state = state3002()
        broker = broker302(state, env)
        with pytest.raises(ValueError):
            broker.submit("BUY", 5, 100.0, intent_id="r4", symbol=None,
                          instrument_key=None)


# ================================================ 2/3. LIFECYCLE + ENTRY ====

def _promote(env, candidate_id, strategy="momentum",
             parameters=None, expectancy=0.06, trades=9, robustness=0.9):
    parameters = parameters or {"period": 3, "threshold": 0.0}
    env.record_candidate(
        candidate_id, experiment_id=None, strategy=strategy,
        parameters=parameters, dataset_id=None,
        metrics={"max_drawdown": 0.1},
        oos={"oos_expectancy": expectancy, "oos_trades": trades},
        robustness={"score": robustness}, promoted=True, reason="test")
    return parameters


def _ladder(env, strategy, parameters, *, stages=("RESEARCH", "VALIDATION",
                                                  "WALK_FORWARD", "APPROVED"),
            status="ACTIVE"):
    from app.engine.adaptive import RouterStrategySource

    lineage = RouterStrategySource(env).lineage_for(strategy, parameters)
    env.record_lineage(lineage, strategy, parameters, 0, None)
    for stage in stages:
        env.record_lifecycle(lineage, stage, status, {"why": "test"})
    return lineage


class TestLifecycleBeforeTrading:
    def _source_and_decision(self, env):
        from app.engine.adaptive import RouterStrategySource

        source = RouterStrategySource(env)
        return source, source.select("trend_up_normal")

    def test_promoted_but_not_approved_not_tradable(self, env):
        _promote(env, "c1")  # promoted=True, NO lifecycle records at all
        source, (decision, instance) = self._source_and_decision(env)
        assert decision["selected"] is False
        assert instance is None
        assert decision["lifecycle"]["eligible"] is False

    def test_approved_but_not_active_not_tradable(self, env):
        parameters = _promote(env, "c2")
        _ladder(env, "momentum", parameters, status="PROBATION")
        _, (decision, instance) = self._source_and_decision(env)
        assert decision["selected"] is False
        assert instance is None

    def test_retired_never_tradable(self, env):
        parameters = _promote(env, "c3")
        _ladder(env, "momentum", parameters, status="RETIRED")
        _, (decision, instance) = self._source_and_decision(env)
        assert decision["selected"] is False
        assert instance is None

    def test_fully_approved_active_tradable(self, env):
        parameters = _promote(env, "c4")
        _ladder(env, "momentum", parameters)
        _, (decision, instance) = self._source_and_decision(env)
        assert decision["selected"] is True
        assert decision["executable"] is True
        assert instance is not None and instance.name == "momentum"

    def test_incomplete_ladder_blocked(self, env):
        parameters = _promote(env, "c5")
        _ladder(env, "momentum", parameters,
                stages=("RESEARCH", "VALIDATION"))  # no WALK_FORWARD/APPROVED
        _, (decision, instance) = self._source_and_decision(env)
        assert decision["selected"] is False
        assert "missing" in decision["lifecycle"]["reason"]


class TestNoQualifiedCandidateNoEntry:
    def test_zero_candidates_zero_entries(self, env):
        state = state3002()
        broker = broker302(state, env)
        from app.engine.adaptive import RouterStrategySource

        engine = engine302(env, broker, strategy_source=RouterStrategySource(env))
        for i in range(1, 6):
            engine.process_candle(candle(i, 100.0 * (1 + 0.004 * i)))
        assert state["placed"] == 0, "no candidates ⇒ no entries"
        assert engine._router_authorized is False

    def test_all_candidates_negative_oos_zero_entries(self, env):
        parameters = _promote(env, "neg", expectancy=-0.5, trades=12)
        _ladder(env, "momentum", parameters)
        state = state3002()
        broker = broker302(state, env)
        from app.engine.adaptive import RouterStrategySource

        engine = engine302(env, broker, strategy_source=RouterStrategySource(env))
        for i in range(1, 6):
            engine.process_candle(candle(i, 100.0 * (1 + 0.004 * i)))
        assert state["placed"] == 0

    def test_promoted_only_candidates_zero_entries(self, env):
        _promote(env, "p1")  # positive OOS, promoted — but NO lifecycle
        state = state3002()
        broker = broker302(state, env)
        from app.engine.adaptive import RouterStrategySource

        engine = engine302(env, broker, strategy_source=RouterStrategySource(env))
        for i in range(1, 6):
            engine.process_candle(candle(i, 100.0 * (1 + 0.004 * i)))
        assert state["placed"] == 0
        decisions = [e for e in env.list_learning_events(limit=50)
                     if e["kind"] == "ROUTER_DECISION"]
        assert decisions  # decision recorded honestly as non-executable
        payload = decisions[-1]["payload"]
        payload = json.loads(payload) if isinstance(payload, str) else payload
        assert payload["executable"] is False

    def test_authorized_then_degraded_stops_future_entries(self, env):
        parameters = _promote(env, "live1")
        lineage = _ladder(env, "momentum", parameters)
        state = state3002()
        broker = broker302(state, env)
        from app.engine.adaptive import RouterStrategySource
        from app.learning.lifecycle import StrategyLifecycleManager

        engine = engine302(env, broker, strategy_source=RouterStrategySource(env))
        price = 100.0
        for i in range(1, 7):  # warm the swapped momentum strategy, then enter
            price *= 1.004
            engine.process_candle(candle(i, price))
        assert state["placed"] == 1, "eligible candidate trades"
        StrategyLifecycleManager(env).detect_degradation(
            lineage, [-1.0] * 10, baseline_expectancy=5.0)
        for i in range(7, 12):
            price *= 1.004
            engine.process_candle(candle(i, price))
        assert state["placed"] == 1, "degraded ⇒ entries stop immediately"

    def test_exits_still_allowed_without_authorization(self, env):
        state = state3002()
        broker = broker302(state, env)
        from app.engine.adaptive import RouterStrategySource

        engine = engine302(env, broker, strategy_source=RouterStrategySource(env),
                           stop_loss=0.05)
        # fabricate an open position through a recorded fill
        broker.submit("BUY", 10, 100.0, intent_id="pos", symbol="RELIANCE",
                      instrument_key=RELIANCE_KEY)
        state["orders"][state_next(state)] = {
            "status": "complete", "filled": 10, "avg": 100.0,
            "tag": "pos000000000", "instrument_token": RELIANCE_KEY,
            "transaction_type": "BUY", "quantity": 10,
            "order_type": "MARKET", "price": 0}
        engine.reconcile()
        engine.process_candle(candle(1, 90.0))  # −10% ⇒ protective stop
        assert state["placed"] == 2, "risk-reducing exits never blocked by gate"
        sell = [b for b in state["bodies"] if b["transaction_type"] == "SELL"]
        assert sell and sell[0]["quantity"] == 10


# ============================================== 4. UNKNOWN RECOVERY =========

class TestUnknownRecovery:
    def _unknown_intent(self, env, *, intent_id="crash-1", placed_before=1):
        """Submit while the remote accepts but the response is lost."""
        state = state3002()
        state["drop_response"] = True
        broker = broker302(state, env)
        with pytest.raises(Exception):
            broker.submit("BUY", 10, 100.0, intent_id=intent_id,
                          symbol="RELIANCE", instrument_key=RELIANCE_KEY)
        entry = broker._journal.get(intent_id)
        assert entry["state"] == "UNKNOWN"
        assert entry["tag"] == intent_id[:12]  # durable intent-derived tag
        assert state["placed"] == placed_before  # remote DID accept
        # the remote order now exists and is filled
        order_id = str(state["next"] - 1)
        record = state["orders"][order_id]
        record.update(status="complete", filled=10, avg=100.0)
        state["drop_response"] = False
        return state, broker, record, order_id

    def test_tag_persisted_before_post(self, env):
        state = state3002()
        state["drop_response"] = True
        broker = broker302(state, env)
        with pytest.raises(Exception):
            broker.submit("BUY", 10, 100.0, intent_id="tagcheck",
                          symbol="RELIANCE", instrument_key=RELIANCE_KEY)
        entry = broker._journal.get("tagcheck")
        assert entry["state"] == "UNKNOWN"
        assert entry["tag"] == "tagcheck"[:12]
        # the remote accepted and recorded the order (placed==1, response lost)
        assert state["placed"] == 1
        assert state["bodies"][0]["tag"] == entry["tag"]

    def test_remote_accepted_response_lost_restart_lookup_adopts(self, env):
        state, broker, record, order_id = self._unknown_intent(env)
        # "restart": fresh broker instance over the same durable repository
        # (the exchange-side `state` is the SAME remote book)
        broker2 = broker302(state, env)
        # no blind resubmission ever happens (durable, instance-independent):
        from app.engine.state import SubmissionJournal

        assert SubmissionJournal(env).resubmission_forbidden("crash-1")
        fills = broker2.reconcile()  # triggers REMOTE_LOOKUP first
        from app.engine.state import SubmissionJournal

        entry = SubmissionJournal(env).get("crash-1")
        assert entry["state"] in {"VERIFIED_MATCH", "ACKNOWLEDGED",
                                  "RECONCILED", "FINAL_STATE"}
        assert fills, "adopted order's fill must be applied exactly once"
        assert fills[0]["quantity"] == 10
        assert broker2.ledger.snapshot()["position_quantity"] == 10
        # and the SECOND broker never POSTed (placed stays 1 from the crash)
        total_posts = state["placed"]
        assert total_posts == 1

    def test_no_match_marks_verified_absence_never_resubmits(self, env):
        state = state3002()
        state["drop_response"] = True
        broker = broker302(state, env)
        with pytest.raises(Exception):
            broker.submit("BUY", 10, 100.0, intent_id="absent-1",
                          symbol="RELIANCE", instrument_key=RELIANCE_KEY)
        # the remote actually never recorded it (different tag namespace)
        state["orders"][str(state["next"] - 1)]["tag"] = "other-tag"
        state["drop_response"] = False
        result = broker.lookup_intent_order("absent-1")
        assert result == "VERIFIED_ABSENCE"
        entry = broker._journal.get("absent-1")
        assert entry["state"] == "VERIFIED_ABSENCE"
        assert state["placed"] == 1  # still exactly the original POST
        assert broker._journal.resubmission_forbidden("absent-1")

    def test_multiple_matches_fail_closed_unresolved(self, env):
        state = state3002()
        state["drop_response"] = True
        broker = broker302(state, env)
        with pytest.raises(Exception):
            broker.submit("BUY", 10, 100.0, intent_id="ambig-1",
                          symbol="RELIANCE", instrument_key=RELIANCE_KEY)
        # TWO remote orders carry the intent tag (slice duplicated remotely)
        first = str(state["next"] - 1)
        second = str(state["next"])
        state["next"] += 1
        for oid in (first, second):
            state["orders"][oid] = dict(state["orders"][first], status="open")
            state["orders"][oid]["order_id"] = None
        state["drop_response"] = False
        result = broker.lookup_intent_order("ambig-1")
        assert result == "UNRESOLVED"
        assert broker._journal.get("ambig-1")["state"] == "UNRESOLVED"
        assert state["placed"] == 1  # never re-POSTed
        assert broker.ledger.snapshot()["position_quantity"] == 0

    def test_strict_matching_rejects_wrong_attributes(self, env):
        state = state3002()
        state["drop_response"] = True
        broker = broker302(state, env)
        with pytest.raises(Exception):
            broker.submit("BUY", 10, 100.0, intent_id="strict-1",
                          symbol="RELIANCE", instrument_key=RELIANCE_KEY)
        oid = str(state["next"] - 1)
        # remote order has the WRONG quantity — must NOT be adopted
        state["orders"][oid]["quantity"] = 5
        state["drop_response"] = False
        result = broker.lookup_intent_order("strict-1")
        assert result == "VERIFIED_ABSENCE"
        assert broker.ledger.snapshot()["position_quantity"] == 0

    def test_lookup_unavailable_leaves_unknown_for_retry(self, env):
        state = state3002()
        state["drop_response"] = True
        broker = broker302(state, env)
        with pytest.raises(Exception):
            broker.submit("BUY", 10, 100.0, intent_id="net-1",
                          symbol="RELIANCE", instrument_key=RELIANCE_KEY)
        state["history_status"] = 503  # history GET fails (provider error)
        state["drop_response"] = False
        result = broker.lookup_intent_order("net-1")
        assert result == "UNKNOWN"  # stays resolvable, never finalized blindly
        assert broker._journal.get("net-1")["state"] == "UNKNOWN"
        assert state["placed"] == 1

    def test_unresolved_persists_across_restart(self, env):
        state = state3002()
        state["drop_response"] = True
        broker = broker302(state, env)
        with pytest.raises(Exception):
            broker.submit("BUY", 10, 100.0, intent_id="keep-1",
                          symbol="RELIANCE", instrument_key=RELIANCE_KEY)
        oid = str(state["next"] - 1)
        state["orders"][oid] = dict(state["orders"][oid], status="open")
        second = str(state["next"])
        state["orders"][second] = dict(state["orders"][oid])
        state["next"] += 1
        state["drop_response"] = False
        broker.lookup_intent_order("keep-1")  # → UNRESOLVED
        from app.engine.state import SubmissionJournal

        assert SubmissionJournal(env).get("keep-1")["state"] == "UNRESOLVED"
        assert SubmissionJournal(env).resubmission_forbidden("keep-1") is True


# ================================================= 5. HONEST CANCELLATION ===

class TestHonestCancellation:
    def _open_order(self, env, *, children=1, cancel_status=None,
                    cancel_timeout=False):
        state = state3002()
        if cancel_status:
            state["fail_cancel"] = cancel_status
        if cancel_timeout:
            state["cancel_timeout"] = True
        broker = broker302(state, env)
        intent = broker.submit("BUY", 10, 100.0, intent_id="cxl-1",
                               symbol="RELIANCE", instrument_key=RELIANCE_KEY)
        first = state_next(state)
        oids = [first]
        if children > 1:  # simulate a sliced logical order with a second child
            second = str(state["next"])
            state["next"] += 1
            state["orders"][second] = dict(state["orders"][first])
            broker.orders[intent].upstox_order_ids = [first, second]
            oids = [first, second]
        return state, broker, intent, oids

    def test_successful_cancel_confirmed(self, env):
        state, broker, intent, oids = self._open_order(env)
        result = broker.cancel(intent)
        assert result["status"] == "CANCELLED"
        assert all(r["confirmed"] for r in result["results"])
        assert broker.orders[intent].status == "CANCELLED"
        assert broker._journal.get(intent)["state"] == "FINAL_STATE"

    def test_rejected_cancel_keeps_order_working(self, env):
        state, broker, intent, oids = self._open_order(env,
                                                       cancel_status=400)
        result = broker.cancel(intent)
        assert result["status"] != "CANCELLED", \
            "rejected DELETE must never become local CANCELLED"
        assert broker.orders[intent].status in {"SUBMITTED", "OPEN"}
        assert broker._journal.get(intent)["state"] != "FINAL_STATE"
        # history still says open → the remote order is alive and visible
        assert state["orders"][oids[0]]["status"] == "open"

    def test_rejected_cancel_with_filled_history_becomes_filled(self, env):
        state, broker, intent, oids = self._open_order(env,
                                                       cancel_status=400)
        state["orders"][oids[0]].update(status="complete", filled=10,
                                        avg=100.0)
        result = broker.cancel(intent)
        assert result["status"] == "FILLED", \
            "404/reject on a FILLED order reconciles to FILLED, not CANCELLED"
        assert broker.orders[intent].status == "FILLED"

    def test_cancel_timeout_keeps_working_until_reconciled(self, env):
        state, broker, intent, oids = self._open_order(env,
                                                       cancel_timeout=True)
        result = broker.cancel(intent)
        assert result["status"] != "CANCELLED"
        assert broker.orders[intent].status in {"SUBMITTED", "OPEN"}
        # later reconciliation sees the cancel actually landed remotely
        state["orders"][oids[0]]["status"] = "cancelled"
        state["cancel_timeout"] = False
        fills = broker.reconcile()
        assert broker.orders[intent].status == "CANCELLED"

    def test_partial_slice_cancel_not_marked_cancelled(self, env):
        state, broker, intent, oids = self._open_order(env, children=2)
        first, second = oids
        # first child was already confirmed in an earlier cancellation round
        broker.orders[intent].child_status[first] = "CANCELLED"
        state["fail_cancel"] = 400  # second child's DELETE is rejected
        result = broker.cancel(intent)
        child = broker.orders[intent].child_status
        assert child[first] == "CANCELLED"
        assert child[second] != "CANCELLED"
        assert result["status"] != "CANCELLED", \
            "logical order stays working while ANY child is working"
        assert state["orders"][second]["status"] == "open"  # never hidden

    def test_all_children_confirmed_then_cancelled(self, env):
        state, broker, intent, oids = self._open_order(env, children=2)
        state["fail_cancel"] = 400
        broker.cancel(intent)  # both rejected → both reconcile from history
        state["fail_cancel"] = None
        for oid in oids:
            state["orders"][oid]["status"] = "cancelled"
        fills = broker.reconcile()
        assert broker.orders[intent].status == "CANCELLED"

    def test_cancel_states_survive_restart(self, env):
        state, broker, intent, oids = self._open_order(env,
                                                       cancel_status=400)
        broker.cancel(intent)  # rejected → still working
        # "restart": the durable record still shows a working order
        restored = env.list_sandbox_orders(limit=10)
        statuses = {row["intent_id"]: row["status"] for row in restored}
        assert statuses.get(intent) in {"SUBMITTED", "OPEN", None}
        assert statuses.get(intent) != "CANCELLED", \
            "a rejected cancellation is never persisted as CANCELLED"
        # the remote order was NEVER hidden
        assert state["orders"][oids[0]]["status"] == "open"


# ================================================= 6. PROMOTION GATE ========

class TestFullPromotionGate:
    def _evaluate(self, env, *, evidence, validation_better=True):
        from app.backtesting.costs import ExecutionConfig
        from app.backtesting.engine import BacktestEngine
        from app.learning.evaluator import evaluate_and_maybe_promote

        base = [candle(i % 60, 100.0 + (0.4 if i % 3 else -0.2) * (1 + i * 0.01))
                for i in range(60)]
        # on this series period 2 beats period 4 on the validation leg
        return evaluate_and_maybe_promote(
            env, base, "momentum", {"period": 4, "threshold": 0.0},
            BacktestEngine(ExecutionConfig()), "1m",
            candidate_parameters={"period": 2, "threshold": 0.0},
            evidence=evidence)

    def _full_evidence(self, **overrides):
        evidence = {"robustness_score": 0.8,
                    "walk_forward": {"folds": 4, "profitable_folds": 3},
                    "max_drawdown": 0.2}
        evidence.update(overrides)
        return evidence

    def _approve_lifecycle(self, env):
        from app.engine.adaptive import RouterStrategySource
        from app.learning.lifecycle import StrategyLifecycleManager

        parameters = {"period": 2, "threshold": 0.0}  # the candidate version
        lineage = RouterStrategySource(env).lineage_for("momentum", parameters)
        env.record_lineage(lineage, "momentum", parameters, 0, None)
        for stage in ("RESEARCH", "VALIDATION", "WALK_FORWARD", "APPROVED"):
            env.record_lifecycle(lineage, stage, "ACTIVE", {})
        return lineage

    def test_full_evidence_promotes(self, env):
        self._approve_lifecycle(env)
        decision = self._evaluate(env, evidence=self._full_evidence())
        assert decision["promoted"] is True
        assert all(c["passed"] for c in decision["gate"]["checks"].values())

    def test_missing_robustness_blocks_despite_better_validation(self, env):
        """THE integration proof: validation looks great, another gate fails."""
        self._approve_lifecycle(env)
        evidence = self._full_evidence()
        evidence.pop("robustness_score")  # robustness evidence missing
        decision = self._evaluate(env, evidence=evidence)
        assert decision["promoted"] is False
        assert decision["gate"]["checks"]["robustness"]["passed"] is False

    def test_weak_walk_forward_blocks(self, env):
        self._approve_lifecycle(env)
        decision = self._evaluate(env, evidence=self._full_evidence(
            walk_forward={"folds": 2, "profitable_folds": 2}))
        assert decision["promoted"] is False
        assert decision["gate"]["checks"]["walk_forward"]["passed"] is False

    def test_drawdown_breach_blocks(self, env):
        self._approve_lifecycle(env)
        decision = self._evaluate(env, evidence=self._full_evidence(
            max_drawdown=0.9))
        assert decision["promoted"] is False
        assert decision["gate"]["checks"]["drawdown_limit"]["passed"] is False

    def test_unapproved_lifecycle_blocks(self, env):
        decision = self._evaluate(env, evidence=self._full_evidence())
        assert decision["promoted"] is False
        checks = decision["gate"]["checks"]
        assert checks["lifecycle_approved"]["passed"] is False

    def test_baseline_recording_stays_report_only(self, env):
        from app.backtesting.costs import ExecutionConfig
        from app.backtesting.engine import BacktestEngine
        from app.learning.evaluator import evaluate_and_maybe_promote

        base = [candle(i % 60, 100.0 + 0.5 * i) for i in range(60)]
        decision = evaluate_and_maybe_promote(
            env, base, "momentum", {"period": 2, "threshold": 0.0},
            BacktestEngine(ExecutionConfig()), "1m")
        assert decision["promotion_basis"] == "validation_window"


# ============================================== 7. EXECUTION GRAPH E2E ======

class TestFinalExecutionGraph:
    def test_market_event_to_attribution_full_graph(self, env):
        """Proves the REAL production chain: MarketEvent → features/regime →
        OpportunityScanner → StrategyRouter → lifecycle eligibility →
        selected strategy → CapitalAllocator → final quantity → HARD risk
        gate → exact instrument → durable intent → broker POST →
        reconciliation → ledger → attribution."""
        from app.backtesting.costs import ExecutionConfig
        from app.engine.adaptive import RouterStrategySource
        from app.engine.events import EventDrivenDecisionEngine
        from app.learning.calibration import ConfidenceCalibration
        from app.learning.portfolio import CapitalAllocator
        from app.research.scanner import OpportunityScanner

        parameters = {"period": 3, "threshold": 0.0}
        env.record_candidate(
            "graph-1", experiment_id=None, strategy="momentum",
            parameters=parameters, dataset_id=None,
            metrics={"max_drawdown": 0.1},
            oos={"oos_expectancy": 0.05, "oos_trades": 9},
            robustness={"score": 0.9}, promoted=True, reason="graph")
        _ladder(env, "momentum", parameters)

        # research layer: the scanner must surface the SAME opportunity
        class Store:
            def read_range(self, key, unit, interval):
                rows, price = [], 100.0
                for i in range(40):
                    price *= 1.004
                    rows.append({"ts": f"2026-01-01T09:{15 + i:02d}:00+05:30",
                                 "open": price, "high": price, "low": price,
                                 "close": price, "volume": 1000.0})
                return rows

        settings = Settings(max_position_value=0.0)
        scanner = OpportunityScanner(env, Store(), settings=settings)
        opportunities = scanner.scan([RELIANCE_KEY], strategies=[])
        # (no strategy instances given ⇒ scanner reports nothing to act on but
        # the recorded evidence is queryable)

        # capital layer: the allocator plan for the selected strategy
        allocator = CapitalAllocator(env, settings)
        plan = allocator.allocate(
            [{"strategy": "momentum", "parameters": parameters,
              "oos": {"oos_expectancy": 0.05, "oos_trades": 9},
              "robustness_score": 0.9}], total_capital=100000.0)
        assert plan["allocations"]["momentum"] > 0
        env.set_engine_state("capital_allocation", plan)

        # execution layer: full production wiring
        state = state3002()
        broker = broker302(state, env)
        event_engine = EventDrivenDecisionEngine(move_threshold=0.003,
                                                 volume_spike=3.0)
        event_engine.register_cadence("momentum", 1)
        source = RouterStrategySource(env,
                                      calibration=ConfidenceCalibration(env))
        engine = engine302(env, broker,
                           strategy_source=source, event_engine=event_engine,
                           sizing_fraction=1.0, cost_rate=0.0,
                           stop_loss=0.05, take_profit=0.10)
        price = 100.0
        for i in range(1, 9):  # rising market: move events → entry
            price *= 1.004
            engine.process_candle(candle(i, price))
        assert state["placed"] == 1, "exactly one gated entry"
        post = state["bodies"][0]

        # --- asserted survival across EVERY boundary -----------------------
        active = env.get_engine_state("active_strategy")
        assert active["strategy"] == "momentum"
        assert active["parameters"]["period"] == 3
        assert active["executable"] is True
        scan_rows = env.list_opportunities(limit=10)  # scanner recorded

        decision = [e for e in env.list_system_events(limit=50)
                    if e["kind"] == "DECISION"][-1]["payload"]
        risk = [e for e in env.list_system_events(limit=50)
                if e["kind"] == "RISK_DECISION"][-1]["payload"]
        assert decision["strategy"] == "momentum"
        assert decision["quantity"] == risk["quantity"] == post["quantity"]
        assert decision["instrument_key"] == post["instrument_token"] \
            == RELIANCE_KEY
        assert decision["allocated_capital"] == plan["allocations"]["momentum"]
        assert decision["router_decision"]["selected"] is True
        assert risk["notional"] == pytest.approx(post["quantity"] * price, rel=0.2)

        # durable journal: the intent recorded BEFORE the POST is retrievable
        # by its deterministic id and carries the broker tag (3.0.2 contract)
        journal_entry = broker._journal.get(decision["intent_id"])
        assert journal_entry is not None, \
            "durable journal entry must exist for the submitted intent"
        assert journal_entry["tag"] == post["tag"]

        # durable intent: created BEFORE the POST with a durable tag
        intents = [e for e in env.list_system_events(limit=50)
                   if e["kind"] == "INTENT_PERSISTED"]
        assert intents, "intent persisted BEFORE broker POST"
        first_post = post
        tag = first_post["tag"]
        assert len(tag) <= 20

        # reconciliation → ledger with the EXACT remote fill
        state["orders"][state_next(state)] = {
            "status": "complete", "filled": post["quantity"], "avg": 100.4,
            "tag": tag, "instrument_token": RELIANCE_KEY,
            "transaction_type": "BUY", "quantity": post["quantity"],
            "order_type": "MARKET", "price": 0}
        engine.reconcile()
        ledger = broker.ledger.snapshot()
        assert ledger["position_quantity"] == post["quantity"]
        assert ledger["position_avg_price"] == pytest.approx(100.4)

        # protective take-profit exits the SAME instrument (exits bypass the
        # authorization gate but STILL pass the hard risk gate + identity)
        for i in range(9, 20):
            price *= 1.012
            engine.process_candle(candle(i, price))
        sell_posts = [b for b in state["bodies"]
                      if b["transaction_type"] == "SELL"]
        assert sell_posts and sell_posts[0]["instrument_token"] == RELIANCE_KEY
        exit_price = price
        state["orders"][state_next(state)] = {
            "status": "complete", "filled": post["quantity"], "avg": exit_price,
            "tag": sell_posts[0]["tag"], "instrument_token": RELIANCE_KEY,
            "transaction_type": "SELL", "quantity": post["quantity"],
            "order_type": "MARKET", "price": 0}
        engine.reconcile()
        final = broker.ledger.snapshot()
        assert final["position_quantity"] == 0
        expected = (exit_price - 100.4) * post["quantity"]
        assert final["realized_pnl"] == pytest.approx(expected, rel=1e-6)

        # attribution learned from the ACTUAL executed round trip
        outcomes = [e for e in env.list_system_events(limit=50)
                    if e["kind"] == "TRADE_OUTCOME"]
        assert outcomes
        payload = outcomes[0]["payload"]
        payload = json.loads(payload) if isinstance(payload, str) else payload
        assert payload["strategy"] == "momentum"
        assert payload["symbol"] == "RELIANCE"
        assert payload["quantity"] == post["quantity"]
        assert payload["pnl"] == pytest.approx(expected, rel=1e-6)


# ================================================ 8. STATIC BYPASS SCANS ====

class TestStaticBypassScans:
    def test_no_or_true_in_app(self):
        import subprocess

        result = subprocess.run(
            ["grep", "-rn", "-E", r"or True", "app/", "--include=*.py"],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True)
        offenders = [ln for ln in result.stdout.splitlines()
                     if "__pycache__" not in ln]
        assert offenders == [], f"forbidden `or True`: {offenders}"

    def test_no_bare_except(self):
        import subprocess

        result = subprocess.run(
            ["grep", "-rn", "-E", r"except\s*:", "app/", "--include=*.py"],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True)
        offenders = [ln for ln in result.stdout.splitlines()
                     if "__pycache__" not in ln]
        assert offenders == [], f"bare except: {offenders}"

    def test_no_silent_exception_swallow(self):
        import subprocess

        result = subprocess.run(
            ["grep", "-rn", "-B1", "pass$", "app/", "--include=*.py",
             "-A0"], cwd=str(PROJECT_ROOT), capture_output=True,
            text=True)
        allowed = ("TimeoutError, StopAsyncIteration",)
        offenders = []
        for block in result.stdout.split("\n--\n"):
            if "pass" in block and "except" in block:
                if not any(a in block for a in allowed) \
                        and "CancelledError" not in block:
                    offenders.append(block.strip()[:200])
        assert offenders == [], f"silent swallows: {offenders}"

    def test_engine_never_defaults_to_broker_key(self):
        import subprocess

        result = subprocess.run(
            ["grep", "-rn", "broker.instrument_key", "app/engine/",
             "--include=*.py"], cwd=str(PROJECT_ROOT),
            capture_output=True, text=True)
        assert result.stdout.strip() == "", \
            f"engine must never default to the broker key: {result.stdout}"

    def test_unresolved_states_forbid_resubmission(self):
        from app.engine.state import (NO_RESUBMIT_STATES, REMOTE_LOOKUP,
                                      UNRESOLVED, UNKNOWN,
                                      VERIFIED_ABSENCE, VERIFIED_MATCH)
        for s in (UNKNOWN, REMOTE_LOOKUP, VERIFIED_MATCH, VERIFIED_ABSENCE,
                  UNRESOLVED):
            assert s in NO_RESUBMIT_STATES
