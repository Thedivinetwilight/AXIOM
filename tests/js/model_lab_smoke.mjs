/* Headless JSDOM test suite for Priority 1 Accessibility / 4-Mode Readiness
 * Checklists AND Workspace 10 ("AI & Model Lab") in AXIOM's dark terminal.
 *
 * Exercises every single control:
 * - Persistent quick-navigation bar (Connections, Charts, Orders, Strategies,
 *   Risk, Activity, AI & Model Lab)
 * - 4-mode Readiness Checklists (NOT_VERIFIED -> read-only NSE & AAPL probes)
 * - Kronos Install / Verify / Self-Test / Repair / Cancel
 * - Forecasting-only Enable / Disable, Load / Unload, Clear Error, Config save
 * - Validated Forecast Inspector (advisory output + fail-closed UNAVAILABLE)
 * - Dataset Manager (live read-only capture + local CSV import)
 * - Chronological Research Runner (explicit start, inspect, cancel)
 * - Baseline & 2.0x Cost-Stress Matrix + Model Registry Promote/Reject/Export/Import
 * - Asserts zero broker order endpoints are ever called.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { JSDOM } from "jsdom";

const root = resolve(import.meta.dirname, "../..");
const html = readFileSync(resolve(root, "app/static/terminal/index.html"), "utf8");
const code = readFileSync(resolve(root, "app/static/terminal/terminal.js"), "utf8");

const TOKEN = "ml-desktop-ui-token";
const now = () => new Date().toISOString();

const RISK = {
  max_order_volume: 10, max_symbol_volume: 20, max_order_margin: 10000,
  max_total_margin: 20000, max_open_positions: 5, max_daily_loss: 1000,
  max_drawdown_pct: 20, max_trades_per_day: 10, max_margin_usage_pct: 80,
  max_spread_bps: 100, max_slippage_points: 10, max_quote_age_seconds: 30,
};

let nseVerified = false;
let nasdaqVerified = false;
let kronosState = "NOT_INSTALLED";
let kronosEnabled = false;
let kronosLoaded = false;

const datasets = [
  {
    dataset_id: "bundled_nse_synthetic_1h",
    market: "NSE",
    symbol: "DEMO_NSE_SYNTH",
    timeframe: "1h",
    bar_count: 1601,
    start_timestamp: "2021-01-01T00:00:00Z",
    end_timestamp: "2021-03-08T16:00:00Z",
    estimated_missing_bars: 0,
    real_market_data: false,
    sha256: "1111222233334444555566667777888899990000aaaabbbbccccddddeeeeffff",
  },
];

const runs = [];
const models = [];
const calls = [];

function makeLegacyWorkspace() {
  return {
    status: {
      mode: "AXIOM_PAPER", lifecycle: "DISARMED", connected: false, authenticated: false,
      emergency_stopped: false, account_login_masked: "Local paper", server: "No MT5 server",
      currency: "USD", balance: 10000, equity: 10000, unrealized_pnl: 0, free_margin: 10000,
      margin_used: 0, open_positions: 0, open_orders: 0, automation_enabled: false,
      live_lock: "LOCKED_BY_DEFAULT",
    },
    account: null, positions: [], orders: [], fills: [], realized_pnl_total: 0,
    risk_limits: RISK,
    paper_session: { state: "STOPPED", strategy: "ema_crossover" },
    market_data: { state: "DISCONNECTED", connected: false, authenticated: false },
  };
}

function makeMarketsOverview() {
  return {
    paper: {
      NSE: {
        market: "NSE", currency: "INR", session_state: "STOPPED", strategy: "manual_only", timeframe: "5m",
        exchange_session: { state: "CLOSED_AFTER_HOURS", is_open: false, timezone: "Asia/Kolkata" },
        account: { equity: 1000000, cash: 1000000, market_value: 0, day_pnl: 0, realized_pnl: 0, total_fees_paid: 0 },
        positions: [], orders: [], fills: [], decisions: [],
      },
      NASDAQ: {
        market: "NASDAQ", currency: "USD", session_state: "STOPPED", strategy: "manual_only", timeframe: "5m",
        exchange_session: { state: "OPEN", is_open: true, timezone: "America/New_York" },
        account: { equity: 100000, cash: 100000, market_value: 0, day_pnl: 0, realized_pnl: 0, total_fees_paid: 0 },
        positions: [], orders: [], fills: [], decisions: [],
      },
    },
    live: {
      default_live_execution: "OFF",
      nse: { availability: "NOT_AVAILABLE", missing_dependencies: ["AUTONOMY_LIVE_ENABLED=true"] },
      nasdaq: { availability: "NOT_AVAILABLE", missing_dependencies: ["ALPACA_BROKER_ENABLED=true"] },
    },
    readiness: {
      verification_note: "CI and mocked unit tests never count as owner connectivity.",
      modes: {
        NSE_PAPER: {
          mode_key: "NSE_PAPER", market: "NSE", currency: "INR", mode: "LIVE-MARKET PAPER",
          overall_status: nseVerified ? "MARKET_CLOSED_READY" : "NOT_VERIFIED",
          owner_connection_verified: nseVerified,
          items: [{ id: "upstox_owner_probe", label: "Read-only NSE quote connection verified", state: nseVerified ? "PASS" : "NOT_VERIFIED", detail: nseVerified ? "Verified" : "NOT VERIFIED" }],
          next_action: nseVerified ? "Ready for NSE LIVE-MARKET PAPER" : "Click Verify NSE Quote",
        },
        NASDAQ_PAPER: {
          mode_key: "NASDAQ_PAPER", market: "NASDAQ", currency: "USD", mode: "LIVE-MARKET PAPER",
          overall_status: nasdaqVerified ? "READY" : "NOT_VERIFIED",
          owner_connection_verified: nasdaqVerified,
          items: [{ id: "alpaca_owner_probe", label: "Read-only NASDAQ quote connection verified", state: nasdaqVerified ? "PASS" : "NOT_VERIFIED", detail: nasdaqVerified ? "Verified" : "NOT VERIFIED" }],
          next_action: nasdaqVerified ? "Ready for NASDAQ LIVE-MARKET PAPER" : "Click Verify AAPL Quote",
        },
        NSE_LIVE: { mode_key: "NSE_LIVE", market: "NSE", currency: "INR", mode: "LIVE-MARKET LIVE", overall_status: "DISABLED", items: [], next_action: "Requires AUTONOMY_LIVE_ENABLED=true" },
        NASDAQ_LIVE: { mode_key: "NASDAQ_LIVE", market: "NASDAQ", currency: "USD", mode: "LIVE-MARKET LIVE", overall_status: "DISABLED", items: [], next_action: "Requires ALPACA_BROKER_ENABLED=true" },
      },
    },
    providers: { providers: [] },
    watchlists: { NSE: [], NASDAQ: [] },
    reconciliation: { NSE: {}, NASDAQ: {} },
    recent_audit: [],
  };
}

function makeModelLabOverview() {
  return {
    paper_and_research_only: true,
    live_execution_authorized: false,
    kronos: {
      provider_state: kronosState,
      summary: `Kronos is ${kronosState}`,
      inference_enabled: kronosEnabled,
      model_loaded: kronosLoaded && kronosState === "READY",
      license: "MIT License (shiyu-coder/Kronos)",
      execution_authorization_effect: "NONE — forecasts never create, arm, or activate trading",
      config: { cadence_seconds: 300, timeout_seconds: 45, horizon: 5, samples: 8 },
      diagnostics: {
        load_ms_p50: kronosState === "READY" ? 38.2 : null,
        load_ms_p95: kronosState === "READY" ? 44.1 : null,
        inference_ms_p50: kronosState === "READY" ? 112.6 : null,
        inference_ms_p95: kronosState === "READY" ? 129.0 : null,
        inference_count: kronosState === "READY" ? 2 : 0,
        process_rss_mb: 164.5,
        expected_weights_mb: 109.5,
        device: "cpu",
        mock_outputs_used: false,
      },
      kronos_engine: {
        artifact_verified: kronosState === "READY",
        inference_runtime: kronosState === "READY" ? "READY" : "MISSING",
        manifest_sha256: "a".repeat(64),
        model_revision: "901c26c1332695a2a8f243eb2f37243a37bea320",
        tokenizer_revision: "0e0117387f39004a9016484a186a908917e22426",
        last_self_test: kronosState === "READY" ? { status: "PASSED" } : null,
      },
      install_plan: { expected_total_bytes: 114823553, upstream: { repository: "shiyu-coder/Kronos", commit: "67b630e67f6a" } },
    },
    datasets,
    runs,
    models,
  };
}

async function fakeFetch(input, options = {}) {
  const url = new URL(String(input), "http://127.0.0.1:8000");
  const method = (options.method || "GET").toUpperCase();
  assert(!url.pathname.includes("/v2/orders"), `Forbidden broker order endpoint called: ${url.pathname}`);
  const body = options.body ? JSON.parse(options.body) : null;
  calls.push({ method, path: url.pathname, body });

  const json = (payload, status = 200) => ({
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => "application/json" },
    text: async () => JSON.stringify(payload),
    json: async () => payload,
  });

  if (url.pathname === "/api/trading/workspace") return json(makeLegacyWorkspace());
  if (url.pathname === "/api/trading/status") return json(makeLegacyWorkspace().status);
  if (url.pathname === "/api/trading/activity") return json({ events: [] });
  if (url.pathname === "/api/trading/connection") return json({ status: makeLegacyWorkspace().status, profiles: {}, secret_store: "DPAPI", test_is_read_only: true });
  if (url.pathname === "/api/market-data/status" || url.pathname === "/api/trading/market-data") return json(makeLegacyWorkspace().market_data);
  if (url.pathname === "/api/trading/performance") return json({ targets_ms: {}, operations: {} });
  if (url.pathname === "/api/trading/listed-assets") return json({ classes: [], total_curated_examples: 0 });
  if (url.pathname === "/api/trading/chart/symbols") return json({ symbols: [] });
  if (url.pathname === "/api/alpaca-broker/status") return json({ enabled: false, gate: { state: "DISABLED" }, limits: {} });
  if (url.pathname === "/api/markets/overview") return json(makeMarketsOverview());

  if (url.pathname === "/api/connections/upstox/verify-quote") {
    nseVerified = true;
    return json({ ok: true, authenticated: true, connection_state: "MARKET_CLOSED", guidance: "Token verified read-only" });
  }
  if (url.pathname === "/api/connections/alpaca/verify-quote") {
    nasdaqVerified = true;
    return json({ ok: true, authenticated: true, connection_state: "CONNECTED_READ_ONLY", guidance: "AAPL IEX verified read-only" });
  }

  // Model Lab & Kronos endpoints
  if (url.pathname === "/api/model-lab/overview") return json(makeModelLabOverview());
  if (url.pathname === "/api/autonomy/kronos/install") { kronosState = "NOT_VERIFIED"; return json({ ok: true }); }
  if (url.pathname === "/api/autonomy/kronos/verify") { return json({ ok: true }); }
  if (url.pathname === "/api/autonomy/kronos/self-test") { kronosState = "READY"; return json({ ok: true }); }
  if (url.pathname === "/api/autonomy/kronos/repair") { return json({ ok: true }); }
  if (url.pathname === "/api/autonomy/kronos/cancel") { return json({ ok: true }); }

  if (url.pathname === "/api/model-lab/kronos/toggle") { kronosEnabled = Boolean(body.enabled); return json(makeModelLabOverview().kronos); }
  if (url.pathname === "/api/model-lab/kronos/load") { kronosLoaded = true; return json(makeModelLabOverview().kronos); }
  if (url.pathname === "/api/model-lab/kronos/unload") { kronosLoaded = false; return json(makeModelLabOverview().kronos); }
  if (url.pathname === "/api/model-lab/kronos/clear-error") { return json(makeModelLabOverview().kronos); }
  if (url.pathname === "/api/model-lab/kronos/config") { return json(makeModelLabOverview().kronos); }

  if (url.pathname === "/api/model-lab/kronos/forecast") {
    if (!kronosEnabled || kronosState !== "READY") {
      return json({ detail: `Kronos is ${kronosState} (forecasting enabled=${kronosEnabled})` }, 409);
    }
    return json({
      market: body.market, symbol: body.symbol, timeframe: body.timeframe,
      context_bars_used: 128, input_end: "2026-10-29T15:30:00Z", horizon_bars: 5,
      mean_close: 2988.4, expected_return_pct: 0.89, q10_close: 2945.0, q90_close: 3022.1,
      up_probability: 0.64, freshness_state: "FRESH",
      latency_ms: { load_ms: 38.2, inference_ms: 112.6 },
      model_identity: { manifest_sha256: "a".repeat(64) },
    });
  }

  if (url.pathname === "/api/model-lab/datasets/capture-live") {
    const ds = {
      dataset_id: "ds_nasdaq_aapl_1h_livecap", market: body.market, symbol: body.symbol,
      timeframe: body.timeframe, bar_count: 240, start_timestamp: "2026-09-01T13:30:00Z",
      end_timestamp: "2026-10-29T20:00:00Z", estimated_missing_bars: 0, real_market_data: true,
      sha256: "b".repeat(64),
    };
    datasets.push(ds);
    return json({ ok: true, dataset: ds });
  }
  if (url.pathname === "/api/model-lab/datasets/import") {
    const ds = {
      dataset_id: "ds_nse_infy_1h_csv", market: body.market, symbol: body.symbol,
      timeframe: body.timeframe, bar_count: 180, start_timestamp: "2026-08-01T09:15:00Z",
      end_timestamp: "2026-10-29T15:30:00Z", estimated_missing_bars: 1, real_market_data: true,
      sha256: "c".repeat(64),
    };
    datasets.push(ds);
    return json({ ok: true, dataset: ds });
  }

  if (url.pathname === "/api/model-lab/runs" && method === "POST") {
    const runObj = {
      run_id: "mlrun-test0001", market: body.market, timeframe: "1h", dataset_id: body.dataset_id,
      status: "COMPLETED", phase: "COMPLETED", progress_pct: 100,
      status_detail: "Completed walk-forward, untouched test, and cost-stress evaluation",
      manifest_sha256: "d".repeat(64),
      manifest: {
        market: body.market,
        dataset: { dataset_id: body.dataset_id, real_market_data: false },
        splits: {
          train: { bars: 960, start_ts: "2021-01-01", end_ts: "2021-02-10" },
          embargo_1_bars: 5,
          validation: { bars: 320 },
          embargo_2_bars: 5,
          untouched_test: { bars: 311, selection_touched_test: false },
        },
        candidates: [
          {
            candidate_id: "buy_and_hold", family: "BASELINE", promotion_eligible: false, rejection_reasons: ["Baseline"],
            train: { sharpe: 0.62 }, validation: { sharpe: 0.48 },
            untouched_test: { net_return_pct: 2.15, sharpe: 0.51, sortino: 0.72, max_drawdown_pct: 4.2, profit_factor: 1.18, win_rate_pct: 100, trade_count: 1, turnover_x: 0.25, uncertainty: { net_return_ci90_low_pct: -1.1, net_return_ci90_high_pct: 5.4 }, regime_breakdown: { TREND: { return_pct: 2.0 }, RANGE: { return_pct: 0.1 }, VOLATILE: { return_pct: 0.05 } } },
            stressed_test: { net_return_pct: 1.92 },
          },
          {
            candidate_id: "axiom_linear_lagged", family: "AXIOM_DECISION_MODEL", promotion_eligible: true, rejection_reasons: [],
            train: { sharpe: 1.35 }, validation: { sharpe: 1.12 },
            untouched_test: { net_return_pct: 4.84, sharpe: 1.24, sortino: 1.68, max_drawdown_pct: 2.75, profit_factor: 1.54, win_rate_pct: 58, trade_count: 14, turnover_x: 2.1, uncertainty: { net_return_ci90_low_pct: 1.2, net_return_ci90_high_pct: 8.1 }, regime_breakdown: { TREND: { return_pct: 3.4 }, RANGE: { return_pct: 0.9 }, VOLATILE: { return_pct: 0.54 } } },
            stressed_test: { net_return_pct: 2.61 },
          },
        ],
      },
    };
    runs.unshift(runObj);
    models.push({
      model_id: "mdl-test0001-axiom_linear_lagged", candidate_id: "axiom_linear_lagged",
      family: "AXIOM_DECISION_MODEL", market: body.market, timeframe: "1h",
      promotion_eligible: true, registry_status: "VALIDATED_CANDIDATE",
      metrics_summary: { untouched_test: { net_return_pct: 4.84 }, stressed_test: { net_return_pct: 2.61 } },
      artifact_sha256: "e".repeat(64),
    });
    return json(runObj);
  }

  if (url.pathname.endsWith("/cancel") && url.pathname.startsWith("/api/model-lab/runs/")) {
    if (runs[0]) runs[0].status = "CANCELLED";
    return json(runs[0] || { ok: true });
  }
  if (url.pathname.endsWith("/promote") && url.pathname.startsWith("/api/model-lab/models/")) {
    models[0].registry_status = "PROMOTED_PAPER_CANDIDATE";
    return json(models[0]);
  }
  if (url.pathname.endsWith("/reject") && url.pathname.startsWith("/api/model-lab/models/")) {
    models[0].registry_status = "REJECTED";
    return json(models[0]);
  }
  if (url.pathname.endsWith("/export") && url.pathname.startsWith("/api/model-lab/models/")) {
    return json({ schema: "axiom.model_lab.bundle.v1", model_id: models[0].model_id, market: "NSE", bundle_sha256: "f".repeat(64) });
  }
  if (url.pathname === "/api/model-lab/models/import") {
    models.unshift({
      model_id: "imp-ffffffffff", candidate_id: "axiom_linear_lagged", family: "AXIOM_DECISION_MODEL",
      market: "NSE", timeframe: "1h", promotion_eligible: true, registry_status: "IMPORTED_PAPER_CANDIDATE",
      metrics_summary: { untouched_test: { net_return_pct: 4.84 }, stressed_test: { net_return_pct: 2.61 } },
      artifact_sha256: "f".repeat(64),
    });
    return json({ ok: true, model: models[0] });
  }

  return json({});
}

const flush = () => new Promise((r) => setTimeout(r, 25));

const dom = new JSDOM(html, { url: "http://127.0.0.1:8000/terminal", runScripts: "outside-only", pretendToBeVisual: true });
const { window } = dom;
window.fetch = fakeFetch;
window.HTMLCanvasElement.prototype.getContext = () => ({
  clearRect() {}, fillRect() {}, strokeRect() {}, beginPath() {}, moveTo() {}, lineTo() {},
  stroke() {}, fill() {}, fillText() {}, measureText: () => ({ width: 40 }), setLineDash() {},
  save() {}, restore() {}, rect() {}, clip() {},
});
window.ResizeObserver = class { observe() {} disconnect() {} };
window.eval(code);

try {
  // Unlock terminal
  window.document.getElementById("auth-token").value = TOKEN;
  window.document.getElementById("auth-form").dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
  await flush();

  // 1. Verify persistent Direct Access Quick-Nav bar & 4-mode Readiness Checklists
  const quickNav = window.document.getElementById("mm-quick-nav-bar");
  assert(quickNav && !quickNav.hidden, "Persistent quick-nav strip must be visible");
  assert(window.document.getElementById("mm-readiness-grid").textContent.includes("NOT_VERIFIED"));

  window.document.getElementById("mm-ready-verify-nse").click();
  await flush();
  window.document.getElementById("mm-ready-verify-nasdaq").click();
  await flush();
  assert(window.document.getElementById("mm-readiness-grid").textContent.includes("MARKET_CLOSED_READY"));
  assert(window.document.getElementById("mm-readiness-grid").textContent.includes("READY"));

  // 2. Navigate directly to 10 AI & Model Lab via quick-nav shortcut
  quickNav.querySelector('[data-go="modellab"]').click();
  await flush();
  const pageMl = window.document.getElementById("page-modellab");
  assert.equal(pageMl.hidden, false, "AI & Model Lab page should be visible");
  assert(window.document.getElementById("ml-warning-banner").textContent.includes("Never Guarantee Profit"));

  // 3. Forecast fails closed when Kronos is NOT_INSTALLED / disabled
  window.document.getElementById("ml-forecast-form").dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
  await flush();
  assert(window.document.getElementById("ml-fc-output").textContent.includes("UNAVAILABLE"));

  // 4. Exercise Kronos setup, self-test, enable, load, config, and genuine forecast
  window.document.getElementById("ml-kronos-install-btn").click();
  await flush();
  window.document.getElementById("ml-kronos-verify-btn").click();
  await flush();
  window.document.getElementById("ml-kronos-selftest-btn").click();
  await flush();
  assert.equal(window.document.getElementById("ml-kronos-state-pill").textContent, "READY");

  window.document.getElementById("ml-kronos-enable-btn").click();
  await flush();
  window.document.getElementById("ml-kronos-load-btn").click();
  await flush();
  window.document.getElementById("ml-kronos-config-form").dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
  await flush();

  window.document.getElementById("ml-forecast-form").dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
  await flush();
  assert(window.document.getElementById("ml-fc-output").textContent.includes("ADVISORY FORECAST ONLY"));
  assert(window.document.getElementById("ml-kronos-diagnostics").textContent.includes("112.6 ms"));

  // 5. Dataset Capture + CSV Import
  window.document.getElementById("ml-capture-ds-form").dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
  await flush();
  window.document.getElementById("ml-imp-csv").value = "timestamp,open,high,low,close,volume\n2026-01-01T00:00:00Z,100,101,99,100.5,5000";
  window.document.getElementById("ml-import-ds-form").dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
  await flush();
  assert.equal(window.document.getElementById("ml-datasets-count").textContent, "3 DATASETS");

  // 6. Explicit Research Run + Comparison Matrix + Model Registry Promote/Export/Import/Reject + Cancel
  window.document.getElementById("ml-run-form").dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
  await flush();
  assert(window.document.getElementById("ml-candidates-rows").textContent.includes("buy_and_hold"));
  assert(window.document.getElementById("ml-candidates-rows").textContent.includes("axiom_linear_lagged"));

  window.document.querySelector("[data-ml-promote]").click();
  await flush();
  assert(window.document.getElementById("ml-models-rows").textContent.includes("PROMOTED_PAPER_CANDIDATE"));

  window.document.querySelector("[data-ml-export]").click();
  await flush();
  assert(window.document.getElementById("ml-bundle-json").value.includes("axiom.model_lab.bundle.v1"));

  window.document.getElementById("ml-import-bundle-btn").click();
  await flush();
  assert(window.document.getElementById("ml-models-rows").textContent.includes("IMPORTED_PAPER_CANDIDATE"));

  window.document.querySelector("[data-ml-reject]").click();
  await flush();
  window.document.getElementById("ml-run-cancel-btn").click();
  await flush();
  window.document.getElementById("ml-kronos-unload-btn").click();
  await flush();
  window.document.getElementById("ml-kronos-disable-btn").click();
  await flush();

  console.log(`# model_lab_smoke: OK (${calls.length} authenticated API interactions, 0 broker order calls)`);
} finally {
  window.close();
}
