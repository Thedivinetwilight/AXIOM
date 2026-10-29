/* Local-only autonomy UI interaction test; no account, network, or real credential. */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { JSDOM } from "jsdom";

const root = resolve(import.meta.dirname, "../..");
const html = readFileSync(resolve(root, "app/static/autonomy/index.html"), "utf8");
const code = readFileSync(resolve(root, "app/static/autonomy/autonomy.js"), "utf8");
const dom = new JSDOM(html, { url: "https://ui.test/autonomy", runScripts: "outside-only" });
const { window } = dom;
const calls = [];
const key = "NSE_EQ|INE000000000";
const instrument = {
  instrument_key: key, display_symbol: "NSE_EQ-INE000000000", trading_symbol: "TEST",
  product_label: "Upstox-listed equity", tick_size: 0.05, quantity_step: 1,
  risk_metadata_complete: true,
};
// Real complete.json.gz BOD row (Upstox format): the equity's padded `expiry`
// epoch must never block it; values below are the parser's normalized output
// for RELIANCE (raw tick_size 5.0 paise -> 0.05 rupees).
const relianceKey = "NSE_EQ|INE002A01018";
const reliance = {
  instrument_key: relianceKey, display_symbol: "NSE_EQ-INE002A01018",
  segment: "NSE_EQ", exchange: "NSE", trading_symbol: "RELIANCE",
  instrument_type: "EQ", tick_size: 0.05, quantity_step: 1, freeze_quantity: 100000,
  contract_multiplier: 1, expiry: null, market_kind: "EQUITY",
  product_label: "Upstox-listed equity", risk_metadata_complete: true,
};
let session = {
  session_id: "ui-session", mode: "PAPER", state: "UNCONFIGURED", paper_only: true,
  simulated: true, authorized_day: "", risk: null, entries_blocked: false,
  fault: "", fault_reason: "", fresh_approval_required: false, instruments: [instrument],
  account: null, kronos: { status: "UNAVAILABLE", issues: ["weights absent"], fallback: "non-Kronos" },
  evidence: [{
    strategy_family: "TREND_MOMENTUM", side: "BUY", instrument_key: key,
    trades: 25, net_expectancy: 0.1, max_drawdown: 0.1,
    provenance_class: "SYNTHETIC_RESEARCH", provenance_effect: "research only",
    paper_execution_effect: "BLOCKED_SYNTHETIC_RESEARCH_ONLY", live_eligible_structure: false,
  }],
  diagnostics: [], working_instruments: [], scan_count: 0, last_scan: null,
  last_scan_kind: "", scan_endpoint_effect: "READ_ONLY_NO_ORDER_MUTATIONS",
  orders: [], intents: [], emergency: {}, recovered: false, recovery_reconciled: false,
  readiness: { ready: false, blockers: ["daily setup"] },
  operator_note: "PAPER is local simulation",
};
let availableSessions = [];
const confirmations = [];
const manifestFile = "379ffadfb56911c7c85048e159286457008fcff684b2647b4ea847dc479785aa";
const installPlan = {
  manifest_sha256: "a".repeat(64), manifest_file_sha256: manifestFile,
  expected_total_bytes: 114823553, atomic_install: "verified generation then pointer",
  location: "per-user data only",
  source: { repository: "https://github.com/shiyu-coder/Kronos", commit: "67b630e67f6a18c9e9be918d9b4337c960db1e9a" },
  model: { repository: "NeoQuasar/Kronos-small", revision: "901c26c1332695a2a8f243eb2f37243a37bea320", expected_bytes: 98980884,
    files: [{ name: "model.safetensors", size: 98980656, sha256: "b082dfcbd8e8c142a725c8bbb99781802f38fec81210e13479effb32b3c3e020", url: "https://huggingface.co/pinned-model" }] },
  tokenizer: { repository: "NeoQuasar/Kronos-Tokenizer-base", revision: "0e0117387f39004a9016484a186a908917e22426", expected_bytes: 15842669,
    files: [{ name: "model.safetensors", size: 15842368, sha256: "59d85f6af76a2c3b8240ea06cb21db4213b4eeca053f246b23e29cf832fc6bee", url: "https://huggingface.co/pinned-tokenizer" }] },
  runtime: { python: "CPython 3.11/3.12", lock_sha256: "f".repeat(64), packages: [{ name: "torch", version: "2.7.1+cpu" }], minimum_free_bytes: 4294967296, indexes: ["https://pypi.org/simple"] },
};
let kronos = {
  status: "UNAVAILABLE", available_for_inference: false, issues: ["pinned files missing"],
  source: { status: "VERIFIED", files: { "model/kronos.py": { present: true, verified: true } } },
  model: { status: "UNAVAILABLE", files: { "model.safetensors": { present: false, verified: false, issue: "MISSING" } } },
  tokenizer: { status: "UNAVAILABLE", files: { "model.safetensors": { present: false, verified: false, issue: "MISSING" } } },
  runtime: { status: "UNAVAILABLE", installed: false }, operation: { state: "IDLE", active: false },
  install_plan: installPlan, weights_bundled: false, mock_outputs_used: false,
};
function response(body, ok = true, status = 200) {
  return Promise.resolve({ ok, status, json: async () => body });
}
window.sessionStorage.setItem("arena_token", "local-ui-test-only");
window.prompt = () => null;
window.confirm = message => { confirmations.push(String(message)); return true; };
window.LightweightCharts = {
  CandlestickSeries: {},
  createChart: () => ({
    addSeries: () => ({ setData: () => {} }),
    timeScale: () => ({ fitContent: () => {} }),
  }),
};
window.fetch = (url, options = {}) => {
  const path = String(url);
  calls.push({ path, options });
  if (path === "/api/autonomy/kronos" && !options.method) return response(kronos);
  if (path === "/api/autonomy/kronos/python" && options.method === "POST") {
    const selection = JSON.parse(options.body);
    const automatic = selection.executable === "";
    kronos = {
      ...kronos,
      runtime: {
        ...kronos.runtime,
        python: {
          version: [3, 11, 9], machine: "AMD64", system: "Windows", implementation: "CPython",
          bits: 64, pip: "24.0", selection: automatic ? "automatic" : "manual",
          command_label: automatic ? "py -3.11-64" : "manual selection",
        },
      },
    };
    return response(kronos);
  }
  if (path === "/api/autonomy/kronos/install" && options.method === "POST") {
    kronos = {
      ...kronos, status: "READY", available_for_inference: true, issues: [],
      model: { status: "VERIFIED", files: { "model.safetensors": { present: true, verified: true } } },
      tokenizer: { status: "VERIFIED", files: { "model.safetensors": { present: true, verified: true } } },
      runtime: { status: "READY", installed: true, device: "cpu", cuda_available: false },
      operation: { kind: "INSTALL_PINNED", state: "COMPLETED", active: false, phase: "COMPLETED" },
      last_inference: { status: "REAL_FORECAST" },
      last_self_test: { status: "PASSED", input_label: "SYNTHETIC_INFERENCE_SMOKE_ONLY_NOT_MARKET_EVALUATION", device: "cpu", load_ms: 2, inference_ms: 3, manifest_sha256: "a".repeat(64), runtime_lock_sha256: "f".repeat(64) },
    };
    return response(kronos, true, 202);
  }
  if (path === "/api/autonomy/kronos/self-test" && options.method === "POST") {
    kronos = { ...kronos, operation: { kind: "OFFLINE_SELF_TEST", state: "COMPLETED", active: false, phase: "COMPLETED" } };
    return response(kronos, true, 202);
  }
  if (path === "/api/autonomy/kronos/cancel" && options.method === "POST") return response(kronos);
  if (path.startsWith("/api/autonomy/catalog?")) {
    const query = decodeURIComponent((path.split("query=")[1] || "").split("&")[0]).trim().toUpperCase();
    const matches = [instrument, reliance].filter(item =>
      !query || item.instrument_key.toUpperCase().includes(query)
        || item.trading_symbol.toUpperCase().includes(query));
    return response({ total: matches.length, refreshed_at: "2026-10-25T00:00:00+00:00", scope: "test exact scope", instruments: matches, warnings: [],
      stats: { source_rows: 2, suspended_keys: 0, kept_by_segment: { NSE_EQ: 2 }, dropped_suspended: 0, dropped_unsupported: 0, dropped_invalid: 0, dropped_invalid_samples: [] } });
  }
  if (path === "/api/autonomy/status") return response({ sessions: availableSessions });
  if (path === "/api/autonomy/sessions" && options.method === "POST") return response(session, true, 201);
  if (path.includes("/bars/")) return response({ bars: [] });
  if (path.endsWith("/recover-view")) {
    session = {
      ...session, state: "HALTED", authorized_day: "", risk: null,
      recovered: true, recovery_reconciled: false, fresh_approval_required: true,
      readiness: { ready: false, blockers: ["fresh authorization", "fresh risk", "reconciliation"] },
    };
    return response(session);
  }
  if (path.endsWith("/authorize")) { session = { ...session, authorized_day: "2026-10-25" }; return response(session); }
  if (path.endsWith("/risk")) { session = { ...session, state: "READY", readiness: { ready: true, blockers: [] } }; return response(session); }
  if (path.endsWith("/arm")) { session = { ...session, state: "ARMED" }; return response(session); }
  if (path.endsWith("/activate")) {
    session = {
      ...session, state: "ACTIVE", working_instruments: [key],
      orders: [{ gtt_order_id: "PAPER-GTT-UI", instrument_token: key, rules: [{ strategy: "ENTRY", status: "OPEN", trigger_price: 100 }] }],
      intents: [{ intent_id: "ui-intent", instrument_key: key, state: "WORKING", broker_ids: ["PAPER-GTT-UI"] }],
    };
    return response(session);
  }
  if (path.endsWith("/cancel-unfilled")) {
    session = {
      ...session, working_instruments: [],
      orders: [{ gtt_order_id: "PAPER-GTT-UI", instrument_token: key, rules: [{ strategy: "ENTRY", status: "CANCELLED", trigger_price: 100 }] }],
      intents: [{ intent_id: "ui-intent", instrument_key: key, state: "CANCELLED", broker_ids: ["PAPER-GTT-UI"] }],
    };
    return response(session);
  }
  if (path.endsWith("/emergency-stop")) {
    session = {
      ...session, state: "EMERGENCY_STOPPED", working_instruments: [key],
      intents: [{ intent_id: "ui-intent", instrument_key: key, state: "UNRESOLVED", broker_ids: ["PAPER-GTT-UI"] }],
      emergency: { cleanup_status: "UNRESOLVED", managed_entries_resolved: false, account_clear: false, unresolved_orders: [{ intent_id: "ui-intent" }] },
    };
    return response(session);
  }
  if (path.endsWith("/scan")) {
    session = { ...session, last_scan_kind: "READ_ONLY", scan_endpoint_effect: "READ_ONLY_NO_ORDER_MUTATIONS" };
    return response(session);
  }
  if (path.endsWith("/reconcile")) return response(session);
  if (path === "/api/autonomy/sessions/ui-session") return response(session);
  throw new Error(`Unexpected local UI request: ${path}`);
};
window.eval(code);
const tick = () => new Promise(resolveTick => setImmediate(resolveTick));
const click = async id => { window.document.getElementById(id).click(); await tick(); await tick(); };

window.document.getElementById("selectedKeys").value = key;
await click("createSession");
assert.equal(window.document.getElementById("sessionId").textContent, "ui-session");
assert.equal(window.sessionStorage.getItem("autonomy_session_id"), "ui-session");
assert.match(window.document.getElementById("evidence").textContent, /BLOCKED_SYNTHETIC_RESEARCH_ONLY/);

// BOD regression (real complete.json.gz format): searching the symbol renders
// the correct NSE equity row and its Select button stores the exact key,
// which createSession then submits unchanged.
window.document.getElementById("catalogQuery").value = "RELIANCE";
await click("searchCatalog");
const rows = window.document.getElementById("catalogRows");
assert.match(rows.textContent, /RELIANCE/);
assert.match(rows.textContent, /NSE_EQ\|INE002A01018/);
assert.match(window.document.getElementById("catalogMeta").textContent, /BOD rows 2/);
assert.match(window.document.getElementById("catalogMeta").textContent, /kept NSE_EQ 2/);
rows.querySelector("[data-add]").click(); await tick();
assert.match(window.document.getElementById("selectedKeys").value, /NSE_EQ\|INE002A01018/);
await click("createSession");
const relianceCreate = calls.filter(call => call.path === "/api/autonomy/sessions").pop();
assert.ok(JSON.parse(relianceCreate.options.body).instrument_keys.includes(relianceKey));
assert.equal(JSON.parse(relianceCreate.options.body).mode, "PAPER");
availableSessions = [{ session_id: "ui-session", mode: "PAPER", state: "UNCONFIGURED" }];
await click("loadSession");
assert.match(window.document.getElementById("notice").textContent, /Loaded durable read-only view/);
assert.match(window.document.getElementById("modeBanner").textContent, /HALTED/);
assert(calls.some(call => call.path.endsWith("/recover-view") && call.options.method === "POST"));
await click("authorize");
await click("saveRisk");
await click("arm");
assert.equal(window.document.getElementById("activate").disabled, false);
await click("activate");
assert.match(window.document.getElementById("modeBanner").textContent, /PAPER/);
assert.match(window.document.getElementById("modeBanner").textContent, /ACTIVE/);
assert.equal(window.document.getElementById("cancelUnfilled").disabled, false);
await click("cancelUnfilled");
const cancel = calls.find(call => call.path.endsWith("/cancel-unfilled"));
assert.deepEqual(JSON.parse(cancel.options.body), { instrument_key: key });
await click("scan");
assert.match(window.document.getElementById("statusMetrics").textContent, /READ_ONLY/);
await click("emergency");
assert.match(window.document.getElementById("emergencyStatus").textContent, /UNRESOLVED/);
assert.match(window.document.getElementById("emergencyStatus").textContent, /Account clear is NOT claimed/);
assert.match(window.document.getElementById("intents").textContent, /UNRESOLVED/);

assert.match(window.document.getElementById("kronosInstallPlan").textContent, new RegExp(manifestFile));
const sessionBeforePython = JSON.stringify(session);
const pythonPath = "C:\\Users\\operator\\AppData\\Local\\Programs\\Python\\Python311\\python.exe";
window.document.getElementById("kronosPythonExecutable").value = pythonPath;
await click("selectKronosPython");
const manualPython = calls.find(call => call.path === "/api/autonomy/kronos/python");
assert.deepEqual(JSON.parse(manualPython.options.body), { executable: pythonPath });
assert.match(window.document.getElementById("kronosSetupStatus").textContent, /3.11.9 AMD64 64-bit · pip 24.0 · manual selection/);
assert(!window.document.getElementById("kronosSetupStatus").textContent.includes(pythonPath));
assert.equal(window.document.getElementById("kronosPythonExecutable").value, "");
assert.equal(JSON.stringify(session), sessionBeforePython);
await click("autoKronosPython");
assert.match(window.document.getElementById("kronosSetupStatus").textContent, /py -3.11-64/);
assert.equal(JSON.stringify(session), sessionBeforePython);
await click("installKronos");
assert(confirmations.some(text => text.includes("pinned model/tokenizer") && text.includes("No GPU/CUDA stack")));
assert(calls.some(call => call.path === "/api/autonomy/kronos/install" && call.options.method === "POST" && !call.options.body));
assert.match(window.document.getElementById("kronosSetupStatus").textContent, /model.safetensors: present, SHA-256 verified/);
assert.match(window.document.getElementById("kronosSetupStatus").textContent, /REAL_FORECAST/);
assert.equal(window.document.getElementById("testKronos").disabled, false);
kronos = { ...kronos, status: "DEGRADED", audit_status: "DEGRADED", issues: ["corrupt optional audit history"] };
await click("testKronos");
assert(calls.some(call => call.path === "/api/autonomy/kronos/self-test" && call.options.method === "POST" && !call.options.body));
assert.match(window.document.getElementById("kronosOperation").textContent, /OFFLINE_SELF_TEST · COMPLETED/);
assert.match(window.document.getElementById("kronosSetupPill").textContent, /DEGRADED/);
assert.match(window.document.getElementById("kronosSetupPill").className, /warn/);
assert.equal(window.document.getElementById("testKronos").disabled, false);

const create = calls.find(call => call.path === "/api/autonomy/sessions");
assert.deepEqual(JSON.parse(create.options.body).instrument_keys, [key]);
assert.equal(JSON.parse(create.options.body).mode, "PAPER");
for (const name of ["authorize", "risk", "arm", "activate"]) {
  assert(calls.some(call => call.path.endsWith(`/${name}`)), `missing ${name}`);
}
const authorize = calls.find(call => call.path.endsWith("/authorize"));
assert.equal(authorize.options.body, undefined);
for (const call of calls) {
  assert(!call.path.includes("place-order"));
  assert(!String(call.options.body || "").includes("access_token"));
  assert(!String(call.options.body || "").includes("password"));
  assert(!call.path.includes("localhost"));
}
assert(html.includes("/static/vendor/lightweight-charts/"));
assert(!html.includes("https://"));
console.log(`PASS autonomy UI: ${calls.length} local API interactions, separate Arm/Activate, no credential/order API`);
window.close();
