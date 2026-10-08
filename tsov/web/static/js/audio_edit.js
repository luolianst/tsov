/* audio_edit.js —— 音频 clip 编辑引擎（总谱 lanes.js / 单轨 roll.js 共用）
   E6 段1 补（2026-09-27）：自 lanes.js 抽出——多 clip 几何 / 命中 / 拖拽 / 切分 / 落盘；
   两视图只传「clip 带」几何（lanes=轨道行内带；roll=单轨主区/叠加区带）与重绘回调。
   手势：拖块=移动 ｜ 拖左右缘=修剪 ｜ 拖顶角=淡入/淡出 ｜ Alt+拖右缘=伸缩（0.5–2.0 保音高）
        剪刀工具单击 / Alt+单击 = 切分（网格吸附；Alt=自由）
   全部落盘经命令层 set_audio_clips / split_audio_clip（ADR-0017 同一动作路径）。 */

import { bus } from './events.js';
import { store, tempo, setError, audioClipsOf } from './state.js';
import { api } from './api.js';
import { peaksGet } from './peaks.js';
import { xOf } from './geom.js';   /* v0.2 批C 前段（R2 地基件）：时间↔x 几何共享 */

const KEYS = 56;          // 左侧标签槽（与 lanes/roll 的 KEYS_W 对齐）
const FONT_UI = '11px "Microsoft YaHei UI","PingFang SC","MiSans","HarmonyOS Sans SC",system-ui,sans-serif';

const EDGE = 6;          // 左右缘命中带宽（px）
const CORNER = 11;       // 顶部角句柄带高（px）
const MIN_SPAN = 0.02;   // clip 最短时间线长（秒）

let drag = null;      // {ti, mode, idx, startX, alt, base, preview, moved}
let selClip = null;   // 最近操作 clip {ti, idx}（高亮）
let lastDraw = () => {};

export function selOf() { return selClip; }
export function clearSel() { selClip = null; }
export function isDragging() { return !!drag; }
export function dragPreviewOf(ti) { return (drag && drag.ti === ti) ? drag.preview : null; }

export function r6(x) { return Math.round(Number(x) * 1e6) / 1e6; }

/* v0.2 批C 前段（R2 地基件）：时间↔x 几何改用共享 geom.js（行为一字不差） */

export function fileDur(rel) {
  const pv = peaksGet(rel);
  return pv && pv.seconds ? pv.seconds : null;
}

/* clip 显示几何：{x0, w, span, srcSpan, dur}（span=null → 素材时长/区间未知） */
export function clipGeom(c) {
  const x0 = xOf(c.start);
  const dur = fileDur(c.file);
  let srcSpan = null;
  if (c.src_len != null) srcSpan = dur == null ? c.src_len : Math.min(c.src_len, Math.max(0, dur - c.src_offset));
  else if (dur != null) srcSpan = Math.max(0, dur - c.src_offset);
  const span = srcSpan == null ? null : srcSpan * (c.stretch || 1);
  const w = span == null ? null : Math.max(2, span * store.view.pxPerSec);
  return { x0, w, span, srcSpan, dur };
}

export function spanOf(c) { return clipGeom(c).span; }

/* 网格吸附（free=true 原样返回；否则节拍网格——单轨 Grid 值设过则沿用其比例） */
export function snapTime(t, free) {
  t = Math.max(0, Number(t) || 0);
  if (free) return t;
  const beat = 60 / tempo();
  const grid = (store.snapFrac && store.snapFrac > 0) ? beat * store.snapFrac : beat;
  return Math.max(0, Math.round(t / grid) * grid);
}

/* 命中（含 zone）：move | trim-in | trim-out | fade-in | fade-out
   带几何由调用方提供（yTop/hh = clip 带上下缘画布坐标；±2px 容差） */
export function clipHitBand(ti, offX, offY, yTop, hh) {
  const trk = store.score && store.score.tracks[ti];
  if (!trk || trk.kind !== 'audio') return null;
  const yBot = yTop + hh;
  const list = (drag && drag.ti === ti) ? drag.preview : audioClipsOf(trk);
  for (let idx = list.length - 1; idx >= 0; idx--) {
    const c = list[idx];
    const g = clipGeom(c);
    const w = g.w == null ? 3 * store.view.pxPerSec : g.w;   // 素材未知 → 3s 占位宽
    const x0 = g.x0, x1 = x0 + w;
    if (offX < x0 - 2 || offX > x1 + 2) continue;
    if (offY < yTop - 2 || offY > yBot + 2) continue;
    let zone = 'move';
    if (Math.abs(offX - x0) <= EDGE) zone = 'trim-in';
    if (Math.abs(offX - x1) <= EDGE) zone = 'trim-out';
    if (offY <= yTop + CORNER) {
      if (Math.abs(offX - x0) <= CORNER + 3) zone = 'fade-in';
      else if (Math.abs(offX - x1) <= CORNER + 3) zone = 'fade-out';
    }
    return { idx, c, zone, w };
  }
  return null;
}

/* 切分（剪刀工具单击 / Alt+单击）：时间先吸附（Alt=自由）；细护栏在服务端 */
export async function splitClipAt(ti, idx, t0, free) {
  const trk = store.score && store.score.tracks[ti];
  if (!trk || !store.project) return;
  const c = audioClipsOf(trk)[idx];
  if (!c) return;
  const at = r6(snapTime(t0, free));
  if (at <= c.start + 0.002) { setError('切点须在片段内（>= 起点）'); return; }
  try {
    const r = await api.postBatch(store.project, '切分音频片段',
      [{ op: 'split_audio_clip', track: ti, value: { clip_id: c.clip_id || '', at } }],
      '剪刀：切分音频 @ ' + at.toFixed(3) + 's');
    if (r.applied) bus.dispatch('toast', '已切分音频片段 @ ' + at.toFixed(2) + 's');
    else setError('被拒：' + (r.errors || []).join('；'));
  } catch (e) { setError(e.message); }
}

export function beginAudioDrag(ti, hit, e, drawFn) {
  const trk = store.score.tracks[ti];
  const base = audioClipsOf(trk).map((c) => Object.assign({}, c));
  const mode = (e.altKey && hit.zone === 'trim-out') ? 'stretch' : hit.zone;
  drag = { ti, mode, idx: hit.idx, startX: e.clientX, alt: !!e.altKey,
           base, preview: base.map((c) => Object.assign({}, c)), moved: false };
  selClip = { ti, idx: hit.idx };
  if (typeof drawFn === 'function') lastDraw = drawFn;
}

export function updateAudioDrag(e) {
  const d = drag;
  const b = d.base[d.idx];
  const k = b.stretch || 1;
  const dt = (e.clientX - d.startX) / store.view.pxPerSec;
  if (Math.abs(dt) > 0.002) d.moved = true;
  const c = Object.assign({}, b);
  if (d.mode === 'move') {
    c.start = r6(snapTime(b.start + dt, d.alt));
  } else if (d.mode === 'trim-in') {
    let dsrc = dt / k;
    dsrc = Math.max(dsrc, -b.src_offset, -b.start / k);   // 源起点 ≥ 0 ｜ 时间线 ≥ 0
    if (b.src_len != null) dsrc = Math.min(dsrc, b.src_len - MIN_SPAN);
    c.src_offset = r6(b.src_offset + dsrc);
    c.start = r6(Math.max(0, b.start + dsrc * k));
    if (b.src_len != null) c.src_len = r6(Math.max(MIN_SPAN, b.src_len - dsrc));
  } else if (d.mode === 'trim-out') {
    const dur = fileDur(c.file);
    let L = b.src_len == null ? (dur == null ? null : Math.max(0, dur - b.src_offset)) : b.src_len;
    if (L == null) return;
    L = L + dt / k;
    if (dur != null) L = Math.min(L, Math.max(MIN_SPAN, dur - b.src_offset));
    L = Math.max(MIN_SPAN, L);
    c.src_len = r6(L);
  } else if (d.mode === 'fade-in') {
    const span = spanOf(b);
    c.fade_in = r6(Math.max(0, Math.min(span == null ? 1e9 : span, b.fade_in + dt)));
  } else if (d.mode === 'fade-out') {
    const span = spanOf(b);
    c.fade_out = r6(Math.max(0, Math.min(span == null ? 1e9 : span, b.fade_out - dt)));
  } else if (d.mode === 'stretch') {
    const dur = fileDur(c.file);
    const srcSpan = b.src_len != null ? b.src_len : (dur == null ? null : Math.max(0, dur - b.src_offset));
    if (srcSpan == null || srcSpan <= 0) return;
    const span = srcSpan * k + dt;
    c.stretch = r6(Math.max(0.5, Math.min(2.0, span / srcSpan)));
  }
  d.preview = d.base.map((x, i) => (i === d.idx ? c : x));
  lastDraw();
}

export async function finishAudioDrag() {
  const d = drag;
  drag = null;
  const fn = lastDraw;
  if (!d || !d.moved || !store.project) { fn(); return; }
  const clips = d.preview.map((c) => ({
    clip_id: c.clip_id || '', file: c.file, start: r6(c.start), src_offset: r6(c.src_offset),
    src_len: c.src_len == null ? null : r6(c.src_len), stretch: r6(c.stretch || 1),
    fade_in: r6(c.fade_in || 0), fade_out: r6(c.fade_out || 0),
  }));
  const LABEL = {
    move: '移动音频片段', 'trim-in': '修剪音频片段', 'trim-out': '修剪音频片段',
    'fade-in': '调整淡入淡出', 'fade-out': '调整淡入淡出', stretch: '伸缩音频片段',
  };
  try {
    const r = await api.postBatch(store.project, LABEL[d.mode] || '编辑音频片段',
      [{ op: 'set_audio_clips', track: d.ti, value: { clips } }], null);
    if (!r.applied) setError('被拒：' + (r.errors || []).join('；'));
  } catch (e) { setError(e.message); }
  fn();
}

/* clip 块绘制（块底/波形/淡化斜线/角句柄/标签）；总谱与单轨共用
   o = {list, col, yTop, hh, selIdx, inDrag, dim, label, p, v, xLo, xHi} */
export function drawClipBlocks(ctx, o) {
  const v = o.v || store.view;
  const p = o.p;
  (o.list || []).forEach((c, idx) => {
    const g = clipGeom(c);
    const w = g.w == null ? 3 * v.pxPerSec : g.w;   /* 素材时长未知：占位宽（peaks 拉回后重绘） */
    const x0 = g.x0, x1 = x0 + w;
    if (x1 < o.xLo || x0 > o.xHi) return;
    const isSel = o.selIdx != null && o.selIdx === idx;
    ctx.fillStyle = o.col;
    ctx.globalAlpha = o.inDrag ? 0.30 : (isSel ? 0.26 : (o.dim ? 0.10 : 0.16));
    ctx.fillRect(x0, o.yTop, w, o.hh);
    ctx.globalAlpha = 1;
    const pv = peaksGet(c.file);
    if (g.srcSpan != null && g.dur && pv && pv.max && pv.max.length) {
      /* 波形：源区间 [src_offset, +srcSpan) 映射到块内像素（min/max 竖线） */
      const mid = o.yTop + o.hh / 2;
      const amp = o.hh / 2 - 1.5;
      const nB = pv.max.length;
      const s0 = c.src_offset, s1 = c.src_offset + g.srcSpan;
      const px = Math.max(1, Math.floor(w));
      ctx.strokeStyle = o.col;
      if (o.dim) ctx.globalAlpha = 0.45;
      ctx.beginPath();
      for (let i = 0; i < px; i++) {
        const st = s0 + (i + 0.5) / px * (s1 - s0);
        const b = Math.max(0, Math.min(nB - 1, Math.floor((st / g.dur) * nB)));
        const xa = Math.round(x0 + i) + 0.5;
        ctx.moveTo(xa, mid - pv.max[b] * amp);
        ctx.lineTo(xa, mid - pv.min[b] * amp);
      }
      ctx.stroke();
      ctx.globalAlpha = 1;
    } else {
      ctx.strokeStyle = p.laneLabel;   /* 波形未就绪：虚线占位 */
      ctx.setLineDash([3, 3]);
      ctx.strokeRect(x0 + 0.5, o.yTop + 0.5, Math.max(2, w) - 1, o.hh - 1);
      ctx.setLineDash([]);
    }
    if (!o.dim) {
      /* 淡入/淡出斜线 + 顶部角句柄（拖拽入口） */
      const fiPx = Math.min(w, (c.fade_in || 0) * v.pxPerSec);
      const foPx = Math.min(w, (c.fade_out || 0) * v.pxPerSec);
      ctx.strokeStyle = p.laneLabel;
      if (fiPx > 0.5) {
        ctx.beginPath(); ctx.moveTo(x0 + 0.5, o.yTop + o.hh - 0.5); ctx.lineTo(x0 + fiPx, o.yTop + 0.5); ctx.stroke();
      }
      if (foPx > 0.5) {
        ctx.beginPath(); ctx.moveTo(x1 - foPx, o.yTop + 0.5); ctx.lineTo(x1 - 0.5, o.yTop + o.hh - 0.5); ctx.stroke();
      }
      ctx.fillStyle = p.keySep;
      ctx.fillRect(x0, o.yTop, 5, 5);
      ctx.fillRect(Math.max(x0 + 5, x1 - 5), o.yTop, 5, 5);
    }
    ctx.strokeStyle = p.keySep;
    ctx.strokeRect(x0 + 0.5, o.yTop + 0.5, Math.max(2, w) - 1, o.hh - 1);
    if (!o.dim && w > 64 && o.label) {
      const st = (c.stretch && Math.abs(c.stretch - 1) > 1e-6) ? (' ×' + Number(c.stretch).toFixed(2)) : '';
      ctx.fillStyle = p.laneLabel;
      ctx.font = FONT_UI;
      ctx.fillText('🎵 ' + o.label + st, x0 + 5, o.yTop + 12);
    }
  });
}
