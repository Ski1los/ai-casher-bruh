/* AI Casher — Telegram Mini App. Без библиотек: быстрый старт и мгновенный отклик. */
(() => {
"use strict";

const tg = window.Telegram && window.Telegram.WebApp;
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

// ---------------------------------------------------------------------------
// Утилиты
// ---------------------------------------------------------------------------

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const NB = "\u202f";

function groupInt(s) { return s.replace(/\B(?=(\d{3})+(?!\d))/g, NB); }
function money(v, opts = {}) {
  if (v == null || isNaN(v)) return "—";
  const sign = opts.sign ? (v > 0 ? "+" : v < 0 ? "−" : "") : (v < 0 ? "−" : "");
  const a = Math.abs(v);
  const d = opts.d ?? (a >= 1000 ? 2 : a >= 1 ? 2 : a >= 0.01 ? 4 : a === 0 ? 2 : 6);
  const [i, f] = a.toFixed(d).split(".");
  return `${sign}$${groupInt(i)}${f ? "." + f : ""}`;
}
function moneyHero(v) {
  const a = Math.abs(v || 0);
  const [i, f] = a.toFixed(a < 1 && a > 0 ? 4 : 2).split(".");
  return `${v < 0 ? "−" : ""}$${groupInt(i)}<span class="cents">.${f}</span>`;
}
function price(v) {
  if (v == null || isNaN(v)) return "—";
  const a = Math.abs(v);
  if (a >= 1000) return groupInt(a.toFixed(2).split(".")[0]) + "." + a.toFixed(2).split(".")[1];
  if (a >= 1) return a.toFixed(4).replace(/0{1,2}$/, "");
  if (a === 0) return "0";
  const digits = Math.min(12, Math.max(4, -Math.floor(Math.log10(a)) + 3));
  return a.toFixed(digits).replace(/0+$/, "");
}
function amount(v) {
  if (v == null || isNaN(v)) return "—";
  const a = Math.abs(v);
  if (a >= 1000) return groupInt(a.toFixed(2).split(".")[0]);
  if (a >= 1) return (+a.toFixed(4)).toString();
  return (+a.toPrecision(6)).toString();
}
function pct(v, d = 2) {
  if (v == null || isNaN(v)) return "—";
  return `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(d)}%`;
}
const cls = (v) => (v > 0 ? "up" : v < 0 ? "down" : "muted");
function compact(v) {
  if (!v) return "0";
  const a = Math.abs(v);
  if (a >= 1e9) return (v / 1e9).toFixed(1) + "B";
  if (a >= 1e6) return (v / 1e6).toFixed(1) + "M";
  if (a >= 1e3) return (v / 1e3).toFixed(1) + "K";
  return v.toFixed(0);
}
function ago(ts) {
  if (!ts) return "—";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return `${Math.floor(s)} с назад`;
  if (s < 3600) return `${Math.floor(s / 60)} мин назад`;
  if (s < 86400) return `${Math.floor(s / 3600)} ч назад`;
  return `${Math.floor(s / 86400)} д назад`;
}
function hm(ts) { const d = new Date(ts * 1000); return d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" }); }
function dm(ts) { const d = new Date(ts * 1000); return d.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit" }); }
function when(ts) { return (Date.now() / 1000 - ts < 86400 ? "" : dm(ts) + " ") + hm(ts); }
function hue(s) { let h = 0; for (const c of String(s)) h = (h * 31 + c.charCodeAt(0)) % 360; return h; }
const COIN_COLORS = { BTC: "#f7931a", ETH: "#627eea", USDT: "#26a17b", USDC: "#2775ca", SOL: "#9945ff", BNB: "#f3ba2f", XRP: "#23292f", TON: "#0098ea", DOGE: "#c2a633", PEPE: "#3d9a3a", TRX: "#eb0029", ADA: "#0033ad" };
const coinColor = (c) => COIN_COLORS[c] || `hsl(${hue(c)} 55% 48%)`;
const coinIcon = (c) => `<div class="coin" style="background:${coinColor(c)}">${esc(String(c).slice(0, 4))}</div>`;
const SERIES = ["#7c5cff", "#22d3ee", "#16c784", "#f5b300", "#f0475a", "#ff7ad9", "#4f8cff", "#a3e635"];
const svg = (d, extra = "") => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" ${extra}>${d}</svg>`;
const ICON = {
  scan: svg('<path d="M13 2 4 14h7l-1 8 9-12h-7z"/>', 'style="color:var(--accent2)"'),
  buy: svg('<path d="M7 17 17 7M8 7h9v9"/>', 'style="color:var(--green)"'),
  sell: svg('<path d="M7 7l10 10M17 8v9H8"/>', 'style="color:var(--red)"'),
  stop: svg('<rect x="6" y="6" width="12" height="12" rx="2.5"/>', 'style="color:var(--red)"'),
  arrow: svg('<path d="M5 12h14M13 6l6 6-6 6"/>'),
  refresh: svg('<path d="M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7"/>'),
  doctor: svg('<path d="M3 12h4l2-5 4 10 2-5h6"/>', 'style="color:var(--green)"'),
  bell: svg('<path d="M6 8a6 6 0 1 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.9 1.9 0 0 0 3.4 0"/>', 'style="color:var(--yellow)"'),
  price: svg('<path d="M3 17l6-6 4 4 8-8"/><path d="M15 7h6v6"/>', 'style="color:#4f8cff"'),
  x: svg('<path d="M6 6l12 12M18 6 6 18"/>', 'style="color:var(--red)"'),
  dex: svg('<path d="M12 3c4 3 6 6 6 10a6 6 0 0 1-12 0c0-2 1-4 2-5 0 2 1 3 2 3 0-3 1-6 2-8z"/>', 'style="color:#ff7a45"'),
  bank: svg('<path d="M3 10h18M5 10v8M9.5 10v8M14.5 10v8M19 10v8M3 20h18M12 3l9 5H3z"/>', 'style="color:var(--yellow)"'),
  pilot: svg('<circle cx="12" cy="12" r="8.5"/><path d="M12 12l4-3M12 7v1M7 12h1M16 12h1"/>', 'style="color:var(--accent2)"'),
  fire: svg('<path d="M4 17l5-5 4 4 7-7M14 9h6v6"/>', 'style="color:var(--green)"'),
  ghost: svg('<path d="M5 20V11a7 7 0 0 1 14 0v9l-2.5-2-2.3 2-2.2-2-2.2 2-2.3-2z"/><circle cx="9.5" cy="11" r="1"/><circle cx="14.5" cy="11" r="1"/>', 'style="color:#ab9ff2"'),
};
const ARR = `<span class="arr">${ICON.arrow}</span>`;
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

const store = {
  get(k, d) { try { const v = localStorage.getItem("ac_" + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("ac_" + k, JSON.stringify(v)); } catch { /* квота */ } },
};

// ---------------------------------------------------------------------------
// Telegram
// ---------------------------------------------------------------------------

const haptic = {
  tap() { try { tg.HapticFeedback.impactOccurred("light"); } catch { /* вне Telegram */ } },
  ok() { try { tg.HapticFeedback.notificationOccurred("success"); } catch { /* */ } },
  err() { try { tg.HapticFeedback.notificationOccurred("error"); } catch { /* */ } },
  sel() { try { tg.HapticFeedback.selectionChanged(); } catch { /* */ } },
};

function confirmBox(text) {
  return new Promise((res) => {
    if (tg && tg.showConfirm && tg.isVersionAtLeast && tg.isVersionAtLeast("6.2")) tg.showConfirm(text, (ok) => res(!!ok));
    else res(window.confirm(text));
  });
}
function popup(title, message, okText) {
  return new Promise((res) => {
    if (tg && tg.showPopup && tg.isVersionAtLeast && tg.isVersionAtLeast("6.2")) {
      tg.showPopup({ title, message, buttons: [{ id: "ok", type: "destructive", text: okText }, { id: "no", type: "cancel" }] }, (id) => res(id === "ok"));
    } else res(window.confirm(`${title}\n\n${message}`));
  });
}

function applyTheme() {
  const scheme = (tg && tg.colorScheme) || (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
  document.documentElement.dataset.theme = scheme;
  const bg = css("--bg");
  try {
    tg.setHeaderColor(bg); tg.setBackgroundColor(bg);
    if (tg.setBottomBarColor) tg.setBottomBarColor(css("--bg2"));
  } catch { /* старая версия Telegram */ }
  Charts.redrawAll();
}

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------

async function api(path, body, { timeout = 30000 } = {}) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeout);
  try {
    const r = await fetch(path, {
      method: body === undefined ? "GET" : "POST",
      headers: { "Content-Type": "application/json", "X-Telegram-Init-Data": (tg && tg.initData) || "" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: ctrl.signal,
    });
    let data = null;
    try { data = await r.json(); } catch { /* не JSON */ }
    if (!r.ok) throw new Error((data && data.error) || `Ошибка сервера ${r.status}`);
    return data;
  } catch (e) {
    if (e.name === "AbortError") throw new Error("Сервер долго не отвечает. Попробуй ещё раз");
    if (e instanceof TypeError) throw new Error("Нет связи с сервером");
    throw e;
  } finally { clearTimeout(timer); }
}

// ---------------------------------------------------------------------------
// Тосты и шторка
// ---------------------------------------------------------------------------

let toastTimer;
function toast(text, err = false) {
  const t = $("#toast");
  t.textContent = text;
  t.className = "toast on" + (err ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = "toast"), err ? 4200 : 2600);
  (err ? haptic.err : haptic.ok)();
}
const fail = (e) => toast(e.message || String(e), true);

const Sheet = {
  onClose: null,
  open(title, html, { full = false, foot = "", onClose = null } = {}) {
    $("#sheetTitle").textContent = title;
    $("#sheetBody").innerHTML = html;
    $("#sheetFoot").innerHTML = foot;
    $("#sheet").className = "sheet on" + (full ? " full" : "");
    $("#backdrop").classList.add("on");
    this.onClose = onClose;
    try { tg.BackButton.show(); } catch { /* */ }
  },
  body() { return $("#sheetBody"); },
  isOpen() { return $("#sheet").classList.contains("on"); },
  close() {
    if (!this.isOpen()) return;
    $("#sheet").className = "sheet";
    $("#backdrop").classList.remove("on");
    try { tg.BackButton.hide(); } catch { /* */ }
    const cb = this.onClose; this.onClose = null;
    if (cb) cb();
  },
};

// ---------------------------------------------------------------------------
// Графики (canvas, без библиотек)
// ---------------------------------------------------------------------------

const Charts = {
  live: new Map(),
  mount(el, chart) { this.live.set(el, chart); chart.draw(); },
  redrawAll() { for (const [el, c] of this.live) { if (!el.isConnected) this.live.delete(el); else c.draw(); } },
};
window.addEventListener("resize", () => Charts.redrawAll());

function prep(canvas) {
  const dpr = Math.min(window.devicePixelRatio || 1, 3);
  const r = canvas.getBoundingClientRect();
  const w = Math.max(10, r.width), h = Math.max(10, r.height);
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  return { ctx, w, h };
}
function niceTicks(min, max, n = 4) {
  if (max === min) { max += Math.abs(max) * 0.01 || 1; min -= Math.abs(min) * 0.01 || 1; }
  const step0 = (max - min) / n, mag = Math.pow(10, Math.floor(Math.log10(step0)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= step0) || step0;
  const out = [];
  for (let v = Math.ceil(min / step) * step; v <= max + 1e-12; v += step) out.push(+v.toPrecision(12));
  return out;
}

/** Линейный график: несколько серий [[t, v]], заливка градиентом, перекрестье по касанию. */
class LineChart {
  constructor(wrap, series, opts = {}) {
    this.wrap = wrap; this.series = series.filter((s) => s.points && s.points.length);
    this.o = Object.assign({ fmt: (v) => v.toFixed(2), area: true, baseline: null, axis: true, padTop: 14, padBottom: 18, tipTime: true }, opts);
    this.canvas = document.createElement("canvas");
    wrap.appendChild(this.canvas);
    this.tip = document.createElement("div"); this.tip.className = "tip hidden"; wrap.appendChild(this.tip);
    this.hover = null;
    const move = (e) => { const r = this.canvas.getBoundingClientRect(); this.hover = (e.touches ? e.touches[0].clientX : e.clientX) - r.left; this.draw(); };
    const leave = () => { this.hover = null; this.tip.classList.add("hidden"); this.draw(); };
    this.canvas.addEventListener("pointerdown", (e) => { move(e); haptic.sel(); });
    this.canvas.addEventListener("pointermove", (e) => { if (e.pointerType === "mouse" || e.buttons) move(e); });
    this.canvas.addEventListener("pointerup", leave);
    this.canvas.addEventListener("pointerleave", leave);
    this.canvas.addEventListener("pointercancel", leave);
  }
  draw() {
    const { ctx, w, h } = prep(this.canvas);
    const S = this.series;
    if (!S.length) return;
    const all = S.flatMap((s) => s.points);
    let t0 = Math.min(...all.map((p) => p[0])), t1 = Math.max(...all.map((p) => p[0]));
    if (t1 === t0) { t0 -= 60; t1 += 60; }
    let lo = Math.min(...all.map((p) => p[1])), hi = Math.max(...all.map((p) => p[1]));
    if (this.o.baseline != null) { lo = Math.min(lo, this.o.baseline); hi = Math.max(hi, this.o.baseline); }
    const span = hi - lo || Math.abs(hi) * 0.02 || 1;
    lo -= span * 0.12; hi += span * 0.12;
    const padR = this.o.axis ? 54 : 6, padL = 8, pt = this.o.padTop, pb = this.o.padBottom;
    const X = (t) => padL + ((t - t0) / (t1 - t0)) * (w - padL - padR);
    const Y = (v) => pt + (1 - (v - lo) / (hi - lo)) * (h - pt - pb);
    const muted = css("--dim"), line = css("--line");

    if (this.o.axis) {
      ctx.font = "10.5px -apple-system, Roboto, sans-serif"; ctx.textAlign = "left"; ctx.textBaseline = "middle";
      for (const v of niceTicks(lo, hi, 3)) {
        const y = Y(v); if (y < pt - 2 || y > h - pb + 2) continue;
        ctx.strokeStyle = line; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR + 4, y); ctx.stroke();
        ctx.fillStyle = muted; ctx.fillText(this.o.fmt(v), w - padR + 8, y);
      }
      ctx.textAlign = "center"; ctx.textBaseline = "alphabetic";
      const multiDay = t1 - t0 > 86400 * 1.5;
      for (let i = 0; i <= 3; i++) {
        const t = t0 + ((t1 - t0) * i) / 3;
        ctx.fillStyle = muted; ctx.fillText(multiDay ? dm(t) : hm(t), Math.min(Math.max(X(t), 20), w - padR - 16), h - 3);
      }
    }
    if (this.o.baseline != null) {
      const y = Y(this.o.baseline);
      ctx.setLineDash([4, 4]); ctx.strokeStyle = muted; ctx.globalAlpha = 0.6; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke(); ctx.setLineDash([]); ctx.globalAlpha = 1;
    }
    S.forEach((s, si) => {
      const pts = s.points.map((p) => [X(p[0]), Y(p[1])]);
      const color = s.color || SERIES[si % SERIES.length];
      if (this.o.area && si === 0 && S.length === 1) {
        const g = ctx.createLinearGradient(0, pt, 0, h - pb);
        g.addColorStop(0, color + "55"); g.addColorStop(1, color + "00");
        ctx.beginPath(); ctx.moveTo(pts[0][0], h - pb);
        pts.forEach(([x, y]) => ctx.lineTo(x, y));
        ctx.lineTo(pts[pts.length - 1][0], h - pb); ctx.closePath(); ctx.fillStyle = g; ctx.fill();
      }
      ctx.beginPath(); ctx.lineJoin = "round"; ctx.lineCap = "round";
      pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
      ctx.strokeStyle = color; ctx.lineWidth = S.length > 1 ? 2 : 2.4; ctx.stroke();
      const [lx, ly] = pts[pts.length - 1];
      ctx.beginPath(); ctx.arc(lx, ly, 6, 0, 7); ctx.fillStyle = color + "33"; ctx.fill();
      ctx.beginPath(); ctx.arc(lx, ly, 3, 0, 7); ctx.fillStyle = color; ctx.fill();
    });

    if (this.hover != null) {
      const x = Math.min(Math.max(this.hover, padL), w - padR);
      const t = t0 + ((x - padL) / (w - padL - padR)) * (t1 - t0);
      ctx.strokeStyle = css("--muted"); ctx.globalAlpha = 0.5; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(x, pt - 6); ctx.lineTo(x, h - pb); ctx.stroke(); ctx.globalAlpha = 1;
      const rows = [];
      S.forEach((s, si) => {
        let best = s.points[0];
        for (const p of s.points) if (p[0] <= t) best = p;
        const color = s.color || SERIES[si % SERIES.length];
        ctx.beginPath(); ctx.arc(X(best[0]), Y(best[1]), 4, 0, 7); ctx.fillStyle = color; ctx.fill();
        ctx.lineWidth = 2; ctx.strokeStyle = css("--card"); ctx.stroke();
        rows.push(`<div><i style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${color};margin-right:5px"></i>${s.name ? esc(s.name) + ": " : ""}<b>${this.o.fmt(best[1])}</b></div>`);
      });
      this.tip.innerHTML = (this.o.tipTime ? `<div class="muted">${when(t)}</div>` : "") + rows.join("");
      this.tip.classList.remove("hidden");
      const tw = this.tip.offsetWidth;
      this.tip.style.left = Math.min(Math.max(x - tw / 2, 4), w - tw - 4) + "px";
    }
  }
}

/** Свечной график с объёмом и перекрестьем. */
class CandleChart {
  constructor(wrap, candles, opts = {}) {
    this.wrap = wrap; this.c = candles; this.o = opts;
    this.canvas = document.createElement("canvas"); wrap.appendChild(this.canvas);
    this.hover = null;
    const move = (e) => { const r = this.canvas.getBoundingClientRect(); this.hover = { x: e.clientX - r.left, y: e.clientY - r.top }; this.draw(); };
    const leave = () => { this.hover = null; this.draw(); if (this.o.onHover) this.o.onHover(null); };
    this.canvas.addEventListener("pointerdown", (e) => { move(e); haptic.sel(); });
    this.canvas.addEventListener("pointermove", (e) => { if (e.pointerType === "mouse" || e.buttons) move(e); });
    this.canvas.addEventListener("pointerup", leave);
    this.canvas.addEventListener("pointerleave", leave);
    this.canvas.addEventListener("pointercancel", leave);
  }
  update(candles) { this.c = candles; this.draw(); }
  draw() {
    const { ctx, w, h } = prep(this.canvas);
    const c = this.c; if (!c || !c.length) return;
    const padR = 60, padL = 4, pt = 10, volH = h * 0.16, pb = 18;
    const priceH = h - pt - pb - volH - 6;
    let lo = Math.min(...c.map((k) => k[3])), hi = Math.max(...c.map((k) => k[2]));
    const span = hi - lo || hi * 0.01 || 1; lo -= span * 0.06; hi += span * 0.06;
    const vmax = Math.max(...c.map((k) => k[5])) || 1;
    const n = c.length, cw = (w - padL - padR) / n;
    const X = (i) => padL + cw * i + cw / 2;
    const Y = (v) => pt + (1 - (v - lo) / (hi - lo)) * priceH;
    const green = css("--green"), red = css("--red"), muted = css("--dim"), line = css("--line");
    ctx.font = "10.5px -apple-system, Roboto, sans-serif";
    ctx.textBaseline = "middle"; ctx.textAlign = "left";
    for (const v of niceTicks(lo, hi, 4)) {
      const y = Y(v); if (y < pt || y > pt + priceH) continue;
      ctx.strokeStyle = line; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke();
      ctx.fillStyle = muted; ctx.fillText(price(v), w - padR + 6, y);
    }
    const bw = Math.max(1, Math.min(cw * 0.68, 14));
    c.forEach((k, i) => {
      const [, o, hh, l, cl, v] = k;
      const col = cl >= o ? green : red;
      const x = X(i);
      ctx.strokeStyle = col; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(Math.round(x) + 0.5, Y(hh)); ctx.lineTo(Math.round(x) + 0.5, Y(l)); ctx.stroke();
      const y1 = Y(Math.max(o, cl)), y2 = Y(Math.min(o, cl));
      ctx.fillStyle = col; ctx.fillRect(x - bw / 2, y1, bw, Math.max(1, y2 - y1));
      const vh = (v / vmax) * volH;
      ctx.globalAlpha = 0.35; ctx.fillRect(x - bw / 2, h - pb - vh, bw, vh); ctx.globalAlpha = 1;
    });
    // последняя цена
    const last = c[n - 1][4], ly = Y(last), up = last >= c[n - 1][1];
    ctx.setLineDash([3, 3]); ctx.strokeStyle = up ? green : red; ctx.beginPath(); ctx.moveTo(padL, ly); ctx.lineTo(w - padR, ly); ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = up ? green : red; roundRect(ctx, w - padR + 2, ly - 9, padR - 4, 18, 5); ctx.fill();
    ctx.fillStyle = "#fff"; ctx.fillText(price(last), w - padR + 6, ly);
    // время
    ctx.textAlign = "center"; ctx.textBaseline = "alphabetic"; ctx.fillStyle = muted;
    const daily = c.length > 1 && c[1][0] - c[0][0] >= 86400000;
    for (let j = 0; j < 4; j++) {
      const i = Math.round(((n - 1) * (j + 0.5)) / 4);
      const t = c[i][0] / 1000;
      ctx.fillText(daily ? dm(t) : (Date.now() / 1000 - t > 86400 ? dm(t) + " " : "") + hm(t), X(i), h - 4);
    }
    if (this.hover) {
      const i = Math.min(n - 1, Math.max(0, Math.floor((this.hover.x - padL) / cw)));
      const x = X(i), y = Math.min(Math.max(this.hover.y, pt), pt + priceH);
      ctx.strokeStyle = css("--muted"); ctx.globalAlpha = 0.6; ctx.setLineDash([3, 3]);
      ctx.beginPath(); ctx.moveTo(x, pt); ctx.lineTo(x, h - pb); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke();
      ctx.setLineDash([]); ctx.globalAlpha = 1;
      const v = lo + (1 - (y - pt) / priceH) * (hi - lo);
      ctx.fillStyle = css("--card3"); roundRect(ctx, w - padR + 2, y - 9, padR - 4, 18, 5); ctx.fill();
      ctx.fillStyle = css("--text"); ctx.textAlign = "left"; ctx.textBaseline = "middle"; ctx.fillText(price(v), w - padR + 6, y);
      if (this.o.onHover) this.o.onHover(c[i]);
    }
  }
}
function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath(); ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath();
}

/** Кольцевая диаграмма распределения портфеля. */
class Donut {
  constructor(wrap, items, center) { this.items = items; this.center = center; this.canvas = document.createElement("canvas"); wrap.prepend(this.canvas); }
  draw() {
    const { ctx, w, h } = prep(this.canvas);
    const total = this.items.reduce((s, i) => s + i.value, 0) || 1;
    const r = Math.min(w, h) / 2 - 4, cx = w / 2, cy = h / 2;
    let a = -Math.PI / 2;
    this.items.forEach((it) => {
      const da = (it.value / total) * Math.PI * 2;
      ctx.beginPath(); ctx.arc(cx, cy, r, a + 0.015, a + da - 0.015); ctx.strokeStyle = it.color; ctx.lineWidth = 16; ctx.lineCap = "butt"; ctx.stroke();
      a += da;
    });
    ctx.fillStyle = css("--text"); ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.font = "700 15px -apple-system, Roboto, sans-serif"; ctx.fillText(this.center[0], cx, cy - 7);
    ctx.font = "11px -apple-system, Roboto, sans-serif"; ctx.fillStyle = css("--muted"); ctx.fillText(this.center[1], cx, cy + 11);
  }
}

/** Мини-график без осей. */
function spark(wrap, points, color) {
  if (!points || points.length < 2) return;
  Charts.mount(wrap, new LineChart(wrap, [{ points, color }], { axis: false, padTop: 4, padBottom: 4, fmt: (v) => money(v) }));
}

// ---------------------------------------------------------------------------
// Состояние
// ---------------------------------------------------------------------------

const S = {
  ov: store.get("ov", null),
  tab: store.get("tab", "home"),
  homeMode: store.get("homeMode", null),
  sigTab: store.get("sigTab", "tri"),
  m: Object.assign({ ex: null, symbol: "BTC/USDT", tf: "15m", side: "buy", type: "market", amount: "", price: "" }, store.get("m", {})),
  market: null, portfolio: store.get("portfolio", null), orders: null, demo: null, phantom: store.get("phantom", null),
  chat: [], symbols: {}, scanning: false, sigs: {},
};
const owner = () => !!(S.ov && S.ov.user && S.ov.user.owner);
const tr = () => (S.ov && S.ov.trading) || {};

// ---------------------------------------------------------------------------
// Шапка
// ---------------------------------------------------------------------------

function renderHeader() {
  const t = tr();
  const pill = $("#modePill");
  pill.className = "pill " + (t.live ? "live" : "test");
  pill.textContent = t.live ? "LIVE" : "ТЕСТ";
  const sc = S.ov && S.ov.scan;
  $("#sub").textContent = S.ov ? (t.auto ? "⚙️ автоторговля включена" : sc ? `скан ${ago(sc.finished_at)}` : "готов к работе") : "загрузка…";
}

// ---------------------------------------------------------------------------
// Главная
// ---------------------------------------------------------------------------

function demoAggregate(demo) {
  const accs = (demo && demo.accounts) || [];
  if (!accs.length) return [];
  const times = [...new Set(accs.flatMap((a) => a.curve.map((p) => p[0])))].sort((a, b) => a - b);
  return times.map((t) => [t, accs.reduce((sum, a) => {
    let v = a.curve[0][1];
    for (const p of a.curve) { if (p[0] <= t) v = p[1]; else break; }
    return sum + v;
  }, 0)]);
}

function heroBlock() {
  const t = tr(), d = S.ov.demo;
  const hasReal = t.keys && t.keys.length;
  const hasDemo = d && d.accounts.length;
  let mode = S.homeMode || (hasReal ? "real" : "demo");
  if (mode === "real" && !hasReal) mode = "demo";
  let value, change = null, base = null, points = [], label;
  if (mode === "real") {
    const p = S.portfolio;
    value = p && p.total_usd != null ? p.total_usd : (t.cached_total ?? 0);
    points = (p && p.history && p.history.length ? p.history : t.history) || [];
    if (points.length > 1) { base = points[0][1]; change = value - base; }
    label = "Реальный портфель";
  } else {
    value = hasDemo ? d.total_equity : 0;
    base = hasDemo ? d.total_start : null;
    change = hasDemo ? d.total_equity - d.total_start : null;
    points = demoAggregate(d);
    label = hasDemo ? `Демо-счета · ${d.accounts.length} шт` : "Демо-счета";
  }
  const chg = change != null && base ? `<span class="${cls(change)} b num">${money(change, { sign: true })} (${pct((change / base) * 100)})</span> <span class="muted small">за всё время</span>` : `<span class="muted small">${mode === "real" ? "история появится через 15 минут" : "запусти демо, чтобы увидеть график"}</span>`;
  const seg = hasReal ? `<div class="seg" style="width:180px"><button data-act="home-mode" data-v="real" class="${mode === "real" ? "on" : ""}">Реальный</button><button data-act="home-mode" data-v="demo" class="${mode === "demo" ? "on" : ""}">Демо</button></div>` : "";
  return { mode, points, base, html: `
    <div class="card hero">
      <div class="row between"><div class="label">${label}</div>${seg}</div>
      <div class="big num">${moneyHero(value)}</div>
      <div>${chg}</div>
      <div class="chart-wrap" id="heroChart">${points.length < 2 ? `<div class="chart-empty">${mode === "real" ? "График баланса строится автоматически каждые 15 минут" : hasDemo ? "Сделок пока не было: график появится после первой" : "Нет данных"}</div>` : ""}</div>
    </div>` };
}

function signalMini(s, kind) {
  const p = s.depth_pct ?? s.net;
  const title = kind === "tri" ? s.path.slice(1, -1).map(esc).join(ARR) : esc(s.symbol);
  const sub = kind === "tri" ? `Треугольник · ${esc(s.name)}` : `${esc(s.buy_name)} → ${esc(s.sell_name)}`;
  return `<div class="item press" data-act="goto" data-tab="signals" data-sig="${kind}">
    <div class="coin" style="background:${kind === "tri" ? "var(--accent)" : "#4f8cff"}">${kind === "tri" ? "🔺" : "⇄"}</div>
    <div class="grow"><div class="t ellipsis">${title}</div><div class="s">${sub}</div></div>
    <div class="r"><div class="b num ${cls(p)}">${pct(p, 3)}</div><div class="s">${s.filled ? "по стакану" : "по тикеру"}</div></div></div>`;
}

function renderHome() {
  const v = $("#v-home");
  if (!S.ov) { v.innerHTML = skeleton(); return; }
  const t = tr();
  const hero = heroBlock();
  const banners = [];
  if (S.ov.preview) banners.push(`<div class="banner info">👀 Предпросмотр с ненастоящими данными. В Telegram приложение покажет твои реальные цифры.</div>`);
  if (t.paused_reason && !t.auto) banners.push(`<div class="banner red">⏸ Автоторговля остановлена: ${esc(t.paused_reason)}</div>`);
  if (!t.keys || !t.keys.length) banners.push(`<div class="banner info press" data-act="keys"><div>🔑</div><div><b>Подключи биржу, чтобы торговать.</b><br><span class="muted">5 минут с телефона. Без ключа работают сигналы и демо.</span> <span class="link">Как подключить →</span></div></div>`);
  else if (t.keys.some((k) => k.status !== "ok" && k.status !== "unchecked")) banners.push(`<div class="banner red">⚠️ Ключ не работает: ${t.keys.filter((k) => k.status !== "ok" && k.status !== "unchecked").map((k) => `${esc(k.name)} — ${esc(k.status)}`).join("; ")}</div>`);

  for (const w of t.key_warnings || []) banners.push(`<div class="banner red press" data-act="doctor"><div>🔑</div><div>${esc(w)} <span class="link">Проверить →</span></div></div>`);

  const sc = S.ov.scan;
  const sigs = sc ? [...sc.tri.map((s) => [s, "tri"]), ...sc.cross.map((s) => [s, "cross"])]
    .sort((a, b) => (b[0].depth_pct ?? b[0].net) - (a[0].depth_pct ?? a[0].net)).slice(0, 3) : [];
  const log = (t.log || []).slice(0, 5);

  v.innerHTML = `
    ${banners.length ? `<div class="col" style="gap:8px;margin-bottom:10px">${banners.join("")}</div>` : ""}
    ${hero.html}
    <div class="grid2" style="margin-top:10px">
      <div class="stat"><div class="k">Сегодня</div><div class="v num ${cls(t.today ? t.today.pnl : 0)}">${money(t.today ? t.today.pnl : 0, { sign: true })}</div><div class="small muted">сделок: ${t.today ? t.today.trades : 0} из ${t.limits ? t.limits.daily_trades : "—"}</div></div>
      <div class="stat"><div class="row between"><div class="k">Автоторговля</div>
        <label class="switch"><input type="checkbox" data-act="auto" ${t.auto ? "checked" : ""} ${owner() ? "" : "disabled"}><span></span></label></div>
        <div class="small muted" style="margin-top:6px">${t.auto ? (t.live ? "🔴 реально" : "🧪 в тесте") + `, от ${t.limits.auto_min}%` : "треугольники, по стакану"}</div></div>
    </div>
    ${autoCard()}
    <div class="grid4" style="margin-top:10px">
      <div class="action" data-act="scan"><div class="ic">${ICON.scan}</div>Скан</div>
      <div class="action" data-act="quick" data-side="buy"><div class="ic" style="background:var(--green-bg)">${ICON.buy}</div>Купить</div>
      <div class="action" data-act="quick" data-side="sell"><div class="ic" style="background:var(--red-bg)">${ICON.sell}</div>Продать</div>
      <div class="action danger" data-act="stop"><div class="ic">${ICON.stop}</div>Стоп</div>
    </div>
    <div class="grid4" style="margin-top:8px">
      <div class="action" data-act="doctor"><div class="ic">${ICON.doctor}</div>Проверка</div>
      <div class="action" data-act="alerts"><div class="ic">${ICON.bell}</div>Алерты</div>
      <div class="action" data-act="prices"><div class="ic">${ICON.price}</div>Цены</div>
      <div class="action" data-act="phantom"><div class="ic">${ICON.ghost}</div>Phantom</div>
    </div>
    <div class="grid4" style="margin-top:8px">
      <div class="action" data-act="dex" data-v="hot"><div class="ic">${ICON.dex}</div>Мемкоины</div>
      <div class="action" data-act="dex" data-v="search"><div class="ic">${ICON.fire}</div>DEX-поиск</div>
      <div class="action" data-act="tinvest"><div class="ic">${ICON.bank}</div>Т-Инвест</div>
      <div class="action" data-act="why"><div class="ic">${ICON.pilot}</div>Автопилот</div>
    </div>
    <div class="section-title">Лучшие сигналы <a data-act="goto" data-tab="signals">Все</a></div>
    ${sigs.length ? `<div class="list">${sigs.map(([s, k]) => signalMini(s, k)).join("")}</div>` : `<div class="card empty"><div class="e">🔍</div>${sc ? "Прибыльных связок сейчас нет. Это нормально: рынок эффективен." : "Сканирований ещё не было"}<div style="margin-top:12px"><button class="btn primary sm" data-act="scan">Сканировать</button></div></div>`}
    <div class="section-title">Последние сделки <a data-act="goto" data-tab="wallet">Журнал</a></div>
    ${log.length ? `<div class="list">${log.map(tradeRow).join("")}</div>` : `<div class="card empty"><div class="e">🧾</div>Сделок через бота пока не было</div>`}
    <div class="center tiny dim" style="margin:18px 0 4px">Сканов: ${S.ov.stats.scans} · работает ${S.ov.stats.uptime_h} ч · биржи: ${S.ov.exchanges.map((e) => esc(e.name)).join(", ")}</div>`;
  if (hero.points.length >= 2) {
    const color = hero.base != null && hero.points[hero.points.length - 1][1] < hero.base ? css("--red") : "#7c5cff";
    Charts.mount($("#heroChart"), new LineChart($("#heroChart"), [{ points: hero.points, color }], { fmt: (x) => money(x), baseline: hero.base, axis: false, padBottom: 6 }));
  }
}

function tradeRow(r) {
  const src = { ai: "🤖 ИИ", auto: "⚙️ авто", manual: "вручную" }[r.source] || "";
  const pnl = r.pnl_usd != null ? `<div class="b num ${cls(r.pnl_usd)}">${money(r.pnl_usd, { sign: true })}</div>` : `<div class="b num">${amount(r.got)} ${esc(r.got_ccy)}</div>`;
  const icon = r.kind === "triangle" ? "🔺" : r.kind === "buy" ? "↗" : r.kind === "sell" ? "↘" : "📌";
  const bg = r.kind === "buy" ? "var(--green)" : r.kind === "sell" ? "var(--red)" : r.kind === "triangle" ? "var(--accent)" : "#4f8cff";
  return `<div class="item"><div class="coin" style="background:${bg}">${icon}</div>
    <div class="grow"><div class="t ellipsis">${esc(r.what)} ${r.live ? "" : '<span class="pill test" style="padding:1px 6px">тест</span>'}</div>
    <div class="s ellipsis">${when(r.at)} · ${esc(r.exchange)} · ${src}${r.ok === false ? " · ⚠️ " + esc(r.note) : ""}</div></div>
    <div class="r">${pnl}<div class="s num">${amount(r.spent)} ${esc(r.spent_ccy)}</div></div></div>`;
}

function skeleton() {
  return `<div class="skel" style="height:240px"></div><div class="grid2" style="margin-top:10px"><div class="skel" style="height:84px"></div><div class="skel" style="height:84px"></div></div><div class="skel" style="height:180px;margin-top:10px"></div>`;
}

// ---------------------------------------------------------------------------
// Рынок
// ---------------------------------------------------------------------------

function marketEx() {
  const exs = (S.ov && S.ov.exchanges) || [];
  if (!S.m.ex || !exs.some((e) => e.id === S.m.ex)) S.m.ex = (tr().working && tr().working[0]) || (exs[0] && exs[0].id) || null;
  return S.m.ex;
}

function renderMarket() {
  const v = $("#v-market");
  if (!S.ov) { v.innerHTML = skeleton(); return; }
  const ex = marketEx();
  const m = S.market && S.market.symbol === S.m.symbol && S.market.exchange === ex && S.market.tf === S.m.tf ? S.market : null;
  const tk = m ? m.ticker : {};
  const base = S.m.symbol.split("/")[0], quote = S.m.symbol.split("/")[1] || "USDT";
  const keyed = (tr().working || []).includes(ex);
  const hasKey = (tr().keys || []).some((k) => k.exchange === ex);
  const sideBuy = S.m.side === "buy";
  const quick = sideBuy ? ["5", "10", "25", "50%", "all"] : ["25%", "50%", "75%", "all"];
  const lim = tr().limits || {};
  v.innerHTML = `
    <div class="row" style="gap:8px">
      <div class="pair-btn" data-act="pick-symbol">${coinIcon(base).replace('class="coin"', 'class="coin" style="width:26px;height:26px;font-size:9px;background:' + coinColor(base) + '"')}${esc(S.m.symbol)}</div>
      <div class="chips grow">${S.ov.exchanges.map((e) => `<div class="chip ${e.id === ex ? "on" : ""}" data-act="m-ex" data-v="${esc(e.id)}">${esc(e.name)}${(tr().working || []).includes(e.id) ? " 🔑" : ""}</div>`).join("")}</div>
    </div>
    <div class="card" style="margin-top:10px;padding:14px 14px 8px">
      <div class="row between">
        <div><div class="price-big num" id="mPrice">${m ? price(tk.last) : '<span class="skel" style="display:inline-block;width:140px;height:30px"></span>'}</div>
        <div class="ohlc num" id="mOhlc">${m ? `<span>24ч: <b class="${cls(tk.change)}">${pct(tk.change)}</b></span><span>H <b>${price(tk.high)}</b></span><span>L <b>${price(tk.low)}</b></span><span>Об. <b>$${compact(tk.volume)}</b></span>` : ""}</div></div>
        ${m && tk.change != null ? `<span class="badge ${cls(tk.change)} num">${pct(tk.change)}</span>` : ""}
      </div>
      <div class="chips" style="margin-top:10px">${["5m", "15m", "1h", "4h", "1d"].map((t) => `<div class="chip ${S.m.tf === t ? "on" : ""}" data-act="m-tf" data-v="${t}">${t}</div>`).join("")}</div>
      <div class="chart-wrap tall" id="candles">${m ? "" : `<div class="chart-empty"><div class="spin dark"></div></div>`}</div>
    </div>
    <div class="card" style="margin-top:10px">
      <div class="seg buy-sell"><button class="buy ${sideBuy ? "on" : ""}" data-act="m-side" data-v="buy">Купить</button><button class="sell ${sideBuy ? "" : "on"}" data-act="m-side" data-v="sell">Продать</button></div>
      <div class="seg" style="margin-top:8px"><button class="${S.m.type === "market" ? "on" : ""}" data-act="m-type" data-v="market">По рынку</button><button class="${S.m.type === "limit" ? "on" : ""}" data-act="m-type" data-v="limit">Лимитный</button></div>
      ${S.m.type === "limit" ? `<div class="field"><label>Цена за 1 ${esc(base)}</label><input id="mPriceIn" inputmode="decimal" placeholder="${m ? price(tk.last) : "цена"}" value="${esc(S.m.price)}"><span class="unit">${esc(quote)}</span></div>` : ""}
      <div class="field"><label>${sideBuy ? `Сумма покупки (лимит $${lim.max_trade ?? "—"})` : `Сколько продать`}</label><input id="mAmount" inputmode="decimal" placeholder="${sideBuy ? "10" : "all"}" value="${esc(S.m.amount)}"><span class="unit">${sideBuy ? esc(quote) : esc(base)}</span></div>
      <div class="chips" style="margin-top:8px">${quick.map((q) => `<div class="chip ${S.m.amount === q ? "on" : ""}" data-act="m-amt" data-v="${q}">${q === "all" ? (sideBuy ? "макс" : "всё") : /%$/.test(q) ? q : "$" + q}</div>`).join("")}</div>
      ${m ? `<div style="margin-top:12px"><div class="kv"><span>Комиссия биржи</span><span>${m.fee_pct.toFixed(3)}%</span></div><div class="kv"><span>Мин. ордер</span><span>${m.min_cost ? "$" + m.min_cost : "—"}</span></div><div class="kv"><span>Защита от проскальзывания</span><span>${lim.slippage ?? "—"}%</span></div></div>` : ""}
      <button class="btn block ${sideBuy ? "green" : "red"}" style="margin-top:14px" data-act="m-preview" ${owner() ? "" : "disabled"}>${sideBuy ? "Купить" : "Продать"} ${esc(base)} · предпросмотр</button>
      <div class="tiny muted center" style="margin-top:8px">${!owner() ? "Торговать может только владелец бота" : !hasKey ? `Для ${esc(S.ov.exchanges.find((e) => e.id === ex)?.name || ex)} нет ключа: <span class="link" data-act="keys">подключить</span>` : !keyed ? "Ключ этой биржи не работает" : tr().live ? "🔴 Реальный режим: ордер уйдёт на биржу после подтверждения" : "🧪 Тестовый режим: ордер посчитается, но не отправится"}</div>
    </div>`;
  if (m) {
    const wrap = $("#candles");
    const base0 = $("#mOhlc").innerHTML;
    Charts.mount(wrap, new CandleChart(wrap, m.candles, {
      onHover: (k) => {
        const el = $("#mOhlc"); if (!el) return;
        if (!k) { el.innerHTML = base0; return; }
        const ch = ((k[4] - k[1]) / k[1]) * 100;
        el.innerHTML = `<span>${when(k[0] / 1000)}</span><span>O <b>${price(k[1])}</b></span><span>H <b>${price(k[2])}</b></span><span>L <b>${price(k[3])}</b></span><span>C <b class="${cls(ch)}">${price(k[4])}</b></span>`;
      },
    }));
  }
}

let marketBusy = false;
async function loadMarket(force) {
  const ex = marketEx(); if (!ex || marketBusy) return;
  marketBusy = true;
  try {
    const data = await api(`/api/market?ex=${encodeURIComponent(ex)}&symbol=${encodeURIComponent(S.m.symbol)}&tf=${S.m.tf}`);
    const same = S.market && S.market.symbol === data.symbol && S.market.exchange === data.exchange && S.market.tf === data.tf;
    S.market = data;
    if (S.tab !== "market") return;
    const chart = $("#candles") && Charts.live.get($("#candles"));
    if (same && chart && !force) {   // обновляем на месте, без перерисовки панели
      chart.update(data.candles);
      $("#mPrice").textContent = price(data.ticker.last);
    } else renderMarket();
  } catch (e) {
    if (S.tab === "market") { const w = $("#candles"); if (w) w.innerHTML = `<div class="chart-empty">${esc(e.message)}</div>`; }
  } finally { marketBusy = false; }
}

async function pickSymbol() {
  const ex = marketEx();
  Sheet.open("Выбор монеты", `<div class="field" style="margin-top:0"><input id="symSearch" placeholder="Поиск: BTC, ETH, PEPE…" autocomplete="off"></div><div id="symList" style="margin-top:10px"><div class="empty"><div class="spin dark" style="margin:auto"></div></div></div>`, { full: true });
  const draw = (rows, q) => {
    const f = q ? rows.filter((r) => r[0].toLowerCase().includes(q.toLowerCase())) : rows;
    $("#symList").innerHTML = f.length ? `<div class="list">${f.slice(0, 120).map((r) => {
      const b = r[0].split("/")[0];
      return `<div class="item press" data-act="set-symbol" data-v="${esc(r[0])}">${coinIcon(b)}<div class="grow"><div class="t">${esc(b)}<span class="muted">/${esc(r[0].split("/")[1])}</span></div><div class="s">Объём $${compact(r[3])}</div></div><div class="r"><div class="b num">${price(r[1])}</div><div class="s num ${cls(r[2])}">${pct(r[2])}</div></div></div>`;
    }).join("")}</div>` : `<div class="empty">Ничего не найдено</div>`;
  };
  try {
    if (!S.symbols[ex] || Date.now() - S.symbols[ex].at > 60000) S.symbols[ex] = { at: Date.now(), rows: (await api(`/api/symbols?ex=${encodeURIComponent(ex)}`)).symbols };
    const rows = S.symbols[ex].rows;
    draw(rows, "");
    $("#symSearch").addEventListener("input", (e) => draw(rows, e.target.value.trim()));
  } catch (e) { $("#symList").innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
}

// ---------------------------------------------------------------------------
// Предпросмотр и исполнение ордера
// ---------------------------------------------------------------------------

function planHtml(p) {
  const side = p.side === "buy" ? "Покупка" : "Продажа";
  return `<div class="card">
    <div class="row between"><div class="b" style="font-size:17px">${side} ${esc(p.symbol)}</div><span class="pill ${p.live ? "live" : "test"}">${p.live ? "РЕАЛЬНО" : "ТЕСТ"}</span></div>
    <div class="muted small">${esc(p.name)} · ${p.type === "market" ? "по рынку" : "лимитный по " + price(p.limit_price)}${p.source === "ai" ? " · предложил ИИ" : ""}</div>
    <div class="row" style="margin:14px 0 6px;gap:12px">
      <div class="grow"><div class="tiny muted">Отдаёшь</div><div class="b num" style="font-size:17px">${amount(p.spent)} ${esc(p.spent_ccy)}</div></div>
      <div style="font-size:20px" class="muted">→</div>
      <div class="grow r" style="text-align:right"><div class="tiny muted">Получишь ≈</div><div class="b num up" style="font-size:17px">${amount(p.got)} ${esc(p.got_ccy)}</div></div>
    </div>
    <div class="kv"><span>Средняя цена</span><span class="num">${price(p.price)}</span></div>
    <div class="kv"><span>Комиссия</span><span>${p.fee_pct.toFixed(3)}%</span></div>
    <div class="kv"><span>Сумма</span><span class="num">${money(p.usd)}</span></div>
    ${p.type === "market" ? `<div class="kv"><span>Отмена, если цена уйдёт хуже чем на</span><span>${p.slippage_pct}%</span></div>` : ""}
    ${p.reason ? `<div class="small muted" style="margin-top:6px">💬 ${esc(p.reason)}</div>` : ""}
    ${(p.warnings || []).map((w) => `<div class="banner" style="margin-top:8px;padding:8px 10px;font-size:12.5px">⚠️ ${esc(w)}</div>`).join("")}
  </div>`;
}

function showPlan(p) {
  Sheet.open("Подтверждение", planHtml(p) + `<div class="tiny muted center" style="margin-top:10px">Предпросмотр действует 5 минут</div>`, {
    foot: `<div class="row" style="padding:10px 16px;gap:10px"><button class="btn ghost grow" data-act="plan-no" data-id="${p.id}">Отмена</button><button class="btn grow ${p.live ? "red" : "primary"}" data-act="plan-ok" data-id="${p.id}" data-live="${p.live ? 1 : 0}">${p.live ? "Отправить ордер" : "Выполнить тест"}</button></div>`,
  });
}

async function executePlan(id, live, btn) {
  if (live && !(await confirmBox("Отправить РЕАЛЬНЫЙ ордер на биржу?"))) return;
  busy(btn, true);
  try {
    const r = await api("/api/execute", { id });
    const rec = r.record;
    Sheet.close();
    toast(rec.live ? (rec.kind === "limit" ? "📌 Лимитный ордер выставлен" : `✅ Исполнено: получено ${amount(rec.got)} ${rec.got_ccy}`) : `🧪 Тест: получил бы ${amount(rec.got)} ${rec.got_ccy}`);
    refresh(); if (rec.live) loadPortfolio();
  } catch (e) { fail(e); } finally { busy(btn, false); }
}

function busy(btn, on) {
  if (!btn) return;
  if (on) { btn.dataset.html = btn.innerHTML; btn.innerHTML = `<div class="spin"></div>`; btn.disabled = true; }
  else if (btn.dataset.html != null) { btn.innerHTML = btn.dataset.html; btn.disabled = false; }
}

// ---------------------------------------------------------------------------
// Сигналы
// ---------------------------------------------------------------------------

function triCard(s) {
  const p = s.depth_pct ?? s.net;
  const lim = tr().limits || {};
  const be = (1 / Math.pow(1 - s.fee_pct / 100, 3) - 1) * 100;
  const scale = Math.max(1, Math.abs(p) * 1.4, (lim.auto_min || 0.3) * 1.6);
  const w = Math.min(100, (Math.max(0, p) / scale) * 100);
  return `<div class="card sig">
    <div class="row between"><div class="path">${s.path.map((c, i) => `${i ? ARR : ""}<span>${esc(c)}</span>`).join("")}</div><div class="pct num ${cls(p)}">${pct(p, 3)}</div></div>
    <div class="row between small muted" style="margin-top:2px"><span>🔺 ${esc(s.name)}</span><span>${s.filled ? "проверено стаканом" : s.depth_error ? "стакан: ошибка" : "по тикеру"}</span></div>
    <div class="meter"><i style="width:${w}%;background:${p >= (lim.auto_min || 0.3) ? "var(--green)" : p > 0 ? "var(--yellow)" : "var(--red)"}"></i><b style="left:${Math.min(99, ((lim.auto_min || 0.3) / scale) * 100)}%"></b></div>
    <div class="kv"><span>До комиссий / после 3 комиссий</span><span class="num">${pct(s.gross, 3)} / ${pct(s.net, 3)}</span></div>
    ${s.depth_usd != null ? `<div class="kv"><span>По стакану на $${S.ov.scan.trade_amount}</span><span class="num ${cls(s.depth_usd)}">${money(s.depth_usd, { sign: true })}</span></div>` : ""}
    <div class="kv"><span>Безубыточность (комиссия ${s.fee_pct}% × 3)</span><span>${be.toFixed(2)}%</span></div>
    <div class="small muted" style="margin-top:6px">${s.legs.map((l) => `${l.side === "buy" ? "купить" : "продать"} ${esc(l.symbol)} по ${price(l.price)}`).join(" · ")}</div>
    ${s.executable && owner() ? `<button class="btn block ${tr().live ? "red" : "primary"}" style="margin-top:12px" data-act="tri" data-key="${esc(s.key)}">⚡ Исполнить на $${lim.max_trade}${tr().live ? "" : " (тест)"}</button>` : `<div class="tiny muted" style="margin-top:8px">${owner() ? `Чтобы исполнять кнопкой, подключи ключ ${esc(s.name)}` : ""}</div>`}
  </div>`;
}

function crossCard(s) {
  const p = s.depth_pct ?? s.net;
  return `<div class="card sig">
    <div class="row between"><div class="path">${coinIcon(s.symbol.split("/")[0]).replace('class="coin"', 'class="coin" style="width:24px;height:24px;font-size:8px;background:' + coinColor(s.symbol.split("/")[0]) + '"')}<span>${esc(s.symbol)}</span></div><div class="pct num ${cls(p)}">${pct(p, 2)}</div></div>
    <div class="small muted" style="margin-top:2px">купить на <b style="color:var(--text)">${esc(s.buy_name)}</b> по ${price(s.ask)} → продать на <b style="color:var(--text)">${esc(s.sell_name)}</b> по ${price(s.bid)}</div>
    <div style="margin-top:8px">
      <div class="kv"><span>Спред / после торговых комиссий</span><span class="num">${pct(s.gross)} / ${pct(s.net)}</span></div>
      ${s.depth_usd != null ? `<div class="kv"><span>По стакану на $${S.ov.scan.trade_amount}</span><span class="num ${cls(s.depth_usd)}">${money(s.depth_usd, { sign: true })}</span></div>` : ""}
      <div class="kv"><span>Плата за вывод ${s.withdraw_known ? "" : "(оценка)"}</span><span class="num">−${money(s.withdraw_usd)}</span></div>
      ${s.after_withdraw_usd != null ? `<div class="kv"><span><b>Итого после вывода</b></span><span class="num ${cls(s.after_withdraw_usd)}">${money(s.after_withdraw_usd, { sign: true })}</span></div>` : ""}
    </div>
    <div class="tiny muted" style="margin-top:6px">Межбиржевой арбитраж не исполняется автоматически: нужны деньги на обеих биржах и перевод монеты.</div>
  </div>`;
}

function renderSignals() {
  const v = $("#v-signals");
  if (!S.ov) { v.innerHTML = skeleton(); return; }
  const sc = S.ov.scan, t = tr();
  const list = sc ? (S.sigTab === "tri" ? sc.tri : sc.cross) : [];
  const near = sc ? (S.sigTab === "tri" ? sc.near_tri : sc.near_cross) : null;
  v.innerHTML = `
    <div class="card">
      <div class="row between">
        <div><div class="b">Сканирование ${S.ov.exchanges.length} бирж</div><div class="small muted">${sc ? `${ago(sc.finished_at)} · ${sc.duration} с · пар ${sc.symbols}` : "ещё не запускалось"}</div></div>
        <button class="btn primary sm" data-act="scan" ${S.scanning ? "disabled" : ""}>${S.scanning ? '<div class="spin"></div>' : "⚡ Скан"}</button>
      </div>
      ${sc && Object.keys(sc.errors).length ? `<div class="tiny muted" style="margin-top:8px">Не ответили: ${Object.keys(sc.errors).map(esc).join(", ")}</div>` : ""}
    </div>
    <div class="card" style="margin-top:10px">
      <div class="row between"><div><div class="b">⚙️ Автоторговля</div><div class="small muted">Лучший треугольник от ${t.limits ? t.limits.auto_min : "—"}% по стакану, до $${t.limits ? t.limits.max_trade : "—"} за цикл</div></div>
      <label class="switch"><input type="checkbox" data-act="auto" ${t.auto ? "checked" : ""} ${owner() ? "" : "disabled"}><span></span></label></div>
    </div>
    <div class="seg" style="margin-top:12px"><button data-act="sig-tab" data-v="tri" class="${S.sigTab === "tri" ? "on" : ""}">🔺 Треугольные ${sc ? `(${sc.tri.length})` : ""}</button><button data-act="sig-tab" data-v="cross" class="${S.sigTab === "cross" ? "on" : ""}">⇄ Межбиржевые ${sc ? `(${sc.cross.length})` : ""}</button></div>
    <div style="margin-top:10px">${list.length ? list.map(S.sigTab === "tri" ? triCard : crossCard).join("") : `<div class="card empty"><div class="e">🌙</div>${sc ? "Сейчас прибыльных связок нет" : "Нажми «Скан»"}${near ? `<div class="small" style="margin-top:10px">Ближе всего к прибыли: <b class="${cls(near.net)}">${pct(near.net, 3)}</b> после комиссий (${pct(near.gross, 3)} до)<br><span class="muted">${esc(near.text)}</span></div>` : ""}</div>`}</div>
    <div class="banner info" style="margin-top:12px">💡 Треугольники живут доли секунды. Перед исполнением бот заново запрашивает стаканы и пропускает сделку, если прибыль исчезла.</div>`;
}

async function doScan(btn) {
  if (S.scanning) return;
  S.scanning = true; haptic.tap();
  if (S.tab === "signals") renderSignals(); else busy(btn, true);
  try {
    const r = await api("/api/scan", {}, { timeout: 90000 });
    S.ov.scan = r.scan;
    const n = r.scan ? r.scan.tri.length + r.scan.cross.length : 0;
    toast(n ? `Найдено связок: ${n}` : "Готово: прибыльных связок нет");
  } catch (e) { fail(e); } finally {
    S.scanning = false; busy(btn, false);
    renderActive(true); renderHeader();
  }
}

async function runTriangle(key, btn) {
  const live = tr().live;
  if (live && !(await popup("Исполнить треугольник", "Три реальных ордера подряд. Перед отправкой бот проверит свежие стаканы.", "Исполнить"))) return;
  busy(btn, true);
  try {
    const r = await api("/api/triangle", { key }, { timeout: 60000 });
    if (!r.executed) toast("⏭ " + r.reason, true);
    else {
      const head = `${r.live ? "✅" : "🧪"} ${r.path}: ${money(r.pnl, { sign: true })} (${pct((r.pnl / r.amount) * 100, 3)})`;
      Sheet.open(r.live ? "Треугольник исполнен" : "Тест треугольника", `<div class="card"><div class="b" style="font-size:17px">${esc(head)}</div>
        <div class="kv" style="margin-top:8px"><span>Вложено</span><span class="num">${money(r.amount)}</span></div>
        <div class="kv"><span>Получено</span><span class="num">${money(r.final)}</span></div>
        ${(r.steps || []).map((s) => `<div class="small muted" style="margin-top:4px">${esc(s)}</div>`).join("")}
        ${r.reason ? `<div class="banner" style="margin-top:10px">⚠️ ${esc(r.reason)}</div>` : ""}
        ${r.stuck ? `<div class="banner red" style="margin-top:10px">Осталось не в USDT: ${esc(r.stuck)}</div>` : ""}</div>`);
      haptic.ok();
    }
    refresh();
  } catch (e) { fail(e); } finally { busy(btn, false); }
}

// ---------------------------------------------------------------------------
// Демо
// ---------------------------------------------------------------------------

function renderDemo() {
  const v = $("#v-demo");
  if (!S.ov) { v.innerHTML = skeleton(); return; }
  const d = S.demo || S.ov.demo;
  if (!d.accounts.length) {
    v.innerHTML = `<div class="card hero"><div class="label">Демо-торговля</div><div class="big">Виртуальные $</div>
      <div class="muted small" style="margin:4px 0 12px">Бот «торгует» по реальным стаканам и комиссиям. Так видно, есть ли прибыль, без риска.</div>
      <button class="btn primary block" data-act="demo-start">Запустить демо</button><div style="height:12px"></div></div>`;
    return;
  }
  const pnl = d.total_equity - d.total_start, hours = d.created_at ? (Date.now() / 1000 - d.created_at) / 3600 : 0;
  const trades = d.accounts.reduce((s, a) => s + a.trades, 0), wins = d.accounts.reduce((s, a) => s + a.wins, 0);
  const series = d.accounts.filter((a) => a.curve.length > 1).map((a, i) => ({ name: a.label, color: SERIES[i % SERIES.length], points: a.curve.map((p) => [p[0], (p[1] / a.start - 1) * 100]) }));
  const hist = S.demo && S.demo.history;
  v.innerHTML = `
    <div class="card hero">
      <div class="row between"><div class="label">Все демо-счета</div><span class="pill ${d.active ? "ok" : "gray"}">${d.active ? "● работают" : "остановлены"} · ${hours.toFixed(1)} ч</span></div>
      <div class="big num">${moneyHero(d.total_equity)}</div>
      <div><span class="${cls(pnl)} b num">${money(pnl, { sign: true })} (${pct((pnl / d.total_start) * 100)})</span> <span class="muted small">из ${money(d.total_start)}</span></div>
      <div class="grid2" style="margin:12px 0 4px"><div><div class="tiny muted">Сделок</div><div class="b num">${trades}</div></div><div><div class="tiny muted">Прибыльных</div><div class="b num">${trades ? Math.round((wins / trades) * 100) : 0}%</div></div></div>
    </div>
    <div class="card" style="margin-top:10px">
      <div class="b">Доходность счетов, %</div>
      <div class="chart-wrap mid" id="demoChart">${series.length ? "" : '<div class="chart-empty">Сделок пока не было. Бот ждёт связку, которая прибыльна по стакану.</div>'}</div>
      ${series.length ? `<div class="legend">${series.map((s) => `<span><i style="background:${s.color}"></i>${esc(s.name)}</span>`).join("")}</div>` : ""}
    </div>
    <div class="section-title">Счета</div>
    <div class="grid2">${d.accounts.map((a, i) => `<div class="card tight" style="margin:0">
      <div class="small muted ellipsis">${a.strategy === "tri" ? "🔺" : "⇄"} ${esc(a.label)}</div>
      <div class="b num" style="font-size:17px;margin-top:2px">${money(a.equity)}</div>
      <div class="small num ${cls(a.pct)}">${pct(a.pct)}</div>
      <div style="height:38px;margin:6px -4px 2px" id="sp-${i}"></div>
      <div class="tiny muted">${a.trades} сделок · ${a.wins}✅ ${a.trades - a.wins}🔻</div>
      ${a.top_reject ? `<div class="tiny dim ellipsis" title="${esc(a.top_reject.reason)}">чаще всего: ${esc(a.top_reject.reason)}</div>` : ""}
    </div>`).join("")}</div>
    <div class="row" style="margin-top:12px;gap:10px">
      <button class="btn grow" data-act="demo-start">↻ Перезапустить</button>
      ${d.active ? `<button class="btn soft-red grow" data-act="demo-stop">⏸ Остановить</button>` : ""}
    </div>
    <div class="section-title">История сделок</div>
    <div id="demoHist">${hist ? (hist.length ? `<div class="list">${hist.slice(0, 50).map((h) => `<div class="item"><div class="coin" style="background:${h.strategy === "tri" ? "var(--accent)" : "#4f8cff"}">${h.strategy === "tri" ? "🔺" : "⇄"}</div><div class="grow"><div class="t ellipsis">${esc(h.symbol)}</div><div class="s ellipsis">${when(h.at)} · ${esc(h.account)} · ${esc(h.route)}</div></div><div class="r"><div class="b num ${cls(h.profit)}">${money(h.profit, { sign: true })}</div><div class="s num">${pct(h.pct, 3)} / ждали ${pct(h.expected, 3)}</div></div></div>`).join("")}</div>` : `<div class="card empty"><div class="e">⏳</div>Сделок пока нет</div>`) : `<div class="skel" style="height:120px"></div>`}</div>`;
  if (series.length) Charts.mount($("#demoChart"), new LineChart($("#demoChart"), series, { fmt: (x) => pct(x, 2), baseline: 0, area: false }));
  d.accounts.forEach((a, i) => spark($(`#sp-${i}`), a.curve, a.pct >= 0 ? css("--green") : css("--red")));
}

async function loadDemo() {
  try { S.demo = await api("/api/demo"); if (S.tab === "demo") renderDemo(); } catch (e) { fail(e); }
}

function demoStartSheet() {
  const def = (S.ov.demo.default_amounts || [5, 50, 500]).map(String);
  const opts = ["5", "10", "25", "50", "100", "500", "1000"];
  S.demoSel = new Set(def);
  const draw = () => opts.map((o) => `<div class="chip ${S.demoSel.has(o) ? "on" : ""}" data-act="demo-amt" data-v="${o}">$${o}</div>`).join("");
  Sheet.open("Запуск демо", `<div class="muted small">Выбери до 5 сумм. На каждую создаются счета для межбиржевой и треугольной стратегии. Старые демо-счета обнулятся.</div>
    <div class="chips" id="demoChips" style="flex-wrap:wrap;margin-top:12px">${draw()}</div>`, {
    foot: `<div style="padding:10px 16px"><button class="btn primary block" data-act="demo-go">Запустить</button></div>`,
  });
  S.redrawDemoChips = () => { $("#demoChips").innerHTML = draw(); };
}

// ---------------------------------------------------------------------------
// Портфель
// ---------------------------------------------------------------------------

function renderWallet() {
  const v = $("#v-wallet");
  if (!S.ov) { v.innerHTML = skeleton(); return; }
  const t = tr();
  if (!t.keys || !t.keys.length) {
    v.innerHTML = `${keysHelp()}${phantomBlock()}<div class="section-title">Журнал сделок</div>${(t.log || []).length ? `<div class="list">${t.log.map(tradeRow).join("")}</div>` : `<div class="card empty">Сделок пока нет</div>`}`;
    return;
  }
  const p = S.portfolio;
  const coins = {};
  if (p && p.exchanges) for (const e of p.exchanges) for (const c of e.coins || []) coins[c.coin] = (coins[c.coin] || 0) + (c.usd || 0);
  const alloc = Object.entries(coins).filter(([, u]) => u >= 0.01).sort((a, b) => b[1] - a[1]);
  const top = alloc.slice(0, 6), other = alloc.slice(6).reduce((s, [, u]) => s + u, 0);
  const items = top.map(([c, u]) => ({ label: c, value: u, color: coinColor(c) }));
  if (other > 0) items.push({ label: "Другие", value: other, color: "#64748b" });
  const total = p ? p.total_usd : null;
  const hist = (p && p.history) || t.history || [];
  const triLog = (t.log || []).filter((r) => r.pnl_usd != null && r.live).reverse();
  let cum = 0; const pnlCurve = triLog.map((r) => [r.at, (cum += r.pnl_usd)]);
  v.innerHTML = `
    <div class="card hero">
      <div class="row between"><div class="label">Баланс на биржах</div><button class="icon-btn" style="width:32px;height:32px" data-act="refresh-portfolio">${ICON.refresh}</button></div>
      <div class="big num">${total != null ? moneyHero(total) : '<span class="skel" style="display:inline-block;width:160px;height:34px"></span>'}</div>
      <div class="muted small">${p ? "обновлено " + ago(p.updated_at) : "загружаю балансы…"}</div>
      <div class="chart-wrap" id="pfChart">${hist.length < 2 ? '<div class="chart-empty">График баланса строится каждые 15 минут</div>' : ""}</div>
    </div>
    ${items.length ? `<div class="card" style="margin-top:10px"><div class="donut-wrap" id="donut"><div class="col grow">${items.map((i) => `<div class="row between small"><span><i style="display:inline-block;width:9px;height:9px;border-radius:3px;background:${i.color};margin-right:6px"></i>${esc(i.label)}</span><span class="num b">${((i.value / (total || 1)) * 100).toFixed(1)}%</span></div>`).join("")}</div></div></div>` : ""}
    ${p ? p.exchanges.map((e) => `<div class="section-title">${esc(e.name)} ${e.error ? "" : `<span class="num" style="text-transform:none">${money(e.total_usd)}</span>`}</div>
      ${e.error ? `<div class="banner red">❌ ${esc(e.error)}</div>` : (e.coins || []).length ? `<div class="list">${e.coins.filter((c) => (c.usd || 0) >= 0.01 || c.usd == null).slice(0, 20).map((c) => `<div class="item press" data-act="coin" data-ex="${esc(e.exchange)}" data-coin="${esc(c.coin)}">${coinIcon(c.coin)}<div class="grow"><div class="t">${esc(c.coin)}</div><div class="s num">${amount(c.total)}${c.total - c.free > 1e-12 ? ` · в ордерах ${amount(c.total - c.free)}` : ""}</div></div><div class="r"><div class="b num">${c.usd != null ? money(c.usd) : "—"}</div><div class="s">${c.coin === S.ov.quote ? "" : "продать →"}</div></div></div>`).join("")}</div>` : `<div class="card empty">Пусто. Если деньги на бирже есть, переведи их на торговый счёт (Funding → Trading / Unified)</div>`}`).join("") : ""}
    ${phantomBlock()}
    <div class="section-title">Открытые ордера <a data-act="load-orders">Обновить</a></div>
    <div id="ordersBox">${ordersHtml()}</div>
    ${pnlCurve.length > 1 ? `<div class="section-title">Результат циклов</div><div class="card"><div class="chart-wrap mid" id="pnlChart"></div></div>` : ""}
    <div class="section-title">Журнал сделок</div>
    ${(t.log || []).length ? `<div class="list">${t.log.map(tradeRow).join("")}</div>` : `<div class="card empty"><div class="e">🧾</div>Сделок через бота пока не было</div>`}`;
  if (hist.length >= 2) Charts.mount($("#pfChart"), new LineChart($("#pfChart"), [{ points: hist, color: "#22d3ee" }], { fmt: (x) => money(x), axis: false, padBottom: 6, baseline: hist[0][1] }));
  if (items.length) { const dn = new Donut($("#donut"), items, [total != null ? money(total, { d: total >= 1000 ? 0 : 2 }) : "", "всего"]); Charts.mount($("#donut"), dn); }
  if (pnlCurve.length > 1) Charts.mount($("#pnlChart"), new LineChart($("#pnlChart"), [{ points: pnlCurve, color: cum >= 0 ? css("--green") : css("--red") }], { fmt: (x) => money(x, { sign: true }), baseline: 0 }));
}

function ordersHtml() {
  if (S.orders == null) return `<div class="skel" style="height:60px"></div>`;
  const rows = [];
  for (const e of S.orders.exchanges) {
    if (e.error) { rows.push(`<div class="item"><div class="grow"><div class="t">${esc(e.name)}</div><div class="s">❌ ${esc(e.error)}</div></div></div>`); continue; }
    for (const o of e.orders) rows.push(`<div class="item"><div class="coin" style="background:${o.side === "buy" ? "var(--green)" : "var(--red)"}">${o.side === "buy" ? "↗" : "↘"}</div><div class="grow"><div class="t">${esc(o.symbol)} <span class="muted small">${esc(e.name)}</span></div><div class="s num">${o.side === "buy" ? "покупка" : "продажа"} ${amount(o.amount)} по ${price(o.price)} · исполнено ${amount(o.filled || 0)}</div></div><button class="btn soft-red sm" data-act="cancel-order" data-ex="${esc(e.exchange)}" data-id="${esc(o.id)}" data-symbol="${esc(o.symbol)}">Отменить</button></div>`);
  }
  return rows.length ? `<div class="list">${rows.join("")}</div>` : `<div class="card empty" style="padding:16px">Открытых ордеров нет</div>`;
}

async function loadPortfolio() {
  if (!tr().keys || !tr().keys.length) return;
  try {
    S.portfolio = await api("/api/portfolio", undefined, { timeout: 40000 });
    store.set("portfolio", S.portfolio);
    if (S.tab === "wallet" || S.tab === "home") renderActive(true);
  } catch (e) { if (S.tab === "wallet") fail(e); }
}
async function loadOrders() {
  if (!tr().working || !tr().working.length) { S.orders = { exchanges: [] }; return; }
  try { S.orders = await api("/api/orders"); } catch (e) { S.orders = { exchanges: [{ name: "Ошибка", error: e.message }] }; }
  const box = $("#ordersBox"); if (box) box.innerHTML = ordersHtml();
}

// ---------------------------------------------------------------------------
// Phantom, проверка, алерты, цены
// ---------------------------------------------------------------------------

function phantomBlock() {
  const ph = S.phantom;
  if (!ph || !ph.address) return `<div class="section-title">Кошелёк Phantom</div><div class="card press" data-act="phantom"><div class="row" style="gap:12px"><div class="ic-round">${ICON.ghost}</div><div class="grow"><div class="b">Подключить Phantom</div><div class="small muted">Только просмотр баланса по публичному адресу. Сид-фраза не нужна</div></div><span class="link">→</span></div></div>`;
  const rows = (ph.assets || []).slice(0, 15).map((a) => `<div class="item">${coinIcon(a.coin)}<div class="grow"><div class="t">${esc(a.coin)}</div><div class="s num">${amount(a.amount)}</div></div><div class="r"><div class="b num">${a.usd != null ? money(a.usd) : "—"}</div></div></div>`).join("");
  return `<div class="section-title">👻 Phantom <span class="num" style="text-transform:none">${money(ph.total_usd || 0)}</span> <a data-act="phantom-refresh">Обновить</a></div>
    ${ph.error ? `<div class="banner red" style="margin-bottom:8px"><div>⚠️ ${esc(ph.error)}</div></div>` : ""}
    ${rows ? `<div class="list">${rows}</div>` : `<div class="card empty" style="padding:16px">${ph.error ? "Нет данных" : "Кошелёк пуст"}</div>`}
    <div class="tiny dim" style="margin-top:6px">${esc(shortAddr(ph.address))} · только просмотр · обновлено ${ph.updated_at ? ago(ph.updated_at) : "—"}</div>`;
}

const shortAddr = (a) => (a && a.length > 10 ? a.slice(0, 4) + "…" + a.slice(-4) : a || "");

async function loadPhantom(force = false) {
  try {
    S.phantom = await api("/api/phantom" + (force ? "?force=1" : ""), undefined, { timeout: 30000 });
    store.set("phantom", S.phantom);
    if (S.tab === "wallet") renderActive(true);
    const box = $("#phBlock"); if (box && S.phantom && S.phantom.address) box.innerHTML = phantomBlock();
    const inp = $("#phAddr"); if (inp && !inp.value && document.activeElement !== inp && S.phantom) inp.value = S.phantom.address || "";
  } catch (e) { if (force) fail(e); }
}

function phantomSheet() {
  const ph = S.phantom;
  Sheet.open("Кошелёк Phantom", `
    <div class="banner red" style="margin-bottom:12px"><div>🔒 <b>Сид-фразу (12 слов) и приватный ключ не вводи никуда</b>: ни сюда, ни боту, ни «поддержке». По ним крадут деньги. Здесь нужен только <b>публичный адрес</b>.</div></div>
    <div class="card">
      <div class="b">Публичный адрес Solana</div>
      <div class="small muted" style="margin:4px 0 10px">Phantom → «Получить» (Receive) → Solana → «Копировать». По адресу можно только смотреть баланс.</div>
      <input id="phAddr" class="input" placeholder="например 7xKX…" value="${esc((ph && ph.address) || "")}" autocomplete="off" autocapitalize="off" spellcheck="false" ${owner() ? "" : "disabled"}>
      <div class="row" style="gap:8px;margin-top:10px">
        <button class="btn primary grow" data-act="phantom-save" ${owner() ? "" : "disabled"}>Сохранить</button>
        ${ph && ph.address ? `<button class="btn soft-red" data-act="phantom-off" ${owner() ? "" : "disabled"}>Отключить</button>` : ""}
      </div>
    </div>
    <div id="phBlock">${ph && ph.address ? phantomBlock() : ""}</div>
    <div class="tiny dim" style="margin-top:12px">Phantom не биржа: это твой личный кошелёк, и бот им не торгует. Чтобы торговать монетами из Phantom, переведи их на Bybit или BingX (сеть Solana, адрес пополнения на бирже).</div>`, { full: true });
}

async function savePhantom(btn, off = false) {
  const address = off ? "" : ($("#phAddr").value || "").trim();
  if (!off && address.split(/\s+/).length >= 10) { toast("Это сид-фраза! Не вводи её никуда. Нужен публичный адрес", true); $("#phAddr").value = ""; return; }
  btn.disabled = true;
  try {
    S.phantom = await api("/api/phantom", { address }, { timeout: 30000 });
    haptic.ok(); toast(off ? "Кошелёк отключён" : "Кошелёк подключён 👻");
    phantomSheet(); if (S.tab === "wallet") renderActive(true);
  } catch (e) { fail(e); } finally { btn.disabled = false; }
}

const docSvg = (p) => `<svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="3" stroke-linecap="round" stroke-linejoin="round">${p}</svg>`;
const DOC_ICON = { ok: docSvg('<path d="M6 12.5l4 4 8-9"/>'), warn: docSvg('<path d="M12 6v7M12 17.5v.5"/>'), bad: docSvg('<path d="M7 7l10 10M17 7 7 17"/>'), info: docSvg('<path d="M12 11v6M12 7v.5"/>') };

async function doctorSheet() {
  Sheet.open("Проверка всего", `<div class="card empty"><div class="spin dark" style="width:22px;height:22px;margin:0 auto 10px"></div>Проверяю биржи, ключи, сервис… до 30 секунд</div>`, { full: true });
  try {
    const r = await api("/api/doctor", undefined, { timeout: 90000 });
    if (!Sheet.isOpen() || $("#sheetTitle").textContent !== "Проверка всего") return;
    const groups = {};
    for (const c of r.checks) (groups[c.group] = groups[c.group] || []).push(c);
    const s = r.summary;
    const verdict = s.bad ? ["red", "Есть проблемы: исправь красные пункты"] : s.warn ? ["", "Работает, но посмотри жёлтые пункты"] : ["info", "Всё в порядке"];
    Sheet.body().innerHTML = `
      <div class="grid4" style="margin-bottom:10px">
        <div class="stat center"><div class="v num up">${s.ok}</div><div class="k">ок</div></div>
        <div class="stat center"><div class="v num" style="color:var(--yellow)">${s.warn}</div><div class="k">внимание</div></div>
        <div class="stat center"><div class="v num down">${s.bad}</div><div class="k">ошибки</div></div>
        <div class="stat center"><div class="v num">${s.info}</div><div class="k">инфо</div></div>
      </div>
      <div class="banner ${verdict[0]}">${verdict[1]}</div>
      ${Object.entries(groups).map(([g, list]) => `<div class="section-title">${esc(g)}</div><div class="list">${list.map((c) => `<div class="item" style="align-items:flex-start"><div class="doc-ic ${c.status}">${DOC_ICON[c.status]}</div><div class="grow"><div class="t">${esc(c.title)}</div><div class="s" style="white-space:normal">${esc(c.text)}</div></div></div>`).join("")}</div>`).join("")}
      <button class="btn block ghost" style="margin-top:14px" data-act="doctor">Проверить ещё раз</button>`;
    s.bad ? haptic.err() : haptic.ok();
  } catch (e) { Sheet.body().innerHTML = `<div class="banner red">❌ ${esc(e.message)}</div>`; }
}

function alertsHtml(list) {
  if (!list.length) return `<div class="card empty" style="padding:16px"><div class="e">🔔</div>Алертов нет. Бот пришлёт сообщение в Telegram, когда цена дойдёт до уровня</div>`;
  return `<div class="list">${list.map((a) => {
    const dist = a.start_price ? ((a.price / a.start_price - 1) * 100) : null;
    return `<div class="item">${coinIcon(a.coin)}<div class="grow"><div class="t">${esc(a.coin)} ${a.op === "<" ? "≤" : "≥"} ${price(a.price)}</div><div class="s">${esc(a.name)}${dist != null ? ` · ${dist > 0 ? "+" : ""}${dist.toFixed(2)}% от цены создания` : ""}</div></div><button class="icon-btn" style="width:36px;height:36px;background:var(--red-bg)" data-act="alert-del" data-id="${esc(a.id)}" aria-label="Удалить">${ICON.x}</button></div>`;
  }).join("")}</div>`;
}

async function alertsSheet() {
  Sheet.open("Ценовые алерты", `
    <div class="card">
      <div class="row" style="gap:8px"><input id="alertText" class="input grow" placeholder="TON < 5" autocomplete="off" autocapitalize="characters"><button class="btn primary" data-act="alert-add">Добавить</button></div>
      <div class="chips" style="margin-top:10px">${["TON < 5", "BTC > 100000", "SOL +5%", "ETH -3%"].map((x) => `<button class="chip" data-act="alert-ex" data-v="${esc(x)}">${esc(x)}</button>`).join("")}</div>
      <div class="tiny dim" style="margin-top:8px">Цена или процент от текущей. Можно указать биржу: «TON &lt; 5 bingx». Срабатывает один раз, проверка каждые 30 секунд.</div>
    </div>
    <div class="section-title">Активные</div><div id="alertsBox"><div class="skel" style="height:60px"></div></div>`, { full: true });
  try { const r = await api("/api/alerts"); $("#alertsBox").innerHTML = alertsHtml(r.alerts); } catch (e) { $("#alertsBox").innerHTML = `<div class="banner red">${esc(e.message)}</div>`; }
}

async function addAlert(btn) {
  const text = ($("#alertText").value || "").trim();
  if (!text) { $("#alertText").focus(); return; }
  btn.disabled = true;
  try {
    const r = await api("/api/alerts", { text });
    $("#alertsBox").innerHTML = alertsHtml(r.alerts); $("#alertText").value = "";
    haptic.ok(); toast("🔔 Алерт добавлен");
  } catch (e) { fail(e); } finally { btn.disabled = false; }
}

async function delAlert(id) {
  try { const r = await api("/api/alerts/delete", { id }); $("#alertsBox").innerHTML = alertsHtml(r.alerts); haptic.tap(); } catch (e) { fail(e); }
}

function pricesSheet() {
  Sheet.open("Цена на Bybit и BingX", `
    <div class="card">
      <div class="row" style="gap:8px"><input id="priceCoin" class="input grow" placeholder="TON" autocomplete="off" autocapitalize="characters" value="${esc(store.get("priceCoin", "TON"))}"><button class="btn primary" data-act="price-go">Найти</button></div>
      <div class="chips" style="margin-top:10px">${["BTC", "ETH", "SOL", "TON", "XRP", "DOGE"].map((x) => `<button class="chip" data-act="price-coin" data-v="${x}">${x}</button>`).join("")}</div>
    </div>
    <div id="pricesBox" style="margin-top:10px"></div>`, { full: true });
  loadPrices();
}

async function loadPrices(btn) {
  const coin = ($("#priceCoin").value || "").trim().toUpperCase();
  if (!coin) return;
  store.set("priceCoin", coin);
  const box = $("#pricesBox");
  box.innerHTML = `<div class="skel" style="height:120px"></div>`;
  if (btn) btn.disabled = true;
  try {
    const r = await api("/api/price?coin=" + encodeURIComponent(coin), undefined, { timeout: 40000 });
    const mid = (x) => (x.bid && x.ask ? (x.bid + x.ask) / 2 : x.last || x.bid || x.ask);
    const rows = r.rows.filter(mid).sort((a, b) => mid(a) - mid(b));
    if (!rows.length) { box.innerHTML = `<div class="card empty">Не нашёл ${esc(coin)}/${esc(r.quote)} на биржах из списка</div>`; return; }
    const lo = rows[0], hi = rows[rows.length - 1];
    const gap = rows.length > 1 ? ((hi.bid || mid(hi)) / (lo.ask || mid(lo)) - 1) * 100 : null;
    box.innerHTML = `<div class="list">${rows.map((x) => `<div class="item"><div class="grow"><div class="t">${esc(x.name)}</div><div class="s num">${x.ask ? `купить ${price(x.ask)} · продать ${price(x.bid)}` : ""}</div></div><div class="r"><div class="b num">${price(mid(x))}</div><div class="s num ${cls(x.change || 0)}">${x.change != null ? pct(x.change) : ""}</div></div></div>`).join("")}</div>
      ${gap != null ? `<div class="banner ${gap > 1.5 ? "info" : ""}" style="margin-top:10px"><div>Купить на ${esc(lo.name)}, продать на ${esc(hi.name)}: <b class="num">${pct(gap)}</b> до комиссий. ${gap < 1.5 ? "Перевод между биржами съест эту разницу комиссиями." : "Разница заметная: проверь комиссию вывода и сеть, прежде чем переводить."}</div></div>` : ""}`;
  } catch (e) { box.innerHTML = `<div class="banner red">❌ ${esc(e.message)}</div>`; } finally { if (btn) btn.disabled = false; }
}

function keysHelp() {
  return `<div class="card">
    <div class="b" style="font-size:17px">🔑 Подключи биржу</div>
    <div class="muted small" style="margin-top:4px">Чтобы торговать из приложения, нужен API-ключ биржи. Это 5 минут с телефона.</div>
    <ol class="steps">
      <li>На <b>сайте</b> биржи (в браузере, «версия для ПК»): <b>Профиль → API → Создать ключ</b> (системный).</li>
      <li>Права: <b>чтение + спотовая торговля</b>. <b>Вывод (Withdraw) не включай</b>: тогда деньги нельзя вывести даже при утечке ключа.</li>
      <li>IP-ограничение не ставь. Ключ Bybit без IP живёт ~3 месяца (бот напомнит), ключ BingX живёт, пока бот им пользуется.</li>
      <li>Render → сервис <b>ai-casher-bot</b> → <b>Environment</b>: <b>BYBIT_API_KEY</b> и <b>BYBIT_API_SECRET</b>, <b>BINGX_API_KEY</b> и <b>BINGX_API_SECRET</b> → <b>Save</b>.</li>
      <li>Через минуту бот перезапустится, и здесь появится твой баланс.</li>
    </ol>
    <div class="banner" style="margin-top:12px">Ключи никогда не присылай в чат. Только в настройки Render.</div>
  </div>`;
}

// ---------------------------------------------------------------------------
// Настройки
// ---------------------------------------------------------------------------

function settingsSheet() {
  const t = tr(), st = S.ov.settings;
  const risk = Object.entries(st).filter(([, s]) => s.risk), other = Object.entries(st).filter(([, s]) => !s.risk);
  const row = ([k, s]) => `<div class="setting"><div class="grow"><div class="small">${esc(s.desc)}</div><div class="tiny dim">${k} · от ${s.min} до ${s.max}</div></div><input inputmode="decimal" data-set="${k}" value="${s.value}" ${s.risk && !owner() ? "disabled" : ""}></div>`;
  Sheet.open("Настройки", `
    <div class="card">
      <div class="row between"><div><div class="b">Реальная торговля</div><div class="small muted">${t.live ? "🔴 ордера уходят на биржу" : "🧪 тест: ордера не отправляются"}</div></div>
      <label class="switch"><input type="checkbox" data-act="live" ${t.live ? "checked" : ""} ${owner() ? "" : "disabled"}><span></span></label></div>
    </div>
    <button class="btn soft-red block" style="margin-top:10px" data-act="stop">🛑 Стоп-кран: выключить всё и отменить ордера</button>
    <div class="section-title">Лимиты риска <span class="lock">только владелец</span></div>
    <div class="list">${risk.map(row).join("")}</div>
    <div class="section-title">Поиск связок</div>
    <div class="list">${other.map(row).join("")}</div>
    <div class="section-title">Биржи</div>
    <div class="list">${S.ov.exchanges.map((e) => { const k = (t.keys || []).find((x) => x.exchange === e.id); return `<div class="item"><div class="grow"><div class="t">${esc(e.name)}</div></div>${k ? `<span class="pill ${k.status === "ok" ? "ok" : k.status === "unchecked" ? "gray" : "bad"}">${k.status === "ok" ? "ключ работает" : k.status === "unchecked" ? "проверяется" : "ошибка ключа"}</span>` : '<span class="pill gray">без ключа</span>'}</div>`; }).join("")}</div>
    <div class="tiny dim center" style="margin-top:14px">Изменения сохраняются сразу и переживают перезапуск.</div>`, { full: true });
}

async function saveSetting(input) {
  const key = input.dataset.set, value = input.value.replace(",", ".");
  try {
    const r = await api("/api/set", { key, value: Number(value) });
    S.ov.settings = r.settings; toast("Сохранено ✅"); refresh();
  } catch (e) { fail(e); input.value = S.ov.settings[key].value; }
}

async function toggleLive(input) {
  const want = input.checked;
  input.checked = !want;
  if (want) {
    const L = tr().limits;
    if (!(await confirmBox(`Включить реальную торговлю?\n\nОрдера будут уходить на биржу с настоящими деньгами. Лимит на сделку $${L.max_trade}, на убыток за день $${L.daily_loss}.`))) return;
    if (!(await popup("Точно?", "Прибыль не гарантирована. Ставь только деньги, которые не жалко потерять.", "Да, торговать реально"))) return;
  }
  try {
    const r = await api("/api/mode", want ? { live: true, confirm: "LIVE" } : { live: false });
    S.ov.trading.live = r.live; input.checked = r.live;
    toast(r.live ? "🔴 Реальный режим включён" : "🧪 Тестовый режим");
    renderHeader(); renderActive(true);
    if (Sheet.isOpen() && $("#sheetTitle").textContent === "Настройки") settingsSheet();
  } catch (e) { fail(e); }
}

async function toggleAuto(input) {
  const on = input.checked;
  if (on && tr().live && !(await confirmBox("Включить автоторговлю в РЕАЛЬНОМ режиме? Бот сам будет исполнять треугольники в пределах лимитов."))) { input.checked = false; return; }
  try {
    const r = await api("/api/auto", { on });
    S.ov.trading.auto = r.auto; toast(r.auto ? "⚙️ Автоторговля включена" : "⏸ Автоторговля выключена");
    renderHeader(); renderActive(true);
  } catch (e) { input.checked = !on; fail(e); }
}

async function panic() {
  if (!(await popup("Стоп-кран", "Выключить автоторговлю и реальный режим, отменить все открытые ордера?", "Остановить всё"))) return;
  try { const r = await api("/api/stop", {}); Sheet.close(); toast(r.text.split("\n")[0]); refresh(); loadOrders(); } catch (e) { fail(e); }
}

// ---------------------------------------------------------------------------
// ИИ
// ---------------------------------------------------------------------------

function chatHtml() {
  if (!S.chat.length) {
    return `<div class="empty" style="padding:16px 4px"><div class="e">🤖</div><div class="b" style="color:var(--text)">ИИ-трейдер и аналитик</div><div class="small" style="margin-top:4px">Видит комиссии, сигналы, демо, балансы. Может подготовить ордер, но исполнится он только после твоей кнопки.</div></div>
      <div class="chips" style="flex-wrap:wrap;justify-content:center">${["Почему нет прибыльных сделок?", "Разбор комиссий моих бирж", "Купи BTC на 5$ на bybit", "Что поменять в настройках?"].map((q) => `<div class="chip" data-act="ai-q" data-v="${esc(q)}">${esc(q)}</div>`).join("")}</div>`;
  }
  return `<div class="chat">${S.chat.map((m, i) => {
    if (m.role === "me") return `<div class="msg me">${esc(m.text)}</div>`;
    if (m.role === "typing") return `<div class="msg ai typing"><span></span><span></span><span></span></div>`;
    if (m.role === "err") return `<div class="msg ai" style="border-color:var(--red)">⚠️ ${esc(m.text)}</div>`;
    const props = (m.proposals || []).map((p, j) => `<button class="btn sm soft-green" style="margin:6px 6px 0 0" data-act="ai-set" data-i="${i}" data-j="${j}">${esc(p.desc)}: ${p.old} → <b>${p.value}</b></button>`).join("");
    const plans = (m.plans || []).map((p) => planHtml(p) + `<div class="row" style="gap:8px;margin-top:8px"><button class="btn ghost sm grow" data-act="plan-no" data-id="${p.id}">Отмена</button><button class="btn sm grow ${p.live ? "red" : "primary"}" data-act="plan-ok" data-id="${p.id}" data-live="${p.live ? 1 : 0}">${p.live ? "Отправить ордер" : "Выполнить тест"}</button></div>`).join("");
    return `<div class="msg ai">${m.html}${m.errors && m.errors.length ? `<div class="small" style="color:var(--red);margin-top:6px">${m.errors.map(esc).join("<br>")}</div>` : ""}${props ? `<div>${props}</div>` : ""}${m.model ? `<div class="tiny dim" style="margin-top:6px">${esc(m.model)}</div>` : ""}</div>${plans ? `<div style="align-self:stretch">${plans}</div>` : ""}`;
  }).join("")}</div>`;
}

function aiSheet() {
  if (!S.ov.ai) {
    Sheet.open("ИИ-помощник", `<div class="card"><div class="b">ИИ не подключён</div><ol class="steps"><li>Открой <b>console.groq.com/keys</b>, войди через Google (бесплатно, без карты).</li><li><b>Create API Key</b> → скопируй ключ (начинается с gsk_).</li><li>Render → <b>Environment</b> → <b>AI_API_KEY</b> = ключ → <b>Save</b>.</li></ol></div>`);
    return;
  }
  Sheet.open("ИИ-трейдер", `<div id="chatBox">${chatHtml()}</div>`, {
    full: true,
    foot: `<form class="composer" id="aiForm"><input id="aiInput" placeholder="Спроси или попроси купить…" autocomplete="off" maxlength="1500"><button type="submit">➤</button></form>`,
  });
  $("#aiForm").addEventListener("submit", (e) => { e.preventDefault(); const q = $("#aiInput").value.trim(); if (q) askAI(q); });
  scrollChat();
}
function scrollChat() { const b = Sheet.body(); b.scrollTop = b.scrollHeight; }
function redrawChat() { const box = $("#chatBox"); if (box) { box.innerHTML = chatHtml(); scrollChat(); } }

async function askAI(q) {
  if (S.chat.some((m) => m.role === "typing")) return;
  const input = $("#aiInput"); if (input) input.value = "";
  S.chat.push({ role: "me", text: q }, { role: "typing" });
  redrawChat(); haptic.tap();
  try {
    const r = await api("/api/ai", { q }, { timeout: 90000 });
    S.chat.pop(); S.chat.push({ role: "ai", ...r });
  } catch (e) { S.chat.pop(); S.chat.push({ role: "err", text: e.message }); }
  redrawChat();
}

// ---------------------------------------------------------------------------
// Навигация, обновление, события
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Автоторговля: что делает и почему нет сделок
// ---------------------------------------------------------------------------

function autoState(t) {
  const a = t.auto_status || {};
  const blocked = Object.keys(a.blocked_now || {});
  if (!t.auto) return { cls: "gray", icon: "⏸", text: "выключена" };
  if (blocked.length && blocked.length >= (t.working || []).length) return { cls: "bad", icon: "⛔", text: "не может торговать" };
  if (a.funding && Object.keys(a.funding).length) return { cls: "bad", icon: "📥", text: "деньги на Funding" };
  if (a.low_money && Object.keys(a.low_money).length) return { cls: "bad", icon: "💵", text: "мало денег на счёте" };
  if (!a.at) return { cls: "gray", icon: "⏳", text: "ждёт первого скана" };
  if (a.executed) return { cls: "ok", icon: "✅", text: "исполнила цикл" };
  return { cls: "ok", icon: "🟢", text: "ждёт выгодный цикл" };
}

function autoCard() {
  const t = tr();
  if (!owner() || !(t.keys || []).length) return "";
  const a = t.auto_status || {};
  const st = autoState(t);
  const need = a.need ?? (t.limits || {}).auto_min ?? 0.3;
  const best = a.best ? a.best.net : null;
  const fill = best == null ? 0 : Math.max(0, Math.min(100, (best / need) * 100));
  const problems = Object.entries(a.blocked_now || {}).map(([ex, why]) => `<div class="tiny down" style="margin-top:6px">⛔ ${esc(exName(ex))}: ${esc(why)}</div>`).join("");
  const funding = a.funding || {};
  const money_ = Object.entries(a.low_money || {}).map(([ex, v]) => funding[ex]
    ? `<div class="tiny down" style="margin-top:6px">📥 ${esc(exName(ex))}: $${amount(funding[ex])} лежат на счёте Funding. Переведи на торговый счёт (Активы → Перевод)</div>`
    : `<div class="tiny down" style="margin-top:6px">💵 ${esc(exName(ex))}: на торговом счёте ${amount(v)} ${esc(S.ov.quote)}, для цикла нужно от $${amount((a.need_usd || {})[ex] || 5)}. Лучше $10–20</div>`).join("");
  return `<div class="card press auto-card" data-act="why" style="margin-top:10px">
    <div class="row between"><div class="b">⚙️ Автопилот</div><span class="pill ${st.cls}">${st.icon} ${esc(st.text)}</span></div>
    ${t.auto && a.at ? `<div class="small muted" style="margin-top:6px">Проверка ${ago(a.at)} · циклов ${a.triangles ?? 0} · подходящих ${a.candidates ?? 0}</div>
    <div class="meter" style="margin-top:10px"><div style="width:${fill.toFixed(0)}%"></div></div>
    <div class="row between tiny muted" style="margin-top:4px"><span>лучший ${best == null ? "—" : pct(best, 3)}</span><span>нужно ≥ ${need}%</span></div>` : `<div class="small muted" style="margin-top:6px">${t.auto ? "Скоро первая проверка" : "Включи переключатель выше: в тесте безопасно"}</div>`}
    ${problems}${money_}
    <div class="tiny link" style="margin-top:8px">Почему нет сделок →</div></div>`;
}

const exName = (id) => ((S.ov.exchanges || []).find((e) => e.id === id) || {}).name || id;

async function whySheet() {
  Sheet.open("Автопилот", `<div class="card empty"><div class="spin dark" style="width:22px;height:22px;margin:0 auto 10px"></div>Загружаю…</div>`, { full: true });
  try {
    const r = await api("/api/autostatus");
    if (!Sheet.isOpen() || $("#sheetTitle").textContent !== "Автопилот") return;
    const a = r.status || {}, s = a.stats || {}, t = tr();
    const st = autoState(Object.assign({}, t, { auto: r.auto, auto_status: a }));
    const rows = [];
    for (const k of t.keys || []) {
      const why = (a.blocked_now || {})[k.exchange], warn = (a.key_problems || {})[k.exchange];
      const status = why ? "bad" : warn ? "warn" : "ok";
      rows.push(`<div class="item" style="align-items:flex-start"><div class="doc-ic ${status}">${DOC_ICON[status]}</div><div class="grow"><div class="t">${esc(k.name)}</div><div class="s" style="white-space:normal">${esc(why || (warn ? warn + " (в тесте не мешает)" : "может торговать"))}${a.free && a.free[k.exchange] != null ? ` · на торговом счёте ${amount(a.free[k.exchange])} ${esc(S.ov.quote)}` : ""}${a.funding && a.funding[k.exchange] ? ` · на Funding $${amount(a.funding[k.exchange])}` : ""}</div></div></div>`);
    }
    const tried = (a.tried || []).map((x) => `<div class="item" style="align-items:flex-start"><div class="doc-ic ${x.executed ? "ok" : "info"}">${DOC_ICON[x.executed ? "ok" : "info"]}</div><div class="grow"><div class="t">${esc(x.path)} <span class="muted small">${esc(exName(x.exchange))} · ${pct(x.estimate, 3)}</span></div><div class="s" style="white-space:normal">${esc(x.reason || "исполнен")}</div></div></div>`).join("");
    Sheet.body().innerHTML = `
      <div class="row between"><span class="pill ${r.live ? "live" : "test"}">${r.live ? "Реальный" : "Тест"}</span><span class="pill ${st.cls}">${st.icon} ${esc(st.text)}</span></div>
      ${a.reason && !a.executed ? `<div class="banner ${a.code === "no_candidates" || a.code === "moved" ? "info" : "red"}" style="margin-top:10px"><div><b>Почему нет сделки:</b> ${esc(a.reason)}</div></div>` : ""}
      ${a.hint ? `<div class="card small" style="margin-top:10px;white-space:normal">${esc(a.hint)}</div>` : ""}
      <div class="grid4" style="margin-top:10px">
        <div class="stat center"><div class="v num">${s.checks ?? 0}</div><div class="k">сканов</div></div>
        <div class="stat center"><div class="v num">${s.candidates ?? 0}</div><div class="k">подходящих</div></div>
        <div class="stat center"><div class="v num">${s.attempts ?? 0}</div><div class="k">проверено</div></div>
        <div class="stat center"><div class="v num up">${s.executed ?? 0}</div><div class="k">сделок</div></div>
      </div>
      ${a.best ? `<div class="small muted" style="margin-top:8px">Лучший цикл сейчас: <b class="${cls(a.best.net)}">${pct(a.best.net, 3)}</b> ${esc(a.best.path)}${a.best.exchange ? " · " + esc(exName(a.best.exchange)) : ""} (нужно ≥ ${a.need}%)</div>` : ""}
      <div class="section-title">Биржи</div><div class="list">${rows.join("") || `<div class="item">Ключей нет</div>`}</div>
      ${tried ? `<div class="section-title">Последняя проверка</div><div class="list">${tried}</div>` : ""}
      <div class="tiny muted" style="margin-top:12px;white-space:normal">Бот покупает, только когда цикл приносит ≥ ${a.need ?? "?"}% после комиссий по свежему стакану. Если таких нет, он ждёт: это защищает деньги. Сводка приходит в чат раз в ${S.ov.settings && S.ov.settings.auto_report ? S.ov.settings.auto_report.value : 6} ч.</div>
      <button class="btn block ghost" style="margin-top:14px" data-act="why">Обновить</button>`;
  } catch (e) { Sheet.body().innerHTML = `<div class="banner red">❌ ${esc(e.message)}</div>`; }
}

// ---------------------------------------------------------------------------
// Мемкоины: DexScreener + RugCheck, ссылки на GMGN и Axiom
// ---------------------------------------------------------------------------

const VERDICT = { green: ["ok", "🟢 явных флагов нет"], yellow: ["test", "🟡 осторожно"], red: ["bad", "🔴 высокий риск"] };

function tokenIcon(it) {
  if (!it.image || !/^https:\/\//.test(it.image)) return coinIcon(it.symbol);
  return `<div class="coin" style="background:${coinColor(it.symbol)};position:relative;overflow:hidden">${esc(String(it.symbol).slice(0, 4))}<img src="${esc(it.image)}" alt="" loading="lazy" style="position:absolute;inset:0;width:100%;height:100%;object-fit:cover" onerror="this.remove()"></div>`;
}

function age(h) { return h == null ? "?" : h < 1 ? `${Math.round(h * 60)} мин` : h < 48 ? `${Math.round(h)} ч` : `${Math.round(h / 24)} дн`; }

function tokenCard(it, full) {
  const v0 = VERDICT[it.verdict] || VERDICT.yellow;
  const v = [v0[0], (it.verdict_text || v0[1]).replace("явных красных флагов нет", "явных флагов нет")];
  const ch = it.change || {};
  const L = it.links || {};
  const btn = (k, label) => L[k] ? `<button class="btn sm ghost" data-act="open" data-url="${esc(L[k])}">${label}</button>` : "";
  return `<div class="card token" style="margin-top:10px">
    <div class="row" style="gap:10px">${tokenIcon(it)}<div class="grow" style="min-width:0"><div class="b">${esc(it.symbol)} <span class="muted small">${esc(it.chain_title)} · ${esc(it.dex)}</span></div><div class="small muted ellipsis">${esc(it.name)}</div></div>
      <div class="r" style="text-align:right"><div class="b num">${it.price != null ? price(it.price) : "—"}</div><div class="small num ${cls(ch.h24)}">${ch.h24 != null ? pct(ch.h24, 1) : ""}</div></div></div>
    <div class="grid4 tok-stats" style="margin-top:10px">
      <div><div class="k">Ликвидн.</div><div class="num">$${compact(it.liquidity)}</div></div>
      <div><div class="k">Объём 24ч</div><div class="num">$${compact(it.volume)}</div></div>
      <div><div class="k">Капитал.</div><div class="num">$${compact(it.mcap || it.fdv)}</div></div>
      <div><div class="k">Возраст</div><div class="num">${age(it.age_h)}</div></div>
    </div>
    <div class="row between" style="margin-top:10px"><span class="pill ${v[0]}">${v[1]}</span><span class="tiny muted">🟢 ${it.buys} / 🔴 ${it.sells} за 24ч</span></div>
    ${it.rug ? `<div class="small" style="margin-top:8px">RugCheck: риск <b>${it.rug.score}/100</b> (${esc(it.rug.label)})${it.rug.lp_locked != null ? ` · ликвидность заблокирована ${Math.round(it.rug.lp_locked)}%` : ""}</div>` : ""}
    ${full && (it.flags || []).length ? `<div class="col" style="gap:4px;margin-top:8px">${it.flags.slice(0, 5).map((f) => `<div class="tiny" style="color:var(--yellow)">⚠️ ${esc(f)}</div>`).join("")}</div>` : ""}
    ${full ? `<div class="tiny muted mono" style="margin-top:8px;word-break:break-all">${esc(it.address)}</div>` : ""}
    <div class="row" style="gap:6px;margin-top:10px;flex-wrap:wrap">${btn("gmgn", "GMGN")}${btn("axiom", "Axiom")}${btn("dexscreener", "DexScreener")}${it.chain === "solana" && !it.rug ? `<button class="btn sm ghost" data-act="dex-rug" data-v="${esc(it.address)}">🛡 RugCheck</button>` : ""}</div>
  </div>`;
}

function dexSheet(tab = "hot") {
  S.dexTab = tab;
  Sheet.open("Мемкоины", `
    <div class="seg"><button data-act="dex-tab" data-v="hot" class="${tab === "hot" ? "on" : ""}">🔥 Тренды</button><button data-act="dex-tab" data-v="search" class="${tab === "search" ? "on" : ""}">🔎 Поиск</button></div>
    ${tab === "search" ? `<div class="row" style="gap:8px;margin-top:10px"><input id="dexQ" class="input grow" placeholder="BONK или адрес токена" autocomplete="off" value="${esc(store.get("dexQ", ""))}"><button class="btn primary" data-act="dex-go">Найти</button></div>
    <div class="chips" style="margin-top:10px">${["BONK", "WIF", "POPCAT", "TRUMP", "PENGU"].map((x) => `<button class="chip" data-act="dex-q" data-v="${x}">${x}</button>`).join("")}</div>` :
    `<div class="row between card" style="margin-top:10px"><div><div class="b">📡 Радар в чат</div><div class="tiny muted">Новые токены Solana: ликвидность ≥ $30K, объём ≥ $100K, RugCheck ≤ 30</div></div><label class="switch"><input type="checkbox" data-act="radar" ${S.radar ? "checked" : ""}><span></span></label></div>`}
    <div id="dexBox"><div class="card empty"><div class="spin dark" style="width:22px;height:22px;margin:0 auto 10px"></div>Загружаю…</div></div>
    <div class="tiny muted" style="margin-top:12px;white-space:normal">Бот мемкоины не покупает: у GMGN, Axiom и FOMO нет официального API, а для торговли с кошелька нужен приватный ключ. Открывай токен в GMGN/Axiom кнопкой. Большинство мемкоинов падает почти до нуля.</div>`, { full: true });
  if (tab === "hot") loadDexHot(); else if (store.get("dexQ", "")) loadDexSearch(); else $("#dexBox").innerHTML = "";
}

async function loadDexHot() {
  try {
    const r = await api("/api/dex/hot");
    S.radar = r.radar;
    const box = $("#dexBox"); if (!box) return;
    const sw = $('[data-act="radar"]'); if (sw) sw.checked = !!r.radar;
    box.innerHTML = r.items.length ? r.items.map((it) => tokenCard(it, false)).join("") : `<div class="card empty">Сейчас трендов нет</div>`;
  } catch (e) { const box = $("#dexBox"); if (box) box.innerHTML = `<div class="banner red">❌ ${esc(e.message)}</div>`; }
}

async function loadDexSearch(btn) {
  const q = (($("#dexQ") || {}).value || "").trim();
  if (!q) { toast("Введи название или адрес", true); return; }
  store.set("dexQ", q);
  if (btn) busy(btn, true);
  $("#dexBox").innerHTML = `<div class="card empty"><div class="spin dark" style="width:22px;height:22px;margin:0 auto 10px"></div>Ищу «${esc(q)}»…</div>`;
  try {
    const r = await api("/api/dex/search?q=" + encodeURIComponent(q));
    const box = $("#dexBox"); if (!box) return;
    box.innerHTML = r.items.length ? r.items.map((it, i) => (i === 1 ? `<div class="tiny muted" style="margin-top:12px">Ещё с таким названием. Проверяй адрес: скамеры копируют тикеры</div>` : "") + tokenCard(it, i === 0)).join("") : `<div class="card empty">Ничего не нашёл</div>`;
  } catch (e) { $("#dexBox").innerHTML = `<div class="banner red">❌ ${esc(e.message)}</div>`; } finally { if (btn) busy(btn, false); }
}

async function dexRug(mint, btn) {
  busy(btn, true);
  try {
    const r = await api("/api/dex/rug?mint=" + encodeURIComponent(mint));
    const card = btn.closest(".token");
    if (card && r.item) card.outerHTML = tokenCard(r.item, true);
  } catch (e) { fail(e); busy(btn, false); }
}

async function toggleRadar(input) {
  try { const r = await api("/api/radar", { on: input.checked }); S.radar = r.radar; toast(r.radar ? "📡 Радар включён: новые токены придут в чат" : "Радар выключен"); } catch (e) { input.checked = !input.checked; fail(e); }
}

function openLink(url) {
  try { tg.openLink(url); } catch { window.open(url, "_blank", "noopener"); }
}

// ---------------------------------------------------------------------------
// Т-Инвестиции (только просмотр)
// ---------------------------------------------------------------------------

const RUB = (v, cur = "rub") => `${groupInt(Math.round(v).toString())}${NB}${{ rub: "₽", usd: "$", eur: "€", cny: "¥" }[cur] || cur.toUpperCase()}`;
const TI_TYPES = { share: "Акции", bond: "Облигации", etf: "Фонды", currency: "Валюта", futures: "Фьючерсы", option: "Опционы", sp: "Ноты" };

async function tinvestSheet(force = false) {
  Sheet.open("Т-Инвестиции", `<div class="card empty"><div class="spin dark" style="width:22px;height:22px;margin:0 auto 10px"></div>Загружаю портфель…</div>`, { full: true });
  try {
    const r = await api("/api/tinvest" + (force ? "?force=1" : ""));
    if (!Sheet.isOpen() || $("#sheetTitle").textContent !== "Т-Инвестиции") return;
    if (!r.configured) {
      Sheet.body().innerHTML = `<div class="card"><div class="b">Подключить портфель (только просмотр)</div><ol class="small" style="padding-left:18px;margin:8px 0 0;line-height:1.55">
        <li>Т-Банк → Инвестиции → ⚙️ Настройки → «Токены T-Invest API»</li>
        <li>«Создать токен» → доступ <b>«Только чтение»</b> → все счета → скопировать</li>
        <li>Render → сервис → Environment → <b>TINVEST_TOKEN</b> = токен → Save</li>
        <li>Через минуту открой этот экран снова</li></ol>
        <div class="tiny muted" style="margin-top:10px">🔒 Токен не присылай в чат. «Только чтение» не даёт выставлять заявки и выводить деньги.</div></div>`;
      return;
    }
    if (r.error) { Sheet.body().innerHTML = `<div class="banner red"><div>❌ ${esc(r.error)}</div></div>`; return; }
    Sheet.body().innerHTML = `
      ${r.accounts.length > 1 ? `<div class="stat"><div class="k">Всего</div><div class="v num">${RUB(r.total)}</div></div>` : ""}
      ${r.accounts.map((a) => a.error ? `<div class="banner red" style="margin-top:10px"><div><b>${esc(a.name)}</b>: ${esc(a.error)}</div></div>` : `
        <div class="card" style="margin-top:10px">
          <div class="row between"><div><div class="b">${esc(a.name)}</div><div class="tiny muted">${esc(a.type || "")}</div></div>
          <div style="text-align:right"><div class="b num">${RUB(a.total, a.currency)}</div><div class="small num ${cls(a.yield_pct)}">${pct(a.yield_pct)} всего${a.daily_yield != null ? ` · <span class="${cls(a.daily_yield)}">${a.daily_yield > 0 ? "+" : ""}${RUB(a.daily_yield, a.currency)} день</span>` : ""}</div></div></div>
          ${Object.keys(a.by_type || {}).length ? `<div class="chips" style="margin-top:10px;flex-wrap:wrap">${Object.entries(a.by_type).map(([k, v]) => `<span class="chip">${esc(TI_TYPES[k] || k)} ${RUB(v, a.currency)}</span>`).join("")}</div>` : ""}
        </div>
        <div class="list" style="margin-top:8px">${a.positions.slice(0, 30).map((p) => `<div class="item">${coinIcon(p.ticker)}<div class="grow"><div class="t">${esc(p.ticker)} <span class="muted small">${esc(TI_TYPES[p.type] || p.type)}</span></div><div class="s num">${amount(p.quantity)} шт × ${price(p.price)}</div></div><div class="r"><div class="b num">${RUB(p.value, p.currency)}</div><div class="s num ${cls(p.yield)}">${p.yield ? (p.yield > 0 ? "+" : "") + RUB(p.yield, p.currency) + (p.yield_pct != null ? ` (${pct(p.yield_pct, 1)})` : "") : ""}</div></div></div>`).join("")}</div>`).join("")}
      <button class="btn block ghost" style="margin-top:14px" data-act="tinvest-refresh">Обновить</button>
      <div class="tiny muted center" style="margin-top:8px">Только просмотр. Заявки бот в Т-Инвестициях не выставляет.</div>`;
  } catch (e) { Sheet.body().innerHTML = `<div class="banner red"><div>❌ ${esc(e.message)}</div></div>`; }
}

const RENDER = { home: renderHome, market: renderMarket, signals: renderSignals, demo: renderDemo, wallet: renderWallet };
let lastSig = {};
function renderActive(force = false) {
  const sig = JSON.stringify([S.ov && S.ov.trading, S.ov && S.ov.scan && S.ov.scan.finished_at, S.ov && S.ov.demo, S.portfolio && S.portfolio.updated_at, S.ov && S.ov.settings, S.scanning]);
  if (!force && lastSig[S.tab] === sig) return;
  if (!force && document.querySelector(".tip:not(.hidden)")) return; // не мешаем, пока палец на графике
  if (!force && S.tab === "market") {        // рынок обновляется своим таймером
    if (!$("#candles")) { lastSig.market = sig; renderMarket(); loadMarket(true); }
    return;
  }
  const active = document.activeElement;
  if (!force && active && active.tagName === "INPUT" && $("#v-" + S.tab).contains(active)) return; // не сбиваем ввод
  lastSig[S.tab] = sig;
  RENDER[S.tab]();
}

function go(tab) {
  if (!RENDER[tab]) tab = "home";
  S.tab = tab; store.set("tab", tab);
  $$("nav.tabbar button").forEach((b) => b.classList.toggle("on", b.dataset.tab === tab));
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === "v-" + tab));
  window.scrollTo({ top: 0 });
  renderActive(true);
  if (tab === "market") loadMarket(true);
  if (tab === "demo") loadDemo();
  if (tab === "wallet") { loadPortfolio(); loadOrders(); loadPhantom(); }
}

let refreshing = false;
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    const ov = await api("/api/overview");
    S.ov = ov; store.set("ov", ov);
    renderHeader(); renderActive();
  } catch (e) {
    if (!S.ov) $("#v-home").innerHTML = `<div class="card empty"><div class="e">⚠️</div>${esc(e.message)}<div style="margin-top:12px"><button class="btn primary sm" data-act="retry">Повторить</button></div></div>`;
  } finally { refreshing = false; }
}

document.addEventListener("click", async (e) => {
  const tabBtn = e.target.closest("nav.tabbar button");
  if (tabBtn) { haptic.sel(); go(tabBtn.dataset.tab); return; }
  const el = e.target.closest("[data-act]");
  if (!el || el.tagName === "INPUT") return;
  const act = el.dataset.act, d = el.dataset;
  switch (act) {
    case "close-sheet": Sheet.close(); break;
    case "settings": haptic.tap(); if (S.ov) settingsSheet(); break;
    case "ai": haptic.tap(); if (S.ov) aiSheet(); break;
    case "ai-q": askAI(d.v); break;
    case "ai-set": {
      const p = S.chat[+d.i].proposals[+d.j];
      try { const r = await api("/api/set", { key: p.key, value: p.value }); S.ov.settings = r.settings; el.disabled = true; el.innerHTML = "✅ Применено"; toast("Настройка применена"); } catch (er) { fail(er); }
      break;
    }
    case "goto": haptic.sel(); if (d.sig) { S.sigTab = d.sig; store.set("sigTab", d.sig); } go(d.tab); break;
    case "home-mode": S.homeMode = d.v; store.set("homeMode", d.v); haptic.sel(); renderHome(); if (d.v === "real") loadPortfolio(); break;
    case "scan": doScan(el); break;
    case "stop": haptic.tap(); panic(); break;
    case "keys": Sheet.open("Подключение биржи", keysHelp()); break;
    case "doctor": haptic.tap(); doctorSheet(); break;
    case "alerts": haptic.tap(); alertsSheet(); break;
    case "alert-add": addAlert(el); break;
    case "alert-del": delAlert(d.id); break;
    case "alert-ex": $("#alertText").value = d.v; $("#alertText").focus(); break;
    case "prices": haptic.tap(); pricesSheet(); break;
    case "price-go": loadPrices(el); break;
    case "price-coin": $("#priceCoin").value = d.v; loadPrices(); break;
    case "phantom": haptic.tap(); phantomSheet(); loadPhantom(); break;
    case "why": haptic.tap(); whySheet(); break;
    case "dex": haptic.tap(); dexSheet(d.v || "hot"); break;
    case "dex-tab": haptic.sel(); dexSheet(d.v); break;
    case "dex-go": loadDexSearch(el); break;
    case "dex-q": $("#dexQ").value = d.v; loadDexSearch(); break;
    case "dex-rug": dexRug(d.v, el); break;
    case "open": haptic.tap(); openLink(d.url); break;
    case "tinvest": haptic.tap(); tinvestSheet(); break;
    case "tinvest-refresh": haptic.tap(); tinvestSheet(true); break;
    case "phantom-save": savePhantom(el); break;
    case "phantom-off": savePhantom(el, true); break;
    case "phantom-refresh": haptic.tap(); loadPhantom(true); break;
    case "quick": S.m.side = d.side; store.set("m", S.m); go("market"); break;
    case "m-ex": S.m.ex = d.v; S.market = null; store.set("m", S.m); haptic.sel(); renderMarket(); loadMarket(true); break;
    case "m-tf": S.m.tf = d.v; store.set("m", S.m); haptic.sel(); $$('[data-act="m-tf"]').forEach((c) => c.classList.toggle("on", c.dataset.v === d.v)); loadMarket(true); break;
    case "m-side": S.m.side = d.v; S.m.amount = ""; store.set("m", S.m); haptic.sel(); renderMarket(); break;
    case "m-type": S.m.type = d.v; store.set("m", S.m); haptic.sel(); renderMarket(); break;
    case "m-amt": S.m.amount = d.v; store.set("m", S.m); haptic.sel(); $("#mAmount").value = d.v; $$('[data-act="m-amt"]').forEach((c) => c.classList.toggle("on", c.dataset.v === d.v)); break;
    case "m-preview": {
      const amt = ($("#mAmount").value || "").trim();
      if (!amt) { toast("Укажи сумму", true); return; }
      busy(el, true);
      try {
        const body = { exchange: marketEx(), side: S.m.side, coin: S.m.symbol.split("/")[0], amount: amt, type: S.m.type };
        if (S.m.type === "limit") body.price = ($("#mPriceIn").value || "").trim();
        const r = await api("/api/plan", body);
        haptic.tap(); showPlan(r.plan);
      } catch (er) { fail(er); } finally { busy(el, false); }
      break;
    }
    case "pick-symbol": haptic.tap(); pickSymbol(); break;
    case "set-symbol": S.m.symbol = d.v; S.market = null; store.set("m", S.m); Sheet.close(); haptic.sel(); renderMarket(); loadMarket(true); break;
    case "plan-ok": executePlan(d.id, d.live === "1", el); break;
    case "plan-no": api("/api/cancel_plan", { id: d.id }).catch(() => {}); if ($("#chatBox")) { el.closest(".row").innerHTML = '<div class="small muted">Отменено</div>'; } else Sheet.close(); break;
    case "tri": runTriangle(d.key, el); break;
    case "sig-tab": S.sigTab = d.v; store.set("sigTab", d.v); haptic.sel(); renderSignals(); break;
    case "demo-start": demoStartSheet(); break;
    case "demo-amt": {
      if (S.demoSel.has(d.v)) S.demoSel.delete(d.v); else if (S.demoSel.size < 5) S.demoSel.add(d.v); else toast("Не больше 5 сумм", true);
      haptic.sel(); S.redrawDemoChips(); break;
    }
    case "demo-go": {
      if (!S.demoSel.size) { toast("Выбери хотя бы одну сумму", true); return; }
      busy(el, true);
      try { const r = await api("/api/demo/start", { amounts: [...S.demoSel].map(Number) }); S.ov.demo = r; S.demo = null; Sheet.close(); toast("🧪 Демо запущено"); renderDemo(); loadDemo(); } catch (er) { fail(er); } finally { busy(el, false); }
      break;
    }
    case "demo-stop": {
      if (!(await confirmBox("Остановить демо-счета? Результаты сохранятся."))) return;
      try { const r = await api("/api/demo/stop", {}); S.ov.demo = r; S.demo = null; toast("Демо остановлено"); renderDemo(); loadDemo(); } catch (er) { fail(er); }
      break;
    }
    case "refresh-portfolio": haptic.tap(); el.innerHTML = '<div class="spin dark" style="width:14px;height:14px"></div>'; await loadPortfolio(); break;
    case "load-orders": S.orders = null; $("#ordersBox").innerHTML = ordersHtml(); loadOrders(); break;
    case "cancel-order": {
      if (!(await confirmBox(`Отменить ордер ${d.symbol}?`))) return;
      busy(el, true);
      try { await api("/api/cancel", { exchange: d.ex, id: d.id, symbol: d.symbol }); toast("Ордер отменён"); loadOrders(); loadPortfolio(); } catch (er) { fail(er); busy(el, false); }
      break;
    }
    case "coin": {
      if (d.coin === S.ov.quote) return;
      S.m = Object.assign(S.m, { ex: d.ex, symbol: `${d.coin}/${S.ov.quote}`, side: "sell", amount: "all", type: "market" });
      S.market = null; store.set("m", S.m); go("market"); break;
    }
    case "retry": refresh(); break;
    default: break;
  }
});

document.addEventListener("change", (e) => {
  const el = e.target;
  if (el.dataset.act === "auto") toggleAuto(el);
  else if (el.dataset.act === "radar") toggleRadar(el);
  else if (el.dataset.act === "live") toggleLive(el);
  else if (el.dataset.set) saveSetting(el);
});
document.addEventListener("input", (e) => {
  if (e.target.id === "mAmount") { S.m.amount = e.target.value; store.set("m", S.m); $$('[data-act="m-amt"]').forEach((c) => c.classList.toggle("on", c.dataset.v === S.m.amount)); }
  if (e.target.id === "mPriceIn") { S.m.price = e.target.value; store.set("m", S.m); }
});
$("#backdrop").addEventListener("click", () => Sheet.close());

// ---------------------------------------------------------------------------
// Старт
// ---------------------------------------------------------------------------

if (tg) {
  try {
    tg.ready(); tg.expand();
    if (tg.disableVerticalSwipes) tg.disableVerticalSwipes(); // свайп по графику не закрывает приложение
    tg.onEvent("themeChanged", applyTheme);
    tg.BackButton.onClick(() => Sheet.close());
  } catch { /* */ }
}
applyTheme();
renderHeader();
go(S.tab);                         // мгновенно рисуем по кэшу
refresh().then(() => { if (S.tab === "home" && tr().keys && tr().keys.length) loadPortfolio(); });

setInterval(() => { if (document.visibilityState === "visible") refresh(); }, 7000);
setInterval(() => { if (document.visibilityState === "visible" && S.tab === "market") loadMarket(false); }, 5000);
setInterval(() => { if (document.visibilityState === "visible") renderHeader(); }, 15000);
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") refresh(); });
})();
