/* Headless JSDOM suite for the quota-aware read-only scan panel
 * "Scan Capacity & Quotas" inside workspace 12 (#page-ensemble).
 *
 * Proves:
 *  - the panel renders entirely from /api/ensemble/scan/capacity (no hard-coded
 *    quota numbers in the client, no provider fetch on render);
 *  - documented windows are printed readably, with text statuses (never colour-only);
 *  - the requested cadence and the server-computed minimum are both visible and the
 *    Start button is disabled whenever the server says the cycle cannot run;
 *  - Preview is an offline call that consumes no scan cycle;
 *  - Start posts to the read-only scan route only, and the rendered results keep
 *    ANALYZED, RISK-APPROVED CANDIDATES and ORDERS (0) separate;
 *  - nothing ever calls an order, strategy-start or live-trading endpoint.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { JSDOM } from "jsdom";
import { consoleReadRoutes, consoleWriteRoutes } from "./console_routes.mjs";

const root = resolve(import.meta.dirname, "../..");
const html = readFileSync(resolve(root, "app/static/terminal/index.html"), "utf8");
const code = readFileSync(resolve(root, "app/static/terminal/terminal.js"), "utf8");

const TOKEN = "scan-capacity-token";
const apiCalls = [];
const now = () => new Date("2026-11-07T10:00:00Z").toISOString();

const BASE = {
  provider_network_calls: 0,
  broker_order_calls: 0,
  quota_math: "DOCUMENTED",
  timing_model: "MODELED OFFLINE",
};
const NOTICE = "Scanning a symbol does not mean it will be traded.";

let scanRuns = [];

const UPSTOX_POLICY = {
  category: "upstox.market_quote",
  label: "Upstox GET /v2/market-quote/quotes",
  windows: [
    { name: "1s", seconds: 1, limit: 50 },
    { name: "1m", seconds: 60, limit: 500 },
    { name: "30m", seconds: 1800, limit: 2000 },
  ],
  source_url: "https://upstox.com/developer/api-documentation/rate-limiting/",
  documented_as: "Other Standard APIs 50/s, 500/min, 2000/30min",
};
const UPSTOX_CANDLE_POLICY = {
  ...UPSTOX_POLICY,
  category: "upstox.historical_candle",
  label: "Upstox GET /v3/historical-candle (and /intraday)",
};
const ALPACA_POLICY = {
  category: "alpaca.market_data",
  label: "Alpaca market data (basic: 200/min)",
  windows: [{ name: "1m", seconds: 60, limit: 200 }],
  source_url: "https://docs.alpaca.markets/us/docs/about-market-data-api.md",
  documented_as: "Basic plan: 200 historical API calls per minute",
};

const windowRow = (name, seconds, limit, current, requests, { paced = false } = {}) => {
  const projected = current + requests;
  const fits = projected <= limit;
  const blocks = !fits && !paced;
  return {
    window: name, seconds, label: seconds === 1 ? "1 second" : `${seconds / 60} minute`,
    limit, current, requests_this_cycle: requests, projected, fits_now: fits, paced, blocks,
    wait_seconds: blocks ? 6 : 0, headroom: limit - projected,
    readable: `${current} / ${limit.toLocaleString("en-US")} requests this ${seconds === 1 ? "1 second" : `${seconds / 60} minute`}`,
  };
};

function planFor(market, { symbols = 100, cadence = 60, credential = true } = {}) {
  const isNse = market === "NSE";
  const sustainable = isNse ? 90 : 60;
  const requests = isNse
    ? { "upstox.market_quote": 100, "upstox.historical_candle": 100 }
    : { "alpaca.market_data": 200 };
  const reasons = [];
  let status = "READY";
  let canStart = true;
  if (cadence < sustainable) {
    status = "SLOWER CADENCE REQUIRED";
    canStart = false;
    reasons.push(`${market}: ${symbols} symbols need at least ${sustainable.toFixed(1)}s between cycles (${isNse ? 16 : 25} symbols fit a ${cadence}s cadence)`);
  } else if (!isNse) {
    status = "PLAN UNVERIFIED";
    reasons.push("Basic (free) profile assumed; entitlement not verified");
  }
  if (!credential) {
    status = "NO CREDENTIAL";
    canStart = false;
    reasons.push(`${market}: read-only market-data credential is not configured`);
  }
  return {
    market, symbols, cadence_requested_seconds: cadence, cadence_sustainable_seconds: sustainable,
    max_symbols_at_requested_cadence: cadence < sustainable ? (isNse ? 16 : 25) : 100,
    requests_per_cycle: requests, total_requests_per_cycle: Object.values(requests).reduce((a, b) => a + b, 0),
    expected_dispatch_seconds: isNse ? 2 : 1,
    status, can_start: canStart, reasons, window_usage: isNse
      ? {
          "upstox.market_quote": [
            windowRow("1s", 1, 50, 0, 100, { paced: true }),
            windowRow("1m", 60, 500, 0, 100),
            windowRow("30m", 1800, 2000, 0, 100),
          ],
          "upstox.historical_candle": [
            windowRow("1s", 1, 50, 0, 100, { paced: true }),
            windowRow("1m", 60, 500, 0, 100),
            windowRow("30m", 1800, 2000, 0, 100),
          ],
        }
      : { "alpaca.market_data": [windowRow("1m", 60, 200, 0, 200)] },
    blocking_window: null, wait_seconds: 0,
    quota_profile: isNse ? "upstox_standard" : "alpaca_basic",
    credential_present: credential,
    plan_state: isNse ? "verified" : "unverified",
    plan_detail: isNse ? "Upstox standard API windows" : "Basic (free) profile assumed",
  };
}

function quotaFor(market, credential = true) {
  const isNse = market === "NSE";
  return {
    market,
    profile: isNse ? "upstox_standard" : "alpaca_basic",
    plan_state: isNse ? "verified" : "unverified",
    plan_detail: isNse ? "Upstox standard API windows" : "Basic (free) profile assumed",
    verification: isNse ? "DOCUMENTED" : "UNVERIFIED",
    credential_present: credential,
    policies: isNse ? [UPSTOX_POLICY, UPSTOX_CANDLE_POLICY] : [ALPACA_POLICY],
    endpoints_per_category: isNse
      ? { "upstox.market_quote": 1, "upstox.historical_candle": 1 }
      : { "alpaca.market_data": 2 },
  };
}

const LIMITS = {
  ensemble_allocator: { max_open_positions: 8, max_symbol_exposure_pct: 0.25, max_total_exposure_pct: 0.8 },
  paper_risk_gate: { max_open_positions: 10, max_daily_orders: 40 },
  note: "Unchanged by scanning",
};

function capacityPayload({ credential = true, cadence = 60 } = {}) {
  const snap = (market) => ({
    market,
    quota: quotaFor(market, credential),
    plan: planFor(market, { cadence, credential }),
    gate: { requests: 0, deferred: 0, peak_inflight: 0 },
    limits: LIMITS,
    selected_symbols: 33,
    scan_running: false,
    history: scanRuns.filter((run) => run.market === market),
  });
  return {
    generated_at: now(), basis: BASE, notice: NOTICE, max_symbols_per_scan: 100,
    markets: { NSE: snap("NSE"), NASDAQ: snap("NASDAQ") },
    scan_status: { running: {}, history_count: scanRuns.length, max_symbols_per_scan: 100 },
  };
}

let credentialPresent = true;

function dispatchLines(plan) {
  return [
    `Symbols per cycle: ${plan.symbols} (each symbol is analyzed; at most the risk-allocator limits can become trades)`,
    `Provider requests per cycle: ${plan.total_requests_per_cycle} (${Object.entries(plan.requests_per_cycle).map(([k, v]) => `${k}=${v}`).join(", ")})`,
    `Computed sustainable cadence: every ${plan.cadence_sustainable_seconds}s`,
    `Requested cadence: every ${plan.cadence_requested_seconds}s`,
    "Orders on this path: 0 (read-only market data)",
  ];
}

const json = (payload, status = 200) =>
  new Response(JSON.stringify(payload), { status, headers: { "Content-Type": "application/json" } });

async function fetchFake(input, init = {}) {
  const rawUrl = typeof input === "string" ? input : input.url;
  const url = new URL(rawUrl, "http://127.0.0.1:8000");
  const method = (init.method || "GET").toUpperCase();
  const body = init.body ? JSON.parse(init.body) : null;
  apiCalls.push({ method, path: url.pathname, body });

  // The scan panel must never reach an order / strategy-start / live route.
  if (/(\/order|\/orders|\/strategy\/start|\/live\/)/.test(url.pathname)) {
    throw new Error(`Scan panel must never call ${method} ${url.pathname}`);
  }

  if (url.pathname === "/api/ensemble/scan/capacity") {
    const cadence = Number(url.searchParams.get("cadence_s") || 60);
    return json(capacityPayload({ credential: credentialPresent, cadence }));
  }
  if (url.pathname === "/api/ensemble/scan/preview" && method === "POST") {
    const plan = planFor(body.market, {
      symbols: body.symbol_count, cadence: body.cadence_s, credential: credentialPresent,
    });
    return json({
      market: body.market, requested_symbols: body.symbol_count, basis: BASE, notice: NOTICE,
      plan, quota: quotaFor(body.market, credentialPresent), gate: { requests: 0 }, limits: LIMITS,
      dispatch_plan: dispatchLines(plan),
    });
  }
  if (url.pathname === "/api/ensemble/scan/start" && method === "POST") {
    const plan = planFor(body.market, {
      symbols: body.symbol_count, cadence: body.cadence_s, credential: credentialPresent,
    });
    if (!plan.can_start) {
      return json({ started: false, status: plan.status, plan, detail: plan.reasons.join("; "), market: body.market, basis: BASE, notice: NOTICE });
    }
    const run = {
      run_id: "run-smoke-0001", market: body.market, state: "COMPLETED",
      started_at: now(), finished_at: now(),
      symbols_selected: plan.symbols, symbols_analyzed: plan.symbols - 2, symbols_stale: 1, symbols_failed: 1, symbols_deferred: 0,
      candidates_risk_approved: 3, orders: 0, broker_order_api_calls: 0,
      cadence_requested_seconds: plan.cadence_requested_seconds,
      cadence_achieved_seconds: 3.2, cadence_met: true, elapsed_ms: 3200,
      calls_by_category: plan.requests_per_cycle, quota_after: {}, quota_windows_after: {},
      detail: "analyzed", error: "", orders_submitted: 0, live_execution: "DISABLED",
    };
    scanRuns = [run, ...scanRuns];
    return json({
      started: true, status: "SCANNING", run_id: run.run_id, market: body.market,
      requested_symbols: plan.symbols, plan, basis: BASE, notice: NOTICE,
      dispatch_plan: dispatchLines(plan),
    });
  }
  if (url.pathname === "/api/ensemble/scan/history") {
    return json({
      market: (url.searchParams.get("market") || "ALL").toUpperCase(), runs: scanRuns,
      scan_status: { running: {}, history_count: scanRuns.length }, basis: BASE, notice: NOTICE,
    });
  }

  const legacyWs = {
    generated_at: now(),
    status: { mode: "AXIOM_PAPER", connected: false, armed: false, emergency_stop: false, live_trading_enabled: false, data_feed: "Dataset", risk_limits: {} },
    account: { currency: "USD", balance: 100000, equity: 100000, free_margin: 100000, margin_used: 0, unrealized_pnl: 0, realized_pnl: 0 },
    market_data: { connected: false, state: "DISCONNECTED" },
    paper_session: { state: "STOPPED", strategy: "moving_average", symbols: ["EURUSD"], timeframe: "1m" },
    positions: [], open_orders: [], order_history: [], deals: [], quotes: [], audit: [],
    risk_rejections: [], reconciliation: [], strategies: [],
  };
  const projected = consoleReadRoutes(url.pathname, legacyWs);
  if (projected !== undefined) return json(projected);
  const written = consoleWriteRoutes(url.pathname, method, body, {});
  if (written !== undefined) return json(written);
  if (url.pathname === "/api/trading/workspace" || url.pathname === "/api/terminal/workspace") return json(legacyWs);
  if (url.pathname === "/api/trading/status") return json(legacyWs.status);
  if (url.pathname === "/api/trading/activity") return json({ events: [] });
  if (url.pathname === "/api/trading/connection") return json({ status: legacyWs.status, profiles: {}, secret_store: "DPAPI", test_is_read_only: true, mt5_package_available: false, mt5_integration: "UNAVAILABLE" });
  if (url.pathname === "/api/market-data/status" || url.pathname === "/api/trading/market-data") return json(legacyWs.market_data);
  if (url.pathname === "/api/trading/performance") return json({ targets_ms: {}, operations: {} });
  if (url.pathname === "/api/trading/listed-assets") return json({ classes: [], total_curated_examples: 0 });
  if (url.pathname === "/api/trading/chart/symbols") return json({ symbols: [] });
  if (url.pathname === "/api/trading/chart") return json({ symbol: "EURUSD", timeframe: "1m", source: "Dataset", simulated: true, bars: [] });
  if (url.pathname === "/api/alpaca-broker/status") return json({ enabled: false, environment: "paper", real_money: false, gate: { state: "DISABLED" }, limits: {} });
  if (url.pathname === "/api/markets/overview") return json({ markets: [], generated_at: now() });
  if (url.pathname === "/api/markets/search") return json({ items: [] });
  if (url.pathname === "/api/ensemble/overview") return json({ paper_and_research_only: true, live_execution_authorized: false, preset: "compact", registry: { enabled_variants: 24, unique_canonical_variants: 24 }, enabled_families: [], default_timeframe: "5m", concurrency_limit: 4, batch_timeout_seconds: 30, allocators: {}, calibration: {}, weights: {}, recent_decisions: [], ablation_runs: [], benchmarks: [] });
  return json({});
}

const flush = async (rounds = 10) => {
  for (let i = 0; i < rounds; i += 1) await new Promise((r) => setTimeout(r, 0));
};

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
window.eval(code);
await flush(16);

const doc = window.document;
const $ = (id) => doc.getElementById(id);
const click = async (el) => { assert.ok(el, "missing element"); el.click(); await flush(14); };
const scanCalls = (suffix) => apiCalls.filter((c) => c.path === `/api/ensemble/scan/${suffix}`);

// ---------------------------------------------------------------- navigation
await click(doc.querySelector('.nav-item[data-page="ensemble"]'));
assert.ok(!$("page-ensemble").hidden, "ensemble page must be visible");

// 1. Panel chrome, badge and the non-trading sentence
const panel = $("ens-scan-panel");
assert.ok(panel, "Scan Capacity & Quotas panel missing");
assert.match(panel.textContent, /Scan Capacity &amp; Quotas|Scan Capacity & Quotas/);
assert.equal($("ens-scan-badge").textContent.trim(), "READ-ONLY MARKET DATA · NO ORDER SUBMISSION");
assert.match($("ens-scan-notice").textContent, /Scanning a symbol does not mean it will be traded/);
assert.match($("ens-scan-basis").textContent, /MODELED OFFLINE/);

// 2. Both market cards exist with machine-independent structure
const cards = [...doc.querySelectorAll("#ens-scan-cards [data-scan-market]")];
assert.deepEqual(cards.map((c) => c.dataset.scanMarket), ["NSE", "NASDAQ"]);
const nse = cards[0];
const nasdaq = cards[1];
const role = (card, name) => card.querySelector(`[data-scan-role="${name}"]`);

// 3. Documented windows are readable text, per endpoint category
const nseWindows = role(nse, "windows").textContent;
assert.match(nseWindows, /Upstox GET \/v2\/market-quote\/quotes/);
assert.match(nseWindows, /0 \/ 500 requests this 1 minute/);
assert.match(nseWindows, /0 \/ 2,000 requests this 30 minute/);
assert.match(nseWindows, /paced by the gate/);
assert.match(role(nasdaq, "windows").textContent, /0 \/ 200 requests this 1 minute/);

// 4. 100 symbols @ 60 s on NSE is below the documented cadence -> blocked, text-only status
assert.equal(role(nse, "status").textContent.trim(), "SLOWER CADENCE REQUIRED");
assert.equal(role(nse, "min").value, "90 s (server-computed)");
assert.equal(role(nse, "start").disabled, true);
assert.match(role(nse, "verdict").textContent, /SLOWER CADENCE REQUIRED/);
assert.match(role(nse, "verdict").textContent, /90\.0s between cycles/);
assert.match(role(nse, "facts").textContent, /Max open positions \(unchanged\)/);
assert.match(role(nse, "facts").textContent, /Max orders per day \(unchanged\)/);

// 5. NASDAQ: documented cadence fits, but the plan is unverified -> start stays allowed
assert.equal(role(nasdaq, "status").textContent.trim(), "PLAN UNVERIFIED");
assert.equal(role(nasdaq, "start").disabled, false);
assert.match(role(nasdaq, "facts").textContent, /UNVERIFIED/);

// 6. Nothing has been started by rendering; the only calls are reads
assert.equal(scanCalls("start").length, 0);
assert.equal(scanCalls("preview").length, 0);
assert.ok(scanCalls("capacity").length >= 1);

// 7. Preview is offline: it posts the requested selection and starts no cycle
role(nse, "cadence").value = "90";
role(nse, "cadence").dispatchEvent(new window.Event("input", { bubbles: true }));
await click(role(nse, "preview"));
await flush(6);
const preview = scanCalls("preview")[0];
assert.ok(preview, "preview must call the preview route");
assert.equal(preview.method, "POST");
assert.equal(preview.body.market, "NSE");
assert.equal(preview.body.cadence_s, 90);
assert.equal(preview.body.symbol_count, 100);
assert.equal(scanCalls("start").length, 0);
assert.equal(role(nse, "status").textContent.trim(), "READY");
assert.equal(role(nse, "start").disabled, false);
assert.match(role(nse, "estimate").textContent, /EXACT EXPECTED DISPATCH/);
assert.match(role(nse, "estimate").textContent, /Orders on this path: 0/);

// 8. Start runs one read-only cycle and the result keeps candidates and orders apart
await click(role(nse, "start"));
await flush(20);
const started = scanCalls("start");
assert.equal(started.length, 1);
assert.equal(started[0].method, "POST");
assert.equal(started[0].body.market, "NSE");
assert.equal(started[0].body.cadence_s, 90);
assert.ok(!("execute_in_paper" in started[0].body), "the client must not choose an execution target");
assert.ok(!("live" in started[0].body));

const results = role(nse, "results").textContent;
assert.match(results, /Analysed/);
assert.match(results, /Risk-approved candidates/);
assert.match(results, /Orders\s*0/);
assert.match(results, /ANALYZED = symbols with a completed decision/);
const history = $("ens-scan-history-rows").textContent;
assert.match(history, /run-smoke-0001/);
assert.match(history, /COMPLETED/);
assert.match($("ens-scan-history-pill").textContent, /1 CYCLE/);

// 9. Missing credentials surface NO CREDENTIAL and disable Start
credentialPresent = false;
role(nasdaq, "preview").dispatchEvent(new window.Event("click", { bubbles: true }));
await flush(14);
assert.equal(role(nasdaq, "status").textContent.trim(), "NO CREDENTIAL");
assert.equal(role(nasdaq, "start").disabled, true);

// 10. The scan panel never writes to an order / strategy / live route. The console's
// own read-only book load (GET /paper/orders) is unrelated and excluded by method.
const forbidden = apiCalls.filter(
  (c) => /(\/order|\/orders|\/strategy\/start|\/live\/)/.test(c.path) && c.method !== "GET",
);
assert.deepEqual(forbidden, [], `unexpected order endpoints: ${JSON.stringify(forbidden)}`);
assert.equal(scanCalls("start").filter((c) => c.body && "execute_in_paper" in c.body).length, 0);

console.log(`# scan_capacity_smoke: OK (${apiCalls.length} local API calls, ${scanRuns.length} simulated read-only cycle, 0 order endpoints)`);
window.close();
