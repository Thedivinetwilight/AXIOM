/* Headless JSDOM regression suite for Concurrent Multi-Market Selection,
 * Immediate Start/Resume Symbol Coverage, Closed-Market Analysis Display,
 * Parallel Ensemble Default Strategy, and Live Chart Timeframe/Error Honesty.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { JSDOM } from "jsdom";
import { consoleReadRoutes, consoleWriteRoutes } from "./console_routes.mjs";

const root = resolve(import.meta.dirname, "../..");
const html = readFileSync(resolve(root, "app/static/terminal/index.html"), "utf8");
const code = readFileSync(resolve(root, "app/static/terminal/terminal.js"), "utf8");

const TOKEN = "mm-concurrent-smoke-token";
const now = () => new Date().toISOString();

const RISK = {
  max_order_volume: 10, max_symbol_volume: 20, max_order_margin: 10000,
  max_total_margin: 20000, max_open_positions: 5, max_daily_loss: 1000,
  max_drawdown_pct: 20, max_trades_per_day: 10, max_margin_usage_pct: 80,
  max_spread_bps: 100, max_slippage_points: 10, max_quote_age_seconds: 30,
};

let selectedMarkets = ["NSE", "NASDAQ"];
let nseState = "STOPPED";
let nasdaqState = "STOPPED";
let nseAnalysis = null;
let nasdaqAnalysis = null;
let chartProviderError = "";
const apiCalls = [];

function buildAnalysis(mkt, trigger, isOpen) {
  if (mkt === "NSE") {
    return {
      market: "NSE",
      currency: "INR",
      trigger,
      analyzed_at: now(),
      exchange_session: { state: isOpen ? "REGULAR" : "CLOSED", is_open: isOpen, timezone: "Asia/Kolkata" },
      strategy: "parallel_ensemble",
      timeframe: "5m",
      enabled_strategy_variants: 12,
      trade_execution_permitted_now: isOpen,
      counts: { selected_symbols: 2, succeeded: 2, missing_data: 0, stale_data: 0, failed: 0 },
      symbols: [
        { symbol: "RELIANCE", status: "SUCCEEDED", code: "OK_CLOSED_SESSION_ANALYZED", reason: "Analyzed 165 bars (closed)", ensemble_final_action: "NO_TRADE", active_weighted_variants: 12 },
        { symbol: "TCS", status: "SUCCEEDED", code: "OK_CLOSED_SESSION_ANALYZED", reason: "Analyzed 165 bars (closed)", ensemble_final_action: "NO_TRADE", active_weighted_variants: 12 },
      ],
    };
  }
  return {
    market: "NASDAQ",
    currency: "USD",
    trigger,
    analyzed_at: now(),
    exchange_session: { state: "REGULAR", is_open: true, timezone: "America/New_York" },
    strategy: "parallel_ensemble",
    timeframe: "5m",
    enabled_strategy_variants: 12,
    trade_execution_permitted_now: true,
    counts: { selected_symbols: 3, succeeded: 1, missing_data: 1, stale_data: 0, failed: 1 },
    symbols: [
      { symbol: "AAPL", status: "SUCCEEDED", code: "OK_OPEN_SESSION", reason: "Analyzed 165 bars", ensemble_final_action: "BUY", active_weighted_variants: 12 },
      { symbol: "MSFT", status: "MISSING_DATA", code: "EMPTY_BARS", reason: "Provider returned 0 completed 5m bars" },
      { symbol: "NVDA", status: "FAILED", code: "UPSTREAM_503", reason: "Provider HTTP 503" },
    ],
  };
}

function makeOverview() {
  return {
    generated_at: now(),
    selected_markets: [...selectedMarkets],
    runner: {
      enabled: true,
      background_loop_running: true,
      interval_seconds: 10.0,
      market_timeout_seconds: 12.0,
      selected_markets: [...selectedMarkets],
      live_trading_enabled: false,
      broker_order_api_calls: 0,
      markets: {},
    },
    paper: {
      NSE: {
        market: "NSE",
        currency: "INR",
        session_state: nseState,
        strategy: nseState === "STOPPED" ? "manual_only" : "parallel_ensemble",
        timeframe: "5m",
        effective_symbols: ["RELIANCE", "TCS"],
        watched_symbols: ["RELIANCE", "TCS"],
        last_analysis: nseAnalysis,
        exchange_session: { state: "CLOSED", is_open: false, timezone: "Asia/Kolkata" },
        account: { equity: 1000000, cash: 1000000, market_value: 0, day_pnl: 0, realized_pnl: 0, total_fees_paid: 0 },
        positions: [], orders: [], fills: [], decisions: [],
      },
      NASDAQ: {
        market: "NASDAQ",
        currency: "USD",
        session_state: nasdaqState,
        strategy: nasdaqState === "STOPPED" ? "manual_only" : "parallel_ensemble",
        timeframe: "5m",
        effective_symbols: ["AAPL", "MSFT", "NVDA"],
        watched_symbols: ["AAPL", "MSFT", "NVDA"],
        last_analysis: nasdaqAnalysis,
        exchange_session: { state: "REGULAR", is_open: true, timezone: "America/New_York" },
        account: { equity: 100000, cash: 100000, market_value: 0, day_pnl: 0, realized_pnl: 0, total_fees_paid: 0 },
        positions: [], orders: [], fills: [], decisions: [],
      },
    },
    live: {
      default_live_execution: "OFF",
      nse: { availability: "NOT_AVAILABLE", live_master_flag: false, live_credential_present: false, algo_name_configured: false, missing_dependencies: [] },
      nasdaq: { availability: "NOT_AVAILABLE", live_master_flag: false, live_credential_present: false, missing_dependencies: [], broker_status: { gate: { state: "DISABLED" } } },
    },
    providers: {
      providers: [
        { id: "upstox_market_data", name: "Upstox Market Data (NSE)", role: "MARKET_DATA_READ_ONLY", connection_state: "CONNECTED_READ_ONLY", credential_present: true, credential_source: "dpapi", used_for: ["NSE quotes"] },
        { id: "alpaca_market_data", name: "Alpaca Market Data (NASDAQ)", role: "MARKET_DATA_READ_ONLY", connection_state: "CONNECTED_READ_ONLY", credential_source: "dpapi", credential_present: true, feed: "iex", used_for: ["NASDAQ quotes"] },
        { id: "alpaca_broker_paper", name: "Alpaca Broker Paper", role: "BROKER_PAPER_SANDBOX", connection_state: "DISCONNECTED", credential_present: false, used_for: [] },
        { id: "upstox_live", name: "Upstox LIVE (NSE)", role: "LIVE_BROKER_GATED", connection_state: "LOCKED", credential_present: false, used_for: [] },
        { id: "alpaca_broker_live", name: "Alpaca LIVE (US)", role: "LIVE_BROKER_GATED", connection_state: "LOCKED", credential_present: false, used_for: [] },
      ],
    },
    watchlists: {
      NSE: [{ symbol: "RELIANCE", instrument_key: "NSE_EQ|INE002A01018" }, { symbol: "TCS", instrument_key: "NSE_EQ|INE467B01029" }],
      NASDAQ: [{ symbol: "AAPL", instrument_key: "NASDAQ|AAPL" }, { symbol: "MSFT", instrument_key: "NASDAQ|MSFT" }, { symbol: "NVDA", instrument_key: "NASDAQ|NVDA" }],
    },
    reconciliation: { NSE: { tradable_count: 2 }, NASDAQ: { counts: { total: 3 } } },
    recent_audit: [],
  };
}

function json(payload, status = 200) {
  return new Response(JSON.stringify(payload), { status, headers: { "Content-Type": "application/json" } });
}

async function fetchFake(input, init = {}) {
  const rawUrl = typeof input === "string" ? input : input.url;
  const url = new URL(rawUrl, "http://127.0.0.1:8000");
  assert.ok(!url.hostname.includes("alpaca.markets") && !url.hostname.includes("upstox.com"), "Browser must never call external broker URLs");
  const method = (init.method || "GET").toUpperCase();
  const body = init.body ? JSON.parse(init.body) : null;
  apiCalls.push({ method, path: url.pathname, search: url.search, body });

  const legacyWs = {
    generated_at: now(),
    status: { mode: "AXIOM_PAPER", connected: false, armed: false, emergency_stop: false, live_trading_enabled: false, data_feed: "Dataset", risk_limits: RISK },
    account: { currency: "USD", balance: 100000, equity: 100000, free_margin: 100000, margin_used: 0, unrealized_pnl: 0, realized_pnl: 0 },
    market_data: { connected: false, state: "DISCONNECTED" },
    paper_session: { state: "STOPPED", strategy: "moving_average", symbols: ["EURUSD"], timeframe: "1m" },
    positions: [], open_orders: [], order_history: [], deals: [], quotes: [], audit: [], risk_rejections: [], reconciliation: [], strategies: [],
  };
  const projected = consoleReadRoutes(url.pathname, legacyWs, { events: [{ seq: 1, market: "NSE", event_kind: "FAKE_AUDIT", payload: { safe: true }, created_at: now(), entry_hash: "fake-hash" }] });
  if (projected !== undefined) return json(projected);
  const emergency = consoleWriteRoutes(url.pathname, method, body, {});
  if (emergency !== undefined) return json(emergency);
  if (url.pathname === "/api/trading/workspace" || url.pathname === "/api/terminal/workspace") return json(legacyWs);
  if (url.pathname === "/api/trading/status") return json(legacyWs.status);
  if (url.pathname === "/api/trading/activity") return json({ events: [] });
  if (url.pathname === "/api/trading/connection") {
    return json({ status: legacyWs.status, profiles: { VANTAGE_DEMO: null, VANTAGE_LIVE: null }, secret_store: "DPAPI", test_is_read_only: true, mt5_package_available: false, mt5_integration: "UNAVAILABLE" });
  }
  if (url.pathname === "/api/market-data/status" || url.pathname === "/api/trading/market-data") return json(legacyWs.market_data);
  if (url.pathname === "/api/trading/performance") return json({ targets_ms: {}, operations: {} });
  if (url.pathname === "/api/trading/listed-assets") return json({ classes: [], total_curated_examples: 0 });
  if (url.pathname === "/api/trading/chart/symbols") return json({ symbols: [{ symbol: "EURUSD", source: "Dataset" }] });
  if (url.pathname === "/api/trading/chart") return json({ symbol: "EURUSD", timeframe: "1m", source: "Dataset", simulated: true, bars: [] });
  if (url.pathname === "/api/alpaca-broker/status") return json({ enabled: false, environment: "paper", real_money: false, gate: { state: "DISABLED" }, limits: {} });
  if (url.pathname === "/api/markets/overview") return json(makeOverview());
  if (url.pathname === "/api/markets/search") return json({ items: [] });
  if (url.pathname === "/api/markets/paper/decisions") return json({ total: 0, decisions: [], latest_by_symbol: [] });
  if (url.pathname === "/api/markets/paper/logs") return json({ ok: true, total_matching: 0, returned: 0, events: [], observed_sessions: [], observed_strategies: [], observed_symbols: [] });

  if (url.pathname === "/api/markets/paper/selection" && method === "POST") {
    selectedMarkets = [...body.markets];
    return json({ ok: true, selected_markets: selectedMarkets });
  }
  if (url.pathname === "/api/markets/paper/sessions/start" && method === "POST") {
    selectedMarkets = [...body.markets];
    const marketsOut = {};
    if (selectedMarkets.includes("NSE")) {
      nseState = "MARKET_CLOSED_WAITING";
      nseAnalysis = buildAnalysis("NSE", "session_start", false);
      marketsOut.NSE = { session_state: nseState, initial_analysis: nseAnalysis };
    }
    if (selectedMarkets.includes("NASDAQ")) {
      nasdaqState = "RUNNING";
      nasdaqAnalysis = buildAnalysis("NASDAQ", "session_start", true);
      marketsOut.NASDAQ = { session_state: nasdaqState, initial_analysis: nasdaqAnalysis };
    }
    return json({ ok: true, simulated_only: true, live_trading_enabled: false, broker_order_api_calls: 0, selected_markets: selectedMarkets, markets: marketsOut, errors: {} });
  }
  if (url.pathname === "/api/markets/paper/sessions/transition" && method === "POST") {
    const marketsOut = {};
    for (const m of body.markets || selectedMarkets) {
      if (body.action === "pause") {
        if (m === "NSE") nseState = "PAUSED";
        if (m === "NASDAQ") nasdaqState = "PAUSED";
        marketsOut[m] = { session_state: "PAUSED" };
      } else if (body.action === "resume") {
        if (m === "NSE") { nseState = "MARKET_CLOSED_WAITING"; nseAnalysis = buildAnalysis("NSE", "session_resume", false); marketsOut.NSE = { session_state: nseState, initial_analysis: nseAnalysis }; }
        if (m === "NASDAQ") { nasdaqState = "RUNNING"; nasdaqAnalysis = buildAnalysis("NASDAQ", "session_resume", true); marketsOut.NASDAQ = { session_state: nasdaqState, initial_analysis: nasdaqAnalysis }; }
      } else if (body.action === "stop") {
        if (m === "NSE") nseState = "STOPPED";
        if (m === "NASDAQ") nasdaqState = "STOPPED";
        marketsOut[m] = { session_state: "STOPPED" };
      }
    }
    return json({ ok: true, action: body.action, markets: marketsOut, errors: {} });
  }
  if (url.pathname === "/api/markets/paper/analyze" && method === "POST") {
    const reports = {};
    for (const m of body.markets || selectedMarkets) {
      const rep = buildAnalysis(m, "operator_analyze", m === "NASDAQ");
      if (m === "NSE") nseAnalysis = rep; else nasdaqAnalysis = rep;
      reports[m] = rep;
    }
    return json({ ok: true, selected_markets: body.markets || selectedMarkets, reports, errors: {} });
  }
  if (url.pathname === "/api/markets/chart") {
    const mkt = url.searchParams.get("market");
    const sym = url.searchParams.get("symbol");
    const tf = url.searchParams.get("timeframe");
    if (chartProviderError) {
      return json({
        ok: false, market: mkt, symbol: sym, timeframe: tf,
        connection_state: "UNAVAILABLE", provider: "alpaca_market_data",
        data_source: "alpaca-market-data:iex", data_feed_label: "ALPACA IEX · SINGLE-VENUE",
        bars: [], quote: null, error: chartProviderError, simulated_bars: false, real_order_api_used: false, generated_at: now(),
      });
    }
    return json({
      ok: true, market: mkt, symbol: sym, timeframe: tf,
      connection_state: "CONNECTED_READ_ONLY", provider: "alpaca_market_data",
      data_source: "alpaca-market-data:iex", data_feed_label: "ALPACA IEX · SINGLE-VENUE",
      bars: Array.from({ length: 30 }, (_, i) => ({
        timestamp: new Date(Date.now() - (30 - i) * 60_000).toISOString(),
        open: 220 + i, high: 221 + i, low: 219 + i, close: 220.5 + i, volume: 10000,
      })),
      quote: { symbol: sym, bid: 250.1, ask: 250.2, last: 250.15, timestamp: now(), age_seconds: 1.1, fresh: true },
      simulated_bars: false, real_order_api_used: false, generated_at: now(),
    });
  }

  throw new Error(`Unhandled route in concurrent_multimarket_smoke: ${method} ${url.pathname}${url.search}`);
}

const dom = new JSDOM(html, { url: "http://127.0.0.1:8000/terminal", pretendToBeVisual: true, runScripts: "outside-only" });
const { window } = dom;
window.sessionStorage.setItem("axiom-api-token", TOKEN);
window.fetch = fetchFake;
window.HTMLCanvasElement.prototype.getContext = () => ({
  setTransform() {}, clearRect() {}, beginPath() {}, moveTo() {}, lineTo() {}, closePath() {},
  setLineDash() {}, save() {}, restore() {}, fillRect() {}, stroke() {}, fillText() {},
  measureText: (t) => ({ width: String(t).length * 6 }),
});
window.HTMLCanvasElement.prototype.getBoundingClientRect = () => ({ left: 0, top: 0, width: 920, height: 480 });

const flush = async (rounds = 8) => {
  for (let i = 0; i < rounds; i += 1) await new Promise((r) => setTimeout(r, 0));
};

window.eval(code);
await flush(10);

const doc = window.document;
const $ = (id) => doc.getElementById(id);
const click = async (el) => { assert(el, "Missing element"); el.click(); await flush(8); };

// 1. Fresh sessions default to parallel_ensemble (all enabled strategy variants together)
assert.equal($("mm-strat-nse-select").value, "parallel_ensemble");
assert.equal($("mm-strat-nasdaq-select").value, "parallel_ensemble");
assert.equal($("mm-select-nse").checked, true);
assert.equal($("mm-select-nasdaq").checked, true);
assert.ok($("mm-runner-status-pill").textContent.includes("RUNNER ACTIVE"));

// 2. Concurrent Start Selected Markets analyzes all selected markets & symbols immediately
await click($("mm-start-selected-btn"));
const startCall = apiCalls.find((c) => c.path === "/api/markets/paper/sessions/start");
assert.ok(startCall, "Expected POST /api/markets/paper/sessions/start");
assert.deepEqual(startCall.body.markets, ["NSE", "NASDAQ"]);
assert.equal(startCall.body.strategy_by_market.NSE, "parallel_ensemble");
assert.equal(startCall.body.strategy_by_market.NASDAQ, "parallel_ensemble");

// Closed NSE shows read-only historical analysis (2 succeeded, 0 simulated trades)
assert.ok($("mm-ov-nse-coverage").textContent.includes("Succeeded: 2"));
assert.ok($("mm-ov-nse-coverage").textContent.includes("0 simulated trades"));
// Open NASDAQ shows 1 succeeded (AAPL), 1 missing (MSFT), 1 failed (NVDA)
assert.ok($("mm-ov-nasdaq-coverage").textContent.includes("Succeeded: 1"));
assert.ok($("mm-ov-nasdaq-coverage").textContent.includes("Missing: 1"));
assert.ok($("mm-ov-nasdaq-coverage").textContent.includes("Failed: 1"));
assert.ok($("mm-ov-nasdaq-coverage").textContent.includes("MSFT"));
assert.ok($("mm-ov-nasdaq-coverage").textContent.includes("NVDA"));

// 3. Pause & Resume Selected re-analyzes immediately and updates status
await click($("mm-pause-selected-btn"));
assert.equal($("mm-nse-paper-state").textContent, "PAUSED");
assert.equal($("mm-nasdaq-paper-state").textContent, "PAUSED");

await click($("mm-resume-selected-btn"));
assert.equal($("mm-nse-paper-state").textContent, "MARKET_CLOSED_WAITING");
assert.equal($("mm-nasdaq-paper-state").textContent, "RUNNING");
assert.ok($("mm-strat-nasdaq-coverage").textContent.includes("session_resume"));

// 4. Live NASDAQ chart routes timeframe buttons & reload to /api/markets/chart (never local replay) and displays provider errors honestly
await click(doc.querySelector('[data-page="charts"]'));
$("mm-chart-source-mode").value = "NASDAQ";
$("mm-chart-source-mode").dispatchEvent(new window.Event("change", { bubbles: true }));
await flush(8);
const tf15Btn = doc.querySelector('[data-chart-timeframe="15m"]');
await click(tf15Btn);
const liveChart15m = [...apiCalls].reverse().find((c) => c.path === "/api/markets/chart");
assert.ok(liveChart15m.search.includes("market=NASDAQ") && liveChart15m.search.includes("timeframe=15m"), "Timeframe click on live feed must query /api/markets/chart");

chartProviderError = "Alpaca upstream rate limit HTTP 429";
await click($("reload-chart"));
assert.ok($("chart-feed-context").textContent.includes("Alpaca upstream rate limit HTTP 429"));
assert.ok($("chart-empty").textContent.includes("Zero sample fallback"));

console.log("concurrent_multimarket_smoke (jsdom): PASS");
window.close();
