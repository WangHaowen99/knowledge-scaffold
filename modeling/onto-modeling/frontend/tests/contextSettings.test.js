// 上下文配置页辅助（lib/contextSettings.js）：提交体、留空生效值、推荐表预填与提示、自动压缩估算。
// 跑法：cd modeling/onto-modeling/frontend && npm test
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  PRESET_MISSED,
  PRESET_MISS_TEXT,
  contextPayload,
  defaultValuesOf,
  estimateCompaction,
  estimateJiuwenCompaction,
  filledHint,
  jiuwenTriggerRatio,
  presetChange,
  presetSuggestion,
  restoredHint,
} from '../src/lib/contextSettings.js';

const preset = {
  id: 'example-model',
  origin: 'builtin',
  aliases: [],
  max_context_tokens: 262144,
  max_output_tokens: 32768,
};

// 与服务端内置默认一致：窗口 200000、最大输出 32000，压缩参数未配
const DEFAULTS = {
  max_context_tokens: 200000,
  max_output_tokens: 32000,
  auto_compact_window: null,
  autocompact_pct: null,
};

test('提交体只有窗口与最大输出两项，留空与缺失都提交 null', () => {
  assert.deepEqual(contextPayload({ max_context_tokens: 128000, autocompact_pct: 80, extra: 1 }), {
    max_context_tokens: 128000,
    max_output_tokens: null,
  });
  assert.deepEqual(contextPayload(undefined), { max_context_tokens: null, max_output_tokens: null });
});

test('留空生效值取服务端 context_defaults 的 value', () => {
  assert.deepEqual(
    defaultValuesOf({
      max_context_tokens: { value: 200000, source: 'default', env: 'ONTO_WEB_CONTEXT_WINDOW' },
      autocompact_pct: { value: null, source: null, env: 'ONTO_WEB_CONTEXT_COMPACT_PCT' },
    }),
    { max_context_tokens: 200000, autocompact_pct: null },
  );
  assert.deepEqual(defaultValuesOf(undefined), {});
});

test('命中新模型时填入窗口与最大输出，并记下匹配的条目', () => {
  const change = presetChange(preset, null);
  assert.equal(change.matchedId, 'example-model');
  assert.deepEqual(change.fill, { max_context_tokens: 262144, max_output_tokens: 32768 });
  assert.deepEqual(change.hint, filledHint(preset));
  assert.equal(change.hint.type, 'success');
  assert.match(change.hint.text, /^已识别 example-model，已填入上下文窗口 262,144，最大输出 32,768/);
});

test('命中自定义表的条目时在提示中注明', () => {
  assert.match(filledHint({ ...preset, origin: 'custom' }).text, /已识别 example-model（自定义表）/);
});

test('匹配的条目没变时不重填，保留用户手改的值', () => {
  assert.equal(presetChange(preset, 'example-model'), null);
});

test('未命中时清空两项；已清空过的继续未命中不再清空，保留随后手填的值', () => {
  const change = presetChange(null, 'example-model');
  assert.deepEqual(change, {
    matchedId: PRESET_MISSED,
    fill: { max_context_tokens: null, max_output_tokens: null },
    hint: { type: 'warning', text: PRESET_MISS_TEXT },
  });
  assert.equal(presetChange(null, PRESET_MISSED), null);
  // 未命中之后再命中，算作换了模型，重新填
  assert.ok(presetChange(preset, PRESET_MISSED).fill);
});

test('打开页面时只在生效值与推荐值不同时提示，且不自动填', () => {
  const hint = presetSuggestion(preset, { max_context_tokens: null, max_output_tokens: null }, DEFAULTS);
  assert.equal(hint.preset, preset);
  assert.equal(hint.text, '推荐表中 example-model：上下文窗口 262,144，最大输出 32,768');

  const saved = { max_context_tokens: 262144, max_output_tokens: 32768 };
  assert.equal(presetSuggestion(preset, saved, DEFAULTS), null);
  // 留空项按 .env / 内置默认的生效值比较
  assert.equal(presetSuggestion(preset, {}, saved), null);
  assert.equal(presetSuggestion(null, saved, DEFAULTS), null);
});

test('恢复已保存的值时说明来源：与推荐表一致、不同或表中没有该模型', () => {
  const saved = { max_context_tokens: 262144, max_output_tokens: 32768 };
  assert.deepEqual(restoredHint('', null, saved, DEFAULTS), {
    type: 'success',
    text: '已恢复为已保存的上下文参数',
  });
  assert.deepEqual(restoredHint('example-model', preset, saved, DEFAULTS), {
    type: 'success',
    text: '已恢复为该模型已保存的上下文参数，与推荐表一致',
  });
  assert.deepEqual(restoredHint('example-model', null, saved, DEFAULTS), {
    type: 'success',
    text: '已恢复为该模型已保存的上下文参数（推荐表中没有该模型）',
  });
  // 推荐值与已保存的不同：一并给出推荐值，可点「填入推荐值」
  const hint = restoredHint('example-model', preset, { max_context_tokens: 128000 }, DEFAULTS);
  assert.equal(hint.type, 'success');
  assert.equal(hint.preset, preset);
  assert.equal(
    hint.text,
    '已恢复为该模型已保存的上下文参数；推荐表中 example-model：上下文窗口 262,144，最大输出 32,768',
  );
});

test('两项都留空时不估算', () => {
  assert.equal(estimateCompaction({}, DEFAULTS), null);
  assert.equal(estimateCompaction(undefined, DEFAULTS), null);
});

test('按内置默认推算压缩窗口 180000，触发点留足 10% 余量', () => {
  assert.deepEqual(estimateCompaction({ max_context_tokens: 200000 }, DEFAULTS), {
    compactAt: 147000,
    percent: 74,
    margin: 21000,
  });
});

test('推算的压缩窗口低于 CLI 下限时按触发百分比估算', () => {
  // 推算出触发百分比 45%：有效窗口 108000 × 45%
  assert.deepEqual(estimateCompaction({ max_context_tokens: 128000, max_output_tokens: 65536 }, DEFAULTS), {
    compactAt: 48600,
    percent: 38,
    margin: 13864,
  });
});

test('.env 配了压缩参数时按 .env 估算，不再推算', () => {
  const defaults = { ...DEFAULTS, auto_compact_window: 150000 };
  assert.equal(estimateCompaction({ max_context_tokens: 200000 }, defaults).compactAt, 117000);
});

test('最大输出未知时按 CLI 预留 20000 估算，不给余量', () => {
  const defaults = { ...DEFAULTS, max_output_tokens: null };
  assert.deepEqual(estimateCompaction({ max_context_tokens: 200000 }, defaults), {
    compactAt: 167000,
    percent: 84,
    margin: null,
  });
});

test('保存不了或算不出压缩点时返回错误', () => {
  assert.deepEqual(estimateCompaction({ max_context_tokens: 32000, max_output_tokens: 32000 }, DEFAULTS), {
    error: '最大输出必须小于上下文窗口',
  });
  // 最大输出留空时按 .env / 内置默认生效，同样须小于窗口
  assert.match(estimateCompaction({ max_context_tokens: 32000 }, DEFAULTS).error, /^最大输出留空时按 .+ 生效/);
  assert.deepEqual(estimateCompaction({ max_context_tokens: 10000, max_output_tokens: 1000 }, DEFAULTS), {
    error: '上下文窗口过小，留不出回答和自动压缩所需的空间',
  });
});

// 内测引擎留空时的生效值，示例取最大输出 8192 的模型
const JIUWEN_DEFAULTS = { max_context_tokens: 200000, max_output_tokens: 8192 };

test('内测引擎的触发比例与后端一致：留 10% 余量，限定在 0.3–0.8，向下取到 0.01', () => {
  assert.equal(jiuwenTriggerRatio(200000, 8192), 0.8);
  assert.equal(jiuwenTriggerRatio(128000, 16384), 0.77);
  assert.equal(jiuwenTriggerRatio(32000, 16000), 0.4);
  assert.equal(jiuwenTriggerRatio(32000, 24000), 0.3);
});

test('内测引擎按窗口 × 触发比例估算压缩点', () => {
  assert.equal(estimateJiuwenCompaction({}, JIUWEN_DEFAULTS), null);
  assert.deepEqual(estimateJiuwenCompaction({ max_context_tokens: 128000, max_output_tokens: 16384 }, JIUWEN_DEFAULTS), {
    compactAt: 98560,
    percent: 77,
    margin: 13056,
  });
  // 最大输出大到留不出余量：比例取下限 0.3，余量为负由配置页提示调小最大输出
  assert.deepEqual(estimateJiuwenCompaction({ max_context_tokens: 32000, max_output_tokens: 24000 }, JIUWEN_DEFAULTS), {
    compactAt: 9600,
    percent: 30,
    margin: -1600,
  });
  assert.match(estimateJiuwenCompaction({ max_context_tokens: 8000 }, JIUWEN_DEFAULTS).error, /^最大输出留空时按 8,192 生效/);
});
