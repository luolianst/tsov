/* timeline.js —— 时间标尺（小节/拍/秒）+ 轨道头面板（M1b：色条/名/参数控件/选中）+ 乐句 segments 底条 */

import { bus } from './events.js';
import { api } from './api.js';
import { KEYS_W, store, tempo, beatsPerBar, segments, scoreBounds, setSelection, setError, partnerOf, setPartner, swapMainPartner, toggleRef, setPartnerLane, setAutoLane, trackLanes, refTag, paramLabel, hasVelocityLane, toggleVelocityLane, bookmarks, folderTracks, setLoop, setLoopOn, setSelBookmark, setSelFolder, toggleFolderCollapse, setSingleTrack } from './state.js';
import { pal, trackColors } from './theme.js';
import { seekTo } from './playback.js';
import { xOf, tOf } from './geom.js';   /* v0.2 批C 前段（R2 地基件）：时间↔x 几何共享 */

let canvas, ctx, W = 0, H = 0, dpr = 1;

/* v0.2 批C 后段（P4/P5 轨管理）：拖拽状态（dragstart 记源；drop 端兼容 CDP 注入的 dataTransfer） */
let dragTrack = -1;      // 正在拖的轨 index（-1 = 无）
let dragFolder = '';     // 正在拖的文件夹名（'' = 无）

function resize() {
  dpr = window.devicePixelRatio || 1;
  const r = canvas.getBoundingClientRect();
  W = Math.max(10, Math.floor(r.width));
  H = Math.max(10, Math.floor(r.height));
  canvas.width = Math.floor(W * dpr);
  canvas.height = Math.floor(H * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}

/* v0.2 批C 前段（R2 地基件）：时间↔x 几何改用共享 geom.js（行为一字不差） */

/* UI 修正轮3：拍级吸附（循环段 / 乐段创建与调整）；按住 Alt = 临时自由。拍长定义与 playback.js 一致 */
function beatSec() {
  const sc = store.score;
  const m = sc && sc.time_signature ? /^(\d+)\s*\/\s*(\d+)/.exec(sc.time_signature) : null;
  const den = m ? parseInt(m[2], 10) : 4;
  return (60 / tempo()) * (4 / (den || 4));
}
function snapT(t, e) {
  if (e && e.altKey) return Math.max(0, t);
  const b = beatSec();
  if (!(b > 0)) return Math.max(0, t);
  return Math.max(0, Math.round(t / b) * b);
}

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

  /* M-V8 E1：循环区间（整高淡色带 + 左右把手；开启时加深） */
  const L = store.loop;
  if (L) {
    const xa = Math.max(KEYS_W, xOf(L.start)), xb = Math.min(W, xOf(L.end));
    if (xb > xa) {
      ctx.globalAlpha = store.loopOn ? 0.16 : 0.07;
      ctx.fillStyle = p.playhead;
      ctx.fillRect(xa, 0, xb - xa, H);
      ctx.globalAlpha = store.loopOn ? 0.9 : 0.4;
      ctx.fillRect(xa, 0, 2, H);
      ctx.fillRect(xb - 2, 0, 2, H);
      ctx.fillRect(xa, 0, xb - xa, 3);
      ctx.globalAlpha = 1;
    }
  }

  /* M-V8 E1：播放头（顶部三角 + 细线） */
  const xp = xOf(store.playhead);
  if (xp >= KEYS_W && xp <= W) {
    ctx.fillStyle = p.playhead;
    ctx.beginPath();
    ctx.moveTo(xp - 4, 0); ctx.lineTo(xp + 4, 0); ctx.lineTo(xp, 6);
    ctx.closePath(); ctx.fill();
    ctx.fillRect(Math.round(xp), 0, 1, H);
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

/* E3 段2：用户侧「新建轨道」入口（#183② 闭环；命令层 add_track 同路径） */
function trackAddBtn() {
  const add = document.createElement('button');
  add.className = 'track-add';
  add.textContent = '＋ 新建轨道';
  add.title = '新建一条空白 MIDI 轨（双击进入单轨视图画音符；走命令层可撤销）';
  add.addEventListener('click', async () => {
    if (!store.project) { setError('先打开一个工程'); return; }
    try {
      const r = await api.postBatch(store.project, '新建轨道', [{ op: 'add_track', value: {} }], null);
      if (!r.applied) setError('被拒：' + (r.errors || []).join('；'));
      else bus.dispatch('toast', '已新建轨道（双击选中它进单轨画音符）');
    } catch (e) { setError(e.message); }
  });
  return add;
}

/* ---- v0.2 批C 后段（P4/P5）：拖拽排序辅助 ---- */

function clearDropMarks(el) {
  for (const c of el.querySelectorAll('.drop-before, .drop-after, .drop-into')) {
    c.classList.remove('drop-before', 'drop-after', 'drop-into');
  }
  el.classList.remove('drop-end');
}

function dragKinds(e) {
  /* dragover 期 getData 被浏览器禁用 → 以「在途状态 + 类型表」判定；drop 期由 getDragSource 取真值 */
  const out = { track: dragTrack >= 0, folder: !!dragFolder };
  try {
    const ts = Array.from((e.dataTransfer && e.dataTransfer.types) || []);
    if (ts.includes('application/x-tsov-track')) out.track = true;
    if (ts.includes('application/x-tsov-folder')) out.folder = true;
  } catch (err) { /* 合成事件无 dt */ }
  return out;
}

function getDragSource(e) {
  if (dragTrack >= 0) return { kind: 'track', ti: dragTrack };
  if (dragFolder) return { kind: 'folder', folder: dragFolder };
  try {
    const dt = e && e.dataTransfer;
    if (dt) {
      const t = dt.getData('application/x-tsov-track');
      if (t !== '' && t != null) return { kind: 'track', ti: parseInt(t, 10) };
      const f = dt.getData('application/x-tsov-folder');
      if (f !== '' && f != null) return { kind: 'folder', folder: f };
    }
  } catch (err) { /* ignore */ }
  return null;
}

function remapIdx(i, frm, to) {
  let x = i - (i > frm ? 1 : 0);
  if (x >= to) x += 1;
  return x;
}

/* 拖拽后前端索引态重映射（对齐 remove_track 的索引校正口径） */
function remapTrackStates(frm, to) {
  const remapSet = (s) => {
    const items = [...s].map((i) => remapIdx(i, frm, to));
    s.clear();
    items.forEach((i) => s.add(i));
  };
  remapSet(store.hiddenTracks);
  remapSet(store.refs);
  remapSet(store.agentTracks);
  remapSet(store.collapsedLanes);
  remapSet(store.velocityLanes);
  store.singleTrack = remapIdx(store.singleTrack, frm, to);
  if (store.selection && typeof store.selection.track === 'number') {
    store.selection = { track: remapIdx(store.selection.track, frm, to), indices: store.selection.indices };
  }
  if (store.partner) {
    if (store.partner.kind === 'lane') store.partner = { kind: 'lane', ti: remapIdx(store.partner.ti, frm, to), param: store.partner.param };
    else store.partner = { kind: store.partner.kind, ti: remapIdx(store.partner.ti, frm, to) };
  }
  store.focus = remapIdx(store.focus, frm, to);
}

async function dropTrackAt(el, from, overTi, before) {
  if (!store.project || from === overTi) return;
  const insert = before ? overTi : overTi + 1;
  const to = Math.max(0, Math.min(insert - (insert > from ? 1 : 0), store.score.tracks.length - 1));
  if (to === from) return;
  try {
    const r = await api.postBatch(store.project, '拖动排序',
      [{ op: 'move_track', track: from, value: { to } }], '拖动排序：轨 ' + from + ' → ' + to);
    if (r.applied) {
      remapTrackStates(from, to);
      bus.dispatch('toast', '轨已移动（可撤销）');
      bus.dispatch('state');
    } else setError('被拒：' + (r.errors || []).join('；'));
  } catch (e) { setError(e.message); }
}

async function joinFolder(ti, folder) {
  if (!store.project) return;
  const curF = (store.score.tracks[ti] || {}).folder || '';
  if (curF === folder) return;
  try {
    const r = await api.postBatch(store.project, '拖入文件夹',
      [{ op: 'set_track_folder', track: ti, value: { folder } }], '拖入文件夹：' + folder);
    if (r.applied) bus.dispatch('toast', '已拖入文件夹「' + folder + '」（可再拖动排序）');
    else setError('被拒：' + (r.errors || []).join('；'));
  } catch (e) { setError(e.message); }
}

async function reorderFolder(srcFolder, overFolder, before) {
  if (!store.project || srcFolder === overFolder) return;
  const tracks = store.score.tracks;
  const isSrc = (t) => (t.folder || '') === srcFolder;
  const isOver = (t) => (t.folder || '') === overFolder;
  const rest = tracks.filter((t) => !isSrc(t));
  const firstOver = rest.findIndex(isOver);
  if (firstOver < 0) return;
  let lastOver = -1;
  rest.forEach((t, i) => { if (isOver(t)) lastOver = i; });
  const to = before ? firstOver : lastOver + 1;
  try {
    const r = await api.postBatch(store.project, '文件夹排序',
      [{ op: 'move_folder', value: { folder: srcFolder, to } }], '文件夹排序：' + srcFolder + ' → ' + overFolder + (before ? ' 前' : ' 后'));
    if (r.applied) bus.dispatch('toast', '文件夹「' + srcFolder + '」已整体移动');
    else setError('被拒：' + (r.errors || []).join('；'));
  } catch (e) { setError(e.message); }
}

async function leaveToEnd(ti) {
  if (!store.project) return;
  const n = store.score.tracks.length;
  const tr = store.score.tracks[ti] || {};
  const cmds = [];
  if ((tr.folder || '') !== '') cmds.push({ op: 'set_track_folder', track: ti, value: { folder: '' } });
  if (ti !== n - 1) cmds.push({ op: 'move_track', track: ti, value: { to: n - 1 } });
  if (!cmds.length) return;
  try {
    const r = await api.postBatch(store.project, '拖出/置底', cmds, '拖出/置底');
    if (r.applied) {
      if (ti !== n - 1) remapTrackStates(ti, n - 1);
      bus.dispatch('toast', '已移到列表末尾（并移出文件夹）');
      bus.dispatch('state');
    } else setError('被拒：' + (r.errors || []).join('；'));
  } catch (e) { setError(e.message); }
}

function renderTracks(el) {
  el.innerHTML = '';
  if (!store.score || !store.score.tracks.length) {
    const d = document.createElement('div');
    d.className = 'muted small';
    d.style.padding = '0 10px';
    d.textContent = '（无音轨）';
    el.appendChild(d);
    el.appendChild(trackAddBtn());
    return;
  }
  const tc = trackColors();
  const single = store.viewMode === 'single';
  /* M-V8 E1：文件夹分组——记录每个文件夹的首个轨道下标（分组行插在那之前） */
  const firstOf = {};
  store.score.tracks.forEach((tr, i) => { if (tr.folder && firstOf[tr.folder] === undefined) firstOf[tr.folder] = i; });
  const po = partnerOf();   /* v0.2 C2：副区对象（单轨态；主/副行标记用） */
  store.score.tracks.forEach((tr, ti) => {
    const isMain = single && store.singleTrack === ti;
    const isPartner = !!(po && po.kind !== 'lane' && po.ti === ti);
    const lanesOfTr = trackLanes(tr);   /* v0.2 C2（F4）：道列表（树序；道栈渲染在 autoroll） */
    /* M-V8 E1：文件夹分组行（折叠 ▸/▾ + 单击选中为 M 键作用域） */
    if (tr.folder && firstOf[tr.folder] === ti) {
      const hd = document.createElement('div');
      hd.className = 'tl-folder' + (store.selFolder === tr.folder ? ' sel' : '');
      const tg = document.createElement('button');
      const collapsed = store.collapsedFolders.has(tr.folder);
      tg.className = 'fold-toggle';
      tg.textContent = collapsed ? '▸' : '▾';
      tg.title = collapsed ? '展开文件夹' : '折叠文件夹';
      tg.addEventListener('click', (e) => { e.stopPropagation(); toggleFolderCollapse(tr.folder); });
      const nm = document.createElement('span');
      nm.className = 'fold-name';
      nm.textContent = '📁 ' + tr.folder + '（' + folderTracks(tr.folder).length + ' 轨）';
      hd.appendChild(tg);
      hd.appendChild(nm);
      /* v0.2 C2（F6，闸门 G3）：文件夹行内联 M/S + 音量（最小形态；VCA 式不动路由） */
      const fd = ((store.score.folders || {})[tr.folder]) || {};
      const fm = document.createElement('button');
      fm.className = 'ms' + (fd.mute ? ' on' : '');
      fm.textContent = 'M';
      fm.title = '文件夹静音（成员轨；VCA 式）';
      fm.addEventListener('click', (e) => { e.stopPropagation(); postFolderMix(tr.folder, { mute: !fd.mute }); });
      const fs = document.createElement('button');
      fs.className = 'ms solo' + (fd.solo ? ' on' : '');
      fs.textContent = 'S';
      fs.title = '文件夹独奏（成员轨；VCA 式）';
      fs.addEventListener('click', (e) => { e.stopPropagation(); postFolderMix(tr.folder, { solo: !fd.solo }); });
      const fv = document.createElement('input');
      fv.type = 'number'; fv.min = '0'; fv.max = '2'; fv.step = '0.05';
      fv.className = 'fold-vol';
      fv.value = (fd.volume == null ? 1 : fd.volume);
      fv.title = '文件夹音量乘子（0~2）';
      fv.addEventListener('click', (e) => e.stopPropagation());
      fv.addEventListener('change', () => postFolderMix(tr.folder, { volume: Number(fv.value) }));
      hd.appendChild(fm);
      hd.appendChild(fs);
      hd.appendChild(fv);
      hd.title = '单击：选中该文件夹（M 键给它建旗）｜ ▾ 折叠/展开 ｜ M/S/音量：文件夹层（VCA）｜ 拖动：整体排序';
      /* v0.2 批C 后段（P5）：文件夹整块拖排序（成员连续化不变量）+ 接受轨拖入 */
      hd.draggable = true;
      hd.addEventListener('dragstart', (e) => {
        dragFolder = tr.folder;
        try { e.dataTransfer.setData('application/x-tsov-folder', tr.folder); e.dataTransfer.effectAllowed = 'move'; } catch (err) { /* ignore */ }
        hd.classList.add('dragging');
      });
      hd.addEventListener('dragend', () => { dragFolder = ''; hd.classList.remove('dragging'); clearDropMarks(el); });
      hd.addEventListener('dragover', (e) => {
        const k = dragKinds(e);
        if (!k.track && !k.folder) return;
        e.preventDefault();
        clearDropMarks(el);
        if (k.folder) {
          const r = hd.getBoundingClientRect();
          hd.classList.add((e.clientY - r.top) < r.height / 2 ? 'drop-before' : 'drop-after');
        } else {
          hd.classList.add('drop-into');
        }
      });
      hd.addEventListener('drop', (e) => {
        const src = getDragSource(e);
        if (!src) return;
        e.preventDefault();
        clearDropMarks(el);
        if (src.kind === 'track') { joinFolder(src.ti, tr.folder); return; }
        if (src.kind === 'folder' && src.folder !== tr.folder) {
          const r2 = hd.getBoundingClientRect();
          reorderFolder(src.folder, tr.folder, (e.clientY - r2.top) < r2.height / 2);
        }
      });
      hd.addEventListener('click', () => setSelFolder(store.selFolder === tr.folder ? '' : tr.folder));
      el.appendChild(hd);
    }
    const item = document.createElement('div');
    item.className = 'track-item' + (store.hiddenTracks.has(ti) ? ' hidden-track' : '') +
      (tr.folder && store.collapsedFolders.has(tr.folder) ? ' fold-hidden' : '') +
      (isMain ? ' main-track' : '') + (isPartner ? ' is-overlay' : '') +
      (store.agentTracks.has(ti) ? ' agent-touched' : '') +          // 批B B1-3：agent 改动标记
      (store.selection.track === ti ? ' selected' : '');
    item.dataset.track = String(ti);
    /* v0.2 批C 后段（P4）：轨拖拽重排 / 拖入文件夹 */
    item.draggable = true;
    item.addEventListener('dragstart', (e) => {
      dragTrack = ti;
      try { e.dataTransfer.setData('application/x-tsov-track', String(ti)); e.dataTransfer.effectAllowed = 'move'; } catch (err) { /* ignore */ }
      item.classList.add('dragging');
    });
    item.addEventListener('dragend', () => { dragTrack = -1; item.classList.remove('dragging'); clearDropMarks(el); });
    item.addEventListener('dragover', (e) => {
      const k = dragKinds(e);
      if (!k.track) return;
      e.preventDefault();
      clearDropMarks(el);
      const r = item.getBoundingClientRect();
      item.classList.add((e.clientY - r.top) < r.height / 2 ? 'drop-before' : 'drop-after');
    });
    item.addEventListener('drop', (e) => {
      const src = getDragSource(e);
      if (!src || src.kind !== 'track') return;
      e.preventDefault();
      clearDropMarks(el);
      if (src.ti === ti) return;
      const r = item.getBoundingClientRect();
      dropTrackAt(el, src.ti, ti, (e.clientY - r.top) < r.height / 2);
    });

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
    /* v0.2 C2（F4）：道列表展开钮（有道的轨才显示） */
    if (lanesOfTr.length) {
      const lt = document.createElement('button');
      lt.className = 'lane-toggle';
      lt.textContent = store.collapsedLanes.has(ti) ? '▸' : '▾';
      lt.title = '展开/折叠自动化道（' + lanesOfTr.length + '）';
      lt.addEventListener('click', (e) => {
        e.stopPropagation();
        if (store.collapsedLanes.has(ti)) store.collapsedLanes.delete(ti); else store.collapsedLanes.add(ti);
        renderTracks(el);
      });
      top.appendChild(lt);
    }
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
    if (isMain || isPartner) {
      const mk = document.createElement('span');
      mk.className = 'tr-mark';
      mk.textContent = isMain ? '主轨' : '副区';
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
      (isMain ? ' ｜ 主轨（单轨写谱中）' : '') + (isPartner ? ' ｜ 副区' : '') +
      ' ｜ 单击：' + (single ? '上/下副区' : '选中') + ' ｜ Ctrl+单击：' + (single ? '切主（原主降副）' : '参照层') +
      ' ｜ 双击：' + (single ? '独立打开' : '进入单轨写谱') + ' ｜ 右键：轨道菜单';

    body.appendChild(top);
    body.appendChild(sub);
    item.appendChild(chip);
    item.appendChild(body);
    item.addEventListener('click', (e) => {
      if (e.target.closest('button')) return;
      /* v0.2 C2 手势族：单轨 = 单击留而不切（副位）/ Ctrl+单击切且留（升主降副）；总谱 = Ctrl/Shift 参照层 */
      if (single) {
        if (ti === store.singleTrack) { setSelection(ti, []); return; }
        if (e.ctrlKey || e.metaKey) { swapMainPartner(ti); return; }
        setPartner(ti);
        return;
      }
      if (e.ctrlKey || e.metaKey || e.shiftKey) { toggleRef(ti); return; }
      setSelection(ti, []);
    });
    item.addEventListener('dblclick', (e) => {
      if (e.target.closest('button')) return;
      if (onEnter) onEnter(ti);   // 进入单轨 / 切换主轨（main.js 处理）
    });
    /* M-V8 E1：右键 = 轨道操作菜单（文件夹归属；小修包加：重命名 / 删除轨道；全部走命令层） */
    item.addEventListener('contextmenu', (e) => {
      e.preventDefault();
      if (!store.project) return;
      const cur = tr.folder || '';
      const tname = tr.name || ('track ' + ti);
      const nTracks = store.score ? store.score.tracks.length : 0;
      openBmMenu(e.clientX, e.clientY, [
        /* E3 段2：音频轨 → 转乐谱（挂「哼唱快车道」链并切到处理链面板） */
        ...(tr.kind === 'audio' ? [{
          label: '转乐谱…（实验性）',
          fn: () => bus.dispatch('voice2score', { track: ti }),
        }] : []),
        {
          label: '文件夹归属…',
          fn: () => {
            const v0 = prompt('归入文件夹（留空 = 移出；单层，不嵌套）：', cur);
            if (v0 === null) return;
            const v = v0.trim();
            if (v === cur) return;
            api.postBatch(store.project, '文件夹归属', [{ op: 'set_track_folder', track: ti, value: { folder: v } }], null)
              .then((r) => {
                if (r.applied) bus.dispatch('toast', '「' + tname + '」→ ' + (v || '（无文件夹）'));
                else setError('被拒：' + (r.errors || []).join('；'));
              })
              .catch((err) => setError(err.message));
          },
        },
        {
          label: '添加自动化道…',
          fn: () => openBmMenu(e.clientX, e.clientY, lanePickerItems(ti, tr)),
        },
        {
          label: '重命名…',
          fn: () => {
            const v0 = prompt('新轨道名：', tr.name || '');
            if (v0 === null) return;
            const v = v0.trim();
            if (!v || v === tr.name) return;
            api.postBatch(store.project, '重命名轨道', [{ op: 'rename_track', track: ti, value: { name: v } }], null)
              .then((r) => {
                if (r.applied) bus.dispatch('toast', '轨道已改名：' + tname + ' → ' + v);
                else setError('被拒：' + (r.errors || []).join('；'));
              })
              .catch((err) => setError(err.message));
          },
        },
        {
          label: '删除轨道',
          fn: () => {
            if (!confirm('删除轨道「' + tname + '」？' + (nTracks <= 1 ? '（最后一条音轨，工程将变空！）' : '') +
                         '（书签引用一并清理；可 Ctrl+Z 撤销）')) return;
            api.postBatch(store.project, '删除轨道', [{ op: 'remove_track', track: ti }], null)
              .then((r) => {
                if (!r.applied) { setError('被拒：' + (r.errors || []).join('；')); return; }
                bus.dispatch('toast', '已删除「' + tname + '」（可 Ctrl+Z 撤销）');
                /* 索引校正：删除后原 ti+1 起前移；主轨/选中轨按删位更新（防脏索引渲染） */
                const newCount = Math.max(0, nTracks - 1);
                const fix = (i) => (i === ti ? Math.min(ti, Math.max(0, newCount - 1)) : (i > ti ? i - 1 : i));
                const sti = fix(store.singleTrack);
                if (sti !== store.singleTrack) setSingleTrack(sti);
                const st = store.selection && typeof store.selection.track === 'number' ? store.selection.track : null;
                if (st !== null) {
                  const ns = fix(st);
                  if (ns !== st) setSelection(ns, []);
                }
              })
              .catch((err) => setError(err.message));
          },
        },
      ]);
    });
    el.appendChild(item);
    /* v0.2 C2（F4）：道行（轨树展开；单击=定位道栈 / 双击=入副区 / 右键=删除道）
       热修（10-08）：文件夹折叠时道行随折（与行 fold-hidden 同条件——不再孤悬） */
    if (lanesOfTr.length && !store.collapsedLanes.has(ti) && !(tr.folder && store.collapsedFolders.has(tr.folder))) {
      for (const l of lanesOfTr) {
        const lr = document.createElement('div');
        lr.className = 'lane-row';
        lr.dataset.param = l.param;
        lr.textContent = '⤷ ' + l.label;
        lr.title = '道「' + l.label + '」｜ 单击：定位道栈 ｜ 双击：入副区（全宽曲线）｜ 右键：删除道';
        lr.addEventListener('click', (e) => { e.stopPropagation(); locateLane(ti, l.param); });
        lr.addEventListener('dblclick', (e) => { e.stopPropagation(); enterLanePartner(ti, l.param); });
        lr.addEventListener('contextmenu', (e) => {
          e.preventDefault();
          e.stopPropagation();
          openBmMenu(e.clientX, e.clientY, [{ label: '删除道「' + l.label + '」（曲线数据保留）', fn: () => removeLaneOp(ti, l) }]);
        });
        el.appendChild(lr);
      }
    }
  });
  /* v0.2 批C 后段（P4）：容器空白区 = 置底（并移出文件夹）
     —— 幂等绑定：#track-list 是持久元素、renderTracks 反复进入，直接 addEventListener 会叠挂 N 份，
     一次 drop 触发 N 次 leaveToEnd（首跑冒烟实测：4 轨 folder 被连环清空） */
  if (!el.__dndEndBound) {
    el.__dndEndBound = true;
    el.addEventListener('dragover', (e) => {
      if (e.target !== el) return;
      const k = dragKinds(e);
      if (!k.track) return;
      e.preventDefault();
      el.classList.add('drop-end');
    });
    el.addEventListener('dragleave', (e) => { if (e.target === el) el.classList.remove('drop-end'); });
    el.addEventListener('drop', (e) => {
      if (e.target !== el) return;
      const src = getDragSource(e);
      if (!src || src.kind !== 'track') return;
      e.preventDefault();
      el.classList.remove('drop-end');
      leaveToEnd(src.ti);
    });
  }
  el.appendChild(trackAddBtn());
  bus.dispatch('rowlayout');   /* UI 修正轮3.2：行布局就绪 → main.js 收集推给卷帘（文件夹行/折叠路径对齐） */
}

/* ---- v0.2 C2（F3/F4）：道实体 UI——选择器 / 增删 / 定位 / 入副区 ---- */

async function addLaneOp(ti, param, label) {
  if (!store.project) return;
  try {
    const r = await api.postBatch(store.project, '添加自动化道',
      [{ op: 'add_lane', track: ti, value: { param } }],
      '添加自动化道：' + (label || param));
    if (r.applied) {
      bus.dispatch('toast', '已建道：' + (label || param) + '（下方道栈可编辑）');
      if (store.collapsedLanes.has(ti)) store.collapsedLanes.delete(ti);
      setAutoLane({ open: true, param });
    } else setError('被拒：' + (r.errors || []).join('；'));
  } catch (e) { setError(e.message); }
}

/* v0.2 批C 后段（挂道族）：力度道开关（bars 伪道；视图态，不进谱）。 */
function velocityToggle(ti) {
  const on = toggleVelocityLane(ti);
  if (on) {
    setAutoLane({ open: true, param: 'velocity' });
    bus.dispatch('toast', '已开力度道（柱状）——单轨视图道栈中编辑；再点菜单项关闭');
  } else bus.dispatch('toast', '已关力度道');
}

async function removeLaneOp(ti, lane) {
  if (!store.project) return;
  try {
    const r = await api.postBatch(store.project, '移除自动化道',
      [{ op: 'remove_lane', track: ti, value: { id: lane.id || lane.param } }],
      '移除自动化道：' + lane.label);
    if (r.applied) bus.dispatch('toast', '已移除道「' + lane.label + '」（曲线数据保留，可 Ctrl+Z 撤销）');
    else setError('被拒：' + (r.errors || []).join('；'));
  } catch (e) { setError(e.message); }
}

/* 两段式选择器（闸门 G2；v0.2 批C 后段：+ 力度伪道 + perf 曲线（弯音/CC）；效果参数置灰按 Q3 →「随效果域深化」）。 */
function lanePickerItems(ti, tr) {
  const specs = (store.metaParams && store.metaParams.automation) || null;
  const auto = specs
    ? Object.keys(specs).filter((k) => specs[k] && specs[k].automatable !== false)
    : ['volume', 'pan'];
  const bound = new Set(trackLanes(tr).map((l) => l.param));
  const items = [];
  for (const k of auto) {
    const lab = (specs && specs[k] && specs[k].label) || paramLabel(k);
    if (bound.has(k)) items.push({ label: '✓ ' + lab + '（已建道）', disabled: true });
    else items.push({ label: '＋ ' + lab, fn: () => addLaneOp(ti, k, lab) });
  }
  /* v0.2 批C 后段：力度道（bars 伪道开关；零存储——数据 = Note.velocity） */
  if (hasVelocityLane(ti)) items.push({ label: '✓ 力度（柱状·已开·点击关闭）', fn: () => velocityToggle(ti) });
  else items.push({ label: '＋ 力度（柱状编辑）', fn: () => velocityToggle(ti) });
  const fxSpecs = (store.metaParams && store.metaParams.effects) || {};
  const chain = ((tr.instrument || {}).effects) || [];
  let nFx = 0;
  for (const fx of chain) {
    const kind = String((fx && fx.type) || '');
    const table = fxSpecs[kind] || {};
    for (const pk of Object.keys(table)) {
      nFx += 1;
      items.push({ label: '⛔ ' + pk + ' · ' + kind + '（随效果域深化）', disabled: true });
    }
  }
  if (!nFx) items.push({ label: '⛔ 效果参数（随效果域深化）', disabled: true });
  return items;
}

/* 道行单击：定位道栈（必要时先进入该轨单轨态） + 闪烁。 */
function locateLane(ti, param) {
  if (store.viewMode !== 'single' || store.singleTrack !== ti) {
    if (onEnter) onEnter(ti);
  }
  setAutoLane({ open: true, param });
  setTimeout(() => {
    const row = document.querySelector('#autolanes .alane[data-param="' + param + '"]');
    if (row) {
      row.scrollIntoView({ block: 'nearest' });
      row.classList.add('alane-flash');
      setTimeout(() => row.classList.remove('alane-flash'), 700);
    }
  }, 140);
}

/* v0.2 C2（F6）：文件夹行内联控件 → set_folder_mix（命令层同一路径）。 */
async function postFolderMix(name, patchV) {
  if (!store.project) return;
  try {
    const r = await api.postBatch(store.project, '文件夹混音',
      [{ op: 'set_folder_mix', value: Object.assign({ folder: name }, patchV) }],
      '文件夹混音：' + name + ' ' + JSON.stringify(patchV));
    if (r.applied) bus.dispatch('toast', '文件夹「' + name + '」已更新 ' + refTag(r));
    else setError('被拒：' + (r.errors || []).join('；'));
  } catch (e) { setError(e.message); }
}

/* 道行双击：入副区（全宽曲线编辑）；再双击同参撤下。 */
function enterLanePartner(ti, param) {
  const go = () => {
    setPartnerLane(ti, param);
    const raw = store.partner;
    const on = !!(raw && raw.kind === 'lane' && raw.ti === ti && raw.param === param);
    bus.dispatch('toast', on ? '道「' + param + '」入副区' : '已撤下道副区');
  };
  if (store.viewMode !== 'single' || store.singleTrack !== ti) {
    if (onEnter) onEnter(ti);
    setTimeout(go, 90);
  } else go();
}

/* ======================================================================
   M-V8 E1：段道（markers canvas）——项目层乐段/记号的常驻呈现与编辑
   拖动空白=新建段（内联命名）｜拖动段=移动 ｜ 拖边缘=拉伸 ｜ 单击=选中 ｜ 双击=播放轴定位 ｜ 右键菜单
   ====================================================================== */

let mcanvas = null, mctx = null, MW = 0, MH = 0, mdpr = 1;

function mresize() {
  mdpr = window.devicePixelRatio || 1;
  const r = mcanvas.getBoundingClientRect();
  MW = Math.max(10, Math.floor(r.width));
  MH = Math.max(10, Math.floor(r.height));
  mcanvas.width = Math.floor(MW * mdpr);
  mcanvas.height = Math.floor(MH * mdpr);
  mctx.setTransform(mdpr, 0, 0, mdpr, 0, 0);
}

function round3(x) { return Math.round(Number(x) * 1000) / 1000; }

/** 项目层书签（段/记号）→ [{b, i}]（i = score.bookmarks 下标，命令层寻址用）。 */
function projectMarks() {
  const out = [];
  bookmarks().forEach((b, i) => { if (b.scope === 'project') out.push({ b, i }); });
  return out;
}

function sectionAt(offX) {
  const list = projectMarks().filter((x) => x.b.kind === 'section');
  for (let k = list.length - 1; k >= 0; k--) {
    const { b, i } = list[k];
    if (offX >= xOf(b.start) && offX <= xOf(b.end)) return i;
  }
  return -1;
}

function markAt(offX) {
  for (const { b, i } of projectMarks()) {
    if (b.kind !== 'section' && Math.abs(offX - xOf(b.start)) <= 6) return i;
  }
  return -1;
}

export function drawMarkers() {
  if (!mctx) return;
  mctx.clearRect(0, 0, MW, MH);
  const p = pal();
  const tc = trackColors();
  /* 左槽（与标尺一致） */
  mctx.fillStyle = p.rowBlack;
  mctx.fillRect(0, 0, KEYS_W, MH);
  mctx.fillStyle = p.keyLabel;
  mctx.font = '10px sans-serif';
  mctx.fillText('段', 6, MH - 7);

  let si = 0;
  projectMarks().forEach(({ b, i }) => {
    const x0 = xOf(b.start);
    if (b.kind === 'section') {
      const x1 = xOf(b.end);
      const a = Math.max(KEYS_W, Math.min(x0, x1)), c = Math.min(MW, Math.max(x0, x1));
      if (c <= a) return;
      si += 1;
      const col = b.color || tc[i % tc.length];
      mctx.globalAlpha = 0.22; mctx.fillStyle = col; mctx.fillRect(a, 2, c - a, MH - 4); mctx.globalAlpha = 1;
      mctx.strokeStyle = (i === store.selBookmark) ? p.playhead : col;
      mctx.lineWidth = (i === store.selBookmark) ? 2 : 1;
      mctx.strokeRect(a + 0.5, 2.5, Math.max(1, c - a - 1), MH - 5);
      mctx.lineWidth = 1;
      mctx.save();
      mctx.beginPath(); mctx.rect(a + 2, 1, Math.max(1, c - a - 4), MH - 2); mctx.clip();
      mctx.fillStyle = p.rulerText;
      mctx.fillText(b.label || ('段 ' + si), a + 5, MH - 7);
      mctx.restore();
    } else {
      if (x0 < KEYS_W || x0 > MW) return;
      mctx.fillStyle = (i === store.selBookmark) ? p.playhead : p.rulerText;
      mctx.beginPath();
      mctx.moveTo(x0, 5); mctx.lineTo(x0 + 8, 5); mctx.lineTo(x0 + 4, 13);
      mctx.closePath(); mctx.fill();
      mctx.save();
      mctx.beginPath(); mctx.rect(x0, 0, Math.max(1, MW - x0), MH); mctx.clip();
      mctx.fillText(b.label || '记号', x0 + 10, 13);
      mctx.restore();
    }
  });

  /* 播放头细线（与标尺对齐） */
  const xp = xOf(store.playhead);
  if (xp >= KEYS_W && xp <= MW) {
    mctx.fillStyle = p.playhead;
    mctx.fillRect(Math.round(xp), 0, 1, MH);
  }
}

/* ---- 拖动预览 / 内联命名 / 右键菜单 ---- */

let previewEl = null;

function showPreview(a, b) {
  if (!previewEl) {
    previewEl = document.createElement('div');
    previewEl.className = 'mk-preview';
    document.body.appendChild(previewEl);
  }
  const r = mcanvas.getBoundingClientRect();
  const x0 = Math.max(KEYS_W, xOf(Math.min(a, b)));
  const x1 = Math.min(MW, xOf(Math.max(a, b)));
  previewEl.style.display = 'block';
  previewEl.style.left = (r.left + x0) + 'px';
  previewEl.style.top = (r.top + 2) + 'px';
  previewEl.style.width = Math.max(2, x1 - x0) + 'px';
  previewEl.style.height = (MH - 4) + 'px';
}

function hidePreview() { if (previewEl) previewEl.style.display = 'none'; }

let menuEl = null;

export function closeBmMenu() { if (menuEl) { menuEl.remove(); menuEl = null; } }

/* E3 段2：改名导出——通用右键菜单（轨头/后续 lanes 可复用） */
export function openBmMenu(x, y, items) {
  closeBmMenu();
  menuEl = document.createElement('div');
  menuEl.className = 'bm-menu';
  menuEl.style.left = Math.min(x, window.innerWidth - 140) + 'px';
  menuEl.style.top = Math.min(y, window.innerHeight - 30 - items.length * 24) + 'px';
  for (const it of items) {
    const b = document.createElement('button');
    b.textContent = it.label;
    if (it.disabled) {
      b.classList.add('bm-disabled');   /* 置灰占位（效果参数——随 P26 批开放） */
      b.disabled = true;
    } else {
      b.addEventListener('click', () => { closeBmMenu(); it.fn(); });
    }
    menuEl.appendChild(b);
  }
  document.body.appendChild(menuEl);
}

/** 内联命名输入框（段道新建段后 / 右键改名 / 书签树双击共用）。 */
export function openRenameAt(i, anchorClientX, anchorClientY) {
  const bms = bookmarks();
  if (!(i >= 0 && i < bms.length)) return;
  const inp = document.createElement('input');
  inp.className = 'mk-rename';
  inp.value = bms[i].label || '';
  inp.placeholder = '名称（回车确认 / Esc 取消）';
  inp.style.left = Math.max(8, Math.min(anchorClientX, window.innerWidth - 240)) + 'px';
  inp.style.top = Math.max(8, anchorClientY) + 'px';
  document.body.appendChild(inp);
  inp.focus();
  inp.select();
  let finish = false;
  const done = async (commit) => {
    if (finish) return;
    finish = true;
    const v = inp.value.trim();
    inp.remove();
    if (commit) {
      const r = await patchBookmark(i, { label: v });
      if (r && r.applied) bus.dispatch('toast', '已命名：' + (v || '（空）'));
    }
  };
  inp.addEventListener('keydown', (e) => {
    e.stopPropagation();
    if (e.key === 'Enter') done(true);
    else if (e.key === 'Escape') done(false);
  });
  inp.addEventListener('blur', () => done(true));
}

async function patchBookmark(i, value) {
  if (!store.project) return null;
  try {
    const r = await api.postBatch(store.project, '书签调整', [{ op: 'set_bookmark', track: 0, index: i, value }], null);
    if (!r.applied) setError('被拒：' + (r.errors || []).join('；'));
    return r;
  } catch (e) { setError(e.message); return null; }
}

async function removeBookmarkAt(i) {
  const bms = bookmarks();
  if (!(i >= 0 && i < bms.length)) return;
  if (!confirm('删除书签「' + (bms[i].label || '（未命名）') + '」？')) return;
  try {
    const r = await api.postBatch(store.project, '删除书签', [{ op: 'remove_bookmark', track: 0, index: i }], null);
    if (!r.applied) setError('被拒：' + (r.errors || []).join('；'));
    else { setSelBookmark(-1); bus.dispatch('toast', '已删除书签'); }
  } catch (e) { setError(e.message); }
}

/** E1：按书签设循环区间（段右键 / 书签树 🔁 共用）。 */
export function loopFromBookmark(i) {
  const b = bookmarks()[i];
  if (!b) return;
  if (b.kind === 'section' && b.end != null) {
    setLoop({ start: b.start, end: b.end });
    setLoopOn(true);
    bus.dispatch('toast', '循环区间：' + b.start.toFixed(1) + '–' + b.end.toFixed(1) + 's');
  } else {
    setLoop(null);
    setLoopOn(false);
    bus.dispatch('toast', '记号不是区间，已清除循环');
  }
}

async function addProjectMark() {
  if (!store.project || !store.score) return;
  const len = bookmarks().length;
  const t = round3(Math.max(0, store.playhead));
  try {
    const r = await api.postBatch(store.project, '项目记号', [{ op: 'add_bookmark', track: 0, value: { scope: 'project', kind: 'mark', start: t, label: '' } }], null);
    if (!r.applied) { setError('被拒：' + (r.errors || []).join('；')); return; }
    const rr = mcanvas.getBoundingClientRect();
    openRenameAt(len, rr.left + xOf(t) + 4, rr.top - 30);
  } catch (e) { setError(e.message); }
}

export function initMarkers(markersCanvas) {
  mcanvas = markersCanvas;
  mctx = mcanvas.getContext('2d');
  mresize();
  new ResizeObserver(() => { mresize(); drawMarkers(); }).observe(mcanvas);
  for (const topic of ['state', 'view', 'playhead', 'markers']) bus.on(topic, drawMarkers);

  document.addEventListener('mousedown', (e) => { if (menuEl && !menuEl.contains(e.target)) closeBmMenu(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeBmMenu(); });

  let mdrag = null;

  mcanvas.addEventListener('mousedown', (e) => {
    if (e.button !== 0 || e.offsetX < KEYS_W) return;
    closeBmMenu();
    const t = Math.max(0, tOf(e.offsetX));
    const i = sectionAt(e.offsetX);
    if (i >= 0) {
      const b = bookmarks()[i];
      setSelBookmark(i);
      const x0 = xOf(b.start), x1 = xOf(b.end);
      const mode = (Math.abs(e.offsetX - x0) <= 5) ? 'a' : ((Math.abs(e.offsetX - x1) <= 5) ? 'b' : 'body');
      mdrag = { i, mode, t0: t, b0: { start: b.start, end: b.end }, cur: { start: b.start, end: b.end }, moved: false };
      return;
    }
    const mi = markAt(e.offsetX);
    if (mi >= 0) { setSelBookmark(mi); return; }
    setSelBookmark(-1);
    mdrag = { i: -1, mode: 'new', t0: snapT(t, e), cur: { start: t, end: t }, moved: false };   /* UI 修正轮3：起点吸附 */
  });

  window.addEventListener('mousemove', (e) => {
    if (!mdrag) return;
    const r = mcanvas.getBoundingClientRect();
    if (e.clientY < r.top - 60 || e.clientY > r.bottom + 60) return;
    const traw = Math.max(0, tOf(e.clientX - r.left));
    const t = snapT(traw, e);   /* UI 修正轮3：拍级吸附（Alt = 自由） */
    if (!mdrag.moved && Math.abs(e.clientX - r.left - xOf(mdrag.t0)) < 3) return;
    mdrag.moved = true;
    const c = mdrag.cur;
    if (mdrag.mode === 'new') { c.start = Math.min(mdrag.t0, t); c.end = Math.max(mdrag.t0, t); }
    else if (mdrag.mode === 'body') {
      const d = traw - mdrag.t0;
      const ns = snapT(mdrag.b0.start + d, e);   /* 起点吸附、保长 */
      c.start = Math.max(0, ns);
      c.end = c.start + (mdrag.b0.end - mdrag.b0.start);
    } else if (mdrag.mode === 'a') { c.start = Math.max(0, Math.min(t, c.end - 0.05)); }
    else if (mdrag.mode === 'b') { c.end = Math.max(t, c.start + 0.05); }
    showPreview(c.start, c.end);
  });

  window.addEventListener('mouseup', async (e) => {
    if (!mdrag) return;
    const d = mdrag;
    mdrag = null;
    hidePreview();
    if (!d.moved) {
      if (d.i >= 0) return;            // 段上单击 = 仅选中（双击跳转）
      seekTo(d.t0);                    // 空白单击 = 播放轴定位（与标尺一致）
      return;
    }
    const c = d.cur;
    if (d.mode === 'new') {
      if (c.end - c.start < 0.15) return;
      const len = bookmarks().length;  // 追加在尾部 → 新下标
      try {
        const r = await api.postBatch(store.project, '新建乐段', [{ op: 'add_bookmark', track: 0, value: { scope: 'project', kind: 'section', start: round3(c.start), end: round3(c.end), label: '' } }], null);
        if (!r.applied) { setError('被拒：' + (r.errors || []).join('；')); return; }
        openRenameAt(len, e.clientX + 6, e.clientY - 30);
      } catch (err) { setError(err.message); }
      return;
    }
    await patchBookmark(d.i, { start: round3(c.start), end: round3(c.end) });
  });

  mcanvas.addEventListener('dblclick', (e) => {
    if (e.offsetX < KEYS_W) return;
    const i = sectionAt(e.offsetX);
    const mi = (i >= 0) ? i : markAt(e.offsetX);
    if (mi >= 0) { setSelBookmark(mi); seekTo(bookmarks()[mi].start); }
  });

  mcanvas.addEventListener('contextmenu', (e) => {
    if (e.offsetX < KEYS_W) return;
    e.preventDefault();
    const i = sectionAt(e.offsetX);
    const mi = (i >= 0) ? i : markAt(e.offsetX);
    if (mi < 0) {
      openBmMenu(e.clientX, e.clientY, [{ label: '在播放轴建记号', fn: addProjectMark }]);
      return;
    }
    setSelBookmark(mi);
    const b = bookmarks()[mi];
    const r = mcanvas.getBoundingClientRect();
    const items = [{ label: '跳转', fn: () => seekTo(b.start) }];
    if (b.kind === 'section') items.push({ label: '循环此段', fn: () => loopFromBookmark(mi) });
    items.push({ label: '改名…', fn: () => openRenameAt(mi, r.left + xOf(b.start) + 4, r.top - 30) });
    items.push({ label: '删除', fn: () => removeBookmarkAt(mi) });
    openBmMenu(e.clientX, e.clientY, items);
  });
}

let lastSelTrack = null;

export function init(rulerCanvas, trackListEl, segmentsInfoEl, metaInfoEl, opts) {
  canvas = rulerCanvas;
  ctx = canvas.getContext('2d');
  onEnter = (opts && opts.onEnter) || null;
  resize();
  new ResizeObserver(() => { resize(); draw(); }).observe(canvas);
  for (const topic of ['state', 'view', 'playhead', 'markers']) bus.on(topic, draw);

  /* ===== UI 修正轮3：标尺交互——左键单击/拖动 = 同一逻辑定位播放起点（跟手 seek）；
     右键拖动 = 划循环区间（拍级吸附，Alt 自由）；右键拖端点=调区间 / 拖区间中=整段移动 / 右键双击=清除 ===== */
  let rdrag = null;
  let lastRC = { t: 0, x: 0 };
  canvas.addEventListener('contextmenu', (e) => { if (e.clientX - canvas.getBoundingClientRect().left >= KEYS_W) e.preventDefault(); });
  canvas.addEventListener('mousedown', (e) => {
    if (e.offsetX < KEYS_W) return;
    const t = Math.max(0, tOf(e.offsetX));
    if (e.button === 0) {                     /* 左键：按下即定位；拖动持续跟手（同一 seekTo 逻辑） */
      rdrag = { mode: 'seek', t0: t, last: 0 };
      seekTo(t);
      return;
    }
    if (e.button !== 2) return;
    const now = performance.now();            /* 右键双击（同点、350ms 内）= 清除循环区间 */
    if (now - lastRC.t < 350 && Math.abs(e.offsetX - lastRC.x) < 6) {
      lastRC = { t: 0, x: 0 };
      const L0 = store.loop;
      if (L0 && e.offsetX >= xOf(L0.start) && e.offsetX <= xOf(L0.end)) {
        setLoop(null);
        setLoopOn(false);
        bus.dispatch('toast', '已清除循环区间');
      }
      return;
    }
    lastRC = { t: now, x: e.offsetX };
    const L = store.loop;                     /* 右键：把手 / 整段 / 新建 */
    if (L) {
      const xa = xOf(L.start), xb = xOf(L.end);
      if (Math.abs(e.offsetX - xa) <= 5) { rdrag = { mode: 'a', t0: t, loop0: { start: L.start, end: L.end }, moved: false }; return; }
      if (Math.abs(e.offsetX - xb) <= 5) { rdrag = { mode: 'b', t0: t, loop0: { start: L.start, end: L.end }, moved: false }; return; }
      if (e.offsetX > xa + 5 && e.offsetX < xb - 5) { rdrag = { mode: 'move', t0: t, loop0: { start: L.start, end: L.end }, moved: false }; return; }
    }
    rdrag = { mode: 'new', t0: snapT(t, e), moved: false };
  });
  window.addEventListener('mousemove', (e) => {
    if (!rdrag) return;
    const r = canvas.getBoundingClientRect();
    if (e.clientY < r.top - 60 || e.clientY > r.bottom + 60) return;
    const traw = Math.max(0, tOf(e.clientX - r.left));
    if (rdrag.mode === 'seek') {              /* 跟手定位（~60fps 节流） */
      const now = performance.now();
      if (now - rdrag.last < 16) return;
      rdrag.last = now;
      seekTo(traw);
      return;
    }
    const t = snapT(traw, e);                 /* UI 修正轮3：拍级吸附（Alt = 自由） */
    if (!rdrag.moved && Math.abs(e.clientX - r.left - xOf(rdrag.t0)) < 3) return;
    rdrag.moved = true;
    if (rdrag.mode === 'new') setLoop({ start: Math.min(rdrag.t0, t), end: Math.max(rdrag.t0, t) });
    else if (rdrag.mode === 'a') setLoop({ start: Math.max(0, Math.min(t, rdrag.loop0.end - 0.05)), end: rdrag.loop0.end });
    else if (rdrag.mode === 'b') setLoop({ start: rdrag.loop0.start, end: Math.max(t, rdrag.loop0.start + 0.05) });
    else if (rdrag.mode === 'move') {
      const d = traw - rdrag.t0;
      const ns = Math.max(0, snapT(rdrag.loop0.start + d, e));
      setLoop({ start: ns, end: ns + (rdrag.loop0.end - rdrag.loop0.start) });
    }
  });
  window.addEventListener('mouseup', (e) => {
    if (!rdrag) return;
    const d = rdrag;
    rdrag = null;
    if (d.mode === 'seek') {                  /* 左键：松手收尾精准定位一次 */
      seekTo(Math.max(0, tOf(e.clientX - canvas.getBoundingClientRect().left)));
      return;
    }
    if (!d.moved) return;                     /* 右键单击（未拖动）：不动 */
    if (!store.loopOn) setLoopOn(true);       /* 划出/调整区间 → 自动打开循环 */
  });

  /* 修正轮2：视图模式切换 → 重画行（主轨/叠加标记） */
  bus.on('viewmode', () => renderTracks(trackListEl));
  bus.on('agenttracks', () => renderTracks(trackListEl));   // 批B B1-3：改动高亮
  bus.on('markers', () => renderTracks(trackListEl));       // M-V8 E1：文件夹折叠 / 作用域选中


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
