import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'inflight-operations-'));

const { InFlightOperations, OperationAlreadyRunningError, INFLIGHT_TTL_MS } =
  await import('../src/in-flight-operations.js');
const { getDb } = await import('../src/db/index.js');

test('同一个键不能重复登记', async () => {
  const inFlight = new InFlightOperations();
  let release: () => void = () => {};
  const held = new Promise<void>((resolve) => { release = resolve; });
  const running = inFlight.run('p1:optimize', 'optimize', () => held);

  assert.equal(inFlight.current('p1:optimize'), 'optimize');
  await assert.rejects(
    inFlight.run('p1:optimize', 'optimize', async () => 'second'),
    (error: unknown) => error instanceof OperationAlreadyRunningError,
  );

  release();
  await running;
  assert.equal(inFlight.current('p1:optimize'), null, '结束后要清掉登记');
});

test('不同操作互不阻塞——键的粒度是项目加操作，不是整个项目', async () => {
  const inFlight = new InFlightOperations();
  let release: () => void = () => {};
  const held = new Promise<void>((resolve) => { release = resolve; });
  const running = inFlight.run('p2:optimize', 'optimize', () => held);

  const other = await inFlight.run('p2:check', 'check', async () => 'ok');
  assert.equal(other, 'ok');

  release();
  await running;
});

test('另一个实例的登记也能看见——这正是改成库表的原因', async () => {
  // 两个实例共享同一个库，等价于两个进程
  const instanceA = new InFlightOperations();
  const instanceB = new InFlightOperations();
  let release: () => void = () => {};
  const held = new Promise<void>((resolve) => { release = resolve; });
  const running = instanceA.run('p3:analyze', 'analyze', () => held);

  assert.equal(instanceB.current('p3:analyze'), 'analyze', 'B 必须看得到 A 上跑的任务');
  assert.equal(instanceB.currentByPrefix('p3:'), 'analyze');
  await assert.rejects(
    instanceB.run('p3:analyze', 'analyze', async () => 'dup'),
    (error: unknown) => error instanceof OperationAlreadyRunningError,
    'B 不能再跑一份',
  );

  release();
  await running;
  assert.equal(instanceB.current('p3:analyze'), null);
});

test('前缀匹配不会因为项目 id 里的下划线串到别的项目', () => {
  const at = new Date('2026-08-02T10:00:00.000Z');
  const inFlight = new InFlightOperations({ now: () => at });
  getDb().prepare(
    `INSERT INTO inflight_operations (key, operation, token, started_at, expires_at)
     VALUES ('pjXa:optimize', 'optimize', 't', ?, ?)`,
  ).run(at.toISOString(), new Date(at.getTime() + INFLIGHT_TTL_MS).toISOString());
  // LIKE 里 _ 是通配符：不转义的话 'pj_a:' 会匹配到 'pjXa:'
  assert.equal(inFlight.currentByPrefix('pj_a:'), null);
});

test('过期的登记不算数，不会把项目永久挡住', () => {
  const at = new Date('2026-08-02T12:00:00.000Z');
  getDb().prepare(
    `INSERT INTO inflight_operations (key, operation, token, started_at, expires_at)
     VALUES ('p4:stale', 'stale', 't', ?, ?)`,
  ).run(at.toISOString(), new Date(at.getTime() - 1_000).toISOString());
  const inFlight = new InFlightOperations({ now: () => at });
  assert.equal(inFlight.current('p4:stale'), null);
  assert.equal(inFlight.status('p4:stale'), null);
});

test('迟到的收尾不会删掉别人接手后的登记', async () => {
  const inFlight = new InFlightOperations();
  await inFlight.run('p5:job', 'job', async () => 'first');
  // 模拟前一轮结束后新一轮开始
  const second = inFlight.run('p5:job', 'job', async () => new Promise<string>((resolve) => {
    setTimeout(() => resolve('second'), 30);
  }));
  const beforeToken = getDb().prepare('SELECT token FROM inflight_operations WHERE key = ?')
    .get('p5:job') as { token: string } | undefined;
  assert.ok(beforeToken?.token, '第二轮应有自己的登记');
  assert.equal(await second, 'second');
});

test('任务抛错也要清掉登记，否则整个操作永远起不来', async () => {
  const inFlight = new InFlightOperations();
  await assert.rejects(inFlight.run('p6:boom', 'boom', async () => { throw new Error('炸了'); }));
  assert.equal(inFlight.current('p6:boom'), null);
});
