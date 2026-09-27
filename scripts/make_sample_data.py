"""Deterministic synthetic prices, not actual prices of any traded instrument."""

import math
from pathlib import Path
import random
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.data.models import Candle
from app.data.historical import write_csv
from app.config.settings import ROOT


def sample(symbol="DEMO", count=1600, step=86400, seed=17):
    rng = random.Random(seed)
    bars, price = [], 100.0
    start = datetime(2021, 1, 1, tzinfo=timezone.utc)
    for i in range(count):
        opening = price * (1 + rng.gauss(0, 0.003))
        drift = 0.0003 + 0.006 * math.sin(i / 31) + (0.0006 if i % 500 < 250 else -0.0006)
        price = max(5, opening * (1 + drift + rng.gauss(0, 0.009)))
        spread = rng.uniform(0.001, 0.014)
        bars.append(
            Candle(
                start + timedelta(seconds=i * step),
                opening,
                max(opening, price) * (1 + spread),
                min(opening, price) * (1 - spread),
                price,
                rng.randint(5000, 50000),
                symbol,
            )
        )
    return bars


def main():
    for filename, symbol, step, seed in (
        ("sample.csv", "DEMO", 86400, 17),
        ("sample_alt.csv", "ALT", 86400, 29),
        ("sample_1h.csv", "DEMO", 3600, 37),
        ("sample_alt_1h.csv", "ALT", 3600, 43),
    ):
        write_csv(ROOT / "data" / filename, sample(symbol, step=step, seed=seed))
    print("Wrote four SYNTHETIC datasets: two symbols x two timeframes")


if __name__ == "__main__":
    main()
