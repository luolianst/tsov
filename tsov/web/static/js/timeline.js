/* timeline.js —— 时间标尺（小节/拍/秒）+ 轨道头面板（M1b：色条/名/参数控件/选中）+ 乐句 segments 底条 */

import { bus } from './events.js';
import { api } from './api.js';
import { KEYS_W, store, tempo, beatsPerBar, segments, scoreBounds, setSelection, setError, toggleOverlay, refTag } from './state.js';
import { pal, trackColors } from './theme.js';

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

export function draw() {
  if (!ctx) return;
  ctx.clearRect(0, 0, W, H);
  const p = pal();
  const beat = 60 / tempo();
  const t0 = Math.max(0, tOf(KEYS_W));
  const t1 = tOf(W);
  const tMax = Math.max(t1, scoreBounds().tEnd + 2);

  /* 乐句 segments 背景条（ruler 下半段） */
  const segs = segments();
  for (const s of segs) {
    const x0 = Math.max(KEYS_W, xOf(s.start)), x1 = Math.min(W, xOf(s.end));
    if (x1 <= x0) continue;
    ctx.fillStyle = s.type === 'phrase' ? p.segPhrase : (s.type === 'breath' ? p.segBreath : p.segPause);
    ctx.fillRect(x0, H - 6, x1 - x0, 6);
  }

  /* 小节刻度 + 编号；细拍刻度（小节长度按拍号算） */
  ctx.font = '10px ' + getComputedStyle(document.documentElement).getPropertyValue('--font-mono');
  const barS = beat * beatsPerBar();
  let bar = Math.floor(t0 / barS);
  for (; bar * barS <= tMax; bar++) {
    const t = bar * barS;
    const x = Math.round(xOf(t)) + 0.5;
    if (x < KEYS_W) continue;
    if (x > W) break;
    ctx.strokeStyle = p.rulerBar;
    ctx.beginPath(); ctx.moveTo(x, H - 12); ctx.lineTo(x, H); ctx.stroke();
    ctx.fillStyle = p.rulerText;
    ctx.fillText(String(bar + 1), x + 3, H - 14);
  }
  if (store.view.pxPerSec * beat > 10) {
    const bq = beatsPerBar();
    ctx.strokeStyle = p.rulerTick;
    for (let k = Math.floor(t0 / beat); k * beat <= tMax; k++) {
      if (Math.abs(k / bq - Math.round(k / bq)) < 1e-6) continue;   // 小节线单独画
      const x = Math.round(xOf(k * beat)) + 0.5;
      if (x < KEYS_W || x > W) continue;
      ctx.beginPath(); ctx.moveTo(x, H - 6); ctx.lineTo(x, H); ctx.stroke();
    }
  }
  /* 秒刻度（细网格足够密时显示） */
  if (store.view.pxPerSec > 26) {
    ctx.fillStyle = p.rulerTick;
    for (let s = Math.ceil(t0); s <= tMax; s++) {
      const x = Math.round(xOf(s)) + 0.5;
      if (x < KEYS_W || x > W) continue;
      ctx.fillText(s + 's', x + 2, 12);
    }
  }
  /* 键盘列槽位 */
  ctx.fillStyle = p.rowBlack;
  ctx.fillRect(0, 0, KEYS_W, H);
  ctx.fillStyle = p.keyLabel;
  ctx.font = '10px sans-serif';
  ctx.fillText('小节', 6, H - 6);
}

/* ---- 轨道头面板（DOM；修正轮2：去推子 + 单击选中 / Ctrl 多选 / 双击进单轨） ---- */

let onEnter = null;   // 双击回调（main.js 注入）

async function postMix(ti, value, label) {
  if (!store.project) return;
  try {
    const r = await api.postBatch(store.project, label, [{ op: 'set_track_mix', track: ti, value }], '参数：' + label);
    if (r.applied) bus.dispatch('toast', '已更新 ' + label + ' ' + refTag(r));
    else setError('被拒：' + (r.errors || []).join('；'));
  } catch (e) { setError(e.message); }
}

function markSelected(el) {
  let sel = -1;
  for (const node of el.children) {
    if (node.dataset && node.dataset.track !== undefined) {
      const ti = Number(node.dataset.track);
      if (ti === store.selection.track && sel < 0) { sel = ti; node.classList.add('selected'); }
      else node.classList.remove('selected');
    }
  }
}

function renderTracks(el) {
  el.innerHTML = '';
  if (!store.score || !store.score.tracks.length) {
    const d = document.createElement('div');
    d.className = 'muted small';
    d.style.padding = '0 10px';
    d.textContent = '（无音轨）';
    el.appendChild(d);
    return;
  }
  const tc = trackColors();
  const single = store.viewMode === 'single';
  store.score.tracks.forEach((tr, ti) => {
    const isMain = single && store.singleTrack === ti;
    const isOverlay = store.overlayTracks.has(ti);
    const item = document.createElement('div');
    item.className = 'track-item' + (store.hiddenTracks.has(ti) ? ' hidden-track' : '') +
      (isMain ? ' main-track' : '') + (isOverlay ? ' is-overlay' : '') +
      (store.agentTracks.has(ti) ? ' agent-touched' : '') +          // 批B B1-3：agent 改动标记
      (store.selection.track === ti ? ' selected' : '');
    item.dataset.track = String(ti);

    const chip = document.createElement('span');
    chip.className = 'track-chip';
    chip.style.background = tc[ti % tc.length];

    const body = document.createElement('div');
    body.className = 'track-body';

    const top = document.createElement('div');
    top.className = 'track-top';
    const name = document.createElement('div');
    name.className = 'track-name';
    name.textContent = tr.name || ('track ' + ti);
    const eye = document.createElement('button');
    eye.className = 'track-eye';
    eye.textContent = store.hiddenTracks.has(ti) ? '🚫' : '👁';
    eye.title = '显示/隐藏该轨';
    eye.onclick = (e) => {
      e.stopPropagation();
      if (store.hiddenTracks.has(ti)) store.hiddenTracks.delete(ti);
      else store.hiddenTracks.add(ti);
      renderTracks(el);
      bus.dispatch('view');
    };
    top.appendChild(name);
    const m = document.createElement('button');
    m.className = 'ms' + (tr.mute ? ' on' : '');
    m.textContent = 'M';
    m.title = '静音';
    m.onclick = (e) => { e.stopPropagation(); postMix(ti, { mute: !tr.mute }, tr.mute ? '取消静音' : '静音'); };
    const s = document.createElement('button');
    s.className = 'ms solo' + (tr.solo ? ' on' : '');
    s.textContent = 'S';
    s.title = '独奏';
    s.onclick = (e) => { e.stopPropagation(); postMix(ti, { solo: !tr.solo }, tr.solo ? '取消独奏' : '独奏'); };
    if (isMain || isOverlay) {
      const mk = document.createElement('span');
      mk.className = 'tr-mark';
      mk.textContent = isMain ? '主轨' : '叠加';
      top.appendChild(mk);
    }
    top.appendChild(m);
    top.appendChild(s);
    top.appendChild(eye);

    const sub = document.createElement('div');
    sub.className = 'track-sub';
    const inst = tr.instrument || {};
    sub.textContent = (inst.program || 'default') + ' · ' + tr.notes.length + ' 音' +
      (tr.bus && tr.bus !== 'master' ? ' · ' + tr.bus : '');
    /* 窄档小字行会隐藏（CSS）→ 信息并进 tooltip */
    item.title = (tr.name || ('track ' + ti)) + ' · ' + sub.textContent +
      (isMain ? ' ｜ 主轨（单轨写谱中）' : '') + (isOverlay ? ' ｜ 灰叠加' : '') +
      ' ｜ 双击：' + (single ? '切换主轨' : '进入单轨写谱');

    body.appendChild(top);
    body.appendChild(sub);
    item.appendChild(chip);
    item.appendChild(body);
    item.addEventListener('click', (e) => {
      if (e.target.closest('button')) return;
      /* 单轨模式：单击其他轨 = 灰叠加开关；总谱模式：Ctrl/Shift = 叠加集 */
      if ((single && ti !== store.singleTrack) || e.ctrlKey || e.metaKey || e.shiftKey) {
        toggleOverlay(ti);
        return;
      }
      setSelection(ti, []);
    });
    item.addEventListener('dblclick', (e) => {
      if (e.target.closest('button')) return;
      if (onEnter) onEnter(ti);   // 进入单轨 / 切换主轨（main.js 处理）
    });
    el.appendChild(item);
  });
}

let lastSelTrack = null;

export function init(rulerCanvas, trackListEl, segmentsInfoEl, metaInfoEl, opts) {
  canvas = rulerCanvas;
  ctx = canvas.getContext('2d');
  onEnter = (opts && opts.onEnter) || null;
  resize();
  new ResizeObserver(() => { resize(); draw(); }).observe(canvas);
  for (const topic of ['state', 'view']) bus.on(topic, draw);

  /* 修正轮2：视图模式切换 → 重画行（主轨/叠加标记） */
  bus.on('viewmode', () => renderTracks(trackListEl));
  bus.on('agenttracks', () => renderTracks(trackListEl));   // 批B B1-3：改动高亮


  bus.on('state', () => {
    renderTracks(trackListEl);
    lastSelTrack = store.selection.track;
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

  /* 选中变化：只切 class（不重建 DOM，避免打断滑块交互） */
  bus.on('selection', () => {
    if (store.selection.track !== lastSelTrack) {
      lastSelTrack = store.selection.track;
      markSelected(trackListEl);
    }
  });
}
