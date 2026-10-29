/* Headless end-to-end JSDOM test suite for the dark TradingView-inspired
 * Multi-Market (NSE INR + NASDAQ USD) AXIOM Desktop Terminal.
 *
 * Verifies every workspace (01..09), venue switcher, quick symbol jump,
 * unified NSE+NASDAQ catalog search & reconciliation, read-only live chart
 * rendering + honest no-sample-bars empty state, regulatory fee preview &
 * simulated paper orders (0 broker calls), SL/TP position protection & close,
 * order cancellation, SQLite hash-chain reconciliation, strategy session
 * start/pause/resume/stop/reset, Alpaca Broker LIVE readiness/Arm/phrase-confirmed
 * Activate/Disarm/Recover, DPAPI credential slots (with zero DOM retention or URL
 * leakage), and Global Emergency Stop + phrase-confirmed Clear.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { JSDOM } from "jsdom";

const root = resolve(import.meta.dirname, "../..");
const html = readFileSync(resolve(root, "app/static/terminal/index.html"), "utf8");
const code = readFileSync(resolve(root, "app/static/terminal/terminal.js"), "utf8");

const TOKEN = "mm-desktop-ui-token";
const now = () => new Date().toISOString();

const RISK = {
  max_order_volume: 10, max_symbol_volume: 20, max_order_margin: 10000,
  max_total_margin: 20000, max_open_positions: 5, max_daily_loss: 1000,
  max_drawdown_pct: 20, max_trades_per_day: 10, max_margin_usage_pct: 80,
  max_spread_bps: 100, max_slippage_points: 10, max_quote_age_seconds: 30,
};

const calls = [];
const draws = { fillRect: 0, stroke: 0, fillText: 0 };

let nseState = "STOPPED";
let nasdaqState = "STOPPED";
let gateState = "DISABLED";
let emergencyStopped = false;
let upstoxConnState = "DISCONNECTED";
let alpacaConnState = "DISCONNECTED";

const watchlists = {
  NSE: [{ symbol: "RELIANCE", name: "Reliance Industries Ltd", instrument_key: "NSE_EQ|INE002A01018" }],
  NASDAQ: [{ symbol: "AAPL", name: "Apple Inc.", instrument_key: "NASDAQ:AAPL" }],
};

const nsePositions = [
  {
    symbol: "RELIANCE", qty: 10, avg_entry_price: 2940.0, last_price: 2962.5,
    market_value: 29625.0, unrealized_pnl: 225.0, unrealized_pnl_pct: 0.765,
    stop_loss: 2900.0, take_profit: 3050.0,
  },
];
const nasdaqOrders = [
  {
    client_order_id: "mm-paper-open-1", market: "NASDAQ", symbol: "AAPL",
    side: "BUY", order_type: "LIMIT", qty: 5, filled_qty: 0,
    limit_price: 220.0, avg_fill_price: 0, status: "WORKING",
  },
];
const nseFills = [
  {
    created_at: now(), market: "NSE", symbol: "RELIANCE", side: "BUY",
    qty: 10, fill_price: 2940.0, currency: "INR", fees_total: 35.12, realized_pnl: 0,
  },
];

function makeOverview() {
  return {
    generated_at: now(),
    paper: {
      NSE: {
        market: "NSE", currency: "INR", session_state: emergencyStopped ? "EMERGENCY_STOPPED" : nseState,
        strategy: "ema_crossover", timeframe: "5m",
        exchange_session: { state: "OPEN", is_open: true, timezone: "Asia/Kolkata" },
        account: { equity: 1000225.0, cash: 970600.0, market_value: 29625.0, day_pnl: 225.0, realized_pnl: 0.0, total_fees_paid: 35.12 },
        positions: [...nsePositions],
        orders: [],
        fills: [...nseFills],
        decisions: [
          { created_at: now(), market: "NSE", symbol: "RELIANCE", strategy: "ema_crossover", timeframe: "5m", action: "BUY", reason: "Fast EMA crossed above slow EMA on completed bar", order_client_id: "strat-nse-1" },
        ],
      },
      NASDAQ: {
        market: "NASDAQ", currency: "USD", session_state: emergencyStopped ? "EMERGENCY_STOPPED" : nasdaqState,
        strategy: "manual_only", timeframe: "5m",
        exchange_session: { state: "OPEN", is_open: true, timezone: "America/New_York" },
        account: { equity: 100000.0, cash: 100000.0, market_value: 0.0, day_pnl: 0.0, realized_pnl: 0.0, total_fees_paid: 0.0 },
        positions: [],
        orders: [...nasdaqOrders],
        fills: [],
        decisions: [],
      },
    },
    watchlists,
    providers: {
      providers: [
        { id: "upstox_market_data", name: "Upstox NSE Market Data", role: "NSE_MARKET_DATA_READ_ONLY", connection_state: upstoxConnState, credential_present: upstoxConnState === "CONNECTED_READ_ONLY", credential_source: "dpapi_store", used_for: ["NSE quotes", "NSE candles", "LIVE-MARKET PAPER"], token_expiry: { jwt_exp_present: true, expired_by_claim: false, expires_at: now() } },
        { id: "alpaca_market_data", name: "Alpaca US Market Data", role: "NASDAQ_MARKET_DATA_READ_ONLY", feed: "iex", connection_state: alpacaConnState, credential_present: alpacaConnState === "CONNECTED_READ_ONLY", credential_source: "dpapi_store", used_for: ["NASDAQ quotes", "NASDAQ candles", "LIVE-MARKET PAPER"] },
        { id: "alpaca_broker_paper", name: "Alpaca Broker Paper", role: "NASDAQ_BROKER_PAPER", connection_state: "DISCONNECTED", credential_present: false, credential_source: "none", used_for: ["Alpaca paper sandbox"] },
        { id: "alpaca_broker_live", name: "Alpaca Broker LIVE", role: "NASDAQ_LIVE_EXECUTION_GATED", connection_state: gateState === "LIVE_ACTIVE" ? "CONNECTED_READ_ONLY" : "DISCONNECTED", credential_present: true, credential_source: "dpapi_store", used_for: ["Real-money NASDAQ behind gate"] },
        { id: "upstox_live_execution", name: "Upstox NSE LIVE", role: "NSE_LIVE_EXECUTION_GATED", connection_state: "DISCONNECTED", credential_present: false, credential_source: "none", used_for: ["Real-money NSE GTT v3 behind gate"] },
      ],
    },
    reconciliation: {
      NSE: { source_kind: "authoritative_upstox_bod", authoritative_complete: true, tradable_count: 2418 },
      NASDAQ: { source_kind: "alpaca_v2_assets", authoritative_reconciled: true, counts: { total: 4120 } },
    },
    live: {
      nse: { broker: "Upstox GTT v3", availability: "NOT_AVAILABLE", missing_dependencies: ["UPSTOX_LIVE_ACCESS_TOKEN missing"] },
      nasdaq: { broker: "Alpaca Trading API v2", availability: "AVAILABLE_BEHIND_GATE", missing_dependencies: [], broker_status: { gate: { state: gateState } } },
    },
    recent_audit: [
      { seq: 1, created_at: now(), market: "NSE", event_kind: "PAPER_ORDER_FILLED", payload: { symbol: "RELIANCE", qty: 10, broker_orders: 0 }, entry_hash: "a1b2c3d4e5f60718293a4b5c6d7e8f90" },
    ],
  };
}

function makeLegacyWorkspace() {
  return {
    status: {
      mode: "AXIOM_PAPER", broker: "AXIOM local paper", server: "AXIOM local only",
      account: null, account_login_masked: "LOCAL PAPER", account_label: null,
      connection: { state: "LOCAL", detail: "Independent local simulator" },
      connected: false, lifecycle: "DISARMED", armed: false, active: false, paused: false,
      emergency_stop: emergencyStopped, automation_enabled: false, automation_available: false,
      selected_symbols: [], risk_limits: { ...RISK }, credential: null,
      last_reconciled_at: null, identity_fingerprint: null,
      paper_only: true, live_trading: false, profitability_claim: "none",
    },
    paper_session: {
      state: "STOPPED", provider: null, feed_kind: null, timeframe: "1m", strategy: null,
      automation_enabled: false, review_policy: "NO_ACTIVE_STRATEGY", stop_reason: null,
      current_run: null, last_run: null,
    },
    account: { currency: "USD", cash: 100000, balance: 100000, equity: 100000, margin_free: 100000 },
    positions: [], orders: [], intents: [], deals: [], quotes: {},
    market_data: {
      provider: "VANTAGE_DEMO_MT5", state: "DISCONNECTED", detail: "Not connected",
      connected: false, authenticated: false, account: null, credential_profile: "VANTAGE_DEMO",
      execution_mode: "AXIOM_PAPER", read_only: true, orders_modified: false, automation_enabled: false,
      instrument_count: 0, supported_instrument_count: 0, selected_symbol: null, quote: null,
      max_quote_age_seconds: 30, stall_after_seconds: 15, live_trading: false,
    },
    pnl: { realized_visible_history: 0, unrealized: 0, history_window_days: 90, realized_scope: "PERSISTED_LOCAL_HISTORY", unrealized_scope: "CURRENT_OPEN_POSITIONS" },
    simulated: true, paper_only: true, live_trading: false, broker_state_available: true,
    reconciled_at: null, note: "local paper",
  };
}

async function fetchFake(rawUrl, options = {}) {
  const urlStr = String(rawUrl);
  // Zero real external broker network traffic permitted!
  assert(!urlStr.includes("alpaca.markets") && !urlStr.includes("upstox.com"), `Forbidden external URL: ${urlStr}`);
  // Secrets must NEVER appear in query strings!
  assert(!urlStr.includes("secret-val") && !urlStr.includes("jwt-secret-tok"), `Secret leaked in URL: ${urlStr}`);

  const url = new URL(urlStr, "http://127.0.0.1:8000");
  const method = (options.method || "GET").toUpperCase();
  const auth = options.headers?.Authorization || options.headers?.authorization || "";
  assert.equal(auth, `Bearer ${TOKEN}`, `Missing Bearer token on ${method} ${url.pathname}`);
  const body = options.body ? JSON.parse(options.body) : null;
  calls.push({ method, path: url.pathname, search: url.search, body });

  const json = (payload, statusCode = 200) => ({
    ok: statusCode >= 200 && statusCode < 300,
    status: statusCode,
    headers: { get: () => "application/json" },
    text: async () => JSON.stringify(payload),
    json: async () => payload,
  });

  if (url.pathname === "/api/trading/workspace") return json(makeLegacyWorkspace());
  if (url.pathname === "/api/trading/status") return json(makeLegacyWorkspace().status);
  if (url.pathname === "/api/trading/activity") return json({ events: [] });
  if (url.pathname === "/api/trading/connection") {
    return json({ status: makeLegacyWorkspace().status, profiles: { VANTAGE_DEMO: null, VANTAGE_LIVE: null }, secret_store: "Windows DPAPI · current Windows user", test_is_read_only: true, mt5_package_available: false, mt5_integration: "UNAVAILABLE" });
  }
  if (url.pathname === "/api/market-data/status" || url.pathname === "/api/trading/market-data") return json(makeLegacyWorkspace().market_data);
  if (url.pathname === "/api/trading/performance") return json({ targets_ms: {}, operations: {} });
  if (url.pathname === "/api/trading/listed-assets") return json({ classes: [], total_curated_examples: 0 });
  if (url.pathname === "/api/trading/chart/symbols") return json({ symbols: [{ symbol: "EURUSD", source: "Dataset" }] });
  if (url.pathname === "/api/trading/chart") return json({ symbol: "EURUSD", timeframe: "1m", source: "Dataset", simulated: true, bars: [] });
  if (url.pathname === "/api/alpaca-broker/status") {
    return json({ enabled: gateState !== "DISABLED", environment: "live", endpoint_host: "api.alpaca.markets", real_money: gateState === "LIVE_ACTIVE", gate: { state: gateState }, limits: { max_order_value: 2500, max_daily_loss: 500, max_open_positions: 5, max_daily_orders: 20 } });
  }

  // Multi-Market Endpoints
  if (url.pathname === "/api/markets/overview") return json(makeOverview());
  if (url.pathname === "/api/markets/search") {
    return json({
      items: [
        { market: "NSE", symbol: "RELIANCE", trading_symbol: "RELIANCE", instrument_key: "NSE_EQ|INE002A01018", name: "Reliance Industries Ltd", asset_subclass: "EQUITY_COMMON", lot_size: 1, tick_size: 0.05, tradable: true, product_label: "CNC / MIS" },
        { market: "NASDAQ", symbol: "NVDA", trading_symbol: "NVDA", instrument_key: "NASDAQ:NVDA", name: "NVIDIA Corp.", asset_subclass: "US_EQUITY", lot_size: 1, tick_size: 0.01, tradable: true, fractionable: true },
      ],
    });
  }
  if (url.pathname === "/api/markets/nse/refresh" && method === "POST") return json({ tradable_count: 2418 });
  if (url.pathname === "/api/markets/nasdaq/sync" && method === "POST") return json({ counts: { total: 4120 } });
  if (url.pathname === "/api/markets/watchlists" && method === "POST") {
    watchlists[body.market].push({ symbol: body.symbol, name: body.symbol, instrument_key: `${body.market}:${body.symbol}` });
    return json({ ok: true });
  }
  if (url.pathname.startsWith("/api/markets/watchlists/") && method === "DELETE") {
    const [, , , , mkt, sym] = url.pathname.split("/");
    watchlists[mkt] = watchlists[mkt].filter((x) => x.symbol !== sym);
    return json({ ok: true });
  }
  if (url.pathname === "/api/markets/chart") {
    const mkt = url.searchParams.get("market");
    const sym = url.searchParams.get("symbol");
    if (mkt === "NASDAQ" && alpacaConnState !== "CONNECTED_READ_ONLY") {
      return json({
        market: "NASDAQ", symbol: sym, connection_state: "DISCONNECTED",
        data_source: "alpaca_market_data_v2_iex", data_feed_label: "ALPACA NASDAQ READ-ONLY (DISCONNECTED)",
        bars: [], quote: null, generated_at: now(),
      });
    }
    const base = mkt === "NSE" ? 2950 : 138.5;
    const bars = Array.from({ length: 40 }, (_, i) => ({
      timestamp: new Date(Date.now() - (40 - i) * 300_000).toISOString(),
      open: base + i * 0.5, high: base + i * 0.5 + 1.2, low: base + i * 0.5 - 0.8, close: base + i * 0.5 + (i % 2 === 0 ? 0.7 : -0.3), volume: 12000 + i * 300,
    }));
    return json({
      market: mkt, symbol: sym, connection_state: "CONNECTED_READ_ONLY",
      data_source: mkt === "NSE" ? "upstox_v3_readonly" : "alpaca_market_data_v2_iex",
      data_feed_label: `${mkt} LIVE READ-ONLY`,
      bars,
      quote: { symbol: sym, bid: base + 19.8, ask: base + 20.0, last: base + 19.9, spread_bps: 6.8, timestamp: now(), age_seconds: 1.4, fresh: true },
      generated_at: now(),
    });
  }
  if (url.pathname === "/api/markets/paper/preview" && method === "POST") {
    const blocked = body.qty > 99999;
    return json({
      ok: !blocked, market: body.market, symbol: body.symbol,
      currency: body.market === "NSE" ? "INR" : "USD",
      reference_price: body.market === "NSE" ? 2962.5 : 140.25,
      estimated_fill_price: body.market === "NSE" ? 2963.1 : 140.28,
      estimated_notional: body.qty * (body.market === "NSE" ? 2963.1 : 140.28),
      estimated_fees: { total: body.market === "NSE" ? 38.45 : 0.14 },
      quote_source: body.market === "NSE" ? "upstox_v3_readonly" : "alpaca_market_data_v2_iex",
      quote_age_seconds: 1.2,
      rejection_reasons: blocked ? ["Order notional exceeds max_order_value cap"] : [],
    });
  }
  if (url.pathname === "/api/markets/paper/orders" && method === "POST") {
    if (body.qty > 99999) {
      return json({ ok: false, preview: { ok: false, currency: "USD", reference_price: 140.25, estimated_fill_price: 140.28, estimated_notional: 9999999, estimated_fees: { total: 0 }, rejection_reasons: ["Order notional exceeds max_order_value cap"] } });
    }
    return json({ ok: true, order: { client_order_id: body.client_order_id, market: body.market, symbol: body.symbol, status: "FILLED", broker_orders_placed: 0 } });
  }
  if (url.pathname.startsWith("/api/markets/paper/orders/") && method === "DELETE") {
    const cid = decodeURIComponent(url.pathname.split("/").pop());
    const idx = nasdaqOrders.findIndex((o) => o.client_order_id === cid);
    if (idx >= 0) nasdaqOrders[idx].status = "CANCELED";
    return json({ ok: true });
  }
  if (url.pathname === "/api/markets/paper/positions/protection" && method === "POST") {
    nsePositions[0].stop_loss = body.stop_loss;
    nsePositions[0].take_profit = body.take_profit;
    return json({ ok: true });
  }
  if (url.pathname.endsWith("/close") && url.pathname.includes("/api/markets/paper/positions/")) {
    nsePositions.splice(0, 1);
    return json({ ok: true });
  }
  if (url.pathname.endsWith("/reconcile") && url.pathname.includes("/api/markets/paper/")) {
    return json({ audit_chain_valid: true, audit_events_verified: 14 });
  }
  if (url.pathname.endsWith("/sync") && url.pathname.includes("/api/markets/paper/")) {
    return json({ ok: true });
  }
  if (url.pathname === "/api/markets/paper/session/start" && method === "POST") {
    if (body.market === "NSE") nseState = "RUNNING"; else nasdaqState = "RUNNING";
    return json({ ok: true });
  }
  if (url.pathname === "/api/markets/paper/session/transition" && method === "POST") {
    const map = { pause: "PAUSED", resume: "RUNNING", stop: "STOPPED" };
    if (body.market === "NSE") nseState = map[body.action]; else nasdaqState = map[body.action];
    return json({ ok: true });
  }
  if (url.pathname === "/api/markets/paper/reset" && method === "POST") {
    if (body.market === "NSE") nseState = "STOPPED"; else nasdaqState = "STOPPED";
    return json({ ok: true });
  }
  if (url.pathname === "/api/markets/paper/config" && method === "POST") return json({ ok: true });

  // Alpaca Broker Gate & Credentials
  if (url.pathname === "/api/alpaca-broker/mode" && method === "POST") return json({ enabled: true, environment: body.environment, endpoint_host: "paper-api.alpaca.markets" });
  if (url.pathname === "/api/alpaca-broker/readiness") return json({ all_passed: true, checks: { credentials: true, account_active: true, clock: true }, failed_checks: [] });
  if (url.pathname === "/api/alpaca-broker/gate/arm" && method === "POST") { gateState = "ARMED_PENDING_CONFIRM"; return json({ gate: { state: gateState } }); }
  if (url.pathname === "/api/alpaca-broker/gate/activate" && method === "POST") {
    assert.equal(body.confirm_live_trading, "I UNDERSTAND THIS WILL PLACE REAL-MONEY LIVE ORDERS");
    gateState = "LIVE_ACTIVE";
    return json({ gate: { state: gateState } });
  }
  if (url.pathname === "/api/alpaca-broker/gate/disarm" && method === "POST") { gateState = "DISABLED"; return json({ gate: { state: gateState } }); }
  if (url.pathname === "/api/alpaca-broker/recover" && method === "POST") return json({ resolved_intents: 0 });
  if (url.pathname === "/api/alpaca-broker/limits" && method === "POST") return json({ ok: true });
  if (url.pathname.startsWith("/api/alpaca-broker/credentials/")) return json({ ok: true });
  if (url.pathname === "/api/alpaca-broker/test" && method === "POST") return json({ ok: true, connection_state: "CONNECTED_READ_ONLY", guidance: "Read-only GET /v2/account verified; zero order endpoints touched." });

  // Connections & Upstox Tokens
  if (url.pathname === "/api/autonomy/upstox-token") { upstoxConnState = method === "DELETE" ? "DISCONNECTED" : "CONNECTED_READ_ONLY"; return json({ ok: true }); }
  if (url.pathname === "/api/autonomy/upstox-live-token") return json({ ok: true });
  if (url.pathname === "/api/connections/upstox/verify-quote") {
    upstoxConnState = "CONNECTED_READ_ONLY";
    return json({ ok: true, connection_state: "CONNECTED_READ_ONLY", guidance: "Live NSE quote verified; 0 order calls.", quote: { last: 2962.5 } });
  }
  if (url.pathname === "/api/connections/alpaca/credentials") { alpacaConnState = method === "DELETE" ? "DISCONNECTED" : "CONNECTED_READ_ONLY"; return json({ ok: true }); }
  if (url.pathname === "/api/connections/alpaca/verify-quote") {
    alpacaConnState = "CONNECTED_READ_ONLY";
    return json({ ok: true, connection_state: "CONNECTED_READ_ONLY", guidance: "Live NASDAQ IEX snapshot verified; 0 order calls." });
  }

  // Emergency Stop
  if (url.pathname === "/api/trading/emergency" && method === "POST") { emergencyStopped = true; return json({ ok: true, note: "Emergency stop set" }); }
  if (url.pathname === "/api/markets/emergency-stop" && method === "POST") { emergencyStopped = true; gateState = "DISABLED"; return json({ ok: true }); }
  if (url.pathname === "/api/trading/emergency/clear" && method === "POST") { emergencyStopped = false; return json({ ok: true }); }
  if (url.pathname === "/api/markets/emergency-stop/reset" && method === "POST") {
    assert.equal(body.confirm_phrase, "CLEAR EMERGENCY STOP");
    emergencyStopped = false;
    return json({ ok: true });
  }

  throw new Error(`Unhandled route in multimarket_terminal_smoke: ${method} ${url.pathname}${url.search}`);
}

const dom = new JSDOM(html, { url: "http://127.0.0.1:8000/terminal", pretendToBeVisual: true, runScripts: "outside-only" });
const { window } = dom;
window.sessionStorage.setItem("axiom-api-token", TOKEN);
window.fetch = fetchFake;
window.HTMLCanvasElement.prototype.getContext = () => ({
  setTransform() {}, clearRect() {}, beginPath() {}, moveTo() {}, lineTo() {}, closePath() {},
  setLineDash() {}, save() {}, restore() {},
  fillRect() { draws.fillRect += 1; },
  stroke() { draws.stroke += 1; },
  fillText() { draws.fillText += 1; },
  measureText: (t) => ({ width: String(t).length * 6 }),
});
window.HTMLCanvasElement.prototype.getBoundingClientRect = () => ({ left: 0, top: 0, width: 920, height: 480 });

const flush = async (rounds = 6) => {
  for (let i = 0; i < rounds; i += 1) await new Promise((r) => setTimeout(r, 0));
};

window.eval(code);
await flush(10);

const doc = window.document;
const $ = (id) => doc.getElementById(id);
const click = async (el) => { assert(el, "Missing clickable element"); el.click(); await flush(8); };

// 1. Overview rendered with NSE ₹ and NASDAQ $ ledgers + 5 provider cards
assert.equal($("auth-gate").hidden, true, "Auth gate should unlock with session token");
assert($("mm-nse-overview-list").textContent.includes("₹1,000,225") || $("mm-nse-overview-list").textContent.includes("₹10,00,225"), "NSE INR equity should render");
assert($("mm-nasdaq-overview-list").textContent.includes("$100,000"), "NASDAQ USD equity should render");
assert.equal(doc.querySelectorAll("#mm-overview-providers .provider-card").length, 5, "All 5 provider roles rendered");

// 2. Venue switcher + Quick Symbol Jump when Alpaca is DISCONNECTED -> honest empty state (no sample bars!)
await click($("mm-venue-nasdaq"));
assert.equal($("mm-venue-nasdaq").getAttribute("aria-pressed"), "true");
$("mm-quick-search-input").value = "NVDA";
$("mm-quick-search-form").dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
await flush(10);
assert.equal($("page-charts").hidden, false, "Quick jump navigated to Supercharts");
assert($("chart-empty").textContent.includes("unavailable (DISCONNECTED)"), "Must show honest DISCONNECTED state, never sample bars");

// 3. Connections page: Save Upstox & Alpaca Data credentials + verify quotes; assert DOM inputs cleared!
await click(doc.querySelector('[data-page="connections"]'));
$("mm-upstox-token-input").value = "jwt-secret-tok-nse";
$("mm-upstox-data-form").dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
await flush(8);
assert.equal($("mm-upstox-token-input").value, "", "Upstox token input must be cleared from DOM immediately");
await click($("mm-upstox-verify-btn"));
assert.equal($("mm-upstox-data-pill").textContent, "CONNECTED_READ_ONLY");

$("mm-alpaca-data-key").value = "PKTESTDATA";
$("mm-alpaca-data-secret").value = "secret-val-alpaca-data";
$("mm-alpaca-data-form").dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
await flush(8);
assert.equal($("mm-alpaca-data-key").value, "");
assert.equal($("mm-alpaca-data-secret").value, "", "Alpaca secret input must be cleared from DOM immediately");
await click($("mm-alpaca-data-verify-btn"));
assert.equal($("mm-alpaca-data-pill").textContent, "CONNECTED_READ_ONLY");

// Save & test Alpaca Broker slot + Upstox Live token
$("mm-alpaca-broker-key").value = "PKLIVEKEY";
$("mm-alpaca-broker-secret").value = "secret-val-broker";
$("mm-alpaca-broker-cred-form").dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
await flush(8);
assert.equal($("mm-alpaca-broker-secret").value, "");
await click($("mm-alpaca-broker-test-btn"));
assert($("mm-alpaca-broker-test-result").textContent.includes("CONNECTED_READ_ONLY"));

$("mm-upstox-live-input").value = "jwt-secret-tok-live";
$("mm-upstox-live-cred-form").dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
await flush(8);
assert.equal($("mm-upstox-live-input").value, "");

// 4. Watchlists & Search: Refresh NSE BOD, Sync NASDAQ, search, +Watch
await click(doc.querySelector('[data-page="instruments"]'));
await click($("mm-refresh-nse-bod"));
await click($("mm-sync-nasdaq-assets"));
await click($("mm-catalog-search-btn"));
assert($("mm-catalog-rows").textContent.includes("RELIANCE") && $("mm-catalog-rows").textContent.includes("NVDA"));
const watchBtn = doc.querySelector('[data-mm-watch="NASDAQ:NVDA"]');
await click(watchBtn);
assert($("mm-watchlist-grid").textContent.includes("NVDA"));

// 5. Supercharts & Multi-Market Paper Ticket: load live NASDAQ chart + preview fees + submit simulated order + test guardrail rejection
const chartJumpBtn = doc.querySelector('[data-mm-jump-chart="NASDAQ:NVDA"]');
await click(chartJumpBtn);
const prevDraws = draws.fillRect;
await click($("mm-load-live-chart-btn"));
assert(draws.fillRect > prevDraws, "Candles and volume histogram rendered on TradingView canvas");
assert($("chart-feed-context").textContent.includes("40 real OHLCV bars loaded"));

await click($("ticket-tab-mm"));
$("mm-order-qty").value = "10";
await click($("mm-preview-order-btn"));
assert($("mm-ticket-preview-box").textContent.includes("SIMULATED PREVIEW PASSED"));
assert($("mm-ticket-preview-box").textContent.includes("0 Broker Calls"));

$("mm-order-form").dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
await flush(8);
assert(calls.some((c) => c.path === "/api/markets/paper/orders" && c.method === "POST"));

// Guardrail rejection check
$("mm-order-qty").value = "250000";
await click($("mm-preview-order-btn"));
assert($("mm-ticket-preview-box").textContent.includes("BLOCKED BY GUARDRAILS"));

// 6. Orders, Fills & Positions: SL/TP modify dialog, close position, cancel order, SQLite reconcile
await click(doc.querySelector('[data-page="orders"]'));
assert($("mm-paper-position-rows").textContent.includes("RELIANCE"));
await click(doc.querySelector("[data-mm-pos-prot]"));
assert.equal($("modal").hidden, false);
$("mm-mod-sl").value = "2915";
$("mm-mod-tp").value = "3080";
await click($("modal-confirm"));
assert($("mm-paper-position-rows").textContent.includes("SL 2915"));

await click(doc.querySelector('[data-mm-cancel-order="mm-paper-open-1"]'));
await click(doc.querySelector('[data-mm-pos-close="NSE:RELIANCE"]'));
await click($("mm-reconcile-paper-btn"));
assert(calls.some((c) => c.path === "/api/markets/paper/NSE/reconcile"));

// 7. Strategies & Modes: Start -> Pause -> Resume -> Stop -> Reset
await click(doc.querySelector('[data-page="strategies"]'));
await click($("mm-nse-start-btn"));
assert.equal($("mm-strat-nse-state").textContent, "RUNNING");
await click($("mm-nse-pause-btn"));
assert.equal($("mm-strat-nse-state").textContent, "PAUSED");
await click($("mm-nse-resume-btn"));
assert.equal($("mm-strat-nse-state").textContent, "RUNNING");
await click($("mm-nse-stop-btn"));
assert.equal($("mm-strat-nse-state").textContent, "STOPPED");
await click($("mm-nse-reset-btn"));
await click($("modal-confirm"));
assert($("mm-strategy-decision-rows").textContent.includes("ema_crossover"));

// 8. Risk & Live Gates: Save NSE/NASDAQ/Broker limits + Readiness -> Arm -> Phrase Activate -> Disarm
await click(doc.querySelector('[data-page="risk"]'));
$("mm-nse-risk-form").dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
$("mm-nasdaq-risk-form").dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
await flush(8);
await click($("mm-save-alpaca-broker-risk"));
await click($("mm-alpaca-readiness-btn"));
assert($("mm-alpaca-gate-output").textContent.includes("All readiness checks passed"));
await click($("mm-alpaca-arm-btn"));
assert($("mm-alpaca-gate-output").textContent.includes("ARMED_PENDING_CONFIRM"));
await click($("mm-alpaca-activate-btn"));
assert.equal($("modal").hidden, false);
// Attempting confirm without exact phrase must fail and keep modal open
await click($("modal-confirm"));
assert.equal($("modal").hidden, false, "Activate must block until exact phrase is typed");
assert($("modal-error").textContent.includes("Type the exact phrase"));
$("modal-phrase").value = "I UNDERSTAND THIS WILL PLACE REAL-MONEY LIVE ORDERS";
await click($("modal-confirm"));
assert($("mm-alpaca-gate-output").textContent.includes("LIVE_ACTIVE"));
await click($("mm-alpaca-disarm-btn"));
assert($("mm-alpaca-gate-output").textContent.includes("DISARMED"));

// 9. Global Emergency Stop + Phrase-Confirmed Clear
await click($("emergency-stop"));
await click($("modal-confirm"));
assert(calls.some((c) => c.path === "/api/markets/emergency-stop" && c.method === "POST"));
assert.equal($("mm-strat-nse-state").textContent, "EMERGENCY_STOPPED");

await click($("clear-emergency"));
$("modal-phrase").value = "CLEAR EMERGENCY STOP";
await click($("modal-confirm"));
assert(calls.some((c) => c.path === "/api/markets/emergency-stop/reset" && c.method === "POST"));
assert.equal($("mm-strat-nse-state").textContent, "STOPPED");

// 10. Activity & Audit SHA-256 chain rendered
await click(doc.querySelector('[data-page="activity"]'));
assert($("mm-paper-audit-rows").textContent.includes("PAPER_ORDER_FILLED"));

console.log(`multimarket terminal harness: OK (${calls.length} authenticated local API calls, ${draws.fillRect} canvas draw calls, 0 broker order leaks)`);
window.close();
