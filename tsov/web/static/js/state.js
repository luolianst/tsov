/* state.js —— 前端状态 store（不可变更新 + 订阅；选区/播放头/视图全在这里） */

import { bus } from './events.js';

export const KEYS_W = 56;   // 卷帘左侧钢琴键盘列宽（roll/timeline 共用）

export const store = {
  /* 工程 */
  project: null,
  projects: [],
  score: null,                 // Score JSON（ADR-0005 schema）
  summary: '',
  duration: 0,                 // M-V8 E6：总时长（含音频 clip 尾 +1s 释放；服务端 score_duration 口径）
  savedAt: null,               // M-V8 E6 段2：score.json 落盘时间（epoch 秒；顶栏保存徽章）
  gitLog: [],
  history: { can_undo: false, can_redo: false },

  /* 交互态 */
  selection: { track: 0, indices: [] },
  diff: null,                  // docs/05 §五 diff schema；null = 无叠层
  playhead: 0,
  playing: null,               // null | 'wav' | 'host'
  agentBusy: false,
  error: '',
  toast: '',

  /* 卷帘视图 */
  view: { pxPerSec: 90, pxPerSemi: 14, scrollSec: 0, midiTop: 84 },

  /* 视图模式（修正轮2）：'lanes' = 总谱预览（每轨 lane）| 'single' = 单轨写谱 */
  viewMode: 'lanes',
  singleTrack: 0,
  /* v0.2 批C2（工作台 v2）：副区对象（单值）——{kind:'track', ti} | {kind:'lane', ti, param} | null。
     手势族：单击=留而不切 ｜ Ctrl+单击=切且留 ｜ 双击=切不留（清副区）。 */
  partner: null,
  focus: 0,              // 工作台焦点轨（编辑作用对象；主/副二者之一）
  splitSameAxis: true,   // 副区 MIDI 音轴：true 同音轴（与主区同映射，可编辑）｜ false 自适配（紧凑概览，只读）
  refs: new Set(),       // 总谱态参照层（旧「灰叠加」仅存落点；单轨态不再渲染）
  splitRatio: 0.5,       // M-V8 E3 段1：单轨分屏上下比例（主轨上 / 副区下；0.15~0.85，可拖分界）

  /* 轨道可见性（Set<trackIndex>；空 = 全可见） */
  hiddenTracks: new Set(),

  /* 批B B1-3：agent 本次改动过的轨（改动高亮；空 = 无标记） */
  agentTracks: new Set(),

  /* M-V3：标注队列（[{ann:{index,action,value}, label}]，随下条对话发送）+ 写谱吸附比例（0=关/0.5=1/8/0.25=1/16） */
  pendingAnnotations: [],
  snapFrac: 0,

  /* M-V8 E1：定位工具包——循环区间 / 节拍器 / 书签选择 / 文件夹折叠（transport 态，不进谱） */
  loop: null,                  // {start, end} 秒；null = 无循环
  loopOn: false,               // 循环开关（🔁 / L）
  metronome: false,            // 节拍器开关（🥁）
  selFolder: '',               // 左栏选中文件夹（M 键作用域 + 高亮）
  collapsedFolders: new Set(), // 折叠的文件夹（lane 列表分组行）
  selBookmark: -1,             // 选中书签下标（score.bookmarks 索引；-1 = 无）
  collapsedLanes: new Set(),   // v0.2 C2（F4）：轨树道列表折叠的轨（Set<trackIndex>）

  /* M-V8 E5：编辑工具集（工具态 / 剪贴板 / 时间区间） */
  tool: 'smart',               // smart 智能指针 | range 范围 | scissors 剪刀 | glue 胶水 | eraser 橡皮
  clipboard: [],               // 音符剪贴板：[{pitch_midi,start,end,velocity,confidence}]（保留原绝对时间，粘贴按锚点平移）
  clipAnchor: 0,               // 剪贴板锚点（最小 start，秒）
  clipTrack: 0,                // 剪贴板来源轨（粘贴缺省目标参考）
  range: null,                 // {start, end} 秒：标尺区间 / 范围框选联动（供 E6 选段导出）

  /* M-V8 E5 段2：自动化道（单轨视图底部子道）与电平表 */
  autoLane: { open: false, param: 'volume' },   // param: 任意道 key（volume | pan | bend | …；目标恒为单轨视图当前轨）
  velocityLanes: new Set(),    // v0.2 批C 后段（挂道族）：开着力度道的轨（bars 伪道；零存储派生视图）
  meter: { l: 0, r: 0, hold_l: 0, hold_r: 0, clip: false },   // 试听通路实时电平（CDP 断言口）

  /* v0.2 批C 前段（R1/R2 地基件）：参数注册表快照（/api/meta.params；null = 未到 → 回退内置） */
  metaParams: null,

  /* M-V8 E3 段2：处理链（哼唱快车道）——前端镜像（真值 = 后端 ChainRunner / 工程 chain.json） */
  chain: {
    tools: [],                 // 工具 schema（/chain/tools：id/tier/params）
    steps: [],                 // 槽位状态（/chain/status：params/mute/status/artifact/stats）
    preset: 'humming-quicklane',
    sourceTrack: null,
    runTs: '',
    running: false,
    finished: false,
    savedOnly: true,
    needsApply: '',            // 慢档改参标脏（等「应用」）
    error: null,
  },

  /* M-V8 E4 段1：暂存区 + 工程上下文（AI 页签；真值 = 后端 staging + 工程伴生文件） */
  staging: {
    items: [],                 // /staging 列表（pending 前 + 已处置后）
    counts: { pending: 0, adopted: 0, discarded: 0 },
    agents: null,              // /agents 现状（project_md / user_md）
    loaded: false,
  },

  /* M-V8 E4 段2：AI 调参（事实包 / 建议批次 / 勾选 / 应用小结；真值 = 后端 tune/） */
  tune: {
    facts: null,               // 最近一次 analyze / suggest 内附事实快照
    batch: null,               // 展示中的批次（suggest / get 回填）
    selected: {},              // sid -> bool（默认全选）
    report: null,              // 最近一次应用小结（build_report）
    pack: '',                  // 风格包（'' = 通用平衡口径）
    busy: null,                // 'analyze' | 'suggest' | 'apply' | 'preview'
    loaded: false,
  },

  /* M-V8 E4 段3：AI 配器（风格包/强度 → 生成批次 → 试听/进工程；真值 = 后端 arrange/） */
  arrange: {
    packs: [],                 // /arrange/packs（有模式库的风格包）
    pack: '',                  // 选中风格包（生成必填）
    strength: 'standard',      // 密度档 light | standard | rich
    batch: null,               // 展示中的批次（generate / get 回填）
    busy: null,                // 'generate' | 'preview' | 'apply'
    loaded: false,
  },
};

export function setError(msg) {
  store.error = msg || '';
  bus.dispatch('error', store.error);
}

export function toast(msg) {
  store.toast = msg || '';
  bus.dispatch('toast', store.toast);
}

/* M-V7 D2（ADR-0019）：编辑不再逐条 commit——展示引用优先 commit（收藏点），否则窗口快照号 */
export function refTag(r) {
  if (!r) return '';
  if (r.commit) return '@' + String(r.commit).slice(0, 7);
  const s = r.seq;
  return (s === 0 || s) ? '@#' + s : '';
}

/* M-V7 D3：收藏 tag → 来源标注（手动 / 自动·迭代 / 自动·定时） */
export function favSource(tag) {
  const t = String(tag || '');
  if (t.endsWith('-auto')) return '自动·迭代';
  if (t.endsWith('-time')) return '自动·定时';
  return '手动';
}

/* 用 GET state / state_updated 事件的数据装载 store（保持选区不闪断） */
export function setState(s, opts) {
  const keep = (opts && opts.preserveSelection) ? store.selection : null;
  if (store.project && s.project !== store.project) { store.pendingAnnotations = []; bus.dispatch('annotations'); }
  store.project = s.project;
  store.score = s.score;
  store.summary = s.summary || '';
  store.duration = Number(s.duration) || 0;   /* M-V8 E6：含音频 clip 尾（fit/时长用） */
  store.savedAt = Number(s.saved_at) || null; /* M-V8 E6 段2：保存徽章（服务端 score.json mtime） */
  store.gitLog = s.git_log || [];
  store.history = s.history || { can_undo: false, can_redo: false };
  if (keep && store.score && store.score.tracks[keep.track]) {
    const n = store.score.tracks[keep.track].notes.length;
    store.selection = { track: keep.track, indices: keep.indices.filter((i) => i >= 0 && i < n) };
  } else {
    store.selection = { track: 0, indices: [] };
  }
  // 轨道可见性清理
  const nTracks = store.score ? store.score.tracks.length : 0;
  for (const t of Array.from(store.hiddenTracks)) if (t >= nTracks) store.hiddenTracks.delete(t);
  bus.dispatch('state');
}

export function setDiff(d) { store.diff = d; bus.dispatch('diff'); }
export function clearDiff() { store.diff = null; bus.dispatch('diff'); }   // 批B2：徽章/叠层统一由 store.diff 驱动（diff_applied → setDiff）；清除 = 置空

export function setSelection(track, indices) {
  store.selection = { track, indices: indices.slice() };
  bus.dispatch('selection');
}

export function setPlayhead(t) { store.playhead = t; bus.dispatch('playhead'); }
export function setPlaying(p) { store.playing = p; bus.dispatch('playing'); }
export function setAgentBusy(b) { store.agentBusy = !!b; bus.dispatch('agentbusy'); }

/* ---------------- M-V8 E1：定位 / 循环 / 节拍器 / 书签 / 文件夹 ---------------- */

export function setLoop(l) {
  const ok = l && Number.isFinite(l.start) && Number.isFinite(l.end) && l.end - l.start > 1e-3;
  store.loop = ok ? { start: Math.max(0, l.start), end: Math.max(0, l.end) } : null;
  bus.dispatch('markers');
}

export function setLoopOn(b) { store.loopOn = !!b; bus.dispatch('markers'); }
export function setMetronome(b) { store.metronome = !!b; bus.dispatch('markers'); }
export function setSelFolder(name) { store.selFolder = name || ''; bus.dispatch('markers'); }

export function toggleFolderCollapse(name) {
  if (!name) return false;
  if (store.collapsedFolders.has(name)) store.collapsedFolders.delete(name);
  else store.collapsedFolders.add(name);
  bus.dispatch('markers');
  return store.collapsedFolders.has(name);
}

export function setSelBookmark(i) {
  store.selBookmark = Number.isInteger(i) ? i : -1;
  bus.dispatch('markers');
}

/* ---------------- M-V8 E5：编辑工具集（工具 / 剪贴板 / 时间区间） ---------------- */

export const TOOL_NAMES = ['smart', 'range', 'scissors', 'glue', 'eraser'];

export function setTool(t) {
  store.tool = TOOL_NAMES.includes(t) ? t : 'smart';
  bus.dispatch('tool');
}

export function setClipboard(items, anchor, track) {
  const ok = Array.isArray(items) && items.length > 0;
  store.clipboard = ok ? items.map((n) => ({
    pitch_midi: n.pitch_midi, start: n.start, end: n.end,
    velocity: n.velocity != null ? n.velocity : 0.8,
    confidence: n.confidence != null ? n.confidence : 0.8,
  })) : [];
  store.clipAnchor = ok ? (Number(anchor) || 0) : 0;
  store.clipTrack = Number.isInteger(track) ? track : 0;
  bus.dispatch('clipboard');
}

export function setRange(r) {
  const ok = r && Number.isFinite(r.start) && Number.isFinite(r.end) && r.end - r.start > 1e-3;
  store.range = ok ? { start: Math.max(0, r.start), end: Math.max(0, r.end) } : null;
  bus.dispatch('tool');
}

/* M-V8 E5 段2 / v0.2 C2：自动化道栈开关与选中道（视图态，不进谱；数据写走命令层 set_automation）。
   param = 选中道 param（v0.2 C2 起为任意道 key；缺失 → 回 'volume' 兜底）。 */
export function setAutoLane(partial) {
  Object.assign(store.autoLane, partial || {});
  if (!store.autoLane.param || typeof store.autoLane.param !== 'string') store.autoLane.param = 'volume';
  bus.dispatch('auto');
}

/* v0.2 批C 后段（挂道族）：力度道开关（bars 伪道；视图态，不进谱；零存储——数据 = Note.velocity）。 */
export function toggleVelocityLane(ti) {
  const t = Math.max(0, ti | 0);
  if (store.velocityLanes.has(t)) store.velocityLanes.delete(t);
  else store.velocityLanes.add(t);
  bus.dispatch('auto');
  return store.velocityLanes.has(t);
}

export function hasVelocityLane(ti) {
  return store.velocityLanes.has(Math.max(0, ti | 0));
}

/* v0.2 批C 前段（R1/R2 地基件）：注册表快照接入 + 范围/标签查询。
   单一来源 = /api/meta.params（后端 host/params.py）；缺快照回退内置——渐进不翻车。
   前端不再有第三份硬编码参数值域（原 autoroll.js RANGE 已归并至此）。 */
const PARAM_FALLBACK = {
  automation: {
    volume: { label: '音量', lo: 0.0, hi: 2.0 },
    pan: { label: '声像', lo: -1.0, hi: 1.0 },
  },
  /* v0.2 批C 后段（挂道族）：perf 域兜底（label/范围；快照缺失时仍可用） */
  perf: {
    velocity: { label: '力度', lo: 0.0, hi: 1.0 },
    bend: { label: '弯音', lo: -1.0, hi: 1.0 },
    cc1: { label: '调制', lo: 0.0, hi: 1.0 },
    cc11: { label: '表情', lo: 0.0, hi: 1.0 },
    cc64: { label: '延音', lo: 0.0, hi: 1.0 },
  },
};

/* 快照查询（automation 区 = 曲线白名单（mix+perf 可绑）；perf 区兜底其余条目） */
function _metaSpec(pid) {
  const mp = store.metaParams;
  if (!mp) return null;
  return (mp.automation && mp.automation[pid]) || (mp.perf && mp.perf[pid]) || null;
}

function _fallbackSpec(pid) {
  return PARAM_FALLBACK.automation[pid] || PARAM_FALLBACK.perf[pid] || null;
}

export function setMetaParams(p) {
  store.metaParams = p || null;
  bus.dispatch('meta');
}

/** 自动化域参数范围 [lo, hi]（快照优先；缺快照回退内置）。 */
export function paramRange(pid) {
  const m = _metaSpec(pid);
  const f = _fallbackSpec(pid);
  const lo = (m && Number.isFinite(m.lo)) ? m.lo : (f ? f.lo : 0.0);
  const hi = (m && Number.isFinite(m.hi)) ? m.hi : (f ? f.hi : 1.0);
  return [lo, hi];
}

/** 自动化域参数中文名（快照优先；缺快照回退内置）。 */
export function paramLabel(pid) {
  const m = _metaSpec(pid);
  const f = _fallbackSpec(pid);
  return (m && m.label) || (f && f.label) || pid;
}

/* 高频直写（每帧；不 dispatch——UI 由 playback.js 直接刷 DOM，快照供 CDP 断言） */
export function setMeter(m) {
  store.meter = m;
}

/* M-V8 E3 段2：处理链状态镜像（批量并入 + 广播；渲染在 chain.js） */
export function setChainState(partial) {
  Object.assign(store.chain, partial || {});
  bus.dispatch('chain');
}

/* M-V8 E4 段1：暂存区/上下文镜像（批量并入 + 广播；渲染在 staging.js） */
export function setStagingState(partial) {
  Object.assign(store.staging, partial || {});
  bus.dispatch('staging');
}

/* M-V8 E4 段2：调参镜像（同上；渲染在 staging.js） */
export function setTuneState(partial) {
  Object.assign(store.tune, partial || {});
  bus.dispatch('tune');
}

/* M-V8 E4 段3：配器镜像（同上；渲染在 arrange.js/staging.js） */
export function setArrangeState(partial) {
  Object.assign(store.arrange, partial || {});
  bus.dispatch('arrange');
}

/** 当前谱内书签列表（只读引用；写走命令层）。 */
export function bookmarks() { return (store.score && store.score.bookmarks) || []; }

/** 文件夹名列表（按轨道出现顺序去重；folder 由轨道归属隐式定义）。 */
export function folders() {
  const out = [];
  for (const t of (store.score && store.score.tracks) || []) {
    if (t.folder && !out.includes(t.folder)) out.push(t.folder);
  }
  return out;
}

/** 某文件夹下的轨道下标列表。 */
export function folderTracks(name) {
  const out = [];
  ((store.score && store.score.tracks) || []).forEach((t, i) => { if (t.folder === name) out.push(i); });
  return out;
}

export function setView(partial) {
  Object.assign(store.view, partial);
  bus.dispatch('view');
}

/* ---------------- 视图模式（修正轮2：总谱 lane ↔ 单轨写谱） ---------------- */

export function setViewMode(mode, track) {
  const m = (mode === 'single') ? 'single' : 'lanes';
  store.viewMode = m;
  if (m === 'lanes') {
    store.singleTrack = 0;   /* E3 段1 修：回总谱/切工程 → 单轨主轨记忆清空（残留值曾静默挡住叠加） */
    store.partner = null;    /* v0.2 C2：副区是单轨态状态 → 回总谱清空 */
  }
  if (track != null && m === 'single') {
    store.singleTrack = Math.max(0, track | 0);
    store.focus = store.singleTrack;
    store.partner = null;    /* v0.2 C2：进入单轨 = 干净单屏；副区由手势再建（双击=切不留） */
  }
  bus.dispatch('viewmode');
}

export function setSingleTrack(ti) {
  store.singleTrack = Math.max(0, ti | 0);
  store.focus = store.singleTrack;
  if (store.partner && store.partner.kind === 'track' && store.partner.ti === store.singleTrack) store.partner = null;   /* 主轨不可同为轨道副区 */
  store.selection = { track: store.singleTrack, indices: [] };
  bus.dispatch('viewmode');
}

/* ---------------- v0.2 批C2（工作台 v2）：副区 / 焦点 / 手势族 ops ---------------- */

/** 该轨的道列表（只读规范形）：已管理（数组，含空列表=全失活）以此为准；未管理（null）派生自 automation dict。 */
export function trackLanes(tr) {
  if (!tr) return [];
  if (Array.isArray(tr.lanes)) {
    return tr.lanes.map((l) => ({ id: l.id || l.param, param: l.param, label: l.name || paramLabel(l.param) }));
  }
  const a = tr.automation || {};
  /* v0.2 批C 后段：未管理派生键 += perf 曲线（bend/cc1/cc11/cc64——旧工程零出现，纯 additive） */
  const known = (k) => k === 'volume' || k === 'pan' || k === 'bend' || k === 'cc1' || k === 'cc11' || k === 'cc64';
  return Object.keys(a).filter(known)
    .map((k) => ({ id: k, param: k, label: paramLabel(k) }));
}

/** 副区对象（校验后的规范形；供 roll/main 只读消费）：
    {kind:'midi'|'audio'|'lane', ti, param?} ｜ null */
export function partnerOf() {
  const p = store.partner;
  if (!p || !store.score || store.viewMode !== 'single') return null;
  const tr = store.score.tracks[p.ti];
  if (!tr) return null;
  if (p.kind === 'lane') {
    if (!trackLanes(tr).some((l) => l.param === p.param)) return null;
    return { kind: 'lane', ti: p.ti, param: p.param };
  }
  if (p.ti === store.singleTrack) return null;
  return { kind: tr.kind === 'audio' ? 'audio' : 'midi', ti: p.ti };
}

/** 单击（单轨）：留而不切——上副位 / 再点同一轨撤下 / 主轨无操作。 */
export function setPartner(ti) {
  const t = Math.max(0, ti | 0);
  if (store.viewMode !== 'single' || t === store.singleTrack) return;
  const raw = store.partner;
  if (raw && raw.kind === 'track' && raw.ti === t) store.partner = null;   /* 再点同一轨 = 撤下（比原始 partner，partnerOf 为归一化形） */
  else store.partner = { kind: 'track', ti: t };
  bus.dispatch('viewmode');
}

/** 道副区（单轨）：轨 ti 的道 param 入副区 / 再设同参撤下。 */
export function setPartnerLane(ti, param) {
  const t = Math.max(0, ti | 0);
  const p = String(param || '');
  if (store.viewMode !== 'single' || !p) return;
  const raw = store.partner;
  if (raw && raw.kind === 'lane' && raw.ti === t && raw.param === p) store.partner = null;
  else store.partner = { kind: 'lane', ti: t, param: p };
  bus.dispatch('viewmode');
}

export function clearPartner() {
  if (!store.partner) return;
  store.partner = null;
  if (store.focus !== store.singleTrack) store.focus = store.singleTrack;
  bus.dispatch('viewmode');
}

/** Ctrl+单击（单轨）：切且留——X 升主、原主降副（X 已是副位 → 与主对调）。 */
export function swapMainPartner(ti) {
  const t = Math.max(0, ti | 0);
  if (store.viewMode !== 'single' || t === store.singleTrack) return;
  const old = store.singleTrack;
  store.singleTrack = t;
  store.partner = { kind: 'track', ti: old };
  store.focus = t;
  store.selection = { track: t, indices: [] };
  bus.dispatch('viewmode');
}

/** 焦点轨（点谁编谁；仅主/副二位之内）。 */
export function setFocus(ti) {
  if (store.viewMode !== 'single') return;
  const t = Math.max(0, ti | 0);
  const p = partnerOf();
  const ok = (t === store.singleTrack) || !!(p && p.ti === t);
  if (!ok || store.focus === t) return;
  store.focus = t;
  bus.dispatch('viewmode');
}

/* 总谱态参照层（旧灰叠加遗留；仅总谱视图渲染） */
export function toggleRef(ti) {
  if (store.refs.has(ti)) store.refs.delete(ti);
  else store.refs.add(ti);
  bus.dispatch('viewmode');
  return store.refs.has(ti);
}
export function isRef(ti) { return store.refs.has(ti); }

/* ---------------- M-V8 E3 段1：单轨分屏（主轨上 / 副区下） ---------------- */

/* 分屏对象（工作台 v2 统一）：partnerOf() 非空即分屏；主轨↔副区各类型通吃。 */
export function splitPartner() { return partnerOf(); }

/** 副区音轴切换（同音轴 ↔ 自适配概览）。 */
export function setSplitSameAxis(on) {
  store.splitSameAxis = !!on;
  bus.dispatch('view');
}

export function setSplitRatio(r) {
  store.splitRatio = Math.max(0.15, Math.min(0.85, Number(r) || 0.5));
  bus.dispatch('view');
}

/** 道快照（__tsovState.lanes 用；CDP 断言面）：单轨主轨的道列表。 */
export function lanesState() {
  const single = store.viewMode === 'single';
  const tr = single && store.score ? store.score.tracks[store.singleTrack] : null;
  if (!tr) return null;
  const ls = trackLanes(tr);
  return { ti: store.singleTrack, count: ls.length, params: ls.map((l) => l.param) };
}

/** 文件夹层快照（只读；__tsovState.folders 用）。 */
export function foldersState() {
  const f = (store.score && store.score.folders) || {};
  const out = {};
  for (const k of Object.keys(f)) {
    const v = f[k];
    if (v && typeof v === 'object') out[k] = { volume: v.volume == null ? 1.0 : v.volume, mute: !!v.mute, solo: !!v.solo };
  }
  return out;
}

/** 工作台快照（__tsovState.single 用；CDP 断言面）。 */
export function singleState() {
  const single = store.viewMode === 'single';
  const p = single ? partnerOf() : null;
  return {
    main: single ? store.singleTrack : null,
    partner: p ? Object.assign({}, p) : null,
    focus: single ? store.focus : null,
    sameAxis: store.splitSameAxis,
  };
}

/* 分屏快照（__tsovState / roll / main 共用） */
export function splitState() {
  const sc = store.score;
  const single = store.viewMode === 'single';
  const main = single && sc ? sc.tracks[store.singleTrack] : null;
  const p = single ? partnerOf() : null;
  return {
    on: !!p,
    ratio: store.splitRatio,
    main: main ? store.singleTrack : null,
    mainKind: main ? ((main.kind === 'audio') ? 'audio' : 'midi') : null,
    partner: p ? p.ti : null,
    partnerKind: p ? p.kind : null,
    partnerParam: (p && p.kind === 'lane') ? p.param : null,
    focus: single ? store.focus : null,
    sameAxis: store.splitSameAxis,
  };
}

/* ---------------- 批B B1-3：agent 改动高亮 ---------------- */
export function setAgentTracks(indices) {
  store.agentTracks = new Set((indices || []).map(Number));
  bus.dispatch('agenttracks');
}
export function addAgentTracks(indices) {
  let changed = false;
  for (const i of indices || []) { const n = Number(i); if (!store.agentTracks.has(n)) { store.agentTracks.add(n); changed = true; } }
  if (changed) bus.dispatch('agenttracks');
}
export function clearAgentTracks() {
  if (!store.agentTracks.size) return;
  store.agentTracks = new Set();
  bus.dispatch('agenttracks');
}

/* 单轨视图：把纵向视图适配到该轨音域 */
export function fitViewTrack(ti, h) {
  const tr = store.score && store.score.tracks[ti];
  let lo = 127, hi = 0;
  if (tr) {
    for (const n of tr.notes) {
      if (n.pitch_midi < lo) lo = n.pitch_midi;
      if (n.pitch_midi > hi) hi = n.pitch_midi;
    }
  }
  if (lo > hi) { lo = 60; hi = 72; }
  const rows = Math.max(10, hi - lo + 5);
  const pxPerSemi = Math.min(24, Math.max(6, Math.floor((h - 4) / rows)));
  setView({ pxPerSemi, midiTop: hi + 3 });
}

/* M-V3：标注队列（选区操作挂起；随下条对话发送，后端确定性先行应用） */
export function pushAnnotations(items) {
  store.pendingAnnotations = store.pendingAnnotations.concat(items);
  bus.dispatch('annotations');
}
export function setAnnotations(items) {
  store.pendingAnnotations = items.slice();
  bus.dispatch('annotations');
}
export function clearAnnotations() {
  if (!store.pendingAnnotations.length) return;
  store.pendingAnnotations = [];
  bus.dispatch('annotations');
}
/* M-V3：写谱吸附（比例=拍；0 关 / 0.5 →1/8 / 0.25 →1/16） */
export function setSnap(frac) { store.snapFrac = Number(frac) || 0; bus.dispatch('view'); }

/* 音符域（全轨）：[minMidi, maxMidi] 与总时长 */
export function scoreBounds() {
  let lo = 127, hi = 0, tEnd = 0;
  if (store.score) {
    for (const tr of store.score.tracks) {
      for (const n of tr.notes) {
        if (n.pitch_midi < lo) lo = n.pitch_midi;
        if (n.pitch_midi > hi) hi = n.pitch_midi;
        if (n.end > tEnd) tEnd = n.end;
      }
    }
  }
  /* M-V8 E6：音频 clip 尾并入内容界（服务端 duration 含 +1s 释放尾 → 减回）——纯音频工程 fit/时长正确 */
  if (store.duration > 1) tEnd = Math.max(tEnd, store.duration - 1.0);
  if (lo > hi) { lo = 60; hi = 72; }
  return { lo, hi, tEnd };
}

/* 适配整谱（调用方给画布尺寸） */
export function fitView(w, h) {
  const b = scoreBounds();
  const rows = Math.max(10, b.hi - b.lo + 5);
  const pxPerSemi = Math.min(24, Math.max(6, Math.floor((h - 4) / rows)));
  const pxPerSec = Math.min(400, Math.max(20, (w - KEYS_W - 24) / Math.max(b.tEnd, 1) || 90));
  setView({ pxPerSec, pxPerSemi, scrollSec: 0, midiTop: b.hi + 3 });
}

export function tempo() { return (store.score && store.score.tempo) || 120; }

/* 拍号（M-V6）：Score.time_signature（"6/8"）→ [本数, 分母]；缺省 4/4 */
export function timeSig() {
  const m = /^\s*(\d{1,2})\s*\/\s*(\d{1,2})\s*$/.exec((store.score && store.score.time_signature) || '4/4');
  if (!m) return [4, 4];
  const n = parseInt(m[1], 10), d = parseInt(m[2], 10);
  if (!n || !d) return [4, 4];
  return [n, d];
}

/* 每小节拍数（以四分音符为单位：4/4→4，6/8→3，3/4→3）——卷帘/标尺画小节线用（原来写死 4/4） */
export function beatsPerBar() {
  const [n, d] = timeSig();
  return (4 * n) / d;
}

/* 乐句 segments（meta.segments 可选，ADR-0005 meta 自由字段） */
export function segments() {
  const m = store.score && store.score.meta;
  if (m && Array.isArray(m.segments)) return m.segments;
  return [];
}

/* ---------------- 批B B1-4：用户操作缓冲（回流 agent 上下文） ----------------
   手动改动经命令层成功落盘后记一条（api.postBatch 钩子），随下条消息发送一次、发送后清空。 */
const userActions = [];

export function noteUserAction(text) {
  const t = String(text || '').trim();
  if (!t) return;
  userActions.push(t);
  if (userActions.length > 20) userActions.shift();   // 缓冲上限（Q44 弱留存窗口原型）
}

export function peekUserActions() { return userActions.slice(-10); }
export function clearUserActions() { userActions.length = 0; }

/* ---------------- M-V8 E6 段1：音频 clip 规范化（与后端读时迁移同口径） ----------------
   旧形态 {file, offset} → 单 clip；id 缺失 = ''（提交时由服务端补；旧轨道 id 稳定后随状态回填）。 */
export function audioClipsOf(trk) {
  const a = (trk && trk.audio) || {};
  if (Array.isArray(a.clips)) {
    return a.clips.map((c) => ({
      clip_id: c.clip_id || '',
      file: c.file || '',
      start: +c.start || 0,
      src_offset: +c.src_offset || 0,
      src_len: (c.src_len == null) ? null : +c.src_len,
      stretch: (c.stretch == null) ? 1 : +c.stretch,
      fade_in: +c.fade_in || 0,
      fade_out: +c.fade_out || 0,
    }));
  }
  if (a.file) {
    return [{
      clip_id: '', file: a.file, start: +a.offset || 0, src_offset: 0,
      src_len: null, stretch: 1, fade_in: 0, fade_out: 0,
    }];
  }
  return [];
}

/* ---------------- 批B（ADR-0017）：只读可编程面 ----------------
   window.__tsovState() —— 返回当前前端状态快照（只读，调用即取新值）。
   用途：CDP 实测断言 / 外部 agent（MCP 线）观察宿主状态；不提供写入口（写走命令层）。 */
export function snapshot() {
  return {
    project: store.project,
    score: store.score,               // ADR-0005 schema（大对象，按需取字段）
    summary: store.summary,
    duration: store.duration || 0,    /* M-V8 E6：总时长（含音频尾，服务端口径） */
    savedAt: store.savedAt || null,   /* M-V8 E6 段2：落盘时间（CDP 断言保存徽章用） */
    gitLog: store.gitLog,
    history: store.history,
    selection: { track: store.selection.track, indices: store.selection.indices.slice() },
    viewMode: store.viewMode,
    singleTrack: store.singleTrack,
    partner: store.partner ? Object.assign({}, store.partner) : null,
    focus: store.focus,
    refs: Array.from(store.refs),
    hiddenTracks: Array.from(store.hiddenTracks),
    agentTracks: Array.from(store.agentTracks),
    pendingUserActions: userActions.slice(),
    view: Object.assign({}, store.view),
    snapFrac: store.snapFrac,
    pendingAnnotations: store.pendingAnnotations.slice(),
    agentBusy: store.agentBusy,
    playing: store.playing,
    playhead: store.playhead,
    /* M-V8 E1：定位工具包快照（CDP 断言用） */
    loop: store.loop ? Object.assign({}, store.loop) : null,
    loopOn: store.loopOn,
    metronome: store.metronome,
    selFolder: store.selFolder,
    collapsedFolders: Array.from(store.collapsedFolders),
    selBookmark: store.selBookmark,
    /* M-V8 E5：编辑工具集快照（CDP 断言用） */
    tool: store.tool,
    clipboard: store.clipboard.length,
    clipTrack: store.clipTrack,
    range: store.range ? Object.assign({}, store.range) : null,
    lanesScroll: store.lanesScroll || 0,
    laneRows: store.laneRows || null,   /* UI 修正轮3.2：左栏行布局表（文件夹行/折叠对齐观测） */
    split: splitState(),   /* M-V8 E3 段1：单轨分屏快照（CDP 断言用） */
    single: singleState(),   /* v0.2 批C2（工作台 v2）：{main, partner, focus, sameAxis}（CDP 断言用） */
    lanes: lanesState(),   /* v0.2 C2（F4）：单轨主轨道列表 {ti, count, params[]}（CDP 断言用） */
    folders: foldersState(),   /* v0.2 C2（F6）：文件夹层（volume/mute/solo；CDP 断言用） */
    /* M-V8 E5 段2：自动化道 / 电平表快照（CDP 断言用）；v0.2 批C 前段：+ range（注册表快照同源） */
    automation: {
      open: store.autoLane.open,
      param: store.autoLane.param,
      range: { volume: paramRange('volume'), pan: paramRange('pan') },
    },
    perf: {
      /* v0.2 批C 后段（挂道族）：力度道开着的轨（bars 伪道；CDP 断言用） */
      velocity: Array.from(store.velocityLanes),
    },
    /* M-V8 E6 段1：音频轨 clip 规范快照（CDP 断言用；旧 {file, offset} → 单 clip 同口径） */
    audio: (store.score && Array.isArray(store.score.tracks) ? store.score.tracks : [])
      .map((t, i) => ((t && t.kind === 'audio') ? { ti: i, name: t.name || '', clips: audioClipsOf(t) } : null))
      .filter(Boolean),
    meter: Object.assign({}, store.meter),
    /* M-V8 E3 段2：处理链快照（CDP 断言用；steps 浅拷贝防外部持引用） */
    chain: {
      preset: store.chain.preset,
      sourceTrack: store.chain.sourceTrack,
      runTs: store.chain.runTs,
      running: store.chain.running,
      finished: store.chain.finished,
      savedOnly: store.chain.savedOnly,
      needsApply: store.chain.needsApply,
      error: store.chain.error,
      toolIds: store.chain.tools.map((t) => t.id),
      steps: store.chain.steps.map((s) => Object.assign({}, s)),
    },
    /* M-V8 E4 段1：暂存区快照（CDP 断言用；items 浅拷贝防外部持引用） */
    staging: {
      counts: Object.assign({}, store.staging.counts),
      items: store.staging.items.map((x) => ({ id: x.id, state: x.state, producer: x.producer,
                                               ready: !!x.ready, title: x.title })),
      agents: store.staging.agents
        ? { hasProject: !!store.staging.agents.project_md, hasUser: !!store.staging.agents.user_md }
        : null,
      loaded: !!store.staging.loaded,
    },
    /* M-V8 E4 段2：调参快照（CDP 断言用） */
    tune: {
      loaded: !!store.tune.loaded,
      busy: store.tune.busy || null,
      pack: store.tune.pack || '',
      batchTs: (store.tune.batch && store.tune.batch.batch_ts) || null,
      batchState: (store.tune.batch && store.tune.batch.state) || null,
      nSuggestions: ((store.tune.batch && store.tune.batch.suggestions) || []).length,
      selected: Object.keys(store.tune.selected || {}).filter((k) => store.tune.selected[k]).length,
      hasFacts: !!store.tune.facts,
      reportStatus: (store.tune.report && store.tune.report.status) || null,
    },
    /* M-V8 E4 段3：配器快照（CDP 断言用） */
    arrange: {
      loaded: !!store.arrange.loaded,
      busy: store.arrange.busy || null,
      pack: store.arrange.pack || '',
      strength: store.arrange.strength || 'standard',
      nPacks: (store.arrange.packs || []).length,
      batchTs: (store.arrange.batch && store.arrange.batch.batch_ts) || null,
      batchState: (store.arrange.batch && store.arrange.batch.state) || null,
      nTracks: ((store.arrange.batch && store.arrange.batch.tracks) || []).length,
      nNotes: ((store.arrange.batch && store.arrange.batch.stats) || {}).notes || 0,
      source: ((store.arrange.batch && store.arrange.batch.stats) || {}).source || null,
    },
    hasDiff: !!store.diff,
    /* 对话产物流 B 件：步骤流快照（对话内嵌轮聚合；CDP 断言用） */
    steps: store.steps
      ? { count: store.steps.count || 0,
          rounds: (store.steps.rounds || []).map((r) => Object.assign({}, r,
            { stats: Object.assign({}, r.stats), seqs: (r.seqs || []).slice() })),
          /* C 件：步骤视图快照（服务端合流 feed 计数 + DOM 实测计数；CDP 断言用） */
          feed: store.steps.feed ? Object.assign({}, store.steps.feed) : null }
      : null,
    error: store.error,
  };
}

if (typeof window !== 'undefined') window.__tsovState = snapshot;
