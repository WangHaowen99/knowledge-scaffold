import { consumerFetch } from '../consumer/client.js';
import type { ProjectDataSource } from './data-source-repo.js';
import type { ConsumerRequest } from './shadow-workspace.js';

async function responseJson(response: Response, action: string): Promise<any> {
  const data = await response.json().catch(() => ({})) as any;
  if (!response.ok) {
    const detail = data.error || data.detail || `${response.status}`;
    throw new Error(`${action}失败：${detail}`);
  }
  return data;
}

export async function prepareProjectDataSourceForEvaluation({
  token,
  ontologyId,
  dataSource,
  request = consumerFetch,
}: {
  token: string;
  ontologyId: string;
  dataSource: ProjectDataSource;
  request?: ConsumerRequest;
}): Promise<void> {
  const dataSourcePath = `/agentic/ontologies/${encodeURIComponent(ontologyId)}/data-source`;
  if (dataSource.mode === 'none') {
    await responseJson(await request(token, dataSourcePath, {
      method: 'DELETE',
      workspaceId: dataSource.workspaceId,
    }), '清除本体数据源配置');
    return;
  }

  if (dataSource.ontologyId !== ontologyId) {
    throw new Error('本体数据源与当前知识包不匹配，请重新保存数据源配置');
  }
  const current = await responseJson(
    await request(token, dataSourcePath, { workspaceId: dataSource.workspaceId }),
    '读取本体数据源',
  );
  if (!current.configured || current.source?.mode !== 'direct_provider') {
    throw new Error('已配置的本体数据源不存在，请返回“数据源配置”重新保存');
  }
  if (current.source?.provider !== dataSource.provider) {
    throw new Error('本体数据源 Provider 已变化，请返回“数据源配置”重新保存');
  }
}
