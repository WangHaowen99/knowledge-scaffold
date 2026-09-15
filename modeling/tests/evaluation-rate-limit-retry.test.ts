import { test } from 'node:test';
import assert from 'node:assert/strict';

import { isRateLimitedFailure, rateLimitBackoffMs, withRateLimitRetry } from '../src/evaluation/engine.js';

test('HTTP 429/503 判为限流', () => {
  assert.equal(isRateLimitedFailure(429, ''), true);
  assert.equal(isRateLimitedFailure(503, ''), true);
});

test('上游模型限流包在错误体里也要判为限流', () => {
  // 并发跑用例时实际遇到的：consumer 内部调模型被限流，错误体带上来
  assert.equal(isRateLimitedFailure(500, '429 status code (no body)'), true);
  assert.equal(isRateLimitedFailure(500, 'Error code: 429 - rate_limit_exceeded'), true);
  assert.equal(isRateLimitedFailure(500, 'Rate limit reached for model'), true);
  assert.equal(isRateLimitedFailure(502, 'Too Many Requests'), true);
});

test('普通失败不得误判为限流', () => {
  assert.equal(isRateLimitedFailure(500, 'internal server error'), false);
  assert.equal(isRateLimitedFailure(400, '知识包规格不完整'), false);
  assert.equal(isRateLimitedFailure(404, 'workspace not found'), false);
  // 4290/1429 这类包含 429 的数字不应命中
  assert.equal(isRateLimitedFailure(500, 'processed 4290 tokens'), false);
  assert.equal(isRateLimitedFailure(500, 'run 1429 failed'), false);
});

test('退避是指数增长且带抖动', () => {
  const lo = rateLimitBackoffMs(1, () => 0);
  const hi = rateLimitBackoffMs(1, () => 1);
  assert.equal(lo, 1600);                      // 2000 × 0.8
  assert.equal(hi, 2400);                      // 2000 × 1.2
  assert.ok(hi > lo, '抖动应产生区间而不是定值');

  const mid = (n: number) => rateLimitBackoffMs(n, () => 0.5);
  assert.equal(mid(1), 2000);
  assert.equal(mid(2), 4000);
  assert.equal(mid(3), 8000);
  assert.equal(mid(6), 30_000, '应封顶在 30 秒');
  assert.equal(mid(10), 30_000);
});

test('抖动落在基准值的 ±20% 内', () => {
  for (let attempt = 1; attempt <= 5; attempt += 1) {
    const base = Math.min(2000 * (2 ** (attempt - 1)), 30_000);
    for (let i = 0; i < 50; i += 1) {
      const value = rateLimitBackoffMs(attempt);
      assert.ok(value >= base * 0.8 - 1 && value <= base * 1.2 + 1,
        `attempt=${attempt} 退避 ${value} 超出 ${base} 的 ±20%`);
    }
  }
});

test('判分被限流也会重试——限流只在异常文案里，没有 HTTP 状态码', async () => {
  // 实测踩到的：被测 agent 跑完了，卡在判分调用上，整条用例记成 error。
  let calls = 0;
  const waits: number[] = [];
  const verdict = await withRateLimitRetry('评测判分', async () => {
    calls += 1;
    if (calls <= 2) throw new Error('429 status code (no body)');
    return { verdict: 'pass' as const, reason: '一致' };
  }, {
    backoffMs: (attempt) => attempt,
    onRateLimited: (_attempt, waitMs) => waits.push(waitMs),
  }, new AbortController().signal);

  assert.equal(calls, 3);
  assert.deepEqual(waits, [1, 2], '两次限流各等一次，退避递增');
  assert.equal(verdict.verdict, 'pass');
});

test('判分的非限流失败不重试，原样抛出', async () => {
  let calls = 0;
  await assert.rejects(
    () => withRateLimitRetry('评测判分', async () => {
      calls += 1;
      throw new Error('评测判分服务未按协议提交结论');
    }, { backoffMs: () => 1 }, new AbortController().signal),
    /未按协议提交结论/,
  );
  assert.equal(calls, 1, '协议错误重试也没用，不该浪费配额');
});

test('判分重试耗尽后报错点明是判分环节', async () => {
  await assert.rejects(
    () => withRateLimitRetry('评测判分', async () => {
      throw new Error('429 status code (no body)');
    }, { backoffMs: () => 1 }, new AbortController().signal),
    /评测判分 重试 6 次后仍被限流：429 status code \(no body\)/,
  );
});
