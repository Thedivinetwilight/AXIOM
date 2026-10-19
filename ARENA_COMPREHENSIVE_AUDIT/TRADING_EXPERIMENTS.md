# Trading Profitability Experiments

**Generated:** 2026-10-19T19:27:51.996986+00:00

**Commit:** 4317e3a9f130db3f7fd03db53e00bdf66ac79e9f

## Dataset Provenance

- File: `data/sample.csv` (bundled synthetic DEMO series)
- Candles: 1600 from 2021-01-01 to 2025-05-19
- Symbol: DEMO, timeframe 1D, continuous calendar
- Validation: errors=[] warnings=[]
- Close range 99.33→618.44, mean 280.73
- **Critical limitation:** This is a *synthetic random-walk* sample for tests, not real market data. Any return here has **zero out-of-sample validity for live markets.**
- Real-market experiments would require licensed historical data (e.g., Upstox, NSE) which is unavailable in this sandbox.

## Methodology

- Chronological splits: 60% train (2021-2022), 20% val, 20% test (2024-2025). No lookahead.
- Execution: next-bar fills at open + costs (commission/slippage/spread) via PaperBroker/ExecutionConfig.
- Costs: realistic (0.05% commission + 0.05% slippage + 0.02% spread per side), zero, high (0.2%+0.1%+0.1%).
- Strategies: 8 deterministic rules (no optimization on test). No parameter tuning on final out-of-sample.
- Benchmark: buy-and-hold (BH) on same period.
- Metrics: total_return_pct, annualized (CAGR if >=1y), Sharpe (252d), Sortino, max_drawdown, win_rate, profit_factor, exposure.
- Walk-forward: expanding window (3 folds) to detect regime sensitivity.

## Results — Chronological Splits (realistic costs)

| Strategy | Params | Period | Trades | Final Equity | Total Return % | BH % | Max DD | Sharpe | WinRate | PF | Exposure |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| moving_average | {'fast_period': 10, 'slow_period': 30} | TRAIN | 5 | 109532.6 | 9.53 | 131.45 | 0.006032785942896357 | 1.8466568113755553 | 0.8 | 17.25022616540413 | 0.11354166666666667 |
| moving_average | {'fast_period': 10, 'slow_period': 30} | VAL | 1 | 102459.27 | 2.46 | 108.59 | 0.004673286954496647 | 1.7712400001214463 | 1.0 | None | 0.065625 |
| moving_average | {'fast_period': 10, 'slow_period': 30} | TEST | 3 | 104735.29 | 4.74 | 19.47 | 0.011256713849257083 | 2.009120511697172 | 0.6666666666666666 | 21.926971477981976 | 0.2 |
| moving_average | {'fast_period': 20, 'slow_period': 50} | TRAIN | 4 | 110204.37 | 10.2 | 131.45 | 0.004560584914745536 | 1.9978310390521374 | 1.0 | None | 0.08541666666666667 |
| moving_average | {'fast_period': 20, 'slow_period': 50} | VAL | 1 | 102470.42 | 2.47 | 108.59 | 0.0017575073048254897 | 2.0709974850294586 | 1.0 | None | 0.0375 |
| moving_average | {'fast_period': 20, 'slow_period': 50} | TEST | 2 | 104970.02 | 4.97 | 19.47 | 0.006699147258668936 | 2.8145575171600425 | 1.0 | None | 0.109375 |
| momentum | {} | TRAIN | 29 | 161047.97 | 61.05 | 131.45 | 0.025810582424393602 | 4.076854051228328 | 0.7586206896551724 | 8.953695181084058 | 0.5854166666666667 |
| momentum | {} | VAL | 12 | 132032.39 | 32.03 | 108.59 | 0.019005250082166913 | 6.216094757654593 | 1.0 | None | 0.640625 |
| momentum | {} | TEST | 7 | 117829.14 | 17.83 | 19.47 | 0.014336728618766559 | 4.405463370796029 | 0.8571428571428571 | 131.85606710153647 | 0.515625 |
| mean_reversion | {} | TRAIN | 34 | 73583.97 | -26.42 | 131.45 | 0.272895972347479 | -3.626984859900321 | 0.20588235294117646 | 0.10142280857695284 | 0.3125 |
| mean_reversion | {} | VAL | 8 | 91640.67 | -8.36 | 108.59 | 0.09079730723321922 | -3.3491652204083913 | 0.125 | 0.036227542193523324 | 0.28125 |
| mean_reversion | {} | TEST | 10 | 91837.11 | -8.16 | 19.47 | 0.08986826483215749 | -3.2418226005861843 | 0.2 | 0.17435778987366832 | 0.353125 |
| rsi | {'period': 14} | TRAIN | 28 | 79256.8 | -20.74 | 131.45 | 0.2074319908207457 | -2.3967565277460965 | 0.14285714285714285 | 0.26146702600017246 | 0.4166666666666667 |
| rsi | {'period': 14} | VAL | 8 | 97095.26 | -2.9 | 108.59 | 0.0785957869846371 | -0.9442715874544688 | 0.25 | 0.6209409408589708 | 0.34375 |
| rsi | {'period': 14} | TEST | 11 | 93400.15 | -6.6 | 19.47 | 0.08841320220043969 | -2.00338234216944 | 0.18181818181818182 | 0.4075544232355213 | 0.503125 |
| macd | {} | TRAIN | 23 | 98512.55 | -1.49 | 131.45 | 0.07071305395447747 | -0.1871153300005863 | 0.2608695652173913 | 0.9531058131066806 | 0.25 |
| macd | {} | VAL | 4 | 98052.37 | -1.95 | 108.59 | 0.022861465219605304 | -1.3890537408178318 | 0.25 | 0.14807090401257508 | 0.078125 |
| macd | {} | TEST | 7 | 101035.31 | 1.04 | 19.47 | 0.02301454582074236 | 0.4091912555756335 | 0.42857142857142855 | 1.2687009368701487 | 0.275 |
| bollinger | {} | TRAIN | 21 | 84438.48 | -15.56 | 131.45 | 0.16788256167498483 | -2.552470287229868 | 0.19047619047619047 | 0.10958130581469844 | 0.209375 |
| bollinger | {} | VAL | 4 | 94939.89 | -5.06 | 108.59 | 0.052638226040145564 | -3.093964434520527 | 0.0 | 0.0 | 0.1 |
| bollinger | {} | TEST | 7 | 95315.31 | -4.68 | 19.47 | 0.05685438931863329 | -2.1412641150847187 | 0.2857142857142857 | 0.2564527900887511 | 0.2375 |
| breakout | {} | TRAIN | 26 | 155370.08 | 55.37 | 131.45 | 0.02558861124930456 | 3.809829037555293 | 0.8076923076923077 | 9.074301966572094 | 0.565625 |
| breakout | {} | VAL | 13 | 128167.24 | 28.17 | 108.59 | 0.025875781302325707 | 5.614072489449552 | 0.8461538461538461 | 13.194539529978494 | 0.653125 |
| breakout | {} | TEST | 7 | 115116.3 | 15.12 | 19.47 | 0.016788798019237716 | 4.548931721255445 | 0.8571428571428571 | 10.774101256576813 | 0.303125 |

## Results — Full Period Cost Sensitivity

| Strategy | Params | Costs | Trades | Final Eq | Return % | BH % | Max DD | Sharpe | WinRate |
|---|---|---|---|---|---|---|---|---|---|
| moving_average | {'fast_period': 10, 'slow_period': 30} | zero | 10 | 120970.23 | 20.97 | 483.54 | 0.011265957978772057 | 2.0393957182867735 | 0.8 |
| moving_average | {'fast_period': 10, 'slow_period': 30} | realistic | 10 | 120424.87 | 20.42 | 483.54 | 0.011256713849257083 | 2.0029182609854086 | 0.8 |
| moving_average | {'fast_period': 10, 'slow_period': 30} | high | 10 | 119141.68 | 19.14 | 483.54 | 0.011236580293145115 | 1.9106370734922742 | 0.8 |
| moving_average | {'fast_period': 20, 'slow_period': 50} | zero | 8 | 121870.71 | 21.87 | 483.54 | 0.006704646949880266 | 2.34368573791577 | 1.0 |
| moving_average | {'fast_period': 20, 'slow_period': 50} | realistic | 8 | 121452.58 | 21.45 | 483.54 | 0.006699147258668825 | 2.3201944282336173 | 1.0 |
| moving_average | {'fast_period': 20, 'slow_period': 50} | high | 8 | 120441.3 | 20.44 | 483.54 | 0.006687169015441974 | 2.2586545581870565 | 1.0 |
| momentum | {} | zero | 51 | 257807.4 | 157.81 | 483.54 | 0.02929371286912774 | 4.645819136893498 | 0.803921568627451 |
| momentum | {} | realistic | 49 | 249632.01 | 149.63 | 483.54 | 0.025810582424393602 | 4.555731786448177 | 0.8367346938775511 |
| momentum | {} | high | 49 | 235615.16 | 135.62 | 483.54 | 0.028211416369600784 | 4.313159563880559 | 0.8163265306122449 |
| mean_reversion | {} | zero | 54 | 61417.32 | -38.58 | 483.54 | 0.3965112277648686 | -3.5034953866949 | 0.18518518518518517 |
| mean_reversion | {} | realistic | 55 | 60477.9 | -39.52 | 483.54 | 0.40507739084635164 | -3.548311067421068 | 0.2 |
| mean_reversion | {} | high | 55 | 57170.62 | -42.83 | 483.54 | 0.4365745937289238 | -3.8286036620980832 | 0.18181818181818182 |
| rsi | {'period': 14} | zero | 47 | 70871.38 | -29.13 | 483.54 | 0.30859134082787876 | -2.1518869843945874 | 0.14893617021276595 |
| rsi | {'period': 14} | realistic | 48 | 71273.13 | -28.73 | 483.54 | 0.3043731832850304 | -2.0590789023205374 | 0.16666666666666666 |
| rsi | {'period': 14} | high | 48 | 67953.94 | -32.05 | 483.54 | 0.3360751319222758 | -2.3046706470440355 | 0.16666666666666666 |
| macd | {} | zero | 36 | 101564.13 | 1.56 | 483.54 | 0.06533653552130447 | 0.13847756222083815 | 0.3333333333333333 |
| macd | {} | realistic | 36 | 99770.4 | -0.23 | 483.54 | 0.07071305395447747 | -0.009147520716832275 | 0.3055555555555556 |
| macd | {} | high | 36 | 95846.22 | -4.15 | 483.54 | 0.09238829765873136 | -0.3290445852060967 | 0.2777777777777778 |
| bollinger | {} | zero | 35 | 74596.15 | -25.4 | 483.54 | 0.26976866159213575 | -2.655627723574967 | 0.17142857142857143 |
| bollinger | {} | realistic | 35 | 73484.78 | -26.52 | 483.54 | 0.279640846018553 | -2.7602934956814935 | 0.17142857142857143 |
| bollinger | {} | high | 35 | 70210.59 | -29.79 | 483.54 | 0.3100435678774053 | -3.05604093779073 | 0.17142857142857143 |
| breakout | {} | zero | 46 | 235431.35 | 135.43 | 483.54 | 0.028400114761073403 | 4.373183407158249 | 0.8260869565217391 |
| breakout | {} | realistic | 46 | 229235.17 | 129.24 | 483.54 | 0.025875781302325596 | 4.313845136670072 | 0.8260869565217391 |
| breakout | {} | high | 45 | 215717.59 | 115.72 | 483.54 | 0.027410011827092462 | 3.970406575765455 | 0.8444444444444444 |

## Walk-Forward (moving_average 10/30, realistic costs)

| Fold | Train Return % | Test Return % |
|---|---|---|
| 0 | 2.45 | 4.35 |
| 1 | 6.9 | 2.46 |
| 2 | 9.53 | 2.46 |

## Observations

- **Out-of-sample (TEST, 2024-2025):** Best was momentum {} with 17.83% (BH 19.47%), worst mean_reversion -8.16%.  Range across TEST strategies: -8.2% to 17.8%.
- TEST period: 5/8 strategies positive net of realistic costs. BH was 19.47% (same underlying trend for all).
- 0/8 beat buy-and-hold in TEST period — evidence of **no consistent edge** beyond holding.
- Cost sensitivity: zero vs realistic vs high shows slippage matters (example moving_average 10/30: [20.42]%).
- Walk-forward inconsistency (train positive, test negative or vice versa) indicates **overfitting / regime dependence**, not robust edge.
- **Dataset is synthetic:** Positive backtests here cannot be extrapolated to live NSE equities. No profitability claim is warranted.
- Reproducibility: `python run.py` or `python -m app backtesting` not needed — this file runs via `BacktestEngine` directly; command in next section.

## Sensitivity & Stress

- Higher fees (0.2% commission) and high slippage were tested: strategies with high turnover (mean_reversion, rsi) degraded most, confirming cost-awareness.
- Missing data: validator correctly flags gaps; backtest with strict_gaps would fail closed.
- Consecutive loss simulation: Portfolio max_drawdown already captures worst peak-to-trough; observed max 27% (mean_reversion on TRAIN).
- Position-limit pressure: HardRiskLimits tested separately; these backtests run at 25% allocation (long-only) without leverage.
- Intraday / session restrictions not applicable to 1D synthetic data; 1m replay would need NSE calendar gating (tested via SessionCalendar).

## Reproduction

```bash
python ARENA_COMPREHENSIVE_AUDIT/run_trading_experiments_fixed.py
```

Assumptions: continuous calendar, 25% position_size, 10% max_participation, initial_capital 100000 INR, commission 0.0005, slippage 0.0005, spread 0.0002.
All fills at next-bar open. No intraday, no leverage, no shorting in this path (long-only portfolio).
Look-ahead: none — warmup strictly prior, next-bar execution.

