import {
  normalizeDataSourceInput,
  type ConnectionDataSourceInput,
  type DataSourceInput,
} from './data-source.js';
import {
  testProjectDataSourceConnection,
  type DataSourceConnectionTester,
} from './data-source-connection-test.js';
import {
  saveProjectDataSource,
  type ProjectDataSource,
} from './data-source-repo.js';
import {
  syncProjectDataSourceToConsumer,
  type DataSourceConsumerSync,
} from './data-source-consumer-sync.js';
import { evaluationWorkspaceId } from './shadow-workspace.js';
import { latestPackageVersion } from '../workflow/m2-repo.js';

function assertEvaluationGraphScope(
  ontologyId: string,
  source: ConnectionDataSourceInput,
): void {
  if (source.provider !== 'neo4j') return;
  const ontologyProperty = String(source.config.ontology_property ?? '').trim();
  if (ontologyProperty) {
    const expectedOntology = ontologyId;
    const actualOntology = String(source.config.ontology_value ?? '').trim();
    if (actualOntology !== expectedOntology) {
      throw new Error(`Neo4j Ontology 隔离值必须为 ${expectedOntology}，以匹配当前知识包 ID`);
    }
  }
}

export async function configureProjectDataSource({
  projectId,
  owner,
  token,
  input,
  testConnection = testProjectDataSourceConnection,
  syncConnection = syncProjectDataSourceToConsumer,
}: {
  projectId: string;
  owner?: string;
  token: string;
  input: DataSourceInput;
  testConnection?: DataSourceConnectionTester;
  syncConnection?: DataSourceConsumerSync;
}): Promise<ProjectDataSource> {
  const normalized = normalizeDataSourceInput(input);
  const workspaceId = evaluationWorkspaceId(projectId, normalized);
  if (normalized.mode === 'none') {
    return saveProjectDataSource({
      projectId,
      mode: 'none',
      workspaceId,
    });
  }

  const latest = latestPackageVersion(projectId);
  if (!latest) throw new Error('尚未生成知识包，请先完成知识包生成');
  const ontologyId = String(latest.spec.id ?? '').trim();
  assertEvaluationGraphScope(ontologyId, normalized);
  await testConnection(normalized);
  const syncOptions = {
    projectId,
    workspaceId,
    ontologyId,
    token,
    source: normalized,
    ...(owner ? { owner } : {}),
  };
  const reference = await syncConnection(syncOptions);
  return saveProjectDataSource({
    projectId,
    mode: 'connection',
    provider: normalized.provider,
    workspaceId: reference.workspaceId,
    ontologyId: reference.ontologyId,
    config: normalized.config,
  });
}
