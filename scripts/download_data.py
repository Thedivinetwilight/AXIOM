"""Import/normalize CSV by default; explicit --provider binance enables a GET-only download."""

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config.settings import ROOT
from app.data.cache import DataCache
from app.data.historical import CSVProvider, BinanceHistorical, write_csv
from app.data.models import parse_timestamp


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--provider", choices=["csv", "binance"], default="csv")
    p.add_argument("--input", type=Path, default=ROOT / "data/sample.csv")
    p.add_argument("--symbol", default="DEMO")
    p.add_argument("--timeframe", default="1D")
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--output", type=Path, default=ROOT / "data/imported.csv")
    p.add_argument("--clean", action="store_true")
    args = p.parse_args()
    cache = DataCache(ROOT / "data/cache")
    provider = (
        CSVProvider(args.input, args.timeframe, cache, clean=args.clean)
        if args.provider == "csv"
        else BinanceHistorical(cache)
    )
    bars = provider.historical(
        args.symbol,
        args.timeframe,
        parse_timestamp(args.start) if args.start else None,
        parse_timestamp(args.end) if args.end else None,
    )
    write_csv(args.output, bars)
    print(f"Saved {len(bars)} normalized candles to {args.output}")
    for warning in getattr(provider, "warnings", [])[:10]:
        print("WARNING:", warning)


if __name__ == "__main__":
    main()
