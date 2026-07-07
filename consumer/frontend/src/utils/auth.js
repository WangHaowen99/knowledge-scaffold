const TOKEN_KEY = 'onto_agent_token';
const WORKSPACE_KEY = 'onto_agent_workspace_id';
export const DEFAULT_WORKSPACE_ID = '';

export const getAuthToken = () => {
  try {
    return window.localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
};

export const setAuthToken = (token) => {
  try {
    window.localStorage.setItem(TOKEN_KEY, token);
  } catch {
    // ignore storage errors
  }
};

export const clearAuthToken = () => {
  try {
    window.localStorage.removeItem(TOKEN_KEY);
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

export const authFetch = (url, options = {}) => {
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
