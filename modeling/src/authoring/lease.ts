import { randomUUID } from 'node:crypto';
import type { DatabaseSync } from 'node:sqlite';

import { getDb } from '../db/index.js';

const MAX_TTL_MS = 24 * 60 * 60 * 1000;

export const SHORT_WRITE_LEASE_TTL_MS = 2 * 60 * 1000;
export const ASSISTED_WRITE_LEASE_TTL_MS = 15 * 60 * 1000;

export interface ProjectLease {
  projectId: string;
  operation: string;
  owner: string;
  token: string;
  acquiredAt: string;
  updatedAt: string;
  expiresAt: string;
}

export interface ProjectOperationLeaseOptions {
  db?: DatabaseSync;
  now?: () => Date;
  createToken?: () => string;
}

export interface ProjectLeaseRunOptions {
  projectId: string;
  operation: string;
  owner: string;
  ttlMs: number;
}

export type ProjectLeaseErrorCode =
  | 'invalid_input'
  | 'project_not_found'
  | 'busy'
  | 'not_found'
  | 'expired'
  | 'not_owner';

export class ProjectLeaseError extends Error {
  constructor(
    public readonly code: ProjectLeaseErrorCode,
    message: string,
    public readonly activeOperation?: string,
  ) {
    super(message);
    this.name = 'ProjectLeaseError';
  }
}

/** 同一项目只允许一个有效写租约，操作名称仅用于向用户说明当前任务。 */
export class ProjectOperationLease {
  private readonly db: DatabaseSync;
  private readonly now: () => Date;
  private readonly createToken: () => string;

  constructor(options: ProjectOperationLeaseOptions = {}) {
    this.db = options.db ?? getDb();
    this.now = options.now ?? (() => new Date());
    this.createToken = options.createToken ?? (() => `lease_${randomUUID()}`);
  }

  acquire(projectId: string, operation: string, owner: string, ttlMs: number): ProjectLease {
    const input = validateLeaseInput(projectId, operation, owner, ttlMs);
    return this.transaction(() => {
      this.assertProject(input.projectId);
      const now = this.nowDate();
      const current = this.get(input.projectId);
      if (current && Date.parse(current.expiresAt) > now.getTime()) {
        throw new ProjectLeaseError('busy', '项目正在处理中', current.operation);
      }
      const token = requiredText(this.createToken(), '租约令牌', 200);
      const timestamp = now.toISOString();
      const expiresAt = new Date(now.getTime() + input.ttlMs).toISOString();
      this.db.prepare(`INSERT INTO project_operation_leases
        (project_id, operation, owner, token, acquired_at, updated_at, expires_at)
        VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(project_id) DO UPDATE SET
          operation = excluded.operation,
          owner = excluded.owner,
          token = excluded.token,
          acquired_at = excluded.acquired_at,
          updated_at = excluded.updated_at,
          expires_at = excluded.expires_at`)
        .run(input.projectId, input.operation, input.owner, token, timestamp, timestamp, expiresAt);
      return this.get(input.projectId)!;
    });
  }

  renew(projectId: string, owner: string, token: string, ttlMs: number): ProjectLease {
    const project = requiredText(projectId, '项目', 200);
    const normalizedOwner = requiredText(owner, '操作人', 200);
    const normalizedToken = requiredText(token, '租约令牌', 200);
    const ttl = validateTtl(ttlMs);
    return this.transaction(() => {
      const current = this.get(project);
      if (!current) throw new ProjectLeaseError('not_found', '项目未被占用');
      if (current.owner !== normalizedOwner || current.token !== normalizedToken) {
        throw new ProjectLeaseError('not_owner', '无法操作这个任务');
      }
      const now = this.nowDate();
      if (Date.parse(current.expiresAt) <= now.getTime()) {
        throw new ProjectLeaseError('expired', '任务占用已过期');
      }
      const timestamp = now.toISOString();
      const expiresAt = new Date(now.getTime() + ttl).toISOString();
      this.db.prepare(`UPDATE project_operation_leases SET updated_at = ?, expires_at = ?
        WHERE project_id = ? AND owner = ? AND token = ?`)
        .run(timestamp, expiresAt, project, normalizedOwner, normalizedToken);
      return this.get(project)!;
    });
  }

  release(projectId: string, owner: string, token: string): void {
    const project = requiredText(projectId, '项目', 200);
    const normalizedOwner = requiredText(owner, '操作人', 200);
    const normalizedToken = requiredText(token, '租约令牌', 200);
    this.transaction(() => {
      const current = this.get(project);
      if (!current) throw new ProjectLeaseError('not_found', '项目未被占用');
      if (current.owner !== normalizedOwner || current.token !== normalizedToken) {
        throw new ProjectLeaseError('not_owner', '无法操作这个任务');
      }
      this.db.prepare(`DELETE FROM project_operation_leases
        WHERE project_id = ? AND owner = ? AND token = ?`)
        .run(project, normalizedOwner, normalizedToken);
    });
  }

  private get(projectId: string): ProjectLease | null {
    const row = this.db.prepare('SELECT * FROM project_operation_leases WHERE project_id = ?').get(projectId);
    return row ? rowToLease(row as any) : null;
  }

  private assertProject(projectId: string): void {
    if (!this.db.prepare('SELECT 1 FROM projects WHERE id = ?').get(projectId)) {
      throw new ProjectLeaseError('project_not_found', '项目不存在');
    }
  }

  private nowDate(): Date {
    const value = this.now();
    if (!(value instanceof Date) || !Number.isFinite(value.getTime())) {
      throw new ProjectLeaseError('invalid_input', '系统时间不正确');
    }
    return value;
  }

  private transaction<T>(work: () => T): T {
    if (this.db.isTransaction) {
      const savepoint = `project_lease_${randomUUID().replaceAll('-', '')}`;
      this.db.exec(`SAVEPOINT ${savepoint}`);
      try {
        const result = work();
        this.db.exec(`RELEASE SAVEPOINT ${savepoint}`);
        return result;
      } catch (error) {
        this.db.exec(`ROLLBACK TO SAVEPOINT ${savepoint}`);
        this.db.exec(`RELEASE SAVEPOINT ${savepoint}`);
        throw error;
      }
    }
    this.db.exec('BEGIN IMMEDIATE');
    try {
      const result = work();
      this.db.exec('COMMIT');
      return result;
    } catch (error) {
      if (this.db.isTransaction) this.db.exec('ROLLBACK');
      throw error;
    }
  }
}

/** 抢占项目写租约，并保证操作结束后释放。 */
export async function runWithProjectLease<T>(
  lease: ProjectOperationLease,
  options: ProjectLeaseRunOptions,
  work: (activeLease: ProjectLease) => Promise<T> | T,
): Promise<T> {
  const active = lease.acquire(options.projectId, options.operation, options.owner, options.ttlMs);
  let renewalError: unknown;
  const renewEveryMs = Math.max(10, Math.floor(options.ttlMs / 3));
  const renewal = setInterval(() => {
    if (renewalError) return;
    try {
      lease.renew(active.projectId, active.owner, active.token, options.ttlMs);
    } catch (error) {
      renewalError = error;
    }
  }, renewEveryMs);
  renewal.unref?.();
  try {
    const result = await work(active);
    if (renewalError) throw renewalError;
    return result;
  } finally {
    clearInterval(renewal);
    try {
      lease.release(active.projectId, active.owner, active.token);
    } catch (error) {
      // 超时后可能已被新请求接管，旧请求不能释放新租约。
      if (!(error instanceof ProjectLeaseError)
        || (error.code !== 'not_found' && error.code !== 'not_owner')) {
        throw error;
      }
    }
  }
}

function rowToLease(row: any): ProjectLease {
  return {
    projectId: row.project_id,
    operation: row.operation,
    owner: row.owner,
    token: row.token,
    acquiredAt: row.acquired_at,
    updatedAt: row.updated_at,
    expiresAt: row.expires_at,
  };
}

function validateLeaseInput(projectId: string, operation: string, owner: string, ttlMs: number) {
  return {
    projectId: requiredText(projectId, '项目', 200),
    operation: requiredText(operation, '操作名称', 100),
    owner: requiredText(owner, '操作人', 200),
    ttlMs: validateTtl(ttlMs),
  };
}

function validateTtl(value: number): number {
  if (!Number.isInteger(value) || value < 1 || value > MAX_TTL_MS) {
    throw new ProjectLeaseError('invalid_input', '占用时长不正确');
  }
  return value;
}

function requiredText(value: string, label: string, maxLength: number): string {
  const text = String(value ?? '').trim();
  if (!text) throw new ProjectLeaseError('invalid_input', `${label}不能为空`);
  if (text.length > maxLength || /[\u0000-\u001f\u007f]/.test(text)) {
    throw new ProjectLeaseError('invalid_input', `${label}不正确`);
  }
  return text;
}
