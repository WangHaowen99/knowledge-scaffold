import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'evaluation-auth-fail-fast-'));

test('认证失败会立即终止整轮，不为剩余用例重复记录凭据错误', async (t) => {
  const requests: string[] = [];
  const server = createServer((request, response) => {
    const path = String(request.url ?? '');
    requests.push(`${request.method} ${path}`);
    request.resume();
    response.setHeader('Content-Type', 'application/json');
    if (path === '/agentic/runs/sync') {
      response.statusCode = 401;
      response.end('{"error":"Unauthorized"}');
      return;
    }
    if (path === '/agentic/workspaces') {
      response.end('{"workspaces":[]}');
      return;
    }
    if (path === '/agentic/ontologies/import-zip') {
      response.end('{"ontology_id":"evaluation-auth-package","objects_count":0,"operations_count":0}');
      return;
    }
    if (path.endsWith('/active-version')) {
      response.end('{"active_version":"1.0.0"}');
      return;
    }
    response.end('{}');
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise<void>((resolve) => server.close(() => resolve())));
  const address = server.address();
  assert.ok(address && typeof address === 'object');

  const [engine, dataSources, m3repo, projects, settings] = await Promise.all([
    import('../src/evaluation/engine.js'),
    import('../src/evaluation/data-source-repo.js'),
    import('../src/workflow/m3-repo.js'),
    import('../src/projects/repo.js'),
    import('../src/settings/repo.js'),
  ]);
  settings.setSetting('consumer_api_url', `http://127.0.0.1:${address.port}`);

  const project = projects.createProject({ owner: 'modeler', name: '评测认证中止测试' });
  for (let index = 1; index <= 17; index += 1) {
    m3repo.addTestcase(project.id, `真实场景问题 ${index}`, `参考答案 ${index}`, 'manual');
  }
  dataSources.saveProjectDataSource({
    projectId: project.id,
    mode: 'none',
    workspaceId: `mw-shadow-${project.id.replace(/[^a-zA-Z0-9-]/g, '-')}`.slice(0, 60),
  });

  const { roundId } = engine.startEvaluation({
    projectId: project.id,
    token: 'access-without-refresh',
    packageSnapshot: {
      version: '1.0.0',
      revisionOid: '1'.repeat(40),
      files: {},
      spec: {
        id: 'evaluation-auth-package',
        name: '评测认证测试包',
        description: '验证认证失败时终止整轮',
        version: '1.0.0',
        status: 'review',
        language: 'zh-CN',
        defaultProviderAlias: 'data',
        objects: [],
        operations: [],
      },
    },
  });
  const round = await engine.waitForEvaluation(project.id, roundId);

  assert.equal(round.status, 'failed');
  assert.equal(round.progressDone, 0);
  assert.equal(m3repo.listResults(roundId).length, 0);
  assert.equal(requests.filter((item) => item === 'POST /agentic/runs/sync').length, 1);
  assert.equal((round.log.match(/评测访问凭据已过期/gu) ?? []).length, 1);
});
