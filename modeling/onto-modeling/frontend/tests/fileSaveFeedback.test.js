import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { ApiError, errorText } from '../src/api/errors.js';
import { fileSaveFeedback, fileSaveRejection } from '../src/api/fileSaveFeedback.js';

const SAVED = '已保存并记录版本 v0.2';

test('保存告警包含位置且不会显示无告警成功提示', () => {
  const feedback = fileSaveFeedback({ warnings: [{ path: 'ontology/scripts/script_demo.py', line: 8, message: '存在 TODO 未完成标记' }] }, SAVED);
  assert.equal(feedback.icon, 'alert');
  assert.equal(feedback.text, `${SAVED}，有 1 条告警`);
  assert.equal(feedback.note.tone, 'amber');
  assert.match(feedback.note.lines[0], /script_demo.py 第 8 行.*TODO/);
});

test('兼容旧返回且限制单次展示的告警数量', () => {
  assert.deepEqual(fileSaveFeedback({}, SAVED), { text: SAVED, icon: 'check', note: null });
  assert.deepEqual(fileSaveFeedback({ warnings: [] }, SAVED), { text: SAVED, icon: 'check', note: null });
  const { note } = fileSaveFeedback({ warnings: Array.from({ length: 8 }, (_, i) => ({ path: 'script_demo.py', line: i + 1, message: 'TODO' })) }, SAVED);
  assert.equal(note.lines.length, 6);
  assert.equal(note.lines.at(-1), '另有 3 条告警');
  assert.doesNotMatch(note.lines.join('\n'), /第 6 行/);
});

test('脚本检查没过（422）写出具体错误，其他错误不归这里', () => {
  const rejected = new ApiError({
    status: 422,
    code: 'check_rejected',
    message: 'ontology/scripts/script_demo.py：脚本语法错误',
    details: { errors: [{ code: 'script_contract', level: 'error', path: 'ontology/scripts/script_demo.py', line: 3, column: 5, message: '脚本语法错误：无效语法（第 3 行，第 5 列）' }], warnings: [] },
  });
  const note = fileSaveRejection(rejected);
  assert.equal(note.tone, 'red');
  assert.deepEqual(note.lines, ['ontology/scripts/script_demo.py：脚本语法错误：无效语法（第 3 行，第 5 列）']);
  assert.equal(errorText(rejected), '检查发现硬伤，没有保存');
  // 没有明细时用服务端的说明
  assert.deepEqual(fileSaveRejection(new ApiError({ status: 422, code: 'check_rejected', message: '合成说明' })).lines, ['合成说明']);
  assert.equal(fileSaveRejection(new ApiError({ status: 409, code: 'revision_conflict' })), null);
  assert.equal(fileSaveRejection(new TypeError('网络断开')), null);
});

test('原文编辑器和版本对比的保存都接了告警与错误清单', () => {
  for (const path of ['../src/pages/studio/RawEditor.jsx', '../src/pages/versions/VersionsPage.jsx']) {
    const source = readFileSync(new URL(path, import.meta.url), 'utf8');
    assert.match(source, /fileSaveFeedback\(r, /);
    assert.match(source, /fileSaveRejection\(e\)/);
    assert.match(source, /<SaveNote note=\{saveNote\}/);
  }
});
