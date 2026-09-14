import { test } from 'node:test';
import assert from 'node:assert/strict';

import { normalizeCaseConcurrency, runCasesWithLimit } from '../src/evaluation/case-runner.js';

type Case = { id: string; delay: number };

const sleep = (ms: number) => new Promise<void>((resolve) => { setTimeout(resolve, ms); });

function cases(delays: number[]): Case[] {
  return delays.map((delay, index) => ({ id: `tc-${index + 1}`, delay }));
}

test('并发执行但结果仍按用例顺序提交', async () => {
  // 故意让靠后的用例先跑完：延时递减
  const items = cases([80, 60, 40, 20, 1]);
  const finished: string[] = [];
  const committed: string[] = [];

  await runCasesWithLimit<Case, string>(items, 5, {
    run: async (item) => {
      await sleep(item.delay);
      finished.push(item.id);
      return `${item.id}-outcome`;
    },
    recover: (_item, _index, error) => { throw error; },
    commit: (item, index, outcome) => {
      assert.equal(outcome, `${item.id}-outcome`);
      assert.equal(index, committed.length);
      committed.push(item.id);
    },
  });

  assert.deepEqual(finished, ['tc-5', 'tc-4', 'tc-3', 'tc-2', 'tc-1'], '完成顺序应与用例顺序相反');
  assert.deepEqual(committed, ['tc-1', 'tc-2', 'tc-3', 'tc-4', 'tc-5'], '提交顺序必须是用例顺序');
});

test('在途条数不超过并发上限', async () => {
  const items = cases(Array.from({ length: 9 }, () => 20));
  let inFlight = 0;
  let peak = 0;

  await runCasesWithLimit<Case, string>(items, 3, {
    run: async (item) => {
      inFlight += 1;
      peak = Math.max(peak, inFlight);
      await sleep(item.delay);
      inFlight -= 1;
      return item.id;
    },
    recover: (_item, _index, error) => { throw error; },
    commit: () => {},
  });

  assert.equal(peak, 3, '峰值在途条数应等于并发上限');
  assert.equal(inFlight, 0);
});

test('并发上限为 1 时退化为串行', async () => {
  const items = cases([30, 20, 10]);
  let inFlight = 0;
  let peak = 0;
  const order: string[] = [];

  await runCasesWithLimit<Case, string>(items, 1, {
    run: async (item) => {
      inFlight += 1;
      peak = Math.max(peak, inFlight);
      await sleep(item.delay);
      inFlight -= 1;
      order.push(item.id);
      return item.id;
    },
    recover: (_item, _index, error) => { throw error; },
    commit: () => {},
  });

  assert.equal(peak, 1);
  assert.deepEqual(order, ['tc-1', 'tc-2', 'tc-3']);
});

test('单条用例失败由 recover 兜底，其余用例继续跑完', async () => {
  const items = cases([10, 10, 10, 10]);
  const committed: string[] = [];

  await runCasesWithLimit<Case, string>(items, 4, {
    run: async (item) => {
      await sleep(item.delay);
      if (item.id === 'tc-2') throw new Error('boom');
      return `${item.id}-ok`;
    },
    recover: (item) => `${item.id}-recovered`,
    commit: (_item, _index, outcome) => { committed.push(outcome); },
  });

  assert.deepEqual(committed, ['tc-1-ok', 'tc-2-recovered', 'tc-3-ok', 'tc-4-ok']);
});

test('致命错误中止后续用例并向上抛出，已完成的前缀仍然提交', async () => {
  const items = cases([5, 5, 200, 200, 200, 200]);
  const started: string[] = [];
  const committed: string[] = [];
  const fatal = new Error('鉴权失败');

  await assert.rejects(
    runCasesWithLimit<Case, string>(items, 2, {
      onStart: (item) => { started.push(item.id); },
      run: async (item) => {
        await sleep(item.delay);
        if (item.id === 'tc-2') throw fatal;
        return item.id;
      },
      recover: (_item, _index, error) => { throw error; },
      commit: (item) => { committed.push(item.id); },
    }),
    (error: unknown) => error === fatal,
  );

  assert.deepEqual(committed, ['tc-1'], '致命用例之后的结果不得提交');
  assert.ok(started.length < items.length, '致命错误后不应再领取新用例');
});

test('外部取消后立刻停止领取新用例并抛出取消原因', async () => {
  const items = cases(Array.from({ length: 8 }, () => 30));
  const controller = new AbortController();
  const reason = new Error('已取消');
  const started: string[] = [];

  const running = runCasesWithLimit<Case, string>(items, 2, {
    onStart: (item) => { started.push(item.id); },
    run: async (item) => { await sleep(item.delay); return item.id; },
    recover: (_item, _index, error) => { throw error; },
    commit: () => {},
  }, controller.signal);

  setTimeout(() => controller.abort(reason), 40);
  await assert.rejects(running, (error: unknown) => error === reason);
  assert.ok(started.length < items.length, '取消后不应继续领取用例');
});

test('空用例集直接返回', async () => {
  await runCasesWithLimit<Case, string>([], 4, {
    run: async () => { throw new Error('不应执行'); },
    recover: (_item, _index, error) => { throw error; },
    commit: () => { throw new Error('不应提交'); },
  });
});

test('并发上限取值兜底', () => {
  assert.equal(normalizeCaseConcurrency(4), 4);
  assert.equal(normalizeCaseConcurrency(0), 1);
  assert.equal(normalizeCaseConcurrency(-3), 1);
  assert.equal(normalizeCaseConcurrency('abc'), 1);
  assert.equal(normalizeCaseConcurrency(1000), 16);
  assert.equal(normalizeCaseConcurrency(undefined, 2), 2);
});
