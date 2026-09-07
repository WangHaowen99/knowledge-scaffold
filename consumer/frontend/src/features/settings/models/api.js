import { authFetch } from '../../../shared/api/auth.js';

export async function requestModelJson(
  request,
  url,
  options = {},
  fallbackMessage = '模型配置请求失败',
) {
  const response = await request(url, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(payload.detail || payload.error || `${fallbackMessage}（HTTP ${response.status}）`);
    error.status = response.status;
    error.code = payload.code || '';
    throw error;
  }
  return payload;
}

const jsonOptions = (method, body, signal) => ({
  method,
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
  signal,
});

export async function loadGlobalModels({ request = authFetch, signal } = {}) {
  const response = await request('/api/system-config', { signal });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(payload.detail || payload.error || `加载全局模型失败（HTTP ${response.status}）`);
    error.status = response.status;
    error.code = payload.code || '';
    throw error;
  }
  const generationHeader = response.headers?.get?.('x-system-config-generation') || '';
  const generation = Number(generationHeader);
  return {
    config: payload.llm_profiles || { profiles: [], default_main_profile_ref: null, default_task_profile_ref: null },
    snapshot: {
      generation: generationHeader && Number.isSafeInteger(generation) && generation >= 0
        ? generation
        : null,
      fingerprint: response.headers?.get?.('x-llm-profiles-fingerprint') || '',
    },
  };
}

export function saveGlobalModels(data, { request = authFetch, signal, precondition } = {}) {
  const body = { section: 'llm_profiles', data };
  if (precondition) body.precondition = precondition;
  return requestModelJson(
    request,
    '/api/system-config',
    jsonOptions('PATCH', body, signal),
    '保存全局模型失败',
  );
}

export function testGlobalModel(data, { request = authFetch, signal } = {}) {
  return requestModelJson(
    request,
    '/api/system-config/test',
    jsonOptions('POST', { section: 'llm_profiles', data }, signal),
    '测试全局模型失败',
  );
}

const workspacePath = (workspaceId, suffix = '') => (
  `/agentic/workspaces/${encodeURIComponent(String(workspaceId || ''))}${suffix}`
);

export async function loadWorkspaceModels(workspaceId, { request = authFetch, signal } = {}) {
  if (!workspaceId) throw new Error('缺少工作空间标识');
  const [available, privateConfig, runtime] = await Promise.all([
    requestModelJson(request, '/agentic/model-profiles', { signal }, '加载可用模型失败'),
    requestModelJson(request, workspacePath(workspaceId, '/llm-profiles'), { signal }, '加载空间私有模型失败'),
    requestModelJson(request, workspacePath(workspaceId, '/runtime-config'), { signal }, '加载空间模型引用失败'),
  ]);
  return {
    availableProfiles: Array.isArray(available.profiles) ? available.profiles : [],
    privateProfiles: Array.isArray(privateConfig.profiles) ? privateConfig.profiles : [],
    runtime,
  };
}

export async function loadWorkspacePrivateModels(workspaceId, { request = authFetch, signal } = {}) {
  if (!workspaceId) throw new Error('缺少工作空间标识');
  const payload = await requestModelJson(
    request,
    workspacePath(workspaceId, '/llm-profiles'),
    { signal },
    '加载空间私有模型失败',
  );
  return Array.isArray(payload.profiles) ? payload.profiles : [];
}

export function loadWorkspaceRuntime(workspaceId, { request = authFetch, signal } = {}) {
  if (!workspaceId) return Promise.reject(new Error('缺少工作空间标识'));
  return requestModelJson(
    request,
    workspacePath(workspaceId, '/runtime-config'),
    { signal },
    '加载空间模型引用失败',
  );
}

export function saveWorkspaceModels(workspaceId, data, { request = authFetch, signal } = {}) {
  if (!workspaceId) return Promise.reject(new Error('缺少工作空间标识'));
  return requestModelJson(
    request,
    workspacePath(workspaceId, '/llm-profiles'),
    jsonOptions('PATCH', data, signal),
    '保存空间私有模型失败',
  );
}

export function testWorkspaceModel(workspaceId, data, { request = authFetch, signal } = {}) {
  if (!workspaceId) return Promise.reject(new Error('缺少工作空间标识'));
  return requestModelJson(
    request,
    workspacePath(workspaceId, '/llm-profiles/test'),
    jsonOptions('POST', data, signal),
    '测试空间私有模型失败',
  );
}

export function saveWorkspaceModelRefs(workspaceId, llm, { request = authFetch, signal } = {}) {
  if (!workspaceId) return Promise.reject(new Error('缺少工作空间标识'));
  return requestModelJson(
    request,
    workspacePath(workspaceId, '/runtime-config'),
    jsonOptions('PATCH', { llm }, signal),
    '保存空间模型引用失败',
  );
}

export async function loadModelProfileReferences(profileId, { request = authFetch, signal } = {}) {
  const normalizedProfileId = String(profileId || '').trim();
  const query = normalizedProfileId ? `?profile_id=${encodeURIComponent(normalizedProfileId)}` : '';
  const payload = await requestModelJson(
    request,
    `/api/model-profile-references${query}`,
    { signal },
    '加载模型引用失败',
  );
  const sourceEntries = Array.isArray(payload.entries) ? payload.entries : [];
  const entries = sourceEntries.map((entry) => {
    const references = Array.isArray(entry?.references) ? entry.references.map((reference) => ({
      field: String(reference?.field || ''),
      scope: String(reference?.scope || ''),
      profile_id: String(reference?.profile_id || ''),
    })) : [];
    const referencesValid = Array.isArray(entry?.references) && references.every((reference) => (
      ['default_main_profile_ref', 'default_task_profile_ref'].includes(reference.field)
      && ['global', 'workspace'].includes(reference.scope)
      && Boolean(reference.profile_id)
    ));
    const diagnostic = entry?.diagnostic && typeof entry.diagnostic === 'object'
      ? {
        code: String(entry.diagnostic.code || 'runtime_config_unavailable'),
        message: String(entry.diagnostic.message || '模型引用无法核对'),
      }
      : null;
    return {
      workspaceId: String(entry?.workspace_id || ''),
      workspaceName: String(entry?.workspace_name || ''),
      configGeneration: entry?.config_generation ?? null,
      references,
      runtimeKnown: !diagnostic && referencesValid,
      ...(diagnostic ? { diagnostic } : {}),
    };
  });
  const generation = typeof payload.generation === 'string' ? payload.generation : '';
  return {
    complete: payload.complete === true
      && Array.isArray(payload.entries)
      && Boolean(generation)
      && entries.every((entry) => entry.runtimeKnown),
    generation,
    entries,
  };
}
