"use strict";
// P21 Studio editor. The preview image always comes from the server-side renderer
// (the same code that prints), and this script only draws edit handles on top of it.

const $ = (sel) => document.querySelector(sel);
const HEAD = 96;
const DEFAULTS = {
  common: { x: 0, y: 0, w: 50, h: 30, rotate: 0, color: "black" },
  text: { text: "", font: "auto", size: "auto", min_size: 8, max_size: 200, bold: false, align: "center", valign: "middle", line_spacing: 1.15, letter_spacing: 0, wrap: true, vertical: false, stroke: 0 },
  qr: { data: "", ecc: "M", border: 0 },
  barcode: { data: "", symbology: "code128", show_text: true, text_size: 14 },
  image: { src: "", fit: "contain", dither: "floyd", threshold: 128, invert: false, brightness: 1, contrast: 1 },
  rect: { fill: false, stroke: 2, radius: 0 },
  ellipse: { fill: false, stroke: 2 },
  line: { x1: 0, y1: 0, x2: 50, y2: 0, width: 2 },
};
const TYPE_NAMES = { text: "Text", qr: "QR", barcode: "Barcode", image: "Image", rect: "Rectangle", ellipse: "Ellipse", line: "Line" };
const SYMBOLOGIES = ["code128", "code39", "ean13", "ean8", "jan", "upca", "itf", "codabar", "isbn13", "gs1_128"];

const S = {
  label: null,
  sel: -1,
  zoom: 0, // 0 = fit
  values: {},
  csv: null,
  csvRow: 0,
  history: [],
  future: [],
  lastCheckpoint: 0,
  render: { inflight: false, dirty: false, boxes: [], variables: [] },
  meta: {},
  device: null,
  printing: false,
};

// ---------------------------------------------------------------- utilities
const clone = (o) => JSON.parse(JSON.stringify(o));
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

async function api(path, body) {
  const opt = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const r = await fetch(path, opt);
  let j;
  try { j = await r.json(); } catch { j = { ok: false, message: r.statusText }; }
  if (!r.ok || j.ok === false) throw new Error(j.message || r.statusText);
  return j;
}

function toast(msg, kind = "", ms = 4500, actions = []) {
  const t = document.createElement("div");
  t.className = "toast " + kind;
  t.textContent = msg;
  for (const [label, fn] of actions) {
    const b = document.createElement("button");
    b.className = "btn small";
    b.style.marginLeft = "10px";
    b.textContent = label;
    b.onclick = () => { fn(); t.remove(); };
    t.appendChild(b);
  }
  $("#toasts").appendChild(t);
  setTimeout(() => t.remove(), ms);
}

function lengthDots(mm) { return Math.abs(mm - 40) < 1e-6 ? 284 : Math.max(8, Math.round(mm * 7.1)); }
function canvasSize(label = S.label) {
  const len = label.media.length_dots || lengthDots(label.media.length_mm);
  return label.orientation === "portrait" ? [HEAD, len] : [len, HEAD];
}

function rescaleTo(oldW, oldH) {
  const [W, H] = canvasSize();
  if ((oldW === W && oldH === H) || !S.label.elements.length) return;
  const kx = W / oldW, ky = H / oldH;
  for (const el of S.label.elements) {
    if (el.type === "line") {
      el.x1 = Math.round(el.x1 * kx); el.x2 = Math.round(el.x2 * kx);
      el.y1 = Math.round(el.y1 * ky); el.y2 = Math.round(el.y2 * ky);
    } else {
      el.x = Math.round((el.x ?? 0) * kx); el.y = Math.round((el.y ?? 0) * ky);
      el.w = Math.max(4, Math.round((el.w ?? 50) * kx)); el.h = Math.max(4, Math.round((el.h ?? 30) * ky));
      if (el.type === "qr") { const s = Math.min(el.w, el.h); el.x += Math.round((el.w - s) / 2); el.y += Math.round((el.h - s) / 2); el.w = el.h = s; }
    }
  }
  toast("Element positions and sizes adjusted to the new size (Undo to revert)", "", 5000);
}

function newLabel() {
  return { version: 1, name: "", media: { width_mm: 14, length_mm: 40 }, orientation: "landscape", invert: false, density: 15, copies: 1, variables: {}, elements: [] };
}

function fill(el) {
  return { ...DEFAULTS.common, ...DEFAULTS[el.type], ...el };
}

function lineBox(el) {
  const x = Math.min(el.x1, el.x2), y = Math.min(el.y1, el.y2);
  return { x, y, w: Math.abs(el.x2 - el.x1), h: Math.abs(el.y2 - el.y1) };
}

// ---------------------------------------------------------------- history
function checkpoint(force = true) {
  const now = Date.now();
  if (!force && now - S.lastCheckpoint < 900) return;
  S.lastCheckpoint = now;
  S.history.push(JSON.stringify(S.label));
  if (S.history.length > 150) S.history.shift();
  S.future = [];
  updateUndo();
}
function undo() {
  if (!S.history.length) return;
  S.future.push(JSON.stringify(S.label));
  S.label = JSON.parse(S.history.pop());
  S.sel = Math.min(S.sel, S.label.elements.length - 1);
  changed(true);
}
function redo() {
  if (!S.future.length) return;
  S.history.push(JSON.stringify(S.label));
  S.label = JSON.parse(S.future.pop());
  S.sel = Math.min(S.sel, S.label.elements.length - 1);
  changed(true);
}
function updateUndo() {
  $("#undo").disabled = !S.history.length;
  $("#redo").disabled = !S.future.length;
}

let saveTimer = 0;
function saveLocal() {
  try { localStorage.setItem("p21studio.label", JSON.stringify(S.label)); } catch {}
}
window.addEventListener("beforeunload", saveLocal);
function changed(rebuildProps = false) {
  updateUndo();
  syncTop();
  renderOverlay();
  renderLayers();
  if (rebuildProps) renderProps();
  else syncBoxInputs();
  requestRender();
  clearTimeout(saveTimer);
  saveTimer = setTimeout(saveLocal, 400);
}

// ---------------------------------------------------------------- rendering
function previewValues() {
  const v = { ...S.values };
  if (S.csv && S.csv[S.csvRow]) Object.assign(v, S.csv[S.csvRow]);
  return v;
}

async function requestRender() {
  if (S.render.inflight) { S.render.dirty = true; return; }
  S.render.inflight = true;
  try {
    const r = await api("/api/render", { label: S.label, values: previewValues() });
    $("#preview").src = r.png;
    S.render.boxes = r.boxes;
    S.render.w = r.width; S.render.h = r.height;
    $("#warnings").innerHTML = r.warnings.map((w) => (r.errors.includes(w) ? "⛔ " : "⚠ ") + esc(w)).join("<br>") + (r.errors.length ? "<br><b>Elements marked ⛔ cannot be rendered, so this cannot be printed as is.</b>" : "");
    if (JSON.stringify(r.variables) !== JSON.stringify(S.render.variables)) {
      S.render.variables = r.variables;
      renderVars();
    }
    if (S.inline) S.inline.layout();
    const sel = r.boxes[S.sel];
    const auto = $("#auto-size-note");
    if (auto) auto.textContent = sel && sel.font_size ? `Current ${sel.font_size}px` : "";
  } catch (e) {
    $("#preview").src = "data:image/gif;base64,R0lGODlhAQABAAAAACw=";
    $("#warnings").innerHTML = "⚠ Cannot render: " + esc(e.message).replace(/\n/g, "<br>");
  } finally {
    S.render.inflight = false;
    if (S.render.dirty) { S.render.dirty = false; requestRender(); }
  }
}

// ---------------------------------------------------------------- stage / overlay
function zoom() {
  const [W, H] = canvasSize();
  if (S.zoom) return S.zoom;
  const wrap = $("#stage-wrap");
  const fitW = (wrap.clientWidth - 60) / W;
  const fitH = (Math.max(window.innerHeight * 0.55, 240)) / H;
  return clamp(Math.floor(Math.min(fitW, fitH) * 2) / 2, 1, 8);
}

// ---------------------------------------------------------------- inline text editing
// Double-click a text element to type directly on the label. The textarea mimics the
// rendered text (size from the auto-fit result, alignment, weight, vertical writing).
function startInlineEdit(i, selectAll = false) {
  const el = S.label.elements[i];
  if (!el || el.type !== "text") return;
  if (S.inline) S.inline.finish(true);
  if (S.sel !== i) select(i);
  checkpoint();
  const original = el.text ?? "";
  const ta = document.createElement("textarea");
  ta.className = "inline-edit";
  ta.value = original;
  ta.spellcheck = false;
  $("#stage").appendChild(ta);

  const layout = () => {
    const e = fill(el), z = zoom();
    const box = S.render.boxes[i];
    const size = (e.size === "auto" || e.size == null ? box?.font_size || 24 : e.size) * z;
    const fam = e.font && e.font !== "auto" && !/[\\/]/.test(e.font) ? `"${e.font}", ` : "";
    const pref = (S.meta.fonts_preferred || []).map((f) => `"${f}"`).join(", ");
    const rot = e.rotate || 0;
    const sideways = rot === 90 || rot === 270;
    const bw = (sideways ? e.h : e.w) * z, bh = (sideways ? e.w : e.h) * z;
    Object.assign(ta.style, {
      left: e.x * z + (e.w * z - bw) / 2 + "px",
      top: e.y * z + (e.h * z - bh) / 2 + "px",
      width: bw + "px",
      height: bh + "px",
      transform: rot ? `rotate(${rot}deg)` : "",
      fontSize: size + "px",
      lineHeight: String(e.line_spacing || 1.15),
      letterSpacing: (e.letter_spacing || 0) * z + "px",
      fontWeight: e.bold ? "700" : "400",
      fontFamily: `${fam}${pref ? pref + ", " : ""}sans-serif`,
      textAlign: e.vertical ? ({ top: "start", middle: "center", bottom: "end" }[e.valign || "middle"]) : (e.align || "center"),
      writingMode: e.vertical ? "vertical-rl" : "horizontal-tb",
      whiteSpace: e.wrap === false ? "pre" : "pre-wrap",
      color: e.color === "white" ? "#fff" : "#000",
      background: e.color === "white" ? "#000" : "#fff",
      padding: "0",
    });
    // textareas cannot centre their content: measure it and pad the start side instead
    if (e.vertical) {
      ta.style.width = "0px";
      const contentW = ta.scrollWidth;
      ta.style.width = bw + "px";
      const free = Math.max(0, bw - contentW);
      const al = e.align || "center"; // vertical columns flow from the right
      ta.style.paddingRight = (al === "right" ? 0 : al === "center" ? free / 2 : free) + "px";
    } else {
      ta.style.height = "0px";
      const contentH = ta.scrollHeight;
      ta.style.height = bh + "px";
      const free = Math.max(0, bh - contentH);
      const v = e.valign || "middle";
      ta.style.paddingTop = (v === "top" ? 0 : v === "middle" ? free / 2 : free) + "px";
    }
  };

  const finish = (commit) => {
    if (S.inline !== state) return;
    S.inline = null;
    ta.remove();
    if (!commit) {
      el.text = original;
      S.history.pop(); // nothing changed: drop the checkpoint taken when editing started
    } else if (el.text === original) {
      S.history.pop();
    }
    changed(true);
  };

  ta.addEventListener("input", () => { el.text = ta.value; requestRender(); renderLayers(); const t = $("#prop-text"); if (t && t !== document.activeElement) t.value = ta.value; layout(); });
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); finish(false); }
    else if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); finish(true); }
    else if (e.key === "Tab") { e.preventDefault(); finish(true); }
    e.stopPropagation();
  });
  ta.addEventListener("blur", () => finish(true));
  ta.addEventListener("pointerdown", (e) => e.stopPropagation());

  const state = { i, layout, finish };
  S.inline = state;
  layout();
  ta.focus();
  if (selectAll) ta.select(); else ta.setSelectionRange(ta.value.length, ta.value.length);
}

function renderOverlay() {
  const [W, H] = canvasSize();
  const z = zoom();
  const stage = $("#stage");
  if (S.inline) S.inline.layout();
  stage.style.width = W * z + "px";
  stage.style.height = H * z + "px";
  stage.classList.toggle("inverted", !!S.label.invert);
  $("#stage-info").textContent = `${S.label.media.width_mm}×${S.label.media.length_mm} mm · Print area ${W}×${H} dots · ${z}x`;
  const ov = $("#overlay");
  ov.classList.toggle("hide-boxes", !$("#show-boxes").checked);
  ov.innerHTML = "";
  S.label.elements.forEach((raw, i) => {
    const el = fill(raw);
    const b = el.type === "line" ? lineBox(el) : el;
    const pad = el.type === "line" ? 4 : 0;
    const d = document.createElement("div");
    d.className = "box" + (i === S.sel ? " sel" : "");
    d.dataset.i = i;
    d.style.left = (b.x * z - pad) + "px";
    d.style.top = (b.y * z - pad) + "px";
    d.style.width = Math.max(b.w * z + 2 * pad, 6) + "px";
    d.style.height = Math.max(b.h * z + 2 * pad, 6) + "px";
    if (i === S.sel) {
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = `${TYPE_NAMES[el.type]}  ${b.x},${b.y}  ${b.w}×${b.h}`;
      d.appendChild(tag);
      const handles = el.type === "line" ? ["p1", "p2"] : ["nw", "n", "ne", "e", "se", "s", "sw", "w"];
      for (const h of handles) {
        const hd = document.createElement("span");
        hd.className = "h " + h;
        hd.dataset.h = h;
        if (h === "p1" || h === "p2") {
          const px = (h === "p1" ? el.x1 : el.x2) - b.x, py = (h === "p1" ? el.y1 : el.y2) - b.y;
          hd.style.left = (px * z + pad - 6) + "px";
          hd.style.top = (py * z + pad - 6) + "px";
        }
        d.appendChild(hd);
      }
    }
    ov.appendChild(d);
  });
}

let drag = null;
function onPointerDown(e) {
  if (document.activeElement && document.activeElement.closest && document.activeElement.closest("input, textarea, select")) document.activeElement.blur();
  const box = e.target.closest(".box");
  if (!box) { if (e.target.closest("#stage")) select(-1); return; }
  const i = +box.dataset.i;
  if (i !== S.sel) select(i);
  const el = S.label.elements[i];
  drag = { i, mode: e.target.dataset.h ? "resize" : "move", h: e.target.dataset.h, sx: e.clientX, sy: e.clientY, orig: fill(clone(el)), before: JSON.stringify(S.label), moved: false };
  e.preventDefault();
  $("#stage").setPointerCapture(e.pointerId);
}

function snap(value, size, targets, thr) {
  // snap the start, centre or end of [value, value+size] to a target line
  let best = null;
  for (const t of targets) {
    for (const [off, kind] of [[0, "s"], [size / 2, "c"], [size, "e"]]) {
      const d = t - (value + off);
      if (Math.abs(d) <= thr && (!best || Math.abs(d) < Math.abs(best.d))) best = { d, line: t };
    }
  }
  return best;
}

function onPointerMove(e) {
  if (!drag) return;
  const z = zoom();
  const dx = Math.round((e.clientX - drag.sx) / z), dy = Math.round((e.clientY - drag.sy) / z);
  if (!drag.moved && Math.abs(e.clientX - drag.sx) + Math.abs(e.clientY - drag.sy) < 3) return;
  drag.moved = true;
  const [W, H] = canvasSize();
  const o = drag.orig;
  const el = S.label.elements[drag.i];
  const gv = $("#guide-v"), gh = $("#guide-h");
  gv.style.display = gh.style.display = "none";
  const thr = Math.max(2, Math.round(6 / z));

  if (o.type === "line") {
    if (drag.mode === "move") {
      Object.assign(el, { x1: o.x1 + dx, y1: o.y1 + dy, x2: o.x2 + dx, y2: o.y2 + dy });
    } else {
      const k = drag.h === "p1" ? ["x1", "y1"] : ["x2", "y2"];
      let nx = o[k[0]] + dx, ny = o[k[1]] + dy;
      if (e.shiftKey) { // keep horizontal/vertical
        const ox = drag.h === "p1" ? o.x2 : o.x1, oy = drag.h === "p1" ? o.y2 : o.y1;
        if (Math.abs(nx - ox) > Math.abs(ny - oy)) ny = oy; else nx = ox;
      }
      el[k[0]] = nx; el[k[1]] = ny;
    }
  } else if (drag.mode === "move") {
    let nx = o.x + dx, ny = o.y + dy;
    if (!e.altKey) {
      const sx = snap(nx, o.w, [0, W / 2, W], thr), sy = snap(ny, o.h, [0, H / 2, H], thr);
      if (sx) { nx += sx.d; gv.style.left = sx.line * z + "px"; gv.style.display = "block"; }
      if (sy) { ny += sy.d; gh.style.top = sy.line * z + "px"; gh.style.display = "block"; }
    }
    el.x = Math.round(nx); el.y = Math.round(ny);
  } else {
    let { x, y, w, h } = o;
    const hd = drag.h;
    if (hd.includes("e")) w = o.w + dx;
    if (hd.includes("s")) h = o.h + dy;
    if (hd.includes("w")) w = o.w - dx;
    if (hd.includes("n")) h = o.h - dy;
    w = Math.max(4, w); h = Math.max(4, h);
    if (hd.includes("w")) x = o.x + o.w - w;
    if (hd.includes("n")) y = o.y + o.h - h;
    const keepRatio = e.shiftKey || o.type === "qr";
    if (keepRatio && hd.length === 2) {
      const r = o.w / o.h;
      if (Math.abs(w - o.w) > Math.abs(h - o.h) * r) h = Math.round(w / r); else w = Math.round(h * r);
      if (hd.includes("w")) x = o.x + o.w - w;
      if (hd.includes("n")) y = o.y + o.h - h;
    }
    w = Math.max(4, w); h = Math.max(4, h);
    Object.assign(el, { x, y, w, h });
  }
  renderOverlay();
  syncBoxInputs();
  requestRender();
}

function onPointerUp() {
  if (!drag) return;
  $("#guide-v").style.display = $("#guide-h").style.display = "none";
  if (drag.moved) {
    S.history.push(drag.before);
    S.future = [];
    changed();
  }
  drag = null;
}

function select(i) {
  S.sel = i;
  renderOverlay();
  renderLayers();
  renderProps();
}

// ---------------------------------------------------------------- elements
function addElement(type, extra = {}) {
  const [W, H] = canvasSize();
  const m = 6;
  let el;
  switch (type) {
    case "text": {
      const first = !S.label.elements.some((e) => e.type === "text");
      el = first ? { type, x: m, y: m, w: W - 2 * m, h: H - 2 * m, text: "Text" } : { type, x: Math.round(W / 4), y: Math.round(H / 3), w: Math.round(W / 2), h: Math.round(H / 3), text: "Text" };
      break;
    }
    case "date":
      el = { type: "text", x: Math.round(W / 4), y: Math.round(H / 3), w: Math.round(W / 2), h: Math.round(H / 3), text: "{{date:%Y/%m/%d}}" };
      break;
    case "qr": { const s = Math.min(W, H) - 2 * m; el = { type, x: m, y: m, w: s, h: s, data: "https://example.com" }; break; }
    case "barcode": el = { type, x: m, y: m, w: Math.min(W - 2 * m, 220), h: H - 2 * m, data: "123456789012", symbology: "code128" }; break;
    case "rect": el = { type, x: 2, y: 2, w: W - 4, h: H - 4, stroke: 3, radius: 8 }; break;
    case "ellipse": { const s = Math.min(W, H) - 2 * m; el = { type, x: Math.round(W / 2 - s / 2), y: m, w: s, h: s, stroke: 3 }; break; }
    case "line": el = W >= H ? { type, x1: m, y1: Math.round(H / 2), x2: W - m, y2: Math.round(H / 2), width: 2 } : { type, x1: Math.round(W / 2), y1: m, x2: Math.round(W / 2), y2: H - m, width: 2 }; break;
    case "image": el = { type, x: 0, y: 0, w: Math.min(W, H), h: Math.min(W, H), ...extra }; break;
    default: return;
  }
  if (type === "image") Object.assign(el, extra);
  checkpoint();
  S.label.elements.push(el);
  S.sel = S.label.elements.length - 1;
  changed(true);
  if (el.type === "text") setTimeout(() => startInlineEdit(S.label.elements.length - 1, true), 60);
}

function deleteSelected() {
  if (S.sel < 0) return;
  checkpoint();
  S.label.elements.splice(S.sel, 1);
  S.sel = Math.min(S.sel, S.label.elements.length - 1);
  changed(true);
}

function duplicateSelected() {
  if (S.sel < 0) return;
  checkpoint();
  const el = clone(S.label.elements[S.sel]);
  if (el.type === "line") { el.x1 += 6; el.x2 += 6; el.y1 += 6; el.y2 += 6; } else { el.x += 6; el.y += 6; }
  S.label.elements.splice(S.sel + 1, 0, el);
  S.sel += 1;
  changed(true);
}

function moveLayer(delta) {
  const i = S.sel, j = i + delta;
  if (i < 0 || j < 0 || j >= S.label.elements.length) return;
  checkpoint();
  const els = S.label.elements;
  [els[i], els[j]] = [els[j], els[i]];
  S.sel = j;
  changed(true);
}

function nudge(dx, dy) {
  if (S.sel < 0) return;
  checkpoint(false);
  const el = S.label.elements[S.sel];
  if (el.type === "line") { el.x1 += dx; el.x2 += dx; el.y1 += dy; el.y2 += dy; } else { el.x = (el.x ?? 0) + dx; el.y = (el.y ?? 0) + dy; }
  changed();
}

function elementTitle(el) {
  const t = el.text ?? el.data ?? "";
  if (t) return String(t).replace(/\n/g, " ").slice(0, 22);
  return TYPE_NAMES[el.type];
}

function renderLayers() {
  const ol = $("#layers");
  ol.innerHTML = "";
  S.label.elements.forEach((el, i) => {
    const li = document.createElement("li");
    li.className = i === S.sel ? "sel" : "";
    li.innerHTML = `<span class="kind">${esc(TYPE_NAMES[el.type])}</span>${esc(elementTitle(el))}`;
    li.onclick = () => select(i);
    ol.appendChild(li);
  });
  if (!S.label.elements.length) ol.innerHTML = '<li class="muted">Nothing yet</li>';
}

// ---------------------------------------------------------------- property panel
function propField(container, el, key, label, kind, opts = {}) {
  const wrap = document.createElement("label");
  wrap.className = kind === "check" ? "check" : "field";
  let input;
  const val = el[key] ?? DEFAULTS[el.type]?.[key] ?? DEFAULTS.common[key];
  const set = (v) => { checkpoint(false); el[key] = v; changed(); };
  if (kind === "textarea") {
    input = document.createElement("textarea");
    input.value = val ?? "";
    input.oninput = () => set(input.value);
  } else if (kind === "select") {
    input = document.createElement("select");
    for (const [v, t] of opts.options) {
      const o = document.createElement("option");
      o.value = v; o.textContent = t;
      if (String(v) === String(val)) o.selected = true;
      input.appendChild(o);
    }
    input.onchange = () => set(opts.number ? +input.value : input.value);
  } else if (kind === "check") {
    input = document.createElement("input");
    input.type = "checkbox";
    input.checked = !!val;
    input.onchange = () => set(input.checked);
  } else if (kind === "range") {
    const row = document.createElement("div");
    row.className = "range-row";
    input = document.createElement("input");
    input.type = "range";
    Object.assign(input, { min: opts.min, max: opts.max, step: opts.step || 1, value: val });
    const out = document.createElement("output");
    out.textContent = val;
    input.oninput = () => { out.textContent = input.value; set(+input.value); };
    row.append(input, out);
    wrap.append(label, row);
    container.appendChild(wrap);
    return input;
  } else {
    input = document.createElement("input");
    input.type = kind === "number" ? "number" : "text";
    if (opts.list) input.setAttribute("list", opts.list);
    if (opts.step) input.step = opts.step;
    input.value = val ?? "";
    input.oninput = () => {
      if (kind === "number") { if (input.value === "" || isNaN(+input.value)) return; set(+input.value); } else set(input.value);
    };
  }
  if (opts.id) input.id = opts.id;
  if (kind === "check") wrap.append(input, label); else wrap.append(label, input);
  container.appendChild(wrap);
  return input;
}

function segField(container, el, key, label, options) {
  const f = document.createElement("div");
  f.className = "field";
  f.append(label);
  const seg = document.createElement("div");
  seg.className = "seg";
  const cur = el[key] ?? DEFAULTS[el.type][key];
  for (const [v, t] of options) {
    const b = document.createElement("button");
    b.textContent = t;
    b.type = "button";
    if (v === cur) b.className = "on";
    b.onclick = () => { checkpoint(); el[key] = v; [...seg.children].forEach((c) => c.classList.remove("on")); b.classList.add("on"); changed(); };
    seg.appendChild(b);
  }
  f.appendChild(seg);
  container.appendChild(f);
}

function section(parent, title) {
  const s = document.createElement("div");
  s.className = "sect";
  if (title) { const h = document.createElement("h2"); h.textContent = title; s.appendChild(h); }
  parent.appendChild(s);
  return s;
}

function syncBoxInputs() {
  if (S.sel < 0) return;
  const el = S.label.elements[S.sel];
  for (const k of ["x", "y", "w", "h", "x1", "y1", "x2", "y2"]) {
    const inp = document.getElementById("prop-" + k);
    if (inp && document.activeElement !== inp && el[k] !== undefined) inp.value = el[k];
  }
}

function renderProps() {
  const p = $("#props");
  p.innerHTML = "";
  if (S.sel < 0 || !S.label.elements[S.sel]) return renderLabelProps(p);
  const el = S.label.elements[S.sel];
  const h = document.createElement("h2");
  h.textContent = TYPE_NAMES[el.type];
  p.appendChild(h);

  if (el.type === "text") {
    propField(p, el, "text", "Text (newlines allowed)", "textarea", { id: "prop-text" });
    if (/\{\{/.test(el.text || "")) {
      const hint = document.createElement("p");
      hint.className = "muted";
      hint.style.margin = "-4px 0 10px";
      hint.textContent = "{{name}} is replaced by the Variables field or CSV value. {{date:%Y/%m/%d}} is replaced by the print date (%m=month %d=day %H:%M=time).";
      p.appendChild(hint);
    }
    propField(p, el, "font", "Font (auto / name / path)", "text", { list: "font-list" });
    const sz = document.createElement("div");
    sz.className = "field";
    sz.append("Font size");
    const row = document.createElement("div");
    row.className = "size-row";
    const auto = document.createElement("label");
    auto.className = "check";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = el.size === undefined || el.size === "auto" || el.size === null;
    auto.append(cb, "Fit to box");
    const num = document.createElement("input");
    num.type = "number"; num.min = 4; num.max = 400;
    num.value = cb.checked ? (S.render.boxes[S.sel]?.font_size || 24) : el.size;
    num.disabled = cb.checked;
    const note = document.createElement("span");
    note.id = "auto-size-note";
    note.className = "muted";
    note.textContent = S.render.boxes[S.sel]?.font_size ? `Current ${S.render.boxes[S.sel].font_size}px` : "";
    cb.onchange = () => { checkpoint(); el.size = cb.checked ? "auto" : (+num.value || 24); num.disabled = cb.checked; changed(); };
    num.oninput = () => { if (+num.value > 0) { checkpoint(false); el.size = +num.value; changed(); } };
    row.append(auto, num, note);
    sz.appendChild(row);
    p.appendChild(sz);
    const g = document.createElement("div");
    g.className = "row";
    propField(g, el, "bold", "Bold", "check");
    propField(g, el, "wrap", "Auto wrap", "check");
    propField(g, el, "vertical", "Vertical text", "check");
    p.appendChild(g);
    segField(p, el, "align", "Horizontal align", [["left", "Left"], ["center", "Center"], ["right", "Right"]]);
    segField(p, el, "valign", "Vertical align", [["top", "Top"], ["middle", "Middle"], ["bottom", "Bottom"]]);
    const adv = section(p, "Advanced");
    const g2 = document.createElement("div");
    g2.className = "grid2";
    propField(g2, el, "line_spacing", "Line spacing", "number", { step: 0.05 });
    propField(g2, el, "letter_spacing", "Letter spacing (px)", "number");
    propField(g2, el, "stroke", "Thicken (px)", "number");
    propField(g2, el, "min_size", "Min size", "number");
    adv.appendChild(g2);
  } else if (el.type === "qr") {
    propField(p, el, "data", "Content (URL / text)", "textarea");
    const g = document.createElement("div");
    g.className = "grid2";
    propField(g, el, "ecc", "Error correction", "select", { options: [["L", "L (7%)"], ["M", "M (15%)"], ["Q", "Q (25%)"], ["H", "H (30%)"]] });
    propField(g, el, "border", "Margin (modules)", "number");
    p.appendChild(g);
  } else if (el.type === "barcode") {
    propField(p, el, "data", "Content", "text");
    propField(p, el, "symbology", "Type", "select", { options: SYMBOLOGIES.map((s) => [s, s.toUpperCase()]) });
    const g = document.createElement("div");
    g.className = "grid2";
    propField(g, el, "show_text", "Show digits", "check");
    propField(g, el, "text_size", "Digit size", "number");
    p.appendChild(g);
  } else if (el.type === "image") {
    const b = document.createElement("button");
    b.className = "btn";
    b.textContent = "Replace image…";
    b.onclick = () => pickImage((src) => { checkpoint(); el.src = src; changed(); });
    p.appendChild(b);
    segField(p, el, "fit", "Fit", [["contain", "Contain"], ["cover", "Cover"], ["stretch", "Stretch"]]);
    segField(p, el, "dither", "Black/white", [["floyd", "Dither (photo)"], ["threshold", "Threshold (logo)"]]);
    propField(p, el, "threshold", "Threshold", "range", { min: 1, max: 254 });
    propField(p, el, "brightness", "Brightness", "range", { min: 0.2, max: 2, step: 0.05 });
    propField(p, el, "contrast", "Contrast", "range", { min: 0.2, max: 3, step: 0.05 });
    propField(p, el, "invert", "Invert", "check");
  } else if (el.type === "rect" || el.type === "ellipse") {
    const g = document.createElement("div");
    g.className = "grid2";
    propField(g, el, "stroke", "Line width", "number");
    if (el.type === "rect") propField(g, el, "radius", "Corner radius", "number");
    p.appendChild(g);
    propField(p, el, "fill", "Fill", "check");
  } else if (el.type === "line") {
    const g = document.createElement("div");
    g.className = "grid4";
    for (const k of ["x1", "y1", "x2", "y2"]) propField(g, el, k, k, "number", { id: "prop-" + k });
    p.appendChild(g);
    propField(p, el, "width", "Width", "number");
  }

  const pos = section(p, "Position and size (dots)");
  if (el.type !== "line") {
    const g = document.createElement("div");
    g.className = "grid4";
    for (const k of ["x", "y", "w", "h"]) propField(g, el, k, k.toUpperCase(), "number", { id: "prop-" + k });
    pos.appendChild(g);
    const g2 = document.createElement("div");
    g2.className = "grid2";
    propField(g2, el, "rotate", "Rotate", "select", { number: true, options: [[0, "0°"], [90, "90°"], [180, "180°"], [270, "270°"]] });
    propField(g2, el, "color", "Color", "select", { options: [["black", "Black"], ["white", "White (knockout)"]] });
    pos.appendChild(g2);
    const al = document.createElement("div");
    al.className = "row";
    const [W, H] = canvasSize();
    for (const [t, fn] of [
      ["Center horizontally", () => { el.x = Math.round((W - el.w) / 2); }],
      ["Center vertically", () => { el.y = Math.round((H - el.h) / 2); }],
      ["Fill label", () => { el.x = 0; el.y = 0; el.w = W; el.h = H; }],
    ]) {
      const b = document.createElement("button");
      b.className = "btn small ghost";
      b.textContent = t;
      b.onclick = () => { checkpoint(); fn(); changed(); syncBoxInputs(); };
      al.appendChild(b);
    }
    pos.appendChild(al);
  } else {
    propField(pos, el, "color", "Color", "select", { options: [["black", "Black"], ["white", "White (knockout)"]] });
  }

  const act = document.createElement("div");
  act.className = "actions";
  for (const [t, fn, cls] of [["Bring forward", () => moveLayer(1)], ["Send back", () => moveLayer(-1)], ["Duplicate", duplicateSelected], ["Delete", deleteSelected, "danger"]]) {
    const b = document.createElement("button");
    b.className = "btn " + (cls || "");
    b.textContent = t;
    b.onclick = fn;
    act.appendChild(b);
  }
  p.appendChild(act);
}

function renderLabelProps(p) {
  const h = document.createElement("h2");
  h.textContent = "Label settings";
  p.appendChild(h);
  const L = S.label;
  propField(p, L, "name", "Name", "text");
  const g = document.createElement("div");
  g.className = "grid2";
  const mw = propField(g, L.media, "width_mm", "Width (mm)", "number");
  const ml = propField(g, L.media, "length_mm", "Length (mm)", "number");
  mw.oninput = () => { if (+mw.value > 0) { checkpoint(false); L.media.width_mm = +mw.value; changed(); } };
  ml.oninput = () => { if (+ml.value > 0) { checkpoint(false); L.media.length_mm = +ml.value; delete L.media.length_dots; changed(); } };
  p.appendChild(g);
  propField(p, L, "invert", "Invert black and white", "check");
  const info = document.createElement("p");
  info.className = "empty";
  const [W, H] = canvasSize();
  info.innerHTML = `Print area: <b>${W}×${H}</b> dots (about 8 dots = 1 mm).<br>Add elements from "Add" on the left. Drag to move, drag a corner to resize.<br><br>
    Text: <b>double-click</b> (or <span class="kbd">Enter</span>) to edit in place, <span class="kbd">Esc</span> to cancel.<br>
    <span class="kbd">Del</span> Delete · <span class="kbd">Ctrl+D</span> Duplicate · <span class="kbd">Arrows</span> Move 1 dot (Shift for 8)<br>
    <span class="kbd">Ctrl+Z</span> Undo · <span class="kbd">Ctrl+S</span> Save · <span class="kbd">Ctrl+P</span> Print<br>
    While dragging: <span class="kbd">Alt</span> disables snapping, <span class="kbd">Shift</span> keeps the aspect ratio.<br>
    You can also drag and drop images or .json files.`;
  p.appendChild(info);
}

// ---------------------------------------------------------------- variables / CSV
function renderVars() {
  const vars = S.render.variables;
  const card = $("#vars-card");
  card.hidden = !vars.length && !S.csv;
  const grid = $("#vars-grid");
  grid.innerHTML = "";
  for (const name of vars) {
    const f = document.createElement("label");
    f.className = "field";
    f.append(`{{${name}}}`);
    const inp = document.createElement("input");
    inp.value = S.values[name] ?? S.label.variables?.[name] ?? "";
    inp.placeholder = S.csv ? "(using CSV value)" : "";
    inp.oninput = () => { S.values[name] = inp.value; S.label.variables = { ...(S.label.variables || {}), [name]: inp.value }; requestRender(); clearTimeout(saveTimer); saveTimer = setTimeout(saveLocal, 400); };
    f.appendChild(inp);
    grid.appendChild(f);
  }
  const info = $("#csv-info");
  info.hidden = !S.csv;
  $("#csv-clear").hidden = !S.csv;
  if (S.csv) {
    const cols = Object.keys(S.csv[0] || {});
    info.innerHTML = `CSV: <b>${S.csv.length}</b> rows (columns: ${esc(cols.join(", "))}) -> printing makes ${S.csv.length} labels. Preview row:`;
    const rows = document.createElement("div");
    rows.className = "rows";
    S.csv.slice(0, 200).forEach((r, i) => {
      const b = document.createElement("button");
      b.textContent = i + 1;
      b.title = Object.values(r).join(" / ");
      if (i === S.csvRow) b.className = "on";
      b.onclick = () => { S.csvRow = i; renderVars(); requestRender(); };
      rows.appendChild(b);
    });
    info.appendChild(rows);
  }
  updatePrintLabel();
}

function parseCSV(text) {
  const rows = [];
  let row = [], cur = "", q = false;
  text = text.replace(/^﻿/, "");
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (q) {
      if (c === '"' && text[i + 1] === '"') { cur += '"'; i++; } else if (c === '"') q = false; else cur += c;
    } else if (c === '"') q = true;
    else if (c === ",") { row.push(cur); cur = ""; }
    else if (c === "\n" || c === "\r") {
      if (c === "\r" && text[i + 1] === "\n") i++;
      row.push(cur); cur = "";
      if (row.some((v) => v !== "")) rows.push(row);
      row = [];
    } else cur += c;
  }
  row.push(cur);
  if (row.some((v) => v !== "")) rows.push(row);
  const head = rows.shift() || [];
  return rows.map((r) => Object.fromEntries(head.map((h, i) => [h.trim(), r[i] ?? ""])));
}

// ---------------------------------------------------------------- top bar / files
function syncTop() {
  const sel = $("#media");
  const key = `${S.label.media.width_mm}x${S.label.media.length_mm}`;
  if ([...sel.options].some((o) => o.value === key)) sel.value = key; else sel.value = "custom";
  for (const b of $("#orientation").children) b.classList.toggle("on", b.dataset.v === (S.label.orientation || "landscape"));
  $("#copies").value = S.label.copies || 1;
  $("#density").value = S.label.density || 15;
  $("#density-out").textContent = S.label.density || 15;
}

function updatePrintLabel() {
  const n = (S.csv ? S.csv.length : 1) * (S.label.copies || 1);
  $("#print .label").textContent = n > 1 ? `Print (${n})` : "Print";
}

function loadLabel(lab, keepHistory = false) {
  if (!keepHistory) checkpoint();
  S.label = { ...newLabel(), ...lab, media: { ...newLabel().media, ...(lab.media || {}) } };
  if (typeof lab.media === "string" && S.meta.media_presets?.[lab.media]) S.label.media = { ...S.meta.media_presets[lab.media] };
  S.label.elements = S.label.elements || [];
  S.values = { ...(S.label.variables || {}) };
  S.sel = -1;
  S.zoom = 0;
  changed(true);
}

function download(name, blob) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

function saveFile() {
  const name = (S.label.name || "label").replace(/[\\/:*?"<>|]/g, "_") + ".p21.json";
  download(name, new Blob([JSON.stringify(S.label, null, 2)], { type: "application/json" }));
}

function exportPNG(scale = 4) {
  const img = $("#preview");
  if (!img.naturalWidth) return;
  const c = document.createElement("canvas");
  c.width = img.naturalWidth * scale; c.height = img.naturalHeight * scale;
  const g = c.getContext("2d");
  g.imageSmoothingEnabled = false;
  g.drawImage(img, 0, 0, c.width, c.height);
  c.toBlob((b) => download((S.label.name || "label") + ".png", b));
}

function readImageFile(file, cb) {
  const fr = new FileReader();
  fr.onload = () => {
    const im = new Image();
    im.onload = () => {
      // shrink big photos so the label file stays small (the printer only has 96 dots anyway)
      const max = 800, k = Math.min(1, max / Math.max(im.width, im.height));
      const c = document.createElement("canvas");
      c.width = Math.round(im.width * k); c.height = Math.round(im.height * k);
      const g = c.getContext("2d");
      g.drawImage(im, 0, 0, c.width, c.height);
      const px = g.getImageData(0, 0, c.width, c.height).data, seen = new Set();
      for (let i = 0; i < px.length && seen.size < 40; i += 4 * 7) seen.add((px[i] >> 4) * 256 + (px[i + 1] >> 4) * 16 + (px[i + 2] >> 4));
      cb(c.toDataURL("image/png"), im.width / im.height, seen.size < 40 ? "threshold" : "floyd");
    };
    im.src = fr.result;
  };
  fr.readAsDataURL(file);
}

let pickCb = null;
function pickImage(cb) { pickCb = cb; $("#image-file").value = ""; $("#image-file").click(); }

function addImageFromFile(file) {
  readImageFile(file, (src, ratio, dither) => {
    const [W, H] = canvasSize();
    let h = H, w = Math.round(H * ratio);
    if (w > W) { w = W; h = Math.round(W / ratio); }
    addElement("image", { src, w, h, x: Math.round((W - w) / 2), y: Math.round((H - h) / 2), dither });
  });
}

function openFile(file) {
  if (file.type.startsWith("image/")) return addImageFromFile(file);
  file.text().then((t) => {
    let data;
    try { data = JSON.parse(t); } catch { return toast(`${file.name} is not a label file (.p21.json)`, "err"); }
    if (!data || !Array.isArray(data.elements)) return toast(`${file.name} is not a label file (.p21.json)`, "err");
    loadLabel(data);
    toast(`Opened ${file.name}`, "ok");
  });
}

// ---------------------------------------------------------------- templates
const TEMPLATES = [
  ["One-line text", (W, H) => [{ type: "text", x: 6, y: 4, w: W - 12, h: H - 8, text: "Label", bold: true }], true],
  ["Title + subtitle", (W, H) => [
    { type: "text", x: 6, y: 4, w: W - 12, h: Math.round(H * 0.6) - 4, text: "Title", bold: true },
    { type: "line", x1: 10, y1: Math.round(H * 0.62), x2: W - 10, y2: Math.round(H * 0.62), width: 2 },
    { type: "text", x: 6, y: Math.round(H * 0.66), w: W - 12, h: Math.round(H * 0.32), text: "Subtitle" },
  ]],
  ["QR + text", (W, H) => [
    { type: "qr", x: 6, y: 6, w: H - 12, h: H - 12, data: "https://example.com" },
    { type: "text", x: H, y: 6, w: W - H - 6, h: Math.round(H * 0.58), text: "Title", bold: true },
    { type: "text", x: H, y: Math.round(H * 0.62), w: W - H - 6, h: Math.round(H * 0.32), text: "example.com" },
  ]],
  ["Barcode", (W, H) => [{ type: "barcode", x: 8, y: 6, w: W - 16, h: H - 12, data: "4901234567894", symbology: "ean13" }]],
  ["Framed name", (W, H) => [
    { type: "rect", x: 1, y: 1, w: W - 2, h: H - 2, stroke: 3, radius: 12 },
    { type: "text", x: 12, y: 10, w: W - 24, h: H - 20, text: "Name", bold: true },
  ], true],
  ["Knockout header", (W, H) => [
    { type: "rect", x: 0, y: 0, w: W, h: Math.round(H * 0.4), fill: true, radius: 0 },
    { type: "text", x: 6, y: 2, w: W - 12, h: Math.round(H * 0.4) - 4, text: "Heading", bold: true, color: "white" },
    { type: "text", x: 6, y: Math.round(H * 0.44), w: W - 12, h: Math.round(H * 0.54), text: "Body text" },
  ]],
  ["Dated label", (W, H) => [
    { type: "text", x: 6, y: 4, w: W - 12, h: Math.round(H * 0.5), text: "{{item}}", bold: true },
    { type: "text", x: 6, y: Math.round(H * 0.56), w: W - 12, h: Math.round(H * 0.4), text: "{{date:%Y/%m/%d}}" },
  ]],
  ["Cable flag", (W, H) => {
    const wrap = Math.round(W * 0.22), side = Math.round((W - wrap) / 2);
    return [
      { type: "text", x: 3, y: 4, w: side - 6, h: H - 8, text: "LAN-01", bold: true },
      { type: "line", x1: side, y1: 4, x2: side, y2: 12, width: 1 },
      { type: "line", x1: side, y1: H - 12, x2: side, y2: H - 4, width: 1 },
      { type: "line", x1: W - side, y1: 4, x2: W - side, y2: 12, width: 1 },
      { type: "line", x1: W - side, y1: H - 12, x2: W - side, y2: H - 4, width: 1 },
      { type: "text", x: W - side + 3, y: 4, w: side - 6, h: H - 8, text: "LAN-01", bold: true },
    ];
  }],
  ["Vertical text", (W, H) => [{ type: "text", x: 4, y: 4, w: W - 8, h: H - 8, text: "Vertical label", vertical: true, bold: true }], true],
];

function renderTemplates() {
  const box = $("#templates");
  for (const [name, fn, portraitOk] of TEMPLATES) {
    const b = document.createElement("button");
    b.textContent = name;
    b.onclick = () => {
      if (S.label.elements.length && !confirm("Replace the current design? (Undo to revert)")) return;
      checkpoint();
      if (S.label.orientation === "portrait" && !portraitOk) {
        S.label.orientation = "landscape";
        S.zoom = 0;
        toast("This template is for landscape, so orientation was set to Landscape", "", 5000);
      }
      const [W, H] = canvasSize();
      S.label.elements = fn(W, H);
      S.sel = -1;
      changed(true);
    };
    box.appendChild(b);
  }
}

// ---------------------------------------------------------------- user templates (saved on the server)
async function loadMyTemplates() {
  const box = $("#my-templates");
  let list = [];
  try { list = (await api("/api/templates")).templates; } catch (e) { box.innerHTML = `<p class="empty">Cannot load:${esc(e.message)}</p>`; return; }
  box.innerHTML = "";
  if (!list.length) {
    box.innerHTML = '<p class="empty">Save a design you use often with "Save current design" to reuse it from here (also usable from the CLI and AI).</p>';
    return;
  }
  for (const t of list) {
    const item = document.createElement("div");
    item.className = "tpl";
    item.title = t.error ? t.error : `${t.media.width_mm}×${t.media.length_mm} mm` + (t.variables.length ? `\nVariables: ${t.variables.join(", ")}` : "");
    item.innerHTML = (t.png ? `<img src="${t.png}" alt="">` : "") +
      `<span class="tpl-name">${esc(t.name)}</span>` +
      `<span class="tpl-meta">${t.error ? "Unreadable" : `${t.media.width_mm}×${t.media.length_mm}` + (t.variables.length ? ` · {{${esc(t.variables.join("}} {{"))}}}` : "")}</span>`;
    const del = document.createElement("button");
    del.className = "tpl-del";
    del.title = "Delete this template";
    del.textContent = "✕";
    del.onclick = async (e) => {
      e.stopPropagation();
      if (!confirm(`Delete template "${t.name}"? This cannot be undone.`)) return;
      try { await api("/api/templates/delete", { name: t.name }); toast(`Deleted "${t.name}"`, "ok"); loadMyTemplates(); } catch (err) { toast(err.message, "err"); }
    };
    item.appendChild(del);
    item.onclick = async () => {
      if (t.error) return toast(t.error, "err");
      if (S.label.elements.length && !confirm(`Replace the current design with "${t.name}"? (Undo to revert)`)) return;
      try {
        const r = await api("/api/templates/load", { name: t.name });
        loadLabel(r.label);
        toast(`Loaded "${t.name}"` + (t.variables.length ? ". Fill in the variable values." : ""), "ok");
      } catch (err) { toast(err.message, "err"); }
    };
    box.appendChild(item);
  }
}

async function saveAsTemplate() {
  if (!S.label.elements.length) return toast("Cannot save: no elements", "err");
  const name = (prompt("Template name", S.label.name || "") || "").trim();
  if (!name) return;
  try {
    const ex = await api("/api/templates/exists", { name });
    if (ex.exists && !confirm(`"${name}" already exists. Overwrite?`)) return;
    await api("/api/templates/save", { name, label: { ...S.label, name }, overwrite: ex.exists });
    toast(`Saved template "${name}"`, "ok");
    loadMyTemplates();
  } catch (e) { toast("Could not save: " + e.message, "err"); }
}

// ---------------------------------------------------------------- printer
function setDeviceUI(state, label, battery = "") {
  $("#device-dot").className = "dot " + (state || "");
  $("#device-label").textContent = label;
  $("#device-battery").textContent = battery;
}

async function refreshDevice(quiet = true) {
  if (!S.device) { setDeviceUI("", "No printer set"); return null; }
  setDeviceUI("busy", S.device);
  try {
    const info = await api("/api/info");
    const bat = info.battery ? `${info.battery.percent}%${info.battery.charging ? "⚡" : ""}` : "";
    const st = info.status;
    setDeviceUI(st && st.readiness === 0 ? "ok" : "err", st ? `${S.device} · ${st.readiness_text}` : S.device, bat);
    if (st && st.has_rfid_tag && (st.label_width_mm !== S.label.media.width_mm || st.label_length_mm !== S.label.media.length_mm)) {
      toast(`Printer has ${st.label_width_mm}×${st.label_length_mm} mm labels loaded`, "", 9000, [["Match size", () => {
        checkpoint(); S.label.media = { width_mm: st.label_width_mm, length_mm: st.label_length_mm }; S.zoom = 0; changed(true);
      }]]);
    }
    return info;
  } catch (e) {
    setDeviceUI("err", `${S.device} · Cannot connect`);
    if (!quiet) toast(e.message, "err", 8000);
    return null;
  }
}

async function doPrint() {
  if (S.printing) return;
  if (!S.device) { toast("Set a printer first", "err"); openDeviceDialog(); return; }
  const n = S.csv ? S.csv.length : 1;
  if (n > 1 && !confirm(`Print ${n} CSV rows, ${n * (S.label.copies || 1)} labels in total. Continue?`)) return;
  S.printing = true;
  const btn = $("#print");
  btn.disabled = true;
  btn.querySelector(".spinner").hidden = false;
  setDeviceUI("busy", `${S.device} · Printing…`);
  try {
    const r = await api("/api/print", { label: S.label, values: S.values, rows: S.csv, copies: S.label.copies, density: S.label.density });
    const warn = r.results.flatMap((x) => x.warnings).concat(r.render_warnings);
    let head = r.ok ? `Printed (${r.sheets} sheets)` : `Print problem (${r.printed}/${r.labels} labels printed)`;
    if (r.uncertain && r.uncertain.length) head += `\nSheet(s) ${r.uncertain.join(", ")} may not have printed. Check the labels.`;
    if (r.not_sent) head += `\n${r.not_sent} label(s) not sent. Refill labels and print those again.`;
    toast(head + (r.error ? `\nStopped partway. Printing again will duplicate: ${r.error}` : "") + (warn.length ? "\n" + [...new Set(warn)].join("\n") : ""), r.ok ? "ok" : "err", 9000);
    setDeviceUI(r.ok ? "ok" : "err", `${S.device} · ${r.ok ? "Ready" : "Check printer"}`, $("#device-battery").textContent);
  } catch (e) {
    toast("Could not print: " + e.message, "err", 9000);
    setDeviceUI("err", `${S.device} · Error`);
  } finally {
    S.printing = false;
    btn.disabled = false;
    btn.querySelector(".spinner").hidden = true;
  }
}

function openDeviceDialog() {
  $("#device-input").value = S.device || "";
  $("#device-info").hidden = true;
  $("#device-dialog").showModal();
}

async function scanDevices() {
  const list = $("#scan-list");
  $("#scan-spinner").hidden = false;
  $("#scan").disabled = true;
  list.innerHTML = "";
  try {
    const r = await api(`/api/scan?ble=${$("#scan-ble").checked ? 1 : 0}`);
    if (!r.suggestions.length) list.innerHTML = '<li class="none">None found. Check power and pairing.</li>';
    for (const s of r.suggestions) {
      const li = document.createElement("li");
      li.innerHTML = `<div class="meta"><div>${esc(s.name || "P21")}</div><code>${esc(s.device)}</code> <span class="muted">${esc(s.via)}</span></div>`;
      const b = document.createElement("button");
      b.className = "btn";
      b.textContent = "Use";
      b.onclick = () => { $("#device-input").value = s.device; saveDevice(); };
      li.appendChild(b);
      list.appendChild(li);
    }
  } catch (e) {
    list.innerHTML = `<li class="none">Scan failed:${esc(e.message)}</li>`;
  } finally {
    $("#scan-spinner").hidden = true;
    $("#scan").disabled = false;
  }
}

async function saveDevice() {
  const dev = $("#device-input").value.trim();
  try {
    await api("/api/device", { device: dev || null });
    S.device = dev || null;
    toast(dev ? `Printer set to ${dev}` : "Printer setting cleared", "ok");
    checkDevice();
  } catch (e) { toast(e.message, "err"); }
}

async function checkDevice() {
  const box = $("#device-info");
  box.hidden = false;
  box.textContent = "Checking…";
  const info = await refreshDevice(false);
  if (!info) { box.textContent = "Cannot connect. Check that the printer is on and the device spec is correct."; return; }
  const st = info.status, bat = info.battery, cfg = info.config;
  box.textContent = [
    `Device: ${info.device}`,
    st ? `Status: ${st.readiness_text}` : "",
    st ? (st.has_rfid_tag ? `Label: ${st.label_width_mm}×${st.label_length_mm} mm (${st.label_color} / ${st.paper_type})` : "Label: cannot read RFID") : "",
    bat ? `Battery: ${bat.percent}%${bat.charging ? " (charging)" : ""}` : "",
    cfg ? `Firmware ${cfg.firmware_version} / ${cfg.dpi} dpi / Auto-off ${cfg.auto_off_minutes || "none"} / Beep ${cfg.beep ? "on" : "off"}` : "",
  ].filter(Boolean).join("\n");
}

// ---------------------------------------------------------------- wiring
function bind() {
  const stage = $("#stage");
  $("#stage-wrap").addEventListener("pointerdown", onPointerDown);
  stage.addEventListener("pointermove", onPointerMove);
  stage.addEventListener("pointerup", onPointerUp);
  stage.addEventListener("pointercancel", onPointerUp);
  stage.addEventListener("dblclick", (e) => {
    if (e.target.closest(".inline-edit") || S.sel < 0) return;
    const el = S.label.elements[S.sel];
    if (el.type === "text") startInlineEdit(S.sel);
    else { const t = $("#props textarea") || $("#props input"); if (t) t.focus(); }
  });

  document.querySelectorAll("[data-add]").forEach((b) => b.onclick = () => b.dataset.add === "image" ? pickImage(null) : addElement(b.dataset.add));
  $("#image-file").onchange = (e) => {
    const f = e.target.files[0];
    if (!f) return;
    if (pickCb) { const cb = pickCb; pickCb = null; readImageFile(f, (src) => cb(src)); } else addImageFromFile(f);
  };

  const media = $("#media");
  for (const [k, m] of Object.entries(S.meta.media_presets || {})) {
    const o = document.createElement("option");
    o.value = `${m.width_mm}x${m.length_mm}`;
    o.textContent = `${m.width_mm}×${m.length_mm} mm`;
    media.appendChild(o);
  }
  media.insertAdjacentHTML("beforeend", '<option value="custom">Custom…</option>');
  media.onchange = () => {
    checkpoint();
    const [oW, oH] = canvasSize();
    if (media.value === "custom") {
      const len = prompt("Label length (mm)", S.label.media.length_mm);
      if (len && +len > 0) S.label.media = { ...S.label.media, length_mm: +len };
    } else {
      const [w, l] = media.value.split("x").map(Number);
      S.label.media = { width_mm: w, length_mm: l };
    }
    rescaleTo(oW, oH);
    S.zoom = 0;
    changed(true);
  };
  for (const b of $("#orientation").children) b.onclick = () => {
    if ((S.label.orientation || "landscape") === b.dataset.v) return;
    checkpoint();
    const [oW, oH] = canvasSize();
    S.label.orientation = b.dataset.v;
    rescaleTo(oW, oH);
    S.zoom = 0;
    changed(true);
  };
  $("#copies").oninput = (e) => { S.label.copies = clamp(+e.target.value || 1, 1, 99); updatePrintLabel(); saveLocal(); };
  $("#copies").onchange = (e) => { e.target.value = S.label.copies; };
  $("#density").oninput = (e) => { S.label.density = +e.target.value; $("#density-out").textContent = e.target.value; saveLocal(); };

  $("#undo").onclick = undo;
  $("#redo").onclick = redo;
  $("#new").onclick = () => { if (!S.label.elements.length || confirm("Create a new label? (Undo to revert)")) loadLabel({ media: S.label.media }); };
  $("#open").onclick = () => { $("#open-file").value = ""; $("#open-file").click(); };
  $("#open-file").onchange = (e) => e.target.files[0] && openFile(e.target.files[0]);
  $("#save").onclick = saveFile;
  $("#export-png").onclick = () => exportPNG();
  $("#print").onclick = doPrint;
  $("#zoom-in").onclick = () => { S.zoom = clamp(zoom() + 0.5, 1, 12); renderOverlay(); };
  $("#zoom-out").onclick = () => { S.zoom = clamp(zoom() - 0.5, 1, 12); renderOverlay(); };
  $("#zoom-fit").onclick = () => { S.zoom = 0; renderOverlay(); };
  $("#show-boxes").onchange = renderOverlay;
  window.addEventListener("resize", () => { if (!S.zoom) renderOverlay(); });

  $("#csv-load").onclick = () => { $("#csv-file").value = ""; $("#csv-file").click(); };
  $("#csv-file").onchange = async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    const buf = await f.arrayBuffer();
    let text;
    try { text = new TextDecoder("utf-8", { fatal: true }).decode(buf); }
    catch { text = new TextDecoder("shift_jis").decode(buf); toast("CSV read as Shift_JIS (Excel format)", "", 4000); }
    const rows = parseCSV(text);
    if (!rows.length) return toast("CSV has no data rows", "err");
    S.csv = rows; S.csvRow = 0;
    const missing = S.render.variables.filter((v) => !(v in rows[0]));
    if (missing.length) toast(`Variables not in CSV: ${missing.join(", ")} (using the input field values)`, "", 7000);
    renderVars(); requestRender();
  };
  $("#csv-clear").onclick = () => { S.csv = null; renderVars(); requestRender(); };

  $("#device-pill").onclick = openDeviceDialog;
  $("#scan").onclick = scanDevices;
  $("#device-save").onclick = saveDevice;
  $("#device-mock").onclick = () => { $("#device-input").value = "mock:"; saveDevice(); };
  $("#device-check").onclick = checkDevice;
  $("#settings-apply").onclick = async () => {
    const beep = $("#set-beep").value, off = $("#set-autooff").value;
    try {
      await api("/api/settings", { beep: beep === "" ? null : beep === "1", auto_off: off === "" ? null : +off });
      toast("Settings applied", "ok");
      checkDevice();
    } catch (e) { toast(e.message, "err"); }
  };

  const wrap = $("#stage-wrap");
  wrap.addEventListener("dragover", (e) => { e.preventDefault(); });
  wrap.addEventListener("drop", (e) => { e.preventDefault(); const f = e.dataTransfer.files[0]; if (f) openFile(f); });
  document.addEventListener("paste", (e) => {
    if (e.target.closest("input, textarea")) return;
    const item = [...(e.clipboardData?.items || [])].find((i) => i.type.startsWith("image/"));
    if (item) addImageFromFile(item.getAsFile());
  });

  document.addEventListener("keydown", (e) => {
    const typing = e.target.closest("input, textarea, select");
    const mod = e.ctrlKey || e.metaKey;
    if (mod && e.key.toLowerCase() === "s") { e.preventDefault(); saveFile(); return; }
    if (mod && e.key.toLowerCase() === "p") { e.preventDefault(); doPrint(); return; }
    if (typing) return;
    if (mod && e.key.toLowerCase() === "z") { e.preventDefault(); e.shiftKey ? redo() : undo(); }
    else if (mod && e.key.toLowerCase() === "y") { e.preventDefault(); redo(); }
    else if (mod && e.key.toLowerCase() === "d") { e.preventDefault(); duplicateSelected(); }
    else if (e.key === "Delete" || e.key === "Backspace") { if (S.sel >= 0) { e.preventDefault(); deleteSelected(); } }
    else if (e.key === "Escape") select(-1);
    else if ((e.key === "Enter" || e.key === "F2") && S.sel >= 0 && S.label.elements[S.sel].type === "text") { e.preventDefault(); startInlineEdit(S.sel); }
    else if (e.key.startsWith("Arrow") && S.sel >= 0) {
      e.preventDefault();
      const s = e.shiftKey ? 8 : 1;
      nudge(e.key === "ArrowLeft" ? -s : e.key === "ArrowRight" ? s : 0, e.key === "ArrowUp" ? -s : e.key === "ArrowDown" ? s : 0);
    }
  });
}

async function loadFonts() {
  try {
    const r = await api("/api/fonts");
    const dl = document.createElement("datalist");
    dl.id = "font-list";
    const all = ["auto", ...(S.meta.fonts_preferred || []), ...r.families.filter((f) => !(S.meta.fonts_preferred || []).includes(f))];
    for (const f of all) { const o = document.createElement("option"); o.value = f; dl.appendChild(o); }
    document.body.appendChild(dl);
  } catch {}
}

async function main() {
  try { S.meta = await api("/api/meta"); } catch (e) { toast("Cannot connect to server: " + e.message, "err", 20000); return; }
  S.device = S.meta.device || null;
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem("p21studio.label") || "null"); } catch {}
  S.label = saved && saved.elements ? { ...newLabel(), ...saved } : { ...newLabel(), ...clone(S.meta.example) };
  S.values = { ...(S.label.variables || {}) };
  bind();
  renderTemplates();
  loadMyTemplates();
  $("#save-template").onclick = saveAsTemplate;
  changed(true);
  S.history = [];
  updateUndo();
  loadFonts();
  refreshDevice();
}

main();
