/* peaks.js —— 波形峰谷公共管线（M-V8 E2 总谱 / E3 段1 单轨波形共用）
   缓存 `${project}|${rel}` → {seconds,min,max}（内容寻址文件名 = 天然失效键）；
   未就绪时触发一次拉取并返回 null，拉回后广播 bus 'peaks'（订阅方自行重绘）。 */
import { api } from './api.js';
import { bus } from './events.js';
import { store } from './state.js';

const cache = new Map();
const pending = new Set();

/** 取波形峰值；未就绪返回 null（已触发后台拉取）。 */
export function peaksGet(rel) {
  if (!rel || !store.project) return null;
  const key = store.project + '|' + rel;
  const hit = cache.get(key);
  if (hit) return hit;
  if (!pending.has(key)) {
    pending.add(key);
    api.fetchPeaks(store.project, rel, 900)
      .then((d) => { if (d && d.max && d.max.length) { cache.set(key, d); bus.dispatch('peaks', key); } })
      .catch(() => { /* 拿不到波形 → 调用方画占位 */ })
      .finally(() => pending.delete(key));
  }
  return null;
}
