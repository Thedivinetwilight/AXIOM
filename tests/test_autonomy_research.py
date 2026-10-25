from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math

import pytest

from app.autonomy.models import Forecast, Instrument
from app.autonomy.research import (
    HoldoutLocked, ResearchConfig, ResearchDataset, ResearchRunner, chronological_folds,
)
from app.autonomy.strategy import EvidenceRegistry
from app.data.models import Candle

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
KEY = "NSE_EQ|INE000000000"


def item():
    return Instrument(KEY, "NSE_EQ-INE000000000", "NSE_EQ", "NSE", "TEST", "EQ",
                      0.05, 1, 100000, 1.0, short_route_metadata=True)


def dataset(count=150):
    bars = []
    for n in range(count):
        close = round((100 + n * 0.2 + math.sin(n * 0.7) * 2) / 0.05) * 0.05
        open_price = round((close - 0.1) / 0.05) * 0.05
        high = round((max(open_price, close) + 0.3) / 0.05) * 0.05
        low = round((min(open_price, close) - 0.3 - (1 if n % 5 == 0 else 0)) / 0.05) * 0.05
        bars.append(Candle(NOW + timedelta(minutes=n), open_price, high, low, close,
                           100000, item().display_symbol))
    return ResearchDataset(item(), "1m", tuple(bars), "a" * 64, "csv", True, False, False, {})


def config():
    return ResearchConfig(train_bars=60, test_bars=20, folds=2, holdout_bars=20,
                          embargo_bars=2, rejection_rate=0.02)


def test_chronological_folds_reserve_embargo_and_holdout():
    folds, holdout = chronological_folds(150, config())
    assert holdout == (130, 150)
    for train_end, start, end, _ in folds:
        assert train_end < start < end <= 130
        assert start - train_end == 2
    assert folds[0][2] == folds[1][1]


def test_research_is_deterministic_and_holdout_cannot_be_reused(tmp_path):
    first = ResearchRunner(config()).run(dataset(), output_dir=tmp_path / "one")
    second = ResearchRunner(config()).run(dataset(), output_dir=tmp_path / "two")
    for key in first["results"]:
        assert first["results"][key]["evidence_metrics"]["trade_log"] == \
            second["results"][key]["evidence_metrics"]["trade_log"]
    with pytest.raises(HoldoutLocked):
        ResearchRunner(config()).run(dataset(), output_dir=tmp_path / "one")
    evidence_files = list((tmp_path / "one" / "evidence").glob("*.json"))
    assert len(evidence_files) == 8
    raw = json.loads(evidence_files[0].read_text())
    assert raw["upstox_authoritative"] is False
    assert raw["synthetic"] is True


def test_research_forecast_hook_sees_only_prefix_and_reports_calibration(tmp_path):
    calls = []

    def forecast_provider(*, instrument_key, bars, horizon, holdout):
        calls.append((len(bars), bars[-1].timestamp, holdout))
        last = bars[-1].close
        return Forecast(
            instrument_key=instrument_key, input_end=bars[-1].timestamp,
            generated_at=bars[-1].timestamp, horizon=horizon, mean_close=last * 1.01,
            q10_close=last * 0.99, q90_close=last * 1.02, up_probability=0.8,
            artifact_digest="test-artifact", model_revision="m" * 40,
            tokenizer_revision="t" * 40, validated=False,
        )

    report = ResearchRunner(config(), forecast_provider).run(dataset(), output_dir=tmp_path / "research")
    assert calls and all(length >= 61 for length, _, _ in calls)
    metrics = report["results"]["KRONOS_INFORMED:BUY"]["walk_forward"]["forecast"]
    assert metrics["status"] == "EVALUATED"
    assert metrics["count"] > 0
    assert 0 <= metrics["direction_accuracy"] <= 1
    assert 0 <= metrics["brier_score"] <= 1
    assert metrics["calibration"]
    assert report["results"]["KRONOS_INFORMED:BUY"]["evidence_metrics"]["forecast"]["count"] >= 0


def test_csv_cannot_be_relabelled_as_upstox_authoritative(tmp_path):
    path = tmp_path / "bars.csv"
    path.write_text("timestamp,open,high,low,close,volume,symbol\n"
                    "2026-01-01T00:00:00+00:00,100,101,99,100.5,1000,WRONG\n")
    with pytest.raises(ValueError, match="exactly match"):
        ResearchDataset.from_csv(path, item(), "1m", synthetic=True)


def test_raw_research_source_must_be_exact_upstox_key_and_host(tmp_path):
    path = tmp_path / "raw.json"
    path.write_text(json.dumps({"status": "success", "data": {"candles": []}}))
    with pytest.raises(ValueError, match="api.upstox.com"):
        ResearchDataset.from_upstox_json(path, item(), "1m", source_url="https://example.com/x")
    with pytest.raises(ValueError, match="exact instrument"):
        ResearchDataset.from_upstox_json(path, item(), "1m",
                                         source_url="https://api.upstox.com/v3/historical-candle/OTHER")
