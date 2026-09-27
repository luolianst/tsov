/* steps.js —— 步骤视图（对话产物流 C 件：右栏「步骤」tab 的合流 feed）
   - 双 tab：对话｜步骤 ●N（角标=暂存待处置数）——切换记忆 localStorage（默认「对话」）
   - 数据源（服务端真值，UI 不另造留存）：
     · journal 轮聚合 GET /agent-actions（rounds + entries；窗口后端已限 user 20 / agent 5 轮）
     · 暂存产物 GET /staging（pending / adopted / discarded）
   - 合流布局（对齐 v2 稿）：时间序、最新在底；顶部「更早 N 步」折叠区（已处置产物）
   - 置灰规则：未处置永不置灰；已处置折叠；stale 轮置灰
   - 状态暴露：__tsovState().steps.feed（计数 + DOM 实测数，CDP 断言用）
   - 视觉复用 chat.js 的 ac- 卡族样式（卡片口径与对话内一致） */

import { bus } from './events.js';
import { api } from './api.js';
import { store, setError } from './state.js';
import { itemCard, refreshStaging } from './staging.js';   // D 件：配器/链/已处置 → 复用完整操作区；E 件：数据订阅保活

const VIEW_KEY = 'tsov-rail-view';            // 切换态记忆（C 件 §5）
const STATE_TEXT = { pending: '待处置', adopted: '已采纳', discarded: '已丢弃' };
const KIND_TEXT = { chain: '处理链', tune: '调参', arrange: '配器' };
// 口径与 chat.js 一致（B 件；重构可抽公共模块）
const STAT_LABELS = { notes: '修改音符', track_add: '轨道', track_del: '轨道', track_edit: '轨名',
                      fx: '效果器', param: '参数', other: '动作' };
const TUNE_KIND_TEXT = { level: '电平', pan: '声像', effect: '效果链' };

let panelEl = null;
let viewEl = null;
let feedEl = null;
let badgeEl = null;
let cache = { rounds: [], entries: [], items: [] };
let busy = false;
let timer = 0;

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

function statText(k, v) {
  const name = STAT_LABELS[k] || k;
  if (k === 'track_add') return name + ' +' + v;
  if (k === 'track_del') return name + ' \u2212' + v;
  if (k === 'fx') return name + ' +' + v;
  return name + ' \u00d7' + v;
}

/** 条目时间戳（兼容 unix 秒/毫秒 与 '20260927-225129' 批号；返回秒） */
function parseWhen(v) {
  if (v == null) return 0;
  const s = String(v);
  const m = s.match(/^(\d{8})-(\d{6})$/);
  if (m) {
    const d = m[1], t = m[2];
    const ms = Date.parse(`${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6, 8)}` +
                          `T${t.slice(0, 2)}:${t.slice(2, 4)}:${t.slice(4, 6)}`);
    return Number.isFinite(ms) ? ms / 1000 : 0;
  }
  const n = Number(v);
  if (!Number.isFinite(n)) return 0;
  return n > 1e12 ? n / 1000 : n;   // 毫秒 → 秒
}

function itemTs(it) {
  const meta = it.meta || {};
  const raw = it.ts != null ? it.ts : (meta.run_ts != null ? meta.run_ts : meta.batch_ts);
  return parseWhen(raw);
}

/* --------------------------------- 渲染 --------------------------------- */

function renderRoundCard(rd, entries) {
  const root = el('div', 'ac-group step-round' + ((rd.n_stale || 0) > 0 ? ' stale' : ''));   // E 件：后端字段=n_stale
  root.dataset.round = String(rd.round || '');

  const seqs = (rd.seqs || []).filter((s) => s != null && s > 0);
  const n = rd.n || seqs.length || entries.length;

  // 明细容器（默认折叠）
  const details = el('div', 'step-details');
  details.hidden = true;
  if (entries.length) for (const e of entries) details.appendChild(entryRow(e));
  else details.appendChild(el('div', 'ac-readrow', '（该轮无聚合条目）'));

  // 头（点击展开/折叠）
  const head = el('button', 'ac-group-head');
  let open = false;
  const paint = () => { head.textContent = `本轮变更 · ${n} 个动作 ` + (open ? '\u25be' : '\u25b8'); };
  paint();
  head.addEventListener('click', () => { open = !open; details.hidden = !open; paint(); });

  // 统计 chips
  const stats = el('div', 'ac-stats');
  for (const [k, v] of Object.entries(rd.stats || {})) if (v) stats.appendChild(el('span', 'ac-stat', statText(k, v)));

  if ((rd.n_stale || 0) > 0) root.appendChild(el('div', 'step-stale-flag', '（本轮已失效/撤销）'));

  // 底部动作：撤销本轮（后端 action_undone 事件全链置灰后本视图刷新）
  const acts = el('div', 'ac-round-actions');
  const undo = el('button', 'ac-btn ac-undo', '撤销本轮');
  undo.title = '撤销本轮全部动作（其后动作将失效）';
  if (!seqs.length || (rd.n_stale || 0) > 0) undo.disabled = true;
  undo.addEventListener('click', async () => {
    if (!store.project || !seqs.length) return;
    undo.disabled = true;
    try {
      await api.actionUndo(store.project, Math.min.apply(null, seqs));
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => refreshSteps(), 500);
    } catch (e) {
      setError('撤销本轮失败：' + e.message);
      undo.disabled = false;
    }
  });
  acts.appendChild(undo);

  root.appendChild(head);
  root.appendChild(stats);
  root.appendChild(details);
  root.appendChild(acts);
  return root;
}

function entryRow(e) {
  const row = el('div', 'actcard step-entry' + (e.stale ? ' stale' : ''));
  row.dataset.seq = String(e.seq != null ? e.seq : '');
  const hd = el('div', 'ac-head');
  hd.appendChild(el('span', 'ac-label', '\u2699 ' + (e.label || e.tool || '工具')));
  if (e.args) hd.appendChild(el('span', 'ac-sum', e.args));
  row.appendChild(hd);
  const imp = e.impact && e.impact.text;
  if (imp) row.appendChild(el('div', 'ac-impact', '影响：' + imp));
  return row;
}

function renderItemRow(it) {
  const state = it.state || 'pending';
  const box = el('div', 'step-itembox');
  const row = el('div', 'step-item stg-' + state);
  row.dataset.itemId = it.id || '';
  row.appendChild(el('span', 'stg-badge stg-badge-' + state, STATE_TEXT[state] || state));
  row.appendChild(el('span', 'stg-title step-item-title', it.title || it.id || ''));
  const sub = [KIND_TEXT[it.producer] || it.producer || '', it.ready === false ? '未就绪' : '']
    .filter(Boolean).join(' · ');
  row.appendChild(el('span', 'step-item-sub', sub));
  const t = itemTs(it);
  if (t) row.appendChild(el('span', 'step-item-ts', new Date(t * 1000).toTimeString().slice(0, 8)));
  const arrow = el('span', 'step-item-arrow', '\u25b8');
  row.appendChild(arrow);

  /* D 件：点开 = 操作区（懒渲染一次） */
  const body = el('div', 'step-itembody');
  body.hidden = true;
  let loaded = false;
  row.addEventListener('click', async () => {
    body.hidden = !body.hidden;
    arrow.textContent = body.hidden ? '\u25b8' : '\u25be';
    if (body.hidden || loaded) return;
    loaded = true;
    const tip = el('div', 'stg-sub', '加载中…');
    body.appendChild(tip);
    try {
      const node = await buildItemBody(it);
      body.innerHTML = '';
      body.appendChild(node);
    } catch (e) {
      body.innerHTML = '';
      body.appendChild(el('div', 'stg-sub', '展开失败：' + ((e && e.message) || e)));
    }
  });
  box.appendChild(row);
  box.appendChild(body);
  return box;
}

/* ---------------- D 件：产物卡操作区 ---------------- */

/** 待处置调参 = 建议勾选壳（REST：tune get/preview/apply）；其余复用 itemCard */
async function buildItemBody(it) {
  if (it.producer === 'tune' && it.state === 'pending') return buildTuneBody(it);
  return itemCard(it);
}

let chipAudio = null;
function playUrl(url) {
  bus.dispatch('audio_started', { owner: 'steps' });
  if (chipAudio) { chipAudio.pause(); chipAudio = null; }
  chipAudio = new Audio(url);
  chipAudio.play().catch(() => {});
}

async function buildTuneBody(it) {
  const ts = (it.meta && it.meta.batch_ts) || '';
  if (!ts) return itemCard(it);
  const r = await api.tuneGet(store.project, ts);
  const b = (r && r.batch) || r || {};
  const sels = b.suggestions || [];
  if (!sels.length) return itemCard(it);

  const wrap = el('div', 'tune-wrap');
  const selected = {};
  for (const s of sels) selected[s.id] = true;   // 默认全选（同 AI 页签 defaultSelected 口径）

  const list = el('div', 'tune-list');
  for (const s of sels) {
    const card = el('div', 'tune-card sel tune-k-' + s.kind);
    card.dataset.suggId = s.id;
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = true;
    cb.dataset.role = 'steps-tune-check';
    cb.addEventListener('change', () => {
      selected[s.id] = cb.checked;
      card.classList.toggle('sel', cb.checked);
    });
    card.appendChild(cb);
    const cbody = el('div', 'tune-card-body');
    const hd = el('div', 'stg-hd');
    hd.appendChild(el('span', 'tune-kind', TUNE_KIND_TEXT[s.kind] || s.kind || ''));
    hd.appendChild(el('span', 'stg-title', s.title || s.id));
    cbody.appendChild(hd);
    if (s.reason) cbody.appendChild(el('div', 'tune-reason', s.reason));
    const acts = el('div', 'stg-acts');
    const pv = el('button', 'stg-btn stg-play', '▶ 试听');
    pv.dataset.role = 'steps-tune-preview';
    pv.title = '在副本上试跑渲染（工程零改动）';
    pv.addEventListener('click', async (ev) => {
      ev.stopPropagation();
      pv.disabled = true;
      pv.textContent = '渲染中…';
      try {
        const rr = await api.tunePreview(store.project, ts, s.id);
        playUrl(rr.url);
      } catch (e) { setError('试听失败：' + ((e && e.message) || e)); }
      finally { pv.disabled = false; pv.textContent = '▶ 试听'; }
    });
    acts.appendChild(pv);
    cbody.appendChild(acts);
    card.appendChild(cbody);
    list.appendChild(card);
  }
  wrap.appendChild(list);

  const foot = el('div', 'stg-acts');
  const all = el('button', 'stg-btn', '全选');
  all.addEventListener('click', () => {
    for (const s of sels) selected[s.id] = true;
    for (const cb of list.querySelectorAll('input[type=checkbox]')) cb.checked = true;
    for (const c of list.querySelectorAll('.tune-card')) c.classList.add('sel');
  });
  const none = el('button', 'stg-btn', '全不选');
  none.addEventListener('click', () => {
    for (const s of sels) selected[s.id] = false;
    for (const cb of list.querySelectorAll('input[type=checkbox]')) cb.checked = false;
    for (const c of list.querySelectorAll('.tune-card')) c.classList.remove('sel');
  });
  const okBtn = el('button', 'stg-btn stg-adopt', '⇥ 应用选中');
  okBtn.dataset.role = 'steps-tune-apply';
  okBtn.title = '命令层事务 + 自动对拍；可整批撤销';
  okBtn.addEventListener('click', async () => {
    const ids = Object.keys(selected).filter((k) => selected[k]);
    if (!ids.length) { setError('未勾选任何建议'); return; }
    okBtn.disabled = true;
    okBtn.textContent = '应用中…';
    try {
      const rr = await api.tuneApply(store.project, { batch_ts: ts, ids });
      bus.dispatch('toast', '已应用 ' + ((rr.applied || []).length || ids.length) + ' 条建议（对拍小结；可整批撤销）');
      setTimeout(() => refreshSteps(), 500);
    } catch (e) {
      setError('应用失败：' + ((e && e.message) || e));
      okBtn.disabled = false;
      okBtn.textContent = '⇥ 应用选中';
    }
  });
  foot.appendChild(all);
  foot.appendChild(none);
  foot.appendChild(okBtn);
  wrap.appendChild(foot);
  return wrap;
}

function renderFeed() {
  if (!feedEl) return;
  feedEl.innerHTML = '';
  const { rounds, entries, items } = cache;
  const byRound = {};
  for (const e of entries) {
    const k = String(e.round || '');
    (byRound[k] = byRound[k] || []).push(e);
  }

  const handled = items.filter((i) => i.state !== 'pending');
  const pending = items.filter((i) => i.state === 'pending');

  // 顶部折叠区：更早 N 步（已处置产物）
  if (handled.length) {
    const box = el('div', 'step-earlier');
    const head = el('button', 'step-earlier-head', `更早 ${handled.length} 步 \u25b8`);
    const body = el('div', 'step-earlier-body');
    body.hidden = true;
    head.addEventListener('click', () => {
      body.hidden = !body.hidden;
      head.textContent = `更早 ${handled.length} 步 ` + (body.hidden ? '\u25b8' : '\u25be');
    });
    for (const it of handled) body.appendChild(renderItemRow(it));
    box.appendChild(head);
    box.appendChild(body);
    feedEl.appendChild(box);
  }

  // feed：轮卡 + 待处置产物 → 时间序（最新在底）
  const flow = [];
  for (const rd of rounds) {
    flow.push({ ts: Number(rd.ts_first) || 0, node: () => renderRoundCard(rd, byRound[String(rd.round || '')] || []) });
  }
  for (const it of pending) flow.push({ ts: itemTs(it), node: () => renderItemRow(it) });
  flow.sort((a, b) => a.ts - b.ts);
  for (const f of flow) feedEl.appendChild(f.node());

  if (!flow.length && !handled.length) {
    feedEl.appendChild(el('div', 'step-empty', '暂无步骤——发一条指令，或从快捷命令触发产物。'));
  }
  syncSnapshot();
}

/* --------------------------------- 数据 --------------------------------- */

export async function refreshSteps() {
  if (!viewEl) return;
  if (!store.project) {
    cache = { rounds: [], entries: [], items: [] };
    updateBadge(null);
    renderFeed();
    return;
  }
  if (busy) return;
  busy = true;
  try {
    const [acts, stg] = await Promise.all([
      api.actions(store.project).catch(() => null),
      api.stagingList(store.project).catch(() => null),
    ]);
    cache = {
      rounds: (acts && acts.rounds) || [],
      entries: (acts && acts.entries) || [],
      items: (stg && stg.items) || [],
    };
    updateBadge(stg && stg.counts);
    renderFeed();
  } catch (e) {
    setError('步骤视图加载失败：' + e.message);
  } finally {
    busy = false;
  }
}

async function refreshBadge() {
  if (!badgeEl || !store.project) return;
  try {
    const stg = await api.stagingList(store.project);
    updateBadge(stg && stg.counts);
  } catch (e) { /* 静默：角标非关键路径 */ }
}

function updateBadge(counts) {
  if (!badgeEl) return;
  const n = (counts && Number(counts.pending)) || 0;
  badgeEl.hidden = !n;
  badgeEl.textContent = String(n);
}

function scheduleRefresh() {
  if (timer) clearTimeout(timer);
  timer = setTimeout(() => {
    timer = 0;
    if (isStepsVisible()) refreshSteps();
    else refreshBadge();
  }, 200);
}

export function isStepsVisible() {
  return !!(panelEl && panelEl.classList.contains('view-steps'));
}

export function switchView(v) {
  if (!panelEl) return;
  const steps = v === 'steps';
  panelEl.classList.toggle('view-steps', steps);
  if (viewEl) viewEl.hidden = false;   // 首切揭幕（初始 hidden 防首屏闪烁）
  document.querySelectorAll('#sp-tabs .sp-tab').forEach((b) => {
    b.classList.toggle('active', b.dataset.view === (steps ? 'steps' : 'chat'));
  });
  try { localStorage.setItem(VIEW_KEY, steps ? 'steps' : 'chat'); } catch (e) { /* 隐私模式忽略 */ }
  if (steps) refreshSteps();
  else refreshBadge();
}

function syncSnapshot() {
  store.steps = store.steps || { rounds: [], count: 0 };
  store.steps.feed = {
    rounds: cache.rounds.length,
    items: cache.items.length,
    pending: cache.items.filter((i) => i.state === 'pending').length,
    domRounds: viewEl ? viewEl.querySelectorAll('.step-round').length : 0,
    domItems: viewEl ? viewEl.querySelectorAll('.step-item').length : 0,
  };
}

export function initStepsPanel() {
  panelEl = document.getElementById('chat-panel');
  viewEl = document.getElementById('steps-view');
  badgeEl = document.getElementById('sp-badge');
  if (!panelEl || !viewEl) return;
  feedEl = el('div', 'steps-feed');
  viewEl.appendChild(feedEl);
  document.querySelectorAll('#sp-tabs .sp-tab').forEach((b) => {
    b.addEventListener('click', () => switchView(b.dataset.view));
  });
  bus.on('agent_tool', scheduleRefresh);
  bus.on('agent_answer', scheduleRefresh);
  bus.on('action_undone', scheduleRefresh);   // E 件：撤销本轮 → 全链置灰刷新
  bus.on('staging', scheduleRefresh);
  bus.on('state', scheduleRefresh);
  /* E 件：接管原「AI」页签的数据订阅（initStaging 裁撤后的语义迁移） */
  bus.on('chain', () => { if (store.project) { scheduleRefresh(); refreshStaging(); } });        // 链跑完 → 新产物
  bus.on('tune_updated', () => { if (store.project) { scheduleRefresh(); refreshStaging(); } });  // 他通道（agent/REST）调参
  bus.on('refresh_staging', () => { if (store.project) refreshStaging(); });                      // 配器区等刷新请求
  bus.on('state', () => { if (store.project) refreshStaging(); });                                // store.staging 快照保活
  bus.on('audio_started', (ev) => {   // 试听互斥（原 initStaging 语义）
    if (((ev && ev.owner) || '') !== 'steps' && chipAudio) { try { chipAudio.pause(); } catch (e) { /* */ } chipAudio = null; }
  });
  let saved = 'chat';
  try { saved = localStorage.getItem(VIEW_KEY) === 'steps' ? 'steps' : 'chat'; } catch (e) { /* */ }
  switchView(saved);
}
