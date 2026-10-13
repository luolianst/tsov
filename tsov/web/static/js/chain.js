/* chain.js —— 处理链面板（M-V8 E3 段2；09-26 卡片化：横排卡片 + › 连接）：哼唱快车道 5 工具槽位
   - 状态真值在后端（ChainRunner + 工程 chain.json）；前端镜像 store.chain
   - REST：tools / status / run / config / cancel / artifact（ADR-0017：与 agent 同一动作路径）
   - 快档改参 → config 返回 plan.auto → 自动顺跑；慢档 → needs_apply 亮「应用」
   - 运行中轮询 status（600ms）；结束即停表
   - __tsovState().chain 暴露给 CDP 断言 */
import { t } from './i18n.js';

import { bus } from './events.js';
import { api } from './api.js';
import { store, setError, setChainState } from './state.js';

let pane = null;
let pollTimer = null;
let toolsLoaded = false;
let busy = false;
let lastProject = null;
let refreshing = false;

const STATUS_TEXT = { pending: t('待跑'), running: t('运行中'), done: t('完成'), failed: t('失败'), skipped: t('跳过'), cancelled: t('已取消'), dirty: t('待应用') };

function el(tag, cls, text) {
  const d = document.createElement(tag);
  if (cls) d.className = cls;
  if (text != null) d.textContent = text;
  return d;
}

function finalText(steps) {
  const cnt = (x) => steps.filter((i) => i.status === x).length;
  const parts = [];
  if (cnt('done')) parts.push(t('完成 ') + cnt('done'));
  if (cnt('skipped')) parts.push(t('跳过 ') + cnt('skipped'));
  if (cnt('failed')) parts.push(t('失败 ') + cnt('failed'));
  if (cnt('cancelled')) parts.push(t('取消 ') + cnt('cancelled'));
  return parts.join(' · ') || t('无变化');
}

async function ensureTools() {
  if (toolsLoaded || !store.project) return;
  try {
    const r = await api.chainTools(store.project);
    toolsLoaded = true;
    setChainState({ tools: r.tools || [], preset: r.preset || store.chain.preset,
                    presets: r.presets || store.chain.presets });
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
    if (prevRunning && !st.running) bus.dispatch('toast', t('处理链结束：') + finalText(store.chain.steps));
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
  if (!store.project) { setError(t('先打开一个工程')); return; }
  busy = true;
  try {
    const body = {};
    if (steps && steps.length) body.steps = steps;
    const r = await api.chainRun(store.project, body);
    setChainState({ running: !!r.running, needsApply: '', steps: r.steps || store.chain.steps });
    bus.dispatch('toast', t('链已开跑（') + (steps && steps.length ? steps.join(' → ') : t('全链')) + t('）'));
    ensurePoll();
  } catch (e) {
    setError(t('链运行：') + e.message);
  } finally {
    busy = false;
  }
}

async function doCancel() {
  if (!store.project) return;
  try {
    await api.chainCancel(store.project);
    bus.dispatch('toast', t('已请求取消（步边界生效；已完成产物保留）'));
  } catch (e) { setError(t('取消：') + e.message); }
}

async function changeParam(idx, key, val) {
  if (!store.project) return;
  try {
    /* 段2：步序寻址（step_overrides）——重复工具（transcribe×4…）精确到步 */
    const r = await api.chainConfig(store.project, { step_overrides: { [idx]: { [key]: val } } });
    const plan = r.plan || {};
    if (plan.auto && plan.auto.length) {
      await doRun(plan.auto);                       // 快档：从脏步起自动顺跑（Q2；步序子集）
    } else if (plan.needs_apply != null) {
      setChainState({ needsApply: plan.needs_apply });
      bus.dispatch('toast', t('慢档参数已存：点该步「应用」重跑'));
    } else {
      await refreshChain();
    }
  } catch (e) { setError(t('参数保存：') + e.message); }
}

async function toggleMute(idx, mute) {
  if (!store.project) return;
  try {
    await api.chainConfig(store.project, { step_mute: { [idx]: mute } });
    await refreshChain();
  } catch (e) { setError('mute：' + e.message); }
}

/* E1-6：链预设切换（存 chain.json → 面板按新预设重渲染；运行中禁切） */
async function changePreset(presetId) {
  if (!store.project || store.chain.running || busy) return;
  if (!presetId || presetId === store.chain.preset) return;
  try {
    await api.chainConfig(store.project, { preset: presetId });
    setChainState({ preset: presetId, needsApply: '', steps: [] });
    await refreshChain();
    renderChain();
    bus.dispatch('toast', t('链预设已切换：') + presetId);
  } catch (e) { setError(t('预设切换：') + e.message); }
}

function restIds(fromIdx) {
  /* 段2：返回**步序**子集（重复工具靠步序定位；服务端 run.steps 收 int） */
  return store.chain.steps.slice(fromIdx).map((_, i) => fromIdx + i);
}

/* 段3：失败恢复——读取该步输入框当前参数 → 保存（chain.json）→ 从该步起重跑 */
async function rerunWithParams(s, idx) {
  if (busy || store.chain.running || !store.project) return;
  const tool = store.chain.tools.find((t) => t.id === s.tool_id);
  const specs = (tool && tool.params) || {};
  const p = {};
  const row = document.querySelector('.chain-step[data-idx="' + idx + '"]');
  if (row) {
    row.querySelectorAll('[data-param]').forEach((ctl) => {
      const spec = specs[ctl.dataset.param] || {};
      p[ctl.dataset.param] = (spec.type === 'number') ? Number(ctl.value) : ctl.value;
    });
  }
  if (!Object.keys(p).length) p = null;   // 无输入框：退回「重试」语义
  try {
    if (p) await api.chainConfig(store.project, { step_overrides: { [idx]: p } });
  } catch (e) {
    setError(t('参数保存：') + e.message);
    return;
  }
  doRun(restIds(idx));
}

/* 段3：链产物进工程——REST → 命令层事务；SSE state_updated 自动刷新轨列表。
   哼唱=单轨处理版；拆解（E1-5）=文件夹落位（toast 汇总 folder/轨道数/tempo）。 */
async function doApply() {
  if (busy || store.chain.running || !store.project) return;
  busy = true;
  try {
    const r = await api.chainApply(store.project, {});
    if (r.folder) {
      const nA = (r.audio_tracks || []).length;
      const nM = (r.midi_tracks || []).length;
      let msg = t('已落位「') + r.folder + '」：' + nA + t(' 音频轨') + (nM ? ' + ' + nM + t(' MIDI 轨') : '');
      if (r.tempo && r.tempo.to != null) {
        msg += '；tempo ' + r.tempo.from + ' → ' + r.tempo.to + (r.tempo.applied ? '' : t('（未生效）'));
      }
      bus.dispatch('toast', msg);
    } else {
      bus.dispatch('toast', t('已进工程：') + (r.names || []).join(' + ')
        + t('（轨 ') + (r.tracks != null ? r.tracks : '?') + t('，命令 ') + (r.applied || 0) + t('）'));
    }
  } catch (e) {
    setError(t('进工程：') + e.message);
  } finally {
    busy = false;
  }
}

function renderStep(s, idx) {
  const card = el('div', 'chain-step ' + (s.status || 'pending'));
  card.dataset.tool = s.tool_id;
  card.dataset.idx = String(idx);

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
      ? t('调性：auto=自动检测；或形如 C major / a minor / D dorian')
      : ((tool && tool.desc && (tool.desc.zh || tool.desc.en)) || '');
    ctl.dataset.param = key;
    ctl.addEventListener('change', () => {
      const val = (spec.type === 'number') ? Number(ctl.value) : ctl.value;
      changeParam(idx, key, val);
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
    const err = el('span', 'chain-err', s.error || t('失败'));
    err.title = s.error || '';
    card.appendChild(err);
  }

  /* 卡脚：动作 + 产物 */
  const ft = el('div', 'chain-ft');
  const mb = el('button', 'chain-btn chain-mute' + (s.mute ? ' on' : ''), s.mute ? t('已静音') : t('静音'));
  mb.title = t('mute：本步跳过（存 chain.json，可撤销式重跑）');
  mb.onclick = () => toggleMute(idx, !s.mute);
  ft.appendChild(mb);

  if (s.status === 'running') ft.appendChild(el('span', 'chain-sub', t('运行中…')));

  if (s.status === 'failed') {
    const retry = el('button', 'chain-btn', t('重试'));
    retry.title = t('从该步起重跑（输入用上一轮产物，免重跑上游）');
    retry.onclick = () => doRun(restIds(idx));
    const rerun = el('button', 'chain-btn', t('改参再跑'));
    rerun.title = t('读取本步当前参数并保存，从该步起重跑（先在上方输入框改好参数）');
    rerun.onclick = () => rerunWithParams(s, idx);
    const skip = el('button', 'chain-btn', t('跳过'));
    skip.title = t('静音该步并从其后继续');
    skip.onclick = () => toggleMute(idx, true).then(() => doRun(restIds(idx + 1)));
    ft.appendChild(retry);
    ft.appendChild(rerun);
    ft.appendChild(skip);
  }

  /* 慢档「应用」（needsApply → 从该步起跑）；段2：needsApply 为步序 */
  if (tool && tool.tier === 'slow' && store.chain.needsApply != null
      && String(store.chain.needsApply) === String(idx)) {
    const ap = el('button', 'chain-btn chain-apply', t('应用'));
    ap.title = t('从该步起重跑（含下游）');
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
    a.title = t('打开产物（wav 直接试听；json 下载）');
    ft.appendChild(a);
  }

  card.appendChild(ft);
  return card;
}

let lastSig = '';   /* 渲染签名（10-12 深夜）：轮询节流 + 防横滚重置 */

export function renderChain() {
  if (!pane) return;
  /* 600ms 轮询每拍都进这里；全量重建会把 .chain-row 的横滚位置清零——
     签名不变 → 跳过重建；变化时重建也保留横滚/纵滚位置。 */
  const sig = JSON.stringify([store.chain.steps, store.chain.running, store.chain.runTs,
    store.chain.preset, store.chain.presets, store.chain.sourceTrack, store.chain.error,
    store.chain.tools, store.chain.needsApply, busy]);
  if (sig === lastSig) return;
  lastSig = sig;
  const oldRow = pane.querySelector('.chain-row');
  const oldWrap = pane.querySelector('.chain-wrap');
  const keep = { row: oldRow ? oldRow.scrollLeft : 0, wrap: oldWrap ? oldWrap.scrollTop : 0 };
  pane.innerHTML = '';
  if (!store.project) { pane.innerHTML = t('<div class="fx-none">先打开一个工程</div>'); return; }
  const wrap = el('div', 'chain-wrap');

  const head = el('div', 'chain-head');
  const presetMeta = (store.chain.presets || []).find((p) => p.id === store.chain.preset) || null;
  const title = el('span', 'chain-title',
    (presetMeta && presetMeta.name && (presetMeta.name.zh || presetMeta.name.en)) || t('处理链'));
  title.title = (presetMeta && presetMeta.description
    && (presetMeta.description.zh || presetMeta.description.en))
    || t('链 = 预设里的工具序列 + 参数；快档改参即自动顺跑，慢档改参后点「应用」｜静音=跳过该步｜产物在 chain/<运行时间戳>/');
  head.appendChild(title);

  /* E1-6：预设选择器（切换 → 存 chain.json → 面板按新预设重渲染；运行中禁切） */
  const presetSel = document.createElement('select');
  presetSel.id = 'chain-preset-select';
  presetSel.className = 'chain-preset-select';
  presetSel.title = store.chain.running ? t('运行中不可切换预设') : t('链预设（哼唱快车道 / 成品歌拆解）');
  for (const p of (store.chain.presets || [])) {
    const o = document.createElement('option');
    o.value = p.id;
    o.textContent = (p.name && (p.name.zh || p.name.en)) || p.id;
    presetSel.appendChild(o);
  }
  presetSel.value = store.chain.preset;
  presetSel.disabled = store.chain.running || busy;
  presetSel.addEventListener('change', () => changePreset(presetSel.value));
  head.appendChild(presetSel);

  const runAll = el('button', 'chain-btn chain-run', store.chain.running ? t('运行中…') : t('▶ 运行全链'));
  runAll.disabled = store.chain.running || busy;
  runAll.onclick = () => doRun(null);
  head.appendChild(runAll);

  const cancel = el('button', 'chain-btn', t('■ 取消'));
  cancel.disabled = !store.chain.running;
  cancel.onclick = doCancel;
  head.appendChild(cancel);

  const toProj = el('button', 'chain-btn chain-to-proj', t('⇥ 进工程'));
  toProj.disabled = store.chain.running || busy || !store.chain.runTs;
  toProj.title = (store.chain.preset === 'song-decompose')
    ? t('把最近一次拆解落成「拆解 · <源轨名>」文件夹（4 音频轨 + 转录 MIDI，并写入网格 tempo）；重跑幂等；undo 可回')
    : t('把最近一次链产物落成一条「处理版」MIDI 轨（量化 + 调内吸附），插在源轨正下方；')
      + t('重跑幂等（先删旧同名轨、含旧版双轨遗留）；undo 可回');
  toProj.onclick = doApply;
  head.appendChild(toProj);

  head.appendChild(el('span', 'chain-sub',
    store.chain.running ? t('运行中…') : (store.chain.runTs ? (t('最近运行 ') + store.chain.runTs) : t('未运行'))));
  /* E1-7（P50）：显示当前应用轨道（缺省=第一条音频轨，标注「自动」） */
  {
    const tracks = (store.score && store.score.tracks) || [];
    let idx = store.chain.sourceTrack;
    let auto = false;
    if (idx == null) {
      idx = tracks.findIndex((t) => t.kind === 'audio');
      auto = true;
    }
    if (typeof idx === 'number' && idx >= 0 && tracks[idx]) {
      head.appendChild(el('span', 'chain-sub chain-applied',
        t('应用到：轨 ') + (idx + 1) + ' · ' + (tracks[idx].name || t('未命名')) + (auto ? t('（自动）') : '')));
    }
  }
  if (store.chain.error) head.appendChild(el('span', 'chain-err', store.chain.error));
  wrap.appendChild(head);

  if (!store.chain.steps.length) {
    wrap.appendChild(el('div', 'fx-none', t('链未初始化（打开工程后自动取 5 步槽位）')));
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
  const newRow = pane.querySelector('.chain-row');
  if (newRow) newRow.scrollLeft = keep.row;   /* 重建后恢复横滚位置（10-12 深夜：防轮询重置） */
  wrap.scrollTop = keep.wrap;
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
    lastSig = '';   /* 切工程：签名失效，强制下一次重建 */
    stopPoll();
    setChainState({ steps: [], running: false, runTs: '', savedOnly: true, needsApply: '', error: null, sourceTrack: null });
    if (pane && pane.classList.contains('active')) enterChain();
  });
}
