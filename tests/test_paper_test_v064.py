"""V0.6.4 tests: offline synthetic feed → paper pipeline, Termux-relevant
zero-dependency execution, and safe refusal without Upstox credentials.

The synthetic provider is clearly NOT real Upstox data; these tests prove the
pipeline (MarketEvent → candle builder → market-status gate → strategy → risk
→ PaperBroker → SQLite) is deterministic and runs on the standard library
alone. External Upstox verification remains a separate, credential-gated task.
"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from app.backtesting.costs import ExecutionConfig
from app.config.settings import Settings
from app.data.synthetic_feed import (
    PROVIDER_NAME,
    SyntheticMarketDataProvider,
    SyntheticStream,
    build_scenario,
)
from app.database.database import Database
from app.database.repository import Repository
from app.paper_trading.broker import PaperBroker
from app.paper_trading.simulator import PaperSimulator
from app.strategies import create_strategy

PROJECT = Path(__file__).resolve().parents[1]
EXPECTED_EQUITY = 99637.58463385397
EXPECTED_REALIZED_PNL = -362.41536614602916
EXPECTED_CLOSES = [100.5, 101.0, 101.5, 102.0, 101.0, 100.75]


def run_pipeline(database_path: str):
    config = ExecutionConfig(**Settings().execution_kwargs())
    strategy = create_strategy("momentum", {"period": 2, "threshold": 0.0})

    async def scenario():
        db = Database(f"sqlite:///{database_path}")
        repo = Repository(db)
        broker = PaperBroker(config, timeframe="1m", repository=repo)
        sim = PaperSimulator(broker)
        provider = SyntheticStream("1m")
        await sim.start(provider, strategy, provider.symbols)
        await sim.task
        result = {
            "state": sim.status()["state"],
            "account": broker.get_balance(),
            "orders": broker.get_orders(),
            "fills": len(broker.fills),
            "trades": broker.portfolio.trades,
            "processed": sim.processed,
            "metrics": provider.metrics(),
            "risk": sim.status()["risk_counters"],
        }
        broker.close()
        db.close()
        return result

    return asyncio.run(scenario())


def test_synthetic_provider_emits_normalized_completed_candles_only():
    """Provider-neutral events → completed candles; closed-market candle withheld."""
    async def collect():
        provider = SyntheticStream("1m")
        candles = []
        async for bar in provider.stream(provider.symbols, "1m"):
            candles.append(bar)
        metrics = provider.metrics()
        await provider.close()
        return candles, metrics

    candles, metrics = asyncio.run(collect())
    assert [c.close for c in candles] == EXPECTED_CLOSES
    assert metrics["provider"] == PROVIDER_NAME == "synthetic-offline"
    assert metrics["market_status"] == "NORMAL_OPEN" and metrics["market_open"] is True
    # Exactly one candle completed while the market was still CLOSED: built,
    # counted as suppressed, NEVER emitted to the strategy.
    assert metrics["suppressed_by_market_status"] == 1
    assert metrics["feed_counters"]["duplicates"] == 1  # embedded duplicate tick
    assert metrics["feed_counters"]["completed_candles"] == 7  # 6 emitted + 1 held
    assert candles[0].symbol == "NSE_EQ-INE002A01018"
    assert SyntheticMarketDataProvider is SyntheticStream


def test_synthetic_pipeline_paper_execution_and_pinned_determinism(tmp_path):
    result = run_pipeline(str(tmp_path / "synthetic.db"))
    assert result["state"] == "STOPPED" and result["processed"] == 6
    assert [o["side"] for o in result["orders"]] == ["BUY", "SELL"]
    assert len({o["id"] for o in result["orders"]}) == 2  # no duplicate orders
    assert result["fills"] == 2 and len(result["trades"]) == 1
    assert not result["account"]["positions"]  # round trip closed
    assert result["account"]["realized_pnl"] == pytest.approx(EXPECTED_REALIZED_PNL, abs=1e-9)
    assert result["account"]["equity"] == pytest.approx(EXPECTED_EQUITY, abs=1e-9)
    assert result["risk"]["orders_created"] == 2 and result["risk"]["skipped"] == 2


def test_restart_replay_determinism_on_persisted_account(tmp_path):
    path = str(tmp_path / "restart.db")
    first = run_pipeline(path)
    config = ExecutionConfig(**Settings().execution_kwargs())
    strategy = create_strategy("momentum", {"period": 2, "threshold": 0.0})

    async def restart():
        from app.__main__ import _paper_test_continuation_scenario

        db = Database(f"sqlite:///{path}")
        repo = Repository(db)
        broker = PaperBroker(config, timeframe="1m", repository=repo)  # restore
        sim = PaperSimulator(broker)
        provider = SyntheticStream("1m", scenario=_paper_test_continuation_scenario())
        await sim.start(provider, strategy, provider.symbols)
        await sim.task
        result = {
            "equity": broker.get_balance()["equity"],
            "orders": len(broker.get_orders()),
            "processed": sim.processed,
            "state": sim.status()["state"],
        }
        broker.close()
        db.close()
        return result

    restarted = asyncio.run(restart())
    assert restarted["state"] == "STOPPED"
    assert restarted["equity"] == pytest.approx(EXPECTED_EQUITY, abs=1e-9)
    assert restarted["orders"] == 2  # replay added no orders
    assert restarted["processed"] == 2  # cursor duplicate ignored, 2 fresh candles
    assert first["account"]["equity"] == pytest.approx(EXPECTED_EQUITY, abs=1e-9)


def test_paper_test_cli_is_deterministic_across_processes(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("UPSTOX", "MARKET_DATA"))}
    outputs = []
    for run in range(2):
        proc = subprocess.run(
            [sys.executable, "-m", "app", "paper-test"],
            cwd=PROJECT, env=env, capture_output=True, text=True, timeout=120,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        outputs.append(json.loads(proc.stdout))
    assert outputs[0]["ok"] is True and outputs[1]["ok"] is True
    assert outputs[0]["actual"]["final_equity"] == outputs[1]["actual"]["final_equity"]
    assert outputs[0]["actual"]["final_equity"] == pytest.approx(EXPECTED_EQUITY, abs=1e-9)
    assert outputs[0]["checks"]["restart_replay_duplicate_suppressed"] is True


def test_paper_test_runs_without_any_third_party_packages(tmp_path):
    """Termux-relevant guarantee: the core pipeline needs ONLY the stdlib.

    Runs `python -m app paper-test` with imports of fastapi/pydantic/httpx/
    websockets/protobuf/dotenv/uvicorn/anyio/starlette hard-blocked.
    """
    script = (
        "import sys\n"
        f"BANNED = {{'fastapi','pydantic','httpx','websockets','protobuf',"
        f"'dotenv','uvicorn','anyio','starlette'}}\n"
        "class Block:\n"
        "    def find_spec(self, fullname, path=None, target=None):\n"
        "        if fullname.split('.')[0] in BANNED:\n"
        "            raise ImportError('blocked for offline test: ' + fullname)\n"
        "        return None\n"
        "sys.meta_path.insert(0, Block())\n"
        "from app.__main__ import main\n"
        f"main(['paper-test', '--database', {str(tmp_path / 'offline.db')!r}])\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = json.loads(proc.stdout)
    assert report["ok"] is True
    assert report["paper_only"] == {"mode": "PAPER", "live_trading": False}


def test_missing_upstox_credentials_safe_refusal_no_connection(tmp_path):
    """No credentials → CLI refuses safely (exit 2) with NO connection attempt."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("UPSTOX", "MARKET_DATA"))}
    env["MARKET_DATA_SYMBOLS"] = "NSE_EQ|INE002A01018"  # isolate the token refusal
    proc = subprocess.run(
        [sys.executable, "-m", "app", "upstox-test"],
        cwd=PROJECT, env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 2
    summary = json.loads(proc.stdout)
    assert summary["ok"] is False
    assert "access token" in summary["error"]  # refused before any connection
    # The harness fail-closes too (documented exit codes 2/3).
    harness = subprocess.run(
        [sys.executable, str(PROJECT.parent / "verification_v061_live" / "real_upstox_check.py")],
        cwd=PROJECT, env=env, capture_output=True, text=True, timeout=60,
    )
    if harness.returncode not in (2, 3):  # file may be absent in thin checkouts
        assert "not set" in harness.stdout or harness.returncode == 0


def test_scenario_builder_is_deterministic_and_validated():
    events = build_scenario("NSE_EQ|INE002A01018")
    again = build_scenario("NSE_EQ|INE002A01018")
    assert [(e.kind, e.received_at, e.ltp) for e in events] == [
        (e.kind, e.received_at, e.ltp) for e in again
    ]
    assert events[0].kind == "market_info" and events[0].market_status == "NORMAL_CLOSE"
    assert any(e.kind == "snapshot" for e in events)
    assert sum(1 for e in events if e.kind == "live") == 7  # incl. the duplicate
    assert sum(1 for e in events if e.kind == "snapshot") == 1
    with pytest.raises(ValueError):
        SyntheticStream("1m", instrument_key="NSE_INDEX|Nifty 50")  # unusable symbol
    with pytest.raises(ValueError):
        SyntheticStream("2h")  # unsupported timeframe
