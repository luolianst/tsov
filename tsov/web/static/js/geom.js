/* geom.js —— 共享视图几何（v0.2 批C 前段·地基件 R2；设计件 §3.3）。
   时间↔x 的视图变换单一来源（roll / timeline / lanes / audio_edit / lane_render 共用）；
   KEYS_W 已由 state.js 共享。行为口径 = 原各处副本，一字不差（raw、不钳制——
   需要 t ≥ 0 的调用方自行 Math.max(0, tOf(x))，与 autoroll 历史口径一致）。 */

import { KEYS_W, store } from './state.js';

export function xOf(t) { return KEYS_W + (t - store.view.scrollSec) * store.view.pxPerSec; }
export function tOf(x) { return store.view.scrollSec + (x - KEYS_W) / store.view.pxPerSec; }
