import { getDb, nowIso } from '../db/index.js';
import { decryptLlmDefaultApiKey, encryptLlmDefaultApiKey } from './llm-secret-codec.js';

export const LLM_PURPOSES = ['main', 'task'] as const;
export type LlmPurpose = typeof LLM_PURPOSES[number];

interface LlmDefaultRow {
  purpose: LlmPurpose;
  base_url: string;
  model: string;
  api_key_ciphertext: string | null;
  max_tokens: number;
  revision: number;
  updated_by: string;
  updated_at: string;
}

export interface LlmDefaultSettings {
  purpose: LlmPurpose;
  baseUrl: string;
  model: string;
  apiKey: string;
  maxTokens: number;
  revision: number;
  updatedBy: string;
  updatedAt: string;
}

export interface PublicLlmDefaultSettings extends Omit<LlmDefaultSettings, 'apiKey'> {
  apiKeySet: boolean;
}

export interface SaveLlmDefaultInput {
  purpose: LlmPurpose;
  baseUrl: string;
  model: string;
  maxTokens: number;
  apiKey?: string;
  apiKeyClear?: boolean;
  expectedRevision?: number;
}

export class LlmDefaultValidationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'LlmDefaultValidationError';
  }
}

export class LlmDefaultRevisionConflictError extends Error {
  constructor() {
    super('平台 LLM 默认配置已被其他管理员修改，请刷新后重试');
    this.name = 'LlmDefaultRevisionConflictError';
  }
}

function rowFor(purpose: LlmPurpose): LlmDefaultRow | undefined {
  return getDb().prepare(`SELECT purpose, base_url, model, api_key_ciphertext,
      max_tokens, revision, updated_by, updated_at
    FROM llm_default_settings WHERE purpose = ?`).get(purpose) as LlmDefaultRow | undefined;
}

function publicRow(row: LlmDefaultRow): PublicLlmDefaultSettings {
  return {
    purpose: row.purpose,
    baseUrl: row.base_url,
    model: row.model,
    maxTokens: row.max_tokens,
    apiKeySet: Boolean(row.api_key_ciphertext),
    revision: row.revision,
    updatedBy: row.updated_by,
    updatedAt: row.updated_at,
  };
}

export function isLlmPurpose(value: unknown): value is LlmPurpose {
  return value === 'main' || value === 'task';
}

export function validateLlmDefaultDraft(input: Pick<SaveLlmDefaultInput, 'baseUrl' | 'model' | 'maxTokens'>): void {
  const baseUrl = input.baseUrl.trim();
  const model = input.model.trim();
  if (!baseUrl || !model) throw new LlmDefaultValidationError('Base URL 与模型名必填');
  if (baseUrl !== 'faux') {
    try {
      const parsed = new URL(baseUrl);
      if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password) throw new Error('invalid');
    } catch {
      throw new LlmDefaultValidationError('Base URL 必须是无内嵌凭据的 http(s) 地址，或填写 faux');
    }
  }
  if (!Number.isSafeInteger(input.maxTokens) || input.maxTokens < 1 || input.maxTokens > 1_000_000) {
    throw new LlmDefaultValidationError('maxTokens 必须是 1 到 1000000 的整数');
  }
}

export function getLlmDefaultSettings(purpose: LlmPurpose): LlmDefaultSettings | undefined {
  const row = rowFor(purpose);
  if (!row) return undefined;
  return {
    purpose: row.purpose,
    baseUrl: row.base_url,
    model: row.model,
    apiKey: decryptLlmDefaultApiKey(row.api_key_ciphertext, purpose),
    maxTokens: row.max_tokens,
    revision: row.revision,
    updatedBy: row.updated_by,
    updatedAt: row.updated_at,
  };
}

export function getPublicLlmDefaultSettings(purpose: LlmPurpose): PublicLlmDefaultSettings | undefined {
  const row = rowFor(purpose);
  return row ? publicRow(row) : undefined;
}

export function getPublicLlmDefaults(): Record<LlmPurpose, PublicLlmDefaultSettings | null> {
  return {
    main: getPublicLlmDefaultSettings('main') ?? null,
    task: getPublicLlmDefaultSettings('task') ?? null,
  };
}

export function saveLlmDefaultSettings(input: SaveLlmDefaultInput, updatedBy: string): PublicLlmDefaultSettings {
  validateLlmDefaultDraft(input);
  const actor = updatedBy.trim();
  if (!actor) throw new LlmDefaultValidationError('修改者不能为空');
  if (input.expectedRevision !== undefined
    && (!Number.isSafeInteger(input.expectedRevision) || input.expectedRevision < 0)) {
    throw new LlmDefaultValidationError('expectedRevision 必须是非负整数');
  }

  const db = getDb();
  db.exec('BEGIN IMMEDIATE');
  try {
    const current = rowFor(input.purpose);
    if (input.expectedRevision !== undefined && input.expectedRevision !== (current?.revision ?? 0)) {
      throw new LlmDefaultRevisionConflictError();
    }
    let ciphertext = current?.api_key_ciphertext ?? null;
    if (input.apiKeyClear === true && input.apiKey) {
      throw new LlmDefaultValidationError('apiKey 与 apiKeyClear 不能同时提交');
    }
    if (input.apiKeyClear === true) ciphertext = null;
    else if (input.apiKey !== undefined && input.apiKey !== '') {
      ciphertext = encryptLlmDefaultApiKey(input.apiKey, input.purpose);
    }
    const revision = (current?.revision ?? 0) + 1;
    db.prepare(`INSERT INTO llm_default_settings (
        purpose, base_url, model, api_key_ciphertext, max_tokens, revision, updated_by, updated_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(purpose) DO UPDATE SET
        base_url = excluded.base_url,
        model = excluded.model,
        api_key_ciphertext = excluded.api_key_ciphertext,
        max_tokens = excluded.max_tokens,
        revision = excluded.revision,
        updated_by = excluded.updated_by,
        updated_at = excluded.updated_at`)
      .run(
        input.purpose,
        input.baseUrl.trim().replace(/\/+$/, ''),
        input.model.trim(),
        ciphertext,
        input.maxTokens,
        revision,
        actor,
        nowIso(),
      );
    db.exec('COMMIT');
  } catch (error) {
    try { db.exec('ROLLBACK'); } catch { /* transaction 已结束时忽略 */ }
    throw error;
  }
  return getPublicLlmDefaultSettings(input.purpose)!;
}

export function deleteLlmDefaultSettings(
  purpose: LlmPurpose,
  expectedRevision: number | undefined,
): void {
  if (expectedRevision !== undefined && (!Number.isSafeInteger(expectedRevision) || expectedRevision < 1)) {
    throw new LlmDefaultValidationError('expectedRevision 必须是正整数');
  }
  const db = getDb();
  db.exec('BEGIN IMMEDIATE');
  try {
    const current = rowFor(purpose);
    if (expectedRevision !== undefined && current?.revision !== expectedRevision) {
      throw new LlmDefaultRevisionConflictError();
    }
    db.prepare('DELETE FROM llm_default_settings WHERE purpose = ?').run(purpose);
    db.exec('COMMIT');
  } catch (error) {
    try { db.exec('ROLLBACK'); } catch { /* transaction 已结束时忽略 */ }
    throw error;
  }
}
