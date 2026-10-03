"""The browser console page.

Kept inline as a Python string, like the v1 dashboard, so the page ships inside
the package without needing package-data rules in the build config. The
trade-off is that backslashes in the markup would be Python escapes — the test
suite scans every file under ``app/`` for invalid escape sequences, so a stray
backslash fails the build rather than becoming a future SyntaxError.
"""

from app import __version__

PAGE = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark">
<title>Arena __VERSION__ — Feature Console</title><style>
:root{
  --bg:#010806; --bg-2:#03130d; --panel:rgba(5,38,26,.52); --panel-2:rgba(2,16,11,.72);
  --line:rgba(0,255,156,.20); --line-2:rgba(0,255,156,.45); --line-3:rgba(0,255,156,.75);
  --cash:#14f195; --green:#00ff9c; --mint:#8affce; --dim:#3f7d63; --muted:#5e9c81;
  --text:#c6ffe4; --amber:#ffc861; --red:#ff5f6d; --cyan:#5ad2ff;
  --mono:ui-monospace,SFMono-Regular,"JetBrains Mono","Cascadia Mono",Menlo,Consolas,monospace;
  --g-low:rgba(20,241,149,.16); --g-mid:rgba(20,241,149,.34); --g-high:rgba(20,241,149,.58);
  --scan:.028; --vig:.48;
}
body[data-glow="calm"]{ --g-low:rgba(20,241,149,.05); --g-mid:rgba(20,241,149,.12);
  --g-high:rgba(20,241,149,.22); --scan:.008; --vig:.30}
body[data-glow="vivid"]{ --g-low:rgba(20,241,149,.28); --g-mid:rgba(20,241,149,.55);
  --g-high:rgba(20,241,149,.85); --scan:.055; --vig:.62}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:
    radial-gradient(1200px 600px at 50% -10%,var(--g-mid),transparent 70%),
    radial-gradient(900px 500px at 100% 100%,var(--g-low),transparent 70%),
    linear-gradient(180deg,var(--bg) 0%,var(--bg-2) 60%,#010705 100%);
  color:var(--text);font:13px/1.5 var(--mono);letter-spacing:.02em;min-height:100%}
body::before{content:"";position:fixed;inset:0;pointer-events:none;z-index:9;
  background:repeating-linear-gradient(0deg,rgba(0,255,156,var(--scan)) 0 1px,transparent 1px 3px);
  mix-blend-mode:screen}
body::after{content:"";position:fixed;inset:0;pointer-events:none;z-index:10;
  background:radial-gradient(120% 100% at 50% 50%,transparent 55%,rgba(0,0,0,var(--vig)) 100%)}
header{position:relative;display:flex;align-items:center;gap:14px;flex-wrap:wrap;
  padding:10px 18px;background:linear-gradient(180deg,var(--g-low),rgba(0,0,0,0));
  border-bottom:1px solid var(--line-2)}
.brand{display:flex;align-items:center;gap:10px;margin-right:auto}
.core{width:34px;height:34px;position:relative;flex:0 0 auto}
.core .ring{position:absolute;inset:0;border-radius:50%;
  border:1px solid var(--line-2);border-top-color:var(--green);border-right-color:var(--dim);
  animation:spin 4.5s linear infinite}
.core .ring.b{inset:5px;border-top-color:var(--dim);border-left-color:var(--cash);
  animation-duration:7s;animation-direction:reverse}
.core .pip{position:absolute;inset:12px;border-radius:50%;background:var(--cash);
  box-shadow:0 0 12px var(--g-high),0 0 26px var(--g-mid);animation:pulse 2.4s ease-in-out infinite}
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes pulse{0%,100%{opacity:.55;transform:scale(.85)}50%{opacity:1;transform:scale(1.12)}}
.core.busy .ring{animation-duration:.8s}
.core.busy .pip{animation-duration:.5s;background:var(--amber);
  box-shadow:0 0 12px var(--amber),0 0 26px rgba(255,200,97,.4)}
h1{font-size:14px;margin:0;letter-spacing:.16em;text-transform:uppercase;color:var(--mint)}
h1 small{display:block;font-size:10px;letter-spacing:.1em;color:var(--muted);text-transform:none;
  margin-top:2px}
.pill{border:1px solid var(--line-2);border-radius:999px;padding:3px 10px;font-size:11px;
  color:var(--mint);background:var(--panel-2);white-space:nowrap}
.pill.warn{border-color:rgba(255,200,97,.5);color:var(--amber)}
.pill.ok{border-color:var(--line-3);color:var(--green)}
.pill.link{cursor:pointer}
.pill.link:hover{border-color:var(--green);background:var(--g-low)}
button{font:inherit;color:var(--mint);background:var(--panel);border:1px solid var(--line-2);
  border-radius:6px;padding:6px 12px;cursor:pointer;letter-spacing:.04em}
button:hover{border-color:var(--green);background:var(--g-low)}
button:disabled{opacity:.4;cursor:not-allowed}
button.primary{border-color:var(--line-3);color:#02150e;background:linear-gradient(180deg,var(--cash),#0bb874);
  font-weight:700}
button.primary:hover{background:linear-gradient(180deg,#28ffb0,#0fd584)}
button.danger{border-color:rgba(255,95,109,.55);color:var(--red)}
button.danger:hover{background:rgba(255,95,109,.12)}
main{display:grid;grid-template-columns:320px 1fr;gap:0;height:calc(100vh - 57px)}
aside{border-right:1px solid var(--line);overflow-y:auto;padding:12px;background:var(--panel-2)}
section.pane{overflow-y:auto;padding:16px 18px}
.search{width:100%;font:inherit;color:var(--text);background:var(--panel);border:1px solid var(--line-2);
  border-radius:6px;padding:7px 10px;margin-bottom:10px}
.search:focus{outline:none;border-color:var(--green)}
.grp{font-size:10px;letter-spacing:.18em;text-transform:uppercase;color:var(--muted);
  margin:14px 0 6px}
.cmd{display:block;width:100%;text-align:left;border:1px solid transparent;background:transparent;
  border-radius:6px;padding:7px 9px;cursor:pointer;color:var(--text)}
.cmd:hover{background:var(--g-low);border-color:var(--line)}
.cmd.on{background:var(--g-mid);border-color:var(--line-3)}
.cmd b{display:block;color:var(--mint);font-weight:600;font-size:12px}
.cmd span{display:block;color:var(--muted);font-size:11px;margin-top:2px;line-height:1.35}
.tag{display:inline-block;font-size:9px;letter-spacing:.1em;text-transform:uppercase;
  border:1px solid var(--line-2);border-radius:4px;padding:0 5px;margin-left:6px;color:var(--muted);
  vertical-align:middle}
.tag.net{border-color:rgba(90,210,255,.45);color:var(--cyan)}
.tag.state{border-color:rgba(255,200,97,.5);color:var(--amber)}
.tag.long{border-color:rgba(138,255,206,.4);color:var(--mint)}
.panel{border:1px solid var(--line);border-radius:8px;background:var(--panel);padding:14px;
  margin-bottom:14px}
.panel h2{margin:0 0 4px;font-size:15px;color:var(--mint);letter-spacing:.04em}
.panel p.desc{margin:0 0 12px;color:var(--muted);font-size:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}
label.f{display:block}
label.f span{display:block;font-size:11px;color:var(--muted);margin-bottom:4px}
label.f span b{color:var(--mint);font-weight:600}
label.f em{display:block;color:var(--dim);font-style:normal;font-size:10px;margin-top:3px}
input[type=text],input[type=number],select{width:100%;font:inherit;color:var(--text);
  background:var(--panel-2);border:1px solid var(--line);border-radius:6px;padding:6px 9px}
input:focus,select:focus{outline:none;border-color:var(--green);background:var(--g-low)}
.chk{display:flex;align-items:center;gap:8px;padding:6px 0;cursor:pointer}
.chk input{accent-color:var(--cash);width:15px;height:15px}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.out{background:#000906;border:1px solid var(--line);border-radius:8px;padding:12px;
  white-space:pre-wrap;word-break:break-word;font-size:12px;line-height:1.5;
  max-height:60vh;overflow:auto;margin:0}
.out.err{color:#ffb3b9}
.rc{display:inline-block;border-radius:4px;padding:1px 7px;font-size:11px;margin-right:8px}
.rc.ok{background:rgba(20,241,149,.15);color:var(--green);border:1px solid var(--line-3)}
.rc.bad{background:rgba(255,95,109,.14);color:var(--red);border:1px solid rgba(255,95,109,.5)}
.rc.warn{background:rgba(255,200,97,.14);color:var(--amber);border:1px solid rgba(255,200,97,.5)}
.hint{color:var(--muted);font-size:11px}
.recent{list-style:none;margin:0;padding:0}
.recent li{display:flex;gap:8px;align-items:baseline;padding:4px 0;border-bottom:1px solid var(--line);
  font-size:11px;cursor:pointer}
.recent li:hover{background:var(--g-low)}
.recent code{color:var(--mint);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bar{display:flex;gap:8px;align-items:center;margin:12px 0 14px;flex-wrap:wrap}
.grow{flex:1}
.banner{border:1px solid var(--line-3);border-radius:8px;background:var(--panel-2);
  padding:10px 14px;margin-bottom:14px;font-size:12px;color:var(--mint)}
.banner b{color:var(--green)}
@media(max-width:820px){main{grid-template-columns:1fr;height:auto}
  aside{border-right:none;border-bottom:1px solid var(--line);max-height:40vh}}
</style>
<header>
  <div class="brand">
    <div class="core" id="core"><div class="ring"></div><div class="ring b"></div><div class="pip"></div></div>
    <h1>Arena Feature Console<small>every command, in the browser</small></h1>
  </div>
  <span class="pill" id="counts">loading…</span>
  <span class="pill link" id="glowbtn" title="cycle glow intensity">glow: standard</span>
  <span class="pill link" id="tokenbtn" title="set your API token">token</span>
  <span class="pill" id="health">—</span>
</header>
<main>
  <aside>
    <input class="search" id="q" placeholder="filter commands…" autocomplete="off">
    <div id="list"></div>
  </aside>
  <section class="pane">
    <div class="banner" id="tokenbar" style="display:none">
      <b>Authentication required.</b> Enter the value of <code>API_TOKEN</code>
      from the environment the server is running in.
      <div class="row" style="margin-top:9px">
        <input type="password" id="tokeninput" placeholder="API_TOKEN" autocomplete="off"
               style="flex:1;min-width:220px">
        <button class="primary" id="tokenset">Use token</button>
      </div>
      <div class="hint" id="tokenerr" style="margin-top:6px"></div>
    </div>
    <div class="banner" id="intro">
      This console runs the <b>real</b> <code>python -m app</code> and
      <code>run.py</code> commands for you and shows their exact output. It is a
      front end, not a second implementation — every mode check, lock and refusal
      behaves exactly as it does in the terminal, and nothing here can enable a
      mode your configuration has not already enabled.
    </div>
    <div id="form" style="display:none">
      <div class="panel">
        <h2 id="cname"></h2>
        <p class="desc" id="cdesc"></p>
        <div class="grid" id="fields"></div>
      </div>
      <div class="bar">
        <button class="primary" id="run">Run command</button>
        <button id="clear">Clear</button>
        <span class="grow"></span>
        <label class="hint">timeout
          <select id="timeout" style="width:auto;display:inline-block">
            <option value="30">30s</option>
            <option value="120" selected>2 min</option>
            <option value="300">5 min</option>
            <option value="900">15 min</option>
          </select>
        </label>
      </div>
      <div class="panel">
        <h2>Command</h2>
        <p class="desc" id="cmdline">—</p>
        <div class="row"><button id="copy">Copy command</button></div>
      </div>
      <div id="result"></div>
    </div>
    <div id="recentwrap" style="display:none" class="panel">
      <h2>Recent runs</h2>
      <p class="desc">Click a row to reload its command and parameters.</p>
      <ul class="recent" id="recent"></ul>
    </div>
  </section>
</main>
<script>
"use strict";
var TOKEN_KEY = "arena_token";
var GLOW_KEY = "arena_glow";
var token = sessionStorage.getItem(TOKEN_KEY) || "";
var catalog = null, current = null, recentRuns = [], lastPayload = null;

function H() { return {Authorization: "Bearer " + token}; }

function headers(extra) {
  var h = {"Content-Type": "application/json"};
  if (token) h.Authorization = "Bearer " + token;
  if (extra) for (var k in extra) h[k] = extra[k];
  return h;
}

function showTokenBar(show) {
  document.getElementById("tokenbar").style.display = show ? "block" : "none";
}

function applyToken(value) {
  token = (value || "").trim();
  if (token) sessionStorage.setItem(TOKEN_KEY, token);
  else sessionStorage.removeItem(TOKEN_KEY);
  paintToken();
  showTokenBar(!token);
}

function askToken() {
  var t = prompt("API token (the value of API_TOKEN in your environment):", token || "");
  if (t !== null && t.trim()) {
    applyToken(t);
    load();
    return;
  }
  // prompt() returns null both when the operator cancels AND when the browser
  // blocks modals — which it does inside an iframe without allow-modals, i.e.
  // exactly how this console is often embedded. Never rely on the prompt alone:
  // fall back to an inline field so the token can always be entered.
  applyToken(token);
  load();
}

function paintToken() {
  var el = document.getElementById("tokenbtn");
  el.textContent = token ? "token: set" : "token: not set";
  el.className = "pill link" + (token ? " ok" : " warn");
}

function applyGlow() {
  var mode = sessionStorage.getItem(GLOW_KEY) || "standard";
  document.body.setAttribute("data-glow", mode);
  document.getElementById("glowbtn").textContent = "glow: " + mode;
}

function cycleGlow() {
  var order = ["calm", "standard", "vivid"];
  var now = sessionStorage.getItem(GLOW_KEY) || "standard";
  var next = order[(order.indexOf(now) + 1) % order.length];
  sessionStorage.setItem(GLOW_KEY, next);
  applyGlow();
}

function esc(text) { return text === null || text === undefined ? "" : String(text); }

function api(path, options) {
  return fetch(path, options || {headers: H()}).then(function (res) {
    return res.json().catch(function () { return {detail: "unreadable response"}; })
      .then(function (body) {
        if (!res.ok) {
          var err = new Error(body.detail || body.message || ("HTTP " + res.status));
          err.status = res.status;
          err.body = body;
          throw err;
        }
        return body;
      });
  });
}

function badge(cmd) {
  var out = "";
  if (cmd.reaches_network) out += '<i class="tag net">network</i>';
  if (cmd.state_changing) out += '<i class="tag state">changes state</i>';
  if (cmd.long_running) out += '<i class="tag long">long-running</i>';
  return out;
}

function renderList(filter) {
  var host = document.getElementById("list");
  host.innerHTML = "";
  var needle = (filter || "").toLowerCase();

  function block(title, items) {
    var shown = items.filter(function (c) {
      if (!needle) return true;
      return (c.name + " " + (c.summary || "")).toLowerCase().indexOf(needle) >= 0;
    });
    if (!shown.length) return;
    var h = document.createElement("div");
    h.className = "grp";
    h.textContent = title + " (" + shown.length + ")";
    host.appendChild(h);
    shown.forEach(function (c) {
      var b = document.createElement("button");
      b.className = "cmd" + (current && current.name === c.name && current.program === c.program ? " on" : "");
      b.innerHTML = "<b>" + esc(c.name) + badge(c) + "</b><span>" + esc(c.summary) + "</span>";
      b.onclick = function () { select(c); };
      host.appendChild(b);
    });
  }

  block("CLI commands — python -m app", catalog.cli);
  block("run.py modes", catalog.modes);
}

function select(cmd) {
  current = cmd;
  document.getElementById("form").style.display = "block";
  document.getElementById("cname").textContent = cmd.name;
  document.getElementById("cdesc").textContent = cmd.summary || "";
  document.getElementById("result").innerHTML = "";
  // Build the fields FIRST: preview() reads the live DOM, so rendering the
  // command line before the form would show the PREVIOUS command's arguments.
  buildFields(cmd);
  document.getElementById("cmdline").textContent = preview(cmd);
  renderList(document.getElementById("q").value);
}

function fieldLabel(text, help) {
  return '<span><b>' + esc(text) + "</b>" + (help ? "<em>" + esc(help) + "</em>" : "") + "</span>";
}

function buildFields(cmd) {
  var host = document.getElementById("fields");
  host.innerHTML = "";

  (cmd.positionals || []).forEach(function (p) {
    var wrap = document.createElement("label");
    wrap.className = "f";
    var id = "pos_" + p.name;
    if (p.choices) {
      var opts = ['<option value="">(choose ' + esc(p.name) + ")</option>"];
      p.choices.forEach(function (c) { opts.push('<option value="' + esc(c) + '">' + esc(c) + "</option>"); });
      wrap.innerHTML = fieldLabel(p.name, p.help) +
        '<select id="' + id + '" data-kind="positional" data-name="' + esc(p.name) + '">' +
        opts.join("") + "</select>";
    } else {
      wrap.innerHTML = fieldLabel(p.name, p.help) +
        '<input type="text" id="' + id + '" data-kind="positional" data-name="' + esc(p.name) +
        '" placeholder="' + (p.variadic ? "space separated" : "") + '" autocomplete="off">';
    }
    host.appendChild(wrap);
  });

  (cmd.options || []).forEach(function (o) {
    var wrap = document.createElement("label");
    wrap.className = "f";
    var id = "opt_" + o.dest;
    if (o.is_flag) {
      wrap.className = "chk";
      wrap.innerHTML = '<input type="checkbox" id="' + id + '" data-kind="flag" data-flag="' +
        esc(o.flag) + '"><span><b>' + esc(o.flag) + "</b>" +
        (o.help ? "<em>" + esc(o.help) + "</em>" : "") + "</span>";
    } else if (o.choices) {
      var opts = ['<option value="">(default)</option>'];
      o.choices.forEach(function (c) { opts.push('<option value="' + esc(c) + '">' + esc(c) + "</option>"); });
      wrap.innerHTML = fieldLabel(o.flag, o.help) +
        '<select id="' + id + '" data-kind="option" data-flag="' + esc(o.flag) + '">' +
        opts.join("") + "</select>";
    } else {
      wrap.innerHTML = fieldLabel(o.flag, o.help) +
        '<input type="text" id="' + id + '" data-kind="option" data-flag="' + esc(o.flag) +
        '" placeholder="' + esc(o.default === null || o.default === undefined ? "" : String(o.default)) +
        '" autocomplete="off">';
    }
    host.appendChild(wrap);
  });

  if (!cmd.positionals.length && !cmd.options.length) {
    host.innerHTML = '<p class="hint">This command takes no arguments.</p>';
  }
}

function collect() {
  var args = [];
  Array.prototype.forEach.call(document.querySelectorAll("[data-kind=positional]"), function (el) {
    var value = (el.value || "").trim();
    if (!value) return;
    if (el.tagName === "SELECT") args.push(value);
    // NOTE: the doubled backslash is a Python escape, not a JS one — the
    // browser receives the regex /\\s+/ exactly as written. A single backslash
    // here would be a Python DeprecationWarning now and a SyntaxError later
    // (the test suite scans app/ for that).
    else Array.prototype.push.apply(args, value.split(/\\s+/).filter(Boolean));
  });
  Array.prototype.forEach.call(document.querySelectorAll("[data-kind=flag]"), function (el) {
    if (el.checked) args.push(el.getAttribute("data-flag"));
  });
  Array.prototype.forEach.call(document.querySelectorAll("[data-kind=option]"), function (el) {
    var value = (el.value || "").trim();
    if (value) args.push(el.getAttribute("data-flag"), value);
  });
  return args;
}

function preview(cmd) {
  var base = cmd.program === "app" ? "python -m app " + cmd.name : "run.py --mode " + cmd.name;
  var args = collect();
  return args.length ? base + " " + args.join(" ") : base;
}

function refreshPreview() {
  if (current) document.getElementById("cmdline").textContent = preview(current);
}

function run() {
  if (!current) return;
  if (!token) { askToken(); return; }
  var args = collect();
  var timeout = parseInt(document.getElementById("timeout").value, 10);
  // Remember exactly what was sent, so a recent run can be replayed faithfully
  // (flags included) rather than approximately.
  lastPayload = {program: current.program, command: current.name, args: args};
  var core = document.getElementById("core");
  var button = document.getElementById("run");
  core.className = "core busy";
  button.disabled = true;
  button.textContent = "Running…";
  document.getElementById("result").innerHTML =
    '<div class="panel"><p class="desc">Running ' + esc(preview(current)) + " …</p></div>";

  api("/console/run", {
    method: "POST",
    headers: headers(),
    body: JSON.stringify({program: current.program, command: current.name, args: args, timeout: timeout})
  }).then(showResult).catch(function (err) {
    showResult({ok: false, program: current.program, argv: [], exit_code: null,
                stdout: "", stderr: err.message || String(err), duration_ms: 0,
                timed_out: false, truncated: false, refused: err.status === 409 ? err.message : null});
  }).then(function () {
    core.className = "core";
    button.disabled = false;
    button.textContent = "Run command";
  });
}

function showResult(r) {
  if (r.argv && r.argv.length && lastPayload) {
    recentRuns.unshift({cmd: r.argv.join(" "), payload: lastPayload});
    recentRuns = recentRuns.slice(0, 30);
  }
  paintRecent();

  var cls = r.ok ? "ok" : (r.timed_out ? "warn" : "bad");
  var label = r.ok ? "exit 0" : (r.timed_out ? "stopped" : "exit " + esc(r.exit_code));
  var head = '<div class="panel"><div class="row" style="margin-bottom:8px">' +
    '<span class="rc ' + cls + '">' + label + "</span>" +
    '<span class="hint">' + esc(r.duration_ms) + " ms</span>" +
    (r.truncated ? '<span class="pill warn">output truncated</span>' : "") +
    "</div>" +
    '<div class="out" id="stdout"></div>';
  var body = "";
  if (r.stderr) body += '<h2 style="margin-top:12px">stderr</h2><div class="out err" id="stderr"></div>';
  document.getElementById("result").innerHTML = head + body + "</div>";
  document.getElementById("stdout").textContent = r.stdout || "(no output)";
  if (r.stderr) document.getElementById("stderr").textContent = r.stderr;
}

function paintRecent() {
  var wrap = document.getElementById("recentwrap");
  var ul = document.getElementById("recent");
  if (!recentRuns.length) { wrap.style.display = "none"; return; }
  wrap.style.display = "block";
  ul.innerHTML = "";
  recentRuns.slice(0, 12).forEach(function (item) {
    var li = document.createElement("li");
    var code = document.createElement("code");
    code.textContent = item.cmd;
    li.appendChild(code);
    li.onclick = function () {
      var cmd = catalog.cli.concat(catalog.modes).filter(function (c) {
        return c.name === item.payload.command && c.program === item.payload.program;
      })[0];
      if (!cmd) return;
      select(cmd);
      applyPayload(item.payload.args);
    };
    ul.appendChild(li);
  });
}

function applyPayload(args) {
  // Reconstruct the form from a flat argv list: flags are routed to the field
  // that owns them (`data-flag`), a following bare word becomes that field's
  // value, and everything else fills the positionals in order. An earlier
  // version only restored positionals, so replaying a run silently dropped
  // every flag — which is exactly the kind of quiet wrongness this console is
  // supposed to eliminate.
  var positional = document.querySelectorAll("[data-kind=positional]");
  var next = 0;
  for (var i = 0; i < args.length; i++) {
    var arg = args[i];
    if (arg.charAt(0) === "-") {
      var field = document.querySelector('[data-flag="' + arg + '"]');
      if (!field) continue;
      if (field.getAttribute("data-kind") === "flag") { field.checked = true; continue; }
      var value = args[i + 1];
      if (value !== undefined && value.charAt(0) !== "-") { field.value = value; i++; }
      continue;
    }
    if (next < positional.length) positional[next++].value = arg;
  }
  refreshPreview();
}

function load() {
  api("/console/catalog").then(function (data) {
    catalog = data;
    document.getElementById("counts").textContent =
      data.counts.total + " features — " + data.counts.cli + " CLI, " + data.counts.modes + " modes";
    renderList("");
  }).catch(function (err) {
    if (err.status === 401 || err.status === 503) {
      showTokenBar(true);
      document.getElementById("tokenerr").textContent =
        err.status === 503 ? "No API_TOKEN is configured on the server." : "That token was rejected.";
    }
    document.getElementById("list").innerHTML =
      '<p class="hint">Could not load the catalogue: ' + esc(err.message) + "</p>";
  });
}

function health() {
  fetch("/health").then(function (r) { return r.json(); }).then(function (h) {
    var el = document.getElementById("health");
    var mode = h.execution_mode || h.mode || "?";
    el.textContent = mode;
    el.className = "pill" + (String(mode).indexOf("LIVE") >= 0 ? " warn" : " ok");
  }).catch(function () {});
}

document.getElementById("q").addEventListener("input", function (e) { renderList(e.target.value); });
document.getElementById("run").onclick = run;
document.getElementById("clear").onclick = function () {
  if (!current) return;
  buildFields(current);
  document.getElementById("result").innerHTML = "";
  refreshPreview();
};
document.getElementById("copy").onclick = function () {
  var text = preview(current);
  if (navigator.clipboard) navigator.clipboard.writeText(text);
};
document.getElementById("tokenbtn").onclick = askToken;
document.getElementById("tokenset").onclick = function () {
  applyToken(document.getElementById("tokeninput").value);
  if (!token) {
    document.getElementById("tokenerr").textContent = "Enter a token first.";
    return;
  }
  document.getElementById("tokenerr").textContent = "";
  document.getElementById("tokeninput").value = "";
  load();
};
document.getElementById("tokeninput").addEventListener("keydown", function (e) {
  if (e.key === "Enter") { e.preventDefault(); document.getElementById("tokenset").click(); }
});
document.getElementById("glowbtn").onclick = cycleGlow;
document.addEventListener("input", refreshPreview);
document.addEventListener("change", refreshPreview);

// Rerun with Ctrl/Cmd+Enter, the same reflex as a terminal.
document.addEventListener("keydown", function (e) {
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); run(); }
});

(function boot() {
  applyGlow();
  paintToken();
  health();
  // Ask for the token FIRST when there is none, so the operator is not shown a
  // red authentication error before being offered the chance to authenticate.
  if (token) load();
  else askToken();
})();
</script>
</html>
"""


def render() -> str:
    return PAGE.replace("__VERSION__", __version__)
