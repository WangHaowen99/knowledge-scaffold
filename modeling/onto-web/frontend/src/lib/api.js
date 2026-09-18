async function request(path, options = {}) {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!response.ok) {
    let detail = `${response.status}`;
    try {
      const body = await response.json();
      detail = body.error || body.detail || detail;
    } catch { /* keep status text */ }
    throw new Error(detail);
  }
  return response.json();
}

export const api = {
  health: () => request('/api/health'),
  listProjects: () => request('/api/projects'),
  createProject: (name) =>
    request('/api/projects', { method: 'POST', body: JSON.stringify({ name }) }),
  fileTree: (pid) => request(`/api/projects/${pid}/files`),
  readFile: (pid, path) =>
    request(`/api/projects/${pid}/file?path=${encodeURIComponent(path)}`),
  versions: (pid) => request(`/api/projects/${pid}/versions`),
  sessionState: (pid) => request(`/api/projects/${pid}/session`),
  sendMessage: (pid, text) =>
    request(`/api/projects/${pid}/session/messages`, {
      method: 'POST',
      body: JSON.stringify({ text }),
    }),
  respondPermission: (pid, requestId, behavior, message = '') =>
    request(`/api/projects/${pid}/session/permissions/${requestId}`, {
      method: 'POST',
      body: JSON.stringify({ behavior, message }),
    }),
  interrupt: (pid) =>
    request(`/api/projects/${pid}/session/interrupt`, { method: 'POST' }),
  uploadResource: (pid, subdir, file) => {
    const form = new FormData();
    form.append('subdir', subdir);
    form.append('file', file);
    return fetch(`/api/projects/${pid}/resources`, { method: 'POST', body: form }).then(
      (r) => {
        if (!r.ok) throw new Error(`上传失败 ${r.status}`);
        return r.json();
      },
    );
  },
};

export function openEventStream(pid, onEvent, after = 0) {
  const source = new EventSource(`/api/projects/${pid}/session/stream?after=${after}`);
  source.onmessage = (msg) => {
    try {
      onEvent(JSON.parse(msg.data));
    } catch { /* keepalive 等非 JSON 行忽略 */ }
  };
  return source;
}
