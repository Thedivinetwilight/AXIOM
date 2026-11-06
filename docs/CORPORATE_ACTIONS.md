# Corporate actions in AXIOM research and backtesting

Status: implemented, paper/research only. Applies to `app/data/corporate_actions.py`
and the dataset, research-manifest, report and backtest surfaces that consume it.

## 1. Scope

Supported and actually applied:

| Action | Import type | Effect |
| --- | --- | --- |
| Stock split / sub-division | `SPLIT` | Prices ÷ factor, volume × factor on bars strictly before the ex-date; simulated holdings rescaled so value is unchanged |
| Cash dividend | `CASH_DIVIDEND` | **Either** folded into prices (`TOTAL_RETURN_ADJUSTED`) **or** exposed as a cash event (`RAW` / `SPLIT_ADJUSTED`) — never both |

Recognised, recorded, labelled, and **never applied** (`UNSUPPORTED_ACTION_TYPES`):

`BONUS_ISSUE`, `RIGHTS_ISSUE`, `MERGER`, `DEMERGER`, `SPIN_OFF`, `DELISTING`,
`SYMBOL_CHANGE`.

An import containing one of these is accepted (the record is kept for audit) but the
affected series is reported as `PARTIAL_UNSUPPORTED` with an explicit
`UNSUPPORTED_ACTIONS_PRESENT` limitation. Any *unrecognised* action type is rejected
outright — AXIOM never guesses what an unknown action means.

This release does **not** claim: correct handling of bonus/rights/merger/delisting
economics, dividend withholding tax, currency conversion, or that any synthetic test
demonstrates real-market accuracy or profitability.

## 2. Import format — `axiom.corporate-actions` v1

Provider-neutral JSON. Every record carries instrument, action type, ex-date,
effective values, source/version and provenance; the whole document is hashed.

```json
{
  "format": "axiom.corporate-actions",
  "format_version": 1,
  "provider": {"name": "operator-reviewed", "version": "2024-07-01", "notes": "manual check vs exchange filing"},
  "actions": [
    {
      "record_id": "NSE-DEMO-SPLIT-2024-06-14",
      "revision": 1,
      "market": "NSE",
      "symbol": "DEMO",
      "action_type": "SPLIT",
      "ex_date": "2024-06-14",
      "announced_at": "2024-05-02",
      "ratio_from": 1,
      "ratio_to": 2,
      "source": "NSE corporate-action filing",
      "source_version": "2024-06-14",
      "provenance": "operator transcription of the exchange filing"
    },
    {
      "record_id": "NSE-DEMO-DIV-2024-07-05",
      "revision": 1,
      "market": "NSE",
      "symbol": "DEMO",
      "action_type": "CASH_DIVIDEND",
      "ex_date": "2024-07-05",
      "announced_at": "2024-05-20",
      "cash_per_share": 5.0,
      "currency": "INR",
      "source": "NSE corporate-action filing",
      "source_version": "2024-07-05",
      "provenance": "operator transcription of the exchange filing"
    }
  ]
}
```

Field rules:

| Field | Rule |
| --- | --- |
| `format` / `format_version` | must equal `axiom.corporate-actions` / `1`; other versions are rejected |
| `provider.name`, `provider.version` | required; identifies who produced the feed |
| `record_id` | required, unique per record; `revision` orders corrections |
| `market` | `NSE` or `NASDAQ` (research markets only) |
| `symbol` | uppercased; must match the dataset symbol to be applied |
| `action_type` | one of the supported or recognised-unsupported types above |
| `ex_date` | required ISO date |
| `announced_at` | optional ISO date; must not follow `ex_date` for `revision: 1` |
| `ratio_from` / `ratio_to` | `SPLIT` only, finite and positive, not equal (`ratio_to / ratio_from` is the factor) |
| `cash_per_share`, `currency` | `CASH_DIVIDEND` only; currency must match the market (`INR` / `USD`) |
| `source`, `source_version`, `provenance` | required on every record |

### Validation, duplicates and corrections

| Situation | Behaviour |
| --- | --- |
| Structurally invalid record (missing field, bad date, zero ratio, naive timestamp, dividend carrying ratios) | **Import rejected** (`MalformedCorporateAction`) |
| Same `record_id` + `revision` supplied twice | **Import rejected** (`DuplicateCorporateAction`) — identical or not, an operator must state intent |
| Same `record_id`, different `revision` | Treated as a **correction**: the highest revision wins, the older one is retained as `superseded` |
| Two different `record_id`s claiming the same `(market, symbol, ex_date, action_type)` with **different** values | **Import rejected** (`ConflictingCorporateAction`) |
| Two different `record_id`s claiming the same event with **identical** values | **Import rejected** (`AmbiguousCorporateAction`) |
| Unsupported-but-recognised action type | Accepted, labelled, excluded from adjustment, series downgraded to `PARTIAL_UNSUPPORTED` |
| Unknown action type | **Import rejected** — never silently inferred |

A rejected import stores nothing: the action feed is validated before any bar file or
index entry is written, so a dataset store that rejects an import is byte-identical
afterwards.

If a feed holds records for the same ticker in more than one market and no market is
given, the series is reported `UNVERIFIED` with `AMBIGUOUS_MARKET_SCOPE` — AXIOM does
not guess which action set applies.

## 3. Conventions (price / volume / position / cash)

### Splits preserve economic value

For every bar **strictly before** the ex-date, with `factor = ratio_to / ratio_from`:

```
open, high, low, close /= factor        volume *= factor
```

so `price × volume` is invariant (`test_split_preserves_economic_value_and_updates_price_and_volume`).
Bars on/after the ex-date are untouched — the raw file already reflects the split there.

A simulated holding keeps its value via `apply_split_to_position(quantity, average_price, action)`:

```
quantity    *= factor
average_price /= factor     # quantity × average_price is unchanged
```

### Cash dividends are counted exactly once

Two mutually exclusive conventions:

| Policy | Prices | Dividend accounting | Provenance |
| --- | --- | --- | --- |
| `RAW` | unadjusted | cash events listed for a research ledger | `dividends_as_cash: true`, `dividend_price_adjustment: false` |
| `SPLIT_ADJUSTED` | splits only | cash events listed for a research ledger | `dividends_as_cash: true`, `dividend_price_adjustment: false` |
| `TOTAL_RETURN_ADJUSTED` | splits **and** dividends | **no** cash credit | `dividends_as_cash: false`, `dividend_price_adjustment: true` |

Total-return price factor for bars before the ex-date:

```
P_adj = P × (1 − cash_per_share / prev_close)
```

`prev_close` is the last close before the ex-date **in the already split-adjusted
series**. If there is no such bar, or the dividend is not smaller than that close, the
adjustment is refused (`ADJUSTMENT_NOT_DEFENSIBLE`) instead of being invented, and the
series is marked `PARTIAL_UNSUPPORTED`.

`assert_no_double_counting(provenance)` raises if a record tries to set
`dividend_price_adjustment` **and** `dividends_as_cash`. The backtest engine calls it
on every run.

### Raw prices vs research-adjusted prices

* **Dataset storage is always raw.** `DatasetManager.register_dataset` writes the bar
  file exactly as imported and pins `sha256`. Adjusted series are *derived per call*
  by `DatasetManager.load_adjusted_dataset(dataset_id, policy=..., as_of=...)`, so one
  dataset can serve both paths without either being able to impersonate the other.
* **Execution boundaries refuse adjusted series.** `AdjustedSeries.for_execution()`
  raises `AdjustedSeriesError` unless the series is `RAW`; `app/api/services.py:
  load_dataset` (the paper replay/backtest dataset loader) runs every bar list through
  `assert_raw_candles`, and `BacktestEngine.run` requires an explicit provenance
  declaration before it will simulate on adjusted prices — and then warns in the
  result that the fills are simulated on adjusted, not traded, prices.
* Every provenance block carries `raw_bars_for_execution: true` plus both
  `dataset_bars_sha256` and `adjusted_bars_sha256`.

## 4. Look-ahead safety

Rule (recorded verbatim in every provenance block as `look_ahead_rule`):

> An action affects a simulated date only when `ex_date <= as_of` and
> (`announced_at` is `None` or `announced_at <= as_of`).

Consequences:

* A split announced before the simulation date but effective after it is **not**
  applied; it appears in `pending_announced_actions` instead.
* `as_of` selects the revision that was **public at that moment**. A correction
  published later (revision ≥ 2, announcement date allowed after the ex-date) cannot
  rewrite an earlier simulation; the earlier run keeps the values that were known then
  (`test_later_correction_does_not_rewrite_earlier_decisions`).
* `build_series(..., as_of=None)` intentionally means "retrospective, all recorded
  actions" and must not be used for a decision made on a historical date.

## 5. Provenance recorded in dataset and run records

`DatasetManager.register_dataset(..., corporate_actions=<payload>, corporate_action_policy=...)`
stores a `<dataset_id>.corporate_actions.json` sidecar and adds to the dataset metadata:

| Key | Meaning |
| --- | --- |
| `corporate_action_status` | `RAW` / `SPLIT_ADJUSTED` / `DIVIDEND_ADJUSTED` / `PARTIAL_UNSUPPORTED` / `UNVERIFIED` |
| `corporate_actions_verified` | verified action data **and** a claim the data supports, and no limitations |
| `corporate_actions_adjusted` | prices are adjusted under the registered policy |
| `corporate_actions_file` | sidecar filename (re-parsed and hash-checked on load) |
| `corporate_actions.policy` | `RAW` / `SPLIT_ADJUSTED` / `TOTAL_RETURN_ADJUSTED` |
| `corporate_actions.actions_sha256` | hash of the effective action set (current revisions) |
| `corporate_actions.actions_history_sha256` | hash of every record received, including superseded revisions |
| `corporate_actions.actions_source_sha256` | hash of the imported source document |
| `corporate_actions.dataset_bars_sha256` / `adjusted_bars_sha256` | raw and derived series fingerprints |
| `corporate_actions.actions_applied` | per-action `bars_adjusted` count actually applied |
| `corporate_actions.cash_dividend_events` | dividend cash events (empty under `TOTAL_RETURN_ADJUSTED`) |
| `corporate_actions.unsupported_actions`, `superseded_records` | labelled, unapplied records |
| `corporate_actions.limitations` | machine-readable reasons the result is unverified |
| `corporate_actions.claim`, `claim_honoured` | the free-text adjustment claim, and whether verified data supports it |

A later tamper with the sidecar is detected: `load_corporate_actions` recomputes
`actions_sha256` and raises on mismatch.

Existing free-text claims are still accepted (`corporate_action_adjustment`), but a
claim the data does not support is **not honoured**: it is recorded, flagged with
`UNVERIFIED_ADJUSTMENT_CLAIM` and reported as unverified rather than silently believed.

## 6. Where the status is surfaced

| Surface | What appears |
| --- | --- |
| `POST /api/model-lab/datasets/import` | full block in the returned dataset; invalid/duplicate/conflicting/ambiguous records → `422` |
| `GET /api/model-lab/datasets` | `corporate_actions` block per dataset (existing endpoint, no new screen) |
| Terminal → Model Lab → Historical Dataset Manager | new **Corp. Actions** column in the existing table: status pill (`UNVERIFIED` / `SPLIT_ADJUSTED` / `DIVIDEND_ADJUSTED` / `PARTIAL_UNSUPPORTED`) with a hover detail listing policy, action hash prefix, provider, unsupported actions and limitations. No new screen |
| Research run manifest (`axiom.model_lab.research_run.v1`) | `dataset.corporate_action_status`, `dataset.corporate_actions_verified`, `dataset.corporate_actions` |
| Ensemble evidence report | `methodology_audit_disclosures.corporate_actions_status` (`CORPORATE_ACTIONS_VERIFIED` / `PARTIALLY_VERIFIED_CORPORATE_ACTION_ADJUSTMENT` / `UNVERIFIED_CORPORATE_ACTION_ADJUSTMENT`) and `methodology_audit_disclosures.corporate_actions` |
| Backtest result | `BacktestResult.corporate_actions` (the provenance the run was executed under) + warnings |

## 7. Known limitations

1. Only splits and cash dividends are implemented; everything else is labelled
   unsupported (see §1).
2. No provider feed is bundled or auto-fetched. A corporate-action file must be
   supplied and reviewed by an operator; without one every dataset stays `UNVERIFIED`.
3. Cash dividends are supplied as **events**, not booked automatically. The backtest
   engine does not credit them (and says so in `warnings`); a research ledger must
   credit each event exactly once, and never while prices are dividend-adjusted.
4. Split adjustment assumes ratios are constant for the whole pre-ex history and does
   not model reverse splits spanning multiple events beyond applying each factor in
   ex-date order.
5. `prev_close`-based dividend factors ignore tax, withholding, special-dividend
   classification and currency conversion.
6. Timestamps are UTC dates; intraday ex-date cut-offs and exchange session rules are
   not modelled.
7. Passing these deterministic synthetic tests demonstrates bookkeeping and
   provenance discipline only — **not** financial accuracy, alpha, or profitability.
