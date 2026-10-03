"""Portable CLI for research, replay and paper trading.

This entry point exposes no broker selection: it always runs the in-process
PaperBroker. Real-money routing is only reachable through the gated
``python -m app engine`` path (see LIVE_TRADING_ENABLEMENT.md).
"""

import argparse
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import signal
import sys
from app.config.settings import ROOT, Settings
from app.backtesting.costs import ExecutionConfig
from app.backtesting.engine import BacktestEngine
from app.data.cache import DataCache
from app.data.historical import CSVProvider
from app.data.factory import stream_provider
from app.database.database import Database
from app.database.repository import Repository
from app.logging.logger import configure
from app.paper_trading.broker import PaperBroker
from app.paper_trading.simulator import PaperSimulator
from app.strategies import create_strategy
from app.tournament.runner import TournamentRunner, DEFAULT_GRIDS
from app.tournament.scoring import ScoringConfig
from app.tournament.selection import validate_candidate
from app.validation.train_test import SplitConfig
from app.validation.walk_forward import WalkForwardConfig, walk_forward
from app.validation.robustness import robustness


def parser():
    p = argparse.ArgumentParser(description="V0.6 research and PAPER ONLY trading")
    p.add_argument(
        "--mode",
        choices=[
            "backtest",
            "tournament",
            "validate",
            "walk-forward",
            "robustness",
            "paper",
            "api",
        ],
        default="paper",
    )
    p.add_argument("--csv", type=Path, default=ROOT / "data/sample.csv")
    p.add_argument("--symbol")
    p.add_argument("--timeframe", choices=["1m", "5m", "15m", "30m", "1h", "4h", "1D"])
    p.add_argument("--strategy", default="moving_average")
    p.add_argument("--parameters", default="{}", help="JSON strategy parameters")
    p.add_argument("--grids", type=Path, help="JSON parameter grid file")
    p.add_argument("--scoring", type=Path, help="JSON ScoringConfig file")
    p.add_argument(
        "--datasets", type=Path, help="JSON list of {csv,symbol,timeframe} for tournament"
    )
    p.add_argument("--splits", nargs=3, type=float, default=[0.6, 0.2, 0.2])
    p.add_argument(
        "--windows",
        nargs=4,
        type=int,
        default=[360, 120, 120, 120],
        metavar=("TRAIN", "VALIDATE", "TEST", "STEP"),
    )
    p.add_argument("--shortlist", type=int, default=5)
    p.add_argument("--source", choices=["replay", "binance", "generic", "upstox"])
    p.add_argument("--delay", type=float, default=0.0)
    p.add_argument("--database", help="SQLite URL (default from env)")
    p.add_argument("--output", type=Path)
    p.add_argument(
        "--annualization", type=int, default=365, help="365 for 24x7, 252 for typical equities"
    )
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--clean", action="store_true", help="Explicit CSV sort/exact deduplication")
    p.add_argument("--strict-gaps", action="store_true")
    p.add_argument("--calendar", choices=["continuous", "nse-session"], default=None)
    return p


def load_bars(path, symbol, timeframe, args, calendar):
    provider = CSVProvider(
        path,
        timeframe,
        DataCache(ROOT / "data/cache"),
        clean=args.clean,
        strict_gaps=args.strict_gaps,
        calendar=calendar,
    )
    bars = provider.historical(symbol, timeframe)
    if provider.warnings:
        print(
            f"Data warnings: {len(provider.warnings)}; first: {provider.warnings[0]}",
            file=sys.stderr,
        )
    return bars


async def run_paper(settings, args, repository, config, strategy, bars, calendar):
    broker = PaperBroker(
        config,
        currency=settings.currency,
        timeframe=settings.default_timeframe,
        calendar=calendar,
        repository=repository,
    )
    simulator = PaperSimulator(broker)
    provider = stream_provider(
        settings,
        args.source or settings.data_source,
        bars=bars,
        delay=args.delay,
        symbol=settings.default_symbol,
    )
    # Live data adapters report the symbols they actually subscribe to. This
    # was computed and then dropped, so the operator never saw which
    # instruments a live feed was really streaming — the one fact worth
    # confirming before trusting a paper session driven by real data.
    subscribed = list(getattr(provider, "symbols", None) or [settings.default_symbol])
    print(json.dumps({"event": "STREAM_SUBSCRIBED",
                      "source": args.source or settings.data_source,
                      "timeframe": settings.default_timeframe,
                      "symbols": subscribed}), flush=True)
    loop = asyncio.get_running_loop()

    def stop_requested():
        asyncio.create_task(simulator.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_requested)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await simulator.start(provider, strategy, [settings.default_symbol])
        await simulator.task
        result = {
            "label": "PAPER TRADING",
            "status": simulator.status(),
            "account": broker.get_balance(),
            "orders": broker.get_orders(),
            "trades": broker.portfolio.trades,
        }
        if simulator.error:
            result["warning"] = simulator.error
        return result
    finally:
        await simulator.stop()
        broker.close()


def main(argv=None):
    args = parser().parse_args(argv)
    settings = Settings.from_env()
    settings = replace(
        settings,
        default_symbol=args.symbol or settings.default_symbol,
        default_timeframe=args.timeframe or settings.default_timeframe,
        database_url=args.database or settings.database_url,
    )
    configure(settings.log_level, ROOT / "logs/bot.jsonl")
    if args.mode == "api":
        import uvicorn
        from app.api.server import create_app

        uvicorn.run(
            create_app(settings), host=args.host, port=args.port, log_level="info", access_log=False
        )
        return
    from app.paper_trading.execution import make_calendar

    calendar = make_calendar(args.calendar or settings.market_calendar, settings.default_timeframe)
    config = ExecutionConfig(**settings.execution_kwargs())
    strategy = create_strategy(args.strategy, json.loads(args.parameters))
    engine = BacktestEngine(config, annualization=args.annualization, calendar=calendar)
    grids = json.loads(args.grids.read_text()) if args.grids else DEFAULT_GRIDS
    scoring = (
        ScoringConfig(**json.loads(args.scoring.read_text())) if args.scoring else ScoringConfig()
    )
    symbol, timeframe = settings.default_symbol, settings.default_timeframe
    bars = None
    if args.mode != "paper" or (args.source or settings.data_source) == "replay":
        bars = load_bars(args.csv, symbol, timeframe, args, calendar)
    database = Database(settings.database_url)
    repository = Repository(database)
    output = args.output or ROOT / "results" / args.mode
    output.mkdir(parents=True, exist_ok=True)
    try:
        if args.mode == "backtest":
            result = engine.run(bars, strategy, timeframe)
            repository.save_backtest(result)
            result.save(output)
            print(
                json.dumps(
                    {"label": result.label, "id": result.id, "metrics": result.metrics}, indent=2
                )
            )
            return
        if args.mode == "tournament":
            datasets = {(symbol, timeframe): bars}
            if args.datasets:
                datasets = {}
                for entry in json.loads(args.datasets.read_text()):
                    path = Path(entry["csv"])
                    if not path.is_absolute():
                        path = args.datasets.parent / path
                    key = (entry["symbol"], entry["timeframe"])
                    if key in datasets:
                        raise ValueError("Duplicate dataset key")
                    datasets[key] = load_bars(path, *key, args, calendar)
            result = TournamentRunner(engine, scoring).run(datasets, grids, repository=repository)
        elif args.mode == "validate":
            result = validate_candidate(
                bars,
                timeframe,
                engine,
                grids=grids,
                splits=SplitConfig(*args.splits),
                scoring=scoring,
                shortlist=args.shortlist,
            )
        elif args.mode == "walk-forward":
            result = walk_forward(
                bars,
                timeframe,
                engine,
                grids=grids,
                scoring=scoring,
                config=WalkForwardConfig(*args.windows),
                shortlist=args.shortlist,
            )
        elif args.mode == "robustness":
            result = robustness(
                bars,
                timeframe,
                {"strategy": strategy.name, "parameters": strategy.parameters},
                engine,
            )
        else:
            result = asyncio.run(
                run_paper(settings, args, repository, config, strategy, bars, calendar)
            )
        (output / "result.json").write_text(json.dumps(result, indent=2, allow_nan=False))
        if args.mode != "paper":
            repository.save_research(args.mode, result)
        summary = {
            key: value
            for key, value in result.items()
            if key
            in {
                "label",
                "runs",
                "selected_candidate",
                "warnings",
                "account",
                "status",
                "aggregate_metrics",
                "split_sizes",
            }
        }
        print(json.dumps(summary, indent=2, allow_nan=False))
        print(f"Results: {output / 'result.json'}")
        if args.mode == "paper" and result["status"]["state"] == "ERROR":
            raise RuntimeError("Paper stream failed safely; see status/logs")
    finally:
        repository.release_writer()
        database.close()


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"Stopped safely: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
