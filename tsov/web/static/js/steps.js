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

const VIEW_KEY = 'tsov-rail-view';            // 切换态记忆（C 件 §5）
const STATE_TEXT = { pending: '待处置', adopted: '已采纳', discarded: '已丢弃' };
const KIND_TEXT = { chain: '处理链', tune: '调参', arrange: '配器' };
// 口径与 chat.js 一致（B 件；重构可抽公共模块）
const STAT_LABELS = { notes: '修改音符', track_add: '轨道', track_del: '轨道', track_edit: '轨名',
                      fx: '效果器', param: '参数', other: '动作' };

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
  const root = el('div', 'ac-group step-round' + (rd.any_stale ? ' stale' : ''));
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

  if (rd.any_stale) root.appendChild(el('div', 'step-stale-flag', '（本轮已失效/撤销）'));

  // 底部动作：撤销本轮（后端 action_undone 事件全链置灰后本视图刷新）
  const acts = el('div', 'ac-round-actions');
  const undo = el('button', 'ac-btn ac-undo', '撤销本轮');
  undo.title = '撤销本轮全部动作（其后动作将失效）';
  if (!seqs.length || rd.any_stale) undo.disabled = true;
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
  const row = el('div', 'step-item stg-' + state);
  row.dataset.itemId = it.id || '';
  row.appendChild(el('span', 'stg-badge stg-badge-' + state, STATE_TEXT[state] || state));
  row.appendChild(el('span', 'stg-title step-item-title', it.title || it.id || ''));
  const sub = [KIND_TEXT[it.producer] || it.producer || '', it.ready === false ? '未就绪' : '']
    .filter(Boolean).join(' · ');
  row.appendChild(el('span', 'step-item-sub', sub));
  const t = itemTs(it);
  if (t) row.appendChild(el('span', 'step-item-ts', new Date(t * 1000).toTimeString().slice(0, 8)));
  return row;
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
  bus.on('staging', scheduleRefresh);
  bus.on('state', scheduleRefresh);
  let saved = 'chat';
  try { saved = localStorage.getItem(VIEW_KEY) === 'steps' ? 'steps' : 'chat'; } catch (e) { /* */ }
  switchView(saved);
}
