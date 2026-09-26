/* staging.js —— 「AI」页签：审查中心 + AI 调参（M-V8 E4 段1/段2 · Q10.5/Q2–Q6）
   - 工程上下文：agents.md 双层（工程 agents.md + 全局 agents-user.md）——「刷新」= 后端确定性重生成
   - AI 调参：analyze（事实包）→ suggest（双通道建议卡：标题/理由/前后值/证据）
     → 勾选应用（命令层事务 EditBatch）+ 自动对拍「应用小结」；卡试听 = 预览渲染（工程零改动）；
     意见框 = 带意见重生成；撤销 = 整批一次；丢弃 = 仅记处置（工程零触碰）
   - 暂存区：AI/链产物默认先进暂存，人批才落地——列表 / 试听 / 采纳 / 丢弃
   - 状态真值在后端（tsov/staging.py + tsov/tune/store.py）；前端镜像 store.staging / store.tune
   - REST：staging / agents + tune（analyze/suggest/apply/preview/discard/list/get/file）+ undo
   - __tsovState().staging / .tune 暴露给 CDP 断言 */

import { bus } from './events.js';
import { api } from './api.js';
import { store, setError, setStagingState, setTuneState } from './state.js';

let pane = null;
let busy = false;
let audio = null;          // 单例试听（切页/再点即停）
let playing = null;        // {itemId, file}
let tuneAudio = null;      // 调参试听（独立于暂存区）
let tunePlaying = null;    // {url}
let lastProject = null;
let chainWasRunning = false;

const STATE_TEXT = { pending: '待处置', adopted: '已采纳', discarded: '已丢弃' };
const TUNE_STATE_TEXT = { pending: '待处置', adopted: '已应用', discarded: '已丢弃' };
const KIND_TEXT = { level: '电平', pan: '声像', effect: '效果链' };
const REP_ICON = { ok: '✅', warn: '⚠', bad: '❗', info: 'ℹ' };
const PACKS = [
  ['', '通用（平衡口径）'],
  ['pop-band-standard', '流行乐队'],
  ['wotaiko-fast-6-8', '和风快步'],
  ['edm-electro-4-4', '电音'],
  ['orchestral-basic', '管弦'],
  ['piano-ballad-duet', '钢琴二重'],
  ['rock-band', '摇滚'],
];

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

function fmtDb(v) {
  if (v == null) return '—';
  const n = Number(v);
  return (n > 0 ? '+' : '') + n.toFixed(1) + ' dB';
}

function fmtNum(v) { return v == null ? '—' : String(v); }

function fmtSigned(v) { const n = Number(v); return (n > 0 ? '+' : '') + n.toFixed(1); }

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

/* ---------------- 试听（调参） ---------------- */

function stopTuneAudio() {
  if (tuneAudio) { try { tuneAudio.pause(); } catch (e) { /* ignore */ } }
  tuneAudio = null;
  tunePlaying = null;
  try { window.__tsovTuneAudio = null; } catch (e) { /* ignore */ }
}

function playTune(url) {
  const wasSame = !!(tunePlaying && tunePlaying.url === url);
  stopAudio();
  stopTuneAudio();
  if (wasSame) { renderStaging(); return; }
  try {
    tuneAudio = new Audio(url);
    try { window.__tsovTuneAudio = tuneAudio; } catch (e) { /* ignore */ }   // CDP 调参试听钩子
    tuneAudio.addEventListener('ended', () => { stopTuneAudio(); renderStaging(); });
    tuneAudio.play().catch(() => { /* ignore */ });
    tunePlaying = { url };
  } catch (e) { /* ignore */ }
  renderStaging();
}

function tuneFileBtn(ts, file, label) {
  const url = api.tuneFileUrl(store.project, ts, file);
  const on = !!(tunePlaying && tunePlaying.url === url);
  const b = el('button', 'stg-btn stg-play' + (on ? ' on' : ''), (on ? '⏸ ' : '▶ ') + label);
  b.title = file;
  b.dataset.role = 'tune-file';
  b.dataset.file = file;
  b.addEventListener('click', () => playTune(url));
  return b;
}

/* ---------------- 动作（暂存区） ---------------- */

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

/* ---------------- 动作（调参） ---------------- */

function selectedCount() {
  const sel = store.tune.selected || {};
  return Object.keys(sel).filter((k) => sel[k]).length;
}

function defaultSelected(batch) {
  const m = {};
  for (const s of (batch && batch.suggestions) || []) m[s.id] = true;
  return m;
}

async function doAnalyze(btn) {
  if (busy || !store.project) return;
  busy = true; const old = btn.textContent; btn.disabled = true; btn.textContent = '分析中…';
  setTuneState({ busy: 'analyze' });
  try {
    const r = await api.tuneAnalyze(store.project, store.tune.pack || '');
    setTuneState({ facts: r.facts });
    bus.dispatch('toast', '事实包已就绪（结构 / 电平 / 逐段 / 频谱 / 目标）');
  } catch (e) { setError(String((e && e.message) || e)); }
  finally {
    busy = false; setTuneState({ busy: null });
    btn.disabled = false; btn.textContent = old;
    renderStaging();
  }
}

async function doSuggest(btn, context) {
  if (busy || !store.project) return;
  busy = true; const old = btn.textContent; btn.disabled = true; btn.textContent = '生成中…（< 1 分钟）';
  setTuneState({ busy: 'suggest' });
  try {
    const r = await api.tuneSuggest(store.project,
                                    { pack: store.tune.pack || null, context: context || '' });
    const b = r.batch;
    setTuneState({ batch: b, report: null, facts: b.facts || store.tune.facts,
                   selected: defaultSelected(b), loaded: true });
    bus.dispatch('toast', `建议批次 ${fmtTs(b.batch_ts)}：${(b.suggestions || []).length} 条`
      + `（已进暂存区，人批才落地）`);
    await refreshStaging();
  } catch (e) { setError(String((e && e.message) || e)); }
  finally {
    busy = false; setTuneState({ busy: null });
    btn.disabled = false; btn.textContent = old;
    renderStaging();
  }
}

/* 批次应用（勾选集 / 缺省全选）——调参区与暂存区卡片共用 */
async function applyBatchCore(ts, ids) {
  const r = await api.tuneApply(store.project, { batch_ts: ts, ids: ids || undefined });
  const t = store.tune;
  const done = Object.assign({}, r.report, { batch_ts: ts });
  if (t.batch && t.batch.batch_ts === ts) {
    setTuneState({ report: done,
                   batch: Object.assign({}, t.batch, { state: 'adopted', applied: r.applied }) });
  } else {
    setTuneState({ report: done });
  }
  return r;
}

async function doApply(btn) {
  const t = store.tune;
  if (busy || !store.project || !t.batch) return;
  const ids = (t.batch.suggestions || []).filter((s) => t.selected[s.id]).map((s) => s.id);
  if (!ids.length) return;
  busy = true; const old = btn.textContent; btn.disabled = true; btn.textContent = '应用中…';
  setTuneState({ busy: 'apply' });
  try {
    const r = await applyBatchCore(t.batch.batch_ts, ids);
    bus.dispatch('toast', `已应用 ${r.n} 条建议（${r.commands} 条命令 · 对拍见小结卡 · 可整批撤销）`);
    await refreshStaging();
    await refreshAgents();
  } catch (e) { setError(String((e && e.message) || e)); }
  finally {
    busy = false; setTuneState({ busy: null });
    btn.disabled = false; btn.textContent = old;
    renderStaging();
  }
}

/* 暂存区 tune 条目：整批应用（全部建议） */
async function doTuneItemApply(item, btn) {
  if (busy || !store.project) return;
  const ts = (item.refs && item.refs.batch_ts) || '';
  busy = true; btn.disabled = true; const old = btn.textContent; btn.textContent = '应用中…';
  try {
    const r = await applyBatchCore(ts, null);
    bus.dispatch('toast', `已应用批次 ${fmtTs(ts)}：${r.n} 条建议（${r.commands} 条命令）`);
    await refreshStaging();
    await refreshAgents();
  } catch (e) { setError(String((e && e.message) || e)); }
  finally {
    busy = false; btn.disabled = false; btn.textContent = old;
    renderStaging();
  }
}

async function doTuneDiscard(ts, btn) {
  if (busy || !store.project) return;
  busy = true; btn.disabled = true; const old = btn.textContent; btn.textContent = '丢弃中…';
  try {
    await api.tuneDiscard(store.project, { batch_ts: ts });
    const t = store.tune;
    if (t.batch && t.batch.batch_ts === ts) {
      setTuneState({ batch: Object.assign({}, t.batch, { state: 'discarded' }), report: null });
    }
    bus.dispatch('toast', `已丢弃批次 ${fmtTs(ts)}（工程未动、文件保留）`);
    await refreshStaging();
  } catch (e) { setError(String((e && e.message) || e)); }
  finally {
    busy = false; btn.disabled = false; btn.textContent = old;
    renderStaging();
  }
}

async function doUndoApply(btn) {
  if (busy || !store.project) return;
  busy = true; btn.disabled = true; const old = btn.textContent; btn.textContent = '撤销中…';
  try {
    await api.undo(store.project);
    setTuneState({ report: null });
    bus.dispatch('toast', '已整批撤销该次调参应用（工程回到应用前）');
    await refreshStaging();
  } catch (e) { setError(String((e && e.message) || e)); }
  finally {
    busy = false; btn.disabled = false; btn.textContent = old;
    renderStaging();
  }
}

async function doPreview(batch, s, btn) {
  if (busy || !store.project) return;
  const pvUrl = api.tuneFileUrl(store.project, batch.batch_ts, 'preview-' + s.id + '.wav');
  if (tunePlaying && tunePlaying.url === pvUrl) {   // 再点 = 停（同暂存区卡片交互）
    stopTuneAudio();
    bus.dispatch('toast', '已停止试听');
    renderStaging();
    return;
  }
  busy = true; btn.disabled = true; const old = btn.textContent; btn.textContent = '渲染中…';
  setTuneState({ busy: 'preview' });
  try {
    const r = await api.tunePreview(store.project, batch.batch_ts, s.id);
    stopTuneAudio();
    playTune(r.url);
    bus.dispatch('toast', `试听预览：${s.title || s.id}（工程未动）`);
  } catch (e) { setError(String((e && e.message) || e)); }
  finally {
    busy = false; setTuneState({ busy: null });
    btn.disabled = false; btn.textContent = old;
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

/* ---------- 调参区 ---------- */

function renderTuneFacts() {
  const f = store.tune.facts;
  if (!f) return null;
  const st = f.structure || {};
  const lv = f.levels || {};
  const tracks = st.tracks || [];
  const anchor = (lv.anchor != null && tracks[lv.anchor]) ? tracks[lv.anchor].name : '—';
  const bits = [
    `${tracks.length} 轨`,
    st.duration_sec != null ? `时长 ${Number(st.duration_sec).toFixed(1)}s` : null,
    `锚点「${anchor}」`,
    `混音峰值 ${fmtDb(lv.mix_peak_dbfs)}`,
    lv.clipping ? '⚠ 削波' : null,
    `目标：${(f.targets && f.targets.source) || '通用'}`,
  ];
  const line = el('div', 'stg-ctx');
  line.dataset.role = 'tune-facts';
  line.appendChild(el('span', 'stg-ctx-item', '事实：' + bits.filter(Boolean).join(' · ')));
  const unmatched = (f.targets && f.targets.unmatched) || [];
  if (unmatched.length) {
    line.appendChild(el('span', 'stg-ctx-item', `未匹配目标轨：${unmatched.join('、')}`));
  }
  return line;
}

function beforeAfterText(s) {
  const v = s.values || {};
  if (s.kind === 'level') {
    return `音量 ${fmtNum(v.before_volume)} → ${fmtNum(v.after_volume)}`
      + `（${fmtSigned(v.delta_db)} dB${v.capped ? ' · 本步封顶' : ''}）`;
  }
  if (s.kind === 'pan') return `声像 ${fmtNum(v.before_pan)} → ${fmtNum(v.pan)}`;
  const e = s.effect || {};
  if (e.preset) return `效果链「${e.preset}」`;
  if (e.effect) return `加效果：${e.effect.type}`;
  if (e.set_params) return `改参数：参数 #${e.set_params.index}`;
  return '';
}

function suggestionCard(batch, s) {
  const t = store.tune;
  const on = !!(t.batch && t.batch.batch_ts === batch.batch_ts && t.selected[s.id]);
  const card = el('div', 'tune-card' + (on ? ' sel' : '') + ' tune-k-' + s.kind);
  card.dataset.suggId = s.id;
  const cb = document.createElement('input');
  cb.type = 'checkbox';
  cb.checked = on;
  cb.disabled = batch.state !== 'pending';
  cb.dataset.role = 'tune-check';
  cb.addEventListener('change', () => {
    const m = Object.assign({}, store.tune.selected);
    m[s.id] = cb.checked;
    setTuneState({ selected: m });
  });
  card.appendChild(cb);

  const body = el('div', 'tune-card-body');
  const hd = el('div', 'stg-hd');
  hd.appendChild(el('span', 'tune-kind', KIND_TEXT[s.kind] || s.kind));
  hd.appendChild(el('span', 'stg-title', s.title || s.id));
  body.appendChild(hd);
  const ba = beforeAfterText(s);
  if (ba) body.appendChild(el('div', 'tune-ba', ba));
  if (s.reason) body.appendChild(el('div', 'tune-reason', s.reason));
  const refs = (s.evidence && s.evidence.refs) || [];
  if (refs.length) body.appendChild(el('div', 'tune-ev', '证据：' + refs.join(' · ')));
  const acts = el('div', 'stg-acts');
  const pvUrl = api.tuneFileUrl(store.project, batch.batch_ts, 'preview-' + s.id + '.wav');
  const pvOn = !!(tunePlaying && tunePlaying.url === pvUrl);
  const pv = el('button', 'stg-btn stg-play' + (pvOn ? ' on' : ''), (pvOn ? '⏸ ' : '▶ ') + '试听');
  pv.dataset.role = 'tune-preview';
  pv.disabled = !!store.tune.busy;
  pv.title = '在副本上试跑渲染（工程零改动）';
  pv.addEventListener('click', () => doPreview(batch, s, pv));
  acts.appendChild(pv);
  body.appendChild(acts);
  card.appendChild(body);
  return card;
}

function renderReportCard() {
  const rep = store.tune.report;
  if (!rep) return null;
  const card = el('div', 'tune-report tune-rep-' + (rep.status || 'info'));
  card.dataset.role = 'tune-report';
  const hd = el('div', 'stg-hd');
  const head = rep.status === 'ok' ? '✅ 应用小结' : rep.status === 'warn' ? '⚠ 应用小结'
    : rep.status === 'bad' ? '❗ 应用小结' : 'ℹ 应用小结';
  hd.appendChild(el('span', 'tune-kind', head));
  hd.appendChild(el('span', 'stg-sub',
    `批次 ${fmtTs(rep.batch_ts || '')} · ${(rep.applied || []).length} 条建议`));
  card.appendChild(hd);
  const items = el('div', 'tune-rep-items');
  for (const it of rep.items || []) {
    items.appendChild(el('div', 'tune-rep-item', (REP_ICON[it.status] || '·') + ' ' + it.text));
  }
  card.appendChild(items);
  const acts = el('div', 'stg-acts');
  const f = rep.files || {};
  if (f.before) acts.appendChild(tuneFileBtn(rep.batch_ts, f.before, 'before'));
  if (f.after) acts.appendChild(tuneFileBtn(rep.batch_ts, f.after, 'after'));
  const un = el('button', 'stg-btn stg-discard', '↩ 整批撤销');
  un.dataset.role = 'tune-undo';
  un.title = '一次撤销整批调参应用（工程回到应用前）';
  un.addEventListener('click', () => doUndoApply(un));
  acts.appendChild(un);
  card.appendChild(acts);
  return card;
}

function renderTuneBlock() {
  const wrap = el('div', 'tune-wrap');

  const head = el('div', 'stg-head');
  head.appendChild(el('span', 'stg-title-main', 'AI 调参'));
  const sel = document.createElement('select');
  sel.className = 'tune-pack';
  sel.dataset.role = 'tune-pack';
  for (const [v, label] of PACKS) {
    const o = document.createElement('option');
    o.value = v; o.textContent = label;
    sel.appendChild(o);
  }
  sel.value = store.tune.pack || '';
  sel.addEventListener('change', () => setTuneState({ pack: sel.value }));
  head.appendChild(sel);
  const ba = el('button', 'stg-btn', '🔍 分析事实');
  ba.dataset.role = 'tune-analyze';
  ba.disabled = !!store.tune.busy || !store.project;
  ba.addEventListener('click', () => doAnalyze(ba));
  head.appendChild(ba);
  const bs = el('button', 'stg-btn', '✨ 生成建议');
  bs.dataset.role = 'tune-suggest';
  bs.disabled = !!store.tune.busy || !store.project;
  bs.addEventListener('click', () => doSuggest(bs));
  head.appendChild(bs);
  if (store.tune.busy) {
    head.appendChild(el('span', 'tune-busy',
      store.tune.busy === 'analyze' ? '分析中…（渲染测量）'
        : store.tune.busy === 'suggest' ? '生成建议中…'
          : store.tune.busy === 'apply' ? '应用中…（命令层 + 对拍）' : '试听渲染中…'));
  }
  wrap.appendChild(head);

  const factsLine = renderTuneFacts();
  if (factsLine) wrap.appendChild(factsLine);

  const repCard = renderReportCard();
  if (repCard) wrap.appendChild(repCard);

  const b = store.tune.batch;
  if (b) {
    const sHead = el('div', 'stg-head');
    sHead.appendChild(el('span', 'stg-title-main', '建议'));
    sHead.appendChild(el('span', 'stg-sub',
      `批次 ${fmtTs(b.batch_ts)} · ${TUNE_STATE_TEXT[b.state] || b.state}`
      + ` · ${(b.suggestions || []).length} 条`
      + (b.stats && b.stats.dropped ? ` · 校验拒绝 ${b.stats.dropped}` : '')));
    if (b.state === 'pending' && (b.suggestions || []).length) {
      const all = el('button', 'stg-btn', '全选');
      all.dataset.role = 'tune-select-all';
      all.addEventListener('click', () => setTuneState({ selected: defaultSelected(b) }));
      const none = el('button', 'stg-btn', '全不选');
      none.dataset.role = 'tune-select-none';
      none.addEventListener('click', () => setTuneState({ selected: {} }));
      sHead.appendChild(all);
      sHead.appendChild(none);
    }
    wrap.appendChild(sHead);

    const list = el('div', 'tune-list');
    if (!(b.suggestions || []).length) {
      const why = (b.stats && b.stats.llm_errors && b.stats.llm_errors[0]) || '暂无建议';
      list.appendChild(el('div', 'stg-empty', '该批次没有建议——' + why));
    } else {
      for (const s of b.suggestions) list.appendChild(suggestionCard(b, s));
    }
    wrap.appendChild(list);

    /* 意见框 = 迭代入口（常在：任意状态都可带意见重生成新批次）；
       应用/丢弃 = 仅 pending 批次的处置动作 */
    const foot = el('div', 'tune-foot');
    const ta = document.createElement('textarea');
    ta.className = 'tune-ctx';
    ta.dataset.role = 'tune-context';
    ta.placeholder = b.state === 'pending'
      ? '带意见重生成（例：人声再亮一点、鼓别太猛、整体更贴流行）…'
      : '对这套结果不满意？写下意见，再生成一版…';
    foot.appendChild(ta);
    const rg = el('button', 'stg-btn', '🔁 带意见重生成');
    rg.dataset.role = 'tune-regen';
    rg.addEventListener('click', () => doSuggest(rg, ta.value));
    foot.appendChild(rg);
    if (b.state === 'pending' && (b.suggestions || []).length) {
      const nSel = selectedCount();
      const ap = el('button', 'stg-btn stg-adopt', `⇥ 应用所选（${nSel} 条）`);
      ap.dataset.role = 'tune-apply';
      ap.disabled = !!store.tune.busy || nSel === 0;
      ap.addEventListener('click', () => doApply(ap));
      foot.appendChild(ap);
      const dp = el('button', 'stg-btn stg-discard', '✕ 丢弃批次');
      dp.dataset.role = 'tune-discard';
      dp.addEventListener('click', () => doTuneDiscard(b.batch_ts, dp));
      foot.appendChild(dp);
    }
    wrap.appendChild(foot);
  }
  return wrap;
}

/* ---------- 暂存区卡片 ---------- */

function itemCard(it) {
  const card = el('div', 'stg-card stg-' + it.state);
  card.dataset.itemId = it.id;   // CDP 验收定向选择器（E4 段1）
  const hd = el('div', 'stg-hd');
  hd.appendChild(el('span', 'stg-badge stg-badge-' + it.state, STATE_TEXT[it.state] || it.state));
  hd.appendChild(el('span', 'stg-title', it.title));
  card.appendChild(hd);

  const bits = [];
  if (it.producer === 'tune') {
    const m = it.meta || {};
    bits.push(`调参批次 ${fmtTs(m.batch_ts)}`);
    bits.push(`${m.suggestions || 0} 条建议`);
    if (m.applied) bits.push(`已应用 ${m.applied}`);
    if (m.pack) bits.push('包 ' + m.pack);
    if (m.has_report) bits.push('有对拍小结');
  } else {
    bits.push('链运行 ' + fmtTs(it.meta && it.meta.run_ts));
    const raw = it.meta && it.meta.notes_raw, proc = it.meta && it.meta.notes_processed;
    if (raw != null) bits.push('原始 ' + raw + ' 音');
    if (proc != null) bits.push('处理 ' + proc + ' 音');
    if (!it.ready) bits.push('未跑完（缺 ' + ((it.meta && it.meta.missing) || []).join('、') + '）');
  }
  if (it.handled_at) bits.push((it.state === 'adopted' ? '采纳于 ' : '丢弃于 ') + fmtUnix(it.handled_at));
  card.appendChild(el('div', 'stg-sub', bits.join(' · ')));

  const acts = el('div', 'stg-acts');
  if (it.producer === 'tune') {
    const m = it.meta || {};
    if (m.has_report) {
      acts.appendChild(tuneFileBtn(m.batch_ts, 'before.wav', 'before'));
      acts.appendChild(tuneFileBtn(m.batch_ts, 'after.wav', 'after'));
    }
    if (it.state === 'pending') {
      const ok = el('button', 'stg-btn stg-adopt', `⇥ 应用批次（${m.suggestions || 0} 条）`);
      ok.disabled = !it.ready;
      ok.dataset.role = 'stg-tune-apply';
      ok.title = '整批应用（命令层事务 + 自动对拍；可一次撤销）';
      ok.addEventListener('click', () => doTuneItemApply(it, ok));
      const no = el('button', 'stg-btn stg-discard', '✕ 丢弃');
      no.title = '仅记处置：工程不动、产物保留';
      no.addEventListener('click', () => doTuneDiscard(m.batch_ts, no));
      acts.appendChild(ok);
      acts.appendChild(no);
    }
  } else {
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
  rf.addEventListener('click', () => { refreshStaging(); refreshAgents(); refreshTune(); });
  head.appendChild(rf);
  wrap.appendChild(head);
  wrap.appendChild(renderAgentsBlock());
  wrap.appendChild(renderTuneBlock());

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
      '暂无暂存条目——在「链」页签跑一次哼唱快车道，或用上方「AI 调参」生成一版建议，产物会先进这里等你处置'));
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

export async function refreshTune() {
  if (!store.project) return;
  try {
    const lst = await api.tuneList(store.project);
    const first = (lst.batches || [])[0];
    if (!first) {
      setTuneState({ batch: null, report: null, selected: {}, loaded: true });
      return;
    }
    const r = await api.tuneGet(store.project, first.batch_ts);
    const b = r.batch;
    const cur = store.tune;
    const keep = !!(cur.batch && cur.batch.batch_ts === b.batch_ts);
    setTuneState({
      batch: b,
      facts: b.facts || cur.facts,
      report: b.report || (keep ? cur.report : null),
      selected: keep ? cur.selected : defaultSelected(b),
      loaded: true,
    });
  } catch (e) {
    setTuneState({ loaded: true });
  }
}

export function enterStaging() {
  renderStaging();
  if (store.project) {
    refreshStaging();
    refreshAgents();
    refreshTune();
  }
}

export function initStaging(paneEl) {
  pane = paneEl;
  bus.on('staging', renderStaging);
  bus.on('tune', renderStaging);
  bus.on('state', () => {
    if (lastProject === store.project) return;
    lastProject = store.project;   // 工程切换：清镜像（页签可见时重拉）
    stopAudio();
    stopTuneAudio();
    setStagingState({ items: [], counts: { pending: 0, adopted: 0, discarded: 0 },
                      agents: null, loaded: false });
    setTuneState({ facts: null, batch: null, selected: {}, report: null, busy: null, loaded: false });
    if (paneEl.classList.contains('active')) enterStaging();
  });
  bus.on('chain', () => {   // 链跑完 → 新产物自动进列表
    const running = !!store.chain.running;
    if (chainWasRunning && !running && store.project) refreshStaging();
    chainWasRunning = running;
  });
  bus.on('tune_updated', (ev) => {   // 另一通道（agent/REST）的调参动作 → 刷新
    if (!store.project) return;
    const t = store.tune;
    if (ev && t.batch && ev.batch_ts === t.batch.batch_ts) {
      api.tuneGet(store.project, ev.batch_ts).then((r) => {
        setTuneState({ batch: r.batch, report: r.batch.report || store.tune.report });
      }).catch(() => { /* ignore */ });
    }
    refreshStaging();
  });
}
