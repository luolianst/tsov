/* lane_render.js —— 通用道渲染器（v0.2 批C 前段·地基件 R2；设计件 §3.3）。
   descriptor 驱动（每道一份）：
     { id, title, bottomTag, range() -> [lo, hi], readPoints() -> [[t, v], …], commit(pts, label) }
   本批落地 curve 道（自动化音量/声像）；bars（力度）等 kind 挂接时扩展。
   交互与既有 autoroll 一字不差：点空白=加点 ｜ 拖点=移动 ｜ 双击/右键点=删除（每笔一命令、可撤销）。
   显示折线 = 段外端点延伸（与引擎 host/mix.py::_curve 的 np.interp 语义一致）。 */

import { KEYS_W, store } from './state.js';
import { xOf, tOf } from './geom.js';

const PAD = 12;              // 上下留白
const HIT = 7;               // 点命中半径（px）
const FONT = '10px "Microsoft YaHei UI","PingFang SC",system-ui,sans-serif';

function css(sel) { return getComputedStyle(document.documentElement).getPropertyValue(sel).trim(); }

export function createLane(descriptor) {
  const lane = { canvas: null, ctx: null, W: 0, H: 0, dpr: 1, drag: null, ro: null, sel: new Set(), rubber: null };
  const isBars = descriptor.kind === 'bars';   /* v0.2 批C 后段：bars 道型（力度柱状） */

  function resize() {
    if (!lane.canvas) return;
    lane.dpr = window.devicePixelRatio || 1;
    lane.W = Math.max(10, Math.floor(lane.canvas.clientWidth));      // client* 排除边框，避免反馈环
    lane.H = Math.max(10, Math.floor(lane.canvas.clientHeight));
    lane.canvas.width = Math.floor(lane.W * lane.dpr);
    lane.canvas.height = Math.floor(lane.H * lane.dpr);
    lane.ctx.setTransform(lane.dpr, 0, 0, lane.dpr, 0, 0);
  }

  /* 隐藏期 ResizeObserver 不保证补发 → 显示时按需重适（避免 v 值域错乱） */
  function ensureSized() {
    if (!lane.canvas) return;
    const w = Math.floor(lane.canvas.clientWidth * lane.dpr);
    const h = Math.floor(lane.canvas.clientHeight * lane.dpr);
    if (lane.canvas.width !== w || lane.canvas.height !== h) resize();
  }

  const yOfV = (v) => {
    const [lo, hi] = descriptor.range();
    return PAD + (hi - v) / (hi - lo) * (lane.H - PAD * 2);
  };
  const vOfY = (y) => {
    const [lo, hi] = descriptor.range();
    const raw = hi - (y - PAD) / (lane.H - PAD * 2) * (hi - lo);
    return Math.min(hi, Math.max(lo, raw));
  };
  const tOfC = (x) => Math.max(0, tOf(x));   // 历史 autoroll 口径：t ≥ 0 钳制

  function nearest(x, y) {
    const pts = descriptor.readPoints();
    let best = -1, bd = HIT;
    pts.forEach((p, i) => {
      const d = Math.hypot(xOf(p[0]) - x, yOfV(p[1]) - y);
      if (d <= bd) { bd = d; best = i; }
    });
    return best;
  }

  function draw() {
    if (!lane.canvas || !lane.ctx) return;
    ensureSized();
    if (isBars) { drawBars(); return; }   /* v0.2 批C 后段：bars 分流 */
    const ctx = lane.ctx, W = lane.W, H = lane.H;
    ctx.clearRect(0, 0, W, H);
    const [lo, hi] = descriptor.range();
    const cBg = css('--bg-elev') || '#fff';
    const cGrid = css('--border') || '#ccc';
    const cLine = css('--accent') || '#333';
    const cText = css('--muted') || '#666';
    const cHead = css('--text') || '#111';

    ctx.fillStyle = cBg;
    ctx.fillRect(0, 0, W, H);
    ctx.font = FONT;

    /* 值域参考行（5 等分；音量 0/0.5/1/1.5/2、声像 -1/-0.5/0/0.5/1 逐值一致） */
    const rows = [0, 1, 2, 3, 4].map((i) => lo + (hi - lo) * i / 4);
    ctx.strokeStyle = cGrid;
    for (const v of rows) {
      const y = Math.round(yOfV(v)) + 0.5;
      ctx.beginPath(); ctx.moveTo(KEYS_W, y); ctx.lineTo(W, y); ctx.stroke();
    }
    ctx.fillStyle = cText;
    ctx.fillText(descriptor.title, 6, yOfV(hi) + 10);
    ctx.fillText(descriptor.bottomTag, 6, yOfV(lo) - 3);

    /* 折线（段外端点延伸，与引擎 np.interp 一致） */
    const pts = descriptor.readPoints();
    if (pts.length) {
      const t0 = Math.max(0, tOf(KEYS_W));
      const t1 = tOf(W);
      ctx.strokeStyle = cLine;
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(xOf(t0), yOfV(pts[0][1]));
      for (const q of pts) {
        const x = xOf(q[0]);
        if (x < KEYS_W - 8) continue;
        if (x > W + 8) break;
        ctx.lineTo(x, yOfV(q[1]));
      }
      ctx.lineTo(xOf(t1), yOfV(pts[pts.length - 1][1]));
      ctx.stroke();
      ctx.lineWidth = 1;
      pts.forEach((q, i) => {
        const x = xOf(q[0]);
        if (x < KEYS_W - 6 || x > W + 6) return;
        const y = yOfV(q[1]);
        const active = lane.drag && lane.drag.idx === i;
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
      ctx.fillText('（无自动化点：单击此道加点）', KEYS_W + 8, H / 2);
    }

    /* 播放头 */
    const px = xOf(store.playhead);
    if (px >= KEYS_W && px <= W) {
      ctx.strokeStyle = cHead;
      ctx.beginPath(); ctx.moveTo(px, 0); ctx.lineTo(px, H); ctx.stroke();
    }
  }

  /* ---- v0.2 批C 后段（挂道族）：bars 道（力度柱状）——拖=设置 / 框选=批量（一事务） ---- */

  function bars() { return descriptor.readBars ? descriptor.readBars() : []; }

  function drawBars() {
    const ctx = lane.ctx, W = lane.W, H = lane.H;
    ctx.clearRect(0, 0, W, H);
    const [lo, hi] = descriptor.range();
    const cBg = css('--bg-elev') || '#fff';
    const cGrid = css('--border') || '#ccc';
    const cBar = css('--accent') || '#333';
    const cText = css('--muted') || '#666';
    const cHead = css('--text') || '#111';
    ctx.fillStyle = cBg;
    ctx.fillRect(0, 0, W, H);
    ctx.font = FONT;
    const rows = [0, 1, 2, 3, 4].map((i) => lo + (hi - lo) * i / 4);
    ctx.strokeStyle = cGrid;
    for (const v of rows) {
      const y = Math.round(yOfV(v)) + 0.5;
      ctx.beginPath(); ctx.moveTo(KEYS_W, y); ctx.lineTo(W, y); ctx.stroke();
    }
    ctx.fillStyle = cText;
    ctx.fillText(descriptor.title, 6, yOfV(hi) + 10);
    ctx.fillText(descriptor.bottomTag, 6, yOfV(lo) - 3);
    const baseY = yOfV(lo);
    const list = bars();
    for (let i = 0; i < list.length; i++) {
      const b = list[i];
      const x0 = xOf(b.t0), x1 = Math.max(x0 + 2, xOf(b.t1));
      if (x1 < KEYS_W - 6 || x0 > W + 6) continue;
      const v = (lane.drag && lane.drag.pending && lane.drag.pending.has(i)) ? lane.drag.pending.get(i) : b.v;
      const y = yOfV(v);
      const sel = lane.sel && lane.sel.has(i);
      const act = lane.drag && lane.drag.idx === i;
      ctx.fillStyle = act ? cHead : (sel ? cBar : cText);
      ctx.globalAlpha = (act || sel) ? 1.0 : 0.8;
      const bx = Math.max(KEYS_W, x0);
      ctx.fillRect(bx, y, Math.max(2, Math.min(x1 - x0 - 1, W - bx)), Math.max(1, baseY - y));
      ctx.globalAlpha = 1;
    }
    if (lane.rubber) {
      const rx0 = lane.rubber.x0, rx1 = lane.rubber.x1;
      ctx.strokeStyle = cBar;
      ctx.setLineDash([4, 3]);
      ctx.strokeRect(Math.min(rx0, rx1) + 0.5, 0.5, Math.abs(rx1 - rx0), H - 1);
      ctx.setLineDash([]);
    }
    const px = xOf(store.playhead);
    if (px >= KEYS_W && px <= W) {
      ctx.strokeStyle = cHead;
      ctx.beginPath(); ctx.moveTo(px, 0); ctx.lineTo(px, H); ctx.stroke();
    }
  }

  function barAt(x, y) {
    const list = bars();
    const lo = descriptor.range()[0];
    for (let i = list.length - 1; i >= 0; i--) {
      const b = list[i];
      const x0 = xOf(b.t0), x1 = Math.max(x0 + 2, xOf(b.t1));
      if (x >= x0 - 3 && x <= x1 + 3) {
        const yTop = yOfV(b.v), yBase = yOfV(lo);
        if (y >= yTop - 6 && y <= yBase + 4) return i;
      }
    }
    return -1;
  }

  function onDownBars(e) {
    if (e.button !== 0) return;
    const list = bars();
    const idx = barAt(e.offsetX, e.offsetY);
    if (idx >= 0) {
      if (!lane.sel.has(idx)) lane.sel = new Set([idx]);
      const selIdx = Array.from(lane.sel).filter((i) => i < list.length);
      lane.drag = { idx, v: vOfY(e.offsetY), origs: selIdx.map((i) => [i, list[i].v]), pending: null };
      draw();
      return;
    }
    lane.rubber = { x0: e.offsetX, x1: e.offsetX, moved: false };
    draw();
  }

  function onMoveBars(e) {
    if (lane.drag) {
      const dv = vOfY(e.offsetY) - lane.drag.v;
      const lo = descriptor.range()[0], hi = descriptor.range()[1];
      lane.drag.pending = new Map(lane.drag.origs.map(([i, v0]) => [i, Math.min(hi, Math.max(lo, v0 + dv))]));
      draw();
      return;
    }
    if (lane.rubber) {
      lane.rubber.x1 = e.offsetX;
      if (Math.abs(lane.rubber.x1 - lane.rubber.x0) > 3) lane.rubber.moved = true;
      draw();
    }
  }

  function onUpBars() {
    if (lane.drag) {
      const d = lane.drag;
      lane.drag = null;
      if (d.pending) {
        const changes = [];
        d.pending.forEach((v, i) => {
          const orig = d.origs.find((o) => o[0] === i);
          if (orig && Math.abs(v - orig[1]) > 1e-9) changes.push([i, v]);
        });
        if (changes.length) {
          descriptor.commitBars(changes, changes.length > 1 ? ('批量设置 ' + changes.length + ' 音') : '设置力度');
        }
      }
      draw();
      return;
    }
    if (lane.rubber) {
      const rb = lane.rubber;
      lane.rubber = null;
      if (rb.moved) {
        const ta = tOf(Math.min(rb.x0, rb.x1)), tb = tOf(Math.max(rb.x0, rb.x1));
        const sel = new Set();
        bars().forEach((b, i) => { if (b.t0 >= ta - 1e-9 && b.t0 <= tb + 1e-9) sel.add(i); });
        lane.sel = sel;
      } else {
        lane.sel = new Set();
      }
      draw();
    }
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
    if (e.button !== 0) return;
    if (isBars) { onDownBars(e); return; }   /* v0.2 批C 后段：bars 分流 */
    const pts = descriptor.readPoints();
    const idx = nearest(e.offsetX, e.offsetY);
    if (idx >= 0) {
      lane.drag = { idx, t0: pts[idx][0], v0: pts[idx][1], t: pts[idx][0], v: pts[idx][1] };
      draw();
      return;
    }
    descriptor.commit(sortedWith(pts, tOfC(e.offsetX), vOfY(e.offsetY)), '加点');
  }

  function onMove(e) {
    if (isBars) { onMoveBars(e); return; }   /* v0.2 批C 后段：bars 分流 */
    if (!lane.drag) return;
    lane.drag.t = tOfC(e.offsetX);
    lane.drag.v = vOfY(e.offsetY);
    draw();
  }

  function onUp() {
    if (isBars) { onUpBars(); return; }   /* v0.2 批C 后段：bars 分流 */
    if (!lane.drag) return;
    const d = lane.drag;
    lane.drag = null;
    const moved = Math.abs(d.t - d.t0) > 1e-9 || Math.abs(d.v - d.v0) > 1e-9;
    if (moved) {
      const pts = descriptor.readPoints();
      if (d.idx >= 0 && d.idx < pts.length) {
        const next = pts.map((q, i) => (i === d.idx ? [d.t, d.v] : [q[0], q[1]]));
        next.sort((a, b) => a[0] - b[0]);
        descriptor.commit(next, '移动');
      }
    }
    draw();
  }

  function removeAt(e) {
    const idx = nearest(e.offsetX, e.offsetY);
    if (idx < 0) return false;
    const pts = descriptor.readPoints().filter((_, i) => i !== idx);
    descriptor.commit(pts, '删除');
    return true;
  }

  function attach(el) {
    lane.canvas = el;
    if (!lane.canvas) return;
    lane.ctx = el.getContext('2d');
    resize();
    lane.ro = new ResizeObserver(() => { resize(); draw(); });
    lane.ro.observe(el);
    el.addEventListener('mousedown', onDown);
    el.addEventListener('mousemove', onMove);
    el.addEventListener('dblclick', (e) => { e.preventDefault(); if (!isBars) removeAt(e); });
    el.addEventListener('contextmenu', (e) => { e.preventDefault(); if (!isBars) removeAt(e); });
    window.addEventListener('mouseup', onUp);
    draw();
  }

  function destroy() {
    window.removeEventListener('mouseup', onUp);
    if (lane.ro) { lane.ro.disconnect(); lane.ro = null; }
    lane.canvas = null; lane.ctx = null; lane.drag = null;
  }

  return { descriptor, attach, resize, draw, destroy };
}
