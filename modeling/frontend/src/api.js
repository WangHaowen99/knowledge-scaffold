// 后端 API 客户端：token 存 localStorage，统一注入 Bearer；有 refresh token 时 401 静默续期。
const TOKEN_KEY = 'mw_modeling_token';
const REFRESH_KEY = 'mw_modeling_refresh_token';

export const getToken = () => { try { return localStorage.getItem(TOKEN_KEY) || ''; } catch { return ''; } };
export const setToken = (t) => { try { t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY); } catch { /* ignore */ } };
export const getRefreshToken = () => { try { return localStorage.getItem(REFRESH_KEY) || ''; } catch { return ''; } };

export const setSession = (data) => {
  setToken(data.token);
  try { data.refresh_token ? localStorage.setItem(REFRESH_KEY, data.refresh_token) : localStorage.removeItem(REFRESH_KEY); } catch { /* ignore */ }
};
export const clearSession = () => {
  setToken('');
  try { localStorage.removeItem(REFRESH_KEY); } catch { /* ignore */ }
};

const doFetch = (path, options = {}) => {
  const headers = { ...(options.headers || {}) };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  if (options.body && !(options.body instanceof FormData)) headers['Content-Type'] = 'application/json';
  return fetch(path, { ...options, headers });
};

// 静默续期并发去重：同一时刻只发一个 refresh 请求，其余 401 请求共用其结果
let refreshInFlight = null;

const refreshAuthToken = () => {
  if (!refreshInFlight) {
    refreshInFlight = (async () => {
      const res = await fetch('/auth/refresh', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: getRefreshToken() }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.token) throw Object.assign(new Error('刷新登录令牌失败'), { status: res.status });
      setSession(data);
    })().finally(() => { refreshInFlight = null; });
  }
  return refreshInFlight;
};

// 认证接口自身不参与静默续期（防递归）
const REFRESH_EXEMPT = ['/auth/refresh', '/auth/login', '/auth/register'];

export async function authFetch(path, options = {}) {
  const res = await doFetch(path, options);
  if (res.status !== 401 || !getRefreshToken() || REFRESH_EXEMPT.some((p) => path.startsWith(p))) return res;
  try {
    await refreshAuthToken();
  } catch (e) {
    // 400/401=上游明确拒绝（过期/吊销），清凭据走现有登出路径；
    // 503/网络错=认证服务暂不可用，保留凭据待下次重试。原 401 均交还调用方
    if (e.status === 400 || e.status === 401) clearSession();
    return res;
  }
  return doFetch(path, options);
}

export async function api(path, options = {}) {
  const res = await authFetch(path, options);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data.error || `请求失败（${res.status}）`), { status: res.status, data });
  return data;
}

export const login = (username, password) =>
  api('/auth/login', { method: 'POST', body: JSON.stringify({ username, password }) });
export const register = (username, email, password) =>
  api('/auth/register', { method: 'POST', body: JSON.stringify({ username, email, password }) });
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
export const getDataSource = (id) => api(`${P(id)}/data-source`);
export const saveDataSource = (id, input) => api(`${P(id)}/data-source`, { method: 'PUT', body: JSON.stringify(input) });
export const listTestcases = (id) => api(`${P(id)}/testcases`);
export const addTestcase = (id, question, groundTruth) => api(`${P(id)}/testcases`, { method: 'POST', body: JSON.stringify({ question, groundTruth }) });
export const prefillTestcases = (id) => api(`${P(id)}/testcases/prefill`, { method: 'POST', body: '{}' });
export const updateTestcase = (id, tcId, patch) => api(`${P(id)}/testcases/${encodeURIComponent(tcId)}`, { method: 'PATCH', body: JSON.stringify(patch) });
export const deleteTestcase = (id, tcId) => api(`${P(id)}/testcases/${encodeURIComponent(tcId)}`, { method: 'DELETE' });
export const startEvaluate = (id) => api(`${P(id)}/evaluate`, { method: 'POST', body: '{}' });
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
export const packageChatApply = (id, message, history, plan) => api(`${P(id)}/package/chat/apply`, { method: 'POST', body: JSON.stringify({ message, history, plan }) });
export const runPackageCheck = (id) => api(`${P(id)}/package/check`, { method: 'POST', body: '{}' });
export const getPackageCheck = (id) => api(`${P(id)}/package/check`);

// ---- 配置中心 ----
export const getSettings = () => api('/api/settings');
export const saveSettings = (patch) => api('/api/settings', { method: 'PUT', body: JSON.stringify(patch) });
export const testLlm = (cfg) => api('/api/settings/llm/test', { method: 'POST', body: JSON.stringify(cfg || {}) });
export const testConsumer = (url) => api('/api/settings/consumer/test', { method: 'POST', body: JSON.stringify({ url }) });
