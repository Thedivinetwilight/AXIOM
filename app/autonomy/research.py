"""Chronological research and immutable evidence generation.

This runner is not a live readiness or profitability claim.  It accepts exact
instrument metadata and either raw Upstox historical responses or explicitly
research-only CSV data.  CSV/sample data can never produce LIVE-eligible
 evidence because it is not an authenticated Upstox source.

The only forecast hook receives the prefix of candles available at the current
completed bar.  A real Kronos adapter can be supplied; no synthetic forecast is
constructed here.  Holdout locks are written before the holdout is evaluated,
so a second run cannot silently tune on it.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import statistics
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit, quote as url_quote

from app.autonomy.models import (
    Forecast, Instrument, ProtectedOrder, StrategyEvidence, ExecutionPermit,
)
from app.autonomy.paper import PaperExecution
from app.autonomy.risk import stop_sized_quantity
from app.autonomy.strategy import FAMILIES, classify_regime, generate_candidates
from app.data.models import Candle, parse_timestamp
from app.data.validator import clean_rows, validate


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class ResearchDataset:
    instrument: Instrument
    timeframe: str
    bars: tuple[Candle, ...]
    data_sha256: str
    source: str
    synthetic: bool
    upstox_authoritative: bool
    survivorship_aware: bool
    source_detail: dict[str, Any]
    data_gaps: int = 0

    def validate(self) -> None:
        self.instrument.validate()
        if not self.bars or self.timeframe not in {"1m", "5m", "15m", "30m"}:
            raise ValueError("Research requires non-empty intraday completed bars")
        if any(bar.symbol != self.instrument.display_symbol for bar in self.bars):
            raise ValueError("Research bar symbol does not match exact instrument metadata")
        report = validate(list(self.bars), self.timeframe, strict_gaps=False)
        report.require_valid()
        if len({bar.timestamp for bar in self.bars}) != len(self.bars):
            raise ValueError("Duplicate research timestamps")
        if any(self.bars[index].timestamp <= self.bars[index - 1].timestamp
               for index in range(1, len(self.bars))):
            raise ValueError("Research bars are not chronological")
        if self.upstox_authoritative and self.source != "upstox":
            raise ValueError("Only raw Upstox responses can be marked authoritative")

    @classmethod
    def from_csv(cls, path: Path, instrument: Instrument, timeframe: str, *,
                 synthetic: bool = False) -> "ResearchDataset":
        raw = path.read_bytes()
        import csv
        import io
        rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8"))))
        normalized = []
        for row in rows:
            row = dict(row)
            supplied = str(row.get("symbol") or "").strip()
            if supplied not in {instrument.display_symbol, instrument.instrument_key}:
                raise ValueError("CSV symbol does not exactly match the supplied instrument metadata")
            row["symbol"] = instrument.display_symbol
            normalized.append(row)
        bars, _ = clean_rows(normalized)
        dataset = cls(instrument, timeframe, tuple(bars), sha256_bytes(raw), "csv", synthetic,
                      False, False, {"path": path.name, "live_eligible": False})
        dataset.validate()
        gaps = len(validate(list(dataset.bars), timeframe, strict_gaps=False).warnings)
        return replace(dataset, data_gaps=gaps)

    @classmethod
    def from_upstox_json(cls, path: Path, instrument: Instrument, timeframe: str, *,
                         source_url: str, survivorship_aware: bool = False,
                         universe_snapshot_sha256: str = "") -> "ResearchDataset":
        raw = path.read_bytes()
        payload = json.loads(raw)
        if payload.get("status") != "success" or not isinstance(payload.get("data"), dict):
            raise ValueError("Raw Upstox research response is not successful")
        rows = payload["data"].get("candles")
        source_host = urlsplit(source_url).hostname
        if not isinstance(rows, list) or source_host != "api.upstox.com":
            raise ValueError("Raw Upstox candles must come from the exact api.upstox.com source")
        encoded_key = url_quote(instrument.instrument_key, safe="")
        if encoded_key not in source_url and instrument.instrument_key not in source_url:
            raise ValueError("Research source URL does not identify the exact instrument key")
        bars = []
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 6:
                raise ValueError("Malformed raw Upstox research candle")
            bars.append(Candle.from_dict({
                "timestamp": row[0], "open": row[1], "high": row[2], "low": row[3],
                "close": row[4], "volume": row[5], "symbol": instrument.display_symbol,
            }))
        if any(bars[index].timestamp <= bars[index - 1].timestamp
               for index in range(1, len(bars))):
            raise ValueError("Raw Upstox research candles are out of order or duplicated")
        if survivorship_aware and len(universe_snapshot_sha256) != 64:
            raise ValueError("Survivorship claims require a pinned universe snapshot hash")
        dataset = cls(instrument, timeframe, tuple(bars), sha256_bytes(raw), "upstox", False,
                      True, survivorship_aware,
                      {"source_url": source_url, "raw_response_sha256": sha256_bytes(raw),
                       "universe_snapshot_sha256": universe_snapshot_sha256})
        dataset.validate()
        gaps = len(validate(list(dataset.bars), timeframe, strict_gaps=False).warnings)
        return replace(dataset, data_gaps=gaps)


@dataclass(frozen=True)
class ResearchConfig:
    train_bars: int = 120
    test_bars: int = 60
    folds: int = 3
    holdout_bars: int = 60
    embargo_bars: int = 2
    starting_equity: float = 100000.0
    per_trade_risk_fraction: float = 0.005
    commission_rate: float = 0.0005
    slippage_rate: float = 0.0005
    spread_rate: float = 0.0002
    max_participation: float = 0.10
    rejection_rate: float = 0.02
    gap_risk_fraction: float = 0.002
    reward_risk: float = 1.5
    swing_bars: int = 2
    forecast_horizon: int = 5

    def __post_init__(self):
        if self.train_bars < 60 or self.test_bars < 20 or self.folds < 2 or self.holdout_bars < 20:
            raise ValueError("Insufficient chronological train/test/holdout configuration")
        if self.embargo_bars < 1 or not 0 < self.per_trade_risk_fraction <= 0.01:
            raise ValueError("Invalid embargo or risk configuration")
        if not 0 <= self.rejection_rate <= 1 or not 0 < self.max_participation <= 1:
            raise ValueError("Invalid execution-model configuration")

    @property
    def sha256(self) -> str:
        return sha256_bytes(canonical(asdict(self)).encode())


def chronological_folds(count: int, config: ResearchConfig,
                        include_holdout: bool = True) -> tuple[list[tuple[int, int, int, int]], tuple[int, int] | None]:
    """Return (train_end,test_start,test_end,fold) and a reserved holdout."""
    reserve = config.holdout_bars if include_holdout else 0
    usable = count - reserve
    folds = []
    for fold in range(config.folds):
        train_end = config.train_bars + fold * config.test_bars
        test_start = train_end + config.embargo_bars
        test_end = test_start + config.test_bars
        if test_end > usable:
            raise ValueError("Not enough bars for the requested chronological folds")
        if test_start <= train_end or test_end <= test_start:
            raise ValueError("Fold boundaries overlap")
        folds.append((train_end, test_start, test_end, fold))
    holdout = (usable, count) if include_holdout else None
    if holdout and holdout[1] - holdout[0] != config.holdout_bars:
        raise ValueError("Holdout boundary is invalid")
    return folds, holdout


class HoldoutLocked(RuntimeError):
    pass


def _forecast_metrics(records: list[dict], errors: int = 0) -> dict:
    if not records:
        return {"status": "NO_REAL_FORECAST", "count": 0, "errors": errors, "calibration": []}
    errors = [record["actual"] - record["predicted"] for record in records]
    directions = [record["direction_correct"] for record in records]
    brier = [(record["up_probability"] - record["actual_up"]) ** 2 for record in records]
    coverage = [record["q10"] <= record["actual"] <= record["q90"] for record in records]
    bins = []
    for lower in (0.0, 0.2, 0.4, 0.6, 0.8):
        selected = [record for record in records
                    if lower <= record["up_probability"] < lower + 0.2
                    or (lower == 0.8 and record["up_probability"] == 1.0)]
        if selected:
            bins.append({"lower": lower, "count": len(selected),
                         "predicted_mean": statistics.mean(r["up_probability"] for r in selected),
                         "observed_frequency": statistics.mean(r["actual_up"] for r in selected)})
    return {
        "status": "EVALUATED", "count": len(records), "errors": errors,
        "mae": statistics.mean(abs(error) for error in errors),
        "rmse": math.sqrt(statistics.mean(error * error for error in errors)),
        "direction_accuracy": statistics.mean(directions),
        "brier_score": statistics.mean(brier), "interval_coverage": statistics.mean(coverage),
        "calibration": bins,
    }


def _aggregate(results: list[dict]) -> dict:
    trades = [trade for result in results for trade in result["trade_log"]]
    forecasts = [record for result in results for record in result["forecast_records"]]
    forecast_errors = sum(result.get("forecast_errors", 0) for result in results)
    net = sum(result["net_total"] for result in results)
    drawdown = max((result["max_drawdown"] for result in results), default=0.0)
    regimes = {}
    for trade in trades:
        regime = trade.get("regime") or "UNKNOWN"
        regimes.setdefault(regime, []).append(trade["net"])
    return {
        "orders": sum(result["orders"] for result in results),
        "rejections": sum(result["rejections"] for result in results),
        "trades": len(trades), "net_total": net,
        "net_expectancy": statistics.mean([trade["net"] for trade in trades]) if trades else 0.0,
        "max_drawdown": drawdown,
        "risk_adjusted_score": net / max(drawdown, 1e-9),
        "win_rate": statistics.mean([trade["net"] > 0 for trade in trades]) if trades else 0.0,
        "instrument_regime": {key: {"trades": len(values), "net_expectancy": statistics.mean(values),
                                    "net_total": sum(values)} for key, values in regimes.items()},
        "forecast": _forecast_metrics(forecasts, forecast_errors),
        "trade_log": trades, "forecast_records": forecasts,
    }


def _run_segment(dataset: ResearchDataset, config: ResearchConfig, family: str, side: str,
                 start: int, end: int, forecast_provider: Callable | None,
                 holdout: bool) -> dict:
    bars = list(dataset.bars)
    item = dataset.instrument
    paper = PaperExecution(
        {item.instrument_key: item}, starting_equity=config.starting_equity,
        commission_rate=config.commission_rate, slippage_rate=config.slippage_rate,
        spread_rate=config.spread_rate, max_participation=config.max_participation,
        rejection_rate=config.rejection_rate,
    )
    from app.autonomy.models import DailyRisk
    daily = DailyRisk("research", config.starting_equity * 0.05,
                      config.per_trade_risk_fraction, config.starting_equity,
                      datetime.now(timezone.utc))
    forecast_records: list[dict] = []
    forecast_errors = 0
    pending: list[tuple[int, Forecast]] = []
    equity_curve = [config.starting_equity]
    last_day = None
    for index in range(start, end):
        bar = bars[index]
        day = bar.timestamp.date()
        if day != last_day:
            paper.day_realised = 0.0
            last_day = day
        paper.on_bar(bar)
        current = bars[:index + 1]  # only information available at this completed bar
        forecast = None
        if family == "KRONOS_INFORMED" and forecast_provider is not None:
            try:
                forecast = forecast_provider(
                    instrument_key=item.instrument_key, bars=current,
                    horizon=config.forecast_horizon, holdout=holdout,
                )
                if forecast is not None:
                    if forecast.input_end != bar.timestamp:
                        raise ValueError("Forecast input includes a different/future bar")
                    forecast.validate(bar.close)
                    pending.append((index, forecast))
            except Exception:
                # Missing/failed real inference is an explicit no-forecast
                # outcome, never a synthetic replacement.
                forecast_errors += 1
                forecast = None
        # Resolve forecast evaluation only when the future observation has
        # actually arrived; these values are never fed back into the signal.
        remaining = []
        for forecast_index, old_forecast in pending:
            horizon_index = forecast_index + old_forecast.horizon
            if horizon_index > index:
                remaining.append((forecast_index, old_forecast))
                continue
            if horizon_index >= len(bars):
                continue
            actual = bars[horizon_index].close
            prior_close = bars[forecast_index].close
            forecast_records.append({
                "predicted": old_forecast.mean_close, "actual": actual,
                "q10": old_forecast.q10_close, "q90": old_forecast.q90_close,
                "up_probability": old_forecast.up_probability,
                "actual_up": float(actual > prior_close),
                "direction_correct": float((old_forecast.mean_close > prior_close) == (actual > prior_close)),
                "artifact_digest": old_forecast.artifact_digest,
                "instrument_key": item.instrument_key,
            })
        pending = remaining
        output = generate_candidates(
            item.instrument_key, current, tick_size=item.tick_size, timeframe=dataset.timeframe,
            swing_bars=config.swing_bars, reward_risk=config.reward_risk, forecast=forecast,
            kronos_status="REAL_PROVIDER" if forecast_provider else "UNAVAILABLE",
            evidence=(), require_forecast_validation=False,
        )
        candidate = next((candidate for candidate in output.candidates
                          if candidate.family == family and candidate.side == side), None)
        has_working = any(record["instrument_token"] == item.instrument_key and
                          record["rules"][0]["status"] in {"OPEN", "PENDING", "SCHEDULED"}
                          for record in paper.gtt.values())
        if candidate is not None and item.instrument_key not in paper.positions and not has_working:
            try:
                size = stop_sized_quantity(
                    candidate, item, paper.account(), daily, bar_volume=bar.volume,
                    max_participation=config.max_participation,
                    gap_risk_fraction=config.gap_risk_fraction,
                    cost_rate=config.commission_rate + config.slippage_rate,
                )
                if size.quantity:
                    ablation_group = "TREND_FORECAST_ABLATION" \
                        if family in {"TREND_MOMENTUM", "KRONOS_INFORMED"} else family
                    order = ProtectedOrder(
                        intent_id=hashlib.sha256(
                            f"{item.instrument_key}|{bar.timestamp.isoformat()}|{ablation_group}|{side}".encode()
                        ).hexdigest()[:32], instrument_key=item.instrument_key, side=side,
                        quantity=size.quantity, limit_price=candidate.entry_price,
                        stop_price=candidate.stop_price, target_price=candidate.target_price,
                        margin_price=candidate.entry_price,
                    )
                    paper.place_protected_entry(
                        order, ExecutionPermit("research", "ENTRY", "research",
                                               bar.timestamp + timedelta(days=1)),
                        session_id="research", instrument=item,
                    )
                    # Set by exact order id, not by dictionary iteration assumptions.
                    for identifier, intent in paper.intent_for_id.items():
                        if intent == order.intent_id:
                            paper.gtt[identifier]["research_regime"] = candidate.regime
            except (ValueError, PermissionError):
                # A rejected candidate is an observed research outcome, not a
                # reason to alter prices or relax contract rules.
                continue
        equity_curve.append(paper.account().equity)
    if paper.positions:
        paper.close_all_for_research(bars[end - 1])
    equity_curve.append(paper.account().equity)
    peak = equity_curve[0]
    max_drawdown = 0.0
    for equity in equity_curve:
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    return {
        "orders": len(paper.gtt), "rejections": paper.rejections,
        "trade_log": paper.trade_log, "forecast_records": forecast_records,
        "forecast_errors": forecast_errors,
        "net_total": paper.account().equity - config.starting_equity,
        "max_drawdown": max_drawdown, "equity_curve": equity_curve,
    }


class KronosForecastProvider:
    """Adapt verified upstream inference to the leakage-safe research hook."""

    def __init__(self, service, timeframe: str):
        self.service = service
        self.timeframe = timeframe

    def __call__(self, *, instrument_key: str, bars: list[Candle], horizon: int,
                 holdout: bool) -> Forecast | None:
        if self.service.status().get("status") != "VERIFIED":
            return None
        return self.service.forecast(
            instrument_key, bars, timeframe=self.timeframe, horizon=horizon,
            validated=False, validation_id="",
        )


class ResearchRunner:
    def __init__(self, config: ResearchConfig | None = None,
                 forecast_provider: Callable | None = None):
        self.config = config or ResearchConfig()
        self.forecast_provider = forecast_provider

    def run(self, dataset: ResearchDataset, *, include_holdout: bool = True,
            output_dir: Path | None = None) -> dict:
        dataset.validate()
        folds, holdout = chronological_folds(len(dataset.bars), self.config, include_holdout)
        output_dir = output_dir or Path("data/autonomy/research")
        output_dir.mkdir(parents=True, exist_ok=True)
        lock_path = output_dir / "holdout.lock.json"
        if include_holdout:
            if lock_path.exists():
                raise HoldoutLocked("This research holdout is already locked; no repeated tuning")
            lock = {"dataset_sha256": dataset.data_sha256, "config_sha256": self.config.sha256,
                    "locked_at": datetime.now(timezone.utc).isoformat(), "holdout_runs": 1}
            temporary = lock_path.with_suffix(".tmp")
            temporary.write_text(canonical(lock), encoding="utf-8")
            temporary.replace(lock_path)
        results: dict[str, dict] = {}
        for family in FAMILIES:
            for side in ("BUY", "SELL"):
                fold_results = [
                    _run_segment(dataset, self.config, family, side, test_start, test_end,
                                 self.forecast_provider, False)
                    for _, test_start, test_end, _ in folds
                ]
                holdout_result = (_run_segment(dataset, self.config, family, side,
                                               holdout[0], holdout[1], self.forecast_provider, True)
                                  if holdout else None)
                combined = _aggregate(fold_results)
                final = _aggregate([holdout_result]) if holdout_result else combined
                key = f"{family}:{side}"
                results[key] = {
                    "walk_forward": combined, "holdout": final if holdout_result else None,
                    "evidence_metrics": final,
                }
        incremental = {}
        for side in ("BUY", "SELL"):
            kronos = results[f"KRONOS_INFORMED:{side}"]["evidence_metrics"]
            baseline = results[f"TREND_MOMENTUM:{side}"]["evidence_metrics"]
            incremental[side] = {
                "design": "paired_forecast_filter_ablation",
                "baseline": "TREND_MOMENTUM: identical underlying setup without Kronos filter",
                "same_underlying_setup": True,
                "paired_order_identity": True,
                "filter": "Kronos expected direction and directional probability >= 0.55",
                "delta_risk_adjusted_score": kronos["risk_adjusted_score"] - baseline["risk_adjusted_score"],
                "delta_net_expectancy": kronos["net_expectancy"] - baseline["net_expectancy"],
                "kronos_forecast": kronos["forecast"],
            }
        report = {
            "schema": "axiom.autonomy.research.v1", "generated_at": datetime.now(timezone.utc).isoformat(),
            "instrument_key": dataset.instrument.instrument_key, "timeframe": dataset.timeframe,
            "data_sha256": dataset.data_sha256, "config_sha256": self.config.sha256,
            "source": dataset.source, "synthetic": dataset.synthetic,
            "upstox_authoritative": dataset.upstox_authoritative,
            "survivorship_aware": dataset.survivorship_aware, "data_gaps": dataset.data_gaps,
            "walk_forward_runs": len(folds), "holdout_runs": 1 if holdout else 0,
            "holdout_locked": bool(holdout), "results": results, "incremental_value": incremental,
            "profitability_claim": "none", "live_readiness_claim": "none",
            "limitations": ["Historical/net-of-cost results are not future guarantees.",
                             "A comparator delta is not proof of causality.",
                             "Synthetic/CSV data cannot enable LIVE evidence."],
        }
        (output_dir / "report.json").write_text(canonical(report), encoding="utf-8")
        self._write_evidence(dataset, report, output_dir)
        return report

    def _write_evidence(self, dataset: ResearchDataset, report: dict, output_dir: Path) -> None:
        evidence_dir = output_dir / "evidence"
        evidence_dir.mkdir(exist_ok=True)
        for family in FAMILIES:
            for side in ("BUY", "SELL"):
                result = report["results"][f"{family}:{side}"]["evidence_metrics"]
                baseline = report["results"][f"TREND_MOMENTUM:{side}"]["evidence_metrics"] \
                    if family == "KRONOS_INFORMED" else {"risk_adjusted_score": 0.0}
                digests = {record["artifact_digest"] for record in result["forecast_records"]}
                evidence_id = hashlib.sha256(
                    f"{dataset.data_sha256}|{self.config.sha256}|{family}|{side}".encode()
                ).hexdigest()[:32]
                item = StrategyEvidence(
                    evidence_id=evidence_id, instrument_key=dataset.instrument.instrument_key,
                    strategy_family=family, side=side, timeframe=dataset.timeframe,
                    regime="ALL", generated_at=datetime.now(timezone.utc),
                    data_end=dataset.bars[-1].timestamp, data_sha256=dataset.data_sha256,
                    config_sha256=self.config.sha256, walk_forward_runs=report["walk_forward_runs"],
                    holdout_runs=report["holdout_runs"], trades=result["trades"],
                    net_expectancy=result["net_expectancy"], max_drawdown=result["max_drawdown"],
                    risk_adjusted_score=result["risk_adjusted_score"],
                    baseline_risk_adjusted_score=baseline["risk_adjusted_score"],
                    costs_modelled=True, gaps_modelled=True, partial_fills_modelled=True,
                    rejections_modelled=self.config.rejection_rate > 0,
                    survivorship_aware=dataset.survivorship_aware,
                    upstox_authoritative=dataset.upstox_authoritative,
                    holdout_locked=report["holdout_locked"], synthetic=dataset.synthetic,
                    kronos_required=family == "KRONOS_INFORMED",
                    kronos_artifact_digest=next(iter(digests)) if len(digests) == 1 else "",
                    breakdown={"instrument_regime": result["instrument_regime"],
                               "forecast": result["forecast"], "data_gaps": dataset.data_gaps,
                               "incremental_value": report["incremental_value"].get(side, {})},
                )
                raw = asdict(item)
                raw["generated_at"] = item.generated_at.isoformat()
                raw["data_end"] = item.data_end.isoformat()
                raw["schema"] = "axiom.autonomy.evidence.v1"
                (evidence_dir / f"{evidence_id}.json").write_text(canonical(raw), encoding="utf-8")


def _instrument_from_json(path: Path) -> Instrument:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("expiry"):
        raw["expiry"] = parse_timestamp(raw["expiry"])
    allowed = {field for field in Instrument.__dataclass_fields__}
    return Instrument(**{key: value for key, value in raw.items() if key in allowed})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Chronological AXIOM research; no live orders")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--csv", type=Path)
    source.add_argument("--upstox-json", type=Path)
    parser.add_argument("--instrument-metadata", type=Path, required=True)
    parser.add_argument("--timeframe", default="1m", choices=["1m", "5m", "15m", "30m"])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--known-synthetic", action="store_true")
    parser.add_argument("--kronos", action="store_true", help="Use only locally verified real Kronos artifacts")
    parser.add_argument("--kronos-manifest", type=Path, default=Path("config/kronos-small.json"))
    parser.add_argument("--kronos-root", type=Path, default=Path("data/autonomy/kronos"))
    parser.add_argument("--source-url", default="")
    parser.add_argument("--survivorship-aware", action="store_true")
    parser.add_argument("--universe-snapshot-sha256", default="")
    args = parser.parse_args(argv)
    instrument = _instrument_from_json(args.instrument_metadata)
    if args.csv:
        dataset = ResearchDataset.from_csv(args.csv, instrument, args.timeframe,
                                           synthetic=args.known_synthetic)
    else:
        dataset = ResearchDataset.from_upstox_json(
            args.upstox_json, instrument, args.timeframe, source_url=args.source_url,
            survivorship_aware=args.survivorship_aware,
            universe_snapshot_sha256=args.universe_snapshot_sha256,
        )
    provider = None
    if args.kronos:
        from app.autonomy.kronos import KronosService
        kronos = KronosService(args.kronos_manifest, args.kronos_root,
                               Path("app/autonomy/vendor/kronos"))
        kronos.verify()
        provider = KronosForecastProvider(kronos, args.timeframe)
    report = ResearchRunner(forecast_provider=provider).run(dataset, output_dir=args.output)
    print(json.dumps({"report": str(args.output / "report.json"),
                      "holdout_locked": report["holdout_locked"],
                      "live_eligible": False if not dataset.upstox_authoritative else "gated by evidence"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
