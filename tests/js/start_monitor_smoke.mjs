/* Headless JSDOM suite for the primary operator screen:
 * workspace 00 · "Start & Monitor" (#page-start).
 *
 * Proves the screen is honest and bounded:
 *  1. it is the default page, reachable from the sidebar and the quick-nav;
 *  2. every figure comes from /api/markets/start-monitor (no hard-coded quota,
 *     cadence, cap or readiness conclusion);
 *  3. PAPER start posts only to the existing per-market paper session route and
 *     never to an order route;
 *  4. LIVE is offered only as the existing four separate steps
 *     (readiness -> mode -> arm -> acknowledge + phrase -> activate), in that
 *     order, and activation never starts or resumes a strategy;
 *  5. the market-data credential is never re-rendered, streamed to storage, or
 *     echoed back after saving;
 *  6. monitoring keeps SIMULATED rows separate from BROKER rows;
 *  7. Autopilot is disabled while any server-reported requirement is missing,
 *     and the page never raises a position cap or sends an order.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { JSDOM } from "jsdom";
import { consoleReadRoutes, consoleWriteRoutes } from "./console_routes.mjs";

const root = resolve(import.meta.dirname, "../..");
const html = readFileSync(resolve(root, "app/static/terminal/index.html"), "utf8");
const code = readFileSync(resolve(root, "app/static/terminal/terminal.js"), "utf8");

const TOKEN = "start-monitor-token";
const calls = [];

const LIMITS = {
  market: "NSE", currency: "INR",
  max_order_value: 250000, max_symbol_exposure: 400000, max_total_exposure: 900000,
  max_open_positions: 10, max_daily_loss: 25000, max_daily_orders: 40,
  max_quote_age_seconds: 90, brokerage_bps: 0,
};

const NSE_EXIT = {
  routed: false,
  summary: "No NSE exit is routed.",
  closing_paths: ["protected GTT legs placed with the entry"],
  operator_fallback: "close the position manually in the Upstox app, then run reconcile",
};
const US_EXIT = { routed: true, summary: "close-only exits documented", closing_paths: [], operator_fallback: "" };

let liveState = {
  NSE: { mode: "PAPER", gate: "DISABLED", ready: false, failed: ["configuration_complete"],
         missing: ["Save a separate UPSTOX_LIVE_ACCESS_TOKEN"], credential: false,
         liveCredential: false },
  NASDAQ: { mode: "PAPER", gate: "DISABLED", ready: false, failed: [],
            missing: [], credential: true, liveCredential: false },
};

/* Rebuild credential presence from the fixture flags, so a test can toggle a
 * slot and re-read the server without restarting the harness. */
function refreshCredentialRows() {
  // Presence is read lazily from liveState by the row getters above, so nothing
  // has to be rewritten here: flipping a flag is enough for the next payload.
}
let paperOrders = [];
let liveEvents = [];

function autopilotGaps(market) {
  const live = liveState[market];
  const gaps = [];
  if (!live.credential) {
    gaps.push({ code: "NO_MARKET_DATA_CREDENTIAL", blocks: ["PAPER", "LIVE"],
                text: "market-data credential is not configured" });
  }
  return gaps;
}

/* Mirrors the server projection: one union of blockers, tagged with the modes
 * each fact blocks, plus the mode-specific gap lists filtered from it. */
function autopilotBlock(market) {
  const isNse = market === "NSE";
  const gaps = autopilotGaps(market);
  const blockers = [...gaps];
  if (isNse) {
    blockers.push({ code: "NO_ROUTED_EXIT", blocks: ["LIVE"],
                    text: "AXIOM cannot close an NSE position: no exit is routed." });
  }
  return {
    paper: { available: gaps.length === 0, active: false, gaps,
             reason: gaps.length ? "Blocked until every listed requirement is met." : "May create SIMULATED orders." },
    live: { available: false, active: false,
            gaps: blockers.filter((b) => (b.blocks || []).includes("LIVE")),
            reason: "LIVE Autopilot stays disabled until readiness passes." },
    blockers,
    cautions: isNse ? [] : [{ code: "PLAN_UNVERIFIED", text: "declared plan not verified" }],
    may: ["analyse selected symbols on completed bars only", "hold at most the open-position cap"],
    may_not: ["raise any risk limit", "turn a 100-symbol scan into 100 orders"],
    limits_source: "server",
  };
}

function marketPayload(market) {
  const live = liveState[market];
  const isNse = market === "NSE";
  const built = {
    market,
    venue_label: isNse ? "NSE · Upstox (INR)" : "NASDAQ · Alpaca (USD)",
    broker: isNse ? "Upstox" : "Alpaca",
    currency: isNse ? "INR" : "USD",
    badge: live.mode === "LIVE"
      ? (live.gate === "ACTIVE" ? "LIVE · ACTIVE" : live.gate === "ARMED" ? "LIVE · ARMED" : "LIVE · DISABLED")
      : "PAPER",
    mode: live.mode,
    selected_symbols: isNse
      ? [{ symbol: "RELIANCE", instrument_key: "NSE_EQ|INE002A01018", name: "Reliance Industries" }]
      : [{ symbol: "AAPL", instrument_key: "NASDAQ|AAPL", name: "Apple" }],
    selected_symbol_count: 1,
    session_symbols: isNse ? ["RELIANCE"] : ["AAPL"],
    symbol_cap: 100,
    cadence: {
      requested_seconds: 15,
      sustainable_seconds: isNse ? 0.9 : 0.6,
      max_symbols_at_cadence: isNse ? 16 : 25,
      requests_per_cycle: isNse
        ? { "upstox.market_quote": 1, "upstox.historical_candle": 1 }
        : { "alpaca.market_data": 2 },
      dispatch_seconds: 2,
      status: "READY",
      source: "server",
      supportable: true,
      // The server writes this sentence; the browser prints it verbatim.
      label: isNse
        ? "15 s every cycle is possible for 1 selected symbol(s): the provider quota needs only 0.9 s per cycle. At 15 s the provider quota carries at most 16 of 100 symbols."
        : "15 s every cycle is possible for 1 selected symbol(s): the provider quota needs only 0.6 s per cycle. At 15 s the provider quota carries at most 25 of 100 symbols.",
    },
    quota: {
      profile: isNse ? "upstox_standard" : "alpaca_basic",
      verification: isNse ? "DOCUMENTED" : "UNVERIFIED",
      credential_present: live.credential,
    },
    credentials: {
      rows: [
        { slot: isNse ? "UPSTOX MARKET DATA" : "ALPACA MARKET DATA", role: "market_data", scope: "read-only prices",
          get present() { return live.credential; }, detail: "presence only", affects_execution: false },
        ...(isNse
          ? [{ slot: "UPSTOX LIVE ACCESS TOKEN", role: "live_execution", scope: "real money · order writes",
               get present() { return live.liveCredential; },
               detail: "presence only", affects_execution: true }]
          : [
            { slot: "ALPACA BROKER PAPER", role: "paper_broker", scope: "broker paper orders",
              get present() { return live.paperBrokerCredential; },
              detail: "presence only", affects_execution: false },
            { slot: "ALPACA BROKER LIVE", role: "live_execution", scope: "real money · order writes",
              get present() { return live.liveCredential; },
              detail: "presence only", affects_execution: true },
          ]),
      ],
      // Mirrors app/markets/start_monitor.py _mode_credential_requirements():
      // PAPER needs market data only; LIVE adds the real-money slot.
      roles: { consistent: true, problems: [], resolved: {} },
      requirements: {
        PAPER: {
          required_slots: [isNse ? "UPSTOX MARKET DATA" : "ALPACA MARKET DATA"],
          consistent: true, available: true, roles: ["market_data"],
          source: "POST /api/markets/paper/session/start",
          note: "paper reads quotes, fills locally, never reads an order credential",
        },
        LIVE: {
          required_slots: isNse
            ? ["UPSTOX MARKET DATA", "UPSTOX LIVE ACCESS TOKEN"]
            : ["ALPACA MARKET DATA", "ALPACA BROKER LIVE"],
          consistent: true, available: true, roles: ["market_data", "live_execution"],
          source: "per-market live readiness and execution gate",
          note: "LIVE needs market data plus the separate real-money credential",
        },
      },
    },
    paper: {
      session_state: "STOPPED", strategy: "manual_only", positions: 0, working_orders: 0, fills: 0,
    },
    live: {
      mode: live.mode,
      gate_state: live.gate,
      ready: live.ready,
      failed_checks: live.failed,
      checks: {},
      blockers: live.missing,
      exit_route: isNse ? NSE_EXIT : US_EXIT,
      confirmation_phrase: "ACTIVATE LIVE REAL-MONEY TRADING",
      available: true,
    },
    autopilot: autopilotBlock(market),
    limits: { ...LIMITS, currency: isNse ? "INR" : "USD" },
    position_cap: 10,
    daily_order_cap: 40,
    readiness: {
      overall_status: live.credential ? "NOT_VERIFIED" : "BLOCKED",
      owner_connection_verified: false,
      blockers: live.credential ? [] : ["Missing provider token"],
    },
    cycle_missed: { behavior: "A cycle that cannot get quota before its deadline is deferred, not sent." },
    stale_policy: { max_quote_age_seconds: 90, behavior: "Quotes older than 90s are refused fail-closed." },
  };
  // A test may replace the credential contract to exercise malformed shapes.
  if (live.overrideRequirements) built.credentials.requirements = live.overrideRequirements;
  if (live.overrideRows) built.credentials.rows = live.overrideRows;
  return built;
}

function launchpadPayload() {
  return {
    schema: "axiom.start_monitor.v1",
    generated_at: "2026-11-07T10:00:00Z",
    server: { connection: "LOCAL", secret_returned: false },
    mode_choices: [
      { id: "PAPER", label: "PAPER", real_money: false, detail: "local simulated fills" },
      { id: "LIVE", label: "LIVE", real_money: true, detail: "real broker orders" },
    ],
    markets: { NSE: marketPayload("NSE"), NASDAQ: marketPayload("NASDAQ") },
    strategies: [{ name: "manual_only" }, { name: "parallel_ensemble" }, { name: "moving_average" }],
    strategy_names: ["manual_only", "parallel_ensemble", "moving_average"],
    autopilot_strategies: ["parallel_ensemble", "moving_average"],
    max_symbols_per_scan: 100,
    execution_boundary: "app.markets.live_execution",
    broker_order_api_calls: 0,
    evidence: { provider_network_calls: 0, real_money_enabled: liveState.NSE.mode === "LIVE" && liveState.NSE.gate === "ACTIVE" },
  };
}

const json = (body) => ({
  ok: true, status: 200,
  headers: { get: () => "application/json" },
  json: async () => body,
});

function fakeFetch(url, options = {}) {
  const parsed = new URL(url, "http://127.0.0.1:8000");
  const body = options.body ? JSON.parse(options.body) : null;
  calls.push({ path: parsed.pathname + parsed.search, method: options.method || "GET", body });
  const p = parsed.pathname;

  if (p === "/api/markets/start-monitor") return Promise.resolve(json(launchpadPayload()));
  if (p === "/api/markets/search") {
    const market = parsed.searchParams.get("market");
    const items = market === "NSE"
      ? [
        { market: "NSE", symbol: "RELIANCE", trading_symbol: "RELIANCE", instrument_key: "NSE_EQ|INE002A01018", segment: "NSE_EQ", asset_category: "CASH_EQUITY_OR_ETF", asset_subclass: "EQUITY", name: "Reliance", tradable: true },
        { market: "NSE", symbol: "NIFTY 50", trading_symbol: "NIFTY 50", instrument_key: "NSE_INDEX|Nifty 50", segment: "NSE_INDEX", asset_category: "INDEX", asset_subclass: "INDEX", name: "Nifty 50", tradable: false },
      ]
      : [
        { market: "NASDAQ", symbol: "AAPL", asset_class: "us_equity", status: "active", asset_subclass: "COMMON_STOCK", name: "Apple Inc", tradable: true },
        { market: "NASDAQ", symbol: "OLDCO", asset_class: "us_equity", status: "inactive", asset_subclass: "COMMON_STOCK", name: "Old Co", tradable: true },
      ];
    return Promise.resolve(json({
      items,
      nse: market === "NSE" ? { total_matches: 1 } : null,
      nasdaq: market === "NASDAQ" ? { total_matches: 1 } : null,
    }));
  }
  if (p === "/api/ensemble/scan/preview") {
    const market = body?.market;
    return Promise.resolve(json({
      plan: {
        can_start: false, status: "NO CREDENTIAL", cadence_requested_seconds: 15,
        cadence_sustainable_seconds: market === "NSE" ? 90 : 60,
        total_requests_per_cycle: 200,
        window_usage: [{ category: market === "NSE" ? "upstox.market_quote" : "alpaca.market_data",
                         readable: "0 / 500 requests this 1 minute" }],
        reasons: [`${market}: credential not configured`],
      },
    }));
  }
  if (p === "/api/markets/paper/session/start") return Promise.resolve(json({ session_state: "MARKET_CLOSED_WAITING" }));
  if (p === "/api/markets/paper/session/transition") return Promise.resolve(json({ session_state: "STOPPED" }));
  if (p.startsWith("/api/markets/paper/")) return Promise.resolve(json({ orders: paperOrders, fills: [], positions: [] }));
  if (p === "/api/markets/live/audit") return Promise.resolve(json({ events: liveEvents }));
  if (/^\/api\/markets\/live\/[A-Z]+\/(readiness|mode|arm|activate|disarm)$/.test(p)) {
    const market = p.split("/")[4];
    const action = p.split("/")[5];
    if (action === "readiness") liveState[market].ready = true;
    if (action === "mode") liveState[market].mode = "LIVE";
    if (action === "arm") liveState[market].gate = "ARMED";
    if (action === "activate") liveState[market].gate = "ACTIVE";
    if (action === "disarm") { liveState[market].mode = "PAPER"; liveState[market].gate = "DISABLED"; }
    return Promise.resolve(json({ ok: true, market, action }));
  }
  if (/credentials|upstox-token|verify-quote/.test(p)) return Promise.resolve(json({ saved: true, present: true, secret_returned: false }));

  const read = consoleReadRoutes(p, {}, {});
  if (read !== undefined) return Promise.resolve(json(read));
  const written = consoleWriteRoutes(p, options.method, body, {});
  if (written !== undefined) return Promise.resolve(json(written));
  return Promise.resolve(json({}));
}

const flush = async (rounds = 12) => {
  for (let i = 0; i < rounds; i += 1) await new Promise((r) => setTimeout(r, 0));
};

const dom = new JSDOM(html, { url: "http://127.0.0.1:8000/terminal", runScripts: "outside-only", pretendToBeVisual: true });
const { window } = dom;
window.fetch = fakeFetch;
window.HTMLCanvasElement.prototype.getContext = () => ({
  setTransform() {}, clearRect() {}, beginPath() {}, moveTo() {}, lineTo() {}, closePath() {},
  setLineDash() {}, save() {}, restore() {}, fillRect() {}, stroke() {}, fillText() {},
  strokeRect() {}, fill() {}, arc() {}, rect() {}, clip() {}, measureText: () => ({ width: 20 }),
});
window.ResizeObserver = class { observe() {} disconnect() {} };
window.eval(code);

const doc = window.document;
const $ = (id) => doc.getElementById(id);
const text = (id) => ($(id)?.textContent || "").replace(/\s+/g, " ").trim();
const stepDone = (n) => Boolean(doc.querySelector(`[data-start-step="${n}"]`)?.classList.contains("done"));
const stepDoneCount = () => [...doc.querySelectorAll("[data-start-step].done")].length;

try {
  doc.getElementById("auth-token").value = TOKEN;
  doc.getElementById("auth-form").dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
  await flush(30);

  // 1. The primary screen is the default page and is reachable from both navs.
  assert.equal($("auth-gate").hidden, true, "the shell unlocks with the local token");
  assert.equal($("page-start").hidden, false, "Start & Monitor must be the visible page after unlock");
  assert.equal($("page-overview").hidden, true, "the overview leaves the primary slot");
  const navStart = doc.querySelector('.nav-item[data-page="start"]');
  assert.ok(navStart && navStart.classList.contains("active"), "sidebar exposes 00 Start & Monitor as active");
  assert.ok(doc.querySelector('#mm-quick-nav-bar [data-go="start"]'), "quick-nav exposes Start & Monitor");
  assert.equal(text("page-title"), "Start & Monitor");

  // The flow strip must not tick a step the operator has not taken. At first
  // paint nothing is done: mode and review have defaults but were never chosen.
  // Only step 2 may be ticked, because the server already reports one symbol
  // selected in each market's watchlist. Mode and review have defaults but were
  // never chosen, so they must stay pending.
  const doneSteps = [...doc.querySelectorAll("[data-start-step].done")].map((n) => n.dataset.startStep);
  assert.deepEqual(doneSteps, ["2"],
    "only the step backed by real server state is ticked at first paint");
  assert.equal(doc.querySelectorAll("[data-start-step].pending").length, 6,
    "the other six steps start pending");

  // 2. Values come from the server payload, not from the browser.
  assert.equal($("start-badge-NSE").textContent.trim(), "PAPER");
  assert.equal($("start-badge-NASDAQ-bar").textContent.trim(), "NASDAQ · PAPER");
  assert.match(text("start-facts-NSE"), /At 15 s the provider quota carries at most 16 of 100 symbols/,
    "the cadence sentence is the server's, printed verbatim");
  assert.match(text("start-facts-NASDAQ"), /at most 25 of 100 symbols/,
    "and it is per market, not copied between cards");
  assert.match(text("start-facts-NSE"), /0 open \/ 10 cap/, "position cap comes from the server limits");
  assert.match(text("start-cadence-line"), /upstox\.market_quote 1,/,
    "per-cycle request cost comes from the server plan, not the page");
  assert.match(text("start-facts-NASDAQ"), /UNVERIFIED/, "an unverified Alpaca plan stays unverified");

  // Credential slots stay distinct and never show a value.
  const credRows = [...doc.querySelectorAll("#start-cred-table tbody tr")].map((tr) => tr.textContent);
  assert.equal(credRows.length, 5, "every provider credential slot is listed separately");
  for (const slot of ["UPSTOX MARKET DATA", "UPSTOX LIVE ACCESS TOKEN", "ALPACA MARKET DATA", "ALPACA BROKER PAPER", "ALPACA BROKER LIVE"]) {
    assert(credRows.some((row) => row.includes(slot)), `credential slot ${slot} must be listed`);
  }
  // The loopback API token unlocks this tab; it is not a provider credential
  // and must not sit in the provider table where it could be read as one.
  assert(!credRows.some((row) => row.includes("AXIOM API TOKEN")),
    "the console token is not listed as a provider credential");
  assert.doesNotMatch(text("start-cred-table"), /Bearer |AKIA|[A-Za-z0-9]{24,}/, "no secret material renders");

  // 3. Blocker + exit limitation are visible before anything starts.
  assert.match(text("start-blockers-NSE"), /NO_MARKET_DATA_CREDENTIAL/, "the machine-readable blocker is printed");
  assert.match(text("start-blockers-NSE"), /market-data credential is not configured/, "with a plain-language explanation");
  assert.match(text("start-exit-NSE"), /NO ROUTED EXIT/, "NSE keeps its routed-exit limitation on screen");
  assert.match(text("start-exit-NSE"), /close the position manually in the Upstox app/, "and its manual fallback");

  // 4. Selecting symbols (including select-all) never adds a watch-only instrument
  //    and never touches a position cap.
  $("start-select-NSE").checked = true;
  $("start-select-NSE").dispatchEvent(new window.Event("change", { bubbles: true }));
  await flush();
  const beforeAll = calls.length;
  $("start-select-all-NSE").dispatchEvent(new window.Event("click", { bubbles: true }));
  await flush(20);
  const selectAllCalls = calls.slice(beforeAll);
  assert.ok(selectAllCalls.every((c) => c.method === "GET"), "select-all only reads the catalog");
  assert.equal(text("start-symbol-count-NSE"), "1 SELECTED", "the NSE index is not silently selected");
  assert.match(text("start-eligible-note-NSE"), /Indices, derivatives and suspended series are never included/);
  assert.match(text("start-facts-NSE"), /0 open \/ 10 cap/, "the position cap is unchanged by a selection");

  // The picker checkbox also works and stays capped.
  const firstBox = doc.querySelector('[data-start-symbol]');
  assert.ok(firstBox, "eligible NSE symbols render as checkboxes");
  if (!firstBox.checked) { firstBox.checked = true; firstBox.dispatchEvent(new window.Event("change", { bubbles: true })); }
  await flush();
  assert.match(text("start-symbol-count-NSE"), /SELECTED/);

  // 5. Preview is offline and reports the server verdict.
  $("start-preview-btn").dispatchEvent(new window.Event("click", { bubbles: true }));
  await flush(25);
  assert.equal(text("start-preview-pill"), "PREVIEWED · AT LEAST ONE MARKET BLOCKED");
  assert.match(text("start-preview-body"), /OFFLINE · 0 PROVIDER REQUESTS/);
  assert.match(text("start-preview-body"), /Orders from this preview0/, "the preview states 0 orders explicitly");
  // A 100-symbol NSE scan cannot keep a 15 s cadence: the preview must say so.
  assert.match(text("start-preview-body"), /15 s requested · NOT SUPPORTED for this selection: the provider quota needs 90 s per cycle/,
    "requested vs sustainable cadence is stated, not implied");
  assert.match(text("start-preview-body"), /15 s requested · NOT SUPPORTED for this selection: the provider quota needs 60 s per cycle/,
    "the verdict is per market, computed from that market's own numbers");

  // 6. PAPER start uses the existing per-market session route only.
  const beforeStart = calls.length;
  $("start-paper-btn").dispatchEvent(new window.Event("click", { bubbles: true }));
  await flush(15);
  assert.equal($("modal").hidden, false, "starting a session always asks first");
  $("modal-confirm").dispatchEvent(new window.Event("click", { bubbles: true }));
  await flush(25);
  const startCalls = calls.slice(beforeStart).filter((c) => c.method === "POST");
  assert.ok(startCalls.some((c) => c.path === "/api/markets/paper/session/start"),
    "PAPER start posts to the existing paper session route");
  const paperStart = startCalls.find((c) => c.path === "/api/markets/paper/session/start");
  assert.deepEqual(Object.keys(paperStart.body).sort(), ["market", "strategy", "symbols", "timeframe"]);
  assert.equal(paperStart.body.strategy, "manual_only", "manual review sends no automation strategy");
  assert.equal(paperStart.body.market, "NSE", "only the ticked market starts");
  // Reads of the paper order book are allowed; any *write* toward an order
  // path is not.
  const orderWrites = calls
    .filter((c) => c.method !== "GET")
    .filter((c) => /\/order(s)?\b|paper\/orders|alpaca-broker\/orders/.test(c.path));
  assert.equal(orderWrites.map((c) => `${c.method} ${c.path}`).join(",") || "none", "none",
    "starting PAPER never writes to an order route");

  // 7. LIVE is only reachable as the four separate existing steps.
  $("start-mode-live").dispatchEvent(new window.Event("click", { bubbles: true }));
  await flush();
  assert.equal($("start-mode-badge").textContent.trim(), "LIVE");
  assert.match(text("start-mode-detail"), /changes nothing/i, "selecting LIVE claims no state change");
  assert.equal(liveState.NSE.mode, "PAPER", "selecting LIVE in the page does not touch the server");

  $("start-live-NSE-btn").dispatchEvent(new window.Event("click", { bubbles: true }));
  await flush(20);
  const stepsHost = $("start-live-steps");
  assert.ok(stepsHost, "the guided LIVE sequence renders inside the dialog");
  assert.equal(doc.querySelectorAll("[data-start-live-step]").length, 4, "four separate steps, never combined");
  assert.match($("modal-body").textContent, /Activating a gate does not start or resume a strategy/i,
    "the dialog says activation is not a strategy start");

  const stepButton = (name) => doc.querySelector(`[data-start-live-step="${name}"]`);
  assert.equal(stepButton("mode").disabled, true, "mode cannot be set before readiness passes");
  stepButton("readiness").dispatchEvent(new window.Event("click", { bubbles: true }));
  await flush(20);
  assert.ok(calls.some((c) => c.path === "/api/markets/live/NSE/readiness" && c.method === "GET"),
    "step 1 is a read-only GET");

  stepButton("mode").dispatchEvent(new window.Event("click", { bubbles: true }));
  await flush(20);
  stepButton("arm").dispatchEvent(new window.Event("click", { bubbles: true }));
  await flush(20);
  assert.equal(liveState.NSE.mode, "LIVE");
  assert.equal(liveState.NSE.gate, "ARMED", "arm happens as its own call");

  // Activation refuses without the acknowledgement.
  stepButton("activate").dispatchEvent(new window.Event("click", { bubbles: true }));
  await flush(20);
  assert.equal(liveState.NSE.gate, "ARMED", "activation refused without the risk acknowledgement");
  assert.equal(calls.filter((c) => /\/activate$/.test(c.path)).length, 0);

  $("start-live-ack").checked = true;
  $("start-live-ack").dispatchEvent(new window.Event("change", { bubbles: true }));
  $("start-live-phrase-input").value = "ACTIVATE LIVE REAL-MONEY TRADING";
  $("start-live-phrase-input").dispatchEvent(new window.Event("input", { bubbles: true }));
  stepButton("activate").dispatchEvent(new window.Event("click", { bubbles: true }));
  await flush(25);
  const activateCall = calls.find((c) => /\/api\/markets\/live\/NSE\/activate$/.test(c.path));
  assert.ok(activateCall, "activation uses the existing live activate route");
  assert.deepEqual(activateCall.body, {
    acknowledge_risk: true,
    confirm_live_trading: "ACTIVATE LIVE REAL-MONEY TRADING",
  });
  assert.equal(liveState.NSE.gate, "ACTIVE");

  // Ordering: readiness -> mode -> arm -> activate, never reordered or skipped.
  const order = calls
    .filter((c) => /\/api\/markets\/live\/NSE\/(readiness|mode|arm|activate)$/.test(c.path))
    .map((c) => c.path.split("/").pop());
  assert.deepEqual(order, ["readiness", "mode", "arm", "activate"]);

  // Activation must not start or resume a strategy or run a live cycle.
  const activationIndex = calls.findIndex((c) => /\/activate$/.test(c.path));
  const afterActivation = calls.slice(activationIndex);
  assert.equal(afterActivation.filter((c) => /run-cycle|\/sessions?\/|paper\/session\/start|engine\/resume/.test(c.path)).length, 0,
    "activation never starts a session, resumes an engine or runs a live cycle");

  $("modal-close").dispatchEvent(new window.Event("click", { bubbles: true }));
  await flush(20);
  assert.equal(text("start-badge-NSE-bar"), "NSE · LIVE · ACTIVE", "the sticky bar reflects the gate state");

  // 8. Monitoring keeps simulated and broker rows visibly separate.
  paperOrders = [{
    client_order_id: "c1", order_id: "o1", symbol: "RELIANCE", side: "BUY", qty: 5, filled_qty: 0,
    status: "WORKING", avg_fill_price: null, fees_total: 0, created_at: "2026-11-07T09:59:00Z",
    updated_at: "2026-11-07T09:59:00Z", reject_reason: null,
  }];
  liveEvents = [{
    seq: 1, market: "NSE", event_kind: "LIVE_ORDER_SUBMITTED",
    payload: { symbol: "RELIANCE", side: "BUY", qty: 1, unresolved: false },
    created_at: "2026-11-07T09:58:00Z",
  }];
  $("start-monitor-refresh").dispatchEvent(new window.Event("click", { bubbles: true }));
  await flush(25);
  const monitorText = text("start-monitor-rows");
  assert.match(monitorText, /PAPER · SIMULATED/, "simulated rows are labelled SIMULATED");
  assert.match(monitorText, /LIVE · BROKER/, "broker rows are labelled LIVE · BROKER");

  // 9. Emergency stop is a top-level control, not buried, and asks first.
  const emergencyCallsBefore = calls.filter((c) => c.path === "/api/markets/emergency-stop").length;
  $("start-emergency-btn").dispatchEvent(new window.Event("click", { bubbles: true }));
  await flush(15);
  assert.equal($("modal").hidden, false, "emergency stop confirms first");
  $("modal-confirm").dispatchEvent(new window.Event("click", { bubbles: true }));
  await flush(20);
  assert.equal(calls.filter((c) => c.path === "/api/markets/emergency-stop").length, emergencyCallsBefore + 1);

  // 10. Stop session is one click away and disarms a LIVE market.
  $("start-stop-btn").dispatchEvent(new window.Event("click", { bubbles: true }));
  await flush(15);
  $("modal-confirm").dispatchEvent(new window.Event("click", { bubbles: true }));
  await flush(25);
  assert.ok(calls.some((c) => c.path === "/api/markets/live/NSE/disarm"), "Stop disarms a LIVE market");

  // 11. Credentials: posted in a body, cleared, never stored or echoed.
  const tokenValue = "super-secret-upstox-token-value";
  $("start-upstox-token").value = tokenValue;
  $("start-upstox-form").dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
  await flush(20);
  const tokenCall = calls.find((c) => c.path === "/api/autonomy/upstox-token");
  assert.ok(tokenCall && tokenCall.body.access_token === tokenValue, "the token travels in the JSON body");
  assert.equal($("start-upstox-token").value, "", "the field clears immediately");
  assert.doesNotMatch(doc.body.textContent, new RegExp(tokenValue), "the value is never re-rendered");
  assert.equal(window.sessionStorage.getItem(tokenValue), null, "the provider secret never reaches tab storage");
  const storage = Object.keys(window.sessionStorage).map((k) => window.sessionStorage.getItem(k)).join("|");
  assert.doesNotMatch(storage, new RegExp(tokenValue), "no provider secret is written to sessionStorage");

  /* ------------------------------------------------------------------
   * Setup-progress regressions (audit findings 1 and 2).
   * ---------------------------------------------------------------- */
  $("start-stop-btn").dispatchEvent(new window.Event("click", { bubbles: true }));
  await flush(15);
  $("modal-confirm").dispatchEvent(new window.Event("click", { bubbles: true }));
  await flush(20);

  const resetSetup = () => {
    liveState.NSE.credential = false;
    liveState.NSE.liveCredential = false;
    liveState.NASDAQ.credential = true;
    liveState.NASDAQ.liveCredential = false;
    refreshCredentialRows();
  };
  const pickMode = (mode) => {
    $(mode === "LIVE" ? "start-mode-live" : "start-mode-paper")
      .dispatchEvent(new window.Event("click", { bubbles: true }));
  };
  // Re-read server state through the real control the operator would use.
  const reloadStart = async () => {
    $("refresh-all").dispatchEvent(new window.Event("click", { bubbles: true }));
    await flush(40);
  };
  resetSetup();
  pickMode("PAPER");
  $("start-select-NSE").checked = true;
  $("start-select-NSE").dispatchEvent(new window.Event("change", { bubbles: true }));
  $("start-select-NASDAQ").checked = false;
  $("start-select-NASDAQ").dispatchEvent(new window.Event("change", { bubbles: true }));
  await reloadStart();

  // (a) Regression 1: step 4 stays incomplete until the operator chooses, for
  //     EVERY ticked market — a default in state is not a choice.
  assert.equal(stepDone(4), false, "step 4 must not be complete before any review-mode choice");
  assert.match(text("start-setup-notes"), /4 · STRATEGY &amp; AUTOPILOT|4 · STRATEGY & AUTOPILOT/,
    "an incomplete step 4 explains itself instead of a silent grey chip");
  assert.match(text("start-setup-notes"), /choose manual review or Autopilot for NSE/,
    "and names the market that still needs a review choice");

  doc.querySelector('[data-start-review="manual"][data-start-market="NSE"]')
    .dispatchEvent(new window.Event("click", { bubbles: true }));
  await flush();
  assert.equal(stepDone(4), false,
    "a review choice alone is not enough: the strategy must be chosen too");
  assert.match(text("start-setup-notes"), /choose a strategy for NSE/,
    "the note names the choice still outstanding");

  const strategySelect = $("start-strategy-NSE");
  strategySelect.value = strategySelect.options[1]?.value || "parallel_ensemble";
  strategySelect.dispatchEvent(new window.Event("change", { bubbles: true }));
  await flush();
  assert.equal(stepDone(4), true, "review mode plus strategy completes step 4");

  // Re-choosing either one keeps it complete (no regression to grey).
  doc.querySelector('[data-start-review="autopilot"][data-start-market="NSE"]')
    .dispatchEvent(new window.Event("click", { bubbles: true }));
  await flush();
  assert.equal(stepDone(4), true, "switching review mode keeps step 4 complete for that market");

  // (b) A newly ticked market without a choice makes step 4 incomplete again.
  $("start-select-NASDAQ").checked = true;
  $("start-select-NASDAQ").dispatchEvent(new window.Event("change", { bubbles: true }));
  await flush();
  assert.equal(stepDone(4), false, "ticking another market re-opens step 4 for that market");
  assert.match(text("start-setup-notes"), /manual review or Autopilot for NASDAQ/);
  doc.querySelector('[data-start-review="manual"][data-start-market="NASDAQ"]')
    .dispatchEvent(new window.Event("click", { bubbles: true }));
  await flush();
  assert.equal(stepDone(4), false, "NASDAQ still needs its strategy choice");
  const nasdaqStrategy = $("start-strategy-NASDAQ");
  nasdaqStrategy.value = nasdaqStrategy.options[1]?.value || "parallel_ensemble";
  nasdaqStrategy.dispatchEvent(new window.Event("change", { bubbles: true }));
  await flush();
  assert.equal(stepDone(4), true, "both markets chosen completes step 4 again");

  // (c) Regression 2: PAPER needs only the market-data credential, read from the
  //     server's route-derived requirement list. Both ticked markets have one;
  //     neither has a live execution credential.
  liveState.NSE.credential = true;
  liveState.NSE.liveCredential = false;
  liveState.NASDAQ.credential = true;
  liveState.NASDAQ.liveCredential = false;
  refreshCredentialRows();
  await reloadStart();
  assert.equal(stepDone(3), true,
    "PAPER completes step 3 with market data alone — it never reads an order credential");

  // (d) Missing market data leaves PAPER step 3 incomplete, with the reason.
  liveState.NASDAQ.credential = false;
  refreshCredentialRows();
  await reloadStart();
  assert.equal(stepDone(3), false, "PAPER stays incomplete without the market-data credential");
  assert.match(text("start-setup-notes"), /PAPER needs NASDAQ ALPACA MARKET DATA/);

  // (e) LIVE with market data present but the live execution credential missing
  //     must NOT complete: this is the audit's second bug.
  liveState.NASDAQ.credential = true;
  liveState.NASDAQ.liveCredential = false;
  refreshCredentialRows();
  pickMode("LIVE");
  await reloadStart();
  assert.equal(stepDone(3), false, "LIVE must not complete with market data alone");
  assert.match(text("start-setup-notes"),
    /LIVE needs NSE UPSTOX LIVE ACCESS TOKEN, NASDAQ ALPACA BROKER LIVE/,
    "and reports every market's missing live credential, not just the first");

  // (f) LIVE completes only when every ticked market has its live credential.
  liveState.NASDAQ.liveCredential = true;
  liveState.NSE.credential = true;
  liveState.NSE.liveCredential = true;
  refreshCredentialRows();
  await reloadStart();
  assert.equal(stepDone(3), true, "LIVE completes when each selected market has its live credential");

  // (g) Switching PAPER <-> LIVE recalculates rather than trusting a stale tick.
  pickMode("PAPER");
  await flush();
  liveState.NASDAQ.liveCredential = false;
  liveState.NASDAQ.credential = true;
  refreshCredentialRows();
  await reloadStart();
  pickMode("PAPER");
  await flush();
  assert.equal(stepDone(3), true, "PAPER ignores a missing live credential");
  pickMode("LIVE");
  await flush();
  assert.equal(stepDone(3), false, "switching to LIVE re-checks and drops the step immediately");

  // (h) Unknown credential status never reads as satisfied. Isolate it: only
  //     the market-data presence is unknown, so nothing else is missing.
  pickMode("PAPER");
  liveState.NSE.credential = true;
  liveState.NASDAQ.credential = null;   // present === null, not false
  liveState.NASDAQ.liveCredential = false;
  refreshCredentialRows();
  await reloadStart();
  assert.equal(stepDone(3), false, "an unknown credential status leaves step 3 incomplete");
  assert.match(text("start-setup-notes"), /Credential status cannot be confirmed/,
    "an unknown status explains itself rather than silently passing");

  // (i) No market ticked -> both steps incomplete and explained.
  $("start-select-NSE").checked = false;
  $("start-select-NSE").dispatchEvent(new window.Event("change", { bubbles: true }));
  $("start-select-NASDAQ").checked = false;
  $("start-select-NASDAQ").dispatchEvent(new window.Event("change", { bubbles: true }));
  await flush();
  assert.equal(stepDone(3), false, "no ticked market means no credential step");
  assert.equal(stepDone(4), false, "no ticked market means no strategy step");
  assert.match(text("start-setup-notes"), /Tick at least one market card/);

  // (j) A failed server read must clear every payload-derived indicator.
  //     This test asserts its own precondition so it cannot silently pass by
  //     skipping: steps 3 and 4 must genuinely be complete first.
  $("start-select-NSE").checked = true;
  $("start-select-NSE").dispatchEvent(new window.Event("change", { bubbles: true }));
  $("start-select-NASDAQ").checked = true;
  $("start-select-NASDAQ").dispatchEvent(new window.Event("change", { bubbles: true }));
  liveState.NSE.credential = true;
  liveState.NSE.liveCredential = true;
  liveState.NASDAQ.credential = true;
  liveState.NASDAQ.liveCredential = true;
  refreshCredentialRows();
  pickMode("PAPER");
  await reloadStart();
  // Preconditions, asserted — not assumed.
  assert.equal(stepDone(3), true, "PRECONDITION: step 3 must be complete before the failure");
  assert.equal(stepDone(4), true, "PRECONDITION: step 4 must be complete before the failure");

  const savedFetch = window.fetch;
  window.fetch = (url, opts) => (String(url).includes("/api/markets/start-monitor")
    ? Promise.reject(new Error("network down")) : savedFetch(url, opts));
  $("refresh-all").dispatchEvent(new window.Event("click", { bubbles: true }));
  await flush(40);
  window.fetch = savedFetch;

  assert.equal(stepDone(3), false, "a failed reload must clear step 3");
  assert.equal(stepDone(4), false, "a failed reload must clear step 4");
  assert.equal(stepDoneCount(), 0, "no step may remain complete after the payload is gone");
  assert.match(text("start-setup-notes"), /Server status unavailable/,
    "and the panel says why, instead of showing stale completion");

  // Restore the fixture baseline the remaining sections assume.
  resetSetup();
  refreshCredentialRows();
  await reloadStart();

  /* (l) Fail-closed credential contract. Each malformed shape must leave step 3
   * incomplete with an explanation — never satisfied. */
  const pullSelection = (keep) => {
    for (const market of ["NSE", "NASDAQ"]) {
      $(`start-select-${market}`).checked = market === keep;
      $(`start-select-${market}`).dispatchEvent(new window.Event("change", { bubbles: true }));
    }
  };
  const contractCase = async (name, requirement, expectation) => {
    liveState.NSE.overrideRequirements = requirement;
    liveState.NSE.credential = true;
    pullSelection("NSE");
    await reloadStart();
    assert.equal(stepDone(3), false, `${name}: step 3 must stay incomplete`);
    assert.match(text("start-setup-notes"), expectation, `${name}: must explain why`);
  };
  // Only NSE is ticked for these cases, so the contract under test is isolated.
  liveState.NSE.credentialRowsOk = true;
  pickMode("PAPER");

  // A well-formed base contract; each case breaks exactly one rule of it.
  const PAPER_OK = {
    PAPER: {
      required_slots: ["UPSTOX MARKET DATA"], consistent: true, available: true,
      roles: ["market_data"], source: "POST /api/markets/paper/session/start", note: "",
    },
    LIVE: {
      required_slots: ["UPSTOX MARKET DATA", "UPSTOX LIVE ACCESS TOKEN"],
      consistent: true, available: true, roles: ["market_data", "live_execution"],
      source: "gate", note: "",
    },
  };
  const withPaper = (over) => ({ ...PAPER_OK, PAPER: { ...PAPER_OK.PAPER, ...over } });

  await contractCase("empty array",
    withPaper({ required_slots: [] }), /not a non-empty array/);
  await contractCase("wrong type (string)",
    withPaper({ required_slots: "UPSTOX MARKET DATA" }), /not a non-empty array/);
  await contractCase("blank string",
    withPaper({ required_slots: ["   "] }), /non-string or blank entry/);
  await contractCase("non-string entry",
    withPaper({ required_slots: [42] }), /non-string or blank entry/);
  await contractCase("duplicate requirement",
    withPaper({ required_slots: ["UPSTOX MARKET DATA", "UPSTOX MARKET DATA"] }), /duplicate slot/);
  await contractCase("unknown slot",
    withPaper({ required_slots: ["NOT IN THE MATRIX"] }), /is not in the credential matrix/);
  await contractCase("inconsistent role assignment",
    withPaper({ consistent: false }), /inconsistent credential role assignment/);
  await contractCase("unresolvable slot",
    withPaper({ available: false }), /could not resolve the required credential slot/);
  await contractCase("missing requirements object",
    { ...(PAPER_OK || {}), PAPER: undefined }, /did not report credential requirements/);

  // Duplicate matrix rows must not silently pass.
  liveState.NSE.overrideRequirements = null;
  liveState.NSE.overrideRows = [
    { slot: "UPSTOX MARKET DATA", role: "market_data", present: true },
    { slot: "UPSTOX MARKET DATA", role: "market_data", present: true },
  ];
  pullSelection("NSE");
  await reloadStart();
  assert.equal(stepDone(3), false, "a duplicated matrix row must not satisfy step 3");
  assert.match(text("start-setup-notes"), /appears more than once|not in the credential matrix/);

  // A non-boolean presence must not be read as present.
  liveState.NSE.overrideRows = [{ slot: "UPSTOX MARKET DATA", role: "market_data", present: "yes" }];
  await reloadStart();
  assert.equal(stepDone(3), false, "a non-boolean presence must not satisfy step 3");
  assert.match(text("start-setup-notes"), /no boolean presence/);

  // Reordering the matrix rows must not change the outcome: slots are matched
  // by identity, not position.
  liveState.NSE.overrideRows = null;
  pullSelection("NSE");
  liveState.NSE.credential = true;
  await reloadStart();
  liveState.NSE.credentialRowsSnapshot = [
    { slot: "UPSTOX LIVE ACCESS TOKEN", role: "live_execution", present: true },
    { slot: "UPSTOX MARKET DATA", role: "market_data", present: true },
  ];
  const orderedOk = stepDone(3);
  assert.equal(orderedOk, true, "baseline: step 3 completes for a well-formed contract");
  const reversed = liveState.NSE.credentialRowsSnapshot;
  liveState.NSE.overrideRows = reversed;
  await reloadStart();
  assert.equal(stepDone(3), true, "reversing the credential rows must not change the result");
  liveState.NSE.overrideRows = null;
  liveState.NSE.overrideRequirements = null;
  pullSelection("NSE");
  resetSetup();
  await reloadStart();

  // (k) Presence-only: no credential *value* is ever rendered.
  const pageText = doc.body.textContent;
  assert(!pageText.includes("api_secret"), "no credential field name is rendered");
  assert(!pageText.includes("super-secret"), "the submitted token never comes back");

  // 12. Autopilot cannot be enabled where the server reports a blocker, and the
  //     page explains what automation may and may not do.
  doc.querySelector('[data-start-review="autopilot"][data-start-market="NSE"]')
    .dispatchEvent(new window.Event("click", { bubbles: true }));
  await flush(10);
  assert.match(text("start-autopilot-NSE"), /Autopilot unavailable on NSE/);
  assert.match(text("start-autopilot-may"), /hold at most the open-position cap/);
  assert.match(text("start-autopilot-may-not"), /turn a 100-symbol scan into 100 orders/);

  // 13. No order route, no live cycle route, no removed trading route anywhere.
  const forbidden = calls.filter((c) =>
    /\/api\/trading|\/api\/alpaca-broker\/orders|paper\/orders$|paper\/order$/.test(c.path));
  assert.deepEqual(forbidden, [], `the screen must not call an order or removed route: ${JSON.stringify(forbidden)}`);
  assert.equal(calls.filter((c) => /run-cycle/.test(c.path)).length, 0, "the page never runs a live cycle");

  console.log(`# start_monitor_smoke: OK (${calls.length} local API calls, ${calls.filter((c) => c.method !== "GET").length} writes, 0 order endpoints)`);
} finally {
  window.close();
}
