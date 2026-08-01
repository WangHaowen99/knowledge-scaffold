import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createFauxCore, fauxAssistantMessage, fauxToolCall } from '@earendil-works/pi-ai/providers/faux';
import { runAnalyze } from '../src/workflow/engine.js';
import { createProject } from '../src/projects/repo.js';
import type { LlmDeps } from '../src/llm/provider.js';

/** 按批次提示词里声明的来源，生成一份刚好通过校验的应答 */
function artifactReplyFor(systemPrompt: string) {
  const match = systemPrompt.match(/当前批次来源：(\[.*?\])\n/s);
  const refs: Array<{ source: string; sourceType: string }> = match ? JSON.parse(match[1]) : [];
  return {
    materialAssessments: refs.map((ref) => ({
      source: ref.source, sourceType: ref.sourceType,
      suitableFor: ['rule'], rationale: '材料给出了判定条件',
    })),
    candidateArtifacts: refs.map((ref) => ({
      kind: 'rule', name: `规则-${ref.source}`, rationale: '来自该材料',
      sources: [ref.source], readiness: 'extractable',
    })),
  };
}

function countingLlm() {
  const calls = { domain: 0, artifact: 0 };
  const core = createFauxCore({ provider: 'incremental', models: [{ id: 'incremental' }] });
  const llm: LlmDeps = {
    model: core.getModel(),
    getApiKey: () => 'faux',
    streamFn: ((model: unknown, context: any, options: unknown) => {
      const system = String(context.systemPrompt ?? '');
      if (system.includes('submit_artifact_analysis')) {
        calls.artifact += 1;
        core.setResponses([fauxAssistantMessage([
          fauxToolCall('submit_artifact_analysis', artifactReplyFor(system)),
        ])]);
      } else if (system.includes('submit_analysis')) {
        calls.domain += 1;
        core.setResponses([fauxAssistantMessage([fauxToolCall('submit_analysis', {
          domainOverview: '增量分析验证', candidateObjects: [], openQuestions: [],
        })])]);
      } else {
        throw new Error(`未处理的 prompt: ${system.slice(0, 60)}`);
      }
      return core.streamSimple(model as any, context, options as any);
    }) as LlmDeps['streamFn'],
  };
  return { llm, calls };
}

const doc = (name: string) => ({ filename: name, textContent: `${name} 的内容：满足条件时执行。` });

test('补传资料只重跑变化的批次，已分析过的批次直接复用', async () => {
  const project = createProject({ owner: 'tester', name: '增量分析' });
  // 批大小是 4，先用 4 份填满第一批
  const first = [doc('a.md'), doc('b.md'), doc('c.md'), doc('d.md')];

  const run1 = countingLlm();
  await runAnalyze(project.id, [], first, run1.llm);
  assert.equal(run1.calls.artifact, 1, '首轮应分析 1 批');

  // 补传第 5 份：第一批内容没变，只有新起的第二批需要跑
  const run2 = countingLlm();
  const progress: string[] = [];
  const result = await runAnalyze(
    project.id, [], [...first, doc('e.md')], run2.llm, (update) => progress.push(update.title ?? ''),
  );

  assert.equal(run2.calls.artifact, 1, '第一批应复用，只跑新增的第二批');
  assert.equal(run2.calls.domain, 1, '领域分析覆盖全部资料，必须重跑');
  assert.ok(progress.some((title) => title.includes('复用已有分析')), '复用应当在进度里说明');

  // 复用不能丢材料：5 份都要有评估结论
  const sources = (result.analysis.materialAssessments ?? []).map((item) => item.source).sort();
  assert.deepEqual(sources, ['a.md', 'b.md', 'c.md', 'd.md', 'e.md']);
});

test('资料内容被改动后不复用旧结论', async () => {
  const project = createProject({ owner: 'tester', name: '改动后重分析' });
  const before = [doc('x.md'), doc('y.md')];
  const run1 = countingLlm();
  await runAnalyze(project.id, [], before, run1.llm);
  assert.equal(run1.calls.artifact, 1);

  const run2 = countingLlm();
  await runAnalyze(project.id, [], [
    { filename: 'x.md', textContent: '改过的内容：门槛提高到 500 万。' }, before[1],
  ], run2.llm);
  assert.equal(run2.calls.artifact, 1, '批次内容变了必须重跑，不能复用');
});
