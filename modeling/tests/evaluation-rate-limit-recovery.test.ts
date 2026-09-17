import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'evaluation-rate-limit-recovery-'));

const { runCase } = await import('../src/evaluation/engine.js');

/** 造一个假的 consumer：前 n 次把上游限流包在响应体里返回，之后返回正常结果。 */
function rateLimitedThen(n: number, body: string) {
  let calls = 0;
  const seen: string[] = [];
  const request = async (_token: string, path: string) => {
    calls += 1;
    seen.push(path);
    if (calls <= n) {
      return new Response(JSON.stringify({ detail: body }), {
        status: 500,
        headers: { 'Content-Type': 'application/json' },
      });
    }
    return new Response(JSON.stringify({
      session_id: 'sess-1',
      run_id: 'run-1',
      final_answer: '评估完成',
      trace: { run_id: 'run-1', session_id: 'sess-1', status: 'complete', tool_calls: [] },
    }), { status: 200, headers: { 'Content-Type': 'application/json' } });
  };
  return { request: request as any, calls: () => calls, seen };
}

test('上游限流被重试消化，最终拿到答案', async () => {
  const fake = rateLimitedThen(2, '429 status code (no body)');
  const retries: number[] = [];
  const result = await runCase(fake.request, 'ws-1', '问题', new AbortController().signal, {
    onRateLimited: (attempt) => { retries.push(attempt); },
    backoffMs: () => 1,
  });

  assert.equal(result.finalAnswer, '评估完成');
  assert.equal(fake.calls(), 3, '两次限流 + 一次成功');
  assert.deepEqual(retries, [1, 2], '两次限流都应回调，便于写进轮次日志');
});

test('重试耗尽后报错带上原始限流文案', async () => {
  const fake = rateLimitedThen(99, 'Error code: 429 - rate_limit_exceeded');
  await assert.rejects(
    runCase(fake.request, 'ws-1', '问题', new AbortController().signal, { backoffMs: () => 1 }),
    (error: Error) => /重试 6 次后仍被限流/u.test(error.message)
      && /rate_limit_exceeded/u.test(error.message),
  );
  assert.equal(fake.calls(), 6, '达到上限即停止，不无限重试');
});

test('非限流失败不重试，直接抛出', async () => {
  const fake = rateLimitedThen(99, '知识包规格不完整');
  await assert.rejects(
    runCase(fake.request, 'ws-1', '问题', new AbortController().signal, { backoffMs: () => 1 }),
    (error: Error) => /知识包规格不完整/u.test(error.message),
  );
  assert.equal(fake.calls(), 1, '普通失败只调一次');
});

test('取消信号能打断重试等待', async () => {
  const fake = rateLimitedThen(99, '429 status code (no body)');
  const controller = new AbortController();
  // 这条故意用真实退避：要验证取消能打断「正在等待」的重试
  const running = runCase(fake.request, 'ws-1', '问题', controller.signal);
  setTimeout(() => controller.abort(new Error('已取消')), 50);
  await assert.rejects(running);
  assert.ok(fake.calls() <= 2, `取消后不应继续重试，实际调用 ${fake.calls()} 次`);
});
