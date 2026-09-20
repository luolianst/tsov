/* playback.js —— 播放：浏览器 wav 试听（驱动播放头）+ 后端宿主播放（sounddevice） */

import { api } from './api.js';
import { bus } from './events.js';
import { store, setPlayhead, setPlaying, toast, setError } from './state.js';

let audio = null;        // <audio> wav 试听
let rafId = 0;
let hostT0 = 0;          // 后端播放起始时刻（playback_start）

function ensureAudio() {
  if (audio) return audio;
  audio = new Audio();
  audio.preload = 'auto';
  audio.addEventListener('ended', () => { setPlaying(null); setPlayhead(0); });
  audio.addEventListener('pause', () => { if (store.playing === 'wav') setPlaying(null); });
  return audio;
}

function tick() {
  if (store.playing === 'wav' && audio) setPlayhead(audio.currentTime);
  else if (store.playing === 'host') setPlayhead((performance.now() - hostT0) / 1000);
  rafId = requestAnimationFrame(tick);
}

export async function playWav() {
  if (!store.project) { setError('先打开一个工程'); return; }
  const a = ensureAudio();
  try {
    /* src 带时间戳：工程每次变化（渲染自动更新）后强制刷新流 */
    a.src = api.wavUrl(store.project) + '?v=' + Date.now();
    await a.play();
    setPlaying('wav');
    if (!rafId) rafId = requestAnimationFrame(tick);
  } catch (e) {
    setError('试听失败：' + e.message);
  }
}

export function stopWav() {
  if (audio) audio.pause();
  if (store.playing === 'wav') { setPlaying(null); setPlayhead(0); }
}

export async function playHost() {
  if (!store.project) { setError('先打开一个工程'); return; }
  try {
    toast('宿主播放中（阻塞到播完）…');
    const r = await api.play(store.project);
    toast('宿主播放完毕（' + r.duration + 's）');
  } catch (e) {
    setError(e.message);
  }
}

export async function rerender() {
  if (!store.project) return;
  try {
    const r = await api.render(store.project);
    toast('已渲染：' + r.wav + '（' + r.duration + 's / ' + r.sr + 'Hz）');
    if (audio) audio.src = api.wavUrl(store.project) + '?v=' + Date.now();
  } catch (e) { setError(e.message); }
}

/* M-V7 D1（ADR-0018）：松手即听——手势落盘后自动重拼混音（缓存命中为亚秒级），播放中位置保持续播 */
let freshTimer = 0;

export function scheduleFresh() {
  clearTimeout(freshTimer);
  freshTimer = setTimeout(ensureFresh, 400);
}

export async function ensureFresh() {
  if (!store.project) return;
  const a = ensureAudio();
  const wasPlaying = store.playing === 'wav' && !a.paused;
  const pos = a.currentTime || 0;
  const t0 = performance.now();
  try {
    const r = await api.render(store.project);
    const n = (r.rendered || []).length;
    if (n > 0) toast('已重渲 ' + r.rendered.join('、') + '（' + Math.round(performance.now() - t0) + 'ms）');
    a.src = api.wavUrl(store.project) + '?v=' + Date.now();
    if (wasPlaying) {
      a.addEventListener('loadedmetadata', () => {
        try { a.currentTime = pos; a.play(); } catch (e) { /* 忽略 */ }
      }, { once: true });
    }
  } catch (e) { /* 静默：失败不打扰（下次手动试听会重试） */ }
}

export function init(opts) {
  opts.playWavBtn.addEventListener('click', playWav);
  opts.stopBtn.addEventListener('click', stopWav);
  opts.playHostBtn.addEventListener('click', playHost);
  opts.renderBtn.addEventListener('click', rerender);

  /* M-V7 D1：命令层落盘（手势/面板参数）→ 松手即听；渲染进度轻提示 */
  bus.on('batch_applied', () => scheduleFresh());
  bus.on('render_progress', (ev) => { if (ev && ev.state === 'render') toast('重渲 ' + ev.name + '…'); });

  /* SSE：后端播放事件驱动播放头动画 */
  bus.on('playback_start', (d) => {
    hostT0 = performance.now();
    setPlaying('host');
    setPlayhead(0);
    if (!rafId) rafId = requestAnimationFrame(tick);
  });
  bus.on('playback_stop', () => {
    if (store.playing === 'host') { setPlaying(null); setPlayhead(0); }
  });
}
