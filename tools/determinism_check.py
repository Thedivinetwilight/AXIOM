"""Replay determinism check: the same inputs must produce the same decisions.

A backtest that does not reproduce cannot be trusted with money, and the usual
cause is silent: hash-order dependence, a set iterated for decisions, a
timestamp or uuid leaking into a comparison. This tool runs the *real*
backtest engine over a fixed synthetic market twice — in-process, and again in
fresh interpreter processes with different ``PYTHONHASHSEED`` values — and
compares a canonical fingerprint of the resulting orders, fills, trades and
metrics.

Usage::

    python tools/determinism_check.py                 # in-process + 2 seeds
    python tools/determinism_check.py --seeds 0,1,2   # more processes
    python tools/determinism_check.py --candles 400

Exit code 0 when every run agrees, 1 otherwise (and it prints the first
difference it found — a check that only says "different" is not actionable).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_CANDLES = 300
DEFAULT_SEEDS = ("0", "1")

#: keys that legitimately differ between runs (wall-clock/telemetry, not
#: decisions) and are dropped outright
VOLATILE_KEYS = {"created_at", "run_id", "ts", "duration_ms", "duration_seconds",
                 "elapsed_ms", "wall_clock_ms"}

#: keys holding an opaque identity (uuid4 etc.). Two runs legitimately mint
#: different ids, so the VALUE is not comparable — but the way ids are refereed
#: to (the same order appearing in two fills, a trade's entry/exit pairing) is.
IDENTIFIER_KEYS = {"id", "order_id", "intent_id", "trade_id", "fill_id",
                   "position_id", "result_id"}


def synthetic_candles(count: int = DEFAULT_CANDLES, *, symbol: str = "RELIANCE"):
    """A deterministic price path — same candles in every process, always."""
    from app.data.models import Candle

    rng = random.Random(20261002)  # fixed seed: the market is part of the input
    price = 100.0
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    candles = []
    for index in range(count):
        drift = rng.gauss(0.0008, 0.012)
        open_price = price
        close_price = max(1.0, open_price * (1 + drift))
        high = max(open_price, close_price) * (1 + abs(rng.gauss(0, 0.004)))
        low = min(open_price, close_price) * (1 - abs(rng.gauss(0, 0.004)))
        candles.append(Candle(
            timestamp=start + timedelta(days=index), open=round(open_price, 4),
            high=round(high, 4), low=round(low, 4), close=round(close_price, 4),
            volume=1000 + index, symbol=symbol))
        price = close_price
    return candles


def _canonical(value, labels: dict | None = None):
    """Round floats; drop wall-clock keys; relabel opaque identities.

    ``labels`` is a per-run map from an observed id to a stable label, so the
    same order is labelled the same way everywhere it appears while two
    different runs (which mint different uuid4s) stay comparable.
    """
    labels = {} if labels is None else labels
    if isinstance(value, dict):
        canonical = {}
        for key in sorted(value):
            name = str(key)
            if name in VOLATILE_KEYS:
                continue
            item = value[key]
            if name in IDENTIFIER_KEYS:
                if item is None:
                    canonical[name] = None
                else:
                    label = labels.get(str(item))
                    if label is None:
                        label = f"id-{len(labels) + 1}"
                        labels[str(item)] = label
                    canonical[name] = label
                continue
            canonical[name] = _canonical(item, labels)
        return canonical
    if isinstance(value, (list, tuple)):
        return [_canonical(item, labels) for item in value]
    if isinstance(value, float):
        return round(value, 8)
    return value


def fingerprint(result) -> dict:
    """Decision fingerprint of one backtest run + the material it used."""
    payload = _canonical({
        "strategy": result.strategy,
        "parameters": result.parameters,
        "symbol": result.symbol,
        "timeframe": result.timeframe,
        "start_date": result.start_date,
        "end_date": result.end_date,
        "metrics": result.metrics,
        "trades": result.trades,
        "orders": result.orders,
        "fills": result.fills,
        "equity_curve": result.equity_curve,
    })
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    return {
        "sha256": digest,
        "trades": len(result.trades),
        "orders": len(result.orders),
        "fills": len(result.fills),
        "equity_points": len(result.equity_curve),
        "final_equity": (round(float(result.equity_curve[-1]["equity"]), 6)
                         if result.equity_curve else None),
        "payload": payload,
    }


def run_once(*, candles: int = DEFAULT_CANDLES, strategy: str = "momentum",
             parameters: dict | None = None, timeframe: str = "1D") -> dict:
    """One full backtest run over the synthetic market."""
    from app.backtesting.engine import BacktestEngine
    from app.strategies import create_strategy

    parameters = dict(parameters or {"period": 5, "threshold": 0.0})
    engine = BacktestEngine()
    result = engine.run(synthetic_candles(candles),
                        create_strategy(strategy, parameters),
                        timeframe, label="BACKTEST")
    return fingerprint(result)


def _first_difference(left, right, path: str = "") -> str:
    """A human-readable location for the first disagreement."""
    if type(left) is not type(right):
        return f"{path or '<root>'}: {type(left).__name__} vs {type(right).__name__}"
    if isinstance(left, dict):
        for key in sorted(set(left) | set(right)):
            if key not in left:
                return f"{path}.{key}: missing on the left"
            if key not in right:
                return f"{path}.{key}: missing on the right"
            if left[key] != right[key]:
                return _first_difference(left[key], right[key], f"{path}.{key}")
        return ""
    if isinstance(left, list):
        if len(left) != len(right):
            return f"{path}: {len(left)} vs {len(right)} items"
        for index, (a, b) in enumerate(zip(left, right)):
            if a != b:
                return _first_difference(a, b, f"{path}[{index}]")
        return ""
    return f"{path}: {left!r} vs {right!r}"


def _subprocess_fingerprint(seed: str, *, candles: int) -> dict:
    """Run the same check in a fresh interpreter with a different hash seed."""
    env = dict(os.environ, PYTHONHASHSEED=str(seed))
    code = (
        "import json, sys;"
        f"sys.path.insert(0, {str(ROOT)!r});"
        "from tools.determinism_check import run_once;"
        f"print(run_once(candles={candles})['sha256'])"
    )
    completed = subprocess.run([sys.executable, "-c", code], env=env,
                               capture_output=True, text=True, cwd=str(ROOT),
                               timeout=600)
    if completed.returncode != 0:
        return {"seed": seed, "ok": False,
                "error": (completed.stderr or "").strip()[-400:] or "non-zero exit"}
    return {"seed": seed, "ok": True,
            "sha256": completed.stdout.strip().splitlines()[-1].strip('"')}


def check(*, candles: int = DEFAULT_CANDLES, seeds=DEFAULT_SEEDS,
          strategy: str = "momentum", parameters: dict | None = None) -> dict:
    """Run the in-process pair and the cross-process seeds; report honestly."""
    first = run_once(candles=candles, strategy=strategy, parameters=parameters)
    second = run_once(candles=candles, strategy=strategy, parameters=parameters)
    in_process_ok = first["sha256"] == second["sha256"]
    report: dict = {
        "scope": "REPLAY DETERMINISM CHECK",
        "candles": candles,
        "strategy": strategy,
        "parameters": parameters or {"period": 5, "threshold": 0.0},
        "in_process": {
            "ok": in_process_ok,
            "runs": [first["sha256"], second["sha256"]],
            "difference": "" if in_process_ok else _first_difference(
                first["payload"], second["payload"]),
        },
        "trades": first["trades"],
        "fills": first["fills"],
        "cross_process_checked": bool(seeds),
        "processes": [],
    }
    if not first["trades"]:
        report["note"] = ("the scenario produced no trades, so this run only "
                          "proves the pipeline is stable — not that order "
                          "decisions are")
    for seed in seeds:
        outcome = _subprocess_fingerprint(str(seed), candles=candles)
        outcome["matches"] = (outcome.get("sha256") == first["sha256"]
                              if outcome.get("ok") else False)
        report["processes"].append(outcome)
    # With no seeds requested the check is narrower, not failing: it says so via
    # cross_process_checked rather than pretending a process check happened.
    report["ok"] = bool(in_process_ok
                        and all(item["matches"] for item in report["processes"]))
    if report["ok"]:
        report["fingerprint"] = first["sha256"]
    else:
        report["diagnosis"] = _diagnose(report)
    return report


def _diagnose(report: dict) -> str:
    """Name the likely cause — "different" without a lead is not actionable."""
    if not report["in_process"]["ok"]:
        return ("Runs disagree inside one process, so the non-determinism is in "
                "the engine itself (not hash ordering): "
                + (report["in_process"]["difference"] or "unknown location"))
    mismatched = [item["seed"] for item in report["processes"]
                  if not item.get("matches")]
    failed = [item["seed"] for item in report["processes"] if not item.get("ok")]
    if failed:
        return (f"The replay could not complete under PYTHONHASHSEED={failed}; "
                "fix that error before trusting any determinism result.")
    if mismatched:
        return ("Runs agree in-process but differ across processes with different "
                f"PYTHONHASHSEED values ({mismatched}): a decision depends on "
                "string-hash ordering — look for set iteration or dict ordering "
                "feeding signals, sizing or order construction.")
    return "No cause determined."


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Replay determinism check")
    parser.add_argument("--candles", type=int, default=DEFAULT_CANDLES)
    parser.add_argument("--strategy", default="momentum")
    parser.add_argument("--seeds", default=",".join(DEFAULT_SEEDS),
                        help="PYTHONHASHSEED values for fresh processes")
    args = parser.parse_args(argv)
    seeds = [seed for seed in args.seeds.split(",") if seed.strip()]
    report = check(candles=args.candles, seeds=seeds, strategy=args.strategy)
    print(json.dumps(report, indent=2, default=str))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
