/* diff.js —— diff 数据 → 卷帘叠层着色（绿=added、红=removed 残影、黄=changed 新旧对照） */

import { store } from './state.js';
import { bus } from './events.js';

const TOL = 0.03;   // 与后端 diff_notes tol_s 同量级（前端仅用于把 diff 条目关联回当前音符）

function keyOf(n) { return Math.round(n.start * 1000) + '|' + n.pitch_midi; }

/* 返回当前谱 track0 各音符的分类：'add' | 'mod' | null + 旧位置残影清单 */
export function diffLayers() {
  const d = store.diff;
  if (!d || !store.score || !store.score.tracks.length) return null;

  const addedKeys = new Set((d.added || []).map(keyOf));
  const changedOld = new Map();   // newKey -> old note（画残影）
  const changedNewKeys = new Set();
  for (const pair of d.changed || []) {
    const k = keyOf(pair.new);
    changedNewKeys.add(k);
    changedOld.set(k, pair.old);
  }

  const classify = new Map();     // key -> 'add' | 'mod'
  for (const k of addedKeys) classify.set(k, 'add');
  for (const k of changedNewKeys) classify.set(k, 'mod');

  return {
    classify,
    removed: (d.removed || []).slice(),             // 红残影（不在当前谱里）
    ghostOf(n) {
      const k = keyOf(n);
      return changedOld.has(k) ? changedOld.get(k) : null;   // 黄对照的旧音
    },
    summary: d.summary || '',
    total: d.total || 0,
  };
}

export function badgeHtml(d) {
  if (!d || !d.total) return '';
  const parts = [];
  if (d.added && d.added.length) parts.push('<span class="badge-add">+' + d.added.length + '</span>');
  if (d.removed && d.removed.length) parts.push('<span class="badge-del">-' + d.removed.length + '</span>');
  if (d.changed && d.changed.length) parts.push('<span class="badge-mod">~' + d.changed.length + '</span>');
  if (d.commit) parts.push(' <span class="muted">@' + String(d.commit).slice(0, 7) + '</span>');
  return parts.length ? 'diff：' + parts.join(' ') : '';
}

/* 状态栏徽章（订阅 diff 主题） */
export function initDiffBadge(el) {
  bus.on('diff', () => { el.innerHTML = badgeHtml(store.diff); });
}
