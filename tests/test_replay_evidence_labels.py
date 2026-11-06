"""Replay provenance and result visibility (task 5: explain the 50s/21,000 run).

The default local replay consumes the bundled synthetic ``DEMO`` sample CSV
(finite, deterministic prices) and its simulated P&L must never be presented as
real-market evidence. These tests pin the visibility contract: data source,
simulated time span, starting capital, currency, processed bars, fills and stop
reason are all exposed and honestly labelled, and they reproduce the reported
≈21,000 result so the explanation cannot silently rot.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.backtesting.costs import ExecutionConfig
from app.config.settings import Settings
from app.data.cache import DataCache
from app.data.factory import stream_provider
from app.data.historical import CSVProvider
from app.data.models import Candle
from app.data.realtime import ReplayStream, replay_provenance
from app.database.database import Database
from app.database.repository import Repository
from app.paper_trading.broker import PaperBroker
from app.paper_trading.simulator import PaperSimulator
from app.strategies import create_strategy


def config(initial_capital: float = 1_000) -> ExecutionConfig:
    return ExecutionConfig(
        initial_capital=initial_capital,
        commission=0,
        slippage=0,
        spread=0,
        position_size=1,
        stop_loss=None,
        take_profit=None,
        max_participation=1,
    )


def bars() -> list[Candle]:
    prices = (100, 101, 102, 103, 90, 89)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        Candle(
            start + timedelta(days=index), price, price + 1, price - 1,
            price, 10_000, "DEMO",
        )
        for index, price in enumerate(prices)
    ]


def run_replay(broker, candles, *, source_label="local-csv-replay",
               data_kind="local-csv", strategy=None, symbols=("DEMO",)):
    async def scenario():
        simulator = PaperSimulator(broker)
        provider = ReplayStream(
            candles, source_label=source_label, data_kind=data_kind
        )
        await simulator.start(
            provider,
            strategy or create_strategy("momentum", {"period": 2, "threshold": 0}),
            list(symbols),
        )
        await simulator.task
        return simulator

    return asyncio.run(scenario())


def test_replay_provenance_classifies_only_bundled_files_as_synthetic():
    assert replay_provenance("sample.csv") == ("csv-replay:sample.csv", "synthetic-sample")
    assert replay_provenance("sample_1h.csv")[1] == "synthetic-sample"
    assert replay_provenance("sample_alt.csv")[1] == "synthetic-sample"
    assert replay_provenance("sample_alt_1h.csv")[1] == "synthetic-sample"
    # Anything else is local CSV of unverified provenance — never market data.
    assert replay_provenance("exported_nse.csv") == (
        "csv-replay:exported_nse.csv", "local-csv",
    )
    assert replay_provenance(None) == ("csv-replay:local-csv-replay", "local-csv")


def test_stream_provider_replay_passes_provenance_through():
    provider = stream_provider(
        Settings(), "replay", bars=bars(), delay=0,
        source_label="csv-replay:sample.csv", data_kind="synthetic-sample",
    )
    assert isinstance(provider, ReplayStream)
    assert provider.source_label == "csv-replay:sample.csv"
    assert provider.data_kind == "synthetic-sample"


def test_run_view_exposes_data_source_span_capital_currency_and_labels(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'labels.db'}")
    repository = Repository(database)
    broker = PaperBroker(config(), currency="INR", timeframe="1D", repository=repository)
    candles = bars()
    simulator = run_replay(
        broker, candles,
        source_label="csv-replay:sample.csv", data_kind="synthetic-sample",
    )
    run = simulator.status()["current_run"]
    assert run["data_source"] == {
        "label": "csv-replay:sample.csv",
        "kind": "LOCAL_CSV_REPLAY",
        "synthetic": True,
        "data_kind": "synthetic-sample",
    }
    assert "NOT REAL MARKET DATA" in run["evidence_label"]
    assert "NOT PROFIT EVIDENCE" in run["evidence_label"]
    assert run["simulated_span"] == {
        "first_bar_ts": candles[0].timestamp.isoformat(),
        "last_bar_ts": candles[-1].timestamp.isoformat(),
    }
    assert run["starting_capital"] == 1_000.0
    assert run["currency"] == "INR"
    assert run["processed_bars"] == 6
    assert run["fills"] == 2
    assert run["stop_reason"] == "END_OF_DATA"
    # Top-level status carries the same standing labels.
    status = simulator.status()
    assert status["data_source"]["synthetic"] is True
    assert "NOT REAL MARKET DATA" in status["evidence_label"]


def test_unverified_local_csv_replay_never_claims_market_data(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'labels2.db'}")
    repository = Repository(database)
    broker = PaperBroker(config(), currency="INR", timeframe="1D", repository=repository)
    simulator = run_replay(broker, bars())  # default data_kind="local-csv"
    run = simulator.status()["current_run"]
    assert run["data_source"]["kind"] == "LOCAL_CSV_REPLAY"
    assert run["data_source"]["synthetic"] is False
    assert run["data_source"]["data_kind"] == "local-csv"
    assert "PROVENANCE UNVERIFIED" in run["evidence_label"]
    assert "PROFIT EVIDENCE" in run["evidence_label"]


def test_bundled_sample_replay_reproduces_and_labels_reported_21000_result(tmp_path):
    """The reported ~50s / ~21,000 result: finite bundled DEMO sample data.

    Pins the explanation to reality: 1600 synthetic daily bars, END_OF_DATA,
    100,000 INR starting capital, ≈21,452.58 simulated P&L — synthetic sample
    P&L, never market evidence, and never silently tunable (a strategy or cost
    change must update this test explicitly).
    """
    from app.config.settings import ROOT

    dataset = ROOT / "data" / "sample.csv"
    provider_csv = CSVProvider(dataset, "1D", DataCache(tmp_path / "cache"))
    candles = provider_csv.historical("DEMO", "1D")
    assert len(candles) == 1600

    settings = Settings()
    broker = PaperBroker(
        ExecutionConfig(**settings.execution_kwargs()),
        currency=settings.currency,
        timeframe="1D",
    )
    source_label, data_kind = replay_provenance("sample.csv")
    assert data_kind == "synthetic-sample"
    simulator = run_replay(
        broker, candles,
        source_label=source_label, data_kind=data_kind,
        strategy=create_strategy("moving_average", {}),
    )
    run = simulator.status()["current_run"]
    assert run["processed_bars"] == 1600
    assert run["stop_reason"] == "END_OF_DATA"
    assert run["starting_capital"] == pytest.approx(100_000.0)
    assert run["currency"] == "INR"
    assert run["data_source"] == {
        "label": "csv-replay:sample.csv",
        "kind": "LOCAL_CSV_REPLAY",
        "synthetic": True,
        "data_kind": "synthetic-sample",
    }
    assert run["simulated_span"]["first_bar_ts"] == "2021-01-01T00:00:00+00:00"
    assert run["simulated_span"]["last_bar_ts"] == candles[-1].timestamp.isoformat()
    assert run["fills"] == 16
    assert run["realized_pnl"] == pytest.approx(21_452.584797572003, rel=1e-9)
    assert "NOT REAL MARKET DATA" in run["evidence_label"]
