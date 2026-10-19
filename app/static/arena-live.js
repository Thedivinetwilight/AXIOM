/* ===========================================================================
 * ARENA LIVE SWITCH — the PAPER / LIVE control and its activation wizard.
 *
 * WHAT THIS IS
 *   One integrated workflow for the operator:
 *     connect the broker account -> set and confirm the hard risk limits ->
 *     review the readiness contract -> type the exact confirmation phrase ->
 *     START REAL-MONEY TRADING.
 *   and the reverse path: moving the switch away from LIVE runs an explicit
 *   pause/stop/drain flow that shows the open positions, the working orders
 *   and the reconciliation state, and never claims the account is flat.
 *
 * WHAT THIS IS NOT
 *   This file never places an order. It calls the workstation API, and the
 *   backend owns the live state machine, the readiness contract, the
 *   supervised child engine and the execution gateway. Clicking the switch
 *   on its own changes nothing: the four live switches are written only by
 *   the final, explicit activation.
 *
 * No secret ever reaches this file: the browser sends the broker client
 * secret once, over the local API, and receives only a masked credential
 * state (present / verified / expired) back.
 * =========================================================================== */
"use strict";

(function () {
  var snapshot = null;      // the last /api/live/activation payload
  var switchHost = null;    // the topbar switch container
  var wizard = null;        // the wizard state while it is open
  var lastError = "";

  // ------------------------------------------------------------- helpers --
  function el(tag, attrs, children) { return Arena.el(tag, attrs, children); }

  function text(value) { return document.createTextNode(value === null || value === undefined ? "" : String(value)); }

  function banner(host, kind, title, message, detail) {
    var node = el("div", { "class": "banner " + (kind || "info") }, [
      el("b", {}, [text(title)]),
      text(message || "")
    ]);
    if (detail) node.appendChild(el("div", { "class": "detail" }, [text(detail)]));
    host.appendChild(node);
    return node;
  }

  function field(label, hint, input) {
    return el("div", { "class": "field" }, [
      el("label", {}, [text(label)]),
      input,
      hint ? el("div", { "class": "hint" }, [text(hint)]) : null
    ]);
  }

  function inputNode(id, type, value, placeholder) {
    var node = el("input", { "id": id, "type": type || "text" });
    if (value !== null && value !== undefined) node.value = String(value);
    if (placeholder) node.setAttribute("placeholder", placeholder);
    return node;
  }

  function selectNode(id, options, value) {
    var node = el("select", { "id": id });
    options.forEach(function (option) {
      var opt = el("option", { "value": option[0] }, [text(option[1])]);
      if (option[0] === value) opt.selected = true;
      node.appendChild(opt);
    });
    return node;
  }

  function kv(pairs) {
    var node = el("dl", { "class": "kv" });
    pairs.forEach(function (pair) {
      node.appendChild(el("dt", {}, [text(pair[0])]));
      node.appendChild(el("dd", {}, [text(pair[1] === null || pair[1] === undefined ? "unavailable" : pair[1])]));
    });
    return node;
  }

  function money(value) {
    if (value === null || value === undefined || isNaN(value)) return "unavailable";
    return Arena.fmtMoney(Number(value), "INR");
  }

  function stepsBar(names, current) {
    var bar = el("div", { "class": "steps" });
    names.forEach(function (name, index) {
      if (index > 0) bar.appendChild(el("div", { "class": "step-line" }));
      bar.appendChild(el("div", {
        "class": "step-dot" + (index === current ? " active" : (index < current ? " done" : ""))
      }, [
        el("div", { "class": "n" }, [text(index < current ? "✓" : String(index + 1))]),
        el("div", { "class": "t" }, [text(name)])
      ]));
    });
    return bar;
  }

  // ------------------------------------------------------------- the API --
  function load() {
    return Arena.get("/api/live/activation").then(function (data) {
      snapshot = data;
      paintSwitch();
      return data;
    }).catch(function (e) {
      lastError = e.message;
      paintSwitch();
      throw e;
    });
  }

  function switchPosition() {
    if (!snapshot || !snapshot.switch) return "PAPER";
    return snapshot.switch.position || "PAPER";
  }

  // ----------------------------------------------------------- the switch --
  function mountSwitch(host) {
    switchHost = host;
    if (!host) return;
    host.innerHTML = "";
    var paper = el("button", { "class": "mode-seg paper", id: "mode-paper", type: "button",
      title: "Paper trading: every order is simulated." }, [text("PAPER")]);
    var live = el("button", { "class": "mode-seg live", id: "mode-live", type: "button",
      title: "Live trading: real money at the broker. Opens the activation wizard." },
      [text("LIVE")]);
    paper.addEventListener("click", function () { onSwitchPaper(); });
    live.addEventListener("click", function () { onSwitchLive(); });
    var group = el("div", { "class": "mode-switch", id: "live-switch", role: "group",
      "aria-label": "Trading mode" }, [paper, live]);
    var state = el("span", { "id": "live-switch-state" });
    host.appendChild(group);
    host.appendChild(state);
    paintSwitch();
    load().catch(function () { /* the state line explains the problem */ });
  }

  function paintSwitch() {
    if (!switchHost) return;
    var position = switchPosition();
    var paper = switchHost.querySelector("#mode-paper");
    var live = switchHost.querySelector("#mode-live");
    if (paper) paper.classList.toggle("active", position === "PAPER");
    if (live) live.classList.toggle("active", position !== "PAPER");
    var state = switchHost.querySelector("#live-switch-state");
    if (!state) return;
    state.innerHTML = "";
    if (lastError) {
      state.appendChild(Arena.stateBadge("UNAVAILABLE"));
      state.appendChild(el("span", { "class": "faint", style: "margin-left:6px;font-size:11px" },
        [text(lastError)]));
      return;
    }
    if (position === "LIVE_ACTIVE") {
      state.appendChild(el("span", { "class": "badge lg live" }, [
        el("span", { "class": "dot" }), text("LIVE · ACTIVE")]));
    } else if (position === "LIVE_CONFIGURED") {
      state.appendChild(el("span", { "class": "badge lg danger" }, [
        el("span", { "class": "dot" }), text("LIVE · NOT RUNNING")]));
    } else {
      state.appendChild(el("span", { "class": "badge lg paper" }, [
        el("span", { "class": "dot" }), text("PAPER · SIMULATED")]));
    }
  }

  function onSwitchLive() {
    if (switchPosition() === "LIVE_ACTIVE") {
      openWizard(3);
      return;
    }
    openWizard(0);
  }

  function onSwitchPaper() {
    var position = switchPosition();
    if (position === "PAPER") {
      Arena.toast("info", "Already in PAPER: orders are simulated and no broker is contacted.");
      return;
    }
    openSwitchOff();
  }

  // ------------------------------------------------------------- wizard ---
  var STEP_NAMES = ["Connect account", "Limits & instruments", "Readiness", "Confirm"];

  function openWizard(step) {
    closeDialog();
    wizard = {
      step: step === undefined ? 0 : step,
      body: null,
      error: "",
      form: {
        client_id: "", client_secret: "", redirect_uri: "", code: "",
        max_position_value: "", max_open_positions: "", max_daily_loss: "",
        max_trades_per_day: "", live_canary_max_notional: "",
        live_canary_orders: "", live_max_equity_drawdown: "",
        symbols: "", timeframe: "1m", calendar: "nse-session", data_source: "upstox"
      }
    };
    var backdrop = el("div", { "class": "dialog-backdrop", id: "live-wizard" });
    var panel = el("div", { "class": "dialog wide", role: "dialog", "aria-modal": "true",
      "aria-label": "Live trading activation" });
    var head = el("div", { "class": "dialog-head" }, [
      el("div", {}, [
        el("h3", {}, [text("Live trading activation")]),
        el("div", { "class": "faint", style: "font-size:12px" }, [
          text("Real money at the broker. Nothing is enabled until the final confirmation.")])
      ]),
      el("button", { "class": "btn ghost sm", id: "wizard-close", type: "button",
        onclick: function () { closeDialog(); } }, [text("Close")])
    ]);
    var steps = el("div", { id: "wizard-steps" });
    var body = el("div", { "class": "dialog-body", id: "wizard-body" });
    var foot = el("div", { "class": "dialog-foot", id: "wizard-foot" });
    panel.appendChild(head);
    panel.appendChild(steps);
    panel.appendChild(body);
    panel.appendChild(foot);
    backdrop.appendChild(panel);
    document.body.appendChild(backdrop);
    backdrop.addEventListener("click", function (ev) {
      if (ev.target === backdrop) closeDialog();
    });
    document.addEventListener("keydown", onKey);
    wizard.body = body;
    paintWizard();
    load().then(function () { prefill(); paintWizard(); }).catch(function () { paintWizard(); });
  }

  function onKey(ev) {
    if (ev.key === "Escape" && wizard) closeDialog();
  }

  function closeDialog() {
    if (wizard && wizard.backdrop) wizard.backdrop.remove();
    wizard = null;
    document.removeEventListener("keydown", onKey);
    load().catch(function () { });
  }

  function prefill() {
    if (!wizard || !snapshot) return;
    var config = snapshot.configuration || {};
    var form = wizard.form;
    function set(name, value) {
      if (value === null || value === undefined || value === "") return;
      form[name] = String(value);
    }
    set("max_position_value", config.max_position_value);
    set("max_open_positions", config.max_open_positions);
    set("max_daily_loss", config.max_daily_loss);
    set("max_trades_per_day", config.max_trades_per_day);
    set("live_canary_max_notional", config.live_canary_max_notional);
    set("live_canary_orders", config.live_canary_orders);
    set("live_max_equity_drawdown", config.live_max_equity_drawdown);
    set("symbols", config.symbols);
    set("timeframe", config.timeframe);
    set("calendar", config.calendar);
  }

  function paintWizard() {
    if (!wizard) return;
    var steps = document.getElementById("wizard-steps");
    var body = document.getElementById("wizard-body");
    var foot = document.getElementById("wizard-foot");
    if (steps) {
      steps.innerHTML = "";
      steps.appendChild(stepsBar(STEP_NAMES, wizard.step));
    }
    if (body) {
      body.innerHTML = "";
      if (wizard.step === 0) body.appendChild(stepConnect());
      else if (wizard.step === 1) body.appendChild(stepLimits());
      else if (wizard.step === 2) body.appendChild(stepReadiness());
      else body.appendChild(stepConfirm());
    }
    if (foot) {
      foot.innerHTML = "";
      foot.appendChild(el("div", { "class": "btn-row" }, [
        el("button", { "class": "btn ghost", id: "wizard-back", type: "button",
          onclick: function () { wizard.step = Math.max(0, wizard.step - 1); paintWizard(); } },
          [text("Back")]),
        el("span", { "class": "spacer", style: "flex:1" }),
        el("button", { "class": "btn primary", id: "wizard-next", type: "button",
          onclick: function () { wizard.step = Math.min(STEP_NAMES.length - 1, wizard.step + 1); paintWizard(); } },
          [text(wizard.step === STEP_NAMES.length - 1 ? "Review again" : "Next")])
      ]));
    }
  }

  // -- step 1: connect the broker account --------------------------------
  function stepConnect() {
    var box = el("div");
    var credential = (snapshot && snapshot.credential) || {};
    box.appendChild(banner(box, "info", "Step 1 — connect your broker account",
      "Arena uses the broker's official OAuth login. Your broker application credentials and the " +
      "authorization code are used by the local backend only: the access token is written to your own " +
      "Arena configuration file and is never returned to this page.",
      "Never enter your broker website password, PIN, TOTP seed or a one-time code here. Arena never asks for them."));
    box.appendChild(banner(box, credential.present ? "ok" : "warn",
      credential.present ? ("Credential stored (" + credential.state + ")") : "No live credential stored",
      credential.note || "",
      credential.present ? ("Stored value: " + (credential.masked || "present (hidden)")) : ""));
    if (credential.reconnect_required) {
      box.appendChild(banner(box, "err",
        "The stored credential must be refreshed before it can be used",
        "New entries are blocked while the credential is expired, changed or unverified. " +
        "Reconnect the broker account: the new access token replaces the old one and is verified " +
        "against the live host before anything else happens.",
        "The switch stays exactly where it is until you reconnect."));
    }

    var form = wizard.form;
    box.appendChild(field("Broker application client id (public)",
      "The public API key from your broker developer console.",
      inputNode("live-client-id", "text", form.client_id, "e.g. 1a2b3c4d-…")));
    box.appendChild(field("Broker application client secret",
      "Sent once to the local backend over this machine's API. It is stored in your own configuration file.",
      inputNode("live-client-secret", "password", form.client_secret, "••••••••")));
    box.appendChild(field("Registered redirect URI",
      "Must be an https:// URI registered with the broker, for example https://localhost:8443/callback.",
      inputNode("live-redirect-uri", "text", form.redirect_uri, "https://localhost:8443/callback")));

    var openRow = el("div", { "class": "btn-row" });
    var openBtn = el("button", { "class": "btn ghost", id: "live-open-login", type: "button" },
      [text("Open the broker's official login page")]);
    openBtn.addEventListener("click", function () {
      form.client_id = document.getElementById("live-client-id").value.trim();
      form.redirect_uri = document.getElementById("live-redirect-uri").value.trim();
      Arena.setBusy(openBtn, true, "Opening…");
      Arena.post("/api/live/activation/oauth/url", {
        client_id: form.client_id, redirect_uri: form.redirect_uri
      }).then(function (data) {
        Arena.setBusy(openBtn, false);
        window.open(data.authorization_url, "_blank", "noopener");
        Arena.toast("info", "Approve access in the broker page, then paste the code it redirects with below.");
      }).catch(function (e) {
        Arena.setBusy(openBtn, false);
        Arena.toast("err", e.message);
      });
    });
    openRow.appendChild(openBtn);
    box.appendChild(openRow);

    box.appendChild(field("Authorization code from the broker",
      "The broker redirects to your registered URI with a code. Copy the code (or the whole redirected URL) here.",
      inputNode("live-code", "text", form.code, "code=…")));

    var row = el("div", { "class": "btn-row", style: "margin-top:10px" });
    var connectBtn = el("button", { "class": "btn primary", id: "live-connect", type: "button" },
      [text(credential.reconnect_required ? "Reconnect account" : "Connect account")]);
    connectBtn.addEventListener("click", function () {
      form.client_id = document.getElementById("live-client-id").value.trim();
      form.client_secret = document.getElementById("live-client-secret").value;
      form.redirect_uri = document.getElementById("live-redirect-uri").value.trim();
      form.code = document.getElementById("live-code").value.trim();
      Arena.setBusy(connectBtn, true, "Connecting…");
      Arena.post("/api/live/activation/oauth/exchange", {
        code: form.code, client_id: form.client_id,
        client_secret: form.client_secret, redirect_uri: form.redirect_uri
      }).then(function (data) {
        Arena.setBusy(connectBtn, false);
        form.client_secret = "";
        var secret = document.getElementById("live-client-secret");
        if (secret) secret.value = "";
        Arena.toast("ok", "Broker account connected and verified against the live host.");
        load().then(paintWizard);
      }).catch(function (e) {
        Arena.setBusy(connectBtn, false);
        Arena.toast("err", e.message);
        wizard.error = e.message;
        paintWizard();
      });
    });
    row.appendChild(connectBtn);
    if (credential.present) {
      var forgetBtn = el("button", { "class": "btn warn", id: "live-forget-credential", type: "button" },
        [text("Forget stored credential")]);
      forgetBtn.addEventListener("click", function () {
        Arena.setBusy(forgetBtn, true, "Removing…");
        Arena.post("/api/live/activation/credential/forget", {}).then(function (data) {
          Arena.setBusy(forgetBtn, false);
          Arena.toast("ok", data.note || "The stored credential was removed.");
          load().then(paintWizard);
        }).catch(function (e) {
          Arena.setBusy(forgetBtn, false);
          Arena.toast("err", e.message);
        });
      });
      row.appendChild(forgetBtn);
    }
    box.appendChild(row);
    return box;
  }

  // -- step 2: the hard risk limits --------------------------------------
  function stepLimits() {
    var box = el("div");
    box.appendChild(banner(box, "warn", "Step 2 — state your hard risk limits",
      "Every limit must be a finite, positive number. 0 means unlimited and is refused for live trading. " +
      "Arena never invents a default for you.",
      "These limits are enforced by the execution gateway: a breach blocks new entries and can trigger the " +
      "persisted emergency stop."));
    var form = wizard.form;
    var limits = (snapshot && snapshot.limits) || [];
    var grid = el("div", { "class": "grid cols-2" });
    limits.forEach(function (limit) {
      var name = limit.key.toLowerCase();
      var input = inputNode("limit-" + name, limit.kind === "count" ? "number" : "text",
        form[name], limit.required ? "required" : "optional");
      if (limit.kind === "count") input.setAttribute("min", limit.required ? "1" : "0");
      var cell = field(limit.label + (limit.required ? " *" : ""),
        limit.kind === "money" ? "Amount in Indian rupees (INR), e.g. 10000"
          : (limit.kind === "count" ? "Whole number of " + limit.unit : "Fraction between 0 and 1"),
        input);
      grid.appendChild(cell);
    });
    box.appendChild(grid);
    box.appendChild(el("div", { "class": "section-gap" }));
    box.appendChild(field("Instrument(s) to trade",
      "Upstox instrument keys, comma separated. Start with ONE instrument.",
      inputNode("live-symbols", "text", form.symbols, "NSE_EQ|INE002A01018")));
    box.appendChild(field("Timeframe",
      "The live engine is intraday-only.",
      selectNode("live-timeframe", [["1m", "1 minute"], ["5m", "5 minutes"], ["15m", "15 minutes"],
        ["30m", "30 minutes"], ["1h", "1 hour"]], form.timeframe)));
    box.appendChild(field("Market session calendar",
      "Indian equity orders are usually sent with the nse-session calendar.",
      selectNode("live-calendar", [["nse-session", "NSE session (09:15–15:30 IST)"],
        ["continuous", "Continuous (24×7)"]], form.calendar)));
    var row = el("div", { "class": "btn-row", style: "margin-top:6px" });
    var saveBtn = el("button", { "class": "btn primary", id: "live-save-limits", type: "button" },
      [text("Save configuration and check readiness")]);
    saveBtn.addEventListener("click", function () {
      limits.forEach(function (limit) {
        var name = limit.key.toLowerCase();
        var node = document.getElementById("limit-" + name);
        form[name] = node ? node.value.trim() : "";
      });
      form.symbols = document.getElementById("live-symbols").value.trim();
      form.timeframe = document.getElementById("live-timeframe").value;
      form.calendar = document.getElementById("live-calendar").value;
      var payload = {
        data_source: "upstox",
        symbols: form.symbols,
        timeframe: form.timeframe,
        calendar: form.calendar
      };
      limits.forEach(function (limit) {
        var name = limit.key.toLowerCase();
        if (form[name] !== "") payload[name] = form[name];
      });
      Arena.setBusy(saveBtn, true, "Saving…");
      Arena.post("/api/live/activation/configuration", payload).then(function (data) {
        Arena.setBusy(saveBtn, false);
        (data.warnings || []).forEach(function (warning) {
          Arena.toast("warn", warning);
        });
        Arena.toast("ok", "Live configuration saved. Live trading is still not enabled.");
        load().then(function () { wizard.step = 2; paintWizard(); });
      }).catch(function (e) {
        Arena.setBusy(saveBtn, false);
        Arena.toast("err", e.message);
        banner(box, "err", "The configuration was refused", e.message);
      });
    });
    row.appendChild(saveBtn);
    box.appendChild(row);
    return box;
  }

  // -- step 3: the readiness contract ------------------------------------
  function stepReadiness() {
    var box = el("div");
    var readiness = (snapshot && snapshot.readiness) || {};
    var checks = readiness.checks || [];
    var blocked = checks.filter(function (c) { return c.status === "BLOCKED"; });
    var unknown = checks.filter(function (c) { return c.status === "UNKNOWN"; });
    var warnings = checks.filter(function (c) { return c.status === "WARNING"; });
    var verdict = readiness.verdict || "UNKNOWN";
    box.appendChild(banner(box,
      verdict === "READY" ? "ok" : (verdict === "READY_WITH_WARNINGS" ? "warn" : "err"),
      "Step 3 — readiness review (" + verdict + ")",
      verdict === "READY" ? "Every required check passed."
        : (verdict === "READY_WITH_WARNINGS"
          ? "Every required check passed; the warnings below are accepted operational gaps."
          : "Live trading stays blocked until every required check passes."),
      blocked.length + " blocked · " + unknown.length + " unknown · " + warnings.length + " warning"));
    var list = el("div", { "class": "checklist" });
    checks.forEach(function (check) {
      var kind = check.status === "READY" ? "ok" : (check.status === "WARNING" ? "warn"
        : (check.status === "UNKNOWN" ? "info" : "danger"));
      list.appendChild(el("div", { "class": "check-item" }, [
        el("span", { "class": "badge " + kind }, [text(check.status)]),
        el("span", { "class": "name" }, [text(check.name)]),
        el("span", { "class": "msg" }, [text(check.detail || "")])
      ]));
    });
    box.appendChild(list);
    var row = el("div", { "class": "btn-row", style: "margin-top:12px" });
    var recheck = el("button", { "class": "btn ghost", id: "live-recheck", type: "button" },
      [text("Recheck readiness")]);
    recheck.addEventListener("click", function () {
      Arena.setBusy(recheck, true, "Checking…");
      load().then(function () {
        Arena.setBusy(recheck, false);
        paintWizard();
      }).catch(function (e) {
        Arena.setBusy(recheck, false);
        Arena.toast("err", e.message);
      });
    });
    row.appendChild(recheck);
    box.appendChild(row);
    box.appendChild(el("div", { "class": "section-gap" }));
    box.appendChild(summaryPanel());
    return box;
  }

  // -- the pre-confirmation summary ---------------------------------------
  function summaryPanel() {
    var box = el("div", { "class": "card", style: "border-color:rgba(244,63,94,.45)" });
    var body = el("div", { "class": "card-body" });
    var config = (snapshot && snapshot.configuration) || {};
    var book = (snapshot && snapshot.book) || {};
    var credential = (snapshot && snapshot.credential) || {};
    body.appendChild(banner(body, "err", "Real orders can lose real money",
      "Everything below describes a live session that places REAL orders at the broker. " +
      "Prior simulated or backtest returns do not guarantee live profits."));
    body.appendChild(kv([
      ["Mode", (config.mode || "?") + " / " + (config.broker || "?")],
      ["Broker account", credential.present ? ("connected · " + credential.state + " · " + (credential.masked || "")) : "not connected"],
      ["Data source", config.data_source || "?"],
      ["Instrument(s)", config.symbols || "?"],
      ["Timeframe", config.timeframe || "?"],
      ["Market session", config.calendar || "?"],
      ["Max position / order notional", money(config.max_position_value)],
      ["Max open positions", config.max_open_positions],
      ["Max daily loss", money(config.max_daily_loss)],
      ["Max entries per day", config.max_trades_per_day],
      ["Canary cap (first entries)", money(config.live_canary_max_notional)],
      ["Canary entries", config.live_canary_orders],
      ["Peak-equity drawdown auto-stop", config.live_max_equity_drawdown],
      ["Open positions", book.positions === null || book.positions === undefined ? "unavailable" : String(book.positions)],
      ["Working orders", book.working_orders === null || book.working_orders === undefined ? "unavailable" : String(book.working_orders)],
      ["Unresolved intents", book.unresolved_intents === null || book.unresolved_intents === undefined ? "unavailable" : String(book.unresolved_intents)],
      ["Last reconciliation", book.reconciliation && book.reconciliation.ok
        ? ("fresh · " + (book.reconciliation.timestamp || ""))
        : ("not confirmed · " + ((book.reconciliation && book.reconciliation.reason) || "unavailable"))]
    ]));
    var row = el("div", { "class": "btn-row", style: "margin-top:12px" });
    var stopBtn = el("button", { "class": "btn live", id: "live-emergency-stop", type: "button" },
      [text("EMERGENCY STOP")]);
    stopBtn.addEventListener("click", function () {
      Arena.confirmDialog("EMERGENCY STOP",
        "This persists the kill switch, invalidates the live authorization and asks the engine to drain. " +
        "No position is closed automatically and risk-reducing exits stay enabled.",
        "TRIGGER EMERGENCY STOP", true, function () {
          Arena.post("/api/live/emergency-stop", { reason: "operator emergency stop (workstation)" })
            .then(function (data) {
              Arena.toast("err", "Emergency stop active: " + (data.note || ""));
              load().then(paintWizard);
            }).catch(function (e) { Arena.toast("err", e.message); });
        });
    });
    row.appendChild(stopBtn);
    body.appendChild(row);
    box.appendChild(body);
    return box;
  }

  // -- step 4: the final, explicit confirmation ---------------------------
  function stepConfirm() {
    var box = el("div");
    var readiness = (snapshot && snapshot.readiness) || {};
    var verdict = readiness.verdict || "UNKNOWN";
    var phrase = (readiness.required_phrase) || (snapshot && snapshot.required_phrase) || "";
    var canStart = verdict === "READY" || verdict === "READY_WITH_WARNINGS";
    box.appendChild(summaryPanel());
    box.appendChild(el("div", { "class": "section-gap" }));
    if (!canStart) {
      box.appendChild(banner(box, "err", "Live trading is blocked",
        "The readiness contract does not pass yet, so the start button is refused. " +
        "Fix every blocker above and recheck — Arena will not start a live session that is not ready.",
        (readiness.required_blocked || []).concat(readiness.required_unknown || []).join("; ")));
    }
    var phraseField = field("Type the exact confirmation phrase",
      phrase ? ("The phrase is: " + phrase) : "The phrase is required.",
      inputNode("live-confirm-phrase", "text", "", phrase || ""));
    box.appendChild(phraseField);
    var row = el("div", { "class": "btn-row" });
    var startBtn = el("button", { "class": "btn live big", id: "live-start", type: "button" },
      [text("START REAL-MONEY TRADING")]);
    if (!canStart) startBtn.disabled = true;
    startBtn.addEventListener("click", function () {
      var typed = document.getElementById("live-confirm-phrase").value;
      Arena.confirmDialog("Start real-money trading",
        "This starts the supervised live engine. Orders placed from now on are real orders at the broker " +
        "and can lose real money. The emergency stop stays available in the toolbar.",
        "START REAL-MONEY TRADING", true, function () {
          Arena.setBusy(startBtn, true, "Starting…");
          Arena.post("/api/live/activation/start", { confirmation_phrase: typed }).then(function (data) {
            Arena.setBusy(startBtn, false);
            Arena.toast("ok", "Live session is " + data.state + ". Real orders are possible from now on.");
            closeDialog();
          }).catch(function (e) {
            Arena.setBusy(startBtn, false);
            Arena.toast("err", e.message);
            load().then(paintWizard);
          });
        });
    });
    row.appendChild(startBtn);
    box.appendChild(row);
    box.appendChild(el("p", { "class": "faint", style: "font-size:11.5px;margin-top:8px" }, [
      text("The start button is a separate action: it is not a toggle, and no background task, restart or " +
        "recovery path can perform it for you.")]));
    return box;
  }

  // ------------------------------------------------------- the switch-off --
  function openSwitchOff() {
    closeDialog();
    var backdrop = el("div", { "class": "dialog-backdrop", id: "live-switch-off" });
    var panel = el("div", { "class": "dialog wide", role: "dialog", "aria-modal": "true",
      "aria-label": "Return to paper trading" });
    var body = el("div", { "class": "dialog-body", id: "switchoff-body" });
    panel.appendChild(el("div", { "class": "dialog-head" }, [
      el("div", {}, [
        el("h3", {}, [text("Return to PAPER trading")]),
        el("div", { "class": "faint", style: "font-size:12px" }, [
          text("New entries stop first, the engine drains, the authorization is invalidated and the " +
            "configuration returns to PAPER.")]),
      ]),
      el("button", { "class": "btn ghost sm", id: "switchoff-close", type: "button",
        onclick: function () { closeDialog(); } }, [text("Close")])
    ]));
    panel.appendChild(body);
    backdrop.appendChild(panel);
    document.body.appendChild(backdrop);
    document.addEventListener("keydown", onKey);
    paintSwitchOff(body);
    Arena.get("/api/live/activation/book").then(function (book) {
      paintSwitchOff(body, book);
    }).catch(function (e) {
      banner(body, "err", "The live book could not be read", e.message);
    });
  }

  function paintSwitchOff(body, book) {
    body.innerHTML = "";
    body.appendChild(banner(body, "err", "This stops live trading",
      "Arena will stop new entries, ask the supervised engine to drain safely, invalidate the live " +
      "authorization and return the configuration to PAPER.",
      "Arena never force-closes a position and never claims the account is flat: the state below is what " +
      "the broker last confirmed."));
    if (!book) {
      body.appendChild(el("div", { "class": "empty" }, [text("Reading the live book…")]));
      return;
    }
    body.appendChild(kv([
      ["Open positions", book.positions === null || book.positions === undefined ? "unavailable" : String(book.positions)],
      ["Working orders", book.working_orders === null || book.working_orders === undefined ? "unavailable" : String(book.working_orders)],
      ["Unresolved intents", book.unresolved_intents === null || book.unresolved_intents === undefined ? "unavailable" : String(book.unresolved_intents)],
      ["Last reconciliation", book.reconciliation && book.reconciliation.ok
        ? ("fresh · " + (book.reconciliation.timestamp || ""))
        : ("not confirmed · " + ((book.reconciliation && book.reconciliation.reason) || "unavailable"))]
    ]));
    var row = el("div", { "class": "btn-row", style: "margin-top:14px" });
    var stopBtn = el("button", { "class": "btn live big", id: "live-stop", type: "button" },
      [text("STOP LIVE TRADING — DRAIN AND RETURN TO PAPER")]);
    stopBtn.addEventListener("click", function () {
      Arena.setBusy(stopBtn, true, "Stopping…");
      Arena.post("/api/live/activation/stop", {
        reason: "operator moved the switch to PAPER"
      }).then(function (data) {
        Arena.setBusy(stopBtn, false);
        var out = el("div");
        out.appendChild(banner(out, "ok", "Live trading stopped",
          data.note || "New entries are refused and the live authorization is invalidated.",
          (data.errors || []).join("; ")));
        out.appendChild(kv([
          ["Session state", data.state],
          ["Open positions", data.positions === null || data.positions === undefined ? "unavailable" : String(data.positions)],
          ["Working orders", data.working_orders === null || data.working_orders === undefined ? "unavailable" : String(data.working_orders)],
          ["Last reconciliation", data.reconciliation && data.reconciliation.ok
            ? ("fresh · " + (data.reconciliation.timestamp || ""))
            : ("not confirmed · " + ((data.reconciliation && data.reconciliation.reason) || "unavailable"))]
        ]));
        body.innerHTML = "";
        body.appendChild(out);
        load().catch(function () { });
      }).catch(function (e) {
        Arena.setBusy(stopBtn, false);
        banner(body, "err", "The live session could not be stopped", e.message);
      });
    });
    row.appendChild(stopBtn);
    body.appendChild(row);
  }

  // -------------------------------------------------------------- exports --
  window.ArenaLive = {
    mountSwitch: mountSwitch,
    refresh: load,
    openWizard: openWizard,
    openSwitchOff: openSwitchOff,
    position: switchPosition
  };
})();
