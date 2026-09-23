// 评测数据源配置表单：provider 字段与校验口径对齐 modeling（data-source-form.js），
// 另含 onto-modeling 自有的数据集通道（eval_data 资料表 / mock 种子）。

export const PROVIDER_OPTIONS = [
  { value: 'nl2sql_template', label: 'NL2SQL Template' },
  { value: 'nl2sql_http', label: 'NL2SQL HTTP' },
  { value: 'neo4j', label: 'Neo4j' },
];

// 表单上的数据源形态；mock / dataset 落库时同为 mode=dataset
export const SOURCE_KINDS = [
  {
    value: 'none',
    icon: '📖',
    title: '不使用数据源',
    desc: '纯知识包问答——验证本体结构与业务语义，不查询具体数据。',
  },
  {
    value: 'mock',
    icon: '🧪',
    title: '示例数据种子',
    desc: '用「示例数据」阶段的 mock 产物生成评测库——没有真实数据也能验证数据问答链路。',
  },
  {
    value: 'dataset',
    icon: '🗄️',
    title: '业务数据表',
    desc: '导入打了「评测数据」用途标签的业务数据表（在左栏「资料」页签上传并打标）。',
  },
  {
    value: 'connection',
    icon: '🔌',
    title: '外部数据源',
    desc: '连接已有的 NL2SQL 数据服务或 Neo4j 图库（与 modeling 数据源配置一致），consumer 直接查询作答。',
  },
];

const WORKSPACE_ID = /^[a-z][a-z0-9_-]*$/;
const WORKSPACE_ID_MAX_LENGTH = 64;
const NL2SQL_SCENE = /^[a-zA-Z0-9_]{1,60}$/;
const WORKSPACE_ID_RULE =
  '必须以英文小写字母开头，只能包含小写字母、数字、下划线或中划线，且最长 64 个字符';

export function providerLabel(provider) {
  return PROVIDER_OPTIONS.find((option) => option.value === provider)?.label || provider || '—';
}

export function sourceKind(source) {
  if (!source) return 'none';
  if (source.mode === 'connection') return 'connection';
  if (source.mode === 'dataset') {
    return source.include_mock && !(source.resources || []).length ? 'mock' : 'dataset';
  }
  return 'none';
}

export function kindMeta(kind) {
  return SOURCE_KINDS.find((item) => item.value === kind) || SOURCE_KINDS[0];
}

export function emptySourceForm(defaults = {}) {
  return {
    name: '',
    kind: 'none',
    resources: [],
    provider: 'nl2sql_template',
    url: '',
    token: '',
    scene: '',
    evaluation_workspace_id: defaults.evaluation_workspace_id || '',
    uri: '',
    username: '',
    password: '',
    database: 'neo4j',
    workspace_property: 'd2o_workspace_id',
    workspace_value: '',
    ontology_property: 'd2o_ontology_id',
    has_token: false,
    has_password: false,
  };
}

export function sourceToForm(source, defaults = {}) {
  const form = emptySourceForm(defaults);
  if (!source) return form;
  const kind = sourceKind(source);
  const next = { ...form, name: source.name || '', kind };
  if (kind === 'dataset') return { ...next, resources: [...(source.resources || [])] };
  if (kind !== 'connection') return next;
  const config = source.config || {};
  if (source.provider === 'neo4j') {
    return {
      ...next,
      provider: 'neo4j',
      uri: config.uri || '',
      username: config.username || '',
      database: config.database || 'neo4j',
      workspace_property: config.workspace_property || '',
      workspace_value: config.workspace_value || '',
      ontology_property: config.ontology_property || '',
      has_password: Boolean(config.has_password),
    };
  }
  return {
    ...next,
    provider: source.provider,
    url: config.url || '',
    scene: config.scene || '',
    evaluation_workspace_id: config.evaluation_workspace_id || '',
    has_token: Boolean(config.has_token),
  };
}

export function sourceFormPayload(form) {
  const name = String(form.name || '').trim();
  if (form.kind === 'mock') return { name, mode: 'dataset', resources: [], include_mock: true };
  if (form.kind === 'dataset') {
    return { name, mode: 'dataset', resources: form.resources, include_mock: false };
  }
  if (form.kind !== 'connection') return { name, mode: 'none' };
  if (form.provider === 'neo4j') {
    return {
      name,
      mode: 'connection',
      provider: 'neo4j',
      config: {
        uri: form.uri,
        username: form.username,
        password: form.password, // 留空 = 沿用已保存的值
        database: form.database,
        workspace_property: form.workspace_property,
        workspace_value: form.workspace_value,
        ontology_property: form.ontology_property,
      },
    };
  }
  return {
    name,
    mode: 'connection',
    provider: form.provider,
    config: {
      url: form.url,
      token: form.token, // 留空 = 沿用已保存的值
      scene: form.scene,
      evaluation_workspace_id: form.evaluation_workspace_id,
    },
  };
}

function workspaceIdError(value, label) {
  const workspaceId = String(value || '').trim();
  if (!workspaceId) return `${label}不能为空`;
  if (!WORKSPACE_ID.test(workspaceId) || workspaceId.length > WORKSPACE_ID_MAX_LENGTH) {
    return `${label}格式非法：${WORKSPACE_ID_RULE}`;
  }
  return '';
}

export function sourceFormError(form, options = {}) {
  if (form.kind === 'mock' && !options.mock_tables) return '尚无示例数据：请先完成「示例数据」阶段';
  if (form.kind === 'dataset' && !form.resources.length) return '请至少勾选一份评测数据表';
  if (form.kind !== 'connection') return '';
  if (form.provider === 'neo4j') {
    if (!String(form.uri || '').trim()) return 'Neo4j URL 不能为空';
    if (!String(form.username || '').trim()) return 'Neo4j User 不能为空';
    if (!String(form.password || '').trim() && !form.has_password) return 'Neo4j Password 不能为空';
    if (!String(form.workspace_property || '').trim()) return 'Workspace 隔离字段不能为空';
    return workspaceIdError(form.workspace_value, 'Workspace 对应值');
  }
  if (!/^https?:\/\//.test(String(form.url || '').trim())) {
    return '数据服务 URL 不能为空，且须以 http:// 或 https:// 开头';
  }
  const scene = String(form.scene || '').trim();
  if (!scene) return 'Scene 不能为空';
  if (!NL2SQL_SCENE.test(scene)) {
    return 'Scene 格式非法：只能包含英文字母、数字或下划线，且最长 60 个字符';
  }
  return workspaceIdError(form.evaluation_workspace_id, '评测工作空间 ID');
}

// 已保存配置的一行摘要（选择列表用，不含任何密钥）
export function sourceSummary(source, options = {}) {
  const kind = sourceKind(source);
  if (kind === 'mock') return `示例数据种子（${options.mock_tables || 0} 张示例表）`;
  if (kind === 'dataset') {
    const names = (source.resources || []).map((path) => path.split('/').pop());
    return `数据表：${names.join('、') || '—'}`;
  }
  if (kind === 'connection') {
    const config = source.config || {};
    if (source.provider === 'neo4j') {
      return `${config.uri || '—'} · 库 ${config.database || 'neo4j'} · 工作空间 ${config.workspace_value || '—'}`;
    }
    return `${config.url || '—'} · Scene ${config.scene || '—'} · 工作空间 ${config.evaluation_workspace_id || '—'}`;
  }
  return '纯知识包问答，不查询数据';
}

export function sourceBadge(source) {
  const kind = sourceKind(source);
  if (kind === 'connection') return providerLabel(source.provider);
  return kindMeta(kind).title;
}
