/* api.js —— REST 封装（docs/05 §三 全部接口；模块间只经本文件访问后端） */

import { noteUserAction } from './state.js';
import { bus } from './events.js';

async function req(path, opts) {
  let res;
  try {
    res = await fetch(path, opts);
  } catch (e) {
    throw new Error('网络错误：' + (e && e.message ? e.message : e));
  }
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || ('HTTP ' + res.status));
  return body;
}

function post(path, data) {
  return req(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data || {}),
  });
}

export const api = {
  health: () => req('/api/health'),
  meta: () => req('/api/meta'),
  listProjects: () => req('/api/projects'),
  createProject: (name, score) => post('/api/projects', { name, score: score || null }),
  importCandidates: () => req('/api/import-candidates'),
  importProject: (source, name) => post('/api/projects/import', { source, name: name || null }),

  getState: (name) => req('/api/projects/' + encodeURIComponent(name) + '/state'),
  postBatch: (name, label, commands, commitMessage) => {
    const desc = label || ('命令层：' + (commands || []).map((c) => c.op).join(' + '));
    return post('/api/projects/' + encodeURIComponent(name) + '/batch',
      { label: label || '', commands, commit_message: commitMessage || null }).then((r) => {
        /* 批B B1-4：成功落盘的用户手动操作 → 缓冲（下条消息回流给 agent） */
        if (r && r.applied > 0) {
          noteUserAction(desc);
          /* M-V7 D1：手势落盘成功 → 通知播放层「松手即听」（缓存重拼） */
          bus.dispatch('batch_applied', { name });
        }
        return r;
      });
  },
  undo: (name) => post('/api/projects/' + encodeURIComponent(name) + '/undo'),
  redo: (name) => post('/api/projects/' + encodeURIComponent(name) + '/redo'),
  rollback: (name, rev) =>
    post('/api/projects/' + encodeURIComponent(name) + '/rollback', { rev: rev || 'HEAD~1' }),
  getLog: (name) => req('/api/projects/' + encodeURIComponent(name) + '/log'),
  getSummary: (name) => req('/api/projects/' + encodeURIComponent(name) + '/summary'),
  render: (name, out) => post('/api/projects/' + encodeURIComponent(name) + '/render', { out: out || null }),
  play: (name, body) => post('/api/projects/' + encodeURIComponent(name) + '/play', body || {}),
  /* M-V8 E1：停止宿主播放（打断 sounddevice；试听/宿主共用停止按钮） */
  playStop: (name) => post('/api/projects/' + encodeURIComponent(name) + '/play/stop', {}),
  chat: (project, message, baseRev, annotations, selection, userActions) => post('/api/chat', { project, message, base_rev: baseRev || 'HEAD', annotations: annotations || null, selection: selection || null, user_actions: userActions || null }),
  chatStop: () => post('/api/chat/stop', {}),
  chatReset: (project) => post('/api/chat/reset', { project }),
  setTitle: (name, title) => post('/api/projects/' + encodeURIComponent(name) + '/title', { title }),
  exportProject: (name, opts) => post('/api/projects/' + encodeURIComponent(name) + '/export', opts || {}),
  favorite: (name) => post('/api/projects/' + encodeURIComponent(name) + '/favorite', {}),
  favorites: (name) => req('/api/projects/' + encodeURIComponent(name) + '/favorites'),
  /* 批B B1-2：动作级撤销（快照日志；seq 由 agent_tool 事件给出） */
  actionUndo: (name, seq) =>
    post('/api/projects/' + encodeURIComponent(name) + '/agent-actions/' + encodeURIComponent(seq) + '/undo', {}),
  actions: (name) => req('/api/projects/' + encodeURIComponent(name) + '/agent-actions'),
  /* M-V7 D1（ADR-0018）：stem 缓存 GC（无引用即清 + 清退役 .render-cache） */
  cacheGc: (name) => post('/api/projects/' + encodeURIComponent(name) + '/cache/gc', {}),
  /* M-V7 D2/D3（ADR-0019）：快照窗口 / 留存设置 / 收藏删除 */
  getWindow: (name) => req('/api/projects/' + encodeURIComponent(name) + '/window'),
  windowJump: (name, cursor) => post('/api/projects/' + encodeURIComponent(name) + '/window/jump', { cursor }),
  deleteFavorite: (name, tag) =>
    req('/api/projects/' + encodeURIComponent(name) + '/favorites?tag=' + encodeURIComponent(tag), { method: 'DELETE' }),
  getSettings: (name) => req('/api/projects/' + encodeURIComponent(name) + '/settings'),
  setSettings: (name, patch) => post('/api/projects/' + encodeURIComponent(name) + '/settings', patch),
  setGlobalSettings: (patch) => post('/api/settings', patch),
  listSessions: () => req('/api/sessions'),
  loadSession: (project, name) => post('/api/sessions/load', { project, name }),

  wavUrl: (name) => '/api/projects/' + encodeURIComponent(name) + '/wav',
  wavUrlRev: (name, rev) => '/api/projects/' + encodeURIComponent(name) + '/wav?rev=' + encodeURIComponent(rev || 'HEAD'),
  eventsUrl: (name) => '/api/projects/' + encodeURIComponent(name) + '/events',
  /* M-V8 E2：音频素材（导入双来源 / 波形峰值 / 原文件 URL） */
  importAudioPath: (name, path, label) =>
    post('/api/projects/' + encodeURIComponent(name) + '/audio/import', { path, name: label || null }),
  importAudioUpload: (name, file) => {
    const fd = new FormData();
    fd.append('file', file, file.name);
    fd.append('name', String(file.name || '').replace(/\.[^.]+$/, ''));   // 用原文件名（非临时名）
    return req('/api/projects/' + encodeURIComponent(name) + '/audio/import', { method: 'POST', body: fd });
  },
  /* M-V8 E5：MIDI 导入（双来源，与音频入库同模式） */
  importMidiPath: (name, path, label) =>
    post('/api/projects/' + encodeURIComponent(name) + '/midi/import', { path, name: label || null }),
  importMidiUpload: (name, file) => {
    const fd = new FormData();
    fd.append('file', file, file.name);
    fd.append('name', String(file.name || '').replace(/\.[^.]+$/, ''));
    return req('/api/projects/' + encodeURIComponent(name) + '/midi/import', { method: 'POST', body: fd });
  },
  fetchPeaks: (name, file, buckets) =>
    req('/api/projects/' + encodeURIComponent(name) + '/audio/peaks?file=' + encodeURIComponent(file) +
        '&buckets=' + (buckets || 900)),
  audioUrl: (name, file) =>
    '/api/projects/' + encodeURIComponent(name) + '/audio/file?file=' + encodeURIComponent(file),
  /* M-V8 E2 段 3：录音（设备枚举 / 起停 / 状态） */
  recordDevices: () =>
    req('/api/record/devices'),
  recordStart: (name, body) =>
    post('/api/projects/' + encodeURIComponent(name) + '/record/start', body || {}),
  recordStop: (name, body) =>
    post('/api/projects/' + encodeURIComponent(name) + '/record/stop', body || {}),
  recordStatus: (name) =>
    req('/api/projects/' + encodeURIComponent(name) + '/record/status'),
  /* M-V8 E3 段2：处理链（哼唱快车道）——5 工具 / 链运行 / 参数配置 / 产物 */
  chainTools: (name) =>
    req('/api/projects/' + encodeURIComponent(name) + '/chain/tools'),
  chainStatus: (name) =>
    req('/api/projects/' + encodeURIComponent(name) + '/chain/status'),
  chainRun: (name, body) =>
    post('/api/projects/' + encodeURIComponent(name) + '/chain/run', body || {}),
  chainConfig: (name, body) =>
    post('/api/projects/' + encodeURIComponent(name) + '/chain/config', body || {}),
  chainCancel: (name) =>
    post('/api/projects/' + encodeURIComponent(name) + '/chain/cancel', {}),
  chainApply: (name, body) =>
    post('/api/projects/' + encodeURIComponent(name) + '/chain/apply', body || {}),
  chainArtifactUrl: (name, file) =>
    '/api/projects/' + encodeURIComponent(name) + '/chain/artifact?file=' + encodeURIComponent(file),
  /* M-V8 E4 段1：暂存区 + 工程上下文（agents.md 双层） */
  stagingList: (name) =>
    req('/api/projects/' + encodeURIComponent(name) + '/staging'),
  stagingAdopt: (name, itemId) =>
    post('/api/projects/' + encodeURIComponent(name) + '/staging/' + encodeURIComponent(itemId) + '/adopt', {}),
  stagingDiscard: (name, itemId) =>
    post('/api/projects/' + encodeURIComponent(name) + '/staging/' + encodeURIComponent(itemId) + '/discard', {}),
  stagingArtifactUrl: (name, itemId, file) =>
    '/api/projects/' + encodeURIComponent(name) + '/staging/' + encodeURIComponent(itemId) + '/artifact?file=' + encodeURIComponent(file),
  agentsGet: (name) =>
    req('/api/projects/' + encodeURIComponent(name) + '/agents'),
  agentsSync: (name) =>
    post('/api/projects/' + encodeURIComponent(name) + '/agents/sync', {}),

  /* M-V8 E4 段2：AI 调参（事实 / 建议 / 应用 / 试听 / 丢弃；与 agent 同动作路径） */
  tuneAnalyze: (name, pack) =>
    post('/api/projects/' + encodeURIComponent(name) + '/tune/analyze', { pack: pack || null }),
  tuneSuggest: (name, body) =>
    post('/api/projects/' + encodeURIComponent(name) + '/tune/suggest', body || {}),
  tuneApply: (name, body) =>
    post('/api/projects/' + encodeURIComponent(name) + '/tune/apply', body || {}),
  tuneDiscard: (name, body) =>
    post('/api/projects/' + encodeURIComponent(name) + '/tune/discard', body || {}),
  tunePreview: (name, batchTs, id) =>
    post('/api/projects/' + encodeURIComponent(name) + '/tune/preview', { batch_ts: batchTs, id }),
  tuneList: (name) =>
    req('/api/projects/' + encodeURIComponent(name) + '/tune/list'),
  tuneGet: (name, ts) =>
    req('/api/projects/' + encodeURIComponent(name) + '/tune/' + encodeURIComponent(ts)),
  tuneFileUrl: (name, ts, file) =>
    '/api/projects/' + encodeURIComponent(name) + '/tune/' + encodeURIComponent(ts) + '/file?file=' + encodeURIComponent(file),

  /* M-V8 E4 段3：配器（packs / generate / list / get / preview / apply / discard / file） */
  arrangePacks: (name) =>
    req('/api/projects/' + encodeURIComponent(name) + '/arrange/packs'),
  arrangeGenerate: (name, body) =>
    post('/api/projects/' + encodeURIComponent(name) + '/arrange/generate', body || {}),
  arrangeList: (name) =>
    req('/api/projects/' + encodeURIComponent(name) + '/arrange/list'),
  arrangeGet: (name, ts) =>
    req('/api/projects/' + encodeURIComponent(name) + '/arrange/' + encodeURIComponent(ts)),
  arrangePreview: (name, ts) =>
    post('/api/projects/' + encodeURIComponent(name) + '/arrange/preview', { batch_ts: ts }),
  arrangeApply: (name, body) =>
    post('/api/projects/' + encodeURIComponent(name) + '/arrange/apply', body || {}),
  arrangeDiscard: (name, ts) =>
    post('/api/projects/' + encodeURIComponent(name) + '/arrange/discard', { batch_ts: ts }),
  arrangeFileUrl: (name, ts, file) =>
    '/api/projects/' + encodeURIComponent(name) + '/arrange/' + encodeURIComponent(ts) + '/file?file=' + encodeURIComponent(file),
};
