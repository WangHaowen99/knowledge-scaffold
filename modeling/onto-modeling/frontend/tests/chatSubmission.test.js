import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { runInNewContext } from 'node:vm';

const source = readFileSync(new URL('../src/components/ChatPane.jsx', import.meta.url), 'utf8');
const start = source.indexOf('  const send = async');
const handler = source.slice(start, source.indexOf('  const confirmGoal =', start));

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

// Synthetic requests; setters deliberately do not rerender, reproducing two clicks
// before React commits the pending state or the server acknowledges the message.
function harness() {
  const requests = [], uploads = [], errors = [], warnings = [];
  const changes = { pending: [], uploads: [], input: [], state: [] };
  const view = { input: '', submitting: false };
  const context = {
    project: { id: 'project_alpha' }, attachments: [], uploading: false, goalPending: false,
    sendingRef: { current: false }, conversationIdRef: { current: 'default' },
    recallIndexRef: { current: -1 },
    setInput: (value) => {
      view.input = typeof value === 'function' ? value(view.input) : value;
      changes.input.push(view.input);
    },
    setAttachments: () => {}, setPending: (value) => changes.pending.push(value),
    setSubmitting: (value) => { view.submitting = value; },
    setUploading: () => {}, setUploads: (value) => changes.uploads.push(value),
    setState: (value) => changes.state.push(value),
    setConversationsEpoch: () => {}, onWorkspaceChanged: () => {},
    requestPermissionOnFirstBuild: () => {}, resourceSubdir: () => 'documents',
    validateResourceFile: () => ({ valid: true }), DETECTED_PURPOSE_LABELS: {},
    message: { error: (text) => errors.push(text), warning: (text) => warnings.push(text) },
    api: {
      sendMessage: (...args) => {
        const request = deferred();
        requests.push({ ...request, args });
        return request.promise;
      },
      uploadResource: () => {
        const request = deferred();
        uploads.push(request);
        return request.promise;
      },
    },
  };
  const send = runInNewContext(`${handler}\nsend`, context);
  return { context, send, requests, uploads, errors, warnings, changes, view };
}

for (const command of ['/ontology-extract-object', '/ontology-validate']) {
  test(`${command}: double click submits once before the request is acknowledged`, async () => {
    const h = harness();
    const first = h.send(command);
    const second = h.send(command);
    for (let i = 0; i < 10; i += 1) await Promise.resolve();
    assert.equal(h.requests.length, 1);
    await second;
    assert.equal(h.warnings.length, 1);
    assert.deepEqual(h.requests[0].args, ['project_alpha', command, 'default']);
    h.requests[0].resolve({ state: 'running' });
    await first;
    assert.equal(h.context.sendingRef.current, false);
  });
}

for (const command of ['/ontology-extract-object', '/ontology-validate']) {
  test(`${command}: rejected duplicate only shows a toast and never flashes a chat message`, async () => {
    const h = harness();
    h.view.input = 'synthetic draft';
    const sending = h.send(command);
    await Promise.resolve();
    assert.equal(h.view.input, 'synthetic draft');
    assert.deepEqual(h.changes.pending, []);
    const error = Object.assign(new Error('synthetic duplicate warning'), { status: 409 });
    h.requests[0].reject(error);
    await sending;
    assert.deepEqual(h.errors, ['synthetic duplicate warning']);
    assert.deepEqual(h.changes, { pending: [], uploads: [], input: [], state: [] });
    assert.equal(h.view.input, 'synthetic draft');
    assert.equal(h.view.submitting, false);
  });
}

test('a plain message keeps its draft until acceptance and relies on server events for the chat row', async () => {
  const h = harness();
  h.view.input = 'synthetic message';
  const sending = h.send(h.view.input);
  await Promise.resolve();
  assert.equal(h.view.input, 'synthetic message');
  assert.equal(h.view.submitting, true);
  assert.deepEqual(h.changes.pending, []);
  h.requests[0].resolve({ state: 'running' });
  await sending;
  assert.equal(h.view.input, '');
  assert.deepEqual(h.changes.pending, []);
  assert.deepEqual(h.changes.state, ['running']);
  assert.equal(h.view.submitting, false);
});

test('accepting a command does not clear a draft edited while the request was pending', async () => {
  const h = harness();
  h.view.input = 'synthetic message';
  const sending = h.send(h.view.input);
  await Promise.resolve();
  h.view.input = 'synthetic next message';
  h.requests[0].resolve({ state: 'running' });
  await sending;
  assert.equal(h.view.input, 'synthetic next message');
});

test('object, operation, then object submissions remain available while double clicks submit once each', async () => {
  const h = harness();
  const commands = ['/ontology-extract-object', '/ontology-extract-operation', '/ontology-extract-object'];
  for (const [index, command] of commands.entries()) {
    const first = h.send(command);
    const second = h.send(command);
    for (let i = 0; i < 10; i += 1) await Promise.resolve();
    assert.equal(h.requests.length, index + 1);
    await second;
    h.requests[index].resolve({ state: 'running' });
    await first;
  }
  assert.deepEqual(h.requests.map((request) => request.args[1]), commands);
});

test('a failed send releases the guard for an explicit retry', async () => {
  const h = harness();
  const first = h.send('/ontology-validate');
  await Promise.resolve();
  h.requests[0].reject(new Error('synthetic failure'));
  await first;
  assert.deepEqual(h.errors, ['synthetic failure']);
  const retry = h.send('/ontology-validate');
  await Promise.resolve();
  assert.equal(h.requests.length, 2);
  h.requests[1].resolve({ state: 'running' });
  await retry;
});

test('double click during resource upload neither uploads nor submits twice', async () => {
  const h = harness();
  h.context.attachments = [{ uid: 'synthetic-file', name: 'sample.md', file: {} }];
  const first = h.send('/ontology-extract-object');
  const second = h.send('/ontology-extract-object');
  assert.equal(h.uploads.length, 1);
  await second;
  assert.equal(h.changes.pending.length, 1);
  assert.equal(h.changes.pending[0].text, '/ontology-extract-object');
  h.uploads[0].resolve({ path: 'resource/documents/sample.md' });
  for (let i = 0; i < 10; i += 1) await Promise.resolve();
  assert.equal(h.requests.length, 1);
  h.requests[0].resolve({ state: 'running' });
  await first;
});

test('upload failure releases the guard before retrying', async () => {
  const h = harness();
  const attachment = { uid: 'synthetic-file', name: 'sample.md', file: {} };
  const first = h.send('', [attachment]);
  h.uploads[0].reject(new Error('synthetic upload failure'));
  await first;
  assert.equal(h.requests.length, 0);
  assert.equal(h.context.sendingRef.current, false);
  const retry = h.send('', [attachment]);
  h.uploads[1].resolve({ path: 'resource/documents/sample.md' });
  for (let i = 0; i < 10; i += 1) await Promise.resolve();
  h.requests[0].resolve({ state: 'running' });
  await retry;
});
