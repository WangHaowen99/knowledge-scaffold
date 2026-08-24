import { Agent, fetch as undiciFetch } from 'undici';
import { config } from '../config.js';
import { getConsumerApiUrl } from '../settings/repo.js';

const DEFAULT_CONSUMER_LONG_REQUEST_TIMEOUT_MS = 45 * 60_000;
const MIN_CONSUMER_LONG_REQUEST_TIMEOUT_MS = 60_000;
const MAX_CONSUMER_LONG_REQUEST_TIMEOUT_MS = 2 * 60 * 60_000;

export function resolveConsumerLongRequestTimeoutMs(raw: string | undefined): number {
  const parsed = Number(raw);
  if (!Number.isFinite(parsed) || parsed <= 0) return DEFAULT_CONSUMER_LONG_REQUEST_TIMEOUT_MS;
  return Math.min(
    MAX_CONSUMER_LONG_REQUEST_TIMEOUT_MS,
    Math.max(MIN_CONSUMER_LONG_REQUEST_TIMEOUT_MS, Math.trunc(parsed)),
  );
}

export function describeConsumerRequestError(error: unknown): string {
  if (!(error instanceof Error)) return String(error);
  const cause = (error as Error & { cause?: unknown }).cause;
  if (!(cause instanceof Error)) return error.message;
  const code = String((cause as Error & { code?: unknown }).code || '').trim();
  const detail = [code, cause.message].filter(Boolean).join('：');
  return detail ? `${error.message}（${detail}）` : error.message;
}

// 评测单用例的 runs/sync 可能持续数十分钟（复杂业务题多轮工具循环）。超时由部署环境
// 配置，默认 45 分钟。npm undici 的 Agent 不能传给 Node 内置 fetch（版本不匹配会
// fetch failed），必须配套使用 undici 自己的 fetch。
const longRequestTimeoutMs = resolveConsumerLongRequestTimeoutMs(
  process.env.MODELING_CONSUMER_REQUEST_TIMEOUT_MS,
);
const longDispatcher = new Agent({
  headersTimeout: longRequestTimeoutMs,
  bodyTimeout: longRequestTimeoutMs,
});

// consumer API 客户端（M3 评测闭环的地基）。
// 地址来自页面设置（settings 表），专家 token 透传——以专家身份调用。
export interface ConsumerConnectivity {
  url: string;
  reachable: boolean; // /health 可达
  authOk: boolean; // /auth/me 以当前 token 通过
  modelReady?: boolean; // /api/system-config/readiness 的模型就绪状态
  detail?: string;
}

export interface ShadowWorkspaceProvisionResult {
  created: boolean;
  workspace: { id: string; owner: string | null };
}

interface ShadowWorkspaceProvisionOptions {
  baseUrl?: string;
  serviceToken?: string;
  modelProfileRef?: string;
  managedModelProfile?: {
    provider: string;
    baseUrl: string;
    model: string;
    apiKey: string;
    timeout: number;
    maxTokens: number;
    temperature: number;
  };
  signal?: AbortSignal;
}

export interface ModelingServiceRequestOptions {
  baseUrl?: string;
  serviceToken?: string;
}

export async function modelingServiceFetch(
  path: string,
  options: RequestInit & { workspaceId?: string } = {},
  serviceOptions: ModelingServiceRequestOptions = {},
): Promise<Response> {
  const baseUrl = (serviceOptions.baseUrl ?? config.consumerApiUrl).replace(/\/+$/, '');
  const serviceToken = String(serviceOptions.serviceToken ?? config.modelingServiceToken).trim();
  if (serviceToken.length < 32) {
    throw new Error('缺少有效的 MODELING_SERVICE_TOKEN（至少 32 个字符）');
  }
  const { workspaceId, ...init } = options;
  const headers: Record<string, string> = {
    Authorization: `Bearer ${serviceToken}`,
    ...((init.headers as Record<string, string>) ?? {}),
  };
  if (workspaceId) headers['X-Workspace-Id'] = encodeURIComponent(workspaceId);
  try {
    return await undiciFetch(`${baseUrl}${path}`, {
      ...(init as any),
      headers,
      dispatcher: longDispatcher,
    }) as unknown as Response;
  } catch (error) {
    throw new Error(`Consumer 请求失败：${describeConsumerRequestError(error)}`, { cause: error });
  }
}

/**
 * Privileged Modeling calls deliberately use the deployment-fixed
 * CONSUMER_API_URL instead of the page-overridable evaluation URL, so the
 * service credential cannot be sent to an arbitrary host configured from UI.
 */
export async function provisionShadowWorkspace(
  projectId: string,
  owner: string,
  options: ShadowWorkspaceProvisionOptions = {},
): Promise<ShadowWorkspaceProvisionResult> {
  const { signal, modelProfileRef, managedModelProfile, ...serviceOptions } = options;
  const response = await modelingServiceFetch(
    '/agentic/internal/modeling/shadow-workspaces/ensure',
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        project_id: projectId,
        owner,
        ...(modelProfileRef ? { model_profile_ref: modelProfileRef } : {}),
        ...(managedModelProfile ? {
          managed_model_profile: {
            provider: managedModelProfile.provider,
            base_url: managedModelProfile.baseUrl,
            model: managedModelProfile.model,
            api_key: managedModelProfile.apiKey,
            timeout: managedModelProfile.timeout,
            max_tokens: managedModelProfile.maxTokens,
            temperature: managedModelProfile.temperature,
          },
        } : {}),
      }),
      signal,
    },
    serviceOptions,
  );
  const data = (await response.json().catch(() => ({}))) as any;
  if (!response.ok) {
    throw new Error(`创建影子工作区失败（${response.status}）：${data.error || data.detail || ''}`);
  }
  return data as ShadowWorkspaceProvisionResult;
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

// 统一的 consumer 请求封装：透传专家 token + 可选评测工作空间头（M3 评测使用）。
export async function consumerFetch(token: string, path: string, options: RequestInit & { workspaceId?: string } = {}): Promise<Response> {
  const { workspaceId, ...init } = options;
  const headers: Record<string, string> = {
    Authorization: `Bearer ${token}`,
    ...((init.headers as Record<string, string>) ?? {}),
  };
  if (workspaceId) headers['X-Workspace-Id'] = encodeURIComponent(workspaceId);
  try {
    return await undiciFetch(`${getConsumerApiUrl()}${path}`, {
      ...(init as any), headers, dispatcher: longDispatcher,
    }) as unknown as Response;
  } catch (error) {
    throw new Error(`Consumer 请求失败：${describeConsumerRequestError(error)}`, { cause: error });
  }
}
