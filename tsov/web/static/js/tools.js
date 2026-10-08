/* tools.js —— M-V8 E5：编辑工具集（工具条 / 快捷键 / 微推 / 剪贴板）。
   工具键仅单轨卷帘视图生效；全部动作走命令层 /batch（ADR-0017 同一动作路径）。
   快捷键：1-5 切换工具 · Esc 回智能指针 · 按住 Alt 临时剪刀 ·
          ←/→ 微推一格（Shift=一小节） · Ctrl+C/X/V/D 复制/剪切/粘贴/再制 */

import { bus } from './events.js';
import { api } from './api.js';
import { store, setTool, setClipboard, setSnap, setError, refTag, toast, tempo, beatsPerBar, scoreBounds, timeSig, setAutoLane } from './state.js';

const $ = (id) => document.getElementById(id);

/* 工具定义：[id, 标签, 键位] */
export const TOOLS = [
  ['smart', '🖱 智能', '1'],
  ['range', '▭ 范围', '2'],
  ['scissors', '✂ 剪刀', '3'],
  ['glue', '🩹 胶水', '4'],
  ['eraser', '🧽 橡皮', '5'],
];

let bar = null, snapChk = null, gridSel = null, swingIn = null, quantBtn = null, chipsEl = null;
let autoChk = null, autoSel = null;   // M-V8 E5 段2：自动化 lane 开关/参数
let altTemp = false;          // 按住 Alt = 临时剪刀
let prevTool = 'smart';

/* 网格口径：1/4=1 拍 · 1/8=1/2 拍 · 1/16=1/4 拍
   gridFrac = 拍比例（吸附用）；gridNum = 命令层 grid（cell=beat/grid → 1/4:1 · 1/8:2 · 1/16:4） */
function gridDen() { return gridSel ? (Number(gridSel.value) || 16) : 16; }
function gridFrac() { return 4 / gridDen(); }
function gridNum() { return gridDen() / 4; }
function gridStepSec() { return (60 / tempo()) * gridFrac(); }
function gridLabel() { return '1/' + gridDen(); }

function isInput(t) {
  if (!t) return false;
  const tag = (t.tagName || '').toUpperCase();
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || !!t.isContentEditable;
}

export function init(els) {
  bar = els.bar; snapChk = els.snap; gridSel = els.grid; swingIn = els.swing; quantBtn = els.quant; chipsEl = els.chips;
  if (!bar) return;

  for (const [id, , key] of TOOLS) {
    const btn = bar.querySelector('button[data-tool="' + id + '"]');
    if (btn) {
      btn.title = btn.title + '（快捷键 ' + key + '）';
      btn.addEventListener('click', () => setTool(id));
    }
  }
  renderBar();
  bus.on('tool', renderBar);
  bus.on('state', () => { renderBar(); renderChips(); });

  snapChk.addEventListener('change', setSnapFromBar);
  gridSel.addEventListener('change', setSnapFromBar);
  quantBtn.addEventListener('click', () => { doQuantize(); });

  /* M-V8 E5 段2：自动化 lane（开关 / 参数切换；数据提交在 autoroll.js） */
  autoChk = bar.querySelector('#tl-auto');
  autoSel = bar.querySelector('#tl-auto-param');
  if (autoChk) autoChk.addEventListener('change', () => setAutoLane({ open: autoChk.checked }));
  if (autoSel) autoSel.addEventListener('change', () => setAutoLane({ param: autoSel.value }));
  bus.on('auto', renderBar);
  /* v0.2 批C 前段（R2 地基件）：选项由注册表快照生成（缺快照回退内置；含「音量+声像」双道） */
  fillAutoParamOptions();
  bus.on('meta', fillAutoParamOptions);

  document.addEventListener('keydown', onKeydown);
  document.addEventListener('keyup', (e) => {
    if (e.key === 'Alt' && altTemp) { altTemp = false; setTool(prevTool || 'smart'); }
  });
  window.addEventListener('blur', () => { if (altTemp) { altTemp = false; setTool(prevTool || 'smart'); } });

  /* CDP / 外部 agent 观察口（动作仍走命令层） */
  window.__tsovTools = {
    setTool: (t) => setTool(t),
    copy: copySel,
    cut: cutSel,
    paste: () => pasteTo(store.playhead),
    duplicate: duplicateSel,
    quantize: () => doQuantize(),
    nudge: (dt) => nudge(dt),
  };
}

/* ---- 工具条渲染（active 高亮 + 吸附开关同步） ---- */
export function renderBar() {
  if (!bar) return;
  for (const [id] of TOOLS) {
    const btn = bar.querySelector('button[data-tool="' + id + '"]');
    if (btn) btn.classList.toggle('on', store.tool === id);
  }
  if (snapChk) snapChk.checked = !!store.snapFrac;
  if (autoChk) autoChk.checked = !!store.autoLane.open;
  if (autoSel) autoSel.value = store.autoLane.param;
}

/* v0.2 批C 前段（R2 地基件）：自动化参数选项（注册表快照 → 选项；缺快照回退内置两项）。
   「音量+声像」= 双道堆叠（通用道框架演示；value=both 由 setAutoLane 校验放行）。 */
function fillAutoParamOptions() {
  if (!autoSel) return;
  const specs = (store.metaParams && store.metaParams.automation) || null;
  const items = specs
    ? Object.keys(specs).filter((k) => specs[k] && specs[k].automatable !== false).map((k) => [k, specs[k].label || k])
    : [['volume', '音量'], ['pan', '声像']];
  const keep = store.autoLane.param;
  autoSel.innerHTML = '';
  for (const [id, label] of items) {
    const o = document.createElement('option');
    o.value = id;
    o.textContent = label;
    autoSel.appendChild(o);
  }
  if (items.some(([id]) => id === 'volume') && items.some(([id]) => id === 'pan')) {
    const o2 = document.createElement('option');
    o2.value = 'both';
    o2.textContent = '音量+声像';
    autoSel.appendChild(o2);
  }
  autoSel.value = ['volume', 'pan', 'both'].includes(keep) ? keep : 'volume';
}

/* ---- 状态栏 chips：44.1kHz · 16-bit · 拍号 · 调号 · 时长 ---- */
export function renderChips() {
  if (!chipsEl) return;
  if (!store.score) { chipsEl.textContent = ''; return; }
  const [n, d] = timeSig();
  const b = scoreBounds();
  const kc = store.score.key_candidates && store.score.key_candidates[0];
  const key = (kc && kc.key) || '—';
  const secs = Math.max(0, Math.round(b.tEnd));
  const dur = Math.floor(secs / 60) + ':' + String(secs % 60).padStart(2, '0');
  chipsEl.textContent = '44.1kHz · 16-bit · ' + n + '/' + d + ' · ' + key + ' · ' + dur;
}

/* ---- Esc 回位（main.js 的 Esc 分支先问这里） ---- */
export function cancelTool() {
  if (altTemp) { altTemp = false; }
  if (store.tool !== 'smart') { setTool('smart'); return true; }
  return false;
}

function setSnapFromBar() {
  const on = !!(snapChk && snapChk.checked);
  setSnap(on ? gridFrac() : 0);
  toast(on ? ('吸附开：网格 ' + gridLabel()) : '吸附关');
}

/* ---- 量化（选区 / 整轨 + Swing）---- */
async function doQuantize() {
  if (!store.project || !store.score) { setError('先打开工程'); return; }
  const sel = store.selection;
  if (!store.score.tracks[sel.track]) return;
  const value = { grid: gridNum() };
  const sw = Number(swingIn && swingIn.value || 0);
  if (sw > 0) value.swing = Math.min(1, Math.max(0, sw / 100));
  if (sel.indices.length) value.indices = sel.indices.slice();
  const scope = sel.indices.length ? (sel.indices.length + ' 音选区') : '整轨';
  try {
    const r = await api.postBatch(store.project, '量化 ' + scope,
      [{ op: 'quantize_time', track: sel.track, value }],
      '量化：' + gridLabel() + (sw > 0 ? ' · swing ' + sw + '%' : ''));
    if (r.applied) toast('已量化 ' + scope + ' ' + refTag(r));
    else setError('被拒：' + (r.errors || []).join('；'));
  } catch (e) { setError(e.message); }
}

/* ---- 微推（选区 / 整轨；dtime 秒）---- */
async function nudge(dt) {
  if (!store.project || !store.score) return;
  const sel = store.selection;
  if (!store.score.tracks[sel.track]) return;
  const value = { dtime: Math.round(dt * 1e6) / 1e6 };
  if (sel.indices.length) value.indices = sel.indices.slice();
  try {
    const r = await api.postBatch(store.project, '微推',
      [{ op: 'shift_notes', track: sel.track, value }],
      '微推 ' + (dt >= 0 ? '+' : '') + dt.toFixed(3) + 's');
    if (!r.applied) setError('被拒：' + (r.errors || []).join('；'));
  } catch (e) { setError(e.message); }
}

/* ---- 剪贴板（单轨；粘贴到播放头；Ctrl+D 再制紧跟选区） ---- */
function selNotes() {
  const sel = store.selection;
  const tr = store.score && store.score.tracks[sel.track];
  if (!tr || !sel.indices.length) return null;
  const notes = sel.indices.map((i) => tr.notes[i]).filter(Boolean);
  if (!notes.length) return null;
  return { track: sel.track, notes, indices: sel.indices.slice() };
}

function copySel() {
  const s = selNotes();
  if (!s) { toast('先选中音符（框选 / Ctrl 多选）'); return null; }
  const anchor = Math.min(...s.notes.map((n) => n.start));
  setClipboard(s.notes, anchor, s.track);
  toast('已复制 ' + s.notes.length + ' 音');
  return s;
}

async function cutSel() {
  const s = copySel();
  if (!s) return;
  const idxs = s.indices.slice().sort((a, b) => b - a);   // 降序：索引不漂移
  try {
    const r = await api.postBatch(store.project, '剪切 ' + idxs.length + ' 音',
      idxs.map((i) => ({ op: 'remove', track: s.track, index: i })), '剪切：' + idxs.length + ' 音');
    if (!r.applied) setError('被拒：' + (r.errors || []).join('；'));
  } catch (e) { setError(e.message); }
}

async function duplicateSel() {
  const s = selNotes();
  if (!s) { toast('先选中音符'); return; }
  const anchor = Math.min(...s.notes.map((n) => n.start));
  const end = Math.max(...s.notes.map((n) => n.end));
  setClipboard(s.notes, anchor, s.track);
  await pasteTo(end);
}

async function pasteTo(t0) {
  if (!store.project || !store.score) return;
  const clip = store.clipboard;
  if (!clip.length) { toast('剪贴板为空（先 Ctrl+C）'); return; }
  const track = store.score.tracks[store.selection.track] ? store.selection.track : store.clipTrack;
  if (!store.score.tracks[track]) { toast('目标轨不存在'); return; }
  let dt = t0 - store.clipAnchor;
  const minStart = Math.min(...clip.map((n) => n.start));
  if (minStart + dt < 0) dt = -minStart;                // 越出 0 点 → 整体平移到 0
  const r6 = (x) => Math.round(x * 1e6) / 1e6;
  try {
    const r = await api.postBatch(store.project, '粘贴 ' + clip.length + ' 音',
      clip.map((n) => ({ op: 'add', track, index: null, value: {
        pitch_midi: n.pitch_midi, start: r6(n.start + dt), end: r6(n.end + dt), velocity: n.velocity } })),
      '粘贴：' + clip.length + ' 音');
    if (r.applied) toast('已粘贴 ' + clip.length + ' 音 ' + refTag(r));
    else setError('被拒：' + (r.errors || []).join('；'));
  } catch (e) { setError(e.message); }
}

/* ---- 键盘 ---- */
function onKeydown(e) {
  /* 按住 Alt = 临时剪刀（松回原工具） */
  if (e.key === 'Alt' && !e.ctrlKey && !e.metaKey) {
    if (store.viewMode === 'single' && !isInput(e.target) && !altTemp) {
      altTemp = true;
      prevTool = store.tool;
      if (store.tool !== 'scissors') setTool('scissors');
      e.preventDefault();
    }
    return;
  }
  if (isInput(e.target)) return;

  /* Ctrl 组合：剪贴板（仅卷帘视图） */
  if ((e.ctrlKey || e.metaKey) && !e.altKey) {
    if (!e.shiftKey && store.viewMode === 'single' && store.score && store.project) {
      const k = String(e.key || '').toLowerCase();
      if (k === 'c' || k === 'x' || k === 'v' || k === 'd') {
        e.preventDefault();
        if (k === 'c') copySel();
        else if (k === 'x') cutSel();
        else if (k === 'v') pasteTo(store.playhead);
        else duplicateSel();
      }
    }
    return;
  }
  if (e.altKey || e.metaKey) return;
  if (!store.project || !store.score) return;
  if (store.viewMode !== 'single') return;   // 工具键仅卷帘生效

  /* 1-5 切换工具 */
  if (e.key >= '1' && e.key <= '5' && !e.shiftKey) {
    e.preventDefault();
    setTool(TOOLS[Number(e.key) - 1][0]);
    return;
  }
  /* 微推：←/→ 一格 · Shift+←/→ 一小节 */
  if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
    e.preventDefault();
    const dt = (e.key === 'ArrowLeft' ? -1 : 1) *
      (e.shiftKey ? (60 / tempo()) * beatsPerBar() : gridStepSec());
    nudge(dt);
  }
}
