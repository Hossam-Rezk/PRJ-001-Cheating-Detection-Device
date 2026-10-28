// ExamGuard Rover dashboard
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pct = (v, d = 1) => (v === null || v === undefined ? "–" : (v * 100).toFixed(d) + "%");
const fmtTime = (t) => new Date(t * 1000).toLocaleTimeString();
const fmtDate = (t) => new Date(t * 1000).toLocaleString();

async function api(path, method = "GET", body) {
  const r = await fetch(path, { method, headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || r.statusText);
  return data;
}
function toast(text, level = "") {
  const el = document.createElement("div");
  el.className = "toast " + level; el.textContent = text;
  $("#toasts").appendChild(el); setTimeout(() => el.remove(), 4500);
}
const guard = (fn) => async (...a) => { try { await fn(...a); } catch (e) { toast(e.message, "error"); } };

const S = { settings: {}, model: null, calibration: null, history: [], alerts: [], map: null, last: null, meta: {}, connected: false };

// ---------------------------------------------------------------- websocket
let ws;
function connect() {
  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onmessage = (ev) => onMessage(JSON.parse(ev.data));
  ws.onclose = () => { setConn(false, "Server offline"); setTimeout(connect, 1500); };
}
function send(obj) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); }

function onMessage(m) {
  switch (m.type) {
    case "hello": applyState(m.state); break;
    case "frame": onFrame(m); break;
    case "map": S.map = m; if (activeTab === "monitor") drawMap(); break;
    case "alert":
      upsertAlert(m.alert);
      if (m.event === "raised") {
        send({ type: "alert_ack", id: m.alert.id });
        if ($("#browserSound").checked) beep();
        toast(`Possible device ${alertRange(m.alert)}`, "error");
      }
      renderAlerts(); break;
    case "calibration":
      S.calibration = m.record; renderCalib(); toast("Calibration complete", "ok"); break;
    case "train_done": S.model = m.meta; renderModelPill(); toast(`New model active: ${pct(m.meta.test_accuracy)} test accuracy`, "ok"); break;
    case "toast": toast(m.text, m.level); if (/connected/i.test(m.text)) setConn(!/dis/i.test(m.text)); break;
  }
}

function applyState(st) {
  S.settings = st.settings; S.model = st.model; S.calibration = st.calibration;
  S.alerts = st.alerts || []; S.ignored = st.ignored_macs || [];
  S.history = (st.history || []).map(rowToHist);
  setConn(st.connected);
  renderAlerts(); renderModelPill(); renderSettings(); renderCalib();
}
const rowToHist = (r) => ({ t: r[0], hall: r[1], dev: r[2], dist: r[3], score: r[4], ml: r[5], ble: r[6], rule: r[7] });

function setConn(ok, text) {
  S.connected = ok;
  const p = $("#connPill");
  p.textContent = text || (ok ? "Car connected" : "Car offline");
  p.className = "pill " + (ok ? "ok" : "bad");
}
function renderModelPill() {
  const p = $("#modelPill");
  if (!S.model) { p.textContent = "No model · threshold only"; p.className = "pill warn"; return; }
  p.textContent = `${S.model.algorithm} · ${pct(S.model.test_accuracy)}`;
  p.className = "pill " + (S.model.meets_requirement ? "ok" : "warn");
}

// ---------------------------------------------------------------- live
let renderQueued = false;
function onFrame(f) {
  S.last = f;
  if (!S.connected) setConn(true);
  S.history.push({ t: f.t, hall: f.hall_mean, dev: f.hall_dev, dist: f.distance_cm, score: f.score,
    ml: f.components.ml, ble: f.components.ble, rule: f.components.rule });
  const cut = f.t - 65;
  while (S.history.length && S.history[0].t < cut) S.history.shift();
  if (!renderQueued) { renderQueued = true; setTimeout(renderLive, 80); }
}

function renderLive() {
  renderQueued = false;
  const f = S.last; if (!f) return;
  const s = S.settings;
  $("#ratePill").textContent = f.rate_hz.toFixed(1) + " Hz";
  $("#ratePill").className = "pill " + (f.rate_hz >= 1 ? "ok" : "bad");
  $("#motorState").textContent = { F: "Forward", B: "Reverse", L: "Left", R: "Right", S: "Stopped" }[f.motor] || f.motor;
  if (f.motor !== "S" && f.speed) $("#motorState").textContent += ` ${f.speed}%`;
  $("#fwVal").textContent = `firmware ${f.fw}`;
  const audio = f.devices.filter((d) => d.audio).length;
  $("#carBle").textContent = f.devices.length;
  $("#carBleAudio").textContent = audio ? `${audio} audio device${audio > 1 ? "s" : ""}` : "no audio devices";
  $("#carLed").textContent = "LED: " + (f.alert && s.server_alert_led ? "red (alert)" : f.calibrating ? "yellow (calibrating)" : "green");
  $("#distState").textContent = (f.distance_cm ?? "–") + " cm";
  $("#blockedMsg").classList.toggle("hidden", !f.blocked);

  // status
  const alerting = !!f.alert;
  const card = $("#statusCard");
  card.classList.toggle("alerting", alerting); card.classList.toggle("clear", !alerting);
  $("#statusText").textContent = f.calibrating ? "Calibrating…" : alerting ? `Possible device ${alertRange(f.alert)}` : "Clear";
  $("#thrVal").textContent = s.alert_threshold;
  $("#scoreBar").style.width = (f.score * 100).toFixed(1) + "%";
  $("#thrMark").style.left = (s.alert_threshold * 100) + "%";
  $("#scoreVal").textContent = f.score.toFixed(2);
  $("#zoneVal").textContent = f.where;

  // magnetic + evidence
  const thr = S.calibration ? S.calibration.threshold : 500;
  $("#hallDev").textContent = Math.round(f.hall_dev);
  $("#hallThr").textContent = Math.round(thr);
  for (const [k, id] of [["ml", "Ml"], ["ble", "Ble"], ["rule", "Rule"]]) {
    const v = f.components[k];
    $("#c" + id + "V").textContent = v === null ? "off" : v.toFixed(2);
    $("#chip" + id).classList.toggle("on", v !== null && v >= 0.5);
  }

  // charts
  const now = f.t, xs = { xmin: now - 60, xmax: now, xfmt: (v) => Math.round(v - now) + "s" };
  const H = S.history;
  lineChart($("#hallChart"), [{ data: H.map((p) => [p.t, p.dev]), color: css("--accent") }],
    { ...xs, minSpan: thr * 3, hlines: [{ y: thr, color: css("--bad"), label: "+threshold" }, { y: -thr, color: css("--bad") }, { y: 0, color: css("--muted"), dash: [2, 4] }] });
  lineChart($("#scoreChart"), [
    { data: H.map((p) => [p.t, p.rule]), color: css("--rule"), width: 1 },
    { data: H.map((p) => [p.t, p.ble]), color: css("--ble"), width: 1 },
    { data: H.map((p) => [p.t, p.ml]), color: css("--ml"), width: 1 },
    { data: H.map((p) => [p.t, p.score]), color: css("--text"), width: 2.2 },
  ], { ...xs, ymin: 0, ymax: 1.02, hlines: [{ y: s.alert_threshold, color: css("--bad"), label: "alert" }] });

  // BLE table
  const ignored = new Set(S.ignored || []);
  $("#bleCount").textContent = `${f.devices.length} devices` + (f.ignored_count ? ` · ${f.ignored_count} ignored` : "");
  $("#bleTable").innerHTML = f.devices.map((d) => `
    <tr class="${d.audio ? "audio" : ""}">
      <td><span class="tag ${d.kind}">${esc(d.kind)}</span></td>
      <td>${esc(d.model || d.name)}<div class="muted" style="font-size:11px">${esc(d.mac)}</div></td>
      <td><span class="rssi"><i style="width:${Math.max(0, Math.min(100, (d.rssi + 100) * 1.6))}%"></i></span>${d.rssi}</td>
      <td>${d.est_m} m</td>
      <td><button class="ghost sm" data-ignore="${esc(d.mac)}">${ignored.has(d.mac) ? "Unignore" : "Ignore"}</button></td>
    </tr>`).join("") || `<tr><td colspan="5" class="muted">No devices heard</td></tr>`;

  // recording / trial runs come from the command-line tool; show that they're on
  const act = f.recording ? `● Recording ${f.recording.label} · ${f.recording.frames}` : f.trial ? `● Trial #${f.trial.id}` : "";
  $("#activityPill").textContent = act; $("#activityPill").classList.toggle("hidden", !act);
}

$("#bleTable").addEventListener("click", guard(async (e) => {
  const mac = e.target.dataset.ignore; if (!mac) return;
  const ignore = e.target.textContent === "Ignore";
  const r = await api("/api/ble/ignore", "POST", { mac, ignore });
  S.ignored = r.ignored; renderIgnored();
}));

// ---------------------------------------------------------------- alerts
function upsertAlert(a) {
  const i = S.alerts.findIndex((x) => x.id === a.id);
  if (i >= 0) S.alerts[i] = a; else S.alerts.unshift(a);
}
function renderAlerts() {
  $("#alertList").innerHTML = S.alerts.slice(0, 50).map((a) => {
    const ev = [];
    if (a.ml_label) ev.push(`ML: ${a.ml_label} (${pct(a.ml_p_target, 0)})`);
    if (a.ble_device) ev.push(`BLE: ${a.ble_device.model} ${a.ble_device.rssi} dBm ≈${a.ble_device.est_distance_m} m`);
    ev.push(`Δ field ${a.hall_dev}`);
    const lat = a.display_latency_ms ?? a.detect_latency_ms;
    return `<div class="alert-item ${a.status}">
      <div class="top"><b>${esc(alertRange(a))}</b><span class="muted small">${fmtTime(a.t_alert)} · ${a.status}${a.reopened ? ` · re-raised ${a.reopened}×` : ""}</span></div>
      ${a.where ? `<div class="ev">Car was ${esc(a.where)}</div>` : ""}
      <div class="ev">Peak ${a.peak_score.toFixed(2)} · ${ev.map(esc).join(" · ")} · ${lat ? Math.round(lat) + " ms" : ""}</div>
      <div class="acts">
        ${a.feedback ? `<span class="tag ${a.feedback === "confirmed" ? "earphones" : "clear"}">${a.feedback.replace("_", " ")}</span>` : ""}
        <button class="ghost sm" data-fb="confirmed" data-id="${a.id}">Device found</button>
        <button class="ghost sm" data-fb="false_positive" data-id="${a.id}">False alarm</button>
      </div></div>`;
  }).join("") || `<div class="empty">No alerts yet. Drive the car along the desks.</div>`;
}
$("#alertList").addEventListener("click", guard(async (e) => {
  const v = e.target.dataset.fb; if (!v) return;
  upsertAlert(await api(`/api/alerts/${e.target.dataset.id}/feedback`, "POST", { verdict: v }));
  renderAlerts();
}));
$("#clearAlerts").onclick = guard(async () => { await api("/api/alerts", "DELETE"); S.alerts = []; renderAlerts(); });

// older alerts (before distance reporting) carry a seat "zone" instead
const alertRange = (a) => a.range_text || (a.zone ? `near ${a.zone}` : "");

let audioCtx;
function beep() {
  try {
    audioCtx = audioCtx || new AudioContext();
    const o = audioCtx.createOscillator(), g = audioCtx.createGain();
    o.frequency.value = 880; g.gain.value = 0.08; o.connect(g); g.connect(audioCtx.destination);
    o.start(); o.stop(audioCtx.currentTime + 0.25);
  } catch (_) {}
}

// ---------------------------------------------------------------- driving
let held = null, driveTimer = null;
const speed = () => +$("#speed").value;
$("#speed").oninput = () => ($("#speedVal").textContent = speed() + "%");
function hold(cmd) {
  if (cmd === "S") return release();
  held = cmd; send({ type: "drive", cmd, speed: speed() });
  $$(".pad button").forEach((b) => b.classList.toggle("held", b.dataset.cmd === cmd));
  clearInterval(driveTimer);
  driveTimer = setInterval(() => held && send({ type: "drive", cmd: held, speed: speed() }), 120);
}
function release() {
  held = null; clearInterval(driveTimer); send({ type: "drive", cmd: "S", speed: 0 });
  $$(".pad button").forEach((b) => b.classList.remove("held"));
}
$$(".pad button").forEach((b) => {
  b.addEventListener("pointerdown", (e) => { e.preventDefault(); b.setPointerCapture(e.pointerId); hold(b.dataset.cmd); });
  b.addEventListener("pointerup", release);
  b.addEventListener("pointercancel", release);
});
const KEYS = { w: "F", arrowup: "F", s: "B", arrowdown: "B", a: "L", arrowleft: "L", d: "R", arrowright: "R", " ": "S" };
document.addEventListener("keydown", (e) => {
  if (/INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName)) return;
  const c = KEYS[e.key.toLowerCase()]; if (!c) return;
  e.preventDefault(); if (held !== c) hold(c);
});
document.addEventListener("keyup", (e) => { if (KEYS[e.key.toLowerCase()] && KEYS[e.key.toLowerCase()] === held) release(); });
window.addEventListener("blur", () => held && release());

const activeTab = "monitor"; // single page

// ---------------------------------------------------------------- map
// Metres from where the car started: x = ahead, y = left. The view fits
// whatever has been driven, so it works for any room size.
let mapGeom = null;
const loadMap = guard(async () => { S.map = await api("/api/map"); drawMap(); });
function drawMap() {
  const m = S.map; if (!m) return;
  const { ctx, w, h } = setupCanvas($("#mapCanvas"));
  const [px, py, th] = (S.last && S.last.pose) || m.pose;
  const pts = [[0, 0], [px, py], ...m.trail, ...m.heat.map(([i, j]) => [(i + 0.5) * m.cell, (j + 0.5) * m.cell])];
  for (const e of m.estimates) pts.push([e.x - e.radius_m, e.y - e.radius_m], [e.x + e.radius_m, e.y + e.radius_m]);
  let x0 = Math.min(...pts.map((p) => p[0])) - 0.5, x1 = Math.max(...pts.map((p) => p[0])) + 0.5;
  let y0 = Math.min(...pts.map((p) => p[1])) - 0.5, y1 = Math.max(...pts.map((p) => p[1])) + 0.5;
  const grow = (lo, hi) => { const d = Math.max(0, 3 - (hi - lo)) / 2; return [lo - d, hi + d]; };
  [x0, x1] = grow(x0, x1); [y0, y1] = grow(y0, y1);
  const padL = 36, padB = 26, sc = Math.min((w - padL - 10) / (x1 - x0), (h - padB - 10) / (y1 - y0));
  const ox = padL + ((w - padL - 10) - (x1 - x0) * sc) / 2, oy = 10 + ((h - padB - 10) - (y1 - y0) * sc) / 2;
  const X = (x) => ox + (x - x0) * sc, Y = (y) => oy + (y1 - y) * sc; // left is up
  mapGeom = { sc, ox, oy, x0, y1 };

  ctx.fillStyle = css("--panel2"); ctx.fillRect(X(x0), Y(y1), (x1 - x0) * sc, (y1 - y0) * sc);
  // 1 m grid (2 m when zoomed out)
  const step = x1 - x0 > 12 || y1 - y0 > 12 ? 2 : 1;
  ctx.strokeStyle = css("--line"); ctx.fillStyle = css("--muted"); ctx.font = "11px Segoe UI"; ctx.lineWidth = 1;
  ctx.textAlign = "center";
  for (let v = Math.ceil(x0 / step) * step; v <= x1; v += step) {
    ctx.beginPath(); ctx.moveTo(X(v), Y(y0)); ctx.lineTo(X(v), Y(y1)); ctx.stroke(); ctx.fillText(`${v} m`, X(v), Y(y0) + 16);
  }
  ctx.textAlign = "right";
  for (let v = Math.ceil(y0 / step) * step; v <= y1; v += step) {
    ctx.beginPath(); ctx.moveTo(X(x0), Y(v)); ctx.lineTo(X(x1), Y(v)); ctx.stroke(); ctx.fillText(`${v}`, X(x0) - 6, Y(v) + 4);
  }
  // evidence heat
  for (const [i, j, v] of m.heat) {
    ctx.fillStyle = `rgba(220,38,38,${Math.min(0.8, v)})`;
    ctx.fillRect(X(i * m.cell), Y((j + 1) * m.cell), m.cell * sc + 0.5, m.cell * sc + 0.5);
  }
  // start point
  ctx.strokeStyle = css("--muted"); ctx.lineWidth = 1.5; ctx.strokeRect(X(0) - 5, Y(0) - 5, 10, 10);
  ctx.fillStyle = css("--muted"); ctx.textAlign = "left"; ctx.fillText("start", X(0) + 8, Y(0) + 4);
  // trail
  ctx.strokeStyle = css("--accent"); ctx.globalAlpha = 0.6; ctx.lineWidth = 2; ctx.beginPath();
  m.trail.forEach(([x, y], i) => (i ? ctx.lineTo(X(x), Y(y)) : ctx.moveTo(X(x), Y(y)))); ctx.stroke(); ctx.globalAlpha = 1;
  // estimates
  for (const e of m.estimates) {
    const col = e.method.startsWith("ble") ? css("--ble") : css("--bad");
    ctx.strokeStyle = col; ctx.setLineDash([6, 4]); ctx.lineWidth = 2;
    ctx.beginPath(); ctx.arc(X(e.x), Y(e.y), Math.max(8, e.radius_m * sc), 0, 7); ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = col; ctx.beginPath(); ctx.arc(X(e.x), Y(e.y), 4, 0, 7); ctx.fill();
  }
  // car
  ctx.save(); ctx.translate(X(px), Y(py)); ctx.rotate(-th);
  ctx.fillStyle = css("--text"); ctx.beginPath(); ctx.moveTo(14, 0); ctx.lineTo(-9, 8); ctx.lineTo(-5, 0); ctx.lineTo(-9, -8); ctx.closePath(); ctx.fill();
  ctx.restore();

  $("#coverage").textContent = `${m.driven_m} m`;
  $("#poseVal").textContent = m.where;
  $("#estList").innerHTML = m.estimates.map((e) => `<div class="est">
      <b>${e.from_car_m} m from the car</b>${esc(e.where)} · ${e.method.startsWith("ble") ? "Bluetooth" : "magnetic"} · ±${e.radius_m} m</div>`).join("")
    || `<span class="muted small">No location estimate yet: drive along and across the desks.</span>`;
}
$("#mapCanvas").addEventListener("click", guard(async (e) => {
  if (!$("#placeMode").checked || !mapGeom) return;
  const r = e.target.getBoundingClientRect();
  const x = mapGeom.x0 + (e.clientX - r.left - mapGeom.ox) / mapGeom.sc, y = mapGeom.y1 - (e.clientY - r.top - mapGeom.oy) / mapGeom.sc;
  S.map = await api("/api/pose", "POST", { x, y }); S.last && (S.last.pose = S.map.pose); drawMap();
}));
$("#resetMap").onclick = guard(async () => { S.map = await api("/api/pose", "POST", { reset_map: true }); S.last && (S.last.pose = S.map.pose); drawMap(); });
window.addEventListener("resize", () => { if (activeTab === "monitor") { drawMap(); renderLive(); } });

// ---------------------------------------------------------------- calibration
function renderCalib() {
  const c = S.calibration;
  $("#calInfo").textContent = c
    ? `Last calibrated ${fmtDate(c.time)} · baseline ${c.baseline} · threshold ±${c.threshold}`
    : "Not calibrated yet: using the car's own baseline.";
}
$("#calBtn").onclick = guard(async () => {
  await api("/api/calibrate", "POST", { seconds: 4, k: 5 });
  $("#calInfo").textContent = "Calibrating… keep magnets, phones and metal away from the car.";
});

// ---------------------------------------------------------------- settings
const FIELDS = {
  setBasic: [
    ["alert_threshold", "Alert sensitivity (score ≥)", "number", 0.05], ["mode", "Detection mode", "select", ["fusion", "ml", "threshold"]],
    ["obstacle_stop_cm", "Block forward under (cm)", "number", 1], ["ble_alert_rssi", "Bluetooth 'near' RSSI (dBm)", "number", 1],
    ["server_alert_led", "Red LED on car during alerts", "checkbox"], ["buzzer", "Car buzzer (off = silent exam mode)", "checkbox"],
  ],
};
function renderSettings() {
  const s = S.settings;
  for (const [box, fields] of Object.entries(FIELDS)) {
    $("#" + box).innerHTML = fields.map(([k, label, type, extra]) => {
      if (type === "select") return `<label>${label}<select data-key="${k}">${extra.map((o) => `<option ${o === s[k] ? "selected" : ""}>${o}</option>`).join("")}</select></label>`;
      if (type === "checkbox") return `<label class="chk wide"><input type="checkbox" data-key="${k}" ${s[k] ? "checked" : ""}> ${label}</label>`;
      return `<label>${label}<input type="number" step="${extra}" data-key="${k}" value="${s[k]}"></label>`;
    }).join("");
  }
  renderIgnored();
}
function renderIgnored() {
  $("#ignoredList").innerHTML = (S.ignored || []).map((m) => `<div class="kv"><span>${esc(m)}</span><button class="ghost sm" data-unig="${esc(m)}">Remove</button></div>`).join("") || "None";
}
$("#ignoredList").addEventListener("click", guard(async (e) => {
  const mac = e.target.dataset.unig; if (!mac) return;
  S.ignored = (await api("/api/ble/ignore", "POST", { mac, ignore: false })).ignored; renderIgnored();
}));
$("#saveSettings").onclick = guard(async () => {
  const patch = {};
  $$("[data-key]").forEach((el) => (patch[el.dataset.key] = el.type === "checkbox" ? el.checked : el.tagName === "SELECT" ? el.value : +el.value));
  S.settings = await api("/api/settings", "POST", patch);
  toast("Settings saved", "ok");
});

// ---------------------------------------------------------------- boot
loadMap();
connect();
