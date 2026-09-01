import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'modeling-mcp-object-direct-'));

const [
  { createProject },
  { getPackageArtifactStore },
  { getDb },
  { insertDocument },
  { getAuthoringState },
  { createOrGetPackageDraft },
  { ObjectMcpService },
  { executeModelingMcpTool, toolNamesByToolset },
  { ModelingMcpToolError },
] = await Promise.all([
  import('../src/projects/repo.js'),
  import('../src/artifacts/runtime.js'),
  import('../src/db/index.js'),
  import('../src/documents/repo.js'),
  import('../src/authoring/state.js'),
  import('../src/workflow/package-draft.js'),
  import('../src/mcp/object/service.js'),
  import('../src/mcp/toolset.js'),
  import('../src/mcp/tool-error.js'),
]);

async function seedProject(projectId: string) {
  insertDocument({
    projectId,
    filename: 'synthetic_records.xlsx',
    kind: 'excel',
    parseStatus: 'parsed',
    tables: [{
      name: 'ods_customer',
      rowCount: 1,
      fields: [
        { name: 'customer_id', inferredType: 'string', sampleValues: ['record_0001'], distinctRatio: 1 },
        { name: 'customer_name', inferredType: 'string', sampleValues: ['person_0001'], distinctRatio: 1 },
        { name: 'customer_level', inferredType: 'string', sampleValues: ['A'], distinctRatio: 0.4 },
      ],
    }],
  });

  const artifacts = getPackageArtifactStore();
  const initialized = await artifacts.initializeDraftPackage(projectId, {
    id: `mcp-object-direct-${projectId}`,
    name: '对象直写测试',
    description: '对象直写测试基线',
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
          relations: [{ relation: '关联', target: 'order', note: '客户与订单关联。' }],
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
    note: '写入对象直写测试基线',
    action: {
      cause: 'mcp_object_direct_seeded',
      summary: '写入对象直写测试基线',
      actorKind: 'system',
      actorName: '测试系统',
    },
  });
}

async function call(project: ReturnType<typeof createProject>, toolName: string, args: Record<string, unknown>) {
  return executeModelingMcpTool({
    toolName,
    project,
    username: project.owner,
    tokenId: 'synthetic-object-direct-token',
    argumentsValue: args,
  });
}

function revisionEventsSince(projectId: string, afterSeq: number) {
  return getAuthoringState().replay(projectId, afterSeq).events.filter((event) => (
    event.type === 'package.revision.created'
  ));
}

async function setPackageStatus(projectId: string, status: 'draft' | 'review' | 'released') {
  const artifacts = getPackageArtifactStore();
  const current = await artifacts.readPackageFiles(projectId);
  return artifacts.savePackageVersion(projectId, {
    expectedRevision: current.revision.oid,
    spec: { ...current.spec, status },
    coverage: current.coverage,
    note: `切换知识包状态到 ${status}`,
    action: {
      cause: 'mcp_object_direct_status_seed',
      summary: `切换知识包状态到 ${status}`,
      actorKind: 'system',
      actorName: '测试系统',
    },
  });
}

function insertCandidateChange(projectId: string) {
  const now = new Date().toISOString();
  getDb().prepare(`INSERT INTO agent_candidate_changes (
    project_id, run_id, path, kind, content, group_id, evidence_json, reason, updated_at
  ) VALUES (?,?,?,'write',?,'mcp-object-test','[]',NULL,?)`)
    .run(projectId, `run-${projectId}`, 'objects/customer.md', '# synthetic candidate change', now);
}

function insertPendingCandidate(projectId: string, baseRevision: string) {
  const now = new Date().toISOString();
  getDb().prepare(`INSERT INTO artifact_candidate_revisions (
    id, project_id, base_revision_oid, status, cause, summary, actor_kind, actor_name,
    groups_json, selected_group_ids_json, created_at
  ) VALUES (?,?,?,?,?,?,?,?,?,?,?)`).run(
    `cr_${projectId}`,
    projectId,
    baseRevision,
    'pending',
    'mcp_object_direct_candidate_seed',
    'synthetic pending candidate',
    'user',
    '测试系统',
    '[]',
    '[]',
    now,
  );
}

function assertBusyValidation(result: Record<string, any>, target: string) {
  assert.equal(result.ok, false);
  assert.ok(result.issues.some((item: any) => item.code === 'PROJECT_EDITING_BUSY'));
  assert.ok(result.issues.some((item: any) => item.target === target));
}

test('Object MCP 工具集已收敛为 read + validate + direct update', () => {
  const tools = toolNamesByToolset();
  assert.deepEqual(tools.read, ['list_ontology_objects', 'get_ontology_object']);
  assert.deepEqual(tools.change, ['validate_ontology_object_change', 'update_ontology_object']);
});

test('Object MCP 支持正式 revision 读取、预校验和直接写入', async () => {
  const project = createProject({ owner: 'alice', name: 'Object MCP direct write', runtime: 'agent' });
  const seeded = await seedProject(project.id);

  const listed = await call(project, 'list_ontology_objects', {});
  assert.equal(listed.revision, seeded.revision.oid);
  assert.equal(listed.total, 2);
  assert.deepEqual(
    listed.items.map((item: any) => item.object_id).sort(),
    ['object:customer', 'object:order'],
  );

  const current = await call(project, 'get_ontology_object', {
    object_id: 'object:customer',
    revision: listed.revision,
  });
  const relationId = current.object.relations[0].id as string;

  const mutation = {
    object_id: 'object:customer',
    expected_revision: listed.revision,
    metadata: {
      aliases: ['客户主档', '客户实体'],
      queryBoundary: '只回答客户主数据和客户等级。',
    },
    field_ops: {
      add: [{ key: 'customer_level', cnName: '客户等级', type: 'string', sourceColumn: 'customer_level' }],
      update: [{ key: 'customer_name', changes: { alias: '名称', note: '客户展示名称。' } }],
    },
    relation_ops: {
      update: [{ relationId, changes: { label: '拥有', description: '客户服务的订单集合。' } }],
    },
  };

  const validated = await call(project, 'validate_ontology_object_change', mutation);
  assert.equal(validated.ok, true);
  assert.equal(validated.resource.id, 'object:customer');
  assert.equal(validated.next_object.fields.length, 3);
  assert.equal(validated.next_object.relations[0].label, '拥有');

  const beforeEventSeq = getAuthoringState().snapshot(project.id).lastEventSeq;
  const updated = await call(project, 'update_ontology_object', mutation);
  assert.equal(updated.resource.id, 'object:customer');
  assert.notEqual(updated.revision, seeded.revision.oid);
  assert.equal(updated.object.metadata.queryBoundary, '只回答客户主数据和客户等级。');
  assert.equal(updated.object.fields.length, 3);
  assert.equal(updated.object.relations[0].label, '拥有');
  assert.deepEqual(updated.affected_resources, [
    { type: 'mapping', key: 'object-data-mappings', relation: 'field_mapping' },
    { type: 'object', key: 'customer' },
    { type: 'object', key: 'order', relation: 'relation' },
  ]);
  const [revisionEvent] = revisionEventsSince(project.id, beforeEventSeq);
  assert.equal(revisionEvent?.type, 'package.revision.created');
  assert.equal(revisionEvent?.payload.revisionId, updated.revision);
  assert.equal(revisionEvent?.payload.cause, 'modeling_mcp_object_update');
  assert.equal(revisionEvent?.payload.packageVersion, seeded.packageVersion);
  assert.deepEqual(revisionEvent?.payload.resource, {
    type: 'object',
    id: 'object:customer',
    key: 'customer',
  });
  assert.equal(revisionEvent?.payload.source, 'mcp');

  const after = await call(project, 'get_ontology_object', {
    object_id: 'object:customer',
    revision: updated.revision,
  });
  assert.deepEqual(after.object.metadata.aliases, ['客户主档', '客户实体']);
  assert.equal(after.object.fields[2].key, 'customer_level');
  assert.equal(after.object.relations[0].label, '拥有');
});

test('Object MCP 细化返回主键删除、关系目标非法和 revision 冲突', async () => {
  const project = createProject({ owner: 'bob', name: 'Object MCP validation errors', runtime: 'agent' });
  const seeded = await seedProject(project.id);

  const primaryKeyInvalid = await call(project, 'validate_ontology_object_change', {
    object_id: 'object:customer',
    expected_revision: seeded.revision.oid,
    field_ops: { remove: ['customer_id'] },
  });
  assert.equal(primaryKeyInvalid.ok, false);
  assert.ok(primaryKeyInvalid.issues.some((item: any) => item.code === 'PRIMARY_KEY_REMOVED'));
  assert.match(primaryKeyInvalid.field_errors.field_ops, /主键字段 customer_id/u);

  await assert.rejects(
    () => call(project, 'update_ontology_object', {
      object_id: 'object:customer',
      expected_revision: seeded.revision.oid,
      field_ops: { remove: ['customer_id'] },
    }),
    (error: unknown) => {
      assert.ok(error instanceof ModelingMcpToolError);
      assert.equal(error.code, 'SUBMISSION_BLOCKED');
      assert.equal(error.details.resource.id, 'object:customer');
      assert.ok(Array.isArray(error.details.issues));
      assert.ok((error.details.issues as any[]).some((item) => item.code === 'PRIMARY_KEY_REMOVED'));
      return true;
    },
  );

  const relationInvalid = await call(project, 'validate_ontology_object_change', {
    object_id: 'object:customer',
    expected_revision: seeded.revision.oid,
    relation_ops: {
      add: [{ kind: 'object_relation', targetId: 'object:missing', label: '指向缺失对象' }],
    },
  });
  assert.equal(relationInvalid.ok, false);
  assert.ok(relationInvalid.issues.some((item: any) => item.code === 'RELATION_TARGET_INVALID'));

  const first = await call(project, 'update_ontology_object', {
    object_id: 'object:customer',
    expected_revision: seeded.revision.oid,
    metadata: { aliases: ['客户主档', '最新对象'] },
  });
  await assert.rejects(
    () => call(project, 'update_ontology_object', {
      object_id: 'object:customer',
      expected_revision: seeded.revision.oid,
      metadata: { aliases: ['客户主档', '过期对象'] },
    }),
    (error: unknown) => {
      assert.ok(error instanceof ModelingMcpToolError);
      assert.equal(error.code, 'REVISION_CONFLICT');
      assert.equal(error.details.reason_code, 'REVISION_CONFLICT');
      assert.equal(error.details.actual_revision, first.revision);
      return true;
    },
  );
});

test('Object MCP 在非 draft/review 知识包上阻止直写', async () => {
  const project = createProject({ owner: 'carol', name: 'Object MCP status gate', runtime: 'agent' });
  await seedProject(project.id);
  const released = await setPackageStatus(project.id, 'released');
  const mutation = {
    object_id: 'object:customer',
    expected_revision: released.revision.oid,
    metadata: { aliases: ['客户主档', '锁定版本'] },
  };

  const validated = await call(project, 'validate_ontology_object_change', mutation);
  assert.equal(validated.ok, false);
  assert.ok(validated.issues.some((item: any) => item.code === 'PACKAGE_NOT_EDITABLE'));
  assert.ok(validated.issues.some((item: any) => item.target === 'package.status'));

  await assert.rejects(
    () => call(project, 'update_ontology_object', mutation),
    (error: unknown) => {
      assert.ok(error instanceof ModelingMcpToolError);
      assert.equal(error.code, 'SUBMISSION_BLOCKED');
      assert.ok((error.details.issues as any[]).some((item) => item.code === 'PACKAGE_NOT_EDITABLE'));
      return true;
    },
  );
});

test('Object MCP 在人工草稿存在时阻止直写', async () => {
  const project = createProject({ owner: 'dave', name: 'Object MCP package draft gate', runtime: 'agent' });
  const seeded = await seedProject(project.id);
  await createOrGetPackageDraft(project.id, project.owner);
  const mutation = {
    object_id: 'object:customer',
    expected_revision: seeded.revision.oid,
    metadata: { aliases: ['客户主档', '草稿冲突'] },
  };

  const validated = await call(project, 'validate_ontology_object_change', mutation);
  assertBusyValidation(validated, 'package_draft');

  await assert.rejects(
    () => call(project, 'update_ontology_object', mutation),
    (error: unknown) => {
      assert.ok(error instanceof ModelingMcpToolError);
      assert.equal(error.code, 'SUBMISSION_BLOCKED');
      assert.ok((error.details.issues as any[]).some((item) => item.target === 'package_draft'));
      return true;
    },
  );
});

test('Object MCP 在 candidate/review 变更存在时阻止直写', async () => {
  const candidateChangeProject = createProject({
    owner: 'erin',
    name: 'Object MCP candidate change gate',
    runtime: 'agent',
  });
  const seededWithCandidateChange = await seedProject(candidateChangeProject.id);
  insertCandidateChange(candidateChangeProject.id);
  const mutation = {
    object_id: 'object:customer',
    expected_revision: seededWithCandidateChange.revision.oid,
    metadata: { aliases: ['客户主档', '候选冲突'] },
  };

  const validated = await call(candidateChangeProject, 'validate_ontology_object_change', mutation);
  assertBusyValidation(validated, 'candidate');

  await assert.rejects(
    () => call(candidateChangeProject, 'update_ontology_object', mutation),
    (error: unknown) => {
      assert.ok(error instanceof ModelingMcpToolError);
      assert.equal(error.code, 'SUBMISSION_BLOCKED');
      assert.ok((error.details.issues as any[]).some((item) => item.target === 'candidate'));
      return true;
    },
  );

  const pendingCandidateProject = createProject({
    owner: 'frank',
    name: 'Object MCP pending candidate gate',
    runtime: 'agent',
  });
  const seededWithPending = await seedProject(pendingCandidateProject.id);
  insertPendingCandidate(pendingCandidateProject.id, seededWithPending.revision.oid);

  const pendingValidated = await call(pendingCandidateProject, 'validate_ontology_object_change', {
    object_id: 'object:customer',
    expected_revision: seededWithPending.revision.oid,
    metadata: { aliases: ['客户主档', '待确认候选'] },
  });
  assertBusyValidation(pendingValidated, 'candidate');
});

test('Object MCP revision 事件发布失败时仍返回已提交 revision', async () => {
  const project = createProject({ owner: 'event_warning_object', name: 'Object MCP event warning', runtime: 'agent' });
  const seeded = await seedProject(project.id);
  const artifacts = getPackageArtifactStore();
  const service = new ObjectMcpService(getDb(), artifacts, () => {
    throw new Error('synthetic revision event failure');
  });
  const originalConsoleError = console.error;
  const loggedErrors: unknown[][] = [];
  console.error = (...args: unknown[]) => { loggedErrors.push(args); };

  try {
    const result = await service.execute('update_ontology_object', project, project.owner, {
      object_id: 'object:customer',
      expected_revision: seeded.revision.oid,
      metadata: { aliases: ['客户主档', '事件降级验证'] },
    }) as Record<string, any>;

    assert.notEqual(result.revision, seeded.revision.oid);
    assert.ok(result.warnings.some((item: any) => item.code === 'REVISION_EVENT_PUBLISH_FAILED'));
    assert.ok(result.warnings.some((item: any) => item.level === 'warning' && item.phase === 'persist'));
    const current = await artifacts.readPackageFiles(project.id);
    assert.equal(current.revision.oid, result.revision);
    assert.ok(current.spec.objects.find((item) => item.key === 'customer')?.aliases?.includes('事件降级验证'));
    assert.equal(loggedErrors.length, 1);
  } finally {
    console.error = originalConsoleError;
  }
});
