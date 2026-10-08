/* autoroll.js —— 自动化道装配（单轨视图底部子道；v0.2 批C 前段：收敛为 lane_render.js 的第一消费例）。
   全泛化（设计件 D2=A）：渲染/命中/拖拽/提交 = 通用道组件（lane_render.js）；
   本文件只负责：道定义（音量/声像 → descriptor）、宿主容器装配、显示条件、命令层落盘。
   数据走命令层 set_automation（/batch，ADR-0017 同一动作路径）；
   交互：点空白=加点 ｜ 拖点=移动 ｜ 双击点 / 右键点=删除（每笔一个命令、可撤销）。
   显示：线性折线（与引擎 host/mix.py::_curve 的 np.interp 语义一致：段外取端点值）。 */

import { bus } from './events.js';
import { api } from './api.js';
import { store, setError, toast, refTag, paramRange, paramLabel } from './state.js';
import { createLane } from './lane_render.js';

/* 道定义：title/bottomTag = 画布内标签（与历史 autoroll 一字不差） */
const DEFS = {
  volume: { id: 'volume', title: '音量(0~200%)', bottomTag: '0%' },
  pan:    { id: 'pan',    title: '声像(L~R)',    bottomTag: 'L' },
};

let host = null;
let mounted = [];            // [{id, lane}]

function owner() {
  const ti = store.viewMode === 'single' ? store.singleTrack : -1;
  return store.score && ti >= 0 && store.score.tracks[ti] ? { ti, track: store.score.tracks[ti] } : null;
}

/* 当前应开的道（'both' = 音量+声像双道堆叠——框架泛化演示） */
function activeIds() {
  const p = store.autoLane.param;
  if (p === 'both') return ['volume', 'pan'];
  return [p === 'pan' ? 'pan' : 'volume'];
}

function descriptorOf(id) {
  const def = DEFS[id];
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

/* ---- 宿主装配（逐道 canvas 堆叠） ---- */

function show() {
  return store.viewMode === 'single' && store.autoLane.open && !!owner();
}

function reconcile() {
  if (!host) return;
  const ids = activeIds();
  const cur = mounted.map((m) => m.id);
  if (cur.length === ids.length && cur.every((v, i) => v === ids[i])) { drawAll(); return; }
  for (const m of mounted) m.lane.destroy();
  mounted = [];
  host.innerHTML = '';
  for (const id of ids) {
    const row = document.createElement('div');
    row.className = 'alane';
    const cv = document.createElement('canvas');
    cv.title = '自动化道：点=加点 ｜ 拖=移动 ｜ 双击/右键点=删除（每笔一个命令、可撤销）';
    row.appendChild(cv);
    host.appendChild(row);
    const lane = createLane(descriptorOf(id));
    lane.attach(cv);
    mounted.push({ id, lane });
  }
}

function drawAll() {
  if (!host) return;
  host.hidden = !show();
  if (host.hidden) return;
  for (const m of mounted) m.lane.draw();
}

export function init(el) {
  host = el;
  if (!host) return;
  reconcile();
  for (const topic of ['state', 'auto', 'view', 'viewmode', 'playhead', 'batch_applied', 'meta']) {
    bus.on(topic, () => { if (topic === 'auto') reconcile(); drawAll(); });
  }
  drawAll();
}

export function resizeNow() {
  for (const m of mounted) { m.lane.resize(); m.lane.draw(); }
}
