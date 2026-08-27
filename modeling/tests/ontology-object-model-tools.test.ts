import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

import Fastify from 'fastify';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'modeling-ontology-object-tools-'));

const [
  { registerAgentCoreRoutes },
  { createProject },
  { getPackageArtifactStore },
  { insertDocument },
] = await Promise.all([
  import('../src/agent/core-routes.js'),
  import('../src/projects/repo.js'),
  import('../src/artifacts/runtime.js'),
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
  name: string,
  args: Record<string, unknown> = {},
  runId = `run-ontology-object-tools-${projectId}`,
) {
  return app.inject({
    method: 'POST',
    url: `/internal/agent/tools/${name}`,
    headers: {
      'x-modeling-project': projectId,
      'x-modeling-run': runId,
      authorization: 'Bearer ontology-object-tool-test',
    },
    payload: { arguments: args },
  });
}

function confirmationReceipt(preflight: { json(): any }) {
  const payload = preflight.json();
  return {
    expected_candidate_revision: payload.candidate_revision,
    expected_candidate_hash: payload.validation_receipt.candidateHash,
    expected_validator_version: payload.validation_receipt.validatorVersion,
  };
}

async function seedProject(projectId: string) {
  insertDocument({
    projectId,
    filename: 'customer.xlsx',
    kind: 'excel',
    parseStatus: 'parsed',
    tables: [{
      name: 'ods_customer',
      rowCount: 1,
      fields: [
        { name: 'customer_id', inferredType: 'string', sampleValues: ['C001'], distinctRatio: 1 },
        { name: 'customer_name', inferredType: 'string', sampleValues: ['张三'], distinctRatio: 1 },
        { name: 'customer_level', inferredType: 'string', sampleValues: ['A'], distinctRatio: 0.4 },
      ],
    }],
  });

  const artifacts = getPackageArtifactStore();
  const initialized = await artifacts.initializeDraftPackage(projectId, {
    id: `ontology-object-tools-${projectId}`,
    name: '对象模型工具测试',
    description: '对象模型工具测试基线',
  });
  return artifacts.savePackageVersion(projectId, {
    expectedRevision: initialized.revision.oid,
    spec: {
      ...structuredClone(initialized.spec),
      objects: [
        {
          key: 'customer',
          title: '客户',
          description: '客户主数据对象。',
          aliases: ['客户主档'],
          queryBoundary: '只回答客户主数据。',
          sourceRefs: [{ kind: 'table', table: 'ods_customer' }],
          primaryKey: ['customer_id'],
          fields: [
            { key: 'customer_id', cnName: '客户ID', type: 'string', required: true, sourceColumn: 'customer_id' },
            { key: 'customer_name', cnName: '客户名称', type: 'string', sourceColumn: 'customer_name' },
          ],
          relations: [{ relation: '关联订单', target: 'order', note: '客户与订单关联。' }],
        },
        {
          key: 'order',
          title: '订单',
          description: '订单对象。',
          fields: [{ key: 'order_id', cnName: '订单ID', type: 'string', required: true }],
          primaryKey: ['order_id'],
        },
      ],
      operations: [],
    },
    coverage: initialized.coverage,
    note: '写入对象模型工具测试基线',
    action: {
      cause: 'ontology_object_tools_seeded',
      summary: '写入对象模型工具测试基线',
      actorKind: 'system',
      actorName: '测试系统',
    },
  });
}

test('对象模型工具支持列表、详情和主键保护校验', async (t) => {
  const project = createProject({ owner: 'alice', name: '对象模型列表校验', runtime: 'agent' });
  const app = appFor(t);
  await seedProject(project.id);

  const listed = await tool(app, project.id, 'list_ontology_objects');
  assert.equal(listed.statusCode, 200, listed.body);
  assert.equal(listed.json().total, 2);
  assert.deepEqual(
    listed.json().items.map((item: any) => item.object_id).sort(),
    ['object:customer', 'object:order'],
  );

  const workingRevision = listed.json().working_revision as string;
  const detail = await tool(app, project.id, 'get_ontology_object', {
    object_id: 'object:customer',
    working_revision: workingRevision,
  });
  assert.equal(detail.statusCode, 200, detail.body);
  assert.equal(detail.json().object.metadata.aliases[0], '客户主档');
  assert.equal(detail.json().object.relations[0].target_id, 'object:order');

  const invalid = await tool(app, project.id, 'validate_ontology_object_patch', {
    object_id: 'object:customer',
    expected_revision: workingRevision,
    field_ops: { remove: ['customer_id'] },
  });
  assert.equal(invalid.statusCode, 200, invalid.body);
  assert.equal(invalid.json().ok, false);
  assert.match(invalid.json().fieldErrors.field_ops, /主键字段 customer_id 不能被删除/u);
});

test('对象模型工具支持预校验、apply、review 和确认生效', async (t) => {
  const project = createProject({ owner: 'alice', name: '对象模型修改闭环', runtime: 'agent' });
  const app = appFor(t);
  const seeded = await seedProject(project.id);

  const listed = await tool(app, project.id, 'list_ontology_objects');
  assert.equal(listed.statusCode, 200, listed.body);
  const workingRevision = listed.json().working_revision as string;

  const current = await tool(app, project.id, 'get_ontology_object', {
    object_id: 'object:customer',
    working_revision: workingRevision,
  });
  assert.equal(current.statusCode, 200, current.body);
  const relationId = current.json().object.relations[0].id as string;

  const mutation = {
    object_id: 'object:customer',
    expected_revision: workingRevision,
    metadata: {
      aliases: ['客户主档', '客户实体'],
      queryBoundary: '只回答客户主数据和客户等级。',
    },
    field_ops: {
      add: [{ key: 'customer_level', cnName: '客户等级', type: 'string', sourceColumn: 'customer_level' }],
      update: [{ key: 'customer_name', changes: { alias: '名称', note: '客户展示名称。' } }],
    },
    relation_ops: {
      update: [{ relationId, changes: { label: '服务订单', description: '客户服务的订单集合。' } }],
    },
  };

  const validated = await tool(app, project.id, 'validate_ontology_object_patch', mutation);
  assert.equal(validated.statusCode, 200, validated.body);
  assert.equal(validated.json().ok, true);
  assert.deepEqual(validated.json().normalized_mutation.fieldOps.addKeys, ['customer_level']);
  assert.equal(validated.json().next_object.fields.length, 3);
  assert.equal(validated.json().next_object.relations[0].label, '服务订单');

  const applied = await tool(app, project.id, 'apply_ontology_object_patch', {
    ...mutation,
    client_mutation_id: 'ontology-object-patch-1',
    edit_session_id: 'ontology-object-session-1',
  });
  assert.equal(applied.statusCode, 200, applied.body);
  assert.equal(applied.json().candidate, true);
  assert.equal(applied.json().change_group.tier, 'structural');
  assert.deepEqual(applied.json().changed_paths, [
    'mappings/object-data-mappings.json',
    'objects/customer.md',
  ]);
  assert.equal(applied.json().impact_summary.mappingAffected, true);
  assert.match(applied.json().display.text, /待确认修改/u);

  const review = await tool(app, project.id, 'submit_ontology_object_review', {
    expected_revision: applied.json().working_revision,
    client_mutation_id: 'ontology-object-review-1',
  });
  assert.equal(review.statusCode, 200, review.body);
  assert.equal(review.json().review_requested, true);
  assert.equal(review.json().element_ids[0], 'object:customer');

  const prepared = await tool(app, project.id, 'prepare_confirmation_groups');
  assert.equal(prepared.statusCode, 200, prepared.body);
  assert.deepEqual(
    [...new Set(prepared.json().change_groups.flatMap((group: any) => group.impact ?? []))].sort(),
    ['mappings/object-data-mappings.json', 'objects/customer.md'],
  );

  const preflight = await tool(app, project.id, 'request_confirmation_preflight', {
    summary: '确认客户对象模型修改',
    change_groups: prepared.json().change_groups,
  });
  assert.equal(preflight.statusCode, 200, preflight.body);

  const confirmed = await tool(app, project.id, 'request_confirmation', {
    summary: '确认客户对象模型修改',
    change_groups: prepared.json().change_groups,
    ...confirmationReceipt(preflight),
    review: {
      approved: true,
      review_mode: 'full',
      selected_groups: prepared.json().change_groups.map((group: any) => group.id),
    },
  });
  assert.equal(confirmed.statusCode, 200, confirmed.body);
  assert.notEqual(confirmed.json().revision, seeded.revision.oid);

  const after = await tool(app, project.id, 'get_ontology_object', {
    object_id: 'object:customer',
  });
  assert.equal(after.statusCode, 200, after.body);
  assert.deepEqual(after.json().object.metadata.aliases, ['客户主档', '客户实体']);
  assert.equal(after.json().object.metadata.queryBoundary, '只回答客户主数据和客户等级。');
  assert.equal(after.json().object.fields.length, 3);
  assert.equal(after.json().object.fields[2].key, 'customer_level');
  assert.equal(after.json().object.relations[0].label, '服务订单');
});

test('对象模型工具支持查询 review 状态与审计记录', async (t) => {
  const project = createProject({ owner: 'alice', name: '对象模型状态审计', runtime: 'agent' });
  const app = appFor(t);
  await seedProject(project.id);

  const listed = await tool(app, project.id, 'list_ontology_objects');
  assert.equal(listed.statusCode, 200, listed.body);
  const workingRevision = listed.json().working_revision as string;

  const current = await tool(app, project.id, 'get_ontology_object', {
    object_id: 'object:customer',
    working_revision: workingRevision,
  });
  assert.equal(current.statusCode, 200, current.body);
  const relationId = current.json().object.relations[0].id as string;

  const mutation = {
    object_id: 'object:customer',
    expected_revision: workingRevision,
    metadata: {
      aliases: ['客户主档', '客户画像对象'],
    },
    field_ops: {
      add: [{ key: 'customer_level', cnName: '客户等级', type: 'string', sourceColumn: 'customer_level' }],
    },
    relation_ops: {
      update: [{ relationId, changes: { label: '客户订单' } }],
    },
  };

  const applied = await tool(app, project.id, 'apply_ontology_object_patch', {
    ...mutation,
    client_mutation_id: 'ontology-object-status-apply-1',
    edit_session_id: 'ontology-object-status-session-1',
  });
  assert.equal(applied.statusCode, 200, applied.body);

  const draftStatus = await tool(app, project.id, 'get_ontology_object_review_status', {
    object_id: 'object:customer',
  });
  assert.equal(draftStatus.statusCode, 200, draftStatus.body);
  assert.equal(draftStatus.json().stage, 'draft');
  assert.equal(draftStatus.json().current.change_groups[0].id, applied.json().change_group.id);
  assert.deepEqual(
    draftStatus.json().current.changed_paths,
    ['mappings/object-data-mappings.json', 'objects/customer.md'],
  );

  const review = await tool(app, project.id, 'submit_ontology_object_review', {
    expected_revision: applied.json().working_revision,
    client_mutation_id: 'ontology-object-status-review-1',
  });
  assert.equal(review.statusCode, 200, review.body);

  const reviewStatus = await tool(app, project.id, 'get_ontology_object_review_status', {
    object_id: 'object:customer',
  });
  assert.equal(reviewStatus.statusCode, 200, reviewStatus.body);
  assert.equal(reviewStatus.json().stage, 'review_requested');
  assert.equal(
    reviewStatus.json().current.review_request.client_mutation_id,
    'ontology-object-status-review-1',
  );

  const prepared = await tool(app, project.id, 'prepare_confirmation_groups');
  assert.equal(prepared.statusCode, 200, prepared.body);

  const preflight = await tool(app, project.id, 'request_confirmation_preflight', {
    summary: '确认客户对象模型修改',
    change_groups: prepared.json().change_groups,
  });
  assert.equal(preflight.statusCode, 200, preflight.body);

  const pendingStatus = await tool(app, project.id, 'get_ontology_object_review_status', {
    object_id: 'object:customer',
  });
  assert.equal(pendingStatus.statusCode, 200, pendingStatus.body);
  assert.equal(pendingStatus.json().stage, 'awaiting_confirmation');
  assert.equal(
    pendingStatus.json().current.candidate_revision,
    preflight.json().candidate_revision,
  );
  assert.equal(
    pendingStatus.json().current.validation_receipt.candidate_hash,
    preflight.json().validation_receipt.candidateHash,
  );

  const confirmed = await tool(app, project.id, 'request_confirmation', {
    summary: '确认客户对象模型修改',
    change_groups: prepared.json().change_groups,
    ...confirmationReceipt(preflight),
    review: {
      approved: true,
      review_mode: 'full',
      selected_groups: prepared.json().change_groups.map((group: any) => group.id),
    },
  });
  assert.equal(confirmed.statusCode, 200, confirmed.body);

  const acceptedStatus = await tool(app, project.id, 'get_ontology_object_review_status', {
    object_id: 'object:customer',
  });
  assert.equal(acceptedStatus.statusCode, 200, acceptedStatus.body);
  assert.equal(acceptedStatus.json().stage, 'accepted');
  assert.equal(acceptedStatus.json().latest_record.status, 'accepted');
  assert.equal(
    acceptedStatus.json().latest_record.accepted_revision,
    confirmed.json().revision,
  );

  const secondApply = await tool(app, project.id, 'apply_ontology_object_patch', {
    object_id: 'object:customer',
    expected_revision: confirmed.json().revision,
    metadata: {
      aliases: ['客户主档', '客户对象'],
    },
    client_mutation_id: 'ontology-object-status-apply-2',
    edit_session_id: 'ontology-object-status-session-2',
  });
  assert.equal(secondApply.statusCode, 200, secondApply.body);

  const secondPrepared = await tool(app, project.id, 'prepare_confirmation_groups');
  assert.equal(secondPrepared.statusCode, 200, secondPrepared.body);
  const secondPreflight = await tool(app, project.id, 'request_confirmation_preflight', {
    summary: '退回客户对象模型修改',
    change_groups: secondPrepared.json().change_groups,
  });
  assert.equal(secondPreflight.statusCode, 200, secondPreflight.body);

  const rejected = await tool(app, project.id, 'request_confirmation_rejected', {
    expected_candidate_revision: secondPreflight.json().candidate_revision,
    review: {
      approved: false,
      feedback: '需要补充字段说明',
    },
  });
  assert.equal(rejected.statusCode, 200, rejected.body);
  assert.equal(rejected.json().rejected, true);

  const auditFirstPage = await tool(app, project.id, 'list_ontology_object_audit_records', {
    object_id: 'object:customer',
    limit: 1,
  });
  assert.equal(auditFirstPage.statusCode, 200, auditFirstPage.body);
  assert.equal(auditFirstPage.json().items.length, 1);
  assert.equal(auditFirstPage.json().items[0].status, 'rejected');
  assert.ok(auditFirstPage.json().next_cursor);

  const auditSecondPage = await tool(app, project.id, 'list_ontology_object_audit_records', {
    object_id: 'object:customer',
    limit: 1,
    cursor: auditFirstPage.json().next_cursor,
  });
  assert.equal(auditSecondPage.statusCode, 200, auditSecondPage.body);
  assert.equal(auditSecondPage.json().items.length, 1);
  assert.equal(auditSecondPage.json().items[0].status, 'accepted');
  assert.equal(auditSecondPage.json().items[0].review.review_mode, 'full');
  assert.equal(auditSecondPage.json().items[0].groups[0].applied, true);

  const acceptedOnly = await tool(app, project.id, 'list_ontology_object_audit_records', {
    object_id: 'object:customer',
    status: 'accepted',
  });
  assert.equal(acceptedOnly.statusCode, 200, acceptedOnly.body);
  assert.equal(acceptedOnly.json().items.length, 1);
  assert.equal(acceptedOnly.json().items[0].status, 'accepted');
});
