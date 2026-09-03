import { config } from '../config.js';
import {
  emptyLlmAdvancedSettings,
  normalizeLlmAdvancedSettings,
  type LlmAdvancedSettings,
} from './llm-extra-params.js';

export type LlmSettingSource = 'user' | 'platform_default' | 'default_snapshot' | 'main' | 'settings' | 'env';
export interface LlmSettings extends LlmAdvancedSettings {
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
  thinkingMode: LlmAdvancedSettings['thinkingMode'] | null;
  reasoningEffort: LlmAdvancedSettings['reasoningEffort'] | null;
  extraBodyJson: string | null;
}

export interface LegacyLlmSettingsSnapshot {
  baseUrl: string | null;
  model: string | null;
  apiKey: string | null;
  maxTokens: string | null;
  contextWindow: string | null;
  thinkingMode: string | null;
  reasoningEffort: string | null;
  extraBodyJson: string | null;
}

interface ResolveLlmInput {
  username?: string;
  personal?: UserLlmSettingsSnapshot;
  personalTask?: UserLlmSettingsSnapshot;
  assignedDefaults?: { main?: LlmSettings; task?: LlmSettings };
  platformDefaults?: { main?: LlmSettings; task?: LlmSettings };
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

function advancedFrom(
  value: Partial<UserLlmSettingsSnapshot | LegacyLlmSettingsSnapshot | LlmSettings> | undefined,
  fallback: LlmAdvancedSettings = emptyLlmAdvancedSettings(),
): LlmAdvancedSettings {
  return normalizeLlmAdvancedSettings({
    thinkingMode: value?.thinkingMode ?? fallback.thinkingMode,
    reasoningEffort: value?.reasoningEffort ?? fallback.reasoningEffort,
    extraBodyJson: value?.extraBodyJson ?? fallback.extraBodyJson,
  });
}

function fromAssignedDefault(
  settings: LlmSettings,
  personalMaxTokens?: number | null,
  personalContextWindow?: number | null,
  personalAdvanced?: Partial<UserLlmSettingsSnapshot>,
): LlmSettings {
  return {
    baseUrl: settings.baseUrl.trim(),
    model: settings.model.trim(),
    apiKey: settings.apiKey,
    maxTokens: normalizedMaxTokens(personalMaxTokens, settings.maxTokens),
    contextWindow: normalizedContextWindow(personalContextWindow, settings.contextWindow),
    ...advancedFrom(personalAdvanced, settings),
  };
}

function deploymentMainSettings(
  personalMaxTokens: number | null | undefined,
  personalContextWindow: number | null | undefined,
  legacy: LegacyLlmSettingsSnapshot | undefined,
  personalAdvanced?: Partial<UserLlmSettingsSnapshot>,
): LlmSettings {
  const deploymentAdvanced = advancedFrom(legacy);
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
    ...advancedFrom(personalAdvanced, deploymentAdvanced),
  };
}

export function resolveLlmSource(input: ResolveLlmInput): LlmSettingSource {
  if (input.username) {
    if (hasLlmProviderOverride(input.personal)) return 'user';
    if (input.platformDefaults?.main) return 'platform_default';
    return input.assignedDefaults?.main ? 'default_snapshot' : 'env';
  }
  return input.legacy?.baseUrl !== null && input.legacy?.baseUrl !== undefined ? 'settings' : 'env';
}

export function resolveTaskLlmSource(input: ResolveLlmInput): LlmSettingSource {
  if (input.username) {
    if (input.personalTask && Object.values(input.personalTask).some((value) => value !== null)) return 'user';
    if (input.platformDefaults?.task ?? input.platformDefaults?.main) return 'platform_default';
    return input.assignedDefaults?.task ? 'default_snapshot' : 'main';
  }
  const deploymentTaskConfigured = Boolean(
    config.taskLlm.baseUrl || config.taskLlm.model || config.taskLlm.apiKey,
  );
  return deploymentTaskConfigured ? 'env' : resolveLlmSource(input);
}

// admin 平台默认是统一运行时兜底；个人端点覆盖仍保持最高优先级。
export function resolveLlmSettings(input: ResolveLlmInput): LlmSettings {
  const { username, personal, assignedDefaults, platformDefaults } = input;
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
      ...advancedFrom(personal),
    };
  }
  if (username && platformDefaults?.main) {
    return fromAssignedDefault(platformDefaults.main, personal?.maxTokens, personal?.contextWindow, personal);
  }
  if (username && assignedDefaults?.main) {
    return fromAssignedDefault(assignedDefaults.main, personal?.maxTokens, personal?.contextWindow, personal);
  }
  return deploymentMainSettings(
    personal?.maxTokens,
    personal?.contextWindow,
    username ? undefined : input.legacy,
    personal,
  );
}

/**
 * 认证用户优先读取个人任务模型，其次读取 admin 平台 task/main 默认；均为空时
 * 使用固化快照或主模型。未绑定用户的后台流程保留部署 task/main 回退。
 */
export function resolveTaskLlmSettings(input: ResolveLlmInput): LlmSettings {
  const { username, personalTask, assignedDefaults, platformDefaults } = input;
  const main = resolveLlmSettings(input);

  if (username && hasLlmProviderOverride(personalTask)) {
    const fallback = platformDefaults?.task ?? assignedDefaults?.task ?? main;
    return {
      baseUrl: (personalTask?.baseUrl ?? '').trim(),
      model: (personalTask?.model ?? '').trim(),
      apiKey: personalTask?.apiKey ?? '',
      maxTokens: normalizedMaxTokens(personalTask?.maxTokens, fallback.maxTokens),
      contextWindow: normalizedContextWindow(personalTask?.contextWindow, fallback.contextWindow),
      ...advancedFrom(personalTask),
    };
  }

  if (username && (platformDefaults?.task ?? platformDefaults?.main)) {
    return fromAssignedDefault(
      platformDefaults.task ?? platformDefaults.main!,
      personalTask?.maxTokens,
      personalTask?.contextWindow,
      personalTask,
    );
  }

  if (username && assignedDefaults?.task) {
    return fromAssignedDefault(
      assignedDefaults.task,
      personalTask?.maxTokens,
      personalTask?.contextWindow,
      personalTask,
    );
  }

  if (username) return fromAssignedDefault(main, personalTask?.maxTokens, personalTask?.contextWindow, personalTask);

  const deploymentTaskConfigured = Boolean(
    config.taskLlm.baseUrl || config.taskLlm.model || config.taskLlm.apiKey,
  );
  if (deploymentTaskConfigured) {
    return {
      baseUrl: config.taskLlm.baseUrl.trim(),
      model: config.taskLlm.model.trim(),
      apiKey: config.taskLlm.apiKey,
      maxTokens: config.llm.maxTokens,
      contextWindow: config.llm.contextWindow,
      ...emptyLlmAdvancedSettings(),
    };
  }
  return deploymentMainSettings(
    undefined,
    undefined,
    username ? undefined : input.legacy,
    undefined,
  );
}
