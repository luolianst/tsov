/* lanes.js —— 总谱预览（Cubase 逻辑，UI 修正轮2）
   每轨一条 lane：行高与左栏轨道行同步（--row-h；滚动联动由 main.js 负责）
   交互：单击选中 ｜ Ctrl/Shift 单击 = 叠加集 ｜ 双击 = 进入单轨写谱
   音频轨（E2/E6）：拖块移动 ｜ 拖缘修剪 ｜ 顶角句柄淡化 ｜ Alt+拖右缘伸缩 ｜ 剪刀/Alt 单击切分
   音符编辑在单轨视图（roll.js）；音频 clip 手势经命令层落盘（ADR-0017） */

import { bus } from './events.js';
import { store, tempo, beatsPerBar, setSelection, toggleOverlay, setView, bookmarks, setSelBookmark, setError, audioClipsOf } from './state.js';
import { pal, trackColors } from './theme.js';
import { seekTo } from './playback.js';
import { api } from './api.js';
import { peaksGet } from './peaks.js';   /* E3 段1：波形峰值公共模块（原本地实现已抽走） */

const KEYS = 56;   // 左侧标签槽（与卷帘 KEYS_W 对齐）
const FONT_UI = '11px "Microsoft YaHei UI","PingFang SC","MiSans","HarmonyOS Sans SC",system-ui,sans-serif';

let canvas, ctx, W = 0, H = 0, dpr = 1;
let onEnter = null;   // 双击回调（main.js 注入）
let onDropAudio = null;   // M-V8 E2：文件拖入回调（main.js 注入）

function rowH() {
  const v = getComputedStyle(document.documentElement).getPropertyValue('--row-h');
  return Math.max(24, parseInt(v, 10) || 46);
}

/* UI 修正轮3：纵向滚动偏移（由左栏 #tracks-scroll 驱动；卷帘自身滚轮语义不变）
   3.1：maxScrollOv = 左栏滚动上限——卷帘范围与左栏一致（滚过行区后下方留白），任何滚动位置行都逐行匹配 */
let scrollY = 0;
let maxScrollOv = 0;
/* UI 修正轮3.2：左栏真实行布局（文件夹行/折叠/行高差异 → 卷帘逐行匹配；由 main.js 收集推送）
   rowMap[trackIndex] = y（相对卷帘内容顶）｜null = 该轨不占行（折叠/隐藏） */
let rowMap = null;

/* 行布局查询：无表时回退等距假设 */
function rowHidden(ti) {
  return !!(rowMap && Object.prototype.hasOwnProperty.call(rowMap, ti) && rowMap[ti] == null);
}
function rowY(ti) {
  if (rowMap && rowMap[ti] != null) return rowMap[ti];
  return ti * rowH();
}

function resize() {
  dpr = window.devicePixelRatio || 1;
  const r = canvas.getBoundingClientRect();
  W = Math.max(10, Math.floor(r.width));
  H = Math.max(10, Math.floor(r.height));
  canvas.width = Math.floor(W * dpr);
  canvas.height = Math.floor(H * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  /* UI 修正轮3：可视高变化后校正滚动上限（3.1：上限与左栏一致，含 maxScrollOv） */
  const n = (store.score && store.score.tracks.length) || 0;
  scrollY = Math.max(0, Math.min(scrollY, Math.max(maxScrollOv, Math.max(0, n * rowH() - H))));
  store.lanesScroll = scrollY;
}

function xOf(t) { return KEYS + (t - store.view.scrollSec) * store.view.pxPerSec; }
function tOf(x) { return store.view.scrollSec + (x - KEYS) / store.view.pxPerSec; }
function laneAt(y) {
  const sc = store.score;
  if (!sc) return -1;
  const yy = y + scrollY;
  for (let i = 0; i < sc.tracks.length; i++) {
    if (rowHidden(i)) continue;
    const ry = rowY(i);
    if (yy >= ry && yy < ry + rowH()) return i;
    if (ry > yy) break;   /* 行按 y 递增；越过即无命中 */
  }
  return -1;
}

/* ---- M-V8 E2/E6：音频素材（多 clip：波形 / 移动 / 修剪 / 淡入淡出 / 伸缩 / 切分） ----
   手势：拖块=移动 ｜ 拖左右缘=修剪 ｜ 拖顶角=淡入/淡出 ｜ Alt+拖右缘=伸缩（0.5–2.0 保音高）
        剪刀工具单击 / Alt+单击 = 切分（网格吸附；Alt=自由）
   全部落盘经命令层 set_audio_clips / split_audio_clip（ADR-0017 同一动作路径）。 */

const EDGE = 6;          // 左右缘命中带宽（px）
const CORNER = 11;       // 顶部角句柄带高（px）
const MIN_SPAN = 0.02;   // clip 最短时间线长（秒）

let drag = null;      // {ti, mode, idx, startX, alt, base, preview, moved}
let selClip = null;   // 最近操作 clip {ti, idx}（高亮）

/* E3 段1：波形峰值 → 公共模块 js/peaks.js（与单轨波形共用；未就绪返回 null 并触发拉取，拉回后经 bus 'peaks' 重绘） */

function r6(x) { return Math.round(Number(x) * 1e6) / 1e6; }

function fileDur(rel) {
  const pv = peaksGet(rel);
  return pv && pv.seconds ? pv.seconds : null;
}

/* clip 显示几何：{x0, w, span, srcSpan, dur}（span=null → 素材时长/区间未知） */
function clipGeom(c) {
  const x0 = xOf(c.start);
  const dur = fileDur(c.file);
  let srcSpan = null;
  if (c.src_len != null) srcSpan = dur == null ? c.src_len : Math.min(c.src_len, Math.max(0, dur - c.src_offset));
  else if (dur != null) srcSpan = Math.max(0, dur - c.src_offset);
  const span = srcSpan == null ? null : srcSpan * (c.stretch || 1);
  const w = span == null ? null : Math.max(2, span * store.view.pxPerSec);
  return { x0, w, span, srcSpan, dur };
}

function spanOf(c) { return clipGeom(c).span; }

/* 网格吸附（free=true 原样返回；否则节拍网格——单轨 Grid 值设过则沿用其比例） */
function snapTime(t, free) {
  t = Math.max(0, Number(t) || 0);
  if (free) return t;
  const beat = 60 / tempo();
  const grid = (store.snapFrac && store.snapFrac > 0) ? beat * store.snapFrac : beat;
  return Math.max(0, Math.round(t / grid) * grid);
}

/* 命中（含 zone）：move | trim-in | trim-out | fade-in | fade-out（Alt+右缘 → 由按下处升级 stretch） */
function clipHit(ti, offX, offY) {
  const trk = store.score && store.score.tracks[ti];
  if (!trk || trk.kind !== 'audio') return null;
  const rh = rowH();
  const y0 = rowY(ti) - scrollY;
  const pad = Math.max(3, rh * 0.10);
  const yTop = y0 + pad;
  const yBot = y0 + rh - pad;
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
async function splitClipAt(ti, idx, t0, free) {
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

function beginAudioDrag(ti, hit, e) {
  const trk = store.score.tracks[ti];
  const base = audioClipsOf(trk).map((c) => Object.assign({}, c));
  const mode = (e.altKey && hit.zone === 'trim-out') ? 'stretch' : hit.zone;
  drag = { ti, mode, idx: hit.idx, startX: e.clientX, alt: !!e.altKey,
           base, preview: base.map((c) => Object.assign({}, c)), moved: false };
  selClip = { ti, idx: hit.idx };
  canvas.style.cursor = 'grabbing';
}

function updateAudioDrag(e) {
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
  draw();
}

async function finishAudioDrag() {
  const d = drag;
  drag = null;
  canvas.style.cursor = '';
  if (!d || !d.moved || !store.project) { draw(); return; }
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
  const ti = laneAt(offY);
  if (ti < 0 || offY > rowY(ti) - scrollY + 16) return null;   /* UI 修正轮3.2：命中区随行布局 */
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
    if (rowHidden(ti)) continue;   /* UI 修正轮3.2：折叠/隐藏 → 与左栏一致不占行 */
    const y0 = rowY(ti) - scrollY;   /* UI 修正轮3.2：行布局 y（文件夹行/行高差异已计入） */
    if (y0 + rh < 0) continue;
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
    if (rowHidden(ti)) continue;   /* UI 修正轮3.2：折叠/隐藏 → 不占行（与左栏一致） */
    const y0 = rowY(ti) - scrollY;   /* UI 修正轮3.2：行布局 y */
    if (y0 + rh < 0) continue;
    if (y0 > H) break;
    if (!trackVisible(ti)) continue;   // M-V8 E1：隐藏/文件夹折叠 → 不画音符
    const trk = sc.tracks[ti];
    if (trk.kind === 'audio') {   /* M-V8 E2/E6：音频轨 → 多 clip 波形块
         拖块=移动 ｜ 拖缘=修剪 ｜ 顶角句柄=淡化 ｜ Alt+右缘=伸缩 ｜ 剪刀/Alt 单击=切分 */
      const inDrag = drag && drag.ti === ti;
      const list = inDrag ? drag.preview : audioClipsOf(trk);
      const col = tc[ti % tc.length];
      const yTop = y0 + pad;
      const hh = inner;
      list.forEach((c, idx) => {
        const g = clipGeom(c);
        const w = g.w == null ? 3 * v.pxPerSec : g.w;   /* 素材时长未知：占位宽（peaks 拉回后重绘） */
        const x0 = g.x0, x1 = x0 + w;
        if (x1 < KEYS || x0 > W) return;
        const isSel = selClip && selClip.ti === ti && selClip.idx === idx;
        ctx.fillStyle = col;
        ctx.globalAlpha = inDrag ? 0.30 : (isSel ? 0.26 : 0.16);
        ctx.fillRect(x0, yTop, w, hh);
        ctx.globalAlpha = 1;
        const pv = peaksGet(c.file);
        if (g.srcSpan != null && g.dur && pv && pv.max && pv.max.length) {
          /* 波形：源区间 [src_offset, +srcSpan) 映射到块内像素（min/max 竖线） */
          const mid = yTop + hh / 2;
          const amp = hh / 2 - 1.5;
          const nB = pv.max.length;
          const s0 = c.src_offset, s1 = c.src_offset + g.srcSpan;
          const px = Math.max(1, Math.floor(w));
          ctx.strokeStyle = col;
          ctx.beginPath();
          for (let i = 0; i < px; i++) {
            const st = s0 + (i + 0.5) / px * (s1 - s0);
            const b = Math.max(0, Math.min(nB - 1, Math.floor((st / g.dur) * nB)));
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
        /* 淡入/淡出斜线 + 顶部角句柄（拖拽入口） */
        const fiPx = Math.min(w, (c.fade_in || 0) * v.pxPerSec);
        const foPx = Math.min(w, (c.fade_out || 0) * v.pxPerSec);
        ctx.strokeStyle = p.laneLabel;
        if (fiPx > 0.5) {
          ctx.beginPath(); ctx.moveTo(x0 + 0.5, yTop + hh - 0.5); ctx.lineTo(x0 + fiPx, yTop + 0.5); ctx.stroke();
        }
        if (foPx > 0.5) {
          ctx.beginPath(); ctx.moveTo(x1 - foPx, yTop + 0.5); ctx.lineTo(x1 - 0.5, yTop + hh - 0.5); ctx.stroke();
        }
        ctx.fillStyle = p.keySep;
        ctx.fillRect(x0, yTop, 5, 5);
        ctx.fillRect(Math.max(x0 + 5, x1 - 5), yTop, 5, 5);
        ctx.strokeStyle = p.keySep;
        ctx.strokeRect(x0 + 0.5, yTop + 0.5, Math.max(2, w) - 1, hh - 1);
        if (w > 64) {
          const st = (c.stretch && Math.abs(c.stretch - 1) > 1e-6) ? (' ×' + Number(c.stretch).toFixed(2)) : '';
          ctx.fillStyle = p.laneLabel;
          ctx.font = FONT_UI;
          ctx.fillText('🎵 ' + (trk.name || '素材') + st, x0 + 5, yTop + 12);
        }
      });
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
      if (host < 0 || rowHidden(host) || rowY(host) - scrollY > H || rowY(host) - scrollY + 16 < 0 || !trackVisible(host)) continue;
      const x = Math.round(xOf(b.start));
      if (x < KEYS - 4 || x > W) continue;
      const y0 = rowY(host) - scrollY;   /* UI 修正轮3.2：随行布局 */
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
    /* E3 段1：叠加集（Ctrl/Shift 单击）对音频轨同样生效（跨类型叠加 → 单轨分屏） */
    if (e.ctrlKey || e.shiftKey || e.metaKey) { toggleOverlay(ti); return; }
    const trk = store.score && store.score.tracks[ti];
    if (trk && trk.kind === 'audio') {   /* M-V8 E2/E6：音频轨手势（移动/修剪/淡化/伸缩/切分） */
      const hit = clipHit(ti, e.offsetX, e.offsetY);
      if (hit) {
        const stretchHold = e.altKey && hit.zone === 'trim-out';
        if ((store.tool === 'scissors' || e.altKey) && !stretchHold) {
          /* 剪刀工具 / Alt 快捷：单击 → 切分（吸附；Alt=自由） */
          splitClipAt(ti, hit.idx, tOf(e.offsetX), e.altKey);
          return;
        }
        beginAudioDrag(ti, hit, e);
        return;
      }
      selClip = null;
      setSelection(ti, []);
      return;
    }
    setSelection(ti, []);
  });

  /* M-V8 E1：小旗悬停提示 ｜ E6：音频块手势与光标反馈 */
  canvas.addEventListener('mousemove', (e) => {
    if (drag) {   /* M-V8 E6：音频手势（预览跟手，松手提交） */
      updateAudioDrag(e);
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
      return;
    }
    hideTip();
    /* M-V8 E6：音频块命中 → 光标语义（grab 移动 / ew-resize 修剪 / col-resize 伸缩 / cell 切分） */
    const ti = laneAt(e.offsetY);
    const trk = ti >= 0 && store.score ? store.score.tracks[ti] : null;
    let cur = '';
    if (trk && trk.kind === 'audio') {
      const hit = clipHit(ti, e.offsetX, e.offsetY);
      if (hit) {
        if (e.altKey && hit.zone === 'trim-out') cur = 'col-resize';
        else if (hit.zone === 'move') cur = (store.tool === 'scissors' || e.altKey) ? 'cell' : 'grab';
        else cur = 'ew-resize';
      }
    }
    canvas.style.cursor = cur;
  });
  canvas.addEventListener('mouseleave', hideTip);

  canvas.addEventListener('dblclick', (e) => {
    const ti = laneAt(e.offsetY);
    if (ti < 0) return;
    /* E3 段1：音频轨同样进单轨 → 主轨波形视图（handoff #183①）；试听移至单轨波形区双击 */
    if (onEnter) onEnter(ti);
  });

  /* M-V8 E2/E6：音频手势（松手提交） + 文件拖入导入 */
  window.addEventListener('mouseup', () => { if (drag) finishAudioDrag(); });
  canvas.addEventListener('dragover', (e) => { e.preventDefault(); });
  canvas.addEventListener('drop', (e) => {
    e.preventDefault();
    const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    if (f && onDropAudio) onDropAudio(f);
  });

  for (const topic of ['state', 'view', 'selection', 'viewmode', 'playhead', 'playing', 'agenttracks', 'markers', 'peaks']) {
    bus.on(topic, draw);
  }
}

/* 修正轮2：外部（main.js 切换显隐后）主动重算尺寸 + 重绘 */
export function resizeNow() {
  if (!canvas) return;
  resize();
  draw();
}

/* UI 修正轮3：总谱纵向滚动偏移（左栏轨道面板驱动；__tsovState 快照观测用）
   3.1：maxOverride = 左栏滚动上限（含信息区）→ 卷帘范围与左栏一致，滚到底两边仍逐行匹配 */
export function setScrollY(y, maxOverride) {
  if (maxOverride != null) maxScrollOv = Math.max(0, Number(maxOverride) || 0);
  const n = (store.score && store.score.tracks.length) || 0;
  const max = Math.max(maxScrollOv, Math.max(0, n * rowH() - H));
  const v = Math.max(0, Math.min(Number(y) || 0, max));
  store.lanesScroll = v;
  if (v === scrollY) return;
  scrollY = v;
  draw();
}

/* UI 修正轮3.2：左栏真实行布局（main.js 收集推送；文件夹行/折叠/行高差异 → 两栏逐行匹配） */
export function setRowLayout(map) {
  rowMap = (map && typeof map === 'object') ? map : null;
  store.laneRows = rowMap;   /* __tsovState 快照观测用 */
  draw();
}
