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
export const deleteDocument = (id, docId) => api(`/api/projects/${encodeURIComponent(id)}/documents/${encodeURIComponent(docId)}`, { method: 'DELETE' });
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

// ---- M3 评测 ----
export const listTestcases = (id) => api(`${P(id)}/testcases`);
export const addTestcase = (id, question, groundTruth) => api(`${P(id)}/testcases`, { method: 'POST', body: JSON.stringify({ question, groundTruth }) });
export const prefillTestcases = (id) => api(`${P(id)}/testcases/prefill`, { method: 'POST', body: '{}' });
export const updateTestcase = (id, tcId, patch) => api(`${P(id)}/testcases/${encodeURIComponent(tcId)}`, { method: 'PATCH', body: JSON.stringify(patch) });
export const deleteTestcase = (id, tcId) => api(`${P(id)}/testcases/${encodeURIComponent(tcId)}`, { method: 'DELETE' });
export const startEvaluate = (id, binding) => api(`${P(id)}/evaluate`, { method: 'POST', body: JSON.stringify(binding ? { binding } : {}) });
export const listRounds = (id) => api(`${P(id)}/rounds`);
export const getRound = (id, roundId) => api(`${P(id)}/rounds/${encodeURIComponent(roundId)}`);
export const overrideVerdict = (id, resultId, verdict) => api(`${P(id)}/results/${encodeURIComponent(resultId)}`, { method: 'PATCH', body: JSON.stringify({ verdict }) });

// ---- M4 优化与版本 ----
export const proposeOptimize = (id, roundId) => api(`${P(id)}/optimize/propose`, { method: 'POST', body: JSON.stringify(roundId ? { roundId } : {}) });
export const listSuggestions = (id, roundId) => api(`${P(id)}/optimize/suggestions${roundId ? `?roundId=${encodeURIComponent(roundId)}` : ''}`);
export const applyOptimize = (id, suggestionIds) => api(`${P(id)}/optimize/apply`, { method: 'POST', body: JSON.stringify({ suggestionIds }) });
export const rejectSuggestions = (id, suggestionIds) => api(`${P(id)}/optimize/reject`, { method: 'POST', body: JSON.stringify({ suggestionIds }) });
export const listVersions = (id) => api(`${P(id)}/versions`);
export const exportVersionUrl = (id, version) => `${P(id)}/package/export${version ? `?version=${encodeURIComponent(version)}` : ''}`;

// ---- 包完善（人工修订 + 修缮对话）----
export const savePackageFile = (id, path, content) => api(`${P(id)}/package/file`, { method: 'PUT', body: JSON.stringify({ path, content }) });
export const resetPackageFile = (id, path) => api(`${P(id)}/package/file?path=${encodeURIComponent(path)}`, { method: 'DELETE' });
export const packageChat = (id, message, history) => api(`${P(id)}/package/chat`, { method: 'POST', body: JSON.stringify({ message, history }) });

// ---- 配置中心 ----
export const getSettings = () => api('/api/settings');
export const saveSettings = (patch) => api('/api/settings', { method: 'PUT', body: JSON.stringify(patch) });
export const testLlm = (cfg) => api('/api/settings/llm/test', { method: 'POST', body: JSON.stringify(cfg || {}) });
export const testConsumer = (url) => api('/api/settings/consumer/test', { method: 'POST', body: JSON.stringify({ url }) });
