# AXIOM-MONEY-PRINTER — MASTER IMPLEMENTATION PROMPT v1.0

## COPY/PASTE THIS ENTIRE FILE INTO ARENA

You are taking ownership of the repository as a senior staff/principal engineer, quantitative-systems architect, product designer, reliability engineer, security engineer, and adversarial QA lead.

The target product is:

# AXIOM-MONEY-PRINTER

Internal short name: **AXIOM**.

“MONEY-PRINTER” is a project codename/brand only. It is **not** a promise of profit, a guarantee of performance, or a claim that the system can predict markets with certainty.

This is one continuous master implementation directive. Do not treat it as a suggestion list, a roadmap presentation, or a documentation exercise.

---

# 0. EXECUTION CONTRACT — DO NOT STOP

**DO NOT STOP EARLY.**

Do not stop after one page, one feature, one subsystem, one refactor, one migration, one green test suite, one successful build, or one apparently complete demo.

Execute the full cycle:

**INSPECT → UNDERSTAND → ARCHITECT → IMPLEMENT → INTEGRATE → TEST → ADVERSARIAL TEST → BENCHMARK → FIX → RETEST → PACKAGE → VERIFY → AUDIT → IMPROVE → RETEST.**

Continue until the entire file has been processed and every practical requirement has either been implemented or honestly recorded as blocked by an external/environmental constraint.

If a dependency is unavailable, a credential is absent, a GPU is unavailable, a display server is unavailable, a market is closed, an API cannot be contacted, or the platform cannot perform a specific verification step:

- do not fake success;
- build the surrounding capability anyway;
- use deterministic stubs/mocks/replays where appropriate;
- create the complete GUI and service contracts;
- add tests around the blocked boundary;
- document exactly what remains unverified;
- continue with all other work;
- return to the blocked area at the end and verify whatever is actually possible.

Do not repeatedly stop and wait for the user when you can make deterministic progress yourself.

When you encounter existing code that is good, preserve it.

When you encounter existing code that is weak, improve it.

When requirements conflict, preserve the **business/product intent** while choosing the stronger engineering design.

Never weaken safety, correctness, data integrity, recovery, security, or observability just to make a feature appear to work.

Do not create placeholder buttons that claim to work.

Do not create fake metrics.

Do not label synthetic/replay/model-derived data as observed market data.

Do not claim a feature is complete because an endpoint returns HTTP 200.

Do not claim GUI completion because a page renders.

A feature is complete only when its user flow, backend behavior, persistence, failure states, tests, and recovery behavior are coherent.

---

# 1. CURRENT REPOSITORY CONTEXT

Repository:

`Thedivinetwilight/AXIOM`

It is a private GitHub repository.

Current known branch landscape at the time this master prompt was authored:

- `arena/01a102dd-axiom` → `6b2dca5470f4f02d427575425fa8200e3705ff14`
- `arena/01a0fcfb-axiom` → `3d2cde7399e26f62158c6c14e44fbdf2f34533a4`
- `arena/01a0ecfa-axiom` → `7dc3e1a5bdc3c97525c599875e41e901b6d4b5d7`
- `main` → `0314db2c2d1473b0b63e04ce7c1455695f93a885`

The latest known Arena-generated branch at authoring time is `arena/01a102dd-axiom`, commit `6b2dca5`.

The recorded 3.1.0 build report on that branch states:

- product version: **3.1.0**;
- base commit: `3d2cde7`;
- source suite: **1426 passed / 37 skipped / 0 failed**;
- frozen smoke suite: **38/38 passed**;
- CI platform jobs succeeded;
- Windows `Arena.exe` was built and passed its recorded frozen gate;
- live trading is implemented in code but is fail-closed and disabled by default in the current baseline.

The older repository README and some historical docs still identify the release as **Arena 3.0.5**. This is a known documentation/version-history inconsistency. Do not blindly trust stale prose. Inspect package metadata, code, tests, current branch history, build reports, and runtime behavior. Reconcile the identity deliberately.

Current repository areas include:

- `app/` — core backend/trading/research infrastructure;
- `desktop/` — PySide6 desktop UI;
- `axiom/` — Kronos/forecasting subsystem;
- `tests/` — broad unit/integration/UI/frozen verification;
- `tools/` — diagnostics, deterministic checks, packaging/building;
- `.github/workflows/` — CI and executable gates;
- numerous operational, research, Kronos and live-trading documents.

Important existing areas/modules include, but are not limited to:

- `app/api/`
- `app/backtesting/`
- `app/brokers/`
- `app/config/`
- `app/console/`
- `app/data/`
- `app/database/`
- `app/engine/`
- `app/history/`
- `app/indicators/`
- `app/learning/`
- strategy/research/risk modules
- Upstox authentication and stream/protobuf handling
- local paper execution
- Upstox sandbox execution
- Upstox live execution
- database-backed recovery/state
- PySide6 desktop client
- isolated AXIOM/Kronos worker architecture.

Before modifying anything substantial, inspect the current tree, current branch, build state, existing UI, tests, execution boundaries, persistence, and packaging. The repository itself is an implementation baseline, not an infallible specification.

---

# 2. WHAT AXIOM-MONEY-PRINTER IS

AXIOM-MONEY-PRINTER is a **market intelligence, quantitative research, opportunity discovery, forecasting, simulation, portfolio/risk, paper/sandbox trading, and carefully controlled live execution workstation**.

It should combine the useful properties of:

- a professional market terminal;
- a quantitative research lab;
- an autonomous research assistant;
- a historical analogue engine;
- a model/forecast workstation;
- an opportunity scanner;
- a strategy discovery and validation engine;
- a portfolio/risk control center;
- a paper-trading simulator;
- a broker-integrated sandbox platform;
- a live execution workstation;
- an operational/recovery console;
- a searchable research memory.

The central questions are:

1. **What is happening?**
2. **What changed?**
3. **What deserves attention?**
4. **What evidence supports that interpretation?**
5. **What historical situations resemble this one?**
6. **What does the model forecast?**
7. **How uncertain is it?**
8. **What would the candidate action imply for risk and portfolio state?**
9. **Is execution currently permitted?**

Always separate:

**OBSERVATION / ANALYSIS / RECOMMENDATION / EXECUTION.**

A good analysis is not automatically a recommendation.

A recommendation is not automatically an order.

An order is not authorized until it passes independent risk/execution checks.

---

# 3. PRODUCT PHILOSOPHY

The system should feel like a professional quantitative workstation, not a generic dashboard.

Prioritize:

- truthfulness;
- evidence;
- recency/freshness;
- explainability;
- observability;
- deterministic behavior where possible;
- latency where it matters;
- recovery;
- strong boundaries;
- clear states;
- high information density without visual chaos;
- fast interaction;
- deliberate automation.

Do not optimize for fake complexity.

Do not create dozens of AI badges with no meaning.

Do not reduce every stock to one opaque “AI score”.

Instead, show an **evidence stack**.

Preferred recommendation language/statuses include:

- **HIGH-CONVICTION SETUP**
- **FAVORABLE**
- **DEVELOPING**
- **WATCH**
- **AVOID**
- **NO EDGE**

The exact label can depend on the system’s evidence contract, but it must communicate evidence/state rather than false certainty.

---

# 4. USER-SELECTED UNIVERSE IS A FIRST-CLASS CONCEPT

Do **not** design AXIOM around “scan every stock in every exchange because the computer can”.

The user explicitly selects the universe of instruments they consider relevant.

AXIOM then continuously analyzes **only that selected universe**, while still using broader market context when useful.

Universe workflow:

- choose market;
- search/filter instruments;
- select instruments;
- save named universe;
- load/switch universe instantly;
- optionally import/export a custom universe;
- monitor data/analysis state per instrument;
- focus/pin especially important instruments.

Saved universes must be **market-scoped**.

Example concept:

- India / Core Equities
- India / High Momentum
- US / Tech
- Japan / Watchlist
- Global / Macro-sensitive

Do not silently mix incompatible instrument identifiers from different markets.

The user controls inclusion.

AXIOM controls analysis scheduling, promotion, prioritization, historical research, model evaluation, and recommendations within that universe.

---

# 5. MULTI-MARKET ARCHITECTURE

The system should support a market abstraction that can grow beyond India.

Initial market choices must include a clean architecture for:

- India
- United States
- Japan
- United Kingdom
- Hong Kong
- Singapore
- Global / cross-market context

Additional markets should be addable without redesigning the core.

Each market must have an abstraction for:

- exchange/instrument metadata;
- timezone;
- trading sessions;
- holidays;
- currency;
- tick/lot rules;
- symbol/instrument identity;
- historical data source;
- live data source;
- event/news relevance;
- broker mapping;
- execution constraints.

Market switching in the UI must switch the relevant universe context, data context, calendars, charts, analysis, and execution settings without mixing states.

Persist selected market and universe.

---

# 6. DATA ARCHITECTURE

Existing market-data functionality must be preserved and extended rather than replaced blindly.

Maintain the distinction between:

- observed market data;
- historical recorded data;
- replay/synthetic data;
- model output;
- inferred regime labels;
- external events/news;
- research conclusions.

Every important derived object should retain provenance.

At minimum, market bars require:

- timestamp;
- open;
- high;
- low;
- close;
- volume;
- instrument identity;
- market/exchange context;
- timeframe;
- source;
- quality status.

Continue strict validation for:

- NaN/Inf;
- duplicated timestamps;
- ordering;
- OHLC relationships;
- positive prices;
- non-negative volume;
- declared timeframe spacing;
- timezone awareness;
- session/calendar consistency;
- missing data.

Never silently repair bad data.

Report it, classify it, and determine whether the dependent computation should proceed.

Do not fabricate candles to make charts look complete.

Calendar gaps such as weekends/holidays must not be treated as market-data defects when the calendar explains them.

---

# 7. REAL-TIME DATA STRATEGY

Design a scalable subscription architecture around the **selected universe**.

Use an event bus / gateway instead of repeatedly polling every symbol from every page.

The intended model is:

**baseline feed → interest/promotion detection → richer detail → deep analysis**

For example, a selected instrument may begin with a lightweight quote/last-traded-price/change feed.

Only interesting instruments should be promoted to expensive/high-detail processing.

Promotion can be triggered by:

- significant price move;
- volume spike;
- volatility expansion;
- regime change;
- event/news impact;
- unusual relative strength/weakness;
- model disagreement;
- forecast change;
- user focus/pin;
- portfolio exposure;
- execution opportunity.

Upstox V3 WebSocket/protobuf support already exists in the baseline. Preserve it, verify the current protocol implementation, and build the new architecture above it rather than duplicating incompatible feeds.

Maintain a dynamic subscription manager.

The architecture must be able to distinguish:

- subscribed;
- connected;
- stale;
- partially connected;
- backfilling;
- unavailable.

Never pretend a stale quote is current.

---

# 8. WHAT CHANGED? IS A FIRST-CLASS PRODUCT FEATURE

“What changed?” must be prominent throughout AXIOM.

For every important instrument, portfolio, market, research item, forecast, regime, or live-session state, the user should be able to see:

- previous state;
- current state;
- material change;
- timestamp/change age;
- evidence that caused or supports the change.

Examples:

- regime changed from trend to high-volatility;
- volume acceleration detected;
- Kronos forecast revised;
- historical analogue similarity increased;
- model agreement improved;
- risk exposure crossed a threshold;
- market event altered expected transmission path;
- data quality degraded;
- broker state diverged from local state.

Do not make “What changed?” a decorative label. It must be driven by state deltas.

---

# 9. RESEARCH INTELLIGENCE ENGINE

AXIOM must behave like an autonomous research system.

It should continuously:

- monitor selected instruments;
- monitor regimes;
- identify anomalies/opportunities;
- compare current conditions to historical situations;
- test hypotheses;
- run controlled experiments;
- evaluate candidate strategies/models;
- track failures and missed opportunities;
- update research memory;
- prioritize the next useful experiment.

Research must be separate from execution.

Research may propose changes.

Research may not silently bypass hard risk controls, broker validation, authentication, live authorization, or emergency stops.

Learning and discovery may change:

- strategy selection;
- strategy parameters within approved boundaries;
- model versions;
- analytical rankings;
- research priorities.

Learning must not change without explicit governed control:

- authentication state;
- broker identity;
- emergency-stop state;
- live authorization policy;
- hard risk limits;
- required reconciliation;
- credential policy.

---

# 10. HISTORICAL ANALOGUES

Build a first-class historical analogue system.

For a current market state, search historical data for comparable states using suitable features such as:

- returns;
- volatility;
- trend strength;
- volume behavior;
- momentum;
- relative position;
- market regime;
- sector behavior;
- time-of-day/session;
- cross-market context;
- macro/event state where available.

The result must show:

- analogue date/window;
- similarity measure;
- relevant features;
- subsequent outcomes;
- sample size;
- regime match quality;
- selection methodology;
- limitations.

Never cherry-pick analogues merely because they support the recommendation.

The analogue engine must support counterexamples and negative analogues.

Historical analogue evidence is contextual evidence, not a guarantee.

---

# 11. MISSED-OPPORTUNITY RESEARCH

Create a permanent research loop for “what did we miss?”

Record situations where:

- a candidate passed later than it could have;
- the system ignored a useful signal;
- a false negative occurred;
- a recommendation was correct but execution failed;
- execution was blocked for a valid safety reason;
- the model disagreed with reality;
- an analogue/research hypothesis would have helped in hindsight.

For each missed opportunity, store:

- what the system saw;
- what it failed to see;
- the available evidence at that time;
- what would have changed the decision;
- whether that information was realistically knowable then;
- counterfactual result;
- proposed research question;
- resulting experiment.

Build a **false-negative lab** and a **counterfactual simulator** where appropriate.

No hindsight leakage into training labels.

---

# 12. REGIME ENGINE

Maintain/extend the regime engine using observable market features.

Relevant features may include:

- trend;
- volatility;
- momentum;
- volume trend;
- breadth where available;
- time-of-day;
- session state;
- correlation structure;
- relative strength;
- cross-market state.

Regime transitions must be event-driven and persisted.

Regime labels must have evidence and timestamps.

When a regime changes, recalculate only the dependent analytics rather than rebuilding everything unnecessarily.

---

# 13. STRATEGY DISCOVERY / LIFECYCLE

Preserve the existing strategy-discovery/lifecycle concept and extend it.

Candidate strategies should move through explicit lifecycle stages, for example:

**RESEARCH → VALIDATION → WALK_FORWARD → SHADOW → CANARY → APPROVED**

No stage skipping.

Degradation should be explicit, for example:

**ACTIVE → PROBATION → SHADOW → RETIRED**

Retired lineage/history must remain searchable.

Promotion must use out-of-sample evidence.

Do not use in-sample return as a promotion shortcut.

Require adequate sample size.

Require robustness.

Require walk-forward validation.

Track cost drag, liquidity/heat, regime dependence and drawdown.

Only approved strategies can become execution candidates.

---

# 14. OPPORTUNITY CENTER

Build an **Opportunity Center** as a live research feed.

Each opportunity should have a lifecycle such as:

- DETECTED
- QUALIFYING
- INVESTIGATING
- HIGH-CONVICTION SETUP
- DEVELOPING
- WATCH
- EXPIRED
- INVALIDATED
- AVOID
- NO EDGE
- EXECUTED / PAPERED / SHADOWED where applicable.

Each item must show:

- instrument;
- market;
- age/freshness;
- current market state;
- what changed;
- evidence stack;
- model agreement/disagreement;
- historical analogue context;
- regime;
- liquidity/volatility;
- risk implications;
- recommendation state;
- execution eligibility if relevant.

An opportunity feed is not an auto-trading queue.

---

# 15. ATTENTION MODE

Provide user-facing attention/intensity settings:

- **Conservative**
- **Balanced**
- **Aggressive**

These settings alter analytical/recommendation thresholds, ranking sensitivity, exploration breadth, and attention priority.

They must **not** silently weaken hard safety controls.

Aggressive mode may find more candidates.

It may never bypass risk, reconciliation, authentication, emergency stop, broker constraints, or live authorization.

---

# 16. EVIDENCE STACK

Do not collapse everything into one AI score.

The recommendation/evidence drawer should support a structure resembling:

**Market State**
→ **Feature State**
→ **Regime**
→ **Kronos Forecast**
→ **Historical Analogues**
→ **Strategy Evidence**
→ **Risk**
→ **Recommendation**
→ **Execution Eligibility**

Every layer should be inspectable.

Show where evidence is:

- observed;
- calculated;
- model-derived;
- historical;
- heuristic;
- uncertain;
- stale;
- unavailable.

---

# 17. KRONOS INTEGRATION

Preserve the existing isolated AXIOM/Kronos architecture.

Current baseline characteristics include:

- isolated Kronos process;
- PyTorch isolated from the main lightweight GUI process;
- worker/supervisor model;
- forecast cache/store;
- model/tokenizer lifecycle;
- model integrity checks;
- separate market-data contract;
- walk-forward forecasting research;
- explicit distinction between actual and forecast values.

Do not couple Kronos directly into order execution.

Kronos should inform analysis/research/recommendations through explicit, typed, validated data contracts.

Kronos must never write trading decisions, risk state, account state, live authorization, or broker state directly.

---

# 18. KRONOS STUDIO

Make model management GUI-first.

Kronos Studio should provide first-class model cards such as:

- Mini
- Small
- Base

Exact available model IDs should be discovered/configured rather than assumed permanently.

For each model show:

- model ID;
- revision;
- status;
- checksum/integrity state;
- size;
- context length;
- supported hardware;
- current runtime/device;
- benchmark latency;
- memory requirement;
- model accuracy/research metrics where measured;
- last validation;
- active/inactive state.

GUI actions:

**DOWNLOAD → VERIFY → INSTALL → BENCHMARK → ACTIVATE**

No manual cloning.

No manual model downloads.

No terminal.

No manual `.env` editing.

Never load a model until integrity is verified.

Never allow an untrusted manifest/path traversal to point the integrity check at an arbitrary file and then approve it.

Preserve path-confinement checks.

Keep weights out of Git.

Uncertainty must remain honest. Sample dispersion is not automatically calibrated probability.

---

# 19. FORECASTING DATA CONTRACT

Preserve the strict forecasting distinction:

Observed fields are observed.

Forecast fields are forecast.

Never label a forecast “Close” as though it were market data.

Forecast panels must clearly say:

**MODEL FORECAST — not observed market data**

Charts should visibly distinguish actual vs forecast.

Only closed market candles should feed forecasting where that is the current contract.

Cache invalidation must account for:

- instrument;
- timeframe;
- last closed timestamp;
- input-window hash;
- model revision;
- configuration hash.

A revised input window must not silently return an old forecast.

---

# 20. RESEARCH LAB

Create a dedicated Research Lab.

It should show:

- active experiments;
- queued experiments;
- completed experiments;
- failed experiments;
- blocked experiments;
- priorities;
- resource use;
- research memory.

The system should surface a **Next Best Experiment** based on:

- expected information gain;
- unresolved model disagreement;
- recent false negatives;
- drift;
- weakly validated hypotheses;
- regime instability;
- opportunity density;
- cost/compute constraints.

Experiment workflow:

**Hypothesis → Dataset → Features → Models → Validation → Result → Regime Analysis → Stress → Lineage → Reproducibility**

Every experiment needs reproducible identifiers, data version, model version, code/build identity, and parameter configuration.

---

# 21. RESEARCH MEMORY

Create searchable persistent research memory with a hierarchy such as:

**Memory → Hypothesis → Experiment → Result → Decision/Follow-up**

Search should work by:

- instrument;
- market;
- topic;
- regime;
- strategy;
- model;
- date;
- event;
- experiment;
- result;
- failure mode.

Do not let “memory” become an unvalidated dumping ground.

Every important memory object needs provenance and confidence.

---

# 22. STRATEGY LINEAGE

Maintain a visual lineage graph for strategy evolution.

Show:

- parent strategy;
- mutation/recombination;
- version;
- lifecycle;
- validation result;
- regimes;
- OOS expectancy;
- robustness;
- walk-forward status;
- promotion/degradation reasons;
- current use.

Lineage is append-only in meaning even if presentation changes.

Historical failures must not be erased merely because a newer version is better.

---

# 23. LATENCY-FIRST SYSTEM ARCHITECTURE

Treat latency as an engineering property, not a dashboard metric.

Separate computation into:

### HOT PATH

Time-critical operations:

- feed event ingestion;
- state update;
- minimal feature updates;
- candidate detection;
- hard risk checks;
- order validation;
- broker submission;
- critical reconciliation.

### WARM PATH

Near-real-time operations:

- richer features;
- deeper model evaluation;
- historical analogue search;
- opportunity ranking;
- research updates;
- chart aggregation;
- noncritical analytics.

### COLD PATH

Asynchronous work:

- large backtests;
- model training;
- long historical scans;
- report generation;
- data audits;
- large-scale experiment sweeps;
- research memory compaction/indexing.

Do not allow cold-path workloads to block the hot path.

The GUI must never sit in the trading critical path.

The system should be event-driven.

Use incremental features instead of recomputing entire histories when possible.

Use caching aggressively where correctness permits.

Use adaptive batching where it improves throughput without harming deadline-sensitive operations.

Consider zero-copy or lower-copy data paths where profiling proves it matters.

Rust/C++ acceleration is optional and should be justified by measured bottlenecks, not used as decoration.

Python remains acceptable for research/orchestration where it is the practical choice.

---

# 24. LATENCY OBSERVABILITY

Track:

- P50;
- P95;
- P99;
- P99.9

for relevant stages such as:

- market-event ingestion;
- feature update;
- candidate generation;
- model inference;
- risk check;
- order validation;
- broker request;
- broker acknowledgement;
- reconciliation.

Record end-to-end latency and stage breakdown.

Record deadline misses.

Create a **Missed Opportunity Recorder** for candidate opportunities that expired before the system could act or recommend.

Do not optimize based on a single average.

---

# 25. CONCEPT DRIFT / MODEL SPECIALIZATION

Add detection for:

- feature distribution drift;
- regime drift;
- performance drift;
- latency drift;
- data-quality drift;
- broker/market microstructure changes where detectable.

Support model/strategy specialization by regime when evidence justifies it.

Do not make a model switch on a whim.

Require measurable evidence and keep fallback behavior.

---

# 26. COMPUTE/GPU RESOURCE MANAGEMENT

Design a resource manager for:

- CPU;
- RAM;
- GPU/VRAM;
- storage;
- background research jobs.

The scheduler should know the distinction between:

- trading-critical work;
- user-interactive work;
- research workloads;
- model downloads;
- training/inference.

Do not allow a giant research task to starve the live trading path.

Support model residency/warmup where useful.

Unload or downgrade gracefully under resource pressure.

Expose resource pressure honestly in the GUI.

---

# 27. PERSISTENT CORE ARCHITECTURE

AXIOM must be a persistent system, not merely a persistent window.

Separate UI lifetime from core lifetime.

Default close behavior:

- UI closes;
- core/supervisor may continue running in the background;
- tray/system indicator shows AXIOM is still active.

Reopen behavior:

- attach to existing core;
- restore cached snapshot immediately;
- reconnect/reconcile with live state;
- never start a duplicate core.

Explicit shutdown behavior:

1. stop accepting new nonessential work;
2. pause appropriate research jobs;
3. checkpoint state;
4. reconcile broker state;
5. flush durable event/state writes;
6. stop workers cleanly;
7. stop supervisor/core;
8. close UI.

Closing the UI must never accidentally disable live execution or leave the system believing it was safely stopped when it was not.

---

# 28. SINGLE-INSTANCE / SUPERVISOR

Preserve/extend single-engine protection.

Two independent live cores must never trade the same account merely because two copies of AXIOM were launched.

Use a robust heartbeat/lock/lease mechanism.

A stale lease must recover safely.

A healthy active instance must cause a second live instance to refuse execution.

Do not depend solely on the UI for this.

---

# 29. COMPLETE STATE PERSISTENCE

Persist at least:

- selected market;
- selected universe;
- watchlists;
- focus/pinned instruments;
- workspace layouts;
- tabs/panes;
- chart state;
- filters;
- alert preferences;
- research queue;
- experiment checkpoints;
- research memory;
- strategy lineage;
- model versions;
- Kronos configuration/state;
- paper account;
- sandbox state where applicable;
- live-session state;
- positions;
- orders;
- fills;
- P&L;
- risk state;
- alerts;
- system settings;
- background task state.

Every state object must have an explicit owner and update contract.

Do not persist secrets in ordinary application state.

---

# 30. RECOVERY

Implement real recovery, not “restart and hope”.

### Market-data gap recovery

Detect missing intervals.

Backfill.

Validate.

Merge.

Record the data-gap event.

### Paper recovery

Restore:

- equity;
- positions;
- working orders;
- fills;
- realized/unrealized P&L.

### Sandbox/live recovery

Reconcile remote broker state to local intent/state.

Unknown remote orders must be recovered by deterministic intent identity/tag/reference mechanisms.

Never blindly re-post an order when the previous remote outcome is unknown.

### Research recovery

Resume from checkpoints.

### Kronos recovery

Verify model identity/integrity.

Reload worker.

Restore state.

### Crash/reboot

Supervisor should restart core/workers where policy permits.

State must be restored without duplicating orders, research tasks, fills, or alerts.

---

# 31. LIVE TRADING: FIRST-CLASS MODE, NOT HARD-CODED OFF

The current baseline uses a multi-switch fail-closed live gate and defaults live trading off.

The target design is different:

**Live trading must be a real first-class operating mode available in the GUI. It must NOT be permanently hard-coded to `LIVE_TRADING=false`.**

Do not remove safety.

Remove the artificial permanent lockout.

Separate:

- **LIVE CONFIGURED** — system has what it needs to attempt readiness;
- **LIVE READY** — readiness contract passes;
- **LIVE ARMED** — explicit user authorization state exists;
- **LIVE ACTIVE** — system is actually authorized to submit real orders;
- **LIVE PAUSED**;
- **LIVE RECONCILING**;
- **LIVE STOPPED**.

Suggested state machine:

**OFF → CONFIGURED → READY → ARMED → ACTIVE → PAUSED → RECONCILING → STOPPED**

No ambiguous boolean-only live state.

---

# 32. LIVE READINESS CENTER

Create a dedicated GUI readiness center.

Checks should include, as applicable:

- broker configuration;
- OAuth authentication;
- account reachability;
- trading permission;
- current token validity/freshness;
- market-data connection;
- instrument metadata;
- market calendar/session;
- clock synchronization;
- registered static IP status;
- strategy lifecycle approval;
- universe validity;
- current risk configuration;
- portfolio reconciliation;
- open-order reconciliation;
- audit-trail integrity;
- emergency-stop state;
- kill-switch availability;
- execution latency health;
- data freshness;
- shadow/paper divergence;
- single-instance/live-engine lock;
- broker API health;
- system CPU/RAM/disk/GPU capacity where relevant.

Readiness result should be:

- READY;
- WARNING;
- BLOCKED;
- UNKNOWN / NOT CHECKED;
- ERROR.

A missing check must never silently become “healthy”.

---

# 33. LIVE START CONFIRMATION

Before starting a live session, show a clear summary:

- broker;
- account identity (masked where appropriate);
- selected market;
- selected universe;
- active strategies;
- active model versions;
- risk limits;
- max daily loss;
- max exposure;
- current exposure;
- current positions;
- pending orders;
- reconciliation state;
- emergency-stop state;
- data freshness;
- execution health;
- canary settings;
- expected behavior.

Require explicit confirmation that the user understands this session can place real orders.

No accidental one-click activation.

---

# 34. LIVE ORDER PATH

Every live entry should pass a dedicated guard chain resembling:

**SIGNAL**
→ **PORTFOLIO CHECK**
→ **RISK CHECK**
→ **POSITION CHECK**
→ **ORDER VALIDATION**
→ **BROKER RULE CHECK**
→ **DATA FRESHNESS / LATENCY CHECK**
→ **DUPLICATE CHECK**
→ **FINAL AUTHORIZATION**
→ **BROKER**

Any failed check means **no new order**.

A safety guard must never trap an already-held position merely because the entry path is blocked.

Risk-reducing exits must remain available according to the configured emergency policy.

---

# 35. LIVE RECONCILIATION

Continuously reconcile AXIOM state against the broker for:

- positions;
- quantities;
- average prices;
- orders;
- fills;
- cash/funds;
- P&L;
- status.

If a meaningful mismatch exists:

- surface it immediately;
- record it durably;
- pause new entries as appropriate;
- attempt deterministic reconciliation;
- never silently overwrite reality with local assumptions.

Never “fix” a mismatch by inventing a fill.

---

# 36. LIVE OPERATIONAL RAILS

Preserve and improve the existing live-operations concepts, including:

- token freshness guard;
- canary ramp;
- stuck-order watchdog;
- reconcile watchdog;
- breach monitor;
- daily-loss stop;
- drawdown stop;
- durable alerts;
- alert deduplication;
- alert acknowledgement;
- single-engine lock;
- emergency stop;
- operational digest;
- audit-trail replay.

Every operational guard should fail closed for new entries when its state cannot be safely established.

A broken guard must not crash the entire order/reconciliation loop.

Operational guards should degrade safely and produce clear alerts.

---

# 37. EMERGENCY CONTROLS

Provide first-class emergency controls:

- **PAUSE NEW ORDERS**
- **CANCEL OPEN ORDERS**
- **EXIT POSITIONS** where supported by configured policy
- **EMERGENCY STOP**

Emergency stop must be persisted.

Emergency controls must not rely solely on the main UI being healthy.

Design a minimal independent path for emergency-stop signaling where practical, so a UI/rendering failure does not make an emergency stop impossible.

Do not make “Exit Positions” reckless or ambiguous. Make scope explicit.

---

# 38. UPSTOX AUTHENTICATION

Do not ask the user to type an Upstox username/password into AXIOM.

The current Upstox documentation specifies OAuth 2.0 authorization-code authentication: the user logs in on Upstox, the application receives a single-use authorization code, and a backend exchanges it for an access token.

Therefore AXIOM’s GUI should provide:

**CONNECT UPSTOX**

which launches the official Upstox authentication/consent flow.

AXIOM should receive the callback/code and securely exchange it as required.

The client secret must remain confidential.

The token should be stored in a secure OS credential facility where available, with an encrypted-at-rest fallback where necessary.

Never put secrets into:

- logs;
- URLs where avoidable;
- GUI text;
- crash reports;
- ordinary SQLite fields;
- browser localStorage;
- build artifacts.

Mask sensitive values by default.

The system should provide a GUI token lifecycle state such as:

- VALID
- EXPIRING
- EXPIRED
- REAUTH REQUIRED

Upstox access tokens currently expire at **3:30 AM the following day**, and the authorization code is single-use. Treat these as dynamic broker rules that must be verified against the current official documentation rather than assumed forever.

---

# 39. STATIC IP / BROKER COMPLIANCE

Current Upstox documentation includes user-level static IP APIs and static-IP enforcement for API order traffic.

The live readiness center must therefore treat static-IP state as an explicit readiness/compliance check where applicable.

Support GUI visibility for:

- current registered primary IP;
- current registered secondary IP;
- local/public source IP detection where meaningful;
- match/mismatch;
- last update time;
- change cooldown constraints;
- required reauthentication after successful IP updates.

Never silently tell the user “live ready” if the current API traffic source violates the broker’s requirements.

Do not hard-code today’s exchange/broker rules if the broker provides authoritative discovery endpoints or documentation.

---

# 40. ORDER RATE LIMITS AND BROKER RULES

The execution engine must be rate-limit aware.

Current Upstox documentation lists combined order API limits of:

- Regular algo: 10 requests/sec, 500/min, 2000/30min;
- SEBI-registered algo: 50 requests/sec, 500/min, 2000/30min;
- other standard APIs: 50/sec, 500/min, 2000/30min.

These values must not be treated as eternal constants. Make rate-limit policy configurable/discoverable and expose effective limits in diagnostics where possible.

The engine must implement:

- request accounting;
- throttling;
- queueing where appropriate;
- bounded retry/backoff;
- rejection handling;
- no order duplication under retries.

Current Upstox V3 order documentation also indicates restrictions around market orders/market protection and errors related to static IP and algo name. The broker adapter must model current rules explicitly and report violations clearly rather than trying repeatedly.

---

# 41. BROKER ABSTRACTION

Use a broker interface that cleanly separates:

- LOCAL_PAPER;
- UPSTOX_SANDBOX;
- UPSTOX_LIVE;
- future brokers.

A sandbox broker must never route to a live host.

A live broker must clearly label itself as live.

Mode, broker, audit tables, dashboard, reports and alerts must agree about reality.

No “sandbox” label over a live trade.

No live broker operation from paper mode.

No hidden broker defaults.

---

# 42. PAPER TRADING

Paper trading remains a first-class mode.

It must support:

- historical replay;
- synthetic deterministic replay;
- near-live paper where actual market data is used but orders are simulated;
- positions;
- fills;
- fees/costs;
- slippage models;
- risk behavior;
- recovery;
- strategy testing.

Clearly label:

- OBSERVED;
- REPLAY;
- SYNTHETIC;
- SIMULATED;
- MODEL.

Do not make paper performance look like live performance.

---

# 43. SANDBOX

Preserve Upstox sandbox integration for end-to-end testing.

Sandbox credentials must never be reused against live hosts.

Sandbox tests should include:

- authentication/credential validation;
- order placement;
- order modification;
- cancellation;
- sliced orders;
- partial fills;
- reconciliation;
- restart recovery;
- unknown remote outcome recovery.

No sandbox action may have a path to real money.

---

# 44. EXACT LIVE ORDER ACCOUNTING REQUIREMENTS

Preserve the current hardening around sliced execution.

Every child/slice order ID should be persisted.

Track child-level quantity and status.

Aggregate fill quantities from child deltas exactly once.

Partial-then-cancel must credit exactly the executed quantity.

Do not reapply cumulative average prices to incremental fills.

Average cost, cash, fees and realized P&L must remain correct under partial fills.

Cancellation must never be reported as successful without broker confirmation.

Unknown orders must be reconstructed deterministically.

---

# 45. GUI MASTER BLUEPRINT

The GUI is a core product, not a cosmetic layer.

Persistent shell:

```text
┌────────────────────────────────────────────────────────────────────┐
│ AXIOM    [MARKET ▼] [UNIVERSE ▼] [MODE ●]   Search      Alerts ⚙ │
├──────────────┬─────────────────────────────────────────────────────┤
│ COMMAND      │                    WORKSPACE                        │
│ MARKETS      │                                                     │
│ RESEARCH     │                                                     │
│ MODELS       │                                                     │
│ TRADING      │                                                     │
│ RISK         │                                                     │
│ INTELLIGENCE │                                                     │
│ REPORTS      │                                                     │
├──────────────┴─────────────────────────────────────────────────────┤
│ Feed ● | Engine ● | Research ● | Kronos ● | Risk ● | Tasks 3     │
└────────────────────────────────────────────────────────────────────┘
```

Primary navigation:

- COMMAND CENTER
- MARKETS
- RESEARCH
- MODELS
- TRADING
- RISK
- INTELLIGENCE
- REPORTS

System/settings controls should live behind explicit system controls instead of cluttering the main workflow.

---

# 46. COMMAND CENTER

The Command Center is the system’s operational brain display.

It should answer:

- What is happening?
- What changed?
- What deserves attention?
- What is broken?
- What is waiting?
- What is the next useful action?

Show compact but meaningful panels for:

- market state;
- universe activity;
- current opportunities;
- research activity;
- Kronos health;
- broker/live health;
- risk;
- alerts;
- background tasks;
- what changed.

Provide universal search/command palette via `Ctrl+K`.

Search should span:

- instruments;
- research;
- strategies;
- experiments;
- reports;
- alerts;
- models;
- settings.

---

# 47. MARKETS WORKSTATION

Market workstation layout:

- watchlist/universe panel;
- main chart;
- analysis panel;
- market context;
- event/context ribbon;
- recommendation/evidence panel;
- what-changed panel.

Symbol selection should be instant.

Do not reload the entire application for each symbol change.

Use cached state and reactive updates.

---

# 48. STOCK DETAIL

Each instrument should support tabs such as:

- Overview
- Chart
- Analysis
- Kronos
- History
- Research
- Events
- Risk

Instrument pages must share a common state model.

Tabs must be real views, not decorative labels.

---

# 49. CHART SYSTEM

Support chart layouts such as:

- 1
- 2
- 4
- 6
- 8 panels

Support linked:

- symbol;
- timeframe;
- crosshair;
- zoom;
- events;
- regime;
- forecast;
- positions;
- signals;
- volume;
- indicators.

Charts must remain responsive under live updates.

Use virtualization/coalescing when necessary.

Provide focus mode:

**F** → full chart/minimal UI.

Do not trigger blocking I/O on chart paint/render code.

Actual and forecast series must remain visually and semantically distinct.

---

# 50. RESEARCH WORKSPACE

Provide a real research workstation with:

- experiment queue;
- active experiments;
- results;
- model comparisons;
- strategy lineage;
- research memory;
- next-best-experiment suggestions;
- resource usage;
- reproducibility information.

Research tasks should have live progress and cancellation/pause where technically safe.

---

# 51. MODEL ARENA

Provide a model comparison workspace.

Compare models by:

- latency;
- memory/VRAM;
- forecast accuracy;
- directional accuracy;
- performance by regime;
- stability;
- calibration where legitimately measured;
- failure rate;
- data requirements.

Do not fabricate benchmark values.

Show benchmark date and environment.

---

# 52. TRADING WORKSPACE

Provide a dedicated Trading workstation containing:

- Paper / Sandbox / Live mode;
- account;
- positions;
- orders;
- fills;
- P&L;
- exposure;
- risk;
- execution health;
- latency;
- live readiness;
- activity feed;
- explicit control buttons.

Paper and sandbox order forms should be straightforward.

Live order forms must provide more context and stronger confirmation.

---

# 53. RISK WORKSPACE

Show:

- exposure;
- concentration;
- daily loss;
- drawdown;
- open positions;
- risk utilization;
- risk events;
- limits;
- pending risk blocks;
- reconciliation anomalies.

The risk layer must remain independent of recommendation presentation.

---

# 54. INTELLIGENCE WORKSPACE

Show:

- market context;
- cross-market relationships;
- events;
- macro/regime state;
- sector/industry context;
- opportunity landscape;
- research themes.

Cross-market intelligence must not create fake precision when data is missing.

---

# 55. REPORTS

Support market/research/performance reports.

Reports should distinguish:

- OBSERVED;
- CALCULATED;
- MODEL INTERPRETATION.

Include:

- assumptions;
- data freshness;
- version/build identity;
- model identity;
- research lineage where appropriate.

---

# 56. ALERTS

Alert categories:

- opportunities;
- research;
- risk;
- Kronos;
- market event;
- system;
- live trading operations.

Each alert needs:

- severity;
- timestamp;
- age;
- source;
- affected object;
- message;
- acknowledgement state;
- remediation where possible.

Alert delivery must be durable where configured.

Critical alerts that remain unacknowledged should escalate according to policy.

---

# 57. TASK CENTER

Provide a live background-task center.

Tasks need:

- name;
- type;
- progress;
- state;
- start time;
- estimated completion when meaningful;
- resource use;
- cancellation/pause;
- error;
- recovery/retry.

Do not make the user stare at a frozen page while a task runs.

---

# 58. SYSTEM CONTROL CENTER

Provide a real GUI System Control Center.

Show health of:

- core;
- market engine;
- research engine;
- Kronos;
- database;
- broker;
- notifications;
- CPU;
- RAM;
- GPU;
- storage;
- network.

Provide controlled actions such as:

- restart component;
- restart worker;
- pause research;
- refresh data;
- repair state;
- open diagnostics;
- backup;
- restore;
- shutdown AXIOM.

No CLI instructions.

---

# 59. ONBOARDING / SETUP WIZARD

The first launch flow should be:

**WELCOME → MARKET → UNIVERSE → DATA → INTELLIGENCE → KRONOS → TRADING MODE → PREPARE → ENTER AXIOM**

Every step must be GUI-driven.

The wizard should detect available resources and make safe defaults.

It should clearly distinguish:

- local paper;
- replay;
- real market-data paper;
- sandbox;
- live.

Live should require extra readiness.

---

# 60. UNIVERSE SELECTION STUDIO

Create an explicit universe-selection studio.

Features:

- search;
- filter;
- exchange/market filter;
- sector filter where available;
- instrument-type filter;
- liquidity filters;
- history-depth filters;
- price/volume filters;
- manual selection;
- bulk selection;
- saved universes;
- import/export;
- duplicate detection;
- instrument validity check.

The user can select a large universe, but the system must handle subscription and compute costs intelligently.

Do not silently subscribe to “everything”.

---

# 61. WORKSPACE CUSTOMIZATION

The user should be able to:

- drag;
- resize;
- pin;
- collapse;
- maximize;
- reorder;
- save;
- duplicate;
- reset.

Named layouts may include:

- Intraday Hunt
- Research Lab
- Kronos Analysis
- Portfolio
- Presentation

Layouts persist.

---

# 62. UI STATE MODEL

Every important UI surface must explicitly support states such as:

- LOADING
- READY
- UPDATING
- STALE
- PARTIAL
- OFFLINE
- ERROR
- LOCKED
- DISABLED
- SUCCESS

Do not represent an unavailable value with a fake zero.

Use explicit “unknown/unavailable” semantics.

---

# 63. UI DESIGN SYSTEM

Target aesthetic:

**premium quantitative workstation**

Preferred visual language:

- dark graphite;
- warm off-white text;
- emerald AXIOM accent;
- violet/blue research accents;
- amber warning;
- red danger;
- green healthy.

Use a clean sans-serif for general text.

Use monospace for:

- prices;
- numbers;
- timestamps;
- IDs;
- technical diagnostics.

Avoid:

- neon cyberpunk overload;
- casino visual language;
- fake holograms;
- excessive glow;
- giant decorative cards.

The interface should feel calm, fast and information-rich.

---

# 64. UI PERFORMANCE TARGETS

Targets:

- button response: ideally <50 ms;
- local interaction: target within one frame / <16 ms where practical;
- navigation: near-instant from cached state;
- chart interaction: 60 fps target;
- no blocking I/O on the GUI thread;
- virtualize large lists;
- batch/coalesce high-frequency updates;
- avoid re-rendering unchanged objects.

Measure before optimizing and verify after optimizing.

---

# 65. CURRENT GUI LEGACY TO REPLACE/SUPERSEDE

The existing code includes a feature console that effectively exposes CLI commands through a browser UI.

That is a historical implementation strategy.

The target product must **not** continue using “GUI = CLI wrapper” as the architecture.

Replace/supersede it with real application services and direct typed UI actions.

The user must never see:

- “Run command”
- “Copy command”
- terminal output
- command-line parameters
- `.env` values
- shell diagnostics.

Human-readable GUI diagnostics can show the underlying reason, but must not expose a terminal as the workflow.

---

# 66. GUI-ONLY OPERATIONAL FLOWS

The following must all be possible from GUI:

### Install

Double-click executable → setup.

### Dependencies

GUI detects missing dependencies → install automatically where legally/technically appropriate → progress.

### Kronos

GUI → model selection → download → verify → install → benchmark → activate.

### Credentials

GUI → broker → connect → official authentication → secure token storage.

### Diagnostics

GUI → system health → actionable errors → repair.

### Updates

GUI → download → verify → prepare → install → migrate → health check → restart.

### Backup

GUI → backup/verify/restore.

### Research

GUI → create experiment → run → monitor → inspect result.

### Training

GUI → configure → train → progress → pause/cancel → evaluate.

### Market selection

GUI only.

### Paper/sandbox/live

GUI only.

### Shutdown

GUI only for normal users.

---

# 67. LIVE CREDENTIAL VAULT

Use an OS-secure credential store where available.

Possible fallback:

- encrypted secret blob;
- strong local key protection;
- restrictive filesystem ACLs;
- no plaintext logs.

Never require a user to create `.env` manually.

Never display complete tokens by default.

Provide GUI actions for:

- connect;
- reauthenticate;
- disconnect;
- rotate/remove credentials;
- test connection;
- inspect nonsecret credential metadata.

---

# 68. BACKUP / RESTORE

Provide GUI backup and restore.

Backup should include appropriate:

- database;
- research metadata;
- strategy lineage;
- model metadata;
- workspace settings;
- universe definitions;
- operational settings;
- nonsecret configuration.

Secrets should use a separate secure path and should not be copied blindly into backups.

Restore must verify integrity before replacing active state.

Protect against partial restores.

---

# 69. UPDATE SYSTEM

Implement GUI updates:

**CHECK → DOWNLOAD → VERIFY → PREPARE → MIGRATE → INSTALL → HEALTH CHECK → RESTART**

Never overwrite working binaries without validation.

Provide rollback/failure recovery where practical.

Preserve user state.

Version/build identity must be visible.

---

# 70. DATA STORAGE

Keep authoritative state in durable storage.

SQLite is acceptable for current/local architecture where it remains performant and correct.

Partition large market-data storage appropriately.

Use atomic writes.

Use versioned manifests/hashes for datasets.

Never mutate source data silently.

Maintain migration paths.

---

# 71. EVENT BUS / REACTIVE STATE

Use a shared event gateway between backend/core and GUI.

The GUI should not poll every stock individually.

Prefer:

- event stream;
- subscription topics;
- local reactive store;
- incremental state patches;
- cached snapshots;
- batched updates.

Every event should have:

- type;
- timestamp;
- source;
- object identity;
- version/sequence where appropriate;
- payload.

Detect dropped/out-of-order events and reconcile where needed.

---

# 72. CORE / UI API CONTRACT

Keep the UI presentation-only with respect to trading safety.

The UI may request:

- analysis;
- research;
- model actions;
- setup operations;
- paper orders;
- sandbox orders;
- live session transitions.

But the UI must never bypass:

- core validation;
- risk;
- broker rules;
- reconciliation;
- authentication;
- authorization.

No UI endpoint may become a hidden backdoor to live execution.

---

# 73. AUDIT TRAIL

Every important state-changing action should be auditable.

Record, as appropriate:

- who/what initiated it;
- timestamp;
- session/build version;
- mode;
- action;
- before state;
- after state;
- result;
- error if any.

Do not store secrets in audit records.

Audit should be replayable where it matters.

---

# 74. SECURITY

Threat-model at least:

- secret leakage;
- malicious model manifests;
- path traversal;
- credential theft;
- forged broker callbacks;
- replayed callbacks;
- duplicated orders;
- stale authentication;
- process injection where relevant;
- malicious local files;
- corrupted database;
- untrusted market/news input;
- compromised external API responses.

Use:

- state/correlation IDs for OAuth;
- strict redirect URI handling;
- token masking;
- secret scrubbing;
- constrained model-cache paths;
- no `eval`/`exec` on remote content;
- strict schema validation;
- bounded network retries;
- timeouts;
- safe logging.

---

# 75. MARKET EVENTS / EXTERNAL INTELLIGENCE

Preserve and extend the current Market Event Listener concepts.

Possible event categories include:

- war/escalation/ceasefire;
- sanctions/relief;
- tariffs/trade;
- policy/central bank;
- oil/gas;
- shipping;
- regulatory;
- geopolitical;
- market-specific corporate/sector events.

Use public/reputable sources and validate/deduplicate.

Model **transmission paths**, not simplistic “bad news = sell”.

Possible event response states:

- IGNORE
- MONITOR
- EVALUATE
- NO_TRADE

An event analyzer must provide context to research/recommendation layers.

It must never bypass hard risk or place orders directly.

Record event provenance and conflict among sources.

---

# 76. NEWS / EXTERNAL DATA FAILURE POLICY

If external news/data is unavailable:

- continue on internal market data;
- clearly mark missing context;
- degrade confidence where appropriate;
- never invent missing events.

Use timeout, retry, backoff, cache, source isolation and deduplication.

---

# 77. RESEARCH VALIDATION

All trading-related claims must survive out-of-sample validation.

Use chronological splits.

Use walk-forward evaluation.

Use locked test sets.

Use robustness/stress testing.

Avoid leakage.

Explicitly test:

- feature leakage;
- future timestamp contamination;
- overlapping labels;
- look-ahead bias;
- survivorship bias where possible;
- unrealistic fills;
- impossible data availability;
- hidden parameter tuning on test data.

---

# 78. BACKTESTING REALISM

Backtests should include realistic:

- fees;
- slippage;
- latency assumptions where relevant;
- liquidity constraints;
- order-type behavior;
- partial fills where supported;
- market session rules;
- position limits.

Backtests remain historical simulations.

Never present them as guarantees.

---

# 79. DETERMINISM

Preserve deterministic replay tests.

Where appropriate, run the same backtest:

- twice in-process;
- again in fresh processes;
- with different hash seeds;
- with canonicalized opaque IDs.

A mismatch should identify the first meaningful divergence.

Do not compare meaningless random UUID identities directly when the decision sequence is what matters.

---

# 80. TEST PYRAMID

Maintain multiple test layers:

### Unit

Pure functions, guards, validators, state transitions.

### Integration

Subsystem interaction.

### UI contract

UI action/state behavior.

### Visual/regression

Critical layout/appearance contracts where tooling permits.

### Responsive

Desktop/QHD/4K/ultrawide/laptop/mobile-recomposed layouts.

### End-to-end

Real application binary/service flow.

### Failure injection

Network loss, stale feed, token expiry, worker crash, DB corruption simulation, broker mismatch, duplicate launch, partial fill, cancelled order, unknown order, slow inference, GPU exhaustion, disk pressure.

### Frozen executable

Verify the actual packaged executable, not only source code.

---

# 81. GUI ACCEPTANCE MATRIX

Every advertised action must be exercised by a test.

Examples:

- market switching;
- universe selection;
- save/load universe;
- symbol selection;
- chart layout;
- chart linking;
- research start/pause/cancel;
- model download/install/activate;
- broker connect/disconnect;
- paper order;
- sandbox flow;
- live readiness;
- live start confirmation;
- pause new orders;
- emergency stop;
- shutdown;
- reopen/recovery;
- backup/restore;
- update.

No dead buttons.

No fake navigation.

No placeholder workflows.

---

# 82. COMPLETE GUI LIFECYCLE TEST

A complete automated/manual acceptance scenario must be possible without a terminal:

**Install**
→ **Launch**
→ **Setup**
→ **Select market**
→ **Select stocks**
→ **Connect data**
→ **Install Kronos**
→ **Configure research**
→ **Run experiment**
→ **View results**
→ **Start paper**
→ **Configure sandbox**
→ **Connect broker**
→ **Configure live**
→ **Authenticate broker through official broker UI**
→ **Readiness checks**
→ **Start live**
→ **Pause**
→ **Close UI**
→ **Core continues**
→ **Reopen**
→ **State restored**
→ **Reconcile**
→ **Shutdown**.

Any requirement for:

- CMD;
- PowerShell;
- Python;
- pip;
- Git;
- `.env` editing;
- manual JSON;
- manually started server;
- manual model download;
- manual worker startup

is a **product acceptance failure**.

The only deliberate exception is a third-party authentication UI, such as the official Upstox login/consent page.

---

# 83. FROZEN BUILD REQUIREMENTS

Preserve one-file packaging where practical.

For Windows:

- double-click executable;
- no terminal required;
- setup wizard launches;
- core starts;
- GUI connects;
- data state works;
- paper flow works;
- recovery works;
- duplicate launch is safe;
- secrets do not leak;
- build identity is consistent.

Validate actual served HTML/JS against a real parser where relevant.

Do not trust source-only tests for packaged behavior.

---

# 84. RELEASE/CI GATES

CI must block release on failed:

- source tests;
- packaging tests;
- frozen E2E tests;
- frontend integrity tests;
- security tests;
- critical performance regressions;
- critical UI action-matrix failures.

Do not hide failures behind `continue-on-error`.

Do not turn failing tests into skips merely to get green CI.

Skips must have a legitimate external/environment reason.

---

# 85. DIAGNOSTICS

Diagnostics should be human-readable.

Show:

- component;
- state;
- reason;
- impact;
- suggested action;
- whether retrying is safe;
- whether trading is blocked;
- recovery status.

Do not dump Python tracebacks at the user.

Advanced diagnostics may expose technical details behind an explicit expert view/export function.

---

# 86. OFFLINE / DEGRADED MODE

AXIOM should remain useful when external systems are unavailable.

Examples:

- offline research;
- cached market state;
- replay;
- local reports;
- local historical analysis;
- model benchmarking if weights are installed.

Clearly mark offline/degraded state.

Do not pretend a disconnected broker is connected.

---

# 87. PERFORMANCE BUDGETING

Define budgets for:

- UI frame work;
- market event handling;
- persistence queue;
- inference;
- API requests;
- database writes;
- research scheduling.

Instrument bottlenecks.

Do not optimize the wrong thing.

---

# 88. FAILURE INJECTION TARGETS

At minimum test:

- feed disconnect;
- feed reconnection;
- delayed feed;
- missing data;
- malformed data;
- data duplication;
- stale cache;
- Kronos crash;
- Kronos timeout;
- model checksum failure;
- model path traversal attempt;
- database interruption;
- database corruption simulation;
- broker timeout;
- broker 401/403;
- broker rate limit;
- remote order unknown;
- partial fill;
- child-order cancellation;
- token expiry;
- static-IP mismatch;
- invalid algo name;
- duplicate engine;
- UI crash;
- UI close/reopen;
- OS reboot simulation where feasible;
- disk-full/low-disk warning;
- CPU/RAM/VRAM pressure.

---

# 89. SAFETY BOUNDARY: RESEARCH CANNOT MUTATE HARD SAFETY

Create architectural enforcement that research/learning code cannot call arbitrary functions that mutate:

- risk limits;
- authentication policy;
- live mode authorization;
- emergency stop;
- broker identity;
- audit integrity.

Prefer type/interface boundaries rather than comments.

Test the boundary.

---

# 90. USER EDUCATION IN PRODUCT

Where an operation is potentially consequential, explain:

- what it will do;
- what it will change;
- what can go wrong;
- how to stop/recover.

Use plain language.

Do not bury important consequences behind jargon.

---

# 91. LEGAL/ELIGIBILITY COMPLIANCE

Do not bypass broker, exchange, jurisdictional, regulatory, account-eligibility, identity, or age restrictions.

The product must never invent credentials or evade a broker’s eligibility rules.

Live trading can only operate on an account/session that is legitimately permitted to use the relevant broker/API.

---

# 92. CURRENT UPSTOX FACTS TO VERIFY AT IMPLEMENTATION TIME

Use the official Upstox developer documentation as the authoritative source for current behavior.

Known current facts at authoring time include:

- OAuth 2.0 authorization-code login;
- login handled on Upstox;
- access token obtained through code exchange;
- authorization code is single-use;
- access token expires at 3:30 AM the following day;
- user-level static IP APIs exist;
- static-IP enforcement can reject order requests from unregistered IPs;
- successful static-IP updates invalidate existing access tokens;
- current API order rate limits include 10/sec regular algo and 50/sec registered algo, with 500/min and 2000/30min caps for combined order APIs;
- V3 order APIs have current constraints around order types/market protection and may reject invalid algo-name/static-IP requests.

Before encoding exact values in code or user-facing documentation, re-check the current official docs.

---

# 93. PRODUCT STATES MUST BE TRUTHFUL

Examples:

If the broker is not authenticated:

**BROKER — NOT CONNECTED**

Not:

**BROKER — READY**

If data is stale:

**STALE — LAST UPDATE 43s AGO**

Not:

**LIVE**

If Kronos uses a development stub:

**KRONOS — STUB / DEVELOPMENT**

Not:

**AI FORECAST ENGINE READY**

If a model’s pretrained weights have not been downloaded:

**WEIGHTS — NOT VERIFIED**

Not:

**MODEL VALIDATED**

---

# 94. NO FAKE “AI MAGIC”

Do not hide simple heuristics behind an AI label.

Every model-driven recommendation must identify:

- model;
- version/revision;
- input data;
- freshness;
- result;
- uncertainty;
- validation status.

Every heuristic must be labeled as heuristic/calculated where appropriate.

---

# 95. OBSERVATIONAL LEARNING

Continue the useful existing concept of recording market/strategy response for later research.

Do not confuse observational memory with online truth.

When a model observes an event and stores the response, preserve:

- observation timestamp;
- source;
- feature state;
- outcome horizon;
- regime;
- data quality.

Do not mutate historical observations after the fact merely to improve model fit.

---

# 96. AUTONOMOUS RESEARCH LOOP

A mature AXIOM cycle should look like:

**Observe → Detect → Explain → Compare → Hypothesize → Test → Validate → Remember → Recommend → Monitor → Review → Improve**

The loop must be bounded.

Avoid runaway experiment generation.

Use resource budgets, scheduling priorities and experiment quotas.

---

# 97. RESEARCH COMPUTE GOVERNOR

The scheduler should understand when markets are open and when execution is active.

During trading-critical windows:

- reserve resources for hot-path tasks;
- reduce/cap large research jobs;
- prioritize active opportunity analysis;
- protect live reconciliation.

During lower-intensity periods:

- expand historical research;
- run backtests;
- train/evaluate models;
- update reports;
- refresh datasets.

Never starve execution because an offline sweep was scheduled.

---

# 98. MODEL AGREEMENT / DISAGREEMENT

Track disagreement between:

- statistical baselines;
- strategy models;
- Kronos;
- historical analogues;
- regime-conditioned models.

Do not treat disagreement as failure automatically.

Sometimes disagreement is exactly what deserves research attention.

Surface it explicitly.

---

# 99. COUNTERFACTUAL SIMULATION

For important recommendation candidates, provide “what if?” analysis.

Examples:

- different entry timing;
- different sizing;
- no-trade;
- delayed entry;
- alternative strategy;
- historical analogue outcomes.

Counterfactuals must clearly state that they are simulations, not observed reality.

---

# 100. OPERATIONAL UX RULE

Every action follows:

**CLICK → IMMEDIATE FEEDBACK → BACKGROUND WORK → LIVE PROGRESS → SUCCESS/FAILURE → RECOVERY**

No button should appear dead while work is happening.

Every object follows:

**VIEW → INSPECT → UNDERSTAND → ACT**

Every important information surface follows:

**CONTEXT → CURRENT STATE → WHAT CHANGED → EVIDENCE → AVAILABLE ACTION**

---

# 101. RESPONSIVE DESIGN

Support:

- 1440p desktop;
- QHD;
- 4K;
- ultrawide;
- laptop widths;
- mobile/recomposed layouts where supported.

Do not simply shrink desktop UI onto a phone.

Recompose navigation and panel density.

---

# 102. OBSERVABILITY

Instrument:

- event throughput;
- queue depth;
- dropped events;
- stale instruments;
- cache hit ratio;
- database latency;
- model latency;
- broker latency;
- reconciliation freshness;
- task latency;
- GUI frame latency where measurable;
- CPU/RAM/GPU/storage pressure;
- alert delivery.

Expose important operational metrics in the GUI.

---

# 103. LOGGING

Logs must be useful to engineers without leaking secrets.

Use structured events where possible.

Include:

- timestamp;
- component;
- event type;
- correlation/session ID;
- severity;
- object ID;
- human-readable explanation.

Scrub:

- passwords;
- tokens;
- API secrets;
- authorization codes;
- signed URLs if sensitive.

---

# 104. DATABASE MIGRATIONS

Use additive, explicit migrations.

Never silently delete historical trading/research state.

Migration should be:

- idempotent;
- tested;
- recoverable;
- versioned.

Backup before destructive migration.

---

# 105. FROZEN EXECUTABLE RECOVERY TEST

The packaged executable must be tested for:

1. clean start;
2. setup;
3. paper operation;
4. state persistence;
5. restart;
6. duplicate launch;
7. GUI reconnect;
8. recovery of orders/positions where applicable;
9. no secrets in boot/logs;
10. healthy shutdown.

Where live execution is testable, use sandbox/mock broker or a purpose-built fake broker, not real money in automated CI.

---

# 106. TEST REALISM

Do not claim real-broker verification without real-broker interaction.

Do not claim pretrained Kronos verification without verified pretrained weights.

Do not claim rendered Qt verification without actually rendering on a suitable host.

Use the exact vocabulary:

- VERIFIED
- NOT VERIFIED
- NOT ATTEMPTED
- FAILED

Where appropriate.

---

# 107. DOCUMENTATION RECONCILIATION

After implementation, update documentation to match reality.

Eliminate contradictory version claims.

Do not leave old docs saying “paper only” if the target product legitimately supports live mode.

Do not claim “live ready” merely because live code exists.

Document the actual operational state machine.

Document GUI-only flows.

Document recovery.

Document broker constraints.

---

# 108. NAMING / VERSIONING

Product display name:

**AXIOM-MONEY-PRINTER**

Short display name:

**AXIOM**

Keep software version distinct from product codename.

Build identity should be surfaced consistently in:

- application metadata;
- UI About/system page;
- diagnostics;
- logs;
- reports;
- executable metadata where practical.

---

# 109. KEEP CURRENT GOOD WORK

The baseline already contains extensive hardening that must not be regressed, including concepts around:

- fail-closed routing;
- explicit strategy lifecycle;
- deterministic intent IDs;
- child-order accounting;
- partial-fill correctness;
- reconciliation;
- emergency stop;
- token freshness;
- watchdogs;
- daily-loss/drawdown stops;
- alerts;
- duplicate-engine protection;
- deterministic backtesting;
- data validation;
- dataset hashing;
- Kronos process isolation;
- model path confinement;
- forecast provenance;
- frozen packaging checks.

Do not throw these away during the redesign.

---

# 110. TARGET ARCHITECTURE SUMMARY

The desired high-level architecture is approximately:

```text
                   ┌───────────────────────────────┐
                   │       AXIOM GUI / UX          │
                   │ Command · Markets · Research  │
                   │ Models · Trading · Risk       │
                   │ Intelligence · Reports       │
                   └──────────────┬────────────────┘
                                  │ reactive API/events
                                  ▼
                   ┌───────────────────────────────┐
                   │     AXIOM SUPERVISOR/CORE     │
                   │ state · scheduler · tasks     │
                   │ event gateway · persistence   │
                   └──────┬─────────┬──────────────┘
                          │         │
              ┌───────────┘         └──────────────┐
              ▼                                    ▼
     ┌──────────────────┐                 ┌──────────────────┐
     │ MARKET/RESEARCH  │                 │ EXECUTION/RISK   │
     │ data · regime    │                 │ paper · sandbox  │
     │ analogues ·      │                 │ live · guards    │
     │ discovery        │                 │ reconciliation   │
     └─────────┬────────┘                 └─────────┬────────┘
               │                                    │
               ▼                                    ▼
     ┌──────────────────┐                 ┌──────────────────┐
     │ MODEL/KRONOS     │                 │ BROKER LAYER     │
     │ isolated worker  │                 │ Upstox etc.      │
     │ cache/integrity  │                 │ authenticated    │
     └──────────────────┘                 └──────────────────┘
               │                                    │
               └────────────────┬───────────────────┘
                                ▼
                    ┌──────────────────────────┐
                    │ DURABLE STATE / AUDIT    │
                    │ DB · datasets · memory   │
                    │ lineage · recovery       │
                    └──────────────────────────┘
```

Keep hot execution paths insulated from UI and heavy research.

---

# 111. DEVELOPMENT STRATEGY

Do not perform a reckless big-bang rewrite if a staged migration is safer.

A preferred internal sequence is:

### Phase A — Baseline audit

Inspect everything and write an internal gap matrix.

### Phase B — Architecture refactor

Stabilize core/service/event/state boundaries.

### Phase C — GUI foundation

Persistent shell, reactive state, router, task center, system health.

### Phase D — Universe/market workstation

Multi-market context and user-selected universes.

### Phase E — Intelligence/research

Opportunity Center, evidence stack, analogues, missed opportunities, research lab.

### Phase F — Kronos/model studio

GUI-managed model lifecycle and integration.

### Phase G — Trading/risk

Unified paper/sandbox/live model and live readiness center.

### Phase H — Recovery/operational hardening

Supervisor, crash recovery, reconciliation, backups, alerts.

### Phase I — Performance

Hot/warm/cold architecture, profiling, optimization.

### Phase J — Frozen packaging

End-to-end executable verification.

Do not stop between phases if remaining work is possible.

---

# 112. IMPLEMENTATION RULES

### Rule 1
Inspect existing code before creating a new subsystem.

### Rule 2
Prefer reuse of proven code over duplicate implementations.

### Rule 3
Prefer explicit interfaces and typed contracts.

### Rule 4
Prefer deterministic tests.

### Rule 5
Make state transitions explicit.

### Rule 6
Do not let learning rewrite safety.

### Rule 7
Do not let the GUI bypass the core.

### Rule 8
Do not let research starve execution.

### Rule 9
Do not let UI closure imply core shutdown.

### Rule 10
Do not allow duplicate live engines.

### Rule 11
Do not allow an unknown remote order to trigger a blind retry.

### Rule 12
Do not treat missing data as zero.

### Rule 13
Do not treat model output as observed reality.

### Rule 14
Do not expose secrets.

### Rule 15
Do not stop because the first implementation works.

---

# 113. WHAT “DONE” MEANS

AXIOM-MONEY-PRINTER is not done when:

- the UI looks good;
- tests are green;
- a build exists;
- a button works on the happy path;
- a forecast renders;
- a broker call succeeds once.

It is done only when the integrated product can satisfy the product contract under normal operation and predictable failures.

At minimum, final acceptance must cover:

- architecture;
- functionality;
- GUI behavior;
- responsive behavior;
- persistence;
- recovery;
- data integrity;
- research integrity;
- model integrity;
- execution safety;
- broker correctness;
- live readiness;
- emergency stop;
- secret hygiene;
- packaging;
- deterministic verification;
- CI gates.

---

# 114. FINAL ACCEPTANCE CHECKLIST

Before claiming completion, verify the following.

## Product

- [ ] AXIOM-MONEY-PRINTER identity is coherent.
- [ ] AXIOM is a market intelligence/research/execution workstation.
- [ ] No misleading profit guarantees.

## Repository

- [ ] Current codebase inspected.
- [ ] Existing good work preserved.
- [ ] Stale docs reconciled.
- [ ] Version/build identity consistent.

## GUI

- [ ] GUI-first.
- [ ] No normal-user CLI requirement.
- [ ] No fake CLI wrapper presented as the product.
- [ ] All major navigation works.
- [ ] All advertised buttons work.
- [ ] Error/loading/stale/offline states work.
- [ ] Responsive layouts work.
- [ ] Performance acceptable.

## Markets / Universes

- [ ] Market switching works.
- [ ] User-selected universes work.
- [ ] Market-scoped persistence works.
- [ ] Dynamic subscriptions work.
- [ ] No unintended all-instrument subscription.

## Data

- [ ] Validation strict.
- [ ] Provenance preserved.
- [ ] Gaps handled correctly.
- [ ] Stale state visible.
- [ ] Backfill/recovery works.

## Research

- [ ] Regime engine works.
- [ ] Opportunity Center works.
- [ ] Evidence stack works.
- [ ] Historical analogues work.
- [ ] Missed-opportunity memory works.
- [ ] False-negative research works.
- [ ] Research Lab works.
- [ ] Experiments reproducible.

## Models/Kronos

- [ ] GUI model management works.
- [ ] Integrity checks work.
- [ ] Model isolation preserved.
- [ ] Actual vs forecast distinction preserved.
- [ ] Benchmarks truthful.

## Trading

- [ ] Paper mode works.
- [ ] Sandbox mode works.
- [ ] Live is first-class.
- [ ] Live is not permanently hard-coded off.
- [ ] Live requires explicit readiness/authorization.
- [ ] Order path has independent guards.
- [ ] Reconciliation works.
- [ ] Unknown-order recovery works.
- [ ] Partial/sliced accounting works.

## Safety

- [ ] Emergency stop works.
- [ ] Risk controls cannot be weakened by learning.
- [ ] Duplicate live engine is prevented.
- [ ] Data/broker failures fail safely.
- [ ] Credential secrets remain protected.

## Recovery

- [ ] UI close/reopen works.
- [ ] Core persistence works.
- [ ] Crash/reboot recovery works where testable.
- [ ] Broker state reconciliation works.
- [ ] Research checkpoint recovery works.
- [ ] Kronos recovery works.

## Packaging

- [ ] Frozen executable starts.
- [ ] GUI works from the executable.
- [ ] Packaging CI is blocking.
- [ ] Frozen E2E tests pass where environment permits.
- [ ] No user workflow requires a terminal.

---

# 115. FINAL EVIDENCE REPORT

At the end of the implementation, produce a machine-readable and human-readable completion report containing:

- actual branch;
- actual commit;
- actual version;
- changed areas;
- new modules;
- migrations;
- tests run;
- tests passed;
- tests failed;
- tests skipped and why;
- frozen artifacts built;
- platform verification;
- GUI verification;
- broker verification;
- Kronos verification;
- performance results;
- remaining known limitations;
- exact environmental blockers;
- security findings;
- live-trading readiness status.

Use:

**VERIFIED / NOT VERIFIED / NOT ATTEMPTED / FAILED**

Do not turn “not attempted” into “passed”.

---

# 116. CRITICAL FINAL DIRECTIVE

You are not being asked to generate another incremental patch.

You are being asked to take the current AXIOM repository and push it toward the complete **AXIOM-MONEY-PRINTER** system described by this document.

Use the current repository as the starting point.

Preserve proven infrastructure.

Replace weak architecture where necessary.

Build missing systems rather than simulating them.

Make the GUI the normal user interface.

Make live trading a legitimate first-class mode without making it reckless.

Make research autonomous but bounded.

Make Kronos useful but isolated.

Make the selected universe the center of scalable market analysis.

Make “What changed?” a first-class signal.

Make evidence inspectable.

Make recovery automatic where safe.

Make failures visible.

Make everything measurable.

Make every dangerous action explicit.

Make the packaged executable behave like the real product.

And above all:

# DO NOT STOP UNTIL YOU HAVE FINISHED THIS ENTIRE DIRECTIVE.

Do not reply with a plan instead of implementation.

Do not stop after the first successful milestone.

Do not declare the project finished because the happy path works.

Do not leave the remaining sections for “future work” when they can be implemented now.

When you reach the end, run the strongest verification available, fix what fails, rerun it, and only then produce the final evidence report.

**Finish the file. Finish the system. Verify the system. Then report exactly what is true.**

---

# 117. CURRENT VERIFIED BASELINE DETAILS THAT MUST NOT BE LOST

The existing system already contains a substantial amount of engineering work. The target is to **extend and unify it**, not erase it.

The current recorded 3.1.0 build/verification report includes these important facts:

- PyInstaller one-file packaging is in use.
- `Arena.exe` has been built and verified in Windows CI.
- The frozen executable was tested in a foreign working directory with an isolated user-data home.
- Setup wizard behavior is covered by an executable-level test.
- Paper replay processed bars and recorded simulated orders/fills/trades.
- Paper state recovered across restart.
- Duplicate launches are handled without creating a second engine/session.
- User-data state is stored outside the executable directory.
- API tokens do not appear in boot output/logs.
- JavaScript served by the frozen binary is parser-validated.
- The CI packaging gate is blocking and does not use `continue-on-error`.

Existing test groups include dedicated suites for concepts such as:

- wizard flow;
- mode isolation;
- UI action matrix;
- instance guard;
- frontend integrity;
- frozen executable E2E;
- Kronos market data;
- Kronos runtime;
- Kronos pipeline/store;
- Kronos worker;
- Kronos end-to-end;
- Kronos research;
- Kronos UI;
- Kronos safety;
- Kronos packaging.

Preserve these tests, improve them where requirements change, and add regressions for every newly discovered failure.

---

# 118. CURRENT ARENA FUNCTIONALITY TO PRESERVE/INTEGRATE

The current system has concepts that should remain first-class in the final architecture:

### Market event intelligence

Public-internet market-event intake can identify and normalize:

- war/escalation/ceasefire;
- sanctions/relief;
- tariffs/trade;
- policy/central-bank changes;
- oil/gas changes;
- shipping events;
- regulatory changes;
- geopolitical events.

It performs relevance assessment, source validation, normalization, deduplication and transmission-path impact analysis.

It produces contextual responses such as:

- IGNORE;
- MONITOR;
- EVALUATE;
- NO_TRADE.

It does not place orders directly.

Integrate this capability into the new Intelligence and Evidence Stack rather than leaving it as a disconnected historical panel.

### Strategy Router

The existing router/research framework should remain evidence-driven.

Negative out-of-sample expectancy must not become tradable merely because a current market move looks exciting.

### Strategy lifecycle

The existing lifecycle gates are important and must remain enforced at the final execution boundary.

### Adaptive discovery

Existing candidate discovery/evolution should become part of the new Research Lab and Strategy Lineage system.

### Monitoring

Existing reality-gap/anomaly/system monitoring should feed the new System Control Center, Risk workspace, Alerts center and reports.

### Recovery

Existing database backup/recovery concepts should become real GUI workflows.

---

# 119. CURRENT KRONOS BASELINE DETAILS

The current Kronos subsystem has a model configuration concept roughly including:

- model such as `NeoQuasar/Kronos-small`;
- tokenizer such as `NeoQuasar/Kronos-Tokenizer-base`;
- configurable lookback;
- configurable prediction horizon;
- known context limit validation;
- temperature/top-k/top-p controls;
- sample-path count;
- automatic device selection;
- closed-candle update behavior.

The current subsystem has explicit states such as:

- DISABLED;
- INITIALIZING;
- DOWNLOADING;
- LOADING;
- READY;
- INFERENCING;
- STALE;
- OFFLINE;
- ERROR;
- INSUFFICIENT_HISTORY;
- DATA_ERROR.

Preserve this explicit-state architecture and integrate it into the new Models/System status surfaces.

The current model cache uses SHA-256 manifest checking and path confinement. Preserve this.

The current forecast stack keeps actual and forecast values as distinct concepts/types. Preserve this.

The current forecasting research uses chronological walk-forward evaluation and explicitly tests for leakage. Preserve and expand this.

---

# 120. CURRENT MARKET-DATA BASELINE DETAILS

The existing data layer contains:

- official Upstox instrument synchronization;
- instrument metadata storage;
- historical candle synchronization;
- intraday candle support;
- source-labelled continuity merging;
- partitioned market-data storage;
- dataset manifests/hashes;
- data quality checks;
- realtime feed support;
- protobuf support for Upstox streaming data;
- replay/synthetic data providers.

Historical synchronization already follows documented chunk constraints rather than fetching arbitrary enormous windows.

Preserve resumable downloads.

Preserve deduplication by timestamp.

Preserve quality states such as VALID/WARNING/INVALID/MISSING/UNVERIFIED where currently used.

Do not silently turn a quality warning into usable truth.

---

# 121. CURRENT EXECUTION HARDENING DETAILS

Existing execution hardening includes concepts around:

- multi-instrument identity validation;
- fail-closed routing;
- strategy lifecycle at execution boundary;
- no candidate means no entry;
- deterministic intent identities;
- remote-order recovery via intent-derived identifiers/tags;
- unknown remote-order outcomes remain unresolved rather than being blindly retried;
- cancellation requires broker confirmation;
- per-child sliced order persistence;
- duplicate transition protection;
- exact incremental fill accounting;
- adaptive execution cadence.

These are not optional historical quirks. They are part of the safety baseline.

---

# 122. CURRENT LIVE-OPS BASELINE DETAILS

The current live operations layer already has concepts including:

### Token freshness

Detects stale/expiring credentials and blocks new entries when credential state cannot be safely verified.

### Canary ramp

First live entries can be capped by order count/notional until the ramp completes.

### Watchdog

Detects stuck orders, stalled reconciliation, broker disablement and feed silence.

### Breach monitor

Daily loss and drawdown breaches can trigger emergency stop behavior.

### Alerts

Durable alerts, optional webhook delivery, retry/backoff, deduplication and acknowledgement exist.

### Single-engine lock

A healthy live engine heartbeat prevents a second live engine from trading the same account/database.

### Audit replay

The operational layer can audit and replay important order trails.

Preserve these capabilities while moving their controls into the GUI.

---

# 123. GUI TRAY REQUIREMENTS

A normal AXIOM desktop session should support a system tray presence.

Tray actions should include, where appropriate:

- Open AXIOM;
- Pause Research;
- Open Opportunities;
- Open Paper Trading;
- View Alerts;
- Stop Paper;
- Shutdown AXIOM.

For live trading, do not create a tray action that can casually toggle live execution without the same authorization/readiness checks used by the main GUI.

Closing the main window must not silently become a trading control operation.

---

# 124. GUI PAGE MAP

The final desktop product should converge toward the following primary page model:

1. **COMMAND CENTER**
2. **MARKETS**
3. **RESEARCH**
4. **MODELS**
5. **TRADING**
6. **RISK**
7. **INTELLIGENCE**
8. **REPORTS**

Within these pages, use subviews/tabs rather than proliferating dozens of unrelated top-level pages.

The existing older desktop page set must be mapped deliberately into this new architecture, not blindly duplicated.

---

# 125. COMMAND CENTER — FINAL INFORMATION HIERARCHY

Top priority:

1. system status;
2. market/universe state;
3. what changed;
4. opportunities;
5. risk;
6. alerts;
7. research progress;
8. model/Kronos health;
9. tasks.

Do not put secondary settings above actionable market intelligence.

---

# 126. MARKETS — FINAL INFORMATION HIERARCHY

Top priority:

1. market/session state;
2. selected universe;
3. selected instrument;
4. price/chart;
5. what changed;
6. evidence;
7. opportunity state;
8. model context;
9. risk/context.

---

# 127. RESEARCH — FINAL INFORMATION HIERARCHY

Top priority:

1. current research queue;
2. active experiments;
3. result quality;
4. unresolved hypotheses;
5. missed opportunities;
6. research memory;
7. strategy lineage;
8. next best experiment.

---

# 128. MODELS — FINAL INFORMATION HIERARCHY

Top priority:

1. active models;
2. runtime status;
3. model health;
4. benchmark;
5. accuracy/validation;
6. regime performance;
7. installation/integrity;
8. resource use.

---

# 129. TRADING — FINAL INFORMATION HIERARCHY

Top priority:

1. mode;
2. broker;
3. readiness;
4. positions;
5. orders;
6. fills;
7. P&L;
8. risk;
9. execution health;
10. emergency controls.

---

# 130. RISK — FINAL INFORMATION HIERARCHY

Top priority:

1. current risk state;
2. limit utilization;
3. exposure;
4. drawdown;
5. daily loss;
6. concentration;
7. active breaches;
8. reconciliation.

---

# 131. INTELLIGENCE — FINAL INFORMATION HIERARCHY

Top priority:

1. market context;
2. cross-market context;
3. events;
4. regimes;
5. sector/industry state;
6. opportunity themes;
7. evidence quality.

---

# 132. REPORTS — FINAL INFORMATION HIERARCHY

Report types should include:

- market report;
- research report;
- strategy report;
- model report;
- trading/performance report;
- risk report;
- operational report.

Every report should carry build/version/data/model provenance where relevant.

---

# 133. CURRENT USER INTERACTION MODEL

The user should not need to understand the internal architecture to operate AXIOM.

The GUI should expose concepts in natural order.

For example:

Instead of:

`UPSTOX_ACCESS_TOKEN`

show:

**Upstox Connection**

Status: `CONNECTED`

Token: `VALID` / `EXPIRING` / `EXPIRED`

Instead of:

`DEFAULT_TIMEFRAME=5m`

show:

**Primary analysis timeframe: 5 minutes**

Instead of:

`MAX_POSITION_VALUE=10000`

show:

**Maximum position value: ₹10,000**

Internally keep precise configuration structures.

---

# 134. GUI FORM SAFETY

Dangerous forms must display:

- current value;
- proposed value;
- effect;
- validation;
- scope;
- whether restart is required;
- whether live trading is active;
- whether the change can affect live orders.

Never allow malformed numeric/enum configuration to propagate into a live engine.

---

# 135. LIVE MODE UX STATES

Top-level mode indicator must distinguish:

- PAPER
- SANDBOX
- LIVE — CONFIGURED
- LIVE — READY
- LIVE — ARMED
- LIVE — ACTIVE
- LIVE — PAUSED
- LIVE — RECONCILING
- LIVE — STOPPED
- LIVE — BLOCKED

The color/appearance alone must not be the only distinction. Use text/state semantics.

---

# 136. LIVE SESSION CLOSE/REOPEN POLICY

If LIVE ACTIVE and the user closes the main UI:

- the core must retain explicit live state;
- the UI must clearly state on reopen that live execution continued;
- reopening must immediately show current broker reconciliation;
- the user must never accidentally start a second engine;
- UI closure must not be interpreted as an emergency stop.

If the user explicitly asks for AXIOM shutdown while LIVE ACTIVE:

- show a high-consequence warning;
- require deliberate confirmation;
- transition through a controlled pause/reconciliation sequence;
- never silently abandon broker reconciliation.

---

# 137. LIVE EMERGENCY MODE

Emergency stop must be visually and operationally prominent.

When active:

- new entries blocked;
- status obvious across relevant pages;
- reason recorded;
- affected strategies shown;
- current positions still visible;
- reconciliation continues where possible;
- recovery requires deliberate action.

Do not hide emergency state in a settings page.

---

# 138. DATA FRESHNESS UX

Every realtime-derived important panel should have a visible freshness indicator.

Examples:

- LIVE / just updated;
- 2s old;
- STALE 30s;
- BACKFILLING;
- OFFLINE.

Use per-object freshness where a single global label would be misleading.

---

# 139. OPPORTUNITY FRESHNESS

Opportunity objects should expire or downgrade when their evidence becomes stale.

A high-conviction setup from 20 minutes ago should not still look equally actionable if the market has materially changed.

Store creation time, last-evidence time, last-update time and invalidation reason.

---

# 140. RECOMMENDATION EXPIRY

Recommendations must have validity windows.

When material evidence changes:

- recalculate;
- mark prior recommendation stale/invalidated;
- record the transition;
- do not preserve old recommendations as though still current.

---

# 141. EVIDENCE AGE

The evidence stack should show how old each important evidence component is.

Example:

- price: 300 ms;
- volume: 300 ms;
- regime: 2.1 s;
- Kronos: 8.4 s;
- analogue search: 22 s;
- news: 4 min.

This helps users understand that “current recommendation” is a composition of components with different freshness.

---

# 142. CROSS-MARKET CONTEXT

Global market context may include:

- major indices;
- currencies;
- commodities;
- rates where data is available;
- sector relationships;
- market-session interactions.

Do not fabricate unavailable cross-market values.

Use timezone/session-aware data handling.

---

# 143. MARKET SESSION ENGINE

Each market should expose:

- current session state;
- next session;
- time until open/close;
- holiday/closed reason;
- timezone;
- data availability state.

The engine must use the market calendar instead of assuming every weekday is identical.

---

# 144. TIME SYNCHRONIZATION

Live readiness should test clock sanity.

Market-event timestamps, broker timestamps and local timestamps should be normalized with explicit timezone handling.

Do not silently assume naive timestamps are UTC.

Detect impossible future/past timestamps where appropriate.

---

# 145. RECOMMENDATION SIZING

The intelligence layer may estimate suitability of a candidate size, but the final executable size must pass portfolio/risk/broker constraints.

Never allow a model’s “recommended size” to bypass hard limits.

Display:

- suggested size;
- max permitted size;
- actual submitted size;
- reason for any reduction/refusal.

---

# 146. SHADOW/PAPER/LIVE COMPARISON

Where possible, maintain parallel evaluation of:

- intended decision;
- paper outcome;
- sandbox outcome;
- live outcome.

Use this to measure the reality gap.

Do not mix P&L between modes.

---

# 147. REALITY GAP

Track differences between:

- backtest;
- walk-forward;
- paper;
- sandbox;
- live, when legitimately available.

Possible reasons:

- slippage;
- latency;
- liquidity;
- model drift;
- regime shift;
- data quality;
- execution failure.

Do not reduce reality gap to a single unexplained percentage.

---

# 148. PERFORMANCE ATTRIBUTION

Performance reports should attribute results to, where possible:

- strategy;
- regime;
- instrument;
- market;
- model;
- execution quality;
- slippage/costs;
- timing.

This makes a “good P&L” diagnostically useful instead of merely flattering.

---

# 149. RESEARCH GOVERNANCE

Research proposals should have:

- hypothesis;
- motivation;
- evidence;
- expected information gain;
- compute budget;
- dataset requirement;
- acceptance criteria.

Do not allow autonomous research to create unbounded compute consumption.

---

# 150. FINAL COMMANDMENT

There is no “Phase 2 someday” hidden in this file.

Implement every feasible requirement in the current engineering cycle.

When you must defer something because it is genuinely impossible in the environment, make the smallest honest deferral and keep the surrounding system complete.

Do not replace difficult work with a mock unless the real external boundary cannot be exercised. In that case, the mock must be clearly identified and tested as a mock.

Do not replace architecture with screenshots.

Do not replace correctness with comments.

Do not replace research with random backtests.

Do not replace live safety with a hidden boolean.

Do not replace a real GUI with a terminal wrapper.

Do not replace recovery with “restart the app”.

Do not replace evidence with an AI score.

Do not replace verification with green CI alone.

**Build AXIOM-MONEY-PRINTER as the actual system described here.**

# END OF MASTER PROMPT
