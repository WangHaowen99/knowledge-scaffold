import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'modeling-mcp-operation-rule-'));
process.env.MODELING_MCP_OPERATION_RULE_ENABLED = 'true';

const [
  { createProject },
  { getPackageArtifactStore },
  { insertDocument },
  { getAuthoringState },
  { getDb },
  { config },
  { createOrGetPackageDraft },
  { OperationRuleMcpService },
  { executeModelingMcpTool, toolDefinitionsFor, toolNamesByToolset },
  { ModelingMcpToolError },
  { ArtifactRepositoryError },
] = await Promise.all([
  import('../src/projects/repo.js'),
  import('../src/artifacts/runtime.js'),
  import('../src/documents/repo.js'),
  import('../src/authoring/state.js'),
  import('../src/db/index.js'),
  import('../src/config.js'),
  import('../src/workflow/package-draft.js'),
  import('../src/mcp/operation-rule/service.js'),
  import('../src/mcp/toolset.js'),
  import('../src/mcp/tool-error.js'),
  import('../src/artifacts/repository.js'),
]);

function artifactsWithPlanningConflict(artifacts: ReturnType<typeof getPackageArtifactStore>) {
  return new Proxy(artifacts, {
    get(target, property) {
      if (property === 'planPackageVersionChanges') {
        return async () => {
          throw new ArtifactRepositoryError('revision_conflict', 'synthetic planning conflict', {
            expectedRevision: 'expected-revision',
            actualRevision: 'actual-revision',
          });
        };
      }
      const value = Reflect.get(target, property, target);
      return typeof value === 'function' ? value.bind(target) : value;
    },
  });
}

config.mcp.operationRuleEnabled = true;

async function seedProject(projectId: string) {
  insertDocument({
    projectId,
    filename: 'synthetic_records.xlsx',
    kind: 'excel',
    parseStatus: 'parsed',
    tables: [{
      name: 'records',
      rowCount: 1,
      fields: [{ name: 'record_id', inferredType: 'string', sampleValues: ['record_0001'], distinctRatio: 1 }],
    }],
  });
  const artifacts = getPackageArtifactStore();
  const initial = await artifacts.initializeDraftPackage(projectId, {
    id: `synthetic-${projectId}`,
    name: '合成本体包',
    description: 'Operation/Rule MCP 合成测试基线',
  });
  return artifacts.savePackageVersion(projectId, {
    expectedRevision: initial.revision.oid,
    spec: {
      ...initial.spec,
      objects: [{
        key: 'record',
        title: '记录',
        description: '用于合成测试的记录对象。',
        sourceRefs: [{ kind: 'table', table: 'records' }],
        fields: [{ key: 'record_id', cnName: '记录编号', type: 'string', required: true, sourceColumn: 'record_id' }],
        primaryKey: ['record_id'],
      }],
    },
    coverage: initial.coverage,
    note: '建立合成测试基线',
    action: {
      cause: 'mcp_operation_rule_test_seed',
      summary: '建立合成测试基线',
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
    tokenId: 'synthetic-token',
    argumentsValue: args,
  });
}

function revisionEventsSince(projectId: string, afterSeq: number) {
  return getAuthoringState().replay(projectId, afterSeq).events.filter((event) => (
    event.type === 'package.revision.created'
  ));
}

function assertRevisionEvent(
  projectId: string,
  afterSeq: number,
  expected: { revisionId: string; cause: string; resource: { type: string; id: string; key: string } },
) {
  const events = revisionEventsSince(projectId, afterSeq);
  assert.equal(events.length, 1);
  assert.equal(events[0].payload.revisionId, expected.revisionId);
  assert.equal(events[0].payload.cause, expected.cause);
  assert.deepEqual(events[0].payload.resource, expected.resource);
  assert.equal(events[0].payload.source, 'mcp');
}

function assertBlockingAdvice(result: Record<string, any>, affectedType?: string) {
  assert.equal(result.ok, false);
  const blocking = (result.issues ?? []).filter((item: any) => item.level === 'blocking');
  assert.ok(blocking.length > 0);
  for (const item of blocking) {
    assert.ok(String(item.code || '').trim());
    assert.ok(String(item.phase || '').trim());
    assert.ok(String(item.target || '').trim());
    assert.ok(String(item.message || '').trim());
    assert.ok(String(item.suggestion || '').trim());
  }
  if (affectedType) {
    assert.ok((result.affected_resources ?? []).some((item: any) => item.type === affectedType));
  }
}

function assertBlockedSubmission(error: unknown) {
  assert.ok(error instanceof ModelingMcpToolError);
  assert.equal(error.code, 'SUBMISSION_BLOCKED');
  assert.equal(error.details.reason_code, 'SUBMISSION_BLOCKED');
  assert.ok(error.details.resource);
  assertBlockingAdvice({ ok: false, issues: error.details.issues, affected_resources: error.details.affected_resources });
  return true;
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
      cause: 'mcp_operation_rule_status_seed',
      summary: `切换知识包状态到 ${status}`,
      actorKind: 'system',
      actorName: '测试系统',
    },
  });
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
    'mcp_operation_rule_candidate_seed',
    'synthetic pending candidate',
    'user',
    '测试系统',
    '[]',
    '[]',
    now,
  );
}

test('Operation/Rule 工具注册到现有 read/change toolset', () => {
  const tools = toolNamesByToolset();
  assert.equal(tools.read.length, 6);
  assert.equal(tools.change.length, 9);
  assert.ok(tools.read.includes('list_ontology_objects'));
  assert.ok(tools.read.includes('list_ontology_operations'));
  assert.ok(tools.read.includes('get_ontology_rule'));
  assert.ok(tools.change.includes('validate_ontology_object_change'));
  assert.ok(tools.change.includes('update_ontology_object'));
  assert.ok(tools.change.includes('validate_ontology_operation_rule_change'));
  assert.ok(tools.change.includes('delete_ontology_rule'));
});

test('Object/Operation/Rule MCP schema 与知识包 Unicode key 规则一致', () => {
  const readTools = toolDefinitionsFor('read', ['ontology.read']);
  const changeTools = toolDefinitionsFor('change', [
    'ontology.change.validate',
    'ontology.change.apply',
  ]);
  const patternFor = (toolName: string, field: string, tools = readTools) => {
    const tool = tools.find((item) => item.name === toolName)!;
    return String((tool.inputSchema.properties as Record<string, any>)[field].pattern);
  };

  assert.match('object:测试对象', new RegExp(patternFor('get_ontology_object', 'object_id'), 'u'));
  assert.match('operation:查询操作', new RegExp(patternFor('get_ontology_operation', 'operation_id'), 'u'));
  assert.match('rule:判断规则', new RegExp(patternFor('get_ontology_rule', 'rule_id'), 'u'));
  assert.match('新增操作', new RegExp(patternFor('create_ontology_operation', 'key', changeTools), 'u'));
});

test('Operation/Rule MCP 分页游标绑定资源类型和 query', async () => {
  const project = createProject({ owner: 'cursor_rule', name: 'Rule cursor context', runtime: 'agent' });
  const seeded = await seedProject(project.id);
  const payload = {
    description: '用于分页游标测试。',
    applies_to_objects: ['record'],
    content: '记录编号非空。',
    conditions: [{ condition: 'record_id 非空', conclusion: '记录有效' }],
    exceptions: [],
  };
  const firstRule = await call(project, 'create_ontology_rule', {
    expected_revision: seeded.revision.oid,
    key: 'record_valid_a',
    title: '记录有效规则 A',
    ...payload,
  });
  await call(project, 'create_ontology_rule', {
    expected_revision: firstRule.revision,
    key: 'record_valid_b',
    title: '记录有效规则 B',
    ...payload,
  });
  const first = await call(project, 'list_ontology_rules', { limit: 1 });
  assert.ok(first.next_cursor);
  await assert.rejects(
    () => call(project, 'list_ontology_rules', { limit: 1, query: 'B', cursor: first.next_cursor }),
    (error: unknown) => error instanceof ModelingMcpToolError && error.code === 'CURSOR_CONTEXT_MISMATCH',
  );
  await assert.rejects(
    () => call(project, 'list_ontology_operations', { limit: 1, cursor: first.next_cursor }),
    (error: unknown) => error instanceof ModelingMcpToolError && error.code === 'CURSOR_CONTEXT_MISMATCH',
  );
});

test('Operation/Rule MCP 无关修改保留 legacy mapping 和人工 Markdown', async () => {
  const project = createProject({ owner: 'legacy_files', name: 'MCP imported file preservation', runtime: 'agent' });
  const seeded = await seedProject(project.id);
  const artifacts = getPackageArtifactStore();
  const archive = await artifacts.readPackageArchiveFiles(project.id, seeded.revision.oid);
  const decoder = new TextDecoder();
  const encoder = new TextEncoder();
  const mappingPath = 'mappings/object-data-mappings.json';
  const objectPath = 'objects/record.md';
  const mappings = JSON.parse(decoder.decode(archive.files[mappingPath]));
  mappings.objects.record.resource = 'legacy_records';
  const manualMarker = '\n<!-- imported manual content -->\n';
  archive.files[mappingPath] = encoder.encode(`${JSON.stringify(mappings, null, 2)}\n`);
  archive.files[objectPath] = encoder.encode(`${decoder.decode(archive.files[objectPath])}${manualMarker}`);
  const imported = await artifacts.importPackageVersion(project.id, {
    expectedRevision: seeded.revision.oid,
    spec: archive.spec,
    coverage: archive.coverage,
    files: archive.files,
    action: {
      cause: 'mcp_legacy_import_seed', summary: '导入 legacy 文件', actorKind: 'user', actorName: project.owner,
    },
  });

  const result = await call(project, 'create_ontology_rule', {
    expected_revision: imported.revision.oid,
    key: 'record_valid',
    title: '记录有效规则',
    description: '判断记录是否有效。',
    applies_to_objects: ['record'],
    content: '记录编号非空时视为有效。',
    conditions: [{ condition: 'record_id 非空', conclusion: '记录有效' }],
    exceptions: [],
  });
  const after = await artifacts.readPackageFiles(project.id, result.revision);
  assert.equal(JSON.parse(after.files[mappingPath]).objects.record.resource, 'legacy_records');
  assert.match(after.files[objectPath], /imported manual content/u);
  const actualChangedPaths = (await artifacts.diff(project.id, imported.revision.oid, result.revision))
    .files.map((file) => file.path.replace(/^package\//u, '')).sort();
  assert.deepEqual([...result.changed_paths].sort(), actualChangedPaths);
  assert.equal(actualChangedPaths.includes(mappingPath), false);
  assert.equal(actualChangedPaths.includes(objectPath), false);
});

test('Rule -> Operation CRUD、影响分析和删除保护形成同步闭环', async () => {
  const project = createProject({ owner: 'operator_a', name: 'Operation Rule CRUD', runtime: 'agent' });
  const seeded = await seedProject(project.id);

  const rulePayload = {
    title: '记录有效规则',
    description: '判断记录是否有效。',
    applies_to_objects: ['record'],
    content: '记录编号非空时视为有效。',
    conditions: [{ condition: 'record_id 非空', conclusion: '记录有效' }],
    exceptions: [],
  };
  const validated = await call(project, 'validate_ontology_operation_rule_change', {
    expected_revision: seeded.revision.oid,
    resource_type: 'rule',
    action: 'create',
    resource_id: 'rule:record_valid',
    payload: rulePayload,
  });
  assert.equal(validated.ok, true);
  assert.equal(validated.resource.id, 'rule:record_valid');
  assert.equal(Array.isArray(validated.issues), true);

  let beforeEventSeq = getAuthoringState().snapshot(project.id).lastEventSeq;
  const createdRule = await call(project, 'create_ontology_rule', {
    expected_revision: seeded.revision.oid,
    key: 'record_valid',
    ...rulePayload,
  });
  assert.notEqual(createdRule.revision, seeded.revision.oid);
  assertRevisionEvent(project.id, beforeEventSeq, {
    revisionId: createdRule.revision,
    cause: 'modeling_mcp_rule_create',
    resource: { type: 'rule', id: 'rule:record_valid', key: 'record_valid' },
  });

  beforeEventSeq = getAuthoringState().snapshot(project.id).lastEventSeq;
  const createdOperation = await call(project, 'create_ontology_operation', {
    expected_revision: createdRule.revision,
    key: 'read_record',
    title: '读取记录',
    description: '根据记录编号读取有效记录。',
    inputs: [{ param: 'record_id', cn_name: '记录编号', type: 'string', required: true }],
    outputs: [{ field: 'result', cn_name: '记录结果', type: 'record' }],
    flow: ['读取 object:record', '按 rule:record_valid 判断记录有效性', '返回 result'],
    related_objects: ['record'],
    uses_rules: ['record_valid'],
    uses_metrics: [],
    notes: [],
  });
  assert.notEqual(createdOperation.revision, createdRule.revision);
  assertRevisionEvent(project.id, beforeEventSeq, {
    revisionId: createdOperation.revision,
    cause: 'modeling_mcp_operation_create',
    resource: { type: 'operation', id: 'operation:read_record', key: 'read_record' },
  });

  beforeEventSeq = getAuthoringState().snapshot(project.id).lastEventSeq;
  const linkedRule = await call(project, 'update_ontology_rule', {
    expected_revision: createdOperation.revision,
    rule_id: 'rule:record_valid',
    changes: { applies_to_operations: ['read_record'] },
  });
  assertRevisionEvent(project.id, beforeEventSeq, {
    revisionId: linkedRule.revision,
    cause: 'modeling_mcp_rule_update',
    resource: { type: 'rule', id: 'rule:record_valid', key: 'record_valid' },
  });

  const ruleDeleteAnalysis = await call(project, 'validate_ontology_operation_rule_change', {
    expected_revision: linkedRule.revision,
    resource_type: 'rule',
    action: 'delete',
    resource_id: 'rule:record_valid',
  });
  assertBlockingAdvice(ruleDeleteAnalysis, 'operation');
  assert.ok(ruleDeleteAnalysis.issues.some((item: any) => item.code === 'RULE_DELETE_BLOCKED_BY_OPERATION'));

  await assert.rejects(
    () => call(project, 'delete_ontology_rule', {
      expected_revision: linkedRule.revision,
      rule_id: 'rule:record_valid',
    }),
    (error: unknown) => {
      assertBlockedSubmission(error);
      assert.match(JSON.stringify(error.details), /read_record/u);
      return true;
    },
  );

  const operationDeleteAnalysis = await call(project, 'validate_ontology_operation_rule_change', {
    expected_revision: linkedRule.revision,
    resource_type: 'operation',
    action: 'delete',
    resource_id: 'operation:read_record',
  });
  assertBlockingAdvice(operationDeleteAnalysis, 'rule');
  assert.ok(operationDeleteAnalysis.issues.some((item: any) => item.code === 'OPERATION_DELETE_BLOCKED_BY_RULE'));
  await assert.rejects(
    () => call(project, 'delete_ontology_operation', {
      expected_revision: linkedRule.revision,
      operation_id: 'operation:read_record',
    }),
    assertBlockedSubmission,
  );

  const operationUpdateAnalysis = await call(project, 'validate_ontology_operation_rule_change', {
    expected_revision: linkedRule.revision,
    resource_type: 'operation',
    action: 'update',
    resource_id: 'operation:read_record',
    payload: { uses_rules: ['missing_rule'] },
  });
  assertBlockingAdvice(operationUpdateAnalysis);

  const ruleUpdateAnalysis = await call(project, 'validate_ontology_operation_rule_change', {
    expected_revision: linkedRule.revision,
    resource_type: 'rule',
    action: 'update',
    resource_id: 'rule:record_valid',
    payload: { applies_to_operations: ['missing_operation'] },
  });
  assertBlockingAdvice(ruleUpdateAnalysis);

  beforeEventSeq = getAuthoringState().snapshot(project.id).lastEventSeq;
  const detachedRule = await call(project, 'update_ontology_rule', {
    expected_revision: linkedRule.revision,
    rule_id: 'rule:record_valid',
    changes: { applies_to_operations: [] },
  });
  assertRevisionEvent(project.id, beforeEventSeq, {
    revisionId: detachedRule.revision,
    cause: 'modeling_mcp_rule_update',
    resource: { type: 'rule', id: 'rule:record_valid', key: 'record_valid' },
  });

  beforeEventSeq = getAuthoringState().snapshot(project.id).lastEventSeq;
  const updatedOperation = await call(project, 'update_ontology_operation', {
    expected_revision: detachedRule.revision,
    operation_id: 'operation:read_record',
    changes: { uses_rules: [] },
  });
  assertRevisionEvent(project.id, beforeEventSeq, {
    revisionId: updatedOperation.revision,
    cause: 'modeling_mcp_operation_update',
    resource: { type: 'operation', id: 'operation:read_record', key: 'read_record' },
  });

  beforeEventSeq = getAuthoringState().snapshot(project.id).lastEventSeq;
  const deletedRule = await call(project, 'delete_ontology_rule', {
    expected_revision: updatedOperation.revision,
    rule_id: 'rule:record_valid',
  });
  assertRevisionEvent(project.id, beforeEventSeq, {
    revisionId: deletedRule.revision,
    cause: 'modeling_mcp_rule_delete',
    resource: { type: 'rule', id: 'rule:record_valid', key: 'record_valid' },
  });

  beforeEventSeq = getAuthoringState().snapshot(project.id).lastEventSeq;
  const deletedOperation = await call(project, 'delete_ontology_operation', {
    expected_revision: deletedRule.revision,
    operation_id: 'operation:read_record',
  });
  assertRevisionEvent(project.id, beforeEventSeq, {
    revisionId: deletedOperation.revision,
    cause: 'modeling_mcp_operation_delete',
    resource: { type: 'operation', id: 'operation:read_record', key: 'read_record' },
  });
  const current = await getPackageArtifactStore().readPackageFiles(project.id);
  assert.equal(current.revision.oid, deletedOperation.revision);
  assert.equal((current.spec.rules ?? []).some((item) => item.key === 'record_valid'), false);
  assert.equal(current.spec.operations.some((item) => item.key === 'read_record'), false);
  assert.equal(current.spec.operations.length, 0);
  assert.equal(current.files['operations/read_record.md'], undefined);
  assert.equal(current.files['rules/record_valid.md'], undefined);
});

test('Operation/Rule 缺失引用在分析和提交阶段均阻塞且零写入', async () => {
  const project = createProject({ owner: 'operator_missing_ref', name: 'Missing reference guards', runtime: 'agent' });
  const seeded = await seedProject(project.id);

  const operationPayload = {
    title: '缺失引用操作',
    description: '验证缺失 Object 和 Rule 引用。',
    inputs: [{ param: 'record_id', cn_name: '记录编号', type: 'string', required: true }],
    outputs: [{ field: 'result', cn_name: '结果', type: 'string' }],
    flow: ['使用 record_id 读取 missing_object', '执行 missing_rule', '返回 result'],
    related_objects: ['missing_object'],
    uses_rules: ['missing_rule'],
    uses_metrics: [],
    notes: [],
  };
  const operationAnalysis = await call(project, 'validate_ontology_operation_rule_change', {
    expected_revision: seeded.revision.oid,
    resource_type: 'operation',
    action: 'create',
    resource_id: 'operation:missing_reference_operation',
    payload: operationPayload,
  });
  assertBlockingAdvice(operationAnalysis);
  assert.match(JSON.stringify(operationAnalysis.issues), /missing_object/u);
  assert.match(JSON.stringify(operationAnalysis.issues), /missing_rule/u);
  await assert.rejects(
    () => call(project, 'create_ontology_operation', {
      expected_revision: seeded.revision.oid,
      key: 'missing_reference_operation',
      ...operationPayload,
    }),
    assertBlockedSubmission,
  );

  const rulePayload = {
    title: '缺失引用规则',
    description: '验证缺失 Object 和 Operation 引用。',
    applies_to_objects: ['missing_object'],
    applies_to_operations: ['missing_operation'],
    content: '仅用于合成缺失引用测试。',
  };
  const ruleAnalysis = await call(project, 'validate_ontology_operation_rule_change', {
    expected_revision: seeded.revision.oid,
    resource_type: 'rule',
    action: 'create',
    resource_id: 'rule:missing_reference_rule',
    payload: rulePayload,
  });
  assertBlockingAdvice(ruleAnalysis);
  assert.match(JSON.stringify(ruleAnalysis.issues), /missing_object/u);
  assert.match(JSON.stringify(ruleAnalysis.issues), /missing_operation/u);
  await assert.rejects(
    () => call(project, 'create_ontology_rule', {
      expected_revision: seeded.revision.oid,
      key: 'missing_reference_rule',
      ...rulePayload,
    }),
    assertBlockedSubmission,
  );

  const current = await getPackageArtifactStore().readPackageFiles(project.id);
  assert.equal(current.revision.oid, seeded.revision.oid);
  assert.equal(current.spec.operations.length, 0);
  assert.equal((current.spec.rules ?? []).length, 0);
});

test('旧 revision 提交返回冲突且不覆盖最新内容', async () => {
  const project = createProject({ owner: 'operator_stale_revision', name: 'Stale revision guard', runtime: 'agent' });
  const seeded = await seedProject(project.id);
  const createdRule = await call(project, 'create_ontology_rule', {
    expected_revision: seeded.revision.oid,
    key: 'current_rule',
    title: '当前规则',
    content: '建立新的正式 revision。',
    applies_to_objects: ['record'],
  });

  await assert.rejects(
    () => call(project, 'create_ontology_operation', {
      expected_revision: seeded.revision.oid,
      key: 'stale_operation',
      title: '过期操作',
      description: '使用旧 revision 的提交。',
      inputs: [],
      outputs: [],
      flow: ['返回空结果'],
      related_objects: ['record'],
    }),
    (error: unknown) => {
      assert.ok(error instanceof ModelingMcpToolError);
      assert.equal(error.code, 'REVISION_CONFLICT');
      assert.equal(error.details.expected_revision, seeded.revision.oid);
      assert.equal(error.details.actual_revision, createdRule.revision);
      return true;
    },
  );

  const current = await getPackageArtifactStore().readPackageFiles(project.id);
  assert.equal(current.revision.oid, createdRule.revision);
  assert.deepEqual((current.spec.rules ?? []).map((item) => item.key), ['current_rule']);
  assert.equal(current.spec.operations.length, 0);
});

test('同一正式 revision 并发写最多一个成功', async () => {
  const project = createProject({ owner: 'operator_b', name: 'Operation Rule concurrency', runtime: 'agent' });
  const seeded = await seedProject(project.id);
  const createRule = (key: string) => call(project, 'create_ontology_rule', {
    expected_revision: seeded.revision.oid,
    key,
    title: `合成规则 ${key}`,
    content: `规则 ${key} 的合成内容。`,
    applies_to_objects: ['record'],
  });
  const settled = await Promise.allSettled([createRule('rule_a'), createRule('rule_b')]);
  assert.equal(settled.filter((item) => item.status === 'fulfilled').length, 1);
  const rejected = settled.find((item): item is PromiseRejectedResult => item.status === 'rejected');
  assert.ok(rejected?.reason instanceof ModelingMcpToolError);
  assert.ok(['REVISION_CONFLICT', 'PROJECT_WRITE_BUSY'].includes(rejected.reason.code));
  const current = await getPackageArtifactStore().readPackageFiles(project.id);
  assert.equal((current.spec.rules ?? []).length, 1);
});

test('Operation/Rule 重复提交相同内容时 validate 与 apply 都返回 NO_CHANGES', async () => {
  const project = createProject({ owner: 'operator_noop', name: 'Operation Rule no-op', runtime: 'agent' });
  const seeded = await seedProject(project.id);
  const created = await call(project, 'create_ontology_rule', {
    expected_revision: seeded.revision.oid,
    key: 'same_rule',
    title: '相同规则',
    content: '相同规则内容。',
    applies_to_objects: ['record'],
  });
  const args = {
    expected_revision: created.revision,
    resource_type: 'rule',
    action: 'update',
    resource_id: 'rule:same_rule',
    payload: { title: '相同规则', content: '相同规则内容。', applies_to_objects: ['record'] },
  };
  const validated = await call(project, 'validate_ontology_operation_rule_change', args);
  assert.equal(validated.ok, false);
  assert.deepEqual(validated.changed_paths, []);
  assert.ok(validated.issues.some((item: any) => item.code === 'NO_CHANGES'));
  await assert.rejects(
    () => call(project, 'update_ontology_rule', {
      expected_revision: created.revision,
      rule_id: 'rule:same_rule',
      changes: {
        title: '相同规则',
        content: '相同规则内容。',
        applies_to_objects: ['record'],
      },
    }),
    (error: unknown) => error instanceof ModelingMcpToolError && error.code === 'SUBMISSION_BLOCKED',
  );
});

test('Operation/Rule MCP 写入同样受包状态与编辑忙碌态门禁约束', async () => {
  const releasedProject = createProject({ owner: 'operator_c', name: 'Operation Rule status gate', runtime: 'agent' });
  await seedProject(releasedProject.id);
  const released = await setPackageStatus(releasedProject.id, 'released');

  const releasedValidation = await call(releasedProject, 'validate_ontology_operation_rule_change', {
    expected_revision: released.revision.oid,
    resource_type: 'rule',
    action: 'create',
    resource_id: 'rule:record_valid',
    payload: { title: '记录有效规则', content: '记录编号存在时有效。', applies_to_objects: ['record'] },
  });
  assert.equal(releasedValidation.ok, false);
  assert.ok(releasedValidation.issues.some((item: any) => item.code === 'PACKAGE_NOT_EDITABLE'));

  const draftProject = createProject({ owner: 'operator_d', name: 'Operation Rule package draft gate', runtime: 'agent' });
  const seededWithDraft = await seedProject(draftProject.id);
  await createOrGetPackageDraft(draftProject.id, draftProject.owner);
  const draftValidation = await call(draftProject, 'validate_ontology_operation_rule_change', {
    expected_revision: seededWithDraft.revision.oid,
    resource_type: 'rule',
    action: 'create',
    resource_id: 'rule:record_valid',
    payload: { title: '记录有效规则', content: '记录编号存在时有效。', applies_to_objects: ['record'] },
  });
  assert.equal(draftValidation.ok, false);
  assert.ok(draftValidation.issues.some((item: any) => item.code === 'PROJECT_EDITING_BUSY'));
  assert.ok(draftValidation.issues.some((item: any) => item.target === 'package_draft'));

  const pendingCandidateProject = createProject({
    owner: 'operator_e',
    name: 'Operation Rule pending candidate gate',
    runtime: 'agent',
  });
  const seededWithPending = await seedProject(pendingCandidateProject.id);
  insertPendingCandidate(pendingCandidateProject.id, seededWithPending.revision.oid);
  await assert.rejects(
    () => call(pendingCandidateProject, 'create_ontology_rule', {
      expected_revision: seededWithPending.revision.oid,
      key: 'record_valid',
      title: '记录有效规则',
      content: '记录编号存在时有效。',
      applies_to_objects: ['record'],
    }),
    (error: unknown) => {
      assertBlockedSubmission(error);
      assert.ok((error as ModelingMcpToolError).details.issues.some((item: any) => item.target === 'candidate'));
      return true;
    },
  );
});

test('Operation/Rule MCP revision 事件发布失败时仍返回已提交 revision', async () => {
  const project = createProject({
    owner: 'event_warning_rule',
    name: 'Operation Rule event warning',
    runtime: 'agent',
  });
  const seeded = await seedProject(project.id);
  const artifacts = getPackageArtifactStore();
  const service = new OperationRuleMcpService(getDb(), artifacts, () => {
    throw new Error('synthetic revision event failure');
  });
  const originalConsoleError = console.error;
  const loggedErrors: unknown[][] = [];
  console.error = (...args: unknown[]) => { loggedErrors.push(args); };

  try {
    const result = await service.execute('create_ontology_rule', project, project.owner, {
      expected_revision: seeded.revision.oid,
      key: 'event_warning_rule',
      title: '事件降级规则',
      content: '事件发布失败不影响正式 revision。',
      applies_to_objects: ['record'],
    }) as Record<string, any>;

    assert.notEqual(result.revision, seeded.revision.oid);
    assert.ok(result.warnings.some((item: any) => item.code === 'REVISION_EVENT_PUBLISH_FAILED'));
    assert.ok(result.warnings.some((item: any) => item.level === 'warning' && item.phase === 'persist'));
    const current = await artifacts.readPackageFiles(project.id);
    assert.equal(current.revision.oid, result.revision);
    assert.ok((current.spec.rules ?? []).some((item) => item.key === 'event_warning_rule'));
    assert.equal(loggedErrors.length, 1);
  } finally {
    console.error = originalConsoleError;
  }
});

test('Operation/Rule MCP 分析期间的 revision 冲突保持 409 可重试语义', async () => {
  const project = createProject({ owner: 'rule_plan_conflict', name: 'Rule MCP plan conflict', runtime: 'agent' });
  const seeded = await seedProject(project.id);
  const service = new OperationRuleMcpService(getDb(), artifactsWithPlanningConflict(getPackageArtifactStore()));
  const analysisArgs = {
    resource_type: 'rule',
    action: 'create',
    expected_revision: seeded.revision.oid,
    key: 'planning_conflict_rule',
    payload: {
      title: '并发冲突规则',
      content: '用于验证 revision 冲突错误语义。',
      applies_to_objects: ['record'],
    },
  };
  const applyArgs = {
    expected_revision: seeded.revision.oid,
    key: 'planning_conflict_rule',
    title: '并发冲突规则',
    content: '用于验证 revision 冲突错误语义。',
    applies_to_objects: ['record'],
  };

  for (const [toolName, args] of [
    ['validate_ontology_operation_rule_change', analysisArgs],
    ['create_ontology_rule', applyArgs],
  ] as const) {
    await assert.rejects(
      () => service.execute(toolName, project, project.owner, args),
      (error: unknown) => {
        assert.ok(error instanceof ModelingMcpToolError);
        assert.equal(error.code, 'REVISION_CONFLICT');
        assert.equal(error.statusCode, 409);
        assert.equal(error.details.phase, 'validate');
        assert.equal(error.details.retryable, true);
        assert.equal(error.details.reason_code, 'REVISION_CONFLICT');
        assert.equal(error.details.expected_revision, 'expected-revision');
        assert.equal(error.details.actual_revision, 'actual-revision');
        return true;
      },
    );
  }
});
