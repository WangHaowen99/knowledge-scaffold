import { randomUUID } from 'node:crypto';
import type { DatabaseSync } from 'node:sqlite';
import { posix } from 'node:path';

import { getDb, nowIso } from '../db/index.js';

const OID_PATTERN = /^[0-9a-f]{40}$/;
const PROJECT_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const CAUSE_PATTERN = /^[a-z][a-z0-9_.-]{0,63}$/;
const ACTOR_KINDS = new Set<ArtifactRevisionActorKind>(['user', 'agent', 'system']);

export type ArtifactRevisionActorKind = 'user' | 'agent' | 'system';

export interface ArtifactRevisionRecord {
  projectId: string;
  oid: string;
  parentOid: string | null;
  sequence: number;
  cause: string;
  summary: string;
  actorKind: ArtifactRevisionActorKind;
  actorName: string;
  packageVersion?: string;
  changedPaths: string[];
  createdAt: string;
}

export interface RecordArtifactRevisionInput {
  projectId: string;
  oid: string;
  parentOid?: string | null;
  cause: string;
  summary: string;
  actorKind: ArtifactRevisionActorKind;
  actorName: string;
  packageVersion?: string | null;
  changedPaths?: readonly string[];
  createdAt?: string;
}

export type ArtifactRevisionIndexErrorCode =
  | 'invalid_input'
  | 'project_not_found';

export class ArtifactRevisionIndexError extends Error {
  constructor(
    public readonly code: ArtifactRevisionIndexErrorCode,
    message: string,
  ) {
    super(message);
    this.name = 'ArtifactRevisionIndexError';
  }
}

/**
 * Git 修订的查询索引。这里只保存列表所需信息，不保存任何文件内容。
 */
export class ArtifactRevisionRepo {
  constructor(private readonly db: DatabaseSync = getDb()) {}

  record(input: RecordArtifactRevisionInput): ArtifactRevisionRecord {
    const normalized = normalizeInput(input);
    return this.transaction(() => {
      const existing = this.get(normalized.projectId, normalized.oid);
      if (existing) return existing;
      this.assertProject(normalized.projectId);

      this.db.prepare(`INSERT INTO artifact_revisions
        (project_id, oid, parent_oid, sequence, cause, summary, actor_kind, actor_name,
         package_version, changed_paths_json, created_at)
        SELECT ?, ?, ?, COALESCE(MAX(sequence), 0) + 1, ?, ?, ?, ?, ?, ?, ?
        FROM artifact_revisions WHERE project_id = ?`)
        .run(
          normalized.projectId,
          normalized.oid,
          normalized.parentOid,
          normalized.cause,
          normalized.summary,
          normalized.actorKind,
          normalized.actorName,
          normalized.packageVersion,
          JSON.stringify(normalized.changedPaths),
          normalized.createdAt,
          normalized.projectId,
        );
      return this.get(normalized.projectId, normalized.oid)!;
    });
  }

  list(projectId: string, limit = 50): ArtifactRevisionRecord[] {
    const project = validateProjectId(projectId);
    if (!Number.isInteger(limit) || limit < 1 || limit > 500) {
      throw new ArtifactRevisionIndexError('invalid_input', '版本数量不正确');
    }
    return (this.db.prepare(`SELECT * FROM artifact_revisions
      WHERE project_id = ? ORDER BY sequence DESC LIMIT ?`).all(project, limit) as any[])
      .map(rowToRecord);
  }

  get(projectId: string, oid: string): ArtifactRevisionRecord | null {
    const row = this.db.prepare(`SELECT * FROM artifact_revisions
      WHERE project_id = ? AND oid = ?`).get(validateProjectId(projectId), validateOid(oid));
    return row ? rowToRecord(row as any) : null;
  }

  latest(projectId: string): ArtifactRevisionRecord | null {
    const row = this.db.prepare(`SELECT * FROM artifact_revisions
      WHERE project_id = ? ORDER BY sequence DESC LIMIT 1`).get(validateProjectId(projectId));
    return row ? rowToRecord(row as any) : null;
  }

  private assertProject(projectId: string): void {
    const project = this.db.prepare('SELECT 1 FROM projects WHERE id = ?').get(projectId);
    if (!project) throw new ArtifactRevisionIndexError('project_not_found', '项目不存在');
  }

  private transaction<T>(task: () => T): T {
    const savepoint = `artifact_revision_${randomUUID().replaceAll('-', '')}`;
    this.db.exec(`SAVEPOINT ${savepoint}`);
    try {
      const result = task();
      this.db.exec(`RELEASE SAVEPOINT ${savepoint}`);
      return result;
    } catch (error) {
      this.db.exec(`ROLLBACK TO SAVEPOINT ${savepoint}`);
      this.db.exec(`RELEASE SAVEPOINT ${savepoint}`);
      throw error;
    }
  }
}

interface NormalizedInput {
  projectId: string;
  oid: string;
  parentOid: string | null;
  cause: string;
  summary: string;
  actorKind: ArtifactRevisionActorKind;
  actorName: string;
  packageVersion: string | null;
  changedPaths: string[];
  createdAt: string;
}

function normalizeInput(input: RecordArtifactRevisionInput): NormalizedInput {
  const changedPaths = [...new Set((input.changedPaths ?? []).map(validateArtifactPath))].sort(compareNames);
  if (changedPaths.length > 10_000) {
    throw new ArtifactRevisionIndexError('invalid_input', '修改文件过多');
  }
  return {
    projectId: validateProjectId(input.projectId),
    oid: validateOid(input.oid),
    parentOid: input.parentOid ? validateOid(input.parentOid) : null,
    cause: validateCause(input.cause),
    summary: requiredText(input.summary, '版本说明', 500),
    actorKind: validateActorKind(input.actorKind),
    actorName: requiredText(input.actorName, '操作人', 100),
    packageVersion: optionalText(input.packageVersion, '包版本', 100),
    changedPaths,
    createdAt: validateCreatedAt(input.createdAt ?? nowIso()),
  };
}

function rowToRecord(row: any): ArtifactRevisionRecord {
  const changedPaths = JSON.parse(row.changed_paths_json) as unknown;
  if (!Array.isArray(changedPaths) || changedPaths.some((path) => typeof path !== 'string')) {
    throw new Error('版本文件记录格式不正确');
  }
  return {
    projectId: row.project_id,
    oid: row.oid,
    parentOid: row.parent_oid ?? null,
    sequence: Number(row.sequence),
    cause: row.cause,
    summary: row.summary,
    actorKind: row.actor_kind,
    actorName: row.actor_name,
    packageVersion: row.package_version ?? undefined,
    changedPaths: [...changedPaths],
    createdAt: row.created_at,
  };
}

function validateProjectId(value: string): string {
  if (!PROJECT_ID_PATTERN.test(value) || value === '.' || value === '..') {
    throw new ArtifactRevisionIndexError('invalid_input', '项目编号不正确');
  }
  return value;
}

function validateOid(value: string): string {
  if (!OID_PATTERN.test(value)) throw new ArtifactRevisionIndexError('invalid_input', '版本号不正确');
  return value;
}

function validateCause(value: string): string {
  if (!CAUSE_PATTERN.test(value)) throw new ArtifactRevisionIndexError('invalid_input', '版本原因不正确');
  return value;
}

function validateActorKind(value: ArtifactRevisionActorKind): ArtifactRevisionActorKind {
  if (!ACTOR_KINDS.has(value)) throw new ArtifactRevisionIndexError('invalid_input', '操作人类型不支持');
  return value;
}

function requiredText(value: string, label: string, maxLength: number): string {
  const text = String(value ?? '').trim();
  if (!text) throw new ArtifactRevisionIndexError('invalid_input', `${label}不能为空`);
  if (text.length > maxLength || /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(text)) {
    throw new ArtifactRevisionIndexError('invalid_input', `${label}不正确`);
  }
  return text;
}

function optionalText(value: string | null | undefined, label: string, maxLength: number): string | null {
  if (value === undefined || value === null) return null;
  const text = value.trim();
  if (!text) return null;
  if (text.length > maxLength || /[\u0000-\u001f\u007f]/.test(text)) {
    throw new ArtifactRevisionIndexError('invalid_input', `${label}不正确`);
  }
  return text;
}

function validateCreatedAt(value: string): string {
  const parsed = new Date(value);
  if (!value || !Number.isFinite(parsed.getTime())) {
    throw new ArtifactRevisionIndexError('invalid_input', '版本时间不正确');
  }
  return parsed.toISOString();
}

function validateArtifactPath(value: string): string {
  if (
    typeof value !== 'string'
    || value.length === 0
    || value.length > 1024
    || /[\u0000-\u001f\u007f]/.test(value)
    || value.includes('\\')
    || value.startsWith('/')
    || posix.normalize(value) !== value
    || value.split('/').some((part) => part === '' || part === '.' || part === '..' || part.toLowerCase() === '.git')
  ) {
    throw new ArtifactRevisionIndexError('invalid_input', '文件路径不正确');
  }
  return value;
}

function compareNames(left: string, right: string): number {
  return Buffer.compare(Buffer.from(left), Buffer.from(right));
}
