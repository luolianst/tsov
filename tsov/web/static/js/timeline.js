/* timeline.js —— 时间标尺（小节/拍/秒）+ 轨道头面板（M1b：色条/名/参数控件/选中）+ 乐句 segments 底条 */

import { bus } from './events.js';
import { api } from './api.js';
import { KEYS_W, store, tempo, beatsPerBar, segments, scoreBounds, setSelection, setError, toggleOverlay, refTag, bookmarks, folderTracks, setLoop, setLoopOn, setSelBookmark, setSelFolder, toggleFolderCollapse, setSingleTrack } from './state.js';
import { pal, trackColors } from './theme.js';
import { seekTo } from './playback.js';

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
  /* M-V8 E1：文件夹分组——记录每个文件夹的首个轨道下标（分组行插在那之前） */
  const firstOf = {};
  store.score.tracks.forEach((tr, i) => { if (tr.folder && firstOf[tr.folder] === undefined) firstOf[tr.folder] = i; });
  store.score.tracks.forEach((tr, ti) => {
    const isMain = single && store.singleTrack === ti;
    const isOverlay = store.overlayTracks.has(ti);
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
      hd.title = '单击：选中该文件夹（M 键给它建旗）｜ ▾ 折叠/展开';
      hd.addEventListener('click', () => setSelFolder(store.selFolder === tr.folder ? '' : tr.folder));
      el.appendChild(hd);
    }
    const item = document.createElement('div');
    item.className = 'track-item' + (store.hiddenTracks.has(ti) ? ' hidden-track' : '') +
      (tr.folder && store.collapsedFolders.has(tr.folder) ? ' fold-hidden' : '') +
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
      ' ｜ 双击：' + (single ? '切换主轨' : '进入单轨写谱') + ' ｜ 右键：轨道菜单';

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
    /* M-V8 E1：右键 = 轨道操作菜单（文件夹归属；小修包加：重命名 / 删除轨道；全部走命令层） */
    item.addEventListener('contextmenu', (e) => {
      e.preventDefault();
      if (!store.project) return;
      const cur = tr.folder || '';
      const tname = tr.name || ('track ' + ti);
      const nTracks = store.score ? store.score.tracks.length : 0;
      openBmMenu(e.clientX, e.clientY, [
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
  });
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

function openBmMenu(x, y, items) {
  closeBmMenu();
  menuEl = document.createElement('div');
  menuEl.className = 'bm-menu';
  menuEl.style.left = Math.min(x, window.innerWidth - 140) + 'px';
  menuEl.style.top = Math.min(y, window.innerHeight - 30 - items.length * 24) + 'px';
  for (const it of items) {
    const b = document.createElement('button');
    b.textContent = it.label;
    b.addEventListener('click', () => { closeBmMenu(); it.fn(); });
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
    mdrag = { i: -1, mode: 'new', t0: t, cur: { start: t, end: t }, moved: false };
  });

  window.addEventListener('mousemove', (e) => {
    if (!mdrag) return;
    const r = mcanvas.getBoundingClientRect();
    if (e.clientY < r.top - 60 || e.clientY > r.bottom + 60) return;
    const t = Math.max(0, tOf(e.clientX - r.left));
    if (!mdrag.moved && Math.abs(e.clientX - r.left - xOf(mdrag.t0)) < 3) return;
    mdrag.moved = true;
    const c = mdrag.cur;
    if (mdrag.mode === 'new') { c.start = Math.min(mdrag.t0, t); c.end = Math.max(mdrag.t0, t); }
    else if (mdrag.mode === 'body') {
      const d = t - mdrag.t0;
      c.start = Math.max(0, mdrag.b0.start + d);
      c.end = Math.max(0, mdrag.b0.end + d);
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

  /* ===== M-V8 E1：标尺交互——单击=定位 ｜ 拖动=划循环区间 ｜ 拖把手=调区间 ｜ 双击区间=清除 ===== */
  let rdrag = null;
  canvas.addEventListener('mousedown', (e) => {
    if (e.button !== 0 || e.offsetX < KEYS_W) return;
    const t = Math.max(0, tOf(e.offsetX));
    const L = store.loop;
    if (L) {
      const xa = xOf(L.start), xb = xOf(L.end);
      if (Math.abs(e.offsetX - xa) <= 5) { rdrag = { mode: 'a', t0: t, loop0: { start: L.start, end: L.end }, moved: false }; return; }
      if (Math.abs(e.offsetX - xb) <= 5) { rdrag = { mode: 'b', t0: t, loop0: { start: L.start, end: L.end }, moved: false }; return; }
      if (e.offsetX > xa + 5 && e.offsetX < xb - 5) { rdrag = { mode: 'move', t0: t, loop0: { start: L.start, end: L.end }, moved: false }; return; }
    }
    rdrag = { mode: 'new', t0: t, moved: false };
  });
  window.addEventListener('mousemove', (e) => {
    if (!rdrag) return;
    const r = canvas.getBoundingClientRect();
    if (e.clientY < r.top - 60 || e.clientY > r.bottom + 60) return;
    const t = Math.max(0, tOf(e.clientX - r.left));
    if (!rdrag.moved && Math.abs(e.clientX - r.left - xOf(rdrag.t0)) < 3) return;
    rdrag.moved = true;
    if (rdrag.mode === 'new') setLoop({ start: Math.min(rdrag.t0, t), end: Math.max(rdrag.t0, t) });
    else if (rdrag.mode === 'a') setLoop({ start: Math.max(0, Math.min(t, rdrag.loop0.end - 0.05)), end: rdrag.loop0.end });
    else if (rdrag.mode === 'b') setLoop({ start: rdrag.loop0.start, end: Math.max(t, rdrag.loop0.start + 0.05) });
    else if (rdrag.mode === 'move') {
      const d = t - rdrag.t0;
      setLoop({ start: Math.max(0, rdrag.loop0.start + d), end: Math.max(0, rdrag.loop0.end + d) });
    }
  });
  window.addEventListener('mouseup', () => {
    if (!rdrag) return;
    const d = rdrag;
    rdrag = null;
    if (!d.moved) { seekTo(d.t0); return; }          // 单击 = 定位
    if (!store.loopOn) setLoopOn(true);              // 划出/调整区间 → 自动打开循环
  });
  canvas.addEventListener('dblclick', (e) => {
    if (e.offsetX < KEYS_W) return;
    const L = store.loop;
    if (!L) return;
    if (e.offsetX >= xOf(L.start) && e.offsetX <= xOf(L.end)) {
      setLoop(null);
      setLoopOn(false);
      bus.dispatch('toast', '已清除循环区间');
    }
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
