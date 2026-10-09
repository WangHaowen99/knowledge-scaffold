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
import { getPackageArtifactStore } from '../artifacts/index.js';
import {
  FieldNameConflictError,
  fieldNameConflictDetails,
  FIELD_ALIAS_CONFLICT_CODE,
  findFieldNameConflicts,
} from '../workflow/field-alias.js';

export class DataSourceConfigurationError extends Error {
  constructor(
    readonly code: string,
    message: string,
    readonly statusCode: 400 | 409 | 422 | 502,
    readonly cause?: unknown,
    readonly details: Record<string, unknown> = {},
  ) {
    super(message);
    this.name = 'DataSourceConfigurationError';
  }
}

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
  let normalized: ReturnType<typeof normalizeDataSourceInput>;
  let workspaceId: string;
  try {
    normalized = normalizeDataSourceInput(input);
    workspaceId = evaluationWorkspaceId(projectId, normalized);
  } catch (error) {
    throw new DataSourceConfigurationError(
      'DATA_SOURCE_INVALID_INPUT',
      error instanceof Error ? error.message : '数据源配置不正确',
      400,
      error,
    );
  }
  if (normalized.mode === 'none') {
    // If a package already exists, reject deterministic package conflicts even
    // for no-database evaluations; the package is imported when the round starts.
    const current = await getPackageArtifactStore().current(projectId);
    if (current) {
      const conflicts = findFieldNameConflicts(current.spec.objects ?? []);
      if (conflicts.length) {
        throw new DataSourceConfigurationError(
          FIELD_ALIAS_CONFLICT_CODE,
          new FieldNameConflictError(conflicts).message,
          422,
          undefined,
          fieldNameConflictDetails(conflicts),
        );
      }
    }
    return saveProjectDataSource({
      projectId,
      mode: 'none',
      workspaceId,
    });
  }

  const current = await getPackageArtifactStore().current(projectId);
  if (!current) {
    throw new DataSourceConfigurationError(
      'DATA_SOURCE_PACKAGE_REQUIRED',
      '尚未生成知识包，请先完成知识包生成',
      409,
    );
  }
  const ontologyId = String(current.spec.id ?? '').trim();
  const packageConflicts = findFieldNameConflicts(current.spec.objects ?? []);
  if (packageConflicts.length) {
    throw new DataSourceConfigurationError(
      FIELD_ALIAS_CONFLICT_CODE,
      new FieldNameConflictError(packageConflicts).message,
      422,
      undefined,
      fieldNameConflictDetails(packageConflicts),
    );
  }
  try {
    assertEvaluationGraphScope(ontologyId, normalized);
  } catch (error) {
    throw new DataSourceConfigurationError(
      'DATA_SOURCE_INVALID_INPUT',
      error instanceof Error ? error.message : '数据源隔离配置不正确',
      400,
      error,
    );
  }
  try {
    await testConnection(normalized);
  } catch (error) {
    if (error instanceof FieldNameConflictError) {
      throw new DataSourceConfigurationError(
        FIELD_ALIAS_CONFLICT_CODE,
        error.message,
        422,
        error,
        fieldNameConflictDetails(error.conflicts),
      );
    }
    if (error instanceof DataSourceConfigurationError) throw error;
    throw new DataSourceConfigurationError(
      'DATA_SOURCE_CONNECTION_FAILED',
      '数据源连接测试失败，请检查连接配置和上游服务状态',
      502,
      error,
    );
  }
  const syncOptions = {
    projectId,
    workspaceId,
    ontologyId,
    token,
    source: normalized,
    ...(owner ? { owner } : {}),
  };
  let reference: Awaited<ReturnType<DataSourceConsumerSync>>;
  try {
    reference = await syncConnection(syncOptions);
  } catch (error) {
    if (error instanceof FieldNameConflictError) {
      throw new DataSourceConfigurationError(
        FIELD_ALIAS_CONFLICT_CODE,
        error.message,
        422,
        error,
        fieldNameConflictDetails(error.conflicts),
      );
    }
    if (error instanceof DataSourceConfigurationError) throw error;
    throw new DataSourceConfigurationError(
      'DATA_SOURCE_SYNC_FAILED',
      '数据源暂时无法同步到运行服务，请稍后重试',
      502,
      error,
    );
  }
  return saveProjectDataSource({
    projectId,
    mode: 'connection',
    provider: normalized.provider,
    workspaceId: reference.workspaceId,
    ontologyId: reference.ontologyId,
    config: normalized.config,
  });
}
