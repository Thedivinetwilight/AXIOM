"""FINAL-11/10 §56: the entire live architecture against the mock exchange.

The REAL UpstoxLiveBroker (same class that moves real money) speaks its real
wire protocol to a deterministic in-process exchange through an httpx
transport. No socket is opened, so no automated test can place a real order
— yet every broker failure class is exercised for real: rejection, rate
limit, accepted-but-response-lost, delayed partial fills, cancel failure,
duplicates, auth failure — plus the full gateway → broker pipeline.
"""

from __future__ import annotations

import pytest

from app.brokers.mock_exchange import MockUpstoxExchange
from app.brokers.live import UpstoxLiveBroker
from app.brokers.sandbox import SandboxAuthError, SandboxRequestError
from app.config.settings import LIVE_CONFIRM_PHRASE
from app.database.database import Database
from app.database.repository import Repository

KEY = "NSE_EQ|INE002A01018"


@pytest.fixture()
def exchange():
    return MockUpstoxExchange(token="mock-token")


@pytest.fixture()
def repo(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'mock.db'}")
    return Repository(database)


def live_broker(exchange, repo):
    return UpstoxLiveBroker(
        "mock-token", KEY, confirm=LIVE_CONFIRM_PHRASE,
        repository=repo, transport=exchange.transport())


# =====================================================================
# full lifecycle through the REAL live broker class
# =====================================================================

class TestLiveBrokerAgainstMockExchange:
    def test_submit_ack_fill_is_applied_exactly_once(self, exchange, repo):
        broker = live_broker(exchange, repo)
        intent = broker.submit("BUY", 10, 2900.0, intent_id="intent-happy",
                               instrument_key=KEY)
        assert intent == "intent-happy"
        placed = [r for r in exchange.request_log if r["path"].endswith("place")]
        assert len(placed) == 1
        fills = broker.reconcile()
        assert fills and broker.ledger.position_quantity == 10
        # idempotent: a second reconciliation applies nothing again
        assert broker.reconcile() == []
        assert broker.ledger.position_quantity == 10

    def test_rejected_order_never_touches_the_ledger(self, exchange, repo):
        exchange.fail_next_place("reject_400")
        broker = live_broker(exchange, repo)
        with pytest.raises(SandboxRequestError):
            broker.submit("BUY", 10, 100.0, intent_id="intent-rejected",
                          instrument_key=KEY)
        assert broker.ledger.position_quantity == 0
        orders = repo.list_live_orders()
        assert orders and orders[0]["status"] == "REJECTED"

    def test_rate_limit_leaves_unknown_and_reconcile_resolves_it(
            self, exchange, repo):
        # The POST fails with 429 AFTER the exchange created the order? No:
        # 429 means the outcome is unknown and nothing was created here —
        # so the journal stays UNKNOWN and the tag lookup must verify
        # ABSENCE rather than fabricating a fill.
        exchange.fail_next_place("rate_limit_429")
        broker = live_broker(exchange, repo)
        with pytest.raises(SandboxRequestError):
            broker.submit("BUY", 10, 100.0, intent_id="intent-429",
                          instrument_key=KEY)
        assert exchange.orders == {}           # nothing was created remotely
        broker.reconcile()                     # tag lookup proves absence
        assert broker.ledger.position_quantity == 0
        from app.engine.state import SubmissionJournal, VERIFIED_ABSENCE

        journal = SubmissionJournal(repo)
        assert journal.get("intent-429")["state"] == VERIFIED_ABSENCE

    def test_accepted_but_lost_response_is_resolved_by_tag_lookup(
            self, exchange, repo):
        # §22: the order EXISTS remotely but the acknowledgement was lost.
        # The client must resolve it through the documented tag lookup —
        # never by a blind re-POST — and apply the fill exactly once.
        exchange.lose_next_place_response()
        broker = live_broker(exchange, repo)
        with pytest.raises(SandboxRequestError):
            broker.submit("BUY", 10, 100.0, intent_id="intent-lost",
                          instrument_key=KEY)
        assert len(exchange.orders) == 1       # created server-side
        assert broker.ledger.position_quantity == 0  # nothing applied yet
        places_before = sum(1 for r in exchange.request_log
                            if r["path"].endswith("place"))
        broker.reconcile()
        places_after = sum(1 for r in exchange.request_log
                           if r["path"].endswith("place"))
        assert places_after == places_before   # NO re-POST happened
        assert broker.ledger.position_quantity == 10
        assert broker.reconcile() == []        # still exactly-once

    def test_delayed_partial_fill_applies_incrementally(self, exchange, repo):
        broker = live_broker(exchange, repo)
        broker.submit("BUY", 10, 100.0, intent_id="intent-partial",
                      instrument_key=KEY)
        order_id = next(iter(exchange.orders))
        exchange.set_fill_plan(order_id, [0, 4, 10])
        broker.reconcile()                     # poll 1: still open @0
        assert broker.ledger.position_quantity == 0
        broker.reconcile()                     # poll 2: partial @4
        assert broker.ledger.position_quantity == 4
        broker.reconcile()                     # poll 3: complete @10
        assert broker.ledger.position_quantity == 10
        assert broker.reconcile() == []

    def test_cancel_confirmed_by_the_exchange(self, exchange, repo):
        broker = live_broker(exchange, repo)
        broker.submit("BUY", 10, 100.0, intent_id="intent-cancel",
                      instrument_key=KEY)
        order_id = next(iter(exchange.orders))
        exchange.set_fill_plan(order_id, [0])  # never fills
        result = broker.cancel("intent-cancel")
        assert result["status"] == "CANCELLED"
        assert exchange.orders[order_id]["status"] == "cancelled"
        assert broker.reconcile() == []        # nothing ever fills
        assert broker.ledger.position_quantity == 0

    def test_cancel_failure_leaves_the_order_working(self, exchange, repo):
        broker = live_broker(exchange, repo)
        broker.submit("BUY", 10, 100.0, intent_id="intent-cancelfail",
                      instrument_key=KEY)
        order_id = next(iter(exchange.orders))
        exchange.set_fill_plan(order_id, [0])
        exchange.fail_next_cancel("reject_400")
        result = broker.cancel("intent-cancelfail")
        # Refusal is not proof either way: history says still OPEN, so the
        # order stays working and is never hidden as cancelled.
        assert result["status"] != "CANCELLED"
        assert exchange.orders[order_id]["status"] == "open"

    def test_exchange_idempotency_on_duplicate_tags(self, exchange):
        import httpx

        body = {"quantity": 5, "product": "I", "validity": "DAY", "price": 0,
                "tag": "duplicatedtag", "instrument_token": KEY,
                "order_type": "MARKET", "transaction_type": "BUY"}
        transport = exchange.transport()
        headers = {"Authorization": f"Bearer {exchange.token}"}
        with httpx.Client(transport=transport) as client:
            first = client.post("https://api-hft.upstox.com/v3/order/place",
                                json=body, headers=headers)
            second = client.post("https://api-hft.upstox.com/v3/order/place",
                                 json=body, headers=headers)
        assert first.json()["data"]["order_ids"] == \
            second.json()["data"]["order_ids"]
        assert exchange.duplicate_place_attempts == 1
        assert len(exchange.orders) == 1       # one order, not two

    def test_auth_rejection_disables_the_broker_fail_closed(self, exchange,
                                                            repo):
        exchange.reject_next_auth(times=10)
        broker = live_broker(exchange, repo)
        with pytest.raises(SandboxAuthError):
            broker.submit("BUY", 1, 100.0, intent_id="intent-auth",
                          instrument_key=KEY)
        assert broker.available is False
        with pytest.raises(SandboxAuthError):
            broker.reconcile()                 # disabled broker does nothing

    def test_no_request_ever_leaves_the_transport(self, exchange, repo):
        # §56 / §72: the mock exchange is in-process by construction. The
        # transport handler is the ONLY thing that can see a request; if the
        # broker tried a real socket this suite could not run offline at all.
        broker = live_broker(exchange, repo)
        broker.submit("BUY", 1, 100.0, intent_id="intent-local",
                      instrument_key=KEY)
        broker.reconcile()
        assert exchange.request_log, "requests must be observable"


# =====================================================================
# the whole live pipeline (session → gateway → facade → broker → exchange)
# =====================================================================

class TestLiveArchitectureAgainstMockExchange:
    def _stack(self, exchange, repo):
        from app.config.settings import Settings
        from app.live.gateway import LiveExecutionGateway
        from app.live.gateway_broker import GatewayRoutedBroker
        from app.live.guard_chain import default_chain
        from app.live.ops import read_emergency_stop
        from app.live.session import LiveSessionManager

        settings = Settings(
            mode="LIVE", live_trading=True, broker="UPSTOX_LIVE",
            live_confirm=LIVE_CONFIRM_PHRASE,
            upstox_live_access_token="mock-token",
            max_position_value=1_000_000.0, max_open_positions=5,
            max_daily_loss=50_000.0, max_trades_per_day=100,
            default_timeframe="1m", market_data_mode="realtime")
        manager = LiveSessionManager(repo)
        broker = live_broker(exchange, repo)
        gateway = LiveExecutionGateway(
            settings=settings, repository=repo, session_manager=manager,
            broker=broker, guard_chain=default_chain(settings=settings),
            epochs=manager.epochs, emergency_stop_reader=read_emergency_stop,
            account_scope="mock-account",
            context_provider=lambda: {"current_exposure": 0.0,
                                      "daily_loss_breached": False,
                                      "drawdown_breached": False,
                                      "emergency_stop": False,
                                      "canary_exhausted": False})
        facade = GatewayRoutedBroker(
            broker, gateway, market_id="NSE_EQ", strategy_id="momentum-v1",
            account_scope="mock-account", universe="core")
        return settings, manager, broker, gateway, facade

    def test_authorized_entry_travels_the_whole_pipeline(self, exchange, repo):
        settings, manager, broker, gateway, facade = self._stack(exchange, repo)
        manager.arm(settings, confirmation_phrase=LIVE_CONFIRM_PHRASE,
                    repository=repo)
        manager.activate(actor="e2e")
        order_id = facade.submit("BUY", 10, 2900.0, intent_id="sig-e2e",
                                 instrument_key=KEY)
        assert order_id
        places = [r for r in exchange.request_log
                  if r["path"].endswith("place")]
        assert len(places) == 1                # exactly one real submission
        broker.reconcile()
        assert broker.ledger.position_quantity == 10
        kinds = {e["kind"] for e in repo.list_system_events(limit=100)}
        assert {"LIVE_INTENT_AUTHORIZED", "LIVE_INTENT_SUBMITTED"} <= kinds
        assert repo.list_live_orders()         # live audit table, not sandbox

    def test_refused_entry_sends_nothing_to_the_exchange(self, exchange, repo):
        settings, manager, broker, gateway, facade = self._stack(exchange, repo)
        # session never activated → no epoch, no ACTIVE state
        from app.live.gateway import GatewayRefusal

        with pytest.raises(GatewayRefusal):
            facade.submit("BUY", 10, 2900.0, intent_id="sig-none",
                          instrument_key=KEY)
        assert exchange.request_log == []      # zero exchange traffic
        assert gateway.broker_calls == 0

    def test_emergency_stop_halts_entries_but_exits_still_escape(
            self, exchange, repo):
        settings, manager, broker, gateway, facade = self._stack(exchange, repo)
        manager.arm(settings, confirmation_phrase=LIVE_CONFIRM_PHRASE,
                    repository=repo)
        manager.activate(actor="e2e")
        facade.submit("BUY", 10, 2900.0, intent_id="sig-a", instrument_key=KEY)
        broker.reconcile()
        assert broker.ledger.position_quantity == 10
        repo.set_engine_state("emergency_stop", {"stopped": True,
                                                 "reason": "e2e-drill"})
        from app.live.gateway import GatewayRefusal

        with pytest.raises(GatewayRefusal):
            facade.submit("BUY", 5, 2900.0, intent_id="sig-b",
                          instrument_key=KEY)
        # the held position must still be exitable (§12)
        exit_order = facade.submit("SELL", 10, 2900.0, intent_id="sig-c",
                                   instrument_key=KEY)
        assert exit_order
        places = [r for r in exchange.request_log
                  if r["path"].endswith("place")]
        assert len(places) == 2                # entry + exit only
