/* chat.js —— 对话框：用户/助手消息 + 工具调用过程卡（可折叠）+ 轮次操作（撤销本轮/回滚） */

import { api } from './api.js';
import { bus } from './events.js';
import { store, setAgentBusy, clearDiff, toast, setError } from './state.js';

let logEl, inputEl, sendBtn, undoRoundBtn, rollbackSel, rollbackBtn;

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

function appendCard(node) {
  logEl.appendChild(node);
  logEl.scrollTop = logEl.scrollHeight;
  return node;
}

function sysMsg(msg) { appendCard(text('msg sys', msg)); }

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
  appendCard(d);
  return { card: d, pre, summary };
}

async function send(message) {
  if (!store.project) { setError('先打开一个工程'); return; }
  if (!message.trim() || store.agentBusy) return;

  appendCard(text('msg user', message));
  inputEl.value = '';
  setAgentBusy(true);
  sysMsg('agent 会话已启动（工具循环中，事件经 SSE 推送）…');
  try {
    const res = await api.chat(store.project, message);
    sysMsg('session：' + res.session_id);
  } catch (e) {
    setAgentBusy(false);
    sysMsg('');
    appendCard(text('msg error', '启动失败：' + e.message));
  }
}

/* ---- SSE 事件渲染 ---- */
let pendingTool = null;   // 最近一个工具卡（observation 回填目标）

function wireEvents() {
  bus.on('agent_turn', (d) => {
    if (d.content && d.content.trim()) {
      appendCard(text('msg assistant', d.content));
    }
    for (const tc of d.tool_calls || []) {
      pendingTool = toolCard(tc.name, undefined);
      pendingTool.pre.textContent = '参数：' + JSON.stringify(tc.arguments, null, 2);
    }
  });
  bus.on('agent_tool', (d) => {
    if (pendingTool && pendingTool.summary.textContent.indexOf(d.tool) >= 0) {
      const LF = String.fromCharCode(10);
      pendingTool.pre.textContent += LF + '观测：' + LF + (d.observation || '');
      pendingTool = null;
    } else {
      toolCard(d.tool, d.observation || '');
    }
  });
  bus.on('agent_answer', (d) => {
    setAgentBusy(false);
    pendingTool = null;
    const msg = text('msg assistant', d.content || '');
    const tag = el('tag');
    tag.textContent = 'agent 完成 · ' + d.turns + ' 轮 · ' + d.tool_calls_made + ' 次工具调用' +
      (d.adopted ? ' · 编辑已采用（见 diff 叠层）' : ' · 无谱面改动');
    msg.prepend(tag);
    appendCard(msg);
  });
  bus.on('agent_error', (d) => {
    setAgentBusy(false);
    pendingTool = null;
    appendCard(text('msg error', 'agent 出错：' + (d.error || '')));
  });
}

/* ---- 历史操作 ---- */
function refreshRollbackOptions() {
  rollbackSel.innerHTML = '';
  store.gitLog.forEach((line, i) => {
    const opt = document.createElement('option');
    opt.value = line.split(' ')[0];
    opt.textContent = line.length > 40 ? line.slice(0, 40) + '…' : line;
    rollbackSel.appendChild(opt);
  });
}

export function init(opts) {
  logEl = opts.logEl;
  inputEl = opts.inputEl;
  sendBtn = opts.sendBtn;
  undoRoundBtn = opts.undoRoundBtn;
  rollbackSel = opts.rollbackSel;
  rollbackBtn = opts.rollbackBtn;

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

  /* 快捷 chip：data-msg = 走 agent；data-cmd = 命令层 demo（直接 EditBatch） */
  opts.chipsEl.addEventListener('click', (e) => {
    const btn = e.target.closest('button.chip');
    if (!btn) return;
    if (btn.dataset.msg) { send(btn.dataset.msg); return; }
    if (btn.dataset.cmd === 'transpose+2') {
      if (!store.project) { setError('先打开一个工程'); return; }
      api.postBatch(store.project, '+2', [{ op: 'transpose', track: 0, index: null, value: 2 }], '命令层 demo：+2 半音')
        .then((r) => {
          if (r.applied) toast('命令层：已 +2 半音 @' + (r.commit || '').slice(0, 7));
          else setError('命令被拒：' + (r.errors || []).join('；'));
        })
        .catch((err) => setError(err.message));
    }
  });

  wireEvents();
  bus.on('state', refreshRollbackOptions);
  bus.on('agentbusy', () => {
    sendBtn.disabled = store.agentBusy;
    inputEl.disabled = store.agentBusy;
  });
}
