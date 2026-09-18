/* state.js —— 前端状态 store（不可变更新 + 订阅；选区/播放头/视图全在这里） */

import { bus } from './events.js';

export const KEYS_W = 56;   // 卷帘左侧钢琴键盘列宽（roll/timeline 共用）

export const store = {
  /* 工程 */
  project: null,
  projects: [],
  score: null,                 // Score JSON（ADR-0005 schema）
  summary: '',
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
  overlayTracks: new Set(),   // single 模式灰叠加轨（不可编辑）

  /* 轨道可见性（Set<trackIndex>；空 = 全可见） */
  hiddenTracks: new Set(),

  /* M-V3：标注队列（[{ann:{index,action,value}, label}]，随下条对话发送）+ 写谱吸附比例（0=关/0.5=1/8/0.25=1/16） */
  pendingAnnotations: [],
  snapFrac: 0,
};

export function setError(msg) {
  store.error = msg || '';
  bus.dispatch('error', store.error);
}

export function toast(msg) {
  store.toast = msg || '';
  bus.dispatch('toast', store.toast);
}

/* 用 GET state / state_updated 事件的数据装载 store（保持选区不闪断） */
export function setState(s, opts) {
  const keep = (opts && opts.preserveSelection) ? store.selection : null;
  if (store.project && s.project !== store.project) { store.pendingAnnotations = []; bus.dispatch('annotations'); }
  store.project = s.project;
  store.score = s.score;
  store.summary = s.summary || '';
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
export function clearDiff() { store.diff = null; bus.dispatch('diff'); }   // 审计修 M-V2.3：徽章由 diff_applied 直写 DOM，不能靠 store.diff 判空

export function setSelection(track, indices) {
  store.selection = { track, indices: indices.slice() };
  bus.dispatch('selection');
}

export function setPlayhead(t) { store.playhead = t; bus.dispatch('playhead'); }
export function setPlaying(p) { store.playing = p; bus.dispatch('playing'); }
export function setAgentBusy(b) { store.agentBusy = !!b; bus.dispatch('agentbusy'); }

export function setView(partial) {
  Object.assign(store.view, partial);
  bus.dispatch('view');
}

/* ---------------- 视图模式（修正轮2：总谱 lane ↔ 单轨写谱） ---------------- */

export function setViewMode(mode, track) {
  const m = (mode === 'single') ? 'single' : 'lanes';
  if (m === 'lanes') {
    store.overlayTracks = new Set();
  }
  store.viewMode = m;
  if (track != null) {
    store.singleTrack = Math.max(0, track | 0);
    store.overlayTracks.delete(store.singleTrack);
  }
  bus.dispatch('viewmode');
}

export function setSingleTrack(ti) {
  store.singleTrack = Math.max(0, ti | 0);
  store.overlayTracks.delete(store.singleTrack);
  store.selection = { track: store.singleTrack, indices: [] };
  bus.dispatch('viewmode');
}

/* 灰叠加开关（返回是否已成为叠加） */
export function toggleOverlay(ti) {
  if (ti === store.singleTrack) return false;
  if (store.overlayTracks.has(ti)) store.overlayTracks.delete(ti);
  else store.overlayTracks.add(ti);
  bus.dispatch('viewmode');
  return store.overlayTracks.has(ti);
}

export function isOverlay(ti) { return store.overlayTracks.has(ti); }

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

/* ---------------- 批B（ADR-0017）：只读可编程面 ----------------
   window.__tsovState() —— 返回当前前端状态快照（只读，调用即取新值）。
   用途：CDP 实测断言 / 外部 agent（MCP 线）观察宿主状态；不提供写入口（写走命令层）。 */
export function snapshot() {
  return {
    project: store.project,
    score: store.score,               // ADR-0005 schema（大对象，按需取字段）
    summary: store.summary,
    gitLog: store.gitLog,
    history: store.history,
    selection: { track: store.selection.track, indices: store.selection.indices.slice() },
    viewMode: store.viewMode,
    singleTrack: store.singleTrack,
    overlayTracks: Array.from(store.overlayTracks),
    hiddenTracks: Array.from(store.hiddenTracks),
    view: Object.assign({}, store.view),
    snapFrac: store.snapFrac,
    pendingAnnotations: store.pendingAnnotations.slice(),
    agentBusy: store.agentBusy,
    playing: store.playing,
    playhead: store.playhead,
    hasDiff: !!store.diff,
    error: store.error,
  };
}

if (typeof window !== 'undefined') window.__tsovState = snapshot;
