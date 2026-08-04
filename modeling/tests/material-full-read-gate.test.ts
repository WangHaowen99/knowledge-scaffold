import assert from 'node:assert/strict';
import test from 'node:test';
import { createFauxCore, fauxAssistantMessage, fauxToolCall } from '@earendil-works/pi-ai/providers/faux';

import { getDb } from '../src/db/index.js';
import type { LlmDeps } from '../src/llm/provider.js';
import { createProject } from '../src/projects/repo.js';
import { listNodes, runAnalyze } from '../src/workflow/engine.js';

test('runMaterialAnalysis 未完成全文覆盖时失败且不写批次缓存', async () => {
  const project = createProject({ owner: 'tester', name: '全文覆盖门禁' });
  const core = createFauxCore({ provider: 'skip-reading', models: [{ id: 'skip-reading' }] });
  const streamFn: LlmDeps['streamFn'] = async (model, context, options) => {
    core.setResponses([fauxAssistantMessage([fauxToolCall('submit_analysis', {
      domainOverview: '模型自称已读完，但没有调用 read_document',
      candidateObjects: [],
      openQuestions: [],
    })])]);
    return core.streamSimple(model as any, context, options as any);
  };
  const llm: LlmDeps = { model: core.getModel(), streamFn, getApiKey: () => 'faux' };

  await assert.rejects(
    runAnalyze(project.id, [], [{ filename: '营销产品手册.docx', textContent: 'x'.repeat(20_000) }], llm),
    /本次未完成全文读取.*已读取 0 \/ 20000 字符/s,
  );

  const node = listNodes(project.id).find((item) => item.type === 'analyze');
  assert.equal(node?.status, 'failed');
  assert.match(node?.reason ?? '', /未读取区间为 0–20000/);
  const cached = getDb().prepare(
    'SELECT COUNT(*) AS count FROM material_analysis_batch_cache WHERE project_id = ?',
  ).get(project.id) as { count: number };
  assert.equal(cached.count, 0);
});
