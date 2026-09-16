/* timeline.js —— 时间标尺（小节/拍/秒）+ 轨道头面板 + 乐句 segments 区条 */

import { bus } from './events.js';
import { KEYS_W, store, tempo, beatsPerBar, segments, scoreBounds } from './state.js';
import { TRACK_COLORS } from './roll.js';

let canvas, ctx, W = 0, H = 0, dpr = 1;

function resize() {
  dpr = window.devicePixelRatio || 1;
  const r = canvas.getBoundingClientRect();
  W = Math.max(10, Math.floor(r.width));
  H = Math.max(10, Math.floor(r.height));
  canvas.width = Math.floor(W * dpr);
  canvas.height = Math.floor(H * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}

function xOf(t) { return KEYS_W + (t - store.view.scrollSec) * store.view.pxPerSec; }
function tOf(x) { return store.view.scrollSec + (x - KEYS_W) / store.view.pxPerSec; }

const SEG_COLORS = { phrase: 'rgba(79,195,247,0.5)', pause: 'rgba(139,147,163,0.4)', breath: 'rgba(186,104,200,0.4)' };

export function draw() {
  if (!ctx) return;
  ctx.clearRect(0, 0, W, H);
  const beat = 60 / tempo();
  const t0 = Math.max(0, tOf(KEYS_W));
  const t1 = tOf(W);
  const tMax = Math.max(t1, scoreBounds().tEnd + 2);

  /* 乐句 segments 背景条（ruler 下半段） */
  const segs = segments();
  for (const s of segs) {
    const x0 = Math.max(KEYS_W, xOf(s.start)), x1 = Math.min(W, xOf(s.end));
    if (x1 <= x0) continue;
    ctx.fillStyle = SEG_COLORS[s.type] || 'rgba(255,183,77,0.4)';
    ctx.fillRect(x0, H - 6, x1 - x0, 6);
  }

  /* 小节刻度 + 编号；细拍刻度（M-V6：小节长度按拍号算，不再写死 4/4） */
  ctx.font = '10px sans-serif';
  const barS = beat * beatsPerBar();
  let bar = Math.floor(t0 / barS);
  for (; bar * barS <= tMax; bar++) {
    const t = bar * barS;
    const x = Math.round(xOf(t)) + 0.5;
    if (x < KEYS_W) continue;
    if (x > W) break;
    ctx.strokeStyle = '#39414f';
    ctx.beginPath(); ctx.moveTo(x, H - 12); ctx.lineTo(x, H); ctx.stroke();
    ctx.fillStyle = '#9aa3b2';
    ctx.fillText(String(bar + 1), x + 3, H - 14);
  }
  if (store.view.pxPerSec * beat > 10) {
    const bq = beatsPerBar();
    ctx.strokeStyle = '#242a34';
    for (let k = Math.floor(t0 / beat); k * beat <= tMax; k++) {
      if (Math.abs(k / bq - Math.round(k / bq)) < 1e-6) continue;   // 小节线单独画
      const x = Math.round(xOf(k * beat)) + 0.5;
      if (x < KEYS_W || x > W) continue;
      ctx.beginPath(); ctx.moveTo(x, H - 6); ctx.lineTo(x, H); ctx.stroke();
    }
  }
  /* 秒刻度（细网格足够密时显示；H 加高后 y=9 → 12 与小节号分层不重叠） */
  if (store.view.pxPerSec > 26) {
    ctx.fillStyle = '#5f6774';
    for (let s = Math.ceil(t0); s <= tMax; s++) {
      const x = Math.round(xOf(s)) + 0.5;
      if (x < KEYS_W || x > W) continue;
      ctx.fillText(s + 's', x + 2, 12);
    }
  }
  /* 键盘列槽位 */
  ctx.fillStyle = '#1b1e25';
  ctx.fillRect(0, 0, KEYS_W, H);
  ctx.fillStyle = '#5f6774';
  ctx.font = '10px sans-serif';
  ctx.fillText('小节', 6, H - 6);
}

/* ---- 轨道头面板（DOM）---- */
function renderTracks(el) {
  el.innerHTML = '';
  if (!store.score || !store.score.tracks.length) {
    el.innerHTML = '<div class="muted small">（无音轨）</div>';
    return;
  }
  store.score.tracks.forEach((tr, ti) => {
    const item = document.createElement('div');
    item.className = 'track-item' + (store.hiddenTracks.has(ti) ? ' hidden-track' : '');
    const color = TRACK_COLORS[ti % TRACK_COLORS.length];
    item.innerHTML =
      '<span class="track-chip" style="background:' + color + '"></span>' +
      '<div class="track-meta">' +
        '<div class="track-name"></div>' +
        '<div class="track-sub"></div>' +
      '</div>';
    item.querySelector('.track-name').textContent = tr.name || ('track ' + ti);
    const sub = item.querySelector('.track-sub');
    sub.textContent = (tr.instrument ? (tr.instrument.program || 'default') + ' · ' : '') +
      (tr.instrument ? Math.round((tr.instrument.volume || 1) * 100) + '% · ' : '') +
      tr.notes.length + ' 音';
    const eye = document.createElement('button');
    eye.className = 'track-eye';
    eye.textContent = store.hiddenTracks.has(ti) ? '🚫' : '👁';
    eye.title = '显示/隐藏该轨';
    eye.onclick = () => {
      if (store.hiddenTracks.has(ti)) store.hiddenTracks.delete(ti);
      else store.hiddenTracks.add(ti);
      renderTracks(el);
      bus.dispatch('view');
    };
    item.appendChild(eye);
    el.appendChild(item);
  });
}

export function init(rulerCanvas, trackListEl, segmentsInfoEl, metaInfoEl) {
  canvas = rulerCanvas;
  ctx = canvas.getContext('2d');
  resize();
  new ResizeObserver(() => { resize(); draw(); }).observe(canvas);
  for (const topic of ['state', 'view']) bus.on(topic, draw);

  bus.on('state', () => {
    renderTracks(trackListEl);
    const segs = segments();
    segmentsInfoEl.textContent = segs.length
      ? segs.map((s) => (s.start.toFixed(1) + '-' + s.end.toFixed(1) + 's ' + (s.type || ''))).join('；')
      : '（无）';
    const sc = store.score;
    metaInfoEl.textContent = sc
      ? 'tempo ' + Math.round(sc.tempo) + ' · ' + (sc.time_signature || '4/4') + ' · ' +
        (sc.key_candidates.length ? sc.key_candidates.map((k) => k.key).join('/') : '调性未知') +
        ' · ' + sc.tracks.reduce((a, t) => a + t.notes.length, 0) + ' 音'
      : '（未打开）';
  });
}
