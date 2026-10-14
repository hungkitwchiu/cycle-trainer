"use strict";
/* CycleTrainer front-end: plain JS, no build step. */

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const api = async (method, url, body) => {
  const opt = { method, headers: {} };
  if (body instanceof FormData) opt.body = body;
  else if (body !== undefined) { opt.body = JSON.stringify(body); opt.headers["Content-Type"] = "application/json"; }
  const r = await fetch(url, opt);
  const txt = await r.text();
  let data; try { data = txt ? JSON.parse(txt) : null; } catch { data = txt; }
  if (!r.ok) throw new Error((data && data.detail) || r.statusText);
  return data;
};
const toast = (msg, err = false) => {
  const t = $("#toast");
  t.textContent = msg; t.className = "toast" + (err ? " error" : ""); t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), err ? 6000 : 3000);
};
const fmtTime = (s) => {
  if (s == null || isNaN(s)) return "--:--";
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(x).padStart(2, "0")}` : `${m}:${String(x).padStart(2, "0")}`;
};
const parseTime = (str) => {
  str = String(str).trim();
  if (!str) return 0;
  const parts = str.split(":").map(Number);
  if (parts.some(isNaN)) return 0;
  return parts.reduce((a, v) => a * 60 + v, 0);
};
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const ZONES = [[0.55, "--z1"], [0.75, "--z2"], [0.90, "--z3"], [1.05, "--z4"], [1.20, "--z5"], [99, "--z6"]];
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const zoneColor = (f) => css(ZONES.find(([lim]) => f < lim)[1]);

let SETTINGS = { ftp: 200 };
let STATE = null;

/* ================================================================= tabs */
function showTab(name) {
  if (!$("#tab-" + name)) name = "ride";
  $$(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + name));
  $$("nav a").forEach((a) => a.classList.toggle("active", a.dataset.tab === name));
  if (name === "workouts") loadLibrary();
  if (name === "activities") loadActivities();
  if (name === "settings") loadSettings();
  requestAnimationFrame(() => { drawRide(); drawEditor(); });
}
window.addEventListener("hashchange", () => showTab(location.hash.slice(1)));
$("#pills").onclick = () => (location.hash = "#settings");

/* ================================================================= canvas */
function setupCanvas(cv) {
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth, h = cv.clientHeight;
  if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {
    cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
  }
  const ctx = cv.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  return { ctx, w, h };
}

/** Draw workout blocks. segs: [{duration,start,end,free}], x(t) and y(frac) mappers. */
function drawBlocks(ctx, segs, x, y, alpha = 1) {
  let t = 0;
  for (const s of segs) {
    const x0 = x(t), x1 = x(t + s.duration);
    if (s.free) {
      ctx.fillStyle = `rgba(100,116,139,${0.35 * alpha})`;
      ctx.fillRect(x0, y(0.5), x1 - x0, y(0) - y(0.5));
    } else {
      ctx.globalAlpha = alpha;
      ctx.fillStyle = zoneColor((s.start + s.end) / 2);
      ctx.beginPath();
      ctx.moveTo(x0, y(0)); ctx.lineTo(x0, y(s.start)); ctx.lineTo(x1, y(s.end)); ctx.lineTo(x1, y(0));
      ctx.closePath(); ctx.fill();
      ctx.globalAlpha = 1;
    }
    t += s.duration;
  }
}

function drawMini(cv, segs) {
  const { ctx, w, h } = setupCanvas(cv);
  const total = segs.reduce((a, s) => a + s.duration, 0) || 1;
  const top = Math.max(1.2, ...segs.map((s) => Math.max(s.start, s.end))) * 1.05;
  drawBlocks(ctx, segs, (t) => (t / total) * w, (f) => h - (f / top) * h, 0.9);
}

/* ================================================================= ride */
let trace = [];          // [i, power, hr, target, wt]
let profile = [];        // segments of loaded workout
let profileKey = null;

function drawRide() {
  const cv = $("#ride-chart");
  if (!cv || !cv.clientWidth) return;
  const { ctx, w, h } = setupCanvas(cv);
  const ftp = (STATE && STATE.ftp) || SETTINGS.ftp || 200;
  const padL = 44, padR = 40, padT = 12, padB = 22;
  const W = w - padL - padR, H = h - padT - padB;
  const hasWk = profile.length > 0;
  const total = hasWk ? profile.reduce((a, s) => a + s.duration, 0) : 0;
  const now = STATE ? (hasWk ? STATE.workout_time : STATE.elapsed) : 0;

  let t0, t1, tx;
  if (hasWk) { t0 = 0; t1 = Math.max(total, now + 5); tx = (p) => p[4]; }
  else { t1 = Math.max(600, (STATE && STATE.elapsed) || 0); t0 = Math.max(0, t1 - 1200); tx = (p) => p[0]; }

  const maxP = Math.max(ftp * 1.3,
    ...profile.map((s) => Math.max(s.start, s.end) * ftp * ((STATE && STATE.intensity) || 1)),
    ...trace.slice(-1200).map((p) => p[1] || 0)) * 1.08;
  const x = (t) => padL + ((t - t0) / (t1 - t0 || 1)) * W;
  const yW = (watts) => padT + H - (watts / maxP) * H;
  const yF = (f) => yW(f * ftp * ((STATE && STATE.intensity) || 1));
  const yHR = (b) => padT + H - ((b - 40) / 170) * H;

  // grid
  ctx.font = "11px Segoe UI, system-ui"; ctx.fillStyle = css("--muted"); ctx.strokeStyle = "#232a34"; ctx.lineWidth = 1;
  const step = maxP > 600 ? 200 : maxP > 300 ? 100 : 50;
  for (let p = 0; p <= maxP; p += step) {
    const yy = Math.round(yW(p)) + 0.5;
    ctx.beginPath(); ctx.moveTo(padL, yy); ctx.lineTo(w - padR, yy); ctx.stroke();
    ctx.fillText(p + "", 8, yy + 4);
  }
  // FTP line
  ctx.setLineDash([4, 4]); ctx.strokeStyle = "#475569";
  ctx.beginPath(); ctx.moveTo(padL, yW(ftp)); ctx.lineTo(w - padR, yW(ftp)); ctx.stroke(); ctx.setLineDash([]);
  ctx.fillText("FTP", padL + 6, yW(ftp) - 5);
  // time labels
  const span = t1 - t0, tick = span > 5400 ? 1800 : span > 1800 ? 600 : span > 600 ? 300 : 60;
  for (let t = Math.ceil(t0 / tick) * tick; t <= t1; t += tick) ctx.fillText(fmtTime(t), x(t) - 12, h - 6);

  if (hasWk) {
    ctx.save(); ctx.beginPath(); ctx.rect(padL, padT, W, H); ctx.clip();
    drawBlocks(ctx, profile, x, yF, 0.55);
    ctx.fillStyle = "rgba(14,17,22,.45)"; // dim the completed part
    ctx.fillRect(padL, padT, x(now) - padL, H);
    ctx.restore();
  }
  // target line in free ride
  const pts = trace.filter((p) => tx(p) >= t0 - 1);
  if (!hasWk) line(ctx, pts, (p) => x(tx(p)), (p) => (p[3] == null ? null : yW(p[3])), css("--accent"), 2);
  // HR & power
  line(ctx, pts, (p) => x(tx(p)), (p) => (p[2] ? yHR(p[2]) : null), css("--hr"), 1.5);
  line(ctx, pts, (p) => x(tx(p)), (p) => (p[1] == null ? null : yW(p[1])), css("--power"), 1.6);
  // cursor
  if (STATE && (STATE.state !== "ready" || hasWk)) {
    ctx.strokeStyle = "#e6e9ee"; ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.moveTo(x(now), padT); ctx.lineTo(x(now), padT + H); ctx.stroke();
  }
  // HR axis
  ctx.fillStyle = css("--hr");
  for (const b of [80, 120, 160, 200]) ctx.fillText(b, w - padR + 8, yHR(b) + 4);
}

function line(ctx, pts, fx, fy, color, width) {
  ctx.strokeStyle = color; ctx.lineWidth = width; ctx.lineJoin = "round"; ctx.beginPath();
  let pen = false, lastX = null;
  for (const p of pts) {
    const yy = fy(p), xx = fx(p);
    if (yy == null || (lastX != null && xx - lastX > 20)) { pen = false; if (yy == null) continue; }
    pen ? ctx.lineTo(xx, yy) : ctx.moveTo(xx, yy); pen = true; lastX = xx;
  }
  ctx.stroke();
}

let traceBusy = null;
function refreshTrace() {
  if (!traceBusy) traceBusy = _refreshTrace().finally(() => (traceBusy = null));
  return traceBusy;
}
async function _refreshTrace() {
  try {
    const d = await api("GET", "/api/session?since=" + trace.length);
    const key = d.workout ? d.workout.id + ":" + d.workout.duration : "free";
    if (key !== profileKey) { profile = d.profile; profileKey = key; }
    if (d.recorded < trace.length) trace = [];
    trace.push(...d.trace);
  } catch { /* server restarting */ }
}

function renderRide(s) {
  STATE = s;
  const m = s.metrics || {};
  const set = (id, v) => { const el = $(id); if (el.textContent !== String(v)) el.textContent = v; };
  set("#m-power", m.power3s ?? "--");
  set("#m-target", s.target ?? (s.erg ? "--" : "RES"));
  set("#m-target-sub", s.target != null ? `W · ${Math.round((s.target / s.ftp) * 100)}% FTP` : s.erg ? "W" : `resistance · ${s.grade.toFixed(1)}%`);
  set("#m-hr", m.heart_rate != null ? Math.round(m.heart_rate) : "--");
  set("#m-cad", m.cadence != null ? Math.round(m.cadence) : "--");
  const seg = s.segment;
  set("#m-cad-sub", seg && seg.cadence ? `rpm · target ${seg.cadence}` : "rpm");
  $(".metric.cad").classList.toggle("warn", !!(seg && seg.cadence && m.cadence != null && Math.abs(m.cadence - seg.cadence) > 8));
  set("#m-int", seg ? fmtTime(seg.remaining) : "--:--");
  set("#m-int-sub", seg ? `remaining · step ${seg.index + 1}/${profile.length || "?"}` : "remaining");
  set("#m-elapsed", fmtTime(s.elapsed));
  set("#m-remaining", s.workout ? fmtTime(Math.max(0, s.workout.duration - s.workout_time)) : "--");
  set("#m-dist", (s.distance_km || 0).toFixed(1));
  set("#m-ftp", s.ftp);
  const st = $("#m-state");
  st.textContent = s.state === "paused" && s.paused_by === "auto" ? "paused · pedal to resume" : s.state;
  st.className = "state " + s.state;

  set("#ride-title", s.workout ? s.workout.name : "Free ride");
  set("#ride-mode", s.workout ? `Workout · ${fmtTime(s.workout.duration)}` : "Free ride · set your own target");

  // cues
  let cue = "", next = "";
  if (s.state === "finished") cue = "Ride saved. Pick another workout or start a free ride.";
  else if (s.workout_done) cue = "Workout complete! Keep spinning to cool down, then press End ride.";
  else if (seg) {
    cue = seg.text || (seg.free ? "Free ride: ride at your own effort" : "");
    if (seg.next) next = seg.next.free ? `Next: free ride ${fmtTime(seg.next.duration)}` :
      `Next: ${fmtTime(seg.next.duration)} @ ${Math.round(seg.next.start * s.ftp * s.intensity)}` +
      (seg.next.end !== seg.next.start ? `→${Math.round(seg.next.end * s.ftp * s.intensity)}` : "") + " W";
    if (seg.remaining <= 10 && seg.next) cue = `Get ready… ${seg.remaining}`;
  } else if (s.state === "ready") cue = noTrainer() ? "Connect your trainer in Settings, or turn on the simulator." : "Press Start or just start pedaling.";
  set("#cue-text", cue || " ");
  set("#cue-next", next);

  // controls
  const btn = $("#btn-start");
  btn.textContent = s.state === "running" ? "Pause" : s.state === "paused" ? "Resume" : "Start";
  btn.disabled = s.state === "finished";
  set("#v-intensity", Math.round(s.intensity * 100) + "%");
  set("#v-watts", s.manual_watts + " W");
  set("#v-grade", s.grade.toFixed(1) + "%");
  $("#btn-erg").classList.toggle("on", s.erg);
  $("#btn-res").classList.toggle("on", !s.erg);
  $("#grp-intensity").hidden = !s.workout;
  $("#grp-watts").hidden = !!s.workout || !s.erg;
  $("#grp-grade").hidden = s.erg && !(seg && seg.free);
  $("#btn-skip").hidden = $("#btn-extend").hidden = !s.workout;
  $("#btn-end").disabled = !s.recorded || s.state === "finished";

  if (s.recorded !== trace.length || (s.workout ? s.workout.id + ":" + s.workout.duration : "free") !== profileKey) refreshTrace().then(drawRide);
  else drawRide();
}

let DEVICES = {};
const noTrainer = () => !DEVICES.trainer || DEVICES.trainer.state !== "connected";
function renderPills(devs) {
  DEVICES = devs;
  const label = { trainer: "Trainer", hr: "HR", cadence: "Cadence" };
  const sim = devs.trainer && devs.trainer.kind === "sim";
  const html = Object.keys(label).filter((r) => (r !== "cadence" || devs[r]) && !(sim && r === "hr")).map((r) => {
    const d = devs[r];
    const cls = d ? d.state : "";
    const name = d ? (d.kind === "sim" ? "Simulator" : label[r]) : label[r];
    return `<span class="pill ${cls}" title="${esc(d ? d.name + " · " + d.state + (d.error ? " · " + d.error : "") : "not paired")}">${name}</span>`;
  }).join("");
  if ($("#pills").innerHTML !== html) $("#pills").innerHTML = html;
  if ($("#tab-settings").classList.contains("active")) renderCurrentDevices();
}

const act = (a, body) => api("POST", "/api/session/" + a, body).then((s) => s && s.state && renderRide({ ...STATE, ...s })).catch((e) => toast(e.message, true));
$("#btn-start").onclick = () => act(STATE && STATE.state === "running" ? "pause" : "start");
$("#btn-skip").onclick = () => act("skip");
$("#btn-extend").onclick = () => act("extend", { seconds: 60 });
$("#btn-erg").onclick = () => act("erg", { enabled: true });
$("#btn-res").onclick = () => act("erg", { enabled: false });
$$("[data-int]").forEach((b) => (b.onclick = () => act("intensity", { delta: +b.dataset.int })));
$$("[data-watts]").forEach((b) => (b.onclick = () => act("watts", { watts: (STATE ? STATE.manual_watts : 150) + +b.dataset.watts })));
$$("[data-grade]").forEach((b) => (b.onclick = () => act("grade", { grade: (STATE ? STATE.grade : 0) + +b.dataset.grade })));
$("#btn-pick").onclick = () => (location.hash = "#workouts");
$("#btn-free").onclick = () => loadSession(null);

document.addEventListener("keydown", (e) => {
  if (!$("#tab-ride").classList.contains("active") || e.target.matches("input,textarea,select")) return;
  if (e.code === "Space") { e.preventDefault(); $("#btn-start").click(); }
  if (e.key === "ArrowUp") { e.preventDefault(); STATE && STATE.workout ? act("intensity", { delta: 0.01 }) : $("[data-watts='5']").click(); }
  if (e.key === "ArrowDown") { e.preventDefault(); STATE && STATE.workout ? act("intensity", { delta: -0.01 }) : $("[data-watts='-5']").click(); }
});

async function loadSession(workoutId) {
  try {
    await api("POST", "/api/session/load", { workout_id: workoutId });
  } catch (e) {
    if (!confirm(e.message + "\n\nDiscard the current ride and load this one?")) return;
    await api("POST", "/api/session/load", { workout_id: workoutId, force: true });
  }
  trace = []; profileKey = null;
  await refreshTrace();
  location.hash = "#ride";
}

/* end ride dialog */
$("#btn-end").onclick = async () => {
  if (STATE.state === "running") await act("pause");
  const s = await api("POST", "/api/session/summary");
  $("#end-summary").innerHTML = [
    ["Time", fmtTime(s.duration)], ["Avg power", s.avg_power + " W"], ["NP", s.np + " W"],
    ["TSS", s.tss], ["IF", s.if], ["Avg HR", s.avg_hr ?? "--"], ["Work", s.kj + " kJ"], ["Distance", s.distance_km + " km"], ["Avg cadence", s.avg_cadence ?? "--"],
  ].map(([k, v]) => `<div>${k}<b>${v}</b></div>`).join("");
  $("#end-name").value = STATE.workout ? STATE.workout.name : "Indoor ride";
  $("#dlg-end").showModal();
};
$("#end-resume").onclick = () => { $("#dlg-end").close(); act("start"); };
$("#end-discard").onclick = async () => {
  if (!confirm("Throw away this ride? This can't be undone.")) return;
  $("#dlg-end").close(); await act("discard"); trace = []; drawRide();
};
$("#end-save").onclick = async () => {
  const btn = $("#end-save"); btn.disabled = true;
  try {
    const rec = await api("POST", "/api/session/save", { name: $("#end-name").value });
    $("#dlg-end").close();
    toast(`Saved "${rec.name}"` + (SETTINGS.auto_upload_garmin ? ". Uploading to Garmin…" : ""));
    location.hash = "#activities";
  } catch (e) { toast(e.message, true); } finally { btn.disabled = false; }
};

/* ================================================================= websocket */
function connectWS() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "state") { renderPills(msg.devices); renderRide(msg.session); }
  };
  ws.onclose = () => setTimeout(connectWS, 1500);
}

/* ================================================================= workouts */
let LIB = [];
let ED = null; // workout being edited

async function loadLibrary() {
  LIB = await api("GET", "/api/workouts");
  renderLibrary();
  if (!ED && LIB.length) selectWorkout(LIB[0].id);
  else if (!ED) newWorkout();
}

function renderLibrary() {
  const q = $("#lib-filter").value.toLowerCase();
  const list = $("#lib-list");
  list.innerHTML = "";
  for (const w of LIB.filter((w) => w.name.toLowerCase().includes(q))) {
    const el = document.createElement("div");
    el.className = "wk" + (ED && ED.id === w.id ? " sel" : "");
    el.innerHTML = `<div class="wk-name">${esc(w.name)}</div><div class="wk-meta">${fmtTime(w.duration)} · TSS ${w.tss} · IF ${w.if.toFixed(2)}</div><canvas></canvas>`;
    el.onclick = () => selectWorkout(w.id);
    el.ondblclick = () => loadSession(w.id);
    list.appendChild(el);
    requestAnimationFrame(() => drawMini($("canvas", el), w.segments));
  }
}
$("#lib-filter").oninput = renderLibrary;

async function selectWorkout(id) {
  ED = await api("GET", "/api/workouts/" + encodeURIComponent(id));
  renderEditor(); renderLibrary();
}

function newWorkout() {
  ED = { id: "", name: "New workout", description: "", segments: [
    { duration: 600, start: 0.45, end: 0.7, cadence: null, text: "Warm up", free: false },
    { duration: 1200, start: 0.75, end: 0.75, cadence: null, text: "", free: false },
    { duration: 480, start: 0.6, end: 0.4, cadence: null, text: "Cool down", free: false },
  ] };
  renderEditor(); renderLibrary();
}
$("#btn-new").onclick = newWorkout;

function renderEditor() {
  if (!ED) return;
  $("#ed-name").value = ED.name;
  $("#ed-desc").value = ED.description || "";
  $("#ed-delete").hidden = !ED.id;
  const ftp = SETTINGS.ftp || 200;
  const body = $("#seg-body");
  body.innerHTML = "";
  ED.segments.forEach((s, i) => {
    const tr = document.createElement("tr");
    const watts = s.free ? "free" : s.start === s.end ? `${Math.round(s.start * ftp)} W` : `${Math.round(s.start * ftp)}→${Math.round(s.end * ftp)} W`;
    tr.innerHTML = `
      <td class="zone"><div style="background:${s.free ? "#475569" : zoneColor((s.start + s.end) / 2)}"></div></td>
      <td class="num"><input data-k="duration" value="${fmtTime(s.duration)}"></td>
      <td class="num"><input data-k="start" type="number" value="${s.free ? "" : Math.round(s.start * 100)}" ${s.free ? "disabled placeholder='free'" : ""}></td>
      <td class="num"><input data-k="end" type="number" value="${s.free ? "" : Math.round(s.end * 100)}" ${s.free ? "disabled" : ""}></td>
      <td class="watts">${watts}</td>
      <td class="num"><input data-k="cadence" type="number" value="${s.cadence ?? ""}" placeholder="any"></td>
      <td><input data-k="text" value="${esc(s.text)}" placeholder=""></td>
      <td class="seg-act"><button data-a="up" title="Move up">↑</button><button data-a="down" title="Move down">↓</button><button data-a="dup" title="Duplicate">⧉</button><button data-a="del" title="Remove">✕</button></td>`;
    $$("input", tr).forEach((inp) => (inp.onchange = () => {
      const k = inp.dataset.k;
      if (k === "duration") s.duration = Math.max(1, parseTime(inp.value));
      else if (k === "start") { const old = s.start; s.start = (+inp.value || 0) / 100; if (s.end === old) s.end = s.start; }
      else if (k === "end") s.end = (+inp.value || 0) / 100;
      else if (k === "cadence") s.cadence = inp.value ? +inp.value : null;
      else s.text = inp.value;
      renderEditor();
    }));
    $$("button", tr).forEach((b) => (b.onclick = () => {
      const a = b.dataset.a, segs = ED.segments;
      if (a === "del") segs.splice(i, 1);
      if (a === "dup") segs.splice(i + 1, 0, { ...s });
      if (a === "up" && i > 0) [segs[i - 1], segs[i]] = [segs[i], segs[i - 1]];
      if (a === "down" && i < segs.length - 1) [segs[i + 1], segs[i]] = [segs[i], segs[i + 1]];
      renderEditor();
    }));
    body.appendChild(tr);
  });
  const total = ED.segments.reduce((a, s) => a + s.duration, 0);
  const { tss, IF } = estimate(ED.segments);
  $("#ed-stats").innerHTML = `<span>Duration <b>${fmtTime(total)}</b></span><span>Est. TSS <b>${tss}</b></span><span>IF <b>${IF.toFixed(2)}</b></span><span>${esc(ED.source || "")}</span>`;
  drawEditor();
}

function estimate(segs) {
  const vals = [];
  for (const s of segs) for (let k = 0; k < s.duration; k++) vals.push(s.free ? 0.5 : s.start + (s.end - s.start) * (k / s.duration));
  if (!vals.length) return { tss: 0, IF: 0 };
  const roll = []; let acc = 0;
  vals.forEach((v, i) => { acc += v; if (i >= 30) acc -= vals[i - 30]; if (i >= 29) roll.push(acc / 30); });
  const r = roll.length ? roll : vals;
  const np = Math.pow(r.reduce((a, v) => a + v ** 4, 0) / r.length, 0.25);
  return { IF: np, tss: Math.round((vals.length / 3600) * np * np * 100) };
}

function drawEditor() {
  const cv = $("#ed-chart");
  if (!ED || !cv.clientWidth) return;
  const { ctx, w, h } = setupCanvas(cv);
  const total = ED.segments.reduce((a, s) => a + s.duration, 0) || 1;
  const top = Math.max(1.25, ...ED.segments.map((s) => Math.max(s.start, s.end))) * 1.08;
  const y = (f) => h - 18 - (f / top) * (h - 26);
  ctx.strokeStyle = "#475569"; ctx.setLineDash([4, 4]);
  ctx.beginPath(); ctx.moveTo(0, y(1)); ctx.lineTo(w, y(1)); ctx.stroke(); ctx.setLineDash([]);
  drawBlocks(ctx, ED.segments, (t) => 8 + (t / total) * (w - 16), y, 0.9);
  ctx.fillStyle = css("--muted"); ctx.font = "11px Segoe UI, system-ui";
  ctx.fillText("100% FTP", 10, y(1) - 4);
}

$("#ed-name").onchange = () => (ED.name = $("#ed-name").value);
$("#ed-desc").onchange = () => (ED.description = $("#ed-desc").value);

$$("[data-add]").forEach((b) => (b.onclick = () => {
  const last = ED.segments[ED.segments.length - 1];
  const lvl = last && !last.free ? last.end : 0.5;
  const add = (seg) => { ED.segments.push({ cadence: null, text: "", free: false, ...seg }); renderEditor(); };
  switch (b.dataset.add) {
    case "warmup": return add({ duration: 600, start: 0.45, end: 0.7, text: "Warm up" });
    case "steady": return add({ duration: 300, start: Math.max(0.5, lvl), end: Math.max(0.5, lvl) });
    case "cooldown": return add({ duration: 480, start: 0.6, end: 0.4, text: "Cool down" });
    case "free": return add({ duration: 600, start: 0.5, end: 0.5, free: true, text: "Free ride" });
    case "intervals": $("#iv-form").hidden = !$("#iv-form").hidden;
  }
}));
$("#iv-add").onclick = () => {
  const n = Math.max(1, +$("#iv-n").value || 1), on = parseTime($("#iv-on").value), off = parseTime($("#iv-off").value);
  const onp = (+$("#iv-onp").value || 100) / 100, offp = (+$("#iv-offp").value || 50) / 100;
  for (let k = 0; k < n; k++) {
    ED.segments.push({ duration: on, start: onp, end: onp, cadence: null, text: `Interval ${k + 1}/${n}`, free: false });
    if (off) ED.segments.push({ duration: off, start: offp, end: offp, cadence: null, text: "Recover", free: false });
  }
  $("#iv-form").hidden = true; renderEditor();
};

async function saveEditor() {
  ED.name = $("#ed-name").value || "Untitled";
  ED.description = $("#ed-desc").value;
  const saved = await api("POST", "/api/workouts", ED);
  ED.id = saved.id;
  LIB = await api("GET", "/api/workouts");
  renderLibrary(); renderEditor();
  return saved.id;
}
$("#ed-save").onclick = () => saveEditor().then(() => toast("Workout saved")).catch((e) => toast(e.message, true));
$("#ed-ride").onclick = async () => { try { const id = await saveEditor(); await loadSession(id); } catch (e) { toast(e.message, true); } };
$("#ed-delete").onclick = async () => {
  if (!ED.id || !confirm(`Delete "${ED.name}"?`)) return;
  await api("DELETE", "/api/workouts/" + encodeURIComponent(ED.id));
  ED = null; loadLibrary();
};
$("#ed-export").onchange = async (e) => {
  const fmt = e.target.value; e.target.value = "";
  if (!fmt) return;
  if (!ED.id) await saveEditor();
  location.href = `/api/workouts/${encodeURIComponent(ED.id)}/export?fmt=${fmt}`;
};
$("#file-import").onchange = async (e) => {
  const f = e.target.files[0]; e.target.value = "";
  if (!f) return;
  const fd = new FormData(); fd.append("file", f);
  try {
    const w = await api("POST", "/api/workouts/import", fd);
    toast(`Imported "${w.name}"`);
    LIB = await api("GET", "/api/workouts");
    selectWorkout(w.id);
  } catch (err) { toast(err.message, true); }
};

/* ================================================================= activities */
let actPoll = null;
async function loadActivities() {
  const list = await api("GET", "/api/activities");
  const box = $("#act-list");
  box.innerHTML = list.length ? "" : `<p class="hint">No rides yet. Your finished rides will show up here.</p>`;
  let busy = false;
  for (const a of list) {
    const s = a.summary, el = document.createElement("div");
    el.className = "act";
    const garminBtn = (() => {
      const up = (a.uploads || {}).garmin, job = a.job;
      if (job === "uploading") { busy = true; return `<button class="small" disabled>Garmin: uploading…</button>`; }
      if (up && up.url) return `<a class="done" href="${esc(up.url)}" target="_blank" rel="noopener">✓ Garmin</a>`;
      const err = job && job.startsWith("error") ? `<span class="err">${esc(job)}</span>` : "";
      return `<button class="small" data-up>↑ Garmin Connect</button>${err}`;
    })();
    el.innerHTML = `
      <div><div class="act-title">${esc(a.name)}</div><div class="act-date">${new Date(a.start).toLocaleString()}</div><canvas></canvas></div>
      <div class="act-stats">
        <span>Time<b>${fmtTime(s.duration)}</b></span><span>Avg / NP<b>${s.avg_power} / ${s.np} W</b></span>
        <span>TSS<b>${s.tss}</b></span><span>IF<b>${s.if}</b></span><span>Avg HR<b>${s.avg_hr ?? "--"}</b></span><span>Work<b>${s.kj} kJ</b></span>
      </div>
      <div class="act-actions">${garminBtn}
        <a href="/api/activities/${a.id}/fit"><button class="small ghost">.FIT</button></a>
        <button class="small ghost" data-tpl>Save as workout</button>
        <button class="small ghost danger-text" data-del>Delete</button></div>`;
    $$("[data-up]", el).forEach((b) => (b.onclick = async () => {
      await api("POST", `/api/activities/${a.id}/upload`); loadActivities();
    }));
    $("[data-tpl]", el).onclick = async () => {
      try { const w = await api("POST", `/api/activities/${a.id}/as_workout`); toast(`Created workout "${w.name}"`); ED = null; location.hash = "#workouts"; setTimeout(() => selectWorkout(w.id), 300); }
      catch (e) { toast(e.message, true); }
    };
    $("[data-del]", el).onclick = async () => {
      if (!confirm("Delete this ride and its FIT file?")) return;
      await api("DELETE", `/api/activities/${a.id}`); loadActivities();
    };
    box.appendChild(el);
    requestAnimationFrame(() => {
      const { ctx, w, h } = setupCanvas($("canvas", el));
      const pw = a.power_trace || [], mx = Math.max(1, ...pw) * 1.05;
      ctx.fillStyle = "rgba(250,204,21,.25)"; ctx.strokeStyle = css("--power"); ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(0, h);
      pw.forEach((p, i) => ctx.lineTo((i / Math.max(1, pw.length - 1)) * w, h - (p / mx) * h));
      ctx.lineTo(w, h); ctx.closePath(); ctx.fill(); ctx.stroke();
    });
  }
  clearTimeout(actPoll);
  if (busy && $("#tab-activities").classList.contains("active")) actPoll = setTimeout(loadActivities, 2500);
}

/* ================================================================= settings */
async function loadSettings() {
  const d = await api("GET", "/api/settings");
  SETTINGS = d.settings;
  $("#s-ftp").value = SETTINGS.ftp;
  $("#s-weight").value = SETTINGS.weight_kg;
  $("#s-autopause").checked = !!SETTINGS.auto_pause;
  $("#s-auto-garmin").checked = !!SETTINGS.auto_upload_garmin;
  $("#s-sim").checked = !!SETTINGS.simulate;
  $("#data-dir").textContent = d.data_dir + "\\activities";
  const g = d.garmin;
  $("#garmin-status").innerHTML = g.connected ? `<span class="ok">● Signed in</span> ${esc(g.email)}` : `<span class="no">○ Not signed in</span>`;
  $("#garmin-form").hidden = g.connected || g.awaiting_mfa;
  $("#garmin-mfa").hidden = !g.awaiting_mfa;
  $("#gc-logout").hidden = !g.connected;
  if (g.email && !$("#gc-email").value) $("#gc-email").value = g.email;
  renderCurrentDevices();
}
const saveSetting = (k, v) => api("PUT", "/api/settings", { [k]: v }).then((s) => { SETTINGS = s; toast("Saved"); }).catch((e) => toast(e.message, true));
$("#s-ftp").onchange = (e) => saveSetting("ftp", +e.target.value);
$("#s-weight").onchange = (e) => saveSetting("weight_kg", +e.target.value);
$("#s-autopause").onchange = (e) => saveSetting("auto_pause", e.target.checked);
$("#s-auto-garmin").onchange = (e) => saveSetting("auto_upload_garmin", e.target.checked);
$("#s-sim").onchange = async (e) => { await api("POST", "/api/devices/simulate", { enabled: e.target.checked }); SETTINGS.simulate = e.target.checked; toast(e.target.checked ? "Simulator on" : "Simulator off"); };

function renderCurrentDevices() {
  const box = $("#dev-current");
  const roles = { trainer: "Trainer", hr: "Heart rate", cadence: "Cadence" };
  const html = Object.entries(DEVICES).map(([r, d]) => `
    <div class="dev-row"><span class="pill ${d.state}">${roles[r]}</span><span class="name">${esc(d.name)}</span>
    <span class="meta">${esc(d.state)}${d.info ? " · " + esc(d.info) : ""}${d.error ? " · " + esc(d.error) : ""}</span>
    ${d.kind === "sim" ? "" : `<button class="small ghost" data-forget="${r}">Forget</button>`}</div>`).join("") ||
    `<p class="hint">No sensors paired yet.</p>`;
  if (box.innerHTML !== html) {
    box.innerHTML = html;
    $$("[data-forget]", box).forEach((b) => (b.onclick = () => api("POST", "/api/devices/disconnect", { role: b.dataset.forget, forget: true })));
  }
}

$("#btn-scan").onclick = async () => {
  const btn = $("#btn-scan"); btn.disabled = true; btn.textContent = "Scanning…";
  const box = $("#scan-results"); box.innerHTML = "";
  try {
    const { devices } = await api("POST", "/api/devices/scan");
    if (!devices.length) box.innerHTML = `<p class="hint">Nothing found. Spin the pedals to wake the trainer and try again.</p>`;
    for (const d of devices) {
      const row = document.createElement("div");
      row.className = "dev-row";
      row.innerHTML = `<span class="name">${esc(d.name)}</span><span>${d.tags.map((t) => `<span class="tag">${t}</span>`).join("")}</span>
        <span class="meta">${d.rssi ?? ""} dBm</span>
        <select><option value="trainer">as Trainer</option><option value="hr">as Heart rate</option><option value="cadence">as Cadence</option></select>
        <button class="small primary">Connect</button>`;
      if (d.suggested_role) $("select", row).value = d.suggested_role;
      $("button", row).onclick = async () => {
        await api("POST", "/api/devices/connect", { address: d.address, name: d.name, role: $("select", row).value });
        $("#s-sim").checked = false; row.remove(); toast("Connecting to " + d.name);
      };
      box.appendChild(row);
    }
  } catch (e) { toast(e.message, true); }
  btn.disabled = false; btn.textContent = "Scan for devices";
};

$("#gc-login").onclick = async () => {
  const b = $("#gc-login"); b.disabled = true; b.textContent = "Signing in…";
  try {
    const r = await api("POST", "/api/garmin/login", { email: $("#gc-email").value, password: $("#gc-pass").value });
    $("#gc-pass").value = "";
    if (r.mfa_required) toast("Enter the code Garmin sent you"); else toast("Signed in to Garmin Connect");
    loadSettings();
  } catch (e) { toast(e.message, true); }
  b.disabled = false; b.textContent = "Sign in";
};
$("#gc-mfa").onclick = async () => {
  try { await api("POST", "/api/garmin/mfa", { code: $("#gc-code").value }); toast("Signed in to Garmin Connect"); loadSettings(); }
  catch (e) { toast(e.message, true); }
};
$("#gc-logout").onclick = async () => { await api("POST", "/api/garmin/logout"); loadSettings(); };

/* ================================================================= boot */
window.addEventListener("resize", () => { drawRide(); drawEditor(); });
(async () => {
  try { const d = await api("GET", "/api/settings"); SETTINGS = d.settings; $("#data-dir").textContent = d.data_dir + "\\activities"; } catch {}
  showTab(location.hash.slice(1) || "ride");
  await refreshTrace();
  connectWS();
})();
