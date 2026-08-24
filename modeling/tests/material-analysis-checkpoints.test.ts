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
