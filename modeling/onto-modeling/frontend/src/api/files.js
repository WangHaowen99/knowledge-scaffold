// 保留的预览 / 编辑组件（drawio、Excel、HTML、流程图编辑器、Studio 图谱）用的文件接口。
// 这些组件在 F2 接进资料区、Studio 和流程图弹窗；接线时改用应用的 services，这里只做 v1 路径的转接。
import { API_PREFIX, buildQuery, createTokenStore } from './client.js';
import { ApiError, errorFromResponse } from './errors.js';

const tokens = createTokenStore(typeof window !== 'undefined' ? window.localStorage : null);

export async function request(path, options = {}) {
  const headers = { ...(options.body && !(options.body instanceof FormData) ? { 'Content-Type': 'application/json' } : {}), ...(options.headers || {}) };
  if (tokens.access) headers.Authorization = `Bearer ${tokens.access}`;
  let res;
  try {
    res = await fetch(path, { ...options, headers });
  } catch {
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
  rawUrl: (pid, path) => `${P(pid)}/files/raw${buildQuery({ path, token: tokens.access || undefined })}`,
};
