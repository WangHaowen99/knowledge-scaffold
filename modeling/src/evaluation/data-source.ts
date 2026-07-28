export type DataSourceMode = 'connection' | 'none';
export type EvaluationDataSourceProvider = 'nl2sql_template' | 'nl2sql_http' | 'neo4j';
export type EvaluationDatabaseEngine = 'mysql' | 'postgresql';

export interface EvaluationDatabaseConfig {
  engine: EvaluationDatabaseEngine;
  host: string;
  port: number;
  database: string;
  username: string;
  password: string;
}

export type EvaluationDatabaseServerConfig = Omit<EvaluationDatabaseConfig, 'database'>;

export interface DataSourceInput {
  mode?: unknown;
  provider?: unknown;
  config?: Record<string, unknown>;
}

export interface DisabledDataSourceInput {
  mode: 'none';
}

export interface ConnectionDataSourceInput {
  mode: 'connection';
  provider: EvaluationDataSourceProvider;
  config: Record<string, unknown>;
}

export type NormalizedDataSourceInput = DisabledDataSourceInput | ConnectionDataSourceInput;

const PROVIDERS = new Set<EvaluationDataSourceProvider>([
  'nl2sql_template',
  'nl2sql_http',
  'neo4j',
]);
const DATABASE_ENGINES = new Set<EvaluationDatabaseEngine>(['mysql', 'postgresql']);
const CONSUMER_WORKSPACE_ID = /^[a-z][a-z0-9_-]*$/;
const CONSUMER_WORKSPACE_ID_MAX_LENGTH = 64;

const GRAPH_IDENTIFIER = /^[A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]*$/;

export function assertGraphIdentifier(value: string, label: string): void {
  if (!GRAPH_IDENTIFIER.test(value)) throw new Error(`${label}格式非法：${value}`);
}

const text = (value: unknown): string => String(value ?? '').trim();

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function requireText(config: Record<string, unknown>, key: string, label: string): string {
  const value = text(config[key]);
  if (!value) throw new Error(`${label}不能为空`);
  return value;
}

export function normalizeEvaluationWorkspaceId(value: unknown): string {
  const workspaceId = text(value);
  if (!workspaceId) throw new Error('评测工作空间 ID 不能为空');
  if (!CONSUMER_WORKSPACE_ID.test(workspaceId)
    || workspaceId.length > CONSUMER_WORKSPACE_ID_MAX_LENGTH) {
    throw new Error('评测工作空间 ID 格式非法：必须以英文小写字母开头，只能包含小写字母、数字、下划线或中划线，且最长 64 个字符');
  }
  return workspaceId;
}

export function normalizeEvaluationDatabaseServerConfig(
  value: unknown,
): EvaluationDatabaseServerConfig {
  const config = record(value);
  const engine = text(config.engine).toLowerCase() as EvaluationDatabaseEngine;
  if (!DATABASE_ENGINES.has(engine)) throw new Error('评测库类型仅支持 mysql 或 postgresql');
  const defaultPort = engine === 'mysql' ? 3306 : 5432;
  const port = Number(text(config.port) || defaultPort);
  if (!Number.isInteger(port) || port < 1 || port > 65_535) {
    throw new Error('评测库端口必须是 1 到 65535 之间的整数');
  }
  const password = text(config.password);
  if (!password) throw new Error('评测库 Password 每次保存并测试时都必须填写');
  return {
    engine,
    host: requireText(config, 'host', '评测库 Host'),
    port,
    username: requireText(config, 'username', '评测库 User'),
    password,
  };
}

export function normalizeEvaluationDatabaseConfig(value: unknown): EvaluationDatabaseConfig {
  const config = record(value);
  return {
    ...normalizeEvaluationDatabaseServerConfig(config),
    database: requireText(config, 'database', '评测库 Database'),
  };
}

function normalizeIsolationPair(
  config: Record<string, unknown>,
  propertyKey: string,
  valueKey: string,
  label: string,
): { property: string; value: string } | null {
  const property = text(config[propertyKey]);
  const value = text(config[valueKey]);
  if (!property && !value) return null;
  if (!property || !value) throw new Error(`${label}隔离字段和值必须同时填写`);
  assertGraphIdentifier(property, `${label}隔离字段`);
  return { property, value };
}

export function normalizeDataSourceInput(input: DataSourceInput): NormalizedDataSourceInput {
  const mode = text(input?.mode || 'connection');
  if (mode === 'none') return { mode: 'none' };
  if (mode !== 'connection') throw new Error('数据源模式仅支持 connection 或 none');

  const provider = text(input?.provider).toLowerCase() as EvaluationDataSourceProvider;
  if (!PROVIDERS.has(provider)) throw new Error('provider 仅支持 nl2sql_template、nl2sql_http 或 neo4j');
  const config = { ...(input?.config ?? {}) };

  if (provider === 'neo4j') {
    const workspace = normalizeIsolationPair(
      config,
      'workspace_property',
      'workspace_value',
      'Workspace ',
    );
    const ontology = normalizeIsolationPair(
      config,
      'ontology_property',
      'ontology_value',
      'Ontology ',
    );
    const normalized: Record<string, unknown> = {
      uri: requireText(config, 'uri', 'Neo4j URL'),
      username: requireText(config, 'username', 'Neo4j User'),
      password: text(config.password),
      database: text(config.database) || 'neo4j',
    };
    if (workspace) {
      normalized.workspace_property = workspace.property;
      normalized.workspace_value = workspace.value;
    }
    if (ontology) {
      normalized.ontology_property = ontology.property;
      normalized.ontology_value = ontology.value;
    }
    return { mode: 'connection', provider, config: normalized };
  }

  const evaluationDatabase = normalizeEvaluationDatabaseConfig(config.evaluation_database);

  return {
    mode: 'connection',
    provider,
    config: {
      url: requireText(config, 'url', '数据服务 URL').replace(/\/+$/, ''),
      token: text(config.token),
      scene: text(config.scene) || evaluationDatabase.database,
      evaluation_workspace_id: normalizeEvaluationWorkspaceId(config.evaluation_workspace_id),
      evaluation_database: evaluationDatabase,
    },
  };
}

export function redactProviderConfig(
  provider: EvaluationDataSourceProvider,
  config: Record<string, unknown>,
): Record<string, unknown> {
  const publicConfig = { ...config };
  if (provider === 'neo4j') {
    const hasPassword = Boolean(text(publicConfig.password) || publicConfig.has_password);
    delete publicConfig.password;
    delete publicConfig.has_password;
    if (hasPassword) publicConfig.has_password = true;
  } else {
    const hasToken = Boolean(text(publicConfig.token) || publicConfig.has_token);
    delete publicConfig.token;
    delete publicConfig.has_token;
    if (hasToken) publicConfig.has_token = true;

    if (publicConfig.evaluation_database) {
      const evaluationDatabase = record(publicConfig.evaluation_database);
      const hasDatabasePassword = Boolean(
        text(evaluationDatabase.password) || evaluationDatabase.has_password,
      );
      delete evaluationDatabase.password;
      delete evaluationDatabase.has_password;
      if (hasDatabasePassword) evaluationDatabase.has_password = true;
      publicConfig.evaluation_database = evaluationDatabase;
    }
  }
  return publicConfig;
}
