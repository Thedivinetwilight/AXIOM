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
let session = {
  session_id: "ui-session", mode: "PAPER", state: "UNCONFIGURED", paper_only: true,
  simulated: true, authorized_day: "", risk: null, entries_blocked: false,
  fault: "", fault_reason: "", fresh_approval_required: false, instruments: [instrument],
  account: null, kronos: { status: "UNAVAILABLE", issues: ["weights absent"], fallback: "non-Kronos" },
  evidence: [], diagnostics: [], working_instruments: [], scan_count: 0, last_scan: null,
  readiness: { ready: false, blockers: ["daily setup"] },
  operator_note: "PAPER is local simulation",
};
function response(body, ok = true, status = 200) {
  return Promise.resolve({ ok, status, json: async () => body });
}
window.sessionStorage.setItem("arena_token", "local-ui-test-only");
window.prompt = () => null;
window.confirm = () => true;
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
  if (path.startsWith("/api/autonomy/catalog?")) {
    return response({ total: 1, refreshed_at: "2026-10-25T00:00:00+00:00", scope: "test exact scope", instruments: [instrument], warnings: [] });
  }
  if (path === "/api/autonomy/sessions" && options.method === "POST") return response(session, true, 201);
  if (path.includes("/bars/")) return response({ bars: [] });
  if (path.endsWith("/authorize")) { session = { ...session, authorized_day: "2026-10-25" }; return response(session); }
  if (path.endsWith("/risk")) { session = { ...session, state: "READY", readiness: { ready: true, blockers: [] } }; return response(session); }
  if (path.endsWith("/arm")) { session = { ...session, state: "ARMED" }; return response(session); }
  if (path.endsWith("/activate")) { session = { ...session, state: "ACTIVE" }; return response(session); }
  if (path.endsWith("/scan") || path.endsWith("/reconcile")) return response(session);
  if (path === "/api/autonomy/sessions/ui-session") return response(session);
  throw new Error(`Unexpected local UI request: ${path}`);
};
window.eval(code);
const tick = () => new Promise(resolveTick => setImmediate(resolveTick));
const click = async id => { window.document.getElementById(id).click(); await tick(); await tick(); };

window.document.getElementById("selectedKeys").value = key;
await click("createSession");
assert.equal(window.document.getElementById("sessionId").textContent, "ui-session");
await click("authorize");
await click("saveRisk");
await click("arm");
assert.equal(window.document.getElementById("activate").disabled, false);
await click("activate");
assert.match(window.document.getElementById("modeBanner").textContent, /PAPER/);
assert.match(window.document.getElementById("modeBanner").textContent, /ACTIVE/);

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
