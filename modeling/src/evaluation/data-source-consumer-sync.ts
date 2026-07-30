import { FormData } from 'undici';
import { consumerFetch } from '../consumer/client.js';
import { latestPackageVersion } from '../workflow/m2-repo.js';
import { renderPackage } from '../workflow/package-render.js';
import { zipPackage } from '../workflow/export.js';
import type { ConnectionDataSourceInput } from './data-source.js';
import { ensureEvaluationWorkspace, type ConsumerRequest } from './shadow-workspace.js';

export interface ConsumerDataSourceReference {
  workspaceId: string;
  ontologyId: string;
}

export interface SyncProjectDataSourceOptions {
  projectId: string;
  workspaceId: string;
  ontologyId: string;
  token: string;
  source: ConnectionDataSourceInput;
  request?: ConsumerRequest;
}

export type DataSourceConsumerSync = (
  options: Omit<SyncProjectDataSourceOptions, 'request'>,
) => Promise<ConsumerDataSourceReference>;

function consumerProviderConfig(source: ConnectionDataSourceInput): Record<string, unknown> {
  const config = { ...source.config };
  if (source.provider === 'neo4j') {
    delete config.workspace_value;
    delete config.ontology_value;
  } else {
    delete config.evaluation_workspace_id;
  }
  return config;
}

async function readResponse(
  responsePromise: Promise<Response>,
  action: string,
): Promise<{ response: Response; data: any }> {
  let response: Response;
  try {
    response = await responsePromise;
  } catch (error) {
    throw new Error(`${action}失败：${error instanceof Error ? error.message : error}`);
  }
  return { response, data: await response.json().catch(() => ({})) as any };
}

async function responseJson(responsePromise: Promise<Response>, action: string): Promise<any> {
  const { response, data } = await readResponse(responsePromise, action);
  if (!response.ok) throw new Error(`${action}失败：${data.error || data.detail || response.status}`);
  return data;
}

export async function importPackageZip({
  token,
  workspaceId,
  ontologyId,
  zip,
  request = consumerFetch,
}: {
  token: string;
  workspaceId: string;
  ontologyId: string;
  zip: Uint8Array;
  request?: ConsumerRequest;
}): Promise<any> {
  const attempt = (action?: string) => {
    const form = new FormData();
    form.append('file', new Blob([Buffer.from(zip)], { type: 'application/zip' }), `${ontologyId}.zip`);
    if (action) form.append('action', action);
    return request(token, '/agentic/ontologies/import-zip', {
      method: 'POST',
      workspaceId,
      body: form as any,
    });
  };

  let { response, data } = await readResponse(attempt(), '导入知识包');
  if (response.status === 409 && data.requires_confirmation) {
    ({ response, data } = await readResponse(attempt('overwrite_version'), '覆盖知识包版本'));
  }
  if (!response.ok) {
    throw new Error(`导入知识包失败：${data.error || data.detail || response.status}`);
  }
  return data;
}

async function currentPackageZip(projectId: string, ontologyId: string): Promise<Uint8Array> {
  const latest = latestPackageVersion(projectId);
  if (!latest) throw new Error('同步本体数据源失败：尚未生成知识包');
  if (String(latest.spec.id ?? '').trim() !== ontologyId) {
    throw new Error('同步本体数据源失败：知识包 ID 已变化，请重新保存数据源配置');
  }
  const files = renderPackage(latest.spec);
  return zipPackage(files);
}

export async function syncProjectDataSourceToConsumer(
  options: SyncProjectDataSourceOptions,
): Promise<ConsumerDataSourceReference> {
  const {
    projectId,
    workspaceId,
    ontologyId,
    token,
    source,
    request = consumerFetch,
  } = options;
  const workspace = await ensureEvaluationWorkspace({
    projectId,
    workspaceId,
    token,
    request,
  }).catch((error) => {
    throw new Error(`同步评测工作空间失败：${error instanceof Error ? error.message : error}`);
  });

  const zip = await currentPackageZip(projectId, ontologyId);
  const imported = await importPackageZip({
    token,
    workspaceId: workspace.id,
    ontologyId,
    zip,
    request,
  });
  const importedOntologyId = String(imported.ontology_id ?? '').trim();
  if (importedOntologyId !== ontologyId) {
    throw new Error(`导入知识包失败：返回本体 ID ${importedOntologyId || '为空'}，预期 ${ontologyId}`);
  }

  await responseJson(request(token, `/agentic/ontologies/${encodeURIComponent(ontologyId)}/data-source`, {
    method: 'PUT',
    workspaceId: workspace.id,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      mode: 'direct_provider',
      provider: source.provider,
      config: consumerProviderConfig(source),
    }),
  }), '保存本体数据源');
  return { workspaceId: workspace.id, ontologyId };
}
