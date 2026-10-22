import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

import multipart from '@fastify/multipart';
import Fastify from 'fastify';

import {
  agentReplyEvents,
  authoritativeResult,
  batches,
  cardName,
  fakeConsumer,
  json,
  multipartBody,
  ONTOLOGY_ID,
  packageSpec,
  solverCase,
  solverToolEvents,
  syntheticDataSource,
  VERSION,
  WORKSPACE_ID,
} from './helpers/solver-consumer-fake.js';

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

// 全部为合成数据：知识包、批次、工作空间与引用均为测试虚构。

function validate(request: any, cases: any[], extra: Record<string, unknown> = {}) {
  return runSolverContractValidation({
    projectId: 'pj_synthetic',
    owner: 'alice',
    auth: relayAuthSession('synthetic-access'),
    dataSource: syntheticDataSource,
    ontologyId: ONTOLOGY_ID,
    version: VERSION,
    zip: new Uint8Array([1, 2, 3]),
    cases: parseSolverValidationCases(cases),
    idempotencyPrefix: 'run1',
    request,
    ...extra,
  });
}

function singleCase(request: any, args: Record<string, unknown> = batches(1)) {
  const [item] = parseSolverValidationCases([solverCase('c1', args)]);
  return runSolverValidationCase({
    request,
    workspaceId: WORKSPACE_ID,
    ontologyId: ONTOLOGY_ID,
    version: VERSION,
    item,
    idempotencyKey: 'run1:c1',
    deadline: Date.now() + 60_000,
  });
}

const candidateQueries = (calls: { path: string }[]) => calls.filter((call) => call.path.includes('candidate-results'));

test('验证用例只接受合法的 operation_key、唯一 id、对象参数和非空提问', () => {
  const [item] = parseSolverValidationCases(
    '[{"id":"c1","operation_key":"plan_route","question":"求解","arguments":{"items":[]}}]',
  );
  assert.deepEqual(item, { id: 'c1', operationKey: 'plan_route', question: '求解', arguments: { items: [] } });
  const valid = (extra: Record<string, unknown> = {}) => ({
    id: 'c1', operation_key: 'a', question: '求解', arguments: {}, ...extra,
  });
  const rejects = (raw: unknown, pattern: RegExp) => assert.throws(
    () => parseSolverValidationCases(raw),
    (error: any) => error.code === 'SOLVER_VALIDATION_CASES_INVALID' && pattern.test(error.message),
  );
  rejects('not json', /JSON 数组/);
  rejects([], /至少/);
  rejects(Array.from({ length: 21 }, (_, i) => valid({ id: `c${i}` })), /最多 20/);
  rejects([valid({ operation_key: 'Plan-Route' })], /operation_key/);
  rejects([valid(), valid()], /重复/);
  rejects([valid({ arguments: [] })], /arguments/);
  rejects([valid({ id: 'c 1' })], /id 不合法/);
  rejects([valid({ question: '  ' })], /question/);
});

test('Consumer 不可用时整体失败，且不回传连接地址', async () => {
  const request = async () => {
    throw new Error('Consumer 请求失败：fetch failed（ECONNREFUSED：connect 192.0.2.10:8000）');
  };
  await assert.rejects(validate(request, [solverCase('c1', batches(1))]), (error: any) => {
    assert.ok(error instanceof SolverContractValidationError);
    assert.equal(error.code, 'CONSUMER_UNAVAILABLE');
    assert.equal(error.statusCode, 502);
    assert.equal(error.stage, 'workspace');
    assert.doesNotMatch(error.message, /192\.0\.2\.10/);
    return true;
  });
});

test('导入阶段被拒映射为包被拒，报错中的地址被隐藏', async () => {
  const consumer = fakeConsumer({
    importZip: () => json({ error: '校验失败，详见 https://consumer.example.com/docs' }, 422),
  });
  await assert.rejects(validate(consumer.request, [solverCase('c1', batches(1))]), (error: any) => {
    assert.equal(error.code, 'SOLVER_PACKAGE_REJECTED');
    assert.equal(error.stage, 'package');
    assert.doesNotMatch(error.message, /example\.com/);
    return true;
  });
});

test('导入、激活、数据源后逐用例向 Agent 提问，钉住精确版本并以运行 ID 派生幂等键', async () => {
  const consumer = fakeConsumer();
  const cases = [solverCase('n7', batches(7)), solverCase('n9', batches(9))];
  const report = await validate(consumer.request, cases);
  const order = consumer.calls.map((call) => `${call.method} ${call.path}`);
  assert.deepEqual(order, [
    'GET /agentic/workspaces',
    'POST /agentic/ontologies/import-zip',
    `PUT /agentic/ontologies/${ONTOLOGY_ID}/active-version`,
    `DELETE /agentic/ontologies/${ONTOLOGY_ID}/data-source`,
    'POST /agentic/runs',
    'GET /agentic/runs/agent-run-1/events?after_seq=0',
    `GET /api/solver/history/${encodeURIComponent('job-run1:n7')}/candidate-results`,
    'POST /agentic/runs',
    'GET /agentic/runs/agent-run-2/events?after_seq=0',
    `GET /api/solver/history/${encodeURIComponent('job-run1:n9')}/candidate-results`,
  ]);
  const runs = consumer.calls.filter((call) => call.path === '/agentic/runs');
  assert.deepEqual(runs.map((call) => call.headers['Idempotency-Key']), ['run1:n7', 'run1:n9']);
  assert.deepEqual(runs[0].body.arguments, { selected_ontology_id: ONTOLOGY_ID, selected_ontology_version: VERSION });
  assert.equal(runs[0].body.request, cases[0].question);
  assert.match(runs[0].body.session_id, /^mw-solver-[0-9a-f]{24}$/);
  assert.notEqual(runs[0].body.session_id, runs[1].body.session_id, '每条用例一个独立会话');
  assert.ok(consumer.calls.slice(4).every((call) => call.workspaceId === WORKSPACE_ID));
  assert.deepEqual(report.package, { id: ONTOLOGY_ID, version: VERSION });
  assert.deepEqual(
    report.cases.map((item) => [
      item.id, item.status, item.authoritative, item.candidate_count, item.solver_tool, item.arguments_match,
    ]),
    [
      ['n7', 'completed', true, 14, cardName('plan_route'), true],
      ['n9', 'completed', true, 18, cardName('plan_route'), true],
    ],
  );
});

test('Agent 未调用对应求解工具或运行失败时如实标出，不当作求解结果', async () => {
  const consumer = fakeConsumer({
    agent: (ask) => {
      if (ask.idempotencyKey === 'run1:reply') return { events: agentReplyEvents('请先告诉我计划月份。') };
      if (ask.idempotencyKey === 'run1:other') {
        return {
          events: solverToolEvents({
            operationKey: 'other_route', arguments: ask.arguments, result: authoritativeResult('job-other'),
          }),
        };
      }
      return {
        events: [{
          event_type: 'turn.finished',
          payload: { status: 'failed', error: { error_code: 'MODEL_UNAVAILABLE', message: '模型暂不可用' } },
        }],
      };
    },
  });
  const report = await validate(consumer.request, [
    solverCase('reply', batches(1)),
    solverCase('other', batches(1)),
    solverCase('failed', batches(1)),
  ]);
  const [reply, other, failed] = report.cases;
  assert.deepEqual([reply.status, reply.code, reply.solver_tool_called, reply.authoritative], [
    'error', 'solver_tool_not_called', false, false,
  ]);
  assert.match(String(reply.detail), /请先告诉我计划月份/);
  assert.equal(other.code, 'solver_tool_not_called');
  assert.match(String(other.detail), new RegExp(cardName('other_route')));
  assert.deepEqual([failed.code, failed.phase, failed.detail], ['model_unavailable', 'agent', '模型暂不可用']);
  assert.equal('solver_tool_called' in failed, false, 'Agent 运行没有开始求解时不给调用结论');
  assert.equal(candidateQueries(consumer.calls).length, 0);
});

test('Agent 改写入参时标出不一致的路径；数字与数字字符串视为一致', async () => {
  const expected = batches(2);
  const consumer = fakeConsumer({
    agent: (ask) => {
      const changed = structuredClone(ask.arguments);
      changed.items[0].item_order = '1';
      changed.items[1].item_id = 'B9';
      return {
        events: solverToolEvents({
          operationKey: ask.operationKey, arguments: changed, result: authoritativeResult('job-changed'),
        }),
      };
    },
  });
  const [result] = (await validate(consumer.request, [solverCase('c1', expected)])).cases;
  assert.equal(result.solver_tool_called, true);
  assert.equal(result.arguments_match, false);
  assert.deepEqual(result.argument_diff, ['items[1].item_id']);
});

test('工具结果自称权威但缺少或对不上权威结果事件时，不算权威结果，也不读候选', async () => {
  const consumer = fakeConsumer({
    agent: (ask) => {
      const result = authoritativeResult(`job-${ask.idempotencyKey}`);
      const base = { operationKey: ask.operationKey, arguments: ask.arguments, result };
      if (ask.idempotencyKey === 'run1:missing') return { events: solverToolEvents({ ...base, authoritativeEvent: false }) };
      if (ask.idempotencyKey === 'run1:mismatch') {
        return { events: solverToolEvents({ ...base, authoritativeEvent: { problem_digest: 'sha256:synthetic-other' } }) };
      }
      return { events: solverToolEvents({ ...base, result: { ...result, authoritative: false } }) };
    },
  });
  const report = await validate(consumer.request, [
    solverCase('missing', batches(2)),
    solverCase('mismatch', batches(2)),
    solverCase('weak', batches(2)),
  ]);
  assert.deepEqual(report.cases.map((item) => [item.id, item.status, item.authoritative, item.code ?? '']), [
    ['missing', 'completed', false, 'solver_authoritative_event_missing'],
    ['mismatch', 'completed', false, 'solver_authoritative_event_mismatch'],
    ['weak', 'completed', false, ''],
  ]);
  assert.ok(report.cases.every((item) => item.candidate_count === null));
  assert.equal(candidateQueries(consumer.calls).length, 0);
});

test('工具输出被截断时用同一调用的 solver.* 事件补齐公开字段', async () => {
  const consumer = fakeConsumer({
    agent: (ask) => ({
      events: solverToolEvents({
        operationKey: ask.operationKey,
        arguments: ask.arguments,
        result: authoritativeResult('job-truncated'),
        outputPreview: '{"status":"completed","authoritative":true,"summary":{"rows":[',
      }),
    }),
  });
  const [result] = (await validate(consumer.request, [solverCase('c1', batches(3))])).cases;
  assert.deepEqual([result.status, result.authoritative, result.job_id, result.candidate_count], [
    'completed', true, 'job-truncated', 6,
  ]);
  assert.equal(result.problem_digest, 'sha256:synthetic-problem-job-truncated');
});

test('Consumer 拒绝求解或等待补充输入时回传阶段与原因，原因中的地址被隐藏', async () => {
  const consumer = fakeConsumer({
    agent: (ask) => {
      const result = ask.idempotencyKey === 'run1:snapshot'
        ? {
          status: 'error', code: 'fact_snapshot_invalid', phase: 'snapshot', retryable: false,
          message: 'business data table synthetic_table is empty, see https://graph.example.net',
        }
        : { status: 'awaiting_input', phase: 'bind', missing: ['plan_month'], authoritative: false };
      return {
        events: solverToolEvents({
          operationKey: ask.operationKey, arguments: ask.arguments, result,
          toolStatus: result.status === 'error' ? 'failed' : 'succeeded',
        }),
      };
    },
  });
  const [rejected, awaiting] = (await validate(consumer.request, [
    solverCase('snapshot', batches(1)),
    solverCase('awaiting', batches(1)),
  ])).cases;
  assert.deepEqual([rejected.status, rejected.code, rejected.phase, rejected.retryable, rejected.authoritative], [
    'error', 'fact_snapshot_invalid', 'snapshot', false, false,
  ]);
  assert.equal(rejected.detail, 'business data table synthetic_table is empty, see [地址已隐藏]');
  assert.deepEqual([awaiting.status, awaiting.missing, awaiting.authoritative], ['awaiting_input', ['plan_month'], false]);
});

test('候选读取失败带诊断码，结果只保留公开字段', async () => {
  const consumer = fakeConsumer({
    agent: (ask) => ({
      events: solverToolEvents({
        operationKey: ask.operationKey,
        arguments: ask.arguments,
        result: {
          ...authoritativeResult('job-x'),
          solver_endpoint: 'https://solver.example.net',
          summary: { blob: 'x'.repeat(5000) },
        },
      }),
    }),
    candidates: () => ({ status: 409, body: { code: 'candidate_digest_mismatch' } }),
  });
  const [result] = (await validate(consumer.request, [solverCase('c1', batches(2))])).cases;
  assert.equal(result.authoritative, true);
  assert.equal(result.candidate_count, null);
  assert.equal(result.candidate_results_error, 'candidate_digest_mismatch');
  assert.equal('solver_endpoint' in result, false);
  assert.equal('summary' in result, false, '超过 4 KB 的摘要不回传');
});

test('受理失败与事件流失败按用例错误回传，不中断其余用例', async () => {
  const consumer = fakeConsumer({
    agent: (ask) => (ask.idempotencyKey === 'run1:rejected'
      ? {
        admission: {
          status: 500,
          body: { error: { error_code: 'INTERNAL_ERROR', retriable: false, detail: '详见 https://consumer.example.com' } },
        },
      }
      : undefined),
  });
  const report = await validate(consumer.request, [solverCase('rejected', batches(1)), solverCase('ok', batches(1))]);
  const [rejected, ok] = report.cases;
  assert.deepEqual([rejected.status, rejected.code, rejected.phase, rejected.retryable], [
    'error', 'internal_error', 'admission', false,
  ]);
  assert.doesNotMatch(String(rejected.detail), /example\.com/);
  assert.deepEqual([ok.status, ok.authoritative], ['completed', true]);

  const lost = await singleCase(async (_token: string, path: string) => (path === '/agentic/runs'
    ? json({ run_id: 'agent-run-x' }, 202)
    : json({ error: { error_code: 'RUN_NOT_FOUND', retriable: false } }, 404)));
  assert.deepEqual([lost.status, lost.code, lost.phase], ['error', 'run_not_found', 'events']);
  const bare = await singleCase(async () => json({}, 500));
  assert.deepEqual([bare.code, bare.phase, 'detail' in bare], ['http_500', 'admission', false]);
});

test('超过截止时间后剩余用例记为超时，不再提问', async () => {
  let clock = 0;
  const consumer = fakeConsumer({
    agent: () => {
      clock = 10_000;
      return undefined;
    },
  });
  const report = await validate(consumer.request, [solverCase('c1', batches(1)), solverCase('c2', batches(1))], {
    now: () => clock,
    deadlineMs: 5_000,
  });
  assert.deepEqual(report.cases.map((item) => [item.id, item.status, item.code ?? '']), [
    ['c1', 'completed', ''],
    ['c2', 'error', 'solver_timeout'],
  ]);
  assert.equal(consumer.calls.filter((call) => call.path === '/agentic/runs').length, 1);
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
  const cases = JSON.stringify([solverCase('n1', batches(1))]);
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
      { run_id: 'r1', cases: JSON.stringify([solverCase('n1', batches(1))]) },
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
