import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'modeling-mcp-object-scope-'));

const [
  { createProject },
  { getPackageArtifactStore },
  { executeModelingMcpTool },
  { ModelingMcpToolError },
] = await Promise.all([
  import('../src/projects/repo.js'),
  import('../src/artifacts/runtime.js'),
  import('../src/mcp/toolset.js'),
  import('../src/mcp/tool-error.js'),
]);

async function seed(projectId: string) {
  const artifacts = getPackageArtifactStore();
  const initial = await artifacts.initializeDraftPackage(projectId, {
    id: `synthetic-${projectId}`,
    name: '对象复核范围合成包',
    description: '验证对象级 review scope。',
  });
  return artifacts.savePackageVersion(projectId, {
    expectedRevision: initial.revision.oid,
    spec: {
      ...initial.spec,
      objects: [
        { key: 'record_a', title: '记录 A', description: '合成对象 A。', fields: [] },
        { key: 'record_b', title: '记录 B', description: '合成对象 B。', fields: [] },
      ],
    },
    coverage: initial.coverage,
    action: {
      cause: 'object_scope_test_seed',
      summary: '建立对象 scope 测试基线',
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
    tokenId: 'synthetic-object-review-token',
    argumentsValue: args,
  });
}

test('对象级 review 拒绝带入同项目其他对象的待处理修改', async () => {
  const project = createProject({ owner: 'operator_a', name: '对象 scope 冲突', runtime: 'agent' });
  const seeded = await seed(project.id);
  const first = await call(project, 'apply_ontology_object_patch', {
    object_id: 'object:record_a',
    expected_revision: seeded.revision.oid,
    metadata: { aliases: ['synthetic_alias_a'] },
    client_mutation_id: 'scope-record-a',
  });
  const second = await call(project, 'apply_ontology_object_patch', {
    object_id: 'object:record_b',
    expected_revision: first.working_revision,
    metadata: { aliases: ['synthetic_alias_b'] },
    client_mutation_id: 'scope-record-b',
  });

  const mixedStatus = await call(project, 'get_ontology_object_review_status', {
    object_id: 'object:record_a',
  });
  assert.equal(mixedStatus.stage, 'scope_conflict');
  assert.equal(mixedStatus.current.object_scope_exclusive, false);
  assert.deepEqual(mixedStatus.current.conflicting_paths, ['objects/record_b.md']);

  await assert.rejects(
    () => call(project, 'submit_ontology_object_review', {
      object_id: 'object:record_a',
      expected_revision: second.working_revision,
      client_mutation_id: 'scope-review-a',
    }),
    (error: unknown) => {
      assert.ok(error instanceof ModelingMcpToolError);
      assert.equal(error.code, 'OBJECT_REVIEW_SCOPE_CONFLICT');
      assert.deepEqual(error.details.conflicting_paths, ['objects/record_b.md']);
      return true;
    },
  );
});

test('对象 review 状态区分当前对象 pending 与项目其他 candidate', async () => {
  const project = createProject({ owner: 'operator_b', name: '对象 scope 状态', runtime: 'agent' });
  const seeded = await seed(project.id);
  const applied = await call(project, 'apply_ontology_object_patch', {
    object_id: 'object:record_a',
    expected_revision: seeded.revision.oid,
    metadata: { aliases: ['synthetic_alias_a'] },
    client_mutation_id: 'status-record-a',
  });
  await call(project, 'submit_ontology_object_review', {
    object_id: 'object:record_a',
    expected_revision: applied.working_revision,
    client_mutation_id: 'status-review-a',
  });
  const prepared = await call(project, 'prepare_ontology_object_confirmation_groups', {
    object_id: 'object:record_a',
  });
  assert.deepEqual(
    prepared.object_scope.change_group_ids,
    prepared.change_groups.map((group: any) => group.id),
  );
  await call(project, 'preflight_ontology_object_confirmation', {
    object_id: 'object:record_a',
    summary: '确认对象 record_a 修改',
    change_groups: prepared.change_groups,
  });

  const target = await call(project, 'get_ontology_object_review_status', {
    object_id: 'object:record_a',
  });
  assert.equal(target.object_pending_candidate_count, 1);
  assert.equal(target.project_pending_candidate_count, 1);
  assert.equal(target.blocked_by_other_candidate, false);

  const other = await call(project, 'get_ontology_object_review_status', {
    object_id: 'object:record_b',
  });
  assert.equal(other.stage, 'idle');
  assert.equal(other.pending_candidate_count, 0);
  assert.equal(other.object_pending_candidate_count, 0);
  assert.equal(other.project_pending_candidate_count, 1);
  assert.equal(other.blocked_by_other_candidate, true);
});
