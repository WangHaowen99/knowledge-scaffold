export const PROVIDER_OPTIONS = [
  { value: 'nl2sql_template', label: 'NL2SQL Template' },
  { value: 'nl2sql_http', label: 'NL2SQL HTTP' },
  { value: 'neo4j', label: 'Neo4j' },
];
export const EVALUATION_DATABASE_OPTIONS = [
  { value: 'mysql', label: 'MySQL', port: '3306' },
  { value: 'postgresql', label: 'PostgreSQL', port: '5432' },
];

const CONSUMER_WORKSPACE_ID = /^[a-z][a-z0-9_-]*$/;
const CONSUMER_WORKSPACE_ID_MAX_LENGTH = 64;

export function projectEvaluationDatabaseName(projectId) {
  const normalizedProjectId = String(projectId || '').trim().replace(/[^a-zA-Z0-9_]/g, '_');
  return `mv_shadow_${normalizedProjectId}`.slice(0, 60);
}

function sceneUsesEvaluationDatabase(form) {
  const database = String(form.eval_db_database || '').trim();
  return database && String(form.scene || '').trim() === database;
}

export function switchEvaluationDatabaseEngine(form, engine) {
  const option = EVALUATION_DATABASE_OPTIONS.find((candidate) => candidate.value === engine);
  return {
    ...form,
    eval_db_engine: engine,
    eval_db_port: option?.port || '',
    eval_db_database: '',
    scene: sceneUsesEvaluationDatabase(form) ? '' : form.scene,
  };
}

export function changeEvaluationDatabaseName(form, database) {
  return {
    ...form,
    eval_db_database: database,
    scene: sceneUsesEvaluationDatabase(form) ? '' : form.scene,
  };
}

export function applyCreatedEvaluationDatabaseName(form, database) {
  const next = changeEvaluationDatabaseName(form, database);
  return {
    ...next,
    scene: String(next.scene || '').trim() || database,
  };
}

export function emptyDataSourceForm(defaults = {}) {
  return {
    mode: 'connection',
    provider: defaults.provider || 'nl2sql_template',
    url: defaults.url || '',
    token: defaults.token || '',
    scene: defaults.scene || '',
    evaluation_workspace_id: '',
    eval_db_engine: 'mysql',
    eval_db_host: '',
    eval_db_port: '3306',
    eval_db_database: '',
    eval_db_username: '',
    eval_db_password: '',
    eval_db_has_password: false,
    uri: '',
    username: '',
    password: '',
    database: 'neo4j',
    workspace_property: 'd2o_workspace_id',
    workspace_value: '',
    ontology_property: 'd2o_ontology_id',
    ontology_value: '',
    has_password: false,
    has_token: Boolean(defaults.token),
  };
}

export function dataSourceToForm(dataSource, defaults = {}) {
  const form = emptyDataSourceForm(defaults);
  if (!dataSource) return form;
  const config = dataSource.config || {};
  const evaluationDatabase = config.evaluation_database || {};
  return {
    ...form,
    mode: dataSource.mode || 'connection',
    provider: dataSource.provider || form.provider,
    ...config,
    eval_db_engine: evaluationDatabase.engine || 'mysql',
    eval_db_host: evaluationDatabase.host || '',
    eval_db_port: String(evaluationDatabase.port || (evaluationDatabase.engine === 'postgresql' ? 5432 : 3306)),
    eval_db_database: evaluationDatabase.database || '',
    eval_db_username: evaluationDatabase.username || '',
    eval_db_password: '',
    eval_db_has_password: Boolean(evaluationDatabase.has_password),
    token: '',
    password: '',
    has_token: Boolean(config.has_token),
    has_password: Boolean(config.has_password),
  };
}

export function dataSourceFormPayload(form) {
  if (form.mode === 'none') return { mode: 'none' };
  if (form.provider === 'neo4j') {
    return {
      mode: 'connection',
      provider: 'neo4j',
      config: {
        uri: form.uri,
        username: form.username,
        password: form.password,
        database: form.database,
        workspace_property: form.workspace_property,
        workspace_value: form.workspace_value,
        ontology_property: form.ontology_property,
        ontology_value: form.ontology_value,
      },
    };
  }
  return {
    mode: 'connection',
    provider: form.provider,
    config: {
      url: form.url,
      token: form.token,
      scene: form.scene,
      evaluation_workspace_id: form.evaluation_workspace_id,
      evaluation_database: {
        engine: form.eval_db_engine,
        host: form.eval_db_host,
        port: form.eval_db_port,
        database: form.eval_db_database,
        username: form.eval_db_username,
        password: form.eval_db_password,
      },
    },
  };
}

function evaluationDatabaseConnectionError(form, requireDatabase) {
  if (!EVALUATION_DATABASE_OPTIONS.some((option) => option.value === form.eval_db_engine)) {
    return '评测库类型仅支持 MySQL 或 PostgreSQL';
  }
  if (!String(form.eval_db_host || '').trim()) return '评测库 Host 不能为空';
  const port = Number(form.eval_db_port);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    return '评测库端口必须是 1 到 65535 之间的整数';
  }
  if (requireDatabase && !String(form.eval_db_database || '').trim()) {
    return '评测库 Database 不能为空';
  }
  if (!String(form.eval_db_username || '').trim()) return '评测库 User 不能为空';
  if (!String(form.eval_db_password || '').trim()) {
    return '评测库 Password 每次保存并测试时都必须填写';
  }
  return '';
}

export function evaluationDatabaseServerFormError(form) {
  return evaluationDatabaseConnectionError(form, false);
}

export function dataSourceFormError(form) {
  if (form.mode === 'none') return '';
  if (form.provider === 'neo4j') {
    const workspaceId = String(form.workspace_value || '').trim();
    if (!workspaceId) {
      return 'Workspace 对应值不能为空，该值同时作为 consumer 评测工作空间 ID';
    }
    if (!CONSUMER_WORKSPACE_ID.test(workspaceId)
      || workspaceId.length > CONSUMER_WORKSPACE_ID_MAX_LENGTH) {
      return '评测工作空间 ID 格式非法：必须以英文小写字母开头，只能包含小写字母、数字、下划线或中划线，且最长 64 个字符';
    }
    if (!String(form.password || '').trim()) {
      return 'Neo4j Password 每次保存并测试时都必须填写';
    }
    return '';
  }
  const workspaceId = String(form.evaluation_workspace_id || '').trim();
  if (!workspaceId) return '评测工作空间 ID 不能为空';
  if (!CONSUMER_WORKSPACE_ID.test(workspaceId)
    || workspaceId.length > CONSUMER_WORKSPACE_ID_MAX_LENGTH) {
    return '评测工作空间 ID 格式非法：必须以英文小写字母开头，只能包含小写字母、数字、下划线或中划线，且最长 64 个字符';
  }
  return evaluationDatabaseConnectionError(form, true);
}

export function providerLabel(provider) {
  return PROVIDER_OPTIONS.find((option) => option.value === provider)?.label || provider || '—';
}
