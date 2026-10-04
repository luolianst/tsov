/* main.js —— 启动：加载工程列表 → 打开工程 → 挂各模块（模块间只经 api/state/events 交互） */

import { api } from './api.js';
import { bus, connectEvents } from './events.js';
import { store, setState, setError, toast, fitView, setView, clearDiff, setAnnotations, clearAnnotations, setSnap, setViewMode, setSingleTrack, fitViewTrack, refTag, favSource, bookmarks, setSelBookmark, setLoopOn, splitPartner, audioClipsOf } from './state.js';
import * as roll from './roll.js';
import * as timeline from './timeline.js';
import * as dock from './dock.js';
import * as lanes from './lanes.js';
import { initTheme, setTheme, themeName, trackColors } from './theme.js';
import { initDiffBadge } from './diff.js';
import * as chat from './chat.js';
import * as playback from './playback.js';
import * as tools from './tools.js';
import * as autoroll from './autoroll.js';
import { initStepsPanel } from './steps.js';

const $ = (id) => document.getElementById(id);

/* ---------------- 状态栏 / 工具栏 ---------------- */

function refreshStatusBar() {
  const first = (store.summary || '').split('\n')[0] || '（无摘要）';
  $('status-summary').textContent = store.project ? (store.project + ' · ' + first) : '未打开工程';
}

function refreshToolbar() {
  $('btn-undo').disabled = !store.history.can_undo;
  $('btn-redo').disabled = !store.history.can_redo;
  const sel = $('project-select');
  sel.innerHTML = '';
  if (!store.projects.length) {
    /* M-V6 批1：空列表引导（不再是无信息的空下拉） */
    const opt = document.createElement('option');
    opt.value = '';
    opt.textContent = '（无工程——「＋ 新建」或「📥 导入」）';
    sel.appendChild(opt);
    return;
  }
  for (const p of store.projects) {
    const opt = document.createElement('option');
    opt.value = p.name;
    const title = (p.title && p.title !== p.name) ? ' — ' + p.title : '';
    opt.textContent = p.name + title + ' · ' + p.tracks + '轨 · ' + p.notes + '音';
    opt.title = '最近修改：' + new Date((p.mtime || 0) * 1000).toLocaleString();
    if (p.name === store.project) opt.selected = true;
    sel.appendChild(opt);
  }
}

let toastTimer = 0;
function showError(msg) {
  $('status-error').textContent = msg || '';
  if (msg) console.warn('[tsov]', msg);
}
function showToast(msg) {
  if (!msg) return;
  refreshStatusBar();   // toast 并入状态栏展示（原型极简）
  $('status-summary').textContent = 'ℹ ' + msg;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(refreshStatusBar, 4000);
}

/* ---------------- 工程列表 / 打开 ---------------- */

async function loadProjects(keep) {
  try {
    const r = await api.listProjects();
    store.projects = r.projects || [];
    const names = store.projects.map((p) => p.name);
    if (!keep && store.projects.length && !names.includes(store.project)) {
      /* M-V6 批1：后端按 mtime 倒序 → [0] = 最近修改工程（不再无脑开列表第一个） */
      await openProject(store.projects[0].name);
    } else {
      refreshToolbar();
    }
  } catch (e) { setError(e.message); }
}

async function openProject(name) {
  if (!name) return;
  try {
    const s = await api.getState(name);
    setState(s, { preserveSelection: false });
    clearDiff();   // 审计修 M-V2.3：清掉上一个工程的 diff 徽章（切工程残留实测）
    store.project = name;
    connectEvents(name);
    chat.switchProject(name);
    /* 修正轮2：切工程 → 回到总谱视图（单轨态不带过去） */
    setViewMode('lanes');
    $('stage-head').hidden = true;
    $('tools-bar').hidden = true;   /* E3 段1 修：工具条残留（+30px 占位/遮挡） */
    $('lanes').hidden = false;
    $('roll').hidden = true;
    bus.dispatch('state');
    requestAnimationFrame(() => {
      const rc = $('stage-body').getBoundingClientRect();
      fitView(rc.width, rc.height);
      lanes.resizeNow();
    });
    toast('已打开工程 ' + name);
    /* M-V7 D1：打开工程时做一次 stem 缓存 GC（无引用即清 + 清退役 .render-cache） */
    api.cacheGc(name).catch(() => {});
  } catch (e) { setError(e.message); }
}

async function newProject() {
  const name = prompt('新工程名（将创建 output/<name>/ 并 git init）：', '');
  if (!name) return;
  try {
    await api.createProject(name.trim());
    await loadProjects(false);
    await openProject(name.trim());
  } catch (e) { setError(e.message); }
}

/* ---------------- M-V6 批1：导入 score json → 工程 ---------------- */

function applyImportCandidate(c) {
  if (!c) return;
  $('import-project-path').value = c.path;
  $('import-project-name').value = c.name_hint || '';
}

async function showImportRow() {
  const row = $('import-project-row');
  const sel = $('import-project-select');
  row.hidden = false;
  sel.innerHTML = '<option value="">（扫描中…）</option>';
  try {
    const r = await api.importCandidates();
    const cands = r.candidates || [];
    sel.innerHTML = '';
    if (!cands.length) {
      const opt = document.createElement('option');
      opt.value = '';
      opt.textContent = '（output/ 下没有可导入的 score json——可手动填路径）';
      sel.appendChild(opt);
      return;
    }
    for (const c of cands) {
      const opt = document.createElement('option');
      opt.value = c.path;
      opt.dataset.nameHint = c.name_hint || '';
      opt.textContent = c.path + '（' + c.title + ' · ' + c.tracks + '轨 · ' + c.notes + '音）';
      sel.appendChild(opt);
    }
    applyImportCandidate(cands[0]);
  } catch (e) { setError(e.message); }
}

async function doImportProject() {
  const source = $('import-project-path').value.trim() || $('import-project-select').value;
  const name = $('import-project-name').value.trim();
  if (!source) { setError('先选候选，或填 output/ 内的 json 路径'); return; }
  try {
    const r = await api.importProject(source, name || null);
    $('import-project-row').hidden = true;
    toast('已导入「' + r.source + '」→ 工程 ' + r.name);
    await loadProjects(true);
    await openProject(r.name);
  } catch (e) { setError(e.message); }
}

async function renameProject() {
  if (!store.project) { setError('先打开一个工程'); return; }
  const cur = (store.score && store.score.title) || store.project;
  const title = prompt('新的工程显示名（score 标题，一个 commit，可撤销）：', cur);
  if (!title || !title.trim()) return;
  try {
    const r = await api.setTitle(store.project, title.trim());
    toast('已改名：' + r.title + ' ' + refTag(r));
  } catch (e) { setError(e.message); }
}

/* ---------------- M-V8 E2：音频素材导入（本机路径 / 浏览器文件 / 拖拽） ---------------- */

function openAudioImport() {
  if (!store.project) { setError('先打开一个工程'); return; }
  const p = prompt('本机音频路径（留空则打开文件选择框；支持 wav/mp3/flac/m4a/ogg…）：', '');
  if (p === null) return;
  if (p.trim()) { importAudioByPath(p.trim()); return; }
  const inp = $('audio-file-input');
  if (inp) inp.click();
}

async function importAudioByPath(path) {
  if (!store.project) { setError('先打开一个工程'); return; }
  try {
    const info = await api.importAudioPath(store.project, path);
    await attachAudio(info, String(path).split(/[\\/]/).pop());
  } catch (e) { setError('导入失败：' + e.message); }
}

async function importAudioFile(file) {
  if (!store.project) { setError('先打开一个工程'); return; }
  try {
    const info = await api.importAudioUpload(store.project, file);
    await attachAudio(info, file.name);
  } catch (e) { setError('导入失败：' + e.message); }
}

/** 入库成功后接命令层：add_audio_track 追加音频轨（与 agent 同一动作路径）。 */
async function attachAudio(info, fallbackName) {
  const nm = String(info.title || fallbackName || '').replace(/\.[^.]+$/, '');
  const r = await api.postBatch(store.project, '导入音频',
    [{ op: 'add_audio_track', value: { file: info.file, name: nm || null } }], '素材：' + info.file);
  if (!r.applied) { setError('被拒：' + (r.errors || []).join('；')); return; }
  bus.dispatch('toast', '已导入素材「' + (nm || info.file) + '」' +
    (info.deduped ? '（复用已有文件）' : '') + ' · ' + Number(info.seconds).toFixed(2) + 's');
}

/* ---------------- M-V8 E5：MIDI 导入（本机路径 / 浏览器文件） ---------------- */

function openMidiImport() {
  if (!store.project) { setError('先打开一个工程'); return; }
  const p = prompt('本机 MIDI 路径（留空则打开文件选择框；支持 .mid/.midi）：', '');
  if (p === null) return;
  if (p.trim()) { importMidiByPath(p.trim()); return; }
  const inp = $('midi-file-input');
  if (inp) inp.click();
}

async function importMidiByPath(path) {
  if (!store.project) { setError('先打开一个工程'); return; }
  try {
    const r = await api.importMidiPath(store.project, path);
    afterMidiImport(r);
  } catch (e) { setError('MIDI 导入失败：' + e.message); }
}

async function importMidiFile(file) {
  if (!store.project) { setError('先打开一个工程'); return; }
  try {
    const r = await api.importMidiUpload(store.project, file);
    afterMidiImport(r);
  } catch (e) { setError('MIDI 导入失败：' + e.message); }
}

function afterMidiImport(r) {
  const names = (r.tracks || []).join('、');
  bus.dispatch('toast', '已导入 MIDI：+' + r.added + ' 轨 / ' + r.notes + ' 音' +
    (names ? '（' + names + '）' : '') + (r.adopted ? ' · 已采纳文件元数据' : '') +
    (r.skipped ? ' · 跳过空轨 ' + r.skipped : ''));
}

/* ---------------- 启动 ---------------- */

function boot() {
  initTheme();   // UI 批A：浅色默认（?theme= / localStorage 可覆盖）
  roll.init($('roll'));
  lanes.init($('lanes'), { onEnter: enterSingle, onDropAudio: importAudioFile });
  /* UI 修正轮3.2：把左栏真实行布局（文件夹行/折叠/行高差异）推给卷帘——两栏逐行匹配 */
  function syncLaneRows() {
    const sc = $('tracks-scroll'); const list = $('track-list');
    if (!sc || !list) return;
    const scTop = sc.getBoundingClientRect().top;
    const base = sc.scrollTop;   /* 行内容坐标（相对 scroller 内容顶）= rect.top - scTop + scrollTop */
    const map = {};
    for (const el of list.querySelectorAll('.track-item')) {
      const ti = Number(el.dataset.track);
      if (!Number.isFinite(ti)) continue;
      if (!el.getClientRects().length) { map[ti] = null; continue; }   // 折叠/隐藏：不占行
      map[ti] = Math.round(el.getBoundingClientRect().top - scTop + base);
    }
    lanes.setRowLayout(Object.keys(map).length ? map : null);
  }
  bus.on('rowlayout', syncLaneRows);   /* 须在 timeline.init 前注册（捕获首渲染） */
  window.addEventListener('resize', () => requestAnimationFrame(syncLaneRows));
  timeline.init($('ruler'), $('track-list'), $('segments-info'), $('meta-info'), { onEnter: enterSingle });
  /* UI 修正轮3：左栏滚动 → 卷帘同步纵向滚动（卷帘自身滚轮=横向/缩放，语义不变；3.1：范围与左栏一致） */
  $('tracks-scroll').addEventListener('scroll', () => { const ts = $('tracks-scroll'); lanes.setScrollY(ts.scrollTop, ts.scrollHeight - ts.clientHeight); });
  timeline.initMarkers($('markers'));   // M-V8 E1：段道
  /* 修正轮2.1：段轨已删（洛怜：段落/和弦两行可以删；后续以「书签」替代，见 Q46） */
  dock.init($('dock'));
  initDiffBadge($('status-diff'));
  chat.init({
    logEl: $('chat-log'),
    inputEl: $('chat-input'),
    sendBtn: $('btn-send'),
    undoRoundBtn: $('btn-undo-round'),
    rollbackSel: $('rollback-select'),
    rollbackBtn: $('btn-rollback'),
    stopBtn: $('btn-stop-agent'),
    newSessionBtn: $('btn-new-session'),
    importBtn: $('btn-import-session'),
    importRow: $('import-row'),
    importSelect: $('import-select'),
    importConfirm: $('btn-import-confirm'),
    importCancel: $('btn-import-cancel'),
    chipsEl: $('chat-chips'),
    cmpSel: $('cmp-base'),
  });
  playback.init({
    playWavBtn: $('btn-play-wav'),
    stopBtn: $('btn-stop'),
    playHostBtn: $('btn-play-host'),
    renderBtn: $('btn-render'),
    loopBtn: $('btn-loop'),      // M-V8 E1：循环开关
    metroBtn: $('btn-metro'),    // M-V8 E1：节拍器开关
  });
  /* M-V8 E5：编辑工具集（工具条 / 快捷键 / 剪贴板 / 状态栏 chips） */
  tools.init({
    bar: $('tools-bar'),
    snap: $('tl-snap'),
    grid: $('tl-grid'),
    swing: $('tl-swing'),
    quant: $('tl-quant'),
    chips: $('status-chips'),
  });
  autoroll.init($('autoroll'));   // M-V8 E5 段2：自动化 lane（单轨视图底部子道）

  /* ================= 修正轮2：菜单 / 面板收起 / 视图模式 / 导出 / 收藏 ================= */

  /* ---- 二级菜单（文件 / 设置）；点外部或 Esc 关闭 ---- */
  const menuFile = $('menu-file'), menuSet = $('menu-set');
  function closeMenus(except) {
    for (const m of [menuFile, menuSet]) if (m && m !== except) m.hidden = true;
  }
  function toggleMenu(pop) {
    const show = pop.hidden;
    closeMenus(pop);
    pop.hidden = !show;
  }
  $('btn-menu-file').addEventListener('click', (e) => { e.stopPropagation(); toggleMenu(menuFile); });
  $('btn-menu-set').addEventListener('click', (e) => { e.stopPropagation(); toggleMenu(menuSet); if (!menuSet.hidden) { loadRetentionSet(); loadLlmSet(); } });
  document.addEventListener('click', (e) => {
    if (!e.target.closest('.menu-pop') && !e.target.closest('.menu-btn')) closeMenus(null);
  });

  $('mi-new').addEventListener('click', () => { closeMenus(null); newProject(); });
  $('mi-rename').addEventListener('click', () => { closeMenus(null); renameProject(); });
  $('mi-import').addEventListener('click', (e) => { e.stopPropagation(); showImportRow(); });
  $('mi-import-audio').addEventListener('click', () => { closeMenus(null); openAudioImport(); });
  const audioInp = $('audio-file-input');
  if (audioInp) audioInp.addEventListener('change', () => {
    const f = audioInp.files && audioInp.files[0];
    audioInp.value = '';
    if (f) importAudioFile(f);
  });
  $('mi-import-midi').addEventListener('click', () => { closeMenus(null); openMidiImport(); });
  const midiInp = $('midi-file-input');
  if (midiInp) midiInp.addEventListener('change', () => {
    const f = midiInp.files && midiInp.files[0];
    midiInp.value = '';
    if (f) importMidiFile(f);
  });
  $('mi-refresh').addEventListener('click', () => { closeMenus(null); loadProjects(true); });
  $('mi-export').addEventListener('click', () => { closeMenus(null); openExport(); });
  $('mi-fav').addEventListener('click', () => { closeMenus(null); doFavorite(); });
  $('mi-fav-restore').addEventListener('click', () => { closeMenus(null); openFavDlg(); });
  $('mi-settings').addEventListener('click', () => { closeMenus(null); menuSet.hidden = false; loadRetentionSet(); loadLlmSet(); });

  /* ---- 主题（◐ 与设置菜单同步） ---- */
  $('btn-theme').addEventListener('click', () => { setTheme(themeName() === 'dark' ? 'light' : 'dark'); closeMenus(null); });
  const setThemeSel = $('set-theme');
  function syncThemeSel() { if (setThemeSel) setThemeSel.value = (themeName() === 'dark') ? 'dark' : 'light'; }
  if (setThemeSel) setThemeSel.addEventListener('change', () => setTheme(setThemeSel.value));
  window.addEventListener('tsov-theme', () => { syncThemeSel(); bus.dispatch('view'); });
  syncThemeSel();
  const setOut = $('set-out');
  if (setOut) setOut.addEventListener('change', () => toast('输出：' + setOut.value + '（ASIO / 低延迟后续迭代）'));

  /* ---- 面板收起（窄边条；默认全开；独立不互斥；记忆 localStorage） ---- */
  function applyFold(panel, folded) {
    document.body.classList.toggle(panel === 'tracks' ? 'tracks-folded' : 'chat-folded', folded);
    try { localStorage.setItem('tsov.fold.' + panel, folded ? '1' : '0'); } catch (e) { /* ignore */ }
    // 中栏尺寸变化 → 画布重算
    requestAnimationFrame(() => { roll.resizeNow(); lanes.resizeNow(); });
  }
  for (const [panel, btnId, railId] of [['tracks', 'btn-fold-tracks', 'rail-tracks'], ['chat', 'btn-fold-chat', 'rail-chat']]) {
    let saved = null;
    try { saved = localStorage.getItem('tsov.fold.' + panel); } catch (e) { /* ignore */ }
    applyFold(panel, saved === '1');
    $(btnId).addEventListener('click', () => applyFold(panel, true));
    $(railId).addEventListener('click', () => applyFold(panel, false));
  }

  /* ---- 总谱 ↔ 单轨写谱（修正轮2） ---- */
  function renderSingleHead() {
    const ti = store.singleTrack;
    const tr = store.score && store.score.tracks[ti];
    const tc = trackColors();
    $('single-name').textContent = tr ? (tr.name || ('track ' + ti)) : '—';
    $('single-chip').style.background = tc[ti % tc.length];
    const inst = (tr && tr.instrument) || {};
    let info = '';
    if (tr) {
      if (tr.kind === 'audio') {
        const cls = audioClipsOf(tr);
        const fn = cls.length ? String(cls[0].file).split('/').pop() : '';
        info = '音频轨 · ' + fn + (cls.length > 1 ? ' ×' + cls.length : '');
      } else {
        info = tr.notes.length + ' 音 · ' + (inst.program || 'default');
      }
    }
    /* E3 段1：分屏对象提示（叠加：轨名） */
    const sp = splitPartner();
    if (tr && sp) {
      const ov = store.score.tracks[sp.ti];
      info += ' ｜ 叠加：' + ((ov && ov.name) || ('track ' + sp.ti));
    }
    $('single-info').textContent = info;
  }
  function enterSingle(ti) {
    setViewMode('single', ti);
    setSingleTrack(ti);
    $('stage-head').hidden = false;
    $('tools-bar').hidden = false;
    $('lanes').hidden = true;
    $('roll').hidden = false;
    const h = $('stage-body').getBoundingClientRect().height;
    /* E3 段1：分屏时卷帘按上区高适配；音频主轨跳过音域适配（波形区自适应当前区高） */
    const trk = store.score && store.score.tracks[ti];
    if (trk && trk.kind !== 'audio') {
      const sp = splitPartner();
      fitViewTrack(ti, sp ? Math.max(60, h * store.splitRatio) : h);
    }
    roll.resizeNow();
    renderSingleHead();
  }
  function backToLanes() {
    if (store.viewMode !== 'single') return;
    setViewMode('lanes');
    $('stage-head').hidden = true;
    $('tools-bar').hidden = true;
    $('lanes').hidden = false;
    $('roll').hidden = true;
    const rc = $('stage-body').getBoundingClientRect();
    fitView(rc.width, rc.height);
    lanes.resizeNow();
  }
  $('btn-back-lanes').addEventListener('click', backToLanes);
  /* E3 段1：单轨内主轨/叠加变化（双击交换等）→ 头部同步 */
  bus.on('viewmode', () => { if (store.viewMode === 'single') renderSingleHead(); });

  /* M-V8 E1：M 键建旗（选中文件夹 > 单轨选中 > 项目层；Shift+M 强制项目层） */
  async function addFlag(forceProject) {
    if (!store.project || !store.score) { setError('先打开一个工程'); return; }
    const t = Math.round(Math.max(0, store.playhead) * 1000) / 1000;
    let scope = 'project', ref = '';
    if (!forceProject) {
      if (store.selFolder) { scope = 'folder'; ref = store.selFolder; }
      else {
        const ti = (store.viewMode === 'single') ? store.singleTrack : store.selection.track;
        const tr = (ti >= 0 && store.score.tracks[ti]) ? store.score.tracks[ti] : null;
        if (tr) { scope = 'track'; ref = tr.name; }
      }
    }
    const len = bookmarks().length;
    const label = scope === 'project' ? '项目记号' : (scope === 'folder' ? ('文件夹记号 ' + ref) : ('轨道记号 ' + ref));
    try {
      const r = await api.postBatch(store.project, '书签', [{ op: 'add_bookmark', track: 0, value: { scope, ref, kind: 'mark', start: t, label: '' } }], null);
      if (!r.applied) { setError('被拒：' + (r.errors || []).join('；')); return; }
      toast('已建旗：' + label + ' @ ' + t + 's（回车命名）');
      const mr = $('markers').getBoundingClientRect();
      timeline.openRenameAt(len, mr.left + 120, Math.max(40, mr.top - 30));
    } catch (e) { setError(e.message); }
  }
  window.__tsovAddFlag = addFlag;   // CDP 实测用（等价键盘路径）

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      closeMenus(null);
      if (!$('dlg-export').hidden) { $('dlg-export').hidden = true; return; }
      if (!$('dlg-fav').hidden) { $('dlg-fav').hidden = true; return; }
      if (!$('dlg-del').hidden) { $('dlg-del').hidden = true; return; }
      if (!$('dlg-keys').hidden) { $('dlg-keys').hidden = true; return; }
      if (tools.cancelTool()) return;   // M-V8 E5：工具非智能指针 → 先回智能指针
      backToLanes();
      return;
    }
    /* M-V8 E1：播放/定位快捷键（输入态不劫持；按钮焦点仅对 Space 让位原生激活） */
    const tgt = e.target || {};
    const tag = (tgt.tagName || '').toUpperCase();
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || tgt.isContentEditable) return;
    /* M-V8 E6 段3：? = 快捷键浮层开关（无工程也可查） */
    if (e.key === '?') { e.preventDefault(); toggleKeys(); return; }
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (!store.project) return;
    if (e.key === ' ') {
      if (tag === 'BUTTON') return;
      e.preventDefault();
      if (store.playing === 'wav') playback.stopAll();
      else playback.playWav();
    } else if (e.key === 'Home') {
      e.preventDefault();
      playback.rewind();
    } else if (e.key === 'l' || e.key === 'L') {
      if (!store.loop) { setError('先在标尺上右键拖动划循环区间'); return; }
      setLoopOn(!store.loopOn);
      toast(store.loopOn ? '循环开' : '循环关');
    } else if (e.key === 'm' || e.key === 'M') {
      addFlag(e.shiftKey);
    }
  });

  /* ---- M-V8 E6 段2：保存状态徽章（每次编辑即写盘；异常中断可经「回滚」恢复） ---- */
  const saveEl = $('save-state');
  let saveDirty = 0;
  function renderSaved() {
    if (saveDirty > 0) { saveEl.textContent = '保存中…'; saveEl.classList.add('saving'); return; }
    saveEl.classList.remove('saving');
    const at = store.savedAt;
    if (!at) { saveEl.textContent = '—'; return; }
    const d = new Date(at * 1000);
    const z = (n) => String(n).padStart(2, '0');
    saveEl.textContent = '已保存 ' + z(d.getHours()) + ':' + z(d.getMinutes()) + ':' + z(d.getSeconds());
    saveEl.title = '每次编辑即写盘（score.json）· 异常中断可用「版本/回滚」恢复';
  }
  bus.on('state', renderSaved);
  bus.on('save_begin', () => { saveDirty += 1; renderSaved(); });
  bus.on('save_done', (d) => {
    saveDirty = Math.max(0, saveDirty - 1);
    if (d && d.ok && d.saved_at) store.savedAt = Number(d.saved_at) || store.savedAt;
    renderSaved();
  });
  renderSaved();

  /* ---- M-V8 E6 段3：删除工程（安全删 → output/.trash；二次确认） ---- */
  const dlgDel = $('dlg-del');
  function openDelete() {
    if (!store.project) { setError('先打开一个工程'); return; }
    $('del-name').textContent = store.project;
    const res = $('del-result');
    res.hidden = true; res.textContent = '';
    dlgDel.hidden = false;
  }
  $('mi-delete').addEventListener('click', () => { closeMenus(null); openDelete(); });
  $('del-cancel').addEventListener('click', () => { dlgDel.hidden = true; });
  $('del-run').addEventListener('click', async () => {
    if (!store.project) return;
    const name = store.project;
    const btn = $('del-run');
    btn.disabled = true; btn.textContent = '删除中…';
    try {
      await api.deleteProject(name);
      dlgDel.hidden = true;
      toast('已删除「' + name + '」→ .trash（可找回）');
      await loadProjects(false);   /* 当前工程已不在列表 → 自动打开最近一个 */
      if (!store.projects.length) location.reload();   /* 一个不剩 → 冷启动空态 */
    } catch (e) {
      const res = $('del-result');
      res.hidden = false; res.textContent = '✗ ' + e.message;
    } finally {
      btn.disabled = false; btn.textContent = '删除工程';
    }
  });

  /* ---- M-V8 E6 段3：快捷键浮层（纯展示；内容 = 已实现键位全景） ---- */
  const dlgKeys = $('dlg-keys');
  const KEYMAP = [
    ['播放 / 定位（全局）', [
      ['Space', '播放 / 暂停'],
      ['Home', '回到起点'],
      ['L', '循环开 / 关（先划区间）'],
      ['M / Shift+M', '建书签旗 / 项目记号'],
    ]],
    ['工具（单轨详情）', [
      ['1 – 5', '智能 / 范围 / 剪刀 / 胶水 / 橡皮'],
      ['Esc', '回智能指针 · 关菜单 · 退出单轨'],
      ['按住 Alt', '临时剪刀'],
      ['← / →', '微推一格（Shift = 一小节）'],
      ['Ctrl+C / X / V / D', '复制 / 剪切 / 粘贴 / 再制'],
    ]],
    ['鼠标（时间轴 / 卷帘）', [
      ['标尺左键单击 / 拖动', '定位播放起点'],
      ['标尺右键拖动', '划 / 调循环区间'],
      ['双击轨道头', '进单轨；双击音频波段 = 试听'],
      ['Ctrl / Shift + 单击轨道头', '多选对照（叠加显示）'],
      ['拖动 clip / 音符', '移动；拖音符左右缘 = 改时长'],
      ['拖 clip 左 / 右缘', '修剪入点 / 出点'],
      ['拖 clip 上缘两角', '淡入 / 淡出'],
      ['Alt + 拖 clip 右缘', '时间伸缩（0.5×–2×）'],
      ['剪刀单击 / Alt + 单击', '切分'],
      ['Ctrl + 滚轮', '时间缩放（Alt + 滚轮 = 纵向滚动）'],
    ]],
    ['本浮层', [
      ['?', '打开 / 关闭速查'],
    ]],
  ];
  function buildKeys() {
    const body = $('keys-body');
    body.innerHTML = '';
    let n = 0;
    for (const [group, rows] of KEYMAP) {
      const h = document.createElement('div');
      h.className = 'k-group';
      h.textContent = group;
      body.appendChild(h);
      for (const [k, desc] of rows) {
        const row = document.createElement('div');
        row.className = 'k-row';
        const kb = document.createElement('kbd');
        kb.textContent = k;
        const dd = document.createElement('span');
        dd.textContent = desc;
        row.appendChild(kb); row.appendChild(dd);
        body.appendChild(row);
        n += 1;
      }
    }
    dlgKeys.dataset.rows = String(n);
    return n;
  }
  function toggleKeys(force) {
    const show = force === undefined ? dlgKeys.hidden : !!force;
    if (show) buildKeys();
    dlgKeys.hidden = !show;
    return show;
  }
  $('btn-keys').addEventListener('click', () => toggleKeys());
  $('keys-close').addEventListener('click', () => toggleKeys(false));
  window.__tsovKeys = { toggle: toggleKeys, count: () => Number(dlgKeys.dataset.rows) || 0 };   // CDP 实测用

  /* ---- 导出弹窗（三级；接 host export_matrix） ---- */
  const dlgExport = $('dlg-export');
  function openExport() {
    if (!store.project) { setError('先打开一个工程'); return; }
    /* E6 段2：选段 = 循环区间（无循环则禁用；先用标尺右键拖出区间） */
    const hasLoop = !!(store.loop && store.loop.end - store.loop.start > 1e-3);
    const rangeEl = $('ex-range');
    rangeEl.disabled = !hasLoop;
    if (!hasLoop) rangeEl.checked = false;
    $('ex-range-hint').textContent = hasLoop
      ? '（' + store.loop.start.toFixed(2) + ' – ' + store.loop.end.toFixed(2) + ' s）'
      : '（先右键在标尺拖出循环区间）';
    $('ex-result').hidden = true;
    $('ex-result').textContent = '';
    dlgExport.hidden = false;
  }
  $('ex-cancel').addEventListener('click', () => { dlgExport.hidden = true; });
  $('ex-run').addEventListener('click', async () => {
    if (!store.project) return;
    const btn = $('ex-run');
    btn.disabled = true; btn.textContent = '导出中…';
    try {
      const r = await api.exportProject(store.project, {
        mix: $('ex-mix').checked, stems: $('ex-stems').checked, buses: $('ex-buses').checked,
        midi: $('ex-midi').checked, midi_stems: $('ex-midi-stems').checked,
        bit_depth: $('ex-depth').value,
        range: ($('ex-range').checked && store.loop) ? [store.loop.start, store.loop.end] : null,
      });
      const files = r.files || [];
      $('ex-result').hidden = false;
      $('ex-result').textContent = '✓ 已导出 ' + files.length + ' 个文件 → ' + r.out_dir +
        (r.range ? '\n· 选段 ' + r.range[0] + ' – ' + r.range[1] + ' s（音频产物）' : '') +
        '\n· 位深 ' + (r.bit_depth || 'PCM_16') + '\n' +
        files.map((f) => '· ' + f).join('\n');
      toast('导出完成：' + files.length + ' 个文件');
    } catch (e) {
      $('ex-result').hidden = false;
      $('ex-result').textContent = '✗ ' + e.message;
    } finally {
      btn.disabled = false; btn.textContent = '开始导出';
    }
  });

  /* ---- 收藏版本（强留存 = git tag；恢复走现有 rollback） ---- */
  const dlgFav = $('dlg-fav');
  let favPick = null;
  async function doFavorite() {
    if (!store.project) { setError('先打开一个工程'); return; }
    try {
      const r = await api.favorite(store.project);
      toast('已收藏 ' + r.tag + '（git tag，可随时恢复）');
    } catch (e) { setError(e.message); }
  }
  async function openFavDlg() {
    if (!store.project) { setError('先打开一个工程'); return; }
    favPick = null;
    const list = $('fav-list');
    list.innerHTML = '<div class="fav-none">读取中…</div>';
    dlgFav.hidden = false;
    try {
      const r = await api.favorites(store.project);
      const favs = r.favorites || [];
      list.innerHTML = '';
      if (!favs.length) { list.innerHTML = '<div class="fav-none">（暂无收藏——用「收藏当前版本 ★」创建）</div>'; return; }
      for (const f of favs) {
        const it = document.createElement('div');
        it.className = 'fav-item';
        const src = document.createElement('span');
        src.className = 'fav-src';
        src.textContent = favSource(f.tag);
        const tg = document.createElement('span');
        tg.className = 'fav-tag';
        tg.textContent = String(f.tag).replace(/^fav\//, '');
        const inf = document.createElement('span');
        inf.textContent = f.info || '';
        const del = document.createElement('button');
        del.className = 'fav-del';
        del.textContent = '✕';
        del.title = '删除该收藏（只删标签，不影响版本内容）';
        del.addEventListener('click', async (ev) => {
          ev.stopPropagation();
          if (!confirm('删除收藏 ' + f.tag + ' ？')) return;
          try {
            await api.deleteFavorite(store.project, f.tag);
            toast('已删除 ' + f.tag);
            openFavDlg();
          } catch (e) { setError(e.message); }
        });
        it.appendChild(src); it.appendChild(tg); it.appendChild(inf); it.appendChild(del);
        it.addEventListener('click', () => {
          favPick = f.tag;
          for (const x of Array.from(list.children)) x.classList.toggle('picked', x === it);
        });
        list.appendChild(it);
      }
    } catch (e) {
      list.innerHTML = '';
      const err = document.createElement('div');
      err.className = 'fav-none';
      err.textContent = '读取失败：' + e.message;
      list.appendChild(err);
    }
  }
  $('fav-cancel').addEventListener('click', () => { dlgFav.hidden = true; });
  $('fav-restore').addEventListener('click', async () => {
    if (!favPick || !store.project) { setError('先点选一个收藏版本'); return; }
    try {
      await api.rollback(store.project, favPick);
      dlgFav.hidden = true;
      toast('已恢复到 ' + favPick);
      await openProject(store.project);
    } catch (e) { setError(e.message); }
  });

  /* ---- 版本留存设置（M-V7 D3）：⚙设置面板读写 /settings（默认本工程 + 应用到全部工程） ---- */
  async function loadRetentionSet() {
    if (!store.project) return;
    try {
      const r = await api.getSettings(store.project);
      const s = r.settings || {};
      const tf = s.timed_favorite || {};
      $('set-rt-iters').value = (s.auto_favorite_iters != null) ? s.auto_favorite_iters : 40;
      $('set-rt-timer').checked = !!tf.enabled;
      $('set-rt-min').value = (tf.interval_min != null) ? tf.interval_min : 30;
      $('set-rt-count').textContent = (r.counter || 0) + ' 轮（自上次收藏以来）';
    } catch (e) { /* 忽略：面板保持旧值 */ }
  }
  function retentionPatch() {
    return {
      auto_favorite_iters: Math.max(1, parseInt($('set-rt-iters').value || '40', 10)),
      timed_favorite: {
        enabled: $('set-rt-timer').checked,
        interval_min: Math.max(1, parseFloat($('set-rt-min').value || '30')),
      },
    };
  }
  async function saveRetentionSet() {
    if (!store.project) { setError('先打开一个工程'); return; }
    try {
      await api.setSettings(store.project, retentionPatch());
      toast('留存设置已保存（本工程）');
    } catch (e) { setError(e.message); }
  }
  for (const id of ['set-rt-iters', 'set-rt-min', 'set-rt-timer']) {
    $(id).addEventListener('change', saveRetentionSet);
  }
  $('set-rt-apply-all').addEventListener('click', async () => {
    try {
      await api.setGlobalSettings(retentionPatch());
      toast('已应用到全部工程（全局档 tsov-settings.json）');
    } catch (e) { setError(e.message); }
  });

  /* ---- LLM 接入设置（E2）：⚙面板「对话 / LLM」——设置档 llm.* + 连通自检 ---- */
  const LLM_SRC_TXT = { env: '环境变量/.env', file: '设置界面', dsh: 'dsh 凭据', default: '默认', none: '未配置' };
  async function loadLlmSet() {
    try {
      const r = await api.getLlmSettings();
      const key = r.key || {}, ep = r.endpoint || {}, md = r.model || {}, fl = r.file || {}, def = r.defaults || {};
      const kInp = $('set-llm-key');
      kInp.value = '';
      kInp.placeholder = key.configured ? ('已配置 ' + key.hint + ' · 留空不改动') : '未配置（粘贴 API Key）';
      const epInp = $('set-llm-endpoint');
      epInp.value = fl.endpoint || '';
      epInp.placeholder = '默认：' + (def.endpoint || '');
      const mdInp = $('set-llm-model');
      mdInp.value = fl.model || '';
      mdInp.placeholder = '默认：' + (def.model || '');
      $('set-llm-src').textContent = '当前生效 — ' + [
        'Key：' + (LLM_SRC_TXT[key.source] || key.source),
        '地址：' + (LLM_SRC_TXT[ep.source] || ep.source),
        '模型：' + (LLM_SRC_TXT[md.source] || md.source),
      ].join(' · ');
      const st0 = $('set-llm-status');
      st0.textContent = '';
      st0.className = 'set-hint';
    } catch (e) { /* 忽略：面板保持旧值 */ }
  }
  async function saveLlmSet() {
    const body = { endpoint: $('set-llm-endpoint').value.trim(), model: $('set-llm-model').value.trim() };
    const kv = $('set-llm-key').value.trim();
    if (kv) body.api_key = kv;
    try {
      await api.setLlmSettings(body);
      $('set-llm-key').value = '';
      toast('LLM 设置已保存（本机设置档）');
      await loadLlmSet();
    } catch (e) { setError(e.message); }
  }
  $('set-llm-save').addEventListener('click', saveLlmSet);
  $('set-llm-clear').addEventListener('click', async () => {
    try {
      await api.setLlmSettings({ clear_key: true });
      toast('已清除设置档里的 Key');
      await loadLlmSet();
    } catch (e) { setError(e.message); }
  });
  $('set-llm-test').addEventListener('click', async () => {
    const st = $('set-llm-status');
    const body = {};
    const kv = $('set-llm-key').value.trim(); if (kv) body.api_key = kv;
    const ev = $('set-llm-endpoint').value.trim(); if (ev) body.endpoint = ev;
    const mv = $('set-llm-model').value.trim(); if (mv) body.model = mv;
    st.className = 'set-hint';
    st.textContent = '测试中…';
    try {
      const r = await api.testLlm(body);
      st.className = 'set-hint ' + (r.ok ? 'ok' : 'bad');
      st.textContent = (r.ok ? '✓ ' : '✗ ') + (r.message || '');
      toast(r.ok ? '连接成功' : '连接失败');
    } catch (e) {
      st.className = 'set-hint bad';
      st.textContent = '✗ ' + e.message;
    }
  });

  /* SSE → store（本地直接消费事件负载，契约 §六 数据流） */
  bus.on('state_updated', (s) => setState(s, { preserveSelection: true }));
  bus.on('diff_applied', (d) => {
    const box = $('status-diff');
    const parts = [];
    if (d.added && d.added.length) parts.push('+' + d.added.length);
    if (d.removed && d.removed.length) parts.push('-' + d.removed.length);
    if (d.changed && d.changed.length) parts.push('~' + d.changed.length);
    box.textContent = parts.length ? ('本轮 diff ' + parts.join(' ') + (refTag(d) ? ' ' + refTag(d) : '')) : '';
  });

  /* 版本对比（议题 ④；M-V7 D3 加收藏段）：左槽选版本 → 试听；base_rev 由 chat.js 读取 */
  let cmpGen = 0;
  async function refreshCmpBase() {
    const gen = ++cmpGen;
    const sel = $('cmp-base');
    sel.innerHTML = '';
    try {
      const r = await api.favorites(store.project);
      if (gen !== cmpGen) return;   // 过期帧
      if ((r.favorites || []).length) {
        const g = document.createElement('optgroup');
        g.label = '收藏版本';
        for (const f of r.favorites) {
          const opt = document.createElement('option');
          opt.value = f.tag;
          opt.textContent = '★ ' + favSource(f.tag) + ' · ' + String(f.tag).replace(/^fav\//, '');
          g.appendChild(opt);
        }
        sel.appendChild(g);
      }
    } catch (e) { /* 忽略 */ }
    const g2 = document.createElement('optgroup');
    g2.label = 'git 版本';
    (store.gitLog || []).forEach((line, i) => {
      const opt = document.createElement('option');
      opt.value = line.split(' ')[0];
      opt.textContent = (i === 0 ? '★ HEAD · ' : '') + (line.length > 36 ? line.slice(0, 36) + '…' : line);
      g2.appendChild(opt);
    });
    if (gen !== cmpGen) return;   // 过期帧
    if (g2.children.length) sel.appendChild(g2);
    $('cmp-head').textContent = '↔ ' + ((store.gitLog && store.gitLog[0] && store.gitLog[0].split(' ')[0]) || 'HEAD');
  }
  bus.on('state', refreshCmpBase);
  bus.on('state_updated', refreshCmpBase);

  $('btn-cmp-listen').addEventListener('click', () => {
    if (!store.project) { setError('先打开一个工程'); return; }
    const rev = $('cmp-base').value;
    if (!rev) { setError('先选一个版本（列表或 HEAD）'); return; }
    const a = new Audio();
    a.src = api.wavUrlRev(store.project, rev) + '&v=' + Date.now();   // 审计修 M-V2.3：wavUrlRev 已带 ?rev=，缓存击穿参数用 &
    a.play().then(() => {
      showToast('▶ 试听旧版 ' + rev.slice(0, 8) + '（缓存渲染，不动 HEAD）');
    }).catch((err) => setError('听旧版失败：' + err.message));
  });

  /* 工具栏（修正轮2：择要保留顶栏；新建/导入/改名/刷新/收藏等在「☰ 文件」二级菜单） */
  $('project-select').addEventListener('change', (e) => openProject(e.target.value));
  $('import-project-select').addEventListener('change', (e) => {
    const opt = e.target.selectedOptions[0];
    if (!opt || !e.target.value) return;
    applyImportCandidate({ path: e.target.value, name_hint: opt.dataset.nameHint || '' });
  });
  $('btn-import-project-confirm').addEventListener('click', doImportProject);
  $('btn-import-project-cancel').addEventListener('click', () => { $('import-project-row').hidden = true; });
  $('btn-undo').addEventListener('click', async () => {
    if (!store.project) return;
    try { await api.undo(store.project); } catch (e) { setError(e.message); }
  });
  $('btn-redo').addEventListener('click', async () => {
    if (!store.project) return;
    try { await api.redo(store.project); } catch (e) { setError(e.message); }
  });
  $('btn-zoom-in').addEventListener('click', () => setView({ pxPerSec: Math.min(600, store.view.pxPerSec * 1.25) }));
  $('btn-zoom-out').addEventListener('click', () => setView({ pxPerSec: Math.max(8, store.view.pxPerSec / 1.25) }));
  $('btn-fit').addEventListener('click', () => {
    const rc = $('roll').getBoundingClientRect();
    fitView(rc.width, rc.height);
  });
  $('btn-clear-diff').addEventListener('click', () => {
    store.diff = null;
    $('status-diff').textContent = '';
    bus.dispatch('diff');
  });

  /* 工程改名：见 renameProject（「☰ 文件 → 重命名工程…」） */

  /* 选区工具条（M-V3 雏形：点音符 → 命令层直编） */
  const selBar = $('sel-bar');
  function refreshSelBar() {
    const sel = store.selection;
    if (!sel || !sel.indices.length) { selBar.hidden = true; return; }
    selBar.hidden = false;
    $('sel-info').textContent = '选区：' + (sel.track + 1) + ' 轨 · ' + sel.indices.length + ' 音（Ctrl+点 加减选）';
  }
  bus.on('selection', refreshSelBar);
  bus.on('state', refreshSelBar);

  function selCommands(fn) {
    const sel = store.selection;
    const indices = (sel && sel.indices) || [];
    if (!indices.length) { setError('先点选音符'); return null; }
    return indices.map((idx) => fn(sel.track, idx));
  }

  /* M-V3：标注模式——选区操作挂起为 annotation（随下条对话发送，不发即时请求）
     同（音+动作）重复点击 = 在已挂起值上继续累加（♯+1 点两次 = +2）；删除去重 */
  const ANN_LABELS = { 'pitch-1': '♭−1', 'pitch+1': '♯+1', 'oct-1': '−8', 'oct+1': '+8', 'vel-': '力度−', 'vel+': '力度+', 'delete': '删除' };
  function pendingBase(index, action) {
    let v = null;
    for (const it of store.pendingAnnotations) {
      if (it.ann.index === index && it.ann.action === action) v = it.ann.value;
    }
    return v;
  }
  function makeAnnotations(op, sel) {
    const tr = store.score ? store.score.tracks[sel.track] : null;
    if (!tr) return null;
    const out = [];
    for (const i of sel.indices) {
      const n = tr.notes[i];
      if (!n) continue;
      if (op === 'pitch-1' || op === 'pitch+1' || op === 'oct-1' || op === 'oct+1') {
        const d = op === 'pitch-1' ? -1 : op === 'pitch+1' ? 1 : op === 'oct-1' ? -8 : 8;
        const base = pendingBase(i, 'pitch');
        const cur = (base === null) ? n.pitch_midi : base;
        out.push({ index: i, action: 'pitch', value: Math.max(0, Math.min(127, cur + d)) });
      } else if (op === 'vel-' || op === 'vel+') {
        const d = op === 'vel-' ? -0.1 : 0.1;
        const base = pendingBase(i, 'velocity');
        const cur = (base === null) ? n.velocity : base;
        out.push({ index: i, action: 'velocity', value: Math.round(Math.max(0, Math.min(1, cur + d)) * 100) / 100 });
      } else if (op === 'delete') {
        const dup = store.pendingAnnotations.some((it) => it.ann.index === i && it.ann.action === 'delete');
        if (!dup) out.push({ index: i, action: 'delete' });
      }
    }
    if (op === 'delete') out.sort((a, b) => b.index - a.index);   // 删除从后往前（index 不漂移）
    return out;
  }

  selBar.addEventListener('click', async (e) => {
    const btn = e.target.closest('button[data-op]');
    if (!btn) return;
    const op = btn.dataset.op;
    if (op === 'close') { refreshSelBar(); selBar.hidden = true; return; }
    if (!store.project) return;
    if ($('ann-mode').checked) {
      const anns = makeAnnotations(op, store.selection);
      if (!anns) { setError('先点选音符'); return; }
      if (!anns.length) { toast('已存在相同标注（未重复挂起）'); return; }
      const keys = new Set(anns.map((a) => a.index + '|' + a.action));
      const kept = store.pendingAnnotations.filter((it) => !keys.has(it.ann.index + '|' + it.ann.action));
      setAnnotations(kept.concat(anns.map((a) => ({ ann: a, label: ANN_LABELS[op] || op }))));
      toast('已挂起 ' + anns.length + ' 条标注（' + (ANN_LABELS[op] || op) + '），随下条对话发送');
      return;
    }
    let commands = null, label = '';
    if (op === 'delete') {
      commands = selCommands((t, i) => ({ op: 'remove', track: t, index: i }));
      commands = commands ? commands.slice().sort((a, b) => b.index - a.index) : null;  // 降序删，索引不漂移
      label = '删除选中音';
    } else if (op === 'pitch-1') { commands = selCommands((t, i) => ({ op: 'set_pitch', track: t, index: i, value: '@pitch-1' })); label = '-1 半音';
    } else if (op === 'pitch+1') { commands = selCommands((t, i) => ({ op: 'set_pitch', track: t, index: i, value: '@pitch+1' })); label = '+1 半音';
    } else if (op === 'oct-1') { commands = selCommands((t, i) => ({ op: 'set_pitch', track: t, index: i, value: '@pitch-8' })); label = '-8';
    } else if (op === 'oct+1') { commands = selCommands((t, i) => ({ op: 'set_pitch', track: t, index: i, value: '@pitch+8' })); label = '+8';
    } else if (op === 'vel-') { commands = selCommands((t, i) => ({ op: 'set_velocity', track: t, index: i, value: '@vel-0.1' })); label = '力度 -0.1';
    } else if (op === 'vel+') { commands = selCommands((t, i) => ({ op: 'set_velocity', track: t, index: i, value: '@vel+0.1' })); label = '力度 +0.1';
    }
    if (!commands) return;
    /* 占位 value 在服务端不可用——先换算成绝对值（读当前谱） */
    const tr = store.score ? store.score.tracks[commands[0].track] : null;
    if (!tr) return;
    commands.forEach((c) => {
      const n = tr.notes[c.index];
      if (!n) return;
      const v = String(c.value || '');
      if (v.startsWith('@pitch')) {
        c.value = Math.max(0, Math.min(127, n.pitch_midi + parseInt(v.slice(6), 10)));
      } else if (v.startsWith('@vel')) {
        c.value = Math.round(Math.max(0, Math.min(1, n.velocity + parseFloat(v.slice(4)))) * 100) / 100;
      }
    });
    try {
      const r = await api.postBatch(store.project, label, commands, '标注：' + label);
      if (r.applied) toast('已执行 ' + label + ' ' + refTag(r));
      else setError('被拒：' + (r.errors || []).join('；'));
    } catch (err) { setError(err.message); }
  });

  /* M-V3：待发送标注队列（输入框上方） */
  function refreshAnnQueue() {
    const wrap = $('ann-queue');
    const list = $('ann-queue-list');
    const q = store.pendingAnnotations;
    if (!q.length) { wrap.hidden = true; list.innerHTML = ''; return; }
    wrap.hidden = false;
    const counts = {};
    for (const it of q) counts[it.label] = (counts[it.label] || 0) + 1;
    list.innerHTML = '';
    for (const [label, n] of Object.entries(counts)) {
      const chip = document.createElement('span');
      chip.className = 'ann-chip';
      chip.textContent = label + ' ×' + n;
      list.appendChild(chip);
    }
  }
  bus.on('annotations', refreshAnnQueue);
  bus.on('state', refreshAnnQueue);
  $('btn-ann-clear').addEventListener('click', () => { clearAnnotations(); toast('已清空待发送标注'); });

  /* M-V3：写谱吸附 */
  $('snap-sel').addEventListener('change', (e) => {
    setSnap(e.target.value);
    toast('吸附：' + e.target.selectedOptions[0].textContent);
  });

  /* UI 批A：速度 / 拍号（命令层 set_tempo；只在此处编辑——段轨与顶栏不重复） */
  /* M-V8 小修包（Q47）：改速度默认「跟速重排」——同事务追加 scale_time（全谱时间等比缩放）；复选可关（记忆） */
  const rescaleChk = $('tempo-rescale');
  try {
    const saved = localStorage.getItem('tsov.tempoRescale');
    if (saved !== null) rescaleChk.checked = (saved === '1');
  } catch (e) { /* localStorage 不可用：保持默认勾选 */ }
  rescaleChk.addEventListener('change', () => {
    try { localStorage.setItem('tsov.tempoRescale', rescaleChk.checked ? '1' : '0'); } catch (e) { /* ignore */ }
    toast(rescaleChk.checked ? '改速度 = 跟速重排（音符随动）' : '改速度 = 仅改谱面（音符不动）');
  });

  async function postTempo(cmds, label) {
    if (!store.project) { setError('先打开一个工程'); return; }
    try {
      const r = await api.postBatch(store.project, label, cmds, '参数：' + label);
      if (r.applied) toast('已更新 ' + label + ' ' + refTag(r));
      else setError('被拒：' + (r.errors || []).join('；'));
    } catch (e) { setError(e.message); }
  }
  const tempoIn = $('tempo-input');
  const sigIn = $('sig-input');
  function syncMetro() {
    const sc = store.score;
    tempoIn.value = sc ? String(Math.round(sc.tempo * 10) / 10) : '';
    sigIn.value = sc ? (sc.time_signature || '4/4') : '';
  }
  tempoIn.addEventListener('change', () => {
    const t = Number(tempoIn.value);
    if (!t || t < 20 || t > 400) { syncMetro(); return; }
    const old = store.score ? Number(store.score.tempo) : null;
    if (store.score && Math.abs(t - old) < 1e-6) return;
    if (rescaleChk.checked && old && Math.abs(t - old) > 1e-9) {
      /* 单命令原子：set_tempo_remap（BPM + 缩放同生共死） */
      const nNotes = (store.score.tracks || []).reduce((a, tr) => a + ((tr.notes || []).length), 0);
      const label = '速度 ♩=' + t + ' · 跟速重排' + (nNotes ? '（' + nNotes + ' 音）' : '');
      postTempo([{ op: 'set_tempo_remap', track: 0, value: { tempo: t } }], label);
    } else {
      postTempo([{ op: 'set_tempo', track: 0, value: { tempo: t } }], '速度 ♩=' + t);
    }
  });
  tempoIn.addEventListener('keydown', (e) => { if (e.key === 'Enter') { tempoIn.blur(); } });
  sigIn.addEventListener('change', () => {
    const v = (sigIn.value || '').trim();
    const cur = (store.score && store.score.time_signature) || '4/4';
    if (!v || v === cur) return;
    postTempo([{ op: 'set_tempo', track: 0, value: { time_signature: v } }], '拍号 ' + v);
  });
  sigIn.addEventListener('keydown', (e) => { if (e.key === 'Enter') { sigIn.blur(); } });
  bus.on('state', syncMetro);
  syncMetro();

  bus.on('state', () => { refreshStatusBar(); refreshToolbar(); });

  /* ===== M-V8 E1：书签面板（三层树：项目 / 文件夹 / 轨道） ===== */
  function bmTime(b) {
    return b.kind === 'section'
      ? (b.start.toFixed(1) + '–' + b.end.toFixed(1) + 's')
      : (b.start.toFixed(2) + 's');
  }

  function refreshBookmarks() {
    const el = $('bm-list');
    if (!store.score) { el.className = 'muted small bm-list'; el.textContent = '（未打开）'; return; }
    const bms = bookmarks();
    if (!bms.length) { el.className = 'muted small bm-list'; el.textContent = '（无书签——M 建旗 / 段道拖建段）'; return; }
    el.className = 'bm-list';
    el.innerHTML = '';

    const mkItem = (i) => {
      const b = bms[i];
      const row = document.createElement('div');
      row.className = 'bm-item' + (i === store.selBookmark ? ' sel' : '');
      const pre = document.createElement('span');
      pre.className = 'bm-pre';
      pre.textContent = (b.scope === 'folder' ? '📁' : (b.scope === 'track' ? '🎵' : (b.kind === 'section' ? '▭' : '⚑')));
      const lb = document.createElement('span');
      lb.className = 'bm-label';
      lb.textContent = b.label || (b.kind === 'section' ? '（未命名段）' : '（未命名记号）');
      const tm = document.createElement('span');
      tm.className = 'bm-time';
      tm.textContent = bmTime(b);
      row.appendChild(pre);
      row.appendChild(lb);
      row.appendChild(tm);
      if (b.kind === 'section') {
        const lp = document.createElement('button');
        lp.className = 'bm-loop';
        lp.textContent = '🔁';
        lp.title = '设循环区间 = 此段';
        lp.addEventListener('click', (e) => { e.stopPropagation(); timeline.loopFromBookmark(i); });
        row.appendChild(lp);
      }
      const del = document.createElement('button');
      del.className = 'bm-x';
      del.textContent = '✕';
      del.title = '删除书签';
      del.addEventListener('click', async (e) => {
        e.stopPropagation();
        if (!confirm('删除书签「' + (b.label || '（未命名）') + '」？')) return;
        try {
          const r = await api.postBatch(store.project, '删除书签', [{ op: 'remove_bookmark', track: 0, index: i }], null);
          if (!r.applied) setError('被拒：' + (r.errors || []).join('；'));
          else { setSelBookmark(-1); toast('已删除书签'); }
        } catch (err) { setError(err.message); }
      });
      row.appendChild(del);
      row.title = '单击：跳转 ｜ 双击：改名';
      row.addEventListener('click', () => { setSelBookmark(i); playback.seekTo(b.start); });
      row.addEventListener('dblclick', () => {
        const r2 = row.getBoundingClientRect();
        timeline.openRenameAt(i, r2.left + 60, r2.bottom + 4);
      });
      return row;
    };

    const addGroup = (title, idxs) => {
      if (!idxs.length) return;
      const g = document.createElement('div');
      g.className = 'bm-group';
      const h = document.createElement('div');
      h.className = 'bm-group-title';
      h.textContent = title;
      g.appendChild(h);
      for (const i of idxs) g.appendChild(mkItem(i));
      el.appendChild(g);
    };

    const projIdx = [];
    const byFolder = {};
    const byTrack = {};
    bms.forEach((b, i) => {
      if (b.scope === 'project') projIdx.push(i);
      else if (b.scope === 'folder') (byFolder[b.ref] = byFolder[b.ref] || []).push(i);
      else if (b.scope === 'track') (byTrack[b.ref] = byTrack[b.ref] || []).push(i);
    });
    addGroup('项目', projIdx);
    for (const f of Object.keys(byFolder)) addGroup('📁 ' + f, byFolder[f]);
    for (const tn of Object.keys(byTrack)) addGroup('🎵 ' + tn, byTrack[tn]);
  }
  bus.on('state', refreshBookmarks);
  bus.on('markers', refreshBookmarks);
  refreshBookmarks();

  bus.on('error', showError);
  bus.on('toast', showToast);

  initStepsPanel();   // 对话产物流 C 件：右栏双 tab（对话|步骤）+ 步骤视图

  loadProjects(false);
}

boot();
