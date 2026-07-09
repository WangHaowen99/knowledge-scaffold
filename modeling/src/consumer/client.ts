import { getConsumerApiUrl } from '../settings/repo.js';

// consumer API 客户端（M3 评测闭环的地基）。
// 地址来自页面设置（settings 表），专家 token 透传——以专家身份调用。
export interface ConsumerConnectivity {
  url: string;
  reachable: boolean; // /health 可达
  authOk: boolean; // /auth/me 以当前 token 通过
  modelReady?: boolean; // /api/system-config/readiness 的模型就绪状态
  detail?: string;
}

export async function testConsumerConnectivity(token: string, urlOverride?: string): Promise<ConsumerConnectivity> {
  const url = (urlOverride || getConsumerApiUrl()).replace(/\/+$/, '');
  const out: ConsumerConnectivity = { url, reachable: false, authOk: false };
  try {
    const h = await fetch(`${url}/health`, { signal: AbortSignal.timeout(5000) });
    out.reachable = h.ok;
  } catch (e) {
    out.detail = `health 不可达：${e instanceof Error ? e.message : e}`;
    return out;
  }
  try {
    const me = await fetch(`${url}/auth/me`, {
      headers: { Authorization: `Bearer ${token}` },
      signal: AbortSignal.timeout(5000),
    });
    out.authOk = me.ok;
    if (!me.ok) out.detail = `auth/me 返回 ${me.status}（该 consumer 与登录 issuer 可能不是同一账号体系）`;
  } catch (e) {
    out.detail = `auth/me 失败：${e instanceof Error ? e.message : e}`;
  }
  try {
    const r = await fetch(`${url}/api/system-config/readiness`, {
      headers: { Authorization: `Bearer ${token}` },
      signal: AbortSignal.timeout(5000),
    });
    if (r.ok) {
      const data = (await r.json()) as { ready?: boolean; model?: { ready?: boolean } };
      out.modelReady = data.model?.ready ?? data.ready;
    }
  } catch { /* readiness 拿不到不算失败 */ }
  return out;
}

// 统一的 consumer 请求封装：透传专家 token + 可选影子工作区头（M3 评测使用）。
export async function consumerFetch(token: string, path: string, options: RequestInit & { workspaceId?: string } = {}): Promise<Response> {
  const { workspaceId, ...init } = options;
  const headers: Record<string, string> = {
    Authorization: `Bearer ${token}`,
    ...((init.headers as Record<string, string>) ?? {}),
  };
  if (workspaceId) headers['X-Workspace-Id'] = encodeURIComponent(workspaceId);
  return fetch(`${getConsumerApiUrl()}${path}`, { ...init, headers });
}
