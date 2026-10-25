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
from app.vantage.secrets import MemorySecretStore
from tests.mt5_fake import FakeMT5Gateway


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
    parser.add_argument("--submit-iterations", type=int, default=12)
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
            fake = FakeMT5Gateway(live=False)
            app.state.vantage.secret_store = MemorySecretStore()
            app.state.vantage.gateway_factory = lambda: fake
            credential = {
                "environment": "VANTAGE_DEMO", "login": str(fake.login_value),
                "password": "fake-benchmark-only-not-a-real-password", "server": fake.server,
                "terminal_path": "", "label": "fake benchmark", "replace_existing": False,
            }
            require(client.put("/api/trading/credentials", json=credential, headers=HEADERS))
            require(client.post("/api/trading/session", json={"mode": "VANTAGE_DEMO"}, headers=HEADERS))
            require(client.post("/api/trading/connection/test", headers=HEADERS))
            require(client.put("/api/trading/risk", json=RISK, headers=HEADERS))
            require(client.put("/api/trading/instruments/selection", json={"symbols": ["EURUSD"]}, headers=HEADERS))
            readiness = require(client.get("/api/trading/readiness", headers=HEADERS))
            armed = require(client.post("/api/trading/arm", json={
                "readiness_id": readiness["readiness_id"], "phrase": readiness["arm_phrase"],
            }, headers=HEADERS))
            require(client.post("/api/trading/activate", json={
                "activation_id": armed["activation_id"], "phrase": armed["activation_phrase"],
            }, headers=HEADERS))

            # Warm caches/JIT-like interpreter paths, then exclude setup/warmup.
            for _ in range(4):
                require(client.get("/api/trading/quotes", headers=HEADERS))
                require(client.get("/api/trading/chart?symbol=EURUSD&timeframe=1m&limit=500", headers=HEADERS))
            app.state.performance.clear()

            def read_work(index):
                def operation():
                    if index % 2:
                        return require(client.get("/api/trading/chart?symbol=EURUSD&timeframe=1m&limit=500", headers=HEADERS))
                    return require(client.get("/api/trading/quotes", headers=HEADERS))
                name = "http.chart.update" if index % 2 else "http.feed.quotes"
                timed(samples, name, operation)

            started = perf_counter()
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                list(pool.map(read_work, range(args.iterations * args.workers)))
            read_wall_seconds = perf_counter() - started

            for index in range(args.submit_iterations):
                payload = {
                    "symbol": "EURUSD", "side": "BUY", "order_type": "MARKET",
                    "volume": 0.01, "idempotency_key": f"benchmark-order-{index:08d}",
                }
                review = timed(samples, "http.order.review", lambda: require(client.post(
                    "/api/trading/orders/review", json=payload, headers=HEADERS,
                )))
                result = timed(samples, "http.order.submit", lambda: require(client.post(
                    "/api/trading/orders/submit", json={
                        **payload, "review_id": review["review_id"],
                        "confirmation": review["confirmation_phrase"],
                    }, headers=HEADERS,
                )))
                if not result.get("reconciled") or result.get("order", {}).get("status") != "FILLED":
                    raise RuntimeError("Fake benchmark order did not reconcile as FILLED")

            for _ in range(max(4, args.submit_iterations // 2)):
                timed(samples, "http.reconcile", lambda: require(client.post(
                    "/api/trading/reconcile", headers=HEADERS,
                )))

            server = require(client.get("/api/trading/performance", headers=HEADERS))
            if len(fake.send_calls) != args.submit_iterations:
                raise RuntimeError("Unexpected order_send count; benchmark safety invariant failed")
            commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
            dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], text=True).strip())
            report = {
                "schema": "axiom-terminal-benchmark-v1",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "commit": commit,
                "working_tree_dirty_at_measurement": dirty,
                "conditions": {
                    "evidence_class": "FAKE VANTAGE DEMO / LOCAL PROCESS ONLY",
                    "gateway": "tests.mt5_fake.FakeMT5Gateway",
                    "real_mt5_terminal": False, "external_demo_account": False,
                    "live_account_or_orders": False, "network": "none",
                    "database": "in-memory SQLite", "python": platform.python_version(),
                    "platform": platform.platform(), "machine": platform.machine(),
                    "logical_cpu_count": os.cpu_count(), "read_workers": args.workers,
                    "read_requests_per_worker": args.iterations,
                    "sequential_reviewed_demo_fakes": args.submit_iterations,
                    "read_wall_seconds": round(read_wall_seconds, 6),
                    "browser_gpu_or_raster_measured": False,
                },
                "http_observations_ms": {name: summary(values) for name, values in sorted(samples.items())},
                "instrumented_server_metrics": server["metrics"],
                "assertions": {
                    "order_send_calls": len(fake.send_calls),
                    "expected_order_send_calls": args.submit_iterations,
                    "all_fake_orders_reconciled_filled": True,
                    "setup_and_warmup_excluded_from_server_metrics": True,
                },
                "limitations": [
                    "The fake gateway responds in-process and adds no broker, network, terminal or real-market delay.",
                    "p50/p95/p99 describe this run only; they are not universal latency promises or service-level objectives.",
                    "Broker acknowledgement is separate from broker-confirmed fill state and from full API/reconciliation time.",
                    "Concurrent reads and sequential reviewed writes do not model every operator or market condition.",
                    "Fake/demo performance and fills establish neither profitability nor LIVE readiness.",
                    "Browser canvas render timing is reported separately by the UI benchmark; this Python run does not measure GPU/raster time.",
                ],
            }
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps({"output": str(args.output), "http": report["http_observations_ms"], "order_send_calls": len(fake.send_calls)}, indent=2))


if __name__ == "__main__":
    main()
