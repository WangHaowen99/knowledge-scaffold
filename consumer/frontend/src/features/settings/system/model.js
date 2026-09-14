// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
export const SYSTEM_CONFIG_SECTIONS = Object.freeze([
  'llm',
  'llm_profiles',
  'storage',
  'auth',
  'agent',
  'memory',
  'web_search',
  'direct_execution',
  'skill_evolution',
  'ontology_evidence',
]);

export const SYSTEM_CONFIG_SECTION_LABELS = Object.freeze({
  llm: '旧版模型兼容配置',
  llm_profiles: '模型池',
  storage: '存储',
  auth: '鉴权',
  agent: 'Agent 参数',
  memory: '长期记忆',
  web_search: '联网搜索',
  direct_execution: '直接执行',
  skill_evolution: '技能进化',
  ontology_evidence: '本体证据',
});

export const EMPTY_TEST_STATUS = Object.freeze({
  success: null,
  message: '',
  tested_at: null,
});

const SUPPORTED_SECTION_SET = new Set(SYSTEM_CONFIG_SECTIONS);
const PREVIEW_SECRET_KEY =
  /(?:^|[_-])(?:api[_-]?key|secret(?:[_-]key)?|access[_-]token|refresh[_-]token|admin[_-]token|password|credential)(?:$|[_-])/i;
const SECRET_MASK = '••••••••';

function isPlainObject(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return false;
  }
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function cloneJsonValue(value) {
  if (Array.isArray(value)) {
    return value.map(cloneJsonValue);
  }
  if (!isPlainObject(value)) {
    return value;
  }
  return Object.fromEntries(
    Object.entries(value).map(([key, item]) => [key, cloneJsonValue(item)]),
  );
}

function isBlankSecret(value) {
  return value == null || (typeof value === 'string' && !value.trim());
}

function removeBlankSecret(target, field, path, removed) {
  if (
    !Object.prototype.hasOwnProperty.call(target, field) ||
    !isBlankSecret(target[field])
  ) {
    return;
  }
  delete target[field];
  removed.push([...path, field].join('.'));
}

function stripSecretPlaceholders(section, data, removed) {
  if (section === 'llm') {
    removeBlankSecret(data, 'api_key', [section], removed);
  }
  if (section === 'auth') {
    removeBlankSecret(data, 'auth_secret', [section], removed);
    removeBlankSecret(data, 'auth_server_admin_token', [section], removed);
  }
  if (section === 'web_search') {
    removeBlankSecret(data, 'api_key', [section], removed);
  }
  if (section === 'llm_profiles' && Array.isArray(data.profiles)) {
    data.profiles.forEach((profile, index) => {
      if (isPlainObject(profile)) {
        removeBlankSecret(
          profile,
          'api_key',
          [section, 'profiles', String(index)],
          removed,
        );
      }
    });
  }
}

function isEmptyModelTemplate(section) {
  if (!Array.isArray(section.profiles) || section.profiles.length !== 0) {
    return false;
  }
  const allowedKeys = new Set([
    'profiles',
    'default_main_profile_ref',
    'default_task_profile_ref',
  ]);
  return (
    Object.keys(section).every((key) => allowedKeys.has(key)) &&
    section.default_main_profile_ref == null &&
    section.default_task_profile_ref == null
  );
}

function maskPreviewValue(value, key = '') {
  const shouldMask =
    PREVIEW_SECRET_KEY.test(key) &&
    value !== undefined &&
    value !== null &&
    value !== '';
  if (shouldMask) {
    return SECRET_MASK;
  }
  if (Array.isArray(value)) {
    return value.map((item) => maskPreviewValue(item));
  }
  if (!isPlainObject(value)) {
    return value;
  }
  return Object.fromEntries(
    Object.entries(value).map(([childKey, childValue]) => [
      childKey,
      maskPreviewValue(childValue, childKey),
    ]),
  );
}

export function assertSystemConfigSection(section) {
  const normalized = String(section || '')
    .trim()
    .toLowerCase();
  if (!SUPPORTED_SECTION_SET.has(normalized)) {
    throw new Error(`不支持的系统配置 section：${normalized || '空值'}`);
  }
  return normalized;
}

export function maskSystemConfigSecrets(config) {
  return maskPreviewValue(cloneJsonValue(config));
}

export function prepareSystemConfigImport(rawConfig) {
  if (!isPlainObject(rawConfig)) {
    throw new Error('导入文件必须是 JSON 对象');
  }

  const unsupportedSections = Object.keys(rawConfig).filter(
    (section) => !SUPPORTED_SECTION_SET.has(section),
  );
  if (unsupportedSections.length) {
    throw new Error(
      `导入文件包含不支持的 section：${unsupportedSections.sort().join('、')}`,
    );
  }

  const sourceSections = SYSTEM_CONFIG_SECTIONS.filter((section) =>
    Object.prototype.hasOwnProperty.call(rawConfig, section),
  );
  if (!sourceSections.length) {
    throw new Error('导入文件未包含合法的系统配置 section');
  }

  const config = {};
  const removedSecretPaths = [];
  const omittedSections = [];
  sourceSections.forEach((section) => {
    const rawSection = rawConfig[section];
    if (!isPlainObject(rawSection)) {
      throw new Error(`${section} 必须是 JSON 对象`);
    }
    const nextSection = cloneJsonValue(rawSection);
    stripSecretPlaceholders(section, nextSection, removedSecretPaths);

    if (
      section === 'llm_profiles' &&
      Object.prototype.hasOwnProperty.call(nextSection, 'profiles')
    ) {
      if (!Array.isArray(nextSection.profiles)) {
        throw new Error('llm_profiles.profiles 必须是数组');
      }
      if (isEmptyModelTemplate(nextSection)) {
        omittedSections.push({
          section,
          reason: '模板中的空模型列表不会覆盖现有模型池',
        });
        return;
      }
      if (!nextSection.profiles.length) {
        throw new Error('llm_profiles.profiles 至少需要一个模型');
      }
    }
    config[section] = nextSection;
  });

  const sections = SYSTEM_CONFIG_SECTIONS.filter((section) =>
    Object.prototype.hasOwnProperty.call(config, section),
  );
  if (!sections.length) {
    throw new Error('导入文件只包含空模型模板，没有可覆盖的配置');
  }

  return {
    config,
    sections,
    omittedSections,
    removedSecretPaths,
    maskedConfig: maskSystemConfigSecrets(config),
  };
}

export function parseSystemConfigImport(text) {
  let rawConfig;
  try {
    rawConfig = JSON.parse(String(text || ''));
  } catch {
    throw new Error('导入文件不是有效的 JSON');
  }
  return prepareSystemConfigImport(rawConfig);
}

export function configValuesEqual(left, right) {
  return JSON.stringify(left ?? {}) === JSON.stringify(right ?? {});
}

export function createSystemConfigModuleState(data = {}, readOnly = false) {
  const source = cloneJsonValue(data);
  return {
    baseline: source,
    draft: cloneJsonValue(source),
    readOnly: Boolean(readOnly),
    busyAction: '',
    error: '',
    success: '',
    testStatus: { ...EMPTY_TEST_STATUS },
  };
}

const resetSystemConfigFeedback = (current, draft) => ({
  ...current,
  draft,
  error: '',
  success: '',
  testStatus: { ...EMPTY_TEST_STATUS },
});

const SYSTEM_CONFIG_STATE_REDUCERS = {
  SOURCE_CHANGED: (current, event) => createSystemConfigModuleState(
    event.data,
    event.readOnly ?? current.readOnly,
  ),
  READ_ONLY_CHANGED: (current, event) => ({
    ...current,
    readOnly: Boolean(event.value),
  }),
  PATCH_DRAFT: (current, event) => resetSystemConfigFeedback(current, {
    ...current.draft,
    ...(event.data || {}),
  }),
  REPLACE_DRAFT: (current, event) => resetSystemConfigFeedback(
    current,
    cloneJsonValue(event.data || {}),
  ),
  RESET_DRAFT: (current) => resetSystemConfigFeedback(
    current,
    cloneJsonValue(current.baseline),
  ),
  BEGIN_ACTION: (current, event) => {
    if (current.readOnly) {
      return current;
    }
    return {
      ...current,
      busyAction: String(event.action || ''),
      error: '',
      success: '',
    };
  },
  SAVE_SUCCEEDED: (current, event) => {
    const saved = cloneJsonValue(event.data || {});
    return {
      ...current,
      baseline: saved,
      draft: cloneJsonValue(saved),
      busyAction: '',
      error: '',
      success: String(event.message || '配置已保存'),
      testStatus: { ...EMPTY_TEST_STATUS },
    };
  },
  TEST_FINISHED: (current, event) => {
    const status = {
      success: Boolean(event.result?.success),
      message: String(event.result?.message || ''),
      tested_at: event.result?.tested_at || new Date().toISOString(),
    };
    return {
      ...current,
      busyAction: '',
      error: status.success ? '' : status.message || '连接测试失败',
      success: status.success ? status.message || '连接测试成功' : '',
      testStatus: status,
    };
  },
  ACTION_FAILED: (current, event) => ({
    ...current,
    busyAction: '',
    error: String(event.error || '操作失败'),
    success: '',
  }),
};

export function reduceSystemConfigModuleState(state, event) {
  const current = state || createSystemConfigModuleState();
  const reducer = SYSTEM_CONFIG_STATE_REDUCERS[event?.type];
  return reducer ? reducer(current, event) : current;
}

export function formatSystemConfigTestTime(value, locale = 'zh-CN') {
  if (!value) {
    return '';
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return String(value);
  }
  return new Intl.DateTimeFormat(locale, {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(date);
}
