# AXIOM-MONEY-PRINTER — Product Manual (v3.2.0)

**Display name:** AXIOM · **Codename:** MONEY-PRINTER (brand only — no profit
promise is made or implied anywhere in the product) · **Version:** 3.2.0

This manual is the authoritative description of the product as shipped. Where an
older document in this repository disagrees with this one, this manual and the
test suite win. The authoritative engineering specification is
`AXIOM-MONEY-PRINTER_MASTER_PROMPT_v1.0.md` (150 sections); this manual maps
the shipped product to it.

---

## 1. What the product is

A self-contained market-intelligence and trading workstation:

- market-scoped **universes** of instruments (India, US, Japan, UK, HK, SG,
  Global context) with deterministic session clocks (holidays, weekends,
  pre-open, timezones) — §4, §5, §60, §143
- strict **data provenance**: every value is OBSERVED / CALCULATED / MODEL /
  HISTORICAL with a provenance string; nothing is fabricated (§6, §16, §93)
- a reactive **event bus** with replay-after-gap ("what changed?" deltas are
  first-class state, §8, §71)
- a **Research Intelligence Engine** (analogues, missed-opportunity lab,
  research memory, next-best-experiment proposals) fully separated from
  execution (§9–§11, §20–§22)
- an **Opportunity Center** with lifecycle states and attention profiles that
  never weaken safety (§14, §15)
- an **Evidence Stack** per instrument (Market → Features → Regime → Kronos →
  Analogues → Strategy → Risk → Recommendation → Eligibility). A
  recommendation is never a single AI score (§16)
- **Kronos Studio**: model cards with DOWNLOAD → VERIFY → INSTALL →
  BENCHMARK → ACTIVATE; isolated from execution; honest statuses only —
  missing weights = no claims (§17–§19)
- **paper trading** (LOCAL_PAPER, never routed to any live host) and a
  **first-class LIVE mode** behind an explicit state machine (§31–§37)
- hot/warm/cold latency architecture with P50/P95/P99/P99.9 observability,
  resource governor, task center, backup/restore with integrity verification
  (§23–§26, §68)

## 2. The cockpit (GUI, no terminal required)

Persistent shell with a status strip (Feed · Engine · Research · Kronos ·
Risk · Tasks · freshness), Ctrl+K command palette, and nine areas:

| Page           | Contents                                                                 |
|----------------|--------------------------------------------------------------------------|
| COMMAND        | system status, engine controls, alerts, research progress                 |
| MARKETS        | session clocks, universe studio, instrument workspace, evidence drawer    |
| RESEARCH       | experiments + research memory (recorded findings with provenance)         |
| MODELS         | Kronos Studio model cards and pipeline actions                            |
| TRADING        | paper controls, LIVE state machine, readiness center, start summary, emergency controls, order ledger |
| RISK           | hard limits, emergency-stop state, live guard chain                       |
| INTELLIGENCE   | Opportunity Center, missed-opportunity lab, market events, next-best      |
| REPORTS        | weekly report, latency observability                                      |
| SYSTEM         | runtime, task center, backup/restore, logs                                |

Every control on every page maps 1:1 to a backend endpoint; the mapping is
pinned by `tests/test_axiom_gui_action_matrix.py` (spec §81) and
`tests/test_v310_ui_action_matrix.py`.

Explicit UI states everywhere: LOADING / READY / UPDATING / STALE / PARTIAL /
OFFLINE / ERROR / LOCKED / DISABLED / SUCCESS — and missing data renders as
"—" or UNAVAILABLE, never as zero (§62).

## 3. Live trading — first-class, fail-closed

Live mode is a legitimate AXIOM mode with the state machine

```
OFF → CONFIGURED → READY → ARMED → ACTIVE ⇄ PAUSED / RECONCILING → STOPPED
```

plus BLOCKED reachable from any state. Hard rules:

- **never auto-activated**: install, setup, restart, UI reopen, research,
  model install, config change, token refresh, recovery and background jobs
  can never arm or activate live trading;
- arming requires the exact configured confirmation phrase **and** a passing
  readiness contract **and** a clear emergency-stop flag (fail-closed if the
  flag cannot be read);
- the readiness center reports READY / WARNING / BLOCKED / UNKNOWN — a check
  that could not run is shown as UNKNOWN, never as verified;
- every live entry traverses the guard chain: SIGNAL → PORTFOLIO → RISK →
  POSITION → ORDER VALIDATION → BROKER RULE → FRESHNESS → DUPLICATE →
  FINAL AUTH → BROKER. Exits skip entry-only guards so held positions are
  never trapped;
- configurable rate limits (per-second / per-minute / per-30-minute profiles),
  continuous reconciliation, emergency controls on an independent path
  (§31–§40, §135–§137).

Broker abstraction: `LOCAL_PAPER`, `UPSTOX_SANDBOX`, `UPSTOX_LIVE` — no
cross-routing; Upstox OAuth only through the official flow; token vault never
in logs, URLs, UI, localStorage, SQLite or build artifacts (§38–§43, §67).

## 4. Honesty contract

- no synthetic data is ever presented as observed;
- no dead buttons: if an external boundary is blocked (e.g. Kronos weight
  download in an offline environment), the action reports the true status
  (`DOWNLOAD_NOT_CONFIGURED`, `NOT_VERIFIED`, `NOT_ATTEMPTED`) instead of
  faking success;
- benchmarks are never fabricated — without an inference runner the studio
  says NOT_ATTEMPTED;
- recommendations carry their full evidence lineage and are guidance for human
  review; execution applies its own risk gates (§16, §93, §106).

## 5. Durability, recovery, backups

- SQLite persistence: durable, atomic, migrations additive (schema v9 in this
  release; v1–v9 all recorded) (§70, §104);
- task center tasks survive restart (recovered at startup, idempotently);
- backup = database + extra data files in a tar.gz with a manifest and SHA-256
  integrity refs; verify refuses corrupt or tampered archives; restore refuses
  unverified archives and takes a safety backup first; all paths confined to
  the backup directory (§68, §74);
- closing the UI never stops the core; a single-instance supervisor prevents
  double launches (§27, §28).

## 6. Testing

- ~1535 automated tests (unit + HTTP integration + GUI action matrices),
  all run in CI; skips only for legitimate external blockers;
- failure injection: corrupt backups, NaN data refusal, terminal-state
  revivals, path traversal, duplicate engines, emergency-stop races;
- the frozen executable is verified end-to-end by `packaging/frozen_e2e.py`
  (real HTTP server + real JS parsing via node) — the built artifact itself is
  tested, not just the source (§83–§85, §105).

## 7. Superseded documents

The following pre-3.2 documents describe earlier states of the product and are
kept for history only: `ARENA_GUIDE.md`, `INSTALL_AND_TRADE.md`,
`LIVE_TRADING_ENABLEMENT.md`, `LIVE_OPERATIONS.md`,
`UPSTOX_SANDBOX_AND_MARKETDATA_GUIDE.md`, the various `*_REPORT.md` /
`*_FIX_REPORT.md` incident write-ups. This manual supersedes them.
