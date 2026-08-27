/* api.js —— REST 封装（docs/05 §三 全部接口；模块间只经本文件访问后端） */

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
  listProjects: () => req('/api/projects'),
  createProject: (name, score) => post('/api/projects', { name, score: score || null }),

  getState: (name) => req('/api/projects/' + encodeURIComponent(name) + '/state'),
  postBatch: (name, label, commands, commitMessage) =>
    post('/api/projects/' + encodeURIComponent(name) + '/batch',
      { label: label || '', commands, commit_message: commitMessage || null }),
  undo: (name) => post('/api/projects/' + encodeURIComponent(name) + '/undo'),
  redo: (name) => post('/api/projects/' + encodeURIComponent(name) + '/redo'),
  rollback: (name, rev) =>
    post('/api/projects/' + encodeURIComponent(name) + '/rollback', { rev: rev || 'HEAD~1' }),
  getLog: (name) => req('/api/projects/' + encodeURIComponent(name) + '/log'),
  getSummary: (name) => req('/api/projects/' + encodeURIComponent(name) + '/summary'),
  render: (name, out) => post('/api/projects/' + encodeURIComponent(name) + '/render', { out: out || null }),
  play: (name) => post('/api/projects/' + encodeURIComponent(name) + '/play'),
  chat: (project, message) => post('/api/chat', { project, message }),

  wavUrl: (name) => '/api/projects/' + encodeURIComponent(name) + '/wav',
  eventsUrl: (name) => '/api/projects/' + encodeURIComponent(name) + '/events',
};
