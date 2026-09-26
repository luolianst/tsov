/* events.js —— 轻量事件总线 + SSE 订阅（模块间不直接引用 DOM，只经 bus 通信） */

const listeners = new Map();   // type -> Set<cb>

export const bus = {
  on(type, cb) {
    if (!listeners.has(type)) listeners.set(type, new Set());
    listeners.get(type).add(cb);
    return () => bus.off(type, cb);
  },
  off(type, cb) {
    const set = listeners.get(type);
    if (set) set.delete(cb);
  },
  dispatch(type, payload) {
    const set = listeners.get(type);
    if (!set) return;
    for (const cb of Array.from(set)) {
      try { cb(payload); } catch (e) { console.error('[bus]', type, e); }
    }
  },
};

/* SSE 事件类型（docs/05 §三）：原样转发到本地总线 */
const SSE_TYPES = [
  'agent_turn', 'agent_tool', 'agent_answer', 'agent_error', 'agent_delta',
  'state_updated', 'diff_applied', 'playback_start', 'playback_stop',
  'render_progress', 'render_done',
  /* M-V8 E3 段2：处理链（agent/REST 通道触发的运行也驱动 UI 刷新） */
  'chain_started', 'chain_step', 'chain_finished',
  /* M-V8 E4 段2：调参批次（suggest/apply/discard 通道触发） */
  'tune_updated',
];

let current = null;   // EventSource

export function connectEvents(projectName) {
  disconnectEvents();
  if (!projectName) return;
  const es = new EventSource('/api/projects/' + encodeURIComponent(projectName) + '/events');
  for (const t of SSE_TYPES) {
    es.addEventListener(t, (ev) => {
      let data = {};
      try { data = JSON.parse(ev.data); } catch (e) { /* 忽略坏帧 */ }
      bus.dispatch(t, data);
    });
  }
  es.onerror = () => bus.dispatch('sse_error', {});
  current = es;
  return es;
}

export function disconnectEvents() {
  if (current) { current.close(); current = null; }
}
