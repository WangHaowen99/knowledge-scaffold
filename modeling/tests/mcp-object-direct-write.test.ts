import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'modeling-mcp-object-direct-'));

const [
  { createProject },
  { getPackageArtifactStore },
  { insertDocument },
  { executeModelingMcpTool, toolNamesByToolset },
  { ModelingMcpToolError },
] = await Promise.all([
  import('../src/projects/repo.js'),
  import('../src/artifacts/runtime.js'),
  import('../src/documents/repo.js'),
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
      update: [{ relationId, changes: { label: '服务订单', description: '客户服务的订单集合。' } }],
    },
  };

  const validated = await call(project, 'validate_ontology_object_change', mutation);
  assert.equal(validated.ok, true);
  assert.equal(validated.resource.id, 'object:customer');
  assert.equal(validated.next_object.fields.length, 3);
  assert.equal(validated.next_object.relations[0].label, '服务订单');

  const updated = await call(project, 'update_ontology_object', mutation);
  assert.equal(updated.resource.id, 'object:customer');
  assert.notEqual(updated.revision, seeded.revision.oid);
  assert.equal(updated.object.metadata.queryBoundary, '只回答客户主数据和客户等级。');
  assert.equal(updated.object.fields.length, 3);
  assert.equal(updated.object.relations[0].label, '服务订单');
  assert.deepEqual(updated.affected_resources, [
    { type: 'mapping', key: 'object-data-mappings', relation: 'field_mapping' },
    { type: 'object', key: 'customer' },
    { type: 'object', key: 'order', relation: 'relation' },
  ]);

  const after = await call(project, 'get_ontology_object', {
    object_id: 'object:customer',
    revision: updated.revision,
  });
  assert.deepEqual(after.object.metadata.aliases, ['客户主档', '客户实体']);
  assert.equal(after.object.fields[2].key, 'customer_level');
  assert.equal(after.object.relations[0].label, '服务订单');
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
