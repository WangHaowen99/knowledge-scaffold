// 后端 API 客户端：token 存 localStorage，统一注入 Bearer。
const TOKEN_KEY = 'mw_modeling_token';

export const getToken = () => { try { return localStorage.getItem(TOKEN_KEY) || ''; } catch { return ''; } };
export const setToken = (t) => { try { t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY); } catch { /* ignore */ } };

export async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  if (options.body && !(options.body instanceof FormData)) headers['Content-Type'] = 'application/json';
  const res = await fetch(path, { ...options, headers });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data.error || `请求失败（${res.status}）`), { status: res.status });
  return data;
}

export const login = (username, password) =>
  api('/auth/login', { method: 'POST', body: JSON.stringify({ username, password }) });
export const me = () => api('/api/me');
export const listProjects = () => api('/api/projects');
export const createProject = (input) => api('/api/projects', { method: 'POST', body: JSON.stringify(input) });
export const getProject = (id) => api(`/api/projects/${encodeURIComponent(id)}`);
export const uploadDocument = (id, file) => {
  const form = new FormData();
  form.append('file', file);
  return api(`/api/projects/${encodeURIComponent(id)}/documents`, { method: 'POST', body: form });
};
export const runAnalyze = (id) => api(`/api/projects/${encodeURIComponent(id)}/analyze`, { method: 'POST' });

// ---- M2 ----
const P = (id) => `/api/projects/${encodeURIComponent(id)}`;
export const getInterview = (id) => api(`${P(id)}/interview`);
export const startInterview = (id) => api(`${P(id)}/interview/start`, { method: 'POST' });
export const answerInterview = (id, batchId, answers) => api(`${P(id)}/interview/answer`, { method: 'POST', body: JSON.stringify({ batchId, answers }) });
export const getCompetency = (id) => api(`${P(id)}/competency`);
export const proposeCompetency = (id) => api(`${P(id)}/competency/propose`, { method: 'POST' });
export const selectCompetency = (id, selectedIds) => api(`${P(id)}/competency/select`, { method: 'POST', body: JSON.stringify({ selectedIds }) });
export const generatePackage = (id) => api(`${P(id)}/generate`, { method: 'POST' });
export const getPackage = (id) => api(`${P(id)}/package`);
export const getPackageFile = (id, path) => api(`${P(id)}/package/file?path=${encodeURIComponent(path)}`);
export const exportPackageUrl = (id) => `${P(id)}/package/export`;
