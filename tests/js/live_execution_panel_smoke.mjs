/* Headless JSDOM regression suite for the per-market LIVE execution panel.
 *
 * Proves:
 *  - the panel renders entirely from the /api/markets/overview payload (no live fetches at load);
 *  - mode / limits / readiness / Arm / Activate / Emergency Stop / reset / reconcile each call
 *    their single documented endpoint, with the exact phrase guard enforced client-side;
 *  - a refused Arm surfaces the readiness failure and never sends an order;
 *  - one market's emergency stop leaves the other market's gate untouched;
 *  - the panel never posts to a paper/broker order endpoint.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { JSDOM } from "jsdom";
import { consoleReadRoutes, consoleWriteRoutes } from "./console_routes.mjs";

const root = resolve(import.meta.dirname, "../..");
const html = readFileSync(resolve(root, "app/static/terminal/index.html"), "utf8");
const code = readFileSync(resolve(root, "app/static/terminal/terminal.js"), "utf8");

const TOKEN = "live-exec-smoke-token";
const PHRASE = "ACTIVATE LIVE REAL-MONEY TRADING";
const now = () => new Date().toISOString();

const apiCalls = [];
const liveCalls = [];

const liveState = {
  NSE: { mode: "LIVE", gate: "DISABLED", kill: false, armed: false, limits: null, blocked: null },
  NASDAQ: { mode: "PAPER", gate: "DISABLED", kill: false, armed: false, limits: null, blocked: null },
};

const limitsPayload = (mkt) => (liveState[mkt].limits
  ? { ...liveState[mkt].limits, configured: true }
  : {
      max_order_value: 0, max_daily_orders: 0, max_daily_loss: 0, max_open_positions: 0,
      max_symbol_exposure: 0, max_total_exposure: 0, max_quote_age_seconds: 0,
      max_price_deviation_bps: 0, configured: false,
    });

const gatePayload = (mkt) => ({
  market: mkt,
  state: liveState[mkt].gate,
  kill_engaged: liveState[mkt].kill,
  kill_reason: liveState[mkt].kill ? "operator per-market emergency stop" : null,
  permit_active: liveState[mkt].gate === "ACTIVE",
  live_available: liveState[mkt].gate === "ACTIVE" && !liveState[mkt].kill,
  armed_at: liveState[mkt].armed ? now() : null,
  activated_at: liveState[mkt].gate === "ACTIVE" ? now() : null,
  live_confirmation_phrase: PHRASE,
  permit_ttl_seconds: 300,
});

const marketRow = (mkt) => ({
  market: mkt,
  currency: mkt === "NSE" ? "INR" : "USD",
  broker: mkt === "NSE" ? "Upstox (Protected Multi-Leg GTT v3)" : "Alpaca Trading API v2",
  destination_label: mkt === "NSE" ? "NSE · UPSTOX LIVE · REAL MONEY" : "NASDAQ · ALPACA LIVE · REAL MONEY",
  mode: liveState[mkt].mode,
  mode_label: liveState[mkt].mode === "LIVE" ? "LIVE · REAL MONEY" : "LIVE-MARKET PAPER · SIMULATED",
  real_money: liveState[mkt].mode === "LIVE",
  gate: gatePayload(mkt),
  configured: Boolean(liveState[mkt].limits),
  missing_dependencies: liveState[mkt].limits ? [] : ["Save the live credential", "Set the static IP"],
  risk_limits: limitsPayload(mkt),
  blocked: liveState[mkt].blocked,
  blocked_detail: liveState[mkt].blocked ? "Reconcile this market before resuming." : "",
  unresolved_intents: [],
  live_available: liveState[mkt].gate === "ACTIVE" && !liveState[mkt].kill,
  last_cycle_at: null,
  last_cycle_status: "IDLE",
  last_error: null,
  cycles_completed: 0,
  submitted_total: 0,
  rejected_total: 0,
  live_confirmation_phrase: PHRASE,
});

function makeOverview() {
  return {
    generated_at: now(),
    selected_markets: ["NSE", "NASDAQ"],
    runner: null,
    live_runner: {
      enabled: false, background_loop_running: false, interval_seconds: 15,
      market_timeout_seconds: 20, cycles_total: 0, last_tick_at: null,
      live_trading_enabled_by_default: false, eligible_markets: [], markets: {},
    },
    paper: {
      NSE: {
        market: "NSE", currency: "INR", session_state: "STOPPED", strategy: "manual_only",
        timeframe: "5m", effective_symbols: [], watched_symbols: [], last_analysis: null,
        exchange_session: { state: "CLOSED", is_open: false, timezone: "Asia/Kolkata" },
        account: { equity: 1000000, cash: 1000000, market_value: 14050, day_pnl: 50, realized_pnl: 0, total_fees_paid: 5 },
        positions: [{
          market: "NSE", symbol: "RELIANCE", instrument_key: "NSE_EQ|INE002A01018", qty: 10,
          avg_entry_price: 1400, last_price: 1405, market_value: 14050, unrealized_pnl: 50,
          unrealized_pnl_pct: 0.36, stop_loss: 1380, take_profit: 1450, currency: "INR",
          mode_label: "LIVE-MARKET PAPER · SIMULATED",
        }],
        orders: [{
          market: "NSE", symbol: "RELIANCE", instrument_key: "NSE_EQ|INE002A01018",
          client_order_id: "paper-o1", side: "BUY", order_type: "LIMIT", qty: 10,
          filled_qty: 0, limit_price: 1400, status: "WORKING", currency: "INR",
          mode_label: "LIVE-MARKET PAPER · SIMULATED",
        }],
        fills: [{
          market: "NSE", symbol: "RELIANCE", instrument_key: "NSE_EQ|INE002A01018",
          fill_key: "paper-f1", side: "BUY", qty: 10, fill_price: 1400, fees_total: 5,
          realized_pnl: 0, currency: "INR", fill_type: "SIMULATED", created_at: now(),
          mode_label: "LIVE-MARKET PAPER · SIMULATED",
        }],
        decisions: [],
      },
      NASDAQ: {
        market: "NASDAQ", currency: "USD", session_state: "STOPPED", strategy: "manual_only",
        timeframe: "5m", effective_symbols: [], watched_symbols: [], last_analysis: null,
        exchange_session: { state: "REGULAR", is_open: true, timezone: "America/New_York" },
        account: { equity: 100000, cash: 100000, market_value: 0, day_pnl: 0, realized_pnl: 0, total_fees_paid: 0 },
        positions: [], orders: [], fills: [], decisions: [],
      },
    },
    live: {
      default_live_execution: "OFF",
      execution_paths_are_separate: true,
      nse: { availability: "NOT_AVAILABLE", live_master_flag: false, live_credential_present: false, algo_name_configured: false, missing_dependencies: [] },
      nasdaq: { availability: "NOT_AVAILABLE", live_master_flag: false, live_credential_present: false, missing_dependencies: [], broker_status: { gate: { state: "DISABLED" } } },
    },
    live_execution: {
      default_live_execution: "OFF",
      real_money_enabled: liveState.NSE.gate === "ACTIVE" || liveState.NASDAQ.gate === "ACTIVE",
      confirmation_phrase: PHRASE,
      markets: { NSE: marketRow("NSE"), NASDAQ: marketRow("NASDAQ") },
    },
    execution_modes: { NSE: liveState.NSE.mode, NASDAQ: liveState.NASDAQ.mode },
    live_audit: [
      {
        seq: 4, market: "NSE", event_kind: "LIVE_MODE_CHANGED",
        payload: { mode: "LIVE", previous_mode: "PAPER", real_money: true },
        prev_hash: "genesis", entry_hash: "0123456789abcdef0123", created_at: now(),
      },
    ],
    providers: {
      providers: [
        { id: "upstox_market_data", name: "Upstox Market Data (NSE)", role: "MARKET_DATA_READ_ONLY", connection_state: "CONNECTED_READ_ONLY", credential_present: true, credential_source: "dpapi", used_for: [] },
        { id: "alpaca_market_data", name: "Alpaca Market Data (NASDAQ)", role: "MARKET_DATA_READ_ONLY", connection_state: "CONNECTED_READ_ONLY", credential_present: true, feed: "iex", used_for: [] },
        { id: "upstox_live", name: "Upstox LIVE (NSE)", role: "LIVE_BROKER_GATED", connection_state: "LOCKED", credential_present: false, used_for: [] },
        { id: "alpaca_broker_live", name: "Alpaca LIVE (US)", role: "LIVE_BROKER_GATED", connection_state: "LOCKED", credential_present: false, used_for: [] },
      ],
    },
    watchlists: { NSE: [], NASDAQ: [] },
    reconciliation: { NSE: { tradable_count: 0 }, NASDAQ: { counts: { total: 0 } } },
    recent_audit: [],
  };
}

const json = (payload, status = 200) =>
  new Response(JSON.stringify(payload), { status, headers: { "Content-Type": "application/json" } });

function readinessFor(mkt) {
  const checks = {
    configuration_complete: Boolean(liveState[mkt].limits),
    risk_limits_configured: Boolean(liveState[mkt].limits),
    mode_is_live: liveState[mkt].mode === "LIVE",
    no_unresolved_intents: liveState[mkt].blocked === null,
    not_blocked: liveState[mkt].blocked === null,
    kill_switch_not_engaged: !liveState[mkt].kill,
  };
  const failed = Object.keys(checks).filter((k) => !checks[k]).sort();
  return { market: mkt, ok: failed.length === 0, checks, failed, missing_dependencies: [], gate: gatePayload(mkt) };
}

async function fetchFake(input, init = {}) {
  const rawUrl = typeof input === "string" ? input : input.url;
  const url = new URL(rawUrl, "http://127.0.0.1:8000");
  assert.ok(!/(alpaca\.markets|upstox\.com|localhost|127\.0\.0\.1:8000\/api\/markets\/live)/.test(rawUrl) || url.origin === "http://127.0.0.1:8000",
    "Browser must never call external broker URLs");
  const method = (init.method || "GET").toUpperCase();
  const body = init.body ? JSON.parse(init.body) : null;
  apiCalls.push({ method, path: url.pathname, search: url.search, body });

  // ------------------------------------------------------------------ LIVE
  const liveMatch = url.pathname.match(/^\/api\/markets\/live\/(nse|nasdaq)\/([a-z-]+)$/);
  if (liveMatch) {
    liveCalls.push({ method, path: url.pathname, body });
    const mkt = liveMatch[1].toUpperCase();
    const action = liveMatch[2];
    const st = liveState[mkt];
    if (action === "readiness" && method === "GET") return json(readinessFor(mkt));
    if (action === "mode" && method === "POST") {
      st.mode = String(body.mode || "").toUpperCase();
      if (st.mode !== "LIVE") {
        st.gate = "DISABLED";
        st.armed = false;
      }
      return json({ market: mkt, mode: st.mode, previous_mode: "PAPER", gate: gatePayload(mkt) });
    }
    if (action === "limits" && method === "POST") {
      st.limits = { ...(st.limits || {}), ...body };
      st.gate = "DISABLED";
      st.armed = false;
      return json({ market: mkt, risk_limits: limitsPayload(mkt), gate: gatePayload(mkt) });
    }
    if (action === "arm" && method === "POST") {
      const report = readinessFor(mkt);
      if (!report.ok) return json({ detail: `${mkt} readiness failed: ${report.failed.join(", ")}` }, 409);
      st.gate = "ARMED";
      st.armed = true;
      return json({ market: mkt, gate: gatePayload(mkt), readiness: report });
    }
    if (action === "activate" && method === "POST") {
      if (st.gate !== "ARMED" || body.confirm_live_trading !== PHRASE || body.acknowledge_risk !== true) {
        return json({ detail: "LIVE activation requires acknowledge_risk=true and the exact phrase" }, 409);
      }
      st.gate = "ACTIVE";
      return json({ market: mkt, activation: { active: true }, gate: gatePayload(mkt) });
    }
    if (action === "disarm" && method === "POST") {
      st.gate = st.kill ? "KILL_SWITCH" : "DISABLED";
      st.armed = false;
      return json({ market: mkt, gate: gatePayload(mkt) });
    }
    if (action === "emergency-stop" && method === "POST") {
      st.gate = "KILL_SWITCH";
      st.kill = true;
      st.armed = false;
      return json({ ok: true, market: mkt, real_money: true, gate: gatePayload(mkt), canceled_orders: [], cancel_errors: [], other_market_untouched: true });
    }
    if (action === "reset-kill-switch" && method === "POST") {
      if (body.operator_ack !== true) return json({ detail: "operator acknowledgement required" }, 409);
      st.kill = false;
      st.gate = "DISABLED";
      return json({ ok: true, market: mkt, gate: gatePayload(mkt) });
    }
    if (action === "reconcile" && method === "POST") {
      st.blocked = null;
      return json({ market: mkt, ok: true, resolved_intents: 0, adopted_orders: 0, positions: 0 });
    }
    return json({ detail: `unexpected live action ${action}` }, 404);
  }

  // ------------------------------------------------------- legacy/terminal
  const legacyWs = {
    generated_at: now(),
    status: { mode: "AXIOM_PAPER", connected: false, armed: false, emergency_stop: false, live_trading_enabled: false, data_feed: "Dataset", risk_limits: {} },
    account: { currency: "USD", balance: 100000, equity: 100000, free_margin: 100000, margin_used: 0, unrealized_pnl: 0, realized_pnl: 0 },
    market_data: { connected: false, state: "DISCONNECTED" },
    paper_session: { state: "STOPPED", strategy: "moving_average", symbols: ["EURUSD"], timeframe: "1m" },
    positions: [], open_orders: [], order_history: [], deals: [], quotes: [], audit: [],
    risk_rejections: [], reconciliation: [], strategies: [],
  };
  const projected = consoleReadRoutes(url.pathname, legacyWs, { events: [{ seq: 1, market: "NSE", event_kind: "FAKE_AUDIT", payload: { safe: true }, created_at: now(), entry_hash: "fake-hash" }] });
  if (projected !== undefined) return json(projected);
  const emergency = consoleWriteRoutes(url.pathname, method, body, {});
  if (emergency !== undefined) return json(emergency);
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
  if (url.pathname === "/api/markets/overview") return json(makeOverview());
  if (url.pathname === "/api/markets/search") return json({ items: [] });
  if (url.pathname === "/api/markets/paper/decisions") return json({ total: 0, decisions: [], latest_by_symbol: [] });
  if (url.pathname === "/api/markets/paper/logs") return json({ ok: true, total_matching: 0, returned: 0, events: [], observed_sessions: [], observed_strategies: [], observed_symbols: [] });
  if (url.pathname.startsWith("/api/markets/paper/orders")) {
    throw new Error(`LIVE panel must never call a paper order endpoint: ${method} ${url.pathname}`);
  }
  if (url.pathname.startsWith("/api/alpaca-broker/order")) {
    throw new Error(`LIVE panel must never call a broker order endpoint: ${method} ${url.pathname}`);
  }
  return json({});
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
await flush(12);

const doc = window.document;
const $ = (id) => doc.getElementById(id);
const click = async (el) => { assert.ok(el, "Missing element"); el.click(); await flush(8); };
const pills = () => ({
  nseMode: $("mm-live-exec-nse-pill").textContent,
  nasdaqMode: $("mm-live-exec-nasdaq-pill").textContent,
  nseGate: $("mm-live-gate-nse").textContent,
  nasdaqGate: $("mm-live-gate-nasdaq").textContent,
});

// 1. Load-time: the panel renders from the overview payload, no live API traffic yet.
assert.ok($("mm-live-execution-panel"), "live execution panel must exist");
assert.equal(liveCalls.length, 0, "LIVE panel must not fetch any live endpoint while merely rendering");
assert.ok($("mm-live-exec-nse-pill").textContent.includes("LIVE · REAL MONEY"), pills().nseMode);
assert.ok($("mm-live-exec-nasdaq-pill").textContent.includes("SIMULATED"), pills().nasdaqMode);
assert.equal(pills().nseGate, "DISABLED");
assert.equal(pills().nasdaqGate, "DISABLED");
assert.ok($("mm-live-readiness-nse-out").textContent.includes("NOT configured"), $("mm-live-readiness-nse-out").textContent);
assert.ok($("mm-live-audit-rows").textContent.includes("LIVE_MODE_CHANGED"));
assert.ok($("mm-live-audit-rows").textContent.includes("0123456789abcd"), "audit hash must be visible");

// 2. Readiness reports the failing checks honestly (no Arm, no orders).
await click($("mm-live-readiness-nse"));
assert.ok(liveCalls.some((c) => c.method === "GET" && c.path === "/api/markets/live/nse/readiness"));
assert.ok($("mm-live-readiness-nse-out").textContent.includes("readiness FAILED"), $("mm-live-readiness-nse-out").textContent);
assert.ok($("mm-live-readiness-nse-out").textContent.includes("risk_limits_configured"));

// 3. Arm is refused (409) and the refusal is surfaced; the gate stays DISABLED.
await click($("mm-live-arm-nse"));
assert.ok(liveCalls.some((c) => c.method === "POST" && c.path === "/api/markets/live/nse/arm"));
assert.ok($("mm-live-exec-output").textContent.includes("arm failed"), $("mm-live-exec-output").textContent);
assert.equal(pills().nseGate, "DISABLED");

// 4. A partial limit set is refused client-side (the server fails closed too).
$("mm-live-limits-nse-order").value = "250000";
$("mm-live-limits-nse-orders").value = "20";
$("mm-live-limits-nse-loss").value = "25000";
$("mm-live-limits-nse-pos").value = "10";
await click($("mm-live-limits-save-nse"));
assert.equal(liveCalls.filter((c) => c.path === "/api/markets/live/nse/limits").length, 0,
  "partial limits must never be sent");
assert.ok($("mm-live-exec-output").textContent.includes("all eight limits are required"),
  $("mm-live-exec-output").textContent);

// 4b. The full eight-limit set is posted verbatim.
$("mm-live-limits-nse-symexp").value = "250000";
$("mm-live-limits-nse-totexp").value = "1000000";
$("mm-live-limits-nse-age").value = "60";
$("mm-live-limits-nse-dev").value = "150";
await click($("mm-live-limits-save-nse"));
const limitsCall = liveCalls.find((c) => c.path === "/api/markets/live/nse/limits");
assert.ok(limitsCall, "limits save endpoint must be called");
assert.deepEqual(limitsCall.body, {
  max_order_value: 250000, max_daily_orders: 20, max_daily_loss: 25000, max_open_positions: 10,
  max_symbol_exposure: 250000, max_total_exposure: 1000000,
  max_quote_age_seconds: 60, max_price_deviation_bps: 150,
});
assert.ok($("mm-live-exec-output").textContent.includes("risk limits saved"));
assert.equal(pills().nseGate, "DISABLED", "saving limits disarms the gate");

// 5. Readiness now passes, Arm succeeds, and no order endpoint was touched.
await click($("mm-live-readiness-nse"));
assert.ok($("mm-live-readiness-nse-out").textContent.includes("readiness PASSED"));
await click($("mm-live-arm-nse"));
assert.equal(pills().nseGate, "ARMED");
assert.ok($("mm-live-exec-output").textContent.includes("ARMED"));

// 6. Activate is impossible without the acknowledgement checkbox + exact phrase (client-side).
const activateCalls = () => liveCalls.filter((c) => c.path === "/api/markets/live/nse/activate");
await click($("mm-live-activate-nse"));
assert.equal(activateCalls().length, 0, "activate must not be called without acknowledgement");
assert.ok($("mm-live-exec-output").textContent.includes("acknowledgement checkbox is required"));

$("mm-live-ack-nse").checked = true;
$("mm-live-phrase-nse").value = "activate live";
await click($("mm-live-activate-nse"));
assert.equal(activateCalls().length, 0, "activate must not be called with a wrong phrase");
assert.ok($("mm-live-exec-output").textContent.includes("exact confirmation phrase is required"));
assert.equal(pills().nseGate, "ARMED");

// 7. The exact phrase + acknowledgement activates NSE only, and the inputs are cleared.
$("mm-live-phrase-nse").value = PHRASE;
await click($("mm-live-activate-nse"));
assert.equal(activateCalls().length, 1);
assert.deepEqual(activateCalls()[0].body, { confirm_live_trading: PHRASE, acknowledge_risk: true });
assert.equal(pills().nseGate, "ACTIVE");
assert.equal($("mm-live-phrase-nse").value, "");
assert.equal($("mm-live-ack-nse").checked, false);
assert.equal(pills().nasdaqGate, "DISABLED", "activating NSE must not arm NASDAQ");
assert.ok($("mm-live-exec-nasdaq-pill").textContent.includes("SIMULATED"));

// 8. NASDAQ stays a separate paper market even while NSE is live.
await click($("mm-live-readiness-nasdaq"));
assert.ok(liveCalls.some((c) => c.path === "/api/markets/live/nasdaq/readiness"));
assert.ok($("mm-live-readiness-nasdaq-out").textContent.includes("readiness FAILED"));

// 9. Per-market emergency stop halts NSE and leaves NASDAQ untouched.
await click($("mm-live-estop-nse"));
assert.equal(pills().nseGate, "KILL_SWITCH");
assert.equal(pills().nasdaqGate, "DISABLED");
assert.ok($("mm-live-exec-output").textContent.includes("EMERGENCY STOP"));
assert.ok($("mm-live-exec-output").textContent.includes("other market's safeguards are untouched"));

// 10. Kill-switch reset requires operator ack and returns the gate to DISABLED.
await click($("mm-live-reset-nse"));
const resetCall = liveCalls.find((c) => c.path === "/api/markets/live/nse/reset-kill-switch");
assert.deepEqual(resetCall.body, { operator_ack: true });
assert.equal(pills().nseGate, "DISABLED");

// 11. Reconcile calls a single reconcile endpoint and reports the resolved count.
await click($("mm-live-reconcile-nse"));
assert.ok(liveCalls.some((c) => c.path === "/api/markets/live/nse/reconcile"));
assert.ok($("mm-live-exec-output").textContent.includes("reconciled with the broker"));
assert.ok($("mm-live-exec-output").textContent.includes("0 intent(s) resolved"));

// 12. Mode switching is explicit and never implies live trading.
$("mm-live-mode-nasdaq").value = "LIVE";
await click($("mm-live-mode-save-nasdaq"));
const modeCall = liveCalls.find((c) => c.path === "/api/markets/live/nasdaq/mode");
assert.deepEqual(modeCall.body, { mode: "LIVE" });
assert.ok($("mm-live-exec-output").textContent.includes("LIVE remains DISABLED until readiness, Arm, and the typed phrase"));
assert.equal(pills().nasdaqGate, "DISABLED");

// 13. Every paper order/fill/position row also states the market's CURRENT routing,
//     so a simulated ledger row can never be read as a live position.
assert.ok($("mm-paper-position-rows").textContent.includes("RELIANCE"));
assert.ok($("mm-paper-position-rows").textContent.includes("NSE · ROUTED LIVE (real money)"),
  `position row must show the market routing: ${$("mm-paper-position-rows").textContent}`);
assert.ok($("mm-paper-order-rows").textContent.includes("NSE · ROUTED LIVE (real money)"));
assert.ok($("mm-paper-fill-rows").textContent.includes("NSE · ROUTED LIVE (real money)"));
assert.ok($("mm-paper-fill-rows").textContent.includes("SIMULATED"));
assert.ok($("mm-paper-position-rows").textContent.includes("PAPER"), "the ledger stays labelled PAPER");

// 14. Absolutely no paper/broker order endpoint was ever contacted by the panel.
assert.equal(
  apiCalls.filter((c) => /paper\/orders|alpaca-broker\/order/.test(c.path) && c.method !== "GET").length, 0,
  "reading the paper order book is not an order; no order write may occur",
);
assert.equal(apiCalls.filter((c) => c.path.startsWith("/api/markets/live/") && c.method === "POST" && /approved|submit|order/.test(c.path)).length, 0);

console.log(`live_execution_panel_smoke (jsdom): PASS (${liveCalls.length} live API calls, 0 order endpoints)`);
window.close();
