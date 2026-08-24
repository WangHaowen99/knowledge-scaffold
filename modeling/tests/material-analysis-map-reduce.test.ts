import assert from 'node:assert/strict';
import test from 'node:test';
import { createFauxCore, fauxAssistantMessage, fauxToolCall } from '@earendil-works/pi-ai/providers/faux';
import type { LlmDeps } from '../src/llm/provider.js';
import { createProject } from '../src/projects/repo.js';
import { runMaterialAnalysisMapReduce } from '../src/workflow/material-analysis-map-reduce.js';

function observedLlm(input: {
  failOnceFor?: string;
  omitChunkSubmitOnceFor?: string;
  omitChunkSubmitAlwaysFor?: string;
  malformedChunkOnceFor?: string;
  documentSuitableFor?: Array<'object' | 'rule' | 'operation' | 'metric'>;
  delayMs?: number;
}) {
  const calls = new Map<string, number>();
  let active = 0;
  let peak = 0;
  let failed = false;
  let reductions = 0;
  const llm: LlmDeps = {
    model: createFauxCore({ provider: 'synthetic-map', models: [{ id: 'synthetic-map' }] }).getModel(),
    getApiKey: () => 'faux',
    streamFn: ((model: unknown, context: any, options: unknown) => {
      const system = String(context.systemPrompt ?? '');
      const filename = (system.match(/文档：([^\n]+)/u) ?? [])[1]?.trim() ?? '';
      const isChunk = system.includes('submit_document_chunk_analysis');
      const core = createFauxCore({ provider: 'synthetic-map', models: [{ id: 'synthetic-map' }] });
      core.setResponses([async () => {
        if (isChunk) {
          calls.set(filename, (calls.get(filename) ?? 0) + 1);
          active += 1;
          peak = Math.max(peak, active);
          await new Promise((resolve) => setTimeout(resolve, input.delayMs ?? 20));
        }
        try {
          if (!failed && filename === input.failOnceFor) {
            failed = true;
            throw new Error('synthetic model failure');
          }
          if (system.includes('submit_document_chunk_analysis')) {
            if (filename === input.omitChunkSubmitAlwaysFor
              || (filename === input.omitChunkSubmitOnceFor && (calls.get(filename) ?? 0) <= 2)) {
              return fauxAssistantMessage('已分析当前分段，但本轮没有调用结构化提交工具。');
            }
            if (filename === input.malformedChunkOnceFor && (calls.get(filename) ?? 0) === 1) {
              const offset = Number((system.match(/原文区间：\[(\d+)/u) ?? [])[1] ?? 0);
              return fauxAssistantMessage([fauxToolCall('submit_document_chunk_analysis', {
                summary: '合成分段摘要',
                candidates: [{ kind: 'fact', rationale: '原文事实', evidenceOffsets: [offset] }],
              })]);
            }
            const offset = Number((system.match(/原文区间：\[(\d+)/u) ?? [])[1] ?? 0);
            return fauxAssistantMessage([fauxToolCall('submit_document_chunk_analysis', {
              summary: '合成分段摘要', evidence: [{ offset, kind: 'rule', note: '合成证据' }],
              candidates: [{
                kind: 'rule', name: `${filename} 规则`, rationale: '合成规则',
                evidenceOffsets: [offset], readiness: 'extractable',
              }], openQuestions: [],
            })]);
          }
          if (system.includes('submit_document_analysis')) {
            reductions += 1;
            return fauxAssistantMessage([fauxToolCall('submit_document_analysis', {
              summary: `${filename} 合成全文摘要`, suitableFor: input.documentSuitableFor ?? ['rule'], rationale: '合成全文规则',
              candidates: [{
                kind: 'rule', name: `${filename} 规则`, rationale: '合成规则',
                evidenceOffsets: [], readiness: 'extractable',
              }], openQuestions: [],
            })]);
          }
          return fauxAssistantMessage([fauxToolCall('submit_material_analysis_result', {
            domainOverview: '合成领域汇总', candidateObjects: [], candidateGoals: [], candidateProcesses: [],
            openQuestions: [], tableMaterialAssessments: [], tableCandidateArtifacts: [],
          })]);
        } finally {
          if (isChunk) active -= 1;
        }
      }]);
      return core.streamSimple(model as any, context, options as any);
    }) as LlmDeps['streamFn'],
  };
  return { llm, calls, peak: () => peak, reductions: () => reductions };
}

function documents(names: string[]) {
  return names.map((name) => ({
    id: `doc_${name}`,
    filename: `${name}.md`,
    revisionFingerprint: `${name}:v1`,
    textContent: `${name} 的合成业务条件。`.repeat(200),
  }));
}

test('资料分段 Map 按有限并发执行而非串行', async () => {
  const project = createProject({ owner: 'tester', name: '合成并发分析' });
  const observed = observedLlm({ delayMs: 40 });
  await runMaterialAnalysisMapReduce({
    projectId: project.id,
    projectContext: '合成场景',
    tables: [],
    documents: documents(['a', 'b', 'c', 'd']),
    llm: observed.llm,
    concurrency: 3,
  });
  assert.ok(observed.peak() >= 2, `峰值并发应大于 1，实际 ${observed.peak()}`);
  assert.ok(observed.peak() <= 3, `峰值并发不得超过配置，实际 ${observed.peak()}`);
});

test('模型失败后重跑只续跑失败分段，不重新分析已完成分段', async () => {
  const project = createProject({ owner: 'tester', name: '合成断点续跑' });
  const observed = observedLlm({ failOnceFor: 'b.md' });
  const input = {
    projectId: project.id,
    projectContext: '合成场景',
    tables: [],
    documents: documents(['a', 'b']),
    llm: observed.llm,
    concurrency: 1,
  };
  await assert.rejects(runMaterialAnalysisMapReduce(input), /synthetic model failure/);
  await runMaterialAnalysisMapReduce(input);
  assert.equal(observed.calls.get('a.md'), 1);
  assert.equal(observed.calls.get('b.md'), 2);
});

test('模型未调用分段提交工具时只重试当前分段', async () => {
  const project = createProject({ owner: 'tester', name: '合成分段协议重试' });
  const observed = observedLlm({ omitChunkSubmitOnceFor: 'b.md' });
  await runMaterialAnalysisMapReduce({
    projectId: project.id,
    projectContext: '合成场景',
    tables: [],
    documents: documents(['a', 'b']),
    llm: observed.llm,
    concurrency: 2,
  });
  assert.equal(observed.calls.get('a.md'), 1);
  assert.equal(observed.calls.get('b.md'), 3);
});

test('模型持续不调用分段工具时保留公开摘要并继续 Reduce', async () => {
  const project = createProject({ owner: 'tester', name: '合成分段摘要降级' });
  const observed = observedLlm({ omitChunkSubmitAlwaysFor: 'b.md' });
  const result = await runMaterialAnalysisMapReduce({
    projectId: project.id,
    projectContext: '合成场景',
    tables: [],
    documents: documents(['a', 'b']),
    llm: observed.llm,
    concurrency: 2,
  });
  assert.equal(result.analysis.documentReadCoverage?.every((item) => item.complete), true);
  assert.equal(observed.calls.get('a.md'), 1);
  assert.ok((observed.calls.get('b.md') ?? 0) >= 8, '应完成多轮协议恢复后再使用公开摘要降级');
});

test('领域汇总缺少辅助表时补齐已分析记录且不伪造知识候选', async () => {
  const project = createProject({ owner: 'tester', name: '合成表格覆盖归一化' });
  const observed = observedLlm({});
  const result = await runMaterialAnalysisMapReduce({
    projectId: project.id,
    projectContext: '合成场景',
    tables: [
      { name: 'main_table', rowCount: 1, fields: [] },
      { name: 'aux_table', rowCount: 1, fields: [] },
    ],
    documents: documents(['a']),
    llm: observed.llm,
    concurrency: 2,
  });
  const auxiliary = result.analysis.materialAssessments?.find((item) => item.source === 'aux_table');
  assert.deepEqual(auxiliary?.suitableFor, []);
  assert.match(auxiliary?.rationale ?? '', /已分析/);
  assert.equal(result.analysis.candidateArtifacts?.some((item) => item.sources.includes('aux_table')), false);
});

test('文档知识类型声明只保留实际存在候选的类型', async () => {
  const project = createProject({ owner: 'tester', name: '合成文档候选覆盖归一化' });
  const observed = observedLlm({ documentSuitableFor: ['rule', 'operation'] });
  const result = await runMaterialAnalysisMapReduce({
    projectId: project.id,
    projectContext: '合成场景',
    tables: [],
    documents: documents(['a']),
    llm: observed.llm,
  });
  const assessment = result.analysis.materialAssessments?.find((item) => item.source === 'a.md');
  assert.deepEqual(assessment?.suitableFor, ['rule']);
});

test('结构化提交字段不完整或候选误标 fact 时在本地归一化，不重跑全文分段', async () => {
  const project = createProject({ owner: 'tester', name: '合成结构化归一化' });
  const observed = observedLlm({ malformedChunkOnceFor: 'b.md' });
  await runMaterialAnalysisMapReduce({
    projectId: project.id,
    projectContext: '合成场景',
    tables: [],
    documents: documents(['a', 'b']),
    llm: observed.llm,
    concurrency: 2,
  });
  assert.equal(observed.calls.get('b.md'), 1);
});

test('未显式配置时默认使用五路有限并发', async () => {
  const project = createProject({ owner: 'tester', name: '合成默认并发' });
  const observed = observedLlm({ delayMs: 40 });
  await runMaterialAnalysisMapReduce({
    projectId: project.id,
    projectContext: '合成场景',
    tables: [],
    documents: documents(['a', 'b', 'c', 'd', 'e', 'f']),
    llm: observed.llm,
  });
  assert.ok(observed.peak() >= 2, `默认并发应大于 1，实际 ${observed.peak()}`);
  assert.ok(observed.peak() <= 5, `默认并发不得超过 5，实际 ${observed.peak()}`);
});

test('超长文档使用多层 Reduce 汇总而不把全部分段一次塞入模型', async () => {
  const project = createProject({ owner: 'tester', name: '合成分层汇总' });
  const observed = observedLlm({ delayMs: 1 });
  observed.llm.model = { ...observed.llm.model, contextWindow: 8_192, maxTokens: 1_024 };
  const textContent = Array.from({ length: 30 }, (_, index) => (
    `# 第 ${index + 1} 章\n${'完整业务条件。'.repeat(1_200)}\n`
  )).join('');
  assert.ok(textContent.length > 200_000);

  const result = await runMaterialAnalysisMapReduce({
    projectId: project.id,
    projectContext: '合成超长文档',
    tables: [],
    documents: [{ id: 'doc-long', filename: '超长规程.md', textContent, revisionFingerprint: 'long:v1' }],
    llm: observed.llm,
    concurrency: 5,
    documentOnly: true,
  });

  assert.ok(observed.reductions() > 1, `应执行多层文档汇总，实际 ${observed.reductions()} 次`);
  assert.equal(result.analysis.documentReadCoverage?.[0].complete, true);
  assert.equal(result.analysis.documentReadCoverage?.[0].contiguousEnd, textContent.length);
});
