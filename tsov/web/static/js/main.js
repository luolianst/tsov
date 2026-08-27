/* main.js —— 启动：加载工程列表 → 打开工程 → 挂各模块（模块间只经 api/state/events 交互） */

import { api } from './api.js';
import { bus, connectEvents } from './events.js';
import { store, setState, setError, toast, fitView, setView } from './state.js';
import * as roll from './roll.js';
import * as timeline from './timeline.js';
import { initDiffBadge } from './diff.js';
import * as chat from './chat.js';
import * as playback from './playback.js';

const $ = (id) => document.getElementById(id);

/* ---------------- 状态栏 / 工具栏 ---------------- */

function refreshStatusBar() {
  const first = (store.summary || '').split('\n')[0] || '（无摘要）';
  $('status-summary').textContent = store.project ? (store.project + ' · ' + first) : '未打开工程';
}

function refreshToolbar() {
  $('btn-undo').disabled = !store.history.can_undo;
  $('btn-redo').disabled = !store.history.can_redo;
  const sel = $('project-select');
  sel.innerHTML = '';
  for (const p of store.projects) {
    const opt = document.createElement('option');
    opt.value = p;
    opt.textContent = p;
    if (p === store.project) opt.selected = true;
    sel.appendChild(opt);
  }
}

let toastTimer = 0;
function showError(msg) {
  $('status-error').textContent = msg || '';
  if (msg) console.warn('[tsov]', msg);
}
function showToast(msg) {
  if (!msg) return;
  refreshStatusBar();   // toast 并入状态栏展示（原型极简）
  $('status-summary').textContent = 'ℹ ' + msg;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(refreshStatusBar, 4000);
}

/* ---------------- 工程列表 / 打开 ---------------- */

async function loadProjects(keep) {
  try {
    const r = await api.listProjects();
    store.projects = r.projects || [];
    if (!keep && store.projects.length && !store.projects.includes(store.project)) {
      await openProject(store.projects[0]);
    } else {
      refreshToolbar();
    }
  } catch (e) { setError(e.message); }
}

async function openProject(name) {
  if (!name) return;
  try {
    const s = await api.getState(name);
    setState(s, { preserveSelection: false });
    store.project = name;
    connectEvents(name);
    bus.dispatch('state');
    requestAnimationFrame(() => {
      const rc = $('roll').getBoundingClientRect();
      fitView(rc.width, rc.height);
    });
    toast('已打开工程 ' + name);
  } catch (e) { setError(e.message); }
}

async function newProject() {
  const name = prompt('新工程名（将创建 output/<name>/ 并 git init）：', '');
  if (!name) return;
  try {
    await api.createProject(name.trim());
    await loadProjects(false);
    await openProject(name.trim());
  } catch (e) { setError(e.message); }
}

/* ---------------- 启动 ---------------- */

function boot() {
  roll.init($('roll'));
  timeline.init($('ruler'), $('track-list'), $('segments-info'), $('meta-info'));
  initDiffBadge($('status-diff'));
  chat.init({
    logEl: $('chat-log'),
    inputEl: $('chat-input'),
    sendBtn: $('btn-send'),
    undoRoundBtn: $('btn-undo-round'),
    rollbackSel: $('rollback-select'),
    rollbackBtn: $('btn-rollback'),
    chipsEl: $('chat-chips'),
  });
  playback.init({
    playWavBtn: $('btn-play-wav'),
    stopBtn: $('btn-stop'),
    playHostBtn: $('btn-play-host'),
    renderBtn: $('btn-render'),
  });

  /* SSE → store（本地直接消费事件负载，契约 §六 数据流） */
  bus.on('state_updated', (s) => setState(s, { preserveSelection: true }));
  bus.on('diff_applied', (d) => {
    const box = $('status-diff');
    const parts = [];
    if (d.added && d.added.length) parts.push('+' + d.added.length);
    if (d.removed && d.removed.length) parts.push('-' + d.removed.length);
    if (d.changed && d.changed.length) parts.push('~' + d.changed.length);
    box.textContent = parts.length ? ('本轮 diff ' + parts.join(' ') + (d.commit ? ' @' + String(d.commit).slice(0, 7) : '')) : '';
  });

  /* 工具栏 */
  $('project-select').addEventListener('change', (e) => openProject(e.target.value));
  $('btn-refresh-projects').addEventListener('click', () => loadProjects(true));
  $('btn-new-project').addEventListener('click', newProject);
  $('btn-undo').addEventListener('click', async () => {
    if (!store.project) return;
    try { await api.undo(store.project); } catch (e) { setError(e.message); }
  });
  $('btn-redo').addEventListener('click', async () => {
    if (!store.project) return;
    try { await api.redo(store.project); } catch (e) { setError(e.message); }
  });
  $('btn-zoom-in').addEventListener('click', () => setView({ pxPerSec: Math.min(600, store.view.pxPerSec * 1.25) }));
  $('btn-zoom-out').addEventListener('click', () => setView({ pxPerSec: Math.max(8, store.view.pxPerSec / 1.25) }));
  $('btn-fit').addEventListener('click', () => {
    const rc = $('roll').getBoundingClientRect();
    fitView(rc.width, rc.height);
  });
  $('btn-clear-diff').addEventListener('click', () => {
    store.diff = null;
    $('status-diff').textContent = '';
    bus.dispatch('diff');
  });

  bus.on('state', () => { refreshStatusBar(); refreshToolbar(); });
  bus.on('error', showError);
  bus.on('toast', showToast);

  loadProjects(false);
}

boot();
