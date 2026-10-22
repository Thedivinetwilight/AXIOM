#!/usr/bin/env python3
"""Evaluate US equity strategies on a real, point-in-time US dataset.

This script is deliberately strict. It will not download prices, will not
generate sample prices, and will not print a performance number it cannot trace
to a delivered dataset with a declared source, licence, adjustment policy and
point-in-time universe. If no such dataset exists it writes a machine-readable
"what is missing" report and exits non-zero.

    python tools/us_equities_research.py --dataset data/us_equities --out results/us_equities
    python tools/us_equities_research.py --dataset ... --strategies momentum_trend,buy_hold
    python tools/us_equities_research.py --dataset ... --stress 3 --max-positions 10

Exit codes: 0 = report produced, 2 = dataset missing/invalid (nothing invented),
3 = the evaluation itself refused (e.g. too few sessions).

No order is placed and no broker is contacted by this script.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DEFAULT_DATASET = "data/us_equities"
DEFAULT_OUT = "results/us_equities"


def _requirement_report(problems: list[str], dataset_path: Path) -> dict:
    from app.research.us_equities.dataset import describe_requirements

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "BLOCKED_MISSING_DATA",
        "dataset_path": str(dataset_path),
        "problems": problems,
        "requirements": describe_requirements(),
        "what_is_needed": [
            "A vendor-licensed US equity daily-bar history with adjusted prices "
            "(split and dividend) covering at least one full bull and one full bear "
            "market, e.g. 2015-01-01 to today.",
            "A point-in-time universe file (symbol,start,end) built from historical "
            "index membership or a documented screen applied as-of each date.",
            "A corporate-actions file (symbol,ex_date,type,ratio,amount) so the "
            "adjustment policy can be audited.",
            "A benchmark series (e.g. SPY) delivered as bars in the same file.",
        ],
        "explicitly_not_done": [
            "No synthetic, demo or replay sample prices were substituted.",
            "No strategy was selected and no out-of-sample number is reported.",
        ],
        "how_to_produce_the_dataset": [
            "Download bars from a licensed provider (Alpaca historical bars with a "
            "paid SIP subscription, or equivalent).",
            "Stamp each daily bar at the New York session close and store UTC "
            "timestamps.",
            "Write manifest.json exactly as documented in "
            "docs/US_EQUITIES_PAPER.md § Research dataset contract.",
        ],
    }


def _markdown(report: dict) -> str:
    lines = ["# US equities research report", ""]
    dataset = report["dataset"]
    lines += [
        f"- Dataset: `{dataset['root']}` (fingerprint `{dataset['fingerprint'][:16]}`)",
        f"- Source: {dataset['source'].get('name')} — {dataset['source'].get('license')}",
        f"- Adjustment: {dataset['adjustment']} ({dataset['price_basis']})",
        f"- Universe: {dataset['universe_kind']}"
        + ("  **survivorship-biased**" if dataset["survivorship_biased"] else ""),
        f"- Symbols: {dataset['symbols']} · sessions: {dataset['sessions']} "
        f"({dataset['first_session']} → {dataset['last_session']})",
        f"- Benchmark: {dataset['benchmark']}",
        "",
        "## Splits",
        "",
        f"- train: {report['splits']['train'][0]} → {report['splits']['train'][1]}",
        f"- validation: {report['splits']['validation'][0]} → "
        f"{report['splits']['validation'][1]}",
        f"- test (untouched): {report['splits']['test'][0]} → "
        f"{report['splits']['test'][1]}",
        "",
        "## Selection",
        "",
        f"- Method: {report['selection']['method']}",
        f"- Selected: **{report['selection']['selected']}**",
        "",
        "| strategy | validation Sharpe | validation net return |",
        "| --- | --- | --- |",
    ]
    for row in report["selection"]["validation_ranking"]:
        lines.append(
            f"| {row['strategy']} | {_fmt(row['validation_sharpe'])} | "
            f"{_pct(row['validation_net_return'])} |"
        )
    lines += ["", "## Out-of-sample (test) — selected strategy", ""]
    if report["test_results"]:
        metrics = report["test_results"][0]["metrics"]
        lines += [
            "| metric | value |",
            "| --- | --- |",
            f"| net return | {_pct(metrics.get('total_return'))} |",
            f"| CAGR | {_pct(metrics.get('cagr'))} |",
            f"| Sharpe | {_fmt(metrics.get('sharpe'))} |",
            f"| Sortino | {_fmt(metrics.get('sortino'))} |",
            f"| max drawdown | {_pct(metrics.get('max_drawdown'))} |",
            f"| profit factor | {_fmt(metrics.get('profit_factor'))} |",
            f"| win rate | {_pct(metrics.get('win_rate'))} |",
            f"| trades | {_fmt(metrics.get('number_of_trades'))} |",
            f"| exposure | {_pct(metrics.get('exposure'))} |",
            f"| turnover (1-way, annualized) | "
            f"{_fmt(metrics.get('turnover_one_way_annualized'))} |",
            f"| max single-position weight | "
            f"{_pct(metrics.get('max_single_position_weight'))} |",
            f"| transaction costs | {_fmt(metrics.get('transaction_costs'))} |",
        ]
    lines += ["", "## Cost stress (all strategies, test window)", "",
              "| strategy | net return | stressed net return |", "| --- | --- | --- |"]
    baseline = {row["strategy"]: row["metrics"].get("total_return")
                for row in report["test_results"]}
    for row in report["stress_results"]:
        lines.append(
            f"| {row['strategy']} | {_pct(baseline.get(row['strategy']))} | "
            f"{_pct(row['metrics'].get('total_return'))} |"
        )
    lines += ["", "## Walk-forward folds", ""]
    for result in report["test_results"]:
        for fold in result.get("folds", []):
            lines.append(
                f"- fold {fold['fold']}: {fold['start']} → {fold['end']} · "
                f"net {_pct(fold['net_return'])} · maxDD {_pct(fold['max_drawdown'])} · "
                f"Sharpe {_fmt(fold['sharpe'])} · trades {fold['trades']}"
            )
    lines += ["", "## Verdict", "",
              f"- status: **{report['verdict']['status']}**",
              f"- {report['verdict'].get('note', report['verdict'].get('reason', ''))}",
              "- paper_ready: false (this harness never certifies real-money use)", ""]
    lines += ["## Limitations", ""]
    lines += [f"- {item}" for item in report["limitations"]]
    lines.append("")
    return "\n".join(lines)


def _fmt(value):
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def _pct(value):
    if value is None:
        return "—"
    if isinstance(value, (int, float)):
        return f"{value * 100:.2f}%"
    return str(value)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dataset", default=DEFAULT_DATASET,
                        help="dataset directory containing manifest.json")
    parser.add_argument("--out", default=DEFAULT_OUT, help="output directory")
    parser.add_argument("--strategies", default="",
                        help="comma-separated candidate keys (default: all)")
    parser.add_argument("--max-positions", type=int, default=5)
    parser.add_argument("--folds", type=int, default=4)
    parser.add_argument("--stress", type=float, default=3.0,
                        help="multiplier applied to spread+slippage in the stress case")
    parser.add_argument("--train", type=float, default=0.5)
    parser.add_argument("--validation", type=float, default=0.2)
    parser.add_argument("--json", action="store_true", help="print the raw report")
    args = parser.parse_args(argv)

    dataset_path = Path(args.dataset)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    from app.research.us_equities.dataset import DatasetError, load_dataset

    try:
        dataset = load_dataset(dataset_path)
    except DatasetError as exc:
        report = _requirement_report(exc.problems, dataset_path)
        (out / "DATA_REQUIRED.json").write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )
        print("NO VALID US EQUITY DATASET — nothing was evaluated.")
        print(f"dataset: {dataset_path}")
        for problem in exc.problems:
            print(f"  - {problem}")
        print("\nWhat is required:")
        for item in report["what_is_needed"]:
            print(f"  * {item}")
        print("\nWhat was NOT done:")
        for item in report["explicitly_not_done"]:
            print(f"  * {item}")
        print(f"\nMachine-readable report: {out / 'DATA_REQUIRED.json'}")
        return 2

    from app.research.us_equities.harness import CostModel, evaluate
    from app.research.us_equities.strategies import CANDIDATES, get_candidate

    if args.strategies.strip():
        candidates = [get_candidate(key.strip())
                      for key in args.strategies.split(",") if key.strip()]
    else:
        candidates = list(CANDIDATES)
    if not any(candidate.key == "buy_hold" for candidate in candidates):
        print("note: buy_hold was not requested; the report will not have a "
              "buy-and-hold baseline", file=sys.stderr)

    try:
        report = evaluate(
            dataset, candidates,
            costs=CostModel(),
            max_positions=args.max_positions,
            fold_count=args.folds,
            stress=args.stress,
            train=args.train,
            validation=args.validation,
        )
    except ValueError as exc:
        print(f"evaluation refused: {exc}")
        return 3

    payload = report.to_dict()
    (out / "report.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    (out / "REPORT.md").write_text(_markdown(payload), encoding="utf-8")
    for result in payload["test_results"]:
        rows = result.get("equity_curve") or []
        path = out / f"equity_{result['strategy']}.csv"
        path.write_text("date,equity\n" + "\n".join(
            f"{row['session_date']},{row['equity']:.2f}" for row in rows
        ) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(_markdown(payload))
    print(f"\nwritten: {out / 'report.json'} and {out / 'REPORT.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
