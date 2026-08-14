/**
 * 长任务的「同项目同操作只能跑一份」登记表。
 *
 * 原先是进程内的 Map。单实例够用，但有两个问题：
 * - 起两个实例后去重直接失效：A 上跑着的任务，B 完全不知道，会再跑一份。
 * - 界面靠 status()/currentByPrefix() 显示「正在做什么」，任务跑在另一个实例上时
 *   这边读到的是空，用户看到的是「没有任务在跑」——比不显示更误导。
 *
 * 改成库表之后跨进程可见。带 TTL 和续期：进程挂掉后登记不会永远留着，最坏卡一个
 * TTL；启动时还会统一清空（见 restart-recovery），因为上个进程的登记不可能再续期。
 *
 * 键的粒度是「项目 + 操作」（如 pj_x:optimize），比项目租约细。两者职责不同：
 * 租约保证同一项目同时只有一个写操作，这里保证同一个操作不会被重复触发。
 */
import { randomUUID } from 'node:crypto';
import type { DatabaseSync } from 'node:sqlite';
import { getDb } from './db/index.js';
import { currentLlmUser, runWithLlmUser } from './llm/user-context.js';

export class OperationAlreadyRunningError extends Error {
  constructor() {
    super('operation already running');
    this.name = 'OperationAlreadyRunningError';
  }
}

/**
 * 任务在检查点看到取消请求后抛这个。调用方据此把状态写成「已取消」而不是「失败」，
 * 两者对用户的含义完全不同：失败要追原因，取消是他自己按的。
 */
export class OperationCancelledError extends Error {
  constructor(operation: string) {
    super(`${operation}已按你的要求取消`);
    this.name = 'OperationCancelledError';
  }
}

/** 登记有效期。任务运行期间每 1/3 周期续一次，够长的任务也不会被误判成已结束 */
export const INFLIGHT_TTL_MS = 2 * 60 * 1000;

interface InFlightRow {
  operation: string;
  started_at: string;
}

export class InFlightOperations {
  private readonly db: DatabaseSync;
  private readonly now: () => Date;

  constructor(options: { db?: DatabaseSync; now?: () => Date } = {}) {
    this.db = options.db ?? getDb();
    this.now = options.now ?? (() => new Date());
  }

  current(key: string): string | null {
    return this.status(key)?.operation ?? null;
  }

  status(key: string): { operation: string; startedAt: string } | null {
    const row = this.db.prepare(
      'SELECT operation, started_at FROM inflight_operations WHERE key = ? AND expires_at > ?',
    ).get(key, this.now().toISOString()) as InFlightRow | undefined;
    return row ? { operation: row.operation, startedAt: row.started_at } : null;
  }

  currentByPrefix(prefix: string): string | null {
    // LIKE 的通配符要转义，否则项目 id 里的下划线会把别的项目也匹配进来
    const escaped = prefix.replaceAll('\\', '\\\\').replaceAll('%', '\\%').replaceAll('_', '\\_');
    const row = this.db.prepare(
      `SELECT operation FROM inflight_operations
       WHERE key LIKE ? ESCAPE '\\' AND expires_at > ? ORDER BY started_at LIMIT 1`,
    ).get(`${escaped}%`, this.now().toISOString()) as InFlightRow | undefined;
    return row?.operation ?? null;
  }

  /**
   * 请求取消。只置位，不中断——单次模型调用中途停不下来，任务要跑到下一个检查点
   * 才能干净地退出。返回 false 表示当前没有在跑的任务，界面据此提示而不是假装成功。
   */
  requestCancel(key: string): boolean {
    const changes = this.db.prepare(
      'UPDATE inflight_operations SET cancel_requested = 1 WHERE key = ? AND expires_at > ?',
    ).run(key, this.now().toISOString()).changes;
    return Number(changes) > 0;
  }

  isCancelRequested(key: string): boolean {
    const row = this.db.prepare(
      'SELECT cancel_requested FROM inflight_operations WHERE key = ? AND expires_at > ?',
    ).get(key, this.now().toISOString()) as { cancel_requested?: number } | undefined;
    return Number(row?.cancel_requested ?? 0) === 1;
  }

  /** 供长任务在检查点调用：已请求取消就抛出，让调用方统一收尾 */
  throwIfCancelled(key: string, operation: string): void {
    if (this.isCancelRequested(key)) throw new OperationCancelledError(operation);
  }

  async run<T>(key: string, operation: string, task: () => Promise<T>): Promise<T> {
    const token = this.acquire(key, operation);
    const renewal = setInterval(() => {
      try { this.renew(key, token); } catch { /* 续期失败不打断任务，最坏是登记提前过期 */ }
    }, Math.max(1_000, Math.floor(INFLIGHT_TTL_MS / 3)));
    renewal.unref?.();
    try {
      const username = currentLlmUser();
      return await (username ? runWithLlmUser(username, task) : task());
    } finally {
      clearInterval(renewal);
      this.release(key, token);
    }
  }

  private acquire(key: string, operation: string): string {
    const at = this.now();
    const token = `inflight_${randomUUID()}`;
    return this.transaction(() => {
      const existing = this.db.prepare(
        'SELECT expires_at FROM inflight_operations WHERE key = ?',
      ).get(key) as { expires_at: string } | undefined;
      if (existing && Date.parse(existing.expires_at) > at.getTime()) {
        throw new OperationAlreadyRunningError();
      }
      this.db.prepare(
        `INSERT INTO inflight_operations (key, operation, token, started_at, expires_at)
         VALUES (?,?,?,?,?)
         ON CONFLICT(key) DO UPDATE SET
           operation = excluded.operation, token = excluded.token,
           started_at = excluded.started_at, expires_at = excluded.expires_at`,
      ).run(key, operation, token, at.toISOString(),
        new Date(at.getTime() + INFLIGHT_TTL_MS).toISOString());
      return token;
    });
  }

  private renew(key: string, token: string): void {
    this.db.prepare('UPDATE inflight_operations SET expires_at = ? WHERE key = ? AND token = ?')
      .run(new Date(this.now().getTime() + INFLIGHT_TTL_MS).toISOString(), key, token);
  }

  /** 只删自己那条：登记过期后被别人接手时，迟到的收尾不能把人家的删掉 */
  private release(key: string, token: string): void {
    try {
      this.db.prepare('DELETE FROM inflight_operations WHERE key = ? AND token = ?').run(key, token);
    } catch { /* 收尾失败不覆盖任务本身的结果 */ }
  }

  private transaction<T>(work: () => T): T {
    if (this.db.isTransaction) {
      const savepoint = `inflight_${randomUUID().replaceAll('-', '')}`;
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
      try { this.db.exec('ROLLBACK'); } catch { /* 没有活动事务时忽略 */ }
      throw error;
    }
  }
}
