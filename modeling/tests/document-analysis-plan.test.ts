import assert from 'node:assert/strict';
import test from 'node:test';
import {
  assertCompleteDocumentPlan,
  dynamicChunkTokenBudget,
  estimateTextTokens,
  planDocumentAnalysis,
  uniqueCoveredChars,
} from '../src/workflow/document-analysis-plan.js';

test('动态分段预算随上下文窗口增长并保持安全上下界', () => {
  const small = dynamicChunkTokenBudget({ model: { contextWindow: 32_000, maxTokens: 8_192 } });
  const medium = dynamicChunkTokenBudget({ model: { contextWindow: 128_000, maxTokens: 8_192 } });
  const large = dynamicChunkTokenBudget({ model: { contextWindow: 1_000_000, maxTokens: 1_000_000 } });

  assert.ok(small.targetChunkTokens >= 2_000);
  assert.ok(medium.targetChunkTokens > small.targetChunkTokens);
  assert.equal(large.targetChunkTokens, 16_000);
  assert.ok(large.outputReserveTokens <= 12_288);
});

test('超过 20 万字的章节文档仍被分段完整覆盖且重叠区间不重复计入进度', () => {
  const text = Array.from({ length: 18 }, (_, index) => (
    `# 第${index + 1}章\n${'合成业务说明。'.repeat(1_800)}\n\n`
  )).join('');
  const documents = [{ id: 'doc_demo', filename: 'manual.md', textContent: text, revisionFingerprint: 'rev-demo' }];
  const plan = planDocumentAnalysis(documents, { model: { id: 'demo', contextWindow: 32_000, maxTokens: 4_096 } });

  assert.ok(plan.chunks.length > 1);
  assert.ok(text.length > 200_000);
  assert.doesNotThrow(() => assertCompleteDocumentPlan(plan, documents));
  assert.equal(uniqueCoveredChars(plan.chunks), text.length);
  assert.ok(plan.chunks.slice(1).some((chunk, index) => chunk.startOffset < plan.chunks[index].endOffset));
  assert.ok(plan.chunks.every((chunk) => chunk.contentHash.length === 64));
});

test('Token 估算对中文比 ASCII 更保守', () => {
  assert.equal(estimateTextTokens('中文正文'), 4);
  assert.equal(estimateTextTokens('abcdefghijklmnop'), 4);
});
