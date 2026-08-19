import { config } from '../config.js';

export type LlmSettingSource = 'user' | 'default_snapshot' | 'settings' | 'env';
export interface LlmSettings {
  baseUrl: string;
  model: string;
  apiKey: string;
  maxTokens: number;
  contextWindow: number;
}

export interface UserLlmSettingsSnapshot {
  baseUrl: string | null;
  model: string | null;
  apiKey: string | null;
  maxTokens: number | null;
  contextWindow: number | null;
}

export interface LegacyLlmSettingsSnapshot {
  baseUrl: string | null;
  model: string | null;
  apiKey: string | null;
  maxTokens: string | null;
  contextWindow: string | null;
}

interface ResolveLlmInput {
  username?: string;
  personal?: UserLlmSettingsSnapshot;
  assignedDefaults?: { main?: LlmSettings; task?: LlmSettings };
  legacy?: LegacyLlmSettingsSnapshot;
}

export function hasLlmProviderOverride(personal: UserLlmSettingsSnapshot | undefined): boolean {
  return Boolean(personal && [personal.baseUrl, personal.model, personal.apiKey]
    .some((value) => value !== null));
}

function normalizedMaxTokens(value: number | string | null | undefined, fallback = config.llm.maxTokens): number {
  const parsed = Number(value || fallback);
  return Number.isSafeInteger(parsed) && parsed >= 1 && parsed <= 1_000_000 ? parsed : fallback;
}

function normalizedContextWindow(
  value: number | string | null | undefined,
  fallback = config.llm.contextWindow,
): number {
  const parsed = Number(value || fallback);
  return Number.isSafeInteger(parsed) && parsed >= 1 ? parsed : fallback;
}

function fromAssignedDefault(
  settings: LlmSettings,
  personalMaxTokens?: number | null,
  personalContextWindow?: number | null,
): LlmSettings {
  return {
    baseUrl: settings.baseUrl.trim(),
    model: settings.model.trim(),
    apiKey: settings.apiKey,
    maxTokens: normalizedMaxTokens(personalMaxTokens, settings.maxTokens),
    contextWindow: normalizedContextWindow(personalContextWindow, settings.contextWindow),
  };
}

function deploymentMainSettings(
  personalMaxTokens: number | null | undefined,
  personalContextWindow: number | null | undefined,
  legacy: LegacyLlmSettingsSnapshot | undefined,
): LlmSettings {
  return {
    baseUrl: (legacy?.baseUrl || config.llm.baseUrl || '').trim(),
    model: (legacy?.model || config.llm.model || '').trim(),
    apiKey: (legacy?.apiKey || config.llm.apiKey || '').trim(),
    maxTokens: normalizedMaxTokens(
      personalMaxTokens ?? legacy?.maxTokens ?? config.llm.maxTokens,
      config.llm.maxTokens,
    ),
    contextWindow: normalizedContextWindow(
      personalContextWindow ?? legacy?.contextWindow ?? config.llm.contextWindow,
      config.llm.contextWindow,
    ),
  };
}

export function resolveLlmSource(input: ResolveLlmInput): LlmSettingSource {
  if (input.username) {
    if (hasLlmProviderOverride(input.personal)) return 'user';
    return input.assignedDefaults?.main ? 'default_snapshot' : 'env';
  }
  return input.legacy?.baseUrl !== null && input.legacy?.baseUrl !== undefined ? 'settings' : 'env';
}

// 平台模板只在注册/主动同步时复制为用户快照；运行时不得读取当前平台默认兜底。
export function resolveLlmSettings(input: ResolveLlmInput): LlmSettings {
  const { username, personal, assignedDefaults } = input;
  if (username && hasLlmProviderOverride(personal)) {
    return {
      baseUrl: (personal?.baseUrl ?? '').trim(),
      model: (personal?.model ?? '').trim(),
      apiKey: personal?.apiKey ?? '',
      maxTokens: normalizedMaxTokens(
        personal?.maxTokens ?? config.llm.maxTokens,
        config.llm.maxTokens,
      ),
      contextWindow: normalizedContextWindow(
        personal?.contextWindow ?? config.llm.contextWindow,
        config.llm.contextWindow,
      ),
    };
  }
  if (username && assignedDefaults?.main) {
    return fromAssignedDefault(assignedDefaults.main, personal?.maxTokens, personal?.contextWindow);
  }
  return deploymentMainSettings(
    personal?.maxTokens,
    personal?.contextWindow,
    username ? undefined : input.legacy,
  );
}

/**
 * 任务模型沿用个人 provider 覆盖；否则读取同步时固化的 task/main 快照，
 * 最后才读取部署 task/main。当前平台模板不参与运行时解析。
 */
export function resolveTaskLlmSettings(input: ResolveLlmInput): LlmSettings {
  const { username, personal, assignedDefaults } = input;
  if (username && hasLlmProviderOverride(personal)) return resolveLlmSettings(input);

  if (username && assignedDefaults?.task) {
    return fromAssignedDefault(assignedDefaults.task, personal?.maxTokens, personal?.contextWindow);
  }
  if (username && assignedDefaults?.main) {
    return fromAssignedDefault(assignedDefaults.main, personal?.maxTokens, personal?.contextWindow);
  }

  const deploymentTaskConfigured = Boolean(
    config.taskLlm.baseUrl || config.taskLlm.model || config.taskLlm.apiKey,
  );
  if (deploymentTaskConfigured) {
    return {
      baseUrl: config.taskLlm.baseUrl.trim(),
      model: config.taskLlm.model.trim(),
      apiKey: config.taskLlm.apiKey,
      maxTokens: normalizedMaxTokens(
        personal?.maxTokens ?? config.llm.maxTokens,
        config.llm.maxTokens,
      ),
      contextWindow: normalizedContextWindow(
        personal?.contextWindow ?? config.llm.contextWindow,
        config.llm.contextWindow,
      ),
    };
  }
  return deploymentMainSettings(
    personal?.maxTokens,
    personal?.contextWindow,
    username ? undefined : input.legacy,
  );
}
