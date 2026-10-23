/* Headless execution harness for app/static/chart/chart.js.
 *
 * There is no browser in the build environment, so this file runs the real
 * chart script against a minimal DOM + 2D-context stub and canned API
 * payloads. It is not a visual test: it proves the script executes end to end
 * (boot -> fetch -> layout -> draw -> ticket -> order) without throwing, that
 * it draws something, and that an order is actually posted with the values the
 * ticket showed.
 *
 * Usage: node tests/js/chart_smoke.mjs [path-to-chart.js]
 */

import { readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const repo = resolve(here, "..", "..");
const scriptPath = process.argv[2] || resolve(repo, "app/static/chart/chart.js");
const htmlPath = resolve(repo, "app/static/chart/index.html");

const html = readFileSync(htmlPath, "utf8");
const code = readFileSync(scriptPath, "utf8");

/* ------------------------------------------------------------- fixtures */

function isoMinutes(offset) {
  const base = Date.parse("2026-10-22T13:30:00Z");
  return new Date(base + offset * 60_000).toISOString();
}

const BARS = 240;
const bars = {
  time: [], open: [], high: [], low: [], close: [], volume: []
};
for (let i = 0; i < BARS; i++) {
  const drift = Math.sin(i / 11) * 1.4 + i * 0.02;
  const open = 100 + drift;
  const close = open + Math.sin(i / 3) * 0.5;
  bars.time.push(isoMinutes(i));
  bars.open.push(open);
  bars.high.push(Math.max(open, close) + 0.4);
  bars.low.push(Math.min(open, close) - 0.4);
  bars.close.push(close);
  bars.volume.push(1000 + (i % 40) * 37);
}

function line(offset, key, label, color, period) {
  return {
    key, label, color, period,
    values: bars.close.map((_, index) =>
      index >= offset ? bars.close[index - offset] : null
    )
  };
}

const CHART = {
  symbol: "AAPL",
  timeframe: "1m",
  currency: "USD",
  paper_only: true,
  live_trading: false,
  source: "engine",
  source_label: "paper engine feed",
  source_timeframe: "1m",
  live: true,
  note: null,
  bars,
  count: BARS,
  bounds: { from: bars.time[0], to: bars.time[BARS - 1], count: BARS },
  overlays: [line(20, "sma20", "SMA 20", "#4aa3ff", 20)],
  oscillators: [{
    key: "rsi14", label: "RSI 14", min: 0, max: 100, guides: [30, 50, 70],
    values: bars.close.map((_, i) => (i > 14 ? 50 + Math.sin(i / 7) * 20 : null))
  }],
  markers: [
    { bar: 40, timestamp: bars.time[40], side: "BUY", price: 101.2, quantity: 3,
      fee: 0.15, reason: "manual", strategy: "manual", order_id: "o1", on_chart: true },
    { bar: 90, timestamp: bars.time[90], side: "SELL", price: 105.1, quantity: 3,
      fee: 0.16, reason: "signal", strategy: "moving_average", order_id: "o2", on_chart: true }
  ],
  orders: [],
  position: {
    quantity: 3, entry_price: 101.2, mark_price: 104.9, entry_fees: 0.15,
    entry_timestamp: bars.time[40], strategy: "manual",
    stop_price: 96.14, target_price: 111.32, unrealized_pnl: 10.95
  },
  session: {
    state: "RUNNING", connection: "CONNECTED", market_status: "OPEN",
    market_open: true, strategy: "moving_average", timeframe: "1m", provider: "alpaca"
  },
  available: {
    timeframes: ["1m", "5m", "15m", "30m", "1h", "4h"],
    indicators: {
      overlays: [
        { key: "sma20", label: "SMA 20", color: "#4aa3ff" },
        { key: "sma50", label: "SMA 50", color: "#f0b429" },
        { key: "ema200", label: "EMA 200", color: "#b47cff" }
      ],
      oscillators: [{ key: "rsi14", label: "RSI 14", min: 0, max: 100 }]
    }
  },
  generated_at: new Date().toISOString()
};

const SYMBOLS = {
  symbols: [{
    symbol: "AAPL", source: "engine", source_timeframe: "1m",
    timeframes: ["1m", "5m", "15m", "30m", "1h", "4h"], live: true
  }],
  default: "AAPL",
  timeframes: ["1m", "5m"],
  paper_only: true,
  live_trading: false
};

const TICKET = {
  symbol: "AAPL", last_price: 104.9, priced_at: bars.time[BARS - 1],
  price_source: "last_closed_bar", cash: 90000, equity: 100314.7,
  daily_pnl: 314.7,
  position: { quantity: 3, entry_price: 101.2, value: 314.7 },
  max_buy_quantity: 850.12, max_sell_quantity: 3, blocked: null,
  limits: {
    max_position_value: 25000, max_open_positions: 3,
    max_daily_loss: 500, max_trades_per_day: 10, allow_short: false
  },
  entries_today: 1, open_positions: 1, paper_only: true, live_trading: false
};

/* ------------------------------------------------------------------ DOM */

const calls = { fillRect: 0, lineTo: 0, stroke: 0, fillText: 0, moveTo: 0 };
let posted = null;
let toasts = 0;

/* A 2D context stub. With CHART_DISPLAY set it also records a display list,
 * which lets a rasteriser outside Node render exactly what the browser would
 * have drawn — the only way to eyeball layout in a browser-less environment. */
const ctx = (() => {
  const state = {
    fillStyle: "#000", strokeStyle: "#000", lineWidth: 1, globalAlpha: 1,
    font: "10px sans-serif", textAlign: "start", textBaseline: "alphabetic",
    lineDash: []
  };
  const stack = [];
  const display = [];
  let path = [];
  let breaks = [];
  let started = false;

  function push(entry) {
    if (process.env.CHART_DISPLAY) {
      display.push({
        ...entry,
        fillStyle: state.fillStyle,
        strokeStyle: state.strokeStyle,
        lineWidth: state.lineWidth,
        globalAlpha: state.globalAlpha,
        font: state.font
      });
    }
  }

  const impl = {
    measureText: (text) => ({ width: String(text).length * 6 }),
    save() { stack.push({ ...state }); },
    restore() { if (stack.length) Object.assign(state, stack.pop()); },
    setTransform() {},
    translate() {},
    scale() {},
    setLineDash(dash) { state.lineDash = dash || []; },
    beginPath() { path = []; breaks = []; started = false; },
    moveTo(x, y) {
      // A moveTo starts a new subpath: renderers must not connect across it.
      if (path.length) breaks.push(path.length);
      path.push([x, y]);
      started = true;
    },
    lineTo(x, y) { if (!started) { path.push([x, y]); started = true; } else path.push([x, y]); },
    closePath() { if (path.length) path.push([path[0][0], path[0][1]]); },
    rect(x, y, w, h) { path = [[x, y], [x + w, y], [x + w, y + h], [x, y + h], [x, y]]; },
    roundRect(x, y, w, h) { impl.rect(x, y, w, h); },
    arc() {},
    fill() { push({ type: "poly", points: path.slice(), closed: true, breaks: [] }); },
    stroke() { push({ type: "poly", points: path.slice(), closed: false, breaks: breaks.slice() }); },
    fillRect(x, y, w, h) { push({ type: "rect", x, y, w, h }); },
    strokeRect(x, y, w, h) { push({ type: "rect", x, y, w, h, stroke: true }); },
    clearRect() {},
    fillText(text, x, y) { push({ type: "text", text: String(text), x, y }); },
    strokeText() {},
    clip() {},
    createLinearGradient() { return { addColorStop() {} }; }
  };

  Object.keys(impl).forEach((key) => {
    const original = impl[key];
    if (key in calls) {
      impl[key] = (...args) => { calls[key] += 1; return original(...args); };
    }
  });

  return new Proxy(state, {
    get(target, prop) {
      if (prop === "__display") return display;
      if (prop === "measureText") return impl.measureText;
      if (prop in impl) return impl[prop];
      if (prop in target) return target[prop];
      return () => undefined;
    },
    set(target, prop, value) { target[prop] = value; return true; }
  });
})();

function makeClassList() {
  const set = new Set();
  return {
    add: (name) => set.add(name),
    remove: (name) => set.delete(name),
    toggle: (name, force) => {
      if (force === undefined) { set.has(name) ? set.delete(name) : set.add(name); }
      else if (force) set.add(name); else set.delete(name);
    },
    contains: (name) => set.has(name)
  };
}

function makeElement(tag, id) {
  const node = {
    tagName: (tag || "div").toUpperCase(),
    id: id || "",
    className: "",
    textContent: "",
    innerHTML: "",
    value: "",
    title: "",
    type: "",
    colSpan: 1,
    children: [],
    style: {},
    dataset: {},
    classList: makeClassList(),
    appendChild(child) { this.children.push(child); return child; },
    removeChild(child) { this.children = this.children.filter((c) => c !== child); },
    remove() {},
    _listeners: {},
    addEventListener(type, fn) { this._listeners[type] = fn; },
    removeEventListener() {},
    getBoundingClientRect() { return { left: 0, top: 0, width: 1180, height: 640 }; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    setAttribute() {},
    getAttribute() { return null; },
    focus() {},
    scrollIntoView() {}
  };
  if (node.tagName === "CANVAS" || id === "chart-canvas") {
    node.width = 1180;
    node.height = 640;
    node.clientWidth = 1180;
    node.clientHeight = 640;
    node.getContext = () => ctx;
  }
  return node;
}

const elements = new Map();
const idPattern = /id="([^"]+)"/g;
let match;
while ((match = idPattern.exec(html)) !== null) {
  const id = match[1];
  const tagMatch = new RegExp(`<(\\w+)[^>]*id="${id}"`).exec(html);
  const node = makeElement(tagMatch ? tagMatch[1] : "div", id);
  if (id === "chart-canvas") {
    node.parentElement = { clientWidth: 1180, clientHeight: 640 };
  }
  // A real <select> reports its first option; the stub has to do the same or
  // the harness would "submit" an empty order type.
  if (id === "order-type") node.value = "MARKET";
  elements.set(id, node);
}

const body = makeElement("body");
Object.defineProperty(body, "contains", { value: () => true });

const document = {
  readyState: "complete",
  title: "",
  body,
  getElementById(id) { return elements.get(id) || null; },
  createElement(tag) { return makeElement(tag); },
  createTextNode(text) { return { nodeType: 3, textContent: String(text) }; },
  addEventListener() {},
  querySelector() { return null; },
  querySelectorAll() { return []; }
};

const storage = new Map([["arena_token", "test-token"]]);
const sessionStorage = {
  getItem: (key) => (storage.has(key) ? storage.get(key) : null),
  setItem: (key, value) => storage.set(key, value),
  removeItem: (key) => storage.delete(key)
};

const window = {
  _listeners: {},
  document,
  sessionStorage,
  devicePixelRatio: 2,
  prompt: () => "test-token",
  confirm: () => true,
  setTimeout: (fn) => { try { fn(); } catch (err) { throw err; } return 0; },
  setInterval: () => 0,
  clearInterval: () => undefined,
  addEventListener(type, fn) { this._listeners[type] = fn; },
  location: { href: "" },
  requestAnimationFrame: (fn) => { fn(0); return 0; }
};

function response(payload) {
  return Promise.resolve({
    ok: true,
    status: 200,
    text: () => Promise.resolve(JSON.stringify(payload))
  });
}

const fetchImpl = (url, options) => {
  if (String(url).indexOf("/chart/symbols") === 0) return response(SYMBOLS);
  if (String(url).indexOf("/chart/data") === 0) {
    const requested = new URL("http://x" + url).searchParams;
    const timeframe = requested.get("timeframe") || "1m";
    const keys = (requested.get("indicators") || "").split(",").filter(Boolean);
    const payload = JSON.parse(JSON.stringify(CHART));
    payload.timeframe = timeframe;
    payload.overlays = payload.overlays.filter((o) => keys.indexOf(o.key) >= 0);
    payload.oscillators = payload.oscillators.filter((o) => keys.indexOf(o.key) >= 0);
    if (keys.indexOf("sma50") >= 0) {
      payload.overlays.push(
        CHART.overlays[0] && { ...CHART.overlays[0], key: "sma50", label: "SMA 50", color: "#f0b429" }
      );
    }
    return response(payload);
  }
  if (String(url).indexOf("/paper/ticket") === 0) return response(TICKET);
  if (String(url).indexOf("/paper/order") === 0) {
    posted = JSON.parse(options.body);
    return response({
      order: { ...posted, id: "manual-1", status: "FILLED", timestamp: bars.time[BARS - 1] },
      fill: { id: "f1", timestamp: bars.time[BARS - 1], price: 104.93, quantity: posted.quantity, fee: 0.05, reason: "manual" },
      status: "FILLED", simulated: true, paper_only: true, live_trading: false,
      priced_at: bars.time[BARS - 1], account: {}, ticket: TICKET
    });
  }
  return Promise.resolve({ ok: false, status: 404, text: () => Promise.resolve('{"detail":"no route"}') });
};

const sandbox = {
  window, document, fetch: fetchImpl, console,
  sessionStorage, location: window.location,
  setTimeout: window.setTimeout, setInterval: window.setInterval,
  clearInterval: window.clearInterval,
  requestAnimationFrame: window.requestAnimationFrame,
  URL, Date, Math, Number, String, JSON, isFinite, isNaN, parseFloat, Infinity, Promise
};

/* --------------------------------------------------------------- execute */

function check(condition, message) {
  if (!condition) {
    console.error("FAIL: " + message);
    process.exitCode = 1;
  }
}

const runner = new Function(
  ...Object.keys(sandbox),
  `${code}\n;return { __booted: true };`
);

let booted = false;
try {
  runner(...Object.keys(sandbox).map((key) => sandbox[key]));
  booted = true;
} catch (error) {
  console.error("FAIL: chart.js threw during boot:\n" + (error && error.stack || error));
  process.exit(1);
}

await new Promise((resolvePromise) => setTimeout(resolvePromise, 60));

check(booted, "script executed");
check(calls.fillRect > 50, "candles and volume were drawn (fillRect=" + calls.fillRect + ")");
check(calls.moveTo > 50, "wick/line primitives were drawn (moveTo=" + calls.moveTo + ")");
check(calls.fillText >= 10, "axis and legend text was drawn (fillText=" + calls.fillText + ")");
check(elements.get("legend-symbol").textContent.indexOf("AAPL") >= 0,
  "legend shows the symbol (got: " + elements.get("legend-symbol").textContent + ")");
check(elements.get("legend-ohlc").textContent.indexOf("O ") === 0,
  "legend shows OHLC");
check(elements.get("source-badge").textContent.indexOf("paper engine") >= 0,
  "source badge names the feed");
check(elements.get("ticket-price-value").textContent.length > 0 &&
  elements.get("ticket-price-value").textContent !== "—",
  "ticket shows a price");
check(elements.get("ticket-position").textContent.indexOf("@") > 0,
  "ticket shows the open position");
check(elements.get("lim-position").textContent !== "—", "risk caps rendered");
check(elements.get("btn-session").textContent.indexOf("RUNNING") > 0,
  "session control reflects the engine state (got " +
  elements.get("btn-session").textContent + ")");
check(elements.get("btn-session").dataset.running === "1",
  "session control is armed to stop a running session");
check(document.title.indexOf("AXIOM paper") > 0, "document title updated");

/* ---------------------------------------------------- trade the ticket */

function click(id) {
  const node = elements.get(id);
  check(node && node._listeners && typeof node._listeners.click === "function",
    id + " has a click handler");
  if (node && node._listeners && node._listeners.click) node._listeners.click({});
}

function settle() {
  return new Promise((resolvePromise) => setTimeout(resolvePromise, 80));
}

check(posted === null, "no order posted before the user acts");

elements.get("qty").value = "2.5";
click("btn-place");
await settle();
check(posted !== null, "clicking PLACE posted an order");
if (posted) {
  check(posted.symbol === "AAPL", "order carries the charted symbol");
  check(posted.side === "BUY", "order carries the selected side");
  check(posted.quantity === 2.5, "order carries the typed quantity (got " + posted.quantity + ")");
  check(posted.order_type === "MARKET", "order type defaults to MARKET");
  check(posted.limit_price === null, "a market order sends no limit price");
}
check(elements.get("toasts").children.length > 0, "a confirmation toast was shown");

/* Switch to SELL: the button must relabel and the side must travel with it. */
posted = null;
click("side-sell");
check(elements.get("btn-place").textContent.indexOf("SELL") >= 0,
  "place button relabels for SELL (got " + elements.get("btn-place").textContent + ")");
elements.get("qty").value = "1";
click("btn-place");
await settle();
check(posted && posted.side === "SELL", "SELL order posted with side=SELL");

/* MAX must use the headroom the ticket reports, not a guess. */
click("side-buy");
click("btn-max");
const maxed = Number(elements.get("qty").value);
check(maxed > 0 && maxed <= TICKET.max_buy_quantity,
  "MAX fills in the ticket headroom (got " + maxed + ", cap " + TICKET.max_buy_quantity + ")");

/* A limit order must send its price, and only then. */
posted = null;
elements.get("order-type").value = "LIMIT";
elements.get("limit-price").value = "99.5";
elements.get("qty").value = "2";
click("btn-place");
await settle();
check(posted && posted.order_type === "LIMIT", "limit order type sent");
check(posted && posted.limit_price === 99.5, "limit price sent (got " + (posted && posted.limit_price) + ")");

/* Clicking the price pane must set the limit price (a click, not a pan). */
elements.get("limit-price").value = "";
const canvasListeners = elements.get("chart-canvas")._listeners;
check(typeof canvasListeners.mousedown === "function", "canvas handles mousedown");
check(typeof canvasListeners.mouseleave === "function", "canvas handles mouseleave");
const winListeners = window._listeners || {};
check(typeof winListeners.mouseup === "function", "window handles mouseup");
canvasListeners.mousedown({ clientX: 600, clientY: 120 });
winListeners.mousemove({ clientX: 600, clientY: 120, movementY: 0 });
winListeners.mouseup({ clientX: 600, clientY: 120 });
const clicked = Number(elements.get("limit-price").value);
check(clicked > 0, "a click on the price pane sets a limit price (got " +
  elements.get("limit-price").value + ")");

/* A drag must pan instead of setting a price. */
elements.get("limit-price").value = "keep-me";
canvasListeners.mousedown({ clientX: 300, clientY: 120 });
winListeners.mousemove({ clientX: 520, clientY: 120, movementY: 0 });
winListeners.mouseup({ clientX: 520, clientY: 120 });
check(elements.get("limit-price").value === "keep-me",
  "dragging pans and leaves the limit price alone");

if (process.env.CHART_DISPLAY) {
  writeFileSync(process.env.CHART_DISPLAY, JSON.stringify({
    width: 1180, height: 640, operations: ctx.__display
  }));
  console.log("display list written to " + process.env.CHART_DISPLAY);
}

console.log("chart harness: " + JSON.stringify(calls));
console.log(process.exitCode ? "chart harness: FAILED" : "chart harness: OK");
