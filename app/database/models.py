"""SQLite schema v1. JSON payloads preserve detail without an ORM dependency."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO schema_version VALUES (1);
CREATE TABLE IF NOT EXISTS backtests (
 id TEXT PRIMARY KEY, strategy TEXT NOT NULL, parameters TEXT NOT NULL,
 symbol TEXT NOT NULL, timeframe TEXT NOT NULL, start_date TEXT, end_date TEXT,
 metrics TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
 id TEXT PRIMARY KEY, symbol TEXT, side TEXT, quantity REAL, price REAL,
 status TEXT, strategy TEXT, timestamp TEXT, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fills (
 id TEXT PRIMARY KEY, order_id TEXT, timestamp TEXT, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trades (
 id TEXT PRIMARY KEY, entry REAL, exit REAL, quantity REAL, pnl REAL,
 strategy TEXT, entry_timestamp TEXT, exit_timestamp TEXT, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
 timestamp TEXT PRIMARY KEY, cash REAL, equity REAL, drawdown REAL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS research_runs (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL
);
-- V1 (Arena): learning, sandbox order audit, engine state. Additive only.
INSERT OR IGNORE INTO schema_version VALUES (2);
CREATE TABLE IF NOT EXISTS signals (
 id TEXT PRIMARY KEY, ts TEXT NOT NULL, strategy TEXT NOT NULL, params_version TEXT NOT NULL,
 symbol TEXT NOT NULL, timeframe TEXT NOT NULL, regime TEXT, signal TEXT NOT NULL,
 price REAL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sandbox_orders (
 id TEXT PRIMARY KEY, upstox_order_id TEXT, symbol TEXT NOT NULL, side TEXT NOT NULL,
 quantity REAL NOT NULL, price REAL, order_type TEXT, status TEXT NOT NULL, tag TEXT,
 created_ts TEXT NOT NULL, updated_ts TEXT, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sandbox_transitions (
 order_id TEXT NOT NULL, upstox_order_id TEXT, status TEXT NOT NULL,
 filled_quantity REAL, average_price REAL, ts TEXT NOT NULL, raw TEXT NOT NULL,
 PRIMARY KEY (order_id, upstox_order_id, status, filled_quantity, ts)
);
CREATE TABLE IF NOT EXISTS model_versions (
 version TEXT PRIMARY KEY, strategy TEXT NOT NULL, parameters TEXT NOT NULL,
 promoted INTEGER NOT NULL, validation TEXT NOT NULL, created_ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS system_events (
 id TEXT PRIMARY KEY, ts TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS engine_state (key TEXT PRIMARY KEY, payload TEXT NOT NULL);

-- V2.1 (Arena): market intelligence. Additive migrations only; V1 data preserved.
INSERT OR IGNORE INTO schema_version VALUES (3);
CREATE TABLE IF NOT EXISTS instruments (
  instrument_key TEXT PRIMARY KEY, trading_symbol TEXT, name TEXT, exchange TEXT,
  segment TEXT, instrument_type TEXT, isin TEXT, lot_size REAL, tick_size REAL,
  freeze_quantity REAL, expiry TEXT, strike_price REAL, option_type TEXT,
  underlying_key TEXT, details TEXT NOT NULL, row_hash TEXT NOT NULL,
  state TEXT NOT NULL, source TEXT NOT NULL, first_seen_ts TEXT NOT NULL,
  last_seen_ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_instruments_exchange ON instruments (exchange, segment, state);
CREATE TABLE IF NOT EXISTS universe_syncs (
  id TEXT PRIMARY KEY, ts TEXT NOT NULL, source TEXT NOT NULL, file_hash TEXT,
  total INTEGER, added INTEGER, changed INTEGER, removed INTEGER, unchanged INTEGER,
  status TEXT NOT NULL, detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS download_jobs (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, instrument_key TEXT NOT NULL,
  unit TEXT NOT NULL, interval INTEGER NOT NULL, from_date TEXT NOT NULL,
  to_date TEXT NOT NULL, status TEXT NOT NULL, chunks_total INTEGER DEFAULT 0,
  chunks_done INTEGER DEFAULT 0, records INTEGER DEFAULT 0, detail TEXT NOT NULL,
  created_ts TEXT NOT NULL, updated_ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS coverage (
  instrument_key TEXT NOT NULL, unit TEXT NOT NULL, interval INTEGER NOT NULL,
  first_ts TEXT, last_ts TEXT, records INTEGER DEFAULT 0, gaps TEXT NOT NULL DEFAULT '[]',
  completeness REAL DEFAULT 0.0, updated_ts TEXT NOT NULL,
  PRIMARY KEY (instrument_key, unit, interval)
);
CREATE TABLE IF NOT EXISTS quality_events (
  id TEXT PRIMARY KEY, ts TEXT NOT NULL, instrument_key TEXT, unit TEXT, interval INTEGER,
  classification TEXT NOT NULL, check_name TEXT NOT NULL, detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dataset_versions (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, version INTEGER NOT NULL, instrument_keys TEXT NOT NULL,
  unit TEXT NOT NULL, interval INTEGER NOT NULL, from_date TEXT, to_date TEXT,
  parts TEXT NOT NULL, hashes TEXT NOT NULL, source_refs TEXT NOT NULL,
  created_ts TEXT NOT NULL, UNIQUE (name, version)
);
CREATE TABLE IF NOT EXISTS experiments (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL, config TEXT NOT NULL,
  result TEXT, created_ts TEXT NOT NULL, finished_ts TEXT
);
CREATE TABLE IF NOT EXISTS candidates (
  id TEXT PRIMARY KEY, experiment_id TEXT, strategy TEXT NOT NULL, parameters TEXT NOT NULL,
  dataset_id TEXT, metrics TEXT NOT NULL, oos TEXT NOT NULL, robustness TEXT,
  promoted INTEGER NOT NULL DEFAULT 0, reason TEXT, created_ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shadow_trades (
  id TEXT PRIMARY KEY, ts TEXT NOT NULL, strategy TEXT NOT NULL, parameters TEXT NOT NULL,
  instrument_key TEXT NOT NULL, action TEXT NOT NULL, price REAL, regime TEXT,
  unit TEXT, interval INTEGER, result TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS learning_events (
  id TEXT PRIMARY KEY, ts TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS daily_snapshots (
  date TEXT PRIMARY KEY, equity REAL, realized REAL, unrealized REAL, trades INTEGER,
  regime TEXT, payload TEXT NOT NULL
);

-- FINAL (Arena 3.0.0): lineage, lifecycle, regime memory, opportunity scans,
-- research questions, system status, reality gap, calibration. Additive only.
INSERT OR IGNORE INTO schema_version VALUES (4);
CREATE TABLE IF NOT EXISTS strategy_lineage (
  id TEXT PRIMARY KEY, strategy TEXT NOT NULL, parameters TEXT NOT NULL,
  generation INTEGER NOT NULL, parent_id TEXT, mutation TEXT,
  origin TEXT NOT NULL DEFAULT 'root', created_ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lineage_strategy ON strategy_lineage (strategy, generation);
CREATE TABLE IF NOT EXISTS strategy_lifecycle (
  id TEXT PRIMARY KEY, lineage_id TEXT NOT NULL, stage TEXT NOT NULL,
  status TEXT NOT NULL, evidence TEXT NOT NULL DEFAULT '{}', ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lifecycle_lineage ON strategy_lifecycle (lineage_id, ts);
CREATE TABLE IF NOT EXISTS regime_history (
  ts TEXT NOT NULL, instrument TEXT NOT NULL, regime TEXT NOT NULL,
  features TEXT NOT NULL DEFAULT '{}', PRIMARY KEY (ts, instrument)
);
CREATE TABLE IF NOT EXISTS opportunity_scans (
  id TEXT PRIMARY KEY, ts TEXT NOT NULL, instrument_key TEXT NOT NULL,
  strategy TEXT NOT NULL, action TEXT NOT NULL, score REAL NOT NULL,
  detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_questions (
  id TEXT PRIMARY KEY, question TEXT NOT NULL UNIQUE, rationale TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open', experiment_id TEXT,
  created_ts TEXT NOT NULL, updated_ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS system_status (
  ts TEXT PRIMARY KEY, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reality_gap (
  id TEXT PRIMARY KEY, ts TEXT NOT NULL, strategy TEXT NOT NULL,
  comparison TEXT NOT NULL, metrics TEXT NOT NULL, flagged INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS confidence_calibration (
  id TEXT PRIMARY KEY, ts TEXT NOT NULL, strategy TEXT NOT NULL,
  predicted REAL NOT NULL, realized INTEGER NOT NULL, detail TEXT NOT NULL DEFAULT '{}'
);

-- Arena 3.0.4: Market Event Listener — internet intelligence, observational memory. Additive.
INSERT OR IGNORE INTO schema_version VALUES (6);
CREATE TABLE IF NOT EXISTS market_events (
  event_id TEXT PRIMARY KEY,
  timestamp TEXT NOT NULL,
  headline TEXT NOT NULL,
  summary TEXT NOT NULL,
  event_type TEXT NOT NULL,
  affected_countries TEXT NOT NULL,
  affected_markets TEXT NOT NULL,
  affected_assets TEXT NOT NULL,
  affected_sectors TEXT NOT NULL,
  confidence REAL NOT NULL,
  relevance REAL NOT NULL,
  expected_market_effect TEXT NOT NULL,
  arena_response TEXT NOT NULL,
  action_taken TEXT NOT NULL,
  strategy_used TEXT,
  instruments_affected TEXT NOT NULL,
  result_after_event TEXT,
  sources TEXT NOT NULL,
  raw_payload TEXT NOT NULL,
  created_ts TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  transmission_paths TEXT NOT NULL,
  conflict_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_market_events_type ON market_events (event_type, timestamp);
CREATE INDEX IF NOT EXISTS idx_market_events_fingerprint ON market_events (fingerprint);
CREATE TABLE IF NOT EXISTS market_event_sources (
  event_id TEXT NOT NULL,
  source TEXT NOT NULL,
  source_url TEXT NOT NULL,
  fetched_ts TEXT NOT NULL,
  headline TEXT NOT NULL,
  raw_data TEXT NOT NULL,
  PRIMARY KEY (event_id, source_url),
  FOREIGN KEY (event_id) REFERENCES market_events(event_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS market_event_observations (
  id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL,
  window TEXT NOT NULL,
  observed_ts TEXT NOT NULL,
  price_movement REAL,
  volatility REAL,
  volume REAL,
  affected_instrument TEXT,
  arena_action TEXT,
  result TEXT NOT NULL,
  FOREIGN KEY (event_id) REFERENCES market_events(event_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_market_observations_event ON market_event_observations (event_id, window);

-- Arena V2 LIVE: real-money order audit, deliberately SEPARATE from the
-- sandbox tables so a real fill can never be read back as a simulated one.
-- Additive only; created empty on every existing database.
INSERT OR IGNORE INTO schema_version VALUES (8);
CREATE TABLE IF NOT EXISTS live_orders (
 id TEXT PRIMARY KEY, upstox_order_id TEXT, symbol TEXT NOT NULL, side TEXT NOT NULL,
 quantity REAL NOT NULL, price REAL, order_type TEXT, status TEXT NOT NULL, tag TEXT,
 created_ts TEXT NOT NULL, updated_ts TEXT, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS live_transitions (
 order_id TEXT NOT NULL, upstox_order_id TEXT, status TEXT NOT NULL,
 filled_quantity REAL, average_price REAL, ts TEXT NOT NULL, raw TEXT NOT NULL,
 PRIMARY KEY (order_id, upstox_order_id, status, filled_quantity, ts)
);
CREATE INDEX IF NOT EXISTS idx_live_orders_status ON live_orders (status, created_ts);

-- Arena 3.0.5: performance / trade-quality hardening — bounded caches, numerical, attribution. Additive only.
INSERT OR IGNORE INTO schema_version VALUES (7);
CREATE TABLE IF NOT EXISTS quality_scores (
  id TEXT PRIMARY KEY, ts TEXT NOT NULL, symbol TEXT NOT NULL, strategy TEXT NOT NULL,
  quality_score REAL NOT NULL, confidence REAL NOT NULL, reasons TEXT NOT NULL, penalties TEXT NOT NULL,
  supporting_features TEXT NOT NULL, raw_score REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS liquidity_snapshots (
  id TEXT PRIMARY KEY, ts TEXT NOT NULL, symbol TEXT NOT NULL, spread_bps REAL, liquidity_score REAL,
  execution_quality TEXT NOT NULL, reasons TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS volatility_sizing_logs (
  id TEXT PRIMARY KEY, ts TEXT NOT NULL, symbol TEXT NOT NULL, base_qty REAL NOT NULL, adjusted_qty REAL NOT NULL,
  scalar REAL NOT NULL, reasons TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cross_market_context (
  ts TEXT PRIMARY KEY, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entry_timing_outcomes (
  id TEXT PRIMARY KEY, ts TEXT NOT NULL, strategy TEXT NOT NULL, entry_type TEXT NOT NULL, ret REAL NOT NULL, detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alpha_decay_records (
  id TEXT PRIMARY KEY, ts TEXT NOT NULL, strategy TEXT NOT NULL, age INTEGER NOT NULL, ret REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS mae_mfe_records (
  trade_id TEXT PRIMARY KEY, strategy TEXT NOT NULL, entry_price REAL NOT NULL, exit_price REAL NOT NULL,
  quantity REAL NOT NULL, side TEXT NOT NULL, mae REAL NOT NULL, mfe REAL NOT NULL, time_in_trade INTEGER NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS correlation_snapshots (
  id TEXT PRIMARY KEY, ts TEXT NOT NULL, concentration TEXT NOT NULL, allocations TEXT NOT NULL, matrix TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS degradation_history (
  id TEXT PRIMARY KEY, ts TEXT NOT NULL, strategy TEXT NOT NULL, action TEXT NOT NULL, metrics TEXT NOT NULL, reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scanner_funnel_metrics (
  id TEXT PRIMARY KEY, ts TEXT NOT NULL, full_universe INTEGER NOT NULL, after_liquidity INTEGER NOT NULL,
  after_volatility INTEGER NOT NULL, after_event INTEGER NOT NULL, after_strategy INTEGER NOT NULL,
  finalists INTEGER NOT NULL, latency_ms REAL NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS event_urgency_scores (
  id TEXT PRIMARY KEY, ts TEXT NOT NULL, event_id TEXT NOT NULL, urgency TEXT NOT NULL, score REAL NOT NULL, affected TEXT NOT NULL, reasons TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hot_path_metrics (
  ts TEXT PRIMARY KEY, latencies TEXT NOT NULL, max_ms REAL NOT NULL, avg_ms REAL NOT NULL, p95_ms REAL NOT NULL, cold_depth INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quality_ts ON quality_scores (ts, symbol);
CREATE INDEX IF NOT EXISTS idx_liquidity_ts ON liquidity_snapshots (ts, symbol);

-- AXIOM-MONEY-PRINTER (master spec v1.0) workstation state. Additive only:
-- saved market-scoped universes, what-changed deltas, opportunity lifecycle,
-- research memory, missed opportunities, live session state machine,
-- latency summaries, background tasks, broker connection METADATA (never
-- secrets — tokens stay in the secure credential path).
INSERT OR IGNORE INTO schema_version VALUES (9);
CREATE TABLE IF NOT EXISTS saved_universes (
 id TEXT PRIMARY KEY, market_id TEXT NOT NULL, name TEXT NOT NULL,
 symbols TEXT NOT NULL, note TEXT, created_ts TEXT NOT NULL, updated_ts TEXT NOT NULL,
 UNIQUE (market_id, name)
);
CREATE TABLE IF NOT EXISTS universe_pins (
 market_id TEXT NOT NULL, identity TEXT NOT NULL, pinned_ts TEXT NOT NULL,
 PRIMARY KEY (market_id, identity)
);
CREATE TABLE IF NOT EXISTS state_changes (
 id TEXT PRIMARY KEY, ts TEXT NOT NULL, domain TEXT NOT NULL, object_key TEXT NOT NULL,
 field TEXT NOT NULL, previous TEXT, current TEXT, evidence TEXT
);
CREATE INDEX IF NOT EXISTS idx_state_changes_domain ON state_changes (domain, ts);
CREATE TABLE IF NOT EXISTS opportunities (
 id TEXT PRIMARY KEY, market_id TEXT NOT NULL, identity TEXT NOT NULL,
 state TEXT NOT NULL, created_ts TEXT NOT NULL, last_evidence_ts TEXT NOT NULL,
 updated_ts TEXT NOT NULL, payload TEXT NOT NULL, invalidation_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_opportunities_state ON opportunities (state, updated_ts);
CREATE TABLE IF NOT EXISTS research_memory (
 id TEXT PRIMARY KEY, ts TEXT NOT NULL, market_id TEXT, topic TEXT NOT NULL,
 kind TEXT NOT NULL, payload TEXT NOT NULL, provenance TEXT NOT NULL,
 confidence REAL NOT NULL, experiment_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_research_memory_topic ON research_memory (topic, ts);
CREATE TABLE IF NOT EXISTS missed_opportunities (
 id TEXT PRIMARY KEY, ts TEXT NOT NULL, kind TEXT NOT NULL, market_id TEXT,
 identity TEXT, payload TEXT NOT NULL, knowable INTEGER NOT NULL,
 counterfactual TEXT, research_question TEXT
);
CREATE TABLE IF NOT EXISTS live_sessions (
 id TEXT PRIMARY KEY, state TEXT NOT NULL, ts_entered TEXT NOT NULL,
 reason TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_live_sessions_state ON live_sessions (ts_entered);
CREATE TABLE IF NOT EXISTS latency_summaries (
 stage TEXT NOT NULL, day TEXT NOT NULL, count INTEGER NOT NULL,
 p50_ms REAL NOT NULL, p95_ms REAL NOT NULL, p99_ms REAL NOT NULL,
 p999_ms REAL NOT NULL, max_ms REAL NOT NULL, deadline_misses INTEGER NOT NULL,
 PRIMARY KEY (stage, day)
);
CREATE TABLE IF NOT EXISTS background_tasks (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL, state TEXT NOT NULL,
 progress REAL NOT NULL, started_ts TEXT NOT NULL, updated_ts TEXT NOT NULL,
 payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS broker_connections (
 broker TEXT PRIMARY KEY, status TEXT NOT NULL, token_state TEXT NOT NULL,
 account_hint TEXT, updated_ts TEXT NOT NULL, note TEXT
);

-- FINAL-11/10 §15: atomic live-engine leases. Ownership is decided by ONE
-- conditional SQL statement (INSERT ... ON CONFLICT ... WHERE), never by a
-- read-then-write pair, so two engines starting at the same instant cannot
-- both conclude they own execution.
INSERT OR IGNORE INTO schema_version VALUES (10);
CREATE TABLE IF NOT EXISTS engine_lease (
 name TEXT PRIMARY KEY,
 owner TEXT NOT NULL,
 pid INTEGER NOT NULL,
 host TEXT NOT NULL,
 started_ts TEXT NOT NULL,
 heartbeat_ts TEXT NOT NULL,
 live INTEGER NOT NULL
);

-- V3.2: permanent per-intent submission journal. The legacy engine_state
-- snapshot remains migratable, while rows are never evicted from this table.
INSERT OR IGNORE INTO schema_version VALUES (11);
CREATE TABLE IF NOT EXISTS submission_intents (
 intent_id TEXT PRIMARY KEY,
 payload TEXT NOT NULL
);

-- V3.2.1: idempotent daily-risk fill accounting. A unique fill key and the
-- daily aggregate are updated in one SQLite transaction, so a restart between
-- broker reconciliation and risk accounting cannot lose or double-count P&L.
INSERT OR IGNORE INTO schema_version VALUES (12);
CREATE TABLE IF NOT EXISTS daily_risk_fills (
 fill_id TEXT PRIMARY KEY,
 observed_ts TEXT,
 trading_date TEXT NOT NULL,
 realized_pnl REAL NOT NULL,
 credited INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_daily_risk_fills_date
 ON daily_risk_fills (trading_date);
"""

# ---------------------------------------------------------------------------
# Arena 3.0.3 — per-child (slice) transition identity (schema_version 5).
#
# The 3.0.2 transition primary key (order_id, status, filled_quantity, ts)
# used only the LOGICAL order id, so two child orders (Upstox `slice`)
# reaching the same status/fill/timestamp collided and one child's transition
# was silently dropped. The identity now includes the child order id. The
# migration below is backward compatible: it runs ONCE per database (guarded
# by schema_version 5), rebuilds the table with the new key, and preserves
# every recorded legacy row.
# ---------------------------------------------------------------------------

SCHEMA_VERSION_TRANSITIONS_CHILD = 5

TRANSITIONS_CHILD_MIGRATION = """
CREATE TABLE IF NOT EXISTS sandbox_transitions_child_v5 (
 order_id TEXT NOT NULL, upstox_order_id TEXT, status TEXT NOT NULL,
 filled_quantity REAL, average_price REAL, ts TEXT NOT NULL, raw TEXT NOT NULL,
 PRIMARY KEY (order_id, upstox_order_id, status, filled_quantity, ts)
);
INSERT INTO sandbox_transitions_child_v5
 SELECT order_id, upstox_order_id, status, filled_quantity, average_price, ts, raw
   FROM sandbox_transitions;
DROP TABLE sandbox_transitions;
ALTER TABLE sandbox_transitions_child_v5 RENAME TO sandbox_transitions;
INSERT OR IGNORE INTO schema_version VALUES (5);
"""


def transitions_child_identity_applied(connection) -> bool:
    """True when schema_version already records the per-child identity."""
    row = connection.execute(
        "SELECT 1 FROM schema_version WHERE version=?",
        (SCHEMA_VERSION_TRANSITIONS_CHILD,)).fetchone()
    return row is not None


def migrate_transitions_child_identity(connection) -> bool:
    """Rebuild sandbox_transitions with the child id in the primary key.

    Idempotent and backward compatible: legacy rows are copied verbatim; a
    database created fresh by the current SCHEMA already has the new key and
    the rebuild is a no-op copy. Returns True when the migration ran.
    """
    if transitions_child_identity_applied(connection):
        return False
    connection.executescript(TRANSITIONS_CHILD_MIGRATION)
    return True
