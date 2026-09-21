/* lanes.js —— 总谱预览（Cubase 逻辑，UI 修正轮2）
   每轨一条 lane：行高与左栏轨道行同步（--row-h；滚动联动由 main.js 负责）
   交互：单击选中 ｜ Ctrl/Shift 单击 = 叠加集 ｜ 双击 = 进入单轨写谱
   只读：编辑都在单轨视图（roll.js） */

import { bus } from './events.js';
import { store, tempo, beatsPerBar, setSelection, toggleOverlay, setView, bookmarks, setSelBookmark, setError } from './state.js';
import { pal, trackColors } from './theme.js';
import { seekTo } from './playback.js';
import { api } from './api.js';

const KEYS = 56;   // 左侧标签槽（与卷帘 KEYS_W 对齐）
const FONT_UI = '11px "Microsoft YaHei UI","PingFang SC","MiSans","HarmonyOS Sans SC",system-ui,sans-serif';

let canvas, ctx, W = 0, H = 0, dpr = 1;
let onEnter = null;   // 双击回调（main.js 注入）
let onDropAudio = null;   // M-V8 E2：文件拖入回调（main.js 注入）

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

/* ---- M-V8 E2：音频素材（波形块 / 拖动 / 试听） ---- */

const peaksCache = new Map();   // `${project}|${rel}` → {seconds,min,max}（内容寻址文件名 = 天然失效键）
const peaksPending = new Set();
let drag = null;                // {ti, startX, baseOffset, curOffset, moved}
let preview = null;             // 试听 Audio 单例

/** 波形峰值缓存；未就绪时触发一次拉取并返回 null（拉回后重绘）。 */
function peaksGet(rel) {
  if (!rel || !store.project) return null;
  const key = store.project + '|' + rel;
  const hit = peaksCache.get(key);
  if (hit) return hit;
  if (!peaksPending.has(key)) {
    peaksPending.add(key);
    api.fetchPeaks(store.project, rel, 900)
      .then((d) => { if (d && d.max && d.max.length) { peaksCache.set(key, d); draw(); } })
      .catch(() => { /* 拿不到波形 → 画占位框 */ })
      .finally(() => peaksPending.delete(key));
  }
  return null;
}

function round3(x) { return Math.round(Number(x) * 1000) / 1000; }

function previewAudio(trk) {
  const rel = trk.audio && trk.audio.file;
  if (!rel || !store.project) return;
  if (preview) { preview.pause(); preview = null; }
  preview = new Audio(api.audioUrl(store.project, rel));
  preview.play()
    .then(() => bus.dispatch('toast', '试听素材：' + (trk.name || rel)))
    .catch((e) => setError('试听失败：' + (e && e.message ? e.message : e)));
}

function commitAudioDrag() {
  const d = drag;
  drag = null;
  if (!d || !d.moved || !store.project) { draw(); return; }
  const off = round3(d.curOffset);
  api.postBatch(store.project, '移动音频素材', [{ op: 'set_audio_track', track: d.ti, value: { offset: off } }], null)
    .then((r) => {
      if (r.applied) bus.dispatch('toast', '素材已移动 → ' + off.toFixed(2) + 's');
      else setError('被拒：' + (r.errors || []).join('；'));
    })
    .catch((e) => setError(e.message));
  draw();
}

/* ---- M-V8 E1：书签小旗（轨道/文件夹层）+ 可见性（隐藏/文件夹折叠） ---- */

function trackVisible(ti) {
  if (store.hiddenTracks.has(ti)) return false;
  const tr = store.score && store.score.tracks[ti];
  return !(tr && tr.folder && store.collapsedFolders.has(tr.folder));
}

/** 命中 lane 顶缘的小旗（±9px）→ {i, b} 或 null。 */
function flagAt(offX, offY) {
  const sc = store.score;
  if (!sc) return null;
  const rh = rowH();
  const ti = laneAt(offY);
  if (ti < 0 || offY > ti * rh + 16) return null;
  const folderHost = {};
  sc.tracks.forEach((t, i) => { if (t.folder && folderHost[t.folder] === undefined) folderHost[t.folder] = i; });
  const list = [];
  bookmarks().forEach((b, i) => { if (b.scope === 'track' || b.scope === 'folder') list.push({ b, i }); });
  for (const { b, i } of list) {
    let host = -1;
    if (b.scope === 'track') host = sc.tracks.findIndex((t) => t.name === b.ref);
    else host = (folderHost[b.ref] !== undefined) ? folderHost[b.ref] : -1;
    if (host !== ti || !trackVisible(ti)) continue;
    if (Math.abs(offX - xOf(b.start)) <= 9) return { i, b };
  }
  return null;
}

let tipEl = null;

function ensureTip() {
  if (!tipEl) {
    tipEl = document.createElement('div');
    tipEl.className = 'bm-tip';
    document.body.appendChild(tipEl);
  }
  return tipEl;
}

function hideTip() { if (tipEl) tipEl.style.display = 'none'; }

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
    ctx.globalAlpha = trackVisible(ti) ? 1 : 0.4;   // M-V8 E1：隐藏/折叠行减淡
    ctx.fillText((sc.tracks[ti].folder ? '📁' : '') + (nm.length > 8 ? nm.slice(0, 8) + '…' : nm), 9, y0 + Math.min(rh - 8, rh / 2 + 4));
    ctx.globalAlpha = 1;
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
    if (!trackVisible(ti)) continue;   // M-V8 E1：隐藏/文件夹折叠 → 不画音符
    const trk = sc.tracks[ti];
    if (trk.kind === 'audio') {   /* M-V8 E2：音频轨 → 波形块（拖块改 offset，双击试听） */
      const off = (drag && drag.ti === ti) ? drag.curOffset : ((trk.audio && trk.audio.offset) || 0);
      const pv = peaksGet(trk.audio && trk.audio.file);
      const secs = pv ? pv.seconds : 0;
      const x0 = xOf(off);
      const w = Math.max(2, secs * v.pxPerSec);
      if (x0 + w < KEYS || x0 > W) continue;
      const yTop = y0 + pad;
      const hh = inner;
      const col = tc[ti % tc.length];
      ctx.fillStyle = col;
      ctx.globalAlpha = (drag && drag.ti === ti) ? 0.30 : 0.16;
      ctx.fillRect(x0, yTop, w, hh);
      ctx.globalAlpha = 1;
      if (pv && pv.max && pv.max.length) {
        /* 波形：min/max 竖线（按像素抽样） */
        const mid = yTop + hh / 2;
        const amp = hh / 2 - 1.5;
        const nB = pv.max.length;
        const px = Math.max(1, Math.floor(w));
        ctx.strokeStyle = col;
        ctx.beginPath();
        for (let i = 0; i < px; i++) {
          const b = Math.min(nB - 1, Math.floor((i / px) * nB));
          const xa = Math.round(x0 + i) + 0.5;
          ctx.moveTo(xa, mid - pv.max[b] * amp);
          ctx.lineTo(xa, mid - pv.min[b] * amp);
        }
        ctx.stroke();
      } else {
        ctx.strokeStyle = p.laneLabel;   /* 波形未就绪：虚线占位 */
        ctx.setLineDash([3, 3]);
        ctx.strokeRect(x0 + 0.5, yTop + 0.5, Math.max(2, w) - 1, hh - 1);
        ctx.setLineDash([]);
      }
      ctx.strokeStyle = p.keySep;
      ctx.strokeRect(x0 + 0.5, yTop + 0.5, Math.max(2, w) - 1, hh - 1);
      if (w > 64) {
        ctx.fillStyle = p.laneLabel;
        ctx.font = FONT_UI;
        ctx.fillText('🎵 ' + (trk.name || '素材'), x0 + 5, yTop + 12);
      }
      continue;
    }
    const notes = trk.notes;
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

  /* ---- M-V8 E1：循环区间淡色带 ---- */
  const L = store.loop;
  if (L) {
    const xa = Math.max(KEYS, xOf(L.start)), xb = Math.min(W, xOf(L.end));
    if (xb > xa) {
      ctx.globalAlpha = store.loopOn ? 0.10 : 0.05;
      ctx.fillStyle = p.playhead;
      ctx.fillRect(xa, 0, xb - xa, H);
      ctx.globalAlpha = 1;
    }
  }

  /* ---- M-V8 E1：书签小旗（轨道层三角旗 / 文件夹层方旗，挂在 lane 顶缘） ---- */
  {
    const folderHost = {};
    sc.tracks.forEach((tr, i) => { if (tr.folder && folderHost[tr.folder] === undefined) folderHost[tr.folder] = i; });
    for (const b of bookmarks()) {
      if (b.scope !== 'track' && b.scope !== 'folder') continue;   // 项目层在段道
      let host = -1;
      if (b.scope === 'track') host = sc.tracks.findIndex((t) => t.name === b.ref);
      else host = (folderHost[b.ref] !== undefined) ? folderHost[b.ref] : -1;
      if (host < 0 || host * rh > H || !trackVisible(host)) continue;
      const x = Math.round(xOf(b.start));
      if (x < KEYS - 4 || x > W) continue;
      const y0 = host * rh;
      const isFolder = b.scope === 'folder';
      ctx.fillStyle = isFolder ? p.rulerText : tc[host % tc.length];
      ctx.fillRect(x, y0 + 2, 1, 10);
      if (isFolder) {
        ctx.fillRect(x + 1, y0 + 2, 7, 6);
      } else {
        ctx.beginPath();
        ctx.moveTo(x + 1, y0 + 2); ctx.lineTo(x + 9, y0 + 5); ctx.lineTo(x + 1, y0 + 8);
        ctx.closePath(); ctx.fill();
      }
    }
  }

  /* ---- 播放头 ---- */
  const px = xOf(store.playhead);
  if (px >= KEYS && px <= W) {
    ctx.strokeStyle = p.playhead;
    ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.moveTo(px, 0); ctx.lineTo(px, H); ctx.stroke();
    ctx.lineWidth = 1;
  }
}

export function init(el, opts) {
  canvas = el;
  onEnter = (opts && opts.onEnter) || null;
  onDropAudio = (opts && opts.onDropAudio) || null;
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
    if (drag) return;   // 拖动中不重入
    const fl = flagAt(e.offsetX, e.offsetY);   // M-V8 E1：小旗 → 选中 + 跳转
    if (fl) { setSelBookmark(fl.i); seekTo(fl.b.start); return; }
    const ti = laneAt(e.offsetY);
    if (ti < 0) return;
    const trk = store.score && store.score.tracks[ti];
    if (trk && trk.kind === 'audio') {   /* M-V8 E2：块内按下 = 拖动改 offset；块外 = 选中 */
      const pv = peaksGet(trk.audio && trk.audio.file);
      const off = (trk.audio && trk.audio.offset) || 0;
      const secs = pv ? pv.seconds : 0;
      if (e.offsetX >= xOf(off) - 2 && e.offsetX <= xOf(off + secs) + 2) {
        drag = { ti, startX: e.clientX, baseOffset: off, curOffset: off, moved: false };
        canvas.style.cursor = 'grabbing';
        return;
      }
      setSelection(ti, []);
      return;
    }
    if (e.ctrlKey || e.shiftKey || e.metaKey) { toggleOverlay(ti); return; }
    setSelection(ti, []);
  });

  /* M-V8 E1：小旗悬停提示 */
  canvas.addEventListener('mousemove', (e) => {
    if (drag) {   /* M-V8 E2：拖动音频块（预览跟手，松手提交） */
      const dt = (e.clientX - drag.startX) / store.view.pxPerSec;
      drag.curOffset = Math.max(0, drag.baseOffset + dt);
      if (Math.abs(dt) > 0.002) drag.moved = true;
      draw();
      return;
    }
    const fl = flagAt(e.offsetX, e.offsetY);
    if (fl) {
      const t = ensureTip();
      t.textContent = (fl.b.scope === 'folder' ? '📁 ' : '') + (fl.b.label || '（未命名）') +
        ' @ ' + fl.b.start.toFixed(2) + 's ｜ 单击跳转';
      t.style.display = 'block';
      t.style.left = (e.clientX + 12) + 'px';
      t.style.top = (e.clientY + 14) + 'px';
      canvas.style.cursor = 'pointer';
    } else {
      hideTip();
      canvas.style.cursor = '';
    }
  });
  canvas.addEventListener('mouseleave', hideTip);

  canvas.addEventListener('dblclick', (e) => {
    const ti = laneAt(e.offsetY);
    if (ti < 0) return;
    const trk = store.score && store.score.tracks[ti];
    if (trk && trk.kind === 'audio') { previewAudio(trk); return; }   /* M-V8 E2：双击试听素材 */
    if (onEnter) onEnter(ti);
  });

  /* M-V8 E2：音频拖拽（松手提交）+ 文件拖入导入 */
  window.addEventListener('mouseup', () => { if (drag) commitAudioDrag(); });
  canvas.addEventListener('dragover', (e) => { e.preventDefault(); });
  canvas.addEventListener('drop', (e) => {
    e.preventDefault();
    const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    if (f && onDropAudio) onDropAudio(f);
  });

  for (const topic of ['state', 'view', 'selection', 'viewmode', 'playhead', 'playing', 'agenttracks', 'markers']) {
    bus.on(topic, draw);
  }
}

/* 修正轮2：外部（main.js 切换显隐后）主动重算尺寸 + 重绘 */
export function resizeNow() {
  if (!canvas) return;
  resize();
  draw();
}
