// develop !608 tests/studioEditorExitBehavior.test.js 按原意移植：develop 在旧 StudioEntryEditor 里驱动「退出」，
// 本分支两个编辑器的暂存都走 enqueueLatestDraftSave（排到时才读最新内容），这里直接驱动它。
// 本分支没有租约，原用例里申请 / 释放租约的步骤去掉，其余断言照旧。
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { enqueueLatestDraftSave } from '../src/lib/studioDraftQueue.js';

const tick = () => new Promise((resolve) => setImmediate(resolve));
const deferred = () => {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};

// 编辑器的最小模型：content 是表单当前内容，flush 是自动暂存 / 离开前补存
function editorHarness(save) {
  const queue = { current: Promise.resolve() };
  const dirty = { current: false };
  const h = {
    content: 'original',
    save,
    dirty,
    edit(value) { h.content = value; dirty.current = true; },
    flush() { return enqueueLatestDraftSave(queue, dirty, () => h.save(h.content)); },
  };
  return h;
}

test('unchanged exit never creates a draft', async () => {
  const calls = [];
  const h = editorHarness(async (content) => calls.push(content));
  assert.deepEqual(await h.flush(), { ok: true, saved: false });
  assert.deepEqual(calls, []);
});

test('autosave failure keeps the edit and exit retries with the latest edited content', async () => {
  const calls = [];
  const h = editorHarness(async () => { throw Error('offline'); });
  h.edit('first change');
  const failed = await h.flush();
  assert.equal(failed.ok, false);
  assert.equal(failed.error.message, 'offline');
  assert.equal(h.dirty.current, true);
  h.edit('latest change');
  h.save = async (content) => calls.push(content);
  assert.equal((await h.flush()).saved, true);
  assert.deepEqual(calls, ['latest change']);
  assert.equal(h.dirty.current, false);
});

test('exit during a failing autosave queues a new save and can succeed on the same click', async () => {
  const first = deferred();
  let attempts = 0;
  const h = editorHarness(async () => {
    attempts += 1;
    if (attempts === 1) await first.promise;
  });
  h.edit('changed');
  const saving = h.flush();
  await tick();
  const exiting = h.flush();
  first.reject(Error('temporary failure'));
  const [autosave, exit] = await Promise.all([saving, exiting]);
  assert.equal(autosave.ok, false);
  assert.deepEqual(exit, { ok: true, saved: true, result: undefined });
  assert.equal(attempts, 2);
});

test('failed exit preserves edits and allows a later retry', async () => {
  let attempts = 0;
  const calls = [];
  const h = editorHarness(async () => { attempts += 1; throw Error('offline'); });
  h.edit('retain this');
  await h.flush();
  assert.equal((await h.flush()).ok, false);
  assert.equal(attempts, 2);
  assert.equal(h.content, 'retain this');
  h.save = async (content) => calls.push(content);
  await h.flush();
  assert.deepEqual(calls, ['retain this']);
});

test('exit saves latest content after an older pending save', async () => {
  const first = deferred();
  const calls = [];
  let attempts = 0;
  const h = editorHarness(async (content) => {
    attempts += 1;
    if (attempts === 1) await first.promise;
    calls.push(content);
  });
  h.edit('older');
  const saving = h.flush();
  await tick();
  h.edit('newer');
  const exiting = h.flush();
  await tick();
  assert.equal(attempts, 1);
  assert.deepEqual(calls, []);
  first.resolve();
  await Promise.all([saving, exiting]);
  assert.deepEqual(calls, ['older', 'newer']);
  await h.flush();
  assert.equal(attempts, 2);
});
