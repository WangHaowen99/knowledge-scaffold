import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

import Fastify from 'fastify';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'modeling-studio-read-'));

const [
  { registerAgentCoreRoutes },
  { StudioReadService, STUDIO_SCHEMA },
  { createProject },
  { getPackageArtifactStore },
  { getDb },
  { renderPackage },
] = await Promise.all([
  import('../src/agent/core-routes.js'),
  import('../src/agent/studio-read-service.js'),
  import('../src/projects/repo.js'),
  import('../src/artifacts/runtime.js'),
  import('../src/db/index.js'),
  import('../src/workflow/package-render.js'),
]);

function fixtureSpec() {
  return {
    id: 'studio-fixture',
    name: '库存知识包',
    description: '验证 Studio 固定投影',
    version: '0.1.0',
    language: 'zh-CN',
    status: 'draft',
    defaultProviderAlias: 'data',
    objects: [
      {
        key: 'inventory',
        title: '库存',
        description: '库存余额与可用量。',
        aliases: ['存量'],
        fields: [{ key: 'available', cnName: '可用量', type: 'number', required: true }],
        queryBoundary: '只读取已确认库存记录。',
        relations: [{ relation: '存放于', target: 'warehouse', note: '库存所属仓库' }],
      },
      {
        key: 'warehouse',
        title: '仓库',
        description: '保存库存的地点。',
        fields: [{ key: 'code', cnName: '仓库编码', type: 'string', required: true }],
      },
    ],
    operations: [{
      key: 'replenish',
      title: '补货建议',
      description: '根据库存和安全库存生成补货建议。',
      inputs: [{ param: 'sku', cnName: '商品', type: 'string', required: true }],
      outputs: [{ field: 'quantity', cnName: '补货量', type: 'number' }],
      flow: ['读取库存', '应用安全库存规则', '计算建议量'],
      relatedObjects: ['inventory'],
      usesRules: ['safety-stock'],
      usesMetrics: ['available-rate'],
      competencyQuestion: '库存不足时应该补多少货？',
      evidence: ['user-decision:studio-fixture'],
    }],
    rules: [{
      key: 'safety-stock',
      title: '安全库存判断',
      description: '判断当前库存是否低于安全线。',
      appliesToObjects: ['inventory'],
      content: '可用量低于安全库存时触发补货。',
      conditions: [{ condition: '可用量 < 安全库存', conclusion: '需要补货' }],
      evidence: ['user-decision:studio-fixture'],
    }],
    metrics: [{
      key: 'available-rate',
      title: '库存可用率',
      description: '可用库存占总库存的比例。',
      unit: '%',
      grain: '仓库、商品、日',
      usesObjects: ['inventory'],
      caliber: '可用量除以总库存。',
      boundaries: ['总库存为零时不计算。'],
      evidence: ['user-decision:studio-fixture'],
    }],
    scripts: [],
  } as any;
}

function workingPackage(revision: string) {
  return {
    workingRevision: revision,
    baseRevision: 'git-base',
    candidateRevision: null,
    files: renderPackage(fixtureSpec()),
  };
}

function fusionWorkingPackage(revision: string) {
  const files = renderPackage(fixtureSpec());
  files['mappings/package-fusion.json'] = `${JSON.stringify({
    schema_version: '0.1',
    resources: [{
      sourceId: 'source-a', kind: 'object', sourceKey: 'stock', fusedKey: 'inventory',
    }, {
      sourceId: 'source-b', kind: 'object', sourceKey: 'warehouse', fusedKey: 'warehouse',
    }],
    concepts: [{
      id: 'inventory-view',
      title: '库存业务视图',
      layer: 'parameterized',
      decision: 'preserve_views',
      rationale: '保留来源口径，用共同概念导航。',
      members: [
        { sourceId: 'source-a', kind: 'object', key: 'stock', relation: 'equivalent' },
        { sourceId: 'source-b', kind: 'object', key: 'warehouse', fusedKey: 'warehouse', relation: 'overlap' },
      ],
    }],
  }, null, 2)}\n`;
  return {
    workingRevision: revision,
    baseRevision: 'git-base',
    candidateRevision: null,
    files,
  };
}

function appFor(t: test.TestContext, username = 'alice') {
  const app = Fastify();
  app.addHook('preHandler', async (request) => {
    request.user = { username, role: 'modeler' };
  });
  registerAgentCoreRoutes(app);
  t.after(() => app.close());
  return app;
}

test('Studio 固定 registry 与元素、关系、图谱共享稳定标识', () => {
  assert.deepEqual(STUDIO_SCHEMA.kinds.map((item) => item.kind), [
    'object', 'operation', 'rule', 'metric', 'concept',
  ]);
  assert.equal(STUDIO_SCHEMA.capabilities.update, true);

  const studio = new StudioReadService(workingPackage('revision-a'));
  const firstPage = studio.list({ sort: 'kind', limit: 2 });
  assert.equal(firstPage.total, 5);
  assert.equal(firstPage.items.length, 2);
  assert.ok(firstPage.nextCursor);
  const secondPage = studio.list({ sort: 'kind', limit: 2, cursor: firstPage.nextCursor });
  assert.equal(secondPage.items.length, 2);
  assert.equal(new Set([...firstPage.items, ...secondPage.items].map((item) => item.id)).size, 4);

  const aliasResult = studio.list({ kind: 'object', query: '存量' });
  assert.deepEqual(aliasResult.items.map((item) => item.id), ['object:inventory']);

  const operation = studio.detail('operation:replenish').element;
  assert.deepEqual(operation.relations.map((relation) => relation.targetId).sort(), [
    'metric:available-rate', 'object:inventory', 'rule:safety-stock',
  ]);
  assert.ok(operation.relations.every((relation) => relation.targetExists));

  const graph = studio.graph();
  assert.equal(graph.nodes.length, 5);
  assert.ok(graph.nodes.some((node) => node.id === operation.id));
  assert.ok(graph.edges.some((edge) => (
    edge.source === operation.id && edge.target === 'rule:safety-stock'
  )));
  assert.ok(graph.nodes.some((node) => node.id === 'object:warehouse'));
  assert.ok(Math.max(...graph.nodes.map((node) => node.x)) - Math.min(...graph.nodes.map((node) => node.x)) <= 720);
  assert.ok(Math.max(...graph.nodes.map((node) => node.y)) - Math.min(...graph.nodes.map((node) => node.y)) <= 644);

  const nextRevisionGraph = new StudioReadService(workingPackage('revision-b')).graph();
  const beforeNode = graph.nodes.find((node) => node.id === 'operation:replenish')!;
  const afterNode = nextRevisionGraph.nodes.find((node) => node.id === 'operation:replenish')!;
  assert.deepEqual(afterNode, beforeNode);

  assert.throws(
    () => new StudioReadService(workingPackage('revision-b')).list({ cursor: firstPage.nextCursor }),
    (error: any) => error.code === 'WORKING_REVISION_CHANGED',
  );
});

test('Studio 将融合 ledger 概念投影为只读元素并指向真实 fusedKey', () => {
  const conceptSchema = STUDIO_SCHEMA.kinds.find((item) => item.kind === 'concept')!;
  assert.deepEqual(conceptSchema.capabilities, {
    create: false, update: false, delete: false, batch: false,
  });

  const studio = new StudioReadService(fusionWorkingPackage('revision-fusion'));
  const concepts = studio.list({ kind: 'concept' });
  assert.equal(concepts.total, 1);
  assert.deepEqual(concepts.items.map((item) => item.id), ['concept:inventory-view']);

  const concept = studio.detail('concept:inventory-view').element;
  assert.equal(concept.values.layer, 'parameterized');
  assert.equal(concept.sourcePath, 'mappings/package-fusion.json');
  assert.deepEqual(concept.relations.map((relation) => ({
    targetId: relation.targetId,
    targetExists: relation.targetExists,
  })), [
    { targetId: 'object:inventory', targetExists: true },
    { targetId: 'object:warehouse', targetExists: true },
  ]);
  const graph = studio.graph();
  assert.ok(graph.nodes.some((node) => node.id === concept.id));
  assert.equal(graph.edges.filter((edge) => edge.source === concept.id).length, 2);
});

test('Studio 路由读取候选覆盖层并用 workingRevision 约束跨视图一致性', async (t) => {
  const project = createProject({ owner: 'alice', name: 'Studio 候选读取', runtime: 'agent' });
  const app = appFor(t);

  const schema = await app.inject({
    method: 'GET',
    url: `/api/projects/${project.id}/studio/schema`,
  });
  assert.equal(schema.statusCode, 200, schema.body);
  assert.equal(schema.json().schemaVersion, 'studio-v3');
  const cachedSchema = await app.inject({
    method: 'GET',
    url: `/api/projects/${project.id}/studio/schema`,
    headers: { 'if-none-match': schema.headers.etag! },
  });
  assert.equal(cachedSchema.statusCode, 304, cachedSchema.body);

  const empty = await app.inject({
    method: 'GET',
    url: `/api/projects/${project.id}/studio/elements`,
  });
  assert.equal(empty.statusCode, 200, empty.body);
  assert.equal(empty.json().total, 0);
  const baseRevision = empty.json().workingRevision as string;

  const now = new Date().toISOString();
  const runId = `studio-${project.id}`;
  getDb().prepare(`INSERT INTO agent_candidate_workspaces (
    project_id, run_id, owner, base_revision_oid, candidate_id, lease_expires_at,
    created_at, updated_at
  ) VALUES (?,?,?,?,NULL,?,?,?)`).run(
    project.id,
    runId,
    'alice',
    baseRevision,
    new Date(Date.now() + 60_000).toISOString(),
    now,
    now,
  );

  const unchangedWorkspace = await app.inject({
    method: 'GET',
    url: `/api/projects/${project.id}/studio/elements`,
  });
  assert.equal(unchangedWorkspace.statusCode, 200, unchangedWorkspace.body);
  assert.equal(unchangedWorkspace.json().workingRevision, baseRevision);

  const rendered = renderPackage(fixtureSpec());
  const insert = getDb().prepare(`INSERT INTO agent_candidate_changes (
    project_id, run_id, path, kind, content, group_id, evidence_json, reason, updated_at
  ) VALUES (?,?,?,'write',?,'studio-fixture','[]',NULL,?)`);
  for (const path of [
    'objects/inventory.md',
    'objects/warehouse.md',
    'operations/replenish.md',
    'rules/safety-stock.md',
    'metrics/available-rate.md',
  ]) {
    insert.run(project.id, runId, path, rendered[path], now);
  }

  const list = await app.inject({
    method: 'GET',
    url: `/api/projects/${project.id}/studio/elements?query=${encodeURIComponent('库存')}`,
  });
  assert.equal(list.statusCode, 200, list.body);
  assert.equal(list.json().total, 5);
  assert.ok(list.json().items.some((item: any) => item.id === 'object:inventory'));
  assert.match(list.json().workingRevision, /^sha256:[a-f0-9]{64}$/u);
  assert.equal(list.headers.etag, `"${list.json().workingRevision}"`);
  const workingRevision = list.json().workingRevision as string;

  const detail = await app.inject({
    method: 'GET',
    url: `/api/projects/${project.id}/studio/elements/${encodeURIComponent('operation:replenish')}`
      + `?workingRevision=${encodeURIComponent(workingRevision)}`,
  });
  assert.equal(detail.statusCode, 200, detail.body);
  assert.equal(detail.json().workingRevision, workingRevision);
  assert.equal(detail.json().element.id, 'operation:replenish');
  assert.equal(detail.json().element.values.flow.length, 3);

  const graph = await app.inject({
    method: 'GET',
    url: `/api/projects/${project.id}/studio/graph?workingRevision=${encodeURIComponent(workingRevision)}`,
  });
  assert.equal(graph.statusCode, 200, graph.body);
  assert.equal(graph.json().workingRevision, workingRevision);
  assert.equal(graph.json().nodes.length, 5);
  assert.ok(graph.json().nodes.some((node: any) => node.id === detail.json().element.id));

  const stale = await app.inject({
    method: 'GET',
    url: `/api/projects/${project.id}/studio/graph?workingRevision=${encodeURIComponent(baseRevision)}`,
  });
  assert.equal(stale.statusCode, 409, stale.body);
  assert.equal(stale.json().code, 'WORKING_REVISION_CHANGED');
  assert.equal(stale.json().actualRevision, workingRevision);

  const invalidKind = await app.inject({
    method: 'GET',
    url: `/api/projects/${project.id}/studio/elements?kind=script`,
  });
  assert.equal(invalidKind.statusCode, 400, invalidKind.body);
  assert.equal(invalidKind.json().code, 'INVALID_STUDIO_QUERY');

  const foreignApp = appFor(t, 'bob');
  const forbidden = await foreignApp.inject({
    method: 'GET',
    url: `/api/projects/${project.id}/studio/elements`,
  });
  assert.equal(forbidden.statusCode, 404, forbidden.body);
});
