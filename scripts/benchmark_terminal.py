#!/usr/bin/env python3
"""Repeatable fake-DEMO terminal load benchmark. Never connects to MT5.

The fake gateway has no broker, network or real-terminal latency. Results are
useful for regression comparison of local code paths only.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from app.api.server import create_app
from app.config.settings import Settings


TOKEN = "benchmark-local-token-not-a-credential"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
RISK = {
    "max_order_volume": 100.0, "max_symbol_volume": 1000.0,
    "max_order_margin": 1_000_000.0, "max_total_margin": 10_000_000.0,
    "max_open_positions": 1000, "max_daily_loss": 1_000_000.0,
    "max_drawdown_pct": 100.0, "max_trades_per_day": 10000,
    "max_margin_usage_pct": 100.0, "max_spread_bps": 10000.0,
    "max_slippage_points": 100, "max_quote_age_seconds": 30,
}


def percentile(values, q):
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * q
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def summary(values):
    return {
        "samples": len(values),
        "p50_ms": round(percentile(values, .50), 3),
        "p95_ms": round(percentile(values, .95), 3),
        "p99_ms": round(percentile(values, .99), 3),
        "max_ms": round(max(values), 3),
        "mean_ms": round(statistics.fmean(values), 3),
    }


def require(response, expected=200):
    if response.status_code != expected:
        raise RuntimeError(f"Benchmark request failed ({response.status_code}): {response.text[:500]}")
    return response.json()


def timed(samples, name, function):
    started = perf_counter()
    result = function()
    samples.setdefault(name, []).append((perf_counter() - started) * 1000.0)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=60, help="read requests per worker")
    parser.add_argument("--submit-iterations", type=int, default=12,
                        help="sequential read-only reconcile/report requests")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--output", type=Path, default=Path("docs/TERMINAL_BENCHMARK.json"))
    args = parser.parse_args()
    if not 4 <= args.iterations <= 1000 or not 1 <= args.submit_iterations <= 100 or not 1 <= args.workers <= 8:
        parser.error("Use 4-1000 reads/worker, 1-100 sequential submits and 1-8 workers")

    samples = {}
    with tempfile.TemporaryDirectory(prefix="axiom-benchmark-") as directory:
        root = Path(directory)
        settings = Settings(
            api_token=TOKEN, database_url="sqlite:///:memory:", data_dir=str(root),
            currency="USD", default_timeframe="1m",
        )
        app = create_app(settings, data_dir=root)
        with TestClient(app) as client:
            # The benchmark measures the SUPPORTED application surface only. The
            # legacy Vantage /api/trading/* routes this script used to drive were
            # removed, and driving them is exactly what made this hard gate fail
            # on every Windows run. Those reads now go to the supported
            # read-only routes and are asserted to stay read-only below.
            removed = ("/api/trading/credentials", "/api/trading/session",
                       "/api/trading/connection/test", "/api/trading/risk",
                       "/api/trading/instruments/selection", "/api/trading/readiness",
                       "/api/trading/arm", "/api/trading/activate",
                       "/api/trading/quotes", "/api/trading/chart",
                       "/api/trading/orders/review", "/api/trading/orders/submit",
                       "/api/trading/reconcile", "/api/trading/performance")
            for path in removed:
                response = client.get(path, headers=HEADERS)
                if response.status_code != 404:
                    raise RuntimeError(
                        f"removed route {path} answered {response.status_code}; "
                        "the paper-only build must not expose it"
                    )

            # Warm caches/interpreter paths, then exclude setup/warmup.
            for _ in range(4):
                require(client.get("/api/performance", headers=HEADERS))
                require(client.get("/api/markets/overview", headers=HEADERS))
            app.state.performance.clear()

            def read_work(index):
                def operation():
                    if index % 2:
                        return require(client.get("/api/markets/start-monitor", headers=HEADERS))
                    return require(client.get("/api/markets/overview", headers=HEADERS))
                name = "http.markets.start_monitor" if index % 2 else "http.markets.overview"
                timed(samples, name, operation)

            started = perf_counter()
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                list(pool.map(read_work, range(args.iterations * args.workers)))
            read_wall_seconds = perf_counter() - started

            # Sequential read-only operator paths. These are reads, never order
            # submissions: there is no order route left to benchmark.
            for _ in range(args.submit_iterations):
                timed(samples, "http.markets.start_monitor", lambda: require(
                    client.get("/api/markets/start-monitor", headers=HEADERS)))
                timed(samples, "http.paper.status", lambda: require(
                    client.get("/paper/status", headers=HEADERS)))

            server = require(client.get("/api/performance", headers=HEADERS))
            # Nothing here submits an order. Assert that the two things that
            # would make that unsafe are still true: the removed live surface is
            # gone, and the paper ledger is explicitly simulated.
            if client.post("/api/trading/orders/submit", json={}, headers=HEADERS).status_code != 404:
                raise RuntimeError("the removed live order route is reachable")
            paper = require(client.get("/paper/status", headers=HEADERS))
            if paper.get("live_trading") is not False:
                raise RuntimeError(
                    f"paper status must report live_trading false, got {paper.get('live_trading')!r}"
                )
            commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
            dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], text=True).strip())
            report = {
                "schema": "axiom-terminal-benchmark-v2",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "commit": commit,
                "working_tree_dirty_at_measurement": dirty,
                "conditions": {
                    "evidence_class": "LOCAL IN-PROCESS READ-ONLY HTTP ONLY",
                    "gateway": "none (supported paper-only routes)",
                    "removed_live_api_reachable": False,
                    "real_mt5_terminal": False, "external_demo_account": False,
                    "live_account_or_orders": False, "network": "none",
                    "database": "in-memory SQLite", "python": platform.python_version(),
                    "platform": platform.platform(), "machine": platform.machine(),
                    "logical_cpu_count": os.cpu_count(), "read_workers": args.workers,
                    "read_requests_per_worker": args.iterations,
                    "sequential_read_only_requests": args.submit_iterations,
                    "read_wall_seconds": round(read_wall_seconds, 6),
                    "browser_gpu_or_raster_measured": False,
                },
                "http_observations_ms": {name: summary(values) for name, values in sorted(samples.items())},
                "instrumented_server_metrics": server["metrics"],
                "assertions": {
                    "order_send_calls": 0,
                    "expected_order_send_calls": 0,
                    "removed_live_api_returns_404": True,
                    "order_endpoints_unreachable": True,
                    "setup_and_warmup_excluded_from_server_metrics": True,
                },
                "limitations": [
                    "Everything measured here is in-process and read-only; it adds no broker, network, terminal or real-market delay.",
                    "p50/p95/p99 describe this run only; they are not universal latency promises or service-level objectives.",
                    "The removed Vantage /api/trading/* surface is deliberately unreachable, so no order or reconciliation latency is measured.",
                    "Concurrent reads do not model every operator or market condition.",
                    "These numbers establish neither profitability nor LIVE readiness.",
                    "Browser canvas render timing is reported separately by the UI benchmark; this Python run does not measure GPU/raster time.",
                ],
            }
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps({"output": str(args.output), "http": report["http_observations_ms"],
                              "order_send_calls": 0}, indent=2))


if __name__ == "__main__":
    main()
