import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

import Fastify from 'fastify';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'modeling-material-context-'));
process.env.MODELING_LLM_BASE_URL = 'faux';
process.env.MODELING_LLM_MODEL = 'faux';

const [
  { registerAgentCoreRoutes },
  { createProject },
  { getDb },
  { getDocument, insertDocument },
] = await Promise.all([
  import('../src/agent/core-routes.js'),
  import('../src/projects/repo.js'),
  import('../src/db/index.js'),
  import('../src/documents/repo.js'),
]);

function appFor(t: test.TestContext, username = 'alice') {
  const app = Fastify();
  app.addHook('preHandler', async (request) => {
    request.user = { username, role: 'modeler' };
  });
  registerAgentCoreRoutes(app);
  t.after(() => app.close());
  return app;
}

function tool(app: ReturnType<typeof Fastify>, projectId: string, name: string,
  args: Record<string, unknown> = {}, runId = `run-mc-${projectId}`) {
  return app.inject({
    method: 'POST',
    url: `/internal/agent/tools/${name}`,
    headers: {
      'x-modeling-project': projectId,
      'x-modeling-run': runId,
      authorization: 'Bearer agent-test-token',
    },
    payload: { arguments: args },
  });
}

// 与 core-service 的 documentRevisionFingerprint 同一拼接规则：
// 档案条目按它标注 stale，测试必须能复算出「当前版本」的指纹。
function fingerprintOf(documentId: string): string {
  const record = getDocument(documentId) as any;
  return [
    record.id,
    record.sourceKey ?? '',
    record.revisionNo ?? 0,
    record.contentHash ?? '',
    record.semanticHash ?? '',
  ].join(':');
}

function seedArchive(projectId: string, entries: Array<{
  documentId: string; filename: string; fingerprint: string;
  facts: Array<{ offset: number; kind: string; note: string }>;
  candidates?: Array<Record<string, unknown>>;
  summary: string;
}>) {
  const db = getDb();
  const now = new Date().toISOString();
  const runId = `mar-${projectId}`;
  db.prepare(`INSERT INTO material_analysis_runs (
      id, project_id, source_set_fingerprint, brief_fingerprint, model_id, protocol_version,
      plan_json, status, stage, result_json, created_at, updated_at, completed_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'completed', 'complete', ?, ?, ?, ?)`)
    .run(runId, projectId, 'fp-src', 'fp-brief', 'model-x', 'v1', '{}',
      JSON.stringify({ domainOverview: '合成领域概览' }), now, now, now);
  for (const [index, entry] of entries.entries()) {
    db.prepare(`INSERT INTO material_analysis_chunks (
        run_id, document_key, document_id, filename, revision_fingerprint, chunk_index,
        start_offset, end_offset, content_hash, status, payload_json, updated_at, completed_at
      ) VALUES (?, ?, ?, ?, ?, 0, 0, 4000, ?, 'completed', ?, ?, ?)`)
      .run(runId, `doc-key-${index}`, entry.documentId, entry.filename, entry.fingerprint,
        `hash-${index}`, JSON.stringify({
          summary: entry.summary,
          evidence: entry.facts,
          candidates: entry.candidates ?? [],
        }), now, now);
    db.prepare(`INSERT INTO material_analysis_document_summaries (
        run_id, document_key, document_id, filename, revision_fingerprint,
        status, payload_json, updated_at, completed_at
      ) VALUES (?, ?, ?, ?, ?, 'completed', ?, ?, ?)`)
      .run(runId, `doc-key-${index}`, entry.documentId, entry.filename, entry.fingerprint,
        JSON.stringify({ summary: entry.summary, suitableFor: ['rule'] }), now, now);
  }
  return runId;
}

test('档案未建立时返回可恢复的空简报，而不是报错', async (t) => {
  const project = createProject({ owner: 'alice', name: '档案未建立', runtime: 'agent' });
  const app = appFor(t);
  await tool(app, project.id, 'claim_candidate_workspace', {});
  const response = await tool(app, project.id, 'get_material_context', {});
  assert.equal(response.statusCode, 200, response.body);
  const payload = response.json();
  assert.equal(payload.available, false);
  assert.match(String(payload.instruction), /全文精读|search_documents/u);
});

test('档案查询返回结论+锚点，并支持 kind/topic/document 过滤', async (t) => {
  const project = createProject({ owner: 'alice', name: '档案查询过滤', runtime: 'agent' });
  const app = appFor(t);
  const document = insertDocument({
    projectId: project.id,
    filename: 'synthetic_inventory_rules.md',
    kind: 'markdown',
    parseStatus: 'parsed',
    textContent: '合成库存管理制度：安全库存阈值为最小周转量的两倍。装置每月检修一次。',
  });
  seedArchive(project.id, [{
    documentId: document.id,
    filename: document.filename,
    fingerprint: fingerprintOf(document.id),
    summary: '该资料描述合成库存管理制度',
    facts: [
      { offset: 12, kind: 'rule', note: '安全库存阈值为最小周转量的两倍' },
      { offset: 80, kind: 'fact', note: '装置每月检修一次' },
    ],
    candidates: [{
      kind: 'rule', name: '安全库存规则',
      rationale: '资料明确了安全库存计算口径', evidenceOffsets: [12],
    }],
  }]);
  await tool(app, project.id, 'claim_candidate_workspace', {});

  const all = await tool(app, project.id, 'get_material_context', {});
  assert.equal(all.statusCode, 200, all.body);
  const payload = all.json();
  assert.equal(payload.available, true);
  assert.equal(payload.documents.length, 1);
  assert.equal(payload.documents[0].stale, false);
  assert.equal(payload.facts.length, 2);
  // 锚点必须可直接用于 read_document 定点核对
  assert.equal(payload.facts[0].filename, 'synthetic_inventory_rules.md');
  assert.equal(typeof payload.facts[0].offset, 'number');
  assert.equal(payload.candidates.length, 1);
  assert.deepEqual(payload.candidates[0].evidenceOffsets, [12]);
  assert.match(String(payload.instruction), /read_document 核对原文/u);

  const byKind = await tool(app, project.id, 'get_material_context', { kind: 'rule' });
  assert.equal(byKind.json().facts.length, 1);
  assert.equal(byKind.json().facts[0].kind, 'rule');

  const byTopic = await tool(app, project.id, 'get_material_context', { topic: '安全库存' });
  assert.equal(byTopic.json().facts.length, 1);
  assert.match(byTopic.json().facts[0].note, /安全库存/u);

  const missTopic = await tool(app, project.id, 'get_material_context', { topic: '完全无关词组' });
  assert.equal(missTopic.json().facts.length, 0);
  assert.equal(missTopic.json().candidates.length, 0);

  const byDocument = await tool(app, project.id, 'get_material_context', {
    document: 'synthetic_inventory_rules.md',
  });
  assert.equal(byDocument.json().documents.length, 1);

  const badKind = await tool(app, project.id, 'get_material_context', { kind: 'nonsense' });
  assert.equal(badKind.statusCode, 400);
});

test('资料已更新时档案条目标记 stale 并提示重新精读', async (t) => {
  const project = createProject({ owner: 'alice', name: '档案过期标注', runtime: 'agent' });
  const app = appFor(t);
  const document = insertDocument({
    projectId: project.id,
    filename: 'synthetic_process_notes.md',
    kind: 'markdown',
    parseStatus: 'parsed',
    textContent: '合成流程说明：反应温度控制在既定区间。',
  });
  // 指纹与当前修订不一致：档案来自旧版本
  seedArchive(project.id, [{
    documentId: document.id,
    filename: document.filename,
    fingerprint: 'stale:fingerprint:0::',
    summary: '旧版本的流程说明摘要',
    facts: [{ offset: 5, kind: 'process', note: '反应温度控制在既定区间' }],
  }]);
  await tool(app, project.id, 'claim_candidate_workspace', {});
  const response = await tool(app, project.id, 'get_material_context', {});
  assert.equal(response.statusCode, 200, response.body);
  const payload = response.json();
  assert.equal(payload.documents[0].stale, true);
  assert.equal(payload.facts[0].stale, true);
  assert.match(String(payload.instruction), /重新全文精读/u);
});
