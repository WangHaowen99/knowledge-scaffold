import { getDb, nowIso } from '../db/index.js';
import { config } from '../config.js';
import { currentLlmUser } from '../llm/user-context.js';

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

type UserLlmSettingKey = 'base_url' | 'model' | 'api_key' | 'max_tokens';
const USER_LLM_COLUMNS: Record<UserLlmSettingKey, UserLlmSettingKey> = {
  base_url: 'base_url',
  model: 'model',
  api_key: 'api_key',
  max_tokens: 'max_tokens',
};

interface UserLlmSettingsRow {
  base_url: string | null;
  model: string | null;
  api_key: string | null;
  max_tokens: number | null;
}

function normalizedUsername(username: string): string {
  const normalized = username.trim();
  if (!normalized) throw new Error('LLM 配置用户不能为空');
  return normalized;
}

function userLlmRow(username: string): UserLlmSettingsRow | undefined {
  return getDb().prepare(`SELECT base_url, model, api_key, max_tokens
    FROM user_llm_settings WHERE username = ?`)
    .get(normalizedUsername(username)) as UserLlmSettingsRow | undefined;
}

function userLlmColumn(key: UserLlmSettingKey): UserLlmSettingKey {
  const column = USER_LLM_COLUMNS[key];
  if (!column) throw new Error('不支持的 LLM 配置项');
  return column;
}

export function getUserLlmSetting(username: string, key: UserLlmSettingKey): string | null {
  const value = userLlmRow(username)?.[userLlmColumn(key)];
  return value === null || value === undefined ? null : String(value);
}

export function setUserLlmSetting(username: string, key: UserLlmSettingKey, value: string): void {
  const normalized = normalizedUsername(username);
  const setting = value.trim();
  if (!setting) {
    deleteUserLlmSetting(normalized, key);
    return;
  }
  const column = userLlmColumn(key);
  if (column === 'max_tokens') {
    const maxTokens = Number(setting);
    if (!Number.isSafeInteger(maxTokens) || maxTokens < 1 || maxTokens > 1_000_000) {
      throw new Error('LLM 单次输出上限必须是 1 到 1000000 的整数');
    }
  }
  getDb().prepare(`INSERT INTO user_llm_settings (username, ${column}, updated_at)
    VALUES (?,?,?)
    ON CONFLICT(username) DO UPDATE SET
      ${column} = excluded.${column}, updated_at = excluded.updated_at`)
    .run(normalized, setting, nowIso());
}

export function deleteUserLlmSetting(username: string, key: UserLlmSettingKey): void {
  const normalized = normalizedUsername(username);
  const column = userLlmColumn(key);
  const db = getDb();
  db.prepare(`UPDATE user_llm_settings SET ${column} = NULL, updated_at = ? WHERE username = ?`)
    .run(nowIso(), normalized);
  db.prepare(`DELETE FROM user_llm_settings
    WHERE username = ? AND base_url IS NULL AND model IS NULL
      AND api_key IS NULL AND max_tokens IS NULL`).run(normalized);
}

export type LlmSettingSource = 'user' | 'settings' | 'env';

export function getLlmSource(username: string | undefined = currentLlmUser()): LlmSettingSource {
  if (username) return getUserLlmSetting(username, 'base_url') !== null ? 'user' : 'env';
  return getSetting('llm_base_url') !== null ? 'settings' : 'env';
}

export function hasUserLlmProviderOverride(username: string): boolean {
  const personal = userLlmRow(username);
  return Boolean(personal && [personal.base_url, personal.model, personal.api_key]
    .some((value) => value !== null));
}

// LLM 配置：当前用户页面配置优先，认证请求回退 env；旧版全局设置只供无认证脚本兼容。
// 无认证上下文的 CLI/测试沿用旧版全局设置与 env，不会读取任一用户的配置。
export interface LlmSettings { baseUrl: string; model: string; apiKey: string; maxTokens: number; }
export function getLlmSettings(username: string | undefined = currentLlmUser()): LlmSettings {
  const personal = username ? userLlmRow(username) : undefined;
  // 旧版 settings.llm_* 可能由任意历史用户写入，无法确认归属。认证请求不能
  // 将其作为默认值，否则新用户会继承他人的端点或密钥；仅无用户 CLI 保留兼容读取。
  const legacy = (key: string): string | null => (username ? null : getSetting(key));
  return {
    baseUrl: (personal?.base_url || legacy('llm_base_url') || config.llm.baseUrl || '').trim(),
    model: (personal?.model || legacy('llm_model') || config.llm.model || '').trim(),
    apiKey: (personal?.api_key || legacy('llm_api_key') || config.llm.apiKey || '').trim(),
    maxTokens: Number(personal?.max_tokens
      || legacy('llm_max_tokens') || process.env.MODELING_LLM_MAX_TOKENS || 8192),
  };
}

// 评测数据源默认值（评测运行面板预填，减少每轮手输）。
export interface EvalBindingDefaults { provider: string; url: string; token: string; scene: string; }
export function getEvalBindingDefaults(): EvalBindingDefaults {
  return {
    provider: getSetting('eval_binding_provider') || 'nl2sql_template',
    url: getSetting('eval_binding_url') || '',
    token: getSetting('eval_binding_token') || '',
    scene: getSetting('eval_binding_scene') || '',
  };
}
