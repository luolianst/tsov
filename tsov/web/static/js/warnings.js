/* warnings.js —— P62a（10-14）：插件静音警告卡（渲染响应 / 导出拦截共用）。
   段2（P62b）接线：「打开插件界面」按钮 → open_instrument_editor 命令 + 关窗自动重渲。 */
import { t } from './i18n.js';
import { store, toast } from './state.js';
import { api } from './api.js';
import { bus } from './events.js';

let el = null;

export function showWarnings(list) {
  store.warnings = Array.isArray(list) ? list.slice() : [];
  render();
}

function clearWarnings() { showWarnings([]); }

function render() {
  if (!el) return;
  const ws = store.warnings || [];
  el.hidden = !ws.length;
  if (!ws.length) { el.innerHTML = ''; return; }
  el.innerHTML = '';
  const hd = document.createElement('div');
  hd.className = 'warn-hd';
  hd.textContent = '⚠ ' + t('插件无输出提醒');
  el.appendChild(hd);
  for (const w of ws) {
    const row = document.createElement('div');
    row.className = 'warn-row';
    row.textContent = t('「{name}」{plugin}：有音符但无输出（{n} 音符）', {
      name: w.name || ('track ' + w.track),
      plugin: w.plugin_name || w.plugin || '',
      n: w.notes_count || 0,
    });
    if (w.hint) {
      const hint = document.createElement('div');
      hint.className = 'warn-hint';
      hint.textContent = w.hint;
      row.appendChild(hint);
    }
    el.appendChild(row);
  }
  const ft = document.createElement('div');
  ft.className = 'warn-ft';
  const bOpen = document.createElement('button');
  bOpen.id = 'warn-open';
  bOpen.textContent = t('打开插件界面');
  bOpen.disabled = !ws.some((w) => typeof w.track === 'number');
  bOpen.title = t('打开该轨的插件界面；完成交互后关窗自动重渲');
  const bClose = document.createElement('button');
  bClose.id = 'warn-close';
  bClose.textContent = t('忽略');
  ft.appendChild(bOpen);
  ft.appendChild(bClose);
  el.appendChild(ft);
}

/* ---- P62b：打开插件界面 + 关窗自动重渲 ---- */

let watchTimer = 0;

/* 评审轮 C1 修复（2026-10-15）：① 监视表以「打开请求是否已落定」为门控——
   加载期（重插件冷加载 >1.5s）服务端 editor-status 仍报 open=false，旧实现据此
   误判「已关」：多一次空重渲 + 清表后真关窗不再自动重渲。落定后才允许把
   open=false 判为关窗（仍覆盖「打开即关」的瞬时场景）。② 命令层拒绝不 reject
   （/batch 对 applied=0 返 HTTP 200）——旧 catch 为死代码；改用 applied 判定，
   被拒时按服务端实况分流：有窗（单窗锁）→ 保留监视并改提示；无窗 → 停表报错，
   防一次空重渲。
   证据：output/v02-p62b-c1-2026-10-15/（慢加载仿真 正/负双跑）。 */

async function openEditorFor(ws) {
  const w = ws.find((x) => typeof x.track === 'number');
  if (!w) return;
  await openPluginEditor(w.track);
}

/* P62b-5a（c+）：常驻开窗入口（警告卡与轨道面板共用这一个动作路径） */
export async function openPluginEditor(trackIndex) {
  if (typeof trackIndex !== 'number' || !store.project) return;
  let settled = false;   /* 本次打开请求已落定（成功/失败都算） */
  startEditorWatch(() => settled);
  let ok = false;
  let errMsg = '';
  try {
    const r = await api.postBatch(store.project, t('打开插件界面'),
      [{ op: 'open_instrument_editor', track: trackIndex }], null);
    ok = !!(r && r.applied > 0);   /* 命令层拒绝 = HTTP 200 + applied:0（不 reject） */
    if (!ok && r && Array.isArray(r.errors) && r.errors.length) errMsg = String(r.errors[0]);
  } catch (e) {
    errMsg = (e && e.message) || String(e);
  }
  settled = true;
  if (ok) {
    toast(t('插件界面已打开 —— 完成界面内操作（如点 START / 解锁）后关闭窗口：自动重渲后即可试听最新声音'));
    return;
  }
  /* 未开成：按服务端实况分流——有窗（单窗锁拒）→ 保留监视；无窗 → 停表报错 */
  let open = false;
  try { open = !!(await api.editorStatus(store.project)).open; } catch (e) { /* 查不到按失败处理 */ }
  if (open) {
    toast(t('已有插件界面打开中 —— 请先完成该窗口的操作并关闭（一次只能开一个）'));
    return;
  }
  stopEditorWatch();
  toast(t('打开插件界面失败：') + errMsg);
}

function stopEditorWatch() { clearInterval(watchTimer); watchTimer = 0; }

/* P62b-6（Ⓐ）：编辑器监视是否活跃（playback.js 播放提示用）。 */
export function isEditorWatchActive() { return watchTimer !== 0; }

function startEditorWatch(isSettled) {
  stopEditorWatch();
  const t0 = Date.now();
  watchTimer = setInterval(async () => {
    if (!store.project || Date.now() - t0 > 15 * 60 * 1000) { stopEditorWatch(); return; }
    try {
      const r = await api.editorStatus(store.project);
      if (!isSettled()) return;   /* C1：落定前 open=false 是「加载中」正常态，不得判关窗 */
      if (!r.open) {
        stopEditorWatch();
        bus.dispatch('editor_closed', {});   /* playback.js 监听 → 自动重渲 */
      }
    } catch (e) { /* 静默重试（服务重启等瞬时） */ }
  }, 1500);
}

export function initWarnings(root) {
  el = root;
  if (!el) return;
  el.addEventListener('click', (e) => {
    const id = e.target && e.target.id;
    if (id === 'warn-close') clearWarnings();
    if (id === 'warn-open') openEditorFor(store.warnings || []);
  });
  render();
}
