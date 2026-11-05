"""Regression coverage for replay automation truth, durable audit and P&L scope.

All feeds and fills are local/synthetic. Secret-looking canaries are attached to
the provider only to prove that arbitrary provider state is never exported.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import time

from fastapi.testclient import TestClient

from app.api.server import create_app
from app.backtesting.costs import ExecutionConfig
from app.config.settings import Settings
from app.data.models import Candle
from app.data.realtime import ReplayStream
from app.database.database import Database
from app.database.repository import Repository
from app.paper_trading.broker import PaperBroker
from app.paper_trading.simulator import PaperSimulator
from app.strategies import create_strategy


TOKEN = "replay-audit-api-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
SECRET_CANARIES = (
    "replay-access-token-canary",
    "replay-password-canary",
    "wss://replay-url-secret-canary.invalid",
)


def config() -> ExecutionConfig:
    return ExecutionConfig(
        initial_capital=1_000,
        commission=0,
        slippage=0,
        spread=0,
        position_size=1,
        stop_loss=None,
        take_profit=None,
        max_participation=1,
    )


def bars() -> list[Candle]:
    # Momentum(2, 0) creates BUY, its next-bar fill, EXIT, then its fill.
    prices = (100, 101, 102, 103, 90, 89)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        Candle(
            start + timedelta(days=index), price, price + 1, price - 1,
            price, 10_000, "DEMO",
        )
        for index, price in enumerate(prices)
    ]


def run_replay(broker: PaperBroker, candles: list[Candle], *, manual_only: bool = False):
    async def scenario():
        simulator = PaperSimulator(broker)
        provider = ReplayStream(candles)
        provider.access_token = SECRET_CANARIES[0]
        provider.password = SECRET_CANARIES[1]
        provider.url = SECRET_CANARIES[2]
        strategy = create_strategy(
            "manual_only" if manual_only else "momentum",
            {} if manual_only else {"period": 2, "threshold": 0},
        )
        await simulator.start(provider, strategy, ["DEMO"])
        await simulator.task
        return simulator

    return asyncio.run(scenario())


def ordered_run_events(repository: Repository, run_id: str) -> list[dict]:
    events = [
        event for event in repository.list_system_events(1_000)
        if (event.get("payload") or {}).get("run_id") == run_id
    ]
    return sorted(events, key=lambda event: event["payload"]["sequence"])


def test_replay_signal_fill_end_audit_and_restart_pnl(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'replay.db'}")
    repository = Repository(database)
    broker = PaperBroker(config(), repository=repository)
    try:
        simulator = run_replay(broker, bars())
        status = simulator.status()
        assert status["state"] == "STOPPED"
        assert status["stop_reason"] == "END_OF_DATA"
        assert status["automation_enabled"] is True
        assert status["review_policy"] == "AUTOMATED_LOCAL_SIMULATION"
        assert status["feed_kind"] == "LOCAL_REPLAY"
        assert status["current_run"]["processed_bars"] == len(bars())
        assert status["current_run"]["orders_created"] == 2
        assert status["current_run"]["fills"] == 2
        assert status["current_run"]["realized_pnl"] == broker.portfolio.realized_pnl
        assert status["current_run"]["realized_pnl"] != 0
        assert status["current_run"]["ending_unrealized_pnl"] == 0
        run_id = status["current_run"]["run_id"]

        events = ordered_run_events(repository, run_id)
        kinds = [event["kind"] for event in events]
        assert kinds[0:2] == ["PAPER_SESSION_STARTED", "PAPER_SESSION_RUNNING"]
        assert kinds[-1] == "PAPER_SESSION_STOPPED"
        assert [event["payload"]["sequence"] for event in events] == list(
            range(1, len(events) + 1)
        )
        stopped = events[-1]["payload"]
        assert stopped["stop_reason"] == "END_OF_DATA"
        assert stopped["state"] == "STOPPED"
        assert stopped["orders_created"] == 2
        assert stopped["fills"] == 2

        signals = [event for event in events if event["kind"] == "PAPER_STRATEGY_SIGNAL"]
        assert {event["payload"]["signal"] for event in signals} >= {"BUY", "EXIT"}
        orders = [event for event in events if event["kind"] == "PAPER_ORDER_SIMULATED"]
        fills = [event for event in events if event["kind"] == "PAPER_FILL_SIMULATED"]
        assert len(orders) == len(fills) == 2
        first_buy_signal = next(
            event["payload"]["sequence"] for event in signals
            if event["payload"]["signal"] == "BUY"
        )
        first_buy_order = next(
            event["payload"]["sequence"] for event in orders
            if event["payload"]["side"] == "BUY"
        )
        first_buy_fill = next(
            event["payload"]["sequence"] for event in fills
            if event["payload"]["side"] == "BUY"
        )
        assert first_buy_signal < first_buy_order < first_buy_fill
        assert all(event["payload"]["simulated"] is True for event in events)

        exported = json.dumps(repository.list_system_events(1_000), sort_keys=True)
        for canary in SECRET_CANARIES:
            assert canary not in exported
        forbidden_keys = {"password", "access_token", "authorization", "api_token", "url"}
        assert forbidden_keys.isdisjoint(
            key
            for event in repository.list_system_events(1_000)
            for key in event["payload"]
        )

        historical_realized = broker.portfolio.realized_pnl
        broker.close()
        restored = PaperBroker(config(), repository=repository)
        restarted = PaperSimulator(restored)
        restart_status = restarted.status()
        assert restart_status["current_run"] is None
        assert restart_status["last_run"]["stop_reason"] == "END_OF_DATA"
        assert restored.portfolio.realized_pnl == historical_realized

        second = run_replay(restored, bars())
        assert second.status()["current_run"]["realized_pnl"] == 0
        assert second.status()["current_run"]["fills"] == 0
        assert restored.portfolio.realized_pnl == historical_realized
        restored.close()
    finally:
        if broker.repository._writer_file or broker.repository._memory_writer:
            broker.close()
        database.close()


def test_immediate_operator_stop_is_durable_even_before_run_loop(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'immediate-stop.db'}")
    repository = Repository(database)
    broker = PaperBroker(config(), repository=repository)

    async def scenario():
        simulator = PaperSimulator(broker)
        provider = ReplayStream(bars(), delay=60)
        await simulator.start(
            provider, create_strategy("momentum", {"period": 2, "threshold": 0}), ["DEMO"]
        )
        await simulator.stop()
        return simulator, provider

    try:
        simulator, provider = asyncio.run(scenario())
        assert provider.closed is True
        assert simulator.state.value == "STOPPED"
        assert simulator.status()["stop_reason"] == "OPERATOR_STOP"
        run_id = simulator.status()["current_run"]["run_id"]
        events = ordered_run_events(repository, run_id)
        assert events[0]["kind"] == "PAPER_SESSION_STARTED"
        assert events[-1]["kind"] == "PAPER_SESSION_STOPPED"
        assert events[-1]["payload"]["stop_reason"] == "OPERATOR_STOP"
        assert all(event["payload"].get("stop_reason") != "END_OF_DATA" for event in events)
    finally:
        broker.close()
        database.close()


def test_manual_only_has_no_strategy_orders_and_stays_distinct(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'manual.db'}")
    repository = Repository(database)
    broker = PaperBroker(config(), repository=repository)
    try:
        simulator = run_replay(broker, bars()[:2], manual_only=True)
        status = simulator.status()
        assert status["strategy"] == "manual_only"
        assert status["automation_enabled"] is False
        assert status["review_policy"] == "MANUAL_ORDER_REVIEW"
        assert not broker.get_orders() and not broker.fills
        run_id = status["current_run"]["run_id"]
        kinds = {event["kind"] for event in ordered_run_events(repository, run_id)}
        assert "PAPER_STRATEGY_SIGNAL" not in kinds
        assert "PAPER_ORDER_SIMULATED" not in kinds
        assert status["stop_reason"] == "END_OF_DATA"
    finally:
        broker.close()
        database.close()


def test_default_replay_api_exposes_automation_stop_and_pnl_scopes(
    tmp_path, monkeypatch,
):
    settings = Settings(
        api_token=TOKEN,
        database_url=f"sqlite:///{tmp_path / 'default.db'}",
        data_source="replay",
        default_symbol="DEMO",
        default_timeframe="1D",
    )
    monkeypatch.setattr(
        "app.api.routes.paper.load_dataset", lambda app, body: bars()[:1]
    )
    app = create_app(settings, data_dir=tmp_path)
    with TestClient(app) as client:
        response = client.post("/api/paper/start-default", headers=HEADERS, json={})
        assert response.status_code == 200, response.text
        started = response.json()
        assert started["strategy"] == "moving_average"
        assert started["automation_enabled"] is True
        assert started["review_policy"] == "AUTOMATED_LOCAL_SIMULATION"
        assert started["feed_kind"] == "LOCAL_REPLAY"
        assert started["paper_mode"] is True and started["live_trading"] is False
        assert app.state.autonomy is not None
        assert app.state.autonomy.sessions() == []
        assert app.state.vantage.status()["active"] is False

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status = client.get("/paper/status", headers=HEADERS).json()
            if status["state"] == "STOPPED":
                break
            time.sleep(0.02)
        assert status["state"] == "STOPPED"
        assert status["stop_reason"] == "END_OF_DATA"
        assert status["current_run"]["stop_reason"] == "END_OF_DATA"
        run_id = status["current_run"]["run_id"]

        workspace = client.get("/api/trading/workspace", headers=HEADERS).json()
        assert workspace["pnl"]["realized_scope"] == "PERSISTED_LOCAL_HISTORY"
        assert workspace["pnl"]["unrealized_scope"] == "CURRENT_OPEN_POSITIONS"
        assert workspace["paper_session"]["current_run"]["run_id"] == run_id
        activity = client.get(
            "/api/trading/activity", headers=HEADERS, params={"limit": 100}
        ).json()["events"]
        replay_events = [
            event for event in activity
            if (event.get("payload") or {}).get("run_id") == run_id
        ]
        assert {event["event_type"] for event in replay_events} >= {
            "PAPER_SESSION_STARTED", "PAPER_SESSION_STOPPED",
        }
        assert TOKEN not in json.dumps(activity)
        for canary in SECRET_CANARIES:
            assert canary not in json.dumps(activity)


def test_operator_assets_do_not_call_automated_replay_manual_only():
    root = Path(__file__).resolve().parents[1]
    terminal = (root / "app/static/terminal/terminal.js").read_text(encoding="utf-8")
    html = (root / "app/static/terminal/index.html").read_text(encoding="utf-8")
    legacy = (root / "app/static/chart/index.html").read_text(encoding="utf-8")
    assert "REPLAY STRATEGY · AUTOMATION ON" in terminal
    assert "Every simulated fill requires manual review" not in terminal
    assert "Local reviewed fills only" not in terminal
    assert "Start automated replay" in terminal
    assert "paper-run-details" in html
    assert "START AUTOMATED REPLAY" in legacy
