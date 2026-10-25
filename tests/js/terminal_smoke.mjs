/* Headless interaction tests for the real operator terminal script.
 * jsdom supplies the DOM; all HTTP and canvas interfaces are local fakes.
 * No MetaTrader5 package, broker account, credential, or external network is used. */
import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { execFileSync } from "node:child_process";
import { arch, cpus, platform } from "node:os";
import { JSDOM } from "jsdom";

const root = resolve(import.meta.dirname, "../..");
const benchmarkOutput = process.env.AXIOM_UI_BENCHMARK_OUTPUT || "";
const benchmarkRenderIterations = 120;
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
function emptyMarketData() {
  return {
    provider: "VANTAGE_DEMO_MT5", state: "DISCONNECTED", detail: "Not connected; execution remains LOCAL PAPER",
    connected: false, authenticated: false, account: null, credential_profile: "VANTAGE_DEMO",
    execution_mode: "AXIOM_PAPER", read_only: true, orders_modified: false, automation_enabled: false,
    instrument_count: 0, supported_instrument_count: 0, selected_symbol: null, quote: null,
    max_quote_age_seconds: 30, stall_after_seconds: 15, live_trading: false,
  };
}
let marketData = emptyMarketData();
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
    paper_session: paper ? { state: "STOPPED", provider: "replay", timeframe: "1m" } : undefined,
    account: paper ? { currency: "USD", cash: 100000, balance: 100000, equity: 100000, margin_free: 100000 } : connected ? currentStatus.account : null,
    positions: [], orders: [], intents: [], deals: [], quotes: {},
    market_data: paper ? marketData : undefined,
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
  if (path === "/api/trading/performance") return response({
    unit: "milliseconds", sample_capacity_per_metric: 512,
    metrics: { "vantage.feed.gateway_receipt": { samples: 3, window_samples: 3, p50_ms: 1.2, p95_ms: 2.4, p99_ms: 2.8, max_ms: 3, last_ms: 1.1 } },
    boundaries: { "vantage.feed.gateway_receipt": "Fake local receipt measurement" },
    note: "Observed local-process timing only; no universal or zero-latency claim.",
    evidence: "No real DEMO or LIVE latency is established by fake-provider tests.",
  });
  if (path === "/api/listed-assets/status") return response({
    interface: "listed-assets-v1", identity_contract: "symbol + explicit exchange/MIC + currency",
    data_configured: false, execution_supported: false, mt5_routing_allowed: false,
    cfd_substitution_allowed: false, note: "No listed execution adapter is available.",
  });
  if (path === "/api/trading/workspace") return response(current);
  if (path === "/api/trading/activity?limit=300") return response({ events: [{ ts: now(), environment: current.status.mode, kind: "FAKE_AUDIT", payload: { safe: true } }] });
  if (path === "/api/trading/connection") return response({ status: current.status, profiles, secret_store: "Windows DPAPI · current Windows user", test_is_read_only: true });
  if (path === "/api/market-data/status") return response(marketData);
  if (path === "/api/market-data/connect") {
    if (current.status.mode !== "AXIOM_PAPER" || !profiles.VANTAGE_DEMO) return response({ detail: "Paper mode and a fake DEMO profile are required" }, 409);
    marketData = {
      ...emptyMarketData(), state: "CONNECTED", detail: "Discovered 3 account-specific fake MT5 instruments",
      connected: true, authenticated: true,
      account: { login_masked: "••••56", server: profiles.VANTAGE_DEMO.server, currency: "USD", account_trade_mode: "DEMO" },
      instrument_count: 3, supported_instrument_count: 2,
    };
    current.market_data = marketData;
    return response(marketData);
  }
  if (path === "/api/market-data/disconnect") {
    marketData = emptyMarketData(); current.market_data = marketData; current.quotes = {};
    return response(marketData);
  }
  if (path === "/api/trading/session") {
    selected = []; marketData = emptyMarketData(); current = makeWorkspace(body.mode, false); return response(current.status);
  }
  if (path === "/api/trading/credentials" && method === "PUT") {
    profiles[body.environment] = {
      login_masked: "••••56", server: body.server, terminal_path: body.terminal_path,
      label: body.label, updated_ts: now(),
    };
    if (body.environment === current.status.mode && current.status.mode !== "AXIOM_PAPER") {
      current.status.credential = profiles[body.environment]; current.status.server = body.server;
      current.status.account_login_masked = "••••56"; current.status.connection = { state: "DISCONNECTED", detail: "Credential saved; not connected" };
    }
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
  if (path.startsWith("/api/market-data/instruments?")) {
    const url = new URL(path, "http://fake.local");
    const supportedOnly = url.searchParams.get("supported_only") === "true";
    const instruments = [contract("EURUSD"), contract("AAPL", "SHARE_CFD"), contract("MYSTERY", "UNKNOWN")]
      .filter((item) => !supportedOnly || item.product_type !== "UNKNOWN");
    return response({
      instruments, total_matching: instruments.length, account_specific: true,
      hard_coded_universe: false, selected: marketData.selected_symbol,
      timeframes: ["1m", "5m", "15m", "30m", "1h", "4h", "1D"], provider: "VANTAGE_DEMO_MT5",
    });
  }
  if (path.startsWith("/api/market-data/chart?")) {
    const url = new URL(path, "http://fake.local");
    const symbol = url.searchParams.get("symbol") || "EURUSD";
    const product = symbol === "AAPL" ? "SHARE_CFD" : "FOREX";
    const instrument = { ...contract(symbol, product) };
    const currentQuote = { ...quote, symbol, timestamp: now(), received_at: now(), fresh: true, advancing: true, age_seconds: 0.01 };
    instrument.quote = currentQuote;
    marketData = { ...marketData, state: "CONNECTED", selected_symbol: symbol, quote: currentQuote, detail: "Fresh advancing fake VANTAGE DEMO quotes confirmed" };
    current.market_data = marketData; current.quotes = { [symbol]: currentQuote };
    return response({
      symbol, timeframe: url.searchParams.get("timeframe") || "1m", bars, quote: currentQuote,
      instrument, product_type: product, is_cfd: product === "SHARE_CFD", currency_profit: "USD", account_currency: "USD",
      volume_kind: "BROKER_TICK_VOLUME", source: "VANTAGE_DEMO_MT5", market_data: true, live: true,
      data_feed_label: "VANTAGE DEMO MT5 · FRESH READ-ONLY MARKET DATA · LOCAL PAPER FILLS",
      generated_at: now(), note: "Fake MT5 closed bars; no replay fallback.", paper_only: true,
    });
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
  if (path.startsWith("/paper/ticket?")) {
    const url = new URL(path, "http://fake.local"); const symbol = url.searchParams.get("symbol") || "AAPL";
    const mt5 = marketData.authenticated && marketData.selected_symbol === symbol;
    return response({
      symbol, last_price: 100, priced_at: now(), price_source: "last_closed_bar", cash: 100000,
      equity: 100000, position: null, max_buy_quantity: 100, max_sell_quantity: 0, blocked: null,
      entry_blocked: mt5 ? null : marketData.authenticated ? "Select and verify this exact MT5 symbol" : null,
      quote_source: mt5 ? "VANTAGE_DEMO_MT5" : null, quote_timestamp: mt5 ? now() : null,
      quote_received_at: mt5 ? now() : null, quote_id: mt5 ? "fake-closed-bar-id" : null,
    });
  }
  if (path === "/paper/order") {
    const order = { ...body, id: `paper-order-${current.orders.length + 1}`, status: "FILLED", timestamp: now() };
    current.orders.push(order); return response({ order, status: "FILLED", simulated: true, paper_only: true });
  }
  if (path === "/paper/start") {
    current.paper_session = {
      state: "RUNNING", provider: body.source, strategy: body.strategy, timeframe: body.timeframe,
      automation_enabled: false, processed_bars: 1,
    };
    return response({ ...current.paper_session, execution_mode: "AXIOM_PAPER" });
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
const percentile = (values, q) => {
  const ordered = [...values].sort((a, b) => a - b); const position = (ordered.length - 1) * q;
  const lower = Math.floor(position), upper = Math.min(lower + 1, ordered.length - 1);
  return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower);
};
const metricSummary = (values) => ({
  samples: values.length, p50_ms: Number(percentile(values, .5).toFixed(3)),
  p95_ms: Number(percentile(values, .95).toFixed(3)), p99_ms: Number(percentile(values, .99).toFixed(3)),
  max_ms: Number(Math.max(...values).toFixed(3)),
});

try {
  window.eval(`${code}\n;globalThis.__axiomTerminalTest = { state, drawChart, ticketSignature, orderFormValues, renderTicketEstimate };`);
  await settle(40);
  assert.equal(element("auth-gate").hidden, true, "saved local token unlocks the shell");
  assert.equal(element("app").hidden, false);
  assert.equal(element("global-mode").textContent, "LOCAL PAPER");
  assert.ok(calls.length > 0 && calls.every((call) => call.headers.Authorization === `Bearer ${TOKEN}`), "every API call uses the local bearer token");

  await navigate("charts");
  assert.equal(element("order-symbol").value, "AAPL");
  assert.match(element("chart-source").textContent, /NOT MARKET DATA/);
  assert.ok(draws.fillRect > 40 && draws.stroke > 40, "paper candles were drawn");
  assert.match(element("terminal-chart").getAttribute("aria-label"), /40 of 40 candles/);
  const chartOnlyCallCount = calls.length;
  await click("zoom-in");
  assert.match(element("terminal-chart").getAttribute("aria-label"), /32 of 40 candles/, "zoom changes only the visible chart window");
  await click("zoom-out");
  assert.match(element("terminal-chart").getAttribute("aria-label"), /40 of 40 candles/);
  await click("zoom-in");
  element("terminal-chart").dispatchEvent(new window.KeyboardEvent("keydown", { key: "ArrowLeft", bubbles: true })); await settle();
  assert.ok(window.eval("globalThis.__axiomTerminalTest.state.chartView.endOffset") > 0, "keyboard pans the visible chart window");
  await click("reset-chart-view");
  assert.equal(window.eval("globalThis.__axiomTerminalTest.state.chartView.endOffset"), 0);
  assert.match(element("terminal-chart").getAttribute("aria-label"), /40 of 40 candles/);
  element("chart-indicator").value = "SMA50";
  element("chart-indicator").dispatchEvent(new window.Event("change", { bubbles: true })); await settle();
  assert.match(element("terminal-chart").getAttribute("aria-label"), /SMA50/);
  element("drawing-tool").value = "HORIZONTAL";
  element("drawing-tool").dispatchEvent(new window.Event("change", { bubbles: true })); await settle();
  element("terminal-chart").dispatchEvent(new window.MouseEvent("pointerdown", { bubbles: true, clientX: 100, clientY: 100 })); await settle();
  assert.match(element("terminal-chart").getAttribute("aria-label"), /1 drawings/, "canvas click creates a drawing, not an order");
  await click("clear-drawings");
  assert.match(element("terminal-chart").getAttribute("aria-label"), /0 drawings/);
  element("drawing-tool").value = "TREND";
  element("drawing-tool").dispatchEvent(new window.Event("change", { bubbles: true }));
  element("terminal-chart").dispatchEvent(new window.MouseEvent("pointerdown", { bubbles: true, clientX: 80, clientY: 90 })); await settle();
  element("terminal-chart").dispatchEvent(new window.MouseEvent("pointerdown", { bubbles: true, clientX: 140, clientY: 120 })); await settle();
  assert.match(element("terminal-chart").getAttribute("aria-label"), /1 drawings/, "two clicks create one trend drawing");
  await click("clear-drawings");
  element("drawing-tool").value = "NONE";
  element("drawing-tool").dispatchEvent(new window.Event("change", { bubbles: true }));
  await click("crosshair-toggle");
  assert.equal(element("crosshair-toggle").getAttribute("aria-pressed"), "false");
  element("terminal-chart").dispatchEvent(new window.MouseEvent("pointermove", { bubbles: true, clientX: 100, clientY: 100 })); await settle();
  assert.equal(element("chart-cursor").textContent, "");
  await click("crosshair-toggle");
  element("terminal-chart").dispatchEvent(new window.MouseEvent("pointermove", { bubbles: true, clientX: 100, clientY: 100 })); await settle();
  assert.match(element("chart-cursor").textContent, /O .* H .* L .* C /, "crosshair exposes OHLC readout");
  assert.equal(calls.length, chartOnlyCallCount, "zoom, indicator, drawing and crosshair emit no API or order requests");
  if (benchmarkOutput) {
    window.eval(`const { state, drawChart } = globalThis.__axiomTerminalTest;
    for (let index = 0; index < ${benchmarkRenderIterations}; index += 1) {
      state.chartView.pointer = { x: 30 + (index % 120), y: 45 + (index % 80) };
      state.chartView.endOffset = index % 9;
      drawChart();
    }`);
    assert.equal(calls.length, chartOnlyCallCount, "render benchmark remains local and creates no API/order requests");
  }
  element("order-volume").value = "2";
  await submit("order-form");
  assert.equal(element("modal").hidden, false, "paper order has a review dialog");
  assert.match(element("modal-body").textContent, /LOCAL PAPER ONLY/);
  await click("modal-confirm");
  const paperOrder = calls.find((call) => call.path === "/paper/order");
  assert.equal(paperOrder.body.quantity, 2, "reviewed paper quantity is the submitted quantity");
  assert.equal(paperOrder.body.side, "BUY");
  assert.match(paperOrder.body.idempotency_key, /^paper-order-/, "paper review binds an idempotency key");

  await navigate("connections");
  assert.equal(element("mt5-login").disabled, false, "paper mode permits the DEMO-only protected credential form");
  assert.equal(element("test-connection").disabled, true, "execution connection test remains disabled in paper mode");
  element("mt5-login").value = "123456";
  element("mt5-password").value = FAKE_PASSWORD;
  element("mt5-server").value = "VantageMarkets-Demo";
  await submit("credential-form");
  assert.equal(calls.find((call) => call.path === "/api/trading/credentials").body.environment, "VANTAGE_DEMO");
  assert.equal(element("mt5-password").value, "", "password field clears after save");
  assert.equal(window.document.documentElement.innerHTML.includes(FAKE_PASSWORD), false, "password is never rendered");
  assert.equal(element("mt5-login").value, "", "full login is not retained in the form");
  assert.equal(element("global-mode").textContent, "LOCAL PAPER", "saving DEMO credentials never changes execution");
  await click("connect-market-data");
  assert.equal(marketData.authenticated, true);
  assert.equal(element("global-mode").textContent, "LOCAL PAPER", "quote connection never changes execution");
  assert.match(element("boundary-execution").textContent, /LOCAL PAPER/);
  assert.match(element("boundary-data").textContent, /VANTAGE DEMO · READ-ONLY/);
  assert.equal(current.status.active, false);
  assert.equal(calls.filter((call) => call.path === "/api/trading/orders/submit").length, 0);

  await navigate("charts");
  assert.equal(element("order-symbol").value, "AAPL", "the exact previously selected broker symbol is preserved");
  assert.match(element("chart-source").textContent, /READ-ONLY MARKET DATA/);
  element("chart-symbol").value = "AAPL";
  element("chart-symbol").dispatchEvent(new window.Event("change", { bubbles: true }));
  await settle();
  assert.equal(element("order-symbol").value, "AAPL");
  assert.match(element("chart-contract").textContent, /CFD · no underlying share\/ETF ownership/);
  assert.match(element("chart-contract").textContent, /Contract size/);
  assert.match(element("volume-label").textContent, /not MT5 lots/);
  await click("start-paper");
  const livePaperStart = calls.find((call) => call.path === "/paper/start");
  assert.equal(livePaperStart.body.source, "vantage_mt5");
  assert.equal(livePaperStart.body.symbol, "AAPL");
  assert.equal(livePaperStart.body.strategy, "manual_only");
  assert.equal(calls.filter((call) => call.path === "/paper/order").length, 1, "starting live prices creates no order");
  element("order-volume").value = "0.25";
  await submit("order-form");
  assert.match(element("modal-body").textContent, /VANTAGE_DEMO_MT5/);
  assert.match(element("modal-body").textContent, /Quote \/ bar ID/);
  await click("modal-confirm");
  const mt5PaperOrder = calls.filter((call) => call.path === "/paper/order").at(-1);
  assert.equal(mt5PaperOrder.body.symbol, "AAPL");
  assert.equal(mt5PaperOrder.body.quantity, 0.25);
  assert.match(mt5PaperOrder.body.idempotency_key, /^paper-order-/);

  await navigate("connections");
  element("session-mode").value = "VANTAGE_DEMO";
  await click("apply-mode");
  assert.equal(element("global-mode").textContent, "VANTAGE DEMO");
  assert.equal(current.status.active, false, "mode selection never activates");
  assert.equal(marketData.authenticated, false, "execution selection disconnects the independent feed");
  assert.equal(calls.filter((call) => call.path === "/api/trading/orders/submit").length, 0);
  await click("test-connection");
  assert.equal(current.status.connected, true);
  assert.equal(current.status.active, false, "read-only connection test never activates");
  assert.equal(calls.filter((call) => call.path === "/api/trading/orders/submit").length, 0);

  await navigate("instruments");
  assert.match(element("instrument-rows").textContent, /Apple share CFD/);
  assert.match(element("instrument-rows").textContent, /CFD, not underlying ownership/);
  assert.match(element("instrument-rows").textContent, /untradable/i);
  const watchCallCount = calls.length;
  window.document.querySelector('[data-watch-symbol="AAPL"]').click(); await settle();
  assert.equal(window.document.querySelector('[data-watch-symbol="AAPL"]').getAttribute("aria-pressed"), "true");
  assert.equal(window.document.querySelector('[data-select-symbol="AAPL"]').checked, false, "watching never selects execution");
  assert.match(element("watchlist-status").textContent, /1 watched symbol/);
  element("watchlist-only").checked = true;
  element("watchlist-only").dispatchEvent(new window.Event("change", { bubbles: true })); await settle();
  assert.match(element("instrument-rows").textContent, /AAPL/);
  assert.doesNotMatch(element("instrument-rows").textContent, /Euro vs US Dollar/);
  element("watchlist-only").checked = false;
  element("watchlist-only").dispatchEvent(new window.Event("change", { bubbles: true })); await settle();
  await click("watch-visible");
  assert.match(element("watchlist-status").textContent, /3 watched symbol/);
  await click("clear-watchlist");
  assert.match(element("watchlist-status").textContent, /0 watched symbol/);
  assert.equal(calls.length, watchCallCount, "watchlist edits and filters emit no selection, feed or order request");
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
  assert.equal(window.eval("globalThis.__axiomTerminalTest.ticketSignature(globalThis.__axiomTerminalTest.orderFormValues())"), window.eval("globalThis.__axiomTerminalTest.state.ticketReview?.signature"), "docked ticket must match the reviewed values");
  assert.match(element("ticket-estimate").textContent, /Server review passed/);
  assert.match(element("ticket-estimate").textContent, /10\.00 USD/, "docked ticket uses actual MT5 review margin");
  await phraseAndConfirm("SUBMIT DEMO ORDER");
  const submissions = calls.filter((call) => call.path === "/api/trading/orders/submit");
  assert.equal(submissions.length, 1);
  assert.equal(submissions[0].body.volume, 0.1);
  assert.equal(submissions[0].body.confirmation, "SUBMIT DEMO ORDER");
  window.eval(`const test = globalThis.__axiomTerminalTest;
    test.state.ticketReview.review.expires_at = new Date(Date.now() - 1000).toISOString();
    test.renderTicketEstimate();`);
  assert.match(element("ticket-estimate").textContent, /Not reviewed · no submission/, "expired review is not presented as current");
  window.eval(`const test = globalThis.__axiomTerminalTest;
    test.state.ticketReview.review.expires_at = new Date(Date.now() + 30000).toISOString();
    test.renderTicketEstimate();`);

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
  assert.equal(window.document.body.dataset.mode, "VANTAGE_LIVE");
  assert.match(element("boundary-execution").textContent, /VANTAGE LIVE · DISCONNECTED/);
  assert.equal(current.status.active, false, "selecting LIVE never infers activation");
  assert.equal(current.status.connected, false, "selecting LIVE never auto-connects");
  await navigate("activity");
  await navigate("settings");
  await click("reload-performance");
  assert.match(element("performance-summary").textContent, /vantage.feed.gateway_receipt/);
  assert.match(element("performance-summary").textContent, /p50/);
  assert.match(element("listed-asset-status").textContent, /execution supported: no/);
  assert.match(element("listed-asset-status").textContent, /substitution: prohibited/);
  await navigate("overview");
  assert.equal(calls.filter((call) => call.path === "/api/trading/orders/submit").length, 2, "no hidden order submission occurred");
  assert.equal(calls.some((call) => /^https?:/i.test(call.path)), false, "browser code makes only relative local API requests");
  if (benchmarkOutput) {
    const browser = window.eval("JSON.parse(JSON.stringify(globalThis.__axiomTerminalTest.state.performance.browser))");
    const report = {
      schema: "axiom-terminal-ui-benchmark-v1", generated_at: now(),
      commit: execFileSync("git", ["rev-parse", "HEAD"], { cwd: root, encoding: "utf8" }).trim(),
      working_tree_dirty_at_measurement: Boolean(execFileSync("git", ["status", "--porcelain"], { cwd: root, encoding: "utf8" }).trim()),
      conditions: {
        evidence_class: "FAKE DOM / FAKE CANVAS COMMANDS ONLY", node: process.version,
        platform: platform(), arch: arch(), logical_cpu_count: cpus().length, cpu_model: cpus()[0]?.model || "unknown",
        dom: "jsdom", canvas: "local command-counting fake 2D context", real_browser: false,
        gpu_or_raster_measured: false, external_demo_account: false, live_account_or_orders: false,
        render_iterations: benchmarkRenderIterations,
      },
      browser_metrics_ms: Object.fromEntries(Object.entries(browser).filter(([, values]) => values.length).map(([name, values]) => [name, metricSummary(values)])),
      functional_evidence: { authenticated_fake_api_calls: calls.length, canvas_calls: draws, expected_reviewed_order_submissions: 2, unexpected_order_submissions: 0 },
      limitations: [
        "This measures JavaScript and fake canvas command execution, not GPU/raster or human-perceived frame latency.",
        "The terminal's performance panel measures real browser canvas duration when an owner runs it locally.",
        "p50/p95/p99 apply only to this run; no universal or zero-latency claim is made.",
        "Fake DOM/canvas and any later DEMO results establish neither profitability nor LIVE readiness.",
      ],
    };
    writeFileSync(resolve(root, benchmarkOutput), `${JSON.stringify(report, null, 2)}\n`);
    console.log(`terminal UI benchmark: ${benchmarkOutput}`);
  }
  console.log(`terminal harness: OK (${calls.length} authenticated fake API calls, ${draws.fillRect} candle/background draw calls)`);
} catch (error) {
  console.error(`terminal harness: FAIL: ${error?.stack || error}`);
  process.exitCode = 1;
} finally {
  window.close();
}
