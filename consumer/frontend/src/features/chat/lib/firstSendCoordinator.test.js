import assert from 'node:assert/strict';
import test from 'node:test';

import {
  createFirstSendCoordinator,
  isBlankChatSubmitting,
  scheduleBlankChatFocus,
} from './firstSendCoordinator.js';

const deferred = () => {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
};

test('first chat creation and send stay single-flight across callers', async () => {
  const creation = deferred();
  const pendingChanges = [];
  const coordinator = createFirstSendCoordinator({
    onPendingChange: (pending) => pendingChanges.push(pending),
  });
  let createCalls = 0;
  let sendCalls = 0;

  const first = coordinator.run(async () => {
    createCalls += 1;
    const session = await creation.promise;
    sendCalls += 1;
    return session;
  });
  const remountedCaller = coordinator.run(async () => {
    createCalls += 1;
    sendCalls += 1;
    return { id: 'duplicate' };
  });

  await Promise.resolve();
  assert.equal(first.started, true);
  assert.equal(remountedCaller.started, false);
  assert.equal(remountedCaller.promise, first.promise);
  assert.equal(createCalls, 1);
  assert.equal(sendCalls, 0);
  assert.equal(coordinator.isPending(), true);

  creation.resolve({ id: 'session-1' });
  assert.deepEqual(await first.promise, { id: 'session-1' });
  await Promise.resolve();
  assert.equal(sendCalls, 1);
  assert.equal(coordinator.isPending(), false);
  assert.deepEqual(pendingChanges, [true, false]);
});

test('failed first send releases the coordinator for retry', async () => {
  const coordinator = createFirstSendCoordinator();
  const failure = coordinator.run(async () => {
    throw new Error('create failed');
  });

  await assert.rejects(failure.promise, /create failed/);
  await Promise.resolve();
  assert.equal(coordinator.isPending(), false);

  const retry = coordinator.run(async () => 'started');
  assert.equal(retry.started, true);
  assert.equal(await retry.promise, 'started');
});

test('reset isolates a new workspace flight from stale completion', async () => {
  const oldRequest = deferred();
  const newRequest = deferred();
  const coordinator = createFirstSendCoordinator();

  const oldFlight = coordinator.run(() => oldRequest.promise);
  coordinator.reset();
  const newFlight = coordinator.run(() => newRequest.promise);

  oldRequest.resolve('old');
  assert.equal(await oldFlight.promise, 'old');
  await Promise.resolve();
  assert.equal(coordinator.isPending(), true);

  newRequest.resolve('new');
  assert.equal(await newFlight.promise, 'new');
  await Promise.resolve();
  assert.equal(coordinator.isPending(), false);
});

test('pending first send only disables the blank chat', () => {
  assert.equal(isBlankChatSubmitting(null, true), true);
  assert.equal(isBlankChatSubmitting('', true), true);
  assert.equal(isBlankChatSubmitting('history-session', true), false);
  assert.equal(isBlankChatSubmitting(null, false), false);
});

test('failed first send restores focus only while the blank chat remains selected', () => {
  let currentChatId = null;
  let scheduled = null;
  let focusCalls = 0;
  const input = {
    focus: () => {
      focusCalls += 1;
    },
  };
  const schedule = () => scheduleBlankChatFocus({
    getCurrentChatId: () => currentChatId,
    getInput: () => input,
    scheduleFrame: (callback) => {
      scheduled = callback;
    },
  });

  assert.equal(schedule(), true);
  scheduled();
  assert.equal(focusCalls, 1);

  assert.equal(schedule(), true);
  currentChatId = 'history-session';
  scheduled();
  assert.equal(focusCalls, 1);
  assert.equal(schedule(), false);
});
