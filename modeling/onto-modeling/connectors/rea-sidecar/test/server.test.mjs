// 服务端：用 test/fake-rea 顶替 REA，核对白名单、环境变量、超时、取消、截断、上限、清理。
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { request } from 'node:http';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { after, before, test } from 'node:test';

import { createApp } from '../src/app.mjs';
import { runRea } from '../src/rea.mjs';

const here = fileURLToPath(new URL('.', import.meta.url));
const reaBin = join(here, 'fake-rea', 'scripts', 'rea.mjs');
const work = mkdtempSync(join(tmpdir(), 'rea-sidecar-test-'));
const scratch = join(work, 'scratch');
let app;
let base;
let counter = 0;

// 子进程不该看到测试进程的这些变量
process.env.HTTPS_PROXY = 'http://proxy.example.invalid:3128';
process.env.ONTO_TEST_SECRET = 'placeholder-not-a-secret';

before(async () => {
  app = createApp({
    scratch, reaBin, reaVersion: '6.8.0-fake', provider: 'rea-static@0.1.0',
    runnerPath: fileURLToPath(new URL('../src/runner.mjs', import.meta.url)), runnerHeapMb: 256,
    maxQueued: 1, maxBytes: 1024 * 1024, maxOutputBytes: 256 * 1024, maxEntries: 1000, maxInventory: 1000,
    maxAssemblies: 2, maxPrograms: 1, maxPyc: 5, ttlMs: 60_000,
  });
  await app.manager.cleanupScratch();
  await new Promise((resolve) => app.server.listen(0, '127.0.0.1', resolve));
  base = `http://127.0.0.1:${app.server.address().port}`;
});

after(async () => {
  app.manager.shutdown();
  await new Promise((resolve) => app.server.close(resolve));
  rmSync(work, { recursive: true, force: true });
});

/** 看起来像 asar 的假程序：头 4 字节是 4，偏移 16 起是 {"files": …}，后面跟替身认的标记。 */
function fakeAsar(marker) {
  const header = Buffer.from('{"files":{}}');
  const prefix = Buffer.alloc(16);
  prefix.writeUInt32LE(4, 0);
  prefix.writeUInt32LE(header.length + 8, 4);
  prefix.writeUInt32LE(header.length + 4, 8);
  prefix.writeUInt32LE(header.length, 12);
  return Buffer.concat([prefix, header, Buffer.from(`\n${marker}\n`)]);
}

async function submit(body, params = {}) {
  counter += 1;
  const id = params.id ?? `ej-t${counter}`;
  const query = new URLSearchParams({
    id, source: 'sample-app', filename: 'app.asar', sha256: createHash('sha256').update(body).digest('hex'),
    max_seconds: '60', max_records: '1000', ...params,
  });
  const response = await fetch(`${base}/v1/jobs?${query}`, { method: 'POST', body });
  return { id, response, data: await response.json() };
}

/** 旁路容器超限时不读请求体就回 413 并断开；fetch 会报 EPIPE，这里像网关那样先收回复。 */
function rawPost(path, body) {
  return new Promise((resolve, reject) => {
    const req = request(`${base}${path}`, { method: 'POST', headers: { 'Content-Length': body.length } }, (res) => {
      let text = '';
      res.setEncoding('utf8');
      res.on('data', (chunk) => { text += chunk; });
      res.on('end', () => resolve({ status: res.statusCode, data: JSON.parse(text) }));
    });
    req.on('error', () => {});
    req.end(body);
    setTimeout(() => reject(new Error('没有回复')), 5000).unref();
  });
}

async function waitFor(id, predicate = (status) => ['succeeded', 'partial', 'failed', 'cancelled'].includes(status.state)) {
  for (let attempt = 0; attempt < 200; attempt += 1) {
    const status = await (await fetch(`${base}/v1/jobs/${id}`)).json();
    if (predicate(status)) return status;
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error(`任务 ${id} 没有等到预期状态`);
}

function alive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

async function pidsFrom(file) {
  for (let attempt = 0; attempt < 100 && !existsSync(file); attempt += 1) await new Promise((resolve) => setTimeout(resolve, 50));
  return JSON.parse(readFileSync(file, 'utf8'));
}

async function waitDead(pids) {
  for (let attempt = 0; attempt < 100 && pids.some(alive); attempt += 1) await new Promise((resolve) => setTimeout(resolve, 50));
  return pids.filter(alive);
}

test('健康检查', async () => {
  const data = await (await fetch(`${base}/health`)).json();
  assert.deepEqual(data, { status: 'ok', rea_version: '6.8.0-fake', running: 0, queued: 0 });
});

test('白名单外的命令不起进程', async () => {
  const home = mkdtempSync(join(work, 'home-'));
  for (const command of ['mcp', 'setup', 'update', 'capture-process', 'debug-with-gdb', 'environment-serve']) {
    await assert.rejects(
      runRea(command, ['x'], { reaBin, cwd: home, env: { HOME: home }, stdoutPath: join(home, 'out'), maxOutputBytes: 10 }),
      (error) => error.code === 'command_not_allowed',
    );
  }
  assert.equal(existsSync(join(home, 'calls.log')), false);
});

test('完整流程：收程序、跑、取结果和记录和原始结果、删除', async () => {
  const { id, response, data } = await submit(fakeAsar('plain'));
  assert.equal(response.status, 202);
  assert.deepEqual(data, { id });
  const status = await waitFor(id);
  assert.equal(status.state, 'succeeded');
  assert.equal(status.phase, '完成');

  const result = await (await fetch(`${base}/v1/jobs/${id}/result`)).json();
  assert.equal(result.artifact.detected, 'javascript');
  assert.deepEqual(result.records, { entity: 1 });
  assert.equal(result.rea_version, '6.8.0-fake');
  assert.deepEqual(result.analyzers.map((item) => [item.command, item.provider]), [['analyze-javascript-application', 'rea-javascript-application@1']]);
  assert.deepEqual(result.limitations, ['fake limitation']);
  assert.equal(result.unknowns, 1);
  assert.deepEqual(result.inventory.items, [{ object: 'app.asar', kind: 'javascript', bytes: fakeAsar('plain').length }]);

  const lines = (await (await fetch(`${base}/v1/jobs/${id}/records`)).text()).trim().split('\n');
  assert.equal(lines.length, 1);
  assert.equal(JSON.parse(lines[0]).id, 'bin:sample-app/mod:main%2Ejs');

  const [raw] = result.raw;
  const body = Buffer.from(await (await fetch(`${base}/v1/jobs/${id}/raw/${raw.name}`)).arrayBuffer());
  assert.equal(body.length, raw.size);
  assert.equal(createHash('sha256').update(body).digest('hex'), raw.sha256);
  // REA 子进程只拿到 PATH、HOME（任务目录下）、LANG、TMPDIR
  const env = JSON.parse(body.toString('utf8')).fake_env;
  // macOS 会给每个进程补 __CF_USER_TEXT_ENCODING，不是我们传的
  assert.deepEqual(Object.keys(env).filter((key) => !key.startsWith('__CF_')).sort(), ['HOME', 'LANG', 'PATH', 'TMPDIR']);
  assert.ok(env.HOME.startsWith(join(scratch, id)));
  assert.ok(env.TMPDIR.startsWith(join(scratch, id)));
  assert.equal((await fetch(`${base}/v1/jobs/${id}/raw/..%2Fjob.json`)).status, 404);

  // 目标程序和中间文件结束就删，结果留到 DELETE
  assert.equal(existsSync(join(scratch, id, 'input')), false);
  assert.equal(existsSync(join(scratch, id, 'out', 'result.json')), true);
  const deleted = await fetch(`${base}/v1/jobs/${id}`, { method: 'DELETE' });
  assert.equal(deleted.status, 200);
  assert.deepEqual(await deleted.json(), { job_id: id, deleted: true });
  assert.equal((await fetch(`${base}/v1/jobs/${id}`)).status, 404);
  assert.equal(existsSync(join(scratch, id)), false);
});

test('超时杀整组进程', async () => {
  const pidFile = join(work, 'timeout-pids.json');
  const { id } = await submit(fakeAsar(`fake:sleep:${pidFile}`), { max_seconds: '1' });
  const running = await waitFor(id, (status) => status.done === 2);
  assert.deepEqual([running.state, running.phase, running.total, running.unit], ['running', '解析程序', 5, '文件']);
  const pids = await pidsFrom(pidFile);
  const status = await waitFor(id);
  assert.deepEqual([status.state, status.error_code], ['failed', 'timeout']);
  assert.deepEqual(await waitDead(pids), []);
  assert.equal((await fetch(`${base}/v1/jobs/${id}/result`)).status, 409);
  await fetch(`${base}/v1/jobs/${id}`, { method: 'DELETE' });
});

test('取消杀整组进程；排满回 503', async () => {
  const pidFile = join(work, 'cancel-pids.json');
  const first = await submit(fakeAsar(`fake:sleep:${pidFile}`));
  const pids = await pidsFrom(pidFile);
  const second = await submit(fakeAsar('plain'));
  assert.equal(second.response.status, 202);
  assert.equal((await waitFor(second.id, () => true)).state, 'queued');
  const third = await submit(fakeAsar('plain'));
  assert.equal(third.response.status, 503);
  assert.equal(third.data.error, 'queue_full');
  assert.equal((await fetch(`${base}/v1/jobs/${third.id}`)).status, 404);

  const cancelled = await fetch(`${base}/v1/jobs/${second.id}/cancel`, { method: 'POST' });
  assert.equal((await cancelled.json()).state, 'cancelled');
  await fetch(`${base}/v1/jobs/${first.id}/cancel`, { method: 'POST' });
  assert.equal((await waitFor(first.id)).state, 'cancelled');
  assert.deepEqual(await waitDead(pids), []);
  for (const id of [first.id, second.id]) await fetch(`${base}/v1/jobs/${id}`, { method: 'DELETE' });
});

test('max_records 超了截断，标 partial', async () => {
  const { id } = await submit(fakeAsar('fake:many:30'), { max_records: '10' });
  const status = await waitFor(id);
  assert.equal(status.state, 'partial');
  const result = await (await fetch(`${base}/v1/jobs/${id}/result`)).json();
  assert.deepEqual(result.records, { entity: 10 });
  assert.equal(result.truncated, true);
  assert.equal(result.coverage_pct, 33.3);
  assert.equal(result.complete, false);
  assert.ok(result.limitations.some((item) => item.includes('只保留前 10 条')));
  const lines = (await (await fetch(`${base}/v1/jobs/${id}/records`)).text()).trim().split('\n');
  assert.equal(lines.length, 10);
  await fetch(`${base}/v1/jobs/${id}`, { method: 'DELETE' });
});

test('REA 输出超限立即杀掉，任务失败', async () => {
  const { id } = await submit(fakeAsar('fake:big'));
  const status = await waitFor(id);
  assert.deepEqual([status.state, status.error_code], ['failed', 'analyzer_failed']);
  await fetch(`${base}/v1/jobs/${id}`, { method: 'DELETE' });
});

test('max_bytes 超了立即拒收；哈希不对拒收', async () => {
  const body = fakeAsar('plain');
  const sha256 = createHash('sha256').update(body).digest('hex');
  const tooLarge = await rawPost(`/v1/jobs?id=ej-large&source=s&sha256=${sha256}&max_bytes=${body.length - 1}`, body);
  assert.deepEqual([tooLarge.status, tooLarge.data.error], [413, 'too_large']);
  assert.equal((await fetch(`${base}/v1/jobs/ej-large`)).status, 404);
  // 网关给的 max_bytes 比旁路容器自己的上限大时，按自己的上限
  const huge = await rawPost(`/v1/jobs?id=ej-huge&source=s&sha256=${sha256}&max_bytes=${10 * 1024 * 1024}`, Buffer.alloc(1024 * 1024 + 1));
  assert.equal(huge.status, 413);

  const mismatch = await submit(body, { sha256: '0'.repeat(64) });
  assert.equal(mismatch.response.status, 422);
  assert.equal(mismatch.data.error, 'artifact_mismatch');
  assert.equal(existsSync(join(scratch, mismatch.id)), false);
});

test('参数校验、没有 Content-Length、重复 id', async () => {
  const bad = await submit(fakeAsar('plain'), { id: '../etc' });
  assert.equal(bad.response.status, 400);
  const chunked = await new Promise((resolve, reject) => {
    const req = request(`${base}/v1/jobs?id=ej-chunked&source=s&sha256=${'0'.repeat(64)}`, { method: 'POST' }, (res) => {
      res.resume();
      resolve(res.statusCode);
    });
    req.on('error', reject);
    req.write('abc');
    req.end();
  });
  assert.equal(chunked, 411);
  const first = await submit(fakeAsar('plain'), { id: 'ej-dup' });
  assert.equal(first.response.status, 202);
  const again = await submit(fakeAsar('plain'), { id: 'ej-dup' });
  assert.equal(again.response.status, 409);
  await waitFor('ej-dup');
  await fetch(`${base}/v1/jobs/ej-dup`, { method: 'DELETE' });
});

test('结束超过 ttl 没取的任务自动清', async () => {
  const { id } = await submit(fakeAsar('plain'));
  await waitFor(id);
  await app.manager.sweep(Date.now() + 30_000);
  assert.equal((await fetch(`${base}/v1/jobs/${id}`)).status, 200);
  await app.manager.sweep(Date.now() + 61_000);
  assert.equal((await fetch(`${base}/v1/jobs/${id}`)).status, 404);
  assert.equal(existsSync(join(scratch, id)), false);
});
