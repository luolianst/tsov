/* chat.js —— 对话框：流式回答（思考/正文增量）+ 工具调用过程卡 + 会话控制（新/停止/导入）

消息记录按工程持久化到 sessionStorage（回滚/刷新不丢，仅 新会话/导入 时清空重载）。
模块间只经 api/state/events 交互。 */

import { api } from './api.js';
import { bus } from './events.js';
import { store, setAgentBusy, clearDiff, toast, setError } from './state.js';

const LF = String.fromCharCode(10);
const MAX_LOGS = 400;

let logEl, inputEl, sendBtn;
let undoRoundBtn, rollbackSel, rollbackBtn, stopBtn, newSessionBtn;
let importBtn, importRow, importSelect, importConfirm, importCancel;
let quickSel, quickRunBtn, cmpSel;

/* 快捷命令（数据驱动：加命令只改这里；value 前缀 msg| 走 LLM / cmd| 走命令层直编） */
const QUICK_CMDS = [
  { group: 'LLM 语义编辑', items: [
    { value: 'msg|改成 D 多利亚调式并渲染试听', label: '改成 D 多利亚调式' },
    { value: 'msg|把旋律整体升 2 个半音', label: '整体 +2 半音' },
  ]},
  { group: '命令层直编（非 LLM）', items: [
    { value: 'cmd|transpose+2', label: '整体 +2 半音（命令层）' },
  ]},
];

function renderQuickCmds() {
  if (!quickSel) return;
  quickSel.innerHTML = '';
  const ph = document.createElement('option');
  ph.value = '';
  ph.textContent = '快捷命令…';
  quickSel.appendChild(ph);
  for (const g of QUICK_CMDS) {
    const og = document.createElement('optgroup');
    og.label = g.group;
    for (const it of g.items) {
      const o = document.createElement('option');
      o.value = it.value;
      o.textContent = it.label;
      og.appendChild(o);
    }
    quickSel.appendChild(og);
  }
}

function runQuickCmd(value) {
  if (!value) return;
  if (quickSel) quickSel.value = '';   // 复位，允许重复执行同一命令
  if (value.startsWith('msg|')) { send(value.slice(4)); return; }
  if (value === 'cmd|transpose+2') {
    if (!store.project) { setError('先打开一个工程'); return; }
    api.postBatch(store.project, '+2', [{ op: 'transpose', track: 0, index: null, value: 2 }], '命令层 demo：+2 半音')
      .then((r) => {
        if (r.applied) toast('命令层：已 +2 半音 @' + (r.commit || '').slice(0, 7));
        else setError('命令被拒：' + (r.errors || []).join('；'));
      })
      .catch((err) => setError(err.message));
  }
}

/* ---------------- 消息记录（按工程持久化） ---------------- */

const logs = {};   // project -> array of cards
let currentProject = null;

function key(p) { return 'tsov-chat-' + p; }

function loadLogs(p) {
  try {
    const raw = sessionStorage.getItem(key(p));
    return raw ? JSON.parse(raw) : [];
  } catch (e) { return []; }
}

function persist() {
  if (!currentProject) return;
  try {
    sessionStorage.setItem(key(currentProject), JSON.stringify(logs[currentProject] || []));
  } catch (e) { /* 存储满/隐私模式忽略 */ }
}

function el(cls, html) {
  const d = document.createElement('div');
  d.className = cls;
  if (html !== undefined) d.innerHTML = html;
  return d;
}

function text(cls, txt) {
  const d = el(cls);
  d.textContent = txt;
  return d;
}

function appendCard(node, record) {
  logEl.appendChild(node);
  if (currentProject && record) {
    logs[currentProject].push(record);
    if (logs[currentProject].length > MAX_LOGS) logs[currentProject].shift();
    persist();
  }
  logEl.scrollTop = logEl.scrollHeight;
  return node;
}

function sysMsg(msg) {
  const n = text('msg sys', msg);
  appendCard(n, { kind: 'sys', text: msg, ts: Date.now() });
}

function toolCard(name, observation) {
  const d = el('toolcard');
  const details = document.createElement('details');
  const summary = document.createElement('summary');
  summary.textContent = '⚙ ' + name;
  details.appendChild(summary);
  const pre = document.createElement('pre');
  if (observation !== undefined) pre.textContent = observation;
  details.appendChild(pre);
  d.appendChild(details);
  const n = appendCard(d, { kind: 'tool', name, text: observation || '', ts: Date.now() });
  return { card: n, pre, summary };
}

function renderAll() {
  logEl.innerHTML = '';
  const arr = logs[currentProject] || [];
  for (const r of arr) {
    if (r.kind === 'sys') { logEl.appendChild(text('msg sys', r.text)); continue; }
    if (r.kind === 'user') { logEl.appendChild(text('msg user', r.text)); continue; }
    if (r.kind === 'error') { logEl.appendChild(text('msg error', r.text)); continue; }
    if (r.kind === 'assistant') {
      const m = text('msg assistant', r.text);
      if (r.tag) { const t = el('tag'); t.textContent = r.tag; m.prepend(t); }
      logEl.appendChild(m);
      continue;
    }
    if (r.kind === 'tool') {
      const d = el('toolcard');
      const details = document.createElement('details');
      const summary = document.createElement('summary');
      summary.textContent = '⚙ ' + (r.name || '');
      details.appendChild(summary);
      const pre = document.createElement('pre');
      pre.textContent = r.text || '';
      details.appendChild(pre);
      d.appendChild(details);
      logEl.appendChild(d);
      continue;
    }
  }
  logEl.scrollTop = logEl.scrollHeight;
}

/* ---------------- 流式卡片 ---------------- */

let streamCard = null;   // { root, body, thinkPre, thinkSummary }

function ensureStreamCard() {
  if (streamCard) return streamCard;
  const root = el('msg assistant streaming');
  const think = el('think');
  const details = document.createElement('details');
  const summary = document.createElement('summary');
  summary.textContent = '…思考中…';
  details.appendChild(summary);
  const pre = document.createElement('pre');
  pre.textContent = '';
  details.appendChild(pre);
  think.appendChild(details);
  const body = el('body');
  body.textContent = '';
  root.appendChild(think);
  root.appendChild(body);
  logEl.appendChild(root);
  streamCard = { root, body, thinkPre: pre, thinkSummary: summary };
  return streamCard;
}

function finalizeStreamCard() {
  if (!streamCard) return;
  streamCard.root.classList.remove('streaming');
  streamCard = null;
}

/* ---------------- 发送 ---------------- */

async function send(message) {
  if (!store.project) { setError('先打开一个工程'); return; }
  if (!message.trim() || store.agentBusy) return;

  const baseRev = (cmpSel && cmpSel.value) || 'HEAD';   // 议题 ④：编辑目标版本标识（默认 HEAD）
  appendCard(text('msg user', message), { kind: 'user', text: message, ts: Date.now() });
  inputEl.value = '';
  setAgentBusy(true);
  sysMsg('已发送（SSE 事件流，多轮会话续接同一 JSONL；目标版本 ' + baseRev + '）…');
  try {
    const res = await api.chat(store.project, message, baseRev);
    sysMsg('session：' + res.session_id);
  } catch (e) {
    setAgentBusy(false);
    appendCard(text('msg error', '启动失败：' + e.message), { kind: 'error', text: '启动失败：' + e.message, ts: Date.now() });
  }
}

/* ---------------- 事件接线 ---------------- */

let pendingTool = null;

function wireEvents() {
  bus.on('agent_delta', (d) => {
    const sc = ensureStreamCard();
    if (d.kind === 'thinking') {
      sc.thinkPre.textContent += d.text || '';
      if (sc.thinkPre.textContent.length > 0 && sc.thinkSummary.textContent === '…思考中…') {
        sc.thinkSummary.textContent = '思考过程';
        const details = sc.thinkPre.closest('details');
        if (details) details.open = true;
      }
    } else {
      sc.body.textContent += d.text || '';
    }
    logEl.scrollTop = logEl.scrollHeight;
  });

  bus.on('agent_turn', (d) => {
    if (streamCard) {
      if (d.content && d.content.trim()) {
        streamCard.body.textContent = d.content;   // 流式累计后以完整正文为准
      }
      finalizeStreamCard();
      const tag = 'agent·第 ' + d.turn + ' 轮' + ((d.tool_calls || []).length ? ' · 调用 ' + d.tool_calls.length + ' 个工具' : '');
      const m = text('msg assistant', d.content || '（本轮仅工具调用）');
      const t = el('tag'); t.textContent = tag; m.prepend(t);
      appendCard(m, { kind: 'assistant', text: d.content || '（本轮仅工具调用）', tag, ts: Date.now() });
    } else if (d.content && d.content.trim()) {
      const tag = 'agent·第 ' + d.turn + ' 轮';
      const m = text('msg assistant', d.content);
      const t = el('tag'); t.textContent = tag; m.prepend(t);
      appendCard(m, { kind: 'assistant', text: d.content, tag, ts: Date.now() });
    }
    for (const tc of d.tool_calls || []) {
      pendingTool = toolCard(tc.name, undefined);
      pendingTool.pre.textContent = '参数：' + JSON.stringify(tc.arguments, null, 2);
    }
  });

  bus.on('agent_tool', (d) => {
    if (pendingTool && pendingTool.summary.textContent.indexOf(d.tool) >= 0) {
      pendingTool.pre.textContent += LF + '观测：' + LF + (d.observation || '');
      pendingTool = null;
    } else {
      toolCard(d.tool, d.observation || '');
    }
  });

  bus.on('agent_answer', (d) => {
    setAgentBusy(false);
    streamCard = null;
    pendingTool = null;
    const tag = 'agent 完成 · ' + d.turns + ' 轮 · ' + d.tool_calls_made + ' 次工具调用' +
      (d.adopted ? ' · 编辑已采用（见 diff 叠层）' : ' · 无谱面改动') +
      (d.stopped ? ' · 已停止' : '');
    const arr = logs[currentProject] || [];
    const last = arr.length ? arr[arr.length - 1] : null;
    if (last && last.kind === 'assistant' && last.text === (d.content || '')) {
      last.tag = tag;
      const node = logEl.lastElementChild;
      if (node && node.classList.contains('msg')) {
        const t = el('tag'); t.textContent = tag; node.prepend(t);
      }
      persist();
    } else {
      const m = text('msg assistant', d.content || '');
      const t = el('tag'); t.textContent = tag; m.prepend(t);
      appendCard(m, { kind: 'assistant', text: d.content || '', tag, ts: Date.now() });
    }
  });

  bus.on('agent_error', (d) => {
    setAgentBusy(false);
    streamCard = null;
    pendingTool = null;
    appendCard(text('msg error', 'agent 出错：' + (d.error || '')), { kind: 'error', text: 'agent 出错：' + (d.error || ''), ts: Date.now() });
  });
}

/* ---------------- 历史 / 会话控制 ---------------- */

function refreshRollbackOptions() {
  rollbackSel.innerHTML = '';
  store.gitLog.forEach((line, i) => {
    const opt = document.createElement('option');
    opt.value = line.split(' ')[0];
    opt.textContent = line.length > 40 ? line.slice(0, 40) + '…' : line;
    rollbackSel.appendChild(opt);
  });
}

async function doNewSession() {
  if (!store.project) return;
  try {
    await api.chatReset(store.project);
    if (logs[store.project]) { logs[store.project] = []; persist(); }
    renderAll();
    sysMsg('已开新会话：下一轮对话将写入新的会话文件');
  } catch (e) { setError(e.message); }
}

async function doStop() {
  try {
    const r = await api.chatStop();
    if (r.stopping) toast('已请求停止 agent 会话…');
    else toast('当前没有运行中的会话');
  } catch (e) { setError(e.message); }
}

async function openImportRow() {
  if (!store.project) { setError('先打开一个工程'); return; }
  try {
    const r = await api.listSessions();
    importSelect.innerHTML = '';
    (r.sessions || []).forEach((s) => {
      const opt = document.createElement('option');
      opt.value = s.name;
      opt.textContent = s.name + '  (' + s.mtime + ')';
      importSelect.appendChild(opt);
    });
    if (!importSelect.options.length) {
      sysMsg('（没有可导入的历史会话）');
    }
    importRow.hidden = false;
    importSelect.focus();
  } catch (e) { setError(e.message); }
}

async function confirmImport() {
  if (!store.project || !importSelect.value) { setError('先选一个会话文件'); return; }
  try {
    const r = await api.loadSession(store.project, importSelect.value);
    importRow.hidden = true;
    logs[store.project] = [];
    renderAll();
    sysMsg('已导入会话 ' + r.session_id + '（消息回放 + 续接同一 JSONL）');
    for (const m of r.messages || []) {
      if (m.role === 'user') logs[store.project].push({ kind: 'user', text: m.content || '', ts: Date.now() });
      else if (m.role === 'assistant') logs[store.project].push({ kind: 'assistant', text: m.content || '', tag: '（历史）', ts: Date.now() });
      else if (m.role === 'tool') logs[store.project].push({ kind: 'tool', name: 'tool', text: m.content || '', ts: Date.now() });
      else if (m.role === 'system') logs[store.project].push({ kind: 'sys', text: m.content || '', ts: Date.now() });
    }
    persist();
    renderAll();
    toast('已续接会话 ' + r.session_id);
  } catch (e) { setError(e.message); }
}

/* ---------------- 工程切换（外部调用） ---------------- */

export function switchProject(project) {
  currentProject = project;
  if (!logs[currentProject]) logs[currentProject] = loadLogs(currentProject);
  streamCard = null;
  pendingTool = null;
  renderAll();
}

/* ---------------- 初始化 ---------------- */

export function init(opts) {
  logEl = opts.logEl;
  inputEl = opts.inputEl;
  sendBtn = opts.sendBtn;
  undoRoundBtn = opts.undoRoundBtn;
  rollbackSel = opts.rollbackSel;
  rollbackBtn = opts.rollbackBtn;
  stopBtn = opts.stopBtn;
  newSessionBtn = opts.newSessionBtn;
  importBtn = opts.importBtn;
  importRow = opts.importRow;
  importSelect = opts.importSelect;
  importConfirm = opts.importConfirm;
  importCancel = opts.importCancel;
  quickSel = opts.quickSel;
  quickRunBtn = opts.quickRunBtn;
  cmpSel = opts.cmpSel;

  renderQuickCmds();

  sendBtn.addEventListener('click', () => send(inputEl.value));
  inputEl.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.isComposing) send(inputEl.value);
  });

  undoRoundBtn.addEventListener('click', async () => {
    if (!store.project) return;
    try {
      await api.undo(store.project);
      clearDiff();
      toast('已撤销本轮');
    } catch (e) { setError(e.message); }
  });

  rollbackBtn.addEventListener('click', async () => {
    if (!store.project || !rollbackSel.value) { setError('先选一个版本'); return; }
    try {
      await api.rollback(store.project, rollbackSel.value);
      clearDiff();
      toast('已回滚到 ' + rollbackSel.value);
    } catch (e) { setError(e.message); }
  });

  newSessionBtn.addEventListener('click', doNewSession);
  stopBtn.addEventListener('click', doStop);
  importBtn.addEventListener('click', openImportRow);
  importConfirm.addEventListener('click', confirmImport);
  importCancel.addEventListener('click', () => { importRow.hidden = true; });

  opts.chipsEl.addEventListener('change', (e) => runQuickCmd(e.target.value));
  if (quickRunBtn) quickRunBtn.addEventListener('click', () => runQuickCmd(quickSel ? quickSel.value : ''));

  wireEvents();
  bus.on('state', refreshRollbackOptions);
  bus.on('agentbusy', () => {
    sendBtn.disabled = store.agentBusy;
    inputEl.disabled = store.agentBusy;
    stopBtn.hidden = !store.agentBusy;
  });
}