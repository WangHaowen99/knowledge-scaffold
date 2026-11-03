// API v1 客户端：鉴权与无感刷新、统一错误体、分页、409/423/428 的处理约定
import { ApiError, errorFromResponse } from './errors.js';

export const API_PREFIX = '/api/v1';

// 令牌存放：access / refresh 两个键；本地模式不带令牌
export function createTokenStore(storage) {
  const ACCESS = 'onto.access_token';
  const REFRESH = 'onto.refresh_token';
  const LOCAL = 'onto.local_mode';
  const get = (k) => {
    try { return storage?.getItem(k) || ''; } catch { return ''; }
  };
  const set = (k, v) => {
    try { if (v) storage?.setItem(k, v); else storage?.removeItem(k); } catch { /* 存储不可用时只在内存里失效 */ }
  };
  return {
    get access() { return get(ACCESS); },
    get refresh() { return get(REFRESH); },
    get localMode() { return get(LOCAL) === '1'; },
    save({ access_token, refresh_token }) {
      set(ACCESS, access_token);
      if (refresh_token) set(REFRESH, refresh_token);
      set(LOCAL, '');
    },
    enterLocal() { set(ACCESS, ''); set(REFRESH, ''); set(LOCAL, '1'); },
    clear() { set(ACCESS, ''); set(REFRESH, ''); set(LOCAL, ''); },
  };
}

export function buildQuery(query) {
  if (!query) return '';
  const params = new URLSearchParams();
  Object.entries(query).forEach(([k, v]) => {
    if (v === undefined || v === null || v === '') return;
    params.set(k, Array.isArray(v) ? v.join(',') : String(v));
  });
  const s = params.toString();
  return s ? `?${s}` : '';
}

/**
 * @param {object} o
 * @param {(url: string, init: object) => Promise<Response>} o.fetchImpl
 * @param {ReturnType<typeof createTokenStore>} o.tokens
 * @param {() => string} [o.opsBaseUrl] 运维管理面地址（C-01 的 ops_base_url），空串为同源
 * @param {() => void} [o.onAuthRequired] 刷新失败、需要重新登录
 * @param {(details: object) => void} [o.onMaintenance] 收到维护模式错误
 */
export function createClient({ fetchImpl, tokens, opsBaseUrl = () => '', onAuthRequired, onMaintenance }) {
  let refreshing = null;

  async function refresh() {
    if (!tokens.refresh) return false;
    if (!refreshing) {
      refreshing = (async () => {
        try {
          const res = await fetchImpl(`${API_PREFIX}/auth/refresh`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ refresh_token: tokens.refresh }),
          });
          if (!res.ok) return false;
          tokens.save(await res.json());
          return true;
        } catch {
          return false;
        } finally {
          setTimeout(() => { refreshing = null; }, 0);
        }
      })();
    }
    return refreshing;
  }

  function url(path, query, { ops } = {}) {
    const base = ops ? `${opsBaseUrl() || ''}${API_PREFIX}/ops` : API_PREFIX;
    return `${base}${path}${buildQuery(query)}`;
  }

  function headersFor(body, extra) {
    const h = {};
    if (body !== undefined && !(body instanceof FormData)) h['Content-Type'] = 'application/json';
    if (tokens.access) h.Authorization = `Bearer ${tokens.access}`;
    return { ...h, ...(extra || {}) };
  }

  async function request(method, path, { query, body, headers, signal, ops, raw } = {}, retried = false) {
    let res;
    try {
      res = await fetchImpl(url(path, query, { ops }), {
        method,
        headers: headersFor(body, headers),
        body: body === undefined ? undefined : body instanceof FormData ? body : JSON.stringify(body),
        signal,
      });
    } catch (e) {
      if (e?.name === 'AbortError') throw e;
      throw new ApiError({ status: 0, code: 'network_error', message: '' });
    }
    if (res.ok) {
      if (raw) return res;
      if (res.status === 204) return null;
      const text = await res.text();
      return text ? JSON.parse(text) : null;
    }
    const err = await errorFromResponse(res);
    const isAuthPath = path.startsWith('/auth/');
    if (res.status === 401 && !retried && !isAuthPath && (err.code === 'token_expired' || err.code === 'unauthenticated')) {
      if (await refresh()) return request(method, path, { query, body, headers, signal, ops, raw }, true);
      if (!tokens.localMode) onAuthRequired?.(err);
    }
    if (err.isMaintenance) onMaintenance?.(err.details || {});
    throw err;
  }

  return {
    request,
    refresh,
    url,
    get: (p, o) => request('GET', p, o),
    post: (p, body, o) => request('POST', p, { ...o, body: body === undefined ? {} : body }),
    put: (p, body, o) => request('PUT', p, { ...o, body }),
    patch: (p, body, o) => request('PATCH', p, { ...o, body }),
    del: (p, body, o) => request('DELETE', p, { ...o, body }),
    // 下载链接、SSE 用查询参数带令牌（契约 1.3）
    withToken(href) {
      if (!tokens.access) return href;
      return `${href}${href.includes('?') ? '&' : '?'}token=${encodeURIComponent(tokens.access)}`;
    },
  };
}

/**
 * 游标分页（契约 1.6）：逐页取，`cursor_stale` 时从头再拉一次。
 * @param {(query: object) => Promise<{items: any[], next_cursor: string|null}>} fetchPage
 */
export async function* paginate(fetchPage, query = {}, { maxRestarts = 1 } = {}) {
  let restarts = 0;
  let cursor = null;
  const seen = new Set();
  for (;;) {
    let page;
    try {
      page = await fetchPage({ ...query, cursor: cursor || undefined });
    } catch (e) {
      if (e instanceof ApiError && e.code === 'cursor_stale' && restarts < maxRestarts) {
        restarts += 1;
        cursor = null;
        continue;
      }
      throw e;
    }
    for (const item of page.items || []) {
      const key = item?.id ?? JSON.stringify(item);
      if (seen.has(key)) continue;
      seen.add(key);
      yield item;
    }
    if (!page.next_cursor) return;
    cursor = page.next_cursor;
  }
}

export async function collectAll(fetchPage, query, opts) {
  const out = [];
  for await (const item of paginate(fetchPage, query, opts)) out.push(item);
  return out;
}

/**
 * 428 需确认（契约 2.1 confirmation_required）：先按原样调用，收到 428 时问用户，
 * 用户确认后带上确认结果再调一次；用户取消返回 null。
 * @param {(confirmation?: any) => Promise<any>} call
 * @param {(details: object) => Promise<any>} ask 返回确认令牌或 true；返回假值表示取消
 */
export async function withConfirmation(call, ask) {
  try {
    return await call(undefined);
  } catch (e) {
    if (!(e instanceof ApiError) || !e.needsConfirmation) throw e;
    const answer = await ask(e.details || {});
    if (!answer) return null;
    return call(answer);
  }
}

/**
 * 乐观并发保存（契约 1.7）：409 revision_conflict 时交给 onConflict 决定
 * 'reload' 放弃本地改动、'overwrite' 用对方的 revision 重试一次、其他值保持冲突。
 */
export async function saveWithRevision(save, baseRevision, onConflict) {
  try {
    return await save(baseRevision);
  } catch (e) {
    if (!(e instanceof ApiError) || !e.isConflict) throw e;
    const choice = await onConflict(e.details || {});
    if (choice === 'overwrite' && e.details?.actual) return save(e.details.actual);
    throw e;
  }
}
