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
  /* 批B P20/P23：流等待心跳 / 断流重试（后端新事件——白名单不登记即不转发） */
  'agent_wait', 'agent_retry',
  'state_updated', 'diff_applied', 'playback_start', 'playback_stop',
  'render_progress', 'render_done',
  /* M-V8 E3 段2：处理链（agent/REST 通道触发的运行也驱动 UI 刷新） */
  'chain_started', 'chain_step', 'chain_finished',
  /* M-V8 E4 段2：调参批次（suggest/apply/discard 通道触发） */
  'tune_updated',
  /* M-V8 E4 段3：配器批次（generate/apply/discard 通道触发） */
  'arrange_updated',
];

let current = null;      // 当前 EventSource
let currentName = '';    // 当前订阅工程名
let retryTimer = null;   // 自管重连定时器
let retryDelay = 0;      // 重连退避（毫秒）
let downSince = 0;       // 断线起点（0 = 连接正常）

export function connectEvents(projectName) {
  disconnectEvents();
  if (!projectName) return;
  currentName = projectName;
  open();
  return current;
}

function open() {
  const name = currentName;
  if (!name) return;
  const es = new EventSource('/api/projects/' + encodeURIComponent(name) + '/events');
  for (const t of SSE_TYPES) {
    es.addEventListener(t, (ev) => {
      let data = {};
      try { data = JSON.parse(ev.data); } catch (e) { /* 忽略坏帧 */ }
      bus.dispatch(t, data);
    });
  }
  es.onopen = () => {
    const wasDown = downSince > 0;
    downSince = 0;
    retryDelay = 0;
    /* 断线回补：重连成功后由上层重拉一次全量 state（断线期间的 state_updated 已丢） */
    if (wasDown) bus.dispatch('sse_reconnected', {});
  };
  es.onerror = () => {
    /* EventSource 在服务重启 / 非 200 响应等场景可能永久停摆（规范：fail the connection），
       一律关闭旧对象走自管重连：2s 起、×2 退避、封顶 15s，直到连上。 */
    es.close();
    if (current !== es) return;   // 已被替换（切工程 / 重连中）
    current = null;
    if (!downSince) downSince = Date.now();
    bus.dispatch('sse_error', {});
    clearTimeout(retryTimer);
    retryTimer = setTimeout(() => { if (currentName) open(); }, retryDelay || 2000);
    retryDelay = Math.min(retryDelay ? retryDelay * 2 : 2000, 15000);
  };
  current = es;
}

export function disconnectEvents() {
  clearTimeout(retryTimer);
  retryTimer = null;
  currentName = '';
  downSince = 0;
  retryDelay = 0;
  if (current) { current.close(); current = null; }
}
