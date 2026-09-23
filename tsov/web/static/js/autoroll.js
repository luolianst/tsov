/* autoroll.js —— M-V8 E5 段2：自动化 lane（单轨视图底部子道）。
   数据走命令层 set_automation（/batch，ADR-0017 同一动作路径）；
   交互：点空白=加点 ｜ 拖点=移动 ｜ 双击点 / 右键点=删除（每笔一个命令、可撤销）。
   显示：线性折线（与引擎 host/mix.py::_curve 的 np.interp 语义一致：段外取端点值）。 */

import { bus } from './events.js';
import { api } from './api.js';
import { store, setError, toast, refTag } from './state.js';

const KEYS = 56;             // 与 roll.js KEYS_W 对齐
const PAD = 12;              // 上下留白
const HIT = 7;               // 点命中半径（px）
const FONT = '10px "Microsoft YaHei UI","PingFang SC",system-ui,sans-serif';
const RANGE = { volume: [0.0, 2.0], pan: [-1.0, 1.0] };

let canvas = null, ctx = null, W = 0, H = 0, dpr = 1;
let drag = null;             // {idx, t0, v0, t, v}（拖动中；mouseup 提交）

function owner() {
  const ti = store.viewMode === 'single' ? store.singleTrack : -1;
  return store.score && ti >= 0 && store.score.tracks[ti] ? { ti, track: store.score.tracks[ti] } : null;
}

function param() { return store.autoLane.param === 'pan' ? 'pan' : 'volume'; }

function points() {
  const o = owner();
  if (!o) return [];
  const pts = (o.track.automation || {})[param()] || [];
  return pts.map((p) => [Number(p[0]), Number(p[1])])
    .filter((p) => Number.isFinite(p[0]) && Number.isFinite(p[1]));
}

/* ---- 几何（与 roll.js 同口径：x 随 scrollSec/pxPerSec；y 值域自适应） ---- */

function resize() {
  if (!canvas) return;
  dpr = window.devicePixelRatio || 1;
  W = Math.max(10, Math.floor(canvas.clientWidth));      // client* 排除边框，避免反馈环
  H = Math.max(10, Math.floor(canvas.clientHeight));
  canvas.width = Math.floor(W * dpr);
  canvas.height = Math.floor(H * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}

function xOf(t) { return KEYS + (t - store.view.scrollSec) * store.view.pxPerSec; }
function tOf(x) { return Math.max(0, store.view.scrollSec + (x - KEYS) / store.view.pxPerSec); }
function yOfV(v) {
  const [lo, hi] = RANGE[param()];
  return PAD + (hi - v) / (hi - lo) * (H - PAD * 2);
}
function vOfY(y) {
  const [lo, hi] = RANGE[param()];
  const raw = hi - (y - PAD) / (H - PAD * 2) * (hi - lo);
  return Math.min(hi, Math.max(lo, raw));
}

function nearest(x, y) {
  const pts = points();
  let best = -1, bd = HIT;
  pts.forEach((p, i) => {
    const d = Math.hypot(xOf(p[0]) - x, yOfV(p[1]) - y);
    if (d <= bd) { bd = d; best = i; }
  });
  return best;
}

function css(sel) { return getComputedStyle(document.documentElement).getPropertyValue(sel).trim(); }

/* 隐藏期 ResizeObserver 不保证补发 → 显示时按需重适（避免 v 值域错乱） */
function ensureSized() {
  if (!canvas) return;
  const w = Math.floor(canvas.clientWidth * dpr);
  const h = Math.floor(canvas.clientHeight * dpr);
  if (canvas.width !== w || canvas.height !== h) resize();
}

/* ---- 绘制 ---- */

export function draw() {
  if (!canvas || !ctx) return;
  const show = store.viewMode === 'single' && store.autoLane.open && !!owner();
  canvas.hidden = !show;
  if (!show) return;
  ensureSized();
  ctx.clearRect(0, 0, W, H);
  const p = param();
  const [lo, hi] = RANGE[p];
  const cBg = css('--bg-elev') || '#fff';
  const cGrid = css('--border') || '#ccc';
  const cLine = css('--accent') || '#333';
  const cText = css('--muted') || '#666';
  const cHead = css('--text') || '#111';

  ctx.fillStyle = cBg;
  ctx.fillRect(0, 0, W, H);
  ctx.font = FONT;

  /* 值域参考行 */
  const rows = p === 'volume' ? [0, 0.5, 1, 1.5, 2] : [-1, -0.5, 0, 0.5, 1];
  ctx.strokeStyle = cGrid;
  for (const v of rows) {
    const y = Math.round(yOfV(v)) + 0.5;
    ctx.beginPath(); ctx.moveTo(KEYS, y); ctx.lineTo(W, y); ctx.stroke();
  }
  ctx.fillStyle = cText;
  ctx.fillText(p === 'volume' ? '音量(0~200%)' : '声像(L~R)', 6, yOfV(hi) + 10);
  ctx.fillText(p === 'volume' ? '0%' : 'L', 6, yOfV(lo) - 3);

  /* 折线（段外端点延伸，与引擎 np.interp 一致） */
  const pts = points();
  if (pts.length) {
    const t0 = Math.max(0, tOf(KEYS));
    const t1 = tOf(W);
    ctx.strokeStyle = cLine;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(xOf(t0), yOfV(pts[0][1]));
    for (const q of pts) {
      const x = xOf(q[0]);
      if (x < KEYS - 8) continue;
      if (x > W + 8) break;
      ctx.lineTo(x, yOfV(q[1]));
    }
    ctx.lineTo(xOf(t1), yOfV(pts[pts.length - 1][1]));
    ctx.stroke();
    ctx.lineWidth = 1;
    pts.forEach((q, i) => {
      const x = xOf(q[0]);
      if (x < KEYS - 6 || x > W + 6) return;
      const y = yOfV(q[1]);
      const active = drag && drag.idx === i;
      ctx.fillStyle = active ? cHead : cLine;
      ctx.beginPath();
      ctx.arc(x, y, active ? 5 : 4, 0, Math.PI * 2);
      ctx.fill();
      if (active) {
        ctx.strokeStyle = cHead;
        ctx.beginPath(); ctx.arc(x, y, 7, 0, Math.PI * 2); ctx.stroke();
      }
    });
  } else {
    ctx.fillStyle = cText;
    ctx.fillText('（无自动化点：单击此道加点）', KEYS + 8, H / 2);
  }

  /* 播放头 */
  const px = xOf(store.playhead);
  if (px >= KEYS && px <= W) {
    ctx.strokeStyle = cHead;
    ctx.beginPath(); ctx.moveTo(px, 0); ctx.lineTo(px, H); ctx.stroke();
  }
}

/* ---- 提交（命令层 /batch：set_automation 点集整体替换） ---- */

async function commit(pts, label) {
  const o = owner();
  if (!o || !store.project) return;
  const r6 = (x) => Math.round(x * 1e6) / 1e6;
  const r4 = (x) => Math.round(x * 1e4) / 1e4;
  const value = { param: param(), points: pts.map((q) => [r6(q[0]), r4(q[1])]) };
  try {
    const r = await api.postBatch(store.project, '自动化 ' + label,
      [{ op: 'set_automation', track: o.ti, value }],
      '自动化（' + (param() === 'volume' ? '音量' : '声像') + '）：' + label);
    if (r.applied) toast('已' + label + ' ' + refTag(r));
    else setError('被拒：' + (r.errors || []).join('；'));
  } catch (e) { setError(e.message); }
}

function sortedWith(pts, t, v) {
  const out = pts.map((q) => [q[0], q[1]]);
  out.push([t, v]);
  out.sort((a, b) => a[0] - b[0]);
  const dedup = [];
  for (const q of out) {
    if (dedup.length && Math.abs(dedup[dedup.length - 1][0] - q[0]) < 1e-9) dedup[dedup.length - 1] = q;
    else dedup.push(q);
  }
  return dedup;
}

/* ---- 交互 ---- */

function onDown(e) {
  if (!store.autoLane.open || e.button !== 0) return;
  if (!owner()) return;
  const pts = points();
  const idx = nearest(e.offsetX, e.offsetY);
  if (idx >= 0) {
    drag = { idx, t0: pts[idx][0], v0: pts[idx][1], t: pts[idx][0], v: pts[idx][1] };
    draw();
    return;
  }
  commit(sortedWith(pts, tOf(e.offsetX), vOfY(e.offsetY)), '加点');
}

function onMove(e) {
  if (!drag) return;
  drag.t = tOf(e.offsetX);
  drag.v = vOfY(e.offsetY);
  draw();
}

function onUp() {
  if (!drag) return;
  const d = drag;
  drag = null;
  const moved = Math.abs(d.t - d.t0) > 1e-9 || Math.abs(d.v - d.v0) > 1e-9;
  if (moved) {
    const pts = points();
    if (d.idx >= 0 && d.idx < pts.length) {
      const next = pts.map((q, i) => (i === d.idx ? [d.t, d.v] : [q[0], q[1]]));
      next.sort((a, b) => a[0] - b[0]);
      commit(next, '移动');
    }
  }
  draw();
}

function removeAt(e) {
  const idx = nearest(e.offsetX, e.offsetY);
  if (idx < 0) return false;
  const pts = points().filter((_, i) => i !== idx);
  commit(pts, '删除');
  return true;
}

export function init(el) {
  canvas = el;
  if (!canvas) return;
  ctx = canvas.getContext('2d');
  resize();
  new ResizeObserver(() => { resize(); draw(); }).observe(canvas);

  canvas.addEventListener('mousedown', onDown);
  canvas.addEventListener('mousemove', onMove);
  window.addEventListener('mouseup', onUp);
  canvas.addEventListener('dblclick', (e) => { e.preventDefault(); removeAt(e); });
  canvas.addEventListener('contextmenu', (e) => { e.preventDefault(); removeAt(e); });

  for (const topic of ['state', 'auto', 'view', 'viewmode', 'playhead', 'batch_applied']) {
    bus.on(topic, draw);
  }
  draw();
}

export function resizeNow() { resize(); draw(); }
