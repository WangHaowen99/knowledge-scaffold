import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { normalizeDataSourceInput, redactProviderConfig } from '../src/evaluation/data-source.js';
import {
  createReadyEvaluationProject, jsonResponse, neo4jConfig, neo4jInput, nl2sqlInput,
} from './helpers/data-source-fixtures.js';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'modeling-data-source-'));

test('data source：规范化连接输入并校验成对隔离字段', () => {
  const normalized = normalizeDataSourceInput(neo4jInput({
    uri: ' bolt://neo4j:7687 ',
    username: ' neo4j ',
    database: ' neo4j ',
    workspace_value: 'workspace-001',
  }));
  assert.deepEqual(normalized, neo4jInput({ workspace_value: 'workspace-001' }));
  assert.throws(() => normalizeDataSourceInput(neo4jInput({ workspace_value: '' })),
    /Workspace 隔离字段和值必须同时填写/);
  assert.deepEqual(normalizeDataSourceInput({
    mode: 'none', provider: 'neo4j', config: { password: 'must-not-survive' },
  }), { mode: 'none' });
  assert.deepEqual(normalizeDataSourceInput(nl2sqlInput({
    scene: ' evaluation ',
    evaluation_workspace_id: ' nl2sql-eval ',
  })), nl2sqlInput());
  assert.throws(() => normalizeDataSourceInput(nl2sqlInput({ scene: '' })), /Scene 不能为空/);
  assert.throws(() => normalizeDataSourceInput(nl2sqlInput({ scene: 'invalid-scene' })),
    /Scene 格式非法/);
});

test('data source：公开配置移除密钥但保留已配置标记', () => {
  assert.deepEqual(redactProviderConfig('neo4j', {
    uri: 'bolt://neo4j:7687', username: 'neo4j', password: 'secret', database: 'neo4j',
  }), {
    uri: 'bolt://neo4j:7687', username: 'neo4j', database: 'neo4j', has_password: true,
  });
  assert.deepEqual(redactProviderConfig('nl2sql_template', {
    url: 'http://nl2sql:8080', token: 'token-value', scene: 'demo',
  }), {
    url: 'http://nl2sql:8080', scene: 'demo', has_token: true,
  });
});

test('评测工作空间：Neo4j 使用 Workspace 隔离值且拒绝 consumer 非法 ID', async () => {
  const workspace = await import('../src/evaluation/shadow-workspace.js');
  assert.equal(workspace.evaluationWorkspaceId('project-a', {
    mode: 'connection', provider: 'neo4j', config: { workspace_value: 'test' },
  }), 'test');
  assert.equal(workspace.evaluationWorkspaceId('project-a', {
    mode: 'connection', provider: 'nl2sql_template',
    config: { evaluation_workspace_id: 'nl2sql-eval' },
  }), 'nl2sql-eval');
  for (const invalidId of ['Test Space', '1test', '测试', `a${'b'.repeat(64)}`]) {
    assert.throws(() => workspace.evaluationWorkspaceId('project-a', {
      mode: 'connection', provider: 'neo4j', config: { workspace_value: invalidId },
    }), /评测工作空间 ID 格式非法/);
  }
  assert.equal(workspace.evaluationWorkspaceId('project-a', {
    mode: 'connection', provider: 'neo4j', config: { workspace_value: `a${'b'.repeat(63)}` },
  }), `a${'b'.repeat(63)}`);
});

test('评测工作空间：隔离值对应工作空间不存在时按该 ID 和名称创建', async () => {
  const { ensureEvaluationWorkspace } = await import('../src/evaluation/shadow-workspace.js');
  const calls: Array<{ method: string; path: string; body?: unknown }> = [];
  const workspace = await ensureEvaluationWorkspace({
    projectId: 'project-a',
    workspaceId: 'test',
    token: 'access-token',
    request: async (_token, path, options: any = {}) => {
      const method = options.method ?? 'GET';
      const body = options.body ? JSON.parse(options.body) : undefined;
      calls.push({ method, path, body });
      return method === 'GET' ? jsonResponse({ workspaces: [] }) : jsonResponse(body, 201);
    },
  });
  assert.deepEqual(workspace, { id: 'test', created: true });
  assert.deepEqual(calls, [
    { method: 'GET', path: '/agentic/workspaces', body: undefined },
    {
      method: 'POST',
      path: '/agentic/workspaces',
      body: { id: 'test', name: 'test', description: '知识包建模工具评测使用，来源项目 project-a' },
    },
  ]);
});

test('评测工作空间：同 ID 存在但当前用户不可见时返回明确错误', async () => {
  const { ensureEvaluationWorkspace } = await import('../src/evaluation/shadow-workspace.js');
  await assert.rejects(ensureEvaluationWorkspace({
    projectId: 'project-a',
    workspaceId: 'test',
    token: 'access-token',
    request: async (_token, _path, options: any = {}) => (
      (options.method ?? 'GET') === 'GET'
        ? jsonResponse({ workspaces: [] })
        : jsonResponse({ detail: "工作空间 ID 'test' 已存在" }, 400)
    ),
  }), /评测工作空间 test 已存在，但当前用户不可访问/);
});

test('data source repository：只持久化 modeling 项目作用域与脱敏配置', async () => {
  const { saveProjectDataSource, getProjectDataSource } = await import('../src/evaluation/data-source-repo.js');
  const { createProject } = await import('../src/projects/repo.js');
  const { getDb } = await import('../src/db/index.js');
  const project = createProject({ owner: 'alice', name: '数据源测试' });
  saveProjectDataSource({
    projectId: project.id,
    mode: 'connection',
    provider: 'neo4j',
    workspaceId: project.id,
    ontologyId: 'ontology-001',
    config: neo4jConfig({
      workspace_value: 'workspace-001', password: 'must-not-persist', has_password: true,
    }),
  });

  const stored = getProjectDataSource(project.id);
  assert.equal(stored?.workspaceId, project.id);
  assert.equal(stored?.ontologyId, 'ontology-001');
  assert.equal(stored?.config.has_password, true);
  assert.equal('password' in (stored?.config ?? {}), false);
  const raw = getDb().prepare('SELECT public_config_json FROM project_data_sources WHERE project_id = ?')
    .get(project.id) as { public_config_json: string };
  assert.equal(raw.public_config_json.includes('must-not-persist'), false);
});

test('data source service：本地直连测试后同步并保存脱敏配置', async () => {
  const { createProject } = await import('../src/projects/repo.js');
  const { insertPackageVersion } = await import('../src/workflow/m2-repo.js');
  const { configureProjectDataSource } = await import('../src/evaluation/data-source-service.js');
  const project = createProject({ owner: 'alice', name: '连接配置测试' });
  insertPackageVersion(project.id, '1.0.0', {
    id: 'ontology-001', name: '连接配置测试', description: '连接配置测试', version: '1.0.0',
    language: 'zh-CN', defaultProviderAlias: 'graph', objects: [], operations: [],
  }, { covered: [], uncovered: [] });
  const input = neo4jInput();
  const tested: unknown[] = [];
  const synced: any[] = [];
  const saved = await configureProjectDataSource({
    projectId: project.id,
    token: 'access-token',
    input,
    testConnection: async (source) => { tested.push(source); },
    syncConnection: async (options) => {
      synced.push(options);
      return { workspaceId: options.workspaceId, ontologyId: 'ontology-001' };
    },
  });

  assert.deepEqual(tested, [input]);
  assert.deepEqual(synced, [{
    projectId: project.id, workspaceId: 'test', ontologyId: 'ontology-001',
    token: 'access-token', source: input,
  }]);
  assert.equal(saved.workspaceId, 'test');
  assert.equal(saved.ontologyId, 'ontology-001');
  assert.equal(saved.config.has_password, true);
  assert.equal('password' in saved.config, false);
});

test('data source service：校验隔离边界且 none 模式跳过外部调用', async () => {
  const { createProject } = await import('../src/projects/repo.js');
  const { insertPackageVersion } = await import('../src/workflow/m2-repo.js');
  const { configureProjectDataSource } = await import('../src/evaluation/data-source-service.js');
  const { shadowWorkspaceId } = await import('../src/evaluation/shadow-workspace.js');
  const invalidCases = [{
      label: 'Workspace', ontologyId: 'ontology-a',
      input: neo4jInput({ workspace_value: 'Test Space', ontology_value: 'ontology-a' }),
      error: /评测工作空间 ID 格式非法/,
    },
    {
      label: 'Ontology', ontologyId: 'expected-ontology',
      input: neo4jInput({ ontology_value: 'other-ontology' }),
      error: /Ontology 隔离值必须为 expected-ontology/,
    },
  ];
  for (const item of invalidCases) {
    const project = createProject({ owner: 'alice', name: `${item.label} 隔离校验` });
    insertPackageVersion(project.id, '1.0.0', {
      id: item.ontologyId, name: item.label, description: item.label, version: '1.0.0',
      language: 'zh-CN', defaultProviderAlias: 'graph', objects: [], operations: [],
    }, { covered: [], uncovered: [] });
    let called = false;
    await assert.rejects(configureProjectDataSource({
      projectId: project.id,
      token: 'access-token',
      input: item.input,
      testConnection: async () => { called = true; },
      syncConnection: async () => {
        called = true;
        return { workspaceId: 'unexpected', ontologyId: 'unexpected' };
      },
    }), item.error);
    assert.equal(called, false);
  }

  const project = createProject({ owner: 'alice', name: '无连接测试' });
  let called = false;
  const saved = await configureProjectDataSource({
    projectId: project.id,
    token: 'access-token',
    input: { mode: 'none' },
    testConnection: async () => { called = true; },
    syncConnection: async () => {
      called = true;
      throw new Error('mode=none 不应同步 consumer 连接');
    },
  });
  assert.equal(called, false);
  assert.equal(saved.mode, 'none');
  assert.equal(saved.workspaceId, shadowWorkspaceId(project.id));
  assert.equal(saved.ontologyId, undefined);
});

test('direct connection tester：Neo4j 选择 database、执行探针并关闭资源', async () => {
  const { testProjectDataSourceConnection } = await import('../src/evaluation/data-source-connection-test.js');
  const calls: Array<{ database?: string; query?: string }> = [];
  let sessionClosed = false;
  let driverClosed = false;
  await testProjectDataSourceConnection(neo4jInput({ database: 'domain' }), {
    neo4jDriverFactory: (uri, username, password) => {
      assert.deepEqual({ uri, username, password }, {
        uri: 'bolt://neo4j:7687', username: 'neo4j', password: 'secret',
      });
      return {
        session: ({ database }) => ({
          run: async (query) => { calls.push({ database, query }); },
          close: async () => { sessionClosed = true; },
        }),
        close: async () => { driverClosed = true; },
      };
    },
  });
  assert.deepEqual(calls, [{ database: 'domain', query: 'RETURN 1 AS ok' }]);
  assert.equal(sessionClosed, true);
  assert.equal(driverClosed, true);
});

test('direct connection tester：Neo4j 每次要求密码且错误不泄露密码', async () => {
  const { testProjectDataSourceConnection } = await import('../src/evaluation/data-source-connection-test.js');
  await assert.rejects(testProjectDataSourceConnection(neo4jInput({ password: '' })), /Password.*每次/);
  await assert.rejects(testProjectDataSourceConnection(neo4jInput({ password: 'secret-value' }), {
    neo4jDriverFactory: () => { throw new Error('认证失败 secret-value'); },
  }), (error: unknown) => {
    assert.match(String(error), /\[REDACTED\]/);
    assert.doesNotMatch(String(error), /secret-value/);
    return true;
  });
});

test('direct connection tester：NL2SQL 直接调用目标服务而非 consumer', async () => {
  const { testProjectDataSourceConnection } = await import('../src/evaluation/data-source-connection-test.js');
  const calls: Array<{ url: string; init: RequestInit }> = [];
  await testProjectDataSourceConnection(nl2sqlInput({ scene: 'scene-a' }), {
    request: async (url, init) => {
      calls.push({ url: String(url), init: init ?? {} });
      return jsonResponse({ ok: true });
    },
  });
  assert.equal(calls[0].url, 'http://nl2sql:8080/api/v1/sql');
  assert.equal((calls[0].init.headers as Record<string, string>).Authorization, 'Bearer token-value');
  assert.deepEqual(JSON.parse(String(calls[0].init.body)), { sql: 'SELECT 1 AS ok', scene: 'scene-a' });
});

test('direct connection tester：NL2SQL Template 允许不配置 Token', async () => {
  const { testProjectDataSourceConnection } = await import('../src/evaluation/data-source-connection-test.js');
  const calls: Array<{ url: string; init: RequestInit }> = [];
  await testProjectDataSourceConnection(nl2sqlInput({ token: '', scene: 'scene-a' }), {
    request: async (url, init) => {
      calls.push({ url: String(url), init: init ?? {} });
      return jsonResponse({ ok: true });
    },
  });
  assert.equal((calls[0].init.headers as Record<string, string>).Authorization, undefined);
});

test('consumer sync：导入当前知识包并写入本体级数据源', async () => {
  const { createProject } = await import('../src/projects/repo.js');
  const { insertPackageVersion } = await import('../src/workflow/m2-repo.js');
  const { syncProjectDataSourceToConsumer } = await import('../src/evaluation/data-source-consumer-sync.js');
  const project = createProject({ owner: 'alice', name: '本体数据源同步' });
  const projectId = project.id;
  const ontologyId = 'ontology-a';
  const workspaceId = 'test';
  insertPackageVersion(projectId, '1.0.0', {
    id: ontologyId, name: '本体数据源同步', description: '本体数据源同步', version: '1.0.0',
    language: 'zh-CN', defaultProviderAlias: 'graph', objects: [], operations: [],
  }, { covered: [], uncovered: [] });
  const calls: Array<{ path: string; method: string; workspaceId?: string; body?: any }> = [];
  const request = async (_token: string, path: string, options: any = {}) => {
    const method = options.method ?? 'GET';
    const body = typeof options.body === 'string' ? JSON.parse(options.body) : undefined;
    calls.push({ path, method, workspaceId: options.workspaceId, body });
    if (path === '/agentic/workspaces') return jsonResponse({ workspaces: [{ id: workspaceId }] });
    if (path === '/agentic/ontologies/import-zip' && method === 'POST') {
      return jsonResponse({ ontology_id: ontologyId }, 201);
    }
    if (path === `/agentic/ontologies/${ontologyId}/data-source` && method === 'PUT') {
      return jsonResponse({ configured: true, source: body });
    }
    throw new Error(`unexpected request: ${method} ${path}`);
  };
  const reference = await syncProjectDataSourceToConsumer({
    projectId, workspaceId, ontologyId, token: 'access-token',
    source: neo4jInput({ workspace_value: workspaceId, ontology_value: ontologyId }),
    request,
  });
  assert.deepEqual(reference, { workspaceId, ontologyId });
  assert.deepEqual(calls.map(({ method, path, workspaceId: id }) => ({ method, path, workspaceId: id })), [
    { method: 'GET', path: '/agentic/workspaces', workspaceId: undefined },
    { method: 'POST', path: '/agentic/ontologies/import-zip', workspaceId },
    { method: 'PUT', path: `/agentic/ontologies/${ontologyId}/data-source`, workspaceId },
  ]);
  assert.deepEqual(calls[2].body, {
    mode: 'direct_provider',
    provider: 'neo4j',
    config: {
      uri: 'bolt://neo4j:7687', username: 'neo4j', password: 'secret', database: 'neo4j',
      workspace_property: 'd2o_workspace_id', ontology_property: 'd2o_ontology_id',
    },
  });
});

test('consumer sync：导包冲突覆盖版本后更新本体级数据源', async () => {
  const { createProject } = await import('../src/projects/repo.js');
  const { insertPackageVersion } = await import('../src/workflow/m2-repo.js');
  const { syncProjectDataSourceToConsumer } = await import('../src/evaluation/data-source-consumer-sync.js');
  const { shadowWorkspaceId } = await import('../src/evaluation/shadow-workspace.js');
  const project = createProject({ owner: 'alice', name: '本体数据源覆盖' });
  const projectId = project.id;
  const workspaceId = shadowWorkspaceId(projectId);
  const ontologyId = 'ontology-b';
  insertPackageVersion(projectId, '1.0.0', {
    id: ontologyId, name: '本体数据源覆盖', description: '本体数据源覆盖', version: '1.0.0',
    language: 'zh-CN', defaultProviderAlias: 'graph', objects: [], operations: [],
  }, { covered: [], uncovered: [] });
  let importAttempts = 0;
  const calls: Array<{ path: string; method: string; workspaceId?: string; body?: any }> = [];
  const request = async (_token: string, path: string, options: any = {}) => {
    const method = options.method ?? 'GET';
    const body = typeof options.body === 'string' ? JSON.parse(options.body) : undefined;
    calls.push({ path, method, workspaceId: options.workspaceId, body });
    if (path === '/agentic/workspaces') return jsonResponse({ workspaces: [{ id: workspaceId }] });
    if (path === '/agentic/ontologies/import-zip' && method === 'POST') {
      importAttempts++;
      return importAttempts === 1
        ? jsonResponse({ requires_confirmation: true }, 409)
        : jsonResponse({ ontology_id: ontologyId }, 201);
    }
    if (path === `/agentic/ontologies/${ontologyId}/data-source` && method === 'PUT') {
      return jsonResponse({ configured: true, source: body });
    }
    throw new Error(`unexpected request: ${method} ${path}`);
  };
  const reference = await syncProjectDataSourceToConsumer({
    projectId, workspaceId, ontologyId, token: 'access-token',
    source: {
      mode: 'connection', provider: 'nl2sql_http',
      config: {
        url: 'http://nl2sql:8080', token: 'new-token', scene: 'scene-a',
        evaluation_workspace_id: 'nl2sql-eval',
      },
    },
    request,
  });
  assert.deepEqual(reference, { workspaceId, ontologyId });
  assert.equal(importAttempts, 2);
  assert.deepEqual(calls.at(-1), {
    path: `/agentic/ontologies/${ontologyId}/data-source`,
    method: 'PUT',
    workspaceId,
    body: {
      mode: 'direct_provider', provider: 'nl2sql_http',
      config: { url: 'http://nl2sql:8080', token: 'new-token', scene: 'scene-a' },
    },
  });
});

test('consumer sync：网络异常保留失败动作上下文', async () => {
  const { createProject } = await import('../src/projects/repo.js');
  const { insertPackageVersion } = await import('../src/workflow/m2-repo.js');
  const { syncProjectDataSourceToConsumer } = await import('../src/evaluation/data-source-consumer-sync.js');
  const { shadowWorkspaceId } = await import('../src/evaluation/shadow-workspace.js');
  const project = createProject({ owner: 'alice', name: '同步异常上下文' });
  const projectId = project.id;
  const workspaceId = shadowWorkspaceId(projectId);
  const ontologyId = 'ontology-error';
  insertPackageVersion(projectId, '1.0.0', {
    id: ontologyId, name: '同步异常上下文', description: '同步异常上下文', version: '1.0.0',
    language: 'zh-CN', defaultProviderAlias: 'graph', objects: [], operations: [],
  }, { covered: [], uncovered: [] });
  const cases = [{
      error: /导入知识包失败：fetch failed/,
      request: async (_token: string, path: string) => {
        if (path === '/agentic/workspaces') return jsonResponse({ workspaces: [{ id: workspaceId }] });
        throw new Error('fetch failed');
      },
    },
    {
      error: /同步评测工作空间失败：fetch failed/,
      request: async () => { throw new Error('fetch failed'); },
    },
  ];
  for (const item of cases) {
    await assert.rejects(syncProjectDataSourceToConsumer({
      projectId,
      workspaceId,
      ontologyId,
      token: 'access-token',
      source: neo4jInput({ workspace_value: workspaceId, ontology_value: ontologyId }),
      request: item.request,
    }), item.error);
  }
});

test('evaluation：本体数据源引用缺失或工作空间不一致时不创建轮次', async () => {
  const { startEvaluation } = await import('../src/evaluation/engine.js');
  const { saveProjectDataSource } = await import('../src/evaluation/data-source-repo.js');
  const missing = await createReadyEvaluationProject({ name: '旧评测连接测试', includeOntologyId: false });
  const mismatched = await createReadyEvaluationProject({ name: '旧评测工作区测试' });
  saveProjectDataSource({
    projectId: mismatched.project.id,
    mode: 'connection',
    provider: 'neo4j',
    workspaceId: mismatched.project.id,
    ontologyId: `${mismatched.project.id}-ontology`,
    config: neo4jConfig({
      password: undefined, workspace_value: 'test', has_password: true,
    }),
  });
  for (const ready of [missing, mismatched]) {
    assert.throws(() => startEvaluation({ projectId: ready.project.id, token: 'token' }),
      /缺少可用的本体数据源.*重新输入密钥并保存/);
    assert.equal(ready.m3repo.listRounds(ready.project.id).length, 0);
  }
});

test('evaluation：评测库后台导入运行中时禁止启动评测', async () => {
  const { startEvaluation } = await import('../src/evaluation/engine.js');
  const { saveProjectDataSource } = await import('../src/evaluation/data-source-repo.js');
  const {
    createEvaluationDatabaseImportRun,
    finishEvaluationDatabaseImportRun,
  } = await import('../src/evaluation/eval-db-import-repo.js');
  const ready = await createReadyEvaluationProject({ name: '评测库导入门禁测试' });
  const ontologyId = `${ready.project.id}-ontology`;
  saveProjectDataSource({
    projectId: ready.project.id,
    mode: 'connection',
    provider: 'nl2sql_http',
    workspaceId: 'nl2sql-eval',
    ontologyId,
    config: {
      url: 'http://nl2sql:8080', scene: 'evaluation', evaluation_workspace_id: 'nl2sql-eval',
    },
  });
  const run = createEvaluationDatabaseImportRun({
    projectId: ready.project.id,
    filename: 'evaluation.xlsx',
    database: 'evaluation',
  });

  assert.throws(
    () => startEvaluation({ projectId: ready.project.id, token: 'token' }),
    /评测库数据仍在后台导入/,
  );
  assert.equal(ready.m3repo.listRounds(ready.project.id).length, 0);
  finishEvaluationDatabaseImportRun(run.id, { error: '测试清理' });
});

test('evaluation：存量脚本只有 SDK 执行契约问题时仍创建正式评测轮次', async () => {
  const { startEvaluation } = await import('../src/evaluation/engine.js');
  const { insertPackageVersion } = await import('../src/workflow/m2-repo.js');
  const { setSetting } = await import('../src/settings/repo.js');
  const ready = await createReadyEvaluationProject({ name: '存量脚本评测' });
  insertPackageVersion(ready.project.id, '2.0.0', {
    id: `${ready.project.id}-ontology`,
    name: '存量脚本评测',
    description: '只有执行契约问题的存量知识包',
    version: '2.0.0',
    language: 'zh-CN',
    defaultProviderAlias: 'graph',
    objects: [], rules: [], metrics: [],
    operations: [{
      key: 'analyze_plan', title: '分析计划', description: '分析计划',
      inputs: [], outputs: [], flow: ['执行确定性脚本'], relatedObjects: [],
      script: { key: 'calculate_plan', required: true },
    }],
    scripts: [{
      key: 'calculate_plan', title: '计算计划', description: '计算计划',
      boundOperation: 'analyze_plan', runtime: 'python', entrypoint: 'calculate_plan.py',
      timeoutSeconds: 30,
      parameters: [{
        name: 'input_json', flag: '--input-json', type: 'string', required: true,
        description: '结构化计划 JSON',
      }],
      outputs: [{ key: 'result', type: 'object', description: '计算结果' }],
      boundaries: ['不得内置评测答案。'],
      sourceCode: `
import argparse
import json
from shared_capabilities.ontology_script_sdk import DataProviderClient

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-json", required=True)
    args = parser.parse_args()
    with DataProviderClient() as client:
        rows = client.query("orders", filters={"status": "open"})
    print(json.dumps({"rows": rows, "input": json.loads(args.input_json)}, ensure_ascii=False))

if __name__ == "__main__":
    main()
`,
    }],
  }, { covered: [], uncovered: [] });
  setSetting('consumer_api_url', 'http://127.0.0.1:1');

  const round = startEvaluation({ projectId: ready.project.id, token: 'token' });
  assert.ok(round.roundId);
  assert.equal(ready.m3repo.listRounds(ready.project.id).length, 1);
  for (let index = 0; index < 50 && ready.m3repo.getRound(round.roundId)?.status === 'running'; index++) {
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
});

test('evaluation：连接工作空间必须与数据源 Workspace 隔离值一致', async () => {
  const { evaluationWorkspaceForRun } = await import('../src/evaluation/engine.js');
  const dataSource = {
    projectId: 'project-a', mode: 'connection' as const, provider: 'neo4j' as const,
    workspaceId: 'test', ontologyId: 'ontology-a',
    config: { workspace_property: 'd2o_workspace_id', workspace_value: 'test', has_password: true },
    createdAt: '2026-01-01T00:00:00.000Z', updatedAt: '2026-01-01T00:00:00.000Z',
  };
  assert.equal(evaluationWorkspaceForRun('project-a', dataSource, 'ontology-a'), 'test');
  assert.throws(() => evaluationWorkspaceForRun('project-a', {
    ...dataSource, workspaceId: 'other',
  }, 'ontology-a'), /缺少可用的本体数据源.*重新输入密钥并保存/);
  assert.throws(() => evaluationWorkspaceForRun('project-a', dataSource, 'ontology-b'),
    /本体数据源与当前知识包不匹配.*重新保存/);
});

test('evaluation：共享工作空间不能并发运行且失败后释放', async () => {
  const { startEvaluation } = await import('../src/evaluation/engine.js');
  const { setSetting } = await import('../src/settings/repo.js');
  setSetting('consumer_api_url', 'http://127.0.0.1:1');
  const firstProject = await createReadyEvaluationProject({ name: '共享工作空间项目一' });
  const secondProject = await createReadyEvaluationProject({ name: '共享工作空间项目二' });
  const first = startEvaluation({ projectId: firstProject.project.id, token: 'token' });
  assert.throws(() => startEvaluation({ projectId: secondProject.project.id, token: 'token' }),
    /评测工作空间 test 已有评测在运行/);
  for (let index = 0; index < 50 && firstProject.m3repo.getRound(first.roundId)?.status === 'running'; index++) {
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  assert.equal(firstProject.m3repo.getRound(first.roundId)?.status, 'failed');
  const second = startEvaluation({ projectId: secondProject.project.id, token: 'token' });
  assert.ok(second.roundId);
  for (let index = 0; index < 50 && secondProject.m3repo.getRound(second.roundId)?.status === 'running'; index++) {
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
});

test('evaluation data source：评测复用已保存的本体级数据源', async () => {
  const { prepareProjectDataSourceForEvaluation } = await import('../src/evaluation/data-source-evaluation-service.js');
  const calls: Array<{ path: string; method: string; body?: any }> = [];
  await prepareProjectDataSourceForEvaluation({
    token: 'token',
    ontologyId: 'ontology-a',
    dataSource: {
      projectId: 'project-a', mode: 'connection', provider: 'neo4j',
      workspaceId: 'mw-shadow-project-a', ontologyId: 'ontology-a',
      config: { has_password: true },
      createdAt: '2026-01-01T00:00:00.000Z', updatedAt: '2026-01-01T00:00:00.000Z',
    },
    request: async (_token, path, options: any = {}) => {
      const method = options.method ?? 'GET';
      const body = options.body ? JSON.parse(options.body) : undefined;
      calls.push({ path, method, body });
      return jsonResponse({
        ontology_id: 'ontology-a', configured: true,
        source: { mode: 'direct_provider', provider: 'neo4j', config: { has_password: true } },
      });
    },
  });
  assert.deepEqual(calls, [{
    path: '/agentic/ontologies/ontology-a/data-source', method: 'GET', body: undefined,
  }]);
});

test('evaluation data source：不使用数据源时清除导入本体的配置', async () => {
  const { prepareProjectDataSourceForEvaluation } = await import('../src/evaluation/data-source-evaluation-service.js');
  const calls: Array<{ path: string; method: string }> = [];
  await prepareProjectDataSourceForEvaluation({
    token: 'token',
    ontologyId: 'ontology-a',
    dataSource: {
      projectId: 'project-a', mode: 'none', workspaceId: 'mw-shadow-project-a', config: {},
      createdAt: '2026-01-01T00:00:00.000Z', updatedAt: '2026-01-01T00:00:00.000Z',
    },
    request: async (_token, path, options: any = {}) => {
      calls.push({ path, method: options.method ?? 'GET' });
      return jsonResponse({ success: true });
    },
  });
  assert.deepEqual(calls, [{
    path: '/agentic/ontologies/ontology-a/data-source', method: 'DELETE',
  }]);
});

test('data source form：脱敏回填并校验 Scene 和 Workspace', async () => {
  const {
    dataSourceToForm,
    dataSourceFormPayload,
    dataSourceFormError,
    applyCreatedProjectDatabaseScene,
    evaluationDatabaseUrlSelectionError,
  } =
    await import('../frontend/src/data-source-form.js');
  const form = dataSourceToForm({
    mode: 'connection',
    provider: 'neo4j',
    config: neo4jConfig({
      password: undefined, database: 'domain', workspace_value: 'workspace-001', has_password: true,
    }),
  });
  assert.equal(form.password, '');
  assert.deepEqual(dataSourceFormPayload(form), neo4jInput({
    password: '', database: 'domain', workspace_value: 'workspace-001',
  }));
  assert.match(dataSourceFormError(form), /Password.*每次/);
  const valid = { ...form, password: 'secret', workspace_value: 'test' };
  assert.equal(dataSourceFormError(valid), '');
  assert.match(dataSourceFormError({ ...valid, workspace_value: '' }), /Workspace 对应值不能为空/);
  assert.match(dataSourceFormError({ ...valid, workspace_value: 'Test Space' }), /工作空间 ID 格式非法/);
  assert.match(dataSourceFormError({ ...valid, workspace_value: `a${'b'.repeat(64)}` }), /最长 64/);

  const nl2sql = dataSourceToForm({
    mode: 'connection',
    provider: 'nl2sql_http',
    workspaceId: 'nl2sql-eval',
    config: {
      url: 'http://nl2sql:8080', scene: 'evaluation', evaluation_workspace_id: 'nl2sql-eval',
    },
  });
  assert.equal(dataSourceFormError(nl2sql), '');
  assert.match(dataSourceFormError({ ...nl2sql, scene: '' }), /Scene 不能为空/);
  assert.match(dataSourceFormError({ ...nl2sql, scene: 'invalid-scene' }), /Scene 格式非法/);
  assert.deepEqual(applyCreatedProjectDatabaseScene(nl2sql, 'mv_shadow_pj_1'), {
    ...nl2sql,
    scene: 'mv_shadow_pj_1',
  });
  assert.equal(evaluationDatabaseUrlSelectionError('default', '', true), '');
  assert.match(evaluationDatabaseUrlSelectionError('default', '', false), /未配置 EVAL_DB_URL/);
  assert.equal(evaluationDatabaseUrlSelectionError(
    'custom', 'mysql://user:secret@mysql.local:3306', true,
  ), '');
  assert.match(evaluationDatabaseUrlSelectionError('custom', '', true), /不能为空/);
  assert.match(evaluationDatabaseUrlSelectionError('custom', 'https://mysql.local', true), /mysql 协议/);
  assert.deepEqual(dataSourceFormPayload(nl2sql), {
    mode: 'connection',
    provider: 'nl2sql_http',
    config: {
      url: 'http://nl2sql:8080', token: '', scene: 'evaluation',
      evaluation_workspace_id: 'nl2sql-eval',
    },
  });
  assert.match(dataSourceFormError({ ...nl2sql, evaluation_workspace_id: '' }), /工作空间 ID 不能为空/);
});

test('workflow：数据源配置位于知识包完善与评测用例录入之间', async () => {
  const { FIXED_TRUNK } = await import('../src/types.js');
  assert.deepEqual(
    FIXED_TRUNK.slice(FIXED_TRUNK.indexOf('review'), FIXED_TRUNK.indexOf('evaluate') + 1),
    ['review', 'data_source', 'testcases', 'evaluate'],
  );
});
