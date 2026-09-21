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
    const error = new Error(detail);
    error.status = response.status;
    throw error;
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
  createProject: (name, template = 'blank', background = '') =>
    request('/api/projects', {
      method: 'POST',
      body: JSON.stringify({ name, template, background }),
    }),
  transferImport: (file) => {
    const form = new FormData();
    form.append('file', file);
    // request() 会强加 JSON Content-Type，FormData 走裸 fetch
    return fetch('/api/projects/transfer-import', {
      method: 'POST',
      headers: authHeaders(),
      body: form,
    }).then(async (r) => {
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.error || `导入失败 ${r.status}`);
      return data;
    });
  },
  renameProject: (pid, name) =>
    request(`/api/projects/${pid}`, { method: 'PATCH', body: JSON.stringify({ name }) }),
  deleteProject: (pid) => request(`/api/projects/${pid}`, { method: 'DELETE' }),
  exportUrl: (pid, scope) =>
    `/api/projects/${pid}/export?scope=${scope}&token=${encodeURIComponent(authStore.token)}`,
  importProject: (name, file, dryRun = false) => {
    const form = new FormData();
    form.append('name', name);
    form.append('file', file);
    if (dryRun) form.append('dry_run', 'true');
    return fetch('/api/projects/import', {
      method: 'POST',
      headers: authHeaders(),
      body: form,
    }).then(async (r) => {
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.error || `导入失败 ${r.status}`);
      return data;
    });
  },
  archiveProject: (pid, archived) =>
    request(`/api/projects/${pid}/archive`, {
      method: 'POST',
      body: JSON.stringify({ archived }),
    }),
  cloneProject: (pid) => request(`/api/projects/${pid}/clone`, { method: 'POST' }),
  getMembers: (pid) => request(`/api/projects/${pid}/members`),
  saveMembers: (pid, members) =>
    request(`/api/projects/${pid}/members`, {
      method: 'PUT',
      body: JSON.stringify({ members }),
    }),
  excelPreview: (pid, path) =>
    request(`/api/projects/${pid}/excel?path=${encodeURIComponent(path)}`),
  tagVersion: (pid, commit, label) =>
    request(`/api/projects/${pid}/versions/tag`, {
      method: 'POST',
      body: JSON.stringify({ commit, label }),
    }),
  deleteTag: (pid, name) =>
    request(`/api/projects/${pid}/versions/tag/${encodeURIComponent(name)}`, {
      method: 'DELETE',
    }),
  changePassword: (username, oldPassword, newPassword) =>
    request('/api/auth/password', {
      method: 'POST',
      body: JSON.stringify({
        username,
        old_password: oldPassword,
        new_password: newPassword,
      }),
    }),
  mcpTest: (server) =>
    request('/api/settings/mcp/test', {
      method: 'POST',
      body: JSON.stringify({ server }),
    }),
  getProjectMcp: (pid) => request(`/api/projects/${pid}/mcp`),
  saveProjectMcp: (pid, disabled) =>
    request(`/api/projects/${pid}/mcp`, {
      method: 'PUT',
      body: JSON.stringify({ disabled }),
    }),
  assetBackups: () => request('/api/settings/assets/backups'),
  assetRollback: (name) =>
    request('/api/settings/assets/rollback', {
      method: 'POST',
      body: JSON.stringify({ name }),
    }),
  adminHealth: () => request('/api/admin/health'),
  adminRecycleSession: (pid) =>
    request(`/api/admin/sessions/${pid}/recycle`, { method: 'POST' }),
  adminTrash: () => request('/api/admin/trash'),
  adminRestoreTrash: (name) =>
    request('/api/admin/trash/restore', { method: 'POST', body: JSON.stringify({ name }) }),
  studioLease: (pid, path) =>
    request(`/api/projects/${pid}/studio/lease`, {
      method: 'POST',
      body: JSON.stringify({ path }),
    }),
  studioReleaseLease: (pid, path) =>
    request(`/api/projects/${pid}/studio/lease?path=${encodeURIComponent(path)}`, {
      method: 'DELETE',
    }),
  studioSaveDraft: (pid, path, content, baseRevision) =>
    request(`/api/projects/${pid}/studio/draft`, {
      method: 'PUT',
      body: JSON.stringify({ path, content, base_revision: baseRevision }),
    }),
  studioGetDraft: (pid, path) =>
    request(`/api/projects/${pid}/studio/draft?path=${encodeURIComponent(path)}`),
  studioDiscardDraft: (pid, path) =>
    request(`/api/projects/${pid}/studio/draft?path=${encodeURIComponent(path)}`, {
      method: 'DELETE',
    }),
  studioSubmit: (pid, path) =>
    request(`/api/projects/${pid}/studio/submit`, {
      method: 'POST',
      body: JSON.stringify({ path }),
    }),
  reportItems: (pid, path) =>
    request(`/api/projects/${pid}/report-items?path=${encodeURIComponent(path)}`),
  reverseQuery: (pid, kind, term = '') =>
    request(
      `/api/projects/${pid}/query?kind=${kind}&term=${encodeURIComponent(term)}`,
    ),
  listKnowledge: (pid) => request(`/api/projects/${pid}/knowledge`),
  createKnowledge: (pid, name, content) =>
    request(`/api/projects/${pid}/knowledge`, {
      method: 'POST',
      body: JSON.stringify({ name, content }),
    }),
  deleteKnowledge: (pid, path) =>
    request(`/api/projects/${pid}/knowledge?path=${encodeURIComponent(path)}`, {
      method: 'DELETE',
    }),
  fileTree: (pid) => request(`/api/projects/${pid}/files`),
  readFile: (pid, path) =>
    request(`/api/projects/${pid}/file?path=${encodeURIComponent(path)}`),
  writeFile: (pid, path, content, baseRevision = '') =>
    request(`/api/projects/${pid}/file`, {
      method: 'PUT',
      body: JSON.stringify({ path, content, base_revision: baseRevision }),
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
  saveJiuwenModelSettings: (payload) =>
    request('/api/settings/model/jiuwen', { method: 'PUT', body: JSON.stringify(payload) }),
  getProjectEngine: (pid) => request(`/api/projects/${pid}/engine`),
  evalDataAlignment: (pid) => request(`/api/projects/${pid}/eval-data/alignment`),
  evalStatus: (pid) => request(`/api/projects/${pid}/eval/status`),
  evalCases: (pid) => request(`/api/projects/${pid}/eval/cases`),
  evalRun: (pid, cases) =>
    request(`/api/projects/${pid}/eval/run`, {
      method: 'POST',
      body: JSON.stringify({ cases }),
    }),
  setProjectEngine: (pid, engine) =>
    request(`/api/projects/${pid}/engine`, {
      method: 'PUT',
      body: JSON.stringify({ engine }),
    }),
  mcpService: () => request('/api/settings/mcp-service'),
  mcpServiceCreateToken: (name) =>
    request('/api/settings/mcp-service/tokens', {
      method: 'POST',
      body: JSON.stringify({ name }),
    }),
  mcpServiceRevokeToken: (id) =>
    request(`/api/settings/mcp-service/tokens/${id}`, { method: 'DELETE' }),
  metrics: (pid) => request(`/api/projects/${pid}/metrics`),
  search: (pid, q) =>
    request(`/api/projects/${pid}/search?q=${encodeURIComponent(q)}`),
  rawUrl: (pid, path) =>
    `/api/projects/${pid}/raw?path=${encodeURIComponent(path)}&token=${encodeURIComponent(authStore.token)}`,
  getRuntime: () => request('/api/settings/runtime'),
  saveRuntime: (permission_mode) =>
    request('/api/settings/runtime', {
      method: 'PUT',
      body: JSON.stringify({ permission_mode }),
    }),
  getAssets: () => request('/api/settings/assets'),
  uploadAssets: (file) => {
    const form = new FormData();
    form.append('file', file);
    return fetch('/api/settings/assets/upload', {
      method: 'POST',
      headers: authHeaders(),
      body: form,
    }).then(async (r) => {
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.error || `上传失败 ${r.status}`);
      return data;
    });
  },
  adminOverview: () => request('/api/admin/overview'),
  adminAudit: () => request('/api/admin/audit'),
  adminBackup: () => request('/api/admin/backup', { method: 'POST' }),
  adminUsers: () => request('/api/admin/users'),
  adminUpdateUser: (username, payload) =>
    request(`/api/admin/users/${encodeURIComponent(username)}`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),
  adminDeleteUser: (username) =>
    request(`/api/admin/users/${encodeURIComponent(username)}`, { method: 'DELETE' }),
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
  uploadResource: (pid, subdir, file, onProgress, purpose = 'auto', readingMode = 'on_demand') => {
    const form = new FormData();
    form.append('subdir', subdir);
    form.append('file', file);
    form.append('purpose', purpose);
    form.append('reading_mode', readingMode);
    return xhrUpload(`/api/projects/${pid}/resources`, form, onProgress);
  },
  resourceDirectives: (pid) => request(`/api/projects/${pid}/resource-meta`),
  setResourceDirective: (pid, path, purpose, readingMode = 'on_demand') =>
    request(`/api/projects/${pid}/resource-meta`, {
      method: 'PATCH',
      body: JSON.stringify({ path, purpose, reading_mode: readingMode }),
    }),
};

// fetch 拿不到上传进度，附件上传走 XHR；onProgress 收 0~1 的比例
function xhrUpload(path, form, onProgress, retried = false) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', path);
    if (authStore.token) {
      xhr.setRequestHeader('Authorization', `Bearer ${authStore.token}`);
    }
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress?.(e.loaded / e.total);
    };
    xhr.onload = async () => {
      if (xhr.status === 401 && !retried) {
        if (await tryRefresh()) {
          return resolve(xhrUpload(path, form, onProgress, true));
        }
        window.dispatchEvent(new Event('onto-auth-required'));
        return reject(new Error('未登录或登录已过期'));
      }
      let data = {};
      try {
        data = JSON.parse(xhr.responseText);
      } catch { /* 非 JSON 响应按状态码处理 */ }
      if (xhr.status >= 200 && xhr.status < 300) {
        onProgress?.(1);
        resolve(data);
      } else {
        reject(new Error(data.error || data.detail || `上传失败 ${xhr.status}`));
      }
    };
    xhr.onerror = () => reject(new Error('网络异常，上传中断'));
    xhr.send(form);
  });
}

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
