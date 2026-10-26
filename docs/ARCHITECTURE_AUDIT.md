# AXIOM architecture and integration audit — 3.7.0a5

Date: 2026-10-26. Baseline: published a4 source `e281b392cc654a64bb9c98d4b1b9e56536aebec2`; the exact a5 build commit is recorded in `SOURCE_COMMIT.txt` and `BUILD_METADATA.json`.

This is a source/integration audit, not an account-specific, profitability, or live-readiness certification. No broker credential, external DEMO/LIVE order, or real account was used. Windows runtime evidence belongs to the exact release metadata, not to this design document by itself.

## 1. Architecture map

```text
Local browser (no CDN)
  ├─ /terminal, /chart, /dashboard
  │    ├─ local chart/replay UI
  │    ├─ authenticated legacy research/backtest/paper APIs
  │    └─ Vantage controls ── VantageService ── guarded MT5 gateway ── local MT5 terminal
  │                                  └─ VantageStore / DPAPI secret boundary
  ├─ independent MT5 market-data controls
  │    └─ MT5MarketDataService ── read-only gateway (no order functions)
  │             └─ may supply fresh DEMO prices to manual-only local paper
  ├─ listed-assets UI/API
  │    └─ ListedAssetService ── separate listed identity/provider boundary
  │             └─ no MT5 CFD substitution or listed execution route
  └─ /autonomy (authenticated API; local static JS)
       └─ AutonomyService (one session lock; in-memory permits only)
            ├─ InstrumentCatalog + Upstox completed-bar read-only feed
            ├─ strategy/evidence + portfolio/daily-risk gates
            ├─ PaperExecution (PAPER) OR UpstoxLiveBroker (default-disabled LIVE)
            ├─ AutonomyStore: sessions/intents/snapshots/hash-chained audit
            ├─ AutonomyRunner: activated in-process sessions only
            └─ optional KronosService
                 ├─ manifest/source/artifact verification
                 ├─ per-user versioned artifacts + exact CPU runtime
                 └─ isolated offline Python worker ── pinned Kronos model/tokenizer

Legacy local paper/research use the main SQLite Database/Repository.
Vantage shares that database through VantageStore but has a distinct service/order boundary.
Autonomy deliberately has its own SQLite journal and recovery state.
Research runners are offline/read-only and do not obtain an execution permit.
```

### Trust and execution boundaries

* FastAPI bearer authentication protects data and mutation APIs. Public HTML contains no credential or remote script.
* `LOCAL PAPER`, `VANTAGE DEMO`, `VANTAGE LIVE`, Upstox `PAPER`, and `UPSTOX_LIVE` are separate modes, not aliases.
* Vantage market data and Vantage execution have separate gateways. Upstox market data and Vantage orders are not integrated.
* Autonomy startup cannot create, Arm, or Activate a session. Recovery halts interrupted sessions; permits are never serialized.
* Kronos setup has no session or order API. Missing artifacts/runtime/inference produce `UNAVAILABLE`, not a mock forecast.
* The emergency-stop preemption flag and submission-time lifecycle rechecks remain in the execution service; background setup does not hold the session lock.

## 2. Feature and integration matrix

| Area | Active path and persistence | Integration assessment | Verification boundary |
|---|---|---|---|
| Operator UI | Static terminal/chart/dashboard/autonomy assets; local Lightweight Charts; authenticated fetch | Distinct workflows share a server but not execution semantics. a4 adds Kronos plan/progress/status without changing Arm/Activate controls. | JS syntax + all three local DOM/fetch harnesses; installed-package asset checks. |
| API/auth | FastAPI routers in `app/api`; bearer dependencies; sanitized validation errors | Kronos endpoints are fixed actions with no URL/revision/package/credential body. No generic Upstox order endpoint exists. | API authentication/schema tests and frozen token-gate checks. |
| Public/providers | Upstox BOD catalog/completed bars; independent MT5 read-only feed; separate listed provider interface | Provider identity is not interchangeable. Forming/stale/invalid bars fail closed. No provider is used to obtain research data in this audit. | Fake transport/contract tests; real account/feed behavior remains owner-verified. |
| Brokers | Guarded Vantage MT5 service; Upstox live broker behind autonomy; no default live connection | Separate account contracts and reconciliation models. No claim that all markets/products are supported. | Fake MT5/Upstox tests only; no external DEMO/LIVE calls. |
| Paper engines | Legacy `PaperBroker`/`PaperSimulator`; autonomy `PaperExecution`; manual-only MT5-price paper; replay | Overlap is intentional: fill lifecycle, journal, instrument, and review semantics differ. Do not merge without a dedicated migration proof. | Deterministic unit/replay/recovery tests; none is external execution evidence. |
| Strategies | Legacy strategy registry/tournament; autonomy families plus frozen `EvidenceRegistry` | Autonomy does not treat legacy tournament rank as evidence. Kronos is an optional filter on an identical baseline setup. | Chronological research and provenance/gate tests. |
| Risk | Vantage contract/risk checks; autonomy stop sizing, aggregate margin/correlation, daily loss and non-loosening stops | Risk is enforced inside each execution boundary, before every submission/modification. Kronos cannot change risk values. | Fault, gap, sizing, lifecycle and emergency-preemption tests. |
| Persistence/recovery | Main SQLite/Repository; VantageStore; separate AutonomyStore WAL/FULL journal | Separate schemas preserve blast-radius boundaries. Interrupted autonomy sessions recover HALTED and require fresh approval. | Restart/corruption/snapshot/hash-chain tests; package excludes all runtime DBs. |
| Scheduling | `AutonomyRunner` for activated sessions; maintenance scheduler for background legacy jobs; offline research CLI | Research/maintenance does not schedule order writes. Autonomy scan API remains read-only; only the background runner executes activated sessions. | Runner/API tests; no complete market-session schedule was observed. |
| Logging/audit | Bounded performance metrics, structured logs, durable redacted autonomy hash chain | Kronos emits global setup events and sanitized status; signed redirect URLs, credentials, paths and raw worker stderr are not returned. | Redaction/audit tests; release logs contain no broker credentials. |
| Research | Chronological folds + embargo + one locked holdout; deterministic costs/rejections; paired Kronos ablation | Forecast metrics are separate from trading metrics. Failed/unavailable/right-censored forecasts are counted; no resolved sample means null incremental deltas. | Local synthetic fixtures test mechanics only. No suitable licensed chronological intraday ON/OFF dataset/result is supplied. |
| Packaging | PyInstaller onedir + loaderless Inno; exact-commit workflow; payload guards | Worker/support resources and locks are shipped, but Torch/Pandas runtime, weights, credentials, journals and state are not. Installed Windows gate performs per-user setup and real inference before release. | Linux source/payload tests plus exact-commit Windows build/install/upgrade/uninstall/portable/Kronos gates. |

## 3. Confirmed defects and disposition

Severity: **P0** = external execution/data-loss safety; **P1** = core feature/release-blocking correctness; **P2** = bounded reliability/maintainability; **P3** = cleanup/observability.

| ID | Severity | Evidence and impact | Disposition |
|---|---|---|---|
| A4-K01 | P1, fixed | The new worker attempted `json.load(stdin)` during a runtime-only probe with no payload, so a valid runtime would report `ERROR`. | Added explicit `--probe-only` IPC and an isolated subprocess regression in `tests/test_kronos_setup.py`. |
| A4-K02 | P1, fixed | A combined PyPI/PyTorch index command allowed index selection to blur package provenance even with exact versions. | Split installation: ordinary exact packages only from PyPI; only the exact `+cpu` Torch distribution from the official CPU index. Test asserts both command closures and `--no-deps`/binary/isolated flags. |
| A4-K03 | P1, fixed | Reinstalling an already-valid content-addressed generation could delete and replace the active directory unnecessarily. | Preserve a valid generation on retry; stage/verify before replacing only invalid state. Regression checks generation identity and absence of replacement debris. |
| A4-K04 | P1, fixed | Windows gates showed that `fsync` on a read-only current-pointer handle fails with `EBADF`; Git checkout also changed the raw manifest and LF-only vendored `model/kronos.py` hashes through CRLF conversion. | Open the pointer `r+b` for flushing, pin the authoritative manifest/runtime lock to LF, and disable text conversion for all pinned vendored source bytes. Make marker tests explicitly select Python 3.11/3.12 instead of assuming the host version. |
| A4-K05 | P1, fixed | The pinned Hub loader prints local weight-loading notices to stdout, corrupting the worker's single-record JSON IPC even when model loading succeeds. | Redirect third-party stdout to stderr and reserve original stdout for exactly one JSON result; regression reproduces the notice without claiming inference. |
| A4-K06 | P1, fixed | The worker supplied `DatetimeIndex`, while pinned upstream `calc_time_stamps` requires the Series `.dt` accessor; genuine inference failed with `AttributeError`. | Convert both timestamp arrays with `pd.Series(pd.to_datetime(...))`; locked pandas 2.2.2 reproduces the failure and validates the adapter contract. |
| K07 | P1, fixed | Frozen Windows discovery omitted explicit `py -3.11-64` / `py -3.12-64` selectors and the launcher catalog, and offered no executable fallback. A working owner Python could therefore be reported as missing. | Probe explicit x64 selectors and `py -0p`; add authenticated direct-executable selection with CPython/version/64-bit/pip validation, bounded path-free diagnostics, per-user persistence, safe labels, selected-interpreter lock markers, and interpreter-bound self-test evidence. |
| R01 | P1, fixed | `_forecast_metrics` replaced integer failed-forecast count with residual values; unavailable and unresolved attempts were invisible. This could misstate an ON/OFF result. | Separate integer errors/unavailable/unresolved/attempt counts and right-censored sample limits. Null incremental deltas when no resolved real forecast exists. `tests/test_autonomy_research.py` covers both mechanics and no-benefit behavior. |
| D01 | P2, deferred | Several paper engines and two principal persistence stacks have overlapping concepts but different contracts. Broad consolidation risks changing fill/recovery behavior. | Retain documented boundaries; no broad rewrite in a4. |
| D02 | P2, deferred | Legacy tournament/research and autonomy evidence are separate implementations. | Retain provenance separation; share only narrowly reviewed metrics/contracts in future work. |
| O01 | P3, deferred | The global Kronos panel filters the most recent 300 mixed audit events, so very old setup events can age out of this small view. | Durable events remain in SQLite; add a filtered global-event query later rather than changing journal schema now. |
| L01 | Residual limitation | Runtime versions, transitive closure, platform markers and index hosts are locked, but individual per-OS wheel SHA-256 values are not. Model/tokenizer files are individually size/SHA-256 locked. | Windows exact-runtime probe is mandatory for a4. A future multi-platform wheel-hash lock can strengthen supply-chain evidence without changing inference behavior. |

No new pre-existing P0 execution defect was confirmed in this focused audit. This is not a proof that none exists. Existing emergency-stop, ambiguous-submission, non-loosening-protection, and recovery regressions must remain green in every release gate.

## 4. Kronos and research evidence limits

* `config/kronos-small.json` is the only model/tokenizer/source authority. The raw file SHA-256 and canonical manifest identity are both reported.
* A genuine self-test means verified local artifacts, exact imported CPU runtime, pinned model/tokenizer load, and a real forecast on deterministic synthetic smoke bars. It is explicitly not external market evidence.
* A paired profitability evaluation requires suitable local/properly licensed chronological data and identical instruments, timestamps, strategy parameters, costs, slippage, risk, and deterministic rejection/seed schedules in both arms. Walk-forward tuning and the final holdout must remain chronologically separate; holdout locks cannot be deleted for tuning.
* The local sample CSVs are synthetic demonstrations. They cannot establish Kronos benefit. In the absence of a suitable dataset and genuine forecasts, paired profitability impact is **UNMEASURED**.

## 5. Safe supervised-PAPER startup for the next session

This path does not require Kronos and does not make the application live-ready.

1. Use only the exact reviewed package and verify every downloaded file against that release's `SHA256SUMS.txt`. Keep the previous a3 package until a4's exact Windows/install evidence is reviewed.
2. Start the installed app normally with its generated local token, `PAPER`/local-paper defaults, LIVE disabled, and no Vantage connection. Confirm `/health`, the execution boundary, and disarmed state. Do not import broker credentials for a smoke start.
3. On `/autonomy`, create a **fresh PAPER** session only for exact supported instruments. If live-market bars are required, the owner may use their own fresh read-only Upstox daily authorization; never paste it into chat or release files. PAPER has no broker order/cancel/modify route.
4. Enter new conservative risk bounds. Review evidence/provenance and the exchange calendar. Use read-only scan/reconcile first. Kronos `UNAVAILABLE` is an acceptable fail-closed state; do not substitute a mock forecast.
5. Only while supervising, explicitly Arm and then Activate PAPER. Observe completed bars, intents, simulated three-leg protection, rejects/gaps, daily-loss blocking, and audit-chain status. Do not infer fills or protection from submission IDs.
6. Pause/Emergency before leaving unattended. Reconcile and resolve every simulated unresolved intent; restart must remain disarmed. Complete the owner-attested full-session shakedown before making operational claims.

Real account permissions, external DEMO behavior, complete-session operation, Windows owner-device policy, and profitability remain separately unverified unless the owner supplies evidence. **Do not enable LIVE as a continuation of this checklist.**
