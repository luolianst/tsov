/* warnings.js —— P62a（10-14）：插件静音警告卡（渲染响应 / 导出拦截共用）。
   段2（P62b）接线：「打开插件界面」按钮 → open_instrument_editor 命令 + 关窗自动重渲。 */
import { t } from './i18n.js';
import { store } from './state.js';

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
  bOpen.disabled = true;   /* P62b 接线 */
  bOpen.title = t('段2（P62b）接线后可用');
  const bClose = document.createElement('button');
  bClose.id = 'warn-close';
  bClose.textContent = t('忽略');
  ft.appendChild(bOpen);
  ft.appendChild(bClose);
  el.appendChild(ft);
}

export function initWarnings(root) {
  el = root;
  if (!el) return;
  el.addEventListener('click', (e) => {
    if (e.target && e.target.id === 'warn-close') clearWarnings();
  });
  render();
}
