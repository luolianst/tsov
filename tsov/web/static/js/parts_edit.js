/* parts_edit.js —— Part 片段手势引擎（总谱 lanes / 单轨 roll 共用）
   UI 手势：拖 Part 顶条 = 移动 ｜ 拖左缘 = 左拉裁剪 ｜ 拖右缘 = 循环延伸/裁剪
            Alt+拖 = 复制到落点
   全部落盘经命令层 move_part / copy_part / resize_part（ADR-0017 同一动作路径）。 */
import { t } from './i18n.js';
import { bus } from './events.js';
import { store, tempo, setError, setSelection } from './state.js';
import { api } from './api.js';
import { xOf } from './geom.js';

const EDGE = 7;        // 左右缘热区（px）
const STRIP = 13;      // Part 顶条高度（px；与 drawPartBands 同口径）
const MIN_LEN = 0.05;  // 最短 Part 长度（秒）

let drag = null;
let sel = null;        // {ti, id}
let lastDraw = () => {};

function r6(x) { return Math.round(Number(x) * 1e6) / 1e6; }

export function partsOf(trk) {
  return (trk && Array.isArray(trk.parts)) ? trk.parts : [];
}
export function partSelOf() { return sel; }
export function clearPartSel() { sel = null; store.partSel = null; }
export function isPartDragging() { return !!drag; }
export function partPreviewOf(ti) { return (drag && drag.ti === ti) ? drag.preview : null; }
export function partPreviewFor(ti) {
  if (!drag) return null;
  if (drag.targetTi != null && ti === drag.targetTi) return [drag.targetPreview];
  return (drag.ti === ti) ? drag.preview : null;
}
export function partStripH() { return STRIP; }

/* 网格吸附（与音频 clip 手势同口径；Alt=自由） */
export function snapPartTime(v, free) {
  v = Math.max(0, Number(v) || 0);
  if (free) return r6(v);
  const beat = 60 / tempo();
  const grid = (store.snapFrac && store.snapFrac > 0) ? beat * store.snapFrac : beat;
  return r6(Math.max(0, Math.round(v / grid) * grid));
}

function geom(part) {
  const x0 = xOf(part.start);
  const w = Math.max(8, (Number(part.length) || 0) * store.view.pxPerSec);
  return { x0, w, x1: x0 + w };
}

/* 命中：只在 Part 顶条内（保留下方音符编辑）；zone=move/trim-in/trim-out */
export function partHitBand(ti, offX, offY, yTop, stripH = STRIP) {
  const trk = store.score && store.score.tracks[ti];
  if (!trk || trk.kind === 'audio') return null;
  if (offY < yTop - 2 || offY > yTop + stripH + 2) return null;
  const list = (drag && drag.ti === ti) ? drag.preview : partsOf(trk);
  for (let idx = list.length - 1; idx >= 0; idx--) {
    const p = list[idx];
    if (String(p.id || '').startsWith('__')) continue;   // 复制预览不参与命中
    const g = geom(p);
    if (offX < g.x0 - 2 || offX > g.x1 + 2) continue;
    let zone = 'move';
    if (Math.abs(offX - g.x0) <= EDGE) zone = 'trim-in';
    else if (Math.abs(offX - g.x1) <= EDGE) zone = 'trim-out';
    return { idx, part: p, zone };
  }
  return null;
}

export function beginPartDrag(ti, hit, e, drawFn, laneResolver = null) {
  const trk = store.score && store.score.tracks[ti];
  if (!trk) return;
  const base = partsOf(trk).map((p) => ({ ...p, notes: (p.notes || []).map((n) => ({ ...n })) }));
  const orig = base[hit.idx];
  if (!orig) return;
  drag = {
    ti, mode: hit.zone, idx: hit.idx, orig, base, preview: base,
    startX: e.clientX, startY: e.clientY, alt: !!e.altKey, moved: false,
    laneResolver, targetTi: null, targetPreview: null,
  };
  sel = { ti, id: orig.id };
  store.partSel = sel;
  setSelection(ti, []);
  if (typeof drawFn === 'function') lastDraw = drawFn;
}

export function updatePartDrag(e) {
  const d = drag;
  if (!d) return;
  const dt = (e.clientX - d.startX) / store.view.pxPerSec;
  const dy = e.clientY - d.startY;
  if (Math.abs(dt) > 0.002 || Math.abs(dy) > 2) d.moved = true;
  const o = d.orig;
  const end = o.start + o.length;
  let part = { ...o };
  if (d.mode === 'move') {
    part.start = snapPartTime(o.start + dt, d.alt);
  } else if (d.mode === 'trim-in') {
    part.start = Math.min(snapPartTime(o.start + dt, false), end - MIN_LEN);
    part.length = Math.max(MIN_LEN, end - part.start);
  } else if (d.mode === 'trim-out') {
    const e2 = Math.max(part.start + MIN_LEN, snapPartTime(end + dt, false));
    part.length = Math.max(MIN_LEN, e2 - part.start);
  }
  if (d.mode === 'move' && d.alt) {
    // 原 Part 保留，落点显示一份复制预览
    d.preview = d.base.concat([{ ...part, id: '__part_copy__' }]);
  } else {
    d.preview = d.base.map((p, i) => (i === d.idx ? part : p));
  }
  /* 跨轨移动：鼠标纵向进入另一条 MIDI 轨 */
  d.targetTi = null;
  d.targetPreview = null;
  if (d.mode === 'move' && !d.alt && typeof d.laneResolver === 'function') {
    const target = d.laneResolver(e.offsetY);
    if (target >= 0 && target !== d.ti) {
      const tr = store.score.tracks[target];
      if (tr && tr.kind !== 'audio') {
        d.targetTi = target;
        d.targetPreview = { ...part, id: '__part_target__' };
      }
    }
  }
  lastDraw();
}

export async function finishPartDrag() {
  const d = drag;
  drag = null;
  const fn = lastDraw;
  if (!d || !d.moved || !store.project) { if (fn) fn(); return; }
  const o = d.orig;
  const p = d.preview.find((x) => x.id === (d.alt && d.mode === 'move' ? '__part_copy__' : o.id));
  if (!p) { if (fn) fn(); return; }
  const label = d.alt && d.mode === 'move' ? t('复制片段')
    : (d.targetTi != null ? t('跨轨移动片段')
      : (d.mode === 'move' ? t('移动片段') : (d.mode === 'trim-in' ? t('裁剪片段') : t('延伸片段'))));
  const cmd = d.targetTi != null
    ? { op: 'move_part_to_track', track: d.ti,
        value: { part: o.id, target_track: d.targetTi, start: r6(p.start) } }
    : d.alt && d.mode === 'move'
    ? { op: 'copy_part', track: d.ti, value: { part: o.id, start: r6(p.start), length: r6(o.length) } }
    : d.mode === 'move'
      ? { op: 'move_part', track: d.ti, value: { part: o.id, start: r6(p.start) } }
      : d.mode === 'trim-in'
        ? { op: 'resize_part', track: d.ti, value: { part: o.id, start: r6(p.start), length: r6(p.length), mode: 'crop' } }
        : { op: 'resize_part', track: d.ti, value: { part: o.id, length: r6(p.length),
          mode: p.length > o.length ? 'loop' : 'crop' } };
  try {
    const r = await api.postBatch(store.project, label, [cmd], null);
    if (!r.applied) setError(t('被拒：') + (r.errors || []).join('；'));
    else {
      if (d.targetTi != null) sel = { ti: d.targetTi, id: o.id };
      if (sel) store.partSel = sel;
      bus.dispatch('toast', label + ' @ ' + r6(p.start ?? o.start).toFixed(2) + 's');
    }
  } catch (e) {
    setError(e.message);
  }
  if (fn) fn();
}

/* 顶条绘制：半透明底 + 左右缘把手 + 标签；o={list,col,yTop,h,selId,inDrag,p,v,xLo,xHi} */
export function drawPartBands(ctx, o) {
  const v = o.v || store.view;
  const p = o.p;
  const h = o.h || STRIP;
  (o.list || []).forEach((part, idx) => {
    const g = geom(part);
    if (g.x1 < o.xLo || g.x0 > o.xHi) return;
    const isPreview = String(part.id || '').startsWith('__');
    const isSel = o.selId && part.id === o.selId;
    ctx.fillStyle = o.col;
    ctx.globalAlpha = isPreview ? 0.24 : (o.inDrag ? 0.28 : (isSel ? 0.34 : 0.16));
    ctx.fillRect(g.x0, o.yTop, g.w, h);
    ctx.globalAlpha = 1;
    ctx.strokeStyle = isPreview ? p.playhead : (isSel ? p.selStroke : p.keySep);
    ctx.setLineDash(isPreview ? [3, 2] : []);
    ctx.strokeRect(g.x0 + 0.5, o.yTop + 0.5, Math.max(2, g.w) - 1, h - 1);
    ctx.setLineDash([]);
    /* 左右缘把手 */
    ctx.fillStyle = isPreview ? p.playhead : o.col;
    ctx.fillRect(g.x0, o.yTop, 3, h);
    ctx.fillRect(Math.max(g.x0 + 3, g.x1 - 3), o.yTop, 3, h);
    if (g.w > 24 && !isPreview) {
      ctx.fillStyle = p.laneLabel;
      ctx.font = '10px "Microsoft YaHei UI","PingFang SC",system-ui,sans-serif';
      ctx.fillText('▭ P' + (idx + 1), g.x0 + 5, o.yTop + h - 3);
    }
  });
}

/* 模块级 mouseup：视图只需在 mousedown 启动；松手统一提交 */
if (typeof window !== 'undefined') {
  window.addEventListener('mouseup', () => { if (drag) finishPartDrag(); });
  document.addEventListener('keydown', (e) => {
    if (!sel || !store.project) return;
    if (e.key !== 'Delete' && e.key !== 'Backspace') return;
    const tgt = e.target || {};
    const tag = String(tgt.tagName || '').toUpperCase();
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || tgt.isContentEditable) return;
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    e.preventDefault();
    (async () => {
      const s = sel;
      try {
        const r = await api.postBatch(store.project, t('删除片段'),
          [{ op: 'delete_part', track: s.ti, value: { part: s.id } }], null);
        if (!r.applied) setError(t('被拒：') + (r.errors || []).join('；'));
        else { sel = null; store.partSel = null; lastDraw(); }
      } catch (err) { setError(err.message); }
    })();
  });
}
