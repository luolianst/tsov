/* dock.js —— 下方面板：效果器 / 音源 / 混音台（UI 批A；可折叠 + localStorage 记忆）
   与命令层 op 直连：add_effect / remove_effect / set_instrument / set_track_mix */

import { bus } from './events.js';
import { api } from './api.js';
import { store, setError, refTag } from './state.js';
import { themeName, trackColors } from './theme.js';
import { initChain, enterChain } from './chain.js';

let dockEl = null, chanPane = null, fxPane = null, srcPane = null, mixPane = null, hintEl = null;
let recPane = null;
let chainPane = null;
let meta = { programs: [], effect_kinds: [] };
let metaLoaded = false;
/* M-V8 E5 段2：效果目标（当前轨 / 各总线 / master） */
let fxTarget = { mode: 'track', ref: '' };
let fxEdit = null;   // v0.2 批C 后段（P26）：效果参数编辑器槽位（null = 关闭）

function selectedTrack() {
  const i = (store.selection && store.selection.track) || 0;
  return store.score && store.score.tracks[i] ? { track: store.score.tracks[i], index: i } : null;
}

async function post(label, commands) {
  if (!store.project) { setError('先打开一个工程'); return null; }
  try {
    const r = await api.postBatch(store.project, label, commands, '面板：' + label);
    if (r.applied) bus.dispatch('toast', '已执行 ' + label + ' ' + refTag(r));
    else setError('被拒：' + (r.errors || []).join('；'));
    return r;
  } catch (e) { setError(e.message); return null; }
}

async function loadMeta() {
  if (metaLoaded) return;
  try {
    const r = await api.meta();
    meta = { programs: r.programs || [], effect_kinds: r.effect_kinds || [] };
    metaLoaded = true;
  } catch (e) { /* 端点缺失时用兜底列表 */ }
  if (!meta.effect_kinds.length) {
    meta.effect_kinds = ['reverb', 'delay', 'compressor', 'chorus', 'distortion', 'gain', 'highpass', 'lowpass', 'limiter', 'brickwall', 'phaser', 'vst3'];
  }
}

/* ---------------- 效果器 tab ---------------- */
function renderFx() {
  if (!fxPane) return;
  fxPane.innerHTML = '';
  const cur = selectedTrack();
  if (!cur) {
    fxPane.innerHTML = '<div class="fx-none">（先选一条轨道）</div>';
    return;
  }
  /* E5 段2：目标解析——当前轨 / 各总线 / master */
  const busList = (store.score && store.score.buses) || [];
  if (fxTarget.mode === 'bus' && !busList.some((b) => b.name === fxTarget.ref)) fxTarget = { mode: 'track', ref: '' };
  const targetBus = fxTarget.mode === 'bus' ? busList.find((b) => b.name === fxTarget.ref) : null;
  const isTrackTarget = fxTarget.mode === 'track';
  const chain = isTrackTarget
    ? ((cur.track.instrument && cur.track.instrument.effects) || [])
    : (fxTarget.mode === 'master'
      ? ((store.score.master && store.score.master.effects) || [])
      : ((targetBus && targetBus.effects) || []));
  const tbase = isTrackTarget
    ? { target: 'track' }
    : (fxTarget.mode === 'master' ? { target: 'master' } : { target: 'bus', ref: fxTarget.ref });
  const wrap = document.createElement('div');
  wrap.className = 'fx-chain';
  chain.forEach((fx, i) => {
    const slot = document.createElement('div');
    slot.className = 'fx-slot';
    const hd = document.createElement('div');
    hd.className = 'fx-hd';
    const dot = document.createElement('span');
    dot.className = 'fx-dot';
    const nm = document.createElement('span');
    nm.className = 'fx-name';
    nm.textContent = fx.type;
    hd.appendChild(dot);
    hd.appendChild(nm);
    const sub = document.createElement('div');
    sub.className = 'fx-sub';
    const keys = Object.keys(fx.params || {});
    sub.textContent = keys.slice(0, 3).map((k) => k.replace(/_db|_hz|_seconds|_level/g, '') + ' ' + fx.params[k]).join(' · ') || '默认参数';
    const x = document.createElement('button');
    x.className = 'fx-x';
    x.textContent = '✕';
    x.title = '移除该效果（一个 commit，可撤销）';
    x.onclick = () => post('移除效果 ' + fx.type, [{ op: 'remove_effect', track: cur.index, index: i, value: Object.assign({}, tbase) }]);
    slot.appendChild(hd);
    slot.appendChild(sub);
    /* v0.2 批C 后段（P26）：⚙ 参数编辑（注册表驱动；一命令 set_effect_params） */
    const gear = document.createElement('button');
    gear.className = 'fx-gear';
    gear.textContent = '⚙';
    gear.title = '编辑该效果参数（范围=注册表；一命令提交，可撤销）';
    gear.onclick = (e) => { e.stopPropagation(); fxEdit = (fxEdit === i ? null : i); renderFx(); };
    slot.appendChild(gear);
    slot.appendChild(x);
    wrap.appendChild(slot);
    if (i < chain.length - 1) {
      const ar = document.createElement('span');
      ar.className = 'fx-arrow';
      ar.textContent = '›';
      wrap.appendChild(ar);
    }
  });
  const tgtSel = document.createElement('select');
  tgtSel.className = 'fx-kind';
  tgtSel.title = '效果目标：当前轨 / 总线 / master（E5）';
  {
    const ot = document.createElement('option');
    ot.value = 'track'; ot.textContent = '当前轨'; tgtSel.appendChild(ot);
    for (const b of busList) {
      const ob = document.createElement('option');
      ob.value = 'bus:' + b.name; ob.textContent = '总线 ' + b.name; tgtSel.appendChild(ob);
    }
    const om = document.createElement('option');
    om.value = 'master'; om.textContent = 'master'; tgtSel.appendChild(om);
    tgtSel.value = isTrackTarget ? 'track' : (fxTarget.mode === 'master' ? 'master' : 'bus:' + fxTarget.ref);
    tgtSel.onchange = () => {
      const v = tgtSel.value;
      fxTarget = v.indexOf('bus:') === 0 ? { mode: 'bus', ref: v.slice(4) } : { mode: v, ref: '' };
      renderFx();
    };
  }
  const kindSel = document.createElement('select');
  kindSel.className = 'fx-kind';
  kindSel.title = '选择效果类型后点「＋」加入链尾';
  for (const k of meta.effect_kinds) {
    const o = document.createElement('option');
    o.value = k; o.textContent = k;
    kindSel.appendChild(o);
  }
  const add = document.createElement('button');
  add.className = 'fx-add';
  add.textContent = '＋';
  add.title = '添加效果（默认参数，可在检查器/预设里再调）';
  add.onclick = async () => {
    const v = Object.assign({}, tbase, { type: kindSel.value });
    if (kindSel.value === 'vst3') {
      const p = window.prompt('vst3 效果：内层二进制路径（相对仓库或绝对路径）',
        'vendor/vst3/TAL-Chorus-LX.vst3/Contents/x86_64-win/TAL-Chorus-LX.vst3');
      if (!p || !p.trim()) return;
      v.params = { path: p.trim() };
    }
    await post('添加效果 ' + kindSel.value, [{ op: 'add_effect', track: cur.index, value: v }]);
  };
  wrap.appendChild(tgtSel);
  wrap.appendChild(kindSel);
  wrap.appendChild(add);
  fxPane.appendChild(wrap);

  /* v0.2 批C 后段（P26）：效果参数编辑器（注册表驱动；仅作用于目标链的选中槽） */
  if (fxEdit != null && fxEdit >= 0 && fxEdit < chain.length) {
    const fxe = chain[fxEdit];
    const specs = ((store.metaParams && store.metaParams.effects) || {})[String(fxe.type || '')] || {};
    const ed = document.createElement('div');
    ed.className = 'fx-edit';
    const ttl = document.createElement('div');
    ttl.className = 'fx-edit-ttl';
    ttl.textContent = '参数编辑：' + fxe.type + '（第 ' + (fxEdit + 1) + ' 槽；范围=注册表）';
    ed.appendChild(ttl);
    const keys = Object.keys(specs);
    const inputs = {};
    if (!keys.length) {
      const none = document.createElement('div');
      none.className = 'fx-none';
      none.textContent = '（该类型无注册表参数——vst3 动态参数请走插件面）';
      ed.appendChild(none);
    }
    for (const k of keys) {
      const row = document.createElement('div');
      row.className = 'fx-edit-row';
      const lb = document.createElement('label');
      lb.textContent = k;
      const rng = Array.isArray(specs[k]) ? specs[k] : null;
      const sld = document.createElement('input');
      sld.type = 'range'; sld.className = 'fx-edit-sld';
      if (rng) { sld.min = rng[0]; sld.max = rng[1]; sld.step = ((rng[1] - rng[0]) / 100) || 0.01; }
      const num = document.createElement('input');
      num.type = 'number'; num.step = 'any'; num.className = 'fx-edit-num';
      if (rng) { num.min = rng[0]; num.max = rng[1]; }
      const curVal = (fxe.params || {})[k];
      num.value = (curVal == null ? '' : String(curVal));
      num.placeholder = rng ? ('[' + rng[0] + '~' + rng[1] + ']') : '默认';
      sld.value = (curVal == null ? (rng ? (rng[0] + rng[1]) / 2 : 0) : curVal);
      sld.oninput = () => { num.value = String(Math.round(Number(sld.value) * 1000) / 1000); };
      num.oninput = () => { const vv = Number(num.value); if (Number.isFinite(vv)) sld.value = String(vv); };
      row.appendChild(lb); row.appendChild(sld); row.appendChild(num);
      ed.appendChild(row);
      inputs[k] = num;
    }
    const ft = document.createElement('div');
    ft.className = 'fx-edit-ft';
    const applyB = document.createElement('button');
    applyB.className = 'primary fx-edit-apply';
    applyB.textContent = '应用';
    applyB.onclick = async () => {
      const patch = {};
      let n = 0;
      for (const [k, el] of Object.entries(inputs)) {
        const raw = el.value.trim();
        if (raw === '') continue;
        const vv = Number(raw);
        if (!Number.isFinite(vv)) continue;
        const oldV = (fxe.params || {})[k];
        if (oldV == null || Math.abs(vv - Number(oldV)) > 1e-9) { patch[k] = vv; n += 1; }
      }
      if (!n) { bus.dispatch('toast', '无改动'); return; }
      const idxEdit = fxEdit;
      await post('参数编辑 ' + fxe.type, [{ op: 'set_effect_params', track: cur.index, value: Object.assign({}, tbase, { index: idxEdit, params: patch }) }]);
      fxEdit = null;
      renderFx();
      setTimeout(renderFx, 450);   /* 批处理回包后刷新显示值 */
    };
    const closeB = document.createElement('button');
    closeB.textContent = '收起';
    closeB.onclick = () => { fxEdit = null; renderFx(); };
    /* v0.2 批C 后段（P26）：重置 = 回填引擎默认（注册表 effect_defaults；点「应用」才写入） */
    const resetB = document.createElement('button');
    resetB.className = 'fx-edit-reset';
    resetB.textContent = '重置（回默认）';
    resetB.title = '各参数填回引擎默认值（/api/meta params.effect_defaults）；点「应用」才落盘';
    resetB.onclick = () => {
      const defs = ((store.metaParams && store.metaParams.effect_defaults) || {})[String(fxe.type || '')] || {};
      if (!Object.keys(defs).length) { bus.dispatch('toast', '该类型无默认值表（vst3 走插件面）'); return; }
      let n = 0;
      for (const [k, el] of Object.entries(inputs)) {
        if (defs[k] == null) continue;
        el.value = String(defs[k]);
        const sld = el.parentElement && el.parentElement.querySelector('.fx-edit-sld');
        if (sld) sld.value = String(defs[k]);
        n += 1;
      }
      bus.dispatch('toast', n ? ('已回填 ' + n + ' 项默认值（点「应用」写入）') : '无默认值可回填');
    };
    ft.appendChild(applyB);
    ft.appendChild(resetB);
    ft.appendChild(closeB);
    ed.appendChild(ft);
    fxPane.appendChild(ed);
  } else if (fxEdit != null) {
    fxEdit = null;
  }
}

/* ---------------- 音源 tab ---------------- */
function renderSrc() {
  if (!srcPane) return;
  srcPane.innerHTML = '';
  const cur = selectedTrack();
  if (!cur) {
    srcPane.innerHTML = '<div class="fx-none">（先选一条轨道）</div>';
    return;
  }
  const inst = cur.track.instrument || {};
  const form = document.createElement('div');
  form.className = 'src-form';

  const row1 = document.createElement('div');
  row1.className = 'src-row';
  const l1 = document.createElement('label');
  l1.textContent = '音源';
  const sel = document.createElement('select');
  const curProg = inst.program || '';
  const isPreset = meta.programs.includes(curProg);
  const o0 = document.createElement('option');
  o0.value = ''; o0.textContent = isPreset ? '（默认钢琴）' : ('当前：' + (curProg || '默认'));
  if (!isPreset) { o0.value = curProg; }
  sel.appendChild(o0);
  for (const name of meta.programs) {
    const o = document.createElement('option');
    o.value = name; o.textContent = name;
    if (name === curProg) o.selected = true;
    sel.appendChild(o);
  }
  sel.onchange = () => post('音源 ' + (sel.value || '默认'), [{ op: 'set_instrument', track: cur.index, value: { program: sel.value } }]);
  row1.appendChild(l1);
  row1.appendChild(sel);

  const row2 = document.createElement('div');
  row2.className = 'src-row';
  const l2 = document.createElement('label');
  l2.textContent = '路径';
  const pth = document.createElement('input');
  pth.placeholder = 'vst3:<内层二进制路径> 或 sfz:<路径>（回车应用）';
  pth.value = (curProg.startsWith('vst3:') || curProg.startsWith('sfz:')) ? curProg : '';
  pth.onkeydown = (e) => {
    if (e.key === 'Enter' && pth.value.trim()) {
      post('音源路径', [{ op: 'set_instrument', track: cur.index, value: { program: pth.value.trim() } }]);
    }
  };
  row2.appendChild(l2);
  row2.appendChild(pth);

  const note = document.createElement('div');
  note.className = 'src-note';
  note.textContent = 'backend=' + (inst.backend || 'fluidsynth') + ' ｜ GM 名 / vst3: / sfz: / 空串=默认 ｜ 改动一个 commit、可撤销';

  form.appendChild(row1);
  form.appendChild(row2);
  form.appendChild(note);
  srcPane.appendChild(form);
}

/* ---------------- 通道 tab（修正轮2：总谱=项目 profile ｜ 单轨=该轨通道条） ---------------- */
function renderChan() {
  if (!chanPane) return;
  chanPane.innerHTML = '';
  const strip = document.createElement('div');
  strip.className = 'chan';

  if (store.viewMode !== 'single') {
    /* 项目 profile（总谱视图默认显示） */
    const nm = document.createElement('span');
    nm.className = 'ch-name';
    nm.innerHTML = '<b>项目 profile</b><span class="chan-sub muted small">主输出 · master</span>';

    const volCell = document.createElement('span');
    volCell.className = 'ch-cell';
    const volLb = document.createElement('label'); volLb.textContent = '主输出';
    const vol = document.createElement('input');
    vol.type = 'range'; vol.min = '0'; vol.max = '200';
    const mv = store.score && store.score.master ? store.score.master.volume : 1;
    vol.value = String(Math.round((mv != null ? mv : 1) * 100));
    vol.disabled = true;
    vol.title = '总线混音命令层后续迭代（Q45/批4）';
    const volVal = document.createElement('span'); volVal.className = 'ch-val';
    volVal.textContent = vol.value + '%';
    volCell.appendChild(volLb); volCell.appendChild(vol); volCell.appendChild(volVal);

    const info = document.createElement('span');
    info.className = 'ch-hint';
    const sc = store.score;
    info.textContent = sc
      ? ('tempo ' + Math.round(sc.tempo) + ' · ' + (sc.time_signature || '4/4') + ' · ' +
         sc.tracks.length + ' 轨 · ' + sc.tracks.reduce((a, t) => a + t.notes.length, 0) + ' 音')
      : '（未打开工程）';

    const hint2 = document.createElement('span');
    hint2.className = 'ch-hint';
    hint2.textContent = '双击轨道 → 单轨写谱（此处改为该轨通道条）｜全轨推子见「混音台」';

    strip.appendChild(nm); strip.appendChild(volCell); strip.appendChild(info); strip.appendChild(hint2);
    chanPane.appendChild(strip);
    return;
  }

  /* 单轨：该轨通道条（音源 + 音量/声像/M/S + 效果链摘要） */
  const cur = selectedTrack();
  if (!cur) { chanPane.innerHTML = '<div class="fx-none">（先选一条轨道）</div>'; return; }
  const inst = cur.track.instrument || {};
  const tc = trackColors();

  const nm = document.createElement('span');
  nm.className = 'ch-name';
  const chip = document.createElement('span');
  chip.className = 'track-chip';
  chip.style.background = tc[cur.index % tc.length];
  chip.style.width = '4px'; chip.style.height = '16px'; chip.style.flex = 'none';
  const nameB = document.createElement('b');
  nameB.textContent = cur.track.name || ('track ' + cur.index);
  nm.appendChild(chip); nm.appendChild(nameB);

  /* 音源 */
  const srcCell = document.createElement('span');
  srcCell.className = 'ch-cell';
  const srcLb = document.createElement('label'); srcLb.textContent = '音源';
  const srcSel = document.createElement('select');
  const curProg = inst.program || '';
  const o0 = document.createElement('option');
  o0.value = ''; o0.textContent = meta.programs.includes(curProg) ? '（默认钢琴）' : ('当前：' + (curProg || '默认'));
  if (!meta.programs.includes(curProg)) o0.value = curProg;
  srcSel.appendChild(o0);
  for (const p of meta.programs) {
    const o = document.createElement('option');
    o.value = p; o.textContent = p;
    if (p === curProg) o.selected = true;
    srcSel.appendChild(o);
  }
  srcSel.onchange = () => post('音源 ' + (srcSel.value || '默认'), [{ op: 'set_instrument', track: cur.index, value: { program: srcSel.value } }]);
  srcCell.appendChild(srcLb); srcCell.appendChild(srcSel);

  /* 音量 / 声像 */
  const mkRange = (label, min, max, val, fmt, op) => {
    const cell = document.createElement('span');
    cell.className = 'ch-cell';
    const lb = document.createElement('label'); lb.textContent = label;
    const rg = document.createElement('input');
    rg.type = 'range'; rg.min = String(min); rg.max = String(max); rg.value = String(val);
    const vv = document.createElement('span'); vv.className = 'ch-val'; vv.textContent = fmt(val);
    rg.addEventListener('input', () => { vv.textContent = fmt(Number(rg.value)); });
    rg.addEventListener('change', () => post(label + ' ' + fmt(Number(rg.value)), [{ op: 'set_track_mix', track: cur.index, value: op(Number(rg.value)) }]));
    cell.appendChild(lb); cell.appendChild(rg); cell.appendChild(vv);
    return cell;
  };
  const volCell = mkRange('音量', 0, 200, Math.round((inst.volume != null ? inst.volume : 1) * 100),
    (v) => v + '%', (v) => ({ volume: v / 100 }));
  const panCell = mkRange('声像', -100, 100, Math.round((cur.track.pan || 0) * 100),
    (v) => (v === 0 ? 'C' : (v > 0 ? 'R' + v : 'L' + (-v))), (v) => ({ pan: v / 100 }));

  /* M/S */
  const msCell = document.createElement('span');
  msCell.className = 'ch-cell';
  const m = document.createElement('button');
  m.className = 'ms' + (cur.track.mute ? ' on' : '');
  m.textContent = 'M';
  m.onclick = () => post(cur.track.mute ? '取消静音' : '静音', [{ op: 'set_track_mix', track: cur.index, value: { mute: !cur.track.mute } }]);
  const s = document.createElement('button');
  s.className = 'ms solo' + (cur.track.solo ? ' on' : '');
  s.textContent = 'S';
  s.onclick = () => post(cur.track.solo ? '取消独奏' : '独奏', [{ op: 'set_track_mix', track: cur.index, value: { solo: !cur.track.solo } }]);
  msCell.appendChild(m); msCell.appendChild(s);

  /* 效果链摘要 */
  const fxCell = document.createElement('span');
  fxCell.className = 'ch-fx';
  const fxs = (inst.effects || []);
  const fxLb = document.createElement('label');
  fxLb.textContent = '效果链';
  fxLb.style.color = 'var(--muted)';
  fxLb.style.fontSize = 'var(--fs-xs)';
  fxCell.appendChild(fxLb);
  if (!fxs.length) {
    const none = document.createElement('span'); none.className = 'cf off'; none.textContent = '（空）';
    fxCell.appendChild(none);
  } else {
    fxs.forEach((fx, i) => {
      if (i) { const ar = document.createElement('span'); ar.className = 'chan-sub muted small'; ar.textContent = '›'; fxCell.appendChild(ar); }
      const c = document.createElement('span');
      c.className = 'cf';
      c.textContent = fx.type;
      fxCell.appendChild(c);
    });
  }

  strip.appendChild(nm);
  strip.appendChild(srcCell);
  strip.appendChild(volCell);
  strip.appendChild(panCell);
  strip.appendChild(msCell);
  strip.appendChild(fxCell);
  chanPane.appendChild(strip);
}

/* ---------------- 混音台 tab ---------------- */
function renderMix() {
  if (!mixPane) return;
  mixPane.innerHTML = '';
  if (!store.score || !store.score.tracks.length) {
    mixPane.innerHTML = '<div class="fx-none">（无音轨）</div>';
    return;
  }
  const wrap = document.createElement('div');
  wrap.className = 'mix-strip';
  const selTi = store.viewMode === 'single' ? store.singleTrack : (store.selection.track || 0);
  store.score.tracks.forEach((tr, ti) => {
    const inst = tr.instrument || {};
    const cell = document.createElement('div');
    cell.className = 'fader' + (ti === selTi ? ' sel' : '');
    cell.title = '选中轨：' + (tr.name || ('track ' + ti));
    const nm = document.createElement('div');
    nm.className = 'f-name';
    nm.textContent = tr.name || ('track ' + ti);
    nm.title = nm.textContent;
    const val = Math.round((inst.volume != null ? inst.volume : 1) * 100);
    const rg = document.createElement('input');
    rg.type = 'range'; rg.min = '0'; rg.max = '200'; rg.value = String(val);
    rg.title = '音量（%）';
    const vv = document.createElement('div');
    vv.className = 'f-val';
    vv.textContent = val + '%';
    rg.addEventListener('input', () => { vv.textContent = rg.value + '%'; });
    rg.addEventListener('change', () => post('音量 ' + rg.value + '%', [{ op: 'set_track_mix', track: ti, value: { volume: Number(rg.value) / 100 } }]));
    const ms = document.createElement('div');
    ms.className = 'f-ms';
    const m = document.createElement('button');
    m.className = 'ms' + (tr.mute ? ' on' : '');
    m.textContent = 'M';
    m.onclick = () => post(tr.mute ? '取消静音' : '静音', [{ op: 'set_track_mix', track: ti, value: { mute: !tr.mute } }]);
    const s = document.createElement('button');
    s.className = 'ms solo' + (tr.solo ? ' on' : '');
    s.textContent = 'S';
    s.onclick = () => post(tr.solo ? '取消独奏' : '独奏', [{ op: 'set_track_mix', track: ti, value: { solo: !tr.solo } }]);
    ms.appendChild(m);
    ms.appendChild(s);
    cell.appendChild(nm);
    cell.appendChild(rg);
    cell.appendChild(vv);
    cell.appendChild(ms);

    /* M-V8 E5 段2：Send 支路（post-fader → 目标总线；✕ = 移除） */
    const busNames = ((store.score && store.score.buses) || []).map((b) => b.name);
    const sends = tr.sends || {};
    const sendCell = document.createElement('div');
    sendCell.className = 'f-send';
    for (const sname of Object.keys(sends)) {
      const row = document.createElement('div');
      row.className = 'fs-row';
      const nmx = document.createElement('span');
      nmx.className = 'fs-nm';
      nmx.textContent = '→' + sname;
      nmx.title = 'Send 支路：post-fader → 总线 ' + sname;
      const rng = document.createElement('input');
      rng.type = 'range'; rng.min = '0'; rng.max = '100';
      rng.value = String(Math.round(Number(sends[sname]) * 100));
      rng.title = 'Send 量（%）：改动一个 commit、可撤销';
      rng.addEventListener('change', () => {
        const next = Object.assign({}, sends);
        next[sname] = Number(rng.value) / 100;
        post('Send ' + sname + ' ' + rng.value + '%', [{ op: 'set_track_mix', track: ti, value: { sends: next } }]);
      });
      const x = document.createElement('button');
      x.className = 'fs-x';
      x.textContent = '✕';
      x.title = '移除该 Send 支路';
      x.onclick = () => {
        const next = Object.assign({}, sends);
        delete next[sname];
        post('移除 Send ' + sname, [{ op: 'set_track_mix', track: ti, value: { sends: next } }]);
      };
      row.appendChild(nmx); row.appendChild(rng); row.appendChild(x);
      sendCell.appendChild(row);
    }
    if (busNames.some((bn) => sends[bn] == null)) {
      const addSel = document.createElement('select');
      addSel.title = '添加 Send：选目标总线（默认量 50%）';
      const o0 = document.createElement('option');
      o0.value = ''; o0.textContent = '＋ Send →';
      addSel.appendChild(o0);
      for (const bn of busNames) {
        if (sends[bn] != null) continue;
        const ob = document.createElement('option');
        ob.value = bn; ob.textContent = bn;
        addSel.appendChild(ob);
      }
      addSel.onchange = () => {
        if (!addSel.value) return;
        const next = Object.assign({}, sends);
        next[addSel.value] = 0.5;
        post('新增 Send ' + addSel.value, [{ op: 'set_track_mix', track: ti, value: { sends: next } }]);
      };
      sendCell.appendChild(addSel);
    }
    cell.appendChild(sendCell);
    wrap.appendChild(cell);
  });
  mixPane.appendChild(wrap);
}

function renderTabs() {
  const cur = selectedTrack();
  if (hintEl) hintEl.textContent = cur ? ((cur.track.name || ('track ' + cur.index)) + ' · 轨道参数') : '';
}

/* M-V8 E2 段 3：录音面板（设备 / 录制 / 监听 → 录完直进时间线） */
const recState = { devs: null, recording: false, startedAt: 0, timer: null, busy: false, project: null };

function recFmt(sec) {
  sec = Math.max(0, sec || 0);
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0') + '.' +
    String(Math.floor((sec % 1) * 10));
}

function renderRec() {
  if (!recPane) return;
  if (recState.recording) return;   // 录音中不重建（计时器原地更新）
  if (!store.project) { recPane.innerHTML = '<div class="fx-none">先打开一个工程</div>'; return; }
  recPane.innerHTML =
    '<div class="rec-wrap">' +
      '<div class="rec-row"><label>输入</label><select id="rec-in"></select>' +
      '<label>输出·监听</label><select id="rec-out"></select></div>' +
      '<div class="rec-row"><label class="rec-mon"><input type="checkbox" id="rec-monitor">' +
      ' 监听（软件直通，实测延迟约 30-90ms）</label></div>' +
      '<div class="rec-row"><button id="rec-btn" class="rec-btn">● 开始录制</button>' +
      '<span id="rec-timer" class="rec-timer">00:00.0</span>' +
      '<span class="rec-hint">录完自动入库（44.1k flac）并加入时间线</span></div>' +
      '<div class="rec-row"><button id="rec-v2s" class="chain-btn">转乐谱 ◈实验性</button>' +
      '<span class="rec-hint">录音 / 任意音频轨 → 哼唱快车道（降噪→响度→转录→量化→吸附）</span></div>' +
    '</div>';
  const selIn = recPane.querySelector('#rec-in');
  const selOut = recPane.querySelector('#rec-out');
  const btn = recPane.querySelector('#rec-btn');
  const fill = (sel, list, defIdx) => {
    sel.innerHTML = list.map((d) =>
      '<option value="' + d.index + '"' + (d.index === defIdx ? ' selected' : '') + '>' +
      d.index + ': ' + d.name + '</option>').join('');
  };
  (async () => {
    try {
      recState.devs = recState.devs || await api.recordDevices();
      fill(selIn, recState.devs.inputs, recState.devs.default_in);
      fill(selOut, recState.devs.outputs, recState.devs.default_out);
    } catch (e) { setError('设备枚举失败：' + e.message); }
  })();
  btn.addEventListener('click', () => recToggle(btn, selIn, selOut));
  /* E3 段2：「转乐谱」——切到处理链面板（voice2score 在 init 里接） */
  const v2s = recPane.querySelector('#rec-v2s');
  if (v2s) v2s.addEventListener('click', () => bus.dispatch('voice2score', {}));
}

async function recToggle(btn, selIn, selOut) {
  if (recState.busy) return;
  recState.busy = true;
  try {
    if (!recState.recording) {
      const monitor = recPane.querySelector('#rec-monitor').checked;
      recState.project = store.project;   // 记住开始时的工程（stop 用它）
      await api.recordStart(recState.project, {
        device: Number(selIn.value),
        out_device: monitor ? Number(selOut.value) : null,
        monitor,
      });
      recState.recording = true;
      recState.startedAt = Date.now();
      btn.textContent = '■ 停止';
      btn.classList.add('recording');
      recState.timer = setInterval(() => {
        const el = recPane.querySelector('#rec-timer');
        if (el) el.textContent = recFmt((Date.now() - recState.startedAt) / 1000);
      }, 200);
      bus.dispatch('toast', monitor ? '开始录制（监听已开）' : '开始录制');
    } else {
      btn.disabled = true;
      const r = await api.recordStop(recState.project, {});
      clearInterval(recState.timer);
      recState.timer = null;
      recState.recording = false;
      const secs = Number(r.seconds || 0);
      bus.dispatch('toast', '已录音 ' + secs.toFixed(1) + 's → 时间线' + (r.added ? '（已入轨）' : '（未加轨）'));
      renderRec();
    }
  } catch (e) {
    if (recState.timer) { clearInterval(recState.timer); recState.timer = null; }
    recState.recording = false;
    setError('录音：' + e.message);
    renderRec();
  } finally {
    recState.busy = false;
  }
}

export function renderAll() {
  renderChan();
  renderFx();
  renderSrc();
  renderMix();
  renderTabs();
}

function applyFold(folded) {
  if (!dockEl) return;
  dockEl.classList.toggle('folded', folded);
  const btn = dockEl.querySelector('#dock-fold');
  if (btn) btn.textContent = folded ? '▸ 展开' : '▾ 收起';
  try { localStorage.setItem('tsov.dock', folded ? 'folded' : 'open'); } catch (e) { /* ignore */ }
}

export function init(el) {
  dockEl = el;
  chanPane = dockEl.querySelector('#dock-chan');
  fxPane = dockEl.querySelector('#dock-fx');
  srcPane = dockEl.querySelector('#dock-src');
  mixPane = dockEl.querySelector('#dock-mix');
  recPane = dockEl.querySelector('#dock-rec');
  chainPane = dockEl.querySelector('#dock-chain');
  hintEl = dockEl.querySelector('#dock-hint');

  /* tab 切换 */
  const tabs = Array.from(dockEl.querySelectorAll('.dock-tab'));
  const panes = { chan: chanPane, fx: fxPane, src: srcPane, mix: mixPane, rec: recPane, chain: chainPane };
  const activateTab = (name) => {
    for (const x of tabs) x.classList.toggle('active', x.dataset.pane === name);
    for (const k of Object.keys(panes)) panes[k].classList.toggle('active', k === name);
    if (name === 'rec') renderRec();   // E2 段 3：录音面板懒渲染
    if (name === 'chain') enterChain();   // E3 段 2：处理链面板懒渲染
  };
  for (const t of tabs) t.addEventListener('click', () => activateTab(t.dataset.pane));
  bus.on('focus-chain', () => activateTab('chain'));   // D 件：chips「＋跑处理链」聚焦链页签
  /* E3 段2：「转乐谱」入口（录音面板按钮 / 音频轨右键）→ 切处理链 tab（#3 验收；链 = 预设默认挂载） */
  bus.on('voice2score', () => {
    activateTab('chain');
    const hasAudio = !!(store.score && store.score.tracks.some((t) => t.kind === 'audio'));
    bus.dispatch('toast', hasAudio
      ? '已挂链「哼唱快车道」：降噪→响度→转录→量化→吸附——点「运行全链」开始'
      : '本工程暂无音频轨：先录音或导入音频，链会以它为首环');
  });

  /* 折叠（记忆；无记忆时：紧凑档默认折叠） */
  dockEl.querySelector('#dock-fold').addEventListener('click', () => {
    applyFold(!dockEl.classList.contains('folded'));
  });
  let saved = null;
  try { saved = localStorage.getItem('tsov.dock'); } catch (e) { /* ignore */ }
  const compact = window.matchMedia('(max-width: 1439px)').matches;
  const folded = saved ? saved === 'folded' : compact;
  dockEl.classList.toggle('folded', folded);
  const btn = dockEl.querySelector('#dock-fold');
  if (btn) btn.textContent = folded ? '▸ 展开' : '▾ 收起';

  initChain(chainPane);   // E3 段2：处理链面板（自挂 bus；tab 打开时懒渲染）
  bus.on('state', renderAll);
  bus.on('selection', renderAll);
  bus.on('viewmode', renderAll);   // 修正轮2：总谱 ↔ 单轨切换 → 通道条跟随上下文
  loadMeta().then(renderAll);
  renderAll();
}
