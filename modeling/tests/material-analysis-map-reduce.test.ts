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
  /** 这些文档的分段会逐项提交产品事实，模拟产品手册类原文。 */
  productChunksFor?: string[];
  /** 这些文档声明已提取产品事实但不提交逐项数据，用来验证捕获契约不被绕过。 */
  emptyInventoryClaimFor?: string;
  /** 这些文档首轮提交角色混用的产品事实，用来验证被拒后重试能拿到纠错提示。 */
  rejectInventoryOnceFor?: string;
  /** 分段产品事实里故意留空的来源字段，用来验证缺口由平台反推。 */
  missingFactField?: 'applicableSubjects' | 'triggerScenarios' | 'businessNeeds' | 'admissionConditions' | 'exclusionConditions';
}) {
  const calls = new Map<string, number>();
  let active = 0;
  let peak = 0;
  let failed = false;
  let reductions = 0;
  const productItem = (filename: string, offset: number) => {
    const empty = input.missingFactField;
    const field = (name: string, values: string[]) => (empty === name ? [] : values);
    return {
      canonicalItemId: `product:${filename}:${offset}`,
      name: `${filename} 合成产品 ${offset}`,
      ruleRefs: [`rule:${filename}`],
      sourceRecordIds: [`source:${filename}:${offset}`],
      sourceLocator: `${filename}#${offset}`,
      evidenceStatus: 'complete',
      applicableSubjects: field('applicableSubjects', ['合成主体']),
      triggerScenarios: field('triggerScenarios', ['合成场景']),
      businessNeeds: field('businessNeeds', ['合成需求']),
      admissionConditions: field('admissionConditions', ['合成准入条件']),
      exclusionConditions: field('exclusionConditions', ['合成排除条件']),
      structuredGaps: [],
    };
  };
  const chunkPrompts: string[] = [];
  const llm: LlmDeps = {
    model: createFauxCore({ provider: 'synthetic-map', models: [{ id: 'synthetic-map' }] }).getModel(),
    getApiKey: () => 'faux',
    streamFn: ((model: unknown, context: any, options: unknown) => {
      const system = String(context.systemPrompt ?? '');
      if (system.includes('submit_document_chunk_analysis')) {
        chunkPrompts.push(JSON.stringify(context.messages ?? []));
      }
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
                productInventoryDisposition: 'not_applicable',
              })]);
            }
            const offset = Number((system.match(/原文区间：\[(\d+)/u) ?? [])[1] ?? 0);
            if (filename === input.rejectInventoryOnceFor && (calls.get(filename) ?? 0) === 1) {
              // 角色混用：把 canonicalItemId 塞进 sourceRecordIds，会被捕获契约在 execute 里拒绝。
              const broken = productItem(filename, offset);
              return fauxAssistantMessage([fauxToolCall('submit_document_chunk_analysis', {
                summary: '合成分段摘要', evidence: [{ offset, kind: 'rule', note: '合成证据' }],
                candidates: [], openQuestions: [],
                productInventoryDisposition: 'extracted',
                productInventory: [{ ...broken, sourceRecordIds: [broken.canonicalItemId] }],
              })]);
            }
            const hasProducts = (input.productChunksFor ?? []).includes(filename);
            return fauxAssistantMessage([fauxToolCall('submit_document_chunk_analysis', {
              summary: '合成分段摘要', evidence: [{ offset, kind: 'rule', note: '合成证据' }],
              candidates: [{
                kind: 'rule', name: `${filename} 规则`, rationale: '合成规则',
                evidenceOffsets: [offset], readiness: 'extractable',
              }], openQuestions: [],
              ...(filename === input.emptyInventoryClaimFor
                ? { productInventoryDisposition: 'extracted' }
                : hasProducts
                  ? {
                    productInventoryDisposition: 'extracted',
                    productInventory: [productItem(filename, offset)],
                  }
                  : { productInventoryDisposition: 'not_applicable' }),
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
  return { llm, calls, chunkPrompts, peak: () => peak, reductions: () => reductions };
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

test('相同资料和模型的并发全文分析共享同一执行', async () => {
  const project = createProject({ owner: 'tester', name: '合成并发去重' });
  const observed = observedLlm({ delayMs: 40 });
  const input = {
    projectId: project.id,
    projectContext: '合成场景',
    tables: [],
    documents: documents(['singleflight']),
    llm: observed.llm,
    concurrency: 2,
    documentOnly: true,
  };
  const [first, second] = await Promise.all([
    runMaterialAnalysisMapReduce(input),
    runMaterialAnalysisMapReduce(input),
  ]);
  assert.equal(first.runId, second.runId);
  assert.equal(observed.calls.get('singleflight.md'), 1);
});

test('来源集合不同的并发全文分析共享重叠文档的在途分块和汇总', async () => {
  const project = createProject({ owner: 'tester', name: '合成跨范围并发复用' });
  const observed = observedLlm({ delayMs: 80 });
  const shared = documents(['shared'])[0];
  const first = runMaterialAnalysisMapReduce({
    projectId: project.id,
    projectContext: '完整资料范围',
    tables: [],
    documents: [shared, ...documents(['extra'])],
    llm: observed.llm,
    concurrency: 2,
    documentOnly: true,
  });
  const second = runMaterialAnalysisMapReduce({
    projectId: project.id,
    projectContext: '单文档精读',
    tables: [],
    documents: [shared],
    llm: observed.llm,
    concurrency: 2,
    documentOnly: true,
  });
  const [firstResult, secondResult] = await Promise.all([first, second]);
  assert.notEqual(firstResult.runId, secondResult.runId, '不同来源集合仍应保留独立运行记录');
  assert.equal(observed.calls.get('shared.md'), 1, '重叠文档的在途分块不得重复调用模型');
  assert.equal(observed.calls.get('extra.md'), 1);
  assert.equal(observed.reductions(), 2, '重叠文档汇总应共享，额外文档独立汇总');
  assert.equal(secondResult.reusedChunks, 1, '加入在途分块应计入复用数量');
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

test('模型持续不调用分段工具时保留公开摘要，但如实报告覆盖不完整', async () => {
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
  const coverage = new Map((result.analysis.documentReadCoverage ?? []).map((item) => [item.filename, item]));
  assert.equal(coverage.get('a.md')?.complete, true, '未降级文档仍应完整');
  assert.equal(coverage.get('b.md')?.complete, false, '兜底摘要不得冒充已覆盖原文');
  assert.equal(coverage.get('b.md')?.eofSeen, false);
  assert.equal(result.analysis.coverageComplete, false);
  assert.ok((result.analysis.failedChunkCount ?? 0) > 0, '降级分段数必须如实计数');
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

test('产品事实在分段层捕获，精读结果直接带出逐项清单和计数', async () => {
  const project = createProject({ owner: 'tester', name: '合成产品事实分段捕获' });
  const observed = observedLlm({ delayMs: 1, productChunksFor: ['catalog.md'] });
  const result = await runMaterialAnalysisMapReduce({
    projectId: project.id,
    projectContext: '合成产品手册',
    tables: [],
    documents: [{
      id: 'doc-catalog', filename: 'catalog.md', revisionFingerprint: 'catalog:v1',
      textContent: '合成产品条目与准入条件。'.repeat(400),
    }],
    llm: observed.llm,
    documentOnly: true,
    requireProductInventory: true,
  });

  const items = result.analysis.productInventory ?? [];
  assert.ok(items.length > 0, '分段层应产出逐项产品事实');
  assert.ok(items.every((item) => item.completenessContract === 'source-fields-v1'));
  assert.ok(items.every((item) => item.evidenceStatus === 'complete'));
  assert.deepEqual(result.analysis.productInventorySummary, {
    rawCount: items.length,
    canonicalCount: items.length,
    mergedGroupCount: 0,
    completeCount: items.length,
    insufficientCount: 0,
  });
  assert.equal(result.analysis.coverageComplete, true);
  assert.equal(result.analysis.failedChunkCount, 0);
});

test('分段产品事实缺来源字段时判为资料不足，而不是按完整计数', async () => {
  const project = createProject({ owner: 'tester', name: '合成产品事实缺口' });
  const observed = observedLlm({
    delayMs: 1, productChunksFor: ['gap.md'], missingFactField: 'admissionConditions',
  });
  const result = await runMaterialAnalysisMapReduce({
    projectId: project.id,
    projectContext: '合成产品手册',
    tables: [],
    documents: [{
      id: 'doc-gap', filename: 'gap.md', revisionFingerprint: 'gap:v1',
      textContent: '合成产品条目缺少准入条件。'.repeat(400),
    }],
    llm: observed.llm,
    documentOnly: true,
    requireProductInventory: true,
  });

  const items = result.analysis.productInventory ?? [];
  assert.ok(items.length > 0);
  assert.ok(items.every((item) => item.structuredGaps?.includes('admissionConditions')));
  assert.ok(items.every((item) => item.evidenceStatus === 'insufficient'));
  assert.ok(items.every((item) => item.processingStatus === 'blocked'));
  assert.equal(result.analysis.productInventorySummary?.completeCount, 0);
  assert.equal(result.analysis.productInventorySummary?.insufficientCount, items.length);
});

test('声明已提取却不交逐项产品事实时分段提交不被接受，分析不会假装完成', async () => {
  const project = createProject({ owner: 'tester', name: '合成产品事实空口径' });
  const observed = observedLlm({ delayMs: 1, emptyInventoryClaimFor: 'claim.md' });
  const input = {
    projectId: project.id,
    projectContext: '合成产品手册',
    tables: [],
    documents: [{
      id: 'doc-claim', filename: 'claim.md', revisionFingerprint: 'claim:v1',
      textContent: '合成产品条目。'.repeat(400),
    }],
    llm: observed.llm,
    documentOnly: true,
    requireProductInventory: true,
  };
  // 只声明口径、不交逐项数据的提交会被捕获契约拒绝：此处模型除该提交外没有任何正文，
  // 因此本段既拿不到结构化结果也没有可兜底的摘要，分析必须失败而不是记为已完成。
  await assert.rejects(runMaterialAnalysisMapReduce(input), /分段分析未提交结构化结果/u);
});

test('分段提交被捕获契约拒绝后，重试提示必须说明是参数校验失败而不是没调用工具', async () => {
  const project = createProject({ owner: 'tester', name: '合成分段校验纠错' });
  const observed = observedLlm({ delayMs: 1, rejectInventoryOnceFor: 'catalog.md', productChunksFor: ['catalog.md'] });
  const result = await runMaterialAnalysisMapReduce({
    projectId: project.id,
    projectContext: '合成产品手册',
    tables: [],
    documents: [{
      id: 'doc-reject', filename: 'catalog.md', revisionFingerprint: 'catalog:v1',
      textContent: '合成产品条目与准入条件。'.repeat(400),
    }],
    llm: observed.llm,
    documentOnly: true,
    requireProductInventory: true,
  });

  // 不带诊断时模型只会收到"请调用提交工具"，而它明明已经调用过，于是原样重复直到本段失败。
  const retryPrompts = observed.chunkPrompts.slice(1);
  assert.ok(
    retryPrompts.some((prompt) => prompt.includes('上一次工具参数未通过校验')),
    '被拒后的重试必须携带参数校验失败的错因，而不是通用催调用话术',
  );
  assert.ok(
    retryPrompts.every((prompt) => !prompt.includes('上一次未调用目标提交工具')),
    '已调用过工具的分段不得被判为未调用',
  );
  assert.equal(result.analysis.failedChunkCount, 0, '拿到错因后应能自行纠正并完成本段');
  assert.ok((result.analysis.productInventory ?? []).length > 0);
});

test('要求产品台账时不复用只含摘要的既有分析，重新按分段捕获', async () => {
  const project = createProject({ owner: 'tester', name: '合成台账检查点隔离' });
  const documentSet = [{
    id: 'doc-reuse-inventory', filename: 'catalog.md', revisionFingerprint: 'catalog:v1',
    textContent: '合成产品条目与准入条件。'.repeat(400),
  }];
  const summaryOnly = observedLlm({ delayMs: 1 });
  const first = await runMaterialAnalysisMapReduce({
    projectId: project.id,
    projectContext: '合成产品手册',
    tables: [],
    documents: documentSet,
    llm: summaryOnly.llm,
    documentOnly: true,
  });
  assert.deepEqual(first.analysis.productInventory ?? [], []);

  const withInventory = observedLlm({ delayMs: 1, productChunksFor: ['catalog.md'] });
  const second = await runMaterialAnalysisMapReduce({
    projectId: project.id,
    projectContext: '合成产品手册',
    tables: [],
    documents: documentSet,
    llm: withInventory.llm,
    documentOnly: true,
    requireProductInventory: true,
  });
  assert.notEqual(second.runId, first.runId, '产品事实要求不同的分析不得共用检查点');
  assert.ok((withInventory.calls.get('catalog.md') ?? 0) > 0, '应重新分析分段而不是复用摘要');
  assert.ok((second.analysis.productInventory ?? []).length > 0);
});
