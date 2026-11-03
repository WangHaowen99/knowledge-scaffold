import assert from 'node:assert/strict';
import test from 'node:test';
import { prime, take } from '../src/app/earlyData.js';

test('首屏提前取数：挂载时接着用已发出的请求，只用一次', async () => {
  let calls = 0;
  const start = () => { calls += 1; return Promise.resolve(calls); };
  prime('k1', start, 1000);
  assert.equal(await take('k1', start, 1500), 1);
  assert.equal(await take('k1', start, 1600), 2, '第二次取不到提前的结果，照常请求');
});

test('首屏提前取数：超过 10 秒的结果不用，没提前发过的照常请求', async () => {
  let calls = 0;
  const start = () => { calls += 1; return Promise.resolve(calls); };
  prime('k2', start, 1000);
  assert.equal(await take('k2', start, 12000), 2);
  assert.equal(await take('k3', start, 12000), 3);
});

test('首屏提前取数：提前发出的请求失败不抛未处理的拒绝，取走后照常拿到错误', async () => {
  prime('k4', () => Promise.reject(new Error('boom')), 1000);
  await assert.rejects(take('k4', () => Promise.resolve('fresh'), 1500), /boom/);
});
