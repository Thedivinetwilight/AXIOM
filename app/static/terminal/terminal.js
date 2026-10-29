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
  overview: ["WORKSPACE", "Overview"], instruments: ["ACCOUNT UNIVERSE", "Instruments"],
  charts: ["MARKET & EXECUTION", "Charts & order"], orders: ["BROKER STATE", "Orders & positions"],
  risk: ["FAIL-CLOSED CONTROLS", "Risk & lifecycle"], connections: ["MT5 CONNECTION", "Connections"],
  activity: ["LOCAL AUDIT", "Activity & audit"], settings: ["PREFERENCES & HELP", "Settings & help"],
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
  performance: { backend: null, browser: {} }, listedAssets: null, ticketReview: null,
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
async function refreshAll() {
  await loadWorkspace();
  await Promise.all([loadActivity(), loadConnectionInfo(), loadMarketData(), loadPerformance(), loadListedAssets()]);
  if (state.page === "instruments" && currentStatus().connected) await loadInstruments();
  if (state.page === "charts") await loadChartSymbols();
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
  ctx.clearRect(0, 0, width, height); ctx.fillStyle = "#080f17"; ctx.fillRect(0, 0, width, height);
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
  const left = 12, right = 76, top = 20, bottom = 31;
  const plotW = Math.max(1, width - left - right), plotH = Math.max(1, height - top - bottom);
  const high = Math.max(...visible.map((bar) => Number(bar.high))); const low = Math.min(...visible.map((bar) => Number(bar.low)));
  const padding = Math.max((high - low) * .08, high * .0001); const max = high + padding, min = low - padding;
  const step = plotW / visible.length; const bodyW = Math.max(1, Math.min(12, step * .68));
  const y = (price) => top + (max - Number(price)) / (max - min || 1) * plotH;
  const xForIndex = (index) => left + (index - view.start + .5) * step;
  const geometry = { left, right, top, bottom, plotW, plotH, max, min, start: view.start, end: view.end, step, width, height };
  state.chartView.geometry = geometry;
  ctx.font = "10px ui-sans-serif, system-ui"; ctx.textBaseline = "middle";
  for (let index = 0; index <= 5; index += 1) {
    const gy = top + plotH * index / 5; const price = max - (max - min) * index / 5;
    ctx.strokeStyle = "#1b2a39"; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(left, gy); ctx.lineTo(left + plotW, gy); ctx.stroke();
    ctx.fillStyle = "#8298ac"; ctx.fillText(fmt(price, 6), left + plotW + 8, gy);
  }
  visible.forEach((bar, localIndex) => {
    const x = left + step * localIndex + step / 2; const up = Number(bar.close) >= Number(bar.open);
    ctx.strokeStyle = up ? "#70dfad" : "#ff8999"; ctx.fillStyle = up ? "#70dfad" : "#ff8999";
    ctx.beginPath(); ctx.moveTo(x, y(bar.high)); ctx.lineTo(x, y(bar.low)); ctx.stroke();
    const bodyTop = y(Math.max(Number(bar.open), Number(bar.close)));
    const bodyBottom = y(Math.min(Number(bar.open), Number(bar.close)));
    ctx.fillRect(x - bodyW / 2, bodyTop, bodyW, Math.max(1, bodyBottom - bodyTop));
  });
  const indicator = $("chart-indicator")?.value || "NONE";
  if (["EMA20", "BOTH"].includes(indicator) && bars.length) drawIndicator(ctx, indicatorSeries("EMA", 20), view, geometry, "#85b5ff");
  if (["SMA50", "BOTH"].includes(indicator) && bars.length) drawIndicator(ctx, indicatorSeries("SMA", 50), view, geometry, "#f4c676");
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
    body: `<p><strong>This affects the selected execution session (${escapeHtml(MODE_LABELS[mode])}) and persists across restart.</strong></p><p>${mode === "AXIOM_PAPER" ? "The local paper strategy stops and working simulated orders are cancelled. New entries/resume stay blocked until explicit clear." : "AXIOM first sets the durable halt, then attempts to cancel managed working orders once and reconciles. It does not assume positions are closed or the account is flat."}</p>`,
    confirmLabel: "Set emergency stop", onConfirm: async () => {
      const result = await api("/api/trading/emergency", { body: {} });
      state.activation = null; state.readiness = null; closeDialog(); await loadWorkspace(); await loadActivity();
      toast("Durable emergency stop SET", result.note || "New entries blocked. Inspect broker/local remainder.", "err", 12000);
    },
  });
}
function clearEmergency() {
  openDialog({
    title: "Clear durable emergency stop",
    body: "<p>Clearing does not connect, Arm, Activate, resume a strategy, or submit an order. Vantage remains DISARMED and requires fresh readiness.</p>",
    confirmLabel: "Clear stop — remain disarmed", confirmClass: "warning", phrase: "CLEAR EMERGENCY STOP",
    onConfirm: async () => {
      await api("/api/trading/emergency/clear", { body: { phrase: "CLEAR EMERGENCY STOP" } });
      closeDialog(); await loadWorkspace(); toast("Emergency stop cleared", "Still disarmed. No session or strategy was automatically resumed.", "ok");
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
}

function init() {
  bindEvents(); $("refresh-interval").value = "10000"; renderActivity();
  const saved = storageGet("axiom-api-token");
  if (saved) unlock(saved); else showAuth();
}
init();
