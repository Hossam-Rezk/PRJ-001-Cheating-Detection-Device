// Small dependency-free canvas charts (works offline in the exam room).
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();

function setupCanvas(canvas) {
  const dpr = window.devicePixelRatio || 1;
  // canvas.height is rewritten below for HiDPI, so keep the CSS height from the markup
  canvas.dataset.h = canvas.dataset.h || canvas.getAttribute("height");
  const w = canvas.clientWidth, h = +canvas.dataset.h;
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
    canvas.style.height = h + "px";
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  return { ctx, w, h };
}

/**
 * series: [{data: [[x, y], ...], color, width, dash}]
 * opts: {xmin, xmax, ymin, ymax, hlines: [{y, color, dash, label}], xfmt, yfmt, xlabel, ylabel, fixedY}
 */
function lineChart(canvas, series, opts = {}) {
  const { ctx, w, h } = setupCanvas(canvas);
  const pad = { l: 46, r: 10, t: 10, b: opts.xlabel ? 34 : 22 };
  const pts = series.flatMap((s) => s.data.filter((p) => p[1] !== null && p[1] !== undefined));
  let xmin = opts.xmin ?? Math.min(...pts.map((p) => p[0]));
  let xmax = opts.xmax ?? Math.max(...pts.map((p) => p[0]));
  let ymin = opts.ymin, ymax = opts.ymax;
  if (ymin === undefined || ymax === undefined) {
    const ys = pts.map((p) => p[1]).concat((opts.hlines || []).map((l) => l.y));
    const lo = ys.length ? Math.min(...ys) : 0, hi = ys.length ? Math.max(...ys) : 1;
    const span = Math.max(hi - lo, opts.minSpan || 1);
    ymin = ymin ?? lo - span * 0.1; ymax = ymax ?? hi + span * 0.1;
  }
  if (!isFinite(xmin) || xmin === xmax) { xmin = 0; xmax = 1; }
  const X = (x) => pad.l + ((x - xmin) / (xmax - xmin)) * (w - pad.l - pad.r);
  const Y = (y) => pad.t + (1 - (y - ymin) / (ymax - ymin)) * (h - pad.t - pad.b);

  ctx.font = "11px Segoe UI, system-ui"; ctx.fillStyle = css("--muted"); ctx.strokeStyle = css("--line"); ctx.lineWidth = 1;
  const yfmt = opts.yfmt || ((v) => (Math.abs(ymax - ymin) < 5 ? v.toFixed(2) : Math.round(v)));
  for (let i = 0; i <= 4; i++) {
    const v = ymin + ((ymax - ymin) * i) / 4, y = Y(v);
    ctx.globalAlpha = 0.5; ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(w - pad.r, y); ctx.stroke(); ctx.globalAlpha = 1;
    ctx.textAlign = "right"; ctx.fillText(yfmt(v), pad.l - 6, y + 4);
  }
  if (opts.xfmt) {
    ctx.textAlign = "center";
    for (let i = 0; i <= 4; i++) { const v = xmin + ((xmax - xmin) * i) / 4; ctx.fillText(opts.xfmt(v), X(v), h - (opts.xlabel ? 18 : 6)); }
  }
  if (opts.xlabel) { ctx.textAlign = "center"; ctx.fillText(opts.xlabel, (pad.l + w) / 2, h - 3); }
  for (const l of opts.hlines || []) {
    if (l.y < ymin || l.y > ymax) continue;
    ctx.strokeStyle = l.color; ctx.setLineDash(l.dash || [5, 4]); ctx.beginPath();
    ctx.moveTo(pad.l, Y(l.y)); ctx.lineTo(w - pad.r, Y(l.y)); ctx.stroke(); ctx.setLineDash([]);
    if (l.label) { ctx.fillStyle = l.color; ctx.textAlign = "left"; ctx.fillText(l.label, pad.l + 4, Y(l.y) - 4); }
  }
  ctx.save(); ctx.beginPath(); ctx.rect(pad.l, pad.t, w - pad.l - pad.r, h - pad.t - pad.b); ctx.clip();
  for (const s of series) {
    ctx.strokeStyle = s.color; ctx.lineWidth = s.width || 1.6; ctx.setLineDash(s.dash || []);
    ctx.beginPath(); let started = false;
    for (const [x, y] of s.data) {
      if (y === null || y === undefined) { started = false; continue; }
      if (!started) { ctx.moveTo(X(x), Y(y)); started = true; } else ctx.lineTo(X(x), Y(y));
    }
    ctx.stroke(); ctx.setLineDash([]);
    if (s.points) { ctx.fillStyle = s.color; for (const [x, y] of s.data) { ctx.beginPath(); ctx.arc(X(x), Y(y), 3, 0, 7); ctx.fill(); } }
  }
  ctx.restore();
}
