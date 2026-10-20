// 推荐表页面编辑（lib/contextPresets.js）：模型名规范化、行操作、重名检查、条目增删与表单转换。
// 跑法：cd modeling/onto-modeling/frontend && npm test
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  VERIFIED_AT_PATTERN,
  entryFormValues,
  entryFromForm,
  isSourceUrl,
  nameOwner,
  normalizeModelName,
  removeEntry,
  rowActions,
  splitLines,
  upsertEntry,
} from '../src/lib/contextPresets.js';

const entry = (id, extra = {}) => ({
  id,
  vendor: '',
  aliases: [],
  max_context_tokens: 200000,
  max_output_tokens: 32000,
  note: '',
  sources: [],
  ...extra,
});

test('模型名规范化与后端 normalize_model_name 一致', () => {
  for (const [raw, expected] of [
    ['  Example-Model-V4.1-Flash ', 'example-model-v4-1-flash'],
    ['provider/example-model-pro', 'example-model-pro'],
    ['example_model_5.1', 'example-model-5-1'],
    ['example-model-pro-991231', 'example-model-pro'], // 网关 6 位日期后缀
    ['example-model-pro-20991231', 'example-model-pro'], // 8 位日期后缀
    ['example-model-0902', 'example-model-0902'], // 4 位快照号不是日期后缀
    ['example-model-2025-04-28', 'example-model-2025-04-28'], // 带横线的日期快照不剥
    ['', ''],
    [undefined, ''],
  ]) {
    assert.equal(normalizeModelName(raw), expected, raw);
  }
});

test('行操作：内置可覆盖，覆盖内置的可编辑与恢复内置，新增的可编辑与删除', () => {
  assert.deepEqual(rowActions({ origin: 'builtin' }), ['override']);
  assert.deepEqual(rowActions({ origin: 'custom', overrides_builtin: true }), ['edit', 'restore']);
  assert.deepEqual(rowActions({ origin: 'custom' }), ['edit', 'delete']);
});

test('重名检查按规范化后的 id 与别名，正在编辑的那条不算', () => {
  const presets = [entry('example-model-pro', { aliases: ['example-pro'] }), entry('example-model-mini')];
  assert.equal(nameOwner('Example_Model.Pro', presets, null).id, 'example-model-pro');
  assert.equal(nameOwner('provider/example-pro', presets, null).id, 'example-model-pro');
  assert.equal(nameOwner('example-model-max', presets, null), null);
  assert.equal(nameOwner('example-pro', presets, 'example-model-pro'), null);
  assert.equal(nameOwner('example-model-mini', presets, 'example-model-pro').id, 'example-model-mini');
});

test('保存条目按规范化 id 原位替换，没有就追加；删除按规范化 id', () => {
  const entries = [entry('example-model-pro'), entry('example-model-mini')];
  const replaced = upsertEntry(entries, entry('Example-Model-Pro', { max_output_tokens: 16000 }));
  assert.deepEqual(
    replaced.map((item) => [item.id, item.max_output_tokens]),
    [
      ['Example-Model-Pro', 16000],
      ['example-model-mini', 32000],
    ],
  );
  assert.deepEqual(
    upsertEntry(entries, entry('example-model-max')).map((item) => item.id),
    ['example-model-pro', 'example-model-mini', 'example-model-max'],
  );
  assert.deepEqual(
    removeEntry(entries, 'example_model_pro').map((item) => item.id),
    ['example-model-mini'],
  );
  // 原数组不被修改
  assert.equal(entries.length, 2);
});

test('条目与表单值互转：来源链接每行一个，去掉首尾空白与空项', () => {
  const original = entry('example-model-pro', {
    vendor: 'Example',
    aliases: ['example-pro'],
    note: '思考模式',
    sources: ['https://example.com/docs/models', 'https://example.com/docs/pricing'],
  });
  const values = entryFormValues(original);
  assert.equal(values.sources, 'https://example.com/docs/models\nhttps://example.com/docs/pricing');
  assert.deepEqual(entryFromForm(values), original);

  assert.deepEqual(
    entryFromForm({
      id: ' example-model-max ',
      aliases: [' example-max ', ' '],
      max_context_tokens: 128000,
      max_output_tokens: 8000,
      sources: '\n https://example.com/a \n\n',
    }),
    entry('example-model-max', {
      aliases: ['example-max'],
      max_context_tokens: 128000,
      max_output_tokens: 8000,
      sources: ['https://example.com/a'],
    }),
  );
  assert.deepEqual(entryFormValues(null), {
    id: '',
    vendor: '',
    aliases: [],
    max_context_tokens: null,
    max_output_tokens: null,
    note: '',
    sources: '',
  });
});

test('来源只接受 https 链接，核对日期须为 年-月-日', () => {
  assert.deepEqual(splitLines(' a \n\nb'), ['a', 'b']);
  assert.deepEqual(splitLines(undefined), []);
  assert.ok(isSourceUrl('https://example.com/docs'));
  assert.ok(!isSourceUrl('见 https://example.com/docs'));
  assert.ok(!isSourceUrl('example.com/docs'));
  assert.ok(!isSourceUrl('官方文档'));
  assert.ok(VERIFIED_AT_PATTERN.test('2026-10-15'));
  assert.ok(!VERIFIED_AT_PATTERN.test('2026/10/15'));
  assert.ok(!VERIFIED_AT_PATTERN.test('2026-10-15 12:00'));
});
