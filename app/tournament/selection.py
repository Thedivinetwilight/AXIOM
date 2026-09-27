"""Training discovery -> validation selection -> ONE evaluation of locked test."""

from app.strategies import create_strategy
from app.tournament.runner import configurations, DEFAULT_GRIDS
from app.tournament.scoring import score
from app.validation.train_test import split_data
from app.validation.robustness import suspicious_warnings


def discover_and_select(
    train, validation, timeframe, engine, grids=None, scoring=None, shortlist=5
):
    if shortlist < 1:
        raise ValueError("shortlist must be positive")
    discovered = []
    for candidate in configurations(grids if grids is not None else DEFAULT_GRIDS):
        result = engine.run(
            train,
            create_strategy(candidate["strategy"], candidate["parameters"]),
            timeframe,
            label="TRAINING",
        )
        discovered.append(
            {
                **candidate,
                "training_metrics": result.metrics,
                "training_score": score(result.metrics, scoring)["score"],
            }
        )
    discovered.sort(key=lambda row: row["training_score"], reverse=True)
    finalists = []
    for candidate in discovered[:shortlist]:
        result = engine.run(
            validation,
            create_strategy(candidate["strategy"], candidate["parameters"]),
            timeframe,
            warmup=train,
            label="VALIDATION",
        )
        finalists.append(
            {
                **candidate,
                "validation_metrics": result.metrics,
                "validation_score": score(result.metrics, scoring)["score"],
            }
        )
    finalists.sort(key=lambda row: row["validation_score"], reverse=True)
    return finalists[0], {"training_candidates": discovered, "validation_finalists": finalists}


class LockedTest:
    """One-use guard per research session, not a claim users cannot rerun files."""

    def __init__(self, candles):
        self._candles = tuple(candles)
        self._used = False

    def evaluate(self, candidate, engine, timeframe, warmup):
        if self._used:
            raise RuntimeError("Unseen test already evaluated; it is no longer unseen")
        self._used = True
        return engine.run(
            self._candles,
            create_strategy(candidate["strategy"], candidate["parameters"]),
            timeframe,
            warmup=warmup,
            label="UNSEEN TEST",
        )


def validate_candidate(
    candles, timeframe, engine, *, grids=None, splits=None, scoring=None, shortlist=5
):
    train, validation, test = split_data(candles, splits, timeframe)
    locked = LockedTest(test)
    candidate, research = discover_and_select(
        train, validation, timeframe, engine, grids, scoring, shortlist
    )
    test_result = locked.evaluate(candidate, engine, timeframe, train + validation)
    warnings = suspicious_warnings(test_result.metrics, candidate["training_metrics"])
    return {
        "label": "VALIDATION / UNSEEN TEST",
        "selected_candidate": candidate,
        "research": research,
        "unseen_test": test_result.to_dict(),
        "warnings": warnings,
        "split_sizes": {"train": len(train), "validation": len(validation), "test": len(test)},
    }
