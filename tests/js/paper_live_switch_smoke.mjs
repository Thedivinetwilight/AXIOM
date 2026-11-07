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
  NSE: { mode: "PAPER", gate: "DISABLED", kill: false, armed: false, limits: null, blocked: null },
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

const exitRouteFor = (mkt) => (mkt === "NSE"
  ? {
      routed: false,
      summary: "No NSE exit is routed. Every documented SELL path is either executable while flat or cannot be verified as closing from inside AXIOM.",
      closing_paths: ["the STOPLOSS / TARGET legs of the protected GTT that opened the position"],
      opening_risks: ["a standalone SELL GTT would open a short position"],
      operator_fallback: "close the position manually in the Upstox app, then run the AXIOM reconcile step",
    }
  : {
      routed: true,
      summary: "Alpaca documents plain close-side orders bounded by the held quantity.",
      closing_paths: ["a SELL order against a held long"],
      opening_risks: ["a SELL with no held position is refused by the naked-short guard"],
      operator_fallback: "close the position in the Alpaca dashboard",
    });

function readinessReport() {
  return {
    schema: "axiom.live_readiness.v1",
    generated_at: now(),
    markets: ["NSE", "NASDAQ"].map((mkt) => ({
      market: mkt,
      broker_label: mkt === "NSE" ? "Upstox (Protected Multi-Leg GTT v3)" : "Alpaca Trading API v2",
      mode: liveState[mkt].mode,
      gate_state: liveState[mkt].gate,
      ready: false,
      live_enabled: false,
      failed_checks: readinessFor(mkt).failed,
      credential_requirements: [
        { name: mkt === "NSE" ? "UPSTOX_LIVE_ACCESS_TOKEN" : "ALPACA_BROKER_LIVE_KEY_ID", configured: Boolean(liveState[mkt].limits) },
      ],
      exit_route: exitRouteFor(mkt),
      kill_switch: { engaged: liveState[mkt].kill, reason: null },
    })),
    all_markets_live_enabled: false,
    blocking_markets: ["NSE", "NASDAQ"],
    open_order_policy: {
      blocks_new_orders: true,
      scope: "the affected market only",
      working_orders: "left untouched unless the operator passes cancel_working=true",
      partial_fills: "a canceled partially filled order keeps its filled quantity",
      unresolved_intents: "must be reconciled before the market can submit again",
      reset: "requires an explicit operator acknowledgement and returns the gate to DISABLED",
    },
    notes: ["read-only"],
  };
}

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
  return {
    market: mkt, ok: failed.length === 0, checks, failed,
    missing_dependencies: [], gate: gatePayload(mkt),
    live_confirmation_phrase: PHRASE,
    documented_capabilities: { exit_route: exitRouteFor(mkt) },
  };
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
  if (url.pathname === "/api/markets/live/readiness" && method === "GET") {
    liveCalls.push({ method, path: url.pathname, body });
    return json(readinessReport());
  }
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

/* ==========================================================================
 * PAPER / LIVE switch suite
 *
 * Boots the real console and drives the per-market switch:
 *  - nothing is fetched or changed while the panel merely renders;
 *  - a blocked market's LIVE control is disabled and a click contacts nothing;
 *  - the Upstox exit limitation is rendered and gates Activate;
 *  - a passing market walks mode -> readiness re-read -> Arm -> ack + phrase -> Activate,
 *    one deliberate click per existing gate;
 *  - returning to PAPER revokes the permit without cancelling anything;
 *  - no order, strategy-start or session route is ever called.
 * ======================================================================== */

// NASDAQ starts with a complete limit set (so its readiness can pass); NSE does not.
liveState.NASDAQ.limits = {
  max_order_value: 5000, max_daily_orders: 10, max_daily_loss: 500, max_open_positions: 4,
  max_symbol_exposure: 20000, max_total_exposure: 50000, max_quote_age_seconds: 15,
  max_price_deviation_bps: 35,
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

const flush = async (rounds = 8) => {
  for (let i = 0; i < rounds; i += 1) await new Promise((r) => setTimeout(r, 0));
};

window.eval(code);
await flush(14);

const doc = window.document;
const $ = (id) => doc.getElementById(id);
const click = async (el) => { assert.ok(el, "Missing element"); el.click(); await flush(10); };
const type = (el, value) => { el.value = value; el.dispatchEvent(new window.Event("input", { bubbles: true })); };
const tick = (el, checked) => { el.checked = checked; el.dispatchEvent(new window.Event("change", { bubbles: true })); };
const writes = () => apiCalls.filter((c) => c.method !== "GET");

// 1. Rendering proves the state without contacting any live endpoint.
assert.ok($("mm-mode-switch-panel"), "the switch panel must exist");
assert.ok($("mm-switch-card-nse") && $("mm-switch-card-nasdaq"), "one card per market");
assert.equal(liveCalls.length, 0, "rendering the switch must not contact a live endpoint");
assert.equal($("mm-switch-state-nse").textContent, "NSE · PAPER");
assert.equal($("mm-switch-state-nasdaq").textContent, "NASDAQ · PAPER");
assert.ok($("mm-switch-live-nse").disabled, "LIVE is disabled until a readiness read passes");
assert.ok($("mm-switch-live-nasdaq").disabled, "LIVE is disabled until a readiness read passes");
assert.ok($("mm-switch-live-reason-nse").textContent.includes("run step 1"), $("mm-switch-live-reason-nse").textContent);
assert.ok($("mm-switch-exit-nse").textContent.includes("not read yet"));
assert.ok($("mm-switch-live-nasdaq").getAttribute("aria-pressed") === "false");
assert.equal($("mm-switch-paper-nse").getAttribute("aria-pressed"), "true");

// 2. A disabled LIVE control cannot contact the server at all.
await click($("mm-switch-live-nse"));
assert.equal(liveCalls.length, 0, "a blocked switch click must not reach the server");
assert.equal(writes().length, 0);

// 3. NSE readiness reports blockers and the missing exit route; LIVE stays disabled.
await click($("mm-switch-check-nse"));
assert.ok(liveCalls.some((c) => c.method === "GET" && c.path === "/api/markets/live/nse/readiness"));
assert.ok(liveCalls.some((c) => c.method === "GET" && c.path === "/api/markets/live/readiness"));
assert.equal(writes().length, 0, "reading readiness is GET-only");
assert.ok($("mm-switch-blockers-nse").textContent.includes("risk_limits_configured"), $("mm-switch-blockers-nse").textContent);
assert.ok($("mm-switch-blockers-nse").textContent.includes("All eight per-market risk limits"));
assert.ok($("mm-switch-blockers-nse").textContent.includes("configuration_complete"));
assert.ok($("mm-switch-exit-nse").textContent.includes("NO ROUTED EXIT"), $("mm-switch-exit-nse").textContent);
assert.ok($("mm-switch-exit-nse").textContent.includes("close the position manually in the Upstox app"));
assert.ok($("mm-switch-live-nse").disabled, "a failing check keeps the switch disabled");
assert.ok($("mm-switch-live-reason-nse").textContent.includes("LIVE is disabled by"), $("mm-switch-live-reason-nse").textContent);
assert.ok($("mm-switch-mode-nse").disabled, "the mode step stays locked while blocked");
assert.ok($("mm-switch-arm-nse").disabled);
assert.ok($("mm-switch-activate-nse").disabled);
await click($("mm-switch-mode-nse"));
assert.equal(liveCalls.filter((c) => c.path === "/api/markets/live/nse/mode").length, 0, "a blocked market never changes mode");
assert.equal($("mm-switch-state-nse").textContent, "NSE · PAPER");

// 4. NASDAQ readiness passes: the switch unlocks, but choosing LIVE changes nothing yet.
await click($("mm-switch-check-nasdaq"));
assert.ok(!$("mm-switch-live-nasdaq").disabled, "a passing check enables LIVE");
assert.ok($("mm-switch-exit-nasdaq").textContent.includes("Exit route: routed"), $("mm-switch-exit-nasdaq").textContent);
await click($("mm-switch-live-nasdaq"));
assert.equal(writes().length, 0, "choosing LIVE must not set a mode or arm a gate");
assert.ok($("mm-switch-output").textContent.includes("review only"), $("mm-switch-output").textContent);
assert.ok($("mm-switch-arm-nasdaq").disabled, "Arm is locked until mode=LIVE");
assert.ok($("mm-switch-activate-nasdaq").disabled);

// 5. Step 2 sets mode=LIVE only: the gate stays DISARMED.
await click($("mm-switch-mode-nasdaq"));
const modePosts = liveCalls.filter((c) => c.path === "/api/markets/live/nasdaq/mode");
assert.equal(modePosts.length, 1);
assert.deepEqual(modePosts[0].body, { mode: "LIVE" });
assert.equal($("mm-switch-state-nasdaq").textContent, "NASDAQ · LIVE");
assert.equal($("mm-switch-gate-nasdaq").textContent, "DISABLED");
assert.ok($("mm-switch-arm-nasdaq").disabled, "the mode change invalidates the readiness evidence: re-read first");
assert.ok($("mm-switch-live-reason-nasdaq").textContent.includes("not ARMED"), $("mm-switch-live-reason-nasdaq").textContent);

// 6. Fresh readiness read, then Arm, then the acknowledgement + phrase gates Activate.
await click($("mm-switch-check-nasdaq"));
assert.ok(!$("mm-switch-arm-nasdaq").disabled, "a fresh passing read unlocks Arm");
await click($("mm-switch-arm-nasdaq"));
assert.equal(liveCalls.filter((c) => c.path === "/api/markets/live/nasdaq/arm").length, 1);
assert.equal($("mm-switch-gate-nasdaq").textContent, "ARMED");
assert.ok($("mm-switch-activate-nasdaq").disabled, "Activate needs the acknowledgement and the phrase");
type($("mm-switch-phrase-nasdaq"), PHRASE);
assert.ok($("mm-switch-activate-nasdaq").disabled, "the phrase alone is not enough: the risk acknowledgement is required");
assert.ok($("mm-switch-activate-hint-nasdaq").textContent.includes("risk acknowledgement"));
type($("mm-switch-phrase-nasdaq"), "ACTIVATE LIVE");
tick($("mm-switch-ack-nasdaq"), true);
assert.ok($("mm-switch-activate-nasdaq").disabled, "the exact phrase is required");
assert.ok($("mm-switch-activate-hint-nasdaq").textContent.includes(PHRASE));
type($("mm-switch-phrase-nasdaq"), PHRASE);
assert.ok(!$("mm-switch-activate-nasdaq").disabled, "acknowledgement + exact phrase enable Activate");

// 7. Activate sends the existing request once and starts no strategy.
await click($("mm-switch-activate-nasdaq"));
const activateCalls = liveCalls.filter((c) => c.path === "/api/markets/live/nasdaq/activate");
assert.equal(activateCalls.length, 1, "one click, one activation");
assert.deepEqual(activateCalls[0].body, { confirm_live_trading: PHRASE, acknowledge_risk: true });
assert.equal($("mm-switch-state-nasdaq").textContent, "NASDAQ · LIVE · ACTIVE");
assert.equal($("mm-switch-gate-nasdaq").textContent, "ACTIVE");
assert.ok($("mm-switch-output").textContent.includes("did not start or resume a strategy"));
assert.equal($("mm-switch-phrase-nasdaq").value, "", "the phrase is cleared after activation");
assert.equal($("mm-switch-ack-nasdaq").checked, false);

// 8. NSE is untouched by everything above and stays blocked.
assert.equal($("mm-switch-state-nse").textContent, "NSE · PAPER");
assert.ok($("mm-switch-live-nse").disabled);
assert.ok($("mm-switch-exit-nse").textContent.includes("NO ROUTED EXIT"));
assert.equal(liveCalls.filter((c) => c.path.startsWith("/api/markets/live/nse/") && c.method === "POST").length, 0);

// 9. Returning to PAPER disarms/revokes through the same existing mode route.
await click($("mm-switch-paper-nasdaq"));
const modeCalls = liveCalls.filter((c) => c.path === "/api/markets/live/nasdaq/mode");
assert.equal(modeCalls.length, 2);
assert.deepEqual(modeCalls[1].body, { mode: "PAPER" });
assert.equal($("mm-switch-state-nasdaq").textContent, "NASDAQ · PAPER");
assert.equal($("mm-switch-gate-nasdaq").textContent, "DISABLED");
assert.ok($("mm-switch-output").textContent.includes("revoked the live permit"), $("mm-switch-output").textContent);
assert.ok($("mm-switch-output").textContent.includes("No working order and no protective exit was cancelled"));
assert.ok($("mm-switch-live-nasdaq").disabled, "LIVE needs a fresh readiness read after returning to PAPER");

// 10. Exactly four writes happened, all of them existing live-control routes.
assert.deepEqual(writes().map((c) => c.path), [
  "/api/markets/live/nasdaq/mode",
  "/api/markets/live/nasdaq/arm",
  "/api/markets/live/nasdaq/activate",
  "/api/markets/live/nasdaq/mode",
]);
assert.equal(writes().filter((c) => /order|cancel|close|exit|submit|session|cycle|resume|start/i.test(c.path)).length, 0,
  "the switch must never place, cancel or test-submit an order, nor start/resume a strategy");
assert.equal(apiCalls.filter((c) => c.path.startsWith("/api/markets/live/") && c.method === "POST" && !/(mode|arm|activate|disarm)$/.test(c.path)).length, 0);

console.log(`paper_live_switch_smoke (jsdom): PASS (${liveCalls.length} live API calls, ${writes().length} writes, 0 order endpoints)`);
window.close();
