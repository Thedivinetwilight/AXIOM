/* ===========================================================================
 * ARENA MAIN APPLICATION (3.1.0)
 *
 * The operating cockpit: command center, paper controls, research lab,
 * market events, alerts, execution detail, settings and system diagnostics.
 *
 * Static asset — no Python templating. All state comes from JSON endpoints.
 * Design rules implemented here:
 *   - every control has a backend action and visible busy/success/error state
 *   - missing data is shown as "—"/empty state, never as zero
 *   - PAPER / SANDBOX / LIVE modes are visually distinct everywhere
 *   - live controls render locked until the backend reports them enabled
 * =========================================================================== */
"use strict";

(function () {
  var state = {
    boot: null,
    view: location.hash.replace(/^#\/?/, "") || "command",
    paper: null,
    account: null,
    orders: [],
    trades: [],
    positions: [],
    dash: null,
    equityHistory: [],   // session-scoped rolling equity series (client-observed)
    strategies: [],
    datasets: [],
    polling: null,
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
      Arena.el("div", { "class": "logo" }, [document.createTextNode("A")]),
      Arena.el("div", { "class": "name" }, [
        document.createTextNode("ARENA"),
        Arena.el("small", {}, [document.createTextNode("research · paper trading")])
      ])
    ]);

    var topbar = Arena.el("div", { "class": "topbar", id: "topbar" }, [
      Arena.modeBadge(state.boot),
      Arena.el("span", { id: "conn-badge" }, [Arena.stateBadge("CONNECTING")]),
      Arena.el("span", { "class": "spacer" }),
      Arena.el("span", { "class": "faint mono", id: "build-label" },
        [document.createTextNode("Arena " + state.boot.version + " · " + state.boot.build_id)]),
      Arena.el("span", { id: "engine-controls" })
    ]);

    var nav = Arena.el("nav", { "class": "sidenav", id: "sidenav" });
    [
      ["command", "◧", "Command Center"],
      ["research", "⚗", "Research Lab"],
      ["execution", "⇄", "Orders & Trades"],
      ["events", "◉", "Market Events"],
      ["alerts", "▲", "Alerts"],
      ["settings", "⚙", "Settings"],
      ["system", "▤", "System"]
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
    nav.appendChild(Arena.el("a", { "class": "navlink", href: "/console", id: "nav-console" }, [
      Arena.el("span", { "class": "ico" }, [document.createTextNode("❯")]),
      document.createTextNode("Feature console")
    ]));

    var main = Arena.el("main", { "class": "main", id: "main-view" });
    app.appendChild(brand); app.appendChild(topbar); app.appendChild(nav); app.appendChild(main);
    document.body.appendChild(app);
    document.body.appendChild(Arena.el("div", { id: "toasts" }));
    mainEl = main;
    renderEngineControls();
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
      command: viewCommand, research: viewResearch, execution: viewExecution,
      events: viewEvents, alerts: viewAlerts, settings: viewSettings, system: viewSystem
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
  function startPolling() {
    if (state.polling) clearInterval(state.polling);
    state.polling = setInterval(pollOnce, 3000);
    pollOnce();
  }

  function pollOnce() {
    if (document.hidden) return;
    Promise.allSettled([
      Arena.get("/paper/status"),
      Arena.get("/paper/account"),
      Arena.get("/paper/orders?limit=60"),
      Arena.get("/paper/trades?limit=60"),
      Arena.get("/paper/positions"),
      Arena.get("/dashboard/data")
    ]).then(function (results) {
      var okCount = 0;
      results.forEach(function (r) { if (r.status === "fulfilled") okCount += 1; });
      if (okCount === 0) { setConn("ERROR"); return; }
      setConn(okCount === results.length ? "CONNECTED" : "STALE");
      if (results[0].status === "fulfilled") state.paper = results[0].value;
      if (results[1].status === "fulfilled") {
        state.account = results[1].value;
        if (state.account && state.account.equity !== null && state.account.equity !== undefined) {
          state.equityHistory.push({ x: new Date().toLocaleTimeString(), y: state.account.equity });
          if (state.equityHistory.length > 400) state.equityHistory.shift();
        }
      }
      if (results[2].status === "fulfilled") state.orders = results[2].value.orders || [];
      if (results[3].status === "fulfilled") state.trades = results[3].value.trades || [];
      if (results[4].status === "fulfilled") state.positions = results[4].value.positions || [];
      if (results[5].status === "fulfilled") state.dash = results[5].value;
      refreshLive();
    });
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

    if (paperState === "RUNNING") {
      host.appendChild(btn("STOP PAPER", "danger", "stop-paper-top", function (b) {
        Arena.confirmDialog("Stop paper trading?",
          "Open orders are cancelled and the session is persisted. You can start again at any time.",
          "Stop paper", true, function () {
            Arena.setBusy(b, true, "Stopping…");
            Arena.post("/paper/stop").then(function () {
              Arena.toast("ok", "Paper engine stopped. State was saved.");
              pollOnce();
            }).catch(function (e) { Arena.setBusy(b, false); Arena.toast("err", e.message); });
          });
      }));
      host.appendChild(btn(paperState === "PAUSED" ? "RESUME" : "PAUSE", "ghost", "pause-resume-top", function (b) {
        var target = paperState === "PAUSED" ? "/paper/resume" : "/paper/pause";
        Arena.setBusy(b, true);
        Arena.post(target).then(function () { pollOnce(); }).catch(function (e) {
          Arena.setBusy(b, false); Arena.toast("err", e.message);
        });
      }));
    } else {
      host.appendChild(btn("START PAPER", "success", "start-paper-top", function (b) {
        Arena.setBusy(b, true, "Starting…");
        Arena.post("/api/paper/start-default", { source: state.boot.data_source || "replay" })
          .then(function () { Arena.toast("ok", "Paper engine is running."); pollOnce(); })
          .catch(function (e) { Arena.setBusy(b, false); Arena.toast("err", e.message); });
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

  function card(title, bodyNode, actions) {
    var head = Arena.el("div", { "class": "card-head" }, [Arena.el("h3", {}, [document.createTextNode(title)])]);
    if (actions) { head.appendChild(Arena.el("span", { "class": "grow" })); actions.forEach(function (a) { head.appendChild(a); }); }
    return Arena.el("div", { "class": "card" }, [head, Arena.el("div", { "class": "card-body" }, [bodyNode])]);
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
    box.appendChild(host);
    loadAlerts(host, compact ? 6 : 200);
    return box;
  }

  function loadAlerts(host, limit) {
    host.innerHTML = "";
    host.appendChild(Arena.el("div", { "class": "muted" }, [document.createTextNode("Loading alerts…")]));
    Arena.get("/api/v2/alerts?limit=" + limit).then(function (d) {
      host.innerHTML = "";
      var alerts = d.alerts || [];
      if (!alerts.length) {
        host.appendChild(Arena.el("div", { "class": "empty" }, [
          Arena.el("span", { "class": "ico" }, [document.createTextNode("▲")]),
          document.createTextNode("No alerts recorded. Risk, watchdog and system events will appear here.")
        ]));
        return;
      }
      var tbl = Arena.el("table", { "class": "table" });
      tbl.appendChild(Arena.el("thead", {}, [Arena.el("tr", {}, [
        Arena.el("th", {}, [document.createTextNode("Severity")]), Arena.el("th", {}, [document.createTextNode("Kind")]),
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
                loadAlerts(host, limit);
              }).catch(function (e) { Arena.toast("err", e.message); });
            } }, [document.createTextNode("Acknowledge")]);
        body.appendChild(Arena.el("tr", {}, [
          Arena.el("td", {}, [sev]),
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
    Arena.get("/api/live/readiness").then(function (d) {
      host.innerHTML = "";
      host.appendChild(Arena.el("div", { "class": "banner " + (d.enabled ? "err" : "info") }, [
        Arena.el("b", {}, [document.createTextNode(d.enabled
          ? "LIVE ROUTING IS ENABLED in this configuration"
          : "Live trading is LOCKED — exactly as intended for research.")]),
        document.createTextNode(d.enabled
          ? "Real money can move. Verify every guard below before doing anything."
          : "Activation requires the documented multi-switch opt-in set by hand outside this application; nothing in this UI can flip those switches.")
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

  document.addEventListener("DOMContentLoaded", start);
})();
