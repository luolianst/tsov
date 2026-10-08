/* chain.js —— 处理链面板（M-V8 E3 段2；09-26 卡片化：横排卡片 + › 连接）：哼唱快车道 5 工具槽位
   - 状态真值在后端（ChainRunner + 工程 chain.json）；前端镜像 store.chain
   - REST：tools / status / run / config / cancel / artifact（ADR-0017：与 agent 同一动作路径）
   - 快档改参 → config 返回 plan.auto → 自动顺跑；慢档 → needs_apply 亮「应用」
   - 运行中轮询 status（600ms）；结束即停表
   - __tsovState().chain 暴露给 CDP 断言 */

import { bus } from './events.js';
import { api } from './api.js';
import { store, setError, setChainState } from './state.js';

let pane = null;
let pollTimer = null;
let toolsLoaded = false;
let busy = false;
let lastProject = null;
let refreshing = false;

const STATUS_TEXT = { pending: '待跑', running: '运行中', done: '完成', failed: '失败', skipped: '跳过', cancelled: '已取消', dirty: '待应用' };

function el(tag, cls, text) {
  const d = document.createElement(tag);
  if (cls) d.className = cls;
  if (text != null) d.textContent = text;
  return d;
}

function finalText(steps) {
  const cnt = (x) => steps.filter((i) => i.status === x).length;
  const parts = [];
  if (cnt('done')) parts.push('完成 ' + cnt('done'));
  if (cnt('skipped')) parts.push('跳过 ' + cnt('skipped'));
  if (cnt('failed')) parts.push('失败 ' + cnt('failed'));
  if (cnt('cancelled')) parts.push('取消 ' + cnt('cancelled'));
  return parts.join(' · ') || '无变化';
}

async function ensureTools() {
  if (toolsLoaded || !store.project) return;
  try {
    const r = await api.chainTools(store.project);
    toolsLoaded = true;
    setChainState({ tools: r.tools || [], preset: r.preset || store.chain.preset });
  } catch (e) { /* 端点缺失/网络异常：槽位名与参数表单暂缺，运行仍可用 */ }
}

export async function refreshChain() {
  if (!store.project || refreshing) return;
  refreshing = true;
  try {
    const st = await api.chainStatus(store.project);
    const prevRunning = store.chain.running;
    setChainState({
      steps: st.steps || [],
      running: !!st.running,
      savedOnly: !!st.saved_only,
      preset: st.preset || store.chain.preset,
      runTs: st.run_ts || '',
      sourceTrack: (st.source_track != null) ? st.source_track : null,
      finished: !st.running && !st.saved_only,
      error: st.error || null,
    });
    if (st.running) ensurePoll(); else stopPoll();
    if (prevRunning && !st.running) bus.dispatch('toast', '处理链结束：' + finalText(store.chain.steps));
  } catch (e) {
    /* 工程无链也无妨（status 恒可读） */
  } finally {
    refreshing = false;
  }
}

function ensurePoll() {
  if (pollTimer) return;
  pollTimer = setInterval(() => { refreshChain(); }, 600);
}

function stopPoll() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

async function doRun(steps) {
  if (busy) return;
  if (!store.project) { setError('先打开一个工程'); return; }
  busy = true;
  try {
    const body = {};
    if (steps && steps.length) body.steps = steps;
    const r = await api.chainRun(store.project, body);
    setChainState({ running: !!r.running, needsApply: '', steps: r.steps || store.chain.steps });
    bus.dispatch('toast', '链已开跑（' + (steps && steps.length ? steps.join(' → ') : '全链') + '）');
    ensurePoll();
  } catch (e) {
    setError('链运行：' + e.message);
  } finally {
    busy = false;
  }
}

async function doCancel() {
  if (!store.project) return;
  try {
    await api.chainCancel(store.project);
    bus.dispatch('toast', '已请求取消（步边界生效；已完成产物保留）');
  } catch (e) { setError('取消：' + e.message); }
}

async function changeParam(toolId, key, val) {
  if (!store.project) return;
  try {
    const r = await api.chainConfig(store.project, { overrides: { [toolId]: { [key]: val } } });
    const plan = r.plan || {};
    if (plan.auto && plan.auto.length) {
      await doRun(plan.auto);                       // 快档：从脏步起自动顺跑（Q2）
    } else if (plan.needs_apply) {
      setChainState({ needsApply: plan.needs_apply });
      bus.dispatch('toast', '慢档参数已存：点该步「应用」重跑');
    } else {
      await refreshChain();
    }
  } catch (e) { setError('参数保存：' + e.message); }
}

async function toggleMute(toolId, mute) {
  if (!store.project) return;
  try {
    await api.chainConfig(store.project, { mute: { [toolId]: mute } });
    await refreshChain();
  } catch (e) { setError('mute：' + e.message); }
}

function restIds(fromIdx) {
  return store.chain.steps.slice(fromIdx).map((x) => x.tool_id);
}

/* 段3：失败恢复——读取该步输入框当前参数 → 保存（chain.json）→ 从该步起重跑 */
async function rerunWithParams(s, idx) {
  if (busy || store.chain.running || !store.project) return;
  const tool = store.chain.tools.find((t) => t.id === s.tool_id);
  const specs = (tool && tool.params) || {};
  const p = {};
  const row = document.querySelector('.chain-step[data-tool="' + s.tool_id + '"]');
  if (row) {
    row.querySelectorAll('[data-param]').forEach((ctl) => {
      const spec = specs[ctl.dataset.param] || {};
      p[ctl.dataset.param] = (spec.type === 'number') ? Number(ctl.value) : ctl.value;
    });
  }
  if (!Object.keys(p).length) p = null;   // 无输入框：退回「重试」语义
  try {
    if (p) await api.chainConfig(store.project, { overrides: { [s.tool_id]: p } });
  } catch (e) {
    setError('参数保存：' + e.message);
    return;
  }
  doRun(restIds(idx));
}

/* 段3：链产物进工程（双轨）——REST → 命令层事务；SSE state_updated 自动刷新轨列表 */
async function doApply() {
  if (busy || store.chain.running || !store.project) return;
  busy = true;
  try {
    const r = await api.chainApply(store.project, {});
    bus.dispatch('toast', '已进工程：' + (r.names || []).join(' + ')
      + '（轨 ' + (r.tracks != null ? r.tracks : '?') + '，命令 ' + (r.applied || 0) + '）');
  } catch (e) {
    setError('进工程：' + e.message);
  } finally {
    busy = false;
  }
}

function renderStep(s, idx) {
  const card = el('div', 'chain-step ' + (s.status || 'pending'));
  card.dataset.tool = s.tool_id;

  /* 卡头：序号 / 名称 / 状态徽 */
  const hd = el('div', 'chain-hd');
  hd.appendChild(el('span', 'chain-no', String(idx + 1)));
  hd.appendChild(el('span', 'chain-name', (s.name && (s.name.zh || s.name.en)) || s.tool_id));
  hd.appendChild(el('span', 'chain-badge ' + (s.status || 'pending'),
    STATUS_TEXT[s.status] || s.status));
  card.appendChild(hd);

  /* 参数控件（来自 /chain/tools 的 schema） */
  const tool = store.chain.tools.find((t) => t.id === s.tool_id);
  const specs = (tool && tool.params) || {};
  const pbox = el('span', 'chain-params');
  for (const [key, spec] of Object.entries(specs)) {
    const lab = el('label', '');
    lab.appendChild(document.createTextNode((spec.label && (spec.label.zh || spec.label.en)) || key));
    let ctl;
    if (spec.type === 'select') {
      ctl = document.createElement('select');
      for (const opt of (spec.options || [])) {
        const o = document.createElement('option');
        /* v0.2 批C 后段（C7）：选项支持 {value,label} 对象（additive——纯数字/字符串照旧） */
        if (opt && typeof opt === 'object') {
          o.value = String(opt.value);
          o.textContent = String((opt.label && (opt.label.zh || opt.label.en)) || opt.value);
        } else {
          o.value = String(opt);
          o.textContent = String(opt);
        }
        ctl.appendChild(o);
      }
      ctl.value = String(s.params[key] != null ? s.params[key] : spec.default);
    } else {
      ctl = document.createElement('input');
      ctl.type = (spec.type === 'text') ? 'text' : 'number';
      if (spec.min != null) ctl.min = String(spec.min);
      if (spec.max != null) ctl.max = String(spec.max);
      if (spec.step != null) ctl.step = String(spec.step);
      if (spec.type === 'text') ctl.size = 7;
      ctl.value = String(s.params[key] != null ? s.params[key] : spec.default);
    }
    ctl.title = spec.type === 'text'
      ? '调性：auto=自动检测；或形如 C major / a minor / D dorian'
      : ((tool && tool.desc && (tool.desc.zh || tool.desc.en)) || '');
    ctl.dataset.param = key;
    ctl.addEventListener('change', () => {
      const val = (spec.type === 'number') ? Number(ctl.value) : ctl.value;
      changeParam(s.tool_id, key, val);
    });
    lab.appendChild(ctl);
    pbox.appendChild(lab);
  }
  if (pbox.childNodes.length) card.appendChild(pbox);

  /* 统计（完成时的小字摘要） */
  if (s.status === 'done' && s.stats && Object.keys(s.stats).length) {
    card.appendChild(el('span', 'chain-sub',
      Object.entries(s.stats).slice(0, 3).map(([k, v]) => k + '=' + v).join(' · ')));
  }

  /* 失败：错误行（卡内全宽，悬停看全） */
  if (s.status === 'failed') {
    const err = el('span', 'chain-err', s.error || '失败');
    err.title = s.error || '';
    card.appendChild(err);
  }

  /* 卡脚：动作 + 产物 */
  const ft = el('div', 'chain-ft');
  const mb = el('button', 'chain-btn chain-mute' + (s.mute ? ' on' : ''), s.mute ? '已静音' : '静音');
  mb.title = 'mute：本步跳过（存 chain.json，可撤销式重跑）';
  mb.onclick = () => toggleMute(s.tool_id, !s.mute);
  ft.appendChild(mb);

  if (s.status === 'running') ft.appendChild(el('span', 'chain-sub', '运行中…'));

  if (s.status === 'failed') {
    const retry = el('button', 'chain-btn', '重试');
    retry.title = '从该步起重跑（输入用上一轮产物，免重跑上游）';
    retry.onclick = () => doRun(restIds(idx));
    const rerun = el('button', 'chain-btn', '改参再跑');
    rerun.title = '读取本步当前参数并保存，从该步起重跑（先在上方输入框改好参数）';
    rerun.onclick = () => rerunWithParams(s, idx);
    const skip = el('button', 'chain-btn', '跳过');
    skip.title = '静音该步并从其后继续';
    skip.onclick = () => toggleMute(s.tool_id, true).then(() => doRun(restIds(idx + 1)));
    ft.appendChild(retry);
    ft.appendChild(rerun);
    ft.appendChild(skip);
  }

  /* 慢档「应用」（needs_apply → 从该步起跑） */
  if (tool && tool.tier === 'slow' && store.chain.needsApply === s.tool_id) {
    const ap = el('button', 'chain-btn chain-apply', '应用');
    ap.title = '从该步起重跑（含下游）';
    ap.onclick = () => doRun(restIds(idx));
    ft.appendChild(ap);
  }

  /* 产物 */
  if (s.artifact && (s.status === 'done' || s.status === 'skipped')) {
    const a = document.createElement('a');
    a.className = 'chain-art';
    a.textContent = '↗ ' + s.artifact;
    a.href = api.chainArtifactUrl(store.project, s.artifact);
    a.target = '_blank';
    a.title = '打开产物（wav 直接试听；json 下载）';
    ft.appendChild(a);
  }

  card.appendChild(ft);
  return card;
}

export function renderChain() {
  if (!pane) return;
  pane.innerHTML = '';
  if (!store.project) { pane.innerHTML = '<div class="fx-none">先打开一个工程</div>'; return; }
  const wrap = el('div', 'chain-wrap');

  const head = el('div', 'chain-head');
  const title = el('span', 'chain-title', '哼唱快车道');
  title.title = '降噪 → 响度 → 转录 → 量化 → 调内吸附（一句哼唱 → 可编辑音符）｜快档（降噪/响度/量化/吸附）改参即自动顺跑；转录为秒级 → 改参后点「应用」｜静音=跳过该步｜产物在 chain/<运行时间戳>/';
  head.appendChild(title);

  const runAll = el('button', 'chain-btn chain-run', store.chain.running ? '运行中…' : '▶ 运行全链');
  runAll.disabled = store.chain.running || busy;
  runAll.onclick = () => doRun(null);
  head.appendChild(runAll);

  const cancel = el('button', 'chain-btn', '■ 取消');
  cancel.disabled = !store.chain.running;
  cancel.onclick = doCancel;
  head.appendChild(cancel);

  const toProj = el('button', 'chain-btn chain-to-proj', '⇥ 进工程');
  toProj.disabled = store.chain.running || busy || !store.chain.runTs;
  toProj.title = '把最近一次链产物落成两条 MIDI 轨（原始 + 处理），插在源轨正下方；'
    + '重跑幂等（先删旧同名轨）；undo 可回';
  toProj.onclick = doApply;
  head.appendChild(toProj);

  head.appendChild(el('span', 'chain-sub',
    store.chain.running ? '运行中…' : (store.chain.runTs ? ('最近运行 ' + store.chain.runTs) : '未运行')));
  if (store.chain.sourceTrack != null) {
    const tr = store.score && store.score.tracks[store.chain.sourceTrack];
    head.appendChild(el('span', 'chain-sub',
      '源轨：' + store.chain.sourceTrack + (tr && tr.name ? ('（' + tr.name + '）') : '')));
  }
  if (store.chain.error) head.appendChild(el('span', 'chain-err', store.chain.error));
  wrap.appendChild(head);

  if (!store.chain.steps.length) {
    wrap.appendChild(el('div', 'fx-none', '链未初始化（打开工程后自动取 5 步槽位）'));
  } else {
    /* 09-26 卡片化：横排卡片 + › 连接（对齐效果器栏 .fx-chain 设计语言） */
    const row = el('div', 'chain-row');
    store.chain.steps.forEach((s, i) => {
      row.appendChild(renderStep(s, i));
      if (i < store.chain.steps.length - 1) row.appendChild(el('span', 'chain-arrow', '›'));
    });
    wrap.appendChild(row);
  }

  pane.appendChild(wrap);
}

export function enterChain() {
  if (!pane) return;
  renderChain();
  ensureTools()
    .then(() => refreshChain())
    .then(() => renderChain())
    .catch(() => { /* 静默 */ });
}

let wired = false;

export function initChain(paneEl) {
  pane = paneEl;
  if (wired) return;
  wired = true;
  bus.on('chain', renderChain);
  /* M-V8 E3 段2：agent/REST 通道触发的链运行 → SSE 事件驱动 UI 刷新（ADR-0017 双通道同视图） */
  bus.on('chain_started', () => refreshChain());
  bus.on('chain_step', () => refreshChain());
  bus.on('chain_finished', () => refreshChain());
  bus.on('state', () => {
    if (store.project === lastProject) return;
    lastProject = store.project;
    toolsLoaded = false;
    stopPoll();
    setChainState({ steps: [], running: false, runTs: '', savedOnly: true, needsApply: '', error: null, sourceTrack: null });
    if (pane && pane.classList.contains('active')) enterChain();
  });
}
