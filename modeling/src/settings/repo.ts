import { getDb, nowIso } from '../db/index.js';
import { config } from '../config.js';
import { currentLlmUser } from '../llm/user-context.js';
import {
  hasLlmProviderOverride,
  resolveLlmSettings,
  resolveLlmSource,
  resolveTaskLlmSettings,
  resolveTaskLlmSource,
  type LegacyLlmSettingsSnapshot,
  type LlmSettingSource,
  type LlmSettings,
  type UserLlmSettingsSnapshot,
} from './llm-resolver.js';
import {
  deleteUserLlmDefaultSnapshot,
  getUserLlmDefaultSnapshot,
  getUserLlmDefaultSnapshotContextWindow,
  getUserLlmDefaultSnapshotMaxTokens,
} from './llm-default-snapshots-repo.js';
import type { LlmPurpose } from './llm-defaults-repo.js';
import { getLlmDefaultSettings } from './llm-defaults-repo.js';

export type { LlmSettingSource, LlmSettings } from './llm-resolver.js';

export const LLM_MAX_TOKENS_MIN = 1;
export const LLM_MAX_TOKENS_MAX = 1_000_000;
export const LLM_CONTEXT_WINDOW_MIN = 1;

// 服务设置：页面可配置的键值（存 SQLite），env 只作默认值/兜底。
export function getSetting(key: string): string | null {
  const r = getDb().prepare('SELECT value FROM settings WHERE key = ?').get(key) as { value: string } | undefined;
  return r?.value ?? null;
}

export function setSetting(key: string, value: string): void {
  getDb().prepare(`INSERT INTO settings (key, value, updated_at) VALUES (?,?,?)
    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at`)
    .run(key, value, nowIso());
}

export function deleteSetting(key: string): void {
  getDb().prepare('DELETE FROM settings WHERE key = ?').run(key);
}

// 评测用 consumer 业务 API 地址：页面设置优先，其次 env CONSUMER_API_URL（默认 consumer 本机地址）。
// 注意：这是 consumer 的业务 API，与登录 issuer（auth-server）是两个不同服务，不再互为回退。
export function getConsumerApiUrl(): string {
  return (getSetting('consumer_api_url') || config.consumerApiUrl).replace(/\/+$/, '');
}

export type UserLlmSettingKey = 'base_url' | 'model' | 'api_key' | 'max_tokens' | 'context_window';
const USER_LLM_COLUMNS: Record<UserLlmSettingKey, UserLlmSettingKey> = {
  base_url: 'base_url',
  model: 'model',
  api_key: 'api_key',
  max_tokens: 'max_tokens',
  context_window: 'context_window',
};

interface UserLlmSettingsRow {
  base_url: string | null;
  model: string | null;
  api_key: string | null;
  max_tokens: number | null;
  context_window: number | null;
}

function normalizedUsername(username: string): string {
  const normalized = username.trim();
  if (!normalized) throw new Error('LLM 配置用户不能为空');
  return normalized;
}

function userLlmTable(purpose: LlmPurpose): 'user_llm_settings' | 'user_task_llm_settings' {
  return purpose === 'main' ? 'user_llm_settings' : 'user_task_llm_settings';
}

function userLlmRow(username: string, purpose: LlmPurpose = 'main'): UserLlmSettingsRow | undefined {
  return getDb().prepare(`SELECT base_url, model, api_key, max_tokens, context_window
    FROM ${userLlmTable(purpose)} WHERE username = ?`)
    .get(normalizedUsername(username)) as UserLlmSettingsRow | undefined;
}

function userLlmColumn(key: UserLlmSettingKey): UserLlmSettingKey {
  const column = USER_LLM_COLUMNS[key];
  if (!column) throw new Error('不支持的 LLM 配置项');
  return column;
}

export function getUserLlmSetting(
  username: string,
  key: UserLlmSettingKey,
  purpose: LlmPurpose = 'main',
): string | null {
  const value = userLlmRow(username, purpose)?.[userLlmColumn(key)];
  return value === null || value === undefined ? null : String(value);
}

export function setUserLlmSetting(
  username: string,
  key: UserLlmSettingKey,
  value: string,
  purpose: LlmPurpose = 'main',
): void {
  const normalized = normalizedUsername(username);
  const setting = value.trim();
  if (!setting) {
    deleteUserLlmSetting(normalized, key, purpose);
    return;
  }
  const column = userLlmColumn(key);
  if (column === 'max_tokens') {
    const maxTokens = Number(setting);
    if (!Number.isSafeInteger(maxTokens) || maxTokens < LLM_MAX_TOKENS_MIN || maxTokens > LLM_MAX_TOKENS_MAX) {
      throw new Error('LLM 单次输出上限必须是 1 到 1000000 的整数');
    }
  }
  if (column === 'context_window') {
    const contextWindow = Number(setting);
    if (!Number.isSafeInteger(contextWindow) || contextWindow < LLM_CONTEXT_WINDOW_MIN) {
      throw new Error('LLM 上下文窗口必须是大于等于 1 的整数');
    }
  }
  const db = getDb();
  db.exec('BEGIN IMMEDIATE');
  try {
    if (column !== 'max_tokens' && column !== 'context_window') {
      deleteUserLlmDefaultSnapshot(normalized, purpose);
    }
    const table = userLlmTable(purpose);
    db.prepare(`INSERT INTO ${table} (username, ${column}, updated_at)
      VALUES (?,?,?)
      ON CONFLICT(username) DO UPDATE SET
        ${column} = excluded.${column}, updated_at = excluded.updated_at`)
      .run(normalized, setting, nowIso());
    db.exec('COMMIT');
  } catch (error) {
    try { db.exec('ROLLBACK'); } catch { /* transaction 已结束时忽略 */ }
    throw error;
  }
}

export function deleteUserLlmSetting(
  username: string,
  key: UserLlmSettingKey,
  purpose: LlmPurpose = 'main',
): void {
  const normalized = normalizedUsername(username);
  const column = userLlmColumn(key);
  const table = userLlmTable(purpose);
  const db = getDb();
  db.prepare(`UPDATE ${table} SET ${column} = NULL, updated_at = ? WHERE username = ?`)
    .run(nowIso(), normalized);
  db.prepare(`DELETE FROM ${table}
    WHERE username = ? AND base_url IS NULL AND model IS NULL
      AND api_key IS NULL AND max_tokens IS NULL AND context_window IS NULL`).run(normalized);
}

export interface UpdateUserLlmSettingsInput {
  baseUrl?: string;
  model?: string;
  apiKey?: string;
  apiKeyClear?: boolean;
  providerClear?: boolean;
  maxTokens?: string;
  contextWindow?: string;
}

export class UserLlmSettingsValidationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'UserLlmSettingsValidationError';
  }
}

function validateUserLlmBaseUrl(baseUrl: string): void {
  if (baseUrl === 'faux') return;
  try {
    const parsed = new URL(baseUrl);
    if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password) throw new Error('invalid');
  } catch {
    throw new UserLlmSettingsValidationError('个人 LLM Base URL 必须是无内嵌凭据的 http(s) 地址，或填写 faux');
  }
}

/**
 * 页面/API 的个人 LLM patch 先合并成完整草稿，再在单个事务中提交。
 * Key 留空表示保留；修改 provider 会脱离同用途快照，providerClear 回退到该用途的下一解析层。
 */
export function updateUserLlmSettings(
  username: string,
  input: UpdateUserLlmSettingsInput,
  purpose: LlmPurpose = 'main',
): void {
  const normalized = normalizedUsername(username);
  const current = userLlmRow(normalized, purpose);
  const providerChanged = input.providerClear === true
    || input.baseUrl !== undefined
    || input.model !== undefined
    || input.apiKey !== undefined
    || input.apiKeyClear === true;
  let baseUrl = current?.base_url ?? null;
  let model = current?.model ?? null;
  let apiKey = current?.api_key ?? null;
  let maxTokens = current?.max_tokens
    ?? (providerChanged ? (getUserLlmDefaultSnapshotMaxTokens(normalized, purpose) ?? null) : null);
  let contextWindow = current?.context_window
    ?? (providerChanged ? (getUserLlmDefaultSnapshotContextWindow(normalized, purpose) ?? null) : null);

  if (input.providerClear === true) {
    if (input.baseUrl || input.model || input.apiKey) {
      throw new UserLlmSettingsValidationError('llmProviderClear 不能与个人 provider 字段同时提交');
    }
    baseUrl = null;
    model = null;
    apiKey = null;
  } else {
    if (input.baseUrl !== undefined) baseUrl = input.baseUrl.trim() || null;
    if (input.model !== undefined) model = input.model.trim() || null;
    if (input.apiKeyClear === true) apiKey = null;
    else if (input.apiKey !== undefined && input.apiKey !== '') apiKey = input.apiKey;
  }
  if (input.maxTokens !== undefined) {
    const value = input.maxTokens.trim();
    if (!value) maxTokens = null;
    else {
      const parsed = Number(value);
      if (!Number.isSafeInteger(parsed) || parsed < 1 || parsed > 1_000_000) {
        throw new UserLlmSettingsValidationError('llmMaxTokens 必须是 1 到 1000000 的整数');
      }
      maxTokens = parsed;
    }
  }
  if (input.contextWindow !== undefined) {
    const value = input.contextWindow.trim();
    if (!value) contextWindow = null;
    else {
      const parsed = Number(value);
      if (!Number.isSafeInteger(parsed) || parsed < LLM_CONTEXT_WINDOW_MIN) {
        throw new UserLlmSettingsValidationError('llmContextWindow 必须是大于等于 1 的整数');
      }
      contextWindow = parsed;
    }
  }

  const providerConfigured = [baseUrl, model, apiKey].some((value) => value !== null);
  if (providerConfigured && (!baseUrl || !model)) {
    throw new UserLlmSettingsValidationError('个人 LLM 覆盖的 Base URL 与模型名必须同时设置');
  }
  if (baseUrl) validateUserLlmBaseUrl(baseUrl);

  const db = getDb();
  db.exec('BEGIN IMMEDIATE');
  try {
    if (providerChanged) deleteUserLlmDefaultSnapshot(normalized, purpose);
    const table = userLlmTable(purpose);
    if ([baseUrl, model, apiKey, maxTokens, contextWindow].every((value) => value === null)) {
      db.prepare(`DELETE FROM ${table} WHERE username = ?`).run(normalized);
    } else {
      db.prepare(`INSERT INTO ${table} (
          username, base_url, model, api_key, max_tokens, context_window, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(username) DO UPDATE SET
          base_url = excluded.base_url,
          model = excluded.model,
          api_key = excluded.api_key,
          max_tokens = excluded.max_tokens,
          context_window = excluded.context_window,
          updated_at = excluded.updated_at`)
        .run(normalized, baseUrl, model, apiKey, maxTokens, contextWindow, nowIso());
    }
    db.exec('COMMIT');
  } catch (error) {
    try { db.exec('ROLLBACK'); } catch { /* transaction 已结束时忽略 */ }
    throw error;
  }
}

function userLlmSnapshot(
  username: string | undefined,
  purpose: LlmPurpose = 'main',
): UserLlmSettingsSnapshot | undefined {
  const row = username ? userLlmRow(username, purpose) : undefined;
  return row ? {
    baseUrl: row.base_url,
    model: row.model,
    apiKey: row.api_key,
    maxTokens: row.max_tokens,
    contextWindow: row.context_window,
  } : undefined;
}

function legacyLlmSnapshot(): LegacyLlmSettingsSnapshot {
  return {
    baseUrl: getSetting('llm_base_url'),
    model: getSetting('llm_model'),
    apiKey: getSetting('llm_api_key'),
    maxTokens: getSetting('llm_max_tokens'),
    contextWindow: getSetting('llm_context_window'),
  };
}

function resolverInput(username: string | undefined) {
  return {
    username,
    personal: userLlmSnapshot(username),
    personalTask: userLlmSnapshot(username, 'task'),
    assignedDefaults: username ? {
      main: getUserLlmDefaultSnapshot(username, 'main'),
      task: getUserLlmDefaultSnapshot(username, 'task'),
    } : undefined,
    platformDefaults: username ? {
      main: getLlmDefaultSettings('main'),
      task: getLlmDefaultSettings('task'),
    } : undefined,
    legacy: username ? undefined : legacyLlmSnapshot(),
  };
}

export function getLlmSource(username: string | undefined = currentLlmUser()): LlmSettingSource {
  return resolveLlmSource(resolverInput(username));
}

export function getTaskLlmSource(username: string | undefined = currentLlmUser()): LlmSettingSource {
  return resolveTaskLlmSource(resolverInput(username));
}

export function hasUserLlmProviderOverride(username: string, purpose: LlmPurpose = 'main'): boolean {
  return hasLlmProviderOverride(userLlmSnapshot(username, purpose));
}

export function getLlmSettings(username: string | undefined = currentLlmUser()): LlmSettings {
  return resolveLlmSettings(resolverInput(username));
}

export function getTaskLlmSettings(username: string | undefined = currentLlmUser()): LlmSettings {
  return resolveTaskLlmSettings(resolverInput(username));
}

// 评测数据源默认值（评测运行面板预填，减少每轮手输）。
export interface EvalBindingDefaults { provider: string; url: string; token: string; scene: string; }
export function getEvalBindingDefaults(): EvalBindingDefaults {
  return {
    provider: getSetting('eval_binding_provider') || config.evalBinding.provider,
    url: getSetting('eval_binding_url') || config.evalBinding.url,
    token: getSetting('eval_binding_token') || config.evalBinding.token,
    scene: getSetting('eval_binding_scene') || config.evalBinding.scene,
  };
}
