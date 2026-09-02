export const PROVIDER_OPTIONS = [
  { value: 'nl2sql_template', label: 'NL2SQL Template' },
  { value: 'nl2sql_http', label: 'NL2SQL HTTP' },
  { value: 'neo4j', label: 'Neo4j' },
];

const PROVIDER_OPTIONS_BY_PACKAGE_SOURCE = {
  neo4j: ['neo4j'],
  nl2sql: ['nl2sql_template', 'nl2sql_http'],
};

const CONSUMER_WORKSPACE_ID = /^[a-z][a-z0-9_-]*$/;
const CONSUMER_WORKSPACE_ID_MAX_LENGTH = 64;
const NL2SQL_SCENE = /^[a-zA-Z0-9_]{1,60}$/;

export function providerOptionsForPackageDataSource(packageDataSourceType) {
  const allowed = PROVIDER_OPTIONS_BY_PACKAGE_SOURCE[packageDataSourceType];
  if (!allowed) return PROVIDER_OPTIONS;
  return PROVIDER_OPTIONS.filter((option) => allowed.includes(option.value));
}

export function defaultProviderForPackageDataSource(packageDataSourceType) {
  return providerOptionsForPackageDataSource(packageDataSourceType)[0]?.value || 'nl2sql_template';
}

export function normalizeProviderForPackageDataSource(provider, packageDataSourceType) {
  const options = providerOptionsForPackageDataSource(packageDataSourceType);
  return options.some((option) => option.value === provider)
    ? provider
    : defaultProviderForPackageDataSource(packageDataSourceType);
}

export function applyCreatedProjectDatabaseScene(form, database) {
  return { ...form, scene: database };
}

export function evaluationDatabaseUrlSelectionError(mode, customUrl, defaultConfigured) {
  if (mode === 'default') {
    return defaultConfigured ? '' : '服务端未配置 EVAL_DB_URL，请选择并填写自定义 MySQL URL';
  }
  if (mode !== 'custom') return '请选择要使用的数据库连接';
  const value = String(customUrl || '').trim();
  if (!value) return '自定义数据库 URL 不能为空';
  if (value.length > 2048) return '自定义数据库 URL 不能超过 2048 个字符';
  try {
    const parsed = new URL(value);
    if (parsed.protocol !== 'mysql:') return '自定义数据库 URL 仅支持 mysql 协议';
    if (!parsed.hostname) return '自定义数据库 URL 缺少数据库主机';
  } catch {
    return '自定义数据库 URL 格式非法';
  }
  return '';
}

export function emptyDataSourceForm(defaults = {}, packageDataSourceType = '') {
  const provider = normalizeProviderForPackageDataSource(
    defaults.provider || defaultProviderForPackageDataSource(packageDataSourceType),
    packageDataSourceType,
  );
  return {
    mode: 'connection',
    provider,
    url: defaults.url || '',
    token: defaults.token || '',
    scene: defaults.scene || '',
    evaluation_workspace_id: '',
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

export function dataSourceToForm(dataSource, defaults = {}, packageDataSourceType = '') {
  const form = emptyDataSourceForm(defaults, packageDataSourceType);
  if (!dataSource) return form;
  const config = dataSource.config || {};
  const provider = normalizeProviderForPackageDataSource(dataSource.provider || form.provider, packageDataSourceType);
  const common = {
    ...form,
    mode: dataSource.mode || 'connection',
    provider,
  };
  if (provider === 'neo4j') {
    return {
      ...common,
      uri: config.uri || '',
      username: config.username || '',
      database: config.database || 'neo4j',
      workspace_property: config.workspace_property || '',
      workspace_value: config.workspace_value || '',
      ontology_property: config.ontology_property || '',
      ontology_value: config.ontology_value || '',
      password: '',
      has_password: Boolean(config.has_password),
    };
  }
  return {
    ...common,
    url: config.url || '',
    scene: config.scene || '',
    evaluation_workspace_id: config.evaluation_workspace_id || '',
    token: '',
    has_token: Boolean(config.has_token),
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
    },
  };
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
      return 'Neo4j Password 每次保存并测试连接时都必须填写';
    }
    return '';
  }
  if (!String(form.url || '').trim()) return '数据服务 URL 不能为空';
  const scene = String(form.scene || '').trim();
  if (!scene) return 'Scene 不能为空';
  if (!NL2SQL_SCENE.test(scene)) {
    return 'Scene 格式非法：只能包含英文字母、数字或下划线，且最长 60 个字符';
  }
  const workspaceId = String(form.evaluation_workspace_id || '').trim();
  if (!workspaceId) return '评测工作空间 ID 不能为空';
  if (!CONSUMER_WORKSPACE_ID.test(workspaceId)
    || workspaceId.length > CONSUMER_WORKSPACE_ID_MAX_LENGTH) {
    return '评测工作空间 ID 格式非法：必须以英文小写字母开头，只能包含小写字母、数字、下划线或中划线，且最长 64 个字符';
  }
  return '';
}

export function providerLabel(provider) {
  return PROVIDER_OPTIONS.find((option) => option.value === provider)?.label || provider || '—';
}
