import { getDb, nowIso } from '../db/index.js';
import {
  type LlmDefaultSettings,
  type LlmPurpose,
} from './llm-defaults-repo.js';
import {
  decryptLlmDefaultApiKey,
  encryptLlmDefaultApiKey,
} from './llm-secret-codec.js';
import {
  normalizeLlmAdvancedSettings,
  type LlmAdvancedSettings,
} from './llm-extra-params.js';

export type LlmDefaultAssignmentReason = 'registration' | 'sync';

interface UserLlmDefaultSnapshotRow {
  username: string;
  purpose: LlmPurpose;
  base_url: string;
  model: string;
  api_key_ciphertext: string | null;
  max_tokens: number;
  context_window: number;
  thinking_mode: LlmAdvancedSettings['thinkingMode'];
  reasoning_effort: LlmAdvancedSettings['reasoningEffort'];
  extra_body_json: string;
  default_revision: number;
  assigned_via: LlmDefaultAssignmentReason;
  assigned_at: string;
}

export interface UserLlmDefaultSnapshot extends LlmAdvancedSettings {
  purpose: LlmPurpose;
  baseUrl: string;
  model: string;
  apiKey: string;
  maxTokens: number;
  contextWindow: number;
  defaultRevision: number;
  assignedVia: LlmDefaultAssignmentReason;
  assignedAt: string;
}

function normalizedUsername(username: string): string {
  const normalized = username.trim();
  if (!normalized) throw new Error('LLM 默认快照用户不能为空');
  return normalized;
}

function cipherContext(username: string, purpose: LlmPurpose): string {
  return `user_default_snapshot.${username}.${purpose}`;
}

function snapshotRow(username: string, purpose: LlmPurpose): UserLlmDefaultSnapshotRow | undefined {
  return getDb().prepare(`SELECT username, purpose, base_url, model, api_key_ciphertext,
      max_tokens, context_window, thinking_mode, reasoning_effort, extra_body_json,
      default_revision, assigned_via, assigned_at
    FROM user_llm_default_snapshots WHERE username = ? AND purpose = ?`)
    .get(normalizedUsername(username), purpose) as UserLlmDefaultSnapshotRow | undefined;
}

export function getUserLlmDefaultSnapshot(
  username: string,
  purpose: LlmPurpose,
): UserLlmDefaultSnapshot | undefined {
  const row = snapshotRow(username, purpose);
  if (!row) return undefined;
  const advanced = normalizeLlmAdvancedSettings({
    thinkingMode: row.thinking_mode,
    reasoningEffort: row.reasoning_effort,
    extraBodyJson: row.extra_body_json,
  });
  return {
    purpose: row.purpose,
    baseUrl: row.base_url,
    model: row.model,
    apiKey: decryptLlmDefaultApiKey(row.api_key_ciphertext, cipherContext(row.username, row.purpose)),
    maxTokens: row.max_tokens,
    contextWindow: row.context_window,
    ...advanced,
    defaultRevision: row.default_revision,
    assignedVia: row.assigned_via,
    assignedAt: row.assigned_at,
  };
}

export function getUserLlmDefaultSnapshotMaxTokens(
  username: string,
  purpose: LlmPurpose = 'main',
): number | undefined {
  return snapshotRow(username, purpose)?.max_tokens;
}

export function getUserLlmDefaultSnapshotContextWindow(
  username: string,
  purpose: LlmPurpose = 'main',
): number | undefined {
  return snapshotRow(username, purpose)?.context_window;
}

export function deleteUserLlmDefaultSnapshots(username: string): void {
  getDb().prepare('DELETE FROM user_llm_default_snapshots WHERE username = ?')
    .run(normalizedUsername(username));
}

export function deleteUserLlmDefaultSnapshot(username: string, purpose: LlmPurpose): void {
  getDb().prepare('DELETE FROM user_llm_default_snapshots WHERE username = ? AND purpose = ?')
    .run(normalizedUsername(username), purpose);
}

interface UserLlmDefaultSnapshotSet {
  main: LlmDefaultSettings;
  task?: LlmDefaultSettings;
}

interface PreparedSnapshot {
  purpose: LlmPurpose;
  baseUrl: string;
  model: string;
  apiKeyCiphertext: string | null;
  maxTokens: number;
  contextWindow: number;
  thinkingMode: LlmAdvancedSettings['thinkingMode'];
  reasoningEffort: LlmAdvancedSettings['reasoningEffort'];
  extraBodyJson: string;
  defaultRevision: number;
}

function prepareSnapshot(
  username: string,
  purpose: LlmPurpose,
  settings: LlmDefaultSettings,
): PreparedSnapshot {
  return {
    purpose,
    baseUrl: settings.baseUrl,
    model: settings.model,
    apiKeyCiphertext: encryptLlmDefaultApiKey(settings.apiKey, cipherContext(username, purpose)),
    maxTokens: settings.maxTokens,
    contextWindow: settings.contextWindow,
    thinkingMode: settings.thinkingMode,
    reasoningEffort: settings.reasoningEffort,
    extraBodyJson: settings.extraBodyJson,
    defaultRevision: settings.revision,
  };
}

function prepareSnapshots(username: string, defaults: UserLlmDefaultSnapshotSet): PreparedSnapshot[] {
  return [defaults.main, defaults.task]
    .filter((value): value is LlmDefaultSettings => Boolean(value))
    .map((settings) => prepareSnapshot(username, settings.purpose, settings));
}

function insertPreparedSnapshot(
  insert: ReturnType<ReturnType<typeof getDb>['prepare']>,
  username: string,
  snapshot: PreparedSnapshot,
  assignedVia: LlmDefaultAssignmentReason,
  assignedAt: string,
): void {
  insert.run(
    username,
    snapshot.purpose,
    snapshot.baseUrl,
    snapshot.model,
    snapshot.apiKeyCiphertext,
    snapshot.maxTokens,
    snapshot.contextWindow,
    snapshot.thinkingMode,
    snapshot.reasoningEffort,
    snapshot.extraBodyJson,
    snapshot.defaultRevision,
    assignedVia,
    assignedAt,
  );
}

function saveUserLlmDefaultSnapshots(
  username: string,
  defaults: UserLlmDefaultSnapshotSet,
  assignedVia: LlmDefaultAssignmentReason,
): boolean {
  const normalized = normalizedUsername(username);
  const prepared = prepareSnapshots(normalized, defaults);
  const assignedAt = nowIso();
  const db = getDb();
  db.exec('BEGIN IMMEDIATE');
  try {
    db.prepare('DELETE FROM user_llm_settings WHERE username = ?').run(normalized);
    db.prepare('DELETE FROM user_task_llm_settings WHERE username = ?').run(normalized);
    db.prepare('DELETE FROM user_llm_default_snapshots WHERE username = ?').run(normalized);
    const insert = db.prepare(`INSERT INTO user_llm_default_snapshots (
      username, purpose, base_url, model, api_key_ciphertext, max_tokens,
        context_window, thinking_mode, reasoning_effort, extra_body_json,
        default_revision, assigned_via, assigned_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`);
    for (const snapshot of prepared) {
      insertPreparedSnapshot(insert, normalized, snapshot, assignedVia, assignedAt);
    }
    db.exec('COMMIT');
    return true;
  } catch (error) {
    try { db.exec('ROLLBACK'); } catch { /* transaction 已结束时忽略 */ }
    throw error;
  }
}

/** 成功注册代表新的账号身份；清除同名旧账号遗留，避免用户名复用泄露配置。 */
export function clearNewUserLlmConfiguration(username: string): void {
  const normalized = normalizedUsername(username);
  const db = getDb();
  db.exec('BEGIN IMMEDIATE');
  try {
    db.prepare('DELETE FROM user_llm_settings WHERE username = ?').run(normalized);
    db.prepare('DELETE FROM user_task_llm_settings WHERE username = ?').run(normalized);
    db.prepare('DELETE FROM user_llm_default_snapshots WHERE username = ?').run(normalized);
    db.exec('COMMIT');
  } catch (error) {
    try { db.exec('ROLLBACK'); } catch { /* transaction 已结束时忽略 */ }
    throw error;
  }
}

export function initializeUserLlmDefaultSnapshots(
  username: string,
  defaults: UserLlmDefaultSnapshotSet,
): boolean {
  return saveUserLlmDefaultSnapshots(username, defaults, 'registration');
}

export function replaceUserLlmWithDefaultSnapshots(
  username: string,
  defaults: UserLlmDefaultSnapshotSet,
): void {
  saveUserLlmDefaultSnapshots(username, defaults, 'sync');
}

export function replaceUserLlmPurposeWithDefaultSnapshot(
  username: string,
  purpose: LlmPurpose,
  settings: LlmDefaultSettings,
): void {
  const normalized = normalizedUsername(username);
  const snapshot = prepareSnapshot(normalized, purpose, settings);
  const assignedAt = nowIso();
  const table = purpose === 'main' ? 'user_llm_settings' : 'user_task_llm_settings';
  const db = getDb();
  db.exec('BEGIN IMMEDIATE');
  try {
    db.prepare(`DELETE FROM ${table} WHERE username = ?`).run(normalized);
    db.prepare('DELETE FROM user_llm_default_snapshots WHERE username = ? AND purpose = ?')
      .run(normalized, purpose);
    const insert = db.prepare(`INSERT INTO user_llm_default_snapshots (
      username, purpose, base_url, model, api_key_ciphertext, max_tokens,
        context_window, thinking_mode, reasoning_effort, extra_body_json,
        default_revision, assigned_via, assigned_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`);
    insertPreparedSnapshot(insert, normalized, snapshot, 'sync', assignedAt);
    db.exec('COMMIT');
  } catch (error) {
    try { db.exec('ROLLBACK'); } catch { /* transaction 已结束时忽略 */ }
    throw error;
  }
}
