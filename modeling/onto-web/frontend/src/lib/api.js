const TOKEN_KEY = 'onto-access-token';
const REFRESH_KEY = 'onto-refresh-token';

export const authStore = {
  get token() {
    return localStorage.getItem(TOKEN_KEY) || '';
  },
  get refreshToken() {
    return localStorage.getItem(REFRESH_KEY) || '';
  },
  get username() {
    try {
      const payload = JSON.parse(atob(this.token.split('.')[1]));
      return payload.sub || '';
    } catch {
      return '';
    }
  },
  get role() {
    try {
      const payload = JSON.parse(atob(this.token.split('.')[1]));
      return payload.role || '';
    } catch {
      return '';
    }
  },
  save(tokens) {
    localStorage.setItem(TOKEN_KEY, tokens.access_token);
    if (tokens.refresh_token) localStorage.setItem(REFRESH_KEY, tokens.refresh_token);
  },
  clear() {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(REFRESH_KEY);
  },
};

function authHeaders() {
  return authStore.token ? { Authorization: `Bearer ${authStore.token}` } : {};
}

let refreshing = null;
async function tryRefresh() {
  if (!authStore.refreshToken) return false;
  refreshing =
    refreshing ||
    fetch('/api/auth/refresh', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: authStore.refreshToken }),
    })
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (data) {
          authStore.save(data);
          return true;
        }
        authStore.clear();
        return false;
      })
      .catch(() => false)
      .finally(() => {
        refreshing = null;
      });
  return refreshing;
}

async function request(path, options = {}, retried = false) {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    ...options,
    ...(options.headers
      ? { headers: { 'Content-Type': 'application/json', ...authHeaders(), ...options.headers } }
      : {}),
  });
  if (response.status === 401 && !retried && !path.startsWith('/api/auth/')) {
    if (await tryRefresh()) return request(path, options, true);
    window.dispatchEvent(new Event('onto-auth-required'));
    throw new Error('未登录或登录已过期');
  }
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
  authStatus: () => request('/api/auth/status'),
  login: async (username, password) => {
    const tokens = await request('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    });
    authStore.save(tokens);
    return tokens;
  },
  register: async (username, password) => {
    const response = await fetch('/api/auth/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    });
    let data = {};
    try {
      data = await response.json();
    } catch { /* empty body */ }
    if (!response.ok) {
      const error = new Error(data.error || `注册失败 ${response.status}`);
      error.status = response.status;
      error.data = data;
      throw error;
    }
    return data;
  },
  listProjects: () => request('/api/projects'),
  createProject: (name, template = 'blank') =>
    request('/api/projects', {
      method: 'POST',
      body: JSON.stringify({ name, template }),
    }),
  renameProject: (pid, name) =>
    request(`/api/projects/${pid}`, { method: 'PATCH', body: JSON.stringify({ name }) }),
  deleteProject: (pid) => request(`/api/projects/${pid}`, { method: 'DELETE' }),
  exportUrl: (pid, scope) =>
    `/api/projects/${pid}/export?scope=${scope}&token=${encodeURIComponent(authStore.token)}`,
  importProject: (name, file) => {
    const form = new FormData();
    form.append('name', name);
    form.append('file', file);
    return fetch('/api/projects/import', {
      method: 'POST',
      headers: authHeaders(),
      body: form,
    }).then((r) => {
      if (!r.ok) throw new Error(`导入失败 ${r.status}`);
      return r.json();
    });
  },
  fileTree: (pid) => request(`/api/projects/${pid}/files`),
  readFile: (pid, path) =>
    request(`/api/projects/${pid}/file?path=${encodeURIComponent(path)}`),
  writeFile: (pid, path, content) =>
    request(`/api/projects/${pid}/file`, {
      method: 'PUT',
      body: JSON.stringify({ path, content }),
    }),
  fileAt: (pid, rev, path) =>
    request(
      `/api/projects/${pid}/file-at?rev=${encodeURIComponent(rev)}&path=${encodeURIComponent(path)}`,
    ),
  deleteResource: (pid, path) =>
    request(`/api/projects/${pid}/resources?path=${encodeURIComponent(path)}`, {
      method: 'DELETE',
    }),
  versions: (pid) => request(`/api/projects/${pid}/versions`),
  revert: (pid, commit) =>
    request(`/api/projects/${pid}/versions/revert`, {
      method: 'POST',
      body: JSON.stringify({ commit }),
    }),
  sessionState: (pid) => request(`/api/projects/${pid}/session`),
  usage: (pid) => request(`/api/projects/${pid}/session/usage`),
  closeSession: (pid) =>
    request(`/api/projects/${pid}/session/close`, { method: 'POST' }),
  getMcp: () => request('/api/settings/mcp'),
  saveMcp: (servers) =>
    request('/api/settings/mcp', { method: 'PUT', body: JSON.stringify({ servers }) }),
  getModelSettings: () => request('/api/settings/model'),
  saveModelSettings: (payload) =>
    request('/api/settings/model', { method: 'PUT', body: JSON.stringify(payload) }),
  mcpService: () => request('/api/settings/mcp-service'),
  mcpServiceCreateToken: (name) =>
    request('/api/settings/mcp-service/tokens', {
      method: 'POST',
      body: JSON.stringify({ name }),
    }),
  mcpServiceRevokeToken: (id) =>
    request(`/api/settings/mcp-service/tokens/${id}`, { method: 'DELETE' }),
  adminOverview: () => request('/api/admin/overview'),
  studioGraph: (pid) => request(`/api/projects/${pid}/studio/graph`),
  studioElement: (pid, path) =>
    request(`/api/projects/${pid}/studio/element?path=${encodeURIComponent(path)}`),
  sendMessage: (pid, text) =>
    request(`/api/projects/${pid}/session/messages`, {
      method: 'POST',
      body: JSON.stringify({ text }),
    }),
  respondPermission: (pid, requestId, behavior, message = '', answers = null) =>
    request(`/api/projects/${pid}/session/permissions/${requestId}`, {
      method: 'POST',
      body: JSON.stringify({ behavior, message, answers }),
    }),
  interrupt: (pid) =>
    request(`/api/projects/${pid}/session/interrupt`, { method: 'POST' }),
  uploadResource: (pid, subdir, file) => {
    const form = new FormData();
    form.append('subdir', subdir);
    form.append('file', file);
    return fetch(`/api/projects/${pid}/resources`, {
      method: 'POST',
      headers: authHeaders(),
      body: form,
    }).then((r) => {
      if (!r.ok) throw new Error(`上传失败 ${r.status}`);
      return r.json();
    });
  },
};

export function openEventStream(pid, onEvent, after = 0) {
  const token = encodeURIComponent(authStore.token);
  const source = new EventSource(
    `/api/projects/${pid}/session/stream?after=${after}&token=${token}`,
  );
  source.onmessage = (msg) => {
    try {
      onEvent(JSON.parse(msg.data));
    } catch { /* keepalive 等非 JSON 行忽略 */ }
  };
  return source;
}
