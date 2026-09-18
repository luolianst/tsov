/* dock.js —— 下方面板：效果器 / 音源 / 混音台（UI 批A；可折叠 + localStorage 记忆）
   与命令层 op 直连：add_effect / remove_effect / set_instrument / set_track_mix */

import { bus } from './events.js';
import { api } from './api.js';
import { store, setError } from './state.js';

let dockEl = null, fxPane = null, srcPane = null, mixPane = null, hintEl = null;
let meta = { programs: [], effect_kinds: [] };
let metaLoaded = false;

function selectedTrack() {
  const i = (store.selection && store.selection.track) || 0;
  return store.score && store.score.tracks[i] ? { track: store.score.tracks[i], index: i } : null;
}

async function post(label, commands) {
  if (!store.project) { setError('先打开一个工程'); return null; }
  try {
    const r = await api.postBatch(store.project, label, commands, '面板：' + label);
    if (r.applied) bus.dispatch('toast', '已执行 ' + label + ' @' + String(r.commit || '').slice(0, 7));
    else setError('被拒：' + (r.errors || []).join('；'));
    return r;
  } catch (e) { setError(e.message); return null; }
}

async function loadMeta() {
  if (metaLoaded) return;
  try {
    const r = await api.meta();
    meta = { programs: r.programs || [], effect_kinds: r.effect_kinds || [] };
    metaLoaded = true;
  } catch (e) { /* 端点缺失时用兜底列表 */ }
  if (!meta.effect_kinds.length) {
    meta.effect_kinds = ['reverb', 'delay', 'compressor', 'chorus', 'distortion', 'gain', 'highpass', 'lowpass', 'limiter', 'brickwall', 'phaser'];
  }
}

/* ---------------- 效果器 tab ---------------- */
function renderFx() {
  if (!fxPane) return;
  fxPane.innerHTML = '';
  const cur = selectedTrack();
  if (!cur) {
    fxPane.innerHTML = '<div class="fx-none">（先选一条轨道）</div>';
    return;
  }
  const chain = (cur.track.instrument && cur.track.instrument.effects) || [];
  const wrap = document.createElement('div');
  wrap.className = 'fx-chain';
  chain.forEach((fx, i) => {
    const slot = document.createElement('div');
    slot.className = 'fx-slot';
    const hd = document.createElement('div');
    hd.className = 'fx-hd';
    const dot = document.createElement('span');
    dot.className = 'fx-dot';
    const nm = document.createElement('span');
    nm.className = 'fx-name';
    nm.textContent = fx.type;
    hd.appendChild(dot);
    hd.appendChild(nm);
    const sub = document.createElement('div');
    sub.className = 'fx-sub';
    const keys = Object.keys(fx.params || {});
    sub.textContent = keys.slice(0, 3).map((k) => k.replace(/_db|_hz|_seconds|_level/g, '') + ' ' + fx.params[k]).join(' · ') || '默认参数';
    const x = document.createElement('button');
    x.className = 'fx-x';
    x.textContent = '✕';
    x.title = '移除该效果（一个 commit，可撤销）';
    x.onclick = () => post('移除效果 ' + fx.type, [{ op: 'remove_effect', track: cur.index, index: i }]);
    slot.appendChild(hd);
    slot.appendChild(sub);
    slot.appendChild(x);
    wrap.appendChild(slot);
    if (i < chain.length - 1) {
      const ar = document.createElement('span');
      ar.className = 'fx-arrow';
      ar.textContent = '›';
      wrap.appendChild(ar);
    }
  });
  const kindSel = document.createElement('select');
  kindSel.className = 'fx-kind';
  kindSel.title = '选择效果类型后点「＋」加入链尾';
  for (const k of meta.effect_kinds) {
    const o = document.createElement('option');
    o.value = k; o.textContent = k;
    kindSel.appendChild(o);
  }
  const add = document.createElement('button');
  add.className = 'fx-add';
  add.textContent = '＋';
  add.title = '添加效果（默认参数，可在检查器/预设里再调）';
  add.onclick = async () => {
    await post('添加效果 ' + kindSel.value, [{ op: 'add_effect', track: cur.index, value: { type: kindSel.value } }]);
  };
  wrap.appendChild(kindSel);
  wrap.appendChild(add);
  fxPane.appendChild(wrap);
}

/* ---------------- 音源 tab ---------------- */
function renderSrc() {
  if (!srcPane) return;
  srcPane.innerHTML = '';
  const cur = selectedTrack();
  if (!cur) {
    srcPane.innerHTML = '<div class="fx-none">（先选一条轨道）</div>';
    return;
  }
  const inst = cur.track.instrument || {};
  const form = document.createElement('div');
  form.className = 'src-form';

  const row1 = document.createElement('div');
  row1.className = 'src-row';
  const l1 = document.createElement('label');
  l1.textContent = '音源';
  const sel = document.createElement('select');
  const curProg = inst.program || '';
  const isPreset = meta.programs.includes(curProg);
  const o0 = document.createElement('option');
  o0.value = ''; o0.textContent = isPreset ? '（默认钢琴）' : ('当前：' + (curProg || '默认'));
  if (!isPreset) { o0.value = curProg; }
  sel.appendChild(o0);
  for (const name of meta.programs) {
    const o = document.createElement('option');
    o.value = name; o.textContent = name;
    if (name === curProg) o.selected = true;
    sel.appendChild(o);
  }
  sel.onchange = () => post('音源 ' + (sel.value || '默认'), [{ op: 'set_instrument', track: cur.index, value: { program: sel.value } }]);
  row1.appendChild(l1);
  row1.appendChild(sel);

  const row2 = document.createElement('div');
  row2.className = 'src-row';
  const l2 = document.createElement('label');
  l2.textContent = '路径';
  const pth = document.createElement('input');
  pth.placeholder = 'vst3:<内层二进制路径> 或 sfz:<路径>（回车应用）';
  pth.value = (curProg.startsWith('vst3:') || curProg.startsWith('sfz:')) ? curProg : '';
  pth.onkeydown = (e) => {
    if (e.key === 'Enter' && pth.value.trim()) {
      post('音源路径', [{ op: 'set_instrument', track: cur.index, value: { program: pth.value.trim() } }]);
    }
  };
  row2.appendChild(l2);
  row2.appendChild(pth);

  const note = document.createElement('div');
  note.className = 'src-note';
  note.textContent = 'backend=' + (inst.backend || 'fluidsynth') + ' ｜ GM 名 / vst3: / sfz: / 空串=默认 ｜ 改动一个 commit、可撤销';

  form.appendChild(row1);
  form.appendChild(row2);
  form.appendChild(note);
  srcPane.appendChild(form);
}

/* ---------------- 混音台 tab ---------------- */
function renderMix() {
  if (!mixPane) return;
  mixPane.innerHTML = '';
  if (!store.score || !store.score.tracks.length) {
    mixPane.innerHTML = '<div class="fx-none">（无音轨）</div>';
    return;
  }
  const wrap = document.createElement('div');
  wrap.className = 'mix-strip';
  store.score.tracks.forEach((tr, ti) => {
    const inst = tr.instrument || {};
    const cell = document.createElement('div');
    cell.className = 'fader';
    const nm = document.createElement('div');
    nm.className = 'f-name';
    nm.textContent = tr.name || ('track ' + ti);
    nm.title = nm.textContent;
    const val = Math.round((inst.volume != null ? inst.volume : 1) * 100);
    const rg = document.createElement('input');
    rg.type = 'range'; rg.min = '0'; rg.max = '200'; rg.value = String(val);
    rg.title = '音量（%）';
    const vv = document.createElement('div');
    vv.className = 'f-val';
    vv.textContent = val + '%';
    rg.addEventListener('input', () => { vv.textContent = rg.value + '%'; });
    rg.addEventListener('change', () => post('音量 ' + rg.value + '%', [{ op: 'set_track_mix', track: ti, value: { volume: Number(rg.value) / 100 } }]));
    const ms = document.createElement('div');
    ms.className = 'f-ms';
    const m = document.createElement('button');
    m.className = 'ms' + (tr.mute ? ' on' : '');
    m.textContent = 'M';
    m.onclick = () => post(tr.mute ? '取消静音' : '静音', [{ op: 'set_track_mix', track: ti, value: { mute: !tr.mute } }]);
    const s = document.createElement('button');
    s.className = 'ms solo' + (tr.solo ? ' on' : '');
    s.textContent = 'S';
    s.onclick = () => post(tr.solo ? '取消独奏' : '独奏', [{ op: 'set_track_mix', track: ti, value: { solo: !tr.solo } }]);
    ms.appendChild(m);
    ms.appendChild(s);
    cell.appendChild(nm);
    cell.appendChild(rg);
    cell.appendChild(vv);
    cell.appendChild(ms);
    wrap.appendChild(cell);
  });
  mixPane.appendChild(wrap);
}

function renderTabs() {
  const cur = selectedTrack();
  if (hintEl) hintEl.textContent = cur ? ((cur.track.name || ('track ' + cur.index)) + ' · 轨道参数') : '';
}

export function renderAll() {
  renderFx();
  renderSrc();
  renderMix();
  renderTabs();
}

function applyFold(folded) {
  if (!dockEl) return;
  dockEl.classList.toggle('folded', folded);
  const btn = dockEl.querySelector('#dock-fold');
  if (btn) btn.textContent = folded ? '▸ 展开' : '▾ 收起';
  try { localStorage.setItem('tsov.dock', folded ? 'folded' : 'open'); } catch (e) { /* ignore */ }
}

export function init(el) {
  dockEl = el;
  fxPane = dockEl.querySelector('#dock-fx');
  srcPane = dockEl.querySelector('#dock-src');
  mixPane = dockEl.querySelector('#dock-mix');
  hintEl = dockEl.querySelector('#dock-hint');

  /* tab 切换 */
  const tabs = Array.from(dockEl.querySelectorAll('.dock-tab'));
  const panes = { fx: fxPane, src: srcPane, mix: mixPane };
  for (const t of tabs) {
    t.addEventListener('click', () => {
      for (const x of tabs) x.classList.toggle('active', x === t);
      const name = t.dataset.pane;
      for (const k of Object.keys(panes)) panes[k].classList.toggle('active', k === name);
    });
  }

  /* 折叠（记忆；无记忆时：紧凑档默认折叠） */
  dockEl.querySelector('#dock-fold').addEventListener('click', () => {
    applyFold(!dockEl.classList.contains('folded'));
  });
  let saved = null;
  try { saved = localStorage.getItem('tsov.dock'); } catch (e) { /* ignore */ }
  const compact = window.matchMedia('(max-width: 1439px)').matches;
  const folded = saved ? saved === 'folded' : compact;
  dockEl.classList.toggle('folded', folded);
  const btn = dockEl.querySelector('#dock-fold');
  if (btn) btn.textContent = folded ? '▸ 展开' : '▾ 收起';

  bus.on('state', renderAll);
  bus.on('selection', renderAll);
  loadMeta().then(renderAll);
  renderAll();
}
