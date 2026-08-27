/* roll.js —— 钢琴卷帘 canvas：绘制/缩放/滚动/点击选择/播放头/三色 diff 叠层 */

import { bus } from './events.js';
import { KEYS_W, store, scoreBounds, tempo, setSelection, setView } from './state.js';
import { diffLayers } from './diff.js';

export const TRACK_COLORS = ['#4fc3f7', '#aed581', '#ffb74d', '#f06292', '#ba68c8', '#4db6ac', '#fff176', '#90a4ae'];
const BLACK = new Set([1, 3, 6, 8, 10]);
const NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B'];

let canvas, ctx, W = 0, H = 0, dpr = 1;

export function xOf(t) { return KEYS_W + (t - store.view.scrollSec) * store.view.pxPerSec; }
export function tOf(x) { return store.view.scrollSec + (x - KEYS_W) / store.view.pxPerSec; }
export function yOf(midi) { return (store.view.midiTop - midi) * store.view.pxPerSemi; }
export function midiOf(y) { return Math.round(store.view.midiTop - y / store.view.pxPerSemi); }

function noteName(m) { return NAMES[((m % 12) + 12) % 12] + (Math.floor(m / 12) - 1); }

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
  const layers = diffLayers();

  /* ---- 琴键行底色 ---- */
  const topMidi = Math.ceil(v.midiTop);
  for (let i = 0; i < rowsVisible(); i++) {
    const m = topMidi - i;
    const y = yOf(m);
    ctx.fillStyle = BLACK.has(((m % 12) + 12) % 12) ? '#101216' : '#171a20';
    ctx.fillRect(KEYS_W, y, W - KEYS_W, v.pxPerSemi);
  }

  /* ---- 拍/小节竖线 ---- */
  const beat = 60 / tempo();
  const t0 = Math.max(0, tOf(KEYS_W));
  const t1 = tOf(W);
  const tEnd = scoreBounds().tEnd;
  const tMax = Math.max(t1, tEnd + 2);
  ctx.lineWidth = 1;
  let k = Math.floor(t0 / beat);
  for (; k * beat <= tMax; k++) {
    const t = k * beat;
    const x = Math.round(xOf(t)) + 0.5;
    if (x < KEYS_W) continue;
    if (x > W) break;
    if (k % 4 === 0) {
      ctx.strokeStyle = '#39414f';
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke();
    } else if (v.pxPerSec * beat > 7) {
      ctx.strokeStyle = '#242a34';
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke();
    }
  }

  /* ---- diff 红残影（removed，不在当前谱里） ---- */
  if (layers) {
    for (const n of layers.removed) {
      const x = xOf(n.start), y = yOf(n.pitch_midi);
      const w = Math.max(2, (n.end - n.start) * v.pxPerSec), h = v.pxPerSemi - 1;
      ctx.fillStyle = 'rgba(229, 57, 53, 0.30)';
      ctx.fillRect(x, y + 0.5, w, h);
      ctx.strokeStyle = 'rgba(229, 57, 53, 0.8)';
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
      const color = TRACK_COLORS[ti % TRACK_COLORS.length];
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
          if (cls === 'add') { fill = '#2e7d32'; stroke = '#66bb6a'; }
          else if (cls === 'mod') { stroke = '#f9a825'; }
        }

        /* 黄对照：旧音残影（虚线框在旧位置） */
        if (layers && ti === 0) {
          const old = layers.ghostOf(n);
          if (old) {
            const ox = xOf(old.start), oy = yOf(old.pitch_midi);
            const ow = Math.max(2, (old.end - old.start) * v.pxPerSec);
            ctx.fillStyle = 'rgba(249, 168, 37, 0.18)';
            ctx.fillRect(ox, oy + 0.5, ow, v.pxPerSemi - 1);
            ctx.strokeStyle = 'rgba(249, 168, 37, 0.7)';
            ctx.setLineDash([3, 2]);
            ctx.strokeRect(ox + 0.5, oy + 0.5, ow, v.pxPerSemi - 1);
            ctx.setLineDash([]);
            /* 新旧连线 */
            ctx.strokeStyle = 'rgba(249, 168, 37, 0.35)';
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
        ctx.strokeStyle = stroke || 'rgba(0,0,0,0.45)';
        ctx.strokeRect(x + 0.5, y + 0.5, w, h);

        /* 选中高亮 */
        if (sel.track === ti && sel.indices.includes(ni)) {
          ctx.strokeStyle = '#ffffff';
          ctx.lineWidth = 2;
          ctx.strokeRect(x + 1, y + 1, w - 1, h - 1);
          ctx.lineWidth = 1;
        }
      }
    });
  }

  /* ---- 播放头 ---- */
  const px = xOf(store.playhead);
  if (store.playing && px >= KEYS_W && px <= W) {
    ctx.strokeStyle = '#ff7043';
    ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.moveTo(px, 0); ctx.lineTo(px, H); ctx.stroke();
    ctx.fillStyle = '#ff7043';
    ctx.beginPath();
    ctx.moveTo(px - 5, 0); ctx.lineTo(px + 5, 0); ctx.lineTo(px, 7);
    ctx.closePath(); ctx.fill();
    ctx.lineWidth = 1;
  }

  /* ---- 左侧钢琴键盘列 ---- */
  ctx.fillStyle = '#1b1e25';
  ctx.fillRect(0, 0, KEYS_W, H);
  for (let i = 0; i < rowsVisible(); i++) {
    const m = topMidi - i;
    const y = yOf(m);
    const pc = ((m % 12) + 12) % 12;
    ctx.fillStyle = BLACK.has(pc) ? '#22262e' : '#e8eaf0';
    ctx.fillRect(0, y, KEYS_W - 4, v.pxPerSemi);
    if (pc === 0 && v.pxPerSemi >= 9) {
      ctx.fillStyle = '#444a56';
      ctx.font = '10px sans-serif';
      ctx.fillText(noteName(m), 6, y + v.pxPerSemi - 3);
    }
  }
  ctx.strokeStyle = '#2c313c';
  ctx.beginPath(); ctx.moveTo(KEYS_W - 3.5, 0); ctx.lineTo(KEYS_W - 3.5, H); ctx.stroke();
}

/* ---- 命中测试（逆序=后画的优先） ---- */
function hitNote(mx, my) {
  if (!store.score) return null;
  const v = store.view;
  for (let ti = store.score.tracks.length - 1; ti >= 0; ti--) {
    if (store.hiddenTracks.has(ti)) continue;
    const notes = store.score.tracks[ti].notes;
    for (let ni = notes.length - 1; ni >= 0; ni--) {
      const n = notes[ni];
      const x = xOf(n.start), y = yOf(n.pitch_midi);
      const w = Math.max(2, (n.end - n.start) * v.pxPerSec), h = v.pxPerSemi - 1;
      if (mx >= x - 1 && mx <= x + w + 1 && my >= y && my <= y + h) return { track: ti, index: ni };
    }
  }
  return null;
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
    const hit = hitNote(e.offsetX, e.offsetY);
    if (!hit) { setSelection(0, []); return; }
    const cur = store.selection;
    let idx;
    if (e.ctrlKey || e.shiftKey) {
      idx = cur.track === hit.track ? cur.indices.slice() : [];
      const at = idx.indexOf(hit.index);
      if (at >= 0) idx.splice(at, 1); else idx.push(hit.index);
    } else {
      idx = [hit.index];
    }
    setSelection(hit.track, idx);
    bus.dispatch('note-selected', hit);
  });

  canvas.addEventListener('mousemove', (e) => {
    canvas.style.cursor = hitNote(e.offsetX, e.offsetY) ? 'pointer' : 'default';
  });

  for (const topic of ['state', 'view', 'selection', 'diff', 'playhead', 'playing']) {
    bus.on(topic, draw);
  }
}
