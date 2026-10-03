/* ===========================================================================
 * ARENA CORE — shared frontend runtime (3.1.0)
 *
 * This file is a STATIC asset: it is served byte-for-byte from app/static and
 * never passes through a Python string template. That is a deliberate fix for
 * the 3.0.5 incident where inline JavaScript inside a triple-quoted Python
 * string was corrupted by backslash escaping. Keep it that way — no server
 * templating in this file, ever. Configuration comes from /api/boot.
 * =========================================================================== */
"use strict";

var Arena = (function () {
  var TOKEN_KEY = "arena_token";

  // ------------------------------------------------------------- token ----
  function getToken() {
    try { return sessionStorage.getItem(TOKEN_KEY) || ""; } catch (e) { return ""; }
  }
  function setToken(token) {
    try {
      if (token) sessionStorage.setItem(TOKEN_KEY, token);
      else sessionStorage.removeItem(TOKEN_KEY);
    } catch (e) { /* storage may be unavailable; the app still works in-memory */ }
  }

  // --------------------------------------------------------------- api ----
  function api(method, path, body, opts) {
    opts = opts || {};
    var headers = { "Content-Type": "application/json" };
    var token = getToken();
    if (token) headers["Authorization"] = "Bearer " + token;
    return fetch(path, {
      method: method,
      headers: headers,
      body: body === undefined ? undefined : JSON.stringify(body)
    }).then(function (res) {
      return res.text().then(function (text) {
        var data = null;
        if (text) { try { data = JSON.parse(text); } catch (e) { data = { raw: text }; } }
        if (!res.ok) {
          var err = new Error(humanError(res.status, data));
          err.status = res.status;
          err.data = data;
          throw err;
        }
        return data;
      });
    });
  }
  var get = function (p) { return api("GET", p); };
  var post = function (p, b) { return api("POST", p, b || {}); };

  function humanError(status, data) {
    var detail = data && (data.detail || data.error || data.message);
    if (typeof detail === "object" && detail !== null) {
      try { detail = JSON.stringify(detail); } catch (e) { detail = String(detail); }
    }
    if (detail) return String(detail);
    if (status === 401) return "Not authorized — the session token is missing or wrong.";
    if (status === 403) return "This action is not allowed in the current mode.";
    if (status === 404) return "Arena could not find that resource.";
    if (status === 409) return "Conflict: another operation is already in progress.";
    if (status === 503) return "This function needs an API token that is not configured yet.";
    return "Request failed (HTTP " + status + ").";
  }

  // -------------------------------------------------------------- boot ----
  function boot() { return get("/api/boot"); }

  // --------------------------------------------------------------- dom ----
  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        if (k === "class") node.className = attrs[k];
        else if (k === "html") node.innerHTML = attrs[k];
        else if (k === "onclick") node.addEventListener("click", attrs[k]);
        else if (k === "onchange") node.addEventListener("change", attrs[k]);
        else if (k === "text") node.textContent = attrs[k];
        else if (attrs[k] !== null && attrs[k] !== undefined) node.setAttribute(k, attrs[k]);
      });
    }
    (children || []).forEach(function (c) {
      if (c === null || c === undefined) return;
      node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return node;
  }

  function esc(text) {
    // Escape for text content. We build DOM with createElement + textContent
    // everywhere, so this is only for the few places we compose strings.
    return String(text === null || text === undefined ? "" : text)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // ----------------------------------------------------------- numbers ----
  function fmtNum(value, digits) {
    if (value === null || value === undefined || isNaN(value)) return "—";
    var n = Number(value);
    if (!isFinite(n)) return "—";
    return n.toLocaleString(undefined, {
      minimumFractionDigits: digits === undefined ? 2 : digits,
      maximumFractionDigits: digits === undefined ? 2 : digits
    });
  }
  function fmtMoney(value, currency) {
    if (value === null || value === undefined || isNaN(value)) return "—";
    var sym = currency === "INR" ? "₹" : (currency === "USD" ? "$" : (currency || "") + " ");
    return sym + fmtNum(value);
  }
  function fmtPct(fraction, digits) {
    if (fraction === null || fraction === undefined || isNaN(fraction)) return "—";
    return (Number(fraction) * 100).toFixed(digits === undefined ? 2 : digits) + "%";
  }
  function fmtTime(iso) {
    if (!iso) return "—";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso);
    return d.toLocaleString();
  }
  function timeAgo(iso) {
    if (!iso) return "never";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso);
    var s = Math.max(0, (Date.now() - d.getTime()) / 1000);
    if (s < 5) return "just now";
    if (s < 60) return Math.floor(s) + "s ago";
    if (s < 3600) return Math.floor(s / 60) + "m ago";
    if (s < 86400) return Math.floor(s / 3600) + "h ago";
    return Math.floor(s / 86400) + "d ago";
  }
  function pnlClass(value) {
    if (value === null || value === undefined || isNaN(value)) return "";
    return Number(value) > 0 ? "pos" : (Number(value) < 0 ? "neg" : "");
  }

  // ------------------------------------------------------------- state ----
  function stateBadge(state) {
    var map = {
      "RUNNING": "ok", "READY": "ok", "CONNECTED": "ok", "COMPLETED": "ok",
      "PAUSED": "warn", "STARTING": "info", "CONNECTING": "info", "STALE": "warn",
      "STOPPED": "neutral", "OFFLINE": "neutral", "DISCONNECTED": "neutral", "IDLE": "neutral",
      "ERROR": "danger", "BLOCKED": "danger", "LOCKED": "danger", "FAILED": "danger",
      "FILLED": "ok", "PARTIALLY_FILLED": "info", "NEW": "info", "CANCELLED": "neutral",
      "REJECTED": "danger", "ACTIVE": "ok", "PROBATION": "warn", "SHADOW": "warn", "RETIRED": "neutral"
    };
    var key = String(state || "UNKNOWN").toUpperCase();
    var cls = map[key] || "neutral";
    var span = el("span", { "class": "badge " + cls }, [el("span", { "class": "dot" }), document.createTextNode(key)]);
    return span;
  }

  function modeBadge(boot) {
    var broker = String((boot && boot.broker) || "LOCAL_PAPER").toUpperCase();
    var cls, label;
    if (broker === "UPSTOX_LIVE" && boot.live_trading) { cls = "live"; label = "LIVE — REAL MONEY"; }
    else if (broker === "UPSTOX_SANDBOX") { cls = "sandbox"; label = "SANDBOX ORDERS"; }
    else if (String((boot && boot.data_source) || "") === "upstox") { cls = "paper"; label = "PAPER — LIVE MARKET DATA / SIMULATED ORDERS"; }
    else { cls = "paper"; label = "PAPER — SIMULATED"; }
    return el("span", { "class": "badge lg " + cls }, [el("span", { "class": "dot" }), document.createTextNode(label)]);
  }

  // ------------------------------------------------------------ toasts ----
  function toast(kind, message) {
    var host = document.getElementById("toasts");
    if (!host) return;
    var node = el("div", { "class": "toast " + (kind || "info") }, [document.createTextNode(message)]);
    host.appendChild(node);
    setTimeout(function () {
      node.style.opacity = "0";
      node.style.transition = "opacity .4s";
      setTimeout(function () { node.remove(); }, 420);
    }, kind === "err" ? 9000 : 5200);
  }

  // ------------------------------------------------------------ dialog ----
  function confirmDialog(title, message, okLabel, danger, onOk) {
    var backdrop = el("div", { "class": "dialog-backdrop" });
    var okBtn = el("button", { "class": "btn " + (danger ? "danger" : "primary"), "id": "dlg-ok" }, [document.createTextNode(okLabel || "Confirm")]);
    var cancelBtn = el("button", { "class": "btn ghost" }, [document.createTextNode("Cancel")]);
    var dialog = el("div", { "class": "dialog", role: "dialog", "aria-modal": "true" }, [
      el("h3", {}, [document.createTextNode(title)]),
      el("p", {}, [document.createTextNode(message)]),
      el("div", { "class": "btn-row", style: "margin-top:14px" }, [okBtn, cancelBtn])
    ]);
    backdrop.appendChild(dialog);
    document.body.appendChild(backdrop);
    function close() { backdrop.remove(); document.removeEventListener("keydown", onKey); }
    function onKey(e) { if (e.key === "Escape") close(); }
    document.addEventListener("keydown", onKey);
    cancelBtn.addEventListener("click", close);
    okBtn.addEventListener("click", function () { close(); onOk(); });
    okBtn.focus();
  }

  // ------------------------------------------------------------ charts ----
  // Minimal, honest SVG line charts. Axes, tooltips and empty-data handling;
  // no decorative animation. A missing point renders as a gap, never as zero.
  function lineChart(container, series, opts) {
    opts = opts || {};
    container.innerHTML = "";
    var points = (series || []).filter(function (p) {
      return p && p.y !== null && p.y !== undefined && !isNaN(p.y);
    });
    if (!points.length) {
      container.appendChild(el("div", { "class": "empty" }, [
        el("span", { "class": "ico" }, [document.createTextNode("◔")]),
        document.createTextNode(opts.emptyText || "No data yet — values appear as soon as they are recorded.")
      ]));
      return;
    }
    var w = opts.width || 560, h = opts.height || 180, pad = 34;
    var ys = points.map(function (p) { return Number(p.y); });
    var min = Math.min.apply(null, ys), max = Math.max.apply(null, ys);
    if (min === max) { min -= 1; max += 1; }
    var span = max - min;
    min -= span * 0.08; max += span * 0.08;
    var n = points.length;
    function x(i) { return pad + (n === 1 ? (w - 2 * pad) / 2 : i * (w - 2 * pad) / (n - 1)); }
    function y(v) { return h - pad - (v - min) * (h - 2 * pad) / (max - min); }
    var path = points.map(function (p, i) {
      return (i === 0 ? "M" : "L") + x(i).toFixed(1) + " " + y(Number(p.y)).toFixed(1);
    }).join(" ");
    var color = opts.color || "#60a5fa";
    var svgNS = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(svgNS, "svg");
    svg.setAttribute("viewBox", "0 0 " + w + " " + h);
    svg.setAttribute("width", "100%");
    svg.setAttribute("height", String(h));
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", opts.label || "chart");

    // horizontal grid + axis labels (5 ticks)
    for (var t = 0; t <= 4; t++) {
      var gy = pad + t * (h - 2 * pad) / 4;
      var val = max - t * (max - min) / 4;
      var line = document.createElementNS(svgNS, "line");
      line.setAttribute("x1", pad); line.setAttribute("x2", w - pad + 6);
      line.setAttribute("y1", gy); line.setAttribute("y2", gy);
      line.setAttribute("stroke", "#232b3d"); line.setAttribute("stroke-width", "1");
      svg.appendChild(line);
      var label = document.createElementNS(svgNS, "text");
      label.setAttribute("x", pad - 6); label.setAttribute("y", gy + 3);
      label.setAttribute("text-anchor", "end");
      label.setAttribute("font-size", "10"); label.setAttribute("fill", "#7d89a3");
      label.setAttribute("font-family", "monospace");
      label.textContent = opts.format ? opts.format(val) : val.toFixed(2);
      svg.appendChild(label);
    }
    var pathEl = document.createElementNS(svgNS, "path");
    pathEl.setAttribute("d", path);
    pathEl.setAttribute("fill", "none");
    pathEl.setAttribute("stroke", color);
    pathEl.setAttribute("stroke-width", "2");
    pathEl.setAttribute("stroke-linejoin", "round");
    svg.appendChild(pathEl);

    // hover: nearest-point tooltip via an invisible wide tracking rect
    var tip = el("div", { "class": "chart-tip", style: "position:absolute;display:none;pointer-events:none;background:#161d2c;border:1px solid #2e3950;border-radius:8px;padding:6px 9px;font-size:11.5px;font-family:monospace;color:#e6ebf4;z-index:20;white-space:nowrap" });
    var wrap = el("div", { style: "position:relative" }, [svg, tip]);
    svg.addEventListener("mousemove", function (ev) {
      var rect = svg.getBoundingClientRect();
      var fx = (ev.clientX - rect.left) * (w / rect.width);
      var i = Math.round((fx - pad) / ((w - 2 * pad) / Math.max(1, n - 1)));
      i = Math.max(0, Math.min(n - 1, i));
      var p = points[i];
      tip.textContent = (p.x || ("#" + (i + 1))) + "  →  " + (opts.format ? opts.format(Number(p.y)) : p.y);
      tip.style.display = "block";
      tip.style.left = Math.min(rect.width - 150, ev.clientX - rect.left + 12) + "px";
      tip.style.top = Math.max(0, ev.clientY - rect.top - 34) + "px";
    });
    svg.addEventListener("mouseleave", function () { tip.style.display = "none"; });
    container.appendChild(wrap);
  }

  // ------------------------------------------------------------- misc ----
  function setBusy(button, busy, busyLabel) {
    if (!button) return;
    if (busy) {
      button.dataset.label = button.innerHTML;
      button.disabled = true;
      button.innerHTML = "";
      var spin = el("span", { "class": "spin" });
      button.appendChild(spin);
      button.appendChild(document.createTextNode(busyLabel || "Working…"));
    } else {
      button.disabled = false;
      if (button.dataset.label) button.innerHTML = button.dataset.label;
    }
  }

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise(function (resolve) {
      var ta = el("textarea", { style: "position:fixed;opacity:0" });
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); } catch (e) { /* best effort */ }
      ta.remove();
      resolve();
    });
  }

  return {
    getToken: getToken, setToken: setToken,
    api: api, get: get, post: post, boot: boot,
    el: el, esc: esc,
    fmtNum: fmtNum, fmtMoney: fmtMoney, fmtPct: fmtPct, fmtTime: fmtTime,
    timeAgo: timeAgo, pnlClass: pnlClass,
    stateBadge: stateBadge, modeBadge: modeBadge,
    toast: toast, confirmDialog: confirmDialog, lineChart: lineChart,
    setBusy: setBusy, copyText: copyText
  };
})();
