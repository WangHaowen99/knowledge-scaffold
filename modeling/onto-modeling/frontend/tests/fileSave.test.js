import assert from 'node:assert/strict';
import { test } from 'node:test';
import { saveResolved, saveText, withSavedText } from '../src/lib/fileSave.js';

// 合成的文件服务：按修订做乐观并发，与后端 write_file 的 409 语义一致
function fakeFiles(content, revision) {
  const state = { content, revision, writes: [] };
  let next = 0;
  return {
    state,
    async write(text, base) {
      state.writes.push({ text, base });
      if (base && base !== state.revision) throw Object.assign(new Error('conflict'), { status: 409 });
      next += 1;
      state.content = text;
      state.revision = `rev-saved-${next}`;
      return { revision: state.revision };
    },
    async read() {
      return { content: state.content, revision: state.revision };
    },
  };
}

test('正常保存：收尾拿到实际写入的文本与新修订，不是保存前的修订', async () => {
  const files = fakeFiles('基线', 'rev-0');
  const saved = [];
  const outcome = await saveText({
    write: files.write, read: files.read, text: '第一次编辑', baseRevision: 'rev-0', after: (s) => saved.push(s),
  });

  assert.equal(outcome.result.revision, 'rev-saved-1');
  assert.deepEqual(saved, [{ text: '第一次编辑', revision: 'rev-saved-1' }]);

  // 用收尾给的修订连续保存第二次，不会误报冲突
  const again = await saveText({
    write: files.write, read: files.read, text: '第二次编辑', baseRevision: saved[0].revision,
  });
  assert.ok(again.result);
  assert.equal(files.state.content, '第二次编辑');
});

test('冲突后采用对方版本：收尾拿到对方内容和新修订，而不是放弃的旧草稿（#404）', async () => {
  const files = fakeFiles('基线', 'rev-0');
  await files.write('对方先保存的内容', 'rev-0');
  const saved = [];

  const outcome = await saveText({
    write: files.write, read: files.read, text: '旧草稿', baseRevision: 'rev-0', after: (s) => saved.push(s),
  });
  assert.equal(outcome.result, undefined);
  assert.equal(outcome.conflict.mine, '旧草稿');
  assert.equal(outcome.conflict.theirs.content, '对方先保存的内容');
  assert.deepEqual(saved, []);

  const result = await saveResolved(outcome.conflict, outcome.conflict.theirs.content, files.write);

  assert.deepEqual(files.state.writes.at(-1), { text: '对方先保存的内容', base: outcome.conflict.theirs.revision });
  assert.deepEqual(saved, [{ text: '对方先保存的内容', revision: result.revision, resolved: true }]);
});

test('冲突后以合并后的内容保存：收尾拿到合并后的文本', async () => {
  const files = fakeFiles('基线', 'rev-0');
  await files.write('对方先保存的内容', 'rev-0');
  const saved = [];
  const { conflict } = await saveText({
    write: files.write, read: files.read, text: '旧草稿', baseRevision: 'rev-0', after: (s) => saved.push(s),
  });

  await saveResolved(conflict, '在冲突框里合并后的内容', files.write);

  assert.equal(saved[0].text, '在冲突框里合并后的内容');
  assert.equal(files.state.content, '在冲突框里合并后的内容');
});

test('冲突时读不到最新内容不弹冲突框，其他错误照常抛出', async () => {
  const conflicted = await saveText({
    write: async () => { throw Object.assign(new Error('conflict'), { status: 409 }); },
    read: async () => { throw new Error('offline'); },
    text: '草稿',
    after: () => assert.fail('不应收尾'),
  });
  assert.deepEqual(conflicted, { conflict: null });

  await assert.rejects(
    saveText({
      write: async () => { throw Object.assign(new Error('forbidden'), { status: 403 }); },
      read: async () => assert.fail('不应读取'),
      text: '草稿',
      after: () => assert.fail('不应收尾'),
    }),
    /forbidden/,
  );
});

test('文件视图换成保存后的内容与修订，其余字段保留', () => {
  const content = { content: '旧草稿', revision: 'rev-0', truncated: false, size: 9 };
  assert.deepEqual(withSavedText(content, { text: '对方内容', revision: 'rev-2' }), {
    content: '对方内容', revision: 'rev-2', truncated: false, size: 9,
  });
  assert.equal(withSavedText(null, { text: 'x', revision: 'r' }), null);
});
