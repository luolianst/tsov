/* segbar.js —— 全局段轨（段落 / 和弦，只读；UI 批A）
   时间映射与卷帘一致：行容器左起 = KEYS_W + (t - scrollSec) * pxPerSec */

import { bus } from './events.js';
import { KEYS_W, store, segments } from './state.js';

let rowsEl = null;

/* 行内局部 x：与 roll.canvas 的 xOf 同源（容器已含 KEYS_W 的左内边距） */
function xl(t) { return (t - store.view.scrollSec) * store.view.pxPerSec; }

function clip(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

function render() {
  if (!rowsEl) return;
  rowsEl.innerHTML = '';
  const W = rowsEl.clientWidth;   // 含左 padding（KEYS_W）

  /* ---- 段落行 ---- */
  const row1 = document.createElement('div');
  row1.className = 'seg-row';
  const segs = segments();
  if (segs.length) {
    for (const s of segs) {
      const x0 = xl(s.start), x1 = xl(s.end);
      if (x1 < 0 || x0 > W) continue;
      const a = clip(x0, 0, W), b = clip(x1, 0, W);
      if (b - a < 2) continue;
      const d = document.createElement('div');
      d.className = 'seg-span';
      d.style.left = a + 'px';
      d.style.width = (b - a) + 'px';
      d.textContent = s.label || s.type || '';
      d.title = (s.label || s.type || '') + '（' + s.start.toFixed(2) + '–' + s.end.toFixed(2) + 's）';
      row1.appendChild(d);
    }
    if (!row1.children.length) {
      const e = document.createElement('span');
      e.className = 'seg-empty';
      e.textContent = '（本可视区内无段落）';
      row1.appendChild(e);
    }
  } else {
    const e = document.createElement('span');
    e.className = 'seg-empty';
    e.textContent = store.score ? '（无段落数据）' : '';
    row1.appendChild(e);
  }

  /* ---- 和弦行 ---- */
  const row2 = document.createElement('div');
  row2.className = 'seg-row';
  const chords = (store.score && store.score.meta && Array.isArray(store.score.meta.chords)) ? store.score.meta.chords : [];
  if (chords.length) {
    const beat = 60 / ((store.score && store.score.tempo) || 120);
    const barS = beat * (4 * (parseSig(store.score && store.score.time_signature)[0] / parseSig(store.score && store.score.time_signature)[1]));
    let lastEnd = -1;
    for (const c of chords) {
      const start = (c.start != null) ? c.start : ((c.bar != null ? c.bar - 1 : 0) * barS);
      const end = (c.end != null) ? c.end : start + (c.len ? c.len * barS : barS);
      if (end <= lastEnd) continue;
      lastEnd = end;
      const x0 = xl(start), x1 = xl(end);
      if (x1 < 0 || x0 > W) continue;
      const a = clip(x0, 0, W), b = clip(x1, 0, W);
      if (b - a < 2) continue;
      const d = document.createElement('div');
      d.className = 'seg-chord';
      d.style.left = a + 'px';
      d.style.width = (b - a) + 'px';
      d.textContent = c.name || c.chord || '';
      row2.appendChild(d);
    }
  } else {
    const e = document.createElement('span');
    e.className = 'seg-empty';
    e.textContent = store.score ? '（无和弦数据——留位）' : '';
    row2.appendChild(e);
  }

  rowsEl.appendChild(row1);
  rowsEl.appendChild(row2);
}

function parseSig(sig) {
  const m = /^\s*(\d{1,2})\s*\/\s*(\d{1,2})\s*$/.exec(sig || '4/4');
  if (!m) return [4, 4];
  const n = parseInt(m[1], 10), d = parseInt(m[2], 10);
  return (n && d) ? [n, d] : [4, 4];
}

export function init(el) {
  rowsEl = el;
  new ResizeObserver(render).observe(rowsEl);
  for (const topic of ['state', 'view']) bus.on(topic, render);
  render();
}
