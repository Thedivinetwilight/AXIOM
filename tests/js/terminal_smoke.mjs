/* Headless interaction tests for the real operator terminal script.
 * jsdom supplies the DOM; all HTTP and canvas interfaces are local fakes.
 * No MetaTrader5 package, broker account, credential, or external network is used. */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { JSDOM } from "jsdom";

const root = resolve(import.meta.dirname, "../..");
const html = readFileSync(resolve(root, "app/static/terminal/index.html"), "utf8");
const code = readFileSync(resolve(root, "app/static/terminal/terminal.js"), "utf8");
const TOKEN = "ui-local-test-token";
const FAKE_PASSWORD = "fake-ui-password-not-a-credential";
const now = () => new Date().toISOString();
const RISK = {
  max_order_volume: 10, max_symbol_volume: 20, max_order_margin: 10000,
  max_total_margin: 20000, max_open_positions: 5, max_daily_loss: 1000,
  max_drawdown_pct: 20, max_trades_per_day: 10, max_margin_usage_pct: 80,
  max_spread_bps: 100, max_slippage_points: 10, max_quote_age_seconds: 30,
};
const maskedAccount = {
  login_masked: "••••56", server: "VantageMarkets-Demo", currency: "USD",
  balance: 10000, equity: 10000, margin: 0, margin_free: 9000, leverage: 100,
  trade_allowed: true, expert_allowed: true, account_trade_mode: "DEMO",
  connected: true, terminal_trade_allowed: true, timestamp: now(),
};
let profiles = { VANTAGE_DEMO: null, VANTAGE_LIVE: null };
let selected = [];
let current;
const calls = [];
const draws = { fillRect: 0, stroke: 0, fillText: 0 };

function status(mode, connected = false) {
  const paper = mode === "AXIOM_PAPER";
  const profile = profiles[mode];
  return {
    mode, broker: paper ? "AXIOM local paper" : "Vantage MT5",
    server: paper ? "AXIOM local only" : profile?.server || null,
    account: connected ? { ...maskedAccount, server: profile.server, account_trade_mode: mode === "VANTAGE_LIVE" ? "REAL" : "DEMO" } : null,
    account_login_masked: paper ? "LOCAL PAPER" : profile?.login_masked || null,
    account_label: profile?.label || null,
    connection: { state: paper ? "LOCAL" : connected ? "CONNECTED" : profile ? "DISCONNECTED" : "NOT_CONFIGURED", detail: paper ? "Independent local simulator" : connected ? "Verified fake MT5 connection" : "No current MT5 connection" },
    connected, lifecycle: "DISARMED", armed: false, active: false, paused: false,
    emergency_stop: false, automation_enabled: false, automation_available: false,
    selected_symbols: paper ? [] : [...selected], risk_limits: { ...RISK }, credential: paper ? null : profile,
    last_reconciled_at: connected ? now() : null, identity_fingerprint: connected ? "fake-identity" : null,
    paper_only: paper, live_trading: false, profitability_claim: "none",
  };
}
function makeWorkspace(mode, connected = false) {
  const paper = mode === "AXIOM_PAPER";
  const currentStatus = status(mode, connected);
  return {
    status: currentStatus,
    paper_session: paper ? { state: "STOPPED", provider: "replay" } : undefined,
    account: paper ? { currency: "USD", cash: 100000, balance: 100000, equity: 100000, margin_free: 100000 } : connected ? currentStatus.account : null,
    positions: [], orders: [], intents: [], deals: [], quotes: {},
    pnl: paper || connected ? { realized_visible_history: 0, unrealized: 0, history_window_days: 90 } : null,
    simulated: mode !== "VANTAGE_LIVE", paper_only: paper, live_trading: false,
    broker_state_available: paper || connected,
    reconciled_at: connected ? now() : null,
    note: connected ? "fake broker snapshot" : "No current broker snapshot; empty collections do not prove a flat account.",
  };
}
current = makeWorkspace("AXIOM_PAPER");

const contract = (symbol, product = "FOREX") => ({
  symbol, display_name: symbol === "AAPL" ? "Apple share CFD" : "Euro vs US Dollar",
  description: symbol === "AAPL" ? "Apple share CFD" : "Euro vs US Dollar",
  path: product === "SHARE_CFD" ? "CFD\\Shares\\US" : "Forex\\Major", exchange: null,
  product_type: product, is_cfd: product === "SHARE_CFD", currency_base: symbol.slice(0, 3),
  currency_profit: "USD", account_currency: "USD", volume_min: 0.01, volume_max: 100,
  volume_step: 0.01, volume_limit: 0, contract_size: 100000, tick_size: 0.0001,
  tick_value: 1, digits: 4, point: 0.0001, trade_mode: 4, trade_mode_label: "FULL",
  execution_mode: 2, filling_mode: 2, supported_filling_modes: ["IOC"], order_mode: 127,
  supports_market: true, supports_limit: true, supports_stop_loss: true, supports_take_profit: true,
  stops_level_points: 10, freeze_level_points: 0, margin_initial: 0, margin_maintenance: 0,
  swap_long: -0.1, swap_short: -0.2, sessions_available: false, next_session_transition: null,
  commission_estimate: null, commission_note: "Broker deals are authoritative.", visible: true,
  metadata_complete: product !== "UNKNOWN", metadata_issues: product === "UNKNOWN" ? ["product type unknown"] : [],
  tradable: product !== "UNKNOWN", selected: selected.includes(symbol),
  quote: { symbol, bid: 1.0998, ask: 1.1002, timestamp: now(), received_at: now() },
});
const bars = Array.from({ length: 40 }, (_, index) => ({
  timestamp: new Date(Date.now() - (40 - index) * 60000).toISOString(),
  open: 1.1 + index * 0.0001, high: 1.1002 + index * 0.0001,
  low: 1.0998 + index * 0.0001, close: 1.1001 + index * 0.0001, volume: 100 + index,
}));
const quote = { symbol: "EURUSD", bid: 1.0998, ask: 1.1002, last: 1.1, timestamp: now(), received_at: now() };

function payloadForReview(body, close = false) {
  return {
    review_id: close ? "close-review-0001" : "order-review-0001", expires_at: new Date(Date.now() + 30000).toISOString(),
    broker: "Vantage MT5", server: current.status.server, account: current.status.account,
    mode: current.status.mode, lifecycle: current.status.lifecycle, emergency_stop: false,
    instrument: contract(body.symbol), order: { ...body }, quote: { ...quote, symbol: body.symbol },
    risk: { allowed: true, issues: [], quote_age_seconds: 0.1, spread_bps: 3.6, estimated_margin: 10 },
    precheck: { retcode: 0, comment: "fake precheck passed" }, risk_reducing: close,
    confirmation_phrase: close ? "CLOSE DEMO POSITION" : "SUBMIT DEMO ORDER",
    note: "Read-only fake review; final submission repeats checks.",
  };
}
function response(payload, statusCode = 200) {
  return { ok: statusCode >= 200 && statusCode < 300, status: statusCode, json: async () => payload };
}
function updateLifecycle(lifecycle) {
  current.status.lifecycle = lifecycle;
  current.status.armed = ["ARMED", "ACTIVE", "PAUSED"].includes(lifecycle);
  current.status.active = lifecycle === "ACTIVE";
  current.status.paused = lifecycle === "PAUSED";
  current.status.live_trading = current.status.mode === "VANTAGE_LIVE" && lifecycle === "ACTIVE";
}

async function fetchFake(url, options = {}) {
  const path = String(url); const method = options.method || "GET";
  const body = options.body ? JSON.parse(options.body) : null;
  calls.push({ path, method, body, headers: options.headers || {} });
  if (path === "/api/trading/status") return response(current.status);
  if (path === "/api/trading/workspace") return response(current);
  if (path === "/api/trading/activity?limit=300") return response({ events: [{ ts: now(), environment: current.status.mode, kind: "FAKE_AUDIT", payload: { safe: true } }] });
  if (path === "/api/trading/connection") return response({ status: current.status, profiles, secret_store: "Windows DPAPI · current Windows user", test_is_read_only: true });
  if (path === "/api/trading/session") {
    selected = []; current = makeWorkspace(body.mode, false); return response(current.status);
  }
  if (path === "/api/trading/credentials" && method === "PUT") {
    profiles[body.environment] = {
      login_masked: "••••56", server: body.server, terminal_path: body.terminal_path,
      label: body.label, updated_ts: now(),
    };
    current.status.credential = profiles[body.environment]; current.status.server = body.server;
    current.status.account_login_masked = "••••56"; current.status.connection = { state: "DISCONNECTED", detail: "Credential saved; not connected" };
    return response(current.status);
  }
  if (path === "/api/trading/connection/test") {
    const mode = current.status.mode; current = makeWorkspace(mode, true);
    return response({ ...current.status, read_only: true, orders_modified: false, instruments_discovered: 3, reconciled_at: now() });
  }
  if (path.startsWith("/api/trading/instruments?")) return response({
    instruments: [contract("EURUSD"), contract("AAPL", "SHARE_CFD"), contract("MYSTERY", "UNKNOWN")],
    total_matching: 3, selected, account_specific: true,
  });
  if (path === "/api/trading/instruments/selection") {
    selected = [...body.symbols]; current.status.selected_symbols = [...selected]; updateLifecycle("DISARMED");
    return response({ selected, status: current.status });
  }
  if (path === "/api/trading/risk") { current.status.risk_limits = { ...body }; updateLifecycle("DISARMED"); return response({ risk_limits: current.status.risk_limits, status: current.status }); }
  if (path === "/api/trading/readiness") return response({
    ready: true, checks: [{ key: "fake", passed: true, label: "Fake readiness", detail: "All fake checks passed" }],
    blockers: [], readiness_id: "readiness-challenge-0001", generated_at: now(),
    arm_phrase: "ARM VANTAGE DEMO", status: current.status,
  });
  if (path === "/api/trading/arm") { updateLifecycle("ARMED"); return response({ status: current.status, activation_id: "activation-challenge-0001", activation_phrase: "ACTIVATE VANTAGE DEMO", orders_submitted: 0 }); }
  if (path === "/api/trading/activate") { updateLifecycle("ACTIVE"); return response({ status: current.status, orders_submitted: 0 }); }
  if (path === "/api/trading/pause") { updateLifecycle("PAUSED"); return response({ status: current.status, activation_id: "activation-challenge-0002", activation_phrase: "ACTIVATE VANTAGE DEMO" }); }
  if (path === "/api/trading/stop") { updateLifecycle("STOPPED"); return response({ status: current.status, note: "Fake broker remainder reported." }); }
  if (path === "/api/trading/emergency") { updateLifecycle("STOPPED"); current.status.emergency_stop = true; return response({ status: current.status, note: "Durable fake stop set." }); }
  if (path === "/api/trading/emergency/clear") { updateLifecycle("DISARMED"); current.status.emergency_stop = false; return response(current.status); }
  if (path === "/api/trading/reconcile") return response({ ...current, unresolved_intents: 0 });
  if (path === "/api/trading/connection/disconnect") { current = makeWorkspace(current.status.mode, false); return response(current.status); }
  if (path === "/api/trading/orders/review") return response(payloadForReview(body, Boolean(body.position_id)));
  if (path === "/api/trading/orders/submit") {
    const intent = {
      id: body.idempotency_key, symbol: body.symbol, side: body.side, order_type: body.order_type,
      volume: body.volume, status: "FILLED", broker_order: "1001", reason: "fake broker deals confirm full execution",
      created_ts: now(), confirmed_ts: now(), request: body.position_id ? { position: Number(body.position_id) } : {},
    };
    current.intents.unshift(intent);
    if (body.position_id) current.positions = [];
    else current.positions = [{ position_id: "1001", symbol: body.symbol, side: body.side, volume: body.volume, price_open: 1.1002, price_current: 1.1, profit: 0, swap: 0, stop_loss: body.stop_loss, take_profit: body.take_profit, managed: true }];
    return response({ duplicate: false, submission_attempted: true, order: intent, reconciled: true });
  }
  if (path === "/api/trading/positions/modify/review") return response({
    review_id: "modify-review-0001", expires_at: new Date(Date.now() + 30000).toISOString(),
    server: current.status.server, account: current.status.account, mode: current.status.mode,
    lifecycle: current.status.lifecycle, position: current.positions[0], instrument: contract(current.positions[0].symbol),
    proposed: body, quote, risk: { allowed: true, issues: [] }, confirmation_phrase: "MODIFY DEMO POSITION",
    note: "Read-only fake modification review.",
  });
  if (path === "/api/trading/positions/modify/submit") {
    current.positions[0].stop_loss = body.stop_loss; current.positions[0].take_profit = body.take_profit;
    const intent = { id: body.idempotency_key, symbol: current.positions[0].symbol, side: current.positions[0].side, order_type: "MODIFY_SLTP", volume: current.positions[0].volume, status: "MODIFIED", reason: "fake broker position confirms SL/TP", created_ts: now(), confirmed_ts: now() };
    current.intents.unshift(intent); return response({ duplicate: false, submission_attempted: true, order: intent, reconciled: true });
  }
  if (path === "/chart/symbols") return response({ symbols: [{ symbol: "AAPL", timeframes: ["1m", "5m"], live: true }], default: "AAPL", timeframes: ["1m", "5m"] });
  if (path.startsWith("/chart/data?")) return response({
    symbol: "AAPL", timeframe: "1m", bars: {
      time: bars.map((bar) => bar.timestamp), open: bars.map((bar) => bar.open), high: bars.map((bar) => bar.high),
      low: bars.map((bar) => bar.low), close: bars.map((bar) => bar.close), volume: bars.map((bar) => bar.volume),
    },
    data_feed_label: "DEMO REPLAY SAMPLE DATA · NOT MARKET DATA", source_label: "fake paper feed",
    market_data: false, paper_only: true, generated_at: now(), note: "Fake local paper chart.",
  });
  if (path.startsWith("/api/trading/chart?")) return response({
    symbol: "EURUSD", timeframe: "1m", bars, quote, product_type: "FOREX", is_cfd: false,
    source: "VANTAGE_MT5", market_data: true, data_feed_label: "VANTAGE_DEMO · SAME MT5 ACCOUNT AS EXECUTION",
    generated_at: now(), note: "Fake MT5 bars.",
  });
  if (path.startsWith("/paper/ticket?")) return response({
    symbol: "AAPL", last_price: 100, priced_at: now(), price_source: "last_closed_bar", cash: 100000,
    equity: 100000, position: null, max_buy_quantity: 100, max_sell_quantity: 0, blocked: null,
  });
  if (path === "/paper/order") {
    const order = { ...body, id: "paper-order-1", status: "FILLED", timestamp: now() };
    current.orders.push(order); return response({ order, status: "FILLED", simulated: true, paper_only: true });
  }
  if (path === "/api/paper/start-default" || path === "/paper/stop" || path === "/paper/pause" || path === "/paper/resume") return response({ state: path === "/paper/stop" ? "STOPPED" : path === "/paper/pause" ? "PAUSED" : "RUNNING" });
  return response({ detail: `Unhandled fake route: ${method} ${path}` }, 404);
}

const dom = new JSDOM(html, { url: "http://127.0.0.1:8000/terminal", runScripts: "outside-only", pretendToBeVisual: true });
const { window } = dom;
window.sessionStorage.setItem("axiom-api-token", TOKEN);
window.fetch = fetchFake;
window.ResizeObserver = class { constructor(callback) { this.callback = callback; } observe() {} disconnect() {} };
window.HTMLElement.prototype.scrollIntoView = function () {};
window.URL.createObjectURL = () => "blob:fake";
window.URL.revokeObjectURL = () => {};
window.HTMLAnchorElement.prototype.click = function () {};
const context = {
  setTransform() {}, clearRect() {}, fillRect() { draws.fillRect += 1; }, beginPath() {}, moveTo() {}, lineTo() {},
  stroke() { draws.stroke += 1; }, fillText() { draws.fillText += 1; },
  set fillStyle(value) {}, set strokeStyle(value) {}, set lineWidth(value) {}, set font(value) {},
  set textBaseline(value) {}, set textAlign(value) {},
};
window.HTMLCanvasElement.prototype.getContext = () => context;

const settle = async (rounds = 20) => { for (let index = 0; index < rounds; index += 1) await new Promise((resolvePromise) => setTimeout(resolvePromise, 0)); };
const element = (id) => window.document.getElementById(id);
const click = async (id) => { assert.equal(element(id).disabled, false, `${id} must be enabled`); element(id).click(); await settle(); };
const navigate = async (page) => { window.document.querySelector(`[data-page="${page}"]`).click(); await settle(); };
const submit = async (id) => { element(id).dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true })); await settle(); };
const phraseAndConfirm = async (phrase) => { element("modal-phrase").value = phrase; await click("modal-confirm"); };

try {
  window.eval(code);
  await settle(40);
  assert.equal(element("auth-gate").hidden, true, "saved local token unlocks the shell");
  assert.equal(element("app").hidden, false);
  assert.equal(element("global-mode").textContent, "AXIOM PAPER");
  assert.ok(calls.length > 0 && calls.every((call) => call.headers.Authorization === `Bearer ${TOKEN}`), "every API call uses the local bearer token");

  await navigate("charts");
  assert.equal(element("order-symbol").value, "AAPL");
  assert.match(element("chart-source").textContent, /NOT MARKET DATA/);
  assert.ok(draws.fillRect > 40 && draws.stroke > 40, "paper candles were drawn");
  element("order-volume").value = "2";
  await submit("order-form");
  assert.equal(element("modal").hidden, false, "paper order has a review dialog");
  assert.match(element("modal-body").textContent, /LOCAL PAPER ONLY/);
  await click("modal-confirm");
  const paperOrder = calls.find((call) => call.path === "/paper/order");
  assert.equal(paperOrder.body.quantity, 2, "reviewed paper quantity is the submitted quantity");
  assert.equal(paperOrder.body.side, "BUY");

  await navigate("connections");
  assert.equal(element("mt5-login").disabled, true, "broker credential form is disabled in paper mode");
  element("session-mode").value = "VANTAGE_DEMO";
  await click("apply-mode");
  assert.equal(element("global-mode").textContent, "VANTAGE DEMO");
  assert.equal(current.status.active, false, "mode selection never activates");
  assert.equal(calls.filter((call) => call.path === "/api/trading/orders/submit").length, 0);

  await navigate("connections");
  element("mt5-login").value = "123456";
  element("mt5-password").value = FAKE_PASSWORD;
  element("mt5-server").value = "VantageMarkets-Demo";
  await submit("credential-form");
  assert.equal(element("mt5-password").value, "", "password field clears after save");
  assert.equal(window.document.documentElement.innerHTML.includes(FAKE_PASSWORD), false, "password is never rendered");
  assert.equal(element("mt5-login").value, "", "full login is not retained in the form");
  await click("test-connection");
  assert.equal(current.status.connected, true);
  assert.equal(current.status.active, false, "read-only connection test never activates");
  assert.equal(calls.filter((call) => call.path === "/api/trading/orders/submit").length, 0);

  await navigate("instruments");
  assert.match(element("instrument-rows").textContent, /Apple share CFD/);
  assert.match(element("instrument-rows").textContent, /CFD, not underlying ownership/);
  assert.match(element("instrument-rows").textContent, /untradable/i);
  const select = window.document.querySelector('[data-select-symbol="EURUSD"]');
  select.checked = true; select.dispatchEvent(new window.Event("change", { bubbles: true }));
  await click("save-instruments");
  assert.deepEqual(calls.find((call) => call.path === "/api/trading/instruments/selection").body.symbols, ["EURUSD"]);

  await navigate("risk");
  await click("run-readiness");
  await click("arm-session");
  await phraseAndConfirm("ARM VANTAGE DEMO");
  assert.equal(current.status.lifecycle, "ARMED");
  assert.equal(calls.filter((call) => call.path === "/api/trading/orders/submit").length, 0, "Arm submits no order");
  await click("activate-session");
  await phraseAndConfirm("ACTIVATE VANTAGE DEMO");
  assert.equal(current.status.active, true);
  assert.equal(calls.filter((call) => call.path === "/api/trading/orders/submit").length, 0, "Activate submits no order");

  await navigate("charts");
  assert.equal(element("order-symbol").value, "EURUSD");
  assert.match(element("chart-source").textContent, /SAME MT5 ACCOUNT AS EXECUTION/);
  element("order-volume").value = "0.1";
  await submit("order-form");
  assert.match(element("modal-body").textContent, /VANTAGE_DEMO/);
  await phraseAndConfirm("SUBMIT DEMO ORDER");
  const submissions = calls.filter((call) => call.path === "/api/trading/orders/submit");
  assert.equal(submissions.length, 1);
  assert.equal(submissions[0].body.volume, 0.1);
  assert.equal(submissions[0].body.confirmation, "SUBMIT DEMO ORDER");

  await navigate("orders");
  await click("reconcile-now");
  const modifyButton = window.document.querySelector("[data-modify]");
  assert.ok(modifyButton, "confirmed position exposes modification control");
  modifyButton.click(); await settle();
  element("modify-sl").value = "1.09"; element("modify-tp").value = "1.12";
  await click("modal-confirm");
  assert.match(element("modal-body").textContent, /Proposed SL \/ TP/);
  await phraseAndConfirm("MODIFY DEMO POSITION");
  assert.equal(calls.filter((call) => call.path === "/api/trading/positions/modify/submit").length, 1);

  const closeButton = window.document.querySelector("[data-close]");
  assert.ok(closeButton, "confirmed position exposes close control");
  closeButton.click(); await settle();
  await phraseAndConfirm("CLOSE DEMO POSITION");
  assert.equal(calls.filter((call) => call.path === "/api/trading/orders/submit").length, 2);
  assert.equal(calls.filter((call) => call.path === "/api/trading/orders/submit")[1].body.position_id, "1001");

  await click("emergency-stop");
  assert.equal(element("modal").hidden, false);
  await click("modal-confirm");
  assert.equal(current.status.emergency_stop, true);
  assert.equal(element("global-lifecycle").textContent, "EMERGENCY STOP");
  await navigate("risk");
  await click("clear-emergency");
  await phraseAndConfirm("CLEAR EMERGENCY STOP");
  assert.equal(current.status.emergency_stop, false);
  assert.equal(current.status.active, false, "clearing emergency never reactivates");

  element("session-mode").value = "VANTAGE_LIVE";
  await click("apply-mode");
  assert.equal(element("global-mode").textContent, "VANTAGE LIVE");
  assert.equal(current.status.active, false, "selecting LIVE never infers activation");
  assert.equal(current.status.connected, false, "selecting LIVE never auto-connects");
  await navigate("activity");
  await navigate("settings");
  await navigate("overview");
  assert.equal(calls.filter((call) => call.path === "/api/trading/orders/submit").length, 2, "no hidden order submission occurred");
  assert.equal(calls.some((call) => /^https?:/i.test(call.path)), false, "browser code makes only relative local API requests");
  console.log(`terminal harness: OK (${calls.length} authenticated fake API calls, ${draws.fillRect} candle/background draw calls)`);
} catch (error) {
  console.error(`terminal harness: FAIL: ${error?.stack || error}`);
  process.exitCode = 1;
} finally {
  window.close();
}
