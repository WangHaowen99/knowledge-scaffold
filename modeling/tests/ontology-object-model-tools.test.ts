import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

import Fastify from 'fastify';

import type { SpecScript } from '../src/types.js';
import { buildPythonScriptScaffold } from '../src/workflow/python-script-scaffold.js';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'modeling-ontology-object-tools-'));

const [
  { registerAgentCoreRoutes },
  { createProject },
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
      operations: [{
        key: 'render_synthetic_summary',
        title: '生成合成摘要',
        description: '把合成输入转换为结构化摘要。',
        inputs: [{ param: 'input_json', type: 'string', required: true }],
        outputs: [{ field: 'result', type: 'object' }],
        flow: ['读取合成输入', '返回结构化摘要'],
        relatedObjects: ['order'],
        evidence: [],
      }],
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

async function seedProjectWithManagedScript(projectId: string) {
  const seeded = await seedProject(projectId);
  const contract: Omit<SpecScript, 'sourceCode'> = {
    key: 'render_synthetic_summary',
    title: '生成合成摘要脚本',
    description: '读取合成 JSON 并返回结构化摘要。',
    boundOperation: 'render_synthetic_summary',
    runtime: 'python',
    entrypoint: 'render_synthetic_summary.py',
    timeoutSeconds: 30,
    parameters: [{
      name: 'input_json', flag: '--input-json', type: 'string', required: true,
      description: 'Synthetic JSON input',
    }],
    outputs: [{ key: 'result', type: 'object', description: 'Structured synthetic result' }],
    boundaries: ['只处理显式传入的合成 JSON，不访问网络或本地文件'],
    evidence: ['user-decision:managed-script-baseline'],
  };
  const sourceCode = buildPythonScriptScaffold(contract).replace(
    'def run_business_logic(inputs):\n    raise NotImplementedError("run_business_logic must be implemented")',
    'def run_business_logic(inputs):\n    payload = json.loads(inputs["input_json"])\n    return {"result": payload}',
  );
  return getPackageArtifactStore().savePackageVersion(projectId, {
    expectedRevision: seeded.revision.oid,
    spec: {
      ...structuredClone(seeded.spec),
      operations: seeded.spec.operations.map((operation) => operation.key === contract.boundOperation
        ? { ...operation, script: { key: contract.key, required: true } }
        : operation),
      rules: [...(seeded.spec.rules ?? []), {
        key: 'unplanned',
        title: '计划外规则',
        description: '用于验证脚本计划不会夹带未授权知识修改。',
        content: '这条规则保持基线内容。',
        evidence: ['user-decision:managed-script-baseline'],
      }],
      scripts: [{ ...contract, sourceCode }],
    },
    coverage: seeded.coverage,
    note: '写入受管脚本增量测试基线',
    action: {
      cause: 'test.managed_script_seeded', summary: '写入受管脚本增量测试基线',
      actorKind: 'system', actorName: '测试系统',
    },
  });
}

async function prepareIncrementalScriptPlan(
  app: ReturnType<typeof Fastify>,
  projectId: string,
  runId: string,
) {
  const operation = await tool(app, projectId, 'get_domain_resource', {
    resource_id: 'operation:render_synthetic_summary',
  }, runId);
  assert.equal(operation.statusCode, 200, operation.body);
  const planResponse = await tool(app, projectId, 'create_change_plan', {
    goal: '只更新合成摘要操作说明和其已绑定脚本函数',
    mode: 'expert_edit',
    expected_revision: operation.json().working_revision,
    tasks: [{
      client_key: 'operation', title: '更新合成摘要操作', resource_kind: 'operation',
      target_key: 'render_synthetic_summary', allowed_tools: ['patch_operation'],
      acceptance: '只更新操作说明',
    }, {
      client_key: 'unrelated-rule', title: '更新计划外规则', resource_kind: 'rule',
      target_key: 'unplanned', allowed_tools: ['patch_rule'],
      acceptance: '该任务不属于脚本依赖链',
    }, {
      client_key: 'script', title: '更新合成摘要脚本函数', resource_kind: 'script',
      target_key: 'render_synthetic_summary', allowed_tools: ['stage_script_patch'],
      depends_on: ['operation'], acceptance: '只替换 run_business_logic',
    }],
  }, runId);
  assert.equal(planResponse.statusCode, 200, planResponse.body);
  const plan = planResponse.json().plan;
  const stagedOperation = await tool(app, projectId, 'stage_change_set', {
    plan_id: plan.id,
    expected_revision: operation.json().working_revision,
    mutation_id: `${runId}-operation`,
    actions: [{
      task_id: plan.tasks[0].id,
      tool: 'patch_operation',
      arguments: {
        resource_id: 'operation:render_synthetic_summary',
        expected_resource_hash: operation.json().resource.resource_hash,
        actions: [{ op: 'set_description', value: '读取合成 JSON 并返回带字段数量的结构化摘要。' }],
      },
    }],
  }, runId);
  assert.equal(stagedOperation.statusCode, 200, stagedOperation.body);
  const updatedOperation = await tool(app, projectId, 'get_domain_resource', {
    resource_id: 'operation:render_synthetic_summary',
    working_revision: stagedOperation.json().working_revision,
  }, runId);
  assert.equal(updatedOperation.statusCode, 200, updatedOperation.body);
  const defined = await tool(app, projectId, 'define_script_contract', {
    expected_revision: updatedOperation.json().working_revision,
    mutation_id: `${runId}-contract`,
    reason: '按同一增量计划更新脚本契约',
    script_key: 'render_synthetic_summary',
    title: '生成合成摘要脚本',
    description: '读取合成 JSON 并返回结构化摘要。',
    operation_id: 'operation:render_synthetic_summary',
    expected_operation_hash: updatedOperation.json().resource.operation_contract_hash,
    entrypoint: 'render_synthetic_summary.py',
    timeout_seconds: 30,
    parameters: [{
      name: 'input_json', flag: '--input-json', type: 'string', required: true,
      description: 'Synthetic JSON input',
    }],
    outputs: [{ key: 'result', type: 'object', description: 'Structured synthetic result' }],
    boundaries: ['只处理显式传入的合成 JSON，不访问网络或本地文件'],
    evidence: ['user-decision:managed-script-increment'],
  }, runId);
  assert.equal(defined.statusCode, 200, defined.body);
  const context = await tool(app, projectId, 'get_script_edit_context', {
    script_key: 'render_synthetic_summary',
    symbols: ['run_business_logic'],
  }, runId);
  assert.equal(context.statusCode, 200, context.body);
  return { plan, defined: defined.json(), context: context.json() };
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
      update: [{ relationId, changes: { label: '拥有', description: '客户服务的订单集合。' } }],
    },
  };

  const validated = await tool(app, project.id, 'validate_ontology_object_patch', mutation);
  assert.equal(validated.statusCode, 200, validated.body);
  assert.equal(validated.json().ok, true);
  assert.deepEqual(validated.json().normalized_mutation.fieldOps.addKeys, ['customer_level']);
  assert.equal(validated.json().next_object.fields.length, 3);
  assert.equal(validated.json().next_object.relations[0].label, '拥有');

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
  assert.equal(after.json().object.relations[0].label, '拥有');
});

test('领域 Patch 只编译语义受影响路径，并保留无关候选和隐藏规格字段', async (t) => {
  const project = createProject({ owner: 'alice', name: '合成增量编译边界', runtime: 'agent' });
  const app = appFor(t);
  await seedProject(project.id);
  const runId = `run-incremental-compiler-${project.id}`;

  const claimed = await tool(app, project.id, 'claim_candidate_workspace', {}, runId);
  assert.equal(claimed.statusCode, 200, claimed.body);
  const unrelatedContent = '# Synthetic retained candidate\n\nThis unrelated candidate must remain byte-for-byte unchanged.\n';
  const unrelated = await tool(app, project.id, 'write_file', {
    path: 'index.md',
    content: unrelatedContent,
    change_group: 'synthetic_unrelated_candidate',
    evidence: [],
  }, runId);
  assert.equal(unrelated.statusCode, 200, unrelated.body);

  const listed = await tool(app, project.id, 'list_domain_resources', { kind: 'object' }, runId);
  assert.equal(listed.statusCode, 200, listed.body);
  const order = listed.json().items.find((item: any) => item.resource_id === 'object:order');
  const customerBefore = await tool(app, project.id, 'get_domain_resource', {
    resource_id: 'object:customer',
    working_revision: listed.json().working_revision,
  }, runId);
  assert.equal(customerBefore.statusCode, 200, customerBefore.body);
  assert.equal(customerBefore.json().resource.values.fields[0].sourceColumn, 'customer_id');

  const patched = await tool(app, project.id, 'patch_object', {
    resource_id: 'object:order',
    expected_revision: listed.json().working_revision,
    expected_resource_hash: order.resource_hash,
    mutation_id: 'synthetic-scoped-object-patch-v2',
    reason: 'Change only one synthetic object description',
    actions: [{ op: 'set_description', value: '仅修改合成订单的描述。' }],
  }, runId);
  assert.equal(patched.statusCode, 200, patched.body);
  assert.deepEqual(patched.json().changed_paths, ['objects/index.md', 'objects/order.md']);

  const retained = await tool(app, project.id, 'read_file', { path: 'index.md' }, runId);
  assert.equal(retained.statusCode, 200, retained.body);
  assert.equal(retained.json().content, unrelatedContent);
  const customerAfter = await tool(app, project.id, 'get_domain_resource', {
    resource_id: 'object:customer',
    working_revision: patched.json().working_revision,
  }, runId);
  assert.equal(customerAfter.statusCode, 200, customerAfter.body);
  assert.equal(customerAfter.json().resource.resource_hash, customerBefore.json().resource.resource_hash);
  assert.equal(customerAfter.json().resource.values.fields[0].sourceColumn, 'customer_id');
});

test('领域资源工具支持四类资源增量修改、幂等重放、哈希冲突和引用感知删除', async (t) => {
  const project = createProject({ owner: 'alice', name: '合成领域增量闭环', runtime: 'agent' });
  const app = appFor(t);
  await seedProject(project.id);
  const material = insertDocument({
    projectId: project.id,
    filename: 'synthetic_policy.md',
    kind: 'markdown',
    parseStatus: 'parsed',
    textContent: '# Synthetic policy\nOnly synthetic records are used in this test.',
  });
  const evidence = [`material:${material.sourceKey}#Synthetic policy`];

  const objects = await tool(app, project.id, 'list_domain_resources', { kind: 'object' });
  assert.equal(objects.statusCode, 200, objects.body);
  const order = objects.json().items.find((item: any) => item.resource_id === 'object:order');
  assert.ok(order?.resource_hash);

  const managedWrite = await tool(app, project.id, 'write_file', {
    path: 'objects/order.md',
    content: 'This direct managed write must not be evaluated.',
    change_group: 'synthetic_direct_write',
    evidence: [],
  });
  assert.equal(managedWrite.statusCode, 409, managedWrite.body);
  assert.equal(managedWrite.json().code, 'MANAGED_FILE_REQUIRES_DOMAIN_TOOL');
  assert.equal(managedWrite.json().requiredAction, 'redirect_tool');
  assert.equal(managedWrite.json().requiredTool, 'patch_object');
  assert.match(managedWrite.json().issueId, /^agent-issue-[a-f0-9]{20}$/u);

  const managedDelete = await tool(app, project.id, 'delete_file', {
    path: 'metrics/index.md',
    change_group: 'synthetic_direct_delete',
    reason: 'This direct managed delete must be rejected.',
  });
  assert.equal(managedDelete.statusCode, 409, managedDelete.body);
  assert.equal(managedDelete.json().requiredTool, 'preview_domain_delete');

  const objectPatch = {
    resource_id: 'object:order',
    expected_revision: objects.json().working_revision,
    expected_resource_hash: order.resource_hash,
    mutation_id: 'synthetic-object-add-field-v2',
    reason: 'Add one synthetic status field',
    actions: [{
      op: 'add_field',
      field: { key: 'record_status', cn_name: '记录状态', type: 'string', required: false },
    }],
  };
  const patchedObject = await tool(app, project.id, 'patch_object', objectPatch);
  assert.equal(patchedObject.statusCode, 200, patchedObject.body);
  assert.deepEqual(
    patchedObject.json().resource.values.fields.map((field: any) => field.key),
    ['order_id', 'record_status'],
  );
  assert.match(patchedObject.json().resource.resource_hash, /^sha256:[a-f0-9]{64}$/u);

  const replayed = await tool(app, project.id, 'patch_object', objectPatch);
  assert.equal(replayed.statusCode, 200, replayed.body);
  assert.equal(replayed.json().replayed, true);
  assert.equal(replayed.json().working_revision, patchedObject.json().working_revision);

  const staleHash = await tool(app, project.id, 'patch_object', {
    ...objectPatch,
    expected_revision: patchedObject.json().working_revision,
    mutation_id: 'synthetic-object-stale-hash-v2',
    actions: [{ op: 'set_description', value: 'A changed synthetic description.' }],
  });
  assert.equal(staleHash.statusCode, 409, staleHash.body);
  assert.equal(staleHash.json().code, 'RESOURCE_HASH_CONFLICT');
  assert.equal(staleHash.json().retryable, false);

  const repeatedStaleHash = await tool(app, project.id, 'patch_object', {
    ...objectPatch,
    expected_revision: patchedObject.json().working_revision,
    mutation_id: 'synthetic-object-stale-hash-v2',
    actions: [{ op: 'set_description', value: 'A changed synthetic description.' }],
  });
  assert.equal(repeatedStaleHash.statusCode, 409, repeatedStaleHash.body);
  assert.equal(repeatedStaleHash.json().negative_receipt, true);
  assert.equal(repeatedStaleHash.json().replayed, true);

  const tableDocument = insertDocument({
    projectId: project.id,
    filename: 'synthetic_records_binding.xlsx',
    kind: 'excel',
    parseStatus: 'parsed',
    tables: [{
      name: 'synthetic_records',
      rowCount: 1,
      fields: [
        { name: 'record_id', inferredType: 'string', sampleValues: ['synthetic_001'], distinctRatio: 1 },
        { name: 'record_status', inferredType: 'string', sampleValues: ['ready'], distinctRatio: 1 },
      ],
    }],
  });
  const boundObject = await tool(app, project.id, 'patch_object_source_binding', {
    resource_id: 'object:order',
    expected_revision: patchedObject.json().working_revision,
    expected_resource_hash: patchedObject.json().resource.resource_hash,
    mutation_id: 'synthetic-bind-object-v2',
    reason: 'Bind every synthetic object field to one parsed table',
    document_id: tableDocument.id,
    table: 'synthetic_records',
    field_bindings: [
      { field_key: 'order_id', source_column: 'record_id' },
      { field_key: 'record_status', source_column: 'record_status' },
    ],
    primary_key: ['order_id'],
    evidence: [`material:${tableDocument.sourceKey}#synthetic_records`],
  });
  assert.equal(boundObject.statusCode, 409, boundObject.body);
  assert.equal(boundObject.json().code, 'PHYSICAL_SCHEMA_REQUIRED');

  const createdRule = await tool(app, project.id, 'create_rule', {
    expected_revision: patchedObject.json().working_revision,
    mutation_id: 'synthetic-create-rule-v2',
    reason: 'Create a synthetic rule',
    key: 'synthetic_policy',
    title: '合成规则',
    description: '仅用于确定性测试。',
    content: '记录状态满足条件时返回合成结论。',
    conditions: [{ condition: 'record_status 非空', conclusion: '返回记录状态' }],
    exceptions: [],
    targets: ['object:order'],
    evidence,
  });
  assert.equal(createdRule.statusCode, 200, createdRule.body);
  assert.equal(createdRule.json().resource.id, 'rule:synthetic_policy');

  const patchedRule = await tool(app, project.id, 'patch_rule', {
    resource_id: 'rule:synthetic_policy',
    expected_revision: createdRule.json().working_revision,
    expected_resource_hash: createdRule.json().resource.resource_hash,
    mutation_id: 'synthetic-patch-rule-v2',
    reason: 'Add one synthetic exception without replacing the rule',
    actions: [{ op: 'add_exception', value: 'record_status 为测试保留值时不返回结论' }],
  });
  assert.equal(patchedRule.statusCode, 200, patchedRule.body);
  assert.deepEqual(patchedRule.json().resource.values.exceptions, ['record_status 为测试保留值时不返回结论']);

  const createdMetric = await tool(app, project.id, 'create_metric', {
    expected_revision: patchedRule.json().working_revision,
    mutation_id: 'synthetic-create-metric-v2',
    reason: 'Create a synthetic metric',
    key: 'synthetic_count',
    title: '合成记录数',
    description: '仅统计合成记录。',
    unit: '条',
    grain: '状态',
    caliber: 'COUNT(order_id)',
    boundaries: ['仅统计测试夹具中的合成记录'],
    object_references: ['object:order'],
    evidence,
  });
  assert.equal(createdMetric.statusCode, 200, createdMetric.body);
  assert.equal(createdMetric.json().resource.id, 'metric:synthetic_count');

  const createdOperation = await tool(app, project.id, 'create_operation', {
    expected_revision: createdMetric.json().working_revision,
    mutation_id: 'synthetic-create-operation-v2',
    reason: 'Create a synthetic operation',
    key: 'summarize_synthetic_records',
    title: '汇总合成记录',
    description: '按状态汇总合成记录。',
    inputs: [{ param: 'record_status', type: 'string', required: false }],
    outputs: [{ field: 'record_count', type: 'number' }],
    flow: ['读取合成记录', '按状态汇总并返回结果'],
    references: ['object:order', 'rule:synthetic_policy', 'metric:synthetic_count'],
    evidence,
  });
  assert.equal(createdOperation.statusCode, 200, createdOperation.body);
  assert.equal(createdOperation.json().resource.id, 'operation:summarize_synthetic_records');

  const firstStepId = createdOperation.json().resource.editable_items.flow[0].item_id;
  const patchedOperation = await tool(app, project.id, 'patch_operation', {
    resource_id: 'operation:summarize_synthetic_records',
    expected_revision: createdOperation.json().working_revision,
    expected_resource_hash: createdOperation.json().resource.resource_hash,
    mutation_id: 'synthetic-patch-operation-v2',
    reason: 'Insert one step after the stable first step',
    actions: [{ op: 'insert_step', after_id: firstStepId, value: '校验合成记录状态' }],
  });
  assert.equal(patchedOperation.statusCode, 200, patchedOperation.body);
  assert.deepEqual(patchedOperation.json().resource.values.flow, [
    '读取合成记录', '校验合成记录状态', '按状态汇总并返回结果',
  ]);

  const metricSnapshot = await tool(app, project.id, 'get_domain_resource', {
    resource_id: 'metric:synthetic_count',
    working_revision: patchedOperation.json().working_revision,
  });
  assert.equal(metricSnapshot.statusCode, 200, metricSnapshot.body);
  const boundaryId = metricSnapshot.json().resource.editable_items.boundaries[0].item_id;
  const patchedMetric = await tool(app, project.id, 'patch_metric', {
    resource_id: 'metric:synthetic_count',
    expected_revision: metricSnapshot.json().working_revision,
    expected_resource_hash: metricSnapshot.json().resource.resource_hash,
    mutation_id: 'synthetic-patch-metric-v2',
    reason: 'Refine one synthetic boundary',
    actions: [{ op: 'update_boundary', item_id: boundaryId, value: '只统计有效的合成记录' }],
  });
  assert.equal(patchedMetric.statusCode, 200, patchedMetric.body);
  assert.deepEqual(patchedMetric.json().resource.values.boundaries, ['只统计有效的合成记录']);

  const preview = await tool(app, project.id, 'preview_domain_delete', {
    resource_ids: ['metric:synthetic_count'],
    expected_revision: patchedMetric.json().working_revision,
  });
  assert.equal(preview.statusCode, 200, preview.body);
  assert.equal(preview.json().cleanup_required, true);
  assert.deepEqual(preview.json().affected_resource_ids, ['operation:summarize_synthetic_records']);

  const deleted = await tool(app, project.id, 'delete_domain_resource', {
    resource_ids: ['metric:synthetic_count'],
    expected_revision: preview.json().working_revision,
    mutation_id: 'synthetic-delete-metric-v2',
    reason: 'Delete the synthetic metric and remove its explicit reference',
    preview_token: preview.json().preview_token,
    cleanup_references: true,
  });
  assert.equal(deleted.statusCode, 200, deleted.body);
  assert.deepEqual(deleted.json().deleted_resource_ids, ['metric:synthetic_count']);

  const operationAfterDelete = await tool(app, project.id, 'get_domain_resource', {
    resource_id: 'operation:summarize_synthetic_records',
    working_revision: deleted.json().working_revision,
  });
  assert.equal(operationAfterDelete.statusCode, 200, operationAfterDelete.body);
  assert.equal(
    operationAfterDelete.json().resource.relations.some((relation: any) => (
      relation.target_id === 'metric:synthetic_count'
    )),
    false,
  );
});

test('脚本工具先固化契约，再原子生成源码、说明、索引和操作绑定', async (t) => {
  const project = createProject({ owner: 'alice', name: '合成脚本契约闭环', runtime: 'agent' });
  const app = appFor(t);
  await seedProject(project.id);
  const material = insertDocument({
    projectId: project.id,
    filename: 'synthetic_script_contract.md',
    kind: 'markdown',
    parseStatus: 'parsed',
    textContent: '# Synthetic script contract\nReturn a structured result from synthetic input.',
  });

  const operations = await tool(app, project.id, 'list_domain_resources', { kind: 'operation' });
  assert.equal(operations.statusCode, 200, operations.body);
  const operation = await tool(app, project.id, 'get_domain_resource', {
    resource_id: 'operation:render_synthetic_summary',
    working_revision: operations.json().working_revision,
  });
  assert.equal(operation.statusCode, 200, operation.body);
  assert.match(operation.json().resource.operation_contract_hash, /^sha256:[a-f0-9]{64}$/u);

  const defined = await tool(app, project.id, 'define_script_contract', {
    expected_revision: operation.json().working_revision,
    mutation_id: 'synthetic-define-script-v1',
    reason: 'Define a synthetic script contract before source generation',
    script_key: 'render_synthetic_summary',
    title: '生成合成摘要脚本',
    description: '读取合成 JSON 并返回结构化摘要。',
    operation_id: 'operation:render_synthetic_summary',
    expected_operation_hash: operation.json().resource.operation_contract_hash,
    entrypoint: 'render_synthetic_summary.py',
    timeout_seconds: 30,
    parameters: [{
      name: 'input_json', flag: '--input-json', type: 'string', required: true,
      description: 'Synthetic JSON input',
    }, {
      name: 'verbose', flag: '--verbose', type: 'boolean', required: false,
      action: 'store_true', description: 'Enable verbose output',
    }],
    outputs: [{ key: 'result', type: 'object', description: 'Structured synthetic result' }],
    boundaries: ['只处理显式传入的合成 JSON，不访问网络或本地文件'],
    evidence: [`material:${material.sourceKey}#Synthetic script contract`],
  });
  assert.equal(defined.statusCode, 200, defined.body);
  assert.equal(defined.json().candidate, false);
  assert.equal(defined.json().contract.parameters[1].action, 'store_true');
  assert.equal(defined.json().contract.parameters[1].default_value, false);

  const scriptIndex = await tool(app, project.id, 'get_script_edit_context', {
    script_key: 'render_synthetic_summary',
  });
  assert.equal(scriptIndex.statusCode, 200, scriptIndex.body);
  assert.equal(scriptIndex.json().scaffold, true);
  assert.match(scriptIndex.json().base_source_hash, /^sha256:[a-f0-9]{64}$/u);
  assert.ok(scriptIndex.json().symbols.some((item: any) => item.qualified_name === 'run_business_logic'));
  assert.equal(scriptIndex.json().symbols.some((item: any) => Object.hasOwn(item, 'source')), false);

  const editContext = await tool(app, project.id, 'get_script_edit_context', {
    script_key: 'render_synthetic_summary',
    symbols: ['run_business_logic'],
  });
  assert.equal(editContext.statusCode, 200, editContext.body);
  const businessNode = editContext.json().symbols.find(
    (item: any) => item.qualified_name === 'run_business_logic',
  );
  assert.match(businessNode.source, /NotImplementedError/u);
  assert.match(businessNode.node_hash, /^sha256:[a-f0-9]{64}$/u);

  const staleSourcePatch = await tool(app, project.id, 'stage_script_patch', {
    script_key: 'render_synthetic_summary',
    expected_revision: defined.json().working_revision,
    expected_contract_hash: defined.json().contract_hash,
    mutation_id: 'synthetic-stage-stale-source-v1',
    reason: 'A stale source hash must be rejected',
    base_source_hash: `sha256:${'0'.repeat(64)}`,
    edits: [{
      kind: 'replace_function',
      symbol: 'run_business_logic',
      expected_node_hash: businessNode.node_hash,
      source: 'def run_business_logic(inputs):\n    return {"result": inputs}',
    }],
  });
  assert.equal(staleSourcePatch.statusCode, 409, staleSourcePatch.body);
  assert.equal(staleSourcePatch.json().code, 'SCRIPT_PATCH_CONFLICT');

  const staleNodePatch = await tool(app, project.id, 'stage_script_patch', {
    script_key: 'render_synthetic_summary',
    expected_revision: defined.json().working_revision,
    expected_contract_hash: defined.json().contract_hash,
    mutation_id: 'synthetic-stage-stale-node-v1',
    reason: 'A stale node hash must be rejected',
    base_source_hash: editContext.json().base_source_hash,
    edits: [{
      kind: 'replace_function',
      symbol: 'run_business_logic',
      expected_node_hash: `sha256:${'f'.repeat(64)}`,
      source: 'def run_business_logic(inputs):\n    return {"result": inputs}',
    }],
  });
  assert.equal(staleNodePatch.statusCode, 409, staleNodePatch.body);
  assert.equal(staleNodePatch.json().code, 'SCRIPT_PATCH_CONFLICT');

  const directSourceWrite = await tool(app, project.id, 'write_file', {
    path: 'scripts/render_synthetic_summary.py',
    content: 'print("bypass")',
    change_group: 'synthetic_script_bypass',
    evidence: [],
  });
  assert.equal(directSourceWrite.statusCode, 409, directSourceWrite.body);
  assert.equal(directSourceWrite.json().requiredTool, 'stage_script_patch');

  const placeholderSource = await tool(app, project.id, 'stage_script_patch', {
    script_key: 'render_synthetic_summary',
    expected_revision: defined.json().working_revision,
    expected_contract_hash: defined.json().contract_hash,
    mutation_id: 'synthetic-stage-placeholder-v1',
    reason: 'The placeholder must be rejected',
    base_source_hash: editContext.json().base_source_hash,
    edits: [{
      kind: 'replace_function',
      symbol: 'run_business_logic',
      expected_node_hash: businessNode.node_hash,
      source: 'def run_business_logic(inputs):\n    return {"result": "hello world"}',
    }],
  });
  assert.equal(placeholderSource.statusCode, 400, placeholderSource.body);
  assert.equal(placeholderSource.json().code, 'INVALID_DOMAIN_PAYLOAD');
  assert.match(placeholderSource.json().reason, /hello world 演示实现|演示或占位实现/u);

  const businessSource = `def run_business_logic(inputs):
    payload = json.loads(inputs["input_json"])
    result = {"received_keys": sorted(payload.keys())}
    return {"result": result}
`;
  const staged = await tool(app, project.id, 'stage_script_patch', {
    script_key: 'render_synthetic_summary',
    expected_revision: defined.json().working_revision,
    expected_contract_hash: defined.json().contract_hash,
    mutation_id: 'synthetic-stage-script-v1',
    reason: 'Stage a contract-compliant synthetic script',
    base_source_hash: editContext.json().base_source_hash,
    edits: [{
      kind: 'replace_function',
      symbol: 'run_business_logic',
      expected_node_hash: businessNode.node_hash,
      source: businessSource,
    }],
  });
  assert.equal(staged.statusCode, 200, staged.body);
  assert.equal(staged.json().candidate, true);
  assert.deepEqual(staged.json().rendered_paths, [
    'scripts/render_synthetic_summary.md',
    'scripts/render_synthetic_summary.py',
    'scripts/index.md',
    'operations/render_synthetic_summary.md',
    'operations/index.md',
    'index.md',
  ]);

  const generatedSource = await tool(app, project.id, 'read_file', {
    path: 'scripts/render_synthetic_summary.py',
  });
  assert.equal(generatedSource.statusCode, 409, generatedSource.body);
  assert.equal(generatedSource.json().code, 'MANAGED_FILE_REQUIRES_DOMAIN_TOOL');
  assert.equal(generatedSource.json().requiredTool, 'get_script_edit_context');

  const businessContext = await tool(app, project.id, 'get_script_edit_context', {
    script_key: 'render_synthetic_summary',
    symbols: ['run_business_logic'],
  });
  assert.equal(businessContext.statusCode, 200, businessContext.body);
  assert.equal(businessContext.json().symbols.length, 1);
  assert.equal(businessContext.json().symbols[0].qualified_name, 'run_business_logic');
  assert.equal(businessContext.json().symbols[0].source, businessSource);
  assert.doesNotMatch(businessContext.json().symbols[0].source, /def main/u);

  const replayed = await tool(app, project.id, 'stage_script_patch', {
    script_key: 'render_synthetic_summary',
    expected_revision: defined.json().working_revision,
    expected_contract_hash: defined.json().contract_hash,
    mutation_id: 'synthetic-stage-script-v1',
    reason: 'Stage a contract-compliant synthetic script',
    base_source_hash: editContext.json().base_source_hash,
    edits: [{
      kind: 'replace_function',
      symbol: 'run_business_logic',
      expected_node_hash: businessNode.node_hash,
      source: businessSource,
    }],
  });
  assert.equal(replayed.statusCode, 200, replayed.body);
  assert.equal(replayed.json().replayed, true);

  const nextContext = await tool(app, project.id, 'get_script_edit_context', {
    script_key: 'render_synthetic_summary',
    symbols: ['run_business_logic'],
  });
  assert.equal(nextContext.statusCode, 200, nextContext.body);
  assert.equal(nextContext.json().scaffold, true);
  const protectedContext = await tool(app, project.id, 'get_script_edit_context', {
    script_key: 'render_synthetic_summary',
    symbols: ['main'],
  });
  assert.equal(protectedContext.statusCode, 200, protectedContext.body);
  const mainNode = protectedContext.json().symbols.find(
    (item: any) => item.qualified_name === 'main',
  );
  const protectedPatch = await tool(app, project.id, 'stage_script_patch', {
    script_key: 'render_synthetic_summary',
    expected_revision: staged.json().working_revision,
    expected_contract_hash: defined.json().contract_hash,
    mutation_id: 'synthetic-stage-protected-main-v1',
    reason: 'Core scaffold functions stay immutable after the first business patch',
    base_source_hash: protectedContext.json().base_source_hash,
    edits: [{
      kind: 'replace_function',
      symbol: 'main',
      expected_node_hash: mainNode.node_hash,
      source: 'def main(argv=None):\n    return None',
    }],
  });
  assert.equal(protectedPatch.statusCode, 409, protectedPatch.body);
  assert.equal(protectedPatch.json().code, 'SCAFFOLD_NODE_IMMUTABLE');
  const nextBusinessNode = nextContext.json().symbols.find(
    (item: any) => item.qualified_name === 'run_business_logic',
  );
  const revisedBusinessSource = businessSource
    .replace('result = {"received_keys": sorted(payload.keys())}', 'revised_result = {"received_keys": sorted(payload.keys())}')
    .replace('return {"result": result}', 'return {"result": revised_result}');
  const restaged = await tool(app, project.id, 'stage_script_patch', {
    script_key: 'render_synthetic_summary',
    expected_revision: staged.json().working_revision,
    expected_contract_hash: defined.json().contract_hash,
    mutation_id: 'synthetic-stage-script-v2',
    reason: 'Restage the current synthetic script after a source-only rename',
    base_source_hash: nextContext.json().base_source_hash,
    edits: [{
      kind: 'replace_function',
      symbol: 'run_business_logic',
      expected_node_hash: nextBusinessNode.node_hash,
      source: revisedBusinessSource,
    }],
  });
  assert.equal(restaged.statusCode, 200, restaged.body);
  assert.notEqual(restaged.json().source_hash, staged.json().source_hash);

  const formalBeforeStudioReview = (await getPackageArtifactStore().current(project.id))!.revision.oid;
  const blockedStudioReview = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/studio/review`,
    payload: {
      expectedRevision: restaged.json().working_revision,
      clientMutationId: 'synthetic-studio-review-before-script-test',
    },
  });
  assert.equal(blockedStudioReview.statusCode, 409, blockedStudioReview.body);
  assert.equal(blockedStudioReview.json().code, 'SCRIPT_TEST_REQUIRED');
  assert.deepEqual(blockedStudioReview.json().blockers, ['scripts/render_synthetic_summary.py']);
  assert.equal((await getPackageArtifactStore().current(project.id))!.revision.oid, formalBeforeStudioReview);
  assert.ok(getDb().prepare(`SELECT COUNT(*) AS count FROM agent_candidate_changes
    WHERE project_id = ?`).get(project.id)!.count > 0);

  const prepared = await tool(app, project.id, 'prepare_confirmation_groups');
  assert.equal(prepared.statusCode, 200, prepared.body);
  const preparedImpacts = prepared.json().change_groups.flatMap((group: any) => group.impact ?? []);
  assert.equal(preparedImpacts.includes('operations/index.md'), false);
  const preflight = await tool(app, project.id, 'request_confirmation_preflight', {
    summary: '确认合成脚本候选',
    change_groups: prepared.json().change_groups,
  });
  assert.equal(preflight.statusCode, 409, preflight.body);
  assert.equal(preflight.json().code, 'SCRIPT_TEST_REQUIRED');
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
      update: [{ relationId, changes: { label: '拥有' } }],
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
  assert.equal(rejected.json().feedback, '需要补充字段说明');

  const rejectedStatus = await tool(app, project.id, 'get_ontology_object_review_status', {
    object_id: 'object:customer',
  });
  assert.equal(rejectedStatus.statusCode, 200, rejectedStatus.body);
  assert.equal(rejectedStatus.json().stage, 'rejected');
  assert.equal(rejectedStatus.json().current.stage, 'rejected');

  const retainedDraft = await tool(app, project.id, 'get_ontology_object', {
    object_id: 'object:customer',
  });
  assert.deepEqual(retainedDraft.json().object.metadata.aliases, ['客户主档', '客户对象']);

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

test('增量脚本补丁可依赖同一专家计划内已暂存的知识修改', async (t) => {
  const project = createProject({ owner: 'alice', name: '计划内增量脚本依赖', runtime: 'agent' });
  const app = appFor(t);
  await seedProjectWithManagedScript(project.id);
  const runId = `run-planned-script-${project.id}`;
  const { plan, defined, context } = await prepareIncrementalScriptPlan(app, project.id, runId);
  const node = context.symbols.find((item: any) => item.qualified_name === 'run_business_logic');
  const staged = await tool(app, project.id, 'stage_script_patch', {
    plan_task_id: plan.tasks[2].id,
    script_key: 'render_synthetic_summary',
    expected_revision: defined.working_revision,
    expected_contract_hash: defined.contract_hash,
    mutation_id: `${runId}-script`,
    reason: '只替换同一计划内脚本的业务函数',
    base_source_hash: context.base_source_hash,
    edits: [{
      kind: 'replace_function',
      symbol: 'run_business_logic',
      expected_node_hash: node.node_hash,
      source: [
        'def run_business_logic(inputs):',
        '    payload = json.loads(inputs["input_json"])',
        '    return {"result": {"field_count": len(payload)}}',
      ].join('\n'),
    }],
  }, runId);
  assert.equal(staged.statusCode, 200, staged.body);
  assert.deepEqual(staged.json().applied_edits, [{ kind: 'replace_function', symbol: 'run_business_logic' }]);
  assert.deepEqual(staged.json().plan.tasks.map((task: any) => task.status), ['staged', 'pending', 'staged']);
});

test('计划内脚本补丁不会放行未授权的待确认知识修改', async (t) => {
  const project = createProject({ owner: 'alice', name: '增量脚本拒绝夹带修改', runtime: 'agent' });
  const app = appFor(t);
  await seedProjectWithManagedScript(project.id);
  const runId = `run-planned-script-blocked-${project.id}`;
  const { plan, defined } = await prepareIncrementalScriptPlan(app, project.id, runId);
  const unrelatedRule = await tool(app, project.id, 'get_domain_resource', {
    resource_id: 'rule:unplanned',
    working_revision: defined.working_revision,
  }, runId);
  assert.equal(unrelatedRule.statusCode, 200, unrelatedRule.body);
  const unrelated = await tool(app, project.id, 'stage_change_set', {
    plan_id: plan.id,
    expected_revision: unrelatedRule.json().working_revision,
    mutation_id: `${runId}-unrelated-rule`,
    actions: [{
      task_id: plan.tasks[1].id,
      tool: 'patch_rule',
      arguments: {
        resource_id: 'rule:unplanned',
        expected_resource_hash: unrelatedRule.json().resource.resource_hash,
        actions: [{ op: 'set_description', value: '这项待确认修改不属于脚本任务的依赖链。' }],
      },
    }],
  }, runId);
  assert.equal(unrelated.statusCode, 200, unrelated.body);
  const blockedContext = await tool(app, project.id, 'get_script_edit_context', {
    script_key: 'render_synthetic_summary',
    symbols: ['run_business_logic'],
  }, runId);
  assert.equal(blockedContext.statusCode, 200, blockedContext.body);
  const node = blockedContext.json().symbols.find(
    (item: any) => item.qualified_name === 'run_business_logic',
  );
  const blocked = await tool(app, project.id, 'stage_script_patch', {
    plan_task_id: plan.tasks[2].id,
    script_key: 'render_synthetic_summary',
    expected_revision: blockedContext.json().working_revision,
    expected_contract_hash: defined.contract_hash,
    mutation_id: `${runId}-script`,
    reason: '夹带计划外知识时必须拒绝脚本修改',
    base_source_hash: blockedContext.json().base_source_hash,
    edits: [{
      kind: 'replace_function',
      symbol: 'run_business_logic',
      expected_node_hash: node.node_hash,
      source: [
        'def run_business_logic(inputs):',
        '    payload = json.loads(inputs["input_json"])',
        '    return {"result": {"field_count": len(payload)}}',
      ].join('\n'),
    }],
  }, runId);
  assert.equal(blocked.statusCode, 409, blocked.body);
  assert.equal(blocked.json().code, 'GATE_UNMET');
});

test('Change Plan 用一个原子 Change Set 完成跨类型修改并可重放回执', async (t) => {
  const project = createProject({ owner: 'alice', name: '受控跨资源修改', runtime: 'agent' });
  const app = appFor(t);
  await seedProject(project.id);
  const runId = `run-change-set-success-${project.id}`;

  const objects = await tool(app, project.id, 'list_domain_resources', { kind: 'object' }, runId);
  const operations = await tool(app, project.id, 'list_domain_resources', { kind: 'operation' }, runId);
  assert.equal(objects.statusCode, 200, objects.body);
  assert.equal(operations.statusCode, 200, operations.body);
  const revision = objects.json().working_revision as string;
  const customer = objects.json().items.find((item: any) => item.key === 'customer');
  const operation = operations.json().items.find((item: any) => item.key === 'render_synthetic_summary');

  const planResponse = await tool(app, project.id, 'create_change_plan', {
    goal: '同时更新客户对象说明和合成摘要操作说明，其他内容保持不变',
    mode: 'expert_edit',
    expected_revision: revision,
    tasks: [
      {
        client_key: 'customer-description', title: '更新客户对象说明', resource_kind: 'object',
        target_key: 'customer', allowed_tools: ['patch_object'], acceptance: '只改变对象说明',
      },
      {
        client_key: 'operation-description', title: '更新合成摘要操作说明', resource_kind: 'operation',
        target_key: 'render_synthetic_summary', allowed_tools: ['patch_operation'],
        depends_on: ['customer-description'], acceptance: '只改变操作说明',
      },
    ],
  }, runId);
  assert.equal(planResponse.statusCode, 200, planResponse.body);
  const plan = planResponse.json().plan;
  const stageArguments = {
    plan_id: plan.id,
    expected_revision: revision,
    mutation_id: 'change-set-cross-resource-success',
    actions: [
      {
        task_id: plan.tasks[0].id,
        tool: 'patch_object',
        arguments: {
          resource_id: 'object:customer', expected_resource_hash: customer.resource_hash,
          actions: [{ op: 'set_description', value: '客户对象说明由原子 Change Set 更新。' }],
        },
      },
      {
        task_id: plan.tasks[1].id,
        tool: 'patch_operation',
        arguments: {
          resource_id: 'operation:render_synthetic_summary', expected_resource_hash: operation.resource_hash,
          actions: [{ op: 'set_description', value: '操作说明由同一原子 Change Set 更新。' }],
        },
      },
    ],
  };
  const staged = await tool(app, project.id, 'stage_change_set', stageArguments, runId);
  assert.equal(staged.statusCode, 200, staged.body);
  assert.equal(staged.json().status, 'staged');
  assert.equal(staged.json().action_count, 2);
  assert.ok(staged.json().changed_paths.some((path: string) => path === 'objects/customer.md'));
  assert.ok(staged.json().changed_paths.some((path: string) => path === 'operations/render_synthetic_summary.md'));

  const replayed = await tool(app, project.id, 'stage_change_set', stageArguments, runId);
  assert.equal(replayed.statusCode, 200, replayed.body);
  assert.equal(replayed.json().replayed, true);
  assert.equal(replayed.json().change_set_id, staged.json().change_set_id);

  const currentPlan = await tool(app, project.id, 'get_change_plan', { plan_id: plan.id }, runId);
  assert.deepEqual(currentPlan.json().plan.tasks.map((item: any) => item.status), ['staged', 'staged']);
  assert.equal(currentPlan.json().plan.expected_revision, staged.json().working_revision);
  const receipt = await tool(app, project.id, 'get_change_set_receipt', {
    mutation_id: 'change-set-cross-resource-success',
  }, runId);
  assert.equal(receipt.statusCode, 200, receipt.body);
  assert.equal(receipt.json().change_set.status, 'staged');

  const validated = await tool(app, project.id, 'validate_change_set', {
    plan_id: plan.id,
    summary: '核对跨资源原子修改',
  }, runId);
  assert.equal(validated.statusCode, 200, validated.body);
  assert.deepEqual(validated.json().plan.tasks.map((item: any) => item.status), ['validated', 'validated']);
  const validation = validated.json().validation;
  const confirmed = await tool(app, project.id, 'request_confirmation', {
    summary: '确认跨资源原子修改',
    change_groups: validation.change_groups,
    expected_candidate_revision: validation.candidate_revision,
    expected_candidate_hash: validation.validation_receipt.candidateHash,
    expected_validator_version: validation.validation_receipt.validatorVersion,
    review: {
      approved: true,
      review_mode: 'full',
      selected_groups: validation.change_groups.filter((item: any) => !item.autoApproved).map((item: any) => item.id),
      sampled_groups: [],
    },
  }, runId);
  assert.equal(confirmed.statusCode, 200, confirmed.body);
  const completedPlan = await tool(app, project.id, 'get_change_plan', { plan_id: plan.id }, runId);
  assert.equal(completedPlan.json().plan.status, 'completed');
  assert.deepEqual(completedPlan.json().plan.tasks.map((item: any) => item.status), ['accepted', 'accepted']);
});

test('Schema 生成计划由 Core 注入全量同步任务并逐 Sheet 原子生成对象和 mapping', async (t) => {
  const project = createProject({ owner: 'alice', name: '合成 Schema 全量同步', runtime: 'agent' });
  const app = appFor(t);
  const runId = `run-schema-sync-${project.id}`;
  const schemaDocument = insertDocument({
    projectId: project.id,
    filename: 'synthetic_schema.xlsx',
    kind: 'excel',
    parseStatus: 'parsed',
    purpose: 'schema',
    tables: [{
      name: 'synthetic_customer',
      rowCount: 1,
      fields: [
        { name: 'customer_id', inferredType: 'string', sampleValues: ['customer_0001'], distinctRatio: 1 },
        { name: 'customer_name', inferredType: 'string', sampleValues: ['synthetic_name'], distinctRatio: 1 },
      ],
    }, {
      name: 'synthetic_empty',
      rowCount: 0,
      fields: [
        { name: 'empty_id', inferredType: 'string', sampleValues: [], distinctRatio: 0 },
        { name: 'empty_note', inferredType: 'string', sampleValues: [], distinctRatio: 0 },
        { name: 'customer_id', inferredType: 'string', sampleValues: [], distinctRatio: 0 },
        { name: 'customer_code', inferredType: 'string', sampleValues: [], distinctRatio: 0 },
      ],
    }],
  });
  const artifacts = getPackageArtifactStore();
  const initialized = await artifacts.initializeDraftPackage(project.id, {
    id: `schema-sync-${project.id}`,
    name: '合成 Schema 全量同步',
    description: '只使用合成表结构验证',
  });
  await artifacts.savePackageVersion(project.id, {
    expectedRevision: initialized.revision.oid,
    spec: {
      ...structuredClone(initialized.spec),
      objects: [{
        key: 'legacy_derived', title: '旧派生对象', description: '同步时必须删除',
        fields: [{ key: 'legacy_id', cnName: '旧编号', type: 'string' }],
      }],
      operations: [], rules: [], metrics: [], scripts: [],
    },
    coverage: initialized.coverage,
    note: '建立待同步的合成基线',
    action: {
      cause: 'schema_sync_seeded',
      summary: '建立待同步的合成基线',
      actorKind: 'system',
      actorName: '测试系统',
    },
  });

  const listed = await tool(app, project.id, 'list_domain_resources', { kind: 'object' }, runId);
  const invalidAggregatePlan = await tool(app, project.id, 'create_change_plan', {
    goal: '验证泛化对象目标不会进入计划',
    mode: 'generation',
    expected_revision: listed.json().working_revision,
    tasks: [{
      client_key: 'all-object-relations', title: '补充全部对象关系', resource_kind: 'object',
      target_key: 'all_schema_objects', allowed_tools: ['patch_object'],
      acceptance: '补充对象关系',
    }],
  }, runId);
  assert.equal(invalidAggregatePlan.statusCode, 409, invalidAggregatePlan.body);
  assert.equal(invalidAggregatePlan.json().code, 'CHANGE_PLAN_TARGET_NOT_FOUND');

  const planResponse = await tool(app, project.id, 'create_change_plan', {
    goal: '按已确认数据 Schema 生成全部物理对象',
    mode: 'generation',
    expected_revision: listed.json().working_revision,
    tasks: [
      {
        client_key: 'model-created-object', title: '让模型创建合成对象', resource_kind: 'object',
        target_key: 'model_object', allowed_tools: ['create_object'],
        acceptance: '生成对象',
      },
      {
        client_key: 'schema-rule', title: '生成合成 Schema 规则', resource_kind: 'rule',
        target_key: 'synthetic_schema_rule', allowed_tools: ['create_rule'],
        acceptance: '规则完整',
      },
      {
        client_key: 'schema-operation', title: '生成合成 Schema 操作', resource_kind: 'operation',
        target_key: 'synthetic_schema_operation', allowed_tools: ['create_operation'],
        acceptance: '操作完整',
      },
      {
        client_key: 'schema-metric', title: '生成合成 Schema 指标', resource_kind: 'metric',
        target_key: 'synthetic_schema_metric', allowed_tools: ['create_metric'],
        acceptance: '指标完整',
      },
    ],
  }, runId);
  assert.equal(planResponse.statusCode, 200, planResponse.body);
  const plan = planResponse.json().plan;
  assert.equal(planResponse.json().object_source_policy.mode, 'schema');
  assert.equal(planResponse.json().object_source_policy.schema_table_count, 2);
  assert.equal(plan.tasks.length, 5);
  assert.deepEqual(plan.tasks[0].allowed_tools, ['sync_schema_objects']);
  assert.equal(plan.tasks[0].target_key, '__schema__');
  assert.deepEqual(
    plan.tasks[1].allowed_tools,
    ['stage_schema_semantic_changes', 'confirm_schema_semantics'],
  );
  assert.equal(plan.tasks[1].target_key, '__schema_semantics__');
  assert.deepEqual(plan.tasks[1].depends_on, [plan.tasks[0].id]);
  assert.deepEqual(
    plan.tasks.slice(2).map((item: any) => item.resource_kind),
    ['rule', 'operation', 'metric'],
  );
  assert.ok(plan.tasks.slice(2).every((item: any) => (
    item.depends_on.length === 1 && item.depends_on[0] === plan.tasks[1].id
  )));

  const approved = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/agent/change-plans/${plan.id}/respond`,
    payload: { approved: true },
  });
  assert.equal(approved.statusCode, 200, approved.body);

  const blockedDiscard = await tool(app, project.id, 'discard_change_set', {
    plan_id: plan.id,
    reason: '尝试在未获用户同意时缩小生成范围',
  }, runId);
  assert.equal(blockedDiscard.statusCode, 409, blockedDiscard.body);
  assert.equal(blockedDiscard.json().code, 'CHANGE_PLAN_DISCARD_USER_ACTION_REQUIRED');

  const directMapping = await tool(app, project.id, 'write_file', {
    path: 'mappings/object-data-mappings.json',
    content: '{}',
    change_group: 'manual-schema-mapping',
    evidence: [],
  }, runId);
  assert.equal(directMapping.statusCode, 409, directMapping.body);
  assert.equal(directMapping.json().code, 'MANAGED_FILE_REQUIRES_DOMAIN_TOOL');
  assert.equal(directMapping.json().requiredAction, 'redirect_tool');
  assert.equal(directMapping.json().requiredTool, 'sync_schema_objects');
  assert.equal(directMapping.json().targetKind, 'schema_objects');
  assert.equal(directMapping.json().recoveryAction.id, 'resume_schema_object_sync');
  assert.match(directMapping.json().issueId, /^agent-issue-[a-f0-9]{20}$/u);

  const synced = await tool(app, project.id, 'sync_schema_objects', {
    plan_task_id: plan.tasks[0].id,
    expected_revision: listed.json().working_revision,
    mutation_id: 'synthetic-schema-sync-1',
    reason: '按全部合成 Schema Sheet 确定性同步',
  }, runId);
  assert.equal(synced.statusCode, 200, synced.body);
  assert.equal(synced.json().object_count, 2);
  assert.equal(synced.json().removed_derived_object_count, 1);
  assert.deepEqual(synced.json().objects.map((item: any) => item.table), [
    'synthetic_customer', 'synthetic_empty',
  ]);
  assert.ok(synced.json().objects.every((item: any) => item.document_id === schemaDocument.id));
  assert.equal(synced.json().plan.tasks[0].status, 'staged');
  assert.equal(synced.json().plan.expected_revision, synced.json().working_revision);
  const replayedSync = await tool(app, project.id, 'sync_schema_objects', {
    plan_task_id: plan.tasks[0].id,
    expected_revision: listed.json().working_revision,
    mutation_id: 'synthetic-schema-sync-1',
    reason: '按全部合成 Schema Sheet 确定性同步',
  }, runId);
  assert.equal(replayedSync.statusCode, 200, replayedSync.body);
  assert.equal(replayedSync.json().replayed, true);
  assert.equal(replayedSync.json().plan.version, synced.json().plan.version);
  const duplicateSync = await tool(app, project.id, 'sync_schema_objects', {
    plan_task_id: plan.tasks[0].id,
    expected_revision: synced.json().working_revision,
    mutation_id: 'synthetic-schema-sync-duplicate',
    reason: '尝试用新操作标识重复同步',
  }, runId);
  assert.equal(duplicateSync.statusCode, 409, duplicateSync.body);
  assert.equal(duplicateSync.json().code, 'CHANGE_PLAN_TASK_ALREADY_SETTLED');

  const blockedSemanticConfirmation = await tool(app, project.id, 'confirm_schema_semantics', {
    plan_task_id: plan.tasks[1].id,
    expected_revision: synced.json().working_revision,
    mutation_id: 'synthetic-schema-semantics-too-early',
    reason: '验证占位语义不能直接通过',
  }, runId);
  assert.equal(blockedSemanticConfirmation.statusCode, 409, blockedSemanticConfirmation.body);
  assert.equal(blockedSemanticConfirmation.json().code, 'SCHEMA_SEMANTICS_INCOMPLETE');

  const semanticSummaryRoute = await app.inject({
    method: 'GET',
    url: `/api/projects/${project.id}/schema-semantics/summary`,
  });
  assert.equal(semanticSummaryRoute.statusCode, 200, semanticSummaryRoute.body);
  assert.equal(semanticSummaryRoute.json().summary.pass, false);
  const semanticItemsRoute = await app.inject({
    method: 'GET',
    url: `/api/projects/${project.id}/schema-semantics/items?level=error&offset=0&limit=2`,
  });
  assert.equal(semanticItemsRoute.statusCode, 200, semanticItemsRoute.body);
  assert.equal(semanticItemsRoute.json().items.length, 2);
  assert.equal(semanticItemsRoute.json().offset, 0);
  assert.equal(semanticItemsRoute.json().next_offset, 2);
  assert.ok(semanticItemsRoute.json().items.every((item: any) => !('sampleValues' in item)));
  assert.ok(semanticItemsRoute.json().items.every((item: any) => /^sha256:/u.test(item.resource_hash)));

  const semanticStatus = await tool(app, project.id, 'get_schema_semantics', {
    level: 'error', limit: 100,
  }, runId);
  assert.equal(semanticStatus.statusCode, 200, semanticStatus.body);
  assert.equal(semanticStatus.json().summary.pass, false);
  assert.ok(semanticStatus.json().summary.error_count > 0);
  const semanticObjects = await Promise.all(synced.json().objects.map((item: any) => tool(
    app,
    project.id,
    'get_domain_resource',
    { resource_id: item.resource_id, working_revision: synced.json().working_revision },
    runId,
  )));
  const stagedSemantics = await tool(app, project.id, 'stage_schema_semantic_changes', {
    plan_task_id: plan.tasks[1].id,
    expected_revision: synced.json().working_revision,
    mutation_id: 'synthetic-schema-semantics-1',
    reason: '使用合成业务定义补全对象语义',
    objects: semanticObjects.map((response, objectIndex) => {
      const resource = response.json().resource;
      return {
        resource_id: resource.id,
        expected_resource_hash: resource.resource_hash,
        actions: [
          { op: 'set_title', value: objectIndex === 0 ? '合成客户记录' : '合成空记录' },
          { op: 'set_description', value: objectIndex === 0
            ? '保存合成客户的基础记录。'
            : '保存可为空的合成辅助记录。' },
          ...resource.editable_items.fields.map((field: any, fieldIndex: number) => ({
            op: 'update_field',
            field_key: field.key,
            expected_field_hash: field.item_hash,
            changes: {
              cn_name: `合成字段${objectIndex + 1}-${fieldIndex + 1}`,
              note: `第 ${objectIndex + 1} 个合成对象的第 ${fieldIndex + 1} 个字段。`,
            },
          })),
        ],
      };
    }),
  }, runId);
  assert.equal(stagedSemantics.statusCode, 200, stagedSemantics.body);
  assert.equal(stagedSemantics.json().semantic_status.pass, true);
  assert.equal(stagedSemantics.json().plan.tasks[1].status, 'pending');
  const semanticIndex = await tool(app, project.id, 'read_file', { path: 'objects/index.md' }, runId);
  assert.match(semanticIndex.json().content, /合成客户记录/u);
  const semanticDetail = await tool(app, project.id, 'read_file', {
    path: 'objects/synthetic_customer.md',
  }, runId);
  assert.match(semanticDetail.json().content, /合成字段1-1/u);
  assert.match(semanticDetail.json().content, /第 1 个合成对象的第 1 个字段/u);

  const confirmedSemantics = await tool(app, project.id, 'confirm_schema_semantics', {
    plan_task_id: plan.tasks[1].id,
    expected_revision: stagedSemantics.json().working_revision,
    mutation_id: 'synthetic-schema-semantics-confirm-1',
    reason: '确定性复核全部 Schema 业务语义',
  }, runId);
  assert.equal(confirmedSemantics.statusCode, 200, confirmedSemantics.body);
  assert.equal(confirmedSemantics.json().plan.tasks[1].status, 'staged');
  assert.equal(confirmedSemantics.json().relation_suggestion_required, true);

  const downstreamActions = [
    {
      task_id: plan.tasks[2].id,
      tool: 'create_rule',
      arguments: {
        key: 'synthetic_schema_rule',
        title: '合成 Schema 规则',
        description: '只用于验证关系推荐顺序。',
        content: '合成客户记录存在时返回通过。',
        conditions: [{ condition: '客户编号非空', conclusion: '返回通过' }],
        exceptions: [],
        targets: ['object:synthetic_customer'],
        evidence: [`material:${schemaDocument.sourceKey}#synthetic_customer`],
      },
    },
    {
      task_id: plan.tasks[3].id,
      tool: 'create_operation',
      arguments: {
        key: 'synthetic_schema_operation',
        title: '处理合成 Schema 记录',
        description: '读取并处理合成客户记录。',
        inputs: [{ param: 'customer_id', type: 'string', required: true }],
        outputs: [{ field: 'accepted', type: 'boolean' }],
        flow: ['读取合成客户记录', '返回处理结果'],
        references: ['object:synthetic_customer'],
        evidence: [`material:${schemaDocument.sourceKey}#synthetic_customer`],
      },
    },
    {
      task_id: plan.tasks[4].id,
      tool: 'create_metric',
      arguments: {
        key: 'synthetic_schema_metric',
        title: '合成客户记录数',
        description: '统计合成客户记录。',
        unit: '条',
        grain: '客户',
        caliber: 'COUNT(customer_id)',
        boundaries: ['仅统计合成客户记录'],
        object_references: ['object:synthetic_customer'],
        evidence: [`material:${schemaDocument.sourceKey}#synthetic_customer`],
      },
    },
  ];

  const rejectedScopedSuggestion = await tool(app, project.id, 'suggest_object_relations', {
    working_revision: confirmedSemantics.json().working_revision,
    scope: { affectedObjects: ['synthetic_customer'] },
    limit: 20,
  }, runId);
  assert.equal(rejectedScopedSuggestion.statusCode, 409, rejectedScopedSuggestion.body);
  assert.equal(
    rejectedScopedSuggestion.json().code,
    'SCHEMA_RELATION_SUGGESTION_FULL_REQUIRED',
  );

  const relationSuggestion = await tool(app, project.id, 'suggest_object_relations', {
    working_revision: confirmedSemantics.json().working_revision,
    limit: 1,
  }, runId);
  assert.equal(relationSuggestion.statusCode, 200, relationSuggestion.body);
  assert.equal(relationSuggestion.json().hasMore, true);
  assert.ok(relationSuggestion.json().nextCursor);
  assert.equal(relationSuggestion.json().relation_suggestion_recorded, false);
  const pendingRelationSuggestion = getDb().prepare(`SELECT status, total_candidate_count,
      returned_candidate_count FROM agent_schema_relation_suggestions WHERE project_id = ?`)
    .get(project.id) as any;
  assert.deepEqual({ ...pendingRelationSuggestion }, {
    status: 'pending', total_candidate_count: 2, returned_candidate_count: 1,
  });

  const stillBlockedAfterFirstPage = await tool(app, project.id, 'stage_change_set', {
    plan_id: plan.id,
    expected_revision: confirmedSemantics.json().working_revision,
    mutation_id: 'synthetic-schema-downstream-before-last-relation-page',
    actions: downstreamActions,
  }, runId);
  assert.equal(stillBlockedAfterFirstPage.statusCode, 409, stillBlockedAfterFirstPage.body);
  assert.equal(stillBlockedAfterFirstPage.json().code, 'SCHEMA_RELATION_SUGGESTION_REQUIRED');

  const finalRelationSuggestionPage = await tool(app, project.id, 'suggest_object_relations', {
    working_revision: confirmedSemantics.json().working_revision,
    cursor: relationSuggestion.json().nextCursor,
    limit: 1,
  }, runId);
  assert.equal(finalRelationSuggestionPage.statusCode, 200, finalRelationSuggestionPage.body);
  assert.equal(finalRelationSuggestionPage.json().hasMore, false);
  assert.equal(finalRelationSuggestionPage.json().relation_suggestion_recorded, true);
  const completedRelationSuggestion = getDb().prepare(`SELECT status, total_candidate_count,
      returned_candidate_count FROM agent_schema_relation_suggestions WHERE project_id = ?`)
    .get(project.id) as any;
  assert.deepEqual({ ...completedRelationSuggestion }, {
    status: 'completed', total_candidate_count: 2, returned_candidate_count: 2,
  });

  const replayedSemantics = await tool(app, project.id, 'confirm_schema_semantics', {
    plan_task_id: plan.tasks[1].id,
    expected_revision: stagedSemantics.json().working_revision,
    mutation_id: 'synthetic-schema-semantics-confirm-1',
    reason: '确定性复核全部 Schema 业务语义',
  }, runId);
  assert.equal(replayedSemantics.statusCode, 200, replayedSemantics.body);
  assert.equal(replayedSemantics.json().replayed, true);
  assert.equal(replayedSemantics.json().relation_suggestion_required, false);

  const downstream = await tool(app, project.id, 'stage_change_set', {
    plan_id: plan.id,
    expected_revision: confirmedSemantics.json().working_revision,
    mutation_id: 'synthetic-schema-downstream-resources-1',
    actions: downstreamActions,
  }, runId);
  assert.equal(downstream.statusCode, 200, downstream.body);
  const planAfterDownstream = await tool(app, project.id, 'get_change_plan', {
    plan_id: plan.id,
  }, runId);
  assert.equal(planAfterDownstream.statusCode, 200, planAfterDownstream.body);
  assert.deepEqual(planAfterDownstream.json().plan.tasks.slice(2).map((item: any) => item.status), [
    'staged', 'staged', 'staged',
  ]);

  const validated = await tool(app, project.id, 'validate_change_set', {
    plan_id: plan.id,
    summary: '核对全部 Schema Sheet 对象及 mapping',
  }, runId);
  assert.equal(validated.statusCode, 200, validated.body);
  assert.equal(validated.json().plan.tasks[0].status, 'validated');
  const mapping = await tool(app, project.id, 'read_file', {
    path: 'mappings/object-data-mappings.json',
  }, runId);
  assert.equal(mapping.statusCode, 200, mapping.body);
  const parsedMapping = JSON.parse(mapping.json().content);
  assert.equal(Object.keys(parsedMapping.objects).length, 2);
  assert.equal(parsedMapping.objects.synthetic_customer.fields.customer_id.required, true);
  assert.equal(parsedMapping.objects.synthetic_customer.fields.customer_name.required, false);
  const legacy = await tool(app, project.id, 'read_file', { path: 'objects/legacy_derived.md' }, runId);
  assert.equal(legacy.statusCode, 404, legacy.body);

  const validation = validated.json().validation;
  const accepted = await tool(app, project.id, 'request_confirmation', {
    summary: '确认合成 Schema 对象与业务语义',
    change_groups: validation.change_groups,
    expected_candidate_revision: validation.candidate_revision,
    expected_candidate_hash: validation.validation_receipt.candidateHash,
    expected_validator_version: validation.validation_receipt.validatorVersion,
    review: {
      approved: true,
      review_mode: 'full',
      selected_groups: validation.change_groups
        .filter((item: any) => !item.autoApproved)
        .map((item: any) => item.id),
      sampled_groups: [],
    },
  }, runId);
  assert.equal(accepted.statusCode, 200, accepted.body);
  const acceptedRevision = (await artifacts.current(project.id))!.revision.oid;

  const secondRunId = runId;
  const repeatedList = await tool(app, project.id, 'list_domain_resources', { kind: 'object' }, secondRunId);
  const repeatedPlanResponse = await tool(app, project.id, 'create_change_plan', {
    goal: '再次核对相同 Schema',
    mode: 'generation',
    expected_revision: repeatedList.json().working_revision,
    tasks: [{
      client_key: 'schema-repeat', title: '再次同步合成 Schema', resource_kind: 'object',
      target_key: 'synthetic_customer', allowed_tools: ['create_object'], acceptance: '结果保持一致',
    }],
  }, secondRunId);
  assert.equal(repeatedPlanResponse.statusCode, 200, repeatedPlanResponse.body);
  const repeatedPlan = repeatedPlanResponse.json().plan;
  assert.equal(repeatedPlan.status, 'completed');
  assert.equal(repeatedPlan.task_count, 0);
  assert.equal(repeatedPlanResponse.json().object_source_policy.schema_sync_required, false);
  assert.equal(repeatedPlanResponse.json().object_source_policy.schema_semantics_required, false);
  assert.equal(repeatedPlanResponse.json().object_source_policy.reused_schema_sync, true);
  assert.equal(repeatedPlanResponse.json().object_source_policy.reused_schema_semantics, true);
  assert.equal((await artifacts.current(project.id))!.revision.oid, acceptedRevision);

  const storedSchema = getDb().prepare('SELECT tables_json FROM documents WHERE id = ?')
    .get(schemaDocument.id) as { tables_json: string };
  const changedTables = JSON.parse(storedSchema.tables_json);
  changedTables[0].fields[1].required = true;
  getDb().prepare('UPDATE documents SET tables_json = ? WHERE id = ?')
    .run(JSON.stringify(changedTables), schemaDocument.id);
  const changedList = await tool(app, project.id, 'list_domain_resources', { kind: 'object' }, secondRunId);
  const changedPlanResponse = await tool(app, project.id, 'create_change_plan', {
    goal: '显式必填约束变化后重新核对 Schema',
    mode: 'generation',
    expected_revision: changedList.json().working_revision,
    tasks: [{
      client_key: 'schema-constraint-change', title: '应用显式 Schema 约束', resource_kind: 'object',
      target_key: 'synthetic_customer', allowed_tools: ['create_object'], acceptance: '约束保持一致',
    }],
  }, secondRunId);
  assert.equal(changedPlanResponse.statusCode, 200, changedPlanResponse.body);
  assert.equal(changedPlanResponse.json().object_source_policy.schema_sync_required, true);
  assert.equal(changedPlanResponse.json().object_source_policy.schema_semantics_required, true);
  assert.deepEqual(changedPlanResponse.json().plan.tasks.map((task: any) => task.target_key), [
    '__schema__', '__schema_semantics__',
  ]);
});

test('Change Set 最后一项失败时整批不落候选，计划外动作和漏项校验均被阻断', async (t) => {
  const project = createProject({ owner: 'alice', name: '受控原子失败', runtime: 'agent' });
  const app = appFor(t);
  await seedProject(project.id);
  const runId = `run-change-set-failure-${project.id}`;
  const objects = await tool(app, project.id, 'list_domain_resources', { kind: 'object' }, runId);
  const operations = await tool(app, project.id, 'list_domain_resources', { kind: 'operation' }, runId);
  const revision = objects.json().working_revision as string;
  const customer = objects.json().items.find((item: any) => item.key === 'customer');
  const operation = operations.json().items.find((item: any) => item.key === 'render_synthetic_summary');
  const planResponse = await tool(app, project.id, 'create_change_plan', {
    goal: '验证跨资源失败不会留下半成品', mode: 'expert_edit', expected_revision: revision,
    tasks: [
      { client_key: 'customer', title: '修改客户', resource_kind: 'object', target_key: 'customer', allowed_tools: ['patch_object'], acceptance: '说明更新' },
      { client_key: 'operation', title: '修改操作', resource_kind: 'operation', target_key: 'render_synthetic_summary', allowed_tools: ['patch_operation'], acceptance: '说明更新' },
    ],
  }, runId);
  const plan = planResponse.json().plan;

  const outOfScope = await tool(app, project.id, 'stage_change_set', {
    plan_id: plan.id, expected_revision: revision, mutation_id: 'change-set-out-of-scope',
    actions: [{
      task_id: plan.tasks[0].id, tool: 'patch_operation',
      arguments: { resource_id: 'operation:render_synthetic_summary', expected_resource_hash: operation.resource_hash, actions: [{ op: 'set_description', value: '越界修改' }] },
    }],
  }, runId);
  assert.equal(outOfScope.statusCode, 409, outOfScope.body);
  assert.equal(outOfScope.json().code, 'CHANGE_SET_OUT_OF_SCOPE');

  const failed = await tool(app, project.id, 'stage_change_set', {
    plan_id: plan.id, expected_revision: revision, mutation_id: 'change-set-final-action-fails',
    actions: [
      {
        task_id: plan.tasks[0].id, tool: 'patch_object',
        arguments: { resource_id: 'object:customer', expected_resource_hash: customer.resource_hash, actions: [{ op: 'set_description', value: '不应留下的说明' }] },
      },
      {
        task_id: plan.tasks[1].id, tool: 'patch_operation',
        arguments: { resource_id: 'operation:render_synthetic_summary', expected_resource_hash: 'sha256:0000000000000000000000000000000000000000000000000000000000000000', actions: [{ op: 'set_description', value: '失败动作' }] },
      },
    ],
  }, runId);
  assert.equal(failed.statusCode, 409, failed.body);
  assert.equal(failed.json().code, 'RESOURCE_HASH_CONFLICT');

  const after = await tool(app, project.id, 'get_domain_resource', {
    resource_id: 'object:customer', working_revision: revision,
  }, runId);
  assert.equal(after.statusCode, 200, after.body);
  assert.equal(after.json().resource.description, '客户主数据对象。');
  assert.equal(after.json().working_revision, revision);

  const incomplete = await tool(app, project.id, 'validate_change_set', { plan_id: plan.id }, runId);
  assert.equal(incomplete.statusCode, 409, incomplete.body);
  assert.equal(incomplete.json().code, 'CHANGE_PLAN_INCOMPLETE');
  assert.equal(incomplete.json().incomplete_tasks.length, 2);
});

test('Change Set 可按唯一同类目标纠正模型选错的任务 ID', async (t) => {
  const project = createProject({ owner: 'alice', name: '受控任务目标纠正', runtime: 'agent' });
  const app = appFor(t);
  await seedProject(project.id);
  const runId = `run-change-set-task-alignment-${project.id}`;
  const objects = await tool(app, project.id, 'list_domain_resources', { kind: 'object' }, runId);
  const revision = objects.json().working_revision as string;
  const order = objects.json().items.find((item: any) => item.key === 'order');
  const planResponse = await tool(app, project.id, 'create_change_plan', {
    goal: '分别补充客户和订单对象说明', mode: 'expert_edit', expected_revision: revision,
    tasks: [
      { client_key: 'customer', title: '补充客户说明', resource_kind: 'object', target_key: 'customer', allowed_tools: ['patch_object'], acceptance: '客户说明完整' },
      { client_key: 'order', title: '补充订单说明', resource_kind: 'object', target_key: 'order', allowed_tools: ['patch_object'], acceptance: '订单说明完整' },
    ],
  }, runId);
  const plan = planResponse.json().plan;

  const staged = await tool(app, project.id, 'stage_change_set', {
    plan_id: plan.id, expected_revision: revision, mutation_id: 'change-set-align-same-kind-task',
    actions: [{
      task_id: plan.tasks[0].id,
      tool: 'patch_object',
      arguments: {
        resource_id: 'object:order', expected_resource_hash: order.resource_hash,
        actions: [{ op: 'set_description', value: '订单说明由唯一目标任务纠正后更新。' }],
      },
    }],
  }, runId);
  assert.equal(staged.statusCode, 200, staged.body);
  const currentPlan = await tool(app, project.id, 'get_change_plan', { plan_id: plan.id }, runId);
  assert.deepEqual(currentPlan.json().plan.tasks.map((item: any) => item.status), ['pending', 'staged']);
});

test('计划可通过 Core API 原位替换错误目标并重新等待页面批准', async (t) => {
  const project = createProject({ owner: 'alice', name: '合成计划目标替换', runtime: 'agent' });
  const app = appFor(t);
  await seedProject(project.id);
  const runId = `run-plan-replace-${project.id}`;
  const listed = await tool(app, project.id, 'list_domain_resources', { kind: 'object' }, runId);
  const created = await tool(app, project.id, 'create_change_plan', {
    goal: '修正合成对象的计划目标',
    mode: 'expert_edit',
    expected_revision: listed.json().working_revision,
    tasks: [{
      client_key: 'object-description', title: '补充合成客户说明', resource_kind: 'object',
      target_key: 'customer', allowed_tools: ['patch_object'], acceptance: '说明完整',
    }],
  }, runId);
  assert.equal(created.statusCode, 200, created.body);
  const plan = created.json().plan;
  const invalidReplacement = await tool(app, project.id, 'revise_change_plan', {
    plan_id: plan.id,
    expected_version: plan.version,
    reason: '验证泛化替换目标仍会被入口拒绝',
    replace_tasks: [{
      task_id: plan.tasks[0].id,
      title: '补充全部对象说明',
      resource_kind: 'object',
      target_key: 'all_objects',
      allowed_tools: ['patch_object'],
      depends_on_task_ids: [],
      acceptance: '补充对象说明',
    }],
  }, runId);
  assert.equal(invalidReplacement.statusCode, 409, invalidReplacement.body);
  assert.equal(invalidReplacement.json().code, 'CHANGE_PLAN_TARGET_NOT_FOUND');
  const revised = await tool(app, project.id, 'revise_change_plan', {
    plan_id: plan.id,
    expected_version: plan.version,
    reason: '将目标修正为具体合成订单对象',
    replace_tasks: [{
      task_id: plan.tasks[0].id,
      title: '补充合成订单说明',
      resource_kind: 'object',
      target_key: 'order',
      allowed_tools: ['patch_object'],
      depends_on_task_ids: [],
      acceptance: '只补充合成订单对象说明',
    }],
  }, runId);
  assert.equal(revised.statusCode, 200, revised.body);
  assert.equal(revised.json().plan.approval_status, 'pending');
  assert.equal(revised.json().plan.tasks[0].id, plan.tasks[0].id);
  assert.equal(revised.json().plan.tasks[0].target_key, 'order');

  const blockedValidation = await tool(app, project.id, 'validate_change_set', {
    plan_id: plan.id,
  }, runId);
  assert.equal(blockedValidation.statusCode, 409, blockedValidation.body);
  assert.equal(blockedValidation.json().code, 'CHANGE_PLAN_APPROVAL_REQUIRED');
});

test('正常生成计划必须经页面账号批准后才能形成 Change Set', async (t) => {
  const project = createProject({ owner: 'alice', name: '生成计划批准门禁', runtime: 'agent' });
  const app = appFor(t);
  await seedProject(project.id);
  const runId = `run-generation-approval-${project.id}`;
  const listed = await tool(app, project.id, 'list_domain_resources', { kind: 'object' }, runId);
  const revision = listed.json().working_revision as string;
  const customer = listed.json().items.find((item: any) => item.key === 'customer');
  const created = await tool(app, project.id, 'create_change_plan', {
    goal: '根据合成资料生成更新', mode: 'generation', expected_revision: revision,
    tasks: [{
      client_key: 'customer-description', title: '补充客户对象说明', resource_kind: 'object',
      target_key: 'customer', allowed_tools: ['patch_object'], acceptance: '只补充对象说明',
    }],
  }, runId);
  assert.equal(created.statusCode, 200, created.body);
  const plan = created.json().plan;
  assert.equal(plan.approval_status, 'pending');

  const changeSet = {
    plan_id: plan.id,
    expected_revision: revision,
    mutation_id: 'generation-after-visible-approval',
    actions: [{
      task_id: plan.tasks[0].id,
      tool: 'patch_object',
      arguments: {
        resource_id: 'object:customer', expected_resource_hash: customer.resource_hash,
        actions: [{ op: 'set_description', value: '用户批准生成计划后补充的说明。' }],
      },
    }],
  };
  const blocked = await tool(app, project.id, 'stage_change_set', changeSet, runId);
  assert.equal(blocked.statusCode, 409, blocked.body);
  assert.equal(blocked.json().code, 'CHANGE_PLAN_APPROVAL_REQUIRED');

  const approved = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/agent/change-plans/${plan.id}/respond`,
    payload: { approved: true },
  });
  assert.equal(approved.statusCode, 200, approved.body);
  assert.equal(approved.json().plan.approval_status, 'approved');
  assert.equal(approved.json().plan.approved_by, 'alice');

  const staged = await tool(app, project.id, 'stage_change_set', changeSet, runId);
  assert.equal(staged.statusCode, 200, staged.body);
  assert.equal(staged.json().status, 'staged');
});

test('已批准生成计划只能由用户基于最新预览整批放弃', async (t) => {
  const project = createProject({ owner: 'alice', name: '合成计划放弃确认', runtime: 'agent' });
  const app = appFor(t);
  await seedProject(project.id);
  const runId = `run-plan-discard-${project.id}`;
  const listed = await tool(app, project.id, 'list_domain_resources', { kind: 'object' }, runId);
  const revision = listed.json().working_revision as string;
  const customer = listed.json().items.find((item: any) => item.key === 'customer');
  const created = await tool(app, project.id, 'create_change_plan', {
    goal: '验证整份生成计划只能由真实用户放弃',
    mode: 'generation',
    expected_revision: revision,
    tasks: [{
      client_key: 'customer-description', title: '补充合成客户说明', resource_kind: 'object',
      target_key: 'customer', allowed_tools: ['patch_object'], acceptance: '说明完整',
    }],
  }, runId);
  const plan = created.json().plan;
  await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/agent/change-plans/${plan.id}/respond`,
    payload: { approved: true },
  });
  const staged = await tool(app, project.id, 'stage_change_set', {
    plan_id: plan.id,
    expected_revision: revision,
    mutation_id: 'synthetic-plan-discard-change',
    actions: [{
      task_id: plan.tasks[0].id,
      tool: 'patch_object',
      arguments: {
        resource_id: 'object:customer',
        expected_resource_hash: customer.resource_hash,
        actions: [{ op: 'set_description', value: '尚未确认的合成候选说明。' }],
      },
    }],
  }, runId);
  assert.equal(staged.statusCode, 200, staged.body);

  const forged = await tool(app, project.id, 'discard_change_set', {
    plan_id: plan.id,
    reason: 'Agent 尝试自行声称已经获得批准',
    scope_approved: true,
  }, runId);
  assert.equal(forged.statusCode, 409, forged.body);
  assert.equal(forged.json().code, 'CHANGE_PLAN_DISCARD_USER_ACTION_REQUIRED');
  const legacyBypass = await tool(app, project.id, 'discard_rejected_candidate_changes', {}, runId);
  assert.equal(legacyBypass.statusCode, 409, legacyBypass.body);
  assert.equal(legacyBypass.json().code, 'CHANGE_PLAN_DISCARD_USER_ACTION_REQUIRED');

  const preview = await app.inject({
    method: 'GET',
    url: `/api/projects/${project.id}/agent/change-plans/${plan.id}/discard-preview`,
  });
  assert.equal(preview.statusCode, 200, preview.body);
  assert.ok(preview.json().discard_count > 0);
  assert.match(preview.json().candidate_fingerprint, /^sha256:[a-f0-9]{64}$/u);

  const stale = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/agent/change-plans/${plan.id}/discard`,
    payload: {
      expected_version: preview.json().plan_version,
      expected_candidate_fingerprint: 'sha256:0000000000000000000000000000000000000000000000000000000000000000',
      request_id: 'synthetic-discard-stale-preview',
      reason: '验证过期预览不能删除候选',
    },
  });
  assert.equal(stale.statusCode, 409, stale.body);
  assert.equal(stale.json().code, 'CHANGE_PLAN_DISCARD_PREVIEW_STALE');

  const discarded = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/agent/change-plans/${plan.id}/discard`,
    payload: {
      expected_version: preview.json().plan_version,
      expected_candidate_fingerprint: preview.json().candidate_fingerprint,
      request_id: 'synthetic-discard-confirmed',
      reason: '用户查看候选影响后明确放弃合成计划',
    },
  });
  assert.equal(discarded.statusCode, 200, discarded.body);
  assert.equal(discarded.json().plan.status, 'cancelled');
  assert.equal(discarded.json().discarded_count, preview.json().discard_count);
  const decision = getDb().prepare(`SELECT actor, action, status FROM agent_plan_scope_decisions
    WHERE project_id = ? AND plan_id = ?`).get(project.id, plan.id) as any;
  assert.equal(decision.actor, 'alice');
  assert.equal(decision.action, 'discard');
  assert.equal(decision.status, 'consumed');
  const remaining = getDb().prepare(`SELECT COUNT(*) AS count FROM agent_candidate_changes
    WHERE project_id = ? AND run_id = ?`).get(project.id, runId) as { count: number };
  assert.equal(Number(remaining.count), 0);
  const replayedDiscard = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/agent/change-plans/${plan.id}/discard`,
    payload: {
      expected_version: preview.json().plan_version,
      expected_candidate_fingerprint: preview.json().candidate_fingerprint,
      request_id: 'synthetic-discard-confirmed',
      reason: '用户查看候选影响后明确放弃合成计划',
    },
  });
  assert.equal(replayedDiscard.statusCode, 200, replayedDiscard.body);
  assert.equal(replayedDiscard.json().replayed, true);
});
