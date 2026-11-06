"""Corporate actions wired into datasets, research manifests, reports and backtests.

Synthetic fixtures only: these tests prove deterministic, auditable bookkeeping and
that adjusted prices cannot reach an execution path — not market accuracy.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.backtesting.costs import ExecutionConfig
from app.backtesting.engine import BacktestEngine
from app.config.settings import Settings
from app.data.corporate_actions import (
    FORMAT_ID,
    AdjustedSeriesError,
    block_for_meta,
    build_series,
    report_status,
)
from app.data.models import Candle
from app.model_lab.datasets import DatasetManager
from app.model_lab.registry import ModelLabService
from app.strategies import create_strategy

T0 = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)
SPLIT_DAY = 200
EX_SPLIT = T0 + timedelta(hours=SPLIT_DAY)


def synth_bars(count: int = 360, *, symbol: str = "AAPL", base: float = 100.0) -> list[Candle]:
    """Deterministic walk with a 2-for-1 split discontinuity at SPLIT_DAY."""
    bars: list[Candle] = []
    px = base
    for i in range(count):
        drift = 0.0012 * math.sin(i / 9.0) + 0.0004
        nxt = px * (1.0 + drift)
        level = nxt if i < SPLIT_DAY else nxt / 2.0  # post-split traded level
        bars.append(
            Candle(
                timestamp=T0 + timedelta(hours=i),
                open=round(level, 4),
                high=round(level * 1.002, 4),
                low=round(level * 0.998, 4),
                close=round(level, 4),
                volume=50_000 + (i % 11) * 500,
                symbol=symbol,
            )
        )
        px = nxt
    return bars


def actions_payload(*, symbol: str = "AAPL", market: str = "NASDAQ", **extra) -> dict:
    base = {
        "format": FORMAT_ID,
        "format_version": 1,
        "provider": {"name": "operator-reviewed", "version": "2026-02-01"},
        "actions": [
            {
                "record_id": f"{market}-{symbol}-SPLIT-2026-01-10",
                "revision": 1,
                "market": market,
                "symbol": symbol,
                "action_type": "SPLIT",
                "ex_date": EX_SPLIT.date().isoformat(),
                "announced_at": (EX_SPLIT - timedelta(days=20)).date().isoformat(),
                "ratio_from": 1,
                "ratio_to": 2,
                "source": "exchange filing",
                "source_version": "2026-01-10",
                "provenance": "operator transcription",
            },
            {
                "record_id": f"{market}-{symbol}-DIV-2026-01-20",
                "revision": 1,
                "market": market,
                "symbol": symbol,
                "action_type": "CASH_DIVIDEND",
                "ex_date": (EX_SPLIT + timedelta(days=8)).date().isoformat(),
                "announced_at": (EX_SPLIT - timedelta(days=30)).date().isoformat(),
                "cash_per_share": 0.5,
                "currency": "USD",
                "source": "exchange filing",
                "source_version": "2026-01-20",
                "provenance": "operator transcription",
            },
        ],
    }
    base.update(extra)
    return base


def register(
    manager: DatasetManager,
    *,
    policy: str = "SPLIT_ADJUSTED",
    claim: str = "SPLIT_ADJUSTED",
    **kwargs,
) -> dict:
    return manager.register_dataset(
        market="NASDAQ",
        symbol="AAPL",
        timeframe="1h",
        bars_or_csv=synth_bars(),
        source="Unit-test synthetic feed",
        feed="LOCAL_OHLCV",
        corporate_action_adjustment=claim,
        corporate_actions=actions_payload(),
        corporate_action_policy=policy,
        real_market_data=False,
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# Dataset manager
# --------------------------------------------------------------------------- #
def test_dataset_import_records_verified_action_provenance(tmp_path: Path):
    manager = DatasetManager(tmp_path)
    meta = register(manager)

    block = meta["corporate_actions"]
    assert meta["corporate_action_status"] == "SPLIT_ADJUSTED"
    assert meta["corporate_action_policy"] == "SPLIT_ADJUSTED"
    assert meta["corporate_actions_verified"] is True
    assert meta["corporate_actions_adjusted"] is True
    assert len(block["actions_sha256"]) == 64
    assert len(block["actions_source_sha256"]) == 64
    assert len(block["actions_history_sha256"]) == 64
    assert block["dataset_bars_sha256"] == meta["sha256"]
    assert block["raw_bars_for_execution"] is True
    assert block["provider"]["name"] == "operator-reviewed"
    assert block["actions_total"] == 2
    assert [a["action_type"] for a in block["actions_applied"]] == ["SPLIT"]
    # dividends are cash events here, not a second price adjustment
    assert block["dividends_as_cash"] is True
    assert block["cash_dividend_events"][0]["cash_per_share"] == 0.5

    sidecar = manager.storage_dir / meta["corporate_actions_file"]
    assert sidecar.is_file()
    assert json.loads(sidecar.read_text())["format"] == FORMAT_ID


def test_overstated_adjustment_claim_is_flagged_not_honoured(tmp_path: Path):
    manager = DatasetManager(tmp_path)
    meta = register(manager, claim="SPLIT_AND_DIVIDEND_ADJUSTED")
    block = meta["corporate_actions"]
    assert block["claim"] == "SPLIT_AND_DIVIDEND_ADJUSTED"
    assert block["claim_honoured"] is False
    assert block["verified"] is False
    assert meta["corporate_actions_verified"] is False
    assert any("UNVERIFIED_ADJUSTMENT_CLAIM" in n for n in block["limitations"])
    assert report_status(block) == "PARTIALLY_VERIFIED_CORPORATE_ACTION_ADJUSTMENT"


def test_stored_bars_stay_raw_while_adjusted_series_is_derived(tmp_path: Path):
    manager = DatasetManager(tmp_path)
    meta = register(manager)
    expected = synth_bars()[0]
    _, raw = manager.load_dataset(meta["dataset_id"])
    assert raw[0].close == pytest.approx(expected.close)
    assert raw[0].volume == expected.volume

    adjusted_meta, adjusted = manager.load_adjusted_dataset(meta["dataset_id"])
    assert adjusted_meta["corporate_actions"]["status"] == "SPLIT_ADJUSTED"
    assert adjusted.bars[0].close == pytest.approx(expected.close / 2.0)
    assert adjusted.bars[0].volume == pytest.approx(expected.volume * 2.0)
    assert adjusted.bars[0].close * adjusted.bars[0].volume == pytest.approx(
        raw[0].close * raw[0].volume
    )

    total_return_meta, total = manager.load_adjusted_dataset(
        meta["dataset_id"], policy="TOTAL_RETURN_ADJUSTED"
    )
    assert total.status == "DIVIDEND_ADJUSTED"
    assert total.provenance["dividends_as_cash"] is False
    assert total.provenance["cash_dividend_events"] == []


def test_point_in_time_load_excludes_actions_not_yet_effective(tmp_path: Path):
    manager = DatasetManager(tmp_path)
    meta = register(manager)
    _, early = manager.load_adjusted_dataset(
        meta["dataset_id"], as_of=T0 + timedelta(days=2)
    )
    assert early.provenance["actions_applied"] == []
    assert early.bars[0].close == pytest.approx(synth_bars()[0].close)
    assert early.provenance["pending_announced_actions"]


def test_tampered_action_sidecar_is_detected(tmp_path: Path):
    manager = DatasetManager(tmp_path)
    meta = register(manager)
    sidecar = manager.storage_dir / meta["corporate_actions_file"]
    payload = json.loads(sidecar.read_text())
    payload["actions"][0]["ratio_to"] = 3
    sidecar.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="Corporate-action SHA-256 mismatch"):
        manager.load_corporate_actions(meta["dataset_id"])


def test_invalid_actions_reject_the_dataset_import(tmp_path: Path):
    manager = DatasetManager(tmp_path)
    broken = actions_payload()
    broken["actions"][0]["ratio_from"] = broken["actions"][0]["ratio_to"]
    with pytest.raises(ValueError):
        manager.register_dataset(
            market="NASDAQ",
            symbol="AAPL",
            timeframe="1h",
            bars_or_csv=synth_bars(),
            source="x",
            feed="LOCAL_OHLCV",
            corporate_actions=broken,
            real_market_data=False,
        )
    # Nothing is written when the action feed is rejected.
    assert sorted(path.name for path in manager.storage_dir.glob("*.json")) == []
    assert all(
        entry["dataset_id"].startswith("bundled_") for entry in manager.list_datasets()
    )


def test_import_without_action_data_is_unverified_and_keeps_raw_prices(tmp_path: Path):
    manager = DatasetManager(tmp_path)
    meta = manager.register_dataset(
        market="NASDAQ",
        symbol="AAPL",
        timeframe="1h",
        bars_or_csv=synth_bars(),
        source="x",
        feed="LOCAL_OHLCV",
        corporate_action_adjustment="SPLIT_AND_DIVIDEND_ADJUSTED",
        real_market_data=False,
    )
    assert meta["corporate_action_status"] == "UNVERIFIED"
    assert meta["corporate_actions_verified"] is False
    assert meta["corporate_actions"]["claim_honoured"] is False
    _, series = manager.load_adjusted_dataset(meta["dataset_id"], policy="SPLIT_ADJUSTED")
    assert series.status == "UNVERIFIED"
    assert series.bars[0].close == pytest.approx(synth_bars()[0].close)
    assert any("NO_CORPORATE_ACTION_DATA" in n for n in series.provenance["limitations"])


# --------------------------------------------------------------------------- #
# HTTP surface (existing Model Lab endpoints only)
# --------------------------------------------------------------------------- #
def _app(tmp_path: Path):
    settings = Settings(api_token="ca-tok", database_url=f"sqlite:///{tmp_path}/ca.db")
    return TestClient(create_app(settings, data_dir=tmp_path))


def test_http_import_exposes_corporate_action_status(tmp_path: Path):
    payload = actions_payload()
    body = {
        "market": "NASDAQ",
        "symbol": "AAPL",
        "timeframe": "1h",
        "bars": [b.to_dict() for b in synth_bars()],
        "source": "HTTP unit test",
        "feed": "LOCAL_OHLCV",
        "real_market_data": False,
        "corporate_action_adjustment": "SPLIT_ADJUSTED",
        "corporate_action_policy": "SPLIT_ADJUSTED",
        "corporate_actions": payload,
    }
    headers = {"Authorization": "Bearer ca-tok"}
    with _app(tmp_path) as web:
        response = web.post("/api/model-lab/datasets/import", json=body, headers=headers)
        assert response.status_code == 200, response.text
        dataset = response.json()["dataset"]
        assert dataset["corporate_action_status"] == "SPLIT_ADJUSTED"
        assert dataset["corporate_actions"]["verified"] is True
        assert dataset["corporate_actions"]["actions_sha256"]

        listing = web.get("/api/model-lab/datasets", headers=headers)
        assert listing.status_code == 200
        entry = next(
            d for d in listing.json()["datasets"] if d["dataset_id"] == dataset["dataset_id"]
        )
        assert entry["corporate_actions"]["status"] == "SPLIT_ADJUSTED"
        assert entry["corporate_actions"]["provider"]["version"] == "2026-02-01"


def test_http_import_rejects_conflicting_action_records(tmp_path: Path):
    payload = actions_payload()
    clashing = dict(payload["actions"][0])
    clashing["record_id"] = "OTHER-SPLIT-RECORD"
    clashing["ratio_to"] = 3
    payload["actions"].append(clashing)
    body = {
        "market": "NASDAQ",
        "symbol": "AAPL",
        "timeframe": "1h",
        "bars": [b.to_dict() for b in synth_bars()],
        "real_market_data": False,
        "corporate_actions": payload,
    }
    headers = {"Authorization": "Bearer ca-tok"}
    with _app(tmp_path) as web:
        response = web.post("/api/model-lab/datasets/import", json=body, headers=headers)
        assert response.status_code == 422
        assert "disagree" in response.json()["detail"]


def test_http_import_rejects_unknown_policy(tmp_path: Path):
    body = {
        "market": "NASDAQ",
        "symbol": "AAPL",
        "timeframe": "1h",
        "bars": [b.to_dict() for b in synth_bars()],
        "real_market_data": False,
        "corporate_action_policy": "MAGIC_ADJUSTED",
    }
    headers = {"Authorization": "Bearer ca-tok"}
    with _app(tmp_path) as web:
        response = web.post("/api/model-lab/datasets/import", json=body, headers=headers)
        assert response.status_code == 422


# --------------------------------------------------------------------------- #
# Research manifest provenance
# --------------------------------------------------------------------------- #
def test_research_run_manifest_pins_action_hashes(tmp_path: Path):
    service = ModelLabService(tmp_path / "ml", repo_data_dir=Path("data"))
    meta = service.datasets.register_dataset(
        market="NASDAQ",
        symbol="AAPL",
        timeframe="1h",
        bars_or_csv=synth_bars(),
        source="Unit-test synthetic feed",
        feed="LOCAL_OHLCV",
        corporate_action_adjustment="SPLIT_ADJUSTED",
        corporate_actions=actions_payload(),
        corporate_action_policy="SPLIT_ADJUSTED",
        real_market_data=False,
    )
    run = service.start_research_run(
        dataset_id=meta["dataset_id"],
        market="NASDAQ",
        timeframe="1h",
        spread_bps=2.0,
        slippage_bps=2.0,
        min_trades=1,
        async_execution=False,
    )
    assert run["status"] == "COMPLETED"
    dataset_block = run["manifest"]["dataset"]
    assert dataset_block["corporate_action_status"] == "SPLIT_ADJUSTED"
    assert dataset_block["corporate_actions_verified"] is True
    assert (
        dataset_block["corporate_actions"]["actions_sha256"]
        == meta["corporate_actions"]["actions_sha256"]
    )
    assert dataset_block["corporate_actions"]["policy"] == "SPLIT_ADJUSTED"
    assert dataset_block["corporate_actions"]["dataset_bars_sha256"] == meta["sha256"]


def test_research_run_manifest_marks_unverified_data(tmp_path: Path):
    service = ModelLabService(tmp_path / "ml", repo_data_dir=Path("data"))
    meta = service.datasets.register_dataset(
        market="NASDAQ",
        symbol="AAPL",
        timeframe="1h",
        bars_or_csv=synth_bars(),
        source="Unit-test synthetic feed",
        feed="LOCAL_OHLCV",
        corporate_action_adjustment="UNVERIFIED_RAW",
        real_market_data=False,
    )
    run = service.start_research_run(
        dataset_id=meta["dataset_id"],
        market="NASDAQ",
        timeframe="1h",
        min_trades=1,
        async_execution=False,
    )
    dataset_block = run["manifest"]["dataset"]
    assert dataset_block["corporate_action_status"] == "UNVERIFIED"
    assert dataset_block["corporate_actions_verified"] is False
    assert any(
        "NO_CORPORATE_ACTION_DATA" in note
        for note in dataset_block["corporate_actions"]["limitations"]
    )


def test_report_status_labels():
    assert report_status({"verified": True, "status": "SPLIT_ADJUSTED"}) == (
        "CORPORATE_ACTIONS_VERIFIED"
    )
    assert report_status({"verified": False, "status": "SPLIT_ADJUSTED"}) == (
        "PARTIALLY_VERIFIED_CORPORATE_ACTION_ADJUSTMENT"
    )
    assert report_status({"verified": False, "status": "UNVERIFIED"}) == (
        "UNVERIFIED_CORPORATE_ACTION_ADJUSTMENT"
    )
    assert block_for_meta({"corporate_action_adjustment": "UNVERIFIED_RAW"})["status"] == (
        "UNVERIFIED"
    )


# --------------------------------------------------------------------------- #
# Backtest engine: provenance recorded, raw/execution separation enforced
# --------------------------------------------------------------------------- #
def _config() -> ExecutionConfig:
    return ExecutionConfig(
        initial_capital=100_000,
        commission=0.0,
        slippage=0.0,
        spread=0.0,
        position_size=0.5,
        stop_loss=None,
        take_profit=None,
        max_participation=1.0,
    )


def test_backtest_records_corporate_action_basis_and_warns():
    manager = DatasetManager(Path("/tmp") / f"ca-bt-{datetime.now(UTC).timestamp()}")
    meta = register(manager)
    _, series = manager.load_adjusted_dataset(meta["dataset_id"])
    engine = BacktestEngine(_config())
    result = engine.run(list(series.bars), create_strategy("moving_average"), timeframe="1h", warmup=[])
    assert result.corporate_actions == {}  # a bare candle list declares nothing

    from app.data.corporate_actions import AdjustedSeries

    adjusted = AdjustedSeries(
        market="NASDAQ", symbol="AAPL", timeframe="1h",
        bars=list(series.bars), provenance=series.provenance,
    )
    result = engine.run(adjusted, create_strategy("moving_average"), timeframe="1h", warmup=[])
    assert result.corporate_actions["status"] == "SPLIT_ADJUSTED"
    assert result.corporate_actions["actions_sha256"] == meta["corporate_actions"]["actions_sha256"]
    assert any("adjusted prices" in w for w in result.warnings)
    assert any("Cash dividends are NOT credited" in w for w in result.warnings)


def test_backtest_refuses_double_counted_dividends():
    engine = BacktestEngine(_config())
    with pytest.raises(ValueError, match="double counting"):
        engine.run(
            synth_bars(80),
            create_strategy("moving_average"),
            timeframe="1h",
            adjustment={"dividend_price_adjustment": True, "dividends_as_cash": True},
        )


def test_research_backtest_cannot_silently_use_adjusted_prices_for_fills():
    manager = DatasetManager(Path("/tmp") / f"ca-guard-{datetime.now(UTC).timestamp()}")
    meta = register(manager)
    _, series = manager.load_adjusted_dataset(meta["dataset_id"])
    with pytest.raises(AdjustedSeriesError):
        series.for_execution()


# --------------------------------------------------------------------------- #
# Paper replay path stays raw
# --------------------------------------------------------------------------- #
def test_paper_replay_load_returns_raw_prices(tmp_path: Path):
    from app.api.services import load_dataset

    csv_path = tmp_path / "demo.csv"
    header = "timestamp,open,high,low,close,volume,symbol\n"
    rows = "".join(
        f"2026-01-0{day}T00:00:00Z,100,101,99,100,1000,DEMO\n" for day in range(1, 6)
    )
    csv_path.write_text(header + rows, encoding="utf-8")
    app = SimpleNamespace(state=SimpleNamespace(data_dir=tmp_path))
    body = SimpleNamespace(dataset="demo.csv", timeframe="1D", symbol="DEMO")
    bars = load_dataset(app, body)
    assert [b.close for b in bars] == [100.0] * 5

    # An adjusted series handed to the same boundary is refused outright.
    adjusted = build_series(bars, None, policy="SPLIT_ADJUSTED")
    from app.data.corporate_actions import AdjustedSeries, assert_raw_candles

    wrapper = AdjustedSeries(
        market="NASDAG", symbol="DEMO", timeframe="1D", bars=bars,
        provenance={"status": "SPLIT_ADJUSTED", "prices_adjusted": True},
    )
    with pytest.raises(AdjustedSeriesError):
        assert_raw_candles(wrapper, context="paper replay dataset")
    assert adjusted.status == "UNVERIFIED"
