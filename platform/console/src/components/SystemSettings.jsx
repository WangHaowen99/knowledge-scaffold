import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Activity,
  ArrowLeft,
  Bot,
  ChevronDown,
  Download,
  Eye,
  EyeOff,
  FolderCog,
  Loader2,
  Network,
  RefreshCw,
  Save,
  Search,
  Settings2,
  Shield,
  Trash2,
  Upload,
} from 'lucide-react';

import { authFetch } from '../utils/auth';

const CONNECTION_SECTIONS = ['llm_profiles', 'task_llm', 'web_search', 'observability'];
const VISIBLE_SECTION_ORDER = [...CONNECTION_SECTIONS, 'agent'];
const IMPORTABLE_SECTION_ORDER = ['llm', ...VISIBLE_SECTION_ORDER];
const EMPTY_STATUS = { success: false, message: '', tested_at: null };
const MODEL_PROVIDER_OPTIONS = [
  { value: 'glm', label: 'GLM' },
  { value: 'deepseek', label: 'DeepSeek' },
  { value: 'openai_compatible', label: 'OpenAI Compatible' },
];
const MODEL_PROVIDER_LABELS = Object.fromEntries(MODEL_PROVIDER_OPTIONS.map((option) => [option.value, option.label]));
const MODEL_PROVIDER_DEFAULT_BASE_URLS = {
  glm: 'https://open.bigmodel.cn/api/paas/v4',
  deepseek: 'https://api.deepseek.com',
};
const OBSERVABILITY_PROVIDER_OPTIONS = [
  { value: 'local', label: '本地日志' },
  { value: 'langfuse', label: 'Langfuse' },
];
const WEB_SEARCH_PROVIDER_OPTIONS = [
  { value: 'tavily', label: 'Tavily' },
];
const WEB_SEARCH_DEPTH_OPTIONS = [
  { value: 'basic', label: 'Basic' },
  { value: 'fast', label: 'Fast' },
  { value: 'ultra-fast', label: 'Ultra Fast' },
  { value: 'advanced', label: 'Advanced' },
];
const SECTION_META = {
  llm_profiles: {
    title: '模型列表',
    detailTitle: '多模型配置',
    description: '配置可在对话中选择的模型，并指定默认模型。',
    icon: Bot,
    navTone: 'llm',
  },
  llm: {
    title: '旧版单模型',
    detailTitle: '旧版单模型配置',
    description: '旧版单模型配置；未配置多模型列表时会作为默认模型。',
    secretField: 'api_key',
    clearField: 'clear_api_key',
    icon: Bot,
    navTone: 'llm',
    fields: [
      { name: 'base_url', label: '基础 URL', placeholder: 'https://open.bigmodel.cn/api/paas/v4' },
      { name: 'model', label: '模型名称', placeholder: 'glm-5' },
      { name: 'api_key', label: 'API Key', placeholder: '输入新的 API Key', type: 'password' },
      { name: 'timeout', label: '超时（秒）', placeholder: '600', type: 'number', step: '1' },
      { name: 'max_tokens', label: '最大 Tokens', placeholder: '4096', type: 'number', step: '1' },
      { name: 'temperature', label: 'Temperature', placeholder: '0.7', type: 'number', step: '0.1' },
    ],
    fieldGroups: [
      { title: '连接信息', fields: ['base_url', 'model', 'api_key'] },
      { title: '生成参数', fields: ['timeout', 'max_tokens', 'temperature'] },
    ],
  },
  task_llm: {
    title: '轻量模型',
    detailTitle: '轻量模型配置',
    description: '配置任务摘要、小任务等场景的轻量 LLM；留空时沿用主模型。',
    secretField: 'api_key',
    clearField: 'clear_api_key',
    icon: Settings2,
    navTone: 'task-llm',
    fields: [
      { name: 'base_url', label: '基础 URL', placeholder: '留空则复用主模型' },
      { name: 'model', label: '模型名称', placeholder: '留空则复用主模型' },
      { name: 'api_key', label: 'API Key', placeholder: '输入新的 API Key', type: 'password' },
      { name: 'timeout', label: '超时（秒）', placeholder: '600', type: 'number', step: '1' },
    ],
    fieldGroups: [
      { title: '连接信息', fields: ['base_url', 'model', 'api_key'] },
      { title: '生成参数', fields: ['timeout'] },
    ],
  },
  observability: {
    title: '观测打点',
    detailTitle: '观测打点配置',
    description: '配置运行时观测输出目标；保存后只影响新请求。',
    secretField: 'langfuse_secret_key',
    clearField: 'clear_langfuse_secret_key',
    icon: Activity,
    navTone: 'observability',
    layout: 'double',
    fields: [
      { name: 'observability_enabled', label: '观测日志', type: 'checkbox', fullWidth: true, enabledText: '运行时观测日志已启用', disabledText: '运行时不输出观测日志' },
      { name: 'observability_provider', label: '对接方式', type: 'select', options: OBSERVABILITY_PROVIDER_OPTIONS },
      { name: 'local_retention_days', label: '日志保留天数', placeholder: '30', type: 'number', step: '1' },
      { name: 'langfuse_base_url', label: 'Langfuse 地址', placeholder: 'https://cloud.langfuse.com' },
      { name: 'langfuse_public_key', label: 'Public Key', placeholder: 'pk-lf-...' },
      { name: 'langfuse_secret_key', label: 'Secret Key', placeholder: '输入新的 Secret Key', type: 'password' },
      { name: 'langfuse_debug', label: 'SDK 调试日志', type: 'checkbox', fullWidth: true, enabledText: '详细记录 SDK 调试信息', disabledText: '常规日志级别' },
    ],
    fieldGroups: [
      { title: '日志开关', fields: ['observability_enabled'] },
      { title: '对接方式', fields: ['observability_provider'] },
      { title: '本地日志', fields: ['local_retention_days'] },
      { title: 'Langfuse 连接', fields: ['langfuse_base_url', 'langfuse_public_key', 'langfuse_secret_key', 'langfuse_debug'] },
    ],
  },
  web_search: {
    title: '联网搜索',
    detailTitle: '联网搜索配置',
    description: '配置 Agent 可调用的公开互联网搜索工具；保存后只影响新请求。',
    secretField: 'api_key',
    clearField: 'clear_api_key',
    icon: Search,
    navTone: 'web-search',
    layout: 'double',
    fields: [
      { name: 'enabled', label: '联网搜索工具', type: 'checkbox', fullWidth: true, enabledText: '模型可按需调用联网搜索', disabledText: '模型不会看到联网搜索工具' },
      { name: 'provider', label: 'Provider', type: 'select', options: WEB_SEARCH_PROVIDER_OPTIONS },
      { name: 'base_url', label: '服务地址', placeholder: 'https://api.tavily.com' },
      { name: 'api_key', label: 'API Key', placeholder: '输入新的 API Key', type: 'password' },
      { name: 'timeout_seconds', label: '超时（秒）', placeholder: '20', type: 'number', step: '1' },
      { name: 'default_max_results', label: '默认结果数', placeholder: '5', type: 'number', step: '1' },
      { name: 'max_results_limit', label: '最大结果数上限', placeholder: '10', type: 'number', step: '1' },
      { name: 'search_depth', label: '搜索深度', type: 'select', options: WEB_SEARCH_DEPTH_OPTIONS },
      { name: 'allowed_domains', label: '允许域名', placeholder: '逗号分隔，留空表示不限制', fullWidth: true },
      { name: 'blocked_domains', label: '屏蔽域名', placeholder: '逗号分隔', fullWidth: true },
    ],
    fieldGroups: [
      { title: '工具开关', fields: ['enabled'] },
      { title: 'Provider 连接', fields: ['provider', 'base_url', 'api_key'] },
      { title: '请求控制', fields: ['timeout_seconds', 'default_max_results', 'max_results_limit', 'search_depth'] },
      { title: '域名限制', fields: ['allowed_domains', 'blocked_domains'] },
    ],
  },
  storage: {
    title: '存储路径',
    detailTitle: '存储与工作目录',
    description: '配置聊天记录、执行记录与运行根目录。',
    icon: FolderCog,
    fields: [
      { name: 'chat_storage_dir', label: '聊天存储目录', placeholder: './data/chats' },
      { name: 'execution_storage_dir', label: '执行记录目录', placeholder: './data/executions' },
      { name: 'project_root', label: '运行根目录', placeholder: '.onto-runtime' },
      { name: 'ontologies_root', label: '本体目录', placeholder: '.onto-runtime/workspaces/<workspace_id>/ontologies（运行时动态解析）' },
      { name: 'skills_root', label: '技能目录', placeholder: '.onto-runtime/workspaces/<workspace_id>/skills（运行时动态解析）' },
      { name: 'auth_users_file', label: '用户文件', placeholder: '.onto-runtime/users.json' },
    ],
    fieldGroups: [
      {
        title: '运行目录',
        description: '这些路径会影响运行根目录、会话存储以及用户鉴权文件位置；workspace 模式下本体与技能目录按当前工作空间动态解析。',
        tone: 'embedding',
        fields: [
          { name: 'chat_storage_dir', label: '聊天存储目录', placeholder: './data/chats' },
          { name: 'execution_storage_dir', label: '执行记录目录', placeholder: './data/executions' },
          { name: 'project_root', label: '运行根目录', placeholder: '.onto-runtime' },
          { name: 'ontologies_root', label: '本体目录', placeholder: '.onto-runtime/workspaces/<workspace_id>/ontologies（运行时动态解析）' },
          { name: 'skills_root', label: '技能目录', placeholder: '.onto-runtime/workspaces/<workspace_id>/skills（运行时动态解析）' },
          { name: 'auth_users_file', label: '用户文件', placeholder: '.onto-runtime/users.json', fullWidth: true },
        ],
      },
    ],
  },
  auth: {
    title: '鉴权参数',
    detailTitle: '鉴权配置',
    description: '管理登录签名密钥和 Token 有效期。',
    secretField: 'auth_secret',
    clearField: 'clear_auth_secret',
    icon: Shield,
    fields: [
      { name: 'auth_secret', label: '签名密钥', placeholder: '输入新的 AUTH_SECRET', type: 'password', fullWidth: true },
      { name: 'auth_token_ttl_seconds', label: 'Token 有效期（秒）', placeholder: '43200', type: 'number', step: '1', fullWidth: true },
    ],
    fieldGroups: [
      {
        title: '登录会话',
        description: '修改后仅影响后续新签发的登录 Token。',
        tone: 'highlight',
        layout: 'single',
        fields: [
          { name: 'auth_secret', label: '签名密钥', placeholder: '输入新的 AUTH_SECRET', type: 'password', fullWidth: true },
          { name: 'auth_token_ttl_seconds', label: 'Token 有效期（秒）', placeholder: '43200', type: 'number', step: '1', fullWidth: true },
        ],
      },
    ],
  },
  agent: {
    title: 'Agent 参数',
    detailTitle: 'Agent 运行参数',
    description: '配置 Jiuwen ReAct 主链路、主循环、子智能体、并发和上下文裁剪行为。',
    icon: Network,
    fields: [
      { name: 'llm_connection_max_retries', label: 'LLM 连接重试次数', placeholder: '3', type: 'number', step: '1' },
      { name: 'main_agent_max_iterations', label: '主智能体最大轮次', placeholder: '50', type: 'number', step: '1' },
      { name: 'sub_agent_max_iterations', label: '子智能体最大轮次', placeholder: '5', type: 'number', step: '1' },
      { name: 'sub_agent_max_concurrency', label: '子智能体最大并发数', placeholder: '4', type: 'number', step: '1' },
      { name: 'sub_agent_max_depth', label: '子智能体最大深度', placeholder: '2', type: 'number', step: '1' },
      { name: 'parallel_default_max_workers', label: '默认并发数', placeholder: '10', type: 'number', step: '1' },
      { name: 'context_max_rounds', label: '上下文保留轮次', placeholder: '10', type: 'number', step: '1' },
      { name: 'context_prune_tools', label: '上下文裁剪工具', placeholder: '逗号分隔，例如 ontology_context,business_data_search,web_search', fullWidth: true },
    ],
    fieldGroups: [
      {
        title: '执行控制',
        description: '影响主流程、子流程的重试和迭代次数。',
        tone: 'terminology',
        fields: [
          { name: 'llm_connection_max_retries', label: 'LLM 连接重试次数', placeholder: '3', type: 'number', step: '1' },
          { name: 'main_agent_max_iterations', label: '主智能体最大轮次', placeholder: '50', type: 'number', step: '1' },
          { name: 'sub_agent_max_iterations', label: '子智能体最大轮次', placeholder: '5', type: 'number', step: '1' },
          { name: 'sub_agent_max_concurrency', label: '子智能体最大并发数', placeholder: '4', type: 'number', step: '1' },
          { name: 'sub_agent_max_depth', label: '子智能体最大深度', placeholder: '2', type: 'number', step: '1' },
        ],
      },
      {
        title: '并发与上下文',
        description: '影响并行任务数、上下文保留轮次以及需要裁剪的工具结果。',
        tone: 'embedding',
        fields: [
          { name: 'parallel_default_max_workers', label: '默认并发数', placeholder: '10', type: 'number', step: '1' },
          { name: 'context_max_rounds', label: '上下文保留轮次', placeholder: '10', type: 'number', step: '1' },
          { name: 'context_prune_tools', label: '上下文裁剪工具', placeholder: '逗号分隔，例如 ontology_context,business_data_search,web_search', fullWidth: true },
        ],
      },
    ],
  },
};

function buildInitialState() {
  return {
    llm_profiles: {
      default_profile_id: 'default',
      profiles: [],
    },
    llm: { base_url: '', model: '', api_key: '', timeout: '600', max_tokens: '4096', temperature: '0.7', has_api_key: false },
    task_llm: { base_url: '', model: '', api_key: '', timeout: '600', has_api_key: false },
    observability: {
      mode: 'local',
      observability_enabled: false,
      observability_provider: 'local',
      local_retention_days: '30',
      langfuse_public_key: '',
      langfuse_secret_key: '',
      langfuse_base_url: '',
      langfuse_debug: false,
      has_langfuse_secret_key: false,
    },
    web_search: {
      enabled: false,
      provider: 'tavily',
      base_url: 'https://api.tavily.com',
      api_key: '',
      timeout_seconds: '20',
      default_max_results: '5',
      max_results_limit: '10',
      search_depth: 'basic',
      allowed_domains: '',
      blocked_domains: '',
      has_api_key: false,
    },
    storage: {
      chat_storage_dir: './data/chats',
      execution_storage_dir: './data/executions',
      project_root: '',
      ontologies_root: '',
      skills_root: '',
      auth_users_file: '.onto-runtime/users.json',
    },
    auth: {
      auth_secret: '',
      auth_token_ttl_seconds: '43200',
      has_auth_secret: false,
    },
    agent: {
      llm_connection_max_retries: '3',
      main_agent_max_iterations: '50',
      sub_agent_max_iterations: '5',
      sub_agent_max_concurrency: '4',
      sub_agent_max_depth: '2',
      parallel_default_max_workers: '10',
      context_max_rounds: '10',
      context_prune_tools: '',
    },
  };
}

function getSectionFields(meta) {
  if (!meta?.fields) return [];
  const fieldMap = new Map((meta.fields || []).map((field) => [field.name, field]));
  if (Array.isArray(meta.fieldGroups) && meta.fieldGroups.length > 0) {
    return meta.fieldGroups.flatMap((group) =>
      (group.fields || [])
        .map((field) => (typeof field === 'string' ? fieldMap.get(field) : field))
        .filter(Boolean)
    );
  }
  return meta.fields || [];
}

function createBlankModelProfile(index = 1) {
  return {
    id: `model_${Date.now()}_${index}`,
    name: `模型 ${index}`,
    provider: 'openai_compatible',
    base_url: '',
    model: '',
    api_key: '',
    timeout: '600',
    max_tokens: '4096',
    temperature: '0.7',
    enabled: true,
    has_api_key: false,
  };
}

function mergeModelProfilesState(remoteConfig, previousState) {
  const profiles = Array.isArray(remoteConfig?.profiles)
    ? remoteConfig.profiles.map((profile, index) => ({
      ...createBlankModelProfile(index + 1),
      ...profile,
      api_key: '',
      timeout: String(profile.timeout ?? '600'),
      max_tokens: String(profile.max_tokens ?? '4096'),
      temperature: String(profile.temperature ?? '0.7'),
      has_api_key: !!profile.has_api_key,
    }))
    : previousState.profiles;
  return {
    default_profile_id: remoteConfig?.default_profile_id || previousState.default_profile_id || profiles[0]?.id || 'default',
    profiles,
  };
}

function mergeSectionState(section, remoteConfig, previousState) {
  if (section === 'llm_profiles') {
    return mergeModelProfilesState(remoteConfig, previousState);
  }
  if (section === 'observability') {
    const localConfig = remoteConfig?.local || {};
    const langfuseConfig = remoteConfig?.langfuse || {};
    const mode = remoteConfig?.mode || previousState.mode || 'local';
    const provider = mode === 'langfuse' ? 'langfuse' : (previousState.observability_provider || 'local');
    return {
      ...previousState,
      mode,
      observability_enabled: remoteConfig?.enabled ?? previousState.observability_enabled ?? false,
      observability_provider: provider,
      local_retention_days: String(localConfig.retention_days ?? previousState.local_retention_days ?? '30'),
      langfuse_public_key: langfuseConfig.public_key ?? previousState.langfuse_public_key ?? '',
      langfuse_secret_key: '',
      langfuse_base_url: langfuseConfig.base_url ?? previousState.langfuse_base_url ?? '',
      langfuse_debug: !!langfuseConfig.debug,
      has_langfuse_secret_key: !!langfuseConfig.has_secret_key,
    };
  }
  const nextState = { ...previousState };
  Object.entries(remoteConfig || {}).forEach(([key, value]) => {
    if (key.startsWith('has_')) {
      nextState[key] = value;
      return;
    }
    nextState[key] = typeof value === 'number' ? String(value) : value;
  });
  const secretField = SECTION_META[section].secretField;
  if (secretField) nextState[secretField] = '';
  return nextState;
}

function mergeSavedSectionState(section, remoteConfig, submittedState, latestState) {
  const savedState = mergeSectionState(section, remoteConfig, submittedState);
  const nextState = { ...latestState };
  Object.keys(savedState).forEach((key) => {
    if (key.startsWith('has_')) {
      nextState[key] = savedState[key];
      return;
    }
    if (!Object.prototype.hasOwnProperty.call(submittedState, key) || Object.is(latestState[key], submittedState[key])) {
      nextState[key] = savedState[key];
    }
  });
  return nextState;
}

function getObservabilityModeFromState(state) {
  return state.observability_provider === 'langfuse' ? 'langfuse' : 'local';
}

function buildPayload(section, state) {
  if (section === 'llm_profiles') {
    return {
      default_profile_id: state.default_profile_id,
      profiles: (state.profiles || []).map((profile) => ({
        id: profile.id,
        name: profile.name,
        provider: profile.provider,
        base_url: profile.base_url,
        model: profile.model,
        ...(profile.api_key?.trim() ? { api_key: profile.api_key.trim() } : {}),
        timeout: profile.timeout,
        max_tokens: profile.max_tokens,
        temperature: profile.temperature,
        enabled: !!profile.enabled,
      })),
    };
  }
  if (section === 'observability') {
    return {
      enabled: !!state.observability_enabled,
      mode: getObservabilityModeFromState(state),
      local: {
        retention_days: state.local_retention_days,
      },
      langfuse: {
        public_key: state.langfuse_public_key,
        ...(state.langfuse_secret_key?.trim() ? { secret_key: state.langfuse_secret_key.trim() } : {}),
        base_url: state.langfuse_base_url,
        debug: !!state.langfuse_debug,
      },
    };
  }

  const meta = SECTION_META[section];
  const payload = {};
  getSectionFields(meta).forEach((field) => {
    if (field.name === meta.secretField) return;
    payload[field.name] = state[field.name];
  });
  if (meta.secretField && state[meta.secretField]?.trim()) {
    payload[meta.secretField] = state[meta.secretField].trim();
  }
  return payload;
}

function normalizeStatusMap(rawStatus) {
  const source = rawStatus && typeof rawStatus === 'object' ? rawStatus : {};
  return VISIBLE_SECTION_ORDER.reduce((acc, section) => {
    const item = source[section] && typeof source[section] === 'object' ? source[section] : {};
    acc[section] = {
      success: !!item.success,
      message: typeof item.message === 'string' ? item.message : '',
      tested_at: typeof item.tested_at === 'string' ? item.tested_at : null,
    };
    return acc;
  }, {});
}

function createEmptyStatusMap() {
  return normalizeStatusMap({});
}

function formatRelativeTime(isoString) {
  if (!isoString) return '';
  const timestamp = Date.parse(isoString);
  if (Number.isNaN(timestamp)) return '';
  const diffSeconds = Math.round((timestamp - Date.now()) / 1000);
  const formatter = new Intl.RelativeTimeFormat('zh-CN', { numeric: 'auto' });
  const units = [
    { limit: 60, value: 1, unit: 'second' },
    { limit: 3600, value: 60, unit: 'minute' },
    { limit: 86400, value: 3600, unit: 'hour' },
    { limit: 604800, value: 86400, unit: 'day' },
  ];
  for (const item of units) {
    if (Math.abs(diffSeconds) < item.limit) {
      return formatter.format(Math.round(diffSeconds / item.value), item.unit);
    }
  }
  return formatter.format(Math.round(diffSeconds / 604800), 'week');
}

function getFieldPlaceholder(field, hasSecret) {
  if (field.type !== 'password') return field.placeholder;
  return hasSecret ? '••••••' : '待配置';
}

function isSectionConfigured(section, state) {
  if (!state) return false;
  if (section === 'llm_profiles') return Array.isArray(state.profiles) && state.profiles.some((profile) => String(profile.base_url || '').trim() && String(profile.model || '').trim());
  if (section === 'llm') return !!(String(state.base_url || '').trim() && String(state.model || '').trim() && state.has_api_key);
  if (section === 'task_llm') return !!(String(state.base_url || '').trim() && String(state.model || '').trim());
  if (section === 'observability') {
    if (!state.observability_enabled) return false;
    const mode = getObservabilityModeFromState(state);
    if (mode === 'local') return true;
    return !!(
      String(state.langfuse_base_url || '').trim()
      && String(state.langfuse_public_key || '').trim()
      && state.has_langfuse_secret_key
    );
  }
  if (section === 'web_search') {
    if (!state.enabled) return false;
    return !!(
      String(state.base_url || '').trim()
      && state.has_api_key
    );
  }
  if (section === 'storage') return !!(String(state.project_root || '').trim() && String(state.ontologies_root || '').trim() && String(state.skills_root || '').trim());
  if (section === 'auth') return !!state.has_auth_secret;
  if (section === 'agent') return !!String(state.main_agent_max_iterations || '').trim();
  return false;
}

function getConnectionSectionStatus(section, state, status, isDirty) {
  if (isDirty) return { tone: 'edited', detail: '有未保存变更' };
  if (status?.success) {
    const relativeTime = status.tested_at ? formatRelativeTime(status.tested_at) : '';
    const successMsg = status.message || '配置校验通过';
    return {
      tone: 'success',
      detail: relativeTime ? `本次校验通过：${successMsg} (${relativeTime})` : `本次校验通过：${successMsg}`,
    };
  }
  if (status?.tested_at) return { tone: 'warning', detail: `本次校验失败：${status.message || '连接测试未通过'}` };
  if (isSectionConfigured(section, state)) return { tone: 'pending', detail: '配置已填写，可按需校验' };
  return { tone: 'idle', detail: '尚未完成配置' };
}

function shouldShowField(section, field, state) {
  if (section === 'web_search') {
    if (field?.name === 'enabled') return true;
    return !!state?.enabled;
  }
  if (section !== 'observability') return true;
  const fieldName = field?.name || '';
  const enabled = !!state?.observability_enabled;
  const mode = getObservabilityModeFromState(state);
  if (fieldName === 'observability_enabled') return true;
  if (fieldName === 'observability_provider') return enabled;
  if (fieldName.startsWith('local_')) return enabled && mode === 'local';
  if (fieldName.startsWith('langfuse_')) return enabled && mode === 'langfuse';
  return true;
}

function shouldShowClearSecretAction(section, state) {
  if (section === 'web_search') return !!state?.enabled;
  if (section !== 'observability') return true;
  return !!state?.observability_enabled && getObservabilityModeFromState(state) === 'langfuse';
}

function SystemSettings({ onBack, canEdit = false, embedded = false }) {
  const [formState, setFormState] = useState(buildInitialState);
  const [baselineState, setBaselineState] = useState(buildInitialState);
  const [sectionStatuses, setSectionStatuses] = useState(createEmptyStatusMap);
  const [busySectionActions, setBusySectionActions] = useState({});
  const [sectionErrors, setSectionErrors] = useState({});
  const [sectionSuccess, setSectionSuccess] = useState({});
  const [pageFeedback, setPageFeedback] = useState(null);
  const [topAction, setTopAction] = useState('');
  const [pendingImport, setPendingImport] = useState(null);
  const [pendingModelDelete, setPendingModelDelete] = useState(null);
  const [loading, setLoading] = useState(true);
  const [loadingError, setLoadingError] = useState('');
  const [activeGroupKey, setActiveGroupKey] = useState('connections');
  const [activeConnectionSection, setActiveConnectionSection] = useState('llm_profiles');
  const [isAdvancedRuntimeOpen, setIsAdvancedRuntimeOpen] = useState(false);
  const [expandedModelProfiles, setExpandedModelProfiles] = useState({});
  const [visibleSecrets, setVisibleSecrets] = useState({});
  const importInputRef = useRef(null);
  const formStateRef = useRef(formState);
  const isReadOnly = !canEdit;

  useEffect(() => {
    formStateRef.current = formState;
  }, [formState]);

  const runtimeEntries = useMemo(() => [['agent', SECTION_META.agent]], []);

  const applyPublicConfig = useCallback((config) => {
    const nextState = buildInitialState();
    VISIBLE_SECTION_ORDER.forEach((section) => {
      nextState[section] = mergeSectionState(section, config?.[section] || {}, nextState[section]);
    });
    setFormState(nextState);
    setBaselineState(nextState);
    setSectionStatuses(createEmptyStatusMap());
    setExpandedModelProfiles({});
  }, []);

  const loadConfig = useCallback(async () => {
    setLoading(true);
    setLoadingError('');
    try {
      const response = await authFetch('/api/system-config');
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data?.detail || '加载系统配置失败');
      applyPublicConfig(data);
    } catch (err) {
      const message = err?.message || '加载系统配置失败';
      setLoadingError(message === 'Forbidden' ? '无权限访问系统配置' : message);
    } finally {
      setLoading(false);
    }
  }, [applyPublicConfig]);

  useEffect(() => {
    loadConfig();
  }, [loadConfig]);

  const handleFieldChange = (section, fieldName, value) => {
    if (isReadOnly || busySectionActions[section]) return;
    setFormState((prev) => ({ ...prev, [section]: { ...prev[section], [fieldName]: value } }));
  };

  const handleSave = async (section, extraData = null, successMessage = '保存成功', action = 'save') => {
    if (isReadOnly) return;
    const submittedSectionState = { ...formStateRef.current[section] };
    const payload = extraData || buildPayload(section, submittedSectionState);
    setPageFeedback(null);
    setBusySectionActions((prev) => ({ ...prev, [section]: action }));
    setSectionErrors((prev) => ({ ...prev, [section]: '' }));
    setSectionSuccess((prev) => ({ ...prev, [section]: '' }));
    try {
      const response = await authFetch('/api/system-config', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ section, data: payload }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data?.detail || '保存失败');
      const mergedBaselineState = mergeSectionState(section, data?.config || {}, submittedSectionState);
      setFormState((prev) => ({ ...prev, [section]: mergeSavedSectionState(section, data?.config || {}, submittedSectionState, prev[section]) }));
      setBaselineState((prev) => ({ ...prev, [section]: mergedBaselineState }));
      setSectionStatuses((prev) => ({ ...prev, [section]: { ...EMPTY_STATUS } }));
      setSectionSuccess((prev) => ({ ...prev, [section]: successMessage }));
      setVisibleSecrets((prev) => ({ ...prev, [section]: false }));
      if (section === 'llm_profiles') setExpandedModelProfiles({});
    } catch (err) {
      setSectionErrors((prev) => ({ ...prev, [section]: err?.message || '保存失败' }));
    } finally {
      setBusySectionActions((prev) => ({ ...prev, [section]: null }));
    }
  };

  const handleTestConnection = async (section) => {
    if (isReadOnly) return;
    const submittedSectionState = { ...formStateRef.current[section] };
    setPageFeedback(null);
    setBusySectionActions((prev) => ({ ...prev, [section]: 'test' }));
    setSectionErrors((prev) => ({ ...prev, [section]: '' }));
    setSectionSuccess((prev) => ({ ...prev, [section]: '' }));
    try {
      const response = await authFetch('/api/system-config/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ section, data: buildPayload(section, submittedSectionState) }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data?.detail || '校验失败');
      const nextStatus = { success: !!data?.success, message: data?.message || '', tested_at: data?.tested_at || new Date().toISOString() };
      setSectionStatuses((prev) => ({ ...prev, [section]: nextStatus }));
      if (nextStatus.success) {
        setSectionSuccess((prev) => ({ ...prev, [section]: nextStatus.message || '校验成功' }));
      } else {
        setSectionErrors((prev) => ({ ...prev, [section]: nextStatus.message || '校验失败' }));
      }
    } catch (err) {
      setSectionErrors((prev) => ({ ...prev, [section]: err?.message || '校验失败' }));
    } finally {
      setBusySectionActions((prev) => ({ ...prev, [section]: null }));
    }
  };

  const handleClearSecret = async (section) => {
    if (isReadOnly) return;
    const clearField = SECTION_META[section].clearField;
    const secretField = SECTION_META[section].secretField;
    if (!clearField || !secretField) return;
    const currentSectionState = formStateRef.current[section];
    await handleSave(
      section,
      { ...buildPayload(section, { ...currentSectionState, [secretField]: '' }), [clearField]: true },
      '已清空已保存密钥',
      'clear'
    );
  };

  const handleDownloadTemplate = async () => {
    if (isReadOnly) return;
    setPageFeedback(null);
    setTopAction('download');
    try {
      const response = await authFetch('/api/system-config/template');
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(data?.detail || '下载模板失败');
      }
      const blob = await response.blob();
      const disposition = response.headers.get('content-disposition') || '';
      const match = disposition.match(/filename="?([^"]+)"?/i);
      const fileName = match?.[1] || 'system_config.template.json';
      const objectUrl = window.URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = objectUrl;
      link.download = fileName;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      window.URL.revokeObjectURL(objectUrl);
    } catch (err) {
      setPageFeedback({ type: 'error', message: err?.message || '下载模板失败' });
    } finally {
      setTopAction('');
    }
  };

  const handleImportButtonClick = () => {
    if (isReadOnly) return;
    setPageFeedback(null);
    if (importInputRef.current) {
      importInputRef.current.value = '';
      importInputRef.current.click();
    }
  };

  const handleImportFileSelected = async (event) => {
    if (isReadOnly) return;
    const file = event.target.files?.[0];
    if (!file) return;
    setPageFeedback(null);
    try {
      const text = await file.text();
      const parsed = JSON.parse(text);
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        throw new Error('导入文件必须是 JSON 对象');
      }
      const sections = IMPORTABLE_SECTION_ORDER.filter((section) => Object.prototype.hasOwnProperty.call(parsed, section));
      if (sections.length === 0) throw new Error('导入文件中未包含可识别的配置 section');
      setPendingImport({ fileName: file.name, config: parsed, sections });
    } catch (err) {
      setPendingImport(null);
      setPageFeedback({ type: 'error', message: err?.message || '导入文件解析失败' });
    }
  };

  const handleConfirmImport = async () => {
    if (isReadOnly || !pendingImport?.config) return;
    setTopAction('import');
    setPageFeedback(null);
    try {
      const response = await authFetch('/api/system-config/import', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ config: pendingImport.config }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data?.detail || '配置导入失败');
      applyPublicConfig(data?.config || {});
      setPendingImport(null);
      const importedSections = Array.isArray(data?.imported_sections) ? data.imported_sections : pendingImport.sections;
      setPageFeedback({ type: 'success', message: `配置导入成功，已更新：${importedSections.join('、')}` });
    } catch (err) {
      setPageFeedback({ type: 'error', message: err?.message || '配置导入失败' });
    } finally {
      setTopAction('');
    }
  };

  const isSectionDirty = useCallback(
    (section) => JSON.stringify(buildPayload(section, formState[section])) !== JSON.stringify(buildPayload(section, baselineState[section])),
    [baselineState, formState]
  );

  const saveModelProfilesSection = async (
    sectionState,
    successMessage = '模型配置已保存',
    action = 'save-profile',
    options = {}
  ) => {
    if (isReadOnly) return false;
    const { preserveDraftProfiles = false, savedProfileId = '' } = options;
    const payload = buildPayload('llm_profiles', sectionState);
    setPageFeedback(null);
    setBusySectionActions((prev) => ({ ...prev, llm_profiles: action }));
    setSectionErrors((prev) => ({ ...prev, llm_profiles: '' }));
    setSectionSuccess((prev) => ({ ...prev, llm_profiles: '' }));
    try {
      const response = await authFetch('/api/system-config', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ section: 'llm_profiles', data: payload }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data?.detail || '保存失败');
      const savedState = mergeSectionState('llm_profiles', data?.config || {}, sectionState);

      if (preserveDraftProfiles && savedProfileId) {
        setFormState((prev) => {
          const prevSection = prev.llm_profiles || { default_profile_id: '', profiles: [] };
          const savedProfiles = savedState.profiles || [];
          const savedById = new Map(savedProfiles.map((profile) => [profile.id, profile]));

          const mergedProfiles = (prevSection.profiles || []).map((profile) => {
            if (profile.id === savedProfileId) {
              return savedById.get(profile.id) || profile;
            }
            return profile;
          });

          savedProfiles.forEach((profile) => {
            if (!mergedProfiles.some((item) => item.id === profile.id)) {
              mergedProfiles.push(profile);
            }
          });

          return {
            ...prev,
            llm_profiles: {
              ...prevSection,
              default_profile_id: savedState.default_profile_id || prevSection.default_profile_id,
              profiles: mergedProfiles,
            },
          };
        });
      } else {
        setFormState((prev) => ({ ...prev, llm_profiles: savedState }));
      }

      setBaselineState((prev) => ({ ...prev, llm_profiles: savedState }));
      setSectionStatuses((prev) => ({ ...prev, llm_profiles: { ...EMPTY_STATUS } }));
      setSectionSuccess((prev) => ({ ...prev, llm_profiles: successMessage }));
      return true;
    } catch (err) {
      setSectionErrors((prev) => ({ ...prev, llm_profiles: err?.message || '保存失败' }));
      return false;
    } finally {
      setBusySectionActions((prev) => ({ ...prev, llm_profiles: null }));
    }
  };

  const updateModelProfile = (index, fieldName, value) => {
    if (isReadOnly || busySectionActions.llm_profiles) return;
    setFormState((prev) => {
      const profiles = [...(prev.llm_profiles.profiles || [])];
      const currentProfile = profiles[index] || {};
      const nextProfile = { ...currentProfile, [fieldName]: value };
      if (fieldName === 'provider') {
        const previousDefaultUrl = MODEL_PROVIDER_DEFAULT_BASE_URLS[currentProfile.provider];
        const nextDefaultUrl = MODEL_PROVIDER_DEFAULT_BASE_URLS[value];
        const currentBaseUrl = String(currentProfile.base_url || '').trim();
        if (nextDefaultUrl && (!currentBaseUrl || currentBaseUrl === previousDefaultUrl)) {
          nextProfile.base_url = nextDefaultUrl;
        }
      }
      profiles[index] = nextProfile;
      return { ...prev, llm_profiles: { ...prev.llm_profiles, profiles } };
    });
  };

  const addModelProfile = () => {
    if (isReadOnly || busySectionActions.llm_profiles) return;
    setFormState((prev) => {
      const nextIndex = (prev.llm_profiles.profiles || []).length + 1;
      const profile = createBlankModelProfile(nextIndex);
      const profiles = [profile, ...(prev.llm_profiles.profiles || [])];
      return {
        ...prev,
        llm_profiles: {
          ...prev.llm_profiles,
          default_profile_id: prev.llm_profiles.default_profile_id || profile.id,
          profiles,
        },
      };
    });
    setExpandedModelProfiles((prev) => {
      const next = { 0: true };
      Object.entries(prev).forEach(([key, value]) => {
        const itemIndex = Number(key);
        if (Number.isNaN(itemIndex)) return;
        next[itemIndex + 1] = value;
      });
      return next;
    });
  };

  const requestRemoveModelProfile = (index) => {
    if (isReadOnly || busySectionActions.llm_profiles) return;
    const profile = formStateRef.current.llm_profiles.profiles?.[index];
    if (!profile) return;
    setPendingModelDelete({
      index,
      name: profile.name || profile.model || profile.id || `模型 ${index + 1}`,
      model: profile.model || '',
      isDefault: formStateRef.current.llm_profiles.default_profile_id === profile.id,
    });
  };

  const confirmRemoveModelProfile = async () => {
    if (isReadOnly || !pendingModelDelete || busySectionActions.llm_profiles) return;
    const index = pendingModelDelete.index;
    const currentState = formStateRef.current.llm_profiles;
    const profiles = (currentState.profiles || []).filter((_, itemIndex) => itemIndex !== index);
    const currentDefault = currentState.default_profile_id;
    const nextState = {
      ...currentState,
      default_profile_id: profiles.some((profile) => profile.id === currentDefault)
        ? currentDefault
        : (profiles[0]?.id || ''),
      profiles,
    };
    const saved = await saveModelProfilesSection(nextState, '模型已删除', 'delete-profile');
    if (!saved) return;
    setPendingModelDelete(null);
    setExpandedModelProfiles((prev) => {
      const next = {};
      Object.entries(prev).forEach(([key, value]) => {
        const itemIndex = Number(key);
        if (Number.isNaN(itemIndex) || itemIndex === index) return;
        next[itemIndex > index ? itemIndex - 1 : itemIndex] = value;
      });
      return next;
    });
  };

  const setDefaultModelProfile = async (profileId) => {
    if (isReadOnly || busySectionActions.llm_profiles) return;
    const currentState = formStateRef.current.llm_profiles;
    await saveModelProfilesSection(
      { ...currentState, default_profile_id: profileId },
      '默认模型已更新',
      'default-profile'
    );
  };

  const saveSingleModelProfile = async (index) => {
    if (isReadOnly || busySectionActions.llm_profiles) return;
    const currentState = formStateRef.current.llm_profiles;
    const targetProfile = currentState?.profiles?.[index];
    if (!targetProfile) return;

    const baselineProfiles = baselineState.llm_profiles?.profiles || [];
    const baselineById = new Map(baselineProfiles.map((profile) => [profile.id, profile]));

    // 单模型保存：仅提交当前模型 + 其他已落库模型的 baseline，
    // 避免把其它未确认编辑一并持久化。
    const payloadProfiles = (currentState.profiles || []).reduce((acc, profile, profileIndex) => {
      if (profileIndex === index) {
        acc.push(profile);
        return acc;
      }
      const baselineProfile = baselineById.get(profile.id);
      if (baselineProfile) acc.push(baselineProfile);
      return acc;
    }, []);

    const payloadProfileIds = new Set(payloadProfiles.map((profile) => profile.id));
    const fallbackDefault = payloadProfiles.find((profile) => profile.enabled)?.id || payloadProfiles[0]?.id || '';
    const payloadState = {
      ...currentState,
      default_profile_id: payloadProfileIds.has(currentState.default_profile_id)
        ? currentState.default_profile_id
        : fallbackDefault,
      profiles: payloadProfiles,
    };

    const saved = await saveModelProfilesSection(
      payloadState,
      '模型已保存',
      `save-profile-${index}`,
      { preserveDraftProfiles: true, savedProfileId: targetProfile.id }
    );
    if (saved) {
      setExpandedModelProfiles((prev) => ({ ...prev, [index]: false }));
    }
  };

  const testSingleModelProfile = async (index) => {
    if (isReadOnly || busySectionActions.llm_profiles) return;
    const currentState = formStateRef.current.llm_profiles;
    const profile = currentState.profiles?.[index];
    if (!profile) return;
    setPageFeedback(null);
    setBusySectionActions((prev) => ({ ...prev, llm_profiles: `test-profile-${index}` }));
    setSectionErrors((prev) => ({ ...prev, llm_profiles: '' }));
    setSectionSuccess((prev) => ({ ...prev, llm_profiles: '' }));
    try {
      const response = await authFetch('/api/system-config/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          section: 'llm_profiles',
          data: {
            ...buildPayload('llm_profiles', currentState),
            profile_id: profile.id,
          },
        }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data?.detail || '校验失败');
      const nextStatus = { success: !!data?.success, message: data?.message || '', tested_at: data?.tested_at || new Date().toISOString() };
      setSectionStatuses((prev) => ({ ...prev, llm_profiles: nextStatus }));
      if (nextStatus.success) {
        setSectionSuccess((prev) => ({ ...prev, llm_profiles: nextStatus.message || '模型校验成功' }));
      } else {
        setSectionErrors((prev) => ({ ...prev, llm_profiles: nextStatus.message || '模型校验失败' }));
      }
    } catch (err) {
      setSectionErrors((prev) => ({ ...prev, llm_profiles: err?.message || '模型校验失败' }));
    } finally {
      setBusySectionActions((prev) => ({ ...prev, llm_profiles: null }));
    }
  };

  const toggleModelProfileExpanded = (index) => {
    setExpandedModelProfiles((prev) => ({ ...prev, [index]: !prev[index] }));
  };

  const renderField = (section, field, layout = 'single') => {
    const meta = SECTION_META[section];
    const state = formState[section];
    const inputId = `${section}-${field.name}`;
    const secretField = meta.secretField;
    const hasSecret = secretField ? state[`has_${secretField}`] : false;
    const value = state[field.name];
    const fullWidth = layout === 'single' || field.fullWidth;
    const isSectionBusy = isReadOnly || !!busySectionActions[section];

    if (field.type === 'checkbox') {
      return (
        <div className={`workspace-field settings-toggle-field ${fullWidth ? 'full-width' : ''}`} key={field.name}>
          <label className={`settings-toggle-row ${value ? 'is-on' : ''} ${isSectionBusy ? 'is-disabled' : ''}`} htmlFor={inputId}>
            <span className="settings-toggle-copy">
              <strong>{field.label}</strong>
              <span>{value ? field.enabledText || '已启用' : field.disabledText || '未启用'}</span>
            </span>
            <span className="settings-toggle-control">
              <span className={`settings-toggle-state ${value ? 'is-on' : ''}`}>{value ? '已启用' : '未启用'}</span>
              <span className="settings-toggle-switch">
                <input
                  id={inputId}
                  type="checkbox"
                  checked={!!value}
                  disabled={isSectionBusy}
                  onChange={(event) => handleFieldChange(section, field.name, event.target.checked)}
                />
                <span className="settings-toggle-slider" aria-hidden="true" />
              </span>
            </span>
          </label>
        </div>
      );
    }

    if (field.type === 'select') {
      return (
        <label className={`workspace-field ${fullWidth ? 'full-width' : ''}`} key={field.name} htmlFor={inputId}>
          <span>{field.label}</span>
          <select
            id={inputId}
            value={value}
            disabled={isSectionBusy}
            onChange={(event) => handleFieldChange(section, field.name, event.target.value)}
          >
            {(field.options || []).map((option) => (
              <option key={option.value} value={option.value}>{option.label}</option>
            ))}
          </select>
        </label>
      );
    }

    const isPassword = field.type === 'password';
    const showSecret = !!visibleSecrets[section];

    return (
      <label className={`workspace-field ${fullWidth ? 'full-width' : ''}`} key={field.name} htmlFor={inputId}>
        <span>{field.label}</span>
        <div className={isPassword ? 'settings-password-wrap' : ''}>
          <input
            id={inputId}
            type={isPassword && !showSecret ? 'password' : field.type || 'text'}
            step={field.step}
            value={value}
            disabled={isSectionBusy}
            className={isPassword && hasSecret ? 'settings-secret-input configured' : ''}
            placeholder={getFieldPlaceholder(field, hasSecret)}
            onChange={(event) => handleFieldChange(section, field.name, event.target.value)}
          />
          {isPassword && (
            <button
              className="settings-password-toggle"
              type="button"
              disabled={isSectionBusy}
              aria-label={showSecret ? '隐藏密钥/密码' : '显示密钥/密码'}
              onClick={() => setVisibleSecrets((prev) => ({ ...prev, [section]: !prev[section] }))}
            >
              {showSecret ? <EyeOff size={18} /> : <Eye size={18} />}
            </button>
          )}
        </div>
      </label>
    );
  };

  const renderModelProfilesEditor = () => {
    const section = 'llm_profiles';
    const state = formState[section];
    const profiles = state.profiles || [];
    const activeAction = busySectionActions[section] || '';
    const isBusy = isReadOnly || !!activeAction;

    return (
      <div className="model-profiles-editor">
        {sectionErrors[section] && <div className="system-settings-message error" style={{ marginBottom: '1.5rem' }}>{sectionErrors[section]}</div>}
        {sectionSuccess[section] && <div className="system-settings-message success" style={{ marginBottom: '1.5rem' }}>{sectionSuccess[section]}</div>}
        {profiles.length === 0 ? (
          <div className="system-settings-message warning">暂无模型配置，请添加至少一个模型。</div>
        ) : profiles.map((profile, index) => {
          const profileKey = profile.id || `profile-${index}`;
          const isDefault = state.default_profile_id === profile.id;
          const isExpanded = !!expandedModelProfiles[index];
          const providerLabel = MODEL_PROVIDER_LABELS[profile.provider] || profile.provider || 'OpenAI Compatible';
          return (
            <div className={`model-profile-card ${isDefault ? 'is-default' : ''} ${isExpanded ? 'is-expanded' : ''}`} key={profileKey}>
              <div className="model-profile-card-header">
                <button className="model-profile-summary" type="button" onClick={() => toggleModelProfileExpanded(index)} aria-expanded={isExpanded}>
                  <span className={`model-profile-expand-icon ${isExpanded ? 'is-open' : ''}`} aria-hidden="true">
                    <ChevronDown size={18} />
                  </span>
                  <span className="model-profile-main">
                    <span className="model-profile-title-row">
                      <strong>{profile.name || profile.model || profile.id || `模型 ${index + 1}`}</strong>
                      {isDefault && <span className="model-profile-default-badge">默认</span>}
                    </span>
                    <span className="model-profile-meta">
                      <span>{providerLabel}</span>
                      <span>{profile.model || '未填写模型 ID'}</span>
                      <span>{profile.base_url || '未填写基础 URL'}</span>
                    </span>
                  </span>
                </button>
                <div className="model-profile-actions">
                  <button className="btn btn-secondary settings-action-btn" onClick={() => toggleModelProfileExpanded(index)} disabled={isBusy} type="button">
                    <span>{isExpanded ? '收起' : '编辑'}</span>
                  </button>
                  <button className="btn btn-primary settings-action-btn" onClick={() => saveSingleModelProfile(index)} disabled={isBusy} type="button">
                    {activeAction === `save-profile-${index}` ? <Loader2 size={16} className="spin" /> : <Save size={16} />}
                    <span>保存</span>
                  </button>
                  <button className="btn btn-secondary settings-action-btn" onClick={() => testSingleModelProfile(index)} disabled={isBusy || !profile.id} type="button">
                    {activeAction === `test-profile-${index}` ? <Loader2 size={16} className="spin" /> : <RefreshCw size={16} />}
                    <span>校验</span>
                  </button>
                  <button className="btn btn-secondary settings-action-btn" onClick={() => setDefaultModelProfile(profile.id)} disabled={isBusy || isDefault || !profile.id} type="button">
                    {activeAction === 'default-profile' ? <Loader2 size={16} className="spin" /> : null}
                    <span>{isDefault ? '默认模型' : '设为默认'}</span>
                  </button>
                  <button className="btn btn-secondary settings-action-btn" onClick={() => requestRemoveModelProfile(index)} disabled={isBusy || profiles.length <= 1} type="button">
                    <Trash2 size={16} />
                    <span>删除</span>
                  </button>
                </div>
              </div>

              {isExpanded && (
                <div className="settings-fields-grid model-profile-form is-double">
                  <label className="workspace-field" htmlFor={`llm-profile-${index}-name`}>
                    <span>显示名称</span>
                    <input id={`llm-profile-${index}-name`} value={profile.name} disabled={isBusy} onChange={(event) => updateModelProfile(index, 'name', event.target.value)} />
                  </label>
                  <label className="workspace-field" htmlFor={`llm-profile-${index}-provider`}>
                    <span>Provider</span>
                    <select id={`llm-profile-${index}-provider`} value={profile.provider} disabled={isBusy} onChange={(event) => updateModelProfile(index, 'provider', event.target.value)}>
                      {MODEL_PROVIDER_OPTIONS.map((option) => (
                        <option key={option.value} value={option.value}>{option.label}</option>
                      ))}
                    </select>
                  </label>
                  <label className="workspace-field" htmlFor={`llm-profile-${index}-model`}>
                    <span>模型 ID</span>
                    <input id={`llm-profile-${index}-model`} value={profile.model} disabled={isBusy} onChange={(event) => updateModelProfile(index, 'model', event.target.value)} />
                  </label>
                  <label className="workspace-field full-width" htmlFor={`llm-profile-${index}-base-url`}>
                    <span>基础 URL</span>
                    <input id={`llm-profile-${index}-base-url`} value={profile.base_url} disabled={isBusy} onChange={(event) => updateModelProfile(index, 'base_url', event.target.value)} />
                  </label>
                  <label className="workspace-field full-width" htmlFor={`llm-profile-${index}-api-key`}>
                    <span>API Key</span>
                    <input
                      id={`llm-profile-${index}-api-key`}
                      type="password"
                      value={profile.api_key}
                      disabled={isBusy}
                      placeholder={profile.has_api_key ? '••••••' : '待配置'}
                      onChange={(event) => updateModelProfile(index, 'api_key', event.target.value)}
                    />
                  </label>
                  <label className="workspace-field" htmlFor={`llm-profile-${index}-timeout`}>
                    <span>超时（秒）</span>
                    <input id={`llm-profile-${index}-timeout`} type="number" step="1" value={profile.timeout} disabled={isBusy} onChange={(event) => updateModelProfile(index, 'timeout', event.target.value)} />
                  </label>
                  <label className="workspace-field" htmlFor={`llm-profile-${index}-max-tokens`}>
                    <span>最大 Tokens</span>
                    <input id={`llm-profile-${index}-max-tokens`} type="number" step="1" value={profile.max_tokens} disabled={isBusy} onChange={(event) => updateModelProfile(index, 'max_tokens', event.target.value)} />
                  </label>
                  <label className="workspace-field" htmlFor={`llm-profile-${index}-temperature`}>
                    <span>Temperature</span>
                    <input id={`llm-profile-${index}-temperature`} type="number" step="0.1" value={profile.temperature} disabled={isBusy} onChange={(event) => updateModelProfile(index, 'temperature', event.target.value)} />
                  </label>
                </div>
              )}

            </div>
          );
        })}
      </div>
    );
  };
  const renderConnectionsView = () => {
    const section = activeConnectionSection;
    const meta = SECTION_META[section];
    const state = formState[section];
    const status = sectionStatuses[section];
    const activeAction = busySectionActions[section] || '';
    const isBusy = isReadOnly || !!activeAction;
    const isDirty = isSectionDirty(section);
    const summary = getConnectionSectionStatus(section, state, status, isDirty);
    const fieldGroups = (meta.fieldGroups || [{ title: '', fields: meta.fields }]).map((group) => ({
      ...group,
      fields: (group.fields || [])
        .map((name) => meta.fields.find((field) => field.name === name))
        .filter((field) => field && shouldShowField(section, field, state)),
    })).filter((group) => group.fields.length > 0);

    return (
      <div className="settings-connections-shell">
        <section className="system-settings-panel settings-connection-detail">
          <div className="settings-panel-header">
            <div>
              <h3>{meta.detailTitle}</h3>
              <p>{meta.description}</p>
            </div>
            {section === 'llm_profiles' && (
              <button className="btn btn-secondary settings-action-btn" onClick={addModelProfile} disabled={isBusy} type="button">
                <Bot size={16} />
                <span>添加模型</span>
              </button>
            )}
          </div>

          {section !== 'llm_profiles' && sectionErrors[section] && <div className="system-settings-message error">{sectionErrors[section]}</div>}
          {section !== 'llm_profiles' && sectionSuccess[section] && <div className="system-settings-message success">{sectionSuccess[section]}</div>}

          {section === 'llm_profiles' ? null : (
            <div className="settings-field-groups">
              {fieldGroups.map((group) => (
                <div className="settings-field-group" key={group.title || `${section}-group`}>
                  {group.title && <h4 className="settings-field-group-title">{group.title}</h4>}
                  <div className={`settings-fields-grid ${meta.layout === 'double' ? 'is-double' : 'is-single'}`}>
                    {group.fields.map((field) => renderField(section, field, meta.layout === 'double' ? 'double' : 'single'))}
                  </div>
                </div>
              ))}
            </div>
          )}

          {section === 'llm_profiles' && renderModelProfilesEditor()}

          {section !== 'llm_profiles' && (
            <div className="settings-detail-footer">
              <div className="settings-detail-status-copy">
                {summary.detail && <div className={`settings-detail-meta tone-${summary.tone}`}>{summary.detail}</div>}
              </div>
              <div className="settings-detail-actions">
                <div className="settings-actions">
                  <button className="btn btn-primary settings-action-btn" onClick={() => handleSave(section)} disabled={isBusy} type="button">
                    {activeAction === 'save' ? <Loader2 size={16} className="spin" /> : <Save size={16} />}
                    <span>保存配置</span>
                  </button>
                  <button className="btn btn-secondary settings-action-btn" onClick={() => handleTestConnection(section)} disabled={isBusy} type="button">
                    {activeAction === 'test' ? <Loader2 size={16} className="spin" /> : <RefreshCw size={16} />}
                    <span>校验配置</span>
                  </button>
                  {meta.secretField && state[`has_${meta.secretField}`] && shouldShowClearSecretAction(section, state) && (
                    <button className="btn btn-secondary settings-action-btn" onClick={() => handleClearSecret(section)} disabled={isBusy} type="button">
                      {activeAction === 'clear' ? <Loader2 size={16} className="spin" /> : <Trash2 size={16} />}
                      <span>清空已保存密钥</span>
                    </button>
                  )}
                </div>
                <div className="settings-footnote settings-footnote-right">保存后会刷新后端运行时，新请求会立即使用新配置。</div>
              </div>
            </div>
          )}
        </section>
      </div>
    );
  };

  const renderRuntimeView = () => (
    <div className="settings-section-group">
      <section className={`system-settings-panel settings-advanced-panel ${isAdvancedRuntimeOpen ? 'is-open' : ''}`}>
        <div className="settings-advanced-summary">
          <div>
            <h3>高级运行参数</h3>
            <p>这些参数会影响主循环、子智能体并发和上下文裁剪。日常配置模型和观测时不需要修改。</p>
          </div>
          <button
            className="btn btn-secondary settings-action-btn settings-advanced-toggle"
            type="button"
            onClick={() => setIsAdvancedRuntimeOpen((prev) => !prev)}
          >
            <ChevronDown size={16} className={isAdvancedRuntimeOpen ? 'rotated' : ''} />
            <span>{isAdvancedRuntimeOpen ? '收起' : '展开高级设置'}</span>
          </button>
        </div>
      </section>
      {!isAdvancedRuntimeOpen ? null : (
      <div className="settings-runtime-shell">
        {runtimeEntries.map(([section, meta]) => {
          const state = formState[section];
          const secretField = meta.secretField;
          const hasSecret = secretField ? state[`has_${secretField}`] : false;
          const activeAction = busySectionActions[section] || '';
          const isBusy = !!activeAction;
          const fieldGroups = (meta.fieldGroups || [{ title: '', fields: meta.fields }]).map((fieldGroup) => ({
            ...fieldGroup,
            fields: fieldGroup.fields || [],
          }));

          return (
            <section className="system-settings-panel settings-panel settings-panel-runtime" key={section}>
              <div className="settings-panel-header">
                <div>
                  <h3>{meta.detailTitle}</h3>
                  <p>{meta.description}</p>
                </div>
              </div>

              {sectionErrors[section] && <div className="system-settings-message error">{sectionErrors[section]}</div>}
              {sectionSuccess[section] && <div className="system-settings-message success">{sectionSuccess[section]}</div>}

              <div className="settings-field-groups">
                {fieldGroups.map((fieldGroup) => (
                  <div className={`settings-field-group ${fieldGroup.tone ? `tone-${fieldGroup.tone}` : ''}`} key={fieldGroup.title || `${section}-group`}>
                    {(fieldGroup.title || fieldGroup.description) && (
                      <div className="settings-field-group-head">
                        {fieldGroup.title && <h4 className="settings-field-group-title">{fieldGroup.title}</h4>}
                        {fieldGroup.description && <p className="settings-field-group-description">{fieldGroup.description}</p>}
                      </div>
                    )}
                    <div className="settings-field-group-body">
                      <div className={`settings-fields-grid ${fieldGroup.layout === 'single' ? 'is-single' : 'is-double'}`}>
                        {fieldGroup.fields.map((field) => renderField(section, field, fieldGroup.layout === 'single' ? 'single' : 'double'))}
                      </div>
                    </div>
                  </div>
                ))}
              </div>

              <div className="settings-detail-footer">
                <div className="settings-detail-status-copy">
                  {sectionStatuses[section]?.tested_at && (
                    <div className={`settings-detail-meta tone-${sectionStatuses[section]?.success ? 'success' : 'warning'}`}>上次校验：{formatRelativeTime(sectionStatuses[section].tested_at)}</div>
                  )}
                </div>
                <div className="settings-detail-actions">
                  <div className="settings-actions">
                    <button className="btn btn-primary settings-action-btn" onClick={() => handleSave(section)} disabled={isBusy} type="button">
                      {activeAction === 'save' ? <Loader2 size={16} className="spin" /> : <Save size={16} />}
                      <span>保存配置</span>
                    </button>
                    <button className="btn btn-secondary settings-action-btn" onClick={() => handleTestConnection(section)} disabled={isBusy} type="button">
                      {activeAction === 'test' ? <Loader2 size={16} className="spin" /> : <RefreshCw size={16} />}
                      <span>校验配置</span>
                    </button>
                    {secretField && hasSecret && (
                      <button className="btn btn-secondary settings-action-btn" onClick={() => handleClearSecret(section)} disabled={isBusy} type="button">
                        {activeAction === 'clear' ? <Loader2 size={16} className="spin" /> : <Trash2 size={16} />}
                        <span>清空已保存密钥</span>
                      </button>
                    )}
                  </div>
                  <div className="settings-footnote settings-footnote-right">保存后会刷新后端运行时，新请求会立即使用新配置。</div>
                </div>
              </div>
            </section>
          );
        })}
      </div>
      )}
    </div>
  );

  const renderRuntimeTab = () => {
    const RuntimeIcon = SECTION_META.agent.icon;
    return (
      <button
        className={`settings-inline-tab ${activeGroupKey === 'runtime' ? 'active' : ''}`}
        type="button"
        role="tab"
        aria-selected={activeGroupKey === 'runtime'}
        onClick={() => setActiveGroupKey('runtime')}
      >
        <RuntimeIcon className="settings-inline-tab-icon" size={16} strokeWidth={2} aria-hidden="true" />
        <span className="settings-inline-tab-label">高级设置</span>
      </button>
    );
  };

  const renderHeaderActions = () => (
    <div className="settings-header-actions">
      <button className="btn btn-secondary settings-action-btn" onClick={handleDownloadTemplate} disabled={isReadOnly || loading || !!topAction} type="button">
        {topAction === 'download' ? <Loader2 size={16} className="spin" /> : <Download size={16} />}
        <span>下载模板</span>
      </button>
      <button className="btn btn-secondary settings-action-btn" onClick={handleImportButtonClick} disabled={isReadOnly || loading || !!topAction} type="button">
        {topAction === 'import' ? <Loader2 size={16} className="spin" /> : <Upload size={16} />}
        <span>导入配置</span>
      </button>
      <button className="btn btn-secondary settings-action-btn" onClick={loadConfig} disabled={loading || !!topAction} type="button">
        {loading ? <Loader2 size={16} className="spin" /> : <RefreshCw size={16} />}
        <span>刷新</span>
      </button>
    </div>
  );

  return (
    <div className={`system-settings-page ${embedded ? 'is-embedded' : ''}`}>
      <input ref={importInputRef} type="file" accept=".json,application/json" className="settings-hidden-file-input" onChange={handleImportFileSelected} disabled={isReadOnly} />

      {!embedded && (
        <div className="system-settings-header">
          <div className="system-settings-heading">
            <button className="system-settings-back" onClick={onBack} type="button" aria-label="返回对话" title="返回对话">
              <ArrowLeft size={16} />
            </button>
            <div>
              <h2>系统设置</h2>
              <p>配置 `onto-agent` 的模型、观测、路径、鉴权与 Agent 运行参数。</p>
            </div>
          </div>
          {renderHeaderActions()}
        </div>
      )}
      {loadingError && <div className="system-settings-message error">{loadingError}</div>}
      {pageFeedback && <div className={`system-settings-message ${pageFeedback.type}`}>{pageFeedback.message}</div>}
      {isReadOnly && !loadingError && (
        <div className="system-settings-message warning">当前为只读访问模式。普通用户可查看配置，但不能修改、校验、导入或删除。</div>
      )}

      <div className="settings-tabs">
        <div className={`settings-inline-tab-row ${embedded ? 'is-embedded' : ''}`}>
          <div className="settings-inline-tabs" role="tablist" aria-label="系统配置分组">
            {CONNECTION_SECTIONS.map((sectionKey) => {
              const itemMeta = SECTION_META[sectionKey];
              const SectionIcon = itemMeta.icon;
              const isActive = activeGroupKey === 'connections' && activeConnectionSection === sectionKey;
              return (
                <button
                  key={sectionKey}
                  className={`settings-inline-tab ${isActive ? 'active' : ''}`}
                  type="button"
                  role="tab"
                  aria-selected={isActive}
                  onClick={() => {
                    setActiveGroupKey('connections');
                    setActiveConnectionSection(sectionKey);
                  }}
                >
                  <SectionIcon className="settings-inline-tab-icon" size={16} strokeWidth={2} aria-hidden="true" />
                  <span className="settings-inline-tab-label">{itemMeta.title}</span>
                </button>
              );
            })}
            {renderRuntimeTab()}
          </div>
          {embedded && renderHeaderActions()}
        </div>
        {activeGroupKey === 'connections' ? renderConnectionsView() : renderRuntimeView()}
      </div>

      {pendingImport && (
        <div className="modal-overlay" role="presentation" onClick={(event) => { if (event.target === event.currentTarget) setPendingImport(null); }}>
          <div className="modal-content settings-import-modal" role="dialog" aria-modal="true" aria-labelledby="settings-import-title">
            <div className="modal-header">
              <h2 id="settings-import-title">确认导入配置</h2>
            </div>
            <div className="modal-body">
              <div className="settings-import-copy">
                <p>文件：{pendingImport.fileName}</p>
                <p>导入会覆盖文件中包含的已有配置模块。</p>
              </div>
              <div className="settings-import-sections">
                <span>本次将导入并覆盖的配置模块：</span>
                <div className="settings-import-chip-list">
                  {pendingImport.sections.map((section) => (
                    <span className="settings-import-chip" key={section}>{section}</span>
                  ))}
                </div>
              </div>
            </div>
            <div className="modal-footer">
              <button className="modal-btn-cancel" onClick={() => setPendingImport(null)} disabled={topAction === 'import'} type="button">取消</button>
              <button className="modal-btn-confirm" onClick={handleConfirmImport} disabled={topAction === 'import'} type="button">
                {topAction === 'import' ? <Loader2 size={16} className="spin" /> : <Upload size={16} />}
                <span>确认导入</span>
              </button>
            </div>
          </div>
        </div>
      )}

      {pendingModelDelete && (
        <div
          className="modal-overlay"
          role="presentation"
          onClick={(event) => {
            if (busySectionActions.llm_profiles === 'delete-profile') return;
            if (event.target === event.currentTarget) setPendingModelDelete(null);
          }}
        >
          <div className="modal-content settings-delete-modal" role="dialog" aria-modal="true" aria-labelledby="settings-delete-model-title">
            <div className="modal-header">
              <h2 id="settings-delete-model-title">确认删除模型</h2>
            </div>
            <div className="modal-body">
              <div className="settings-import-copy">
                <p>即将删除：{pendingModelDelete.name}</p>
                {pendingModelDelete.model && <p>模型 ID：{pendingModelDelete.model}</p>}
                <p>确认后会立即删除并保存到后端。</p>
                {pendingModelDelete.isDefault && <p>该模型当前是默认模型，删除后会自动选择列表中的其他模型作为默认模型。</p>}
              </div>
            </div>
            <div className="modal-footer">
              <button className="modal-btn-cancel" onClick={() => setPendingModelDelete(null)} disabled={busySectionActions.llm_profiles === 'delete-profile'} type="button">取消</button>
              <button className="modal-btn-confirm danger" onClick={confirmRemoveModelProfile} disabled={busySectionActions.llm_profiles === 'delete-profile'} type="button">
                {busySectionActions.llm_profiles === 'delete-profile' ? <Loader2 size={16} className="spin" /> : <Trash2 size={16} />}
                <span>确认删除</span>
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default SystemSettings;
