"""Finite JSON-safe performance metrics based on UTC daily marked equity."""

from math import sqrt
from statistics import mean, pstdev, stdev


def daily_returns(snapshots: list[dict], initial_capital: float) -> list[dict]:
    daily = {}
    for row in snapshots:
        daily[row["timestamp"][:10]] = row["equity"]
    previous, result = initial_capital, []
    for day, equity in sorted(daily.items()):
        result.append({"date": day, "return": equity / previous - 1 if previous else 0.0})
        previous = equity
    return result


def calculate(snapshots, trades, initial_capital, *, annualization=252, start=None, end=None):
    if annualization <= 0:
        raise ValueError("annualization must be positive")
    returns = [row["return"] for row in daily_returns(snapshots, initial_capital)]
    end_equity = snapshots[-1]["equity"] if snapshots else initial_capital
    total_return = end_equity / initial_capital - 1
    deviation = stdev(returns) if len(returns) > 1 else 0.0
    downside = sqrt(mean([min(0, r) ** 2 for r in returns])) if returns else 0.0
    average = mean(returns) if returns else 0.0
    pnls = [trade["pnl"] for trade in trades]
    wins, losses = [p for p in pnls if p > 0], [p for p in pnls if p < 0]
    years = (end - start).total_seconds() / (365.25 * 86400) if start and end else 0.0
    # Short samples should not be advertised with annualized growth forecasts.
    cagr = (
        (end_equity / initial_capital) ** (1 / years) - 1 if years >= 1 and end_equity > 0 else None
    )
    peak, max_dd = initial_capital, 0.0
    for row in snapshots:
        peak = max(peak, row["equity"])
        max_dd = max(max_dd, 1 - row["equity"] / peak)
    quarter_returns = []
    for i in range(4):
        part = returns[i * len(returns) // 4 : (i + 1) * len(returns) // 4]
        if part:
            value = 1.0
            for r in part:
                value *= 1 + r
            quarter_returns.append(value - 1)
    instability = pstdev(quarter_returns) if len(quarter_returns) > 1 else 0.0
    commission_costs = snapshots[-1]["fees"] if snapshots else 0.0
    impact_costs = snapshots[-1].get("spread_slippage_cost", 0.0) if snapshots else 0.0
    return {
        "total_return": total_return,
        "cagr": cagr,
        "max_drawdown": max_dd,
        "sharpe": average / deviation * sqrt(annualization) if deviation else None,
        "sortino": average / downside * sqrt(annualization) if downside else None,
        "volatility": deviation * sqrt(annualization),
        "win_rate": len(wins) / len(pnls) if pnls else 0.0,
        "average_win": mean(wins) if wins else 0.0,
        "average_loss": mean(losses) if losses else 0.0,
        "profit_factor": sum(wins) / abs(sum(losses)) if losses else None,
        "expectancy": mean(pnls) if pnls else 0.0,
        "number_of_trades": len(pnls),
        "average_trade": mean(pnls) if pnls else 0.0,
        "largest_win": max(wins, default=0.0),
        "largest_loss": min(losses, default=0.0),
        "exposure": mean([float(s["exposed"]) for s in snapshots]) if snapshots else 0.0,
        "instability": instability,
        "stability": 1 / (1 + instability),
        "commission_costs": commission_costs,
        "spread_slippage_costs": impact_costs,
        "transaction_costs": commission_costs + impact_costs,
        "cost_ratio": (commission_costs + impact_costs) / initial_capital,
        "annualization": annualization,
        "observed_days": len(returns),
    }
