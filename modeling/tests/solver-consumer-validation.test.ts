import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

import multipart from '@fastify/multipart';
import Fastify from 'fastify';

import type { PackageSpec } from '../src/types.js';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'modeling-solver-validation-'));

const [
  validation,
  { registerSolverContractValidationRoutes },
  { createProject },
  { saveProjectDataSource },
  { shadowWorkspaceId },
  { zipPackage },
  { renderPackage },
] = await Promise.all([
  import('../src/evaluation/solver-contract-validation.js'),
  import('../src/evaluation/solver-contract-validation-routes.js'),
  import('../src/projects/repo.js'),
  import('../src/evaluation/data-source-repo.js'),
  import('../src/evaluation/shadow-workspace.js'),
  import('../src/workflow/export.js'),
  import('../src/workflow/package-render.js'),
]);
const {
  parseSolverValidationCases,
  relayAuthSession,
  runSolverContractValidation,
  runSolverValidationCase,
  SolverContractValidationError,
} = validation;

// 全部为合成数据：知识包、批次与工作空间均为测试虚构。
const ONTOLOGY_ID = 'synthetic-route-package';
const VERSION = '0.1.3';

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });

function batches(count: number) {
  return { items: Array.from({ length: count }, (_, index) => ({ batch: `B${index + 1}` })) };
}

function authoritative(jobId: string, extra: Record<string, unknown> = {}) {
  return {
    status: 'completed',
    authoritative: true,
    run_id: `run-${jobId}`,
    job_id: jobId,
    feasibility: 'feasible',
    optimality: 'proven_optimal',
    proof_kind: 'exact_search',
    summary: { selected: 1 },
    result_artifact_ref: `artifact://result/${jobId}`,
    verification_evidence_ref: `artifact://evidence/${jobId}`,
    knowledge_revision_id: VERSION,
    fact_snapshot_digest: 'sha256:facts',
    problem_digest: 'sha256:problem',
    candidate_results_artifact_ref: `artifact://candidates/${jobId}`,
    candidate_results_artifact_digest: 'sha256:candidates',
    ...extra,
  };
}

interface Call { path: string; method: string; headers: Record<string, string>; body?: any }

/** 合成 Consumer：按 operation_key 决定求解结果，按批次数给出 2N 个候选。 */
function fakeConsumer(options: {
  solve?: (body: any, attempt: number) => { status?: number; body: unknown };
  candidates?: (jobId: string) => { status?: number; body: unknown };
} = {}) {
  const calls: Call[] = [];
  const attempts = new Map<string, number>();
  const counts = new Map<string, number>();
  const request = async (_token: string, path: string, init: RequestInit & { workspaceId?: string } = {}) => {
    const method = String(init.method ?? 'GET');
    const headers = { ...(init.headers as Record<string, string> ?? {}) };
    const body = typeof init.body === 'string' ? JSON.parse(init.body) : undefined;
    calls.push({ path, method, headers, body });
    if (path === '/agentic/workspaces' && method === 'GET') return json({ workspaces: [{ id: 'synthetic-ws' }] });
    if (path === '/agentic/ontologies/import-zip') return json({ ontology_id: ONTOLOGY_ID, objects_count: 0 });
    if (path.endsWith('/active-version')) return json({ active_version: JSON.parse(String(init.body)).version });
    if (path.endsWith('/data-source')) return json({ ok: true });
    if (path === '/api/solver/runs') {
      const key = headers['Idempotency-Key'];
      const attempt = (attempts.get(key) ?? 0) + 1;
      attempts.set(key, attempt);
      const outcome = options.solve?.(body, attempt)
        ?? { body: authoritative(`job-${key}`) };
      const jobId = (outcome.body as any)?.job_id;
      if (jobId) counts.set(jobId, (body.arguments.items?.length ?? 0) * 2);
      return json(outcome.body, outcome.status ?? 200);
    }
    const match = /^\/api\/solver\/history\/([^/]+)\/candidate-results$/.exec(path);
    if (match) {
      const jobId = decodeURIComponent(match[1]);
      const outcome = options.candidates?.(jobId) ?? { body: { candidate_count: counts.get(jobId) } };
      return json(outcome.body, outcome.status ?? 200);
    }
    return json({ error: 'not found' }, 404);
  };
  return { request, calls };
}

const dataSource = {
  projectId: 'pj_synthetic',
  mode: 'none' as const,
  workspaceId: 'synthetic-ws',
  config: {},
  createdAt: '2026-01-01T00:00:00Z',
  updatedAt: '2026-01-01T00:00:00Z',
};

function validate(request: any, cases: any[], extra: Record<string, unknown> = {}) {
  return runSolverContractValidation({
    projectId: 'pj_synthetic',
    owner: 'alice',
    auth: relayAuthSession('synthetic-access'),
    dataSource,
    ontologyId: ONTOLOGY_ID,
    version: VERSION,
    zip: new Uint8Array([1, 2, 3]),
    cases: parseSolverValidationCases(cases),
    idempotencyPrefix: 'run1',
    request,
    pollIntervalMs: 1,
    ...extra,
  });
}

test('验证用例只接受合法的 operation_key、唯一 id 和对象参数', () => {
  const [item] = parseSolverValidationCases('[{"id":"c1","operation_key":"plan_route","arguments":{"items":[]}}]');
  assert.deepEqual(item, { id: 'c1', operationKey: 'plan_route', arguments: { items: [] } });
  const rejects = (raw: unknown, pattern: RegExp) => assert.throws(
    () => parseSolverValidationCases(raw),
    (error: any) => error.code === 'SOLVER_VALIDATION_CASES_INVALID' && pattern.test(error.message),
  );
  rejects('not json', /JSON 数组/);
  rejects([], /至少/);
  rejects(Array.from({ length: 21 }, (_, i) => ({ id: `c${i}`, operation_key: 'a', arguments: {} })), /最多 20/);
  rejects([{ id: 'c1', operation_key: 'Plan-Route', arguments: {} }], /operation_key/);
  rejects([{ id: 'c1', operation_key: 'a', arguments: {} }, { id: 'c1', operation_key: 'a', arguments: {} }], /重复/);
  rejects([{ id: 'c1', operation_key: 'a', arguments: [] }], /arguments/);
  rejects([{ id: 'c 1', operation_key: 'a', arguments: {} }], /id 不合法/);
});

test('Consumer 不可用时整体失败，且不回传连接地址', async () => {
  const request = async () => {
    throw new Error('Consumer 请求失败：fetch failed（ECONNREFUSED：connect 192.0.2.10:8000）');
  };
  await assert.rejects(
    validate(request, [{ id: 'c1', operation_key: 'plan_route', arguments: batches(1) }]),
    (error: any) => {
      assert.ok(error instanceof SolverContractValidationError);
      assert.equal(error.code, 'CONSUMER_UNAVAILABLE');
      assert.equal(error.statusCode, 502);
      assert.equal(error.stage, 'workspace');
      assert.doesNotMatch(error.message, /192\.0\.2\.10/);
      return true;
    },
  );
});

test('导入阶段被拒映射为包被拒，报错中的地址被隐藏', async () => {
  const consumer = fakeConsumer();
  const request = async (token: string, path: string, init?: any) => (
    path === '/agentic/ontologies/import-zip'
      ? json({ error: '校验失败，详见 https://consumer.example.com/docs' }, 422)
      : consumer.request(token, path, init)
  );
  await assert.rejects(
    validate(request, [{ id: 'c1', operation_key: 'plan_route', arguments: batches(1) }]),
    (error: any) => {
      assert.equal(error.code, 'SOLVER_PACKAGE_REJECTED');
      assert.equal(error.stage, 'package');
      assert.doesNotMatch(error.message, /example\.com/);
      return true;
    },
  );
});

test('导入、激活、数据源、求解按顺序执行，并以运行 ID 派生幂等键', async () => {
  const consumer = fakeConsumer();
  const report = await validate(consumer.request, [
    { id: 'n7', operation_key: 'plan_route', arguments: batches(7) },
    { id: 'n9', operation_key: 'plan_route', arguments: batches(9) },
  ]);
  const order = consumer.calls.map((call) => `${call.method} ${call.path}`);
  assert.deepEqual(order.slice(0, 4), [
    'GET /agentic/workspaces',
    'POST /agentic/ontologies/import-zip',
    `PUT /agentic/ontologies/${ONTOLOGY_ID}/active-version`,
    `DELETE /agentic/ontologies/${ONTOLOGY_ID}/data-source`,
  ]);
  const runs = consumer.calls.filter((call) => call.path === '/api/solver/runs');
  assert.deepEqual(runs.map((call) => call.headers['Idempotency-Key']), ['run1:n7', 'run1:n9']);
  assert.deepEqual(Object.keys(runs[0].body).sort(), [
    'arguments', 'knowledge_package_id', 'knowledge_package_revision', 'operation_key',
  ]);
  assert.equal(runs[0].body.knowledge_package_revision, VERSION);
  assert.deepEqual(report.package, { id: ONTOLOGY_ID, version: VERSION });
  assert.deepEqual(report.cases.map((item) => [item.id, item.status, item.authoritative, item.candidate_count]), [
    ['n7', 'completed', true, 14],
    ['n9', 'completed', true, 18],
  ]);
});

test('running 时用同一幂等键续接，直到 Consumer 给出终态', async () => {
  const consumer = fakeConsumer({
    solve: (_body, attempt) => (attempt < 3
      ? { body: { status: 'running', job_id: 'job-poll', phase: 'solve' } }
      : { body: authoritative('job-poll') }),
  });
  const [result] = (await validate(consumer.request, [
    { id: 'c1', operation_key: 'plan_route', arguments: batches(1) },
  ])).cases;
  const keys = consumer.calls.filter((call) => call.path === '/api/solver/runs')
    .map((call) => call.headers['Idempotency-Key']);
  assert.deepEqual(keys, ['run1:c1', 'run1:c1', 'run1:c1']);
  assert.equal(result.status, 'completed');
  assert.equal(result.candidate_count, 2);
});

test('超过截止时间仍在运行的用例记为超时，后续用例不再提交', async () => {
  let clock = 0;
  const consumer = fakeConsumer({
    solve: () => {
      clock += 10;
      return { body: { status: 'running', job_id: 'job-slow' } };
    },
  });
  const report = await validate(consumer.request, [
    { id: 'c1', operation_key: 'plan_route', arguments: batches(1) },
    { id: 'c2', operation_key: 'plan_route', arguments: batches(1) },
  ], { now: () => clock, deadlineMs: 25 });
  assert.deepEqual(report.cases.map((item) => [item.id, item.code]), [['c1', 'solver_timeout'], ['c2', 'solver_timeout']]);
  assert.equal(consumer.calls.filter((call) => call.body?.operation_key).length, 3);
});

test('契约不兼容与不可用以用例错误码回传，不中断其余用例', async () => {
  const consumer = fakeConsumer({
    solve: (body) => (body.operation_key === 'drifted'
      ? { status: 409, body: { code: 'solver_capability_drift' } }
      : body.operation_key === 'down'
        ? { status: 503, body: { code: 'solver_unavailable', retryable: true } }
        : { body: authoritative('job-ok') }),
  });
  const report = await validate(consumer.request, [
    { id: 'c1', operation_key: 'drifted', arguments: batches(1) },
    { id: 'c2', operation_key: 'down', arguments: batches(1) },
    { id: 'c3', operation_key: 'plan_route', arguments: batches(1) },
  ]);
  assert.deepEqual(report.cases.map((item) => [item.status, item.code ?? '', item.retryable ?? false]), [
    ['error', 'solver_capability_drift', false],
    ['error', 'solver_unavailable', true],
    ['completed', '', false],
  ]);
});

test('全不可行是有效的权威终态；非权威结果原样标记且不查询候选', async () => {
  const consumer = fakeConsumer({
    solve: (body) => (body.operation_key === 'infeasible_case'
      ? { body: authoritative('job-inf', {
        feasibility: 'infeasible', optimality: 'not_applicable', proof_kind: 'infeasibility_certificate',
        candidate_results_artifact_ref: undefined, candidate_results_artifact_digest: undefined,
      }) }
      : { body: { ...authoritative('job-weak'), authoritative: false } }),
  });
  const report = await validate(consumer.request, [
    { id: 'c1', operation_key: 'infeasible_case', arguments: batches(3) },
    { id: 'c2', operation_key: 'weak_case', arguments: batches(3) },
  ]);
  const [infeasible, weak] = report.cases;
  assert.equal(infeasible.authoritative, true);
  assert.equal(infeasible.feasibility, 'infeasible');
  assert.equal(infeasible.candidate_count, null);
  assert.equal(weak.status, 'completed');
  assert.equal(weak.authoritative, false);
  assert.equal(consumer.calls.some((call) => call.path.includes('candidate-results')), false);
});

test('缺少输入与候选读取失败都带诊断码，结果只保留公开字段', async () => {
  const consumer = fakeConsumer({
    solve: (body) => (body.operation_key === 'needs_input'
      ? { body: { status: 'awaiting_input', code: 'awaiting_input', phase: 'bind', missing: ['month'], authoritative: false } }
      : { body: { ...authoritative('job-x'), solver_endpoint: 'https://solver.example.net', summary: { blob: 'x'.repeat(5000) } } }),
    candidates: () => ({ status: 409, body: { code: 'candidate_digest_mismatch' } }),
  });
  const report = await validate(consumer.request, [
    { id: 'c1', operation_key: 'needs_input', arguments: {} },
    { id: 'c2', operation_key: 'plan_route', arguments: batches(2) },
  ]);
  const [awaiting, completed] = report.cases;
  assert.equal(awaiting.status, 'awaiting_input');
  assert.deepEqual(awaiting.missing, ['month']);
  assert.equal(completed.candidate_count, null);
  assert.equal(completed.candidate_results_error, 'candidate_digest_mismatch');
  assert.equal('solver_endpoint' in completed, false);
  assert.equal('summary' in completed, false, '超过 4 KB 的摘要不回传');
});

test('调用方令牌被拒后借用可续期会话，没有会话时提示重新登录', async () => {
  const fallback = {
    getAccessToken: async () => 'renewed-access',
    refreshAfterUnauthorized: async () => 'renewed-access',
  };
  const relay = relayAuthSession('caller-access', fallback as any);
  assert.equal(await relay.getAccessToken(), 'caller-access');
  assert.equal(await relay.refreshAfterUnauthorized(), 'renewed-access');
  assert.equal(await relay.getAccessToken(), 'renewed-access');
  await assert.rejects(relayAuthSession('caller-access').refreshAfterUnauthorized(), /重新登录/);
});

test('单条用例直接驱动时，HTTP 错误与未知状态都不会被当作通过', async () => {
  const item = { id: 'c1', operationKey: 'plan_route', arguments: batches(1) };
  const common = { workspaceId: 'synthetic-ws', ontologyId: ONTOLOGY_ID, version: VERSION, item, idempotencyKey: 'k', deadline: Date.now() + 1000 };
  const failed = await runSolverValidationCase({ ...common, request: async () => json({}, 500) });
  assert.deepEqual([failed.status, failed.code, failed.authoritative], ['error', 'http_500', false]);
  const unknown = await runSolverValidationCase({ ...common, request: async () => json({ status: 'cancelled', phase: 'solve' }) });
  assert.deepEqual([unknown.status, unknown.code, unknown.phase], ['error', 'solver_result_rejected', 'solve']);
});

test('Consumer 拒绝求解时回传它给出的阶段与原因，原因中的地址被隐藏', async () => {
  const item = { id: 'c1', operationKey: 'plan_route', arguments: batches(1) };
  const common = { workspaceId: 'synthetic-ws', ontologyId: ONTOLOGY_ID, version: VERSION, item, idempotencyKey: 'k', deadline: Date.now() + 1000 };
  const rejected = await runSolverValidationCase({
    ...common,
    request: async () => json({
      status: 'error',
      code: 'fact_snapshot_invalid',
      phase: 'snapshot',
      error: 'business data table synthetic_table is empty, see https://graph.example.net',
    }, 422),
  });
  assert.deepEqual([rejected.status, rejected.code, rejected.phase, rejected.retryable], [
    'error', 'fact_snapshot_invalid', 'snapshot', false,
  ]);
  assert.equal(rejected.detail, 'business data table synthetic_table is empty, see [地址已隐藏]');
  const bare = await runSolverValidationCase({ ...common, request: async () => json({}, 500) });
  assert.deepEqual([bare.phase, 'detail' in bare], ['request', false]);
});

function packageSpec(): PackageSpec {
  return {
    id: ONTOLOGY_ID,
    name: '合成求解知识包',
    description: '用于验证求解契约端到端接口的合成知识包',
    version: VERSION,
    language: 'zh-CN',
    defaultProviderAlias: 'data',
    objects: [],
    operations: [],
  };
}

function multipartBody(fields: Record<string, string>, zip?: Uint8Array) {
  const boundary = 'solver-validation-test-boundary';
  const parts: Buffer[] = Object.entries(fields).map(([name, value]) => Buffer.from(
    `--${boundary}\r\nContent-Disposition: form-data; name="${name}"\r\n\r\n${value}\r\n`,
  ));
  if (zip) {
    parts.push(Buffer.from(
      `--${boundary}\r\nContent-Disposition: form-data; name="file"; filename="package.zip"\r\n`
      + 'Content-Type: application/zip\r\n\r\n',
    ));
    parts.push(Buffer.from(zip));
    parts.push(Buffer.from('\r\n'));
  }
  parts.push(Buffer.from(`--${boundary}--\r\n`));
  return { payload: Buffer.concat(parts), contentType: `multipart/form-data; boundary=${boundary}` };
}

async function routeApp(dependencies: Parameters<typeof registerSolverContractValidationRoutes>[1]) {
  const app = Fastify();
  await app.register(multipart);
  app.addHook('preHandler', async (req) => {
    req.user = { username: String(req.headers['x-test-user'] ?? 'alice'), role: 'user' } as any;
  });
  registerSolverContractValidationRoutes(app, dependencies);
  await app.ready();
  return app;
}

test('验证接口校验归属、数据源和忙碌状态，并把 ZIP 中的包 ID 与版本交给编排', async () => {
  const project = createProject({ owner: 'alice', name: '合成求解验证项目', kind: 'package' });
  const bare = createProject({ owner: 'alice', name: '合成未配置数据源项目', kind: 'package' });
  saveProjectDataSource({ projectId: project.id, mode: 'none', workspaceId: shadowWorkspaceId(project.id) });
  const zip = zipPackage(renderPackage(packageSpec()));
  const cases = JSON.stringify([{ id: 'n1', operation_key: 'plan_route', arguments: batches(1) }]);
  const seen: any[] = [];
  let busy = false;
  const app = await routeApp({
    isEvaluationActive: () => busy,
    authSessions: { get: () => undefined },
    run: async (input) => {
      seen.push(input);
      return { package: { id: input.ontologyId, version: input.version }, cases: [] };
    },
  });
  const post = (projectId: string, fields: Record<string, string>, file: Uint8Array | undefined = zip, user = 'alice') => {
    const body = multipartBody(fields, file);
    return app.inject({
      method: 'POST',
      url: `/api/projects/${projectId}/solver-contract-validations`,
      headers: { 'content-type': body.contentType, authorization: 'Bearer synthetic-access', 'x-test-user': user },
      payload: body.payload,
    });
  };

  try {
    assert.equal((await post(project.id, { run_id: 'r1', cases }, zip, 'mallory')).statusCode, 404);
    const noSource = await post(bare.id, { run_id: 'r1', cases });
    assert.equal(noSource.statusCode, 409);
    assert.equal(noSource.json().code, 'EVALUATION_DATA_SOURCE_REQUIRED');
    busy = true;
    assert.equal((await post(project.id, { run_id: 'r1', cases })).json().code, 'SOLVER_VALIDATION_BUSY');
    busy = false;
    assert.equal((await post(project.id, { run_id: 'r1', cases: '[]' })).json().code, 'SOLVER_VALIDATION_CASES_INVALID');
    const broken = await post(project.id, { run_id: 'r1', cases }, new Uint8Array([1, 2, 3]));
    assert.equal(broken.statusCode, 422);
    assert.equal(broken.json().code, 'SOLVER_PACKAGE_INVALID');

    const ok = await post(project.id, { run_id: 'r1', cases });
    assert.equal(ok.statusCode, 200, ok.body);
    assert.deepEqual(ok.json(), { validation: { package: { id: ONTOLOGY_ID, version: VERSION }, cases: [] } });
    assert.equal(seen.length, 1);
    assert.equal(seen[0].projectId, project.id);
    assert.equal(seen[0].owner, 'alice');
    assert.equal(seen[0].idempotencyPrefix, 'r1');
    assert.equal(seen[0].cases[0].operationKey, 'plan_route');
    assert.equal(await seen[0].auth.getAccessToken(), 'synthetic-access');
  } finally {
    await app.close();
  }
});

test('编排阶段错误按稳定错误码返回', async () => {
  const project = createProject({ owner: 'alice', name: '合成错误映射项目', kind: 'package' });
  saveProjectDataSource({ projectId: project.id, mode: 'none', workspaceId: shadowWorkspaceId(project.id) });
  const app = await routeApp({
    isEvaluationActive: () => false,
    authSessions: { get: () => undefined },
    run: async () => {
      throw new SolverContractValidationError('CONSUMER_UNAVAILABLE', 'Consumer 暂不可用，请稍后重试', 502, 'workspace');
    },
  });
  try {
    const body = multipartBody(
      { run_id: 'r1', cases: JSON.stringify([{ id: 'n1', operation_key: 'plan_route', arguments: batches(1) }]) },
      zipPackage(renderPackage(packageSpec())),
    );
    const response = await app.inject({
      method: 'POST',
      url: `/api/projects/${project.id}/solver-contract-validations`,
      headers: { 'content-type': body.contentType, authorization: 'Bearer synthetic-access' },
      payload: body.payload,
    });
    assert.equal(response.statusCode, 502);
    assert.deepEqual(response.json(), {
      code: 'CONSUMER_UNAVAILABLE', error: 'Consumer 暂不可用，请稍后重试', stage: 'workspace',
    });
  } finally {
    await app.close();
  }
});
