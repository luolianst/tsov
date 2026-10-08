/* roll.js —— 钢琴卷帘 canvas：绘制/缩放/滚动/点击选择/播放头/三色 diff 叠层 */

import { bus } from './events.js';
import { api } from './api.js';
import { KEYS_W, store, scoreBounds, tempo, beatsPerBar, setSelection, setView, refTag, setRange, splitPartner, setSplitRatio, setSingleTrack, fitViewTrack, audioClipsOf, setFocus, swapMainPartner } from './state.js';
import { peaksGet } from './peaks.js';   /* E3 段1：单轨波形峰值（公共管线） */
import { xOf, tOf } from './geom.js';   /* v0.2 批C 前段（R2 地基件）：时间↔x 几何共享 */
import { clipHitBand, beginAudioDrag, updateAudioDrag, finishAudioDrag, isDragging, dragPreviewOf, selOf, clearSel, splitClipAt, drawClipBlocks } from './audio_edit.js';   /* E6 段1 补：音频 clip 手势引擎（单轨/总谱共用） */
import { diffLayers } from './diff.js';
import { pal, trackColors } from './theme.js';

export { trackColors };
const BLACK = new Set([1, 3, 6, 8, 10]);
const NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B'];

let canvas, ctx, W = 0, H = 0, dpr = 1;

/* v0.2 批C 前段（R2 地基件）：几何实现抽至 geom.js（单一来源）；下方 re-export 兼容既有外部引用 */
export { xOf, tOf };
export function yOf(midi) { return (store.view.midiTop - midi) * store.view.pxPerSemi; }
export function midiOf(y) { return Math.round(store.view.midiTop - y / store.view.pxPerSemi); }

function noteName(m) { return NAMES[((m % 12) + 12) % 12] + (Math.floor(m / 12) - 1); }

/* M-V3：写谱吸附——把时间对齐到网格（store.snapFrac = 拍比例；0 = 关） */
function snapT(t) {
  const f = store.snapFrac;
  if (!f) return t;
  const grid = (60 / tempo()) * f;
  return Math.round(t / grid) * grid;
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

function rowsVisible() { return Math.ceil(H / store.view.pxPerSemi) + 1; }

export function draw() {
  if (!ctx) return;
  ctx.clearRect(0, 0, W, H);

  const v = store.view;
  const p = pal();
  const layers = diffLayers();

  /* ---- M-V8 E3 段1 / v0.2 批C2：单轨分屏（主轨上 / 副区下；splitH1 = 上区高） ---- */
  const single = store.viewMode === 'single';
  const sp = single ? splitPartner() : null;
  splitOn = !!sp;
  splitH1 = splitOn ? Math.round(H * Math.max(0.15, Math.min(0.85, store.splitRatio))) : H;
  const mainTrk = single && store.score ? store.score.tracks[store.singleTrack] : null;
  const mainIsAudio = !!(mainTrk && mainTrk.kind === 'audio');

  if (single && mainIsAudio) {
    /* 主轨 = 音频：上区波形（设计记录 #183①：音频轨进单轨不再空卷帘） */
    drawRegion(0, splitH1, store.singleTrack, mainTrk, 'main');
    drawSplitTail(single, sp);
    return;
  }

  /* 卷帘绘制限高上区（clip；非分屏时 = 全高，无副作用） */
  ctx.save();
  ctx.beginPath(); ctx.rect(0, 0, W, splitH1); ctx.clip();

  /* ---- 琴键行底色 ---- */
  const topMidi = Math.ceil(v.midiTop);
  for (let i = 0; i < rowsVisible(); i++) {
    const m = topMidi - i;
    const y = yOf(m);
    ctx.fillStyle = BLACK.has(((m % 12) + 12) % 12) ? p.rowBlack : p.rowWhite;
    ctx.fillRect(KEYS_W, y, W - KEYS_W, v.pxPerSemi);
  }

  /* ---- 拍/小节竖线 ---- */
  const beat = 60 / tempo();
  const t0 = Math.max(0, tOf(KEYS_W));
  const t1 = tOf(W);
  const tEnd = scoreBounds().tEnd;
  const tMax = Math.max(t1, tEnd + 2);
  ctx.lineWidth = 1;
  const bq = beatsPerBar();   // M-V6：小节 = bq 个四分拍（原来写死 4）
  let k = Math.floor(t0 / beat);
  for (; k * beat <= tMax; k++) {
    const t = k * beat;
    const x = Math.round(xOf(t)) + 0.5;
    if (x < KEYS_W) continue;
    if (x > W) break;
    if (Math.abs(k / bq - Math.round(k / bq)) < 1e-6) {
      ctx.strokeStyle = p.barLine;
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke();
    } else if (v.pxPerSec * beat > 7) {
      ctx.strokeStyle = p.beatLine;
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke();
    }
  }

  /* ---- diff 红残影（removed，不在当前谱里） ---- */
  if (layers) {
    for (const n of layers.removed) {
      const x = xOf(n.start), y = yOf(n.pitch_midi);
      const w = Math.max(2, (n.end - n.start) * v.pxPerSec), h = v.pxPerSemi - 1;
      ctx.fillStyle = p.diffDelFill;
      ctx.fillRect(x, y + 0.5, w, h);
      ctx.strokeStyle = p.diffDelStroke;
      ctx.setLineDash([3, 2]);
      ctx.strokeRect(x + 0.5, y + 0.5, w, h);
      ctx.setLineDash([]);
    }
  }

  /* ---- 音符 ---- */
  const sel = store.selection;
  if (store.score) {
    store.score.tracks.forEach((tr, ti) => {
      if (store.hiddenTracks.has(ti)) return;
      if (single && ti !== store.singleTrack) return;   /* v0.2 批C2：单轨只画主轨（副区在下区单独绘制） */
      const tc = trackColors();
      const color = tc[ti % tc.length];
      for (let ni = 0; ni < tr.notes.length; ni++) {
        const n = tr.notes[ni];
        const x = xOf(n.start), y = yOf(n.pitch_midi);
        const w = Math.max(2, (n.end - n.start) * v.pxPerSec), h = v.pxPerSemi - 1;
        if (x + w < KEYS_W || x > W || y + h < 0 || y > H) continue;

        /* diff 叠层（只对 track 0） */
        let stroke = null, fill = color;
        if (layers && ti === 0) {
          const key = Math.round(n.start * 1000) + '|' + n.pitch_midi;
          const cls = layers.classify.get(key);
          if (cls === 'add') { fill = p.diffAddFill; stroke = p.diffAddStroke; }
          else if (cls === 'mod') { stroke = p.diffModStroke; }
        }

        /* 黄对照：旧音残影（虚线框在旧位置） */
        if (layers && ti === 0) {
          const old = layers.ghostOf(n);
          if (old) {
            const ox = xOf(old.start), oy = yOf(old.pitch_midi);
            const ow = Math.max(2, (old.end - old.start) * v.pxPerSec);
            ctx.fillStyle = p.ghostFillMod;
            ctx.fillRect(ox, oy + 0.5, ow, v.pxPerSemi - 1);
            ctx.strokeStyle = p.ghostStrokeMod;
            ctx.setLineDash([3, 2]);
            ctx.strokeRect(ox + 0.5, oy + 0.5, ow, v.pxPerSemi - 1);
            ctx.setLineDash([]);
            /* 新旧连线 */
            ctx.strokeStyle = p.ghostStrokeMod;
            ctx.beginPath();
            ctx.moveTo(ox, oy + v.pxPerSemi / 2);
            ctx.lineTo(x, y + v.pxPerSemi / 2);
            ctx.stroke();
          }
        }

        ctx.fillStyle = fill;
        ctx.globalAlpha = 0.92;
        ctx.fillRect(x, y + 0.5, w, h);
        ctx.globalAlpha = 1;
        ctx.strokeStyle = stroke || p.noteStroke;
        ctx.strokeRect(x + 0.5, y + 0.5, w, h);

        /* 选中高亮 */
        if (sel.track === ti && sel.indices.includes(ni)) {
          ctx.strokeStyle = p.selStroke;
          ctx.lineWidth = 2;
          ctx.strokeRect(x + 1, y + 1, w - 1, h - 1);
          ctx.lineWidth = 1;
        }
      }
    });
  }

  /* ---- M-V8 E5：范围框选 / 橡皮拖刷 视觉 ---- */
  if (rangeDrag) {
    const x0 = Math.min(rangeDrag.x0, rangeDrag.x1), x1 = Math.max(rangeDrag.x0, rangeDrag.x1);
    const y0 = Math.min(rangeDrag.y0, rangeDrag.y1), y1 = Math.max(rangeDrag.y0, rangeDrag.y1);
    ctx.fillStyle = p.selStroke;
    ctx.globalAlpha = 0.08;
    ctx.fillRect(x0, y0, x1 - x0, y1 - y0);
    ctx.globalAlpha = 1;
    ctx.strokeStyle = p.selStroke;
    ctx.setLineDash([4, 3]);
    ctx.strokeRect(x0 + 0.5, y0 + 0.5, x1 - x0, y1 - y0);
    ctx.setLineDash([]);
  }
  if (eraserDrag && eraserDrag.set.size && store.score) {
    const etr = store.score.tracks[eraserDrag.track];
    if (etr) {
      for (const i of eraserDrag.set) {
        const n = etr.notes[i];
        if (!n) continue;
        const x = xOf(n.start), y = yOf(n.pitch_midi);
        const w = Math.max(2, (n.end - n.start) * v.pxPerSec), h = v.pxPerSemi - 1;
        ctx.fillStyle = p.diffDelFill;
        ctx.fillRect(x, y + 0.5, w, h);
        ctx.strokeStyle = p.diffDelStroke;
        ctx.strokeRect(x + 0.5, y + 0.5, w, h);
      }
    }
  }

  /* ---- 播放头 ---- E3 段1：改由 drawSplitTail 全高绘制（不受上区裁剪） */

  /* ---- 左侧钢琴键盘列 ---- */
  ctx.fillStyle = p.rowBlack;
  ctx.fillRect(0, 0, KEYS_W, H);
  for (let i = 0; i < rowsVisible(); i++) {
    const m = topMidi - i;
    const y = yOf(m);
    const pc = ((m % 12) + 12) % 12;
    ctx.fillStyle = BLACK.has(pc) ? p.keyBlack : p.keyWhite;
    ctx.fillRect(0, y, KEYS_W - 4, v.pxPerSemi);
    if (pc === 0 && v.pxPerSemi >= 9) {
      ctx.fillStyle = p.keyLabel;
      ctx.font = '10px sans-serif';
      ctx.fillText(noteName(m), 6, y + v.pxPerSemi - 3);
    }
  }
  ctx.strokeStyle = p.keySep;
  ctx.beginPath(); ctx.moveTo(KEYS_W - 3.5, 0); ctx.lineTo(KEYS_W - 3.5, H); ctx.stroke();

  /* ---- 手势幽灵预览（最后画，覆盖在上层） ---- */
  drawGhost();

  ctx.restore();   /* E3 段1：上区裁剪结束 */
  drawSplitTail(single, sp);
}

/* ---- 编辑手势（M-V2.2 议题 ③：FL/Cubase 心智） ----
   拖音符本体 = move（set_time + set_pitch）；拖左右两端 = resize（set_time）；
   点空白 = 铅笔创建（add）。一次手势 = 一个 EditBatch = 一个 commit。 */
const EDGE_PX = 6;          // 两端热区宽（px）
const MIN_DUR = 0.08;       // 最小音符时长（秒）
let drag = null;            // {mode, track, index, orig, ghost:{start,end,pitch}}
let mouseInCanvas = false;
/* M-V8 E5：工具手势态 */
let rangeDrag = null;       // 范围工具：{x0,y0,x1,y1}（画布坐标）
let eraserDrag = null;      // 橡皮：{track, set:Set<noteIndex>}
const TOOL_CURSORS = { range: 'crosshair', scissors: 'col-resize', glue: 'pointer', eraser: 'cell' };

/* ---- E3 段1：分屏运行时态（draw 更新；手势/命中读） ---- */
let splitOn = false;      // 当前是否分屏（跨类型叠加对象存在）
let splitH1 = 0;          // 上区高（像素；非分屏 = 全高）
let splitDrag = false;    // 分界线拖拽中
let previewAudioEl = null;   // 波形区双击试听单例

/* E6 段1 补：单轨音频块的带内缩（与 drawRegion 同口径） */
function bandPadOf(h) { return Math.max(6, Math.min(18, h * 0.06)); }

/* E6 段1 补：当前单轨主区为音频 → 可编辑带 {ti, yTop, hh}；否则 null */
function audioMainBand() {
  if (store.viewMode !== 'single' || !store.score) return null;
  const trk = store.score.tracks[store.singleTrack];
  if (!trk || trk.kind !== 'audio') return null;
  const h = splitOn ? splitH1 : H;
  if (h <= 8) return null;
  const pad = bandPadOf(h);
  return { ti: store.singleTrack, yTop: pad, hh: Math.max(10, h - pad * 2) };
}

/* v0.2 C2：副区音频可编辑带（下落区 → 可编；主区波形带为 audioMainBand） */
function partnerBand() {
  if (store.viewMode !== 'single' || !splitOn || !store.score) return null;
  const sp = splitPartner();
  if (!sp || sp.kind !== 'audio') return null;
  const h = H - splitH1;
  if (h <= 8) return null;
  const pad = bandPadOf(h);
  return { ti: sp.ti, yTop: splitH1 + pad, hh: Math.max(10, h - pad * 2) };
}

/* v0.2 C2：区域命中路由 —— 单轨工作台两区（上=主轨 / 下=副区）。
   返回 {ti, lower, audio, edit, lane}；edit=false = 该区不吃音符手势（自适配概览/道副区/音频）。 */
function regionTrack(mx, my) {
  if (store.viewMode !== 'single' || !store.score) return null;
  const sp = splitPartner();
  const lower = !!sp && my > splitH1;
  const ti = lower ? sp.ti : store.singleTrack;
  const tr = store.score.tracks[ti];
  if (!tr) return null;
  const audio = tr.kind === 'audio';
  let edit = !audio;
  if (lower) {
    if (sp.kind === 'lane') edit = false;
    else if (sp.kind === 'midi') edit = !!store.splitSameAxis;
    else edit = false;   /* 音频副区走 clip 引擎，不进音符路由 */
  }
  return { ti, lower, audio, edit, lane: (lower && sp.kind === 'lane') ? sp.param : null };
}

/* v0.2 批C2：分屏区渲染。
   音频 → 波形块（与总谱共用引擎/命令层；主/副区均可编辑）；
   MIDI 副区 → 同音轴（与主区同 pitch 映射，可编辑外观）/ 自适配（紧凑概览，只读）。 */
function drawRegion(y0, h, ti, trk, role) {
  if (h <= 8 || !trk) return;
  const p = pal();
  const tc = trackColors();
  const col = tc[ti % tc.length];
  const v = store.view;

  /* 底色 + 左侧标签槽 */
  ctx.fillStyle = p.rollBg;
  ctx.fillRect(KEYS_W, y0, Math.max(0, W - KEYS_W), h);
  ctx.fillStyle = p.panelBg;
  ctx.fillRect(0, y0, KEYS_W, h);
  ctx.fillStyle = col;
  ctx.fillRect(0, y0, 4, h);
  ctx.strokeStyle = p.keySep;
  ctx.beginPath(); ctx.moveTo(KEYS_W - 3.5, y0); ctx.lineTo(KEYS_W - 3.5, y0 + h); ctx.stroke();
  ctx.fillStyle = p.laneLabel;
  ctx.font = '11px "Microsoft YaHei UI","PingFang SC",system-ui,sans-serif';
  const nm = (trk.name || ('track ' + ti));
  ctx.fillText('🎵 ' + (nm.length > 7 ? nm.slice(0, 7) + '…' : nm), 8, y0 + 15);
  if (role === 'partner') ctx.fillText('副区', 8, Math.min(y0 + 30, y0 + h - 6));

  if (trk.kind === 'audio') {
    /* E6 段1 补（2026-09-27）：多 clip 波形块（与总谱共用引擎/命令层）；主/副区均可编辑 */
    const pad = bandPadOf(h);
    const prev = dragPreviewOf(ti);
    const sel = selOf();
    drawClipBlocks(ctx, {
      list: prev || audioClipsOf(trk),
      col,
      yTop: y0 + pad,
      hh: Math.max(10, h - pad * 2),
      selIdx: (sel && sel.ti === ti) ? sel.idx : null,
      inDrag: !!prev,
      dim: false,
      label: (trk.name || '素材'),
      p, v,
      xLo: KEYS_W, xHi: W,
    });
    return;
  }

  const notes = trk.notes || [];
  if (role === 'partner' && !store.splitSameAxis) {
    /* 自适配：紧凑概览（只读） */
    let lo = 127, hi = 0;
    for (const n of notes) { if (n.pitch_midi < lo) lo = n.pitch_midi; if (n.pitch_midi > hi) hi = n.pitch_midi; }
    if (lo > hi) { lo = 60; hi = 72; }
    const pad = Math.max(6, h * 0.12);
    const inner = Math.max(8, h - pad * 2);
    const nh = Math.max(3, Math.min(8, h * 0.09));
    ctx.fillStyle = col;
    ctx.globalAlpha = 0.8;
    for (const n of notes) {
      const x = xOf(n.start);
      const w = Math.max(2, (n.end - n.start) * v.pxPerSec);
      if (x + w < KEYS_W || x > W) continue;
      const y = y0 + pad + ((hi - n.pitch_midi) / Math.max(1, hi - lo)) * (inner - nh);
      ctx.fillRect(x, y, w, nh);
    }
    ctx.globalAlpha = 1;
    ctx.fillStyle = p.laneLabel;
    ctx.fillText('概览（只读）', 8, y0 + h - 8);
    return;
  }

  /* 同音轴：与主区同一音高映射（可编辑） */
  ctx.save();
  ctx.beginPath(); ctx.rect(KEYS_W, y0, Math.max(0, W - KEYS_W), h); ctx.clip();
  const sel2 = store.selection;
  for (let ni = 0; ni < notes.length; ni++) {
    const n = notes[ni];
    const x = xOf(n.start), y = yOf(n.pitch_midi);
    const w = Math.max(2, (n.end - n.start) * v.pxPerSec), hh = v.pxPerSemi - 1;
    if (x + w < KEYS_W || x > W || y + hh < y0 || y > y0 + h) continue;
    ctx.fillStyle = col;
    ctx.globalAlpha = 0.92;
    ctx.fillRect(x, y + 0.5, w, hh);
    ctx.globalAlpha = 1;
    ctx.strokeStyle = p.noteStroke;
    ctx.strokeRect(x + 0.5, y + 0.5, w, hh);
    if (sel2.track === ti && sel2.indices.includes(ni)) {
      ctx.strokeStyle = p.selStroke;
      ctx.lineWidth = 2;
      ctx.strokeRect(x + 1, y + 1, w - 1, hh - 1);
      ctx.lineWidth = 1;
    }
  }
  ctx.restore();
}

/* E3 段1 / v0.2 C2：分屏尾部（下区 → 分界线 → 全高播放头）；卷帘分支与音频主轨分支共用 */
function drawSplitTail(single, sp) {
  if (single && sp && store.score) {
    if (sp.kind === 'lane') {
      drawLaneRegion(splitH1, H - splitH1, sp);   /* v0.2 C2：道副区（段2 F4 接全渲染） */
    } else {
      const ov = store.score.tracks[sp.ti];
      if (ov) drawRegion(splitH1, H - splitH1, sp.ti, ov, 'partner');
    }
    drawSplitBar();
  }
  drawPlayheadLine();
}

/* v0.2 C2：道副区占位渲染（曲线全宽；段2 F4 接 lane_render 全道渲染） */
function drawLaneRegion(y0, h, sp) {
  const p = pal();
  ctx.fillStyle = p.rollBg;
  ctx.fillRect(KEYS_W, y0, Math.max(0, W - KEYS_W), h);
  ctx.fillStyle = p.laneLabel;
  ctx.font = '11px "Microsoft YaHei UI","PingFang SC",system-ui,sans-serif';
  ctx.fillText('道：' + (sp.param || ''), 8, y0 + 15);
}

function drawSplitBar() {
  const p = pal();
  ctx.fillStyle = p.panelBg;
  ctx.fillRect(0, splitH1 - 1, W, 3);
  ctx.fillStyle = p.keySep;
  ctx.beginPath(); ctx.moveTo(0, splitH1 + 1.5); ctx.lineTo(W, splitH1 + 1.5); ctx.stroke();
  ctx.fillStyle = p.laneLabel;
  ctx.globalAlpha = 0.6;
  ctx.fillRect(Math.max(0, W / 2 - 24), splitH1 - 1, 48, 3);
  ctx.globalAlpha = 1;
}

/* E3 段1：全高播放头（原在卷帘主体内，现画在裁剪外、两分支共用） */
function drawPlayheadLine() {
  if (!store.playing) return;
  const p = pal();
  const px = xOf(store.playhead);
  if (px < KEYS_W || px > W) return;
  ctx.strokeStyle = p.playhead;
  ctx.lineWidth = 1.5;
  ctx.beginPath(); ctx.moveTo(px, 0); ctx.lineTo(px, H); ctx.stroke();
  ctx.fillStyle = p.playhead;
  ctx.beginPath();
  ctx.moveTo(px - 5, 0); ctx.lineTo(px + 5, 0); ctx.lineTo(px, 7);
  ctx.closePath(); ctx.fill();
  ctx.lineWidth = 1;
}

/* E3 段1 / v0.2 C2：分界变化 / 主副变化后——主轨为 MIDI 时按上区高重适配音域 */
function refitSplit() {
  if (!store.score || store.viewMode !== 'single') { draw(); return; }
  const tr = store.score.tracks[store.singleTrack];
  const sp = splitPartner();
  const h1 = sp ? Math.round(H * Math.max(0.15, Math.min(0.85, store.splitRatio))) : H;
  if (tr && tr.kind !== 'audio') fitViewTrack(store.singleTrack, Math.max(60, h1));
  draw();
}

/* E3 段1 / v0.2 C2：下区双击 —— 主副对调（副区升主、旧主降副；上下交换） */
function swapSplit() {
  const sp = splitPartner();
  if (!sp || sp.kind === 'lane') return;
  swapMainPartner(sp.ti);
  refitSplit();
  bus.dispatch('toast', '已切主轨（原主轨入副区）');
}

/* E3 段1：波形区双击 —— 试听素材原文件（E6 段1 补：多 clip → 取首 clip 文件） */
function previewTrackAudio(trk) {
  const cls = audioClipsOf(trk);
  const rel = cls.length ? cls[0].file : null;
  if (!rel || !store.project) return;
  if (previewAudioEl) { previewAudioEl.pause(); previewAudioEl = null; }
  previewAudioEl = new Audio(api.audioUrl(store.project, rel));
  previewAudioEl.play()
    .then(() => showStatus('试听素材：' + (trk.name || rel)))
    .catch((err) => showStatus('试听失败：' + ((err && err.message) || err), true));
}

/* ---- 命中测试（逆序=后画的优先；edge=左右端热区）
   v0.2 C2：单轨态按区域路由（上=主轨 / 下=副区〔可编辑条件内〕）；总谱态保留旧全轨扫描兜底 ---- */
function hitNote(mx, my) {
  if (!store.score) return null;
  const v = store.view;
  const single = store.viewMode === 'single';
  let list;
  if (single) {
    const r = regionTrack(mx, my);
    if (!r || !r.edit) return null;
    list = [r.ti];
  } else {
    list = store.score.tracks.map((_, i) => i);
  }
  for (let k = list.length - 1; k >= 0; k--) {
    const ti = list[k];
    if (store.hiddenTracks.has(ti)) continue;
    const notes = store.score.tracks[ti].notes;
    for (let ni = notes.length - 1; ni >= 0; ni--) {
      const n = notes[ni];
      const x = xOf(n.start), y = yOf(n.pitch_midi);
      const w = Math.max(2, (n.end - n.start) * v.pxPerSec), h = v.pxPerSemi - 1;
      if (mx >= x - 1 && mx <= x + w + 1 && my >= y && my <= y + h) {
        let edge = 'body';
        const ep = Math.min(EDGE_PX, Math.max(2, w / 3));
        if (mx <= x + ep) edge = 'left';
        else if (mx >= x + w - ep) edge = 'right';
        return { track: ti, index: ni, edge };
      }
    }
  }
  return null;
}

/* ---- 幽灵预览绘制（拖拽/新建时） ---- */
function drawGhost() {
  if (!drag) return;
  const p = pal();
  if (drag.mode === 'move-multi') {
    /* M-V3：多选整体移动——逐音画幽灵 + 原位残影 */
    const v = store.view;
    for (const o of drag.origs) {
      const gx = xOf(o.n.start + drag.ghost.dt), gy = yOf(o.n.pitch_midi + drag.ghost.dp);
      const gw = Math.max(2, (o.n.end - o.n.start) * v.pxPerSec), gh = v.pxPerSemi - 1;
      ctx.fillStyle = p.ghostFillMove;
      ctx.fillRect(gx, gy + 0.5, gw, gh);
      ctx.strokeStyle = p.ghostStrokeMove;
      ctx.setLineDash([4, 3]);
      ctx.strokeRect(gx + 0.5, gy + 0.5, gw, gh);
      ctx.strokeStyle = p.residual;
      ctx.setLineDash([2, 2]);
      ctx.strokeRect(xOf(o.n.start) + 0.5, yOf(o.n.pitch_midi) + 0.5, gw, gh);
      ctx.setLineDash([]);
    }
    return;
  }
  const g = drag.ghost;
  const v = store.view;
  const x = xOf(g.start), y = yOf(g.pitch);
  const w = Math.max(2, (g.end - g.start) * v.pxPerSec), h = v.pxPerSemi - 1;
  ctx.fillStyle = drag.mode === 'create' ? p.ghostFillCreate : p.ghostFillMove;
  ctx.fillRect(x, y + 0.5, w, h);
  ctx.strokeStyle = drag.mode === 'create' ? p.ghostStrokeCreate : p.ghostStrokeMove;
  ctx.setLineDash([4, 3]);
  ctx.strokeRect(x + 0.5, y + 0.5, w, h);
  ctx.setLineDash([]);
  /* move 模式画原位置残影 */
  if (drag.mode === 'move' && drag.orig) {
    const ox = xOf(drag.orig.start), oy = yOf(drag.orig.pitch_midi);
    const ow = Math.max(2, (drag.orig.end - drag.orig.start) * v.pxPerSec);
    ctx.strokeStyle = p.residual;
    ctx.setLineDash([2, 2]);
    ctx.strokeRect(ox + 0.5, oy + 0.5, ow, h);
    ctx.setLineDash([]);
  }
}

/* ---- 手势提交：合成 EditBatch（一次手势 = 一个 commit） ---- */
async function submitDrag() {
  if (!drag || !store.project) { drag = null; draw(); return; }
  const d = drag;
  drag = null;
  const cmd = [];
  try {
    if (d.mode === 'create') {
      const { start, end, pitch } = d.ghost;
      cmd.push({ op: 'add', track: d.track, index: null, value: { pitch_midi: pitch, start: round3(start), end: round3(end) } });
      const r = await api.postBatch(store.project, '添加音符', cmd, '手绘：添加音符');
      if (r.applied) showStatus('已添加 ' + midiName(pitch) + ' ' + refTag(r));
      else showStatus('被拒：' + (r.errors || []).join('；'), true);
      return;
    }
    if (d.mode === 'move-multi') {
      const { dt, dp } = d.ghost;
      if (Math.abs(dt) < 1e-6 && dp === 0) return;
      for (const o of d.origs) {
        cmd.push({ op: 'set_time', track: d.track, index: o.i,
                   value: { start: round3(o.n.start + dt), end: round3(o.n.end + dt) } });
        if (dp !== 0) {
          cmd.push({ op: 'set_pitch', track: d.track, index: o.i,
                     value: Math.max(0, Math.min(127, o.n.pitch_midi + dp)) });
        }
      }
      const r = await api.postBatch(store.project, '移动 ' + d.origs.length + ' 音', cmd,
                                    '手绘：移动 ' + d.origs.length + ' 音');
      if (r.applied) showStatus('已移动 ' + d.origs.length + ' 音 ' + refTag(r));
      else showStatus('被拒：' + (r.errors || []).join('；'), true);
      return;
    }
    const orig = d.orig;
    const g = d.ghost;
    const ns = round3(g.start), ne = round3(g.end), np = Math.max(0, Math.min(127, Math.round(g.pitch)));
    if (d.mode === 'move') {
      if (Math.abs(ns - orig.start) < 1e-6 && np === orig.pitch_midi) return;
      cmd.push({ op: 'set_time', track: d.track, index: d.index, value: { start: ns, end: ne } });
      if (np !== orig.pitch_midi) cmd.push({ op: 'set_pitch', track: d.track, index: d.index, value: np });
    } else { // resize
      if (Math.abs(ns - orig.start) < 1e-6 && Math.abs(ne - orig.end) < 1e-6) return;
      cmd.push({ op: 'set_time', track: d.track, index: d.index, value: { start: ns, end: ne } });
    }
    const r = await api.postBatch(store.project, d.mode === 'move' ? '移动音符' : '缩放音符', cmd, '手绘：' + (d.mode === 'move' ? '移动' : '缩放'));
    if (r.applied) showStatus((d.mode === 'move' ? '已移动' : '已缩放') + ' ' + refTag(r));
    else showStatus('被拒：' + (r.errors || []).join('；'), true);
  } catch (err) {
    showStatus(err.message, true);
  }
}

function round3(x) { return Math.round(x * 1000) / 1000; }
function midiName(m) { return NAMES[((m % 12) + 12) % 12] + (Math.floor(m / 12) - 1); }
function showStatus(msg, isErr) {
  bus.dispatch('toast', msg);
  console[isErr ? 'warn' : 'log']('[tsov hand]', msg);
}

/* ---- M-V3：音符右键菜单 ---- */
let noteMenu = null;
function closeNoteMenu() { if (noteMenu) { noteMenu.remove(); noteMenu = null; } }
async function postNoteOp(hit, commands, label) {
  try {
    const r = await api.postBatch(store.project, label, commands, label);
    if (r.applied) showStatus('已执行 ' + label + ' ' + refTag(r));
    else showStatus('被拒：' + (r.errors || []).join('；'), true);
  } catch (err) { showStatus(err.message, true); }
}
function openNoteMenu(hit, cx, cy) {
  closeNoteMenu();
  const n = store.score.tracks[hit.track].notes[hit.index];
  noteMenu = document.createElement('div');
  noteMenu.className = 'note-menu';
  const mk = (txt, fn) => {
    const b = document.createElement('button');
    b.textContent = txt;
    b.addEventListener('click', async () => { closeNoteMenu(); await fn(); });
    noteMenu.appendChild(b);
  };
  const setP = (delta, tag) => () => postNoteOp(hit,
    [{ op: 'set_pitch', track: hit.track, index: hit.index, value: Math.max(0, Math.min(127, n.pitch_midi + delta)) }], tag);
  mk('🗑 删除 ' + midiName(n.pitch_midi), () => postNoteOp(hit, [{ op: 'remove', track: hit.track, index: hit.index }], '右键删除'));
  mk('♯ +1 半音', setP(1, '右键 +1'));
  mk('♭ −1 半音', setP(-1, '右键 −1'));
  mk('+8 八度', setP(8, '右键 +8'));
  mk('−8 八度', setP(-8, '右键 −8'));
  noteMenu.style.left = Math.min(cx, window.innerWidth - 150) + 'px';
  noteMenu.style.top = Math.min(cy, window.innerHeight - 200) + 'px';
  document.body.appendChild(noteMenu);
}

/* ---- M-V8 E5：编辑工具（范围框选 / 剪刀切分 / 胶水合并 / 橡皮拖删） ---- */

function onToolDown(e, tool) {
  if (!store.project || !store.score) return;
  if (tool === 'scissors') {
    const hit = hitNote(e.offsetX, e.offsetY);
    if (hit) splitAt(hit, e.offsetX);
    return;
  }
  if (tool === 'glue') {
    const hit = hitNote(e.offsetX, e.offsetY);
    if (hit) glueAt(hit);
    return;
  }
  if (tool === 'eraser') {
    const rr = regionTrack(e.offsetX, e.offsetY);
    eraserDrag = { track: (rr && rr.edit) ? rr.ti : store.singleTrack, set: new Set() };
    markEraser(e);
    draw();
    return;
  }
  if (tool === 'range') {
    rangeDrag = { x0: e.offsetX, y0: e.offsetY, x1: e.offsetX, y1: e.offsetY };
    draw();
  }
}

/* 剪刀：点击音符内 → split_note（切点先吸附；吸附越出音符则用原始位置；护栏在服务端） */
async function splitAt(hit, px) {
  const n = store.score.tracks[hit.track].notes[hit.index];
  let at = Math.round(snapT(tOf(px)) * 1000) / 1000;
  if (at <= n.start + 1e-3 || at >= n.end - 1e-3) at = Math.round(tOf(px) * 1000) / 1000;
  try {
    const r = await api.postBatch(store.project, '剪刀切分',
      [{ op: 'split_note', track: hit.track, index: hit.index, value: { at } }], '剪刀：切分 @ ' + at + 's');
    if (r.applied) showStatus('已切分 ' + midiName(n.pitch_midi) + ' @ ' + at.toFixed(3) + 's ' + refTag(r));
    else showStatus('被拒：' + (r.errors || []).join('；'), true);
  } catch (err) { showStatus(err.message, true); }
}

/* 胶水：点音符 → 与后邻同音高合并（gap ≤ 0.5s；服务端校验） */
async function glueAt(hit) {
  try {
    const r = await api.postBatch(store.project, '胶水合并',
      [{ op: 'merge_notes', track: hit.track, index: hit.index }], '胶水：合并相邻音');
    if (r.applied) showStatus('已合并 ' + refTag(r));
    else showStatus('被拒：' + (r.errors || []).join('；'), true);
  } catch (err) { showStatus(err.message, true); }
}

function markEraser(e) {
  if (!eraserDrag) return;
  const hit = hitNote(e.offsetX, e.offsetY);
  if (hit && hit.track === eraserDrag.track) eraserDrag.set.add(hit.index);
}

async function commitErase() {
  const ed = eraserDrag;
  eraserDrag = null;
  if (!ed || !ed.set.size) { draw(); return; }
  const idxs = Array.from(ed.set).sort((a, b) => b - a);   // 降序：索引不漂移
  try {
    const r = await api.postBatch(store.project, '橡皮删除 ' + idxs.length + ' 音',
      idxs.map((i) => ({ op: 'remove', track: ed.track, index: i })), '橡皮：删除 ' + idxs.length + ' 音');
    if (r.applied) showStatus('已删除 ' + idxs.length + ' 音 ' + refTag(r));
    else showStatus('被拒：' + (r.errors || []).join('；'), true);
  } catch (err) { showStatus(err.message, true); }
  draw();
}

function commitRange() {
  const rd = rangeDrag;
  rangeDrag = null;
  if (rd && store.score) {
    const x0 = Math.min(rd.x0, rd.x1), x1 = Math.max(rd.x0, rd.x1);
    const y0 = Math.min(rd.y0, rd.y1), y1 = Math.max(rd.y0, rd.y1);
    const t0 = tOf(x0), t1 = tOf(x1);
    const rr = store.viewMode === 'single' ? regionTrack(rd.x0, rd.y0) : null;
    const ti = store.viewMode === 'single' ? ((rr && rr.edit) ? rr.ti : store.singleTrack) : store.selection.track;
    const tr = store.score.tracks[ti];
    if (tr) {
      const idxs = [];
      const pxh = store.view.pxPerSemi;
      tr.notes.forEach((n, i) => {
        if (n.end < t0 || n.start > t1) return;
        const y = yOf(n.pitch_midi);
        if (y + pxh < y0 || y > y1) return;
        idxs.push(i);
      });
      setSelection(ti, idxs);
      if (t1 - t0 > 1e-3) setRange({ start: Math.max(0, t0), end: Math.max(0, t1) });
      if (idxs.length) showStatus('框选 ' + idxs.length + ' 音');
    }
  }
  draw();
}

export function init(rollCanvas) {
  canvas = rollCanvas;
  ctx = canvas.getContext('2d');
  resize();
  new ResizeObserver(() => { resize(); draw(); }).observe(canvas);

  canvas.addEventListener('wheel', (e) => {
    e.preventDefault();
    if (e.ctrlKey) {
      /* 时间缩放（锚定光标处的时间） */
      const anchorT = tOf(e.offsetX);
      const f = e.deltaY < 0 ? 1.15 : 1 / 1.15;
      const px = Math.min(600, Math.max(8, store.view.pxPerSec * f));
      setView({ pxPerSec: px, scrollSec: Math.max(0, anchorT - (e.offsetX - KEYS_W) / px) });
    } else if (e.altKey) {
      /* 纵向滚动（音高区） */
      const f = e.deltaY > 0 ? 1 : -1;
      setView({ midiTop: store.view.midiTop + f * Math.max(1, Math.round(3 * 14 / store.view.pxPerSemi)) });
    } else {
      setView({ scrollSec: Math.max(0, store.view.scrollSec + e.deltaY * 0.02) });
    }
  }, { passive: false });

  canvas.addEventListener('mousedown', (e) => {
    if (e.button !== 0) return;          // 仅左键
    /* E3 段1：分界线拖拽（优先于一切手势） */
    if (splitOn && Math.abs(e.offsetY - splitH1) <= 6) {
      splitDrag = true;
      canvas.style.cursor = 'row-resize';
      return;
    }
    /* E6 段1 补 / v0.2 C2：音频 clip 手势（主区带与副区带共引擎；带外不吃音符逻辑） */
    for (const band of [audioMainBand(), partnerBand()]) {
      if (!band) continue;
      const ahit = clipHitBand(band.ti, e.offsetX, e.offsetY, band.yTop, band.hh);
      if (ahit) {
        setFocus(band.ti);
        const stretchHold = e.altKey && ahit.zone === 'trim-out';
        if ((store.tool === 'scissors' || e.altKey) && !stretchHold) {
          splitClipAt(band.ti, ahit.idx, tOf(e.offsetX), e.altKey);
          return;
        }
        beginAudioDrag(band.ti, ahit, e, draw);
        canvas.style.cursor = 'grabbing';
        return;
      }
      if (e.offsetY >= band.yTop - 6 && e.offsetY <= band.yTop + band.hh + 6) {
        clearSel();
        setFocus(band.ti);
        draw();
        return;
      }
    }
    /* M-V8 E5：工具手势分发（仅单轨卷帘视图；智能指针走原逻辑） */
    if (store.viewMode === 'single' && (store.tool || 'smart') !== 'smart') {
      onToolDown(e, store.tool);
      return;
    }
    /* v0.2 C2：区域路由 —— 不可编辑区（自适配概览/道副区/音频）不吃音符手势 */
    const r = regionTrack(e.offsetX, e.offsetY);
    if (store.viewMode === 'single' && (!r || !r.edit)) return;
    if (r) setFocus(r.ti);
    const hit = hitNote(e.offsetX, e.offsetY);
    /* v0.2 C2：副区空白不吃铅笔（保「下区双击=对调」手势纯净）；焦点/选区归副轨 */
    if (r && r.lower && !hit && !e.ctrlKey && !e.shiftKey) { setSelection(r.ti, []); draw(); return; }
    const v = store.view;

    /* Ctrl/Shift：只管多选，不拖拽 */
    if (e.ctrlKey || e.shiftKey) {
      if (!hit) { setSelection(0, []); return; }
      const cur = store.selection;
      const idx = (cur.track === hit.track) ? cur.indices.slice() : [];
      const at = idx.indexOf(hit.index);
      if (at >= 0) idx.splice(at, 1); else idx.push(hit.index);
      setSelection(hit.track, idx);
      return;
    }

    if (hit) {
      const curSel = store.selection;
      /* M-V3：多选整体拖动——点住已选中的音（选区≥2）→ 整个选区一起平移 */
      if (hit.edge === 'body' && curSel.track === hit.track && curSel.indices.length > 1 && curSel.indices.includes(hit.index)) {
        const notes = store.score.tracks[hit.track].notes;
        drag = { mode: 'move-multi', track: hit.track, anchor: hit.index,
                 indices: curSel.indices.slice(),
                 origs: curSel.indices.map((i) => ({ i, n: { ...notes[i] } })),
                 ghost: { dt: 0, dp: 0 },
                 grabT: notes[hit.index].start - tOf(e.offsetX),
                 grabP: notes[hit.index].pitch_midi - midiOf(e.offsetY) };
        bus.dispatch('note-selected', hit);
        return;
      }
      setSelection(hit.track, [hit.index]);
      bus.dispatch('note-selected', hit);
      const n = store.score.tracks[hit.track].notes[hit.index];
      if (hit.edge === 'body') {
        /* move：以点击位置为偏移量（音符在指针正下方跟随） */
        drag = { mode: 'move', track: hit.track, index: hit.index, orig: { ...n },
                 ghost: { start: n.start, end: n.end, pitch: n.pitch_midi } };
        drag.grabT = n.start - tOf(e.offsetX);
        drag.grabP = n.pitch_midi - midiOf(e.offsetY);
      } else {
        /* resize：拖两端（left=改 start / right=改 end） */
        drag = { mode: 'resize-left', track: hit.track, index: hit.index, orig: { ...n },
                 ghost: { start: n.start, end: n.end, pitch: n.pitch_midi } };
        if (hit.edge === 'right') drag.mode = 'resize-right';
      }
    } else {
      /* 铅笔：创建新音符（默认时长 = 半拍，可拖动拉长）
         v0.2 C2：单轨态建到区域所属轨（上=主轨 / 下=副区）；总谱模式卷帘不可见 */
      const createTrack = r ? r.ti : (store.viewMode === 'single' ? store.singleTrack : (store.selection.track || 0));
      setSelection(createTrack, []);
      const t0 = Math.max(0, snapT(tOf(e.offsetX)));
      const beat = 60 / tempo();
      const dur = Math.max(MIN_DUR, beat / 2);
      drag = { mode: 'create', track: createTrack, index: null, orig: null,
               ghost: { start: t0, end: t0 + dur, pitch: midiOf(e.offsetY) } };
    }
  });

  canvas.addEventListener('mousemove', (e) => {
    mouseInCanvas = true;
    /* E3 段1：分界线拖拽（比例跟随；松手重适配） */
    if (splitDrag) {
      setSplitRatio(e.offsetY / Math.max(1, H));
      return;
    }
    /* M-V8 E5：工具拖拽（范围框选 / 橡皮拖刷） */
    if (rangeDrag) {
      rangeDrag.x1 = e.offsetX;
      rangeDrag.y1 = e.offsetY;
      draw();
      return;
    }
    if (eraserDrag) {
      markEraser(e);
      draw();
      return;
    }
    if (isDragging()) {   /* E6 段1 补：音频手势（预览跟手，松手提交） */
      updateAudioDrag(e);
      canvas.style.cursor = 'grabbing';
      return;
    }
    if (drag) {
      const v = store.view;
      if (drag.mode === 'move') {
        const t = Math.max(0, snapT(tOf(e.offsetX) + drag.grabT));
        const p = Math.max(0, Math.min(127, midiOf(e.offsetY) + drag.grabP));
        const dur = drag.orig.end - drag.orig.start;
        drag.ghost = { start: t, end: t + dur, pitch: p };
      } else if (drag.mode === 'move-multi') {
        /* M-V3：选区整体平移（锚点音吸附网格；其余同位移；边界收敛） */
        const anchor = drag.origs.find((o) => o.i === drag.anchor) || drag.origs[0];
        const tT = snapT(tOf(e.offsetX) + drag.grabT);
        let dt = tT - anchor.n.start;
        let dp = (midiOf(e.offsetY) + drag.grabP) - anchor.n.pitch_midi;
        for (const o of drag.origs) {
          dt = Math.max(dt, -o.n.start);
          dp = Math.max(dp, -o.n.pitch_midi);
          dp = Math.min(dp, 127 - o.n.pitch_midi);
        }
        drag.ghost = { dt, dp };
      } else if (drag.mode === 'create') {
        const t1 = Math.max(snapT(tOf(e.offsetX)), drag.ghost.start + MIN_DUR);
        drag.ghost.end = t1;
        drag.ghost.pitch = midiOf(e.offsetY);
      } else if (drag.mode === 'resize-left') {
        const s = Math.min(Math.max(0, snapT(tOf(e.offsetX))), drag.ghost.end - MIN_DUR);
        drag.ghost.start = s;
      } else if (drag.mode === 'resize-right') {
        drag.ghost.end = Math.max(drag.ghost.start + MIN_DUR, snapT(tOf(e.offsetX)));
      }
      draw();
      return;
    }
    /* E3 段1 / v0.2 C2：分界线悬停（row-resize）；音频带光标（主/副两带共引擎） */
    if (splitOn && Math.abs(e.offsetY - splitH1) <= 6) { canvas.style.cursor = 'row-resize'; return; }
    for (const band of [audioMainBand(), partnerBand()]) {
      if (!band) continue;
      const hit = clipHitBand(band.ti, e.offsetX, e.offsetY, band.yTop, band.hh);
      if (hit) {
        if (e.altKey && hit.zone === 'trim-out') canvas.style.cursor = 'col-resize';
        else if (hit.zone === 'move') canvas.style.cursor = (store.tool === 'scissors' || e.altKey) ? 'cell' : 'grab';
        else canvas.style.cursor = 'ew-resize';
        return;
      }
      if (e.offsetY >= band.yTop - 6 && e.offsetY <= band.yTop + band.hh + 6) { canvas.style.cursor = 'default'; return; }
    }
    if (store.viewMode === 'single' && (store.tool || 'smart') !== 'smart') {
      canvas.style.cursor = TOOL_CURSORS[store.tool] || 'default';
      return;
    }
    /* v0.2 C2：不可编辑区（自适配概览/道副区）→ 默认光标 */
    if (store.viewMode === 'single') {
      const rr = regionTrack(e.offsetX, e.offsetY);
      if (!rr || !rr.edit) { canvas.style.cursor = 'default'; return; }
    }
    const hit = hitNote(e.offsetX, e.offsetY);
    if (hit) {
      canvas.style.cursor = hit.edge === 'body' ? 'move' : 'ew-resize';
    } else {
      canvas.style.cursor = 'copy';   // 铅笔/新建
    }
  });

  canvas.addEventListener('mouseleave', () => { mouseInCanvas = false; });
  document.addEventListener('mouseup', (e) => {
    if (splitDrag) { splitDrag = false; refitSplit(); }   /* E3 段1：分界线松手 → 按新上区高重适配 */
    if (isDragging()) finishAudioDrag();   /* E6 段1 补：单轨音频手势（松手提交） */
    if (drag) submitDrag();
    if (rangeDrag) commitRange();
    if (eraserDrag) commitErase();
  });

  /* M-V3：双击删除音符（命令层即时，一个 commit）
     E3 段1 / v0.2 C2：① 下区双击 = 命中音符（可编区）则删除、否则主副对调；② 主轨为音频时双击波形区 = 试听素材 */
  canvas.addEventListener('dblclick', async (e) => {
    if (splitOn && e.offsetY > splitH1) {
      const rr = regionTrack(e.offsetX, e.offsetY);
      const lhit = (rr && rr.edit) ? hitNote(e.offsetX, e.offsetY) : null;
      if (lhit && store.project) {
        const n = store.score.tracks[lhit.track].notes[lhit.index];
        await postNoteOp(lhit, [{ op: 'remove', track: lhit.track, index: lhit.index }], '双击删除 ' + midiName(n.pitch_midi));
        return;
      }
      swapSplit();
      return;
    }
    const mt = (store.score && store.viewMode === 'single') ? store.score.tracks[store.singleTrack] : null;
    if (mt && mt.kind === 'audio') { previewTrackAudio(mt); return; }
    const hit = hitNote(e.offsetX, e.offsetY);
    if (!hit || !store.project) return;
    const n = store.score.tracks[hit.track].notes[hit.index];
    await postNoteOp(hit, [{ op: 'remove', track: hit.track, index: hit.index }], '双击删除 ' + midiName(n.pitch_midi));
  });

  /* M-V3：右键菜单（删除 / ±半音 / ±八度） */
  canvas.addEventListener('contextmenu', (e) => {
    e.preventDefault();
    const hit = hitNote(e.offsetX, e.offsetY);
    if (!hit || !store.project) { closeNoteMenu(); return; }
    openNoteMenu(hit, e.clientX, e.clientY);
  });
  document.addEventListener('mousedown', (e) => {
    if (noteMenu && !noteMenu.contains(e.target)) closeNoteMenu();
  });

  for (const topic of ['state', 'view', 'selection', 'diff', 'playhead', 'playing', 'tool', 'peaks']) {
    bus.on(topic, draw);
  }
  /* 修正轮2 / v0.2 C2：总谱 ↔ 单轨切换重绘 + 副区出现/消失时主区重适配 */
  let lastSplit = false;
  bus.on('viewmode', () => {
    const single = store.viewMode === 'single';
    const has = !!(single && splitPartner());
    if (single && has !== lastSplit) { lastSplit = has; refitSplit(); return; }
    lastSplit = has;
    draw();
  });
}

/* 修正轮2：外部（main.js 切换显隐后）主动重算尺寸 + 重绘 */
export function resizeNow() {
  if (!canvas) return;
  resize();
  draw();
}
