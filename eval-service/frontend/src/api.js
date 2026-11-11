export const TOKEN = 'onto-access-token';
export const REFRESH = 'onto-refresh-token';
export const SESSION = 'evaluation-login-session';
const prefix = '/evaluation/api';
// Starting, resuming or stopping a run hands over the refresh token, so a run longer than one access token
// keeps its login; so do changing a verdict and regenerating, whose suggestions may wait in a queue. The service
// keeps it in memory only.
const DELEGATED = /^\/runs(\/[^/]+\/(resume|credentials|cancel|results\/\d+\/override|artifacts\/regenerate))?$/;
export function saveLogin(data) {
  // Change the session before writing credentials so a late refresh cannot replace a new login.
  localStorage.setItem(SESSION, randomId());
  localStorage.setItem(TOKEN, data.access_token);
  if (data.refresh_token) localStorage.setItem(REFRESH, data.refresh_token);
  else localStorage.removeItem(REFRESH);
}
export function clearLogin() {
  localStorage.removeItem(SESSION);
  localStorage.removeItem(TOKEN);
  localStorage.removeItem(REFRESH);
}
export function loginSession() {
  // Older credentials have no marker; all tabs treat them as one legacy session.
  return localStorage.getItem(SESSION) || (localStorage.getItem(TOKEN) ? 'legacy' : '');
}
export function tokenSubject(token) {
  try {
    const payload = JSON.parse(atob(token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/')));
    return typeof payload.sub === 'string' ? payload.sub : '';
  } catch {
    return '';
  }
}
const accountChanged = (original, current) => {
  const before = tokenSubject(original), after = tokenSubject(current);
  return before && after && before !== after;
};
let pendingRefresh;
const identityChanged = () => new Error('登录身份已变化，请刷新');
async function refresh(original, session) {
  if (pendingRefresh?.session === session) return pendingRefresh.promise;
  const action = async () => {
    if (loginSession() !== session) return 'changed';
    const current = localStorage.getItem(TOKEN);
    if (current && current !== original) return accountChanged(original, current) ? 'changed' : 'updated';
    if (current !== original) return 'changed';
    const refreshToken = localStorage.getItem(REFRESH);
    if (!refreshToken) return 'invalid';
    const response = await fetch(`${prefix}/auth/refresh`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: refreshToken }),
    });
    if (loginSession() !== session) return 'changed';
    if (localStorage.getItem(TOKEN) !== original)
      return accountChanged(original, localStorage.getItem(TOKEN)) ? 'changed' : 'updated';
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      if (response.status === 400 && error.code === 'refresh_invalid') return 'invalid';
      throw Object.assign(new Error(error.message || `续期失败 (${response.status})`), {
        code: error.code, status: response.status,
      });
    }
    const data = await response.json();
    // An account change or logout while refreshing must win.
    if (loginSession() !== session) return 'changed';
    if (localStorage.getItem(TOKEN) !== original)
      return accountChanged(original, localStorage.getItem(TOKEN)) ? 'changed' : 'updated';
    if (localStorage.getItem(REFRESH) !== refreshToken) return 'changed';
    if (!data.access_token || !data.refresh_token) throw new Error('续期响应缺少凭据');
    localStorage.setItem(TOKEN, data.access_token);
    localStorage.setItem(REFRESH, data.refresh_token);
    return 'updated';
  };
  const promise = navigator.locks ? navigator.locks.request('onto-auth-refresh', action) : action();
  pendingRefresh = { session, promise };
  try {
    return await promise;
  } finally {
    if (pendingRefresh?.promise === promise) pendingRefresh = undefined;
  }
}
// read 'text' returns the body as text, for markdown artifacts; errors are still JSON.
export async function api(path, body, method = body === undefined ? 'GET' : 'POST', retry = true, read = 'json', expectedSession) {
  const session = loginSession();
  if (expectedSession !== undefined && session !== expectedSession) throw identityChanged();
  const token = localStorage.getItem(TOKEN) || '';
  const delegated = method === 'POST' && DELEGATED.test(path) ? localStorage.getItem(REFRESH) : '';
  if (!path.startsWith('/auth/') && (loginSession() !== session || accountChanged(token, localStorage.getItem(TOKEN))))
    throw identityChanged();
  const upload = body instanceof FormData;
  const response = await fetch(prefix + path, {
    method,
    headers: {
      ...(upload ? {} : { 'Content-Type': 'application/json' }),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(delegated ? { 'X-Refresh-Token': delegated } : {}),
    },
    body: body === undefined ? undefined : upload ? body : JSON.stringify(body),
  });
  if (!path.startsWith('/auth/') && (loginSession() !== session || accountChanged(token, localStorage.getItem(TOKEN))))
    throw identityChanged();
  if (response.status === 401 && retry && !path.startsWith('/auth/')) {
    const outcome = await refresh(token, session);
    if (loginSession() !== session || accountChanged(token, localStorage.getItem(TOKEN)) || outcome === 'changed')
      throw identityChanged();
    if (outcome === 'updated') return api(path, body, method, false, read, session);
    if (localStorage.getItem(TOKEN) !== token) return api(path, body, method, false, read, session);
    window.dispatchEvent(new CustomEvent('evaluation-auth-required', { detail: { session, token } }));
  }
  const data = read === 'text' && response.ok ? await response.text() : await response.json().catch(() => ({}));
  if (!response.ok)
    throw Object.assign(new Error(data.message || `请求失败 (${response.status})`), {
      code: data.code,
      status: response.status,
      phase: data.phase,
      retryable: data.retryable,
      details: data.details,
    });
  if (!path.startsWith('/auth/') && (loginSession() !== session || accountChanged(token, localStorage.getItem(TOKEN))))
    throw identityChanged();
  return data;
}

export async function allPages(path) {
  const items = [],
    visited = new Set();
  let offset = 0;
  do {
    if (visited.has(offset)) throw new Error('分页响应重复，请重新加载');
    visited.add(offset);
    const page = await api(`${path}${path.includes('?') ? '&' : '?'}offset=${offset}`);
    items.push(...page.items);
    offset = page.next_offset;
  } while (offset != null);
  return items;
}
// crypto.randomUUID exists only on HTTPS and localhost pages; the service is often opened over plain HTTP.
export function randomId() {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
export function requestId(operation, value) {
  const key = `evaluation-request:${operation}:${sessionStorage.getItem('evaluation-subject') || ''}:${value}`;
  let id = sessionStorage.getItem(key);
  if (!id) {
    id = randomId();
    sessionStorage.setItem(key, id);
  }
  return { id, done: () => sessionStorage.removeItem(key) };
}
