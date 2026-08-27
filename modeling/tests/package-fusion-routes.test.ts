import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

import Fastify from 'fastify';

import type { ImportedPackageArchive } from '../src/workflow/package-import.js';
import type { PackageSpec } from '../src/types.js';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'modeling-package-fusion-routes-'));

const [
  { registerPackageFusionRoutes },
  { registerAgentCoreRoutes },
  { createProject },
  { getPackageArtifactStore },
  { getDb },
  { storePackageMergeSource },
  { renderPackage },
  { renderDiagramFiles },
  { config },
] = await Promise.all([
  import('../src/agent/package-fusion-routes.js'),
  import('../src/agent/core-routes.js'),
  import('../src/projects/repo.js'),
  import('../src/artifacts/runtime.js'),
  import('../src/db/index.js'),
  import('../src/agent/package-merge.js'),
  import('../src/workflow/package-render.js'),
  import('../src/flowchart/index.js'),
  import('../src/config.js'),
]);

function appFor(t: test.TestContext) {
  const app = Fastify();
  app.addHook('preHandler', async (request) => {
    request.user = { username: 'alice', role: 'modeler' };
  });
  registerPackageFusionRoutes(app);
  registerAgentCoreRoutes(app);
  t.after(() => app.close());
  return app;
}

function agentTool(
  app: ReturnType<typeof Fastify>,
  projectId: string,
  name: string,
  args: Record<string, unknown> = {},
  runId = `fusion-run-${projectId}`,
) {
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

async function usePassingScriptSandbox(t: test.TestContext) {
  const socketPath = join(mkdtempSync(join(tmpdir(), 'modeling-fusion-script-sandbox-')), 'jiuwenbox.sock');
  const previousSocketPath = config.jiuwenboxSocketPath;
  const server = createServer((request, response) => {
    const chunks: Buffer[] = [];
    request.on('data', (chunk) => chunks.push(Buffer.from(chunk)));
    request.on('end', () => {
      if (request.method === 'POST' && request.url?.endsWith('/exec')) {
        const body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
        const encodedCases = body.command.at(-1);
        const cases = JSON.parse(Buffer.from(encodedCases, 'base64').toString('utf8'));
        const results = cases.map((item: Record<string, unknown>, index: number) => ({
          index, passed: true, actual: item.expected, expected: item.expected,
        }));
        response.setHeader('Content-Type', 'application/json');
        response.end(JSON.stringify({
          exit_code: 0,
          stdout: `__MODELING_RESULT__${JSON.stringify(results)}\n`,
          stderr: '',
        }));
        return;
      }
      if (request.method === 'DELETE') {
        response.statusCode = 204;
        response.end();
        return;
      }
      response.setHeader('Content-Type', 'application/json');
      response.end('{}');
    });
  });
  await new Promise<void>((resolve) => server.listen(socketPath, resolve));
  config.jiuwenboxSocketPath = socketPath;
  t.after(async () => {
    config.jiuwenboxSocketPath = previousSocketPath;
    await new Promise<void>((resolve) => server.close(() => resolve()));
  });
}

function sourceArchive(sourceId: string, objectKey: string): ImportedPackageArchive {
  const spec: PackageSpec = {
    id: `source-${sourceId}`,
    name: `来源包 ${sourceId}`,
    description: `${sourceId} 场景知识包`,
    version: '1.0.0',
    language: 'zh-CN',
    status: 'released',
    defaultProviderAlias: 'pkg',
    objects: [{
      key: objectKey,
      title: '共享对象',
      description: `${sourceId} 的共享对象定义`,
      aliases: ['共享项'],
      sourceRefs: [{ kind: 'table', table: `table_${sourceId}` }],
      fields: [{ key: 'id', cnName: '标识', type: 'string', required: true }],
      primaryKey: ['id'],
    }],
    operations: [{
      key: 'inspect',
      title: '检查共享对象',
      description: `${sourceId} 的检查流程`,
      inputs: [],
      outputs: [],
      flow: ['读取共享对象', '返回检查结果'],
      relatedObjects: [objectKey],
      evidence: ['flow-node:step:process_1:step_1'],
    }],
    rules: [],
    metrics: [],
    scripts: [],
  };
  const diagrams = renderDiagramFiles({
    diagrams: [{
      id: 'process_1',
      title: '共享检查流程',
      nodes: [
        { id: 'start:process_1', label: '开始', kind: 'start' },
        { id: 'step:process_1:step_1', label: '检查', kind: 'action' },
        { id: 'end:process_1', label: '结束', kind: 'end' },
      ],
      edges: [
        { id: 'edge_1', from: 'start:process_1', to: 'step:process_1:step_1', kind: 'sequence' },
        { id: 'edge_2', from: 'step:process_1:step_1', to: 'end:process_1', kind: 'sequence' },
      ],
    }],
  });
  const files = { ...renderPackage(spec, { validateScripts: false }), ...diagrams };
  return {
    spec,
    coverage: { covered: [{ question: `如何检查 ${sourceId}`, operation: 'inspect' }], uncovered: [] },
    files,
    rawFiles: {
      ...Object.fromEntries(Object.entries(files).map(([path, content]) => [
        path,
        new TextEncoder().encode(content),
      ])),
      'assets/source.bin': new Uint8Array([0, 255, sourceId.length]),
    },
    warnings: [],
  };
}

function sourceArchiveWithScript(sourceId: string, objectKey: string): ImportedPackageArchive {
  const archive = sourceArchive(sourceId, objectKey);
  archive.spec.operations[0] = {
    ...archive.spec.operations[0],
    script: { key: 'calculate', required: true },
  };
  archive.spec.scripts = [{
    key: 'calculate', title: '计算脚本', description: '来源计算逻辑', boundOperation: 'inspect',
    runtime: 'python', entrypoint: 'calculate.py', timeoutSeconds: 30,
    parameters: [], outputs: [{ key: 'status', type: 'string' }], boundaries: [],
    sourceCode: [
      'import argparse',
      'import json',
      'parser = argparse.ArgumentParser()',
      'parser.parse_args()',
      'print(json.dumps({"status": "ok"}))',
      '',
    ].join('\n'),
  }];
  const diagrams = Object.fromEntries(Object.entries(archive.files).filter(([path]) => path.startsWith('diagrams/')));
  archive.files = { ...renderPackage(archive.spec, { validateScripts: false }), ...diagrams };
  archive.rawFiles = Object.fromEntries(Object.entries(archive.files).map(([path, content]) => [
    path,
    new TextEncoder().encode(content),
  ]));
  return archive;
}

function decisions(plan: any) {
  return plan.ledger.filter((entry: any) => entry.decision === null).map((entry: any) => ({
    sourceId: entry.sourceId,
    kind: entry.kind,
    key: entry.sourceKey,
    action: 'preserve',
  }));
}

function storedSourceSnapshot(projectId: string) {
  return getDb().prepare(`SELECT s.id, a.fingerprint,
      (SELECT COUNT(*) FROM agent_package_source_files f WHERE f.source_id = s.id) AS file_count,
      (SELECT hex(f.content) FROM agent_package_source_files f
        WHERE f.source_id = s.id AND f.path = 'assets/source.bin') AS binary_hex
    FROM agent_package_sources s
    JOIN agent_package_source_archives a ON a.source_id = s.id
    WHERE s.project_id = ? ORDER BY a.fingerprint`).all(projectId);
}

test('未逐元素裁决时阻断批量融合且不产生候选版本', async (t) => {
  const project = createProject({ owner: 'alice', name: '未裁决融合阻断', runtime: 'agent' });
  const artifacts = getPackageArtifactStore();
  const current = await artifacts.initializeDraftPackage(project.id, {
    id: 'fusion-target', name: '融合目标', description: '验证未裁决阻断',
  });
  const first = storePackageMergeSource(getDb(), {
    projectId: project.id,
    filename: 'alpha.zip',
    uploadedBy: 'alice',
    archive: sourceArchive('alpha', 'Shared'),
  });
  const second = storePackageMergeSource(getDb(), {
    projectId: project.id,
    filename: 'beta.zip',
    uploadedBy: 'alice',
    archive: sourceArchive('beta', 'shared'),
  });
  const app = appFor(t);
  const created = await agentTool(app, project.id, 'create_package_fusion_plan', {
    source_ids: [first.id, second.id],
    output: {
      id: 'fused-package', name: '融合知识包', description: '一次性多来源融合',
    },
  });
  assert.equal(created.statusCode, 200, created.body);
  assert.equal(created.json().status, 'awaiting_decisions');
  assert.equal(created.json().unresolvedCount, 6);
  const replayed = await agentTool(app, project.id, 'create_package_fusion_plan', {
    source_ids: [second.id, first.id],
    output: {
      id: 'fused-package', name: '融合知识包', description: '一次性多来源融合',
    },
  });
  assert.equal(replayed.statusCode, 200, replayed.body);
  assert.equal(replayed.json().id, created.json().id);
  const listed = await agentTool(app, project.id, 'list_package_fusion_plans');
  assert.equal(listed.statusCode, 200, listed.body);
  assert.equal(listed.json().plans.length, 1);
  assert.equal(listed.json().plans[0].id, created.json().id);

  const compiled = await agentTool(app, project.id, 'compile_package_fusion_plan', {
    plan_id: created.json().id,
  });
  assert.equal(compiled.statusCode, 409, compiled.body);
  assert.equal(compiled.json().code, 'FUSION_DECISIONS_REQUIRED');
  assert.equal(compiled.json().unresolved.length, 6);
  assert.equal(await artifacts.countPendingCandidateRevisions(project.id), 0);
  assert.equal((await artifacts.current(project.id))?.revision.oid, current.revision.oid);

  const resolved = await agentTool(app, project.id, 'update_package_fusion_plan', {
    plan_id: created.json().id,
    dispositions: decisions(created.json()),
  });
  assert.equal(resolved.statusCode, 200, resolved.body);
  assert.equal(resolved.json().status, 'ready');
  const candidate = await agentTool(app, project.id, 'compile_package_fusion_plan', {
    plan_id: created.json().id,
  });
  assert.equal(candidate.statusCode, 200, candidate.body);
  assert.equal(candidate.json().status, 'candidate');
  assert.equal(candidate.json().changeGroups[0].id, 'fusion-batch');
  const confirmationGroups = await agentTool(app, project.id, 'prepare_confirmation_groups');
  assert.equal(confirmationGroups.statusCode, 200, confirmationGroups.body);
  assert.equal(confirmationGroups.json().frozen, true);
  assert.equal(confirmationGroups.json().change_groups[0].tier, 'caliber');
  const storedPlan = getDb().prepare(`SELECT status, ledger_json, candidate_id
    FROM agent_package_fusion_plans WHERE id = ?`).get(created.json().id) as any;
  const storedLedger = JSON.parse(storedPlan.ledger_json);
  assert.equal(storedPlan.status, 'candidate');
  assert.equal(storedPlan.candidate_id, candidate.json().candidateId);
  assert.equal(storedLedger.resources.length, 6);
  assert.ok(storedLedger.resources.every((entry: any) => entry.decision === 'preserve' && entry.fusedKey));
  assert.equal(await artifacts.countPendingCandidateRevisions(project.id), 1);
  assert.equal((await artifacts.current(project.id))?.revision.oid, current.revision.oid);
});

test('含脚本的冻结融合候选在展示确认卡前要求当前隔离测试回执', async (t) => {
  const project = createProject({ owner: 'alice', name: '融合脚本预检', runtime: 'agent' });
  await getPackageArtifactStore().initializeDraftPackage(project.id, {
    id: 'fusion-script-target', name: '脚本融合目标', description: '验证确认前脚本门禁',
  });
  const first = storePackageMergeSource(getDb(), {
    projectId: project.id, filename: 'source-a.zip', uploadedBy: 'alice',
    archive: sourceArchiveWithScript('source-a-script', 'ObjectA'),
  });
  const second = storePackageMergeSource(getDb(), {
    projectId: project.id, filename: 'source-b.zip', uploadedBy: 'alice',
    archive: sourceArchive('source-b-script', 'ObjectB'),
  });
  const app = appFor(t);
  const created = await agentTool(app, project.id, 'create_package_fusion_plan', {
    source_ids: [first.id, second.id],
    output: {
      id: 'fusion-script-result', name: '脚本融合结果', description: '保留来源脚本并验证隔离测试',
    },
  });
  assert.equal(created.statusCode, 200, created.body);
  const resolved = await agentTool(app, project.id, 'update_package_fusion_plan', {
    plan_id: created.json().id,
    dispositions: decisions(created.json()),
  });
  assert.equal(resolved.statusCode, 200, resolved.body);
  const compiled = await agentTool(app, project.id, 'compile_package_fusion_plan', {
    plan_id: created.json().id,
  });
  assert.equal(compiled.statusCode, 200, compiled.body);
  const groups = await agentTool(app, project.id, 'prepare_confirmation_groups');
  assert.equal(groups.statusCode, 200, groups.body);

  const blocked = await agentTool(app, project.id, 'request_confirmation_preflight', {
    summary: '确认含脚本的融合候选',
    change_groups: groups.json().change_groups,
  });

  assert.equal(blocked.statusCode, 409, blocked.body);
  assert.equal(blocked.json().code, 'SCRIPT_TEST_REQUIRED');
  assert.ok(blocked.json().blockers.some((path: string) => path.endsWith('.py')));
  assert.equal(getDb().prepare(`SELECT COUNT(*) AS count FROM agent_candidate_validation_receipts
    WHERE candidate_id = ?`).get(compiled.json().candidateId)?.count, 0);
  assert.equal((await getPackageArtifactStore().getCandidateRevision(
    project.id,
    compiled.json().candidateId,
  )).status, 'pending');

  await usePassingScriptSandbox(t);
  const tested = await agentTool(app, project.id, 'run_script_test', {
    script: 'scripts/calculate.py',
    cases: [{ input: {}, expected: { status: 'ok' } }],
  });
  assert.equal(tested.statusCode, 200, tested.body);
  assert.equal(tested.json().passed, 1);

  const ready = await agentTool(app, project.id, 'request_confirmation_preflight', {
    summary: '确认含脚本的融合候选',
    change_groups: groups.json().change_groups,
  });
  assert.equal(ready.statusCode, 200, ready.body);
  assert.equal(getDb().prepare(`SELECT COUNT(*) AS count FROM agent_script_test_receipts
    WHERE project_id = ?`).get(project.id)?.count, 1);
});

test('既有融合计划可增量登记跨包概念并写入候选来源账本', async (t) => {
  const project = createProject({ owner: 'alice', name: '融合概念增量登记', runtime: 'agent' });
  const artifacts = getPackageArtifactStore();
  await artifacts.initializeDraftPackage(project.id, {
    id: 'fusion-concept-target', name: '融合概念目标', description: '验证概念计划唯一持久化',
  });
  const alpha = storePackageMergeSource(getDb(), {
    projectId: project.id, filename: 'alpha.zip', uploadedBy: 'alice',
    archive: sourceArchive('alpha-concept', 'Substation'),
  });
  const beta = storePackageMergeSource(getDb(), {
    projectId: project.id, filename: 'beta.zip', uploadedBy: 'alice',
    archive: sourceArchive('beta-concept', 'substation_ledger'),
  });
  const app = appFor(t);
  const created = await agentTool(app, project.id, 'create_package_fusion_plan', {
    source_ids: [alpha.id, beta.id],
    output: { id: 'fusion-concepts', name: '融合概念知识包', description: '登记跨包语义关系' },
  });
  assert.equal(created.statusCode, 200, created.body);
  assert.deepEqual(created.json().concepts, []);

  const concept = {
    id: 'common.substation', title: '变电站', layer: 'common', decision: 'preserve_views',
    rationale: '一个来源按变电站建模，另一个来源聚合成台账，粒度不同，保留两个场景视图。',
    members: [
      { sourceId: alpha.id, kind: 'object', key: 'Substation', relation: 'overlap' },
      { sourceId: beta.id, kind: 'object', key: 'substation_ledger', relation: 'broader' },
    ],
  };
  const conceptOnly = await app.inject({
    method: 'PATCH',
    url: `/api/projects/${project.id}/agent/fusion-plans/${created.json().id}`,
    payload: { concepts: [concept] },
  });
  assert.equal(conceptOnly.statusCode, 200, conceptOnly.body);
  assert.equal(conceptOnly.json().status, 'awaiting_decisions');
  assert.deepEqual(conceptOnly.json().concepts, [concept]);

  const invalid = await app.inject({
    method: 'PATCH',
    url: `/api/projects/${project.id}/agent/fusion-plans/${created.json().id}`,
    payload: { concepts: [{ ...concept, id: 'missing', members: [
      concept.members[0],
      { ...concept.members[1], key: '不存在的对象' },
    ] }] },
  });
  assert.equal(invalid.statusCode, 400, invalid.body);
  assert.match(invalid.json().reason, /不在本次纳入范围/u);

  const resolved = await app.inject({
    method: 'PATCH',
    url: `/api/projects/${project.id}/agent/fusion-plans/${created.json().id}`,
    payload: { dispositions: decisions(conceptOnly.json()) },
  });
  assert.equal(resolved.statusCode, 200, resolved.body);
  assert.equal(resolved.json().status, 'ready');
  assert.deepEqual(resolved.json().concepts, [concept]);
  const compiled = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/agent/fusion-plans/${created.json().id}/compile`,
  });
  assert.equal(compiled.statusCode, 200, compiled.body);
  const preview = await artifacts.previewCandidateRevision(project.id, compiled.json().candidateId);
  const provenance = JSON.parse(preview.files['mappings/package-fusion.json']);
  assert.equal(provenance.concepts.length, 1);
  assert.equal(provenance.concepts[0].id, concept.id);
});

test('批量融合与来源输入顺序无关并只写待确认候选', async (t) => {
  const project = createProject({ owner: 'alice', name: '稳定批量融合', runtime: 'agent' });
  const artifacts = getPackageArtifactStore();
  const current = await artifacts.initializeDraftPackage(project.id, {
    id: 'fusion-target-order', name: '融合目标', description: '验证来源顺序无关',
  });
  const alpha = storePackageMergeSource(getDb(), {
    projectId: project.id,
    filename: 'alpha.zip',
    uploadedBy: 'alice',
    archive: sourceArchive('alpha', 'Shared'),
  });
  const beta = storePackageMergeSource(getDb(), {
    projectId: project.id,
    filename: 'beta.zip',
    uploadedBy: 'alice',
    archive: sourceArchive('beta', 'shared'),
  });
  const sourceBefore = storedSourceSnapshot(project.id);
  const app = appFor(t);
  const output = {
    id: 'fused-package-order', name: '稳定融合知识包', description: '验证批量融合顺序无关', version: '1.0.0-rc.1',
  };

  const create = (sourceIds: string[]) => app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/agent/fusion-plans`,
    payload: { expectedRevision: current.revision.oid, sourceIds, output },
  });
  const forwardCreated = await create([alpha.id, beta.id]);
  assert.equal(forwardCreated.statusCode, 201, forwardCreated.body);

  const resolveAndCompile = async (created: any) => {
    const resolved = await app.inject({
      method: 'PATCH',
      url: `/api/projects/${project.id}/agent/fusion-plans/${created.id}`,
      payload: { dispositions: decisions(created) },
    });
    assert.equal(resolved.statusCode, 200, resolved.body);
    assert.equal(resolved.json().status, 'ready');
    assert.equal(resolved.json().unresolvedCount, 0);
    const compiled = await app.inject({
      method: 'POST',
      url: `/api/projects/${project.id}/agent/fusion-plans/${created.id}/compile`,
    });
    assert.equal(compiled.statusCode, 200, compiled.body);
    assert.equal(compiled.json().status, 'candidate');
    return compiled.json();
  };
  const forward = await resolveAndCompile(forwardCreated.json());
  const reverseCreated = await create([beta.id, alpha.id]);
  assert.equal(reverseCreated.statusCode, 201, reverseCreated.body);
  assert.notEqual(reverseCreated.json().id, forwardCreated.json().id);
  assert.deepEqual(
    forwardCreated.json().sources.map((source: any) => source.compilerSourceId),
    reverseCreated.json().sources.map((source: any) => source.compilerSourceId),
  );
  const reverse = await resolveAndCompile(reverseCreated.json());

  assert.equal(forward.outputDigest, reverse.outputDigest);
  const forwardPreview = await artifacts.previewCandidateRevision(project.id, forward.candidateId);
  const reversePreview = await artifacts.previewCandidateRevision(project.id, reverse.candidateId);
  assert.deepEqual(forwardPreview.spec, reversePreview.spec);
  assert.deepEqual(forwardPreview.coverage, reversePreview.coverage);
  const operationKeys = new Set(forwardPreview.spec.operations.map((operation) => operation.key));
  assert.ok(forwardPreview.coverage.covered.every((item) => operationKeys.has(item.operation)));
  assert.deepEqual(forwardPreview.files, reversePreview.files);
  assert.deepEqual(forwardPreview.binaryFiles, reversePreview.binaryFiles);
  assert.equal(forwardPreview.candidate.status, 'pending');
  assert.equal(reversePreview.candidate.status, 'pending');
  assert.equal((await artifacts.current(project.id))?.revision.oid, current.revision.oid);

  const accepted = await artifacts.acceptCandidateRevision(project.id, {
    candidateId: forward.candidateId,
    action: {
      cause: 'fusion_confirmed',
      summary: '专家确认批量融合候选',
      actorKind: 'user',
      actorName: 'alice',
    },
  });
  assert.equal(accepted.candidate.status, 'accepted');
  assert.notEqual(accepted.revision.oid, current.revision.oid);
  assert.equal((await artifacts.current(project.id))?.revision.oid, accepted.revision.oid);
  assert.deepEqual(storedSourceSnapshot(project.id), sourceBefore);
});

test('来源范围排除脚本时自动检查引用闭包', async (t) => {
  const project = createProject({ owner: 'alice', name: '融合范围闭包', runtime: 'agent' });
  const current = await getPackageArtifactStore().initializeDraftPackage(project.id, {
    id: 'scope-target', name: '范围目标', description: '验证范围引用闭包',
  });
  const alpha = storePackageMergeSource(getDb(), {
    projectId: project.id, filename: 'alpha-script.zip', uploadedBy: 'alice',
    archive: sourceArchiveWithScript('alpha-scope', 'Alpha'),
  });
  const beta = storePackageMergeSource(getDb(), {
    projectId: project.id, filename: 'beta.zip', uploadedBy: 'alice',
    archive: sourceArchive('beta-scope', 'Beta'),
  });
  const app = appFor(t);
  const created = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/agent/fusion-plans`,
    payload: {
      expectedRevision: current.revision.oid,
      sourceSelections: [
        { sourceId: alpha.id, scopes: ['objects', 'operations', 'diagrams'] },
        { sourceId: beta.id, scopes: ['objects', 'operations', 'diagrams'] },
      ],
      output: { id: 'scope-fusion', name: '范围融合', description: '排除脚本范围', version: '1.0.0-rc.1' },
    },
  });
  assert.equal(created.statusCode, 409, created.body);
  assert.equal(created.json().code, 'FUSION_SCOPE_NOT_CLOSED');
  assert.ok(created.json().dependencies.some((item: any) => item.requires === 'scripts/calculate'));

  const valid = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/agent/fusion-plans`,
    payload: {
      expectedRevision: current.revision.oid,
      sourceSelections: [
        { sourceId: alpha.id, scopes: ['objects', 'diagrams'] },
        { sourceId: beta.id, scopes: ['objects', 'operations', 'diagrams'] },
      ],
      output: { id: 'scope-fusion-valid', name: '闭合范围融合', description: '同时排除依赖脚本的操作', version: '1.0.0-rc.1' },
    },
  });
  assert.equal(valid.statusCode, 201, valid.body);
  assert.ok(valid.json().ledger.some((entry: any) => entry.kind === 'script' && entry.decision === 'exclude_scope'));
  const resolved = await app.inject({
    method: 'PATCH',
    url: `/api/projects/${project.id}/agent/fusion-plans/${valid.json().id}`,
    payload: { dispositions: decisions(valid.json()) },
  });
  assert.equal(resolved.statusCode, 200, resolved.body);
  const compiled = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/agent/fusion-plans/${valid.json().id}/compile`,
  });
  assert.equal(compiled.statusCode, 200, compiled.body);
  const reviewEvent = getDb().prepare(`SELECT event_type, payload_json
    FROM agent_project_event_outbox WHERE project_id = ? AND event_type = 'fusion_review'`)
    .get(project.id) as { event_type: string; payload_json: string };
  assert.equal(reviewEvent.event_type, 'fusion_review');
  assert.equal(JSON.parse(reviewEvent.payload_json).candidateId, compiled.json().candidateId);
  const preview = await getPackageArtifactStore().previewCandidateRevision(project.id, compiled.json().candidateId);
  assert.equal(preview.spec.scripts?.length, 0);
  const provenance = JSON.parse(preview.files['mappings/package-fusion.json']);
  assert.ok(provenance.resources.some((entry: any) => (
    entry.sourceKey === 'calculate' && entry.kind === 'script' && entry.decision === 'exclude_scope'
  )));
  assert.ok(provenance.resources.every((entry: any) => entry.sourceRecordId === undefined));
});

test('归并和排除裁决真实改变候选并完整记录 provenance', async (t) => {
  const project = createProject({ owner: 'alice', name: '三种融合裁决', runtime: 'agent' });
  const artifacts = getPackageArtifactStore();
  const current = await artifacts.initializeDraftPackage(project.id, {
    id: 'decision-target', name: '裁决目标', description: '验证真实裁决',
  });
  const alpha = storePackageMergeSource(getDb(), {
    projectId: project.id, filename: 'alpha.zip', uploadedBy: 'alice',
    archive: sourceArchive('alpha-decision', 'Shared'),
  });
  const beta = storePackageMergeSource(getDb(), {
    projectId: project.id, filename: 'beta.zip', uploadedBy: 'alice',
    archive: sourceArchive('beta-decision', 'shared'),
  });
  const app = appFor(t);
  const created = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/agent/fusion-plans`,
    payload: {
      expectedRevision: current.revision.oid,
      sourceIds: [alpha.id, beta.id],
      output: { id: 'decision-fusion', name: '裁决融合', description: '验证三种真实去向', version: '1.0.0-rc.1' },
    },
  });
  assert.equal(created.statusCode, 201, created.body);
  const dispositions = decisions(created.json()).map((item: any) => {
    if (item.sourceId === beta.id && item.kind === 'object') return {
      ...item,
      action: 'canonicalize', targetSourceId: alpha.id, targetKey: 'Shared',
      reason: '专家确认两个对象字段与口径等价。',
    };
    if (item.sourceId === beta.id && item.kind === 'operation') return {
      ...item,
      action: 'exclude', reason: '该来源操作不属于本次交付场景。',
    };
    return item;
  });
  const resolved = await app.inject({
    method: 'PATCH',
    url: `/api/projects/${project.id}/agent/fusion-plans/${created.json().id}`,
    payload: { dispositions },
  });
  assert.equal(resolved.statusCode, 200, resolved.body);
  assert.equal(resolved.json().status, 'ready');
  const compiled = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/agent/fusion-plans/${created.json().id}/compile`,
  });
  assert.equal(compiled.statusCode, 200, compiled.body);
  const preview = await artifacts.previewCandidateRevision(project.id, compiled.json().candidateId);
  assert.equal(preview.spec.objects.length, 1);
  assert.equal(preview.spec.operations.length, 1);
  const resources = compiled.json().ledger.resources;
  const canonical = resources.find((entry: any) => entry.sourceRecordId === beta.id && entry.kind === 'object');
  const excluded = resources.find((entry: any) => entry.sourceRecordId === beta.id && entry.kind === 'operation');
  assert.equal(canonical.decision, 'canonicalize');
  assert.equal(canonical.disposition, 'canonicalized');
  assert.ok(canonical.provenance);
  assert.equal(excluded.decision, 'exclude');
  assert.equal(excluded.disposition, 'excluded');
  assert.equal(excluded.fusedKey, '');
  assert.ok(excluded.provenance);
  const provenance = JSON.parse(preview.files['mappings/package-fusion.json']);
  assert.ok(provenance.resources.some((entry: any) => entry.decision === 'canonicalize'));
  assert.ok(provenance.resources.some((entry: any) => entry.decision === 'exclude'));
});
