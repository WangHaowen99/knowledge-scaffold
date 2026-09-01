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
});

test('Studio 单条创建和编辑只形成候选，CAS、幂等及人工确认后 Git 原子生效', async (t) => {
  const project = createProject({ owner: 'alice', name: 'Studio 写入', runtime: 'agent' });
  const app = appFor(t);
  const artifacts = getPackageArtifactStore();

  const schema = await app.inject({ method: 'GET', url: `/api/projects/${project.id}/studio/schema` });
  assert.equal(schema.statusCode, 200, schema.body);
  assert.deepEqual(schema.json().capabilities, {
    read: true, create: true, update: true, delete: true, batch: true,
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
  assert.match(created.json().display.text, /待确认修改/u);
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

  const reviewPayload = {
    expectedRevision: patchedAgain.json().workingRevision,
    clientMutationId: 'review-inventory-1',
  };
  const staleReview = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/review`,
    payload: { ...reviewPayload, expectedRevision: patched.json().workingRevision },
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
  assert.equal(review.statusCode, 202, review.body);
  assert.equal(review.json().reviewRequested, true);
  assert.deepEqual(review.json().elementIds, ['object:inventory']);
  assert.match(review.json().display.text, /整体复核/u);
  const studioEvent = getDb().prepare(`SELECT event_type, payload_json, delivered_at
    FROM agent_project_event_outbox WHERE id = ?`).get(
    `studio-review:${project.id}:review-inventory-1`,
  ) as { event_type: string; payload_json: string; delivered_at: string | null };
  assert.equal(studioEvent.event_type, 'studio_review');
  assert.equal(studioEvent.delivered_at, null);
  assert.deepEqual(JSON.parse(studioEvent.payload_json).elementIds, ['object:inventory']);

  const reviewReplay = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/review`,
    payload: reviewPayload,
  });
  assert.equal(reviewReplay.statusCode, 202, reviewReplay.body);
  assert.equal(reviewReplay.json().replayed, true);
  assert.equal(getDb().prepare(`SELECT COUNT(*) AS count FROM agent_project_event_outbox
    WHERE id = ?`).get(`studio-review:${project.id}:review-inventory-1`)!.count, 1);
  const reusedReview = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/review`,
    payload: { ...reviewPayload, expectedRevision: patched.json().workingRevision },
  });
  assert.equal(reusedReview.statusCode, 409, reusedReview.body);
  assert.equal(reusedReview.json().code, 'MUTATION_ID_REUSED');

  const workspace = getDb().prepare(`SELECT run_id FROM agent_candidate_workspaces
    WHERE project_id = ?`).get(project.id) as { run_id: string };
  const groups = getDb().prepare(`SELECT DISTINCT group_id FROM agent_candidate_changes
    WHERE project_id = ? AND group_id <> 'compiled_resources' ORDER BY group_id`)
    .all(project.id) as Array<{ group_id: string }>;
  const confirmationGroups = groups.map((row) => ({
    id: row.group_id,
    tier: 'structural',
    title: '确认 Studio 修改',
  }));
  const preflight = await tool(
    app,
    project.id,
    workspace.run_id,
    'request_confirmation_preflight',
    { summary: '确认 Studio 修改', change_groups: confirmationGroups },
  );
  assert.equal(preflight.statusCode, 200, preflight.body);
  const confirmed = await tool(
    app,
    project.id,
    workspace.run_id,
    'request_confirmation',
    {
      expected_candidate_revision: preflight.json().candidate_revision,
      expected_candidate_hash: preflight.json().validation_receipt.candidateHash,
      expected_validator_version: preflight.json().validation_receipt.validatorVersion,
      review: {
        approved: true,
        review_mode: 'full',
        selected_groups: confirmationGroups.map((group) => group.id),
      },
    },
  );
  assert.equal(confirmed.statusCode, 200, confirmed.body);
  assert.notEqual(confirmed.json().revision, gitBefore);
  assert.equal((await artifacts.current(project.id))!.revision.oid, confirmed.json().revision);
  const history = await artifacts.history(project.id);
  assert.equal(history[0].oid, confirmed.json().revision);
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
