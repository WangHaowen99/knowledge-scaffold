// 第一次切换页面白屏（F2 第 1 条）：构建产物预压缩、首屏空闲后预取各路由的代码块、代码块地址失效时刷新一次
import assert from 'node:assert/strict';
import { mkdtemp, readFile, readdir, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { brotliDecompressSync, gunzipSync } from 'node:zlib';
import { MIN_SIZE, precompressFiles } from '../build/precompress.js';
import { prefetchWhenIdle } from '../src/app/prefetch.js';
import { reloadOnce } from '../src/app/routeChunks.js';

// ---------- 构建产物预压缩 ----------

test('预压缩：1 KB 以上的 js、css 各写 .br、.gz，解开与原文一致；小文件、图片不压', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'precompress-'));
  const big = `export const s = ${JSON.stringify('页面代码'.repeat(800))};\n`;
  await writeFile(join(dir, 'Studio-AbC1.js'), big);
  await writeFile(join(dir, 'page-AbC2.css'), `.a{color:red}\n`.repeat(200));
  await writeFile(join(dir, 'tiny-AbC3.js'), 'export {};\n');
  await writeFile(join(dir, 'logo-AbC4.png'), Buffer.alloc(4096, 1));
  await precompressFiles(dir, ['Studio-AbC1.js', 'page-AbC2.css', 'tiny-AbC3.js', 'logo-AbC4.png']);
  const files = (await readdir(dir)).sort();
  assert.deepEqual(files, [
    'Studio-AbC1.js', 'Studio-AbC1.js.br', 'Studio-AbC1.js.gz', 'logo-AbC4.png',
    'page-AbC2.css', 'page-AbC2.css.br', 'page-AbC2.css.gz', 'tiny-AbC3.js',
  ]);
  assert.equal(brotliDecompressSync(await readFile(join(dir, 'Studio-AbC1.js.br'))).toString(), big);
  assert.equal(gunzipSync(await readFile(join(dir, 'Studio-AbC1.js.gz'))).toString(), big);
  assert.ok((await readFile(join(dir, 'Studio-AbC1.js.br'))).length < Buffer.byteLength(big) / 3);
  assert.equal(MIN_SIZE, 1024);
});

// ---------- 预取 ----------

const manualIdle = () => {
  const queue = [];
  const idle = (fn) => queue.push(fn);
  const flush = async () => { while (queue.length) { queue.shift()(); await new Promise((r) => setTimeout(r, 0)); } };
  return { idle, flush, queue };
};

test('预取：等首屏之后、一个一个在空闲时取，取完一个才排下一个；失败的跳过', async () => {
  const calls = [];
  let release;
  const { idle, queue } = manualIdle();
  const loaders = [
    () => { calls.push('a'); return new Promise((r) => { release = r; }); },
    () => { calls.push('b'); return Promise.reject(new Error('offline')); },
    () => { calls.push('c'); return Promise.resolve(); },
  ];
  prefetchWhenIdle(loaders, { delay: 0, idle, saveData: false });
  await new Promise((r) => setTimeout(r, 5));
  assert.equal(queue.length, 1);
  queue.shift()();
  assert.deepEqual(calls, ['a']);
  await new Promise((r) => setTimeout(r, 5));
  assert.equal(queue.length, 0, '上一个没取完不排下一个');
  release();
  await new Promise((r) => setTimeout(r, 5));
  queue.shift()();
  await new Promise((r) => setTimeout(r, 5));
  queue.shift()();
  assert.deepEqual(calls, ['a', 'b', 'c']);
});

test('预取：取消后不再取；省流量模式整个不取', async () => {
  const calls = [];
  const { idle, flush } = manualIdle();
  const loaders = ['a', 'b'].map((k) => () => { calls.push(k); return Promise.resolve(); });
  const stop = prefetchWhenIdle(loaders, { delay: 0, idle, saveData: false });
  stop();
  await new Promise((r) => setTimeout(r, 5));
  await flush();
  assert.deepEqual(calls, []);
  prefetchWhenIdle(loaders, { delay: 0, idle, saveData: true });
  await new Promise((r) => setTimeout(r, 5));
  await flush();
  assert.deepEqual(calls, []);
});

// ---------- 代码块加载失败 ----------

const memoryStorage = () => {
  const m = new Map();
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)) };
};

test('代码块加载失败（重新部署后旧地址没了）：刷新一次；30 秒内又失败就不再刷，交给错误边界', () => {
  const storage = memoryStorage();
  let reloads = 0;
  const reload = () => { reloads += 1; };
  const err = new Error('Failed to fetch dynamically imported module');
  const pending = reloadOnce(err, { storage, now: 1000000, reload });
  assert.equal(reloads, 1);
  assert.ok(pending instanceof Promise, '刷新前一直挂起，页面不变');
  assert.throws(() => reloadOnce(err, { storage, now: 1000000 + 5000, reload }), err);
  assert.equal(reloads, 1);
  reloadOnce(err, { storage, now: 1000000 + 60000, reload });
  assert.equal(reloads, 2);
});
