"""The first-run credential screen.

The same inline-string approach as the feature console, so the page ships inside
the package and inside a frozen executable without package-data rules. The test
suite scans every file under ``app/`` for invalid escape sequences, so a stray
backslash here fails the build rather than becoming a future SyntaxError.
"""

from app import __version__

PAGE = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark">
<title>Arena __VERSION__ — Setup</title><style>
:root{
  --bg:#010806; --bg-2:#03130d; --panel:rgba(5,38,26,.52); --panel-2:rgba(2,16,11,.72);
  --line:rgba(0,255,156,.20); --line-2:rgba(0,255,156,.45); --line-3:rgba(0,255,156,.75);
  --cash:#14f195; --green:#00ff9c; --mint:#8affce; --dim:#3f7d63; --muted:#5e9c81;
  --text:#c6ffe4; --amber:#ffc861; --red:#ff5f6d; --cyan:#5ad2ff;
  --mono:ui-monospace,SFMono-Regular,"JetBrains Mono","Cascadia Mono",Menlo,Consolas,monospace;
  --g-low:rgba(20,241,149,.16); --g-mid:rgba(20,241,149,.34); --g-high:rgba(20,241,149,.58);
  --scan:.028; --vig:.48;
}
*{box-sizing:border-box}
body{margin:0;min-height:100%;background:
    radial-gradient(1200px 600px at 50% -10%,var(--g-mid),transparent 70%),
    radial-gradient(900px 500px at 100% 100%,var(--g-low),transparent 70%),
    linear-gradient(180deg,var(--bg) 0%,var(--bg-2) 60%,#010705 100%);
  color:var(--text);font:13px/1.55 var(--mono);letter-spacing:.02em}
body::before{content:"";position:fixed;inset:0;pointer-events:none;z-index:9;
  background:repeating-linear-gradient(0deg,rgba(0,255,156,var(--scan)) 0 1px,transparent 1px 3px);
  mix-blend-mode:screen}
body::after{content:"";position:fixed;inset:0;pointer-events:none;z-index:10;
  background:radial-gradient(120% 100% at 50% 50%,transparent 55%,rgba(0,0,0,var(--vig)) 100%)}
.wrap{position:relative;z-index:11;max-width:900px;margin:0 auto;padding:26px 20px 60px}
header{display:flex;align-items:center;gap:14px;margin-bottom:6px}
.core{width:36px;height:36px;position:relative;flex:0 0 auto}
.core .ring{position:absolute;inset:0;border-radius:50%;
  border:1px solid var(--line-2);border-top-color:var(--green);border-right-color:var(--dim);
  animation:spin 4.5s linear infinite}
.core .ring.b{inset:5px;border-top-color:var(--dim);border-left-color:var(--cash);
  animation-duration:7s;animation-direction:reverse}
.core .pip{position:absolute;inset:13px;border-radius:50%;background:var(--cash);
  box-shadow:0 0 12px var(--g-high),0 0 26px var(--g-mid);animation:pulse 2.4s ease-in-out infinite}
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes pulse{0%,100%{opacity:.55;transform:scale(.85)}50%{opacity:1;transform:scale(1.12)}}
h1{font-size:16px;margin:0;letter-spacing:.16em;text-transform:uppercase;color:var(--mint)}
h1 small{display:block;font-size:11px;letter-spacing:.08em;color:var(--muted);
  text-transform:none;margin-top:3px}
.lead{color:var(--muted);margin:14px 0 20px;font-size:12.5px}
.panel{border:1px solid var(--line);border-radius:8px;background:var(--panel);
  padding:16px 18px;margin-bottom:16px}
.panel h2{margin:0 0 3px;font-size:14px;color:var(--mint);letter-spacing:.04em}
.panel h2 .step{color:var(--dim);margin-right:8px}
.panel p.desc{margin:0 0 14px;color:var(--muted);font-size:12px}
label.f{display:block;margin-bottom:13px}
label.f span{display:block;font-size:11.5px;color:var(--mint);margin-bottom:4px;font-weight:600}
label.f span em{color:var(--dim);font-style:normal;font-weight:400;margin-left:6px;font-size:10.5px}
label.f i{display:block;color:var(--dim);font-style:normal;font-size:10.5px;margin-top:3px}
input[type=text],input[type=password],select{width:100%;font:inherit;color:var(--text);
  background:var(--panel-2);border:1px solid var(--line);border-radius:6px;padding:8px 10px}
input:focus,select:focus{outline:none;border-color:var(--green);background:var(--g-low)}
.two{display:grid;grid-template-columns:1fr 1fr;gap:0 16px}
@media(max-width:700px){.two{grid-template-columns:1fr}}
button{font:inherit;color:var(--mint);background:var(--panel);border:1px solid var(--line-2);
  border-radius:6px;padding:8px 14px;cursor:pointer;letter-spacing:.04em}
button:hover{border-color:var(--green);background:var(--g-low)}
button.primary{border-color:var(--line-3);color:#02150e;font-weight:700;
  background:linear-gradient(180deg,var(--cash),#0bb874)}
button.primary:hover{background:linear-gradient(180deg,#28ffb0,#0fd584)}
button.primary:disabled{opacity:.5;cursor:not-allowed}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.grow{flex:1}
.hint{color:var(--muted);font-size:11px}
.warnbox{border:1px solid rgba(255,200,97,.45);border-radius:8px;background:rgba(255,200,97,.06);
  padding:13px 16px;margin-bottom:16px;color:#ffe0b0;font-size:12px}
.warnbox b{color:var(--amber)}
.okbox{border:1px solid var(--line-3);border-radius:8px;background:var(--g-low);
  padding:13px 16px;margin-bottom:16px;font-size:12px}
.okbox b{color:var(--green)}
.errbox{border:1px solid rgba(255,95,109,.5);border-radius:8px;background:rgba(255,95,109,.08);
  padding:13px 16px;margin-bottom:16px;color:#ffc9cd;font-size:12px}
table.status{width:100%;border-collapse:collapse;font-size:11.5px}
table.status td{padding:4px 6px;border-bottom:1px solid var(--line)}
table.status td:first-child{color:var(--muted)}
table.status td:last-child{text-align:right}
.yes{color:var(--green)} .no{color:var(--dim)}
code{background:rgba(0,0,0,.4);border:1px solid var(--line);border-radius:4px;padding:1px 5px;
  font-size:11.5px;color:var(--mint)}
.pathbox{background:#000906;border:1px solid var(--line);border-radius:6px;padding:9px 11px;
  font-size:11px;color:var(--muted);word-break:break-all;margin-top:9px}
a{color:var(--cyan)}
ul.tight{margin:8px 0 0 18px;padding:0}
ul.tight li{margin-bottom:5px;color:var(--muted);font-size:11.5px}
.token-show{font-size:13px;color:var(--green);word-break:break-all;background:#000906;
  border:1px solid var(--line-2);border-radius:6px;padding:10px 12px;margin-top:8px}
</style>
<div class="wrap">
<header>
  <div class="core"><div class="ring"></div><div class="ring b"></div><div class="pip"></div></div>
  <h1>Arena Setup<small>version __VERSION__ — first run</small></h1>
</header>

<p class="lead">
  Arena is running. Fill in what you have; you can change everything later on
  this same screen. Nothing here turns on real-money trading.
</p>

<div id="banner"></div>

<div class="panel">
  <h2><span class="step">0</span>See it work right now <em>(no credentials needed)</em></h2>
  <p class="desc">
    Arena ships with sample price data, so you can watch the whole pipeline —
    signals, risk checks, simulated fills — before configuring anything. Paste
    this into a terminal in the Arena folder:
  </p>
  <div class="pathbox">python run.py --mode paper --source replay --symbol DEMO</div>
  <p class="desc" style="margin-top:10px">
    It prints a JSON line per event and keeps running until you press Ctrl+C.
    <b>DEFAULT_SYMBOL must match your data</b>: the bundled sample is
    <code>DEMO</code>, while a live Upstox feed needs an instrument key such as
    <code>NSE_EQ|INE002A01018</code>. A mismatch shows up as
    <code>Invalid symbol</code>.
  </p>
</div>

<div class="panel">
  <h2><span class="step">1</span>Lock the interface</h2>
  <p class="desc">
    Arena's API is password-protected. Generate a token, then paste it when the
    console asks. It is stored in the <code>.env</code> file on this machine and
    never shown again.
  </p>
  <div class="row">
    <button id="gen">Generate a secure token</button>
    <button id="copy" style="display:none">Copy</button>
  </div>
  <div id="tokenshow" style="display:none"><div class="token-show" id="tokenval"></div>
    <div class="hint" style="margin-top:7px">Save this somewhere safe before pressing Save.</div>
  </div>
  <input type="hidden" id="API_TOKEN">
</div>

<div class="panel">
  <h2><span class="step">2</span>Choose what Arena may do</h2>
  <p class="desc">
    Paper trading needs no credentials at all — it is simulated and can run
    right now. Sandbox needs an Upstox sandbox app.
  </p>
  <div class="two">
    <label class="f"><span>BROKER</span>
      <select id="BROKER">
        <option value="LOCAL_PAPER" selected>LOCAL_PAPER — simulated, no broker, no credentials</option>
        <option value="UPSTOX_SANDBOX">UPSTOX_SANDBOX — real API, simulated fills</option>
      </select>
      <i>Live brokers are not selectable here on purpose (see below).</i>
    </label>
    <label class="f"><span>MODE</span>
      <select id="MODE">
        <option value="PAPER" selected>PAPER</option>
      </select>
      <i>MODE=LIVE must be set deliberately, by hand.</i>
    </label>
  </div>
</div>

<div class="panel">
  <h2><span class="step">3</span>Credentials <em>(optional)</em></h2>
  <p class="desc">
    Skip this entirely for paper trading. Fill it in for sandbox or for the
    live app you have registered with Upstox.
  </p>
  <label class="f"><span>UPSTOX_CLIENT_ID <em>Upstox app key</em></span>
    <input type="text" id="UPSTOX_CLIENT_ID" autocomplete="off" placeholder="from the Upstox developer console"></label>
  <label class="f"><span>UPSTOX_CLIENT_SECRET</span>
    <input type="password" id="UPSTOX_CLIENT_SECRET" autocomplete="off" placeholder="kept in .env, never displayed again"></label>
  <label class="f"><span>UPSTOX_REDIRECT_URI</span>
    <input type="text" id="UPSTOX_REDIRECT_URI" autocomplete="off" placeholder="must match the URI registered on your Upstox app"></label>
  <label class="f"><span>MARKET_DATA_ACCESS_TOKEN <em>or UPSTOX_ACCESS_TOKEN</em></span>
    <input type="password" id="MARKET_DATA_ACCESS_TOKEN" autocomplete="off" placeholder="daily token; refresh with: python -m app upstox-login"></label>
  <label class="f"><span>UPSTOX_SANDBOX_ACCESS_TOKEN <em>sandbox only</em></span>
    <input type="password" id="UPSTOX_SANDBOX_ACCESS_TOKEN" autocomplete="off" placeholder="only if BROKER is UPSTOX_SANDBOX"></label>
  <div class="two">
    <label class="f"><span>DEFAULT_SYMBOL</span>
      <input type="text" id="DEFAULT_SYMBOL" autocomplete="off" placeholder="NSE_EQ|INE002A01018">
      <i>Must match your data source. The bundled sample data uses DEMO.</i></label>
    <label class="f"><span>ALERT_WEBHOOK_URL <em>optional</em></span>
      <input type="text" id="ALERT_WEBHOOK_URL" autocomplete="off" placeholder="Slack/Discord webhook for alerts"></label>
  </div>
  <div class="hint">Blank fields are left untouched. Arena never deletes a value you already had.</div>
</div>

<div class="warnbox">
  <b>Live trading is not switched on from this screen — deliberately.</b><br>
  Arena is built so that no single control can start real-money trading. This
  form will not set <code>LIVE_TRADING</code>, a live <code>BROKER</code> or
  <code>MODE</code>, or any risk limit; it refuses those and tells you so. Live
  needs the credentials above, a static IP registered with Upstox, and the
  enablement switches set by hand.
  <ul class="tight">
    <li>Check readiness any time: <code>python -m app live-preflight</code></li>
    <li>Check your configuration file: <code>python tools/live_config_check.py</code></li>
    <li>Full procedure: <code>ARENA_GUIDE.md</code>, section "Going live"</li>
  </ul>
</div>

<div class="row">
  <button class="primary" id="save">Save configuration</button>
  <button id="reload">Reload current values</button>
  <span class="grow"></span>
  <a href="/console">Skip to the console &rarr;</a>
</div>

<div id="result"></div>

<div class="panel" style="margin-top:20px">
  <h2>Where Arena keeps your files</h2>
  <p class="desc">
    A packaged Arena does not write next to the executable — that folder may be
    read-only, and a one-file build unpacks into a temporary directory. Your
    database, data, logs and configuration live here:
  </p>
  <div class="pathbox" id="homepath">…</div>
  <table class="status" id="status"></table>
</div>

<script>
"use strict";
var TOKEN_KEY = "arena_token";
var generated = "";

var FIELDS = ["API_TOKEN","BROKER","MODE","UPSTOX_CLIENT_ID","UPSTOX_CLIENT_SECRET",
              "UPSTOX_REDIRECT_URI","MARKET_DATA_ACCESS_TOKEN","UPSTOX_SANDBOX_ACCESS_TOKEN",
              "DEFAULT_SYMBOL","ALERT_WEBHOOK_URL"];

function esc(t){ return t === null || t === undefined ? "" : String(t); }

function banner(kind, html) {
  var el = document.getElementById("banner");
  el.className = kind === "ok" ? "okbox" : (kind === "err" ? "errbox" : "warnbox");
  el.innerHTML = html;
}
function clearBanner(){ document.getElementById("banner").className = ""; document.getElementById("banner").innerHTML = ""; }

function post(path, body) {
  return fetch(path, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body || {})
  }).then(function (res) {
    return res.json().catch(function(){ return {}; }).then(function (data) {
      if (!res.ok) { var e = new Error(data.detail || ("HTTP " + res.status)); throw e; }
      return data;
    });
  });
}

document.getElementById("gen").onclick = function () {
  post("/setup/generate-token", {}).then(function (d) {
    generated = d.token;
    sessionStorage.setItem(TOKEN_KEY, d.token);
    document.getElementById("tokenval").textContent = d.token;
    document.getElementById("tokenshow").style.display = "block";
    document.getElementById("copy").style.display = "inline-block";
    banner("warn", "<b>Your new API token is shown above.</b> Copy it and keep it safe, " +
      "then press <b>Generate a secure token</b> once more if you need to see it again — " +
      "Arena will not display it again after you save.");
  }).catch(function (e) { banner("err", "<b>Could not generate a token:</b> " + esc(e.message)); });
};

document.getElementById("copy").onclick = function () {
  if (generated && navigator.clipboard) navigator.clipboard.writeText(generated);
};

function collect() {
  var values = {};
  // the generated token wins if the operator made one and did not overwrite
  if (generated) values.API_TOKEN = generated;
  FIELDS.forEach(function (key) {
    var el = document.getElementById(key);
    if (!el) return;
    var v = (el.value || "").trim();
    if (v) values[key] = v;
  });
  return values;
}

document.getElementById("save").onclick = function () {
  var values = collect();
  if (!Object.keys(values).length) {
    banner("warn", "Nothing to save yet — generate a token or fill in a field.");
    return;
  }
  var button = document.getElementById("save");
  button.disabled = true; button.textContent = "Saving…";
  post("/setup/save", {values: values}).then(function (d) {
    var extra = "";
    if (d.refused && d.refused.length) {
      extra = "<br><br>Refused (by design): <code>" + d.refused.join("</code>, <code>") +
              "</code>. Live switches are set by hand, never from this screen.";
    }
    banner("ok", "<b>Saved to " + esc(d.path) + "</b><br>" +
      "Updated: <code>" + (d.updated.join("</code>, <code>") || "none") + "</code>" +
      (d.added.length ? "<br>Added: <code>" + d.added.join("</code>, <code>") + "</code>" : "") +
      "<br><br>" + esc(d.note) + extra +
      "<br><br>Next: <a href='/console'>open the feature console</a> once you have restarted Arena.");
    document.getElementById("result").innerHTML = "";
    loadStatus();
  }).catch(function (e) {
    banner("err", "<b>Not saved.</b> " + esc(e.message));
  }).then(function () {
    button.disabled = false; button.textContent = "Save configuration";
  });
};

document.getElementById("reload").onclick = function () { loadStatus(); location.reload(); };

function loadStatus() {
  return fetch("/setup/status").then(function (r) { return r.json(); }).then(function (d) {
    document.getElementById("homepath").textContent = d.home.arena_home;
    var rows = [
      ["Configuration file", esc(d.env_file_exists ? "present" : "not created yet")],
      ["Running as", d.home.frozen ? "packaged executable" : "source checkout"],
      ["Active broker", esc(d.broker || "?")],
      ["Active mode", esc(d.mode || "?")],
      ["Live trading", d.live_trading ? "ENABLED" : "disabled"]
    ];
    Object.keys(d.present).forEach(function (k) {
      rows.push([esc(k), d.present[k] ? "set" : "not set"]);
    });
    document.getElementById("status").innerHTML = rows.map(function (r) {
      var cls = /ENABLED|present|^set$/.test(r[1]) ? "yes" : "no";
      if (r[0] === "Live trading") cls = d.live_trading ? "yes" : "no";
      return "<tr><td>" + r[0] + "</td><td class='" + cls + "'>" + r[1] + "</td></tr>";
    }).join("");
  }).catch(function (e) {
    document.getElementById("homepath").textContent = "unavailable: " + esc(e.message);
  });
}

(function boot() {
  var token = sessionStorage.getItem(TOKEN_KEY);
  if (token) { generated = token; }
  loadStatus();
})();
</script>
</div>
</html>
"""


def render(diagnostics: dict | None = None) -> str:
    html = PAGE.replace("__VERSION__", __version__)
    if diagnostics and diagnostics.get("arena_home"):
        marker = 'id="homepath">…<'
        html = html.replace(marker, 'id="homepath">' + diagnostics["arena_home"] + "<")
    return html
