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
  { config },
  { executeModelingMcpTool, toolDefinitionsFor, toolNamesByToolset },
  { ModelingMcpToolError },
] = await Promise.all([
  import('../src/projects/repo.js'),
  import('../src/artifacts/runtime.js'),
  import('../src/config.js'),
  import('../src/mcp/toolset.js'),
  import('../src/mcp/tool-error.js'),
]);

config.mcp.operationRuleEnabled = true;

async function seedProject(projectId: string) {
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
        fields: [{ key: 'record_id', cnName: '记录编号', type: 'string', required: true }],
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

  const createdRule = await call(project, 'create_ontology_rule', {
    expected_revision: seeded.revision.oid,
    key: 'record_valid',
    ...rulePayload,
  });
  assert.notEqual(createdRule.revision, seeded.revision.oid);

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

  const linkedRule = await call(project, 'update_ontology_rule', {
    expected_revision: createdOperation.revision,
    rule_id: 'rule:record_valid',
    changes: { applies_to_operations: ['read_record'] },
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

  const detachedRule = await call(project, 'update_ontology_rule', {
    expected_revision: linkedRule.revision,
    rule_id: 'rule:record_valid',
    changes: { applies_to_operations: [] },
  });

  const updatedOperation = await call(project, 'update_ontology_operation', {
    expected_revision: detachedRule.revision,
    operation_id: 'operation:read_record',
    changes: { uses_rules: [] },
  });
  const deletedRule = await call(project, 'delete_ontology_rule', {
    expected_revision: updatedOperation.revision,
    rule_id: 'rule:record_valid',
  });
  const current = await getPackageArtifactStore().readPackageFiles(project.id);
  assert.equal(current.revision.oid, deletedRule.revision);
  assert.equal((current.spec.rules ?? []).some((item) => item.key === 'record_valid'), false);
  assert.equal(current.spec.operations[0].usesRules?.length ?? 0, 0);
  assert.ok(current.files['operations/read_record.md']);
  assert.equal(current.files['rules/record_valid.md'], undefined);
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
