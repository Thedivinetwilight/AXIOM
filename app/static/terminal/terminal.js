"use strict";

/* AXIOM operator shell. No credential or API token is ever written to the DOM
 * by an API response, logged, exported, or persisted outside tab sessionStorage. */
const $ = (id) => document.getElementById(id);
const MODE_LABELS = {
  AXIOM_PAPER: "LOCAL PAPER",
  VANTAGE_DEMO: "VANTAGE DEMO",
  VANTAGE_LIVE: "VANTAGE LIVE",
};
const PAGE_TITLES = {
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
  token: "", workspace: null, activity: [], connectionInfo: null, marketData: null,
  page: "overview", mode: null, side: "BUY", readiness: null, activation: null,
  instruments: [], selected: new Set(), productTypes: new Set(), instrumentLoading: false,
  watchlist: new Set(), watchlistKey: "",
  contracts: {}, chart: { mode: null, feed: "legacy", symbol: "", timeframe: "1m", bars: [], quote: null, payload: null },
  chartView: { visible: 160, endOffset: 0, crosshair: true, drawings: {}, pending: null, geometry: null, pointer: null, drag: null, frame: null },
  chartSymbolsLoaded: false, chartRequest: 0, riskDirty: false, credentialMode: null,
  performance: { backend: null, browser: {} }, listedAssets: null, alpacaBroker: null, ticketReview: null,
  mm: { available: false, activeVenue: "NSE", overview: null, catalogItems: [], side: "BUY", chartFeed: "SESSION", lastPreview: null },
  ml: { available: false, overview: null, selectedRunId: "" },
  tm: { page: 1, pageSize: 50, summary: null, selected: null, chartPayload: null, hitBoxes: [], selectedMarkerId: "" },
  ens: { overview: null, selectedDecisionIdx: 0, latestAblation: null, latestBenchmark: null },
  dialog: null, dialogReturnFocus: null, refreshing: false, refreshTimer: null,
  submissionUncertain: false, lastError: null,
};

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
function isVantage() { return state.workspace?.status?.mode !== "AXIOM_PAPER"; }
function currentStatus() { return state.workspace?.status || {}; }
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
function watchlistStorageKey() {
  const status = currentStatus();
  const identity = String(status.identity_fingerprint || "no-verified-account").slice(0, 24);
  return `axiom-watchlist-v1:${status.mode || "UNKNOWN"}:${identity}`;
}
function loadWatchlist() {
  const key = watchlistStorageKey();
  if (state.watchlistKey === key) return;
  state.watchlistKey = key; state.watchlist = new Set();
  try {
    const saved = JSON.parse(storageGet(key) || "[]");
    if (Array.isArray(saved)) state.watchlist = new Set(saved.filter((item) => typeof item === "string" && item.length <= 64).slice(0, 200));
  } catch (_) { state.watchlist = new Set(); }
}
function saveWatchlist() {
  storageSet(state.watchlistKey || watchlistStorageKey(), JSON.stringify([...state.watchlist].slice(0, 200)));
  renderInstruments();
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
    await api("/api/trading/status", { keepAuth: true });
    storageSet("axiom-api-token", token);
    $("auth-token").value = ""; $("auth-error").textContent = "";
    $("auth-gate").hidden = true; $("app").hidden = false;
    await refreshAll();
    configureRefresh();
  } catch (error) {
    state.token = ""; storageSet("axiom-api-token", "");
    $("auth-gate").hidden = false; $("app").hidden = true;
    $("auth-error").textContent = error.message;
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

async function loadWorkspace({ background = false } = {}) {
  if (state.refreshing) return state.workspace;
  state.refreshing = true;
  try {
    const workspace = await api("/api/trading/workspace");
    const previousMode = state.mode;
    state.workspace = workspace; state.mode = workspace.status.mode; state.lastError = null;
    if (workspace.market_data) state.marketData = workspace.market_data;
    if (previousMode && previousMode !== state.mode) resetModeLocalState();
    if (state.submissionUncertain) {
      const unresolved = (workspace.intents || []).some((item) => UNRESOLVED.has(item.status));
      if (!unresolved && workspace.broker_state_available !== false) state.submissionUncertain = false;
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
    const result = await api("/api/trading/activity?limit=300");
    state.activity = result.events || [];
    renderActivity(); renderOverviewActivity();
  } catch (error) { toast("Activity refresh failed", error.message, "err"); }
}
async function loadConnectionInfo() {
  try { state.connectionInfo = await api("/api/trading/connection"); renderConnections(); }
  catch (error) { toast("Connection details unavailable", error.message, "err"); }
}
async function loadMarketData() {
  try {
    state.marketData = await api("/api/market-data/status");
    if (state.workspace?.paper_session) state.workspace.market_data = state.marketData;
    renderMarketData();
  } catch (error) {
    state.marketData = { state: "UNAVAILABLE", connected: false, authenticated: false, detail: error.message };
    renderMarketData();
  }
}
async function loadPerformance() {
  try { state.performance.backend = await api("/api/trading/performance"); }
  catch (error) { state.performance.backend = { error: error.message, metrics: {} }; }
  renderPerformance();
}
async function loadListedAssets() {
  try { state.listedAssets = await api("/api/listed-assets/status"); }
  catch (error) { state.listedAssets = { error: error.message, execution_supported: false }; }
  renderListedAssets();
}

async function loadBrokerDestination() {
  // Broker path is optional; absence must never break the console.
  try { state.alpacaBroker = await api("/api/alpaca-broker/status"); }
  catch (error) { state.alpacaBroker = null; }
  renderMode();
}
async function refreshAll() {
  await loadWorkspace();
  await Promise.all([loadActivity(), loadConnectionInfo(), loadMarketData(), loadPerformance(), loadListedAssets(), loadBrokerDestination(), loadMultiMarketOverview(), loadModelLabOverview()]);
  if (state.page === "instruments" && currentStatus().connected) await loadInstruments();
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
  state.readiness = null; state.activation = null; state.riskDirty = false; state.ticketReview = null;
  state.instruments = []; state.selected = new Set(); state.contracts = {};
  state.watchlist = new Set(); state.watchlistKey = "";
  state.chart = { mode: state.mode, feed: "legacy", symbol: "", timeframe: "1m", bars: [], quote: null, payload: null };
  state.chartView.endOffset = 0; state.chartView.pending = null; state.chartView.pointer = null; state.chartView.drag = null;
  state.chartSymbolsLoaded = false; state.submissionUncertain = false; state.chartRequest += 1;
  drawChart();
}
function configureRefresh() {
  clearInterval(state.refreshTimer);
  const interval = Number($("refresh-interval").value || 0);
  if (interval > 0) state.refreshTimer = setInterval(() => {
    if (!document.hidden && !state.dialog?.busy) loadWorkspace({ background: true }).catch(() => {});
  }, interval);
}

function renderAll() {
  if (!state.workspace) return;
  renderMode(); renderBoundary(); renderOverview(); renderOrders(); renderRisk(); renderConnections(); renderMarketData(); renderChartContext(); renderActivity();
}
function badgeClass(mode) { return mode === "VANTAGE_LIVE" ? "live" : mode === "VANTAGE_DEMO" ? "demo" : "paper"; }
function renderMode() {
  const status = currentStatus(); const mode = status.mode; const label = MODE_LABELS[mode] || mode;
  const paperSession = state.workspace.paper_session || {};
  const paperState = paperSession.state || "STOPPED";
  const automatedPaper = mode === "AXIOM_PAPER" && paperSession.automation_enabled === true;
  for (const id of ["global-mode", "overview-mode"]) {
    $(id).className = `mode-badge ${badgeClass(mode)}`; $(id).textContent = label;
  }
  $("session-mode").value = mode;
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
  const connection = status.connection?.state || "NOT_CONFIGURED";
  $("global-connection").textContent = mode === "AXIOM_PAPER" ? "Local simulated fills" : connection.replaceAll("_", " ");
  const connectionKind = status.connected ? "ok" : connection === "NOT_CONFIGURED" ? "" : "warn";
  $("global-connection-dot").className = `connection-dot ${connectionKind}`;
  $("global-server").textContent = status.server || (mode === "AXIOM_PAPER" ? "AXIOM local only" : "No MT5 server");
  $("global-account").textContent = status.account_login_masked || status.account_label || (mode === "AXIOM_PAPER" ? "Local paper" : "No verified account");
  const lifecycle = status.emergency_stop ? "EMERGENCY STOP" : mode === "AXIOM_PAPER" ? `PAPER ${paperState}` : status.lifecycle || "DISARMED";
  $("global-lifecycle").textContent = lifecycle;
  $("global-lifecycle").className = `status-pill ${status.emergency_stop || paperState === "ERROR" ? "err" : status.active || paperState === "RUNNING" ? "ok" : status.armed || paperState === "PAUSED" ? "warn" : ""}`;
  $("global-lock-note").textContent = status.emergency_stop
    ? "Durable stop blocks new entries across restart."
    : mode === "VANTAGE_LIVE"
      ? (status.active ? "Real order placement explicitly activated; automation remains OFF." : "Real order placement blocked until Arm and separate Activate.")
      : mode === "VANTAGE_DEMO" ? "Demo orders only; demo results do not prove live readiness."
        : automatedPaper ? `${paperSession.strategy} can create local simulated orders automatically; no broker routing.`
          : paperSession.review_policy === "MANUAL_ORDER_REVIEW" ? "Manual-only local paper; this console reviews each manual order." : "Independent local paper; no Vantage order routing.";
  $("sidebar-automation").textContent = mode !== "AXIOM_PAPER"
    ? "Vantage strategy automation OFF. No profitability claim."
    : automatedPaper ? `${paperSession.strategy} automation ON · local simulated orders only.`
      : paperSession.review_policy === "MANUAL_ORDER_REVIEW" ? "manual_only automation OFF · this console reviews manual orders."
        : "No active paper strategy. Default replay enables moving_average automation.";
  $("ticket-mode").textContent = label; $("ticket-mode").className = `status-pill ${status.active ? "ok" : "warn"}`;
  $("deals-label").textContent = mode === "AXIOM_PAPER" ? "LOCAL SIMULATED FILLS" : mode === "VANTAGE_DEMO" ? "MT5 DEMO DEALS" : "MT5 LIVE DEALS";
  document.body.dataset.mode = mode || "AXIOM_PAPER";
  renderFreshness(); renderBoundary();
}
function renderBoundary() {
  if (!state.workspace) return;
  const status = currentStatus(); const paper = status.mode === "AXIOM_PAPER";
  const session = state.workspace.paper_session || {};
  const paperState = session.state || "STOPPED";
  const stop = session.stop_reason ? ` · ${session.stop_reason}` : "";
  const automated = paper && session.automation_enabled === true;
  const manualOnly = paper && session.review_policy === "MANUAL_ORDER_REVIEW";
  $("boundary-execution").textContent = paper ? "LOCAL PAPER · SIMULATED" : `${MODE_LABELS[status.mode]} · ${status.connected ? "MT5 CONNECTED" : "DISCONNECTED"}`;
  $("boundary-execution-note").textContent = paper ? "Local simulated strategy/manual fills only; no Vantage order routing" : status.mode === "VANTAGE_LIVE" ? "Real-money capability remains separately locked" : "Demo orders only; no live-readiness inference";
  if (paper) {
    const connected = Boolean(state.marketData?.authenticated);
    const replay = session.feed_kind === "LOCAL_REPLAY" && session.strategy;
    $("boundary-data").textContent = connected ? "VANTAGE DEMO · READ-ONLY"
      : replay ? `LOCAL REPLAY · ${paperState}${stop}`
        : session.strategy ? `READ-ONLY STREAM · ${paperState}${stop}` : "LOCAL PAPER / NO ACTIVE FEED";
    $("boundary-data-note").textContent = connected ? "Independent data interface; execution remains LOCAL PAPER"
      : replay ? `Sample data only · ${session.strategy} · ${session.automation_enabled ? "automated local simulation" : "manual-only"}`
        : session.strategy ? "Read-only input feeding local simulation; not a broker execution session" : "Default replay is sample data and enables moving_average automation";
  } else {
    $("boundary-data").textContent = status.connected ? "SAME VANTAGE MT5 ACCOUNT" : "NO CURRENT BROKER FEED";
    $("boundary-data-note").textContent = "Quote connectivity alone never grants order permission";
  }
  const lifecycle = status.emergency_stop ? "EMERGENCY STOP" : status.lifecycle || "DISARMED";
  $("boundary-auth").textContent = !paper ? lifecycle : status.emergency_stop ? "EMERGENCY STOP"
    : automated ? (session.feed_kind === "LOCAL_REPLAY" ? "REPLAY STRATEGY · AUTOMATION ON" : "LOCAL STRATEGY · AUTOMATION ON")
      : manualOnly ? "LOCAL REVIEW · AUTOMATION OFF" : "LOCAL PAPER · NO ACTIVE STRATEGY";
  $("boundary-auth-note").textContent = !paper ? (status.active ? "Explicitly active; every order still requires review + final phrase" : "Readiness → Arm → Activate → reviewed order required")
    : status.emergency_stop ? "Durable stop blocks new entries and strategy resume"
      : automated ? `${session.strategy} can create simulated orders/fills automatically · ${paperState}${stop} · no broker routing`
        : manualOnly ? "manual_only creates no strategy orders; this console reviews each manual simulated order"
          : "Default replay enables moving_average automation; manual tickets are separately reviewed";
}
function renderFreshness() {
  if (!state.workspace) return;
  const status = currentStatus();
  if (state.lastError) {
    $("global-freshness").innerHTML = statusPill("REFRESH FAILED · LAST VIEW MAY BE STALE", "err");
    $("global-freshness").title = state.lastError; return;
  }
  if (status.mode === "AXIOM_PAPER") {
    const marketData = state.marketData || state.workspace.market_data;
    if (marketData?.authenticated) {
      const quote = marketData.quote;
      const fresh = marketData.state === "CONNECTED" && quote?.fresh;
      const age = ageSeconds(quote?.timestamp);
      $("global-freshness").innerHTML = statusPill(
        fresh ? `MT5 DEMO QUOTE ${Number.isFinite(age) ? `${Math.max(0, age).toFixed(1)}s OLD` : "TIME UNKNOWN"}` : "MT5 FEED DEGRADED · ENTRIES BLOCKED",
        fresh ? "ok" : "err",
      );
      $("global-freshness").title = fresh ? `Source ${timeText(quote.timestamp)} · received ${timeText(quote.received_at)}` : marketData.detail || "Feed is not fresh";
      return;
    }
    $("global-freshness").innerHTML = statusPill("LOCAL PAPER · SIMULATED", "warn"); return;
  }
  if (!status.connected || state.workspace.broker_state_available === false) {
    $("global-freshness").innerHTML = statusPill("NO CURRENT BROKER SNAPSHOT", "warn"); return;
  }
  const age = ageSeconds(state.workspace.reconciled_at);
  const kind = age > 30 ? "err" : age > 15 ? "warn" : "ok";
  $("global-freshness").innerHTML = statusPill(`RECONCILED ${Number.isFinite(age) ? `${Math.max(0, age).toFixed(0)}s AGO` : "UNKNOWN"}`, kind);
  $("global-freshness").title = timeText(state.workspace.reconciled_at);
}

function renderOverview() {
  const workspace = state.workspace; const status = currentStatus(); const account = workspace.account || {};
  const currency = account.currency || workspace.account_currency || "";
  $("metric-equity").textContent = money(account.equity, currency);
  $("metric-currency").textContent = currency ? `${currency} account` : status.connected ? "account currency unavailable" : "no verified account";
  const positions = workspace.positions || [];
  const unrealized = workspace.pnl?.unrealized ?? (
    workspace.broker_state_available === false ? null
      : positions.reduce((sum, item) => sum + Number(item.unrealized_pnl ?? item.profit ?? 0) + Number(item.swap || 0), 0)
  );
  $("metric-unrealized").textContent = money(unrealized, currency);
  $("metric-unrealized-note").textContent = status.mode === "AXIOM_PAPER" ? "All current local open positions · not this run only" : "broker-confirmed current view";
  $("metric-realized").textContent = money(workspace.pnl?.realized_visible_history, currency);
  $("realized-window").textContent = status.mode === "AXIOM_PAPER" ? "All persisted local history · survives restart" : workspace.pnl?.history_window_days ? `${workspace.pnl.history_window_days}-day visible broker history` : "visible local/broker history only";
  $("metric-margin").textContent = money(account.margin_free ?? account.cash, currency);
  $("metric-margin-note").textContent = status.mode === "AXIOM_PAPER" ? "local simulated cash" : status.connected ? "MT5 free margin" : "unavailable while disconnected";
  const connectionRows = [
    ["Execution", MODE_LABELS[status.mode]],
    ["Connection", status.connection?.state || "NOT_CONFIGURED"],
    ["Server", status.server || "Not configured"],
    ["Account", status.account_login_masked || "Not verified"],
    ["Reconciled", timeText(workspace.reconciled_at)],
    ["Detail", status.connection?.detail || "Local paper runtime"],
  ];
  $("overview-connection").innerHTML = connectionRows.map(([key, value]) => `<dt>${escapeHtml(key)}</dt><dd>${escapeHtml(value)}</dd>`).join("");
  const limits = status.risk_limits || {};
  const configured = Object.values(limits).filter((value) => Number(value) > 0).length;
  const unresolved = (workspace.intents || []).filter((item) => UNRESOLVED.has(item.status)).length;
  const session = workspace.paper_session || {};
  const guardLabel = status.emergency_stop ? "Durable emergency stop is SET"
    : status.mode === "AXIOM_PAPER" ? `${session.state || "STOPPED"} · ${session.automation_enabled ? `${session.strategy} automation ON` : session.review_policy === "MANUAL_ORDER_REVIEW" ? "manual-only · automation OFF" : "no active strategy"}`
      : `${status.lifecycle || "DISARMED"} · automation OFF`;
  $("overview-risk").innerHTML = `
    <div class="inline-status ${status.emergency_stop ? "err" : status.active ? "ok" : "warn"}">${escapeHtml(guardLabel)}</div>
    <dl class="detail-list"><dt>Vantage risk fields</dt><dd>${status.mode === "AXIOM_PAPER" ? "Separate legacy paper limits" : `${configured}/${Object.keys(limits).length} above zero`}</dd><dt>Unresolved intents</dt><dd>${unresolved}</dd><dt>Live placement</dt><dd>${status.mode === "VANTAGE_LIVE" && status.active ? "Explicitly active" : "Blocked"}</dd></dl>`;
  const quotes = workspace.quotes || {};
  const quoteEntries = Object.entries(quotes);
  $("overview-quotes").innerHTML = quoteEntries.length ? quoteEntries.map(([symbol, quote]) => {
    const age = ageSeconds(quote.timestamp);
    return `<div class="quote-row"><div><strong>${escapeHtml(symbol)}</strong><small>${escapeHtml(timeText(quote.timestamp))}</small></div><div><strong>${fmt(quote.bid)} / ${fmt(quote.ask)}</strong><small>${Number.isFinite(age) ? `${age.toFixed(1)}s old` : "freshness unknown"}</small></div></div>`;
  }).join("") : `<p class="empty-state">${status.mode === "AXIOM_PAPER" ? "Open Charts to load the independent paper feed." : status.connected ? "Select instruments and reload quotes/charts." : "No broker quotes while disconnected."}</p>`;
  const checks = operatorChecklist();
  $("overview-checklist").innerHTML = checks.map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  renderPaperRun(); renderOverviewActivity();
}
function renderPaperRun() {
  const panel = $("paper-run-panel");
  if (!panel) return;
  const paper = currentStatus().mode === "AXIOM_PAPER";
  panel.hidden = !paper;
  if (!paper) return;
  const session = state.workspace.paper_session || {};
  const current = session.current_run;
  const run = current || session.last_run;
  const currency = state.workspace.account?.currency || "";
  if (!run) {
    $("paper-run-title").textContent = "No current paper run";
    $("paper-run-state").textContent = session.state || "STOPPED";
    $("paper-run-summary").textContent = "No current or persisted run summary. Account cards above are persisted local totals, not a new run result.";
    $("paper-run-details").innerHTML = "";
    $("paper-run-pnl-note").textContent = "Start automated replay or the separate manual-only paper flow to create a run-scoped result. No profitability claim.";
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
  const status = currentStatus();
  if (status.mode === "AXIOM_PAPER") {
    if (state.marketData?.authenticated) return [
      "Open Charts and choose an exact symbol discovered from the connected VANTAGE DEMO account.",
      "Verify fresh advancing tick timestamps and the READ-ONLY MARKET DATA label; CFDs are not owned shares.",
      "Start the manual-only live-price paper session. Automation stays OFF; every fill requires reviewed local submission.",
      "If the feed degrades, new BUY entries block; existing local paper records remain available.",
    ];
    return [
      "Default replay is sample data with moving_average automation ON: it can create simulated strategy orders/fills without reviewing each one.",
      "For reviewed-only manual fills, use the separate manual-only MT5 paper flow or a manual ticket; no Vantage order is routed.",
      "For fresh MT5 prices, save VANTAGE_DEMO locally and connect independent read-only market data on Connections; execution stays LOCAL PAPER.",
      "Daily Upstox live-market PAPER is a separate /autonomy workflow; replay results are not live-market or Kronos evidence.",
    ];
  }
  const steps = [];
  if (!status.credential) steps.push("Enter the MT5 credential locally on Connections; never send it to chat or CI.");
  if (!status.connected) steps.push("Run the read-only connection test and verify the exact server and masked account identity.");
  if (!(status.selected_symbols || []).length) steps.push("Discover and explicitly select instruments exposed by this account.");
  if ((status.risk_limits?.max_order_volume || 0) <= 0) steps.push("Set every bounded Vantage risk limit; zero blocks entries.");
  steps.push("Run readiness, then Arm and separately Activate only if you intend to enable order placement.");
  steps.push("Review each order and reconcile ambiguous outcomes; tests and demo fills do not establish profitability or live readiness.");
  return steps;
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
  const workspace = state.workspace; const status = currentStatus(); const positions = workspace.positions || [];
  $("position-count").textContent = status.connected || status.mode === "AXIOM_PAPER" ? String(positions.length) : "UNKNOWN";
  if (!positions.length) emptyRow("position-rows", 8, status.mode !== "AXIOM_PAPER" && !status.connected ? "No broker snapshot; this does not prove there are no positions." : "No positions in the current confirmed/local view.");
  else $("position-rows").innerHTML = positions.map((position) => {
    const paper = status.mode === "AXIOM_PAPER";
    const id = String(position.position_id || position.symbol || "");
    const volume = position.volume ?? position.quantity;
    const ownership = paper ? "Local simulated position" : position.managed ? "AXIOM-matched MT5 position" : "External / unmatched MT5 position";
    const action = paper
      ? `<button class="button secondary" data-paper-exit="${escapeHtml(position.symbol)}">Review exit</button>`
      : `<div class="button-row"><button class="button secondary" data-modify="${escapeHtml(id)}">Modify SL / TP</button><button class="button danger-outline" data-close="${escapeHtml(id)}">Review full close</button></div>`;
    return `<tr><td>${escapeHtml(position.symbol)}</td><td>${escapeHtml(position.side || "LONG")}</td><td>${fmt(volume, 8)}</td><td>${fmt(position.price_open ?? position.entry_price)} / ${fmt(position.price_current ?? position.mark)}</td><td>${fmt(position.unrealized_pnl ?? position.profit, 2)}</td><td>${fmt(position.stop_loss)} / ${fmt(position.take_profit)}</td><td><small>${escapeHtml(ownership)}</small></td><td>${action}</td></tr>`;
  }).join("");
  const orders = status.mode === "AXIOM_PAPER" ? (workspace.orders || []) : (workspace.intents || []);
  if (!orders.length) emptyRow("order-rows", 8, status.mode !== "AXIOM_PAPER" && !status.connected ? "No current broker snapshot; persisted intents may still appear after reconnect." : "No orders or intents in this view.");
  else $("order-rows").innerHTML = orders.map((order) => {
    const paper = status.mode === "AXIOM_PAPER";
    const id = String(order.id || order.intent_id || "");
    const active = ["NEW", "PARTIALLY_FILLED"].includes(order.status);
    const canCancel = paper ? active : Boolean(order.broker_order) && ["PENDING", "PARTIALLY_FILLED"].includes(order.status);
    const action = canCancel ? `<button class="button danger-outline" data-cancel="${escapeHtml(id)}">Review cancel</button>` : `<small>${UNRESOLVED.has(order.status) ? "Reconcile; do not resubmit" : "No cancel action"}</small>`;
    return `<tr><td>${escapeHtml(timeText(order.created_ts || order.timestamp))}</td><td>${escapeHtml(order.symbol)}</td><td>${escapeHtml(order.side)} / ${escapeHtml(order.order_type)}</td><td>${fmt(order.volume ?? order.quantity, 8)}</td><td>${statusPill(order.status, UNRESOLVED.has(order.status) ? "err" : ["FILLED", "MODIFIED"].includes(order.status) ? "ok" : "warn")}</td><td>${escapeHtml(order.broker_order || order.id || "—")}</td><td><small>${escapeHtml(order.reason || order.confirmed_ts || "No broker confirmation")}</small></td><td>${action}</td></tr>`;
  }).join("");
  const deals = workspace.deals || [];
  if (!deals.length) emptyRow("deal-rows", 7, status.mode !== "AXIOM_PAPER" && !status.connected ? "Stored deals are unavailable; no current broker snapshot." : "No fills/deals in the visible history.");
  else $("deal-rows").innerHTML = deals.map((deal) => `<tr><td>${escapeHtml(timeText(deal.timestamp))}</td><td>${escapeHtml(deal.symbol)}</td><td>${escapeHtml(deal.side)} / ${escapeHtml(deal.entry || (deal.reason === "signal" ? "SIMULATED" : "LOCAL"))}</td><td>${fmt(deal.volume ?? deal.quantity, 8)}</td><td>${fmt(deal.price)}</td><td>${fmt(deal.profit, 2)}</td><td>${fmt(deal.commission, 2)} / ${fmt(deal.swap, 2)} / ${fmt(deal.fee, 2)}</td></tr>`).join("");
  $("reconcile-now").disabled = status.mode === "AXIOM_PAPER" || !status.connected;
  $("reconcile-now").title = status.mode === "AXIOM_PAPER" ? "Local paper state is already in the workspace; no MT5 reconciliation applies." : !status.connected ? "Connect to MT5 before reconciliation." : "";
}

function renderRisk() {
  const status = currentStatus(); const paper = status.mode === "AXIOM_PAPER";
  $("risk-emergency").textContent = status.emergency_stop ? "EMERGENCY STOP SET" : "STOP CLEAR";
  $("risk-emergency").className = `status-pill ${status.emergency_stop ? "err" : "ok"}`;
  const paperSession = state.workspace.paper_session || {};
  $("lifecycle-current").textContent = paper ? `PAPER · ${paperSession.state || "STOPPED"}${paperSession.stop_reason ? ` · ${paperSession.stop_reason}` : ""}` : status.lifecycle || "DISARMED";
  const limits = status.risk_limits || {};
  if (!state.riskDirty) for (const [key, value] of Object.entries(limits)) {
    const input = $(`risk-${key}`); if (input && document.activeElement !== input) input.value = value ?? 0;
  }
  document.querySelectorAll("#risk-form input").forEach((input) => {
    input.disabled = paper; input.title = paper ? "These are Vantage limits. Legacy paper uses its separate configured limits." : "";
  });
  setDisabled("save-risk", paper, "Select a Vantage session to edit its server-side limits.");
  setDisabled("arm-session", paper || !state.readiness?.ready || !state.readiness?.readiness_id || ageSeconds(state.readiness.generated_at) > 60 || status.emergency_stop,
    paper ? "Paper does not use Vantage Arm/Activate." : "Run a fresh, fully passing readiness check first.");
  setDisabled("activate-session", paper || !state.activation?.activation_id, paper ? "Paper does not use Vantage activation." : "Arm or resume first to obtain a fresh activation challenge.");
  const paperState = state.workspace.paper_session?.state;
  setDisabled("pause-session", paper ? paperState !== "RUNNING" : !status.active, paper ? "Only a running paper strategy can pause." : "Only an active Vantage session can pause.");
  $("pause-session").textContent = paper ? "Pause paper strategy" : "Pause new entries";
  setDisabled("stop-session", paper ? paperState === "STOPPED" : false, paper ? "Paper session is already stopped." : "");
  $("stop-session").textContent = paper ? "Stop paper session" : "Stop & reconcile";
  setDisabled("clear-emergency", !status.emergency_stop, "Emergency stop is not set.");
  $("lifecycle-help").textContent = paper
    ? "PAPER uses the independent local simulator. Emergency stop is durable and blocks entries/resume until explicitly cleared."
    : status.mode === "VANTAGE_LIVE"
      ? "Activate explicitly enables real order placement for this armed account only. Credentials, restart or reconnect never activate it."
      : "Demo activation permits demo order review/submission only. It proves neither live readiness nor profitability.";
  renderReadiness();
}
function renderReadiness() {
  const readiness = state.readiness;
  if (!readiness) {
    $("readiness-summary").className = "inline-status"; $("readiness-summary").textContent = "Readiness has not been run in this browser session.";
    $("readiness-list").innerHTML = ""; return;
  }
  $("readiness-summary").className = `inline-status ${readiness.ready ? "ok" : "err"}`;
  $("readiness-summary").textContent = readiness.ready ? `All ${readiness.checks.length} checks passed at ${timeText(readiness.generated_at)}. Arm challenge expires in 60 seconds.` : `${readiness.blockers.length} blocker(s). Unknown or failed checks block Arm.`;
  $("readiness-list").innerHTML = (readiness.checks || []).map((check) => `<div class="readiness-item ${check.passed ? "pass" : "fail"}"><span class="check-icon">${check.passed ? "✓" : "×"}</span><strong>${escapeHtml(check.label)}</strong><span>${escapeHtml(check.detail)}</span></div>`).join("");
}

function credentialEnvironment() {
  return currentStatus().mode === "AXIOM_PAPER" ? "VANTAGE_DEMO" : currentStatus().mode;
}
function renderConnections() {
  const status = currentStatus(); const paper = status.mode === "AXIOM_PAPER";
  const credentialMode = credentialEnvironment();
  const info = state.connectionInfo; const secretStatus = info?.secret_store || "Checking protected storage…";
  $("secret-store-status").textContent = secretStatus.startsWith("Windows") ? "DPAPI PROTECTED" : "PROTECTED STORE UNAVAILABLE";
  $("secret-store-status").className = `status-pill ${secretStatus.startsWith("Windows") ? "ok" : "err"}`;
  $("credential-environment").textContent = paper ? "VANTAGE_DEMO market-data credential · execution stays LOCAL PAPER" : `${MODE_LABELS[status.mode]} credential`;
  const profile = info?.profiles?.[credentialMode] || (paper ? null : status.credential);
  if (state.credentialMode !== credentialMode) {
    state.credentialMode = credentialMode;
    for (const id of ["mt5-login", "mt5-password", "mt5-server", "mt5-path", "mt5-label"]) $(id).value = "";
    if (profile) {
      $("mt5-server").value = profile.server || ""; $("mt5-path").value = profile.terminal_path || ""; $("mt5-label").value = profile.label || "";
    } else if (status.mode === "VANTAGE_LIVE") $("mt5-server").value = "VantageMarkets-Live 13";
  }
  const storeUnavailable = !secretStatus.startsWith("Windows") && Boolean(info);
  document.querySelectorAll("#credential-form input").forEach((input) => {
    input.disabled = storeUnavailable;
    input.title = paper ? "LOCAL PAPER edits only the protected VANTAGE_DEMO profile; execution does not change." : storeUnavailable ? "OS-protected storage is unavailable; credentials cannot be entered safely." : "";
  });
  setDisabled("save-credential", storeUnavailable || Boolean(profile), storeUnavailable ? "DPAPI is unavailable; no plaintext fallback exists." : "Credential exists; use explicit Replace.");
  setDisabled("replace-credential", storeUnavailable || !profile, !profile ? "No existing credential to replace." : storeUnavailable ? "DPAPI is unavailable." : "");
  setDisabled("edit-credential", storeUnavailable || !profile, !profile ? "Save a credential first." : "Protected storage is unavailable.");
  setDisabled("delete-credential", !profile, "No saved credential to delete.");
  setDisabled("test-connection", paper || !profile || storeUnavailable, paper ? "Select a Vantage environment." : !profile ? "Save a credential first." : "Protected storage is unavailable.");
  setDisabled("disconnect-connection", paper || (!status.connected && status.lifecycle === "DISARMED"), paper ? "No MT5 connection in local paper mode." : "Already disconnected and disarmed.");
  $("connection-state").textContent = paper ? "LOCAL PAPER ONLY" : status.connection?.state || "NOT CONFIGURED";
  $("connection-state-dot").className = `connection-dot ${status.connected ? "ok" : paper ? "" : "warn"}`;
  $("connection-detail").className = `inline-status ${status.connected ? "ok" : paper ? "" : "warn"}`;
  $("connection-detail").textContent = paper ? "Execution is local LOCAL PAPER. The credential above is DEMO-only; use the independent read-only market-data panel below. The execution connection test remains disabled." : status.connection?.detail || "No credential configured.";
  const account = status.account || {};
  const rows = [
    ["Masked login", status.account_login_masked || profile?.login_masked || "Not configured"],
    ["Server", account.server || profile?.server || "Not configured"],
    ["Account type", account.account_trade_mode || "Not verified"],
    ["Currency", account.currency || "Not verified"],
    ["Trading permission", status.connected ? (account.trade_allowed && account.terminal_trade_allowed ? "Permitted by account/terminal" : "Disabled") : "Not verified"],
    ["Read-only test", info?.test_is_read_only === false ? "Not guaranteed" : "Initialize / identify / discover / reconcile only"],
    ["MT5 Python package", info ? (info.mt5_package_available ? "Installed (not necessarily connected)" : "Unavailable on this host") : "Checking…"],
  ];
  $("connection-account").innerHTML = rows.map(([key, value]) => `<dt>${escapeHtml(key)}</dt><dd>${escapeHtml(value)}</dd>`).join("");
}

function renderMarketData() {
  if (!$("market-data-state")) return;
  const status = currentStatus(); const paper = status.mode === "AXIOM_PAPER";
  const data = state.marketData || state.workspace?.market_data || {};
  const quote = data.quote || null; const fresh = data.state === "CONNECTED" && quote?.fresh;
  $("market-data-state").textContent = data.state || "DISCONNECTED";
  $("market-data-state").className = `status-pill ${fresh ? "ok" : data.authenticated ? "warn" : ""}`;
  $("market-data-detail").className = `inline-status ${data.state === "DEGRADED" || data.state === "UNAVAILABLE" ? "err" : fresh ? "ok" : "warn"}`;
  $("market-data-detail").textContent = data.detail || "Independent read-only market data is not connected.";
  const rows = [
    ["Execution", "LOCAL PAPER · local simulated fills only"],
    ["Credential profile", "VANTAGE_DEMO · Windows DPAPI"],
    ["Masked account", data.account?.login_masked || "Not verified"],
    ["MT5 server", data.account?.server || "Not verified"],
    ["Account type / currency", `${data.account?.account_trade_mode || "Not verified"} / ${data.account?.currency || "Not verified"}`],
    ["Discovered instruments", `${data.supported_instrument_count ?? 0} supported / ${data.instrument_count ?? 0} returned`],
    ["Selected exact symbol", data.selected_symbol || "Load an MT5 chart to subscribe"],
    ["Quote source time", quote ? timeText(quote.timestamp) : "No current quote"],
    ["Local receive time", quote ? timeText(quote.received_at) : "No current quote"],
    ["Freshness", quote ? (fresh ? `Fresh · ${Number(quote.age_seconds || 0).toFixed(1)}s source age` : "Stale/not advancing · new entries blocked") : "Not ready"],
    ["Automation / broker orders", "OFF / no order methods in this interface"],
  ];
  $("market-data-account").innerHTML = rows.map(([key, value]) => `<dt>${escapeHtml(key)}</dt><dd>${escapeHtml(value)}</dd>`).join("");
  const demoProfile = state.connectionInfo?.profiles?.VANTAGE_DEMO;
  const storeUnavailable = state.connectionInfo && !String(state.connectionInfo.secret_store || "").startsWith("Windows");
  setDisabled("connect-market-data", !paper || !demoProfile || storeUnavailable || data.state === "CONNECTING", !paper ? "This update's independent quote feed is restricted to LOCAL PAPER execution." : !demoProfile ? "Save the VANTAGE_DEMO credential locally first." : storeUnavailable ? "Windows DPAPI is unavailable." : "");
  setDisabled("disconnect-market-data", !data.authenticated, "Market data is already disconnected.");
  renderBoundary();
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
    : `${info.identity_contract}. Data configured: ${info.data_configured ? "yes" : "no"}; execution supported: ${info.execution_supported ? "yes" : "no"}; MT5/CFD substitution: prohibited. ${info.note || ""}`;
  $("listed-scope-badge").textContent = info.execution_supported ? "PROVIDER VERIFICATION REQUIRED" : "NO LISTED EXECUTION";
}

function renderChartContract() {
  const panel = $("chart-contract"); if (!panel) return;
  const payload = state.chart.payload || {}; const item = payload.instrument;
  if (state.chart.feed !== "vantage_mt5" || !item) {
    panel.innerHTML = `<p class="empty-state">${state.marketData?.authenticated ? "Load a valid MT5 chart to inspect its exact broker contract." : "No independent MT5 contract loaded. Replay/CSV metadata is not presented as broker metadata."}</p>`;
    return;
  }
  const fields = [
    ["Exact broker symbol", item.symbol], ["Product", productLabel(item.product_type)],
    ["CFD / ownership", item.is_cfd ? "CFD · no underlying share/ETF ownership" : "Not identified as a CFD by broker metadata"],
    ["Description / broker path", `${item.description || item.display_name || "Unavailable"} · ${item.path || "Unavailable"}`],
    ["Base / profit currency", `${item.currency_base || "Unavailable"} / ${item.currency_profit || "Unavailable"}`],
    ["Demo account currency", item.account_currency || "Unavailable"],
    ["Contract size", item.contract_size ?? "Unavailable"],
    ["MT5 volume min / max / step (lots; metadata only)", `${fmt(item.volume_min, 8)} / ${fmt(item.volume_max, 8)} / ${fmt(item.volume_step, 8)}`],
    ["Tick size / value", `${fmt(item.tick_size, 8)} / ${fmt(item.tick_value, 8)}`],
    ["Digits / point", `${item.digits ?? "Unavailable"} / ${fmt(item.point, 8)}`],
    ["Chart volume", payload.volume_kind || "Broker-supplied volume"],
    ["Metadata issues", (item.metadata_issues || []).join("; ") || "None"],
    ["Paper sizing", "Explicit units, not MT5 lots; no contract-size or FX conversion is implied"],
  ];
  panel.innerHTML = fields.map(([key, value]) => `<div class="contract-item"><span>${escapeHtml(key)}</span><strong>${escapeHtml(value)}</strong></div>`).join("");
}

function renderChartContext() {
  renderChartContract();
  const status = currentStatus(); const paper = status.mode === "AXIOM_PAPER";
  const mt5Paper = paper && state.chart.feed === "vantage_mt5";
  $("ticket-title").textContent = paper ? "Paper ticket · simulated" : `${MODE_LABELS[status.mode]} reviewed ticket`;
  $("volume-label").textContent = mt5Paper ? "Paper quantity (units, not MT5 lots)" : paper ? "Quantity" : "Volume (lots)";
  $("protection-fields").hidden = paper;
  $("order-disclaimer").textContent = paper
    ? mt5Paper ? "Paper uses explicit units, not MT5 lots or contract-size conversion. Fills are local simulations; SELL exits an existing position and short selling is not modelled." : "Paper fills are simulated locally. SELL exits an existing position; short selling is not modelled."
    : status.mode === "VANTAGE_DEMO" ? "MT5 demo order only. Review/precheck and demo fills do not establish live readiness or profitability." : "Real-money capability. Submission requires fresh ACTIVE lifecycle, server guards, review and exact final confirmation.";
  $("start-paper").hidden = !paper;
  if (paper) {
    const paperState = state.workspace.paper_session?.state;
    const mt5 = state.chart.feed === "vantage_mt5" && state.marketData?.authenticated;
    $("start-paper").textContent = paperState === "RUNNING" ? "Stop paper session" : paperState === "PAUSED" ? "Resume paper session" : mt5 ? "Start manual-only MT5 paper" : "Start automated replay (moving_average)";
  }
  const feedContext = $("chart-feed-context");
  if (feedContext) {
    const mt5Payload = state.chart.feed === "vantage_mt5" && state.chart.payload;
    feedContext.className = `inline-status ${mt5Payload ? (state.chart.payload.market_data ? "ok" : "err") : "warn"}`;
    feedContext.textContent = !paper ? "This chart belongs to the selected Vantage execution session; it is separate from LOCAL PAPER market data."
      : mt5Payload ? `${state.chart.payload.note || "Read-only MT5 closed bars and current tick."} Automation OFF; local reviewed fills only.`
      : state.marketData?.authenticated ? "Choose an account-discovered symbol and reload the MT5 chart. No DEMO/replay symbol is substituted."
      : "Replay/CSV is sample data, not market data. Default start enables moving_average automation and simulated strategy orders; manual-only MT5 paper and Daily Upstox PAPER are separate workflows.";
  }
  const noSymbol = !$("order-symbol").value;
  const disabledReason = state.submissionUncertain ? "An action outcome is uncertain. Reconcile before another order." : noSymbol ? "Load/select an instrument first." : !paper && !status.connected ? "Connect to the selected MT5 account first." : "";
  setDisabled("review-order", Boolean(disabledReason), disabledReason);
  setDisabled("reload-chart", !$("chart-symbol").value || (!paper && !status.connected), !$("chart-symbol").value ? "No instrument is available." : "Connect to MT5 first.");
  $("order-context").className = `inline-status ${status.emergency_stop || state.submissionUncertain ? "err" : status.active || paper ? "warn" : ""}`;
  $("order-context").textContent = state.submissionUncertain
    ? "An order/cancel/modify outcome is uncertain. Reconcile and inspect the durable intent before another action."
    : status.emergency_stop ? "Durable emergency stop blocks new entries. Risk-reducing exits remain subject to review."
    : paper ? "Review shows local price/headroom. Final result is explicitly simulated."
    : status.active ? "Active session. Review is read-only; submission repeats every server check." : "Review is available but will show lifecycle blockers until readiness, Arm and Activate complete.";
  $("order-buy").setAttribute("aria-pressed", String(state.side === "BUY"));
  $("order-sell").setAttribute("aria-pressed", String(state.side === "SELL"));
  document.querySelectorAll("[data-chart-timeframe]").forEach((button) => {
    const active = button.dataset.chartTimeframe === state.chart.timeframe;
    button.classList.toggle("active", active); button.setAttribute("aria-pressed", String(active));
  });
  for (const id of ["zoom-out", "zoom-in", "reset-chart-view", "clear-drawings"]) setDisabled(id, !(state.chart.bars || []).length, "Load valid chart bars first.");
  renderTicketEstimate();
}

async function selectMode() {
  const mode = $("session-mode").value;
  if (!MODE_LABELS[mode]) return;
  setDisabled("apply-mode", true, "Applying session selection…");
  try {
    await api("/api/trading/session", { body: { mode } });
    state.mode = mode; resetModeLocalState(); state.connectionInfo = null;
    await refreshAll();
    toast("Execution session selected", `${MODE_LABELS[mode]} is selected. No connection, Arm, Activate, or order was automatic.`, mode === "VANTAGE_LIVE" ? "warn" : "ok");
  } catch (error) { toast("Mode selection failed", error.message, "err"); $("session-mode").value = currentStatus().mode; }
  finally { setDisabled("apply-mode", false); }
}

async function loadInstruments() {
  if (state.instrumentLoading || !currentStatus().connected) return;
  state.instrumentLoading = true;
  $("instrument-status").className = "inline-status"; $("instrument-status").textContent = "Loading account-discovered contracts…";
  try {
    const query = new URLSearchParams({ q: $("instrument-search").value, product_type: $("product-filter").value, selected_only: String($("selected-only").checked) });
    const result = await api(`/api/trading/instruments?${query}`);
    state.instruments = result.instruments || [];
    state.selected = new Set(result.selected || []);
    state.instruments.forEach((item) => { state.productTypes.add(item.product_type); state.contracts[item.symbol] = item; });
    loadWatchlist(); renderProductFilter(); renderInstruments();
    $("instrument-status").className = "inline-status ok";
    $("instrument-status").textContent = `${result.total_matching} account-specific instrument(s) matched. Only the connected MT5 symbols_get universe is shown.`;
  } catch (error) {
    $("instrument-status").className = "inline-status err"; $("instrument-status").textContent = error.message;
    emptyRow("instrument-rows", 9, "Instrument discovery failed; no substitute universe is used.");
  } finally { state.instrumentLoading = false; }
}
function renderProductFilter() {
  const select = $("product-filter"); const current = select.value;
  select.innerHTML = `<option value="">All product types</option>` + [...state.productTypes].sort().map((type) => `<option value="${escapeHtml(type)}">${escapeHtml(productLabel(type))}</option>`).join("");
  select.value = current;
}
function productLabel(type) {
  return ({ SHARE_CFD: "Share CFD · no share ownership", ETF_CFD: "ETF CFD · no ETF ownership", FOREX: "Forex", INDEX_CFD: "Index CFD", COMMODITY_CFD: "Commodity CFD", METAL_CFD: "Metal CFD", CRYPTO_CFD: "Crypto CFD", LISTED_SHARE: "Listed share · separate provider", LISTED_ETF: "Listed ETF · separate provider", ETF: "ETF", UNKNOWN: "Unknown · untradable" })[type] || String(type || "UNKNOWN").replaceAll("_", " ");
}
function quoteFreshness(quote) {
  if (!quote) return ["No quote", "err"];
  const age = ageSeconds(quote.timestamp);
  if (!Number.isFinite(age) || age < 0) return ["Invalid time", "err"];
  return [`${age.toFixed(1)}s`, age > Number(currentStatus().risk_limits?.max_quote_age_seconds || 30) ? "err" : "ok"];
}
function renderInstruments() {
  const status = currentStatus();
  if (status.connected) loadWatchlist();
  const watchOnly = Boolean($("watchlist-only")?.checked);
  const rows = watchOnly ? state.instruments.filter((item) => state.watchlist.has(item.symbol)) : state.instruments;
  if (!status.connected) {
    $("instrument-status").className = "inline-status warn";
    $("instrument-status").textContent = "Connect with the read-only test on Connections. No hard-coded or website instrument universe is displayed.";
    emptyRow("instrument-rows", 9, "No account-discovered instruments while disconnected.");
  } else if (!rows.length) emptyRow("instrument-rows", 9, watchOnly ? "No watched symbols match the current account/search filters. Watching never selects execution." : "No instruments match the current filters.");
  else $("instrument-rows").innerHTML = rows.map((item) => {
    const [fresh, freshKind] = quoteFreshness(item.quote);
    const spread = item.quote ? Number(item.quote.ask) - Number(item.quote.bid) : null;
    const watched = state.watchlist.has(item.symbol);
    return `<tr><td><button type="button" class="watch-button" data-watch-symbol="${escapeHtml(item.symbol)}" aria-pressed="${watched}" aria-label="${watched ? "Remove" : "Add"} ${escapeHtml(item.symbol)} ${watched ? "from" : "to"} watchlist" title="Watchlist only; does not select execution">${watched ? "★" : "☆"}</button></td><td><input type="checkbox" data-select-symbol="${escapeHtml(item.symbol)}" ${state.selected.has(item.symbol) ? "checked" : ""} aria-label="Select ${escapeHtml(item.symbol)} for execution"></td><td><strong>${escapeHtml(item.symbol)}</strong><small>${escapeHtml(item.display_name || item.description)}</small></td><td>${statusPill(productLabel(item.product_type), item.tradable ? "" : "err")}${item.is_cfd ? "<small>CFD, not underlying ownership</small>" : ""}</td><td>${escapeHtml(item.trade_mode_label)}<small>${item.tradable ? "Metadata understood" : escapeHtml((item.metadata_issues || []).join("; "))}</small></td><td>${fmt(item.volume_min, 8)} / ${fmt(item.volume_step, 8)}</td><td>${fmt(item.tick_size, 8)} / ${escapeHtml(item.digits)}</td><td>${statusPill(fresh, freshKind)}<small>Spread ${spread === null ? "—" : fmt(spread, 8)}</small></td><td><button class="button secondary" data-inspect="${escapeHtml(item.symbol)}">Inspect</button></td></tr>`;
  }).join("");
  $("watchlist-status").textContent = status.connected
    ? `${state.watchlist.size} watched symbol(s) in this tab · ${rows.length} visible · ${state.selected.size} selected for execution. Watchlist edits never change selection, connection, Arm or Activate.`
    : "Watchlists are local to this tab and scoped to an opaque account fingerprint. Connect to edit an account-discovered watchlist.";
  setDisabled("reload-instruments", !status.connected, "Run the read-only MT5 connection first.");
  setDisabled("save-instruments", !status.connected || state.selected.size === 0, !status.connected ? "Connect before saving an account selection." : "Select at least one instrument; the API rejects an empty selection.");
  setDisabled("watch-visible", !status.connected || !state.instruments.length, "Load account-discovered rows first.");
  setDisabled("clear-watchlist", !status.connected || state.watchlist.size === 0, "The current scoped watchlist is empty.");
  for (const id of ["instrument-search", "product-filter", "selected-only", "watchlist-only"]) setDisabled(id, !status.connected, "Connect to discover this account's instruments.");
}
function inspectInstrument(symbol) {
  const item = state.contracts[symbol] || state.instruments.find((row) => row.symbol === symbol);
  if (!item) return;
  $("contract-title").textContent = `${item.symbol} · ${item.display_name || item.description || ""}`;
  $("contract-tradable").textContent = item.tradable ? "METADATA UNDERSTOOD" : "UNTRADABLE / INCOMPLETE";
  $("contract-tradable").className = `status-pill ${item.tradable ? "ok" : "err"}`;
  const quote = item.quote || {};
  const fields = [
    ["Broker symbol", item.symbol], ["Description", item.description], ["Broker path", item.path],
    ["Product", productLabel(item.product_type)], ["CFD", item.is_cfd ? "Yes — no underlying ownership" : "No / not identified as CFD"],
    ["Quote/profit currency", item.currency_profit], ["Base currency", item.currency_base], ["Account currency", item.account_currency],
    ["Contract size", item.contract_size], ["Volume min/max", `${fmt(item.volume_min, 8)} / ${fmt(item.volume_max, 8)}`],
    ["Volume step/limit", `${fmt(item.volume_step, 8)} / ${fmt(item.volume_limit, 8)}`], ["Tick size/value", `${fmt(item.tick_size, 8)} / ${fmt(item.tick_value, 8)}`],
    ["Digits / point", `${item.digits ?? "—"} / ${fmt(item.point, 8)}`], ["Order support", `Market ${item.supports_market ? "yes" : "no"} · Limit ${item.supports_limit ? "yes" : "no"}`],
    ["SL / TP", `${item.supports_stop_loss ? "SL yes" : "SL no"} · ${item.supports_take_profit ? "TP yes" : "TP no"}`],
    ["Filling modes", (item.supported_filling_modes || []).join(", ") || "Unavailable"], ["Trade mode", item.trade_mode_label],
    ["Stops/freeze points", `${item.stops_level_points ?? "—"} / ${item.freeze_level_points ?? "—"}`],
    ["Bid / ask", `${fmt(quote.bid)} / ${fmt(quote.ask)}`], ["Quote timestamp", timeText(quote.timestamp)],
    ["Current session info", item.sessions_available ? "Broker session data available" : "Next-session transition not exposed; current trade mode + fresh quote only"],
    ["Initial/maintenance margin", `${fmt(item.margin_initial)} / ${fmt(item.margin_maintenance)}`],
    ["Swap long/short", `${fmt(item.swap_long)} / ${fmt(item.swap_short)}`], ["Commission", item.commission_note],
    ["Metadata issues", (item.metadata_issues || []).join("; ") || "None"],
  ];
  $("contract-details").innerHTML = fields.map(([key, value]) => `<div class="contract-item"><span>${escapeHtml(key)}</span><strong>${escapeHtml(value ?? "Unavailable")}</strong></div>`).join("");
  $("contract-panel").scrollIntoView({ behavior: "smooth", block: "nearest" });
}
async function saveInstruments() {
  try {
    const symbols = [...state.selected];
    const result = await api("/api/trading/instruments/selection", { method: "PUT", body: { symbols } });
    state.readiness = null; state.activation = null;
    toast("Selection saved", `${result.selected.length} instrument(s). Prior Arm/Activate was invalidated.`, "ok");
    await loadWorkspace(); await loadInstruments();
  } catch (error) { toast("Selection rejected", error.message, "err"); }
}

async function loadChartSymbols() {
  const status = currentStatus(); const paper = status.mode === "AXIOM_PAPER";
  try {
    let symbols = []; let defaultSymbol = ""; let timeframes = null;
    const independentMT5 = paper && Boolean(state.marketData?.authenticated);
    if (independentMT5) {
      const result = await api("/api/market-data/instruments?supported_only=true");
      symbols = (result.instruments || []).map((item) => ({ symbol: item.symbol, timeframes: result.timeframes || [] }));
      symbols.forEach((item) => {
        const contract = result.instruments.find((row) => row.symbol === item.symbol);
        state.contracts[item.symbol] = contract;
      });
      defaultSymbol = result.selected || symbols[0]?.symbol || "";
      timeframes = result.timeframes;
    } else if (paper) {
      const result = await api("/chart/symbols"); symbols = result.symbols || []; defaultSymbol = result.default || "";
      timeframes = result.timeframes;
    } else if (status.connected) {
      symbols = (status.selected_symbols || []).map((symbol) => ({ symbol })); defaultSymbol = symbols[0]?.symbol || "";
      const result = await api("/api/trading/instruments?selected_only=true");
      (result.instruments || []).forEach((item) => { state.contracts[item.symbol] = item; });
    }
    const select = $("chart-symbol"); const previous = state.chart.symbol || select.value;
    select.innerHTML = symbols.length ? symbols.map((item) => `<option value="${escapeHtml(item.symbol)}">${escapeHtml(item.symbol)}</option>`).join("") : `<option value="">No instruments available</option>`;
    const chosen = symbols.some((item) => item.symbol === previous) ? previous : defaultSymbol;
    select.value = chosen || "";
    if (paper && chosen) {
      const entry = symbols.find((item) => item.symbol === chosen);
      const available = entry?.timeframes || timeframes || [];
      if (available.length) {
        $("chart-timeframe").innerHTML = available.map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`).join("");
        if (!available.includes(state.chart.timeframe)) state.chart.timeframe = available[0];
        $("chart-timeframe").value = state.chart.timeframe;
      }
    }
    state.chartSymbolsLoaded = true;
    const wantedFeed = independentMT5 ? "vantage_mt5" : "legacy";
    if (chosen && (!state.chart.payload || state.chart.symbol !== chosen || state.chart.mode !== status.mode || state.chart.feed !== wantedFeed)) await loadChart();
    else {
      $("order-symbol").value = chosen || "";
      state.chart.feed = wantedFeed;
      if (!chosen) {
        state.chart.bars = []; state.chart.payload = null; state.chart.quote = null; drawChart();
        $("chart-empty").textContent = independentMT5 ? "No supported account-discovered MT5 instrument is available; no substitute symbol is used." : paper ? "Start the paper session or load a configured dataset." : "Select at least one account instrument, then reload the chart.";
      }
      renderChartContext();
    }
  } catch (error) {
    if (paper && state.marketData?.authenticated) {
      $("chart-symbol").innerHTML = `<option value="">MT5 instruments unavailable</option>`;
      state.chart.bars = []; state.chart.payload = null; state.chart.quote = null; state.chart.feed = "vantage_mt5";
      $("order-symbol").value = ""; drawChart();
    }
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
  const status = currentStatus(); const paper = status.mode === "AXIOM_PAPER";
  const independentMT5 = paper && Boolean(state.marketData?.authenticated);
  const symbol = $("chart-symbol").value; const timeframe = $("chart-timeframe").value;
  if (!symbol) return;
  const requestId = ++state.chartRequest;
  setDisabled("reload-chart", true, "Loading chart…");
  $("chart-source").textContent = "LOADING REAL SESSION DATA"; $("chart-source").className = "status-pill warn";
  try {
    const query = new URLSearchParams({ symbol, timeframe, limit: "500" });
    if (paper && !independentMT5) query.set("indicators", "");
    const endpoint = independentMT5 ? "/api/market-data/chart" : paper ? "/chart/data" : "/api/trading/chart";
    const started = performance.now(); let payload;
    try { payload = await api(`${endpoint}?${query}`); }
    finally { observeBrowser("browser.chart.load", performance.now() - started); }
    if (requestId !== state.chartRequest) return;
    const bars = normalizeBars(payload.bars);
    const previousKey = chartKey();
    state.chart = { mode: status.mode, feed: independentMT5 ? "vantage_mt5" : "legacy", symbol, timeframe, bars, quote: payload.quote || null, payload };
    if (previousKey !== chartKey()) {
      state.chartView.endOffset = 0; state.chartView.pending = null; state.chartView.pointer = null;
      state.chartView.visible = Math.min(160, Math.max(20, bars.length || 20));
    }
    if (independentMT5) {
      state.marketData = await api("/api/market-data/status");
      renderMarketData(); renderFreshness();
    }
    if (requestId !== state.chartRequest) return;
    $("order-symbol").value = symbol;
    $("chart-source").textContent = payload.data_feed_label || payload.source_label || (paper ? "LOCAL PAPER DATA" : "VANTAGE MT5");
    $("chart-source").className = `status-pill ${payload.market_data ? "ok" : "warn"}`;
    $("chart-legend").textContent = `${bars.length} bars · ${payload.note || payload.data_feed_label || payload.source_label || "Confirmed source"} · generated ${timeText(payload.generated_at)}`;
    renderChartQuote(); drawChart(); renderChartContext(); updateOrderContract();
  } catch (error) {
    if (requestId !== state.chartRequest) return;
    if (independentMT5 || state.chart.symbol !== symbol || state.chart.mode !== status.mode) {
      state.chart.bars = []; state.chart.payload = null; state.chart.quote = null;
      if (independentMT5) state.chart.feed = "vantage_mt5";
      drawChart();
    }
    $("chart-source").textContent = independentMT5 ? "MT5 DATA UNAVAILABLE · NO REPLAY FALLBACK" : "CHART LOAD FAILED";
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
    const maxAge = Number(state.chart.payload?.max_quote_age_seconds || state.marketData?.max_quote_age_seconds || currentStatus().risk_limits?.max_quote_age_seconds || 30);
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
  const paper = currentStatus().mode === "AXIOM_PAPER";
  const symbol = $("order-symbol").value; const volume = Number($("order-volume").value);
  const contract = state.contracts[symbol] || state.chart.payload?.instrument || {};
  const quote = state.chart.symbol === symbol ? state.chart.quote : contract.quote;
  const side = state.side; const type = $("order-type").value;
  const limit = type === "LIMIT" ? Number($("order-limit").value) : null;
  const lastClose = Number(state.chart.bars?.at(-1)?.close);
  const quotePrice = side === "BUY" ? Number(quote?.ask) : Number(quote?.bid);
  const price = Number.isFinite(limit) && limit > 0 ? limit : Number.isFinite(quotePrice) && quotePrice > 0 ? quotePrice : lastClose;
  const contractSize = paper ? 1 : Number(contract.contract_size);
  const hasSize = Number.isFinite(contractSize) && contractSize > 0;
  const notional = Number.isFinite(volume) && volume > 0 && Number.isFinite(price) && price > 0 && hasSize ? volume * contractSize * price : null;
  const spread = Number.isFinite(Number(quote?.ask)) && Number.isFinite(Number(quote?.bid)) ? Number(quote.ask) - Number(quote.bid) : null;
  const spreadValue = spread !== null && spread >= 0 && notional !== null && Number.isFinite(price) && price > 0 ? spread * volume * contractSize : null;
  const currency = contract.currency_profit || state.workspace.account?.currency || "";
  const accountCurrency = contract.account_currency || state.workspace.account?.currency || "";
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
    ["Product", productLabel(contract.product_type || state.chart.payload?.product_type || (paper ? "LOCAL PAPER" : "UNKNOWN"))],
    ["Quantity", Number.isFinite(volume) && volume > 0 ? `${fmt(volume, 8)} ${paper ? "units" : "MT5 lots"}` : "Enter quantity"],
    ["Indicative value", notional === null ? "Unavailable until price + contract metadata" : `${money(notional, currency)}${paper ? " · units × price" : " · lots × contract size × price"}`],
    ["Bid / ask", quote ? `${fmt(quote.bid, 8)} / ${fmt(quote.ask, 8)}` : "No current bid/ask"],
    ["Spread", spread === null ? "Unavailable" : `${fmt(spread, 8)} price${spreadValue === null ? "" : ` · ${money(spreadValue, currency)} indicative`}`],
    ["Costs", reviewed?.precheck ? "Precheck shown in review; broker deals authoritative" : paper ? "Local review shows simulation context; configured costs apply" : "Commission/fees not invented; review + broker deals"],
    ["Margin", paper ? "No MT5 margin conversion; local cash/position bounds" : reviewed ? money(risk?.estimated_margin, accountCurrency) : "Calculated by MT5 in read-only review"],
    ["Risk", reviewed ? (risk?.allowed ? "Server review passed; final submit rechecks" : `Blocked: ${(risk?.issues || []).join("; ") || "see review"}`) : "Not reviewed · no submission"],
  ];
  panel.innerHTML = values.map(([key, value], index) => `<div class="ticket-value ${index >= 5 ? "wide" : ""}"><span>${escapeHtml(key)}</span><strong>${escapeHtml(value)}</strong></div>`).join("");
}

function updateOrderContract() {
  const contract = state.contracts[$("order-symbol").value];
  const paper = currentStatus().mode === "AXIOM_PAPER";
  const limit = $("order-type").querySelector('option[value="LIMIT"]');
  limit.disabled = Boolean(contract && !contract.supports_limit);
  if (limit.disabled && $("order-type").value === "LIMIT") { $("order-type").value = "MARKET"; $("limit-field").hidden = true; }
  for (const [id, supported] of [["order-sl", contract?.supports_stop_loss], ["order-tp", contract?.supports_take_profit]]) {
    $(id).disabled = !paper && Boolean(contract) && !supported;
    $(id).title = $(id).disabled ? "Broker contract does not support this protection field." : "";
  }
  if (contract && !paper) {
    $("order-volume").min = contract.volume_min ?? 0; $("order-volume").step = contract.volume_step ?? "any";
    if (!$("order-volume").value) $("order-volume").value = contract.volume_min ?? "";
  } else if (paper) {
    $("order-volume").min = 0; $("order-volume").step = "any";
  }
  renderTicketEstimate();
}

function orderFormValues() {
  const type = $("order-type").value; const volume = Number($("order-volume").value);
  if (!$("order-symbol").value || !Number.isFinite(volume) || volume <= 0) throw new ApiError("Select an instrument and enter a finite quantity/volume above zero");
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
    if (currentStatus().mode === "AXIOM_PAPER") await reviewPaper(values);
    else {
      const payload = { ...values, idempotency_key: newKey("order") };
      const review = await api("/api/trading/orders/review", { body: payload });
      showVantageReview(review, payload, "order");
    }
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
  const body = `<p><strong>LOCAL PAPER ONLY.</strong> This review and any result are simulated; no Vantage order is routed. One idempotency key is bound to this reviewed request.</p>${reviewGrid([
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
function showVantageReview(review, payload, kind) {
  const order = review.order || payload; const quote = review.quote || {}; const risk = review.risk || {};
  if (kind === "order") {
    state.ticketReview = { review, signature: ticketSignature(order) };
    renderTicketEstimate();
  }
  const live = review.mode === "VANTAGE_LIVE";
  const body = `<p><strong>${escapeHtml(review.mode)} · ${live ? "REAL-MONEY ORDER CAPABILITY" : review.mode === "VANTAGE_DEMO" ? "DEMO ORDER" : "ORDER"}.</strong> ${escapeHtml(review.note || "Review every field.")}</p>${reviewGrid([
    ["Server / account", `${review.server} · ${review.account?.login_masked}`], ["Lifecycle", review.lifecycle],
    ["Symbol / product", `${order.symbol || review.instrument?.symbol} · ${productLabel(review.instrument?.product_type)}`],
    ["Side / type", `${order.side} / ${order.order_type}`], ["Volume (lots)", order.volume],
    ["Limit", order.limit_price ?? "Market"], ["SL / TP", `${order.stop_loss ?? "—"} / ${order.take_profit ?? "—"}`],
    ["Bid / ask", `${fmt(quote.bid)} / ${fmt(quote.ask)}`], ["Quote time", timeText(quote.timestamp)],
    ["Estimated margin", fmt(risk.estimated_margin, 2)], ["Spread", `${fmt(risk.spread_bps, 2)} bps`],
    ["MT5 precheck", review.precheck ? `${review.precheck.retcode} · ${review.precheck.comment}` : "Not applicable"],
    ["Review expires", timeText(review.expires_at)],
  ])}${risk.issues?.length ? `<div class="review-issues"><strong>Submission blocked</strong><br>${escapeHtml(risk.issues.join("; "))}</div>` : `<div class="inline-status ok">All server review checks passed. Submission repeats reconciliation, contract, quote, margin, risk, lifecycle and duplicate checks.</div>`}`;
  openDialog({
    title: kind === "close" ? "Review position close" : `Review ${live ? "LIVE" : "DEMO"} order`, body,
    confirmLabel: kind === "close" ? "Submit reviewed close" : `Submit ${live ? "LIVE" : "DEMO"} order`,
    phrase: review.confirmation_phrase, confirmDisabled: !risk.allowed,
    onConfirm: () => submitVantageReview(review, payload),
  });
}
async function submitVantageReview(review, payload) {
  try {
    const started = performance.now(); let result;
    try { result = await api("/api/trading/orders/submit", { body: { ...payload, review_id: review.review_id, confirmation: review.confirmation_phrase } }); }
    finally { observeBrowser("browser.order.submit_roundtrip", performance.now() - started); }
    closeDialog(); await loadWorkspace(); await loadActivity();
    const order = result.order || {};
    const kind = UNRESOLVED.has(order.status) ? "warn" : "ok";
    toast(result.duplicate ? "Duplicate suppressed" : "MT5 result reconciled", `${order.status || "Unknown"} · ${order.reason || "Inspect Orders and broker confirmation."}`, kind, 10000);
  } catch (error) {
    if (uncertainError(error)) { state.submissionUncertain = true; await goPage("orders"); }
    throw error;
  }
}

async function closePosition(positionId) {
  const position = (state.workspace.positions || []).find((item) => String(item.position_id) === String(positionId));
  if (!position) { toast("Position unavailable", "Refresh/reconcile before closing.", "err"); return; }
  const payload = {
    symbol: position.symbol, side: position.side === "BUY" ? "SELL" : "BUY", order_type: "MARKET",
    volume: Number(position.volume), limit_price: null, stop_loss: null, take_profit: null,
    position_id: String(position.position_id), idempotency_key: newKey("close"),
  };
  try {
    const review = await api("/api/trading/orders/review", { body: payload });
    showVantageReview(review, payload, "close");
  } catch (error) { toast("Close review blocked", error.message, "err"); }
}
function modifyPosition(positionId) {
  const position = (state.workspace.positions || []).find((item) => String(item.position_id) === String(positionId));
  if (!position) return;
  const body = `<p>Modify only the SL/TP of MT5 position <strong>${escapeHtml(position.position_id)}</strong> (${escapeHtml(position.symbol)}). Leave a field blank to clear that protection. No volume or direction change is included.</p><div class="form-grid"><div class="field"><label for="modify-sl">New stop-loss</label><input id="modify-sl" type="number" min="0" step="any" value="${escapeHtml(position.stop_loss ?? "")}"></div><div class="field"><label for="modify-tp">New take-profit</label><input id="modify-tp" type="number" min="0" step="any" value="${escapeHtml(position.take_profit ?? "")}"></div></div><p class="muted small">The next step is read-only review. Modification requires ACTIVE lifecycle and is shown as MODIFIED only when the broker position confirms the values.</p>`;
  openDialog({
    title: "Prepare SL / TP modification", body, confirmLabel: "Review modification", confirmClass: "primary",
    onConfirm: async () => {
      const numberOrNull = (id) => $(id).value === "" ? null : Number($(id).value);
      const stop = numberOrNull("modify-sl"), take = numberOrNull("modify-tp");
      if ((stop !== null && (!Number.isFinite(stop) || stop <= 0)) || (take !== null && (!Number.isFinite(take) || take <= 0))) throw new ApiError("SL/TP must be blank or positive finite prices");
      const payload = { position_id: String(position.position_id), stop_loss: stop, take_profit: take, idempotency_key: newKey("modify") };
      const review = await api("/api/trading/positions/modify/review", { body: payload });
      showModificationReview(review, payload);
    },
  });
}
function showModificationReview(review, payload) {
  const position = review.position; const risk = review.risk || {};
  const body = `<p><strong>${escapeHtml(review.mode)} · POSITION PROTECTION.</strong> ${escapeHtml(review.note)}</p>${reviewGrid([
    ["Server / account", `${review.server} · ${review.account?.login_masked}`], ["Position", position.position_id],
    ["Symbol / side", `${position.symbol} / ${position.side}`], ["Current volume", position.volume],
    ["Current SL / TP", `${position.stop_loss ?? "None"} / ${position.take_profit ?? "None"}`],
    ["Proposed SL / TP", `${payload.stop_loss ?? "Clear"} / ${payload.take_profit ?? "Clear"}`],
    ["Quote bid / ask", `${fmt(review.quote?.bid)} / ${fmt(review.quote?.ask)}`], ["Review expires", timeText(review.expires_at)],
  ])}${risk.issues?.length ? `<div class="review-issues">${escapeHtml(risk.issues.join("; "))}</div>` : `<div class="inline-status ok">Review passed. Submission repeats all checks and rejects if the position changed since this review.</div>`}`;
  openDialog({
    title: "Review position modification", body, confirmLabel: "Submit reviewed modification", phrase: review.confirmation_phrase,
    confirmDisabled: !risk.allowed,
    onConfirm: async () => {
      const result = await api("/api/trading/positions/modify/submit", { body: { ...payload, review_id: review.review_id, confirmation: review.confirmation_phrase } });
      closeDialog(); await loadWorkspace(); await loadActivity();
      toast("Modification result", `${result.order?.status || "UNKNOWN"} · ${result.order?.reason || "Inspect broker position state."}`, UNRESOLVED.has(result.order?.status) ? "warn" : "ok", 10000);
    },
  });
}
function cancelOrder(intentId) {
  const paper = currentStatus().mode === "AXIOM_PAPER";
  const phrase = paper ? "" : `CANCEL ${currentStatus().mode === "VANTAGE_LIVE" ? "LIVE" : "DEMO"} ORDER`;
  openDialog({
    title: paper ? "Cancel simulated working order" : "Review MT5 order cancellation",
    body: `<p>${paper ? "This cancels only the local simulated working order." : "One cancel request will be attempted. CANCELLED is displayed only if broker history confirms it; an uncertain result must be reconciled, not retried."}</p>`,
    confirmLabel: paper ? "Cancel simulated order" : "Submit cancel request", phrase,
    onConfirm: async () => {
      const result = await api(paper ? `/paper/orders/${encodeURIComponent(intentId)}/cancel` : `/api/trading/orders/${encodeURIComponent(intentId)}/cancel`, { body: paper ? {} : { confirmation: phrase } });
      closeDialog(); await loadWorkspace(); await loadActivity();
      toast("Cancellation result", paper ? "Local simulated order CANCELLED" : result.cancel_confirmed ? "Broker history confirms CANCELLED" : "Cancellation is not confirmed; reconcile and do not retry blindly.", !paper && !result.cancel_confirmed ? "warn" : "ok");
    },
  });
}
async function reconcileNow() {
  setDisabled("reconcile-now", true, "Reconciling…");
  try {
    const result = await api("/api/trading/reconcile", { body: {} });
    state.submissionUncertain = Number(result.unresolved_intents || 0) > 0;
    await loadWorkspace(); await loadActivity();
    toast("Broker reconciliation complete", `${result.unresolved_intents ?? 0} unresolved intent(s). Remaining orders/positions come from the broker snapshot.`, result.unresolved_intents ? "warn" : "ok");
  } catch (error) { toast("Reconciliation failed", `${error.message}. Do not assume orders are cancelled or the account is flat.`, "err"); }
  finally { renderOrders(); }
}

async function runReadiness() {
  setDisabled("run-readiness", true, "Running readiness…");
  try {
    state.readiness = await api("/api/trading/readiness"); state.activation = null; renderRisk();
    toast("Readiness complete", state.readiness.ready ? "All checks passed. Arm remains a separate action and submits no order." : `${state.readiness.blockers.length} blocker(s) prevent Arm.`, state.readiness.ready ? "ok" : "warn");
  } catch (error) { toast("Readiness failed", error.message, "err"); }
  finally { setDisabled("run-readiness", false); }
}
function armSession() {
  const readiness = state.readiness; if (!readiness?.ready) return;
  openDialog({
    title: `Arm ${MODE_LABELS[currentStatus().mode]}`,
    body: `<p>Arm submits <strong>no order</strong> and does not start automation. It binds a short-lived activation challenge to the verified account <strong>${escapeHtml(currentStatus().account_login_masked)}</strong> on <strong>${escapeHtml(currentStatus().server)}</strong>.</p><p>Activate is a separate fresh confirmation.</p>`,
    confirmLabel: "Arm only — no orders", confirmClass: "warning", phrase: readiness.arm_phrase,
    onConfirm: async () => {
      const result = await api("/api/trading/arm", { body: { readiness_id: readiness.readiness_id, phrase: readiness.arm_phrase } });
      state.activation = result; closeDialog(); await loadWorkspace();
      toast("Session ARMED only", "No order was submitted. Complete the separate Activate challenge within 60 seconds if intended.", "warn");
    },
  });
}
function activateSession() {
  const activation = state.activation; if (!activation) return;
  const live = currentStatus().mode === "VANTAGE_LIVE";
  openDialog({
    title: `Activate ${MODE_LABELS[currentStatus().mode]}`,
    body: `<p>${live ? "This explicitly enables real-money order placement for the currently armed account, subject to every order review and server guard." : "This enables reviewed demo order submission for the currently armed demo account."}</p><p>Automation remains OFF. No order is submitted by Activate.</p>`,
    confirmLabel: live ? "Activate LIVE order capability" : "Activate DEMO order capability", phrase: activation.activation_phrase,
    onConfirm: async () => {
      await api("/api/trading/activate", { body: { activation_id: activation.activation_id, phrase: activation.activation_phrase } });
      state.activation = null; closeDialog(); await loadWorkspace();
      toast("Session ACTIVE", "No order was submitted by activation. Automation remains OFF.", live ? "warn" : "ok");
    },
  });
}
async function pauseSession() {
  try {
    const paper = currentStatus().mode === "AXIOM_PAPER";
    const result = await api(paper ? "/paper/pause" : "/api/trading/pause", { body: {} });
    if (!paper) state.activation = result;
    await loadWorkspace(); toast("Session paused", paper ? "Paper strategy paused; pending entries handled by the local simulator." : "New entries blocked. Working broker orders/positions are not assumed cancelled or closed.", "warn");
  } catch (error) { toast("Pause failed", error.message, "err"); }
}
function stopSession() {
  const paper = currentStatus().mode === "AXIOM_PAPER";
  openDialog({
    title: paper ? "Stop local paper session" : "Stop Vantage session",
    body: `<p>${paper ? "Stops the local strategy and cancels its simulated working orders. Positions and fills remain local paper records." : "Disarms the session, makes one cancel attempt per AXIOM-managed working order, and reconciles. External orders are left untouched. Stop does not claim positions are closed or the account is flat."}</p>`,
    confirmLabel: paper ? "Stop paper session" : "Stop and reconcile", confirmClass: "warning",
    onConfirm: async () => {
      const result = await api(paper ? "/paper/stop" : "/api/trading/stop", { body: {} });
      state.activation = null; state.readiness = null; closeDialog(); await loadWorkspace();
      toast("Stop result recorded", paper ? `Local paper session stopped · ${result.stop_reason || "OPERATOR_STOP"}.` : result.note || "Inspect broker-reported remainder.", "warn", 10000);
    },
  });
}
function emergencyStop() {
  const mode = currentStatus().mode;
  openDialog({
    title: "Set durable emergency stop",
    body: `<p><strong>This halts the selected execution session (${escapeHtml(MODE_LABELS[mode])})${state.mm.available ? " plus NSE & NASDAQ Live-Market Paper, Upstox Autonomy, and Alpaca Broker" : ""}, and persists across restart.</strong></p><p>${mode === "AXIOM_PAPER" ? "The local paper strategy stops and working simulated orders are cancelled. Protective SL/TP on open positions are preserved. New entries stay blocked until explicit clear." : "AXIOM sets the durable halt, cancels unfilled entry orders while preserving protective stops, and reconciles."}</p>`,
    confirmLabel: "Set emergency stop", onConfirm: async () => {
      const result = await api("/api/trading/emergency", { body: {} });
      if (state.mm.available) await api("/api/markets/emergency-stop", { body: { reason: "operator emergency stop" } }).catch(() => {});
      state.activation = null; state.readiness = null; closeDialog(); await loadWorkspace(); await loadActivity();
      if (state.mm.available) await loadMultiMarketOverview();
      toast("Durable emergency stop SET", result.note || "New entries blocked across sessions. Protective exits preserved.", "err", 12000);
    },
  });
}
function clearEmergency() {
  openDialog({
    title: "Clear durable emergency stop",
    body: "<p>Clearing does not connect, Arm, Activate, resume a strategy, or submit an order. Live execution remains OFF and disarmed.</p>",
    confirmLabel: "Clear stop — remain disarmed", confirmClass: "warning", phrase: "CLEAR EMERGENCY STOP",
    onConfirm: async () => {
      await api("/api/trading/emergency/clear", { body: { phrase: "CLEAR EMERGENCY STOP" } });
      if (state.mm.available) await api("/api/markets/emergency-stop/reset", { body: { confirm_phrase: "CLEAR EMERGENCY STOP" } }).catch(() => {});
      closeDialog(); await loadWorkspace();
      if (state.mm.available) await loadMultiMarketOverview();
      toast("Emergency stop cleared", "Still disarmed and LIVE OFF. No session or strategy was automatically resumed.", "ok");
    },
  });
}

async function saveCredential(replace) {
  const payload = {
    environment: credentialEnvironment(), login: $("mt5-login").value.trim(), password: $("mt5-password").value,
    server: $("mt5-server").value.trim(), terminal_path: $("mt5-path").value.trim(),
    label: $("mt5-label").value.trim(), replace_existing: replace,
  };
  try {
    await api("/api/trading/credentials", { method: "PUT", body: payload });
    $("mt5-password").value = ""; $("mt5-login").value = "";
    state.readiness = null; state.activation = null;
    await loadConnectionInfo(); await loadWorkspace();
    toast(replace ? "Credential replaced" : "Credential saved", "Password retained only in the OS-protected store. No connection or activation occurred.", "ok");
  } catch (error) {
    $("mt5-password").value = "";
    toast("Credential not saved", `${error.message}. Password field cleared; no secret was logged.`, "err");
  }
}
function deleteCredential() {
  const mode = credentialEnvironment();
  openDialog({
    title: "Delete protected Vantage credential",
    body: `<p>Delete the protected ${escapeHtml(MODE_LABELS[mode])} credential and masked metadata for this environment. Any matching connection is disconnected and disarmed. This does not delete broker orders or positions.</p>`,
    confirmLabel: "Delete protected credential",
    onConfirm: async () => {
      await api(`/api/trading/credentials/${encodeURIComponent(mode)}`, { method: "DELETE" });
      closeDialog(); state.credentialMode = null; state.connectionInfo = null;
      await loadConnectionInfo(); await loadWorkspace(); await loadMarketData(); toast("Credential deleted", "No protected password or full login remains in AXIOM's metadata. Matching market data was disconnected.", "ok");
    },
  });
}
async function testConnection() {
  setDisabled("test-connection", true, "Read-only test in progress…");
  $("connection-detail").className = "inline-status warn"; $("connection-detail").textContent = "Initializing official MT5 gateway, verifying identity, discovering instruments and reconciling. No order operation is performed.";
  try {
    const result = await api("/api/trading/connection/test", { body: {} });
    state.readiness = null; state.activation = null; state.instruments = []; state.chartSymbolsLoaded = false;
    await loadWorkspace(); await loadConnectionInfo();
    toast("Read-only connection succeeded", `${result.instruments_discovered} account-specific instruments discovered. Session remains DISARMED; no orders were modified.`, "ok", 9000);
  } catch (error) { await loadWorkspace({ background: true }).catch(() => {}); toast("MT5 connection failed", `${error.message}. No live/demo activation occurred.`, "err", 10000); }
  finally { renderConnections(); }
}
async function disconnectConnection() {
  try {
    await api("/api/trading/connection/disconnect", { body: {} });
    state.readiness = null; state.activation = null; state.instruments = []; state.chart = { mode: state.mode, feed: "legacy", symbol: "", timeframe: "1m", bars: [], quote: null, payload: null };
    await loadWorkspace(); drawChart(); toast("MT5 disconnected", "Session disarmed. Broker state is no longer current; empty views do not prove a flat account.", "warn");
  } catch (error) { toast("Disconnect failed", error.message, "err"); }
}

async function connectMarketData() {
  if (currentStatus().mode !== "AXIOM_PAPER") {
    toast("Market-data connection blocked", "Keep execution in LOCAL PAPER for this independent read-only feed.", "err"); return;
  }
  setDisabled("connect-market-data", true, "Authenticating read-only demo market data…");
  $("market-data-detail").className = "inline-status warn";
  $("market-data-detail").textContent = "Authenticating VANTAGE_DEMO through the separate read-only interface. Execution mode and lifecycle are not changed.";
  try {
    const result = await api("/api/market-data/connect", { body: {} });
    state.marketData = result; state.chartSymbolsLoaded = false;
    await loadWorkspace(); await loadMarketData();
    toast("Read-only demo market data connected", `${result.instrument_count} account-returned instruments. Execution remains LOCAL PAPER; no session, strategy, Arm, Activate, or order was automatic.`, "ok", 10000);
  } catch (error) {
    if (state.chart.feed === "vantage_mt5") { state.chart.bars = []; state.chart.payload = null; state.chart.quote = null; drawChart(); }
    await loadMarketData();
    toast("Market-data connection failed", `${error.message}. No replay fallback or execution change occurred.`, "err", 10000);
  } finally { renderConnections(); renderMarketData(); }
}
async function disconnectMarketData() {
  try {
    const result = await api("/api/market-data/disconnect", { body: {} });
    state.marketData = result;
    // Explicit operator disconnect ends the MT5 chart context. A subsequent
    // configured chart is intentionally loaded, never an implicit fallback.
    state.chart = { mode: state.mode, feed: "legacy", symbol: "", timeframe: "1m", bars: [], quote: null, payload: null };
    state.chartSymbolsLoaded = false;
    await loadWorkspace(); await loadMarketData();
    if (state.page === "charts") await loadChartSymbols();
    drawChart();
    toast("Independent market data disconnected", "New MT5-feed entries are blocked. Local paper records are preserved; execution remains LOCAL PAPER.", "warn");
  } catch (error) { toast("Market-data disconnect failed", error.message, "err"); }
}

function renderActivity() {
  if (!$("activity-rows")) return;
  const filter = $("activity-filter").value.trim().toLowerCase();
  const events = state.activity.filter((event) => !filter || `${eventName(event)} ${JSON.stringify(event.payload || {})}`.toLowerCase().includes(filter));
  if (!events.length) { emptyRow("activity-rows", 4, "No redacted audit events match."); return; }
  $("activity-rows").innerHTML = events.map((event) => `<tr><td>${escapeHtml(timeText(eventTime(event)))}</td><td>${escapeHtml(event.environment || "AXIOM_PAPER")}</td><td>${escapeHtml(eventName(event))}</td><td><small>${escapeHtml(JSON.stringify(event.payload || {}))}</small></td></tr>`).join("");
}

async function goPage(page) {
  if (!PAGE_TITLES[page]) return;
  state.page = page;
  document.querySelectorAll(".page").forEach((element) => { element.hidden = element.id !== `page-${page}`; });
  document.querySelectorAll(".nav-item").forEach((element) => {
    const active = element.dataset.page === page;
    element.classList.toggle("active", active);
    if (active) element.setAttribute("aria-current", "page"); else element.removeAttribute("aria-current");
  });
  $("page-eyebrow").textContent = PAGE_TITLES[page][0]; $("page-title").textContent = PAGE_TITLES[page][1];
  document.title = `${PAGE_TITLES[page][1]} · AXIOM Operator`;
  $("sidebar").classList.remove("open"); $("mobile-menu").setAttribute("aria-expanded", "false");
  if (page === "instruments") { renderInstruments(); if (currentStatus().connected && !state.instruments.length) await loadInstruments(); }
  if (page === "charts") await loadChartSymbols();
  if (page === "activity") await loadActivity();
  if (page === "modellab") await loadModelLabOverview();
  if (page === "monitor") await loadTradeMonitor();
  if (page === "ensemble") await loadEnsembleOverview();
  renderAll();
}

async function paperSessionControl() {
  const paperState = state.workspace.paper_session?.state;
  if (paperState === "RUNNING") { stopSession(); return; }
  try {
    let result;
    if (paperState === "PAUSED") result = await api("/paper/resume", { body: {} });
    else if (state.chart.feed === "vantage_mt5") {
      if (!state.marketData?.authenticated || !state.chart.payload?.market_data) throw new ApiError("Reconnect and reload a fresh advancing MT5 chart before starting paper");
      const symbol = $("chart-symbol").value;
      const timeframe = state.workspace.paper_session?.timeframe || state.chart.timeframe;
      result = await api("/paper/start", { body: {
        source: "vantage_mt5", symbol, timeframe, strategy: "manual_only", parameters: {},
        dataset: "sample.csv", replay_delay: 0,
      } });
    } else result = await api("/api/paper/start-default", { body: {} });
    await loadWorkspace(); await loadMarketData(); await loadChartSymbols();
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
  $("apply-mode").addEventListener("click", selectMode);
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
  let searchTimer;
  $("instrument-search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(loadInstruments, 300); });
  $("product-filter").addEventListener("change", loadInstruments); $("selected-only").addEventListener("change", loadInstruments);
  $("watchlist-only").addEventListener("change", renderInstruments);
  $("watch-visible").addEventListener("click", () => {
    state.watchlist = new Set([...state.watchlist, ...state.instruments.map((item) => item.symbol)].slice(0, 200)); saveWatchlist();
  });
  $("clear-watchlist").addEventListener("click", () => { state.watchlist = new Set(); saveWatchlist(); });
  $("reload-instruments").addEventListener("click", loadInstruments); $("save-instruments").addEventListener("click", saveInstruments);
  $("instrument-rows").addEventListener("change", (event) => {
    const symbol = event.target.dataset.selectSymbol; if (!symbol) return;
    if (event.target.checked) state.selected.add(symbol); else state.selected.delete(symbol);
    setDisabled("save-instruments", state.selected.size === 0, "Select at least one instrument.");
  });
  $("instrument-rows").addEventListener("click", (event) => {
    const watched = event.target.dataset.watchSymbol;
    if (watched) {
      if (state.watchlist.has(watched)) state.watchlist.delete(watched); else if (state.watchlist.size < 200) state.watchlist.add(watched);
      saveWatchlist(); return;
    }
    const symbol = event.target.dataset.inspect; if (symbol) inspectInstrument(symbol);
  });
  $("chart-symbol").addEventListener("change", () => { state.ticketReview = null; $("order-symbol").value = $("chart-symbol").value; loadChart(); });
  $("chart-timeframe").addEventListener("change", () => { state.ticketReview = null; loadChart(); });
  document.querySelectorAll("[data-chart-timeframe]").forEach((button) => button.addEventListener("click", () => {
    const timeframe = button.dataset.chartTimeframe;
    if (![...$("chart-timeframe").options].some((option) => option.value === timeframe)) { toast("Timeframe unavailable", "The active data provider does not expose this timeframe for the selected instrument.", "warn"); return; }
    $("chart-timeframe").value = timeframe; state.ticketReview = null; loadChart();
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
  $("reload-chart").addEventListener("click", loadChart); $("start-paper").addEventListener("click", paperSessionControl);
  $("order-buy").addEventListener("click", () => { state.side = "BUY"; state.ticketReview = null; $("order-buy").classList.add("active"); $("order-sell").classList.remove("active"); renderChartContext(); });
  $("order-sell").addEventListener("click", () => { state.side = "SELL"; state.ticketReview = null; $("order-sell").classList.add("active"); $("order-buy").classList.remove("active"); renderChartContext(); });
  $("order-type").addEventListener("change", () => { state.ticketReview = null; $("limit-field").hidden = $("order-type").value !== "LIMIT"; renderTicketEstimate(); });
  $("order-form").addEventListener("input", () => { state.ticketReview = null; renderTicketEstimate(); });
  $("order-form").addEventListener("submit", reviewOrder);
  $("reconcile-now").addEventListener("click", reconcileNow);
  $("position-rows").addEventListener("click", (event) => {
    const close = event.target.dataset.close, modify = event.target.dataset.modify, exit = event.target.dataset.paperExit;
    if (close) closePosition(close); if (modify) modifyPosition(modify);
    if (exit) {
      const position = state.workspace.positions.find((item) => item.symbol === exit);
      goPage("charts").then(() => { state.side = "SELL"; $("order-sell").classList.add("active"); $("order-buy").classList.remove("active"); $("order-symbol").value = exit; $("order-volume").value = position?.quantity || ""; });
    }
  });
  $("order-rows").addEventListener("click", (event) => { const id = event.target.dataset.cancel; if (id) cancelOrder(id); });
  $("run-readiness").addEventListener("click", runReadiness); $("arm-session").addEventListener("click", armSession); $("activate-session").addEventListener("click", activateSession);
  $("pause-session").addEventListener("click", pauseSession); $("stop-session").addEventListener("click", stopSession); $("clear-emergency").addEventListener("click", clearEmergency);
  $("risk-form").addEventListener("input", () => { state.riskDirty = true; }); $("risk-form").addEventListener("submit", (event) => event.preventDefault());
  $("save-risk").addEventListener("click", async () => {
    const body = {};
    for (const input of document.querySelectorAll("#risk-form input")) body[input.name] = Number(input.value);
    setDisabled("save-risk", true, "Saving risk limits…");
    try {
      await api("/api/trading/risk", { method: "PUT", body }); state.riskDirty = false; state.readiness = null; state.activation = null;
      await loadWorkspace(); toast("Risk limits saved", "Server-side bounds updated. Prior Arm/Activate was invalidated.", "ok");
    } catch (error) { toast("Risk limits rejected", error.message, "err"); }
    finally { renderRisk(); }
  });
  $("credential-form").addEventListener("submit", (event) => { event.preventDefault(); saveCredential(false); });
  $("replace-credential").addEventListener("click", () => saveCredential(true));
  $("edit-credential").addEventListener("click", () => { $("mt5-login").focus(); toast("Explicit credential replacement", "Enter the full login and password again, then choose Replace credential. Password fields are never repopulated.", "warn"); });
  $("delete-credential").addEventListener("click", deleteCredential); $("test-connection").addEventListener("click", testConnection); $("disconnect-connection").addEventListener("click", disconnectConnection);
  $("connect-market-data").addEventListener("click", connectMarketData); $("disconnect-market-data").addEventListener("click", disconnectMarketData);
  $("activity-filter").addEventListener("input", renderActivity); $("reload-activity").addEventListener("click", loadActivity);
  $("export-activity").addEventListener("click", () => downloadJson(`axiom-redacted-activity-${new Date().toISOString().slice(0, 10)}.json`, { generated_at: new Date().toISOString(), note: "Redacted local audit; no credentials or API token.", events: state.activity }));
  $("token-form").addEventListener("submit", async (event) => {
    event.preventDefault(); const candidate = $("settings-token").value;
    try {
      await api("/api/trading/status", { token: candidate, keepAuth: true }); state.token = candidate; storageSet("axiom-api-token", candidate); $("settings-token").value = "";
      toast("Local token updated for this tab", "The server configuration and Vantage credentials were not changed.", "ok");
    } catch (error) { $("settings-token").value = ""; toast("Token rejected", error.message, "err"); }
  });
  $("lock-app").addEventListener("click", () => { clearInterval(state.refreshTimer); state.workspace = null; showAuth("Tab locked. Broker/session state was not activated or changed."); });
  $("refresh-interval").addEventListener("change", configureRefresh);
  $("reload-performance").addEventListener("click", async () => {
    setDisabled("reload-performance", true, "Loading measurements…");
    await loadPerformance(); setDisabled("reload-performance", false);
  });
  $("download-diagnostics").addEventListener("click", () => downloadJson(`axiom-redacted-diagnostics-${new Date().toISOString().slice(0, 10)}.json`, {
    generated_at: new Date().toISOString(), note: "No API token, password, ciphertext, or full account login included. Review before sharing.",
    workspace: state.workspace, readiness: state.readiness, performance: state.performance, listed_assets: state.listedAssets,
    connection: state.connectionInfo ? { secret_store: state.connectionInfo.secret_store, profiles: state.connectionInfo.profiles, test_is_read_only: state.connectionInfo.test_is_read_only, mt5_package_available: state.connectionInfo.mt5_package_available, mt5_integration: state.connectionInfo.mt5_integration } : null,
  }));
  if (globalThis.ResizeObserver) new ResizeObserver(() => drawChart()).observe($("terminal-chart"));
  bindMultiMarketEvents();
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
    const ov = await api("/api/markets/overview", { keepAuth: true });
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

  if ($("mm-boundary-live")) {
    const nseGate = live.nse?.availability === "AVAILABLE_BEHIND_GATE" ? "GATED READY" : "NOT AVAILABLE";
    const usGate = live.nasdaq?.broker_status?.gate?.state === "LIVE_ACTIVE" ? "LIVE ACTIVE" : (live.nasdaq?.availability === "AVAILABLE_BEHIND_GATE" ? "GATED READY" : "OFF / LOCKED");
    $("mm-boundary-live").textContent = `NSE ${nseGate} · NASDAQ ${usGate}`;
  }

  // Overview NSE & NASDAQ Paper Ledger cards
  const renderLedgerList = (elId, badgeId, snap, symPrefix) => {
    const acct = snap.account || {};
    if ($(badgeId)) {
      $(badgeId).textContent = snap.session_state || "STOPPED";
      $(badgeId).className = `status-pill ${snap.session_state === "RUNNING" ? "ok" : snap.session_state === "EMERGENCY_STOPPED" ? "err" : "info"}`;
    }
    if ($(elId)) {
      const rows = [
        ["Execution mode", "LIVE-MARKET PAPER · Simulated · 0 Broker Orders"],
        ["Exchange session", `${snap.exchange_session?.state || "—"} (${snap.exchange_session?.timezone || ""})`],
        ["Equity", `${symPrefix}${fmt(acct.equity, 2)}`],
        ["Available cash", `${symPrefix}${fmt(acct.cash, 2)}`],
        ["Open market value", `${symPrefix}${fmt(acct.market_value, 2)} (${(snap.positions || []).length} pos)`],
        ["Day / Realized P&L", `${symPrefix}${fmt(acct.day_pnl, 2)} / ${symPrefix}${fmt(acct.realized_pnl, 2)}`],
        ["Regulatory fees simulated", `${symPrefix}${fmt(acct.total_fees_paid, 2)}`],
        ["Strategy", `${snap.strategy || "manual_only"} · ${snap.timeframe || "5m"}`],
      ];
      $(elId).innerHTML = rows.map(([k, v]) => `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(v)}</dd>`).join("");
    }
  };
  renderLedgerList("mm-nse-overview-list", "mm-nse-paper-state", nse, "₹");
  renderLedgerList("mm-nasdaq-overview-list", "mm-nasdaq-paper-state", nasdaq, "$");

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
        <div class="muted small">${escapeHtml(w.name || w.instrument_key)}</div>
        <div class="button-row">
          <button type="button" class="button secondary xs" data-mm-jump-chart="${escapeHtml(`${w.market}:${w.symbol}`)}">Chart</button>
          <button type="button" class="button primary xs" data-mm-jump-ticket="${escapeHtml(`${w.market}:${w.symbol}`)}">Paper Ticket</button>
          <button type="button" class="button danger-outline xs" data-mm-unwatch="${escapeHtml(`${w.market}:${w.symbol}`)}">Remove</button>
        </div>
      </div>
    `).join("") : `<p class="empty-state">No pinned watchlist symbols.</p>`;
  }

  if ($("mm-chart-watchlist-bar")) {
    $("mm-chart-watchlist-bar").innerHTML = allWatch.map((w) =>
      `<button type="button" class="wl-pill" data-mm-jump-chart="${escapeHtml(`${w.market}:${w.symbol}`)}"><span>${escapeHtml(w.market)}</span><strong>${escapeHtml(w.symbol)}</strong></button>`
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
        <td>${statusPill(`${p.market} · PAPER`, p.market === "NSE" ? "info" : "ok")}</td>
        <td><strong>${escapeHtml(p.symbol)}</strong></td>
        <td>${fmt(p.qty, 4)}</td>
        <td>${p.curSym}${fmt(p.avg_entry_price, 2)}</td>
        <td>${p.curSym}${fmt(p.last_price, 2)}</td>
        <td>${p.curSym}${fmt(p.market_value, 2)}</td>
        <td class="${Number(p.unrealized_pnl) >= 0 ? "pnl-pos" : "pnl-neg"}">${p.curSym}${fmt(p.unrealized_pnl, 2)} (${fmt(p.unrealized_pnl_pct, 2)}%)</td>
        <td>SL ${p.stop_loss ?? "—"} / TP ${p.take_profit ?? "—"}</td>
        <td>
          <div class="button-row">
            <button type="button" class="button secondary xs" data-mm-pos-prot="${escapeHtml(`${p.market}:${p.symbol}:${p.stop_loss ?? ""}:${p.take_profit ?? ""}`)}">SL/TP</button>
            <button type="button" class="button danger xs" data-mm-pos-close="${escapeHtml(`${p.market}:${p.symbol}`)}">Close</button>
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
        <td>${escapeHtml(o.market)}</td>
        <td><strong>${escapeHtml(o.symbol)}</strong></td>
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
        <td><small>${escapeHtml(timeText(f.created_at))}</small></td>
        <td><strong>${escapeHtml(f.market)}:${escapeHtml(f.symbol)}</strong></td>
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

  const allDecisions = [...(nse.decisions || []), ...(nasdaq.decisions || [])].slice(0, 40);
  if ($("mm-strategy-decision-rows")) {
    $("mm-strategy-decision-rows").innerHTML = allDecisions.length ? allDecisions.map((d) => `
      <tr>
        <td><small>${escapeHtml(timeText(d.created_at))}</small></td>
        <td>${escapeHtml(d.market)}</td>
        <td><strong>${escapeHtml(d.symbol)}</strong></td>
        <td>${escapeHtml(d.strategy)} · ${escapeHtml(d.timeframe)}</td>
        <td>${statusPill(d.action, d.action === "BUY" ? "ok" : d.action === "SELL" ? "err" : "")}</td>
        <td>${escapeHtml(d.reason)}</td>
        <td><code>${escapeHtml(d.order_client_id || "—")}</code></td>
      </tr>
    `).join("") : `<tr><td colspan="7" class="empty-state">No automated paper strategy decisions recorded yet.</td></tr>`;
  }

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
      return `<tr>
        <td><button type="button" class="button secondary xs" data-mm-watch="${escapeHtml(`${mkt}:${sym}`)}">+ Watch</button></td>
        <td>${statusPill(mkt, mkt === "NSE" ? "info" : "ok")}</td>
        <td><strong>${escapeHtml(sym)}</strong><br><small class="muted">${escapeHtml(it.instrument_key || sym)}</small></td>
        <td>${escapeHtml(it.name || sym)}</td>
        <td><code>${escapeHtml(it.asset_subclass || it.instrument_type || "EQ")}</code></td>
        <td>Lot ${fmt(it.quantity_step || it.lot_size || 1, 0)} · Tick ${fmt(it.tick_size, 4)}</td>
        <td>${it.tradable === false ? statusPill("WATCH ONLY", "warn") : escapeHtml(it.product_label || (it.fractionable ? "USD Cash · Fractionable" : "Cash / Delivery"))}</td>
        <td>
          <div class="button-row">
            <button type="button" class="button secondary xs" data-mm-jump-chart="${escapeHtml(`${mkt}:${sym}`)}">Chart</button>
            ${it.tradable === false ? "" : `<button type="button" class="button primary xs" data-mm-jump-ticket="${escapeHtml(`${mkt}:${sym}`)}">Trade Paper</button>`}
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
  const symbol = ($("mm-chart-symbol-input")?.value || (feedMode === "NSE" ? "RELIANCE" : "AAPL")).trim();
  const timeframe = $("chart-timeframe")?.value || "5m";
  if ($("mm-chart-feed-badge")) $("mm-chart-feed-badge").textContent = `${feedMode} LIVE READ-ONLY`;
  try {
    const q = new URLSearchParams({ market: feedMode, symbol, timeframe });
    const payload = await api(`/api/markets/chart?${q}`);
    const bars = normalizeBars(payload.bars || []);
    state.chart = {
      mode: currentStatus().mode,
      feed: `mm_${feedMode.toLowerCase()}`,
      symbol: payload.symbol || symbol,
      timeframe,
      bars,
      quote: payload.quote || null,
      payload,
    };
    state.chartView.endOffset = 0;
    state.chartView.visible = Math.min(160, Math.max(20, bars.length || 20));
    $("chart-source").textContent = payload.data_feed_label || `${feedMode} READ-ONLY`;
    $("chart-source").className = `status-pill ${connStatePillClass(payload.connection_state)}`;
    $("chart-feed-context").className = `inline-status ${bars.length ? "ok" : "warn"}`;
    $("chart-feed-context").textContent = bars.length
      ? `${payload.data_feed_label} · Source: ${payload.data_source} · ${bars.length} real OHLCV bars loaded.`
      : `${payload.data_feed_label} (${payload.connection_state}). AXIOM never substitutes sample or replay bars for live ${feedMode} market data.`;
    if (!bars.length && $("chart-empty")) {
      $("chart-empty").hidden = false;
      $("chart-empty").textContent = `${feedMode} live bars unavailable (${payload.connection_state}). Save ${feedMode === "NSE" ? "Upstox daily access_token" : "Alpaca Market Data API Key & Secret"} on Connections.`;
    }
    $("chart-legend").textContent = `${bars.length} real bars · ${payload.data_source} · checked ${timeText(payload.generated_at)}`;
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
  return {
    market: $("mm-order-market").value,
    symbol: $("mm-order-symbol").value.trim(),
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
}

function renderMultiMarketPreview(prev) {
  const box = $("mm-ticket-preview-box");
  if (!box || !prev) return;
  const c = prev.currency === "INR" ? "₹" : "$";
  const fees = prev.estimated_fees || {};
  box.innerHTML = `
    <div class="inline-status ${prev.ok ? "ok" : "err"}" style="margin-bottom:6px">
      <strong>${prev.ok ? "SIMULATED PREVIEW PASSED" : "BLOCKED BY GUARDRAILS"}</strong> · 0 Broker Calls
    </div>
    <div>Ref / Est Fill: <strong>${c}${fmt(prev.reference_price, 2)} → ${c}${fmt(prev.estimated_fill_price, 2)}</strong></div>
    <div>Est Notional: <strong>${c}${fmt(prev.estimated_notional, 2)}</strong> · Total Fees: <strong>${c}${fmt(fees.total, 2)}</strong></div>
    <div class="muted small">Quote: ${escapeHtml(prev.quote_source || "unavailable")} (${prev.quote_age_seconds !== null && prev.quote_age_seconds !== undefined ? `${prev.quote_age_seconds}s old` : "no fresh quote"})</div>
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
      const [mkt, sym] = target.dataset.mmJumpChart.split(":");
      await goPage("charts");
      $("mm-chart-source-mode").value = mkt;
      $("mm-chart-symbol-input").value = sym;
      await loadMultiMarketChart();
    } else if (target.dataset.mmJumpTicket) {
      const [mkt, sym] = target.dataset.mmJumpTicket.split(":");
      await goPage("charts");
      switchTicketTab("MULTIMARKET");
      $("mm-order-market").value = mkt;
      $("mm-order-symbol").value = sym;
      $("mm-ticket-currency-badge").textContent = mkt === "NSE" ? "INR ₹" : "USD $";
    } else if (target.dataset.mmWatch) {
      const [market, symbol] = target.dataset.mmWatch.split(":");
      try {
        await api("/api/markets/watchlists", { body: { market, symbol } });
        await loadMultiMarketOverview();
        toast("Added to watchlist", `${market}:${symbol} pinned.`, "ok");
      } catch (err) { toast("Watchlist failed", err.message, "err"); }
    } else if (target.dataset.mmUnwatch) {
      const [market, symbol] = target.dataset.mmUnwatch.split(":");
      try {
        await api(`/api/markets/watchlists/${encodeURIComponent(market)}/${encodeURIComponent(symbol)}`, { method: "DELETE" });
        await loadMultiMarketOverview();
        toast("Removed from watchlist", `${market}:${symbol} removed.`, "ok");
      } catch (err) { toast("Watchlist failed", err.message, "err"); }
    } else if (target.dataset.mmCancelOrder) {
      try {
        await api(`/api/markets/paper/orders/${encodeURIComponent(target.dataset.mmCancelOrder)}`, { method: "DELETE" });
        await loadMultiMarketOverview();
        toast("Simulated paper order canceled", target.dataset.mmCancelOrder, "ok");
      } catch (err) { toast("Cancel failed", err.message, "err"); }
    } else if (target.dataset.mmPosClose) {
      const [mkt, sym] = target.dataset.mmPosClose.split(":");
      try {
        await api(`/api/markets/paper/positions/${encodeURIComponent(mkt)}/${encodeURIComponent(sym)}/close`, { body: {} });
        await loadMultiMarketOverview();
        toast("Paper position closed", `${mkt}:${sym} closed against live quote.`, "ok");
      } catch (err) { toast("Position close blocked", err.message, "err"); }
    } else if (target.dataset.mmPosProt) {
      const [mkt, sym, slCur, tpCur] = target.dataset.mmPosProt.split(":");
      openDialog({
        title: `Update ${mkt}:${sym} Paper Protection (SL / TP)`,
        body: `<div class="form-grid"><div class="field"><label for="mm-mod-sl">Stop-loss</label><input id="mm-mod-sl" type="number" step="any" value="${escapeHtml(slCur)}"></div><div class="field"><label for="mm-mod-tp">Take-profit</label><input id="mm-mod-tp" type="number" step="any" value="${escapeHtml(tpCur)}"></div></div>`,
        confirmLabel: "Save SL / TP",
        confirmClass: "primary",
        onConfirm: async () => {
          const stop_loss = $("mm-mod-sl").value ? Number($("mm-mod-sl").value) : null;
          const take_profit = $("mm-mod-tp").value ? Number($("mm-mod-tp").value) : null;
          await api("/api/markets/paper/positions/protection", { body: { market: mkt, symbol: sym, stop_loss, take_profit } });
          await loadMultiMarketOverview();
          toast("Protection updated", `${mkt}:${sym} SL/TP updated.`, "ok");
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
  $("mm-chart-source-mode")?.addEventListener("change", loadMultiMarketChart);
  $("mm-load-live-chart-btn")?.addEventListener("click", loadMultiMarketChart);
  $("ticket-tab-session")?.addEventListener("click", () => switchTicketTab("SESSION"));
  $("ticket-tab-mm")?.addEventListener("click", () => switchTicketTab("MULTIMARKET"));

  $("mm-order-market")?.addEventListener("change", () => {
    const m = $("mm-order-market").value;
    $("mm-ticket-currency-badge").textContent = m === "NSE" ? "INR ₹" : "USD $";
    if (m === "NASDAQ" && $("mm-order-symbol").value === "RELIANCE") $("mm-order-symbol").value = "AAPL";
    if (m === "NSE" && $("mm-order-symbol").value === "AAPL") $("mm-order-symbol").value = "RELIANCE";
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

  // Strategy Session Controls (NSE & NASDAQ)
  const bindPaperLifecycle = (mkt, prefix) => {
    $(`${prefix}-start-btn`)?.addEventListener("click", async () => {
      try {
        const strategy = $(`mm-strat-${mkt.toLowerCase()}-select`).value;
        const timeframe = $(`mm-strat-${mkt.toLowerCase()}-tf`).value;
        const wl = (state.mm.overview?.watchlists?.[mkt] || []).map((w) => w.symbol);
        await api("/api/markets/paper/session/start", { body: { market: mkt, strategy, timeframe, symbols: wl } });
        await loadMultiMarketOverview();
        toast(`${mkt} Paper Session Started`, `Strategy=${strategy} (${timeframe}) · 0 broker orders.`, "ok");
      } catch (err) { toast(`${mkt} start blocked`, err.message, "err"); }
    });
    for (const act of ["pause", "resume", "stop"]) {
      $(`${prefix}-${act}-btn`)?.addEventListener("click", async () => {
        try {
          await api("/api/markets/paper/session/transition", { body: { market: mkt, action: act, reason: `operator ${act}` } });
          await loadMultiMarketOverview();
          toast(`${mkt} Paper ${act.toUpperCase()}`, "Session state updated.", "ok");
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

  if ($("ml-kronos-details")) {
    const plan = k.install_plan || {};
    const rows = [
      ["Provider State", pState],
      ["Upstream Source", `${plan.upstream?.repository || "shiyu-coder/Kronos"} @ ${String(plan.upstream?.commit || "67b630e67f6a").slice(0, 12)}`],
      ["License", k.license || "MIT License"],
      ["Model Pin", `NeoQuasar/Kronos-small @ ${String(kEngine.model_revision || plan.model?.revision || "").slice(0, 12)}`],
      ["Tokenizer Pin", `NeoQuasar/Kronos-Tokenizer-base @ ${String(kEngine.tokenizer_revision || plan.tokenizer?.revision || "").slice(0, 12)}`],
      ["Manifest SHA-256", `${String(kEngine.manifest_sha256 || plan.manifest_sha256 || "—").slice(0, 20)}…`],
      ["Weights Verified", String(Boolean(kEngine.artifact_verified))],
      ["Self-Test Verdict", kEngine.last_self_test?.status || "NOT_VERIFIED"],
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
        <td><code>${escapeHtml(String(d.sha256 || "").slice(0, 14))}…</code></td>
      </tr>
    `).join("") : `<tr><td colspan="8" class="empty-state">No datasets registered.</td></tr>`;
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
        <td>${statusPill(m.registry_status, stCls)}</td>
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
    $("ens-ablation-summary").textContent =
      `Run ${abl.ablation_id} (${abl.market} · ${abl.dataset?.dataset_id} · real_market_data=${abl.dataset?.real_market_data}): ` +
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

function init() {
  bindEvents(); bindModelLabEvents(); bindTradeMonitorEvents(); bindEnsembleEvents();
  $("refresh-interval").value = "10000"; renderActivity();
  const saved = storageGet("axiom-api-token");
  if (saved) unlock(saved); else showAuth();
}
init();
