"""Strategy evolution: versioned lineage with mutation/recombination.

Children validate INDEPENDENTLY (their own evidence, own lifecycle); a child
never trades because its parent succeeded. All lineage rows are kept forever.
"""

from __future__ import annotations

import random

from app.learning.lifecycle import lineage_id_for
from app.research.discovery import Spec, evaluate_spec


def mutate(spec: Spec, *, seed: int) -> Spec:
    """Deterministic single-parameter mutation: the first integer parameter
    moves ±1 step (seed picks the direction); float parameters get a small
    documented nudge. The same seed always produces the same child."""
    rng = random.Random(seed)
    parameters = dict(spec.parameters)
    int_keys = [k for k, v in parameters.items() if isinstance(v, int)
                and not isinstance(v, bool)]
    float_keys = [k for k, v in parameters.items() if isinstance(v, float)]
    if int_keys:
        key = int_keys[0]
        parameters[key] = max(1, parameters[key] + rng.choice((-1, 1)))
    elif float_keys:
        key = float_keys[0]
        parameters[key] = round(parameters[key] * (1 + rng.choice((-0.1, 0.1)))
                                + rng.choice((-0.001, 0.001)), 6)
    else:
        raise ValueError("spec has no mutable numeric parameter")
    return Spec(spec.strategy, parameters, f"mutate:seed={seed}")


def recombine(spec_a: Spec, spec_b: Spec, *, seed: int = 0) -> Spec:
    """Per-parameter uniform choice between both parents (deterministic seed)."""
    if spec_a.strategy != spec_b.strategy:
        raise ValueError("recombine requires the same strategy family")
    rng = random.Random(seed)
    parameters = {}
    for key in sorted(set(spec_a.parameters) | set(spec_b.parameters)):
        from_a = key in spec_a.parameters
        from_b = key in spec_b.parameters
        if from_a and from_b:
            parameters[key] = (spec_a.parameters[key] if rng.random() < 0.5
                               else spec_b.parameters[key])
        else:
            parameters[key] = (spec_a.parameters[key] if from_a
                               else spec_b.parameters[key])
    return Spec(spec_a.strategy, parameters, f"recombine:seed={seed}")


class StrategyEvolution:
    def __init__(self, repository):
        self.repository = repository

    def register_root(self, spec: Spec) -> str:
        lineage_id = lineage_id_for(spec.strategy, spec.parameters, 0, None)
        self.repository.record_lineage(lineage_id, spec.strategy,
                                       spec.parameters, 0, None,
                                       origin="root")
        self.repository.record_lifecycle(lineage_id, "RESEARCH", "ACTIVE",
                                         {"why": "root registration",
                                          "source": spec.source or "grid"})
        return lineage_id

    def child(self, parent_id: str, spec: Spec, *,
              mutation: str = "mutate") -> str:
        parent = self.repository.get_lineage(parent_id)
        if parent is None:
            raise ValueError(f"unknown parent lineage {parent_id}")
        generation = int(parent["generation"]) + 1
        lineage_id = lineage_id_for(spec.strategy, spec.parameters, generation,
                                    parent_id)
        self.repository.record_lineage(lineage_id, spec.strategy,
                                       spec.parameters, generation, parent_id,
                                       mutation=mutation)
        self.repository.record_lifecycle(lineage_id, "RESEARCH", "ACTIVE",
                                         {"why": "child registration",
                                          "parent": parent_id,
                                          "note": "child validates "
                                                  "independently"})
        return lineage_id

    def validate_child(self, lineage_id: str, candles, timeframe: str) -> dict:
        lineage = self.repository.get_lineage(lineage_id)
        if lineage is None:
            return {"tradable": False, "reason": "unknown lineage id"}
        result = evaluate_spec(candles, timeframe,
                               Spec(lineage["strategy"], lineage["parameters"],
                                    "lineage"))
        self.repository.record_candidate(
            lineage_id, experiment_id=None, strategy=lineage["strategy"],
            parameters=lineage["parameters"], dataset_id=None,
            metrics=result.get("metrics", {}), oos=result.get("oos", {}),
            robustness={"score": result.get("robustness_score", 0.0),
                        "wf_expectancy": result.get("wf_expectancy")},
            promoted=bool(result.get("tradable")),
            reason=str(result.get("reason", "")))
        return result
