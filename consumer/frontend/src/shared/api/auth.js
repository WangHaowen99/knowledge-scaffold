const TOKEN_KEY = 'onto_agent_token';
const REFRESH_TOKEN_KEY = 'onto_agent_refresh_token';
const WORKSPACE_KEY = 'onto_agent_workspace_id';
export const AUTH_CLEARED_EVENT = 'onto-auth-cleared';
export const DEFAULT_WORKSPACE_ID = '';
let credentialEpoch = 0;

export const getAuthToken = () => {
  try {
    return window.localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
};

export const getRefreshToken = () => {
  try {
    return window.localStorage.getItem(REFRESH_TOKEN_KEY);
  } catch {
    return null;
  }
};

export const setAuthToken = (token, refreshToken) => {
  credentialEpoch += 1;
  try {
    window.localStorage.setItem(TOKEN_KEY, token);
    if (refreshToken) {
      window.localStorage.setItem(REFRESH_TOKEN_KEY, refreshToken);
    } else {
      window.localStorage.removeItem(REFRESH_TOKEN_KEY);
    }
  } catch {
    // ignore storage errors
  }
};

export const clearAuthToken = () => {
  credentialEpoch += 1;
  try {
    window.localStorage.removeItem(TOKEN_KEY);
    window.localStorage.removeItem(REFRESH_TOKEN_KEY);
    window.dispatchEvent(new Event(AUTH_CLEARED_EVENT));
  } catch {
    // ignore storage errors
  }
};

export const getCurrentWorkspaceId = () => {
  try {
    return window.localStorage.getItem(WORKSPACE_KEY) || DEFAULT_WORKSPACE_ID;
  } catch {
    return DEFAULT_WORKSPACE_ID;
  }
};

export const setCurrentWorkspaceId = (workspaceId) => {
  try {
    if (workspaceId) {
      window.localStorage.setItem(WORKSPACE_KEY, workspaceId);
    } else {
      window.localStorage.removeItem(WORKSPACE_KEY);
    }
  } catch {
    // ignore storage errors
  }
};

const doAuthFetch = (url, options = {}, workspaceId = getCurrentWorkspaceId()) => {
  const token = getAuthToken();
  const headers = new Headers(options.headers || {});
  if (token) {
    headers.set('Authorization', `Bearer ${token}`);
  }
  if (workspaceId) {
    // Headers.set only supports ISO-8859-1, so we encode for safety
    headers.set('X-Workspace-Id', encodeURIComponent(workspaceId));
  }
  return fetch(url, { ...options, headers });
};

// 静默续期并发去重：同一时刻只发一个 refresh 请求，其余 401 请求共用其结果
let refreshInFlight = null;

const refreshAuthToken = () => {
  if (!refreshInFlight) {
    const refreshToken = getRefreshToken();
    const requestEpoch = credentialEpoch;
    const refreshRequest = {
      epoch: requestEpoch,
      promise: null,
    };
    refreshRequest.promise = (async () => {
      const res = await fetch('/auth/refresh', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: refreshToken }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.token) {
        throw new Error('刷新登录令牌失败');
      }
      if (credentialEpoch !== requestEpoch || getRefreshToken() !== refreshToken) {
        throw new Error('登录状态已变化，忽略过期的令牌刷新结果');
      }
      setAuthToken(data.token, data.refresh_token);
    })().finally(() => {
      if (refreshInFlight === refreshRequest) refreshInFlight = null;
    });
    refreshInFlight = refreshRequest;
  }
  return refreshInFlight;
};

// refresh 自身与 login 不参与静默续期（防递归）
const REFRESH_EXEMPT_PATHS = ['/auth/refresh', '/auth/login'];

export const authFetch = async (url, options = {}) => {
  const workspaceId = getCurrentWorkspaceId();
  const requestCredentialEpoch = credentialEpoch;
  const res = await doAuthFetch(url, options, workspaceId);
  if (
    res.status !== 401
    || !getRefreshToken()
    || REFRESH_EXEMPT_PATHS.some((path) => String(url).startsWith(path))
  ) {
    return res;
  }
  const refreshRequest = refreshAuthToken();
  try {
    await refreshRequest.promise;
  } catch {
    // 只有与本次请求同一代的 refresh 才能清理它看到的凭据。新登录请求即使
    // 短暂加入旧 refresh，也不会被旧请求失败结果清空。
    if (
      refreshRequest.epoch === requestCredentialEpoch
      && credentialEpoch === requestCredentialEpoch
    ) clearAuthToken();
    return res;
  }
  return doAuthFetch(url, options, workspaceId);
};
