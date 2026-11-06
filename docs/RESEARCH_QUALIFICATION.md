# Research qualification gate

AXIOM's Model Lab already separates train / validation / untouched holdout,
embargoes the boundaries, models NSE and NASDAQ costs, re-runs the holdout at a
2× cost stress, refuses bundled synthetic fixtures as real data and stores
corporate-action provenance per dataset. What it did not have was **one
enforceable decision** saying whether a given research record is complete enough
to be called qualified — and nothing prevented a green label from being attached
to a run that quietly lacked a benchmark, a cost model, a search manifest or an
independently attested dataset.

`app/model_lab/qualification.py` is that decision. It reads one research manifest
plus the evidence an operator supplies, and returns an immutable record with
`QUALIFIED`, `UNVERIFIED` or `BLOCKED`.

> Qualification is research evidence only. It never arms a strategy, never
> selects a paper candidate, never sizes a position, never issues an
> `ExecutionPermit` and never enables `LIVE-MARKET LIVE`. No statement in this
> document claims profitability.

## What a record pins

| Field | Source | Why it matters |
| --- | --- | --- |
| `run.manifest_sha256` | research pipeline | The whole evaluation is addressed by one digest; `MANIFEST_INTEGRITY` re-derives it |
| `dataset.sha256` + `recomputed_sha256` | dataset manager / fresh hash of the bars | Proves the bars scored are the bars stored |
| `corporate_actions.{status,verified,actions_sha256,actions_history_sha256,unsupported_actions}` | `app.data.corporate_actions` | Splits/dividends are labelled, not silently applied; unsupported types block |
| `provenance` | operator attestation | Names the capture and the verification level (`OPERATOR_ATTESTED_NOT_INDEPENDENTLY_VERIFIED`) |
| `provider_conformance` | `app.data.conformance` | Offline adapter contract only — never live connectivity |
| `search_family` | research pipeline | Declared-before-search variant list, family hash, selection split/metric |
| `search_family.correction` | `multiple_testing_sharpe_haircut` | Family-wide Sharpe haircut applied before selection |
| `splits` | research pipeline | Train/embargo/validation/embargo/holdout with `selection_touched_test=false` |
| `costs` | research pipeline | Spread, slippage, fee schedule, stress multiplier |
| `benchmark` | research pipeline | Same-split comparator (`buy_and_hold`) |
| `software.{commit,tree_dirty,feature_set_sha256,pipeline_schema}` | `app/model_lab/provenance.py` | Names the exact code that produced the numbers |
| `universe.{symbols,survivorship_aware}` | research pipeline | Names the evaluated symbol set and survivorship treatment |

`record_sha256` covers everything except `evaluated_at` and `run_completed_at`,
so re-scoring the same manifest reproduces the same digest — and because the
pipeline's own digest already excludes completion time, two identical runs
produce an identical record.

## The 20 requirements

Overall verdict: **BLOCKED** if any requirement failed, **UNVERIFIED** if nothing
failed but something lacks evidence, **QUALIFIED** only when every requirement
passed. There is no partial green.

| # | Requirement | Group | Passes when | Failure reason codes |
| --- | --- | --- | --- | --- |
| 1 | `DATASET_IDENTITY` | DATA | dataset id, hash, symbol, market and timeframe are recorded | `DATASET_IDENTITY_MISSING` |
| 2 | `DATASET_HASH_VERIFIED` | DATA | a fresh hash of the evaluated bars equals the pinned hash | `DATASET_HASH_MISSING`, `DATASET_HASH_MISMATCH` |
| 3 | `ACTION_DATA_HASH` | PROVENANCE | action-book SHA-256 recorded whenever actions are in scope | `ACTION_HASH_MISSING` |
| 4 | `REAL_DATA_PROVENANCE` | PROVENANCE | real provider data + attested capture naming a provider | `SYNTHETIC_DATA` (blocking), `PROVENANCE_ATTESTATION_MISSING` / `PROVENANCE_NOT_ATTESTED` (unverified) |
| 5 | `CORPORATE_ACTION_STATUS` | PROVENANCE | status ∈ {`RAW`,`SPLIT_ADJUSTED`,`DIVIDEND_ADJUSTED`}, `verified=true`, no unsupported actions | `CORPORATE_ACTIONS_UNSUPPORTED` (blocking), `CORPORATE_ACTIONS_UNVERIFIED` (unverified) |
| 6 | `PROVIDER_CONFORMANCE` | PROVENANCE | the read-only conformance suite reports PASS for this market's provider, with zero order calls and live trading off | `PROVIDER_CONFORMANCE_FAILED`, `CONFORMANCE_NOT_PAPER_SAFE` (blocking), `PROVIDER_CONFORMANCE_MISSING` (unverified) |
| 7 | `CHRONOLOGICAL_SPLITS` | METHOD | train < validation < test, both embargoes ≥ 1 bar, no overlap, `selection_touched_test=false`, `holdout_used_for_selection=false` | `SPLIT_LEAKAGE` |
| 8 | `MINIMUM_OBSERVATIONS` | METHOD | ≥ 250 total bars, ≥ 40 validation bars, ≥ 25 holdout bars, ≥ 5 trades in validation **and** holdout | `INSUFFICIENT_OBSERVATIONS` |
| 9 | `DATA_RECENCY` | DATA | last bar not in the future of the run and ≤ 400 days old | `STALE_DATA` |
| 10 | `SEARCH_FAMILY_DECLARED` | METHOD | family declared before search, declared == evaluated, hashed, selection on validation | `SEARCH_MANIFEST_INCOMPLETE` |
| 11 | `MULTIPLE_TESTING` | METHOD | correction family size == declared family, applied before selection, reported validation Sharpe > haircut | `MULTIPLE_TESTING_INCOMPLETE` |
| 12 | `OOS_CONSISTENCY` | METHOD | validation and holdout Sharpe > 0, holdout net return > 0, signs agree, ≥ 60 % of walk-forward folds positive | `OOS_INCONSISTENT` |
| 13 | `PIPELINE_GATE` | METHOD | the pipeline's own validation / minimum-trades / cost-stress gate accepted the reported candidate | `PIPELINE_GATE_REJECTED` |
| 14 | `COSTS_MODELLED` | COSTS | spread and slippage > 0, stress multiplier ≥ 1.25, stressed holdout return > 0 | `COSTS_MISSING`, `COST_STRESS_MISSING`, `COST_STRESS_FAILED` |
| 15 | `BENCHMARK` | METHOD | a same-split benchmark exists and the candidate beats it on the holdout | `UNDERPERFORMS_BENCHMARK` (blocking), `BENCHMARK_MISSING` / `BENCHMARK_NOT_COMPARABLE` (unverified) |
| 16 | `RISK_LIMITS` | RISK | holdout max drawdown ≤ 25 % | `RISK_LIMIT_EXCEEDED`, `RISK_METRICS_MISSING` (unverified) |
| 17 | `STRATEGY_VERSION` | REPRO | strategy id, version, parameters and the feature-schema hash are pinned and match this build | `STRATEGY_VERSION_MISSING` |
| 18 | `UNIVERSE` | REPRO | the evaluated symbols and survivorship treatment are recorded | `UNIVERSE_MISSING` (unverified) |
| 19 | `SOFTWARE_COMMIT` | REPRO | producing commit recorded, tree clean, equal to the expected commit | `SOFTWARE_TREE_DIRTY`, `COMMIT_MISMATCH`, `SOFTWARE_TREE_STATE_UNKNOWN` (unverified) |
| 20 | `MANIFEST_INTEGRITY` | REPRO | `manifest_sha256` equals a fresh digest of the manifest body | `MANIFEST_TAMPERED` |

Thresholds live in `app.model_lab.qualification.DEFAULTS` and are echoed into
every record under `thresholds`, so a reader never has to guess which limits
applied.

### Multiple testing

The family is fixed **before** any evaluation: `MA_GRID` (3) + `RSI_GRID` (3) +
`RIDGE_GRID` (3 × 3 = 9) = 15 variants, plus the Kronos ablation when it is
genuinely evaluated. The existing
`app.ensemble.calibration.multiple_testing_sharpe_haircut(M, n_validation_bars)`
produces the threshold; the pipeline records it in
`search_family.correction`, adds a rejection reason to any tuned candidate that
does not clear it, and the gate re-checks that the correction covers the declared
family before accepting the reported number.

### What can never qualify

* bundled synthetic fixtures and any dataset with a synthetic source type or feed;
* a series whose corporate-action status is `UNVERIFIED` or `PARTIAL_UNSUPPORTED`;
* a search whose family was declared after the fact, or whose correction covers
  fewer variants than were searched;
* a run whose holdout was used for selection (or does not say it was not);
* a run without a cost model, a stressed rerun, or a same-split benchmark;
* a run whose reported candidate the pipeline itself rejected;
* a manifest whose digest no longer matches;
* a run produced from a dirty tree (unverified, never qualified).

## Surfaces

| Surface | What it shows |
| --- | --- |
| `GET /api/model-lab/overview` | `qualification` block: schema, scope, `execution_authority: NONE`, the full rule document; each run carries a compact verdict |
| `GET /api/model-lab/runs/{run_id}/qualification` | the full immutable record (404 until the run completes) |
| `GET /api/model-lab/qualification/rules` | the published rules, thresholds and limitations |
| `GET /api/model-lab/models` | `qualification_status`, `qualification_record_sha256`, `execution_authority: NONE` per model |
| Terminal → Model Lab → Runs / Comparison | a Qualification column plus a verdict banner with counts, verification level, blocked and unverified reasons, record SHA-256 |
| `python -m app research-qualify` | text or `--json` record for a manifest file or a stored run |

### Operator command

```bash
# Score a stored run (uses the record written at completion)
python -m app research-qualify --run mlrun-abc123

# Score a manifest file, with optional provenance and a fresh bar hash
python -m app research-qualify --manifest manifest.json \
    --dataset-meta meta.json --provenance attestation.json \
    --recomputed-sha256 <sha256> --expect-commit <commit> --json

# Recompute instead of trusting the stored verdict
python -m app research-qualify --run mlrun-abc123 --store data/model_lab --reevaluate
```

Exit codes: **0** `QUALIFIED`, **1** `BLOCKED` / `FAIL`, **3** `UNVERIFIED`, **2**
usage error (missing manifest, unknown run, run not finished).

The command is read-only: it reads JSON files and the local Model Lab SQLite
store, never contacts a provider, never submits an order, never writes anything
except an explicit `--output` path and never prints a credential.

## Taking a record with you

A qualification record travels inside a **portable research-run bundle**
(`axiom.research.bundle.v1`): `python -m app research-bundle export --run <id>`
writes the manifest, the bars, the corporate-action sidecar, the config, the
fingerprints and this record, all content-addressed.
`python -m app research-bundle verify --bundle <path>` checks it offline — and
re-derives this record to prove it was not edited — and
`python -m app research-bundle replay --bundle <path>` re-runs the pipeline from
the pinned inputs. Neither can upgrade a verdict: the replay report's
`effective_status` is the worse of the bundled and the re-derived status, and
`upgraded` is always `false`. See `docs/RESEARCH_RUN_BUNDLE.md`.

## Limitations (also embedded in every record)

1. A passed gate is a *documentation* claim, not a market claim: it never
   guarantees profitability, and a historical simulation is not a forecast.
2. Provenance is **operator-attested**, not independently verified; the record
   says so in `verification_level` and lists `INDEPENDENT_PROVENANCE_REVIEW` in
   `open_reviews`.
3. Offline provider conformance proves the adapter contract, not live
   connectivity, entitlement or production data quality — those stay unverified.
4. Thresholds are conventional, not derived from a profitability study.
5. Synthetic fixtures can exercise these mechanics (as the test-suite does) but
   can never produce a `QUALIFIED` verdict, because requirement 4 rejects them
   before any metric is considered.

## Tests

`tests/test_research_qualification.py` (44 deterministic tests) covers:
chronological splits and embargoes; mutated-holdout invariance of feature
scaling, selection and ranking; pinned dataset/action/strategy/software identity;
synthetic, unverified, stale, future-dated and corporate-action-incomplete data
staying ineligible; unsupported action types blocking; the declared family and
its correction; candidate-below-haircut blocking; missing costs, benchmark and
minimum evidence producing explicit reason codes; tampered manifests; the
qualified mechanics path and its `execution_authority: NONE`; refusal to promote
or arm; reproduction of the same record; the API and CLI surfaces.
