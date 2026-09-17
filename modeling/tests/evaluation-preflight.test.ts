import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'evaluation-preflight-'));
process.env.MODELING_EVALUATION_CASE_CONCURRENCY = '2';

const engine = await import('../src/evaluation/engine.js');
const { saveProjectDataSource } = await import('../src/evaluation/data-source-repo.js');
const m3repo = await import('../src/workflow/m3-repo.js');
const { createProject } = await import('../src/projects/repo.js');
const { setSetting } = await import('../src/settings/repo.js');

function packageSnapshot() {
  return {
    version: '1.0.0',
    revisionOid: '1'.repeat(40),
    files: {},
    spec: {
      id: 'preflight-package', name: '预检测试包', description: '环境预检',
      version: '1.0.0', status: 'review' as const, language: 'zh-CN',
      defaultProviderAlias: 'data', objects: [], operations: [],
    },
  };
}

test('isEnvironmentFailureMessage 识别凭据/令牌/连接类失败', () => {
  assert.ok(engine.isEnvironmentFailureMessage('运行失败：评测访问凭据已过期，请重新登录后再启动评测'));
  assert.ok(engine.isEnvironmentFailureMessage('缺少有效的 MODELING_SERVICE_TOKEN（至少 32 个字符）'));
  assert.ok(engine.isEnvironmentFailureMessage('Consumer 请求失败：fetch failed'));
  assert.ok(engine.isEnvironmentFailureMessage('connect ECONNREFUSED 127.0.0.1:5600'));
  // 知识包质量问题不算环境失败，不应触发环境熔断。
  assert.ok(!engine.isEnvironmentFailureMessage('The deterministic operation failed'));
  assert.ok(!engine.isEnvironmentFailureMessage('回答达到输出上限，内容尚未完成'));
});

test('注入的同步 preflight 抛错时阻止开轮，且不创建轮次', () => {
  const project = createProject({ owner: 'alice', name: '预检-注入失败' });
  m3repo.addTestcase(project.id, '真实评测问题', '真实参考答案', 'manual');
  saveProjectDataSource({
    projectId: project.id, mode: 'none',
    workspaceId: `mw-shadow-${project.id}`.slice(0, 60),
  });
  assert.throws(
    () => engine.startEvaluation(
      {
        projectId: project.id,
        owner: 'alice',
        token: 'access-token',
        packageSnapshot: packageSnapshot(),
      },
      {
        preflight: () => {
          throw new engine.EvaluationOperationError(
            'EVALUATION_ENVIRONMENT_UNAVAILABLE', '注入的环境不可用', 409,
          );
        },
      },
    ),
    /注入的环境不可用/,
  );
  // 关键：环境门在轮次创建之前拒绝，不留下 failed 轮次污染统计。
  assert.equal(m3repo.listRounds(project.id).length, 0);
});

test('连续环境失败达到阈值时提前熔断整轮', async (t) => {
  const server = createServer((request, response) => {
    request.resume();
    response.setHeader('Content-Type', 'application/json');
    const path = String(request.url ?? '');
    if (path === '/agentic/runs/sync') {
      // 每条用例都以连接类文案失败，模拟评测运行环境阻断。
      response.statusCode = 502;
      response.end('{"error":"connect ECONNREFUSED 10.0.0.1:5600"}');
      return;
    }
    if (path === '/agentic/workspaces') { response.end('{"workspaces":[]}'); return; }
    if (path === '/agentic/ontologies/import-zip') {
      response.end('{"ontology_id":"preflight-package","objects_count":0,"operations_count":0}');
      return;
    }
    if (path.endsWith('/active-version')) { response.end('{"active_version":"1.0.0"}'); return; }
    response.end('{}');
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise<void>((resolve) => server.close(() => resolve())));
  const address = server.address();
  assert.ok(address && typeof address === 'object');
  setSetting('consumer_api_url', `http://127.0.0.1:${address.port}`);

  const project = createProject({ owner: 'alice', name: '预检-环境熔断' });
  for (let index = 1; index <= 20; index += 1) {
    m3repo.addTestcase(project.id, `真实问题 ${index}`, `参考答案 ${index}`, 'manual');
  }
  saveProjectDataSource({
    projectId: project.id, mode: 'none',
    workspaceId: `mw-shadow-${project.id}`.slice(0, 60),
  });

  const { roundId } = engine.startEvaluation({
    projectId: project.id,
    owner: 'alice',
    token: 'access-token',
    packageSnapshot: packageSnapshot(),
  });
  const round = await engine.waitForEvaluation(project.id, roundId);
  assert.equal(round.status, 'failed');
  // 20 条用例不会全部烧完：熔断在连续环境失败达到阈值后终止整轮。
  assert.ok(round.progressDone < 20, `应提前熔断，实际完成 ${round.progressDone} 条`);
  assert.ok(/评测环境持续异常/u.test(round.log), '轮次日志应记录环境熔断终因');
});
