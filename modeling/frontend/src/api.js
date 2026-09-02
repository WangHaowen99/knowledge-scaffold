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
  const { bodyFactory, ...requestOptions } = options;
  const body = typeof bodyFactory === 'function' ? bodyFactory() : requestOptions.body;
  const headers = { ...(requestOptions.headers || {}) };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body && !(body instanceof FormData)) headers['Content-Type'] = 'application/json';
  return fetch(path, { ...requestOptions, body, headers });
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
  let res;
  try {
    res = await authFetch(path, options);
  } catch (cause) {
    throw Object.assign(new Error('网络连接中断，请稍后重试；已经提交的数据不会丢失'), { cause });
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw Object.assign(new Error(data.error || `请求失败（${res.status}）`), { status: res.status, data });
  }
  return data;
}

export async function requireOkResponse(res) {
  if (res.ok) return res;
  const data = await res.json().catch(() => ({}));
  throw Object.assign(new Error(data.error || `请求失败（${res.status}）`), {
    status: res.status,
    data,
  });
}

export const login = (username, password) =>
  api('/auth/login', { method: 'POST', body: JSON.stringify({ username, password }) });
export const register = (username, email, password) =>
  api('/auth/register', { method: 'POST', body: JSON.stringify({ username, email, password }) });
export const me = () => api('/api/me');
export const getAcceptanceEnvironment = () => api('/api/acceptance');
export const createAcceptanceScenario = (scenarioId) => api(
  `/api/acceptance/scenarios/${encodeURIComponent(scenarioId)}/create`,
  { method: 'POST', body: '{}' },
);
export const armAcceptanceFault = (faultId) => api(
  `/api/acceptance/faults/${encodeURIComponent(faultId)}/arm`,
  { method: 'POST', body: '{}' },
);
export const closeAcceptanceFault = (faultId) => api(
  `/api/acceptance/faults/${encodeURIComponent(faultId)}/close`,
  { method: 'POST', body: '{}' },
);
export const closeAllAcceptanceFaults = () => api(
  '/api/acceptance/faults/close-all',
  { method: 'POST', body: '{}' },
);
export const listProjects = () => api('/api/projects');
export const listProjectMembers = (id) => api(`/api/projects/${encodeURIComponent(id)}/members`);
export const setProjectMember = (id, username, role) => api(`/api/projects/${encodeURIComponent(id)}/members`, {
  method: 'PUT', body: JSON.stringify({ username, role }),
});
export const createProject = (input) => api('/api/projects', { method: 'POST', body: JSON.stringify(input) });
export const deleteProject = (id) => api(`/api/projects/${encodeURIComponent(id)}`, {
  method: 'DELETE', body: JSON.stringify({ confirmed: true }),
});
export const startProjectTransferExport = (id) => api(
  `/api/projects/${encodeURIComponent(id)}/transfer-exports`,
  { method: 'POST', body: '{}' },
);
export const startProjectTransferImport = (file) => {
  const form = new FormData();
  form.append('file', file);
  return api('/api/project-transfers/imports', { method: 'POST', body: form });
};
export const getProjectTransferJob = (jobId) => api(
  `/api/project-transfers/jobs/${encodeURIComponent(jobId)}`,
);
export const downloadProjectTransfer = async (jobId) => requireOkResponse(await authFetch(
  `/api/project-transfers/jobs/${encodeURIComponent(jobId)}/download`,
));
export const importPackageWorkspace = (file, packageName = '') => {
  const form = new FormData();
  if (String(packageName || '').trim()) form.append('package_name', String(packageName).trim());
  form.append('file', file);
  return api('/api/package-workspaces/import', { method: 'POST', body: form });
};
export const updateProject = (id, input) => api(`/api/projects/${encodeURIComponent(id)}`, {
  method: 'PUT', body: JSON.stringify(input),
});
export const getProject = (id) => api(`/api/projects/${encodeURIComponent(id)}`);
export const uploadDocument = (id, file, replacementOf = '', directives = {}) => {
  const form = new FormData();
  form.append('file', file);
  const query = new URLSearchParams();
  if (replacementOf) query.set('replacementOf', replacementOf);
  if (directives.purpose) query.set('purpose', directives.purpose);
  if (directives.readingMode) query.set('readingMode', directives.readingMode);
  if (directives.autoContinue === false) query.set('agentFollowUp', '0');
  if (directives.uploadBatchId) query.set('uploadBatchId', directives.uploadBatchId);
  if (directives.uploadBatchSize) query.set('uploadBatchSize', String(directives.uploadBatchSize));
  const suffix = query.size ? `?${query}` : '';
  return api(`/api/projects/${encodeURIComponent(id)}/documents${suffix}`, { method: 'POST', body: form });
};
export const updateDocumentDirectives = (id, docId, directives) => api(
  `/api/projects/${encodeURIComponent(id)}/documents/${encodeURIComponent(docId)}/directives`,
  { method: 'PATCH', body: JSON.stringify(directives) },
);
export const confirmDocumentPurposeReview = (id, docId, input) => api(
  `/api/projects/${encodeURIComponent(id)}/documents/${encodeURIComponent(docId)}/purpose-review`,
  { method: 'PUT', body: JSON.stringify(input) },
);
export const confirmDocumentPurposeReviews = (id, documents) => api(
  `/api/projects/${encodeURIComponent(id)}/documents/purpose-reviews`,
  { method: 'PUT', body: JSON.stringify({ documents }) },
);
export const deleteDocument = (id, docId) => api(`/api/projects/${encodeURIComponent(id)}/documents/${encodeURIComponent(docId)}`, { method: 'DELETE' });
export const downloadDocument = async (id, docId) => requireOkResponse(await authFetch(
  `/api/projects/${encodeURIComponent(id)}/documents/${encodeURIComponent(docId)}/download`,
));
export const downloadTermTableTemplate = async (id = '') => requireOkResponse(await authFetch(
  id ? `/api/projects/${encodeURIComponent(id)}/documents/term-template` : '/api/documents/term-template',
));
export const listMaterialRevisions = (id, sourceKey = '') => api(
  `/api/projects/${encodeURIComponent(id)}/documents/revisions${sourceKey ? `?sourceKey=${encodeURIComponent(sourceKey)}` : ''}`,
);
export const runAnalyze = (id) => api(`/api/projects/${encodeURIComponent(id)}/analyze`, { method: 'POST' });

const P = (id) => `/api/projects/${encodeURIComponent(id)}`;
export const savePackageDataSource = (id, dataSourceType) => api(`${P(id)}/agent/package-data-source`, {
  method: 'PUT', body: JSON.stringify({ dataSourceType }),
});
export const getPackageDataSource = (id) => api(`${P(id)}/agent/package-data-source`);
export const getPackage = (id) => api(`${P(id)}/package`);
export const getPackageFile = (id, path, options = {}) => api(
  `${P(id)}/package/file?path=${encodeURIComponent(path)}`,
  options,
);
export const exportPackageUrl = (id) => `${P(id)}/package/export`;
export const exportModelingAuditUrl = (id) => `${P(id)}/audit/export`;
export const getPackageDraft = (id) => api(`${P(id)}/package/draft`);
export const createPackageDraft = (id) => api(`${P(id)}/package/draft`, { method: 'POST', body: '{}' });
export const getPackageDraftFile = (id, path, options = {}) => api(
  `${P(id)}/package/draft/file?path=${encodeURIComponent(path)}`,
  options,
);
export const savePackageDraftFile = (id, path, content, revision, expectedContentHash, draftId) => api(`${P(id)}/package/draft/file`, {
  method: 'PUT', body: JSON.stringify({ path, content, revision, expectedContentHash, draftId }),
});
export const revertPackageDraftFile = (id, path, revision, draftId) => api(
  `${P(id)}/package/draft/file?path=${encodeURIComponent(path)}&revision=${encodeURIComponent(revision)}${draftId ? `&draftId=${encodeURIComponent(draftId)}` : ''}`,
  { method: 'DELETE' },
);
export const discardPackageDraft = (id, revision, draftId) => api(`${P(id)}/package/draft`, {
  method: 'DELETE', body: JSON.stringify({ confirmed: true, revision, draftId }),
});
export const publishPackageDraft = (id, revision, summary, draftId) => api(`${P(id)}/package/draft/publish`, {
  method: 'POST', body: JSON.stringify({ confirmed: true, revision, summary, draftId }),
});
export const getSemanticBindingSuggestions = (id) => api(`${P(id)}/package/semantic-bindings/suggestions`);
export const applySemanticBindings = (id, input) => api(`${P(id)}/package/semantic-bindings/apply`, {
  method: 'POST', body: JSON.stringify(input),
});
export const clearSemanticBindings = (id, input) => api(`${P(id)}/package/semantic-bindings/clear`, {
  method: 'POST', body: JSON.stringify(input),
});
export const ignoreSemanticBindings = (id, input) => api(`${P(id)}/package/semantic-bindings/ignore`, {
  method: 'POST', body: JSON.stringify(input),
});

// ---- M3 评测 ----
export const getDataSource = (id) => api(`${P(id)}/data-source`);
export const saveDataSource = (id, input) => api(`${P(id)}/data-source`, { method: 'PUT', body: JSON.stringify(input) });
export const getEvaluationDatabaseTarget = (id) => api(`${P(id)}/evaluation-database/target`);
export const createProjectEvaluationDatabase = (id, selection) => api(
  `${P(id)}/evaluation-database/project-database`,
  {
    method: 'POST',
    body: JSON.stringify({
      database_url_mode: selection.mode,
      ...(selection.mode === 'custom' ? { database_url: selection.databaseUrl } : {}),
    }),
  },
);
export const importEvaluationDatabase = (id, file, replaceConfirmed, selection) => {
  const form = new FormData();
  if (replaceConfirmed) form.append('replace_confirmation', 'replace_same_name_tables');
  form.append('database_url_mode', selection.mode);
  if (selection.mode === 'custom') form.append('database_url', selection.databaseUrl);
  form.append('file', file);
  return api(`${P(id)}/evaluation-database/import`, { method: 'POST', body: form });
};
export const quickImportEvaluationDatabase = (id, file) => {
  const form = new FormData();
  form.append('file', file);
  return api(`${P(id)}/evaluation-database/quick-import`, { method: 'POST', body: form });
};
export const listEvaluationDatabaseImports = (id) => api(`${P(id)}/evaluation-database/imports`);
export const uploadGraphImport = (id, file) => {
  const form = new FormData();
  form.append('file', file);
  return api(`${P(id)}/graph-import/uploads`, { method: 'POST', body: form });
};
export const validateGraphImport = (id, uploadId, mapping) => api(
  `${P(id)}/graph-import/uploads/${encodeURIComponent(uploadId)}/validate`,
  { method: 'POST', body: JSON.stringify({ mapping }) },
);
export const executeGraphImport = (id, uploadId, mapping, password, replaceExisting = false) => api(
  `${P(id)}/graph-import/uploads/${encodeURIComponent(uploadId)}/execute`,
  { method: 'POST', body: JSON.stringify({ mapping, password, replaceExisting }) },
);
export const cancelGraphImport = (id, uploadId) => api(
  `${P(id)}/graph-import/uploads/${encodeURIComponent(uploadId)}`,
  { method: 'DELETE' },
);
export const cancelGraphImportRun = (id, runId) => api(
  `${P(id)}/graph-imports/${encodeURIComponent(runId)}/cancel`,
  { method: 'POST', body: '{}' },
);
export const listGraphImports = (id) => api(`${P(id)}/graph-imports`);
export const listTestcases = (id) => api(`${P(id)}/testcases`);
export const addTestcase = (id, question, groundTruth) => api(`${P(id)}/testcases`, { method: 'POST', body: JSON.stringify({ question, groundTruth }) });
export const addTestcases = (id, cases) => api(`${P(id)}/testcases/batch`, { method: 'POST', body: JSON.stringify({ cases }) });
export const prefillTestcases = (id) => api(`${P(id)}/testcases/prefill`, { method: 'POST', body: '{}' });
export const updateTestcase = (id, tcId, patch) => api(`${P(id)}/testcases/${encodeURIComponent(tcId)}`, { method: 'PATCH', body: JSON.stringify(patch) });
export const deleteTestcase = (id, tcId) => api(`${P(id)}/testcases/${encodeURIComponent(tcId)}`, { method: 'DELETE' });
export const startEvaluate = (id) => api(`${P(id)}/evaluate`, {
  method: 'POST',
  bodyFactory: () => JSON.stringify({ refresh_token: getRefreshToken() }),
});
export const cancelEvaluate = (id, roundId) => api(`${P(id)}/evaluate/cancel`, {
  method: 'POST', body: JSON.stringify({ roundId }),
});
export const listRounds = (id) => api(`${P(id)}/rounds`);
export const getRound = (id, roundId) => api(`${P(id)}/rounds/${encodeURIComponent(roundId)}`);
export const overrideVerdict = (id, resultId, verdict, reason) => api(`${P(id)}/results/${encodeURIComponent(resultId)}`, { method: 'PATCH', body: JSON.stringify({ verdict, reason }) });

// ---- M4 优化与版本 ----
const optimizeTasks = new Map();

const runOptimizeTask = (id, kind, request) => {
  const current = optimizeTasks.get(id);
  if (current) {
    if (current.kind === kind) return current.promise;
    return Promise.reject(new Error('该项目已有优化任务正在进行中'));
  }

  const task = { kind, startedAt: new Date().toISOString(), promise: Promise.resolve(request()) };
  task.promise = task.promise.finally(() => {
    if (optimizeTasks.get(id) === task) optimizeTasks.delete(id);
  });
  optimizeTasks.set(id, task);
  return task.promise;
};

// 优化面板切换阶段会卸载；任务放在 API 模块中，重新挂载后可继续等待并刷新结果。
export const getOptimizeTask = (id) => optimizeTasks.get(id) ?? null;
export const diagnoseOptimize = (id, roundId) => runOptimizeTask(id, 'diagnose', () =>
  api(`${P(id)}/optimize/diagnose`, { method: 'POST', body: JSON.stringify(roundId ? { roundId } : {}) }));
export const listDiagnoses = (id, roundId) => api(`${P(id)}/optimize/diagnoses${roundId ? `?roundId=${encodeURIComponent(roundId)}` : ''}`);
export const resolveDiagnosis = (id, diagnosisId, action, note = '') =>
  api(`${P(id)}/optimize/diagnoses/${encodeURIComponent(diagnosisId)}/resolve`, {
    method: 'POST', body: JSON.stringify({ action, note }),
  });
export const proposeOptimize = (id, roundId) => runOptimizeTask(id, 'propose', () =>
  api(`${P(id)}/optimize/propose`, { method: 'POST', body: JSON.stringify(roundId ? { roundId } : {}) }));
export const listSuggestions = (id, roundId) => api(`${P(id)}/optimize/suggestions${roundId ? `?roundId=${encodeURIComponent(roundId)}` : ''}`);
export const getOptimizeApplyStatus = (id) => api(`${P(id)}/optimize/apply-status`);
export const applyOptimize = (id, suggestionIds) => runOptimizeTask(id, 'apply', () =>
  api(`${P(id)}/optimize/apply`, { method: 'POST', body: JSON.stringify({ suggestionIds }) }));
export const reviewOptimizeApply = (id, taskId, action, note = '') =>
  api(`${P(id)}/optimize/apply-review`, {
    method: 'POST', body: JSON.stringify({ taskId, action, note }),
  });
export const rejectSuggestions = (id, suggestionIds) => api(`${P(id)}/optimize/reject`, { method: 'POST', body: JSON.stringify({ suggestionIds }) });
export const listVersions = (id) => api(`${P(id)}/versions`);
export const exportVersionUrl = (id, version) => `${P(id)}/package/export${version ? `?version=${encodeURIComponent(version)}` : ''}`;

// ---- 包完善（人工草稿 + 修缮对话）----
// 整份流程图文档落盘；服务端据此重渲染 processes.json、index.md 与各流程同名 .mmd
export const savePackageDiagram = (id, document, expectedRevision) => api(`${P(id)}/package/diagram`, {
  method: 'PUT', body: JSON.stringify({ document, expectedRevision }),
});
export const packageChat = (id, message, requestId) => api(`${P(id)}/package/chat`, {
  method: 'POST', body: JSON.stringify({ message, requestId }),
});
// 把评测诊断出的知识包缺陷登记成建模修缮任务（只登记，不直接改包）
export const planEvaluationDefectRepair = (id, roundId, diagnosisIds) => api(
  `${P(id)}/optimize/diagnoses/plan-repair`,
  { method: 'POST', body: JSON.stringify({ roundId, diagnosisIds }) },
);
export const runPackageCheck = (id) => api(`${P(id)}/package/check`, { method: 'POST', body: '{}' });
export const getPackageCheck = (id) => api(`${P(id)}/package/check`);
export const deferPackageValidationErrors = (id, reason) => api(
  `${P(id)}/ai-construction/knowledge/defer-errors`, {
    method: 'POST', body: JSON.stringify({ reason }),
  },
);
export const acceptPackageWarnings = (id, revision, fingerprints, note, phase) => api(
  `${P(id)}/package/warnings/accept`, {
    method: 'POST', body: JSON.stringify({ revision, fingerprints, note, phase }),
  },
);
export const releaseProject = (id) => api(`${P(id)}/release`, { method: 'POST', body: '{}' });

// ---- 配置中心 ----
export const getSettings = () => api('/api/settings');
export const saveSettings = (patch) => api('/api/settings', { method: 'PUT', body: JSON.stringify(patch) });
export const syncDefaultLlm = (purpose) => api('/api/settings/llm/sync-default', {
  method: 'POST', body: JSON.stringify(purpose ? { purpose } : {}),
});
export const testLlm = (cfg) => api('/api/settings/llm/test', { method: 'POST', body: JSON.stringify(cfg || {}) });
export const getAdminLlmDefaults = () => api('/api/admin/llm-defaults');
export const saveAdminLlmDefault = (draft) => api('/api/admin/llm-defaults', {
  method: 'PUT', body: JSON.stringify(draft),
});
export const testAdminLlmDefault = (draft) => api('/api/admin/llm-defaults/test', {
  method: 'POST', body: JSON.stringify(draft),
});
export const testConsumer = (url) => api('/api/settings/consumer/test', { method: 'POST', body: JSON.stringify({ url }) });
export const getProjectMcpRuntimeStatus = (id) => api(`${P(id)}/mcp/runtime/status`);
export const listProjectMcpTokens = (id) => api(`${P(id)}/mcp/tokens`);
export const createProjectMcpToken = (id, draft) => api(`${P(id)}/mcp/tokens`, {
  method: 'POST',
  body: JSON.stringify(draft || {}),
});
export const revokeProjectMcpToken = (id, tokenId) => api(`${P(id)}/mcp/tokens/${encodeURIComponent(tokenId)}`, {
  method: 'DELETE',
});
