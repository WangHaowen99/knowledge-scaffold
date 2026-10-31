import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'evaluation-prepare-failure-retry-'));

const { isTransientRunFailure, runCase } = await import('../src/evaluation/engine.js');

/**
 * 造一个假的 consumer：前 n 次 runs/sync 以 2xx 返回「会话运行状态准备失败」终态
 * （consumer 并发下会话老化撞上摘要索引投影冲突时的真实响应），之后返回正常结果。
 */
function prepareFailedThen(n: number, terminal: Record<string, unknown>) {
  let calls = 0;
  const sessions: string[] = [];
  const request = async (_token: string, _path: string, options?: RequestInit) => {
    calls += 1;
    const body = JSON.parse(String(options?.body ?? '{}')) as { session_id?: string };
    sessions.push(String(body.session_id ?? ''));
    if (calls <= n) {
      return new Response(JSON.stringify({
        status: 'failed',
        final_answer: '',
        session_id: body.session_id,
        run_id: `run-${calls}`,
        ...terminal,
      }), { status: 200, headers: { 'Content-Type': 'application/json' } });
    }
    return new Response(JSON.stringify({
      session_id: body.session_id,
      run_id: `run-${calls}`,
      final_answer: '评估完成',
      trace: { run_id: `run-${calls}`, session_id: body.session_id, status: 'complete', tool_calls: [] },
    }), { status: 200, headers: { 'Content-Type': 'application/json' } });
  };
  return { request: request as any, calls: () => calls, sessions };
}

const PREPARE_FAILED = {
  error: '会话运行状态准备失败',
  error_code: 'SESSION_PREPARE_FAILED',
  retriable: false,
};

test('SESSION_PREPARE_FAILED 终态判为瞬时失败；旧版 consumer 只有文案也要认', () => {
  assert.equal(isTransientRunFailure({ status: 'failed', ...PREPARE_FAILED }), true);
  assert.equal(isTransientRunFailure({ status: 'failed', error: '会话运行状态准备失败' }), true);
});

test('其他失败终态、成功和等待输入都不是瞬时失败', () => {
  assert.equal(isTransientRunFailure({ status: 'failed', error: '运行失败', error_code: 'INTERNAL_ERROR' }), false);
  assert.equal(isTransientRunFailure({ status: 'completed', final_answer: '答案' }), false);
  assert.equal(isTransientRunFailure({ status: 'awaiting_input', pending_user_interaction: {} }), false);
  assert.equal(isTransientRunFailure(undefined), false);
});

test('会话准备失败换新会话重试，最终拿到答案', async () => {
  const fake = prepareFailedThen(2, PREPARE_FAILED);
  const retries: number[] = [];
  const result = await runCase(fake.request, 'ws-1', '问题', new AbortController().signal, {
    onTransientFailure: (attempt) => { retries.push(attempt); },
    backoffMs: () => 1,
  });

  assert.equal(result.finalAnswer, '评估完成');
  assert.equal(fake.calls(), 3, '两次准备失败 + 一次成功');
  assert.deepEqual(retries, [1, 2], '每次重试都应回调，便于写进轮次日志');
  assert.equal(new Set(fake.sessions).size, 3, '失败的会话已留下 failed 轮次，重试必须换新会话');
});

test('会话准备失败重试耗尽后报错点明原因', async () => {
  const fake = prepareFailedThen(99, PREPARE_FAILED);
  await assert.rejects(
    runCase(fake.request, 'ws-1', '问题', new AbortController().signal, { backoffMs: () => 1 }),
    (error: Error) => /重试 6 次后仍然会话运行状态准备失败/u.test(error.message),
  );
  assert.equal(fake.calls(), 6, '达到上限即停止，不无限重试');
});

test('非瞬时的失败终态不重试，原样抛出', async () => {
  const fake = prepareFailedThen(99, { error: '运行失败', error_code: 'INTERNAL_ERROR' });
  await assert.rejects(
    runCase(fake.request, 'ws-1', '问题', new AbortController().signal, { backoffMs: () => 1 }),
    /运行失败/u,
  );
  assert.equal(fake.calls(), 1);
});
