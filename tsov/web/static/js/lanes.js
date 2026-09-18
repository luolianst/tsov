/* lanes.js —— 总谱预览（Cubase 逻辑，UI 修正轮2）
   每轨一条 lane：行高与左栏轨道行同步（--row-h；滚动联动由 main.js 负责）
   交互：单击选中 ｜ Ctrl/Shift 单击 = 叠加集 ｜ 双击 = 进入单轨写谱
   只读：编辑都在单轨视图（roll.js） */

import { bus } from './events.js';
import { store, tempo, beatsPerBar, setSelection, toggleOverlay, setView } from './state.js';
import { pal, trackColors } from './theme.js';

const KEYS = 56;   // 左侧标签槽（与卷帘 KEYS_W 对齐）
const FONT_UI = '11px "Microsoft YaHei UI","PingFang SC","MiSans","HarmonyOS Sans SC",system-ui,sans-serif';

let canvas, ctx, W = 0, H = 0, dpr = 1;
let onEnter = null;   // 双击回调（main.js 注入）

function rowH() {
  const v = getComputedStyle(document.documentElement).getPropertyValue('--row-h');
  return Math.max(24, parseInt(v, 10) || 46);
}

function resize() {
  dpr = window.devicePixelRatio || 1;
  const r = canvas.getBoundingClientRect();
  W = Math.max(10, Math.floor(r.width));
  H = Math.max(10, Math.floor(r.height));
  canvas.width = Math.floor(W * dpr);
  canvas.height = Math.floor(H * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}

function xOf(t) { return KEYS + (t - store.view.scrollSec) * store.view.pxPerSec; }
function tOf(x) { return store.view.scrollSec + (x - KEYS) / store.view.pxPerSec; }
function laneAt(y) {
  const i = Math.floor(y / rowH());
  return (store.score && i >= 0 && i < store.score.tracks.length) ? i : -1;
}

export function draw() {
  if (!ctx) return;
  ctx.clearRect(0, 0, W, H);
  const p = pal();
  const sc = store.score;
  const rh = rowH();

  if (!sc || !sc.tracks.length) {
    ctx.fillStyle = p.laneLabel;
    ctx.font = FONT_UI;
    ctx.fillText('（无音轨）', 12, 22);
    return;
  }

  const tc = trackColors();
  const v = store.view;
  const beat = 60 / tempo();
  const bq = beatsPerBar();

  /* ---- 行背景 + 标签槽（第一遍） ---- */
  for (let ti = 0; ti < sc.tracks.length; ti++) {
    const y0 = ti * rh;
    if (y0 > H) break;
    const selected = store.selection.track === ti;
    const overlay = store.overlayTracks.has(ti);
    const touched = store.agentTracks.has(ti);   // 批B B1-3：本轮 agent 改动过
    ctx.fillStyle = selected ? p.selSoft : (overlay ? p.rowBlack : p.rollBg);
    ctx.fillRect(0, y0, W, rh);
    /* 标签槽 */
    ctx.fillStyle = p.panelBg;
    ctx.fillRect(0, y0, KEYS, rh);
    ctx.fillStyle = tc[ti % tc.length];
    ctx.fillRect(0, y0, 4, rh);
    ctx.fillStyle = p.laneLabel;
    ctx.font = FONT_UI;
    const nm = sc.tracks[ti].name || ('track ' + ti);
    ctx.fillText(nm.length > 8 ? nm.slice(0, 8) + '…' : nm, 9, y0 + Math.min(rh - 8, rh / 2 + 4));
    if (overlay) ctx.fillText('叠加', 9, y0 + rh - 8);
    if (touched) {   /* 批B B1-3：改动标记（标签槽右缘小方块，agent 语义色） */
      ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--agent').trim() || '#141414';
      ctx.fillRect(KEYS - 9, y0 + rh / 2 - 3, 5, 6);
    }
    /* 行分隔线 */
    ctx.strokeStyle = p.beatLine;
    ctx.beginPath(); ctx.moveTo(0, y0 + rh + 0.5); ctx.lineTo(W, y0 + rh + 0.5); ctx.stroke();
    /* 标签槽右界 */
    ctx.strokeStyle = p.keySep;
    ctx.beginPath(); ctx.moveTo(KEYS - 3.5, y0); ctx.lineTo(KEYS - 3.5, y0 + rh); ctx.stroke();
  }

  /* ---- 竖网格（第二遍，垫在音符下） ---- */
  const t0 = Math.max(0, tOf(KEYS));
  const t1 = tOf(W);
  let tEnd = 0;
  for (const tr of sc.tracks) for (const n of tr.notes) if (n.end > tEnd) tEnd = n.end;
  const tMax = Math.max(t1, tEnd + 2);
  ctx.lineWidth = 1;
  let k = Math.floor(t0 / beat);
  for (; k * beat <= tMax; k++) {
    const t = k * beat;
    const x = Math.round(xOf(t)) + 0.5;
    if (x < KEYS) continue;
    if (x > W) break;
    if (Math.abs(k / bq - Math.round(k / bq)) < 1e-6) {
      ctx.strokeStyle = p.barLine;
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke();
    } else if (v.pxPerSec * beat > 7) {
      ctx.strokeStyle = p.beatLine;
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke();
    }
  }

  /* ---- 音符（第三遍；轨内音域自适应行高） ---- */
  const pad = Math.max(3, rh * 0.10);
  const inner = Math.max(8, rh - pad * 2 - 2);
  const nh = Math.max(3, Math.min(9, rh * 0.20));
  for (let ti = 0; ti < sc.tracks.length; ti++) {
    const y0 = ti * rh;
    if (y0 > H) break;
    const notes = sc.tracks[ti].notes;
    if (!notes.length) continue;
    const overlay = store.overlayTracks.has(ti);
    const selected = store.selection.track === ti;
    let lo = 127, hi = 0;
    for (const n of notes) { if (n.pitch_midi < lo) lo = n.pitch_midi; if (n.pitch_midi > hi) hi = n.pitch_midi; }
    if (hi <= lo) hi = lo + 1;
    const yOfM = (m) => y0 + pad + (hi - m) / (hi - lo) * (inner - nh);
    ctx.fillStyle = tc[ti % tc.length];
    ctx.globalAlpha = overlay ? 0.32 : (selected ? 0.95 : 0.78);
    for (const n of notes) {
      const x = xOf(n.start);
      const w = Math.max(2, (n.end - n.start) * v.pxPerSec);
      if (x + w < KEYS || x > W) continue;
      const y = yOfM(n.pitch_midi);
      if (ctx.roundRect) {
        ctx.beginPath();
        ctx.roundRect(x, y, w, nh, 2);
        ctx.fill();
      } else {
        ctx.fillRect(x, y, w, nh);
      }
    }
    ctx.globalAlpha = 1;
  }

  /* ---- 播放头 ---- */
  const px = xOf(store.playhead);
  if (store.playing && px >= KEYS && px <= W) {
    ctx.strokeStyle = p.playhead;
    ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.moveTo(px, 0); ctx.lineTo(px, H); ctx.stroke();
    ctx.lineWidth = 1;
  }
}

export function init(el, opts) {
  canvas = el;
  onEnter = (opts && opts.onEnter) || null;
  ctx = canvas.getContext('2d');
  resize();
  new ResizeObserver(() => { resize(); draw(); }).observe(canvas);

  canvas.addEventListener('wheel', (e) => {
    e.preventDefault();
    if (e.ctrlKey) {
      const anchorT = tOf(e.offsetX);
      const f = e.deltaY < 0 ? 1.15 : 1 / 1.15;
      const px = Math.min(600, Math.max(8, store.view.pxPerSec * f));
      setView({ pxPerSec: px, scrollSec: Math.max(0, anchorT - (e.offsetX - KEYS) / px) });
    } else {
      setView({ scrollSec: Math.max(0, store.view.scrollSec + e.deltaY * 0.02) });
    }
  }, { passive: false });

  canvas.addEventListener('mousedown', (e) => {
    if (e.button !== 0) return;
    const ti = laneAt(e.offsetY);
    if (ti < 0) return;
    if (e.ctrlKey || e.shiftKey || e.metaKey) { toggleOverlay(ti); return; }
    setSelection(ti, []);
  });

  canvas.addEventListener('dblclick', (e) => {
    const ti = laneAt(e.offsetY);
    if (ti < 0) return;
    if (onEnter) onEnter(ti);
  });

  for (const topic of ['state', 'view', 'selection', 'viewmode', 'playhead', 'playing', 'agenttracks']) {
    bus.on(topic, draw);
  }
}

/* 修正轮2：外部（main.js 切换显隐后）主动重算尺寸 + 重绘 */
export function resizeNow() {
  if (!canvas) return;
  resize();
  draw();
}
