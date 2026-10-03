#!/usr/bin/env python3
"""Compare Kronos against naive baselines on held-out folds.

The question this answers is the only one that matters before any forecast is
allowed near an order: is the model better than guessing? WalkForwardEvaluator
already ships three baselines (last_close, drift, moving_average), so this
script just makes them runnable and prints the comparison.

Runs baselines-only by default. That is deliberate and useful - it establishes
the numbers Kronos must beat, and it needs no model weights, no GPU and no
network. Pass --with-kronos to add the model once real weights are present.

    python tools/run_walkforward.py --csv data.csv --timeframe 1m
    python tools/run_walkforward.py --csv data.csv --with-kronos

No order is placed and no broker is constructed by this script.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASELINES = ("last_close", "drift", "moving_average")


def _load_bars(csv_path: str, timeframe: str):
    from axiom.market.replay import ReplayMarketDataProvider

    provider = ReplayMarketDataProvider.from_csv(csv_path, timeframe=timeframe)
    frames = provider.timeframes()
    if not frames:
        raise SystemExit(f"no bars found in {csv_path}")
    frame = timeframe if timeframe in frames else frames[0]
    instruments = provider.instruments(frame)
    if not instruments:
        raise SystemExit(f"no instruments found for timeframe {frame}")
    result = provider.fetch(instruments[0], frame)
    return list(result.bars), instruments[0], frame


def _report_table(report) -> str:
    """One row per origin, MAE/RMSE per forecaster, best in each row marked."""
    lines = []
    header = f"{'fold':>5} {'origin':>20} {'forecaster':>16} {'n':>5} {'MAE':>12} {'RMSE':>12} {'dir%':>7}"
    lines.append(header)
    lines.append("-" * len(header))
    for i, step in enumerate(report.steps, start=1):
        metrics = step.metrics()
        origin_label = step.origin_timestamp.strftime("%Y-%m-%d %H:%M")[:20]
        best = None
        best_mae = None
        for name, m in metrics.items():
            if m.mae is not None and (best_mae is None or m.mae < best_mae):
                best_mae, best = m.mae, name
        for name in sorted(metrics):
            m = metrics[name]
            mark = " *" if name == best else "  "
            dir_acc = "-" if m.directional_accuracy is None else f"{m.directional_accuracy * 100:.1f}"
            lines.append(
                f"{i:>5} {origin_label:>20} {name + mark:>16} "
                f"{m.n:>5} {m.mae:>12.6f} {m.rmse:>12.6f} {dir_acc:>7}"
            )
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--csv", required=True, help="OHLCV csv to evaluate on")
    parser.add_argument("--timeframe", required=True, help="e.g. 1m, 5m, 1D")
    parser.add_argument("--pred-len", type=int, default=8)
    parser.add_argument("--lookback", type=int, default=120)
    parser.add_argument("--max-context", type=int, default=512)
    parser.add_argument("--stride", type=int, default=1)
    parser.add_argument("--max-folds", type=int, default=10)
    parser.add_argument("--with-kronos", action="store_true",
                        help="include the real model (needs weights present)")
    parser.add_argument("--json", action="store_true", help="emit raw report json")
    args = parser.parse_args(argv)

    if args.with_kronos:
        # WalkForwardEvaluator expects a plain callable
        #   (window, future_timestamps) -> list[list[float]]   (forecast paths)
        # Wiring the real model means resolving weights and building that
        # callable from ForecastPipeline. That is NOT done here: it has never
        # been verified in this environment, and a guessed wiring would print
        # numbers that look authoritative and are not.
        raise SystemExit(
            "--with-kronos is not wired. Supply a forecaster callable "
            "(window, future) -> [[path], ...] built from ForecastPipeline "
            "with real weights present. Run without this flag for the "
            "baseline numbers the model must beat."
        )

    bars, instrument, frame = _load_bars(args.csv, args.timeframe)
    print(f"loaded {len(bars)} bars | instrument={instrument} | timeframe={frame}")

    from axiom.models import KronosConfig
    from axiom.research.walkforward import WalkForwardEvaluator

    config = KronosConfig(
        enabled=True,
        backend="upstream" if args.with_kronos else "stub",
        lookback=args.lookback,
        pred_len=args.pred_len,
        max_context=args.max_context,
    )

    forecaster = None
    print("baselines only - these are the numbers Kronos must beat")

    evaluator = WalkForwardEvaluator(
        bars=bars,
        config=config,
        instrument=instrument,
        timeframe=frame,
        kronos_forecaster=forecaster,
        baselines=BASELINES,
        stride=args.stride,
        max_folds=args.max_folds,
    )
    report = evaluator.run()

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, default=str))
        return 0

    print(f"\nfolds: {report.folds}")
    if not report.steps:
        for note in report.notes:
            print(f"  note: {note}")
        print("\nnot enough history for even one fold. Supply more bars.")
        return 2
    for note in report.notes:
        print(f"  note: {note}")
    print()
    print(_report_table(report))
    print("\n* = lowest MAE on that fold. If Kronos never wins, it is not adding value.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
