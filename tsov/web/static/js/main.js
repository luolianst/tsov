/* main.js —— 启动：加载工程列表 → 打开工程 → 挂各模块（模块间只经 api/state/events 交互） */

import { api } from './api.js';
import { bus, connectEvents } from './events.js';
import { store, setState, setError, toast, fitView, setView, clearDiff, setAnnotations, clearAnnotations, setSnap } from './state.js';
import * as roll from './roll.js';
import * as timeline from './timeline.js';
import * as segbar from './segbar.js';
import * as dock from './dock.js';
import { initTheme } from './theme.js';
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
  if (!store.projects.length) {
    /* M-V6 批1：空列表引导（不再是无信息的空下拉） */
    const opt = document.createElement('option');
    opt.value = '';
    opt.textContent = '（无工程——「＋ 新建」或「📥 导入」）';
    sel.appendChild(opt);
    return;
  }
  for (const p of store.projects) {
    const opt = document.createElement('option');
    opt.value = p.name;
    const title = (p.title && p.title !== p.name) ? ' — ' + p.title : '';
    opt.textContent = p.name + title + ' · ' + p.tracks + '轨 · ' + p.notes + '音';
    opt.title = '最近修改：' + new Date((p.mtime || 0) * 1000).toLocaleString();
    if (p.name === store.project) opt.selected = true;
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
    const names = store.projects.map((p) => p.name);
    if (!keep && store.projects.length && !names.includes(store.project)) {
      /* M-V6 批1：后端按 mtime 倒序 → [0] = 最近修改工程（不再无脑开列表第一个） */
      await openProject(store.projects[0].name);
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
    clearDiff();   // 审计修 M-V2.3：清掉上一个工程的 diff 徽章（切工程残留实测）
    store.project = name;
    connectEvents(name);
    chat.switchProject(name);
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

/* ---------------- M-V6 批1：导入 score json → 工程 ---------------- */

function applyImportCandidate(c) {
  if (!c) return;
  $('import-project-path').value = c.path;
  $('import-project-name').value = c.name_hint || '';
}

async function showImportRow() {
  const row = $('import-project-row');
  const sel = $('import-project-select');
  row.hidden = false;
  sel.innerHTML = '<option value="">（扫描中…）</option>';
  try {
    const r = await api.importCandidates();
    const cands = r.candidates || [];
    sel.innerHTML = '';
    if (!cands.length) {
      const opt = document.createElement('option');
      opt.value = '';
      opt.textContent = '（output/ 下没有可导入的 score json——可手动填路径）';
      sel.appendChild(opt);
      return;
    }
    for (const c of cands) {
      const opt = document.createElement('option');
      opt.value = c.path;
      opt.dataset.nameHint = c.name_hint || '';
      opt.textContent = c.path + '（' + c.title + ' · ' + c.tracks + '轨 · ' + c.notes + '音）';
      sel.appendChild(opt);
    }
    applyImportCandidate(cands[0]);
  } catch (e) { setError(e.message); }
}

async function doImportProject() {
  const source = $('import-project-path').value.trim() || $('import-project-select').value;
  const name = $('import-project-name').value.trim();
  if (!source) { setError('先选候选，或填 output/ 内的 json 路径'); return; }
  try {
    const r = await api.importProject(source, name || null);
    $('import-project-row').hidden = true;
    toast('已导入「' + r.source + '」→ 工程 ' + r.name);
    await loadProjects(true);
    await openProject(r.name);
  } catch (e) { setError(e.message); }
}

/* ---------------- 启动 ---------------- */

function boot() {
  initTheme();   // UI 批A：浅色默认（?theme= / localStorage 可覆盖）
  roll.init($('roll'));
  timeline.init($('ruler'), $('track-list'), $('segments-info'), $('meta-info'));
  segbar.init($('seg-rows'));
  dock.init($('dock'));
  initDiffBadge($('status-diff'));
  chat.init({
    logEl: $('chat-log'),
    inputEl: $('chat-input'),
    sendBtn: $('btn-send'),
    undoRoundBtn: $('btn-undo-round'),
    rollbackSel: $('rollback-select'),
    rollbackBtn: $('btn-rollback'),
    stopBtn: $('btn-stop-agent'),
    newSessionBtn: $('btn-new-session'),
    importBtn: $('btn-import-session'),
    importRow: $('import-row'),
    importSelect: $('import-select'),
    importConfirm: $('btn-import-confirm'),
    importCancel: $('btn-import-cancel'),
    chipsEl: $('chat-chips'),
    quickSel: $('quick-cmd'),
    quickRunBtn: $('btn-quick-run'),
    cmpSel: $('cmp-base'),
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

  /* 版本对比（议题 ④）：左槽选历史版本 → 试听；base_rev 由 chat.js 读取 */
  function refreshCmpBase() {
    const sel = $('cmp-base');
    sel.innerHTML = '';
    (store.gitLog || []).forEach((line, i) => {
      const opt = document.createElement('option');
      opt.value = line.split(' ')[0];
      opt.textContent = (i === 0 ? '★ HEAD · ' : '') + (line.length > 36 ? line.slice(0, 36) + '…' : line);
      sel.appendChild(opt);
    });
    $('cmp-head').textContent = '↔ ' + ((store.gitLog && store.gitLog[0] && store.gitLog[0].split(' ')[0]) || 'HEAD');
  }
  bus.on('state', refreshCmpBase);
  bus.on('state_updated', refreshCmpBase);

  $('btn-cmp-listen').addEventListener('click', () => {
    if (!store.project) { setError('先打开一个工程'); return; }
    const rev = $('cmp-base').value;
    if (!rev) { setError('先选一个版本（列表或 HEAD）'); return; }
    const a = new Audio();
    a.src = api.wavUrlRev(store.project, rev) + '&v=' + Date.now();   // 审计修 M-V2.3：wavUrlRev 已带 ?rev=，缓存击穿参数用 &
    a.play().then(() => {
      showToast('▶ 试听旧版 ' + rev.slice(0, 8) + '（缓存渲染，不动 HEAD）');
    }).catch((err) => setError('听旧版失败：' + err.message));
  });

  /* 工具栏 */
  $('project-select').addEventListener('change', (e) => openProject(e.target.value));
  $('btn-refresh-projects').addEventListener('click', () => loadProjects(true));
  $('btn-new-project').addEventListener('click', newProject);

  /* M-V6 批1：导入 score json → 工程 */
  $('btn-import-project').addEventListener('click', () => {
    const row = $('import-project-row');
    if (row.hidden) showImportRow(); else row.hidden = true;
  });
  $('import-project-select').addEventListener('change', (e) => {
    const opt = e.target.selectedOptions[0];
    if (!opt || !e.target.value) return;
    applyImportCandidate({ path: e.target.value, name_hint: opt.dataset.nameHint || '' });
  });
  $('btn-import-project-confirm').addEventListener('click', doImportProject);
  $('btn-import-project-cancel').addEventListener('click', () => { $('import-project-row').hidden = true; });
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

  /* 工程改名（score 标题，一个 commit） */
  $('btn-rename').addEventListener('click', async () => {
    if (!store.project) { setError('先打开一个工程'); return; }
    const cur = (store.score && store.score.title) || store.project;
    const title = prompt('新的工程显示名（score 标题，一个 commit，可撤销）：', cur);
    if (!title || !title.trim()) return;
    try {
      const r = await api.setTitle(store.project, title.trim());
      toast('已改名：' + r.title + ' @' + String(r.commit || '').slice(0, 7));
    } catch (e) { setError(e.message); }
  });

  /* 选区工具条（M-V3 雏形：点音符 → 命令层直编） */
  const selBar = $('sel-bar');
  function refreshSelBar() {
    const sel = store.selection;
    if (!sel || !sel.indices.length) { selBar.hidden = true; return; }
    selBar.hidden = false;
    $('sel-info').textContent = '选区：' + (sel.track + 1) + ' 轨 · ' + sel.indices.length + ' 音（Ctrl+点 加减选）';
  }
  bus.on('selection', refreshSelBar);
  bus.on('state', refreshSelBar);

  function selCommands(fn) {
    const sel = store.selection;
    const indices = (sel && sel.indices) || [];
    if (!indices.length) { setError('先点选音符'); return null; }
    return indices.map((idx) => fn(sel.track, idx));
  }

  /* M-V3：标注模式——选区操作挂起为 annotation（随下条对话发送，不发即时请求）
     同（音+动作）重复点击 = 在已挂起值上继续累加（♯+1 点两次 = +2）；删除去重 */
  const ANN_LABELS = { 'pitch-1': '♭−1', 'pitch+1': '♯+1', 'oct-1': '−8', 'oct+1': '+8', 'vel-': '力度−', 'vel+': '力度+', 'delete': '删除' };
  function pendingBase(index, action) {
    let v = null;
    for (const it of store.pendingAnnotations) {
      if (it.ann.index === index && it.ann.action === action) v = it.ann.value;
    }
    return v;
  }
  function makeAnnotations(op, sel) {
    const tr = store.score ? store.score.tracks[sel.track] : null;
    if (!tr) return null;
    const out = [];
    for (const i of sel.indices) {
      const n = tr.notes[i];
      if (!n) continue;
      if (op === 'pitch-1' || op === 'pitch+1' || op === 'oct-1' || op === 'oct+1') {
        const d = op === 'pitch-1' ? -1 : op === 'pitch+1' ? 1 : op === 'oct-1' ? -8 : 8;
        const base = pendingBase(i, 'pitch');
        const cur = (base === null) ? n.pitch_midi : base;
        out.push({ index: i, action: 'pitch', value: Math.max(0, Math.min(127, cur + d)) });
      } else if (op === 'vel-' || op === 'vel+') {
        const d = op === 'vel-' ? -0.1 : 0.1;
        const base = pendingBase(i, 'velocity');
        const cur = (base === null) ? n.velocity : base;
        out.push({ index: i, action: 'velocity', value: Math.round(Math.max(0, Math.min(1, cur + d)) * 100) / 100 });
      } else if (op === 'delete') {
        const dup = store.pendingAnnotations.some((it) => it.ann.index === i && it.ann.action === 'delete');
        if (!dup) out.push({ index: i, action: 'delete' });
      }
    }
    if (op === 'delete') out.sort((a, b) => b.index - a.index);   // 删除从后往前（index 不漂移）
    return out;
  }

  selBar.addEventListener('click', async (e) => {
    const btn = e.target.closest('button[data-op]');
    if (!btn) return;
    const op = btn.dataset.op;
    if (op === 'close') { refreshSelBar(); selBar.hidden = true; return; }
    if (!store.project) return;
    if ($('ann-mode').checked) {
      const anns = makeAnnotations(op, store.selection);
      if (!anns) { setError('先点选音符'); return; }
      if (!anns.length) { toast('已存在相同标注（未重复挂起）'); return; }
      const keys = new Set(anns.map((a) => a.index + '|' + a.action));
      const kept = store.pendingAnnotations.filter((it) => !keys.has(it.ann.index + '|' + it.ann.action));
      setAnnotations(kept.concat(anns.map((a) => ({ ann: a, label: ANN_LABELS[op] || op }))));
      toast('已挂起 ' + anns.length + ' 条标注（' + (ANN_LABELS[op] || op) + '），随下条对话发送');
      return;
    }
    let commands = null, label = '';
    if (op === 'delete') {
      commands = selCommands((t, i) => ({ op: 'remove', track: t, index: i }));
      commands = commands ? commands.slice().sort((a, b) => b.index - a.index) : null;  // 降序删，索引不漂移
      label = '删除选中音';
    } else if (op === 'pitch-1') { commands = selCommands((t, i) => ({ op: 'set_pitch', track: t, index: i, value: '@pitch-1' })); label = '-1 半音';
    } else if (op === 'pitch+1') { commands = selCommands((t, i) => ({ op: 'set_pitch', track: t, index: i, value: '@pitch+1' })); label = '+1 半音';
    } else if (op === 'oct-1') { commands = selCommands((t, i) => ({ op: 'set_pitch', track: t, index: i, value: '@pitch-8' })); label = '-8';
    } else if (op === 'oct+1') { commands = selCommands((t, i) => ({ op: 'set_pitch', track: t, index: i, value: '@pitch+8' })); label = '+8';
    } else if (op === 'vel-') { commands = selCommands((t, i) => ({ op: 'set_velocity', track: t, index: i, value: '@vel-0.1' })); label = '力度 -0.1';
    } else if (op === 'vel+') { commands = selCommands((t, i) => ({ op: 'set_velocity', track: t, index: i, value: '@vel+0.1' })); label = '力度 +0.1';
    }
    if (!commands) return;
    /* 占位 value 在服务端不可用——先换算成绝对值（读当前谱） */
    const tr = store.score ? store.score.tracks[commands[0].track] : null;
    if (!tr) return;
    commands.forEach((c) => {
      const n = tr.notes[c.index];
      if (!n) return;
      const v = String(c.value || '');
      if (v.startsWith('@pitch')) {
        c.value = Math.max(0, Math.min(127, n.pitch_midi + parseInt(v.slice(6), 10)));
      } else if (v.startsWith('@vel')) {
        c.value = Math.round(Math.max(0, Math.min(1, n.velocity + parseFloat(v.slice(4)))) * 100) / 100;
      }
    });
    try {
      const r = await api.postBatch(store.project, label, commands, '标注：' + label);
      if (r.applied) toast('已执行 ' + label + ' @' + String(r.commit || '').slice(0, 7));
      else setError('被拒：' + (r.errors || []).join('；'));
    } catch (err) { setError(err.message); }
  });

  /* M-V3：待发送标注队列（输入框上方） */
  function refreshAnnQueue() {
    const wrap = $('ann-queue');
    const list = $('ann-queue-list');
    const q = store.pendingAnnotations;
    if (!q.length) { wrap.hidden = true; list.innerHTML = ''; return; }
    wrap.hidden = false;
    const counts = {};
    for (const it of q) counts[it.label] = (counts[it.label] || 0) + 1;
    list.innerHTML = '';
    for (const [label, n] of Object.entries(counts)) {
      const chip = document.createElement('span');
      chip.className = 'ann-chip';
      chip.textContent = label + ' ×' + n;
      list.appendChild(chip);
    }
  }
  bus.on('annotations', refreshAnnQueue);
  bus.on('state', refreshAnnQueue);
  $('btn-ann-clear').addEventListener('click', () => { clearAnnotations(); toast('已清空待发送标注'); });

  /* M-V3：写谱吸附 */
  $('snap-sel').addEventListener('change', (e) => {
    setSnap(e.target.value);
    toast('吸附：' + e.target.selectedOptions[0].textContent);
  });

  /* UI 批A：速度 / 拍号（命令层 set_tempo；只在此处编辑——段轨与顶栏不重复） */
  async function postTempo(value, label) {
    if (!store.project) { setError('先打开一个工程'); return; }
    try {
      const r = await api.postBatch(store.project, label, [{ op: 'set_tempo', track: 0, value }], '参数：' + label);
      if (r.applied) toast('已更新 ' + label + ' @' + String(r.commit || '').slice(0, 7));
      else setError('被拒：' + (r.errors || []).join('；'));
    } catch (e) { setError(e.message); }
  }
  const tempoIn = $('tempo-input');
  const sigIn = $('sig-input');
  function syncMetro() {
    const sc = store.score;
    tempoIn.value = sc ? String(Math.round(sc.tempo * 10) / 10) : '';
    sigIn.value = sc ? (sc.time_signature || '4/4') : '';
  }
  tempoIn.addEventListener('change', () => {
    const t = Number(tempoIn.value);
    if (!t || t < 20 || t > 400) { syncMetro(); return; }
    if (store.score && Math.abs(t - store.score.tempo) < 1e-6) return;
    postTempo({ tempo: t }, '速度 ♩=' + t);
  });
  tempoIn.addEventListener('keydown', (e) => { if (e.key === 'Enter') { tempoIn.blur(); } });
  sigIn.addEventListener('change', () => {
    const v = (sigIn.value || '').trim();
    const cur = (store.score && store.score.time_signature) || '4/4';
    if (!v || v === cur) return;
    postTempo({ time_signature: v }, '拍号 ' + v);
  });
  sigIn.addEventListener('keydown', (e) => { if (e.key === 'Enter') { sigIn.blur(); } });
  bus.on('state', syncMetro);
  syncMetro();

  /* UI 批A：紧凑档抽屉（轨道 / 对话面板） */
  $('btn-drawer-tracks').addEventListener('click', () => {
    document.body.classList.toggle('drawer-tracks');
    document.body.classList.remove('drawer-chat');
  });
  $('btn-drawer-chat').addEventListener('click', () => {
    document.body.classList.toggle('drawer-chat');
    document.body.classList.remove('drawer-tracks');
  });

  bus.on('state', () => { refreshStatusBar(); refreshToolbar(); });
  bus.on('error', showError);
  bus.on('toast', showToast);

  loadProjects(false);
}

boot();
