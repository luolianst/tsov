/* autoroll.js —— 自动化道栈装配（单轨视图底部子道；v0.2 批C 前段让位 lane_render.js；
   v0.2 批C2（F4）：消费「该轨**全部道、按树序**」渲染堆叠条——道 = Track.lanes（未管理时派生
   automation 现存键，旧工程同构）；不再固定 volume/pan 双道。#tl-auto 开关与下拉 = 道选择（定位）。
   数据走命令层 set_automation（/batch，ADR-0017 同一动作路径）；
   交互：点空白=加点 ｜ 拖点=移动 ｜ 双击点 / 右键点=删除（每笔一个命令、可撤销）。
   显示：线性折线（与引擎 host/mix.py::_curve 的 np.interp 语义一致：段外取端点值）。 */

import { bus } from './events.js';
import { api } from './api.js';
import { store, setError, toast, refTag, paramRange, paramLabel, trackLanes, hasVelocityLane } from './state.js';
import { createLane } from './lane_render.js';

/* 历史画布内标签（与既有视觉一字不差）；其他道走注册表标签/范围。 */
const DEFS = {
  volume: { title: '音量(0~200%)', bottomTag: '0%' },
  pan:    { title: '声像(L~R)',    bottomTag: 'L' },
};

let host = null;
let mounted = [];            // [{id, lane}]

function owner() {
  const ti = store.viewMode === 'single' ? store.singleTrack : -1;
  return store.score && ti >= 0 && store.score.tracks[ti] ? { ti, track: store.score.tracks[ti] } : null;
}

/* 当前应开的道（v0.2 C2：该轨全部道、按树序——trackLanes 规范形） */
function activeIds() {
  const o = owner();
  if (!o) return [];
  const ids = trackLanes(o.track).map((l) => l.param);
  if (hasVelocityLane(o.ti)) ids.push('velocity');   /* v0.2 批C 后段：力度伪道 */
  return ids;
}

function descriptorOf(id) {
  if (id === 'velocity') return velocityDescriptor();   /* v0.2 批C 后段：力度 bars 伪道 */
  const def = DEFS[id] || { title: paramLabel(id), bottomTag: String(paramRange(id)[0]) };
  return {
    id,
    title: def.title,
    bottomTag: def.bottomTag,
    range: () => paramRange(id),
    readPoints: () => {
      const o = owner();
      if (!o) return [];
      const pts = (o.track.automation || {})[id] || [];
      return pts.map((p) => [Number(p[0]), Number(p[1])])
        .filter((p) => Number.isFinite(p[0]) && Number.isFinite(p[1]));
    },
    commit: (pts, label) => commit(id, pts, label),
  };
}

/* ---- 提交（命令层 /batch：set_automation 点集整体替换） ---- */

async function commit(id, pts, label) {
  const o = owner();
  if (!o || !store.project) return;
  const r6 = (x) => Math.round(x * 1e6) / 1e6;
  const r4 = (x) => Math.round(x * 1e4) / 1e4;
  const value = { param: id, points: pts.map((q) => [r6(q[0]), r4(q[1])]) };
  try {
    const r = await api.postBatch(store.project, '自动化 ' + label,
      [{ op: 'set_automation', track: o.ti, value }],
      '自动化（' + paramLabel(id) + '）：' + label);
    if (r.applied) toast('已' + label + ' ' + refTag(r));
    else setError('被拒：' + (r.errors || []).join('；'));
  } catch (e) { setError(e.message); }
}

/* v0.2 批C 后段（挂道族）：力度 bars 伪道描述子 + 批量提交（set_velocity 一事务；>200 分批）。 */

function velocityDescriptor() {
  return {
    id: 'velocity',
    kind: 'bars',
    title: '力度(0~100%)',
    bottomTag: '0%',
    range: () => paramRange('velocity'),
    readBars: () => {
      const o = owner();
      if (!o) return [];
      return o.track.notes.map((n, i) => ({
        t0: Number(n.start),
        t1: Math.max(Number(n.end), Number(n.start) + 0.001),
        v: Number(n.velocity),
        idx: i,
      }));
    },
    commitBars: (changes, label) => commitVelocity(changes, label),
  };
}

async function commitVelocity(changes, label) {
  const o = owner();
  if (!o || !store.project) return;
  const r4 = (x) => Math.round(x * 1e4) / 1e4;
  const cmds = changes.map(([i, v]) => ({ op: 'set_velocity', track: o.ti, index: i, value: r4(v) }));
  const CHUNK = 200;   /* 单批上限（防超大事务） */
  try {
    let applied = 0; const errs = [];
    for (let k = 0; k < cmds.length; k += CHUNK) {
      const r = await api.postBatch(store.project, '力度 ' + label, cmds.slice(k, k + CHUNK), '力度（柱状）：' + label);
      applied += r.applied || 0;
      errs.push(...(r.errors || []));
    }
    if (applied) toast('已' + label + '（' + applied + ' 音，可撤销）');
    if (errs.length) setError('被拒：' + errs.join('；'));
  } catch (e) { setError(e.message); }
}

/* ---- 宿主装配（逐道 canvas 堆叠；无道 → 空态引导） ---- */

function show() {
  return store.viewMode === 'single' && store.autoLane.open && !!owner();
}

function reconcile() {
  if (!host) return;
  const ids = activeIds();
  const cur = mounted.map((m) => m.id);
  const same = cur.length === ids.length && cur.every((v, i) => v === ids[i]);
  const hasEmptyEl = !!host.querySelector('.alane-empty');
  const wantEmpty = ids.length === 0;
  if (same && hasEmptyEl === wantEmpty) { drawAll(); return; }
  for (const m of mounted) m.lane.destroy();
  mounted = [];
  host.innerHTML = '';
  if (!ids.length) {
    const d = document.createElement('div');
    d.className = 'alane-empty';
    d.textContent = '尚无自动化道——左栏右键轨道「添加自动化道…」（含「力度（柱状）」；或点轨行道展开钮 ▸）';
    host.appendChild(d);
    return;
  }
  for (const id of ids) {
    const row = document.createElement('div');
    row.className = 'alane';
    row.dataset.param = id;
    const cv = document.createElement('canvas');
    cv.title = '道「' + paramLabel(id) + '」：点=加点 ｜ 拖=移动 ｜ 双击/右键点=删除（每笔一个命令、可撤销）';
    row.appendChild(cv);
    host.appendChild(row);
    const lane = createLane(descriptorOf(id));
    lane.attach(cv);
    mounted.push({ id, lane });
  }
}

function drawAll() {
  if (!host) return;
  const lanePartner = !!(store.partner && store.partner.kind === 'lane');
  host.hidden = !show() || lanePartner;   /* v0.2 C2：道副区激活时道栈让位（同区空间冲突） */
  if (host.hidden) return;
  for (const m of mounted) m.lane.draw();
}

export function init(el) {
  host = el;
  if (!host) return;
  reconcile();
  for (const topic of ['state', 'auto', 'view', 'viewmode', 'playhead', 'batch_applied', 'meta']) {
    bus.on(topic, () => { reconcile(); drawAll(); });
  }
  drawAll();
}

export function resizeNow() {
  for (const m of mounted) { m.lane.resize(); m.lane.draw(); }
}
