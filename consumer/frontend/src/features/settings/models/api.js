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
  const payload = await requestModelJson(request, '/api/system-config', { signal }, '加载全局模型失败');
  return payload.llm_profiles || { profiles: [], default_main_profile_ref: null, default_task_profile_ref: null };
}

export function saveGlobalModels(data, { request = authFetch, signal } = {}) {
  return requestModelJson(
    request,
    '/api/system-config',
    jsonOptions('PATCH', { section: 'llm_profiles', data }, signal),
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

export async function loadWorkspaceReferenceInventory({ request = authFetch, signal, workspaces: providedWorkspaces } = {}) {
  const listPayload = Array.isArray(providedWorkspaces) && providedWorkspaces.length
    ? { workspaces: providedWorkspaces }
    : await requestModelJson(
      request,
      '/agentic/workspaces',
      { signal },
      '加载工作空间列表失败',
    );
  const workspaces = Array.isArray(listPayload.workspaces) ? listPayload.workspaces : [];
  const entries = await Promise.all(workspaces.map(async (workspace) => {
    const workspaceId = String(workspace?.id || '');
    try {
      const runtime = await requestModelJson(
        request,
        workspacePath(workspaceId, '/runtime-config'),
        { signal },
        `加载工作空间 ${workspaceId} 的运行时引用失败`,
      );
      return {
        workspaceId,
        workspaceName: String(workspace?.name || ''),
        runtime,
        runtimeKnown: true,
      };
    } catch (error) {
      if (error?.name === 'AbortError') throw error;
      return {
        workspaceId,
        workspaceName: String(workspace?.name || ''),
        runtime: null,
        runtimeKnown: false,
        error: error?.message || '运行时引用加载失败',
      };
    }
  }));
  return {
    complete: entries.every((entry) => entry.runtimeKnown),
    entries,
  };
}
