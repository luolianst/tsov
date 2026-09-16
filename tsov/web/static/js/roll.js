/* roll.js —— 钢琴卷帘 canvas：绘制/缩放/滚动/点击选择/播放头/三色 diff 叠层 */

import { bus } from './events.js';
import { api } from './api.js';
import { KEYS_W, store, scoreBounds, tempo, beatsPerBar, setSelection, setView } from './state.js';
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
  const bq = beatsPerBar();   // M-V6：小节 = bq 个四分拍（原来写死 4）
  let k = Math.floor(t0 / beat);
  for (; k * beat <= tMax; k++) {
    const t = k * beat;
    const x = Math.round(xOf(t)) + 0.5;
    if (x < KEYS_W) continue;
    if (x > W) break;
    if (Math.abs(k / bq - Math.round(k / bq)) < 1e-6) {
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

  /* ---- 手势幽灵预览（最后画，覆盖在上层） ---- */
  drawGhost();
}

/* ---- 编辑手势（M-V2.2 议题 ③：FL/Cubase 心智） ----
   拖音符本体 = move（set_time + set_pitch）；拖左右两端 = resize（set_time）；
   点空白 = 铅笔创建（add）。一次手势 = 一个 EditBatch = 一个 commit。 */
const EDGE_PX = 6;          // 两端热区宽（px）
const MIN_DUR = 0.08;       // 最小音符时长（秒）
let drag = null;            // {mode, track, index, orig, ghost:{start,end,pitch}}
let mouseInCanvas = false;

/* ---- 命中测试（逆序=后画的优先；edge=左右端热区） ---- */
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
  if (drag.mode === 'move-multi') {
    /* M-V3：多选整体移动——逐音画蓝色幽灵 + 原位残影 */
    const v = store.view;
    for (const o of drag.origs) {
      const gx = xOf(o.n.start + drag.ghost.dt), gy = yOf(o.n.pitch_midi + drag.ghost.dp);
      const gw = Math.max(2, (o.n.end - o.n.start) * v.pxPerSec), gh = v.pxPerSemi - 1;
      ctx.fillStyle = 'rgba(79, 195, 247, 0.30)';
      ctx.fillRect(gx, gy + 0.5, gw, gh);
      ctx.strokeStyle = '#4fc3f7';
      ctx.setLineDash([4, 3]);
      ctx.strokeRect(gx + 0.5, gy + 0.5, gw, gh);
      ctx.strokeStyle = 'rgba(255, 255, 255, 0.35)';
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
  ctx.fillStyle = drag.mode === 'create' ? 'rgba(102, 187, 106, 0.35)' : 'rgba(79, 195, 247, 0.30)';
  ctx.fillRect(x, y + 0.5, w, h);
  ctx.strokeStyle = drag.mode === 'create' ? '#66bb6a' : '#4fc3f7';
  ctx.setLineDash([4, 3]);
  ctx.strokeRect(x + 0.5, y + 0.5, w, h);
  ctx.setLineDash([]);
  /* move 模式画原位置残影 */
  if (drag.mode === 'move' && drag.orig) {
    const ox = xOf(drag.orig.start), oy = yOf(drag.orig.pitch_midi);
    const ow = Math.max(2, (drag.orig.end - drag.orig.start) * v.pxPerSec);
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.35)';
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
      if (r.applied) showStatus('已添加 ' + midiName(pitch) + ' @' + (r.commit || '').slice(0, 7));
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
      if (r.applied) showStatus('已移动 ' + d.origs.length + ' 音 @' + (r.commit || '').slice(0, 7));
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
    if (r.applied) showStatus((d.mode === 'move' ? '已移动' : '已缩放') + ' @' + (r.commit || '').slice(0, 7));
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
    if (r.applied) showStatus('已执行 ' + label + ' @' + (r.commit || '').slice(0, 7));
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
    const hit = hitNote(e.offsetX, e.offsetY);
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
      /* 铅笔：创建新音符（默认时长 = 半拍，可拖动拉长） */
      setSelection(0, []);
      const t0 = Math.max(0, snapT(tOf(e.offsetX)));
      const beat = 60 / tempo();
      const dur = Math.max(MIN_DUR, beat / 2);
      drag = { mode: 'create', track: 0, index: null, orig: null,
               ghost: { start: t0, end: t0 + dur, pitch: midiOf(e.offsetY) } };
    }
  });

  canvas.addEventListener('mousemove', (e) => {
    mouseInCanvas = true;
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
    const hit = hitNote(e.offsetX, e.offsetY);
    if (hit) {
      canvas.style.cursor = hit.edge === 'body' ? 'move' : 'ew-resize';
    } else {
      canvas.style.cursor = 'copy';   // 铅笔/新建
    }
  });

  canvas.addEventListener('mouseleave', () => { mouseInCanvas = false; });
  document.addEventListener('mouseup', (e) => {
    if (drag) submitDrag();
  });

  /* M-V3：双击删除音符（命令层即时，一个 commit） */
  canvas.addEventListener('dblclick', async (e) => {
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

  for (const topic of ['state', 'view', 'selection', 'diff', 'playhead', 'playing']) {
    bus.on(topic, draw);
  }
}
