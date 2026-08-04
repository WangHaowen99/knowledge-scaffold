import assert from 'node:assert/strict';
import test from 'node:test';

import { runAgent } from '../src/workflow/agent.js';

test('模型任务超过时限会中止并返回可识别的超时错误', async () => {
  let aborted = false;
  const agent = {
    state: { messages: [] },
    subscribe: () => () => undefined,
    prompt: async () => undefined,
    waitForIdle: async () => new Promise<void>(() => undefined),
    abort: () => { aborted = true; },
  };

  const outcome = await Promise.race([
    runAgent(agent as any, '执行原子修订', undefined, { timeoutMs: 20 })
      .then(() => 'completed', (error) => error),
    new Promise((resolve) => setTimeout(() => resolve('hung'), 150)),
  ]);

  assert.notEqual(outcome, 'hung');
  assert.match(String((outcome as Error)?.message ?? outcome), /模型任务.*超时/);
  assert.equal(aborted, true);
});

test('模型流把供应商错误写入 agent state 时必须向调用方抛出', async () => {
  const agent = {
    state: {
      messages: [{ role: 'assistant', stopReason: 'error', errorMessage: '429 insufficient_quota', content: [] }],
      errorMessage: '429 insufficient_quota',
    },
    subscribe: () => () => undefined,
    prompt: async () => undefined,
    waitForIdle: async () => undefined,
    abort: () => undefined,
  };

  await assert.rejects(
    runAgent(agent as any, '执行诊断'),
    /429 insufficient_quota/,
  );
});

test('取消模型任务会等待底层运行完成中断清理后再返回', async () => {
  const controller = new AbortController();
  let releaseRun: () => void = () => undefined;
  let waitStarted: () => void = () => undefined;
  const started = new Promise<void>((resolve) => { waitStarted = resolve; });
  const running = new Promise<void>((resolve) => { releaseRun = resolve; });
  let aborted = false;
  const agent = {
    state: { messages: [] },
    subscribe: () => () => undefined,
    prompt: async () => undefined,
    waitForIdle: async () => {
      waitStarted();
      await running;
    },
    abort: () => { aborted = true; },
  };

  let settled = false;
  const outcome = runAgent(agent as any, '执行评测判分', undefined, { signal: controller.signal })
    .then(() => undefined, (error) => error)
    .finally(() => { settled = true; });
  await started;
  controller.abort(new Error('本轮评测已由用户停止'));
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(aborted, true);
  assert.equal(settled, false);
  releaseRun();
  const error = await outcome;
  assert.match(String(error?.message ?? error), /本轮评测已由用户停止/);
});

test('全文读取的共享回合预算跨多次内部继续提示累计消耗', async () => {
  let listener: ((event: unknown) => void) | undefined;
  let aborted = 0;
  const agent = {
    state: { messages: [] },
    subscribe: (next: (event: unknown) => void) => { listener = next; return () => undefined; },
    prompt: async () => { listener?.({ type: 'turn_end' }); },
    waitForIdle: async () => undefined,
    abort: () => { aborted += 1; },
  };
  const turnBudget = { remaining: 2 };

  await runAgent(agent as any, '第一次继续读取', undefined, { turnBudget });
  await runAgent(agent as any, '第二次继续读取', undefined, { turnBudget });

  assert.equal(turnBudget.remaining, 0);
  assert.equal(aborted, 1);
});
