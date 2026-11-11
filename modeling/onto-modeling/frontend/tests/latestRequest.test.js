import assert from 'node:assert/strict';
import test from 'node:test';

import { createAbortableRequest, createGenerationGuard, createLatestRequest, createSerialPoller } from '../src/api/latestRequest.js';

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

test('a late response cannot overwrite the latest selected file', async () => {
  const first = deferred();
  const second = deferred();
  const values = [];
  let settled = 0;
  const latest = createLatestRequest();
  const callbacks = {
    success: (value) => values.push(value),
    failure: () => assert.fail('request should not fail'),
    settled: () => { settled += 1; },
  };

  const firstRun = latest.run(() => first.promise, callbacks);
  const secondRun = latest.run(() => second.promise, callbacks);
  second.resolve('file-b');
  await secondRun;
  first.resolve('file-a');
  await firstRun;

  assert.deepEqual(values, ['file-b']);
  assert.equal(settled, 1);
});

test('invalidating a request prevents a closed dialog from being updated', async () => {
  const pending = deferred();
  const latest = createLatestRequest();
  let updated = false;
  const run = latest.run(() => pending.promise, {
    success: () => { updated = true; },
    failure: () => { updated = true; },
    settled: () => { updated = true; },
  });

  latest.invalidate();
  pending.resolve('ignored');
  await run;

  assert.equal(updated, false);
});

test('an invalidated request can dispose a resource returned after the dialog closed', async () => {
  const pending = deferred();
  const latest = createLatestRequest();
  const disposed = [];
  const run = latest.run(() => pending.promise, {
    success: () => assert.fail('stale request must not update the dialog'),
    failure: () => assert.fail('request should not fail'),
    settled: () => assert.fail('stale request must not update loading state'),
    stale: (value) => disposed.push(value.id),
  });
  latest.invalidate();
  pending.resolve({ id: 'server-resource' });
  await run;
  assert.deepEqual(disposed, ['server-resource']);
});

test('a late poll for the old comparison cannot replace the new comparison', async () => {
  const oldPoll = deferred();
  const newPoll = deferred();
  const latest = createLatestRequest();
  let result = null;
  const callbacks = {
    success: (value) => { result = value; },
    failure: () => assert.fail('poll should not fail'),
    settled: () => {},
  };

  const oldRun = latest.run(() => oldPoll.promise, callbacks);
  latest.invalidate();
  const newRun = latest.run(() => newPoll.promise, callbacks);
  newPoll.resolve({ comparison_id: 'cmp_new', status: 'ready' });
  await newRun;
  oldPoll.resolve({ comparison_id: 'cmp_old', status: 'ready' });
  await oldRun;

  assert.equal(result.comparison_id, 'cmp_new');
});

test('a slow poll response is accepted and the next poll waits for it', async () => {
  const response = deferred();
  const values = [];
  let calls = 0;
  const stop = createSerialPoller({
    delay: 1,
    poll: async () => { calls += 1; return response.promise; },
    onValue: (value) => values.push(value),
    onError: assert.fail,
    isDone: (value) => value.status === 'ready',
  });

  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(calls, 1);
  response.resolve({ comparison_id: 'cmp_slow', status: 'ready' });
  await new Promise((resolve) => setImmediate(resolve));
  stop();

  assert.deepEqual(values, [{ comparison_id: 'cmp_slow', status: 'ready' }]);
});

test('source changes during awaited cleanup invalidate the pending compare action', async () => {
  const cleanup = deferred();
  const guard = createGenerationGuard();
  let created = false;
  const compare = async () => {
    const generation = guard.begin();
    await cleanup.promise;
    if (!guard.isCurrent(generation)) return;
    created = true;
  };
  const pending = compare();
  guard.invalidate();
  cleanup.resolve();
  await pending;
  assert.equal(created, false);
});

test('cancelling during awaited cleanup invalidates the pending compare action', async () => {
  const cleanup = deferred();
  const guard = createGenerationGuard();
  let created = false;
  const compare = async () => {
    const generation = guard.begin();
    await cleanup.promise;
    if (!guard.isCurrent(generation)) return;
    created = true;
  };
  const pending = compare();
  guard.invalidate();
  cleanup.resolve();
  await pending;
  assert.equal(created, false);
});

test('a terminal polling error stops retries', async () => {
  let calls = 0;
  createSerialPoller({
    delay: 1,
    poll: async () => { calls += 1; throw Object.assign(new Error('expired'), { status: 410 }); },
    onValue: () => assert.fail('poll must fail'),
    onError: (error) => error.status === 410,
    isDone: () => false,
  });
  await new Promise((resolve) => setTimeout(resolve, 15));
  assert.equal(calls, 1);
});

test('starting or cancelling an upload aborts the previous network request', () => {
  const request = createAbortableRequest();
  const first = request.begin();
  const second = request.begin();
  assert.equal(first.aborted, true);
  assert.equal(second.aborted, false);
  request.abort();
  assert.equal(second.aborted, true);
});
