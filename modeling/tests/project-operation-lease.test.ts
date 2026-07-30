import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { test } from 'node:test';

import {
  ProjectLeaseError,
  ProjectOperationLease,
  runWithProjectLease,
} from '../src/authoring/lease.js';

const schema = readFileSync(new URL('../src/db/schema.sql', import.meta.url), 'utf8');

function insertProject(db: DatabaseSync, id = 'pj_lease'): void {
  db.prepare(`INSERT INTO projects
    (id, owner, name, domain, background, stage, created_at, updated_at)
    VALUES (?,?,?,?,?,?,?,?)`).run(id, 'test', '租约项目', '', '', 'project_setup', '2026-01-01', '2026-01-01');
}

test('同一项目不同操作也只能有一个有效租约', () => {
  const db = new DatabaseSync(':memory:');
  db.exec('PRAGMA foreign_keys = ON');
  db.exec(schema);
  insertProject(db);
  const lease = new ProjectOperationLease({ db, createToken: () => 'token-1' });
  const acquired = lease.acquire('pj_lease', '生成知识包', 'worker-1', 60_000);
  assert.equal(acquired.operation, '生成知识包');

  assert.throws(() => lease.acquire('pj_lease', '修改文件', 'worker-2', 60_000),
    (error) => error instanceof ProjectLeaseError
      && error.code === 'busy' && error.message === '项目正在处理中'
      && error.activeOperation === '生成知识包');
  db.close();
});

test('过期租约可以接管，旧 owner 和 token 不能再操作', () => {
  const db = new DatabaseSync(':memory:');
  db.exec('PRAGMA foreign_keys = ON');
  db.exec(schema);
  insertProject(db);
  let now = Date.UTC(2026, 0, 1);
  let tokenNumber = 0;
  const lease = new ProjectOperationLease({
    db,
    now: () => new Date(now),
    createToken: () => `token-${++tokenNumber}`,
  });
  const first = lease.acquire('pj_lease', '生成知识包', 'worker-1', 1_000);
  now += 1_001;
  const second = lease.acquire('pj_lease', '恢复版本', 'worker-2', 2_000);
  assert.notEqual(second.token, first.token);
  assert.equal(second.owner, 'worker-2');

  assert.throws(() => lease.renew('pj_lease', 'worker-1', first.token, 1_000),
    (error) => error instanceof ProjectLeaseError && error.code === 'not_owner');
  assert.throws(() => lease.release('pj_lease', 'worker-1', first.token),
    (error) => error instanceof ProjectLeaseError && error.code === 'not_owner');

  const renewed = lease.renew('pj_lease', 'worker-2', second.token, 4_000);
  assert.equal(Date.parse(renewed.expiresAt), now + 4_000);
  lease.release('pj_lease', 'worker-2', second.token);
  const third = lease.acquire('pj_lease', '修改文件', 'worker-3', 1_000);
  assert.equal(third.owner, 'worker-3');
  db.close();
});

test('续租已过期任务会被拒绝', () => {
  const db = new DatabaseSync(':memory:');
  db.exec(schema);
  insertProject(db);
  let now = Date.UTC(2026, 0, 1);
  const lease = new ProjectOperationLease({ db, now: () => new Date(now), createToken: () => 'token' });
  const acquired = lease.acquire('pj_lease', '生成知识包', 'worker', 100);
  now += 101;
  assert.throws(() => lease.renew('pj_lease', 'worker', acquired.token, 100),
    (error) => error instanceof ProjectLeaseError
      && error.code === 'expired' && error.message === '任务占用已过期');
  db.close();
});

test('写操作失败后也会释放租约', async () => {
  const db = new DatabaseSync(':memory:');
  db.exec(schema);
  insertProject(db);
  let tokenNumber = 0;
  const lease = new ProjectOperationLease({
    db,
    createToken: () => `token-${++tokenNumber}`,
  });
  await assert.rejects(
    runWithProjectLease(lease, {
      projectId: 'pj_lease', operation: '修改知识包', owner: 'worker-1', ttlMs: 1_000,
    }, async () => { throw new Error('保存失败'); }),
    /保存失败/,
  );
  const next = lease.acquire('pj_lease', '恢复版本', 'worker-2', 1_000);
  assert.equal(next.owner, 'worker-2');
  lease.release(next.projectId, next.owner, next.token);
  db.close();
});

test('长操作会自动续期', async () => {
  const db = new DatabaseSync(':memory:');
  db.exec('PRAGMA foreign_keys = ON');
  db.exec(schema);
  insertProject(db);
  const lease = new ProjectOperationLease({ db });
  const task = runWithProjectLease(lease, {
    projectId: 'pj_lease', operation: '长任务', owner: 'worker', ttlMs: 45,
  }, () => new Promise((resolve) => setTimeout(resolve, 100)));

  await new Promise((resolve) => setTimeout(resolve, 70));
  assert.throws(() => lease.acquire('pj_lease', '并发任务', 'other', 100),
    (error) => error instanceof ProjectLeaseError && error.code === 'busy');
  await task;
  const next = lease.acquire('pj_lease', '后续任务', 'other', 100);
  lease.release(next.projectId, next.owner, next.token);
  db.close();
});

test('两个数据库连接抢同一项目时只有一个成功', () => {
  const directory = mkdtempSync(join(tmpdir(), 'project-lease-'));
  const path = join(directory, 'lease.db');
  const firstDb = new DatabaseSync(path);
  firstDb.exec('PRAGMA busy_timeout = 1000');
  firstDb.exec(schema);
  insertProject(firstDb);
  const secondDb = new DatabaseSync(path);
  secondDb.exec('PRAGMA busy_timeout = 1000');
  secondDb.exec('PRAGMA foreign_keys = ON');
  const first = new ProjectOperationLease({ db: firstDb, createToken: () => 'first-token' });
  const second = new ProjectOperationLease({ db: secondDb, createToken: () => 'second-token' });

  first.acquire('pj_lease', '生成知识包', 'worker-1', 60_000);
  assert.throws(() => second.acquire('pj_lease', '修改文件', 'worker-2', 60_000),
    (error) => error instanceof ProjectLeaseError && error.code === 'busy');

  secondDb.close();
  firstDb.close();
  rmSync(directory, { recursive: true, force: true });
});

test('旧项目库可以直接增加治理表', () => {
  const db = new DatabaseSync(':memory:');
  db.exec(`CREATE TABLE projects (id TEXT PRIMARY KEY);
    INSERT INTO projects (id) VALUES ('old-project');`);
  db.exec(schema);
  const tables = db.prepare(`SELECT name FROM sqlite_master
    WHERE type = 'table' AND name IN ('project_operation_leases')
    ORDER BY name`).all() as Array<{ name: string }>;
  assert.deepEqual(tables.map((row) => row.name), ['project_operation_leases']);
  db.close();
});
