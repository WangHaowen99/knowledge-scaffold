import { consumerFetch } from '../consumer/client.js';

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

const CONSUMER_WORKSPACE_ID = /^[a-z][a-z0-9_-]*$/;
const CONSUMER_WORKSPACE_ID_MAX_LENGTH = 64;

export function shadowWorkspaceId(projectId: string): string {
  return `mw-shadow-${projectId.replace(/[^a-zA-Z0-9-]/g, '-')}`.slice(0, 60);
}

function assertConsumerWorkspaceId(value: unknown): string {
  const id = String(value ?? '').trim();
  if (!id) {
    throw new Error('Neo4j Workspace 隔离值不能为空，因为该值同时作为评测工作空间 ID');
  }
  if (!CONSUMER_WORKSPACE_ID.test(id) || id.length > CONSUMER_WORKSPACE_ID_MAX_LENGTH) {
    throw new Error('评测工作空间 ID 格式非法：必须以英文小写字母开头，只能包含小写字母、数字、下划线或中划线，且最长 64 个字符');
  }
  return id;
}

export function evaluationWorkspaceId(
  projectId: string,
  source: EvaluationWorkspaceSource,
): string {
  if (source.mode === 'connection' && source.provider === 'neo4j') {
    return assertConsumerWorkspaceId(source.config?.workspace_value);
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
  token,
  request = consumerFetch,
}: {
  projectId: string;
  workspaceId?: string;
  token: string;
  request?: ConsumerRequest;
}): Promise<{ id: string; created: boolean }> {
  const defaultId = shadowWorkspaceId(projectId);
  const id = workspaceId ? assertConsumerWorkspaceId(workspaceId) : defaultId;
  const list = await responseJson(
    await request(token, '/agentic/workspaces'),
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
