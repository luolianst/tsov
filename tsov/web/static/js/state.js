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

/* 乐句 segments（meta.segments 可选，ADR-0005 meta 自由字段） */
export function segments() {
  const m = store.score && store.score.meta;
  if (m && Array.isArray(m.segments)) return m.segments;
  return [];
}
