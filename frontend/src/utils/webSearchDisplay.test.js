import assert from 'node:assert/strict';
import test from 'node:test';

import { buildWebSearchProcessModel } from './webSearchDisplay.js';

test('buildWebSearchProcessModel extracts query, timeline and result sources', () => {
  const model = buildWebSearchProcessModel({
    segment: {
      type: 'tool',
      finished: true,
      state: 'completed',
      startTs: 1000,
      endTs: 2400,
      meta: {
        tool_name: 'web_search',
        input: { query: '示例企业总部地址' },
        web_search_events: [
          { type: 'start', content: '开始联网搜索', ts: 1100 },
          { type: 'final', content: '联网搜索完成，返回 2 条结果', ts: 2300 },
        ],
        output_preview: JSON.stringify({
          ok: true,
          provider: 'tavily',
          query: '示例企业总部地址',
          response_time: 0.42,
          results: [
            {
              rank: 1,
              title: 'ExampleBrand Headquarters',
              url: 'https://example.com/example-brand',
              domain: 'example.com',
              snippet: 'ExampleBrand headquarters address summary.',
            },
            {
              rank: 2,
              title: 'ExampleBrand Shenzhen',
              url: 'https://example.cn/example-brand',
              domain: 'example.cn',
              snippet: 'Another source.',
            },
          ],
        }),
        elapsed_ms: 1400,
      },
    },
    messageStatus: 'completed',
    nowTs: 3000,
  });

  assert.equal(model.query, '示例企业总部地址');
  assert.equal(model.provider, 'tavily');
  assert.equal(model.resultCount, 2);
  assert.equal(model.elapsedMs, 1400);
  assert.deepEqual(model.timeline.map((event) => event.content), [
    '开始联网搜索',
    '联网搜索完成，返回 2 条结果',
  ]);
  assert.deepEqual(model.sources.map((source) => source.domain), [
    'example.com',
    'example.cn',
  ]);
});

test('buildWebSearchProcessModel returns null for non web search tools', () => {
  assert.equal(
    buildWebSearchProcessModel({
      segment: {
        type: 'tool',
        meta: { tool_name: 'business_data_search' },
      },
    }),
    null,
  );
});

test('buildWebSearchProcessModel prefers parsed provider error over raw tool error JSON', () => {
  const rawErrorPayload = JSON.stringify({
    ok: false,
    status: 'error',
    error: '联网搜索 API Key 未配置',
  });

  const model = buildWebSearchProcessModel({
    segment: {
      type: 'tool',
      finished: true,
      state: 'error',
      meta: {
        tool_name: 'web_search',
        input: { query: '测试' },
        output_preview: rawErrorPayload,
        error: rawErrorPayload,
      },
    },
  });

  assert.equal(model.errorText, '联网搜索 API Key 未配置');
  assert.equal(model.isFailed, true);
});
