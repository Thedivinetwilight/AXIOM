/* Headless smoke & integration suite for:
 * - Kronos 8-way PAPER Truth Matrix & Lifecycle Telemetry (#mm-kronos-paper-panel, #ml-kronos-truth-grid)
 * - Auditable Kronos PAPER Contribution & Failure Policy Form (#mm-kronos-policy-form)
 * - Per-Symbol PAPER Decision & Risk Inspector (#mm-decision-inspector-panel)
 * - Correlated Secret-Redacted Structured Paper & Kronos Event Logs (#mm-structured-log-panel)
 * - Strictly Read-Only Workspace 11 (#page-monitor) invariant (0 order-mutation controls)
 *
 * Works seamlessly with JSDOM when installed (CI) and falls back to a pure Node 20 `node:vm`
 * HTML/DOM harness when `node_modules` is absent (offline source checkout).
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import vm from "node:vm";

const root = resolve(import.meta.dirname, "../..");
const html = readFileSync(resolve(root, "app/static/terminal/index.html"), "utf8");
const css = readFileSync(resolve(root, "app/static/terminal/terminal.css"), "utf8");
const code = readFileSync(resolve(root, "app/static/terminal/terminal.js"), "utf8");

function stripTags(raw) {
  return String(raw ?? "")
    .replace(/<br\s*\/?>/gi, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/\s+/g, " ")
    .trim();
}

function createVmDomHarness(rawHtml) {
  const elementsById = new Map();

  function makeClassList(el) {
    const syncSet = () => new Set(String(el.className || "").split(/\s+/).filter(Boolean));
    return {
      add(...names) {
        const s = syncSet();
        for (const n of names) if (n) s.add(n);
        el.className = [...s].join(" ");
      },
      remove(...names) {
        const s = syncSet();
        for (const n of names) s.delete(n);
        el.className = [...s].join(" ");
      },
      contains(name) {
        return syncSet().has(name);
      },
      toggle(name, force) {
        const s = syncSet();
        const next = force !== undefined ? Boolean(force) : !s.has(name);
        if (next) s.add(name);
        else s.delete(name);
        el.className = [...s].join(" ");
        return next;
      },
    };
  }

  function createElement(tagName = "DIV", id = "") {
    let _innerHTML = "";
    let _textContent = "";
    const listeners = new Map();
    const attrs = new Map();
    const el = {
      id,
      tagName: String(tagName || "DIV").toUpperCase(),
      className: "",
      value: "",
      checked: false,
      disabled: false,
      hidden: false,
      style: {},
      dataset: {},
      options: [],
      children: [],
      parentElement: null,
      get innerHTML() {
        return _innerHTML;
      },
      set innerHTML(val) {
        _innerHTML = String(val ?? "");
        _textContent = stripTags(_innerHTML);
      },
      get textContent() {
        return _textContent;
      },
      set textContent(val) {
        _textContent = String(val ?? "");
        _innerHTML = _textContent;
      },
      setAttribute(k, v) {
        attrs.set(k, String(v));
        if (k === "class") el.className = String(v);
        if (k === "hidden") el.hidden = true;
      },
      getAttribute(k) {
        return attrs.has(k) ? attrs.get(k) : null;
      },
      removeAttribute(k) {
        attrs.delete(k);
        if (k === "hidden") el.hidden = false;
      },
      addEventListener(type, fn) {
        if (!listeners.has(type)) listeners.set(type, []);
        listeners.get(type).push(fn);
      },
      async dispatchAsync(type, extra = {}) {
        const handlers = listeners.get(type) || [];
        const event = {
          type,
          target: extra.target || el,
          currentTarget: el,
          defaultPrevented: false,
          preventDefault() {
            this.defaultPrevented = true;
          },
          stopPropagation() {},
          ...extra,
        };
        for (const fn of handlers) {
          await fn(event);
        }
        return event;
      },
      dispatchEvent(ev) {
        const handlers = listeners.get(ev.type) || [];
        ev.target = ev.target || el;
        ev.currentTarget = el;
        ev.preventDefault = ev.preventDefault || (() => {});
        for (const fn of handlers) fn(ev);
        return true;
      },
      focus() {
        document.activeElement = el;
      },
      blur() {
        if (document.activeElement === el) document.activeElement = null;
      },
      closest(selector) {
        if (selector === "[data-inspect-decision]" && el.dataset.inspectDecision) return el;
        if (selector === "button" && el.tagName === "BUTTON") return el;
        return null;
      },
      querySelector(sel) {
        if (sel === "button") return createElement("BUTTON");
        return null;
      },
      querySelectorAll() {
        return [];
      },
      append(...nodes) {
        for (const node of nodes) {
          if (node && typeof node === "object") node.parentElement = el;
          el.children.push(node);
        }
      },
      appendChild(child) {
        child.parentElement = el;
        el.children.push(child);
        return child;
      },
      remove() {},
      getBoundingClientRect() {
        return { left: 0, top: 0, width: 920, height: 380 };
      },
      getContext() {
        return {
          clearRect() {}, fillRect() {}, strokeRect() {}, beginPath() {}, moveTo() {}, lineTo() {},
          stroke() {}, fill() {}, arc() {}, closePath() {}, fillText() {}, save() {}, restore() {},
          setLineDash() {}, measureText: (t) => ({ width: String(t).length * 6 }),
        };
      },
    };
    el.classList = makeClassList(el);
    if (id) elementsById.set(id, el);
    return el;
  }

  // Parse all elements with `id="..."` from index.html so real markup defaults are honored
  const tagRegex = /<([a-zA-Z0-9-]+)\b([^>]*)>/g;
  let match;
  while ((match = tagRegex.exec(rawHtml)) !== null) {
    const tag = match[1].toUpperCase();
    const attrText = match[2] || "";
    const idMatch = /\bid=["']([^"']+)["']/.exec(attrText);
    if (!idMatch) continue;
    const id = idMatch[1];
    const el = createElement(tag, id);
    const clsMatch = /\bclass=["']([^"']+)["']/.exec(attrText);
    if (clsMatch) el.className = clsMatch[1];
    const valMatch = /\bvalue=["']([^"']*)["']/.exec(attrText);
    if (valMatch) el.value = valMatch[1];
    if (/\bhidden\b/.test(attrText)) el.hidden = true;

    if (tag === "SELECT") {
      const closeIdx = rawHtml.indexOf("</select>", match.index);
      if (closeIdx > match.index) {
        const inner = rawHtml.slice(match.index, closeIdx);
        const optRegex = /<option\b([^>]*)>([\s\S]*?)<\/option>/gi;
        let om;
        while ((om = optRegex.exec(inner)) !== null) {
          const oAttr = om[1] || "";
          const oValMatch = /\bvalue=["']([^"']*)["']/.exec(oAttr);
          const optVal = oValMatch ? oValMatch[1] : stripTags(om[2]);
          const selected = /\bselected\b/i.test(oAttr);
          el.options.push({ value: optVal, textContent: stripTags(om[2]), selected });
          if (selected || el.options.length === 1) {
            if (selected || !el.value) el.value = optVal;
          }
        }
      }
    }
  }

  const docListeners = new Map();
  const document = {
    activeElement: null,
    body: createElement("BODY"),
    documentElement: createElement("HTML"),
    getElementById(id) {
      if (!elementsById.has(id)) {
        // Lazily return safe element stub only if dynamic ID requested
        return null;
      }
      return elementsById.get(id);
    },
    createElement: (t) => createElement(t),
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener(type, fn) {
      if (!docListeners.has(type)) docListeners.set(type, []);
      docListeners.get(type).push(fn);
    },
  };

  const storageMap = new Map();
  const sessionStorage = {
    getItem: (k) => (storageMap.has(k) ? storageMap.get(k) : null),
    setItem: (k, v) => storageMap.set(k, String(v)),
    removeItem: (k) => storageMap.delete(k),
  };

  return { document, elementsById, sessionStorage };
}

// ---------------------------------------------------------------------------
// 1. Static HTML & Dark-Theme Invariants (including Read-Only Monitor 11)
// ---------------------------------------------------------------------------
for (const requiredId of [
  "mm-pill-kronos-session",
  "mm-kronos-paper-panel",
  "mm-kronos-truth-grid",
  "ml-kronos-truth-grid",
  "mm-kronos-policy-form",
  "mm-kp-session-enabled",
  "mm-kp-failure-policy",
  "mm-kp-conflict-policy",
  "mm-kp-min-confidence",
  "mm-kp-max-uncertainty",
  "mm-kp-weight-cap",
  "mm-nse-kronos-mode",
  "mm-nse-kronos-policy",
  "mm-nasdaq-kronos-mode",
  "mm-nasdaq-kronos-policy",
  "mm-decision-inspector-panel",
  "mm-decision-market-filter",
  "mm-decision-symbol-filter",
  "mm-decision-symbol-cards",
  "mm-decision-explanation-view",
  "mm-strategy-decision-rows",
  "mm-structured-log-panel",
  "log-filter-form",
  "log-filter-window",
  "log-filter-symbol",
  "log-filter-event-type",
  "log-filter-severity",
  "log-filter-strategy",
  "log-filter-session",
  "log-filter-query",
  "log-error-banner",
  "mm-structured-log-rows",
]) {
  assert.ok(html.includes(`id="${requiredId}"`), `Missing required element #${requiredId} in index.html`);
}

assert.ok(css.includes(".truth-cell"), "Missing dark-themed .truth-cell CSS");
assert.ok(css.includes(".decision-card"), "Missing dark-themed .decision-card CSS");
assert.ok(css.includes(".corr-chip"), "Missing dark-themed .corr-chip CSS");

// Verify Workspace 11 (#page-monitor) remains strictly read-only (0 order-mutation controls)
const monitorStart = html.indexOf('id="page-monitor"');
const ensembleStart = html.indexOf('id="page-ensemble"');
assert.ok(monitorStart > 0 && ensembleStart > monitorStart, "Expected #page-monitor before #page-ensemble");
const monitorMarkup = html.slice(monitorStart, ensembleStart);
assert.ok(
  !/place order|submit order|cancel order|modify order|close position/i.test(monitorMarkup),
  "Workspace 11 Live Trade Monitor must remain strictly read-only with 0 order-mutation controls"
);

// ---------------------------------------------------------------------------
// 2. Execute real terminal.js and verify end-to-end Kronos PAPER, Decisions & Logs
// ---------------------------------------------------------------------------
const apiCalls = [];
let policySaveShouldFail = false;
let logQueryShouldFail = false;
let currentPolicyConfig = {
  inference_enabled: false,
  paper_session_enabled: true,
  failure_policy: "FAIL_CLOSED_NO_ENTRY",
  conflict_policy: "VETO_CONFLICTING_ENTRY",
  min_confidence: 0.55,
  max_uncertainty_pct: 8.0,
  weight_cap: 0.2,
};

let mockLogsResponse = {
  total_matching: 2,
  returned_count: 2,
  real_money_events_included: false,
  events: [
    {
      event_id: "ev-1",
      timestamp: "2026-10-30T15:31:00Z",
      severity: "INFO",
      simulated: true,
      execution_badge: "SIMULATED · LIVE_MARKET_PAPER (0 BROKER ORDERS)",
      pnl_scope_label: "SIMULATED_SESSION_AND_PERSISTED_PAPER_PNL",
      market: "NASDAQ",
      symbol: "AAPL",
      event_type: "PAPER_FILL_SIMULATED",
      strategy: "kronos_ensemble",
      session_id: "sess-nasdaq-001",
      decision_id: "dec-aapl-001",
      order_id: "ord-aapl-001",
      fill_id: "fill-aapl-001",
      detail: { side: "BUY", qty: 5, fill_price: 200.05, access_token: "[REDACTED]" },
    },
    {
      event_id: "ev-2",
      timestamp: "2026-10-30T15:32:00Z",
      severity: "WARN",
      simulated: false,
      execution_badge: "BROKER-CONFIRMED · REAL MONEY",
      pnl_scope_label: "BROKER_REPORTED_REAL_MONEY_AUDIT",
      market: "NASDAQ",
      symbol: "AAPL",
      event_type: "BROKER_AUDIT",
      strategy: "",
      session_id: "",
      decision_id: "",
      order_id: "brk-999",
      fill_id: "",
      detail: { note: "read-only broker audit comparison row" },
    },
  ],
};

async function mockFetch(url, opts = {}) {
  const path = String(url);
  const method = opts.method || "GET";
  const body = opts.body ? JSON.parse(opts.body) : undefined;
  apiCalls.push({ path, method, body });

  // Guard: zero live order placement calls
  assert.ok(!path.includes("/v2/orders"), `Forbidden broker order endpoint called: ${path}`);

  if (path.startsWith("/api/model-lab/kronos/paper-policy") && method === "POST") {
    if (policySaveShouldFail) {
      return {
        ok: false,
        status: 422,
        json: async () => ({ detail: "weight_cap must be <= 0.50" }),
      };
    }
    currentPolicyConfig = { ...currentPolicyConfig, ...body };
    return {
      ok: true,
      status: 200,
      json: async () => ({
        provider_state: "VERIFIED_READY",
        lifecycle_state: "READY",
        config: currentPolicyConfig,
        truth_matrix: {
          installed: true,
          validated: true,
          loaded: false,
          running: false,
          degraded: false,
          failed: false,
          forecast_produced: false,
          forecast_consumed: false,
          produced_count: 0,
          consumed_count: 0,
          lifecycle_state: "READY",
          provider_state: "VERIFIED_READY",
        },
        kronos_engine: {
          artifact_verified: true,
          self_test_verified: true,
          model_revision: "901c26c1332695a2",
          manifest_sha256: "abcdef0123456789abcdef0123456789",
        },
        inference_diagnostics: {},
      }),
    };
  }

  if (path.startsWith("/api/markets/paper/logs")) {
    if (logQueryShouldFail) {
      return {
        ok: false,
        status: 503,
        json: async () => ({ detail: "SQLite audit ledger temporarily locked" }),
      };
    }
    return {
      ok: true,
      status: 200,
      json: async () => mockLogsResponse,
    };
  }

  if (path.startsWith("/api/markets/paper/decisions")) {
    return {
      ok: true,
      status: 200,
      json: async () => ({ decisions: [] }),
    };
  }

  return {
    ok: true,
    status: 200,
    json: async () => ({}),
  };
}

let env;
let usingJsdom = false;
try {
  const { JSDOM } = await import("jsdom");
  const dom = new JSDOM(html, { url: "http://127.0.0.1:8765/terminal", pretendToBeVisual: true, runScripts: "outside-only" });
  dom.window.fetch = mockFetch;
  dom.window.HTMLCanvasElement.prototype.getContext = () => ({
    clearRect() {}, fillRect() {}, strokeRect() {}, beginPath() {}, moveTo() {}, lineTo() {},
    stroke() {}, fill() {}, arc() {}, closePath() {}, fillText() {}, save() {}, restore() {},
    setLineDash() {}, measureText: (t) => ({ width: String(t).length * 6 }),
  });
  dom.window.eval(`${code}\nwindow.__axiomTest = { state, renderKronosPaperSection, renderDecisionInspector, loadStructuredPaperLogs, renderStructuredPaperLogs, getStructuredLogQueryParams };`);
  env = {
    doc: dom.window.document,
    testApi: dom.window.__axiomTest,
    submitForm: async (id) => {
      const form = dom.window.document.getElementById(id);
      form.dispatchEvent(new dom.window.Event("submit", { bubbles: true, cancelable: true }));
      await new Promise((r) => setTimeout(r, 20));
    },
    clickById: async (id) => {
      const el = dom.window.document.getElementById(id);
      el.click();
      await new Promise((r) => setTimeout(r, 20));
    },
    changeSelect: async (id, value) => {
      const el = dom.window.document.getElementById(id);
      el.value = value;
      el.dispatchEvent(new dom.window.Event("change", { bubbles: true }));
      await new Promise((r) => setTimeout(r, 20));
    },
  };
  usingJsdom = true;
} catch (_) {
  const { document, sessionStorage } = createVmDomHarness(html);
  const sandbox = {
    window: null,
    document,
    sessionStorage,
    fetch: mockFetch,
    URLSearchParams,
    console,
    setTimeout: (fn, ms) => { const t = setTimeout(fn, ms); if (t && typeof t.unref === "function") t.unref(); return t; },
    clearTimeout,
    setInterval: () => 1,
    clearInterval: () => {},
    requestAnimationFrame: (fn) => { fn(0); return 1; },
    cancelAnimationFrame: () => {},
    performance: { now: () => 100 },
    navigator: { clipboard: { writeText: async () => {} } },
    Blob: class Blob {},
    URL: { createObjectURL: () => "blob:mock", revokeObjectURL: () => {} },
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(
    `${code}\nwindow.__axiomTest = { state, renderKronosPaperSection, renderDecisionInspector, loadStructuredPaperLogs, renderStructuredPaperLogs, getStructuredLogQueryParams };`,
    sandbox
  );
  env = {
    doc: document,
    testApi: sandbox.__axiomTest,
    submitForm: async (id) => {
      await document.getElementById(id).dispatchAsync("submit");
    },
    clickById: async (id) => {
      await document.getElementById(id).dispatchAsync("click");
    },
    changeSelect: async (id, value) => {
      const el = document.getElementById(id);
      el.value = value;
      await el.dispatchAsync("change");
    },
  };
}

const { doc, testApi } = env;

// ---------------------------------------------------------------------------
// 3. Verify 8-Way Kronos Truth Matrix Distinctions
// ---------------------------------------------------------------------------
// Case A: NOT_CONFIGURED
testApi.renderKronosPaperSection(
  {
    lifecycle_state: "NOT_CONFIGURED",
    provider_state: "NOT_INSTALLED",
    lifecycle_reason: "Kronos artifacts are not installed in %LOCALAPPDATA%\\AXIOM.",
    config: currentPolicyConfig,
    truth_matrix: {
      installed: false, validated: false, loaded: false, running: false,
      degraded: false, failed: false, forecast_produced: false, forecast_consumed: false,
      produced_count: 0, consumed_count: 0,
      lifecycle_state: "NOT_CONFIGURED", provider_state: "NOT_INSTALLED",
    },
    kronos_engine: { artifact_verified: false, self_test_verified: false },
    diagnostics: {},
  },
  { session_state: "STOPPED", kronos_enabled: false },
  { session_state: "STOPPED", kronos_enabled: false }
);
assert.equal(doc.getElementById("mm-pill-kronos-session").textContent, "KRONOS PAPER · NOT_CONFIGURED · 0 CONSUMED");
assert.ok(doc.getElementById("mm-kronos-truth-grid").innerHTML.includes("NO · MISSING"));
assert.ok(doc.getElementById("mm-kronos-truth-grid").innerHTML.includes("NO · UNVERIFIED"));

// Case B: Installed & Self-Test Validated (READY), NOT Running, 0 Consumed
testApi.renderKronosPaperSection(
  {
    lifecycle_state: "READY",
    provider_state: "VERIFIED_READY",
    lifecycle_reason: "Artifacts and genuine CPU self-test verified; idle until PAPER session starts.",
    config: currentPolicyConfig,
    truth_matrix: {
      installed: true, validated: true, loaded: false, running: false,
      degraded: false, failed: false, forecast_produced: false, forecast_consumed: false,
      produced_count: 0, consumed_count: 0,
      lifecycle_state: "READY", provider_state: "VERIFIED_READY",
    },
    kronos_engine: {
      artifact_verified: true, self_test_verified: true,
      model_revision: "901c26c13326", manifest_sha256: "1234567890abcdef1234",
    },
    diagnostics: {},
  },
  { session_state: "STOPPED", kronos_enabled: true },
  { session_state: "STOPPED", kronos_enabled: true }
);
assert.equal(doc.getElementById("mm-pill-kronos-session").textContent, "KRONOS PAPER · READY · 0 CONSUMED");
const readyGrid = doc.getElementById("mm-kronos-truth-grid").innerHTML;
assert.ok(readyGrid.includes("YES · SHA-256 + SELF-TEST"));
assert.ok(readyGrid.includes("NO · UNLOADED"));
assert.ok(readyGrid.includes("NO · READY"));

// Case C: RUNNING with Forecast Produced (3) != Forecast Consumed (1)
testApi.renderKronosPaperSection(
  {
    lifecycle_state: "RUNNING",
    provider_state: "INFERENCE_ENABLED",
    config: currentPolicyConfig,
    truth_matrix: {
      installed: true, validated: true, loaded: true, running: true,
      degraded: false, failed: false, forecast_produced: true, forecast_consumed: true,
      produced_count: 3, consumed_count: 1,
      lifecycle_state: "RUNNING", provider_state: "INFERENCE_ENABLED",
    },
    kronos_engine: {
      artifact_verified: true, self_test_verified: true,
      model_revision: "901c26c13326", manifest_sha256: "1234567890abcdef1234",
    },
    diagnostics: {
      last_inference_at: "2026-10-30T15:30:00Z",
      last_latency_ms: 18.4,
      inference_ms_p50: 17.9,
      inference_ms_p95: 22.1,
      last_produced_summary: {
        market: "NASDAQ", symbol: "AAPL", timeframe: "5m",
        direction: "UP", confidence: 0.74, uncertainty_pct: 2.35,
      },
    },
  },
  { session_state: "STOPPED", kronos_enabled: false },
  {
    session_state: "RUNNING",
    kronos_enabled: true,
    kronos_failure_policy: "FAIL_CLOSED_NO_ENTRY",
    kronos_session: { lifecycle_state: "RUNNING", kronos_configured: true, produced_count: 3, consumed_count: 1 },
  }
);
assert.equal(doc.getElementById("mm-pill-kronos-session").textContent, "KRONOS PAPER · RUNNING · 1 CONSUMED");
const runGrid = doc.getElementById("mm-kronos-truth-grid").innerHTML;
assert.ok(runGrid.includes("YES (3)"));
assert.ok(runGrid.includes("YES (1)"));
const runTelemetry = doc.getElementById("mm-kronos-paper-telemetry").textContent;
assert.ok(runTelemetry.includes("3 produced / 1 consumed by active PAPER strategy"));
assert.ok(runTelemetry.includes("18.4 ms"));
assert.ok(runTelemetry.includes("NASDAQ:AAPL"));

// Case D: DEGRADED / FAILED on Timeout — NEVER mislabeled healthy
testApi.renderKronosPaperSection(
  {
    lifecycle_state: "DEGRADED",
    provider_state: "DEGRADED_OR_FAILED",
    config: currentPolicyConfig,
    truth_matrix: {
      installed: true, validated: true, loaded: false, running: false,
      degraded: true, failed: false, forecast_produced: true, forecast_consumed: false,
      produced_count: 1, consumed_count: 0,
      lifecycle_state: "DEGRADED", provider_state: "DEGRADED_OR_FAILED",
    },
    kronos_engine: { artifact_verified: true, self_test_verified: true },
    diagnostics: {},
    last_error: {
      code: "INFERENCE_TIMEOUT",
      reason: "Kronos worker exceeded 2.0s deadline; subprocess killed cleanly",
      at: "2026-10-30T15:35:00Z",
    },
  },
  { session_state: "STOPPED" },
  { session_state: "RUNNING", kronos_enabled: true, kronos_session: { lifecycle_state: "DEGRADED" } }
);
assert.equal(doc.getElementById("mm-pill-kronos-session").textContent, "KRONOS PAPER · DEGRADED · 0 CONSUMED");
assert.ok(
  doc.getElementById("mm-kronos-paper-telemetry").textContent.includes("INFERENCE_TIMEOUT"),
  "Timeout error code and reason must be shown explicitly"
);

// ---------------------------------------------------------------------------
// 4. Interactive Kronos Policy Form (Success + Validation Error Feedback)
// ---------------------------------------------------------------------------
doc.getElementById("mm-kp-failure-policy").value = "EXPLICIT_NON_KRONOS_FALLBACK";
doc.getElementById("mm-kp-conflict-policy").value = "REDUCE_SCORE_ON_CONFLICT";
doc.getElementById("mm-kp-min-confidence").value = "0.62";
await env.submitForm("mm-kronos-policy-form");
assert.ok(
  doc.getElementById("mm-kp-policy-feedback").textContent.includes("EXPLICIT_NON_KRONOS_FALLBACK"),
  "Expected visible save confirmation on #mm-kp-policy-feedback"
);
assert.ok(doc.getElementById("mm-kp-policy-feedback").className.includes("ok"));

policySaveShouldFail = true;
await env.submitForm("mm-kronos-policy-form");
assert.ok(doc.getElementById("mm-kp-policy-feedback").className.includes("err"));
assert.ok(doc.getElementById("mm-kp-policy-feedback").textContent.includes("weight_cap must be <= 0.50"));
policySaveShouldFail = false;

// Verify explicit operator PAPER session start transmits kronos_enabled & kronos_failure_policy
doc.getElementById("mm-nasdaq-kronos-mode").value = "enabled";
doc.getElementById("mm-nasdaq-kronos-policy").value = "EXPLICIT_NON_KRONOS_FALLBACK";
await env.clickById("mm-nasdaq-start-btn");
const startCall = [...apiCalls].reverse().find((c) => c.path === "/api/markets/paper/session/start");
assert.ok(startCall, "Expected POST /api/markets/paper/session/start");
assert.equal(startCall.body.market, "NASDAQ");
assert.equal(startCall.body.kronos_enabled, true);
assert.equal(startCall.body.kronos_failure_policy, "EXPLICIT_NON_KRONOS_FALLBACK");

// ---------------------------------------------------------------------------
// 5. Per-Symbol PAPER Decision Inspector (Kronos Consumed vs Produced-Not-Consumed + Risk Gate)
// ---------------------------------------------------------------------------
const sampleDecisions = [
  {
    decision_id: "dec-aapl-1",
    created_at: "2026-10-30T15:30:00Z",
    market: "NASDAQ",
    symbol: "AAPL",
    strategy: "kronos_ensemble",
    timeframe: "5m",
    action: "BUY",
    reason: "Rule BUY confirmed by fresh Kronos UP forecast (conf=0.78)",
    order_client_id: "ord-aapl-001",
    kronos_status: "OK",
    forecast_produced: true,
    forecast_consumed: true,
    policy_applied: "AGREEMENT_CONFIRMED",
    explanation: {
      rule_action: "BUY",
      input_window: { bar_count: 48, input_start: "2026-10-30T11:30:00Z", input_end: "2026-10-30T15:30:00Z" },
      forecast_summary: {
        status: "OK", direction: "UP", confidence: 0.78, uncertainty_pct: 2.1, latency_ms: 16.5,
        model_id: "NeoQuasar/Kronos-small",
      },
      risk_checks: { passed: true, rejection_reasons: [] },
    },
  },
  {
    decision_id: "dec-nvda-2",
    created_at: "2026-10-30T15:30:00Z",
    market: "NASDAQ",
    symbol: "NVDA",
    strategy: "parallel_ensemble",
    timeframe: "5m",
    action: "NO_TRADE",
    reason: "symbol_and_total_exposure: Symbol exposure cap reached",
    order_client_id: "",
    kronos_status: "VERIFIED_FRESH",
    forecast_produced: true,
    forecast_consumed: false,
    policy_applied: "REQUIRE_KRONOS",
    explanation: {
      strategy_signals: [
        { variant_id: "ema_cross_9_21", direction: "BUY" },
        { variant_id: "rsi_rev_14", direction: "HOLD" },
      ],
      kronos_contribution: {
        status: "VERIFIED_FRESH", up_probability: 0.69, model_id: "NeoQuasar/Kronos-small",
      },
      risk_checks: [
        { check_id: "quote_freshness", passed: true, detail: "Fresh quote" },
        { check_id: "symbol_and_total_exposure", passed: false, detail: "Symbol exposure cap reached" },
      ],
    },
  },
];

testApi.state.mm.selectedDecisionId = "dec-aapl-1";
testApi.renderDecisionInspector(sampleDecisions);
assert.ok(doc.getElementById("mm-strategy-decision-rows").innerHTML.includes("KRONOS CONSUMED"));
assert.ok(doc.getElementById("mm-strategy-decision-rows").innerHTML.includes("PRODUCED · NOT CONSUMED"));
assert.ok(doc.getElementById("mm-decision-explanation-view").textContent.includes("AGREEMENT_CONFIRMED"));
assert.ok(doc.getElementById("mm-decision-explanation-view").textContent.includes("PASSED authoritative paper risk bounds"));

// Switch to NVDA decision to verify array-shaped risk_checks & strategy_signals rendering
testApi.state.mm.selectedDecisionId = "dec-nvda-2";
testApi.renderDecisionInspector(sampleDecisions);
const nvdaExpText = doc.getElementById("mm-decision-explanation-view").textContent;
assert.ok(nvdaExpText.includes("ema_cross_9_21=BUY"));
assert.ok(nvdaExpText.includes("REJECTED (symbol_and_total_exposure: Symbol exposure cap reached)"));

// ---------------------------------------------------------------------------
// 6. Structured Secret-Redacted Logs: Multi-Filter Queries, Correlation, Simulated vs Broker, Empty & Error States
// ---------------------------------------------------------------------------
doc.getElementById("log-filter-window").value = "1h";
doc.getElementById("log-filter-symbol").value = "AAPL";
doc.getElementById("log-filter-event-type").value = "CAT:KRONOS_LIFECYCLE";
doc.getElementById("log-filter-severity").value = "INFO";
doc.getElementById("log-filter-strategy").value = "kronos_ensemble";
doc.getElementById("log-filter-session").value = "sess-nasdaq-001";
await testApi.loadStructuredPaperLogs();

const lastLogCall = [...apiCalls].reverse().find((c) => c.path.startsWith("/api/markets/paper/logs"));
assert.ok(lastLogCall, "Expected GET /api/markets/paper/logs call");
assert.ok(lastLogCall.path.includes("window=1h"));
assert.ok(lastLogCall.path.includes("symbol=AAPL"));
assert.ok(lastLogCall.path.includes("category=KRONOS_LIFECYCLE"));
assert.ok(lastLogCall.path.includes("severity=INFO"));
assert.ok(lastLogCall.path.includes("strategy=kronos_ensemble"));
assert.ok(lastLogCall.path.includes("session_id=sess-nasdaq-001"));

const logHtml = doc.getElementById("mm-structured-log-rows").innerHTML;
assert.ok(logHtml.includes("SIMULATED · LIVE_MARKET_PAPER (0 BROKER ORDERS)"));
assert.ok(logHtml.includes("SIMULATED_SESSION_AND_PERSISTED_PAPER_PNL"));
assert.ok(logHtml.includes("BROKER-CONFIRMED · REAL MONEY"));
assert.ok(logHtml.includes("sess:sess-nasdaq-00"));
assert.ok(logHtml.includes("dec:dec-aapl-001"));
assert.ok(logHtml.includes("ord:ord-aapl-001"));
assert.ok(logHtml.includes("fill:fill-aapl-001"));
assert.ok(logHtml.includes("[REDACTED]"));

// Empty state
mockLogsResponse = { total_matching: 0, returned_count: 0, real_money_events_included: false, events: [] };
await testApi.loadStructuredPaperLogs();
assert.ok(doc.getElementById("mm-structured-log-rows").textContent.includes("No structured paper or Kronos events match"));

// Error state
logQueryShouldFail = true;
await testApi.loadStructuredPaperLogs();
assert.equal(doc.getElementById("log-error-banner").hidden, false);
assert.ok(doc.getElementById("log-error-banner").textContent.includes("SQLite audit ledger temporarily locked"));

console.log(`kronos_paper_and_logs_smoke (${usingJsdom ? "jsdom" : "vm-dom"}): PASS`);
