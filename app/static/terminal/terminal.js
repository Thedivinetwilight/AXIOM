"use strict";

/* AXIOM operator shell. No credential or API token is ever written to the DOM
 * by an API response, logged, exported, or persisted outside tab sessionStorage. */
const $ = (id) => document.getElementById(id);
const MODE_LABELS = {
  AXIOM_PAPER: "AXIOM PAPER",
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
  token: "", workspace: null, activity: [], connectionInfo: null,
  page: "overview", mode: null, side: "BUY", readiness: null, activation: null,
  instruments: [], selected: new Set(), productTypes: new Set(), instrumentLoading: false,
  contracts: {}, chart: { mode: null, symbol: "", timeframe: "1m", bars: [], quote: null, payload: null },
  chartSymbolsLoaded: false, riskDirty: false, credentialMode: null,
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
async function refreshAll() {
  await loadWorkspace();
  await Promise.all([loadActivity(), loadConnectionInfo()]);
  if (state.page === "instruments" && currentStatus().connected) await loadInstruments();
  if (state.page === "charts") await loadChartSymbols();
}
function resetModeLocalState() {
  state.readiness = null; state.activation = null; state.riskDirty = false;
  state.instruments = []; state.selected = new Set(); state.contracts = {};
  state.chart = { mode: state.mode, symbol: "", timeframe: "1m", bars: [], quote: null, payload: null };
  state.chartSymbolsLoaded = false; state.submissionUncertain = false;
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
  renderMode(); renderOverview(); renderOrders(); renderRisk(); renderConnections(); renderChartContext(); renderActivity();
}
function badgeClass(mode) { return mode === "VANTAGE_LIVE" ? "live" : mode === "VANTAGE_DEMO" ? "demo" : "paper"; }
function renderMode() {
  const status = currentStatus(); const mode = status.mode; const label = MODE_LABELS[mode] || mode;
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
  const lifecycle = status.emergency_stop ? "EMERGENCY STOP" : status.lifecycle || "DISARMED";
  $("global-lifecycle").textContent = lifecycle;
  $("global-lifecycle").className = `status-pill ${status.emergency_stop ? "err" : status.active ? "ok" : status.armed ? "warn" : ""}`;
  $("global-lock-note").textContent = status.emergency_stop
    ? "Durable stop blocks new entries across restart."
    : mode === "VANTAGE_LIVE"
      ? (status.active ? "Real order placement explicitly activated; automation remains OFF." : "Real order placement blocked until Arm and separate Activate.")
      : mode === "VANTAGE_DEMO" ? "Demo orders only; demo results do not prove live readiness." : "Independent local paper; no Vantage order routing.";
  $("ticket-mode").textContent = label; $("ticket-mode").className = `status-pill ${status.active ? "ok" : "warn"}`;
  $("deals-label").textContent = mode === "AXIOM_PAPER" ? "LOCAL SIMULATED FILLS" : mode === "VANTAGE_DEMO" ? "MT5 DEMO DEALS" : "MT5 LIVE DEALS";
  renderFreshness();
}
function renderFreshness() {
  if (!state.workspace) return;
  const status = currentStatus();
  if (state.lastError) {
    $("global-freshness").innerHTML = statusPill("REFRESH FAILED · LAST VIEW MAY BE STALE", "err");
    $("global-freshness").title = state.lastError; return;
  }
  if (status.mode === "AXIOM_PAPER") {
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
  $("metric-realized").textContent = money(workspace.pnl?.realized_visible_history, currency);
  $("realized-window").textContent = workspace.pnl?.history_window_days ? `${workspace.pnl.history_window_days}-day visible broker history` : "visible local/broker history only";
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
  $("overview-risk").innerHTML = `
    <div class="inline-status ${status.emergency_stop ? "err" : status.active ? "ok" : "warn"}">${escapeHtml(status.emergency_stop ? "Durable emergency stop is SET" : `${status.lifecycle || "DISARMED"} · automation OFF`)}</div>
    <dl class="detail-list"><dt>Vantage risk fields</dt><dd>${status.mode === "AXIOM_PAPER" ? "Separate legacy paper limits" : `${configured}/${Object.keys(limits).length} above zero`}</dd><dt>Unresolved intents</dt><dd>${unresolved}</dd><dt>Live placement</dt><dd>${status.mode === "VANTAGE_LIVE" && status.active ? "Explicitly active" : "Blocked"}</dd></dl>`;
  const quotes = workspace.quotes || {};
  const quoteEntries = Object.entries(quotes);
  $("overview-quotes").innerHTML = quoteEntries.length ? quoteEntries.map(([symbol, quote]) => {
    const age = ageSeconds(quote.timestamp);
    return `<div class="quote-row"><div><strong>${escapeHtml(symbol)}</strong><small>${escapeHtml(timeText(quote.timestamp))}</small></div><div><strong>${fmt(quote.bid)} / ${fmt(quote.ask)}</strong><small>${Number.isFinite(age) ? `${age.toFixed(1)}s old` : "freshness unknown"}</small></div></div>`;
  }).join("") : `<p class="empty-state">${status.mode === "AXIOM_PAPER" ? "Open Charts to load the independent paper feed." : status.connected ? "Select instruments and reload quotes/charts." : "No broker quotes while disconnected."}</p>`;
  const checks = operatorChecklist();
  $("overview-checklist").innerHTML = checks.map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  renderOverviewActivity();
}
function operatorChecklist() {
  const status = currentStatus();
  if (status.mode === "AXIOM_PAPER") return [
    "Start or load the independent local paper session on Charts.",
    "Review the feed label: replay data is not market data; fills are always simulated.",
    "Use a reviewed manual ticket or the legacy strategy controls; no Vantage order is routed.",
  ];
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
  $("lifecycle-current").textContent = paper ? `PAPER · ${state.workspace.paper_session?.state || "STOPPED"}` : status.lifecycle || "DISARMED";
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

function renderConnections() {
  const status = currentStatus(); const paper = status.mode === "AXIOM_PAPER";
  const info = state.connectionInfo; const secretStatus = info?.secret_store || "Checking protected storage…";
  $("secret-store-status").textContent = secretStatus.startsWith("Windows") ? "DPAPI PROTECTED" : "PROTECTED STORE UNAVAILABLE";
  $("secret-store-status").className = `status-pill ${secretStatus.startsWith("Windows") ? "ok" : "err"}`;
  $("credential-environment").textContent = paper ? "Select a Vantage environment" : `${MODE_LABELS[status.mode]} credential`;
  const profile = info?.profiles?.[status.mode] || status.credential;
  if (state.credentialMode !== status.mode) {
    state.credentialMode = status.mode;
    for (const id of ["mt5-login", "mt5-password", "mt5-server", "mt5-path", "mt5-label"]) $(id).value = "";
    if (profile) {
      $("mt5-server").value = profile.server || ""; $("mt5-path").value = profile.terminal_path || ""; $("mt5-label").value = profile.label || "";
    } else if (status.mode === "VANTAGE_LIVE") $("mt5-server").value = "VantageMarkets-Live 13";
  }
  const storeUnavailable = !secretStatus.startsWith("Windows") && Boolean(info);
  document.querySelectorAll("#credential-form input").forEach((input) => {
    input.disabled = paper || storeUnavailable;
    input.title = paper ? "Select VANTAGE DEMO or VANTAGE LIVE first." : storeUnavailable ? "OS-protected storage is unavailable; credentials cannot be entered safely." : "";
  });
  setDisabled("save-credential", paper || storeUnavailable || Boolean(profile), paper ? "Select a Vantage environment." : storeUnavailable ? "DPAPI is unavailable; no plaintext fallback exists." : "Credential exists; use explicit Replace.");
  setDisabled("replace-credential", paper || storeUnavailable || !profile, paper ? "Select a Vantage environment." : !profile ? "No existing credential to replace." : storeUnavailable ? "DPAPI is unavailable." : "");
  setDisabled("edit-credential", paper || storeUnavailable || !profile, paper ? "Select a Vantage environment." : !profile ? "Save a credential first." : "Protected storage is unavailable.");
  setDisabled("delete-credential", paper || !profile, paper ? "Select a Vantage environment." : "No saved credential to delete.");
  setDisabled("test-connection", paper || !profile || storeUnavailable, paper ? "Select a Vantage environment." : !profile ? "Save a credential first." : "Protected storage is unavailable.");
  setDisabled("disconnect-connection", paper || (!status.connected && status.lifecycle === "DISARMED"), paper ? "No MT5 connection in local paper mode." : "Already disconnected and disarmed.");
  $("connection-state").textContent = paper ? "LOCAL PAPER ONLY" : status.connection?.state || "NOT CONFIGURED";
  $("connection-state-dot").className = `connection-dot ${status.connected ? "ok" : paper ? "" : "warn"}`;
  $("connection-detail").className = `inline-status ${status.connected ? "ok" : paper ? "" : "warn"}`;
  $("connection-detail").textContent = paper ? "Vantage credential controls are disabled. Select a Vantage mode explicitly; this never connects or activates automatically." : status.connection?.detail || "No credential configured.";
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

function renderChartContext() {
  const status = currentStatus(); const paper = status.mode === "AXIOM_PAPER";
  $("ticket-title").textContent = paper ? "Paper ticket · simulated" : `${MODE_LABELS[status.mode]} reviewed ticket`;
  $("volume-label").textContent = paper ? "Quantity" : "Volume (lots)";
  $("protection-fields").hidden = paper;
  $("order-disclaimer").textContent = paper
    ? "Paper fills are simulated locally. SELL exits an existing position; short selling is not modelled."
    : status.mode === "VANTAGE_DEMO" ? "MT5 demo order only. Review/precheck and demo fills do not establish live readiness or profitability." : "Real-money capability. Submission requires fresh ACTIVE lifecycle, server guards, review and exact final confirmation.";
  $("start-paper").hidden = !paper;
  if (paper) {
    const paperState = state.workspace.paper_session?.state;
    $("start-paper").textContent = paperState === "RUNNING" ? "Stop paper session" : paperState === "PAUSED" ? "Resume paper session" : "Start paper session";
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
    renderProductFilter(); renderInstruments();
    $("instrument-status").className = "inline-status ok";
    $("instrument-status").textContent = `${result.total_matching} account-specific instrument(s) matched. Only the connected MT5 symbols_get universe is shown.`;
  } catch (error) {
    $("instrument-status").className = "inline-status err"; $("instrument-status").textContent = error.message;
    emptyRow("instrument-rows", 8, "Instrument discovery failed; no substitute universe is used.");
  } finally { state.instrumentLoading = false; }
}
function renderProductFilter() {
  const select = $("product-filter"); const current = select.value;
  select.innerHTML = `<option value="">All product types</option>` + [...state.productTypes].sort().map((type) => `<option value="${escapeHtml(type)}">${escapeHtml(productLabel(type))}</option>`).join("");
  select.value = current;
}
function productLabel(type) {
  return ({ SHARE_CFD: "Share CFD · no share ownership", ETF_CFD: "ETF CFD · no ETF ownership", FOREX: "Forex", INDEX_CFD: "Index CFD", COMMODITY_CFD: "Commodity CFD", ETF: "ETF", UNKNOWN: "Unknown · untradable" })[type] || String(type || "UNKNOWN").replaceAll("_", " ");
}
function quoteFreshness(quote) {
  if (!quote) return ["No quote", "err"];
  const age = ageSeconds(quote.timestamp);
  if (!Number.isFinite(age) || age < 0) return ["Invalid time", "err"];
  return [`${age.toFixed(1)}s`, age > Number(currentStatus().risk_limits?.max_quote_age_seconds || 30) ? "err" : "ok"];
}
function renderInstruments() {
  const status = currentStatus();
  if (!status.connected) {
    $("instrument-status").className = "inline-status warn";
    $("instrument-status").textContent = "Connect with the read-only test on Connections. No hard-coded or website instrument universe is displayed.";
    emptyRow("instrument-rows", 8, "No account-discovered instruments while disconnected.");
  } else if (!state.instruments.length) emptyRow("instrument-rows", 8, "No instruments match the current filters.");
  else $("instrument-rows").innerHTML = state.instruments.map((item) => {
    const [fresh, freshKind] = quoteFreshness(item.quote);
    const spread = item.quote ? Number(item.quote.ask) - Number(item.quote.bid) : null;
    return `<tr><td><input type="checkbox" data-select-symbol="${escapeHtml(item.symbol)}" ${state.selected.has(item.symbol) ? "checked" : ""} aria-label="Select ${escapeHtml(item.symbol)}"></td><td><strong>${escapeHtml(item.symbol)}</strong><small>${escapeHtml(item.display_name || item.description)}</small></td><td>${statusPill(productLabel(item.product_type), item.tradable ? "" : "err")}${item.is_cfd ? "<small>CFD, not underlying ownership</small>" : ""}</td><td>${escapeHtml(item.trade_mode_label)}<small>${item.tradable ? "Metadata understood" : escapeHtml((item.metadata_issues || []).join("; "))}</small></td><td>${fmt(item.volume_min, 8)} / ${fmt(item.volume_step, 8)}</td><td>${fmt(item.tick_size, 8)} / ${escapeHtml(item.digits)}</td><td>${statusPill(fresh, freshKind)}<small>Spread ${spread === null ? "—" : fmt(spread, 8)}</small></td><td><button class="button secondary" data-inspect="${escapeHtml(item.symbol)}">Inspect</button></td></tr>`;
  }).join("");
  setDisabled("reload-instruments", !status.connected, "Run the read-only MT5 connection first.");
  setDisabled("save-instruments", !status.connected || state.selected.size === 0, !status.connected ? "Connect before saving an account selection." : "Select at least one instrument; the API rejects an empty selection.");
  for (const id of ["instrument-search", "product-filter", "selected-only"]) setDisabled(id, !status.connected, "Connect to discover this account's instruments.");
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
    if (paper) {
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
    if (chosen && (!state.chart.payload || state.chart.symbol !== chosen || state.chart.mode !== status.mode)) await loadChart();
    else {
      $("order-symbol").value = chosen || "";
      if (!chosen) { state.chart.bars = []; drawChart(); $("chart-empty").textContent = paper ? "Start the paper session or load a configured dataset." : "Select at least one account instrument, then reload the chart."; }
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
  const status = currentStatus(); const paper = status.mode === "AXIOM_PAPER";
  const symbol = $("chart-symbol").value; const timeframe = $("chart-timeframe").value;
  if (!symbol) return;
  setDisabled("reload-chart", true, "Loading chart…");
  $("chart-source").textContent = "LOADING REAL SESSION DATA"; $("chart-source").className = "status-pill warn";
  try {
    const query = new URLSearchParams({ symbol, timeframe, limit: "500" });
    if (paper) query.set("indicators", "");
    const payload = await api(paper ? `/chart/data?${query}` : `/api/trading/chart?${query}`);
    const bars = normalizeBars(payload.bars);
    state.chart = { mode: status.mode, symbol, timeframe, bars, quote: payload.quote || null, payload };
    $("order-symbol").value = symbol;
    $("chart-source").textContent = payload.data_feed_label || payload.source_label || (paper ? "LOCAL PAPER DATA" : "VANTAGE MT5");
    $("chart-source").className = `status-pill ${payload.market_data ? "ok" : "warn"}`;
    $("chart-legend").textContent = `${bars.length} bars · ${payload.note || payload.data_feed_label || payload.source_label || "Confirmed source"} · generated ${timeText(payload.generated_at)}`;
    renderChartQuote(); drawChart(); renderChartContext(); updateOrderContract();
  } catch (error) {
    if (state.chart.symbol !== symbol || state.chart.mode !== status.mode) { state.chart.bars = []; state.chart.payload = null; drawChart(); }
    $("chart-source").textContent = "CHART LOAD FAILED"; $("chart-source").className = "status-pill err";
    $("chart-empty").hidden = false; $("chart-empty").textContent = error.message;
    toast("Chart load failed", error.message, "err");
  } finally { setDisabled("reload-chart", false); }
}
function renderChartQuote() {
  const chart = state.chart; const payload = chart.payload || {}; const quote = chart.quote;
  if (quote) {
    const age = ageSeconds(quote.timestamp);
    $("chart-quote").innerHTML = `<span><strong>${escapeHtml(chart.symbol)}</strong> ${escapeHtml(productLabel(payload.product_type))}</span><span>Bid <strong>${fmt(quote.bid)}</strong></span><span>Ask <strong>${fmt(quote.ask)}</strong></span><span>Spread ${fmt(Number(quote.ask) - Number(quote.bid), 8)}</span><span class="${age > Number(currentStatus().risk_limits?.max_quote_age_seconds || 30) ? "status-pill err" : "status-pill ok"}">Quote ${Number.isFinite(age) ? `${age.toFixed(1)}s old` : "age unknown"}</span>`;
  } else {
    const last = chart.bars.at(-1);
    $("chart-quote").innerHTML = `<span><strong>${escapeHtml(chart.symbol)}</strong></span><span>Last closed bar <strong>${fmt(last?.close)}</strong></span><span>${escapeHtml(payload.data_feed_label || "Local paper data")}</span><span>${last ? escapeHtml(timeText(last.timestamp)) : "No bar timestamp"}</span>`;
  }
}
function drawChart() {
  const canvas = $("terminal-chart"); if (!canvas) return;
  const rect = canvas.getBoundingClientRect(); const width = Math.max(300, rect.width); const height = Math.max(250, rect.height);
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  canvas.width = Math.round(width * dpr); canvas.height = Math.round(height * dpr);
  const ctx = canvas.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height); ctx.fillStyle = "#09131d"; ctx.fillRect(0, 0, width, height);
  const bars = state.chart.bars || [];
  $("chart-empty").hidden = bars.length > 0;
  if (!bars.length) { canvas.setAttribute("aria-label", "No chart data loaded"); return; }
  const valid = bars.every((bar) => [bar.open, bar.high, bar.low, bar.close].every((value) => Number.isFinite(Number(value)) && Number(value) > 0));
  if (!valid) { $("chart-empty").hidden = false; $("chart-empty").textContent = "Invalid OHLC data refused; no chart drawn."; return; }
  const left = 12, right = 74, top = 20, bottom = 30;
  const plotW = width - left - right, plotH = height - top - bottom;
  const high = Math.max(...bars.map((bar) => Number(bar.high))); const low = Math.min(...bars.map((bar) => Number(bar.low)));
  const padding = Math.max((high - low) * 0.08, high * 0.0001); const max = high + padding, min = low - padding;
  const y = (price) => top + (max - Number(price)) / (max - min || 1) * plotH;
  const step = plotW / bars.length; const bodyW = Math.max(1, Math.min(12, step * 0.68));
  ctx.font = "10px ui-sans-serif, system-ui"; ctx.textBaseline = "middle";
  for (let index = 0; index <= 5; index += 1) {
    const gy = top + plotH * index / 5; const price = max - (max - min) * index / 5;
    ctx.strokeStyle = "#1c2c3c"; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(left, gy); ctx.lineTo(left + plotW, gy); ctx.stroke();
    ctx.fillStyle = "#71899f"; ctx.fillText(fmt(price, 6), left + plotW + 8, gy);
  }
  bars.forEach((bar, index) => {
    const x = left + step * index + step / 2; const up = Number(bar.close) >= Number(bar.open);
    ctx.strokeStyle = up ? "#65d6a5" : "#ff7d8c"; ctx.fillStyle = up ? "#65d6a5" : "#ff7d8c";
    ctx.beginPath(); ctx.moveTo(x, y(bar.high)); ctx.lineTo(x, y(bar.low)); ctx.stroke();
    const bodyTop = y(Math.max(Number(bar.open), Number(bar.close)));
    const bodyBottom = y(Math.min(Number(bar.open), Number(bar.close)));
    ctx.fillRect(x - bodyW / 2, bodyTop, bodyW, Math.max(1, bodyBottom - bodyTop));
  });
  ctx.fillStyle = "#71899f";
  for (let index = 0; index <= 4; index += 1) {
    const barIndex = Math.min(bars.length - 1, Math.floor((bars.length - 1) * index / 4));
    const date = new Date(bars[barIndex].timestamp); const label = Number.isNaN(date.getTime()) ? "" : date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
    const x = left + plotW * index / 4; ctx.textAlign = index === 0 ? "left" : index === 4 ? "right" : "center"; ctx.fillText(label, x, height - 12);
  }
  ctx.textAlign = "left";
  canvas.setAttribute("aria-label", `${state.chart.symbol} ${state.chart.timeframe}: ${bars.length} candles, high ${high}, low ${low}, last close ${bars.at(-1).close}`);
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
  if (contract) {
    $("order-volume").min = contract.volume_min ?? 0; $("order-volume").step = contract.volume_step ?? "any";
    if (!$("order-volume").value) $("order-volume").value = contract.volume_min ?? "";
  }
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
  const body = `<p><strong>LOCAL PAPER ONLY.</strong> This review and any result are simulated; no Vantage order is routed.</p>${reviewGrid([
    ["Symbol", values.symbol], ["Side / type", `${values.side} / ${values.order_type}`],
    ["Quantity", values.volume], ["Limit price", values.limit_price ?? "Market"],
    ["Last closed-bar price", ticket.last_price ?? "Unavailable"], ["Price timestamp", timeText(ticket.priced_at)],
    ["Cash", money(ticket.cash, state.workspace.account?.currency)], ["Held quantity", ticket.position?.quantity ?? 0],
    ["Max buy / sell", `${fmt(ticket.max_buy_quantity, 8)} / ${fmt(ticket.max_sell_quantity, 8)}`], ["Feed", ticket.price_source],
  ])}${ticket.blocked ? `<div class="review-issues">${escapeHtml(ticket.blocked)}</div>` : ""}${blocked ? `<div class="review-issues">Durable emergency stop blocks a new paper entry.</div>` : ""}`;
  openDialog({
    title: "Review simulated paper order", body, confirmLabel: "Place simulated paper order", confirmClass: "primary",
    confirmDisabled: Boolean(ticket.blocked || blocked || ticket.last_price === null),
    onConfirm: async () => {
      const payload = { symbol: values.symbol, side: values.side, quantity: values.volume, order_type: values.order_type, limit_price: values.limit_price };
      const result = await api("/paper/order", { body: payload });
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
    const result = await api("/api/trading/orders/submit", { body: { ...payload, review_id: review.review_id, confirmation: review.confirmation_phrase } });
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
      toast("Stop result recorded", paper ? "Local paper session stopped." : result.note || "Inspect broker-reported remainder.", "warn", 10000);
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
  const status = currentStatus();
  const payload = {
    environment: status.mode, login: $("mt5-login").value.trim(), password: $("mt5-password").value,
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
  const mode = currentStatus().mode;
  openDialog({
    title: "Delete protected Vantage credential",
    body: `<p>Delete the protected ${escapeHtml(MODE_LABELS[mode])} credential and masked metadata for this environment. Any matching connection is disconnected and disarmed. This does not delete broker orders or positions.</p>`,
    confirmLabel: "Delete protected credential",
    onConfirm: async () => {
      await api(`/api/trading/credentials/${encodeURIComponent(mode)}`, { method: "DELETE" });
      closeDialog(); state.credentialMode = null; state.connectionInfo = null;
      await loadConnectionInfo(); await loadWorkspace(); toast("Credential deleted", "No protected password or full login remains in AXIOM's metadata.", "ok");
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
    state.readiness = null; state.activation = null; state.instruments = []; state.chart = { mode: state.mode, symbol: "", timeframe: "1m", bars: [], quote: null, payload: null };
    await loadWorkspace(); drawChart(); toast("MT5 disconnected", "Session disarmed. Broker state is no longer current; empty views do not prove a flat account.", "warn");
  } catch (error) { toast("Disconnect failed", error.message, "err"); }
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
  document.querySelectorAll(".nav-item").forEach((element) => element.classList.toggle("active", element.dataset.page === page));
  $("page-eyebrow").textContent = PAGE_TITLES[page][0]; $("page-title").textContent = PAGE_TITLES[page][1];
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
    const result = await api(paperState === "PAUSED" ? "/paper/resume" : "/api/paper/start-default", { body: {} });
    await loadWorkspace(); await loadChartSymbols();
    toast("Paper session updated", `${result.state || "RUNNING"} · local simulated execution only`, "ok");
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
  $("refresh-all").addEventListener("click", () => refreshAll().then(() => toast("Workspace refreshed", "Latest available state loaded.", "ok")).catch(() => {}));
  $("apply-mode").addEventListener("click", selectMode);
  $("emergency-stop").addEventListener("click", emergencyStop);
  $("modal-close").addEventListener("click", closeDialog); $("modal-cancel").addEventListener("click", closeDialog); $("modal-confirm").addEventListener("click", confirmDialog);
  document.addEventListener("keydown", (event) => { if (event.key === "Escape" && !$("modal").hidden) closeDialog(); });
  let searchTimer;
  $("instrument-search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(loadInstruments, 300); });
  $("product-filter").addEventListener("change", loadInstruments); $("selected-only").addEventListener("change", loadInstruments);
  $("reload-instruments").addEventListener("click", loadInstruments); $("save-instruments").addEventListener("click", saveInstruments);
  $("instrument-rows").addEventListener("change", (event) => {
    const symbol = event.target.dataset.selectSymbol; if (!symbol) return;
    if (event.target.checked) state.selected.add(symbol); else state.selected.delete(symbol);
    setDisabled("save-instruments", state.selected.size === 0, "Select at least one instrument.");
  });
  $("instrument-rows").addEventListener("click", (event) => { const symbol = event.target.dataset.inspect; if (symbol) inspectInstrument(symbol); });
  $("chart-symbol").addEventListener("change", () => { $("order-symbol").value = $("chart-symbol").value; loadChart(); });
  $("chart-timeframe").addEventListener("change", loadChart); $("reload-chart").addEventListener("click", loadChart); $("start-paper").addEventListener("click", paperSessionControl);
  $("order-buy").addEventListener("click", () => { state.side = "BUY"; $("order-buy").classList.add("active"); $("order-sell").classList.remove("active"); });
  $("order-sell").addEventListener("click", () => { state.side = "SELL"; $("order-sell").classList.add("active"); $("order-buy").classList.remove("active"); });
  $("order-type").addEventListener("change", () => { $("limit-field").hidden = $("order-type").value !== "LIMIT"; });
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
  $("download-diagnostics").addEventListener("click", () => downloadJson(`axiom-redacted-diagnostics-${new Date().toISOString().slice(0, 10)}.json`, {
    generated_at: new Date().toISOString(), note: "No API token, password, ciphertext, or full account login included. Review before sharing.",
    workspace: state.workspace, readiness: state.readiness, connection: state.connectionInfo ? { secret_store: state.connectionInfo.secret_store, profiles: state.connectionInfo.profiles, test_is_read_only: state.connectionInfo.test_is_read_only, mt5_package_available: state.connectionInfo.mt5_package_available, mt5_integration: state.connectionInfo.mt5_integration } : null,
  }));
  if (globalThis.ResizeObserver) new ResizeObserver(() => drawChart()).observe($("terminal-chart"));
}

function init() {
  bindEvents(); $("refresh-interval").value = "10000"; renderActivity();
  const saved = storageGet("axiom-api-token");
  if (saved) unlock(saved); else showAuth();
}
init();
