/* Test-only projection layer for the headless operator-console suites.
 *
 * The console no longer calls the removed legacy order surface; it composes its
 * views from existing read-only paper/state routes. Every smoke suite boots the
 * real terminal.js, so each one needs those routes served. Keeping the mapping
 * here means a console route change is a one-file test change, and the suites
 * keep exercising the paths the server actually exposes.
 *
 * No network, no credential, no order route: every response is a local fixture.
 */

function paperStatus(ws) {
  const session = ws.paper_session || {};
  return {
    mode: "PAPER",
    live_trading: false,
    paper_mode: true,
    state: session.state || "STOPPED",
    provider: session.provider ?? null,
    feed_kind: session.feed_kind ?? null,
    strategy: session.strategy ?? null,
    automation_enabled: session.automation_enabled === true,
    review_policy: session.review_policy ?? null,
    stop_reason: session.stop_reason ?? null,
    current_run: session.current_run ?? null,
    last_run: session.last_run ?? null,
    symbols: session.symbols || [],
    connection: session.connection || "LOCAL",
    processed_bars: session.processed_bars ?? 0,
  };
}

/** Read-only console routes. Returns undefined when the path is not handled. */
export function consoleReadRoutes(pathname, ws, { events = [], performance = null } = {}) {
  const workspace = ws || {};
  switch (pathname) {
    case "/paper/status":
      return paperStatus(workspace);
    case "/paper/account":
      return workspace.account || { currency: "USD", cash: 0, equity: 0, realized_pnl: 0, unrealized_pnl: 0 };
    case "/paper/positions":
      return { positions: workspace.positions || [] };
    case "/paper/orders":
      return { orders: workspace.orders || [] };
    case "/paper/trades":
      return { trades: workspace.deals || [] };
    case "/dashboard/data":
      return {
        mode: "PAPER",
        live_trading: false,
        emergency_stop: Boolean(workspace.status?.emergency_stop),
        emergency_reason: workspace.status?.emergency_reason ?? null,
      };
    case "/api/performance":
      return performance || workspace.performance || { metrics: {} };
    case "/api/connections/alpaca/credentials":
      return { slot: "ALPACA_MARKET_DATA", present: false, backend: "windows-dpapi", secret_returned: false };
    case "/api/markets/paper/audit/events":
      return { events };
    default:
      return undefined;
  }
}

/** Emergency/kill-switch routes the console calls. Returns undefined when unhandled. */
export function consoleWriteRoutes(pathname, method, body, hooks = {}) {
  if (String(method || "GET").toUpperCase() !== "POST") return undefined;
  switch (pathname) {
    case "/engine/stop":
    case "/api/markets/emergency-stop":
      hooks.halt?.();
      return { ok: true, stopped: true, reason: body?.reason ?? "operator emergency stop", subsystems: {} };
    case "/api/markets/emergency-stop/reset":
      hooks.clear?.();
      return { ok: true, live_execution_remains_off: true, subsystems: {} };
    case "/engine/resume":
      hooks.clear?.();
      return { ok: true, stopped: false, scope: "LEGACY_PAPER_SANDBOX" };
    default:
      return undefined;
  }
}
