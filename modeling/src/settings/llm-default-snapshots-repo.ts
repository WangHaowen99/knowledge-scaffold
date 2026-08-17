import { getDb, nowIso } from '../db/index.js';
import {
  type LlmDefaultSettings,
  type LlmPurpose,
} from './llm-defaults-repo.js';
import {
  decryptLlmDefaultApiKey,
  encryptLlmDefaultApiKey,
} from './llm-secret-codec.js';

export type LlmDefaultAssignmentReason = 'registration' | 'sync';

interface UserLlmDefaultSnapshotRow {
  username: string;
  purpose: LlmPurpose;
  base_url: string;
  model: string;
  api_key_ciphertext: string | null;
  max_tokens: number;
  default_revision: number;
  assigned_via: LlmDefaultAssignmentReason;
  assigned_at: string;
}

export interface UserLlmDefaultSnapshot {
  purpose: LlmPurpose;
  baseUrl: string;
  model: string;
  apiKey: string;
  maxTokens: number;
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
      max_tokens, default_revision, assigned_via, assigned_at
    FROM user_llm_default_snapshots WHERE username = ? AND purpose = ?`)
    .get(normalizedUsername(username), purpose) as UserLlmDefaultSnapshotRow | undefined;
}

export function getUserLlmDefaultSnapshot(
  username: string,
  purpose: LlmPurpose,
): UserLlmDefaultSnapshot | undefined {
  const row = snapshotRow(username, purpose);
  if (!row) return undefined;
  return {
    purpose: row.purpose,
    baseUrl: row.base_url,
    model: row.model,
    apiKey: decryptLlmDefaultApiKey(row.api_key_ciphertext, cipherContext(row.username, row.purpose)),
    maxTokens: row.max_tokens,
    defaultRevision: row.default_revision,
    assignedVia: row.assigned_via,
    assignedAt: row.assigned_at,
  };
}

export function getUserLlmDefaultSnapshotMaxTokens(username: string): number | undefined {
  return snapshotRow(username, 'main')?.max_tokens;
}

export function deleteUserLlmDefaultSnapshots(username: string): void {
  getDb().prepare('DELETE FROM user_llm_default_snapshots WHERE username = ?')
    .run(normalizedUsername(username));
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
  defaultRevision: number;
}

function prepareSnapshots(username: string, defaults: UserLlmDefaultSnapshotSet): PreparedSnapshot[] {
  return [defaults.main, defaults.task].filter((value): value is LlmDefaultSettings => Boolean(value))
    .map((settings) => ({
      purpose: settings.purpose,
      baseUrl: settings.baseUrl,
      model: settings.model,
      apiKeyCiphertext: encryptLlmDefaultApiKey(
        settings.apiKey,
        cipherContext(username, settings.purpose),
      ),
      maxTokens: settings.maxTokens,
      defaultRevision: settings.revision,
    }));
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
    db.prepare('DELETE FROM user_llm_default_snapshots WHERE username = ?').run(normalized);
    const insert = db.prepare(`INSERT INTO user_llm_default_snapshots (
        username, purpose, base_url, model, api_key_ciphertext, max_tokens,
        default_revision, assigned_via, assigned_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`);
    for (const snapshot of prepared) {
      insert.run(
        normalized,
        snapshot.purpose,
        snapshot.baseUrl,
        snapshot.model,
        snapshot.apiKeyCiphertext,
        snapshot.maxTokens,
        snapshot.defaultRevision,
        assignedVia,
        assignedAt,
      );
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
