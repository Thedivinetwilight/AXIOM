/* AXIOM chart terminal.
 *
 * Reading order:
 *   1. auth + fetch helpers
 *   2. state
 *   3. geometry + scales
 *   4. draw pipeline (grid, volume, candles, overlays, markers, crosshair)
 *   5. interaction (zoom, pan, hover, keyboard)
 *   6. ticket + order placement
 *   7. boot
 *
 * No third-party library and no remote asset is loaded: the page must work
 * offline and inside whatever reverse proxy the operator uses. Everything it
 * draws comes from /chart/data, and every order goes to /paper/order, which
 * fills it in the local paper broker.
 */
(function () {
  "use strict";

  /* ------------------------------------------------------------ helpers */

  var TOKEN_KEY = "arena_token";

  function token() {
    return window.sessionStorage.getItem(TOKEN_KEY) || "";
  }

  function askToken() {
    var value = window.prompt("API token (AXIOM_API_TOKEN):");
    if (value) {
      window.sessionStorage.setItem(TOKEN_KEY, value.trim());
      return true;
    }
    return false;
  }

  function api(path, options) {
    var opts = options || {};
    var headers = { "Content-Type": "application/json" };
    var bearer = token();
    if (bearer) headers.Authorization = "Bearer " + bearer;
    return fetch(path, {
      method: opts.method || "GET",
      headers: headers,
      body: opts.body ? JSON.stringify(opts.body) : undefined
    }).then(function (response) {
      return response.text().then(function (text) {
        var payload = {};
        if (text) {
          try { payload = JSON.parse(text); } catch (err) { payload = { detail: text.slice(0, 200) }; }
        }
        if (response.status === 401 || response.status === 503) {
          window.sessionStorage.removeItem(TOKEN_KEY);
          if (askToken()) return api(path, opts);
        }
        if (!response.ok) {
          var error = new Error(payload.detail || ("HTTP " + response.status));
          error.status = response.status;
          throw error;
        }
        return payload;
      });
    });
  }

  function $(id) { return document.getElementById(id); }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function decimals(value) {
    var abs = Math.abs(value);
    if (abs === 0) return 2;
    if (abs >= 1000) return 2;
    if (abs >= 1) return 3;
    if (abs >= 0.01) return 5;
    return 8;
  }

  function num(value, dp) {
    if (value === null || value === undefined || !isFinite(value)) return "—";
    var places = dp === undefined ? decimals(value) : dp;
    return Number(value).toLocaleString(undefined, {
      minimumFractionDigits: places, maximumFractionDigits: places
    });
  }

  function compact(value) {
    if (value === null || value === undefined || !isFinite(value)) return "—";
    var abs = Math.abs(value);
    if (abs >= 1e9) return (value / 1e9).toFixed(2) + "B";
    if (abs >= 1e6) return (value / 1e6).toFixed(2) + "M";
    if (abs >= 1e3) return (value / 1e3).toFixed(1) + "K";
    return String(Math.round(value));
  }

  function money(value) {
    if (value === null || value === undefined || !isFinite(value)) return "—";
    return num(value, 2);
  }

  function signed(value) {
    if (value === null || value === undefined || !isFinite(value)) return "—";
    return (value > 0 ? "+" : "") + num(value, 2);
  }

  function pad2(value) { return String(value).padStart(2, "0"); }

  function clockOf(iso) {
    var date = new Date(iso);
    if (isNaN(date.getTime())) return "—";
    return pad2(date.getHours()) + ":" + pad2(date.getMinutes());
  }

  function dayOf(iso) {
    var date = new Date(iso);
    if (isNaN(date.getTime())) return "—";
    return date.getFullYear() + "-" + pad2(date.getMonth() + 1) + "-" + pad2(date.getDate());
  }

  function monthDay(iso) {
    var day = dayOf(iso);
    // dayOf() returns a placeholder for an unparsable stamp; keep it as-is.
    return day.length > 5 ? day.slice(5) : day;
  }

  function axisLabel(iso, timeframe) {
    if (timeframe === "1D") return dayOf(iso).slice(2);
    return clockOf(iso);
  }

  function toast(kind, title, detail) {
    var host = $("toasts");
    var node = el("div", "toast " + (kind || ""));
    node.appendChild(el("b", null, title));
    if (detail) node.appendChild(document.createTextNode(detail));
    host.appendChild(node);
    window.setTimeout(function () {
      node.style.opacity = "0";
      node.style.transition = "opacity .25s";
      window.setTimeout(function () { node.remove(); }, 260);
    }, kind === "err" ? 9000 : 5200);
  }

  /* -------------------------------------------------------------- state */

  var COLORS = {
    up: "#26a69a", down: "#ef5350", grid: "#17212c", axis: "#1e2a38",
    text: "#8296ad", textStrong: "#dbe6f3", accent: "#4aa3ff",
    warn: "#f0b429", danger: "#e05c6e", bg: "#0b1017", panel: "#121a24"
  };

  var state = {
    symbol: "",
    timeframe: "",
    limit: 700,
    symbols: [],          // [{symbol, timeframes:[...], source}]
    data: null,
    ticket: null,
    view: null,           // {start, end} float indices into bars
    follow: true,
    pointer: null,        // {x, y} in CSS pixels
    drag: null,
    hoverMarker: null,
    overlays: ["sma20", "sma50"],
    oscillators: ["rsi14"],
    side: "BUY",
    busy: false,
    error: null
  };

  var canvas = null;
  var ctx = null;
  var geo = { w: 0, h: 0, plot: null };

  /* -------------------------------------------------------- chart model */

  function bars() {
    return (state.data && state.data.bars && state.data.bars.time) ? state.data.bars : null;
  }

  function barCount() { return bars() ? bars().time.length : 0; }

  function clampView(view, count) {
    var span = Math.max(20, Math.min(count, view.end - view.start));
    var start = Math.max(0, Math.min(count - span, view.start));
    return { start: start, end: start + span };
  }

  function resetView(count) {
    var span = Math.max(20, Math.min(count, state.limit));
    state.view = { start: Math.max(0, count - span), end: count };
    state.follow = true;
  }

  function syncView() {
    var count = barCount();
    if (!count) { state.view = null; return; }
    if (!state.view) { resetView(count); return; }
    var span = state.view.end - state.view.start;
    if (state.follow) {
      state.view = { start: Math.max(0, count - span), end: count };
    } else {
      state.view = clampView(state.view, count);
    }
  }

  function visibleRange() {
    var view = state.view;
    if (!view) return { from: 0, to: 0 };
    return {
      from: Math.max(0, Math.floor(view.start)),
      to: Math.min(barCount(), Math.ceil(view.end))
    };
  }

  /* ----------------------------------------------------------- geometry */

  function computeGeometry() {
    var w = geo.w, h = geo.h;
    var axisW = 66, timeH = 22, gap = 8;
    var oscH = state.oscillators.length ? 78 : 0;
    var volH = Math.max(44, Math.round(h * 0.16));
    var top = 30;
    var priceH = Math.max(80, h - top - volH - oscH - timeH - gap * 2 - 8);
    var plot = { x: 10, y: top, w: Math.max(40, w - axisW - 18), h: priceH };
    var volume = { x: plot.x, y: plot.y + plot.h + gap, w: plot.w, h: volH - gap };
    var osc = oscH ? { x: plot.x, y: volume.y + volume.h + gap, w: plot.w, h: oscH - gap } : null;
    geo.plot = plot;
    geo.volume = volume;
    geo.osc = osc;
    geo.axisX = plot.x + plot.w + 6;
    geo.axisW = axisW;
    geo.timeY = h - 16;
  }

  function span() {
    var view = state.view;
    return view ? Math.max(1e-6, view.end - view.start) : 1;
  }

  function xOf(index) {
    var plot = geo.plot, view = state.view;
    return plot.x + (index - view.start + 0.5) * (plot.w / span());
  }

  function indexAt(x) {
    var plot = geo.plot, view = state.view;
    var raw = view.start + ((x - plot.x) / plot.w) * span() - 0.5;
    return Math.max(0, Math.min(barCount() - 1, Math.round(raw)));
  }

  function priceBounds() {
    var data = state.data;
    if (!data || !state.view) return { min: 0, max: 1 };
    var range = visibleRange();
    var min = Infinity, max = -Infinity;
    var i;
    for (i = range.from; i < range.to; i++) {
      if (data.bars.low[i] < min) min = data.bars.low[i];
      if (data.bars.high[i] > max) max = data.bars.high[i];
    }
    (data.overlays || []).forEach(function (line) {
      for (var j = range.from; j < range.to; j++) {
        var value = line.values[j];
        if (value === null || value === undefined) continue;
        if (value < min) min = value;
        if (value > max) max = value;
      }
    });
    var position = data.position;
    if (position) {
      [position.entry_price, position.stop_price, position.target_price].forEach(function (level) {
        if (level === null || level === undefined) return;
        if (level < min) min = level;
        if (level > max) max = level;
      });
    }
    if (!isFinite(min) || !isFinite(max)) return { min: 0, max: 1 };
    if (max - min < 1e-9) { max = min + Math.max(1e-6, Math.abs(min) * 0.01); }
    var pad = (max - min) * 0.06;
    return { min: min - pad, max: max + pad };
  }

  function yOf(price, bounds) {
    var plot = geo.plot;
    return plot.y + (bounds.max - price) / (bounds.max - bounds.min) * plot.h;
  }

  /* -------------------------------------------------------------- draw */

  function draw() {
    if (!canvas || !ctx) return;
    computeGeometry();
    var plot = geo.plot, w = geo.w, h = geo.h;
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = COLORS.bg;
    ctx.fillRect(0, 0, w, h);

    var count = barCount();
    if (!count || !state.view) { drawEmptyChrome(); return; }

    var bounds = priceBounds();
    drawGrid(bounds);
    drawVolume(bounds);
    drawCandles(bounds);
    drawOverlays(bounds);
    drawPosition(bounds);
    drawMarkers(bounds);
    drawOscillator();
    drawTimeAxis();
    drawLastPrice(bounds);
    drawCrosshair(bounds);
    updateLegendText();
  }

  function drawEmptyChrome() {
    ctx.fillStyle = COLORS.grid;
    ctx.fillRect(geo.plot.x, geo.plot.y, geo.plot.w, geo.plot.h);
  }

  function drawGrid(bounds) {
    var plot = geo.plot;
    ctx.save();
    ctx.strokeStyle = COLORS.grid;
    ctx.lineWidth = 1;
    ctx.font = "11px system-ui, sans-serif";
    ctx.fillStyle = COLORS.text;
    ctx.textBaseline = "middle";
    var steps = 6;
    for (var i = 0; i <= steps; i++) {
      var y = Math.round(plot.y + (plot.h * i) / steps) + 0.5;
      ctx.beginPath();
      ctx.moveTo(plot.x, y);
      ctx.lineTo(plot.x + plot.w, y);
      ctx.stroke();
      var price = bounds.max - ((bounds.max - bounds.min) * i) / steps;
      ctx.fillText(num(price, decimals(price)), plot.x + plot.w + 8, y);
    }
    ctx.strokeStyle = COLORS.axis;
    ctx.beginPath();
    ctx.moveTo(plot.x + plot.w + 0.5, plot.y - 6);
    ctx.lineTo(plot.x + plot.w + 0.5, geo.volume.y + geo.volume.h);
    ctx.stroke();
    ctx.restore();
  }

  function drawVolume() {
    var data = state.data, range = visibleRange(), pane = geo.volume;
    var maxVolume = 0;
    for (var i = range.from; i < range.to; i++) maxVolume = Math.max(maxVolume, data.bars.volume[i] || 0);
    if (maxVolume <= 0) return;
    var bw = pane.w / span();
    var width = Math.max(1, Math.min(12, bw * 0.66));
    ctx.save();
    for (var j = range.from; j < range.to; j++) {
      var value = data.bars.volume[j] || 0;
      var height = Math.max(0.5, (value / maxVolume) * pane.h);
      var up = data.bars.close[j] >= data.bars.open[j];
      ctx.fillStyle = up ? "rgba(38,166,154,.45)" : "rgba(239,83,80,.45)";
      ctx.fillRect(xOf(j) - width / 2, pane.y + pane.h - height, width, height);
    }
    ctx.fillStyle = COLORS.text;
    ctx.font = "10px system-ui, sans-serif";
    ctx.textBaseline = "top";
    ctx.fillText("VOL " + compact(maxVolume), pane.x, pane.y + 2);
    ctx.restore();
  }

  function drawCandles(bounds) {
    var data = state.data, range = visibleRange(), plot = geo.plot;
    var bw = plot.w / span();
    var width = Math.max(1, Math.min(16, bw * 0.64));
    ctx.save();
    for (var i = range.from; i < range.to; i++) {
      var open = data.bars.open[i], close = data.bars.close[i];
      var high = data.bars.high[i], low = data.bars.low[i];
      var up = close >= open;
      var color = up ? COLORS.up : COLORS.down;
      var x = xOf(i);
      ctx.strokeStyle = color;
      ctx.fillStyle = color;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(Math.round(x) + 0.5, yOf(high, bounds));
      ctx.lineTo(Math.round(x) + 0.5, yOf(low, bounds));
      ctx.stroke();
      var yo = yOf(open, bounds), yc = yOf(close, bounds);
      var top = Math.min(yo, yc);
      var height = Math.max(1, Math.abs(yc - yo));
      ctx.fillRect(Math.round(x - width / 2), Math.round(top), Math.max(1, Math.round(width)), Math.round(height));
    }
    ctx.restore();
  }

  function drawOverlays(bounds) {
    var data = state.data, range = visibleRange();
    ctx.save();
    ctx.lineWidth = 1.4;
    ctx.lineJoin = "round";
    (data.overlays || []).forEach(function (line) {
      ctx.strokeStyle = line.color || COLORS.accent;
      ctx.beginPath();
      var started = false;
      for (var i = range.from; i < range.to; i++) {
        var value = line.values[i];
        if (value === null || value === undefined) { started = false; continue; }
        var x = xOf(i), y = yOf(value, bounds);
        if (!started) { ctx.moveTo(x, y); started = true; } else { ctx.lineTo(x, y); }
      }
      ctx.stroke();
    });
    ctx.restore();
  }

  function drawPosition(bounds) {
    var position = state.data.position;
    if (!position) return;
    var plot = geo.plot;
    var levels = [
      { price: position.entry_price, color: COLORS.accent, label: "entry" },
      { price: position.stop_price, color: COLORS.danger, label: "stop" },
      { price: position.target_price, color: COLORS.up, label: "target" }
    ];
    ctx.save();
    ctx.font = "10px system-ui, sans-serif";
    ctx.textBaseline = "middle";
    levels.forEach(function (level) {
      if (level.price === null || level.price === undefined) return;
      var y = yOf(level.price, bounds);
      if (y < plot.y - 2 || y > plot.y + plot.h + 2) return;
      ctx.strokeStyle = level.color;
      ctx.globalAlpha = 0.85;
      ctx.setLineDash([5, 4]);
      ctx.beginPath();
      ctx.moveTo(plot.x, y);
      ctx.lineTo(plot.x + plot.w, y);
      ctx.stroke();
      ctx.setLineDash([]);
      var text = level.label + " " + num(level.price);
      var width = ctx.measureText(text).width + 10;
      ctx.globalAlpha = 1;
      ctx.fillStyle = level.color;
      ctx.fillRect(plot.x + plot.w - width - 4, y - 8, width, 16);
      ctx.fillStyle = "#06121c";
      ctx.fillText(text, plot.x + plot.w - width + 1, y);
    });
    ctx.restore();
  }

  function markerPoint(marker, bounds) {
    var data = state.data;
    var index = marker.bar;
    if (index === null || index === undefined || index < 0) return null;
    var x = xOf(index);
    if (x < geo.plot.x - 20 || x > geo.plot.x + geo.plot.w + 20) return null;
    var y;
    if (marker.side === "BUY") {
      y = yOf(data.bars.low[index], bounds) + 12;
    } else {
      y = yOf(data.bars.high[index], bounds) - 12;
    }
    return { x: x, y: y };
  }

  function drawMarkers(bounds) {
    var list = state.data.markers || [];
    var orders = state.data.orders || [];
    ctx.save();
    ctx.font = "bold 9px system-ui, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";

    orders.forEach(function (order) {
      var point = markerPoint({ bar: order.bar, side: order.side }, bounds);
      if (!point || order.limit_price === null || order.limit_price === undefined) return;
      var y = yOf(order.limit_price, bounds);
      ctx.strokeStyle = order.side === "BUY" ? "rgba(38,166,154,.7)" : "rgba(239,83,80,.7)";
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(geo.plot.x, y);
      ctx.lineTo(geo.plot.x + geo.plot.w, y);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = COLORS.text;
      ctx.textAlign = "left";
      ctx.fillText("limit " + num(order.limit_price) + " · working", geo.plot.x + 4, y - 8);
      ctx.textAlign = "center";
    });

    list.forEach(function (marker) {
      var point = markerPoint(marker, bounds);
      if (!point) return;
      var buy = marker.side === "BUY";
      var color = buy ? COLORS.up : COLORS.down;
      ctx.fillStyle = color;
      ctx.beginPath();
      if (buy) {
        ctx.moveTo(point.x, point.y - 8);
        ctx.lineTo(point.x - 5, point.y + 1);
        ctx.lineTo(point.x + 5, point.y + 1);
      } else {
        ctx.moveTo(point.x, point.y + 8);
        ctx.lineTo(point.x - 5, point.y - 1);
        ctx.lineTo(point.x + 5, point.y - 1);
      }
      ctx.closePath();
      ctx.fill();
      ctx.fillStyle = "#06121c";
      ctx.fillText(buy ? "B" : "S", point.x, point.y - 1);
    });

    if (state.hoverMarker) {
      var hovered = state.hoverMarker;
      var spot = markerPoint(hovered, bounds);
      if (spot) {
        var lines = [
          hovered.side + " " + num(hovered.quantity, 6) + " @ " + num(hovered.price),
          "fee " + num(hovered.fee, 4) + " · " + hovered.reason,
          clockOf(hovered.timestamp) + " · " + hovered.strategy
        ];
        ctx.font = "11px system-ui, sans-serif";
        var width = 0;
        lines.forEach(function (line) { width = Math.max(width, ctx.measureText(line).width); });
        width += 16;
        var bx = Math.min(spot.x + 10, geo.plot.x + geo.plot.w - width);
        var by = Math.max(geo.plot.y + 4, spot.y - 58);
        ctx.fillStyle = "rgba(10,17,25,.96)";
        ctx.strokeStyle = COLORS.axis;
        ctx.beginPath();
        if (ctx.roundRect) { ctx.roundRect(bx, by, width, 52, 6); } else { ctx.rect(bx, by, width, 52); }
        ctx.fill();
        ctx.stroke();
        ctx.textAlign = "left";
        ctx.textBaseline = "top";
        ctx.fillStyle = COLORS.textStrong;
        lines.forEach(function (line, index) {
          ctx.fillStyle = index === 0 ? COLORS.textStrong : COLORS.text;
          ctx.fillText(line, bx + 8, by + 7 + index * 14);
        });
      }
    }
    ctx.restore();
  }

  function drawOscillator() {
    var pane = geo.osc;
    if (!pane) return;
    var series = (state.data.oscillators || [])[0];
    ctx.save();
    ctx.strokeStyle = COLORS.axis;
    ctx.beginPath();
    ctx.moveTo(pane.x, pane.y);
    ctx.lineTo(pane.x + pane.w, pane.y);
    ctx.stroke();
    if (!series) { ctx.restore(); return; }
    var range = visibleRange();
    var min = series.min, max = series.max;
    function y(value) { return pane.y + (max - value) / (max - min) * pane.h; }
    ctx.strokeStyle = "rgba(130,150,173,.35)";
    ctx.setLineDash([2, 3]);
    (series.guides || []).forEach(function (guide) {
      ctx.beginPath();
      ctx.moveTo(pane.x, y(guide));
      ctx.lineTo(pane.x + pane.w, y(guide));
      ctx.stroke();
    });
    ctx.setLineDash([]);
    ctx.font = "10px system-ui, sans-serif";
    ctx.fillStyle = COLORS.text;
    ctx.textBaseline = "middle";
    ctx.fillText(series.label, pane.x + 2, pane.y + 8);
    ctx.strokeStyle = "#b47cff";
    ctx.lineWidth = 1.3;
    ctx.beginPath();
    var started = false;
    for (var i = range.from; i < range.to; i++) {
      var value = series.values[i];
      if (value === null || value === undefined) { started = false; continue; }
      var x = xOf(i);
      if (!started) { ctx.moveTo(x, y(value)); started = true; } else { ctx.lineTo(x, y(value)); }
    }
    ctx.stroke();
    ctx.restore();
  }

  function drawTimeAxis() {
    var plot = geo.plot, view = state.view;
    var ticks = Math.max(2, Math.min(9, Math.floor(plot.w / 96)));
    ctx.save();
    ctx.font = "10px system-ui, sans-serif";
    ctx.fillStyle = COLORS.text;
    ctx.textBaseline = "top";
    ctx.textAlign = "center";
    var previousDay = null;
    for (var i = 0; i <= ticks; i++) {
      var index = Math.round(view.start + ((view.end - view.start) * i) / ticks);
      index = Math.max(0, Math.min(barCount() - 1, index));
      var x = xOf(index);
      if (x < plot.x - 1 || x > plot.x + plot.w + 1) continue;
      ctx.strokeStyle = COLORS.grid;
      ctx.beginPath();
      ctx.moveTo(Math.round(x) + 0.5, plot.y);
      ctx.lineTo(Math.round(x) + 0.5, geo.volume.y + geo.volume.h);
      ctx.stroke();
      var stamp = state.data.bars.time[index];
      var label = axisLabel(stamp, state.timeframe);
      if (state.timeframe !== "1D" && (i === 0 || dayOf(stamp) !== previousDay)) {
        // On an intraday chart the day boundary is the important label: show
        // month-day once, then the clock, rather than repeating a full date.
        label = monthDay(stamp) + " " + label;
        previousDay = dayOf(stamp);
      }
      ctx.fillText(label, x, geo.timeY);
    }
    ctx.restore();
  }

  function drawLastPrice(bounds) {
    var data = state.data, count = barCount();
    if (!count) return;
    var close = data.bars.close[count - 1];
    var y = yOf(close, bounds);
    var plot = geo.plot;
    if (y < plot.y || y > plot.y + plot.h) return;
    var up = data.bars.close[count - 1] >= data.bars.open[count - 1];
    var color = up ? COLORS.up : COLORS.down;
    ctx.save();
    ctx.strokeStyle = color;
    ctx.globalAlpha = 0.6;
    ctx.setLineDash([2, 3]);
    ctx.beginPath();
    ctx.moveTo(plot.x, y);
    ctx.lineTo(plot.x + plot.w, y);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.globalAlpha = 1;
    var text = num(close);
    ctx.font = "11px system-ui, sans-serif";
    var width = Math.max(geo.axisW - 6, ctx.measureText(text).width + 10);
    ctx.fillStyle = color;
    ctx.fillRect(geo.axisX - 2, y - 8, width, 16);
    ctx.fillStyle = "#06121c";
    ctx.textBaseline = "middle";
    ctx.textAlign = "left";
    ctx.fillText(text, geo.axisX + 3, y);
    ctx.restore();
  }

  function drawCrosshair(bounds) {
    if (!state.pointer) return;
    var plot = geo.plot, x = state.pointer.x, y = state.pointer.y;
    if (x < plot.x || x > plot.x + plot.w || y < plot.y || y > plot.y + plot.h) return;
    var index = indexAt(x);
    ctx.save();
    ctx.strokeStyle = "rgba(130,150,173,.55)";
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    ctx.moveTo(Math.round(xOf(index)) + 0.5, plot.y);
    ctx.lineTo(Math.round(xOf(index)) + 0.5, plot.y + plot.h);
    ctx.moveTo(plot.x, Math.round(y) + 0.5);
    ctx.lineTo(plot.x + plot.w, Math.round(y) + 0.5);
    ctx.stroke();
    ctx.setLineDash([]);

    var price = bounds.max - ((y - plot.y) / plot.h) * (bounds.max - bounds.min);
    ctx.fillStyle = "#26364a";
    ctx.fillRect(geo.axisX - 2, y - 8, geo.axisW - 2, 16);
    ctx.fillStyle = COLORS.textStrong;
    ctx.font = "11px system-ui, sans-serif";
    ctx.textBaseline = "middle";
    ctx.textAlign = "left";
    ctx.fillText(num(price), geo.axisX + 3, y);

    var label = axisLabel(state.data.bars.time[index], state.timeframe);
    ctx.font = "10px system-ui, sans-serif";
    var width = ctx.measureText(label).width + 12;
    ctx.fillStyle = "#26364a";
    ctx.fillRect(xOf(index) - width / 2, geo.timeY - 3, width, 16);
    ctx.fillStyle = COLORS.textStrong;
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    ctx.fillText(label, xOf(index), geo.timeY);
    ctx.restore();
  }

  function updateLegendText() {
    var data = state.data;
    if (!data || !barCount()) return;
    var index = state.pointer ? indexAt(state.pointer.x) : barCount() - 1;
    var open = data.bars.open[index], high = data.bars.high[index];
    var low = data.bars.low[index], close = data.bars.close[index];
    var prev = index > 0 ? data.bars.close[index - 1] : open;
    var change = prev ? ((close - prev) / prev) * 100 : 0;
    $("legend-symbol").textContent = state.symbol + " · " + state.timeframe;
    var ohlc = $("legend-ohlc");
    ohlc.textContent = "O " + num(open) + "  H " + num(high) + "  L " + num(low) + "  C " + num(close) +
      "  " + (change >= 0 ? "+" : "") + change.toFixed(2) + "%";
    ohlc.className = "ohlc " + (change >= 0 ? "up" : "down");
    $("legend-extra").textContent = "VOL " + compact(data.bars.volume[index]) +
      " · " + clockOf(data.bars.time[index]);
    var ind = (data.overlays || []).map(function (line) {
      var value = line.values[index];
      return line.label + " " + (value === null || value === undefined ? "—" : num(value));
    }).join("   ");
    $("legend-ind").textContent = ind;
  }

  /* --------------------------------------------------------- interaction */

  function setPointer(event) {
    var rect = canvas.getBoundingClientRect();
    state.pointer = { x: event.clientX - rect.left, y: event.clientY - rect.top };
  }

  function findMarker(x, y) {
    var data = state.data;
    if (!data) return null;
    var bounds = priceBounds();
    var found = null;
    (data.markers || []).forEach(function (marker) {
      var point = markerPoint(marker, bounds);
      if (!point) return;
      if (Math.abs(point.x - x) < 9 && Math.abs(point.y - y) < 12) found = marker;
    });
    return found;
  }

  function onWheel(event) {
    if (!barCount() || !state.view) return;
    event.preventDefault();
    var rect = canvas.getBoundingClientRect();
    var x = event.clientX - rect.left;
    var pivot = state.view.start + ((x - geo.plot.x) / geo.plot.w) * span();
    var factor = event.deltaY > 0 ? 1.18 : 1 / 1.18;
    var count = barCount();
    var newSpan = Math.max(20, Math.min(count, span() * factor));
    var ratio = (pivot - state.view.start) / span();
    var start = pivot - newSpan * ratio;
    state.view = clampView({ start: start, end: start + newSpan }, count);
    state.follow = state.view.end >= count - 0.5;
    draw();
  }

  function onMouseDown(event) {
    if (!state.view) return;
    state.drag = {
      x: event.clientX,
      start: state.view.start,
      end: state.view.end,
      moved: 0
    };
    canvas.classList.add("dragging");
  }

  function onMouseMove(event) {
    setPointer(event);
    if (state.drag) {
      var dx = event.clientX - state.drag.x;
      state.drag.moved = Math.max(state.drag.moved, Math.abs(dx) + Math.abs(event.movementY || 0));
      var shift = (dx / geo.plot.w) * (state.drag.end - state.drag.start);
      state.view = clampView({ start: state.drag.start - shift, end: state.drag.end - shift }, barCount());
      state.follow = state.view.end >= barCount() - 0.5;
      draw();
      return;
    }
    state.hoverMarker = findMarker(state.pointer.x, state.pointer.y);
    draw();
  }

  function onMouseUp() {
    var drag = state.drag;
    state.drag = null;
    canvas.classList.remove("dragging");
    // A click (not a pan) on the price pane sets the limit price while a
    // LIMIT order is being composed: the fastest way to say "buy here".
    if (!drag || drag.moved > 6 || !state.pointer) return;
    if (!$("order-type") || $("order-type").value !== "LIMIT") return;
    var plot = geo.plot;
    if (state.pointer.x < plot.x || state.pointer.x > plot.x + plot.w) return;
    if (state.pointer.y < plot.y || state.pointer.y > plot.y + plot.h) return;
    var bounds = priceBounds();
    var price = bounds.max - ((state.pointer.y - plot.y) / plot.h) * (bounds.max - bounds.min);
    if (!isFinite(price) || price <= 0) return;
    $("limit-price").value = price.toFixed(decimals(price));
    toast("", "Limit price set", "Clicking the chart set the limit to " + $("limit-price").value + ".");
  }

  function onLeave() {
    state.pointer = null;
    state.hoverMarker = null;
    draw();
  }

  function onKey(event) {
    if (!barCount() || !state.view) return;
    var tag = (event.target && event.target.tagName) || "";
    if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return;
    var count = barCount();
    if (event.key === "+" || event.key === "=") {
      var mid = (state.view.start + state.view.end) / 2;
      var newSpan = Math.max(20, span() / 1.2);
      state.view = clampView({ start: mid - newSpan / 2, end: mid + newSpan / 2 }, count);
    } else if (event.key === "-" || event.key === "_") {
      var mid2 = (state.view.start + state.view.end) / 2;
      var wider = Math.min(count, span() * 1.2);
      state.view = clampView({ start: mid2 - wider / 2, end: mid2 + wider / 2 }, count);
    } else if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      var step = Math.max(1, Math.round(span() * 0.08)) * (event.key === "ArrowLeft" ? -1 : 1);
      state.view = clampView({ start: state.view.start + step, end: state.view.end + step }, count);
      state.follow = false;
    } else if (event.key === "f" || event.key === "F") {
      toggleFollow();
      return;
    } else if (event.key === "r" || event.key === "R") {
      resetView(count);
    } else {
      return;
    }
    event.preventDefault();
    draw();
  }

  function toggleFollow() {
    if (!barCount()) return;
    state.follow = !state.follow;
    if (state.follow) resetView(barCount());
    $("btn-follow").classList.toggle("on", state.follow);
    $("btn-follow").style.color = state.follow ? "#7fc8ef" : "";
    draw();
  }

  /* ------------------------------------------------------------- ticket */

  function renderTicket(ticket) {
    state.ticket = ticket;
    if (!ticket) return;
    $("ticket-price-value").textContent = ticket.last_price === null ? "—" : num(ticket.last_price);
    $("ticket-priced-at").textContent = ticket.priced_at ? "as of " + clockOf(ticket.priced_at) : "";
    var position = ticket.position;
    var positionNode = $("ticket-position");
    positionNode.textContent = position
      ? num(position.quantity, 6) + " @ " + num(position.entry_price)
      : "flat";
    positionNode.className = "v" + (position ? " pos" : "");
    $("ticket-cash").textContent = money(ticket.cash) + " " + (state.data && state.data.currency || "");
    $("ticket-equity").textContent = money(ticket.equity);
    var day = $("ticket-daypnl");
    day.textContent = signed(ticket.daily_pnl);
    day.className = "v " + (ticket.daily_pnl > 0 ? "pos" : ticket.daily_pnl < 0 ? "neg" : "");

    $("lim-position").textContent = ticket.limits.max_position_value ? money(ticket.limits.max_position_value) : "unset (cash bound only)";
    $("lim-open").textContent = ticket.limits.max_open_positions || "unset";
    $("lim-loss").textContent = ticket.limits.max_daily_loss ? money(ticket.limits.max_daily_loss) : "unset";
    $("lim-trades").textContent = ticket.limits.max_trades_per_day
      ? ticket.entries_today + " / " + ticket.limits.max_trades_per_day
      : "unset";

    var blocked = $("ticket-blocked");
    blocked.textContent = ticket.blocked || "";
    blocked.style.display = ticket.blocked ? "block" : "none";

    var maxQty = state.side === "BUY" ? ticket.max_buy_quantity : ticket.max_sell_quantity;
    $("btn-max").title = "use " + num(maxQty, 6);
    $("btn-place").disabled = state.busy || (state.side === "BUY"
      ? (ticket.blocked !== null || maxQty <= 0 || ticket.last_price === null)
      : maxQty <= 0);
    updateEstimate();
  }

  function currentQuantity() {
    var raw = String($("qty").value || "").replace(/,/g, "").trim();
    if (!raw) return null;
    var value = Number(raw);
    if (!isFinite(value) || value <= 0) return null;
    return value;
  }

  function updateEstimate() {
    var ticket = state.ticket;
    var quantity = currentQuantity();
    var node = $("estimate");
    if (!ticket || ticket.last_price === null || quantity === null) {
      node.innerHTML = "notional —";
      return;
    }
    var notional = quantity * ticket.last_price;
    node.innerHTML = quantity.toLocaleString() + " × " + num(ticket.last_price) +
      " = <b>" + money(notional) + "</b> notional · fees and slippage are applied at fill";
  }

  function setSide(side) {
    state.side = side;
    $("side-buy").classList.toggle("on", side === "BUY");
    $("side-sell").classList.toggle("on", side === "SELL");
    var place = $("btn-place");
    place.textContent = "PLACE SIMULATED " + side;
    place.className = "place " + (side === "BUY" ? "buy" : "sell");
    if (state.ticket) renderTicket(state.ticket);
  }

  function placeOrder() {
    if (state.busy) return;
    var quantity = currentQuantity();
    if (quantity === null) { toast("err", "Quantity required", "Enter a positive quantity."); return; }
    var orderType = $("order-type").value;
    var limitPrice = null;
    if (orderType === "LIMIT") {
      limitPrice = Number(String($("limit-price").value || "").replace(/,/g, ""));
      if (!isFinite(limitPrice) || limitPrice <= 0) {
        toast("err", "Limit price required", "A limit order needs a positive price.");
        return;
      }
    }
    var priceText = state.ticket && state.ticket.last_price !== null ? num(state.ticket.last_price) : "the last close";
    var prompt = state.side + " " + quantity + " " + state.symbol + " " + orderType.toLowerCase() +
      (orderType === "LIMIT" ? " @ " + num(limitPrice) : " (last close " + priceText + ")") +
      "\n\nThis is a SIMULATED paper order. Nothing is sent to a broker.";
    if (!window.confirm(prompt)) return;

    state.busy = true;
    $("btn-place").disabled = true;
    api("/paper/order", {
      method: "POST",
      body: {
        symbol: state.symbol,
        side: state.side,
        quantity: quantity,
        order_type: orderType,
        limit_price: limitPrice
      }
    }).then(function (result) {
      var fill = result.fill;
      if (fill) {
        toast("ok", "Simulated " + state.side + " filled",
          num(fill.quantity, 6) + " @ " + num(fill.price) + " · fee " + num(fill.fee, 4));
      } else {
        toast("", "Simulated order working",
          "It will fill when a bar trades through " + num(limitPrice) + ".");
      }
      return refreshAll();
    }).catch(function (error) {
      toast("err", "Order rejected", error.message);
    }).then(function () {
      state.busy = false;
      if (state.ticket) renderTicket(state.ticket);
    });
  }

  /* --------------------------------------------------------------- load */

  function applyData(data) {
    var previousCount = barCount();
    state.data = data;
    if (data.timeframe && data.timeframe !== state.timeframe) {
      state.timeframe = data.timeframe;
      state.view = null;
    }
    var count = barCount();
    if (!count) {
      state.view = null;
      $("empty-state").classList.add("show");
      $("empty-reason").textContent = data.note || "No bars for this instrument.";
    } else {
      $("empty-state").classList.remove("show");
      if (!state.view) resetView(count);
      else if (state.follow) {
        var spanNow = state.view.end - state.view.start;
        state.view = { start: Math.max(0, count - spanNow), end: count };
      } else if (count !== previousCount) {
        state.view = clampView(state.view, count);
      }
    }
    var available = (data.available && data.available.timeframes) || [];
    if (available.length) buildTimeframeButtons(available, state.timeframe);
    renderChrome(data);
    draw();
    renderFills(data.markers || []);
  }

  function renderChrome(data) {
    var source = $("source-badge");
    source.textContent = "source " + (data.source_label || "—");
    source.className = "badge " + (data.live ? "ok" : "");
    source.title = data.note || "";
    var session = data.session || {};
    var conn = $("conn");
    conn.textContent = "market " + (session.market_status || (data.live ? "LIVE" : "no session"));
    conn.className = "badge " + (session.market_open === true ? "ok" : "");
    renderSessionButton(session);
    document.title = (data.symbol || "") + " · " + (data.timeframe || "") + " · AXIOM paper";
  }

  function renderFills(markers) {
    var body = $("fills-body");
    body.innerHTML = "";
    $("fills-count").textContent = markers.length ? markers.length + " shown" : "";
    if (!markers.length) {
      var row = el("tr");
      var cell = el("td", "muted", "no simulated fills yet for this instrument");
      cell.colSpan = 7;
      row.appendChild(cell);
      body.appendChild(row);
      return;
    }
    markers.slice(-40).reverse().forEach(function (fill) {
      var row = el("tr", fill.side === "BUY" ? "buy" : "sell");
      row.appendChild(el("td", "muted", clockOf(fill.timestamp)));
      row.appendChild(el("td", "side", fill.side));
      row.appendChild(el("td", "num", num(fill.quantity, 6)));
      row.appendChild(el("td", "num", num(fill.price)));
      row.appendChild(el("td", "num muted", num(fill.fee, 4)));
      row.appendChild(el("td", "muted", fill.reason));
      row.appendChild(el("td", "muted", fill.strategy || "—"));
      body.appendChild(row);
    });
  }

  function refreshData() {
    if (!state.symbol) return Promise.resolve();
    var query = "/chart/data?symbol=" + encodeURIComponent(state.symbol) +
      "&timeframe=" + encodeURIComponent(state.timeframe) +
      "&limit=" + state.limit +
      "&indicators=" + encodeURIComponent(state.overlays.concat(state.oscillators).join(","));
    return api(query).then(function (data) {
      hideError();
      applyData(data);
      return refreshTicket();
    }).catch(showError);
  }

  function refreshTicket() {
    if (!state.symbol) return Promise.resolve();
    return api("/paper/ticket?symbol=" + encodeURIComponent(state.symbol))
      .then(renderTicket)
      .catch(function (error) {
        // A ticket failure must not blank the chart.
        toast("err", "Ticket unavailable", error.message);
      });
  }

  function refreshAll() {
    return refreshData();
  }

  function renderSessionButton(session) {
    var button = $("btn-session");
    var current = (session && session.state) || "STOPPED";
    var running = current === "RUNNING" || current === "PAUSED" || current === "STARTING";
    button.textContent = "SESSION " + current;
    button.classList.toggle("on", running);
    button.disabled = current === "STARTING";
    button.title = running
      ? "Stop the simulated paper session (cancels resting simulated orders)"
      : "Start a paper session from the configured data source";
    button.dataset.running = running ? "1" : "";
  }

  function toggleSession() {
    var button = $("btn-session");
    if (button.dataset.running) {
      if (!window.confirm("Stop the simulated paper session?\n\nResting simulated " +
        "orders are cancelled and the account is persisted. Nothing is sent to a broker.")) return;
      button.disabled = true;
      api("/paper/stop", { method: "POST" })
        .then(function () { toast("", "Session stopped", "The account is persisted."); })
        .catch(function (error) { toast("err", "Stop failed", error.message); })
        .then(function () { button.disabled = false; refreshAll(); });
      return;
    }
    startSession();
  }

  function startSession() {
    var button = $("btn-start-session");
    button.disabled = true;
    button.textContent = "STARTING…";
    api("/api/paper/start-default", { method: "POST", body: {} })
      .then(function (result) {
        toast("ok", "Paper session starting",
          "State: " + (result && result.state ? result.state : "accepted") +
          ". Bars appear as the feed delivers them.");
        window.setTimeout(function () {
          loadSymbols(true).then(refreshAll).catch(refreshAll);
        }, 1200);
      })
      .catch(function (error) {
        toast("err", "Could not start a session", error.message);
      })
      .then(function () {
        button.disabled = false;
        button.textContent = "START PAPER SESSION";
      });
  }

  function showError(error) {
    var node = $("err");
    node.style.display = "";
    node.textContent = error.message || String(error);
    window.setTimeout(function () { node.style.display = "none"; }, 8000);
  }

  function hideError() { $("err").style.display = "none"; }

  function buildTimeframeButtons(list, selected) {
    var host = $("tf-buttons");
    if (!list.length) return;
    if (host.dataset.signature !== list.join(",")) {
      host.dataset.signature = list.join(",");
      host.innerHTML = "";
    } else {
      // Same set: only the active flag can have changed.
      Array.prototype.forEach.call(host.children, function (button) {
        button.classList.toggle("on", button.textContent === selected);
      });
      return;
    }
    list.forEach(function (tf) {
      var button = el("button", tf === selected ? "on" : "", tf);
      button.type = "button";
      button.addEventListener("click", function () {
        if (state.timeframe === tf) return;
        state.timeframe = tf;
        state.view = null;
        state.follow = true;
        buildTimeframeButtons(list, tf);
        refreshData();
      });
      host.appendChild(button);
    });
  }

  function buildIndicatorButtons(data) {
    var host = $("ind-buttons");
    host.innerHTML = "";
    var options = [];
    (data.overlays || []).forEach(function (line) {
      options.push({ key: line.key, label: line.label, group: "overlays", color: line.color });
    });
    (data.available || {}).indicators && ((data.available || {}).indicators.overlays || []).forEach(function (spec) {
      if (!options.some(function (o) { return o.key === spec.key; })) {
        options.push({ key: spec.key, label: spec.label, group: "overlays", color: spec.color });
      }
    });
    ((data.available || {}).indicators && (data.available || {}).indicators.oscillators || []).forEach(function (spec) {
      options.push({ key: spec.key, label: spec.label, group: "oscillators" });
    });
    options.forEach(function (option) {
      var active = option.group === "overlays"
        ? state.overlays.indexOf(option.key) >= 0
        : state.oscillators.indexOf(option.key) >= 0;
      var button = el("button", active ? "on" : "", option.label);
      button.type = "button";
      if (option.color) button.style.color = active ? option.color : "";
      button.addEventListener("click", function () {
        var list = option.group === "overlays" ? state.overlays : state.oscillators;
        var at = list.indexOf(option.key);
        if (at >= 0) list.splice(at, 1); else list.push(option.key);
        var live = list.indexOf(option.key) >= 0;
        button.classList.toggle("on", live);
        if (option.color) button.style.color = live ? option.color : "";
        refreshData();
      });
      host.appendChild(button);
    });
  }

  function buildSymbolSelect(symbols, selected) {
    var host = $("symbol");
    if (host.dataset.signature === symbols.map(function (s) { return s.symbol; }).join(",")) {
      host.value = selected;
      return;
    }
    host.dataset.signature = symbols.map(function (s) { return s.symbol; }).join(",");
    host.innerHTML = "";
    symbols.forEach(function (entry) {
      var option = el("option", null, entry.symbol);
      option.value = entry.symbol;
      if (entry.symbol === selected) option.selected = true;
      host.appendChild(option);
    });
  }

  function loadSymbols(refresh) {
    return api("/chart/symbols").then(function (payload) {
      state.symbols = payload.symbols || [];
      if (refresh) {
        // Keep the operator's instrument if it is still drawable.
        if (state.symbol && state.symbols.some(function (s) { return s.symbol === state.symbol; })) {
          buildSymbolSelect(state.symbols, state.symbol);
          var current = state.symbols.filter(function (s) { return s.symbol === state.symbol; })[0];
          if (current && current.timeframes.indexOf(state.timeframe) < 0) {
            state.timeframe = current.timeframes[0];
            state.view = null;
          }
          return state.symbol;
        }
        if (state.symbol && !state.symbols.length) return state.symbol;
      }
      if (!state.symbols.length) {
        $("empty-state").classList.add("show");
        $("empty-reason").textContent = "No instrument has data yet. Start a paper session or add a dataset.";
        return null;
      }
      var chosen = payload.default && state.symbols.some(function (s) { return s.symbol === payload.default; })
        ? payload.default
        : state.symbols[0].symbol;
      state.symbol = chosen;
      buildSymbolSelect(state.symbols, chosen);
      var entry = state.symbols.filter(function (s) { return s.symbol === chosen; })[0];
      var timeframes = (entry && entry.timeframes) || ["1D"];
      // Default to the finest timeframe the source can draw: that is the one
      // the bars actually arrive at.
      if (!state.timeframe || timeframes.indexOf(state.timeframe) < 0) {
        state.timeframe = timeframes[0];
      }
      buildTimeframeButtons(timeframes, state.timeframe);
      return chosen;
    });
  }

  function onSymbolChange() {
    state.symbol = $("symbol").value;
    state.view = null;
    state.follow = true;
    var entry = state.symbols.filter(function (s) { return s.symbol === state.symbol; })[0];
    var timeframes = (entry && entry.timeframes) || ["1D"];
    if (timeframes.indexOf(state.timeframe) < 0) state.timeframe = timeframes[0];
    buildTimeframeButtons(timeframes, state.timeframe);
    refreshData();
  }

  /* ---------------------------------------------------------------- boot */

  function resize() {
    var wrap = canvas.parentElement;
    var dpr = window.devicePixelRatio || 1;
    var width = Math.max(320, wrap.clientWidth);
    var height = Math.max(240, wrap.clientHeight);
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    canvas.style.width = width + "px";
    canvas.style.height = height + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    geo.w = width;
    geo.h = height;
    draw();
  }

  function bind() {
    canvas.addEventListener("wheel", onWheel, { passive: false });
    canvas.addEventListener("mousedown", onMouseDown);
    window.addEventListener("mousemove", onMouseMove);
    window.addEventListener("mouseup", onMouseUp);
    canvas.addEventListener("mouseleave", onLeave);
    canvas.addEventListener("dblclick", function () {
      resetView(barCount());
      $("btn-follow").classList.add("on");
      state.follow = true;
      draw();
    });
    window.addEventListener("keydown", onKey);
    window.addEventListener("resize", resize);

    $("symbol").addEventListener("change", onSymbolChange);
    $("btn-reset").addEventListener("click", function () {
      resetView(barCount());
      state.follow = true;
      draw();
    });
    $("btn-follow").addEventListener("click", toggleFollow);
    $("btn-dashboard").addEventListener("click", function () { window.location.href = "/dashboard"; });
    $("btn-start-session").addEventListener("click", startSession);
    $("btn-session").addEventListener("click", toggleSession);

    $("side-buy").addEventListener("click", function () { setSide("BUY"); });
    $("side-sell").addEventListener("click", function () { setSide("SELL"); });
    $("btn-max").addEventListener("click", function () {
      if (!state.ticket) return;
      var value = state.side === "BUY" ? state.ticket.max_buy_quantity : state.ticket.max_sell_quantity;
      if (value <= 0) { toast("err", "Nothing available", "No headroom under the current caps."); return; }
      $("qty").value = String(Math.floor(value * 10000) / 10000);
      updateEstimate();
    });
    $("qty").addEventListener("input", updateEstimate);
    $("order-type").addEventListener("change", function () {
      $("limit-field").style.display = $("order-type").value === "LIMIT" ? "" : "none";
      if ($("order-type").value === "LIMIT" && !$("limit-price").value && state.ticket && state.ticket.last_price) {
        $("limit-price").value = String(state.ticket.last_price);
      }
    });
    $("btn-place").addEventListener("click", placeOrder);
  }

  function boot() {
    canvas = $("chart-canvas");
    ctx = canvas.getContext("2d");
    if (!token()) askToken();
    bind();
    resize();
    setSide("BUY");
    loadSymbols().then(function () {
      if (!state.symbol) return;
      return refreshData().then(function () {
        if (state.data) buildIndicatorButtons(state.data);
      });
    }).catch(showError);

    window.setInterval(function () {
      // A hidden tab must not keep polling: the page is one of many local
      // clients and the browser throttles it anyway.
      if (!state.symbol || document.hidden) return;
      refreshData();
    }, 4000);

    // Instruments appear when a session starts or a dataset is added; a slow
    // refresh picks them up without disturbing the current selection.
    var ticks = 0;
    window.setInterval(function () {
      if (document.hidden) return;
      ticks += 1;
      if (ticks % 8 !== 0) return;
      loadSymbols(true).then(function () {
        if (!state.symbol) return;
        return refreshData();
      }).catch(function () { /* the data poll already reports errors */ });
    }, 4000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
