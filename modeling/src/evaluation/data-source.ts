export type DataSourceMode = 'connection' | 'none';
export type EvaluationDataSourceProvider = 'nl2sql_template' | 'nl2sql_http' | 'neo4j';

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

const GRAPH_IDENTIFIER = /^[A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]*$/;

export function assertGraphIdentifier(value: string, label: string): void {
  if (!GRAPH_IDENTIFIER.test(value)) throw new Error(`${label}格式非法：${value}`);
}

const text = (value: unknown): string => String(value ?? '').trim();

function requireText(config: Record<string, unknown>, key: string, label: string): string {
  const value = text(config[key]);
  if (!value) throw new Error(`${label}不能为空`);
  return value;
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

  return {
    mode: 'connection',
    provider,
    config: {
      url: requireText(config, 'url', '数据服务 URL').replace(/\/+$/, ''),
      token: text(config.token),
      scene: text(config.scene),
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
  }
  return publicConfig;
}
