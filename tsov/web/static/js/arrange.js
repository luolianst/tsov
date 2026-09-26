/* arrange.js —— 「AI」页签·配器区（M-V8 E4 段3 · Q14 三段一库）
   - 流程：选风格包 + 密度档 → 生成配器（LLM 只选 ID，坐标全机械展开）→ 试听（候选混音，工程零改动）
     → 进工程（命令层事务，幂等）或丢弃 → 不满意可带意见重生成
   - 状态真值在后端（tsov/arrange/store.py）；前端镜像 store.arrange（state.js）
   - REST：arrange（packs/generate/list/get/preview/apply/discard/file）
   - 试听钩子：window.__tsovArrangeAudio（CDP 断言用）；跨模块音频互斥走 bus 'audio_started' */

import { bus } from './events.js';
import { api } from './api.js';
import { store, setError, setArrangeState } from './state.js';

let arrAudio = null;      // 配器试听（独立于暂存区/调参试听）
let arrPlaying = null;    // {url}
let lastProjectA = null;

const ARR_STATE_TEXT = { pending: '待处置', adopted: '已进工程', discarded: '已丢弃' };
/* 强度档（G9：收/标准/满）= 模式库 vel 缩放（soft 0.9 / standard 1.0 / full 1.1） */
const STRENGTH_TEXT = { soft: '收', standard: '标准', full: '满' };
const STRENGTHS = [
  ['soft', '强度·收'],
  ['standard', '强度·标准'],
  ['full', '强度·满'],
];
const SR_TEXT = { default: '默认策略', mixed: 'LLM+默认', llm: 'LLM 决策' };

function el(tag, cls, text) {
  const d = document.createElement(tag);
  if (cls) d.className = cls;
  if (text != null) d.textContent = text;
  return d;
}

function fmtTs(ts) {
  const m = /^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})$/.exec(String(ts || ''));
  return m ? `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}` : String(ts || '');
}

/* ---------------- 试听 ---------------- */

function stopArrAudio() {
  if (arrAudio) { try { arrAudio.pause(); } catch (e) { /* ignore */ } }
  arrAudio = null;
  arrPlaying = null;
  try { window.__tsovArrangeAudio = null; } catch (e) { /* ignore */ }
}

function playArr(url) {
  const wasSame = !!(arrPlaying && arrPlaying.url === url);
  stopArrAudio();
  if (wasSame) { renderStagingHost(); return; }   // 再点 = 停
  try {
    arrAudio = new Audio(url);
    try { window.__tsovArrangeAudio = arrAudio; } catch (e) { /* ignore */ }   // CDP 试听钩子
    arrAudio.addEventListener('ended', () => { stopArrAudio(); renderStagingHost(); });
    arrAudio.play().catch(() => { /* ignore */ });
    arrPlaying = { url };
    bus.dispatch('audio_started', { owner: 'arrange' });   // 音频互斥（停别家）
  } catch (e) { /* ignore */ }
  renderStagingHost();
}

function renderStagingHost() { bus.dispatch('arrange', {}); }   // 重渲染交还宿主（staging.js）

function fileBtn(ts, file, label) {
  const url = api.arrangeFileUrl(store.project, ts, file);
  const on = !!(arrPlaying && arrPlaying.url === url);
  const b = el('button', 'stg-btn stg-play' + (on ? ' on' : ''), (on ? '⏸ ' : '▶ ') + label);
  b.dataset.role = 'arrange-file';
  b.dataset.file = file;
  b.addEventListener('click', () => playArr(url));
  return b;
}

/* ---------------- 动作 ---------------- */

export async function doArrangeGenerate(btn, context) {
  if (!store.project) return;
  const a = store.arrange;
  if (!a.pack) { setError('先选一个风格包（有模式库的包才可配器）'); return; }
  const pack = a.pack, strength = a.strength || 'standard';   // 起手快照：防异步状态中途被改
  btn.disabled = true; const old = btn.textContent; btn.textContent = '生成中…（< 1 分钟）';
  setArrangeState({ busy: 'generate' });
  try {
    const r = await api.arrangeGenerate(store.project, {
      pack, strength, context: context || '',
    });
    const b = r.batch;
    setArrangeState({ batch: b, loaded: true });
    bus.dispatch('toast',
      `配器批次 ${fmtTs(b.batch_ts)}：${b.stats.tracks} 轨 / ${b.stats.notes} 音`
      + `（${SR_TEXT[b.stats.source] || b.stats.source}，已进暂存区，人批才落地）`);
    bus.dispatch('refresh_staging', {});
  } catch (e) { setError(String((e && e.message) || e)); }
  finally {
    setArrangeState({ busy: null });
    btn.disabled = false; btn.textContent = old;
  }
}

export async function doArrangePreview(ts, btn) {
  if (!store.project) return;
  const url = api.arrangeFileUrl(store.project, ts, 'preview-mix.wav');
  if (arrPlaying && arrPlaying.url === url) {   // 已渲染过：再点 = 停
    stopArrAudio();
    bus.dispatch('toast', '已停止试听');
    renderStagingHost();
    return;
  }
  if (btn) { btn.disabled = true; const old = btn.textContent; btn.textContent = '渲染中…'; }
  setArrangeState({ busy: 'preview' });
  try {
    const r = await api.arrangePreview(store.project, ts);
    stopArrAudio();
    playArr(r.url);
    bus.dispatch('toast', '试听预览：配器 + 现有工程（工程未动）');
  } catch (e) { setError(String((e && e.message) || e)); }
  finally {
    setArrangeState({ busy: null });
    if (btn) { btn.disabled = false; btn.textContent = old; }
  }
}

export async function doArrangeApply(ts, btn) {
  if (!store.project) return;
  btn.disabled = true; const old = btn.textContent; btn.textContent = '进工程中…';
  setArrangeState({ busy: 'apply' });
  try {
    const r = await api.arrangeApply(store.project, { batch_ts: ts });
    bus.dispatch('toast', `已进工程：${r.n_tracks} 轨 / ${r.n_notes} 音（命令层事务）
      — 不满意可整批撤销或再生成`.replace(/\s+/g, ''));
    stopArrAudio();
    // 批次状态回填（走 get，与后端一致）
    try {
      const got = await api.arrangeGet(store.project, ts);
      const a = store.arrange;
      if (a.batch && a.batch.batch_ts === ts) setArrangeState({ batch: got.batch });
    } catch (e) { /* ignore */ }
    bus.dispatch('refresh_staging', {});
  } catch (e) { setError(String((e && e.message) || e)); }
  finally {
    setArrangeState({ busy: null });
    btn.disabled = false; btn.textContent = old;
  }
}

export async function doArrangeDiscard(ts, btn) {
  if (!store.project) return;
  btn.disabled = true; const old = btn.textContent; btn.textContent = '丢弃中…';
  try {
    await api.arrangeDiscard(store.project, ts);
    const a = store.arrange;
    if (a.batch && a.batch.batch_ts === ts) setArrangeState({ batch: Object.assign({}, a.batch, { state: 'discarded' }) });
    bus.dispatch('toast', `已丢弃批次 ${fmtTs(ts)}（工程未动、文件保留）`);
    bus.dispatch('refresh_staging', {});
  } catch (e) { setError(String((e && e.message) || e)); }
  finally {
    btn.disabled = false; btn.textContent = old;
  }
}

/* ---------------- 渲染 ---------------- */

function rolesLine(batch) {
  const tracks = batch.tracks || [];
  const secs = (batch.facts && batch.facts.sections) || [];
  const kindOf = (si) => {
    const s = secs[Number(si)];
    return s ? (s.kind || '?') : ('段' + si);
  };
  const list = el('div', 'arr-list');
  for (const t of tracks) {
    const row = el('div', 'arr-role');
    row.dataset.role = 'arrange-role';
    row.dataset.roleId = t.role;
    row.appendChild(el('span', 'arr-name', t.name));
    const seg = Object.entries(t.sections || {}).map(([si, info]) => {
      return `${kindOf(si)}·${info.variant}${info.fill ? '+fill' : ''}${info.crash ? '+crash' : ''}`;
    }).join(' · ');
    row.appendChild(el('span', 'arr-seg', seg));
    row.appendChild(el('span', 'arr-n', `${t.n_notes || 0} 音`));
    const reason = firstReason(batch, t.role);
    if (reason) row.title = reason;
    if (t.program) row.appendChild(el('span', 'arr-prog', t.program));
    list.appendChild(row);
  }
  return list;
}

function firstReason(batch, role) {
  const rs = (batch.plan && batch.plan.roles && batch.plan.roles[role]) || {};
  for (const k of Object.keys(rs)) {
    const r = rs[k] && rs[k].reason;
    if (r && r !== 'LLM 选择') return r;
  }
  return '';
}

export function renderArrangeBlock() {
  const a = store.arrange;
  const wrap = el('div', 'tune-wrap arr-wrap');

  const head = el('div', 'stg-head');
  head.appendChild(el('span', 'stg-title-main', 'AI 配器'));
  head.appendChild(el('span', 'stg-sub', 'AI 出初稿 → 试听 → 进工程；不满意可带意见重生成'));

  const sel = document.createElement('select');
  sel.className = 'tune-pack';
  sel.dataset.role = 'arrange-pack';
  const ph = document.createElement('option');
  ph.value = ''; ph.textContent = a.packs.length ? '选择风格包…' : '（无可用风格包）';
  sel.appendChild(ph);
  for (const p of (a.packs || [])) {
    const o = document.createElement('option');
    o.value = p.pack;
    o.textContent = p.pack + (p.meter ? `（${p.meter}）` : '');
    sel.appendChild(o);
  }
  sel.value = a.pack || '';
  sel.addEventListener('change', () => setArrangeState({ pack: sel.value }));
  head.appendChild(sel);

  const ss = document.createElement('select');
  ss.className = 'tune-pack';
  ss.dataset.role = 'arrange-strength';
  ss.title = '强度档：整段力度缩放（收 -10% / 标准 ±0 / 满 +10%）';
  const curPack = (a.packs || []).find((p) => p.pack === a.pack);
  const opts = (curPack && curPack.strengths && curPack.strengths.length)
    ? curPack.strengths.map((s) => [s, '强度·' + (STRENGTH_TEXT[s] || s)]) : STRENGTHS;
  for (const [v, label] of opts) {
    const o = document.createElement('option');
    o.value = v; o.textContent = label;
    ss.appendChild(o);
  }
  ss.value = a.strength || 'standard';
  ss.addEventListener('change', () => setArrangeState({ strength: ss.value }));
  head.appendChild(ss);

  const bg = el('button', 'stg-btn', '🎛 生成配器');
  bg.dataset.role = 'arrange-generate';
  bg.disabled = !!a.busy || !store.project;
  bg.addEventListener('click', () => doArrangeGenerate(bg));
  head.appendChild(bg);
  if (a.busy) {
    head.appendChild(el('span', 'tune-busy',
      a.busy === 'generate' ? '生成中…（LLM 决策 + 机械展开）'
        : a.busy === 'preview' ? '试听渲染中…' : '进工程中…（命令层事务）'));
  }
  wrap.appendChild(head);

  const b = a.batch;
  if (!b) return wrap;

  const st = b.stats || {};
  const line = el('div', 'stg-ctx');
  line.dataset.role = 'arrange-facts';
  line.appendChild(el('span', 'stg-ctx-item',
    `批次 ${fmtTs(b.batch_ts)} · ${ARR_STATE_TEXT[b.state] || b.state} · ${(b.tracks || []).length} 轨 / ${st.notes || 0} 音`
    + ` · 包 ${b.pack}${b.strength ? '·' + b.strength : ''} · 来源 ${SR_TEXT[st.source] || st.source || '—'}`));
  if (st.dropped && st.dropped.length) {
    line.appendChild(el('span', 'stg-ctx-item', `校验拒绝 ${st.dropped.length}`));
  }
  wrap.appendChild(line);

  if (b.overall) {
    const ov = el('div', 'tune-reason');
    ov.dataset.role = 'arrange-overall';
    ov.textContent = '整体思路：' + b.overall;
    wrap.appendChild(ov);
  }

  wrap.appendChild(rolesLine(b));

  const foot = el('div', 'tune-foot');
  const pv = el('button', 'stg-btn stg-play' + ((arrPlaying && arrPlaying.url === api.arrangeFileUrl(store.project, b.batch_ts, 'preview-mix.wav')) ? ' on' : ''),
                '▶ 试听预览');
  pv.dataset.role = 'arrange-preview';
  pv.disabled = !!a.busy || !(b.tracks || []).length;
  pv.title = '渲染候选混音（现有工程 + 配器轨副本；工程零改动）';
  pv.addEventListener('click', () => doArrangePreview(b.batch_ts, pv));
  foot.appendChild(pv);
  if (b.state === 'pending' && (b.tracks || []).length) {
    const ap = el('button', 'stg-btn stg-adopt', `⇥ 进工程（${(b.tracks || []).length} 轨）`);
    ap.dataset.role = 'arrange-apply';
    ap.disabled = !!a.busy;
    ap.addEventListener('click', () => doArrangeApply(b.batch_ts, ap));
    foot.appendChild(ap);
    const dp = el('button', 'stg-btn stg-discard', '✕ 丢弃批次');
    dp.dataset.role = 'arrange-discard';
    dp.addEventListener('click', () => doArrangeDiscard(b.batch_ts, dp));
    foot.appendChild(dp);
  }
  const ta = document.createElement('textarea');
  ta.className = 'tune-ctx';
  ta.dataset.role = 'arrange-context';
  ta.placeholder = b.state === 'pending'
    ? '带意见重生成（例：鼓太密了、铺底别那么满、加点拨弦）…'
    : '对这套不满意？写下意见，再生成一版…';
  foot.appendChild(ta);
  const rg = el('button', 'stg-btn', '🔁 带意见重生成');
  rg.dataset.role = 'arrange-regen';
  rg.addEventListener('click', () => doArrangeGenerate(rg, ta.value));
  foot.appendChild(rg);
  wrap.appendChild(foot);
  return wrap;
}

/* ---------------- 刷新 / 初始化 ---------------- */

export async function refreshArrangePacks() {
  if (!store.project) return;
  try {
    const r = await api.arrangePacks(store.project);
    const packs = r.packs || [];
    const cur = store.arrange.pack;
    const keep = packs.some((p) => p.pack === cur);
    // 不自动选包（异步晚到曾覆盖手选 → 用错包生成）：保持占位，交用户显式选择
    setArrangeState({ packs, pack: keep ? cur : '' });
  } catch (e) { /* 端点缺失/网络异常：保持空 */ }
}

export async function refreshArrange() {
  if (!store.project) return;
  try {
    const lst = await api.arrangeList(store.project);
    const first = (lst.batches || [])[0];
    if (!first) {
      setArrangeState({ batch: null, loaded: true });
      return;
    }
    const r = await api.arrangeGet(store.project, first.batch_ts);
    setArrangeState({ batch: r.batch, loaded: true });
  } catch (e) {
    setArrangeState({ loaded: true });
  }
}

export function enterArrange() {
  refreshArrangePacks();
  refreshArrange();
}

export function initArrange() {
  bus.on('arrange_updated', (ev) => {   // 另一通道（REST/agent）的配器动作 → 刷新
    if (!store.project) return;
    refreshArrange();
    bus.dispatch('refresh_staging', {});
  });
  bus.on('audio_started', (ev) => {   // 音频互斥：别家开播 → 停我
    if (!ev || ev.owner !== 'arrange') stopArrAudio();
  });
  bus.on('state', () => {   // 工程切换：清镜像
    if (lastProjectA === store.project) return;
    lastProjectA = store.project;
    stopArrAudio();
    setArrangeState({ packs: [], batch: null, busy: null, loaded: false });
  });
}
