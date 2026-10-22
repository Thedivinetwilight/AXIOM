/* ===========================================================================
 * AXIOM-MONEY-PRINTER MAIN APPLICATION (3.2.0)
 *
 * The operating cockpit: COMMAND CENTER · MARKETS · RESEARCH · MODELS ·
 * TRADING · RISK · INTELLIGENCE · REPORTS · SYSTEM (master spec §45, §124).
 *
 * Static asset — no Python templating. All state comes from JSON endpoints.
 * Design rules implemented here (master spec §62, §93, §100):
 *   - every control has a backend action and visible busy/success/error state
 *   - missing data is shown as "—"/UNAVAILABLE, never as a fake zero
 *   - PAPER / SANDBOX / LIVE states are textually distinct everywhere
 *   - live controls render locked until the state machine reports them armed
 *   - every realtime panel carries a freshness indicator
 * =========================================================================== */
"use strict";

(function () {
  var state = {
    boot: null,
    view: location.hash.replace(/^#\/?/, "") || "command",
    paper: null,
    paperStatusFresh: false,
    paperStatusUpdatedAt: null,
    account: null,
    orders: [],
    trades: [],
    positions: [],
    dash: null,
    equityHistory: [],   // session-scoped rolling equity series (client-observed)
    strategies: [],
    datasets: [],
    polling: null,
    paperPoller: null,
    pollingVisibilityHandler: null,
    pollingPageHideHandler: null,
    pollingPageShowHandler: null,
    tokenGatePassed: false
  };

  var mainEl = null;

  // ------------------------------------------------------------------ boot
  function start() {
    mainEl = document.getElementById("app-root");
    Arena.boot().then(function (d) {
      state.boot = d;
      if (!Arena.getToken()) { gate(); return; }
      state.tokenGatePassed = true;
      buildShell();
      show(state.view);
      startPolling();
      document.addEventListener("keydown", function (ev) {
        if (ev.key !== "F" && ev.key !== "f") return;
        var t = ev.target;
        if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable)) return;
        toggleFocusMode();
      });
    }).catch(function (e) {
      mainEl.innerHTML = "";
      mainEl.appendChild(Arena.el("div", { "class": "banner err", style: "max-width:560px;margin:60px auto" }, [
        Arena.el("b", {}, [document.createTextNode("Arena backend not reachable")]),
        document.createTextNode(e.message),
        Arena.el("div", { "class": "detail" }, [document.createTextNode(
          "The page loaded but the API did not answer. Reload, or check the Arena console window / logs/arena.log.")])
      ]));
    });
  }

  // ------------------------------------------------------------- token gate
  function gate() {
    mainEl.innerHTML = "";
    var card = Arena.el("div", { "class": "card", style: "max-width:520px;margin:60px auto" }, [
      Arena.el("div", { "class": "card-head" }, [Arena.el("h3", {}, [document.createTextNode("Unlock Arena")])]),
      Arena.el("div", { "class": "card-body" }, [
        Arena.el("p", { "class": "muted" }, [document.createTextNode(
          "This screen reads protected data and needs your local API token. If you completed the guided setup, " +
          "the token was handed to this browser automatically — try ")
        ]),
        Arena.el("div", { "class": "field" }, [
          Arena.el("label", { "for": "gate-token" }, [document.createTextNode("API token")]),
          Arena.el("input", { type: "password", id: "gate-token", autocomplete: "off" })
        ]),
        Arena.el("div", { "class": "banner err", id: "gate-error", style: "display:none" }),
        Arena.el("div", { "class": "btn-row" }, [
          Arena.el("button", { "class": "btn primary", id: "gate-unlock" }, [document.createTextNode("Unlock")]),
          Arena.el("a", { "class": "btn ghost", href: "/setup" }, [document.createTextNode("Open setup instead")])
        ])
      ])
    ]);
    mainEl.appendChild(card);
    var btn = document.getElementById("gate-unlock");
    function attempt() {
      var value = document.getElementById("gate-token").value.trim();
      var errBox = document.getElementById("gate-error");
      if (!value) {
        errBox.style.display = "block";
        errBox.textContent = "Paste the token from your Arena configuration (.env), or run the setup to create a new one.";
        return;
      }
      Arena.setToken(value);
      Arena.get("/paper/status").then(function () {
        state.tokenGatePassed = true;
        buildShell();
        show(state.view);
        startPolling();
      }).catch(function (e) {
        Arena.setToken("");
        errBox.style.display = "block";
        errBox.textContent = e.message;
      });
    }
    btn.addEventListener("click", attempt);
    document.getElementById("gate-token").addEventListener("keydown", function (e) {
      if (e.key === "Enter") attempt();
    });
  }

  // ------------------------------------------------------------------ shell
  function buildShell() {
    document.body.innerHTML = "";
    var app = Arena.el("div", { "class": "app" });

    var brand = Arena.el("div", { "class": "brand" }, [
      Arena.el("div", { "class": "logo" }, [document.createTextNode("Ω")]),
      Arena.el("div", { "class": "name" }, [
        document.createTextNode("AXIOM"),
        Arena.el("small", {}, [document.createTextNode("MONEY-PRINTER workstation")])
      ])
    ]);

    var topbar = Arena.el("div", { "class": "topbar", id: "topbar" }, [
      Arena.modeBadge(state.boot),
      Arena.el("span", { id: "live-chip" }, [Arena.stateBadge("OFFLINE")]),
      Arena.el("span", { id: "live-switch-host", style: "display:inline-flex;align-items:center;gap:8px" }),
      Arena.el("span", { id: "market-chip" }, [document.createTextNode("market: —")]),
      Arena.el("span", { id: "conn-badge" }, [Arena.stateBadge("CONNECTING")]),
      Arena.el("span", { "class": "spacer" }),
      Arena.el("button", { "class": "btn ghost sm", id: "open-palette", title: "Ctrl+K",
        onclick: function () { openPalette(); } }, [document.createTextNode("⌘ Search (Ctrl+K)")]),
      Arena.el("span", { "class": "faint mono", id: "build-label" },
        [document.createTextNode("AXIOM " + state.boot.version + " · " + state.boot.build_id)]),
      Arena.el("span", { id: "engine-controls" })
    ]);

    var nav = Arena.el("nav", { "class": "sidenav", id: "sidenav" });
    [
      ["command", "◧", "Command Center"],
      ["markets", "◎", "Markets"],
      ["forex", "¤", "Forex (blocked)"],
      ["research", "⚗", "Research"],
      ["models", "▲", "Models"],
      ["trading", "⇄", "Trading"],
      ["risk", "⛨", "Risk"],
      ["intelligence", "◉", "Intelligence"],
      ["reports", "▦", "Reports"],
      ["system", "⚙", "System"]
    ].forEach(function (item) {
      var link = Arena.el("div", {
        "class": "navlink", "data-view": item[0], tabindex: "0", role: "button", id: "nav-" + item[0]
      }, [
        Arena.el("span", { "class": "ico" }, [document.createTextNode(item[1])]),
        document.createTextNode(item[2])
      ]);
      link.addEventListener("click", function () { show(item[0]); });
      link.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); show(item[0]); }
      });
      nav.appendChild(link);
    });
    nav.appendChild(Arena.el("div", { style: "flex:1" }));

    var main = Arena.el("main", { "class": "main", id: "main-view" });
    var statusbar = Arena.el("div", { "class": "statusbar", id: "statusbar" }, [
      Arena.el("span", { id: "sb-feed" }, [document.createTextNode("Feed —")]),
      Arena.el("span", { id: "sb-engine" }, [document.createTextNode("Engine —")]),
      Arena.el("span", { id: "sb-research" }, [document.createTextNode("Research —")]),
      Arena.el("span", { id: "sb-kronos" }, [document.createTextNode("Kronos —")]),
      Arena.el("span", { id: "sb-risk" }, [document.createTextNode("Risk —")]),
      Arena.el("span", { id: "sb-tasks" }, [document.createTextNode("Tasks —")]),
      Arena.el("span", { "class": "spacer" }),
      Arena.el("span", { id: "sb-freshness", "class": "faint" }, [document.createTextNode("updated —")])
    ]);
    app.appendChild(brand); app.appendChild(topbar); app.appendChild(nav); app.appendChild(main); app.appendChild(statusbar);
    document.body.appendChild(app);
    document.body.appendChild(Arena.el("div", { id: "toasts" }));
    mainEl = main;
    renderEngineControls();
    connectStream();
    refreshMarketChip();
    refreshLiveChip();
    mountLiveSwitch();
  }

  // The PAPER/LIVE switch lives in the topbar, next to the mode badge, so
  // the effective trading mode is visible from every screen. It is a
  // navigation control, not a toggle: clicking LIVE opens the activation
  // wizard, and moving away from LIVE opens the safe drain flow.
  function mountLiveSwitch() {
    var host = document.getElementById("live-switch-host");
    if (!host) return;
    if (window.ArenaLive && typeof window.ArenaLive.mountSwitch === "function") {
      window.ArenaLive.mountSwitch(host);
      return;
    }
    host.appendChild(Arena.el("span", { "class": "faint", style: "font-size:11px" },
      [document.createTextNode("switch unavailable")]));
  }

  function show(view) {
    state.view = view;
    location.hash = "/" + view;
    document.querySelectorAll(".navlink[data-view]").forEach(function (n) {
      n.classList.toggle("active", n.getAttribute("data-view") === view);
    });
    mainEl.innerHTML = "";
    mainEl.scrollTop = 0;
    var views = {
      command: viewCommand, markets: viewMarkets, forex: viewForex, research: viewResearch,
      models: viewModels, trading: viewTrading, risk: viewRisk,
      intelligence: viewIntelligence, reports: viewReports,
      execution: viewTrading, events: viewIntelligence, alerts: viewIntelligence,
      settings: viewSettings, system: viewSystem
    };
    try {
      (views[view] || viewCommand)(mainEl);
    } catch (e) {
      mainEl.appendChild(Arena.el("div", { "class": "banner err" }, [
        Arena.el("b", {}, [document.createTextNode("This screen could not render")]),
        document.createTextNode(String(e && e.message || e)),
        Arena.el("div", { "class": "detail" }, [document.createTextNode(
          "The backend is unaffected. Switch to another screen or reload.")])
      ]));
    }
  }

  // ------------------------------------------------------------ polling
  var PAPER_POLL_DEADLINE_MS = 5000;
  var PAPER_STATUS_FRESH_MS = 8000;

  // Kept independent of DOM/network APIs so timeout and response ordering can
  // be exercised behaviorally under Node as well as in the browser.
  function createPaperPoller(requests, callbacks, deadlineMs, freshnessMs) {
    var generation = 0;
    var statusRevision = 0;
    var active = false;
    var stopped = false;
    var cycleTimer = null;
    var freshnessTimer = null;

    function clearCycleTimer() {
      if (cycleTimer !== null) clearTimeout(cycleTimer);
      cycleTimer = null;
    }

    function clearFreshnessTimer() {
      if (freshnessTimer !== null) clearTimeout(freshnessTimer);
      freshnessTimer = null;
    }

    function markStatus(result) {
      statusRevision += 1;
      clearFreshnessTimer();
      callbacks.onStatus(result);
      if (result.status !== "fulfilled") return;
      var revision = statusRevision;
      freshnessTimer = setTimeout(function () {
        freshnessTimer = null;
        if (!stopped && revision === statusRevision) callbacks.onStale("freshness_deadline");
      }, freshnessMs);
    }

    function poll() {
      if (stopped || active) return false;
      active = true;
      var cycle = ++generation;
      var results = new Array(requests.length);
      var settled = 0;

      function isCurrent() { return !stopped && cycle === generation; }
      function finish(timedOut) {
        if (!isCurrent()) return;
        clearCycleTimer();
        active = false;
        if (timedOut) {
          // Invalidate every outstanding promise before allowing another poll.
          generation += 1;
          if (!results[0]) {
            results[0] = { status: "rejected", reason: new Error("paper status poll timed out") };
            markStatus(results[0]);
          }
        }
        callbacks.onComplete(results, timedOut);
      }

      cycleTimer = setTimeout(function () { finish(true); }, deadlineMs);
      requests.forEach(function (request, index) {
        Promise.resolve().then(request).then(function (value) {
          if (!isCurrent()) return;
          results[index] = { status: "fulfilled", value: value };
          settled += 1;
          if (index === 0) markStatus(results[index]);
          if (settled === requests.length) finish(false);
        }, function (error) {
          if (!isCurrent()) return;
          results[index] = { status: "rejected", reason: error };
          settled += 1;
          if (index === 0) markStatus(results[index]);
          if (settled === requests.length) finish(false);
        });
      });
      return true;
    }

    function invalidate() {
      if (stopped) return;
      generation += 1;
      active = false;
      clearCycleTimer();
    }

    function stop() {
      if (stopped) return;
      stopped = true;
      generation += 1;
      active = false;
      clearCycleTimer();
      clearFreshnessTimer();
    }

    return { poll: poll, invalidate: invalidate, stop: stop };
  }

  function stopPolling(keepPageLifecycleListeners) {
    if (state.polling !== null) clearInterval(state.polling);
    state.polling = null;
    if (state.paperPoller) state.paperPoller.stop();
    state.paperPoller = null;
    if (state.pollingVisibilityHandler) {
      document.removeEventListener("visibilitychange", state.pollingVisibilityHandler);
      state.pollingVisibilityHandler = null;
    }
    if (!keepPageLifecycleListeners && state.pollingPageHideHandler) {
      window.removeEventListener("pagehide", state.pollingPageHideHandler);
      state.pollingPageHideHandler = null;
    }
    if (!keepPageLifecycleListeners && state.pollingPageShowHandler) {
      window.removeEventListener("pageshow", state.pollingPageShowHandler);
      state.pollingPageShowHandler = null;
    }
  }

  function startPolling() {
    stopPolling();
    state.paperStatusFresh = false;
    refreshLive();
    var paths = [
      "/paper/status", "/paper/account", "/paper/orders?limit=60",
      "/paper/trades?limit=60", "/paper/positions", "/dashboard/data"
    ];
    state.paperPoller = createPaperPoller(paths.map(function (path) {
      return function () { return Arena.get(path); };
    }), {
      onStatus: function (result) {
        state.paperStatusFresh = result.status === "fulfilled";
        if (state.paperStatusFresh) {
          state.paper = result.value;
          state.paperStatusUpdatedAt = Date.now();
        }
        refreshLive();
      },
      onStale: function () {
        state.paperStatusFresh = false;
        setConn("STALE");
        refreshLive();
      },
      onComplete: function (results) {
        var okCount = 0;
        results.forEach(function (r) { if (r && r.status === "fulfilled") okCount += 1; });
        if (okCount === 0) {
          setConn("ERROR");
          refreshLive();
          return;
        }
        setConn(okCount === results.length ? "CONNECTED" : "STALE");
        if (results[1] && results[1].status === "fulfilled") {
          state.account = results[1].value;
          if (state.account && state.account.equity !== null && state.account.equity !== undefined) {
            state.equityHistory.push({ x: new Date().toLocaleTimeString(), y: state.account.equity });
            if (state.equityHistory.length > 400) state.equityHistory.shift();
          }
        }
        if (results[2] && results[2].status === "fulfilled") state.orders = results[2].value.orders || [];
        if (results[3] && results[3].status === "fulfilled") state.trades = results[3].value.trades || [];
        if (results[4] && results[4].status === "fulfilled") state.positions = results[4].value.positions || [];
        if (results[5] && results[5].status === "fulfilled") state.dash = results[5].value;
        refreshLive();
      }
    }, PAPER_POLL_DEADLINE_MS, PAPER_STATUS_FRESH_MS);

    state.polling = setInterval(pollOnce, 3000);
    state.pollingVisibilityHandler = function () { if (!document.hidden) pollOnce(); };
    state.pollingPageHideHandler = function () { stopPolling(true); };
    state.pollingPageShowHandler = function () { if (!state.polling) startPolling(); };
    document.addEventListener("visibilitychange", state.pollingVisibilityHandler);
    window.addEventListener("pagehide", state.pollingPageHideHandler);
    window.addEventListener("pageshow", state.pollingPageShowHandler);
    pollOnce();
  }

  function pollOnce(force) {
    if (document.hidden || !state.paperPoller) return;
    if (force) state.paperPoller.invalidate();
    state.paperPoller.poll();
  }

  function paperStatusPresentation(fresh, paper) {
    if (fresh) return { stale: false, label: null, lastKnown: null, stopLabel: null };
    var lastKnown = paper && paper.state ? paper.state : null;
    return {
      stale: true,
      label: "PAPER STATUS STALE",
      lastKnown: lastKnown,
      stopLabel: lastKnown === "RUNNING" ? "STOP PAPER · LAST KNOWN RUNNING" : null
    };
  }

  function setConn(status) {
    var holder = document.getElementById("conn-badge");
    if (holder) { holder.innerHTML = ""; holder.appendChild(Arena.stateBadge(status)); }
  }

  // Called on every poll AND after navigation; only updates in-place nodes so
  // forms being edited are never clobbered.
  function refreshLive() {
    renderEngineControls();
    var badges = document.querySelectorAll("[data-live-bind]");
    badges.forEach(function (node) {
      var key = node.getAttribute("data-live-bind");
      var value = lookup(state, key);
      node.innerHTML = "";
      if (value instanceof Node) node.appendChild(value);
      else node.textContent = value === null || value === undefined ? "—" : String(value);
    });
    var agers = document.querySelectorAll("[data-live-ago]");
    agers.forEach(function (node) {
      node.textContent = Arena.timeAgo(lookup(state, node.getAttribute("data-live-ago")));
    });
    if (state.view === "command" && document.getElementById("cc-equity-chart")) {
      Arena.lineChart(document.getElementById("cc-equity-chart"), state.equityHistory, {
        label: "session equity", color: "#34d399", height: 160,
        format: function (v) { return Arena.fmtNum(v, 0); },
        emptyText: "Equity appears here while the paper engine runs (observed this browser session)."
      });
    }
  }

  function lookup(obj, path) {
    return path.split(".").reduce(function (o, k) { return o === null || o === undefined ? o : o[k]; }, obj);
  }

  // ------------------------------------------------------ engine controls
  function renderEngineControls() {
    var host = document.getElementById("engine-controls");
    if (!host) return;
    host.innerHTML = "";
    var paperState = state.paper ? state.paper.state : "UNKNOWN";
    var presentation = paperStatusPresentation(state.paperStatusFresh, state.paper);

    if (presentation.stale) {
      var lastSeen = state.paperStatusUpdatedAt
        ? " · last seen " + new Date(state.paperStatusUpdatedAt).toLocaleTimeString()
        : " · no successful status read yet";
      host.appendChild(Arena.el("span", { "class": "badge warn", id: "paper-status-stale" }, [
        document.createTextNode(presentation.label + lastSeen +
          (presentation.lastKnown ? " · last known " + presentation.lastKnown : ""))
      ]));
      // Keep a safe stop path available for a last-known running session;
      // never offer START from a stale stopped/unknown snapshot.
      if (presentation.stopLabel) addStopButton(presentation.stopLabel);
      return;
    }

    if (paperState === "STARTING") {
      host.appendChild(Arena.el("span", { "class": "badge info" }, [
        document.createTextNode("PAPER STARTING · awaiting provider status")
      ]));
      return;
    }

    if (paperState === "PAUSED") {
      addStopButton("STOP PAPER");
      host.appendChild(btn("RESUME", "ghost", "pause-resume-top", function (b) {
        Arena.setBusy(b, true);
        Arena.post("/paper/resume").then(function () { pollOnce(true); }).catch(function (e) {
          Arena.setBusy(b, false); Arena.toast("err", e.message); pollOnce(true);
        });
      }));
      return;
    }

    if (paperState === "RUNNING") {
      addStopButton("STOP PAPER");
      host.appendChild(btn("PAUSE", "ghost", "pause-resume-top", function (b) {
        var target = "/paper/pause";
        Arena.setBusy(b, true);
        Arena.post(target).then(function () { pollOnce(true); }).catch(function (e) {
          Arena.setBusy(b, false); Arena.toast("err", e.message); pollOnce(true);
        });
      }));
    } else {
      host.appendChild(btn("START PAPER", "success", "start-paper-top", function (b) {
        Arena.setBusy(b, true, "Requesting…");
        state.paperStatusFresh = false;
        refreshLive();
        Arena.post("/api/paper/start-default", { source: state.boot.data_source || "replay" })
          .then(function (result) {
            var acceptedState = result && result.state ? result.state : "accepted";
            Arena.toast("info", "Paper start accepted (" + acceptedState + "); checking current engine and feed status.");
            pollOnce(true);
          })
          .catch(function (e) {
            Arena.setBusy(b, false);
            Arena.toast("err", e.message);
            pollOnce(true);
          });
      }));
    }

    function addStopButton(label) {
      host.appendChild(btn(label, "danger", "stop-paper-top", function (b) {
        Arena.confirmDialog("Stop paper trading?",
          "Open orders are cancelled and the session is persisted. " +
          (state.paperStatusFresh ? "You can start again at any time." :
            "Status polling is stale; this uses the last known state and must be verified after polling recovers."),
          "Stop paper", true, function () {
            Arena.setBusy(b, true, "Stopping…");
            Arena.post("/paper/stop").then(function () {
              Arena.toast("info", "Paper stop request accepted; verifying current status.");
              pollOnce(true);
            }).catch(function (e) { Arena.setBusy(b, false); Arena.toast("err", e.message); pollOnce(true); });
          });
      }));
    }

    function btn(label, kind, id, onClick) {
      var b = Arena.el("button", { "class": "btn " + kind + " sm", id: id }, [document.createTextNode(label)]);
      b.addEventListener("click", function () { onClick(b); });
      return b;
    }
  }

  // ======================================================== COMMAND CENTER
  function viewCommand(root) {
    root.appendChild(Arena.el("h1", { "class": "page-title" }, [document.createTextNode("Command Center")]));
    root.appendChild(Arena.el("p", { "class": "page-sub" }, [document.createTextNode(
      "One screen for everything that needs attention: account, risk, market, strategy, execution, learning and alerts.")]));

    // mode strip
    var stripCls = state.boot.broker === "UPSTOX_LIVE" && state.boot.live_trading ? "live"
      : (state.boot.broker === "UPSTOX_SANDBOX" ? "sandbox" : "paper");
    root.appendChild(Arena.el("div", { "class": "mode-strip " + stripCls }, [
      Arena.modeBadge(state.boot),
      Arena.el("span", { style: "font-weight:500;font-size:13px" }, [document.createTextNode(
        stripCls === "paper" ? "All orders are simulated. No broker is contacted."
        : stripCls === "sandbox" ? "Orders go to Upstox SANDBOX only — no real money."
        : "REAL MONEY ROUTING IS ACTIVE — verify before any action.")]),
      Arena.el("span", { style: "flex:1" }),
      Arena.el("span", { "data-live-bind": "paper.state" })
    ]));

    // ACCOUNT row
    root.appendChild(sectionTitle("Account"));
    var accountGrid = Arena.el("div", { "class": "grid cols-4" });
    accountGrid.appendChild(stat("Starting capital", bootSetting("initial_capital"), "sub", "config"));
    accountGrid.appendChild(statNode("Equity", bindMoney("account.equity", "account.currency")));
    accountGrid.appendChild(statNode("Cash", bindMoney("account.cash", "account.currency")));
    accountGrid.appendChild(statNode("Realized P&L", bindMoney("account.realized_pnl", "account.currency")));
    accountGrid.appendChild(statNode("Unrealized P&L", bindMoney("account.unrealized_pnl", "account.currency")));
    accountGrid.appendChild(statNode("Daily P&L", bindMoney("account.daily_pnl", "account.currency")));
    root.appendChild(accountGrid);

    // equity chart
    var chartCard = card("Equity (this browser session)", Arena.el("div", { id: "cc-equity-chart" }));
    root.appendChild(Arena.el("div", { "class": "section-gap" }, [chartCard]));

    // RISK row
    root.appendChild(sectionTitle("Risk"));
    var riskGrid = Arena.el("div", { "class": "grid cols-4" });
    riskGrid.appendChild(statNode("Drawdown", bindPct("account.drawdown")));
    riskGrid.appendChild(statNode("Max drawdown", bindPct("account.max_drawdown")));
    riskGrid.appendChild(statNode("Open positions", bindCount("positions.length")));
    riskGrid.appendChild(statNode("Trades recorded", bindCount("trades.length")));
    riskGrid.appendChild(stat("Exposure", null, "sub", null, bindNode(function () {
      return state.account && state.account.exposed ? Arena.stateBadge("ACTIVE") : Arena.stateBadge("FLAT");
    })));
    riskGrid.appendChild(statNode("Emergency stop", bindNode(function () {
      var stopped = state.dash && state.dash.emergency_stop;
      return Arena.stateBadge(stopped ? "BLOCKED" : "READY");
    })));
    root.appendChild(riskGrid);

    // MARKET + STRATEGY
    var twoCol = Arena.el("div", { "class": "grid cols-2 section-gap" });
    twoCol.appendChild(card("Market data", marketPanel()));
    twoCol.appendChild(card("Strategy", strategyPanel()));
    root.appendChild(twoCol);

    // EXECUTION + LEARNING
    var twoCol2 = Arena.el("div", { "class": "grid cols-2 section-gap" });
    twoCol2.appendChild(card("Execution", executionPanel()));
    twoCol2.appendChild(card("Learning & research", learningPanel()));
    root.appendChild(twoCol2);

    // ALERTS + MARKET INTELLIGENCE
    var twoCol3 = Arena.el("div", { "class": "grid cols-2 section-gap" });
    twoCol3.appendChild(card("Alerts", alertsPanel(true)));
    twoCol3.appendChild(card("Market intelligence (observational)", marketEventsPanel()));
    root.appendChild(twoCol3);

    // SYSTEM strip
    root.appendChild(sectionTitle("System"));
    var sysGrid = Arena.el("div", { "class": "grid cols-4" });
    sysGrid.appendChild(stat("Engine", null, "sub", null, bindNode(function () { return Arena.stateBadge(state.paper ? state.paper.state : "UNKNOWN"); })));
    sysGrid.appendChild(stat("Mode", state.boot.mode_display, "sub"));
    sysGrid.appendChild(stat("Version", state.boot.version + " · " + state.boot.build_id, "sub"));
    sysGrid.appendChild(stat("Data directory", state.boot.arena_home, "sub"));
    root.appendChild(sysGrid);
  }

  function sectionTitle(text) {
    return Arena.el("h2", { style: "font-size:13px;letter-spacing:.09em;text-transform:uppercase;color:#7d89a3;margin:26px 0 10px" }, [document.createTextNode(text)]);
  }

  // Workspace customization (spec §61): per-card collapse/maximize state is
  // persisted; named layouts apply bundles of these preferences.
  function cardSlug(title) {
    return String(title || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").slice(0, 48);
  }
  function cardPrefs() {
    try { return JSON.parse(localStorage.getItem("axiom_card_prefs")) || {}; }
    catch (e) { return {}; }
  }
  function saveCardPrefs(prefs) {
    localStorage.setItem("axiom_card_prefs", JSON.stringify(prefs));
  }

  function card(title, bodyNode, actions) {
    var slug = cardSlug(title);
    var prefs = cardPrefs()[slug] || {};
    var bodyWrap = Arena.el("div", { "class": "card-body" + (prefs.collapsed ? " collapsed" : "") }, [bodyNode]);
    var collapseBtn = Arena.el("button", {
      "class": "btn ghost sm", "aria-label": "Collapse " + title,
      style: "padding:0 6px;font-size:11px"
    }, [document.createTextNode(prefs.collapsed ? "▸" : "▾")]);
    collapseBtn.addEventListener("click", function () {
      var all = cardPrefs();
      var mine = all[slug] || {};
      mine.collapsed = !bodyWrap.classList.contains("collapsed");
      all[slug] = mine; saveCardPrefs(all);
      bodyWrap.classList.toggle("collapsed", mine.collapsed);
      collapseBtn.textContent = mine.collapsed ? "▸" : "▾";
    });
    var maxBtn = Arena.el("button", {
      "class": "btn ghost sm", "aria-label": "Maximize " + title,
      style: "padding:0 6px;font-size:11px"
    }, [document.createTextNode("⤢")]);
    var head = Arena.el("div", { "class": "card-head" }, [
      Arena.el("h3", {}, [document.createTextNode(title)]),
      Arena.el("span", { "class": "grow" }), collapseBtn, maxBtn]);
    if (actions) { actions.forEach(function (a) { head.appendChild(a); }); }
    var box = Arena.el("div", { "class": "card" + (prefs.maximized ? " maximized" : "") }, [head, bodyWrap]);
    maxBtn.addEventListener("click", function () {
      var all = cardPrefs();
      var mine = all[slug] || {};
      mine.maximized = !box.classList.contains("maximized");
      all[slug] = mine; saveCardPrefs(all);
      box.classList.toggle("maximized", mine.maximized);
    });
    return box;
  }

  function stat(label, value, subText, kind, nodeOverride) {
    var s = Arena.el("div", { "class": "stat" + (kind ? " " + kind : "") }, [
      Arena.el("div", { "class": "label" }, [document.createTextNode(label)]),
      nodeOverride || Arena.el("div", { "class": "value" }, [document.createTextNode(value === null || value === undefined ? "—" : String(value))]),
      subText ? Arena.el("div", { "class": "sub" }, [document.createTextNode(subText)]) : null
    ]);
    return s;
  }

  function bindNode(fn) {
    var holder = Arena.el("div", { "class": "value", "data-live-node": "1" });
    holder.appendChild(fn());
    holder._render = function () { holder.innerHTML = ""; holder.appendChild(fn()); };
    // register for live refresh
    registeredNodes.push(holder);
    return holder;
  }
  var registeredNodes = [];
  var _origRefresh = refreshLive;
  refreshLive = function () {
    _origRefresh();
    registeredNodes.forEach(function (n) { if (n._render && document.body.contains(n)) n._render(); });
  };

  function bindMoney(path, currencyPath) {
    return bindNode(function () {
      var v = lookup(state, path);
      var cur = lookup(state, currencyPath) || "";
      var node = Arena.el("span", { "class": Arena.pnlClass(v) }, [document.createTextNode(Arena.fmtMoney(v, cur))]);
      return node;
    });
  }
  function bindPct(path) {
    return bindNode(function () {
      var v = lookup(state, path);
      return Arena.el("span", {}, [document.createTextNode(Arena.fmtPct(v))]);
    });
  }
  function bindCount(path) {
    return bindNode(function () {
      var v = lookup(state, path);
      return Arena.el("span", {}, [document.createTextNode(v === null || v === undefined ? "—" : String(v))]);
    });
  }
  function statNode(label, node) {
    return Arena.el("div", { "class": "stat" }, [
      Arena.el("div", { "class": "label" }, [document.createTextNode(label)]),
      node
    ]);
  }
  function bootSetting(name) {
    var s = state.boot && state.boot.settings ? state.boot.settings[name] : null;
    return s === null || s === undefined ? "—" : s;
  }

  function marketPanel() {
    var box = document.createElement("div");
    var kv = Arena.el("dl", { "class": "kv" });
    var rows = [
      ["Source", state.boot.data_source === "upstox" ? "Upstox live market data" : "Bundled replay dataset"],
      ["Instrument", (state.paper && state.paper.symbols && state.paper.symbols.join(", ")) || state.boot.settings.default_symbol || "—"],
      ["Timeframe", (state.paper && state.paper.parameters && "(engine)") || state.boot.settings.default_timeframe || "—"],
      ["Feed", null],
      ["Last update", null],
      ["Bars processed", null]
    ];
    rows.forEach(function (r) {
      kv.appendChild(Arena.el("dt", {}, [document.createTextNode(r[0])]));
      if (r[1] === null) {
        var dd = Arena.el("dd");
        if (r[0] === "Feed") dd.setAttribute("data-live-bind", "paper.connection");
        else if (r[0] === "Last update") {
          dd.setAttribute("data-live-ago", "paper.last_message");
          dd.textContent = "—";
        } else dd.setAttribute("data-live-bind", "paper.processed_bars");
        kv.appendChild(dd);
      } else {
        kv.appendChild(Arena.el("dd", {}, [document.createTextNode(String(r[1]))]));
      }
    });
    box.appendChild(kv);
    box.appendChild(Arena.el("p", { "class": "hint faint", style: "font-size:11.5px;margin-top:10px" },
      [document.createTextNode("A stopped engine shows DISCONNECTED by design — missing data is never displayed as live.")]));
    return box;
  }

  function strategyPanel() {
    var box = document.createElement("div");
    var kv = Arena.el("dl", { "class": "kv" });
    kv.appendChild(Arena.el("dt", {}, [document.createTextNode("Active strategy")]));
    var strat = Arena.el("dd"); strat.setAttribute("data-live-bind", "paper.strategy"); kv.appendChild(strat);
    kv.appendChild(Arena.el("dt", {}, [document.createTextNode("Lifecycle state")]));
    var st = Arena.el("dd"); st.setAttribute("data-live-bind", "paper.state"); kv.appendChild(st);
    kv.appendChild(Arena.el("dt", {}, [document.createTextNode("Last signal")]));
    var sig = Arena.el("dd");
    sig.setAttribute("data-live-node-signal", "1");
    kv.appendChild(sig);
    box.appendChild(kv);
    box._renderSignal = function () {
      sig.innerHTML = "";
      var signals = state.paper && state.paper.signals ? state.paper.signals : {};
      var keys = Object.keys(signals);
      if (!keys.length) { sig.textContent = "—"; return; }
      keys.forEach(function (k) {
        sig.appendChild(Arena.el("div", {}, [
          Arena.el("span", { "class": "mono faint" }, [document.createTextNode(k + ": ")]),
          Arena.stateBadge(signals[k].signal || "HOLD")
        ]));
      });
    };
    box._renderSignal();
    var oldRefresh = refreshLive;
    refreshLive = function () { oldRefresh(); if (document.body.contains(sig)) box._renderSignal(); };
    return box;
  }

  function executionPanel() {
    var box = document.createElement("div");
    var openOrders = Arena.el("div", { id: "cc-open-orders" });
    box.appendChild(openOrders);
    box.appendChild(Arena.el("div", { "class": "btn-row", style: "margin-top:10px" }, [
      Arena.el("a", { "class": "btn ghost sm", href: "#/execution", id: "cc-view-trades", onclick: function () { show("execution"); } }, [document.createTextNode("View all orders & trades →")])
    ]));
    renderOpenOrders(openOrders, 5);
    var oldRefresh = refreshLive;
    refreshLive = function () {
      oldRefresh();
      if (document.body.contains(openOrders)) renderOpenOrders(openOrders, 5);
    };
    return box;
  }

  function renderOpenOrders(host, limit) {
    host.innerHTML = "";
    var active = state.orders.filter(function (o) {
      return ["NEW", "PARTIALLY_FILLED", "SUBMITTED", "OPEN"].indexOf(o.status) !== -1;
    });
    if (!active.length) {
      host.appendChild(Arena.el("div", { "class": "empty" }, [
        Arena.el("span", { "class": "ico" }, [document.createTextNode("⇄")]),
        document.createTextNode(state.paper && state.paper.state === "RUNNING"
          ? "No open orders right now — new ones appear here instantly."
          : "Start the paper engine to see orders flow through.")
      ]));
      return;
    }
    var tbl = Arena.el("table", { "class": "table" }, [
      Arena.el("thead", {}, [Arena.el("tr", {}, [
        Arena.el("th", {}, [document.createTextNode("Side")]), Arena.el("th", {}, [document.createTextNode("Qty")]),
        Arena.el("th", {}, [document.createTextNode("Status")])
      ])])
    ]);
    var body = Arena.el("tbody");
    active.slice(0, limit).forEach(function (o) {
      body.appendChild(Arena.el("tr", {}, [
        Arena.el("td", {}, [Arena.stateBadge(o.side)]),
        Arena.el("td", { "class": "num" }, [document.createTextNode(String(o.quantity))]),
        Arena.el("td", {}, [Arena.stateBadge(o.status)])
      ]));
    });
    tbl.appendChild(body);
    host.appendChild(tbl);
  }

  function learningPanel() {
    var box = document.createElement("div");
    box.appendChild(Arena.el("div", { "class": "muted", id: "cc-learning" }, [document.createTextNode("Loading learning state…")]));
    Arena.get("/api/v2/learning").then(function (d) {
      var host = document.getElementById("cc-learning");
      if (!host) return;
      host.innerHTML = "";
      var versions = d.model_versions || [];
      var events = d.learning_events || [];
      var latest = versions.length ? versions[0] : null;
      var kv = Arena.el("dl", { "class": "kv" });
      [
        ["Model versions recorded", String(versions.length)],
        ["Latest model", latest ? String(latest.version || latest.name || latest.id || "—") : "none yet"],
        ["Learning events", String(events.length)],
        ["Shadow trades", String((d.shadow_trades || []).length)]
      ].forEach(function (r) {
        kv.appendChild(Arena.el("dt", {}, [document.createTextNode(r[0])]));
        kv.appendChild(Arena.el("dd", {}, [document.createTextNode(r[1])]));
      });
      host.appendChild(kv);
      host.appendChild(Arena.el("p", { "class": "hint faint", style: "font-size:11.5px;margin-top:8px" },
        [document.createTextNode("Paper sessions, backtests and research runs feed this history — the training record for future live operation.")]));
      host.appendChild(Arena.el("div", { "class": "btn-row", style: "margin-top:10px" }, [
        Arena.el("button", { "class": "btn ghost sm", id: "cc-open-lab", onclick: function () { show("research"); } }, [document.createTextNode("Open Research Lab →")])
      ]));
    }).catch(function (e) {
      var host = document.getElementById("cc-learning");
      if (host) { host.innerHTML = ""; host.appendChild(Arena.el("span", { "class": "faint" }, [document.createTextNode(e.message)])); }
    });
    return box;
  }

  function alertsPanel(compact) {
    var box = document.createElement("div");
    var host = Arena.el("div", { id: compact ? "cc-alerts" : "alerts-list" });
    if (!compact) {
      // Category filter (spec §56): All + every category the backend names.
      var cats = ["all", "live_ops", "risk", "kronos", "research", "market_event", "system"];
      var filterRow = Arena.el("div", { "class": "btn-row", style: "flex-wrap:wrap;margin-bottom:8px" });
      var current = { cat: "all" };
      cats.forEach(function (c) {
        filterRow.appendChild(Arena.el("button", { "class": "btn ghost sm", id: "alert-cat-" + c }, [
          document.createTextNode(c === "all" ? "All" : c.replace("_", " "))
        ]));
        filterRow.lastChild.addEventListener("click", function () {
          current.cat = c;
          loadAlerts(host, 200, c === "all" ? null : c);
        });
      });
      box.appendChild(filterRow);
    }
    box.appendChild(host);
    loadAlerts(host, compact ? 6 : 200);
    return box;
  }

  function loadAlerts(host, limit, category) {
    host.innerHTML = "";
    host.appendChild(Arena.el("div", { "class": "muted" }, [document.createTextNode("Loading alerts…")]));
    var url = "/api/v2/alerts?limit=" + limit + (category ? "&category=" + encodeURIComponent(category) : "");
    Arena.get(url).then(function (d) {
      host.innerHTML = "";
      var alerts = d.alerts || [];
      if (!alerts.length) {
        host.appendChild(Arena.el("div", { "class": "empty" }, [
          Arena.el("span", { "class": "ico" }, [document.createTextNode("▲")]),
          document.createTextNode("No alerts recorded. Risk, watchdog and system events will appear here.")
        ]));
        return;
      }
      if (d.note) {
        host.appendChild(Arena.el("div", { "class": "banner warn" }, [document.createTextNode(d.note)]));
      }
      var tbl = Arena.el("table", { "class": "table" });
      tbl.appendChild(Arena.el("thead", {}, [Arena.el("tr", {}, [
        Arena.el("th", {}, [document.createTextNode("Severity")]), Arena.el("th", {}, [document.createTextNode("Category")]),
        Arena.el("th", {}, [document.createTextNode("Kind")]),
        Arena.el("th", {}, [document.createTextNode("Message")]), Arena.el("th", {}, [document.createTextNode("When")]),
        Arena.el("th", {}, [document.createTextNode("")])
      ])]));
      var body = Arena.el("tbody");
      alerts.forEach(function (a) {
        var sev = Arena.stateBadge(a.severity === "CRITICAL" ? "ERROR" : (a.severity === "WARNING" ? "STALE" : "CONNECTED"));
        sev.querySelector(".dot").nextSibling.textContent = a.severity;
        var ack = a.acknowledged
          ? Arena.el("span", { "class": "badge neutral" }, [document.createTextNode("acked")])
          : Arena.el("button", { "class": "btn ghost sm", onclick: function (ev) {
              Arena.post("/api/v2/alerts/ack", { ids: [a.id], by: "ui" }).then(function () {
                Arena.toast("ok", "Alert acknowledged.");
                loadAlerts(host, limit, category);
              }).catch(function (e) { Arena.toast("err", e.message); });
            } }, [document.createTextNode("Acknowledge")]);
        body.appendChild(Arena.el("tr", {}, [
          Arena.el("td", {}, [sev]),
          Arena.el("td", {}, [Arena.el("span", { "class": "badge neutral" }, [document.createTextNode(a.category || "system")])]),
          Arena.el("td", { "class": "mono" }, [document.createTextNode(a.kind || "—")]),
          Arena.el("td", {}, [document.createTextNode(a.message || a.detail || "")]),
          Arena.el("td", { "class": "mono faint" }, [document.createTextNode(Arena.timeAgo(a.timestamp || a.ts))]),
          Arena.el("td", {}, [ack])
        ]));
      });
      tbl.appendChild(body);
      host.appendChild(tbl);
    }).catch(function (e) {
      host.innerHTML = "";
      host.appendChild(Arena.el("div", { "class": "banner err" }, [document.createTextNode(e.message)]));
    });
  }

  function marketEventsPanel() {
    var box = document.createElement("div");
    box.appendChild(Arena.el("p", { "class": "hint faint", style: "font-size:11.5px;margin-top:0" },
      [document.createTextNode("Observational context only — market events never create orders by themselves.")]));
    var host = Arena.el("div", { id: "cc-events" });
    box.appendChild(host);
    loadMarketEvents(host, 6, false);
    return box;
  }

  function loadMarketEvents(host, limit, withIngest) {
    host.innerHTML = "";
    Arena.get("/api/market-events?limit=" + limit).then(function (d) {
      host.innerHTML = "";
      var events = d.events || d || [];
      if (!Array.isArray(events)) events = [];
      if (!events.length) {
        host.appendChild(Arena.el("div", { "class": "empty" }, [document.createTextNode("No market events recorded yet.")]));
        return;
      }
      events.forEach(function (ev) {
        host.appendChild(Arena.el("div", { "class": "check-item", style: "margin-bottom:8px" }, [
          Arena.el("span", { "class": "name" }, [document.createTextNode(ev.headline || ev.title || "event")]),
          Arena.el("span", { "class": "msg" }, [document.createTextNode(ev.summary || "")]),
          Arena.stateBadge(ev.urgency || "INFO")
        ]));
      });
    }).catch(function (e) {
      host.innerHTML = "";
      host.appendChild(Arena.el("span", { "class": "faint" }, [document.createTextNode(e.message)]));
    });
  }

  // ========================================================= RESEARCH LAB
  function viewResearch(root) {
    root.appendChild(Arena.el("h1", { "class": "page-title" }, [document.createTextNode("Research Lab")]));
    root.appendChild(Arena.el("p", { "class": "page-sub" }, [document.createTextNode(
      "Backtests, chronological validation, walk-forward and robustness checks. Every saved run records its exact " +
      "configuration so results stay reproducible. A backtest is never presented as live performance.")]));

    var grid = Arena.el("div", { "class": "grid cols-2" });

    // ---- backtest form
    var formBody = document.createElement("div");
    var strategySel = Arena.el("select", { id: "bt-strategy" });
    var datasetSel = Arena.el("select", { id: "bt-dataset" });
    var paramBox = Arena.el("div", { id: "bt-params" });

    formBody.appendChild(field("Strategy", strategySel));
    formBody.appendChild(field("Dataset", datasetSel));
    formBody.appendChild(paramBox);
    var small = Arena.el("div", { "class": "grid cols-2" });
    small.appendChild(field("Initial capital", Arena.el("input", { type: "number", id: "bt-capital", value: "100000" })));
    small.appendChild(field("Timeframe", sel("bt-timeframe", ["1D", "1h"], "1D")));
    formBody.appendChild(small);
    var runBtn = Arena.el("button", { "class": "btn primary", id: "bt-run" }, [document.createTextNode("Run backtest")]);
    var result = Arena.el("div", { id: "bt-result" });
    formBody.appendChild(Arena.el("div", { "class": "btn-row" }, [runBtn]));
    formBody.appendChild(result);
    grid.appendChild(card("New backtest", formBody));

    // ---- validation tools
    var valBody = document.createElement("div");
    valBody.appendChild(Arena.el("p", { "class": "muted" }, [document.createTextNode(
      "Chronological train/validation/test splitting, walk-forward windows with disjoint test folds, and cost/parameter " +
      "sensitivity — computed by the same engine that runs backtests.")]));
    var valBtnRow = Arena.el("div", { "class": "btn-row" });
    [["Chronological split", "split"], ["Walk-forward", "walk_forward"], ["Robustness", "robustness"]].forEach(function (item) {
      valBtnRow.appendChild(Arena.el("button", { "class": "btn ghost", id: "val-" + item[1], onclick: function (ev) {
        var b = ev.currentTarget;
        Arena.setBusy(b, true, "Computing…");
        Arena.post("/api/research/validation", { kind: item[1], dataset: datasetSel.value })
          .then(function (d) {
            Arena.setBusy(b, false);
            showValidationResult(valResult, d);
          }).catch(function (e) { Arena.setBusy(b, false); valResult.innerHTML = ""; bannerInto(valResult, "err", "Validation failed", e.message); });
      } }, [document.createTextNode(item[0])]));
    });
    var valResult = Arena.el("div", { id: "val-result", style: "margin-top:12px" });
    valBody.appendChild(valBtnRow);
    valBody.appendChild(valResult);
    grid.appendChild(card("Validation & robustness (OOS discipline)", valBody));
    root.appendChild(grid);

    // ---- saved runs + experiments + tournaments
    var historyGrid = Arena.el("div", { "class": "grid cols-2 section-gap" });
    var savedBody = Arena.el("div", { id: "bt-saved" });
    historyGrid.appendChild(card("Saved backtests", savedBody));
    var expBody = Arena.el("div", { id: "exp-list" });
    historyGrid.appendChild(card("Experiments & research runs", expBody));
    root.appendChild(historyGrid);

    loadBacktestHistory(savedBody);
    loadExperiments(expBody);

    // ---- model agreement + counterfactual research (spec §98, §99)
    var agreeGrid = Arena.el("div", { "class": "grid cols-2 section-gap" });
    var agreeBody = Arena.el("div", { id: "agreement-center" });
    agreeGrid.appendChild(card("Model agreement / disagreement", agreeBody));
    var cfBody = Arena.el("div", { id: "counterfactual-lab" });
    agreeGrid.appendChild(card("Counterfactual lab (what-if, SIMULATED)", cfBody));
    root.appendChild(agreeGrid);
    renderAgreementCenter(agreeBody);
    renderCounterfactualLab(cfBody);

    var schedGrid = Arena.el("div", { "class": "grid cols-2 section-gap" });
    var schedBody = Arena.el("div", { id: "research-schedule" });
    schedGrid.appendChild(card("Research scheduler (bounded loop)", schedBody));
    root.appendChild(schedGrid);
    renderResearchSchedule(schedBody);

    // fill catalogs
    Arena.get("/strategies").then(function (d) {
      (d.strategies || d || []).forEach(function (s) {
        strategySel.appendChild(Arena.el("option", { value: s.name }, [document.createTextNode(s.name)]));
      });
      state.strategies = d.strategies || d || [];
      renderParams();
    }).catch(function () { });
    Arena.get("/market/datasets").then(function (d) {
      (d.datasets || []).forEach(function (name) {
        datasetSel.appendChild(Arena.el("option", { value: name }, [document.createTextNode(name)]));
      });
      datasetSel.value = "sample.csv";
    }).catch(function () { });
    strategySel.addEventListener("change", renderParams);

    function renderParams() {
      paramBox.innerHTML = "";
      var entry = state.strategies.find(function (s) { return s.name === strategySel.value; });
      if (!entry) return;
      var gridP = Arena.el("div", { "class": "grid cols-2" });
      Object.keys(entry.default_parameters || {}).forEach(function (key) {
        gridP.appendChild(field(key, Arena.el("input", {
          type: "number", id: "bt-param-" + key, value: String(entry.default_parameters[key])
        })));
      });
      paramBox.appendChild(gridP);
    }

    runBtn.addEventListener("click", function () {
      var parameters = {};
      paramBox.querySelectorAll("input").forEach(function (input) {
        var key = input.id.replace("bt-param-", "");
        if (input.value !== "") parameters[key] = Number(input.value);
      });
      var body = {
        strategy: strategySel.value,
        parameters: parameters,
        dataset: datasetSel.value || "sample.csv",
        symbol: "DEMO",
        timeframe: document.getElementById("bt-timeframe").value,
        initial_capital: Number(document.getElementById("bt-capital").value || 100000)
      };
      Arena.setBusy(runBtn, true, "Running backtest…");
      result.innerHTML = "";
      Arena.post("/backtests/run", body).then(function (d) {
        Arena.setBusy(runBtn, false);
        renderBacktestResult(result, d);
        loadBacktestHistory(savedBody);
      }).catch(function (e) {
        Arena.setBusy(runBtn, false);
        bannerInto(result, "err", "Backtest failed", e.message);
      });
    });

    function field(label, input) {
      return Arena.el("div", { "class": "field" }, [
        Arena.el("label", {}, [document.createTextNode(label)]), input
      ]);
    }
    function sel(id, options, current) {
      var s = Arena.el("select", { id: id });
      options.forEach(function (o) { s.appendChild(Arena.el("option", { value: o }, [document.createTextNode(o)])); });
      s.value = current;
      return s;
    }
  }

  function bannerInto(host, kind, title, message, detail) {
    var node = Arena.el("div", { "class": "banner " + kind }, [
      Arena.el("b", {}, [document.createTextNode(title)]),
      document.createTextNode(message || "")
    ]);
    if (detail) node.appendChild(Arena.el("div", { "class": "detail" }, [document.createTextNode(detail)]));
    host.appendChild(node);
  }

  function renderBacktestResult(host, d) {
    host.innerHTML = "";
    host.appendChild(Arena.el("div", { "class": "banner ok" }, [
      Arena.el("b", {}, [document.createTextNode("Backtest completed — " + (d.id || "").slice(0, 8))]),
      document.createTextNode("Configuration, parameters and metrics were saved as an experiment record.")
    ]));
    var m = d.metrics || {};
    var names = [
      ["Total return", m.total_return, "pct"], ["Sharpe", m.sharpe, "num"],
      ["Max drawdown", m.max_drawdown, "pct"], ["Trades", m.number_of_trades, "int"],
      ["Win rate", m.win_rate, "pct"], ["Profit factor", m.profit_factor, "num"],
      ["Fees", m.total_fees, "num"]
    ];
    var grid = Arena.el("div", { "class": "grid cols-4", style: "margin-top:10px" });
    names.forEach(function (row) {
      var v = row[2] === "pct" ? Arena.fmtPct(row[1]) : (row[2] === "int" ? (row[1] === null || row[1] === undefined ? "—" : String(row[1])) : Arena.fmtNum(row[1]));
      grid.appendChild(stat(row[0], v, null, Arena.pnlClass(row[2] === "pct" ? row[1] : 0)));
    });
    host.appendChild(grid);
    if (d.warnings && d.warnings.length) {
      var warn = Arena.el("div", { "class": "banner warn", style: "margin-top:10px" }, [
        Arena.el("b", {}, [document.createTextNode("Validation warnings")])
      ]);
      d.warnings.forEach(function (w) { warn.appendChild(Arena.el("div", {}, [document.createTextNode("• " + w)])); });
      host.appendChild(warn);
    }
    // equity curve from the stored run
    Arena.get("/backtests/" + encodeURIComponent(d.id)).then(function (full) {
      var curve = full.equity_curve || [];
      if (!curve.length) return;
      var chartHost = Arena.el("div", { style: "margin-top:12px" });
      host.appendChild(chartHost);
      Arena.lineChart(chartHost, curve.map(function (p, i) { return { x: p.timestamp || ("bar " + i), y: p.equity }; }), {
        label: "backtest equity", color: "#60a5fa", height: 200,
        format: function (v) { return Arena.fmtNum(v, 0); }
      });
    }).catch(function () { });
  }

  function showValidationResult(host, d) {
    host.innerHTML = "";
    if (d.kind === "split") {
      var s = d.sizes || {};
      bannerInto(host, "info", "Chronological split (no shuffling)",
        "train " + s.train + " bars · validation " + s.validation + " bars · test " + s.test +
        " bars. Parameters must never be tuned on the test split.");
      return;
    }
    if (d.kind === "walk_forward") {
      var wf = Arena.el("div", { "class": "banner ok" }, [
        Arena.el("b", {}, [document.createTextNode("Walk-forward: " + (d.folds || 0) + " disjoint test folds")]),
        document.createTextNode("Aggregate OOS return " + Arena.fmtPct(d.aggregate && d.aggregate.total_return) +
          " · Sharpe " + Arena.fmtNum(d.aggregate && d.aggregate.sharpe) +
          " · trades " + (d.aggregate && d.aggregate.number_of_trades))
      ]);
      host.appendChild(wf);
      if (d.warnings && d.warnings.length) {
        var warn = Arena.el("div", { "class": "banner warn" }, [Arena.el("b", {}, [document.createTextNode("Warnings")])]);
        d.warnings.forEach(function (w) { warn.appendChild(Arena.el("div", {}, [document.createTextNode("• " + w)])); });
        host.appendChild(warn);
      }
      return;
    }
    // robustness
    var rb = Arena.el("div", { "class": "banner " + (d.sensitive ? "warn" : "ok") }, [
      Arena.el("b", {}, [document.createTextNode(d.sensitive ? "Robustness: SENSITIVE to perturbations" : "Robustness: stable under perturbations")]),
      document.createTextNode("Baseline return " + Arena.fmtPct(d.baseline && d.baseline.total_return) +
        " across " + (d.trials || 0) + " perturbed trials (± parameters, ×2 costs).")
    ]);
    host.appendChild(rb);
    if (d.warnings && d.warnings.length) {
      var warn2 = Arena.el("div", { "class": "banner warn" }, [Arena.el("b", {}, [document.createTextNode("Warnings")])]);
      d.warnings.forEach(function (w) { warn2.appendChild(Arena.el("div", {}, [document.createTextNode("• " + w)])); });
      host.appendChild(warn2);
    }
  }

  function loadBacktestHistory(host) {
    host.innerHTML = "";
    Arena.get("/backtests?limit=30").then(function (d) {
      host.innerHTML = "";
      var rows = d.backtests || [];
      if (!rows.length) {
        host.appendChild(Arena.el("div", { "class": "empty" }, [document.createTextNode("No saved backtests yet — run one on the left.")]));
        return;
      }
      var tbl = Arena.el("table", { "class": "table" });
      tbl.appendChild(Arena.el("thead", {}, [Arena.el("tr", {}, [
        Arena.el("th", {}, [document.createTextNode("Strategy")]), Arena.el("th", {}, [document.createTextNode("Dataset")]),
        Arena.el("th", {}, [document.createTextNode("Return")]), Arena.el("th", {}, [document.createTextNode("Sharpe")]),
        Arena.el("th", {}, [document.createTextNode("Trades")]), Arena.el("th", {}, [document.createTextNode("When")])
      ])]));
      var body = Arena.el("tbody");
      rows.forEach(function (r) {
        body.appendChild(Arena.el("tr", {}, [
          Arena.el("td", { "class": "mono" }, [document.createTextNode(r.strategy)]),
          Arena.el("td", { "class": "mono" }, [document.createTextNode(r.symbol + " " + r.timeframe)]),
          Arena.el("td", { "class": "num " + Arena.pnlClass(r.metrics && r.metrics.total_return) }, [document.createTextNode(Arena.fmtPct(r.metrics && r.metrics.total_return))]),
          Arena.el("td", { "class": "num" }, [document.createTextNode(Arena.fmtNum(r.metrics && r.metrics.sharpe))]),
          Arena.el("td", { "class": "num" }, [document.createTextNode(String(r.metrics && r.metrics.number_of_trades))]),
          Arena.el("td", { "class": "mono faint" }, [document.createTextNode(Arena.timeAgo(r.created_at))])
        ]));
      });
      tbl.appendChild(body);
      host.appendChild(Arena.el("div", { "class": "table-wrap" }, [tbl]));
    }).catch(function (e) { host.innerHTML = ""; host.appendChild(Arena.el("span", { "class": "faint" }, [document.createTextNode(e.message)])); });
  }

  function loadExperiments(host) {
    host.innerHTML = "";
    Promise.allSettled([Arena.get("/api/v2/experiments"), Arena.get("/tournaments")]).then(function (results) {
      host.innerHTML = "";
      var experiments = results[0].status === "fulfilled" ? (results[0].value.experiments || []) : [];
      var runs = results[1].status === "fulfilled" ? (results[1].value.research_runs || []) : [];
      if (!experiments.length && !runs.length) {
        host.appendChild(Arena.el("div", { "class": "empty" }, [
          document.createTextNode("No experiments yet. Backtests, paper sessions and research runs register here with their exact configuration.")
        ]));
        return;
      }
      experiments.slice(0, 12).forEach(function (x) {
        host.appendChild(Arena.el("div", { "class": "check-item", style: "margin-bottom:8px" }, [
          Arena.el("span", { "class": "name" }, [document.createTextNode(x.name || x.id || "experiment")]),
          Arena.el("span", { "class": "msg mono" }, [document.createTextNode(JSON.stringify(x.parameters || x.metrics || {}).slice(0, 80))]),
          Arena.stateBadge(x.status || "COMPLETED")
        ]));
      });
      runs.slice(0, 8).forEach(function (r) {
        host.appendChild(Arena.el("div", { "class": "check-item", style: "margin-bottom:8px" }, [
          Arena.el("span", { "class": "name" }, [document.createTextNode("research run")]),
          Arena.el("span", { "class": "msg mono" }, [document.createTextNode((r.created_at || "") + " · " + (r.status || ""))]),
          Arena.stateBadge("COMPLETED")
        ]));
      });
    });
  }

  // ========================================================= EXECUTION
  function viewExecution(root) {
    root.appendChild(Arena.el("h1", { "class": "page-title" }, [document.createTextNode("Orders & Trades")]));
    root.appendChild(Arena.el("p", { "class": "page-sub" }, [document.createTextNode(
      "Every simulated order, fill and round-trip trade — persisted in the paper database and restored on restart.")]));

    var positionsCard = card("Open positions", positionsTable());
    var ordersCard = card("Orders", ordersTable());
    var tradesCard = card("Trades (round trips)", tradesTable());
    root.appendChild(Arena.el("div", { "class": "grid cols-2" }, [positionsCard, ordersCard]));
    root.appendChild(Arena.el("div", { "class": "section-gap" }, [tradesCard]));
  }

  function positionsTable() {
    var host = Arena.el("div", { id: "ex-positions" });
    renderPositions(host);
    var oldRefresh = refreshLive;
    refreshLive = function () { oldRefresh(); if (document.body.contains(host)) renderPositions(host); };
    return host;
  }
  function renderPositions(host) {
    host.innerHTML = "";
    if (!state.positions.length) {
      host.appendChild(Arena.el("div", { "class": "empty" }, [document.createTextNode("No open positions.")]));
      return;
    }
    var tbl = tableShell(["Symbol", "Qty", "Avg price"]);
    var body = Arena.el("tbody");
    state.positions.forEach(function (p) {
      body.appendChild(Arena.el("tr", {}, [
        Arena.el("td", { "class": "mono" }, [document.createTextNode(p.symbol || "—")]),
        Arena.el("td", { "class": "num" }, [document.createTextNode(String(p.quantity))]),
        Arena.el("td", { "class": "num" }, [document.createTextNode(Arena.fmtNum(p.avg_price))])
      ]));
    });
    tbl.appendChild(body);
    host.appendChild(tbl);
  }

  function ordersTable() {
    var host = Arena.el("div", { id: "ex-orders" });
    renderOrders(host);
    var oldRefresh = refreshLive;
    refreshLive = function () { oldRefresh(); if (document.body.contains(host)) renderOrders(host); };
    return host;
  }
  function renderOrders(host) {
    host.innerHTML = "";
    if (!state.orders.length) {
      host.appendChild(Arena.el("div", { "class": "empty" }, [document.createTextNode("No orders yet.")]));
      return;
    }
    var tbl = tableShell(["Time", "Side", "Qty", "Price", "Status", "Strategy"]);
    var body = Arena.el("tbody");
    state.orders.slice().reverse().slice(0, 100).forEach(function (o) {
      body.appendChild(Arena.el("tr", {}, [
        Arena.el("td", { "class": "mono faint" }, [document.createTextNode(o.timestamp || "")]),
        Arena.el("td", {}, [Arena.stateBadge(o.side)]),
        Arena.el("td", { "class": "num" }, [document.createTextNode(String(o.quantity))]),
        Arena.el("td", { "class": "num" }, [document.createTextNode(Arena.fmtNum(o.price))]),
        Arena.el("td", {}, [Arena.stateBadge(o.status)]),
        Arena.el("td", { "class": "mono" }, [document.createTextNode(o.strategy || "")])
      ]));
    });
    tbl.appendChild(body);
    host.appendChild(Arena.el("div", { "class": "table-wrap" }, [tbl]));
  }

  function tradesTable() {
    var host = Arena.el("div", { id: "ex-trades" });
    renderTrades(host);
    var oldRefresh = refreshLive;
    refreshLive = function () { oldRefresh(); if (document.body.contains(host)) renderTrades(host); };
    return host;
  }
  function renderTrades(host) {
    host.innerHTML = "";
    if (!state.trades.length) {
      host.appendChild(Arena.el("div", { "class": "empty" }, [document.createTextNode("No completed trades yet.")]));
      return;
    }
    var tbl = tableShell(["Symbol", "Qty", "Entry", "Exit", "P&L", "Fees"]);
    var body = Arena.el("tbody");
    state.trades.slice().reverse().slice(0, 100).forEach(function (t) {
      body.appendChild(Arena.el("tr", {}, [
        Arena.el("td", { "class": "mono" }, [document.createTextNode(t.symbol || "—")]),
        Arena.el("td", { "class": "num" }, [document.createTextNode(String(t.quantity))]),
        Arena.el("td", { "class": "num" }, [document.createTextNode(Arena.fmtNum(t.entry_price))]),
        Arena.el("td", { "class": "num" }, [document.createTextNode(Arena.fmtNum(t.exit_price))]),
        Arena.el("td", { "class": "num " + Arena.pnlClass(t.pnl) }, [document.createTextNode(Arena.fmtNum(t.pnl))]),
        Arena.el("td", { "class": "num faint" }, [document.createTextNode(Arena.fmtNum(t.fees))])
      ]));
    });
    tbl.appendChild(body);
    host.appendChild(Arena.el("div", { "class": "table-wrap" }, [tbl]));
  }

  function tableShell(cols) {
    var tr = Arena.el("tr");
    cols.forEach(function (c) { tr.appendChild(Arena.el("th", {}, [document.createTextNode(c)])); });
    return Arena.el("table", { "class": "table" }, [Arena.el("thead", {}, [tr])]);
  }

  // ============================================================ EVENTS
  function viewEvents(root) {
    root.appendChild(Arena.el("h1", { "class": "page-title" }, [document.createTextNode("Market Events")]));
    root.appendChild(Arena.el("p", { "class": "page-sub" }, [document.createTextNode(
      "Recorded market context with impact scoring. Explicitly observational: these events inform research, they never place orders.")]));
    var body = Arena.el("div", { id: "ev-list" });
    root.appendChild(card("Recorded events", body));
    loadMarketEvents(body, 50, true);
  }

  // ============================================================ ALERTS
  function viewAlerts(root) {
    root.appendChild(Arena.el("h1", { "class": "page-title" }, [document.createTextNode("Alerts")]));
    root.appendChild(Arena.el("p", { "class": "page-sub" }, [document.createTextNode(
      "Durable alert center: risk breaches, watchdog escalations and system events. Critical alerts stay visible until acknowledged.")]));
    var body = alertsPanel(false);
    root.appendChild(card("Alert feed", body));
  }

  // =========================================================== SETTINGS
  function viewSettings(root) {
    root.appendChild(Arena.el("h1", { "class": "page-title" }, [document.createTextNode("Settings")]));
    root.appendChild(Arena.el("p", { "class": "page-sub" }, [document.createTextNode(
      "Configuration is stored in your local .env and grouped by purpose. Live-trading switches are deliberately NOT settable here.")]));

    var grid = Arena.el("div", { "class": "grid cols-2" });

    // ---- general
    var general = document.createElement("div");
    general.appendChild(mkField("DEFAULT_SYMBOL", "text", "DEMO", "Must match your data source — bundled replay data uses DEMO."));
    general.appendChild(mkField("DEFAULT_TIMEFRAME", "text", "1D", "Replay/research timeframe."));
    general.appendChild(saveRow(["DEFAULT_SYMBOL", "DEFAULT_TIMEFRAME"], "general-save"));
    grid.appendChild(card("General", general));

    // ---- market data
    var md = document.createElement("div");
    md.appendChild(Arena.el("p", { "class": "muted" }, [document.createTextNode(
      "Used for LIVE-MARKET paper (real data, simulated orders). Tokens are stored locally and masked everywhere.")]));
    md.appendChild(mkField("MARKET_DATA_SYMBOLS", "text", "NSE_EQ|INE002A01018", "Instrument keys, comma-separated."));
    md.appendChild(mkField("MARKET_DATA_ACCESS_TOKEN", "password", "", "Daily Upstox access token."));
    var mdTest = Arena.el("button", { "class": "btn ghost", id: "md-test" }, [document.createTextNode("Test market-data credential")]);
    mdTest.addEventListener("click", function () {
      var token = document.getElementById("set-MARKET_DATA_ACCESS_TOKEN").value.trim();
      var symbols = document.getElementById("set-MARKET_DATA_SYMBOLS").value.trim();
      Arena.setBusy(mdTest, true, "Testing…");
      Arena.post("/api/wizard/validate-market-data", { token: token, symbols: symbols }).then(function (d) {
        Arena.setBusy(mdTest, false);
        Arena.toast("ok", "Market-data credential valid (" + d.symbol + ").");
      }).catch(function (e) { Arena.setBusy(mdTest, false); Arena.toast("err", e.message); });
    });
    md.appendChild(Arena.el("div", { "class": "btn-row" }, [mdTest]));
    md.appendChild(saveRow(["MARKET_DATA_SYMBOLS", "MARKET_DATA_ACCESS_TOKEN"], "md-save"));
    grid.appendChild(card("Market data", md));

    // ---- security
    var sec = document.createElement("div");
    sec.appendChild(Arena.el("p", { "class": "muted" }, [document.createTextNode(
      "The local API token protects Arena's endpoints. The guided setup generated one automatically; you can rotate it here.")]));
    var secBtns = Arena.el("div", { "class": "btn-row" });
    var reveal = Arena.el("button", { "class": "btn ghost", id: "sec-reveal" }, [document.createTextNode("Reveal current token")]);
    reveal.addEventListener("click", function () {
      Arena.post("/api/credentials/reveal", {}).then(function (d) {
        Arena.confirmDialog("Your API token", "Shown once, masked again when you close this dialog. Never share it.", "Close", false, function () { });
        var dlg = document.querySelector(".dialog p");
        if (dlg) {
          var box = Arena.el("div", { "class": "tokenbox", style: "margin-top:10px" }, [document.createTextNode(d.token)]);
          dlg.parentNode.insertBefore(box, dlg.nextSibling);
        }
      }).catch(function (e) { Arena.toast("err", e.message); });
    });
    var regen = Arena.el("button", { "class": "btn warn", id: "sec-regen" }, [document.createTextNode("Regenerate token")]);
    regen.addEventListener("click", function () {
      Arena.confirmDialog("Regenerate the API token?",
        "The old token stops working after you save. This browser session is updated automatically.",
        "Regenerate & save", true, function () {
          Arena.post("/setup/generate-token", {}).then(function (d) {
            return Arena.post("/setup/save", { values: { API_TOKEN: d.token } }).then(function () {
              Arena.setToken(d.token);
              Arena.toast("ok", "Token regenerated and saved. It takes effect after an Arena restart.");
            });
          }).catch(function (e) { Arena.toast("err", e.message); });
        });
    });
    secBtns.appendChild(reveal); secBtns.appendChild(regen);
    sec.appendChild(secBtns);
    grid.appendChild(card("Security", sec));

    // ---- broker / live rail
    var live = document.createElement("div");
    live.appendChild(Arena.el("p", { "class": "muted" }, [document.createTextNode(
      "The complete live-trading rail (Upstox authentication, order placement, risk controls, watchdog, kill switch, audit) " +
      "is packaged in this build and stays fail-closed until you activate it deliberately outside the UI.")]));
    var liveStatus = Arena.el("div", { id: "live-readiness" }, [document.createTextNode("Loading readiness…")]);
    live.appendChild(liveStatus);
    live.appendChild(Arena.el("div", { "class": "btn-row", style: "margin-top:10px" }, [
      Arena.el("button", { "class": "btn ghost", id: "live-refresh" }, [document.createTextNode("Refresh readiness")])
    ]));
    document.addEventListener("click", function h(ev) {
      if (ev.target && ev.target.id === "live-refresh") loadLiveReadiness(liveStatus);
    });
    grid.appendChild(card("Live trading rail (LOCKED)", live));
    loadLiveReadiness(liveStatus);

    // ---- upstox app credentials (for sandbox/live later)
    var up = document.createElement("div");
    up.appendChild(mkField("UPSTOX_CLIENT_ID", "text", "", "App key from the Upstox developer console."));
    up.appendChild(mkField("UPSTOX_CLIENT_SECRET", "password", "", "Stored locally, masked everywhere, never logged."));
    up.appendChild(mkField("UPSTOX_REDIRECT_URI", "text", "", "Must match the URI registered on your Upstox app."));
    up.appendChild(mkField("UPSTOX_SANDBOX_ACCESS_TOKEN", "password", "", "Only used when BROKER=UPSTOX_SANDBOX."));
    up.appendChild(saveRow(["UPSTOX_CLIENT_ID", "UPSTOX_CLIENT_SECRET", "UPSTOX_REDIRECT_URI", "UPSTOX_SANDBOX_ACCESS_TOKEN"], "up-save"));
    grid.appendChild(card("Upstox app credentials", up));

    // ---- storage
    var storage = document.createElement("div");
    var kv = Arena.el("dl", { "class": "kv" }, []);
    [
      ["Arena home", state.boot.arena_home],
      ["Config file", state.boot.env_file],
      ["Database", state.boot.database],
      ["First run", state.boot.first_run ? "yes" : "no"]
    ].forEach(function (r) {
      kv.appendChild(Arena.el("dt", {}, [document.createTextNode(r[0])]));
      kv.appendChild(Arena.el("dd", {}, [document.createTextNode(String(r[1] || "—"))]));
    });
    storage.appendChild(kv);
    grid.appendChild(card("Storage & database", storage));

    root.appendChild(grid);

    function mkField(key, type, placeholder, hint) {
      return Arena.el("div", { "class": "field" }, [
        Arena.el("label", { "for": "set-" + key }, [document.createTextNode(key)]),
        Arena.el("input", { type: type, id: "set-" + key, placeholder: placeholder, autocomplete: "off" }),
        hint ? Arena.el("div", { "class": "hint" }, [document.createTextNode(hint)]) : null
      ]);
    }
    function saveRow(keys, btnId) {
      var btn = Arena.el("button", { "class": "btn primary", id: btnId }, [document.createTextNode("Save")]);
      btn.addEventListener("click", function () {
        var values = {};
        keys.forEach(function (k) {
          var input = document.getElementById("set-" + k);
          if (input && input.value.trim()) values[k] = input.value.trim();
        });
        if (!Object.keys(values).length) { Arena.toast("warn", "Nothing to save — all fields are blank."); return; }
        Arena.setBusy(btn, true, "Saving…");
        Arena.post("/setup/save", { values: values }).then(function (d) {
          Arena.setBusy(btn, false);
          Arena.toast("ok", "Saved to " + d.path + ". Restart Arena to apply.");
        }).catch(function (e) { Arena.setBusy(btn, false); Arena.toast("err", e.message); });
      });
      return Arena.el("div", { "class": "btn-row", style: "margin-top:6px" }, [btn]);
    }
  }

  function loadLiveReadiness(host) {
    host.innerHTML = "";
    host.appendChild(document.createTextNode("Loading readiness…"));
    Arena.get("/api/live/readiness-center").then(function (d) {
      host.innerHTML = "";
      var verdict = d.verdict || "BLOCKED";
      var enabled = verdict === "READY" || verdict === "READY_WITH_WARNINGS";
      host.appendChild(Arena.el("div", { "class": "banner " + (enabled ? "err" : "info") }, [
        Arena.el("b", {}, [document.createTextNode(enabled
          ? "LIVE ROUTING IS ENABLED in this configuration"
          : "Live trading is LOCKED — exactly as intended for research.")]),
        document.createTextNode(enabled
          ? "Real money can move. Verify every guard below before doing anything."
          : "Activation requires the documented multi-switch opt-in and an explicit, confirmed activation; nothing in this UI can flip those switches by itself.")
      ]));
      var list = Arena.el("div", { "class": "checklist", style: "margin-top:10px" });
      (d.checks || []).forEach(function (c) {
        list.appendChild(Arena.el("div", { "class": "check-item" }, [
          Arena.el("span", { "class": "name" }, [document.createTextNode(c.name)]),
          Arena.el("span", { "class": "msg" }, [document.createTextNode(c.detail)]),
          Arena.stateBadge(c.ok ? "READY" : (c.severity === "ERROR" ? "BLOCKED" : "STALE"))
        ]));
      });
      host.appendChild(list);
      host.appendChild(Arena.el("p", { "class": "hint faint", style: "font-size:11.5px;margin-top:10px" }, [
        document.createTextNode("Full procedure: LIVE_TRADING_ENABLEMENT.md. Upstox live order traffic must come from your registered static IP; tokens are daily and refresh via the packaged upstox-login flow.")
      ]));
    }).catch(function (e) {
      host.innerHTML = "";
      host.appendChild(Arena.el("span", { "class": "faint" }, [document.createTextNode(e.message)]));
    });
  }

  // ============================================================= SYSTEM
  function viewSystem(root) {
    root.appendChild(Arena.el("h1", { "class": "page-title" }, [document.createTextNode("System")]));
    root.appendChild(Arena.el("p", { "class": "page-sub" }, [document.createTextNode(
      "Engine state, database, session lock and diagnostics. This is the developer-facing corner of Arena.")]));

    var grid = Arena.el("div", { "class": "grid cols-2" });
    var infoHost = Arena.el("div", { id: "sys-info" }, [document.createTextNode("Loading…")]);
    grid.appendChild(card("Runtime", infoHost));
    var instanceHost = Arena.el("div", { id: "sys-instance" }, [document.createTextNode("Loading…")]);
    grid.appendChild(card("Session & lock", instanceHost));
    root.appendChild(grid);

    var logsCard = Arena.el("div", { "class": "section-gap" });
    var logsBody = Arena.el("div", { id: "sys-logs" }, [document.createTextNode("Loading…")]);
    logsCard.appendChild(card("Recent activity log", logsBody));
    root.appendChild(logsCard);

    Arena.get("/api/system/info").then(function (d) {
      infoHost.innerHTML = "";
      var kv = Arena.el("dl", { "class": "kv" });
      [
        ["Version", d.version], ["Build", d.build_id], ["Commit", d.commit || "source"],
        ["Built at", d.built_at || "—"], ["Mode", d.mode_display],
        ["Uptime", d.uptime_seconds !== null ? Math.floor(d.uptime_seconds) + "s" : "—"],
        ["Database", d.database], ["Paper writer lock", d.writer_lock],
        ["Engine state", null]
      ].forEach(function (row) {
        kv.appendChild(Arena.el("dt", {}, [document.createTextNode(row[0])]));
        if (row[1] === null) {
          kv.appendChild(Arena.el("dd", {}, [Arena.stateBadge(state.paper ? state.paper.state : "UNKNOWN")]));
        } else {
          kv.appendChild(Arena.el("dd", {}, [document.createTextNode(String(row[1]))]));
        }
      });
      infoHost.appendChild(kv);
    }).catch(function (e) { infoHost.innerHTML = ""; infoHost.appendChild(Arena.el("span", { "class": "faint" }, [document.createTextNode(e.message)])); });

    Arena.get("/api/instance").then(function (d) {
      instanceHost.innerHTML = "";
      var kv = Arena.el("dl", { "class": "kv" });
      [
        ["Session owner pid", d.pid], ["This process", d.this_pid],
        ["URL", d.url], ["Started", d.started ? Arena.fmtTime(d.started) : "—"],
        ["Last heartbeat", d.last_seen ? Arena.timeAgo(d.last_seen) : "—"],
        ["State", d.lock_held_by_this_process ? "this process owns the session lock"
          : (d.stale ? "metadata from an ended session (safe)" : "see running session")]
      ].forEach(function (row) {
        kv.appendChild(Arena.el("dt", {}, [document.createTextNode(row[0])]));
        kv.appendChild(Arena.el("dd", {}, [document.createTextNode(String(row[1]))]));
      });
      instanceHost.appendChild(kv);
      instanceHost.appendChild(Arena.el("p", { "class": "hint faint", style: "font-size:11.5px;margin-top:8px" }, [
        document.createTextNode("A second launch of Arena opens this session instead of starting a conflicting server.")
      ]));
    }).catch(function (e) { instanceHost.innerHTML = ""; instanceHost.appendChild(Arena.el("span", { "class": "faint" }, [document.createTextNode(e.message)])); });

    Arena.get("/api/v2/logs").then(function (d) {
      logsBody.innerHTML = "";
      var events = d.raw_events || d.events || d.logs || [];
      if (!events.length) {
        logsBody.appendChild(Arena.el("div", { "class": "empty" }, [document.createTextNode("No recorded events yet.")]));
        return;
      }
      var pre = Arena.el("div", { "class": "mono", style: "font-size:11.5px;color:#b6c0d4;white-space:pre-wrap;max-height:360px;overflow:auto" });
      events.forEach(function (ev) {
        pre.appendChild(document.createTextNode(
          (ev.timestamp || ev.ts || "") + "  " + (ev.kind || ev.level || "") + "  " +
          JSON.stringify(ev.payload || ev.message || "") + "\n"));
      });
      logsBody.appendChild(pre);
    }).catch(function (e) { logsBody.innerHTML = ""; logsBody.appendChild(Arena.el("span", { "class": "faint" }, [document.createTextNode(e.message)])); });
  }

/* =====================================================================
 * AXIOM INTELLIGENCE LAYER — realtime stream, chips, palette, pages
 * ===================================================================== */

  var stream = { lastSeq: 0, connected: false, retryDelay: 2000 };

  // High-frequency stream updates are coalesced per target (spec §64): the
  // first request in a 120 ms window schedules one refresh; bursts collapse
  // into it instead of re-rendering unchanged objects per event.
  var _coalescePending = {};
  function coalescedRefresh(key, fn) {
    if (_coalescePending[key]) return;
    _coalescePending[key] = true;
    setTimeout(function () {
      _coalescePending[key] = false;
      try { fn(); } catch (e) { /* a failed paint must not break the stream */ }
    }, 120);
  }

  // ------------------------------------------------------------- stream ----
  function connectStream() {
    var url = "/api/stream?last_seq=" + stream.lastSeq;
    fetch(url, { headers: Arena.getToken() ? { "Authorization": "Bearer " + Arena.getToken() } : {} })
      .then(function (res) {
        if (!res.ok || !res.body) throw new Error("stream unavailable (" + res.status + ")");
        stream.connected = true;
        setConnBadge("LIVE");
        var reader = res.body.getReader();
        var decoder = new TextDecoder();
        var buffer = "";
        function pump() {
          return reader.read().then(function (chunk) {
            if (chunk.done) throw new Error("stream closed");
            buffer += decoder.decode(chunk.value, { stream: true });
            var idx;
            while ((idx = buffer.indexOf("\n\n")) >= 0) {
              var frame = buffer.slice(0, idx); buffer = buffer.slice(idx + 2);
              handleFrame(frame);
            }
            return pump();
          });
        }
        return pump();
      })
      .catch(function () {
        stream.connected = false;
        setConnBadge("POLLING");
        setTimeout(connectStream, stream.retryDelay);
        stream.retryDelay = Math.min(stream.retryDelay * 2, 15000);
      });
  }

  function handleFrame(frame) {
    var lines = frame.split("\n"); var data = "";
    for (var i = 0; i < lines.length; i++) {
      if (lines[i].indexOf("data:") === 0) data += lines[i].slice(5).trim();
    }
    if (!data || data === "[heartbeat]") return;
    var msg = null; try { msg = JSON.parse(data); } catch (e) { return; }
    stream.retryDelay = 2000;
    if (msg.seq) stream.lastSeq = Math.max(stream.lastSeq, msg.seq);
    if (msg.type === "opportunity.detected") {
      var opp = msg.payload || {};
      Arena.toast("info", "New candidate observed: " + (opp.identity || "instrument"));
      if (state.view === "intelligence") {
        coalescedRefresh("opportunities", function () {
          refreshOpportunities(document.getElementById("opp-list"));
        });
      }
    }
    if (msg.type === "state.changed") {
      Arena.toast("info", "What changed: " + ((msg.payload || {}).domain || "state") + "/" + ((msg.payload || {}).object_key || ""));
      if (state.view === "markets") {
        coalescedRefresh("what-changed", function () {
          whatChangedFeed(document.getElementById("mk-changed"), "");
        });
      }
    }
    if (msg.type === "opportunity.transition" && state.view === "intelligence") {
      coalescedRefresh("opportunities", function () {
        refreshOpportunities(document.getElementById("opp-list"));
      });
    }
    if (msg.type && msg.type.indexOf("task.") === 0) {
      coalescedRefresh("tasks-badge", refreshTasksBadge);
    }
    if (msg.type === "live.session") {
      coalescedRefresh("live-chip", refreshLiveChip);
    }
  }

  function setConnBadge(stateName) {
    var host = document.getElementById("conn-badge");
    if (!host) return;
    host.innerHTML = ""; host.appendChild(Arena.stateBadge(stateName));
  }

  // --------------------------------------------------------------- chips ----
  function refreshMarketChip() {
    var host = document.getElementById("market-chip");
    if (!host || host.dataset.loading) return;
    host.dataset.loading = "1";
    var market = localStorage.getItem("axiom_market") || "india";
    Arena.get("/api/markets/" + encodeURIComponent(market) + "/session")
      .then(function (d) {
        host.textContent = market + " · " + d.status;
      })
      .catch(function () { host.textContent = "market: —"; })
      .finally(function () { delete host.dataset.loading; });
  }

  function refreshLiveChip() {
    var host = document.getElementById("live-chip");
    if (!host || host.dataset.loading) return;
    host.dataset.loading = "1";
    Arena.get("/api/live/session")
      .then(function (d) {
        host.innerHTML = "";
        host.appendChild(Arena.stateBadge(d.state || "OFF"));
        host.title = d.emergency_stop_active ? "EMERGENCY STOP ACTIVE" : "Live trading state machine";
      })
      .catch(function () { host.innerHTML = ""; host.appendChild(Arena.stateBadge("OFF")); })
      .finally(function () { delete host.dataset.loading; });
  }

  function refreshTasksBadge() {
    Arena.get("/api/tasks").then(function (d) {
      var el = document.getElementById("sb-tasks");
      if (el) el.textContent = "Tasks " + (d.counts.RUNNING || 0) + " running / " + (d.counts.PENDING || 0) + " pending";
    }).catch(function () {});
  }

  function markFresh(id) {
    var el = document.getElementById(id || "sb-freshness");
    if (el) el.textContent = "updated " + new Date().toLocaleTimeString();
  }

  function updateStatusbar() {
    var engineEl = document.getElementById("sb-engine");
    Arena.get("/api/live/session").then(function (d) {
      if (engineEl) engineEl.textContent = "Engine " + (state.paper && state.paper.state ? state.paper.state : (d.state || "OFF"));
      var riskEl = document.getElementById("sb-risk");
      if (riskEl) riskEl.textContent = d.emergency_stop_active ? "Risk EMERGENCY-STOP" : "Risk CLEAR";
    }).catch(function () {});
    Arena.get("/api/system/control").then(function (d) {
      var comp = d.components || {};
      var kronosEl = document.getElementById("sb-kronos");
      if (kronosEl && comp.kronos) kronosEl.textContent = "Kronos " + comp.kronos;
      var feedEl = document.getElementById("sb-feed");
      if (feedEl) feedEl.textContent = "Feed " + (stream.connected ? "LIVE" : "POLLING");
    }).catch(function () {});
    Arena.get("/api/tasks").then(function (d) {
      var el = document.getElementById("sb-research");
      if (el) el.textContent = "Research " + (d.counts.RUNNING || 0) + " running";
    }).catch(function () {});
    refreshTasksBadge();
    markFresh("sb-freshness");
  }

  // ------------------------------------------------------------ palette ----
  var PALETTE_COMMANDS = [
    ["Go to Command Center", function () { show("command"); }],
    ["Go to Markets", function () { show("markets"); }],
    ["Go to Research", function () { show("research"); }],
    ["Go to Models (Kronos)", function () { show("models"); }],
    ["Go to Trading", function () { show("trading"); }],
    ["Go to Risk", function () { show("risk"); }],
    ["Go to Intelligence", function () { show("intelligence"); }],
    ["Go to Reports", function () { show("reports"); }],
    ["Open System control", function () { show("system"); }],
    ["Stop the paper engine", function () {
      Arena.post("/paper/stop").then(function () { Arena.toast("ok", "Paper engine stop requested."); })
        .catch(function (e) { Arena.toast("err", e.message); });
    }],
    ["Export active universe", function () { exportActiveUniverse(); }],
    ["Set attention: conservative", function () { setAttention("conservative"); }],
    ["Set attention: balanced", function () { setAttention("balanced"); }],
    ["Set attention: aggressive", function () { setAttention("aggressive"); }]
  ];

  function openPalette() {
    if (document.getElementById("palette")) return;
    var overlay = Arena.el("div", { id: "palette", style: "position:fixed;inset:0;background:rgba(5,10,20,.72);z-index:200;display:flex;align-items:flex-start;justify-content:center;padding-top:12vh" });
    var box = Arena.el("div", { "class": "card", style: "width:min(560px,92vw)" }, [
      Arena.el("div", { "class": "card-body" }, [
        Arena.el("input", { id: "palette-input", placeholder: "Search actions… (Esc to close)",
          style: "width:100%;padding:10px;font-size:14px", autocomplete: "off" }),
        Arena.el("div", { id: "palette-list", style: "margin-top:8px;max-height:46vh;overflow:auto" })
      ])
    ]);
    overlay.appendChild(box);
    document.body.appendChild(overlay);
    var input = document.getElementById("palette-input");
    function render(filter) {
      var list = document.getElementById("palette-list");
      list.innerHTML = "";
      PALETTE_COMMANDS.filter(function (c) {
        return !filter || c[0].toLowerCase().indexOf(filter.toLowerCase()) >= 0;
      }).slice(0, 10).forEach(function (c) {
        var row = Arena.el("div", { "class": "palette-row", tabindex: "0",
          style: "padding:8px 10px;border-radius:8px;cursor:pointer" }, [document.createTextNode(c[0])]);
        row.addEventListener("mouseenter", function () { row.style.background = "rgba(120,200,255,.08)"; });
        row.addEventListener("mouseleave", function () { row.style.background = ""; });
        row.addEventListener("click", function () { close(); c[1](); });
        row.addEventListener("keydown", function (e) { if (e.key === "Enter") { close(); c[1](); } });
        list.appendChild(row);
      });
      if (!list.children.length) list.appendChild(Arena.el("div", { "class": "empty" }, [document.createTextNode("No matching actions.")]));
    }
    function close() { document.removeEventListener("keydown", onKey, true); overlay.remove(); }
    function onKey(e) {
      if (e.key === "Escape") close();
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        var rows = Array.prototype.slice.call(document.querySelectorAll(".palette-row"));
        var idx = rows.indexOf(document.activeElement);
        var next = e.key === "ArrowDown" ? Math.min(idx + 1, rows.length - 1) : Math.max(idx - 1, 0);
        if (rows[next]) rows[next].focus();
      }
    }
    input.addEventListener("input", function () { render(input.value); });
    document.addEventListener("keydown", onKey, true);
    render(""); input.focus();
  }
  document.addEventListener("keydown", function (e) {
    if ((e.ctrlKey || e.metaKey) && (e.key === "k" || e.key === "K")) { e.preventDefault(); if (state.tokenGatePassed) openPalette(); }
  });

  // ------------------------------------------------------------ helpers ----
  function attentionBadge(mode) {
    var cls = mode === "aggressive" ? "danger" : mode === "conservative" ? "info" : "warn";
    return Arena.el("span", { "class": "badge " + cls }, [document.createTextNode((mode || "balanced").toUpperCase())]);
  }

  function setAttention(mode) {
    Arena.post("/api/settings/attention", { mode: mode }).then(function (d) {
      Arena.toast("ok", "Attention mode is now " + (d.mode || mode) + ". Research focus only — safety limits unchanged.");
    }).catch(function (e) { Arena.toast("err", e.message); });
  }

  function freshnessRow(tsIso, label) {
    return Arena.el("span", { "class": "faint", style: "font-size:11px" },
      [document.createTextNode((label || "data as of") + ": " + (tsIso ? Arena.timeAgo(tsIso) : "—"))]);
  }

  function whatChangedFeed(host, params) {
    if (!host) return;
    host.innerHTML = "";
    Arena.get("/api/changes" + (params || "")).then(function (d) {
      var changes = d.changes || [];
      if (!changes.length) {
        host.appendChild(Arena.el("div", { "class": "empty" }, [document.createTextNode("No state changes recorded yet.")]));
        return;
      }
      changes.slice(0, 15).forEach(function (c) {
        host.appendChild(Arena.el("div", { style: "margin-bottom:4px" }, [
          Arena.el("span", { "class": "faint mono", style: "font-size:11px" }, [document.createTextNode(Arena.timeAgo(c.ts) + "  ")]),
          Arena.el("b", {}, [document.createTextNode(c.domain + "/" + c.object_key + " · " + c.field + " ")]),
          document.createTextNode(String(c.previous === undefined || c.previous === null ? "—" : c.previous) + " → " +
            String(c.current === undefined || c.current === null ? "—" : c.current))
        ]));
      });
    }).catch(function (e) { host.innerHTML = ""; host.appendChild(Arena.el("span", { "class": "faint" }, [document.createTextNode(e.message)])); });
  }

  function exportActiveUniverse() {
    var id = localStorage.getItem("axiom_universe");
    if (!id) { Arena.toast("err", "Select a universe on the Markets page first."); return; }
    Arena.get("/api/universes/" + encodeURIComponent(id) + "/export")
      .then(function () { Arena.toast("ok", "Universe exported to data/universes/ (check the data folder)."); })
      .catch(function (e) { Arena.toast("err", e.message); });
  }

  function kvRows(rows) {
    var out = [];
    rows.forEach(function (r) {
      out.push(Arena.el("dt", {}, [document.createTextNode(r[0])]));
      out.push(Arena.el("dd", {}, [document.createTextNode(String(r[1]))]));
    });
    return out;
  }

  function startStatusPolling() {
    if (state.axiomPolling) return;
    state.axiomPolling = setInterval(function () {
      if (!document.hidden) updateStatusbar();
    }, 5000);
  }

  // =====================================================================
  // Forex: scope and blockers only. No entry/order controls are exposed.
  // =====================================================================
  function viewForex(root) {
    root.appendChild(Arena.el("h1", { "class": "page-title" }, [document.createTextNode("Forex — blocked / research specification only")]));
    root.appendChild(Arena.el("p", { "class": "page-sub" }, [document.createTextNode(
      "This is not a supported or trade-ready Forex path. The candidate scope is India-listed USDINR currency futures only; OTC spot and CFDs are different products and are excluded. No broker or provider connection is attempted here.")]));
    var host = Arena.el("div", { "class": "grid cols-2" });
    var scope = Arena.el("div", { "class": "card-body" }, [document.createTextNode("Loading scope evidence…")]);
    host.appendChild(card("Candidate instrument and current evidence", scope));
    var blockers = Arena.el("div", { "class": "card-body" }, [document.createTextNode("Loading blockers…")]);
    host.appendChild(card("Readiness and external actions", blockers));
    root.appendChild(host);
    Arena.get("/api/forex/scope").then(function (d) {
      scope.innerHTML = "";
      scope.appendChild(Arena.el("div", { "class": "banner warn" }, [
        Arena.el("b", {}, [document.createTextNode(d.support_status || "BLOCKED_UNVERIFIED")]),
        document.createTextNode(" · Paper accounting domain only; no connected Forex trading lifecycle.")
      ]));
      scope.appendChild(Arena.el("dl", { "class": "kv" }, kvRows([
        ["Asset class", d.asset_class], ["Jurisdiction", d.jurisdiction],
        ["Product", d.candidate_product], ["Pair", d.candidate_pair],
        ["Venue / provider segment", d.exchange + " / " + d.provider_segment],
        ["Provider / account", (d.market_data && d.market_data.provider) + " / " + (d.market_data && d.market_data.account_entitlement)],
        ["Mode", d.paper && d.paper.mode + " only"],
        ["Account currency", d.account_currency],
        ["Contract units", d.contract_units_per_lot + " USD per lot"],
        ["Exchange tick", d.tick_size_quote_currency + " INR per USD"],
        ["Display pip convention", d.display_pip_size_quote_currency + " INR; " + d.ticks_per_display_pip + " ticks per display pip"],
        ["Session", d.session_hint && d.session_hint.open + "–" + d.session_hint.close + " " + d.session_hint.timezone],
        ["Holiday / provider status", d.session_hint && d.session_hint.holiday_status + " / " + (d.market_data && d.market_data.quote_freshness)],
        ["Paper engine / persistence", d.paper && d.paper.accounting_module + " / " + d.paper.durable_state],
        ["Live", "DISABLED — " + (d.live && d.live.reason)]
      ])));
      scope.appendChild(Arena.el("p", { "class": "hint faint" }, [document.createTextNode(
        "The display pip is only a calculation convention for this exchange future; it is not an OTC spot contract. Simulated fills, if added, would not be broker execution evidence.")]));
      blockers.innerHTML = "";
      (d.blockers || []).forEach(function (item) {
        blockers.appendChild(Arena.el("div", { "class": "banner warn", style: "margin:6px 0" }, [document.createTextNode(item)]));
      });
      var excluded = Arena.el("p", { "class": "hint faint" }, [document.createTextNode(
        "Excluded: " + (d.excluded_products || []).join(", ") + ". No readiness, Arm, Activate, order, or cancellation controls are provided on this screen.")]);
      blockers.appendChild(excluded);
      var refs = Arena.el("div", { "class": "hint faint" }, [document.createTextNode("Evidence references: ")]);
      (d.source_urls || []).forEach(function (url, index) {
        if (index) refs.appendChild(document.createTextNode(" · "));
        refs.appendChild(Arena.el("a", { href: url, target: "_blank", rel: "noopener noreferrer" }, [document.createTextNode("Source " + (index + 1))]));
      });
      blockers.appendChild(refs);
      blockers.appendChild(freshnessRow(new Date().toISOString(), "scope evaluated"));
    }).catch(function (e) {
      scope.innerHTML = ""; blockers.innerHTML = "";
      scope.appendChild(Arena.el("div", { "class": "banner err" }, [document.createTextNode("Scope endpoint unavailable: " + e.message)]));
      blockers.appendChild(Arena.el("div", { "class": "banner warn" }, [document.createTextNode("Unknown evidence remains blocked. No Forex execution controls are available." )]));
    });
  }

  // =====================================================================
  // MARKETS workstation
  // =====================================================================
  function viewMarkets(root) {
    root.appendChild(Arena.el("h1", { "class": "page-title" }, [document.createTextNode("Markets")]));
    root.appendChild(Arena.el("p", { "class": "page-sub" }, [document.createTextNode(
      "Market scopes, session clocks, universes and the instrument workspace. Symbols are market-scoped identities; every value shown states its source (OBSERVED only).")]));

    var grid = Arena.el("div", { "class": "grid cols-2" });
    var sessionHost = Arena.el("div", { id: "mk-session" });
    grid.appendChild(card("Session state", sessionHost));
    var universeHost = Arena.el("div", { id: "mk-universes" });
    grid.appendChild(card("Universes", universeHost));
    root.appendChild(grid);

    var instrumentHost = Arena.el("div", { id: "mk-instrument", "class": "section-gap" });
    root.appendChild(card("Instrument workspace", instrumentHost));

    var chartHost = Arena.el("div", { id: "mk-charts", "class": "section-gap" });
    root.appendChild(card("Chart workspace", chartHost));
    renderChartWorkspace(chartHost);

    var changedHost = Arena.el("div", { id: "mk-changed" });
    root.appendChild(Arena.el("div", { "class": "section-gap" }, [card("What changed", changedHost)]));
    whatChangedFeed(changedHost, "");

    renderMarketSessions(sessionHost);
    renderUniverseStudio(universeHost, instrumentHost);
    startStatusPolling();
  }

  function renderMarketSessions(host) {
    Arena.get("/api/markets").then(function (d) {
      var markets = d.markets || [];
      var active = localStorage.getItem("axiom_market") || (markets[0] && markets[0].market_id) || d.selected || "india";
      localStorage.setItem("axiom_market", active);
      host.innerHTML = "";
      var tabs = Arena.el("div", { "class": "btn-row", style: "flex-wrap:wrap" });
      markets.forEach(function (m) {
        tabs.appendChild(Arena.el("button", {
          "class": "btn sm " + (m.market_id === active ? "primary" : "ghost"),
          onclick: function () { localStorage.setItem("axiom_market", m.market_id); viewMarketsRefresh(); }
        }, [document.createTextNode(m.display_name || m.market_id)]));
      });
      host.appendChild(tabs);
      var meta = markets.filter(function (m) { return m.market_id === active; })[0];
      var body = Arena.el("div", { style: "margin-top:10px" }, [document.createTextNode("Loading…")]);
      host.appendChild(body);
      Arena.get("/api/markets/" + encodeURIComponent(active) + "/session").then(function (s) {
        body.innerHTML = "";
        body.appendChild(Arena.el("dl", { "class": "kv" }, kvRows([
          ["Status", s.status], ["Reason", s.reason || "—"],
          ["Local time", s.local_time || "—"], ["Timezone", s.timezone || (meta && meta.timezone) || "—"],
          ["Currency", (meta && meta.currency) || "—"],
          ["Exchanges", (meta && meta.exchanges ? meta.exchanges.join(", ") : "—")],
          ["Next transition", s.next_transition ? s.next_transition + " (" + s.minutes_to_next_transition + " min)" : "—"]
        ])));
        body.appendChild(freshnessRow(new Date().toISOString(), "evaluated"));
      }).catch(function (e) { body.innerHTML = ""; body.appendChild(Arena.el("span", { "class": "faint" }, [document.createTextNode(e.message)])); });
    }).catch(function (e) { host.innerHTML = ""; host.appendChild(Arena.el("span", { "class": "faint" }, [document.createTextNode(e.message)])); });
  }

  function viewMarketsRefresh() {
    var root = document.getElementById("main-view");
    if (root && state.view === "markets") { root.innerHTML = ""; viewMarkets(root); }
  }

  function renderUniverseStudio(host, instrumentHost) {
    function paint() {
      Arena.get("/api/universes").then(function (d) {
        host.innerHTML = "";
        var universes = d.universes || [];
        if (!universes.length) {
          host.appendChild(Arena.el("div", { "class": "empty" }, [document.createTextNode("No universes yet — create one below.")]));
        }
        var list = Arena.el("div", {});
        universes.forEach(function (u) {
          list.appendChild(Arena.el("div", { style: "display:flex;gap:8px;align-items:center;margin:4px 0;flex-wrap:wrap" }, [
            Arena.el("b", {}, [document.createTextNode(u.name)]),
            Arena.el("span", { "class": "faint" }, [document.createTextNode(u.market_id + " · " + (u.symbols || []).length + " symbols")]),
            Arena.el("span", { style: "flex:1" }),
            Arena.el("button", { "class": "btn ghost sm", onclick: function () {
              localStorage.setItem("axiom_universe", u.id);
              Arena.toast("ok", "Active universe: " + u.name);
              renderInstrumentWorkspace(instrumentHost);
            } }, [document.createTextNode("Select")]),
            Arena.el("button", { "class": "btn ghost sm", onclick: function () {
              Arena.get("/api/universes/" + encodeURIComponent(u.id) + "/export")
                .then(function () { Arena.toast("ok", "Exported to data/universes/"); })
                .catch(function (e) { Arena.toast("err", e.message); });
            } }, [document.createTextNode("Export")]),
            Arena.el("button", { "class": "btn ghost sm", onclick: function () {
              Arena.api("DELETE", "/api/universes/" + encodeURIComponent(u.id)).then(function () {
                Arena.toast("ok", "Universe removed."); paint();
              }).catch(function (e) { Arena.toast("err", e.message); });
            } }, [document.createTextNode("Remove")])
          ]));
        });
        host.appendChild(list);

        var form = Arena.el("div", { "class": "section-gap" }, [
          Arena.el("div", { "class": "field" }, [
            Arena.el("label", { for: "uni-name" }, [document.createTextNode("Name")]),
            Arena.el("input", { id: "uni-name", placeholder: "e.g. Nifty 50 core" })
          ]),
          Arena.el("div", { "class": "field" }, [
            Arena.el("label", { for: "uni-symbols" }, [document.createTextNode("Symbols (one per line, e.g. NSE|RELIANCE, NYSE|AAPL, FOREX|EUR/USD)")]),
            Arena.el("textarea", { id: "uni-symbols", rows: "3", style: "width:100%" })
          ]),
          Arena.el("div", { "class": "btn-row" }, [
            Arena.el("button", { "class": "btn primary sm", id: "uni-create" }, [document.createTextNode("Create universe")])
          ])
        ]);
        host.appendChild(form);
        document.getElementById("uni-create").addEventListener("click", function () {
          var name = document.getElementById("uni-name").value.trim();
          var symbols = document.getElementById("uni-symbols").value.split("\n").map(function (s) { return s.trim(); }).filter(Boolean);
          var market = localStorage.getItem("axiom_market") || "india";
          Arena.post("/api/universes", { market_id: market, name: name, symbols: symbols }).then(function () {
            Arena.toast("ok", "Universe created.");
            document.getElementById("uni-name").value = ""; document.getElementById("uni-symbols").value = ""; paint();
          }).catch(function (e) { Arena.toast("err", e.message); });
        });
      }).catch(function (e) { host.innerHTML = ""; host.appendChild(Arena.el("span", { "class": "faint" }, [document.createTextNode(e.message)])); });
    }
    paint();
    renderInstrumentWorkspace(instrumentHost);
  }

  function renderInstrumentWorkspace(host) {
    host.innerHTML = "";
    var universeId = localStorage.getItem("axiom_universe");
    if (!universeId) {
      host.appendChild(Arena.el("div", { "class": "empty" }, [document.createTextNode("Select a universe to open its instrument workspace.")]));
      return;
    }
    Arena.get("/api/universes/" + encodeURIComponent(universeId)).then(function (d) {
      var u = d.universe || d;
      host.innerHTML = "";
      host.appendChild(Arena.el("h3", {}, [document.createTextNode(u.name + " (" + u.market_id + ")")]));
      var chips = Arena.el("div", { "class": "btn-row", style: "flex-wrap:wrap" });
      (u.symbols || []).forEach(function (sym) {
        chips.appendChild(Arena.el("button", { "class": "btn ghost sm", id: "instr-" + sym.replace(/[^A-Za-z0-9]/g, "_"),
          onclick: function () { renderInstrumentDetail(u.market_id, sym); linkChartSymbol(sym); } }, [document.createTextNode(sym)]));
      });
      if (!(u.symbols || []).length) chips.appendChild(Arena.el("span", { "class": "faint" }, [document.createTextNode("Universe has no symbols.")]));
      host.appendChild(chips);
      Arena.get("/api/universe/pins?market_id=" + encodeURIComponent(u.market_id)).then(function (p) {
        (p.pins || []).forEach(function (pin) {
          chips.appendChild(Arena.el("span", { "class": "badge info", style: "margin-left:6px" },
            [document.createTextNode("pinned: " + (pin.identity || pin))]));
        });
      }).catch(function () {});
      host.appendChild(Arena.el("div", { id: "instr-detail", "class": "section-gap" }));
    }).catch(function (e) { host.innerHTML = ""; host.appendChild(Arena.el("span", { "class": "faint" }, [document.createTextNode(e.message)])); });
  }

  function renderInstrumentDetail(marketId, identity) {
    var detail = document.getElementById("instr-detail");
    if (!detail) return;
    detail.innerHTML = "";
    detail.appendChild(document.createTextNode("Loading evidence for " + identity + "…"));
    Arena.get("/api/evidence/" + encodeURIComponent(marketId) + "/" + encodeURIComponent(identity)).then(function (ev) {
      detail.innerHTML = "";
      detail.appendChild(Arena.el("h3", {}, [document.createTextNode(identity + " — evidence stack")]));
      var layers = ev.layers || ev;
      (Array.isArray(layers) ? layers : []).forEach(function (layer) {
        var status = layer.status || "NOT LOADED";
        var row = Arena.el("div", { style: "margin:6px 0;padding:8px;border:1px solid #1d2739;border-radius:8px" }, [
          Arena.el("b", {}, [document.createTextNode(layer.layer + " ")]),
          Arena.stateBadge(status),
          Arena.el("div", { "class": "mono faint", style: "font-size:11.5px;margin-top:4px;word-break:break-word" },
            [document.createTextNode(layer.status === "NOT LOADED" || layer.value === undefined || layer.value === null
              ? ("UNAVAILABLE — " + (layer.unavailable_reason || "no data recorded"))
              : JSON.stringify(layer.value))]),
          Arena.el("div", { "class": "faint", style: "font-size:11px" }, [document.createTextNode("provenance: " + (layer.provenance || "—"))])
        ]);
        detail.appendChild(row);
      });
      detail.appendChild(Arena.el("p", { "class": "hint faint", style: "font-size:11.5px" }, [
        document.createTextNode("Recommendations are guidance for human review only; the execution path applies its own risk gates regardless.")
      ]));
      var actions = Arena.el("div", { "class": "btn-row", style: "flex-wrap:wrap" });
      var recBtn = Arena.el("button", { "class": "btn ghost sm", id: "evidence-recommend-" + identity.replace(/[^A-Za-z0-9]/g, "_") }, [document.createTextNode("Log WATCH recommendation")]);
      recBtn.addEventListener("click", function () {
        Arena.post("/api/evidence/" + encodeURIComponent(marketId) + "/" + encodeURIComponent(identity) +
          "/recommend?state=WATCH&rationale=" + encodeURIComponent("logged from instrument workspace"), {})
          .then(function (r) { Arena.toast("ok", "Recommendation logged: " + (r.recommendation ? r.recommendation.state : "recorded")); })
          .catch(function (e) { Arena.toast("err", e.message); });
      });
      actions.appendChild(recBtn);
      var missedBtn = Arena.el("button", { "class": "btn ghost sm" }, [document.createTextNode("Log missed opportunity")]);
      missedBtn.addEventListener("click", function () {
        Arena.post("/api/research/missed", { kind: "false_negative", payload: { source: "instrument workspace" },
          knowable: true, market_id: marketId, identity: identity,
          research_question: "Why was this setup not acted on?" })
          .then(function () { Arena.toast("ok", "Recorded in the missed-opportunity ledger."); })
          .catch(function (e) { Arena.toast("err", e.message); });
      });
      actions.appendChild(missedBtn);
      addInstrumentPinControl(actions, marketId, function () { return identity; },
        "instrument " + identity);
      detail.appendChild(actions);

      var analogHost = Arena.el("div", { "class": "section-gap" });
      detail.appendChild(analogHost);
      Arena.post("/api/analogues/search", { market_id: marketId, identity: identity }).then(function (a) {
        analogHost.innerHTML = "";
        analogHost.appendChild(Arena.el("b", {}, [document.createTextNode("Historical analogues (" + a.sample_size + " candidate windows, " +
          (a.enough_history ? "enough history" : "insufficient history — treated as such") + ")")]));
        var results = a.results || [];
        if (!results.length) { analogHost.appendChild(Arena.el("div", { "class": "empty" }, [document.createTextNode("No analogue matches recorded.")])); return; }
        var table = Arena.el("table", { "class": "table" });
        table.appendChild(Arena.el("tr", {}, ["Window start", "Similarity", "Forward return", "Forward vol"].map(function (h) { return Arena.el("th", {}, [document.createTextNode(h)]); })));
        results.forEach(function (r) {
          table.appendChild(Arena.el("tr", {}, [
            Arena.el("td", {}, [document.createTextNode(String(r.analogue_start_index))]),
            Arena.el("td", {}, [document.createTextNode((r.similarity * 100).toFixed(1) + "%")]),
            Arena.el("td", {}, [document.createTextNode(r.forward_return !== null && r.forward_return !== undefined ? (r.forward_return * 100).toFixed(2) + "%" : "—")]),
            Arena.el("td", {}, [document.createTextNode(r.forward_volatility !== null && r.forward_volatility !== undefined ? (r.forward_volatility * 100).toFixed(2) + "%" : "—")])
          ]));
        });
        analogHost.appendChild(table);
        if (a.limitations && a.limitations.length) {
          analogHost.appendChild(Arena.el("div", { "class": "faint", style: "font-size:11px;margin-top:4px" },
            [document.createTextNode("Limitations: " + a.limitations.join("; "))]));
        }
      }).catch(function () {});
    }).catch(function (e) { detail.innerHTML = ""; detail.appendChild(Arena.el("span", { "class": "faint" }, [document.createTextNode(e.message)])); });
  }

  // =====================================================================
  // CHART WORKSPACE (spec §49: layouts 1/2/4/6/8, linked panels, focus F)
  // =====================================================================
  var chartRegistry = Object.create(null);
  var draggedChartPanel = null;

  function chartConfig() {
    var raw = localStorage.getItem("axiom_chart_layout");
    try {
      var cfg = JSON.parse(raw);
      if (cfg && [1, 2, 4, 6, 8].indexOf(cfg.layout) >= 0 && Array.isArray(cfg.panels)) return cfg;
    } catch (e) { /* fall through to default */ }
    return { layout: 1, panels: [{ identity: "", unit: "days", interval: 1 }] };
  }
  function saveChartConfig(cfg) {
    localStorage.setItem("axiom_chart_layout", JSON.stringify(cfg));
  }

  function renderChartWorkspace(host) {
    host.innerHTML = "";
    chartRegistry = Object.create(null);
    draggedChartPanel = null;
    var cfg = chartConfig();
    host.appendChild(Arena.el("p", { "class": "muted", style: "font-size:11.5px" }, [document.createTextNode(
      "Charts render RECORDED candles only — an empty chart means nothing is recorded, never a silent zero. " +
      "Picking a symbol in the instrument workspace updates panel 1; panel layout and height are saved. " +
      "Crosshairs are time-linked. Drag or move panels to reorder, resize their chart height, and pin instruments. " +
      "Press F for focus mode.")]));

    var bar = Arena.el("div", { "class": "btn-row", style: "flex-wrap:wrap;margin-bottom:8px" });
    bar.appendChild(Arena.el("span", { "class": "faint", style: "align-self:center" }, [document.createTextNode("Layout:")]));
    [1, 2, 4, 6, 8].forEach(function (n) {
      bar.appendChild(Arena.el("button", {
        "class": "btn sm " + (cfg.layout === n ? "primary" : "ghost"),
        id: "chart-layout-" + n,
        onclick: function () {
          var c = chartConfig(); c.layout = n;
          while (c.panels.length < n) c.panels.push({ identity: c.panels[0] ? c.panels[0].identity : "", unit: "days", interval: 1 });
          saveChartConfig(c); renderChartWorkspace(host);
        }
      }, [document.createTextNode(String(n))]));
    });
    var focusBtn = Arena.el("button", { "class": "btn ghost sm", id: "chart-focus", style: "margin-left:auto" },
      [document.createTextNode("Focus mode (F)")]);
    focusBtn.addEventListener("click", toggleFocusMode);
    bar.appendChild(focusBtn);
    host.appendChild(bar);

    var grid = Arena.el("div", { "class": "chart-grid cols-" + cfg.layout, id: "chart-grid" });
    for (var i = 0; i < cfg.layout; i++) {
      var panel = cfg.panels[i] || { identity: "", unit: "days", interval: 1 };
      grid.appendChild(buildChartPanel(i, panel, cfg.layout, host));
    }
    host.appendChild(grid);
  }

  function chartPanelHeight(value) {
    var height = parseInt(value, 10);
    if (!Number.isFinite(height)) return 320;
    return Math.max(260, Math.min(560, Math.round(height / 20) * 20));
  }

  function reorderChartPanels(from, to, host) {
    var cfg = chartConfig();
    if (!Number.isInteger(from) || !Number.isInteger(to)
        || from < 0 || to < 0 || from >= cfg.layout || to >= cfg.layout
        || from === to) return;
    while (cfg.panels.length < cfg.layout) {
      cfg.panels.push({ identity: "", unit: "days", interval: 1, height: 320 });
    }
    var visible = cfg.panels.slice(0, cfg.layout);
    var hidden = cfg.panels.slice(cfg.layout);
    var moved = visible.splice(from, 1)[0];
    visible.splice(to, 0, moved);
    cfg.panels = visible.concat(hidden);
    saveChartConfig(cfg);
    renderChartWorkspace(host);
  }

  function addInstrumentPinControl(container, marketId, identityProvider, label) {
    var button = Arena.el("button", { "class": "btn ghost sm", type: "button",
      "aria-label": "Pin or unpin " + label }, [document.createTextNode("Checking pin…")]);
    var requestSerial = 0;

    function refresh() {
      var serial = ++requestSerial;
      var identity = String(identityProvider() || "").trim();
      if (!identity) {
        button.disabled = true;
        button.textContent = "Pin";
        button.dataset.identity = "";
        button.dataset.pinned = "false";
        return;
      }
      button.disabled = true;
      button.textContent = "Checking pin…";
      Arena.get("/api/universe/pins?market_id=" + encodeURIComponent(marketId))
        .then(function (result) {
          if (serial !== requestSerial
              || String(identityProvider() || "").trim() !== identity) return;
          var pins = result.pins || [];
          var pinned = pins.some(function (pin) {
            return String(pin.identity || pin) === identity;
          });
          button.dataset.identity = identity;
          button.dataset.pinned = pinned ? "true" : "false";
          button.setAttribute("aria-pressed", pinned ? "true" : "false");
          button.title = pinned ? "Unpin this instrument from the workspace"
                                : "Pin this instrument to the workspace";
          button.textContent = pinned ? "Unpin" : "Pin";
          button.disabled = false;
        }).catch(function (error) {
          if (serial !== requestSerial) return;
          button.disabled = true;
          button.textContent = "Pin status unavailable";
          button.title = error.message || "Could not read the saved pin state.";
        });
    }

    button.addEventListener("click", function () {
      var identity = String(identityProvider() || "").trim();
      if (!identity || button.disabled) return;
      if (button.dataset.identity !== identity) { refresh(); return; }
      var pinned = button.dataset.pinned === "true";
      button.disabled = true;
      button.textContent = pinned ? "Unpinning…" : "Pinning…";
      Arena.post(pinned ? "/api/universe/unpin" : "/api/universe/pin",
        { market_id: marketId, identity: identity }).then(function () {
        Arena.toast("ok", (pinned ? "Unpinned " : "Pinned ") + identity + ".");
        refresh();
      }).catch(function (error) {
        Arena.toast("err", error.message);
        refresh();
      });
    });

    if (container) container.appendChild(button);
    refresh();
    return { button: button, refresh: refresh };
  }

  function buildChartPanel(index, panel, layout, workspaceHost) {
    panel = panel || { identity: "", unit: "days", interval: 1 };
    var box = Arena.el("div", { "class": "chart-panel", id: "chart-panel-" + index,
      "data-chart-index": String(index) });
    var ident = Arena.el("input", { id: "chart-symbol-" + index,
      placeholder: "e.g. NSE|RELIANCE", value: panel.identity || "",
      style: "min-width:160px", "aria-label": "Instrument identity for chart " + (index + 1) });
    var unit = Arena.el("select", { id: "chart-unit-" + index,
      "aria-label": "Candle interval for chart " + (index + 1) });
    [["days", "1D"]].forEach(function (u) {
      unit.appendChild(Arena.el("option", { value: u[0] }, [document.createTextNode(u[1])]));
    });
    unit.value = panel.unit || "days";
    var refresh = Arena.el("button", { "class": "btn ghost sm", id: "chart-refresh-" + index,
      type: "button" }, [document.createTextNode("Load")]);
    var marketId = localStorage.getItem("axiom_market") || "india";
    var pinControl = addInstrumentPinControl(null, marketId,
      function () { return ident.value; }, "chart instrument " + (index + 1));
    var chartHost = Arena.el("div", { id: "chart-canvas-" + index, "class": "chart-canvas" });
    var dragHandle = Arena.el("button", { "class": "btn ghost sm chart-drag-handle",
      type: "button", draggable: "true", title: "Drag to reorder this chart",
      "aria-label": "Drag to reorder chart panel " + (index + 1) },
      [document.createTextNode("↕")]);
    dragHandle.addEventListener("dragstart", function (event) {
      draggedChartPanel = index;
      box.classList.add("chart-dragging");
      if (event.dataTransfer) {
        event.dataTransfer.effectAllowed = "move";
        event.dataTransfer.setData("text/plain", String(index));
      }
    });
    dragHandle.addEventListener("dragend", function () {
      draggedChartPanel = null;
      box.classList.remove("chart-dragging", "chart-drop-target");
    });
    box.addEventListener("dragover", function (event) {
      if (draggedChartPanel === null) return;
      event.preventDefault();
      if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
      box.classList.add("chart-drop-target");
    });
    box.addEventListener("dragleave", function () {
      box.classList.remove("chart-drop-target");
    });
    box.addEventListener("drop", function (event) {
      event.preventDefault();
      box.classList.remove("chart-drop-target");
      var from = draggedChartPanel;
      if (event.dataTransfer) {
        var transferred = parseInt(event.dataTransfer.getData("text/plain"), 10);
        if (Number.isInteger(transferred)) from = transferred;
      }
      draggedChartPanel = null;
      reorderChartPanels(from, index, workspaceHost);
    });

    var moveEarlier = Arena.el("button", { "class": "btn ghost sm", type: "button",
      title: "Move chart earlier", "aria-label": "Move chart panel " + (index + 1) + " earlier" },
      [document.createTextNode("↑")]);
    moveEarlier.disabled = index === 0;
    moveEarlier.addEventListener("click", function () {
      reorderChartPanels(index, index - 1, workspaceHost);
    });
    var moveLater = Arena.el("button", { "class": "btn ghost sm", type: "button",
      title: "Move chart later", "aria-label": "Move chart panel " + (index + 1) + " later" },
      [document.createTextNode("↓")]);
    moveLater.disabled = index >= layout - 1;
    moveLater.addEventListener("click", function () {
      reorderChartPanels(index, index + 1, workspaceHost);
    });

    var head = Arena.el("div", { "class": "btn-row chart-panel-head",
      style: "flex-wrap:wrap;margin-bottom:6px" }, [
      dragHandle,
      Arena.el("span", { "class": "faint chart-panel-number" },
        [document.createTextNode("#" + (index + 1))]),
      moveEarlier, moveLater, ident, unit, refresh, pinControl.button
    ]);
    var height = chartPanelHeight(panel.height);
    var heightInput = Arena.el("input", { type: "range", min: "260", max: "560",
      step: "20", value: String(height), id: "chart-height-" + index,
      "aria-label": "Chart panel " + (index + 1) + " height" });
    heightInput.value = String(height);
    var heightLabel = Arena.el("span", { id: "chart-height-value-" + index,
      "class": "faint mono" }, [document.createTextNode(height + " px")]);
    var sizeRow = Arena.el("div", { "class": "chart-size-row" }, [
      Arena.el("label", { "for": "chart-height-" + index },
        [document.createTextNode("Chart height")]), heightInput, heightLabel
    ]);
    heightInput.addEventListener("input", function () {
      height = chartPanelHeight(heightInput.value);
      heightInput.value = String(height);
      heightLabel.textContent = height + " px";
      var cfg = chartConfig();
      while (cfg.panels.length <= index) {
        cfg.panels.push({ identity: "", unit: "days", interval: 1, height: 320 });
      }
      cfg.panels[index].height = height;
      saveChartConfig(cfg);
      var existing = chartRegistry[index];
      if (existing) drawCandleChart(existing.container, existing.candles,
        existing.label, index, height);
    });
    ident.addEventListener("change", pinControl.refresh);
    refresh.addEventListener("click", load);
    box.appendChild(head);
    box.appendChild(sizeRow);
    box.appendChild(chartHost);

    var loadSerial = 0;
    function load() {
      var request = ++loadSerial;
      var symbol = ident.value.trim();
      delete chartRegistry[index];
      clearSharedCrosshair();
      if (!symbol) {
        chartHost.innerHTML = "";
        chartHost.appendChild(Arena.el("div", { "class": "empty" },
          [document.createTextNode("Enter an instrument identity to chart.")]));
        return;
      }
      var market = localStorage.getItem("axiom_market") || "india";
      chartHost.innerHTML = "";
      chartHost.appendChild(Arena.el("div", { "class": "muted" },
        [document.createTextNode("Loading recorded bars…")]));
      Arena.get("/api/candles/" + encodeURIComponent(market) + "/" + encodeURIComponent(symbol) +
        "?unit=" + encodeURIComponent(unit.value) + "&interval=1&limit=300").then(function (data) {
        if (request !== loadSerial
            || document.getElementById("chart-panel-" + index) !== box) return;
        var cfg = chartConfig();
        while (cfg.panels.length <= index) {
          cfg.panels.push({ identity: "", unit: "days", interval: 1, height: 320 });
        }
        cfg.panels[index] = { identity: symbol, unit: unit.value, interval: 1,
          height: chartPanelHeight(heightInput.value) };
        saveChartConfig(cfg);
        chartHost.innerHTML = "";
        if (!data.count) {
          chartHost.appendChild(Arena.el("div", { "class": "empty" }, [
            document.createTextNode("No recorded bars for " + symbol + " — nothing was synthesised.")]));
          return;
        }
        drawCandleChart(chartHost, data.candles, symbol, index,
          chartPanelHeight(heightInput.value));
      }).catch(function (error) {
        if (request !== loadSerial
            || document.getElementById("chart-panel-" + index) !== box) return;
        chartHost.innerHTML = "";
        chartHost.appendChild(Arena.el("div", { "class": "banner err" },
          [document.createTextNode(error.message)]));
      });
    }

    if (panel.identity) load();
    return box;
  }

  function linkChartSymbol(symbol) {
    var cfg = chartConfig();
    if (!cfg.panels.length) cfg.panels.push({ identity: symbol, unit: "days", interval: 1 });
    cfg.panels[0] = { identity: symbol, unit: (cfg.panels[0] && cfg.panels[0].unit) || "days", interval: 1 };
    saveChartConfig(cfg);
    var input = document.getElementById("chart-symbol-0");
    if (input) {
      input.value = symbol;
      var refresh = document.getElementById("chart-refresh-0");
      if (refresh) refresh.click();
    }
  }

  function clearSharedCrosshair() {
    Object.keys(chartRegistry).forEach(function (key) {
      var chart = chartRegistry[key];
      chart.vertical.setAttribute("visibility", "hidden");
      chart.horizontal.setAttribute("visibility", "hidden");
      chart.readout.textContent = chart.readoutHint;
    });
  }

  function nearestChartCandle(chart, timestamp, sourceIndex, sourceCount) {
    var target = Date.parse(String(timestamp || ""));
    var bestIndex = -1;
    var bestDistance = Infinity;
    if (Number.isFinite(target)) {
      chart.candles.forEach(function (candle, index) {
        var time = Date.parse(String(candle.ts || ""));
        if (!Number.isFinite(time)) return;
        var distance = Math.abs(time - target);
        if (distance < bestDistance) {
          bestDistance = distance;
          bestIndex = index;
        }
      });
    } else {
      bestIndex = chart.candles.findIndex(function (candle) {
        return String(candle.ts || "") === String(timestamp || "");
      });
    }
    if (bestIndex >= 0) return bestIndex;
    var fraction = sourceCount > 1 ? sourceIndex / (sourceCount - 1) : 0;
    return Math.max(0, Math.min(chart.candles.length - 1,
      Math.round(fraction * (chart.candles.length - 1))));
  }

  function syncSharedCrosshair(timestamp, sourceIndex, sourceCount, yFraction) {
    var fraction = Math.max(0, Math.min(1, yFraction));
    Object.keys(chartRegistry).forEach(function (key) {
      var chart = chartRegistry[key];
      if (!chart.candles.length) return;
      var index = nearestChartCandle(chart, timestamp, sourceIndex, sourceCount);
      var candle = chart.candles[index];
      var x = chart.padL + index * chart.step + chart.step / 2;
      var y = chart.padT + fraction * chart.priceH;
      chart.vertical.setAttribute("x1", x);
      chart.vertical.setAttribute("x2", x);
      chart.horizontal.setAttribute("y1", y);
      chart.horizontal.setAttribute("y2", y);
      chart.vertical.setAttribute("visibility", "visible");
      chart.horizontal.setAttribute("visibility", "visible");
      chart.readout.textContent = String(candle.ts || "timestamp unavailable") +
        " · O " + chart.formatPrice(candle.open) +
        " H " + chart.formatPrice(candle.high) +
        " L " + chart.formatPrice(candle.low) +
        " C " + chart.formatPrice(candle.close) +
        " · V " + chart.formatPrice(candle.volume || 0);
    });
  }

  function drawCandleChart(container, candles, label, panelIndex, height) {
    container.innerHTML = "";
    candles = Array.isArray(candles) ? candles : [];
    if (!candles.length) return;
    var W = 640, H = chartPanelHeight(height), padL = 46, padR = 8, padT = 10, padB = 34, volH = 40;
    var highs = candles.map(function (c) { return Number(c.high); });
    var lows = candles.map(function (c) { return Number(c.low); });
    var vols = candles.map(function (c) { return Number(c.volume) || 0; });
    var max = Math.max.apply(null, highs), min = Math.min.apply(null, lows);
    var maxVol = Math.max.apply(null, vols) || 1;
    if (max === min) { max += 1; min -= 1; }
    var priceH = H - padT - padB - volH;
    var step = (W - padL - padR) / candles.length;
    var bw = Math.max(1.5, step * 0.6);
    var ns = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", "0 0 " + W + " " + H);
    svg.setAttribute("width", "100%");
    svg.setAttribute("height", String(H));
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "Recorded candle chart for " + label +
      ". Move the pointer to inspect and synchronize bars across chart panels.");

    function y(price) { return padT + (1 - (price - min) / (max - min)) * priceH; }
    // gridlines + price axis (5 levels)
    for (var g = 0; g <= 4; g++) {
      var price = min + (max - min) * g / 4;
      var gy = y(price);
      var line = document.createElementNS(ns, "line");
      line.setAttribute("x1", padL); line.setAttribute("x2", W - padR);
      line.setAttribute("y1", gy); line.setAttribute("y2", gy);
      line.setAttribute("stroke", "rgba(148,163,184,.14)");
      svg.appendChild(line);
      var txt = document.createElementNS(ns, "text");
      txt.setAttribute("x", padL - 6); txt.setAttribute("y", gy + 3);
      txt.setAttribute("text-anchor", "end");
      txt.setAttribute("fill", "#8ea0b5"); txt.setAttribute("font-size", "9");
      txt.textContent = price.toFixed(price >= 1000 ? 0 : 2);
      svg.appendChild(txt);
    }
    candles.forEach(function (c, i) {
      var x = padL + i * step + (step - bw) / 2;
      var up = Number(c.close) >= Number(c.open);
      var color = up ? "#10b981" : "#f43f5e";
      var wick = document.createElementNS(ns, "line");
      wick.setAttribute("x1", x + bw / 2); wick.setAttribute("x2", x + bw / 2);
      wick.setAttribute("y1", y(Number(c.high))); wick.setAttribute("y2", y(Number(c.low)));
      wick.setAttribute("stroke", color); wick.setAttribute("stroke-width", "1");
      svg.appendChild(wick);
      var body = document.createElementNS(ns, "rect");
      var top = y(Math.max(Number(c.open), Number(c.close)));
      var bot = y(Math.min(Number(c.open), Number(c.close)));
      body.setAttribute("x", x); body.setAttribute("y", top);
      body.setAttribute("width", bw);
      body.setAttribute("height", Math.max(1, bot - top));
      body.setAttribute("fill", up ? "rgba(16,185,129,.85)" : "rgba(244,63,94,.85)");
      svg.appendChild(body);
      // volume lane
      var vh = (Number(c.volume) || 0) / maxVol * volH;
      var vol = document.createElementNS(ns, "rect");
      vol.setAttribute("x", x); vol.setAttribute("width", bw);
      vol.setAttribute("y", H - padB - vh); vol.setAttribute("height", Math.max(0.5, vh));
      vol.setAttribute("fill", "rgba(56,189,248,.35)");
      svg.appendChild(vol);
    });

    var vertical = document.createElementNS(ns, "line");
    vertical.setAttribute("class", "chart-crosshair");
    vertical.setAttribute("x1", padL); vertical.setAttribute("x2", padL);
    vertical.setAttribute("y1", padT); vertical.setAttribute("y2", padT + priceH);
    vertical.setAttribute("visibility", "hidden");
    vertical.setAttribute("pointer-events", "none");
    svg.appendChild(vertical);
    var horizontal = document.createElementNS(ns, "line");
    horizontal.setAttribute("class", "chart-crosshair chart-crosshair-horizontal");
    horizontal.setAttribute("x1", padL); horizontal.setAttribute("x2", W - padR);
    horizontal.setAttribute("y1", padT); horizontal.setAttribute("y2", padT);
    horizontal.setAttribute("visibility", "hidden");
    horizontal.setAttribute("pointer-events", "none");
    svg.appendChild(horizontal);

    var readoutHint = "Move over a chart to inspect the recorded bar across all panels.";
    var readout = Arena.el("div", { "class": "chart-hover-readout", "aria-live": "off" },
      [document.createTextNode(readoutHint)]);
    var first = candles[0], last = candles[candles.length - 1];
    var cap = Arena.el("div", { "class": "faint chart-caption" }, [
      document.createTextNode(label + " · " + candles.length + " recorded bars · " +
        (first.ts || "") + " → " + (last.ts || "") + " · OBSERVED only")]);
    container.appendChild(svg);
    container.appendChild(readout);
    container.appendChild(cap);

    function formatPrice(value) {
      var number = Number(value);
      return Number.isFinite(number) ? number.toFixed(Math.abs(number) >= 10000 ? 0 : 2) : "—";
    }
    var chart = { container: container, svg: svg, candles: candles, label: label,
      padL: padL, padT: padT, priceH: priceH, step: step,
      vertical: vertical, horizontal: horizontal, readout: readout,
      readoutHint: readoutHint, formatPrice: formatPrice };
    chartRegistry[panelIndex] = chart;

    svg.addEventListener("pointermove", function (event) {
      var rect = svg.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      var x = (event.clientX - rect.left) * W / rect.width;
      if (x < padL || x > W - padR) { clearSharedCrosshair(); return; }
      var index = Math.max(0, Math.min(candles.length - 1,
        Math.floor((x - padL) / step)));
      var yPosition = (event.clientY - rect.top) * H / rect.height;
      var yFraction = Math.max(0, Math.min(1, (yPosition - padT) / priceH));
      syncSharedCrosshair(candles[index].ts || String(index), index,
        candles.length, yFraction);
    });
    svg.addEventListener("pointerleave", clearSharedCrosshair);
    svg.addEventListener("pointercancel", clearSharedCrosshair);
  }

  function toggleFocusMode() {
    document.body.classList.toggle("axiom-focus");
    Arena.toast("ok", document.body.classList.contains("axiom-focus")
      ? "Focus mode on — press F to restore the shell." : "Focus mode off.");
  }

  // =====================================================================
  // MODELS — Kronos studio
  // =====================================================================
  function viewModels(root) {
    root.appendChild(Arena.el("h1", { "class": "page-title" }, [document.createTextNode("Models — Kronos Studio")]));
    root.appendChild(Arena.el("p", { "class": "page-sub" }, [document.createTextNode(
      "DOWNLOAD → VERIFY → INSTALL → BENCHMARK → ACTIVATE. Kronos stays isolated from execution: it forecasts; the risk layer decides. Every status is reported honestly — no weights means no claims.")]));
    var studioHost = Arena.el("div", { id: "model-studio" }, [document.createTextNode("Loading…")]);
    root.appendChild(card("Model pipeline", studioHost));
    startStatusPolling();
    renderStudio(studioHost);
  }

  function renderStudio(studioHost) {
    Arena.get("/api/kronos/studio").then(function (d) {
      studioHost.innerHTML = "";
      studioHost.appendChild(Arena.el("div", { "class": "btn-row", style: "margin-bottom:8px" }, [
        Arena.el("span", { "class": "faint" }, [document.createTextNode("cache root: " + d.cache_root)]),
        Arena.el("span", { "class": "faint" }, [document.createTextNode("active model: " + (d.active_model || "none"))])
      ]));
      var grid = Arena.el("div", { "class": "grid cols-3", style: "gap:10px" });
      (d.cards || []).forEach(function (m) {
        var c = Arena.el("div", { "class": "card", style: "margin:0" }, [
          Arena.el("div", { "class": "card-head" }, [
            Arena.el("h3", { style: "font-size:13px" }, [document.createTextNode(m.model_id)]),
            Arena.stateBadge(m.lifecycle)
          ]),
          Arena.el("div", { "class": "card-body", style: "padding:10px" }, [
            Arena.el("div", { "class": "faint", style: "font-size:11.5px;margin-bottom:6px" }, [
              document.createTextNode((m.context_length || "?") + " ctx · ~" + m.parameters_approx + " params · tokenizer " + m.tokenizer_id)]),
            Arena.el("div", { "class": "hint " + ((m.integrity && m.integrity.ok) ? "" : "faint"), style: "font-size:11px" }, [
              document.createTextNode((m.integrity && m.integrity.ok) ? "integrity VERIFIED" : "integrity NOT VERIFIED")])
          ])
        ]);
        var actions = Arena.el("div", { "class": "btn-row", style: "flex-wrap:wrap;padding:0 10px 10px" });
        [["Download", "download"], ["Verify", "verify"], ["Install", "install"], ["Benchmark", "benchmark"], ["Activate", "activate"]].forEach(function (step) {
          var b = Arena.el("button", { "class": "btn ghost sm", id: "kronos-" + step[1] + "-" + m.model_id.replace(/[^A-Za-z0-9.-]/g, "_") }, [document.createTextNode(step[0])]);
          b.addEventListener("click", function () {
            b.disabled = true; b.textContent = "Working…";
            Arena.post("/api/kronos/studio/" + step[1], { model_id: m.model_id }).then(function (r) {
              Arena.toast(r.ok ? "ok" : "err", (r.status || "done") + (r.detail ? " — " + r.detail : ""));
              renderStudio(studioHost);
            }).catch(function (e) { Arena.toast("err", e.message); renderStudio(studioHost); });
          });
          actions.appendChild(b);
        });
        c.appendChild(actions);
        if (m.notes && m.notes.length) {
          c.appendChild(Arena.el("div", { "class": "faint", style: "font-size:10.5px;padding:0 10px 10px" }, [document.createTextNode(m.notes.join(" · "))]));
        }
        grid.appendChild(c);
      });
      studioHost.appendChild(grid);
      studioHost.appendChild(Arena.el("p", { "class": "hint faint", style: "font-size:11.5px;margin-top:8px" }, [document.createTextNode(d.note)]));
    }).catch(function (e) { studioHost.innerHTML = ""; studioHost.appendChild(Arena.el("span", { "class": "faint" }, [document.createTextNode(e.message)])); });
  }

  // =====================================================================
  // TRADING — paper controls + LIVE state machine + orders
  // =====================================================================
  function viewTrading(root) {
    root.appendChild(Arena.el("h1", { "class": "page-title" }, [document.createTextNode("Trading")]));
    root.appendChild(Arena.el("p", { "class": "page-sub" }, [document.createTextNode(
      "Paper execution, the live-trading state machine and the order ledger. Live never auto-activates: OFF → CONFIGURED → READY → ARMED → ACTIVE, each transition explicit.")]));
    root.appendChild(liveSwitchCard());

    var liveHost = Arena.el("div", { id: "live-panel" }, [document.createTextNode("Loading…")]);
    root.appendChild(card("Live trading session", liveHost));

    var readinessHost = Arena.el("div", { id: "readiness-host", "class": "section-gap" });
    root.appendChild(card("Readiness center", readinessHost));

    var summaryHost = Arena.el("div", { id: "summary-host", "class": "section-gap" });
    root.appendChild(card("Start confirmation summary", summaryHost));

    var emergencyHost = Arena.el("div", { "class": "section-gap" });
    root.appendChild(card("Emergency controls", emergencyHost));

    var execInner = Arena.el("div", { id: "exec-inner", "class": "section-gap" });
    root.appendChild(execInner);

    renderLivePanel(liveHost);
    renderReadiness(readinessHost);
    renderStartSummary(summaryHost);
    renderEmergency(emergencyHost);
    viewExecution(execInner);
    startStatusPolling();
  }

  // The PAPER/LIVE control on the Trading screen. It never changes the mode
  // by itself: LIVE opens the activation wizard, PAPER opens the safe
  // drain flow. The truth about the current mode is always shown next to it.
  function liveSwitchCard() {
    var host = Arena.el("div", { id: "live-switch-card" }, [document.createTextNode("Loading…")]);
    renderLiveSwitchCard(host);
    return card("PAPER / LIVE switch", host);
  }

  function renderLiveSwitchCard(host) {
    if (!host) return;
    var position = (window.ArenaLive && window.ArenaLive.position) ? window.ArenaLive.position() : "PAPER";
    host.innerHTML = "";
    var head = Arena.el("div", { "class": "btn-row", style: "align-items:center;flex-wrap:wrap" }, [
      Arena.el("span", { "class": "mode-switch", id: "live-switch-card-toggle" }, [
        Arena.el("span", { "class": "mode-seg paper" + (position === "PAPER" ? " active" : ""),
          id: "card-mode-paper" }, [document.createTextNode("PAPER")]),
        Arena.el("span", { "class": "mode-seg live" + (position !== "PAPER" ? " active" : ""),
          id: "card-mode-live" }, [document.createTextNode("LIVE")])
      ]),
      Arena.el("span", { "class": "faint", id: "live-switch-card-note" }, [document.createTextNode(
        position === "LIVE_ACTIVE" ? "A supervised live engine is running with real money."
          : (position === "LIVE_CONFIGURED" ? "The live configuration is installed; no live engine is authorized."
            : "Live trading is not enabled. Orders are simulated."))])
    ]);
    host.appendChild(head);
    var row = Arena.el("div", { "class": "btn-row", style: "margin-top:12px;flex-wrap:wrap" });
    var liveBtn = Arena.el("button", { "class": "btn live", id: "card-open-wizard" },
      [document.createTextNode("ACTIVATE LIVE TRADING")]);
    liveBtn.addEventListener("click", function () {
      if (window.ArenaLive) window.ArenaLive.openWizard(0);
    });
    row.appendChild(liveBtn);
    var paperBtn = Arena.el("button", { "class": "btn ghost", id: "card-open-switch-off" },
      [document.createTextNode("RETURN TO PAPER")]);
    paperBtn.addEventListener("click", function () {
      if (window.ArenaLive) window.ArenaLive.openSwitchOff();
    });
    row.appendChild(paperBtn);
    host.appendChild(row);
    host.appendChild(Arena.el("p", { "class": "hint faint", style: "font-size:11.5px;margin-top:8px" }, [
      document.createTextNode(
        "Activating live trading is a four-step workflow: connect the broker account, state the hard risk " +
        "limits, review the readiness contract, then confirm with the exact phrase. Nothing is enabled by " +
        "clicking the switch, and no restart or background task can enable it.")]));
  }

  function renderLivePanel(host) {
    if (!host) return;
    Promise.all([Arena.get("/api/live/session"), Arena.get("/api/live/rate-limit"),
      Arena.get("/api/live/session/history")])
      .then(function (results) {
        var d = results[0], rl = results[1], hist = results[2];
        host.innerHTML = "";
        var head = Arena.el("div", { "class": "btn-row", style: "align-items:center;flex-wrap:wrap" }, [
          Arena.stateBadge(d.state || "OFF"),
          Arena.el("span", { "class": "faint" }, [document.createTextNode(
            (d.emergency_stop_active ? "EMERGENCY STOP ACTIVE · " : "") +
            "configuration: " + (d.derived_configuration_state || "—") +
            (d.reason ? " · " + d.reason : ""))]),
          Arena.el("span", { "class": "faint mono" }, [document.createTextNode(
            "rate profile " + rl.profile + " · recent " + rl.current.per_min + "/min, " + rl.current.per_sec + "/s")])
        ]);
        host.appendChild(head);

        var controls = Arena.el("div", { "class": "btn-row", style: "flex-wrap:wrap;margin-top:10px" });
        function liveBtn(label, path, opts) {
          opts = opts || {};
          var b = Arena.el("button", { "class": "btn sm " + (opts.kind || "ghost"), id: "live-" + path.replace(/[^a-z]/g, "-") }, [document.createTextNode(label)]);
          b.addEventListener("click", function () {
            var body = {};
            if (opts.prompt) {
              var phrase = window.prompt(opts.prompt);
              if (phrase === null) return;
              body.confirmation_phrase = phrase;
            }
            if (opts.reasonPrompt) {
              var reason = window.prompt(opts.reasonPrompt, "manual stop from GUI");
              if (reason === null) return;
              body.reason = reason;
            }
            b.disabled = true;
            Arena.post("/api/live/session/" + path, body).then(function (r) {
              Arena.toast("ok", "Live session is now " + r.state + (r.reason ? " — " + r.reason : ""));
              renderLivePanel(host); refreshLiveChip();
            }).catch(function (e) { Arena.toast("err", e.message); b.disabled = false; });
          });
          controls.appendChild(b);
        }
        liveBtn("Arm", "arm", { kind: "primary", prompt: "Arming live trading requires the readiness contract to pass.\nEnter the configured LIVE confirmation phrase:" });
        liveBtn("Activate", "activate", { kind: "danger" });
        liveBtn("Pause", "pause");
        liveBtn("Resume", "resume");
        liveBtn("Stop", "stop", { reasonPrompt: "Why is the live session being stopped?" });
        liveBtn("Disarm", "disarm");
        host.appendChild(controls);

        // Consequences, in plain language (spec §90): every consequential
        // control states what it does, what changes, what can go wrong and
        // how to recover.
        var edu = Arena.el("div", { "class": "section-gap", style: "margin-top:10px" }, [
          Arena.el("dl", { "class": "kv" }, [
            Arena.el("dt", {}, [document.createTextNode("Arm")]),
            Arena.el("dd", {}, [document.createTextNode(
              "Checks the full readiness contract and, only if it passes with your confirmation phrase, " +
              "moves the session to ARMED. No orders yet. Undo: Disarm.")]),
            Arena.el("dt", {}, [document.createTextNode("Activate")]),
            Arena.el("dd", {}, [document.createTextNode(
              "Starts live order flow from an ARMED session — real money at the broker. Every order still " +
              "passes the guard chain. Undo: Pause or Stop.")]),
            Arena.el("dt", {}, [document.createTextNode("Stop")]),
            Arena.el("dd", {}, [document.createTextNode(
              "Ends the session; orders already at the broker are reported by reconciliation, not silently " +
              "forgotten. Returning to live requires a fresh Arm cycle.")]),
            Arena.el("dt", {}, [document.createTextNode("Can go wrong")]),
            Arena.el("dd", {}, [document.createTextNode(
              "Stale broker tokens, clock drift or a blocked static IP make readiness BLOCKED — the buttons " +
              "then refuse instead of guessing. Nothing here can arm live trading without you.")])
          ])
        ]);
        host.appendChild(edu);
        host.appendChild(Arena.el("p", { "class": "hint faint", style: "font-size:11.5px;margin-top:6px" }, [
          document.createTextNode(d.note || "Arming requires the full readiness contract; no restart or background job can arm live trading.")]));

        var history = hist.history || [];
        if (history.length) {
          var hwrap = Arena.el("div", { style: "margin-top:10px" }, [
            Arena.el("b", {}, [document.createTextNode("Transition history (most recent first)")]
          )]);
          history.slice(0, 8).forEach(function (h) {
            hwrap.appendChild(Arena.el("div", { "class": "faint mono", style: "font-size:11px" }, [
              document.createTextNode((h.ts || "") + "  " + (h.from_state || "?") + " → " + (h.to_state || "?") + (h.reason ? "  (" + h.reason + ")" : ""))
            ]));
          });
          host.appendChild(hwrap);
        }
      })
      .catch(function (e) { host.innerHTML = ""; host.appendChild(Arena.el("span", { "class": "faint" }, [document.createTextNode(e.message)])); });
  }

  function renderReadiness(host) {
    if (!host) return;
    Arena.get("/api/live/readiness-center").then(function (d) {
      host.innerHTML = "";
      host.appendChild(Arena.el("div", { "class": "btn-row", style: "flex-wrap:wrap" }, [
        Arena.stateBadge(d.verdict || "UNKNOWN"),
        Arena.el("span", { "class": "faint" }, [document.createTextNode(
          (d.counts.ready || 0) + " passed · " + (d.counts.blocked || 0) + " blockers · " +
          (d.counts.warning || 0) + " warnings · " + (d.counts.unknown || 0) + " not verified (never treated as passing)")])
      ]));
      var table = Arena.el("table", { "class": "table", style: "margin-top:8px" });
      table.appendChild(Arena.el("tr", {}, ["Check", "Status", "Detail"].map(function (h) { return Arena.el("th", {}, [document.createTextNode(h)]); })));
      (d.checks || []).forEach(function (c) {
        table.appendChild(Arena.el("tr", {}, [
          Arena.el("td", {}, [document.createTextNode(c.name)]),
          Arena.el("td", {}, [Arena.stateBadge(c.status)]),
          Arena.el("td", { "class": "faint", style: "font-size:12px" }, [document.createTextNode(c.detail)])
        ]));
      });
      host.appendChild(table);
      host.appendChild(Arena.el("div", { "class": "hint", style: "font-size:12px;margin-top:6px" }, [
        document.createTextNode(d.verdict === "BLOCKED"
          ? "ARMING BLOCKED — resolve the items above before live trading can be armed."
          : "Readiness currently permits arming (confirmation phrase still required).")
      ]));
    }).catch(function (e) { host.innerHTML = ""; host.appendChild(Arena.el("span", { "class": "faint" }, [document.createTextNode(e.message)])); });
  }

  function renderStartSummary(host) {
    if (!host) return;
    Arena.get("/api/live/start-summary").then(function (d) {
      host.innerHTML = "";
      var summary = d.summary || d;
      if (Array.isArray(summary)) {
        summary.forEach(function (line) {
          host.appendChild(Arena.el("div", { style: "font-size:12.5px;margin:2px 0" }, [document.createTextNode(String(typeof line === "object" ? JSON.stringify(line) : line))]));
        });
      } else {
        var pre = Arena.el("pre", { style: "white-space:pre-wrap;font-size:12px" }, [document.createTextNode(JSON.stringify(summary, null, 2))]);
        host.appendChild(pre);
      }
      host.appendChild(Arena.el("p", { "class": "hint faint", style: "font-size:11.5px" }, [
        document.createTextNode("This summary is shown again at arm time — nothing hidden between this view and activation.")]));
    }).catch(function (e) { host.innerHTML = ""; host.appendChild(Arena.el("span", { "class": "faint" }, [document.createTextNode(e.message)])); });
  }

  function renderEmergency(host) {
    if (!host) return;
    host.innerHTML = "";
    var row = Arena.el("div", { "class": "btn-row", style: "flex-wrap:wrap" });
    var emergencyActions = [
      ["GLOBAL EMERGENCY STOP", "/api/live/emergency-stop",
       { reason: "emergency stop from GUI" },
       "What it does: persists the global emergency-stop flag, invalidates live authorization, and asks the supervised engine to cancel working orders. " +
       "What changes: new risk is blocked immediately; the engine stays alive for risk-reducing exits until the live book is reconciled flat. " +
       "What can go wrong: a broker/network failure can leave cancellation unconfirmed, which remains visible and keeps the child draining. " +
       "Positions are never auto-closed. Recovery requires the exact configured confirmation phrase, then a fresh ARM and ACTIVATE."],
      ["Clear emergency stop (fresh ARM required)", "/api/live/emergency-stop/clear", {},
       "What it does: clears the persisted emergency flag only after the exact configured confirmation phrase. " +
       "What changes: this does not resume a session or restore an old authorization epoch; the session remains STOPPED. " +
       "Recovery requires fresh ARM and ACTIVATE after readiness passes.", "clear"],
      ["Pause live (new orders)", "/api/live/session/pause", {},
       "What it does: live order submission stops immediately. " +
       "What changes: the session moves to PAUSED; positions and already-submitted orders are untouched. " +
       "What can go wrong: open orders keep working at the broker while paused — watch them. " +
       "Recovery: Resume from this panel; nothing is lost."],
      ["Stop live session", "/api/live/session/stop", { reason: "emergency stop from GUI" },
       "What it does: ends the live session (STOPPED), invalidates its entry epoch, and asks the supervised child to drain safely. " +
       "What changes: the child requests cancellation through the live gateway and continues risk-reducing management until flat. " +
       "What can go wrong: unconfirmed orders or open positions keep the child and its lease alive; positions are not auto-closed. " +
       "Recovery: re-arm after readiness passes; the child must finish draining first."],
      ["Stop paper engine (cancels open orders)", "/paper/stop", {},
       "What it does: stops the paper engine and cancels open paper orders. " +
       "What changes: simulated trading halts until the engine is started again; the journal is preserved. " +
       "What can go wrong: nothing financial — paper only. " +
       "Recovery: start the engine again from the dashboard; state is restored from the journal."]
    ];
    emergencyActions.forEach(function (action) {
      var b = Arena.el("button", { "class": "btn sm danger", id: "emg-" + action[0].replace(/[^a-z0-9]+/g, "-").toLowerCase() }, [document.createTextNode(action[0])]);
      b.addEventListener("click", function () {
        Arena.confirmDialog("Emergency action", action[3], "Execute", true, function () {
          var body = Object.assign({}, action[2] || {});
          if (action[4] === "clear") {
            var phrase = window.prompt(
              "Enter the exact configured LIVE confirmation phrase to clear the emergency stop:");
            if (phrase === null) return;
            body.confirmation_phrase = phrase;
          }
          b.disabled = true;
          Arena.post(action[1], body).then(function (r) {
            Arena.toast("ok", "Executed: " + (r.state || r.message || "done"));
            refreshLiveChip();
            renderLivePanel(document.getElementById("live-panel"));
            renderEmergency(host);
          }).catch(function (e) {
            Arena.toast("err", e.message);
            b.disabled = false;
          });
        });
      });
      row.appendChild(b);
    });
    host.appendChild(row);
    host.appendChild(Arena.el("p", { "class": "hint faint", style: "font-size:11.5px;margin-top:6px" }, [
      document.createTextNode("Emergency paths are independent of the strategy loop: they act on broker/engine state directly and never require a model to be running.")]));
  }

  // =====================================================================
  // RISK
  // =====================================================================
  function viewRisk(root) {
    root.appendChild(Arena.el("h1", { "class": "page-title" }, [document.createTextNode("Risk")]));
    root.appendChild(Arena.el("p", { "class": "page-sub" }, [document.createTextNode(
      "Hard limits, utilisation and breaches. Limits are enforced by the engine; this page shows their state — it cannot raise them.")]));
    var host = Arena.el("div", { id: "risk-host" }, [document.createTextNode("Loading…")]);
    root.appendChild(card("Risk limits & breaches", host));
    var guardHost = Arena.el("div", { "class": "section-gap", id: "guard-host" });
    root.appendChild(card("Live guard chain", guardHost));
    startStatusPolling();

    Arena.get("/dashboard/data").then(function (d) {
      host.innerHTML = "";
      var limits = d.risk_limits || {};
      var grid = Arena.el("div", { "class": "grid cols-4" });
      [["Max drawdown", limits.max_drawdown], ["Max daily loss", limits.max_daily_loss],
       ["Max trades/day", limits.max_trades_per_day], ["Max position size", limits.max_position_size]].forEach(function (row) {
        grid.appendChild(Arena.el("div", { "class": "card", style: "margin:0" }, [
          Arena.el("div", { "class": "card-body", style: "padding:12px" }, [
            Arena.el("div", { "class": "faint", style: "font-size:11px" }, [document.createTextNode(row[0])]),
            Arena.el("div", { "class": "mono", style: "font-size:16px" }, [document.createTextNode(row[1] === undefined || row[1] === null ? "—" : String(row[1]))])
          ])
        ]));
      });
      host.appendChild(grid);
      host.appendChild(Arena.el("div", { style: "margin-top:10px" }, [
        Arena.stateBadge(d.emergency_stop ? "EMERGENCY-STOP" : "CLEAR"),
        Arena.el("span", { "class": "faint", style: "margin-left:8px" }, [
          document.createTextNode(d.emergency_stop ? ("reason: " + (d.emergency_reason || "—")) : "no emergency stop engaged")])
      ]));
      var breaches = d.breaches || d.risk_breaches || [];
      host.appendChild(Arena.el("div", { style: "margin-top:10px" }, [
        Arena.el("b", {}, [document.createTextNode("Breaches: " + breaches.length)])
      ]));
      if (!breaches.length) host.appendChild(Arena.el("div", { "class": "empty" }, [document.createTextNode("No risk breaches recorded.")]));
      breaches.slice(0, 10).forEach(function (br) {
        host.appendChild(Arena.el("div", { "class": "faint", style: "font-size:12px;margin:2px 0" }, [document.createTextNode(JSON.stringify(br))]));
      });
    }).catch(function (e) { host.innerHTML = ""; host.appendChild(Arena.el("span", { "class": "faint" }, [document.createTextNode(e.message)])); });

    Arena.get("/api/live/guard-chain").then(function (d) {
      guardHost.innerHTML = "";
      var list = Arena.el("div", {});
      (d.guards || []).forEach(function (g, i) {
        list.appendChild(Arena.el("div", { style: "margin:3px 0" }, [
          Arena.el("b", { "class": "mono" }, [document.createTextNode(String(i + 1).padStart(2, "0") + " ")]),
          document.createTextNode(g)
        ]));
      });
      guardHost.appendChild(list);
      guardHost.appendChild(Arena.el("p", { "class": "hint faint", style: "font-size:11.5px;margin-top:6px" }, [
        document.createTextNode(d.note || "Every live ENTRY passes all guards in order; any guard can refuse.")]));
    }).catch(function (e) { guardHost.innerHTML = ""; guardHost.appendChild(Arena.el("span", { "class": "faint" }, [document.createTextNode(e.message)])); });
  }

  // =====================================================================
  // INTELLIGENCE — opportunities, events, missed, next best
  // =====================================================================
  function viewIntelligence(root) {
    root.appendChild(Arena.el("h1", { "class": "page-title" }, [document.createTextNode("Intelligence")]));
    root.appendChild(Arena.el("p", { "class": "page-sub" }, [document.createTextNode(
      "The Opportunity Center, market events and the false-negative lab. Candidates are OBSERVED from recorded market data — never generated filler.")]));

    var toolbar = Arena.el("div", { "class": "btn-row", style: "margin-bottom:10px" }, [
      Arena.el("button", { "class": "btn ghost sm", onclick: function () { viewIntelligenceRefresh(); } }, [document.createTextNode("Refresh")]),
      Arena.el("span", { id: "opp-fresh" })
    ]);
    root.appendChild(toolbar);

    var grid = Arena.el("div", { "class": "grid cols-2" });
    var oppHost = Arena.el("div", { id: "opp-list" }, [document.createTextNode("Loading…")]);
    grid.appendChild(card("Opportunity Center", oppHost));
    var missedHost = Arena.el("div", { id: "missed-list" }, [document.createTextNode("Loading…")]);
    grid.appendChild(card("Missed opportunities (false-negative lab)", missedHost));
    root.appendChild(grid);

    var eventsHost = Arena.el("div", { "class": "section-gap", id: "events-host" });
    root.appendChild(card("Market events", eventsHost));
    var nextHost = Arena.el("div", { "class": "section-gap", id: "next-best-host" });
    root.appendChild(card("Next best experiment (research intelligence)", nextHost));

    refreshOpportunities(oppHost);
    renderMissed(missedHost);
    renderMarketEvents(eventsHost);
    renderNextBest(nextHost);
    startStatusPolling();
  }

  function viewIntelligenceRefresh() {
    var root = document.getElementById("main-view");
    if (root && state.view === "intelligence") { root.innerHTML = ""; viewIntelligence(root); }
  }

  var OPP_TRANSITIONS = [["Watch", "WATCH"], ["Investigate", "INVESTIGATING"],
    ["High conviction", "HIGH-CONVICTION SETUP"], ["Papered", "PAPERED"],
    ["Invalidate", "INVALIDATED"], ["Avoid", "AVOID"]];

  function refreshOpportunities(host) {
    if (!host) return;
    Arena.get("/api/opportunities").then(function (d) {
      host.innerHTML = "";
      var opps = d.opportunities || [];
      host.appendChild(freshnessRow(new Date().toISOString()));
      if (!opps.length) {
        host.appendChild(Arena.el("div", { "class": "empty" }, [document.createTextNode(
          "No candidates right now. Candidates appear when observed moves, volume spikes or regime changes cross the attention profile — the centre never invents them.")]));
        return;
      }
      opps.forEach(function (o) {
        var row = Arena.el("div", { "class": "card", style: "margin:6px 0" }, [
          Arena.el("div", { "class": "card-head" }, [
            Arena.el("b", {}, [document.createTextNode(o.identity)]),
            Arena.stateBadge(o.state),
            Arena.el("span", { "class": "faint" }, [document.createTextNode("age " + o.age_seconds + "s · evidence " + o.evidence_age_seconds + "s")])
          ]),
          Arena.el("div", { "class": "card-body", style: "padding:8px 12px" }, [
            Arena.el("div", { "class": "faint", style: "font-size:12px" }, [
              document.createTextNode("trigger: " + (o.trigger || "—") + "  ·  market: " + o.market_id +
                (o.invalidation_reason ? "  ·  invalidated: " + o.invalidation_reason : ""))]),
            Arena.el("div", { "class": "btn-row", style: "margin-top:6px;flex-wrap:wrap" },
              OPP_TRANSITIONS.map(function (t) {
                var b = Arena.el("button", { "class": "btn ghost sm" }, [document.createTextNode(t[0])]);
                b.addEventListener("click", function () {
                  Arena.post("/api/opportunities/" + encodeURIComponent(o.id) + "/transition", { state: t[1], reason: "user decision" })
                    .then(function () { refreshOpportunities(host); })
                    .catch(function (e) { Arena.toast("err", e.message); });
                });
                return b;
              }))
          ])
        ]);
        host.appendChild(row);
      });
    }).catch(function (e) { host.innerHTML = ""; host.appendChild(Arena.el("span", { "class": "faint" }, [document.createTextNode(e.message)])); });
  }

  function renderMissed(host) {
    Arena.get("/api/research/missed").then(function (d) {
      host.innerHTML = "";
      var items = d.missed || [];
      var stats = d.stats || {};
      host.appendChild(Arena.el("div", { "class": "faint", style: "font-size:11.5px;margin-bottom:6px" }, [
        document.createTextNode("total " + (stats.total || 0) + " · knowable " + (stats.knowable || 0))]));
      if (!items.length) { host.appendChild(Arena.el("div", { "class": "empty" }, [document.createTextNode("No missed opportunities recorded.")])); return; }
      items.slice(0, 12).forEach(function (m) {
        host.appendChild(Arena.el("div", { style: "margin:4px 0" }, [
          Arena.el("b", {}, [document.createTextNode((m.identity || m.market_id || m.kind || "instrument"))]),
          Arena.el("span", { "class": "faint", style: "font-size:12px" }, [
            document.createTextNode(" — " + m.kind + (m.knowable ? " (knowable)" : "") +
              (m.research_question ? " · " + m.research_question : ""))])
        ]));
      });
    }).catch(function (e) { host.innerHTML = ""; host.appendChild(Arena.el("span", { "class": "faint" }, [document.createTextNode(e.message)])); });
  }

  function renderMarketEvents(host) {
    Arena.get("/api/market-events").then(function (d) {
      host.innerHTML = "";
      var events = d.events || d.raw_events || [];
      if (!events.length) { host.appendChild(Arena.el("div", { "class": "empty" }, [document.createTextNode("No market events recorded.")])); return; }
      events.slice(0, 20).forEach(function (ev) {
        host.appendChild(Arena.el("div", { "class": "faint mono", style: "font-size:11.5px;margin:2px 0" }, [
          document.createTextNode((ev.ts || ev.timestamp || "") + "  " + (ev.kind || ev.trigger || "") + "  " + JSON.stringify(ev.payload || ev.data || ev))
        ]));
      });
    }).catch(function (e) { host.innerHTML = ""; host.appendChild(Arena.el("span", { "class": "faint" }, [document.createTextNode(e.message)])); });
  }

  function renderNextBest(host) {
    Arena.get("/api/research/next-best").then(function (d) {
      host.innerHTML = "";
      var suggestions = d.suggestions || [];
      if (!suggestions.length) { host.appendChild(Arena.el("div", { "class": "empty" }, [document.createTextNode("No suggestions yet.")])); return; }
      var table = Arena.el("table", { "class": "table" });
      table.appendChild(Arena.el("tr", {}, ["Hypothesis", "Motivation", "Expected gain", "Budget"].map(function (h) { return Arena.el("th", {}, [document.createTextNode(h)]); })));
      suggestions.forEach(function (s) {
        table.appendChild(Arena.el("tr", {}, [
          Arena.el("td", {}, [document.createTextNode(s.hypothesis)]),
          Arena.el("td", { "class": "faint", style: "font-size:12px" }, [document.createTextNode(s.motivation)]),
          Arena.el("td", { "class": "faint", style: "font-size:12px" }, [document.createTextNode(s.expected_information_gain)]),
          Arena.el("td", {}, [document.createTextNode(s.compute_budget)])
        ]));
      });
      host.appendChild(table);
      host.appendChild(Arena.el("div", { "class": "faint", style: "font-size:11px;margin-top:4px" }, [document.createTextNode(d.note || "")]));
    }).catch(function (e) { host.innerHTML = ""; host.appendChild(Arena.el("span", { "class": "faint" }, [document.createTextNode(e.message)])); });
  }

  // =====================================================================
  // REPORTS
  // =====================================================================
  function viewReports(root) {
    root.appendChild(Arena.el("h1", { "class": "page-title" }, [document.createTextNode("Reports")]));
    root.appendChild(Arena.el("p", { "class": "page-sub" }, [document.createTextNode(
      "Weekly performance report and latency observability. Numbers come from the journal and latency tracker — nothing is synthesised for display.")]));

    var grid = Arena.el("div", { "class": "grid cols-2" });
    var weeklyHost = Arena.el("div", { id: "report-weekly" }, [document.createTextNode("Loading…")]);
    grid.appendChild(card("Weekly report", weeklyHost));
    var latencyHost = Arena.el("div", { id: "report-latency" }, [document.createTextNode("Loading…")]);
    grid.appendChild(card("Latency observability (hot path)", latencyHost));
    root.appendChild(grid);

    Arena.get("/api/v2/report/weekly?format=markdown").then(function (d) {
      weeklyHost.innerHTML = "";
      var text = d.raw || d.report || JSON.stringify(d, null, 2);
      weeklyHost.appendChild(Arena.el("pre", { style: "white-space:pre-wrap;font-size:11.5px;max-height:420px;overflow:auto" }, [document.createTextNode(text)]));
      weeklyHost.appendChild(freshnessRow(new Date().toISOString(), "generated"));
    }).catch(function (e) { weeklyHost.innerHTML = ""; weeklyHost.appendChild(Arena.el("span", { "class": "faint" }, [document.createTextNode(e.message)])); });

    Arena.get("/api/latency").then(function (d) {
      latencyHost.innerHTML = "";
      var l = d.stages || {};
      var stages = Object.keys(l).filter(function (k) { return l[k] && typeof l[k] === "object"; });
      if (!stages.length) { latencyHost.appendChild(Arena.el("div", { "class": "empty" }, [document.createTextNode("No latency samples recorded yet.")])); return; }
      var table = Arena.el("table", { "class": "table" });
      table.appendChild(Arena.el("tr", {}, ["Stage", "P50", "P95", "P99", "P99.9", "samples", "misses"].map(function (h) { return Arena.el("th", {}, [document.createTextNode(h)]); })));
      stages.forEach(function (stage) {
        var s = l[stage];
        table.appendChild(Arena.el("tr", {}, [
          Arena.el("td", {}, [document.createTextNode(stage)]),
          Arena.el("td", {}, [document.createTextNode(s.p50_ms === null || s.p50_ms === undefined ? "—" : String(s.p50_ms))]),
          Arena.el("td", {}, [document.createTextNode(s.p95_ms === null || s.p95_ms === undefined ? "—" : String(s.p95_ms))]),
          Arena.el("td", {}, [document.createTextNode(s.p99_ms === null || s.p99_ms === undefined ? "—" : String(s.p99_ms))]),
          Arena.el("td", {}, [document.createTextNode(s.p999_ms === null || s.p999_ms === undefined ? "—" : String(s.p999_ms))]),
          Arena.el("td", {}, [document.createTextNode(String(s.count || 0))]),
          Arena.el("td", {}, [document.createTextNode(String(s.deadline_misses || 0))])
        ]));
      });
      latencyHost.appendChild(table);
    }).catch(function (e) { latencyHost.innerHTML = ""; latencyHost.appendChild(Arena.el("span", { "class": "faint" }, [document.createTextNode(e.message)])); });
  }

  // =====================================================================
  // SYSTEM control extensions: tasks + backup + research memory
  // =====================================================================
  function renderSystemControl(root) {
    var host = Arena.el("div", { "class": "section-gap" });
    var layoutHost = Arena.el("div", { id: "sys-layouts" });
    host.appendChild(card("Workspace layouts (save · duplicate · reset)", layoutHost));
    renderLayoutManager(layoutHost);
    var tasksHost = Arena.el("div", { id: "sys-tasks" }, [document.createTextNode("Loading…")]);
    host.appendChild(card("Task center (research, backfill, model work)", tasksHost));
    var backupHost = Arena.el("div", { id: "sys-backup" }, [document.createTextNode("Loading…")]);
    host.appendChild(card("Backup & restore", backupHost));
    var updateHost = Arena.el("div", { id: "sys-update" }, [document.createTextNode("Loading…")]);
    host.appendChild(card("Update AXIOM (check → download → verify → prepare → install)", updateHost));
    root.appendChild(host);
    renderTaskCenter(tasksHost);
    renderBackupCenter(backupHost);
    renderUpdateCenter(updateHost);
  }

  // Named workspace layouts (spec §61): persisted bundles of card
  // collapse/maximize preferences plus the chart layout. Built-in presets
  // ship with the product; saved layouts can be duplicated, applied,
  // deleted and reset.
  var BUILTIN_LAYOUTS = {
    "Intraday Hunt": { chart: { layout: 4 }, cards: {} },
    "Research Lab": { chart: { layout: 2 },
      cards: { "model-agreement-disagreement": { maximized: true } } },
    "Kronos Analysis": { chart: { layout: 1 },
      cards: { "model-pipeline": { maximized: true } } },
    "Portfolio": { chart: { layout: 1 },
      cards: { "positions-orders-trades": { maximized: true } } },
    "Presentation": { chart: { layout: 1 },
      cards: { "backup-restore": { collapsed: true },
                 "task-center-research-backfill-model-work": { collapsed: true },
                 "update-axiom-check-download-verify-prepare-install": { collapsed: true } } }
  };

  function savedLayouts() {
    try { return JSON.parse(localStorage.getItem("axiom_named_layouts")) || {}; }
    catch (e) { return {}; }
  }
  function storeLayouts(layouts) {
    localStorage.setItem("axiom_named_layouts", JSON.stringify(layouts));
  }
  function applyNamedLayout(name) {
    var builtins = BUILTIN_LAYOUTS;
    var layout = savedLayouts()[name] || builtins[name];
    if (!layout) { Arena.toast("err", "Layout " + name + " does not exist."); return false; }
    if (layout.chart && [1, 2, 4, 6, 8].indexOf(layout.chart.layout) >= 0) {
      var cfg = chartConfig();
      cfg.layout = layout.chart.layout;
      while (cfg.panels.length < cfg.layout) cfg.panels.push({ identity: "", unit: "days", interval: 1 });
      saveChartConfig(cfg);
    }
    var prefs = cardPrefs();
    Object.keys(layout.cards || {}).forEach(function (slug) {
      prefs[slug] = Object.assign({}, prefs[slug], layout.cards[slug]);
    });
    saveCardPrefs(prefs);
    localStorage.setItem("axiom_active_layout", name);
    Arena.toast("ok", "Layout applied: " + name + " (re-open pages to see every change).");
    show(state.view);
    return true;
  }

  function renderLayoutManager(host) {
    function paint() {
      host.innerHTML = "";
      host.appendChild(Arena.el("p", { "class": "muted", style: "font-size:11.5px" }, [document.createTextNode(
        "A layout bundles card collapse/maximize preferences and the chart grid. Layouts persist in this " +
        "browser; Reset restores the built-in presets and clears saved ones.")]));
      var active = localStorage.getItem("axiom_active_layout") || "";
      var names = Object.keys(BUILTIN_LAYOUTS).concat(Object.keys(savedLayouts()));
      var list = Arena.el("div");
      names.forEach(function (name) {
        var isBuiltin = Object.prototype.hasOwnProperty.call(BUILTIN_LAYOUTS, name);
        var applyBtn = Arena.el("button", { "class": "btn ghost sm" }, [document.createTextNode("Apply")]);
        applyBtn.addEventListener("click", function () { applyNamedLayout(name); });
        var dupBtn = Arena.el("button", { "class": "btn ghost sm" }, [document.createTextNode("Duplicate")]);
        dupBtn.addEventListener("click", function () {
          var layouts = savedLayouts();
          var source = layouts[name] || BUILTIN_LAYOUTS[name];
          var copyName = name + " copy"; var i = 2;
          while (layouts[copyName] || BUILTIN_LAYOUTS[copyName]) { copyName = name + " copy " + (i++); }
          layouts[copyName] = JSON.parse(JSON.stringify(source));
          storeLayouts(layouts); paint();
        });
        var row = Arena.el("div", { style: "display:flex;gap:8px;align-items:center;margin:4px 0;flex-wrap:wrap" }, [
          Arena.el("span", { "class": "mono", style: "font-size:11.5px" }, [
            document.createTextNode(name + (isBuiltin ? " (built-in)" : "") + (name === active ? " · active" : ""))]),
          Arena.el("span", { style: "flex:1" }), applyBtn, dupBtn]);
        if (!isBuiltin) {
          var delBtn = Arena.el("button", { "class": "btn ghost sm" }, [document.createTextNode("Delete")]);
          delBtn.addEventListener("click", function () {
            var layouts = savedLayouts(); delete layouts[name]; storeLayouts(layouts); paint();
          });
          row.appendChild(delBtn);
        }
        list.appendChild(row);
      });
      host.appendChild(list);

      var nameInput = Arena.el("input", { id: "layout-new-name", placeholder: "e.g. My morning desk", style: "min-width:220px" });
      var saveBtn = Arena.el("button", { "class": "btn primary sm", id: "layout-save" }, [document.createTextNode("Save current as…")]);
      saveBtn.addEventListener("click", function () {
        var name = nameInput.value.trim();
        if (!name) { Arena.toast("err", "Give the layout a name first."); return; }
        var layouts = savedLayouts();
        layouts[name] = { chart: { layout: chartConfig().layout }, cards: cardPrefs() };
        storeLayouts(layouts);
        localStorage.setItem("axiom_active_layout", name);
        nameInput.value = "";
        Arena.toast("ok", "Saved layout: " + name);
        paint();
      });
      var resetBtn = Arena.el("button", { "class": "btn ghost sm", id: "layout-reset" }, [document.createTextNode("Reset to defaults")]);
      resetBtn.addEventListener("click", function () {
        Arena.confirmDialog("Reset workspace layouts",
          "What it does: deletes every saved layout and clears card preferences. " +
          "What changes: the workspace returns to the built-in presets. " +
          "What can go wrong: nothing — saved layouts are only display preferences. " +
          "Recovery: re-create saved layouts; built-in presets are never removed.",
          "Reset", true, function () {
          storeLayouts({});
          localStorage.removeItem("axiom_card_prefs");
          localStorage.removeItem("axiom_active_layout");
          Arena.toast("ok", "Workspace reset to defaults.");
          show(state.view); paint();
        });
      });
      host.appendChild(Arena.el("div", { "class": "btn-row", style: "flex-wrap:wrap;margin-top:8px" }, [nameInput, saveBtn, resetBtn]));
    }
    paint();
  }

  function renderUpdateCenter(host) {
    function paint(extra) {
      Arena.get("/api/system/update/status").then(function (d) {
        host.innerHTML = "";
        host.appendChild(Arena.el("div", { "class": "faint", style: "font-size:12px" }, [
          document.createTextNode("running version " + d.current +
            (d.manifest ? " · staged: " + d.manifest.artifact + " (" + d.manifest.status + ")"
                        : " · no update staged"))]
        ));
        if (extra) host.appendChild(Arena.el("div", { "class": "hint", style: "font-size:12px;margin:4px 0" }, [document.createTextNode(extra)]));
        var row = Arena.el("div", { "class": "btn-row", style: "margin-top:8px;flex-wrap:wrap" });
        var checkBtn = Arena.el("button", { "class": "btn ghost sm", id: "update-check" }, [document.createTextNode("Check for update")]);
        checkBtn.addEventListener("click", function () {
          checkBtn.disabled = true;
          Arena.get("/api/system/update/check").then(function (r) {
            paint(r.status + (r.latest ? " — latest " + r.latest + " (current " + r.current + ")" : "") + (r.detail ? " — " + r.detail : ""));
          }).catch(function (e) { paint("check failed: " + e.message); });
        });
        row.appendChild(checkBtn);
        var prepBtn = Arena.el("button", { "class": "btn ghost sm", id: "update-install" }, [document.createTextNode("Install staged update")]);
        prepBtn.addEventListener("click", function () {
          Arena.confirmDialog("Install update",
            "What it does: re-checks the staged artifact's sha256, backs up the current executable (.previous) and replaces it. " +
            "What changes: the next launch runs the new version; a restart is required. " +
            "What can go wrong: if the OS refuses to replace a running binary, the install is reported REFUSED and the verified artifact stays staged — nothing half-written. " +
            "Recovery: the previous executable is preserved as .previous.", "Install", true, function () {
            Arena.post("/api/system/update/install", {}).then(function (r) {
              Arena.toast(r.ok ? "ok" : "err", r.status + (r.detail ? " — " + r.detail : ""));
              paint();
            }).catch(function (e) { Arena.toast("err", e.message); });
          });
        });
        row.appendChild(prepBtn);
        host.appendChild(row);
        host.appendChild(Arena.el("p", { "class": "hint faint", style: "font-size:11.5px;margin-top:6px" }, [
          document.createTextNode("Downloads land in data/updates/ and are verified by SHA-256 before install; nothing here executes downloaded code.")]));
      }).catch(function (e) { host.innerHTML = ""; host.appendChild(Arena.el("span", { "class": "faint" }, [document.createTextNode(e.message)])); });
    }
    paint();
  }

  function renderTaskCenter(host) {
    Arena.get("/api/tasks").then(function (d) {
      host.innerHTML = "";
      var counts = d.counts || {};
      host.appendChild(Arena.el("div", { "class": "faint", style: "font-size:12px;margin-bottom:6px" }, [
        document.createTextNode("RUNNING " + (counts.RUNNING || 0) + " · PENDING " + (counts.PENDING || 0) +
          " · PAUSED " + (counts.PAUSED || 0) + " · COMPLETED " + (counts.COMPLETED || 0) +
          " · CANCELLED " + (counts.CANCELLED || 0) + " · FAILED " + (counts.FAILED || 0))
      ]));
      var tasks = d.tasks || [];
      if (!tasks.length) { host.appendChild(Arena.el("div", { "class": "empty" }, [document.createTextNode("No tasks recorded.")])); return; }
      var table = Arena.el("table", { "class": "table" });
      table.appendChild(Arena.el("tr", {}, ["Name", "Type", "State", "Progress", "Actions"].map(function (h) { return Arena.el("th", {}, [document.createTextNode(h)]); })));
      tasks.slice(0, 20).forEach(function (t) {
        var actions = Arena.el("div", { "class": "btn-row" });
        [["start", "PENDING"], ["pause", "RUNNING"], ["resume", "PAUSED"], ["cancel", null]].forEach(function (act) {
          if (act[1] && t.state !== act[1]) return;
          if (act[0] === "cancel" && ["COMPLETED", "FAILED", "CANCELLED"].indexOf(t.state) >= 0) return;
          var b = Arena.el("button", { "class": "btn ghost sm" }, [document.createTextNode(act[0])]);
          b.addEventListener("click", function () {
            Arena.post("/api/tasks/" + encodeURIComponent(t.id), { action: act[0] })
              .then(function () { renderTaskCenter(host); })
              .catch(function (e) { Arena.toast("err", e.message); });
          });
          actions.appendChild(b);
        });
        table.appendChild(Arena.el("tr", {}, [
          Arena.el("td", {}, [document.createTextNode(t.name)]),
          Arena.el("td", {}, [document.createTextNode(t.type)]),
          Arena.el("td", {}, [Arena.stateBadge(t.state)]),
          Arena.el("td", {}, [document.createTextNode(t.progress !== undefined && t.progress !== null ? Math.round(t.progress * 100) + "%" : "—")]),
          Arena.el("td", {}, [actions])
        ]));
      });
      host.appendChild(table);
    }).catch(function (e) { host.innerHTML = ""; host.appendChild(Arena.el("span", { "class": "faint" }, [document.createTextNode(e.message)])); });
  }

  function renderBackupCenter(host) {
    function paint() {
      Arena.get("/api/backup/list").then(function (d) {
        host.innerHTML = "";
        var row = Arena.el("div", { "class": "btn-row" }, [
          Arena.el("button", { "class": "btn primary sm", id: "backup-create" }, [document.createTextNode("Create backup")])
        ]);
        host.appendChild(row);
        document.getElementById("backup-create").addEventListener("click", function () {
          Arena.post("/api/backup/create", {}).then(function (r) {
            Arena.toast("ok", "Backup created: " + String(r.path || "").split("/").pop()); paint();
          }).catch(function (e) { Arena.toast("err", e.message); });
        });
        var backups = d.backups || [];
        if (!backups.length) { host.appendChild(Arena.el("div", { "class": "empty" }, [document.createTextNode("No backups yet.")])); return; }
        backups.forEach(function (b) {
          var verifyBtn = Arena.el("button", { "class": "btn ghost sm" }, [document.createTextNode("Verify")]);
          var restoreBtn = Arena.el("button", { "class": "btn ghost sm" }, [document.createTextNode("Restore")]);
          var item = Arena.el("div", { style: "display:flex;gap:8px;align-items:center;margin:4px 0;flex-wrap:wrap" }, [
            Arena.el("span", { "class": "mono", style: "font-size:11.5px" }, [document.createTextNode(
              String(b.path || "").split("/").pop() + " · " + (b.bytes || 0) + " B · " + (b.modified_utc || ""))]),
            Arena.el("span", { style: "flex:1" }),
            verifyBtn, restoreBtn
          ]);
          verifyBtn.addEventListener("click", function () {
            Arena.post("/api/backup/verify", { path: b.path }).then(function (r) {
              Arena.toast(r.ok ? "ok" : "err", r.ok ? "Backup verified: archive + integrity refs OK." : "Verification FAILED: " + (r.errors || []).join("; "));
            }).catch(function (e) { Arena.toast("err", e.message); });
          });
          restoreBtn.addEventListener("click", function () {
            Arena.confirmDialog("Restore backup",
              "What it does: verifies this archive, then swaps in its database. " +
              "What changes: journals, universes and settings revert to the backup's state — everything recorded after it is replaced. " +
              "What can go wrong: if the OS keeps the database locked, AXIOM stages the verified file and reports RESTORE_PREPARED_RESTART_REQUIRED instead of a fake success. " +
              "How to recover: the previous database is preserved alongside (.pre-restore) for rollback.",
              "Restore", true, function () {
              Arena.post("/api/backup/restore", { path: b.path }).then(function (r) {
                if (r.restored) {
                  Arena.toast("ok", "Restored from verified backup; previous database preserved for rollback.");
                } else if (r.status === "RESTORE_PREPARED_RESTART_REQUIRED") {
                  Arena.toast("err", r.note || "Verified database staged; apply it with AXIOM stopped.");
                } else {
                  Arena.toast("err", (r.note || "Restore refused") + " — active state untouched.");
                }
              }).catch(function (e) { Arena.toast("err", e.message); });
            });
          });
          host.appendChild(item);
        });
      }).catch(function (e) { host.innerHTML = ""; host.appendChild(Arena.el("span", { "class": "faint" }, [document.createTextNode(e.message)])); });
    }
    paint();
  }

  // Research memory card appended to the existing Research view.
  function renderResearchMemory(root) {
    var memHost = Arena.el("div", { id: "research-memory" }, [document.createTextNode("Loading…")]);
    root.appendChild(Arena.el("div", { "class": "section-gap" }, [card("Research memory (recorded findings & lineage)", memHost)]));
    Arena.get("/api/research/memory").then(function (d) {
      memHost.innerHTML = "";
      var entries = d.results || d.entries || [];
      var form = Arena.el("div", { "class": "btn-row", style: "margin-bottom:8px;flex-wrap:wrap" }, [
        Arena.el("input", { id: "mem-add-summary", placeholder: "Record a finding… (hypothesis + outcome)", style: "min-width:300px" }),
        Arena.el("button", { "class": "btn ghost sm", id: "mem-add" }, [document.createTextNode("Record")])
      ]);
      memHost.appendChild(form);
      document.getElementById("mem-add").addEventListener("click", function () {
        var summary = document.getElementById("mem-add-summary").value.trim();
        if (!summary) return;
        Arena.post("/api/research/memory", { topic: summary, kind: "finding",
          payload: { source: "gui" }, provenance: "operator input via GUI", confidence: 0.5 })
          .then(function () { document.getElementById("mem-add-summary").value = ""; viewResearchRefresh(); })
          .catch(function (e) { Arena.toast("err", e.message); });
      });
      if (!entries.length) { memHost.appendChild(Arena.el("div", { "class": "empty" }, [document.createTextNode("Research memory is empty.")])); return; }
      entries.slice(0, 12).forEach(function (m) {
        memHost.appendChild(Arena.el("div", { style: "margin:4px 0" }, [
          Arena.el("b", { "class": "mono", style: "font-size:11px" }, [document.createTextNode((m.id || "").slice(0, 8) + " ")]),
          document.createTextNode((m.topic || "") + " — " + (m.kind || "") + (m.payload && m.payload.outcome ? " · " + m.payload.outcome : ""))
        ]));
      });
    }).catch(function (e) { memHost.innerHTML = ""; memHost.appendChild(Arena.el("span", { "class": "faint" }, [document.createTextNode(e.message)])); });
  }

  // =====================================================================
  // MODEL AGREEMENT / DISAGREEMENT + COUNTERFACTUAL LAB (spec §98, §99)
  // =====================================================================
  function labField(label, input) {
    return Arena.el("div", { "class": "field" }, [
      Arena.el("label", {}, [document.createTextNode(label)]), input]);
  }
  function labChip(tone, text) {
    return Arena.el("span", { "class": ("badge " + (tone || "")).trim() }, [document.createTextNode(text)]);
  }
  function labSelect(id, options, current) {
    var s = Arena.el("select", { id: id });
    options.forEach(function (o) {
      s.appendChild(Arena.el("option", { value: o[0] }, [document.createTextNode(o[1])]));
    });
    s.value = current;
    return s;
  }

  function renderAgreementCenter(host) {
    host.appendChild(Arena.el("p", { "class": "muted", style: "font-size:11.5px" }, [document.createTextNode(
      "Records the directional stance of each evidence source (strategy, analogues, statistical baseline, " +
      "Kronos, regime-conditioned models) for one instrument and horizon. Disagreement is surfaced as a " +
      "research question — never treated as a failure and never collapsed into one score.")]));
    host.appendChild(Arena.el("p", { "class": "faint", style: "font-size:11px" }, [document.createTextNode(
      "What this changes: one vote is stored in the research ledger (survives restarts). It cannot move money, " +
      "touch risk limits or arm live trading. Wrong votes are corrected by recording a newer vote.")]));

    var identity = Arena.el("input", { id: "agree-identity", placeholder: "e.g. NSE|RELIANCE", value: "NSE|RELIANCE" });
    var horizon = Arena.el("input", { id: "agree-horizon", type: "number", value: "5", min: "1", style: "width:70px" });
    var marketSel = labSelect("agree-market", [["india", "India"], ["us", "US"], ["japan", "Japan"],
      ["uk", "UK"], ["hk", "HK"], ["sg", "Singapore"], ["global", "Global"]], "india");
    var row = Arena.el("div", { "class": "btn-row", style: "flex-wrap:wrap;margin-bottom:8px" }, [
      labField("Instrument", identity), labField("Market", marketSel), labField("Horizon (bars)", horizon)]);
    host.appendChild(row);

    var sourceSel = {};
    var sources = [["STRATEGY", "Strategy model"], ["ANALOGUE", "Historical analogues"],
      ["STATISTICAL_BASELINE", "Statistical baseline"], ["KRONOS", "Kronos forecast"],
      ["REGIME_CONDITIONED", "Regime-conditioned model"]];
    var stanceRow = Arena.el("div", { "class": "grid cols-2", style: "gap:6px;margin-bottom:8px" });
    sources.forEach(function (s) {
      var sel = labSelect("agree-src-" + s[0], [["1", "bullish"], ["0", "no edge"], ["-1", "bearish"]], "0");
      sourceSel[s[0]] = sel;
      stanceRow.appendChild(labField(s[1], sel));
    });
    host.appendChild(stanceRow);

    var recordBtn = Arena.el("button", { "class": "btn primary", id: "agree-record" }, [document.createTextNode("Record stances")]);
    var out = Arena.el("div", { id: "agree-summary", style: "margin-top:8px" });
    host.appendChild(Arena.el("div", { "class": "btn-row" }, [recordBtn]));
    host.appendChild(out);

    recordBtn.addEventListener("click", function () {
      var stances = {};
      sources.forEach(function (s) { stances[s[0]] = parseInt(sourceSel[s[0]].value, 10); });
      Arena.setBusy(recordBtn, true, "Recording…");
      Arena.post("/api/research/agreement", {
        market_id: marketSel.value, identity: identity.value.trim(),
        stances: stances, horizon: parseInt(horizon.value, 10) || 5,
        provenance: "operator input via GUI"
      }).then(function (d) {
        Arena.setBusy(recordBtn, false);
        paintAgreeSummary(out, d.summary);
      }).catch(function (e) {
        Arena.setBusy(recordBtn, false);
        out.innerHTML = ""; bannerInto(out, "err", "Vote refused", e.message);
      });
    });

    Arena.get("/api/research/agreement/watchlist").then(function (d) {
      if (!d.count) return;
      var list = Arena.el("div", { style: "margin-top:8px" }, [
        Arena.el("b", {}, [document.createTextNode("Disagreement radar (" + d.count + ")")])]);
      d.disagreements.forEach(function (item) {
        list.appendChild(Arena.el("div", { "class": "muted", style: "font-size:11.5px" }, [
          document.createTextNode(item.identity + " — " + item.detail)]));
      });
      host.appendChild(list);
    }).catch(function () { });

    function paintAgreeSummary(target, s) {
      target.innerHTML = "";
      var tone = s.verdict === "DISAGREEMENT" ? "warn" : (s.verdict === "INSUFFICIENT" ? "" : "ok");
      target.appendChild(Arena.el("div", { "class": "btn-row", style: "margin-bottom:6px" }, [labChip(tone, s.verdict)]));
      target.appendChild(Arena.el("div", { "class": "muted", style: "font-size:11.5px" }, [document.createTextNode(s.detail)]));
      var rows = Object.keys(s.stances || {}).map(function (src) {
        var v = s.stances[src];
        return Arena.el("div", { style: "font-size:11.5px" }, [
          Arena.el("b", { "class": "mono" }, [document.createTextNode(src)]),
          document.createTextNode(": " + (v > 0 ? "bullish" : v < 0 ? "bearish" : "no edge"))]);
      });
      target.appendChild(Arena.el("div", { style: "margin-top:4px" }, rows));
    }
  }

  function renderCounterfactualLab(host) {
    host.appendChild(Arena.el("p", { "class": "muted", style: "font-size:11.5px" }, [document.createTextNode(
      "What-if arithmetic on RECORDED bars only: earlier/later entry, different sizing, or no-trade — each " +
      "variant is labelled SIMULATED. Nothing here is observed reality and nothing is extrapolated beyond " +
      "recorded history (the request is refused instead).")]));
    host.appendChild(Arena.el("p", { "class": "faint", style: "font-size:11px" }, [document.createTextNode(
      "What can go wrong: treating a simulation as a prediction. It cannot place orders; the only recovery " +
      "needed is to ignore it.")]));

    var identity = Arena.el("input", { id: "cf-identity", placeholder: "e.g. NSE|RELIANCE", value: "NSE|RELIANCE" });
    var entry = Arena.el("input", { id: "cf-entry", type: "number", value: "0", min: "0", style: "width:80px" });
    var horizon = Arena.el("input", { id: "cf-horizon", type: "number", value: "5", min: "1", style: "width:70px" });
    var size = Arena.el("input", { id: "cf-size", type: "number", value: "1", min: "0.01", step: "0.1", style: "width:70px" });
    var cfMarket = labSelect("cf-market", [["india", "India"], ["us", "US"], ["japan", "Japan"],
      ["uk", "UK"], ["hk", "HK"], ["sg", "Singapore"], ["global", "Global"]], "india");
    host.appendChild(Arena.el("div", { "class": "grid cols-2", style: "gap:6px;margin-bottom:8px" }, [
      labField("Instrument", identity), labField("Market", cfMarket)]));
    host.appendChild(Arena.el("div", { "class": "grid cols-2", style: "gap:6px;margin-bottom:8px" }, [
      labField("Entry bar index", entry), labField(" ", Arena.el("span"))]));
    host.appendChild(Arena.el("div", { "class": "btn-row", style: "flex-wrap:wrap;margin-bottom:8px" }, [
      labField("Horizon (bars)", horizon), labField("Size multiplier", size)]));

    var runBtn = Arena.el("button", { "class": "btn primary", id: "cf-run" }, [document.createTextNode("Simulate variants")]);
    var out = Arena.el("div", { id: "cf-result", style: "margin-top:8px" });
    host.appendChild(Arena.el("div", { "class": "btn-row" }, [runBtn]));
    host.appendChild(out);

    runBtn.addEventListener("click", function () {
      Arena.setBusy(runBtn, true, "Simulating…");
      Arena.post("/api/research/counterfactual", {
        market_id: cfMarket.value, identity: identity.value.trim(),
        entry_index: parseInt(entry.value, 10) || 0,
        horizon: parseInt(horizon.value, 10) || 5,
        size: parseFloat(size.value) || 1.0
      }).then(function (d) {
        Arena.setBusy(runBtn, false);
        out.innerHTML = "";
        if (d.label === "NO_DATA") {
          bannerInto(out, "err", "No recorded bars", d.note);
          return;
        }
        out.appendChild(Arena.el("div", { "class": "btn-row", style: "margin-bottom:6px" }, [labChip("warn", d.label)]));
        var table = Arena.el("table", { "class": "table" });
        table.appendChild(Arena.el("tr", {}, ["Variant", "Return %", "Max DD %", "Label"].map(function (h) {
          return Arena.el("th", {}, [document.createTextNode(h)]); })));
        (d.variants || []).forEach(function (v) {
          table.appendChild(Arena.el("tr", {}, [
            Arena.el("td", {}, [document.createTextNode(v.name)]),
            Arena.el("td", { "class": "mono" }, [document.createTextNode(v.return_pct.toFixed(3))]),
            Arena.el("td", { "class": "mono" }, [document.createTextNode(v.max_drawdown_pct.toFixed(3))]),
            Arena.el("td", {}, [labChip("", v.label)])
          ]));
        });
        out.appendChild(table);
        out.appendChild(Arena.el("p", { "class": "faint", style: "font-size:11px;margin-top:6px" },
          [document.createTextNode(d.note)]));
      }).catch(function (e) {
        Arena.setBusy(runBtn, false);
        out.innerHTML = ""; bannerInto(out, "err", "Simulation refused", e.message);
      });
    });
  }

  function renderResearchSchedule(host) {
    host.appendChild(Arena.el("p", { "class": "muted", style: "font-size:11.5px" }, [document.createTextNode(
      "The autonomous loop (observe → detect → explain → compare → hypothesize → test → validate → remember → " +
      "recommend → monitor → review → improve) is bounded: a daily experiment quota, the resource governor and " +
      "trading-critical windows outrank research throughput. During critical windows only opportunity analysis " +
      "and reconciliation checks stay admissible.")]));
    var statusHost = Arena.el("div", { id: "sched-status" }, [document.createTextNode("Loading schedule…")]);
    host.appendChild(statusHost);
    var admitBtn = Arena.el("button", { "class": "btn ghost sm", id: "sched-admit" }, [
      document.createTextNode("Request admission for a backtest")]);
    var verdict = Arena.el("div", { id: "sched-verdict", style: "margin-top:6px" });
    host.appendChild(Arena.el("div", { "class": "btn-row", style: "margin-top:8px" }, [admitBtn]));
    host.appendChild(verdict);

    function paint(d) {
      statusHost.innerHTML = "";
      var kv = Arena.el("dl", { "class": "kv" });
      [["Day (UTC)", d.day], ["Quota", d.quota_used + " / " + d.quota_limit],
       ["Trading-critical window", d.trading_critical_window ? "YES — research yields" : "no"],
       ["Market open (pinned)", d.market_open === null ? "unknown (no pinned universe)" : (d.market_open ? "yes" : "no")]
      ].forEach(function (pair) {
        kv.appendChild(Arena.el("dt", {}, [document.createTextNode(pair[0])]));
        kv.appendChild(Arena.el("dd", {}, [document.createTextNode(String(pair[1]))]));
      });
      statusHost.appendChild(kv);
      var pol = d.policy || {};
      var chips = [["Historical research", pol.expand_historical_research], ["Backtests", pol.run_backtests],
        ["Model training", pol.train_or_evaluate_models], ["Reports", pol.refresh_reports],
        ["Opportunity analysis", pol.opportunity_analysis]];
      var row = Arena.el("div", { "class": "btn-row", style: "margin-top:6px;flex-wrap:wrap" });
      chips.forEach(function (c) {
        row.appendChild(labChip(c[1] ? "ok" : "warn", c[0] + ": " + (c[1] ? "allowed" : "held")));
      });
      statusHost.appendChild(row);
    }

    function refresh() {
      Arena.get("/api/research/schedule").then(paint).catch(function (e) {
        statusHost.innerHTML = "";
        statusHost.appendChild(Arena.el("span", { "class": "faint" }, [document.createTextNode(e.message)]));
      });
    }
    refresh();

    admitBtn.addEventListener("click", function () {
      Arena.setBusy(admitBtn, true, "Asking scheduler…");
      Arena.post("/api/research/schedule/admit", { kind: "backtest" }).then(function (d) {
        Arena.setBusy(admitBtn, false);
        verdict.innerHTML = "";
        if (d.admitted) {
          bannerInto(verdict, "ok", "Admitted", "quota now " + d.quota_used + "/" + d.quota_limit + " for " + d.day);
        } else {
          bannerInto(verdict, "warn", d.verdict, d.reason);
        }
        refresh();
      }).catch(function (e) {
        Arena.setBusy(admitBtn, false);
        verdict.innerHTML = ""; bannerInto(verdict, "err", "Admission failed", e.message);
      });
    });
  }

  function viewResearchRefresh() {
    var root = document.getElementById("main-view");
    if (root && state.view === "research") { root.innerHTML = ""; viewResearch(root); }
  }

  // Extend the existing System and Research views without rewriting them.
  var _origViewSystem = viewSystem;
  viewSystem = function (root) { _origViewSystem(root); renderSystemControl(root); };
  var _origViewResearch = viewResearch;
  viewResearch = function (root) { _origViewResearch(root); renderResearchMemory(root); };


  if (typeof module !== "undefined" && module.exports &&
      typeof process !== "undefined" && process.versions && process.versions.node) {
    module.exports = {
      createPaperPoller: createPaperPoller,
      paperStatusPresentation: paperStatusPresentation
    };
  } else {
    document.addEventListener("DOMContentLoaded", start);
  }
})();
