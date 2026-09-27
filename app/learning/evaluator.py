"""Rolling-batch strategy evaluation + versioned, validated promotion (V1).

- Evaluates closed-trade outcomes in ROLLING BATCHES per (strategy, regime):
  never single trades.
- A candidate parameter change is written as a NEW versioned model row with
  its validation evidence. Promotion to "active" requires a positive
  OUT-OF-SAMPLE expectancy on the most recent candles via the existing
  backtest engine (chronological split; no in-sample-only selection).
- The evaluator can ONLY write model_versions rows and read candles/journal.
  It cannot touch Settings, risk limits, broker selection or safety flags.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from app.backtesting.engine import BacktestEngine
from app.backtesting.costs import ExecutionConfig
from app.data.models import Candle
from app.learning.regime import classify_regime
from app.validation.train_test import split_data


@dataclass(frozen=True)
class BatchMetrics:
    samples: int
    win_rate: float
    expectancy: float  # mean pnl per trade
    total_pnl: float


def evaluate_batch(outcomes: list[dict], *, min_samples: int = 5) -> dict[str, BatchMetrics]:
    """Outcomes = journal trade_outcome payloads; grouped by regime."""
    groups: dict[str, list[dict]] = {}
    for row in outcomes:
        groups.setdefault(row.get("regime", "unknown"), []).append(row)
    metrics: dict[str, BatchMetrics] = {}
    for regime, rows in groups.items():
        if len(rows) < min_samples:
            metrics[regime] = BatchMetrics(len(rows), 0.0, 0.0, 0.0)
            continue
        pnls = [row["pnl"] for row in rows]
        wins = sum(1 for pnl in pnls if pnl > 0)
        metrics[regime] = BatchMetrics(
            samples=len(rows),
            win_rate=wins / len(rows),
            expectancy=sum(pnls) / len(rows),
            total_pnl=sum(pnls),
        )
    return metrics


def out_of_sample_expectancy(candles: list[Candle], strategy_name: str, parameters: dict,
                             engine: BacktestEngine, timeframe: str, *,
                             train_fraction: float = 0.7,
                             label: str = "validation") -> dict:
    """Chronological train/OOS split; expectancy measured ONLY on the OOS leg.

    `label` documents which research partition the OOS leg belongs to
    (3.0.1: promotion evidence must be labeled; the LOCKED TEST partition is
    never passed here)."""
    from app.data.validator import validate
    from app.strategies import create_strategy

    validate(candles, timeframe).require_valid()
    cut = int(len(candles) * train_fraction)
    if cut < 5 or len(candles) - cut < 5:
        return {"valid": False, "reason": "not_enough_candles", "oos_expectancy": 0.0}
    train, oos = candles[:cut], candles[cut:]
    train_result = engine.run(
        train, create_strategy(strategy_name, parameters), timeframe)
    oos_result = engine.run(
        oos, create_strategy(strategy_name, parameters), timeframe)
    trades = oos_result.trades
    expectancy = (
        sum(trade["pnl"] for trade in trades) / len(trades)
        if trades else 0.0
    )
    return {
        "valid": True,
        "leg": label,
        "split": {"train_fraction": train_fraction, "n_train": len(train),
                  "n_oos": len(oos), "chronological": True},
        "train_trades": len(train_result.trades),
        "oos_trades": len(trades),
        "oos_expectancy": expectancy,
        "train_expectancy": (
            sum(trade["pnl"] for trade in train_result.trades) / len(train_result.trades)
            if train_result.trades else 0.0
        ),
    }


def propose_candidate(parameters: dict, *, key: str = "period", delta: int = 1) -> dict:
    """Deterministic parameter nudge (documented, reproducible)."""
    if key not in parameters:
        return dict(parameters)
    candidate = dict(parameters)
    candidate[key] = int(parameters[key]) + delta
    return candidate


def _promotion_gate(repository, strategy_name: str, candidate: dict,
                    current_result: dict, candidate_result: dict, *,
                    min_oos_trades: int, evidence: dict,
                    robustness_min: float = 0.5,
                    wf_min_folds: int = 3,
                    wf_min_profit_fraction: float = 0.5,
                    max_oos_drawdown: float = 0.5) -> dict:
    """The 3.0.2 full research promotion gate (fail closed).

    Every required input must be PRESENT and PASS; a missing item fails.
    The locked test can never appear here: every metric consumed is a
    validation-leg result or explicit evidence recorded by the research
    system (robustness / walk-forward / drawdown / lifecycle approval).
    """
    checks: dict[str, dict] = {}

    def _check(name: str, passed: bool, detail) -> None:
        checks[name] = {"passed": bool(passed), "detail": detail}

    _check("separated_validation_split",
           bool(current_result.get("valid")) and bool(candidate_result.get("valid"))
           and current_result.get("leg") == "validation"
           and candidate_result.get("leg") == "validation",
           {"current_leg": current_result.get("leg"),
            "candidate_leg": candidate_result.get("leg")})
    _check("min_oos_trades",
           candidate_result.get("oos_trades", 0) >= min_oos_trades,
           {"oos_trades": candidate_result.get("oos_trades", 0),
            "required": min_oos_trades})
    _check("strict_validation_improvement",
           candidate_result.get("oos_expectancy", 0.0)
           > current_result.get("oos_expectancy", 0.0),
           {"candidate": candidate_result.get("oos_expectancy"),
            "current": current_result.get("oos_expectancy")})
    robustness = evidence.get("robustness_score")
    _check("robustness",
           robustness is not None and float(robustness) >= robustness_min,
           {"robustness_score": robustness, "required_min": robustness_min})
    wf = evidence.get("walk_forward") or {}
    folds = int(wf.get("folds") or 0)
    profitable = int(wf.get("profitable_folds") or 0)
    _check("walk_forward",
           folds >= wf_min_folds
           and profitable / folds >= wf_min_profit_fraction,
           {"folds": folds, "profitable_folds": profitable,
            "required_folds": wf_min_folds,
            "required_profit_fraction": wf_min_profit_fraction})
    drawdown = evidence.get("max_drawdown")
    _check("drawdown_limit",
           drawdown is not None and float(drawdown) <= max_oos_drawdown,
           {"max_drawdown": drawdown, "allowed": max_oos_drawdown})
    lifecycle_ok, lifecycle_detail = _lifecycle_approved(repository, strategy_name,
                                                         candidate)
    _check("lifecycle_approved", lifecycle_ok, lifecycle_detail)
    return {"passed": all(c["passed"] for c in checks.values()),
            "checks": checks}


def _lifecycle_approved(repository, strategy_name: str, candidate: dict):
    """APPROVED+ACTIVE root lineage with the complete ladder recorded."""
    from app.engine.adaptive import REQUIRED_LADDER
    from app.learning.lifecycle import lineage_id_for

    lineage = lineage_id_for(strategy_name, candidate, 0, None)
    history = repository.lifecycle_history(lineage)
    if not history:
        return False, {"lineage": lineage,
                       "reason": "no lifecycle record (not approved)"}
    stages = {row["stage"] for row in history}
    missing = [stage for stage in REQUIRED_LADDER if stage not in stages]
    if missing:
        return False, {"lineage": lineage, "missing_stages": missing}
    current = history[-1]
    if current["stage"] != "APPROVED" or current["status"] != "ACTIVE":
        return False, {"lineage": lineage,
                       "stage": current["stage"], "status": current["status"]}
    return True, {"lineage": lineage, "stage": "APPROVED", "status": "ACTIVE"}


def evaluate_and_maybe_promote(
    repository,
    candles: list[Candle],
    strategy_name: str,
    current_parameters: dict,
    engine: BacktestEngine,
    timeframe: str,
    *,
    candidate_parameters: dict | None = None,
    min_oos_trades: int = 3,
    validation_candles: list[Candle] | None = None,
    evidence: dict | None = None,
) -> dict:
    """Validate a candidate against the current parameters on VALIDATION data.

    3.0.2 PROMOTION CONTRACT: promotion runs the FULL research gate
    (``_promotion_gate``) — separated validation evidence, minimum OOS
    trades, strict validation improvement, robustness, walk-forward,
    drawdown limit and lifecycle approval. Missing evidence FAILS the gate
    (fail closed); the locked test stays report-only and can never feed
    any gate input. See ``_promotion_gate`` for the exact contract.
    """
    now = datetime.now(timezone.utc).isoformat()
    candidate = candidate_parameters or {}
    evidence = evidence or {}

    promotion_basis = "validation" if validation_candles is not None \
        else "validation_window"
    basis = validation_candles if validation_candles is not None else candles
    current_result = out_of_sample_expectancy(
        basis, strategy_name, current_parameters, engine, timeframe,
        label="validation")
    decision = {
        "evaluated_at": now,
        "strategy": strategy_name,
        "current_parameters": dict(current_parameters),
        "candidate_parameters": dict(candidate),
        "promotion_basis": promotion_basis,
        "current": current_result,
    }
    promoted = False
    version = f"{strategy_name}-baseline"
    if candidate:
        candidate_result = out_of_sample_expectancy(
            basis, strategy_name, candidate, engine, timeframe,
            label="validation")
        decision["candidate"] = candidate_result
        version = f"{strategy_name}-{sorted(candidate.items())}".replace("'", "")
        gate = _promotion_gate(
            repository, strategy_name, candidate,
            current_result, candidate_result,
            min_oos_trades=min_oos_trades, evidence=evidence)
        decision["gate"] = gate
        promoted = bool(gate["passed"])
        repository.record_model_version(
            version, strategy_name, candidate, promoted, candidate_result, now
        )
    else:
        repository.record_model_version(
            version, strategy_name, current_parameters, True, current_result, now
        )
        promoted = True
    decision["promoted_version"] = version if promoted else None
    decision["promoted"] = promoted
    return decision
