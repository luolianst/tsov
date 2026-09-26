/* staging.js —— 「AI」页签：审查中心（M-V8 E4 段1 · Q10.5）
   - 工程上下文：agents.md 双层（工程 agents.md + 全局 agents-user.md）——「刷新」= 后端确定性重生成
   - 暂存区：AI/链产物默认先进暂存，人批才落地——列表 / 试听 / 采纳 / 丢弃
   - 状态真值在后端（tsov/staging.py + 工程 .tsov-state.json）；前端镜像 store.staging
   - REST：list / adopt / discard / artifact + agents get/sync（ADR-0017：与 agent 同一动作路径）
   - __tsovState().staging 暴露给 CDP 断言 */

import { bus } from './events.js';
import { api } from './api.js';
import { store, setError, setStagingState } from './state.js';

let pane = null;
let busy = false;
let audio = null;          // 单例试听（切页/再点即停）
let playing = null;        // {itemId, file}
let lastProject = null;
let chainWasRunning = false;

const STATE_TEXT = { pending: '待处置', adopted: '已采纳', discarded: '已丢弃' };

function el(tag, cls, text) {
  const d = document.createElement(tag);
  if (cls) d.className = cls;
  if (text != null) d.textContent = text;
  return d;
}

/* 链 run_ts：20260925-223655 → 2026-09-25 22:36 */
function fmtTs(ts) {
  const m = /^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})$/.exec(String(ts || ''));
  return m ? `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}` : String(ts || '');
}

function fmtUnix(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/* ---------------- 试听 ---------------- */

function stopAudio() {
  if (audio) { try { audio.pause(); } catch (e) { /* ignore */ } }
  audio = null;
  playing = null;
  try { window.__tsovStagingAudio = null; } catch (e) { /* ignore */ }
}

function togglePlay(itemId, file) {
  const wasThis = playing && playing.itemId === itemId && playing.file === file;
  stopAudio();
  if (wasThis) { renderStaging(); return; }   // 再点 = 停
  try {
    audio = new Audio(api.stagingArtifactUrl(store.project, itemId, file));
    try { window.__tsovStagingAudio = audio; } catch (e) { /* ignore */ }   // CDP 试听断言钩子
    audio.addEventListener('ended', () => { stopAudio(); renderStaging(); });
    audio.play().catch(() => { /* 浏览器拒绝自动播放等：静默 */ });
    playing = { itemId, file };
  } catch (e) { /* ignore */ }
  renderStaging();
}

function artifactBtn(item, file) {
  const isPlaying = !!(playing && playing.itemId === item.id && playing.file === file);
  const label = file.startsWith('01') ? '降噪' : file.startsWith('02') ? '响度' : file;
  const b = el('button', 'stg-btn stg-play' + (isPlaying ? ' on' : ''),
               (isPlaying ? '⏸ ' : '▶ ') + '试听·' + label);
  b.title = file;
  b.dataset.file = file;
  b.addEventListener('click', () => togglePlay(item.id, file));
  return b;
}

/* ---------------- 动作 ---------------- */

async function doAdopt(item, btn) {
  if (busy || !store.project) return;
  busy = true; btn.disabled = true; btn.textContent = '采纳中…';
  try {
    const r = await api.stagingAdopt(store.project, item.id);
    bus.dispatch('toast', r.ok
      ? `已采纳「${item.title}」` + (r.names ? `：${r.names.join(' / ')} 落轨` : '')
      : '采纳未生效（无改动）');
    stopAudio();
    await refreshStaging();
  } catch (e) {
    setError(String((e && e.message) || e));
  } finally {
    busy = false;
    renderStaging();
  }
}

async function doDiscard(item, btn) {
  if (busy || !store.project) return;
  busy = true; btn.disabled = true; btn.textContent = '丢弃中…';
  try {
    await api.stagingDiscard(store.project, item.id);
    bus.dispatch('toast', `已丢弃「${item.title}」（产物保留为历史，工程未动）`);
    stopAudio();
    await refreshStaging();
  } catch (e) {
    setError(String((e && e.message) || e));
  } finally {
    busy = false;
    renderStaging();
  }
}

async function doSyncAgents(btn) {
  if (!store.project) return;
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = '同步中…';
  try {
    const r = await api.agentsSync(store.project);
    setStagingState({ agents: await api.agentsGet(store.project) });
    bus.dispatch('toast', '工程上下文已刷新（agents.md ' + (r.project && r.project.created ? '新建' : '更新') + '）');
  } catch (e) {
    setError(String((e && e.message) || e));
  } finally {
    btn.disabled = false;
    btn.textContent = old;
    renderStaging();
  }
}

/* ---------------- 渲染 ---------------- */

function renderAgentsBlock() {
  const wrap = el('div', 'stg-ctx');
  const a = store.staging.agents;
  wrap.appendChild(el('span', 'stg-title-main', '工程上下文'));
  wrap.appendChild(el('span', 'stg-ctx-item',
    'agents.md：' + (a && a.project_md ? '✓ 已生成' : '— 未生成')));
  wrap.appendChild(el('span', 'stg-ctx-item',
    'agents-user.md（全局）：' + (a && a.user_md ? '✓ 已建档' : '— 未建档')));
  const btn = el('button', 'stg-btn stg-sync', a && a.project_md ? '↻ 刷新状态摘要' : '＋ 生成 agents.md');
  btn.addEventListener('click', () => doSyncAgents(btn));
  wrap.appendChild(btn);
  return wrap;
}

function itemCard(it) {
  const card = el('div', 'stg-card stg-' + it.state);
  card.dataset.itemId = it.id;   // CDP 验收定向选择器（E4 段1）
  const hd = el('div', 'stg-hd');
  hd.appendChild(el('span', 'stg-badge stg-badge-' + it.state, STATE_TEXT[it.state] || it.state));
  hd.appendChild(el('span', 'stg-title', it.title));
  card.appendChild(hd);

  const bits = ['链运行 ' + fmtTs(it.meta && it.meta.run_ts)];
  const raw = it.meta && it.meta.notes_raw, proc = it.meta && it.meta.notes_processed;
  if (raw != null) bits.push('原始 ' + raw + ' 音');
  if (proc != null) bits.push('处理 ' + proc + ' 音');
  if (!it.ready) bits.push('未跑完（缺 ' + ((it.meta && it.meta.missing) || []).join('、') + '）');
  if (it.handled_at) bits.push((it.state === 'adopted' ? '采纳于 ' : '丢弃于 ') + fmtUnix(it.handled_at));
  card.appendChild(el('div', 'stg-sub', bits.join(' · ')));

  const acts = el('div', 'stg-acts');
  for (const f of ((it.meta && it.meta.audio) || [])) acts.appendChild(artifactBtn(it, f));
  if (it.state === 'pending') {
    const ok = el('button', 'stg-btn stg-adopt', '⇥ 采纳进工程');
    ok.disabled = !it.ready;
    if (!it.ready) ok.title = '链尚未跑完（缺 ' + ((it.meta && it.meta.missing) || []).join('、') + '）';
    ok.addEventListener('click', () => doAdopt(it, ok));
    const no = el('button', 'stg-btn stg-discard', '✕ 丢弃');
    no.title = '仅记处置：工程不动、产物保留';
    no.addEventListener('click', () => doDiscard(it, no));
    acts.appendChild(ok);
    acts.appendChild(no);
  }
  card.appendChild(acts);
  return card;
}

export function renderStaging() {
  if (!pane) return;
  pane.innerHTML = '';
  const wrap = el('div', 'stg-wrap');

  const head = el('div', 'stg-head');
  head.appendChild(el('span', 'stg-title-main', 'AI 工作台'));
  head.appendChild(el('span', 'stg-sub', 'AI/链产物默认先进暂存，人批才落地'));
  const rf = el('button', 'stg-btn stg-refresh', '↻ 刷新');
  rf.addEventListener('click', () => { refreshStaging(); refreshAgents(); });
  head.appendChild(rf);
  wrap.appendChild(head);
  wrap.appendChild(renderAgentsBlock());

  const lh = el('div', 'stg-head');
  const c = store.staging.counts || {};
  lh.appendChild(el('span', 'stg-title-main', '暂存区'));
  lh.appendChild(el('span', 'stg-sub',
    `待处置 ${c.pending || 0} · 已采纳 ${c.adopted || 0} · 已丢弃 ${c.discarded || 0}`));
  wrap.appendChild(lh);

  const list = el('div', 'stg-list');
  if (!store.project) list.appendChild(el('div', 'stg-empty', '未打开工程'));
  else if (!store.staging.loaded) list.appendChild(el('div', 'stg-empty', '加载中…'));
  else if (!store.staging.items.length) {
    list.appendChild(el('div', 'stg-empty',
      '暂无暂存条目——在「链」页签跑一次哼唱快车道，产物会先进这里等你处置'));
  } else for (const it of store.staging.items) list.appendChild(itemCard(it));
  wrap.appendChild(list);

  pane.appendChild(wrap);
}

/* ---------------- 刷新 ---------------- */

export async function refreshStaging() {
  if (!store.project) return;
  try {
    const r = await api.stagingList(store.project);
    setStagingState({ items: r.items || [], counts: r.counts || {}, loaded: true });
  } catch (e) {
    setStagingState({ loaded: true });
  }
}

export async function refreshAgents() {
  if (!store.project) return;
  try {
    setStagingState({ agents: await api.agentsGet(store.project) });
  } catch (e) { /* 端点缺失/网络异常：状态区显示未生成 */ }
}

export function enterStaging() {
  renderStaging();
  if (store.project) {
    refreshStaging();
    refreshAgents();
  }
}

export function initStaging(paneEl) {
  pane = paneEl;
  bus.on('staging', renderStaging);
  bus.on('state', () => {
    if (lastProject === store.project) return;
    lastProject = store.project;   // 工程切换：清镜像（页签可见时重拉）
    stopAudio();
    setStagingState({ items: [], counts: { pending: 0, adopted: 0, discarded: 0 },
                      agents: null, loaded: false });
    if (paneEl.classList.contains('active')) enterStaging();
  });
  bus.on('chain', () => {   // 链跑完 → 新产物自动进列表
    const running = !!store.chain.running;
    if (chainWasRunning && !running && store.project) refreshStaging();
    chainWasRunning = running;
  });
}
