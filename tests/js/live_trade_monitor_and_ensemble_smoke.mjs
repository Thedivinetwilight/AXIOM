/* Headless JSDOM smoke & integration suite for:
 * - Workspace 11: Read-Only "Live Trade Monitor" (#page-monitor)
 * - Workspace 12: "Ensemble & Allocation" (#page-ensemble)
 *
 * Verifies:
 * 1. Persistent sidebar + quick-nav navigation to Monitor (11) and Ensemble (12)
 * 2. Strictly read-only DOM on #page-monitor (zero order submit/modify/cancel/close controls)
 * 3. Pagination across 560 instruments without UI freeze + lazy per-instrument chart loading
 * 4. Honest "NOT TRADING" banner vs PAPER (SIMULATED) vs LIVE (BROKER_CONFIRMED) separation
 * 5. Candlestick + all 8 distinct marker glyph types drawn on canvas + canvas hit-testing click
 *    and timeline row click populating full event details
 * 6. Zero sample-candle substitution when market data is disconnected/unavailable
 * 7. Ensemble preset (1,024 variants), risk/Kronos fail-closed config, watchlist evaluation,
 *    12 risk-check inspection, walk-forward untouched-OOS ablation, and scaling benchmark
 * 8. Zero live/broker order API calls across the entire session.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { JSDOM } from "jsdom";

const root = resolve(import.meta.dirname, "../..");
const html = readFileSync(resolve(root, "app/static/terminal/index.html"), "utf8");
const code = readFileSync(resolve(root, "app/static/terminal/terminal.js"), "utf8");

const TOKEN = "tm-ens-desktop-token";
const calls = [];
const canvasOps = [];

const RISK = {
  max_order_volume: 10, max_symbol_volume: 20, max_order_margin: 10000,
  max_total_margin: 20000, max_open_positions: 5, max_daily_loss: 1000,
  max_drawdown_pct: 20, max_trades_per_day: 10, max_margin_usage_pct: 80,
  max_spread_bps: 100, max_slippage_points: 10, max_quote_age_seconds: 30,
};

let ensPreset = "compact";
let ensEnabledVariants = 24;
let ensKronosPolicy = "REQUIRE_KRONOS";

function makeBars(count = 40) {
  const out = [];
  const t0 = Date.parse("2026-10-30T14:00:00Z");
  let px = 225.0;
  for (let i = 0; i < count; i += 1) {
    const close = Number((px + (i % 2 === 0 ? 0.45 : -0.2)).toFixed(2));
    out.push({
      timestamp: new Date(t0 + i * 300_000).toISOString(),
      open: px,
      high: Math.max(px, close) + 0.3,
      low: Math.min(px, close) - 0.3,
      close,
      volume: 18000 + i * 250,
    });
    px = close;
  }
  return out;
}

const AAPL_BARS = makeBars(40);
const EIGHT_MARKERS = [
  {
    marker_id: "m-sig-1", marker_type: "SIGNAL", shape: "diamond_hollow",
    event_time: AAPL_BARS[5].timestamp, side: "BUY", quantity: 10, price: 225.8,
    currency: "USD", fees: 0.05, strategy: "parallel_ensemble_v1",
    reason: "Calibrated OOS consensus +0.41 (N=180)", order_or_fill_status: "SIGNAL_ONLY (PORTFOLIO ALLOCATOR)",
    execution_mode: "LIVE_MARKET_PAPER", simulated: true, verification_origin: "ADVISORY_SIGNAL (PORTFOLIO ALLOCATOR)",
  },
  {
    marker_id: "m-sub-2", marker_type: "ORDER_SUBMITTED", shape: "square_hollow",
    event_time: AAPL_BARS[8].timestamp, side: "BUY", quantity: 10, price: 226.1,
    currency: "USD", fees: 0.0, strategy: "parallel_ensemble_v1",
    reason: "Simulated LIMIT order working in local paper ledger", order_or_fill_status: "WORKING",
    execution_mode: "LIVE_MARKET_PAPER", simulated: true, verification_origin: "SIMULATED (LOCAL PAPER LEDGER)",
  },
  {
    marker_id: "m-part-3", marker_type: "PARTIAL_FILL", shape: "circle_half",
    event_time: AAPL_BARS[12].timestamp, side: "BUY", quantity: 4, price: 226.4,
    currency: "USD", fees: 0.02, strategy: "parallel_ensemble_v1",
    reason: "Simulated partial fill (4/10 units; bar participation cap)", order_or_fill_status: "PARTIALLY_FILLED",
    execution_mode: "LIVE_MARKET_PAPER", simulated: true, verification_origin: "SIMULATED (LOCAL PAPER LEDGER)",
  },
  {
    marker_id: "m-fill-4", marker_type: "COMPLETED_FILL", shape: "triangle_solid",
    event_time: AAPL_BARS[16].timestamp, side: "BUY", quantity: 6, price: 226.9,
    currency: "USD", fees: 0.03, strategy: "parallel_ensemble_v1",
    reason: "Simulated BUY fill from read-only quote (alpaca_iex)", order_or_fill_status: "FILLED",
    execution_mode: "LIVE_MARKET_PAPER", simulated: true, verification_origin: "SIMULATED (LOCAL PAPER LEDGER)",
  },
  {
    marker_id: "m-can-5", marker_type: "CANCELED", shape: "cross_x",
    event_time: AAPL_BARS[20].timestamp, side: "BUY", quantity: 5, price: 224.5,
    currency: "USD", fees: 0.0, strategy: "manual_only",
    reason: "Stale limit order canceled", order_or_fill_status: "CANCELED",
    execution_mode: "LIVE_MARKET_PAPER", simulated: true, verification_origin: "SIMULATED (LOCAL PAPER LEDGER)",
  },
  {
    marker_id: "m-sl-6", marker_type: "STOP_LOSS", shape: "shield_sl",
    event_time: AAPL_BARS[24].timestamp, side: "SELL", quantity: 10, price: 223.2,
    currency: "USD", fees: 0.0, strategy: "parallel_ensemble_v1",
    reason: "Active protective stop-loss level on simulated paper position", order_or_fill_status: "ACTIVE_PROTECTION",
    execution_mode: "LIVE_MARKET_PAPER", simulated: true, verification_origin: "SIMULATED (LOCAL PAPER LEDGER)",
  },
  {
    marker_id: "m-tp-7", marker_type: "TAKE_PROFIT", shape: "target_tp",
    event_time: AAPL_BARS[28].timestamp, side: "SELL", quantity: 10, price: 232.5,
    currency: "USD", fees: 0.0, strategy: "parallel_ensemble_v1",
    reason: "Active protective take-profit level on simulated paper position", order_or_fill_status: "ACTIVE_PROTECTION",
    execution_mode: "LIVE_MARKET_PAPER", simulated: true, verification_origin: "SIMULATED (LOCAL PAPER LEDGER)",
  },
  {
    marker_id: "m-exit-8", marker_type: "EXIT", shape: "chevron_exit",
    event_time: AAPL_BARS[34].timestamp, side: "SELL", quantity: 10, price: 232.6,
    currency: "USD", fees: 0.08, strategy: "parallel_ensemble_v1",
    reason: "Simulated position exit fill", order_or_fill_status: "FILLED",
    execution_mode: "LIVE_MARKET_PAPER", simulated: true, verification_origin: "SIMULATED (LOCAL PAPER LEDGER)",
  },
];

// Build 560 monitored items to test fast server-paginated navigation
const ALL_MONITOR_ITEMS = [
  {
    row_key: "LIVE_MARKET_PAPER:NASDAQ:AAPL",
    market: "NASDAQ", currency: "USD", symbol: "AAPL",
    execution_mode: "LIVE_MARKET_PAPER", mode_badge: "PAPER · SIMULATED",
    simulated: true, verification_origin: "SIMULATED (LOCAL PAPER LEDGER)",
    session_state: "STOPPED", strategy: "parallel_ensemble", timeframe: "5m",
    actively_trading: false, trading_banner: "NOT TRADING (STOPPED)",
    primary_state: "OPEN_POSITION", states: ["OPEN_POSITION", "RECENT_FILL", "ACTIVE_SIGNAL"],
    position_qty: 10, avg_entry_price: 226.7, unrealized_pnl: 31.0,
    working_orders_count: 1, recent_fills_count: 2, latest_signal: "BUY",
  },
  {
    row_key: "LIVE_MARKET_LIVE:NASDAQ:MSFT",
    market: "NASDAQ", currency: "USD", symbol: "MSFT",
    execution_mode: "LIVE_MARKET_LIVE", mode_badge: "LIVE · BROKER JOURNAL",
    simulated: false, verification_origin: "BROKER_CONFIRMED (ALPACA JOURNAL)",
    session_state: "DISABLED", strategy: "operator_reviewed", timeframe: "5m",
    actively_trading: false, trading_banner: "NOT TRADING",
    primary_state: "RECENT_FILL", states: ["RECENT_FILL"],
    position_qty: 0, avg_entry_price: null, unrealized_pnl: 0,
    working_orders_count: 0, recent_fills_count: 1, latest_signal: "NONE",
  },
  {
    row_key: "LIVE_MARKET_PAPER:NSE:DISCONN",
    market: "NSE", currency: "INR", symbol: "DISCONN",
    execution_mode: "LIVE_MARKET_PAPER", mode_badge: "PAPER · SIMULATED",
    simulated: true, verification_origin: "SIMULATED (LOCAL PAPER LEDGER)",
    session_state: "STOPPED", strategy: "manual_only", timeframe: "5m",
    actively_trading: false, trading_banner: "NOT TRADING (STOPPED)",
    primary_state: "WATCHING", states: ["WATCHING"],
    position_qty: 0, avg_entry_price: null, unrealized_pnl: 0,
    working_orders_count: 0, recent_fills_count: 0, latest_signal: "NONE",
  },
];
for (let i = 0; i < 557; i += 1) {
  ALL_MONITOR_ITEMS.push({
    row_key: `LIVE_MARKET_PAPER:NSE:SYM${String(i).padStart(3, "0")}`,
    market: "NSE", currency: "INR", symbol: `SYM${String(i).padStart(3, "0")}`,
    execution_mode: "LIVE_MARKET_PAPER", mode_badge: "PAPER · SIMULATED",
    simulated: true, verification_origin: "SIMULATED (LOCAL PAPER LEDGER)",
    session_state: "STOPPED", strategy: "parallel_ensemble", timeframe: "5m",
    actively_trading: false, trading_banner: "NOT TRADING (STOPPED)",
    primary_state: "WATCHING", states: ["WATCHING"],
    position_qty: 0, avg_entry_price: null, unrealized_pnl: 0,
    working_orders_count: 0, recent_fills_count: 0, latest_signal: "NONE",
  });
}

function makeEnsembleOverview() {
  return {
    paper_and_research_only: true,
    live_execution_authorized: false,
    preset: ensPreset,
    enabled_families: [
      "sma_crossover", "ema_crossover", "rsi_reversion", "bollinger_reversion",
      "donchian_breakout", "macd_momentum", "vwap_zscore_reversion", "volatility_regime_trend",
    ],
    default_timeframe: "5m",
    concurrency_limit: 4,
    batch_timeout_seconds: 30,
    process_rss_mb: 74.2,
    registry: {
      total_variants: ensEnabledVariants,
      enabled_variants: ensEnabledVariants,
      unique_canonical_variants: ensEnabledVariants,
      duplicate_variants_detected: 0,
    },
    allocators: {
      NSE: {
        kronos_enabled: false, kronos_fail_closed_policy: ensKronosPolicy, kronos_advisory_weight: 0.18,
        min_calibrated_confidence: 0.53, min_oos_samples: 20, per_trade_risk_pct: 0.005,
        max_symbol_exposure_pct: 0.25, max_total_exposure_pct: 0.80, max_drawdown_pct: 0.10,
        max_position_correlation: 0.80, max_spread_bps: 35.0, max_open_positions: 8,
      },
      NASDAQ: {
        kronos_enabled: false, kronos_fail_closed_policy: ensKronosPolicy, kronos_advisory_weight: 0.18,
        min_calibrated_confidence: 0.53, min_oos_samples: 20, per_trade_risk_pct: 0.005,
        max_symbol_exposure_pct: 0.25, max_total_exposure_pct: 0.80, max_drawdown_pct: 0.10,
        max_position_correlation: 0.80, max_spread_bps: 35.0, max_open_positions: 8,
      },
    },
    calibration: {
      NSE: { total_oos_samples: 185, ece: 0.031, brier_score: 0.238 },
      NASDAQ: { total_oos_samples: 210, ece: 0.028, brier_score: 0.231 },
    },
    weights: {
      NSE: { positive_weight_count: 9, weights: { sma_crossover_v0001: 0.24 } },
      NASDAQ: { positive_weight_count: 11, weights: { donchian_breakout_v0002: 0.28 } },
    },
    recent_decisions: [
      {
        market: "NASDAQ", symbol: "AAPL", timeframe: "5m",
        bar_timestamp: AAPL_BARS[39].timestamp,
        final_action: "NO_TRADE", selected_qty: 0,
        aggregation: { pre_kronos_consensus: 0.34, post_kronos_consensus: 0.34 },
        calibration: { calibrated_confidence: 0.59, ci90_low: 0.52, ci90_high: 0.66, oos_sample_size: 210 },
        kronos_contribution: { status: "DISABLED" },
        no_trade_reasons: ["market_session_open: NASDAQ regular session is CLOSED"],
        risk_checks: [
          { check_id: "idempotency_bar_lock", passed: true, actual: "NEW_BAR", limit: "1/bar", detail: "Unique bar timestamp verified" },
          { check_id: "market_session_open", passed: false, actual: "CLOSED", limit: "OPEN", detail: "NASDAQ regular session is CLOSED" },
        ],
        strategy_signals: [
          { strategy_id: "donchian_breakout_v0002", family: "donchian_breakout", action: "BUY", raw_score: 0.62, weight: 0.28, status: "WEIGHTED_ACTIVE", rationale: "20-bar channel high breakout" },
        ],
      },
    ],
    ablation_runs: [],
    benchmarks: [],
  };
}

function json(data, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => "application/json" },
    json: async () => structuredClone(data),
    text: async () => JSON.stringify(data),
  };
}

async function fakeFetch(rawUrl, opts = {}) {
  const url = new URL(rawUrl, "http://127.0.0.1:8000");
  const method = (opts.method || "GET").toUpperCase();
  const body = opts.body ? JSON.parse(opts.body) : null;
  calls.push({ method, path: url.pathname + url.search, pathname: url.pathname, body });

  // Strict safety assertion: neither Monitor nor Ensemble may ever call live broker order endpoints
  assert(!url.pathname.includes("/orders/submit"), `Forbidden live order call: ${url.pathname}`);
  assert(!url.pathname.startsWith("/api/alpaca-broker/orders"), `Forbidden Alpaca broker order call: ${url.pathname}`);

  if (url.pathname === "/api/workspace") {
    return json({
      status: {
        mode: "AXIOM_PAPER", lifecycle: "DISARMED", connected: false, authenticated: false,
        emergency_stopped: false, account_login_masked: "Local paper", server: "No MT5 server",
        currency: "USD", balance: 10000, equity: 10000, unrealized_pnl: 0, free_margin: 10000,
        margin_used: 0, open_positions: 0, open_orders: 0, automation_enabled: false, live_lock: "LOCKED_BY_DEFAULT",
      },
      account: null, positions: [], orders: [], fills: [], realized_pnl_total: 0,
      risk_limits: RISK, paper_session: { state: "STOPPED", strategy: "ema_crossover" },
      market_data: { state: "DISCONNECTED", connected: false, authenticated: false },
    });
  }
  if (url.pathname === "/api/markets/overview") {
    return json({
      paper: {
        NSE: { market: "NSE", currency: "INR", session_state: "STOPPED", strategy: "parallel_ensemble", timeframe: "5m", exchange_session: { state: "CLOSED", is_open: false }, account: { equity: 1000000, cash: 1000000, market_value: 0, day_pnl: 0, realized_pnl: 0, total_fees_paid: 0 }, positions: [], orders: [], fills: [], decisions: [] },
        NASDAQ: { market: "NASDAQ", currency: "USD", session_state: "STOPPED", strategy: "parallel_ensemble", timeframe: "5m", exchange_session: { state: "CLOSED", is_open: false }, account: { equity: 100000, cash: 100000, market_value: 0, day_pnl: 0, realized_pnl: 0, total_fees_paid: 0 }, positions: [], orders: [], fills: [], decisions: [] },
      },
      watchlists: { NSE: [], NASDAQ: [] },
      catalogs: { NSE: { instrument_count: 50, source_kind: "bundled_seed_reference" }, NASDAQ: { instrument_count: 50, source_kind: "bundled_seed_reference" } },
      live: { default_live_execution: "OFF", nse: { availability: "NOT_AVAILABLE" }, nasdaq: { availability: "NOT_AVAILABLE" } },
    });
  }
  if (url.pathname === "/api/markets/readiness") {
    return json({
      modes: {
        NSE_PAPER: { label: "NSE Paper", overall_status: "NOT_VERIFIED", checks: [] },
        NASDAQ_PAPER: { label: "NASDAQ Paper", overall_status: "NOT_VERIFIED", checks: [] },
        NSE_LIVE: { label: "NSE Live", overall_status: "DISABLED", checks: [] },
        NASDAQ_LIVE: { label: "NASDAQ Live", overall_status: "DISABLED", checks: [] },
      },
    });
  }
  if (url.pathname === "/api/model-lab/overview") {
    return json({
      paper_and_research_only: true,
      live_execution_authorized: false,
      kronos: { provider_state: "NOT_INSTALLED", inference_enabled: false, model_loaded_in_memory: false, config: { default_lookback: 256, default_pred_len: 12, default_sample_count: 8, temperature: 1.0, top_p: 0.9, timeout_seconds: 60 }, issues: [], model_identity: {} },
      kronos_setup: { state: "IDLE", stage: "idle", percent: 0 },
      datasets: [{ dataset_id: "bundled_nse_synthetic_1h", market: "NSE", bar_count: 1600, symbol: "DEMO_NSE", timeframe: "1h", real_market_data: false, estimated_missing_bars: 0, start_timestamp: "2025-01-01", end_timestamp: "2025-03-01", sha256: "a".repeat(64) }],
      runs: [],
      models: [],
    });
  }

  // --- 11 LIVE TRADE MONITOR ENDPOINTS (STRICTLY GET) ---
  if (url.pathname === "/api/markets/trade-monitor") {
    assert.equal(method, "GET", "Trade monitor summary MUST be strictly GET");
    const page = Number(url.searchParams.get("page") || "1");
    const pageSize = Number(url.searchParams.get("page_size") || "50");
    const mkt = url.searchParams.get("market") || "ALL";
    const mode = url.searchParams.get("execution_mode") || "ALL";
    let filtered = ALL_MONITOR_ITEMS;
    if (mkt !== "ALL") filtered = filtered.filter((x) => x.market === mkt);
    if (mode !== "ALL") filtered = filtered.filter((x) => x.execution_mode === mode);
    const totalPages = Math.max(1, Math.ceil(filtered.length / pageSize));
    const slice = filtered.slice((page - 1) * pageSize, page * pageSize);
    return json({
      read_only: true,
      broker_order_api_calls: 0,
      banners: {
        NSE_PAPER: { actively_trading: false, banner_status: "NOT TRADING (STOPPED)" },
        NASDAQ_PAPER: { actively_trading: false, banner_status: "NOT TRADING (STOPPED)" },
      },
      pagination: {
        page, page_size: pageSize, total_items: filtered.length, total_pages: totalPages,
        has_prev: page > 1, has_next: page < totalPages,
      },
      counts: {
        total_unfiltered: ALL_MONITOR_ITEMS.length, open_positions: 1, working_orders: 1, recent_fills: 3, active_signals: 1,
      },
      items: slice,
    });
  }

  if (url.pathname === "/api/markets/trade-monitor/chart") {
    assert.equal(method, "GET", "Trade monitor chart MUST be strictly GET");
    const sym = url.searchParams.get("symbol");
    const mkt = url.searchParams.get("market");
    const mode = url.searchParams.get("execution_mode");
    if (sym === "DISCONN") {
      return json({
        read_only: true,
        market: "NSE", symbol: "DISCONN", currency: "INR", timeframe: "5m",
        connection_state: "DISCONNECTED", data_source: "upstox_not_configured",
        bars: [], quote: null,
        execution_mode: "LIVE_MARKET_PAPER", simulated_execution: true,
        session_banner: { actively_trading: false, banner_status: "NOT TRADING (STOPPED)", banner_tone: "inactive", reason: "Upstox token not configured; 0 sample bars substituted." },
        reference_lines: [], markers: [],
      });
    }
    if (sym === "MSFT" && mode === "LIVE_MARKET_LIVE") {
      return json({
        read_only: true,
        market: "NASDAQ", symbol: "MSFT", currency: "USD", timeframe: "5m",
        connection_state: "CONNECTED", data_source: "alpaca_iex_readonly",
        bars: AAPL_BARS, quote: { timestamp: AAPL_BARS[39].timestamp, last: 418.2 },
        execution_mode: "LIVE_MARKET_LIVE", simulated_execution: false,
        session_banner: { actively_trading: false, banner_status: "NOT TRADING", banner_tone: "inactive", reason: "Live execution gate is DISABLED (default OFF)." },
        reference_lines: [],
        markers: [
          {
            marker_id: "live-msft-1", marker_type: "COMPLETED_FILL", shape: "triangle_solid",
            event_time: AAPL_BARS[18].timestamp, side: "BUY", quantity: 2, price: 416.5,
            currency: "USD", fees: 0.0, strategy: "operator_reviewed_live",
            reason: "Historical reconciled broker order", order_or_fill_status: "FILLED",
            execution_mode: "LIVE_MARKET_LIVE", simulated: false, verification_origin: "BROKER_CONFIRMED (ALPACA JOURNAL)",
          },
        ],
      });
    }
    return json({
      read_only: true,
      market: mkt, symbol: sym, currency: "USD", timeframe: "5m",
      connection_state: "CONNECTED", data_source: "alpaca_iex_readonly",
      bars: AAPL_BARS, quote: { timestamp: AAPL_BARS[39].timestamp, last: 229.8 },
      execution_mode: "LIVE_MARKET_PAPER", simulated_execution: true,
      session_banner: { actively_trading: false, banner_status: "NOT TRADING (STOPPED)", banner_tone: "inactive", reason: "Paper session is STOPPED — historical simulated ledger shown." },
      reference_lines: [
        { kind: "AVG_ENTRY", price: 226.7, label: "PAPER AVG ENTRY 226.70" },
        { kind: "STOP_LOSS", price: 223.2, label: "STOP LOSS 223.20" },
        { kind: "TAKE_PROFIT", price: 232.5, label: "TAKE PROFIT 232.50" },
      ],
      markers: EIGHT_MARKERS,
    });
  }

  // --- 12 ENSEMBLE & PORTFOLIO ALLOCATION ENDPOINTS ---
  if (url.pathname === "/api/ensemble/overview") {
    return json(makeEnsembleOverview());
  }
  if (url.pathname === "/api/ensemble/config" && method === "POST") {
    if (body.preset) {
      ensPreset = body.preset;
      ensEnabledVariants = body.preset === "massive_1024" ? 1024 : 24;
    }
    if (body.allocator_updates?.kronos_fail_closed_policy) {
      ensKronosPolicy = body.allocator_updates.kronos_fail_closed_policy;
    }
    return json(makeEnsembleOverview());
  }
  if (url.pathname === "/api/ensemble/evaluate" && method === "POST") {
    return json({
      market: body.market,
      timeframe: "5m",
      workers_used: 4,
      total_strategy_evaluations: ensEnabledVariants * 2,
      wall_ms: 14.6,
      orders_submitted_simulated: 0,
      broker_order_api_calls: 0,
      decisions: makeEnsembleOverview().recent_decisions,
      rejected_instruments: {},
    });
  }
  if (url.pathname === "/api/ensemble/ablation" && method === "POST") {
    return json({
      ablation_id: "abl-test-001",
      market: body.market,
      dataset: { dataset_id: body.dataset_id, real_market_data: false },
      multiple_testing: { variants_tested: ensEnabledVariants, sharpe_haircut_threshold: 0.68, accepted_count: 7 },
      calibration: { total_oos_samples: 192, brier_score: 0.236, ece: 0.029 },
      kronos_ablation_summary: { verdict: "Evaluated on synthetic/fixture data only — NEVER claimed as real-market Kronos benefit." },
      comparisons: [
        { candidate_id: "no_trade", label: "No-Trade (0% Exposure)", status: "EVALUATED", untouched_test_1x: { net_return_pct: 0.0, sharpe: 0.0, sortino: 0.0, max_drawdown_pct: 0.0, trade_count: 0, no_trade_bars: 310, win_rate_pct: 0.0, win_rate_ci90_pct: [0, 100] }, stressed_test_2x: { net_return_pct: 0.0, sharpe: 0.0 } },
        { candidate_id: "buy_and_hold", label: "Buy & Hold", status: "EVALUATED", untouched_test_1x: { net_return_pct: -2.14, sharpe: -0.42, sortino: -0.55, max_drawdown_pct: 7.8, trade_count: 1, no_trade_bars: 0, win_rate_pct: 0.0, win_rate_ci90_pct: [0, 70] }, stressed_test_2x: { net_return_pct: -2.16, sharpe: -0.43 } },
        { candidate_id: "ensemble_no_kronos", label: "Parallel Ensemble (Kronos OFF)", status: "EVALUATED", untouched_test_1x: { net_return_pct: 3.12, sharpe: 1.14, sortino: 1.52, max_drawdown_pct: 2.1, trade_count: 9, no_trade_bars: 245, win_rate_pct: 66.7, win_rate_ci90_pct: [41, 86] }, stressed_test_2x: { net_return_pct: 2.74, sharpe: 0.98 } },
        { candidate_id: "ensemble_with_kronos", label: "Parallel Ensemble (Kronos ON · Advisory)", status: "SKIPPED_KRONOS_NOT_READY", reason: "Kronos runtime/weights not installed locally; never fabricated." },
      ],
    });
  }
  if (url.pathname === "/api/ensemble/benchmark" && method === "POST") {
    return json({
      benchmark_id: "bench-test-001",
      results: [
        { variants: 24, instruments: 1, total_evaluations: 24, workers_used: 1, wall_ms: 0.85, per_instrument_p50_ms: 0.85, per_instrument_p95_ms: 0.85, throughput_evals_per_sec: 28235, peak_tracemalloc_mb: 0.12, process_rss_mb: 74.2, missed_instruments: 0, duplicate_bars_processed: 0 },
        { variants: 1024, instruments: 8, total_evaluations: 8192, workers_used: 4, wall_ms: 48.2, per_instrument_p50_ms: 19.4, per_instrument_p95_ms: 24.1, throughput_evals_per_sec: 169958, peak_tracemalloc_mb: 1.45, process_rss_mb: 76.0, missed_instruments: 0, duplicate_bars_processed: 0 },
      ],
    });
  }

  return json({});
}

const flush = () => new Promise((r) => setTimeout(r, 30));

const dom = new JSDOM(html, { url: "http://127.0.0.1:8000/terminal", runScripts: "outside-only", pretendToBeVisual: true });
const { window } = dom;
window.fetch = fakeFetch;
window.HTMLCanvasElement.prototype.getContext = () => ({
  clearRect: (...a) => canvasOps.push(["clearRect", ...a]),
  fillRect: (...a) => canvasOps.push(["fillRect", ...a]),
  strokeRect: (...a) => canvasOps.push(["strokeRect", ...a]),
  beginPath: () => canvasOps.push(["beginPath"]),
  moveTo: (...a) => canvasOps.push(["moveTo", ...a]),
  lineTo: (...a) => canvasOps.push(["lineTo", ...a]),
  stroke: () => canvasOps.push(["stroke"]),
  fill: () => canvasOps.push(["fill"]),
  fillText: (...a) => canvasOps.push(["fillText", ...a]),
  measureText: () => ({ width: 42 }),
  setLineDash: () => {},
  save: () => {},
  restore: () => {},
  rect: () => {},
  clip: () => {},
});
window.ResizeObserver = class { observe() {} disconnect() {} };
window.eval(code);

try {
  // Unlock terminal
  window.document.getElementById("auth-token").value = TOKEN;
  window.document.getElementById("auth-form").dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
  await flush();

  // 1. Verify persistent sidebar + quick-nav links for 11 Live Trade Monitor & 12 Ensemble
  const navMonitor = window.document.querySelector('.nav-item[data-page="monitor"]');
  const navEnsemble = window.document.querySelector('.nav-item[data-page="ensemble"]');
  const quickMonitor = window.document.querySelector('#mm-quick-nav-bar [data-go="monitor"]');
  const quickEnsemble = window.document.querySelector('#mm-quick-nav-bar [data-go="ensemble"]');
  assert(navMonitor && navEnsemble, "Sidebar must include 11 Live Trade Monitor and 12 Ensemble & Allocation");
  assert(quickMonitor && quickEnsemble, "Quick-nav bar must include Live Trade Monitor and Ensemble buttons");

  // 2. Navigate to 11 Live Trade Monitor
  navMonitor.click();
  await flush();
  await flush();

  const pageMonitor = window.document.getElementById("page-monitor");
  assert(!pageMonitor.hidden, "#page-monitor must be visible");

  // Audit #page-monitor DOM: zero order creation/edit/cancel/close forms or buttons!
  const monitorButtons = Array.from(pageMonitor.querySelectorAll("button")).map((b) => b.textContent.toLowerCase());
  for (const label of monitorButtons) {
    assert(!label.includes("place order") && !label.includes("submit order") && !label.includes("cancel order") && !label.includes("close position"),
      `Forbidden order mutation control inside read-only Live Trade Monitor: ${label}`);
  }

  // Verify 560-instrument pagination & lazy chart load (only 1 chart call made for first selected symbol, NOT 560!)
  assert(window.document.getElementById("tm-count-total").textContent.includes("560 MONITORED"));
  assert(window.document.getElementById("tm-page-info").textContent.includes("Page 1 / 12"));
  const chartCallsInitial = calls.filter((c) => c.pathname === "/api/markets/trade-monitor/chart");
  assert.equal(chartCallsInitial.length, 1, "Must lazily load chart ONLY for selected instrument, not all 560");

  // Verify NOT TRADING banner and PAPER SIMULATED badge
  assert(window.document.getElementById("tm-banner-status").textContent.includes("NOT TRADING"));
  assert(window.document.getElementById("tm-selected-mode-badge").textContent.includes("PAPER"));
  assert.equal(window.document.getElementById("tm-events-count").textContent, "8 EVENTS");

  // 3. Test clicking directly on a marker's (x, y) coordinate on #tm-chart-canvas!
  const eventsRows = window.document.querySelectorAll("#tm-events-rows tr[data-tm-marker-row]");
  assert.equal(eventsRows.length, 8, "All 8 distinct marker types must be rendered in timeline");

  // Click timeline row for PARTIAL_FILL first
  eventsRows[2].click();
  await flush();
  let detailHtml = window.document.getElementById("tm-marker-detail-body").textContent;
  assert(detailHtml.includes("PARTIAL_FILL") && detailHtml.includes("circle_half") && detailHtml.includes("SIMULATED (LOCAL PAPER LEDGER)"));

  // Also test direct canvas click hit-testing across the canvas width to hit SIGNAL diamond_hollow
  const canvas = window.document.getElementById("tm-chart-canvas");
  // Bar 5 of 40 on 900x360 canvas: padL=16, plotW=808 => x = 16 + 5.5 * (808/40) = ~127
  let hitSignalOnCanvas = false;
  for (let yProbe = 24; yProbe <= 330; yProbe += 8) {
    canvas.dispatchEvent(new window.MouseEvent("click", { bubbles: true, clientX: 127, clientY: yProbe }));
    if (window.document.getElementById("tm-marker-detail-body").textContent.includes("SIGNAL")) {
      hitSignalOnCanvas = true;
      break;
    }
  }
  assert(hitSignalOnCanvas, "Direct click on #tm-chart-canvas marker glyph must open SIGNAL marker inspection details");

  // 4. Select LIVE broker-confirmed MSFT row and verify unmistakable BROKER_CONFIRMED vs SIMULATED distinction
  const msftBtn = window.document.querySelector('[data-tm-select-sym="MSFT"]');
  assert(msftBtn, "MSFT LIVE row button must be rendered");
  msftBtn.click();
  await flush();
  assert(window.document.getElementById("tm-selected-mode-badge").textContent.includes("LIVE"));
  window.document.querySelector("#tm-events-rows tr[data-tm-marker-row]").click();
  await flush();
  detailHtml = window.document.getElementById("tm-marker-detail-body").textContent;
  assert(detailHtml.includes("BROKER_CONFIRMED (ALPACA JOURNAL)"));

  // 5. Select disconnected NSE:DISCONN row and verify ZERO sample candles are substituted
  const disconnBtn = window.document.querySelector('[data-tm-select-sym="DISCONN"]');
  disconnBtn.click();
  await flush();
  const emptyOverlay = window.document.getElementById("tm-chart-empty");
  assert(!emptyOverlay.hidden && emptyOverlay.textContent.includes("Zero sample/mock candles substituted"));

  // Test pagination next page button (Page 2 / 12)
  window.document.getElementById("tm-next-page").click();
  await flush();
  assert(window.document.getElementById("tm-page-info").textContent.includes("Page 2 / 12"));

  // 6. Navigate to 12 Ensemble & Allocation
  navEnsemble.click();
  await flush();
  await flush();
  const pageEnsemble = window.document.getElementById("page-ensemble");
  assert(!pageEnsemble.hidden, "#page-ensemble must be visible");

  // Switch preset to massive_1024 and submit config form
  window.document.getElementById("ens-preset").value = "massive_1024";
  window.document.getElementById("ens-config-form").dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
  await flush();
  assert(window.document.getElementById("ens-variants-pill").textContent.includes("1024 VARIANTS"));

  // Update per-market Kronos fail-closed policy & risk allocator limits
  window.document.getElementById("ens-kronos-policy").value = "DEGRADE_TO_NON_KRONOS";
  window.document.getElementById("ens-risk-form").dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
  await flush();
  assert.equal(ensKronosPolicy, "DEGRADE_TO_NON_KRONOS");

  // Run watchlist evaluation (advisory/dry-run) and inspect risk checks
  window.document.getElementById("ens-eval-form").dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
  await flush();
  assert(window.document.getElementById("ens-eval-summary").textContent.includes("0 broker order calls"));
  assert(window.document.getElementById("ens-risk-checks-rows").textContent.includes("idempotency_bar_lock"));
  assert(window.document.getElementById("ens-strategy-signals-rows").textContent.includes("donchian_breakout_v0002"));

  // Run Walk-Forward + Untouched OOS Ablation
  window.document.getElementById("ens-ablation-form").dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
  await flush();
  assert(window.document.getElementById("ens-ablation-rows").textContent.includes("Parallel Ensemble (Kronos OFF)"));
  assert(window.document.getElementById("ens-ablation-rows").textContent.includes("SKIPPED_KRONOS_NOT_READY"));

  // Run Parallel Scaling Benchmark
  window.document.getElementById("ens-bench-form").dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
  await flush();
  assert(window.document.getElementById("ens-bench-rows").textContent.includes("1024 × 8"));

  console.log(`# live_trade_monitor_and_ensemble_smoke: OK (${calls.length} local API calls, ${canvasOps.length} canvas ops, 0 broker order calls)`);
} finally {
  dom.window.close();
}
