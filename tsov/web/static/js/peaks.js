/* peaks.js —— 波形峰谷公共管线（M-V8 E2 总谱 / E3 段1 单轨波形共用）
   v0.2 批C 后段（C9）：桶数随缩放——clamp(时长×pxPerSec/2, 900, 4000) 量化为档 [900,1800,3000,4000]；
   缓存键含桶数；分辨率不足时升档重取（pending 节流）；拉回后广播 bus 'peaks'（订阅方自行重绘）。
   未就绪返回 null（已触发后台拉取；先拿旧档顶住显示）。 */
import { api } from './api.js';
import { bus } from './events.js';
import { store } from './state.js';

const LEVELS = [900, 1800, 3000, 4000];
const cache = new Map();      // `${project}|${rel}|${buckets}` → data
const pending = new Set();    // 同上 key（拉取中）

function pickLevel(need) {
  for (const L of LEVELS) if (L >= need) return L;
  return LEVELS[LEVELS.length - 1];
}

/** 取波形峰值；未就绪返回 null（已触发后台拉取）。
    pxPerSec（可选）：按当前缩放选桶数档；精度不足时升档重取（节流）。 */
export function peaksGet(rel, pxPerSec) {
  if (!rel || !store.project) return null;
  const pps = Number(pxPerSec) > 0 ? Number(pxPerSec) : 0;
  let best = null, bestL = 0;
  for (const L of LEVELS) {
    const d = cache.get(store.project + '|' + rel + '|' + L);
    if (d && d.max && d.max.length) { if (L > bestL) { best = d; bestL = L; } }
  }
  if (best) {
    const need = (pps && best.seconds) ? Math.ceil((best.seconds * pps) / 2) : LEVELS[0];
    const want = pickLevel(Math.max(need, LEVELS[0]));
    if (bestL >= want) return best;               // 精度足够
    fetchLevel(rel, want);                        // 升档重取（先拿旧档顶住显示）
    return best;
  }
  fetchLevel(rel, LEVELS[0]);
  return null;
}

function fetchLevel(rel, buckets) {
  const key = store.project + '|' + rel + '|' + buckets;
  if (pending.has(key)) return;
  pending.add(key);
  api.fetchPeaks(store.project, rel, buckets)
    .then((d) => { if (d && d.max && d.max.length) { cache.set(key, d); bus.dispatch('peaks', key); } })
    .catch(() => { /* 拿不到波形 → 调用方画占位 */ })
    .finally(() => pending.delete(key));
}
