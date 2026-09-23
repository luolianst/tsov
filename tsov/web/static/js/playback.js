/* playback.js —— 播放：浏览器 wav 试听（驱动播放头）+ 后端宿主播放（sounddevice）
   M-V8 E1：播放轴定位（seekTo/rewind）、循环区间（wav 过界回跳 / 宿主 loop 参数）、
   节拍器（WebAudio 合成，随播放走拍、强拍高音）、停止停在原地（Home 回零点）。 */

import { api } from './api.js';
import { bus } from './events.js';
import { store, setPlayhead, setPlaying, setLoopOn, setMetronome, toast, setError, setMeter } from './state.js';

let audio = null;        // <audio> wav 试听
let rafId = 0;
let hostT0 = 0;          // 后端播放起始时刻（playback_start；含 start 偏移）
let playGen = 0;         // E1 修复：起播代际——停止作废在途起播（加载中点⏹不再事后冒播）

/* ---------------- E5 段2：电平表（试听通路；MediaElementSource 单次绑定） ---------------- */

let meterCtx = null, meterSrc = null, meterSplit = null;
let meterAn = [null, null], meterBuf = [null, null];
let meterHold = [0, 0], meterClipT = 0, meterLastPush = 0, meterErr = null;

function ensureMeter() {
  if (meterCtx) return meterCtx;
  const AC = window.AudioContext || window.webkitAudioContext;
  if (!AC) return null;
  try {
    meterCtx = new AC();
    meterSrc = meterCtx.createMediaElementSource(ensureAudio());   // 单次绑定（audio 为单例）
    meterSrc.connect(meterCtx.destination);                        // 监听原样出声
    meterSplit = meterCtx.createChannelSplitter(2);
    meterSrc.connect(meterSplit);
    for (let i = 0; i < 2; i++) {
      meterAn[i] = meterCtx.createAnalyser();
      meterAn[i].fftSize = 1024;
      meterBuf[i] = new Float32Array(meterAn[i].fftSize);
      meterSplit.connect(meterAn[i], i);
    }
  } catch (e) {
    meterErr = String((e && e.message) || e);
    try { console.error('[meter]', e); } catch (_) { /* 忽略 */ }
    meterCtx = null;   // 绑定失败（旧内核）→ 弃表，不影响播放
  }
  return meterCtx;
}

/* CDP 诊断口：表初始化状态/错误 + 音频元素状态（不影响播放） */
export function meterDebug() {
  return {
    ok: !!meterCtx, err: meterErr, state: meterCtx ? meterCtx.state : null, an: !!meterAn[0],
    audio: audio ? {
      paused: audio.paused, ct: Math.round((audio.currentTime || 0) * 1000) / 1000,
      dur: Math.round((audio.duration || 0) * 1000) / 1000, ready: audio.readyState,
      err: audio.error ? ("code " + audio.error.code) : null, src: String(audio.src || "").slice(-24),
    } : null,
  };
}

function setFill(sel, v) {
  const el = document.querySelector(sel);
  if (!el) return;
  el.style.width = Math.round(Math.min(1, v) * 100) + '%';
  el.classList.toggle('hot', v >= 0.85);
}

function setHoldMark(sel, v) {
  const el = document.querySelector(sel);
  if (el) el.style.left = Math.round(Math.min(1, v) * 100) + '%';
}

function meterTick() {
  const on = store.playing === 'wav';
  if (on) {
    const ctx = ensureMeter();
    if (ctx && ctx.state !== 'running') ctx.resume().catch(() => {});
  }
  if (!meterCtx || !meterAn[0]) return;
  const now = performance.now();
  const lv = [0, 0];
  if (on) {
    for (let i = 0; i < 2; i++) {
      meterAn[i].getFloatTimeDomainData(meterBuf[i]);
      let m = 0;
      const b = meterBuf[i];
      for (let j = 0; j < b.length; j++) { const a = Math.abs(b[j]); if (a > m) m = a; }
      lv[i] = Math.min(1, m);
    }
  }
  for (let i = 0; i < 2; i++) meterHold[i] = Math.max(lv[i], meterHold[i] * 0.94);   // 保持：~12dB/s 衰减
  if (lv[0] >= 0.999 || lv[1] >= 0.999) meterClipT = now;
  const clip = (now - meterClipT) < 1500;
  setFill('#mt-l', lv[0]); setFill('#mt-r', lv[1]);
  setHoldMark('#mt-lh', meterHold[0]); setHoldMark('#mt-rh', meterHold[1]);
  const clipEl = document.querySelector('#mt-clip');
  if (clipEl) clipEl.hidden = !clip;
  if (now - meterLastPush > 100) {   // store 快照 10Hz 节流（CDP 断言口）
    meterLastPush = now;
    setMeter({ l: Math.round(lv[0] * 1000) / 1000, r: Math.round(lv[1] * 1000) / 1000,
               hold_l: Math.round(meterHold[0] * 1000) / 1000, hold_r: Math.round(meterHold[1] * 1000) / 1000,
               clip });
  }
}

function ensureAudio() {
  if (audio) return audio;
  audio = new Audio();
  audio.preload = 'auto';
  /* E1：播完停在原地（不再自动回零；Home 回零点） */
  audio.addEventListener('ended', () => { setPlaying(null); });
  audio.addEventListener('pause', () => { if (store.playing === 'wav') setPlaying(null); });
  return audio;
}

/* ---------------- E1：节拍器（WebAudio 合成） ---------------- */

let mctx = null, mNext = 0, mLast = 0;

function ensureMctx() {
  if (!mctx) {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return null;
    mctx = new AC();
  }
  return mctx;
}

function clickAt(ctx, when, strong) {
  const o = ctx.createOscillator(), g = ctx.createGain();
  o.type = 'square';
  o.frequency.value = strong ? 1660 : 1100;
  g.gain.setValueAtTime(0.0001, when);
  g.gain.exponentialRampToValueAtTime(strong ? 0.5 : 0.28, when + 0.003);
  g.gain.exponentialRampToValueAtTime(0.0001, when + 0.05);
  o.connect(g).connect(ctx.destination);
  o.start(when);
  o.stop(when + 0.07);
}

/** 拍 = 拍号分母单位：beat = (60/BPM)·(4/den)；强拍 = 每小节第 1 拍。 */
function beatInfo() {
  const sig = (store.score && store.score.time_signature) || '4/4';
  const m = /^\s*(\d+)\s*\/\s*(\d+)\s*$/.exec(String(sig));
  const num = m ? parseInt(m[1], 10) : 4;
  const den = m ? parseInt(m[2], 10) : 4;
  const bpm = (store.score && store.score.tempo) || 120;
  return { dur: (60 / bpm) * (4 / (den || 4)), num: Math.max(1, num || 4) };
}

function metroTick() {
  if (!store.metronome) { mNext = 0; return; }
  const ctx = ensureMctx();
  if (!ctx) return;
  if (ctx.state === 'suspended') ctx.resume().catch(() => {});
  const { dur, num } = beatInfo();
  const now = store.playhead;
  /* 起播 / 定位 / 循环回跳后重同步 */
  if (now + 0.02 < mLast || mNext < now - 0.02 || mNext > now + 2) mNext = Math.ceil(now / dur) * dur;
  mLast = now;
  while (mNext < now + 0.15) {
    const idx = Math.round(mNext / dur);
    clickAt(ctx, ctx.currentTime + (mNext - now), idx % num === 0);
    mNext += dur;
  }
}

/* ---------------- 播放头动画 ---------------- */

/** 宿主播放头展示用：循环区间内回卷。 */
function loopedPos(t) {
  const L = store.loop;
  if (!store.loopOn || !L || t <= L.end) return t;
  const len = L.end - L.start;
  if (len <= 0) return t;
  return L.start + ((t - L.start) % len);
}

function tick() {
  if (store.playing === 'wav' && audio) {
    /* E1：wav 循环过界回跳 */
    const L = store.loop;
    if (store.loopOn && L && audio.currentTime >= L.end) {
      try { audio.currentTime = L.start; } catch (e) { /* 忽略 */ }
    }
    setPlayhead(audio.currentTime);
  } else if (store.playing === 'host') {
    setPlayhead(loopedPos((performance.now() - hostT0) / 1000));
  }
  if (store.playing) metroTick();
  meterTick();                      // E5 段2：电平表（试听通路；停止时自然衰减到 0）
  rafId = requestAnimationFrame(tick);
}

function ensureRaf() { if (!rafId) rafId = requestAnimationFrame(tick); }

/* ---------------- E1：定位 / 回零 ---------------- */

/** 定位播放轴（标尺点击 / 书签双击）。播放中（wav）直接跳；宿主播放在跑时忽略（不可直播跳）。 */
export function seekTo(t) {
  const v = Math.max(0, Number(t) || 0);
  setPlayhead(v);
  mNext = 0;
  if (store.playing === 'wav' && audio) {
    try { audio.currentTime = v; } catch (e) { /* 忽略 */ }
  }
}

/** 回零点（Home）。 */
export function rewind() {
  seekTo(0);
}

/* ---------------- 播放控制 ---------------- */

export async function playWav() {
  if (!store.project) { setError('先打开一个工程'); return; }
  ensureMeter();                    // E5：在用户手势内建 AudioContext（autoplay 政策友好）
  const a = ensureAudio();
  const gen = ++playGen;            // E1：本次起播代际（stop/新起播会作废）
  let from = Math.max(0, store.playhead || 0);
  const L = store.loop;
  /* E1：循环开启且播放轴在区间外 → 从区间头起播 */
  if (store.loopOn && L && (from < L.start || from >= L.end)) from = L.start;
  try {
    /* src 带时间戳：工程每次变化（渲染自动更新）后强制刷新流 */
    a.src = api.wavUrl(store.project) + '?v=' + Date.now();
    await new Promise((res, rej) => {
      const ok = () => { a.removeEventListener('loadedmetadata', ok); a.removeEventListener('error', bad); res(); };
      const bad = () => { a.removeEventListener('loadedmetadata', ok); a.removeEventListener('error', bad); rej(new Error('wav 加载失败（先「渲染」生成试听 wav）')); };
      a.addEventListener('loadedmetadata', ok);
      a.addEventListener('error', bad);
    });
    if (gen !== playGen) return;      // 加载期间已按停止/改播 → 不冒播
    if (from > 0) {
      const dur = a.duration;
      a.currentTime = (Number.isFinite(dur) && dur > 0) ? Math.min(from, Math.max(0, dur - 0.05)) : from;
    }
    await a.play();
    if (gen !== playGen) { try { a.pause(); } catch (e) { /* 忽略 */ } return; }
    setPlaying('wav');
    ensureRaf();
  } catch (e) {
    setError('试听失败：' + e.message);
  }
}

export function stopWav() {
  playGen++;                        // 作废在途起播
  if (audio) audio.pause();
  if (store.playing === 'wav') setPlaying(null);
  mNext = 0;
}

/** E1：停止（试听 + 宿主共用）；停在原地（Home 回零）。 */
export async function stopAll() {
  stopWav();
  if (store.playing === 'host' && store.project) {
    try { await api.playStop(store.project); } catch (e) { /* 忽略 */ }
  }
}

export async function playHost() {
  if (!store.project) { setError('先打开一个工程'); return; }
  const L = store.loop;
  const body = { start: Math.max(0, store.playhead || 0) };
  if (store.loopOn && L) {
    body.loop = [L.start, L.end];
    body.start = L.start;
  }
  try {
    toast(body.loop ? ('宿主播放中（循环 ' + L.start.toFixed(1) + '–' + L.end.toFixed(1) + 's，⏹ 停止）…') : '宿主播放中（阻塞到播完）…');
    const r = await api.play(store.project, body);
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
  opts.stopBtn.addEventListener('click', () => { stopAll(); });
  opts.playHostBtn.addEventListener('click', playHost);
  opts.renderBtn.addEventListener('click', rerender);

  /* E1：循环 / 节拍器开关按钮（状态类随 markers 事件同步） */
  const { loopBtn, metroBtn } = opts;
  if (loopBtn) loopBtn.addEventListener('click', () => {
    if (!store.loop) { setError('先划循环区间：在标尺上拖动'); return; }
    setLoopOn(!store.loopOn);
    toast(store.loopOn ? '循环开（区间 ' + store.loop.start.toFixed(1) + '–' + store.loop.end.toFixed(1) + 's）' : '循环关');
  });
  if (metroBtn) metroBtn.addEventListener('click', () => {
    setMetronome(!store.metronome);
    toast(store.metronome ? '节拍器开' : '节拍器关');
  });
  bus.on('markers', () => {
    if (loopBtn) loopBtn.classList.toggle('on', store.loopOn);
    if (metroBtn) metroBtn.classList.toggle('on', store.metronome);
  });

  window.__tsovMeterDebug = meterDebug;   // E5 段2：CDP 诊断口
  /* M-V7 D1：命令层落盘（手势/面板参数）→ 松手即听；渲染进度轻提示 */
  bus.on('batch_applied', () => scheduleFresh());
  bus.on('render_progress', (ev) => { if (ev && ev.state === 'render') toast('重渲 ' + ev.name + '…'); });

  /* SSE：后端播放事件驱动播放头动画 */
  bus.on('playback_start', (d) => {
    const st = (d && d.start) || 0;
    hostT0 = performance.now() - st * 1000;
    mNext = 0;
    setPlaying('host');
    setPlayhead(st);
    ensureRaf();
  });
  bus.on('playback_stop', () => {
    /* E1：停在原地（不再回零） */
    if (store.playing === 'host') setPlaying(null);
  });

  bus.dispatch('markers');
}
