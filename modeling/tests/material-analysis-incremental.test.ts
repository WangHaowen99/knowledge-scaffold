import assert from 'node:assert/strict';
import test from 'node:test';
import { buildFauxModeler, type LlmDeps } from '../src/llm/provider.js';
import { createProject } from '../src/projects/repo.js';
import { runAnalyze } from '../src/workflow/engine.js';

function countingLlm() {
  const base = buildFauxModeler();
  const calls = { chunks: 0, documents: 0, domain: 0 };
  const llm: LlmDeps = {
    ...base,
    streamFn: ((model: unknown, context: any, options: unknown) => {
      const system = String(context.systemPrompt ?? '');
      if (system.includes('submit_document_chunk_analysis')) calls.chunks += 1;
      if (system.includes('submit_document_analysis')) calls.documents += 1;
      if (system.includes('submit_material_analysis_result')) calls.domain += 1;
      return base.streamFn(model, context, options);
    }) as LlmDeps['streamFn'],
  };
  return { llm, calls };
}

const doc = (name: string, content = `${name} 的合成内容：满足条件时执行。`) => ({
  id: `doc_${name}`,
  filename: name,
  textContent: content,
  revisionFingerprint: `${name}:${content.length}`,
});

test('补传资料只分析新增分段，未变文档的分段和汇总均复用', async () => {
  const project = createProject({ owner: 'tester', name: '合成增量分析' });
  const first = [doc('a.md'), doc('b.md'), doc('c.md'), doc('d.md')];
  const initial = countingLlm();
  await runAnalyze(project.id, [], first, initial.llm);
  assert.equal(initial.calls.chunks, 4);
  assert.equal(initial.calls.documents, 4);

  const next = countingLlm();
  const progress: string[] = [];
  const result = await runAnalyze(
    project.id, [], [...first, doc('e.md')], next.llm, (update) => progress.push(update.title),
  );
  assert.equal(next.calls.chunks, 1);
  assert.equal(next.calls.documents, 1);
  assert.equal(next.calls.domain, 1);
  assert.ok(progress.some((title) => title.includes('复用 4/5 个已完成分段')));
  assert.deepEqual(
    result.analysis.materialAssessments?.map((item) => item.source).sort(),
    ['a.md', 'b.md', 'c.md', 'd.md', 'e.md'],
  );
});

test('资料内容变化后只重跑变化文档，其他文档继续复用', async () => {
  const project = createProject({ owner: 'tester', name: '合成内容变更分析' });
  const before = [doc('x.md'), doc('y.md')];
  await runAnalyze(project.id, [], before, countingLlm().llm);

  const changed = countingLlm();
  await runAnalyze(project.id, [], [
    doc('x.md', '修改后的合成内容：阈值需要专家确认。'),
    before[1],
  ], changed.llm);
  assert.equal(changed.calls.chunks, 1);
  assert.equal(changed.calls.documents, 1);
  assert.equal(changed.calls.domain, 1);
});
