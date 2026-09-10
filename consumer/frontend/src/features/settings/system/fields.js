import { assertSystemConfigSection } from './model.js';

export const SYSTEM_SETTINGS_PANEL_SECTIONS = Object.freeze([
  'auth',
  'agent',
  'memory',
  'web_search',
  'direct_execution',
  'skill_evolution',
  'ontology_evidence',
]);

export const SYSTEM_SETTINGS_SECTION_META = Object.freeze({
  auth: {
    title: '鉴权',
    description: '配置本地签名、Token 有效期以及远端鉴权服务。密钥输入留空时保留现有值。',
  },
  agent: {
    title: 'Agent 参数',
    description: '配置主链路、子智能体、并发、上下文裁剪与运行时进化参数。',
  },
  memory: {
    title: '长期记忆',
    description: '配置记忆召回数量、阈值与工作目录。',
  },
  web_search: {
    title: '联网搜索',
    description: '配置公开互联网搜索服务、请求边界与域名规则。',
  },
  direct_execution: {
    title: '直接执行',
    description: '控制是否允许直接执行，以及是否仅限当前选中的知识包。',
  },
  skill_evolution: {
    title: '技能进化',
    description: '配置 skill-evolver 服务、轮询节奏与任务执行上限。',
  },
  ontology_evidence: {
    title: '本体证据',
    description: '控制证据检索、业务数据 Grounding、运行事实写入与性能保护。',
  },
});

function normalizePanelSection(section) {
  const normalized = String(section || '').trim().toLowerCase();
  if (!SYSTEM_SETTINGS_PANEL_SECTIONS.includes(normalized)) {
    throw new Error(`不支持的系统设置面板：${normalized || '空值'}`);
  }
  return normalized;
}

export function systemSettingsTabId(section) {
  return `system-settings-tab-${normalizePanelSection(section)}`;
}

export function systemSettingsPanelId(section) {
  return `system-settings-panel-${normalizePanelSection(section)}`;
}

export function systemSettingsModuleKey(section, importRevision = 0) {
  const revision = Number.isInteger(importRevision) && importRevision >= 0 ? importRevision : 0;
  return `${normalizePanelSection(section)}:import-${revision}`;
}

export function nextSystemSettingsTab(section, key) {
  const normalized = normalizePanelSection(section);
  const index = SYSTEM_SETTINGS_PANEL_SECTIONS.indexOf(normalized);
  const [firstSection] = SYSTEM_SETTINGS_PANEL_SECTIONS;
  if (key === 'Home') return firstSection;
  if (key === 'End') return SYSTEM_SETTINGS_PANEL_SECTIONS.at(-1);
  if (key === 'ArrowRight' || key === 'ArrowDown') {
    return SYSTEM_SETTINGS_PANEL_SECTIONS[(index + 1) % SYSTEM_SETTINGS_PANEL_SECTIONS.length];
  }
  if (key === 'ArrowLeft' || key === 'ArrowUp') {
    return SYSTEM_SETTINGS_PANEL_SECTIONS[(index - 1 + SYSTEM_SETTINGS_PANEL_SECTIONS.length)
      % SYSTEM_SETTINGS_PANEL_SECTIONS.length];
  }
  return normalized;
}

const number = (name, label, options = {}) => ({ name, label, type: 'number', step: 1, ...options });
const text = (name, label, options = {}) => ({ name, label, type: 'text', ...options });
const toggle = (name, label, options = {}) => ({ name, label, type: 'checkbox', ...options });
const secret = (name, label, options = {}) => ({ name, label, type: 'password', secret: true, ...options });

export const SYSTEM_CONFIG_FIELDS = Object.freeze({
  auth: [
    secret('auth_secret', '本地签名密钥', { hasField: 'has_auth_secret', clearField: 'clear_auth_secret' }),
    number('auth_token_ttl_seconds', 'Token 有效期（秒）', { min: 1 }),
    text('auth_server_url', '远端鉴权服务 URL'),
    secret('auth_server_admin_token', '远端管理 Token', { hasField: 'has_auth_server_admin_token' }),
    text('auth_server_audience', 'Audience'),
    text('auth_server_issuer', 'Issuer'),
  ],
  agent: [
    number('llm_connection_max_retries', 'LLM 连接重试次数', { min: 0 }),
    number('main_agent_max_iterations', '主智能体最大轮次', { min: 1 }),
    number('sub_agent_max_iterations', '子智能体最大轮次', { min: 1 }),
    number('sub_agent_max_concurrency', '子智能体最大并发', { min: 1 }),
    number('sub_agent_max_depth', '子智能体最大深度', { min: 1 }),
    number('parallel_default_max_workers', '默认并发工作数', { min: 1 }),
    number('context_max_rounds', '上下文保留轮次', { min: 1 }),
    toggle('runtime_warmup_enabled', '启动时预热运行时'),
    number('evolution_min_occurrences', '进化最少出现次数', { min: 1 }),
    number('evolution_min_applied', '进化最少应用次数', { min: 1 }),
    number('evolution_max_rejection', '进化最大拒绝次数', { min: 0 }),
    toggle('evolution_passive_reflection_enabled', '启用被动反思'),
    toggle('evolution_patch_auto_apply', '自动应用进化补丁'),
    number('evolution_patch_cooldown_sec', '补丁冷却时间（秒）', { min: 0 }),
    number('evolution_sweep_interval_sec', '进化扫描间隔（秒）', { min: 1 }),
    number('execute_script_timeout', '脚本执行超时（秒）', { min: 1 }),
  ],
  memory: [
    number('recall_topk', '召回条数', { min: 1 }),
    text('recall_threshold', '召回阈值', { placeholder: '留空使用运行时默认值' }),
    text('workspace_root', '记忆工作目录'),
  ],
  web_search: [
    toggle('enabled', '启用联网搜索'),
    {
      name: 'provider',
      label: 'Provider',
      type: 'select',
      options: [{ value: 'tavily', label: 'Tavily' }],
    },
    text('base_url', '服务地址', { placeholder: 'https://api.tavily.com' }),
    secret('api_key', 'API Key', { hasField: 'has_api_key', clearField: 'clear_api_key' }),
    number('timeout_seconds', '请求超时（秒）', { min: 1 }),
    number('default_max_results', '默认结果数', { min: 1 }),
    number('max_results_limit', '结果数上限', { min: 1 }),
    {
      name: 'search_depth',
      label: '搜索深度',
      type: 'select',
      options: ['basic', 'fast', 'ultra-fast', 'advanced'].map((value) => ({ value, label: value })),
    },
    text('allowed_domains', '允许域名', { hint: '逗号分隔，留空表示不限制' }),
    text('blocked_domains', '屏蔽域名', { hint: '逗号分隔' }),
  ],
  direct_execution: [
    toggle('enabled', '启用直接执行'),
    toggle('selected_ontology_only', '仅允许当前选中的知识包'),
  ],
  skill_evolution: [
    toggle('enabled', '启用技能进化'),
    text('base_url', 'skill-evolver 服务地址', { placeholder: 'http://127.0.0.1:8765' }),
    number('timeout_seconds', '连接超时（秒）', { min: 1 }),
    number('poll_interval_seconds', '轮询间隔（秒）', { min: 1 }),
    number('task_timeout_seconds', '任务超时（秒）', { min: 60 }),
    number('max_iterations', '最大迭代次数', { min: 1, max: 30 }),
  ],
  ontology_evidence: [
    toggle('enabled', '启用本体证据能力'),
    {
      name: 'rollout_mode',
      label: '发布模式',
      type: 'select',
      options: [
        { value: 'off', label: '关闭' },
        { value: 'shadow', label: '影子运行' },
        { value: 'allowlist', label: '工作空间白名单' },
        { value: 'all', label: '全部工作空间' },
      ],
    },
    text('rollout_workspace_ids', '发布工作空间', { hint: 'allowlist 模式下使用，逗号分隔 workspace_id' }),
    toggle('search_enabled', '启用证据检索'),
    {
      name: 'tool_exposure_mode',
      label: '检索工具暴露方式',
      type: 'select',
      options: [
        { value: 'adaptive', label: '仅索引就绪时暴露' },
        { value: 'always', label: '始终暴露' },
      ],
    },
    toggle('auto_build_enabled', '本体变更后自动构建索引'),
    toggle('structured_context_enabled', '启用结构化本体上下文'),
    toggle('auto_ground_business_data', '自动补齐业务查询本体依据', {
      hint: '对应 ONTOLOGY_EVIDENCE_AUTO_GROUND_BUSINESS_DATA；关闭可消除业务查询冷路径 Grounding 开销，但运行证据可能没有 ONT 派生关系',
    }),
    {
      name: 'runtime_fact_write_mode',
      label: '运行事实写入方式',
      type: 'select',
      options: [
        { value: 'sync', label: '同步写入' },
        { value: 'batched', label: '批量异步写入' },
        { value: 'off', label: '关闭写入' },
      ],
    },
    toggle('graph_enabled', '启用图关系扩展召回'),
  ],
});

export function buildSystemConfigSectionPayload(section, draft = {}) {
  const normalizedSection = assertSystemConfigSection(section);
  const fields = SYSTEM_CONFIG_FIELDS[normalizedSection];
  if (!fields) throw new Error(`${normalizedSection} 由独立设置功能管理`);

  const payload = {};
  fields.forEach((field) => {
    const value = draft[field.name];
    if (field.secret) {
      if (typeof value === 'string' && value.trim()) payload[field.name] = value;
      if (field.clearField && draft[field.clearField] === true) payload[field.clearField] = true;
      return;
    }
    if (value !== undefined) payload[field.name] = value;
  });
  return payload;
}
