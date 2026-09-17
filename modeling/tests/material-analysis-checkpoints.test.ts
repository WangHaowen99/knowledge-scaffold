import assert from 'node:assert/strict';
import test from 'node:test';
import { getDb } from '../src/db/index.js';
import { planDocumentAnalysis } from '../src/workflow/document-analysis-plan.js';
import {
  MaterialAnalysisCheckpointStore,
  materialSourceSetFingerprint,
} from '../src/workflow/material-analysis-checkpoints.js';

test('分段完成后恢复运行只领取未完成分段', () => {
  const unique = `${Date.now()}_${Math.random().toString(36).slice(2)}`;
  const projectId = `pj_checkpoint_${unique}`;
  getDb().prepare(`INSERT INTO projects (id, owner, name, domain, background, stage, created_at, updated_at)
    VALUES (?,?,?,?,?,'project_setup',?,?)`).run(
    projectId, 'tester', '合成检查点项目', '', '', new Date().toISOString(), new Date().toISOString(),
  );
  const documents = [{ id: `doc_demo_${unique}`, filename: 'manual.md', textContent: '合成资料。'.repeat(8_000), revisionFingerprint: unique }];
  const plan = planDocumentAnalysis(documents, { model: { id: 'demo', contextWindow: 32_000, maxTokens: 4_096 } });
  assert.ok(plan.chunks.length > 1);
  const store = new MaterialAnalysisCheckpointStore();
  const run = store.startOrResume({
    projectId,
    sourceSetFingerprint: materialSourceSetFingerprint({ tables: [], documents }),
    briefFingerprint: 'brief-1',
    plan,
  });
  const first = store.listChunks(run.id)[0];
  store.beginChunk(run.id, first);
  store.completeChunk(run.id, first, { summary: '已完成第一段', evidence: [], candidates: [], openQuestions: [] });
  const running = store.listChunks(run.id)[1];
  store.beginChunk(run.id, running);

  const resumed = store.startOrResume({
    projectId,
    sourceSetFingerprint: run.sourceSetFingerprint,
    briefFingerprint: 'brief-1',
    plan,
  });
  const chunks = store.listChunks(resumed.id);
  assert.equal(chunks[0].status, 'completed');
  assert.equal(chunks[1].status, 'pending');
  assert.equal(chunks.filter((chunk) => chunk.status === 'completed').length, 1);
});

test('相同文档分段在新运行中按内容哈希复用', () => {
  const unique = `${Date.now()}_${Math.random().toString(36).slice(2)}`;
  const projectId = `pj_reuse_${unique}`;
  getDb().prepare(`INSERT INTO projects (id, owner, name, domain, background, stage, created_at, updated_at)
    VALUES (?,?,?,?,?,'project_setup',?,?)`).run(
    projectId, 'tester', '合成复用项目', '', '', new Date().toISOString(), new Date().toISOString(),
  );
  const documents = [{ id: `doc_reuse_${unique}`, filename: 'manual.md', textContent: '合成内容。'.repeat(3_000), revisionFingerprint: unique }];
  const plan = planDocumentAnalysis(documents, { model: { id: 'demo', contextWindow: 32_000, maxTokens: 4_096 } });
  const store = new MaterialAnalysisCheckpointStore();
  const first = store.startOrResume({
    projectId,
    sourceSetFingerprint: 'source-a', briefFingerprint: 'brief-a', plan,
  });
  for (const chunk of store.listChunks(first.id)) {
    store.beginChunk(first.id, chunk);
    store.completeChunk(first.id, chunk, { summary: '可复用', evidence: [], candidates: [], openQuestions: [] });
  }
  const second = store.startOrResume({
    projectId,
    sourceSetFingerprint: 'source-b', briefFingerprint: 'brief-a', plan,
  });
  assert.ok(store.listChunks(second.id).every((chunk) => chunk.status === 'completed'));
});

test('只重开不可用分段：降级段和缺产品事实的段回到待办，其余保持完成', () => {
  const unique = `${Date.now()}_${Math.random().toString(36).slice(2)}`;
  const projectId = `pj_retry_${unique}`;
  getDb().prepare(`INSERT INTO projects (id, owner, name, domain, background, stage, created_at, updated_at)
    VALUES (?,?,?,?,?,'project_setup',?,?)`).run(
    projectId, 'tester', '合成重试项目', '', '', new Date().toISOString(), new Date().toISOString(),
  );
  const documents = [{ id: `doc_retry_${unique}`, filename: 'manual.md', textContent: '合成资料。'.repeat(8_000), revisionFingerprint: unique }];
  const plan = planDocumentAnalysis(documents, { model: { id: 'demo', contextWindow: 32_000, maxTokens: 4_096 } });
  const store = new MaterialAnalysisCheckpointStore();
  const run = store.startOrResume({
    projectId,
    sourceSetFingerprint: materialSourceSetFingerprint({ tables: [], documents }),
    briefFingerprint: 'brief-retry',
    plan,
  });
  const chunks = store.listChunks(run.id);
  assert.ok(chunks.length >= 3);
  const inventoryItem = {
    completenessContract: 'source-fields-v1' as const,
    canonicalItemId: 'product-a', name: '合成产品 A', ruleRefs: ['rule-a'],
    sourceRecordIds: ['source-a'], sourceLocator: 'manual#a',
    evidenceStatus: 'complete' as const, processingStatus: 'ready' as const,
    applicableSubjects: ['合成主体'], triggerScenarios: ['合成场景'], businessNeeds: ['合成需求'],
    admissionConditions: ['合成准入'], exclusionConditions: ['合成排除'], structuredGaps: [],
  };
  const payloads = [
    { summary: '含产品事实', evidence: [], candidates: [], openQuestions: [], productInventory: [inventoryItem] },
    { summary: '仅摘要', evidence: [], candidates: [], openQuestions: [] },
    { summary: '兜底摘要', evidence: [], candidates: [], openQuestions: [], degraded: true },
  ];
  chunks.slice(0, 3).forEach((chunk, index) => {
    store.beginChunk(run.id, chunk);
    store.completeChunk(run.id, chunk, payloads[index]);
  });
  store.beginDocumentSummary(run.id, chunks[0]);
  store.completeDocumentSummary(run.id, chunks[0].documentKey, {
    summary: '文档摘要', suitableFor: [], rationale: '合成', candidates: [], openQuestions: [],
  });

  // 不要求产品事实时，只有降级段需要重做。
  store.retryIncompleteChunks(run.id, false);
  const afterDegradedOnly = store.listChunks(run.id);
  assert.equal(afterDegradedOnly[0].status, 'completed');
  assert.equal(afterDegradedOnly[1].status, 'completed');
  assert.equal(afterDegradedOnly[2].status, 'pending');
  assert.equal(store.getRun(run.id)?.status, 'running');
  assert.equal(store.getDocumentSummary(run.id, chunks[0].documentKey), undefined, '重开分段后文档摘要必须重算');

  // 要求产品事实时，仅有摘要的段也不算可用。
  store.retryIncompleteChunks(run.id, true);
  const afterInventoryRequired = store.listChunks(run.id);
  assert.equal(afterInventoryRequired[0].status, 'completed', '已带产品事实的段不得被重做');
  assert.equal(afterInventoryRequired[1].status, 'pending');
  assert.equal(afterInventoryRequired[0].attemptCount, 1, '保留已完成分段的尝试计数');
});
