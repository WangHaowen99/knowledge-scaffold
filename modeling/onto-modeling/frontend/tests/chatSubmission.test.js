import assert from 'node:assert/strict';
import { test } from 'node:test';

import { createSubmitGuard, likelyDuplicate } from '../src/pages/workbench/composerModel.js';

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

// 页面用法：提交中再点直接返回 skipped，不发第二个请求（合成请求，不碰真实接口）
function harness() {
  const guard = createSubmitGuard();
  const requests = [];
  const warnings = [];
  const submitOnce = async (fn, skipped) => {
    if (guard.busy()) { warnings.push('busy'); return skipped; }
    return guard.run(fn);
  };
  const send = (command) => submitOnce(async () => {
    const request = deferred();
    requests.push({ command, request });
    await request.promise;
    return true;
  }, false);
  return { send, requests, warnings };
}

for (const command of ['/ontology-extract-object', '/ontology-validate']) {
  test(`${command}: double click submits once before the request is acknowledged`, async () => {
    const h = harness();
    const first = h.send(command);
    const second = h.send(command);
    assert.equal(await second, false);
    assert.equal(h.requests.length, 1);
    assert.deepEqual(h.warnings, ['busy']);
    h.requests[0].request.resolve();
    assert.equal(await first, true);
  });
}

test('the guard is released after success or failure, so an explicit retry goes out', async () => {
  const h = harness();
  const first = h.send('/ontology-validate');
  h.requests[0].request.reject(new Error('synthetic failure'));
  await assert.rejects(first, /synthetic failure/);
  const retry = h.send('/ontology-validate');
  assert.equal(h.requests.length, 2);
  h.requests[1].request.resolve();
  assert.equal(await retry, true);
  const again = h.send('/ontology-validate');
  assert.equal(h.requests.length, 3);
  h.requests[2].request.resolve();
  await again;
});

test('a repeated text while the turn is still running gets no optimistic bubble', () => {
  const last = { cid: 'default', text: '/ontology-extract-object' };
  for (const sessionState of ['running', 'connecting', 'queued']) {
    assert.equal(likelyDuplicate(last, { cid: 'default', text: ' /ontology-extract-object\n', sessionState }), true);
  }
});

test('idle sessions, other conversations and different text keep the optimistic bubble', () => {
  const last = { cid: 'default', text: '/ontology-extract-object' };
  assert.equal(likelyDuplicate(last, { cid: 'default', text: '/ontology-extract-object', sessionState: 'idle' }), false);
  assert.equal(likelyDuplicate(last, { cid: 'default', text: '/ontology-extract-object', sessionState: 'error' }), false);
  assert.equal(likelyDuplicate(last, { cid: 'conv-2', text: '/ontology-extract-object', sessionState: 'running' }), false);
  assert.equal(likelyDuplicate(last, { cid: 'default', text: '/ontology-validate', sessionState: 'running' }), false);
  assert.equal(likelyDuplicate(null, { cid: 'default', text: '/ontology-validate', sessionState: 'running' }), false);
  assert.equal(likelyDuplicate(last, { cid: 'default', text: '', sessionState: 'running' }), false);
});
