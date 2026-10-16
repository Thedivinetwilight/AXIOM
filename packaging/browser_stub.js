/* ===========================================================================
 * Arena browser stub + wizard driver (packaging verification only).
 *
 * This is NOT shipped with the product. It exists because the release gate
 * must prove that the frontend JavaScript actually initializes and drives the
 * backend — HTTP 200 on /setup is not success. Where a real browser engine
 * (Playwright/Selenium) is unavailable, this stub provides just enough DOM
 * for Arena's static JS files to run, and drives the REAL wizard flow:
 *
 *   load /setup shell -> arena-core.js + arena-wizard.js
 *   click [START WITH PAPER TRADING]
 *   click [OFFLINE REPLAY]
 *   (prepare-paper happens over real HTTP against the running build)
 *   click [Continue -> preflight checks]
 *   click [START PAPER TRADING]
 *   verify the engine was actually started over HTTP
 *
 * Usage: node browser_stub.js <base-url> <core.js> <wizard.js>
 * Prints a JSON report on stdout; exit 0 only if every step succeeded.
 * =========================================================================== */
"use strict";

const fs = require("fs");
const http = require("http");
const https = require("https");

const BASE = process.argv[2];
const SCRIPTS = process.argv.slice(3);
if (!BASE || SCRIPTS.length === 0) {
  console.error("usage: node browser_stub.js <base-url> <script...>");
  process.exit(2);
}

const report = { steps: [], ok: true };
function step(name, ok, detail) {
  report.steps.push({ name, ok: !!ok, detail: String(detail === undefined ? "" : detail).slice(0, 400) });
  if (!ok) report.ok = false;
  console.error("[stub] " + (ok ? "PASS " : "FAIL ") + name + (detail ? " — " + String(detail).slice(0, 160) : ""));
}

// ------------------------------------------------------------------ fetch --
function stubFetch(url, opts) {
  opts = opts || {};
  const full = /^https?:/.test(url) ? url : BASE + url;
  const mod = full.startsWith("https") ? https : http;
  return new Promise((resolve) => {
    const req = mod.request(full, {
      method: opts.method || "GET",
      headers: opts.headers || {}
    }, (res) => {
      let body = "";
      res.on("data", (c) => { body += c; });
      res.on("end", () => {
        resolve({
          ok: res.statusCode >= 200 && res.statusCode < 300,
          status: res.statusCode,
          text: () => Promise.resolve(body),
          json: () => Promise.resolve(JSON.parse(body))
        });
      });
    });
    req.on("error", () => resolve({ ok: false, status: 0, text: () => Promise.resolve(""), json: () => Promise.reject(new Error("network")) }));
    if (opts.body) req.write(opts.body);
    req.end();
  });
}

// -------------------------------------------------------------------- DOM --
function makeClassList(node) {
  return {
    add: (...cs) => { cs.forEach(c => node._classes.add(c)); node._syncClass(); },
    remove: (...cs) => { cs.forEach(c => node._classes.delete(c)); node._syncClass(); },
    toggle: (c, force) => {
      const want = force === undefined ? !node._classes.has(c) : !!force;
      if (want) node._classes.add(c); else node._classes.delete(c);
      node._syncClass();
    },
    contains: (c) => node._classes.has(c)
  };
}

function matches(node, selector) {
  // Text nodes and synthetic nodes never match element selectors.
  if (!node || !node._classes) return false;
  selector = selector.trim();
  if (selector.startsWith(".") ) return node._classes.has(selector.slice(1));
  let m = selector.match(/^\[([\w-]+)(?:=["']?([^"'\]]*)["']?)?\]$/);
  if (m) {
    if (m[2] === undefined) return node._attrs[m[1]] !== undefined;
    return String(node._attrs[m[1]]) === m[2];
  }
  m = selector.match(/^([\w-]+)\[([\w-]+)(?:=["']?([^"'\]]*)["']?)?\]$/);
  if (m) {
    if (node.tagName !== m[1]) return false;
    if (m[3] === undefined) return node._attrs[m[2]] !== undefined;
    return String(node._attrs[m[2]]) === m[3];
  }
  return node.tagName === selector;
}

function createNode(tag) {
  const node = {
    tagName: String(tag || "div").toUpperCase(),
    children: [],
    _attrs: {},
    _classes: new Set(),
    _listeners: {},
    _textContent: "",
    style: {},
    dataset: {},
    value: "",
    disabled: false,
    checked: false,
    parentNode: null,
    _innerHTML: "",
    _syncClass() { node._attrs.class = Array.from(node._classes).join(" "); },
    get className() { return Array.from(node._classes).join(" "); },
    set className(v) {
      node._classes = new Set(String(v || "").split(/\s+/).filter(Boolean));
      node._attrs.class = node.className;
    },
    get textContent() {
      if (node.children.length) return node.children.map(c => c.textContent || "").join("");
      return node._textContent;
    },
    set textContent(v) { node.children = []; node._textContent = String(v); },
    get innerHTML() { return node._innerHTML; },
    set innerHTML(v) {
      node._innerHTML = String(v);
      if (String(v) === "") { node.children = []; node._textContent = ""; }
      // Note: the stub never parses HTML strings. Arena's code only ever
      // assigns "" (clear) here — real content is built with createElement.
    },
    setAttribute(k, v) {
      node._attrs[k] = String(v);
      if (k === "class") node.className = v;
      if (k === "id") allIds.set(String(v), node);
    },
    getAttribute(k) { return node._attrs[k] === undefined ? null : node._attrs[k]; },
    appendChild(child) {
      if (child === null || child === undefined) return child;
      child.parentNode = node;
      node.children.push(child);
      return child;
    },
    removeChild(child) {
      node.children = node.children.filter(c => c !== child);
      return child;
    },
    remove() { if (node.parentNode) node.parentNode.removeChild(node); },
    insertBefore(child, ref) {
      const i = node.children.indexOf(ref);
      child.parentNode = node;
      if (i === -1) node.children.push(child); else node.children.splice(i, 0, child);
      return child;
    },
    addEventListener(type, fn) { (node._listeners[type] = node._listeners[type] || []).push(fn); },
    removeEventListener(type, fn) {
      node._listeners[type] = (node._listeners[type] || []).filter(f => f !== fn);
    },
    dispatch(type, ev) {
      ev = ev || {};
      ev.type = type;
      ev.target = ev.target || node;
      ev.currentTarget = node;
      ev.preventDefault = ev.preventDefault || (() => {});
      (node._listeners[type] || []).slice().forEach(fn => fn.call(node, ev));
    },
    click() { node.dispatch("click"); },
    focus() { lastFocused = node; },
    get firstChild() { return node.children[0] || null; },
    get nextSibling() {
      if (!node.parentNode) return null;
      const sibs = node.parentNode.children;
      return sibs[sibs.indexOf(node) + 1] || null;
    },
    querySelectorAll(sel) {
      const out = [];
      (function walk(n) {
        (n.children || []).forEach(c => { if (matches(c, sel)) out.push(c); walk(c); });
      })(node);
      return out;
    },
    querySelector(sel) { return node.querySelectorAll(sel)[0] || null; },
    contains(other) {
      let n = other;
      while (n) { if (n === node) return true; n = n.parentNode; }
      return false;
    }
  };
  return node;
}

const allIds = new Map();
let lastFocused = null;

const documentNode = createNode("#document");
const bodyNode = createNode("body");
documentNode.appendChild(bodyNode);
documentNode.getElementById = (id) => allIds.get(id) || null;
documentNode.createElement = (tag) => createNode(tag);
documentNode.createElementNS = (_ns, tag) => createNode(tag);
documentNode.createTextNode = (text) => ({ nodeType: 3, textContent: String(text), parentNode: null });
documentNode.body = bodyNode;
documentNode.hidden = false;

const sessionStorageStub = {
  _m: new Map(),
  getItem(k) { return this._m.has(k) ? this._m.get(k) : null; },
  setItem(k, v) { this._m.set(k, String(v)); },
  removeItem(k) { this._m.delete(k); }
};

global.document = documentNode;
global.window = global;
// Node >= 21 defines its own read-only `navigator` global; replace it defensively.
Object.defineProperty(globalThis, "navigator", {
  value: { clipboard: null, userAgent: "arena-browser-stub" },
  configurable: true,
  writable: true
});
global.sessionStorage = sessionStorageStub;
global.location = { href: BASE + "/", hash: "", reload() {}, assign(u) { this.href = u; } };
global.fetch = stubFetch;

// --------------------------------------------------------------- run files --
// Seed the shell element the page would normally carry (the real /setup page
// ships <div id="wizard-root"> in static HTML).
bodyNode.appendChild((() => { const d = createNode("div"); d.setAttribute("id", "wizard-root"); return d; })());

const vm = require("vm");
for (const file of SCRIPTS) {
  const code = fs.readFileSync(file, "utf-8");
  // runInThisContext executes the file exactly like a browser's classic
  // <script> tag: top-level `var` declarations land on the global object.
  // (Indirect eval of "use strict" code would wrongly scope them to the
  // eval, hiding `Arena` from later scripts.)
  new vm.Script(code, { filename: file }).runInThisContext();
}

// ------------------------------------------------------------------- drive --
function waitFor(check, timeoutMs, label) {
  const started = Date.now();
  return new Promise((resolve) => {
    (function poll() {
      let result = null;
      try { result = check(); } catch (e) { result = null; }
      if (result) return resolve(result);
      if (Date.now() - started > timeoutMs) return resolve(null);
      setTimeout(poll, 100);
    })();
  });
}

async function drive() {
  // fire DOMContentLoaded like a browser would after parse
  documentNode.dispatch("DOMContentLoaded");
  step("dom-content-loaded fired", true);

  const welcome = await waitFor(() => documentNode.getElementById("w-start-paper"), 15000);
  step("wizard initialized and rendered the welcome screen", !!welcome,
       welcome ? "button #w-start-paper present" : "timed out waiting for #w-start-paper");
  if (!welcome) return finish();

  welcome.click();
  const replay = await waitFor(() => documentNode.getElementById("mode-replay"), 8000);
  step("mode selection screen appeared", !!replay,
       replay ? "OFFLINE REPLAY choice rendered" : "timed out");
  if (!replay) return finish();

  replay.click();  // triggers prepare-paper over real HTTP
  const toPreflight = await waitFor(() => documentNode.getElementById("w-to-preflight"), 30000);
  step("prepare-paper completed and credential screen rendered", !!toPreflight,
       toPreflight ? "local credential auto-generated" : "timed out waiting for preflight continue");
  if (!toPreflight) return finish();

  toPreflight.click();
  const startEngine = await waitFor(() => documentNode.getElementById("w-start-engine"), 30000);
  const preflightOk = startEngine && !startEngine.disabled;
  step("preflight checklist rendered with all checks READY", !!preflightOk,
       startEngine ? (preflightOk ? "START PAPER TRADING enabled" : "start button disabled (a check failed)") : "timed out");
  if (!preflightOk) return finish();

  startEngine.click();  // POST /api/paper/start-default over real HTTP
  const navigated = await waitFor(() => String(global.location.href).indexOf("/app") !== -1, 60000);
  step("START PAPER TRADING started the engine and navigated to /app", !!navigated,
       navigated ? global.location.href : "timed out waiting for navigation to /app");

  return finish();
}

function finish() {
  console.log(JSON.stringify(report, null, 2));
  process.exit(report.ok ? 0 : 1);
}

drive().catch((e) => { step("driver crashed", false, e && e.stack || String(e)); finish(); });
