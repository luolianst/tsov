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

async function openEditorFor(ws) {
  const w = ws.find((x) => typeof x.track === 'number');
  if (!w) return;
  await openPluginEditor(w.track);
}

/* P62b-5a（c+）：常驻开窗入口（警告卡与轨道面板共用这一个动作路径） */
export async function openPluginEditor(trackIndex) {
  if (typeof trackIndex !== 'number' || !store.project) return;
  startEditorWatch();   /* 先起监视——防「打开即失败关窗」的瞬时事件被漏掉 */
  try {
    await api.postBatch(store.project, t('打开插件界面'),
      [{ op: 'open_instrument_editor', track: trackIndex }], null);
    toast(t('插件界面已打开 —— 完成界面操作后关闭窗口，将自动重渲'));
  } catch (e) {
    clearInterval(watchTimer);
    toast(t('打开插件界面失败：') + e.message);
  }
}

function startEditorWatch() {
  clearInterval(watchTimer);
  const t0 = Date.now();
  watchTimer = setInterval(async () => {
    if (!store.project || Date.now() - t0 > 15 * 60 * 1000) { clearInterval(watchTimer); return; }
    try {
      const r = await api.editorStatus(store.project);
      if (!r.open) {
        clearInterval(watchTimer);
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
