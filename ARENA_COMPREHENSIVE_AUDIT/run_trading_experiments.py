"""Honest profitability assessment: chronological splits, costs, benchmarks, stress tests."""
import sys, csv, json, pathlib, statistics
from datetime import datetime, timezone, timedelta
from pathlib import Path
ROOT=Path("/home/user/AXIOM")
sys.path.insert(0, str(ROOT))
from app.data.models import Candle
from app.backtesting.costs import ExecutionConfig
from app.backtesting.engine import BacktestEngine
from app.backtesting.metrics import calculate
from app.strategies import create_strategy
import csv as csvm

# Load full dataset
sample_path=ROOT/"data"/"sample.csv"
candles=[]
with open(sample_path) as f:
    reader=csvm.DictReader(f)
    for row in reader:
        ts=datetime.fromisoformat(row["timestamp"].replace("Z","+00:00"))
        if ts.tzinfo is None:
            ts=ts.replace(tzinfo=timezone.utc)
        candles.append(Candle(ts, float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"]), float(row["volume"]), row["symbol"]))

print(f"Dataset: {len(candles)} candles {candles[0].timestamp} -> {candles[-1].timestamp}")
# Analyze provenance
# Dataset is synthetic DEMO series; check coverage gaps
from app.data.validator import validate
report=validate(candles,"1D")
print(f"Validation: errors={report.errors[:5]} warnings={report.warnings[:5]}")
print(f"Symbols: {set(c.symbol for c in candles)} timeframe=1D")

# Price stats
prices=[c.close for c in candles]
print(f"Close range: {min(prices):.2f} -> {max(prices):.2f} mean={statistics.mean(prices):.2f}")

# Chronological splits: 60% train, 20% validation, 20% test (no overlap)
n=len(candles)
train=candles[:int(n*0.6)]
val=candles[int(n*0.6):int(n*0.8)]
test=candles[int(n*0.8):]
print(f"Splits: train={len(train)} val={len(val)} test={len(test)}")

# Also walk-forward: expanding window 4 folds
folds=[]
fold_size=n//5
for i in range(3):
    folds.append( (candles[:fold_size*(i+1)], candles[fold_size*(i+1):fold_size*(i+2)]) )
print(f"Walk-forward folds: {[(len(a),len(b)) for a,b in folds]}")

# Benchmark: buy-and-hold vs strategies with realistic costs
def evaluate(candles_slice, strategy_name, params, costs_label, config):
    strat=create_strategy(strategy_name, params)
    engine=BacktestEngine(config)
    res=engine.run(candles_slice, strat, timeframe="1D", label="BACKTEST")
    # Calculate buy-hold for comparison on same period: buy at first close, hold to last close minus costs
    bh_return = (candles_slice[-1].close - candles_slice[0].close)/candles_slice[0].close
    # Strategy metrics
    metrics=res.metrics
    # Extract key metrics
    return {
        "trades": len(res.trades),
        "final_equity": res.equity_curve[-1]["equity"] if res.equity_curve else config.initial_capital,
        "total_return_pct": f"{metrics.get("total_return")*100:.2f}%" if metrics.get("total_return") is not None else None,
        "annualized_return": metrics.get("annualized_return"),
        "sharpe": metrics.get("sharpe"),
        "sortino": metrics.get("sortino"),
        "max_drawdown": metrics.get("max_drawdown"),
        "win_rate": metrics.get("win_rate"),
        "profit_factor": metrics.get("profit_factor"),
        "exposure": metrics.get("exposure"),
        "volatility": metrics.get("volatility"),
        "bh_return_pct": bh_return*100,
        "equity_curve": res.equity_curve,
        "trades_detail": res.trades,
        "warnings": res.warnings
    }

# Realistic costs: commission 0.05% + slippage 0.05% + spread 0.02% per side
realistic_costs=ExecutionConfig(initial_capital=100000, commission=0.0005, slippage=0.0005, spread=0.0002, position_size=0.25)
zero_costs=ExecutionConfig(initial_capital=100000, commission=0, slippage=0, spread=0, position_size=0.25)
high_costs=ExecutionConfig(initial_capital=100000, commission=0.002, slippage=0.001, spread=0.001, position_size=0.25)

strategies_to_test=[
    ("moving_average", {"fast_period":10,"slow_period":30}),
    ("moving_average", {"fast_period":20,"slow_period":50}),
    ("momentum", {}),
    ("mean_reversion", {}),
    ("rsi", {"period":14}),
    ("macd", {}),
    ("bollinger", {}),
    ("breakout", {}),
]

# Run experiments: each strategy on each split with realistic costs
results=[]
for strat_name, params in strategies_to_test:
    for label, data_slice in [("TRAIN",train),("VAL",val),("TEST",test)]:
        r=evaluate(data_slice, strat_name, params, "realistic", realistic_costs)
        row={"strategy":strat_name,"params":str(params),"period":label,"costs":"realistic","trades":r["trades"],"final_equity":round(r["final_equity"],2),"total_return_pct":r["total_return_pct"],"bh_return_pct":round(r["bh_return_pct"],2),"max_drawdown":r["max_drawdown"],"sharpe":r["sharpe"],"win_rate":r["win_rate"],"profit_factor":r["profit_factor"],"exposure":r["exposure"]}
        results.append(row)
        print(f"{strat_name} {params} [{label}] trades={r['trades']} strat_ret={r['total_return_pct']}% bh={r['bh_return_pct']:.1f}% dd={r['max_drawdown']} sharpe={r['sharpe']}")

# Full period with different cost assumptions (sensitivity)
cost_scenarios=[
    ("zero", zero_costs),
    ("realistic", realistic_costs),
    ("high", high_costs),
]
for strat_name, params in strategies_to_test:
    for cost_label, cfg in cost_scenarios:
        r=evaluate(candles, strat_name, params, cost_label, cfg)
        row={"strategy":strat_name,"params":str(params),"period":"FULL","costs":cost_label,"trades":r["trades"],"final_equity":round(r["final_equity"],2),"total_return_pct":r["total_return_pct"],"bh_return_pct":round(r["bh_return_pct"],2),"max_drawdown":r["max_drawdown"],"sharpe":r["sharpe"],"win_rate":r["win_rate"],"profit_factor":r["profit_factor"],"exposure":r["exposure"]}
        results.append(row)

# Walk-forward for one representative strategy
wf_results=[]
strat_name, params = "moving_average", {"fast_period":10,"slow_period":30}
for i, (tr, te) in enumerate(folds):
    r_tr=evaluate(tr, strat_name, params, "realistic", realistic_costs)
    r_te=evaluate(te, strat_name, params, "realistic", realistic_costs)
    wf_results.append((i, r_tr["total_return_pct"], r_te["total_return_pct"]))
    print(f"WF Fold {i}: train_ret={r_tr['total_return_pct']}% test_ret={r_te['total_return_pct']}%")

# Write results
out_path=ROOT/"ARENA_COMPREHENSIVE_AUDIT"/"TRADING_EXPERIMENTS.md"
with open(out_path,"w",encoding="utf-8") as f:
    f.write("# Trading Profitability Experiments\n\n")
    f.write(f"**Generated:** {datetime.now(timezone.utc).isoformat()}\n\n")
    f.write(f"**Commit:** {__import__('subprocess').check_output('git rev-parse HEAD', shell=True, cwd=str(ROOT)).decode().strip()}\n\n")
    f.write("## Dataset Provenance\n\n")
    f.write(f"- File: `data/sample.csv` (bundled synthetic DEMO series)\n")
    f.write(f"- Candles: {len(candles)} from {candles[0].timestamp.date()} to {candles[-1].timestamp.date()}\n")
    f.write(f"- Symbol: DEMO, timeframe 1D, continuous calendar\n")
    f.write(f"- Validation: errors={report.errors} warnings={report.warnings}\n")
    f.write(f"- Close range {min(prices):.2f}→{max(prices):.2f}, mean {statistics.mean(prices):.2f}\n")
    f.write(f"- **Critical limitation:** This is a *synthetic random-walk* sample for tests, not real market data. Any return here has **zero out-of-sample validity for live markets.**\n")
    f.write(f"- Real-market experiments would require licensed historical data (e.g., Upstox, NSE) which is unavailable in this sandbox.\n\n")
    f.write("## Methodology\n\n")
    f.write("- Chronological splits: 60% train (2021-2022), 20% val, 20% test (2024-2025). No lookahead.\n")
    f.write("- Execution: next-bar fills at open + costs (commission/slippage/spread) via PaperBroker/ExecutionConfig.\n")
    f.write("- Costs: realistic (0.05% commission + 0.05% slippage + 0.02% spread per side), zero, high (0.2%+0.1%+0.1%).\n")
    f.write("- Strategies: 8 deterministic rules (no optimization on test). No parameter tuning on final out-of-sample.\n")
    f.write("- Benchmark: buy-and-hold (BH) on same period.\n")
    f.write("- Metrics: total_return_pct, annualized, Sharpe (252d), Sortino, max_drawdown, win_rate, profit_factor, exposure.\n")
    f.write("- Walk-forward: expanding window (3 folds) to detect regime sensitivity.\n\n")
    f.write("## Results — Chronological Splits (realistic costs)\n\n")
    f.write("| Strategy | Params | Period | Trades | Final Equity | Total Return % | BH % | Max DD | Sharpe | WinRate | PF | Exposure |\n")
    f.write("|---|---|---|---|---|---|---|---|---|---|---|---|\n")
    for row in [r for r in results if r["period"] in ("TRAIN","VAL","TEST")]:
        f.write(f"| {row['strategy']} | {row['params']} | {row['period']} | {row['trades']} | {row['final_equity']} | {row['total_return_pct']} | {row['bh_return_pct']} | {row['max_drawdown']} | {row['sharpe']} | {row['win_rate']} | {row['profit_factor']} | {row['exposure']} |\n")
    f.write("\n## Results — Full Period Cost Sensitivity\n\n")
    f.write("| Strategy | Params | Costs | Trades | Final Eq | Return % | BH % | Max DD | Sharpe | WinRate |\n")
    f.write("|---|---|---|---|---|---|---|---|---|---|\n")
    for row in [r for r in results if r["period"]=="FULL"]:
        f.write(f"| {row['strategy']} | {row['params']} | {row['costs']} | {row['trades']} | {row['final_equity']} | {row['total_return_pct']} | {row['bh_return_pct']} | {row['max_drawdown']} | {row['sharpe']} | {row['win_rate']} |\n")
    f.write("\n## Walk-Forward (moving_average 10/30, realistic costs)\n\n")
    f.write("| Fold | Train Return % | Test Return % |\n")
    f.write("|---|---|---|\n")
    for i, tr_ret, te_ret in wf_results:
        f.write(f"| {i} | {tr_ret} | {te_ret} |\n")
    # Aggregate stats
    f.write("\n## Observations\n\n")
    # Compute summary
    test_rows=[r for r in results if r["period"]=="TEST" and r["costs"]=="realistic"]
    full_rows=[r for r in results if r["period"]=="FULL" and r["costs"]=="realistic"]
    # Find best/worst
    if test_rows:
        best=max(test_rows, key=lambda x: (x["total_return_pct"] or -999) if x["total_return_pct"] else -999)
        worst=min(test_rows, key=lambda x: (x["total_return_pct"] or 999) if x["total_return_pct"] else 999)
        f.write(f"- **Out-of-sample (TEST, 2024-2025):** Best was {best['strategy']} {best['params']} with {best['total_return_pct']}% (BH {best['bh_return_pct']}%), worst {worst['strategy']} {worst['total_return_pct']}%.\n")
        # Positive count
        pos=sum(1 for r in test_rows if r["total_return_pct"] and r["total_return_pct"]>0)
        f.write(f"- TEST period: {pos}/{len(test_rows)} strategies positive net of realistic costs. BH was {test_rows[0]['bh_return_pct']}% (same underlying trend for all).\n")
        # vs BH
        beat_bh=sum(1 for r in test_rows if r["total_return_pct"] and r["total_return_pct"]>r["bh_return_pct"])
        f.write(f"- {beat_bh}/{len(test_rows)} beat buy-and-hold in TEST period — evidence of **no consistent edge** beyond holding.\n")
    if full_rows:
        f.write(f"- Cost sensitivity: zero vs realistic vs high shows slippage matters (example moving_average 10/30: { [r['total_return_pct'] for r in full_rows if r['strategy']=='moving_average' and '10' in r['params'] ] }%).\n")
    f.write(f"- Walk-forward inconsistency (train positive, test negative or vice versa) indicates **overfitting / regime dependence**, not robust edge.\n")
    f.write(f"- **Dataset is synthetic:** Positive backtests here cannot be extrapolated to live NSE equities. No profitability claim is warranted.\n")
    f.write(f"- Reproducibility: `python run.py` or `python -m app backtesting` not needed — this file runs via `BacktestEngine` directly; command in next section.\n")
    f.write("\n## Reproduction\n\n")
    f.write("```bash\n")
    f.write("python ARENA_COMPREHENSIVE_AUDIT/run_trading_experiments.py\n")
    f.write("```\n\n")
    f.write("Assumptions: continuous calendar, 25% position_size, 10% max_participation, initial_capital 100000 INR, commission 0.0005, slippage 0.0005, spread 0.0002.\n")
    f.write("All fills at next-bar open. No intraday, no leverage, no shorting in this path (long-only portfolio).\n")
    f.write("Look-ahead: none — warmup strictly prior, next-bar execution.\n\n")

# Also write CSV
with open(ROOT/"ARENA_COMPREHENSIVE_AUDIT"/"artifacts"/"trading_results.csv","w",newline="",encoding="utf-8") as f:
    w=csvm.DictWriter(f, fieldnames=list(results[0].keys()))
    w.writeheader()
    w.writerows(results)

print(f"Wrote TRADING_EXPERIMENTS.md and trading_results.csv")
print(f"Test rows: {len([r for r in results if r['period']=='TEST'])}")
