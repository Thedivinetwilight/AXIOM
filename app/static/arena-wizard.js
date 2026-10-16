/* ===========================================================================
 * ARENA FIRST-RUN WIZARD (3.1.0)
 *
 * Zero-command path to paper trading:
 *   Welcome → choose data mode → local credential auto-generated →
 *   preflight checklist → START PAPER TRADING → main application.
 *
 * Static asset: no Python templating. All data comes from JSON endpoints.
 * Nothing here can enable live trading; the backend refuses live keys and the
 * wizard only ever asks for paper-related choices.
 * =========================================================================== */
"use strict";

(function () {
  var step = 0;
  var bootInfo = null;
  var prepared = null;      // result of prepare-paper
  var paperPlan = null;     // { data_source, symbol, timeframe, strategy }
  var root = document.getElementById("wizard-root");

  function render() {
    if (!root) return;
    root.innerHTML = "";
    root.appendChild(header());
    root.appendChild(stepsBar());
    var panel = document.createElement("div");
    panel.className = "card";
    var body = document.createElement("div");
    body.className = "card-body";
    if (step === 0) body.appendChild(viewWelcome());
    else if (step === 1) body.appendChild(viewMode());
    else if (step === 2) body.appendChild(viewCredential());
    else if (step === 3) body.appendChild(viewPreflight());
    panel.appendChild(body);
    root.appendChild(panel);
    root.appendChild(Arena.el("p", { "class": "faint", style: "margin-top:16px;font-size:12px" },
      [document.createTextNode("Arena " + (bootInfo ? bootInfo.version : "") + " · build " +
        (bootInfo ? bootInfo.build_id : "") + " · everything on this screen stays on this machine")]));
  }

  // -------------------------------------------------------- credential ----
  function viewCredential() {
    var box = document.createElement("div");
    box.appendChild(Arena.el("h3", { style: "margin-top:0" },
      [document.createTextNode("Preparing your paper environment…")]));
    box.appendChild(Arena.el("div", { "class": "banner info", id: "w-prepare-status" },
      [document.createTextNode("Generating a secure local API credential, initializing the database " +
        "and checking bundled data. No action needed.")]));
    return box;
  }

  function header() {
    return Arena.el("div", { style: "display:flex;align-items:center;gap:12px" }, [
      Arena.el("div", { "class": "logo", style: "width:40px;height:40px;border-radius:12px;background:linear-gradient(135deg,#3b82f6,#7c3aed);display:grid;place-items:center;font-weight:800;color:#fff;font-size:20px" }, [document.createTextNode("A")]),
      Arena.el("div", {}, [
        Arena.el("h1", { style: "margin:0;font-size:22px" }, [document.createTextNode("Welcome to Arena")]),
        Arena.el("div", { "class": "muted", style: "font-size:13px" },
          [document.createTextNode("Quantitative research & paper trading workstation")])
      ])
    ]);
  }

  function stepsBar() {
    var names = ["Welcome", "Data mode", "Credential", "Ready"];
    var bar = Arena.el("div", { "class": "steps" });
    names.forEach(function (name, i) {
      if (i > 0) bar.appendChild(Arena.el("div", { "class": "step-line" }));
      var dot = Arena.el("div", {
        "class": "step-dot" + (i === step ? " active" : (i < step ? " done" : ""))
      }, [
        Arena.el("div", { "class": "n" }, [document.createTextNode(i < step ? "✓" : String(i + 1))]),
        Arena.el("div", { "class": "t" }, [document.createTextNode(name)])
      ]);
      bar.appendChild(dot);
    });
    return bar;
  }

  function bannerInto(host, kind, title, message, detail) {
    var node = Arena.el("div", { "class": "banner " + kind }, [
      Arena.el("b", {}, [document.createTextNode(title)]),
      document.createTextNode(message || "")
    ]);
    if (detail) node.appendChild(Arena.el("div", { "class": "detail" }, [document.createTextNode(detail)]));
    host.appendChild(node);
    return node;
  }

  // ------------------------------------------------------------ welcome ----
  function viewWelcome() {
    var box = document.createElement("div");
    box.appendChild(Arena.el("p", { style: "font-size:14.5px;max-width:640px" }, [
      document.createTextNode("Arena runs a full trading pipeline — market data, strategies, risk, " +
        "execution and learning — on this machine. In "),
      Arena.el("b", {}, [document.createTextNode("paper trading")]),
      document.createTextNode(" every order is simulated: no broker is contacted and no real money can move. " +
        "Live trading exists in this build but stays locked behind an explicit, multi-step activation that this wizard never touches.")
    ]));

    var info = Arena.el("div", { "class": "banner info" }, [
      Arena.el("b", {}, [document.createTextNode("How would you like to begin?")]),
      document.createTextNode("Paper trading needs no credentials and starts in one click. " +
        "Use sandbox or live-market data only if you already have Upstox credentials.")
    ]);
    box.appendChild(info);

    var buttons = Arena.el("div", { "class": "btn-row", style: "margin-top:18px" }, [
      Arena.el("button", { "class": "btn success big", id: "w-start-paper", onclick: function () { step = 1; render(); } },
        [document.createTextNode("START WITH PAPER TRADING")]),
      Arena.el("button", { "class": "btn ghost", id: "w-sandbox", onclick: viewSandbox },
        [document.createTextNode("Configure Upstox sandbox")]),
      Arena.el("button", { "class": "btn ghost", id: "w-advanced", onclick: function () { location.href = "/console"; } },
        [document.createTextNode("Advanced (feature console)")])
    ]);
    box.appendChild(buttons);

    var status = Arena.el("div", { "class": "kv", style: "margin-top:20px;max-width:520px" });
    [
      ["Version", (bootInfo ? bootInfo.version : "…")],
      ["Build", (bootInfo ? bootInfo.build_id : "…")],
      ["Current mode", (bootInfo ? bootInfo.mode_display : "…")],
      ["Real-money routing", (bootInfo && bootInfo.live_trading) ? "ENABLED (set outside this wizard)" : "disabled — fail-closed"],
      ["Data location", (bootInfo ? bootInfo.arena_home : "…")]
    ].forEach(function (row) {
      status.appendChild(Arena.el("dt", {}, [document.createTextNode(row[0])]));
      status.appendChild(Arena.el("dd", {}, [document.createTextNode(String(row[1]))]));
    });
    box.appendChild(status);
    return box;
  }

  // --------------------------------------------------- sandbox (side) ----
  function viewSandbox() {
    var box = document.createElement("div");
    box.appendChild(Arena.el("h3", { style: "margin-top:0" }, [document.createTextNode("Upstox sandbox credentials")]));
    box.appendChild(Arena.el("p", { "class": "muted" }, [document.createTextNode(
      "Sandbox sends real API requests to Upstox's sandbox endpoints with simulated money. " +
      "Paper trading does not need any of this.")]));

    function fld(id, label, type, hint) {
      var input = Arena.el("input", { type: type || "text", id: id, autocomplete: "off" });
      return Arena.el("div", { "class": "field" }, [
        Arena.el("label", { "for": id }, [document.createTextNode(label)]),
        input,
        hint ? Arena.el("div", { "class": "hint" }, [document.createTextNode(hint)]) : null
      ]);
    }
    box.appendChild(fld("SBX_TOKEN", "UPSTOX_SANDBOX_ACCESS_TOKEN", "password", "Sandbox access token from the Upstox developer console."));
    box.appendChild(fld("SBX_CLIENT_ID", "UPSTOX_CLIENT_ID (optional)", "text", "App key, only needed for token refresh flows."));
    box.appendChild(fld("SBX_REDIRECT", "UPSTOX_REDIRECT_URI (optional)", "text", "Must match the URI registered on your Upstox app."));

    var result = Arena.el("div");
    box.appendChild(result);
    box.appendChild(Arena.el("div", { "class": "btn-row", style: "margin-top:12px" }, [
      Arena.el("button", {
        "class": "btn primary", id: "sbx-save", onclick: function () {
          var values = {};
          ["SBX_TOKEN", "SBX_CLIENT_ID", "SBX_REDIRECT"].forEach(function (id) {
            var v = document.getElementById(id).value.trim();
            if (v) values[{ SBX_TOKEN: "UPSTOX_SANDBOX_ACCESS_TOKEN", SBX_CLIENT_ID: "UPSTOX_CLIENT_ID", SBX_REDIRECT: "UPSTOX_REDIRECT_URI" }[id]] = v;
          });
          if (!Object.keys(values).length) {
            bannerInto(result, "warn", "Nothing to save", "Fill in at least the sandbox access token.");
            return;
          }
          Arena.post("/setup/save", { values: values }).then(function (d) {
            bannerInto(result, "ok", "Saved", "Sandbox credentials were saved to " + d.path + ". Restart Arena to use them: this wizard keeps paper as the running mode.");
          }).catch(function (e) {
            bannerInto(result, "err", "Not saved", e.message);
          });
        }
      }, [document.createTextNode("Save sandbox credentials")]),
      Arena.el("button", { "class": "btn ghost", onclick: function () { render(); } }, [document.createTextNode("Back")])
    ]));
    return box;
  }

  // -------------------------------------------------------------- mode ----
  function viewMode() {
    var box = document.createElement("div");
    box.appendChild(Arena.el("h3", { style: "margin-top:0" }, [document.createTextNode("Choose your paper market data")]));
    box.appendChild(Arena.el("p", { "class": "muted" }, [document.createTextNode(
      "Orders are simulated in every mode on this screen. The choice is only where the price data comes from.")]));

    var grid = Arena.el("div", { "class": "choice-grid" });

    var replay = Arena.el("div", { "class": "choice", id: "mode-replay", tabindex: "0", role: "button" }, [
      Arena.el("div", { "class": "ico" }, [document.createTextNode("⏮")]),
      Arena.el("div", { "class": "title" }, [document.createTextNode("OFFLINE REPLAY")]),
      Arena.el("span", { "class": "badge paper" }, [document.createTextNode("REPLAY · no internet needed")]),
      Arena.el("div", { "class": "desc" }, [document.createTextNode(
        "Start immediately using bundled historical demo data. Fully offline, deterministic, perfect for learning the platform and for research.")])
    ]);
    replay.addEventListener("click", function () { chooseReplay(); });
    replay.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); chooseReplay(); } });

    var liveData = Arena.el("div", { "class": "choice", id: "mode-livedata", tabindex: "0", role: "button" }, [
      Arena.el("div", { "class": "ico" }, [document.createTextNode("📡")]),
      Arena.el("div", { "class": "title" }, [document.createTextNode("LIVE MARKET DATA")]),
      Arena.el("span", { "class": "badge warn" }, [document.createTextNode("REAL MARKET DATA · SIMULATED ORDERS")]),
      Arena.el("div", { "class": "desc" }, [document.createTextNode(
        "Stream real market data from Upstox while every order stays simulated inside Arena. Requires a market-data access token.")])
    ]);
    liveData.addEventListener("click", function () { box.appendChild(liveDataForm()); liveData.style.display = "none"; replay.style.display = "none"; });
    liveData.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); liveData.click(); } });

    grid.appendChild(replay);
    grid.appendChild(liveData);
    box.appendChild(grid);

    box.appendChild(Arena.el("div", { "class": "btn-row", style: "margin-top:18px" }, [
      Arena.el("button", { "class": "btn ghost", onclick: function () { step = 0; render(); } }, [document.createTextNode("← Back")])
    ]));
    return box;
  }

  function chooseReplay() {
    paperPlan = { data_source: "replay", symbol: "DEMO", timeframe: "1D", strategy: "moving_average" };
    preparePaper({ data_source: "replay" });
  }

  function liveDataForm() {
    var wrap = document.createElement("div");
    wrap.appendChild(Arena.el("h3", { style: "margin-top:22px" }, [document.createTextNode("Live market-data credentials")]));
    wrap.appendChild(Arena.el("div", { "class": "field" }, [
      Arena.el("label", { "for": "MD_TOKEN" }, [document.createTextNode("MARKET_DATA_ACCESS_TOKEN")]),
      Arena.el("input", { type: "password", id: "MD_TOKEN", autocomplete: "off" }),
      Arena.el("div", { "class": "hint" }, [document.createTextNode("Daily Upstox access token. Arena stores it in your local configuration and never displays it again.")])
    ]));
    wrap.appendChild(Arena.el("div", { "class": "field" }, [
      Arena.el("label", { "for": "MD_SYMBOLS" }, [document.createTextNode("MARKET_DATA_SYMBOLS")]),
      Arena.el("input", { type: "text", id: "MD_SYMBOLS", autocomplete: "off", placeholder: "NSE_EQ|INE002A01018", value: "NSE_EQ|INE002A01018" }),
      Arena.el("div", { "class": "hint" }, [document.createTextNode("Instrument keys, comma-separated. The paper engine trades the first one.")])
    ]));
    var result = Arena.el("div");
    wrap.appendChild(result);
    var btn = Arena.el("button", { "class": "btn primary", id: "md-validate" }, [document.createTextNode("Validate & continue")]);
    btn.addEventListener("click", function () {
      var token = document.getElementById("MD_TOKEN").value.trim();
      var symbols = document.getElementById("MD_SYMBOLS").value.trim();
      result.innerHTML = "";
      if (!token || !symbols) {
        bannerInto(result, "warn", "Missing fields", "Both the access token and at least one instrument key are required.");
        return;
      }
      Arena.setBusy(btn, true, "Validating…");
      Arena.post("/api/wizard/validate-market-data", { token: token, symbols: symbols })
        .then(function (d) {
          Arena.setBusy(btn, false);
          paperPlan = { data_source: "upstox", symbol: d.symbol, timeframe: "1m", strategy: "moving_average" };
          preparePaper({ data_source: "upstox", market_data_access_token: token, market_data_symbols: symbols });
        })
        .catch(function (e) {
          Arena.setBusy(btn, false);
          bannerInto(result, "err", "Could not validate the market-data credential", e.message +
            " Nothing was saved. Check the token (Upstox tokens expire daily) and try again.");
        });
    });
    wrap.appendChild(Arena.el("div", { "class": "btn-row" }, [btn,
      Arena.el("button", { "class": "btn ghost", onclick: function () { render(); } }, [document.createTextNode("← Back")])]));
    return wrap;
  }

  // ----------------------------------------------------------- prepare ----
  function preparePaper(payload) {
    step = 2;
    render();  // viewCredential() renders the "preparing…" panel
    var host = root.querySelector(".card-body");

    Arena.post("/api/wizard/prepare-paper", payload).then(function (d) {
      prepared = d;
      if (d.token_once) Arena.setToken(d.token_once);  // loopback hand-off; never in a URL or log
      host.innerHTML = "";
      host.appendChild(Arena.el("h3", { style: "margin-top:0" }, [document.createTextNode("Paper environment ready")]));
      host.appendChild(Arena.el("div", { "class": "banner ok" }, [
        Arena.el("b", {}, [document.createTextNode("Local credential created automatically.")]),
        document.createTextNode("Arena generated a secure API token and stored it in your local configuration. " +
          "You never need to copy or paste it to use Arena.")
      ]));

      var adv = Arena.el("details", { style: "margin:10px 0" }, [
        Arena.el("summary", { style: "cursor:pointer;color:#7d89a3;font-size:13px" }, [document.createTextNode("Advanced: reveal API token (not needed for normal use)")]),
        Arena.el("div", { "class": "tokenbox", id: "token-reveal", style: "margin-top:8px" }, [document.createTextNode(d.token_once || "(existing token kept — regenerate it from Settings → Security)")])
      ]);
      host.appendChild(adv);

      host.appendChild(Arena.el("div", { "class": "btn-row", style: "margin-top:14px" }, [
        Arena.el("button", { "class": "btn primary", id: "w-to-preflight", onclick: function () { step = 3; render(); } },
          [document.createTextNode("Continue → preflight checks")])
      ]));
    }).catch(function (e) {
      host.innerHTML = "";
      host.appendChild(Arena.el("h3", { style: "margin-top:0" }, [document.createTextNode("Setup could not complete")]));
      host.appendChild(Arena.el("div", { "class": "banner err" }, [
        Arena.el("b", {}, [document.createTextNode("What happened")]),
        document.createTextNode(e.message + " "),
        Arena.el("div", { "class": "detail" }, [document.createTextNode(
          "What Arena did: nothing was changed that it could not redo. You can retry safely.")])
      ]));
      host.appendChild(Arena.el("div", { "class": "btn-row" }, [
        Arena.el("button", { "class": "btn primary", onclick: function () { preparePaper(payload); } }, [document.createTextNode("Retry")]),
        Arena.el("button", { "class": "btn ghost", onclick: function () { step = 1; render(); } }, [document.createTextNode("Choose a different mode")])
      ]));
    });
  }

  // --------------------------------------------------------- preflight ----
  function viewPreflight() {
    var box = document.createElement("div");
    box.appendChild(Arena.el("h3", { style: "margin-top:0" }, [document.createTextNode("Preflight checklist")]));
    box.appendChild(Arena.el("p", { "class": "muted" }, [document.createTextNode(
      "Arena verifies each subsystem before the engine starts.")]));
    var list = Arena.el("div", { "class": "checklist", id: "preflight-list" }, [
      Arena.el("div", { "class": "muted" }, [document.createTextNode("Checking…")])
    ]);
    box.appendChild(list);
    var actions = Arena.el("div", { "class": "btn-row", style: "margin-top:18px" });
    box.appendChild(actions);

    Arena.get("/api/wizard/preflight").then(function (d) {
      list.innerHTML = "";
      var allReady = true;
      d.checks.forEach(function (c) {
        allReady = allReady && c.ok;
        list.appendChild(Arena.el("div", { "class": "check-item" }, [
          Arena.el("span", { "class": "name" }, [document.createTextNode(c.name)]),
          Arena.el("span", { "class": "msg" }, [document.createTextNode(c.detail || "")]),
          Arena.stateBadge(c.ok ? "READY" : (c.severity === "warn" ? "BLOCKED" : "ERROR"))
        ]));
      });
      actions.innerHTML = "";
      var start = Arena.el("button", { "class": "btn success big", id: "w-start-engine" },
        [document.createTextNode("START PAPER TRADING")]);
      start.disabled = !allReady;
      if (!allReady) start.title = "Resolve the blocked checks above first";
      start.addEventListener("click", function () { startPaper(start); });
      actions.appendChild(start);
      actions.appendChild(Arena.el("button", { "class": "btn ghost", onclick: function () { render(); } }, [document.createTextNode("Re-run checks")]));
    }).catch(function (e) {
      list.innerHTML = "";
      bannerInto(list, "err", "Preflight could not run", e.message);
    });
    return box;
  }

  // -------------------------------------------------------------- start ----
  function startPaper(btn) {
    Arena.setBusy(btn, true, "Starting paper engine…");
    Arena.post("/api/paper/start-default", { source: paperPlan ? paperPlan.data_source : "replay" })
      .then(function () {
        Arena.toast("ok", "Paper engine is running. Opening the dashboard…");
        setTimeout(function () { location.href = "/app"; }, 700);
      })
      .catch(function (e) {
        Arena.setBusy(btn, false);
        var host = root.querySelector(".card-body");
        bannerInto(host, "err", "Paper engine could not start", e.message,
          e.data && e.data.detail ? JSON.stringify(e.data.detail, null, 2) : "");
      });
  }

  // ---------------------------------------------------------------- init ----
  function init() {
    Arena.boot().then(function (d) {
      bootInfo = d;
      if (!d.first_run && !d.wizard_forced) {
        // An established install that lands here was sent deliberately
        // (e.g. via /setup); offer going straight in.
        var quick = Arena.el("div", { "class": "banner ok", style: "margin-top:14px" }, [
          Arena.el("b", {}, [document.createTextNode("Arena is already configured.")]),
          document.createTextNode(" ")
        ]);
        quick.appendChild(Arena.el("a", { href: "/app", id: "w-open-app" }, [document.createTextNode("Open the application →")]));
        root.appendChild(quick);
      }
      render();
    }).catch(function (e) {
      root.innerHTML = "";
      root.appendChild(Arena.el("div", { "class": "banner err" }, [
        Arena.el("b", {}, [document.createTextNode("Arena backend not reachable")]),
        document.createTextNode(e.message + " — reload the page; if it persists, check Arena's console window or logs/arena.log.")
      ]));
    });
  }

  document.addEventListener("DOMContentLoaded", init);
})();
