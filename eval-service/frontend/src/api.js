export const TOKEN = 'onto-access-token';
export const REFRESH = 'onto-refresh-token';
const prefix = '/evaluation/api';
export function saveLogin(data) {
  localStorage.setItem(TOKEN, data.access_token);
  if (data.refresh_token) localStorage.setItem(REFRESH, data.refresh_token);
}
export function clearLogin() {
  localStorage.removeItem(TOKEN);
  localStorage.removeItem(REFRESH);
}
async function refresh(original) {
  const action = async () => {
    if (localStorage.getItem(TOKEN) !== original) return false;
    const refreshToken = localStorage.getItem(REFRESH);
    if (!refreshToken) return false;
    const response = await fetch(`${prefix}/auth/refresh`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: refreshToken }),
    });
    if (!response.ok) return false;
    const data = await response.json();
    // An account change or logout while refreshing must win.
    if (localStorage.getItem(TOKEN) !== original || localStorage.getItem(REFRESH) !== refreshToken)
      return false;
    saveLogin(data);
    return true;
  };
  return navigator.locks ? navigator.locks.request('onto-auth-refresh', action) : action();
}
export async function api(path, body, method = body === undefined ? 'GET' : 'POST', retry = true) {
  const token = localStorage.getItem(TOKEN) || '';
  const upload = body instanceof FormData;
  const response = await fetch(prefix + path, {
    method,
    headers: {
      ...(upload ? {} : { 'Content-Type': 'application/json' }),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: body === undefined ? undefined : upload ? body : JSON.stringify(body),
  });
  if (!path.startsWith('/auth/') && (localStorage.getItem(TOKEN) || '') !== token)
    throw new Error('登录身份已变化，请刷新');
  if (response.status === 401 && retry && !path.startsWith('/auth/')) {
    if (await refresh(token)) return api(path, body, method, false);
    window.dispatchEvent(new Event('evaluation-auth-required'));
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok)
    throw Object.assign(new Error(data.message || `请求失败 (${response.status})`), {
      code: data.code,
      status: response.status,
      phase: data.phase,
      retryable: data.retryable,
    });
  if (!path.startsWith('/auth/') && token && localStorage.getItem(TOKEN) !== token)
    throw new Error('登录身份已变化，请刷新');
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
export function requestId(operation, value) {
  const key = `evaluation-request:${operation}:${sessionStorage.getItem('evaluation-subject') || ''}:${value}`;
  let id = sessionStorage.getItem(key);
  if (!id) {
    id = crypto.randomUUID();
    sessionStorage.setItem(key, id);
  }
  return { id, done: () => sessionStorage.removeItem(key) };
}
