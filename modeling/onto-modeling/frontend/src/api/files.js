// 保留的 drawio 预览与编辑组件用的文件接口。
// 应用启动时由 services 绑定到应用的传输层与客户端（bindFileTransport），这样 mock、令牌刷新、
// 统一错误体都和其他接口一致；没绑定时（单测、独立页面）退回浏览器 fetch。
import { API_PREFIX, buildQuery, createTokenStore } from './client.js';
import { ApiError, errorFromResponse } from './errors.js';

let bound = null; // { client, fetch, tokens }
const fallbackTokens = createTokenStore(typeof window !== 'undefined' ? window.localStorage : null);
const tokens = () => bound?.tokens || fallbackTokens;

export function bindFileTransport(next) {
  bound = next;
}

function parseBody(body) {
  if (typeof body !== 'string') return body;
  try { return JSON.parse(body); } catch { return body; }
}

/**
 * 旧调用约定：request(path, {method, body, headers, signal})，path 是完整路径。
 * v1 路径走应用客户端（401 时刷新令牌再试一次）；其余路径照原样经传输层发出。
 */
export async function request(path, options = {}) {
  const method = (options.method || 'GET').toUpperCase();
  if (bound?.client && path.startsWith(`${API_PREFIX}/`)) {
    const body = options.body === undefined ? undefined : parseBody(options.body);
    return bound.client.request(method, path.slice(API_PREFIX.length), { body, headers: options.headers, signal: options.signal });
  }
  const headers = { ...(options.body && !(options.body instanceof FormData) ? { 'Content-Type': 'application/json' } : {}), ...(options.headers || {}) };
  if (tokens().access) headers.Authorization = `Bearer ${tokens().access}`;
  const doFetch = bound?.fetch || ((url, init) => fetch(url, init));
  let res;
  try {
    res = await doFetch(path, { ...options, method, headers });
  } catch (e) {
    if (e?.name === 'AbortError') throw e;
    throw new ApiError({ status: 0, code: 'network_error' });
  }
  if (!res.ok) throw await errorFromResponse(res);
  if (res.status === 204) return null;
  const text = await res.text();
  return text ? JSON.parse(text) : null;
}

const P = (pid) => `${API_PREFIX}/projects/${encodeURIComponent(pid)}`;

export const api = {
  // F-02 读文本文件
  readFile: (pid, path) => request(`${P(pid)}/files/content${buildQuery({ path })}`),
  // F-03 写文本文件（经写入路径提交）
  writeFile: (pid, path, content, baseRevision = '') => request(`${P(pid)}/files/content`, {
    method: 'PUT',
    body: JSON.stringify({ path, content, base_revision: baseRevision }),
  }),
  // F-04 下载原文件（链接带 token）
  rawUrl: (pid, path) => `${P(pid)}/files/raw${buildQuery({ path, token: tokens().access || undefined })}`,
};
