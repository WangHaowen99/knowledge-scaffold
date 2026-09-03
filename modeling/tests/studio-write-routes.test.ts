import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

import Fastify from 'fastify';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'modeling-studio-write-'));

const [
  { registerAgentCoreRoutes },
  { createProject, setProjectMember },
  { getPackageArtifactStore },
  { getDb },
  { insertDocument },
] = await Promise.all([
  import('../src/agent/core-routes.js'),
  import('../src/projects/repo.js'),
  import('../src/artifacts/runtime.js'),
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

function tool(
  app: ReturnType<typeof Fastify>,
  projectId: string,
  runId: string,
  name: string,
  args: Record<string, unknown>,
) {
  return app.inject({
    method: 'POST',
    url: `/internal/agent/tools/${name}`,
    headers: {
      'x-modeling-project': projectId,
      'x-modeling-run': runId,
      authorization: 'Bearer studio-test-token',
    },
    payload: { arguments: args },
  });
}

async function seedLegacyDanglingPackage(projectId: string) {
  const artifacts = getPackageArtifactStore();
  const initialized = await artifacts.initializeDraftPackage(projectId, {
    id: `legacy-${projectId}`,
    name: '存量知识包',
    description: '包含需要后续治理的存量引用。',
  });
  return artifacts.savePackageVersion(projectId, {
    expectedRevision: initialized.revision.oid,
    spec: {
      ...structuredClone(initialized.spec),
      objects: [{
        key: 'customer',
        title: '客户',
        description: '客户主数据。',
        fields: [],
      }],
      rules: [{
        key: 'legacy-policy',
        title: '存量规则',
        description: '待补齐历史对象引用。',
        content: '按存量口径执行。',
        appliesToObjects: ['retired-customer'],
      }],
    },
    coverage: initialized.coverage,
    note: '构造存量不完整知识包',
    action: {
      cause: 'legacy_package_seeded',
      summary: '写入存量知识包测试基线',
      actorKind: 'system',
      actorName: '测试系统',
    },
  });
}

async function seedPhysicalMappingPackage(projectId: string) {
  const artifacts = getPackageArtifactStore();
  const initialized = await artifacts.initializeDraftPackage(projectId, {
    id: `mapping-noop-${projectId}`,
    name: 'Studio 物理对象基线',
    description: '用于验证 mapping no-op 草稿不会阻塞确定性应用。',
  });
  return artifacts.savePackageVersion(projectId, {
    expectedRevision: initialized.revision.oid,
    spec: {
      ...structuredClone(initialized.spec),
      objects: [{
        key: 'synthetic_record',
        title: '合成记录',
        description: '合成物理对象。',
        sourceTable: 'synthetic_records',
        sourceRefs: [{ kind: 'table' as const, table: 'synthetic_records' }],
        primaryKey: ['record_id'],
        fields: [{
          key: 'record_id', cnName: '记录编号', type: 'string', required: true, sourceColumn: 'record_id',
        }],
      }],
    },
    coverage: initialized.coverage,
    note: '建立包含已提交 mapping 的基线',
    action: {
      cause: 'mapping_noop_seeded',
      summary: '写入 mapping no-op 测试基线',
      actorKind: 'system',
      actorName: '测试系统',
    },
  });
}

function insertNoOpDraft(projectId: string, runId: string, baseFiles: Record<string, string>) {
  const mapping = baseFiles['mappings/object-data-mappings.json'];
  assert.ok(mapping);
  const insert = getDb().prepare(`INSERT INTO agent_candidate_changes (
    project_id, run_id, path, kind, content, group_id, evidence_json, reason, updated_at
  ) VALUES (?,?,?,?,?,?,'[]',?,?)`);
  insert.run(
    projectId, runId, 'mappings/object-data-mappings.json', 'write', mapping,
    'legacy-noop', '残留 mapping no-op 草稿', '2026-09-03T00:00:00.000Z',
  );
  insert.run(
    projectId, runId, 'mappings/not-in-base.json', 'delete', null,
    'legacy-noop', '残留 delete no-op 草稿', '2026-09-03T00:00:00.000Z',
  );
}

async function seedRecordPackage(projectId: string) {
  const artifacts = getPackageArtifactStore();
  const initialized = await artifacts.initializeDraftPackage(projectId, {
    id: `studio-format-record-${projectId}`,
    name: 'Studio 格式检查基线',
    description: '包含已提交对象的合成基线。',
  });
  return artifacts.savePackageVersion(projectId, {
    expectedRevision: initialized.revision.oid,
    spec: {
      ...structuredClone(initialized.spec),
      objects: [{
        key: 'record',
        title: '记录',
        description: '已提交的合成记录对象。',
        fields: [{ key: 'id', cnName: '标识', type: 'string', required: true }],
      }],
    },
    coverage: initialized.coverage,
    note: '建立格式检查测试基线',
    action: {
      cause: 'studio_format_seeded',
      summary: '写入格式检查测试基线',
      actorKind: 'system',
      actorName: '测试系统',
    },
  });
}

test('Studio 新建规则必须引用当前项目的真实资料，后续编辑不能篡改证据', async (t) => {
  const project = createProject({ owner: 'alice', name: 'Studio 规则证据', runtime: 'agent' });
  const material = insertDocument({
    projectId: project.id,
    filename: '安全库存管理办法.md',
    kind: 'markdown',
    parseStatus: 'parsed',
    textContent: '# 补货条件\n库存低于安全库存时发起补货。',
  });
  const app = appFor(t);
  const initial = await app.inject({ method: 'GET', url: `/api/projects/${project.id}/studio/elements` });
  const baseRevision = initial.json().workingRevision as string;

  const withoutEvidence = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/elements`,
    payload: {
      expectedRevision: baseRevision,
      clientMutationId: 'create-rule-without-evidence',
      kind: 'rule',
      key: 'replenishment',
      values: { title: '补货规则', content: '库存低于安全库存时发起补货。' },
    },
  });
  assert.equal(withoutEvidence.statusCode, 422, withoutEvidence.body);
  assert.equal(
    withoutEvidence.json().fieldErrors['values.evidence'],
    '至少选择一份项目资料并填写原文位置',
  );

  const unknownEvidence = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/elements`,
    payload: {
      expectedRevision: baseRevision,
      clientMutationId: 'create-rule-unknown-evidence',
      kind: 'rule',
      key: 'replenishment',
      values: {
        title: '补货规则',
        content: '库存低于安全库存时发起补货。',
        evidence: ['material:不存在的资料#补货条件'],
      },
    },
  });
  assert.equal(unknownEvidence.statusCode, 422, unknownEvidence.body);
  assert.equal(
    unknownEvidence.json().fieldErrors['values.evidence[0]'],
    '所选资料不存在或已经停用，请刷新资料清单后重试',
  );

  const withoutAnchor = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/elements`,
    payload: {
      expectedRevision: baseRevision,
      clientMutationId: 'create-rule-without-anchor',
      kind: 'rule',
      key: 'replenishment',
      values: {
        title: '补货规则',
        content: '库存低于安全库存时发起补货。',
        evidence: [`material:${material.sourceKey}`],
      },
    },
  });
  assert.equal(withoutAnchor.statusCode, 422, withoutAnchor.body);
  assert.equal(
    withoutAnchor.json().fieldErrors['values.evidence[0]'],
    '请填写资料中的原文位置',
  );

  const created = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/elements`,
    payload: {
      expectedRevision: baseRevision,
      clientMutationId: 'create-rule-real-evidence',
      kind: 'rule',
      key: 'replenishment',
      values: {
        title: '补货规则',
        content: '库存低于安全库存时发起补货。',
        evidence: [`  material:${material.sourceKey}#补货条件  `],
      },
    },
  });
  assert.equal(created.statusCode, 201, created.body);
  assert.deepEqual(created.json().element.values.evidence, [
    `material:${material.sourceKey}#补货条件`,
  ]);

  const patchEvidence = await app.inject({
    method: 'PATCH',
    url: `/api/projects/${project.id}/studio/elements/${encodeURIComponent('rule:replenishment')}`,
    payload: {
      expectedRevision: created.json().workingRevision,
      clientMutationId: 'patch-rule-evidence',
      changes: { evidence: [`material:${material.sourceKey}#其他位置`] },
    },
  });
  assert.equal(patchEvidence.statusCode, 422, patchEvidence.body);
  assert.equal(
    patchEvidence.json().fieldErrors['changes.evidence'],
    '该字段由建模流程维护，不能在 Studio 中直接编辑',
  );

  const emptyContent = await app.inject({
    method: 'PATCH',
    url: `/api/projects/${project.id}/studio/elements/${encodeURIComponent('rule:replenishment')}`,
    payload: {
      expectedRevision: created.json().workingRevision,
      clientMutationId: 'patch-rule-empty-content',
      changes: { content: '   ' },
    },
  });
  assert.equal(emptyContent.statusCode, 422, emptyContent.body);
  assert.equal(emptyContent.json().fieldErrors['changes.content'], '必填文本不能为空');

  const reviewed = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/review`,
    payload: {
      expectedRevision: created.json().workingRevision,
      clientMutationId: 'review-rule-deterministically',
    },
  });
  assert.equal(reviewed.statusCode, 200, reviewed.body);
  assert.equal(reviewed.json().mode, 'deterministic');
  assert.equal(reviewed.json().applied, true);
  assert.deepEqual(reviewed.json().elementIds, ['rule:replenishment']);
  assert.equal(getDb().prepare(`SELECT COUNT(*) AS count FROM agent_project_event_outbox
    WHERE project_id = ? AND event_type = 'studio_review'`).get(project.id)!.count, 0);
});

test('Studio 单条创建和编辑形成工作草稿，确定性检查后直接提交且不触发 Agent', async (t) => {
  const project = createProject({ owner: 'alice', name: 'Studio 写入', runtime: 'agent' });
  const app = appFor(t);
  const artifacts = getPackageArtifactStore();

  const schema = await app.inject({ method: 'GET', url: `/api/projects/${project.id}/studio/schema` });
  assert.equal(schema.statusCode, 200, schema.body);
  assert.deepEqual(schema.json().capabilities, {
    read: true, create: true, update: true, delete: true, batch: true, operationFlow: true,
  });

  const initial = await app.inject({ method: 'GET', url: `/api/projects/${project.id}/studio/elements` });
  assert.equal(initial.statusCode, 200, initial.body);
  const baseRevision = initial.json().workingRevision as string;
  const gitBefore = (await artifacts.current(project.id))!.revision.oid;

  const createPayload = {
    expectedRevision: baseRevision,
    clientMutationId: 'create-inventory-1',
    editSessionId: 'studio-session-1',
    kind: 'object',
    key: 'inventory',
    values: {
      title: '库存',
      description: '库存余额和可用量。',
      aliases: ['存量'],
    },
  };
  const created = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/elements`,
    payload: createPayload,
  });
  assert.equal(created.statusCode, 201, created.body);
  assert.equal(created.json().candidate, true);
  assert.equal(created.json().element.id, 'object:inventory');
  assert.match(created.json().display.text, /工作草稿/u);
  assert.deepEqual(created.json().warnings, []);
  assert.equal(created.json().historicalProblemCount, 0);
  assert.equal((await artifacts.current(project.id))!.revision.oid, gitBefore);
  assert.equal(created.headers.etag, `"${created.json().workingRevision}"`);
  assert.equal(getDb().prepare(`SELECT COUNT(*) AS count FROM agent_project_event_outbox
    WHERE project_id = ?`).get(project.id)!.count, 0);

  const rows = getDb().prepare(`SELECT DISTINCT group_id FROM agent_candidate_changes
    WHERE project_id = ? AND group_id <> 'compiled_resources'`).all(project.id) as Array<{ group_id: string }>;
  assert.deepEqual(rows.map((row) => row.group_id), [created.json().changeGroup.id]);

  const replay = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/elements`,
    payload: createPayload,
  });
  assert.equal(replay.statusCode, 201, replay.body);
  assert.equal(replay.json().replayed, true);
  assert.equal(replay.json().workingRevision, created.json().workingRevision);
  assert.equal(getDb().prepare(`SELECT COUNT(*) AS count FROM agent_project_event_outbox
    WHERE project_id = ?`).get(project.id)!.count, 0);

  const reused = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/elements`,
    payload: { ...createPayload, key: 'warehouse' },
  });
  assert.equal(reused.statusCode, 409, reused.body);
  assert.equal(reused.json().code, 'MUTATION_ID_REUSED');

  const duplicate = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/elements`,
    payload: {
      ...createPayload,
      expectedRevision: created.json().workingRevision,
      clientMutationId: 'create-inventory-duplicate',
    },
  });
  assert.equal(duplicate.statusCode, 409, duplicate.body);
  assert.equal(duplicate.json().code, 'ELEMENT_EXISTS');

  const missing = await app.inject({
    method: 'PATCH',
    url: `/api/projects/${project.id}/studio/elements/${encodeURIComponent('object:missing')}`,
    payload: {
      expectedRevision: created.json().workingRevision,
      clientMutationId: 'patch-missing-1',
      changes: { description: '不存在' },
    },
  });
  assert.equal(missing.statusCode, 404, missing.body);
  assert.equal(missing.json().code, 'ELEMENT_NOT_FOUND');

  const stale = await app.inject({
    method: 'PATCH',
    url: `/api/projects/${project.id}/studio/elements/${encodeURIComponent('object:inventory')}`,
    payload: {
      expectedRevision: baseRevision,
      clientMutationId: 'patch-inventory-stale',
      changes: { description: '不应写入' },
    },
  });
  assert.equal(stale.statusCode, 409, stale.body);
  assert.equal(stale.json().code, 'REVISION_CONFLICT');
  assert.equal(stale.json().actualRevision, created.json().workingRevision);

  const requiredCleared = await app.inject({
    method: 'PATCH',
    url: `/api/projects/${project.id}/studio/elements/${encodeURIComponent('object:inventory')}`,
    payload: {
      expectedRevision: created.json().workingRevision,
      clientMutationId: 'patch-inventory-empty-description',
      changes: { description: '   ' },
    },
  });
  assert.equal(requiredCleared.statusCode, 422, requiredCleared.body);
  assert.equal(
    requiredCleared.json().fieldErrors['changes.description'],
    '必填文本不能为空',
  );

  const patched = await app.inject({
    method: 'PATCH',
    url: `/api/projects/${project.id}/studio/elements/${encodeURIComponent('object:inventory')}`,
    payload: {
      expectedRevision: created.json().workingRevision,
      clientMutationId: 'patch-inventory-1',
      changes: { description: '库存余额、锁定量和可用量。' },
    },
  });
  assert.equal(patched.statusCode, 200, patched.body);
  assert.equal(patched.json().element.values.description, '库存余额、锁定量和可用量。');
  assert.equal((await artifacts.current(project.id))!.revision.oid, gitBefore);

  const patchedAgain = await app.inject({
    method: 'PATCH',
    url: `/api/projects/${project.id}/studio/elements/${encodeURIComponent('object:inventory')}`,
    payload: {
      expectedRevision: patched.json().workingRevision,
      clientMutationId: 'patch-inventory-2',
      changes: { description: '库存余额、锁定量、在途量和可用量。' },
    },
  });
  assert.equal(patchedAgain.statusCode, 200, patchedAgain.body);
  assert.equal(patchedAgain.json().element.values.description, '库存余额、锁定量、在途量和可用量。');
  assert.equal(getDb().prepare(`SELECT COUNT(*) AS count FROM agent_project_event_outbox
    WHERE project_id = ?`).get(project.id)!.count, 0);
  assert.equal(getDb().prepare(`SELECT candidate_id FROM agent_candidate_workspaces
    WHERE project_id = ?`).get(project.id)!.candidate_id, null);

  const operation = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/elements`,
    payload: {
      expectedRevision: patchedAgain.json().workingRevision,
      clientMutationId: 'create-inspect-inventory',
      kind: 'operation',
      key: 'inspect_inventory',
      values: {
        title: '检查库存',
        description: '读取并检查库存工作草稿。',
        flow: ['读取 object:inventory 并返回结果。'],
        relations: [{ kind: 'uses_object', targetId: 'object:inventory', label: '使用对象' }],
      },
    },
  });
  assert.equal(operation.statusCode, 201, operation.body);
  const emptyFlow = await app.inject({
    method: 'PATCH',
    url: `/api/projects/${project.id}/studio/elements/${encodeURIComponent('operation:inspect_inventory')}`,
    payload: {
      expectedRevision: operation.json().workingRevision,
      clientMutationId: 'patch-operation-empty-flow',
      changes: { flow: [] },
    },
  });
  assert.equal(emptyFlow.statusCode, 422, emptyFlow.body);
  assert.ok(Object.keys(emptyFlow.json().fieldErrors).some((path) => path.startsWith('changes.flow')));

  const reviewPayload = {
    expectedRevision: operation.json().workingRevision,
    clientMutationId: 'review-inventory-1',
  };
  const staleReview = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/review`,
    payload: { ...reviewPayload, expectedRevision: patchedAgain.json().workingRevision },
  });
  assert.equal(staleReview.statusCode, 409, staleReview.body);
  assert.equal(staleReview.json().code, 'REVISION_CONFLICT');
  assert.equal(getDb().prepare(`SELECT COUNT(*) AS count FROM agent_project_event_outbox
    WHERE project_id = ?`).get(project.id)!.count, 0);

  const review = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/review`,
    payload: reviewPayload,
  });
  assert.equal(review.statusCode, 200, review.body);
  assert.equal(review.json().reviewRequested, false);
  assert.equal(review.json().mode, 'deterministic');
  assert.equal(review.json().applied, true);
  assert.deepEqual(review.json().elementIds, ['object:inventory', 'operation:inspect_inventory']);
  assert.match(review.json().display.text, /确定性检查通过/u);
  assert.notEqual(review.json().currentRevision, gitBefore);
  assert.equal((await artifacts.current(project.id))!.revision.oid, review.json().currentRevision);
  assert.equal(getDb().prepare(`SELECT COUNT(*) AS count FROM agent_project_event_outbox
    WHERE project_id = ? AND event_type = 'studio_review'`).get(project.id)!.count, 0);

  const reviewReplay = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/review`,
    payload: reviewPayload,
  });
  assert.equal(reviewReplay.statusCode, 200, reviewReplay.body);
  assert.equal(reviewReplay.json().replayed, true);
  assert.equal(reviewReplay.json().currentRevision, review.json().currentRevision);
  const reusedReview = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/review`,
    payload: { ...reviewPayload, expectedRevision: patched.json().workingRevision },
  });
  assert.equal(reusedReview.statusCode, 409, reusedReview.body);
  assert.equal(reusedReview.json().code, 'MUTATION_ID_REUSED');

  assert.equal(getDb().prepare(`SELECT COUNT(*) AS count FROM agent_candidate_changes
    WHERE project_id = ?`).get(project.id)!.count, 0);
  assert.equal(getDb().prepare(`SELECT candidate_id FROM agent_candidate_workspaces
    WHERE project_id = ?`).get(project.id)!.candidate_id, null);
  const history = await artifacts.history(project.id);
  assert.equal(history[0].oid, review.json().currentRevision);
  assert.equal(history[0].cause, 'studio_deterministic_commit');
});

test('Studio Rule 确定性检查返回关联 Object 与 Operation 影响', async (t) => {
  const project = createProject({ owner: 'alice', name: 'Studio Rule 影响检查', runtime: 'agent' });
  const artifacts = getPackageArtifactStore();
  const initialized = await artifacts.initializeDraftPackage(project.id, {
    id: 'studio-rule-impact',
    name: 'Studio Rule 影响检查',
    description: '合成测试知识包。',
  });
  await artifacts.savePackageVersion(project.id, {
    expectedRevision: initialized.revision.oid,
    spec: {
      ...initialized.spec,
      objects: [{ key: 'record', title: '记录', description: '合成记录。', fields: [] }],
      operations: [{
        key: 'read_record', title: '读取记录', description: '读取合成记录。', inputs: [], outputs: [],
        flow: ['读取 object:record 并执行 rule:record_valid。'], relatedObjects: ['record'], usesRules: ['record_valid'],
      }],
      rules: [{
        key: 'record_valid', title: '记录有效规则', description: '判断合成记录是否有效。',
        content: 'record_id 非空时记录有效。', appliesToObjects: ['record'], appliesToOperations: ['read_record'],
        evidence: ['material:synthetic_source.md#记录有效规则'],
      }],
    },
    coverage: initialized.coverage,
    action: { cause: 'studio_rule_impact_seed', summary: '建立 Rule 影响基线', actorKind: 'system', actorName: '测试系统' },
  });
  const app = appFor(t);
  const initial = await app.inject({ method: 'GET', url: `/api/projects/${project.id}/studio/elements` });
  const patched = await app.inject({
    method: 'PATCH',
    url: `/api/projects/${project.id}/studio/elements/${encodeURIComponent('rule:record_valid')}`,
    payload: {
      expectedRevision: initial.json().workingRevision,
      clientMutationId: 'patch-rule-title-for-impact',
      changes: { title: '合成记录有效规则' },
    },
  });
  assert.equal(patched.statusCode, 200, patched.body);
  const reviewed = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/review`,
    payload: {
      expectedRevision: patched.json().workingRevision,
      clientMutationId: 'review-rule-impact-deterministically',
    },
  });
  assert.equal(reviewed.statusCode, 200, reviewed.body);
  assert.deepEqual(reviewed.json().impacts.changedElementIds, ['rule:record_valid']);
  assert.deepEqual(reviewed.json().impacts.affectedElementIds, ['object:record', 'operation:read_record']);
  assert.equal(reviewed.json().impacts.relationCount, 3);
  assert.equal(getDb().prepare(`SELECT COUNT(*) AS count FROM agent_project_event_outbox
    WHERE project_id = ? AND event_type = 'studio_review'`).get(project.id)!.count, 0);
});

test('Studio Operation 影响与删除预览包含绑定 Script', async (t) => {
  const project = createProject({ owner: 'alice', name: 'Studio Script 影响检查', runtime: 'agent' });
  const artifacts = getPackageArtifactStore();
  const initialized = await artifacts.initializeDraftPackage(project.id, {
    id: 'studio-script-impact',
    name: 'Studio Script 影响检查',
    description: '合成测试知识包。',
  });
  await artifacts.savePackageVersion(project.id, {
    expectedRevision: initialized.revision.oid,
    spec: {
      ...initialized.spec,
      objects: [{ key: 'record', title: '记录', description: '合成记录。', fields: [] }],
      operations: [{
        key: 'read_record', title: '读取记录', description: '读取合成记录。', inputs: [], outputs: [],
        flow: ['读取 object:record。'], relatedObjects: ['record'],
        script: { key: 'read_record_script', required: true },
      }],
      scripts: [{
        key: 'read_record_script', title: '读取记录脚本', description: '读取合成记录。',
        boundOperation: 'read_record', runtime: 'python', entrypoint: 'read_record.py', timeoutSeconds: 30,
        parameters: [{ name: 'record_id', flag: '--record-id', type: 'string', required: true }],
        outputs: [{ key: 'result', type: 'string' }], boundaries: ['只处理合成记录。'],
        sourceCode: [
          'import argparse',
          'import json',
          'parser = argparse.ArgumentParser()',
          'parser.add_argument("--record-id", required=True)',
          'args = parser.parse_args()',
          'print(json.dumps({"result": args.record_id}))',
        ].join('\n'),
      }],
    },
    coverage: initialized.coverage,
    action: { cause: 'studio_script_impact_seed', summary: '建立 Script 影响基线', actorKind: 'system', actorName: '测试系统' },
  });
  const app = appFor(t);
  const initial = await app.inject({ method: 'GET', url: `/api/projects/${project.id}/studio/elements` });
  const patched = await app.inject({
    method: 'PATCH',
    url: `/api/projects/${project.id}/studio/elements/${encodeURIComponent('operation:read_record')}`,
    payload: {
      expectedRevision: initial.json().workingRevision,
      clientMutationId: 'patch-script-bound-operation',
      changes: { title: '读取合成记录' },
    },
  });
  assert.equal(patched.statusCode, 200, patched.body);
  const reviewed = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/review`,
    payload: {
      expectedRevision: patched.json().workingRevision,
      clientMutationId: 'review-script-bound-operation',
    },
  });
  assert.equal(reviewed.statusCode, 200, reviewed.body);
  assert.ok(reviewed.json().impacts.affectedElementIds.includes('script:read_record_script'));

  const current = await app.inject({ method: 'GET', url: `/api/projects/${project.id}/studio/elements` });
  const preview = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/deletions/preview`,
    payload: {
      expectedRevision: current.json().workingRevision,
      elementIds: ['operation:read_record'],
    },
  });
  assert.equal(preview.statusCode, 200, preview.body);
  assert.equal(preview.json().blocked, true);
  assert.ok(preview.json().textReferences.some((reference: any) => (
    reference.sourceId === 'script:read_record_script'
    && reference.field === 'boundOperation'
    && reference.targetId === 'operation:read_record'
  )));
});

test('Studio 允许在存在历史悬空引用时创建无关条目并返回非阻断告警', async (t) => {
  const project = createProject({ owner: 'alice', name: 'Studio 存量问题兼容', runtime: 'agent' });
  const app = appFor(t);
  const legacy = await seedLegacyDanglingPackage(project.id);

  const created = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/elements`,
    payload: {
      expectedRevision: legacy.revision.oid,
      clientMutationId: 'create-unrelated-with-legacy-problem',
      kind: 'object',
      key: 'warehouse',
      values: {
        title: '仓库',
        description: '仓库主数据。',
      },
    },
  });

  assert.equal(created.statusCode, 201, created.body);
  assert.equal(created.json().element.id, 'object:warehouse');
  assert.equal(created.json().historicalProblemCount, 1);
  assert.deepEqual(created.json().warnings, [
    '规则 legacy-policy 引用了不存在的对象 retired-customer',
  ]);
  assert.match(created.json().display.text, /本次未增加历史问题/u);
  assert.equal(getDb().prepare(`SELECT COUNT(*) AS count FROM agent_candidate_changes
    WHERE project_id = ?`).get(project.id)!.count > 0, true);
});

test('Studio 在存量问题基础上新增完整性缺陷时仍阻断且返回明确错误', async (t) => {
  const project = createProject({ owner: 'alice', name: 'Studio 新增问题阻断', runtime: 'agent' });
  const app = appFor(t);
  const legacy = await seedLegacyDanglingPackage(project.id);

  const invalid = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/elements`,
    payload: {
      expectedRevision: legacy.revision.oid,
      clientMutationId: 'create-conflicting-alias-with-legacy-problem',
      kind: 'object',
      key: 'warehouse',
      values: {
        title: '仓库',
        description: '仓库主数据。',
        aliases: ['customer'],
      },
    },
  });

  assert.equal(invalid.statusCode, 422, invalid.body);
  assert.equal(invalid.json().code, 'STUDIO_FIELD_INVALID');
  assert.match(invalid.json().fieldErrors._batch, /本次修改新增或恶化/u);
  assert.match(invalid.json().fieldErrors._batch, /对象别名.*customer.*冲突/u);
  assert.equal(getDb().prepare(`SELECT COUNT(*) AS count FROM agent_candidate_changes
    WHERE project_id = ?`).get(project.id)!.count, 0);
});

test('Studio 批量创建支持同批关系，批量编辑逐字段约束且失败不产生部分修改', async (t) => {
  const project = createProject({ owner: 'alice', name: 'Studio 批量写入', runtime: 'agent' });
  const material = insertDocument({
    projectId: project.id,
    filename: '库存管理规定.md',
    kind: 'markdown',
    parseStatus: 'parsed',
    textContent: '# 库存规则\n安全库存和锁定库存的管理规定。',
  });
  const evidence = [`material:${material.sourceKey}#库存规则`];
  const app = appFor(t);
  const initial = await app.inject({ method: 'GET', url: `/api/projects/${project.id}/studio/elements` });
  const baseRevision = initial.json().workingRevision as string;

  const invalid = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/batch`,
    payload: {
      expectedRevision: baseRevision,
      clientMutationId: 'batch-invalid-1',
      creates: [
        { kind: 'object', key: 'inventory', values: { title: '库存' } },
        {
          kind: 'rule',
          key: 'safety-stock',
          values: {
            title: '安全库存',
            evidence,
            relations: [{ kind: 'applies_to_object', targetId: 'object:missing' }],
          },
        },
      ],
    },
  });
  assert.equal(invalid.statusCode, 422, invalid.body);
  assert.equal(invalid.json().code, 'STUDIO_FIELD_INVALID');
  assert.ok(invalid.json().fieldErrors['creates[1].values.relations[0].targetId']);
  assert.equal(getDb().prepare(`SELECT COUNT(*) AS count FROM agent_candidate_changes
    WHERE project_id = ?`).get(project.id)!.count, 0);

  const created = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/batch`,
    payload: {
      expectedRevision: baseRevision,
      clientMutationId: 'batch-create-1',
      editSessionId: 'batch-session-1',
      creates: [
        { kind: 'object', key: 'inventory', values: { title: '库存', description: '库存对象。' } },
        {
          kind: 'rule',
          key: 'safety-stock',
          values: {
            title: '安全库存',
            content: '低于安全库存时补货。',
            evidence,
            relations: [{ kind: 'applies_to_object', targetId: 'object:inventory' }],
          },
        },
        {
          kind: 'rule',
          key: 'frozen-stock',
          values: { title: '锁定库存', content: '锁定库存不得用于承诺。', evidence },
        },
      ],
    },
  });
  assert.equal(created.statusCode, 200, created.body);
  assert.equal(created.json().elements.length, 3);
  const rule = created.json().elements.find((item: any) => item.id === 'rule:safety-stock');
  assert.deepEqual(rule.relations.map((relation: any) => relation.targetId), ['object:inventory']);
  const groupRows = getDb().prepare(`SELECT DISTINCT group_id FROM agent_candidate_changes
    WHERE project_id = ? AND group_id <> 'compiled_resources'`).all(project.id) as Array<{ group_id: string }>;
  assert.deepEqual(groupRows.map((row) => row.group_id), [created.json().changeGroup.id]);

  const evidencePatch = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/batch`,
    payload: {
      expectedRevision: created.json().workingRevision,
      clientMutationId: 'batch-patch-evidence-invalid',
      patches: [{
        elementId: 'rule:safety-stock',
        changes: { evidence: [`material:${material.sourceKey}#其他位置`] },
      }],
    },
  });
  assert.equal(evidencePatch.statusCode, 422, evidencePatch.body);
  assert.equal(
    evidencePatch.json().fieldErrors['patches[0].changes.evidence'],
    '该字段由建模流程维护，不能在 Studio 中直接编辑',
  );

  const disallowed = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/batch`,
    payload: {
      expectedRevision: created.json().workingRevision,
      clientMutationId: 'batch-patch-invalid-1',
      patches: [
        { elementId: 'rule:safety-stock', changes: { content: '错误覆盖' } },
        { elementId: 'rule:frozen-stock', changes: { description: '可以修改' } },
      ],
    },
  });
  assert.equal(disallowed.statusCode, 422, disallowed.body);
  assert.equal(disallowed.json().fieldErrors['patches[0].changes.content'], '该字段不支持批量编辑');

  const afterRejected = await app.inject({
    method: 'GET',
    url: `/api/projects/${project.id}/studio/elements/${encodeURIComponent('rule:safety-stock')}`,
  });
  assert.equal(afterRejected.json().workingRevision, created.json().workingRevision);
  assert.equal(afterRejected.json().element.values.content, '低于安全库存时补货。');

  const patched = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/batch`,
    payload: {
      expectedRevision: created.json().workingRevision,
      clientMutationId: 'batch-patch-1',
      patches: [
        { elementId: 'rule:safety-stock', changes: { description: '用于判断是否需要补货。' } },
        { elementId: 'rule:frozen-stock', changes: { description: '用于判断库存是否可承诺。' } },
      ],
    },
  });
  assert.equal(patched.statusCode, 200, patched.body);
  assert.equal(patched.json().elements.length, 2);
  assert.ok(patched.json().elements.every((item: any) => item.values.description.includes('判断')));
});

test('Studio 删除先预览真实引用，并原子完成跨类型删除与关系清理', async (t) => {
  const project = createProject({ owner: 'alice', name: 'Studio 原子删除', runtime: 'agent' });
  const artifacts = getPackageArtifactStore();
  const initialized = await artifacts.initializeDraftPackage(project.id, {
    id: 'studio-delete-package',
    name: 'Studio 删除包',
    description: '删除验收基线。',
  });
  await artifacts.savePackageVersion(project.id, {
    expectedRevision: initialized.revision.oid,
    spec: {
      ...structuredClone(initialized.spec),
      objects: [{ key: 'inventory', title: '库存', description: '库存对象。', fields: [] }],
      rules: [
        {
          key: 'safety-stock', title: '安全库存', content: '低于安全库存时补货。',
          appliesToObjects: ['inventory'],
        },
        { key: 'obsolete-rule', title: '废弃规则', content: '这条规则需要删除。' },
      ],
    },
    coverage: initialized.coverage,
    note: '建立删除验收基线',
    action: {
      cause: 'studio_delete_seed', summary: '建立删除验收基线', actorKind: 'system', actorName: '测试系统',
    },
  });
  const app = appFor(t);
  const initial = await app.inject({ method: 'GET', url: `/api/projects/${project.id}/studio/elements` });

  const preview = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/deletions/preview`,
    payload: {
      expectedRevision: initial.json().workingRevision,
      elementIds: ['object:inventory', 'rule:obsolete-rule'],
    },
  });
  assert.equal(preview.statusCode, 200, preview.body);
  assert.equal(preview.json().cleanupRequired, true);
  assert.deepEqual(preview.json().references.map((item: any) => ({
    sourceId: item.sourceId,
    field: item.field,
    targetId: item.targetId,
  })), [{ sourceId: 'rule:safety-stock', field: '适用对象', targetId: 'object:inventory' }]);

  const removed = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/deletions/apply`,
    payload: {
      expectedRevision: preview.json().workingRevision,
      clientMutationId: 'delete-apply',
      editSessionId: 'delete-session',
      elementIds: preview.json().elementIds,
      previewToken: preview.json().previewToken,
      cleanupReferences: true,
    },
  });
  assert.equal(removed.statusCode, 200, removed.body);
  assert.deepEqual(removed.json().deletedElementIds, ['object:inventory', 'rule:obsolete-rule']);

  const list = await app.inject({ method: 'GET', url: `/api/projects/${project.id}/studio/elements` });
  assert.deepEqual(list.json().items.map((item: any) => item.id), ['rule:safety-stock']);
  const remaining = await app.inject({
    method: 'GET',
    url: `/api/projects/${project.id}/studio/elements/${encodeURIComponent('rule:safety-stock')}`,
  });
  assert.deepEqual(remaining.json().element.relations, []);
  const staged = getDb().prepare(`SELECT path, kind FROM agent_candidate_changes
    WHERE project_id = ? ORDER BY path`).all(project.id) as Array<{ path: string; kind: string }>;
  assert.ok(staged.some((item) => item.path === 'objects/inventory.md' && item.kind === 'delete'));
  assert.ok(staged.some((item) => item.path === 'rules/obsolete-rule.md' && item.kind === 'delete'));

  const applied = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/review`,
    payload: {
      expectedRevision: removed.json().workingRevision,
      clientMutationId: 'apply-delete-deterministically',
    },
  });
  assert.equal(applied.statusCode, 200, applied.body);
  assert.equal(applied.json().mode, 'deterministic');
  assert.deepEqual((await artifacts.current(project.id))!.spec.objects, []);
  assert.deepEqual((await artifacts.current(project.id))!.spec.rules?.map((rule) => rule.key), ['safety-stock']);
  assert.equal(getDb().prepare(`SELECT COUNT(*) AS count FROM agent_candidate_changes
    WHERE project_id = ?`).get(project.id)!.count, 0);
  assert.equal(getDb().prepare(`SELECT COUNT(*) AS count FROM agent_project_event_outbox
    WHERE project_id = ? AND event_type = 'studio_review'`).get(project.id)!.count, 0);
});

test('Studio 确定性直提交并发竞争最多一个成功', async (t) => {
  const project = createProject({ owner: 'alice', name: 'Studio 直提交并发', runtime: 'agent' });
  const app = appFor(t);
  const artifacts = getPackageArtifactStore();
  const initial = await app.inject({ method: 'GET', url: `/api/projects/${project.id}/studio/elements` });
  const formalBefore = (await artifacts.current(project.id))!.revision.oid;
  const created = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/elements`,
    payload: {
      expectedRevision: initial.json().workingRevision,
      clientMutationId: 'concurrent-direct-create',
      kind: 'object',
      key: 'record',
      values: { title: '记录', description: '用于直提交并发测试的合成记录。' },
    },
  });
  assert.equal(created.statusCode, 201, created.body);
  const [left, right] = await Promise.all([
    app.inject({
      method: 'POST', url: `/api/projects/${project.id}/studio/review`,
      payload: { expectedRevision: created.json().workingRevision, clientMutationId: 'concurrent-direct-left' },
    }),
    app.inject({
      method: 'POST', url: `/api/projects/${project.id}/studio/review`,
      payload: { expectedRevision: created.json().workingRevision, clientMutationId: 'concurrent-direct-right' },
    }),
  ]);
  assert.deepEqual([left.statusCode, right.statusCode].sort(), [200, 409]);
  const successful = [left, right].find((response) => response.statusCode === 200)!;
  assert.equal(successful.json().applied, true);
  assert.notEqual((await artifacts.current(project.id))!.revision.oid, formalBefore);
  assert.equal((await artifacts.history(project.id)).filter((revision) => (
    revision.cause === 'studio_deterministic_commit'
  )).length, 1);
});

test('Studio 删除预览绑定工作修订，过期确认整批零删除', async (t) => {
  const project = createProject({ owner: 'alice', name: 'Studio 删除冲突', runtime: 'agent' });
  const app = appFor(t);
  const initial = await app.inject({ method: 'GET', url: `/api/projects/${project.id}/studio/elements` });
  const created = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/elements`,
    payload: {
      expectedRevision: initial.json().workingRevision,
      clientMutationId: 'stale-delete-seed',
      kind: 'object',
      key: 'inventory',
      values: { title: '库存' },
    },
  });
  const preview = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/deletions/preview`,
    payload: { expectedRevision: created.json().workingRevision, elementIds: ['object:inventory'] },
  });
  const changed = await app.inject({
    method: 'PATCH',
    url: `/api/projects/${project.id}/studio/elements/${encodeURIComponent('object:inventory')}`,
    payload: {
      expectedRevision: created.json().workingRevision,
      clientMutationId: 'stale-delete-change',
      changes: { description: '刚刚更新的库存说明' },
    },
  });
  assert.equal(changed.statusCode, 200, changed.body);

  const stale = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/deletions/apply`,
    payload: {
      expectedRevision: preview.json().workingRevision,
      clientMutationId: 'stale-delete-apply',
      elementIds: ['object:inventory'],
      previewToken: preview.json().previewToken,
      cleanupReferences: true,
    },
  });
  assert.equal(stale.statusCode, 409, stale.body);
  assert.equal(stale.json().code, 'REVISION_CONFLICT');
  const detail = await app.inject({
    method: 'GET',
    url: `/api/projects/${project.id}/studio/elements/${encodeURIComponent('object:inventory')}`,
  });
  assert.equal(detail.statusCode, 200, detail.body);
  assert.equal(detail.json().element.values.description, '刚刚更新的库存说明');

  const freshPreview = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/deletions/preview`,
    payload: { expectedRevision: detail.json().workingRevision, elementIds: ['object:inventory'] },
  });
  const single = await app.inject({
    method: 'DELETE',
    url: `/api/projects/${project.id}/studio/elements/${encodeURIComponent('object:inventory')}`,
    payload: {
      expectedRevision: freshPreview.json().workingRevision,
      clientMutationId: 'single-delete-apply',
      previewToken: freshPreview.json().previewToken,
      cleanupReferences: true,
    },
  });
  assert.equal(single.statusCode, 200, single.body);
  assert.deepEqual(single.json().deletedElementIds, ['object:inventory']);
});

test('Studio 成员权限与写租约共用服务端事实，过期后首个编辑者原子接管', async (t) => {
  const project = createProject({ owner: 'alice', name: 'Studio 成员与租约', runtime: 'agent' });
  setProjectMember(project.id, 'alice', 'bob', 'viewer');
  setProjectMember(project.id, 'alice', 'carol', 'modeler');
  const ownerApp = appFor(t, 'alice');
  const viewerApp = appFor(t, 'bob');
  const modelerApp = appFor(t, 'carol');

  const initial = await ownerApp.inject({
    method: 'GET', url: `/api/projects/${project.id}/studio/elements`,
  });
  const created = await ownerApp.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/elements`,
    payload: {
      expectedRevision: initial.json().workingRevision,
      clientMutationId: 'lease-owner-create',
      kind: 'object',
      key: 'inventory',
      values: { title: '库存' },
    },
  });
  assert.equal(created.statusCode, 201, created.body);

  const viewerSchema = await viewerApp.inject({
    method: 'GET', url: `/api/projects/${project.id}/studio/schema`,
  });
  assert.equal(viewerSchema.statusCode, 200, viewerSchema.body);
  assert.equal(viewerSchema.json().access.role, 'viewer');
  assert.equal(viewerSchema.json().capabilities.update, false);
  assert.ok(viewerSchema.json().kinds.every((kind: any) => kind.capabilities.update === false));
  const viewerList = await viewerApp.inject({
    method: 'GET', url: `/api/projects/${project.id}/studio/elements`,
  });
  assert.equal(viewerList.statusCode, 200, viewerList.body);
  const viewerWrite = await viewerApp.inject({
    method: 'PATCH',
    url: `/api/projects/${project.id}/studio/elements/${encodeURIComponent('object:inventory')}`,
    payload: {
      expectedRevision: viewerList.json().workingRevision,
      clientMutationId: 'viewer-write-forbidden',
      changes: { description: '只读成员不应写入' },
    },
  });
  assert.equal(viewerWrite.statusCode, 404, viewerWrite.body);

  const lockedSchema = await modelerApp.inject({
    method: 'GET', url: `/api/projects/${project.id}/studio/schema`,
  });
  assert.equal(lockedSchema.statusCode, 200, lockedSchema.body);
  assert.equal(lockedSchema.json().access.lease.mode, 'readonly');
  assert.equal(lockedSchema.json().access.lease.owner, 'alice');
  assert.equal(lockedSchema.json().capabilities.update, false);
  const lockedWrite = await modelerApp.inject({
    method: 'PATCH',
    url: `/api/projects/${project.id}/studio/elements/${encodeURIComponent('object:inventory')}`,
    payload: {
      expectedRevision: created.json().workingRevision,
      clientMutationId: 'active-lease-write-forbidden',
      changes: { description: '有效租约期间不能接管' },
    },
  });
  assert.equal(lockedWrite.statusCode, 409, lockedWrite.body);
  assert.equal(lockedWrite.json().code, 'STUDIO_WRITE_LOCKED');

  const originalWorkspace = getDb().prepare(`SELECT owner, run_id FROM agent_candidate_workspaces
    WHERE project_id = ?`).get(project.id) as { owner: string; run_id: string };
  assert.equal(originalWorkspace.owner, 'alice');

  const groups = getDb().prepare(`SELECT DISTINCT group_id FROM agent_candidate_changes
    WHERE project_id = ? AND group_id <> 'compiled_resources' ORDER BY group_id`)
    .all(project.id) as Array<{ group_id: string }>;
  const preflight = await tool(ownerApp, project.id, originalWorkspace.run_id, 'request_confirmation_preflight', {
    summary: '确认租约接管前的 Studio 修改',
    change_groups: groups.map((row) => ({
      id: row.group_id,
      tier: 'structural',
      title: '确认 Studio 修改',
    })),
  });
  assert.equal(preflight.statusCode, 200, preflight.body);
  getDb().prepare(`UPDATE agent_candidate_workspaces
    SET lease_expires_at = ? WHERE project_id = ?`).run('2000-01-01T00:00:00.000Z', project.id);
  const reviewSchema = await modelerApp.inject({
    method: 'GET', url: `/api/projects/${project.id}/studio/schema`,
  });
  assert.equal(reviewSchema.statusCode, 200, reviewSchema.body);
  assert.equal(reviewSchema.json().access.lease.mode, 'review');
  assert.equal(reviewSchema.json().capabilities.update, false);
  assert.ok(reviewSchema.json().kinds.every((kind: any) => kind.capabilities.update === false));
  const ownerReviewSchema = await ownerApp.inject({
    method: 'GET', url: `/api/projects/${project.id}/studio/schema`,
  });
  assert.equal(ownerReviewSchema.json().access.lease.mode, 'review');
  assert.equal(ownerReviewSchema.json().capabilities.update, false);

  getDb().prepare(`UPDATE agent_candidate_workspaces
    SET candidate_id = NULL WHERE project_id = ?`).run(project.id);
  const takeoverSchema = await modelerApp.inject({
    method: 'GET', url: `/api/projects/${project.id}/studio/schema`,
  });
  assert.equal(takeoverSchema.statusCode, 200, takeoverSchema.body);
  assert.equal(takeoverSchema.json().access.lease.mode, 'takeover');
  assert.equal(takeoverSchema.json().capabilities.update, true);
  const takeover = await modelerApp.inject({
    method: 'PATCH',
    url: `/api/projects/${project.id}/studio/elements/${encodeURIComponent('object:inventory')}`,
    payload: {
      expectedRevision: created.json().workingRevision,
      clientMutationId: 'expired-lease-takeover',
      changes: { description: '由建模成员接管后继续编辑' },
    },
  });
  assert.equal(takeover.statusCode, 200, takeover.body);
  assert.equal(takeover.json().element.values.description, '由建模成员接管后继续编辑');
  const workspace = getDb().prepare(`SELECT owner, run_id FROM agent_candidate_workspaces
    WHERE project_id = ?`).get(project.id) as { owner: string; run_id: string };
  assert.equal(workspace.owner, 'carol');
  assert.equal(workspace.run_id, originalWorkspace.run_id);
});

test('Studio 确定性应用成功后立即释放写租约供其他 Modeler 接管', async (t) => {
  const project = createProject({ owner: 'alice', name: 'Studio 应用后释放租约', runtime: 'agent' });
  setProjectMember(project.id, 'alice', 'carol', 'modeler');
  const ownerApp = appFor(t, 'alice');
  const modelerApp = appFor(t, 'carol');
  const initial = await ownerApp.inject({ method: 'GET', url: `/api/projects/${project.id}/studio/elements` });
  const created = await ownerApp.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/elements`,
    payload: {
      expectedRevision: initial.json().workingRevision,
      clientMutationId: 'release-lease-create',
      kind: 'operation',
      key: 'inspect_record',
      values: {
        title: '检查记录',
        description: '检查合成记录。',
        flow: ['读取输入并返回结果。'],
      },
    },
  });
  assert.equal(created.statusCode, 201, created.body);
  const locked = await modelerApp.inject({ method: 'GET', url: `/api/projects/${project.id}/studio/schema` });
  assert.equal(locked.json().access.lease.mode, 'readonly');

  const applied = await ownerApp.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/review`,
    payload: {
      expectedRevision: created.json().workingRevision,
      clientMutationId: 'release-lease-apply',
    },
  });
  assert.equal(applied.statusCode, 200, applied.body);
  const available = await modelerApp.inject({ method: 'GET', url: `/api/projects/${project.id}/studio/schema` });
  assert.equal(available.json().access.lease.mode, 'takeover');
  assert.equal(available.json().capabilities.update, true);
  const takenOver = await modelerApp.inject({
    method: 'PATCH',
    url: `/api/projects/${project.id}/studio/elements/${encodeURIComponent('operation:inspect_record')}`,
    payload: {
      expectedRevision: applied.json().currentRevision,
      clientMutationId: 'release-lease-takeover',
      changes: { description: '由另一个建模成员继续检查合成记录。' },
    },
  });
  assert.equal(takenOver.statusCode, 200, takenOver.body);
  assert.equal(takenOver.json().element.values.description, '由另一个建模成员继续检查合成记录。');
});

test('Studio 新建 Object 带默认主键字段且字段可继续编辑', async (t) => {
  const project = createProject({ owner: 'alice', name: 'Studio Object 完整创建', runtime: 'agent' });
  const app = appFor(t);
  const initial = await app.inject({ method: 'GET', url: `/api/projects/${project.id}/studio/elements` });
  const created = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/elements`,
    payload: {
      expectedRevision: initial.json().workingRevision,
      clientMutationId: 'create-complete-object',
      kind: 'object',
      key: 'record',
      values: { title: '记录', description: '合成记录对象。' },
    },
  });
  assert.equal(created.statusCode, 201, created.body);
  assert.deepEqual(created.json().element.values.fields, [{
    key: 'id', cnName: '标识', type: 'string', required: true, note: 'Studio 新建对象的默认主键字段。',
  }]);

  const patched = await app.inject({
    method: 'PATCH',
    url: `/api/projects/${project.id}/studio/elements/${encodeURIComponent('object:record')}`,
    payload: {
      expectedRevision: created.json().workingRevision,
      clientMutationId: 'edit-created-object-fields',
      changes: {
        fields: [
          { key: 'id', cnName: '标识', type: 'string', required: true },
          { key: 'name', cnName: '名称', type: 'string', required: false },
        ],
      },
    },
  });
  assert.equal(patched.statusCode, 200, patched.body);
  assert.deepEqual(patched.json().element.values.fields.map((field: any) => field.key), ['id', 'name']);

  const selfRelation = await app.inject({
    method: 'PATCH',
    url: `/api/projects/${project.id}/studio/elements/${encodeURIComponent('object:record')}`,
    payload: {
      expectedRevision: patched.json().workingRevision,
      clientMutationId: 'reject-object-self-relation',
      changes: { relations: [{ kind: 'object_relation', targetId: 'object:record', label: '关联对象' }] },
    },
  });
  assert.equal(selfRelation.statusCode, 422, selfRelation.body);
  assert.equal(
    selfRelation.json().fieldErrors['changes.relations[0].targetId'],
    '不能把知识条目关联到自身',
  );
});

test('Studio 刷新可恢复工作草稿且确定性直提交幂等', async (t) => {
  const project = createProject({ owner: 'alice', name: 'Studio 复核恢复', runtime: 'agent' });
  const app = appFor(t);
  const initial = await app.inject({ method: 'GET', url: `/api/projects/${project.id}/studio/elements` });
  const created = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/elements`,
    payload: {
      expectedRevision: initial.json().workingRevision,
      clientMutationId: 'review-state-create',
      kind: 'object',
      key: 'record',
      values: { title: '记录', description: '合成记录对象。' },
    },
  });
  const staged = await app.inject({ method: 'GET', url: `/api/projects/${project.id}/studio/elements` });
  assert.equal(staged.json().hasStagedChanges, true);
  assert.equal(staged.json().reviewRequested, false);
  const snapshot = await app.inject({ method: 'GET', url: `/api/projects/${project.id}/agent/snapshot` });
  assert.equal(snapshot.statusCode, 200, snapshot.body);
  assert.equal(snapshot.json().studioPendingCount, 1);

  const first = await app.inject({
    method: 'POST', url: `/api/projects/${project.id}/studio/review`,
    payload: { expectedRevision: created.json().workingRevision, clientMutationId: 'review-state-first' },
  });
  assert.equal(first.statusCode, 200, first.body);
  assert.equal(first.json().mode, 'deterministic');
  assert.equal(first.json().applied, true);
  const second = await app.inject({
    method: 'POST', url: `/api/projects/${project.id}/studio/review`,
    payload: { expectedRevision: created.json().workingRevision, clientMutationId: 'review-state-first' },
  });
  assert.equal(second.statusCode, 200, second.body);
  assert.equal(second.json().replayed, true);
  const refreshed = await app.inject({ method: 'GET', url: `/api/projects/${project.id}/studio/elements` });
  assert.equal(refreshed.json().hasStagedChanges, false);
  assert.equal(refreshed.json().reviewRequested, false);
  const settledSnapshot = await app.inject({ method: 'GET', url: `/api/projects/${project.id}/agent/snapshot` });
  assert.equal(settledSnapshot.json().studioPendingCount, 0);
  assert.equal(getDb().prepare(`SELECT COUNT(*) AS count FROM agent_project_event_outbox
    WHERE project_id = ? AND event_type = 'studio_review'`).get(project.id)!.count, 0);
});

test('Studio 忽略内部工作记录并在后续业务修改应用时保留记录', async (t) => {
  const project = createProject({ owner: 'alice', name: 'Studio 合成工作记录', runtime: 'agent' });
  const app = appFor(t);
  const runId = `run-${project.id}`;
  const artifacts = getPackageArtifactStore();

  const initial = await app.inject({ method: 'GET', url: `/api/projects/${project.id}/studio/elements` });
  const plan = await tool(app, project.id, runId, 'update_plan', {
    markdown: '# Synthetic work plan\n\nTrack internal progress only.\n',
  });
  assert.equal(plan.statusCode, 200, plan.body);

  const internalOnly = await app.inject({ method: 'GET', url: `/api/projects/${project.id}/studio/elements` });
  assert.notEqual(internalOnly.json().workingRevision, initial.json().workingRevision);
  assert.equal(internalOnly.json().hasStagedChanges, false);
  const snapshot = await app.inject({ method: 'GET', url: `/api/projects/${project.id}/agent/snapshot` });
  assert.equal(snapshot.json().studioPendingCount, 0);

  const noOp = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/review`,
    payload: {
      expectedRevision: internalOnly.json().workingRevision,
      clientMutationId: 'internal-only-review',
    },
  });
  assert.equal(noOp.statusCode, 200, noOp.body);
  assert.equal(noOp.json().mode, 'noop');
  assert.equal(noOp.json().noOp, true);
  assert.equal(noOp.json().reviewRequested, false);
  assert.equal(getDb().prepare(`SELECT COUNT(*) AS count FROM agent_project_event_outbox
    WHERE project_id = ? AND event_type = 'studio_review'`).get(project.id)!.count, 0);

  const created = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/elements`,
    payload: {
      expectedRevision: internalOnly.json().workingRevision,
      clientMutationId: 'mixed-create-object',
      kind: 'object',
      key: 'synthetic_record',
      values: { title: 'Synthetic record', description: 'A fully synthetic record used for review tests.' },
    },
  });
  assert.equal(created.statusCode, 201, created.body);

  const mixed = await app.inject({ method: 'GET', url: `/api/projects/${project.id}/studio/elements` });
  assert.equal(mixed.json().hasStagedChanges, true);
  const applied = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/review`,
    payload: {
      expectedRevision: mixed.json().workingRevision,
      clientMutationId: 'mixed-review',
    },
  });
  assert.equal(applied.statusCode, 200, applied.body);
  assert.equal(applied.json().mode, 'deterministic');
  assert.equal(applied.json().applied, true);
  assert.ok(applied.json().changedPaths.every((path: string) => !path.startsWith('.modeling/')));

  const currentFiles = await artifacts.readPackageFiles(project.id);
  assert.match(currentFiles.files['.modeling/plan.md'], /Track internal progress only/u);
  assert.ok(currentFiles.files['objects/synthetic_record.md']);
  const settled = await app.inject({ method: 'GET', url: `/api/projects/${project.id}/agent/snapshot` });
  assert.equal(settled.json().studioPendingCount, 0);
});

test('Studio 确定性检查阻止新增格式错误并保留工作草稿', async (t) => {
  const project = createProject({ owner: 'alice', name: 'Studio 确定性检查', runtime: 'agent' });
  const app = appFor(t);
  const artifacts = getPackageArtifactStore();
  await seedRecordPackage(project.id);
  const initial = await app.inject({ method: 'GET', url: `/api/projects/${project.id}/studio/elements` });
  const formalBefore = (await artifacts.current(project.id))!.revision.oid;
  const patched = await app.inject({
    method: 'PATCH',
    url: `/api/projects/${project.id}/studio/elements/${encodeURIComponent('object:record')}`,
    payload: {
      expectedRevision: initial.json().workingRevision,
      clientMutationId: 'patch-format-check-object',
      changes: { description: '用于确定性格式检查的合成记录。' },
    },
  });
  assert.equal(patched.statusCode, 200, patched.body);
  const workspace = getDb().prepare(`SELECT run_id FROM agent_candidate_workspaces
    WHERE project_id = ?`).get(project.id) as { run_id: string };
  const corrupted = getDb().prepare(`UPDATE agent_candidate_changes SET kind = 'delete', content = NULL
    WHERE project_id = ? AND run_id = ? AND path = 'objects/record.md'`)
    .run(project.id, workspace.run_id);
  assert.equal(Number(corrupted.changes), 1);
  const current = await app.inject({ method: 'GET', url: `/api/projects/${project.id}/studio/elements` });
  assert.equal(current.statusCode, 200, current.body);

  const checked = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/review`,
    payload: {
      expectedRevision: current.json().workingRevision,
      clientMutationId: 'check-corrupted-studio-draft',
    },
  });
  assert.equal(checked.statusCode, 422, checked.body);
  assert.equal(checked.json().code, 'DETERMINISTIC_CHECK_FAILED');
  assert.match(checked.json().reason, /详情文件 .* 不存在/u);
  assert.equal((await artifacts.current(project.id))!.revision.oid, formalBefore);
  assert.ok(getDb().prepare(`SELECT COUNT(*) AS count FROM agent_candidate_changes
    WHERE project_id = ?`).get(project.id)!.count > 0);
  assert.equal(getDb().prepare(`SELECT candidate_id FROM agent_candidate_workspaces
    WHERE project_id = ?`).get(project.id)!.candidate_id, null);
  assert.equal(getDb().prepare(`SELECT COUNT(*) AS count FROM agent_project_event_outbox
    WHERE project_id = ? AND event_type = 'studio_review'`).get(project.id)!.count, 0);
});

test('Studio 删除会阻止仍被流程正文显式引用的知识条目', async (t) => {
  const project = createProject({ owner: 'alice', name: 'Studio 正文引用删除保护', runtime: 'agent' });
  const artifacts = getPackageArtifactStore();
  const initial = await artifacts.initializeDraftPackage(project.id, {
    id: 'studio-text-delete', name: 'Studio 正文引用删除保护', description: '合成测试。',
  });
  await artifacts.savePackageVersion(project.id, {
    expectedRevision: initial.revision.oid,
    spec: {
      ...initial.spec,
      objects: [{ key: 'record', title: '记录', description: '合成记录。', fields: [] }],
      operations: [{
        key: 'read_record', title: '读取记录', description: '读取记录。', inputs: [], outputs: [],
        flow: ['读取 object:record 并返回结果'], relatedObjects: [],
      }],
    },
    coverage: initial.coverage,
    action: { cause: 'studio_text_delete_seed', summary: '建立合成基线', actorKind: 'system', actorName: '测试系统' },
  });
  const app = appFor(t);
  const list = await app.inject({ method: 'GET', url: `/api/projects/${project.id}/studio/elements` });
  const preview = await app.inject({
    method: 'POST', url: `/api/projects/${project.id}/studio/deletions/preview`,
    payload: { expectedRevision: list.json().workingRevision, elementIds: ['object:record'] },
  });
  assert.equal(preview.statusCode, 200, preview.body);
  assert.equal(preview.json().blocked, true);
  assert.equal(preview.json().textReferences[0].sourceId, 'operation:read_record');
  assert.equal(preview.json().textReferences[0].field, 'flow[0]');

  const removed = await app.inject({
    method: 'POST', url: `/api/projects/${project.id}/studio/deletions/apply`,
    payload: {
      expectedRevision: preview.json().workingRevision,
      clientMutationId: 'blocked-text-delete',
      elementIds: ['object:record'],
      previewToken: preview.json().previewToken,
      cleanupReferences: true,
    },
  });
  assert.equal(removed.statusCode, 409, removed.body);
  assert.equal(removed.json().code, 'DELETE_TEXT_REFERENCE_BLOCKED');
});

test('Studio 确定性应用过滤已提交 mapping 的 no-op 草稿', async (t) => {
  const project = createProject({ owner: 'alice', name: 'Studio mapping no-op', runtime: 'agent' });
  const app = appFor(t);
  const artifacts = getPackageArtifactStore();
  await seedPhysicalMappingPackage(project.id);
  const baseline = (await artifacts.current(project.id))!;
  const baseFiles = (await artifacts.readPackageFiles(project.id, baseline.revision.oid)).files;

  const initial = await app.inject({ method: 'GET', url: `/api/projects/${project.id}/studio/elements` });
  assert.equal(initial.statusCode, 200, initial.body);
  const created = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/elements`,
    payload: {
      expectedRevision: initial.json().workingRevision,
      clientMutationId: 'create-parse-synthetic-batch',
      kind: 'operation',
      key: 'parse_synthetic_batch',
      values: {
        title: '解析合成批次',
        description: '解析合成批次结果。',
        flow: ['读取 object:synthetic_record 并返回解析结果。'],
      },
    },
  });
  assert.equal(created.statusCode, 201, created.body);

  const workspace = getDb().prepare(`SELECT run_id FROM agent_candidate_workspaces
    WHERE project_id = ?`).get(project.id) as { run_id: string };
  insertNoOpDraft(project.id, workspace.run_id, baseFiles);
  const current = await app.inject({ method: 'GET', url: `/api/projects/${project.id}/studio/elements` });
  const review = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/review`,
    payload: {
      expectedRevision: current.json().workingRevision,
      clientMutationId: 'review-mapping-noop',
    },
  });
  assert.equal(review.statusCode, 200, review.body);
  assert.equal(review.json().mode, 'deterministic');
  assert.deepEqual(review.json().elementIds, ['operation:parse_synthetic_batch']);
  assert.equal(review.json().changedPaths.includes('mappings/object-data-mappings.json'), false);
  assert.equal(review.json().changedPaths.includes('mappings/not-in-base.json'), false);
  assert.equal((await artifacts.current(project.id))!.revision.oid, review.json().currentRevision);
  assert.equal(getDb().prepare(`SELECT COUNT(*) AS count FROM agent_candidate_changes
    WHERE project_id = ?`).get(project.id)!.count, 0);
});

test('Studio Review 清理全部 no-op 草稿后返回 NO_CHANGES 且不创建候选', async (t) => {
  const project = createProject({ owner: 'alice', name: 'Studio all-noop review', runtime: 'agent' });
  const app = appFor(t);
  const artifacts = getPackageArtifactStore();
  await seedPhysicalMappingPackage(project.id);
  const baseline = (await artifacts.current(project.id))!;
  const baseFiles = (await artifacts.readPackageFiles(project.id, baseline.revision.oid)).files;

  const initial = await app.inject({ method: 'GET', url: `/api/projects/${project.id}/studio/elements` });
  const created = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/elements`,
    payload: {
      expectedRevision: initial.json().workingRevision,
      clientMutationId: 'create-all-noop-operation',
      kind: 'operation',
      key: 'temporary_operation',
      values: { title: '临时操作', description: '仅用于建立工作区。', flow: ['返回空结果。'] },
    },
  });
  assert.equal(created.statusCode, 201, created.body);

  const workspace = getDb().prepare(`SELECT run_id FROM agent_candidate_workspaces
    WHERE project_id = ?`).get(project.id) as { run_id: string };
  getDb().prepare(`DELETE FROM agent_candidate_changes WHERE project_id = ?`).run(project.id);
  insertNoOpDraft(project.id, workspace.run_id, baseFiles);

  const current = await app.inject({ method: 'GET', url: `/api/projects/${project.id}/studio/elements` });
  const before = (await artifacts.current(project.id))!.revision.oid;
  const review = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/review`,
    payload: {
      expectedRevision: current.json().workingRevision,
      clientMutationId: 'review-all-noop',
    },
  });
  assert.equal(review.statusCode, 400, review.body);
  assert.equal(review.json().code, 'NO_CHANGES');
  assert.deepEqual(review.json().removedPaths, [
    'mappings/not-in-base.json',
    'mappings/object-data-mappings.json',
  ]);
  assert.equal(getDb().prepare(`SELECT COUNT(*) AS count FROM agent_candidate_changes
    WHERE project_id = ?`).get(project.id)!.count, 0);
  assert.equal(getDb().prepare(`SELECT COUNT(*) AS count FROM artifact_candidate_revisions
    WHERE project_id = ?`).get(project.id)!.count, 0);
  assert.equal((await artifacts.current(project.id))!.revision.oid, before);

  const replay = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/review`,
    payload: {
      expectedRevision: current.json().workingRevision,
      clientMutationId: 'review-all-noop',
    },
  });
  assert.equal(replay.statusCode, 400, replay.body);
  assert.equal(replay.json().code, 'NO_CHANGES');
});

test('Studio 复核失败时不删除 no-op 草稿行', async (t) => {
  const project = createProject({ owner: 'alice', name: 'Studio failed review keeps noops', runtime: 'agent' });
  const app = appFor(t);
  const artifacts = getPackageArtifactStore();
  await seedPhysicalMappingPackage(project.id);
  const baseline = (await artifacts.current(project.id))!;
  const baseFiles = (await artifacts.readPackageFiles(project.id, baseline.revision.oid)).files;

  const initial = await app.inject({ method: 'GET', url: `/api/projects/${project.id}/studio/elements` });
  const created = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/elements`,
    payload: {
      expectedRevision: initial.json().workingRevision,
      clientMutationId: 'create-failed-review-operation',
      kind: 'operation',
      key: 'blocked_operation',
      values: { title: '待复核操作', description: '构造复核失败场景。', flow: ['返回空结果。'] },
    },
  });
  assert.equal(created.statusCode, 201, created.body);

  const workspace = getDb().prepare(`SELECT run_id FROM agent_candidate_workspaces
    WHERE project_id = ?`).get(project.id) as { run_id: string };
  const insert = getDb().prepare(`INSERT INTO agent_candidate_changes (
    project_id, run_id, path, kind, content, group_id, evidence_json, reason, updated_at
  ) VALUES (?,?,?,?,?,?,'[]',?,?)`);
  insert.run(project.id, workspace.run_id, 'scripts/untested_extra.py', 'write',
    'def main():\n    pass\n', 'unverified-script', '未测脚本草稿', '2026-09-03T00:00:00.000Z');
  insert.run(project.id, workspace.run_id, 'mappings/object-data-mappings.json', 'write',
    baseFiles['mappings/object-data-mappings.json'], 'legacy-noop', '残留 mapping no-op 草稿', '2026-09-03T00:00:00.000Z');

  const current = await app.inject({ method: 'GET', url: `/api/projects/${project.id}/studio/elements` });
  const review = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/review`,
    payload: {
      expectedRevision: current.json().workingRevision,
      clientMutationId: 'review-blocked-script',
    },
  });
  assert.equal(review.statusCode, 409, review.body);
  assert.equal(review.json().code, 'SCRIPT_TEST_REQUIRED');
  assert.ok(getDb().prepare(`SELECT 1 FROM agent_candidate_changes
    WHERE project_id = ? AND path = ?`).get(project.id, 'mappings/object-data-mappings.json'));
  assert.ok(getDb().prepare(`SELECT 1 FROM agent_candidate_changes
    WHERE project_id = ? AND path = ?`).get(project.id, 'scripts/untested_extra.py'));
});

test('Studio no-op 元素草稿不改变确定性判定且提交有效改动', async (t) => {
  const project = createProject({ owner: 'alice', name: 'Studio noop element keeps deterministic', runtime: 'agent' });
  const app = appFor(t);
  const artifacts = getPackageArtifactStore();
  await seedPhysicalMappingPackage(project.id);
  const baseline = (await artifacts.current(project.id))!;
  const baseFiles = (await artifacts.readPackageFiles(project.id, baseline.revision.oid)).files;

  const initial = await app.inject({ method: 'GET', url: `/api/projects/${project.id}/studio/elements` });
  const created = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/elements`,
    payload: {
      expectedRevision: initial.json().workingRevision,
      clientMutationId: 'create-deterministic-seed-operation',
      kind: 'operation',
      key: 'seed_operation',
      values: { title: '种子操作', description: '仅用于建立工作区。', flow: ['返回空结果。'] },
    },
  });
  assert.equal(created.statusCode, 201, created.body);

  const workspace = getDb().prepare(`SELECT run_id FROM agent_candidate_workspaces
    WHERE project_id = ?`).get(project.id) as { run_id: string };
  getDb().prepare(`DELETE FROM agent_candidate_changes WHERE project_id = ?`).run(project.id);
  const objectContent = baseFiles['objects/synthetic_record.md'];
  assert.ok(objectContent);
  const lineage = baseFiles['mappings/object-data-mappings.json'];
  assert.ok(lineage);
  const mappingContent = `${lineage}\n`;
  const insert = getDb().prepare(`INSERT INTO agent_candidate_changes (
    project_id, run_id, path, kind, content, group_id, evidence_json, reason, updated_at
  ) VALUES (?,?,?,?,?,?,'[]',?,?)`);
  insert.run(project.id, workspace.run_id, 'objects/synthetic_record.md', 'write', objectContent,
    'legacy-noop', '残留 object no-op 草稿', '2026-09-03T00:00:00.000Z');
  insert.run(project.id, workspace.run_id, 'mappings/object-data-mappings.json', 'write', mappingContent,
    'real-mapping', '真实 mapping 草稿', '2026-09-03T00:00:00.000Z');

  const current = await app.inject({ method: 'GET', url: `/api/projects/${project.id}/studio/elements` });
  const review = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/review`,
    payload: {
      expectedRevision: current.json().workingRevision,
      clientMutationId: 'review-element-noop',
    },
  });
  assert.equal(review.statusCode, 200, review.body);
  assert.equal(review.json().mode, 'deterministic');
  assert.deepEqual(review.json().elementIds, ['object:synthetic_record']);
  assert.equal(review.json().changedPaths.includes('objects/synthetic_record.md'), false);
  assert.equal(review.json().changedPaths.includes('mappings/object-data-mappings.json'), true);
  assert.equal(getDb().prepare(`SELECT COUNT(*) AS count FROM agent_candidate_changes
    WHERE project_id = ?`).get(project.id)!.count, 0);
  assert.notEqual((await artifacts.current(project.id))!.revision.oid, baseline.revision.oid);
});
