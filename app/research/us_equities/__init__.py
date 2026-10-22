"""US equities paper-trading research harness (daily bars, long-only).

Public entry points:

``load_dataset(path)``
    Validate a point-in-time dataset directory against the documented contract.
``CANDIDATES`` / ``get_candidate(key)``
    The explainable, fixed-parameter rules that are evaluated.
``evaluate(dataset, candidates)``
    Chronological train/validation/test evaluation with walk-forward folds and a
    cost stress case, executed through the real paper broker.

Nothing in this package places an order, contacts a broker, or downloads data.
"""

from app.research.us_equities.dataset import (
    DatasetError,
    USEquityDataset,
    describe_requirements,
    load_dataset,
)
from app.research.us_equities.harness import (
    CostModel,
    EvaluationReport,
    RunResult,
    chronological_split,
    evaluate,
    run_candidate,
)
from app.research.us_equities.strategies import (
    BUY_HOLD,
    CANDIDATES,
    Candidate,
    StrategySpec,
    get_candidate,
)

__all__ = [
    "BUY_HOLD",
    "CANDIDATES",
    "Candidate",
    "CostModel",
    "DatasetError",
    "EvaluationReport",
    "RunResult",
    "StrategySpec",
    "USEquityDataset",
    "chronological_split",
    "describe_requirements",
    "evaluate",
    "get_candidate",
    "load_dataset",
    "run_candidate",
]
