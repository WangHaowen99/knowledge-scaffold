const TOKEN_KEY = 'onto_agent_token';
const REFRESH_TOKEN_KEY = 'onto_agent_refresh_token';
const WORKSPACE_KEY = 'onto_agent_workspace_id';
export const AUTH_CLEARED_EVENT = 'onto-auth-cleared';
export const DEFAULT_WORKSPACE_ID = '';

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
  try {
    window.localStorage.setItem(TOKEN_KEY, token);
    if (refreshToken) {
      window.localStorage.setItem(REFRESH_TOKEN_KEY, refreshToken);
    }
  } catch {
    // ignore storage errors
  }
};

export const clearAuthToken = () => {
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

const doAuthFetch = (url, options = {}) => {
  const token = getAuthToken();
  const workspaceId = getCurrentWorkspaceId();
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
    refreshInFlight = (async () => {
      const res = await fetch('/auth/refresh', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: getRefreshToken() }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.token) {
        throw new Error('刷新登录令牌失败');
      }
      setAuthToken(data.token, data.refresh_token);
    })().finally(() => {
      refreshInFlight = null;
    });
  }
  return refreshInFlight;
};

// refresh 自身与 login 不参与静默续期（防递归）
const REFRESH_EXEMPT_PATHS = ['/auth/refresh', '/auth/login'];

export const authFetch = async (url, options = {}) => {
  const res = await doAuthFetch(url, options);
  if (
    res.status !== 401
    || !getRefreshToken()
    || REFRESH_EXEMPT_PATHS.some((path) => String(url).startsWith(path))
  ) {
    return res;
  }
  try {
    await refreshAuthToken();
  } catch {
    // 续期失败：清除全部凭据，把原 401 交还调用方走现有登出路径
    clearAuthToken();
    return res;
  }
  return doAuthFetch(url, options);
};
