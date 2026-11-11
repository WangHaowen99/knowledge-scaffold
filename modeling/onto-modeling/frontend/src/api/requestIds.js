// 发布到 Hub、从 Hub 导入的幂等键（ADR-0091）：同一意图在拿到明确结果前重试都用同一个 request_id，
// 响应丢了再点一次也只得到同一个版本、同一个项目。键存 localStorage，刷新页面后照样接上。
import { ApiError } from './errors.js';

const PREFIX = 'onto.request.';

function storage() {
  try {
    return globalThis.localStorage || null;
  } catch {
    return null;
  }
}

// 32 位十六进制：只用 getRandomValues（老浏览器、非安全上下文没有 randomUUID）
export function newRequestId() {
  const bytes = new Uint8Array(16);
  globalThis.crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
}

// 服务端已经给了明确结论（4xx）就换新键；网络错误、5xx 结果未知，留着重试
const settled = (e) => e instanceof ApiError && e.status >= 400 && e.status < 500;

export async function withRequestId(intent, send) {
  const store = storage();
  const key = PREFIX + intent;
  let id = store?.getItem(key) || '';
  if (!id) {
    id = newRequestId();
    store?.setItem(key, id);
  }
  try {
    const result = await send(id);
    store?.removeItem(key);
    return result;
  } catch (e) {
    if (settled(e)) store?.removeItem(key);
    throw e;
  }
}
