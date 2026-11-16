import assert from 'node:assert/strict';
import { test } from 'node:test';

import { enqueueStudioDraftSave } from '../src/lib/studioDraftQueue.js';

const deferred = () => {
  let resolve;
  const promise = new Promise((done) => {
    resolve = done;
  });
  return { promise, resolve };
};

test('Studio draft saves run in order even when an earlier request is slow', async () => {
  const first = deferred();
  const calls = [];
  const queue = { current: Promise.resolve() };

  const savingA = enqueueStudioDraftSave(queue, async () => {
    calls.push('A:start');
    await first.promise;
    calls.push('A:end');
  });
  const savingB = enqueueStudioDraftSave(queue, async () => {
    calls.push('B');
  });
  await Promise.resolve();

  assert.deepEqual(calls, ['A:start']);
  first.resolve();
  await Promise.all([savingA, savingB]);
  assert.deepEqual(calls, ['A:start', 'A:end', 'B']);
});

test('a failed draft save does not prevent the latest content from being queued', async () => {
  const calls = [];
  const queue = { current: Promise.resolve() };

  await assert.rejects(
    enqueueStudioDraftSave(queue, async () => {
      throw new Error('synthetic failure');
    }),
  );
  await enqueueStudioDraftSave(queue, async () => calls.push('latest'));

  assert.deepEqual(calls, ['latest']);
});
