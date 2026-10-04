/* chat.js —— 对话框：流式回答（思考/正文增量）+ 工具调用过程卡 + 会话控制（新/停止/导入）

消息记录按工程持久化到 sessionStorage（回滚/刷新不丢，仅 新会话/导入 时清空重载）。
模块间只经 api/state/events 交互。 */

import { api } from './api.js';
import { bus } from './events.js';
import { renderAgentsBlock, refreshAgents } from './staging.js';   // D 件：工程上下文折叠条
import { store, setAgentBusy, clearDiff, toast, setError, clearAnnotations, refTag, favSource,
         setSelection, setSingleTrack, addAgentTracks, clearAgentTracks,
         peekUserActions, clearUserActions } from './state.js';

const LF = String.fromCharCode(10);
const MAX_LOGS = 400;

let logEl, inputEl, sendBtn;
let undoRoundBtn, rollbackSel, rollbackBtn, stopBtn, newSessionBtn;
let importBtn, importRow, importSelect, importConfirm, importCancel;
let cmpSel;

/* D 件：AI 动作 chips（快捷命令改造——结构化触发，确定性 REST 不经 LLM；
   参数走默认，要改就在对话里说） */
async function runAiChip(name, btn) {
  if (!store.project) { setError('先打开一个工程'); return; }
  const old = btn.textContent;
  btn.disabled = true;
  try {
    if (name === 'analyze') {
      btn.textContent = '分析中…';
      const r = await api.tuneAnalyze(store.project, store.tune.pack || '');
      const f = (r && r.facts) || {};
      const st = f.structure || {}, lv = f.levels || {};
      const bits = [];
      if (st.tracks) bits.push(st.tracks.length + ' 轨');
      if (lv.mix_peak_dbfs != null) bits.push('混音峰值 ' + lv.mix_peak_dbfs + ' dBFS');
      if (lv.clipping) bits.push('⚠ 削波');
      sysMsg('🔍 分析电平（只读）：' + (bits.join(' · ') || '事实包已就绪（结构 / 电平 / 逐段 / 频谱 / 目标）'));
    } else if (name === 'suggest') {
      btn.textContent = '生成中…';
      const r = await api.tuneSuggest(store.project, { pack: store.tune.pack || null, context: '' });
      const n = ((((r || {}).batch) || {}).suggestions || []).length;
      toast('＋ 调参建议：批次已生成（' + n + ' 条）→ 步骤流「待处置」');
    } else if (name === 'arrange') {
      btn.textContent = '生成中…';
      let pk = store.arrange.pack;
      if (!pk) {
        const rp = await api.arrangePacks(store.project);
        const list = (rp && rp.packs) || [];
        pk = (list[0] && list[0].pack) || '';
      }
      if (!pk) { setError('没有可用风格包（presets/arrangements 为空）'); return; }
      const r = await api.arrangeGenerate(store.project, { pack: pk, strength: 'standard', context: '' });
      const st2 = (((r || {}).batch) || {}).stats || {};
      toast('＋ 配器初稿（包 ' + pk + ' · 标准档）：' + (st2.tracks || 0) + ' 轨 / ' + (st2.notes || 0) + ' 音 → 步骤流「待处置」');
    } else if (name === 'chain') {
      bus.dispatch('focus-chain');
      toast('已定位「链」页签——选好入参后点「运行全链」');
    }
  } catch (e) { setError(String((e && e.message) || e)); }
  finally { btn.disabled = false; btn.textContent = old; }
}

/* D 件：工程上下文折叠条（自 AI 页签迁来；默认收起） */
let engCtxBody = null;
let engCtxSeq = 0;   // 并发渲染去重（初始 + state 事件可能同时触发）
async function renderEngCtx() {
  if (!engCtxBody) return;
  const seq = ++engCtxSeq;
  engCtxBody.innerHTML = '';
  if (!store.project) { engCtxBody.appendChild(text('msg', '未打开工程')); return; }
  await refreshAgents().catch(() => {});
  if (seq !== engCtxSeq) return;   // 过期渲染丢弃
  engCtxBody.innerHTML = '';
  engCtxBody.appendChild(renderAgentsBlock());
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

/* ---------------- 动作卡（批B B1-1 · 工具即控件，ADR-0017） ----------------
   - 写类工具 → 完整动作卡（标签 · 参数摘要 · 影响范围 · 回执 · [撤销][详情]）
   - 只读工具 → 单行汇总（"只读 ×N：…"，防刷屏；默认策略 Q1）
   - 单轮写卡 >3 张 → 折叠为"本轮 N 个动作"（默认策略 Q2） */

const MAX_VISIBLE_CARDS = 3;
// 轮键 -> 聚合卡组（对话产物流 B 件：一「轮」= 一次用户消息触发的执行回合）
const groups = new Map();
function groupKey(rec) {
  if (rec && rec.round != null && rec.round !== '') return 'r:' + rec.round;
  const t = (rec && rec.turn != null) ? rec.turn : 0;
  return 't:' + t;
}
const staleSeqs = new Set();    // 失效动作 seq（后端日志权威 + action_undone 事件增量）
const undoneSeqs = new Set();   // 被撤销的动作 seq（红点：自身被撤）

/** 把失效/撤销集合落到当前 DOM（重渲染后调用；新卡片创建时也会自查） */
function applyStaleClasses() {
  for (const g of groups.values()) {
    for (const node of g.root.querySelectorAll('.actcard')) {
      const s = Number(node.dataset.seq || 0);
      if (!s) continue;
      const b = node.querySelector('.ac-undo');
      if (undoneSeqs.has(s)) { node.classList.add('undone'); node.classList.remove('stale'); if (b) b.disabled = true; }
      else if (staleSeqs.has(s)) { node.classList.add('stale'); if (b) b.disabled = true; }
    }
  }
}

function receiptLine(obs) {
  const s = String(obs || '').split(/\r?\n/).find((l) => l.trim()) || '';
  const t = s.trim();
  return t.length > 96 ? t.slice(0, 95) + '…' : t;
}

function groupFor(key) {
  const k = String(key);
  let g = groups.get(k);
  if (g) return g;
  const root = el('actgroup');
  root.dataset.round = k;
  logEl.appendChild(root);
  g = { key: k, root, cards: 0, readNode: null, readLabels: [], head: null,
        statsNode: null, actionsNode: null, stats: {}, seenTracks: new Set(),
        seqs: [], firstRec: null };
  groups.set(k, g);
  return g;
}

const STAT_LABELS = { notes: '修改音符', track_add: '轨道', track_del: '轨道', track_edit: '轨名',
                      fx: '效果器', param: '参数', other: '动作' };

/** 计数 → 卡面文案（修改音符 ×16 · 轨道 +2 · 效果器 +1 …） */
function statText(k, v) {
  const name = STAT_LABELS[k] || k;
  if (k === 'track_add') return name + ' +' + v;
  if (k === 'track_del') return name + ' \u2212' + v;
  if (k === 'fx') return name + ' +' + v;
  return name + ' \u00d7' + v;
}

function updateGroupHead(g) {
  if (!g.head) {
    g.head = document.createElement('button');
    g.head.className = 'ac-group-head';
    g.head.addEventListener('click', () => {
      g.root.classList.toggle('collapsed');
      updateGroupHead(g);
    });
    g.root.prepend(g.head);
  }
  const collapsed = g.root.classList.contains('collapsed');
  g.head.textContent = '本轮变更 · ' + g.cards + ' 个动作 ' + (collapsed ? '\u25b8' : '\u25be');
}

/** 统计行（对话产物流 B 件：同类合并计数） */
function updateGroupStats(g) {
  const keys = Object.keys(g.stats).filter((k) => g.stats[k]);
  if (!keys.length) return;
  if (!g.statsNode) {
    g.statsNode = el('ac-stats');
    g.root.insertBefore(g.statsNode, g.head.nextSibling);
  }
  g.statsNode.innerHTML = '';
  for (const k of keys) g.statsNode.appendChild(text('ac-stat', statText(k, g.stats[k])));
}

/** 底部动作行（[查看 diff][撤销本轮]）——始终垫底 */
function ensureRoundActions(g) {
  if (!g.actionsNode) {
    g.actionsNode = el('ac-round-actions');
    const diff = document.createElement('button');
    diff.className = 'ac-btn'; diff.textContent = '查看 diff';
    diff.title = '跳转并标记本轮改动范围';
    diff.addEventListener('click', () => viewRoundDiff(g));
    const undo = document.createElement('button');
    undo.className = 'ac-btn ac-undo'; undo.textContent = '撤销本轮';
    undo.title = '撤销本轮全部动作（其后动作将失效）';
    undo.addEventListener('click', () => doRoundUndo(g, undo));
    g.actionsNode.appendChild(diff);
    g.actionsNode.appendChild(undo);
  }
  g.root.appendChild(g.actionsNode);
}

/** 撤销本轮：跳到该轮最小 seq 之前（后端 action_undone 事件负责全链置灰） */
async function doRoundUndo(g, btn) {
  const seqs = g.seqs.filter((s) => s != null && s > 0);
  if (!store.project || !seqs.length) return;
  btn.disabled = true;
  try {
    const min = Math.min.apply(null, seqs);
    const r = await api.actionUndo(store.project, min);
    toast('已撤销本轮（#' + min + ' 起）：' + g.cards + ' 个动作' + (refTag(r) ? ' ' + refTag(r) : ''));
  } catch (e) {
    setError('撤销本轮失败：' + e.message);
    btn.disabled = false;
  }
}

/** 查看 diff：标记 + 跳转该轮改动范围（三色叠层=最新轮由 diff_applied 自动叠加） */
function viewRoundDiff(g) {
  const tracks = Array.from(g.seenTracks);
  if (!tracks.length) { toast('本轮无谱面改动'); return; }
  addAgentTracks(tracks);
  focusImpact({ impact: { tracks: tracks.map((i) => ({ index: i })) } });
}

/** 轮聚合快照（__tsovState().steps 素材） */
function syncStepsSnapshot() {
  const rounds = [];
  for (const g of groups.values()) {
    rounds.push({ key: g.key, n: g.cards, read: g.readLabels.length,
                  stats: Object.assign({}, g.stats), seqs: g.seqs.slice() });
  }
  store.steps = { rounds, count: rounds.length };
}

function buildActionCard(rec) {
  const root = el('actcard');
  root.title = '点击聚焦改动范围';
  root.dataset.actionId = rec.action_id || '';
  if (rec.seq != null) root.dataset.seq = String(rec.seq);
  if (rec.tool) root.dataset.tool = rec.tool;

  const head = el('ac-head');
  head.appendChild(text('ac-label', '⚙ ' + (rec.label || rec.tool || '工具')));
  if (rec.summary) head.appendChild(text('ac-sum', rec.summary));
  head.appendChild(el('ac-sp'));
  if (rec.undoable && rec.seq != null) {
    const b = document.createElement('button');
    b.className = 'ac-btn ac-undo';
    b.textContent = '撤销';
    b.title = '撤销这个动作（其后动作将失效）';
    b.addEventListener('click', () => doActionUndo(rec, b));
    head.appendChild(b);
  }
  const more = document.createElement('button');
  more.className = 'ac-btn ac-more';
  more.textContent = '详情';
  head.appendChild(more);
  root.appendChild(head);

  if (rec.impact && rec.impact.text) root.appendChild(text('ac-impact', '影响：' + rec.impact.text));
  const rc = receiptLine(rec.observation);
  if (rc) root.appendChild(text('ac-receipt', '回执：' + rc));

  const detail = document.createElement('pre');
  detail.className = 'ac-detail';
  detail.hidden = true;
  detail.textContent = (rec.args ? '参数：' + rec.args + LF : '') + '回执：' + LF + (rec.observation || '');
  root.appendChild(detail);
  more.addEventListener('click', () => {
    detail.hidden = !detail.hidden;
    more.textContent = detail.hidden ? '详情' : '收起';
  });
  // 批B B1-3：点动作卡 → 聚焦改动范围（选中该轨 / 单轨视图切到该轨）
  root.addEventListener('click', (e) => {
    if (e.target && e.target.tagName === 'BUTTON') return;
    if (e.target && e.target.tagName === 'PRE') return;
    focusImpact(rec);
  });
  // 失效态（重渲染后新建的卡片也要自查）
  if (rec.seq != null) {
    const s = Number(rec.seq);
    if (undoneSeqs.has(s)) { root.classList.add('undone'); const b = root.querySelector('.ac-undo'); if (b) b.disabled = true; }
    else if (staleSeqs.has(s)) { root.classList.add('stale'); const b = root.querySelector('.ac-undo'); if (b) b.disabled = true; }
  }
  return root;
}

/** 渲染一条动作记录（live 事件与 renderAll 回放共用） */
function appendActionRec(rec) {
  const g = groupFor(groupKey(rec));
  if (rec.read_only) {
    g.readLabels.push(rec.label || rec.tool || '工具');
    if (!g.readNode) { g.readNode = text('ac-readrow', ''); g.root.appendChild(g.readNode); }
    g.readNode.textContent = '只读 ×' + g.readLabels.length + '：' + g.readLabels.join(' · ');
    if (g.actionsNode) g.root.appendChild(g.actionsNode);   // 动作行垫底
    syncStepsSnapshot();
    return;
  }
  const card = buildActionCard(rec);
  g.cards += 1;
  if (!g.firstRec) g.firstRec = rec;
  if (rec.seq != null && rec.seq > 0) g.seqs.push(Number(rec.seq));
  for (const k of Object.keys(rec.stats || {})) {
    const v = Number(rec.stats[k]) || 0;
    if (v) g.stats[k] = (g.stats[k] || 0) + v;
  }
  for (const t of ((rec.impact && rec.impact.tracks) || [])) {
    if (t && t.index != null) g.seenTracks.add(t.index);
  }
  if (g.cards > MAX_VISIBLE_CARDS) {
    card.classList.add('over');
    if (!g.root.classList.contains('folded')) g.root.classList.add('folded');
  }
  g.root.appendChild(card);
  updateGroupHead(g);
  updateGroupStats(g);
  ensureRoundActions(g);
  syncStepsSnapshot();
}

/** 动作级撤销（批B B1-2 服务端接口；seq 由快照日志提供，无 seq 不显示按钮） */
async function doActionUndo(rec, btn) {
  if (!store.project || rec.seq == null) return;
  btn.disabled = true;
  try {
    const r = await api.actionUndo(store.project, rec.seq);
    toast('已撤销动作 #' + rec.seq + '：' + (rec.label || rec.tool) + (refTag(r) ? ' ' + refTag(r) : ''));
    markStaleFrom(groupKey(rec), rec.action_id);
  } catch (e) {
    setError('撤销失败：' + e.message);
    btn.disabled = false;
  }
}

/** 按动作日志恢复失效置灰（刷新/切工程后；后端为权威） */
async function refreshActionStale() {
  if (!currentProject) return;
  let entries = [];
  try {
    const r = await api.actions(currentProject);
    entries = r.entries || [];
  } catch (e) { return; }
  let changed = false;
  for (const e of entries) {
    const s = Number(e.seq || 0);
    if (!s) continue;
    if (e.stale || e.undone) { if (!staleSeqs.has(s)) { staleSeqs.add(s); changed = true; } }
    if (e.undone) { if (!undoneSeqs.has(s)) { undoneSeqs.add(s); changed = true; } }
  }
  if (!changed && !staleSeqs.size) return;
  applyStaleClasses();
}

/** 撤销后：同轮其后动作卡置灰（链式失效提示；同时记入集合供重渲染保持） */
function markStaleFrom(key, actionId) {
  const g = groups.get(String(key));
  if (!g) return;
  let seen = false;
  for (const node of g.root.querySelectorAll('.actcard')) {
    const s = Number(node.dataset.seq || 0);
    if (node.dataset.actionId === actionId) { seen = true; if (s) undoneSeqs.add(s); continue; }
    if (seen && s) { staleSeqs.add(s); }
  }
  applyStaleClasses();
}

/** 批B B1-3：点动作卡 → 聚焦改动范围（选中该轨；单轨视图则切主轨） */
function focusImpact(rec) {
  const tracks = (rec.impact && rec.impact.tracks) || [];
  if (!tracks.length) return;
  const ti = Number(tracks[0].index) || 0;
  if (store.viewMode === 'single') setSingleTrack(ti);
  else setSelection(ti, []);
  toast('聚焦：' + (tracks[0].name || ('轨 ' + ti)));
}

function renderAll() {
  logEl.innerHTML = '';
  groups.clear();                       // 批B：动作分组跟着重建
  store.steps = { rounds: [], count: 0 };   // 对话产物流 B 件：轮快照随重建
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
    if (r.kind === 'action') { appendActionRec(r); continue; }   // 批B：动作卡 v2
    if (r.kind === 'tool') {   // 旧记录（sessionStorage 兼容）
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
  refreshActionStale();   // 批B：按动作日志恢复失效置灰（刷新/切工程后）
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
  const anns = store.pendingAnnotations.map((q) => q.ann);   // M-V3：人工标注（确定性优先，随消息发送）
  const sel = (store.selection && store.selection.indices.length) ? store.selection : null;
  const ua = peekUserActions();   // 批B B1-4：用户手动操作（回流 agent 上下文）
  clearAgentTracks();             // 批B B1-3：新一轮清上一轮改动标记
  appendCard(text('msg user', message), { kind: 'user', text: message, ts: Date.now() });
  inputEl.value = '';
  setAgentBusy(true);
  sysMsg('已发送（' + (anns.length ? '含 ' + anns.length + ' 条人工标注：确定性先行、不走 LLM；' : '')
    + (ua.length ? '含 ' + ua.length + ' 条手动操作摘要；' : '')
    + 'SSE 事件流；目标版本 ' + baseRev + '）…');
  try {
    const res = await api.chat(store.project, message, baseRev, anns.length ? anns : null, sel, ua.length ? ua : null);
    sysMsg('session：' + res.session_id);
    if (anns.length) clearAnnotations();   // 已随消息送达（后端确定性应用；失败会出 agent_error）
    if (ua.length) clearUserActions();     // 批B B1-4：已送达，清缓冲
  } catch (e) {
    setAgentBusy(false);
    const msg = '启动失败：' + e.message;
    appendCard(text('msg error', msg), { kind: 'error', text: msg, ts: Date.now() });
    if (String(e.message || '').indexOf('409') >= 0) sysMsg('（已有会话在跑：若界面卡在"运行中"，刷新页面即可复位）');
  }
}

/* ---------------- 事件接线 ---------------- */

const pendingTools = {};   // tool_call_id -> {name, arguments}（批B：动作卡配对）

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
    // 批B：本轮工具调用入待配表（tool_call_id → 原始参数，供动作卡"详情"用）
    for (const tc of d.tool_calls || []) {
      pendingTools[tc.id || tc.name] = { name: tc.name, arguments: tc.arguments };
    }
  });

  bus.on('agent_tool', (d) => {
    // 批B：动作卡 v2——按 tool_call_id 配对（弃旧"文本包含"匹配）
    const key = d.tool_call_id || d.tool;
    const pending = pendingTools[key];
    delete pendingTools[key];
    const rec = {
      kind: 'action',
      tool: d.tool,
      tool_call_id: d.tool_call_id || null,
      action_id: d.action_id || null,
      turn: (d.turn == null) ? 0 : d.turn,
      round: d.round || null,
      stats: d.stats || {},
      label: d.label || d.tool,
      summary: d.summary || '',
      impact: d.impact || null,
      read_only: !!d.read_only,
      undoable: !!d.undoable,
      seq: (d.seq == null) ? null : d.seq,
      args: pending ? JSON.stringify(pending.arguments, null, 2) : '',
      observation: d.observation || '',
      ts: Date.now(),
    };
    appendActionRec(rec);
    if (rec.impact && rec.impact.tracks && rec.impact.tracks.length) {
      addAgentTracks(rec.impact.tracks.map((t) => t.index));   // 批B B1-3：轨道行 + lane 标记
    }
    if (currentProject) {
      logs[currentProject].push(rec);
      if (logs[currentProject].length > MAX_LOGS) logs[currentProject].shift();
      persist();
    }
    logEl.scrollTop = logEl.scrollHeight;
  });

  /* 批B B1-2：动作撤销事件（含其他客户端触发）→ 按 seq 置灰同动作及其后卡片 */
  bus.on('action_undone', (d) => {
    const seq = Number(d && d.seq) || 0;
    if (!seq) return;
    for (const g of groups.values()) {
      for (const node of g.root.querySelectorAll('.actcard')) {
        const s = Number(node.dataset.seq || 0);
        if (s && s >= seq) { if (s === seq) undoneSeqs.add(s); else staleSeqs.add(s); }
      }
    }
    applyStaleClasses();
  });

  bus.on('agent_answer', (d) => {
    setAgentBusy(false);
    streamCard = null;
    for (const k of Object.keys(pendingTools)) delete pendingTools[k];
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
    for (const k of Object.keys(pendingTools)) delete pendingTools[k];
    appendCard(text('msg error', 'agent 出错：' + (d.error || '')), { kind: 'error', text: 'agent 出错：' + (d.error || ''), ts: Date.now() });
  });
}

/* ---------------- 历史 / 会话控制 ---------------- */

/* M-V7 D3：回滚下拉两段式（+全部 git 版本折叠段）——#最近窗口（快照） / ★收藏（git） / git log。
   代际守卫：并发刷新（短时间内多次 state 事件）时丢弃过期帧，防组重复渲染。 */
let rollbackGen = 0;
async function refreshRollbackOptions() {
  const gen = ++rollbackGen;
  rollbackSel.innerHTML = '';
  if (!store.project) return;

  // 段一：最近窗口（快照）——弱留存，零新版本恢复
  try {
    const w = await api.getWindow(store.project);
    if (gen !== rollbackGen) return;   // 过期帧
    const entries = w.entries || [];
    if (entries.length) {
      const g = document.createElement('optgroup');
      g.label = '最近窗口（快照）';
      for (let k = entries.length; k >= 1; k--) {
        const e = entries[k - 1];
        const opt = document.createElement('option');
        opt.value = 'win:' + k;
        opt.textContent = '#' + k + ' ' + (e.label || e.tool || '编辑')
          + ((typeof w.cursor === 'number' && k === w.cursor) ? '（当前）' : '')
          + (e.stale ? ' · 已失效' : '');
        g.appendChild(opt);
      }
      const opt0 = document.createElement('option');
      opt0.value = 'win:0';
      opt0.textContent = '#0 窗口起点（保留窗口内最早状态）';
      g.appendChild(opt0);
      rollbackSel.appendChild(g);
    }
  } catch (e) { /* 窗口不可读不阻塞其余段 */ }

  // 段二：收藏（git）——强留存，来源标注
  try {
    const r = await api.favorites(store.project);
    if (gen !== rollbackGen) return;   // 过期帧
    const favs = r.favorites || [];
    if (favs.length) {
      const g = document.createElement('optgroup');
      g.label = '收藏（git）';
      for (const f of favs) {
        const opt = document.createElement('option');
        opt.value = 'rev:' + f.tag;
        opt.textContent = '★ ' + favSource(f.tag) + ' · ' + String(f.tag).replace(/^fav\//, '');
        g.appendChild(opt);
      }
      rollbackSel.appendChild(g);
    }
  } catch (e) { /* 忽略 */ }

  // 段三：全部 git 版本（log）——缺省折叠在最后
  if ((store.gitLog || []).length) {
    const g = document.createElement('optgroup');
    g.label = '全部 git 版本';
    store.gitLog.forEach((line) => {
      const opt = document.createElement('option');
      opt.value = 'rev:' + line.split(' ')[0];
      opt.textContent = line.length > 40 ? line.slice(0, 40) + '…' : line;
      g.appendChild(opt);
    });
    rollbackSel.appendChild(g);
  }
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
  for (const k of Object.keys(pendingTools)) delete pendingTools[k];
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
  cmpSel = opts.cmpSel;

  /* D 件：工程上下文折叠条挂载 */
  engCtxBody = document.getElementById('eng-ctx-body');
  renderEngCtx();
  bus.on('state', renderEngCtx);
  bus.on('agents_synced', renderEngCtx);

  sendBtn.addEventListener('click', () => send(inputEl.value));
  inputEl.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.isComposing) send(inputEl.value);
  });

  undoRoundBtn.addEventListener('click', async () => {
    if (!store.project) return;
    try {
      await api.undo(store.project);   // M-V7 D2/D3：窗口游标撤销（零 commit）
      clearDiff();
      toast('已撤销一步（快照窗口）');
    } catch (e) { setError(e.message); }
  });

  rollbackBtn.addEventListener('click', async () => {
    const v = rollbackSel.value;
    if (!store.project || !v) { setError('先选一个恢复点'); return; }
    try {
      if (v.startsWith('win:')) {
        // M-V7 D3：窗口段 = 快照点恢复（零新版本）
        const k = parseInt(v.slice(4), 10);
        await api.windowJump(store.project, k);
        clearDiff();
        toast('已恢复到快照 #' + k + '（窗口内 · 零新版本）');
      } else {
        await api.rollback(store.project, v.slice(4));
        clearDiff();
        toast('已回滚到 ' + v.slice(4));
      }
    } catch (e) { setError(e.message); }
  });

  newSessionBtn.addEventListener('click', doNewSession);
  stopBtn.addEventListener('click', doStop);
  importBtn.addEventListener('click', openImportRow);
  importConfirm.addEventListener('click', confirmImport);
  importCancel.addEventListener('click', () => { importRow.hidden = true; });

  opts.chipsEl.addEventListener('click', (e) => {
    const chip = e.target.closest('.chip');
    if (chip && chip.dataset.chip) runAiChip(chip.dataset.chip, chip);
  });

  wireEvents();
  bus.on('state', refreshRollbackOptions);
  bus.on('agentbusy', () => {
    sendBtn.disabled = store.agentBusy;
    inputEl.disabled = store.agentBusy;
    stopBtn.hidden = !store.agentBusy;
  });
}