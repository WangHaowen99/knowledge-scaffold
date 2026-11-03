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
  const access = authStore.token;
  const refresh = authStore.refreshToken;
  const update = async () => {
    if (authStore.token !== access) return false;
    try {
      const response = await fetch('/api/auth/refresh', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: refresh }),
      });
      if (authStore.token !== access || authStore.refreshToken !== refresh) return false;
      if (!response.ok) { authStore.clear(); return false; }
      const data = await response.json();
      if (authStore.token !== access || authStore.refreshToken !== refresh) return false;
      authStore.save(data);
      return true;
    } catch { return false; }
  };
  refreshing = refreshing || (globalThis.navigator?.locks
    ? navigator.locks.request('onto-auth-refresh', update) : update())
    .finally(() => { refreshing = null; });
  return refreshing;
}

export async function request(path, options = {}, retried = false) {
  // FormData 由浏览器自带 multipart 边界，强制 json Content-Type 会让后端无法解析
  const baseHeaders =
    options.body instanceof FormData
      ? authHeaders()
      : { 'Content-Type': 'application/json', ...authHeaders() };
  const response = await fetch(path, {
    ...options,
    headers: { ...baseHeaders, ...(options.headers || {}) },
  });
  if (response.status === 401 && !retried && !path.startsWith('/api/auth/')) {
    if (await tryRefresh()) return request(path, options, true);
    window.dispatchEvent(new Event('onto-auth-required'));
    throw new Error('未登录或登录已过期');
  }
  if (!response.ok) throw await responseError(response);
  return response.json();
}

// 错误体里的 code 与其余字段随错误带出：调用方据此分流（如数据绑定阻断 → 修复面板）
async function responseError(response) {
  let detail = `${response.status}`;
  let body = {};
  try {
    body = await response.json();
    detail = body.error || body.message || body.detail || detail;
    // FastAPI 校验错误是对象数组，转成可读文本（否则 toast 渲染为空，表现为"无反应"）
    if (typeof detail !== 'string') detail = JSON.stringify(detail).slice(0, 200);
  } catch { /* keep status text */ }
  const error = new Error(detail);
  error.status = response.status;
  error.code = body.code || '';
  error.data = body;
  return error;
}

// 评测轮在 modeling 侧后台执行，常超 access token 有效期：带上 refresh token，
// 由 modeling 为本轮续期凭据。未登录（无鉴权部署）时不带该字段。
export function evalRunPayload(
  {
    cases,
    dataSource,
    dataSourceId = '',
    evalSetId = '',
    allowUnbound = false,
    modelProfileId = '',
    caseConcurrency,
  },
  refreshToken = authStore.refreshToken,
) {
  const payload = {
    cases,
    data_source: dataSource || { mode: 'none' },
    data_source_id: dataSourceId,
    eval_set_id: evalSetId,
    allow_unbound: allowUnbound,
  };
  if (modelProfileId) payload.model_profile_id = modelProfileId;
  if (Number.isSafeInteger(caseConcurrency) && caseConcurrency >= 1) {
    payload.case_concurrency = caseConcurrency;
  }
  if (refreshToken) payload.refresh_token = refreshToken;
  return payload;
}

function conversationQuery(conversationId) {
  return conversationId ? `?conversation=${encodeURIComponent(conversationId)}` : '';
}

function filenameOf(response, fallback) {
  const header = response.headers.get('content-disposition') || '';
  const encoded = header.match(/filename\*=UTF-8''([^;]+)/i);
  if (encoded) return decodeURIComponent(encoded[1]);
  const plain = header.match(/filename="?([^";]+)"?/i);
  return plain ? plain[1] : fallback;
}

function createRequestId() {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), (byte) => byte.toString(16).padStart(2, '0')).join('');
}

async function stableHubRequest(path, body) {
  const key = `onto-hub:${authStore.username}:${path}:${JSON.stringify(body)}`;
  let requestId = localStorage.getItem(key);
  if (!requestId) {
    requestId = createRequestId();
    localStorage.setItem(key, requestId);
  }
  try {
    const result = await request(path, { method: 'POST', body: JSON.stringify({ ...body, request_id: requestId }) });
    localStorage.removeItem(key);
    return result;
  } catch (error) {
    if (error.status === 410) localStorage.removeItem(key);
    throw error;
  }
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
  // projectName 为空时沿用迁移包里的任务名；同名冲突（409）改名后带上新名称重新提交
  transferImport: (file, projectName = '') => {
    const form = new FormData();
    form.append('file', file);
    if (projectName) form.append('project_name', projectName);
    return request('/api/projects/transfer-import', { method: 'POST', body: form });
  },
  renameProject: (pid, name) =>
    request(`/api/projects/${pid}`, { method: 'PATCH', body: JSON.stringify({ name }) }),
  // 开始建模前的目标确认卡：background 省略 = 确认原目标；给出 = 提交改写后的新目标
  confirmProjectGoal: (pid, background) =>
    request(`/api/projects/${pid}/goal`, {
      method: 'PUT',
      body: JSON.stringify(background === undefined ? {} : { background }),
    }),
  // 显式确认体：request() 一律带 application/json，空体在严格服务端/网关会被判为非法 JSON；
  // 同时与 modeling 侧删除接口的 confirmed 契约保持一致
  deleteProject: (pid) =>
    request(`/api/projects/${pid}`, {
      method: 'DELETE',
      body: JSON.stringify({ confirmed: true }),
    }),
  exportUrl: (pid, scope) =>
    `/api/projects/${pid}/export?scope=${scope}&token=${encodeURIComponent(authStore.token)}`,
  // 发布前体检：阻断项、无从核对项与修复所需的表清单、文档版本号
  packageCheck: (pid) => request(`/api/projects/${pid}/package/check`),
  // 求解模型面板：读取当前修订下的全部契约；保存为受管目录整体替换，修订不一致 409
  solverContract: (pid) => request(`/api/projects/${pid}/package/solver-contract`),
  saveSolverContract: (pid, operations, baseRevision = '', summary = '') =>
    request(`/api/projects/${pid}/package/solver-contract`, {
      method: 'PUT',
      body: JSON.stringify({ baseRevision, contract: { operations }, summary }),
    }),
  // 即时检查未保存的草稿（不落盘）：{ rejected: 保存会被拒的问题, findings: 发布会被拦截的问题 }
  checkSolverContract: (pid, operations, { signal } = {}) =>
    request(`/api/projects/${pid}/package/solver-contract/check`, {
      method: 'POST',
      body: JSON.stringify({ contract: { operations } }),
      signal,
    }),
  // 求解端到端验证：用例存于工作区状态（不进知识包）；发起后由后台经 modeling → Consumer 执行，
  // 按 latest 轮询当前修订的记录。revision 不一致 409，allowUnbound 即数据绑定降级
  solverValidationCases: (pid) => request(`/api/projects/${pid}/package/solver-validations/cases`),
  saveSolverValidationCases: (pid, cases) =>
    request(`/api/projects/${pid}/package/solver-validations/cases`, {
      method: 'PUT',
      body: JSON.stringify({ cases }),
    }),
  startSolverValidation: (pid, { revision, caseIds = [], dataSourceId = '', allowUnbound = false }) =>
    request(`/api/projects/${pid}/package/solver-validations`, {
      method: 'POST',
      body: JSON.stringify({
        revision,
        case_ids: caseIds,
        data_source_id: dataSourceId,
        allow_unbound: allowUnbound,
      }),
    }),
  solverValidationLatest: (pid) => request(`/api/projects/${pid}/package/solver-validations/latest`),
  // 修复入口：对象「# 数据来源」改成现有表；table 为空表示不落库（「无」）
  setObjectDataSource: (pid, key, table, baseRevision = '') =>
    request(`/api/projects/${pid}/objects/${encodeURIComponent(key)}/data-source`, {
      method: 'POST',
      body: JSON.stringify({ table, base_revision: baseRevision }),
    }),
  packageReleases: (pid, { offset = 0, limit = 20 } = {}) =>
    request(`/api/projects/${pid}/package/releases?offset=${offset}&limit=${limit}`),
  packageResourcesStatus: (pid) => request(`/api/projects/${pid}/package/resources/status`),
  // 独立评测的版本状态与产物：reconcile 时由后端向评测服务对账，否则只读通知索引
  packageEvaluations: (pid, { reconcile = false } = {}) =>
    request(`/api/projects/${pid}/package/evaluations?reconcile=${reconcile ? 1 : 0}`),
  packageEvaluationReport: (pid, runId) =>
    request(`/api/projects/${pid}/package/evaluations/${encodeURIComponent(runId)}/report`),
  packageEvaluationSuggestions: (pid, runId) =>
    request(`/api/projects/${pid}/package/evaluations/${encodeURIComponent(runId)}/suggestions`),
  regeneratePackageEvaluation: (pid, runId) =>
    request(`/api/projects/${pid}/package/evaluations/${encodeURIComponent(runId)}/regenerate`, { method: 'POST', body: '{}' }),
  optimizePackageEvaluation: (pid, runId) =>
    request(`/api/projects/${pid}/package/evaluations/${encodeURIComponent(runId)}/optimization`, { method: 'POST', body: '{}' }),
  downloadRelease: async (pid, releaseId) => {
    const send = () => fetch(`/api/projects/${pid}/package/releases/${releaseId}/download`, { headers: authHeaders() });
    let response = await send();
    if (response.status === 401 && (await tryRefresh())) response = await send();
    if (!response.ok) throw await responseError(response);
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement('a');
    link.href = url;
    link.download = filenameOf(response, 'package.zip');
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  },
  publishPackage: async (pid, { allowUnbound = false } = {}) => {
    const key = `onto-publish:${authStore.username || 'local-dev'}:${pid}:${allowUnbound}`;
    let requestId = localStorage.getItem(key);
    if (!requestId) {
      requestId = createRequestId();
      localStorage.setItem(key, requestId);
    }
    let receipt;
    try {
      receipt = await request(`/api/projects/${pid}/package/releases`, {
        method: 'POST', body: JSON.stringify({ request_id: requestId, allow_unbound: allowUnbound }),
      });
    } catch (error) {
      // Validation precedes reservation; a corrected workspace starts a new request.
      if (error.code === 'binding_problems' || error.status === 410) localStorage.removeItem(key);
      throw error;
    }
    localStorage.removeItem(key);
    return receipt;
  },
  hubPackages: (offset = 0) => request(`/api/hub/packages?offset=${offset}&limit=20`),
  hubPackageVersions: (packageId, offset = 0) =>
    request(`/api/hub/packages/${encodeURIComponent(packageId)}/releases?offset=${offset}&limit=20`),
  importHubPackage: (body) => stableHubRequest('/api/hub/imports', body),
  importProject: (name, file, dryRun = false) => {
    const form = new FormData();
    form.append('name', name);
    form.append('file', file);
    if (dryRun) form.append('dry_run', 'true');
    return request('/api/projects/import', { method: 'POST', body: form });
  },
  archiveProject: (pid, archived) =>
    request(`/api/projects/${pid}/archive`, {
      method: 'POST',
      body: JSON.stringify({ archived }),
    }),
  // name 为空时用默认的「原名-副本」；同名冲突（409）改名后带上新名称重新提交
  cloneProject: (pid, name = '') =>
    request(`/api/projects/${pid}/clone`, { method: 'POST', body: JSON.stringify({ name }) }),
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
  // 历史时间线：迭代与发布版本由后端合并分页，cursor 原样回传
  history: (pid, { limit = 50, cursor = '' } = {}) =>
    request(`/api/projects/${pid}/history?${new URLSearchParams({ limit: String(limit), cursor })}`),
  revert: (pid, commit) =>
    request(`/api/projects/${pid}/versions/revert`, {
      method: 'POST',
      body: JSON.stringify({ commit }),
    }),
  // conversationId 省略 = 引擎当前绑定的会话，没有在线引擎则取最近更新的一条
  sessionState: (pid, conversationId) =>
    request(`/api/projects/${pid}/session${conversationQuery(conversationId)}`),
  usage: (pid) => request(`/api/projects/${pid}/session/usage`),
  closeSession: (pid, conversationId) =>
    request(`/api/projects/${pid}/session/close${conversationQuery(conversationId)}`, {
      method: 'POST',
    }),
  listConversations: (pid) => request(`/api/projects/${pid}/session/conversations`),
  createConversation: (pid) =>
    request(`/api/projects/${pid}/session/conversations`, { method: 'POST' }),
  renameConversation: (pid, conversationId, title) =>
    request(`/api/projects/${pid}/session/conversations/${conversationId}`, {
      method: 'PATCH',
      body: JSON.stringify({ title }),
    }),
  deleteConversation: (pid, conversationId) =>
    request(`/api/projects/${pid}/session/conversations/${conversationId}`, {
      method: 'DELETE',
    }),
  getMcp: () => request('/api/settings/mcp'),
  saveMcp: (servers) =>
    request('/api/settings/mcp', { method: 'PUT', body: JSON.stringify({ servers }) }),
  getModelSettings: () => request('/api/settings/model'),
  saveModelSettings: (payload) =>
    request('/api/settings/model', { method: 'PUT', body: JSON.stringify(payload) }),
  saveJiuwenModelSettings: (payload) =>
    request('/api/settings/model/jiuwen', { method: 'PUT', body: JSON.stringify(payload) }),
  getModelContextPresets: () => request('/api/settings/model/context-presets'),
  saveCustomContextPresets: (payload) =>
    request('/api/settings/model/context-presets/custom', { method: 'PUT', body: JSON.stringify(payload) }),
  disableCustomContextPresets: () =>
    request('/api/settings/model/context-presets/custom/disable', { method: 'POST' }),
  matchModelContextPreset: (model) =>
    request(`/api/settings/model/context-presets/match?model=${encodeURIComponent(model)}`),
  saveModelPersonalPolicy: (allow) =>
    request('/api/settings/model/personal-policy', {
      method: 'PUT',
      body: JSON.stringify({ allow }),
    }),
  getMyModelSettings: () => request('/api/settings/model/me'),
  saveMyModelSettings: (engine, payload) =>
    request(`/api/settings/model/me/${engine}`, { method: 'PUT', body: JSON.stringify(payload) }),
  clearMyModelSettings: (engine) =>
    request(`/api/settings/model/me/${engine}`, { method: 'DELETE' }),
  saveDefaultEngine: (engine) =>
    request('/api/settings/model/default-engine', {
      method: 'PUT',
      body: JSON.stringify({ engine }),
    }),
  getProjectEngine: (pid) => request(`/api/projects/${pid}/engine`),
  evalStatus: (pid) => request(`/api/projects/${pid}/eval/status`),
  evalFullAnswer: (pid, resultId) =>
    request(`/api/projects/${pid}/eval/answers/${encodeURIComponent(resultId)}`),
  evalCases: (pid) => request(`/api/projects/${pid}/eval/cases`),
  evalDataSourceOptions: (pid) => request(`/api/projects/${pid}/eval/data-source`),
  evalCasesTemplateUrl: (pid) =>
    `/api/projects/${pid}/eval/cases/template?token=${encodeURIComponent(authStore.token)}`,
  // 导入的用例默认同时保存为评测集（名称缺省取文件名）
  evalCasesImport: (pid, file, { save = true, name = '' } = {}) => {
    const form = new FormData();
    form.append('file', file);
    form.append('save', save ? 'true' : 'false');
    if (name) form.append('name', name);
    return request(`/api/projects/${pid}/eval/cases/import`, { method: 'POST', body: form });
  },
  // {cases, dataSourceId, evalSetId}；dataSource 为不落库的内联配置（none / dataset）；
  // allowUnbound：用户已确认数据绑定对不上的对象按未绑定处理
  evalRun: (pid, {
    cases,
    dataSource,
    dataSourceId = '',
    evalSetId = '',
    allowUnbound = false,
    modelProfileId = '',
    caseConcurrency,
  }) =>
    request(`/api/projects/${pid}/eval/run`, {
      method: 'POST',
      body: JSON.stringify(evalRunPayload({
        cases,
        dataSource,
        dataSourceId,
        evalSetId,
        allowUnbound,
        modelProfileId,
        caseConcurrency,
      })),
    }),
  evalModelProfiles: (pid) => request(`/api/projects/${pid}/eval/model-profiles`),
  evalDataSources: (pid) => request(`/api/projects/${pid}/eval/data-sources`),
  // 数据源表单上传业务数据表（Excel）→ { file: { id, name, size } }
  uploadEvalDataFile: (pid, file, onProgress) => {
    const form = new FormData();
    form.append('file', file);
    return xhrUpload(`/api/projects/${pid}/eval/data-files`, form, onProgress);
  },
  // 一键导入预览：{data_source_id} 或 {resources, include_mock, graph}
  evalImportPreview: (pid, payload) =>
    request(`/api/projects/${pid}/eval/import-preview`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  evalDataSourceCreate: (pid, payload) =>
    request(`/api/projects/${pid}/eval/data-sources`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  evalDataSourceUpdate: (pid, sid, payload) =>
    request(`/api/projects/${pid}/eval/data-sources/${sid}`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),
  evalDataSourceDelete: (pid, sid) =>
    request(`/api/projects/${pid}/eval/data-sources/${sid}`, { method: 'DELETE' }),
  evalSets: (pid) => request(`/api/projects/${pid}/eval/sets`),
  evalSet: (pid, sid) => request(`/api/projects/${pid}/eval/sets/${sid}`),
  evalSetCreate: (pid, payload) =>
    request(`/api/projects/${pid}/eval/sets`, { method: 'POST', body: JSON.stringify(payload) }),
  evalSetDelete: (pid, sid) =>
    request(`/api/projects/${pid}/eval/sets/${sid}`, { method: 'DELETE' }),
  evalStop: (pid) => request(`/api/projects/${pid}/eval/stop`, { method: 'POST' }),
  evalResume: (pid) => request(`/api/projects/${pid}/eval/resume`, { method: 'POST' }),
  setProjectEngine: (pid, engine) =>
    request(`/api/projects/${pid}/engine`, {
      method: 'PUT',
      body: JSON.stringify({ engine }),
    }),
  getModelingMcp: (pid) => request(`/api/projects/${encodeURIComponent(pid)}/modeling-mcp`),
  getModelingMcpTokens: (pid) =>
    request(`/api/projects/${encodeURIComponent(pid)}/modeling-mcp/tokens`),
  createModelingMcpToken: (pid, draft) =>
    request(`/api/projects/${encodeURIComponent(pid)}/modeling-mcp/tokens`, {
      method: 'POST',
      body: JSON.stringify(draft),
    }),
  revokeModelingMcpToken: (pid, tokenId) =>
    request(
      `/api/projects/${encodeURIComponent(pid)}/modeling-mcp/tokens/${encodeURIComponent(tokenId)}`,
      { method: 'DELETE' },
    ),
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
  getSandboxStatus: () => request('/api/settings/sandbox'),
  getEvaluationEntry: () => request('/api/settings/evaluation/entry'),
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
  sendMessage: (pid, text, conversationId) =>
    request(`/api/projects/${pid}/session/messages`, {
      method: 'POST',
      body: JSON.stringify({ text, conversation_id: conversationId || null }),
    }),
  respondPermission: (pid, requestId, behavior, message = '', answers = null) =>
    request(`/api/projects/${pid}/session/permissions/${requestId}`, {
      method: 'POST',
      body: JSON.stringify({ behavior, message, answers }),
    }),
  interrupt: (pid, conversationId) =>
    request(`/api/projects/${pid}/session/interrupt${conversationQuery(conversationId)}`, {
      method: 'POST',
    }),
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

export function openEventStream(pid, onEvent, after = 0, conversationId = null) {
  const token = encodeURIComponent(authStore.token);
  const conversation = conversationId
    ? `&conversation=${encodeURIComponent(conversationId)}`
    : '';
  const source = new EventSource(
    `/api/projects/${pid}/session/stream?after=${after}&token=${token}${conversation}`,
  );
  source.onmessage = (msg) => {
    try {
      onEvent(JSON.parse(msg.data));
    } catch { /* keepalive 等非 JSON 行忽略 */ }
  };
  return source;
}
