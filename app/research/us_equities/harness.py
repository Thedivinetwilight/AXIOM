"""Walk-forward evaluation of US equity rules through the real paper broker.

The simulator here is not a second backtester. Every simulated order goes
through :class:`app.paper_trading.broker.PaperBroker` — the same code that runs
a live paper session — so commission, spread, slippage, volume participation
(partial fills), gaps and the cash-conservation rules are identical by
construction. That is the point: a research number that the paper engine cannot
reproduce is not evidence about the paper engine.

Timing model (no look-ahead):

* a daily bar is stamped at its New York session close and is known at that
  instant (the loader verifies the stamp);
* each date is processed in two phases: *fill* (every resting order fills from
  this bar's open, subject to volume participation) then *decide* (the rule sees
  closes up to and including this bar and submits orders timestamped at this
  close);
* a submitted order therefore fills at the next session's open, never at the
  price that produced the signal.

Costs are explicit, never implicit: commission per side, half-spread per side,
slippage per side, and a participation cap that produces partial fills. The
stress case multiplies spread and slippage without changing anything else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from math import sqrt
from statistics import mean
from zoneinfo import ZoneInfo

from app.backtesting.costs import ExecutionConfig
from app.backtesting.metrics import calculate
from app.markets.us_calendar import USEquitiesCalendar
from app.paper_trading.broker import PaperBroker
from app.research.us_equities.dataset import USEquityDataset
from app.research.us_equities.strategies import Candidate, StrategySpec

NY = ZoneInfo("America/New_York")


class ResearchSessionCalendar(USEquitiesCalendar):
    """US session calendar that also accepts session-close-stamped daily bars.

    The dataset loader has already proved each bar sits within five minutes of
    the real New York close, early closes included. This subclass keeps the
    genuine holiday/weekend rules and additionally treats that close instant as
    tradable, so a close-stamped bar is processed instead of silently dropped
    (dropping every bar would produce an empty, meaningless result).
    """

    def is_open(self, timestamp: datetime) -> bool:
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("US session calendar requires a timezone-aware timestamp")
        local = timestamp.astimezone(NY)
        if not self.is_trading_day(local.date()):
            return False
        close = datetime.combine(local.date(), self.close_time(local.date()), NY)
        if abs((local - close).total_seconds()) <= 300:
            return True
        return super().is_open(timestamp)


@dataclass(frozen=True)
class CostModel:
    """Documented cost assumptions. Changing these changes every number."""

    commission: float = 0.00003   # regulatory/all-in per side (~0.3 bps)
    spread: float = 0.0002        # full quoted spread; half paid per side
    slippage: float = 0.0003      # per side
    max_participation: float = 0.02
    initial_capital: float = 100_000.0

    def execution_config(self, *, stress: float = 1.0) -> ExecutionConfig:
        return ExecutionConfig(
            initial_capital=self.initial_capital,
            commission=self.commission,
            spread=self.spread * stress,
            slippage=self.slippage * stress,
            position_size=1.0,          # overridden per run by 1/max_positions
            stop_loss=None,             # exits are rule-driven; a daily stop
            take_profit=None,           # would only add intrabar path ambiguity
            max_participation=self.max_participation,
        )

    def to_dict(self, *, stress: float = 1.0) -> dict:
        return {
            "commission_per_side": self.commission,
            "half_spread_per_side": self.spread * stress / 2,
            "slippage_per_side": self.slippage * stress,
            "max_participation_of_bar_volume": self.max_participation,
            "stress_multiplier_on_spread_and_slippage": stress,
            "initial_capital": self.initial_capital,
            "notes": [
                "US equity commissions are commonly zero; the commission field "
                "models regulatory/sell-side fees only and is an assumption.",
                "Partial fills arise from the participation cap, not from a "
                "queue model: a 2% cap is optimistic for small caps.",
                "Borrow, shorting and locate costs are not modelled (long-only).",
            ],
        }


@dataclass
class RunResult:
    key: str
    label: str
    spec: StrategySpec
    start: date
    end: date
    snapshots: list[dict]
    trades: list[dict]
    fills: list[dict]
    metrics: dict
    folds: list[dict]
    warnings: list[str]
    cost_model: dict
    rebalances: int
    max_positions: int
    universe_size: int

    def to_dict(self) -> dict:
        return {
            "strategy": self.key,
            "label": self.label,
            "spec": {
                "key": self.spec.key,
                "rebalance_days": self.spec.rebalance_days,
                "min_history": self.spec.min_history,
                "regime_filter": self.spec.regime_filter,
                "parameters": self.spec.parameters,
                "rationale": self.spec.rationale,
            },
            "period": {"start": self.start.isoformat(), "end": self.end.isoformat()},
            "equity_curve": [
                {"session_date": row["session_date"],
                 "equity": round(row["equity"], 2),
                 "drawdown": round(1 - row["equity"] / max(
                     snapshot["equity"] for snapshot in self.snapshots[:index + 1]), 6)
                 if row["equity"] > 0 else 0.0}
                for index, row in enumerate(self.snapshots)
            ],
            "metrics": self.metrics,
            "folds": self.folds,
            "rebalances": self.rebalances,
            "max_positions": self.max_positions,
            "universe_size": self.universe_size,
            "execution": self.cost_model,
            "warnings": self.warnings,
        }


def _trading_days(dataset: USEquityDataset) -> list[date]:
    return dataset.dates


def _rolling_window(dataset: USEquityDataset, before: date, keep: int) -> dict:
    """Bars strictly before ``before`` per symbol, newest last (bounded memory)."""
    window = {}
    for symbol, rows in dataset.bars.items():
        prior = [bar for bar in rows if bar.timestamp.astimezone(NY).date() < before]
        window[symbol] = prior[-keep:] if keep else []
    return window


def run_candidate(
    dataset: USEquityDataset,
    candidate: Candidate,
    *,
    costs: CostModel | None = None,
    stress: float = 1.0,
    max_positions: int = 5,
    start: date | None = None,
    end: date | None = None,
    fold_count: int = 4,
) -> RunResult:
    """Run one rule over one date range and return metrics plus per-fold slices."""
    from app.strategies.base import Signal

    costs = costs or CostModel()
    days = [day for day in _trading_days(dataset)
            if (start is None or day >= start) and (end is None or day <= end)]
    if not days:
        raise ValueError("no bars in the requested window")

    # Buy-and-hold equal-weights the point-in-time universe as it stands on the
    # first evaluated session; every other rule is capped at ``max_positions``.
    if candidate.kind == "buy_hold":
        first_bars = dataset.bars_for(days[0])
        first_members = dataset.members(days[0]) & set(first_bars)
        max_positions = max(1, min(len(first_members) or max_positions, 50))
    config = ExecutionConfig(
        **{**costs.execution_config(stress=stress).__dict__,
           "position_size": 1.0 / max_positions}
    )
    broker = PaperBroker(config, currency="USD", timeframe="1D",
                         calendar=ResearchSessionCalendar())

    warnings: list[str] = []
    if len(days) < candidate.min_history + 2:
        warnings.append(
            f"{candidate.key}: only {len(days)} sessions in this window; the rule needs "
            f"{candidate.min_history}+ sessions of history before it can act"
        )
    if broker.portfolio.positions:  # pragma: no cover — a fresh broker is always flat
        raise ValueError("research broker must start flat")

    keep = candidate.min_history + 5
    window = _rolling_window(dataset, days[0], keep)
    benchmark_window = [
        bar for bar in dataset.benchmark_bars()
        if bar.timestamp.astimezone(NY).date() < days[0]
    ][-keep:]
    snapshots: list[dict] = []
    rebalances = 0
    universe_size = 0
    initialised = False

    for index, day in enumerate(days):
        bars = dataset.bars_for(day)
        if not bars:
            continue
        for symbol, bar in sorted(bars.items()):
            window.setdefault(symbol, []).append(bar)
            del window[symbol][:-keep]
            broker.on_bar(bar, persist=False)
        benchmark_window.extend(
            bar for bar in dataset.benchmark_bars()
            if bar.timestamp.astimezone(NY).date() == day
        )
        del benchmark_window[:-keep]

        members = dataset.members(day) & set(bars)
        universe_size = max(universe_size, len(members))
        if candidate.kind == "buy_hold":
            rebalance = not initialised
        else:
            rebalance = (index % candidate.rebalance_days) == 0
        if rebalance and members:
            rebalances += 1
            target = candidate.select(window, members, max_positions,
                                      benchmark_window)
            held = set(broker.portfolio.positions)
            stamp = max(bar.timestamp for bar in bars.values()).isoformat()
            if candidate.kind != "buy_hold":
                for symbol in sorted(held - set(target)):
                    broker.handle_signal(Signal.EXIT, symbol, stamp,
                                         candidate.key, persist=False)
            for symbol in target:
                if symbol in held:
                    continue
                broker.handle_signal(Signal.BUY, symbol, stamp,
                                     candidate.key, persist=False)
            initialised = True

        # Read-only mark: never re-roll the portfolio's own day boundary.
        stamp = max(bar.timestamp for bar in bars.values()).isoformat()
        equity = broker.portfolio.equity
        weights = {
            symbol: position.quantity
            * broker.portfolio.marks.get(symbol, position.entry_price) / equity
            for symbol, position in broker.portfolio.positions.items()
        } if equity > 0 else {}
        snapshots.append({
            "timestamp": stamp,
            "session_date": day.isoformat(),
            "currency": "USD",
            "cash": broker.portfolio.cash,
            "equity": equity,
            "realized_pnl": broker.portfolio.realized_pnl,
            "unrealized_pnl": broker.portfolio.unrealized_pnl,
            "fees": broker.portfolio.fees,
            "spread_slippage_cost": broker.portfolio.spread_slippage_cost,
            "exposed": bool(broker.portfolio.positions),
            "positions": [
                {"symbol": symbol, "quantity": position.quantity,
                 "entry_price": position.entry_price}
                for symbol, position in broker.portfolio.positions.items()
            ],
            "weights": weights,
        })

    broker.cancel_all(persist=False)
    delisted = sorted(
        symbol for symbol in broker.portfolio.positions
        if not dataset.bars_for(max(days)).get(symbol)
    )
    if broker.portfolio.positions:
        warnings.append(
            "open positions at the end of the window are marked at the final close, "
            "not artificially liquidated: the result is not a realized exit"
        )
    if delisted:
        warnings.append(
            "held symbols with no bar on the final session (delisting/suspension) are "
            "marked at their last trade: " + ", ".join(delisted[:10])
        )
    if not snapshots:
        raise ValueError("no evaluable sessions in the requested window")
    metrics = _metrics(snapshots, broker.portfolio.trades, config.initial_capital,
                       snapshots[0]["timestamp"], snapshots[-1]["timestamp"])
    metrics.update(_trading_metrics(snapshots, broker.fills))
    folds = _fold_metrics(snapshots, broker.portfolio.trades, fold_count)
    return RunResult(
        key=candidate.key,
        label=candidate.label,
        spec=StrategySpec.of(candidate),
        start=date.fromisoformat(snapshots[0]["session_date"]),
        end=date.fromisoformat(snapshots[-1]["session_date"]),
        snapshots=snapshots,
        trades=list(broker.portfolio.trades),
        fills=[_fill_dict(fill) for fill in broker.fills],
        metrics=metrics,
        folds=folds,
        warnings=warnings,
        cost_model=costs.to_dict(stress=stress),
        rebalances=rebalances,
        max_positions=max_positions,
        universe_size=universe_size,
    )


def _fill_dict(fill) -> dict:
    return {
        "order_id": fill.order_id, "symbol": fill.symbol, "side": fill.side,
        "quantity": fill.quantity, "price": fill.price, "fee": fill.fee,
        "timestamp": fill.timestamp, "reason": fill.reason,
    }


def _metrics(snapshots, trades, initial_capital, start, end) -> dict:
    base = calculate(
        snapshots, trades, initial_capital,
        annualization=252,
        start=datetime.fromisoformat(str(start)),
        end=datetime.fromisoformat(str(end)),
    )
    # Only the honest, decision-relevant keys are surfaced.
    keep = (
        "total_return", "cagr", "max_drawdown", "sharpe", "sortino", "volatility",
        "win_rate", "profit_factor", "expectancy", "number_of_trades",
        "average_win", "average_loss", "exposure", "commission_costs",
        "spread_slippage_costs", "transaction_costs", "cost_ratio", "observed_days",
    )
    return {key: base[key] for key in keep if key in base}


def _trading_metrics(snapshots, fills) -> dict:
    """Turnover (one-way notional / average equity, annualized) and concentration."""
    if not snapshots:
        return {}
    equities = [row["equity"] for row in snapshots]
    average_equity = mean(equities) if equities else 0.0
    notional = sum(abs(fill.quantity * fill.price) for fill in fills)
    years = max(len(snapshots), 1) / 252
    turnover = (notional / 2.0 / average_equity) if average_equity > 0 else 0.0
    weights = [row.get("weights") or {} for row in snapshots]
    max_weight = max((max(w.values()) if w else 0.0) for w in weights)
    concentration = max((sum(value ** 2 for value in w.values()) if w else 0.0)
                        for w in weights)
    return {
        "turnover_one_way_annualized": turnover / years if years > 0 else 0.0,
        "round_trip_notional": notional,
        "fills": len(fills),
        "max_single_position_weight": max_weight,
        "max_hhi": concentration,
        "average_open_positions": mean([len(row["positions"]) for row in snapshots]),
    }


def _fold_metrics(snapshots, trades, fold_count: int) -> list[dict]:
    if fold_count <= 0 or len(snapshots) < fold_count * 2:
        return []
    size = len(snapshots) // fold_count
    folds = []
    for index in range(fold_count):
        lo = index * size
        hi = len(snapshots) if index == fold_count - 1 else (index + 1) * size
        chunk = snapshots[lo:hi]
        if len(chunk) < 2:
            continue
        start_stamp = chunk[0]["timestamp"]
        chunk_trades = [
            trade for trade in trades
            if start_stamp <= str(trade.get("exit_timestamp", "")) <= chunk[-1]["timestamp"]
        ]
        metrics = _metrics(chunk, chunk_trades, chunk[0]["equity"],
                           chunk[0]["timestamp"], chunk[-1]["timestamp"])
        folds.append({
            "fold": index + 1,
            "start": chunk[0]["session_date"],
            "end": chunk[-1]["session_date"],
            "sessions": len(chunk),
            "start_equity": round(chunk[0]["equity"], 2),
            "end_equity": round(chunk[-1]["equity"], 2),
            "net_return": metrics.get("total_return"),
            "max_drawdown": metrics.get("max_drawdown"),
            "sharpe": metrics.get("sharpe"),
            "trades": metrics.get("number_of_trades"),
            "exposure": metrics.get("exposure"),
        })
    return folds


@dataclass
class Split:
    train: tuple[date, date]
    validation: tuple[date, date]
    test: tuple[date, date]

    def to_dict(self) -> dict:
        return {
            "train": [self.train[0].isoformat(), self.train[1].isoformat()],
            "validation": [self.validation[0].isoformat(), self.validation[1].isoformat()],
            "test": [self.test[0].isoformat(), self.test[1].isoformat()],
            "note": (
                "chronological and contiguous; the test window is never used to "
                "choose a strategy, parameters, or a pass/fail threshold"
            ),
        }


def chronological_split(days: list[date], *, train: float = 0.5,
                        validation: float = 0.2) -> Split:
    if not 0 < train < 1 or not 0 < validation < 1 or train + validation >= 1:
        raise ValueError("train/validation fractions must be positive and sum to < 1")
    if len(days) < 30:
        raise ValueError("a chronological split needs at least 30 sessions")
    first, last = days[0], days[-1]
    train_end = days[int(len(days) * train) - 1]
    validation_end = days[int(len(days) * (train + validation)) - 1]
    return Split(
        train=(first, train_end),
        validation=(train_end + timedelta(days=1), validation_end),
        test=(validation_end + timedelta(days=1), last),
    )


@dataclass
class EvaluationReport:
    dataset: dict
    splits: dict
    selection: dict
    validation_results: list[dict]
    test_results: list[dict]
    stress_results: list[dict]
    regimes: list[dict]
    verdict: dict
    limitations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "dataset": self.dataset,
            "splits": self.splits,
            "selection": self.selection,
            "validation_results": self.validation_results,
            "test_results": self.test_results,
            "stress_results": self.stress_results,
            "regimes": self.regimes,
            "verdict": self.verdict,
            "limitations": self.limitations,
        }


def _regime_windows(dataset: USEquityDataset, days: list[date],
                    window: int = 30) -> list[dict]:
    """Tag consecutive ~monthly blocks by benchmark trend and volatility."""
    rows = dataset.benchmark_bars()
    if not rows:
        return []
    closes = [(bar.timestamp.astimezone(NY).date(), bar.close) for bar in rows]
    blocks: list[dict] = []
    for index in range(0, len(days), window):
        block = days[index:index + window]
        if not block:
            continue
        upto = [close for day, close in closes if day <= block[-1]]
        if len(upto) < 200:
            continue
        trend = sum(upto[-200:]) / 200
        returns = [
            upto[i] / upto[i - 1] - 1 for i in range(max(1, len(upto) - 21), len(upto))
        ]
        blocks.append({
            "start": block[0].isoformat(),
            "end": block[-1].isoformat(),
            "benchmark_trend": "up" if upto[-1] > trend else "down",
            "benchmark_vol_21d_annualized": (
                (sum(r * r for r in returns) / len(returns)) ** 0.5 * sqrt(252)
                if returns else None
            ),
        })
    return blocks


def evaluate(
    dataset: USEquityDataset,
    candidates: list[Candidate],
    *,
    costs: CostModel | None = None,
    max_positions: int = 5,
    fold_count: int = 4,
    stress: float = 3.0,
    train: float = 0.5,
    validation: float = 0.2,
) -> EvaluationReport:
    """Chronological train/validation/test evaluation with a cost stress case."""
    costs = costs or CostModel()
    days = _trading_days(dataset)
    split = chronological_split(days, train=train, validation=validation)

    validation_runs = [
        run_candidate(dataset, candidate, costs=costs, max_positions=max_positions,
                      start=split.validation[0], end=split.validation[1],
                      fold_count=fold_count)
        for candidate in candidates
    ]
    ranked = sorted(
        validation_runs,
        key=lambda run: (run.metrics.get("sharpe") or -99),
        reverse=True,
    )
    selected = ranked[0] if ranked else None
    selection = {
        "method": (
            "the candidate with the highest NET Sharpe ratio on the validation "
            "window only; the test window is opened afterwards and once"
        ),
        "candidates_considered": [candidate.key for candidate in candidates],
        "validation_ranking": [
            {"strategy": run.key, "validation_sharpe": run.metrics.get("sharpe"),
             "validation_net_return": run.metrics.get("total_return")}
            for run in ranked
        ],
        "selected": selected.key if selected else None,
    }

    test_results, stress_results = [], []
    if selected is not None:
        selected_candidate = next(c for c in candidates if c.key == selected.key)
        test_run = run_candidate(
            dataset, selected_candidate, costs=costs, max_positions=max_positions,
            start=split.test[0], end=split.test[1], fold_count=fold_count,
        )
        test_results.append(test_run.to_dict())
        for candidate in candidates:
            stress_results.append(
                run_candidate(dataset, candidate, costs=costs, stress=stress,
                              max_positions=max_positions,
                              start=split.test[0], end=split.test[1],
                              fold_count=0).to_dict()
            )
        verdict = _verdict(selected, test_run, stress_results, dataset)
    else:  # pragma: no cover — candidates is never empty in practice
        verdict = {"status": "UNRESOLVED", "reason": "no candidate produced a run"}

    report = EvaluationReport(
        dataset={
            "root": str(dataset.root),
            "fingerprint": dataset.fingerprint(),
            "source": dataset.source,
            "adjustment": dataset.adjustment,
            "price_basis": dataset.price_basis,
            "universe_kind": dataset.universe_kind,
            "survivorship_biased": dataset.survivorship_biased,
            "benchmark": dataset.benchmark,
            "symbols": len(dataset.bars),
            "sessions": len(days),
            "first_session": days[0].isoformat() if days else None,
            "last_session": days[-1].isoformat() if days else None,
            "bar_timeframe": dataset.manifest.get("bar_timeframe"),
            "bar_timestamp_convention": dataset.manifest.get("bar_timestamp_convention"),
            "warnings": dataset.warnings,
        },
        splits=split.to_dict(),
        selection=selection,
        validation_results=[run.to_dict() for run in validation_runs],
        test_results=test_results,
        stress_results=stress_results,
        regimes=_regime_windows(dataset, days),
        verdict=verdict,
        limitations=[
            "Daily bars only: intraday behaviour, opening auctions and halts are not modelled.",
            "Long-only: no borrow, locate, recall or short-squeeze modelling.",
            "The participation cap approximates partial fills without a queue model.",
            "Survivorship, delisting returns and index reconstitution depend entirely on "
            "the delivered universe file; the harness can only refuse or flag them.",
            "A paper (or backtest) result is not evidence of live profitability; costs, "
            "latency and market impact in production will be worse than modelled.",
            "Choosing the best validation Sharpe is itself a selection step and inflates "
            "the expected out-of-sample result; the test window is reported once.",
        ],
    )
    return report


def _verdict(selected: RunResult, test_run: RunResult, stress: list[dict],
             dataset: USEquityDataset) -> dict:
    if dataset.survivorship_biased:
        return {
            "status": "UNRESOLVED",
            "reason": (
                "the delivered universe is a static survivor list, so no strategy can be "
                "selected from it: the winners were chosen with hindsight"
            ),
            "selected_strategy_from_validation": selected.key,
            "test_net_return": test_run.metrics.get("total_return"),
            "paper_ready": False,
        }
    sharpe = test_run.metrics.get("sharpe")
    net = test_run.metrics.get("total_return")
    stressed = next((row for row in stress if row["strategy"] == selected.key), None)
    stressed_return = (
        stressed["metrics"]["total_return"] if stressed else None
    )
    buy_hold = next((row for row in stress if row["strategy"] == "buy_hold"), None)
    buy_hold_return = buy_hold["metrics"]["total_return"] if buy_hold else None
    beats_cash = (net or 0) > 0
    beats_buy_hold = (
        net is not None and buy_hold_return is not None and net > buy_hold_return
    )
    survives_stress = stressed_return is not None and stressed_return > 0
    status = (
        "SUPPORTED" if beats_cash and beats_buy_hold and survives_stress
        else "NOT SUPPORTED"
    )
    return {
        "status": status,
        "selected_strategy_from_validation": selected.key,
        "test_net_return": net,
        "test_sharpe": sharpe,
        "buy_hold_test_net_return": buy_hold_return,
        "stressed_net_return": stressed_return,
        "beats_buy_and_hold_after_costs": beats_buy_hold,
        "survives_cost_stress": survives_stress,
        "paper_ready": False,
        "note": (
            "SUPPORTED means only that this rule beat cash and buy-and-hold inside this "
            "one historical test window after modelled costs. It is not a forecast and "
            "it does not authorize real-money trading."
        ),
    }
