import { consumerFetch, provisionShadowWorkspace } from '../consumer/client.js';
import { normalizeEvaluationWorkspaceId } from './data-source.js';

export type ConsumerRequest = (
  token: string,
  path: string,
  options?: RequestInit & { workspaceId?: string },
) => Promise<Response>;

interface EvaluationWorkspaceSource {
  mode: 'connection' | 'none';
  provider?: string;
  config?: Record<string, unknown>;
}

export function shadowWorkspaceId(projectId: string): string {
  return `mw-shadow-${projectId.replace(/[^a-zA-Z0-9-]/g, '-')}`.slice(0, 60);
}

export function evaluationWorkspaceId(
  projectId: string,
  source: EvaluationWorkspaceSource,
): string {
  if (source.mode === 'connection' && source.provider === 'neo4j') {
    const value = String(source.config?.workspace_value ?? '').trim();
    if (!value) {
      throw new Error('Neo4j Workspace 隔离值不能为空，因为该值同时作为评测工作空间 ID');
    }
    return normalizeEvaluationWorkspaceId(value);
  }
  if (source.mode === 'connection'
    && (source.provider === 'nl2sql_template' || source.provider === 'nl2sql_http')) {
    return normalizeEvaluationWorkspaceId(source.config?.evaluation_workspace_id);
  }
  return shadowWorkspaceId(projectId);
}

async function responseJson(response: Response, action: string): Promise<any> {
  const data = await response.json().catch(() => ({})) as any;
  if (!response.ok) {
    const detail = data.error || data.detail || `${response.status}`;
    throw new Error(`${action}失败：${detail}`);
  }
  return data;
}

export async function ensureEvaluationWorkspace({
  projectId,
  workspaceId,
  owner,
  token,
  signal,
  request = consumerFetch,
}: {
  projectId: string;
  workspaceId?: string;
  owner?: string;
  token: string;
  signal?: AbortSignal;
  request?: ConsumerRequest;
}): Promise<{ id: string; created: boolean }> {
  const defaultId = shadowWorkspaceId(projectId);
  const id = workspaceId ? normalizeEvaluationWorkspaceId(workspaceId) : defaultId;
  if (id === defaultId && owner && request === consumerFetch) {
    const provisioned = await provisionShadowWorkspace(projectId, owner, { signal });
    if (provisioned.workspace.id !== id || provisioned.workspace.owner !== owner) {
      throw new Error('影子工作区 provisioning 返回了不匹配的 id 或 owner');
    }
    return { id, created: provisioned.created };
  }
  const list = await responseJson(
    await request(token, '/agentic/workspaces', { signal }),
    '读取评测工作空间',
  );
  if ((list.workspaces ?? []).some((workspace: any) => workspace.id === id)) {
    return { id, created: false };
  }

  const createResponse = await request(token, '/agentic/workspaces', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      id,
      name: id === defaultId ? `建模影子·${projectId}` : id,
      description: `知识包建模工具评测使用，来源项目 ${projectId}`,
    }),
    signal,
  });
  if (!createResponse.ok && createResponse.status === 400) {
    const detail = await createResponse.clone().json().catch(() => ({})) as any;
    if (String(detail.error || detail.detail || '').includes('已存在')) {
      throw new Error(`评测工作空间 ${id} 已存在，但当前用户不可访问`);
    }
  }
  await responseJson(createResponse, '创建评测工作空间');
  return { id, created: true };
}

export const ensureShadowWorkspace = ensureEvaluationWorkspace;
