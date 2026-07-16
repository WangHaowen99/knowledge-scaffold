export const PROVIDER_OPTIONS = [
  { value: 'nl2sql_template', label: 'NL2SQL Template' },
  { value: 'nl2sql_http', label: 'NL2SQL HTTP' },
  { value: 'neo4j', label: 'Neo4j' },
];

const CONSUMER_WORKSPACE_ID = /^[a-z][a-z0-9_-]*$/;
const CONSUMER_WORKSPACE_ID_MAX_LENGTH = 64;

export function emptyDataSourceForm(defaults = {}) {
  return {
    mode: 'connection',
    provider: defaults.provider || 'nl2sql_template',
    url: defaults.url || '',
    token: defaults.token || '',
    scene: defaults.scene || '',
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
  return {
    ...form,
    mode: dataSource.mode || 'connection',
    provider: dataSource.provider || form.provider,
    ...config,
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
    config: { url: form.url, token: form.token, scene: form.scene },
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
      return 'Neo4j Password 每次保存并测试时都必须填写';
    }
  }
  return '';
}

export function providerLabel(provider) {
  return PROVIDER_OPTIONS.find((option) => option.value === provider)?.label || provider || '—';
}
