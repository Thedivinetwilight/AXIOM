"""Chronological next-bar engine; the broker is the only execution implementation."""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4
from app.backtesting.costs import ExecutionConfig
from app.backtesting.metrics import calculate, daily_returns
from app.data.validator import validate
from app.paper_trading.broker import PaperBroker


@dataclass
class BacktestResult:
    id: str
    label: str
    strategy: str
    parameters: dict
    symbol: str
    timeframe: str
    start_date: str
    end_date: str
    metrics: dict
    equity_curve: list[dict]
    trades: list[dict]
    orders: list[dict]
    fills: list[dict]
    daily_returns: list[dict]
    position_history: list[dict]
    warnings: list[str]
    execution_config: dict
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self):
        return asdict(self)

    def save(self, directory: Path):
        import csv

        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "result.json").write_text(
            json.dumps(self.to_dict(), indent=2, allow_nan=False),
            encoding="utf-8",
        )
        for name, rows in (
            ("equity", self.equity_curve),
            ("trades", self.trades),
            ("daily_returns", self.daily_returns),
        ):
            if rows:
                with (directory / f"{name}.csv").open(
                    "w", newline="", encoding="utf-8"
                ) as file:
                    writer = csv.DictWriter(file, fieldnames=list(rows[0]))
                    writer.writeheader()
                    writer.writerows(rows)
        # Self-contained equity visualization, no heavy plotting dependency.
        if self.equity_curve:
            equities = [r["equity"] for r in self.equity_curve]
            low, high = min(equities), max(equities)
            points = " ".join(
                f"{30 + 940 * i / max(1, len(equities) - 1):.1f},{350 - 300 * (v - low) / max(1, high - low):.1f}"
                for i, v in enumerate(equities)
            )
            (directory / "equity.svg").write_text(
                f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 400"><rect width="1000" height="400" fill="#102033"/><text x="30" y="25" fill="white">BACKTEST equity — historical simulation, not a forecast</text><polyline points="{points}" fill="none" stroke="#52deb1" stroke-width="2"/><text x="30" y="385" fill="white">Min {low:.2f} / Max {high:.2f}</text></svg>',
                encoding="utf-8",
            )


class BacktestEngine:
    def __init__(
        self, config: ExecutionConfig | None = None, *, annualization: int = 252, calendar=None
    ):
        self.config = config or ExecutionConfig()
        self.annualization, self.calendar = annualization, calendar

    def run(self, candles, strategy, timeframe="1D", *, warmup=None, label="BACKTEST"):
        if label not in {"BACKTEST", "TRAINING", "VALIDATION", "UNSEEN TEST", "ROBUSTNESS"}:
            raise ValueError("Invalid research label")
        report = validate(candles, timeframe, calendar=self.calendar)
        report.require_valid()
        symbols = {b.symbol for b in candles}
        if len(symbols) != 1:
            raise ValueError("One symbol per backtest; use tournament for multiple symbols")
        history = list(warmup or [])
        if history:
            validate(history, timeframe).require_valid()
            if {b.symbol for b in history} != symbols or history[-1].available_at(
                timeframe
            ) > candles[0].timestamp:
                raise ValueError("Warmup must be same-symbol and strictly prior to evaluation")
        broker = PaperBroker(self.config, timeframe=timeframe, calendar=self.calendar)
        snapshots, positions = [], []
        # Warmup is indicator-only. No inherited position or pre-evaluation order.
        for candle in candles:
            snapshot = broker.on_bar(candle)
            if snapshot is None:
                continue
            history.append(candle)
            history = history[-strategy.history_limit :]
            signal = strategy.generate_signal(tuple(history))
            broker.handle_signal(
                signal, candle.symbol, candle.available_at(timeframe).isoformat(), strategy.name
            )
            snapshots.append(snapshot)
            positions.append(
                {"timestamp": snapshot["timestamp"], "positions": snapshot["positions"]}
            )
        # Never fill a last-candle signal using the same candle or invented prices.
        broker.cancel_all()
        warnings = list(report.warnings)
        if broker.portfolio.positions:
            warnings.append("Open positions are marked at final close, not artificially liquidated")
        if not snapshots:
            warnings.append("No tradable bars under configured calendar")
        warnings.append("Historical backtest results are not guarantees of future performance")
        return BacktestResult(
            uuid4().hex,
            label,
            strategy.name,
            strategy.parameters,
            candles[0].symbol,
            timeframe,
            candles[0].timestamp.isoformat(),
            candles[-1].available_at(timeframe).isoformat(),
            calculate(
                snapshots,
                broker.portfolio.trades,
                self.config.initial_capital,
                annualization=self.annualization,
                start=candles[0].timestamp,
                end=candles[-1].available_at(timeframe),
            ),
            snapshots,
            broker.portfolio.trades,
            broker.get_orders(),
            [asdict(f) for f in broker.fills],
            daily_returns(snapshots, self.config.initial_capital),
            positions,
            warnings,
            asdict(self.config),
        )
