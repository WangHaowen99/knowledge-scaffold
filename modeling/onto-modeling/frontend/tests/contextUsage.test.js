// 上下文用量（lib/contextUsage.js）：引擎事件 → 已用/窗口/压缩点，配置页阈值估算。
// 跑法：cd modeling/onto-modeling/frontend && npm test
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  EMPTY_CONTEXT,
  compactionNotice,
  describeContext,
  estimateCompaction,
  formatTokens,
  nextContext,
} from '../src/lib/contextUsage.js';

const replay = (events) => events.reduce(nextContext, EMPTY_CONTEXT);

const usage = (used, window = 1000000, compactAt = 967000) => ({
  type: 'context_usage',
  used,
  window,
  compact_at: compactAt,
});

test('已用量跟随主代理调用，子代理与结果累加不影响', () => {
  const ctx = replay([
    usage(40000),
    { type: 'assistant_message', subagent: false, context_tokens: 52000 },
    { type: 'assistant_message', subagent: true, context_tokens: 900000 },
    { type: 'result', usage: { input_tokens: 3000000 } },
  ]);

  assert.deepEqual(ctx, { used: 52000, window: 1000000, compactAt: 967000 });
});

test('窗口未知的用量事件沿用上一轮窗口', () => {
  const ctx = replay([usage(40000), usage(60000, null, null)]);

  assert.deepEqual(ctx, { used: 60000, window: 1000000, compactAt: 967000 });
});

test('压缩后已用量取压缩结果，未给出则置空', () => {
  assert.equal(replay([usage(150000), { type: 'context_compacted', post_tokens: 9000 }]).used, 9000);
  assert.equal(replay([usage(150000), { type: 'context_compacted' }]).used, null);
});

test('未续接的新会话清空用量，续接保留', () => {
  assert.deepEqual(replay([usage(40000), { type: 'session_started', resumed: false }]), EMPTY_CONTEXT);
  assert.equal(replay([usage(40000), { type: 'session_started', resumed: true }]).used, 40000);
});

test('提示给出已用、窗口、剩余与距压缩点的余量', () => {
  const info = describeContext({ used: 500000, window: 1000000, compactAt: 967000 });

  assert.equal(info.pct, 50);
  assert.equal(info.level, '');
  assert.equal(info.text, '上下文已用 500k / 1000k（50%），剩余 500k；再用约 467k 自动压缩');
});

test('接近或越过压缩点时升级告警', () => {
  assert.equal(describeContext({ used: 750000, window: 1000000, compactAt: 967000 }).level, 'warn');
  const past = describeContext({ used: 980000, window: 1000000, compactAt: 967000 });
  assert.equal(past.level, 'danger');
  assert.match(past.text, /已到自动压缩点/);
});

test('没有压缩点时按窗口占比告警，窗口未知不显示', () => {
  const info = describeContext({ used: 850000, window: 1000000, compactAt: null });
  assert.equal(info.level, 'warn');
  assert.doesNotMatch(info.text, /压缩/);
  assert.equal(describeContext({ used: 850000, window: null, compactAt: null }), null);
  assert.equal(describeContext(EMPTY_CONTEXT), null);
});

test('压缩提示区分自动与手动', () => {
  assert.equal(
    compactionNotice({ trigger: 'auto', pre_tokens: 160000 }),
    '上下文已自动压缩（压缩前约 160k tokens）',
  );
  assert.equal(compactionNotice({ trigger: 'manual' }), '上下文已压缩');
  assert.equal(formatTokens(950), '950');
});

test('估算按 CLI 规则预留输出与 13000 缓冲', () => {
  // 只填真实窗口：输出预留按 20000
  assert.deepEqual(estimateCompaction({ max_context_tokens: 1000000 }), { threshold: 967000 });
  assert.equal(
    estimateCompaction({ max_context_tokens: 1000000, max_output_tokens: 8000 }).threshold,
    979000,
  );
  const result = estimateCompaction({
    max_context_tokens: 1000000,
    auto_compact_window: 800000,
    max_output_tokens: 16000,
  });
  assert.equal(result.threshold, 771000);
  assert.equal(result.margin, 213000);
});

test('触发百分比按有效窗口计算且只能提前', () => {
  const values = { max_context_tokens: 1000000, max_output_tokens: 16000 };
  assert.equal(estimateCompaction({ ...values, autocompact_pct: 60 }).threshold, 590400);
  assert.equal(estimateCompaction({ ...values, autocompact_pct: 99 }).threshold, 971000);
  assert.equal(estimateCompaction({}), null);
});

test('未填真实窗口时按服务端默认窗口估算，填写后以填写值为准', () => {
  assert.deepEqual(estimateCompaction({}, 1000000), { threshold: 967000 });
  assert.equal(estimateCompaction(undefined, 1000000).threshold, 967000);
  assert.equal(
    estimateCompaction({ max_context_tokens: 128000, max_output_tokens: 16000 }, 1000000).threshold,
    99000,
  );
});
