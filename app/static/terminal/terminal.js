"use strict";

/* AXIOM operator shell. No credential or API token is ever written to the DOM
 * by an API response, logged, exported, or persisted outside tab sessionStorage. */
const $ = (id) => document.getElementById(id);
const MODE_LABELS = {
  AXIOM_PAPER: "LOCAL PAPER",
};
const PAGE_TITLES = {
  start: ["PRIMARY OPERATOR SCREEN", "Start & Monitor"],
  overview: ["WORKSPACE", "Overview"], instruments: ["MULTI-MARKET UNIVERSE", "Watchlists & search"],
  charts: ["SUPERCHARTS & TICKET", "Supercharts & ticket"], orders: ["LEDGER & BROKER STATE", "Orders & positions"],
  strategies: ["MODES & STRATEGIES", "Strategies & modes"],
  risk: ["FAIL-CLOSED CONTROLS", "Risk & live gates"], connections: ["PROVIDER CONNECTIONS", "Connections"],
  activity: ["SHA-256 & LOCAL AUDIT", "Activity & audit"], settings: ["PREFERENCES & HELP", "Settings & manuals"],
  modellab: ["RESEARCH & KRONOS LAB", "AI & Model Lab"],
  monitor: ["READ-ONLY MONITOR", "Live Trade Monitor"],
  ensemble: ["PORTFOLIO ENSEMBLE", "Ensemble & Allocation"],
};
const UNRESOLVED = new Set([
  "INTENT_RECORDED", "SUBMISSION_ATTEMPTED", "MODIFICATION_ATTEMPTED", "UNKNOWN",
  "UNRESOLVED", "ACCEPTED_UNRECONCILED", "CANCEL_ATTEMPTED", "CANCEL_UNKNOWN",
]);
const state = {
  token: "", workspace: null, activity: [], connectionInfo: null,
  page: "start", mode: "AXIOM_PAPER", side: "BUY",
  contracts: {}, chart: { mode: "AXIOM_PAPER", feed: "legacy", symbol: "", timeframe: "1m", bars: [], quote: null, payload: null },
  chartView: { visible: 160, endOffset: 0, crosshair: true, drawings: {}, pending: null, geometry: null, pointer: null, drag: null, frame: null },
  chartSymbolsLoaded: false, chartRequest: 0,
  performance: { backend: null, browser: {} }, listedAssets: null, alpacaBroker: null, ticketReview: null,
  liveSwitch: {},
  mm: {
    available: false, activeVenue: "NSE", overview: null, catalogItems: [],
    side: "BUY", chartFeed: "SESSION", lastPreview: null,
    structuredLogs: null, structuredLogsError: "", decisionsPayload: null, selectedDecisionId: "",
  },
  ml: { available: false, overview: null, selectedRunId: "" },
  tm: { page: 1, pageSize: 50, summary: null, selected: null, chartPayload: null, hitBoxes: [], selectedMarkerId: "" },
  ens: { overview: null, selectedDecisionIdx: 0, latestAblation: null, latestBenchmark: null, scan: { data: null, cards: {}, loading: false, pollTimer: null } },
  dialog: null, dialogReturnFocus: null, refreshing: false, refreshTimer: null,
  submissionUncertain: false, lastError: null,
};

/* Every path this console calls, in one place.
 *
 * The legacy broker order surface was removed from the server; calling its
 * routes produced "Not Found" during token unlock. Each entry below is an
 * existing, authenticated route on the current server, and nothing here adds an
 * order endpoint: the console composes read-only paper/state views client-side
 * from routes the server already exposes.
 */
const ROUTES = Object.freeze({
  authProbe: "/paper/status",                   // authenticated + read-only; no broker call
  paperStatus: "/paper/status",
  paperAccount: "/paper/account",
  paperPositions: "/paper/positions",
  paperOrders: "/paper/orders?limit=200",
  paperTrades: "/paper/trades?limit=200",
  localHaltState: "/dashboard/data",            // durable legacy halt flag (read-only)
  auditEvents: "/api/markets/paper/audit/events?limit=150",
  performance: "/api/performance",
  credentialStore: "/api/connections/alpaca/credentials",   // presence + backend only
  emergencyStop: "/api/markets/emergency-stop",
  emergencyReset: "/api/markets/emergency-stop/reset",
  engineStop: "/engine/stop",
  engineResume: "/engine/resume",
  multiMarketOverview: "/api/markets/overview",
  liveReadinessReport: "/api/markets/live/readiness",   // read-only: blockers + exit facts, no broker order call
  listings: "/api/listed-assets/status",
  alpacaBrokerStatus: "/api/alpaca-broker/status",
});

/* Data the current server cannot supply to this console. Shown in the UI so a
 * gap is never rendered as a zero, a blank, or a fresh value. */
const UNAVAILABLE_DATA = Object.freeze({
  intents: "No order-intent feed endpoint exists on the current server, so unresolved intents cannot be counted or auto-cleared here.",
  quotes: "No bulk quote-map endpoint exists on the current server; read quotes on Supercharts or Watchlists.",
  reconciled: "The current server exposes no reconciliation timestamp for this paper workspace.",
});

/* Turn a failed auth probe into a distinct, actionable message. The three cases
 * below must never collapse into one another or into a generic failure. */
function authFailure(error) {
  const status = Number(error?.status || 0);
  const detail = String(error?.message || "").slice(0, 400);
  if (error?.network) {
    return { kind: "unreachable", message: "The local AXIOM server is not reachable. Start it, then unlock again." };
  }
  if (status === 401) {
    return { kind: "wrong_token", message: "The API token was rejected (401). Check the API_TOKEN value configured on the server." };
  }
  if (status === 503) {
    return {
      kind: "token_not_configured",
      message: "This server has no API_TOKEN configured (503), so protected routes are closed. Set API_TOKEN and restart AXIOM.",
    };
  }
  if (status === 404) {
    return {
      kind: "route_missing",
      message: `This server build does not expose ${ROUTES.authProbe} (404). The console and server versions differ; update the installed AXIOM.`,
    };
  }
  return { kind: "other", message: detail || "Token check failed." };
}

class ApiError extends Error {
  constructor(message, status = 0, network = false) {
    super(message);
    this.status = status;
    this.network = network;
  }
}

function storageGet(key) {
  try { return sessionStorage.getItem(key) || ""; } catch (_) { return ""; }
}
function storageSet(key, value) {
  try { if (value) sessionStorage.setItem(key, value); else sessionStorage.removeItem(key); } catch (_) { /* tab storage may be disabled */ }
}
function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
}
function safeDetail(payload, fallback = "Request failed") {
  if (!payload) return fallback;
  if (typeof payload === "string") return payload.slice(0, 1200);
  if (Array.isArray(payload.issues) && payload.issues.length) return payload.issues.join("; ").slice(0, 1600);
  const detail = payload.detail;
  if (typeof detail === "string") return detail.slice(0, 1200);
  if (Array.isArray(detail)) return detail.map((item) => {
    const where = Array.isArray(item.loc) ? item.loc.filter((part) => part !== "body").join(".") : "";
    return `${where ? `${where}: ` : ""}${item.msg || "Invalid input"}`;
  }).join("; ").slice(0, 1600);
  return fallback;
}
async function api(path, options = {}) {
  const headers = { Accept: "application/json", ...(options.headers || {}) };
  const token = options.token || state.token;
  if (token) headers.Authorization = `Bearer ${token}`;
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  let response;
  try {
    response = await fetch(path, {
      method: options.method || (options.body !== undefined ? "POST" : "GET"),
      headers, body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
      cache: "no-store", credentials: "same-origin",
    });
  } catch (_) {
    throw new ApiError("Local AXIOM server could not be reached; outcome is not confirmed", 0, true);
  }
  let payload = null;
  try { payload = await response.json(); } catch (_) { payload = null; }
  if (!response.ok) {
    if (response.status === 401 && !options.keepAuth) showAuth("The local API token was rejected or expired.");
    throw new ApiError(safeDetail(payload, `HTTP ${response.status}`), response.status, false);
  }
  return payload;
}
function toast(title, detail = "", kind = "ok", timeout = 6500) {
  const item = document.createElement("div");
  item.className = `toast ${kind}`;
  const strong = document.createElement("strong"); strong.textContent = title;
  const text = document.createElement("div"); text.textContent = String(detail || "");
  item.append(strong, text); $("toasts").appendChild(item);
  if (timeout) setTimeout(() => item.remove(), timeout);
}
function fmt(value, digits = 4) {
  const number = Number(value);
  if (value === null || value === undefined || value === "" || !Number.isFinite(number)) return "—";
  return number.toLocaleString(undefined, { maximumFractionDigits: digits });
}
function money(value, currency = "") {
  const number = Number(value);
  if (value === null || value === undefined || !Number.isFinite(number)) return "—";
  return `${number.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })} ${currency || ""}`.trim();
}
function timeText(value) {
  if (!value) return "Never / unavailable";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}
function ageSeconds(value) {
  const stamp = new Date(value || 0).getTime();
  return Number.isFinite(stamp) && stamp > 0 ? (Date.now() - stamp) / 1000 : Infinity;
}
function statusPill(text, kind = "") { return `<span class="status-pill ${kind}">${escapeHtml(text)}</span>`; }
// Corporate-action basis for a research dataset: verified splits/dividends only.
// Raw bars always back the paper path; the adjusted series is derived per research run.
function corporateActionCell(dataset) {
  const block = dataset && typeof dataset.corporate_actions === "object" && dataset.corporate_actions
    ? dataset.corporate_actions
    : null;
  const status = String((block && block.status) || dataset?.corporate_action_status || "UNVERIFIED");
  const detail = [];
  if (block) {
    detail.push(`policy ${block.policy || "RAW"}`);
    if (block.actions_sha256) detail.push(`actions ${String(block.actions_sha256).slice(0, 12)}…`);
    if (block.provider && block.provider.name) detail.push(`source ${block.provider.name} ${block.provider.version || ""}`.trim());
    if (Array.isArray(block.unsupported_actions) && block.unsupported_actions.length) {
      detail.push(`unsupported: ${block.unsupported_actions.map((a) => a.action_type).join(", ")}`);
    }
    if (Array.isArray(block.limitations) && block.limitations.length) {
      detail.push(`limitations: ${block.limitations.join(" | ")}`);
    }
  } else {
    detail.push("no verified corporate-action data");
  }
  const kind = status === "RAW" || status === "SPLIT_ADJUSTED" || status === "DIVIDEND_ADJUSTED"
    ? "ok"
    : status === "PARTIAL_UNSUPPORTED" ? "warn" : "err";
  return `<span title="${escapeHtml(detail.join(" · "))}">${statusPill(status, kind)}</span>`;
}
function setDisabled(id, disabled, reason = "") {
  const element = $(id);
  if (!element) return;
  element.disabled = Boolean(disabled);
  element.title = disabled && reason ? reason : "";
  if (disabled && reason) element.setAttribute("aria-description", `Disabled: ${reason}`);
  else element.removeAttribute("aria-description");
}
function emptyRow(id, columns, message) {
  $(id).innerHTML = `<tr><td colspan="${columns}" class="empty-state">${escapeHtml(message)}</td></tr>`;
}
function downloadJson(filename, value) {
  const blob = new Blob([JSON.stringify(value, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a"); link.href = url; link.download = filename;
  document.body.appendChild(link); link.click(); link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function newKey(prefix) {
  const id = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  return `${prefix}-${id}`.replace(/[^A-Za-z0-9_-]/g, "-").slice(0, 80);
}
function currentStatus() { return state.workspace?.status || { mode: "AXIOM_PAPER" }; }
function uncertainError(error) { return error?.network || error?.status >= 500; }
function observeBrowser(name, milliseconds) {
  if (!Number.isFinite(milliseconds) || milliseconds < 0) return;
  const samples = state.performance.browser[name] || [];
  samples.push(milliseconds);
  if (samples.length > 128) samples.splice(0, samples.length - 128);
  state.performance.browser[name] = samples;
}
function percentile(values, quantile) {
  if (!values.length) return null;
  const ordered = [...values].sort((a, b) => a - b);
  const position = (ordered.length - 1) * quantile;
  const lower = Math.floor(position), upper = Math.min(lower + 1, ordered.length - 1);
  return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower);
}

function showAuth(message = "") {
  closeDialog();
  state.token = ""; storageSet("axiom-api-token", "");
  $("app").hidden = true; $("auth-gate").hidden = false;
  $("auth-error").textContent = message;
  setTimeout(() => $("auth-token").focus(), 0);
}
async function unlock(token) {
  state.token = token;
  try {
    // Validated against an authenticated, read-only route: success proves only
    // that API_TOKEN is accepted. It never arms, activates or orders anything.
    await api(ROUTES.authProbe, { keepAuth: true });
    storageSet("axiom-api-token", token);   // this browser tab's sessionStorage only
    $("auth-token").value = ""; $("auth-error").textContent = "";
    $("auth-gate").hidden = true; $("app").hidden = false;
    applyPageChrome(state.page);
    await refreshAll();
    configureRefresh();
  } catch (error) {
    state.token = ""; storageSet("axiom-api-token", "");
    $("auth-gate").hidden = false; $("app").hidden = true;
    state.authFailure = authFailure(error);
    $("auth-error").textContent = state.authFailure.message;
  }
}

function openDialog({ title, body, confirmLabel = "Confirm", confirmClass = "danger", phrase = "", onConfirm, confirmDisabled = false }) {
  state.dialogReturnFocus = document.activeElement;
  state.dialog = { onConfirm, phrase, busy: false };
  $("modal-title").textContent = title;
  $("modal-body").innerHTML = body;
  $("modal-confirm").textContent = confirmLabel;
  $("modal-confirm").className = `button ${confirmClass}`;
  $("modal-confirm").disabled = confirmDisabled;
  $("modal-confirm").title = confirmDisabled ? "Resolve every review blocker first" : "";
  $("modal-error").textContent = "";
  $("modal-phrase-field").hidden = !phrase;
  $("modal-phrase").value = "";
  $("modal-phrase-label").textContent = phrase ? `Type exactly: ${phrase}` : "Confirmation phrase";
  $("modal").hidden = false;
  setTimeout(() => (phrase ? $("modal-phrase") : $("modal").querySelector("input, button, select") || $("modal-confirm")).focus(), 0);
}
function closeDialog() {
  if (state.dialog?.busy) return;
  $("modal").hidden = true; state.dialog = null;
  if (state.dialogReturnFocus?.isConnected) state.dialogReturnFocus.focus();
}
async function confirmDialog() {
  const dialog = state.dialog;
  if (!dialog || dialog.busy) return;
  if (dialog.phrase && $("modal-phrase").value !== dialog.phrase) {
    $("modal-error").textContent = `Type the exact phrase: ${dialog.phrase}`;
    $("modal-phrase").focus(); return;
  }
  dialog.busy = true; $("modal-confirm").disabled = true; $("modal-cancel").disabled = true; $("modal-close").disabled = true;
  try {
    await dialog.onConfirm();
    if (state.dialog === dialog) { state.dialog = null; $("modal").hidden = true; }
  } catch (error) {
    if (uncertainError(error)) {
      state.dialog = null; $("modal").hidden = true;
      state.submissionUncertain = true;
      toast("Outcome not confirmed", "Do not repeat the action blindly. Refresh and reconcile the persisted/broker state first.", "warn", 12000);
      await loadWorkspace({ background: true }).catch(() => {});
      renderAll();
    } else {
      $("modal-error").textContent = error.message || "Action failed";
    }
  } finally {
    dialog.busy = false;
    $("modal-cancel").disabled = false; $("modal-close").disabled = false;
    if (state.dialog === dialog) $("modal-confirm").disabled = false;
  }
}

/* The workspace is a CLIENT-SIDE view of existing read-only routes. No aggregate
 * endpoint was added, and no order route is involved. */
async function loadPaperSnapshot() {
  const settled = await Promise.allSettled([
    api(ROUTES.paperStatus), api(ROUTES.paperAccount), api(ROUTES.paperPositions),
    api(ROUTES.paperOrders), api(ROUTES.paperTrades), api(ROUTES.localHaltState),
  ]);
  const value = (index, fallback) => (settled[index].status === "fulfilled" ? settled[index].value : fallback);
  const failure = settled.find((entry) => entry.status === "rejected");
  const status = value(0, null);
  const account = value(1, null);
  const positions = value(2, { positions: [] });
  const orders = value(3, { orders: [] });
  const trades = value(4, { trades: [] });
  const halt = value(5, {});
  if (status === null || account === null) {
    // Paper state is the console's core view: refuse to render a half-workspace.
    throw failure?.reason || new ApiError("Local paper state is unavailable", 0, false);
  }
  const paperState = String(status.state || "STOPPED");
  const currency = account.currency || "";
  return {
    /* Only fields the current routes genuinely provide are filled. Anything the
     * server cannot supply stays null and is labelled in the UI (UNAVAILABLE_DATA). */
    status: {
      mode: "AXIOM_PAPER",
      engine_state: paperState,
      emergency_stop: Boolean(halt.emergency_stop),
      emergency_reason: halt.emergency_reason || null,
      live_trading: false,
      paper_mode: true,
    },
    paper_session: {
      state: paperState,
      provider: status.provider || null,
      feed_kind: status.feed_kind || null,
      strategy: status.strategy || null,
      automation_enabled: status.automation_enabled === true,
      review_policy: status.review_policy || null,
      stop_reason: status.stop_reason || null,
      current_run: status.current_run || null,
      last_run: status.last_run || null,
      symbols: status.symbols || [],
      connection: status.connection || null,
      processed_bars: status.processed_bars ?? null,
    },
    account: {
      currency, cash: account.cash, equity: account.equity,
      margin_free: account.cash, realized_pnl: account.realized_pnl,
      unrealized_pnl: account.unrealized_pnl, daily_pnl: account.daily_pnl,
      drawdown: account.drawdown, fees: account.fees,
    },
    account_currency: currency,
    positions: positions.positions || [],
    orders: orders.orders || [],
    deals: trades.trades || [],
    pnl: {
      unrealized: account.unrealized_pnl,
      realized_visible_history: account.realized_pnl,
    },
    intents: null,             // not exposed by this server build
    intents_available: false,
    quotes: {},                // no bulk quote route; panel explains the gap
    reconciled_at: null,       // no reconciliation timestamp on this build
    broker_state_available: true,
  };
}
async function loadWorkspace({ background = false } = {}) {
  if (state.refreshing) return state.workspace;
  state.refreshing = true;
  try {
    const workspace = await loadPaperSnapshot();
    const previousMode = state.mode;
    state.workspace = workspace; state.mode = "AXIOM_PAPER"; state.lastError = null;
    if (previousMode && previousMode !== state.mode) resetModeLocalState();
    if (state.submissionUncertain) {
      // Fail closed: without an intent feed the console cannot prove resolution,
      // so an uncertain outcome stays flagged until the operator locks/reloads.
      const intents = workspace.intents;
      if (Array.isArray(intents)) {
        const unresolved = intents.some((item) => UNRESOLVED.has(item.status));
        if (!unresolved && workspace.broker_state_available !== false) state.submissionUncertain = false;
      }
    }
    renderAll();
    return workspace;
  } catch (error) {
    state.lastError = error.message;
    if (!background) toast("Workspace refresh failed", error.message, "err");
    renderFreshness(); throw error;
  } finally { state.refreshing = false; }
}
async function loadActivity() {
  try {
    // Hash-chained local paper audit (read-only). Mapped, not reshaped, server-side.
    const result = await api(ROUTES.auditEvents);
    state.activity = (result.events || []).map((event) => ({
      event_type: event.event_kind || event.event_type || "AUDIT",
      timestamp: event.created_at || event.timestamp || null,
      environment: event.market ? `LOCAL PAPER · ${event.market}` : "LOCAL PAPER",
      payload: event.payload || {},
      seq: event.seq,
      entry_hash: event.entry_hash,
    }));
    renderActivity(); renderOverviewActivity();
  } catch (error) { toast("Activity refresh failed", error.message, "err"); }
  await loadStructuredPaperLogs();
}
async function loadConnectionInfo() {
  // Presence + storage backend only. The response never carries a secret.
  try {
    const store = await api(ROUTES.credentialStore);
    const backend = String(store?.backend || "unavailable");
    state.connectionInfo = {
      secret_store: backend === "windows-dpapi" ? "Windows DPAPI protected store"
        : backend === "owner-0600-file" ? "Owner-only file store (0600)"
          : "Protected store unavailable",
      secret_store_available: backend === "windows-dpapi" || backend === "owner-0600-file",
      backend,
      data_credential_present: Boolean(store?.present ?? store?.credential_present),
    };
  } catch (error) {
    state.connectionInfo = { secret_store: "Protected store unavailable", secret_store_available: false, backend: "unavailable" };
  }
  renderConnections();
}
async function loadPerformance() {
  try { state.performance.backend = await api(ROUTES.performance); }
  catch (error) { state.performance.backend = { error: error.message, metrics: {} }; }
  renderPerformance();
}
async function loadListedAssets() {
  try { state.listedAssets = await api(ROUTES.listings); }
  catch (error) { state.listedAssets = { error: error.message, execution_supported: false }; }
  renderListedAssets();
}

async function loadBrokerDestination() {
  // Broker path is optional; absence must never break the console.
  try { state.alpacaBroker = await api(ROUTES.alpacaBrokerStatus); }
  catch (error) { state.alpacaBroker = null; }
  renderMode();
}
async function refreshAll() {
  await loadWorkspace();
  await Promise.all([loadActivity(), loadConnectionInfo(), loadPerformance(), loadListedAssets(), loadBrokerDestination(), loadMultiMarketOverview(), loadModelLabOverview(), loadStartMonitor({ background: true })]);
  if (state.page === "start") await loadStartMonitorRows();
  if (state.page === "instruments" && state.mm.available) await searchMultiMarketCatalog();
  if (state.page === "charts") {
    if (state.mm.chartFeed !== "SESSION" && state.mm.available) await loadMultiMarketChart();
    else await loadChartSymbols();
  }
  if (state.page === "monitor") {
    await loadTradeMonitor();
    if (state.tm.selected) await loadTradeMonitorChart(state.tm.selected);
  }
  if (state.page === "ensemble") await loadEnsembleOverview();
}
function resetModeLocalState() {
  state.ticketReview = null;
  state.contracts = {};
  state.chart = { mode: "AXIOM_PAPER", feed: "legacy", symbol: "", timeframe: "1m", bars: [], quote: null, payload: null };
  state.chartView.endOffset = 0; state.chartView.pending = null; state.chartView.pointer = null; state.chartView.drag = null;
  state.chartSymbolsLoaded = false; state.submissionUncertain = false; state.chartRequest += 1;
  drawChart();
}
function configureRefresh() {
  clearInterval(state.refreshTimer);
  const interval = Number($("refresh-interval").value || 0);
  if (interval > 0) state.refreshTimer = setInterval(() => {
    if (!document.hidden && !state.dialog?.busy) {
      loadWorkspace({ background: true }).catch(() => {});
      if (state.mm.available) loadMultiMarketOverview().catch(() => {});
    }
  }, interval);
}

function renderAll() {
  if (!state.workspace) return;
  renderMode(); renderBoundary(); renderOverview(); renderOrders(); renderRisk(); renderConnections(); renderChartContext(); renderActivity();
}
function badgeClass() { return "paper"; }
function renderMode() {
  const status = currentStatus(); const label = MODE_LABELS.AXIOM_PAPER;
  const paperSession = state.workspace.paper_session || {};
  const paperState = paperSession.state || "STOPPED";
  const automatedPaper = paperSession.automation_enabled === true;
  for (const id of ["global-mode", "overview-mode"]) {
    if ($(id)) { $(id).className = `mode-badge ${badgeClass()}`; $(id).textContent = label; }
  }
  if ($("session-mode-summary")) {
    const mmProviders = state.mm.overview?.providers?.providers || [];
    const nseState = mmProviders.find((p) => p.id === "upstox_market_data")?.connection_state || "DISCONNECTED";
    const usState = mmProviders.find((p) => p.id === "alpaca_market_data")?.connection_state || "DISCONNECTED";
    $("session-mode-summary").textContent = `${label} · NSE (${nseState}) · NASDAQ (${usState})`;
  }
  // Broker destination (Alpaca PAPER / LIVE): account mode and endpoint are
  // always visible; LIVE is unmistakably marked as real money.
  const broker = state.alpacaBroker;
  if ($("broker-destination")) {
    if (!broker) {
      $("broker-destination").textContent = "ALPACA BROKER · disabled";
      $("broker-destination").className = "broker-destination";
    } else {
      const gateState = broker.gate?.state || "DISABLED";
      $("broker-destination").textContent =
        `${broker.mode || "ALPACA BROKER"} · ${broker.endpoint_host || "no endpoint"} · gate ${gateState}`;
      $("broker-destination").className =
        `broker-destination ${broker.is_real_money ? "live" : ""}`;
    }
  }
  $("global-connection").textContent = "Local simulated fills";
  $("global-connection-dot").className = "connection-dot ok";
  $("global-server").textContent = "NSE (Upstox) · NASDAQ (Alpaca)";
  $("global-account").textContent = "Local paper · INR & USD";
  const lifecycle = status.emergency_stop ? "EMERGENCY STOP" : `PAPER ${paperState}`;
  $("global-lifecycle").textContent = lifecycle;
  $("global-lifecycle").className = `status-pill ${status.emergency_stop || paperState === "ERROR" ? "err" : paperState === "RUNNING" ? "ok" : paperState === "PAUSED" ? "warn" : ""}`;
  $("global-lock-note").textContent = status.emergency_stop
    ? "Durable stop blocks new entries across restart."
    : automatedPaper ? `${paperSession.strategy} can create local simulated orders automatically; no broker routing.`
      : paperSession.review_policy === "MANUAL_ORDER_REVIEW" ? "Manual-only local paper; this console reviews each manual order."
        : "Independent local paper; Upstox (NSE) and Alpaca (NASDAQ) live execution OFF by default.";
  $("sidebar-automation").textContent = automatedPaper
    ? `${paperSession.strategy} automation ON · local simulated orders only.`
    : paperSession.review_policy === "MANUAL_ORDER_REVIEW" ? "manual_only automation OFF · this console reviews manual orders."
      : "No active paper strategy. Default replay enables moving_average automation.";
  $("ticket-mode").textContent = label; $("ticket-mode").className = "status-pill warn";
  $("deals-label").textContent = "LOCAL SIMULATED FILLS";
  document.body.dataset.mode = "AXIOM_PAPER";
  renderFreshness(); renderBoundary();
}
function renderBoundary() {
  if (!state.workspace) return;
  const status = currentStatus();
  const session = state.workspace.paper_session || {};
  const paperState = session.state || "STOPPED";
  const stop = session.stop_reason ? ` · ${session.stop_reason}` : "";
  const automated = session.automation_enabled === true;
  const manualOnly = session.review_policy === "MANUAL_ORDER_REVIEW";
  const replay = session.feed_kind === "LOCAL_REPLAY" && session.strategy;
  $("boundary-execution").textContent = "LOCAL PAPER · SIMULATED";
  $("boundary-execution-note").textContent = "Local simulated strategy/manual fills only; Upstox & Alpaca live routing gated OFF by default";
  $("boundary-data").textContent = replay ? `LOCAL REPLAY · ${paperState}${stop}`
    : session.strategy ? `READ-ONLY STREAM · ${paperState}${stop}` : "UPSTOX (NSE) · ALPACA (NASDAQ) · LOCAL PAPER";
  $("boundary-data-note").textContent = replay
    ? `Sample data only · ${session.strategy} · ${session.automation_enabled ? "automated local simulation" : "manual-only"}`
    : session.strategy ? "Read-only input feeding local simulation; not a broker execution session"
      : "Default replay is sample data and enables moving_average automation; configure Upstox & Alpaca feeds on Connections";
  $("boundary-auth").textContent = status.emergency_stop ? "EMERGENCY STOP"
    : automated ? (session.feed_kind === "LOCAL_REPLAY" ? "REPLAY STRATEGY · AUTOMATION ON" : "LOCAL STRATEGY · AUTOMATION ON")
      : manualOnly ? "LOCAL REVIEW · AUTOMATION OFF" : "LOCAL PAPER · NO ACTIVE STRATEGY";
  $("boundary-auth-note").textContent = status.emergency_stop ? "Durable stop blocks new entries and strategy resume"
    : automated ? `${session.strategy} can create simulated orders/fills automatically · ${paperState}${stop} · no broker routing`
      : manualOnly ? "manual_only creates no strategy orders; this console reviews each manual simulated order"
        : "Default replay enables moving_average automation; manual tickets are separately reviewed";
}
function renderFreshness() {
  if (!state.workspace) return;
  if (state.lastError) {
    $("global-freshness").innerHTML = statusPill("REFRESH FAILED · LAST VIEW MAY BE STALE", "err");
    $("global-freshness").title = state.lastError; return;
  }
  $("global-freshness").innerHTML = statusPill("LOCAL PAPER · SIMULATED", "warn");
}

function renderOverview() {
  const workspace = state.workspace; const status = currentStatus(); const account = workspace.account || {};
  const currency = account.currency || workspace.account_currency || "";
  $("metric-equity").textContent = money(account.equity, currency);
  $("metric-currency").textContent = currency ? `${currency} local paper` : "local simulated account";
  const positions = workspace.positions || [];
  const unrealized = workspace.pnl?.unrealized ?? (
    workspace.broker_state_available === false ? null
      : positions.reduce((sum, item) => sum + Number(item.unrealized_pnl ?? item.profit ?? 0) + Number(item.swap || 0), 0)
  );
  $("metric-unrealized").textContent = money(unrealized, currency);
  $("metric-unrealized-note").textContent = "All current local open positions · not this run only";
  $("metric-realized").textContent = money(workspace.pnl?.realized_visible_history, currency);
  $("realized-window").textContent = "All persisted local history · survives restart";
  $("metric-margin").textContent = money(account.margin_free ?? account.cash, currency);
  $("metric-margin-note").textContent = "local simulated cash";
  const mmProviders = state.mm.overview?.providers?.providers || [];
  const nseConn = mmProviders.find((p) => p.id === "upstox_market_data")?.connection_state || "NOT_CONFIGURED";
  const usConn = mmProviders.find((p) => p.id === "alpaca_market_data")?.connection_state || "NOT_CONFIGURED";
  const alpacaGate = state.alpacaBroker ? `${state.alpacaBroker.mode || "ALPACA"} · gate ${state.alpacaBroker.gate?.state || "DISABLED"}` : "Disabled";
  const connectionRows = [
    ["Execution", MODE_LABELS.AXIOM_PAPER],
    ["NSE (INR ₹)", `Upstox · ${nseConn}`],
    ["NASDAQ (USD $)", `Alpaca · ${usConn}`],
    ["Alpaca Broker", alpacaGate],
    ["Reconciled", workspace.reconciled_at ? timeText(workspace.reconciled_at) : `Not reported — ${UNAVAILABLE_DATA.reconciled}`],
    ["Detail", "Local paper + NSE/NASDAQ read-only market data"],
  ];
  $("overview-connection").innerHTML = connectionRows.map(([key, value]) => `<dt>${escapeHtml(key)}</dt><dd>${escapeHtml(value)}</dd>`).join("");
  const intentsKnown = Array.isArray(workspace.intents);
  const unresolved = intentsKnown ? workspace.intents.filter((item) => UNRESOLVED.has(item.status)).length : null;
  const session = workspace.paper_session || {};
  const guardLabel = status.emergency_stop ? "Durable emergency stop is SET"
    : `${session.state || "STOPPED"} · ${session.automation_enabled ? `${session.strategy} automation ON` : session.review_policy === "MANUAL_ORDER_REVIEW" ? "manual-only · automation OFF" : "no active strategy"}`;
  $("overview-risk").innerHTML = `
    <div class="inline-status ${status.emergency_stop ? "err" : "warn"}">${escapeHtml(guardLabel)}</div>
    <dl class="detail-list"><dt>NSE &amp; NASDAQ risk bounds</dt><dd>Per-market INR &amp; USD caps</dd><dt>Unresolved intents</dt><dd>${intentsKnown ? unresolved : escapeHtml("Unavailable — " + UNAVAILABLE_DATA.intents)}</dd><dt>Live placement</dt><dd>${state.alpacaBroker?.gate?.state === "ACTIVE" ? "Alpaca active" : "Blocked by default"}</dd></dl>`;
  const quotes = workspace.quotes || {};
  const quoteEntries = Object.entries(quotes);
  if (!quoteEntries.length) state.lastQuoteNote = UNAVAILABLE_DATA.quotes;
  $("overview-quotes").innerHTML = quoteEntries.length ? quoteEntries.map(([symbol, quote]) => {
    const age = ageSeconds(quote.timestamp);
    return `<div class="quote-row"><div><strong>${escapeHtml(symbol)}</strong><small>${escapeHtml(timeText(quote.timestamp))}</small></div><div><strong>${fmt(quote.bid)} / ${fmt(quote.ask)}</strong><small>${Number.isFinite(age) ? `${age.toFixed(1)}s old` : "freshness unknown"}</small></div></div>`;
  }).join("") : `<p class="empty-state">${escapeHtml(UNAVAILABLE_DATA.quotes)}</p>`;
  const checks = operatorChecklist();
  $("overview-checklist").innerHTML = checks.map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  renderPaperRun(); renderOverviewActivity();
}
function renderPaperRun() {
  const panel = $("paper-run-panel");
  if (!panel) return;
  panel.hidden = false;
  const session = state.workspace.paper_session || {};
  const current = session.current_run;
  const run = current || session.last_run;
  const currency = state.workspace.account?.currency || "";
  if (!run) {
    $("paper-run-title").textContent = "No current paper run";
    $("paper-run-state").textContent = session.state || "STOPPED";
    $("paper-run-summary").textContent = "No current or persisted run summary. Account cards above are persisted local totals, not a new run result.";
    $("paper-run-details").innerHTML = "";
    $("paper-run-pnl-note").textContent = "Start automated replay or submit a reviewed manual paper order to create a run-scoped result. No profitability claim.";
    return;
  }
  const currentProcessRun = Boolean(current);
  const active = currentProcessRun && run.active;
  $("paper-run-title").textContent = active ? "Current paper run" : currentProcessRun ? "Last run in this process" : "Last persisted run · not active";
  $("paper-run-state").textContent = `${run.state || "STOPPED"}${run.stop_reason ? ` · ${run.stop_reason}` : ""}`;
  $("paper-run-state").className = `status-pill ${run.state === "ERROR" ? "err" : active ? "ok" : "warn"}`;
  $("paper-run-summary").textContent = `${run.feed_kind || "LOCAL PAPER"} · ${run.strategy || "no strategy"} · ${run.automation_enabled ? "AUTOMATION ON · automatic simulated orders possible" : "MANUAL-ONLY · automation OFF"}`;
  const scope = currentProcessRun ? "Current-run" : "Last-run";
  const source = run.data_source;
  const span = run.simulated_span || {};
  const runCurrency = run.currency || currency;
  const fields = [
    ["Run scope", currentProcessRun ? "This process only" : "Persisted completed run; no active process run"],
    ["Data source", source ? `${source.label}${source.kind === "LOCAL_CSV_REPLAY" ? (source.synthetic ? " · SYNTHETIC · NOT MARKET DATA" : " · LOCAL CSV · PROVENANCE UNVERIFIED") : ""}` : "Not recorded"],
    ["Simulated market span", `${timeText(span.first_bar_ts)} → ${timeText(span.last_bar_ts)}`],
    ["Starting capital", money(run.starting_capital, runCurrency)],
    ["Currency", runCurrency || "Not recorded"],
    ["Started / stopped", `${timeText(run.started_at)} / ${timeText(run.stopped_at)}`],
    ["Stop reason", run.stop_reason || "Not stopped"],
    ["Bars / actionable signals", `${run.processed_bars ?? 0} / ${run.actionable_signals ?? 0}`],
    ["Simulated orders / fills", `${run.orders_created ?? 0} / ${run.fills ?? 0}`],
    [`${scope} realized P&L`, money(run.realized_pnl, runCurrency)],
    [`${scope} unrealized P&L Δ`, money(run.unrealized_pnl_delta, runCurrency)],
    ["Ending unrealized · open positions", money(run.ending_unrealized_pnl, runCurrency)],
  ];
  $("paper-run-details").innerHTML = fields.map(([key, value]) => `<dt>${escapeHtml(key)}</dt><dd>${escapeHtml(value)}</dd>`).join("");
  const evidence = run.evidence_label ? `${run.evidence_label}. ` : "";
  $("paper-run-pnl-note").textContent = `${evidence}Run values are account-level deltas from that run’s start, not per-trade attribution. Realized/unrealized cards above include persisted local history/current open positions. All values are simulated—not broker or profitability evidence.`;
}
function operatorChecklist() {
  return [
    "Configure read-only Upstox (NSE · INR ₹) and Alpaca (NASDAQ · USD $) market-data credentials on 07 Connections; test probes never place orders.",
    "Use 02 Watchlists & search and 03 Supercharts & ticket to inspect NSE and NASDAQ quotes/candles and submit simulated INR or USD paper orders.",
    "Default local replay uses sample data with moving_average automation ON: it can create simulated strategy orders/fills without broker routing.",
    "Keep LIVE execution OFF unless readiness passes and you separately complete Arm and Activate for Upstox (/autonomy) or Alpaca (05 Strategies & automation).",
  ];
}
function eventName(event) { return event.event_type || event.kind || "EVENT"; }
function eventTime(event) { return event.timestamp || event.ts; }
function renderOverviewActivity() {
  if (!$("overview-activity")) return;
  const events = state.activity.slice(0, 6);
  if (!events.length) { emptyRow("overview-activity", 3, "No redacted audit events yet."); return; }
  $("overview-activity").innerHTML = events.map((event) => `<tr><td>${escapeHtml(timeText(eventTime(event)))}</td><td>${escapeHtml(eventName(event))}</td><td><small>${escapeHtml(JSON.stringify(event.payload || {}))}</small></td></tr>`).join("");
}

function renderOrders() {
  const workspace = state.workspace; const positions = workspace.positions || [];
  $("position-count").textContent = String(positions.length);
  if (!positions.length) emptyRow("position-rows", 8, "No positions in the current local paper view.");
  else $("position-rows").innerHTML = positions.map((position) => {
    const volume = position.volume ?? position.quantity;
    const ownership = "Local simulated position";
    const action = `<button class="button secondary" data-paper-exit="${escapeHtml(position.symbol)}">Review exit</button>`;
    return `<tr><td>${escapeHtml(position.symbol)}</td><td>${escapeHtml(position.side || "LONG")}</td><td>${fmt(volume, 8)}</td><td>${fmt(position.price_open ?? position.entry_price)} / ${fmt(position.price_current ?? position.mark)}</td><td>${fmt(position.unrealized_pnl ?? position.profit, 2)}</td><td>${fmt(position.stop_loss)} / ${fmt(position.take_profit)}</td><td><small>${escapeHtml(ownership)}</small></td><td>${action}</td></tr>`;
  }).join("");
  const orders = workspace.orders || [];
  if (!orders.length) emptyRow("order-rows", 8, "No local paper orders in this view.");
  else $("order-rows").innerHTML = orders.map((order) => {
    const id = String(order.id || order.intent_id || "");
    const active = ["NEW", "PARTIALLY_FILLED"].includes(order.status);
    const action = active ? `<button class="button danger-outline" data-cancel="${escapeHtml(id)}">Review cancel</button>` : `<small>No cancel action</small>`;
    return `<tr><td>${escapeHtml(timeText(order.created_ts || order.timestamp))}</td><td>${escapeHtml(order.symbol)}</td><td>${escapeHtml(order.side)} / ${escapeHtml(order.order_type)}</td><td>${fmt(order.volume ?? order.quantity, 8)}</td><td>${statusPill(order.status, UNRESOLVED.has(order.status) ? "err" : ["FILLED", "MODIFIED"].includes(order.status) ? "ok" : "warn")}</td><td>${escapeHtml(order.id || "—")}</td><td><small>${escapeHtml(order.reason || "Local simulation")}</small></td><td>${action}</td></tr>`;
  }).join("");
  const deals = workspace.deals || [];
  if (!deals.length) emptyRow("deal-rows", 7, "No simulated fills in the visible history.");
  else $("deal-rows").innerHTML = deals.map((deal) => `<tr><td>${escapeHtml(timeText(deal.timestamp))}</td><td>${escapeHtml(deal.symbol)}</td><td>${escapeHtml(deal.side)} / ${escapeHtml(deal.entry || (deal.reason === "signal" ? "SIMULATED" : "LOCAL"))}</td><td>${fmt(deal.volume ?? deal.quantity, 8)}</td><td>${fmt(deal.price)}</td><td>${fmt(deal.profit, 2)}</td><td>${fmt(deal.fee, 2)}</td></tr>`).join("");
}

function renderRisk() {
  const status = currentStatus();
  $("risk-emergency").textContent = status.emergency_stop ? "EMERGENCY STOP SET" : "STOP CLEAR";
  $("risk-emergency").className = `status-pill ${status.emergency_stop ? "err" : "ok"}`;
  const paperSession = state.workspace.paper_session || {};
  $("lifecycle-current").textContent = `PAPER · ${paperSession.state || "STOPPED"}${paperSession.stop_reason ? ` · ${paperSession.stop_reason}` : ""}`;
  const paperState = paperSession.state;
  setDisabled("pause-session", paperState !== "RUNNING", "Only a running paper strategy can pause.");
  $("pause-session").textContent = "Pause paper strategy";
  setDisabled("stop-session", paperState === "STOPPED", "Paper session is already stopped.");
  $("stop-session").textContent = "Stop paper session";
  setDisabled("clear-emergency", !status.emergency_stop, "Emergency stop is not set.");
  $("lifecycle-help").textContent = "Local paper uses the independent simulator. NSE (Upstox) and NASDAQ (Alpaca) live execution gates remain OFF by default and require separate readiness, Arm, and Activate confirmation.";
}

function renderConnections() {
  if (!$("secret-store-status")) return;
  const info = state.connectionInfo;
  if (!info) { $("secret-store-status").textContent = "STORE —"; $("secret-store-status").className = "status-pill"; return; }
  const available = info.secret_store_available === true;
  $("secret-store-status").textContent = available
    ? (info.backend === "windows-dpapi" ? "DPAPI PROTECTED" : "PROTECTED FILE STORE")
    : "PROTECTED STORE UNAVAILABLE";
  $("secret-store-status").className = `status-pill ${available ? "ok" : "err"}`;
  $("secret-store-status").title = `${info.secret_store || "unavailable"} · presence reported only, no secret is ever returned to this page`;
}

function renderPerformance() {
  const panel = $("performance-summary"); if (!panel) return;
  const backend = state.performance.backend || { metrics: {} };
  const backendRows = Object.entries(backend.metrics || {}).map(([name, row]) => ({
    name, ...row, source: "backend", note: backend.boundaries?.[name] || "Local server observation",
  }));
  const browserRows = Object.entries(state.performance.browser).filter(([, values]) => values.length).map(([name, values]) => ({
    name, samples: values.length, p50_ms: percentile(values, .5), p95_ms: percentile(values, .95), p99_ms: percentile(values, .99),
    source: "browser", note: name === "browser.chart.render" ? "Canvas command/render duration in this tab" : "Browser fetch + response handling; includes local API and scheduling",
  }));
  const rows = [...backendRows, ...browserRows].sort((a, b) => a.name.localeCompare(b.name)).slice(0, 18);
  if (!rows.length) {
    panel.innerHTML = `<p class="empty-state">${escapeHtml(backend.error || "No samples yet. Empty means not measured—not zero latency. Load a chart or perform a read-only review to generate relevant measurements.")}</p>`;
    return;
  }
  panel.innerHTML = rows.map((row) => `<article class="performance-card"><strong>${escapeHtml(row.name)}</strong><dl><div><dt>p50</dt><dd>${fmt(row.p50_ms, 2)}</dd></div><div><dt>p95</dt><dd>${fmt(row.p95_ms, 2)}</dd></div><div><dt>p99</dt><dd>${fmt(row.p99_ms, 2)}</dd></div><div><dt>n</dt><dd>${row.samples}</dd></div></dl><small>${escapeHtml(row.source)} · ${escapeHtml(row.note)}</small></article>`).join("");
}
function renderListedAssets() {
  const panel = $("listed-asset-status"); if (!panel) return;
  const info = state.listedAssets;
  if (!info) { panel.textContent = "Provider boundary has not been loaded."; return; }
  panel.className = `inline-status ${info.error ? "err" : "warn"}`;
  panel.textContent = info.error
    ? `Boundary status unavailable: ${info.error}. No fallback routing is enabled.`
    // A field the server did not send must never render as the word
    // "undefined": say the field is unavailable instead.
    : `${info.identity_contract || "Identity contract not reported"}. Data configured: ${info.data_configured ? "yes" : "no"}; execution supported: ${info.execution_supported ? "yes" : "no"}; CFD substitution: prohibited. ${info.note || ""}`;
  $("listed-scope-badge").textContent = info.execution_supported ? "PROVIDER VERIFICATION REQUIRED" : "NO LISTED EXECUTION";
}

function renderChartContract() {
  const panel = $("chart-contract"); if (!panel) return;
  panel.innerHTML = `<p class="empty-state">Local replay/CSV metadata is kept distinct from live NSE (Upstox) and NASDAQ (Alpaca) instrument metadata.</p>`;
}

function renderChartContext() {
  renderChartContract();
  const status = currentStatus();
  $("ticket-title").textContent = "Paper ticket · simulated";
  $("volume-label").textContent = "Quantity";
  $("protection-fields").hidden = true;
  $("order-disclaimer").textContent = "Paper fills are simulated locally. SELL exits an existing position; short selling is not modelled.";
  $("start-paper").hidden = false;
  const paperState = state.workspace.paper_session?.state;
  $("start-paper").textContent = paperState === "RUNNING" ? "Stop paper session" : paperState === "PAUSED" ? "Resume paper session" : "Start automated replay (moving_average)";
  const isLiveMmChart = ($("mm-chart-source-mode")?.value || "SESSION") !== "SESSION";
  const feedContext = $("chart-feed-context");
  if (feedContext && !isLiveMmChart) {
    feedContext.className = "inline-status warn";
    feedContext.textContent = "Replay/CSV is sample data, not market data. Default start enables moving_average automation and simulated strategy orders; Upstox (NSE) and Alpaca (NASDAQ) Live-Market Paper use separate read-only provider feeds.";
  }
  const noSymbol = !$("order-symbol").value;
  const disabledReason = state.submissionUncertain ? "An action outcome is uncertain. Refresh before another order." : noSymbol ? "Load/select an instrument first." : "";
  setDisabled("review-order", Boolean(disabledReason), disabledReason);
  setDisabled("reload-chart", !isLiveMmChart && !$("chart-symbol").value, (!isLiveMmChart && !$("chart-symbol").value) ? "No instrument is available." : "");
  $("order-context").className = `inline-status ${status.emergency_stop || state.submissionUncertain ? "err" : "warn"}`;
  $("order-context").textContent = state.submissionUncertain
    ? "An order/cancel outcome is uncertain. Refresh and inspect local paper state before another action."
    : status.emergency_stop ? "Durable emergency stop blocks new entries. Risk-reducing exits remain subject to review."
    : "Review shows local price/headroom. Final result is explicitly simulated.";
  $("order-buy").setAttribute("aria-pressed", String(state.side === "BUY"));
  $("order-sell").setAttribute("aria-pressed", String(state.side === "SELL"));
  document.querySelectorAll("[data-chart-timeframe]").forEach((button) => {
    const active = button.dataset.chartTimeframe === state.chart.timeframe;
    button.classList.toggle("active", active); button.setAttribute("aria-pressed", String(active));
  });
  for (const id of ["zoom-out", "zoom-in", "reset-chart-view", "clear-drawings"]) setDisabled(id, !(state.chart.bars || []).length, "Load valid chart bars first.");
  renderTicketEstimate();
}

function productLabel(type) {
  return ({ LISTED_SHARE: "Listed share · Upstox / Alpaca", LISTED_ETF: "Listed ETF · Upstox / Alpaca", EQUITY: "Listed equity", ETF: "Listed ETF", INDEX: "Market index", "LOCAL PAPER": "Local paper simulation", UNKNOWN: "Unknown · untradable" })[type] || String(type || "LOCAL PAPER").replaceAll("_", " ");
}

async function loadChartSymbols() {
  try {
    const result = await api("/chart/symbols");
    const symbols = result.symbols || []; const defaultSymbol = result.default || "";
    const timeframes = result.timeframes;
    const select = $("chart-symbol"); const previous = state.chart.symbol || select.value;
    select.innerHTML = symbols.length ? symbols.map((item) => `<option value="${escapeHtml(item.symbol)}">${escapeHtml(item.symbol)}</option>`).join("") : `<option value="">No instruments available</option>`;
    const chosen = symbols.some((item) => item.symbol === previous) ? previous : defaultSymbol;
    select.value = chosen || "";
    if (chosen) {
      const entry = symbols.find((item) => item.symbol === chosen);
      const available = entry?.timeframes || timeframes || [];
      if (available.length) {
        $("chart-timeframe").innerHTML = available.map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`).join("");
        if (!available.includes(state.chart.timeframe)) state.chart.timeframe = available[0];
        $("chart-timeframe").value = state.chart.timeframe;
      }
    }
    state.chartSymbolsLoaded = true;
    if (chosen && (!state.chart.payload || state.chart.symbol !== chosen || state.chart.feed !== "legacy")) await loadChart();
    else {
      $("order-symbol").value = chosen || "";
      state.chart.feed = "legacy";
      if (!chosen) {
        state.chart.bars = []; state.chart.payload = null; state.chart.quote = null; drawChart();
        $("chart-empty").textContent = "Start the paper session or load a configured dataset.";
      }
      renderChartContext();
    }
  } catch (error) {
    $("chart-source").textContent = "SYMBOLS UNAVAILABLE"; $("chart-source").className = "status-pill err";
    $("chart-empty").hidden = false; $("chart-empty").textContent = error.message;
    toast("Chart instruments unavailable", error.message, "err");
  }
}
function normalizeBars(raw) {
  if (Array.isArray(raw)) return raw.map((bar) => ({ ...bar, timestamp: bar.timestamp || bar.time }));
  if (!raw || !Array.isArray(raw.time)) return [];
  return raw.time.map((timestamp, index) => ({
    timestamp, open: raw.open[index], high: raw.high[index], low: raw.low[index], close: raw.close[index], volume: raw.volume?.[index] || 0,
  }));
}
async function loadChart() {
  const symbol = $("chart-symbol").value; const timeframe = $("chart-timeframe").value;
  if (!symbol) return;
  const requestId = ++state.chartRequest;
  setDisabled("reload-chart", true, "Loading chart…");
  $("chart-source").textContent = "LOADING LOCAL PAPER DATA"; $("chart-source").className = "status-pill warn";
  try {
    const query = new URLSearchParams({ symbol, timeframe, limit: "500", indicators: "" });
    const started = performance.now(); let payload;
    try { payload = await api(`/chart/data?${query}`); }
    finally { observeBrowser("browser.chart.load", performance.now() - started); }
    if (requestId !== state.chartRequest) return;
    const bars = normalizeBars(payload.bars);
    const previousKey = chartKey();
    state.chart = { mode: "AXIOM_PAPER", feed: "legacy", symbol, timeframe, bars, quote: payload.quote || null, payload };
    if (previousKey !== chartKey()) {
      state.chartView.endOffset = 0; state.chartView.pending = null; state.chartView.pointer = null;
      state.chartView.visible = Math.min(160, Math.max(20, bars.length || 20));
    }
    if (requestId !== state.chartRequest) return;
    $("order-symbol").value = symbol;
    $("chart-source").textContent = payload.data_feed_label || payload.source_label || "LOCAL PAPER DATA";
    $("chart-source").className = `status-pill ${payload.market_data ? "ok" : "warn"}`;
    $("chart-legend").textContent = `${bars.length} bars · ${payload.note || payload.data_feed_label || payload.source_label || "Confirmed source"} · generated ${timeText(payload.generated_at)}`;
    renderChartQuote(); drawChart(); renderChartContext(); updateOrderContract();
  } catch (error) {
    if (requestId !== state.chartRequest) return;
    if (state.chart.symbol !== symbol) {
      state.chart.bars = []; state.chart.payload = null; state.chart.quote = null;
      drawChart();
    }
    $("chart-source").textContent = "CHART LOAD FAILED";
    $("chart-source").className = "status-pill err";
    $("chart-empty").hidden = false; $("chart-empty").textContent = error.message;
    renderChartContext();
    toast("Chart load failed", error.message, "err");
  } finally { if (requestId === state.chartRequest) setDisabled("reload-chart", false); }
}
function renderChartQuote() {
  const chart = state.chart; const payload = chart.payload || {}; const quote = chart.quote;
  if (quote) {
    const age = ageSeconds(quote.timestamp);
    const maxAge = Number(state.chart.payload?.max_quote_age_seconds || 30);
    const fresh = quote.fresh !== false && quote.advancing !== false && age >= 0 && age <= maxAge;
    $("chart-quote").innerHTML = `<span><strong>${escapeHtml(chart.symbol)}</strong> ${escapeHtml(productLabel(payload.product_type))}</span><span>Bid <strong>${fmt(quote.bid)}</strong></span><span>Ask <strong>${fmt(quote.ask)}</strong></span><span>Spread ${fmt(Number(quote.ask) - Number(quote.bid), 8)}</span><span class="${fresh ? "status-pill ok" : "status-pill err"}">${fresh ? "Fresh quote" : "STALE / NOT ADVANCING"} ${Number.isFinite(age) ? `${age.toFixed(1)}s old` : "age unknown"}</span><span>Source ${escapeHtml(timeText(quote.timestamp))}</span><span>Received ${escapeHtml(timeText(quote.received_at))}</span>`;
  } else {
    const last = chart.bars.at(-1);
    $("chart-quote").innerHTML = `<span><strong>${escapeHtml(chart.symbol)}</strong></span><span>Last closed bar <strong>${fmt(last?.close)}</strong></span><span>${escapeHtml(payload.data_feed_label || "Local paper data")}</span><span>${last ? escapeHtml(timeText(last.timestamp)) : "No bar timestamp"}</span>`;
  }
}
function chartKey() {
  return `${state.chart.mode}|${state.chart.feed}|${state.chart.symbol}|${state.chart.timeframe}`;
}
function chartWindow() {
  const bars = state.chart.bars || [];
  if (!bars.length) return { bars: [], start: 0, end: 0, visible: 0, offset: 0 };
  const minimum = Math.min(20, bars.length);
  const visible = Math.max(minimum, Math.min(bars.length, Math.round(state.chartView.visible || 160)));
  const maxOffset = Math.max(0, bars.length - visible);
  const offset = Math.max(0, Math.min(maxOffset, Math.round(state.chartView.endOffset || 0)));
  const end = bars.length - offset; const start = Math.max(0, end - visible);
  state.chartView.visible = visible; state.chartView.endOffset = offset;
  return { bars: bars.slice(start, end), start, end, visible, offset };
}
function resetChartView() {
  const count = (state.chart.bars || []).length;
  state.chartView.visible = Math.min(160, Math.max(20, count || 20));
  state.chartView.endOffset = 0; state.chartView.pending = null; state.chartView.pointer = null;
  $("chart-cursor").textContent = ""; drawChart();
}
function zoomChart(factor, anchor = .5) {
  const bars = state.chart.bars || []; if (!bars.length) return;
  const view = chartWindow(); const minimum = Math.min(20, bars.length);
  const nextVisible = Math.max(minimum, Math.min(bars.length, Math.round(view.visible * factor)));
  const anchorIndex = view.start + view.visible * Math.max(0, Math.min(1, anchor));
  const nextStart = anchorIndex - nextVisible * Math.max(0, Math.min(1, anchor));
  state.chartView.visible = nextVisible;
  state.chartView.endOffset = Math.round(bars.length - (nextStart + nextVisible));
  chartWindow(); drawChart();
}
function panChart(offsetDelta) {
  if (!(state.chart.bars || []).length) return;
  state.chartView.endOffset = Math.round(state.chartView.endOffset + offsetDelta);
  chartWindow(); drawChart();
}
function indicatorSeries(kind, period) {
  const bars = state.chart.bars || []; const closes = bars.map((bar) => Number(bar.close));
  if (kind === "SMA") {
    let sum = 0;
    return closes.map((value, index) => {
      sum += value; if (index >= period) sum -= closes[index - period];
      return index >= period - 1 ? sum / period : null;
    });
  }
  const alpha = 2 / (period + 1); let previous = closes[0];
  return closes.map((value, index) => {
    previous = index === 0 ? value : alpha * value + (1 - alpha) * previous;
    return previous;
  });
}
function currentDrawings() {
  return state.chartView.drawings[chartKey()] || [];
}
function scheduleChartDraw() {
  if (state.chartView.frame !== null) return;
  const raf = globalThis.requestAnimationFrame || ((callback) => setTimeout(callback, 16));
  state.chartView.frame = raf(() => { state.chartView.frame = null; drawChart(); });
}
function drawIndicator(ctx, series, view, geometry, color) {
  const { left, top, plotW, plotH, max, min, start, end } = geometry;
  const step = plotW / Math.max(1, end - start);
  const y = (price) => top + (max - price) / (max - min || 1) * plotH;
  ctx.strokeStyle = color; ctx.lineWidth = 1.25; ctx.beginPath(); let open = false;
  for (let index = start; index < end; index += 1) {
    const value = series[index];
    if (!Number.isFinite(value)) { open = false; continue; }
    const x = left + (index - start + .5) * step;
    if (!open) { ctx.moveTo(x, y(value)); open = true; } else ctx.lineTo(x, y(value));
  }
  ctx.stroke();
}
function drawChart() {
  const startedAt = performance.now();
  const canvas = $("terminal-chart"); if (!canvas) return;
  const rect = canvas.getBoundingClientRect(); const width = Math.max(300, rect.width); const height = Math.max(250, rect.height);
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  canvas.width = Math.round(width * dpr); canvas.height = Math.round(height * dpr);
  const ctx = canvas.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height); ctx.fillStyle = "#131722"; ctx.fillRect(0, 0, width, height);
  const bars = state.chart.bars || [];
  $("chart-empty").hidden = bars.length > 0;
  canvas.dataset.tool = $("drawing-tool")?.value || "NONE";
  if (!bars.length) {
    canvas.setAttribute("aria-label", "No chart data loaded"); state.chartView.geometry = null;
    observeBrowser("browser.chart.render", performance.now() - startedAt); return;
  }
  const valid = bars.every((bar) => [bar.open, bar.high, bar.low, bar.close].every((value) => Number.isFinite(Number(value)) && Number(value) > 0));
  if (!valid) {
    $("chart-empty").hidden = false; $("chart-empty").textContent = "Invalid OHLC data refused; no chart drawn.";
    observeBrowser("browser.chart.render", performance.now() - startedAt); return;
  }
  const view = chartWindow(); const visible = view.bars;
  const left = 12, right = 78, top = 20, bottom = 31;
  const plotW = Math.max(1, width - left - right), plotH = Math.max(1, height - top - bottom);
  const high = Math.max(...visible.map((bar) => Number(bar.high))); const low = Math.min(...visible.map((bar) => Number(bar.low)));
  const maxVol = Math.max(1, ...visible.map((bar) => Number(bar.volume || 0)));
  const padding = Math.max((high - low) * .08, high * .0001); const max = high + padding, min = low - padding;
  const step = plotW / visible.length; const bodyW = Math.max(1, Math.min(13, step * .7));
  const y = (price) => top + (max - Number(price)) / (max - min || 1) * plotH;
  const xForIndex = (index) => left + (index - view.start + .5) * step;
  const geometry = { left, right, top, bottom, plotW, plotH, max, min, start: view.start, end: view.end, step, width, height };
  state.chartView.geometry = geometry;
  ctx.font = "10px -apple-system, BlinkMacSystemFont, monospace"; ctx.textBaseline = "middle";
  for (let index = 0; index <= 5; index += 1) {
    const gy = top + plotH * index / 5; const price = max - (max - min) * index / 5;
    ctx.strokeStyle = "#2a2e39"; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(left, gy); ctx.lineTo(left + plotW, gy); ctx.stroke();
    ctx.fillStyle = "#868993"; ctx.fillText(fmt(price, 6), left + plotW + 8, gy);
  }
  visible.forEach((bar, localIndex) => {
    const x = left + step * localIndex + step / 2; const up = Number(bar.close) >= Number(bar.open);
    const volH = Math.max(0, (Number(bar.volume || 0) / maxVol) * (plotH * 0.16));
    if (volH > 0.5) {
      ctx.fillStyle = up ? "rgba(8, 153, 129, 0.2)" : "rgba(242, 54, 69, 0.2)";
      ctx.fillRect(x - bodyW / 2, top + plotH - volH, bodyW, volH);
    }
    ctx.strokeStyle = up ? "#089981" : "#f23645"; ctx.fillStyle = up ? "#089981" : "#f23645";
    ctx.beginPath(); ctx.moveTo(x, y(bar.high)); ctx.lineTo(x, y(bar.low)); ctx.stroke();
    const bodyTop = y(Math.max(Number(bar.open), Number(bar.close)));
    const bodyBottom = y(Math.min(Number(bar.open), Number(bar.close)));
    ctx.fillRect(x - bodyW / 2, bodyTop, bodyW, Math.max(1, bodyBottom - bodyTop));
  });
  const indicator = $("chart-indicator")?.value || "NONE";
  if (["EMA20", "BOTH"].includes(indicator) && bars.length) drawIndicator(ctx, indicatorSeries("EMA", 20), view, geometry, "#2962ff");
  if (["SMA50", "BOTH"].includes(indicator) && bars.length) drawIndicator(ctx, indicatorSeries("SMA", 50), view, geometry, "#f59e0b");
  const drawings = currentDrawings();
  drawings.forEach((drawing) => {
    ctx.strokeStyle = "#c49aff"; ctx.fillStyle = "#c49aff"; ctx.lineWidth = 1.25; ctx.beginPath();
    if (drawing.type === "HORIZONTAL") {
      if (drawing.price < min || drawing.price > max) return;
      ctx.moveTo(left, y(drawing.price)); ctx.lineTo(left + plotW, y(drawing.price)); ctx.stroke();
      ctx.fillText(fmt(drawing.price, 6), left + 4, Math.max(top + 7, y(drawing.price) - 7));
    } else if (drawing.type === "TREND") {
      if (Math.max(drawing.a.index, drawing.b.index) < view.start || Math.min(drawing.a.index, drawing.b.index) >= view.end) return;
      ctx.moveTo(xForIndex(drawing.a.index), y(drawing.a.price)); ctx.lineTo(xForIndex(drawing.b.index), y(drawing.b.price)); ctx.stroke();
    }
  });
  const pending = state.chartView.pending;
  if (pending && pending.key === chartKey() && state.chartView.pointer) {
    const point = state.chartView.pointer;
    if (point.x >= left && point.x <= left + plotW && point.y >= top && point.y <= top + plotH) {
      const price = max - (point.y - top) / plotH * (max - min);
      ctx.strokeStyle = "#c49aff"; ctx.beginPath(); ctx.moveTo(xForIndex(pending.index), y(pending.price)); ctx.lineTo(point.x, y(price)); ctx.stroke();
    }
  }
  ctx.fillStyle = "#8298ac";
  for (let index = 0; index <= 4; index += 1) {
    const localIndex = Math.min(visible.length - 1, Math.floor((visible.length - 1) * index / 4));
    const date = new Date(visible[localIndex].timestamp); const label = Number.isNaN(date.getTime()) ? "" : date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
    const x = left + plotW * index / 4; ctx.textAlign = index === 0 ? "left" : index === 4 ? "right" : "center"; ctx.fillText(label, x, height - 12);
  }
  ctx.textAlign = "left";
  const pointer = state.chartView.pointer;
  if (state.chartView.crosshair && pointer && pointer.x >= left && pointer.x <= left + plotW && pointer.y >= top && pointer.y <= top + plotH) {
    const localIndex = Math.max(0, Math.min(visible.length - 1, Math.floor((pointer.x - left) / step)));
    const bar = visible[localIndex]; const globalIndex = view.start + localIndex; const x = xForIndex(globalIndex);
    const price = max - (pointer.y - top) / plotH * (max - min);
    if (ctx.setLineDash) ctx.setLineDash([3, 3]);
    ctx.strokeStyle = "#718ba3"; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, top + plotH); ctx.moveTo(left, pointer.y); ctx.lineTo(left + plotW, pointer.y); ctx.stroke();
    if (ctx.setLineDash) ctx.setLineDash([]);
    ctx.fillStyle = "#dbe8f2"; ctx.fillText(fmt(price, 6), left + plotW + 8, Math.max(top + 5, Math.min(top + plotH - 5, pointer.y)));
    $("chart-cursor").textContent = `${timeText(bar.timestamp)} · O ${fmt(bar.open, 6)} H ${fmt(bar.high, 6)} L ${fmt(bar.low, 6)} C ${fmt(bar.close, 6)}`;
  } else if (!pending) $("chart-cursor").textContent = "";
  canvas.setAttribute("aria-label", `${state.chart.symbol} ${state.chart.timeframe}: ${visible.length} of ${bars.length} candles, visible high ${high}, low ${low}, last visible close ${visible.at(-1).close}. ${indicator === "NONE" ? "No indicator" : indicator.replace("BOTH", "EMA 20 and SMA 50")}; ${drawings.length} drawings.`);
  observeBrowser("browser.chart.render", performance.now() - startedAt);
}
function chartPoint(event) {
  const canvas = $("terminal-chart"); const rect = canvas.getBoundingClientRect(); const geometry = state.chartView.geometry;
  const scaleX = geometry && rect.width ? geometry.width / rect.width : 1;
  const scaleY = geometry && rect.height ? geometry.height / rect.height : 1;
  return { x: (event.clientX - rect.left) * scaleX, y: (event.clientY - rect.top) * scaleY };
}
function addDrawingPoint(point) {
  const geometry = state.chartView.geometry; if (!geometry) return;
  const { left, top, plotW, plotH, max, min, start, end, step } = geometry;
  if (point.x < left || point.x > left + plotW || point.y < top || point.y > top + plotH) return;
  const index = Math.max(start, Math.min(end - 1, start + Math.floor((point.x - left) / step)));
  const price = max - (point.y - top) / plotH * (max - min);
  const tool = $("drawing-tool").value; const key = chartKey();
  const drawings = state.chartView.drawings[key] || [];
  if (tool === "HORIZONTAL") {
    drawings.push({ type: "HORIZONTAL", price });
    state.chartView.drawings[key] = drawings.slice(-50); drawChart();
  } else if (tool === "TREND") {
    if (!state.chartView.pending || state.chartView.pending.key !== key) {
      state.chartView.pending = { key, index, price };
      $("chart-cursor").textContent = "Trend first point set. Choose the second point; Escape cancels.";
    } else {
      const first = state.chartView.pending;
      if (first.index !== index || Math.abs(first.price - price) > 1e-12) drawings.push({ type: "TREND", a: { index: first.index, price: first.price }, b: { index, price } });
      state.chartView.drawings[key] = drawings.slice(-50); state.chartView.pending = null;
    }
    drawChart();
  }
}
function clearDrawings() {
  delete state.chartView.drawings[chartKey()]; state.chartView.pending = null;
  $("chart-cursor").textContent = ""; drawChart();
}
function bindChartCanvas() {
  const canvas = $("terminal-chart");
  canvas.addEventListener("pointerdown", (event) => {
    if (!(state.chart.bars || []).length) return;
    canvas.focus(); const point = chartPoint(event); state.chartView.pointer = point;
    if (($("drawing-tool").value || "NONE") === "NONE") {
      state.chartView.drag = { x: point.x, offset: state.chartView.endOffset };
      canvas.dataset.dragging = "true"; canvas.setPointerCapture?.(event.pointerId);
    } else addDrawingPoint(point);
    drawChart(); event.preventDefault();
  });
  canvas.addEventListener("pointermove", (event) => {
    const point = chartPoint(event); state.chartView.pointer = point;
    if (state.chartView.drag) {
      const step = state.chartView.geometry?.step || 10;
      state.chartView.endOffset = state.chartView.drag.offset + Math.round((point.x - state.chartView.drag.x) / step);
      chartWindow();
    }
    scheduleChartDraw();
  });
  const finishDrag = (event) => {
    if (state.chartView.drag) {
      state.chartView.drag = null; canvas.dataset.dragging = "false";
      if (event?.pointerId !== undefined && canvas.hasPointerCapture?.(event.pointerId)) canvas.releasePointerCapture(event.pointerId);
    }
    scheduleChartDraw();
  };
  canvas.addEventListener("pointerup", finishDrag); canvas.addEventListener("pointercancel", finishDrag);
  canvas.addEventListener("pointerleave", () => {
    if (!state.chartView.drag) { state.chartView.pointer = null; $("chart-cursor").textContent = ""; scheduleChartDraw(); }
  });
  canvas.addEventListener("wheel", (event) => {
    if (!(state.chart.bars || []).length) return;
    const point = chartPoint(event); const geometry = state.chartView.geometry;
    const anchor = geometry ? (point.x - geometry.left) / geometry.plotW : .5;
    zoomChart(event.deltaY > 0 ? 1.25 : .8, anchor); event.preventDefault();
  }, { passive: false });
  canvas.addEventListener("keydown", (event) => {
    const view = chartWindow(); const amount = Math.max(1, Math.round(view.visible * .1));
    if (event.key === "ArrowLeft") { panChart(amount); event.preventDefault(); }
    else if (event.key === "ArrowRight") { panChart(-amount); event.preventDefault(); }
    else if (["+", "="].includes(event.key)) { zoomChart(.8); event.preventDefault(); }
    else if (event.key === "-") { zoomChart(1.25); event.preventDefault(); }
    else if (event.key === "Home") { resetChartView(); event.preventDefault(); }
    else if (event.key === "Escape" && state.chartView.pending) {
      state.chartView.pending = null; $("chart-cursor").textContent = ""; drawChart(); event.stopPropagation(); event.preventDefault();
    }
  });
}
function ticketSignature(values) {
  return JSON.stringify({
    symbol: values.symbol, side: values.side, order_type: values.order_type,
    volume: Number(values.volume), limit_price: values.limit_price ?? null,
    stop_loss: values.stop_loss ?? null, take_profit: values.take_profit ?? null,
  });
}
function renderTicketEstimate() {
  const panel = $("ticket-estimate"); if (!panel || !state.workspace) return;
  const symbol = $("order-symbol").value; const volume = Number($("order-volume").value);
  const contract = state.contracts[symbol] || state.chart.payload?.instrument || {};
  const quote = state.chart.symbol === symbol ? state.chart.quote : contract.quote;
  const side = state.side; const type = $("order-type").value;
  const limit = type === "LIMIT" ? Number($("order-limit").value) : null;
  const lastClose = Number(state.chart.bars?.at(-1)?.close);
  const quotePrice = side === "BUY" ? Number(quote?.ask) : Number(quote?.bid);
  const price = Number.isFinite(limit) && limit > 0 ? limit : Number.isFinite(quotePrice) && quotePrice > 0 ? quotePrice : lastClose;
  const notional = Number.isFinite(volume) && volume > 0 && Number.isFinite(price) && price > 0 ? volume * price : null;
  const spread = Number.isFinite(Number(quote?.ask)) && Number.isFinite(Number(quote?.bid)) ? Number(quote.ask) - Number(quote.bid) : null;
  const spreadValue = spread !== null && spread >= 0 && notional !== null ? spread * volume : null;
  const currency = contract.currency_profit || state.workspace.account?.currency || "";
  let reviewed = null;
  if (state.ticketReview && symbol && Number.isFinite(volume) && volume > 0) {
    try {
      const values = orderFormValues();
      const reviewAge = ageSeconds(state.ticketReview.review.expires_at);
      if (ticketSignature(values) === state.ticketReview.signature && Number.isFinite(reviewAge) && reviewAge < 0) reviewed = state.ticketReview.review;
    } catch (_) { reviewed = null; }
  }
  const risk = reviewed?.risk;
  const values = [
    ["Product", productLabel(contract.product_type || state.chart.payload?.product_type || "LOCAL PAPER")],
    ["Quantity", Number.isFinite(volume) && volume > 0 ? `${fmt(volume, 8)} units` : "Enter quantity"],
    ["Indicative value", notional === null ? "Unavailable until price is loaded" : `${money(notional, currency)} · units × price`],
    ["Bid / ask", quote ? `${fmt(quote.bid, 8)} / ${fmt(quote.ask, 8)}` : "No current bid/ask"],
    ["Spread", spread === null ? "Unavailable" : `${fmt(spread, 8)} price${spreadValue === null ? "" : ` · ${money(spreadValue, currency)} indicative`}`],
    ["Costs", "Local review shows simulation context; configured costs apply"],
    ["Cash / bounds", "Local simulated cash and position bounds"],
    ["Risk", reviewed ? (risk?.allowed ? "Server review passed; final submit rechecks" : `Blocked: ${(risk?.issues || []).join("; ") || "see review"}`) : "Not reviewed · no submission"],
  ];
  panel.innerHTML = values.map(([key, value], index) => `<div class="ticket-value ${index >= 5 ? "wide" : ""}"><span>${escapeHtml(key)}</span><strong>${escapeHtml(value)}</strong></div>`).join("");
}

function updateOrderContract() {
  const limit = $("order-type").querySelector('option[value="LIMIT"]');
  limit.disabled = false;
  $("order-volume").min = 0; $("order-volume").step = "any";
  renderTicketEstimate();
}

function orderFormValues() {
  const type = $("order-type").value; const volume = Number($("order-volume").value);
  if (!$("order-symbol").value || !Number.isFinite(volume) || volume <= 0) throw new ApiError("Select an instrument and enter a finite quantity above zero");
  const optional = (id) => $(id).value === "" ? null : Number($(id).value);
  const limit = optional("order-limit"); const sl = optional("order-sl"); const tp = optional("order-tp");
  if (type === "LIMIT" && (!Number.isFinite(limit) || limit <= 0)) throw new ApiError("A LIMIT order requires a positive limit price");
  for (const [label, value] of [["Stop-loss", sl], ["Take-profit", tp]]) if (value !== null && (!Number.isFinite(value) || value <= 0)) throw new ApiError(`${label} must be positive and finite`);
  return { symbol: $("order-symbol").value, side: state.side, order_type: type, volume, limit_price: type === "LIMIT" ? limit : null, stop_loss: sl, take_profit: tp };
}
async function reviewOrder(event) {
  event.preventDefault();
  let values;
  try { values = orderFormValues(); } catch (error) { toast("Order ticket incomplete", error.message, "err"); return; }
  setDisabled("review-order", true, "Preparing read-only review…");
  try {
    await reviewPaper(values);
  } catch (error) { toast("Review blocked", error.message, "err"); }
  finally { renderChartContext(); }
}
async function reviewPaper(values) {
  const ticket = await api(`/paper/ticket?symbol=${encodeURIComponent(values.symbol)}`);
  const status = currentStatus(); const blocked = status.emergency_stop && values.side === "BUY";
  const entryBlocked = values.side === "BUY" && Boolean(ticket.entry_blocked);
  const idempotencyKey = newKey("paper-order");
  state.ticketReview = {
    signature: ticketSignature(values),
    review: { mode: "AXIOM_PAPER", expires_at: new Date(Date.now() + 30000).toISOString(), risk: { allowed: !((values.side === "BUY" && ticket.blocked) || entryBlocked || blocked), issues: [ticket.blocked, ticket.entry_blocked, blocked ? "Durable emergency stop" : null].filter(Boolean) } },
  };
  renderTicketEstimate();
  const body = `<p><strong>LOCAL PAPER ONLY.</strong> This review and any result are simulated; no broker order is routed. One idempotency key is bound to this reviewed request.</p>${reviewGrid([
    ["Symbol", values.symbol], ["Side / type", `${values.side} / ${values.order_type}`],
    ["Quantity", values.volume], ["Limit price", values.limit_price ?? "Market"],
    ["Last closed-bar price", ticket.last_price ?? "Unavailable"], ["Bar timestamp", timeText(ticket.priced_at)],
    ["Quote provider", ticket.quote_source || ticket.price_source], ["Quote source timestamp", timeText(ticket.quote_timestamp || ticket.priced_at)],
    ["Quote received", timeText(ticket.quote_received_at)], ["Quote / bar ID", ticket.quote_id || "Not supplied by this feed"],
    ["Cash", money(ticket.cash, state.workspace.account?.currency)], ["Held quantity", ticket.position?.quantity ?? 0],
    ["Max buy / sell", `${fmt(ticket.max_buy_quantity, 8)} / ${fmt(ticket.max_sell_quantity, 8)}`], ["Execution", "LOCAL PAPER · local simulation"],
  ])}${values.side === "BUY" && ticket.blocked ? `<div class="review-issues">${escapeHtml(ticket.blocked)}</div>` : ""}${entryBlocked ? `<div class="review-issues">${escapeHtml(ticket.entry_blocked)}</div>` : ""}${blocked ? `<div class="review-issues">Durable emergency stop blocks a new paper entry.</div>` : ""}`;
  openDialog({
    title: "Review simulated paper order", body, confirmLabel: "Place simulated paper order", confirmClass: "primary",
    confirmDisabled: Boolean((values.side === "BUY" && ticket.blocked) || entryBlocked || blocked || ticket.last_price === null),
    onConfirm: async () => {
      const payload = { symbol: values.symbol, side: values.side, quantity: values.volume, order_type: values.order_type, limit_price: values.limit_price, idempotency_key: idempotencyKey };
      const started = performance.now(); let result;
      try { result = await api("/paper/order", { body: payload }); }
      finally { observeBrowser("browser.paper.submit_roundtrip", performance.now() - started); }
      closeDialog(); await loadWorkspace(); await loadActivity();
      const order = result.order || {};
      toast("Local simulated order result", `${order.symbol || values.symbol} ${order.status || result.status || "recorded"} · no broker order`, "ok");
      if (state.page === "charts") await loadChart().catch(() => {});
    },
  });
}
function reviewGrid(items) {
  return `<div class="review-grid">${items.map(([key, value]) => `<div class="review-item"><span>${escapeHtml(key)}</span><strong>${escapeHtml(value ?? "—")}</strong></div>`).join("")}</div>`;
}

function cancelOrder(intentId) {
  openDialog({
    title: "Cancel simulated working order",
    body: "<p>This cancels only the local simulated working order.</p>",
    confirmLabel: "Cancel simulated order",
    onConfirm: async () => {
      await api(`/paper/orders/${encodeURIComponent(intentId)}/cancel`, { body: {} });
      closeDialog(); await loadWorkspace(); await loadActivity();
      toast("Cancellation result", "Local simulated order CANCELLED", "ok");
    },
  });
}

async function pauseSession() {
  try {
    await api("/paper/pause", { body: {} });
    await loadWorkspace(); toast("Session paused", "Paper strategy paused; pending entries handled by the local simulator.", "warn");
  } catch (error) { toast("Pause failed", error.message, "err"); }
}
function stopSession() {
  openDialog({
    title: "Stop local paper session",
    body: "<p>Stops the local strategy and cancels its simulated working orders. Positions and fills remain local paper records.</p>",
    confirmLabel: "Stop paper session", confirmClass: "warning",
    onConfirm: async () => {
      const result = await api("/paper/stop", { body: {} });
      closeDialog(); await loadWorkspace();
      toast("Stop result recorded", `Local paper session stopped · ${result.stop_reason || "OPERATOR_STOP"}.`, "warn", 10000);
    },
  });
}
function emergencyStop() {
  openDialog({
    title: "Set durable emergency stop",
    body: `<p><strong>This halts LOCAL PAPER${state.mm.available ? " plus NSE & NASDAQ Live-Market Paper, Upstox Autonomy, and Alpaca Broker" : ""}, and persists across restart.</strong></p><p>The local paper strategy stops and working simulated orders are cancelled. Protective SL/TP on open positions are preserved. New entries stay blocked until explicit clear.</p>`,
    confirmLabel: "Set emergency stop", onConfirm: async () => {
      // Both targets already exist and are authenticated: the legacy local halt
      // (persisted flag + simulator pause) and the atomic multi-subsystem stop.
      const results = [];
      try { results.push(await api(ROUTES.engineStop, { body: {} })); }
      catch (error) { results.push({ ok: false, detail: `local halt: ${error.message}` }); }
      try { results.push(await api(ROUTES.emergencyStop, { body: { reason: "operator emergency stop" } })); }
      catch (error) { results.push({ ok: false, detail: `multi-market stop: ${error.message}` }); }
      closeDialog(); await loadWorkspace(); await loadActivity();
      await loadMultiMarketOverview().catch(() => {});
      const failed = results.filter((item) => item && item.ok === false);
      toast(
        failed.length ? "Emergency stop partially applied" : "Durable emergency stop SET",
        failed.length
          ? `Review each subsystem before continuing: ${failed.map((item) => item.detail).join("; ")}`
          : "New entries are blocked. Protective exits and live gates are untouched; LIVE remains OFF.",
        failed.length ? "warn" : "err", 12000,
      );
    },
  });
}
function clearEmergency() {
  openDialog({
    title: "Clear durable emergency stop",
    body: "<p>Clearing does not connect, Arm, Activate, resume a strategy, or submit an order. Live execution remains OFF and disarmed.</p>",
    confirmLabel: "Clear stop — remain disarmed", confirmClass: "warning", phrase: "CLEAR EMERGENCY STOP",
    onConfirm: async () => {
      // The typed phrase is the server-side precondition on the reset route.
      const failures = [];
      try { await api(ROUTES.emergencyReset, { body: { confirm_phrase: "CLEAR EMERGENCY STOP" } }); }
      catch (error) { failures.push(`global reset: ${error.message}`); }
      try { await api(ROUTES.engineResume, { body: {} }); }
      catch (error) { failures.push(`local halt: ${error.message}`); }
      closeDialog(); await loadWorkspace();
      await loadMultiMarketOverview().catch(() => {});
      toast(
        failures.length ? "Emergency stop not fully cleared" : "Emergency stop cleared",
        failures.length ? failures.join("; ") : "Still disarmed and LIVE OFF. No session or strategy was automatically resumed.",
        failures.length ? "warn" : "ok",
      );
    },
  });
}

function renderActivity() {
  if (!$("activity-rows")) return;
  const filter = $("activity-filter").value.trim().toLowerCase();
  const events = state.activity.filter((event) => !filter || `${eventName(event)} ${JSON.stringify(event.payload || {})}`.toLowerCase().includes(filter));
  if (!events.length) { emptyRow("activity-rows", 4, "No redacted audit events match."); return; }
  $("activity-rows").innerHTML = events.map((event) => `<tr><td>${escapeHtml(timeText(eventTime(event)))}</td><td>${escapeHtml(event.environment || "AXIOM_PAPER")}</td><td>${escapeHtml(eventName(event))}</td><td><small>${escapeHtml(JSON.stringify(event.payload || {}))}</small></td></tr>`).join("");
}

function applyPageChrome(page) {
  if (!PAGE_TITLES[page]) return;
  document.querySelectorAll(".page").forEach((element) => { element.hidden = element.id !== `page-${page}`; });
  document.querySelectorAll(".nav-item").forEach((element) => {
    const active = element.dataset.page === page;
    element.classList.toggle("active", active);
    if (active) element.setAttribute("aria-current", "page"); else element.removeAttribute("aria-current");
  });
  $("page-eyebrow").textContent = PAGE_TITLES[page][0]; $("page-title").textContent = PAGE_TITLES[page][1];
  document.title = `${PAGE_TITLES[page][1]} · AXIOM Operator`;
}
async function goPage(page) {
  if (!PAGE_TITLES[page]) return;
  state.page = page;
  applyPageChrome(page);
  $("sidebar").classList.remove("open"); $("mobile-menu").setAttribute("aria-expanded", "false");
  if (page === "start") { await loadStartMonitor(); await loadStartMonitorRows(); }
  if (page === "instruments" && state.mm.available) await searchMultiMarketCatalog();
  if (page === "charts") {
    if (state.mm.chartFeed !== "SESSION" && state.mm.available) await loadMultiMarketChart();
    else await loadChartSymbols();
  }
  if (page === "strategies" && state.mm.available) await loadPaperDecisions();
  if (page === "activity") await loadActivity();
  if (page === "modellab") await loadModelLabOverview();
  if (page === "monitor") await loadTradeMonitor();
  if (page === "ensemble") { await loadEnsembleOverview(); await loadScanCapacity(); }
  renderAll();
}

async function paperSessionControl() {
  const paperState = state.workspace.paper_session?.state;
  if (paperState === "RUNNING") { stopSession(); return; }
  try {
    let result;
    if (paperState === "PAUSED") result = await api("/paper/resume", { body: {} });
    else result = await api("/api/paper/start-default", { body: {} });
    await loadWorkspace(); await loadChartSymbols();
    toast("Paper session updated", `${result.state || "RUNNING"} · ${result.strategy || "strategy"} · ${result.automation_enabled ? "AUTOMATION ON · automatic simulated orders possible" : "manual-only · automation OFF"} · local simulated execution only`, "ok");
  } catch (error) { toast("Paper session failed", error.message, "err"); }
}

function bindEvents() {
  $("auth-form").addEventListener("submit", async (event) => {
    event.preventDefault(); const button = event.target.querySelector("button"); button.disabled = true;
    await unlock($("auth-token").value); button.disabled = false;
  });
  document.querySelectorAll("[data-page]").forEach((button) => button.addEventListener("click", () => goPage(button.dataset.page).catch((error) => toast("Page load failed", error.message, "err"))));
  document.querySelectorAll("[data-go]").forEach((button) => button.addEventListener("click", () => goPage(button.dataset.go)));
  $("mobile-menu").addEventListener("click", () => { const open = $("sidebar").classList.toggle("open"); $("mobile-menu").setAttribute("aria-expanded", String(open)); });
  $("refresh-all").addEventListener("click", async () => {
    setDisabled("refresh-all", true, "Refreshing workspace…");
    try { await refreshAll(); toast("Workspace refreshed", "Latest available state loaded. No order action was triggered.", "ok"); }
    catch (_) { /* individual loaders already show a safe error */ }
    finally { setDisabled("refresh-all", false); }
  });
  $("emergency-stop").addEventListener("click", emergencyStop);
  $("modal-close").addEventListener("click", closeDialog); $("modal-cancel").addEventListener("click", closeDialog); $("modal-confirm").addEventListener("click", confirmDialog);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("modal").hidden) { closeDialog(); return; }
    if (event.key === "Escape" && $("sidebar").classList.contains("open")) {
      $("sidebar").classList.remove("open"); $("mobile-menu").setAttribute("aria-expanded", "false"); $("mobile-menu").focus(); return;
    }
    if (event.key === "Tab" && !$("modal").hidden) {
      const focusable = [...$("modal").querySelectorAll("button:not([disabled]), input:not([disabled]), select:not([disabled]), a[href], [tabindex]:not([tabindex='-1'])")].filter((element) => !element.closest("[hidden]"));
      if (!focusable.length) return;
      const first = focusable[0], last = focusable.at(-1);
      if (event.shiftKey && document.activeElement === first) { last.focus(); event.preventDefault(); }
      else if (!event.shiftKey && document.activeElement === last) { first.focus(); event.preventDefault(); }
    }
  });
  const reloadActiveChart = () => {
    const mmFeed = $("mm-chart-source-mode")?.value || "SESSION";
    return mmFeed !== "SESSION" ? loadMultiMarketChart() : loadChart();
  };
  $("chart-symbol").addEventListener("change", () => { state.ticketReview = null; $("order-symbol").value = $("chart-symbol").value; loadChart(); });
  $("chart-timeframe").addEventListener("change", () => { state.ticketReview = null; reloadActiveChart(); });
  document.querySelectorAll("[data-chart-timeframe]").forEach((button) => button.addEventListener("click", () => {
    const timeframe = button.dataset.chartTimeframe;
    const mmFeed = $("mm-chart-source-mode")?.value || "SESSION";
    if (mmFeed === "SESSION" && ![...$("chart-timeframe").options].some((option) => option.value === timeframe)) {
      toast("Timeframe unavailable", "The active data provider does not expose this timeframe for the selected instrument.", "warn");
      return;
    }
    if (![...$("chart-timeframe").options].some((option) => option.value === timeframe)) {
      const opt = document.createElement("option");
      opt.value = timeframe;
      opt.textContent = timeframe;
      $("chart-timeframe").appendChild(opt);
    }
    $("chart-timeframe").value = timeframe;
    state.ticketReview = null;
    reloadActiveChart();
  }));
  $("zoom-in").addEventListener("click", () => zoomChart(.8)); $("zoom-out").addEventListener("click", () => zoomChart(1.25));
  $("reset-chart-view").addEventListener("click", resetChartView); $("clear-drawings").addEventListener("click", clearDrawings);
  $("chart-indicator").addEventListener("change", drawChart);
  $("drawing-tool").addEventListener("change", () => { state.chartView.pending = null; $("chart-cursor").textContent = ""; drawChart(); });
  $("crosshair-toggle").addEventListener("click", () => {
    state.chartView.crosshair = !state.chartView.crosshair;
    $("crosshair-toggle").textContent = state.chartView.crosshair ? "Crosshair on" : "Crosshair off";
    $("crosshair-toggle").setAttribute("aria-pressed", String(state.chartView.crosshair));
    if (!state.chartView.crosshair) $("chart-cursor").textContent = "";
    drawChart();
  });
  bindChartCanvas();
  $("reload-chart").addEventListener("click", reloadActiveChart); $("start-paper").addEventListener("click", paperSessionControl);
  $("order-buy").addEventListener("click", () => { state.side = "BUY"; state.ticketReview = null; $("order-buy").classList.add("active"); $("order-sell").classList.remove("active"); renderChartContext(); });
  $("order-sell").addEventListener("click", () => { state.side = "SELL"; state.ticketReview = null; $("order-sell").classList.add("active"); $("order-buy").classList.remove("active"); renderChartContext(); });
  $("order-type").addEventListener("change", () => { state.ticketReview = null; $("limit-field").hidden = $("order-type").value !== "LIMIT"; renderTicketEstimate(); });
  $("order-form").addEventListener("input", () => { state.ticketReview = null; renderTicketEstimate(); });
  $("order-form").addEventListener("submit", reviewOrder);
  $("position-rows").addEventListener("click", (event) => {
    const exit = event.target.dataset.paperExit;
    if (exit) {
      const position = state.workspace.positions.find((item) => item.symbol === exit);
      goPage("charts").then(() => { state.side = "SELL"; $("order-sell").classList.add("active"); $("order-buy").classList.remove("active"); $("order-symbol").value = exit; $("order-volume").value = position?.quantity || ""; });
    }
  });
  $("order-rows").addEventListener("click", (event) => { const id = event.target.dataset.cancel; if (id) cancelOrder(id); });
  $("pause-session").addEventListener("click", pauseSession); $("stop-session").addEventListener("click", stopSession); $("clear-emergency").addEventListener("click", clearEmergency);
  $("activity-filter").addEventListener("input", renderActivity); $("reload-activity").addEventListener("click", loadActivity);
  $("export-activity").addEventListener("click", () => downloadJson(`axiom-redacted-activity-${new Date().toISOString().slice(0, 10)}.json`, {
    generated_at: new Date().toISOString(),
    note: "Redacted local audit; no credentials or API token. Simulated PAPER events are explicitly labeled and separated from broker-confirmed events.",
    events: state.activity,
    structured_paper_logs: state.mm.structuredLogs?.events || [],
  }));
  $("token-form").addEventListener("submit", async (event) => {
    event.preventDefault(); const candidate = $("settings-token").value;
    try {
      await api(ROUTES.authProbe, { token: candidate, keepAuth: true });
      state.token = candidate; storageSet("axiom-api-token", candidate); $("settings-token").value = "";
      toast("Local token updated for this tab", "Held in this tab's session storage only. The server configuration and stored Upstox / Alpaca credentials were not changed.", "ok");
    } catch (error) {
      $("settings-token").value = "";
      const failure = authFailure(error);
      toast(failure.kind === "wrong_token" ? "Token rejected" : "Token could not be verified", failure.message, "err", 12000);
    }
  });
  $("lock-app").addEventListener("click", () => { clearInterval(state.refreshTimer); state.workspace = null; showAuth("Tab locked. Broker/session state was not activated or changed."); });
  $("refresh-interval").addEventListener("change", configureRefresh);
  $("reload-performance").addEventListener("click", async () => {
    setDisabled("reload-performance", true, "Loading measurements…");
    await loadPerformance(); setDisabled("reload-performance", false);
  });
  $("download-diagnostics").addEventListener("click", () => downloadJson(`axiom-redacted-diagnostics-${new Date().toISOString().slice(0, 10)}.json`, {
    generated_at: new Date().toISOString(), note: "No API token, password, ciphertext, or full account login included. Review before sharing.",
    workspace: state.workspace, performance: state.performance, listed_assets: state.listedAssets,
    multi_market: state.mm.overview, alpaca_broker: state.alpacaBroker,
    connection: state.connectionInfo ? { secret_store: state.connectionInfo.secret_store } : null,
  }));
  if (globalThis.ResizeObserver) new ResizeObserver(() => drawChart()).observe($("terminal-chart"));
  bindMultiMarketEvents();
  bindLiveExecutionEvents();
  bindPaperLiveSwitchEvents();
}

/* ============================================================================
 * Multi-Market NSE (INR) + NASDAQ (USD) Desktop Controller
 * ============================================================================ */

function connStatePillClass(st) {
  if (st === "CONNECTED_READ_ONLY") return "ok";
  if (st === "STALE" || st === "AUTHENTICATING") return "warn";
  if (st === "REJECTED" || st === "UNAVAILABLE") return "err";
  return "";
}

async function loadMultiMarketOverview() {
  try {
    const ov = await api(ROUTES.multiMarketOverview, { keepAuth: true });
    state.mm.available = true;
    state.mm.overview = ov;
    renderMultiMarketDesk();
  } catch (_) {
    // Graceful no-op when running against legacy/minimal test harnesses without /api/markets/*
    state.mm.available = false;
  }
}

function renderMultiMarketDesk() {
  const ov = state.mm.overview;
  if (!ov) return;

  const nse = ov.paper?.NSE || {};
  const nasdaq = ov.paper?.NASDAQ || {};
  const providers = ov.providers?.providers || [];
  const live = ov.live || {};
  const watchlists = ov.watchlists || { NSE: [], NASDAQ: [] };

  // Session clocks & header pills
  if ($("mm-clock-nse") && nse.exchange_session) {
    const s = nse.exchange_session;
    $("mm-clock-nse").textContent = `NSE IST · ${s.state || "CLOSED"}`;
    $("mm-clock-nse").className = `status-pill ${s.is_open ? "ok" : "warn"}`;
  }
  if ($("mm-clock-nasdaq") && nasdaq.exchange_session) {
    const s = nasdaq.exchange_session;
    $("mm-clock-nasdaq").textContent = `NASDAQ ET · ${s.state || "CLOSED"}`;
    $("mm-clock-nasdaq").className = `status-pill ${s.is_open ? "ok" : "warn"}`;
  }

  const upData = providers.find((p) => p.id === "upstox_market_data");
  const alData = providers.find((p) => p.id === "alpaca_market_data");
  if ($("mm-pill-upstox-data") && upData) {
    $("mm-pill-upstox-data").textContent = `UPSTOX NSE · ${upData.connection_state}`;
    $("mm-pill-upstox-data").className = `status-pill ${connStatePillClass(upData.connection_state)}`;
  }
  if ($("mm-pill-alpaca-data") && alData) {
    $("mm-pill-alpaca-data").textContent = `ALPACA US (${(alData.feed || "iex").toUpperCase()}) · ${alData.connection_state}`;
    $("mm-pill-alpaca-data").className = `status-pill ${connStatePillClass(alData.connection_state)}`;
  }
  if ($("mm-upstox-data-pill") && upData) {
    $("mm-upstox-data-pill").textContent = upData.connection_state;
    $("mm-upstox-data-pill").className = `status-pill ${connStatePillClass(upData.connection_state)}`;
  }
  if ($("mm-alpaca-data-pill") && alData) {
    $("mm-alpaca-data-pill").textContent = alData.connection_state;
    $("mm-alpaca-data-pill").className = `status-pill ${connStatePillClass(alData.connection_state)}`;
  }
  if (state.workspace) { renderMode(); renderOverview(); }

  if ($("mm-boundary-live")) {
    const nseGate = live.nse?.availability === "AVAILABLE_BEHIND_GATE" ? "GATED READY" : "NOT AVAILABLE";
    const usGate = live.nasdaq?.broker_status?.gate?.state === "LIVE_ACTIVE" ? "LIVE ACTIVE" : (live.nasdaq?.availability === "AVAILABLE_BEHIND_GATE" ? "GATED READY" : "OFF / LOCKED");
    $("mm-boundary-live").textContent = `NSE ${nseGate} · NASDAQ ${usGate}`;
  }

  // Concurrent Multi-Market Selection & Runner Status
  const selectedMkts = Array.isArray(ov.selected_markets) ? ov.selected_markets : ["NSE", "NASDAQ"];
  if ($("mm-select-nse")) $("mm-select-nse").checked = selectedMkts.includes("NSE");
  if ($("mm-select-nasdaq")) $("mm-select-nasdaq").checked = selectedMkts.includes("NASDAQ");
  if ($("mm-runner-status-pill")) {
    const r = ov.runner || {};
    const bgOn = Boolean(r.background_loop_running || r.enabled);
    $("mm-runner-status-pill").textContent = bgOn
      ? `RUNNER ACTIVE · ${selectedMkts.join("+")} · PAPER ONLY (0 BROKER CALLS)`
      : `RUNNER STANDBY · ${selectedMkts.join("+")} · PAPER ONLY`;
    $("mm-runner-status-pill").className = `status-pill ${bgOn ? "ok" : "info"}`;
  }
  if ($("mm-concurrent-status-line")) {
    const nseSessLbl = `${nse.exchange_session?.state || "CLOSED"} (${nse.session_state || "STOPPED"})`;
    const usSessLbl = `${nasdaq.exchange_session?.state || "CLOSED"} (${nasdaq.session_state || "STOPPED"})`;
    $("mm-concurrent-status-line").textContent =
      `Selected markets: ${selectedMkts.join(" + ") || "NONE"} · NSE (INR): ${nseSessLbl} · NASDAQ (USD): ${usSessLbl} · ` +
      `Live execution: OFF (0 broker order calls). Closed markets analyze historical bars without placing simulated trades.`;
  }

  const renderCoverageBox = (boxId, snap, mkt) => {
    const box = $(boxId);
    if (!box) return;
    const la = snap.last_analysis;
    const effSyms = snap.effective_symbols || snap.watched_symbols || [];
    if (!la) {
      box.innerHTML = `<span class="muted small">Selected ${escapeHtml(mkt)} universe (${effSyms.length} symbol${effSyms.length === 1 ? "" : "s"}: ${escapeHtml(effSyms.map((s) => String(s).split("|").pop()).join(", ") || "none")}): click Start, Resume, or Analyze to evaluate all enabled strategies.</span>`;
      return;
    }
    const c = la.counts || {};
    const exOpen = Boolean(la.exchange_session?.is_open);
    const gateNote = la.trade_execution_permitted_now
      ? "Open session: eligible completed-bar paper trades active"
      : (exOpen ? `Session ${snap.session_state} (${snap.strategy}): no auto trades` : `Exchange ${la.exchange_session?.state || "CLOSED"}: analyzed read-only · 0 simulated trades`);
    const chips = (la.symbols || []).map((sr) => {
      const cls = sr.status === "SUCCEEDED" ? "ok" : (sr.status === "FAILED" ? "err" : "warn");
      const act = sr.ensemble_final_action || sr.rule_signal || "";
      const detail = sr.status === "SUCCEEDED"
        ? `${act ? `→ ${act}` : "OK"}${sr.active_weighted_variants != null ? ` (${sr.active_weighted_variants}v)` : ""}`
        : `${sr.status}: ${sr.code || sr.reason || "skipped"}`;
      return `<span class="mm-cov-chip ${cls}" title="${escapeHtml(sr.reason || "")}"><strong>${escapeHtml(sr.symbol)}</strong> ${escapeHtml(detail)}</span>`;
    }).join("");
    box.innerHTML = `
      <div><strong>${escapeHtml(mkt)} Analysis (${escapeHtml(la.trigger || "cycle")})</strong> · Selected: <strong>${c.selected_symbols ?? 0}</strong> · Succeeded: <strong class="pnl-pos">${c.succeeded ?? 0}</strong> · Missing: <strong>${c.missing_data ?? 0}</strong> · Stale: <strong>${c.stale_data ?? 0}</strong> · Failed: <strong class="${(c.failed || 0) > 0 ? "pnl-neg" : ""}">${c.failed ?? 0}</strong>${la.enabled_strategy_variants ? ` · Variants: <strong>${la.enabled_strategy_variants}</strong>` : ""}</div>
      <div class="muted small">${escapeHtml(gateNote)} · Analyzed ${escapeHtml(timeText(la.analyzed_at))}</div>
      ${chips ? `<div class="mm-cov-chips">${chips}</div>` : ""}
    `;
  };

  // Overview NSE & NASDAQ Paper Ledger cards
  const renderLedgerList = (elId, badgeId, snap, symPrefix) => {
    const acct = snap.account || {};
    if ($(badgeId)) {
      $(badgeId).textContent = snap.session_state || "STOPPED";
      $(badgeId).className = `status-pill ${snap.session_state === "RUNNING" ? "ok" : snap.session_state === "EMERGENCY_STOPPED" ? "err" : "info"}`;
    }
    if ($(elId)) {
      const curSessPnl = acct.current_session_total_pnl ?? 0;
      const histRealPnl = acct.persisted_historical_realized_pnl ?? acct.realized_pnl ?? 0;
      const effSyms = (snap.effective_symbols || snap.watched_symbols || []).map((s) => String(s).split("|").pop());
      const rows = [
        ["Execution destination", `${snap.execution_destination || "LIVE-MARKET PAPER · Simulated · 0 Broker Orders"}`],
        ["Market-data source", `${snap.market_data_source || "Read-only live provider (0 sample fallback)"}`],
        ["Fill type & automation", `${snap.fill_type || "SIMULATED"} · ${snap.automation_state || "MANUAL_ONLY"}`],
        ["Exchange session", `${snap.exchange_session?.state || "—"} (${snap.exchange_session?.timezone || ""})`],
        ["Selected symbols", `${effSyms.length} (${effSyms.join(", ") || "none"})`],
        ["Equity", `${symPrefix}${fmt(acct.equity, 2)}`],
        ["Available cash", `${symPrefix}${fmt(acct.cash, 2)}`],
        ["Open market value", `${symPrefix}${fmt(acct.market_value, 2)} (${(snap.positions || []).length} pos)`],
        ["Current-session run P&L", `${symPrefix}${fmt(curSessPnl, 2)} (${acct.current_session_run_id ? "active run" : "not running"})`],
        ["Today / Persisted hist. P&L", `${symPrefix}${fmt(acct.day_pnl, 2)} / ${symPrefix}${fmt(histRealPnl, 2)} (history)`],
        ["Regulatory fees simulated", `${symPrefix}${fmt(acct.total_fees_paid, 2)}`],
        ["Strategy", `${snap.strategy || "manual_only"} · ${snap.timeframe || "5m"}`],
      ];
      $(elId).innerHTML = rows.map(([k, v]) => `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(v)}</dd>`).join("");
    }
  };
  renderLedgerList("mm-nse-overview-list", "mm-nse-paper-state", nse, "₹");
  renderLedgerList("mm-nasdaq-overview-list", "mm-nasdaq-paper-state", nasdaq, "$");
  renderCoverageBox("mm-ov-nse-coverage", nse, "NSE");
  renderCoverageBox("mm-ov-nasdaq-coverage", nasdaq, "NASDAQ");
  renderCoverageBox("mm-strat-nse-coverage", nse, "NSE");
  renderCoverageBox("mm-strat-nasdaq-coverage", nasdaq, "NASDAQ");

  // 4-Mode Readiness Checklists (Priority 1)
  if ($("mm-readiness-grid") && ov.readiness?.modes) {
    if ($("mm-readiness-note") && ov.readiness.verification_note) {
      $("mm-readiness-note").textContent = ov.readiness.verification_note;
    }
    const modeOrder = ["NSE_PAPER", "NASDAQ_PAPER", "NSE_LIVE", "NASDAQ_LIVE"];
    $("mm-readiness-grid").innerHTML = modeOrder.map((key) => {
      const m = ov.readiness.modes[key];
      if (!m) return "";
      const stCls = m.overall_status === "READY" || m.overall_status === "MARKET_CLOSED_READY" || m.overall_status === "READY_TO_ARM"
        ? "ok"
        : m.overall_status === "NOT_VERIFIED" || m.overall_status === "AVAILABLE_BEHIND_GATE"
          ? "warn"
          : "err";
      const itemsHtml = (m.items || []).map((it) => {
        const itCls = it.state === "PASS" ? "ok" : it.state === "NOT_VERIFIED" ? "warn" : it.state === "BLOCKED" ? "err" : "info";
        return `<li><span>${escapeHtml(it.label)}<br><small class="muted">${escapeHtml(it.detail)}</small></span>${statusPill(it.state, itCls)}</li>`;
      }).join("");
      return `<div class="readiness-card">
        <div class="panel-head" style="margin-bottom:4px">
          <div><p class="eyebrow">${escapeHtml(m.market)} · ${escapeHtml(m.currency)}</p><h4>${escapeHtml(m.mode)}</h4></div>
          ${statusPill(m.overall_status, stCls)}
        </div>
        <ul class="readiness-list">${itemsHtml}</ul>
        <div class="inline-status ${stCls}"><strong>Next step:</strong> ${escapeHtml(m.next_action || "")}</div>
      </div>`;
    }).join("");
  }

  // Overview 5-Role Provider Matrix
  if ($("mm-overview-providers")) {
    $("mm-overview-providers").innerHTML = providers.map((p) => `
      <div class="provider-card">
        <div class="panel-head" style="margin-bottom:4px">
          <strong>${escapeHtml(p.name)}</strong>
          ${statusPill(p.connection_state, connStatePillClass(p.connection_state))}
        </div>
        <div class="muted small">Role: <code>${escapeHtml(p.role)}</code> · Creds: <strong>${p.credential_present ? escapeHtml(p.credential_source) : "missing"}</strong></div>
        <div class="muted small">${escapeHtml((p.used_for || []).join(" · "))}</div>
        ${p.token_expiry?.jwt_exp_present ? `<div class="small ${p.token_expiry.expired_by_claim ? "pnl-neg" : "pnl-pos"}">JWT expires: ${escapeHtml(timeText(p.token_expiry.expires_at))}</div>` : ""}
      </div>
    `).join("");
  }

  // Reconciliation Note & Watchlists
  const nseRec = ov.reconciliation?.NSE || {};
  const usRec = ov.reconciliation?.NASDAQ || {};
  if ($("mm-reconcile-badge")) {
    $("mm-reconcile-badge").textContent = `NSE: ${nseRec.tradable_count ?? (nseRec.counts?.tradable ?? 0)} · NASDAQ: ${usRec.counts?.total ?? 0}`;
  }
  if ($("mm-catalog-reconciliation-note")) {
    $("mm-catalog-reconciliation-note").textContent =
      `NSE source: ${nseRec.source_kind || "seed"} (${nseRec.authoritative_complete ? "Authoritative BOD" : "Seed reference — click Refresh NSE BOD master for full exchange universe"}) · ` +
      `NASDAQ source: ${usRec.source_kind || "seed"} (${usRec.authoritative_reconciled ? "Synced from Alpaca /v2/assets" : "Seed reference — save Alpaca keys & click Sync NASDAQ universe"}).`;
  }

  const allWatch = [
    ...(watchlists.NSE || []).map((w) => ({ ...w, market: "NSE" })),
    ...(watchlists.NASDAQ || []).map((w) => ({ ...w, market: "NASDAQ" })),
  ];
  if ($("mm-watchlist-grid")) {
    $("mm-watchlist-grid").innerHTML = allWatch.length ? allWatch.map((w) => `
      <div class="provider-card">
        <div class="panel-head" style="margin-bottom:2px">
          <strong>${escapeHtml(w.symbol)}</strong>
          ${statusPill(w.market, w.market === "NSE" ? "info" : "ok")}
        </div>
        <div class="muted small">${escapeHtml(w.name || w.instrument_key)}<br><code>${escapeHtml(w.instrument_key || w.symbol)}</code></div>
        <div class="button-row">
          <button type="button" class="button secondary xs" data-mm-jump-chart="${escapeHtml(`${w.market}:${w.symbol}`)}" data-mm-ikey="${escapeHtml(w.instrument_key || "")}">Chart</button>
          <button type="button" class="button primary xs" data-mm-jump-ticket="${escapeHtml(`${w.market}:${w.symbol}`)}" data-mm-ikey="${escapeHtml(w.instrument_key || "")}">Paper Ticket</button>
          <button type="button" class="button danger-outline xs" data-mm-unwatch="${escapeHtml(`${w.market}:${w.symbol}`)}" data-mm-ikey="${escapeHtml(w.instrument_key || "")}">Remove</button>
        </div>
      </div>
    `).join("") : `<p class="empty-state">No pinned watchlist symbols.</p>`;
  }

  if ($("mm-chart-watchlist-bar")) {
    $("mm-chart-watchlist-bar").innerHTML = allWatch.map((w) =>
      `<button type="button" class="wl-pill" data-mm-jump-chart="${escapeHtml(`${w.market}:${w.symbol}`)}" data-mm-ikey="${escapeHtml(w.instrument_key || "")}"><span>${escapeHtml(w.market)}</span><strong>${escapeHtml(w.symbol)}</strong></button>`
    ).join("");
  }

  // Orders, Fills & Positions Page
  const allPos = [
    ...(nse.positions || []).map((p) => ({ ...p, market: "NSE", curSym: "₹" })),
    ...(nasdaq.positions || []).map((p) => ({ ...p, market: "NASDAQ", curSym: "$" })),
  ];
  if ($("mm-paper-pos-count")) $("mm-paper-pos-count").textContent = `${allPos.length} OPEN`;
  if ($("mm-paper-position-rows")) {
    $("mm-paper-position-rows").innerHTML = allPos.length ? allPos.map((p) => `
      <tr>
        <td>${statusPill(`${p.market} · PAPER`, p.market === "NSE" ? "info" : "ok")}<br><small class="muted">${escapeHtml(mmExecutionModeLabel(p.market, ov))}</small></td>
        <td><strong>${escapeHtml(p.symbol)}</strong><br><small class="muted"><code>${escapeHtml(p.instrument_key || p.symbol)}</code></small></td>
        <td>${fmt(p.qty, 4)}</td>
        <td>${p.curSym}${fmt(p.avg_entry_price, 2)}</td>
        <td>${p.curSym}${fmt(p.last_price, 2)}</td>
        <td>${p.curSym}${fmt(p.market_value, 2)}</td>
        <td class="${Number(p.unrealized_pnl) >= 0 ? "pnl-pos" : "pnl-neg"}">${p.curSym}${fmt(p.unrealized_pnl, 2)} (${fmt(p.unrealized_pnl_pct, 2)}%)</td>
        <td>SL ${p.stop_loss ?? "—"} / TP ${p.take_profit ?? "—"}</td>
        <td>
          <div class="button-row">
            <button type="button" class="button secondary xs" data-mm-pos-prot="${escapeHtml(`${p.market}:${p.symbol}:${p.stop_loss ?? ""}:${p.take_profit ?? ""}`)}" data-mm-ikey="${escapeHtml(p.instrument_key || "")}">SL/TP</button>
            <button type="button" class="button danger xs" data-mm-pos-close="${escapeHtml(`${p.market}:${p.symbol}`)}" data-mm-ikey="${escapeHtml(p.instrument_key || "")}">Close</button>
          </div>
        </td>
      </tr>
    `).join("") : `<tr><td colspan="9" class="empty-state">No open NSE or NASDAQ Live-Market Paper positions.</td></tr>`;
  }

  const allOrders = [...(nse.orders || []), ...(nasdaq.orders || [])].slice(0, 60);
  if ($("mm-paper-order-rows")) {
    $("mm-paper-order-rows").innerHTML = allOrders.length ? allOrders.map((o) => {
      const open = ["WORKING", "PARTIALLY_FILLED"].includes(o.status);
      return `<tr>
        <td>${escapeHtml(o.market)}<br><small class="muted">${escapeHtml(mmExecutionModeLabel(o.market, ov))}</small></td>
        <td><strong>${escapeHtml(o.symbol)}</strong><br><small class="muted"><code>${escapeHtml(o.instrument_key || o.symbol)}</code></small></td>
        <td>${escapeHtml(o.side)} · ${escapeHtml(o.order_type)}</td>
        <td>${fmt(o.filled_qty, 4)} / ${fmt(o.qty, 4)}</td>
        <td>${fmt(o.avg_fill_price || o.limit_price || o.stop_price, 2)}</td>
        <td>${statusPill(o.status, o.status === "FILLED" ? "ok" : open ? "warn" : "")}</td>
        <td>${open ? `<button type="button" class="button danger-outline xs" data-mm-cancel-order="${escapeHtml(o.client_order_id)}">Cancel</button>` : "—"}</td>
      </tr>`;
    }).join("") : `<tr><td colspan="7" class="empty-state">No NSE/NASDAQ paper orders yet.</td></tr>`;
  }

  const allFills = [...(nse.fills || []), ...(nasdaq.fills || [])].slice(0, 60);
  if ($("mm-paper-fill-rows")) {
    $("mm-paper-fill-rows").innerHTML = allFills.length ? allFills.map((f) => {
      const c = f.currency === "INR" ? "₹" : "$";
      return `<tr>
        <td><small>${escapeHtml(timeText(f.created_at))}</small><br><span class="status-pill info">${escapeHtml(f.fill_type || "SIMULATED")}</span></td>
        <td><strong>${escapeHtml(f.market)}:${escapeHtml(f.symbol)}</strong><br><small class="muted">${escapeHtml(mmExecutionModeLabel(f.market, ov))} · <code>${escapeHtml(f.instrument_key || f.symbol)}</code></small></td>
        <td>${escapeHtml(f.side)}</td>
        <td>${fmt(f.qty, 4)} @ ${c}${fmt(f.fill_price, 2)}</td>
        <td>${c}${fmt(f.fees_total, 2)}</td>
        <td class="${Number(f.realized_pnl) >= 0 ? "pnl-pos" : "pnl-neg"}">${c}${fmt(f.realized_pnl, 2)}</td>
      </tr>`;
    }).join("") : `<tr><td colspan="6" class="empty-state">No NSE/NASDAQ paper fills yet.</td></tr>`;
  }

  // Strategies & Modes Page
  if ($("mm-strat-nse-state")) $("mm-strat-nse-state").textContent = nse.session_state || "STOPPED";
  if ($("mm-strat-nasdaq-state")) $("mm-strat-nasdaq-state").textContent = nasdaq.session_state || "STOPPED";
  if ($("mm-strat-nse-select") && nse.strategy && (nse.session_state !== "STOPPED" || nse.strategy !== "manual_only") && document.activeElement !== $("mm-strat-nse-select")) {
    $("mm-strat-nse-select").value = nse.strategy;
  }
  if ($("mm-strat-nasdaq-select") && nasdaq.strategy && (nasdaq.session_state !== "STOPPED" || nasdaq.strategy !== "manual_only") && document.activeElement !== $("mm-strat-nasdaq-select")) {
    $("mm-strat-nasdaq-select").value = nasdaq.strategy;
  }
  if ($("mm-strat-nse-tf") && nse.timeframe && nse.session_state !== "STOPPED" && document.activeElement !== $("mm-strat-nse-tf")) {
    $("mm-strat-nse-tf").value = nse.timeframe;
  }
  if ($("mm-strat-nasdaq-tf") && nasdaq.timeframe && nasdaq.session_state !== "STOPPED" && document.activeElement !== $("mm-strat-nasdaq-tf")) {
    $("mm-strat-nasdaq-tf").value = nasdaq.timeframe;
  }
  if ($("mm-live-mode-cards")) {
    const nseL = live.nse || {};
    const usL = live.nasdaq || {};
    $("mm-live-mode-cards").innerHTML = `
      <div class="provider-card">
        <div class="panel-head" style="margin-bottom:4px">
          <strong>NSE · UPSTOX_LIVE (INR)</strong>
          ${statusPill(nseL.availability || "NOT_AVAILABLE", nseL.availability === "AVAILABLE_BEHIND_GATE" ? "warn" : "err")}
        </div>
        <div class="muted small">Broker: ${escapeHtml(nseL.broker || "Upstox GTT v3")}</div>
        <div class="muted small">${(nseL.missing_dependencies || []).length ? `Missing: ${escapeHtml(nseL.missing_dependencies.join(" · "))}` : "All static prerequisites present; Arm & Activate required."}</div>
      </div>
      <div class="provider-card">
        <div class="panel-head" style="margin-bottom:4px">
          <strong>NASDAQ · ALPACA_LIVE (USD)</strong>
          ${statusPill(usL.availability || "NOT_AVAILABLE", usL.availability === "AVAILABLE_BEHIND_GATE" ? "warn" : "err")}
        </div>
        <div class="muted small">Broker: ${escapeHtml(usL.broker || "Alpaca Trading API v2")} · Gate: <code>${escapeHtml(usL.broker_status?.gate?.state || "DISABLED")}</code></div>
        <div class="muted small">${(usL.missing_dependencies || []).length ? `Missing: ${escapeHtml(usL.missing_dependencies.join(" · "))}` : "Distinct LIVE keys present; run Readiness → Arm → Phrase Activate."}</div>
      </div>
    `;
  }

  renderLiveExecutionPanel(ov);
  renderPaperLiveSwitch(ov);

  const kStatus = ov.kronos || state.ml.overview?.kronos || {};
  renderKronosPaperSection(kStatus, nse, nasdaq);

  const allDecisions = (state.mm.decisionsPayload?.decisions?.length)
    ? state.mm.decisionsPayload.decisions
    : [...(nse.decisions || []), ...(nasdaq.decisions || [])].slice(0, 40);
  renderDecisionInspector(allDecisions);

  // SHA-256 Audit Table
  const audits = ov.recent_audit || [];
  if ($("mm-paper-audit-rows")) {
    $("mm-paper-audit-rows").innerHTML = audits.length ? audits.map((a) => `
      <tr>
        <td>${a.seq}</td>
        <td><small>${escapeHtml(timeText(a.created_at))}</small></td>
        <td>${escapeHtml(a.market)}</td>
        <td><strong>${escapeHtml(a.event_kind)}</strong></td>
        <td><small>${escapeHtml(JSON.stringify(a.payload || {}))}</small></td>
        <td><code>${escapeHtml(String(a.entry_hash || "").slice(0, 14))}…</code></td>
      </tr>
    `).join("") : `<tr><td colspan="6" class="empty-state">No multi-market paper audit rows yet.</td></tr>`;
  }
  if (!state.mm.structuredLogs && !state.mm.structuredLogsError) {
    renderStructuredPaperLogsFallback(audits);
  }
}

function kronosLifecyclePillClass(lc) {
  const s = String(lc || "NOT_CONFIGURED").toUpperCase();
  if (s === "RUNNING" || s === "READY") return "ok";
  if (s === "DEGRADED") return "warn";
  if (s === "FAILED") return "err";
  return "info";
}

function deriveKronosTruthMatrix(kStatus) {
  const tm = kStatus?.truth_matrix;
  const kEng = kStatus?.kronos_engine || {};
  const diag = kStatus?.diagnostics || {};
  const lc = String(kStatus?.lifecycle_state || tm?.lifecycle_state || "NOT_CONFIGURED").toUpperCase();
  const pState = String(kStatus?.provider_state || "NOT_INSTALLED").toUpperCase();
  const installed = tm ? Boolean(tm.installed) : (pState !== "NOT_INSTALLED" && Boolean(kEng.artifact_verified || kEng.installed));
  const validated = tm ? Boolean(tm.validated) : Boolean(kEng.artifact_verified && (kEng.self_test_verified || kEng.last_self_test?.status === "PASSED"));
  const loaded = tm ? Boolean(tm.loaded) : Boolean(kStatus?.model_loaded);
  const running = tm ? Boolean(tm.running) : (lc === "RUNNING");
  const degraded = tm ? Boolean(tm.degraded) : (lc === "DEGRADED" || pState === "STALE");
  const failed = tm ? Boolean(tm.failed) : (lc === "FAILED" || pState === "ERROR");
  const producedCount = Number(tm?.produced_count ?? diag.forecast_produced_count ?? diag.inference_count ?? 0);
  const consumedCount = Number(tm?.consumed_count ?? diag.forecast_consumed_count ?? 0);
  return {
    installed,
    validated,
    loaded,
    running,
    degraded,
    failed,
    forecast_produced: tm ? Boolean(tm.forecast_produced) : producedCount > 0,
    forecast_consumed: tm ? Boolean(tm.forecast_consumed) : consumedCount > 0,
    produced_count: producedCount,
    consumed_count: consumedCount,
    lifecycle_state: lc,
    provider_state: pState,
  };
}

function renderKronosTruthMatrix(elId, kStatus) {
  const el = $(elId);
  if (!el) return;
  const tm = deriveKronosTruthMatrix(kStatus || {});
  const cells = [
    {
      key: "installed",
      label: "1 · Installed",
      value: tm.installed ? "YES · PINNED FILES" : "NO · MISSING",
      sub: tm.installed ? "Local weights & tokenizer present" : "Not downloaded locally",
      cls: tm.installed ? "ok" : "",
    },
    {
      key: "validated",
      label: "2 · Validated",
      value: tm.validated ? "YES · SHA-256 + SELF-TEST" : "NO · UNVERIFIED",
      sub: tm.validated ? "Exact manifest + runtime verified" : "Requires SHA-256 & CPU self-test",
      cls: tm.validated ? "ok" : (tm.installed ? "warn" : ""),
    },
    {
      key: "loaded",
      label: "3 · Loaded",
      value: tm.loaded ? "YES · CPU WORKER WARM" : "NO · UNLOADED",
      sub: tm.loaded ? "Predictor resident in worker" : "Zero idle RAM held",
      cls: tm.loaded ? "ok" : "",
    },
    {
      key: "running",
      label: "4 · Running (Session)",
      value: tm.running ? "YES · PAPER ATTACHED" : `NO · ${tm.lifecycle_state}`,
      sub: tm.running ? "Active PAPER session attached" : "Not running in active PAPER session",
      cls: tm.running ? "ok" : "",
    },
    {
      key: "degraded",
      label: "5 · Degraded",
      value: tm.degraded ? "YES · DEGRADED" : "NO · CLEAR",
      sub: tm.degraded ? "Non-Kronos fallback or stale state" : "No degraded fallback active",
      cls: tm.degraded ? "warn" : "",
    },
    {
      key: "failed",
      label: "6 · Failed",
      value: tm.failed ? "YES · FAILED" : "NO · CLEAR",
      sub: tm.failed ? (kStatus?.last_error?.code || "Inference / startup failure") : "No active Kronos error",
      cls: tm.failed ? "err" : "",
    },
    {
      key: "forecast_produced",
      label: "7 · Forecast Produced",
      value: tm.forecast_produced ? `YES (${tm.produced_count})` : "NO (0)",
      sub: kStatus?.diagnostics?.last_produced_at ? `Last: ${timeText(kStatus.diagnostics.last_produced_at)}` : "Validated bars -> output check",
      cls: tm.forecast_produced ? "ok" : "",
    },
    {
      key: "forecast_consumed",
      label: "8 · Consumed by PAPER",
      value: tm.forecast_consumed ? `YES (${tm.consumed_count})` : "NO (0)",
      sub: kStatus?.diagnostics?.last_consumed_at ? `Last: ${timeText(kStatus.diagnostics.last_consumed_at)}` : "Used in active PAPER strategy decision",
      cls: tm.forecast_consumed ? "ok" : "",
    },
  ];
  el.innerHTML = cells.map((c) => `
    <div class="truth-cell ${c.cls}" data-kronos-truth="${escapeHtml(c.key)}" data-truth-active="${escapeHtml(String(Boolean(tm[c.key])))}">
      <span class="truth-cell-label">${escapeHtml(c.label)}</span>
      <span class="truth-cell-value">${escapeHtml(c.value)}</span>
      <span class="truth-cell-sub">${escapeHtml(c.sub)}</span>
    </div>
  `).join("");
}

function renderKronosPaperSection(kStatus, nseSnap, nasdaqSnap) {
  const tm = deriveKronosTruthMatrix(kStatus || {});
  const cfg = kStatus?.config || {};
  const diag = kStatus?.diagnostics || {};
  const kEng = kStatus?.kronos_engine || {};
  const lastErr = kStatus?.last_error || null;
  const latestProd = diag.last_produced_summary || null;

  if ($("mm-pill-kronos-state") && kStatus?.provider_state) {
    $("mm-pill-kronos-state").textContent = `KRONOS · ${kStatus.provider_state}`;
    $("mm-pill-kronos-state").className = `status-pill ${kronosPillClass(kStatus.provider_state)}`;
  }
  if ($("mm-pill-kronos-session")) {
    $("mm-pill-kronos-session").textContent = `KRONOS PAPER · ${tm.lifecycle_state} · ${tm.consumed_count} CONSUMED`;
    $("mm-pill-kronos-session").className = `status-pill ${kronosLifecyclePillClass(tm.lifecycle_state)}`;
  }
  if ($("mm-kronos-lifecycle-pill")) {
    $("mm-kronos-lifecycle-pill").textContent = `${tm.lifecycle_state} · ${cfg.failure_policy || "FAIL_CLOSED_NO_ENTRY"}`;
    $("mm-kronos-lifecycle-pill").className = `status-pill ${kronosLifecyclePillClass(tm.lifecycle_state)}`;
  }
  renderKronosTruthMatrix("mm-kronos-truth-grid", kStatus);

  const renderMktLine = (elId, mktName, snap) => {
    const el = $(elId);
    if (!el) return;
    const ks = snap?.kronos_session || {};
    const mLc = ks.lifecycle_state || (snap?.kronos_enabled ? tm.lifecycle_state : "NOT_CONFIGURED");
    const pol = snap?.kronos_failure_policy || ks.failure_policy || cfg.failure_policy || "FAIL_CLOSED_NO_ENTRY";
    el.className = `inline-status top-space ${kronosLifecyclePillClass(mLc)}`;
    el.textContent = `${mktName} Kronos Session: ${mLc} · Configured=${Boolean(snap?.kronos_enabled ?? ks.kronos_configured)} · Policy=${pol} · Produced=${ks.produced_count ?? 0} · Consumed=${ks.consumed_count ?? 0} · 0 broker orders.`;
  };
  renderMktLine("mm-nse-kronos-session-line", "NSE", nseSnap);
  renderMktLine("mm-nasdaq-kronos-session-line", "NASDAQ", nasdaqSnap);

  if ($("mm-kronos-paper-telemetry")) {
    const modelRev = String(kEng.model_revision || kStatus?.install_plan?.model?.revision || "901c26c13326").slice(0, 12);
    const manSha = String(kEng.manifest_sha256 || kStatus?.install_plan?.manifest_sha256 || "—").slice(0, 16);
    const stStatus = kEng.last_self_test?.status || (kEng.self_test_verified ? "PASSED" : "NOT_VERIFIED");
    const lastInfTime = diag.last_inference_at ? timeText(diag.last_inference_at) : "Never in this process";
    const lastLat = diag.last_latency_ms != null
      ? `${fmt(diag.last_latency_ms, 1)} ms (p50 ${diag.inference_ms_p50 ?? "—"} ms / p95 ${diag.inference_ms_p95 ?? "—"} ms)`
      : "Not measured";
    const latestFcText = latestProd
      ? `${latestProd.market}:${latestProd.symbol} (${latestProd.timeframe}) · dir=${latestProd.direction || "—"} · conf=${fmt((Number(latestProd.confidence || 0) * 100), 1)}% · band=${fmt(latestProd.uncertainty_pct, 2)}%`
      : "No forecast produced in current session";
    const errText = lastErr
      ? `${lastErr.code || "ERROR"}: ${lastErr.reason || lastErr.detail || "Kronos unavailable"} (${timeText(lastErr.at || lastErr.timestamp)})`
      : (kStatus?.lifecycle_reason || "None");
    const rows = [
      ["PAPER Lifecycle State", `${tm.lifecycle_state} (Provider: ${tm.provider_state})`],
      ["Model & Manifest Identity", `NeoQuasar/Kronos-small @ ${modelRev} · sha256:${manSha}…`],
      ["Artifact & Self-Test Identity", `artifact_verified=${Boolean(kEng.artifact_verified)} · self_test=${stStatus}`],
      ["Last Inference Time & Latency", `${lastInfTime} · ${lastLat}`],
      ["Produced vs Consumed Count", `${tm.produced_count} produced / ${tm.consumed_count} consumed by active PAPER strategy`],
      ["Latest Forecast Confidence / Band", latestFcText],
      ["Active Failure & Conflict Policy", `${cfg.failure_policy || "FAIL_CLOSED_NO_ENTRY"} · ${cfg.conflict_policy || "VETO_CONFLICTING_ENTRY"}`],
      ["Eligibility & Weight Bounds", `min_conf=${cfg.min_confidence ?? 0.55} · max_band=${cfg.max_uncertainty_pct ?? 8.0}% · weight_cap=${cfg.weight_cap ?? 0.20}`],
      ["Failure / Lifecycle Reason", errText],
    ];
    $("mm-kronos-paper-telemetry").innerHTML = rows.map(([dt, dd]) => `<dt>${escapeHtml(dt)}</dt><dd>${escapeHtml(dd)}</dd>`).join("");
  }

  const activeEl = document.activeElement;
  if ($("mm-kp-session-enabled") && activeEl !== $("mm-kp-session-enabled") && cfg.paper_session_enabled !== undefined) {
    $("mm-kp-session-enabled").value = String(Boolean(cfg.paper_session_enabled));
  }
  if ($("mm-kp-failure-policy") && activeEl !== $("mm-kp-failure-policy") && cfg.failure_policy) {
    $("mm-kp-failure-policy").value = String(cfg.failure_policy);
  }
  if ($("mm-kp-conflict-policy") && activeEl !== $("mm-kp-conflict-policy") && cfg.conflict_policy) {
    $("mm-kp-conflict-policy").value = String(cfg.conflict_policy);
  }
  if ($("mm-kp-min-confidence") && activeEl !== $("mm-kp-min-confidence") && cfg.min_confidence != null) {
    $("mm-kp-min-confidence").value = String(cfg.min_confidence);
  }
  if ($("mm-kp-max-uncertainty") && activeEl !== $("mm-kp-max-uncertainty") && cfg.max_uncertainty_pct != null) {
    $("mm-kp-max-uncertainty").value = String(cfg.max_uncertainty_pct);
  }
  if ($("mm-kp-weight-cap") && activeEl !== $("mm-kp-weight-cap") && cfg.weight_cap != null) {
    $("mm-kp-weight-cap").value = String(cfg.weight_cap);
  }
}

async function loadPaperDecisions() {
  if (!state.mm.available) return;
  try {
    const mkt = $("mm-decision-market-filter")?.value || "ALL";
    const sym = ($("mm-decision-symbol-filter")?.value || "").trim();
    const q = new URLSearchParams({ market: mkt, limit: "80" });
    if (sym) q.set("symbol", sym);
    const res = await api(`/api/markets/paper/decisions?${q}`, { keepAuth: true });
    state.mm.decisionsPayload = res;
    if (res.kronos && state.mm.overview) state.mm.overview.kronos = res.kronos;
    renderMultiMarketDesk();
  } catch (_) {
    // Fallback to decisions embedded in /api/markets/overview
  }
}

function renderSelectedDecisionExplanation(d) {
  const el = $("mm-decision-explanation-view");
  if (!el) return;
  if (!d) {
    el.className = "inline-status top-space";
    el.textContent = "Click any decision row or per-symbol card to inspect full input window, Kronos forecast metrics, rule signal, conflict/failure policy, risk checks, and final simulated order action.";
    return;
  }
  const exp = d.explanation || {};
  const fc = exp.forecast_summary || exp.kronos_contribution || {};
  const iw = exp.input_window || fc.input_window || {};
  const rc = exp.risk_checks || {};
  const prod = Boolean(d.forecast_produced ?? exp.forecast_produced);
  const cons = Boolean(d.forecast_consumed ?? exp.forecast_consumed);
  const kStat = d.kronos_status || exp.kronos_status || fc.status || "NOT_CONFIGURED";
  const pol = d.policy_applied || exp.policy_applied || fc.policy || "RULE_ONLY";
  const ruleSig = exp.rule_action || exp.rule_signal || "—";
  const sigArr = Array.isArray(exp.strategy_signals) ? exp.strategy_signals : [];
  const otherSignalsTxt = sigArr.length
    ? sigArr.slice(0, 4).map((s) => `${s.variant_id || s.family || "sig"}=${s.direction || s.action || "HOLD"}`).join(", ") + (sigArr.length > 4 ? ` (+${sigArr.length - 4} more)` : "")
    : ruleSig;
  const confVal = fc.confidence ?? fc.up_probability;
  const confTxt = confVal != null ? `${fmt(Number(confVal) * 100, 1)}%` : "—";
  const bandTxt = fc.uncertainty_pct != null ? `${fmt(fc.uncertainty_pct, 2)}%` : "—";
  const latTxt = fc.latency_ms != null ? `${fmt(fc.latency_ms, 1)} ms` : "—";
  const winTxt = iw.bar_count ? `${iw.bar_count} bars (${iw.input_start || "?"} → ${iw.input_end || "?"})` : (fc.input_end ? `through ${fc.input_end}` : "completed-bar window");
  let riskTxt = "Evaluated via authoritative paper broker";
  if (Array.isArray(rc)) {
    const failedChecks = rc.filter((c) => c && c.passed === false);
    riskTxt = failedChecks.length
      ? `REJECTED (${failedChecks.map((c) => `${c.check_id}: ${c.detail || "blocked"}`).join("; ")})`
      : (rc.length ? `PASSED all ${rc.length} authoritative paper risk checks` : riskTxt);
  } else if (rc && typeof rc === "object") {
    riskTxt = rc.passed === false
      ? `REJECTED (${(rc.rejection_reasons || []).join("; ") || "guardrail blocked"})`
      : (rc.passed === true ? "PASSED authoritative paper risk bounds" : riskTxt);
  }
  el.className = `inline-status top-space ${d.action === "BUY" || d.action === "SELL" ? "ok" : (kStat.includes("FAIL") ? "warn" : "info")}`;
  el.innerHTML = `
    <strong>${escapeHtml(d.market)}:${escapeHtml(d.symbol)}</strong> · <code>${escapeHtml(d.decision_id || "—")}</code> · ${statusPill("SIMULATED · 0 BROKER ORDERS", "info")}<br>
    <span><strong>Inputs:</strong> Strategy=<code>${escapeHtml(d.strategy)}</code> (${escapeHtml(d.timeframe || "—")}) · Window=${escapeHtml(winTxt)} · Strategy Signals=<code>${escapeHtml(otherSignalsTxt)}</code></span><br>
    <span><strong>Kronos:</strong> Status=<code>${escapeHtml(kStat)}</code> · Produced=<code>${escapeHtml(String(prod))}</code> · Consumed=<code>${escapeHtml(String(cons))}</code> · Dir=<code>${escapeHtml(fc.direction || "—")}</code> · Conf=${escapeHtml(confTxt)} · Band=${escapeHtml(bandTxt)} · Latency=${escapeHtml(latTxt)} · Model=<code>${escapeHtml(fc.model_id || "NeoQuasar/Kronos-small")}</code></span><br>
    <span><strong>Decision &amp; Risk:</strong> Policy=<code>${escapeHtml(pol)}</code> · Final Action=<strong>${escapeHtml(d.action)}</strong> · Risk=${escapeHtml(riskTxt)} · Order=<code>${escapeHtml(d.order_client_id || "NONE")}</code> · Reason=${escapeHtml(d.reason || exp.reason || "—")}</span>
  `;
}

function renderDecisionInspector(rawDecisions) {
  const mktFilter = ($("mm-decision-market-filter")?.value || "ALL").toUpperCase();
  const textFilter = ($("mm-decision-symbol-filter")?.value || "").trim().toLowerCase();
  const filtered = (rawDecisions || []).filter((d) => {
    if (mktFilter !== "ALL" && String(d.market || "").toUpperCase() !== mktFilter) return false;
    if (textFilter) {
      const hay = `${d.symbol || ""} ${d.strategy || ""} ${d.reason || ""} ${d.kronos_status || ""}`.toLowerCase();
      if (!hay.includes(textFilter)) return false;
    }
    return true;
  });

  if ($("mm-strategy-decision-rows")) {
    $("mm-strategy-decision-rows").innerHTML = filtered.length ? filtered.map((d, idx) => {
      const decKey = d.decision_id || `${d.market}:${d.symbol}:${idx}`;
      const kBadge = d.forecast_consumed
        ? `<br><span class="status-pill ok">KRONOS CONSUMED</span>`
        : (d.forecast_produced
          ? `<br><span class="status-pill info">PRODUCED · NOT CONSUMED</span>`
          : (d.kronos_status && d.kronos_status !== "NOT_CONFIGURED"
            ? `<br><span class="status-pill warn">${escapeHtml(d.kronos_status)}</span>`
            : ""));
      return `<tr data-inspect-decision="${escapeHtml(decKey)}" style="cursor:pointer">
        <td><small>${escapeHtml(timeText(d.created_at || d.ts))}</small></td>
        <td>${escapeHtml(d.market)}</td>
        <td><strong>${escapeHtml(d.symbol)}</strong></td>
        <td>${escapeHtml(d.strategy)} · ${escapeHtml(d.timeframe)}${kBadge}</td>
        <td>${statusPill(d.action, d.action === "BUY" ? "ok" : d.action === "SELL" ? "err" : "")}</td>
        <td>${escapeHtml(d.reason)}</td>
        <td><code>${escapeHtml(d.order_client_id || "—")}</code></td>
      </tr>`;
    }).join("") : `<tr><td colspan="7" class="empty-state">No automated paper strategy decisions recorded yet.</td></tr>`;
  }

  if ($("mm-decision-symbol-cards")) {
    const bySym = new Map();
    for (const d of filtered) {
      const k = `${d.market}:${d.symbol}`;
      if (!bySym.has(k)) bySym.set(k, d);
    }
    const latestCards = [...bySym.values()].slice(0, 8);
    if (!latestCards.length) {
      $("mm-decision-symbol-cards").innerHTML = `<div class="empty-state">No matching per-symbol PAPER decisions.</div>`;
    } else {
      $("mm-decision-symbol-cards").innerHTML = latestCards.map((d, idx) => {
        const decKey = d.decision_id || `${d.market}:${d.symbol}:${idx}`;
        const exp = d.explanation || {};
        const fc = exp.forecast_summary || exp.kronos_contribution || {};
        const selectedCls = state.mm.selectedDecisionId === decKey ? "selected" : "";
        return `<div class="decision-card ${selectedCls}" data-inspect-decision="${escapeHtml(decKey)}">
          <div class="panel-head" style="margin-bottom:4px">
            <strong>${escapeHtml(d.market)}:${escapeHtml(d.symbol)} · <small>${escapeHtml(d.strategy)}</small></strong>
            ${statusPill(d.action, d.action === "BUY" ? "ok" : d.action === "SELL" ? "err" : "info")}
          </div>
          <div class="muted small">Rule: <code>${escapeHtml(exp.rule_action || exp.rule_signal || "—")}</code> · Kronos: <code>${escapeHtml(d.kronos_status || "NOT_CONFIGURED")}</code> · Consumed: <code>${escapeHtml(String(Boolean(d.forecast_consumed)))}</code></div>
          <div class="muted small">Conf: ${fc.confidence != null ? `${fmt(Number(fc.confidence) * 100, 1)}%` : "—"} · Band: ${fc.uncertainty_pct != null ? `${fmt(fc.uncertainty_pct, 2)}%` : "—"} · Policy: <code>${escapeHtml(d.policy_applied || exp.policy_applied || "—")}</code></div>
          <div class="muted small">${escapeHtml(String(d.reason || "").slice(0, 120))}</div>
        </div>`;
      }).join("");
    }
  }

  const chosen = filtered.find((d, idx) => (d.decision_id || `${d.market}:${d.symbol}:${idx}`) === state.mm.selectedDecisionId) || filtered[0] || null;
  renderSelectedDecisionExplanation(chosen);
}

function getStructuredLogQueryParams() {
  const params = new URLSearchParams({ limit: "250" });
  const win = $("log-filter-window")?.value || "all";
  const sym = ($("log-filter-symbol")?.value || "").trim();
  const evRaw = $("log-filter-event-type")?.value || "";
  const sev = $("log-filter-severity")?.value || "";
  const strat = ($("log-filter-strategy")?.value || "").trim();
  const sess = ($("log-filter-session")?.value || "").trim();
  const q = ($("log-filter-query")?.value || "").trim();
  params.set("window", win);
  if (sym) params.set("symbol", sym);
  if (evRaw.startsWith("CAT:")) params.set("category", evRaw.slice(4));
  else if (evRaw) params.set("event_type", evRaw);
  if (sev) params.set("severity", sev);
  if (strat) params.set("strategy", strat);
  if (sess) params.set("session_id", sess);
  if (q) params.set("query", q);
  return params;
}

async function loadStructuredPaperLogs() {
  if (!$("mm-structured-log-rows")) return;
  const params = getStructuredLogQueryParams();
  try {
    const res = await api(`/api/markets/paper/logs?${params}`, { keepAuth: true });
    state.mm.structuredLogs = res;
    state.mm.structuredLogsError = "";
    renderStructuredPaperLogs();
  } catch (err) {
    if (err && err.status === 404) {
      state.mm.structuredLogsError = "";
      renderStructuredPaperLogsFallback(state.mm.overview?.recent_audit || []);
      return;
    }
    state.mm.structuredLogsError = err?.message || "Unable to load structured paper logs.";
    renderStructuredPaperLogs();
  }
}

function renderStructuredPaperLogsFallback(audits) {
  if (!$("mm-structured-log-rows")) return;
  const rows = (audits || []).map((a) => ({
    timestamp: a.created_at || a.ts || "",
    severity: String(a.event_kind || "").includes("REJECT") || String(a.event_kind || "").includes("FAIL") ? "WARN" : "INFO",
    simulated: true,
    execution_badge: "SIMULATED · LIVE_MARKET_PAPER (0 BROKER ORDERS)",
    pnl_scope_label: "SIMULATED_PAPER_PNL_ONLY",
    market: a.market || "ALL",
    symbol: a.payload?.symbol || "",
    event_type: a.event_kind || "AUDIT_EVENT",
    strategy: a.payload?.strategy || "",
    session_id: a.payload?.session_id || a.payload?.session_run_id || "",
    decision_id: a.payload?.decision_id || "",
    order_id: a.payload?.client_order_id || a.payload?.order_id || "",
    fill_id: a.payload?.fill_id || "",
    detail: a.payload || {},
  }));
  state.mm.structuredLogs = { total_matching: rows.length, returned_count: rows.length, events: rows };
  renderStructuredPaperLogs();
}

function renderStructuredPaperLogs() {
  const errEl = $("log-error-banner");
  const rowsEl = $("mm-structured-log-rows");
  if (!rowsEl) return;
  if (state.mm.structuredLogsError) {
    if (errEl) {
      errEl.hidden = false;
      errEl.textContent = `Structured log query failed: ${state.mm.structuredLogsError}. Verify the local AXIOM server is reachable and click Refresh.`;
    }
    if ($("log-count-pill")) {
      $("log-count-pill").textContent = "LOG ERROR";
      $("log-count-pill").className = "status-pill err";
    }
    rowsEl.innerHTML = `<tr><td colspan="7" class="empty-state">Structured event stream unavailable due to query error above.</td></tr>`;
    return;
  }
  if (errEl) {
    errEl.hidden = true;
    errEl.textContent = "";
  }
  const events = state.mm.structuredLogs?.events || [];
  const total = state.mm.structuredLogs?.total_matching ?? events.length;
  if ($("log-count-pill")) {
    $("log-count-pill").textContent = `${events.length} / ${total} EVENTS`;
    $("log-count-pill").className = "status-pill info";
  }
  if (!events.length) {
    rowsEl.innerHTML = `<tr><td colspan="7" class="empty-state">No structured paper or Kronos events match the active filters.</td></tr>`;
    return;
  }
  rowsEl.innerHTML = events.map((ev) => {
    const sev = String(ev.severity || "INFO").toUpperCase();
    const sevCls = sev === "ERROR" ? "err" : (sev === "WARN" ? "warn" : "info");
    const simBadge = ev.simulated === false
      ? statusPill(ev.execution_badge || "BROKER-CONFIRMED · REAL MONEY", "err")
      : statusPill(ev.execution_badge || "SIMULATED · 0 BROKER ORDERS", "ok");
    const corrParts = [];
    if (ev.session_id) corrParts.push(`<span class="corr-chip" title="Session ID">sess:${escapeHtml(String(ev.session_id).slice(0, 14))}</span>`);
    if (ev.decision_id) corrParts.push(`<span class="corr-chip" title="Decision ID">dec:${escapeHtml(String(ev.decision_id).slice(0, 16))}</span>`);
    if (ev.order_id) corrParts.push(`<span class="corr-chip" title="Order ID">ord:${escapeHtml(String(ev.order_id).slice(0, 16))}</span>`);
    if (ev.fill_id) corrParts.push(`<span class="corr-chip" title="Fill ID">fill:${escapeHtml(String(ev.fill_id).slice(0, 14))}</span>`);
    return `<tr>
      <td><small>${escapeHtml(timeText(ev.timestamp))}</small></td>
      <td>${statusPill(sev, sevCls)}</td>
      <td>${simBadge}<br><small class="muted"><code>${escapeHtml(ev.pnl_scope_label || "SIMULATED_PAPER_PNL_ONLY")}</code></small></td>
      <td><strong>${escapeHtml(ev.market || "—")}</strong>${ev.symbol ? `:${escapeHtml(ev.symbol)}` : ""}</td>
      <td><strong><code>${escapeHtml(ev.event_type || "EVENT")}</code></strong>${ev.strategy ? `<br><small class="muted">strategy=${escapeHtml(ev.strategy)}</small>` : ""}</td>
      <td>${corrParts.length ? corrParts.join(" ") : `<span class="muted">—</span>`}</td>
      <td><small>${escapeHtml(JSON.stringify(ev.detail || ev.payload || {}))}</small></td>
    </tr>`;
  }).join("");
}

async function searchMultiMarketCatalog() {
  if (!state.mm.available || !$("mm-catalog-rows")) return;
  const q = new URLSearchParams({
    query: $("mm-catalog-query")?.value || "",
    market: $("mm-catalog-market")?.value || "ALL",
    subclass: $("mm-catalog-subclass")?.value || "",
    limit: "80",
  });
  try {
    const res = await api(`/api/markets/search?${q}`);
    state.mm.catalogItems = res.items || [];
    if (!state.mm.catalogItems.length) {
      $("mm-catalog-rows").innerHTML = `<tr><td colspan="8" class="empty-state">No matching NSE or NASDAQ instruments found.</td></tr>`;
      return;
    }
    $("mm-catalog-rows").innerHTML = state.mm.catalogItems.map((it) => {
      const sym = it.trading_symbol || it.symbol;
      const mkt = it.market || "NSE";
      const exch = it.exchange || it.venue || mkt;
      const ikey = it.instrument_key || sym;
      const catBadge = it.asset_category || (it.tradable === false ? "INDEX" : "CASH_EQUITY_OR_ETF");
      return `<tr>
        <td><button type="button" class="button secondary xs" data-mm-watch="${escapeHtml(`${mkt}:${sym}`)}" data-mm-ikey="${escapeHtml(ikey)}" data-mm-exchange="${escapeHtml(exch)}">+ Watch</button></td>
        <td>${statusPill(exch, mkt === "NSE" ? "info" : "ok")}</td>
        <td><strong>${escapeHtml(sym)}</strong><br><small class="muted"><code>${escapeHtml(ikey)}</code></small></td>
        <td>${escapeHtml(it.name || sym)}</td>
        <td><code>${escapeHtml(it.asset_subclass || it.instrument_type || "EQ")}</code><br><small class="muted">${escapeHtml(catBadge)}</small></td>
        <td>Lot ${fmt(it.quantity_step || it.lot_size || 1, 0)} · Tick ${fmt(it.tick_size, 4)}</td>
        <td>${it.tradable === false ? statusPill("WATCH ONLY · INDEX", "warn") : escapeHtml(it.product_label || (it.fractionable ? "USD Cash · Fractionable" : "Cash / Delivery"))}</td>
        <td>
          <div class="button-row">
            <button type="button" class="button secondary xs" data-mm-jump-chart="${escapeHtml(`${mkt}:${sym}`)}" data-mm-ikey="${escapeHtml(ikey)}" data-mm-exchange="${escapeHtml(exch)}">Chart</button>
            ${it.tradable === false ? "" : `<button type="button" class="button primary xs" data-mm-jump-ticket="${escapeHtml(`${mkt}:${sym}`)}" data-mm-ikey="${escapeHtml(ikey)}" data-mm-exchange="${escapeHtml(exch)}">Trade Paper</button>`}
          </div>
        </td>
      </tr>`;
    }).join("");
  } catch (err) {
    toast("Catalog search failed", err.message, "err");
  }
}

async function loadMultiMarketChart() {
  const feedMode = $("mm-chart-source-mode")?.value || "SESSION";
  state.mm.chartFeed = feedMode;
  if (feedMode === "SESSION") {
    $("session-chart-symbol-wrap").hidden = false;
    $("mm-chart-symbol-wrap").hidden = true;
    $("mm-load-live-chart-btn").hidden = true;
    if ($("mm-chart-feed-badge")) $("mm-chart-feed-badge").textContent = "SESSION / DATASET";
    await loadChartSymbols();
    return;
  }
  $("session-chart-symbol-wrap").hidden = true;
  $("mm-chart-symbol-wrap").hidden = false;
  $("mm-load-live-chart-btn").hidden = false;
  setDisabled("reload-chart", false);
  const symbol = ($("mm-chart-symbol-input")?.value || (feedMode === "NSE" ? "RELIANCE" : "AAPL")).trim();
  const timeframe = $("chart-timeframe")?.value || "5m";
  const selectedIkey = feedMode === "NSE"
    ? (symbol.includes("|") ? symbol : (state.mm.selectedChartSym === symbol ? (state.mm.selectedInstrumentKey || "") : ""))
    : "";
  if ($("mm-chart-feed-badge")) $("mm-chart-feed-badge").textContent = `${feedMode} LIVE READ-ONLY`;
  try {
    const q = new URLSearchParams({ market: feedMode, symbol, timeframe });
    if (selectedIkey) q.set("instrument_key", selectedIkey);
    const payload = await api(`/api/markets/chart?${q}`);
    const bars = normalizeBars(payload.bars || []);
    const resolvedKey = payload.instrument_key || payload.instrument?.instrument_key || selectedIkey || "";
    if (feedMode === "NSE" && resolvedKey) {
      state.mm.selectedInstrumentKey = resolvedKey;
      state.mm.selectedChartSym = payload.symbol || symbol;
      state.mm.ticketInstrumentKey = resolvedKey;
      state.mm.ticketSymbol = payload.symbol || symbol;
    }
    state.chart = {
      mode: currentStatus().mode,
      feed: `mm_${feedMode.toLowerCase()}`,
      symbol: payload.symbol || symbol,
      instrument_key: resolvedKey,
      timeframe,
      bars,
      quote: payload.quote || null,
      payload,
    };
    state.chartView.endOffset = 0;
    state.chartView.visible = Math.min(160, Math.max(20, bars.length || 20));
    document.querySelectorAll("[data-chart-timeframe]").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.chartTimeframe === timeframe);
    });
    $("chart-source").textContent = payload.data_feed_label || `${feedMode} READ-ONLY`;
    $("chart-source").className = `status-pill ${connStatePillClass(payload.connection_state)}`;
    $("chart-feed-context").className = `inline-status ${bars.length ? "ok" : (payload.error ? "err" : "warn")}`;
    const entInfo = payload.feed_entitlement_label ? ` · Entitlement: ${payload.feed_entitlement_label}` : "";
    const keyInfo = resolvedKey ? ` · Key: ${resolvedKey} (${payload.exchange || feedMode})` : "";
    const ageInfo = payload.quote_age_seconds != null ? ` · Quote age: ${payload.quote_age_seconds}s` : (payload.bar_age_seconds != null ? ` · Bar age: ${payload.bar_age_seconds}s` : "");
    const sessInfo = payload.market_session_status ? ` · Session: ${payload.market_session_status}` : "";
    const errInfo = payload.error ? ` · Provider error: ${payload.error}` : "";
    $("chart-feed-context").textContent = bars.length
      ? `${payload.data_feed_label} · Provider: ${payload.provider || payload.data_source} · ${bars.length} real OHLCV bars loaded${keyInfo}${entInfo}${ageInfo}${sessInfo}.`
      : `${payload.data_feed_label} (${payload.connection_state})${keyInfo}${entInfo}${sessInfo}${errInfo}. AXIOM never substitutes sample or replay bars for live ${feedMode} market data.`;
    if (!bars.length && $("chart-empty")) {
      $("chart-empty").hidden = false;
      $("chart-empty").textContent = payload.error
        ? `${feedMode} live bars unavailable (${payload.connection_state}): ${payload.error}. Zero sample fallback.`
        : `${feedMode} live bars unavailable (${payload.connection_state}). Save ${feedMode === "NSE" ? "Upstox daily access_token" : "Alpaca Market Data API Key & Secret"} on Connections.`;
    }
    const srcTs = payload.source_timestamp ? `src ${timeText(payload.source_timestamp)} · ` : "";
    const recvTs = payload.local_received_at || payload.generated_at;
    $("chart-legend").textContent = `${bars.length} real bars · ${payload.data_source}${resolvedKey ? ` · ${resolvedKey}` : ""} · ${srcTs}recv ${timeText(recvTs)}`;
    if ($("mm-order-market")) $("mm-order-market").value = feedMode;
    if ($("mm-order-symbol")) $("mm-order-symbol").value = payload.symbol || symbol;
    if ($("mm-ticket-currency-badge")) $("mm-ticket-currency-badge").textContent = feedMode === "NSE" ? "INR ₹" : "USD $";
    renderChartQuote();
    drawChart();
  } catch (err) {
    toast(`${feedMode} chart request failed`, err.message, "err");
  }
}

function getMultiMarketTicketBody() {
  const numOrNull = (id) => ($(id)?.value ? Number($(id).value) : null);
  const mkt = $("mm-order-market").value;
  const sym = $("mm-order-symbol").value.trim();
  const ikey = mkt === "NSE"
    ? (sym.includes("|") ? sym : (state.mm.ticketSymbol === sym ? (state.mm.ticketInstrumentKey || null) : null))
    : null;
  const body = {
    market: mkt,
    symbol: sym,
    side: state.mm.side,
    qty: Number($("mm-order-qty").value),
    order_type: $("mm-order-type").value,
    product: $("mm-order-product").value,
    time_in_force: $("mm-order-tif").value,
    limit_price: numOrNull("mm-order-limit"),
    stop_price: numOrNull("mm-order-stop"),
    stop_loss: numOrNull("mm-order-sl"),
    take_profit: numOrNull("mm-order-tp"),
    allow_off_hours_working: Boolean($("mm-order-offhours")?.checked),
  };
  if (ikey) body.instrument_key = ikey;
  return body;
}

function renderMultiMarketPreview(prev) {
  const box = $("mm-ticket-preview-box");
  if (!box || !prev) return;
  const c = prev.currency === "INR" ? "₹" : "$";
  const fees = prev.estimated_fees || {};
  box.innerHTML = `
    <div class="inline-status ${prev.ok ? "ok" : "err"}" style="margin-bottom:6px">
      <strong>${prev.ok ? "SIMULATED PREVIEW PASSED" : "BLOCKED BY GUARDRAILS"}</strong> · 0 Broker Calls · Fill: ${escapeHtml(prev.fill_type || "SIMULATED")}
    </div>
    <div class="small muted">Instrument: <code>${escapeHtml(prev.instrument_key || prev.symbol || "")}</code> (${escapeHtml(prev.exchange || prev.market || "")}) · Session: <strong>${escapeHtml(prev.market_session_status || prev.session?.state || "—")}</strong></div>
    <div>Ref / Est Fill: <strong>${c}${fmt(prev.reference_price, 2)} → ${c}${fmt(prev.estimated_fill_price, 2)}</strong></div>
    <div>Est Notional: <strong>${c}${fmt(prev.estimated_notional, 2)}</strong> · Total Fees: <strong>${c}${fmt(fees.total, 2)}</strong></div>
    <div class="muted small">Quote: ${escapeHtml(prev.quote_source || "unavailable")} · ${escapeHtml(prev.feed_entitlement_label || "")} (${prev.quote_age_seconds !== null && prev.quote_age_seconds !== undefined ? `${prev.quote_age_seconds}s old` : "no fresh quote"})</div>
    ${prev.source_timestamp ? `<div class="muted small">Src TS: ${escapeHtml(prev.source_timestamp)} · Recv TS: ${escapeHtml(prev.local_received_at || prev.checked_at || "")}</div>` : ""}
    ${(prev.rejection_reasons || []).length ? `<div class="pnl-neg small" style="margin-top:5px">${escapeHtml(prev.rejection_reasons.join(" · "))}</div>` : ""}
  `;
}

function switchTicketTab(tab) {
  const isMM = tab === "MULTIMARKET";
  if ($("ticket-tab-session")) $("ticket-tab-session").classList.toggle("active", !isMM);
  if ($("ticket-tab-mm")) $("ticket-tab-mm").classList.toggle("active", isMM);
  if ($("session-ticket-pane")) $("session-ticket-pane").hidden = isMM;
  if ($("mm-ticket-pane")) $("mm-ticket-pane").hidden = !isMM;
}

function mmExecutionModeLabel(mkt, ov) {
  // Every order/position row must state the market's current execution path so a
  // simulated ledger can never be mistaken for live routing (and vice versa).
  const modes = (ov && ov.execution_modes) || {};
  const mode = String(modes[mkt] || "PAPER").toUpperCase();
  if (mode === "LIVE") return `${mkt} · ROUTED LIVE (real money)`;
  if (mode === "OFF") return `${mkt} · EXECUTION OFF`;
  return `${mkt} · PAPER (simulated)`;
}

function liveGatePillClass(state) {
  const s = String(state || "DISABLED").toUpperCase();
  if (s === "ACTIVE") return "err";
  if (s === "ARMED" || s === "READINESS_FAILED") return "warn";
  if (s === "KILL_SWITCH") return "err";
  return "";
}

function liveModePillClass(mode) {
  return String(mode || "PAPER").toUpperCase() === "LIVE" ? "err" : "info";
}

function setLiveExecOutput(message, tone) {
  const el = $("mm-live-exec-output");
  if (!el) return;
  el.className = `inline-status top-space ${tone || ""}`.trim();
  el.textContent = message;
}

function renderLiveExecutionPanel(ov) {
  if (!$("mm-live-execution-panel")) return;
  const exec = (ov && ov.live_execution && ov.live_execution.markets) || null;
  const modes = (ov && ov.execution_modes) || {};

  for (const mkt of ["NSE", "NASDAQ"]) {
    const key = mkt === "NSE" ? "nse" : "nasdaq";
    const row = (exec && exec[mkt]) || null;
    const mode = String((row && row.mode) || modes[mkt] || "PAPER").toUpperCase();
    const gateState = String((row && row.gate && row.gate.state) || "DISABLED").toUpperCase();
    const modePill = $(`mm-live-exec-${key}-pill`);
    if (modePill) {
      modePill.textContent = `${mkt} · ${mode}${mode === "LIVE" ? " · REAL MONEY" : mode === "PAPER" ? " · SIMULATED" : " · NO EXECUTION"}`;
      modePill.className = `status-pill ${liveModePillClass(mode)}`;
    }
    const gatePill = $(`mm-live-gate-${key}`);
    if (gatePill) {
      gatePill.textContent = gateState;
      gatePill.className = `status-pill ${liveGatePillClass(gateState)}`;
    }
    const select = $(`mm-live-mode-${key}`);
    if (select && document.activeElement !== select) select.value = mode;
    const limits = (row && row.risk_limits) || {};
    const pairs = [
      [`mm-live-limits-${key}-order`, limits.max_order_value],
      [`mm-live-limits-${key}-orders`, limits.max_daily_orders],
      [`mm-live-limits-${key}-loss`, limits.max_daily_loss],
      [`mm-live-limits-${key}-pos`, limits.max_open_positions],
      [`mm-live-limits-${key}-symexp`, limits.max_symbol_exposure],
      [`mm-live-limits-${key}-totexp`, limits.max_total_exposure],
      [`mm-live-limits-${key}-age`, limits.max_quote_age_seconds],
      [`mm-live-limits-${key}-dev`, limits.max_price_deviation_bps],
    ];
    for (const [id, value] of pairs) {
      const input = $(id);
      if (input && document.activeElement !== input && value !== undefined && Number(value) > 0 && !input.value) {
        input.value = String(value);
      }
    }
    const out = $(`mm-live-readiness-${key}-out`);
    if (out && row) {
      const missing = (row.missing_dependencies || []).length
        ? ` Missing dependencies: ${row.missing_dependencies.join(" · ")}.`
        : "";
      const blocked = row.blocked ? ` BLOCKED: ${row.blocked}${row.blocked_detail ? ` — ${row.blocked_detail}` : ""}.` : "";
      out.textContent = `Mode ${mode} · gate ${gateState} · broker ${row.broker || "—"} · limits ${row.risk_limits?.configured ? "configured" : "NOT configured (fail closed)"}.${missing}${blocked}`;
      out.className = `inline-status top-space ${row.configured && gateState === "ACTIVE" ? "err" : row.configured ? "warn" : "err"}`;
    }
  }

  const audits = (ov && ov.live_audit) || [];
  if ($("mm-live-audit-rows")) {
    $("mm-live-audit-rows").innerHTML = audits.length
      ? audits.map((a) => `
      <tr>
        <td>${a.seq}</td>
        <td><small>${escapeHtml(timeText(a.created_at))}</small></td>
        <td>${statusPill(a.market, a.market === "NSE" ? "info" : "ok")}</td>
        <td><strong>${escapeHtml(a.event_kind)}</strong></td>
        <td><small>${escapeHtml(JSON.stringify(a.payload || {}).slice(0, 260))}</small></td>
        <td><code>${escapeHtml(String(a.entry_hash || "").slice(0, 14))}…</code></td>
      </tr>`).join("")
      : `<tr><td colspan="6" class="empty-state">No live execution audit events recorded. LIVE has never been activated.</td></tr>`;
  }
}

function liveAction(market, action, body) {
  const mkt = String(market).toLowerCase();
  const method = action === "readiness" ? "GET" : "POST";
  const path = `/api/markets/live/${mkt}/${action}`;
  return api(path, { method, body: method === "POST" ? (body || {}) : undefined, keepAuth: method === "GET" });
}

function bindLiveExecutionEvents() {
  const panel = $("mm-live-execution-panel");
  if (!panel) return;
  // Idempotent: a second bind would double-submit real-money actions on one click.
  if (panel.dataset.liveBound === "1") return;
  panel.dataset.liveBound = "1";
  for (const [mkt, key] of [["NSE", "nse"], ["NASDAQ", "nasdaq"]]) {
    const guard = async (label, fn) => {
      try {
        await fn();
      } catch (err) {
        setLiveExecOutput(`${mkt} ${label} failed: ${err.message}`, "err");
      }
    };

    $(`mm-live-mode-save-${key}`)?.addEventListener("click", () => guard("mode save", async () => {
      const mode = ($(`mm-live-mode-${key}`)?.value || "PAPER").toUpperCase();
      const res = await liveAction(mkt, "mode", { mode });
      setLiveExecOutput(
        `${mkt} execution mode is now ${res.mode}. ${mode === "LIVE" ? "LIVE remains DISABLED until readiness, Arm, and the typed phrase." : "Local paper routing applies; live gate disarmed."}`,
        mode === "LIVE" ? "warn" : "ok",
      );
      await loadMultiMarketOverview();
    }));

    $(`mm-live-limits-save-${key}`)?.addEventListener("click", () => guard("limits save", async () => {
      // All eight limits are required: the server rejects a partial/zero set (fail closed),
      // so the form must never send a silently-incomplete limit set.
      const field = (id) => Number($(`mm-live-limits-${key}-${id}`)?.value || 0);
      const body = {
        max_order_value: field("order"),
        max_daily_orders: field("orders"),
        max_daily_loss: field("loss"),
        max_open_positions: field("pos"),
        max_symbol_exposure: field("symexp"),
        max_total_exposure: field("totexp"),
        max_quote_age_seconds: field("age"),
        max_price_deviation_bps: field("dev"),
      };
      const missing = Object.entries(body).filter(([, value]) => !(Number(value) > 0)).map(([name]) => name);
      if (missing.length) {
        throw new Error(`all eight limits are required (missing: ${missing.join(", ")}); partial limits fail closed`);
      }
      const res = await liveAction(mkt, "limits", body);
      setLiveExecOutput(`${mkt} live risk limits saved (${JSON.stringify(res.risk_limits)}). Gate disarmed until re-armed.`, "ok");
      await loadMultiMarketOverview();
    }));

    $(`mm-live-readiness-${key}`)?.addEventListener("click", () => guard("readiness", async () => {
      const res = await liveAction(mkt, "readiness");
      const failed = res.failed || [];
      const out = $(`mm-live-readiness-${key}-out`);
      if (out) {
        out.className = `inline-status top-space ${res.ok ? "warn" : "err"}`;
        out.textContent = res.ok
          ? `${mkt} readiness PASSED (${Object.keys(res.checks || {}).length} checks). Arm is still required before Activate.`
          : `${mkt} readiness FAILED: ${failed.join(", ")}.`;
      }
      setLiveExecOutput(
        res.ok ? `${mkt} readiness passed; Arm is required next.` : `${mkt} readiness blocked by ${failed.length} check(s).`,
        res.ok ? "warn" : "err",
      );
    }));

    $(`mm-live-arm-${key}`)?.addEventListener("click", () => guard("arm", async () => {
      const res = await liveAction(mkt, "arm");
      setLiveExecOutput(`${mkt} gate ARMED (${res.gate?.state}). No order can be sent until the phrase-confirmed Activate.`, "warn");
      await loadMultiMarketOverview();
    }));

    $(`mm-live-activate-${key}`)?.addEventListener("click", () => guard("activate", async () => {
      const phrase = ($(`mm-live-phrase-${key}`)?.value || "").trim();
      const ack = Boolean($(`mm-live-ack-${key}`)?.checked);
      if (!ack) throw new Error("acknowledgement checkbox is required");
      if (phrase !== "ACTIVATE LIVE REAL-MONEY TRADING") throw new Error("the exact confirmation phrase is required");
      const res = await liveAction(mkt, "activate", { confirm_live_trading: phrase, acknowledge_risk: ack });
      const phraseInput = $(`mm-live-phrase-${key}`);
      if (phraseInput) phraseInput.value = "";
      const ackInput = $(`mm-live-ack-${key}`);
      if (ackInput) ackInput.checked = false;
      setLiveExecOutput(`${mkt} LIVE is ${res.gate?.state || "ACTIVE"} — real-money orders may be routed while the gate stays ACTIVE. Emergency Stop remains available.`, "err");
      await loadMultiMarketOverview();
    }));

    $(`mm-live-disarm-${key}`)?.addEventListener("click", () => guard("disarm", async () => {
      await liveAction(mkt, "disarm", { reason: "operator disarm from terminal" });
      setLiveExecOutput(`${mkt} gate DISARMED. The live client is unbound.`, "ok");
      await loadMultiMarketOverview();
    }));

    $(`mm-live-estop-${key}`)?.addEventListener("click", () => guard("emergency stop", async () => {
      const res = await liveAction(mkt, "emergency-stop", { reason: "operator per-market emergency stop", cancel_working: false });
      setLiveExecOutput(`${mkt} EMERGENCY STOP engaged (${res.gate?.state}). The other market's safeguards are untouched.`, "err");
      await loadMultiMarketOverview();
    }));

    $(`mm-live-reset-${key}`)?.addEventListener("click", () => guard("kill-switch reset", async () => {
      await liveAction(mkt, "reset-kill-switch", { operator_ack: true });
      setLiveExecOutput(`${mkt} kill switch cleared; gate returns to DISABLED and must be re-armed.`, "warn");
      await loadMultiMarketOverview();
    }));

    $(`mm-live-reconcile-${key}`)?.addEventListener("click", () => guard("reconcile", async () => {
      const res = await liveAction(mkt, "reconcile");
      setLiveExecOutput(`${mkt} reconciled with the broker (${res.resolved_intents || 0} intent(s) resolved).`, "ok");
      await loadMultiMarketOverview();
    }));
  }
}

/* ============================================================================
 * PAPER / LIVE switch — one deliberate switch per market
 *
 * This is a *client* for the single existing execution boundary
 * (app.markets.live_execution). It adds no endpoint and no order route, and it
 * never widens a gate:
 *
 *   1. Read readiness (GET only) — every blocker and the documented exit route.
 *   2. Select LIVE mode  — POST .../mode {mode: LIVE}; the gate stays DISARMED.
 *   3. Arm               — POST .../arm; refused by the server while any check fails.
 *   4. Acknowledge risk + optional no-exit acknowledgement + typed phrase,
 *      then Activate     — POST .../activate; the server re-checks everything.
 *
 * Returning to PAPER is one POST .../mode {mode: PAPER}: the server disarms the
 * gate and revokes the permit, and nothing here cancels or changes a working
 * order or a protective exit. No strategy is started or resumed by any step.
 * ==========================================================================*/

const SWITCH_MARKETS = Object.freeze([
  { market: "NSE", key: "nse", currency: "INR", money: "INR ₹" },
  { market: "NASDAQ", key: "nasdaq", currency: "USD", money: "USD $" },
]);
const SWITCH_PHRASE = "ACTIVATE LIVE REAL-MONEY TRADING";
/* The one readiness check this switch performs itself: it is False until step 2
 * sets mode=LIVE, so it is shown as a step and never counted as a blocker. */
const SWITCH_MODE_STEP_CHECK = "mode_is_live";
/* Operator-facing meaning of every readiness check the server can report. */
const SWITCH_BLOCKER_MEANING = Object.freeze({
  configuration_complete: "Required broker/account configuration is missing for this market (see Missing dependencies).",
  risk_limits_configured: "All eight per-market risk limits must be positive; a partial or zero limit set fails closed.",
  no_unresolved_intents: "An earlier live order outcome is unresolved; reconcile before enabling LIVE.",
  not_blocked: "This market is latched after an unknown outcome; reconcile before enabling LIVE.",
  kill_switch_not_engaged: "The per-market kill switch is engaged; reset it with an explicit operator acknowledgement first.",
  readiness_probe: "The server's broker readiness probe raised an error; LIVE stays blocked.",
  readiness_probe_error: "The broker readiness probe returned an error; LIVE stays blocked.",
  live_master_flag: "AUTONOMY_LIVE_ENABLED is not set on the host, so NSE live execution is off at the master flag.",
  separate_live_credential: "The separate UPSTOX_LIVE_ACCESS_TOKEN is missing; PAPER/read-only keys cannot unlock LIVE.",
  algo_name_configured: "UPSTOX_ALGO_NAME is not configured (and the SEBI static IP is not registered).",
  static_ip_configured: "No SEBI static IP is configured for the Upstox live account.",
  catalog_authoritative_today: "The NSE instrument catalog is not authoritative for today.",
  market_session_open: "The exchange session is closed right now.",
  calendar_session_open: "The exchange calendar says the session is closed.",
  account_verified: "The broker account could not be read with the configured credentials.",
  account_trading_allowed: "The broker reports this account as blocked from trading.",
  account_currency_inr: "The NSE account is not an INR account.",
  account_currency_usd: "The NASDAQ account is not a USD account.",
  market_quote_available: "No live quote is available for the requested symbol.",
  broker_enabled: "The Alpaca broker service is disabled on this server.",
  alpaca_broker_configured: "No Alpaca live broker service is bound to this server.",
  service_unblocked: "The Alpaca broker service is latched after an unknown outcome; reconcile first.",
  reconciliation_ok: "The last broker reconciliation did not succeed.",
  environment_is_live: "The Alpaca service is bound to PAPER keys, not the separate live keys.",
  clock_synced: "The server clock is outside the allowed skew from the broker clock.",
  daily_loss_within_limit: "The account's daily loss has reached the configured live limit.",
});

function switchSpec(market) {
  const wanted = String(market || "").toUpperCase();
  return SWITCH_MARKETS.find((spec) => spec.market === wanted) || null;
}

function switchStateFor(market) {
  if (!state.liveSwitch || typeof state.liveSwitch !== "object") state.liveSwitch = {};
  const mkt = String(market || "").toUpperCase();
  if (!state.liveSwitch[mkt]) {
    state.liveSwitch[mkt] = {
      report: null,          // readiness evidence from the last explicit read
      readAt: 0,             // ms timestamp of that read
      modeChangedAt: 0,      // any mode change invalidates the evidence above
      error: "",
      busy: false,
    };
  }
  return state.liveSwitch[mkt];
}

function switchBlockers(report) {
  const failed = (report && report.failed) || [];
  return failed.filter((name) => name !== SWITCH_MODE_STEP_CHECK);
}

function switchBlockerText(name) {
  if (SWITCH_BLOCKER_MEANING[name]) return SWITCH_BLOCKER_MEANING[name];
  const human = String(name || "").replace(/_/g, " ").trim();
  return `Server check "${human}" did not pass.`;
}

function switchEvidenceFresh(st) {
  return Boolean(st.report) && Number(st.readAt || 0) > Number(st.modeChangedAt || 0);
}

function switchOverviewRow(market) {
  const exec = state.mm.overview?.live_execution?.markets;
  return (exec && exec[String(market).toUpperCase()]) || null;
}

function switchMode(market) {
  const row = switchOverviewRow(market);
  const modes = state.mm.overview?.execution_modes || {};
  return String((row && row.mode) || modes[String(market).toUpperCase()] || "PAPER").toUpperCase();
}

function switchGateState(market) {
  const row = switchOverviewRow(market);
  return String((row && row.gate && row.gate.state) || "DISABLED").toUpperCase();
}

function switchExitAckRequired(report) {
  return Boolean(report && report.exitRoute && report.exitRoute.routed === false);
}

function setSwitchOutput(message, tone) {
  const el = $("mm-switch-output");
  if (!el) return;
  el.className = `inline-status top-space ${tone || ""}`.trim();
  el.textContent = message;
}

function switchLimitsSummary(row) {
  const limits = (row && row.risk_limits) || {};
  if (!limits.configured) {
    return "NOT configured — all eight per-market limits must be positive before LIVE can be armed.";
  }
  return [
    `order ≤ ${fmt(limits.max_order_value, 0)}`,
    `${fmt(limits.max_daily_orders, 0)} orders/day`,
    `daily loss ≤ ${fmt(limits.max_daily_loss, 0)}`,
    `open ≤ ${fmt(limits.max_open_positions, 0)}`,
    `symbol exposure ≤ ${fmt(limits.max_symbol_exposure, 0)}`,
    `total exposure ≤ ${fmt(limits.max_total_exposure, 0)}`,
    `quote age ≤ ${fmt(limits.max_quote_age_seconds, 0)}s`,
    `deviation ≤ ${fmt(limits.max_price_deviation_bps, 0)} bps`,
  ].join(" · ");
}

function switchAccountSummary(market, report) {
  const creds = (report && report.credentials) || [];
  const present = creds.filter((row) => row.configured).map((row) => row.name);
  const missing = creds.filter((row) => !row.configured).map((row) => row.name);
  const parts = [];
  if (market === "NASDAQ") {
    const account = state.alpacaBroker?.account;
    const number = account && (account.account_number || account.id);
    if (number) parts.push(`Alpaca account ••••${String(number).slice(-4)}`);
    else parts.push("Alpaca account identity not returned by the current status route");
  } else {
    parts.push("Upstox account identity is not exposed by any current read-only route");
  }
  if (present.length) parts.push(`credentials present: ${present.join(", ")}`);
  if (missing.length) parts.push(`credentials missing: ${missing.join(", ")}`);
  if (!creds.length) parts.push("credential presence is read by the readiness step");
  return parts.join(" · ");
}

function switchOpenOrderPolicyText(report) {
  const policy = (report && report.openOrderPolicy) || null;
  if (!policy || !policy.blocks_new_orders) {
    return "Read by the readiness step: a kill switch blocks new orders and leaves working orders alone unless cancel_working is explicitly true.";
  }
  return [
    `kill switch blocks new orders: ${policy.blocks_new_orders ? "yes" : "no"}`,
    `working orders: ${policy.working_orders}`,
    `unresolved intents: ${policy.unresolved_intents}`,
    `reset: ${policy.reset}`,
  ].join(" · ");
}

function switchExitHtml(spec, report) {
  const key = spec.key;
  if (!report) {
    return `<div class="switch-exit" id="mm-switch-exit-${key}">Exit route: not read yet. Step 1 reads this market's documented exit route (read-only); LIVE stays blocked until it has been read.</div>`;
  }
  const route = report.exitRoute;
  if (!route) {
    return `<div class="switch-exit" id="mm-switch-exit-${key}">Exit route: the server reported no exit route for this market. Treat LIVE as having no programmatic exit.</div>`;
  }
  if (route.routed) {
    return `<div class="switch-exit ok" id="mm-switch-exit-${key}"><b>Exit route: routed.</b> ${escapeHtml(route.summary || "")}</div>`;
  }
  return `<div class="switch-exit" id="mm-switch-exit-${key}"><b>NO ROUTED EXIT — AXIOM cannot close a ${spec.market} position.</b>
    ${escapeHtml(route.summary || "")}
    Closing path: ${escapeHtml((route.closing_paths || []).join(" · ") || "none documented")}
    Operator fallback: ${escapeHtml(route.operator_fallback || "none documented")}
    No exit order is offered or sent from this page, so a live ${spec.market} position must be closed by you, not by this switch.</div>`;
}

function switchBlockerListHtml(spec, report) {
  const key = spec.key;
  if (!report) {
    return `<ul class="switch-blockers" id="mm-switch-blockers-${key}"><li>No readiness read yet — LIVE is disabled until the server reports no blocking check.</li></ul>`;
  }
  const blockers = switchBlockers(report);
  const missing = report.missing || [];
  if (!blockers.length && !missing.length) {
    return `<ul class="switch-blockers" id="mm-switch-blockers-${key}"><li>No blocking check. ${report.checks?.[SWITCH_MODE_STEP_CHECK] ? "mode is already LIVE." : "mode=LIVE is the only remaining readiness step and this switch performs it at step 2."}</li></ul>`;
  }
  const items = blockers.map((name) => `<li><code>${escapeHtml(name)}</code> — ${escapeHtml(switchBlockerText(name))}</li>`);
  for (const dep of missing) items.push(`<li>Missing dependency — ${escapeHtml(dep)}</li>`);
  return `<ul class="switch-blockers" id="mm-switch-blockers-${key}">${items.join("")}</ul>`;
}

function switchStepsHtml(spec, ctx) {
  const { st, mode, gateState, fresh, blockers, report } = ctx;
  const key = spec.key;
  const modeLive = mode === "LIVE";
  const armed = gateState === "ARMED";
  const active = gateState === "ACTIVE";
  const canRead = !st.busy;
  const canMode = !st.busy && Boolean(report) && blockers.length === 0 && !modeLive;
  const canArm = !st.busy && modeLive && !armed && !active && fresh && blockers.length === 0;
  const step = (index, done, current, title, detail, control) => `
    <div class="switch-step ${done ? "done" : current ? "current" : "pending"}">
      <span class="step-index">${done ? "✓" : index}</span>
      <span class="step-text"><b>${escapeHtml(title)}</b> — ${escapeHtml(detail)}</span>
      ${control || ""}
    </div>`;

  const readDetail = !report
    ? "readiness has not been read"
    : fresh
      ? `${blockers.length} blocking check(s) · mode ${mode}`
      : "mode changed since the last read; read again";
  const armDetail = !modeLive
    ? "mode must be LIVE first"
    : active
      ? "gate is already ACTIVE"
      : armed
        ? "gate is ARMED"
        : fresh && blockers.length === 0
          ? "ready to arm (the server re-checks everything)"
          : "readiness must be read again with no blocker";

  return `
    <div class="switch-step ${report ? "done" : "current"}">
      <span class="step-index">${report ? "✓" : 1}</span>
      <span class="step-text"><b>Read readiness</b> — ${escapeHtml(readDetail)}</span>
      <button type="button" class="button secondary xs" id="mm-switch-check-${key}"
        data-switch-action="check" data-switch-market="${spec.market}" ${canRead ? "" : "disabled"}>Read readiness (GET only)</button>
    </div>
    <div class="switch-step ${modeLive ? "done" : report && blockers.length === 0 ? "current" : "pending"}">
      <span class="step-index">${modeLive ? "✓" : 2}</span>
      <span class="step-text"><b>Select LIVE mode</b> — sets mode=LIVE only; the gate stays DISARMED</span>
      <button type="button" class="button warning xs" id="mm-switch-mode-${key}"
        data-switch-action="mode-live" data-switch-market="${spec.market}" ${canMode ? "" : "disabled"}>2. Set mode LIVE</button>
    </div>
    ${step(3, armed || active, modeLive && !armed && !active, "Arm the gate", armDetail,
      `<button type="button" class="button warning xs" id="mm-switch-arm-${key}" data-switch-action="arm" data-switch-market="${spec.market}" ${canArm ? "" : "disabled"}>3. Arm</button>`)}
    ${step(4, active, armed, "Acknowledge risk, type the phrase, activate",
      active ? "gate is ACTIVE — real-money orders may route while it stays ACTIVE" : "each control below is required; nothing activates on its own",
      `<button type="button" class="button danger xs" id="mm-switch-activate-${key}" data-switch-action="activate" data-switch-market="${spec.market}" disabled>4. Activate LIVE</button>`)}
  `;
}

function renderPaperLiveSwitch(ov) {
  const host = $("mm-switch-cards");
  if (!host) return;
  /* Re-rendering must never eat what the operator typed: snapshot first. */
  const typed = {};
  for (const spec of SWITCH_MARKETS) {
    typed[spec.key] = {
      phrase: $(`mm-switch-phrase-${spec.key}`)?.value || "",
      risk: Boolean($(`mm-switch-ack-${spec.key}`)?.checked),
      exit: Boolean($(`mm-switch-exit-ack-${spec.key}`)?.checked),
    };
  }

  const exec = (ov && ov.live_execution && ov.live_execution.markets) || null;
  const modes = (ov && ov.execution_modes) || {};
  const summary = {};

  const cards = SWITCH_MARKETS.map((spec) => {
    const st = switchStateFor(spec.market);
    const row = (exec && exec[spec.market]) || null;
    const mode = String((row && row.mode) || modes[spec.market] || "PAPER").toUpperCase();
    const gate = (row && row.gate) || {};
    const gateState = String(gate.state || "DISABLED").toUpperCase();
    const report = st.report;
    const blockers = switchBlockers(report);
    const fresh = switchEvidenceFresh(st);
    const modeLive = mode === "LIVE";
    const active = gateState === "ACTIVE";
    const liveEnabled = Boolean(report) && fresh && blockers.length === 0 && !modeLive;
    const exitAckNeeded = switchExitAckRequired(report);
    summary[spec.market] = { mode, gateState, liveEnabled };

    let liveReason;
    if (modeLive && active) liveReason = "LIVE is ACTIVE for this market. PAPER disarms the gate and revokes the permit; no working order or protective exit is touched.";
    else if (modeLive) liveReason = "mode=LIVE but the gate is not ARMED — LIVE is not active until steps 3 and 4 are completed.";
    else if (!report) liveReason = "LIVE is disabled: run step 1 (readiness) first. Nothing here changes the mode before every check passes.";
    else if (!fresh) liveReason = "LIVE is disabled: the mode changed since the last readiness read — read readiness again (step 1).";
    else if (blockers.length) liveReason = `LIVE is disabled by ${blockers.length} blocking readiness check(s): ${blockers.join(", ")}. A click here changes nothing.`;
    else liveReason = "LIVE can be selected: every blocking check passes. Steps 2-4 still run one at a time.";

    const stateLabel = `${spec.market} · ${mode}${active ? " · ACTIVE" : ""}`;
    const stateTone = active ? "err" : modeLive ? "warn" : "info";

    return `
      <div class="provider-card mode-switch" id="mm-switch-card-${spec.key}" data-switch-market="${spec.market}">
        <div class="panel-head" style="margin-bottom:4px">
          <strong>${escapeHtml(spec.market)} · ${escapeHtml(spec.money)}</strong>
          <span class="status-pill ${stateTone}" id="mm-switch-state-${spec.key}">${escapeHtml(stateLabel)}</span>
        </div>
        <div class="mode-switch-track" role="group" aria-label="${escapeHtml(spec.market)} execution mode">
          <button type="button" class="mode-switch-choice" id="mm-switch-paper-${spec.key}"
            data-switch-action="paper" data-switch-market="${spec.market}"
            aria-pressed="${mode === "PAPER" ? "true" : "false"}" ${mode === "PAPER" ? "disabled" : ""}>PAPER · simulated</button>
          <button type="button" class="mode-switch-choice live" id="mm-switch-live-${spec.key}"
            data-switch-action="live" data-switch-market="${spec.market}"
            aria-pressed="${modeLive ? "true" : "false"}" ${liveEnabled || modeLive ? "" : "disabled"}>LIVE · real money</button>
        </div>
        <div class="muted small" id="mm-switch-live-reason-${spec.key}">${escapeHtml(liveReason)}</div>
        <div class="switch-facts" id="mm-switch-facts-${spec.key}">
          <span>Broker: <b>${escapeHtml((row && row.broker) || (report && report.brokerLabel) || "—")}</b>${row && row.destination_label ? ` · ${escapeHtml(row.destination_label)}` : ""}</span>
          <span>Account / credentials: <b>${escapeHtml(switchAccountSummary(spec.market, report))}</b></span>
          <span>Gate: <b id="mm-switch-gate-${spec.key}">${escapeHtml(gateState)}</b> · kill switch: <b>${escapeHtml(gate.kill_engaged ? `ENGAGED (${gate.kill_reason || "reason not reported"})` : "not engaged")}</b></span>
          <span>Configured limits: <b>${escapeHtml(switchLimitsSummary(row))}</b></span>
          <span>Open-order policy: ${escapeHtml(switchOpenOrderPolicyText(report))}</span>
          <span>Readiness: <b>${escapeHtml(report ? `${blockers.length} blocking check(s) · read ${timeText(report.readAtIso)}` : "not read yet — LIVE disabled")}</b></span>
        </div>
        ${switchExitHtml(spec, report)}
        ${switchBlockerListHtml(spec, report)}
        <div class="switch-steps" id="mm-switch-steps-${spec.key}">
          ${switchStepsHtml(spec, { st, mode, gateState, fresh, blockers, report })}
        </div>
        <div class="field">
          <label class="muted small"><input type="checkbox" id="mm-switch-ack-${spec.key}"> I acknowledge ${escapeHtml(spec.market)} live orders use real ${escapeHtml(spec.currency)}</label>
          ${exitAckNeeded ? `<label class="muted small"><input type="checkbox" id="mm-switch-exit-ack-${spec.key}"> I understand AXIOM routes no ${escapeHtml(spec.market)} exit; I must close any position myself</label>` : ""}
          <label class="muted small" for="mm-switch-phrase-${spec.key}">Type ${escapeHtml(SWITCH_PHRASE)} to enable Activate</label>
          <input id="mm-switch-phrase-${spec.key}" type="text" autocomplete="off" spellcheck="false" placeholder="${escapeHtml(SWITCH_PHRASE)}">
        </div>
        <div class="muted small" id="mm-switch-activate-hint-${spec.key}"></div>
      </div>`;
  });

  host.innerHTML = cards.join("");

  /* Restore anything the operator had already entered. */
  for (const spec of SWITCH_MARKETS) {
    const saved = typed[spec.key] || {};
    const phrase = $(`mm-switch-phrase-${spec.key}`);
    if (phrase && document.activeElement !== phrase) phrase.value = saved.phrase || "";
    const risk = $(`mm-switch-ack-${spec.key}`);
    if (risk) risk.checked = Boolean(saved.risk);
    const exit = $(`mm-switch-exit-ack-${spec.key}`);
    if (exit) exit.checked = Boolean(saved.exit);
    switchSyncActivate(spec);
  }

  for (const spec of SWITCH_MARKETS) {
    const pill = $(`mm-switch-summary-${spec.key}`);
    const state = summary[spec.market] || {};
    if (!pill) continue;
    pill.textContent = `${spec.market} · ${state.mode || "PAPER"}${state.gateState === "ACTIVE" ? " · LIVE ACTIVE" : ""}`;
    pill.className = `status-pill ${state.gateState === "ACTIVE" ? "err" : state.mode === "LIVE" ? "warn" : "info"}`;
  }
}

function switchActivateChecks(spec) {
  const key = spec.key;
  const st = switchStateFor(spec.market);
  const mode = switchMode(spec.market);
  const gateState = switchGateState(spec.market);
  const exitNeeded = switchExitAckRequired(st.report);
  const phrase = ($(`mm-switch-phrase-${key}`)?.value || "").trim();
  const risk = Boolean($(`mm-switch-ack-${key}`)?.checked);
  const exitOk = !exitNeeded || Boolean($(`mm-switch-exit-ack-${key}`)?.checked);
  const reasons = [];
  if (mode !== "LIVE") reasons.push("mode is not LIVE yet (step 2)");
  if (gateState === "ACTIVE") reasons.push("the gate is already ACTIVE");
  else if (gateState !== "ARMED") reasons.push("the gate is not ARMED yet (step 3)");
  if (!risk) reasons.push("the risk acknowledgement is required");
  if (!exitOk) reasons.push("the no-routed-exit acknowledgement is required for this market");
  if (phrase !== SWITCH_PHRASE) reasons.push(`the exact phrase "${SWITCH_PHRASE}" is required`);
  return { enabled: reasons.length === 0, reasons };
}

function switchSyncActivate(spec) {
  const button = $(`mm-switch-activate-${spec.key}`);
  if (!button) return;
  const { enabled, reasons } = switchActivateChecks(spec);
  button.disabled = !enabled || switchStateFor(spec.market).busy;
  const hint = $(`mm-switch-activate-hint-${spec.key}`);
  if (hint) hint.textContent = enabled ? "Activate is enabled: one click sends the existing activation request." : `Activate locked — ${reasons.join("; ")}.`;
}

async function switchReadReadiness(spec) {
  const st = switchStateFor(spec.market);
  const [report, market] = await Promise.all([
    api(ROUTES.liveReadinessReport, { keepAuth: true }),
    liveAction(spec.market, "readiness"),
  ]);
  const row = ((report && report.markets) || []).find(
    (entry) => String(entry.market || "").toUpperCase() === spec.market,
  ) || {};
  st.report = {
    ok: Boolean(market && market.ok),
    failed: (market && market.failed) || [],
    checks: (market && market.checks) || {},
    missing: (market && market.missing_dependencies) || [],
    gate: (market && market.gate) || {},
    exitRoute: row.exit_route || (market && market.documented_capabilities && market.documented_capabilities.exit_route) || null,
    openOrderPolicy: (report && report.open_order_policy) || null,
    credentials: row.credential_requirements || [],
    brokerLabel: row.broker_label || "",
    readAtIso: new Date().toISOString(),
  };
  st.readAt = Date.now();
  st.error = "";
  const blockers = switchBlockers(st.report);
  const exitNote = switchExitAckRequired(st.report)
    ? " No routed exit for this market: the exit facts above must be acknowledged before Activate."
    : "";
  if (blockers.length) {
    setSwitchOutput(`${spec.market} readiness read: ${blockers.length} blocking check(s) — ${blockers.join(", ")}. LIVE stays disabled; a click on LIVE changes nothing.${exitNote}`, "err");
  } else {
    setSwitchOutput(`${spec.market} readiness read: no blocking check (mode=LIVE is the step this switch performs). Select LIVE at step 2; Arm and the typed Activate still run separately.${exitNote}`, "warn");
  }
}

async function switchReturnToPaper(spec) {
  const st = switchStateFor(spec.market);
  const res = await liveAction(spec.market, "mode", { mode: "PAPER" });
  st.modeChangedAt = Date.now();
  st.report = null;     // the old readiness evidence described the previous mode
  st.error = "";
  setSwitchOutput(
    `${spec.market} is back on PAPER (mode=${res.mode || "PAPER"}). The server disarmed the gate and revoked the live permit. ` +
    "No working order and no protective exit was cancelled, modified, or tested by this switch.",
    "ok",
  );
  await loadMultiMarketOverview();
}

async function switchSetLiveMode(spec) {
  const st = switchStateFor(spec.market);
  const res = await liveAction(spec.market, "mode", { mode: "LIVE" });
  st.modeChangedAt = Date.now();
  st.report = null;     // mode is one of the readiness checks: the evidence is now stale
  st.error = "";
  setSwitchOutput(
    `${spec.market} execution mode is now ${res.mode || "LIVE"}. The gate is still DISARMED: read readiness again (step 1), then Arm, ` +
    "then the risk acknowledgement + exact phrase + Activate. No order can be routed in between.",
    "warn",
  );
  await loadMultiMarketOverview();
}

async function switchArmGate(spec) {
  const st = switchStateFor(spec.market);
  const res = await liveAction(spec.market, "arm");
  st.error = "";
  setSwitchOutput(
    `${spec.market} gate is ${res.gate?.state || "ARMED"}. Nothing has been sent to the broker: the typed phrase and Activate are still required, ` +
    "and no strategy was started.",
    "warn",
  );
  await loadMultiMarketOverview();
}

async function switchActivateLive(spec) {
  const st = switchStateFor(spec.market);
  const { enabled, reasons } = switchActivateChecks(spec);
  if (!enabled) throw new Error(reasons.join("; "));
  const phrase = ($(`mm-switch-phrase-${spec.key}`)?.value || "").trim();
  const risk = Boolean($(`mm-switch-ack-${spec.key}`)?.checked);
  const res = await liveAction(spec.market, "activate", {
    confirm_live_trading: phrase,
    acknowledge_risk: risk,
  });
  const phraseInput = $(`mm-switch-phrase-${spec.key}`);
  if (phraseInput) phraseInput.value = "";
  const riskInput = $(`mm-switch-ack-${spec.key}`);
  if (riskInput) riskInput.checked = false;
  const exitInput = $(`mm-switch-exit-ack-${spec.key}`);
  if (exitInput) exitInput.checked = false;
  setSwitchOutput(
    `${spec.market} LIVE gate is ${res.gate?.state || "ACTIVE"}. Real-money orders may be routed by the existing coordinator while the gate stays ACTIVE. ` +
    "This switch did not start or resume a strategy; Emergency Stop remains below in the live execution panel.",
    "err",
  );
  /* Deliberately no strategy/paper-session call here: activating LIVE must not
   * start or resume trading, and this console has no such call in this path. */
  await loadMultiMarketOverview();
}

async function switchAction(spec, action) {
  if (action === "check") return switchReadReadiness(spec);
  if (action === "paper") return switchReturnToPaper(spec);
  if (action === "mode-live") return switchSetLiveMode(spec);
  if (action === "arm") return switchArmGate(spec);
  if (action === "activate") return switchActivateLive(spec);
  if (action === "live") {
    const st = switchStateFor(spec.market);
    const blockers = switchBlockers(st.report);
    if (!st.report || !switchEvidenceFresh(st) || blockers.length) {
      throw new Error("LIVE is disabled until a fresh readiness read reports no blocking check");
    }
    setSwitchOutput(
      `${spec.market}: LIVE chosen for review only — no execution mode changed and no gate was armed. ` +
      "Complete steps 1-4 below; each one is a separate deliberate click.",
      "warn",
    );
    return undefined;
  }
  throw new Error(`unknown switch action ${action}`);
}

function bindPaperLiveSwitchEvents() {
  const host = $("mm-switch-cards");
  if (!host) return;
  // Idempotent: a second bind would double-submit a real-money action.
  if (host.dataset.switchBound === "1") return;
  host.dataset.switchBound = "1";

  host.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-switch-action]");
    if (!button || button.disabled) return;
    const spec = switchSpec(button.dataset.switchMarket);
    if (!spec) return;
    const st = switchStateFor(spec.market);
    if (st.busy) return;
    st.busy = true;
    button.disabled = true;
    try {
      await switchAction(spec, button.dataset.switchAction);
    } catch (err) {
      st.error = `${spec.market} ${button.dataset.switchAction} failed: ${err.message}`;
      setSwitchOutput(st.error, "err");
      // Any failed or refused answer invalidates the displayed evidence: the card
      // must never keep claiming a passing check that the server did not confirm.
      st.report = null;
    } finally {
      st.busy = false;
      renderPaperLiveSwitch(state.mm.overview);
    }
  });

  // Typing or ticking never re-renders: it only re-evaluates the Activate lock.
  const sync = (event) => {
    const card = event.target.closest("[data-switch-market]");
    const spec = card ? switchSpec(card.dataset.switchMarket) : null;
    if (spec) switchSyncActivate(spec);
  };
  host.addEventListener("input", sync);
  host.addEventListener("change", sync);
}

function bindMultiMarketEvents() {
  if (!$("mm-venue-nse")) return;

  // Venue Switcher
  for (const id of ["mm-venue-nse", "mm-venue-nasdaq"]) {
    $(id)?.addEventListener("click", () => {
      const v = $(id).dataset.mmVenue;
      state.mm.activeVenue = v;
      $("mm-venue-nse").classList.toggle("active", v === "NSE");
      $("mm-venue-nse").setAttribute("aria-pressed", String(v === "NSE"));
      $("mm-venue-nasdaq").classList.toggle("active", v === "NASDAQ");
      $("mm-venue-nasdaq").setAttribute("aria-pressed", String(v === "NASDAQ"));
      if ($("mm-order-market")) $("mm-order-market").value = v;
      if ($("mm-ticket-currency-badge")) $("mm-ticket-currency-badge").textContent = v === "NSE" ? "INR ₹" : "USD $";
    });
  }

  // Quick Symbol Jump
  $("mm-quick-search-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const raw = ($("mm-quick-search-input")?.value || "").trim().toUpperCase();
    if (!raw) return;
    let mkt = state.mm.activeVenue;
    let sym = raw;
    if (raw.includes(":")) {
      const [m, s] = raw.split(":", 2);
      if (m === "NSE" || m === "NASDAQ") { mkt = m; sym = s; }
    } else if (["AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "QQQ", "TQQQ", "AMD", "NFLX"].includes(raw)) {
      mkt = "NASDAQ";
    } else if (["RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "SBIN", "NIFTYBEES"].includes(raw) || raw.startsWith("NSE_")) {
      mkt = "NSE";
    }
    state.mm.selectedInstrumentKey = sym.includes("|") ? sym : "";
    state.mm.selectedChartSym = sym;
    state.mm.ticketInstrumentKey = sym.includes("|") ? sym : "";
    state.mm.ticketSymbol = sym;
    await goPage("charts");
    if ($("mm-chart-source-mode")) $("mm-chart-source-mode").value = mkt;
    if ($("mm-chart-symbol-input")) $("mm-chart-symbol-input").value = sym;
    switchTicketTab("MULTIMARKET");
    if ($("mm-order-market")) $("mm-order-market").value = mkt;
    if ($("mm-order-symbol")) $("mm-order-symbol").value = sym;
    await loadMultiMarketChart();
  });

  // Delegated clicks for jump-chart, jump-ticket, watch, unwatch, cancel, close, modify
  document.addEventListener("click", async (e) => {
    const target = e.target.closest("button");
    if (!target) return;
    if (target.dataset.mmJumpChart) {
      const idx = target.dataset.mmJumpChart.indexOf(":");
      const mkt = idx >= 0 ? target.dataset.mmJumpChart.slice(0, idx) : state.mm.activeVenue;
      const sym = idx >= 0 ? target.dataset.mmJumpChart.slice(idx + 1) : target.dataset.mmJumpChart;
      const ikey = target.dataset.mmIkey || "";
      state.mm.selectedInstrumentKey = mkt === "NSE" ? ikey : "";
      state.mm.selectedChartSym = sym;
      state.mm.ticketInstrumentKey = mkt === "NSE" ? ikey : "";
      state.mm.ticketSymbol = sym;
      await goPage("charts");
      $("mm-chart-source-mode").value = mkt;
      $("mm-chart-symbol-input").value = sym;
      await loadMultiMarketChart();
    } else if (target.dataset.mmJumpTicket) {
      const idx = target.dataset.mmJumpTicket.indexOf(":");
      const mkt = idx >= 0 ? target.dataset.mmJumpTicket.slice(0, idx) : state.mm.activeVenue;
      const sym = idx >= 0 ? target.dataset.mmJumpTicket.slice(idx + 1) : target.dataset.mmJumpTicket;
      const ikey = target.dataset.mmIkey || "";
      state.mm.ticketInstrumentKey = mkt === "NSE" ? ikey : "";
      state.mm.ticketSymbol = sym;
      await goPage("charts");
      switchTicketTab("MULTIMARKET");
      $("mm-order-market").value = mkt;
      $("mm-order-symbol").value = sym;
      $("mm-ticket-currency-badge").textContent = mkt === "NSE" ? "INR ₹" : "USD $";
    } else if (target.dataset.mmWatch) {
      const idx = target.dataset.mmWatch.indexOf(":");
      const market = idx >= 0 ? target.dataset.mmWatch.slice(0, idx) : state.mm.activeVenue;
      const symbol = idx >= 0 ? target.dataset.mmWatch.slice(idx + 1) : target.dataset.mmWatch;
      const ikey = target.dataset.mmIkey || "";
      try {
        const watchBody = { market, symbol };
        if (market === "NSE" && ikey) watchBody.instrument_key = ikey;
        await api("/api/markets/watchlists", { body: watchBody });
        await loadMultiMarketOverview();
        toast("Added to watchlist", `${market}:${ikey || symbol} pinned.`, "ok");
      } catch (err) { toast("Watchlist failed", err.message, "err"); }
    } else if (target.dataset.mmUnwatch) {
      const idx = target.dataset.mmUnwatch.indexOf(":");
      const market = idx >= 0 ? target.dataset.mmUnwatch.slice(0, idx) : state.mm.activeVenue;
      const symbol = idx >= 0 ? target.dataset.mmUnwatch.slice(idx + 1) : target.dataset.mmUnwatch;
      const targetRef = (market === "NSE" && target.dataset.mmIkey) ? target.dataset.mmIkey : symbol;
      try {
        await api(`/api/markets/watchlists/${encodeURIComponent(market)}/${encodeURIComponent(targetRef)}`, { method: "DELETE" });
        await loadMultiMarketOverview();
        toast("Removed from watchlist", `${market}:${targetRef} removed.`, "ok");
      } catch (err) { toast("Watchlist failed", err.message, "err"); }
    } else if (target.dataset.mmCancelOrder) {
      try {
        await api(`/api/markets/paper/orders/${encodeURIComponent(target.dataset.mmCancelOrder)}`, { method: "DELETE" });
        await loadMultiMarketOverview();
        toast("Simulated paper order canceled", target.dataset.mmCancelOrder, "ok");
      } catch (err) { toast("Cancel failed", err.message, "err"); }
    } else if (target.dataset.mmPosClose) {
      const [mkt, sym] = target.dataset.mmPosClose.split(":");
      const targetRef = (mkt === "NSE" && target.dataset.mmIkey) ? target.dataset.mmIkey : sym;
      try {
        await api(`/api/markets/paper/positions/${encodeURIComponent(mkt)}/${encodeURIComponent(targetRef)}/close`, { body: {} });
        await loadMultiMarketOverview();
        toast("Paper position closed", `${mkt}:${targetRef} closed against live quote.`, "ok");
      } catch (err) { toast("Position close blocked", err.message, "err"); }
    } else if (target.dataset.mmPosProt) {
      const [mkt, sym, slCur, tpCur] = target.dataset.mmPosProt.split(":");
      const ikey = target.dataset.mmIkey || "";
      openDialog({
        title: `Update ${mkt}:${ikey || sym} Paper Protection (SL / TP)`,
        body: `<div class="form-grid"><div class="field"><label for="mm-mod-sl">Stop-loss</label><input id="mm-mod-sl" type="number" step="any" value="${escapeHtml(slCur)}"></div><div class="field"><label for="mm-mod-tp">Take-profit</label><input id="mm-mod-tp" type="number" step="any" value="${escapeHtml(tpCur)}"></div></div>`,
        confirmLabel: "Save SL / TP",
        confirmClass: "primary",
        onConfirm: async () => {
          const stop_loss = $("mm-mod-sl").value ? Number($("mm-mod-sl").value) : null;
          const take_profit = $("mm-mod-tp").value ? Number($("mm-mod-tp").value) : null;
          const protBody = { market: mkt, symbol: sym, stop_loss, take_profit };
          if (mkt === "NSE" && ikey) protBody.instrument_key = ikey;
          await api("/api/markets/paper/positions/protection", { body: protBody });
          await loadMultiMarketOverview();
          toast("Protection updated", `${mkt}:${ikey || sym} SL/TP updated.`, "ok");
        },
      });
    }
  });

  // Sync quotes & Reconcile
  $("mm-nse-sync-btn")?.addEventListener("click", async () => {
    try { await api("/api/markets/paper/NSE/sync", { body: {} }); await loadMultiMarketOverview(); toast("NSE paper synced", "Quotes & triggers evaluated.", "ok"); }
    catch (err) { toast("NSE sync notice", err.message, "warn"); }
  });
  $("mm-nasdaq-sync-btn")?.addEventListener("click", async () => {
    try { await api("/api/markets/paper/NASDAQ/sync", { body: {} }); await loadMultiMarketOverview(); toast("NASDAQ paper synced", "Quotes & triggers evaluated.", "ok"); }
    catch (err) { toast("NASDAQ sync notice", err.message, "warn"); }
  });
  $("mm-reconcile-paper-btn")?.addEventListener("click", async () => {
    try {
      const r1 = await api("/api/markets/paper/NSE/reconcile", { body: {} });
      const r2 = await api("/api/markets/paper/NASDAQ/reconcile", { body: {} });
      toast("SQLite & Hash Chain Reconciled", `NSE valid=${r1.audit_chain_valid} · NASDAQ valid=${r2.audit_chain_valid} (${r1.audit_events_verified} events)`, "ok");
    } catch (err) { toast("Reconciliation failed", err.message, "err"); }
  });

  // Catalog Search & Master Sync
  $("mm-catalog-search-btn")?.addEventListener("click", searchMultiMarketCatalog);
  $("mm-catalog-query")?.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); searchMultiMarketCatalog(); } });
  $("mm-catalog-market")?.addEventListener("change", searchMultiMarketCatalog);
  $("mm-catalog-subclass")?.addEventListener("change", searchMultiMarketCatalog);
  $("mm-refresh-nse-bod")?.addEventListener("click", async () => {
    setDisabled("mm-refresh-nse-bod", true, "Downloading Upstox BOD master…");
    try {
      const rep = await api("/api/markets/nse/refresh", { body: {} });
      await loadMultiMarketOverview(); await searchMultiMarketCatalog();
      toast("NSE BOD Master Reconciled", `${rep.tradable_count || rep.counts?.tradable || 0} tradable instruments reconciled.`, "ok");
    } catch (err) { toast("NSE BOD refresh failed", err.message, "err"); }
    finally { setDisabled("mm-refresh-nse-bod", false); }
  });
  $("mm-sync-nasdaq-assets")?.addEventListener("click", async () => {
    setDisabled("mm-sync-nasdaq-assets", true, "Syncing Alpaca NASDAQ universe…");
    try {
      const rep = await api("/api/markets/nasdaq/sync", { body: {} });
      await loadMultiMarketOverview(); await searchMultiMarketCatalog();
      toast("NASDAQ Universe Synced", `${rep.counts?.total || 0} active NASDAQ stocks & ETFs synced.`, "ok");
    } catch (err) { toast("NASDAQ sync blocked", err.message, "err"); }
    finally { setDisabled("mm-sync-nasdaq-assets", false); }
  });

  // Superchart Feed Mode & Multi-Market Ticket
  $("mm-chart-source-mode")?.addEventListener("change", () => {
    state.mm.selectedInstrumentKey = "";
    state.mm.selectedChartSym = "";
    loadMultiMarketChart();
  });
  $("mm-chart-symbol-input")?.addEventListener("input", () => {
    const val = ($("mm-chart-symbol-input")?.value || "").trim();
    state.mm.selectedInstrumentKey = val.includes("|") ? val : "";
    state.mm.selectedChartSym = val;
  });
  $("mm-load-live-chart-btn")?.addEventListener("click", loadMultiMarketChart);
  $("ticket-tab-session")?.addEventListener("click", () => switchTicketTab("SESSION"));
  $("ticket-tab-mm")?.addEventListener("click", () => switchTicketTab("MULTIMARKET"));

  $("mm-order-market")?.addEventListener("change", () => {
    const m = $("mm-order-market").value;
    $("mm-ticket-currency-badge").textContent = m === "NSE" ? "INR ₹" : "USD $";
    state.mm.ticketInstrumentKey = "";
    state.mm.ticketSymbol = "";
    if (m === "NASDAQ" && $("mm-order-symbol").value === "RELIANCE") $("mm-order-symbol").value = "AAPL";
    if (m === "NSE" && $("mm-order-symbol").value === "AAPL") $("mm-order-symbol").value = "RELIANCE";
  });
  $("mm-order-symbol")?.addEventListener("input", () => {
    const val = ($("mm-order-symbol")?.value || "").trim();
    state.mm.ticketInstrumentKey = val.includes("|") ? val : "";
    state.mm.ticketSymbol = val;
  });
  $("mm-side-buy")?.addEventListener("click", () => { state.mm.side = "BUY"; $("mm-side-buy").classList.add("active"); $("mm-side-sell").classList.remove("active"); });
  $("mm-side-sell")?.addEventListener("click", () => { state.mm.side = "SELL"; $("mm-side-sell").classList.add("active"); $("mm-side-buy").classList.remove("active"); });

  $("mm-preview-order-btn")?.addEventListener("click", async () => {
    try {
      const prev = await api("/api/markets/paper/preview", { body: getMultiMarketTicketBody() });
      state.mm.lastPreview = prev;
      renderMultiMarketPreview(prev);
    } catch (err) { toast("Preview failed", err.message, "err"); }
  });
  $("mm-order-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const body = { ...getMultiMarketTicketBody(), client_order_id: newKey("mm-paper") };
      const res = await api("/api/markets/paper/orders", { body });
      if (res.preview) renderMultiMarketPreview(res.preview);
      if (!res.ok) {
        toast("Simulated paper order blocked", (res.preview?.rejection_reasons || ["Guardrail failed"]).join("; "), "err");
        return;
      }
      await loadMultiMarketOverview();
      toast("Simulated paper order recorded", `${res.order.market}:${res.order.symbol} ${res.order.status} · 0 broker orders`, "ok");
    } catch (err) { toast("Paper order error", err.message, "err"); }
  });

  const formatCoverageSummary = (analysis) => {
    if (!analysis || !analysis.counts) return "";
    const c = analysis.counts;
    return `Symbols: ${c.succeeded ?? 0}/${c.selected_symbols ?? 0} succeeded · ${c.missing_data ?? 0} missing · ${c.failed ?? 0} failed`;
  };

  const getSelectedMarketsFromUi = () => {
    const out = [];
    if ($("mm-select-nse")?.checked) out.push("NSE");
    if ($("mm-select-nasdaq")?.checked) out.push("NASDAQ");
    return out;
  };

  for (const cbId of ["mm-select-nse", "mm-select-nasdaq"]) {
    $(cbId)?.addEventListener("change", async () => {
      const selected = getSelectedMarketsFromUi();
      if (!selected.length) {
        $(cbId).checked = true;
        toast("At least one market required", "Keep NSE and/or NASDAQ selected for paper simulation.", "warn");
        return;
      }
      try {
        await api("/api/markets/paper/selection", { body: { markets: selected } });
        await loadMultiMarketOverview();
        toast("Selected markets updated", `Active selection: ${selected.join(" + ")} · ledgers & positions preserved.`, "ok");
      } catch (err) {
        toast("Market selection failed", err.message, "err");
      }
    });
  }

  const startSelectedMarketsConcurrently = async () => {
    const selected = getSelectedMarketsFromUi();
    if (!selected.length) {
      toast("No markets selected", "Select NSE and/or NASDAQ first.", "warn");
      return;
    }
    const strategy_by_market = {
      NSE: $("mm-strat-nse-select")?.value || "parallel_ensemble",
      NASDAQ: $("mm-strat-nasdaq-select")?.value || "parallel_ensemble",
    };
    const timeframe_by_market = {
      NSE: $("mm-strat-nse-tf")?.value || "5m",
      NASDAQ: $("mm-strat-nasdaq-tf")?.value || "5m",
    };
    const kronos_enabled_by_market = {};
    for (const [m, pref] of [["NSE", "mm-nse"], ["NASDAQ", "mm-nasdaq"]]) {
      const km = $(`${pref}-kronos-mode`)?.value || "auto";
      if (km === "enabled") kronos_enabled_by_market[m] = true;
      else if (km === "disabled") kronos_enabled_by_market[m] = false;
    }
    const kronos_policy_by_market = {
      NSE: $("mm-nse-kronos-policy")?.value || "FAIL_CLOSED_NO_ENTRY",
      NASDAQ: $("mm-nasdaq-kronos-policy")?.value || "FAIL_CLOSED_NO_ENTRY",
    };
    try {
      const res = await api("/api/markets/paper/sessions/start", {
        body: {
          markets: selected,
          default_strategy: "parallel_ensemble",
          default_timeframe: "5m",
          strategy_by_market,
          timeframe_by_market,
          kronos_enabled_by_market,
          kronos_policy_by_market,
          execute_immediately: true,
        },
      });
      await loadMultiMarketOverview();
      const parts = selected.map((m) => {
        const s = res.markets?.[m];
        const cov = formatCoverageSummary(s?.initial_analysis || s?.last_analysis);
        return `${m}: ${s?.session_state || "ERROR"}${cov ? ` (${cov})` : ""}`;
      });
      toast("Selected Markets Started (Paper Only)", `${parts.join(" · ")} · 0 broker orders.`, res.ok ? "ok" : "warn");
    } catch (err) {
      toast("Multi-market start blocked", err.message, "err");
    }
  };

  const transitionSelectedMarketsConcurrently = async (action) => {
    const selected = getSelectedMarketsFromUi();
    if (!selected.length) return;
    try {
      const res = await api("/api/markets/paper/sessions/transition", {
        body: { action, markets: selected, reason: `operator multi-market ${action}`, execute_immediately: true },
      });
      await loadMultiMarketOverview();
      const parts = selected.map((m) => {
        const s = res.markets?.[m];
        const cov = action === "resume" ? formatCoverageSummary(s?.initial_analysis || s?.last_analysis) : "";
        return `${m}: ${s?.session_state || "—"}${cov ? ` (${cov})` : ""}`;
      });
      toast(`Selected Markets ${action.toUpperCase()}`, `${parts.join(" · ")} · 0 broker orders.`, "ok");
    } catch (err) {
      toast(`Multi-market ${action} failed`, err.message, "err");
    }
  };

  const analyzeMarketsConcurrently = async (marketsList = null) => {
    const targets = marketsList || getSelectedMarketsFromUi();
    try {
      const res = await api("/api/markets/paper/analyze", { body: { markets: targets } });
      await loadMultiMarketOverview();
      const parts = (res.selected_markets || targets).map((m) => `${m}: ${formatCoverageSummary(res.reports?.[m]) || "no report"}`);
      toast("Immediate Symbol Analysis Complete", `${parts.join(" · ")} · 0 simulated or broker orders.`, "ok");
    } catch (err) {
      toast("Symbol analysis failed", err.message, "err");
    }
  };

  $("mm-start-selected-btn")?.addEventListener("click", startSelectedMarketsConcurrently);
  $("mm-strat-start-both-btn")?.addEventListener("click", startSelectedMarketsConcurrently);
  $("mm-resume-selected-btn")?.addEventListener("click", () => transitionSelectedMarketsConcurrently("resume"));
  $("mm-strat-resume-both-btn")?.addEventListener("click", () => transitionSelectedMarketsConcurrently("resume"));
  $("mm-pause-selected-btn")?.addEventListener("click", () => transitionSelectedMarketsConcurrently("pause"));
  $("mm-stop-selected-btn")?.addEventListener("click", () => transitionSelectedMarketsConcurrently("stop"));
  $("mm-analyze-selected-btn")?.addEventListener("click", () => analyzeMarketsConcurrently());
  $("mm-strat-analyze-both-btn")?.addEventListener("click", () => analyzeMarketsConcurrently());

  // Strategy Session Controls (NSE & NASDAQ)
  const bindPaperLifecycle = (mkt, prefix) => {
    $(`${prefix}-start-btn`)?.addEventListener("click", async () => {
      try {
        const mktLower = mkt.toLowerCase();
        const strategy = $(`mm-strat-${mktLower}-select`).value;
        const timeframe = $(`mm-strat-${mktLower}-tf`).value;
        const kMode = $(`${prefix}-kronos-mode`)?.value || "auto";
        const kPolicy = $(`${prefix}-kronos-policy`)?.value || "";
        const wl = (state.mm.overview?.watchlists?.[mkt] || []).map((w) => w.symbol);
        const startBody = { market: mkt, strategy, timeframe, symbols: wl };
        if (kMode === "enabled") startBody.kronos_enabled = true;
        else if (kMode === "disabled") startBody.kronos_enabled = false;
        if (kPolicy) startBody.kronos_failure_policy = kPolicy;
        const snap = await api("/api/markets/paper/session/start", { body: startBody });
        await loadMultiMarketOverview();
        const kLc = snap?.kronos_session?.lifecycle_state || "NOT_CONFIGURED";
        const cov = formatCoverageSummary(snap?.initial_analysis || snap?.last_analysis);
        toast(`${mkt} Paper Session Started`, `Strategy=${strategy} (${timeframe})${cov ? ` · ${cov}` : ""} · Kronos=${kLc} · 0 broker orders.`, "ok");
      } catch (err) { toast(`${mkt} start blocked`, err.message, "err"); }
    });
    $(`${prefix}-analyze-btn`)?.addEventListener("click", () => analyzeMarketsConcurrently([mkt]));
    for (const act of ["pause", "resume", "stop"]) {
      $(`${prefix}-${act}-btn`)?.addEventListener("click", async () => {
        try {
          const snap = await api("/api/markets/paper/session/transition", { body: { market: mkt, action: act, reason: `operator ${act}` } });
          await loadMultiMarketOverview();
          const cov = act === "resume" ? formatCoverageSummary(snap?.initial_analysis || snap?.last_analysis) : "";
          toast(`${mkt} Paper ${act.toUpperCase()}`, `State=${snap?.session_state || act.toUpperCase()}${cov ? ` · ${cov}` : ""} · 0 broker orders.`, "ok");
        } catch (err) { toast(`${mkt} ${act} failed`, err.message, "err"); }
      });
    }
    $(`${prefix}-reset-btn`)?.addEventListener("click", () => {
      openDialog({
        title: `Reset ${mkt} Simulated Paper Ledger`,
        body: `<p>This resets ${mkt} local paper cash, positions, and orders back to starting capital while preserving the tamper-evident SHA-256 audit log.</p>`,
        confirmLabel: `Reset ${mkt} Paper Account`,
        onConfirm: async () => {
          await api("/api/markets/paper/reset", { body: { market: mkt, confirm: true } });
          await loadMultiMarketOverview();
          toast(`${mkt} Paper Ledger Reset`, "Starting capital restored.", "ok");
        },
      });
    });
  };
  bindPaperLifecycle("NSE", "mm-nse");
  bindPaperLifecycle("NASDAQ", "mm-nasdaq");

  // Kronos PAPER Contribution & Failure Policy Form
  $("mm-kronos-policy-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const feedback = $("mm-kp-policy-feedback");
    try {
      const body = {
        paper_session_enabled: $("mm-kp-session-enabled")?.value === "true",
        failure_policy: $("mm-kp-failure-policy")?.value || "FAIL_CLOSED_NO_ENTRY",
        conflict_policy: $("mm-kp-conflict-policy")?.value || "VETO_CONFLICTING_ENTRY",
        min_confidence: Number($("mm-kp-min-confidence")?.value || 0.55),
        max_uncertainty_pct: Number($("mm-kp-max-uncertainty")?.value || 8.0),
        weight_cap: Number($("mm-kp-weight-cap")?.value || 0.20),
      };
      const updated = await api("/api/model-lab/kronos/paper-policy", { body });
      if (state.mm.overview) state.mm.overview.kronos = updated;
      if (state.ml.overview) state.ml.overview.kronos = updated;
      renderMultiMarketDesk();
      renderModelLab();
      if (feedback) {
        feedback.className = "inline-status top-space ok";
        feedback.textContent = `Saved Kronos PAPER policy: failure_policy=${updated.config?.failure_policy} · conflict_policy=${updated.config?.conflict_policy} · min_conf=${updated.config?.min_confidence} · max_band=${updated.config?.max_uncertainty_pct}% · weight_cap=${updated.config?.weight_cap}. Live execution remains OFF.`;
      }
      toast("Kronos PAPER policy saved", `Failure policy=${updated.config?.failure_policy} · LIVE=OFF.`, "ok");
    } catch (err) {
      if (feedback) {
        feedback.className = "inline-status top-space err";
        feedback.textContent = `Kronos policy update rejected: ${err.message}`;
      }
      toast("Kronos policy update failed", err.message, "err");
    }
  });

  // Per-Symbol PAPER Decision Inspector Filters & Row Clicks
  $("mm-refresh-decisions-btn")?.addEventListener("click", loadPaperDecisions);
  $("mm-decision-market-filter")?.addEventListener("change", () => { renderMultiMarketDesk(); loadPaperDecisions(); });
  $("mm-decision-symbol-filter")?.addEventListener("input", () => renderMultiMarketDesk());
  $("mm-decision-inspector-panel")?.addEventListener("click", (e) => {
    const row = e.target.closest("[data-inspect-decision]");
    if (!row) return;
    state.mm.selectedDecisionId = row.dataset.inspectDecision || "";
    renderMultiMarketDesk();
  });

  // Correlated Structured Paper & Kronos Log Filters
  $("log-filter-form")?.addEventListener("submit", (e) => {
    e.preventDefault();
    loadStructuredPaperLogs();
  });
  for (const id of ["log-filter-window", "log-filter-event-type", "log-filter-severity"]) {
    $(id)?.addEventListener("change", () => loadStructuredPaperLogs());
  }
  $("log-reset-filters-btn")?.addEventListener("click", () => {
    if ($("log-filter-window")) $("log-filter-window").value = "all";
    if ($("log-filter-symbol")) $("log-filter-symbol").value = "";
    if ($("log-filter-event-type")) $("log-filter-event-type").value = "";
    if ($("log-filter-severity")) $("log-filter-severity").value = "";
    if ($("log-filter-strategy")) $("log-filter-strategy").value = "";
    if ($("log-filter-session")) $("log-filter-session").value = "";
    if ($("log-filter-query")) $("log-filter-query").value = "";
    loadStructuredPaperLogs();
  });

  // Alpaca Broker Mode & Gate Controls
  $("mm-alpaca-enable-paper-btn")?.addEventListener("click", async () => {
    try {
      const st = await api("/api/alpaca-broker/mode", { body: { enabled: true, environment: "paper" } });
      $("mm-alpaca-gate-output").textContent = `Alpaca Broker configured in PAPER mode (${st.endpoint_host}). Real money=false.`;
      await loadBrokerDestination(); await loadMultiMarketOverview();
    } catch (err) { toast("Alpaca mode blocked", err.message, "err"); }
  });
  $("mm-alpaca-readiness-btn")?.addEventListener("click", async () => {
    try {
      const r = await api("/api/alpaca-broker/readiness?symbol=AAPL");
      $("mm-alpaca-gate-output").className = `inline-status top-space ${r.all_passed ? "ok" : "warn"}`;
      $("mm-alpaca-gate-output").textContent = r.all_passed
        ? `All readiness checks passed (${Object.keys(r.checks).join(", ")}). Ready to Arm.`
        : `Readiness blockers: ${r.failed_checks.join(", ")}.`;
    } catch (err) { $("mm-alpaca-gate-output").className = "inline-status top-space err"; $("mm-alpaca-gate-output").textContent = err.message; }
  });
  $("mm-alpaca-arm-btn")?.addEventListener("click", async () => {
    try {
      const res = await api("/api/alpaca-broker/gate/arm?symbol=AAPL", { body: {} });
      $("mm-alpaca-gate-output").className = "inline-status top-space warn";
      $("mm-alpaca-gate-output").textContent = `Alpaca Gate ARMED (${res.gate.state}). Separate phrase confirmation required for LIVE Activate.`;
      await loadBrokerDestination(); await loadMultiMarketOverview();
    } catch (err) { toast("Alpaca Arm refused", err.message, "err"); }
  });
  $("mm-alpaca-activate-btn")?.addEventListener("click", () => {
    openDialog({
      title: "Activate ALPACA_LIVE Real-Money Execution",
      body: "<p><strong>REAL-MONEY WARNING:</strong> Activating ALPACA_LIVE binds the authenticated write client to <code>api.alpaca.markets</code> under your bounded risk limits.</p>",
      phrase: "I UNDERSTAND THIS WILL PLACE REAL-MONEY LIVE ORDERS",
      confirmLabel: "Activate LIVE Execution",
      onConfirm: async () => {
        const res = await api("/api/alpaca-broker/gate/activate", {
          body: { confirm_live_trading: "I UNDERSTAND THIS WILL PLACE REAL-MONEY LIVE ORDERS", acknowledge_risk: true, symbol: "AAPL" },
        });
        $("mm-alpaca-gate-output").className = "inline-status top-space err";
        $("mm-alpaca-gate-output").textContent = `ALPACA_LIVE ACTIVE (${res.gate.state}). Prominent Emergency Stop remains armed.`;
        await loadBrokerDestination(); await loadMultiMarketOverview();
      },
    });
  });
  $("mm-alpaca-disarm-btn")?.addEventListener("click", async () => {
    try {
      await api("/api/alpaca-broker/gate/disarm", { body: { reason: "operator disarm" } });
      $("mm-alpaca-gate-output").className = "inline-status top-space";
      $("mm-alpaca-gate-output").textContent = "Alpaca Gate DISARMED. Write client unbound.";
      await loadBrokerDestination(); await loadMultiMarketOverview();
    } catch (err) { toast("Disarm notice", err.message, "warn"); }
  });
  $("mm-alpaca-recover-btn")?.addEventListener("click", async () => {
    try {
      const rec = await api("/api/alpaca-broker/recover", { body: {} });
      toast("Alpaca journal recovered", `Resolved intents: ${rec.resolved_intents || 0}`, "ok");
    } catch (err) { toast("Recover blocked", err.message, "err"); }
  });

  // Risk Forms
  $("mm-nse-risk-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api("/api/markets/paper/config", {
        body: {
          market: "NSE",
          max_order_value: Number($("mm-nse-max-order").value),
          max_daily_loss: Number($("mm-nse-max-loss").value),
          max_open_positions: Number($("mm-nse-max-pos").value),
          slippage_bps: Number($("mm-nse-slippage").value),
        },
      });
      await loadMultiMarketOverview();
      toast("NSE paper risk bounds saved", "INR caps and simulated slippage updated.", "ok");
    } catch (err) { toast("NSE risk rejected", err.message, "err"); }
  });
  $("mm-nasdaq-risk-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api("/api/markets/paper/config", {
        body: {
          market: "NASDAQ",
          max_order_value: Number($("mm-us-max-order").value),
          max_daily_loss: Number($("mm-us-max-loss").value),
          max_open_positions: Number($("mm-us-max-pos").value),
          max_daily_orders: Number($("mm-us-max-orders").value),
        },
      });
      await loadMultiMarketOverview();
      toast("NASDAQ paper risk bounds saved", "USD paper caps updated.", "ok");
    } catch (err) { toast("NASDAQ risk rejected", err.message, "err"); }
  });
  $("mm-save-alpaca-broker-risk")?.addEventListener("click", async () => {
    try {
      await api("/api/alpaca-broker/limits", {
        body: {
          max_order_value: Number($("mm-us-max-order").value),
          max_daily_loss: Number($("mm-us-max-loss").value),
          max_open_positions: Number($("mm-us-max-pos").value),
          max_daily_orders: Number($("mm-us-max-orders").value),
        },
      });
      await loadBrokerDestination();
      toast("Alpaca Broker hard limits saved", "Prior Arm/Activate invalidated.", "ok");
    } catch (err) { toast("Broker limits rejected", err.message, "err"); }
  });

  // Provider Connections (Upstox Data, Alpaca Data, Alpaca Broker, Upstox Live)
  $("mm-upstox-data-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const access_token = $("mm-upstox-token-input").value.trim();
    $("mm-upstox-token-input").value = "";
    if (!access_token) return;
    try {
      await api("/api/autonomy/upstox-token", { body: { access_token } });
      await loadMultiMarketOverview();
      $("mm-upstox-verify-result").className = "inline-status top-space ok";
      $("mm-upstox-verify-result").textContent = "Upstox read-only token encrypted in protected store. Click Verify NSE quote to test.";
      toast("Upstox token saved", "Encrypted in local protected store; never echoed.", "ok");
    } catch (err) { toast("Upstox token save failed", err.message, "err"); }
  });
  const runUpstoxVerify = async () => {
    if ($("mm-upstox-data-pill")) $("mm-upstox-data-pill").textContent = "AUTHENTICATING";
    try {
      const v = await api("/api/connections/upstox/verify-quote", { body: { instrument_key: "NSE_EQ|INE002A01018" } });
      if ($("mm-upstox-data-pill")) {
        $("mm-upstox-data-pill").textContent = v.connection_state;
        $("mm-upstox-data-pill").className = `status-pill ${connStatePillClass(v.connection_state)}`;
      }
      if ($("mm-upstox-verify-result")) {
        $("mm-upstox-verify-result").className = `inline-status top-space ${v.ok ? "ok" : "err"}`;
        $("mm-upstox-verify-result").textContent = `${v.connection_state}: ${v.guidance || ""} ${v.quote?.last ? `(RELIANCE ₹${v.quote.last})` : ""}`;
      }
      await loadMultiMarketOverview();
      toast("NSE quote probe completed", `${v.connection_state} — zero order endpoints called.`, v.authenticated ? "ok" : "warn");
    } catch (err) {
      if ($("mm-upstox-verify-result")) $("mm-upstox-verify-result").textContent = err.message;
      toast("NSE verify failed", err.message, "err");
    }
  };
  $("mm-upstox-verify-btn")?.addEventListener("click", runUpstoxVerify);
  $("mm-ready-verify-nse")?.addEventListener("click", runUpstoxVerify);
  $("mm-upstox-delete-btn")?.addEventListener("click", async () => {
    try {
      await api("/api/autonomy/upstox-token", { method: "DELETE" });
      await loadMultiMarketOverview();
      toast("Upstox token deleted", "Read-only Upstox token removed.", "ok");
    } catch (err) { toast("Delete failed", err.message, "err"); }
  });

  $("mm-alpaca-data-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const api_key = $("mm-alpaca-data-key").value.trim();
    const api_secret = $("mm-alpaca-data-secret").value.trim();
    const feed = $("mm-alpaca-data-feed").value;
    $("mm-alpaca-data-key").value = "";
    $("mm-alpaca-data-secret").value = "";
    try {
      await api("/api/connections/alpaca/credentials", { body: { api_key, api_secret, feed } });
      await loadMultiMarketOverview();
      $("mm-alpaca-verify-result").className = "inline-status top-space ok";
      $("mm-alpaca-verify-result").textContent = `Alpaca Market Data keys saved (${feed.toUpperCase()}). Click Verify NASDAQ quote to test.`;
      toast("Alpaca data keys saved", "Encrypted in ALPACA_MARKET_DATA slot; secrets cleared from DOM.", "ok");
    } catch (err) { toast("Alpaca save failed", err.message, "err"); }
  });
  const runAlpacaVerify = async () => {
    if ($("mm-alpaca-data-pill")) $("mm-alpaca-data-pill").textContent = "AUTHENTICATING";
    try {
      const feed = $("mm-alpaca-data-feed")?.value || "iex";
      const v = await api("/api/connections/alpaca/verify-quote", { body: { symbol: "AAPL", feed } });
      if ($("mm-alpaca-data-pill")) {
        $("mm-alpaca-data-pill").textContent = v.connection_state;
        $("mm-alpaca-data-pill").className = `status-pill ${connStatePillClass(v.connection_state)}`;
      }
      if ($("mm-alpaca-verify-result")) {
        $("mm-alpaca-verify-result").className = `inline-status top-space ${v.ok ? "ok" : "err"}`;
        $("mm-alpaca-verify-result").textContent = `${v.connection_state}: ${v.guidance || ""}`;
      }
      await loadMultiMarketOverview();
      toast("NASDAQ quote probe completed", `${v.connection_state} — zero order endpoints called.`, v.authenticated ? "ok" : "warn");
    } catch (err) {
      if ($("mm-alpaca-verify-result")) $("mm-alpaca-verify-result").textContent = err.message;
      toast("NASDAQ verify failed", err.message, "err");
    }
  };
  $("mm-alpaca-data-verify-btn")?.addEventListener("click", runAlpacaVerify);
  $("mm-ready-verify-nasdaq")?.addEventListener("click", runAlpacaVerify);
  $("mm-alpaca-data-delete-btn")?.addEventListener("click", async () => {
    try {
      await api("/api/connections/alpaca/credentials", { method: "DELETE" });
      await loadMultiMarketOverview();
      toast("Alpaca data keys deleted", "Removed from protected store.", "ok");
    } catch (err) { toast("Delete failed", err.message, "err"); }
  });

  $("mm-alpaca-broker-cred-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const env = $("mm-alpaca-broker-env").value;
    const api_key = $("mm-alpaca-broker-key").value.trim();
    const api_secret = $("mm-alpaca-broker-secret").value.trim();
    $("mm-alpaca-broker-key").value = "";
    $("mm-alpaca-broker-secret").value = "";
    try {
      await api(`/api/alpaca-broker/credentials/${encodeURIComponent(env)}`, { body: { api_key, api_secret } });
      await loadMultiMarketOverview();
      $("mm-alpaca-broker-test-result").className = "inline-status ok";
      $("mm-alpaca-broker-test-result").textContent = `Saved ALPACA_BROKER_${env.toUpperCase()} keys. Gate remains DISARMED.`;
      toast("Broker slot saved", `ALPACA_BROKER_${env.toUpperCase()} saved; gate remains DISARMED.`, "ok");
    } catch (err) { toast("Broker credential save failed", err.message, "err"); }
  });
  $("mm-alpaca-broker-test-btn")?.addEventListener("click", async () => {
    const env = $("mm-alpaca-broker-env").value;
    try {
      const res = await api(`/api/alpaca-broker/test?environment=${encodeURIComponent(env)}`, { body: {} });
      $("mm-alpaca-broker-test-result").className = `inline-status ${res.ok ? "ok" : "err"}`;
      $("mm-alpaca-broker-test-result").textContent = `${res.connection_state}: ${res.guidance || ""}`;
    } catch (err) { $("mm-alpaca-broker-test-result").textContent = err.message; }
  });
  $("mm-alpaca-broker-del-btn")?.addEventListener("click", async () => {
    const env = $("mm-alpaca-broker-env").value;
    try {
      await api(`/api/alpaca-broker/credentials/${encodeURIComponent(env)}`, { method: "DELETE" });
      await loadMultiMarketOverview();
      toast("Broker slot deleted", `ALPACA_BROKER_${env.toUpperCase()} removed.`, "ok");
    } catch (err) { toast("Delete failed", err.message, "err"); }
  });

  $("mm-upstox-live-cred-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const access_token = $("mm-upstox-live-input").value.trim();
    $("mm-upstox-live-input").value = "";
    try {
      await api("/api/autonomy/upstox-live-token", { body: { access_token } });
      await loadMultiMarketOverview();
      $("mm-upstox-live-result").className = "inline-status ok";
      $("mm-upstox-live-result").textContent = "Saved separate UPSTOX_LIVE_ACCESS_TOKEN. Live trading remains OFF.";
      toast("Upstox LIVE token saved", "Stored separately; live trading remains OFF.", "ok");
    } catch (err) { toast("Upstox LIVE save failed", err.message, "err"); }
  });
  $("mm-upstox-live-del-btn")?.addEventListener("click", async () => {
    try {
      await api("/api/autonomy/upstox-live-token", { method: "DELETE" });
      await loadMultiMarketOverview();
      toast("Upstox LIVE token deleted", "Removed from protected store.", "ok");
    } catch (err) { toast("Delete failed", err.message, "err"); }
  });
}

function qualificationPillClass(st) {
  if (st === "QUALIFIED") return "ok";
  if (st === "UNVERIFIED") return "warn";
  if (st === "BLOCKED") return "err";
  return "info";
}

function kronosPillClass(st) {
  if (st === "READY") return "ok";
  if (st === "RUNNING") return "info";
  if (st === "NOT_VERIFIED" || st === "STALE") return "warn";
  return "err";
}

async function loadModelLabOverview() {
  try {
    const ov = await api("/api/model-lab/overview", { keepAuth: true });
    state.ml.available = true;
    state.ml.overview = ov;
    if (!state.ml.selectedRunId && Array.isArray(ov.runs) && ov.runs.length) {
      state.ml.selectedRunId = ov.runs[0].run_id;
    }
    renderModelLab();
  } catch (_) {
    state.ml.available = false;
  }
}

function renderModelLab() {
  const ov = state.ml.overview;
  if (!ov) return;
  const k = ov.kronos || {};
  const pState = k.provider_state || "NOT_INSTALLED";
  const kEngine = k.kronos_engine || {};
  const diag = k.diagnostics || {};
  const cfg = k.config || {};

  if ($("mm-pill-kronos-state")) {
    $("mm-pill-kronos-state").textContent = `KRONOS · ${pState}`;
    $("mm-pill-kronos-state").className = `status-pill ${kronosPillClass(pState)}`;
  }
  if ($("ml-kronos-state-pill")) {
    $("ml-kronos-state-pill").textContent = pState;
    $("ml-kronos-state-pill").className = `status-pill ${kronosPillClass(pState)}`;
  }
  if ($("ml-kronos-runtime-badge")) {
    $("ml-kronos-runtime-badge").textContent = `${pState} · ${kEngine.inference_runtime || "MISSING"}`;
    $("ml-kronos-runtime-badge").className = `status-pill ${kronosPillClass(pState)}`;
  }
  if ($("ml-kronos-summary")) {
    $("ml-kronos-summary").className = `inline-status ${kronosPillClass(pState)}`;
    $("ml-kronos-summary").textContent = k.summary || "Kronos status loaded.";
  }
  if ($("ml-kronos-toggle-pill")) {
    $("ml-kronos-toggle-pill").textContent = k.inference_enabled ? "FORECASTING ENABLED" : "FORECASTING DISABLED";
    $("ml-kronos-toggle-pill").className = `status-pill ${k.inference_enabled ? "ok" : "info"}`;
  }
  renderKronosTruthMatrix("ml-kronos-truth-grid", k);
  renderKronosPaperSection(k, state.mm.overview?.paper?.NSE || {}, state.mm.overview?.paper?.NASDAQ || {});

  if ($("ml-kronos-details")) {
    const plan = k.install_plan || {};
    const rows = [
      ["Provider State", pState],
      ["PAPER Lifecycle", `${k.lifecycle_state || "NOT_CONFIGURED"} (${cfg.failure_policy || "FAIL_CLOSED_NO_ENTRY"})`],
      ["Upstream Source", `${plan.upstream?.repository || "shiyu-coder/Kronos"} @ ${String(plan.upstream?.commit || "67b630e67f6a").slice(0, 12)}`],
      ["License", k.license || "MIT License"],
      ["Model Pin", `NeoQuasar/Kronos-small @ ${String(kEngine.model_revision || plan.model?.revision || "").slice(0, 12)}`],
      ["Tokenizer Pin", `NeoQuasar/Kronos-Tokenizer-base @ ${String(kEngine.tokenizer_revision || plan.tokenizer?.revision || "").slice(0, 12)}`],
      ["Manifest SHA-256", `${String(kEngine.manifest_sha256 || plan.manifest_sha256 || "—").slice(0, 20)}…`],
      ["Weights Verified", String(Boolean(kEngine.artifact_verified))],
      ["Self-Test Verdict", kEngine.last_self_test?.status || "NOT_VERIFIED"],
      ["Produced / Consumed", `${diag.forecast_produced_count ?? 0} produced / ${diag.forecast_consumed_count ?? 0} consumed in PAPER`],
      ["Trading Effect", k.execution_authorization_effect || "NONE"],
    ];
    $("ml-kronos-details").innerHTML = rows.map(([dt, dd]) => `<dt>${escapeHtml(dt)}</dt><dd>${escapeHtml(dd)}</dd>`).join("");
  }

  if ($("ml-cfg-cadence") && cfg.cadence_seconds) $("ml-cfg-cadence").value = String(cfg.cadence_seconds);
  if ($("ml-cfg-timeout") && cfg.timeout_seconds) $("ml-cfg-timeout").value = String(cfg.timeout_seconds);
  if ($("ml-cfg-horizon") && cfg.horizon) $("ml-cfg-horizon").value = String(cfg.horizon);
  if ($("ml-cfg-samples") && cfg.samples) $("ml-cfg-samples").value = String(cfg.samples);

  if ($("ml-kronos-diagnostics")) {
    const dRows = [
      ["Model Loaded", k.model_loaded ? "YES (CPU Ready)" : "NO (Unloaded / Lazy)"],
      ["Load Latency p50 / p95", `${diag.load_ms_p50 != null ? `${diag.load_ms_p50} ms` : "not measured"} / ${diag.load_ms_p95 != null ? `${diag.load_ms_p95} ms` : "not measured"}`],
      ["Inference p50 / p95", `${diag.inference_ms_p50 != null ? `${diag.inference_ms_p50} ms` : "not measured"} / ${diag.inference_ms_p95 != null ? `${diag.inference_ms_p95} ms` : "not measured"}`],
      ["Measured Forecasts", String(diag.inference_count ?? 0)],
      ["Process RSS / Weights Disk", `${diag.process_rss_mb != null ? `${diag.process_rss_mb} MiB` : "n/a"} / ~${diag.expected_weights_mb || 109.5} MiB`],
      ["Device & Mock Policy", `${diag.device || "cpu"} · mock_outputs_used=${String(Boolean(diag.mock_outputs_used))}`],
    ];
    $("ml-kronos-diagnostics").innerHTML = dRows.map(([dt, dd]) => `<dt>${escapeHtml(dt)}</dt><dd>${escapeHtml(dd)}</dd>`).join("");
  }

  // Datasets
  const datasets = ov.datasets || [];
  if ($("ml-datasets-count")) $("ml-datasets-count").textContent = `${datasets.length} DATASETS`;
  if ($("ml-datasets-rows")) {
    $("ml-datasets-rows").innerHTML = datasets.length ? datasets.map((d) => `
      <tr>
        <td><code>${escapeHtml(d.dataset_id)}</code></td>
        <td><strong>${escapeHtml(d.market)}</strong> · ${escapeHtml(d.symbol)}</td>
        <td>${escapeHtml(d.timeframe)}</td>
        <td>${d.bar_count}</td>
        <td><small>${escapeHtml(String(d.start_timestamp || "").slice(0, 10))} → ${escapeHtml(String(d.end_timestamp || "").slice(0, 10))}</small></td>
        <td>${d.estimated_missing_bars ?? 0}</td>
        <td>${d.real_market_data ? statusPill("IMPORTED / CAPTURED", "ok") : statusPill("SYNTHETIC FIXTURE", "warn")}</td>
        <td>${corporateActionCell(d)}</td>
        <td><code>${escapeHtml(String(d.sha256 || "").slice(0, 14))}…</code></td>
      </tr>
    `).join("") : `<tr><td colspan="9" class="empty-state">No datasets registered.</td></tr>`;
  }

  if ($("ml-run-dataset")) {
    const prev = $("ml-run-dataset").value;
    $("ml-run-dataset").innerHTML = datasets.map((d) =>
      `<option value="${escapeHtml(d.dataset_id)}">${escapeHtml(`${d.dataset_id} (${d.market} · ${d.symbol} · ${d.bar_count} bars)`)}</option>`
    ).join("");
    if (prev && datasets.some((d) => d.dataset_id === prev)) $("ml-run-dataset").value = prev;
  }
  if ($("ml-fc-dataset")) {
    const prevFc = $("ml-fc-dataset").value;
    $("ml-fc-dataset").innerHTML = `<option value="">Live Read-Only Provider Bars (No Mock Fallback)</option>` +
      datasets.map((d) => `<option value="${escapeHtml(d.dataset_id)}">${escapeHtml(`Dataset: ${d.dataset_id} (${d.market})`)}</option>`).join("");
    if (prevFc) $("ml-fc-dataset").value = prevFc;
  }

  // Research Runs
  const runs = ov.runs || [];
  const activeRun = runs.find((r) => r.status === "RUNNING" || r.status === "QUEUED");
  if ($("ml-run-active-pill")) {
    $("ml-run-active-pill").textContent = activeRun ? `${activeRun.status} (${activeRun.progress_pct}%)` : "IDLE";
    $("ml-run-active-pill").className = `status-pill ${activeRun ? "warn" : "info"}`;
  }
  if ($("ml-run-progress") && activeRun) {
    $("ml-run-progress").className = "inline-status top-space warn";
    $("ml-run-progress").textContent = `${activeRun.run_id}: [${activeRun.phase} · ${activeRun.progress_pct}%] ${activeRun.status_detail}`;
  }
  if ($("ml-runs-rows")) {
    $("ml-runs-rows").innerHTML = runs.length ? runs.map((r) => `
      <tr>
        <td><code>${escapeHtml(r.run_id)}</code></td>
        <td>${escapeHtml(r.market)} · ${escapeHtml(r.timeframe)}</td>
        <td><code>${escapeHtml(r.dataset_id)}</code></td>
        <td>${statusPill(r.status, r.status === "COMPLETED" ? "ok" : r.status === "RUNNING" ? "warn" : "err")}</td>
        <td>${(r.qualification && r.qualification.status)
          ? `<span title="${escapeHtml((r.qualification.blocked || []).concat(r.qualification.unverified || []).join(", "))}">${statusPill(r.qualification.status, qualificationPillClass(r.qualification.status))}</span>`
          : statusPill("UNVERIFIED", "warn")}</td>
        <td>${r.progress_pct}% · <small>${escapeHtml(r.phase)}</small></td>
        <td><code>${escapeHtml(String(r.manifest_sha256 || "—").slice(0, 14))}</code></td>
        <td><button type="button" class="button secondary xs" data-ml-inspect-run="${escapeHtml(r.run_id)}">Inspect</button></td>
      </tr>
    `).join("") : `<tr><td colspan="7" class="empty-state">No research runs recorded yet. Click 'Run Research (Explicit Start)' above.</td></tr>`;
  }

  const chosenRun = runs.find((r) => r.run_id === state.ml.selectedRunId) || runs.find((r) => r.manifest) || null;
  renderModelLabSelectedRun(chosenRun);

  // Model Registry
  const models = ov.models || [];
  if ($("ml-models-count")) $("ml-models-count").textContent = `${models.length} MODELS`;
  if ($("ml-models-rows")) {
    $("ml-models-rows").innerHTML = models.length ? models.map((m) => {
      const testRet = m.metrics_summary?.untouched_test?.net_return_pct;
      const stressRet = m.metrics_summary?.stressed_test?.net_return_pct;
      const stCls = m.registry_status === "PROMOTED_PAPER_CANDIDATE" || m.registry_status === "VALIDATED_CANDIDATE"
        ? "ok"
        : m.registry_status === "IMPORTED_PAPER_CANDIDATE"
          ? "info"
          : "err";
      return `<tr>
        <td><code>${escapeHtml(m.model_id)}</code></td>
        <td><strong>${escapeHtml(m.candidate_id)}</strong><br><small class="muted">${escapeHtml(m.family)}</small></td>
        <td>${escapeHtml(m.market)} · ${escapeHtml(m.timeframe)}</td>
        <td>${statusPill(m.registry_status, stCls)}${
          m.qualification_status
            ? `<br><small class="muted">QUALIFICATION: ${escapeHtml(m.qualification_status)}</small>`
            : ""
        }</td>
        <td class="${Number(testRet) >= 0 ? "pnl-pos" : "pnl-neg"}">${testRet != null ? `${fmt(testRet, 2)}%` : "—"}</td>
        <td class="${Number(stressRet) >= 0 ? "pnl-pos" : "pnl-neg"}">${stressRet != null ? `${fmt(stressRet, 2)}%` : "—"}</td>
        <td><code>${escapeHtml(String(m.artifact_sha256 || "").slice(0, 12))}…</code></td>
        <td>
          <div class="button-row">
            ${m.promotion_eligible ? `<button type="button" class="button bull xs" data-ml-promote="${escapeHtml(m.model_id)}">Promote (Paper)</button>` : ""}
            <button type="button" class="button danger-outline xs" data-ml-reject="${escapeHtml(m.model_id)}">Reject</button>
            <button type="button" class="button secondary xs" data-ml-export="${escapeHtml(m.model_id)}">Export JSON</button>
          </div>
        </td>
      </tr>`;
    }).join("") : `<tr><td colspan="8" class="empty-state">No registered candidate models yet.</td></tr>`;
  }
}

function renderModelLabQualification(run) {
  const q = (run && run.qualification) || null;
  const status = (q && q.status) || "UNVERIFIED";
  const counts = (q && q.counts) || {};
  if ($("ml-qualification-summary")) {
    const el = $("ml-qualification-summary");
    el.className = `inline-status top-space ${qualificationPillClass(status)}`;
    if (!q || !q.record_sha256) {
      el.textContent =
        "Research qualification: no record yet. A verdict is written when a run completes; it never arms a strategy or enables LIVE trading.";
    } else {
      el.textContent =
        `Research qualification: ${status} · ${counts.PASS ?? 0} passed / ${counts.FAIL ?? 0} blocked / ` +
        `${counts.UNVERIFIED ?? 0} unverified · level ${q.verification_level || "NONE"} · ` +
        `record ${String(q.record_sha256).slice(0, 16)}… · execution authority NONE`;
    }
  }
  if ($("ml-qualification-details")) {
    if (!q || !q.record_sha256) {
      $("ml-qualification-details").innerHTML = "";
      return;
    }
    const blocked = (q.blocked || []).join(", ") || "none";
    const unverified = (q.unverified || []).join(", ") || "none";
    const rows = [
      ["Verdict", status],
      ["Verification Level", q.verification_level || "NONE"],
      ["Blocked Reasons", blocked],
      ["Unverified Reasons", unverified],
      ["Record SHA-256", String(q.record_sha256)],
      ["Manifest SHA-256", String(q.manifest_sha256 || "—")],
      ["Reported Candidate", String(q.candidate_id || "—")],
      ["Execution Authority", "NONE — qualification never arms, sizes or executes"],
    ];
    $("ml-qualification-details").innerHTML = rows
      .map(([dt, dd]) => `<dt>${escapeHtml(dt)}</dt><dd>${escapeHtml(dd)}</dd>`)
      .join("");
  }
}

function renderModelLabSelectedRun(run) {
  if (!run || !run.manifest) {
    if ($("ml-comp-run-label")) $("ml-comp-run-label").textContent = "NO COMPLETED RUN";
    return;
  }
  const mf = run.manifest;
  const sp = mf.splits || {};
  if ($("ml-comp-run-label")) $("ml-comp-run-label").textContent = `${run.run_id} · ${mf.market}`;
  if ($("ml-split-summary")) {
    $("ml-split-summary").className = "inline-status ok";
    $("ml-split-summary").textContent =
      `Dataset ${mf.dataset?.dataset_id} (${mf.dataset?.real_market_data ? "Real Historical" : "Bundled Synthetic Fixture"}) · ` +
      `Train [${sp.train?.bars} bars: ${String(sp.train?.start_ts || "").slice(0, 10)}→${String(sp.train?.end_ts || "").slice(0, 10)}] → ` +
      `Embargo(${sp.embargo_1_bars}) → Val [${sp.validation?.bars} bars] → Embargo(${sp.embargo_2_bars}) → ` +
      `Untouched Test [${sp.untouched_test?.bars} bars, selection_touched_test=${String(sp.untouched_test?.selection_touched_test)}] · ` +
      `Manifest SHA-256: ${String(run.manifest_sha256 || "").slice(0, 16)}…`;
  }
  renderModelLabQualification(run);
  if ($("ml-candidates-rows")) {
    $("ml-candidates-rows").innerHTML = (mf.candidates || []).map((c) => {
      if (!c.untouched_test) {
        return `<tr>
          <td><strong>${escapeHtml(c.candidate_id)}</strong><br><small class="muted">${escapeHtml(c.family)}</small></td>
          <td colspan="9"><span class="muted">${escapeHtml(c.skip_reason || c.status || "Skipped")}</span></td>
          <td>${statusPill(c.status || "SKIPPED", "warn")}</td>
        </tr>`;
      }
      const tr = c.train || {};
      const va = c.validation || {};
      const te = c.untouched_test || {};
      const st = c.stressed_test || {};
      const ci = te.uncertainty || {};
      const rg = te.regime_breakdown || {};
      return `<tr>
        <td><strong>${escapeHtml(c.candidate_id)}</strong><br><small class="muted">${escapeHtml(c.family)}</small></td>
        <td>${fmt(tr.sharpe, 2)}</td>
        <td>${fmt(va.sharpe, 2)}</td>
        <td class="${Number(te.net_return_pct) >= 0 ? "pnl-pos" : "pnl-neg"}"><strong>${fmt(te.net_return_pct, 2)}%</strong><br><small class="muted">[${fmt(ci.net_return_ci90_low_pct, 1)}%, ${fmt(ci.net_return_ci90_high_pct, 1)}%]</small></td>
        <td>${fmt(te.sharpe, 2)} / ${fmt(te.sortino, 2)}</td>
        <td>${fmt(te.max_drawdown_pct, 2)}%</td>
        <td>${fmt(te.profit_factor, 2)} / ${fmt(te.win_rate_pct, 0)}%</td>
        <td>${te.trade_count} / ${fmt(te.turnover_x, 2)}×</td>
        <td class="${Number(st.net_return_pct) >= 0 ? "pnl-pos" : "pnl-neg"}">${fmt(st.net_return_pct, 2)}%</td>
        <td><small>T:${fmt(rg.TREND?.return_pct || 0, 1)}% R:${fmt(rg.RANGE?.return_pct || 0, 1)}% V:${fmt(rg.VOLATILE?.return_pct || 0, 1)}%</small></td>
        <td>${c.promotion_eligible ? statusPill("ELIGIBLE", "ok") : `<span title="${escapeHtml((c.rejection_reasons || []).join("; "))}">${statusPill("REJECTED", "err")}</span>`}</td>
      </tr>`;
    }).join("");
  }
}

function bindModelLabEvents() {
  $("ml-refresh-btn")?.addEventListener("click", loadModelLabOverview);

  const kronosAction = async (path, label) => {
    try {
      await api(path, { body: {} });
      await loadModelLabOverview();
      toast(label, "Kronos operation updated (forecasting-only; zero execution effect).", "ok");
    } catch (err) {
      await loadModelLabOverview();
      toast(`${label} failed`, err.message, "err");
    }
  };
  $("ml-kronos-install-btn")?.addEventListener("click", () => kronosAction("/api/autonomy/kronos/install", "Kronos install started"));
  $("ml-kronos-verify-btn")?.addEventListener("click", () => kronosAction("/api/autonomy/kronos/verify", "Kronos SHA-256 verification"));
  $("ml-kronos-selftest-btn")?.addEventListener("click", () => kronosAction("/api/autonomy/kronos/self-test", "Kronos offline CPU self-test"));
  $("ml-kronos-repair-btn")?.addEventListener("click", () => kronosAction("/api/autonomy/kronos/repair", "Kronos repair started"));
  $("ml-kronos-cancel-btn")?.addEventListener("click", () => kronosAction("/api/autonomy/kronos/cancel", "Kronos operation cancelled"));

  $("ml-kronos-enable-btn")?.addEventListener("click", async () => {
    try {
      await api("/api/model-lab/kronos/toggle", { body: { enabled: true } });
      await loadModelLabOverview();
      toast("Forecasting enabled", "Kronos forecasting enabled for Model Lab & paper advisory only.", "ok");
    } catch (err) { toast("Enable failed", err.message, "err"); }
  });
  $("ml-kronos-disable-btn")?.addEventListener("click", async () => {
    try {
      await api("/api/model-lab/kronos/toggle", { body: { enabled: false } });
      await loadModelLabOverview();
      toast("Forecasting disabled", "Kronos forecasting disabled.", "warn");
    } catch (err) { toast("Disable failed", err.message, "err"); }
  });
  $("ml-kronos-load-btn")?.addEventListener("click", () => kronosAction("/api/model-lab/kronos/load", "Kronos model load"));
  $("ml-kronos-unload-btn")?.addEventListener("click", () => kronosAction("/api/model-lab/kronos/unload", "Kronos worker unloaded"));
  $("ml-kronos-clear-err-btn")?.addEventListener("click", () => kronosAction("/api/model-lab/kronos/clear-error", "Kronos error state cleared"));

  $("ml-kronos-config-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api("/api/model-lab/kronos/config", {
        body: {
          cadence_seconds: Number($("ml-cfg-cadence").value || 300),
          timeout_seconds: Number($("ml-cfg-timeout").value || 45),
          horizon: Number($("ml-cfg-horizon").value || 5),
          samples: Number($("ml-cfg-samples").value || 8),
        },
      });
      await loadModelLabOverview();
      toast("Inference config saved", "Cadence, timeout, horizon, and MC samples updated.", "ok");
    } catch (err) { toast("Config rejected", err.message, "err"); }
  });

  $("ml-forecast-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    $("ml-fc-output").className = "inline-status top-space warn";
    $("ml-fc-output").textContent = "Running validated Kronos CPU inference (no mock fallback)…";
    try {
      const fc = await api("/api/model-lab/kronos/forecast", {
        body: {
          market: $("ml-fc-market").value,
          symbol: $("ml-fc-symbol").value.trim(),
          timeframe: $("ml-fc-tf").value,
          dataset_id: $("ml-fc-dataset").value || null,
        },
      });
      $("ml-fc-freshness-pill").textContent = fc.freshness_state || "FRESH";
      $("ml-fc-freshness-pill").className = "status-pill ok";
      $("ml-fc-output").className = "inline-status top-space ok";
      $("ml-fc-output").textContent =
        `[ADVISORY FORECAST ONLY — NOT A TRADE INSTRUCTION] ${fc.market}:${fc.symbol} (${fc.timeframe}, ${fc.context_bars_used} bars ending ${fc.input_end}) → ` +
        `Horizon ${fc.horizon_bars} bars: Mean ${fc.mean_close} (${fc.expected_return_pct >= 0 ? "+" : ""}${fc.expected_return_pct}%), ` +
        `q10..q90 [${fc.q10_close}, ${fc.q90_close}], P(up)=${fc.up_probability}, Latency=${fc.latency_ms?.inference_ms}ms, Manifest=${String(fc.model_identity?.manifest_sha256 || "").slice(0, 12)}…`;
      await loadModelLabOverview();
    } catch (err) {
      $("ml-fc-freshness-pill").textContent = "UNAVAILABLE";
      $("ml-fc-freshness-pill").className = "status-pill err";
      $("ml-fc-output").className = "inline-status top-space err";
      $("ml-fc-output").textContent = `UNAVAILABLE (Fail-Closed): ${err.message}`;
    }
  });

  $("ml-capture-ds-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const res = await api("/api/model-lab/datasets/capture-live", {
        body: {
          market: $("ml-cap-market").value,
          symbol: $("ml-cap-symbol").value.trim(),
          timeframe: $("ml-cap-tf").value,
        },
      });
      await loadModelLabOverview();
      toast("Dataset captured", `Registered ${res.dataset.dataset_id} (${res.dataset.bar_count} bars, SHA-256 ${res.dataset.sha256.slice(0, 12)}…).`, "ok");
    } catch (err) { toast("Capture blocked", err.message, "err"); }
  });

  $("ml-import-ds-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const res = await api("/api/model-lab/datasets/import", {
        body: {
          market: $("ml-imp-market").value,
          symbol: $("ml-imp-symbol").value.trim(),
          timeframe: $("ml-imp-tf").value,
          csv_text: $("ml-imp-csv").value,
        },
      });
      $("ml-imp-csv").value = "";
      await loadModelLabOverview();
      toast("CSV dataset imported", `Registered ${res.dataset.dataset_id} (${res.dataset.bar_count} bars).`, "ok");
    } catch (err) { toast("CSV import rejected", err.message, "err"); }
  });

  $("ml-run-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    $("ml-run-progress").className = "inline-status top-space warn";
    $("ml-run-progress").textContent = "Starting chronological walk-forward & untouched-test evaluation…";
    try {
      const run = await api("/api/model-lab/runs", {
        body: {
          dataset_id: $("ml-run-dataset").value,
          market: $("ml-run-market").value,
          train_frac: Number($("ml-run-train").value || 0.6),
          val_frac: Number($("ml-run-val").value || 0.2),
          embargo_bars: Number($("ml-run-embargo").value || 5),
          spread_bps: Number($("ml-run-spread").value || 4.0),
          slippage_bps: Number($("ml-run-slippage").value || 5.0),
          stress_multiplier: Number($("ml-run-stress").value || 2.0),
          min_trades: Number($("ml-run-mintrades").value || 5),
          include_kronos: $("ml-run-kronos").value === "true",
          async_execution: false,
        },
      });
      state.ml.selectedRunId = run.run_id;
      $("ml-run-progress").className = `inline-status top-space ${run.status === "COMPLETED" ? "ok" : "warn"}`;
      $("ml-run-progress").textContent = `${run.run_id}: ${run.status} — ${run.status_detail}`;
      await loadModelLabOverview();
      toast("Research run finished", `${run.run_id} (${run.status}) — zero execution changes.`, "ok");
    } catch (err) {
      $("ml-run-progress").className = "inline-status top-space err";
      $("ml-run-progress").textContent = `Research run failed: ${err.message}`;
      toast("Research run failed", err.message, "err");
    }
  });

  $("ml-run-cancel-btn")?.addEventListener("click", async () => {
    const runs = state.ml.overview?.runs || [];
    const active = runs.find((r) => r.status === "RUNNING" || r.status === "QUEUED") || runs[0];
    if (!active) { toast("No active run", "There is no running research job to cancel.", "info"); return; }
    try {
      await api(`/api/model-lab/runs/${encodeURIComponent(active.run_id)}/cancel`, { body: {} });
      await loadModelLabOverview();
      toast("Research run cancelled", `${active.run_id} marked CANCELLED.`, "warn");
    } catch (err) { toast("Cancel failed", err.message, "err"); }
  });

  $("ml-runs-rows")?.addEventListener("click", (e) => {
    const runId = e.target.closest("[data-ml-inspect-run]")?.dataset.mlInspectRun;
    if (!runId) return;
    state.ml.selectedRunId = runId;
    renderModelLab();
  });

  $("ml-models-rows")?.addEventListener("click", async (e) => {
    const promoteId = e.target.closest("[data-ml-promote]")?.dataset.mlPromote;
    const rejectId = e.target.closest("[data-ml-reject]")?.dataset.mlReject;
    const exportId = e.target.closest("[data-ml-export]")?.dataset.mlExport;
    try {
      if (promoteId) {
        await api(`/api/model-lab/models/${encodeURIComponent(promoteId)}/promote`, { body: { note: "Operator promoted for paper reference" } });
        await loadModelLabOverview();
        toast("Promoted for Paper Reference", `${promoteId} marked PROMOTED_PAPER_CANDIDATE (zero live authorization).`, "ok");
      } else if (rejectId) {
        await api(`/api/model-lab/models/${encodeURIComponent(rejectId)}/reject`, { body: { note: "Rejected by operator in Model Lab" } });
        await loadModelLabOverview();
        toast("Model rejected", `${rejectId} marked REJECTED.`, "warn");
      } else if (exportId) {
        const bundle = await api(`/api/model-lab/models/${encodeURIComponent(exportId)}/export`);
        if ($("ml-bundle-json")) $("ml-bundle-json").value = JSON.stringify(bundle, null, 2);
        toast("Model bundle exported", `SHA-256 ${bundle.bundle_sha256.slice(0, 14)}… written to inspector.`, "ok");
      }
    } catch (err) { toast("Model registry action failed", err.message, "err"); }
  });

  $("ml-import-bundle-btn")?.addEventListener("click", async () => {
    const raw = $("ml-bundle-json")?.value.trim();
    if (!raw) { toast("Bundle JSON empty", "Paste or export a valid axiom.model_lab.bundle.v1 JSON first.", "warn"); return; }
    try {
      const parsed = JSON.parse(raw);
      const res = await api("/api/model-lab/models/import", { body: { bundle: parsed } });
      await loadModelLabOverview();
      toast("Bundle SHA-256 verified & imported", `Imported ${res.model.model_id} (paper reference only).`, "ok");
    } catch (err) { toast("Bundle import rejected", err.message, "err"); }
  });
}

/* =========================================================================
 * 11 LIVE TRADE MONITOR (STRICTLY READ-ONLY — ZERO ORDER MUTATION CALLS)
 * ========================================================================= */
async function loadTradeMonitor() {
  const mkt = $("tm-filter-market")?.value || "ALL";
  const mode = $("tm-filter-mode")?.value || "ALL";
  const st = $("tm-filter-state")?.value || "ALL";
  const q = $("tm-filter-search")?.value.trim() || "";
  const params = new URLSearchParams({
    market: mkt,
    execution_mode: mode,
    state: st,
    search: q,
    page: String(state.tm.page),
    page_size: String(state.tm.pageSize),
  });
  try {
    state.tm.summary = await api(`/api/markets/trade-monitor?${params.toString()}`);
    renderTradeMonitor();
    // Auto-load chart only for currently selected row (or first row if none selected yet)
    const items = state.tm.summary?.items || [];
    if (!state.tm.selected && items.length > 0 && state.page === "monitor") {
      state.tm.selected = {
        market: items[0].market,
        symbol: items[0].symbol,
        execution_mode: items[0].execution_mode,
      };
      await loadTradeMonitorChart(state.tm.selected);
    }
  } catch (err) {
    if ($("tm-banner-reason")) $("tm-banner-reason").textContent = `Monitor refresh failed: ${err.message}`;
  }
}

async function loadTradeMonitorChart(sel) {
  if (!sel || !sel.market || !sel.symbol) return;
  const tf = $("tm-chart-tf")?.value || "5m";
  const params = new URLSearchParams({
    market: sel.market,
    symbol: sel.symbol,
    timeframe: tf,
    execution_mode: sel.execution_mode || "LIVE_MARKET_PAPER",
  });
  try {
    const payload = await api(`/api/markets/trade-monitor/chart?${params.toString()}`);
    state.tm.chartPayload = payload;
    state.tm.selectedMarkerId = "";
    renderTradeMonitorChartSection();
  } catch (err) {
    state.tm.chartPayload = {
      ok: false,
      market: sel.market,
      symbol: sel.symbol,
      currency: sel.market === "NSE" ? "INR" : "USD",
      timeframe: tf,
      connection_state: "ERROR",
      data_source: "unavailable",
      bars: [],
      markers: [],
      reference_lines: [],
      error: err.message,
      session_banner: { actively_trading: false, banner_status: "NOT TRADING", banner_tone: "inactive", reason: err.message },
    };
    renderTradeMonitorChartSection();
  }
}

function renderTradeMonitor() {
  const sum = state.tm.summary;
  if (!sum) return;
  const counts = sum.counts || {};
  const pag = sum.pagination || { page: 1, total_pages: 1, total_items: 0 };
  if ($("tm-count-total")) $("tm-count-total").textContent = `${counts.total_unfiltered || 0} MONITORED`;
  if ($("tm-count-positions")) $("tm-count-positions").textContent = `${counts.open_positions || 0} POSITIONS`;
  if ($("tm-count-orders")) $("tm-count-orders").textContent = `${counts.working_orders || 0} WORKING ORDERS`;
  if ($("tm-count-fills")) $("tm-count-fills").textContent = `${counts.recent_fills || 0} FILLS`;
  if ($("tm-count-signals")) $("tm-count-signals").textContent = `${counts.active_signals || 0} SIGNALS`;
  if ($("tm-page-info")) $("tm-page-info").textContent = `Page ${pag.page} / ${pag.total_pages} (${pag.total_items} matching)`;
  if ($("tm-prev-page")) $("tm-prev-page").disabled = !pag.has_prev;
  if ($("tm-next-page")) $("tm-next-page").disabled = !pag.has_next;

  // Global/default banner before or alongside selected instrument
  if (!state.tm.chartPayload && sum.banners) {
    const nseB = sum.banners.NSE_PAPER || {};
    const usB = sum.banners.NASDAQ_PAPER || {};
    const anyActive = Boolean(nseB.actively_trading || usB.actively_trading);
    const bannerEl = $("tm-session-banner");
    if (bannerEl) {
      bannerEl.className = `tm-session-banner ${anyActive ? "paper" : "inactive"}`;
      $("tm-banner-status").textContent = anyActive ? "TRADING ACTIVE · PAPER (SIMULATED)" : "NOT TRADING";
      $("tm-banner-reason").textContent = `NSE Paper: ${nseB.banner_status || "NOT TRADING"} · NASDAQ Paper: ${usB.banner_status || "NOT TRADING"} · Live Gate: OFF`;
    }
  }

  const tbody = $("tm-instruments-rows");
  if (!tbody) return;
  const items = sum.items || [];
  if (!items.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="empty-state">No monitored instruments match the current filter.</td></tr>';
    return;
  }
  const ccySym = (c) => (c === "INR" ? "₹" : "$");
  tbody.innerHTML = items.map((row) => {
    const isSel = state.tm.selected
      && state.tm.selected.market === row.market
      && state.tm.selected.symbol === row.symbol
      && state.tm.selected.execution_mode === row.execution_mode;
    const modeCls = row.simulated ? "info" : "err";
    const sessTxt = row.actively_trading ? `${row.session_state} (ACTIVE)` : `NOT TRADING (${row.session_state})`;
    const posTxt = row.position_qty > 0
      ? `${row.position_qty} @ ${ccySym(row.currency)}${Number(row.avg_entry_price || 0).toFixed(2)} (${row.unrealized_pnl >= 0 ? "+" : ""}${ccySym(row.currency)}${Number(row.unrealized_pnl || 0).toFixed(2)})`
      : "Flat";
    const ordSigTxt = `Ord: ${row.working_orders_count} · Fills: ${row.recent_fills_count} · Sig: ${row.latest_signal}`;
    return `<tr class="${isSel ? "selected-row" : ""}">
      <td><strong>${escapeHtml(row.market)}:${escapeHtml(row.symbol)}</strong><div class="small muted">${escapeHtml(row.currency)} · ${escapeHtml(row.timeframe)}</div></td>
      <td><span class="status-pill ${modeCls}">${escapeHtml(row.mode_badge)}</span><div class="small muted">${escapeHtml(row.verification_origin)}</div></td>
      <td>${escapeHtml(sessTxt)}</td>
      <td><span class="status-pill">${escapeHtml(row.primary_state)}</span></td>
      <td>${escapeHtml(posTxt)}</td>
      <td>${escapeHtml(ordSigTxt)}</td>
      <td><button type="button" class="button secondary xs" data-tm-select-mkt="${escapeHtml(row.market)}" data-tm-select-sym="${escapeHtml(row.symbol)}" data-tm-select-mode="${escapeHtml(row.execution_mode)}">Chart &amp; Events</button></td>
    </tr>`;
  }).join("");
}

function renderTradeMonitorChartSection() {
  const p = state.tm.chartPayload;
  if (!p) return;
  const banner = p.session_banner || { actively_trading: false, banner_status: "NOT TRADING", banner_tone: "inactive", reason: "Session inactive" };
  const bannerEl = $("tm-session-banner");
  if (bannerEl) {
    bannerEl.className = `tm-session-banner ${banner.banner_tone || "inactive"}`;
    $("tm-banner-status").textContent = banner.banner_status || "NOT TRADING";
    $("tm-banner-reason").textContent = banner.reason || "";
  }
  const modeBadge = $("tm-selected-mode-badge");
  if (modeBadge) {
    const isLive = p.execution_mode === "LIVE_MARKET_LIVE" && !p.simulated_execution;
    modeBadge.className = `mode-badge ${isLive ? "live" : "paper"}`;
    modeBadge.textContent = isLive ? "LIVE · BROKER GATE" : "PAPER · SIMULATED FILLS";
  }

  if ($("tm-chart-heading")) {
    $("tm-chart-heading").textContent = `${p.market}:${p.symbol} (${p.currency}) · ${p.execution_mode === "LIVE_MARKET_LIVE" ? "LIVE BROKER JOURNAL" : "LIVE-MARKET PAPER (SIMULATED)"}`;
  }
  const bars = Array.isArray(p.bars) ? p.bars : [];
  const latestBarTs = bars.length ? bars[bars.length - 1].timestamp : "NONE";
  const latestQuoteTs = p.quote?.timestamp || "NONE";
  if ($("tm-chart-meta")) {
    $("tm-chart-meta").textContent = `Market: ${p.market} · Currency: ${p.currency} · TF: ${p.timeframe} · Source: ${p.data_source || "unavailable"} (${p.connection_state || "DISCONNECTED"}) · Latest Bar: ${latestBarTs} · Latest Quote: ${latestQuoteTs}`;
  }

  const markers = Array.isArray(p.markers) ? p.markers : [];
  if ($("tm-events-count")) $("tm-events-count").textContent = `${markers.length} EVENTS`;

  const evRows = $("tm-events-rows");
  if (evRows) {
    if (!markers.length) {
      evRows.innerHTML = '<tr><td colspan="10" class="empty-state">No signals, orders, fills, or exits recorded for this instrument yet.</td></tr>';
    } else {
      const symCcy = p.currency === "INR" ? "₹" : "$";
      evRows.innerHTML = markers.map((m) => {
        const originBadge = m.simulated
          ? '<span class="status-pill info">SIMULATED (PAPER)</span>'
          : '<span class="status-pill err">BROKER-CONFIRMED (LIVE)</span>';
        return `<tr data-tm-marker-row="${escapeHtml(m.marker_id)}" style="cursor:pointer">
          <td>${escapeHtml(m.event_time)}</td>
          <td><strong>${escapeHtml(m.marker_type)}</strong> <span class="small muted">(${escapeHtml(m.shape)})</span></td>
          <td>${originBadge}</td>
          <td>${escapeHtml(m.side)}</td>
          <td>${Number(m.quantity || 0)}</td>
          <td>${m.price > 0 ? `${symCcy}${Number(m.price).toFixed(2)}` : "—"}</td>
          <td>${symCcy}${Number(m.fees || 0).toFixed(2)}</td>
          <td>${escapeHtml(m.strategy)}</td>
          <td>${escapeHtml(m.order_or_fill_status)}</td>
          <td>${escapeHtml(m.reason)}</td>
        </tr>`;
      }).join("");
    }
  }

  drawTradeMonitorCanvas();
}

function drawTradeMonitorCanvas() {
  const canvas = $("tm-chart-canvas");
  const emptyEl = $("tm-chart-empty");
  const p = state.tm.chartPayload;
  state.tm.hitBoxes = [];
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  const W = canvas.width || 900;
  const H = canvas.height || 360;
  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = "#10141f";
  ctx.fillRect(0, 0, W, H);

  const bars = Array.isArray(p?.bars) ? p.bars : [];
  const markers = Array.isArray(p?.markers) ? p.markers : [];
  if (!bars.length) {
    if (emptyEl) {
      emptyEl.hidden = false;
      const st = p?.connection_state || "DISCONNECTED";
      emptyEl.textContent = p
        ? `${p.market}:${p.symbol} market-data feed is ${st} (${p.data_source || "no credentials"}). Zero sample/mock candles substituted. ${markers.length ? `(${markers.length} ledger event(s) remain inspectable in the table below.)` : ""}`
        : "Select an instrument to load real provider candlesticks.";
    }
    return;
  }
  if (emptyEl) emptyEl.hidden = true;

  const padL = 16; const padR = 76; const padT = 20; const padB = 30;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;
  const visible = bars.slice(-120);

  let minP = Infinity; let maxP = -Infinity;
  for (const b of visible) {
    if (b.low < minP) minP = Number(b.low);
    if (b.high > maxP) maxP = Number(b.high);
  }
  for (const ref of (p.reference_lines || [])) {
    if (ref.price > 0) {
      minP = Math.min(minP, Number(ref.price));
      maxP = Math.max(maxP, Number(ref.price));
    }
  }
  for (const m of markers) {
    if (m.price > 0) {
      minP = Math.min(minP, Number(m.price));
      maxP = Math.max(maxP, Number(m.price));
    }
  }
  if (!Number.isFinite(minP) || !Number.isFinite(maxP) || maxP <= minP) {
    minP = 95; maxP = 105;
  }
  const margin = (maxP - minP) * 0.08 || 1;
  minP -= margin; maxP += margin;

  const yOf = (price) => padT + ((maxP - price) / (maxP - minP)) * plotH;
  const stepX = plotW / Math.max(1, visible.length);
  const xOfIdx = (i) => padL + (i + 0.5) * stepX;

  // Price grid & right axis
  ctx.strokeStyle = "rgba(148,163,184,0.12)";
  ctx.fillStyle = "#94a3b8";
  for (let g = 0; g <= 4; g += 1) {
    const gy = padT + (plotH * g) / 4;
    const gPrice = maxP - ((maxP - minP) * g) / 4;
    ctx.beginPath(); ctx.moveTo(padL, gy); ctx.lineTo(W - padR, gy); ctx.stroke();
    ctx.fillText(gPrice.toFixed(2), W - padR + 6, gy + 4);
  }

  // Time axis labels
  const timeTicks = [0, Math.floor(visible.length / 2), visible.length - 1];
  for (const ti of new Set(timeTicks)) {
    if (visible[ti]) {
      const tx = xOfIdx(ti);
      const lbl = String(visible[ti].timestamp || "").slice(5, 16).replace("T", " ");
      ctx.fillText(lbl, Math.max(padL, tx - 28), H - 8);
    }
  }

  // Candlesticks
  const bodyW = Math.max(2, Math.min(12, stepX * 0.66));
  visible.forEach((b, idx) => {
    const x = xOfIdx(idx);
    const yo = yOf(Number(b.open));
    const yc = yOf(Number(b.close));
    const yh = yOf(Number(b.high));
    const yl = yOf(Number(b.low));
    const bull = Number(b.close) >= Number(b.open);
    ctx.strokeStyle = bull ? "#22c55e" : "#ef4444";
    ctx.fillStyle = bull ? "#22c55e" : "#ef4444";
    ctx.beginPath(); ctx.moveTo(x, yh); ctx.lineTo(x, yl); ctx.stroke();
    const top = Math.min(yo, yc);
    const bh = Math.max(1.5, Math.abs(yc - yo));
    ctx.fillRect(x - bodyW / 2, top, bodyW, bh);
  });

  // Reference lines (AVG_ENTRY, STOP_LOSS, TAKE_PROFIT)
  if (typeof ctx.setLineDash === "function") ctx.setLineDash([4, 4]);
  for (const ref of (p.reference_lines || [])) {
    const ry = yOf(Number(ref.price));
    ctx.strokeStyle = ref.kind === "STOP_LOSS" ? "#f87171" : (ref.kind === "TAKE_PROFIT" ? "#4ade80" : "#38bdf8");
    ctx.beginPath(); ctx.moveTo(padL, ry); ctx.lineTo(W - padR, ry); ctx.stroke();
    ctx.fillStyle = ctx.strokeStyle;
    ctx.fillText(String(ref.label || ref.kind), padL + 6, Math.max(padT + 10, ry - 4));
  }
  if (typeof ctx.setLineDash === "function") ctx.setLineDash([]);

  // Map each marker to nearest bar index & draw its distinct shape
  const barTimes = visible.map((b) => Date.parse(b.timestamp) || 0);
  markers.forEach((m, mIdx) => {
    const mts = Date.parse(m.event_time) || barTimes[barTimes.length - 1] || 0;
    let bestIdx = visible.length - 1;
    let bestDiff = Infinity;
    for (let i = 0; i < barTimes.length; i += 1) {
      const d = Math.abs(barTimes[i] - mts);
      if (d <= bestDiff) { bestDiff = d; bestIdx = i; }
    }
    const bar = visible[bestIdx];
    const mx = xOfIdx(bestIdx);
    const refPx = m.price > 0 ? Number(m.price) : Number(bar.close);
    // Offset signals slightly above/below bar high/low so they never collide with fill triangles
    const my = m.marker_type === "SIGNAL"
      ? (m.side === "BUY" ? yOf(Number(bar.low)) + 12 : yOf(Number(bar.high)) - 12)
      : yOf(refPx);
    const clampedY = Math.max(padT + 8, Math.min(padT + plotH - 8, my));
    const isSelected = state.tm.selectedMarkerId === m.marker_id;

    drawDistinctEventGlyph(ctx, m.marker_type, m.side, mx, clampedY, isSelected);
    state.tm.hitBoxes.push({
      marker_id: m.marker_id,
      x: mx,
      y: clampedY,
      r: 11,
      marker: m,
      order: mIdx,
    });
  });
}

function drawDistinctEventGlyph(ctx, markerType, side, x, y, highlighted) {
  ctx.save();
  if (highlighted) {
    ctx.strokeStyle = "#fde047";
    ctx.strokeRect(x - 10, y - 10, 20, 20);
  }
  if (markerType === "SIGNAL") {
    // Hollow diamond — unmistakably advisory signal, NOT an order or fill
    ctx.strokeStyle = "#38bdf8";
    ctx.beginPath();
    ctx.moveTo(x, y - 7); ctx.lineTo(x + 7, y); ctx.lineTo(x, y + 7); ctx.lineTo(x - 7, y); ctx.lineTo(x, y - 7);
    ctx.stroke();
  } else if (markerType === "ORDER_SUBMITTED") {
    // Hollow square
    ctx.strokeStyle = "#eab308";
    ctx.strokeRect(x - 6, y - 6, 12, 12);
  } else if (markerType === "PARTIAL_FILL") {
    // Half-filled box/circle
    ctx.strokeStyle = "#fb923c";
    ctx.fillStyle = "#fb923c";
    ctx.strokeRect(x - 6, y - 6, 12, 12);
    ctx.fillRect(x - 6, y - 6, 6, 12);
  } else if (markerType === "COMPLETED_FILL") {
    // Solid filled directional triangle
    ctx.fillStyle = side === "SELL" ? "#f43f5e" : "#10b981";
    ctx.beginPath();
    if (side === "SELL") {
      ctx.moveTo(x, y + 7); ctx.lineTo(x - 7, y - 6); ctx.lineTo(x + 7, y - 6);
    } else {
      ctx.moveTo(x, y - 7); ctx.lineTo(x - 7, y + 6); ctx.lineTo(x + 7, y + 6);
    }
    ctx.fill();
  } else if (markerType === "CANCELED") {
    // Diagonal X cross
    ctx.strokeStyle = "#94a3b8";
    ctx.beginPath();
    ctx.moveTo(x - 6, y - 6); ctx.lineTo(x + 6, y + 6);
    ctx.moveTo(x + 6, y - 6); ctx.lineTo(x - 6, y + 6);
    ctx.stroke();
  } else if (markerType === "STOP_LOSS") {
    // Red SL shield
    ctx.strokeStyle = "#ef4444";
    ctx.beginPath();
    ctx.moveTo(x - 6, y - 6); ctx.lineTo(x + 6, y - 6); ctx.lineTo(x + 6, y + 1); ctx.lineTo(x, y + 7); ctx.lineTo(x - 6, y + 1); ctx.lineTo(x - 6, y - 6);
    ctx.stroke();
  } else if (markerType === "TAKE_PROFIT") {
    // Green concentric target
    ctx.strokeStyle = "#22c55e";
    ctx.strokeRect(x - 6, y - 6, 12, 12);
    ctx.fillStyle = "#22c55e";
    ctx.fillRect(x - 2, y - 2, 4, 4);
  } else if (markerType === "EXIT") {
    // Double chevron exit glyph
    ctx.strokeStyle = "#c084fc";
    ctx.beginPath();
    ctx.moveTo(x - 6, y - 5); ctx.lineTo(x, y); ctx.lineTo(x - 6, y + 5);
    ctx.moveTo(x, y - 5); ctx.lineTo(x + 6, y); ctx.lineTo(x, y + 5);
    ctx.stroke();
  }
  ctx.restore();
}

function selectTradeMonitorMarker(marker) {
  if (!marker) return;
  state.tm.selectedMarkerId = marker.marker_id;
  drawTradeMonitorCanvas();
  const detailEl = $("tm-marker-detail-body");
  if (!detailEl) return;
  const symCcy = marker.currency === "INR" ? "₹" : "$";
  const originClass = marker.simulated ? "ok" : "err";
  detailEl.innerHTML = `<div class="contract-grid">
    <div><span>Event Time</span><strong>${escapeHtml(marker.event_time)}</strong></div>
    <div><span>Marker / Glyph</span><strong>${escapeHtml(marker.marker_type)} (${escapeHtml(marker.shape)})</strong></div>
    <div><span>Side</span><strong>${escapeHtml(marker.side)}</strong></div>
    <div><span>Quantity</span><strong>${Number(marker.quantity || 0)}</strong></div>
    <div><span>Price</span><strong>${marker.price > 0 ? `${symCcy}${Number(marker.price).toFixed(4)}` : "N/A (Signal)"}</strong></div>
    <div><span>Fees</span><strong>${symCcy}${Number(marker.fees || 0).toFixed(4)}</strong></div>
    <div><span>Strategy</span><strong>${escapeHtml(marker.strategy)}</strong></div>
    <div><span>Order / Fill Status</span><strong>${escapeHtml(marker.order_or_fill_status)}</strong></div>
    <div><span>Execution Mode</span><strong>${escapeHtml(marker.execution_mode)}</strong></div>
    <div><span>Simulated vs Confirmed</span><strong class="${originClass}">${escapeHtml(marker.verification_origin)}</strong></div>
  </div>
  <p class="small top-space"><strong>Reason / Rationale:</strong> ${escapeHtml(marker.reason)}</p>`;
}

function bindTradeMonitorEvents() {
  $("tm-refresh-btn")?.addEventListener("click", async () => {
    await loadTradeMonitor();
    if (state.tm.selected) await loadTradeMonitorChart(state.tm.selected);
  });
  for (const id of ["tm-filter-market", "tm-filter-mode", "tm-filter-state"]) {
    $(id)?.addEventListener("change", () => { state.tm.page = 1; loadTradeMonitor(); });
  }
  $("tm-filter-search")?.addEventListener("input", () => { state.tm.page = 1; loadTradeMonitor(); });
  $("tm-prev-page")?.addEventListener("click", () => {
    if (state.tm.page > 1) { state.tm.page -= 1; loadTradeMonitor(); }
  });
  $("tm-next-page")?.addEventListener("click", () => {
    const maxP = state.tm.summary?.pagination?.total_pages || 1;
    if (state.tm.page < maxP) { state.tm.page += 1; loadTradeMonitor(); }
  });
  $("tm-chart-tf")?.addEventListener("change", () => {
    if (state.tm.selected) loadTradeMonitorChart(state.tm.selected);
  });
  $("tm-instruments-rows")?.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-tm-select-sym]");
    if (!btn) return;
    state.tm.selected = {
      market: btn.dataset.tmSelectMkt,
      symbol: btn.dataset.tmSelectSym,
      execution_mode: btn.dataset.tmSelectMode || "LIVE_MARKET_PAPER",
    };
    renderTradeMonitor();
    await loadTradeMonitorChart(state.tm.selected);
  });
  $("tm-chart-canvas")?.addEventListener("click", (e) => {
    const canvas = $("tm-chart-canvas");
    if (!canvas || !state.tm.hitBoxes.length) return;
    const rect = canvas.getBoundingClientRect();
    const scaleX = canvas.width / Math.max(1, rect.width || canvas.width);
    const scaleY = canvas.height / Math.max(1, rect.height || canvas.height);
    const cx = (e.clientX - (rect.left || 0)) * scaleX;
    const cy = (e.clientY - (rect.top || 0)) * scaleY;
    let best = null; let bestDist = Infinity;
    for (const hb of state.tm.hitBoxes) {
      const dist = Math.hypot(cx - hb.x, cy - hb.y);
      if (dist <= hb.r + 4 && dist < bestDist) { best = hb; bestDist = dist; }
    }
    if (best) selectTradeMonitorMarker(best.marker);
  });
  $("tm-events-rows")?.addEventListener("click", (e) => {
    const tr = e.target.closest("[data-tm-marker-row]");
    if (!tr) return;
    const id = tr.dataset.tmMarkerRow;
    const found = (state.tm.chartPayload?.markers || []).find((m) => m.marker_id === id);
    if (found) selectTradeMonitorMarker(found);
  });
}

/* =========================================================================
 * 12 PARALLEL STRATEGY ENSEMBLE & PORTFOLIO RISK ALLOCATOR
 * ========================================================================= */
async function loadEnsembleOverview() {
  try {
    state.ens.overview = await api("/api/ensemble/overview");
    renderEnsemble();
    await loadScanCapacity();
  } catch (err) {
    if ($("ens-eval-summary")) $("ens-eval-summary").textContent = `Ensemble overview unavailable: ${err.message}`;
  }
}

function renderEnsemble() {
  const ov = state.ens.overview;
  if (!ov) return;
  const reg = ov.registry || {};
  if ($("ens-variants-pill")) {
    $("ens-variants-pill").textContent = `${reg.enabled_variants || 0} VARIANTS (${reg.unique_canonical_variants || 0} UNIQUE)`;
  }
  if ($("ens-preset") && ["compact", "standard", "large_256", "massive_1024"].includes(ov.preset)) {
    $("ens-preset").value = ov.preset;
  }
  if ($("ens-timeframe") && ov.default_timeframe) $("ens-timeframe").value = ov.default_timeframe;
  if ($("ens-workers") && ov.concurrency_limit) $("ens-workers").value = ov.concurrency_limit;
  if ($("ens-timeout") && ov.batch_timeout_seconds) $("ens-timeout").value = ov.batch_timeout_seconds;

  const enabledSet = new Set(ov.enabled_families || []);
  document.querySelectorAll("#ens-family-checks input[type='checkbox']").forEach((cb) => {
    cb.checked = enabledSet.has(cb.value);
  });

  const activeMkt = $("ens-alloc-market")?.value || "NSE";
  const acfg = (ov.allocators || {})[activeMkt] || {};
  if ($("ens-kronos-enabled")) $("ens-kronos-enabled").value = acfg.kronos_enabled ? "true" : "false";
  if ($("ens-kronos-policy") && acfg.kronos_fail_closed_policy) $("ens-kronos-policy").value = acfg.kronos_fail_closed_policy;
  if ($("ens-kronos-weight") && acfg.kronos_advisory_weight !== undefined) $("ens-kronos-weight").value = acfg.kronos_advisory_weight;
  if ($("ens-min-conf") && acfg.min_calibrated_confidence !== undefined) $("ens-min-conf").value = acfg.min_calibrated_confidence;
  if ($("ens-min-oos") && acfg.min_oos_samples !== undefined) $("ens-min-oos").value = acfg.min_oos_samples;
  if ($("ens-risk-pct") && acfg.per_trade_risk_pct !== undefined) $("ens-risk-pct").value = (Number(acfg.per_trade_risk_pct) * 100).toFixed(2);
  if ($("ens-max-sym-exp") && acfg.max_symbol_exposure_pct !== undefined) $("ens-max-sym-exp").value = Math.round(Number(acfg.max_symbol_exposure_pct) * 100);
  if ($("ens-max-tot-exp") && acfg.max_total_exposure_pct !== undefined) $("ens-max-tot-exp").value = Math.round(Number(acfg.max_total_exposure_pct) * 100);
  if ($("ens-max-dd") && acfg.max_drawdown_pct !== undefined) $("ens-max-dd").value = (Number(acfg.max_drawdown_pct) * 100).toFixed(1);
  if ($("ens-max-corr") && acfg.max_position_correlation !== undefined) $("ens-max-corr").value = acfg.max_position_correlation;
  if ($("ens-max-spread") && acfg.max_spread_bps !== undefined) $("ens-max-spread").value = acfg.max_spread_bps;
  if ($("ens-max-pos") && acfg.max_open_positions !== undefined) $("ens-max-pos").value = acfg.max_open_positions;

  const calMkt = (ov.calibration || {})[activeMkt] || {};
  const wMkt = (ov.weights || {})[activeMkt] || {};
  if ($("ens-resource-summary")) {
    $("ens-resource-summary").innerHTML = `
      <div><span>Enabled / Unique</span><strong>${reg.enabled_variants || 0} / ${reg.unique_canonical_variants || 0}</strong></div>
      <div><span>Duplicates Deduped</span><strong>${reg.duplicate_variants_detected || 0}</strong></div>
      <div><span>Worker Pool Cap</span><strong>${ov.concurrency_limit} threads</strong></div>
      <div><span>Process RSS</span><strong>${ov.process_rss_mb != null ? `${ov.process_rss_mb} MB` : "N/A"}</strong></div>
      <div><span>${escapeHtml(activeMkt)} OOS Weights &gt; 0</span><strong>${wMkt.positive_weight_count || 0}</strong></div>
      <div><span>${escapeHtml(activeMkt)} Calibrator N / ECE</span><strong>${calMkt.total_oos_samples || 0} / ${calMkt.ece ?? "—"}</strong></div>
    `;
  }
  if ($("ens-weights-json") && !$("ens-weights-json").value.trim() && wMkt.weights && Object.keys(wMkt.weights).length) {
    $("ens-weights-json").value = JSON.stringify(wMkt.weights);
  }

  // Populate Ablation dataset dropdown from Model Lab overview if available
  const dsSel = $("ens-abl-dataset");
  const dsList = state.ml.overview?.datasets || [];
  if (dsSel && dsList.length) {
    const prev = dsSel.value;
    dsSel.innerHTML = dsList.map((d) => `<option value="${escapeHtml(d.dataset_id)}">${escapeHtml(d.dataset_id)} (${escapeHtml(d.market)} · ${d.bar_count} bars)</option>`).join("");
    if (prev && dsList.some((d) => d.dataset_id === prev)) dsSel.value = prev;
  }

  // Render recent decisions
  const decs = ov.recent_decisions || [];
  const dRows = $("ens-decisions-rows");
  if (dRows) {
    if (!decs.length) {
      dRows.innerHTML = '<tr><td colspan="9" class="empty-state">No ensemble bar decisions recorded yet.</td></tr>';
    } else {
      dRows.innerHTML = decs.map((d, idx) => {
        const agg = d.aggregation || {};
        const cal = d.calibration || {};
        const kc = d.kronos_contribution || {};
        const confTxt = cal.calibrated_confidence != null
          ? `${(Number(cal.calibrated_confidence) * 100).toFixed(1)}% [${(Number(cal.ci90_low || 0) * 100).toFixed(0)}–${(Number(cal.ci90_high || 1) * 100).toFixed(0)}%] (N=${cal.oos_sample_size})`
          : `UNCALIBRATED (N=${cal.oos_sample_size || 0})`;
        const reasons = (d.no_trade_reasons || []).slice(0, 2).join("; ") || "All 12 risk checks passed";
        const actCls = d.final_action === "BUY" ? "ok" : (d.final_action === "SELL" ? "warn" : "");
        return `<tr>
          <td>${escapeHtml(String(d.bar_timestamp || "").slice(0, 19))}</td>
          <td><strong>${escapeHtml(d.market)}:${escapeHtml(d.symbol)}</strong> (${escapeHtml(d.timeframe)})</td>
          <td><span class="status-pill ${actCls}">${escapeHtml(d.final_action)}</span></td>
          <td>${Number(d.selected_qty || 0)}</td>
          <td>${Number(agg.pre_kronos_consensus || 0).toFixed(3)} / ${Number(agg.post_kronos_consensus || 0).toFixed(3)}</td>
          <td>${escapeHtml(confTxt)}</td>
          <td>${escapeHtml(kc.status || "DISABLED")}</td>
          <td>${escapeHtml(reasons)}</td>
          <td><button type="button" class="button secondary xs" data-ens-inspect-idx="${idx}">Inspect</button></td>
        </tr>`;
      }).join("");
      const selIdx = Math.min(state.ens.selectedDecisionIdx, decs.length - 1);
      renderSelectedEnsembleDecision(decs[selIdx]);
    }
  }

  const abl = state.ens.latestAblation || (ov.ablation_runs || [])[0];
  if (abl) renderEnsembleAblation(abl);
  const bench = state.ens.latestBenchmark || (ov.benchmarks || [])[0];
  if (bench) renderEnsembleBenchmark(bench);
}

function renderSelectedEnsembleDecision(dec) {
  if (!dec) return;
  const rRows = $("ens-risk-checks-rows");
  if (rRows) {
    const checks = dec.risk_checks || [];
    rRows.innerHTML = checks.map((c) => `<tr>
      <td><code>${escapeHtml(c.check_id)}</code></td>
      <td><span class="status-pill ${c.passed ? "ok" : "err"}">${c.passed ? "PASS" : "BLOCK"}</span></td>
      <td>${escapeHtml(typeof c.actual === "object" ? JSON.stringify(c.actual) : String(c.actual))}</td>
      <td>${escapeHtml(typeof c.limit === "object" ? JSON.stringify(c.limit) : String(c.limit))}</td>
      <td>${escapeHtml(c.detail)}</td>
    </tr>`).join("");
  }
  const sRows = $("ens-strategy-signals-rows");
  if (sRows) {
    const sigs = (dec.strategy_signals || []).slice(0, 40);
    sRows.innerHTML = sigs.length
      ? sigs.map((s) => `<tr>
          <td><strong>${escapeHtml(s.strategy_id)}</strong><div class="small muted">${escapeHtml(s.family)}</div></td>
          <td>${escapeHtml(s.action)}</td>
          <td>${Number(s.raw_score || 0).toFixed(3)}</td>
          <td>${Number(s.weight || 0).toFixed(4)}</td>
          <td>${escapeHtml(s.status)}</td>
          <td>${escapeHtml(s.rationale)}</td>
        </tr>`).join("")
      : '<tr><td colspan="6" class="empty-state">No per-strategy signals recorded.</td></tr>';
  }
}

function renderEnsembleAblation(abl) {
  if (!abl) return;
  const mt = abl.multiple_testing || {};
  const ks = abl.kronos_ablation_summary || {};
  const cal = abl.calibration || {};
  if ($("ens-ablation-summary")) {
    const evBanner = abl.evidence_classification?.banner || (abl.dataset?.real_market_data ? "HISTORICAL OOS EVIDENCE — NOT EXPECTED FUTURE RETURNS" : "SYNTHETIC ENGINEERING EVIDENCE — NOT EXPECTED RETURNS OR LIVE READINESS");
    $("ens-ablation-summary").textContent =
      `[${evBanner}] Run ${abl.ablation_id} (${abl.market} · ${abl.dataset?.dataset_id} · real_market_data=${abl.dataset?.real_market_data}): ` +
      `Tested ${mt.variants_tested || 0} variants (Multiple-Testing Sharpe Haircut=${mt.sharpe_haircut_threshold || 0}; Accepted OOS=${mt.accepted_count || 0}) · ` +
      `Calibrator OOS N=${cal.total_oos_samples || 0}, Brier=${cal.brier_score ?? "—"}, ECE=${cal.ece ?? "—"} · ` +
      `Kronos Verdict: ${ks.verdict || "N/A"}`;
  }
  const tbody = $("ens-ablation-rows");
  if (!tbody) return;
  tbody.innerHTML = (abl.comparisons || []).map((c) => {
    const u1 = c.untouched_test_1x;
    const s2 = c.stressed_test_2x;
    if (!u1) {
      return `<tr>
        <td><strong>${escapeHtml(c.label)}</strong></td>
        <td><span class="status-pill warn">${escapeHtml(c.status)}</span></td>
        <td colspan="7" class="muted small">${escapeHtml(c.reason || "Skipped")}</td>
      </tr>`;
    }
    return `<tr>
      <td><strong>${escapeHtml(c.label)}</strong></td>
      <td><span class="status-pill ok">${escapeHtml(c.status)}</span></td>
      <td>${Number(u1.net_return_pct).toFixed(2)}%</td>
      <td>${Number(u1.sharpe).toFixed(2)} / ${Number(u1.sortino).toFixed(2)}</td>
      <td>${Number(u1.max_drawdown_pct).toFixed(2)}%</td>
      <td>${u1.trade_count} / ${u1.no_trade_bars}</td>
      <td>${Number(u1.win_rate_pct).toFixed(1)}% [${u1.win_rate_ci90_pct ? u1.win_rate_ci90_pct.join("–") : "—"}%]</td>
      <td>${Number(s2.net_return_pct).toFixed(2)}%</td>
      <td>${Number(s2.sharpe).toFixed(2)}</td>
    </tr>`;
  }).join("");
}

function renderEnsembleBenchmark(bench) {
  const tbody = $("ens-bench-rows");
  if (!tbody || !bench) return;
  tbody.innerHTML = (bench.results || []).map((r) => `<tr>
    <td><strong>${r.variants} &times; ${r.instruments}</strong></td>
    <td>${r.total_evaluations}</td>
    <td>${r.workers_used}</td>
    <td>${Number(r.wall_ms).toFixed(2)} ms</td>
    <td>${Number(r.per_instrument_p50_ms).toFixed(2)} / ${Number(r.per_instrument_p95_ms).toFixed(2)} ms</td>
    <td>${Number(r.throughput_evals_per_sec).toFixed(0)} /s</td>
    <td>${Number(r.peak_tracemalloc_mb).toFixed(2)} / ${r.process_rss_mb ?? "—"} MB</td>
    <td>${r.missed_instruments} / ${r.duplicate_bars_processed}</td>
  </tr>`).join("");
}

function bindEnsembleEvents() {
  $("ens-refresh-btn")?.addEventListener("click", loadEnsembleOverview);
  $("ens-alloc-market")?.addEventListener("change", renderEnsemble);

  $("ens-config-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fams = Array.from(document.querySelectorAll("#ens-family-checks input[type='checkbox']:checked")).map((el) => el.value);
    try {
      state.ens.overview = await api("/api/ensemble/config", {
        body: {
          preset: $("ens-preset").value,
          enabled_families: fams,
          default_timeframe: $("ens-timeframe").value,
          concurrency_limit: Number($("ens-workers").value),
          batch_timeout_seconds: Number($("ens-timeout").value),
        },
      });
      renderEnsemble();
      toast("Ensemble registry updated", `${state.ens.overview.registry.enabled_variants} variants across ${fams.length} families.`, "ok");
    } catch (err) { toast("Ensemble config rejected", err.message, "err"); }
  });

  $("ens-risk-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const mkt = $("ens-alloc-market").value;
    try {
      state.ens.overview = await api("/api/ensemble/config", {
        body: {
          market: mkt,
          allocator_updates: {
            kronos_enabled: $("ens-kronos-enabled").value === "true",
            kronos_fail_closed_policy: $("ens-kronos-policy").value,
            kronos_advisory_weight: Number($("ens-kronos-weight").value),
            min_calibrated_confidence: Number($("ens-min-conf").value),
            min_oos_samples: Number($("ens-min-oos").value),
            per_trade_risk_pct: Number($("ens-risk-pct").value) / 100.0,
            max_symbol_exposure_pct: Number($("ens-max-sym-exp").value) / 100.0,
            max_total_exposure_pct: Number($("ens-max-tot-exp").value) / 100.0,
            max_drawdown_pct: Number($("ens-max-dd").value) / 100.0,
            max_position_correlation: Number($("ens-max-corr").value),
            max_spread_bps: Number($("ens-max-spread").value),
            max_open_positions: Number($("ens-max-pos").value),
          },
        },
      });
      renderEnsemble();
      toast(`${mkt} Portfolio Risk Allocator saved`, "Confidence, Kronos policy, and exposure caps updated.", "ok");
    } catch (err) { toast("Risk config rejected", err.message, "err"); }
  });

  $("ens-save-weights-btn")?.addEventListener("click", async () => {
    const mkt = $("ens-alloc-market").value;
    const raw = $("ens-weights-json")?.value.trim();
    if (!raw) { toast("Weights JSON empty", "Provide a JSON map of variant_id -> non-negative weight.", "warn"); return; }
    try {
      const parsed = JSON.parse(raw);
      state.ens.overview = await api("/api/ensemble/config", { body: { market: mkt, manual_weights: parsed } });
      renderEnsemble();
      toast(`${mkt} weights updated`, "Manual strategy weight override stored.", "ok");
    } catch (err) { toast("Weights rejected", err.message, "err"); }
  });

  $("ens-eval-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const mkt = $("ens-eval-market").value;
    const rawSyms = $("ens-eval-symbols").value.trim();
    const syms = rawSyms ? rawSyms.split(",").map((s) => s.trim()).filter(Boolean) : null;
    const execPaper = $("ens-eval-paper-mode").value === "true";
    try {
      $("ens-eval-status-pill").textContent = "RUNNING";
      const res = await api("/api/ensemble/evaluate", {
        body: { market: mkt, symbols: syms, execute_in_paper: execPaper },
      });
      $("ens-eval-status-pill").textContent = "COMPLETED";
      const rejCnt = Object.keys(res.rejected_instruments || {}).length;
      $("ens-eval-summary").textContent =
        `Evaluated ${res.total_strategy_evaluations} strategy-bar pairs across ${(res.decisions || []).length} valid symbol(s) in ${res.wall_ms} ms ` +
        `(${res.workers_used} worker(s); ${rejCnt} rejected symbol(s); ${res.orders_submitted_simulated} simulated paper order(s); 0 broker order calls).`;
      await loadEnsembleOverview();
    } catch (err) {
      $("ens-eval-status-pill").textContent = "ERROR";
      $("ens-eval-summary").textContent = `Evaluation failed: ${err.message}`;
      toast("Evaluation failed", err.message, "err");
    }
  });

  $("ens-decisions-rows")?.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-ens-inspect-idx]");
    if (!btn) return;
    state.ens.selectedDecisionIdx = Number(btn.dataset.ensInspectIdx);
    const dec = (state.ens.overview?.recent_decisions || [])[state.ens.selectedDecisionIdx];
    if (dec) renderSelectedEnsembleDecision(dec);
  });

  $("ens-ablation-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const mkt = $("ens-abl-market").value;
    const dsId = $("ens-abl-dataset").value || "bundled_nse_synthetic_1h";
    const tf = $("ens-abl-tf").value;
    const persist = $("ens-abl-persist").value === "true";
    try {
      const abl = await api("/api/ensemble/ablation", {
        body: { market: mkt, dataset_id: dsId, timeframe: tf, persist_learned_weights: persist },
      });
      state.ens.latestAblation = abl;
      await loadEnsembleOverview();
      toast("Walk-Forward & Untouched OOS Ablation Complete", `${abl.ablation_id} (${mkt})`, "ok");
    } catch (err) { toast("Ablation failed", err.message, "err"); }
  });

  $("ens-bench-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const vCounts = $("ens-bench-variants").value.split(",").map((x) => Number(x.trim())).filter((n) => n > 0);
    const iCounts = $("ens-bench-instruments").value.split(",").map((x) => Number(x.trim())).filter((n) => n > 0);
    const workers = Number($("ens-bench-workers").value);
    try {
      const bench = await api("/api/ensemble/benchmark", {
        body: { variant_counts: vCounts, instrument_counts: iCounts, concurrency_limit: workers },
      });
      state.ens.latestBenchmark = bench;
      renderEnsembleBenchmark(bench);
      toast("Scaling benchmark complete", `${(bench.results || []).length} configurations measured.`, "ok");
    } catch (err) { toast("Benchmark failed", err.message, "err"); }
  });
}

/* ---------------------------------------------------------------------------
 * Scan Capacity & Quotas (quota-aware read-only 100-symbol scan)
 * Every number here comes from the server: documented quota windows, live
 * rolling usage and the computed plan. Nothing is hard-coded client-side and
 * no provider request is made by rendering or previewing.
 * ------------------------------------------------------------------------- */
const SCAN_MARKETS = [
  { key: "NSE", label: "NSE · Upstox", provider: "UPSTOX STANDARD APIs · READ-ONLY MARKET DATA" },
  { key: "NASDAQ", label: "NASDAQ · Alpaca", provider: "ALPACA MARKET DATA · READ-ONLY MARKET DATA" },
];
const SCAN_STATUS_CLASS = {
  READY: "ok",
  "SLOWER CADENCE REQUIRED": "warn",
  "QUOTA DEFERRED": "warn",
  "PLAN UNVERIFIED": "info",
  "SCAN IN PROGRESS": "info",
  SCANNING: "info",
  "NO CREDENTIAL": "err",
  "PROVIDER ERROR": "err",
  "CONFIG REQUIRED": "err",
  LOADING: "info",
  IDLE: "info",
};

function scanCardState(market) {
  if (!state.ens.scan) state.ens.scan = { data: null, cards: {}, loading: false, pollTimer: null };
  if (!state.ens.scan.cards[market]) {
    state.ens.scan.cards[market] = {
      snapshot: null, plan: null, quota: null, limits: null, dispatch: [], dirty: false,
      previewing: false, starting: false, error: false, lastRun: null, message: "",
    };
  }
  return state.ens.scan.cards[market];
}

function scanCard(market) {
  return document.querySelector(`#ens-scan-cards [data-scan-market="${market}"]`);
}

function scanEl(market, role) {
  return scanCard(market)?.querySelector(`[data-scan-role="${role}"]`) || null;
}

function scanStatusPill(el, status) {
  if (!el) return;
  const text = String(status || "LOADING");
  el.textContent = text;
  el.className = `status-pill ${SCAN_STATUS_CLASS[text] || "info"}`;
}

function scanCardTemplate(market) {
  const meta = SCAN_MARKETS.find((m) => m.key === market) || { label: market, provider: "MARKET DATA" };
  const k = market.toLowerCase();
  return `
    <div class="panel inset-panel scan-card" data-scan-market="${escapeHtml(market)}" role="group" aria-labelledby="ens-scan-${k}-title">
      <div class="panel-head">
        <div>
          <p class="eyebrow">${escapeHtml(meta.provider)}</p>
          <h4 id="ens-scan-${k}-title">${escapeHtml(meta.label)}</h4>
        </div>
        <span class="status-pill info" data-scan-role="status" aria-live="polite">LOADING</span>
      </div>
      <div class="contract-grid" data-scan-role="facts">
        <div><span>Selected symbols</span><strong>—</strong></div>
      </div>
      <div class="scan-windows" data-scan-role="windows">
        <p class="small muted">Loading documented quota windows…</p>
      </div>
      <form data-scan-role="form" autocomplete="off">
        <div class="form-grid">
          <div class="field">
            <label for="ens-scan-${k}-symbols">Selected symbols per cycle (1–100)</label>
            <input id="ens-scan-${k}-symbols" data-scan-role="symbols" type="number" min="1" max="100" step="1" value="100">
          </div>
          <div class="field">
            <label for="ens-scan-${k}-cadence">Requested cadence (seconds, min 1)</label>
            <input id="ens-scan-${k}-cadence" data-scan-role="cadence" type="number" min="1" max="3600" step="1" value="60">
          </div>
          <div class="field">
            <label for="ens-scan-${k}-min">Server-computed minimum cadence (s)</label>
            <input id="ens-scan-${k}-min" data-scan-role="min" type="text" readonly aria-readonly="true">
          </div>
          <div class="field">
            <label for="ens-scan-${k}-watchlist">Watchlist source</label>
            <select id="ens-scan-${k}-watchlist" data-scan-role="watchlist">
              <option value="saved">Saved ${escapeHtml(market)} watchlist</option>
              <option value="explicit">Explicit symbol list below</option>
            </select>
          </div>
        </div>
        <div class="field top-space">
          <label for="ens-scan-${k}-list">Explicit symbols (comma separated, optional · max 100)</label>
          <input id="ens-scan-${k}-list" data-scan-role="list" type="text" placeholder="e.g. ${market === "NSE" ? "RELIANCE, TCS, INFY" : "AAPL, MSFT, NVDA"}">
        </div>
        <div class="button-row top-space">
          <button type="button" class="button secondary xs" data-scan-role="preview">Preview capacity (offline)</button>
          <button type="button" class="button primary xs" data-scan-role="start" disabled>Start read-only scan</button>
        </div>
      </form>
      <div class="inline-status top-space" data-scan-role="verdict" role="status" aria-live="polite">
        Preview capacity to see what this market can sustain. No provider request is made by previewing.
      </div>
      <div class="scan-estimate" data-scan-role="estimate"></div>
      <div class="scan-results" data-scan-role="results"></div>
    </div>`;
}

function ensureScanCards() {
  const host = $("ens-scan-cards");
  if (!host) return false;
  if (host.querySelectorAll("[data-scan-market]").length === SCAN_MARKETS.length) return true;
  host.innerHTML = SCAN_MARKETS.map((m) => scanCardTemplate(m.key)).join("");
  return true;
}

function scanSymbolList(market) {
  const source = scanEl(market, "watchlist")?.value || "saved";
  const raw = (scanEl(market, "list")?.value || "").trim();
  if (source !== "explicit" || !raw) return null;
  const list = raw.split(/[\s,;]+/).map((s) => s.trim().toUpperCase()).filter(Boolean);
  return list.length ? list : null;
}

function scanRequested(market) {
  const symbols = scanSymbolList(market);
  const rawCount = Number(scanEl(market, "symbols")?.value || 0);
  const cadence = Number(scanEl(market, "cadence")?.value || 0);
  return {
    symbols,
    symbol_count: symbols ? symbols.length : (Number.isFinite(rawCount) && rawCount > 0 ? Math.min(100, Math.round(rawCount)) : 100),
    cadence_s: Number.isFinite(cadence) && cadence >= 1 ? cadence : 60,
  };
}

async function loadScanCapacity(options = {}) {
  if (!ensureScanCards()) return;
  const scan = state.ens.scan || (state.ens.scan = { data: null, cards: {}, loading: false, pollTimer: null });
  if (scan.loading) return;
  scan.loading = true;
  try {
    const data = await api("/api/ensemble/scan/capacity");
    scan.data = data;
    for (const meta of SCAN_MARKETS) {
      const card = scanCardState(meta.key);
      const snap = (data.markets || {})[meta.key];
      if (!snap) { card.error = true; continue; }
      card.snapshot = snap;
      card.quota = snap.quota;
      card.limits = snap.limits;
      card.error = false;
      if (!card.plan || options.reset) card.plan = snap.plan;
      card.history = snap.history || [];
      card.lastRun = (snap.history || [])[0] || card.lastRun;
      if (!card.dirty || options.reset) {
        const countInput = scanEl(meta.key, "symbols");
        if (countInput) countInput.value = String(snap.plan?.symbols ?? 100);
        const cadenceInput = scanEl(meta.key, "cadence");
        if (cadenceInput) cadenceInput.value = String(Math.round(snap.plan?.cadence_requested_seconds ?? 60));
      }
    }
    renderScanHistory();
  } catch (err) {
    for (const meta of SCAN_MARKETS) {
      const card = scanCardState(meta.key);
      card.error = true;
      card.message = err.message;
    }
  } finally {
    scan.loading = false;
    renderScanAll();
  }
  if (scan.data?.scan_status?.running && Object.keys(scan.data.scan_status.running).length) scheduleScanPoll();
}

function scheduleScanPoll() {
  const scan = state.ens.scan;
  if (!scan || scan.pollTimer) return;
  scan.pollTimer = setTimeout(async () => {
    scan.pollTimer = null;
    await loadScanCapacity();
  }, 3500);
}

function renderScanAll() {
  if (!ensureScanCards()) return;
  for (const meta of SCAN_MARKETS) renderScanCard(meta.key);
}

function renderScanCard(market) {
  const card = scanCardState(market);
  const plan = card.plan || {};
  const quota = card.quota || {};
  const snapshot = card.snapshot || {};
  const pill = scanEl(market, "status");
  if (card.error) scanStatusPill(pill, "PROVIDER ERROR");
  else scanStatusPill(pill, plan.status || "LOADING");

  const facts = scanEl(market, "facts");
  if (facts) {
    const allocator = (card.limits || {}).ensemble_allocator || {};
    const paper = (card.limits || {}).paper_risk_gate || {};
    const cred = quota.credential_present
      ? '<strong class="scan-ok">PRESENT (value never shown)</strong>'
      : '<strong class="scan-bad">NOT CONFIGURED</strong>';
    facts.innerHTML = `
      <div><span>Selected symbols this cycle</span><strong>${escapeHtml(plan.symbols ?? "—")} of 100 max</strong></div>
      <div><span>Quota profile</span><strong>${escapeHtml(quota.profile || "—")}</strong></div>
      <div><span>Plan / verification</span><strong>${escapeHtml(quota.verification || "—")} · ${escapeHtml(quota.plan_state || "—")}</strong></div>
      <div><span>Market-data credential</span>${cred}</div>
      <div><span>Requested cadence</span><strong>every ${escapeHtml(plan.cadence_requested_seconds ?? "—")} s</strong></div>
      <div><span>Computed sustainable cadence</span><strong>every ${escapeHtml(plan.cadence_sustainable_seconds ?? "—")} s</strong></div>
      <div><span>Max symbols at requested cadence</span><strong>${escapeHtml(plan.max_symbols_at_requested_cadence ?? "—")}</strong></div>
      <div><span>Requests per cycle</span><strong>${escapeHtml(plan.total_requests_per_cycle ?? "—")}</strong></div>
      <div><span>Expected dispatch pacing</span><strong>${escapeHtml(plan.expected_dispatch_seconds ?? "—")} s at documented rate</strong></div>
      <div><span>Max open positions (unchanged)</span><strong>${escapeHtml(allocator.max_open_positions ?? paper.max_open_positions ?? "—")}</strong></div>
      <div><span>Max symbol / total exposure</span><strong>${allocator.max_symbol_exposure_pct !== undefined ? `${Math.round(Number(allocator.max_symbol_exposure_pct) * 100)}% / ${Math.round(Number(allocator.max_total_exposure_pct) * 100)}%` : "—"}</strong></div>
      <div><span>Max orders per day (unchanged)</span><strong>${escapeHtml(paper.max_daily_orders ?? "—")}</strong></div>`;
  }

  const windows = scanEl(market, "windows");
  if (windows) {
    const rows = Object.entries(plan.window_usage || {});
    if (!rows.length) {
      windows.innerHTML = '<p class="small muted">No documented quota windows are active for this market; the server will not plan a scan.</p>';
    } else {
      windows.innerHTML = rows.map(([category, list]) => {
        const label = (quota.policies || []).find((p) => p.category === category)?.label || category;
        const body = (list || []).map((w) => `
          <div class="scan-window-row ${w.blocks ? "blocked" : ""}">
            <span>${escapeHtml(w.readable || "")}</span>
            <span class="scan-window-meta">limit ${escapeHtml(w.limit)}${w.paced ? " · paced by the gate" : ""}${w.blocks ? ` · defer ≈${escapeHtml(w.wait_seconds)} s` : ""}</span>
          </div>`).join("");
        return `<div class="scan-window-category">
          <p class="eyebrow">${escapeHtml(label)}</p>
          ${body}
          <div class="scan-window-note">This cycle needs ${escapeHtml((plan.requests_per_cycle || {})[category] ?? "—")} request(s) in this category.</div>
        </div>`;
      }).join("");
    }
  }

  const minInput = scanEl(market, "min");
  if (minInput && plan.cadence_sustainable_seconds !== undefined) {
    minInput.value = `${plan.cadence_sustainable_seconds} s (server-computed)`;
  }

  const startBtn = scanEl(market, "start");
  if (startBtn) {
    const running = card.starting || Boolean(snapshot.scan_running);
    startBtn.disabled = running || !plan.can_start;
    startBtn.textContent = running ? "Scan in progress…" : "Start read-only scan";
  }

  const verdict = scanEl(market, "verdict");
  if (verdict) {
    if (card.error) {
      verdict.className = "inline-status err top-space";
      verdict.innerHTML = `<strong>PROVIDER ERROR:</strong> scan capacity unavailable — ${escapeHtml(card.message || "server rejected the request")}`;
    } else {
      const tone = SCAN_STATUS_CLASS[plan.status] === "ok" ? "ok" : SCAN_STATUS_CLASS[plan.status] === "err" ? "err" : "warn";
      const reasons = (plan.reasons || []).map((r) => `<li>${escapeHtml(r)}</li>`).join("");
      verdict.className = `inline-status ${tone} top-space`;
      verdict.innerHTML = `
        <strong>${escapeHtml(plan.status || "LOADING")}</strong>
        ${plan.can_start ? "— this market can run one read-only cycle at the requested cadence." : "— a read-only scan cannot start as configured."}
        ${reasons ? `<ul class="scan-reasons">${reasons}</ul>` : ""}
        <p class="scan-caveat">Cadence floors come from the documented provider quota windows. They are not production latency, fill, or timing guarantees.</p>`;
    }
  }

  const estimate = scanEl(market, "estimate");
  if (estimate) {
    const lines = card.dispatch || [];
    estimate.innerHTML = lines.length
      ? `<p class="eyebrow">EXACT EXPECTED DISPATCH (SERVER-COMPUTED)</p><ul class="scan-reasons">${lines.map((l) => `<li>${escapeHtml(l)}</li>`).join("")}</ul>`
      : "";
  }

  renderScanResults(market);
}

function renderScanResults(market) {
  const card = scanCardState(market);
  const host = scanEl(market, "results");
  if (!host) return;
  const run = card.lastRun;
  if (!run) {
    host.innerHTML = `<div class="inline-status top-space">No cycle has run for ${escapeHtml(market)} in this process yet. A scan analyzes symbols; it does not open positions.</div>`;
    return;
  }
  const tone = run.state === "COMPLETED" ? "ok" : run.state === "PROVIDER ERROR" ? "err" : "warn";
  const calls = Object.entries(run.calls_by_category || {}).map(([k, v]) => `${escapeHtml(k)}: ${escapeHtml(v)}`).join(" · ") || "—";
  host.innerHTML = `
    <div class="inline-status ${tone} top-space">
      <strong>${escapeHtml(run.state)}</strong> · ${escapeHtml(run.market)} · run ${escapeHtml(run.run_id)}
      ${run.error ? `<br><strong>Error:</strong> ${escapeHtml(run.error)}` : ""}
    </div>
    <div class="scan-metrics">
      <div><span>Analysed</span><strong>${escapeHtml(run.symbols_analyzed)}</strong></div>
      <div><span>Fetched / stale</span><strong>${escapeHtml(run.symbols_selected)} / ${escapeHtml(run.symbols_stale)}</strong></div>
      <div><span>Failed / deferred</span><strong>${escapeHtml(run.symbols_failed)} / ${escapeHtml(run.symbols_deferred)}</strong></div>
      <div><span>Risk-approved candidates</span><strong>${escapeHtml(run.candidates_risk_approved)}</strong></div>
      <div><span>Orders</span><strong class="scan-zero">0</strong></div>
      <div><span>Elapsed vs cadence</span><strong>${escapeHtml((run.elapsed_ms / 1000).toFixed(2))} s / ${escapeHtml(run.cadence_requested_seconds)} s${run.cadence_met ? " · MET" : " · OVER"}</strong></div>
    </div>
    <p class="scan-caveat">ANALYZED = symbols with a completed decision. RISK-APPROVED CANDIDATES = decisions the shared allocator would allow; they are not orders. ORDERS stays 0 on this path.
    Calls by endpoint category: ${calls}.</p>`;
}

function renderScanHistory() {
  const rows = $("ens-scan-history-rows");
  const pill = $("ens-scan-history-pill");
  if (!rows) return;
  const all = [];
  for (const meta of SCAN_MARKETS) {
    const card = scanCardState(meta.key);
    for (const run of card.history || []) all.push(run);
  }
  all.sort((a, b) => String(b.started_at).localeCompare(String(a.started_at)));
  if (pill) pill.textContent = `${all.length} CYCLE${all.length === 1 ? "" : "S"}`;
  if (!all.length) {
    rows.innerHTML = '<tr><td colspan="9" class="empty-state">No read-only scans have run in this process yet.</td></tr>';
    return;
  }
  rows.innerHTML = all.slice(0, 20).map((run) => {
    const calls = Object.entries(run.calls_by_category || {}).map(([k, v]) => `${k} ${v}`).join("<br>") || "—";
    const stateClass = run.state === "COMPLETED" ? "ok" : run.state === "PROVIDER ERROR" ? "err" : "warn";
    return `<tr>
      <td class="mono">${escapeHtml(String(run.started_at || "").replace("T", " ").slice(0, 19))}</td>
      <td>${escapeHtml(run.market)}</td>
      <td><span class="status-pill ${stateClass}">${escapeHtml(run.state)}</span><div class="small mono">${escapeHtml(run.run_id)}</div></td>
      <td>${escapeHtml(run.symbols_selected)} / ${escapeHtml(run.symbols_analyzed)}</td>
      <td>${escapeHtml(run.symbols_stale)} / ${escapeHtml(run.symbols_failed)} / ${escapeHtml(run.symbols_deferred)}</td>
      <td>${escapeHtml(run.candidates_risk_approved)}</td>
      <td><span class="scan-zero">0</span></td>
      <td class="small">${calls}</td>
      <td>${escapeHtml((Number(run.elapsed_ms || 0) / 1000).toFixed(2))} s · ${run.cadence_met ? "MET" : "OVER"} ${escapeHtml(run.cadence_requested_seconds)} s</td>
    </tr>`;
  }).join("");
}

async function previewScanCapacity(market) {
  const card = scanCardState(market);
  if (card.previewing) return;
  card.previewing = true;
  const btn = scanEl(market, "preview");
  if (btn) { btn.disabled = true; btn.textContent = "Previewing…"; }
  const req = scanRequested(market);
  try {
    const res = await api("/api/ensemble/scan/preview", {
      body: { market, symbol_count: req.symbol_count, symbols: req.symbols, cadence_s: req.cadence_s },
    });
    card.plan = res.plan;
    card.quota = res.quota;
    card.limits = res.limits;
    card.dispatch = res.dispatch_plan || [];
    card.error = false;
  } catch (err) {
    card.error = true;
    card.message = err.message;
    toast("Capacity preview failed", err.message, "err");
  } finally {
    card.previewing = false;
    if (btn) { btn.disabled = false; btn.textContent = "Preview capacity (offline)"; }
    renderScanCard(market);
  }
}

async function startReadOnlyScan(market) {
  const card = scanCardState(market);
  if (card.starting) return;
  const req = scanRequested(market);
  card.starting = true;
  renderScanCard(market);
  try {
    const res = await api("/api/ensemble/scan/start", {
      body: { market, symbol_count: req.symbol_count, symbols: req.symbols, cadence_s: req.cadence_s },
    });
    if (res.started) {
      toast(`${market} read-only scan started`, `${res.requested_symbols} symbols · 0 orders · 0 broker calls`, "ok");
      card.dispatch = res.dispatch_plan || card.dispatch;
    } else {
      card.plan = res.plan || card.plan;
      toast(`${market} scan not started`, res.detail || res.status || "Refused by the server", "warn");
    }
  } catch (err) {
    toast(`${market} scan failed`, err.message, "err");
  } finally {
    card.starting = false;
    await loadScanCapacity();
    renderScanCard(market);
    const running = state.ens.scan?.data?.scan_status?.running;
    if (running && Object.keys(running).length) scheduleScanPoll();
  }
}

function bindScanEvents() {
  ensureScanCards();
  const host = $("ens-scan-cards");
  if (!host) return;
  host.addEventListener("click", (event) => {
    const button = event.target.closest("[data-scan-role]");
    if (!button) return;
    const card = button.closest("[data-scan-market]");
    const market = card?.dataset.scanMarket;
    if (!market) return;
    if (button.dataset.scanRole === "preview") previewScanCapacity(market);
    if (button.dataset.scanRole === "start") startReadOnlyScan(market);
  });
  let timer = null;
  const onEdit = (event) => {
    const field = event.target.closest("[data-scan-role]");
    const card = event.target.closest("[data-scan-market]");
    if (!field || !card) return;
    if (!["symbols", "cadence", "list", "watchlist"].includes(field.dataset.scanRole)) return;
    scanCardState(card.dataset.scanMarket).dirty = true;
    clearTimeout(timer);
    timer = setTimeout(() => previewScanCapacity(card.dataset.scanMarket), 450);
  };
  host.addEventListener("input", onEdit);
  host.addEventListener("change", onEdit);
}

/* =========================================================================
 * 00 · START & MONITOR — the primary operator screen
 *
 * One page for: mode -> markets + symbols -> credentials -> strategy and
 * Autopilot -> preview -> start -> monitor.
 *
 * Deliberate constraints, enforced below and asserted by tests:
 *  - every figure comes from a server payload (no hard-coded quota, cadence,
 *    risk limit or readiness conclusion);
 *  - selecting symbols never raises a position cap and never turns a scan into
 *    orders;
 *  - LIVE is only ever started through the existing sequence
 *    (mode -> fresh readiness -> arm -> acknowledge + exact phrase -> activate),
 *    and activating never starts a strategy;
 *  - starting a session is always a fresh server call: nothing about an active
 *    live session survives a restart;
 *  - secrets are posted in a body, cleared immediately, and never stored in
 *    browser storage or rendered back.
 * ====================================================================== */
const START_MARKETS = ["NSE", "NASDAQ"];
const START_LIVE_PHRASE_FALLBACK = "ACTIVATE LIVE REAL-MONEY TRADING";
const START_CLEAR_PHRASE = "CLEAR EMERGENCY STOP";

state.start = {
  data: null,
  loading: false,
  error: "",
  mode: "PAPER",
  selected: { NSE: [], NASDAQ: [] },
  strategy: { NSE: "manual_only", NASDAQ: "manual_only" },
  review: { NSE: "manual", NASDAQ: "manual" },
  search: { NSE: [], NASDAQ: [] },
  eligible: { NSE: 0, NASDAQ: 0 },
  preview: null,
  cadence: { NSE: null, NASDAQ: null },
  monitor: null,
  monitorError: "",
  busy: {},
  liveSteps: {},
  seeded: {},
  searchError: {},
  touched: {},
  timer: null,
};

const START_CADENCE_OPTIONS = [15, 30, 60, 90, 120, 300];

function startState() { return state.start; }

function startMarketData(market) {
  return (startState().data?.markets || {})[market] || null;
}

function startSelectionKey(market) {
  return startState().selected[market] || [];
}

function startIsSelected(market, key) {
  return startSelectionKey(market).includes(key);
}

function startSetSelection(market, keys) {
  const cap = Math.min(100, Number(startState().data?.max_symbols_per_scan || 100));
  const unique = [...new Set(keys)].slice(0, cap);
  startState().selected[market] = unique;
  startState().preview = null;
  renderStartSymbols(market);
  renderStartCadence();
  renderStartPreview();
  renderStartControls();
  startMarkFlowSteps();
}

function startStrategyOptions() {
  const rows = startState().data?.strategies || [];
  const autopilot = new Set(startState().data?.autopilot_strategies || []);
  return rows.map((row) => ({
    name: String(row.name),
    label: autopilot.has(String(row.name))
      ? `${row.name} · can act without a prompt once started`
      : `${row.name} · manual tickets only`,
    autopilotCapable: autopilot.has(String(row.name)),
  }));
}

function startRenderStrategySelects() {
  const options = startStrategyOptions();
  for (const market of START_MARKETS) {
    const select = $(`start-strategy-${market}`);
    if (!select) continue;
    const current = startState().strategy[market];
    select.innerHTML = options.map((row) =>
      `<option value="${escapeHtml(row.name)}"${row.name === current ? " selected" : ""}>${escapeHtml(row.label)}</option>`).join("");
    if (!options.some((row) => row.name === current) && options.length) {
      startState().strategy[market] = options[0].name;
      select.value = options[0].name;
    }
  }
}

/* ---------------- load + render ---------------- */
async function loadStartMonitor({ background = false } = {}) {
  const st = startState();
  if (st.loading) return;
  st.loading = true;
  if (!background) renderStart();
  try {
    st.data = await api("/api/markets/start-monitor");
    st.error = "";
    // A saved session's symbols seed the picker only once, so the operator can
    // deselect without the panel re-selecting on every refresh.
    for (const market of START_MARKETS) {
      if (st.seeded?.[market]) continue;
      st.seeded = { ...(st.seeded || {}), [market]: true };
      const saved = startMarketData(market)?.session_symbols || [];
      if (saved.length && !startSelectionKey(market).length) {
        st.selected[market] = saved.slice(0, 100);
      }
    }
    startRenderStrategySelects();
    renderStart();
    await Promise.all([loadStartSearch("NSE", ""), loadStartSearch("NASDAQ", "")]);
  } catch (error) {
    st.error = error.message || "start & monitor overview unavailable";
    renderStart();
  } finally {
    st.loading = false;
  }
}

function startFmtTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

function startPillKind(badge) {
  if (badge === "PAPER") return "ok";
  if (badge === "LIVE · ACTIVE") return "err";
  if (badge === "LIVE · ARMED") return "warn";
  if (badge === "ERROR") return "err";
  return "info";
}

function renderStart() {
  const st = startState();
  const data = st.data;
  if (!data) {
    if (st.error) {
      const host = $("start-preview-body");
      if (host) host.innerHTML = `<div class="inline-status err">Start &amp; Monitor could not read the server: ${escapeHtml(st.error)}</div>`;
    }
    return;
  }
  renderStartHeader();
  renderStartMode();
  for (const market of START_MARKETS) {
    renderStartCard(market);
    renderStartSymbols(market);
    renderStartCredentials(market);
    renderStartAutopilot(market);
  }
  renderStartCadence();
  renderStartPreview();
  renderStartControls();
}

function renderStartHeader() {
  const data = startState().data;
  const pill = $("start-evidence-pill");
  if (pill && data) {
    pill.textContent = data.evidence?.real_money_enabled
      ? "LIVE GATE ACTIVE ON AT LEAST ONE MARKET · REAL MONEY"
      : "READ-ONLY VIEW · 0 BROKER ORDERS FROM THIS PAGE";
    pill.className = `status-pill ${data.evidence?.real_money_enabled ? "err" : "info"}`;
  }
  for (const market of START_MARKETS) {
    const row = startMarketData(market);
    const badge = row?.badge || "ERROR";
    const card = $(`start-badge-${market}`);
    if (card) { card.textContent = badge; card.className = `status-pill ${startPillKind(badge)}`; }
    const bar = $(`start-badge-${market}-bar`);
    if (bar) { bar.textContent = `${market} · ${badge}`; bar.className = `status-pill ${startPillKind(badge)}`; }
  }
}

function renderStartMode() {
  const st = startState();
  const mode = st.mode;
  for (const [id, value] of [["start-mode-paper", "PAPER"], ["start-mode-live", "LIVE"]]) {
    const button = $(id);
    if (!button) continue;
    const active = mode === value;
    button.classList.toggle("active", active);
    button.setAttribute("aria-checked", active ? "true" : "false");
  }
  const badge = $("start-mode-badge");
  if (badge) { badge.textContent = mode; badge.className = `status-pill ${mode === "LIVE" ? "err" : "ok"}`; }
  const detail = $("start-mode-detail");
  if (detail) {
    detail.className = `inline-status top-space ${mode === "LIVE" ? "warn" : ""}`;
    detail.innerHTML = mode === "LIVE"
      ? "LIVE selected. This changes <strong>nothing</strong> by itself: each market needs its own readiness read, Arm and typed phrase, and activating a gate never starts a strategy."
      : "PAPER selected. Fills are simulated in the local ledger from read-only market data; no broker order is ever routed.";
  }
  startMarkFlowSteps();
}

/* The flow strip must never claim a step is done before it actually is. Each
 * step is satisfied by observable state, not by which mode button was clicked. */
function startCompletedSteps() {
  const st = startState();
  const done = new Set();
  const touched = st.touched || {};
  const live = (market) => (startMarketData(market)?.paper?.session_state || "STOPPED") !== "STOPPED"
    || (startMarketData(market)?.live?.gate_state || "DISABLED") !== "DISABLED";
  const chosen = START_MARKETS.filter((m) => startSelectionKey(m).length > 0);
  const anySession = START_MARKETS.some(live);
  if (touched.mode || anySession) done.add(1);
  if (chosen.length) done.add(2);
  // Step 3 is credentials: it can only be done once a market is actually chosen
  // and that market reports a present read credential.
  if (chosen.length && chosen.every((m) => startMarketData(m)?.quota?.credential_present === true)) done.add(3);
  if (touched.review || anySession) done.add(4);
  if (st.preview) done.add(5);
  if (anySession) done.add(6);
  if ((st.monitor || []).length > 0) done.add(7);
  return done;
}

function startMarkFlowSteps() {
  const done = startCompletedSteps();
  document.querySelectorAll("[data-start-step]").forEach((node) => {
    const step = Number(node.dataset.startStep);
    node.classList.toggle("done", done.has(step));
    node.classList.toggle("pending", !done.has(step));
  });
}

function renderStartCard(market) {
  const row = startMarketData(market);
  const facts = $(`start-facts-${market}`);
  const blockers = $(`start-blockers-${market}`);
  const exitNote = $(`start-exit-${market}`);
  if (!facts) return;
  if (!row) {
    facts.innerHTML = `<p class="empty-state">Server state unavailable for ${escapeHtml(market)}.</p>`;
    if (blockers) blockers.innerHTML = "";
    return;
  }
  const creds = row.credentials?.rows || [];
  const dataCred = creds.find((c) => c.slot.includes("MARKET DATA"));
  const limits = row.limits || {};
  const quoteCap = limits.max_quote_age_seconds;
  facts.innerHTML = `
    <dl class="detail-list start-detail">
      <dt>Include in session</dt>
      <dd>${startIsSelected(market) ? "YES · selected below" : "NO · not started"}</dd>
      <dt>Market-data credential</dt>
      <dd>${dataCred?.present ? `PRESENT · ${escapeHtml(dataCred.detail)}` : "NOT CONFIGURED"}</dd>
      <dt>Connection / preflight</dt>
      <dd>${escapeHtml(row.readiness?.overall_status || "UNKNOWN")}${row.readiness?.owner_connection_verified ? " · owner-verified" : " · not owner-verified"}</dd>
      <dt>Selected symbols</dt>
      <dd>${fmt(row.selected_symbol_count || 0, 0)} of ${fmt(row.symbol_cap, 0)} cap</dd>
      <dt>Supported scan cadence</dt>
      <dd class="${row.cadence?.supportable ? "" : "start-warn-text"}">${escapeHtml(row.cadence?.label || "Cadence unavailable: no quota plan for this market.")}</dd>
      <dt>Quota plan</dt>
      <dd>${escapeHtml(row.quota?.profile || "none")} · ${escapeHtml(row.quota?.verification || "UNKNOWN")}</dd>
      <dt>Execution gate</dt>
      <dd>${escapeHtml(row.live?.mode || "PAPER")} · ${escapeHtml(row.live?.gate_state || "DISABLED")}${row.live?.kill_engaged ? " · KILL SWITCH ENGAGED" : ""}</dd>
      <dt>Position count / cap</dt>
      <dd>${fmt(row.paper?.positions ?? 0, 0)} open / ${fmt(row.position_cap ?? "—", 0)} cap</dd>
      <dt>Daily orders</dt>
      <dd>${fmt(row.paper?.working_orders ?? 0, 0)} working · ${fmt(row.daily_order_cap ?? "—", 0)} per day cap</dd>
      <dt>Quote age cap</dt>
      <dd>${quoteCap ? `${fmt(quoteCap, 0)} s · stale quotes are refused` : "not reported"}</dd>
      <dt>Session state</dt>
      <dd>${escapeHtml(row.paper?.session_state || "UNAVAILABLE")} · ${escapeHtml(row.paper?.strategy || "no strategy")}</dd>
    </dl>`;
  if (blockers) {
    const list = [];
    // The server sends the union once, tagged with the modes each fact blocks.
    for (const gap of (row.autopilot?.blockers || [])) {
      const scope = (gap.blocks || []).join(" / ");
      list.push(`<li><code>${escapeHtml(gap.code)}</code>${scope ? ` <span class="small muted">(${escapeHtml(scope)})</span>` : ""} — ${escapeHtml(gap.text)}</li>`);
    }
    for (const caution of (row.autopilot?.cautions || [])) {
      list.push(`<li><code>${escapeHtml(caution.code)}</code> <span class="small muted">(CAUTION)</span> — ${escapeHtml(caution.text)}</li>`);
    }
    for (const item of (row.readiness?.blockers || [])) {
      list.push(`<li><code>READINESS</code> — ${escapeHtml(String(item))}</li>`);
    }
    blockers.innerHTML = list.length
      ? `<p class="eyebrow top-space">${startIsSelected(market) ? "BLOCKERS" : "BLOCKERS (WHEN THIS MARKET IS SELECTED)"}</p><ul class="switch-blockers">${list.join("")}</ul>`
      : `<p class="small muted top-space">No blocking requirement reported for this market.</p>`;
  }
  if (exitNote) {
    const route = row.live?.exit_route || {};
    exitNote.innerHTML = route.routed
      ? `<strong>Exit route:</strong> ${escapeHtml(route.summary || "routed")}`
      : `<strong>NO ROUTED EXIT — AXIOM cannot close an ${escapeHtml(market)} position.</strong>
         ${escapeHtml(route.summary || "")}
         Closing path: ${escapeHtml((route.closing_paths || []).join(" · ") || "none documented")}
         Operator fallback: ${escapeHtml(route.operator_fallback || "close manually, then reconcile")}`;
  }
}

function renderStartCredentials() {
  const host = $("start-cred-table");
  if (!host) return;
  const rows = [];
  for (const market of START_MARKETS) {
    const row = startMarketData(market);
    for (const cred of (row?.credentials?.rows || [])) {
      rows.push({ ...cred, market });
    }
  }
  if (!rows.length) { host.innerHTML = `<p class="empty-state">No credential slots reported.</p>`; return; }
  host.innerHTML = `
    <div class="table-wrap">
      <table>
        <thead><tr><th>Market</th><th>Slot</th><th>Scope</th><th>Local state</th><th>Detail</th></tr></thead>
        <tbody>
          ${rows.map((row) => `
            <tr>
              <td>${escapeHtml(row.market)}</td>
              <td><strong>${escapeHtml(row.slot)}</strong></td>
              <td class="small muted">${escapeHtml(row.scope)}</td>
              <td>${row.present === null
                ? `<span class="status-pill info">THIS TAB ONLY</span>`
                : row.present
                  ? `<span class="status-pill ok">PRESENT</span>`
                  : `<span class="status-pill warn">NOT CONFIGURED</span>`}</td>
              <td class="small">${escapeHtml(row.detail)}${row.affects_execution ? ` <strong>· order credential</strong>` : ""}</td>
            </tr>`).join("")}
        </tbody>
      </table>
    </div>`;
}

function renderStartAutopilot(market) {
  const row = startMarketData(market);
  const note = $(`start-autopilot-${market}`);
  if (!note || !row) return;
  const review = startState().review[market];
  const ok = Boolean(row.autopilot?.paper?.available);
  const liveOk = Boolean(row.autopilot?.live?.available);
  // The server sends one blocker union plus per-mode gap lists. Read the PAPER
  // list, never a top-level "gaps" key that does not exist: an absent list must
  // not be read as "no blockers".
  const gaps = row.autopilot?.paper?.gaps || [];
  note.className = `small ${review === "autopilot" && (gaps.length || !ok) ? "start-warn-text" : "muted"}`;
  note.innerHTML = review === "autopilot"
    ? (!ok
      ? `<strong>Autopilot unavailable on ${escapeHtml(market)}:</strong> ${gaps.length ? gaps.map((g) => escapeHtml(g.code)).join(", ") : "the server reports no usable automation state"}. Manual review stays available.`
      : `Autopilot available for PAPER on ${escapeHtml(market)}; LIVE Autopilot ${liveOk ? "is available while the gate is ACTIVE" : "remains disabled"}.`)
    : "Manual review: every simulated order needs an explicit ticket. No automation runs.";
  const may = $("start-autopilot-may");
  if (may && row.autopilot?.may) may.innerHTML = row.autopilot.may.map((t) => `<li>${escapeHtml(t)}</li>`).join("");
  const mayNot = $("start-autopilot-may-not");
  if (mayNot && row.autopilot?.may_not) mayNot.innerHTML = row.autopilot.may_not.map((t) => `<li>${escapeHtml(t)}</li>`).join("");
  const pill = $("start-autopilot-pill");
  if (pill) {
    const anyAuto = START_MARKETS.some((m) => startState().review[m] === "autopilot");
    const running = START_MARKETS.some((m) => (startMarketData(m)?.paper?.session_state || "STOPPED") !== "STOPPED");
    pill.textContent = anyAuto
      ? (running ? "AUTOPILOT SELECTED · SESSION STATE BELOW" : "AUTOPILOT SELECTED · NOTHING RUNNING")
      : "MANUAL REVIEW ON BOTH MARKETS";
    pill.className = `status-pill ${anyAuto ? "warn" : "info"}`;
  }
  const exceptions = $("start-autopilot-exceptions");
  if (exceptions) {
    const cycle = row.cycle_missed || {};
    const stale = row.stale_policy || {};
    exceptions.innerHTML = `
      <strong>When it stops instead of improvising:</strong>
      <ul class="scan-reasons">
        <li>Stale quote — ${escapeHtml(stale.behavior || "quotes older than the server cap are refused")}</li>
        <li>Cadence missed — ${escapeHtml(cycle.behavior || "deferred, never sent late")}</li>
        <li>Provider throttle — the gate cools down and lowers its per-second budget; it never rotates keys or endpoints.</li>
        <li>Ambiguous order outcome — the market is blocked and must be reconciled; nothing retries blindly.</li>
        <li>Kill switch — engaged markets stop entering and report <code>ERROR</code> in the header badge.</li>
      </ul>`;
  }
}

/* ---------------- symbols ---------------- */
async function loadStartSearch(market, query) {
  const st = startState();
  try {
    const params = new URLSearchParams(
      market === "NSE"
        ? { market: "NSE", query, limit: "30", include_indices: "false" }
        : { market: "NASDAQ", query, limit: "30" });
    const payload = await api(`/api/markets/search?${params.toString()}`);
    st.search[market] = (payload.items || []).filter((item) => startEligible(market, item));
    st.eligible[market] = Number(
      (market === "NSE" ? payload.nse?.total_matches : payload.nasdaq?.total_matches) || 0);
    st.searchError = st.searchError || {};
    st.searchError[market] = "";
  } catch (error) {
    st.search[market] = [];
    st.searchError = { ...(st.searchError || {}), [market]: error.message };
  }
  renderStartSymbols(market);
  renderStartEligibility(market);
}

function startEligible(market, item) {
  // "Select all eligible" must never quietly include a watch-only instrument:
  // indices, derivatives and suspended symbols are filtered out here, and the
  // selection is re-validated by the server when the session starts.
  if (!item) return false;
  if (market === "NSE") {
    if (String(item.segment || "").toUpperCase() !== "NSE_EQ") return false;
    if (String(item.asset_category || "CASH_EQUITY_OR_ETF") !== "CASH_EQUITY_OR_ETF") return false;
    if (!String(item.instrument_key || "").startsWith("NSE_EQ|")) return false;
    return item.tradable !== false;
  }
  if (String(item.asset_class || "us_equity").toLowerCase() !== "us_equity") return false;
  if (String(item.status || "active").toLowerCase() !== "active") return false;
  if (item.tradable === false) return false;
  return Boolean(String(item.symbol || "").trim());
}

function startSymbolKey(market, item) {
  return market === "NSE"
    ? String(item.instrument_key || item.trading_symbol || item.symbol || "")
    : String(item.symbol || "");
}

function startSymbolLabel(market, item) {
  return market === "NSE"
    ? String(item.trading_symbol || item.display_symbol || item.symbol || "")
    : String(item.symbol || "");
}

function renderStartSymbols(market) {
  const st = startState();
  const results = $(`start-search-results-${market}`);
  const chips = $(`start-chips-${market}`);
  const count = $(`start-symbol-count-${market}`);
  const selected = startSelectionKey(market);
  if (count) count.textContent = `${selected.length} SELECTED`;
  if (results) {
    const items = st.search[market] || [];
    const error = (st.searchError || {})[market];
    results.innerHTML = error
      ? `<div class="inline-status err">Catalog search failed: ${escapeHtml(error)}</div>`
      : items.length
        ? `<div class="start-result-list">${items.map((item) => {
          const key = startSymbolKey(market, item);
          const checked = selected.includes(key);
          return `<label class="start-result">
            <input type="checkbox" data-start-symbol="${escapeHtml(key)}" data-start-symbol-market="${market}" ${checked ? "checked" : ""}>
            <span><strong>${escapeHtml(startSymbolLabel(market, item))}</strong>
              <small>${escapeHtml(item.name || "")} · ${escapeHtml(item.asset_subclass || item.instrument_type || "")}</small></span>
          </label>`;
        }).join("")}</div>`
        : `<p class="empty-state">No eligible ${escapeHtml(market)} instruments matched. Search the catalog or add symbols on 02 Watchlists.</p>`;
  }
  if (chips) {
    chips.innerHTML = selected.length
      ? selected.map((key) => `<span class="start-chip" role="listitem">${escapeHtml(key)}<button type="button" class="text-button" data-start-remove="${escapeHtml(key)}" data-start-remove-market="${market}" aria-label="Remove ${escapeHtml(key)}">×</button></span>`).join("")
      : `<p class="small muted">No symbols selected yet.</p>`;
  }
}

function renderStartEligibility(market) {
  const note = $(`start-eligible-note-${market}`);
  const button = $(`start-select-all-${market}`);
  const eligible = startState().eligible[market] || 0;
  if (note) {
    note.textContent = market === "NSE"
      ? `Eligible = NSE cash equities/ETFs with a resolved instrument key (${fmt(eligible, 0)} catalog matches). Indices, derivatives and suspended series are never included.`
      : `Eligible = active, tradable US equities/ETFs (${fmt(eligible, 0)} catalog matches). Nothing unsupported is silently added.`;
  }
  if (button) {
    button.textContent = `Select all eligible (${fmt(eligible, 0)})`;
    button.disabled = !eligible;
  }
}

async function startSelectAllEligible(market) {
  // One bounded catalog read, filtered by exactly the same eligibility rule the
  // picker uses, truncated at the scan cap. Never grows a position cap, never
  // includes a watch-only instrument, and adds nothing the server would reject.
  const cap = Math.min(100, Number(startState().data?.max_symbols_per_scan || 100));
  const params = new URLSearchParams(market === "NSE"
    ? { market: "NSE", query: "", limit: String(Math.min(200, cap * 2)), include_indices: "false" }
    : { market: "NASDAQ", query: "", limit: String(Math.min(200, cap * 2)) });
  let payload;
  try { payload = await api(`/api/markets/search?${params.toString()}`); }
  catch (error) { toast("Select all failed", error.message, "err"); return; }
  const collected = [];
  for (const item of (payload.items || [])) {
    if (!startEligible(market, item)) continue;
    const key = startSymbolKey(market, item);
    if (key && !collected.includes(key)) collected.push(key);
    if (collected.length >= cap) break;
  }
  startSetSelection(market, collected);
  const note = collected.length >= cap
    ? `Stopped at the ${cap}-symbol scan cap; the catalog has more eligible symbols.`
    : `Every eligible symbol in the returned catalog page was selected.`;
  toast("Select all eligible", `${collected.length} ${market} symbols selected. ${note} Selection never changes a position cap.`, "ok");
}

/* ---------------- cadence ---------------- */
function renderStartCadence() {
  const host = $("start-cadence-line");
  if (!host) return;
  const parts = [];
  for (const market of START_MARKETS) {
    const row = startMarketData(market);
    const count = startSelectionKey(market).length;
    if (!row) continue;
    const plan = row.cadence || {};
    parts.push(`<strong>${escapeHtml(market)}</strong>: ${fmt(count, 0)} selected · ` +
      `${escapeHtml(plan.label || "cadence unavailable: no quota plan")} · ` +
      `${plan.requests_per_cycle && Object.keys(plan.requests_per_cycle).length
        ? Object.entries(plan.requests_per_cycle).map(([k, v]) => `${escapeHtml(k)} ${fmt(v, 0)}`).join(", ")
        : "requests not computed"}`);
  }
  host.className = "inline-status top-space";
  host.innerHTML = parts.length
    ? `${parts.join(" &nbsp;|&nbsp; ")}<br><span class="small muted">These are server-computed from documented provider quota windows (MODELED OFFLINE). A scan buys market data; it does not buy a trade.</span>`
    : "Sustainable cadence appears here from the server's quota plan for the selected count.";
}

function startCadenceFor(market) {
  const row = startMarketData(market);
  const min = Number(row?.cadence?.sustainable_seconds || 0);
  const options = START_CADENCE_OPTIONS.filter((value) => value >= Math.max(1, Math.ceil(min)));
  return options.length ? options : [Math.max(1, Math.ceil(min || 60))];
}

/* ---------------- preview + start ---------------- */
async function startPreviewSession() {
  const st = startState();
  const selected = START_MARKETS.filter((m) => startSelectionKey(m).length);
  const host = $("start-preview-body");
  if (!selected.length) {
    if (host) host.innerHTML = `<div class="inline-status warn">Select at least one market and one symbol before previewing.</div>`;
    return null;
  }
  const rows = [];
  for (const market of selected) {
    const row = startMarketData(market);
    const count = startSelectionKey(market).length;
    let preview = null;
    let error = "";
    try {
      preview = await api("/api/ensemble/scan/preview", {
        body: { market, symbol_count: count, cadence_s: startCadenceFor(market)[0] },
      });
    } catch (err) { error = err.message; }
    rows.push({ market, row, count, preview, error, strategy: st.strategy[market], review: st.review[market] });
  }
  st.preview = { at: new Date().toISOString(), rows };
  renderStartPreview();
  renderStartControls();
  startMarkFlowSteps();
  return st.preview;
}

/* One comparison of two numbers the capacity service returned. No arithmetic
 * that could imply a cadence the provider does not support. */
function startCadenceVerdict(plan) {
  const requested = Number(plan?.cadence_requested_seconds);
  const sustainable = Number(plan?.cadence_sustainable_seconds);
  if (!Number.isFinite(requested) || !Number.isFinite(sustainable)) {
    return "Cadence not computed by the capacity service.";
  }
  return sustainable > requested
    ? `${fmt(requested, 0)} s requested · NOT SUPPORTED for this selection: the provider quota needs ${fmt(sustainable, 1)} s per cycle.`
    : `${fmt(requested, 0)} s requested · the provider quota needs ${fmt(sustainable, 1)} s per cycle.`;
}

function renderStartPreview() {
  const host = $("start-preview-body");
  const pill = $("start-preview-pill");
  const preview = startState().preview;
  if (!host) return;
  if (!preview) {
    host.innerHTML = `<p class="empty-state">Select a market, symbols, strategy and review mode, then press Preview session. Preview is offline: it consumes no provider quota.</p>`;
    if (pill) { pill.textContent = "NOT PREVIEWED"; pill.className = "status-pill info"; }
    return;
  }
  const blocks = preview.rows.map(({ market, row, count, preview: payload, error, strategy, review }) => {
    if (error) return `<div class="inline-status err"><strong>${escapeHtml(market)}</strong> preview failed: ${escapeHtml(error)}</div>`;
    const plan = payload?.plan || {};
    const windows = Object.entries(plan.window_usage || {});
    const limits = row?.limits || {};
    const setup = preview?.setup || payload?.summary || {};
    return `
      <div class="start-preview-card inset-panel">
        <div class="panel-head">
          <div><p class="eyebrow">PREVIEW · ${escapeHtml(market)} · OFFLINE · 0 PROVIDER REQUESTS</p>
            <h4>${escapeHtml(row?.venue_label || market)}</h4></div>
          <span class="status-pill ${plan.can_start ? "ok" : "warn"}">${escapeHtml(plan.status || "UNKNOWN")}</span>
        </div>
        <dl class="detail-list start-detail">
          <dt>Mode</dt><dd>${escapeHtml(startState().mode)} · ${review === "autopilot" ? "AUTOPILOT" : "MANUAL REVIEW"}</dd>
          <dt>Broker</dt><dd>${escapeHtml(row?.broker || market)}</dd>
          <dt>Symbols</dt><dd>${fmt(count, 0)} selected of ${fmt(row?.symbol_cap ?? 100, 0)} cap</dd>
          <dt>Strategy</dt><dd>${escapeHtml(strategy)}</dd>
          <dt>Scan cadence</dt><dd>${escapeHtml(startCadenceVerdict(plan))}</dd>
          <dt>Provider quota</dt><dd>${escapeHtml(row?.quota?.profile || "none")} · ${escapeHtml(row?.quota?.verification || "UNKNOWN")} · ${fmt(plan.total_requests_per_cycle ?? 0, 0)} requests / cycle</dd>
          <dt>Open positions</dt><dd>${fmt(row?.paper?.positions ?? 0, 0)} now · ${fmt(row?.position_cap ?? "—", 0)} cap (unchanged)</dd>
          <dt>Order / exposure caps</dt><dd>${fmt(limits.max_order_value ?? "—", 0)} per order · ${fmt(limits.max_symbol_exposure ?? "—", 0)} per symbol · ${fmt(limits.max_total_exposure ?? "—", 0)} total · ${fmt(limits.max_daily_orders ?? "—", 0)} orders/day</dd>
          <dt>Daily loss cap</dt><dd>${fmt(limits.max_daily_loss ?? "—", 0)}</dd>
          <dt>Quote age cap</dt><dd>${fmt(limits.max_quote_age_seconds ?? "—", 0)} s</dd>
          <dt>Orders from this preview</dt><dd><strong>0</strong></dd>
        </dl>
        ${setupsSummary(setup)}
        ${windows.length ? `<p class="eyebrow top-space">QUOTA WINDOWS</p><ul class="scan-reasons">${windows.map(([category, usage]) => {
          const rows = Array.isArray(usage) ? usage : [];
          return rows.map((w) => `<li>${escapeHtml(category)} · ${escapeHtml(w.readable || `${w.current} / ${w.limit}`)}${w.blocks ? " · BLOCKS" : ""}</li>`).join("");
        }).join("")}</ul>` : ""}
        ${(plan.reasons || []).length ? `<ul class="scan-reasons top-space">${plan.reasons.map((r) => `<li>${escapeHtml(r)}</li>`).join("")}</ul>` : ""}
      </div>`;
  });
  host.innerHTML = blocks.join("");
  if (pill) {
    const anyBlocked = preview.rows.some((row) => !(row.preview?.plan?.can_start));
    pill.textContent = anyBlocked ? "PREVIEWED · AT LEAST ONE MARKET BLOCKED" : "PREVIEWED · OFFLINE · NO QUOTA USED";
    pill.className = `status-pill ${anyBlocked ? "warn" : "ok"}`;
  }
}

function setupsSummary(setup) {
  if (!setup || !Object.keys(setup).length) return "";
  return `<p class="small muted top-space">Server preview summary: ${escapeHtml(JSON.stringify(setup).slice(0, 600))}</p>`;
}

function startOnMarket(market) {
  return Boolean($(`start-select-${market}`)?.checked);
}

function renderStartControls() {
  const st = startState();
  const data = st.data;
  const selected = START_MARKETS.filter(startOnMarket);
  const busy = Boolean(st.busy.paper || st.busy.live);
  const paperButton = $("start-paper-btn");
  if (paperButton) {
    const anySymbols = selected.some((m) => startSelectionKey(m).length);
    paperButton.disabled = busy || !selected.length || !anySymbols;
    paperButton.title = !selected.length ? "Select at least one market card"
      : !anySymbols ? "Select at least one symbol for a selected market" : "";
  }
  for (const market of START_MARKETS) {
    const button = $(`start-live-${market}-btn`);
    if (!button) continue;
    const row = startMarketData(market);
    const mode = String(row?.live?.mode || "PAPER").toUpperCase();
    const ready = Boolean(row?.live?.ready);
    // LIVE Start is only reachable through the guided sequence; this button
    // opens it. It is never enabled by merely selecting LIVE above.
    const hasSymbols = startSelectionKey(market).length > 0;
    button.disabled = busy || !startOnMarket(market) || !hasSymbols;
    button.textContent = mode === "LIVE"
      ? `LIVE ${market} · ${row?.live?.gate_state || "DISABLED"}`
      : `Start LIVE · ${market}`;
    button.title = !startOnMarket(market) ? `Tick the ${market} card first` : (ready ? "" : "Opens the guided LIVE sequence; readiness gates stay server-side");
  }
  const stop = $("start-stop-btn");
  if (stop) stop.disabled = busy || !data;
  const note = $("start-control-note");
  if (note && data) {
    const running = START_MARKETS.filter((m) => (startMarketData(m)?.paper?.session_state || "STOPPED") !== "STOPPED");
    const live = START_MARKETS.filter((m) => startMarketData(m)?.live?.gate_state === "ACTIVE");
    note.textContent = [
      running.length ? `PAPER running: ${running.join(", ")}` : "No PAPER session running",
      live.length ? `LIVE gate ACTIVE: ${live.join(", ")}` : "no LIVE gate active",
      selected.length ? `selected: ${selected.join(", ")}` : "no market selected",
    ].join(" · ");
  }
  const previewButton = $("start-preview-btn");
  if (previewButton) previewButton.disabled = busy || !selected.length;
}

async function startPaperSession() {
  const st = startState();
  const markets = START_MARKETS.filter(startOnMarket).filter((m) => startSelectionKey(m).length);
  if (!markets.length) { toast("Nothing to start", "Select a market and at least one symbol.", "warn"); return; }
  const confirm = await startConfirm(
    "Start PAPER session",
    `<p>Start a <strong>simulated</strong> PAPER session for <strong>${escapeHtml(markets.join(", "))}</strong>?</p>
     <ul class="checklist">
       <li>Fills are simulated in the local ledger. No broker order is routed.</li>
       <li>Automation runs only for strategies marked capable, and only inside the server's caps.</li>
       <li>The session stops on Stop, Emergency Stop, or restart.</li>
     </ul>`,
    "Start PAPER", "bull");
  if (!confirm) return;
  st.busy.paper = true; renderStartControls();
  const results = [];
  try {
    for (const market of markets) {
      const symbols = startSelectionKey(market);
      const autopilot = st.review[market] === "autopilot";
      const payload = {
        market,
        strategy: autopilot ? st.strategy[market] : "manual_only",
        timeframe: "5m",
        symbols,
      };
      try {
        const out = await api("/api/markets/paper/session/start", { body: payload });
        results.push(`${market}: ${out.session_state || out.state || "STARTED"}`);
      } catch (error) {
        results.push(`${market}: FAILED — ${error.message}`);
      }
    }
    toast("PAPER session", results.join(" · "), "ok");
  } finally {
    st.busy.paper = false;
    await loadStartMonitor({ background: true });
    loadStartMonitorRows();
  }
}

async function startStopSession() {
  const st = startState();
  const running = START_MARKETS.filter((m) => (startMarketData(m)?.paper?.session_state || "STOPPED") !== "STOPPED");
  const confirm = await startConfirm(
    "Stop session",
    `<p>Stop the local paper session for <strong>${escapeHtml(running.join(", ") || "no market")}</strong> and disarm any LIVE gate?</p>
     <p class="small muted">Stopping never cancels broker orders by itself and leaves no live session running.</p>`,
    "Stop session", "danger");
  if (!confirm) return;
  st.busy.stop = true; renderStartControls();
  const out = [];
  try {
    for (const market of START_MARKETS) {
      const row = startMarketData(market);
      if ((row?.paper?.session_state || "STOPPED") !== "STOPPED") {
        try {
          await api("/api/markets/paper/session/transition", { body: { market, action: "stop", reason: "operator stop from Start & Monitor" } });
          out.push(`${market} paper stopped`);
        } catch (error) { out.push(`${market} paper: ${error.message}`); }
      }
      if ((row?.live?.mode || "") === "LIVE") {
        try {
          await api(`/api/markets/live/${market}/disarm`, { body: { reason: "operator stop from Start & Monitor" } });
          out.push(`${market} live disarmed`);
        } catch (error) { out.push(`${market} live: ${error.message}`); }
      }
    }
    toast("Stop session", out.join(" · ") || "Nothing was running.", "warn");
  } finally {
    st.busy.stop = false;
    await loadStartMonitor({ background: true });
  }
}

async function startEmergencyStop() {
  const confirm = await startConfirm(
    "EMERGENCY STOP",
    `<p><strong>This halts every subsystem AXIOM can halt.</strong> It engages the local paper halt, stops Kronos sessions, stops any Upstox autonomy session, and stops the Alpaca broker path.</p>
     <ul class="checklist">
       <li>It does not silently cancel broker orders unless a subsystem implements that.</li>
       <li>Clearing it needs a separate typed confirmation (<code>${escapeHtml(START_CLEAR_PHRASE)}</code>).</li>
     </ul>`,
    "Engage emergency stop", "danger");
  if (!confirm) return;
  try {
    await api(ROUTES.emergencyStop, { body: { reason: "operator emergency stop from Start & Monitor" } });
    toast("Emergency stop engaged", "Every reachable subsystem was told to halt. Live execution stays off.", "err");
  } catch (error) {
    toast("Emergency stop failed", error.message, "err");
  }
  await loadStartMonitor({ background: true });
}

/* ---------------- LIVE: the existing sequence, step by step ---------------- */
async function startOpenLiveSequence(market) {
  const st = startState();
  const row = startMarketData(market);
  const phrase = row?.live?.confirmation_phrase || START_LIVE_PHRASE_FALLBACK;
  const body = `
    <p>The server gates this market through the existing four steps. Each one is a separate call; none is combined, skipped or auto-acknowledged.</p>
    <ol class="checklist">
      <li><strong>Read readiness</strong> — GET only, no broker order.</li>
      <li><strong>Set mode LIVE</strong> — the gate stays DISARMED.</li>
      <li><strong>Arm</strong> — the server re-checks everything first.</li>
      <li><strong>Activate</strong> — requires an explicit risk acknowledgement plus the exact phrase.</li>
    </ol>
    <p class="small muted">Activating a gate does <strong>not</strong> start or resume a strategy. Starting the session is a separate, explicit action, and nothing survives a restart.</p>
    <div id="start-live-steps" class="start-live-steps"></div>`;
  openDialog({
    title: `LIVE sequence · ${market}`,
    body,
    confirmLabel: "Close",
    confirmClass: "secondary",
    onConfirm: async () => { closeDialog(); return true; },
  });
  renderStartLiveSteps(market, phrase);
}

function renderStartLiveSteps(market, phrase) {
  const host = $("start-live-steps");
  if (!host) return;
  const st = startState();
  const row = startMarketData(market);
  const mode = String(row?.live?.mode || "PAPER").toUpperCase();
  const gate = String(row?.live?.gate_state || "DISABLED").toUpperCase();
  const ready = Boolean(row?.live?.ready);
  const failed = row?.live?.failed_checks || [];
  const step = st.liveSteps[market] || {};
  const busy = Boolean(st.busy.live);
  host.innerHTML = `
    <div class="switch-step ${row ? "done" : "pending"}">
      <span class="step-index">1</span>
      <span class="step-text"><b>Read readiness</b> — ${ready ? "no blocking check" : `${failed.length} failing check(s)`}</span>
      <button type="button" class="button secondary xs" data-start-live-step="readiness" data-start-market="${market}" ${busy ? "disabled" : ""}>Read readiness (GET)</button>
    </div>
    ${failed.length ? `<ul class="switch-blockers">${failed.map((name) => `<li><code>${escapeHtml(name)}</code> — ${escapeHtml(startBlockerText(name))}</li>`).join("")}</ul>` : ""}
    <div class="switch-step ${mode === "LIVE" ? "done" : "pending"}">
      <span class="step-index">2</span>
      <span class="step-text"><b>Set mode LIVE</b> — currently ${escapeHtml(mode)}</span>
      <button type="button" class="button warning xs" data-start-live-step="mode" data-start-market="${market}" ${busy || mode === "LIVE" || !ready ? "disabled" : ""}>2. Set mode LIVE</button>
    </div>
    <div class="switch-step ${gate === "ARMED" || gate === "ACTIVE" ? "done" : "pending"}">
      <span class="step-index">3</span>
      <span class="step-text"><b>Arm</b> — currently ${escapeHtml(gate)}</span>
      <button type="button" class="button warning xs" data-start-live-step="arm" data-start-market="${market}" ${busy || mode !== "LIVE" || gate === "ARMED" || gate === "ACTIVE" ? "disabled" : ""}>3. Arm</button>
    </div>
    <div class="switch-step ${gate === "ACTIVE" ? "done" : "pending"}">
      <span class="step-index">4</span>
      <span class="step-text"><b>Activate</b> — needs the risk acknowledgement and the exact phrase</span>
      <button type="button" class="button danger xs" data-start-live-step="activate" data-start-market="${market}" ${busy || gate !== "ARMED" ? "disabled" : ""}>4. Activate LIVE</button>
    </div>
    <div class="field top-space">
      <label for="start-live-phrase"><input type="checkbox" id="start-live-ack" data-start-market="${market}" ${step.ack ? "checked" : ""}> I acknowledge that LIVE orders use real money and that AXIOM routes no exit for NSE positions.</label>
      <label for="start-live-phrase-input">Type exactly: <code>${escapeHtml(phrase)}</code></label>
      <input id="start-live-phrase-input" autocomplete="off" value="${escapeHtml(step.phrase || "")}">
    </div>
    <p class="small muted" id="start-live-status">${escapeHtml(step.status || "Waiting for step 1.")}</p>`;
  const ack = $("start-live-ack");
  if (ack) ack.addEventListener("change", () => { step.ack = ack.checked; });
  const phraseInput = $("start-live-phrase-input");
  if (phraseInput) phraseInput.addEventListener("input", () => { step.phrase = phraseInput.value; });
  st.liveSteps[market] = step;
}

function startBlockerText(name) {
  const map = {
    configuration_complete: "A required dependency or credential is missing for this market.",
    mode_is_live: "The market is not routed LIVE yet (that is step 2).",
    no_unresolved_intents: "An earlier order outcome is ambiguous: reconcile before any new action.",
    not_blocked: "The runtime has blocked this market after a fault.",
    kill_switch_not_engaged: "The kill switch is engaged and must be reset.",
    risk_limits_configured: "Every live risk limit must be a positive number first.",
    calendar_session_open: "The exchange session is closed, so the broker refuses new orders.",
    environment_is_live: "The broker is configured for the paper environment, not live.",
  };
  return map[name] || "The server reported this check as failing; fix it before LIVE.";
}

async function startLiveStep(market, step) {
  const st = startState();
  st.busy.live = true;
  const record = (msg) => {
    st.liveSteps[market] = { ...(st.liveSteps[market] || {}), status: msg };
    renderStartLiveSteps(market, startMarketData(market)?.live?.confirmation_phrase || START_LIVE_PHRASE_FALLBACK);
  };
  try {
    if (step === "readiness") {
      await api(`/api/markets/live/${market}/readiness`);
      record("Readiness re-read from the server. Nothing was armed.");
    } else if (step === "mode") {
      await api(`/api/markets/live/${market}/mode`, { body: { mode: "LIVE" } });
      record("Mode set to LIVE. The gate is still DISARMED.");
    } else if (step === "arm") {
      await api(`/api/markets/live/${market}/arm`);
      record("Armed. The server re-checked every readiness item before arming.");
    } else if (step === "activate") {
      const phrase = $(`start-live-phrase-input`)?.value || "";
      const ack = Boolean($(`start-live-ack`)?.checked);
      if (!ack) { record("Activation refused: the risk acknowledgement is required."); return; }
      await api(`/api/markets/live/${market}/activate`, {
        body: { acknowledge_risk: true, confirm_live_trading: phrase },
      });
      record("Gate is ACTIVE. No strategy was started; start the session explicitly when ready.");
    }
    await loadStartMonitor({ background: true });
    toast(`LIVE ${market}`, `${step} completed`, step === "activate" ? "warn" : "ok");
  } catch (error) {
    record(`${step} failed: ${error.message}`);
    toast(`LIVE ${market} ${step} failed`, error.message, "err");
  } finally {
    st.busy.live = false;
    if ($("start-live-steps")) renderStartLiveSteps(market, startMarketData(market)?.live?.confirmation_phrase || START_LIVE_PHRASE_FALLBACK);
    renderStartControls();
  }
}

/* ---------------- monitor ---------------- */
async function loadStartMonitorRows() {
  const st = startState();
  const market = $("start-monitor-market")?.value || "ALL";
  const mode = $("start-monitor-mode")?.value || "ALL";
  const rows = [];
  const errors = [];
  for (const mkt of (market === "ALL" ? START_MARKETS : [market])) {
    const row = startMarketData(mkt);
    const wantPaper = mode === "ALL" || mode === "LIVE_MARKET_PAPER";
    const wantLive = mode === "ALL" || mode === "LIVE_MARKET_LIVE";
    if (wantPaper && (row?.live?.mode || "PAPER") !== "LIVE") {
      try {
        const payload = await api(`/api/markets/paper/${mkt}`);
        for (const order of (payload.orders || [])) {
          rows.push({
            time: order.updated_at || order.created_at,
            market: mkt,
            mode: "PAPER · SIMULATED",
            symbol: order.symbol,
            side: order.side,
            qty: order.qty,
            state: order.status,
            filled: order.filled_qty,
            price: order.avg_fill_price,
            fees: order.fees_total,
            error: order.reject_reason || "",
          });
        }
        for (const fill of (payload.fills || [])) {
          rows.push({
            time: fill.created_at,
            market: mkt,
            mode: "PAPER · SIMULATED FILL",
            symbol: fill.symbol,
            side: fill.side,
            qty: fill.fill_qty,
            state: "FILLED",
            filled: fill.fill_qty,
            price: fill.fill_price,
            fees: fill.fees_total,
            error: "",
          });
        }
      } catch (error) { errors.push(`${mkt} paper: ${error.message}`); }
    }
    if (wantLive && (row?.live?.mode || "") === "LIVE") {
      try {
        const payload = await api(`/api/markets/live/audit?market=${mkt}&limit=100`);
        for (const event of (payload.events || [])) {
          const p = event.payload || {};
          rows.push({
            time: event.created_at,
            market: mkt,
            mode: "LIVE · BROKER",
            symbol: p.symbol || "—",
            side: p.side || "—",
            qty: p.qty ?? "—",
            state: event.event_kind,
            filled: p.filled_qty ?? "—",
            price: p.avg_fill_price ?? "—",
            fees: p.fees ?? "—",
            error: p.reason || (p.unresolved ? "UNRESOLVED" : ""),
          });
        }
      } catch (error) { errors.push(`${mkt} live: ${error.message}`); }
    }
  }
  rows.sort((a, b) => String(b.time || "").localeCompare(String(a.time || "")));
  st.monitor = rows;
  st.monitorError = errors.join(" · ");
  renderStartMonitorRows();
  startMarkFlowSteps();
}

function renderStartMonitorRows() {
  const st = startState();
  const body = $("start-monitor-rows");
  const pill = $("start-monitor-pill");
  if (!body) return;
  const rows = st.monitor || [];
  if (pill) {
    pill.textContent = rows.length ? `${rows.length} EVENT${rows.length === 1 ? "" : "S"}` : "0 EVENTS";
    pill.className = `status-pill ${st.monitorError ? "err" : "info"}`;
  }
  if (st.monitorError) {
    body.innerHTML = `<tr><td colspan="11" class="empty-state">Monitor read failed: ${escapeHtml(st.monitorError)}</td></tr>`;
    return;
  }
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="11" class="empty-state">No paper or broker events yet. Start a session, or refresh after it runs a cycle.</td></tr>`;
    return;
  }
  body.innerHTML = rows.slice(0, 200).map((row) => {
    const live = String(row.mode).startsWith("LIVE");
    const unresolved = /UNRESOLVED|AMBIGUOUS/i.test(String(row.error || ""));
    return `<tr class="${unresolved ? "start-row-error" : ""}">
      <td class="small">${escapeHtml(startFmtTime(row.time))}</td>
      <td>${escapeHtml(row.market)}</td>
      <td><span class="status-pill ${live ? "err" : "ok"}">${escapeHtml(row.mode)}</span></td>
      <td><strong>${escapeHtml(String(row.symbol))}</strong></td>
      <td>${escapeHtml(String(row.side))}</td>
      <td>${escapeHtml(String(row.qty))}</td>
      <td>${escapeHtml(String(row.state))}</td>
      <td>${escapeHtml(String(row.filled))}</td>
      <td>${escapeHtml(String(row.price))}</td>
      <td>${escapeHtml(String(row.fees))}</td>
      <td class="${unresolved ? "start-warn-text" : "small"}">${escapeHtml(String(row.error || "—"))}</td>
    </tr>`;
  }).join("");
}

/* ---------------- credentials ---------------- */
async function startSaveUpstoxToken(event) {
  event.preventDefault();
  const input = $("start-upstox-token");
  const value = input?.value?.trim();
  if (!value) { toast("Upstox token", "Paste a token first.", "warn"); return; }
  try {
    await api("/api/autonomy/upstox-token", { body: { access_token: value } });
    input.value = "";
    toast("Upstox token saved", "Stored in the protected local store. Not returned to this page.", "ok");
  } catch (error) {
    input.value = "";
    toast("Upstox token not saved", error.message, "err");
  }
  await loadStartMonitor({ background: true });
}

async function startSaveAlpacaData(event) {
  event.preventDefault();
  const key = $("start-alpaca-key");
  const secret = $("start-alpaca-secret");
  const feed = $("start-alpaca-feed")?.value || "iex";
  if (!key?.value?.trim() || !secret?.value?.trim()) {
    toast("Alpaca data keys", "Both the Key ID and the Secret are required.", "warn"); return;
  }
  try {
    await api("/api/connections/alpaca/credentials", {
      body: { api_key: key.value.trim(), api_secret: secret.value.trim(), feed },
    });
    toast("Alpaca data keys saved", `Feed ${feed}. Separate from broker keys.`, "ok");
  } catch (error) {
    toast("Alpaca data keys not saved", error.message, "err");
  } finally {
    key.value = ""; secret.value = "";
  }
  await loadStartMonitor({ background: true });
}

async function startSaveBrokerKeys(event) {
  event.preventDefault();
  const env = $("start-broker-env")?.value || "paper";
  const key = $("start-broker-key");
  const secret = $("start-broker-secret");
  if (!key?.value?.trim() || !secret?.value?.trim()) {
    toast("Broker keys", "Both values are required.", "warn"); return;
  }
  try {
    await api(`/api/alpaca-broker/credentials/${env}`, {
      body: { api_key: key.value.trim(), api_secret: secret.value.trim() },
    });
    toast("Broker keys saved", `${env} slot stored. Nothing was armed or activated.`, "ok");
  } catch (error) {
    toast("Broker keys not saved", error.message, "err");
  } finally {
    key.value = ""; secret.value = "";
  }
  await loadStartMonitor({ background: true });
}

async function startSaveUpstoxLiveToken(event) {
  event.preventDefault();
  const input = $("start-upstox-live-token");
  const value = input?.value?.trim();
  if (!value) { toast("Upstox live token", "Paste the live token first.", "warn"); return; }
  try {
    await api("/api/autonomy/upstox-live-token", { body: { access_token: value } });
    toast("Upstox live token saved", "Separate real-money slot. Nothing was armed or activated.", "ok");
  } catch (error) {
    toast("Upstox live token not saved", error.message, "err");
  } finally {
    if (input) input.value = "";
  }
  await loadStartMonitor({ background: true });
}

async function startVerifyReadOnly(market) {
  try {
    if (market === "NSE") {
      const out = await api("/api/connections/upstox/verify-quote", { body: {} });
      toast("NSE read-only verify", out.connection_state || out.detail || "read-only quote probe returned", out.authenticated ? "ok" : "warn");
    } else {
      const out = await api("/api/connections/alpaca/verify-quote", { body: {} });
      toast("NASDAQ read-only verify", out.connection_state || out.detail || "read-only snapshot probe returned", out.authenticated ? "ok" : "warn");
    }
  } catch (error) {
    toast("Read-only verification failed", error.message, "err");
  }
  await loadStartMonitor({ background: true });
}

/* ---------------- dialog + binding ---------------- */
function startConfirm(title, body, confirmLabel, confirmClass) {
  return new Promise((resolve) => {
    openDialog({
      title,
      body,
      confirmLabel,
      confirmClass,
      onConfirm: async () => { closeDialog(); resolve(true); return true; },
    });
    const cancel = $("modal-cancel");
    const close = $("modal-close");
    const done = () => resolve(false);
    cancel?.addEventListener("click", done, { once: true });
    close?.addEventListener("click", done, { once: true });
  });
}

function bindStartEvents() {
  document.querySelectorAll("[data-page], [data-go]").forEach(() => {});
  $("start-mode-paper")?.addEventListener("click", () => { startState().mode = "PAPER"; startState().touched.mode = true; renderStartMode(); renderStartPreview(); });
  $("start-mode-live")?.addEventListener("click", () => { startState().mode = "LIVE"; startState().touched.mode = true; renderStartMode(); renderStartPreview(); });

  for (const market of START_MARKETS) {
    $(`start-select-${market}`)?.addEventListener("change", () => { renderStartControls(); renderStartCard(market); });
    $(`start-search-form-${market}`)?.addEventListener("submit", async (event) => {
      event.preventDefault();
      await loadStartSearch(market, $(`start-search-${market}`)?.value?.trim() || "");
    });
    $(`start-strategy-${market}`)?.addEventListener("change", (event) => {
      startState().strategy[market] = event.target.value;
      startState().touched.strategy = true;
      renderStartAutopilot(market);
      startMarkFlowSteps();
    });
    document.querySelectorAll(`[data-start-review][data-start-market="${market}"]`).forEach((button) => {
      button.addEventListener("click", () => {
        startState().review[market] = button.dataset.startReview;
        document.querySelectorAll(`[data-start-review][data-start-market="${market}"]`).forEach((other) => {
          const active = other.dataset.startReview === button.dataset.startReview;
          other.classList.toggle("active", active);
          other.setAttribute("aria-checked", active ? "true" : "false");
        });
        renderStartAutopilot(market);
        startMarkFlowSteps();
      });
    });
    document.querySelectorAll(`[data-start-symbol-market="${market}"][data-start-symbol]`).forEach(() => {});
  }

  document.addEventListener("change", (event) => {
    const box = event.target.closest("[data-start-symbol]");
    if (!box) return;
    const market = box.dataset.startSymbolMarket;
    const key = box.dataset.startSymbol;
    const current = new Set(startSelectionKey(market));
    if (box.checked) current.add(key); else current.delete(key);
    startSetSelection(market, [...current]);
  });

  document.addEventListener("click", async (event) => {
    const remove = event.target.closest("[data-start-remove]");
    if (remove) {
      const market = remove.dataset.startRemoveMarket;
      startSetSelection(market, startSelectionKey(market).filter((k) => k !== remove.dataset.startRemove));
      return;
    }
    const action = event.target.closest("[data-start-action]");
    if (action) {
      const market = action.dataset.startMarket;
      if (action.dataset.startAction === "select-all") await startSelectAllEligible(market);
      if (action.dataset.startAction === "clear") startSetSelection(market, []);
      if (action.dataset.startAction === "verify-upstox") await startVerifyReadOnly("NSE");
      if (action.dataset.startAction === "verify-alpaca") await startVerifyReadOnly("NASDAQ");
      return;
    }
    const liveStep = event.target.closest("[data-start-live-step]");
    if (liveStep) { await startLiveStep(liveStep.dataset.startMarket, liveStep.dataset.startLiveStep); return; }
    const liveStart = event.target.closest("[data-start-live]");
    if (liveStart) { await startOpenLiveSequence(liveStart.dataset.startLive); return; }
  });

  $("start-preview-btn")?.addEventListener("click", () => startPreviewSession().catch((e) => toast("Preview failed", e.message, "err")));
  $("start-paper-btn")?.addEventListener("click", () => startPaperSession().catch((e) => toast("Start failed", e.message, "err")));
  $("start-stop-btn")?.addEventListener("click", () => startStopSession().catch((e) => toast("Stop failed", e.message, "err")));
  $("start-emergency-btn")?.addEventListener("click", () => startEmergencyStop().catch((e) => toast("Emergency stop failed", e.message, "err")));
  $("start-monitor-refresh")?.addEventListener("click", () => loadStartMonitorRows());
  $("start-monitor-market")?.addEventListener("change", () => loadStartMonitorRows());
  $("start-monitor-mode")?.addEventListener("change", () => loadStartMonitorRows());
  $("start-upstox-form")?.addEventListener("submit", startSaveUpstoxToken);
  $("start-alpaca-data-form")?.addEventListener("submit", startSaveAlpacaData);
  $("start-alpaca-broker-form")?.addEventListener("submit", startSaveBrokerKeys);
  $("start-upstox-live-form")?.addEventListener("submit", startSaveUpstoxLiveToken);
}

function init() {
  bindEvents(); bindModelLabEvents(); bindTradeMonitorEvents(); bindEnsembleEvents(); bindScanEvents();
  bindStartEvents();
  $("refresh-interval").value = "10000"; renderActivity();
  const saved = storageGet("axiom-api-token");
  if (saved) unlock(saved); else showAuth();
}
init();
