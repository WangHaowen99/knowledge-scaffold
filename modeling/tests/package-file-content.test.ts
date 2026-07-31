import assert from 'node:assert/strict';
import { test } from 'node:test';

import { createLatestRequestGate } from '../frontend/src/packageFileContent.js';

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((next) => { resolve = next; });
  return { promise, resolve };
}

test('文件请求乱序返回时只接受当前选中文件的响应', async () => {
  const gate = createLatestRequestGate();
  const index = deferred<{ content: string }>();
  const consume = deferred<{ content: string }>();
  const committed: string[] = [];

  const indexCurrent = gate.begin();
  const indexLoad = index.promise.then((file) => {
    if (indexCurrent()) committed.push(file.content);
  });
  const consumeCurrent = gate.begin();
  const consumeLoad = consume.promise.then((file) => {
    if (consumeCurrent()) committed.push(file.content);
  });

  consume.resolve({ content: 'Consume 正文' });
  await consumeLoad;
  index.resolve({ content: '首页正文' });
  await indexLoad;

  assert.deepEqual(committed, ['Consume 正文']);
});

test('文件面板卸载后不再接受未完成请求', () => {
  const gate = createLatestRequestGate();
  const isCurrent = gate.begin();
  gate.invalidate();
  assert.equal(isCurrent(), false);
});
